"""
product_manager/main.py
产品经理执行层：读取业务简报与任务描述，生成结构化 PRD
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import (
    parse_args, build_system_prompt, read_input_files,
    write_output_atomic, parse_claude_output_to_files,
    update_skill_status, append_audit, utc_now,
    get_claude_root, get_project_root, call_claude,
    get_docs_dir, get_requirements_dir,
)

SKILL_NAME = "product_manager"
PLACEHOLDER_TASKS = {"", "处理数学分析"}


def main():
    args = parse_args()
    task = (args.task or "").strip()

    update_skill_status(SKILL_NAME, {"status": "busy"})

    claude_root = get_claude_root()
    project_root = get_project_root()
    brief_path = get_requirements_dir() / "business_brief.md"

    has_brief = brief_path.exists() and brief_path.read_text(encoding="utf-8").strip()
    has_task = task and task not in PLACEHOLDER_TASKS

    if not has_brief and not has_task:
        msg = (
            f"[{SKILL_NAME}] 输入缺失：既没有 {brief_path} 也没有有效的 --task。"
            f"请提供 requirements/business_brief.md 或设置 TASK 环境变量。"
        )
        print(msg, file=sys.stderr)
        update_skill_status(SKILL_NAME, {"status": "failed"})
        append_audit({
            "timestamp": utc_now(),
            "skill": SKILL_NAME,
            "task": task,
            "action": "skill_run",
            "result": "failed",
            "error": "missing_input",
        })
        sys.exit(2)

    # system prompt：本技能 skill.md + 输出格式规范
    system_prompt = build_system_prompt(SKILL_NAME)

    # user prompt：合并输入文件 + 任务指令
    input_files = [
        brief_path,
        get_docs_dir() / "tech_stack.md",
        claude_root / "status.json",
    ]
    context = read_input_files(input_files)
    user_prompt = (
        f"{context}\n\n---\n本轮业务诉求：{task or '（未提供，请基于 business_brief.md 推导）'}\n\n"
        "请严格按照你的 PRD 输出模板（skill.md 第 6 节）生成 requirements/PRD.md。"
        "所有无法从输入中确定的问题必须放入『待确认项』章节，不要编造事实。"
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
        print(f"[{SKILL_NAME}] 未检测到 FILE 标签，降级写入 requirements/PRD.md")
        dest = get_requirements_dir() / "PRD.md"
        write_output_atomic(dest, raw_output)
        written = ["requirements/PRD.md"]
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
