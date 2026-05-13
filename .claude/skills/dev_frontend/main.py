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


def main() -> int:
    args = parse_args()
    task = (args.task or "").strip()
    project = resolve_project(args)

    if role_is_blocked(ROLE):
        print(f"[{ROLE}] status=blocked，跳过。", file=sys.stderr)
        return 1

    set_role_status(ROLE, status="busy", enforce_transition=False)

    proj_dir = project_dir(project)
    to_frontend_orig = proj_dir / "指令" / "给前端.md"
    to_frontend_compressed = proj_dir / "指令" / "给前端-压缩.md"
    # § 15 层三：优先使用 haiku 压缩版
    to_frontend = to_frontend_compressed if to_frontend_compressed.exists() else to_frontend_orig
    if to_frontend_compressed.exists():
        print(f"[{ROLE}] 📄 使用压缩版指令：给前端-压缩.md")
    prd = proj_dir / "PRD.md"
    sys_design = proj_dir / "系统设计.md"
    api_spec = proj_dir / "API契约.md"     # 可选（后端先跑则有）
    tech_stack = rules_dir() / "技术栈.md"

    if not to_frontend_orig.exists() or not sys_design.exists():
        print(
            f"[{ROLE}] 必需输入缺失：{to_frontend_orig} 或 {sys_design}。请先跑技术主管。",
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

    # § 15 层二：输入预检 —— 给前端.md 体积告警
    frontend_size = to_frontend.stat().st_size if to_frontend.exists() else 0
    if frontend_size > 30 * 1024:
        print(
            f"[{ROLE}] ⚠️ 给前端.md 体积 {frontend_size // 1024}KB 超过 30KB 约束。"
            f"技术主管产出可能未遵守输出体积限制（§15 层一）。"
            f"本次仍继续执行，但输入将被截断到 25000 chars。",
            file=sys.stderr,
        )

    system_prompt = build_system_prompt(ROLE, project=project)
    context = read_input_files([prd, to_frontend, sys_design, api_spec, tech_stack])

    user_prompt = (
        f"项目名：`{project}`\n\n"
        f"{context}\n\n---\n"
        f"本轮任务：{task or '按指令实现前端代码'}\n\n"
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
                f"[{ROLE}] ⚠️ 疑似输出超限（max_tokens）。建议：\n"
                f"  1. 检查给前端.md 是否仍超 30KB（§15 层一）\n"
                f"  2. 在工作流 YAML 中为前端工程师配置 sub_tasks 分轮执行（§15 层四）",
                file=sys.stderr,
            )
        print(f"[{ROLE}] Claude API 调用失败：{e}", file=sys.stderr)
        set_role_status(
            ROLE, status="failed",
            increment_consecutive_failures=True, increment_error=True,
            enforce_transition=False,
        )
        append_audit({
            "timestamp": utc_now(), "role": ROLE, "project": project,
            "task": task, "result": "failed", "error": err_str,
            "error_phase": error_phase,
        })
        return 1

    output_files = parse_claude_output_to_files(raw_output)
    if not output_files:
        # 降级：整体写入 src/frontend/index.html
        dest = PROJECT_ROOT / "src" / "frontend" / "index.html"
        write_output_atomic(dest, raw_output)
        written = ["src/frontend/index.html"]
        print(f"[{ROLE}] 未检测到 FILE 标签，降级写入 {dest}")
    else:
        written = []
        for rel_path, content in output_files.items():
            rel_path = rel_path.replace("{project}", project)
            dest = resolve_path(rel_path, project)
            write_output_atomic(dest, content)
            print(f"[{ROLE}] 写入: {dest}")
            written.append(rel_path)

    set_role_status(
        ROLE, status="success",
        reset_counters=True, last_output_path="src/frontend/",
    )
    set_role_status(ROLE, status="idle")
    append_audit({
        "timestamp": utc_now(), "role": ROLE, "project": project,
        "task": task, "result": "success", "outputs": written,
    })
    print(f"[{ROLE}] 完成，输出：{written}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
