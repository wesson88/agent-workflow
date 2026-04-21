"""
Token 工具：估算、裁剪历史、截断文本
"""


def estimate_tokens(text: str) -> int:
    """粗估 Token 数：中文约1字=1token，英文约4字符=1token"""
    chinese = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    others = len(text) - chinese
    return chinese + others // 4


def trim_history_by_tokens(
    history: list[dict],
    max_tokens: int = 1500,
    keep_last: int = 2,
) -> list[dict]:
    """
    按 Token 预算裁剪历史，从最旧的开始丢弃。
    始终保留最后 keep_last 条，防止上下文断裂。
    """
    if not history:
        return []
    must_keep = history[-keep_last:] if len(history) >= keep_last else history[:]
    candidates = history[:-keep_last] if len(history) > keep_last else []

    used = sum(estimate_tokens(m["content"]) for m in must_keep)
    result = []
    for msg in reversed(candidates):
        cost = estimate_tokens(msg["content"])
        if used + cost > max_tokens:
            break
        result.insert(0, msg)
        used += cost
    return result + must_keep


def truncate_content(text: str, max_chars: int = 300) -> str:
    """截断过长内容，保留开头，末尾加省略标记"""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "…[已截断]"
