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

# 只匹配起始 marker，用于两件事：
#   1. 统计 LLM **声明**了多少个块（与实际解析出的数量比对，抓未闭合块）
#   2. 在已捕获的块内容里找残留 marker（抓被吞掉的下一个块）
_FILE_START_RE = re.compile(r"<!--\s*FILE:\s*(.+?)\s*-->")

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


def _split_residual_blocks(
    rel: str, body: str, warnings: list[str]
) -> list[tuple[str, str]]:
    """在已捕获的块内容里切出被吞掉的后续块。

    触发条件：LLM 漏写 `<!-- /FILE -->`。此时 non-greedy 正则会一路吃到**下一个**
    块的闭合标签，于是下一个块的起始 marker + 全部内容都被算进上一个文件。
    后果是上一个文件带着残留 marker（进 .py 直接 SyntaxError），下一个文件**静默消失**。

    实战：pain-radar 坑 8（2026-05-16）—— T06a 两个 FILE 块拼进一个文件，
    `test_radar.py` 744 行手工删到 597 行才干净。
    """
    starts = list(_FILE_START_RE.finditer(body))
    if not starts:
        return [(rel, body)]

    warnings.append(
        f"`{rel}` 的内容里发现 {len(starts)} 个残留 FILE marker"
        f"（上游漏写 `<!-- /FILE -->`），已按 marker 位置切分恢复："
        + "、".join(f"`{s.group(1).strip()}`" for s in starts)
    )
    out = [(rel, body[: starts[0].start()])]
    for i, s in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(body)
        out.append((s.group(1).strip(), body[s.end():end].lstrip("\n")))
    return out


def parse_claude_output_to_files(raw_output: str) -> dict:
    """解析 Claude 输出中的 <!-- FILE: path --> ... <!-- /FILE --> 块。

    返回 {相对路径: 内容}。注意路径中的 {project} 占位符不在此处替换，
    由调用方在写盘前用 engine.config.resolve_path 处理。
    自动剥离整体被 markdown 代码围栏包裹的内容（Claude 偶尔违反约定）。

    ## 鲁棒性（2026-08-16 补，pain-radar 坑 8 + mini-ledger §3 驱动）

    三种**静默丢文件**的失效模式，本函数一律改为「尽力恢复 + stderr 告警」，
    不再无声吞掉：

    1. **漏写闭合标签** → 下一个块被吞进上一个文件。切分恢复，见
       `_split_residual_blocks`。
    2. **输出被 max_tokens 截断** → 最后一个块没有闭合标签，正则**完全不匹配**，
       该文件凭空消失且无任何迹象。用「声明块数 vs 恢复块数」比对抓出来。
       （CLI 路径拿不到 API 的 `stop_reason`，只能从输出形态反推。）
    3. **同一路径出现多次** → 后者静默覆盖前者。pain-radar 坑 4 的两份
       `radar.py` 即此类。

    告警走 stderr，不改返回类型（所有调用方都只吃 dict）。**不判失败** ——
    这些都是可恢复的格式问题，判失败要多烧一次 LLM 调用且未必更好；
    但必须可见，否则就是上面三条能潜伏三个月的原因。
    """
    results: dict[str, str] = {}
    warnings: list[str] = []
    recovered = 0

    for m in _FILE_BLOCK_RE.finditer(raw_output):
        for rel, body in _split_residual_blocks(
            m.group(1).strip(), m.group(2), warnings
        ):
            recovered += 1
            content = _normalize_empty_file_placeholder(_strip_outer_code_fence(body))
            if rel in results:
                warnings.append(
                    f"`{rel}` 出现多次，后者覆盖前者"
                    f"（{len(results[rel])} → {len(content)} chars）—— "
                    f"若两块是同一文件的上下半段，本次会丢掉上半段"
                )
            results[rel] = content

    declared = len(_FILE_START_RE.findall(raw_output))
    if declared > recovered:
        warnings.append(
            f"声明了 {declared} 个 FILE 块但只恢复出 {recovered} 个 —— "
            f"最可能是输出被 max_tokens 截断（末块无闭合标签），"
            f"缺失的文件不会落盘"
        )

    for w in warnings:
        print(f"[parse_output] ⚠️ {w}", file=sys.stderr)
    return results
