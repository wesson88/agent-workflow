"""
capability_executor.executors — 按 runtime.type 分派的 Executor 实现集。

- python_executor：`runtime.type = python`（P9 首个实现）
- shell_executor：`runtime.type = shell`（P9）
- node_executor：`runtime.type = node`（P10）
- http / claude-code-skill / mcp：延后（manifest_loader 会 fail-fast 提示未支持）

暴露：`get_executor(runtime_type) -> Executor` 分派工厂。
"""

from __future__ import annotations

from ..base import Executor, RuntimeMismatchError
from .node_executor import NodeExecutor
from .python_executor import PythonExecutor
from .shell_executor import ShellExecutor

_REGISTRY: dict[str, Executor] = {
    "python": PythonExecutor(),
    "shell": ShellExecutor(),
    "node": NodeExecutor(),
}


def get_executor(runtime_type: str) -> Executor:
    """按 runtime.type 返回 executor 实例。

    未实现（如 P9 阶段的 node/http/claude-code-skill/mcp） → RuntimeMismatchError。
    P10 会在此 registry 注册 node_executor；后续 runtime 同款扩展。
    """
    if runtime_type not in _REGISTRY:
        raise RuntimeMismatchError(
            f"runtime.type='{runtime_type}' 未实现 executor（P9 支持 python/shell；"
            f"P10 加 node；http/claude-code-skill/mcp 延后）。"
            f"已注册：{sorted(_REGISTRY.keys())}"
        )
    return _REGISTRY[runtime_type]


def is_runtime_supported(runtime_type: str) -> bool:
    """该 runtime.type 是否有已实现的 executor。

    摘要注入（skills/common._render_capability_summary）据此分档措辞：已实现的
    才告诉角色「可调用」，未实现的明说「无法调用，仅作背景知悉」。
    以前措辞对所有能力一律写「已注册且工程实现完成 / 请优先 invoke」，对
    runtime=mcp 这种没 executor 的能力就是在诱导角色编造调用记录。
    """
    return runtime_type in _REGISTRY


def register_executor(runtime_type: str, executor: Executor) -> None:
    """P10 及以后扩展 runtime 时调用（避免修改本 __init__ 硬编码）。"""
    _REGISTRY[runtime_type] = executor
