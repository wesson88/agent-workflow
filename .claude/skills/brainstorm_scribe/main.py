"""
brainstorm_scribe/main.py — 创意记录员执行入口（T2.3 完整 rolling_brief 保真 + R3/R6 审计）

输入：
  - 10-项目/{project}/inputs/idea.md（必须；脑暴起点）
  - 10-项目/{project}/脑暴/创意发散-R{round}.md（必须；A 方本轮产出）
  - 10-项目/{project}/脑暴/创意质询-R{round}.md（必须；B 方本轮产出）
  - 10-项目/{project}/产品创意原型.md（可选；轮 ≥ 2）
  - 10-项目/{project}/脑暴/rolling_brief.md（可选；轮 ≥ 2）

输出（单 LLM call 3 FILE 块；R3/R6 额外 1 个 audit 块）：
  - 10-项目/{project}/产品创意原型.md（覆盖式）
  - 10-项目/{project}/brainstorm_readiness.json（覆盖式；schema v0.1.0）
  - 10-项目/{project}/脑暴/rolling_brief.md（覆盖式；schema v0.1.0 + source/confidence）
  - 10-项目/{project}/脑暴/rolling_brief_audit-R{round}.md（仅 round ∈ {3, 6}）

CLI：
  python .claude/skills/brainstorm_scribe/main.py --task "..." --project myproj --round 2

T2.3 落地 [[rolling-brief.schema]] v0.1.0 + R3/R6 一致性审计。
T2.4 接 ask_user → human_gate（§13.7 高影响决策卡点）。
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

ROLE = "创意记录员"

AUDIT_ROUNDS = (3, 6)


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


def main() -> int:
    args = parse_args()
    task = (args.task or "").strip()
    project = resolve_project(args)
    round_num = max(1, int(getattr(args, "round_num", 1) or 1))
    is_audit_round = round_num in AUDIT_ROUNDS

    if role_is_blocked(ROLE):
        print(f"[{ROLE}] status=blocked，跳过。", file=sys.stderr)
        return 1

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
    })
    print(f"[{ROLE}] R{round_num} 完成，输出：{written}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
