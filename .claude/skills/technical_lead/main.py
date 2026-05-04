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

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import (
    parse_args, build_system_prompt, read_input_files,
    write_output_atomic, parse_claude_output_to_files,
    call_claude, append_audit, utc_now, render_required_outputs,
)
from engine import (
    set_role_status, role_is_blocked,
    project_dir, rules_dir, resolve_path,
)

ROLE = "技术主管"


def _resolve_project(args) -> str:
    return (
        args.project
        or os.environ.get("PROJECT")
        or os.environ.get("PROJECT_NAME")
        or "default"
    ).strip() or "default"


def main() -> int:
    args = parse_args()
    task = (args.task or "").strip()
    project = _resolve_project(args)

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
    context = read_input_files([to_lead, sys_design, tech_stack])

    user_prompt = (
        f"项目名：`{project}`\n\n"
        f"{context}\n\n---\n"
        f"本轮任务：{task or '按架构设计拆分前后端开发任务'}\n\n"
        "请将开发任务拆分为前端与后端两条线，每条任务必须有：\n"
        "  - 功能描述、对应模块/接口、输入输出、验收标准\n"
        "  - 单任务工作量 ≤ 4 小时，超过须再拆分\n"
        "  - 标注前后端协作关系（如 API 契约、数据流）\n"
        + render_required_outputs([
            f"10-项目/{project}/指令/给后端.md",
            f"10-项目/{project}/指令/给前端.md",
        ])
    )

    try:
        raw_output = call_claude(system_prompt, user_prompt, ROLE)
    except Exception as e:
        print(f"[{ROLE}] Claude API 调用失败：{e}", file=sys.stderr)
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
        # 降级：整体写入给后端.md
        dest = proj_dir / "指令" / "给后端.md"
        write_output_atomic(dest, raw_output)
        written = [f"10-项目/{project}/指令/给后端.md"]
        print(f"[{ROLE}] 未检测到 FILE 标签，降级写入 {dest}")
    else:
        written = []
        for rel_path, content in output_files.items():
            rel_resolved = rel_path.replace("{project}", project)
            dest = resolve_path(rel_resolved, project)
            write_output_atomic(dest, content)
            print(f"[{ROLE}] 写入: {dest}")
            written.append(rel_resolved)

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
