"""
dev_frontend/main.py — 前端工程师执行入口（Phase 2b vault-based）

输入（vault）：
  - 10-项目/{project}/指令/给前端.md   技术主管下发的任务
  - 10-项目/{project}/PRD.md            产品需求
  - 10-项目/{project}/系统设计.md       系统设计
  - 10-项目/{project}/API契约.md        后端 API（若已有）
  - 00-系统/规则/技术栈.md               技术栈

输出：
  - src/frontend/                 ← 项目仓内（不进 vault）
  - tests/frontend/               ← 项目仓内（不进 vault）

CLI：
  python .claude/skills/dev_frontend/main.py --task "..." --project myproj
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
    project_dir, rules_dir, resolve_path, PROJECT_ROOT,
)

ROLE = "前端工程师"


def _collect_task_files(proj_dir, role_prefix: str):
    """收集前端任务文件列表，优先按编号拆分，降级到整体文件。"""
    instr_dir = proj_dir / "指令"
    split_files = sorted(instr_dir.glob(f"{role_prefix}-T*.md"))
    if split_files:
        return split_files, True
    compressed = instr_dir / f"{role_prefix}-压缩.md"
    original = instr_dir / f"{role_prefix}.md"
    single = compressed if compressed.exists() else original
    return ([single] if single.exists() else []), False


def main() -> int:
    args = parse_args()
    task = (args.task or "").strip()
    project = resolve_project(args)

    if role_is_blocked(ROLE):
        print(f"[{ROLE}] status=blocked，跳过。", file=sys.stderr)
        return 1

    set_role_status(ROLE, status="busy", enforce_transition=False)

    proj_dir = project_dir(project)
    tech_stack = rules_dir() / "技术栈.md"

    task_files, use_split = _collect_task_files(proj_dir, "给前端")

    if not task_files:
        print(
            f"[{ROLE}] 必需输入缺失：{proj_dir}/指令/给前端*.md。请先跑技术主管。",
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
        })
        return 2

    if use_split:
        print(f"[{ROLE}] 📋 按任务拆分模式，共 {len(task_files)} 个任务文件")
    else:
        single = task_files[0]
        size = single.stat().st_size if single.exists() else 0
        if size > 30 * 1024:
            print(
                f"[{ROLE}] ⚠️ {single.name} 体积 {size // 1024}KB 超过 30KB 约束。"
                f"建议技术主管切换为按任务拆分输出。",
                file=sys.stderr,
            )

    system_prompt = build_system_prompt(ROLE, project=project)
    written_all: list[str] = []

    for task_file in task_files:
        task_label = task_file.stem
        print(f"[{ROLE}] ▶ 执行任务：{task_label}")

        # 每个任务只加载自己的指令文件 + 技术栈（最小上下文）
        context = read_input_files([task_file, tech_stack])

        user_prompt = (
            f"项目名：`{project}`\n\n"
            f"{context}\n\n---\n"
            f"本轮任务：{task or f'实现 {task_label} 中的前端代码'}\n\n"
            "请按指令清单完整实现前端：\n"
            "  - 入口 HTML + 主 JS：`src/frontend/index.html`、`src/frontend/app.js`\n"
            "  - 公共组件：`src/frontend/components/...`\n"
            "  - 页面：`src/frontend/pages/...`\n"
            "  - 状态管理：`src/frontend/store/...`\n"
            "  - 样式：`src/frontend/styles/...`\n"
            "  - 测试：`tests/frontend/...`\n\n"
            "技术栈严格按 `00-系统/规则/技术栈.md`；需含全局 Error Boundary、"
            "loading/error 状态、响应式适配。\n"
            + render_required_outputs([
                "src/frontend/index.html",
                "src/frontend/app.js",
                "src/frontend/styles/main.css",
                "tests/frontend/test_<x>.js",
            ])
            + "\n上面是路径**示例**；请根据指令清单中的实际功能划分产出对应文件，每个文件用一个 FILE 块。"
        )

        try:
            raw_output = call_claude(system_prompt, user_prompt, ROLE)
        except Exception as e:
            err_str = str(e)
            error_phase = "llm_call"
            if any(k in err_str.lower() for k in ["context", "token", "length", "limit"]):
                error_phase = "output_overflow"
                print(
                    f"[{ROLE}] ⚠️ 疑似输出超限（{task_label}）。任务文件过大，"
                    f"建议进一步拆分。",
                    file=sys.stderr,
                )
            print(f"[{ROLE}] Claude API 调用失败（{task_label}）：{e}", file=sys.stderr)
            set_role_status(
                ROLE, status="failed",
                increment_consecutive_failures=True, increment_error=True,
                enforce_transition=False,
            )
            append_audit({
                "timestamp": utc_now(), "role": ROLE, "project": project,
                "task": task_label, "result": "failed", "error": err_str,
                "error_phase": error_phase,
            })
            return 1

        output_files = parse_claude_output_to_files(raw_output)
        if not output_files:
            dest = PROJECT_ROOT / "src" / "frontend" / f"{task_label}_index.html"
            write_output_atomic(dest, raw_output)
            written_all.append(str(dest))
            print(f"[{ROLE}] 未检测到 FILE 标签，降级写入 {dest}")
        else:
            for rel_path, content in output_files.items():
                rel_path = rel_path.replace("{project}", project)
                dest = resolve_path(rel_path, project)
                write_output_atomic(dest, content)
                print(f"[{ROLE}] 写入: {dest}")
                written_all.append(rel_path)

    set_role_status(
        ROLE, status="success",
        reset_counters=True, last_output_path="src/frontend/",
    )
    set_role_status(ROLE, status="idle")
    append_audit({
        "timestamp": utc_now(), "role": ROLE, "project": project,
        "task": task, "result": "success", "outputs": written_all,
    })
    print(f"[{ROLE}] 完成，输出：{written_all}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
