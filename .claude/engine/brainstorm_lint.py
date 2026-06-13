"""
brainstorm_lint.py — brainstorm_readiness.json 静态校验

依据 [[brainstorm-readiness.schema]] §5 lint 规则：
- §5.1 必填字段（顶层 8 项 + hard_gate 10 项 + scores 10 项）
- §5.2 类型约束（bool / int / list[str] / decision 枚举）
- §5.3 一致性约束（硬门槛 vs ready_for_prd / decision 互锁 / 分数换算 ±2）

API：
    validate_readiness_json(data: dict) -> list[str]
        返回错误清单。空列表 = 通过。

被消费方：
- tests/engine/test_brainstorm_contract_lint.py（静态用例）
- .claude/skills/brainstorm_scribe/main.py（产出后 audit）
"""

from __future__ import annotations

from typing import Any


TOP_LEVEL_REQUIRED = (
    "ready_for_prd", "prd_readiness", "hard_gate", "scores",
    "blocking_gaps", "next_round_focus", "questions_for_user", "decision",
)

HARD_GATE_FIELDS = (
    "product_concept", "target_user", "core_problem", "mvp_scope",
    "core_features", "out_of_scope", "usage_scenarios", "success_metrics",
    "major_risks", "open_questions",
)

SCORE_FIELDS = (
    "user_clarity", "problem_clarity", "scenario_clarity", "mvp_boundary",
    "feature_completeness", "differentiation", "feasibility",
    "risk_identification", "success_metrics", "open_question_quality",
)

DECISION_VALUES = (
    "ready_for_prd", "continue_discussion", "ask_user", "stop_low_value",
)

READY_FOR_PRD_THRESHOLD = 85
READINESS_TOLERANCE = 2


def validate_readiness_json(data: Any) -> list[str]:
    """按 [[brainstorm-readiness.schema]] §5 全规则校验。

    返回错误清单（空 = 通过）。规则编号对应 schema 文档 §5.1 / §5.2 / §5.3。
    """
    errs: list[str] = []

    if not isinstance(data, dict):
        return [f"顶层必须是 dict，实际 {type(data).__name__}"]

    for f in TOP_LEVEL_REQUIRED:
        if f not in data:
            errs.append(f"[§5.1] 缺顶层字段：{f}")
    if errs:
        return errs

    hg = data["hard_gate"]
    if not isinstance(hg, dict):
        errs.append(f"[§5.2] hard_gate 必须是 dict，实际 {type(hg).__name__}")
    else:
        for f in HARD_GATE_FIELDS:
            if f not in hg:
                errs.append(f"[§5.1] hard_gate 缺字段：{f}")
            elif not isinstance(hg[f], bool):
                errs.append(f"[§5.2] hard_gate.{f} 必须是 bool，实际 {type(hg[f]).__name__}")

    sc = data["scores"]
    if not isinstance(sc, dict):
        errs.append(f"[§5.2] scores 必须是 dict，实际 {type(sc).__name__}")
    else:
        for f in SCORE_FIELDS:
            if f not in sc:
                errs.append(f"[§5.1] scores 缺字段：{f}")
            elif isinstance(sc[f], bool) or not isinstance(sc[f], int):
                errs.append(f"[§5.2] scores.{f} 必须是 int，实际 {type(sc[f]).__name__}")
            elif not 0 <= sc[f] <= 5:
                errs.append(f"[§5.2] scores.{f}={sc[f]} 越界（必须 ∈ [0, 5]）")

    rfp = data["ready_for_prd"]
    if not isinstance(rfp, bool):
        errs.append(f"[§5.2] ready_for_prd 必须是 bool，实际 {type(rfp).__name__}")

    prd_r = data["prd_readiness"]
    if isinstance(prd_r, bool) or not isinstance(prd_r, int):
        errs.append(f"[§5.2] prd_readiness 必须是 int，实际 {type(prd_r).__name__}")
    elif not 0 <= prd_r <= 100:
        errs.append(f"[§5.2] prd_readiness={prd_r} 越界（必须 ∈ [0, 100]）")

    for f in ("blocking_gaps", "next_round_focus", "questions_for_user"):
        v = data[f]
        if not isinstance(v, list):
            errs.append(f"[§5.2] {f} 必须是 list，实际 {type(v).__name__}")
        elif not all(isinstance(x, str) for x in v):
            errs.append(f"[§5.2] {f} 必须是 list[str]，含非 str 元素")

    decision = data["decision"]
    if decision not in DECISION_VALUES:
        errs.append(f"[§5.2] decision={decision!r} 非法（必须 ∈ {DECISION_VALUES}）")

    # §5.3 一致性：仅在类型/必填全通过时校验，否则误报
    has_type_issue = any(e.startswith("[§5.1]") or e.startswith("[§5.2]") for e in errs)
    if not has_type_issue:
        if rfp and not all(hg.get(f, False) for f in HARD_GATE_FIELDS):
            failed = [f for f in HARD_GATE_FIELDS if not hg.get(f, False)]
            errs.append(f"[§5.3-1] ready_for_prd=true 但 hard_gate 存在 false：{failed}")

        if rfp and prd_r < READY_FOR_PRD_THRESHOLD:
            errs.append(
                f"[§5.3-2] ready_for_prd=true 但 prd_readiness={prd_r} < {READY_FOR_PRD_THRESHOLD}"
            )

        if decision == "ready_for_prd" and not rfp:
            errs.append("[§5.3-3] decision=ready_for_prd 但 ready_for_prd=false（互锁违反）")

        if decision == "ask_user" and not data["questions_for_user"]:
            errs.append("[§5.3-4] decision=ask_user 但 questions_for_user 为空")

        if decision == "continue_discussion" and not data["next_round_focus"]:
            errs.append("[§5.3-5] decision=continue_discussion 但 next_round_focus 为空")

        total = sum(sc.get(f, 0) for f in SCORE_FIELDS)
        expected = round(total / 50 * 100)
        if abs(prd_r - expected) > READINESS_TOLERANCE:
            errs.append(
                f"[§5.3-6] prd_readiness={prd_r} 与 sum(scores)/50*100={expected}"
                f" 误差 {abs(prd_r - expected)} > {READINESS_TOLERANCE}"
            )

    return errs
