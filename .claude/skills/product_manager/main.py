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
    get_docs_dir, get_inputs_dir,
)

SKILL_NAME = "product_manager"
PLACEHOLDER_TASKS = {"", "处理数学分析"}


def collect_input_docs(inputs_dir):
    """扫描 inputs/ 下所有 .md 素材作为 product_manager 的输入。

    排除规则：
    - README.md（目录说明）
    - *.example.*（模板文件）
    - 以 . 开头的隐藏文件
    - 空文件

    排序：business_brief.md 优先置顶（视为事实基线），其余按文件名字典序。
    """
    if not inputs_dir.exists():
        return []
    docs = []
    for p in sorted(inputs_dir.glob("*.md")):
        name = p.name
        if name == "README.md" or ".example." in name or name.startswith("."):
            continue
        if not p.read_text(encoding="utf-8").strip():
            continue
        docs.append(p)
    docs.sort(key=lambda p: (0 if p.name == "business_brief.md" else 1, p.name))
    return docs


def main():
    args = parse_args()
    task = (args.task or "").strip()

    update_skill_status(SKILL_NAME, {"status": "busy"})

    claude_root = get_claude_root()
    project_root = get_project_root()
    inputs_dir = get_inputs_dir()

    input_docs = collect_input_docs(inputs_dir)
    has_docs = len(input_docs) > 0
    has_task = task and task not in PLACEHOLDER_TASKS

    if not has_docs and not has_task:
        msg = (
            f"[{SKILL_NAME}] 输入缺失：{inputs_dir} 下没有可读的素材文件，"
            f"也没有有效的 --task。请在 .claude/inputs/ 放入至少一份 .md "
            f"（business_brief.md / brainstorm-*.md / meeting-*.md 等），"
            f"或设置 TASK 环境变量。"
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

    if has_docs:
        doc_names = [p.name for p in input_docs]
        print(f"[{SKILL_NAME}] 读取到 {len(input_docs)} 份素材：{doc_names}", flush=True)

    # system prompt：本技能 skill.md + 输出格式规范
    system_prompt = build_system_prompt(SKILL_NAME)

    # user prompt：素材 + 技术栈 + 状态
    input_files = input_docs + [
        get_docs_dir() / "tech_stack.md",
        claude_root / "status.json",
    ]
    context = read_input_files(input_files)

    # 明确告知 LLM 可引用的素材文件名列表，便于生成参考资料章节的相对链接
    source_list = "\n".join(f"- `{p.name}` → `../inputs/{p.name}`" for p in input_docs)
    user_prompt = (
        f"{context}\n\n---\n本轮业务诉求：{task or '（未提供，请基于上述素材综合推导）'}\n\n"
        "以上 `=== 文件名 ===` 块可能同时包含业务简报、脑暴产出、会议纪要、"
        "用户/竞品调研、其他模型的 specs/plans 等。请综合所有输入，识别其中"
        "一致与冲突的部分，对冲突项放入 PRD 的『待确认项』章节。\n\n"
        "PRD 末尾必须包含『参考资料（Source Materials）』章节，列出本次综合"
        "用到的每份素材，用以下相对链接格式（PRD.md 位于 requirements/，素材位于 inputs/）：\n"
        f"{source_list}\n\n"
        "请严格按照你的 PRD 输出模板（skill.md 第 6 节）生成 requirements/PRD.md。"
        "所有无法从输入中确定的事实必须放入『待确认项』章节，不要编造。"
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
