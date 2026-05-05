"""
graph/nodes.py — 单角色 LangGraph node 工厂

Phase 4a：每个 node 是 `.claude/skills/<role>/main.py` 的 subprocess 包装。
- 优点：现有 main.py 不变，最小破坏 + 立即可跑 LangGraph
- 缺点：state 共享只能通过 vault 文件，无法做讨论循环
- Phase 4b：会把核心逻辑抽到 in-process 函数，subprocess 入口保留作为 CLI 兼容

每个 node 接收 ProjectState，返回 patch dict（LangGraph 自动 merge）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from ..config import PROJECT_ROOT
from ..workflow import role_to_skill_dir
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
