"""
graph/discussion.py — 多角色讨论 subgraph（Phase 4b）

模型：
- N 个参与者按 round-robin 顺序发言（L1 主持人；L3 LLM 主持人留给 4c）
- 每轮发言基于当前完整对话历史 + 议题
- 最多跑 max_rounds 轮（每轮 = 一个角色发言一次）
- 全部发言追加到 vault `10-项目/{project}/脑暴-{name}.md`，结束时再写一次完整版

Phase 4b 限制：
- 主持人只做 round-robin（无智能裁决）
- 无共识检测（跑满 max_rounds 退出）
- 无用户介入（推迟到 Phase 5a Canvas）

Phase 4c 升级方向：LLM 主持人 + 共识检测 + 决议 YAML 自动生成。
"""

from __future__ import annotations

from datetime import datetime, timezone
from operator import add
from typing import Annotated, TypedDict

from langgraph.graph import StateGraph, START, END

from ..config import project_dir
from ..llm import call_llm
from ..obsidian_io import write_note
from ..role_loader import load_role


class DiscussionState(TypedDict, total=False):
    # 入口（由父图传入）
    project: str
    task: str
    topic: str                                # 议题描述
    participants: tuple[str, ...]             # 参与者中文名列表
    moderator: str | None                     # 主持人（None 时第一个参与者主持）
    max_rounds: int
    discussion_name: str                      # 用于脑暴笔记文件名

    # 累积
    messages: Annotated[list[dict], add]      # [{round, speaker, content, timestamp}]
    current_round: int
    next_speaker: str | None
    finished: bool


# ── 节点 ─────────────────────────────────────────────────
def _node_init(state: DiscussionState) -> dict:
    """讨论开始：欢迎语 + 议题广播 + 选首发。"""
    print(f"\n{'─' * 60}")
    print(f"💬 讨论开始：{state['discussion_name']}")
    print(f"  参与者：{list(state['participants'])}")
    print(f"  议题：{state['topic'][:80]}{'...' if len(state['topic']) > 80 else ''}")
    print(f"  最多 {state['max_rounds']} 轮")
    print(f"{'─' * 60}\n")
    # 第一个发言者：moderator 优先，否则 participants[0]
    first = state.get("moderator") or state["participants"][0]
    return {"current_round": 0, "next_speaker": first, "finished": False}


def _build_speaker_prompt(state: DiscussionState, speaker: str) -> tuple[str, str]:
    """构造某角色当前轮的 (system_prompt, user_prompt)。"""
    role = load_role(speaker)

    # system prompt：角色基因正文（不走 build_system_prompt 因为不需要 OUTPUT_FORMAT_SPEC）
    system_prompt = (
        f"## 角色摘要\n"
        f"角色：{role.name}\n"
        f"领域：{role.domain}\n"
        f"风格：{role.style}\n\n"
        + role.body.strip()
        + "\n\n## 讨论场约束（覆盖一切默认行为）\n"
        "你正在参加一场多角色讨论。每次只输出**一段发言**（200-500 字），"
        "不要使用 FILE 块、不要写文件、不要询问权限。"
        "直接以你的角色身份说话，可以引用其他角色的具体说法。"
    )

    # user prompt：议题 + 历史
    history_lines = []
    for m in state.get("messages", []):
        history_lines.append(
            f"【第 {m['round']} 轮 · {m['speaker']}】\n{m['content']}"
        )
    history_block = "\n\n".join(history_lines) if history_lines else "（你是第一个发言）"

    user_prompt = (
        f"# 议题\n{state['topic']}\n\n"
        f"# 项目背景\n项目名：{state['project']}\n任务：{state['task']}\n\n"
        f"# 已有讨论历史\n{history_block}\n\n"
        f"---\n"
        f"现在轮到你（**{speaker}**）发言。请直接输出你的一段发言："
    )
    return system_prompt, user_prompt


def _node_speak(state: DiscussionState) -> dict:
    """让 next_speaker 角色发言一次，返回新 message 追加到 messages。"""
    speaker = state["next_speaker"]
    next_round = state["current_round"] + 1

    print(f"\n【第 {next_round} 轮 · {speaker}】", flush=True)
    role = load_role(speaker)
    system_prompt, user_prompt = _build_speaker_prompt(state, speaker)
    text = call_llm(
        system_prompt, user_prompt,
        model=role.model,
        max_tokens=role.max_tokens,
        print_stream=True,
    )

    msg = {
        "round": next_round,
        "speaker": speaker,
        "content": text.strip(),
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
    return {"messages": [msg], "current_round": next_round}


def _node_decide_next(state: DiscussionState) -> dict:
    """L1 round-robin 主持人：选下一个发言者。

    Phase 4c 升级为 LLM 主持人 + 共识检测，可在此 node 内做更智能的决策。
    """
    participants = list(state["participants"])
    if not state.get("messages"):
        # 还没人说话（不应到这里，但保护一下）
        return {"next_speaker": participants[0]}
    last_speaker = state["messages"][-1]["speaker"]
    try:
        idx = participants.index(last_speaker)
    except ValueError:
        idx = -1
    next_idx = (idx + 1) % len(participants)
    return {"next_speaker": participants[next_idx]}


def _check_done(state: DiscussionState) -> str:
    """条件路由：是否已达 max_rounds？"""
    if state.get("current_round", 0) >= state["max_rounds"]:
        return "write_log"
    return "decide_next"


def _node_write_log(state: DiscussionState) -> dict:
    """把讨论历史写入 vault `10-项目/{project}/脑暴-{name}.md`。"""
    project = state["project"]
    name = state["discussion_name"]
    rel_path = f"10-项目/{project}/脑暴-{name}.md"

    lines = [
        "---",
        "type: discussion-log",
        f"project: {project}",
        f"discussion_name: {name}",
        f"participants: [{', '.join(state['participants'])}]",
        f"moderator: {state.get('moderator') or state['participants'][0]}",
        f"max_rounds: {state['max_rounds']}",
        f"actual_rounds: {state.get('current_round', 0)}",
        f"finished_at: {datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')}",
        "---",
        "",
        f"# 讨论：{name}",
        "",
        f"## 议题",
        "",
        state["topic"],
        "",
        f"## 对话记录",
        "",
    ]

    for m in state.get("messages", []):
        lines.append(f"### 第 {m['round']} 轮 · {m['speaker']}")
        lines.append(f"<small>{m['timestamp']}</small>")
        lines.append("")
        lines.append(m["content"])
        lines.append("")

    write_note(rel_path, "\n".join(lines))
    print(f"\n💬 讨论结束（{state['current_round']} 轮），笔记已写入：{rel_path}")
    return {"finished": True}


# ── 构造 subgraph ────────────────────────────────────────
def build_discussion_graph():
    """构造讨论 subgraph，返回 compile 后的 graph。

    输入字段（DiscussionState）：project / task / topic / participants /
        moderator / max_rounds / discussion_name
    """
    g = StateGraph(DiscussionState)
    g.add_node("init", _node_init)
    g.add_node("speak", _node_speak)
    g.add_node("decide_next", _node_decide_next)
    g.add_node("write_log", _node_write_log)

    g.add_edge(START, "init")
    g.add_edge("init", "speak")
    g.add_conditional_edges(
        "speak",
        _check_done,
        {"decide_next": "decide_next", "write_log": "write_log"},
    )
    g.add_edge("decide_next", "speak")
    g.add_edge("write_log", END)

    return g.compile()
