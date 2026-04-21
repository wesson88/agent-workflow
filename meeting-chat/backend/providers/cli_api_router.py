"""
通用双轨路由器（API Key ↔ CLI，任何 Provider 均可使用）
由 llm_providers.yaml 中的 mode / prefer / cli_* 字段驱动。

支持的 mode：
  dual_track  - API Key 与 CLI 互为备用（prefer 控制优先级）
  cli_only    - 仅走 CLI，不使用 API Key

CLI 输出格式（cli_output_format）：
  stream_json - 逐行 JSON 流（Claude Code CLI 格式）
  plain       - 纯文本输出（Gemini CLI / Ollama / 大多数 CLI 工具）

向下兼容旧 mode 别名：
  claude_auto  → dual_track + prefer=auto
  claude_cli   → dual_track + prefer=cli
"""
import json
import asyncio
import shutil
from openai import AsyncOpenAI


class CliApiRouter:
    """通用双轨路由器"""

    # 旧 mode 别名 → (统一 mode, prefer)
    _LEGACY_MODE_MAP = {
        "claude_auto": ("dual_track", "auto"),
        "claude_cli":  ("dual_track", "cli"),
    }

    def __init__(self, provider_name: str, cfg: dict):
        self._provider_name = provider_name

        raw_mode = cfg.get("mode", "dual_track")
        if raw_mode in self._LEGACY_MODE_MAP:
            resolved_mode, default_prefer = self._LEGACY_MODE_MAP[raw_mode]
        else:
            resolved_mode, default_prefer = raw_mode, "auto"

        self._mode        = resolved_mode
        self._prefer      = cfg.get("prefer", default_prefer)
        self._api_key     = cfg.get("api_key", "")
        self._base_url    = cfg.get("base_url", "")
        self._api_model   = cfg.get("model", "")
        self._cli_path    = cfg.get("cli_path", "")
        self._cli_model   = cfg.get("cli_model", self._api_model)
        self._cli_out_fmt = cfg.get("cli_output_format", "plain")
        self._timeout     = cfg.get("timeout", 120)
        self._api_client: AsyncOpenAI | None = None

        self._api_available = bool(self._api_key)
        # 解析 CLI 的实际可执行路径（Windows 上 shutil.which 会返回 .cmd/.exe 全路径）。
        # 不替换原始 cli_path，避免影响日志显示。
        self._cli_resolved = shutil.which(self._cli_path) if self._cli_path else None
        self._cli_available = bool(self._cli_resolved)
        self._active = self._resolve_active()

        print(
            f"[CliApiRouter:{provider_name}] mode={self._mode} prefer={self._prefer} "
            f"api={'✓' if self._api_available else '✗'} "
            f"cli={'✓' if self._cli_available else '✗'} "
            f"→ active={self._active}"
        )

    def _resolve_active(self) -> str:
        if self._mode == "cli_only":
            return "cli" if self._cli_available else "unavailable"
        prefer = self._prefer
        if prefer == "api":
            return "api" if self._api_available else "unavailable"
        if prefer == "cli":
            if self._cli_available:
                return "cli"
            return "api" if self._api_available else "unavailable"
        # auto
        if self._api_available:
            return "api"
        if self._cli_available:
            return "cli"
        return "unavailable"

    def _get_api_client(self) -> AsyncOpenAI:
        if self._api_client is None:
            self._api_client = AsyncOpenAI(
                api_key=self._api_key,
                base_url=self._base_url,
            )
        return self._api_client

    @property
    def active_label(self) -> str:
        labels = {
            "api":         f"{self._provider_name.title()} API",
            "cli":         f"{self._provider_name.title()} CLI",
            "unavailable": "⚠️ 不可用",
        }
        return labels.get(self._active, self._active)

    async def call(
        self,
        system_prompt: str,
        messages: list[dict],
        stream_callback=None,
        temperature: float = 0.7,
        max_tokens: int = 1000,
    ) -> str:
        if self._active == "api":
            return await self._call_api(
                system_prompt, messages, stream_callback, temperature, max_tokens
            )
        if self._active == "cli":
            return await self._call_cli(system_prompt, messages, stream_callback)
        raise RuntimeError(
            f"[{self._provider_name}] 不可用：\n"
            f"  API 轨道：在 .env 中配置对应的 API_KEY\n"
            f"  CLI 轨道：安装对应 CLI 工具并确保可在 PATH 中找到 '{self._cli_path}'"
        )

    async def _call_api(
        self, system_prompt, messages, stream_callback, temperature, max_tokens
    ) -> str:
        """走 API Key 路径（OpenAI 兼容接口）"""
        client = self._get_api_client()
        full_messages = [{"role": "system", "content": system_prompt}] + messages

        if stream_callback:
            stream = await client.chat.completions.create(
                model=self._api_model,
                messages=full_messages,
                stream=True,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            result = ""
            async for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta.content or ""
                if delta:
                    result += delta
                    await stream_callback(delta)
            return result
        else:
            resp = await client.chat.completions.create(
                model=self._api_model,
                messages=full_messages,
                stream=False,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return resp.choices[0].message.content

    async def _call_cli(self, system_prompt, messages, stream_callback) -> str:
        """走本地 CLI 路径，输出格式由 cli_output_format 配置决定"""
        prompt_parts = [f"[系统指令]\n{system_prompt}\n"]
        for m in messages:
            role_label = "用户" if m["role"] == "user" else "助手"
            prompt_parts.append(f"[{role_label}]\n{m['content']}")
        prompt_text = "\n\n".join(prompt_parts)

        cli_exe = self._cli_resolved or self._cli_path
        if self._cli_out_fmt == "stream_json":
            cmd = [
                cli_exe, "--print", "--verbose",
                "--output-format", "stream-json",
                "--model", self._cli_model,
            ]
        else:
            cmd = [cli_exe, self._cli_model, "-"]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        proc.stdin.write(prompt_text.encode("utf-8"))
        proc.stdin.close()

        result = ""
        if self._cli_out_fmt == "stream_json":
            seen_assistant_text = False
            async for raw_line in proc.stdout:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    text = ""
                    etype = event.get("type")
                    # 兼容多种 Claude CLI stream-json 事件格式
                    if etype == "text":
                        text = event.get("text", "")
                    elif etype == "message":
                        for blk in event.get("content", []):
                            if blk.get("type") == "text":
                                text += blk.get("text", "")
                    elif etype == "assistant":
                        # 新格式：{"type":"assistant","message":{"content":[{"type":"text","text":"..."}]}}
                        msg = event.get("message", {}) or {}
                        for blk in msg.get("content", []):
                            if blk.get("type") == "text":
                                text += blk.get("text", "")
                        if text:
                            seen_assistant_text = True
                    elif etype == "result" and not seen_assistant_text:
                        # 兜底：assistant 事件里无 text 时，从 result 取最终文本
                        text = event.get("result", "") or ""
                    if text:
                        result += text
                        if stream_callback:
                            await stream_callback(text)
                except json.JSONDecodeError:
                    pass
        else:
            async for raw_line in proc.stdout:
                line = raw_line.decode("utf-8", errors="replace")
                if line:
                    result += line
                    if stream_callback:
                        await stream_callback(line)

        retcode = await proc.wait()
        if retcode != 0:
            stderr = await proc.stderr.read()
            err = stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"{self._cli_path} 退出码 {retcode}：{err}")
        return result


# 向下兼容别名
ClaudeRouter = CliApiRouter
