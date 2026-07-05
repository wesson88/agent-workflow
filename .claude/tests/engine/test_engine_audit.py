"""
test_engine_audit.py — P10.5 A1 `engine.audit` 单点入口。

覆盖：
- append_audit 写入 .claude/audit.jsonl（相对 PROJECT_ROOT）
- 失败静默（audit 是 side channel，不阻塞主链）
- utc_now 格式（ISO-8601 UTC "Z" 后缀）
- skills.common.append_audit 是 engine.audit.append_audit 的 re-export（向后兼容）
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SKILLS_DIR = Path(__file__).resolve().parent.parent.parent / "skills"
sys.path.insert(0, str(_SKILLS_DIR))


class TestAppendAudit:
    def test_writes_entry_to_jsonl(self, tmp_path, monkeypatch):
        from engine import audit as audit_mod
        monkeypatch.setattr(audit_mod, "PROJECT_ROOT", tmp_path)

        audit_mod.append_audit({"type": "test", "value": 42, "utf": "中文"})

        dest = tmp_path / ".claude" / "audit.jsonl"
        assert dest.exists()
        line = dest.read_text(encoding="utf-8").strip()
        obj = json.loads(line)
        assert obj["type"] == "test"
        assert obj["value"] == 42
        assert obj["utf"] == "中文"

    def test_multiple_calls_append(self, tmp_path, monkeypatch):
        from engine import audit as audit_mod
        monkeypatch.setattr(audit_mod, "PROJECT_ROOT", tmp_path)

        audit_mod.append_audit({"n": 1})
        audit_mod.append_audit({"n": 2})
        audit_mod.append_audit({"n": 3})

        dest = tmp_path / ".claude" / "audit.jsonl"
        lines = dest.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 3
        assert [json.loads(l)["n"] for l in lines] == [1, 2, 3]

    def test_fails_silently_on_io_error(self, tmp_path, monkeypatch):
        """audit 是 side channel；PROJECT_ROOT 指向 read-only 目录时应静默失败不抛。"""
        from engine import audit as audit_mod
        # 指向一个不能创建子目录的路径（file 而不是 dir）
        blocked = tmp_path / "blocker.txt"
        blocked.write_text("x")
        monkeypatch.setattr(audit_mod, "PROJECT_ROOT", blocked / "no_such_subdir")

        # 不 raise
        audit_mod.append_audit({"type": "test"})


class TestUtcNow:
    def test_iso_format_with_z_suffix(self):
        from engine.audit import utc_now
        ts = utc_now()
        assert ts.endswith("Z")
        # 大致 20 字符：'2026-07-05T12:34:56Z'
        assert 19 <= len(ts) <= 25
        # 无时区 offset 尾巴 +00:00
        assert "+" not in ts


class TestSkillsCommonReExport:
    """向后兼容：skills.common.append_audit / utc_now 现在是 engine.audit 的 re-export。"""

    def test_common_append_audit_is_engine_audit(self):
        from common import append_audit as common_append_audit
        from engine.audit import append_audit as engine_append_audit
        assert common_append_audit is engine_append_audit

    def test_common_utc_now_is_engine_utc_now(self):
        from common import utc_now as common_utc_now
        from engine.audit import utc_now as engine_utc_now
        assert common_utc_now is engine_utc_now
