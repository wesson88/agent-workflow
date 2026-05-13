"""
output_parser.py — Claude 输出解析与原子写入（单一职责：输出处理）

职责：
- parse_claude_output_to_files：解析 <!-- FILE: --> 标签
- write_output_atomic：原子写入文件（带 Windows 重试）
- _strip_outer_code_fence / _normalize_empty_file_placeholder：内部辅助

不依赖 LLM / API 调用，可独立测试。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from engine.obsidian_io import _atomic_replace_with_retry  # noqa: E402


# ── 原子写入 ──────────────────────────────────────────────
def write_output_atomic(dest_path: Path, content: str) -> None:
    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w",
        dir=dest_path.parent,
        delete=False,
        encoding="utf-8",
        suffix=".tmp",
        newline="\n",
    ) as tf:
        tf.write(content)
        tmp = tf.name
    _atomic_replace_with_retry(tmp, dest_path)


# ── 输出解析 ──────────────────────────────────────────────
_FILE_BLOCK_RE = re.compile(
    r"<!--\s*FILE:\s*(.+?)\s*-->\n(.*?)<!--\s*/FILE\s*-->",
    re.DOTALL,
)

_LEADING_FENCE_RE = re.compile(r"\A\s*```[^\n`]*\n")
_TRAILING_FENCE_RE = re.compile(r"\n```\s*\Z")
_PURE_COMMENT_RE = re.compile(r"\A\s*(?:<!--.*?-->\s*)+\Z", re.DOTALL)


def _strip_outer_code_fence(content: str) -> str:
    """若 content 整体被一对 markdown 代码围栏包裹，剥离外层。"""
    head = _LEADING_FENCE_RE.search(content)
    tail = _TRAILING_FENCE_RE.search(content)
    if not head or not tail:
        return content
    inner = content[head.end():tail.start()]
    return inner if inner.endswith("\n") else inner + "\n"


def _normalize_empty_file_placeholder(content: str) -> str:
    """若 content 仅包含 HTML/markdown 注释（无实际代码），写空文件。"""
    if _PURE_COMMENT_RE.match(content):
        return ""
    return content


def parse_claude_output_to_files(raw_output: str) -> dict:
    """解析 Claude 输出中的 <!-- FILE: path --> ... <!-- /FILE --> 块。

    返回 {相对路径: 内容}。
    自动剥离整体被 markdown 代码围栏包裹的内容。
    """
    results = {}
    for m in _FILE_BLOCK_RE.finditer(raw_output):
        rel = m.group(1).strip()
        content = _strip_outer_code_fence(m.group(2))
        content = _normalize_empty_file_placeholder(content)
        results[rel] = content
    return results
