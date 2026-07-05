"""
test_manifest_validator.py — P8.1 模块清单 DAG 校验单元测试

覆盖：
- validate_manifest_nodes：结构 / id / role / status / depends_on / DAG 无环
- ManifestValidationError fail-closed
- parse_manifest：文件缺失 / 无 yaml block / yaml 语法错 / DAG 校验 raise
- compute_ready_set：pending + 依赖全 done 的节点
- render_summary：counts / ready / blocked / estimate_hours
- render_mermaid：源码结构 + status 色
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.manifest_render import (
    compute_ready_set,
    parse_manifest,
    render_mermaid,
    render_summary,
)
from engine.manifest_validator import (
    ManifestValidationError,
    validate_manifest_nodes,
)


def _mk(id, role="backend", status="pending", depends_on=None, title=None):
    return {
        "id": id,
        "role": role,
        "title": title or f"任务 {id}",
        "depends_on": depends_on or [],
        "status": status,
        "estimate_hours": 2,
    }


# ── validate_manifest_nodes ─────────────────────────────────
class TestValidateManifestNodes:
    def test_valid_chain(self):
        nodes = [
            _mk("T01", status="done"),
            _mk("T02", depends_on=["T01"]),
        ]
        validate_manifest_nodes(nodes)

    def test_valid_parallel(self):
        nodes = [
            _mk("T01"),
            _mk("T02"),
            _mk("T03", depends_on=["T01", "T02"]),
        ]
        validate_manifest_nodes(nodes)

    def test_empty_list_raises(self):
        with pytest.raises(ManifestValidationError, match="nodes 为空"):
            validate_manifest_nodes([])

    def test_non_list_raises(self):
        with pytest.raises(ManifestValidationError, match="必须是 list"):
            validate_manifest_nodes("not a list")

    def test_missing_required_field_raises(self):
        with pytest.raises(ManifestValidationError, match="缺必填字段"):
            validate_manifest_nodes([{"id": "T01", "role": "backend"}])

    def test_duplicate_id_raises(self):
        nodes = [_mk("T01"), _mk("T01")]
        with pytest.raises(ManifestValidationError, match="id 重复"):
            validate_manifest_nodes(nodes)

    def test_invalid_role_raises(self):
        nodes = [_mk("T01", role="unknown")]
        with pytest.raises(ManifestValidationError, match="role='unknown' 非法"):
            validate_manifest_nodes(nodes)

    def test_invalid_status_raises(self):
        nodes = [_mk("T01", status="cancelled")]
        with pytest.raises(ManifestValidationError, match="status='cancelled' 非法"):
            validate_manifest_nodes(nodes)

    def test_depends_on_not_list_raises(self):
        node = _mk("T01")
        node["depends_on"] = "T02"
        with pytest.raises(ManifestValidationError, match="depends_on 必须是 list"):
            validate_manifest_nodes([node])

    def test_dangling_dep_raises(self):
        nodes = [_mk("T01", depends_on=["T99"])]
        with pytest.raises(
            ManifestValidationError, match="depends_on 'T99' 不在 nodes 里"
        ):
            validate_manifest_nodes(nodes)

    def test_self_dep_raises(self):
        nodes = [_mk("T01", depends_on=["T01"])]
        with pytest.raises(ManifestValidationError, match="自依赖"):
            validate_manifest_nodes(nodes)

    def test_simple_cycle_raises(self):
        nodes = [
            _mk("T01", depends_on=["T02"]),
            _mk("T02", depends_on=["T01"]),
        ]
        with pytest.raises(ManifestValidationError, match="依赖存在环"):
            validate_manifest_nodes(nodes)

    def test_triple_cycle_raises(self):
        nodes = [
            _mk("T01", depends_on=["T03"]),
            _mk("T02", depends_on=["T01"]),
            _mk("T03", depends_on=["T02"]),
        ]
        with pytest.raises(ManifestValidationError, match="依赖存在环"):
            validate_manifest_nodes(nodes)

    def test_id_stringified(self):
        """yaml 可能把 T01 解析为字符串，也可能 001 → int；统一 str 处理。"""
        # int 也应被接受（虽然实际 yaml 很难产生）
        node = {
            "id": 1,
            "role": "backend",
            "title": "任务",
            "depends_on": [],
            "status": "pending",
        }
        validate_manifest_nodes([node])


# ── compute_ready_set ───────────────────────────────────────
class TestComputeReadySet:
    def test_no_deps_pending(self):
        nodes = [_mk("T01"), _mk("T02")]
        ready = compute_ready_set(nodes)
        assert [n["id"] for n in ready] == ["T01", "T02"]

    def test_deps_all_done(self):
        nodes = [
            _mk("T01", status="done"),
            _mk("T02", status="done"),
            _mk("T03", depends_on=["T01", "T02"]),
        ]
        ready = compute_ready_set(nodes)
        assert [n["id"] for n in ready] == ["T03"]

    def test_partial_deps_done_not_ready(self):
        nodes = [
            _mk("T01", status="done"),
            _mk("T02", status="pending"),
            _mk("T03", depends_on=["T01", "T02"]),
        ]
        ready = compute_ready_set(nodes)
        # T03 有 T02 pending 未 done，不 ready；T02 无依赖 ready
        assert [n["id"] for n in ready] == ["T02"]

    def test_in_progress_not_ready(self):
        nodes = [_mk("T01", status="in_progress")]
        assert compute_ready_set(nodes) == []

    def test_done_not_ready(self):
        nodes = [_mk("T01", status="done")]
        assert compute_ready_set(nodes) == []


# ── render_summary ──────────────────────────────────────────
class TestRenderSummary:
    def test_counts_and_estimate(self):
        nodes = [
            _mk("T01", status="done"),
            _mk("T02", status="in_progress"),
            _mk("T03", status="pending"),
            _mk("T04", status="pending"),
        ]
        s = render_summary(nodes)
        assert s["counts"] == {"done": 1, "in_progress": 1, "pending": 2}
        assert s["total_estimate_hours"] == 8.0
        assert s["ready_ids"] == ["T01", "T02", "T03", "T04"][2:]  # T03/T04 pending 且无 deps

    def test_blocked_by_pending_upstream(self):
        nodes = [
            _mk("T01", status="pending"),
            _mk("T02", depends_on=["T01"]),
        ]
        s = render_summary(nodes)
        assert "T02" in s["blocked_ids"]
        assert s["ready_ids"] == ["T01"]


# ── render_mermaid ──────────────────────────────────────────
class TestRenderMermaid:
    def test_mermaid_source_shape(self):
        nodes = [
            _mk("T01", status="done", title="登录 API"),
            _mk("T02", depends_on=["T01"], title="验证码"),
        ]
        src = render_mermaid(nodes)
        assert src.startswith("```mermaid\ngraph LR")
        assert src.endswith("```")
        assert 'T01["T01 登录 API<br/>done"]:::done' in src
        assert 'T02["T02 验证码<br/>pending"]:::pending' in src
        assert "T01 --> T02" in src
        assert "classDef done fill:#90EE90" in src


# ── parse_manifest ──────────────────────────────────────────
_VALID_MANIFEST = """---
type: module-manifest
project: demo
---

# 模块清单

## 结构化（DAG 原始数据）

```yaml
nodes:
  - { id: T01, role: backend, title: 登录 API, depends_on: [], status: done, estimate_hours: 3 }
  - { id: T02, role: backend, title: 验证码, depends_on: [T01], status: pending, estimate_hours: 2 }
```

## 拓扑

（Mermaid 占位）
"""


class TestParseManifest:
    def test_parse_valid(self, tmp_path: Path):
        p = tmp_path / "模块清单.md"
        p.write_text(_VALID_MANIFEST, encoding="utf-8")
        nodes = parse_manifest(p)
        assert len(nodes) == 2
        assert nodes[0]["id"] == "T01"

    def test_missing_file_raises(self, tmp_path: Path):
        with pytest.raises(ManifestValidationError, match="不存在"):
            parse_manifest(tmp_path / "nope.md")

    def test_missing_yaml_block_raises(self, tmp_path: Path):
        p = tmp_path / "模块清单.md"
        p.write_text("# 模块清单\n\n没有 yaml block", encoding="utf-8")
        with pytest.raises(ManifestValidationError, match="未找到"):
            parse_manifest(p)

    def test_invalid_yaml_raises(self, tmp_path: Path):
        p = tmp_path / "模块清单.md"
        p.write_text(
            "# 模块清单\n\n## 结构化\n```yaml\nnodes: [{ id: T01, role: backend }]\n"
            "  invalid indent\n```",
            encoding="utf-8",
        )
        with pytest.raises(ManifestValidationError):
            parse_manifest(p)

    def test_missing_nodes_key_raises(self, tmp_path: Path):
        p = tmp_path / "模块清单.md"
        p.write_text(
            "# 模块清单\n\n## 结构化\n```yaml\nother: 123\n```",
            encoding="utf-8",
        )
        with pytest.raises(ManifestValidationError, match="缺 'nodes:' 键"):
            parse_manifest(p)

    def test_dag_error_bubbles_up(self, tmp_path: Path):
        """parse_manifest 应把 DAG 校验错误也 raise 出来。"""
        p = tmp_path / "模块清单.md"
        p.write_text(
            "# 模块清单\n\n## 结构化\n```yaml\n"
            "nodes:\n"
            "  - { id: T01, role: backend, title: A, depends_on: [T02], status: pending }\n"
            "  - { id: T02, role: backend, title: B, depends_on: [T01], status: pending }\n"
            "```",
            encoding="utf-8",
        )
        with pytest.raises(ManifestValidationError, match="依赖存在环"):
            parse_manifest(p)
