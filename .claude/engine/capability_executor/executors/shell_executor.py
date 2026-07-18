"""
capability_executor/executors/shell_executor.py — runtime.type=shell 分派器。

差异（相对 python/node）：
- argv = shlex.split(entry) 渲染后直接执行，首 token 就是命令（如 `git`）
- 不预设脚本文件必须存在的检查（shell 命令可能是 PATH 命令）
- shell=False（安全默认；`shell_executor` 已经代表用户主动选 shell，不再套 shell=True）

subprocess 执行主体在 `_common.run_capability_subprocess`（2026-07-18 评审去重）。
"""

from __future__ import annotations

import shlex
from typing import Any

from ..base import ExecutorResult
from ._common import (
    error_result,
    render_argv_template,
    resolve_working_dir,
    run_capability_subprocess,
)


class ShellExecutor:
    """runtime.type=shell 的执行器。"""

    def invoke(
        self,
        manifest: dict,
        inputs: dict[str, Any],
        project: str,
    ) -> ExecutorResult:
        runtime = manifest.get("runtime", {})
        entry_raw = runtime.get("entry", "")
        working_dir = resolve_working_dir(manifest)
        if not working_dir.is_dir():
            return error_result(f"runtime.working_dir 不存在：{working_dir}")

        tokens = shlex.split(entry_raw, posix=True)
        if not tokens:
            return error_result("runtime.entry 为空")
        argv = render_argv_template(tokens, inputs, project)

        # A3（P10.5）：shell runtime 默认 network=disabled（依据：规范 §4 强约束），
        # 由 run_capability_subprocess 内的 apply_network_sandbox 统一处理
        return run_capability_subprocess(
            argv,
            working_dir=working_dir,
            manifest=manifest,
            inputs=inputs,
            project=project,
        )
