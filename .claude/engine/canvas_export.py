"""
engine/canvas_export.py — DiscussionState → Obsidian Canvas

设计文档：vault `99-临时/phase-5a-canvas-design.md`

两个入口：
- build_canvas_from_state(state, **opts) → canvas dict
    discussion.py 运行时调用（最准，无 parse 损失）
- build_canvas_from_md(md_text, **opts) → canvas dict
    独立 CLI / 离线补救已有 .md 用

写盘函数：write_canvas_atomic(canvas_dict, dest_path)

设计要点：
- preset 色 "1"-"6"（hex 在 Obsidian 1.x 不渲染节点背景，只控边框且常被主题盖住）
- 内容用 Obsidian Callout 块包裹（[!type]+ ...），给标题彩头 + 浅色背景
- 双布局：grid（参与者=列，时间=行，纵向阅读）/ swimlane（参与者=行，时间=列，横向时间轴）
"""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from typing import Any


# ── 视觉常量 ─────────────────────────────────────────────
CARD_WIDTH = 420
CARD_HEIGHT = 500
GAP = 28
HEADER_HEIGHT = 90
TOPIC_HEIGHT = 180
MAX_MSG_CHARS = 800

# Obsidian preset 色 "1"-"6"，hex 在 1.x 不渲染节点背景
SPEAKER_COLORS = ["6", "2", "5", "3", "1", "4"]    # 紫/橙/青/黄/红/绿
TOPIC_COLOR = "5"
HEADER_COLOR = "6"

# Obsidian Callout 类型（每参与者循环一种）
SPEAKER_CALLOUT = ["example", "tip", "info", "warning", "danger", "success"]
TOPIC_CALLOUT = "quote"
HEADER_CALLOUT = "abstract"

ROLE_EMOJI = {
    "架构师": "🏛",
    "首席架构师": "🏛",
    "技术主管": "🎯",
    "后端工程师": "⚙",
    "前端工程师": "🎨",
    "批判者": "🔍",
    "用户体验者": "👥",
    "产品经理": "📋",
    "复盘者": "🪞",
    "晋升者": "⬆",
    "知识沉淀者": "📚",
}


# ── markdown 解析（build_canvas_from_md 用）────────────────
_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_MESSAGE_HEADER_RE = re.compile(
    r"^### 第\s*(\d+)\s*轮\s*·\s*(.+?)\s*$", re.MULTILINE
)
_TIMESTAMP_RE = re.compile(r"<small>([\dT:.\-Z+]+)</small>")


def _parse_frontmatter(text: str) -> dict:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    fm: dict = {}
    for line in m.group(1).split("\n"):
        line = line.strip()
        if not line or ":" not in line:
            continue
        k, _, v = line.partition(":")
        fm[k.strip()] = v.strip()
    if "participants" in fm:
        raw = fm["participants"].strip("[]").strip()
        fm["participants"] = [p.strip() for p in raw.split(",")] if raw else []
    return fm


def _parse_topic_md(text: str) -> str:
    m = re.search(r"## 议题\s*\n(.+?)(?=\n## )", text, re.DOTALL)
    return m.group(1).strip() if m else "(议题未找到)"


def _parse_messages_md(text: str) -> list[dict]:
    msgs: list[dict] = []
    headers = list(_MESSAGE_HEADER_RE.finditer(text))
    for i, h in enumerate(headers):
        round_n = int(h.group(1))
        speaker = h.group(2).strip()
        body_start = h.end()
        body_end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        body = text[body_start:body_end].strip()
        ts_m = _TIMESTAMP_RE.search(body)
        timestamp = ts_m.group(1) if ts_m else ""
        if ts_m:
            body = body[:ts_m.start()] + body[ts_m.end():]
        msgs.append({
            "round": round_n,
            "speaker": speaker,
            "timestamp": timestamp,
            "content": body.strip(),
        })
    return msgs


# ── 节点文本格式化（Callout 包裹）─────────────────────────
def _quote_lines(text: str) -> str:
    out = []
    for line in text.split("\n"):
        out.append(f"> {line}" if line.strip() else ">")
    return "\n".join(out)


def _truncate(text: str, limit: int = MAX_MSG_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n\n*…（截断；详见对应脑暴笔记）*"


def _format_topic_text(topic: str, project: str, discussion_name: str) -> str:
    body = _quote_lines(f"`项目` **{project}**\n\n{topic}")
    return f"> [!{TOPIC_CALLOUT}]+ 💬 {discussion_name}\n{body}"


def _format_header_text(speaker: str, is_moderator: bool) -> str:
    emoji = ROLE_EMOJI.get(speaker, "👤")
    badge = "（主持人）" if is_moderator else ""
    return f"> [!{HEADER_CALLOUT}]+ {emoji} {speaker}{badge}"


def _format_message_text(
    speaker: str, round_n: int, timestamp: str, content: str, callout_type: str,
) -> str:
    emoji = ROLE_EMOJI.get(speaker, "👤")
    head = f"> [!{callout_type}]+ {emoji} {speaker}  ·  第 {round_n} 轮"
    ts = f"> <sub>{timestamp}</sub>\n>" if timestamp else ">"
    body = _quote_lines(_truncate(content))
    return f"{head}\n{ts}\n{body}"


# ── 布局 ────────────────────────────────────────────────
def _layout_grid(participants: list[str], speaker_col: dict[str, int]):
    n_cols = len(participants)
    canvas_width = max(960, n_cols * (CARD_WIDTH + GAP) - GAP)
    layout = {
        "topic": (0, 0, canvas_width, TOPIC_HEIGHT),
        "headers": [
            (i * (CARD_WIDTH + GAP), TOPIC_HEIGHT + GAP, CARD_WIDTH, HEADER_HEIGHT)
            for i in range(n_cols)
        ],
    }
    first_msg_y = TOPIC_HEIGHT + GAP + HEADER_HEIGHT + GAP
    row_step = CARD_HEIGHT + GAP
    col_step = CARD_WIDTH + GAP

    def msg_pos(speaker: str, round_n: int) -> tuple[int, int, int, int]:
        col = speaker_col[speaker]
        return (
            col * col_step,
            first_msg_y + (round_n - 1) * row_step,
            CARD_WIDTH, CARD_HEIGHT,
        )
    return layout, msg_pos, "vertical"


def _layout_swimlane(participants: list[str], speaker_col: dict[str, int], max_round: int):
    n_rows = len(participants)
    header_col_width = 220
    canvas_width = (
        header_col_width + GAP + max_round * (CARD_WIDTH + GAP) - GAP
    )
    layout = {
        "topic": (0, 0, max(960, canvas_width), TOPIC_HEIGHT),
        "headers": [
            (
                0,
                TOPIC_HEIGHT + GAP + i * (CARD_HEIGHT + GAP),
                header_col_width,
                CARD_HEIGHT,
            )
            for i in range(n_rows)
        ],
        "header_col_width": header_col_width,
    }

    def msg_pos(speaker: str, round_n: int) -> tuple[int, int, int, int]:
        row = speaker_col[speaker]
        return (
            header_col_width + GAP + (round_n - 1) * (CARD_WIDTH + GAP),
            TOPIC_HEIGHT + GAP + row * (CARD_HEIGHT + GAP),
            CARD_WIDTH, CARD_HEIGHT,
        )
    return layout, msg_pos, "horizontal"


# ── 核心构造 ────────────────────────────────────────────
def build_canvas(
    *,
    topic: str,
    project: str,
    discussion_name: str,
    participants: list[str],
    moderator: str,
    messages: list[dict],
    layout: str = "grid",
    draw_edges: bool = True,
) -> dict[str, Any]:
    """通用构造函数。messages 形如 [{round, speaker, timestamp, content}]。"""
    speaker_col = {p: i for i, p in enumerate(participants)}
    speaker_color = {p: SPEAKER_COLORS[i % len(SPEAKER_COLORS)] for i, p in enumerate(participants)}
    speaker_callout = {p: SPEAKER_CALLOUT[i % len(SPEAKER_CALLOUT)] for i, p in enumerate(participants)}

    if layout == "swimlane":
        max_round = max((m["round"] for m in messages), default=1)
        layout_data, msg_pos, flow = _layout_swimlane(participants, speaker_col, max_round)
    else:
        layout_data, msg_pos, flow = _layout_grid(participants, speaker_col)

    nodes: list[dict] = []
    edges: list[dict] = []

    # 议题
    tx, ty, tw, th = layout_data["topic"]
    nodes.append({
        "id": "topic", "type": "text",
        "text": _format_topic_text(topic, project, discussion_name),
        "x": tx, "y": ty, "width": tw, "height": th,
        "color": TOPIC_COLOR,
    })

    # 表头
    for i, p in enumerate(participants):
        hx, hy, hw, hh = layout_data["headers"][i]
        nodes.append({
            "id": f"header_{i}", "type": "text",
            "text": _format_header_text(p, p == moderator),
            "x": hx, "y": hy, "width": hw, "height": hh,
            "color": HEADER_COLOR,
        })

    # 消息节点 + 时序边
    prev_msg_id: str | None = None
    for k, m in enumerate(messages):
        speaker = m["speaker"]
        if speaker not in speaker_col:
            idx = len(speaker_col)
            speaker_col[speaker] = idx
            speaker_color[speaker] = SPEAKER_COLORS[idx % len(SPEAKER_COLORS)]
            speaker_callout[speaker] = SPEAKER_CALLOUT[idx % len(SPEAKER_CALLOUT)]
        col = speaker_col[speaker]
        round_n = m["round"]
        msg_id = f"msg_{round_n}_{col}"
        x, y, w, h = msg_pos(speaker, round_n)
        nodes.append({
            "id": msg_id, "type": "text",
            "text": _format_message_text(
                speaker, round_n, m.get("timestamp", ""), m["content"],
                callout_type=speaker_callout[speaker],
            ),
            "x": x, "y": y, "width": w, "height": h,
            "color": speaker_color[speaker],
        })
        if draw_edges and prev_msg_id is not None:
            from_side, to_side = (
                ("right", "left") if flow == "horizontal" else ("bottom", "top")
            )
            edges.append({
                "id": f"e_{k - 1}",
                "fromNode": prev_msg_id, "toNode": msg_id,
                "fromSide": from_side, "toSide": to_side,
                "color": "#4b5563",
            })
        prev_msg_id = msg_id

    return {"nodes": nodes, "edges": edges}


def build_canvas_from_state(state: dict, *, layout: str = "grid", draw_edges: bool = True) -> dict[str, Any]:
    """从 DiscussionState 构造 canvas（运行时入口，无 parse 损失）。"""
    return build_canvas(
        topic=state.get("topic", ""),
        project=state.get("project", "(unknown)"),
        discussion_name=state.get("discussion_name", "讨论"),
        participants=list(state.get("participants", []) or []),
        moderator=state.get("moderator", ""),
        messages=list(state.get("messages", []) or []),
        layout=layout, draw_edges=draw_edges,
    )


def build_canvas_from_md(md_text: str, *, layout: str = "grid", draw_edges: bool = True) -> dict[str, Any]:
    """从已落盘的 脑暴-*.md 解析后构造 canvas（CLI / 离线入口）。"""
    fm = _parse_frontmatter(md_text)
    return build_canvas(
        topic=_parse_topic_md(md_text),
        project=fm.get("project", "(unknown)"),
        discussion_name=fm.get("discussion_name", "讨论"),
        participants=fm.get("participants", []) or [],
        moderator=fm.get("moderator", ""),
        messages=_parse_messages_md(md_text),
        layout=layout, draw_edges=draw_edges,
    )


# ── 写盘 ────────────────────────────────────────────────
def write_canvas_atomic(canvas: dict, dest_path: Path) -> None:
    """原子写：先写到 sibling temp，再 replace。仿 obsidian-git contention 兼容。"""
    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(canvas, ensure_ascii=False, indent=2)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8",
        dir=dest_path.parent, prefix=".tmp-canvas-", suffix=".canvas",
        delete=False,
    ) as f:
        f.write(payload)
        tmp_path = Path(f.name)
    tmp_path.replace(dest_path)
