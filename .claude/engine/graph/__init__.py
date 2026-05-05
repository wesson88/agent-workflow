"""
.claude/engine/graph — LangGraph-based 工作流编排（Phase 4）

把 Phase 3a 的 run_chain 子进程串行实现升级为 StateGraph：
- Phase 4a：线性等价（每个角色一个 node，subprocess 包装现有 main.py）
- Phase 4b：加讨论循环（架构师 ↔ 技术主管，max_iterations 退出）+ in-process 重构
- Phase 4c：复盘 agent（独立 graph）

公开 API：
- ProjectState：StateGraph 的 state schema
- build_graph(template, start_from, end_at)：从 vault workflow template 构建可调用 graph
"""

from .state import ProjectState
from .build import build_graph

__all__ = ["ProjectState", "build_graph"]
