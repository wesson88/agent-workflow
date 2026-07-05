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

import atexit
import json
import os
import threading
from datetime import datetime, timezone

from .config import PROJECT_ROOT

# B6（P10.5）：batch buffer 阈值。
# 依据：拍脑袋初值 —— 50 条 entry × 平均 ~500B ≈ 25KB 内存驻留，一个 workflow
# 全跑（几十次 audit）通常一次 flush 就够，测试仍能实时观察前几十条即 flush。
# 待 P11 telemetry 采样后校准（如果实际 audit 频率显著低于 50/workflow 就下调）。
_BUFFER_FLUSH_THRESHOLD = 50


def utc_now() -> str:
    """UTC ISO-8601 时间戳（audit 事件 timestamp 字段用）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# B6：进程内 audit buffer + lock（thread-safe，避免并发 workflow / 子进程之间竞态）
_buffer: list[dict] = []
_buffer_lock = threading.Lock()
_atexit_registered = False


def _flush_locked() -> None:
    """caller 持锁调用；把 buffer 冲到 disk 后清空。"""
    global _buffer
    if not _buffer:
        return
    audit_path = PROJECT_ROOT / ".claude" / "audit.jsonl"
    try:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with open(audit_path, "a", encoding="utf-8") as f:
            for entry in _buffer:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — audit 失败绝不阻塞主链
        pass
    finally:
        _buffer = []


def flush() -> None:
    """公开 API：立即把 buffer 冲到 disk（测试 / 关键 checkpoint 用）。"""
    with _buffer_lock:
        _flush_locked()


def _ensure_atexit():
    """进程退出时 flush 剩余 buffer；同时为 subprocess 场景做保护
    （skill main.py 每次是独立进程，进程退出必 flush）。"""
    global _atexit_registered
    if _atexit_registered:
        return
    atexit.register(flush)
    _atexit_registered = True


def append_audit(entry: dict) -> None:
    """追加一条审计日志（B6：走 buffer，阈值到或进程退出时 flush）。

    - **不立即 fsync**：memory buffer 累积 `_BUFFER_FLUSH_THRESHOLD` 条或 atexit 触发才落盘
    - 主链路失败保护：flush 失败**不**抛异常（audit 是 side channel，
      不该阻塞主 workflow）
    - 单点入口：所有 engine + skills 层的 audit 事件都走这里；capability_executor
      的 vault markdown 审计跟本 jsonl 双写走各自路径（audit_writer.py）
    - **实时性权衡**：单次调用 audit 后立即读 audit.jsonl 未必见到；如需强制
      落盘调 `flush()`
    - **subprocess 隔离**：每个 skill main.py 是独立 Python 进程，`atexit` 保证
      进程退出前 buffer 全部落盘，跨进程语义等同 unbatched
    """
    _ensure_atexit()
    with _buffer_lock:
        _buffer.append(entry)
        if len(_buffer) >= _BUFFER_FLUSH_THRESHOLD:
            _flush_locked()


def _reset_for_test() -> None:
    """测试专用：清 buffer + 重置 atexit 状态。"""
    global _buffer, _atexit_registered
    with _buffer_lock:
        _buffer = []
    _atexit_registered = False


# 环境变量 `AGENT_AUDIT_NOBUFFER=1` 时关掉 batch（老行为，同步落盘）
# 场景：调试 / CI 需要实时观察 audit
if os.environ.get("AGENT_AUDIT_NOBUFFER") == "1":
    _BUFFER_FLUSH_THRESHOLD = 1
