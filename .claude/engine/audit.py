"""
engine/audit.py — 全局 audit 事件流的单点入口（P10.5 A1）。

背景：
- P9 前 `append_audit` 只在 `skills/common.py`；engine 模块（如 capability_executor
  的 audit_writer）想双写 jsonl 时被迫 `from skills.common import append_audit`
  —— 反向依赖破坏"engine 是基础层、skills 是应用层"的分层
- P10.5 A1 修法：抽 `append_audit` 到本模块；`capability_executor.audit_writer`
  改正向 `from engine.audit import append_audit`；`skills/common` 里的
  `append_audit` 改为 re-export 保持向后兼容（skill main.py 零改动）

事件 schema（约定，不做 fail-closed 校验，避免污染主链路）：
    {
        "timestamp": "2026-07-05T12:34:56Z"（UTC ISO-8601）,
        "type": "capability_invoke" | "token_audit" | "workflow_step" | ...,
        ...
    }
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .config import PROJECT_ROOT


def utc_now() -> str:
    """UTC ISO-8601 时间戳（audit 事件 timestamp 字段用）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def append_audit(entry: dict) -> None:
    """追加一条审计日志到 `.claude/audit.jsonl`。

    - 主链路失败保护：写盘失败**不**抛异常（audit 是 side channel，
      不该阻塞主 workflow）
    - 单点入口：所有 engine + skills 层的 audit 事件都走这里；capability_executor
      的 vault markdown 审计跟本 jsonl 双写走各自路径（audit_writer.py）
    """
    audit_path = PROJECT_ROOT / ".claude" / "audit.jsonl"
    try:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with open(audit_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — audit 失败绝不阻塞主链
        pass
