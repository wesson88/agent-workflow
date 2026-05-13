"""
audit.py — 审计日志与时间工具（单一职责：审计记录）

职责：
- append_audit：追加一条 JSON 记录到 .claude/audit.jsonl
- utc_now：返回当前 UTC ISO 时间字符串

Phase 4 计划：迁移到 vault 复盘记录。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from engine.config import PROJECT_ROOT  # noqa: E402


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def append_audit(entry: dict) -> None:
    """追加一条审计日志到 .claude/audit.jsonl。"""
    audit_path = PROJECT_ROOT / ".claude" / "audit.jsonl"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with open(audit_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
