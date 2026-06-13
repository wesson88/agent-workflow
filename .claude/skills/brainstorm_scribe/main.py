"""
brainstorm_scribe/main.py — 创意记录员执行入口（T2.2 完整 readiness schema）

输入（vault，来源：角色 frontmatter `inputs` 字段）：
  - 10-项目/{project}/inputs/idea.md（必须；脑暴起点）
  - 10-项目/{project}/脑暴/创意发散-R1.md（必须；A 方本轮产出）
  - 10-项目/{project}/脑暴/创意质询-R1.md（必须；B 方本轮产出）
  - 10-项目/{project}/产品创意原型.md（可选；轮 ≥ 2）
  - 10-项目/{project}/脑暴/rolling_brief.md（可选；轮 ≥ 2）

输出（vault，来源：角色 frontmatter `outputs` 字段，单 LLM call 3 FILE 块）：
  - 10-项目/{project}/产品创意原型.md（覆盖式）
  - 10-项目/{project}/brainstorm_readiness.json（覆盖式；完整 schema v0.1.0）
  - 10-项目/{project}/脑暴/rolling_brief.md（覆盖式；T2.1 占位 8 节模板）

CLI：
  python .claude/skills/brainstorm_scribe/main.py --task "..." --project myproj

T2.2 落地 brainstorm-readiness.schema v0.1.0：硬门槛 10 项 + 评分 10 项 + decision 4 值 +
产物落盘后 engine/brainstorm_lint 静态校验，结果落到 audit.jsonl。
T2.3 接 rolling_brief 保真机制 + 多轮循环。T2.4 接 ask_user → human_gate。
"""

from __future__ import annotations

import json
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
from engine.brainstorm_lint import validate_readiness_json
from engine.role_loader import load_role

ROLE = "创意记录员"


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

    # 上游硬约束：A 方发散 + B 方质询必须都存在
    diverge_path = next(
        (p for p in input_paths if "创意发散" in p.name),
        None,
    )
    challenge_path = next(
        (p for p in input_paths if "创意质询" in p.name),
        None,
    )
    missing = []
    if diverge_path is None or not diverge_path.exists():
        missing.append("脑暴/创意发散-R1.md")
    if challenge_path is None or not challenge_path.exists():
        missing.append("脑暴/创意质询-R1.md")
    if missing:
        print(
            f"[{ROLE}] 上游缺失：{missing}。请先跑完 brainstorm_diverger + brainstorm_challenger。",
            file=sys.stderr,
        )
        set_role_status(
            ROLE, status="failed",
            increment_consecutive_failures=True, increment_error=True,
            enforce_transition=False,
        )
        append_audit({
            "timestamp": utc_now(), "role": ROLE, "project": project,
            "task": task, "result": "failed", "error": "missing_upstream",
            "missing": missing,
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
        f"本轮收敛诉求：{task or '（未提供，请基于 idea + A 发散 + B 质询综合取舍）'}\n\n"
        "作为创意记录员（收敛与原型整理者），请单 LLM call 产 3 份产物：\n\n"
        "**1. `产品创意原型.md`** — 按 [[产品创意原型schema]] §必填字段模板 verbatim：\n"
        "  §1 一句话概念（≤ 50 字）/ §2 目标用户 / §3 核心问题 / §4 产品机会点 / "
        "§5 MVP 范围（3-5 条）/ §6 关键功能草案（≥ 3 条）/ §7 不做什么（3-5 条带理由）/ "
        "§8 差异化亮点（1-3 条，体验/承诺非功能堆砌）/ §9 最大风险 / §10 待用户确认项\n"
        "  - 取舍证据化：每条保留 / 否决 / 待确认必须能引用 A 方或 B 方的具体段落\n"
        "  - 不偏向 A 或 B；不夺取用户最终决策权\n\n"
        "**2. `brainstorm_readiness.json`** — 严格按 [[brainstorm-readiness.schema]] v0.1.0 字段集："
        " 顶层 8 字段 + hard_gate × 10 + scores × 10 + decision 4 值。\n"
        "```json\n"
        "{\n"
        '  "ready_for_prd": false,\n'
        '  "prd_readiness": 74,\n'
        '  "hard_gate": {\n'
        '    "product_concept": true, "target_user": true, "core_problem": true,\n'
        '    "mvp_scope": false, "core_features": true, "out_of_scope": false,\n'
        '    "usage_scenarios": true, "success_metrics": false,\n'
        '    "major_risks": true, "open_questions": true\n'
        "  },\n"
        '  "scores": {\n'
        '    "user_clarity": 4, "problem_clarity": 4, "scenario_clarity": 3,\n'
        '    "mvp_boundary": 2, "feature_completeness": 3, "differentiation": 3,\n'
        '    "feasibility": 3, "risk_identification": 4,\n'
        '    "success_metrics": 1, "open_question_quality": 4\n'
        "  },\n"
        '  "blocking_gaps": ["...具体描述..."],\n'
        '  "next_round_focus": ["...下一轮焦点关键词..."],\n'
        '  "questions_for_user": ["...需用户拍板的问题..."],\n'
        '  "decision": "continue_discussion"\n'
        "}\n"
        "```\n"
        "  - **硬门槛 (hard_gate) 10 项 bool**：对应原型 §1-§10，任一 false → `ready_for_prd` 必须 false。\n"
        "  - **评分 (scores) 10 项 0-5**：换算公式 `prd_readiness = round(sum(scores)/50*100)`，"
        "误差 ≤ 2（整数 round）。\n"
        "  - **decision 4 值**：`ready_for_prd / continue_discussion / ask_user / stop_low_value`。\n"
        "    - `ready_for_prd`：全 hard_gate=true AND prd_readiness ≥ 85（互锁 ready_for_prd=true）\n"
        "    - `continue_discussion`：缺章 / 评分不足 / A 与 B 未收敛（next_round_focus 必非空）\n"
        "    - `ask_user`：有需用户拍板的取舍（questions_for_user 必非空）\n"
        "    - `stop_low_value`：多轮无收敛或核心问题不成立\n"
        "  - 保守原则：拿不准时优先 `continue_discussion`，不轻易给 `ready_for_prd` / `stop_low_value`。\n\n"
        "**3. `脑暴/rolling_brief.md`** — T2.1 占位 8 节模板（T2.3 接保真机制）：\n"
        "```markdown\n"
        f"# Rolling Brief — R{round_num}\n\n"
        "## 1. 用户已确认事实\n## 2. 当前产品判断\n## 3. 已保留方向\n## 4. 已否决方向\n"
        "## 5. 关键争议\n## 6. 已回答问题\n## 7. 未回答问题\n## 8. 下一轮焦点\n"
        "```\n"
        "  - 每节 50-200 字\n"
        "  - 本轮否决的方向必须进 §4 已否决方向（防下一轮反复提同一方向）\n\n"
        "**3 份产物在同一次 LLM 输出里产出 3 个独立 FILE 块。**"
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

    # Audit 检查 3 份产物是否全产出
    expected = {"产品创意原型.md", "brainstorm_readiness.json", "rolling_brief.md"}
    written_basenames = {Path(w).name for w in written}
    missing_outputs = expected - written_basenames
    if missing_outputs:
        print(
            f"[{ROLE}] ⚠️ 产物不全：缺 {missing_outputs}（已写 {written_basenames}）",
            file=sys.stderr,
        )

    # readiness JSON 静态 lint（[[brainstorm-readiness.schema]] §5）
    readiness_errors: list[str] = []
    readiness_rel = next(
        (w for w in written if Path(w).name == "brainstorm_readiness.json"),
        None,
    )
    readiness_decision: str | None = None
    if readiness_rel:
        readiness_path = resolve_path(readiness_rel, project)
        try:
            data = json.loads(readiness_path.read_text(encoding="utf-8"))
            readiness_errors = validate_readiness_json(data)
            if isinstance(data, dict):
                readiness_decision = data.get("decision")
        except json.JSONDecodeError as e:
            readiness_errors = [f"[parse] JSON 解析失败：{e}"]
        if readiness_errors:
            print(
                f"[{ROLE}] ⚠️ readiness lint 失败 {len(readiness_errors)} 条：",
                file=sys.stderr,
            )
            for err in readiness_errors:
                print(f"  - {err}", file=sys.stderr)
        else:
            print(f"[{ROLE}] ✅ readiness lint 通过（decision={readiness_decision}）")

    set_role_status(ROLE, status="success", reset_counters=True)
    set_role_status(ROLE, status="idle")
    append_audit({
        "timestamp": utc_now(), "role": ROLE, "project": project,
        "task": task, "result": "success", "outputs": written,
        "round": round_num,
        "missing_outputs": list(missing_outputs) if missing_outputs else [],
        "readiness_lint_errors": readiness_errors,
        "readiness_decision": readiness_decision,
    })
    print(f"[{ROLE}] 完成，输出：{written}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
