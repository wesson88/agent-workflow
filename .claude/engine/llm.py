"""
engine/llm.py — provider-agnostic LLM 调用入口

支持任意 LLM provider，每个 provider 配置在 engine/llm_providers.yaml 中。
角色 frontmatter 的 `model:` 字段 = providers YAML 的 key。

设计要点：
- 双轨路由：每个 provider 可配 api / cli / dual_track，prefer=auto 时 api 优先回退 cli
- 适配器分离：anthropic SDK / openai_compat SDK / 通用 CLI 子进程
- 配置驱动：新增 provider 只改 YAML，本文件无需改动
- 向后兼容：保留 call_claude / is_api_available / is_cli_available 旧入口
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


_PROVIDERS_FILE = Path(__file__).parent / "llm_providers.yaml"
_providers_cache: dict[str, dict] | None = None

# audit.jsonl 写入路径（与 skills/audit.py 对齐；engine 自己实现避免反向依赖）
# 测试可 monkeypatch 此变量重定向到 tmp 路径。
_AUDIT_JSONL_PATH = Path(__file__).resolve().parent.parent.parent / ".claude" / "audit.jsonl"


def _append_token_audit(level: str, reason: str, ctx: dict) -> None:
    """把 token 审计事件写到 .claude/audit.jsonl。

    设计：engine 内部不引 skills.audit（反向依赖），自带最小实现。
    级别 level=warn|raise；reason 区分触发原因（system_oversized / total_ratio / budget_*）。
    ctx 至少含 model/sys_tokens/user_tokens/total_tokens；其他可选字段直接合并。
    任何 I/O 失败静默吞掉 — 监控本身不能阻断主路径。
    """
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "type": "token_audit",
        "level": level,
        "reason": reason,
        **ctx,
    }
    try:
        _AUDIT_JSONL_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_AUDIT_JSONL_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        # 不阻断主路径；仅在 stderr 留一行
        print(f"[audit] ⚠️ audit.jsonl 写入失败（{type(e).__name__}: {e}）", file=sys.stderr)

# Windows 命令行总长度上限 32767；预留出余量给其它参数。
# 超过此阈值的 system_prompt 自动改走 stdin inline。
_CMD_ARG_LIMIT = 8192

# ── token 预算护栏阈值 ───────────────────────────────────
# system prompt 单独阈值（与 context_window 无关，是 prompt 设计原则）：
#   - 健康 system 抽 section 后 3-5K；DYNAMIC 累积 > 8K 说明应触发 GRADUATE/DROP 收敛
#   - > 20K 视为设计失控（无论 model 容量多大都不应放任）
# 2026-05-18 v0.2：原阈值（15K/30K）过宽容，DYNAMIC 累积到 15K 时已经晚了。
_SYSTEM_WARN_TOKENS = 8_000
_SYSTEM_RAISE_TOKENS = 20_000

# 总量阈值（按 context_window 百分比，可被 input_budget 覆盖）：
#   ≥ 50% 早期信号（一次调用占了一半窗口，必有累积或注入过载）
#   ≥ 85% 主动 raise（留 30K 给 output + retry 余量，比 SDK 拒错更早）
# 2026-05-18 v0.2：原 80/95 太晚，95% 触发时模型实际拒绝距离只剩 10K。
_TOTAL_WARN_RATIO = 0.50
_TOTAL_RAISE_RATIO = 0.85

# 按角色 override 时的 WARN 比例：触发 RAISE = override 值，WARN = override × 0.6
_BUDGET_WARN_RATIO_OF_OVERRIDE = 0.60


# ── 配置加载 ─────────────────────────────────────────────
def _load_providers() -> dict[str, dict]:
    """加载 llm_providers.yaml；进程内缓存。调用 reload_providers() 强制刷新。"""
    global _providers_cache
    if _providers_cache is None:
        if not _PROVIDERS_FILE.exists():
            raise RuntimeError(f"providers config 不存在：{_PROVIDERS_FILE}")
        with open(_PROVIDERS_FILE, encoding="utf-8") as f:
            _providers_cache = yaml.safe_load(f) or {}
        if not isinstance(_providers_cache, dict):
            raise ValueError(f"{_PROVIDERS_FILE} 顶层必须是 mapping")
    return _providers_cache


def reload_providers() -> None:
    """清空 provider 配置缓存（用于 YAML 改动后强制重读）。"""
    global _providers_cache
    _providers_cache = None


def get_provider(name: str) -> dict:
    providers = _load_providers()
    if name not in providers:
        raise KeyError(
            f"未知 provider/model：'{name}'。"
            f"已配置：{sorted(providers.keys())}"
        )
    return providers[name]


def list_providers() -> list[str]:
    return sorted(_load_providers().keys())


# ── 路由：决定走哪条轨道 ─────────────────────────────────
def _resolve_track(cfg: dict) -> str:
    """返回 'api' / 'cli' / 'unavailable'。"""
    mode = cfg.get("mode", "dual_track")
    prefer = cfg.get("prefer", "auto")

    api_cfg = cfg.get("api") or {}
    cli_cfg = cfg.get("cli") or {}
    api_available = bool(api_cfg) and bool(os.environ.get(api_cfg.get("key_env", ""), ""))
    cli_available = bool(cli_cfg) and bool(shutil.which(cli_cfg.get("path", "")))

    if mode == "api_only":
        return "api" if api_available else "unavailable"
    if mode == "cli_only":
        return "cli" if cli_available else "unavailable"

    # dual_track
    if prefer == "api":
        return "api" if api_available else ("cli" if cli_available else "unavailable")
    if prefer == "cli":
        return "cli" if cli_available else ("api" if api_available else "unavailable")
    # auto：API 优先（更快、更稳）；回退 CLI（用 MAX 订阅）
    if api_available:
        return "api"
    if cli_available:
        return "cli"
    return "unavailable"


def is_provider_available(name: str) -> bool:
    try:
        cfg = get_provider(name)
    except KeyError:
        return False
    return _resolve_track(cfg) != "unavailable"


# ── 公共入口 ────────────────────────────────────────────
def call_llm(
    system_prompt: str | tuple[str, str] | tuple[str, str, str],
    user_prompt: str,
    *,
    model: str,
    max_tokens: int = 4096,
    print_stream: bool = True,
    input_budget: int | None = None,
    role_name: str | None = None,
) -> str:
    """统一 LLM 调用入口。

    参数：
        system_prompt: 系统提示词。3 种形态（自动分派）：
            - str：单字符串（老 API）
            - (static, dynamic) 2-tuple：static 打 cache_control，dynamic 不 cache
            - (static, dynamic_own, dynamic_upstream) 3-tuple（**B1 P10.5 新增**）：
              static + dynamic_own 各自打独立 cache breakpoint（own 变化频率
              低，跨 call 命中率高）；dynamic_upstream 不 cache（上游 [NEW]
              补丁频繁变化）
            CLI 路径全部拼为单字符串。
        user_prompt: 用户输入
        model: 必传；同时是 llm_providers.yaml 中的 key
        max_tokens: API 路径有效；CLI 路径不直接控制（受模型/订阅限制）
        print_stream: 是否流式打印到 stdout（默认 True）
        input_budget: 可选的按角色 input token 预算（来自角色 frontmatter
            `budget_input_tokens` 字段）。传入时**覆盖**默认 ratio 计算：
              RAISE 阈值 = input_budget，WARN 阈值 = input_budget × 0.6。
            None（默认）走 _TOTAL_WARN_RATIO / _TOTAL_RAISE_RATIO 的窗口百分比逻辑。
    """
    cfg = get_provider(model)
    track = _resolve_track(cfg)

    # 规范化 system_prompt 为 (static, dynamic_own, dynamic_upstream) 三段
    if isinstance(system_prompt, tuple):
        if len(system_prompt) == 3:
            static, dynamic_own, dynamic_upstream = system_prompt
        elif len(system_prompt) == 2:
            static, dynamic_flat = system_prompt
            dynamic_own, dynamic_upstream = "", dynamic_flat
        else:
            raise ValueError(
                f"system_prompt tuple 长度必须为 2 或 3，实际：{len(system_prompt)}"
            )
    else:
        static, dynamic_own, dynamic_upstream = system_prompt, "", ""

    dynamic_combined = "\n".join(filter(None, [dynamic_own, dynamic_upstream]))

    # 入口审计：在真正调用 LLM 前过两道护栏（system 单独阈值 + 总量百分比/角色预算）
    _audit_token_budget(model, static, dynamic_combined, user_prompt, input_budget=input_budget)

    if track == "api":
        api_cfg = cfg["api"]
        kind = api_cfg.get("kind", "anthropic")
        if kind == "anthropic":
            return _call_anthropic_sdk(
                api_cfg, static, dynamic_own, dynamic_upstream,
                user_prompt, max_tokens, print_stream,
                role_name=role_name, model_name=model,
            )
        # openai_compat：拼接为单字符串
        flat = "\n\n".join(filter(None, [static, dynamic_combined])) if dynamic_combined else static
        if kind == "openai_compat":
            return _call_openai_compat(
                api_cfg, flat, user_prompt, max_tokens, print_stream,
                role_name=role_name, model_name=model,
            )
        raise ValueError(f"未知 api kind：{kind}（provider={model}）")

    if track == "cli":
        flat = "\n\n".join(filter(None, [static, dynamic_combined])) if dynamic_combined else static
        return _call_cli(
            cfg["cli"], flat, user_prompt, print_stream,
            role_name=role_name, model_name=model,
        )

    # unavailable：给出可操作的提示
    api_cfg = cfg.get("api") or {}
    cli_cfg = cfg.get("cli") or {}
    parts = [f"provider '{model}' 不可用（mode={cfg.get('mode')})："]
    if api_cfg:
        parts.append(f"  API 轨道：在 .env 中设置 {api_cfg.get('key_env', '<key_env>')}=...")
    if cli_cfg:
        parts.append(f"  CLI 轨道：安装并确保 `{cli_cfg.get('path')}` 在 PATH 中")
    raise RuntimeError("\n".join(parts))


# ── token 预算审计 ───────────────────────────────────────
def _audit_token_budget(
    model: str, static: str, dynamic: str, user_prompt: str,
    *, input_budget: int | None = None,
) -> None:
    """入口护栏：在真正调用 LLM 前估算 input token 总量，按阈值告警/阻断。

    两道护栏：
      1. system prompt（static + dynamic）单独阈值
         - > _SYSTEM_WARN_TOKENS  → WARNING（DYNAMIC 累积线，提醒 GRADUATE/DROP）
         - > _SYSTEM_RAISE_TOKENS → raise（prompt 设计失控）
      2. 总量（system + user）：
         - 传入 input_budget（角色 frontmatter 显式预算）：
           >= input_budget × 0.6 → WARN；>= input_budget → RAISE
         - 否则按 context_window 百分比（_TOTAL_WARN_RATIO / _TOTAL_RAISE_RATIO）

    token_counter 失败时静默降级（不阻断主路径）：
      - tiktoken 未装、provider 未知、字符编码异常等场景仍能调用。
    """
    try:
        from engine.token_counter import count_tokens, get_context_window
        static_tok = count_tokens(static, model) if static else 0
        dynamic_tok = count_tokens(dynamic, model) if dynamic else 0
        user_tok = count_tokens(user_prompt, model) if user_prompt else 0
        cw = get_context_window(model)
    except Exception as e:
        print(
            f"[audit] ⚠️ token 审计降级（{type(e).__name__}: {e}），跳过校验。",
            file=sys.stderr,
        )
        return

    sys_tok = static_tok + dynamic_tok
    total_tok = sys_tok + user_tok

    base_ctx = {
        "model": model,
        "sys_tokens": sys_tok,
        "static_tokens": static_tok,
        "dynamic_tokens": dynamic_tok,
        "user_tokens": user_tok,
        "total_tokens": total_tok,
        "context_window": cw,
        "budget_input_tokens": input_budget,
    }

    # 护栏 1：system prompt 单独阈值
    if sys_tok > _SYSTEM_RAISE_TOKENS:
        _append_token_audit("raise", "system_prompt_oversized", {
            **base_ctx, "threshold": _SYSTEM_RAISE_TOKENS,
        })
        raise RuntimeError(
            f"[audit] system prompt 过大（{sys_tok} tokens > {_SYSTEM_RAISE_TOKENS} "
            f"阈值）— static={static_tok}, dynamic={dynamic_tok}。"
            f" 排查建议：(1) 角色笔记是否含未被 build_system_prompt 抽取的冗余章节；"
            f" (2) DYNAMIC 区是否累积过多、需要 graduator/reflector 收敛；"
            f" (3) 上游 role.upstream 链是否过长。"
        )
    if sys_tok > _SYSTEM_WARN_TOKENS:
        _append_token_audit("warn", "system_prompt_oversized", {
            **base_ctx, "threshold": _SYSTEM_WARN_TOKENS,
        })
        print(
            f"[audit] ⚠️ system prompt 偏大（{sys_tok} tokens > "
            f"{_SYSTEM_WARN_TOKENS} 告警线）— static={static_tok}, "
            f"dynamic={dynamic_tok}。建议关注 DYNAMIC 累积。",
            file=sys.stderr,
        )

    # 护栏 2：总量阈值
    # 优先用角色 frontmatter 的 input_budget（显式声明）；否则按 context_window 比例
    if input_budget is not None and input_budget > 0:
        raise_at = input_budget
        warn_at = int(input_budget * _BUDGET_WARN_RATIO_OF_OVERRIDE)
        if total_tok >= raise_at:
            _append_token_audit("raise", "budget_input_exceeded", {
                **base_ctx, "raise_at": raise_at, "warn_at": warn_at,
            })
            raise RuntimeError(
                f"[audit] input token 超角色预算（{total_tok} ≥ {raise_at}）— "
                f"system={sys_tok}, user={user_tok}, model={model}。"
                f"建议：拆分 user prompt 或精简 system；"
                f"或角色 frontmatter 调高 budget_input_tokens。"
            )
        if total_tok >= warn_at:
            _append_token_audit("warn", "budget_input_near", {
                **base_ctx, "raise_at": raise_at, "warn_at": warn_at,
            })
            print(
                f"[audit] ⚠️ input token 接近角色预算（{total_tok} ≥ {warn_at}, "
                f"上限 {raise_at}）— system={sys_tok}, user={user_tok}, model={model}。",
                file=sys.stderr,
            )
        return

    # 未声明 input_budget：走 context_window 百分比
    ratio = total_tok / cw if cw else 0.0
    if ratio >= _TOTAL_RAISE_RATIO:
        _append_token_audit("raise", "total_ratio_exceeded", {
            **base_ctx, "ratio": ratio, "threshold_ratio": _TOTAL_RAISE_RATIO,
        })
        raise RuntimeError(
            f"[audit] input token 总量触顶（{total_tok}/{cw} = {ratio:.1%} ≥ "
            f"{_TOTAL_RAISE_RATIO:.0%}）— system={sys_tok}, user={user_tok}, "
            f"model={model}。建议：拆分 user prompt / 精简 system / 角色加 "
            f"budget_input_tokens 显式声明；继续调用预计将被 SDK 拒绝。"
        )
    if ratio >= _TOTAL_WARN_RATIO:
        _append_token_audit("warn", "total_ratio_warn", {
            **base_ctx, "ratio": ratio, "threshold_ratio": _TOTAL_WARN_RATIO,
        })
        print(
            f"[audit] ⚠️ input token 偏高（{total_tok}/{cw} = {ratio:.1%} ≥ "
            f"{_TOTAL_WARN_RATIO:.0%}）— system={sys_tok}, user={user_tok}, "
            f"model={model}。",
            file=sys.stderr,
        )


# ── Anthropic SDK ────────────────────────────────────────
def _call_anthropic_sdk(
    api_cfg: dict, system_static: str,
    system_dynamic_own: str, system_dynamic_upstream: str,
    user_prompt: str,
    max_tokens: int, print_stream: bool,
    *,
    role_name: str | None = None,
    model_name: str | None = None,
) -> str:
    """调用 Anthropic SDK，B1（P10.5）3-block 分层缓存。

    - system_static：角色 §1-§6 + contract + capability + OUTPUT_FORMAT_SPEC
      几乎不变 → 打 breakpoint 1（cache_control=ephemeral）
    - system_dynamic_own：当前角色 DYNAMIC 补丁（B4 label 过滤后只含 [KEEP]/[GRADUATE?]）
      变化频率低 → 打 breakpoint 2（跨 call 复用概率高）
    - system_dynamic_upstream：上游角色 DYNAMIC 补丁
      变化频率高 → 不 cache
    - 缓存有效期 5 分钟（同一 API key 内跨请求共享），命中后费用降至 1/10
    - 向后兼容：3-tuple 调用者传 dynamic_own="", 走 static + dynamic_upstream；
      2-tuple 调用者被 call_llm 归一为 dynamic_upstream 路径（即老行为）
    """
    import anthropic  # 延迟 import
    key = os.environ.get(api_cfg["key_env"], "")
    client = anthropic.Anthropic(api_key=key, timeout=300.0) if key else anthropic.Anthropic(timeout=300.0)

    # B1：3-block 系统 prompt。Anthropic API 最多 4 个 cache breakpoint，
    # 这里用 2 个（static + dynamic_own），dynamic_upstream 不打 cache。
    system_block: list[dict] = [
        {"type": "text", "text": system_static, "cache_control": {"type": "ephemeral"}},
    ]
    if system_dynamic_own.strip():
        system_block.append({
            "type": "text",
            "text": system_dynamic_own,
            "cache_control": {"type": "ephemeral"},
        })
    if system_dynamic_upstream.strip():
        system_block.append({"type": "text", "text": system_dynamic_upstream})

    _RETRYABLE = (
        anthropic.RateLimitError,
        anthropic.APITimeoutError,
        anthropic.APIConnectionError,
    )
    base_delay = 5.0
    for attempt in range(4):
        chunks: list[str] = []
        try:
            t0 = time.monotonic()
            with client.messages.stream(
                model=api_cfg["model"],
                max_tokens=max_tokens,
                system=system_block,
                messages=[{"role": "user", "content": user_prompt}],
            ) as stream:
                for text in stream.text_stream:
                    if print_stream:
                        print(text, end="", flush=True)
                    chunks.append(text)
                usage = stream.get_final_message().usage
            elapsed = time.monotonic() - t0
            if print_stream:
                print()
            cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
            cache_create = getattr(usage, "cache_creation_input_tokens", 0) or 0
            print(
                f"[tokens] input={usage.input_tokens}"
                f"(cache_read={cache_read}"
                f" cache_create={cache_create})"
                f" output={usage.output_tokens}"
                f" total={usage.input_tokens + usage.output_tokens}"
                f" elapsed={elapsed:.1f}s"
            )
            # P10.5+ / M4 实战驱动：每次成功 LLM call 落 type=llm_call 事件到 audit.jsonl，
            # 用于 workflow token 汇总（run_chain 跑完扫 audit.jsonl 按 role 分组）
            _append_token_audit("info", "llm_call", {
                "role": role_name or "(unknown)",
                "model": model_name or api_cfg.get("model") or "(unknown)",
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_create,
                "elapsed_s": round(elapsed, 3),
                "attempt": attempt,
            })
            return "".join(chunks)
        except _RETRYABLE as e:
            if attempt == 3:
                raise
            if isinstance(e, anthropic.RateLimitError):
                retry_after = getattr(getattr(e, "response", None), "headers", {}).get("retry-after")
                wait = float(retry_after) if retry_after else base_delay * (2 ** attempt)
            else:
                wait = base_delay * (2 ** attempt)
            print(f"[llm_retry] {type(e).__name__}，等待 {wait:.0f}s 后重试（{attempt + 1}/3）", flush=True)
            time.sleep(wait)
    raise RuntimeError("unreachable")


# ── OpenAI 兼容 SDK（GPT / DeepSeek / Gemini / Ollama / 国产模型）─
def _call_openai_compat(
    api_cfg: dict, system_prompt: str, user_prompt: str,
    max_tokens: int, print_stream: bool,
    *,
    role_name: str | None = None,
    model_name: str | None = None,
) -> str:
    """2026-07-18 评审修复：此前该路径是"二等公民"——无重试、无超时、
    不落 llm_call 审计事件（gemini/deepseek 角色的 token 消耗在 audit.jsonl
    里是盲区，workflow token 汇总缺这些节点）。现与 Anthropic 路径对齐：
    - timeout=300s、4 次指数退避重试（RateLimit / Timeout / Connection）
    - stream_options.include_usage 拿真实 usage；provider 不支持该参数时
      降级重试一次（usage 记 -1 表示未知），不阻断主路径
    """
    import openai  # 延迟 import
    from openai import OpenAI

    key = os.environ.get(api_cfg["key_env"], "") or "sk-no-key"  # ollama 等不验 key
    client = OpenAI(api_key=key, base_url=api_cfg.get("base_url"), timeout=300.0)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    _RETRYABLE = (
        openai.RateLimitError,
        openai.APITimeoutError,
        openai.APIConnectionError,
    )
    base_delay = 5.0
    include_usage = True
    for attempt in range(4):
        chunks: list[str] = []
        usage = None
        try:
            t0 = time.monotonic()
            kwargs: dict = dict(
                model=api_cfg["model"],
                messages=messages,
                max_tokens=max_tokens,
                stream=True,
            )
            if include_usage:
                kwargs["stream_options"] = {"include_usage": True}
            try:
                stream = client.chat.completions.create(**kwargs)
            except openai.BadRequestError as e:
                # 个别 OpenAI 兼容端点不认 stream_options → 去掉后降级重试一次
                if include_usage and "stream_options" in str(e):
                    include_usage = False
                    kwargs.pop("stream_options", None)
                    stream = client.chat.completions.create(**kwargs)
                else:
                    raise
            for chunk in stream:
                # include_usage 时最后一个 chunk 无 choices、带 usage
                if getattr(chunk, "usage", None):
                    usage = chunk.usage
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta.content
                if delta:
                    if print_stream:
                        print(delta, end="", flush=True)
                    chunks.append(delta)
            elapsed = time.monotonic() - t0
            if print_stream:
                print()
            in_tok = getattr(usage, "prompt_tokens", None) if usage else None
            out_tok = getattr(usage, "completion_tokens", None) if usage else None
            print(
                f"[tokens] input={in_tok if in_tok is not None else '?'}"
                f" output={out_tok if out_tok is not None else '?'}"
                f" elapsed={elapsed:.1f}s"
            )
            _append_token_audit("info", "llm_call", {
                "role": role_name or "(unknown)",
                "model": model_name or api_cfg.get("model") or "(unknown)",
                # usage 拿不到时记 -1（区别于真实 0），汇总侧可识别"未知"
                "input_tokens": in_tok if in_tok is not None else -1,
                "output_tokens": out_tok if out_tok is not None else -1,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "elapsed_s": round(elapsed, 3),
                "attempt": attempt,
                "track": "openai_compat",
            })
            return "".join(chunks)
        except _RETRYABLE as e:
            if attempt == 3:
                raise
            wait = base_delay * (2 ** attempt)
            retry_after = getattr(getattr(e, "response", None), "headers", None)
            if retry_after is not None:
                ra = retry_after.get("retry-after")
                if ra:
                    try:
                        wait = float(ra)
                    except ValueError:
                        pass
            print(f"[llm_retry] {type(e).__name__}，等待 {wait:.0f}s 后重试（{attempt + 1}/3）", flush=True)
            time.sleep(wait)
    raise RuntimeError("unreachable")


# ── 通用 CLI 子进程 ──────────────────────────────────────
def _filter_extra_args(extra_args: list[str]) -> list[str]:
    """过滤掉空字符串值的参数对（如 `--tools ""` → 删除整对）。

    Claude CLI 把 `--tools ""` 解析为非法工具列表；保留会让 CLI 启动即报错，
    导致 stdout pipe 立即 EOF，被上层误判为"管道崩溃"。
    """
    out: list[str] = []
    skip_next = False
    for i, arg in enumerate(extra_args):
        if skip_next:
            skip_next = False
            continue
        # 形如 ["--tools", ""] 的相邻对：当前是 --flag、下一个是空字符串
        if (
            arg.startswith("--")
            and i + 1 < len(extra_args)
            and extra_args[i + 1] == ""
        ):
            skip_next = True
            continue
        out.append(arg)
    return out


# ── F7 心跳/超时参数（设计 [[F7-invoke_role-联合设计-2026-07-18]] §4）──
# HEARTBEAT：拍脑袋初值 300s（正常首 token 30-60s，5-10 倍余量）；
#   每次成功 call 落 max_stdout_gap_s 遥测到 audit.jsonl，5-10 任务后校准。
# HARD_TIMEOUT：1800s 有实测支撑（TL Plan+Detail 撞顶 1650s），从外层
#   subprocess 兜底内移到 CLI 层自持（invoke_role in-process 化后外层消失）。
_CLI_HEARTBEAT_S = 300.0
_CLI_POLL_S = 10.0
_CLI_HARD_TIMEOUT_S = 1800.0

_QUEUE_EOF = object()   # reader 线程送出的流结束哨兵


class CliHeartbeatTimeout(RuntimeError):
    """CLI 子进程超过 _CLI_HEARTBEAT_S 无任何 stdout 输出（F7 M1）。

    继承 RuntimeError → skills main.py 现有 except 映射 rc=1（∉ _PERMANENT_RC）
    → 外层 _execute_single 3 次退避重试自然接住。
    """


class CliHardTimeout(RuntimeError):
    """CLI 子进程总时长超过 _CLI_HARD_TIMEOUT_S（原外层 1800s 兜底内移）。"""


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """杀整个进程树。Windows 用 taskkill /T /F 覆盖孙进程（Popen.kill 只杀
    直接子进程，CLI 可能有 node 孙进程存活 —— F7 立项 §8 风险项）。"""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True, timeout=15,
            )
        else:
            proc.kill()
    except Exception as e:
        print(f"[f7] ⚠️ kill process tree 失败（{type(e).__name__}: {e}）", file=sys.stderr)
    try:
        proc.wait(timeout=10)
    except Exception:
        pass


def _iter_lines_with_heartbeat(
    proc: subprocess.Popen,
    line_queue,
    stats: dict,
    *,
    heartbeat_s: float = None,
    hard_timeout_s: float = None,
    poll_s: float = None,
) -> "list":
    """从 reader 线程的 queue 消费 stdout 行，同时执行 F7 三道判定。

    生成器：yield 原始 bytes 行（解析器无感知——行的来源从直接管道换成
    queue，解析逻辑零改动）。

    判定（主线程每 poll_s 醒一次）：
    - M1 心跳：距上次输出 > heartbeat_s → kill 进程树 + raise CliHeartbeatTimeout
    - 硬超时：总时长 > hard_timeout_s → kill + raise CliHardTimeout
      （有输出也算——与原外层 1800s 总墙钟语义一致）
    - M3 僵尸：proc 已 exit 但连续两个 poll 周期没等到 EOF 哨兵
      （R3 现象：ps 看不到 CLI 但父进程仍在读管道）→ 正常收尾 break

    stats（调用方传入 dict，原地更新）：max_gap_s —— G4 校准遥测。
    """
    hb = heartbeat_s if heartbeat_s is not None else _CLI_HEARTBEAT_S
    hard = hard_timeout_s if hard_timeout_s is not None else _CLI_HARD_TIMEOUT_S
    poll = poll_s if poll_s is not None else _CLI_POLL_S

    t_start = time.monotonic()
    last_output = t_start
    proc_exited_polls = 0
    while True:
        try:
            item = line_queue.get(timeout=poll)
        except queue.Empty:
            now = time.monotonic()
            if now - last_output > hb:
                _kill_process_tree(proc)
                raise CliHeartbeatTimeout(
                    f"CLI {hb:.0f}s 无 stdout 输出（距启动 {now - t_start:.0f}s），"
                    f"已 kill 进程树。F7 M1 心跳触发 —— 将由上层重试。"
                )
            if now - t_start > hard:
                _kill_process_tree(proc)
                raise CliHardTimeout(
                    f"CLI 总时长超 {hard:.0f}s 硬上限，已 kill 进程树。"
                )
            if proc.poll() is not None:
                # 进程已退出：给 reader 线程一个 poll 周期送 EOF；
                # 第二次仍空 → 僵尸/管道异常（R3 现象），主动收尾
                proc_exited_polls += 1
                if proc_exited_polls >= 2:
                    break
            continue
        if item is _QUEUE_EOF:
            break
        ts, raw = item
        gap = ts - last_output
        if gap > stats.get("max_gap_s", 0.0):
            stats["max_gap_s"] = gap
        last_output = ts
        if ts - t_start > hard:
            _kill_process_tree(proc)
            raise CliHardTimeout(
                f"CLI 总时长超 {hard:.0f}s 硬上限（输出仍在流动），已 kill 进程树。"
            )
        yield raw


def _call_cli(
    cli_cfg: dict, system_prompt: str, user_prompt: str,
    print_stream: bool,
    *,
    role_name: str | None = None,
    model_name: str | None = None,
) -> str:
    """通用 CLI 调用，输出格式由 cli_cfg.output_format 决定。

    - stream-json：逐行 JSON 事件（Claude Code CLI 格式）
    - plain：纯文本输出（Gemini CLI / Ollama CLI 等）
    - use_system_prompt_flag=True：用 --system-prompt 替换默认（推荐）
      False：把 system 内联到 user prompt 前部（兼容无此 flag 的 CLI）

    防 Windows pipe 死锁的护栏（M4，历史已在位）：
    1. `--system-prompt` 超过 _CMD_ARG_LIMIT 自动改走 stdin inline
    2. stdin 后台线程写入（不与 stdout 读互等）
    3. stderr=DEVNULL：--verbose 往 stderr 写进度，PIPE 不读会撑满死锁
    4. _filter_extra_args 剔除空字符串参数对

    F7 新增（2026-07-18，设计 [[F7-invoke_role-联合设计-2026-07-18]]）：
    5. stdout 读取移入 reader 线程 + queue；主线程执行 M1 心跳（300s 无输出
       kill+raise）/ M3 僵尸检测 / 1800s 硬超时内移；进程树 kill 用 taskkill /T
    6. 成功 call 落 llm_call 审计事件（含 max_stdout_gap_s 遥测 + stream-json
       result 事件里的 usage —— CLI telemetry 断点就此接通）
    """
    cli = shutil.which(cli_cfg["path"]) or cli_cfg["path"]
    extra_args = _filter_extra_args(list(cli_cfg.get("extra_args") or []))
    cmd = [cli] + extra_args

    if cli_cfg.get("model"):
        cmd.extend(["--model", cli_cfg["model"]])

    use_flag = bool(cli_cfg.get("use_system_prompt_flag", False))
    if use_flag and len(system_prompt) <= _CMD_ARG_LIMIT:
        cmd.extend(["--system-prompt", system_prompt])
        stdin_text = user_prompt
    else:
        # inline 模式：system 走 stdin（避开命令行长度限制 / 解析失败）
        stdin_text = (
            "=== 系统指令（必须严格遵守，覆盖默认助手行为）===\n"
            f"{system_prompt}\n\n"
            "=== 用户输入 ===\n"
            f"{user_prompt}"
        )

    stdin_bytes = stdin_text.encode("utf-8")

    t0 = time.monotonic()
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,   # 彻底丢弃 stderr，防 buffer 撑满
    )
    assert proc.stdin is not None and proc.stdout is not None

    # stdin 后台线程写入：避免 "stdin write 阻塞 + stdout 没读 → 死锁"
    write_exc: list[BaseException] = []

    def _write_stdin() -> None:
        try:
            proc.stdin.write(stdin_bytes)
        except (BrokenPipeError, OSError) as e:
            write_exc.append(e)
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass

    writer = threading.Thread(target=_write_stdin, daemon=True)
    writer.start()

    # F7：stdout 读取移入 reader 线程（主线程腾出来做心跳判定）
    line_queue: queue.Queue = queue.Queue()

    def _read_stdout() -> None:
        try:
            for raw in proc.stdout:
                line_queue.put((time.monotonic(), raw))
        except Exception:
            pass
        finally:
            line_queue.put(_QUEUE_EOF)

    reader = threading.Thread(target=_read_stdout, daemon=True)
    reader.start()

    stats: dict = {"max_gap_s": 0.0}
    lines = _iter_lines_with_heartbeat(proc, line_queue, stats)

    output_format = cli_cfg.get("output_format", "stream-json")
    chunks: list[str] = []
    usage: dict = {}

    if output_format == "stream-json":
        chunks, usage = _read_stream_json(lines, print_stream)
    elif output_format == "plain":
        for raw in lines:
            line = raw.decode("utf-8", errors="replace")
            if line:
                chunks.append(line)
                if print_stream:
                    print(line, end="", flush=True)
    else:
        raise ValueError(f"未知 output_format：{output_format}")

    writer.join(timeout=5)
    rc = proc.wait()
    elapsed = time.monotonic() - t0
    if print_stream:
        print()
    if rc != 0:
        # stderr 已 DEVNULL，无法回带原始消息；给出可操作 hint
        raise RuntimeError(
            f"{cli} 退出码 {rc}（stderr 已丢弃；可手工跑 `{' '.join(cmd[:6])} ...` 排查）"
        )
    # F7 遥测：max_stdout_gap_s（G4 阈值校准数据自动积累）+ CLI usage
    _append_token_audit("info", "llm_call", {
        "role": role_name or "(unknown)",
        "model": model_name or cli_cfg.get("model") or "(unknown)",
        "input_tokens": usage.get("input_tokens", -1),
        "output_tokens": usage.get("output_tokens", -1),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
        "elapsed_s": round(elapsed, 3),
        "max_stdout_gap_s": round(stats.get("max_gap_s", 0.0), 3),
        "track": "cli",
    })
    return "".join(chunks)


def _read_stream_json(lines, print_stream: bool) -> tuple[list[str], dict]:
    """解析 Claude Code CLI 的 stream-json 输出。

    lines：bytes 行迭代器（F7 后来自心跳 queue，解析逻辑不变）。
    返回 (chunks, usage)：usage 从最终 result 事件提取（CLI telemetry）。
    """
    chunks: list[str] = []
    usage: dict = {}
    seen_assistant = False
    for raw in lines:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            # 非 dict 的 JSON 行（如纯数字、字符串）跳过
            continue

        text = ""
        etype = event.get("type")
        if etype == "text":
            text = event.get("text", "")
        elif etype == "message":
            for blk in event.get("content", []):
                if blk.get("type") == "text":
                    text += blk.get("text", "")
        elif etype == "assistant":
            msg = event.get("message", {}) or {}
            for blk in msg.get("content", []):
                if blk.get("type") == "text":
                    text += blk.get("text", "")
            if text:
                seen_assistant = True
        elif etype == "result":
            # usage 提取（CLI telemetry 断点接通；字段防御性读取）
            u = event.get("usage")
            if isinstance(u, dict):
                usage = u
            if not seen_assistant:
                # 兜底：assistant 事件未给 text 时，从最终 result 取
                text = event.get("result", "") or ""

        if text:
            chunks.append(text)
            if print_stream:
                print(text, end="", flush=True)
    return chunks, usage


# ── 向后兼容（Phase 2b 旧接口）──────────────────────────
# 原 call_claude alias 已合并入 call_llm（统一 provider-agnostic 入口）。
# 保留 call_claude 仅供旧代码过渡，新代码请直接使用 call_llm。
call_claude = call_llm


def is_api_available() -> bool:
    """是否任一 Anthropic provider 的 API 可用（默认 ANTHROPIC_API_KEY）"""
    return bool(os.environ.get("ANTHROPIC_API_KEY", ""))


def is_cli_available() -> bool:
    """claude CLI 是否在 PATH 中"""
    return shutil.which("claude") is not None
