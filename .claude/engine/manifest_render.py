"""
engine/manifest_render.py — 模块清单渲染 + ready 集计算（P8.1）

功能：
- `parse_manifest`：从 `模块清单.md` 抽 nodes yaml block；校验后返回 nodes
- `compute_ready_set`：依赖全 done 的 pending 节点（供 human_gate select_module）
- `render_mermaid`：Mermaid graph LR 图（Obsidian 原生渲染）
- `render_summary`：状态汇总 + 阻塞检测

用途：
- P8.3 human_gate select_module：从 manifest 计算 ready 集 → options
- 未来 P8.5 workflow 主循环 status 汇总打印

依赖：manifest_validator.validate_manifest_nodes（fail-closed）
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from .manifest_validator import (
    ALLOWED_ROLES,
    ManifestValidationError,
    _stringify_id,
    validate_manifest_nodes,
)


# 依据：`## 结构化（DAG 原始数据，引擎消费）` 段内嵌 ```yaml ... ``` 块
# 来源 [[模块清单与人机协同工作流-2026-07-02 §4.3]] 示例
_YAML_BLOCK_RE = re.compile(
    r"##\s*结构化.*?\n```yaml\n(?P<body>.*?)\n```",
    re.DOTALL,
)


def parse_manifest(manifest_path: Path) -> list[dict]:
    """从模块清单 .md 抽取 nodes yaml block；校验后返回 nodes。

    fail-closed：文件缺失 / 无 yaml block / yaml 语法错 / DAG 校验失败 →
    raise ManifestValidationError。
    """
    if not manifest_path.is_file():
        raise ManifestValidationError(f"模块清单文件不存在：{manifest_path}")
    text = manifest_path.read_text(encoding="utf-8")
    m = _YAML_BLOCK_RE.search(text)
    if not m:
        raise ManifestValidationError(
            f"模块清单 {manifest_path} 未找到 '## 结构化 ... ```yaml' block"
        )
    try:
        data = yaml.safe_load(m.group("body")) or {}
    except yaml.YAMLError as e:
        raise ManifestValidationError(
            f"模块清单 {manifest_path} yaml 解析失败：{e}"
        ) from e
    if not isinstance(data, dict):
        raise ManifestValidationError(
            f"模块清单 {manifest_path} yaml block 顶层必须是 dict"
        )
    nodes = data.get("nodes")
    if not isinstance(nodes, list):
        raise ManifestValidationError(
            f"模块清单 {manifest_path} 缺 'nodes:' 键或非 list"
        )
    validate_manifest_nodes(nodes)
    return nodes


def compute_ready_set(nodes: list[dict]) -> list[dict]:
    """返回可开始（ready）的 node 列表：status='pending' 且 depends_on 全 done。

    P8.3 human_gate select_module 选项来源。按 id 排序保证稳定性。
    """
    status_by_id: dict[str, str] = {
        _stringify_id(n["id"]): str(n.get("status", "")).strip()
        for n in nodes
    }
    ready: list[dict] = []
    for n in nodes:
        if str(n.get("status", "")).strip() != "pending":
            continue
        deps = n.get("depends_on") or []
        if all(status_by_id.get(_stringify_id(d)) == "done" for d in deps):
            ready.append(n)
    ready.sort(key=lambda n: _stringify_id(n["id"]))
    return ready


def render_summary(nodes: list[dict]) -> dict:
    """返回状态汇总 dict：counts / ready_ids / blocked_ids / total_estimate_hours。

    - blocked_ids：pending 但依赖有 blocked / in_progress 的节点
    - ready_ids：见 compute_ready_set
    """
    counts: dict[str, int] = {}
    total_est = 0.0
    for n in nodes:
        s = str(n.get("status", "")).strip()
        counts[s] = counts.get(s, 0) + 1
        try:
            total_est += float(n.get("estimate_hours") or 0)
        except (TypeError, ValueError):
            pass

    status_by_id = {
        _stringify_id(n["id"]): str(n.get("status", "")).strip() for n in nodes
    }
    ready_ids = [_stringify_id(n["id"]) for n in compute_ready_set(nodes)]

    blocked_ids: list[str] = []
    for n in nodes:
        if str(n.get("status", "")).strip() != "pending":
            continue
        deps = n.get("depends_on") or []
        if any(
            status_by_id.get(_stringify_id(d)) in ("blocked", "in_progress")
            or status_by_id.get(_stringify_id(d)) == "pending"
            for d in deps
        ) and _stringify_id(n["id"]) not in ready_ids:
            blocked_ids.append(_stringify_id(n["id"]))

    return {
        "counts": counts,
        "total_estimate_hours": round(total_est, 1),
        "ready_ids": ready_ids,
        "blocked_ids": sorted(blocked_ids),
    }


# 依据：Mermaid 状态色（Obsidian 原生渲染友好，来源 §4.3 示例）
_STATUS_CLASS = {
    "done": "done",
    "in_progress": "wip",
    "pending": "pending",
    "blocked": "blocked",
}


def render_mermaid(nodes: list[dict]) -> str:
    """产出 Mermaid `graph LR` 源码块（可写回 `## 拓扑` 段落）。"""
    lines: list[str] = ["```mermaid", "graph LR"]
    for n in nodes:
        nid = _stringify_id(n["id"])
        title = str(n.get("title", "")).replace('"', '\\"')
        status = str(n.get("status", "")).strip()
        cls = _STATUS_CLASS.get(status, "pending")
        label = f'{nid} {title}<br/>{status}'
        lines.append(f'  {nid}["{label}"]:::{cls}')
    for n in nodes:
        nid = _stringify_id(n["id"])
        for dep in n.get("depends_on") or []:
            lines.append(f"  {_stringify_id(dep)} --> {nid}")
    lines.extend([
        "classDef done fill:#90EE90",
        "classDef wip fill:#FFD700",
        "classDef pending fill:#D3D3D3",
        "classDef blocked fill:#FFB6C1",
        "```",
    ])
    return "\n".join(lines)
