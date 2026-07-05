"""
capability_executor — P9/P10 能力通路（OS 层 subprocess，model-agnostic）。

规范：`00-系统/规则/capability注册表规范.md`
立项：`20-知识/项目记录/capability注册表机制-立项-2026-07-02.md`

三层架构：
- **base**：`Executor` Protocol + `ExecutorResult` + 异常层次
- **manifest_loader + sandbox**：读 vault manifest + schema/沙箱校验
- **executors/{python,shell,node}**：runtime 分派
- **audit_writer**：写 vault 调用日志 + `.claude/audit.jsonl`
- **invoke**：CLI 入口 `python -m engine.capability_executor.invoke`

暴露的公共 API 供 tests 和 invoke.py 消费。
"""

from __future__ import annotations

from .base import (
    CapabilityExecutorError,
    Executor,
    ExecutorResult,
    ManifestValidationError,
    RuntimeMismatchError,
    SandboxViolationError,
)

__all__ = [
    "CapabilityExecutorError",
    "Executor",
    "ExecutorResult",
    "ManifestValidationError",
    "RuntimeMismatchError",
    "SandboxViolationError",
]
