"""
prompt_builder.py — 系统提示词组装（单一职责：prompt 构造）

职责：
- build_system_prompt：从 vault 角色笔记组装 system prompt
- OUTPUT_FORMAT_SPEC：Claude 输出格式约束规范
- render_required_outputs：生成强约束 FILE 块输出清单
- _extract_dynamic_patch / _strip_evidence_lines：内部辅助

不依赖任何 I/O 或 API 调用（纯字符串构造），可独立测试。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from engine import load_role, RoleNotFound  # noqa: E402


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


# ── 内部辅助 ─────────────────────────────────────────────
_DYNAMIC_RE = re.compile(
    r"<!-- DYNAMIC_START -->(.*?)<!-- DYNAMIC_END -->",
    re.DOTALL,
)

_EVIDENCE_LINE_RE = re.compile(
    r"^[ \t]*-\s*闭环验证\s*\[[^\]]+\][:：].*\n?",
    re.MULTILINE,
)

# Patch 标题行：# Patch [时间戳] [标记] 标题
_PATCH_HEADER_RE = re.compile(
    r"^[ \t]*#\s*Patch\s*\[[^\]]+\]\s*\[(GRADUATE\??|DROP\??|NEW|KEEP)\]",
    re.MULTILINE,
)

# 注入上下文时跳过的标记：待执行 / 待删除 / 用户已确认但 graduator 未跑的
# - [GRADUATE]  用户已确认合入，graduator 执行后会进 body/skill，现在注入是重复且临时态
# - [DROP]      用户已确认删除，注入是反向操作
# - [DROP?]     系统推荐删除，说明此约束可能是噪音或已失效
_SKIP_LABELS_FOR_CONTEXT = frozenset({"GRADUATE", "DROP", "DROP?"})


def _strip_evidence_lines(text: str) -> str:
    """剥离 DYNAMIC 区的"闭环验证"证据行（系统认知图谱 §12 P0 token 控制）。"""
    return _EVIDENCE_LINE_RE.sub("", text)


def _extract_dynamic_patch(body: str) -> str:
    """从角色笔记正文里抽出 DYNAMIC 区域的有效补丁（过滤注释行 + 无效标记状态）。

    注入上下文的只保留：
      [NEW]        — reflector 新发现的候选约束，下游角色应感知
      [KEEP]       — 已验证稳定保留的约束，最核心的注入内容
      [GRADUATE?]  — 系统推荐合入，置信度较高，注入无害

    过滤掉：
      [GRADUATE]   — 用户已确认合入等待 graduator 执行，临时态，注入会与 body 重复
      [DROP]       — 用户已确认删除，注入是反向操作
      [DROP?]      — 系统推荐删除，说明此约束可能是噪音或已失效
    """
    matches = list(_DYNAMIC_RE.finditer(body))
    if not matches:
        return ""
    text = matches[-1].group(1).strip()

    # 按 Patch 块切分，逐块判断标记是否应注入
    # 块边界：遇到 "# Patch" 行时开启新块
    blocks: list[list[str]] = []
    current: list[str] = []
    current_label: str | None = None

    for line in text.splitlines():
        s = line.strip()
        m = _PATCH_HEADER_RE.match(line)
        if m:
            # 上一块收尾
            if current:
                blocks.append((current_label, current))
            current_label = m.group(1).rstrip("?") if m.group(1).endswith("?") else m.group(1)
            # 还原带问号的标记（GRADUATE? 保留问号用于过滤判断）
            raw_label = m.group(1)
            current_label = raw_label
            current = [line]
        else:
            # DYNAMIC 区内非 Patch 块的行（注释/空行等）归到当前块
            if not s:
                if current:
                    current.append(line)
            elif s.startswith("#") or (s.startswith("<!--") and s.endswith("-->")):
                # 注释行跳过
                pass
            else:
                current.append(line)

    if current:
        blocks.append((current_label, current))

    # 过滤：只保留应注入的标记块
    keep_lines: list[str] = []
    for label, lines in blocks:
        # label 是原始标记字符串（可能含 ?）
        base = (label or "").replace("?", "").strip() + ("?" if (label or "").endswith("?") else "")
        if base in _SKIP_LABELS_FOR_CONTEXT:
            continue
        # 去除证据行后加入
        block_text = "\n".join(lines)
        block_text = _strip_evidence_lines(block_text)
        keep_lines.append(block_text.rstrip())

    return "\n\n".join(keep_lines).strip()


# ── 内部：skill_refs 剥离辅助 ────────────────────────────
_SKILL_BLOCK_RE = re.compile(
    r"\n\n## 引用技能（来自 skill_refs）\n\n.*",
    re.DOTALL,
)


def _strip_skill_refs_block(body: str) -> str:
    """从已内联 skill_refs 的 body 中剥除 skill 块，只保留角色笔记主体。

    role_loader._resolve_skill_refs 拼接的标记固定为
    `## 引用技能（来自 skill_refs）` ，匹配到该标题后截断即可。
    """
    return _SKILL_BLOCK_RE.sub("", body)


# ── 公开 API ──────────────────────────────────────────────
def build_system_prompt(role_name_or_alias: str, project: str | None = None) -> tuple[str, str]:
    """从 vault 加载角色笔记，返回 (static, dynamic) 两段 system prompt。

    static：角色设定 + 全局约束 + 输出格式规范（几乎不变，适合 prompt cache）
    dynamic：DYNAMIC 补丁 + 上游补丁（每轮可能变化，不缓存）
    """
    role = load_role(role_name_or_alias)

    summary = [
        f"角色：{role.name}",
        f"领域：{role.domain}",
        f"风格：{role.style}",
    ]
    if role.skills:
        summary.append(f"技能:{', '.join(role.skills)}")

    static_parts = [
        "## 角色摘要",
        "\n".join(summary),
        "",
        _strip_evidence_lines(role.body.strip()),
        OUTPUT_FORMAT_SPEC,
    ]
    static = "\n".join(static_parts)

    dynamic_parts: list[str] = []
    for upstream_name in role.upstream:
        try:
            up_role = load_role(upstream_name)
        except RoleNotFound:
            continue
        patch = _strip_evidence_lines(_extract_dynamic_patch(up_role.body))
        if patch.strip():
            dynamic_parts.append(f"## 上游角色 [{up_role.name}] 动态补丁指令")
            dynamic_parts.append(patch)

    dynamic = "\n".join(dynamic_parts)
    return static, dynamic


def build_system_prompt_no_skills(
    role_name_or_alias: str,
    project: str | None = None,
) -> tuple[str, str]:
    """与 build_system_prompt 相同，但 static 中不内联 skill_refs 文件内容。

    返回 (static_no_skills, dynamic)。
    供需要按任务动态裁剪 skill 的调用方使用（如 technical_lead Detail call）：
    调用方拿到 static_no_skills 后，自行调用 build_task_skill_block 拼接
    只与当前任务相关的 skill 文本，替代全量注入。

    token 收益：每个 Detail call 少注入 ~(N-k) × avg_skill_size tokens，
    其中 N = 角色全量 skill 数，k = 当前任务实际命中数（通常 1-2）。
    """
    role = load_role(role_name_or_alias)

    summary = [
        f"角色：{role.name}",
        f"领域：{role.domain}",
        f"风格：{role.style}",
    ]
    if role.skills:
        summary.append(f"技能:{', '.join(role.skills)}")

    # 从 body 剥除已内联的 skill_refs 块
    body_no_skills = _strip_skill_refs_block(role.body)

    static_parts = [
        "## 角色摘要",
        "\n".join(summary),
        "",
        _strip_evidence_lines(body_no_skills.strip()),
        OUTPUT_FORMAT_SPEC,
    ]
    static = "\n".join(static_parts)

    dynamic_parts: list[str] = []
    for upstream_name in role.upstream:
        try:
            up_role = load_role(upstream_name)
        except RoleNotFound:
            continue
        patch = _strip_evidence_lines(_extract_dynamic_patch(up_role.body))
        if patch.strip():
            dynamic_parts.append(f"## 上游角色 [{up_role.name}] 动态补丁指令")
            dynamic_parts.append(patch)

    dynamic = "\n".join(dynamic_parts)
    return static, dynamic


def build_task_skill_block(
    skill_stems_or_paths: list[str],
    side: str,
    vault_root: "Path | None" = None,
    max_chars_per_skill: int = 3000,
    total_budget: int = 12_000,
) -> str:
    """按任务命中的 wikilink 列表，读取对应 skill 文件并组装注入块。

    side ∈ {"后端", "前端"}；只处理与 side 匹配的前缀（B* / F*），
    其他前缀（A*/TL*）直接跳过，防止跨角色 skill 误注入。

    参数：
      skill_stems_or_paths  — wikilink target 列表（stem 或 vault 相对路径）
      side                  — 当前任务归属侧
      vault_root            — 可选，测试时传 tmp_path；默认用 engine.config.VAULT_ROOT
      max_chars_per_skill   — 单个 skill 文件的字符截断上限
      total_budget          — 所有 skill 合计字符上限

    返回可直接拼入 system prompt 的字符串（无命中时返回空串）。
    """
    from pathlib import Path as _Path
    from engine.config import VAULT_ROOT as _VAULT_ROOT
    from engine.wikilink import resolve_target, _stem_index
    from engine.obsidian_io import split_frontmatter

    root = vault_root if vault_root is not None else _VAULT_ROOT

    # side → 允许的 skill 文件名前缀
    allowed_prefixes: tuple[str, ...] = {
        "后端": ("B",),
        "前端": ("F",),
    }.get(side, ())

    parts: list[str] = []
    total = 0

    for target in skill_stems_or_paths:
        if total >= total_budget:
            break

        # 提取 stem（完整路径取最后一段）
        stem = target.split("/")[-1]

        # 前缀过滤
        if allowed_prefixes and not any(stem.startswith(p) for p in allowed_prefixes):
            continue

        # 解析路径（使用 engine.wikilink resolve）
        path = resolve_target(target, vault_root=root)
        if path is None or not path.is_file():
            print(f"[skill_refs] ⚠️ 未找到 skill 文件：{target}", file=sys.stderr)
            continue

        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as e:
            print(f"[skill_refs] ⚠️ 读取 skill 失败 {target}：{e}", file=sys.stderr)
            continue

        _, body = split_frontmatter(raw)
        body = body.strip()

        # 单文件截断
        if len(body) > max_chars_per_skill:
            body = body[:max_chars_per_skill] + f"\n…（已截断，原文 {len(body)} chars）"

        # 总预算检查
        remaining = total_budget - total
        if len(body) > remaining:
            body = body[:remaining] + f"\n…（总预算已满，截断）"
            parts.append(f"=== Skill: [[{target}]] ===\n{body}")
            total += len(body)
            break

        parts.append(f"=== Skill: [[{target}]] ===\n{body}")
        total += len(body)

    if not parts:
        return ""

    return (
        "\n\n## 本任务相关技能（按 summary wikilink 动态加载）\n\n"
        + "\n\n".join(parts)
        + "\n"
    )
