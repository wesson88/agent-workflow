"""
state.py — 角色状态聚合，状态机语义层

Phase 2b 起，运行时字段（status / last_run / consecutive_failures / error_count
/ last_output_path）实际存储在 `00-系统/.runtime-state/<role>.json`，
由 runtime_state.py 持久化；本文件提供状态机语义包装。

外部 API（兼容 Phase 2b 调用方）：
- get_role_status(role_name_or_alias) -> dict
- set_role_status(role_name_or_alias, **fields)
- role_is_blocked(role_name_or_alias) -> bool
- summarize_all_roles() -> list[dict]

状态机：
    idle ──> busy ──> success ──> idle
                 └─> failed ──> busy   （重试）
                            └─> idle   （人工/复盘 agent 重置）
                            └─> blocked
    blocked ──(仅由上级/复盘 agent 触发)──> idle
    monitoring ──> monitoring             （架构师等监控角色保持不变）
"""

from __future__ import annotations

import time
from typing import Any

from . import role_loader, runtime_state


ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "idle":       ("busy",),
    "busy":       ("success", "failed", "blocked"),
    "failed":     ("busy", "idle"),
    "blocked":    ("idle",),             # 允许超时自动恢复
    "success":    ("idle",),
    "monitoring": ("monitoring",),
}


def validate_transition(
    role_name: str,
    current_status: str,
    target_status: str,
) -> None:
    """纯函数：校验状态机转换合法性，非法时抛出 ValueError。

    单一职责：仅做校验，不读写任何状态存储。
    调用方 set_role_status 在 enforce_transition=True 时使用本函数。
    也可单独用于测试或 pre-flight 检查。
    """
    allowed = ALLOWED_TRANSITIONS.get(current_status, ())
    if target_status not in allowed and target_status != current_status:
        raise ValueError(
            f"非法状态转换：{role_name} {current_status} → {target_status}"
            f"（合法：{allowed}）"
        )


# ── 读 ───────────────────────────────────────────────────
def get_role_status(name_or_alias: str) -> dict[str, Any]:
    """返回 {status, last_run, consecutive_failures, error_count, last_output_path}。

    把 alias 解析为 frontmatter 的 role 字段值（中文名），
    再从 runtime_state 文件读取。
    """
    role = role_loader.load_role(name_or_alias)
    return runtime_state.load(role.name)


def role_is_blocked(name_or_alias: str) -> bool:
    return get_role_status(name_or_alias).get("status") == "blocked"


def summarize_all_roles() -> list[dict[str, Any]]:
    """汇总所有角色的状态快照（定义来自角色笔记，运行时来自 runtime_state）。"""
    out: list[dict[str, Any]] = []
    for r in role_loader.list_roles():
        st = runtime_state.load(r.name)
        out.append({
            "role": r.name,
            "model": r.model,
            "status": st.get("status"),
            "last_run": st.get("last_run"),
            "consecutive_failures": st.get("consecutive_failures"),
            "error_count": st.get("error_count"),
            "downstream": list(r.downstream),
        })
    return out


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
    max_blocked_seconds: float = 3600.0,
) -> None:
    """统一的状态写入入口。

    - status：目标状态。enforce_transition=True 时校验状态机合法性
    - increment_error / increment_consecutive_failures：原子地 +1
    - reset_counters：consecutive_failures 与 error_count 都清零
    - last_output_path：dev_backend / dev_frontend 写代码后的产出根路径
    - extra：兜底字典，额外要写入运行时状态的字段
    - max_blocked_seconds：blocked 状态超过此秒数自动恢复为 idle（默认 1 小时）
    """
    role = role_loader.load_role(name_or_alias)
    current = runtime_state.load(role.name)
    patch: dict[str, Any] = {}

    # blocked 超时自动恢复
    if current.get("status") == "blocked":
        blocked_since = current.get("blocked_since")
        if blocked_since and (time.time() - blocked_since) > max_blocked_seconds:
            print(
                f"[auto_recover] {role.name} blocked 超过 {max_blocked_seconds:.0f}s，自动恢复为 idle",
                flush=True,
            )
            patch["status"] = "idle"
            patch["blocked_since"] = None
            runtime_state.update(role.name, **patch)
            current = runtime_state.load(role.name)
            patch = {}

    if status is not None:
        if enforce_transition:
            validate_transition(role.name, current.get("status", "idle"), status)
        patch["status"] = status
        # 进入 blocked 时记录时间戳，退出时清除
        if status == "blocked":
            patch["blocked_since"] = time.time()
        elif status != "blocked":
            patch["blocked_since"] = None

    if reset_counters:
        patch["consecutive_failures"] = 0
        patch["error_count"] = 0
    if increment_consecutive_failures:
        patch["consecutive_failures"] = current.get("consecutive_failures", 0) + 1
    if increment_error:
        patch["error_count"] = current.get("error_count", 0) + 1
    if last_output_path is not None:
        patch["last_output_path"] = last_output_path

    patch["last_run"] = runtime_state.utc_now()
    if extra:
        patch.update(extra)

    runtime_state.update(role.name, **patch)
