"""
state.py — 角色状态聚合，替代旧的 status.json。

每个角色的状态字段（status / last_run / consecutive_failures / error_count）
都直接存在角色笔记的 frontmatter 中。本模块提供统一读写接口，并在写入后
失效 role_loader 的缓存以保证下次 load_role 拿到最新值。

状态机（沿用旧版语义）：
    idle ──> busy ──> success ──> idle
                 └─> failed ──> busy   （重试）
                            └─> idle   （人工/复盘 agent 重置）
                            └─> blocked
    blocked ──(仅由上级/复盘 agent 触发)──> idle
    monitoring ──> monitoring             （架构师等监控角色保持不变）
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from . import role_loader
from .obsidian_io import update_frontmatter


ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "idle":       ("busy",),
    "busy":       ("success", "failed", "blocked"),
    "failed":     ("busy", "idle"),
    "blocked":    (),                  # blocked 只能由外部介入
    "success":    ("idle",),
    "monitoring": ("monitoring",),
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ── 读 ───────────────────────────────────────────────────
def get_role_status(name_or_alias: str) -> dict[str, Any]:
    """返回 {status, last_run, consecutive_failures, error_count}。"""
    role = role_loader.load_role(name_or_alias)
    return {
        "status": role.status,
        "last_run": role.last_run,
        "consecutive_failures": role.consecutive_failures,
        "error_count": role.error_count,
    }


def role_is_blocked(name_or_alias: str) -> bool:
    return get_role_status(name_or_alias)["status"] == "blocked"


def summarize_all_roles() -> list[dict[str, Any]]:
    """汇总所有角色的状态快照，方便日志/CLI 输出。"""
    return [
        {
            "role": r.name,
            "status": r.status,
            "last_run": r.last_run,
            "consecutive_failures": r.consecutive_failures,
            "error_count": r.error_count,
            "downstream": list(r.downstream),
        }
        for r in role_loader.list_roles()
    ]


# ── 写 ───────────────────────────────────────────────────
def set_role_status(
    name_or_alias: str,
    *,
    status: str | None = None,
    enforce_transition: bool = True,
    increment_error: bool = False,
    increment_consecutive_failures: bool = False,
    reset_counters: bool = False,
    last_output_path: str | None = None,
    extra: dict | None = None,
) -> None:
    """统一的状态写入入口。

    - status：目标状态。若 enforce_transition 为 True，从当前状态转换必须合法
    - increment_error / increment_consecutive_failures：原子地 +1
    - reset_counters：把 consecutive_failures 和 error_count 都清零
      （用于 success → idle）
    - last_output_path：可选，用于 dev_backend / dev_frontend 写代码后回填
    - extra：兜底字典，额外要写入 frontmatter 的字段（如 last_patch_timestamp）
    """
    role = role_loader.load_role(name_or_alias)
    updates: dict[str, Any] = {}

    if status is not None:
        if enforce_transition:
            allowed = ALLOWED_TRANSITIONS.get(role.status, ())
            if status not in allowed and status != role.status:
                raise ValueError(
                    f"非法状态转换：{role.name} {role.status} → {status}"
                    f"（合法：{allowed}）"
                )
        updates["status"] = status

    if reset_counters:
        updates["consecutive_failures"] = 0
        updates["error_count"] = 0
    if increment_consecutive_failures:
        updates["consecutive_failures"] = role.consecutive_failures + 1
    if increment_error:
        updates["error_count"] = role.error_count + 1
    if last_output_path is not None:
        updates["last_output_path"] = last_output_path

    updates["last_run"] = _utc_now()
    if extra:
        updates.update(extra)

    update_frontmatter(role.note_path, updates)
    role_loader.invalidate_cache()
