"""
chief_architect/main.py
首席架构师执行层：读取 PRD，生成系统设计文档和技术主管任务指派
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import (
    parse_args, build_system_prompt, read_input_files,
    write_output_atomic, parse_claude_output_to_files,
    update_skill_status, append_audit, utc_now,
    get_claude_root, get_project_root, call_claude,
    get_docs_dir, get_requirements_dir, get_instructions_dir,
)

SKILL_NAME = "chief_architect"


def main():
    args = parse_args()
    task = args.task

    update_skill_status(SKILL_NAME, {"status": "busy"})

    claude_root = get_claude_root()
    project_root = get_project_root()

    # system prompt：本技能 skill.md（已含自身 DYNAMIC 区域）+ 输出格式规范
    system_prompt = build_system_prompt(SKILL_NAME)

    # user prompt：合并输入文件
    input_files = [
        get_requirements_dir() / "PRD.md",
        get_docs_dir() / "tech_stack.md",
        get_docs_dir() / "rules" / "arch_decomposition_rules.md",
        claude_root / "status.json",
    ]
    context = read_input_files(input_files)
    user_prompt = f"{context}\n\n---\n任务指令：{task}\n\n请严格按照你的角色职责和输出格式规范，生成系统设计文档和技术主管任务指派。"

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

    # 解析输出中的 FILE 块
    output_files = parse_claude_output_to_files(raw_output)

    if not output_files:
        # 降级：将全部输出写入 system_design.md
        print(f"[{SKILL_NAME}] 未检测到 FILE 标签，降级写入 docs/system_design.md")
        dest = get_docs_dir() / "system_design.md"
        write_output_atomic(dest, raw_output)
        written = ["docs/system_design.md"]
    else:
        written = []
        for rel_path, content in output_files.items():
            dest = project_root / rel_path
            write_output_atomic(dest, content)
            print(f"[{SKILL_NAME}] 写入: {dest}")
            written.append(rel_path)

    update_skill_status(SKILL_NAME, {"status": "success", "consecutive_failures": 0})
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
