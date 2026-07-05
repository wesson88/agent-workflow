"""
capability_executor/executors/node_executor.py — runtime.type=node 分派器（P10）。

跟 python_executor 差异：
- argv 首项 = "node"（须在 PATH；shutil.which 前置检查）
- runtime.deps 是 npm 包（node_executor 不做自动 install，只验证存在）
- 其他（sandbox / audit / timeout / stdout 解析）100% 复用 python_executor 模式
"""

from __future__ import annotations

import os
import shlex
import shutil
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


class NodeExecutor:
    """runtime.type=node 的执行器（P10）。"""

    def invoke(
        self,
        manifest: dict,
        inputs: dict[str, Any],
        project: str,
    ) -> ExecutorResult:
        node_bin = shutil.which("node")
        if not node_bin:
            return ExecutorResult(
                exit_code=-2,
                duration_s=0.0,
                stdout="",
                stderr="",
                error="`node` 未在 PATH 中；请先安装 Node.js 18+",
            )

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

        entry_tokens = shlex.split(entry_raw, posix=True)
        if not entry_tokens:
            return ExecutorResult(
                exit_code=-2,
                duration_s=0.0,
                stdout="",
                stderr="",
                error="runtime.entry 为空",
            )
        rendered = render_argv_template(entry_tokens, inputs, project)
        script_rel = rendered[0]
        script_path = (working_dir / script_rel).resolve()
        if not script_path.is_file():
            return ExecutorResult(
                exit_code=-2,
                duration_s=0.0,
                stdout="",
                stderr="",
                error=f"runtime.entry 脚本不存在：{script_path}",
            )

        argv = [node_bin, str(script_path), *rendered[1:]]
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
