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
- 无用户介入

Phase 4c 升级方向：LLM 主持人 + 共识检测 + 决议 YAML 自动生成。

域适配（2026-10-16，视频域观众代表引入）：父图传入工作流 domain 时，每个发言者
- system prompt 追加其域视角 adapter（`00-系统/规则/{domain}/{角色}-视角.md`，有则注入）
- adapter frontmatter 声明 `context_docs` → 用它替代 SE 默认三件套作为项目文档
- adapter frontmatter 声明 `vision_inputs` → 图/视频抽帧作为附件随该发言者的调用发送
通用角色基因本身不动（[[通用角色扩展原则]] §2 开闭：域扩展只落 adapter）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from operator import add
from typing import Annotated, TypedDict

from langgraph.graph import StateGraph, START, END

from ..ability_loader import load_domain_adapter, load_domain_adapter_meta
from ..config import VAULT_ROOT, project_dir
from ..llm import call_llm
from ..obsidian_io import write_note
from ..role_loader import load_role


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


_DEFAULT_CONTEXT_DOCS = ("PRD.md", "系统设计.md", "API契约.md")


def _gather_project_context(project: str, context_docs: list[str] | None = None) -> str:
    """读取 vault 10-项目/{project}/ 下的核心产出，组装成 markdown。

    只读评审讨论真正需要的产物：PRD / 系统设计 / API契约。

    **不读** `指令/*.md`：这是技术主管阶段（讨论之后）才会生成的派活清单，
    架构评审讨论触发时只会扫到架构师写的「给技术主管.md」一个文件——它是
    给 TL 看的任务切分指南，对架构师/前后端/批判者评审"架构是否合理"无价值，
    塞进 prompt 会把讨论带偏到"评审任务切分"且每轮多吃 ~2K tokens。
    """
    sections: list[str] = []
    if context_docs:
        # adapter 声明的 vault 路径（{project} 占位）——非 SE 域的项目目录不在 10-项目/{project}/
        targets = [
            (rel.replace("{project}", project), VAULT_ROOT / rel.replace("{project}", project))
            for rel in context_docs
        ]
    else:
        pdir = project_dir(project)
        targets = [(fname, pdir / fname) for fname in _DEFAULT_CONTEXT_DOCS]

    for label, path in targets:
        content = _read_doc_capped(path)
        if content:
            sections.append(f"### 📄 {label}\n\n{content}")

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
    domain: str | None                        # 工作流域（adapter 目录名），None = 不做域适配

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


def _build_speaker_prompt(state: DiscussionState, speaker: str) -> tuple[str, str, list]:
    """构造某角色当前轮的 (system_prompt, user_prompt, 图片附件)。"""
    role = load_role(speaker)
    domain = state.get("domain")
    adapter_block, _ = load_domain_adapter(role.name, domain)
    adapter_meta = load_domain_adapter_meta(role.name, domain)

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
        + adapter_block
    )

    # user prompt：议题 + 项目文档 + 历史
    project_context = _gather_project_context(
        state["project"], [str(d) for d in (adapter_meta.get("context_docs") or [])],
    )
    attachments, vision_note = _vision_for(domain, state["project"], adapter_meta)

    history_lines = []
    for m in state.get("messages", []):
        history_lines.append(
            f"【第 {m['round']} 轮 · {m['speaker']}】\n{m['content']}"
        )
    history_block = "\n\n".join(history_lines) if history_lines else "（你是第一个发言）"

    user_prompt = (
        f"# 议题\n{state['topic']}\n\n"
        f"# 项目背景\n项目名：{state['project']}\n任务：{state['task']}\n\n"
        f"# 项目文档（已有产出）\n\n"
        f"以下是项目当前已经产出的核心文档。**这是你讨论的事实依据，"
        f"请直接基于这些内容给出具体观点，不要询问『是否能查看』。**\n\n"
        f"{project_context}\n\n"
        f"{vision_note}"
        f"# 已有讨论历史\n{history_block}\n\n"
        f"---\n"
        f"现在轮到你（**{speaker}**）发言。请直接输出你的一段发言："
    )
    return system_prompt, user_prompt, attachments


# 同一讨论内同一素材集只抽一次帧（发言者每轮都发言，素材不变；mtime 变了自动失效）
_VISION_CACHE: dict[tuple, tuple[list, str]] = {}


def _vision_for(domain: str | None, project: str, adapter_meta: dict) -> tuple[list, str]:
    entries = [str(e) for e in (adapter_meta.get("vision_inputs") or [])]
    if not entries or not domain:
        return [], ""
    from ..config import asset_root
    root = asset_root(domain, project)
    stamp = tuple(
        (e, (root / e).stat().st_mtime if root and (root / e).exists() else None) for e in entries
    )
    key = (domain, project, stamp)
    if key not in _VISION_CACHE:
        from ..role_runner import prepare_vision_inputs
        _VISION_CACHE[key] = prepare_vision_inputs(domain, project, entries)
    return _VISION_CACHE[key]


def _node_speak(state: DiscussionState) -> dict:
    """让 next_speaker 角色发言一次，返回新 message 追加到 messages。"""
    speaker = state["next_speaker"]
    next_round = state["current_round"] + 1

    print(f"\n【第 {next_round} 轮 · {speaker}】", flush=True)

    role = load_role(speaker)
    system_prompt, user_prompt, attachments = _build_speaker_prompt(state, speaker)
    extra = {"attachments": attachments} if attachments else {}
    text = call_llm(
        system_prompt, user_prompt,
        model=role.model,
        max_tokens=role.max_tokens,
        print_stream=True,
        **extra,
    )

    msg = {
        "round": next_round,
        "speaker": speaker,
        "content": text.strip(),
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }

    return {
        "messages": [msg],
        "current_round": next_round,
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
