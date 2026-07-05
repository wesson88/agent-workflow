"""
test_audit_writer.py — audit_writer 写盘 + input_hash 稳定性 + 双写行为。

覆盖：
- markdown 落到正确路径（`{ts}` / `{project}` 展开）
- frontmatter yaml 可解析
- 必填字段全出现
- input_hash 对同 inputs 稳定
- append_audit（.claude/audit.jsonl 双写）不失败即算 pass
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from engine.capability_executor.audit_writer import _input_hash, write_audit
from engine.capability_executor.base import ExecutorResult


_MANIFEST = {
    "id": "web-scraper/crawl",
    "version": "0.1.0",
    "audit": {
        "log_to": "20-知识/能力注册表/web-scraper/调用日志/{ts}-{project}.md",
    },
}


@pytest.fixture
def tmp_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from engine import config as engine_config
    from engine.capability_executor import audit_writer as aw
    monkeypatch.setattr(engine_config, "VAULT_ROOT", tmp_path)
    monkeypatch.setattr(aw, "VAULT_ROOT", tmp_path)
    return tmp_path


class TestWriteAudit:
    def test_writes_markdown_with_frontmatter(self, tmp_vault):
        result = ExecutorResult(
            exit_code=0, duration_s=1.234, stdout="ok", stderr="",
            artifact_paths=[Path("/some/artifact.json")],
        )
        dest = write_audit(
            _MANIFEST, "demo", {"url": "https://example.com"}, result,
            token_consumed=500,
        )
        assert dest.exists()
        content = dest.read_text(encoding="utf-8")

        # frontmatter yaml 可解析
        _, fm_raw, _ = content.split("---", 2)
        fm = yaml.safe_load(fm_raw)
        assert fm["capability"] == "web-scraper/crawl"
        assert fm["version"] == "0.1.0"
        assert fm["project"] == "demo"
        assert fm["exit_code"] == 0
        assert fm["duration_s"] == 1.234
        assert fm["token_consumed"] == 500

        # body 含必填段
        assert "## 输入" in content
        assert "## 输出" in content
        assert "## 性能" in content
        assert "https://example.com" in content

    def test_ts_and_project_expanded_in_filename(self, tmp_vault):
        result = ExecutorResult(exit_code=0, duration_s=0.1, stdout="", stderr="")
        dest = write_audit(_MANIFEST, "demo", {"k": "v"}, result)
        # 文件名符合 {ts}-{project}.md 格式
        assert dest.name.endswith("-demo.md")
        # 落到规定目录
        assert "调用日志" in str(dest)

    def test_error_section_when_nonzero(self, tmp_vault):
        result = ExecutorResult(
            exit_code=1, duration_s=0.5, stdout="", stderr="died",
            error="exit_code=1: died",
        )
        dest = write_audit(_MANIFEST, "demo", {}, result)
        body = dest.read_text(encoding="utf-8")
        assert "## 错误" in body
        assert "exit_code=1: died" in body


class TestInputHash:
    def test_same_inputs_same_hash(self):
        h1 = _input_hash({"a": 1, "b": "x"})
        h2 = _input_hash({"a": 1, "b": "x"})
        assert h1 == h2

    def test_different_inputs_different_hash(self):
        h1 = _input_hash({"a": 1})
        h2 = _input_hash({"a": 2})
        assert h1 != h2

    def test_key_order_independent(self):
        h1 = _input_hash({"a": 1, "b": 2})
        h2 = _input_hash({"b": 2, "a": 1})
        assert h1 == h2

    def test_hash_is_sha256_hex(self):
        h = _input_hash({"x": "y"})
        assert len(h) == 64
        int(h, 16)  # hex 可解析
