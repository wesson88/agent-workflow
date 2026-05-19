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
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import yaml


_PROVIDERS_FILE = Path(__file__).parent / "llm_providers.yaml"
_providers_cache: dict[str, dict] | None = None

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
    system_prompt: str | tuple[str, str],
    user_prompt: str,
    *,
    model: str,
    max_tokens: int = 4096,
    print_stream: bool = True,
    input_budget: int | None = None,
) -> str:
    """统一 LLM 调用入口。

    参数：
        system_prompt: 系统提示词，或 (static, dynamic) 两段 tuple。
            tuple 形式仅对 Anthropic SDK 路径生效：static 块打 cache_control，
            dynamic 块不缓存；CLI 路径自动拼接为单字符串。
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

    # 规范化 system_prompt 为 (static, dynamic)，给审计 + 各路径分发统一格式
    if isinstance(system_prompt, tuple):
        static, dynamic = system_prompt
    else:
        static, dynamic = system_prompt, ""

    # 入口审计：在真正调用 LLM 前过两道护栏（system 单独阈值 + 总量百分比/角色预算）
    _audit_token_budget(model, static, dynamic, user_prompt, input_budget=input_budget)

    if track == "api":
        api_cfg = cfg["api"]
        kind = api_cfg.get("kind", "anthropic")
        if kind == "anthropic":
            return _call_anthropic_sdk(api_cfg, static, dynamic, user_prompt, max_tokens, print_stream)
        # openai_compat：拼接为单字符串
        flat = "\n\n".join(filter(None, [static, dynamic])) if dynamic else static
        if kind == "openai_compat":
            return _call_openai_compat(api_cfg, flat, user_prompt, max_tokens, print_stream)
        raise ValueError(f"未知 api kind：{kind}（provider={model}）")

    if track == "cli":
        flat = "\n\n".join(filter(None, [static, dynamic])) if dynamic else static
        return _call_cli(cfg["cli"], flat, user_prompt, print_stream)

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

    # 护栏 1：system prompt 单独阈值
    if sys_tok > _SYSTEM_RAISE_TOKENS:
        raise RuntimeError(
            f"[audit] system prompt 过大（{sys_tok} tokens > {_SYSTEM_RAISE_TOKENS} "
            f"阈值）— static={static_tok}, dynamic={dynamic_tok}。"
            f" 排查建议：(1) 角色笔记是否含未被 build_system_prompt 抽取的冗余章节；"
            f" (2) DYNAMIC 区是否累积过多、需要 graduator/reflector 收敛；"
            f" (3) 上游 role.upstream 链是否过长。"
        )
    if sys_tok > _SYSTEM_WARN_TOKENS:
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
        budget_desc = f"角色预算 {input_budget}"
        if total_tok >= raise_at:
            raise RuntimeError(
                f"[audit] input token 超角色预算（{total_tok} ≥ {raise_at}）— "
                f"system={sys_tok}, user={user_tok}, model={model}。"
                f"建议：拆分 user prompt 或精简 system；"
                f"或角色 frontmatter 调高 budget_input_tokens。"
            )
        if total_tok >= warn_at:
            print(
                f"[audit] ⚠️ input token 接近角色预算（{total_tok} ≥ {warn_at}, "
                f"上限 {raise_at}）— system={sys_tok}, user={user_tok}, model={model}。",
                file=sys.stderr,
            )
        return

    # 未声明 input_budget：走 context_window 百分比
    ratio = total_tok / cw if cw else 0.0
    if ratio >= _TOTAL_RAISE_RATIO:
        raise RuntimeError(
            f"[audit] input token 总量触顶（{total_tok}/{cw} = {ratio:.1%} ≥ "
            f"{_TOTAL_RAISE_RATIO:.0%}）— system={sys_tok}, user={user_tok}, "
            f"model={model}。建议：拆分 user prompt / 精简 system / 角色加 "
            f"budget_input_tokens 显式声明；继续调用预计将被 SDK 拒绝。"
        )
    if ratio >= _TOTAL_WARN_RATIO:
        print(
            f"[audit] ⚠️ input token 偏高（{total_tok}/{cw} = {ratio:.1%} ≥ "
            f"{_TOTAL_WARN_RATIO:.0%}）— system={sys_tok}, user={user_tok}, "
            f"model={model}。",
            file=sys.stderr,
        )


# ── Anthropic SDK ────────────────────────────────────────
def _call_anthropic_sdk(
    api_cfg: dict, system_static: str, system_dynamic: str, user_prompt: str,
    max_tokens: int, print_stream: bool,
) -> str:
    """调用 Anthropic SDK，静态/动态 system prompt 分块缓存。

    - system_static：角色设定 + 全局约束 + 输出格式规范，几乎不变 → cache_control=ephemeral
    - system_dynamic：DYNAMIC 补丁（每轮可能变化）→ 不缓存
    - 缓存有效期 5 分钟（同一 API key 内跨请求共享），命中后费用降至 1/10
    """
    import anthropic  # 延迟 import
    key = os.environ.get(api_cfg["key_env"], "")
    client = anthropic.Anthropic(api_key=key, timeout=300.0) if key else anthropic.Anthropic(timeout=300.0)

    system_block: list[dict] = [
        {"type": "text", "text": system_static, "cache_control": {"type": "ephemeral"}},
    ]
    if system_dynamic.strip():
        system_block.append({"type": "text", "text": system_dynamic})

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
            print(
                f"[tokens] input={usage.input_tokens}"
                f"(cache_read={getattr(usage, 'cache_read_input_tokens', 0)}"
                f" cache_create={getattr(usage, 'cache_creation_input_tokens', 0)})"
                f" output={usage.output_tokens}"
                f" total={usage.input_tokens + usage.output_tokens}"
                f" elapsed={elapsed:.1f}s"
            )
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


# ── OpenAI 兼容 SDK（GPT / DeepSeek / Ollama / 国产模型）─
def _call_openai_compat(
    api_cfg: dict, system_prompt: str, user_prompt: str,
    max_tokens: int, print_stream: bool,
) -> str:
    from openai import OpenAI  # 延迟 import
    key = os.environ.get(api_cfg["key_env"], "") or "sk-no-key"  # ollama 等不验 key
    client = OpenAI(api_key=key, base_url=api_cfg.get("base_url"))
    chunks: list[str] = []
    stream = client.chat.completions.create(
        model=api_cfg["model"],
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=max_tokens,
        stream=True,
    )
    for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta.content
        if delta:
            if print_stream:
                print(delta, end="", flush=True)
            chunks.append(delta)
    if print_stream:
        print()
    return "".join(chunks)


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


def _call_cli(
    cli_cfg: dict, system_prompt: str, user_prompt: str,
    print_stream: bool,
) -> str:
    """通用 CLI 调用，输出格式由 cli_cfg.output_format 决定。

    - stream-json：逐行 JSON 事件（Claude Code CLI 格式）
    - plain：纯文本输出（Gemini CLI / Ollama CLI 等）
    - use_system_prompt_flag=True：用 --system-prompt 替换默认（推荐）
      False：把 system 内联到 user prompt 前部（兼容无此 flag 的 CLI）

    防 Windows pipe 死锁的三道护栏：
    1. `--system-prompt` 超过 _CMD_ARG_LIMIT 自动改走 stdin inline
       （Windows 命令行总长 32767 限制 + 长参数易触发 CLI 解析失败）
    2. stdin 后台线程写入；主线程同步读 stdout（不互等）
    3. stderr=DEVNULL：彻底丢弃 stderr buffer。注意 Claude CLI 的
       --verbose（stream-json 必需）会往 stderr 写进度信息，若用 PIPE 不读
       会撑满 buffer 导致整个 pipe 死锁——这是真正的根因。
    另外：_filter_extra_args 剔除空字符串值的参数对（如 --tools ""），
    避免 CLI 启动即被参数解析失败拒绝、stdout 立即 EOF 被误判为崩溃。
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

    output_format = cli_cfg.get("output_format", "stream-json")
    chunks: list[str] = []

    if output_format == "stream-json":
        chunks = _read_stream_json(proc.stdout, print_stream)
    elif output_format == "plain":
        for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace")
            if line:
                chunks.append(line)
                if print_stream:
                    print(line, end="", flush=True)
    else:
        raise ValueError(f"未知 output_format：{output_format}")

    writer.join(timeout=5)
    rc = proc.wait()
    if print_stream:
        print()
    if rc != 0:
        # stderr 已 DEVNULL，无法回带原始消息；给出可操作 hint
        raise RuntimeError(
            f"{cli} 退出码 {rc}（stderr 已丢弃；可手工跑 `{' '.join(cmd[:6])} ...` 排查）"
        )
    return "".join(chunks)


def _read_stream_json(stdout, print_stream: bool) -> list[str]:
    """解析 Claude Code CLI 的 stream-json 输出。"""
    chunks: list[str] = []
    seen_assistant = False
    for raw in stdout:
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
        elif etype == "result" and not seen_assistant:
            # 兜底：assistant 事件未给 text 时，从最终 result 取
            text = event.get("result", "") or ""

        if text:
            chunks.append(text)
            if print_stream:
                print(text, end="", flush=True)
    return chunks


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
