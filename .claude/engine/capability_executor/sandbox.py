"""
capability_executor/sandbox.py — 路径 sandbox 校验。

规范：`00-系统/规则/capability注册表规范.md §3.2 + §12`

**校验点**：
1. `inputs.file_ref` 类型的输入路径：确认落在 `sandbox.allowed_paths` 内
2. `outputs.path_pattern` 渲染后：确认落在 `sandbox.allowed_paths` 内

**默认允许集**（manifest.sandbox.allowed_paths 缺省时兜底）：
- `10-项目/*/交付物/`（vault 项目产物目录）
- `20-知识/能力注册表/<root>/调用日志/`（audit 落盘位置，由 audit_writer 自动 pass）

**为什么用 fnmatch 而不是 glob.match**：
- fnmatch 是纯字符串模式匹配，跨平台一致（Windows Path.match 有 corner case）
- 现有 obsidian_io.py 未提供 glob 匹配 helper，本模块自持
"""

from __future__ import annotations

import fnmatch
from functools import lru_cache
from pathlib import Path, PurePosixPath

from ..config import VAULT_ROOT
from .base import SandboxViolationError


def _to_rel_posix(path: Path | str, root: Path) -> str | None:
    """把 path 转成相对 root 的 POSIX 路径字符串。

    - 绝对路径 → root.relative_to
    - 相对路径 → 假设已相对 vault_root
    - 无法定位到 root 内 → 返回 None（调用方判定为越权）
    """
    p = Path(path)
    root_r = root.resolve()
    if p.is_absolute():
        try:
            rel = p.resolve().relative_to(root_r)
        except ValueError:
            return None
    else:
        rel = p
    return str(PurePosixPath(rel))


@lru_cache(maxsize=256)
def _match_rel_against_patterns(rel: str, patterns_tuple: tuple[str, ...]) -> bool:
    """B5：可缓存的内层 fnmatch 循环。

    key = (rel_posix_str, allowed_patterns_tuple)；
    workflow 里 allowed_patterns 通常 3-5 个固定 pattern，rel 有大量重复
    （每个 capability 会对多个 input/output 校验），命中率高。
    """
    for pat in patterns_tuple:
        pat_clean = pat.rstrip("/")
        if fnmatch.fnmatch(rel, pat_clean):
            return True
        if pat.endswith("/") and fnmatch.fnmatch(rel, pat_clean + "/*"):
            return True
    return False


def check_path_within(
    path: Path | str,
    allowed_patterns: list[str],
    *,
    vault_root: Path | None = None,
) -> bool:
    """判断 path 是否落在 allowed_patterns 内（glob 匹配）。

    - path：待校验的路径（绝对或相对 vault_root）
    - allowed_patterns：形如 `"10-项目/*/交付物/"` 的 glob（fnmatch 语义）
    - vault_root：默认取 config.VAULT_ROOT；测试可传自定义

    匹配语义：
    - 目录后缀 `/`（如 `10-项目/*/交付物/`）视为"允许该目录及其子孙"
    - 无 `/` 后缀（如 `10-项目/*/API契约.md`）视为"精确文件匹配"
    - fnmatch 特性：`*` 不跨 `/`（跟 shell glob 一致，规范 §3.2 隐含）

    B5（P10.5）：内层 fnmatch 循环走 `_match_rel_against_patterns` lru_cache。
    """
    vr = (vault_root or VAULT_ROOT).resolve()
    rel = _to_rel_posix(path, vr)
    if rel is None:
        return False
    return _match_rel_against_patterns(rel, tuple(allowed_patterns))


def invalidate_cache() -> None:
    """B5：清 fnmatch cache（测试用）。"""
    _match_rel_against_patterns.cache_clear()


def assert_path_within(
    path: Path | str,
    allowed_patterns: list[str],
    *,
    label: str = "path",
    vault_root: Path | None = None,
) -> None:
    """check_path_within 的 fail-closed 变体。违反 → SandboxViolationError。"""
    if not check_path_within(path, allowed_patterns, vault_root=vault_root):
        raise SandboxViolationError(
            f"{label} '{path}' 越出 sandbox.allowed_paths={allowed_patterns}"
        )


def default_allowed_paths(root: str) -> list[str]:
    """capability 的默认沙箱允许集。依据：规范 §3.2。

    root 是 capability_id 的 `<root>` 部分（如 `web-scraper`）。
    """
    return [
        "10-项目/*/交付物/",
        f"20-知识/能力注册表/{root}/调用日志/",
    ]


def get_sandbox_allowed(manifest: dict) -> list[str]:
    """从 manifest 读 sandbox.allowed_paths，缺失回退默认集。"""
    sandbox = manifest.get("sandbox") or {}
    allowed = sandbox.get("allowed_paths")
    if allowed:
        return list(allowed)
    root = manifest["id"].split("/", 1)[0]
    return default_allowed_paths(root)
