"""
graph/brainstorm.py — 多轮脑暴 orchestrator subgraph (T2.6)

模型：
- 3 角色固定顺序：创意发散者 → 创意质询者 → 创意记录员（每轮）
- 每轮末读 `brainstorm_readiness.json` 的 `decision` 字段路由：
    - ready_for_prd      → finish_ready（终止，进 PM）
    - continue_discussion → 下一轮（若未到 max_rounds）
    - ask_user           → finish_ask_user（scribe 已 emit gate）
    - stop_low_value     → finish_stop（终止）
- 跨调用持久化：`10-项目/{project}/.workflow/brainstorm_round_state.json`
- 上下文预算监控：下轮估算 input > context_warn_tokens → stderr WARN + audit.jsonl

T2.6 不上 LangGraph interrupt（HOLD 至 Phase B 之后）；用文件落盘 + has_pending
等效 "暂停-恢复"：ask_user 时退出，用户 CLI resolve gate 后再跑同条命令，
init 节点读 round_state.json 知道从 R(N+1) 续跑。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from operator import add
from pathlib import Path
from typing import Annotated, TypedDict

from langgraph.graph import StateGraph, START, END

from ..config import PROJECT_ROOT, project_dir
from ..human_gate import has_pending, list_gates
from ..workflow import role_to_skill_dir


# ── 常量 ────────────────────────────────────────────────
ROUND_STATE_REL = ".workflow/brainstorm_round_state.json"
READINESS_REL = "brainstorm_readiness.json"

# 重试/超时/永久错误码语义见 nodes._execute_single（2026-07-18 去重后单一来源）


# ── State ───────────────────────────────────────────────
class BrainstormState(TypedDict, total=False):
    # 入口（父图传入）
    project: str
    task: str
    loop_name: str

    # 配置
    roles: tuple[str, str, str]              # (发散者, 质询者, 记录员)
    max_rounds: int
    audit_rounds: tuple[int, ...]
    readiness_threshold: int
    context_warn_tokens: int

    # 累积
    current_round: int                       # 即将跑的轮次（_node_init 设置）
    round_history: Annotated[list[dict], add]
    last_decision: str | None
    last_readiness: dict | None
    pending_gate_id: str | None
    halted: bool
    finished: bool
    finish_reason: str | None


# ── 路径助手 ────────────────────────────────────────────
def _round_state_path(project: str) -> Path:
    return project_dir(project) / ROUND_STATE_REL


def _readiness_path(project: str) -> Path:
    return project_dir(project) / READINESS_REL


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ── round_state.json 读写 ───────────────────────────────
def load_round_state(project: str) -> dict | None:
    """读 round_state.json；不存在返回 None。"""
    p = _round_state_path(project)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"[brainstorm] ⚠️ round_state.json 损坏（{e}），从 R1 重新开始", file=sys.stderr)
        return None


def save_round_state(project: str, state_data: dict) -> Path:
    """落盘 round_state.json（原子写）。父目录自动创建。"""
    p = _round_state_path(project)
    p.parent.mkdir(parents=True, exist_ok=True)
    state_data = {**state_data, "updated_at": _utc_now_iso()}
    state_data.setdefault("created_at", state_data["updated_at"])
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(state_data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, p)
    return p


# ── subprocess 调用 thin wrapper ────────────────────────
def _execute_brainstorm_role(
    role_name: str,
    task: str,
    project: str,
    round_num: int,
) -> int:
    """调一个脑暴角色，传 --round。

    2026-07-18 评审去重：重试/超时/永久错误码语义全部委托
    nodes._execute_single（原为逐行复制，改一处要同步改两份）。
    """
    from .nodes import _execute_single

    skill_dir = role_to_skill_dir(role_name)
    main_py = PROJECT_ROOT / ".claude" / "skills" / skill_dir / "main.py"
    env = os.environ.copy()
    env["PROJECT"] = project
    env["TASK"] = task
    return _execute_single(
        main_py, task, project, env,
        extra_args=["--round", str(round_num)],
        log_prefix="brainstorm",
    )


# ── 节点 ────────────────────────────────────────────────
def _node_init(state: BrainstormState) -> dict:
    """决定起始轮 + has_pending 检测。

    优先级：① has_pending → halted=True，等用户解决 ② round_state.json 存在 →
    续跑（current_round = saved + 1，除非 saved.decision in 终态） ③ 全新 → R1。
    """
    project = state["project"]
    loop_name = state.get("loop_name", "创意脑暴")
    print(f"\n{'─' * 60}")
    print(f"🧠 脑暴 orchestrator 启动：{loop_name}")
    print(f"  项目：{project}")
    print(f"  max_rounds：{state['max_rounds']}  audit_rounds：{state['audit_rounds']}")
    print(f"{'─' * 60}\n")

    # ① has_pending：scribe 已 emit gate 未 resolve
    if has_pending(project):
        pending = list_gates(project, status="pending")
        pending_ids = [g.id for g in pending]
        print(
            f"[brainstorm] 项目 '{project}' 有 {len(pending)} 个 pending human_gate，"
            f"不能启动新一轮：{pending_ids}\n"
            f"  解决：python .claude/engine/cli_human_gate.py --project {project} "
            f"resolve --id <gate-id> --action approve --response \"...\"",
            file=sys.stderr,
        )
        return {
            "halted": True,
            "finished": True,
            "finish_reason": "pending_gate",
            "pending_gate_id": pending_ids[0] if pending_ids else None,
        }

    # ② round_state.json：续跑
    saved = load_round_state(project)
    if saved and not saved.get("finished"):
        saved_decision = saved.get("last_decision")
        # 续跑只对 continue / ask_user_resolved 有效
        if saved_decision in ("continue_discussion", "ask_user"):
            next_round = int(saved.get("current_round", 0)) + 1
            print(
                f"[brainstorm] 续跑：上次结束于 R{saved.get('current_round')} "
                f"(decision={saved_decision})，从 R{next_round} 继续",
                flush=True,
            )
            return {
                "current_round": next_round,
                "round_history": list(saved.get("round_history", [])),
                "last_decision": saved_decision,
                "last_readiness": saved.get("last_readiness"),
            }

    # ③ 全新
    print(f"[brainstorm] 从 R{state.get('start_round', 1)} 开始", flush=True)
    return {"current_round": int(state.get("start_round", 1))}


def _node_run_round(state: BrainstormState) -> dict:
    """跑一轮 3 角色：发散 → 质询 → 记录。

    任一角色 rc != 0 → halted=True，路由到 finish_halted。
    """
    project = state["project"]
    task = state.get("task", "")
    round_num = int(state["current_round"])
    diverger, challenger, scribe = state["roles"]
    audit_rounds = state.get("audit_rounds", ())
    is_audit = round_num in audit_rounds

    print(f"\n{'=' * 60}")
    print(
        f"🌀 第 R{round_num} 轮（max={state['max_rounds']}"
        f"{'，R3/R6 审计模式' if is_audit else ''}）"
    )
    print(f"{'=' * 60}")

    for role in (diverger, challenger, scribe):
        print(f"\n▶ R{round_num} {role}", flush=True)
        rc = _execute_brainstorm_role(role, task, project, round_num)
        if rc != 0:
            print(f"\n❌ R{round_num} {role} 失败（exit={rc}），脑暴 orchestrator 中断")
            return {
                "halted": True,
                "finish_reason": "role_failed",
            }
        print(f"✅ R{round_num} {role} 完成")

    return {}


def _node_evaluate(state: BrainstormState) -> dict:
    """读 readiness.json → 设 last_decision + 写 round_state.json + 预算监控。"""
    project = state["project"]
    round_num = int(state["current_round"])
    readiness_p = _readiness_path(project)

    if not readiness_p.is_file():
        print(
            f"[brainstorm] ⚠️ readiness JSON 不存在：{readiness_p}，"
            f"按 halted 处理",
            file=sys.stderr,
        )
        return {"halted": True, "finish_reason": "readiness_missing"}

    try:
        readiness = json.loads(readiness_p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(
            f"[brainstorm] ⚠️ readiness JSON 解析失败：{e}，按 halted 处理",
            file=sys.stderr,
        )
        return {"halted": True, "finish_reason": "readiness_parse_failed"}

    decision = readiness.get("decision")
    prd_readiness = readiness.get("prd_readiness")
    ready_for_prd = readiness.get("ready_for_prd")

    print(
        f"\n📋 R{round_num} 决策："
        f"decision={decision} / prd_readiness={prd_readiness} / "
        f"ready_for_prd={ready_for_prd}",
        flush=True,
    )

    round_entry = {
        "round": round_num,
        "decision": decision,
        "prd_readiness": prd_readiness,
        "ready_for_prd": ready_for_prd,
    }

    # 关联 emitted gate（如果有）
    pending_gate_id: str | None = None
    if decision == "ask_user":
        pending = list_gates(project, status="pending")
        # scribe emit 的最后一条 brainstorm_* gate
        bs_gates = [g for g in pending if (g.gate or "").startswith("brainstorm_")]
        if bs_gates:
            pending_gate_id = bs_gates[-1].id

    # 预算监控：估算下轮 scribe input tokens（仅当 continue 才有意义，但都监控）
    _monitor_next_round_budget(
        project=project,
        next_round=round_num + 1,
        warn_threshold=int(state.get("context_warn_tokens", 30000)),
    )

    # 落盘 round_state.json
    history = list(state.get("round_history", [])) + [round_entry]
    state_data = {
        "loop_name": state.get("loop_name", "创意脑暴"),
        "current_round": round_num,
        "max_rounds": state["max_rounds"],
        "last_decision": decision,
        "last_readiness": readiness,
        "round_history": history,
        "pending_gate_id": pending_gate_id,
        "finished": False,
        "finish_reason": None,
    }
    save_round_state(project, state_data)

    return {
        "last_decision": decision,
        "last_readiness": readiness,
        "pending_gate_id": pending_gate_id,
        "round_history": [round_entry],
    }


# ── 预算监控（只警告不裁剪）─────────────────────────
def _monitor_next_round_budget(
    *, project: str, next_round: int, warn_threshold: int,
) -> None:
    """估算下轮 scribe input tokens，超阈值 → stderr WARN + audit.jsonl。

    估算口径：scribe 默认会读 idea.md + 创意发散-R{next-1}.md + 创意质询-R{next-1}.md
    + 产品创意原型.md + rolling_brief.md + brainstorm_readiness.json。
    """
    try:
        from ..token_counter import count_tokens
        from ..llm import _append_token_audit  # 复用 audit 写入
    except ImportError as e:
        print(f"[brainstorm] ⚠️ 预算监控降级（{e}）", file=sys.stderr)
        return

    pdir = project_dir(project)
    prev = next_round - 1
    candidates = [
        pdir / "inputs" / "idea.md",
        pdir / "脑暴" / f"创意发散-R{prev}.md",
        pdir / "脑暴" / f"创意质询-R{prev}.md",
        pdir / "产品创意原型.md",
        pdir / "脑暴" / "rolling_brief.md",
        pdir / "brainstorm_readiness.json",
    ]
    total = 0
    for f in candidates:
        if f.is_file():
            try:
                txt = f.read_text(encoding="utf-8")
                # 用 cl100k_base 估算（与 scribe 角色 model claude-opus-4-7 对应）
                total += count_tokens(txt, "claude-opus-4-7")
            except (OSError, Exception):
                continue

    if total > warn_threshold:
        print(
            f"[brainstorm] ⚠️ 下轮 R{next_round} input 估算 {total} tokens > "
            f"{warn_threshold} 警戒线；脑暴历史累积，建议手动检查是否需要压缩 "
            f"rolling_brief 或终止脑暴",
            file=sys.stderr,
        )
        try:
            _append_token_audit(
                "warn", "brainstorm_context_oversized",
                {
                    "project": project,
                    "next_round": next_round,
                    "estimated_input_tokens": total,
                    "threshold": warn_threshold,
                    "model": "claude-opus-4-7",
                },
            )
        except Exception:
            pass


# ── 终止节点 ────────────────────────────────────────────
def _make_finish_node(reason: str):
    def node(state: BrainstormState) -> dict:
        project = state["project"]
        round_num = int(state.get("current_round", 0))
        emoji = {
            "ready_for_prd": "🎯",
            "ask_user": "❓",
            "stop_low_value": "🛑",
            "max_rounds": "⏹️",
            "halted": "💥",
            "pending_gate": "⏸️",
        }.get(reason, "🏁")
        msg = {
            "ready_for_prd": f"PRD readiness 达标，进入 PM 工作流",
            "ask_user": f"等待用户答复（gate={state.get('pending_gate_id')}）；"
                       f"resolve 后再跑同条命令自动从 R{round_num + 1} 继续",
            "stop_low_value": f"决策 stop_low_value，脑暴终止",
            "max_rounds": f"已跑 {round_num} 轮 ≥ max_rounds={state.get('max_rounds')}，强制停止",
            "halted": f"链路中断（角色失败 / readiness 异常）",
            "pending_gate": f"启动前发现 pending gate，本次未跑任何轮次",
        }.get(reason, "脑暴结束")

        print(f"\n{'─' * 60}")
        print(f"{emoji}  脑暴 orchestrator 结束：{reason}")
        print(f"  R{round_num} · {msg}")
        print(f"{'─' * 60}\n")

        # 更新 round_state.json 标 finished
        saved = load_round_state(project) or {}
        saved.update({
            "loop_name": state.get("loop_name", saved.get("loop_name", "创意脑暴")),
            "finished": True,
            "finish_reason": reason,
            "current_round": round_num,
            "max_rounds": state.get("max_rounds", saved.get("max_rounds")),
            "last_decision": state.get("last_decision", saved.get("last_decision")),
            "pending_gate_id": state.get("pending_gate_id", saved.get("pending_gate_id")),
        })
        save_round_state(project, saved)

        return {"finished": True, "finish_reason": reason}

    node.__name__ = f"finish_{reason}"
    return node


# ── 条件路由 ────────────────────────────────────────────
def _route_after_init(state: BrainstormState) -> str:
    if state.get("halted"):
        return "finish_halted"
    if state.get("finished"):
        # _node_init 已经标 pending_gate
        return "finish_pending_gate"
    return "run_round"


def _route_after_run(state: BrainstormState) -> str:
    if state.get("halted"):
        return "finish_halted"
    return "evaluate"


def _route_after_evaluate(state: BrainstormState) -> str:
    if state.get("halted"):
        return "finish_halted"
    decision = state.get("last_decision")
    if decision == "ready_for_prd":
        return "finish_ready_for_prd"
    if decision == "ask_user":
        return "finish_ask_user"
    if decision == "stop_low_value":
        return "finish_stop_low_value"
    # continue_discussion：检查 max_rounds
    if int(state.get("current_round", 0)) >= int(state["max_rounds"]):
        return "finish_max_rounds"
    return "next_round"


def _node_next_round(state: BrainstormState) -> dict:
    """承上：current_round + 1，不做其他事。"""
    return {"current_round": int(state["current_round"]) + 1}


# ── 构造 subgraph ────────────────────────────────────────
def build_brainstorm_graph():
    """构造脑暴 orchestrator subgraph，返回 compile 后的 graph。

    输入字段（BrainstormState）：project / task / loop_name / roles /
        max_rounds / audit_rounds / readiness_threshold / context_warn_tokens /
        start_round（可选，默认 1）
    """
    g = StateGraph(BrainstormState)

    g.add_node("init", _node_init)
    g.add_node("run_round", _node_run_round)
    g.add_node("evaluate", _node_evaluate)
    g.add_node("next_round", _node_next_round)
    g.add_node("finish_ready_for_prd", _make_finish_node("ready_for_prd"))
    g.add_node("finish_ask_user", _make_finish_node("ask_user"))
    g.add_node("finish_stop_low_value", _make_finish_node("stop_low_value"))
    g.add_node("finish_max_rounds", _make_finish_node("max_rounds"))
    g.add_node("finish_halted", _make_finish_node("halted"))
    g.add_node("finish_pending_gate", _make_finish_node("pending_gate"))

    g.add_edge(START, "init")
    g.add_conditional_edges(
        "init",
        _route_after_init,
        {
            "run_round": "run_round",
            "finish_halted": "finish_halted",
            "finish_pending_gate": "finish_pending_gate",
        },
    )
    g.add_conditional_edges(
        "run_round",
        _route_after_run,
        {"evaluate": "evaluate", "finish_halted": "finish_halted"},
    )
    g.add_conditional_edges(
        "evaluate",
        _route_after_evaluate,
        {
            "next_round": "next_round",
            "finish_ready_for_prd": "finish_ready_for_prd",
            "finish_ask_user": "finish_ask_user",
            "finish_stop_low_value": "finish_stop_low_value",
            "finish_max_rounds": "finish_max_rounds",
            "finish_halted": "finish_halted",
        },
    )
    g.add_edge("next_round", "run_round")
    for term in (
        "finish_ready_for_prd",
        "finish_ask_user",
        "finish_stop_low_value",
        "finish_max_rounds",
        "finish_halted",
        "finish_pending_gate",
    ):
        g.add_edge(term, END)

    return g.compile()
