"""
engine/token_counter.py — 跨 provider token 计数工具

设计原则：
- 配置驱动：每个 provider 在 llm_providers.yaml 声明 `tokenizer` 字段
- tiktoken 优先：支持 cl100k_base / o200k_base 等 encoding
- 降级策略：tiktoken 未安装 → chars // 2.5（经验值，对中英混合文本约 ±15%）
- Gemini 用 SentencePiece 分词器，tiktoken 不支持 → 强制 chars 降级

支持的 tokenizer 值（providers YAML 的 tokenizer 字段）：
  cl100k_base   — Claude / GPT-4 / DeepSeek / LLaMA（近似）
  o200k_base    — GPT-4o / GPT-4o-mini
  chars         — 无 tiktoken 分词器，用 chars // CHARS_PER_TOKEN 估算
                  适用：Gemini（SentencePiece）、其他无公开分词器的模型

Gemini 精度说明：
  chars 估算对 Gemini 误差约 ±20%（英文偏低，中文偏高）。
  如需精确计数，安装 google-generativeai 并调用 model.count_tokens()。
  未来升级路径：_get_tokenizer_for_model 检测到 'gemini_api' 时走 Google API。

用法：
    from engine.token_counter import count_tokens, estimate_tokens

    n = count_tokens("你好 world", "claude-sonnet-4-6")  # tiktoken 精确
    n = count_tokens("你好 world", "gemini-2.5-pro")     # chars 估算
    n = estimate_tokens("你好 world")                     # 通用快速估算
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

# chars / token 经验比：英文 ~4 chars/token，中文 ~1.5 chars/token
# 混合文本取折中 ~2.5；保守偏大估算用 2.5 以避免漏检超限
_CHARS_PER_TOKEN: float = 2.5

# tiktoken encoding 实例缓存（进程内复用）
_TIKTOKEN_CACHE: dict[str, "tiktoken.Encoding"] = {}  # type: ignore[name-defined]

# tiktoken 是否可用（首次导入尝试后缓存结果）
_TIKTOKEN_AVAILABLE: bool | None = None


def _try_import_tiktoken() -> bool:
    """尝试导入 tiktoken，缓存结果。未安装时打印一次警告。"""
    global _TIKTOKEN_AVAILABLE
    if _TIKTOKEN_AVAILABLE is not None:
        return _TIKTOKEN_AVAILABLE
    try:
        import tiktoken  # noqa: F401
        _TIKTOKEN_AVAILABLE = True
    except ImportError:
        print(
            "[token_counter] ⚠️ tiktoken 未安装，token 计数将使用字符数估算。"
            "（pip install tiktoken 可获得更精确的计数）",
            file=sys.stderr,
        )
        _TIKTOKEN_AVAILABLE = False
    return _TIKTOKEN_AVAILABLE


def _get_encoding(encoding_name: str):
    """获取（并缓存）tiktoken encoding 实例。"""
    if encoding_name not in _TIKTOKEN_CACHE:
        import tiktoken
        _TIKTOKEN_CACHE[encoding_name] = tiktoken.get_encoding(encoding_name)
    return _TIKTOKEN_CACHE[encoding_name]


def estimate_tokens(text: str) -> int:
    """通用快速估算：不依赖 model，用字符数 / _CHARS_PER_TOKEN。

    适用于不知道 model 的场景，或 tiktoken 未安装的降级路径。
    对中英混合文本误差约 ±15%，足够 pre_flight 粗略判断。
    """
    return max(1, int(len(text) / _CHARS_PER_TOKEN))


def count_tokens(text: str, model_key: str) -> int:
    """精确（或尽量精确）地估算 text 对指定 model 的 token 数。

    流程：
    1. 从 llm_providers.yaml 读取 provider 的 `tokenizer` 字段
    2. tokenizer == 'chars'  → estimate_tokens()
    3. tokenizer 是 tiktoken encoding 名 → 用 tiktoken 编码
    4. tiktoken 未安装 → estimate_tokens() 降级

    Args:
        text:      要计数的文本
        model_key: providers YAML 中的 key（如 'claude-sonnet-4-6'）

    Returns:
        估算的 token 数（int）
    """
    # 获取 provider 配置中的 tokenizer 字段
    tokenizer = _get_tokenizer_for_model(model_key)

    if tokenizer == "chars":
        return estimate_tokens(text)

    if not _try_import_tiktoken():
        return estimate_tokens(text)

    try:
        enc = _get_encoding(tokenizer)
        return len(enc.encode(text))
    except Exception as e:
        print(
            f"[token_counter] ⚠️ tiktoken 编码失败（{tokenizer}）：{e}，降级估算。",
            file=sys.stderr,
        )
        return estimate_tokens(text)


def count_tokens_multi(texts: list[str], model_key: str) -> int:
    """批量计数多段文本的总 token 数（避免重复查配置）。"""
    tokenizer = _get_tokenizer_for_model(model_key)

    if tokenizer == "chars":
        return sum(estimate_tokens(t) for t in texts)

    if not _try_import_tiktoken():
        return sum(estimate_tokens(t) for t in texts)

    try:
        enc = _get_encoding(tokenizer)
        return sum(len(enc.encode(t)) for t in texts)
    except Exception as e:
        print(
            f"[token_counter] ⚠️ tiktoken 批量编码失败（{tokenizer}）：{e}，降级估算。",
            file=sys.stderr,
        )
        return sum(estimate_tokens(t) for t in texts)


# ── 内部：从 providers YAML 读取 tokenizer 字段 ───────────────────────────────
_MODEL_TOKENIZER_CACHE: dict[str, str] = {}


def _get_tokenizer_for_model(model_key: str) -> str:
    """从 llm_providers.yaml 读取 model 对应的 tokenizer；进程内缓存。

    找不到 model 或未声明 tokenizer 时，返回默认值 'cl100k_base'。
    """
    if model_key in _MODEL_TOKENIZER_CACHE:
        return _MODEL_TOKENIZER_CACHE[model_key]

    tokenizer = "cl100k_base"  # 安全默认值
    try:
        # 延迟导入避免循环依赖
        from engine.llm import get_provider  # type: ignore
        cfg = get_provider(model_key)
        tokenizer = cfg.get("tokenizer", "cl100k_base")
    except Exception:
        pass  # 找不到 provider，用默认值

    _MODEL_TOKENIZER_CACHE[model_key] = tokenizer
    return tokenizer


def clear_cache() -> None:
    """清空所有缓存（测试或配置变更后使用）。"""
    global _TIKTOKEN_AVAILABLE
    _TIKTOKEN_CACHE.clear()
    _MODEL_TOKENIZER_CACHE.clear()
    _TIKTOKEN_AVAILABLE = None
