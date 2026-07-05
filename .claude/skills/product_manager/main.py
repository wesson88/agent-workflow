"""
product_manager/main.py — 产品经理执行入口（Phase 2b vault-based）

输入（vault）：
  - 10-项目/{project}/inputs/*.md   素材（business_brief / brainstorm-* / meeting-* / ...）
  - 00-系统/规则/技术栈.md           技术栈约束

输出（vault）：
  - 10-项目/{project}/PRD.md

CLI：
  python .claude/skills/product_manager/main.py --task "..." --project myproj
"""

from __future__ import annotations

import sys
from pathlib import Path

# 把 skills/ 加入 sys.path，能 import common；common.py 内部已加 .claude/ 路径以 import engine
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import (
    parse_args, resolve_project, build_system_prompt, read_input_files,
    write_output_atomic, parse_claude_output_to_files,
    call_claude, append_audit, utc_now, load_rule_block,
)
from engine import (
    set_role_status, role_is_blocked, get_role_status,
    project_dir, rules_dir, resolve_path,
    load_role,
)

ROLE = "产品经理"
PLACEHOLDER_TASKS = {"", "处理数学分析"}


def collect_input_docs(inputs_dir: Path) -> list[Path]:
    """扫描素材目录。

    排除规则：README.md / *.example.* / 隐藏文件 / 空文件
    排序：business_brief.md 置顶（事实基线），其余字典序
    """
    if not inputs_dir.exists():
        return []
    docs: list[Path] = []
    for p in sorted(inputs_dir.glob("*.md")):
        name = p.name
        if name == "README.md" or ".example." in name or name.startswith("."):
            continue
        try:
            if not p.read_text(encoding="utf-8").strip():
                continue
        except Exception:
            continue
        docs.append(p)
    docs.sort(key=lambda p: (0 if p.name == "business_brief.md" else 1, p.name))
    return docs


def main() -> int:
    args = parse_args()
    task = (args.task or "").strip()
    project = resolve_project(args)

    if role_is_blocked(ROLE):
        print(f"[{ROLE}] status=blocked，跳过。需上级或人工介入解除。", file=sys.stderr)
        return 1

    # 进入 busy（容忍状态残留）
    set_role_status(ROLE, status="busy", enforce_transition=False)

    proj_dir = project_dir(project)
    inputs_dir = proj_dir / "inputs"
    tech_stack = rules_dir() / "技术栈.md"

    # 输入扫描
    input_docs = collect_input_docs(inputs_dir)
    has_docs = len(input_docs) > 0
    has_task = bool(task) and task not in PLACEHOLDER_TASKS

    if not has_docs and not has_task:
        msg = (
            f"[{ROLE}] 输入缺失：{inputs_dir} 下没有可读素材，"
            f"也没有有效的 --task。请在该目录放至少一份 .md 素材，或设置 --task / TASK。"
        )
        print(msg, file=sys.stderr)
        set_role_status(
            ROLE, status="failed",
            increment_consecutive_failures=True, increment_error=True,
        )
        append_audit({
            "timestamp": utc_now(), "role": ROLE, "project": project,
            "task": task, "result": "failed", "error": "missing_input",
        })
        return 2

    if has_docs:
        print(
            f"[{ROLE}] 读取到 {len(input_docs)} 份素材："
            f"{[p.name for p in input_docs]}",
            flush=True,
        )

    # system prompt：本角色笔记 + 上游补丁（PM 无上游）+ 输出格式规范
    system_prompt = build_system_prompt(ROLE, project=project)

    # user prompt
    context_files = input_docs + [tech_stack]
    context = read_input_files(context_files)

    # §3.4 rule_refs 章节级注入：frontmatter 声明的规则章节按需拼进 context
    role_def = load_role(ROLE)
    rule_block, source_hint = load_rule_block(role_def.rule_refs)
    print(f"[{ROLE}] rule_refs 注入：{source_hint}")
    if rule_block:
        context = context + "\n\n" + rule_block

    source_list = "\n".join(
        f"- `{p.name}` → `inputs/{p.name}`" for p in input_docs
    )
    user_prompt = (
        f"项目名：`{project}`（写文件时把路径里的 `{{project}}` 占位符替换为本值）\n\n"
        f"{context}\n\n---\n"
        f"本轮业务诉求：{task or '（未提供，请基于上述素材综合推导）'}\n\n"
        "以上 `=== 文件名 ===` 块可能同时包含业务简报、脑暴产出、会议纪要、"
        "用户/竞品调研、其他模型的 specs/plans 等。请综合所有输入，识别其中"
        "一致与冲突的部分，对冲突项放入 PRD 的『待确认项』章节。\n\n"
        "PRD 末尾必须包含『参考资料（Source Materials）』章节，列出本次综合"
        "用到的每份素材，使用以下相对链接（PRD 位于项目目录，inputs/ 是子目录）：\n"
        f"{source_list}\n\n"
        "请严格按照角色笔记 PRD 输出模板生成产物，用一个 FILE 块包裹：\n"
        f"<!-- FILE: 10-项目/{project}/PRD.md -->\n"
        "（PRD 内容）\n"
        "<!-- /FILE -->\n\n"
        "所有无法从输入中确定的事实必须放入『待确认项』，不要编造。"
    )

    # Claude 调用
    try:
        raw_output = call_claude(system_prompt, user_prompt, ROLE)
    except Exception as e:
        print(f"[{ROLE}] Claude API 调用失败：{e}", file=sys.stderr)
        set_role_status(
            ROLE, status="failed",
            increment_consecutive_failures=True, increment_error=True,
        )
        append_audit({
            "timestamp": utc_now(), "role": ROLE, "project": project,
            "task": task, "result": "failed", "error": str(e),
        })
        return 1

    # 写盘
    output_files = parse_claude_output_to_files(raw_output)
    if not output_files:
        # 降级：未识别 FILE 标签，整体写入 PRD.md
        dest = proj_dir / "PRD.md"
        write_output_atomic(dest, raw_output)
        written = [f"10-项目/{project}/PRD.md"]
        print(f"[{ROLE}] 未检测到 FILE 标签，降级写入 {dest}")
    else:
        written = []
        for rel_path, content in output_files.items():
            # 替换占位符（防 Claude 偶尔忘记替换）
            rel_resolved = rel_path.replace("{project}", project)
            dest = resolve_path(rel_resolved, project)
            write_output_atomic(dest, content)
            print(f"[{ROLE}] 写入: {dest}")
            written.append(rel_resolved)

    # 状态收尾：success → idle，重置失败计数
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
