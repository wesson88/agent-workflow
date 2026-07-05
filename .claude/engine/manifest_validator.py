"""
engine/manifest_validator.py — 模块清单 DAG 校验（P8.1）

模块清单 schema（来源 [[模块清单与人机协同工作流-2026-07-02 §4.3]]）：

vault 路径：`10-项目/{project}/模块清单.md`
文档结构：
- H1 `# 模块清单`
- H2 `## 结构化（...）` 段内含一个 ```yaml ... ``` 块
  - 块顶层 dict 含 `nodes: [...]`
  - 每 node dict：id / user_story / role / title / depends_on / estimate_hours / status

本模块只校验 nodes 数组的 DAG 完整性（不解析 Markdown 结构；由调用方
`load_manifest_nodes` 传入 nodes list），fail-closed raise
`ManifestValidationError`。

校验维度：
1. id 唯一
2. 每 node 必填字段（id / role / title / depends_on / status）齐全
3. depends_on 引用的 id 必须在 nodes 里
4. role ∈ 允许集
5. status ∈ 允许集
6. DAG 无环（Kahn 拓扑排序）
"""

from __future__ import annotations

from typing import Iterable


class ManifestValidationError(ValueError):
    """模块清单 schema 或 DAG 完整性违反。fail-closed 由 P8 workflow 强制。"""
    pass


# 依据：来源 [[模块清单与人机协同工作流-2026-07-02 §4.3]] 示例列表
# 未来加 fullstack / infra 等 role 时同步扩展；越权 role 应先加成员再改此处
ALLOWED_ROLES = frozenset({"backend", "frontend"})

# 依据：§4.3 状态权威表 4 值 + blocked（依赖未满足或人工介入时用）
ALLOWED_STATUS = frozenset({"pending", "in_progress", "done", "blocked"})

# 每 node 必填字段（其余可选）
REQUIRED_NODE_FIELDS = ("id", "role", "title", "depends_on", "status")


def _err(msg: str) -> ManifestValidationError:
    return ManifestValidationError(msg)


def _stringify_id(v) -> str:
    """id 字段允许 str / int（yaml 可能解析为 int 如 T01 → 'T01'）；统一转 str。"""
    if v is None:
        raise _err("node id 为 None")
    return str(v).strip()


def validate_manifest_nodes(nodes: list[dict]) -> None:
    """校验 nodes 列表是合法 DAG。fail-closed raise `ManifestValidationError`。

    调用方：`manifest_render.parse_manifest` / P8 workflow 加载模块清单后。
    """
    if not isinstance(nodes, list):
        raise _err(f"nodes 必须是 list，实际：{type(nodes).__name__}")
    if not nodes:
        raise _err("nodes 为空（模块清单至少含 1 个 node）")

    # 1. 逐 node 结构校验
    seen_ids: set[str] = set()
    for i, node in enumerate(nodes):
        if not isinstance(node, dict):
            raise _err(f"node[{i}] 必须是 dict，实际：{type(node).__name__}")
        missing = [f for f in REQUIRED_NODE_FIELDS if f not in node]
        if missing:
            raise _err(f"node[{i}] 缺必填字段：{missing}")
        nid = _stringify_id(node.get("id"))
        if not nid:
            raise _err(f"node[{i}] id 为空")
        if nid in seen_ids:
            raise _err(f"node id 重复：'{nid}'")
        seen_ids.add(nid)
        role = str(node.get("role", "")).strip()
        if role not in ALLOWED_ROLES:
            raise _err(
                f"node '{nid}' role='{role}' 非法；允许：{sorted(ALLOWED_ROLES)}"
            )
        status = str(node.get("status", "")).strip()
        if status not in ALLOWED_STATUS:
            raise _err(
                f"node '{nid}' status='{status}' 非法；允许：{sorted(ALLOWED_STATUS)}"
            )
        deps = node.get("depends_on")
        if not isinstance(deps, list):
            raise _err(
                f"node '{nid}' depends_on 必须是 list，实际："
                f"{type(deps).__name__}"
            )

    # 2. depends_on 引用完整性
    for node in nodes:
        nid = _stringify_id(node["id"])
        for dep in node["depends_on"]:
            dep_id = _stringify_id(dep)
            if dep_id == nid:
                raise _err(f"node '{nid}' 自依赖")
            if dep_id not in seen_ids:
                raise _err(
                    f"node '{nid}' depends_on '{dep_id}' 不在 nodes 里"
                )

    # 3. DAG 无环（Kahn 拓扑排序）
    _assert_no_cycle(nodes)


def _assert_no_cycle(nodes: list[dict]) -> None:
    """Kahn 算法：按入度 0 节点依次剥离；剩余非零即存在环。"""
    id_set = {_stringify_id(n["id"]) for n in nodes}
    # in-edges: id → 依赖它的节点数（本节点的 depends_on 引用了其他节点，其他节点应等自己完成）
    # 但 Kahn 传统是从 depends_on = 0 开始剥离；这里 depends_on 就是入边集合
    remaining_in: dict[str, set[str]] = {
        _stringify_id(n["id"]): set(_stringify_id(d) for d in n["depends_on"])
        for n in nodes
    }

    ready = [nid for nid, deps in remaining_in.items() if not deps]
    processed: list[str] = []
    while ready:
        current = ready.pop()
        processed.append(current)
        for other, deps in remaining_in.items():
            if current in deps:
                deps.discard(current)
                if not deps and other not in processed and other not in ready:
                    ready.append(other)

    if len(processed) != len(id_set):
        cycle_nodes = [nid for nid in id_set if nid not in processed]
        raise _err(
            f"依赖存在环，未处理节点：{sorted(cycle_nodes)}"
        )
