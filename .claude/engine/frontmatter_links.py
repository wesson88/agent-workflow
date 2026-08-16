"""产物 frontmatter 链接字段规范化 + 体检（A/B/C 三类静默错值治理）。

## 背景

2026-08-15 vault 元数据体检把 frontmatter 链接写法的错误分成三类
（见 vault [[Obsidian可视化仪表盘建设-2026-08-15]]）：

- **A 类** `key: [[a]], [[b]]`（多链接未加引号）→ YAML 解析直接失败，
  该篇 Dataview 全盲
- **B 类** `key: [[a]]`（单链接未加引号）→ 被读成嵌套 list `[['a']]`，
  **不报错但值已错**
- **C 类** `key: "[[a]], [[b]]"`（多链接塞进一个字符串）→ 解析成一个
  字符串而不是两个链接，同样**不报错但值已错**

## 根因（2026-08-16 定位，见 [[待办清单严重项梳理-2026-08-16]] S1）

引擎全程**不生成也不校验** frontmatter —— LLM 在 FILE 块里连 frontmatter
一起产出，`parse_claude_output_to_files` 解析后原样落盘。

而 vault 里唯一写了产物 frontmatter 约定的 [[产物schema]] `## 通用规则`
是**章节级 rule_refs 的漏网章节**：8 个音乐角色的 rule_refs 一律只引
`[[产物schema#N. {自己的产物}]]`，`## 通用规则` 从未进过任何 prompt。

铁证：`## 通用规则` 要求 frontmatter 必含 `produced_by`，而 8 个音乐项目
133 份产物里该字段出现 **0 次**（实际用的是 `role`，72 次）。

于是 frontmatter 写法完全靠 LLM 模仿上游输入文件——单链接 `"[[x]]"` 的
正确写法被学会了，一推广到多链接就成了 C 类。

## 治理分层（[[feedback_contract_three_layers]]：三层缺一即失效）

1. **数据层**：[[产物schema]] `## 通用规则` 补链接字段写法（canonical 声明）
2. **提示词层**：`role_runner._build_user_prompt` 注入 frontmatter 契约
3. **主循环层**：本模块 —— 落盘前确定性规范化 + audit 留痕

## 为什么是"规范化"而不是"判 failed 重跑"或"只 warn"

A/B/C 三类都是**纯机械的格式问题**，LLM 的语义意图无歧义（就是一串链接），
引擎能确定性改对：
- 判 `failed` 让角色重跑要多烧一次 LLM 调用，且重跑未必写对（写法约定本就
  没进过 prompt）
- 只 warn 会落进"静默失败"家族 —— 这正是本类 bug 能在产出侧存活的原因

**安全边界**：只有**纯链接**的值才改写。夹带散文的值（如 pain-radar 的
`source: 综合 [[business_brief]] + [[brainstorm-spider-tools]] v0.0–v0.3`）
原样不动 —— 那是句子不是链接列表，改写会破坏语义。
"""

from __future__ import annotations

import re

# frontmatter 块：仅匹配文件**开头**的第一个 `---` ... `---`
_FM_RE = re.compile(r"\A(---[ \t]*\r?\n)(.*?)(\r?\n---[ \t]*)(\r?\n|\Z)", re.DOTALL)

# `key: value` 行（value 非空）。缩进保留，供 block list 对齐用。
_KEY_LINE_RE = re.compile(r"\A([ \t]*)([^\s:#][^:]*):[ \t]+(\S.*?)[ \t]*\Z")

# block list 的项行 `- value`
_ITEM_LINE_RE = re.compile(r"\A([ \t]*)-[ \t]+(\S.*?)[ \t]*\Z")

# 单个 wikilink，且**整个值只有它**
_SINGLE_LINK_RE = re.compile(r"\A\[\[[^\[\]]+\]\]\Z")

# 纯链接列表：链接之间只允许分隔符（, ， 、）与空白，不允许任何其它字符。
# 这条正则就是上面说的"安全边界"——散文句匹配不上。
_PURE_LINK_LIST_RE = re.compile(
    r"\A\[\[[^\[\]]+\]\](?:[ \t]*[,，、][ \t]*\[\[[^\[\]]+\]\])+\Z"
)

_LINK_RE = re.compile(r"\[\[[^\[\]]+\]\]")


def _unquote(value: str) -> tuple[str, bool]:
    """剥掉成对的外层引号。返回 (内容, 是否原本带引号)。

    只在"首尾同引号且内部无同款引号"时剥离，避免把 `"a" 与 "b"` 这类
    值误判成一个被引号包裹的整体。
    """
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        inner = value[1:-1]
        if value[0] not in inner:
            return inner, True
    return value, False


def _quote(link: str) -> str:
    return f'"{link}"'


def normalize_frontmatter_links(content: str) -> tuple[str, list[str]]:
    """规范化 frontmatter 里的链接字段写法。

    两种确定性修正（语义等价，仅改 YAML 表达形态）：

    - **多链接 → block list**：`key: "[[a]], [[b]]"` 或 `key: [[a]], [[b]]`
      改写成每项独立成行、独立加引号的 block list（治 A 类与 C 类）
    - **单链接 → 加引号**：`key: [[a]]` → `key: "[[a]]"`，
      block list 项 `- [[a]]` → `- "[[a]]"`（治 B 类）

    只处理文件开头的第一个 frontmatter 块；无 frontmatter 时原样返回。

    返回 `(新内容, 修正说明列表)`。无修正时说明列表为空且内容对象不变。
    """
    m = _FM_RE.match(content)
    if not m:
        return content, []

    open_tag, fm_text, close_tag, tail = m.groups()
    rest = content[m.end():]

    fixes: list[str] = []
    out_lines: list[str] = []

    for line in fm_text.splitlines():
        km = _KEY_LINE_RE.match(line)
        if km:
            indent, key, raw_value = km.groups()
            value, was_quoted = _unquote(raw_value)
            if _PURE_LINK_LIST_RE.match(value):
                links = _LINK_RE.findall(value)
                out_lines.append(f"{indent}{key}:")
                out_lines.extend(f"{indent}  - {_quote(lk)}" for lk in links)
                fixes.append(
                    f"{key}: {len(links)} 个链接由单值改写为 block list"
                    f"（原写法{'加了引号被读成一个字符串' if was_quoted else '未加引号会导致 YAML 解析失败'}）"
                )
                continue
            if _SINGLE_LINK_RE.match(value) and not was_quoted:
                out_lines.append(f"{indent}{key}: {_quote(value)}")
                fixes.append(f"{key}: 单链接补引号（原写法会被读成嵌套 list）")
                continue
            out_lines.append(line)
            continue

        im = _ITEM_LINE_RE.match(line)
        if im:
            indent, raw_value = im.groups()
            value, was_quoted = _unquote(raw_value)
            if _SINGLE_LINK_RE.match(value) and not was_quoted:
                out_lines.append(f"{indent}- {_quote(value)}")
                fixes.append("block list 项补引号（原写法会被读成嵌套 list）")
                continue

        out_lines.append(line)

    if not fixes:
        return content, []

    new_fm = "\n".join(out_lines)
    return f"{open_tag}{new_fm}{close_tag}{tail}{rest}", fixes


def check_frontmatter(content: str) -> list[str]:
    """体检：返回问题描述列表（空列表 = 干净）。

    覆盖 `normalize_frontmatter_links` **修不了**的结构性问题：

    - frontmatter YAML 解析失败（规范化后仍失败，说明不是链接写法问题）
    - 多个 frontmatter 块（`---` 块紧跟着又一个 `---` 块 —— 只有第一个
      会被 Obsidian 当元数据，第二块整个变成正文，其中的字段静默失效）
    - 规范化后仍残留的多链接字符串值（散文夹链接，需人工判断）
    """
    import yaml

    problems: list[str] = []
    m = _FM_RE.match(content)
    if not m:
        return problems

    fm_text = m.group(2)
    rest = content[m.end():]

    if _FM_RE.match(rest.lstrip("\n")):
        problems.append(
            "存在第 2 个 frontmatter 块：只有第一个会被当元数据，"
            "第二块整个落进正文，其中字段静默失效"
        )

    try:
        data = yaml.safe_load(fm_text)
    except yaml.YAMLError as e:
        problems.append(f"frontmatter YAML 解析失败：{str(e).splitlines()[0]}")
        return problems

    if not isinstance(data, dict):
        return problems

    for key, value in data.items():
        values = value if isinstance(value, list) else [value]
        for v in values:
            if isinstance(v, list):
                problems.append(f"{key}: 值是嵌套 list（B 类），链接未加引号")
                break
            if isinstance(v, str) and len(_LINK_RE.findall(v)) >= 2:
                # 纯链接列表会被 normalize_frontmatter_links 改写掉，所以在
                # 落盘路径上走到这里的一定是夹带散文的值（需人工判断）；
                # 本函数单独当扫描器用时，则是尚未规范化的存量。
                problems.append(
                    f"{key}: 一个字符串里有多个链接（C 类），"
                    f"Dataview 读到的是一个字符串而不是多个链接"
                )
                break

    return problems
