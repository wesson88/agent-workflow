"""
technical_lead/main.py — 技术主管执行入口（Phase 2b vault-based）

输入（vault）：
  - 10-项目/{project}/指令/给技术主管.md   架构师下发的任务
  - 10-项目/{project}/系统设计.md          系统设计
  - 00-系统/规则/技术栈.md                  技术栈

输出（vault）：
  - 10-项目/{project}/指令/给后端.md
  - 10-项目/{project}/指令/给前端.md

CLI：
  python .claude/skills/technical_lead/main.py --task "..." --project myproj
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import (
    parse_args, resolve_project, build_system_prompt, read_input_files,
    write_output_atomic, parse_claude_output_to_files,
    call_claude, append_audit, utc_now, render_required_outputs,
    enforce_output_limits,
)
from engine import (
    set_role_status, role_is_blocked,
    project_dir, rules_dir, resolve_path,
    VAULT_ROOT,
)

ROLE = "技术主管"

_VALID_PROJECT_TYPES = ("backend-only", "frontend-only", "full-stack")


def _backend_done_marker() -> Path:
    """后端轮 done marker：子进程超时 retry 时用来跳过已成功的后端轮。"""
    return VAULT_ROOT / "00-系统" / ".runtime-state" / "技术主管.backend_done"


def _read_project_type(to_lead_path: Path) -> tuple[str, str]:
    """读取「给技术主管.md」frontmatter 中的 project_type 字段。

    返回 (project_type, source)；source ∈ {"frontmatter", "default_full_stack"}。
    架构师未声明时默认 full-stack（向后兼容，旧项目行为不变）。
    """
    try:
        text = to_lead_path.read_text(encoding="utf-8")
    except OSError:
        return "full-stack", "default_full_stack"

    # 匹配 frontmatter 块：开头 --- 到下一个 ---
    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not fm_match:
        return "full-stack", "default_full_stack"

    fm_body = fm_match.group(1)
    type_match = re.search(r"^project_type:\s*([a-z-]+)", fm_body, re.MULTILINE)
    if not type_match:
        return "full-stack", "default_full_stack"

    declared = type_match.group(1).strip()
    if declared not in _VALID_PROJECT_TYPES:
        print(f"[{ROLE}] ⚠️ project_type='{declared}' 不在合法集 {_VALID_PROJECT_TYPES}，"
              f"按 full-stack 处理", file=sys.stderr)
        return "full-stack", "default_full_stack"
    return declared, "frontmatter"


def _write_skip_stub(proj_dir: Path, side: str, project_type: str) -> Path:
    """写「给{side}-索引.md」stub，触发 dev_{side} 的 idle 跳过。

    side ∈ {"后端", "前端"}；project_type 用于落档可观测性。
    """
    dest = proj_dir / "指令" / f"给{side}-索引.md"
    other = "前端" if side == "后端" else "后端"
    content = (
        f"---\n"
        f"type: task-index\n"
        f"role: {side}工程师\n"
        f"decided_by: project_type-frontmatter\n"
        f"project_type: {project_type}\n"
        f"decided_at: {utc_now()}\n"
        f"---\n\n"
        f"# 无{side}任务\n\n"
        f"本项目 `project_type={project_type}`，仅含{other}业务，无{side}实现。\n\n"
        "若架构师判定有误，请改「给技术主管.md」frontmatter 中的 `project_type` 字段"
        f"后重跑 `--start-from 技术主管`。\n"
    )
    write_output_atomic(dest, content)
    return dest


def main() -> int:
    args = parse_args()
    task = (args.task or "").strip()
    project = resolve_project(args)

    if role_is_blocked(ROLE):
        print(f"[{ROLE}] status=blocked，跳过。", file=sys.stderr)
        return 1

    set_role_status(ROLE, status="busy", enforce_transition=False)

    proj_dir = project_dir(project)
    to_lead = proj_dir / "指令" / "给技术主管.md"
    sys_design = proj_dir / "系统设计.md"
    tech_stack = rules_dir() / "技术栈.md"

    missing = [p for p in (to_lead, sys_design) if not p.exists()]
    if missing:
        print(
            f"[{ROLE}] 必需输入缺失：{[str(p) for p in missing]}。请先跑架构师。",
            file=sys.stderr,
        )
        set_role_status(
            ROLE, status="failed",
            increment_consecutive_failures=True, increment_error=True,
            enforce_transition=False,
        )
        append_audit({
            "timestamp": utc_now(), "role": ROLE, "project": project,
            "task": task, "result": "failed", "error": "missing_inputs",
            "missing": [str(p) for p in missing],
        })
        return 2

    # 上游补丁：架构师的 DYNAMIC 区域已在 build_system_prompt 内自动注入
    system_prompt = build_system_prompt(ROLE, project=project)

    # Phase 4c-3：注入项目目录下所有 脑暴-*.md 作为讨论决策来源
    discussion_logs = sorted((proj_dir).glob("脑暴-*.md")) if proj_dir.is_dir() else []
    inputs = [to_lead, sys_design, tech_stack, *discussion_logs]
    context = read_input_files(inputs)

    discussion_hint = ""
    if discussion_logs:
        names = "、".join(f"脑暴-{p.stem.removeprefix('脑暴-')}" for p in discussion_logs)
        discussion_hint = (
            f"\n**注意**：项目内已有讨论笔记（{names}），上面已包含。"
            f"请把讨论中**已被确认采纳**的决策（尤其是架构师在末轮给出的"
            f"裁决/决策清单）直接落到给后端/给前端的实施约束中——"
            f"不要重新发起讨论里已经收敛过的争论。\n"
        )

    base_prompt = (
        f"项目名：`{project}`\n\n"
        f"{context}\n\n---\n"
        f"本轮任务：{task or '按架构设计拆分开发任务'}\n"
        f"{discussion_hint}\n"
        "每个任务必须有：功能描述、对应模块/接口、输入输出、验收标准\n"
        "单任务工作量 ≤ 4 小时，超过须再拆分\n"
        "每个文件只包含该任务自身的描述 + 验收标准 + 必要的接口约束\n"
        "禁止在单个文件中汇总所有任务\n\n"
    )

    backend_prompt = base_prompt + (
        "**本次只输出后端任务文件**，不要输出任何前端内容。\n"
        "每个任务单独一个 FILE 块，按编号命名：\n"
        f"  `10-项目/{project}/指令/给后端-T01.md`、`给后端-T02.md` ...\n\n"
        "**无后端业务时的合法出口**：如果项目本身无服务端业务"
        "（静态站 / 纯前端 SPA / 浏览器扩展），\n"
        f"允许只输出一份 `10-项目/{project}/指令/给后端-索引.md`，内容首行写 `# 无后端任务`，\n"
        "并简述判定理由（≤ 100 字）。**不要凑后端任务**。\n"
        + render_required_outputs([f"10-项目/{project}/指令/给后端-索引.md"])
    )

    frontend_prompt = base_prompt + (
        "**本次只输出前端任务文件**，不要输出任何后端内容。\n"
        "标注与后端的协作关系（API 契约、数据流）。\n"
        "每个任务单独一个 FILE 块，按编号命名：\n"
        f"  `10-项目/{project}/指令/给前端-T01.md`、`给前端-T02.md` ...\n\n"
        "**无前端业务时的合法出口**：如果项目本身无浏览器/移动端 UI"
        "（CLI / 库 / 工具 / 数据管线 / 后台 job / API-only 服务），\n"
        f"允许只输出一份 `10-项目/{project}/指令/给前端-索引.md`，内容首行写 `# 无前端任务`，\n"
        "并简述判定理由（≤ 100 字）。**不要凑前端任务**——拼凑出来的前端任务会让\n"
        "下游 dev_frontend 浪费 token 并误导用户。\n"
        + render_required_outputs([f"10-项目/{project}/指令/给前端-索引.md"])
    )

    LIMIT_CHARS = 30 * 1024
    written = []
    marker = _backend_done_marker()

    # 读取「给技术主管.md」frontmatter 中的 project_type，驱动对称跳过
    project_type, type_source = _read_project_type(to_lead)
    print(f"[{ROLE}] 🏷️ project_type={project_type}（来源：{type_source}）")
    if type_source == "default_full_stack":
        print(f"[{ROLE}] ℹ️ 「给技术主管.md」frontmatter 未声明 project_type，"
              f"默认按 full-stack 跑两轮。若实际为单端项目，请改 frontmatter "
              f"加 `project_type: backend-only` 或 `frontend-only` 后重跑。")

    for pass_name, user_prompt in [("后端任务", backend_prompt), ("前端任务", frontend_prompt)]:
        side = "后端" if pass_name == "后端任务" else "前端"

        # ── project_type 驱动的对称跳过 ────────────────────────────────
        should_skip = (
            (side == "前端" and project_type == "backend-only")
            or (side == "后端" and project_type == "frontend-only")
        )
        if should_skip:
            dest = _write_skip_stub(proj_dir, side, project_type)
            print(f"[{ROLE}] ⏭️ 跳过{side}轮（project_type={project_type}），写 stub: {dest.name}")
            written.append(f"10-项目/{project}/指令/给{side}-索引.md")
            continue

        # ── 后端轮：检测 done marker，子进程超时 retry 时跳过重跑 ────────
        if pass_name == "后端任务":
            existing = sorted((proj_dir / "指令").glob("给后端-T*.md"))
            backend_index = proj_dir / "指令" / "给后端-索引.md"
            if marker.exists() and backend_index.exists() and existing:
                print(f"[{ROLE}] ⏩ 后端轮已完成（marker 存在 + {len(existing)} 个任务卡），跳过重跑")
                for p in [backend_index, *existing]:
                    rel = f"10-项目/{project}/指令/{p.name}"
                    written.append(rel)
                continue

        print(f"[{ROLE}] 📝 生成{pass_name}...")
        try:
            raw_output = call_claude(system_prompt, user_prompt, ROLE)
        except Exception as e:
            print(f"[{ROLE}] Claude API 调用失败（{pass_name}）：{e}", file=sys.stderr)
            set_role_status(
                ROLE, status="failed",
                increment_consecutive_failures=True, increment_error=True,
                enforce_transition=False,
            )
            append_audit({
                "timestamp": utc_now(), "role": ROLE, "project": project,
                "task": task, "result": "failed", "error": str(e),
            })
            return 1

        output_files = parse_claude_output_to_files(raw_output)
        if not output_files:
            # 降级：整体写入对应文件
            fallback_name = "给后端.md" if "后端" in pass_name else "给前端.md"
            dest = proj_dir / "指令" / fallback_name
            enforced = enforce_output_limits(raw_output, ROLE, dest.name, LIMIT_CHARS)
            write_output_atomic(dest, enforced)
            written.append(f"10-项目/{project}/指令/{fallback_name}")
            print(f"[{ROLE}] 未检测到 FILE 标签，降级写入 {dest}")
        else:
            for rel_path, content in output_files.items():
                rel_resolved = rel_path.replace("{project}", project)
                dest = resolve_path(rel_resolved, project)
                is_instruction = (
                    ("给后端" in dest.name or "给前端" in dest.name)
                    and dest.suffix == ".md"
                    and "索引" not in dest.name
                )
                if is_instruction:
                    content = enforce_output_limits(content, ROLE, dest.name, LIMIT_CHARS)
                write_output_atomic(dest, content)
                print(f"[{ROLE}] 写入: {dest}")
                written.append(rel_resolved)

        # 后端轮成功后写 marker（前端轮超时 retry 时复用已有产出）
        if pass_name == "后端任务":
            try:
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text(f"done at {utc_now()}\nfiles: {len(written)}\n",
                                  encoding="utf-8")
            except OSError as e:
                print(f"[{ROLE}] ⚠️ 写 backend_done marker 失败（{e}），retry 时会重跑后端轮",
                      file=sys.stderr)

    # 整轮成功，清理 marker（下次重跑工作流自然失效）
    if marker.exists():
        try:
            marker.unlink()
        except OSError:
            pass

    set_role_status(ROLE, status="success", reset_counters=True)
    set_role_status(ROLE, status="idle")
    append_audit({
        "timestamp": utc_now(), "role": ROLE, "project": project,
        "task": task, "result": "success", "outputs": written,
    })
    print(f"[{ROLE}] 完成，输出：{written}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
