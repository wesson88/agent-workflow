"""
graph/nodes.py — LangGraph node 工厂

Phase 4a：单角色 node = subprocess 包装 `.claude/skills/<role>/main.py`
Phase 4b：新增 make_discussion_node = 多角色讨论 subgraph 包装

每个 node 接收 ProjectState，返回 patch dict（LangGraph 自动 merge）。
"""

from __future__ import annotations

import os
import subprocess
import sys

from ..config import PROJECT_ROOT
from ..workflow import role_to_skill_dir, WorkflowStep
from .discussion import build_discussion_graph
from .state import ProjectState


def make_role_node(role_name: str, halt_on_failure: bool):
    """工厂函数：返回一个 LangGraph node 函数。

    role_name 是 vault 角色 frontmatter 的 role 字段（中文名）。
    halt_on_failure 由工作流模板决定，闭包捕获。
    """
    skill_dir = role_to_skill_dir(role_name)
    main_py = PROJECT_ROOT / ".claude" / "skills" / skill_dir / "main.py"

    def node(state: ProjectState) -> dict:
        # 上游 halt 时跳过本 node
        if state.get("halted"):
            print(f"\n⏭️  跳过 {role_name}（上游 halt）")
            return {"skipped": [role_name]}

        print(f"\n{'=' * 60}\n▶ 运行 {role_name} ({skill_dir})  项目={state['project']}\n{'=' * 60}")

        env = os.environ.copy()
        env["PROJECT"] = state["project"]
        env["TASK"] = state["task"]
        rc = subprocess.run(
            [sys.executable, str(main_py),
             "--task", state["task"],
             "--project", state["project"]],
            env=env,
        ).returncode

        if rc != 0:
            print(f"\n❌ {role_name} 失败（exit={rc}）")
            patch = {"failed": [role_name]}
            if halt_on_failure:
                patch["halted"] = True
                print("中断后续步骤（halt_on_failure=True）")
            return patch
        print(f"\n✅ {role_name} 完成")
        return {"succeeded": [role_name]}

    # node 名要对 LangGraph 唯一；用角色中文名 + skill_dir 拼接
    node.__name__ = f"node_{skill_dir}"
    return node


# ── 讨论 node（Phase 4b）────────────────────────────────
_DISCUSSION_GRAPH = None  # 进程内单例（subgraph 不依赖 step config，可复用）


def _get_discussion_graph():
    global _DISCUSSION_GRAPH
    if _DISCUSSION_GRAPH is None:
        _DISCUSSION_GRAPH = build_discussion_graph()
    return _DISCUSSION_GRAPH


def make_discussion_node(step: WorkflowStep, halt_on_failure: bool):
    """工厂函数：把 type=discussion 的 WorkflowStep 包装成主图 node。

    主图 node 入口接收 ProjectState（项目级 state），内部派生 DiscussionState
    去 invoke 讨论 subgraph，跑完后回写主图 state。
    """
    name = step.name or "未命名讨论"
    participants = step.roles
    moderator = step.moderator
    max_rounds = step.max_rounds
    topic_template = step.topic_template or "评审本项目至此为止的所有产出"

    def node(state: ProjectState) -> dict:
        if state.get("halted"):
            print(f"\n⏭️  跳过讨论『{name}』（上游 halt）")
            return {"skipped": [f"讨论:{name}"]}

        print(f"\n{'=' * 60}\n💬 讨论节点：{name}\n  参与者：{list(participants)}\n{'=' * 60}")

        # 议题：模板里的 {project} / {task} 占位符替换
        topic = topic_template.replace("{project}", state["project"]).replace("{task}", state["task"])

        sub_state = {
            "project": state["project"],
            "task": state["task"],
            "topic": topic,
            "participants": participants,
            "moderator": moderator,
            "max_rounds": max_rounds,
            "discussion_name": name,
            "messages": [],
            "current_round": 0,
            "next_speaker": None,
            "finished": False,
        }

        try:
            _get_discussion_graph().invoke(sub_state)
        except Exception as e:
            print(f"\n❌ 讨论『{name}』异常：{e}")
            patch = {"failed": [f"讨论:{name}"]}
            if halt_on_failure:
                patch["halted"] = True
            return patch

        print(f"\n✅ 讨论『{name}』完成")
        return {"succeeded": [f"讨论:{name}"]}

    node.__name__ = f"discussion_{name.replace(' ', '_')}"
    return node
