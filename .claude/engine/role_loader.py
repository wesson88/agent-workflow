"""
role_loader.py — 把 00-系统/角色基因/ 下的角色笔记加载成 Role 对象。

支持按"中文角色名"或"aliases 中任意别名"查找（兼容旧 skill_id 如
chief_architect / dev_backend，引擎切换 vault 后无需大规模改 main.py）。

DYNAMIC_START/END 标记**保留在** body 中，由 build_system_prompt 端拼接。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from .config import role_genes_dir
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

    # 笔记正文（含 DYNAMIC 区域，未做替换）
    body: str

    # 完整 frontmatter（debug/扩展用）
    frontmatter: dict = field(repr=False)

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


def _build_role(note_path: Path) -> Role:
    content = read_note(note_path)
    fm, body = split_frontmatter(content)
    if not fm.get("role"):
        raise ValueError(f"{note_path} 缺少 frontmatter.role 字段")
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
        body=body,
        frontmatter=fm,
    )


# ── 公共 API ─────────────────────────────────────────────
@lru_cache(maxsize=1)
def _index() -> dict[str, Path]:
    """name/alias → note_path 的索引。

    单进程内缓存；如笔记被外部修改后想刷新，调用 invalidate_cache()。
    """
    idx: dict[str, Path] = {}
    for note in role_genes_dir().glob("角色-*.md"):
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
    for note in role_genes_dir().glob("角色-*.md"):
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
