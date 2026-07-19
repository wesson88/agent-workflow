"""
common.py - Skill 执行层共享工具（Phase 2b 起改为 vault-based）

Phase 5 重构：本文件已按职责拆分为四个子模块：
  - prompt_builder.py：build_system_prompt / OUTPUT_FORMAT_SPEC / render_required_outputs
  - input_reader.py：read_input_files / _extract_sections
  - output_parser.py：parse_claude_output_to_files / write_output_atomic
  - audit.py：append_audit / utc_now

本文件继续作为向后兼容的 re-export 聚合入口，各 skill/main.py 无需修改 import。

仍在本模块的核心功能：
  - parse_args / resolve_project（CLI）
  - call_claude（Anthropic API 调用）
  - check_size_limit / compress_to_limit / enforce_output_limits（层一体积控制）
"""

from __future__ import annotations

import os
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
from engine.llm import call_llm as _llm_call_llm  # noqa: E402


# ── CLI ─────────────────────────────────────────────────
def resolve_project(args: argparse.Namespace) -> str:
    """从 CLI 参数或环境变量解析项目名，最终默认 'default'。

    优先级：--project > $PROJECT > $PROJECT_NAME > 'default'
    集中到 common.py，各 skill/main.py 无需重复实现。
    """
    raw = (
        args.project
        or os.environ.get("PROJECT")
        or os.environ.get("PROJECT_NAME")
        or "default"
    )
    return raw.strip() or "default"


def resolve_module_id(args: argparse.Namespace | None = None) -> str | None:
    """P8.6：从 CLI 参数或环境变量解析当前单模块聚焦的 module id。

    优先级：--module-id > $AGENT_SELECTED_MODULE_ID > None
    - 非模块化 workflow / 传统 dispatch 模式：返回 None，engineer 走原全量输入路径
    - 模块化 workflow：module_development_loop node 塞 env，engineer 读到后
      触发 §6 单模块聚焦模式（仅输出 selected_module_id 对应文件）

    args 可为 None（部分入口不通过 argparse）。
    """
    module_id = None
    if args is not None:
        module_id = getattr(args, "module_id", None)
    if not module_id:
        module_id = os.environ.get("AGENT_SELECTED_MODULE_ID", "").strip() or None
    return module_id.strip() if isinstance(module_id, str) and module_id.strip() else None


def render_module_focus_hint(module_id: str | None, project: str) -> str:
    """P8.6：把 module_id 转成 user_prompt 头部的单模块聚焦约束段。

    module_id=None → 返回空串（非模块化 workflow 全兼容）
    非空 → 返回一段明确约束的 markdown 文本，告诉 LLM：
    - 本轮只输出该模块相关文件
    - 模块清单.md 路径 + 单模块详情路径供读取
    - 额外产出：进度流 + 测试报告
    """
    if not module_id:
        return ""
    return (
        "## 【单模块聚焦模式】\n\n"
        f"本次聚焦模块 **{module_id}**（由 workflow 模块化开发循环选中）。\n\n"
        "**只输出以下类型的 FILE 块**：\n"
        f"1. 模块 {module_id} 对应的实现代码（src/backend/... 或 src/frontend/...）\n"
        f"2. 模块 {module_id} 对应的测试代码（tests/backend/... 或 tests/frontend/...）\n"
        f"3. 进度流：`10-项目/{project}/进度/{module_id}-progress.md`\n"
        f"4. 测试报告：`10-项目/{project}/测试报告/{module_id}.md`\n\n"
        "**不要输出**：\n"
        "- 其他模块的实现（下一轮 workflow 再跑）\n"
        "- 全量 API 契约（若非当前模块所属功能，留给该模块的负责轮次）\n\n"
        "**上下文读取**：\n"
        f"- 模块清单：`10-项目/{project}/模块清单.md`（含 DAG 与 status）\n"
        f"- 模块详情：`10-项目/{project}/模块/{module_id}-*.md`（本模块 spec）\n"
        f"- 系统设计与 PRD：正常读取\n\n"
        "---\n\n"
    )


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
    parser.add_argument(
        "--round", type=int, default=1, dest="round_num",
        help="脑暴轮次（多轮脑暴 skill 用，默认 1；其他 skill 忽略）",
    )
    return parser.parse_args()


def parse_targets(target_args: list[str] | None) -> set[str] | None:
    """元角色 CLI 共用 helper：统一解析 --target 参数。

    支持调用形态（任选其一，可混合）：
      --target 后端工程师                 # 单个
      --target 后端工程师 --target 前端工程师   # 重复
      --target 后端工程师,前端工程师        # 逗号分隔
      --target all                       # 显式全部（同缺省）
      （不传 --target）                    # 默认全部

    返回 None = 全量（默认行为）；返回非空 set = 显式过滤集。
    """
    if not target_args:
        return None
    out: set[str] = set()
    for v in target_args:
        if not v:
            continue
        for item in v.split(","):
            item = item.strip()
            if not item or item.lower() == "all":
                continue
            out.add(item)
    return out or None


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


# ── T2.7 白名单契约：业务角色严格 §1-§6 / 元角色全豁免 ─────────────────
_SECTION_HEADING_RE = re.compile(r"^(##\s+)(\d+)(\.\d+)?\.\s*(.+)$", re.MULTILINE)
_VERSION_HISTORY_RE = re.compile(
    r"^##\s+\d+\.\s*版本历史.*?(?=^##\s+|\Z)",
    re.MULTILINE | re.DOTALL,
)


def _strip_version_history(text: str) -> str:
    """剥除"## N. 版本历史"章节（含整段内容）。"""
    return _VERSION_HISTORY_RE.sub("", text).rstrip() + "\n"


def _strip_dynamic_block(text: str) -> str:
    """剥除 DYNAMIC marker 间内容 + 其外层 "## N. 运行时补丁"标题段。

    元角色 system prompt 不需要 DYNAMIC 区（独立路径处理）+ 控制区说明。
    保留其他章节完整。
    """
    # 剥 DYNAMIC marker 间内容（多个对都剥）
    text = re.sub(
        r"<!-- DYNAMIC_START -->.*?<!-- DYNAMIC_END -->",
        "",
        text,
        flags=re.DOTALL,
    )
    # 剥 "## N. 运行时补丁..." 标题段（到下一个 ##）
    text = re.sub(
        r"^##\s+\d+\.\s*运行时补丁.*?(?=^##\s+|\Z)",
        "",
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    return text.rstrip() + "\n"


def _extract_sections_by_range(body: str, start_n: int, end_n: int) -> str:
    """从 body 抽取 §start_n ~ §end_n 章节（含子节 §N.x）。

    遇到 §(end_n+1) 或更大序号或文末停。保留 H1 标题。
    """
    lines = body.splitlines(keepends=True)
    result: list[str] = []
    in_range = False
    # 先把 H1（# 角色：...）拿出来
    for line in lines:
        if line.startswith("# ") and not line.startswith("## "):
            result.append(line)
            break
    # 抽 §start_n ~ §end_n
    for line in lines:
        m = re.match(r"^##\s+(\d+)(\.\d+)?\.\s*", line)
        if m:
            n = int(m.group(1))
            if start_n <= n <= end_n:
                in_range = True
                result.append(line)
            else:
                in_range = False
        elif in_range:
            result.append(line)
    return "".join(result)


def _extract_role_prompt_sections(body: str, domain: str) -> tuple[str, str]:
    """T2.7 白名单契约：业务角色严格 §1-§6 / 元角色全 body 减 DYNAMIC + 版本历史。

    返回 (text, path_used)：
      - path_used="business_strict"：domain ≠ 元，按 §1-§6 严格抽取
      - path_used="meta_full"：domain == 元，全 body 减 DYNAMIC + 版本历史

    业务角色 §1-§6 任一缺失或乱序 → raise RuntimeError 阻断主路径。
    """
    if domain == "元":
        text = _strip_dynamic_block(body)
        text = _strip_version_history(text)
        return text.strip(), "meta_full"

    # 业务角色严格 §1-§6
    sections = _SECTION_HEADING_RE.findall(body)
    if not sections:
        raise RuntimeError(
            f"业务角色基因结构不合规：未找到任何 ## N. 标题（domain={domain}）"
        )
    # 收集顶层章节序号（不含子节 §N.x）
    top_nums = sorted({int(n) for _, n, sub, _ in sections if not sub})
    if not top_nums:
        raise RuntimeError(
            f"业务角色基因未找到顶层 ## N. 章节（domain={domain}）"
        )
    missing = [i for i in range(1, 7) if i not in top_nums]
    if missing:
        raise RuntimeError(
            f"业务角色基因 §1-§6 缺章：缺 {missing}（domain={domain}，"
            f"实际章节 {top_nums}）"
        )
    text = _extract_sections_by_range(body, 1, 6)
    return text.strip(), "business_strict"


# B4（P10.5）：DYNAMIC 补丁 label 过滤 —— 只把 [KEEP] / [GRADUATE?] 状态的
# 补丁进执行 prompt；[NEW] / [DROP?] 属于复盘者视角的实验状态，不该干扰
# 正在跑的角色 LLM。**依据**：capability 注册表规范 §6 生命周期约定 +
# 2026-07-05 code review Explore-2 分析（DYNAMIC 段占 250-1250 tokens/call）。
#
# 匹配格式：`# Patch [<时间>] [<label>] <标题>`
# 补丁块从含 `# Patch [...] [<label>]` 的注释行开始，直到下一个 `# Patch` 或末尾。
_PATCH_HEADER_RE = re.compile(
    r"^\s*#\s*Patch\s*\[[^\]]+\]\s*\[(?P<label>[A-Z?]+)\]",
    re.IGNORECASE,
)
# 只有这些 label 会进执行 prompt。[NEW] 补丁太新（还没实战证据），[DROP?]
# 已被复盘者标记为待淘汰，两者不该继续污染 prompt。
_EXECUTION_KEEP_LABELS = frozenset({"KEEP", "GRADUATE?"})


def _extract_dynamic_patch(body: str) -> str:
    """从角色笔记正文里抽出 DYNAMIC 区域的**执行相关**补丁。

    取**最后一对** DYNAMIC_START/DYNAMIC_END：角色笔记的 §3.1 / §4 等说明
    段经常字面引用 `<!-- DYNAMIC_START -->` / `<!-- DYNAMIC_END -->` marker
    （在反引号内），non-greedy `.*?` 会误抓到首个 marker → 末尾 marker
    之间的内容，包括所有正文。固定取最后一对就是真正的控制区。

    行级过滤（保留）：
    - 剥离 markdown 注释 `# 这是注释`（含模板说明 / 占位符行）
    - 剥离 HTML 注释 `<!-- 元角色不接收自身补丁 -->`（元角色 DYNAMIC 区惯用占位）

    **B4 patch-block 级过滤**（新增）：
    - 识别 `# Patch [<时间>] [<label>] <标题>` 起始的补丁块
    - 只保留 label ∈ {KEEP, GRADUATE?} 的补丁块；[NEW]/[DROP?] 剥除
    - 补丁块延伸到下一个 `# Patch [...]` 或末尾
    - 若整个 DYNAMIC 区无 `# Patch [...]` header（即老格式无 label），全保留
      （向后兼容：不破坏尚未引入 label 语义的 role）
    """
    matches = list(_DYNAMIC_RE.finditer(body))
    if not matches:
        return ""
    text = matches[-1].group(1).strip()

    # 老流程：先做行级注释剥离
    stripped_lines: list[str] = []
    for l in text.splitlines():
        s = l.strip()
        if not s:
            stripped_lines.append(l)  # 保留空行帮助补丁块分隔
            continue
        if s.startswith("<!--") and s.endswith("-->"):
            continue
        stripped_lines.append(l)

    # B4 patch-block 级过滤
    has_any_patch_header = any(
        _PATCH_HEADER_RE.match(l) for l in stripped_lines
    )
    if not has_any_patch_header:
        # 兼容老 DYNAMIC 区：无 label 语义 → 保留行级过滤结果（并剥离 markdown 注释）
        return "\n".join(
            l for l in stripped_lines
            if not l.strip().startswith("#")
        ).strip()

    # 有 `# Patch [...]` header：按补丁块切分，只保留 KEEP / GRADUATE?
    kept_blocks: list[str] = []
    current_block: list[str] = []
    current_kept = False
    for l in stripped_lines:
        m = _PATCH_HEADER_RE.match(l)
        if m:
            # 上一个补丁块收尾
            if current_kept and current_block:
                kept_blocks.append("\n".join(current_block))
            label = m.group("label").upper()
            current_kept = label in _EXECUTION_KEEP_LABELS
            current_block = [l] if current_kept else []
        elif current_kept:
            current_block.append(l)
    # 尾块
    if current_kept and current_block:
        kept_blocks.append("\n".join(current_block))
    return "\n\n".join(kept_blocks).strip()


def _read_env_contract_overrides() -> dict | None:
    """P8.2：从 AGENT_CONTRACT_OVERRIDES env 读契约参数注入（workflow 层透传）。

    workflow 层（graph/nodes.py::make_role_node）序列化 WorkflowStep.contract_overrides
    为 JSON 塞进 env；skill main.py 无需改代码，通过 build_system_prompt 自动生效。

    - env 缺失或空 → None（走影子模式，等同 P4-P7 现状）
    - env 是合法 JSON dict → 传给 load_role 作为 contract_overrides
    - env 是非法 JSON → 丢 stderr 警告后返回 None（fail-open，避免阻塞主链路）
    """
    raw = os.environ.get("AGENT_CONTRACT_OVERRIDES", "").strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        print(
            f"⚠️ AGENT_CONTRACT_OVERRIDES 解析失败（{e}），忽略 override 走影子模式",
            file=sys.stderr,
        )
        return None
    if not isinstance(parsed, dict):
        print(
            f"⚠️ AGENT_CONTRACT_OVERRIDES 顶层应为 dict，实际 {type(parsed).__name__}，忽略",
            file=sys.stderr,
        )
        return None
    return parsed


def _render_contract_summary(role) -> str:
    """P8.7 修：把契约展开结果暴露给 LLM，纠正 role.body 硬指令与 outputs 声明的冲突。

    P5b 已让 workflow 层通过 contract_overrides 替换 role.outputs / role.inputs，
    但只是数据层替换 —— LLM 看到的 system_prompt 仍是 role.body（硬指令走 legacy 模式）。
    本函数把 resolved_output_contract / resolved_input_contract 的 field_values +
    展开后的 outputs 清单塞进 system_prompt，让 LLM 明确"本轮契约参数是什么、
    应产出什么文件"，凌驾于 body 中的示例产出格式。

    - 无契约声明 → 返回空串（未契约化角色零影响）
    - 有契约但 field_values 与 outputs 都为空 → 空串
    - 有契约 → 一段 markdown，含参数表 + 产出清单 + 优先级声明
    """
    if not role.resolved_output_contract and not role.resolved_input_contract:
        return ""

    parts: list[str] = []
    if role.resolved_output_contract:
        rc = role.resolved_output_contract
        field_lines = [
            f"- **{k}**: `{v}`"
            for k, v in rc.field_values.items()
            if v is not None and str(v) != ""
        ]
        if field_lines:
            parts.append("## 【当前 output_contract 参数】\n\n" + "\n".join(field_lines))
        if role.outputs:
            outputs_lines = "\n".join(f"- `{o}`" for o in role.outputs)
            parts.append(
                "## 【本轮需产出的文件清单】\n\n"
                f"{outputs_lines}\n\n"
                "> **强制契约**：以上路径为本轮 workflow 声明的最终产出结构，"
                "**优先级高于本角色 md 正文中列举的示例产出格式**。若正文示例（如"
                "「给X-T0N.md」等 legacy 命名）与本清单冲突，一律以本清单为准，"
                "禁止产出正文示例但不在本清单里的路径。"
            )

    if role.resolved_input_contract:
        ric = role.resolved_input_contract
        field_lines = [
            f"- **{k}**: `{v}`"
            for k, v in ric.field_values.items()
            if v is not None and str(v) != ""
        ]
        if field_lines:
            parts.append("## 【当前 input_contract 参数】\n\n" + "\n".join(field_lines))
        if role.inputs:
            inputs_lines = "\n".join(f"- `{i}`" for i in role.inputs)
            parts.append(
                "## 【本轮输入文件清单】\n\n"
                f"{inputs_lines}\n\n"
                "> 输入路径由 workflow 契约声明；若正文示例路径与本清单冲突，"
                "以本清单为准。"
            )

    return "\n\n".join(parts)


_CAPABILITY_REF_ROOT_RE = None  # 懒编译，避免模块顶部 import re 触发 reload 副作用


def _capability_ref_root(ref: str) -> str | None:
    """从 wikilink `[[<root>/manifest]]` 提取 root。找不到返回 None。"""
    import re as _re
    global _CAPABILITY_REF_ROOT_RE
    if _CAPABILITY_REF_ROOT_RE is None:
        _CAPABILITY_REF_ROOT_RE = _re.compile(r"^([a-z0-9\-]+)(?:/.*)?$")
    target = ref.strip().strip("[]").split("|", 1)[0].split("#", 1)[0].strip()
    m = _CAPABILITY_REF_ROOT_RE.match(target)
    return m.group(1) if m else None


from functools import lru_cache as _lru_cache  # noqa: E402


@_lru_cache(maxsize=32)
def _render_capability_summary_cached(refs_tuple: tuple[str, ...], proj_hint: str) -> str:
    """P10.5 A2：capability 摘要注入按 (refs, project) 缓存。

    A4 修法：以前 load_manifest 挂 → 静默跳过；现改为 stderr warn（可观测性）。
    role 声明的 refs 加载真出错的 fail-closed 校验由 role_loader 层做（A4）。

    invalidate：manifest 或 refs 变化 → invalidate_capability_summary_cache()。
    """
    import sys as _sys
    from engine.capability_executor.base import ManifestValidationError
    from engine.capability_executor.manifest_loader import (
        load_manifest as _load_manifest,
        validate_manifest as _validate_manifest,
    )

    summaries: list[str] = []
    for ref in refs_tuple:
        root = _capability_ref_root(ref)
        if not root:
            continue
        try:
            # 复用 load_manifest 的 `<root>/xxx` 分派（也走 A2 lru_cache）
            manifest = _load_manifest(f"{root}/manifest")
            _validate_manifest(manifest)
        except ManifestValidationError as e:
            # A4 修：静默跳过 → stderr warn（可观测；不阻塞主链）
            print(
                f"[_render_capability_summary] ⚠️ 加载 capability '{root}' 失败："
                f"{e}（跳过；建议检查 20-知识/能力注册表/{root}/manifest.json）",
                file=_sys.stderr,
            )
            continue

        cap_id = manifest["id"]
        ver = manifest["version"]
        rt_type = manifest["runtime"]["type"]
        triggers = manifest.get("triggers", [])
        trig_preview = ", ".join(triggers[:5])
        summary = (
            f"- **{cap_id}** (v{ver}, runtime={rt_type})\n"
            f"  triggers: {trig_preview}\n"
            f"  invoke: `python -m engine.capability_executor.invoke "
            f"--id {cap_id} --project {proj_hint} --input k=v`"
        )
        # 硬截断到 400 chars（每段最多 ≈ 200 chars 摘要 + 200 chars 调用示例）
        # 依据：capability 注册表规范 §5.2 说的"~200 chars 摘要"指的是 manifest.summary
        # 段本身；含调用方式行后 400 chars 是合理上限
        if len(summary) > 400:
            summary = summary[:400] + "…"
        summaries.append(summary)

    if not summaries:
        return ""

    # R1 措辞加强（M4 实战驱动 · 2026-07-08）：原措辞"你可按需 invoke（不是总要调）"
    # 太软 → LLM 保守选内建。改为**默认 invoke，不 invoke 需 justify**的强推向措辞。
    header = (
        "## 【可调用能力（capability_refs）】\n\n"
        "以下能力已注册且工程实现完成。**若 task 与某能力的 triggers 契合（关键词命中）"
        "或语义匹配，请优先 invoke 而不是自建**——能力产出的 artifact 是稳定、可复用、可审计的"
        "工程实现，通常比你临时手写更快、更准、更可维护。\n\n"
        "**若你判断不 invoke**（如任务过小不值得起子进程 / 能力语义不完全覆盖 / 输入前置条件缺失），"
        "**请在你的输出里明确说明理由**（一句话即可），便于事后复盘。\n\n"
        "产出 artifact 直接落 vault，不回流 prompt。\n"
    )
    return header + "\n\n".join(summaries)


def invalidate_capability_summary_cache() -> None:
    """P10.5 A2：清 capability_summary lru_cache（测试 / manifest 修改后调）。"""
    _render_capability_summary_cached.cache_clear()


# B3（P10.5）：系统设计.md 的章节关键词共享词表。
#
# 抽自 technical_lead/main.py::_SYS_DESIGN_SECTIONS_FOR_TL（2026-06-09 §3.4 首发）。
# 让任何 skill 需要读 `10-项目/{project}/系统设计.md` 时都能用同一套 keyword
# 做 `read_input_files(dict entry with sections)` 裁剪，避免每个 skill 各写一份。
#
# **依据**：3 项目实测（pain-radar / mini-ledger / _quiz-game）章节命名差异大，
# 用关键词 any-match 兜底；全部未命中时 `_extract_sections` 回退全文 + warning。
SYS_DESIGN_SECTIONS_KEYWORDS = [
    "模块划分", "服务边界", "业务域", "业务领域",
    "前后端模块", "模块依赖", "目录布局",
    "数据流", "数据模型",
    "API 契约", "API契约", "接口", "外部依赖",
    "错误处理", "韧性", "失败模式", "降级",
    "任务粒度", "交付下游", "关键约束",
    "技术栈映射", "技术栈",
]


def sys_design_entry(sys_design_path):
    """B3（P10.5）：辅助构造 `read_input_files` 的 dict entry with sections。

    用法：
        read_input_files([sys_design_entry(sys_design_path), tech_stack])

    未来 dev_backend / dev_frontend 若开始读系统设计.md，直接调此 helper 避免
    每个 skill 各写一份 sections list。
    """
    return {"path": sys_design_path, "sections": SYS_DESIGN_SECTIONS_KEYWORDS}


def analyze_task_for_capability_hint(
    role, task_text: str, project: str = "{project}",
) -> str:
    """R3（M4 实战驱动 · 2026-07-08）：task 文本命中 role.capability_refs 里的 triggers
    → 返回强 hint 段供 user_prompt 尾部 append。

    - 无 capability_refs / 加载全失败 / 无 trigger 命中 → 返回 "" （不改 prompt）
    - 有命中 → 返回一段 "⚠️ 能力匹配提示" 强推 invoke 或 justify 不 invoke

    补 R1（capability_summary header 措辞加强）之外的**任务层信号**：R1 在 static
    prompt 里说"若匹配请优先 invoke"，R3 在 user_prompt 里明确"你**现在这个任务**
    确实命中了 X"。两层同修（数据/prompt/skill 主循环）参照 P8.7 教训。

    trigger 命中是**简单子串包含**（跟 role_loader.capability_refs 摘要注入的
    "LLM 自主判断"保持一致；不做 fuzzy / 词形归一化）。
    """
    refs = getattr(role, "capability_refs", ()) or ()
    if not refs or not task_text:
        return ""

    from engine.capability_executor.base import ManifestValidationError
    from engine.capability_executor.manifest_loader import (
        load_manifest as _load_manifest,
        validate_manifest as _validate_manifest,
    )

    hits: list[tuple[str, list[str]]] = []  # [(capability_id, matched_triggers)]
    for ref in refs:
        root = _capability_ref_root(ref)
        if not root:
            continue
        try:
            manifest = _load_manifest(f"{root}/manifest")
            _validate_manifest(manifest)
        except ManifestValidationError:
            continue

        triggers = manifest.get("triggers", []) or []
        matched = [t for t in triggers if isinstance(t, str) and t and t in task_text]
        if matched:
            hits.append((manifest["id"], matched))

    if not hits:
        return ""

    lines = [
        "",
        "---",
        "",
        "⚠️ **能力匹配提示**（R3 · task 文本命中已注册能力的 triggers）：",
        "",
        "本任务描述命中以下能力的触发词，请**优先评估 invoke 而非自建**——能力是稳定"
        "工程实现，artifact 直接落 vault，通常比临时手写更快、更准、更可维护：",
        "",
    ]
    for cap_id, matched in hits:
        preview = ", ".join(f"`{k}`" for k in matched[:5])
        lines.append(f"- **{cap_id}** — 命中触发词：{preview}")
        lines.append(
            f"  invoke: `python -m engine.capability_executor.invoke "
            f"--id {cap_id} --project {project} --input k=v`"
        )
    lines.extend([
        "",
        "**若你决定不 invoke**（如任务过小、能力语义不完全覆盖、输入前置条件缺失），"
        "请在你的输出里**一句话说明理由**；若 invoke 后 artifact 与本任务其他产出"
        "并存（如 capability 产 HTML 原型、你另写 React 组件），请明确各自的边界与职责。",
        "",
    ])
    return "\n".join(lines)


def _render_capability_summary(role, project: str | None = None) -> str:
    """P10：把 role.capability_refs 里每个 manifest 渲染成 ≤ 200 chars 摘要。

    - 规范 §5.2 关键不变量：能力实现**永远不进** prompt，只进 ≤ 200 chars 摘要 + 调用方式
    - 无 capability_refs 或加载全失败 → 返回空串
    - LLM 看到摘要 + triggers 后自主判断是否 invoke（不做 keyword 命中过滤，节省逻辑）
    - P10.5 A2：外层薄壳，转 tuple 给可缓存内层
    """
    refs = getattr(role, "capability_refs", ()) or ()
    if not refs:
        return ""
    return _render_capability_summary_cached(tuple(refs), project or "{project}")


def build_system_prompt(role_name_or_alias: str, project: str | None = None) -> tuple[str, str]:
    """从 vault 加载角色笔记，返回 (static, dynamic) 两段 system prompt。

    static：角色设定 + 全局约束 + 输出格式规范（几乎不变，适合 prompt cache）
    dynamic：DYNAMIC 补丁 + 上游补丁（每轮可能变化，不缓存）

    P8.2 起：若 env `AGENT_CONTRACT_OVERRIDES` 存在（workflow 层塞入），
    自动传给 load_role 走契约参数化路径（覆盖 role.outputs / role.inputs）。
    P8.7 起：契约展开结果通过 `_render_contract_summary` 暴露给 LLM，让 LLM
    明确本轮契约参数 + 产出清单，凌驾于 body 中的示例产出格式（补 P5b 半吊子）。
    B1（P10.5）：**dynamic 段现由 own_patch + upstream_patch 拼接**；如果调用方
    需要更细粒度的 cache 分层，用 `build_system_prompt_ex()` 拿 3-tuple。
    """
    static, dynamic_own, dynamic_upstream = build_system_prompt_ex(
        role_name_or_alias, project=project,
    )
    dynamic = "\n".join(filter(None, [dynamic_own, dynamic_upstream]))
    return static, dynamic


def build_system_prompt_ex(
    role_name_or_alias: str, project: str | None = None,
) -> tuple[str, str, str]:
    """B1（P10.5）：3-block 变体，供 engine.llm 打分层 cache breakpoint。

    返回 (static, dynamic_own, dynamic_upstream)：
    - **static**：角色 §1-§6 + contract_summary + capability_summary + OUTPUT_FORMAT_SPEC
      变化频率极低 → 打 `cache_control: ephemeral` breakpoint
    - **dynamic_own**：当前角色 DYNAMIC 补丁（B4 过滤后只含 [KEEP]/[GRADUATE?]）
      变化频率低（复盘者更新才变） → 也打 breakpoint，提升多 call 场景的 cache 命中
    - **dynamic_upstream**：上游角色 DYNAMIC 补丁
      变化频率高（上游 [NEW] 补丁频繁）→ **不打** breakpoint

    调用方（call_llm）根据 tuple 长度自动适配（2-tuple 走 static+flat_dynamic；
    3-tuple 走 static+dynamic_own+dynamic_upstream 三段 cache 布局）。
    """
    role = load_role(role_name_or_alias, contract_overrides=_read_env_contract_overrides())

    # ── static：核心层（T2.7 白名单契约）────────────────────
    # 业务角色严格 §1-§6 / 元角色全 body 减 DYNAMIC + 版本历史
    core_text, path_used = _extract_role_prompt_sections(role.body, role.domain)

    # 写 audit.jsonl：本次 system prompt 抽取路径
    try:
        from engine.llm import _append_token_audit
        _append_token_audit(
            "info", "role_prompt_extracted",
            {
                "role": role.name,
                "domain": role.domain,
                "path_used": path_used,
                "core_chars": len(core_text),
            },
        )
    except Exception:
        pass

    static_parts = [
        f"## 角色：{role.name}",
        _strip_evidence_lines(core_text.strip()),
    ]
    contract_summary = _render_contract_summary(role)
    if contract_summary:
        static_parts.append(contract_summary)
    capability_summary = _render_capability_summary(role, project=project)
    if capability_summary:
        static_parts.append(capability_summary)
    static_parts.append(OUTPUT_FORMAT_SPEC)
    static = "\n".join(static_parts)

    # ── dynamic_own：当前角色 DYNAMIC 补丁（B4 已 label 过滤）─────
    dynamic_own_parts: list[str] = []
    own_patch = _strip_evidence_lines(_extract_dynamic_patch(role.body))
    if own_patch.strip():
        dynamic_own_parts.append("## 当前动态约束")
        dynamic_own_parts.append(own_patch)
    dynamic_own = "\n".join(dynamic_own_parts)

    # ── dynamic_upstream：上游角色 DYNAMIC 补丁（不 cache）─────
    dynamic_upstream_parts: list[str] = []
    for upstream_name in role.upstream:
        try:
            up_role = load_role(upstream_name)
        except RoleNotFound:
            continue
        patch = _strip_evidence_lines(_extract_dynamic_patch(up_role.body))
        if patch.strip():
            dynamic_upstream_parts.append(f"## 上游角色 [{up_role.name}] 动态补丁指令")
            dynamic_upstream_parts.append(patch)
    dynamic_upstream = "\n".join(dynamic_upstream_parts)

    return static, dynamic_own, dynamic_upstream


# ── rule_refs 章节级展开（W3 P0c+ 音乐域 + SE 架构师共用） ────────────────────
# ── 能力加载器（2026-07-19 架构演进第 4 步上收 engine.ability_loader）────────
# 实现已迁 engine/ability_loader.py（统一装配入口 + 失败必告警 + 域 adapter）。
# 此处 re-export 保持全部 main.py 零改动（append_audit → engine.audit 同款手法）。
from engine.ability_loader import (  # noqa: E402,F401
    load_rule_block,
    load_genre_skill_block,
    load_skill_block,
)


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


# B2（P10.5）：单文件读取进程内缓存 —— 单 subprocess 内避免重复读同一文件。
# key = (path_str, mtime_ns)：mtime 变化即穿透缓存。
#
# **架构限制**：本 cache **仅覆盖单 subprocess 内**多次 read_input_files 调用同一
# 文件的场景（如 TL Plan+Detail×N 循环里的 sys_design.md / tech_stack.md）。
# **跨 skill 共享 cache**（review 报告 B2 原设想的"同轮 PRD 被 3 skill 读只喂 1 次"）
# 需磁盘缓存 or Redis —— 挂 98-待办 P11 独立立项。
_FILE_CACHE: dict[tuple[str, int], str] = {}


def _read_file_cached(path) -> str:
    """B2：按 (path, mtime_ns) 缓存单文件读取结果。"""
    from pathlib import Path as _Path
    p = _Path(path)
    if not p.exists() or not p.is_file():
        return "（文件不存在或为空）"
    try:
        mtime = p.stat().st_mtime_ns
    except OSError:
        # 无法 stat → 走非缓存路径
        try:
            return p.read_text(encoding="utf-8")
        except Exception as e:
            return f"（读取失败：{e}）"
    key = (str(p), mtime)
    if key in _FILE_CACHE:
        return _FILE_CACHE[key]
    try:
        content = p.read_text(encoding="utf-8")
    except Exception as e:
        return f"（读取失败：{e}）"
    _FILE_CACHE[key] = content
    # cache 保护上限：本 dict 只在单 subprocess 生命周期，最多几十条 entry，
    # 无 LRU 淘汰；如未来跨 workflow 长驻进程再加 maxsize/淘汰
    return content


def invalidate_file_cache() -> None:
    """B2：清进程内文件缓存（测试用；vault git pull 后由主入口调）。"""
    _FILE_CACHE.clear()


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

        # B2（P10.5）：走进程内缓存（跨 subprocess 不共享，仅覆盖同 subprocess 多次调用）
        content = _read_file_cached(fp)

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


# ── 时间与审计（P10.5 A1：抽 engine.audit 单点入口，此处仅 re-export 保向后兼容）
from engine.audit import append_audit, utc_now  # noqa: E402, F401


# ── LLM API 调用（按角色配置路由）──────────────────────────────────────────────
_DEFAULT_MAX_TOKENS = 4096
_DEFAULT_MODEL = "claude-sonnet-4-6"


def call_llm_for_role(system_prompt: tuple[str, str] | str, user_prompt: str, role_name_or_alias: str) -> str:
    """从角色 frontmatter 读取 model/max_tokens/budget_input_tokens 后调用 engine.llm.call_llm。

    单一职责：负责"角色配置读取 + 调用路由"，不重复实现 streaming 逻辑。
    底层路由由 engine.llm 处理（API key → SDK，否则 → CLI）。

    `budget_input_tokens`（可选，来自角色 frontmatter）会覆盖 engine.llm 入口护栏
    的默认 ratio 计算，按显式 token 数做 RAISE / WARN（适合"注定吃大上下文"的
    角色显式声明上限，如复盘者 / 角色审计器 / 讨论场参与者）。
    """
    input_budget: int | None = None
    try:
        role = load_role(role_name_or_alias)
        max_tokens = role.max_tokens
        model = role.model
        display_name = role.name
        input_budget = role.budget_input_tokens
    except RoleNotFound:
        max_tokens = _DEFAULT_MAX_TOKENS
        model = _DEFAULT_MODEL
        display_name = role_name_or_alias

    budget_note = f", input_budget={input_budget}" if input_budget else ""
    print(
        f"[{display_name}] 调用 LLM (model={model}, max_tokens={max_tokens}{budget_note})...",
        flush=True,
    )

    return _llm_call_llm(
        system_prompt, user_prompt,
        model=model, max_tokens=max_tokens,
        input_budget=input_budget,
        role_name=display_name,
    )


# 向后兼容：旧代码调用 call_claude(system, user, role) 继续有效
call_claude = call_llm_for_role


def warn_if_no_files(raw_output: str, role: str) -> None:
    """输出解析返回空 dict 时打印结构化告警，供各 main.py 降级路径调用。"""
    print(
        f"[{role}] ⚠️ FILE 块解析失败，已降级写入。"
        f" raw_output 长度={len(raw_output)}，前200字：{raw_output[:200]!r}",
        file=sys.stderr,
    )


# ── 层一强制执行：输出体积硬校验 ────────────────────────────────────────────────
_ENFORCE_SYSTEM = """你是文档精简专家。将输入文档重写为符合体积约束的版本。

规则：
- 目标：最终文档 ≤ {limit_chars} 字符
- 保留：所有接口定义、验收标准、路径约束、功能点编号
- 删除：背景说明、架构推理、设计原因、重复的上下文、示例代码（>10行的）
- 格式：保持原有 Markdown 结构，任务用编号列表
- 禁止新增任何原文没有的需求或接口
- 直接输出重写后的文档，不加任何解释前缀
"""


def check_size_limit(content: str, limit_chars: int) -> bool:
    """纯函数：判断 content 是否在 limit_chars 以内。

    单一职责：仅做尺寸检测，不产生任何副作用。
    """
    return len(content) <= limit_chars


def compress_to_limit(
    content: str,
    filename: str,
    limit_chars: int,
    *,
    max_retries: int = 2,
) -> str:
    """调用 haiku 将 content 重写至 ≤ limit_chars；重试耗尽则硬截断。

    单一职责：仅做压缩/截断，不做判断（判断由调用方或 check_size_limit 负责）。
    返回合规的内容字符串（硬截断时附加警告注释）。
    """
    from engine.llm import call_llm

    system = _ENFORCE_SYSTEM.format(limit_chars=limit_chars)
    current = content

    for attempt in range(1, max_retries + 1):
        try:
            rewritten = call_llm(
                system,
                f"请将以下文档重写为 ≤ {limit_chars} 字符的版本：\n\n{current}",
                model="claude-haiku-4-5",
                max_tokens=4096,
                print_stream=False,
                role_name="(enforce-limit)",
            )
        except Exception as e:
            print(
                f"[compress_to_limit] ❌ haiku 重写失败（尝试 {attempt}/{max_retries}）：{e}",
                file=sys.stderr,
            )
            break

        print(
            f"[compress_to_limit] 尝试 {attempt}/{max_retries}："
            f"{len(current)} → {len(rewritten)} chars",
            file=sys.stderr,
        )

        if check_size_limit(rewritten, limit_chars):
            print(
                f"[compress_to_limit] ✅ {filename} 重写合规 ({len(rewritten)} chars)",
                file=sys.stderr,
            )
            return rewritten

        current = rewritten  # 仍超限，用重写结果继续压缩

    # 重试耗尽：硬截断兜底
    print(
        f"[compress_to_limit] ⚠️ {filename} 重写 {max_retries} 次后仍超限，硬截断。",
        file=sys.stderr,
    )
    truncated = current[:limit_chars]
    truncated += f"\n\n<!-- ⚠️ 文档已被强制截断至 {limit_chars} 字符。原文 {len(content)} 字符。-->"
    return truncated


def enforce_output_limits(
    content: str,
    role: str,
    filename: str,
    limit_chars: int,
    *,
    max_retries: int = 2,
) -> str:
    """层一强制执行：若内容超出 limit_chars，调用 haiku 重写直到合规。
    § 15 层一增强：把"约束写在 prompt 里靠模型自觉"升级为"程序检测 + 强制压缩"。

    组合调用：check_size_limit（判断）+ compress_to_limit（重写/截断）。
    返回合规的内容字符串。
    """
    if check_size_limit(content, limit_chars):
        return content  # 已合规，直接返回

    print(
        f"[enforce_output_limits] ⚠️ {filename} 超限 "
        f"({len(content)} > {limit_chars} chars)，触发 haiku 强制重写...",
        file=sys.stderr,
    )
    return compress_to_limit(
        content, filename, limit_chars, max_retries=max_retries
    )
