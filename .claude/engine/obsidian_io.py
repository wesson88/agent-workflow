"""
obsidian_io.py — vault 文件读写工具（filesystem-only）。

设计取舍（Phase 2）：
- 不走 Obsidian Local REST API，全部走文件系统。理由：
  1. 无需 Obsidian 应用打开；
  2. 写操作无延迟；
  3. obsidian-git 插件已负责把改动同步到远端，本模块不重复造轮子。
- 后续如需"按链接图谱搜索""按 frontmatter 字段过滤"等高级查询，再加 REST 客户端。

所有路径参数都是 vault 相对路径（如 "00-系统/角色基因/角色-架构师.md"），
模块内部统一与 VAULT_ROOT 拼接，不接受绝对路径（防止越界写入）。
"""

from __future__ import annotations

import io
import os
import re
import time
import yaml
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Iterable

from .config import VAULT_ROOT


# ── ruamel.yaml round-trip 实例（用于 update_frontmatter，保留格式）──
# split_frontmatter 仍用 PyYAML 做轻量解析；只有写入时才需要 round-trip。
def _rt_yaml():
    """懒加载 ruamel.yaml RoundTripLoader/Dumper 实例。

    indent 配置匹配 vault 现有 frontmatter 风格：
      skills:
        - 系统设计    <- sequence 子项缩进 2，dash 前再缩进 2
        - 模块划分
    即 sequence=4, offset=2。
    """
    from ruamel.yaml import YAML
    y = YAML(typ="rt")
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)
    y.width = 4096          # 防止长行被自动折行
    y.allow_unicode = True
    return y


_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)


# ── 路径解析 ─────────────────────────────────────────────
def _resolve(rel_path: str | Path) -> Path:
    """把 vault 相对路径解析为绝对路径，并校验未越出 VAULT_ROOT。"""
    p = Path(rel_path)
    if p.is_absolute():
        # 允许传入绝对路径，但必须在 VAULT_ROOT 下
        try:
            p.resolve().relative_to(VAULT_ROOT)
        except ValueError:
            raise ValueError(f"路径越出 VAULT_ROOT：{p}")
        return p.resolve()
    abs_p = (VAULT_ROOT / p).resolve()
    try:
        abs_p.relative_to(VAULT_ROOT)
    except ValueError:
        raise ValueError(f"路径越出 VAULT_ROOT：{rel_path}")
    return abs_p


# ── 读 ───────────────────────────────────────────────────
def read_note(rel_path: str | Path) -> str:
    """读取 vault 内的笔记。文件不存在抛 FileNotFoundError。"""
    return _resolve(rel_path).read_text(encoding="utf-8")


def read_note_safe(rel_path: str | Path, default: str = "") -> str:
    """读取 vault 内的笔记，文件不存在返回 default。"""
    try:
        return read_note(rel_path)
    except FileNotFoundError:
        return default


def list_notes(scope: str | Path = "", pattern: str = "*.md") -> list[Path]:
    """列出 scope（vault 相对路径，可空）下匹配 pattern 的笔记，返回绝对路径。"""
    base = _resolve(scope) if scope else VAULT_ROOT
    if not base.is_dir():
        return []
    return sorted(base.rglob(pattern))


# ── 写（原子，含 Windows 锁文件重试）─────────────────────
_REPLACE_RETRY_ATTEMPTS = 5      # 包含首次尝试，总共 5 次
_REPLACE_RETRY_BASE_DELAY = 0.3  # 首次重试 0.3s，指数退避到 ~4.8s


def _atomic_replace_with_retry(tmp: str, dest: Path) -> None:
    """Windows 上 Obsidian / obsidian-git / Defender 等会偶发锁住目标文件，
    导致 os.replace 抛 PermissionError([WinError 5])。这里加指数退避重试。
    """
    for attempt in range(_REPLACE_RETRY_ATTEMPTS):
        try:
            os.replace(tmp, dest)
            return
        except PermissionError:
            if attempt == _REPLACE_RETRY_ATTEMPTS - 1:
                # 最后一次仍失败：清理 tmp 后向上抛
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            time.sleep(_REPLACE_RETRY_BASE_DELAY * (2 ** attempt))


def write_note(rel_path: str | Path, content: str) -> Path:
    """原子写入笔记。父目录自动创建。返回写入后的绝对路径。"""
    abs_p = _resolve(rel_path)
    abs_p.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w",
        dir=abs_p.parent,
        delete=False,
        encoding="utf-8",
        suffix=".tmp",
        newline="\n",
    ) as tf:
        tf.write(content)
        tmp = tf.name
    _atomic_replace_with_retry(tmp, abs_p)
    return abs_p


def append_to_note(rel_path: str | Path, content: str, ensure_newline: bool = True) -> Path:
    """追加内容到笔记。文件不存在时创建。"""
    abs_p = _resolve(rel_path)
    abs_p.parent.mkdir(parents=True, exist_ok=True)
    if abs_p.exists():
        existing = abs_p.read_text(encoding="utf-8")
        if ensure_newline and existing and not existing.endswith("\n"):
            existing += "\n"
        new = existing + content
    else:
        new = content
    return write_note(rel_path, new)


# ── frontmatter 解析与更新 ────────────────────────────────
def split_frontmatter(content: str) -> tuple[dict, str]:
    """把 markdown 内容拆成 (frontmatter dict, body)。

    若没有 frontmatter，frontmatter 返回 {}，body 是整段。
    """
    m = _FRONTMATTER_RE.match(content)
    if not m:
        return {}, content
    fm_text, body = m.group(1), m.group(2)
    try:
        fm = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"frontmatter YAML 解析失败：{e}") from e
    if not isinstance(fm, dict):
        raise ValueError(f"frontmatter 必须是 mapping，实际类型：{type(fm).__name__}")
    return fm, body


# 匹配开头 frontmatter 块；闭合 --- 后只吃**一个** \n（避免吞掉 body 起始的空行）
_FRONTMATTER_BLOCK_RE = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*\n", re.DOTALL)


def update_frontmatter(
    rel_path: str | Path,
    updates: dict | None = None,
    *,
    delete_keys: Iterable[str] = (),
) -> Path:
    """局部更新某笔记的 frontmatter，保留原始格式与注释。

    使用 ruamel.yaml 的 round-trip 模式：
    - flow style（如 `aliases: [a, b]`）保持 flow
    - block style（如多行 `- xxx`）保持 block
    - 注释、空行、缩进保留
    - 单字段更新只产生最小 diff

    - updates 中的键覆盖原值；新键追加在末尾
    - delete_keys 中的键从 frontmatter 移除
    - body 完全保留（包括 DYNAMIC_START/END 等控制标记）
    """
    content = read_note(rel_path)
    m = _FRONTMATTER_BLOCK_RE.match(content)
    if not m:
        raise ValueError(f"{rel_path} 没有 frontmatter（缺少首尾 ---）")
    fm_text = m.group(1)
    body = content[m.end():]

    yaml_rt = _rt_yaml()
    data = yaml_rt.load(fm_text)
    if data is None:
        # 空 frontmatter，构造一个新映射
        from ruamel.yaml.comments import CommentedMap
        data = CommentedMap()

    if updates:
        for k, v in updates.items():
            data[k] = v
    for k in delete_keys:
        if k in data:
            del data[k]

    buf = io.StringIO()
    yaml_rt.dump(data, buf)
    new_fm_text = buf.getvalue().rstrip("\n")
    new_content = f"---\n{new_fm_text}\n---\n{body}"
    return write_note(rel_path, new_content)
