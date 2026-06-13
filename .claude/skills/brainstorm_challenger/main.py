"""
brainstorm_challenger/main.py — 创意质询者执行入口（T2.1 MVP 单轮 R1）

输入（vault，来源：角色 frontmatter `inputs` 字段）：
  - 10-项目/{project}/inputs/idea.md（必须；脑暴起点）
  - 10-项目/{project}/脑暴/创意发散-R1.md（必须；A 方本轮产出，主要攻击对象）
  - 10-项目/{project}/产品创意原型.md（可选；轮 ≥ 2）
  - 10-项目/{project}/脑暴/rolling_brief.md（可选；轮 ≥ 2）

输出（vault，来源：角色 frontmatter `outputs` 字段）：
  - 10-项目/{project}/脑暴/创意质询-R1.md

CLI：
  python .claude/skills/brainstorm_challenger/main.py --task "..." --project myproj

T2.1 MVP 硬编码 round=1。多轮支持留 T2.3。
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

ROLE = "创意质询者"


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

    # 上游硬约束：A 方发散产物必须存在
    diverge_path = next(
        (p for p in input_paths if "创意发散" in p.name),
        None,
    )
    if diverge_path is None or not diverge_path.exists():
        print(
            f"[{ROLE}] 上游缺失：未找到 `脑暴/创意发散-R1.md`。"
            f"请先跑 brainstorm_diverger 产出 A 方发散。",
            file=sys.stderr,
        )
        set_role_status(
            ROLE, status="failed",
            increment_consecutive_failures=True, increment_error=True,
            enforce_transition=False,
        )
        append_audit({
            "timestamp": utc_now(), "role": ROLE, "project": project,
            "task": task, "result": "failed", "error": "missing_diverge",
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
        f"本轮质询诉求：{task or '（未提供，请基于 idea + A 方发散综合质询）'}\n\n"
        f"作为创意质询者（约束挑战者），请产 `脑暴/创意质询-R{round_num}.md`：\n"
        "- **章节结构严格按角色基因 §3 输出结构模板 verbatim 输出**：\n"
        "  §1 最大不确定性 / §2 伪需求风险 / §3 竞品 / 替代方案 / "
        "§4 应该砍掉的方向 / §5 值得保留的方向 / §6 MVP 缩小建议\n"
        "- §4 + §5 必须**覆盖** A 方提的全部产品方向（每个方向明确「进 / 砍 / 继续讨论」）\n"
        "- §5 值得保留方向**最多 2 个**\n"
        "- 每个质疑必须指向具体「失败场景 + 量化阈值」（如「日活 < 5 次不形成习惯」比「用户可能不爱用」强）\n"
        "- §3 找具体竞品/工具/服务，不要说「市面有替代品」这种空话\n"
        "- §6 每个保留方向给 MVP 切口：「首版只做 X / 能在 N 周内验证 Y / 验证方式 Z」3 元素\n"
        "- 不做纯否定者：每个质疑必须配缩小方式或修正方向\n"
        "- 不替 A 方想新方向，不评估视觉设计 / UI 细节\n\n"
        "产物只有 1 份（`脑暴/创意质询-R1.md`），单 FILE 块即可。"
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
