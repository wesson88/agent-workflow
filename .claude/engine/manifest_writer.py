"""
engine/manifest_writer.py — 模块清单 status 更新（P8.4）

将模块清单.md 里 yaml block 的某个 node.status 改成新值，同时保留 markdown
其余部分（H1 / H2 / Mermaid 等）不变。

策略：yaml block round-trip
1. parse_manifest（P8.1）解析 + DAG 校验
2. 找到目标 module 更新 status
3. yaml.dump 序列化新 yaml body
4. re.sub 回填 markdown（复用 manifest_render._YAML_BLOCK_RE）
5. 原子写入（.tmp → replace）

fail-closed：文件缺失 / module_id 不存在 / 非法 status → raise ManifestWriteError。
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

import yaml

from .manifest_render import _YAML_BLOCK_RE, parse_manifest
from .manifest_validator import (
    ALLOWED_STATUS,
    ManifestValidationError,
    _stringify_id,
    validate_manifest_nodes,
)


class ManifestWriteError(RuntimeError):
    """模块清单 status 更新失败。"""
    pass


def mark_status(manifest_path: Path, module_id: str, new_status: str) -> None:
    """把 manifest 里 module_id 对应 node 的 status 改成 new_status。

    - manifest_path 缺失 / yaml block 缺失 / DAG 校验失败 → raise
    - module_id 不存在 → raise
    - new_status 不在 ALLOWED_STATUS → raise
    - 更新后重新做 DAG 校验（防手动改文件产生环）
    """
    new_status = (new_status or "").strip()
    if new_status not in ALLOWED_STATUS:
        raise ManifestWriteError(
            f"非法 status='{new_status}'；允许：{sorted(ALLOWED_STATUS)}"
        )

    try:
        nodes = parse_manifest(manifest_path)
    except ManifestValidationError as e:
        raise ManifestWriteError(
            f"读 manifest {manifest_path} 失败：{e}"
        ) from e

    module_id_str = _stringify_id(module_id)
    target = None
    for n in nodes:
        if _stringify_id(n.get("id")) == module_id_str:
            target = n
            break
    if target is None:
        raise ManifestWriteError(
            f"module_id='{module_id_str}' 不在 manifest 里；已有："
            f"{sorted(_stringify_id(n['id']) for n in nodes)}"
        )
    target["status"] = new_status

    # 更新后再校验一次（不应破坏 DAG）
    try:
        validate_manifest_nodes(nodes)
    except ManifestValidationError as e:
        raise ManifestWriteError(
            f"更新 {module_id_str}.status={new_status} 后 DAG 校验失败：{e}"
        ) from e

    text = manifest_path.read_text(encoding="utf-8")
    match = _YAML_BLOCK_RE.search(text)
    if not match:
        raise ManifestWriteError(
            f"回写 {manifest_path} 时未找到 yaml block（与 parse_manifest 不一致）"
        )

    new_body = yaml.dump(
        {"nodes": nodes},
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    ).rstrip()

    # 构造替换段：保留 "## 结构化..." 前缀 + 新 yaml body + 尾部 ```
    prefix_end = match.start("body")
    suffix_start = match.end("body")
    new_text = text[:prefix_end] + new_body + text[suffix_start:]

    _atomic_write(manifest_path, new_text)


def _atomic_write(target: Path, content: str) -> None:
    """写入 .tmp → replace，避免中途崩溃留下半个文件。"""
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(target.parent),
        prefix=target.name + ".",
        suffix=".tmp",
        delete=False,
    ) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)
    tmp_path.replace(target)
