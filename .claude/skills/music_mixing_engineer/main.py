"""
music_mixing_engineer/main.py — 混音师执行入口（音乐域 L3）

输入（vault，来源：角色 frontmatter `inputs` 字段）：
  - 10-项目/music/{project}/指令/给混音师.md
  - 10-项目/music/{project}/创作 vision.md
  - 10-项目/music/{project}/Suno-prompt.md
  - 10-项目/music/{project}/编曲方案.md
  - 10-项目/music/{project}/和声谱.md

输出（vault，来源：角色 frontmatter `outputs` 字段）：
  - 10-项目/music/{project}/混音评估.md
  - 10-项目/music/{project}/混音-Suno-retry补丁.md

CLI：
  python .claude/skills/music_mixing_engineer/main.py --task "..." --project myproj
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

ROLE = "混音师"


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

    # 上游硬约束：指令/给混音师.md + 编曲方案.md + 和声谱.md
    required_upstream = {"给混音师.md", "编曲方案.md", "和声谱.md"}
    missing = [
        p.name for p in input_paths
        if p.name in required_upstream and not p.exists()
    ]
    if missing:
        print(
            f"[{ROLE}] 上游缺失：{missing}。请先跑编曲 + 和声编写。",
            file=sys.stderr,
        )
        set_role_status(
            ROLE, status="failed",
            increment_consecutive_failures=True, increment_error=True,
            enforce_transition=False,
        )
        append_audit({
            "timestamp": utc_now(), "role": ROLE, "project": project,
            "task": task, "result": "failed", "error": f"missing_upstream:{missing}",
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
        f"本轮混音诉求：{task or '（未提供，请基于上游编曲方案 + 和声谱 + Suno-prompt 综合评估）'}\n\n"
        "作为混音师，请同时产出 **2 份** FILE 块：\n\n"
        "1. `混音评估.md` — 频段平衡 / 立体声成像 / 动态控制 / 空间感塑造 / 反馈翻译\n"
        "   - **给方向不给数字**（角色基因 style 约束）：例「Vocal 频段中频温暖，避免 2-4kHz 刺耳」\n"
        "   - 评估 Suno take 落地时的混音风险点 + 提出可执行调整方向\n"
        "   - 不越界母带层决策（响度规范 / 平台标准）\n\n"
        "2. `混音-Suno-retry补丁.md` — 若 Suno take 1 偏离混音 vision，给出 Suno 第 2/3 take 的**Style 段微调建议**：\n"
        "   - 例：`+ vocal mid-forward, breath audible`（强化 vocal 距离）\n"
        "   - 例：`+ low-mid bass round`（修正频段平衡）\n"
        "   - 若 Suno take 1 已达标，可声明「无需 retry patch」\n\n"
        "**重要**：两份产物各为独立 FILE 块，缺一不可。"
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
