"""
capability_executor/executors/shell_executor.py — runtime.type=shell 分派器。

跟 python_executor 主要差异：
- argv = shlex.split(entry)，首 token 就是命令（如 `git`）
- 不预设脚本文件必须存在的检查（shell 命令可能是 PATH 命令）
- shell=False（安全默认；`shell_executor` 已经代表用户主动选 shell，不再套 shell=True）
"""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

from ..base import ExecutorResult
from ..manifest_loader import get_timeout_s
from ._common import (
    render_argv_template,
    resolve_artifact_paths,
    resolve_working_dir,
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
            return ExecutorResult(
                exit_code=-2,
                duration_s=0.0,
                stdout="",
                stderr="",
                error=f"runtime.working_dir 不存在：{working_dir}",
            )

        tokens = shlex.split(entry_raw, posix=True)
        if not tokens:
            return ExecutorResult(
                exit_code=-2,
                duration_s=0.0,
                stdout="",
                stderr="",
                error="runtime.entry 为空",
            )
        argv = render_argv_template(tokens, inputs, project)
        env = os.environ.copy()
        for k, v in (runtime.get("env") or {}).items():
            env[str(k)] = str(v)
        timeout_s = get_timeout_s(manifest)

        t0 = time.monotonic()
        try:
            proc = subprocess.run(  # noqa: S603 — argv 全由 executor 内部构造
                argv,
                cwd=str(working_dir),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_s,
                shell=False,
            )
        except subprocess.TimeoutExpired as e:
            return ExecutorResult(
                exit_code=-1,
                duration_s=time.monotonic() - t0,
                stdout=e.stdout or "",
                stderr=e.stderr or "",
                error=f"timeout after {timeout_s}s",
            )
        except OSError as e:
            return ExecutorResult(
                exit_code=-2,
                duration_s=time.monotonic() - t0,
                stdout="",
                stderr="",
                error=f"subprocess 启动失败：{e}",
            )

        duration = time.monotonic() - t0
        rc = proc.returncode
        artifact_paths: list[Path] = []
        error: str | None = None
        if rc == 0:
            artifact_paths = resolve_artifact_paths(manifest, inputs, project)
        else:
            error = f"exit_code={rc}: {proc.stderr.strip()[:500]}"
        return ExecutorResult(
            exit_code=rc,
            duration_s=duration,
            stdout=proc.stdout,
            stderr=proc.stderr,
            artifact_paths=artifact_paths,
            error=error,
        )
