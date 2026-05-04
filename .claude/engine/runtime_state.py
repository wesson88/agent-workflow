"""
engine/runtime_state.py — 角色运行时状态的独立持久化层

之前 status / last_run / consecutive_failures / error_count / last_output_path
直接写在角色笔记 frontmatter 里，每次 e2e 都会脏定义文件，commit 噪音大。

本模块把运行时状态拆到 `00-系统/.runtime-state/<role-name>.json`：
- 每个角色一个 JSON 文件，名字 = 角色 frontmatter 的 `role:` 字段值
- 目录被 .gitignore 排除，state 不进 git 历史
- 读写都是原子的，复用 obsidian_io 的 _atomic_replace_with_retry

API 与旧 state.py 对应字段一致，状态机语义在 state.py 层维护。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile

from .config import VAULT_ROOT


def runtime_state_dir() -> Path:
    return VAULT_ROOT / "00-系统" / ".runtime-state"


_DEFAULT_STATE = {
    "status": "idle",
    "last_run": None,
    "consecutive_failures": 0,
    "error_count": 0,
    "last_output_path": None,
}


def _state_path(role_name: str) -> Path:
    return runtime_state_dir() / f"{role_name}.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ── 读 ───────────────────────────────────────────────────
def load(role_name: str) -> dict:
    """读取角色运行时状态；文件不存在返回默认值（不创建文件）。"""
    p = _state_path(role_name)
    if not p.exists():
        return dict(_DEFAULT_STATE)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # 损坏的 state 文件：返回默认，不阻断流程
        return dict(_DEFAULT_STATE)
    # 合并默认值（防止旧 state 缺字段）
    merged = dict(_DEFAULT_STATE)
    merged.update(data)
    return merged


def load_all() -> dict[str, dict]:
    """枚举所有 .runtime-state/*.json，返回 {role_name: state}。"""
    d = runtime_state_dir()
    if not d.exists():
        return {}
    out: dict[str, dict] = {}
    for f in d.glob("*.json"):
        out[f.stem] = load(f.stem)
    return out


# ── 写（原子）────────────────────────────────────────────
def save(role_name: str, state: dict) -> None:
    """原子写入角色运行时状态。父目录自动创建。"""
    p = _state_path(role_name)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True)
    # 复用 obsidian_io 的重试逻辑（避免 Obsidian-git 锁文件）
    from .obsidian_io import _atomic_replace_with_retry
    with NamedTemporaryFile(
        "w",
        dir=p.parent,
        delete=False,
        encoding="utf-8",
        suffix=".tmp",
        newline="\n",
    ) as tf:
        tf.write(payload)
        tmp = tf.name
    _atomic_replace_with_retry(tmp, p)


def update(role_name: str, **patch) -> dict:
    """局部更新；返回更新后的完整 state。"""
    state = load(role_name)
    state.update(patch)
    save(role_name, state)
    return state


def reset(role_name: str) -> None:
    """清零（idle + 计数清零，保留 last_output_path）。"""
    state = load(role_name)
    state["status"] = "idle"
    state["consecutive_failures"] = 0
    state["error_count"] = 0
    save(role_name, state)
