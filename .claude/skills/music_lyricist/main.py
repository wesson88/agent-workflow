"""
music_lyricist/main.py — 作词执行入口（音乐域 L2-B）

输入（vault，来源：角色 frontmatter `inputs` 字段）：
  - 10-项目/music/{project}/inputs/创作简报.md
  - 10-项目/music/{project}/创作 vision.md
  - 10-项目/music/{project}/指令/给作词.md

输出（vault，来源：角色 frontmatter `outputs` 字段）：
  - 10-项目/music/{project}/词作.md

CLI：
  python .claude/skills/music_lyricist/main.py --task "..." --project myproj
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import (
    parse_args, resolve_project, build_system_prompt, read_input_files,
    write_output_atomic, parse_claude_output_to_files,
    call_claude, append_audit, utc_now, render_required_outputs,
    load_rule_block,
)
from engine import (
    set_role_status, role_is_blocked,
    resolve_path,
)
from engine.role_loader import load_role

ROLE = "作词"


def main() -> int:
    args = parse_args()
    task = (args.task or "").strip()
    project = resolve_project(args)

    if role_is_blocked(ROLE):
        print(f"[{ROLE}] status=blocked，跳过。", file=sys.stderr)
        return 1

    set_role_status(ROLE, status="busy", enforce_transition=False)

    role_def = load_role(ROLE)
    input_paths = [resolve_path(p, project) for p in role_def.inputs]
    output_rels = [p.replace("{project}", project) for p in role_def.outputs]

    # 上游硬约束：指令/给作词.md 必须存在（制作人扇出过）
    instruction_path = next(
        (p for p in input_paths if p.name == "给作词.md"),
        None,
    )
    if instruction_path is None or not instruction_path.exists():
        print(
            f"[{ROLE}] 上游缺失：未找到 `指令/给作词.md`。请先跑制作人扇出。",
            file=sys.stderr,
        )
        set_role_status(
            ROLE, status="failed",
            increment_consecutive_failures=True, increment_error=True,
            enforce_transition=False,
        )
        append_audit({
            "timestamp": utc_now(), "role": ROLE, "project": project,
            "task": task, "result": "failed", "error": "missing_instruction",
        })
        return 2

    existing_inputs = [p for p in input_paths if p.exists()]
    print(
        f"[{ROLE}] 上游 {len(existing_inputs)}/{len(input_paths)} 就位："
        f"{[p.name for p in existing_inputs]}",
        flush=True,
    )

    system_prompt = build_system_prompt(ROLE, project=project)
    context = read_input_files(input_paths)

    rule_block, source_hint = load_rule_block(role_def.rule_refs)
    print(f"[{ROLE}] rule_refs 注入：{source_hint}")
    if rule_block:
        context = context + "\n\n" + rule_block

    user_prompt = (
        f"项目名：`{project}`（写文件时把路径里的 `{{project}}` 占位符替换为本值）\n\n"
        f"{context}\n\n---\n"
        f"本轮作词诉求：{task or '（未提供，请基于上游指令 + vision + 简报综合推导）'}\n\n"
        "作为作词，请产 `词作.md`：\n"
        "- **章节结构严格按 [[产物schema#3. 词作.md（作词产出）]]「必填章节」模板 verbatim 输出**：\n"
        "  §1 段落标记 + 歌词 / §2 钩子句标注 / §3 押韵设计 / §4 语言 / 风格 / §5 主题立意自检 / §6 边界自检\n"
        "- 严格遵守制作人指令的段落结构（如 verse-chorus-bridge 数 / 总时长目标）\n"
        "- 流派 idiom 与 vision 锚域一致（民谣的留白 vs 雷鬼的呼喊感，需在词上体现）\n"
        "- 押韵服务情感，不堆词\n"
        "- 不越界写编曲 / 作曲建议（节奏型 / 调性 / 乐器配置等留给下游）\n\n"
        "产物只有 1 份（`词作.md`），单 FILE 块即可。"
        f"{render_required_outputs(output_rels)}"
    )

    try:
        raw_output = call_claude(system_prompt, user_prompt, ROLE)
    except Exception as e:
        print(f"[{ROLE}] LLM 调用失败：{e}", file=sys.stderr)
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
        print(
            f"[{ROLE}] 未检测到 FILE 块。原始输出长度 {len(raw_output)}。",
            file=sys.stderr,
        )
        set_role_status(
            ROLE, status="failed",
            increment_consecutive_failures=True, increment_error=True,
            enforce_transition=False,
        )
        append_audit({
            "timestamp": utc_now(), "role": ROLE, "project": project,
            "task": task, "result": "failed", "error": "no_file_blocks",
        })
        return 1

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
