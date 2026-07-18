"""
capability_executor/executors/python_executor.py — runtime.type=python 分派器。

差异（相对 shell/node）：argv 首项固定 sys.executable；entry 首 token 是脚本
文件名（必须存在），其余作为参数（含模板变量渲染）。

subprocess 执行主体（env / timeout / 错误归一 / artifact 收集）在
`_common.run_capability_subprocess`（2026-07-18 评审去重）。
**不做**重试。capability 应是幂等；重试语义留给上层 workflow。
"""

from __future__ import annotations

import shlex
import sys
from typing import Any

from ..base import ExecutorResult
from ._common import (
    error_result,
    render_argv_template,
    resolve_working_dir,
    run_capability_subprocess,
)


class PythonExecutor:
    """runtime.type=python 的执行器。"""

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

        # 拆首 token 作为脚本文件 + 剩余作为 args；对首 token 也做模板渲染以支持
        # {{...}} 出现在文件名（少见但允许）
        entry_tokens = shlex.split(entry_raw, posix=True)
        if not entry_tokens:
            return error_result("runtime.entry 为空")
        rendered = render_argv_template(entry_tokens, inputs, project)
        script_path = (working_dir / rendered[0]).resolve()
        if not script_path.is_file():
            return error_result(f"runtime.entry 脚本不存在：{script_path}")

        argv = [sys.executable, str(script_path), *rendered[1:]]
        # Windows 上 Python 子进程 stdout 默认 GBK；强制 utf-8 让 audit stdout 段
        # 可读（P9 PoC 实测：不设时中文变 ��）
        return run_capability_subprocess(
            argv,
            working_dir=working_dir,
            manifest=manifest,
            inputs=inputs,
            project=project,
            extra_env={"PYTHONIOENCODING": "utf-8"},
        )
