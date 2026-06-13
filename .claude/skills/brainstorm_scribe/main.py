"""
brainstorm_scribe/main.py — 创意记录员执行入口（T2.4 ask_user → human_gate 接入）

输入：
  - 10-项目/{project}/inputs/idea.md（必须；脑暴起点）
  - 10-项目/{project}/脑暴/创意发散-R{round}.md（必须；A 方本轮产出）
  - 10-项目/{project}/脑暴/创意质询-R{round}.md（必须；B 方本轮产出）
  - 10-项目/{project}/产品创意原型.md（可选；轮 ≥ 2）
  - 10-项目/{project}/脑暴/rolling_brief.md（可选；轮 ≥ 2）
  - 10-项目/{project}/.workflow/human_gates/*.json（已 resolved 的 brainstorm_* gate；轮 ≥ 2 自动注入）

输出（单 LLM call 3 FILE 块；R3/R6 额外 1 个 audit 块）：
  - 10-项目/{project}/产品创意原型.md（覆盖式）
  - 10-项目/{project}/brainstorm_readiness.json（覆盖式；schema v0.1.0）
  - 10-项目/{project}/脑暴/rolling_brief.md（覆盖式；schema v0.1.0 + source/confidence）
  - 10-项目/{project}/脑暴/rolling_brief_audit-R{round}.md（仅 round ∈ {3, 6}）

副产物（readiness.decision == "ask_user" 时）：
  - 10-项目/{project}/.workflow/human_gates/gate-{date}-{nnn}.json（passive human_gate；T1.2 schema）

CLI：
  python .claude/skills/brainstorm_scribe/main.py --task "..." --project myproj --round 2

T2.4 落地：
- 跑前 has_pending() → 阻塞（避免覆盖未解决的卡点）
- readiness.decision=ask_user 时 emit_gate(gate=brainstorm_readiness, mode=passive)
- R≥2 自动注入历史 resolved brainstorm gate 的 user_response 到 context，提示 LLM
  在 rolling_brief §1 用 source=user_answer-R{N}
"""

from __future__ import annotations

import json
import re
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
from engine.rolling_brief_lint import validate_rolling_brief
from engine.role_loader import load_role
from engine.human_gate import (
    HumanGate, emit_gate, has_pending, list_gates,
)

ROLE = "创意记录员"

AUDIT_ROUNDS = (3, 6)

# T2.4：本 skill 所写 / 消费的 gate 名（与 [[元角色与人工介入机制]] §8.1 brainstorm_readiness 对齐）
BRAINSTORM_GATE = "brainstorm_readiness"


def _apply_round(path: str, round_num: int) -> str:
    """把 frontmatter 里 hardcoded '-R1' 替换为 '-R{round_num}'。"""
    return re.sub(r"-R1(?=\.md|$|\W)", f"-R{round_num}", path)


def _collect_audit_history(project: str, round_num: int) -> str:
    """R3/R6 审计模式：读 R1~R{round} 全部 A/B 原文，拼成 context。"""
    parts: list[str] = []
    for r in range(1, round_num + 1):
        for kind in ("创意发散", "创意质询"):
            p = resolve_path(f"10-项目/{{project}}/脑暴/{kind}-R{r}.md", project)
            if p.exists():
                parts.append(f"\n--- {kind}-R{r}.md ---\n{p.read_text(encoding='utf-8')}")
    return "\n".join(parts)


def _collect_resolved_brainstorm_gates(project: str) -> list[HumanGate]:
    """扫已 resolved 的 brainstorm_* gate，按 created_at 排序。

    供 R≥2 时把历史 user_response 拼进 context。
    """
    resolved = list_gates(project, status="resolved")
    return [g for g in resolved if (g.gate or "").startswith("brainstorm_")]


def _format_resolved_gates_for_context(gates: list[HumanGate]) -> str:
    """把 resolved gate 渲染成 markdown 注入 LLM context。"""
    if not gates:
        return ""
    lines = ["## 历史用户答复（已 resolved 的 brainstorm gate）", ""]
    for i, g in enumerate(gates, start=1):
        lines.append(f"### 答复 #{i}（gate {g.id}，resolved at {g.resolved_at or '未知'}）")
        lines.append(f"- 节点（node）: {g.node or '未标注'}")
        lines.append(f"- 提问 reason: {g.reason}")
        if g.user_response:
            lines.append(f"- 用户回答: {g.user_response}")
        if g.resolution:
            lines.append(f"- resolution: {g.resolution}")
        lines.append("")
    return "\n".join(lines)


def _emit_ask_user_gate(
    *,
    project: str,
    round_num: int,
    readiness_data: dict,
    is_audit_round: bool,
    output_rels: list[str],
) -> HumanGate:
    """readiness.decision == ask_user 时 emit 一条 passive human_gate。

    R3/R6 审计模式下的 §5 高影响决策也走本路径（LLM 已把决策塞进 questions_for_user）。
    """
    questions = readiness_data.get("questions_for_user") or []
    blocking_gaps = readiness_data.get("blocking_gaps") or []
    decision_kind = "R3/R6 审计高影响决策" if is_audit_round else "readiness 追问"
    reason_parts = [f"[brainstorm R{round_num}] {decision_kind}，需用户拍板继续："]
    for q in questions:
        reason_parts.append(f"- {q}")
    if blocking_gaps:
        reason_parts.append("当前 blocking_gaps：")
        for g in blocking_gaps:
            reason_parts.append(f"  · {g}")
    suggested = [
        f"resolve 后用 --round {round_num + 1} 跑下一轮 brainstorm",
        "abort（终止脑暴，回到 idea 阶段）",
    ]
    return emit_gate(
        project=project,
        type="human_gate",
        mode="passive",
        gate=BRAINSTORM_GATE,
        node=f"brainstorm_R{round_num}",
        reason="\n".join(reason_parts),
        context_refs=output_rels,
        suggested_actions=suggested,
    )


def main() -> int:
    args = parse_args()
    task = (args.task or "").strip()
    project = resolve_project(args)
    round_num = max(1, int(getattr(args, "round_num", 1) or 1))
    is_audit_round = round_num in AUDIT_ROUNDS

    if role_is_blocked(ROLE):
        print(f"[{ROLE}] status=blocked，跳过。", file=sys.stderr)
        return 1

    # T2.4 阻塞：若项目有未解决的 human_gate（无论 brainstorm 还是其他），先暂停
    if has_pending(project):
        print(
            f"[{ROLE}] 项目 '{project}' 有未解决的 human_gate，跳过本轮。\n"
            f"  列出：python .claude/engine/cli_human_gate.py --project {project} list\n"
            f"  解决：python .claude/engine/cli_human_gate.py --project {project} resolve "
            f"--id <gate-id> --action approve --response \"...\"",
            file=sys.stderr,
        )
        append_audit({
            "timestamp": utc_now(), "role": ROLE, "project": project,
            "task": task, "result": "skipped", "reason": "pending_human_gate",
            "round": round_num,
        })
        return 3

    set_role_status(ROLE, status="busy", enforce_transition=False)

    role_def = load_role(ROLE)
    input_paths = [resolve_path(_apply_round(p, round_num), project) for p in role_def.inputs]
    output_rels = [
        _apply_round(p, round_num).replace("{project}", project)
        for p in role_def.outputs
    ]

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
        missing.append(f"脑暴/创意发散-R{round_num}.md")
    if challenge_path is None or not challenge_path.exists():
        missing.append(f"脑暴/创意质询-R{round_num}.md")
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
    print(
        f"[{ROLE}] R{round_num} 上游 {len(existing_inputs)}/{len(input_paths)} 就位："
        f"{[p.name for p in existing_inputs]}"
        f"{'（R3/R6 审计模式）' if is_audit_round else ''}",
        flush=True,
    )

    system_prompt = build_system_prompt(ROLE, project=project)
    context = read_input_files(input_paths)

    rule_block, source_hint = load_rule_block(role_def.rule_refs)
    print(f"[{ROLE}] rule_refs 注入：{source_hint}")
    if rule_block:
        context = context + "\n\n" + rule_block

    # T2.4：R≥2 注入历史 resolved brainstorm gate 的 user_response 到 context
    resolved_gates = _collect_resolved_brainstorm_gates(project)
    if resolved_gates:
        gate_block = _format_resolved_gates_for_context(resolved_gates)
        context = context + "\n\n" + gate_block
        print(
            f"[{ROLE}] 注入历史 user_answer：{len(resolved_gates)} 条 resolved gate",
            flush=True,
        )

    # R3/R6 审计：追加全历史 A/B 原文（仅审计轮）
    if is_audit_round:
        history = _collect_audit_history(project, round_num)
        if history:
            context = context + (
                "\n\n--- 全历史原文（R3/R6 审计模式，对照 rolling_brief 保真）---\n"
                + history
            )

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
        "**3. `脑暴/rolling_brief.md`** — 严格按 [[rolling-brief.schema]] v0.1.0：\n"
        "  - 9 节 H2 顺序固定：\n"
        "    §1 用户已确认事实 / §2 LLM 推断 / §3 已做决策 / §4 已保留方向 / "
        "§5 已否决方向 / §6 关键争议 / §7 已回答问题 / §8 未回答问题 / §9 下一轮焦点\n"
        "  - **每个 list item 必须带 `source:` + `confidence:` 子字段**（2 空格缩进）\n"
        "  - source 前缀必须 ∈ {`idea.md`, `user_answer-R{N}`, `创意发散-R{N}.md[#章节]`, "
        "`创意质询-R{N}.md[#章节]`, `创意记录员-R{N}[#章节]`, `brainstorm_readiness-R{N}[#字段]`, "
        "`产品创意原型-R{N}[#章节]`}；多 source 用 `, ` 或 ` vs ` 分隔\n"
        "  - confidence ∈ {`high`, `medium`, `low`}\n"
        "  - **强制规则**：\n"
        "    - §1 用户已确认事实 / §7 已回答问题 下每条 confidence 必须 `high`\n"
        "    - §5 已否决方向下每条**必须有 `reason:` 子字段**（说明为何砍）\n"
        "    - 用户事实 vs LLM 推断**物理隔离**：用户没明确说过 → 进 §2 而不是 §1\n"
        "    - 本轮否决的方向必须进 §5（防下一轮 A 重新提）\n"
        "  - 示例条目：\n"
        "    ```markdown\n"
        "    - 通勤路线异常提醒\n"
        f"      source: 创意质询-R{round_num}.md#值得保留的方向\n"
        "      confidence: medium\n"
        "    ```\n"
        f"  - 文件 H1 = `# Rolling Brief — R{round_num}`\n\n"
        f"**{'4 份产物（含 R3/R6 审计）' if is_audit_round else '3 份产物'}"
        f"在同一次 LLM 输出里产出独立 FILE 块。**"
    )

    # T2.4：有历史 resolved gate 时，强约束 LLM 把 user_response 落到 §1 + source=user_answer
    if resolved_gates:
        user_prompt += (
            f"\n\n**T2.4 注入**：上文 context 含 {len(resolved_gates)} 条 "
            f"已 resolved 的 brainstorm gate 用户答复，必须按以下规则消费：\n"
            "  - 每条用户答复 **必须** 写入 rolling_brief §1 用户已确认事实\n"
            "  - source 锚点：`user_answer-R{N}`（N = 答复对应的脑暴轮次，从 gate.node 字段 "
            "`brainstorm_R{N}` 推断）\n"
            "  - confidence 强制 `high`\n"
            "  - 已被用户**否决**的方向同步进 §5 已否决方向，reason 含「用户 R{N} 答复明确否决」\n"
        )

    if is_audit_round:
        audit_rel = f"10-项目/{project}/脑暴/rolling_brief_audit-R{round_num}.md"
        output_rels.append(audit_rel)
        user_prompt += (
            f"\n\n**4. `脑暴/rolling_brief_audit-R{round_num}.md`** — R{round_num} 一致性审计"
            "（[[rolling-brief.schema]] §8）：\n"
            f"  - 审计范围：R1 ~ R{round_num} 全部 A/B 原文（已在上文 context）vs 当前 "
            "rolling_brief.md（本轮**即将覆盖**前的版本）\n"
            "  - 6 节固定结构：\n"
            "    §1 漏掉的用户事实 / §2 LLM 推断被误写成用户事实 / §3 遗漏的已否决方向 / "
            "§4 遗漏的高风险争议 / §5 需要向用户确认的高影响决策 / §6 修复动作\n"
            "  - **审计联动 readiness 决策**：\n"
            "    - 若 §1 / §2 / §3 任一非空 → readiness.decision 不得 `ready_for_prd`，"
            "降级为 `continue_discussion`\n"
            "    - 若 §5 非空 → readiness.decision = `ask_user`，questions_for_user 包含 §5 条目\n"
            "  - §6 修复动作必须列出本轮 rolling_brief 已补充的修复条目\n"
        )

    user_prompt += render_required_outputs(output_rels)

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

    # Audit 检查产物是否全产出（R3/R6 额外要求 audit md）
    expected = {"产品创意原型.md", "brainstorm_readiness.json", "rolling_brief.md"}
    if is_audit_round:
        expected.add(f"rolling_brief_audit-R{round_num}.md")
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

    # rolling_brief.md 静态 lint（[[rolling-brief.schema]] §7）
    brief_errors: list[str] = []
    brief_rel = next(
        (w for w in written if Path(w).name == "rolling_brief.md"),
        None,
    )
    if brief_rel:
        brief_path = resolve_path(brief_rel, project)
        try:
            brief_text = brief_path.read_text(encoding="utf-8")
            brief_errors = validate_rolling_brief(brief_text)
        except OSError as e:
            brief_errors = [f"[read] 文件读取失败：{e}"]
        if brief_errors:
            print(
                f"[{ROLE}] ⚠️ rolling_brief lint 失败 {len(brief_errors)} 条：",
                file=sys.stderr,
            )
            for err in brief_errors:
                print(f"  - {err}", file=sys.stderr)
        else:
            print(f"[{ROLE}] ✅ rolling_brief lint 通过")

    # T2.4：readiness.decision == ask_user 时 emit passive human_gate
    emitted_gate_id: str | None = None
    if (
        readiness_rel
        and readiness_decision == "ask_user"
        and not readiness_errors
    ):
        try:
            with resolve_path(readiness_rel, project).open(encoding="utf-8") as f:
                readiness_data = json.load(f)
            gate = _emit_ask_user_gate(
                project=project,
                round_num=round_num,
                readiness_data=readiness_data,
                is_audit_round=is_audit_round,
                output_rels=written,
            )
            emitted_gate_id = gate.id
            print(
                f"[{ROLE}] ⏸️  decision=ask_user，已 emit human_gate {gate.id}\n"
                f"  resolve: python .claude/engine/cli_human_gate.py --project {project}"
                f" resolve --id {gate.id} --action approve --response \"...\"\n"
                f"  resolve 后用 --round {round_num + 1} 跑下一轮 brainstorm",
                file=sys.stderr,
            )
        except (json.JSONDecodeError, OSError) as e:
            print(f"[{ROLE}] ⚠️ emit_gate 失败：{e}", file=sys.stderr)

    set_role_status(ROLE, status="success", reset_counters=True)
    set_role_status(ROLE, status="idle")
    append_audit({
        "timestamp": utc_now(), "role": ROLE, "project": project,
        "task": task, "result": "success", "outputs": written,
        "round": round_num,
        "audit_round": is_audit_round,
        "missing_outputs": list(missing_outputs) if missing_outputs else [],
        "readiness_lint_errors": readiness_errors,
        "readiness_decision": readiness_decision,
        "rolling_brief_lint_errors": brief_errors,
        "human_gate_id": emitted_gate_id,
        "resolved_gates_injected": [g.id for g in resolved_gates],
    })
    print(f"[{ROLE}] R{round_num} 完成，输出：{written}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
