"""
engine/injection_fingerprint.py — 注入指纹（P0.1 · 2026-08-24）

立项：[[编排器改造-立项-2026-08-19]] §P0.1。

## 为什么存在

病根不是「某个加载器有 bug」，是「机制存在但接在空管道上，且无人知道」——
归因文档记了 9 条同族「沉默失效」：注入路径返回 True、日志说加载了，
但内容根本没进最终 prompt，或进了错的那一份。980 条测试测不到这一类，
因为它们测的是「函数遵守契约」，不是「契约接到了东西上」。

本模块把「这次 call 实际拿到了什么」从易失 stdout 变成 audit.jsonl 可查字段。
**被动解析，不改任何加载器** —— 所有注入路径都已经在用 `=== … ===` 信封
把每块内容包起来（历史巧合，5 个模块 9 处独立演化出同一形状），这里把它
从巧合升级为受测契约（见 tests/engine/test_injection_fingerprint.py 的
`_REGISTRY`：新增注入点必须登记，否则测试红）。

## 信封格式（全部 7 种，含产出点）

| kind             | 形状                                              | 产出点 |
|------------------|---------------------------------------------------|--------|
| `skill_trigger`  | `=== Skill (auto-trigger:{reason} · {tier}): [[stem]] ===` | skill_trigger.py |
| `skill_wikilink` | `=== Skill (wikilink:[[target]] · full) ===`       | ability_loader.py |
| `genre_primitive`| `=== Primitive ({via} · {tier}): [[stem]] ===`      | ability_loader.py（music 流派 primitive，独立预算） |
| `skill_task`     | `=== Skill: [[target]] ===`                        | prompt_builder.py（TL 子任务） |
| `skill_cite`     | `=== Skill 引用: [[target]] ({文件名}) ===`         | dev_backend / dev_frontend |
| `rule_ref`       | `=== [[F-角色#章节]] ===`                           | ability_loader.py |
| `input_file`     | `=== 文件名.md ===`（不含 `[[`，带扩展名）           | input_reader / common / archivist / graduator / TL |

判别顺序即上表顺序。认不出的信封 → `kind="unknown"`，call_llm 侧打 stderr +
audit warn：**仪表宁可报「我看不懂」也不能默默归错类**。

已退役：`skill_ref`（`=== Skill: {vault相对路径} ===`，role_loader 的静态
skill_refs）。2026-08-25 随 skill_refs 废弃拆除产出点。`Skill:` 后面**不是**
`[[…]]` 的形态因此故意落到 `unknown` —— 若哪天它又出现（vault 回滚、旧脚本、
误改），仪表会喊而不是把它归到一个已不存在的机制名下。

## 一并抓的降级信号（各产出点已有的文本标记）

- `truncated`：`[截断警告]` / `[总量截断]` —— 输入被裁过
- `empty`：信封在、正文空 —— 最典型的沉默失效形态

`[SKILL MISSING:` / `[SKILL READ ERROR:` 两个标记随 `_resolve_skill_refs` 一并
移除：它们唯一的产出点已拆，而「旧形态复活」这一风险已由上面的 `unknown`
兜住 —— 一个风险留一道闸，不留第二道。
"""

from __future__ import annotations

import re
from typing import Any


# 信封头：整行 `=== label ===`。bare `===`（3 字符）不匹配 → 由 _CLOSER_RE 接。
_HEADER_RE = re.compile(r"^===[ \t]*(?P<label>.*?)[ \t]*===[ \t]*$")
_CLOSER_RE = re.compile(r"^===[ \t]*$")
_CLOSER_LABELS = frozenset({"END"})

# `[[target]]` 或 `[[target|alias]]`；rule_ref 的 target 可带 `#章节`
_WIKILINK_ONLY_RE = re.compile(r"^\[\[(?P<target>[^\[\]]+)\]\]$")
_WIKILINK_FIRST_RE = re.compile(r"\[\[(?P<target>[^\[\]]+)\]\]")
_AUTO_TRIGGER_RE = re.compile(
    r"^Skill \(auto-trigger:(?P<reason>.*?) · (?P<tier>\w+)\): \[\[(?P<stem>[^\[\]]+)\]\]$"
)
_WIKILINK_SKILL_RE = re.compile(r"^Skill \(wikilink:\[\[(?P<target>[^\[\]]+)\]\] · (?P<tier>\w+)\)$")
# 流派 primitive（2026-08-26 新增产出点 ability_loader.load_genre_primitive_block）。
# via = `wikilink`（简报 primitive_refs 点名）/ `auto-trigger:keyword:X`（流派名兜底）
_PRIMITIVE_RE = re.compile(
    r"^Primitive \((?P<via>.*?) · (?P<tier>\w+)\): \[\[(?P<stem>[^\[\]]+)\]\]$"
)
# 文件名：含扩展名（`.md` / `.py` / …），可带尾部括注（TL 的「（仅末轮决议）」）
_FILENAME_RE = re.compile(r"^[^\[\]]*\.[0-9A-Za-z]{1,6}(?:\s*[（(].*[）)])?$")

_DEGRADE_MARKERS: tuple[tuple[str, str], ...] = (
    ("truncated", "[截断警告]"),
    ("truncated", "[总量截断]"),
)

SKILL_KINDS = frozenset({
    "skill_trigger", "skill_wikilink", "skill_task", "skill_cite",
})
# genre_primitive **不算** SKILL_KINDS：它走独立通道、独立预算，和角色技能是
# 两种东西（流派 idiom 卡 + 技能索引 vs 工程细则）。混进 SKILL_KINDS 会让
# 「skill 占了多少输入」这个口径失真，而那正是 P1.2 要盯的数。
ALL_KINDS = SKILL_KINDS | {"genre_primitive", "rule_ref", "input_file", "unknown"}


def classify_envelope(label: str) -> dict[str, Any]:
    """把信封 label 归类。纯函数，返回 {kind, name, tier?}。

    判别只看 label（`=== ` 与 ` ===` 之间那段），不看正文 —— 正文可能被截断
    甚至为空，而 kind 判定不能依赖它。
    """
    m = _AUTO_TRIGGER_RE.match(label)
    if m:
        return {
            "kind": "skill_trigger",
            "name": m.group("stem"),
            "tier": m.group("tier"),
            "reason": m.group("reason"),
        }

    m = _WIKILINK_SKILL_RE.match(label)
    if m:
        return {"kind": "skill_wikilink", "name": m.group("target"), "tier": m.group("tier")}

    m = _PRIMITIVE_RE.match(label)
    if m:
        return {
            "kind": "genre_primitive",
            "name": m.group("stem"),
            "tier": m.group("tier"),
            "reason": m.group("via"),
        }

    if label.startswith("Skill 引用:"):
        m = _WIKILINK_FIRST_RE.search(label)
        name = m.group("target") if m else label[len("Skill 引用:"):].strip()
        return {"kind": "skill_cite", "name": name, "tier": "full"}

    if label.startswith("Skill:"):
        rest = label[len("Skill:"):].strip()
        m = _WIKILINK_ONLY_RE.match(rest)
        if m:
            # prompt_builder：TL 按子任务挑的 skill（唯一「角色自己选」的路径）
            return {"kind": "skill_task", "name": m.group("target"), "tier": "full"}
        # 路径形态 = 已退役的 skill_ref。**故意**不给它一个 kind：唯一产出点
        # （role_loader._resolve_skill_refs）已于 2026-08-25 拆除，再出现说明有
        # 东西复活了旧形态，此时该报「看不懂」而不是归到一个死机制名下。
        return {"kind": "unknown", "name": label}

    m = _WIKILINK_ONLY_RE.match(label)
    if m:
        target = m.group("target")
        return {
            "kind": "rule_ref",
            "name": target,
            "tier": "section" if "#" in target else "whole",
        }

    if _FILENAME_RE.match(label):
        return {"kind": "input_file", "name": label}

    return {"kind": "unknown", "name": label}


def parse_blocks(text: str, *, segment: str | None = None) -> list[dict[str, Any]]:
    """扫一段 prompt 文本，返回每个 `=== … ===` 块的指纹。

    块的正文 = 从头行下一行起，到下一个信封头 / 闭合行（`===` 或 `=== END ===`）
    / 文末为止。`chars` 只算正文，不含头行 —— 「这张 skill 给了模型多少字」。

    **`tail: True`** 标在每段最后一个块上：它后面若还有非信封正文（如
    `_build_user_prompt` 追加的产出要求），会被一并计进它的 `chars`，所以那个
    数字是上界而非精确值。2026-08-24 首次真链路比对就是靠这个差异暴露的 ——
    离线基线只喂到 skill 段结束（Ma3 = 3028），真链路 user_prompt 后面还有
    939 字框架文案（Ma3 = 3967）。信封协议里没有闭合标记可依赖（19 个产出点
    里只有 3 个写 `=== END ===`），因此不猜边界，只标明「此数为上界」。
    """
    if not text:
        return []

    out: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    body: list[str] = []

    def _flush() -> None:
        nonlocal cur, body
        if cur is None:
            return
        # 块间空行属于分隔符不属于内容：先剪掉尾部空行，`chars` 才是「这块给了
        # 模型多少字」而不随各产出点的拼接风格（"\n\n".join vs "\n"）漂移
        while body and not body[-1].strip():
            body.pop()
        payload = "\n".join(body)
        cur["chars"] = len(payload)
        flags = [name for name, marker in _DEGRADE_MARKERS if marker in payload]
        if not payload.strip():
            flags.append("empty")
        if flags:
            # dict.fromkeys 去重且保序（truncated 两个 marker 可能同时命中）
            cur["flags"] = list(dict.fromkeys(flags))
        out.append(cur)
        cur, body = None, []

    for line in text.splitlines():
        if _CLOSER_RE.match(line):
            _flush()
            continue
        m = _HEADER_RE.match(line)
        if m:
            label = m.group("label")
            if label in _CLOSER_LABELS:
                _flush()
                continue
            _flush()
            cur = classify_envelope(label)
            if segment:
                cur["seg"] = segment
            continue
        if cur is not None:
            body.append(line)
    tail_open = cur is not None      # 文末仍在块内 = 该块吃到了段尾
    _flush()
    if out and tail_open:
        out[-1]["tail"] = True
    return out


def fingerprint(segments: dict[str, str]) -> dict[str, Any]:
    """把若干 prompt 分段合成一次 call 的注入指纹。

    segments：{段名: 文本}，如 {"static": …, "dynamic_own": …, "user": …}。
    段名会写进每个块的 `seg` 字段 —— 「skill 进的是 system 还是 user」正是
    立项要回答的问题之一。P0.1 实测答案：**全在 user**，system 段注入恒为
    0 chars（9/9 角色）；唯一声称走 system 的静态 skill_refs 实测 0/14 生效，
    已于 2026-08-25 废弃。

    返回：
        {
          "blocks": [{kind, name, chars, tier?, seg, flags?, tail?}, …],
                    # tail=True → 该块吃到了段尾，chars 是上界（见 parse_blocks）
          "counts": {kind: n, …},          # 按 kind 计数
          "chars":  {kind: 总字数, …},
          "unknown": n,                     # > 0 → 出现了第 8 种信封，仪表要瞎
          "degraded": [{kind, name, flags}, …],   # 仅非空时出现
        }
    """
    blocks: list[dict[str, Any]] = []
    for name, text in segments.items():
        blocks.extend(parse_blocks(text, segment=name))

    counts: dict[str, int] = {}
    chars: dict[str, int] = {}
    for b in blocks:
        k = b["kind"]
        counts[k] = counts.get(k, 0) + 1
        chars[k] = chars.get(k, 0) + b.get("chars", 0)

    fp: dict[str, Any] = {
        "blocks": blocks,
        "counts": counts,
        "chars": chars,
        "unknown": counts.get("unknown", 0),
    }
    degraded = [
        {"kind": b["kind"], "name": b["name"], "flags": b["flags"]}
        for b in blocks if b.get("flags")
    ]
    if degraded:
        fp["degraded"] = degraded
    return fp


def format_unknown_warning(fp: dict[str, Any]) -> str:
    """给 stderr 用的一行告警（unknown > 0 时）。"""
    labels = [b["name"] for b in fp.get("blocks", ()) if b["kind"] == "unknown"]
    return (
        f"[injection_fingerprint] ⚠️ {len(labels)} 个信封无法归类，"
        f"注入指纹对这些块是瞎的：{'; '.join(labels[:5])}"
        + ("…" if len(labels) > 5 else "")
        + " → 新增注入点须复用既有信封格式，"
        "或在 classify_envelope 加判别分支并登记进 "
        "tests/engine/test_injection_fingerprint.py 的 _REGISTRY。"
    )
