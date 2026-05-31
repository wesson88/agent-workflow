"""
music_arranger/main.py — 编曲执行入口（音乐域 L3）

输入（vault，来源：角色 frontmatter `inputs` 字段）：
  - 10-项目/music/{project}/指令/给编曲.md
  - 10-项目/music/{project}/曲作.md
  - 10-项目/music/{project}/Suno-prompt.md
  - 10-项目/music/{project}/创作 vision.md
  - 10-项目/music/{project}/词作.md

输出（vault，来源：角色 frontmatter `outputs` 字段）：
  - 10-项目/music/{project}/编曲方案.md
  - 10-项目/music/{project}/编曲-Suno补丁.md

CLI：
  python .claude/skills/music_arranger/main.py --task "..." --project myproj
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import (
    parse_args, resolve_project, build_system_prompt, read_input_files,
    write_output_atomic, parse_claude_output_to_files,
    call_claude, append_audit, utc_now, render_required_outputs,
    load_rule_block, load_genre_skill_block,
)
from engine import (
    set_role_status, role_is_blocked,
    resolve_path,
)
from engine.role_loader import load_role

ROLE = "编曲"


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

    # 上游硬约束：指令/给编曲.md + 曲作.md 必须同时存在
    required_upstream = {"给编曲.md", "曲作.md"}
    missing = [
        p.name for p in input_paths
        if p.name in required_upstream and not p.exists()
    ]
    if missing:
        print(
            f"[{ROLE}] 上游缺失：{missing}。请先跑制作人扇出 + 作曲。",
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

    skill_block, skill_hint = load_genre_skill_block(ROLE, task, context)
    print(f"[{ROLE}] skill_trigger：{skill_hint}")
    if skill_block:
        context = context + "\n\n" + skill_block

    user_prompt = (
        f"项目名：`{project}`（写文件时把路径里的 `{{project}}` 占位符替换为本值）\n\n"
        f"{context}\n\n---\n"
        f"本轮编曲诉求：{task or '（未提供，请基于上游曲作 + Suno-prompt + vision 综合推导）'}\n\n"
        "作为编曲，请同时产出 **2 份** FILE 块：\n\n"
        "1. `编曲方案.md` — 乐器配置 / 织体设计 / 段落情绪推进 / 流派 fusion 落地 / 编配密度\n"
        "   - **章节结构严格按 [[产物schema#9. 编曲方案.md（编曲产出）]]「必填章节」模板 verbatim 输出**：\n"
        "     §1 编曲整体定位 / §2 乐器配置 / §3 织体设计 / §4 段落对比设计 / §5 流派 idiom 配器对应 / §6 配器红线 / §7 与上游对应自检 / §8 边界自检\n"
        "   - **§5 流派 idiom 配器对应必须以 `[[F-{流派名}]]` wikilink 开头**（如 `[[F-民谣]]` / `[[F-雷鬼]]`），不可用裸汉字流派名\n"
        "   - 给下游和声编写 / 混音师消费的乐器层文档\n"
        "   - 严格对齐曲作的段落结构（同 section 数 / 同 BPM / 同调性）\n"
        "   - 不越界写作曲（旋律 / 和弦走向）/ 混音（频段 / 立体声）层决策\n\n"
        "2. `编曲-Suno补丁.md` — 针对 Suno-prompt.md 已有 Style/Lyrics 段的**增补/修正建议**：\n"
        "   - 章节结构按 [[产物schema#10. 编曲-Suno补丁.md（编曲产出，附产物）]] 4 节模板\n"
        "   - 若 Suno-prompt 已有乐器 anchor 不需补，本补丁可仅声明「无需 patch」\n"
        "   - 若发现段间 arrangement 演进描述缺失，给具体 inline tag 改写建议\n"
        "   - 该补丁是 patch 文档，不是完整 Suno-prompt 重写\n\n"
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
