"""
capability_executor/executors/node_executor.py — runtime.type=node 分派器（P10）。

差异（相对 python/shell）：
- argv 首项 = "node"（须在 PATH；shutil.which 前置检查）
- runtime.deps 是 npm 包（node_executor 不做自动 install，只验证存在）

subprocess 执行主体在 `_common.run_capability_subprocess`（2026-07-18 评审去重）。
"""

from __future__ import annotations

import shlex
import shutil
from typing import Any

from ..base import ExecutorResult
from ._common import (
    error_result,
    render_argv_template,
    resolve_working_dir,
    run_capability_subprocess,
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
            return error_result("`node` 未在 PATH 中；请先安装 Node.js 18+")

        runtime = manifest.get("runtime", {})
        entry_raw = runtime.get("entry", "")
        working_dir = resolve_working_dir(manifest)
        if not working_dir.is_dir():
            return error_result(f"runtime.working_dir 不存在：{working_dir}")

        entry_tokens = shlex.split(entry_raw, posix=True)
        if not entry_tokens:
            return error_result("runtime.entry 为空")
        rendered = render_argv_template(entry_tokens, inputs, project)
        script_path = (working_dir / rendered[0]).resolve()
        if not script_path.is_file():
            return error_result(f"runtime.entry 脚本不存在：{script_path}")

        argv = [node_bin, str(script_path), *rendered[1:]]
        # A3（P10.5）：按 sandbox.network 拦截；huashu-design manifest 声明 enabled，
        # 需要网络的 capability 显式声明 network: enabled 即绕过拦截
        return run_capability_subprocess(
            argv,
            working_dir=working_dir,
            manifest=manifest,
            inputs=inputs,
            project=project,
        )
