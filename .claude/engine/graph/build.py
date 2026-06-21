"""
graph/build.py — 从 vault workflow template 构建 LangGraph

Phase 4a：仅支持 type=linear 步骤，构建线性 chain。
Phase 4b：支持 type=discussion，把多角色讨论 subgraph 作为父图的一个 node。
未来：parallel / 自定义条件路由由各 step 类型对应的 make_*_node 决定。
"""

from __future__ import annotations

from langgraph.graph import StateGraph, START, END

from ..role_loader import load_role
from ..workflow import WorkflowTemplate, WorkflowStep, role_to_skill_dir
from .nodes import make_role_node, make_discussion_node, make_brainstorm_loop_node
from .state import ProjectState


def _normalize(name_or_alias: str) -> str:
    return load_role(name_or_alias).name


def _step_node_key(step: WorkflowStep, idx: int) -> str:
    """生成 LangGraph 内部唯一节点名。"""
    if step.type == "linear":
        return f"step_{idx:02d}_{role_to_skill_dir(_normalize(step.role))}"
    if step.type == "discussion":
        # 中文名转 ASCII 替代字符以避免 graph key 问题
        safe = (step.name or "discussion").replace(" ", "_").replace("/", "_")
        return f"step_{idx:02d}_disc_{safe}"
    if step.type == "brainstorm-loop":
        safe = (step.name or "brainstorm").replace(" ", "_").replace("/", "_")
        return f"step_{idx:02d}_bsloop_{safe}"
    raise NotImplementedError(
        f"工作流步骤 type='{step.type}' 暂不支持。"
        f"已支持：linear / discussion / brainstorm-loop。"
    )


def _step_match_target(step: WorkflowStep, target: str) -> bool:
    """步骤是否匹配 --start-from / --end-at 目标。

    - linear：按角色名匹配（中文名或英文别名都可，统一 normalize 后比较）
    - discussion / brainstorm-loop：按 step.name 匹配
      （不按 participants 匹配——避免参与者与后续 linear 步骤角色重名时锚点歧义）
    """
    if step.type == "linear":
        try:
            return _normalize(step.role) == _normalize(target)
        except Exception:
            return False
    if step.type in ("discussion", "brainstorm-loop"):
        return (step.name or "") == target
    return False


def _slice_steps(
    steps: tuple[WorkflowStep, ...],
    start_from: str | None,
    end_at: str | None,
) -> list[WorkflowStep]:
    out = list(steps)
    if start_from:
        for i, s in enumerate(out):
            if _step_match_target(s, start_from):
                out = out[i:]
                break
        else:
            raise ValueError(
                f"--start-from='{start_from}' 不匹配任何步骤"
                f"（linear 用角色名 / discussion 用讨论名）"
            )
    if end_at:
        for i in range(len(out) - 1, -1, -1):
            if _step_match_target(out[i], end_at):
                out = out[: i + 1]
                break
        else:
            raise ValueError(
                f"--end-at='{end_at}' 不匹配任何步骤"
                f"（linear 用角色名 / discussion 用讨论名）"
            )
    return out


def _make_node_for_step(step: WorkflowStep, halt_on_failure: bool):
    if step.type == "linear":
        return make_role_node(
            _normalize(step.role),
            halt_on_failure,
            post_compress=step.post_compress,
            pre_flight=step.pre_flight if (step.pre_flight or step.auto_split) else None,
            skip_if=step.skip_if,
        )
    if step.type == "discussion":
        return make_discussion_node(step, halt_on_failure)
    if step.type == "brainstorm-loop":
        return make_brainstorm_loop_node(step, halt_on_failure)
    raise NotImplementedError(f"未知步骤类型：{step.type}")


def build_graph(
    template: WorkflowTemplate,
    *,
    start_from: str | None = None,
    end_at: str | None = None,
):
    """从工作流模板构建可 invoke 的 StateGraph。

    返回 compile 后的 graph，调用 `.invoke(initial_state)` 执行。
    Phase 4b 支持 type=linear 与 type=discussion 混合的步骤序列。
    """
    sliced = _slice_steps(template.steps, start_from, end_at)
    if not sliced:
        raise ValueError("链路裁剪后为空")

    g = StateGraph(ProjectState)

    node_keys: list[str] = []
    for idx, step in enumerate(sliced):
        key = _step_node_key(step, idx)
        # 防止重复（极少见，但保护一下）
        if key in node_keys:
            continue
        g.add_node(key, _make_node_for_step(step, template.halt_on_failure))
        node_keys.append(key)

    g.add_edge(START, node_keys[0])
    for i in range(len(node_keys) - 1):
        g.add_edge(node_keys[i], node_keys[i + 1])
    g.add_edge(node_keys[-1], END)

    return g.compile()
