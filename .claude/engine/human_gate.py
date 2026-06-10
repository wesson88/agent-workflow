"""
engine/human_gate.py — 人工介入卡点机制（Pending Decision Model）

源自 vault `20-知识/项目记录/元角色与人工介入机制.md` §6-§10 §11。

两种介入统一为 Pending Decision：
- **主动介入**（mode=active）：用户先发起（pause/intervene/reroute/...），type=human_intervention
- **被动介入**（mode=passive）：系统检测到条件自动暂停（gate=module_selection/brainstorm_readiness/
  prd_open_questions/arch_decision/...），type=human_gate

落盘路径：`10-项目/{project}/.workflow/human_gates/{gate_id}.json`

run_chain.py 主流程入口扫 `has_pending(project)` → True 则 raise SystemExit 提示用户先解决。
LangGraph interrupt 集成 HOLD 到 Phase B bridge 部署之后，schema 保持向后兼容。

外部 API：
- HumanGate dataclass
- emit_gate(project, type, mode, reason, ...) → HumanGate    # 角色 / engine 入口
- resolve_gate(project, gate_id, action, ...) → HumanGate    # CLI 入口
- list_gates(project, status=None) → list[HumanGate]
- has_pending(project) → bool
- load_gate(project, gate_id) → HumanGate
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal

from .config import project_dir


# ── 类型 ────────────────────────────────────────────────
GateType = Literal["human_intervention", "human_gate"]
GateMode = Literal["active", "passive"]
GateStatus = Literal["pending", "resolved", "expired", "cancelled"]

# 已知 resolution action（参源文档 §10）
RESOLUTION_ACTIONS = (
    "set_state",        # 修改 workflow state 后继续
    "restart_node",     # 用新上下文重跑当前节点
    "skip_node",        # 标记当前节点 skipped 并进入下一节点
    "reroute",          # 切换到指定节点或分支
    "abort",            # 终止 workflow
    "append_context",   # 用户补充写入上下文并继续
    "approve",          # gate 通过并继续
    "reject",           # 产物被拒绝并回到上游节点
)


@dataclass
class HumanGate:
    """统一 Pending Decision（主动 + 被动）。"""
    id: str
    project: str
    type: GateType
    mode: GateMode
    status: GateStatus
    reason: str
    created_at: str
    # 可选字段
    node: str | None = None                          # workflow 节点
    gate: str | None = None                          # 被动介入 gate 名（如 module_selection）
    context_refs: list[str] = field(default_factory=list)
    options: list[dict] = field(default_factory=list)  # [{id, label, effect}]
    recommended_option: str | None = None
    suggested_actions: list[str] = field(default_factory=list)
    user_response: str | None = None
    resolution: dict | None = None                   # {action, patch?, target_node?, ...}
    resolved_at: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "HumanGate":
        # 过滤未知字段（前向兼容 schema 演进）
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in known})


# ── 路径与时间 ───────────────────────────────────────────
GATES_SUBDIR = ".workflow/human_gates"


def gates_dir(project: str) -> Path:
    return project_dir(project) / GATES_SUBDIR


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _today_stamp() -> str:
    return datetime.now().strftime("%Y%m%d")


# ── ID 生成 ──────────────────────────────────────────────
def new_gate_id(project: str) -> str:
    """gate-{YYYYMMDD}-{nnn}，当日序号自动递增（扫目录得最大值 + 1）。"""
    stamp = _today_stamp()
    d = gates_dir(project)
    if not d.is_dir():
        return f"gate-{stamp}-001"
    prefix = f"gate-{stamp}-"
    max_n = 0
    for f in d.iterdir():
        name = f.name
        if name.startswith(prefix) and name.endswith(".json"):
            try:
                n = int(name[len(prefix):-5])
                max_n = max(max_n, n)
            except ValueError:
                continue
    return f"gate-{stamp}-{max_n + 1:03d}"


# ── 加载 / 保存 ──────────────────────────────────────────
def _gate_path(project: str, gate_id: str) -> Path:
    return gates_dir(project) / f"{gate_id}.json"


def load_gate(project: str, gate_id: str) -> HumanGate:
    p = _gate_path(project, gate_id)
    if not p.is_file():
        raise FileNotFoundError(
            f"human_gate 不存在：{p}\n"
            f"用 `python .claude/engine/cli_human_gate.py --project {project} list` 列出所有 gate。"
        )
    return HumanGate.from_dict(json.loads(p.read_text(encoding="utf-8")))


def save_gate(gate: HumanGate) -> Path:
    """原子写入（NamedTemporaryFile + os.replace）。父目录自动创建。"""
    d = gates_dir(gate.project)
    d.mkdir(parents=True, exist_ok=True)
    dest = d / f"{gate.id}.json"
    content = json.dumps(gate.to_dict(), ensure_ascii=False, indent=2) + "\n"
    with NamedTemporaryFile(
        "w", dir=d, delete=False, encoding="utf-8",
        suffix=".tmp", newline="\n",
    ) as tf:
        tf.write(content)
        tmp = tf.name
    # Windows 偶发文件锁，3 次指数退避（参 obsidian_io._atomic_replace_with_retry）
    for attempt in range(3):
        try:
            os.replace(tmp, dest)
            return dest
        except PermissionError:
            if attempt == 2:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            time.sleep(0.3 * (2 ** attempt))
    return dest


def list_gates(project: str, *, status: GateStatus | None = None) -> list[HumanGate]:
    d = gates_dir(project)
    if not d.is_dir():
        return []
    out: list[HumanGate] = []
    for f in sorted(d.glob("gate-*.json")):
        try:
            g = HumanGate.from_dict(json.loads(f.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if status is None or g.status == status:
            out.append(g)
    return out


def has_pending(project: str) -> bool:
    """主流程入口快速判断是否有未解决的 gate。"""
    return len(list_gates(project, status="pending")) > 0


# ── 创建 ────────────────────────────────────────────────
def emit_gate(
    *,
    project: str,
    type: GateType,
    mode: GateMode,
    reason: str,
    node: str | None = None,
    gate: str | None = None,
    context_refs: list[str] | None = None,
    options: list[dict] | None = None,
    recommended_option: str | None = None,
    suggested_actions: list[str] | None = None,
) -> HumanGate:
    """角色 / engine 入口：生成一条 pending gate 落盘。"""
    g = HumanGate(
        id=new_gate_id(project),
        project=project,
        type=type,
        mode=mode,
        status="pending",
        reason=reason,
        created_at=_utc_now_iso(),
        node=node,
        gate=gate,
        context_refs=list(context_refs or []),
        options=list(options or []),
        recommended_option=recommended_option,
        suggested_actions=list(suggested_actions or []),
    )
    save_gate(g)
    return g


# ── 解决 ────────────────────────────────────────────────
def resolve_gate(
    *,
    project: str,
    gate_id: str,
    action: str,
    user_response: str | None = None,
    patch: dict | None = None,
    target_node: str | None = None,
) -> HumanGate:
    """CLI 入口：解决一条 pending gate。"""
    if action not in RESOLUTION_ACTIONS:
        raise ValueError(
            f"未知 resolution action：'{action}'。"
            f"已知：{RESOLUTION_ACTIONS}"
        )
    g = load_gate(project, gate_id)
    if g.status != "pending":
        raise ValueError(
            f"gate '{gate_id}' 当前 status={g.status}，"
            f"只能解决 status=pending 的 gate。"
        )
    resolution: dict = {"action": action}
    if patch is not None:
        resolution["patch"] = patch
    if target_node is not None:
        resolution["target_node"] = target_node
    g.status = "resolved"
    g.user_response = user_response
    g.resolution = resolution
    g.resolved_at = _utc_now_iso()
    save_gate(g)
    return g
