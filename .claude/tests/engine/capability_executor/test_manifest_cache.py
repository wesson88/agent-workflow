"""
test_manifest_cache.py — P10.5 A2 manifest_loader lru_cache 生效验证。

覆盖：
- 同 path 二次调用不 re-read（cache 命中）
- invalidate_cache() 后重读
- 不同 path 独立缓存
- capability_summary 层 cache
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


_SKILLS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "skills"
if str(_SKILLS_DIR) not in sys.path:
    sys.path.insert(0, str(_SKILLS_DIR))


_VALID = {
    "id": "test-cap/x",
    "version": "0.1.0",
    "source": "test",
    "runtime": {"type": "python", "entry": "x.py"},
    "triggers": ["t"],
    "inputs": [{"name": "u", "type": "url", "required": True}],
    "outputs": [{"name": "r", "type": "file", "path_pattern": "10-项目/{project}/x.json"}],
    "audit": {"log_to": "20-知识/能力注册表/test-cap/调用日志/{ts}-{project}.md"},
}


@pytest.fixture(autouse=True)
def _clear_caches_between_tests():
    from engine.capability_executor.manifest_loader import invalidate_cache
    from common import invalidate_capability_summary_cache
    invalidate_cache()
    invalidate_capability_summary_cache()
    yield
    invalidate_cache()
    invalidate_capability_summary_cache()


@pytest.fixture
def tmp_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from engine import config as engine_config
    from engine.capability_executor import manifest_loader as ml
    monkeypatch.setattr(engine_config, "VAULT_ROOT", tmp_path)
    monkeypatch.setattr(ml, "VAULT_ROOT", tmp_path)
    return tmp_path


def _write_manifest(vault: Path, root: str, manifest: dict) -> Path:
    p = vault / "20-知识" / "能力注册表" / root / "manifest.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return p


class TestManifestCache:
    def test_second_call_hits_cache(self, tmp_vault):
        """cache 命中：磁盘上改文件后二次读仍返回旧值。"""
        from engine.capability_executor.manifest_loader import load_manifest
        _write_manifest(tmp_vault, "test-cap", _VALID)

        m1 = load_manifest("test-cap/x")
        assert m1["version"] == "0.1.0"

        _write_manifest(tmp_vault, "test-cap", {**_VALID, "version": "0.2.0"})
        m2 = load_manifest("test-cap/x")
        assert m2["version"] == "0.1.0", "第二次应命中 cache"

    def test_invalidate_forces_reread(self, tmp_vault):
        from engine.capability_executor.manifest_loader import (
            invalidate_cache, load_manifest,
        )
        _write_manifest(tmp_vault, "test-cap", _VALID)
        _ = load_manifest("test-cap/x")

        _write_manifest(tmp_vault, "test-cap", {**_VALID, "version": "0.2.0"})
        invalidate_cache()
        m2 = load_manifest("test-cap/x")
        assert m2["version"] == "0.2.0"

    def test_different_paths_independent(self, tmp_vault):
        from engine.capability_executor.manifest_loader import load_manifest
        _write_manifest(tmp_vault, "cap-a", {**_VALID, "id": "cap-a/x"})
        _write_manifest(tmp_vault, "cap-b", {**_VALID, "id": "cap-b/x"})

        assert load_manifest("cap-a/x")["id"] == "cap-a/x"
        assert load_manifest("cap-b/x")["id"] == "cap-b/x"


class TestCapabilitySummaryCache:
    def test_second_render_hits_cache(self, tmp_vault):
        from common import _render_capability_summary_cached

        _write_manifest(tmp_vault, "test-cap", _VALID)

        s1 = _render_capability_summary_cached(
            ("[[test-cap/manifest]]",), "demo"
        )
        # 磁盘改文件 → cache 仍返回旧值（跟 manifest lru_cache 一起工作）
        _write_manifest(tmp_vault, "test-cap", {**_VALID, "version": "9.9.9"})
        s2 = _render_capability_summary_cached(
            ("[[test-cap/manifest]]",), "demo"
        )
        assert s1 == s2
        assert "v0.1.0" in s1

    def test_invalidate_forces_rerender(self, tmp_vault):
        from common import (
            _render_capability_summary_cached,
            invalidate_capability_summary_cache,
        )
        from engine.capability_executor.manifest_loader import invalidate_cache

        _write_manifest(tmp_vault, "test-cap", _VALID)
        s1 = _render_capability_summary_cached(
            ("[[test-cap/manifest]]",), "demo"
        )
        assert "v0.1.0" in s1

        _write_manifest(tmp_vault, "test-cap", {**_VALID, "version": "9.9.9"})
        invalidate_cache()
        invalidate_capability_summary_cache()
        s2 = _render_capability_summary_cached(
            ("[[test-cap/manifest]]",), "demo"
        )
        assert "v9.9.9" in s2
