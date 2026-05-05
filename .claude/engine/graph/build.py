"""
graph/build.py — 从 vault workflow template 构建 LangGraph

Phase 4a：仅支持 type=linear 步骤，构建线性 chain。
Phase 4b：扩展支持 discussion-loop / parallel 节点类型。
"""

from __future__ import annotations

from langgraph.graph import StateGraph, START, END

from ..role_loader import load_role
from ..workflow import WorkflowTemplate, role_to_skill_dir
from .nodes import make_role_node
from .state import ProjectState


def _normalize(name_or_alias: str) -> str:
    return load_role(name_or_alias).name


def _slice_chain(chain: list[str], start_from: str | None, end_at: str | None) -> list[str]:
    if start_from:
        target = _normalize(start_from)
        try:
            idx = chain.index(target)
        except ValueError:
            raise ValueError(f"--start-from='{start_from}' 不在工作流链路中：{chain}")
        chain = chain[idx:]
    if end_at:
        target = _normalize(end_at)
        try:
            idx = chain.index(target)
        except ValueError:
            raise ValueError(f"--end-at='{end_at}' 不在工作流链路中：{chain}")
        chain = chain[: idx + 1]
    return chain


def build_graph(
    template: WorkflowTemplate,
    *,
    start_from: str | None = None,
    end_at: str | None = None,
):
    """从工作流模板构建可 invoke 的 StateGraph。

    返回 compile 后的 graph，调用 `.invoke(initial_state)` 执行。
    Phase 4a 仅支持线性；模板包含 parallel / discussion-loop 时由 linear_role_names 抛错。
    """
    chain = [_normalize(r) for r in template.linear_role_names()]
    chain = _slice_chain(chain, start_from, end_at)

    if not chain:
        raise ValueError("链路裁剪后为空")

    g = StateGraph(ProjectState)

    # 加 node：用 skill_dir 做唯一名（角色名可能含中文不利于 graph 内部 key）
    node_keys: list[str] = []
    for role in chain:
        skill_dir = role_to_skill_dir(role)
        node_key = f"step_{skill_dir}"
        # 防止重复（同一角色被裁剪两次出现的极端情况）
        if node_key in node_keys:
            continue
        g.add_node(node_key, make_role_node(role, template.halt_on_failure))
        node_keys.append(node_key)

    # 线性边：START → 第1 → 第2 → ... → END
    g.add_edge(START, node_keys[0])
    for i in range(len(node_keys) - 1):
        g.add_edge(node_keys[i], node_keys[i + 1])
    g.add_edge(node_keys[-1], END)

    return g.compile()
