"""
智能网关 - 消息路由、Agent 调度和响应生成
架构层次：
  gateway.py          ← 本文件：IntelligentGateway 编排层
  providers/          ← LLM 接入层（CliApiRouter 等）
  core/token_utils.py ← Token 工具
  core/routing.py     ← 路由规则与 @mention 解析
"""
import re
import json
import uuid
import time
import asyncio
from openai import AsyncOpenAI

from config import AGENTS, LLM_PROVIDERS, DEFAULT_PROVIDER, Message, RouteDecision
from core.token_utils import trim_history_by_tokens, truncate_content
from core.routing import parse_mentions, ROUTING_SYSTEM, ROUTING_RULES
from providers.cli_api_router import CliApiRouter, ClaudeRouter

# ── 向下兼容：测试和外部代码可继续 from gateway import _parse_mentions ──
_parse_mentions = parse_mentions


# ── OpenAI-compat 客户端工厂 ──────────────────────────────────────

def _make_client(provider_name: str) -> tuple[AsyncOpenAI, str]:
    """创建 OpenAI-compatible 客户端，返回 (client, model)"""
    cfg = LLM_PROVIDERS.get(provider_name) or LLM_PROVIDERS[DEFAULT_PROVIDER]
    client = AsyncOpenAI(
        api_key=cfg["api_key"] or "placeholder",
        base_url=cfg["base_url"],
    )
    return client, cfg["model"]


# ── 需要走双轨路由器的 mode 值 ────────────────────────────────────
_DUAL_TRACK_MODES = {"dual_track", "cli_only", "claude_auto", "claude_cli"}


class IntelligentGateway:
    """智能消息网关：接收用户消息 → 主持人路由 → 调度目标 Agent → 返回响应"""

    def __init__(self):
        self._clients: dict[str, tuple[AsyncOpenAI, str]] = {}
        self._routers: dict[str, CliApiRouter] = {}
        self.conversation_histories: dict[str, list[dict]] = {}
        self._history_locks: dict[str, asyncio.Lock] = {}

    # ── 内部：Provider 路由器懒加载 ───────────────────────────────

    def _get_router(self, provider_name: str) -> CliApiRouter:
        if provider_name not in self._routers:
            cfg = LLM_PROVIDERS[provider_name]
            self._routers[provider_name] = CliApiRouter(provider_name, cfg)
        return self._routers[provider_name]

    # 向下兼容
    def _get_claude_router(self) -> CliApiRouter:
        return self._get_router("claude")

    def _get_client(self, agent_id: str) -> tuple[AsyncOpenAI, str]:
        provider = AGENTS.get(agent_id, {}).get("llm_provider", DEFAULT_PROVIDER)
        if provider not in self._clients:
            self._clients[provider] = _make_client(provider)
        return self._clients[provider]

    # ── 内部：历史管理 ────────────────────────────────────────────

    def _get_lock(self, meeting_id: str) -> asyncio.Lock:
        if meeting_id not in self._history_locks:
            self._history_locks[meeting_id] = asyncio.Lock()
        return self._history_locks[meeting_id]

    def _get_history(self, meeting_id: str) -> list[dict]:
        return self.conversation_histories.setdefault(meeting_id, [])

    def _append_history(self, meeting_id: str, role: str, content: str, name: str = None):
        entry = {"role": role, "content": content}
        if name:
            entry["name"] = name
        hist = self._get_history(meeting_id)
        hist.append(entry)
        if len(hist) > 60:
            self.conversation_histories[meeting_id] = hist[-60:]

    # ── 内部：统一 LLM 调用入口 ───────────────────────────────────

    async def _call_llm(
        self,
        agent_id: str,
        system_prompt: str,
        messages: list[dict],
        stream_callback=None,
        temperature: float = 0.7,
        max_tokens: int = 1000,
    ) -> str:
        provider = AGENTS.get(agent_id, {}).get("llm_provider", DEFAULT_PROVIDER)
        cfg = LLM_PROVIDERS.get(provider, {})
        mode = cfg.get("mode", "openai_compat")

        if mode in _DUAL_TRACK_MODES:
            router = self._get_router(provider)
            print(f"[Gateway] {agent_id} → {router.active_label}")
            return await router.call(
                system_prompt, messages, stream_callback, temperature, max_tokens
            )

        if provider not in self._clients:
            self._clients[provider] = _make_client(provider)
        client, model = self._clients[provider]
        print(f"[Gateway] {agent_id} → provider={provider} model={model}")

        full_messages = [{"role": "system", "content": system_prompt}] + messages
        if stream_callback:
            stream = await client.chat.completions.create(
                model=model, messages=full_messages,
                stream=True, temperature=temperature, max_tokens=max_tokens,
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
                model=model, messages=full_messages,
                stream=False, temperature=temperature, max_tokens=max_tokens,
            )
            return resp.choices[0].message.content

    # ── 公开：Step 1 路由 ─────────────────────────────────────────

    async def route_message(self, user_message: str, meeting_id: str) -> RouteDecision:
        """解析 @mention 或由主持人 LLM 智能路由"""
        mentions = parse_mentions(user_message)
        if mentions:
            names = "、".join(AGENTS[aid]["name"] for aid in mentions if aid in AGENTS)
            return RouteDecision(route_to=mentions, broadcast=False, summary=f"用户直接点名：{names}")

        history = self._get_history(meeting_id)
        recent = trim_history_by_tokens(history, max_tokens=400, keep_last=2)
        routing_prompt = ROUTING_RULES + f"\n\n用户消息：{truncate_content(user_message, 200)}"
        route_messages = recent + [{"role": "user", "content": routing_prompt}]

        raw = ""
        try:
            raw = await self._call_llm(
                "moderator", ROUTING_SYSTEM, route_messages,
                temperature=0.1, max_tokens=80,
            )
            json_match = re.search(r'\{[^{}]*\}', raw, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
                valid_ids = [
                    aid for aid in data.get("route_to", [])
                    if aid in AGENTS and aid != "moderator"
                ]
                return RouteDecision(
                    route_to=valid_ids or ["architect"],
                    broadcast=data.get("broadcast", False),
                    summary=data.get("summary", "正在为您路由消息..."),
                )
        except Exception as e:
            print(f"[Gateway] 路由解析失败: {e}, raw={raw!r}")

        return RouteDecision(route_to=["architect"], broadcast=False, summary="已将消息转发给架构师")

    # ── 公开：Step 2 Agent 响应 ───────────────────────────────────

    async def generate_agent_response(
        self,
        agent_id: str,
        user_message: str,
        meeting_id: str,
        stream_callback=None,
    ) -> str:
        agent = AGENTS[agent_id]
        history = self._get_history(meeting_id)

        provider = agent.get("llm_provider", DEFAULT_PROVIDER)
        history_budget = 800 if provider in ("claude", "gemini") else 600
        trimmed = trim_history_by_tokens(history, max_tokens=history_budget, keep_last=4)

        agent_max_tokens = (
            agent.get("max_tokens")
            or LLM_PROVIDERS.get(provider, {}).get("max_tokens")
            or 1000
        )
        agent_temperature = (
            agent.get("temperature")
            or LLM_PROVIDERS.get(provider, {}).get("temperature")
            or 0.7
        )
        messages = trimmed + [{"role": "user", "content": user_message}]

        try:
            return await self._call_llm(
                agent_id, agent["system_prompt"], messages,
                stream_callback=stream_callback,
                temperature=float(agent_temperature),
                max_tokens=int(agent_max_tokens),
            )
        except Exception as e:
            err_msg = f"⚠️ [{agent['name']}] 响应失败：{e}"
            print(f"[Gateway] {err_msg}")
            if stream_callback:
                await stream_callback(err_msg)
            return err_msg

    # ── 公开：Step 3 主持人总结 ───────────────────────────────────

    async def generate_moderator_summary(
        self,
        user_message: str,
        agent_responses: dict[str, str],
        meeting_id: str,
        stream_callback=None,
    ) -> str:
        moderator = AGENTS["moderator"]
        responses_text = "\n\n".join(
            f"【{AGENTS[aid]['name']}】：{truncate_content(resp, 400)}"
            for aid, resp in agent_responses.items()
        )
        summary_prompt = (
            f"提问：{truncate_content(user_message, 100)}\n\n"
            f"专家回复：\n{responses_text}\n\n"
            "请用2句话做会议纪要，并给出下一步行动建议。"
        )
        messages = [{"role": "user", "content": summary_prompt}]
        try:
            return await self._call_llm(
                "moderator", moderator["system_prompt"], messages,
                stream_callback=stream_callback,
                temperature=0.5, max_tokens=200,
            )
        except Exception as e:
            err = f"⚠️ 主持人总结失败：{e}"
            print(f"[Gateway] {err}")
            return err

    # ── 公开：历史管理 ────────────────────────────────────────────

    async def update_history(
        self, meeting_id: str, user_message: str, agent_id: str, agent_response: str
    ):
        """线程安全地更新对话历史"""
        async with self._get_lock(meeting_id):
            self._append_history(meeting_id, "user", truncate_content(user_message, 500))
            self._append_history(
                meeting_id, "assistant",
                truncate_content(agent_response, 600), name=agent_id,
            )

    def clear_history(self, meeting_id: str):
        self.conversation_histories.pop(meeting_id, None)
        self._history_locks.pop(meeting_id, None)

    # ── 公开：消息构建 ────────────────────────────────────────────

    def build_message(
        self,
        sender_id: str,
        content: str,
        meeting_id: str,
        message_type: str = "agent",
        target_agents: list[str] = None,
    ) -> Message:
        agent = AGENTS.get(sender_id, {})
        return Message(
            id=str(uuid.uuid4()),
            sender_id=sender_id,
            sender_name=agent.get("name", sender_id),
            sender_avatar=agent.get("avatar", "🤖"),
            sender_color=agent.get("color", "#888"),
            content=content,
            timestamp=time.time(),
            message_type=message_type,
            target_agents=target_agents,
            meeting_id=meeting_id,
        )
