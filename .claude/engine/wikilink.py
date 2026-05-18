"""
engine/wikilink.py — Obsidian wikilink 解析与按需展开

设计契约（详见 vault `00-系统/规则/vault命名规则.md`）：
- vault 内"角色 / 工作流 / 规则 / skill / 项目记录"等命名空间的 stem 全局唯一
- 项目产出（`10-项目/<project>/PRD.md` 等）多项目同名是常态，**必须用完整路径** wikilink
  引用，stem 索引主动排除 `10-项目/*/` 下的笔记，命中失败抛错而非静默选第一个
- wikilink resolver 是"声明式按需加载"：上游写 `[[X]]` 标注依赖，引擎按 filter 决定展开

三层 + 一个入口：
  parse_wikilinks(text)              ── 纯字符串（无 I/O）
  resolve_target(target, vault_root) ── 文件名 → Path（带 lru_cache 全 vault 索引）
  load_wikilink(wl, path)            ── 读文件 + 章节抽取 + 截断
  expand_wikilinks(text, ...)        ── 串起来 + filter + 预算 + 深度 + 环检测

调用方拿到 `ExpandResult.expansions` 后**自行决定怎么拼到 prompt**，
本模块不直接 mutate 原文。
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Callable, Literal

from .config import VAULT_ROOT


# ── 数据类 ────────────────────────────────────────────────
@dataclass(frozen=True)
class Wikilink:
    """解析后的一条 wikilink。

    raw 形如 `[[X]]` / `[[X#section]]` / `[[X|alias]]` / `[[X#section|alias]]`，
    target 是去除 section/alias 后的文件名或路径字符串。
    """
    raw: str
    target: str           # "B5-空集守卫" 或 "20-知识/角色技能/后端工程师/B5-空集守卫"
    section: str | None   # `#section` 部分；None 表示无锚点
    alias: str | None     # `|alias` 部分；None 表示无别名
    span: tuple[int, int]  # 在源文本中的 [start, end) 字符位置


@dataclass(frozen=True)
class Expansion:
    """单条 wikilink 的展开结果。"""
    wikilink: Wikilink
    path: Path | None       # None = 未解析成功
    content: str | None     # 解析后注入用的文本（含章节抽取与截断）；None = 未读取
    depth: int              # 当前展开层级（0 = 顶层）
    reason: str             # "ok" / "unresolved" / "max_depth" / "cycle" / "filter_skip" / "budget" / "read_error"


@dataclass
class ExpandResult:
    expansions: list[Expansion]
    total_chars: int = 0
    cycles_detected: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)


# ── 1. parse：纯字符串解析 ───────────────────────────────
# Obsidian wikilink 语法（本模块支持）：
#   [[target]]
#   [[target#section]]
#   [[target|alias]]
#   [[target#section|alias]]
# target 可含 "/"（完整路径）或不含（按 stem 解析）。
# 明确不支持：[[target#^block-id]]（block reference）/ 嵌入 ![[X]]（图片/笔记嵌入）
_WIKILINK_RE = re.compile(r"\[\[(?P<inner>[^\[\]\n]+?)\]\]")


def parse_wikilinks(text: str) -> list[Wikilink]:
    """从 markdown 文本提取所有 wikilink。纯函数，无 I/O。

    `![[X]]`（嵌入式）会被识别但 raw 保留 `![[X]]` 形态。当前不为嵌入式做特殊处理；
    调用方如要区分，可在 filter 里检查 `wl.raw.startswith("![[")`.
    """
    out: list[Wikilink] = []
    for m in _WIKILINK_RE.finditer(text):
        inner = m.group("inner").strip()
        if not inner:
            continue
        # 切 alias（`|` 在最右侧；section 可能含中文标点但不含 `|`）
        if "|" in inner:
            link_part, alias = inner.split("|", 1)
            link_part = link_part.strip()
            alias = alias.strip() or None
        else:
            link_part, alias = inner, None

        # 切 section
        if "#" in link_part:
            target, section = link_part.split("#", 1)
            target = target.strip()
            section = section.strip() or None
            # 拒绝 block reference：以 `^` 开头的 section
            if section and section.startswith("^"):
                # 降级：当作普通 section（按 `_extract_section` 大概率匹配不到 → 全文 fallback）
                # 同时打个轻量警告
                print(
                    f"[wikilink] ⚠️ 不支持 block reference (`#^id`)，"
                    f"按普通 section 处理: {m.group(0)}",
                    file=sys.stderr,
                )
        else:
            target, section = link_part.strip(), None

        # 补 raw 起始位置（含可能的前置 `!`）
        start = m.start()
        if start > 0 and text[start - 1] == "!":
            raw = text[start - 1: m.end()]
            span = (start - 1, m.end())
        else:
            raw = m.group(0)
            span = (start, m.end())

        out.append(Wikilink(
            raw=raw, target=target, section=section, alias=alias, span=span,
        ))
    return out


# ── 2. resolve：文件名 → Path（vault stem 索引）─────────────
# 排除目录：这些下面的笔记不进 stem 索引；引用必须用完整路径
_STEM_EXCLUDED_PREFIXES = (
    "10-项目/",        # 项目产出多项目同名（PRD/系统设计/...）
    "99-临时/",        # 临时文件不参与 wikilink 命名空间
    "00-系统/.runtime-state/",   # 运行时状态
)


@lru_cache(maxsize=1)
def _stem_index() -> dict[str, list[Path]]:
    """vault 全局 stem → 路径列表索引；首次访问触发扫描。

    返回 list 而非单 Path：方便 resolve_target 在命中 ≥2 个时报错（治理失败硬告警）。
    """
    idx: dict[str, list[Path]] = {}
    root = VAULT_ROOT
    if not root.is_dir():
        return idx
    for p in root.rglob("*.md"):
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        rel_posix = rel.as_posix()
        if any(rel_posix.startswith(pre) for pre in _STEM_EXCLUDED_PREFIXES):
            continue
        idx.setdefault(p.stem, []).append(p)
    return idx


def invalidate_cache() -> None:
    """清空 wikilink stem 索引缓存。

    适用时机：
    - 长 running 任务（讨论场、复盘者、整理师）批处理多笔记前
    - 测试中 monkeypatch VAULT_ROOT 之后
    """
    _stem_index.cache_clear()


class DuplicateStemError(RuntimeError):
    """全 vault 出现重名 stem——命名规则被破坏，必须人工介入修复。"""


def resolve_target(target: str, vault_root: Path | None = None) -> Path | None:
    """把 wikilink 的 target 字符串解析为绝对路径。

    解析顺序：
    1. 含 `/` → 视为 vault 相对路径，直接拼接（含 `.md` 后缀也支持）
    2. 无 `/` → 走 stem 索引：命中 0 个 None、命中 1 个返回、命中 ≥2 raise

    vault_root 可选，主要给单测 monkeypatch 用；生产代码不传，复用模块级 VAULT_ROOT。
    """
    root = vault_root or VAULT_ROOT

    # 完整路径模式
    if "/" in target:
        rel = target if target.endswith(".md") else target + ".md"
        cand = (root / rel).resolve()
        try:
            cand.relative_to(root)
        except ValueError:
            return None  # 越界，拒绝
        return cand if cand.is_file() else None

    # stem 模式：使用全局索引
    idx = _stem_index()
    matches = idx.get(target, [])
    if not matches:
        return None
    if len(matches) >= 2:
        rel_list = sorted(str(p.relative_to(root)) for p in matches)
        raise DuplicateStemError(
            f"wikilink stem 重名: '{target}'\n"
            f"命中 {len(matches)} 个文件:\n  - "
            + "\n  - ".join(rel_list)
            + "\n命名规则要求 stem 全 vault 唯一；请重命名或用完整路径 wikilink 消歧。"
        )
    return matches[0]


# ── 3. load：读文件 + 章节抽取 + 截断 ────────────────────
def _extract_section(content: str, section: str) -> tuple[str, bool]:
    """从 Markdown 文档抽取指定章节。返回 (text, hit)。

    匹配规则与 skills.common._extract_sections 对齐：标题文字**包含**关键词
    （大小写不敏感）即命中；遇到同级或更高级标题退出。
    返回 hit=False 时调用方应回退全文。
    """
    if not section:
        return content, True
    lines = content.splitlines(keepends=True)
    out: list[str] = []
    in_section = False
    current_level = 0
    key = section.lower()
    for line in lines:
        heading = None
        for lvl in range(1, 7):
            prefix = "#" * lvl + " "
            if line.startswith(prefix):
                heading = (lvl, line[lvl + 1:].strip())
                break
        if heading:
            lvl, title = heading
            is_target = key in title.lower()
            if is_target and not in_section:
                in_section = True
                current_level = lvl
                out.append(line)
            elif in_section and lvl <= current_level:
                # 同级或更高级标题，退出
                in_section = False
            elif in_section:
                out.append(line)
        elif in_section:
            out.append(line)
    if not out:
        return content, False
    return "".join(out), True


def load_wikilink(
    wl: Wikilink, path: Path,
    *, max_chars: int = 3000,
) -> str:
    """读 path 并返回应注入的内容（已做 section 抽取与截断）。

    section 未命中时回退全文 + 警告；超长截断尾部并追加省略提示。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return f"[WIKILINK READ ERROR: {path.name}: {e}]"

    if wl.section:
        extracted, hit = _extract_section(text, wl.section)
        if not hit:
            print(
                f"[wikilink] ⚠️ 章节 '{wl.section}' 在 {path.name} 未命中，回退全文",
                file=sys.stderr,
            )
        text = extracted

    if len(text) > max_chars:
        text = text[:max_chars] + (
            f"\n\n…（截断：{path.name} 原文 {len(text)} 字符，本次展开取前 {max_chars}）"
        )
    return text


# ── 4. expand：主入口，串起 1-3 + filter + 预算 + 深度 ──
def expand_wikilinks(
    text: str,
    vault_root: Path | None = None,
    *,
    filter: Callable[[Wikilink], bool],   # 必填，无默认（防意外全展开）
    max_chars_per_link: int = 3000,
    total_char_budget: int = 20_000,
    max_depth: int = 0,
    on_unresolved: Literal["skip", "warn", "raise"] = "warn",
) -> ExpandResult:
    """从一段文本展开 wikilink，返回结构化结果。

    `filter` 是必填回调（无默认）：调用方必须显式声明"展开哪些 link"。
    传 `lambda wl: True` 才会全展开；正常用法应按 target 前缀 / section / alias
    精确过滤，例如 `lambda wl: re.match(r"^B\\d+-", wl.target)` 只展开 backend skill。

    递归展开（max_depth > 0 时）：展开后的内容里如有 wikilink，会作为下一层
    输入再次 parse + 展开；用 `(target, section)` 元组的 visited set 防环。

    返回 `ExpandResult` 不修改原文；调用方按需拼到 prompt。
    """
    root = vault_root or VAULT_ROOT
    result = ExpandResult(expansions=[])
    # 环检测：跨递归共享，键 = (resolved_path_str, section)
    visited: set[tuple[str, str | None]] = set()
    # 预算：跨递归共享
    budget_remaining = total_char_budget

    def _recurse(text_: str, depth: int) -> None:
        nonlocal budget_remaining
        for wl in parse_wikilinks(text_):
            # filter
            if not filter(wl):
                result.expansions.append(Expansion(
                    wikilink=wl, path=None, content=None,
                    depth=depth, reason="filter_skip",
                ))
                continue

            # 解析路径
            try:
                target_path = resolve_target(wl.target, root)
            except DuplicateStemError:
                # 命名规则破坏 → 不吞，直接抛给调用方
                raise

            if target_path is None:
                msg = f"未解析: {wl.raw}"
                result.unresolved.append(wl.target)
                if on_unresolved == "raise":
                    raise FileNotFoundError(msg)
                if on_unresolved == "warn":
                    print(f"[wikilink] ⚠️ {msg}", file=sys.stderr)
                result.expansions.append(Expansion(
                    wikilink=wl, path=None, content=None,
                    depth=depth, reason="unresolved",
                ))
                continue

            # 环检测
            cycle_key = (str(target_path), wl.section)
            if cycle_key in visited:
                result.cycles_detected.append(wl.raw)
                result.expansions.append(Expansion(
                    wikilink=wl, path=target_path, content=None,
                    depth=depth, reason="cycle",
                ))
                continue

            # 预算检查（先估算单 link 上限；实际可能更小）
            if budget_remaining <= 0:
                result.expansions.append(Expansion(
                    wikilink=wl, path=target_path, content=None,
                    depth=depth, reason="budget",
                ))
                continue

            visited.add(cycle_key)
            content = load_wikilink(
                wl, target_path,
                max_chars=min(max_chars_per_link, budget_remaining),
            )
            consumed = len(content)
            budget_remaining -= consumed
            result.total_chars += consumed

            result.expansions.append(Expansion(
                wikilink=wl, path=target_path, content=content,
                depth=depth, reason="ok",
            ))

            # 递归（受 max_depth 限制）
            if depth < max_depth:
                _recurse(content, depth + 1)
            else:
                # 已达 max_depth：报告内层 wikilink（filter 命中的）但不读取
                # 注意：这里只解析、不读取——避免预算被无效占用
                for inner_wl in parse_wikilinks(content):
                    if not filter(inner_wl):
                        continue
                    result.expansions.append(Expansion(
                        wikilink=inner_wl, path=None, content=None,
                        depth=depth + 1, reason="max_depth",
                    ))

    _recurse(text, depth=0)
    return result
