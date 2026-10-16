"""
approval.py — 持久化的"批准/驳回"人工闸（2026-10-17 视频域 W2 引入）

工作流每次重跑都会重新进入节点，所以审批结论必须跨运行持久：
- 同名 gate 只要有一条 resolved+approve → 永久视为已批准（重跑不再问）
- 最新一条 pending → 等待（调用方 halt）
- 最新一条 resolved+reject 且未消费 → 返回 rejected 并标记消费（下次重跑会重新发起审批，
  不会因为一条旧的驳回永远卡死——module_dev_loop 的死锁教训）
- 都没有 → 新落一条 pending

gate 名是调用方给的稳定 key（如 `confirm_assets`、`generation:keyframe:G01:a2`）。
"""

from __future__ import annotations

from typing import Literal

from .human_gate import emit_gate, gate_is_consumed, list_gates, mark_gate_consumed

Decision = Literal["approved", "rejected", "pending"]


def _action(g) -> str:
    return str((g.resolution or {}).get("action", ""))


def ask_approval(
    project: str,
    gate_name: str,
    reason: str,
    *,
    node: str | None = None,
    refs: list[str] | None = None,
    suggested_actions: list[str] | None = None,
) -> Decision:
    gates = sorted(
        (g for g in list_gates(project) if g.gate == gate_name),
        key=lambda g: g.created_at, reverse=True,
    )
    if any(g.status == "resolved" and _action(g) == "approve" for g in gates):
        return "approved"
    if gates and gates[0].status == "pending":
        return "pending"
    if gates and gates[0].status == "resolved" and _action(gates[0]) == "reject" \
            and not gate_is_consumed(gates[0]):
        mark_gate_consumed(project, gates[0].id)
        return "rejected"
    g = emit_gate(
        project=project, type="human_gate", mode="passive", reason=reason,
        node=node, gate=gate_name, context_refs=refs,
        options=[{"id": "approve", "label": "通过"}, {"id": "reject", "label": "驳回"}],
        recommended_option="approve",
        suggested_actions=suggested_actions or [
            f"python .claude/engine/cli_human_gate.py --project {project} resolve --id <gate-id> --action approve",
        ],
    )
    print(f"\n⏸  已落人工闸 {g.id}（{gate_name}）：{reason}")
    return "pending"


def pending_gate_id(project: str, gate_name: str) -> str | None:
    for g in list_gates(project, status="pending"):
        if g.gate == gate_name:
            return g.id
    return None
