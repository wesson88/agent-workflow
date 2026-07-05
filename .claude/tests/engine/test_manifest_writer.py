"""
test_manifest_writer.py — P8.4 mark_status 单元测试

覆盖：
- 合法 status 更新 + markdown 前后段（H1/H2/Mermaid）保留
- 非法 status raise
- module_id 不存在 raise
- 文件不存在 raise
- 更新后 DAG 校验回跑
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.manifest_render import parse_manifest
from engine.manifest_writer import ManifestWriteError, mark_status


def _write_manifest(path: Path, nodes_yaml: str, extra_sections: str = "") -> None:
    content = (
        "---\n"
        "type: module-manifest\n"
        "project: demo\n"
        "---\n\n"
        "# 模块清单\n\n"
        "> 说明段（应保留）\n\n"
        "## 结构化（DAG）\n\n"
        "```yaml\n"
        f"{nodes_yaml}"
        "```\n\n"
        f"{extra_sections}"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


_NODES_YAML = """\
nodes:
  - {id: T01, role: backend, title: 登录 API, depends_on: [], status: pending, estimate_hours: 3}
  - {id: T02, role: backend, title: 验证码, depends_on: [T01], status: pending, estimate_hours: 2}
"""

_EXTRA = """## 拓扑

```mermaid
graph LR
  T01 --> T02
```

## 说明

尾部段落应保留。
"""


class TestMarkStatus:
    def test_status_updated(self, tmp_path: Path):
        p = tmp_path / "模块清单.md"
        _write_manifest(p, _NODES_YAML, _EXTRA)
        mark_status(p, "T01", "in_progress")
        nodes = parse_manifest(p)
        by_id = {n["id"]: n for n in nodes}
        assert by_id["T01"]["status"] == "in_progress"
        assert by_id["T02"]["status"] == "pending"

    def test_status_done_flow(self, tmp_path: Path):
        p = tmp_path / "模块清单.md"
        _write_manifest(p, _NODES_YAML)
        mark_status(p, "T01", "in_progress")
        mark_status(p, "T01", "done")
        nodes = parse_manifest(p)
        by_id = {n["id"]: n for n in nodes}
        assert by_id["T01"]["status"] == "done"

    def test_h1_and_h2_preserved(self, tmp_path: Path):
        p = tmp_path / "模块清单.md"
        _write_manifest(p, _NODES_YAML, _EXTRA)
        mark_status(p, "T02", "blocked")
        text = p.read_text(encoding="utf-8")
        assert "# 模块清单" in text
        assert "## 结构化（DAG）" in text
        assert "## 拓扑" in text
        assert "## 说明" in text
        assert "说明段（应保留）" in text
        assert "尾部段落应保留" in text
        # frontmatter 保留
        assert "type: module-manifest" in text

    def test_mermaid_section_preserved(self, tmp_path: Path):
        p = tmp_path / "模块清单.md"
        _write_manifest(p, _NODES_YAML, _EXTRA)
        mark_status(p, "T01", "in_progress")
        text = p.read_text(encoding="utf-8")
        assert "```mermaid" in text
        assert "T01 --> T02" in text

    def test_invalid_status_raises(self, tmp_path: Path):
        p = tmp_path / "模块清单.md"
        _write_manifest(p, _NODES_YAML)
        with pytest.raises(ManifestWriteError, match="非法 status"):
            mark_status(p, "T01", "unknown")

    def test_missing_module_id_raises(self, tmp_path: Path):
        p = tmp_path / "模块清单.md"
        _write_manifest(p, _NODES_YAML)
        with pytest.raises(ManifestWriteError, match="module_id='T99' 不在"):
            mark_status(p, "T99", "done")

    def test_missing_file_raises(self, tmp_path: Path):
        with pytest.raises(ManifestWriteError, match="读 manifest"):
            mark_status(tmp_path / "nope.md", "T01", "done")

    def test_status_variations_all_valid(self, tmp_path: Path):
        p = tmp_path / "模块清单.md"
        _write_manifest(p, _NODES_YAML)
        for s in ("pending", "in_progress", "done", "blocked"):
            mark_status(p, "T01", s)
            nodes = parse_manifest(p)
            by_id = {n["id"]: n for n in nodes}
            assert by_id["T01"]["status"] == s
