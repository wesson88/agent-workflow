"""
capability_executor/executors/_common.py — python/shell/node executor 复用的 helper。

- `render_argv_template`：把 `runtime.entry` 里的 `{{input_name}}` 替换为实际值
- `resolve_working_dir`：算 subprocess cwd（manifest 显式 > project_code_root 兜底）
- `resolve_artifact_paths`：按 manifest.outputs.path_pattern 渲染 + 校验文件存在
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ...config import PROJECT_ROOT, VAULT_ROOT, project_code_root

_TEMPLATE_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")

# vault 前缀清单：一旦渲染后的 token 以这些开头，视为 vault 相对路径，
# executor 会展开为 VAULT_ROOT 绝对路径传给 subprocess。
# 避免 subprocess 相对 cwd（working_dir）写文件导致 artifact 逃逸出 vault 沙箱。
_VAULT_PREFIXES = ("10-项目/", "20-知识/", "00-系统/", "99-临时/")


def render_argv_template(
    tokens: list[str],
    inputs: dict[str, Any],
    project: str,
) -> list[str]:
    """把 tokens 里的 `{{input_name}}` 和 `{project}` 替换成实际值。

    - `{{...}}`：从 inputs 找；找不到 → 保留原样（executor 后续报错 or 脚本处理）
    - `{project}`：直接替换项目名
    - **vault 相对路径 → 绝对路径**：渲染完毕后若 token 以 `10-项目/` / `20-知识/` /
      `00-系统/` / `99-临时/` 开头，前缀 `VAULT_ROOT` 展开为绝对路径。
      避免 subprocess 相对 cwd 写文件（依据：sandbox 强约束 artifact 落 vault 内）。
    - `{ts}` / `{name}` 等其它：只在 outputs.path_pattern 里由 resolve_artifact_paths 处理，
      本函数 argv 层不管
    """
    out: list[str] = []
    for tok in tokens:
        rendered = tok
        # {{input_name}}
        def _repl(m: re.Match) -> str:
            name = m.group(1)
            if name in inputs:
                return str(inputs[name])
            return m.group(0)  # 保留原样
        rendered = _TEMPLATE_RE.sub(_repl, rendered)
        rendered = rendered.replace("{project}", project)
        # vault 相对路径 → 绝对（P9 PoC 实测暴露：subprocess 相对 cwd 写文件会逃出沙箱）
        if any(rendered.startswith(p) for p in _VAULT_PREFIXES):
            rendered = str(VAULT_ROOT / rendered)
        out.append(rendered)
    return out


def resolve_working_dir(manifest: dict) -> Path:
    """算 subprocess cwd。

    优先级：
    1. manifest.runtime.working_dir（如果是绝对路径直接用；相对路径相对 PROJECT_ROOT）
    2. PROJECT_ROOT / tools / <root>（fallback）
    """
    runtime = manifest.get("runtime", {})
    wd = runtime.get("working_dir")
    if wd:
        p = Path(wd)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        return p
    root = manifest["id"].split("/", 1)[0]
    return PROJECT_ROOT / "tools" / root


def resolve_artifact_paths(
    manifest: dict,
    inputs: dict[str, Any],
    project: str,
) -> list[Path]:
    """按 manifest.outputs.path_pattern 渲染，检查文件确实存在，返回绝对路径列表。

    - type=file：渲染 + 检查存在
    - type=json/text/url：不落盘，跳过（executor 从 stdout 拿）
    - 相对路径基准：VAULT_ROOT（vault 内产物）
    """
    result: list[Path] = []
    for out_spec in manifest.get("outputs") or []:
        if out_spec.get("type") not in ("file",):
            continue
        pat = out_spec.get("path_pattern", "")
        rendered = _render_output_path(pat, inputs, project)
        p = Path(rendered)
        if not p.is_absolute():
            p = VAULT_ROOT / p
        if p.is_file():
            result.append(p)
    return result


def _render_output_path(
    pattern: str, inputs: dict[str, Any], project: str
) -> str:
    """展开 path_pattern 里的占位符。支持 `{project}` / `{name}` / `{input.X}` / `{ts}`（本文件不做 ts）。

    ts 由 audit_writer 层处理；outputs.path_pattern 里若含 {ts} 说明配置错误，返回原样让上游报错。
    """
    text = pattern.replace("{project}", project)
    for k, v in inputs.items():
        text = text.replace("{input." + k + "}", str(v))
    # {name} = capability name（id 的第二段）
    return text
