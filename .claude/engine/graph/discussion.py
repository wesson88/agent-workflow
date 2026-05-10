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

import json
import re
from datetime import datetime, timezone
from operator import add
from pathlib import Path
from typing import Annotated, TypedDict

from langgraph.graph import StateGraph, START, END

from ..canvas_export import build_canvas_from_state, write_canvas_atomic
from ..config import VAULT_ROOT, project_dir
from ..llm import call_llm
from ..obsidian_io import write_note
from ..role_loader import load_role


# Phase 5a-4：识别 LangGraph 自己写的节点 vs 用户在 Canvas 加的节点
_LANGGRAPH_NODE_ID_RE = re.compile(r"^(topic|header_\d+|msg_\d+_\d+)$")


# 单文件最多注入字符数（防爆 prompt；超出截尾并附省略提示）
_MAX_DOC_CHARS = 8000


def _read_doc_capped(path) -> str | None:
    """读取一个文档，超长则截尾。文件不存在或为空返回 None。"""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except Exception as e:
        return f"（读取失败：{e}）"
    text = text.strip()
    if not text:
        return None
    if len(text) > _MAX_DOC_CHARS:
        text = text[:_MAX_DOC_CHARS] + f"\n\n…（截断：原文 {len(text)} 字符，已展示前 {_MAX_DOC_CHARS}）"
    return text


def _gather_project_context(project: str) -> str:
    """读取 vault 10-项目/{project}/ 下的核心产出，组装成 markdown。

    优先级文档：PRD.md / 系统设计.md / API契约.md
    指令子目录：指令/*.md（给后端 / 给前端 / 给技术主管 等）
    返回字符串可能为空（项目刚启动时）。
    """
    pdir = project_dir(project)
    sections: list[str] = []

    for fname in ("PRD.md", "系统设计.md", "API契约.md"):
        content = _read_doc_capped(pdir / fname)
        if content:
            sections.append(f"### 📄 {fname}\n\n{content}")

    instr_dir = pdir / "指令"
    if instr_dir.is_dir():
        for sub in sorted(instr_dir.glob("*.md")):
            content = _read_doc_capped(sub)
            if content:
                sections.append(f"### 📄 指令/{sub.name}\n\n{content}")

    if not sections:
        return "（项目当前尚无核心产出文档；请基于议题与项目名讨论。）"
    return "\n\n---\n\n".join(sections)


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

    # Phase 5a-4：用户在 Canvas 上加的节点 ID（已处理的，避免重复 inject）
    processed_injections: Annotated[list[str], add]


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


def _canvas_path(state: DiscussionState) -> Path:
    """讨论 canvas 同位路径。"""
    return VAULT_ROOT / "10-项目" / state["project"] / f"脑暴-{state['discussion_name']}.canvas"


def _read_existing_canvas(state: DiscussionState) -> dict | None:
    """读 vault 内已有的 canvas 文件。无文件 / 解析失败时返回 None。"""
    p = _canvas_path(state)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _extract_user_nodes(canvas: dict | None) -> list[dict]:
    """从 canvas 提取**用户加的节点**（ID 不在 LangGraph 命名空间）。"""
    if not canvas:
        return []
    return [
        n for n in canvas.get("nodes", [])
        if n.get("type") == "text"
        and not _LANGGRAPH_NODE_ID_RE.match(n.get("id", ""))
        and (n.get("text", "") or "").strip()
    ]


def _scan_user_injections(state: DiscussionState) -> list[dict]:
    """返回**未处理**的用户介入节点（每轮调用，inject 到下一发言者 prompt）。"""
    user_nodes = _extract_user_nodes(_read_existing_canvas(state))
    if not user_nodes:
        return []
    processed = set(state.get("processed_injections", []) or [])
    return [n for n in user_nodes if n.get("id") not in processed]


def _build_speaker_prompt(
    state: DiscussionState,
    speaker: str,
    injections: list[dict] | None = None,
) -> tuple[str, str]:
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
        "你正在参加一场多角色讨论：\n"
        "1. **不要调用任何工具、不要请求权限、不要写文件、不要使用 FILE 块。** "
        "讨论场是只对话场景；所有项目文档都已在用户消息里以纯文本提供，"
        "你需要的所有信息都在 prompt 里。\n"
        "2. **每次只输出一段发言（200-500 字）。** 直接以你的角色身份说话。\n"
        "3. **基于已提供的项目文档和讨论历史，给出具体、可操作的观点。** "
        "可以引用其他角色的具体说法，可以指出文档中的具体段落。\n"
        "4. **不要说『我需要查看 X 文件』或『请提供 Y 资料』** —— 该有的资料都已经在下面了。"
    )

    # user prompt：议题 + 项目文档 + 历史
    project_context = _gather_project_context(state["project"])

    history_lines = []
    for m in state.get("messages", []):
        history_lines.append(
            f"【第 {m['round']} 轮 · {m['speaker']}】\n{m['content']}"
        )
    history_block = "\n\n".join(history_lines) if history_lines else "（你是第一个发言）"

    # Phase 5a-4：用户实时介入块（如有未处理的 Canvas 节点）
    injection_block = ""
    if injections:
        lines = ["# 用户实时介入（**必须正面回应**）", ""]
        for inj in injections:
            lines.append(f"> {inj['text'].strip()}")
            lines.append("")
        injection_block = "\n".join(lines) + "\n"

    user_prompt = (
        f"# 议题\n{state['topic']}\n\n"
        f"# 项目背景\n项目名：{state['project']}\n任务：{state['task']}\n\n"
        f"# 项目文档（已有产出）\n\n"
        f"以下是项目当前已经产出的核心文档。**这是你讨论的事实依据，"
        f"请直接基于这些内容给出具体观点，不要询问『是否能查看』。**\n\n"
        f"{project_context}\n\n"
        f"# 已有讨论历史\n{history_block}\n\n"
        f"{injection_block}"
        f"---\n"
        f"现在轮到你（**{speaker}**）发言。请直接输出你的一段发言："
    )
    return system_prompt, user_prompt


def _node_speak(state: DiscussionState) -> dict:
    """让 next_speaker 角色发言一次，返回新 message 追加到 messages。"""
    speaker = state["next_speaker"]
    next_round = state["current_round"] + 1

    print(f"\n【第 {next_round} 轮 · {speaker}】", flush=True)

    # Phase 5a-4：扫 canvas 看用户有没有加新节点（"实时介入"）
    new_injections = _scan_user_injections(state)
    if new_injections:
        print(
            f"  💡 发现 {len(new_injections)} 条用户实时介入，"
            f"已注入 {speaker} 的 prompt"
        )

    role = load_role(speaker)
    system_prompt, user_prompt = _build_speaker_prompt(state, speaker, injections=new_injections)
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

    # Phase 5a-3：每轮发言后**增量覆写** canvas，让 Obsidian 实时刷新
    # Phase 5a-4：覆写时**保留**用户加的节点（含已处理 + 本轮新介入），不让 5a-3 误清
    try:
        live_state = {
            **state,
            "messages": list(state.get("messages", []) or []) + [msg],
            "current_round": next_round,
        }
        canvas = build_canvas_from_state(live_state, layout="grid", draw_edges=True)
        # 把已有 canvas 里的用户节点附加回新 canvas
        existing_user_nodes = _extract_user_nodes(_read_existing_canvas(state))
        if existing_user_nodes:
            canvas["nodes"].extend(existing_user_nodes)
        write_canvas_atomic(canvas, _canvas_path(state))
    except Exception as e:
        # 增量 canvas 失败不阻塞讨论流程；最终版还会再写一次
        print(f"⚠️  增量 canvas 写入失败（忽略，继续讨论）：{e}")

    return {
        "messages": [msg],
        "current_round": next_round,
        "processed_injections": [n["id"] for n in new_injections],
    }


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

    # Phase 5a-2：同位写一份 Obsidian Canvas 视图（grid 默认布局）
    # Phase 5a-4：保留用户介入节点
    try:
        canvas = build_canvas_from_state(state, layout="grid", draw_edges=True)
        existing_user_nodes = _extract_user_nodes(_read_existing_canvas(state))
        if existing_user_nodes:
            canvas["nodes"].extend(existing_user_nodes)
        canvas_rel = f"10-项目/{project}/脑暴-{name}.canvas"
        write_canvas_atomic(canvas, VAULT_ROOT / canvas_rel)
        print(
            f"💬 Canvas 视图已写入：{canvas_rel}"
            f"（{len(canvas['nodes'])} 节点 / {len(canvas['edges'])} 边"
            f"{f'，含 {len(existing_user_nodes)} 条用户介入' if existing_user_nodes else ''}）"
        )
    except Exception as e:
        # Canvas 是 nice-to-have，失败不影响主流程
        print(f"⚠️  Canvas 写入失败（不影响讨论结果）：{e}")

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
