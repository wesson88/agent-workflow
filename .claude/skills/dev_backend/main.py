"""
dev_backend/main.py
后端开发工程师执行层：读取后端任务指派，生成业务代码、API 接口和测试
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import (
    parse_args, build_system_prompt, read_input_files,
    write_output_atomic, parse_claude_output_to_files,
    update_skill_status, append_audit, utc_now,
    get_claude_root, get_project_root, call_claude,
    get_docs_dir, get_instructions_dir,
)

SKILL_NAME = "dev_backend"


def main():
    args = parse_args()
    task = args.task

    update_skill_status(SKILL_NAME, {"status": "busy"})

    project_root = get_project_root()

    # system prompt：本技能 skill.md + 上游 technical_lead 的动态补丁 + 输出格式规范
    system_prompt = build_system_prompt(SKILL_NAME, upstream_skill="technical_lead")

    input_files = [
        get_instructions_dir() / "to_backend.md",
        get_docs_dir() / "system_design.md",
        get_docs_dir() / "tech_stack.md",
    ]
    context = read_input_files(input_files)
    user_prompt = (
        f"{context}\n\n---\n任务指令：{task}\n\n"
        "请根据后端任务指派和系统设计，生成完整的后端代码文件（src/backend/ 目录下），"
        "包括 API 接口、业务逻辑、数据模型，以及 docs/api_spec.md。"
        "如有必要，同时生成 tests/backend/ 下的测试文件。"
    )

    try:
        raw_output = call_claude(system_prompt, user_prompt, SKILL_NAME)
    except Exception as e:
        print(f"[{SKILL_NAME}] Claude API 调用失败: {e}", file=sys.stderr)
        update_skill_status(SKILL_NAME, {"status": "failed"})
        append_audit({
            "timestamp": utc_now(),
            "skill": SKILL_NAME,
            "task": task,
            "action": "skill_run",
            "result": "failed",
            "error": str(e),
        })
        sys.exit(1)

    output_files = parse_claude_output_to_files(raw_output)

    if not output_files:
        print(f"[{SKILL_NAME}] 未检测到 FILE 标签，降级写入 src/backend/output.py")
        dest = project_root / "src" / "backend" / "output.py"
        write_output_atomic(dest, raw_output)
        written = ["src/backend/output.py"]
    else:
        written = []
        for rel_path, content in output_files.items():
            dest = project_root / rel_path
            write_output_atomic(dest, content)
            print(f"[{SKILL_NAME}] 写入: {dest}")
            written.append(rel_path)

    update_skill_status(SKILL_NAME, {
        "status": "success",
        "consecutive_failures": 0,
        "last_output_path": "src/backend/",
    })
    append_audit({
        "timestamp": utc_now(),
        "skill": SKILL_NAME,
        "task": task,
        "action": "skill_run",
        "result": "success",
        "outputs": written,
    })
    print(f"[{SKILL_NAME}] 完成，输出文件: {written}")
    sys.exit(0)


if __name__ == "__main__":
    main()
