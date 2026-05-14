"""
chief_architect/main.py — 首席架构师执行入口（Phase 2b vault-based）

输入（vault）：
  - 10-项目/{project}/PRD.md          产品需求文档
  - 00-系统/规则/技术栈.md             技术栈约束
  - 00-系统/规则/架构分解规则.md       分解方法论

输出（vault）：
  - 10-项目/{project}/系统设计.md
  - 10-项目/{project}/指令/给技术主管.md

CLI：
  python .claude/skills/chief_architect/main.py --task "..." --project myproj
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import (
    parse_args, resolve_project, build_system_prompt, read_input_files,
    write_output_atomic, parse_claude_output_to_files,
    call_claude, append_audit, utc_now, render_required_outputs,
)
from engine import (
    set_role_status, role_is_blocked,
    project_dir, rules_dir, resolve_path,
)

ROLE = "架构师"


def main() -> int:
    args = parse_args()
    task = (args.task or "").strip()
    project = resolve_project(args)

    if role_is_blocked(ROLE):
        print(f"[{ROLE}] status=blocked，跳过。", file=sys.stderr)
        return 1

    set_role_status(ROLE, status="busy", enforce_transition=False)

    proj_dir = project_dir(project)
    prd = proj_dir / "PRD.md"
    tech_stack = rules_dir() / "技术栈.md"
    decomp_rules = rules_dir() / "架构分解规则.md"

    if not prd.exists():
        print(
            f"[{ROLE}] PRD 缺失：{prd}。请先跑产品经理生成 PRD。",
            file=sys.stderr,
        )
        set_role_status(
            ROLE, status="failed",
            increment_consecutive_failures=True, increment_error=True,
            enforce_transition=False,
        )
        append_audit({
            "timestamp": utc_now(), "role": ROLE, "project": project,
            "task": task, "result": "failed", "error": "missing_prd",
        })
        return 2

    system_prompt = build_system_prompt(ROLE, project=project)
    context = read_input_files([prd, tech_stack, decomp_rules])

    base_prompt = (
        f"项目名：`{project}`\n\n"
        f"{context}\n\n---\n"
        f"本轮任务：{task or '按 PRD 完成架构分解'}\n\n"
        "技术栈约束严格按 `00-系统/规则/技术栈.md`，禁止引入未授权的库。\n"
    )

    design_prompt = base_prompt + (
        "**本次只输出系统设计文档**，不要输出任务清单。\n"
        "严格遵循『架构分解规则.md』，产出系统设计：含架构图、模块划分、数据流、API 契约；模块按业务域划分；技术选型严格对照技术栈。\n"
        + render_required_outputs([f"10-项目/{project}/系统设计.md"])
    )

    task_prompt = base_prompt + (
        "**本次只输出给技术主管的任务清单**，不要重复系统设计内容。\n"
        "每项标注归属角色（后端/前端）、输入输出、验收标准、工作量（不超过 4 小时）。\n"
        + render_required_outputs([f"10-项目/{project}/指令/给技术主管.md"])
    )

    written = []
    for pass_name, user_prompt, fallback_name in [
        ("系统设计", design_prompt, "系统设计.md"),
        ("任务清单", task_prompt, "指令/给技术主管.md"),
    ]:
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
            dest = proj_dir / fallback_name
            dest.parent.mkdir(parents=True, exist_ok=True)
            write_output_atomic(dest, raw_output)
            written.append(f"10-项目/{project}/{fallback_name}")
            print(f"[{ROLE}] 未检测到 FILE 标签，降级写入 {dest}")
        else:
            for rel_path, content in output_files.items():
                rel_resolved = rel_path.replace("{project}", project)
                dest = resolve_path(rel_resolved, project)
                write_output_atomic(dest, content)
                print(f"[{ROLE}] 写入: {dest}")
                written.append(rel_resolved)

    # 架构师 status 保持 monitoring（角色基因里的初始值），
    # 仅刷新 last_run + 重置失败计数；不强行转 idle，否则破坏 monitoring 语义
    set_role_status(
        ROLE, status="monitoring",
        reset_counters=True, enforce_transition=False,
    )
    append_audit({
        "timestamp": utc_now(), "role": ROLE, "project": project,
        "task": task, "result": "success", "outputs": written,
    })
    print(f"[{ROLE}] 完成，输出：{written}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
