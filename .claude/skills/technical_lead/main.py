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
)

ROLE = "技术主管"


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
        f"  `10-项目/{project}/指令/给后端-T01.md`、`给后端-T02.md` ...\n"
        + render_required_outputs([f"10-项目/{project}/指令/给后端-索引.md"])
    )

    frontend_prompt = base_prompt + (
        "**本次只输出前端任务文件**，不要输出任何后端内容。\n"
        "标注与后端的协作关系（API 契约、数据流）。\n"
        "每个任务单独一个 FILE 块，按编号命名：\n"
        f"  `10-项目/{project}/指令/给前端-T01.md`、`给前端-T02.md` ...\n"
        + render_required_outputs([f"10-项目/{project}/指令/给前端-索引.md"])
    )

    LIMIT_CHARS = 30 * 1024
    written = []

    for pass_name, user_prompt in [("后端任务", backend_prompt), ("前端任务", frontend_prompt)]:
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
