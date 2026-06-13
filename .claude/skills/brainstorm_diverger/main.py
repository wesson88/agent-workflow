"""
brainstorm_diverger/main.py — 创意发散者执行入口（T2.1 MVP 单轮 R1）

输入（vault，来源：角色 frontmatter `inputs` 字段）：
  - 10-项目/{project}/inputs/idea.md（必须；脑暴起点）
  - 10-项目/{project}/产品创意原型.md（可选；轮 ≥ 2）
  - 10-项目/{project}/脑暴/rolling_brief.md（可选；轮 ≥ 2）

输出（vault，来源：角色 frontmatter `outputs` 字段）：
  - 10-项目/{project}/脑暴/创意发散-R1.md

CLI：
  python .claude/skills/brainstorm_diverger/main.py --task "..." --project myproj

T2.1 MVP 硬编码 round=1。多轮支持 + round 参数传递留 T2.3。
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

ROLE = "创意发散者"


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

    # 上游硬约束：idea.md 必须存在（脑暴起点）
    idea_path = next(
        (p for p in input_paths if p.name == "idea.md"),
        None,
    )
    if idea_path is None or not idea_path.exists():
        print(
            f"[{ROLE}] 上游缺失：未找到 `inputs/idea.md`（脑暴起点）。"
            f"请在 `10-项目/{project}/inputs/idea.md` 放用户的一句话点子。",
            file=sys.stderr,
        )
        set_role_status(
            ROLE, status="failed",
            increment_consecutive_failures=True, increment_error=True,
            enforce_transition=False,
        )
        append_audit({
            "timestamp": utc_now(), "role": ROLE, "project": project,
            "task": task, "result": "failed", "error": "missing_idea",
        })
        return 2

    existing_inputs = [p for p in input_paths if p.exists()]
    round_num = 1  # T2.1 MVP 硬编码 R1
    print(
        f"[{ROLE}] R{round_num} 上游 {len(existing_inputs)}/{len(input_paths)} 就位："
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
        f"本轮发散诉求：{task or '（未提供，请基于 idea.md 综合推导发散方向）'}\n\n"
        f"作为创意发散者（机会放大者），请产 `脑暴/创意发散-R{round_num}.md`：\n"
        "- **章节结构严格按角色基因 §3 输出结构模板 verbatim 输出**：\n"
        "  §1 核心机会点 / §2 目标用户假设 / §3 产品方向候选 / §4 差异化亮点 / "
        "§5 大胆但有风险的方向 / §6 本轮最值得探索的问题\n"
        "- 必须 ≥ 3 个产品方向候选；每个用「主要用户 + 核心场景 + 核心体验」3 元素一句话定义\n"
        "- 必须 ≥ 1 个大胆但有风险的方向作为对照（给质询者攻击靶子）\n"
        "- 差异化亮点是体验/承诺，不是功能堆砌\n"
        "- §6 ≥ 2 个开放问题，明确「我不知道 / 假设是 X / 需要验证 Y」\n"
        "- 不评估技术可行性 / 实现复杂度 / 商业模式（留给质询者）\n\n"
        "产物只有 1 份（`脑暴/创意发散-R1.md`），单 FILE 块即可。"
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
        "round": round_num,
    })
    print(f"[{ROLE}] 完成，输出：{written}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
