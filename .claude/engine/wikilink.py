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
    # 2026-07-18 ingest_check 存量扫描暴露的结构性碰撞（每新增 capability
    # 必产生一对同名 依赖清单.md/触发规则.md）；capability 引用本就走
    # `[[<root>/manifest]]` 完整路径约定，不依赖 bare-stem
    "20-知识/能力注册表/",
    # 待办系统非知识命名空间（index.md 与任意目录 index 撞）
    "98-待办/",
    # 2026-08-11 Claude Code 技能：结构性碰撞，与上面 capability 同形——
    # 每新增一个技能必产生一个 `<技能名>/SKILL.md`，而文件名 `SKILL.md` 由
    # Claude Code 强制、不可改。技能引用本就走 `[[00-系统/可执行技能/<名>/SKILL|<名>]]`
    # 完整路径约定，不依赖 bare-stem。
    # ⚠️ 与 vault 规则 `00-系统/规则/vault命名规则.md` §2.12 成对，改一处必须改另一处。
    "00-系统/可执行技能/",
)


def _domain_rule_domain(rel_posix: str) -> str | None:
    """若路径形如 `00-系统/规则/<domain>/<file>.md` 返回 `<domain>`，否则 None。

    仅识别路径形态，不判定是否要排除。是否排除由 `_stem_index` 的跨域同名碰撞
    检测决定 —— 有跨域同名的才是真跨域 adapter（如 `复盘者-视角.md`），
    单域独有的（如 `创作简报.schema.md` 只在 `music/`）仍进 stem 索引。

    历史：原 `_is_domain_rule_adapter` 无差别排除所有 `00-系统/规则/<domain>/*`，
    导致音乐 `创作简报.schema` 等单域规则文件在 rule_refs bare-stem 引用时全部 `未解析`
    （参见 [[音乐L3实战-非SE机制差异-2026-07-11#R1]]）。
    """
    parts = rel_posix.split("/")
    if len(parts) >= 4 and parts[0] == "00-系统" and parts[1] == "规则":
        return parts[2]
    return None


@lru_cache(maxsize=1)
def _stem_index() -> dict[str, list[Path]]:
    """vault 全局 stem → 路径列表索引；首次访问触发扫描。

    返回 list 而非单 Path：方便 resolve_target 在命中 ≥2 个时报错（治理失败硬告警）。

    排除策略：
    - `_STEM_EXCLUDED_PREFIXES` 目录（项目/临时/运行时状态）无条件排除
    - `00-系统/规则/<domain>/*.md` 采用**碰撞检测**：同 stem 出现在 ≥ 2 个 domain 下
      视为跨域 adapter 排除；单域独有的仍保留在索引
    """
    root = VAULT_ROOT
    if not root.is_dir():
        return {}

    # phase 1: 收集所有候选（先不管跨域 adapter 排除）
    raw: dict[str, list[Path]] = {}
    domain_of: dict[Path, str | None] = {}
    for p in root.rglob("*.md"):
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        rel_posix = rel.as_posix()
        if any(rel_posix.startswith(pre) for pre in _STEM_EXCLUDED_PREFIXES):
            continue
        raw.setdefault(p.stem, []).append(p)
        domain_of[p] = _domain_rule_domain(rel_posix)

    # phase 2: 剔除真跨域 adapter（≥ 2 domain 同 stem）
    idx: dict[str, list[Path]] = {}
    for stem, paths in raw.items():
        domains = {domain_of[p] for p in paths if domain_of[p] is not None}
        if len(domains) >= 2:
            # 真跨域 adapter：全部路径都在 domain rule 下且分属 ≥ 2 domain → 排除
            all_in_domain_rule = all(domain_of[p] is not None for p in paths)
            if all_in_domain_rule:
                continue
        idx[stem] = paths
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
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*(.*)$")


def iter_lines_with_fence_state(lines):
    """逐行 yield `(line, in_fence)`，`in_fence=True` 表示该行属于代码围栏。**公共 API**。

    ## 为什么必须有这个（2026-08-16 加）

    规则文档普遍用 ```` ```markdown ```` 块展示**产出模板**，模板里带自己的
    `## 1. xxx` 标题。任何按标题切分的算法若不识别围栏，就会把模板标题当成
    文档的同级章节 —— 抽取当场中止。

    实测后果（修复前，`00-系统/规则/music/产物schema.md`）：8 个音乐角色的
    章节级 `rule_refs` 全部被截断，36 条里 14 条丢失 > 30%，合计应注入
    21531 chars 实际只到 4525（**丢 79%**）。最严重的「4. 曲作.md」只注入
    79/1088 chars（93%），LLM 拿到的"产物契约"实为「标题 + 一行位置 + 一个
    空的 ```markdown 开头」。且 `extract_section` 返回 `hit=True`，
    **不报错、不回退全文、零告警** —— 与 [[产物frontmatter链接写法-产出方治理-2026-08-16]]
    同属静默失效家族。

    ## 为何提为共享 API

    同一 bug 2026-08-13 已在 `role_auditor._split_sections` 修过一次（该处
    注释明写"不跳围栏的话模板里的 `## 1.` 会把真正的 §1 整个覆盖掉"），
    但修法**没有传播**到本文件与 `skills/input_reader.py`。本次一次性收口：
    三处共用本函数，杜绝第四份实现（参 [[feedback_contract_three_layers]]）。

    闭合规则按 CommonMark：闭合行需同字符、长度 ≥ 开启长度、**且不带 info
    string**。围栏标记行本身一律记为 `in_fence=True`（它不可能是标题）。
    """
    fence: tuple[str, int] | None = None
    for line in lines:
        m = _FENCE_RE.match(line)
        if m:
            marker, info = m.group(1), m.group(2).strip()
            ch, ln = marker[0], len(marker)
            if fence is None:
                fence = (ch, ln)
            elif ch == fence[0] and ln >= fence[1] and not info:
                fence = None
            yield line, True
            continue
        yield line, fence is not None


def extract_section(content: str, section: str) -> tuple[str, bool]:
    """从 Markdown 文档抽取指定章节。返回 (text, hit)。**公共 API**。

    匹配规则与 skills.input_reader._extract_sections 对齐：标题文字**包含**
    关键词（大小写不敏感）即命中；遇到同级或更高级标题退出。
    返回 hit=False 时调用方应回退全文。

    **代码围栏内的标题不参与切分**（2026-08-16 修，见
    `iter_lines_with_fence_state` docstring 的实测数据）。

    2026-07-18 评审去重：原为私有 `_extract_section`，skill_trigger 为避免
    引私有 API 复制了 30 行同款算法；现提为公共 API 供两处共用。
    """
    if not section:
        return content, True
    out: list[str] = []
    in_section = False
    current_level = 0
    key = section.lower()
    for line, in_fence in iter_lines_with_fence_state(content.splitlines(keepends=True)):
        heading = None
        if not in_fence:
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
        extracted, hit = extract_section(text, wl.section)
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
