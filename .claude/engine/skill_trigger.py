"""
skill_trigger.py — keyword 触发器机制：skill 自动按需召回

补 wikilink resolver 的洞：现状 `dev_backend/_load_task_skills` 依赖 task 文本
里的 `[[B?-...]]` 显式声明，但 TL 派活时不会主动写。本模块让 skill 自己声明
trigger（keywords / file_patterns / always），loader 扫 task_text + upstream
files + 项目代码自动召回。

设计要点：
- 与 wikilink 并存，调用方自行 union 去重
- trigger 缺失 = fail-closed（不加载）；显式声明 > 隐式
- skill 文件正文已用 `## 核心约束` h2 分段，loader 默认抽该段
"""

from __future__ import annotations

import fnmatch
import sys
from pathlib import Path
from typing import Iterable

from .obsidian_io import split_frontmatter


# ── 1. 核心约束章节抽取（与 wikilink._extract_section 同语义）───────
def extract_core_section(content: str) -> str:
    """从 skill 正文抽取 `## 核心约束` 章节；未命中则回退全文。

    匹配规则：h1-h6 标题文本**包含**「核心约束」即命中（大小写不敏感），
    遇到同级或更高级标题退出。与 `engine.wikilink._extract_section` 一致，
    但本地独立实现避免引私有 API。
    """
    lines = content.splitlines(keepends=True)
    out: list[str] = []
    in_section = False
    current_level = 0
    key = "核心约束"
    for line in lines:
        heading = None
        for lvl in range(1, 7):
            prefix = "#" * lvl + " "
            if line.startswith(prefix):
                heading = (lvl, line[lvl + 1:].strip())
                break
        if heading:
            lvl, title = heading
            is_target = key in title
            if is_target and not in_section:
                in_section = True
                current_level = lvl
                out.append(line)
            elif in_section and lvl <= current_level:
                in_section = False
            elif in_section:
                out.append(line)
        elif in_section:
            out.append(line)
    if not out:
        return content
    return "".join(out)


# ── 2. 单 skill 触发判断 ────────────────────────────────────────────
def match_skill(
    skill_path: Path,
    task_text: str,
    upstream_text: str = "",
    project_code_root: Path | None = None,
) -> tuple[bool, str]:
    """判断单个 skill 是否被触发，返回 (命中, 触发原因日志)。

    优先级：always > keywords > file_patterns。任一命中即触发。
    trigger 字段缺失 = fail-closed（不加载，返回 False）。
    """
    try:
        content = skill_path.read_text(encoding="utf-8")
    except OSError as e:
        print(f"[skill_trigger] ⚠️ 读 {skill_path.name} 失败：{e}", file=sys.stderr)
        return False, ""

    fm, _ = split_frontmatter(content)
    trigger = fm.get("trigger") if isinstance(fm, dict) else None
    if not isinstance(trigger, dict):
        return False, "no-trigger"

    if trigger.get("always") is True:
        return True, "always"

    keywords = trigger.get("keywords") or []
    if isinstance(keywords, list) and keywords:
        haystack = (task_text + "\n" + upstream_text).lower()
        for kw in keywords:
            if not isinstance(kw, str):
                continue
            if kw.lower() in haystack:
                return True, f"keyword:{kw}"

    file_patterns = trigger.get("file_patterns") or []
    if (
        isinstance(file_patterns, list) and file_patterns
        and project_code_root is not None and project_code_root.is_dir()
    ):
        for fp in file_patterns:
            if not isinstance(fp, str):
                continue
            for actual in project_code_root.rglob("*"):
                if not actual.is_file():
                    continue
                rel = actual.relative_to(project_code_root).as_posix()
                if fnmatch.fnmatch(rel, fp):
                    return True, f"file_pattern:{fp}"

    return False, ""


# ── 3. 角色目录扫描 ────────────────────────────────────────────────
def discover_role_skills(
    role_dir: Path,
    task_text: str,
    upstream_text: str = "",
    project_code_root: Path | None = None,
) -> list[tuple[Path, str]]:
    """扫 role_dir 下所有 *.md，按 frontmatter.trigger 过滤命中的 skill。

    返回 [(skill_path, 触发原因), ...]，按文件名排序保证可复现。
    role_dir 不存在或为空时返回空列表（不抛错，便于跨项目跑）。
    """
    if not role_dir.is_dir():
        return []

    hits: list[tuple[Path, str]] = []
    for skill_path in sorted(role_dir.glob("*.md")):
        if skill_path.name.startswith(".") or skill_path.name.startswith("_"):
            continue
        matched, reason = match_skill(
            skill_path, task_text, upstream_text, project_code_root,
        )
        if matched:
            hits.append((skill_path, reason))
    return hits


# ── 4. 渲染 skill block（拼成可注入 user_prompt 的文本）────────────
def render_triggered_block(
    hits: Iterable[tuple[Path, str]],
    *,
    max_chars_per_skill: int = 3000,
    total_char_budget: int = 12_000,
) -> tuple[str, list[str]]:
    """把 discover_role_skills 的命中结果渲染成 user_prompt 段。

    返回 (skill_block, loaded_stems)。skill_block 默认抽 `## 核心约束` 段。
    双层预算（与 wikilink resolver 对齐）：
    - max_chars_per_skill：单个 skill 截断上限（防单点失控）
    - total_char_budget：所有 skill 总和上限（防 always=true 风暴撑爆 prompt）
    超 total_char_budget 后剩余 skill 跳过 + stderr 警告（治理可见）。
    """
    parts: list[str] = []
    loaded_stems: list[str] = []
    used_chars = 0
    skipped: list[str] = []

    for skill_path, reason in hits:
        try:
            raw = skill_path.read_text(encoding="utf-8")
        except OSError as e:
            print(f"[skill_trigger] ⚠️ 读 {skill_path.name} 失败：{e}", file=sys.stderr)
            continue
        _, body = split_frontmatter(raw)
        core = extract_core_section(body).strip()
        if len(core) > max_chars_per_skill:
            core = core[:max_chars_per_skill] + (
                f"\n\n…（截断：原文 {len(core)} 字符，本次取前 {max_chars_per_skill}）"
            )

        # total_char_budget 兜底：累计超预算则跳过本条 + 后续（保前面已加载的稳定性）
        if used_chars + len(core) > total_char_budget:
            skipped.append(skill_path.stem)
            continue

        parts.append(
            f"=== Skill (auto-trigger:{reason}): [[{skill_path.stem}]] ===\n{core}"
        )
        loaded_stems.append(skill_path.stem)
        used_chars += len(core)

    if skipped:
        print(
            f"[skill_trigger] ⚠️ total_char_budget={total_char_budget} 用满，"
            f"跳过 {len(skipped)} 个 skill：{', '.join(skipped)}",
            file=sys.stderr,
        )

    if not parts:
        return "", []
    skill_block = (
        "\n\n## 自动触发技能（按 frontmatter.trigger 命中）\n\n"
        + "\n\n".join(parts)
        + "\n"
    )
    return skill_block, loaded_stems
