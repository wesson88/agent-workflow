"""
rolling_brief_lint.py — rolling_brief.md 静态校验

依据 [[rolling-brief.schema]] §7 lint 规则：
- §7.1 必填章节（9 节按 §2 顺序）
- §7.3 条目子字段（每 list item 必须有 source + confidence）
- §7.4 强制 confidence（§1 用户事实 / §7 已回答问题 必须 high）
- §7.5 §5 已否决方向强制 reason
- §7.6 source 前缀必须匹配 §4 允许列表

API：
    validate_rolling_brief(text: str) -> list[str]
        返回错误清单。空列表 = 通过。

被消费方：
- tests/engine/test_rolling_brief_lint.py（静态用例）
- .claude/skills/brainstorm_scribe/main.py（产出后 audit）
"""

from __future__ import annotations

import re
from typing import Iterator


REQUIRED_SECTIONS = (
    "1. 用户已确认事实",
    "2. LLM 推断",
    "3. 已做决策",
    "4. 已保留方向",
    "5. 已否决方向",
    "6. 关键争议",
    "7. 已回答问题",
    "8. 未回答问题",
    "9. 下一轮焦点",
)

CONFIDENCE_VALUES = ("high", "medium", "low")

# §1 用户事实 / §7 已回答问题 必须 high
SECTIONS_REQUIRE_HIGH = ("1. 用户已确认事实", "7. 已回答问题")

# §5 已否决方向必须带 reason
SECTIONS_REQUIRE_REASON = ("5. 已否决方向",)

# §4 允许的 source 前缀（拆分多 source 后逐个验证）
SOURCE_PATTERN = re.compile(
    r"^("
    r"idea\.md"
    r"|user_answer-R\d+"
    r"|创意发散-R\d+\.md"
    r"|创意质询-R\d+\.md"
    r"|创意记录员-R\d+"
    r"|brainstorm_readiness-R\d+"
    r"|产品创意原型-R\d+"
    r")(#.*)?$"
)


_SECTION_TITLE_NORMALIZE = re.compile(r"^(?:§(\d+)|(\d+)[.、])\s*(.+?)\s*$")


def _normalize_section_title(raw_title: str) -> str:
    """H2 标题归一化：'§1 用户已确认事实' / '1、用户已确认事实' → '1. 用户已确认事实'。

    LLM 受 prompt 里 §N 简写影响常用 '## §1 xxx' 形式产出（schema §2 定义是 '## 1. xxx'）。
    lint 容忍这两种 + 中文顿号 '1、'，统一归一化后再做 REQUIRED_SECTIONS 匹配。
    """
    m = _SECTION_TITLE_NORMALIZE.match(raw_title)
    if not m:
        return raw_title
    num = m.group(1) or m.group(2)
    body = m.group(3)
    return f"{num}. {body}"


def _iter_sections(text: str) -> Iterator[tuple[str, list[str]]]:
    """拆 markdown 按 H2 (## 起首) 切节。yield (section_title, lines)。

    section_title 归一化（§N / N、 → N.），便于按 REQUIRED_SECTIONS 字面匹配。
    """
    current_title: str | None = None
    current_lines: list[str] = []
    for raw in text.splitlines():
        m = re.match(r"^##\s+(.*?)\s*$", raw)
        if m:
            if current_title is not None:
                yield current_title, current_lines
            current_title = _normalize_section_title(m.group(1).strip())
            current_lines = []
        else:
            if current_title is not None:
                current_lines.append(raw)
    if current_title is not None:
        yield current_title, current_lines


def _iter_items(section_lines: list[str]) -> Iterator[tuple[str, dict[str, str]]]:
    """从节内容里拆 list item（'- ' 起首）+ 缩进子字段（'  key: value'）。

    yield (item_text, subfields_dict)。
    """
    item_text: str | None = None
    subfields: dict[str, str] = {}
    for raw in section_lines:
        item_match = re.match(r"^-\s+(.*?)\s*$", raw)
        if item_match:
            if item_text is not None:
                yield item_text, subfields
            item_text = item_match.group(1).strip()
            subfields = {}
            continue
        sub_match = re.match(r"^\s{2,}([A-Za-z_]+):\s*(.+?)\s*$", raw)
        if sub_match and item_text is not None:
            subfields[sub_match.group(1).strip()] = sub_match.group(2).strip()
    if item_text is not None:
        yield item_text, subfields


def _validate_source(source: str) -> bool:
    """拆多 source（', ' 或 ' vs ' 分隔），逐个验证前缀。"""
    parts = re.split(r"\s*,\s*|\s+vs\s+", source)
    return all(SOURCE_PATTERN.match(p.strip()) for p in parts if p.strip())


def validate_rolling_brief(text: str) -> list[str]:
    """按 [[rolling-brief.schema]] §7 全规则校验。

    返回错误清单（空 = 通过）。规则编号对应 schema 文档 §7.x。
    """
    errs: list[str] = []

    sections = dict(_iter_sections(text))

    # §7.1 必填 9 节
    for required in REQUIRED_SECTIONS:
        if required not in sections:
            errs.append(f"[§7.1] 缺章节：## {required}")

    if errs:
        return errs

    for sec_title in REQUIRED_SECTIONS:
        sec_lines = sections[sec_title]
        for item_text, subfields in _iter_items(sec_lines):
            prefix = f"[§{sec_title.split('.')[0]}] '{item_text[:30]}...' "

            # §7.3 条目必须有 source + confidence
            if "source" not in subfields:
                errs.append(f"[§7.3] {prefix}缺 source 子字段")
            if "confidence" not in subfields:
                errs.append(f"[§7.3] {prefix}缺 confidence 子字段")
                continue

            conf = subfields["confidence"]
            if conf not in CONFIDENCE_VALUES:
                errs.append(
                    f"[§7.3] {prefix}confidence={conf!r} 非法"
                    f"（必须 ∈ {CONFIDENCE_VALUES}）"
                )

            # §7.4 强制 confidence high
            if sec_title in SECTIONS_REQUIRE_HIGH and conf != "high":
                errs.append(
                    f"[§7.4] {prefix}confidence={conf!r}，"
                    f"§{sec_title} 强制 high"
                )

            # §7.5 §5 已否决方向强制 reason
            if sec_title in SECTIONS_REQUIRE_REASON:
                if "reason" not in subfields or not subfields["reason"].strip():
                    errs.append(f"[§7.5] {prefix}§5 已否决方向必须有 reason 子字段")

            # §7.6 source 前缀
            if "source" in subfields:
                if not _validate_source(subfields["source"]):
                    errs.append(
                        f"[§7.6] {prefix}source={subfields['source']!r}"
                        f" 前缀不在 §4 允许列表"
                    )

    return errs
