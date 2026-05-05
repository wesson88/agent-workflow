"""
graph/state.py — StateGraph 共享状态

LangGraph 通过 `Annotated[list, operator.add]` 让多 node 各自 append 到同一字段
最后自动 merge。这个机制对 Phase 4b 的并行节点也适用。

字段最少化原则：state 只追踪元数据（哪些角色跑了 / 失败了 / 当前是否 halt），
角色实际产出仍在 vault 文件里（PRD.md、系统设计.md 等），不进 state。
讨论循环的对话历史是例外（4b 才用，提前预留字段）。
"""

from __future__ import annotations

from operator import add
from typing import Annotated, TypedDict


class ProjectState(TypedDict, total=False):
    # 任务上下文（不变）
    project: str
    task: str
    workflow_name: str

    # 角色执行轨迹（多 node 累加）
    succeeded: Annotated[list[str], add]
    failed: Annotated[list[str], add]
    skipped: Annotated[list[str], add]

    # halt 标志（一处失败置 True，下游 node 自检后跳过）
    halted: bool

    # Phase 4b 讨论循环用（4a 不写也不读）
    discussion_log: Annotated[list[dict], add]
    discussion_iterations: int
    consensus_reached: bool
