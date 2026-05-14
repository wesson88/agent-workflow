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


def _strip_evidence_lines(text: str) -> str:
    """剥离 DYNAMIC 区的"闭环验证"证据行（系统认知图谱 §12 P0 token 控制）。"""
    return _EVIDENCE_LINE_RE.sub("", text)


def _extract_dynamic_patch(body: str) -> str:
    """从角色笔记正文里抽出 DYNAMIC 区域的有效补丁（过滤注释行）。"""
    matches = list(_DYNAMIC_RE.finditer(body))
    if not matches:
        return ""
    text = matches[-1].group(1).strip()
    keep = []
    for l in text.splitlines():
        s = l.strip()
        if not s:
            continue
        if s.startswith("#"):
            continue
        if s.startswith("<!--") and s.endswith("-->"):
            continue
        keep.append(l)
    return "\n".join(keep).strip()


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
