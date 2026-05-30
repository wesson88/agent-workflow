"""
music_producer/main.py — 制作人执行入口（音乐域 L2-A 起步）

输入（vault，来源：角色 frontmatter `inputs` 字段）：
  - 10-项目/music/{project}/inputs/创作简报.md
  - 10-项目/music/{project}/创作 vision.md
  - 10-项目/music/{project}/指令/给制作人.md

输出（vault，来源：角色 frontmatter `outputs` 字段）：
  - 10-项目/music/{project}/制作计划.md
  - 10-项目/music/{project}/指令/给{角色}.md  ← 扇出，按 downstream 每角色一份

CLI：
  python .claude/skills/music_producer/main.py --task "..." --project myproj
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

ROLE = "制作人"


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
    downstream = list(role_def.downstream)

    # 验上游产物：vision.md 必须存在（音乐总监跑过）
    vision_path = next(
        (p for p in input_paths if p.name == "创作 vision.md"),
        None,
    )
    if vision_path is None or not vision_path.exists():
        print(
            f"[{ROLE}] 上游缺失：未找到 `创作 vision.md`。请先跑音乐总监。",
            file=sys.stderr,
        )
        set_role_status(
            ROLE, status="failed",
            increment_consecutive_failures=True, increment_error=True,
            enforce_transition=False,
        )
        append_audit({
            "timestamp": utc_now(), "role": ROLE, "project": project,
            "task": task, "result": "failed", "error": "missing_vision",
        })
        return 2

    print(
        f"[{ROLE}] 上游 vision 已就位；downstream={downstream}",
        flush=True,
    )

    # 扇出输出清单：制作计划 + 每个下游一份指令
    output_rels = [
        f"10-项目/music/{project}/制作计划.md",
    ] + [
        f"10-项目/music/{project}/指令/给{role}.md" for role in downstream
    ]

    system_prompt = build_system_prompt(ROLE, project=project)
    context = read_input_files(input_paths)

    rule_block, source_hint = load_rule_block(role_def.rule_refs)
    print(f"[{ROLE}] rule_refs 注入：{source_hint}")
    if rule_block:
        context = context + "\n\n" + rule_block

    fanout_list = "\n".join(f"  - 给 {r}" for r in downstream)
    user_prompt = (
        f"项目名：`{project}`（写文件时把路径里的 `{{project}}` 占位符替换为本值）\n\n"
        f"{context}\n\n---\n"
        f"本轮制作人诉求：{task or '（未提供，请基于上游 vision + 简报综合推导）'}\n\n"
        "作为制作人，请产出：\n"
        f"1. `制作计划.md` — 项目统筹层（流派配比 / 时间线 / 角色调度 / 质量节点 / "
        f"probational 角色决策 promote/dormant）\n"
        f"2. **扇出 {len(downstream)} 份指令** — 每个下游一份独立 FILE 块（缺一不可）：\n"
        f"{fanout_list}\n\n"
        "**重要**：每份指令的具体内容要按对应下游角色的职责定制化，不要复制粘贴。"
        "如某个 probational 角色本项目 dormant，仍要产出一份 dormant 说明，不留空。"
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

    # 软告警：扇出文件数不达 downstream
    fanout_written = [w for w in written if "/指令/给" in w]
    if len(fanout_written) < len(downstream):
        print(
            f"[{ROLE}] ⚠️ 扇出指令数不足：期望 {len(downstream)}，"
            f"实际 {len(fanout_written)}（{fanout_written}）",
            file=sys.stderr,
        )

    set_role_status(ROLE, status="success", reset_counters=True)
    set_role_status(ROLE, status="idle")
    append_audit({
        "timestamp": utc_now(), "role": ROLE, "project": project,
        "task": task, "result": "success", "outputs": written,
        "fanout_count": len(fanout_written),
        "fanout_expected": len(downstream),
    })
    print(f"[{ROLE}] 完成，输出：{written}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
