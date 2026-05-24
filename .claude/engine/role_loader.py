"""
role_loader.py — 把 00-系统/角色基因/ 下的角色笔记加载成 Role 对象。

支持按"中文角色名"或"aliases 中任意别名"查找（兼容旧 skill_id 如
chief_architect / dev_backend，引擎切换 vault 后无需大规模改 main.py）。

DYNAMIC_START/END 标记**保留在** body 中，由 build_system_prompt 端拼接。
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from .config import VAULT_ROOT, role_genes_dir
from .obsidian_io import read_note, split_frontmatter


class RoleNotFound(KeyError):
    pass


@dataclass(frozen=True)
class Role:
    """角色"定义"，纯静态。

    运行时状态（status / last_run / consecutive_failures / error_count
    / last_output_path）拆到 `00-系统/.runtime-state/<role>.json`，
    通过 engine.state 模块读写。Role 对象不再持有这些字段。
    """
    # 标识
    name: str                          # frontmatter.role，中文角色名
    aliases: tuple[str, ...]           # frontmatter.aliases
    note_path: Path                    # 角色笔记的绝对路径

    # 元数据
    domain: str
    skills: tuple[str, ...]
    style: str
    model: str
    max_tokens: int
    tools: tuple[str, ...]
    version: str

    # 关系图
    upstream: tuple[str, ...]          # 数据流上游
    downstream: tuple[str, ...]        # 数据流下游
    monitors: tuple[str, ...]          # 监控的下游（可触发补丁）

    # 输入输出（含 {project} 占位符的 vault 路径模板）
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]

    # 外迁的技能引用（vault 相对路径），load_role 时已 inline 拼到 body 末尾
    skill_refs: tuple[str, ...]

    # 规则章节按需引用（wikilink 字符串），形如 "[[架构分解规则#§3 分解步骤]]"。
    # **不**拼进 body（避免膨胀 system_prompt 触发 audit 阈值）；调用方自己
    # 用 engine.wikilink.expand_wikilinks 展开后注入到 user_prompt 的 context。
    # 与 skill_refs 的差异：skill 全文进 system 用于稳定方法论；
    # rule_refs 按章节进 user 用于任务相关的规则节选。
    rule_refs: tuple[str, ...]

    # 笔记正文（含 DYNAMIC 区域 + 已 inline 的 skill 内容）
    body: str

    # 完整 frontmatter（debug/扩展用）
    frontmatter: dict = field(repr=False)

    # token 预算 override（可选，单位 tokens）；缺省 None 则走 engine.llm 默认窗口百分比
    # 用法：角色 frontmatter 显式声明 `budget_input_tokens: 80000`，engine.llm 入口
    # 护栏会按此值做 RAISE（warn = 60% × 此值），代替 _TOTAL_RAISE_RATIO 百分比
    budget_input_tokens: int | None = None

    @property
    def all_names(self) -> tuple[str, ...]:
        """name + aliases 的并集，用于查找匹配。"""
        return (self.name, *self.aliases)


# ── 内部：从笔记构造 Role ─────────────────────────────────
def _seq(value, default=()) -> tuple[str, ...]:
    """把 frontmatter 字段规范化成 str 元组。None / 缺失 → 默认。"""
    if value is None:
        return tuple(default)
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(x) for x in value if x is not None)
    return (str(value),)


def _resolve_skill_refs(refs: tuple[str, ...], vault_root: Path) -> str:
    """读取每个 skill 文件，去掉自身 frontmatter，用分隔符拼成单段。

    缺失文件 → 占位 `[SKILL MISSING: <path>]` + stderr 警告，不 fail。
    """
    if not refs:
        return ""
    parts: list[str] = []
    for ref in refs:
        rel = ref.strip()
        if not rel:
            continue
        path = (vault_root / rel).resolve()
        if not path.is_file():
            print(f"⚠️ skill_refs 缺文件：{rel}", file=sys.stderr)
            parts.append(f"=== Skill: {rel} ===\n[SKILL MISSING: {rel}]")
            continue
        try:
            raw = path.read_text(encoding="utf-8")
        except Exception as e:
            print(f"⚠️ skill_refs 读失败 {rel}：{e}", file=sys.stderr)
            parts.append(f"=== Skill: {rel} ===\n[SKILL READ ERROR: {e}]")
            continue
        _, sk_body = split_frontmatter(raw)
        parts.append(f"=== Skill: {rel} ===\n{sk_body.strip()}")
    if not parts:
        return ""
    return "\n\n## 引用技能（来自 skill_refs）\n\n" + "\n\n".join(parts)


def _build_role(note_path: Path) -> Role:
    content = read_note(note_path)
    fm, body = split_frontmatter(content)
    if not fm.get("role"):
        raise ValueError(f"{note_path} 缺少 frontmatter.role 字段")
    skill_refs = _seq(fm.get("skill_refs"))
    skill_block = _resolve_skill_refs(skill_refs, VAULT_ROOT) if skill_refs else ""
    body_with_skills = body + ("\n\n" + skill_block if skill_block else "")
    rule_refs = _seq(fm.get("rule_refs"))
    return Role(
        name=str(fm["role"]),
        aliases=_seq(fm.get("aliases")),
        note_path=note_path,
        domain=str(fm.get("domain", "")),
        skills=_seq(fm.get("skills")),
        style=str(fm.get("style", "")),
        model=str(fm.get("model", "claude-sonnet-4-6")),
        max_tokens=int(fm.get("max_tokens", 4096)),
        tools=_seq(fm.get("tools")),
        version=str(fm.get("version", "0.0.0")),
        upstream=_seq(fm.get("upstream")),
        downstream=_seq(fm.get("downstream")),
        monitors=_seq(fm.get("monitors")),
        inputs=_seq(fm.get("inputs")),
        outputs=_seq(fm.get("outputs")),
        skill_refs=skill_refs,
        rule_refs=rule_refs,
        body=body_with_skills,
        frontmatter=fm,
        budget_input_tokens=(int(fm["budget_input_tokens"])
                             if fm.get("budget_input_tokens") else None),
    )


# ── 公共 API ─────────────────────────────────────────────
@lru_cache(maxsize=1)
def _index() -> dict[str, Path]:
    """name/alias → note_path 的索引。

    单进程内缓存；如笔记被外部修改后想刷新，调用 invalidate_cache()。
    """
    idx: dict[str, Path] = {}
    for note in role_genes_dir().rglob("角色-*.md"):
        try:
            content = read_note(note)
            fm, _ = split_frontmatter(content)
        except Exception:
            continue
        name = fm.get("role")
        if not name:
            continue
        for key in (name, *(_seq(fm.get("aliases")) or ())):
            if key in idx and idx[key] != note:
                # 重名告警，但保留先到的（按 sorted 顺序）
                continue
            idx[key] = note
    return idx


def invalidate_cache() -> None:
    """清空 role_loader 的索引缓存（适合写入 frontmatter 后调用）。"""
    _index.cache_clear()


def load_role(name_or_alias: str) -> Role:
    idx = _index()
    note = idx.get(name_or_alias)
    if note is None:
        available = sorted(set(idx.keys()))
        raise RoleNotFound(
            f"未找到角色 '{name_or_alias}'。已知名称/别名：{available}"
        )
    return _build_role(note)


def list_roles() -> list[Role]:
    """加载 vault 中所有角色笔记，按角色名排序。"""
    seen: set[Path] = set()
    roles: list[Role] = []
    for note in role_genes_dir().rglob("角色-*.md"):
        if note in seen:
            continue
        seen.add(note)
        try:
            roles.append(_build_role(note))
        except Exception as e:
            # 容错：解析失败的笔记跳过，不阻塞整体
            print(f"⚠️ 跳过角色笔记 {note.name}：{e}")
    roles.sort(key=lambda r: r.name)
    return roles
