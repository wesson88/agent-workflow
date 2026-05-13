"""
input_reader.py — 输入文件批量读取（单一职责：文件读取与截断）

职责：
- read_input_files：合并多个输入文件为带分隔符的上下文块
- _extract_sections：从 Markdown 文档中提取指定章节

不依赖 LLM / API 调用，可独立测试。
"""

from __future__ import annotations

import sys
from pathlib import Path


def _extract_sections(content: str, sections: list[str]) -> str:
    """从 Markdown 文档中只提取指定章节（## 标题匹配）。
    匹配规则：标题文字包含 section 关键词即命中（大小写不敏感）。
    若无任何章节命中，返回原文并附加警告。
    """
    if not sections:
        return content
    lines = content.splitlines(keepends=True)
    result: list[str] = []
    in_section = False
    current_level = 0
    for line in lines:
        heading = None
        for lvl in range(1, 7):
            prefix = "#" * lvl + " "
            if line.startswith(prefix):
                heading = (lvl, line[lvl + 1:].strip())
                break
        if heading:
            lvl, title = heading
            is_target = any(s.lower() in title.lower() for s in sections)
            if is_target:
                in_section = True
                current_level = lvl
                result.append(line)
            elif in_section and lvl <= current_level:
                in_section = False
            elif in_section:
                result.append(line)
        elif in_section:
            result.append(line)
    if not result:
        section_list = ", ".join(sections)
        return (
            content
            + f"\n\n⚠️ [sections 警告] 未找到章节 [{section_list}]，已返回全文。"
        )
    return "".join(result)


def read_input_files(
    file_paths: list,
    max_chars_per_file: int = 25000,
    max_total_chars: int = 80000,
) -> str:
    """合并多个输入文件为带分隔符的上下文块，供 user prompt 使用。
    § 15 上游堆积治理（层二：引擎截断兜底）：

    - max_chars_per_file：单文件超限时截断并追加警告
    - max_total_chars：所有文件合计超限时，按声明顺序优先保留

    层二扩展（section 选择器）：
    file_paths 中每个元素可以是：
      - str / Path：直接读取整个文件
      - dict：{ "path": ..., "max_chars": ..., "sections": [...] }
    """
    parts = []
    total_chars = 0
    for fp_entry in file_paths:
        if isinstance(fp_entry, dict):
            fp = Path(fp_entry["path"])
            file_max = int(fp_entry.get("max_chars", max_chars_per_file))
            sections = fp_entry.get("sections") or []
        else:
            fp = Path(fp_entry)
            file_max = max_chars_per_file
            sections = []

        if fp.exists() and fp.is_file():
            try:
                content = fp.read_text(encoding="utf-8")
            except Exception as e:
                content = f"（读取失败：{e}）"
        else:
            content = "（文件不存在或为空）"

        if sections:
            content = _extract_sections(content, sections)

        if len(content) > file_max:
            original_len = len(content)
            content = content[:file_max]
            content += (
                f"\n\n⚠️ [截断警告] 原文 {original_len} 字符，"
                f"已截取前 {file_max} 字符。"
                f"请检查角色产出体积是否超出约束（§15 层一：≤30KB）。"
            )
            print(
                f"[read_input_files] ⚠️ {fp.name} 超过单文件限制"
                f"（{original_len} > {file_max} chars），已截断。",
                file=sys.stderr,
            )

        block = f"=== {fp.name} ===\n{content}\n==="
        block_len = len(block)

        if total_chars + block_len > max_total_chars:
            remaining = max_total_chars - total_chars
            if remaining > 500:
                block = block[:remaining] + (
                    f"\n\n⚠️ [总量截断] 已达 {max_total_chars} 字符上限，"
                    f"{fp.name} 剩余内容及后续文件已丢弃。"
                )
                parts.append(block)
            else:
                parts.append(
                    f"=== {fp.name} ===\n"
                    f"⚠️ [总量截断] 已达 {max_total_chars} 字符上限，本文件已跳过。\n==="
                )
            print(
                f"[read_input_files] ⚠️ 总输入量超过 {max_total_chars} chars 上限，"
                f"从 {fp.name} 起截断，后续文件丢弃。",
                file=sys.stderr,
            )
            break

        parts.append(block)
        total_chars += block_len

    return "\n\n".join(parts)
