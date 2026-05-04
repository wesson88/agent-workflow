"""
engine/llm.py — LLM 调用抽象（API key ↔ Claude Code CLI 双轨）

设计动机：
- 用户使用 Claude Code MAX 订阅时无 API key，希望走 `claude --print`（CLI）路径
- 也支持传统 ANTHROPIC_API_KEY → Anthropic SDK 路径
- 选择逻辑：显式 prefer / LLM_PROVIDER 环境变量 / 自动（API key 在 → SDK，否则 → CLI）

CLI 参考实现：[meeting-chat CliApiRouter](../../meeting-chat/backend/providers/cli_api_router.py)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys


# ── 可用性探测 ──────────────────────────────────────────
def is_api_available() -> bool:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    return key.startswith("sk-")


def is_cli_available() -> bool:
    return shutil.which("claude") is not None


def _resolve_provider(prefer: str | None) -> str:
    """返回 'api' / 'cli' / 'unavailable'。"""
    p = (prefer or os.environ.get("LLM_PROVIDER", "auto")).lower()
    if p == "api":
        return "api" if is_api_available() else "unavailable"
    if p == "cli":
        return "cli" if is_cli_available() else "unavailable"
    # auto
    if is_api_available():
        return "api"
    if is_cli_available():
        return "cli"
    return "unavailable"


# ── 公共入口 ────────────────────────────────────────────
def call_claude(
    system_prompt: str,
    user_prompt: str,
    *,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 4096,
    prefer: str | None = None,
    print_stream: bool = True,
) -> str:
    provider = _resolve_provider(prefer)
    if provider == "api":
        return _call_via_sdk(
            system_prompt, user_prompt, model, max_tokens, print_stream
        )
    if provider == "cli":
        return _call_via_cli(
            system_prompt, user_prompt, model, print_stream
        )
    raise RuntimeError(
        "Claude 不可用：\n"
        "  - 走 SDK：在 .env / 环境变量里设置 ANTHROPIC_API_KEY=sk-...\n"
        "  - 走 CLI：安装 Claude Code，确保 `claude` 可在 PATH 找到\n"
        f"  当前探测：API={'✓' if is_api_available() else '✗'}, "
        f"CLI={'✓' if is_cli_available() else '✗'}"
    )


# ── SDK 路径 ────────────────────────────────────────────
def _call_via_sdk(
    system_prompt: str,
    user_prompt: str,
    model: str,
    max_tokens: int,
    print_stream: bool,
) -> str:
    import anthropic  # 延迟 import，CLI 用户不需要装这个包
    client = anthropic.Anthropic()
    chunks: list[str] = []
    with client.messages.stream(
        model=model,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    ) as stream:
        for text in stream.text_stream:
            if print_stream:
                print(text, end="", flush=True)
            chunks.append(text)
    if print_stream:
        print()
    return "".join(chunks)


# ── CLI 路径 ────────────────────────────────────────────
def _call_via_cli(
    system_prompt: str,
    user_prompt: str,
    model: str,
    print_stream: bool,
) -> str:
    """走 `claude --print --output-format stream-json --model X --system-prompt ...`。

    `--system-prompt` 完全替换 Claude Code 的默认 system prompt，
    避免把角色提示当作"prompt injection"被识别拦截。
    user prompt 通过 stdin 传入，避免命令行长度限制。
    """
    cli = shutil.which("claude") or "claude"
    cmd = [
        cli, "--print", "--verbose",
        "--output-format", "stream-json",
        "--model", model,
        "--system-prompt", system_prompt,
        # 工具不需要：本调用是纯文本生成，禁用所有工具加快响应、避免误用
        "--tools", "",
    ]

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    proc.stdin.write(user_prompt.encode("utf-8"))
    proc.stdin.close()

    result_chunks: list[str] = []
    seen_assistant_text = False

    for raw_line in proc.stdout:
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
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
            # Claude Code 新 stream-json 格式
            msg = event.get("message", {}) or {}
            for blk in msg.get("content", []):
                if blk.get("type") == "text":
                    text += blk.get("text", "")
            if text:
                seen_assistant_text = True
        elif etype == "result" and not seen_assistant_text:
            # 兜底：assistant 事件未给 text 时，从最终 result 取
            text = event.get("result", "") or ""

        if text:
            result_chunks.append(text)
            if print_stream:
                print(text, end="", flush=True)

    rc = proc.wait()
    if print_stream:
        print()
    if rc != 0:
        err = proc.stderr.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"claude CLI 退出码 {rc}：{err}")
    return "".join(result_chunks)
