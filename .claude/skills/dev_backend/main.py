"""
dev_backend/main.py — 后端工程师执行入口（Phase 2b vault-based）

输入（vault）：
  - 10-项目/{project}/指令/给后端.md   技术主管下发的任务
  - 10-项目/{project}/系统设计.md       系统设计
  - 10-项目/{project}/PRD.md            产品需求
  - 00-系统/规则/技术栈.md               技术栈

输出：
  - src/backend/                  ← 项目仓内（不进 vault）
  - tests/backend/                ← 项目仓内（不进 vault）
  - 10-项目/{project}/API契约.md  ← vault 内

CLI：
  python .claude/skills/dev_backend/main.py --task "..." --project myproj
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
    project_dir, rules_dir, resolve_path, PROJECT_ROOT,
)

ROLE = "后端工程师"


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
    to_backend_orig = proj_dir / "指令" / "给后端.md"
    to_backend_compressed = proj_dir / "指令" / "给后端-压缩.md"
    # § 15 层三：优先使用 haiku 压缩版（体积更小，下游读取更安全）
    to_backend = to_backend_compressed if to_backend_compressed.exists() else to_backend_orig
    if to_backend_compressed.exists():
        print(f"[{ROLE}] 📄 使用压缩版指令：给后端-压缩.md")
    sys_design = proj_dir / "系统设计.md"
    prd = proj_dir / "PRD.md"
    tech_stack = rules_dir() / "技术栈.md"

    if not to_backend_orig.exists() or not sys_design.exists():
        print(
            f"[{ROLE}] 必需输入缺失：{to_backend_orig} 或 {sys_design}。请先跑技术主管。",
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

    # § 15 层二：输入预检 —— 给后端.md 体积告警
    backend_size = to_backend.stat().st_size if to_backend.exists() else 0
    if backend_size > 30 * 1024:  # 30KB
        print(
            f"[{ROLE}] ⚠️ 给后端.md 体积 {backend_size // 1024}KB 超过 30KB 约束。"
            f"技术主管产出可能未遵守输出体积限制（§15 层一）。"
            f"本次仍继续执行，但输入将被截断到 25000 chars。",
            file=sys.stderr,
        )

    system_prompt = build_system_prompt(ROLE, project=project)
    context = read_input_files([to_backend, sys_design, prd, tech_stack])

    user_prompt = (
        f"项目名：`{project}`\n\n"
        f"{context}\n\n---\n"
        f"本轮任务：{task or '按指令实现后端代码'}\n\n"
        "请按指令清单完整实现后端：\n"
        "  - 后端代码：路径以 `src/backend/...` 开头（项目仓内）\n"
        "  - 测试代码：路径以 `tests/backend/...` 开头\n"
        f"  - API 文档：路径为 `10-项目/{project}/API契约.md`（vault 内，Swagger/OpenAPI 风格）\n\n"
        "技术栈严格按 `00-系统/规则/技术栈.md`，禁止引入未授权依赖。\n"
        "所有 API 必须含输入校验、鉴权、结构化日志。\n"
        + render_required_outputs([
            "src/backend/main.py",
            "src/backend/<module>.py",
            "tests/backend/test_<module>.py",
            f"10-项目/{project}/API契约.md",
        ])
        + "\n上面是路径**示例**；请根据指令清单中的实际模块产出对应文件，每个文件用一个 FILE 块。"
    )

    try:
        raw_output = call_claude(system_prompt, user_prompt, ROLE)
    except Exception as e:
        err_str = str(e)
        # § 15 层四：输出端超限检测
        error_phase = "llm_call"
        if any(k in err_str.lower() for k in ["context", "token", "length", "limit"]):
            error_phase = "output_overflow"
            print(
                f"[{ROLE}] ⚠️ 疑似输出超限（max_tokens）。建议：\n"
                f"  1. 检查给后端.md 是否仍超 30KB（§15 层一）\n"
                f"  2. 在工作流 YAML 中为后端工程师配置 sub_tasks 分轮执行（§15 层四）",
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
        # 降级：整体写入 src/backend/output.py
        dest = PROJECT_ROOT / "src" / "backend" / "output.py"
        write_output_atomic(dest, raw_output)
        written = ["src/backend/output.py"]
        print(f"[{ROLE}] 未检测到 FILE 标签，降级写入 {dest}")
    else:
        written = []
        for rel_path, content in output_files.items():
            rel_resolved = rel_path.replace("{project}", project)
            dest = resolve_path(rel_resolved, project)
            write_output_atomic(dest, content)
            print(f"[{ROLE}] 写入: {dest}")
            written.append(rel_resolved)

    set_role_status(
        ROLE, status="success",
        reset_counters=True, last_output_path="src/backend/",
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
