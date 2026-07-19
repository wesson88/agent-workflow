"""
engine/role_runner.py — 声明驱动通用角色执行器（架构演进第 3 步 PoC · 2026-07-19）

设计：[[架构演进方向-角色接口化与跨域组合-2026-07-18]] 缺口 1。
25 份 skill main.py 绝大多数同构（八步流水）；本模块把流水收编为引擎实现，
角色差异全部来自声明（vault 角色基因 frontmatter + 产物注册表条目正文）：

  1. 角色态检查（blocked）+ status busy
  2. load_role → inputs/outputs 声明（{role} 消费端绑定自身角色名——与
     artifact_check 同一封闭规则；{project} 由 resolve_path 绑定）
  3. build_system_prompt（基因 body + 契约/能力摘要 + OUTPUT_FORMAT_SPEC）
  4. context = read_input_files + rule_refs 章节注入 + genre/keyword skill 双路径
  5. dormant 识别（music 扇出约定：`指令/给<自身>.md` 开头 dormant 关键词——
     域约定收编为一条规则，非角色特例）
  6. user_prompt 通用组装：产出指引来自**产物注册表条目正文**（D2 决策红利：
     注册表正文本就是"给 LLM 的产物说明"）；未注册产物回退基因职责句
  7. call_claude → FILE 块解析 → resolve_path 写盘
  8. status success/idle + audit（与 main.py 同 schema，附 runner=in_process 标记）

消费端上游检查**不**在本模块重复实现——invoke_role 的 artifact_check
（v0.4 fail 默认）已是单点；直接调用 run_role 者自担前置。

层级债（架构演进第 4 步一并偿还）：prompt 构建/加载器仍在 skills/common.py，
本模块以 sys.path 方式反向引用——第 4 步"能力 loader 统一"会把这批公共件
上收 engine，届时移除此反向依赖。

PoC 范围：music 域同构角色（首个收编：母带工程师）。SE 工程师（任务拆分
循环）、技术主管（1073 行特例）等继续走各自 main.py。
"""

from __future__ import annotations

import sys
from pathlib import Path

from .config import PROJECT_ROOT, VAULT_ROOT, resolve_path
from .role_invoke import RoleResult
from .state import role_is_blocked, set_role_status

# 层级债：skills/common 公共件反向引用（见模块 docstring）
_SKILLS_DIR = PROJECT_ROOT / ".claude" / "skills"
if str(_SKILLS_DIR) not in sys.path:  # pragma: no cover
    sys.path.insert(0, str(_SKILLS_DIR))

from common import (  # noqa: E402
    append_audit,
    build_system_prompt,
    call_claude,
    parse_claude_output_to_files,
    read_input_files,
    render_required_outputs,
    utc_now,
)

from .ability_loader import assemble_user_context  # noqa: E402

# music 扇出 dormant 约定（原 music_mastering_engineer/main.py，收编为通用规则）
_DORMANT_KEYWORDS = ("dormant", "本项目状态：dormant", "不启动")


def _detect_dormant(instruction_path: Path | None) -> bool:
    """读 `指令/给<自身>.md` 开头判断制作人是否明示 dormant。"""
    if instruction_path is None or not instruction_path.exists():
        return False
    try:
        head = instruction_path.read_text(encoding="utf-8")[:2000]
    except Exception:
        return False
    return any(kw in head for kw in _DORMANT_KEYWORDS)


def _artifact_guidance(output_rels: list[str]) -> dict[str, str]:
    """产出路径 → 注册表条目正文摘要（产出指引）。

    匹配 = 声明路径（保留 {project}）== 条目模板渲染（与 coverage_report 同法）。
    未注册产物（如补丁类）→ 不入 dict，调用方回退通用句。
    注册表不可用 → 空 dict（指引降级，不拦执行）。
    """
    try:
        from .artifact_registry import _load_registry_cached, _registry_dir
        from .obsidian_io import read_note, split_frontmatter

        if not _registry_dir().is_dir():
            return {}
        proj_roots, registry = _load_registry_cached()
    except Exception:
        return {}

    rendered_map = {
        spec.resolve(proj_roots).replace("\\", "/"): spec
        for spec in registry.values()
    }
    guidance: dict[str, str] = {}
    for rel in output_rels:
        spec = rendered_map.get(rel.replace("\\", "/"))
        if spec is None or spec.note_path is None:
            continue
        try:
            _, body = split_frontmatter(read_note(spec.note_path))
        except Exception:
            continue
        # 条目正文首段（去标题行），≤400 chars——正文即"给 LLM 的产物说明"
        lines = [ln for ln in body.strip().splitlines()
                 if ln.strip() and not ln.lstrip().startswith("#")]
        if lines:
            guidance[rel] = " ".join(ln.strip() for ln in lines)[:400]
    return guidance


def _build_user_prompt(
    role, project: str, task: str, context: str,
    output_rels: list[str], is_dormant: bool,
) -> str:
    """通用 user_prompt 组装（原各 main.py 手写段落的声明驱动形态）。"""
    n = len(output_rels)
    if is_dormant:
        scenario = (
            f"**重要：上游指令明示本项目 dormant。**\n"
            f"请按 dormant 状态降级输出全部 {n} 份文件：每份仅写 dormant 状态说明"
            f"（确认 dormant 决策 + 未来 promote 触发条件），"
            f"**严禁伪造完整业务内容**。dormant 不等于 dropped，文档保留以便未来 promote。"
        )
    else:
        guidance = _artifact_guidance(output_rels)
        items = []
        for i, rel in enumerate(output_rels, 1):
            hint = guidance.get(rel, "按角色职责与 style 约束产出")
            items.append(f"{i}. `{rel}` — {hint}")
        style_line = (
            f"\n执行风格硬约束（角色基因 style）：{role.style}\n" if role.style else ""
        )
        scenario = (
            f"请产出以下 {n} 份 FILE 块（每份独立，缺一不可）：\n"
            + "\n".join(items)
            + style_line
        )
    return (
        f"项目名：`{project}`（写文件时把路径里的 `{{project}}` 占位符替换为本值）\n\n"
        f"{context}\n\n---\n"
        f"本轮任务：{task or '（未提供，请基于上游输入综合推导）'}\n\n"
        f"{scenario}\n"
        f"{render_required_outputs(output_rels)}"
    )


def run_role(
    role_name: str,
    task: str,
    project: str,
    *,
    domain: str | None = None,
) -> RoleResult:
    """进程内执行一个声明驱动角色。invoke_role(mode="in_process") 的实现体。

    domain：workflow 声明的域（如 "music"）——非空时 ability_loader 自动注入
    `00-系统/规则/{domain}/{角色}-视角.md`（存在才注入，缺口 5 域 adapter）。
    返回 RoleResult：outputs 直接携带（写盘的 vault 相对路径），
    returncode 沿用 main.py 约定（0 成功 / 1 可重试 / 2 永久）。
    """
    import time
    t0 = time.monotonic()

    def _fail(rc: int, error: str, *, status_error: bool = True) -> RoleResult:
        if status_error:
            set_role_status(
                role_name, status="failed",
                increment_consecutive_failures=True, increment_error=True,
                enforce_transition=False,
            )
        append_audit({
            "timestamp": utc_now(), "role": role_name, "project": project,
            "task": task, "result": "failed", "error": error,
            "runner": "in_process",
        })
        return RoleResult(
            status="permanent_failed" if rc == 2 else "failed",
            returncode=rc, role=role_name,
            elapsed_s=time.monotonic() - t0, error=error,
        )

    if role_is_blocked(role_name):
        return RoleResult(
            status="failed", returncode=1, role=role_name,
            elapsed_s=time.monotonic() - t0, error="role status=blocked",
        )

    set_role_status(role_name, status="busy", enforce_transition=False)

    from .role_loader import load_role
    role = load_role(role_name)

    # {role} 消费端绑定自身角色名（与 artifact_check / music main.py 同一规则）
    input_paths = [
        resolve_path(p.replace("{role}", role.name), project)
        for p in role.inputs
    ]
    output_rels = [p.replace("{project}", project) for p in role.outputs]

    instruction_path = next(
        (p for p in input_paths if p.name == f"给{role.name}.md"), None,
    )
    is_dormant = _detect_dormant(instruction_path)
    if is_dormant:
        print(f"[{role.name}] 上游指令明示 dormant，走降级输出路径。", flush=True)

    existing = [p for p in input_paths if p.exists()]
    print(
        f"[{role.name}] (runner) 上游 {len(existing)}/{len(input_paths)} 就位："
        f"{[p.name for p in existing]}",
        flush=True,
    )

    system_prompt = build_system_prompt(role.name, project=project)
    base_context = read_input_files(input_paths)

    context, ability_hints = assemble_user_context(
        role, task, base_context, domain=domain,
    )
    print(f"[{role.name}] rule_refs 注入：{ability_hints['rule_refs']}")
    print(f"[{role.name}] skill_trigger：{ability_hints['skill']}")
    if domain:
        print(f"[{role.name}] 域 adapter：{ability_hints['domain_adapter']}")

    user_prompt = _build_user_prompt(
        role, project, task, context, output_rels, is_dormant,
    )

    try:
        raw_output = call_claude(system_prompt, user_prompt, role.name)
    except Exception as e:
        print(f"[{role.name}] LLM 调用失败：{e}", file=sys.stderr)
        return _fail(1, str(e))

    output_files = parse_claude_output_to_files(raw_output)
    if not output_files:
        print(
            f"[{role.name}] 未检测到 FILE 块。原始输出长度 {len(raw_output)}。",
            file=sys.stderr,
        )
        return _fail(1, "no_file_blocks")

    from .obsidian_io import atomic_write_text
    written: list[str] = []
    for rel_path, content in output_files.items():
        rel_resolved = rel_path.replace("{project}", project)
        dest = resolve_path(rel_resolved, project)
        atomic_write_text(dest, content)
        print(f"[{role.name}] 写入: {dest}")
        written.append(rel_resolved)

    set_role_status(role.name, status="success", reset_counters=True)
    set_role_status(role.name, status="idle")
    append_audit({
        "timestamp": utc_now(), "role": role.name, "project": project,
        "task": task, "result": "success", "outputs": written,
        "is_dormant": is_dormant, "runner": "in_process",
    })
    print(
        f"[{role.name}] 完成（{'dormant 降级' if is_dormant else '正常'}，"
        f"runner），输出：{written}"
    )
    return RoleResult(
        status="success", returncode=0, role=role.name,
        elapsed_s=time.monotonic() - t0, outputs=tuple(written),
    )
