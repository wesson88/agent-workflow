"""
common.py - Skill 执行层共享工具（Phase 2b 起改为 vault-based）

本模块的关键变化（vs Phase 1）：
- read_skill_md / extract_dynamic_patch 移除 → 由 engine.role_loader 取代
- build_system_prompt 改为基于 vault 角色笔记
- get_*_dir 快捷方式移除 → 用 engine.config 的 project_dir / rules_dir 等
- update_skill_status / load_status / save_status 移除 → 用 engine.state.set_role_status

仍保留在本模块的：
- parse_args（CLI，新增 --project）
- read_input_files（文件批量读取）
- write_output_atomic（原子写入）
- parse_claude_output_to_files（解析 <!-- FILE: --> 标签）
- call_claude（Anthropic API 调用，max_tokens/model 自动从角色 frontmatter 读取）
- append_audit / utc_now（审计日志，Phase 4 会迁到 vault 复盘记录）
"""

from __future__ import annotations

import re
import sys
import json
import argparse
from pathlib import Path
from datetime import datetime, timezone
from tempfile import NamedTemporaryFile

# Windows 控制台默认 gbk，主动重配 stdout/stderr 为 utf-8，
# 让中文 + emoji 能正常打印（main.py 加载本模块即生效）
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

# 让 main.py（通过 sys.path.insert 把 skills/ 目录加入路径后）能 import engine
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from engine import load_role, RoleNotFound  # noqa: E402
from engine.config import PROJECT_ROOT  # noqa: E402
from engine.llm import call_claude as _llm_call_claude  # noqa: E402


# ── CLI ─────────────────────────────────────────────────
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--task", required=True,
        help="任务描述（必填）",
    )
    parser.add_argument(
        "--project", default=None,
        help="项目名（缺省从环境变量 PROJECT/PROJECT_NAME 读取，最终默认 'default'）",
    )
    parser.add_argument(
        "--sub-skill", default=None, dest="sub_skill",
        help="子技能名称（可选，部分技能用）",
    )
    return parser.parse_args()


# ── Claude 输出格式规范 ───────────────────────────────────
OUTPUT_FORMAT_SPEC = """
## 输出格式规范（强制遵守）

当你需要产出文件时，使用以下标签格式包裹每个文件的内容：

<!-- FILE: 相对路径/文件名.ext -->
文件内容
<!-- /FILE -->

约束：
- 你**不可调用任何工具**（不要使用 Read/Write/Edit/Bash/MCP 等）
- 你**不可询问写入权限** —— 上层 main.py 会负责落盘
- 路径规则（**严格遵守**）：
  - vault 笔记：以 `10-项目/{project}/...`、`00-系统/...`、`20-知识/...`、`99-临时/...` 之一开头
  - 代码与测试：必须以 `src/...` 或 `tests/...` 开头（**不要**裸 `main.py` 或 `app/main.py`）
  - 项目专属的 README / requirements / 静态资源：放到 `10-项目/{project}/` 下（如
    `10-项目/{project}/README.md`、`10-项目/{project}/requirements.txt`、
    `10-项目/{project}/static/index.html`），**不要**用裸 `README.md` / `requirements.txt`
    （那些路径会落到引擎仓根，覆盖引擎自身文件）
  - 仓根配置文件（仅在确实需要 pytest/构建工具自动发现时用）：`pytest.ini`、`conftest.py`、
    `pyproject.toml`、`package.json`。其余一切**禁止**裸文件名输出
  - 路径不得包含空格
- 一次响应可包含多个 FILE 块；每个文件**必须**有完整的 `<!-- FILE: -->` 开始 + `<!-- /FILE -->` 结束
- 代码文件无需额外的 Markdown 代码块包裹
- `{project}` 占位符在 user prompt 中已替换为实际项目名，请直接使用

如果 user prompt 列举了"必须产出的文件清单"，你的响应**必须为每一项产出对应的 FILE 块**，缺一不可。
"""


def render_required_outputs(paths: list[str]) -> str:
    """生成强约束的 FILE 块输出清单，供 user_prompt 末尾使用。"""
    if not paths:
        return ""
    examples = "\n\n".join(
        f"<!-- FILE: {p} -->\n（此处填入 {p} 的完整内容）\n<!-- /FILE -->"
        for p in paths
    )
    return (
        "\n\n---\n"
        "**输出格式（强制，违反将导致解析失败）**：\n\n"
        "请按以下结构输出，每个文件一段 FILE 块。**禁止调用任何工具、禁止询问权限**，"
        "直接产出文本即可：\n\n"
        f"{examples}\n"
    )


# ── system prompt 拼装 ───────────────────────────────────
_DYNAMIC_RE = re.compile(
    r"<!-- DYNAMIC_START -->(.*?)<!-- DYNAMIC_END -->",
    re.DOTALL,
)


# 闭环验证证据行：复盘者跨次对比用，对当前角色 LLM 执行无价值，剥离省 token。
# 形如 `- 闭环验证 [2026-05-10]: 第 3 项目 _quiz-game ...` 整行剥除。
_EVIDENCE_LINE_RE = re.compile(
    r"^[ \t]*-\s*闭环验证\s*\[[^\]]+\][:：].*\n?",
    re.MULTILINE,
)


def _strip_evidence_lines(text: str) -> str:
    """剥离 DYNAMIC 区的"闭环验证"证据行（系统认知图谱 §12 P0 token 控制）。

    证据行是复盘者每轮往 DYNAMIC 累加的"上轮补丁是否被消费"对照证据，
    用于判定 [GRADUATE?] / [DROP?] 生命周期。对正在执行的工作角色 LLM
    无意义——它只需要"补丁约束本身"，不需要看历史命中证据。

    实测剥离效果（2026-05-10）：后端 -33% / 前端 -28% / 架构师 -16%。
    单步系统提示节约 1-1.5K tokens。

    复盘者读工作角色笔记走 `_gather_worker_role_notes` 直接读 vault 全文，
    不走 build_system_prompt，证据行对复盘者仍可见。
    """
    return _EVIDENCE_LINE_RE.sub("", text)


def _extract_dynamic_patch(body: str) -> str:
    """从角色笔记正文里抽出 DYNAMIC 区域的有效补丁（过滤注释行）。

    取**最后一对** DYNAMIC_START/DYNAMIC_END：角色笔记的 §3.1 / §4 等说明
    段经常字面引用 `<!-- DYNAMIC_START -->` / `<!-- DYNAMIC_END -->` marker
    （在反引号内），non-greedy `.*?` 会误抓到首个 marker → 末尾 marker
    之间的内容，包括所有正文。固定取最后一对就是真正的控制区。

    过滤两类无效行：
    - markdown 注释 `# 这是注释`（含模板说明 / 占位符行）
    - HTML 注释 `<!-- 元角色不接收自身补丁 -->`（元角色 DYNAMIC 区惯用占位）
    """
    matches = list(_DYNAMIC_RE.finditer(body))
    if not matches:
        return ""
    text = matches[-1].group(1).strip()
    keep = []
    for l in text.splitlines():
        s = l.strip()
        if not s:
            continue
        if s.startswith("#"):                         # markdown 注释
            continue
        if s.startswith("<!--") and s.endswith("-->"):  # HTML 注释
            continue
        keep.append(l)
    return "\n".join(keep).strip()


def build_system_prompt(role_name_or_alias: str, project: str | None = None) -> str:
    """从 vault 加载角色笔记，组装 system prompt。

    结构：
        本角色 frontmatter 摘要
        本角色正文（含 DYNAMIC 区域；闭环验证证据行已剥离）
        上游角色的 DYNAMIC 补丁（已剥离证据行，如有）
        OUTPUT_FORMAT_SPEC

    Token 控制（系统认知图谱 §12 P0）：剥离 DYNAMIC 区的"闭环验证"证据行。
    """
    role = load_role(role_name_or_alias)

    summary = [
        f"角色：{role.name}",
        f"领域：{role.domain}",
        f"风格：{role.style}",
    ]
    if role.skills:
        summary.append(f"技能:{', '.join(role.skills)}")

    parts = [
        "## 角色摘要",
        "\n".join(summary),
        "",
        _strip_evidence_lines(role.body.strip()),
    ]

    # 上游补丁注入（保留 Phase 1 的"上游通过 DYNAMIC 影响下游"机制）
    for upstream_name in role.upstream:
        try:
            up_role = load_role(upstream_name)
        except RoleNotFound:
            continue
        patch = _strip_evidence_lines(_extract_dynamic_patch(up_role.body))
        if patch.strip():
            parts.append("")
            parts.append(f"## 上游角色 [{up_role.name}] 动态补丁指令")
            parts.append(patch)

    parts.append(OUTPUT_FORMAT_SPEC)
    return "\n".join(parts)


# ── 输入文件批量读取 ──────────────────────────────────────────────────────────────
def _extract_sections(content: str, sections: list[str]) -> str:
    """从 Markdown 文档中只提取指定章节（## 标题匹配）。
    匹配规则：标题文字包含 section 关键词即命中（大小写不敏感）。
    若无任何章节命中，返回原文并附加警告。
    """
    if not sections:
        return content
    lines = content.splitlines(keepends=True)
    result: list[str] = []
    in_section = False
    current_level = 0
    for line in lines:
        heading = None
        for lvl in range(1, 7):
            prefix = "#" * lvl + " "
            if line.startswith(prefix):
                heading = (lvl, line[lvl + 1:].strip())
                break
        if heading:
            lvl, title = heading
            # 检查是否命中目标章节
            is_target = any(s.lower() in title.lower() for s in sections)
            if is_target:
                in_section = True
                current_level = lvl
                result.append(line)
            elif in_section and lvl <= current_level:
                # 遇到同级或更高级标题，退出当前章节
                in_section = False
            elif in_section:
                result.append(line)
        elif in_section:
            result.append(line)
    if not result:
        section_list = ", ".join(sections)
        return (
            content
            + f"\n\n⚠️ [sections 警告] 未找到章节 [{section_list}]，已返回全文。"
        )
    return "".join(result)


def read_input_files(
    file_paths: list,
    max_chars_per_file: int = 25000,
    max_total_chars: int = 80000,
) -> str:
    """合并多个输入文件为带分隔符的上下文块，供 user prompt 使用。
    § 15 上游堆积治理（层二：引擎截断兜底）：

    - max_chars_per_file：单文件超限时截断并追加警告，防止单一大文件打爆上下文
    - max_total_chars：所有文件合计超限时，按声明顺序优先保留，丢弃末尾文件

    层二扩展（section 选择器）：
    file_paths 中每个元素可以是：
      - str / Path：直接读取整个文件（原有行为）
      - dict：{ "path": ..., "max_chars": ..., "sections": [...] }
        max_chars  覆盖默认单文件上限
        sections   只提取指定 ## 章节（关键词匹配）

    文件不存在或读取失败时不阻断流程，写入占位说明。
    """
    parts = []
    total_chars = 0
    for fp_entry in file_paths:
        # 解析结构化 entry
        if isinstance(fp_entry, dict):
            fp = Path(fp_entry["path"])
            file_max = int(fp_entry.get("max_chars", max_chars_per_file))
            sections = fp_entry.get("sections") or []
        else:
            fp = Path(fp_entry)
            file_max = max_chars_per_file
            sections = []

        if fp.exists() and fp.is_file():
            try:
                content = fp.read_text(encoding="utf-8")
            except Exception as e:
                content = f"（读取失败：{e}）"
        else:
            content = "（文件不存在或为空）"

        # 层二扩展：章节裁剪（在截断前做，尽量保留有效内容）
        if sections:
            content = _extract_sections(content, sections)

        # 单文件截断
        if len(content) > file_max:
            original_len = len(content)
            content = content[:file_max]
            content += (
                f"\n\n⚠️ [截断警告] 原文 {original_len} 字符，"
                f"已截取前 {file_max} 字符。"
                f"请检查角色产出体积是否超出约束（§15 层一：≤30KB）。"
            )
            print(
                f"[read_input_files] ⚠️ {fp.name} 超过单文件限制"
                f"（{original_len} > {file_max} chars），已截断。",
                file=sys.stderr,
            )

        block = f"=== {fp.name} ===\n{content}\n==="
        block_len = len(block)

        # 总量截断：超出后丢弃后续文件
        if total_chars + block_len > max_total_chars:
            remaining = max_total_chars - total_chars
            if remaining > 500:
                block = block[:remaining] + (
                    f"\n\n⚠️ [总量截断] 已达 {max_total_chars} 字符上限，"
                    f"{fp.name} 剩余内容及后续文件已丢弃。"
                )
                parts.append(block)
            else:
                parts.append(
                    f"=== {fp.name} ===\n"
                    f"⚠️ [总量截断] 已达 {max_total_chars} 字符上限，本文件已跳过。\n==="
                )
            print(
                f"[read_input_files] ⚠️ 总输入量超过 {max_total_chars} chars 上限，"
                f"从 {fp.name} 起截断，后续文件丢弃。",
                file=sys.stderr,
            )
            break

        parts.append(block)
        total_chars += block_len

    return "\n\n".join(parts)


# ── 输出文件原子写入（带 Windows 重试）───────────────────
from engine.obsidian_io import _atomic_replace_with_retry  # noqa: E402


def write_output_atomic(dest_path: Path, content: str) -> None:
    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w",
        dir=dest_path.parent,
        delete=False,
        encoding="utf-8",
        suffix=".tmp",
        newline="\n",
    ) as tf:
        tf.write(content)
        tmp = tf.name
    _atomic_replace_with_retry(tmp, dest_path)


# ── Claude 多文件输出解析 ────────────────────────────────
_FILE_BLOCK_RE = re.compile(
    r"<!--\s*FILE:\s*(.+?)\s*-->\n(.*?)<!--\s*/FILE\s*-->",
    re.DOTALL,
)

# 匹配文件首尾被 markdown 代码围栏包裹的情况：
#   ```python
#   ... 实际代码 ...
#   ```
# Claude 偶尔违反 OUTPUT_FORMAT_SPEC 给代码加围栏，写入磁盘前剥离一层。
# 仅当首尾各有一对围栏时才剥离，避免误删合法 markdown 内的代码块。
_LEADING_FENCE_RE = re.compile(
    r"\A\s*```[^\n`]*\n",   # 开始：```（可选语言标签）+ 换行
)
_TRAILING_FENCE_RE = re.compile(
    r"\n```\s*\Z",          # 结尾：换行 + ```
)

# 匹配纯 HTML/markdown 注释占位（如 __init__.py 被写成 `<!-- empty -->`）：
# Claude 偶尔在"应该空文件"的 FILE 块里塞一行注释当占位，但 .py 解释器
# 会把它当语法错误。检测全文都是 <!-- ... --> 注释时，写空文件。
_PURE_COMMENT_RE = re.compile(
    r"\A\s*(?:<!--.*?-->\s*)+\Z",
    re.DOTALL,
)


def _strip_outer_code_fence(content: str) -> str:
    """若 content 整体被一对 markdown 代码围栏包裹，剥离外层。

    保守策略：只在 **同时** 检测到首尾匹配的围栏时剥离，避免误伤含
    内嵌代码块的 markdown 文档。
    """
    head = _LEADING_FENCE_RE.search(content)
    tail = _TRAILING_FENCE_RE.search(content)
    if not head or not tail:
        return content
    inner = content[head.end():tail.start()]
    # 保证文件末尾有换行
    return inner if inner.endswith("\n") else inner + "\n"


def _normalize_empty_file_placeholder(content: str) -> str:
    """若 content 仅包含 HTML/markdown 注释（无实际代码），写空文件。

    场景：Claude 在 `__init__.py` 等本应空的 FILE 块里写
        <!-- empty -->
    或
        <!-- empty – marks src/backend as a Python package -->
    这些进 .py 文件会触发 SyntaxError。
    """
    if _PURE_COMMENT_RE.match(content):
        return ""
    return content


def parse_claude_output_to_files(raw_output: str) -> dict:
    """解析 Claude 输出中的 <!-- FILE: path --> ... <!-- /FILE --> 块。

    返回 {相对路径: 内容}。注意路径中的 {project} 占位符不在此处替换，
    由调用方在写盘前用 engine.config.resolve_path 处理。
    自动剥离整体被 markdown 代码围栏包裹的内容（Claude 偶尔违反约定）。
    """
    results = {}
    for m in _FILE_BLOCK_RE.finditer(raw_output):
        rel = m.group(1).strip()
        content = _strip_outer_code_fence(m.group(2))
        content = _normalize_empty_file_placeholder(content)
        results[rel] = content
    return results


# ── 时间与审计 ───────────────────────────────────────────
def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def append_audit(entry: dict) -> None:
    """追加一条审计日志到 .claude/audit.jsonl（Phase 4 会迁到 vault 复盘记录）。"""
    audit_path = PROJECT_ROOT / ".claude" / "audit.jsonl"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with open(audit_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ── Claude API 调用 ──────────────────────────────────────
_DEFAULT_MAX_TOKENS = 4096
_DEFAULT_MODEL = "claude-sonnet-4-6"


def call_claude(system_prompt: str, user_prompt: str, role_name_or_alias: str) -> str:
    """Streaming 调用 Claude；max_tokens / model 从角色 frontmatter 读取。

    底层路由由 engine.llm 处理：API key 在则走 Anthropic SDK，
    否则走 `claude --print` CLI（用户的 Claude Code MAX 订阅）。
    """
    try:
        role = load_role(role_name_or_alias)
        max_tokens = role.max_tokens
        model = role.model
        display_name = role.name
    except RoleNotFound:
        max_tokens = _DEFAULT_MAX_TOKENS
        model = _DEFAULT_MODEL
        display_name = role_name_or_alias

    print(
        f"[{display_name}] 调用 Claude (model={model}, max_tokens={max_tokens})...",
        flush=True,
    )

    return _llm_call_claude(
        system_prompt, user_prompt,
        model=model, max_tokens=max_tokens,
    )
