"""
input_reader.py — 输入文件批量读取（单一职责：文件读取与截断）

职责：
- read_input_files：合并多个输入文件为带分隔符的上下文块
- _extract_sections：从 Markdown 文档中提取指定章节

token 感知截断：
- 若传入 model_key，使用 engine.token_counter.count_tokens 精确/近似计数
- 未传 model_key 或 token_counter 不可用时，自动降级为字符数截断（原行为）

不依赖 LLM / API 调用，可独立测试。
"""

from __future__ import annotations

import sys
from pathlib import Path

# token_counter 可选依赖（运行时延迟导入，避免循环依赖）
def _count_tok(text: str, model_key: str | None) -> int:
    """返回 text 的 token 估算数；model_key=None 时用字符数 // 2.5 估算。"""
    if not model_key:
        return max(1, int(len(text) / 2.5))
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from engine.token_counter import count_tokens
        return count_tokens(text, model_key)
    except Exception:
        return max(1, int(len(text) / 2.5))


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
    *,
    model_key: str | None = None,
    max_tokens_per_file: int | None = None,
    max_total_tokens: int | None = None,
) -> str:
    """合并多个输入文件为带分隔符的上下文块，供 user prompt 使用。
    § 15 上游堆积治理（层二：引擎截断兜底）：

    token 感知模式（推荐）：传入 model_key + max_tokens_per_file + max_total_tokens
      - max_tokens_per_file：单文件 token 超限时截断
      - max_total_tokens：所有文件合计 token 超限时丢弃后续

    字符数模式（兼容/降级）：不传 model_key，沿用原有 max_chars_per_file / max_total_chars

    两种模式可混用：token 限制优先，chars 作为兜底。

    file_paths 中每个元素可以是：
      - str / Path：直接读取整个文件
      - dict：{ "path": ..., "max_chars": ..., "sections": [...] }
    """
    use_tokens = model_key is not None and (
        max_tokens_per_file is not None or max_total_tokens is not None
    )

    parts = []
    total_measure = 0  # 字符数或 token 数，根据模式决定

    for fp_entry in file_paths:
        if isinstance(fp_entry, dict):
            fp = Path(fp_entry["path"])
            file_max_chars = int(fp_entry.get("max_chars", max_chars_per_file))
            sections = fp_entry.get("sections") or []
        else:
            fp = Path(fp_entry)
            file_max_chars = max_chars_per_file
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

        # ── 单文件截断 ──────────────────────────────────────────
        if use_tokens and max_tokens_per_file is not None:
            tok = _count_tok(content, model_key)
            if tok > max_tokens_per_file:
                # 按比例估算字符截断点
                ratio = max_tokens_per_file / tok
                cut = max(100, int(len(content) * ratio * 0.95))  # 留 5% 余量
                original_tok = tok
                content = content[:cut]
                content += (
                    f"\n\n⚠️ [截断警告] 原文 ~{original_tok} tokens，"
                    f"已截取至约 {max_tokens_per_file} tokens。"
                )
                print(
                    f"[read_input_files] ⚠️ {fp.name} 超过单文件 token 限制"
                    f"（~{original_tok} > {max_tokens_per_file} tokens），已截断。",
                    file=sys.stderr,
                )
        else:
            if len(content) > file_max_chars:
                original_len = len(content)
                content = content[:file_max_chars]
                content += (
                    f"\n\n⚠️ [截断警告] 原文 {original_len} 字符，"
                    f"已截取前 {file_max_chars} 字符。"
                    f"请检查角色产出体积是否超出约束（§15 层一：≤30KB）。"
                )
                print(
                    f"[read_input_files] ⚠️ {fp.name} 超过单文件限制"
                    f"（{original_len} > {file_max_chars} chars），已截断。",
                    file=sys.stderr,
                )

        block = f"=== {fp.name} ===\n{content}\n==="

        # ── 总量截断 ────────────────────────────────────────────
        if use_tokens and max_total_tokens is not None:
            block_measure = _count_tok(block, model_key)
            limit = max_total_tokens
        else:
            block_measure = len(block)
            limit = max_total_chars

        if total_measure + block_measure > limit:
            remaining = limit - total_measure
            unit = "tokens" if use_tokens else "chars"
            if remaining > (50 if use_tokens else 500):
                # 按比例裁剪 block
                ratio = remaining / block_measure
                cut = max(50, int(len(block) * ratio * 0.95))
                block = block[:cut] + (
                    f"\n\n⚠️ [总量截断] 已达 {limit} {unit} 上限，"
                    f"{fp.name} 剩余内容及后续文件已丢弃。"
                )
                parts.append(block)
            else:
                parts.append(
                    f"=== {fp.name} ===\n"
                    f"⚠️ [总量截断] 已达 {limit} {unit} 上限，本文件已跳过。\n==="
                )
            print(
                f"[read_input_files] ⚠️ 总输入量超过 {limit} {unit} 上限，"
                f"从 {fp.name} 起截断，后续文件丢弃。",
                file=sys.stderr,
            )
            break

        parts.append(block)
        total_measure += block_measure

    return "\n\n".join(parts)
