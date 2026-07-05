"""
capability_executor/base.py — Executor Protocol + Result + 异常层次。

设计要点：
- Protocol 而不是 ABC：新 runtime 只需 duck-typing 就能注册（跟 role_loader.Role
  dataclass 不继承的一致风格）
- ExecutorResult 是 frozen dataclass：跨 executor 结构统一（audit_writer 消费）
- 异常层次：所有本模块自定义异常继承 CapabilityExecutorError，方便 invoke.py 一网打尽
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


class CapabilityExecutorError(Exception):
    """capability_executor 顶层异常。所有子模块异常继承此，方便调用方一网打尽。"""


class ManifestValidationError(CapabilityExecutorError):
    """manifest schema 校验失败（缺字段 / 类型错 / 枚举外）。fail-closed。"""


class SandboxViolationError(CapabilityExecutorError):
    """路径超出 sandbox.allowed_paths（inputs.file_ref 或 outputs.path_pattern）。fail-closed。"""


class RuntimeMismatchError(CapabilityExecutorError):
    """manifest.runtime.type 在允许集内但 executor 未实现（如 P9 阶段的 node/http/mcp）。fail-fast。"""


@dataclass(frozen=True)
class ExecutorResult:
    """Executor 调用结果（跨 python/shell/node 结构统一）。

    - exit_code：subprocess returncode；-1 = timeout；-2 = 前置错误（依赖 / working_dir 缺失等）
    - duration_s：从 subprocess 起始到 return 的墙钟时间（秒，精度到毫秒）
    - stdout / stderr：subprocess 捕获，utf-8 解码；截断可选
    - artifact_paths：executor 解析 manifest.outputs 后确认存在的产物文件绝对路径
    - error：exit_code ≠ 0 时的一句话原因（audit 里写入的 error 字段）
    """
    exit_code: int
    duration_s: float
    stdout: str
    stderr: str
    artifact_paths: list[Path] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class Executor(Protocol):
    """Executor 接口约定（duck-typing）。

    实现方（python_executor / shell_executor / node_executor）只需暴露同名方法。
    invoke.py 按 `manifest.runtime.type` 分派到对应 executor 实例。
    """

    def invoke(
        self,
        manifest: dict,
        inputs: dict[str, Any],
        project: str,
    ) -> ExecutorResult:
        """调用 capability。

        参数：
        - manifest：已经过 `manifest_loader.load_and_validate` 校验的 dict
        - inputs：已经过 `sandbox.check_inputs` 校验的用户输入（key -> str/int/bool/...）
        - project：项目名（用于展开 path_pattern / audit.log_to 里的 `{project}` 占位符）

        返回：ExecutorResult。**不** raise（网络失败 / 依赖缺失 / timeout 都返回非零 exit_code）。
        """
        ...
