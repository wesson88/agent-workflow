"""video_gen 基础类型与调度（见包 docstring）。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal

KINDS = ("reference", "keyframe", "video", "tts", "compose")


@dataclass(frozen=True)
class GenTask:
    kind: str                       # reference / keyframe / video / tts / compose
    unit: str                       # references / G01 / voice / master …
    project: str
    domain: str
    expected: tuple[str, ...]       # 相对 ASSET_ROOT 的 glob；每条至少命中 1 个文件才算完成
    refs: tuple[str, ...] = ()      # 执行方要照着做的 vault 文档（prompt 包、表单、时间线）
    attempt: int = 1
    brief: str = ""
    params: dict = field(default_factory=dict)   # 透传给 adapter（时长、数量等）

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError(f"未知生成任务类型 {self.kind!r}（允许：{KINDS}）")
        if not self.expected:
            raise ValueError(f"{self.kind}:{self.unit} 没有期望产物——无法判定完成")

    @property
    def key(self) -> str:
        return f"generation:{self.kind}:{self.unit}:a{self.attempt}"


@dataclass(frozen=True)
class GenResult:
    status: Literal["done", "pending", "failed"]
    missing: tuple[str, ...] = ()
    detail: str = ""


Adapter = Callable[[GenTask, Path], GenResult]
ADAPTERS: dict[str, Adapter] = {}


def register_adapter(name: str, fn: Adapter) -> None:
    ADAPTERS[name] = fn


def adapter_name(kind: str) -> str:
    return (os.environ.get(f"VIDEO_GEN_ADAPTER_{kind.upper()}")
            or os.environ.get("VIDEO_GEN_ADAPTER") or "human_ui")


def missing_outputs(task: GenTask, root: Path) -> list[str]:
    return [pat for pat in task.expected if not any(root.glob(pat))]


def run_task(task: GenTask) -> GenResult:
    from ..config import asset_root
    root = asset_root(task.domain, task.project)
    if root is None:
        return GenResult("failed", task.expected, "ASSET_ROOT 未配置，执行层无落盘根目录")
    if not missing_outputs(task, root):
        return GenResult("done", (), "期望产物已齐（跳过执行）")
    name = adapter_name(task.kind)
    if name not in ADAPTERS:
        _register_builtin()
    fn = ADAPTERS.get(name)
    if fn is None:
        return GenResult("failed", task.expected, f"未注册的执行 adapter：{name}（已知：{sorted(ADAPTERS)}）")
    result = fn(task, root)
    if result.status == "done":
        still = missing_outputs(task, root)
        if still:   # adapter 说做完了但文件不齐——不信口头，按缺件处理
            return GenResult("failed", tuple(still), f"{name} 报完成但缺：{still}")
    return result


def _register_builtin() -> None:
    from . import human_ui, mock  # noqa: F401  导入即注册
