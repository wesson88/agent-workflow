"""
capability_executor/executors/node_executor.py — runtime.type=node 分派器（P10）。

差异（相对 python/shell）：
- argv 首项 = "node"（须在 PATH；shutil.which 前置检查）
- runtime.deps 是 npm 包（node_executor 不做自动 install，只验证存在）

subprocess 执行主体在 `_common.run_capability_subprocess`（2026-07-18 评审去重）。

2026-09-03：上面那句「只验证存在」曾是**空头支票** —— 全仓没有任何一处读
`runtime.deps`，manifest_loader 也只把它当 list 类型校验一下。而
`huashu-design/manifest.json` 实打实声明了 `deps: ["playwright"]`：没装的话
用户拿到的是 node 的 `MODULE_NOT_FOUND` 栈，而不是这里承诺的那句人话。
本次把检查补上（见 `_missing_npm_deps`），让 docstring 与实现对齐。
"""

from __future__ import annotations

import shlex
import shutil
from pathlib import Path
from typing import Any

from ..base import ExecutorResult
from ._common import (
    error_result,
    render_argv_template,
    resolve_working_dir,
    run_capability_subprocess,
)


def npm_package_name(dep: str) -> str:
    """从 `runtime.deps` 条目取包名，剥掉版本区间。

    npm 的 scope 包本身以 `@` 开头（`@playwright/test@^1.4`），所以不能无脑
    split("@")[0] —— scope 包会被切成空串。
    """
    dep = dep.strip()
    if dep.startswith("@"):
        head, sep, _tail = dep[1:].partition("@")
        return "@" + head if sep else dep
    return dep.partition("@")[0]


def _missing_npm_deps(deps: list, working_dir: Path) -> list[str]:
    """按 node 的解析规则逐级向上找 `node_modules/<pkg>`，返回找不到的。

    向上找是必须的：monorepo / npm workspaces 把依赖提升到仓根的
    `node_modules/`，只看 `working_dir/node_modules` 会把装好的包误报为缺失。
    只判目录存在、不读 package.json 版本 —— 版本区间求解是 npm 自己的活，
    这里要的是「早点给一句人话」，不是重做一个包管理器。
    """
    missing: list[str] = []
    for dep in deps:
        if not isinstance(dep, str) or not dep.strip():
            continue
        name = npm_package_name(dep)
        if not name:
            continue
        found = False
        for base in (working_dir, *working_dir.parents):
            try:
                if (base / "node_modules" / name).exists():
                    found = True
                    break
            except OSError:
                break
        if not found:
            missing.append(name)
    return missing


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

        missing = _missing_npm_deps(runtime.get("deps") or [], working_dir)
        if missing:
            return error_result(
                f"runtime.deps 声明的 npm 包未安装：{missing}"
                f"（在 {working_dir} 或其上级目录的 node_modules 下都没找到）。"
                f"先 `npm install` 再重试 —— 本执行器不做自动安装。"
            )

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
