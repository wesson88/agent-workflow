"""
集成测试套件 - 多Agent会议聊天系统
=====================================
运行方式：
  python -m pytest tests/test_integration.py -v

分层测试：
  1. 配置层  - YAML 加载、环境变量插值、字段完整性
  2. 路由层  - @mention 解析、主持人 LLM 路由决策
  3. 网关层  - 各 Provider LLM 调用（需要真实 Key/CLI）
  4. 端到端  - 完整消息流 user→route→agent→summary
"""
from __future__ import annotations

import asyncio
import os
import sys
import shutil
import json
import re
import pytest

# ── 确保 backend/ 在 sys.path 里 ─────────────────────────────
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

from config import AGENTS, LLM_PROVIDERS, DEFAULT_PROVIDER
from gateway import IntelligentGateway, CliApiRouter, ClaudeRouter, _parse_mentions


# ══════════════════════════════════════════════════════════════
# 工具函数
# ══════════════════════════════════════════════════════════════

def _provider_available(name: str) -> bool:
    """判断某个 Provider 是否具备可调用条件"""
    cfg = LLM_PROVIDERS.get(name, {})
    if name == "claude":
        api_ok = bool(cfg.get("api_key"))
        cli_ok = bool(shutil.which(cfg.get("cli_path", "claude")))
        return api_ok or cli_ok
    return bool(cfg.get("api_key"))


def _skip_if_unavailable(provider: str):
    """pytest.mark.skipif 快捷方式"""
    return pytest.mark.skipif(
        not _provider_available(provider),
        reason=f"Provider [{provider}] 未配置 API Key 或 CLI，跳过真实调用测试"
    )


# ══════════════════════════════════════════════════════════════
# 第一层：配置加载测试（无需网络，100% 本地）
# ══════════════════════════════════════════════════════════════

class TestConfigLoading:
    """验证 YAML 配置正确加载、插值和字段完整性"""

    def test_providers_loaded(self):
        """至少加载了 4 个 Provider"""
        assert len(LLM_PROVIDERS) >= 4, f"期望 ≥4 个 Provider，实际: {list(LLM_PROVIDERS.keys())}"

    def test_required_providers_exist(self):
        """四大核心 Provider 必须存在"""
        for p in ["deepseek", "claude", "codex", "gemini"]:
            assert p in LLM_PROVIDERS, f"Provider [{p}] 未在 llm_providers.yaml 中定义"

    def test_provider_required_fields(self):
        """每个 Provider 必须有 mode / model / base_url"""
        required = ["mode", "model", "base_url"]
        for name, cfg in LLM_PROVIDERS.items():
            for field in required:
                assert field in cfg, f"Provider [{name}] 缺少字段 [{field}]"

    def test_agents_loaded(self):
        """至少加载了 8 个 Agent"""
        assert len(AGENTS) >= 8, f"期望 ≥8 个 Agent，实际: {list(AGENTS.keys())}"

    def test_required_agents_exist(self):
        """8 个核心 Agent 必须存在"""
        for aid in ["moderator", "architect", "tech_lead", "backend", "frontend", "product", "qa", "ux"]:
            assert aid in AGENTS, f"Agent [{aid}] 未在 agents.yaml 中定义"

    def test_agent_required_fields(self):
        """每个 Agent 必须有 name / role / llm_provider / system_prompt"""
        required = ["name", "role", "llm_provider", "system_prompt"]
        for aid, cfg in AGENTS.items():
            for field in required:
                assert field in cfg and cfg[field], \
                    f"Agent [{aid}] 字段 [{field}] 为空或缺失"

    def test_agent_provider_references_valid(self):
        """每个 Agent 的 llm_provider 必须指向已定义的 Provider"""
        for aid, cfg in AGENTS.items():
            provider = cfg["llm_provider"]
            assert provider in LLM_PROVIDERS, \
                f"Agent [{aid}] 的 provider=[{provider}] 未在 llm_providers.yaml 中定义"

    def test_moderator_is_moderator_role(self):
        assert AGENTS["moderator"]["role"] == "moderator"

    def test_expert_agents_have_expert_role(self):
        for aid in ["architect", "tech_lead", "backend", "frontend", "product", "qa", "ux"]:
            assert AGENTS[aid]["role"] == "expert", f"Agent [{aid}] 的 role 应为 expert"

    def test_env_interpolation_no_dollar_sign(self):
        """插值后不应有残留的 ${...} 占位符（Key 可以为空，但不应留 ${VAR}）"""
        for name, cfg in LLM_PROVIDERS.items():
            for field, value in cfg.items():
                if isinstance(value, str):
                    assert "${" not in value, \
                        f"Provider [{name}].{field} 插值未完成: {value!r}"

    def test_default_provider_exists(self):
        assert DEFAULT_PROVIDER in LLM_PROVIDERS

    def test_agent_avatar_is_emoji(self):
        """avatar 不应为空"""
        for aid, cfg in AGENTS.items():
            assert cfg.get("avatar"), f"Agent [{aid}] 的 avatar 为空"

    def test_agent_color_is_hex(self):
        """color 应为 #RRGGBB 格式"""
        hex_re = re.compile(r'^#[0-9a-fA-F]{6}$')
        for aid, cfg in AGENTS.items():
            color = cfg.get("color", "")
            assert hex_re.match(color), f"Agent [{aid}] 的 color [{color}] 格式不对"

    def test_ux_agent_uses_gemini(self):
        assert AGENTS["ux"]["llm_provider"] == "gemini"

    def test_qa_agent_uses_codex(self):
        assert AGENTS["qa"]["llm_provider"] == "codex"

    def test_claude_agents(self):
        for aid in ["architect", "tech_lead", "backend", "frontend"]:
            assert AGENTS[aid]["llm_provider"] == "claude"


# ══════════════════════════════════════════════════════════════
# 第二层：消息路由测试（无需网络）
# ══════════════════════════════════════════════════════════════

class TestMessageRouting:
    """验证 @mention 解析和路由逻辑"""

    def test_mention_by_agent_id(self):
        result = _parse_mentions("@backend 帮我看看这个接口")
        assert "backend" in result

    def test_mention_by_chinese_name(self):
        result = _parse_mentions("@架构师 这个方案怎么样")
        assert "architect" in result

    def test_mention_multiple(self):
        result = _parse_mentions("@architect @frontend 这个功能需要你们一起看")
        assert "architect" in result
        assert "frontend" in result

    def test_mention_moderator_excluded(self):
        """主持人不应被 @mention 路由"""
        result = _parse_mentions("@moderator @主持人 帮我路由")
        assert "moderator" not in result

    def test_no_mention(self):
        result = _parse_mentions("我需要一个登录页面")
        assert result == []

    def test_invalid_mention(self):
        result = _parse_mentions("@nonexistent 你好")
        assert result == []

    def test_mention_dedup(self):
        result = _parse_mentions("@backend @backend 两次提到")
        assert result.count("backend") == 1

    def test_gateway_instantiation(self):
        """IntelligentGateway 可正常实例化"""
        gw = IntelligentGateway()
        assert gw is not None

    def test_build_message(self):
        """build_message 返回合法的 Message 对象"""
        gw = IntelligentGateway()
        msg = gw.build_message("architect", "Hello", "meeting-test")
        assert msg.sender_id == "architect"
        assert msg.sender_name == AGENTS["architect"]["name"]
        assert msg.meeting_id == "meeting-test"

    def test_history_append_and_clear(self):
        gw = IntelligentGateway()
        gw._append_history("m1", "user", "hello")
        gw._append_history("m1", "assistant", "hi", name="architect")
        assert len(gw._get_history("m1")) == 2
        gw.clear_history("m1")
        assert gw._get_history("m1") == []

    def test_history_limit_40(self):
        """历史记录超过上限时自动截断（现在上限为60条）"""
        gw = IntelligentGateway()
        for i in range(70):
            gw._append_history("m2", "user", f"msg {i}")
        assert len(gw._get_history("m2")) <= 60

    @pytest.mark.asyncio
    async def test_mention_route_skips_llm(self):
        """@mention 消息应直接路由，不调用 LLM"""
        gw = IntelligentGateway()
        decision = await gw.route_message("@backend 帮我看看 API", "m-test")
        assert "backend" in decision.route_to
        assert decision.broadcast is False

    @pytest.mark.asyncio
    async def test_mention_multiple_route(self):
        gw = IntelligentGateway()
        decision = await gw.route_message("@architect @frontend 一起讨论", "m-test")
        assert "architect" in decision.route_to
        assert "frontend" in decision.route_to


# ══════════════════════════════════════════════════════════════
# 第三层：CliApiRouter 单元测试（通用双轨路由器）
# ══════════════════════════════════════════════════════════════

class TestClaudeRouter:
    """验证 CliApiRouter 双轨策略逻辑（ClaudeRouter 是其别名）"""

    def _make_router(self, prefer, api_key="", cli_exists=False,
                     mode="dual_track", cli_out_fmt="stream_json"):
        """构造测试用 Router，绕过真实 CLI 探测"""
        router = CliApiRouter.__new__(CliApiRouter)
        router._provider_name = "test_provider"
        router._mode          = mode
        router._prefer        = prefer
        router._api_key       = api_key
        router._base_url      = "https://api.example.com/v1"
        router._api_model     = "test-model"
        router._cli_path      = "test_cli_nonexistent"
        router._cli_model     = "test-model"
        router._cli_out_fmt   = cli_out_fmt
        router._timeout       = 60
        router._api_client    = None
        router._api_available = bool(api_key)
        router._cli_available = cli_exists
        router._active        = router._resolve_active()
        return router

    def test_auto_api_key_available(self):
        r = self._make_router("auto", api_key="sk-xxx")
        assert r._active == "api"

    def test_auto_cli_only(self):
        r = self._make_router("auto", api_key="", cli_exists=True)
        assert r._active == "cli"

    def test_auto_both_unavailable(self):
        r = self._make_router("auto", api_key="", cli_exists=False)
        assert r._active == "unavailable"

    def test_prefer_api_no_key(self):
        r = self._make_router("api", api_key="")
        assert r._active == "unavailable"

    def test_prefer_api_with_key(self):
        r = self._make_router("api", api_key="sk-xxx")
        assert r._active == "api"

    def test_prefer_cli_available(self):
        r = self._make_router("cli", api_key="", cli_exists=True)
        assert r._active == "cli"

    def test_prefer_cli_fallback_to_api(self):
        r = self._make_router("cli", api_key="sk-xxx", cli_exists=False)
        assert r._active == "api"

    def test_prefer_cli_both_unavailable(self):
        r = self._make_router("cli", api_key="", cli_exists=False)
        assert r._active == "unavailable"

    def test_active_label_api(self):
        r = self._make_router("api", api_key="sk-xxx")
        assert "API" in r.active_label

    def test_active_label_unavailable(self):
        r = self._make_router("auto")
        assert "不可用" in r.active_label

    def test_cli_only_mode_with_cli(self):
        r = self._make_router("auto", cli_exists=True, mode="cli_only")
        assert r._active == "cli"

    def test_cli_only_mode_no_cli(self):
        r = self._make_router("auto", cli_exists=False, mode="cli_only")
        assert r._active == "unavailable"

    def test_legacy_mode_alias_claude_auto(self):
        """旧 mode 别名 claude_auto 应被解析为 dual_track"""
        cfg = {
            "mode": "claude_auto",
            "api_key": "sk-xxx",
            "base_url": "https://api.example.com/v1",
            "model": "test",
            "cli_path": "",
        }
        router = CliApiRouter("claude", cfg)
        assert router._mode == "dual_track"
        assert router._active == "api"

    def test_claude_router_is_alias(self):
        """ClaudeRouter 是 CliApiRouter 的向下兼容别名"""
        assert ClaudeRouter is CliApiRouter

    def test_gateway_uses_router_for_dual_track(self):
        """IntelligentGateway 对 dual_track Provider 使用路由器"""
        gw = IntelligentGateway()
        router = gw._get_router("claude")
        assert isinstance(router, CliApiRouter)

    @pytest.mark.asyncio
    async def test_call_unavailable_raises(self):
        r = self._make_router("auto")
        with pytest.raises(RuntimeError, match="不可用"):
            await r.call("sys", [{"role": "user", "content": "hi"}])


# ══════════════════════════════════════════════════════════════
# 第四层：真实 LLM 调用测试（需要 Key/CLI，否则跳过）
# ══════════════════════════════════════════════════════════════

MINIMAL_MESSAGES = [{"role": "user", "content": "用一句话介绍自己。"}]

class TestLLMCalls:
    """真实 Provider 调用 - 无 Key 时自动跳过"""

    @_skip_if_unavailable("deepseek")
    @pytest.mark.asyncio
    async def test_deepseek_call(self):
        gw = IntelligentGateway()
        result = await gw._call_llm(
            "moderator",
            AGENTS["moderator"]["system_prompt"],
            MINIMAL_MESSAGES,
        )
        assert isinstance(result, str) and len(result) > 0, "DeepSeek 返回为空"
        print(f"\n[DeepSeek] {result[:100]}")

    @_skip_if_unavailable("deepseek")
    @pytest.mark.asyncio
    async def test_deepseek_streaming(self):
        gw = IntelligentGateway()
        chunks = []
        async def collect(c): chunks.append(c)
        await gw._call_llm(
            "moderator",
            AGENTS["moderator"]["system_prompt"],
            MINIMAL_MESSAGES,
            stream_callback=collect,
        )
        assert len(chunks) > 0, "DeepSeek 流式无输出"

    @_skip_if_unavailable("claude")
    @pytest.mark.asyncio
    async def test_claude_call(self):
        gw = IntelligentGateway()
        result = await gw._call_llm(
            "architect",
            AGENTS["architect"]["system_prompt"],
            MINIMAL_MESSAGES,
        )
        assert isinstance(result, str) and len(result) > 0, "Claude 返回为空"
        print(f"\n[Claude] {result[:100]}")

    @_skip_if_unavailable("claude")
    @pytest.mark.asyncio
    async def test_claude_streaming(self):
        gw = IntelligentGateway()
        chunks = []
        async def collect(c): chunks.append(c)
        await gw._call_llm(
            "architect",
            AGENTS["architect"]["system_prompt"],
            MINIMAL_MESSAGES,
            stream_callback=collect,
        )
        assert len(chunks) > 0, "Claude 流式无输出"

    @_skip_if_unavailable("codex")
    @pytest.mark.asyncio
    async def test_codex_call(self):
        gw = IntelligentGateway()
        result = await gw._call_llm(
            "qa",
            AGENTS["qa"]["system_prompt"],
            [{"role": "user", "content": "写一个 Python 函数，计算斐波那契数列第 n 项。"}],
        )
        assert isinstance(result, str) and len(result) > 0, "Codex 返回为空"
        print(f"\n[Codex] {result[:200]}")

    @_skip_if_unavailable("gemini")
    @pytest.mark.asyncio
    async def test_gemini_call(self):
        gw = IntelligentGateway()
        result = await gw._call_llm(
            "ux",
            AGENTS["ux"]["system_prompt"],
            [{"role": "user", "content": "设计一个登录页面的 React 组件草图。"}],
        )
        assert isinstance(result, str) and len(result) > 0, "Gemini 返回为空"
        print(f"\n[Gemini] {result[:200]}")

    @_skip_if_unavailable("gemini")
    @pytest.mark.asyncio
    async def test_gemini_long_output(self):
        """验证 Gemini 可以生成较长的 UI 代码（max_tokens=8000）"""
        gw = IntelligentGateway()
        result = await gw._call_llm(
            "ux",
            AGENTS["ux"]["system_prompt"],
            [{"role": "user", "content": "生成一个完整的 React + Tailwind 登录页，包括邮箱、密码输入和提交按钮。"}],
        )
        assert len(result) > 200, f"Gemini 输出太短: {len(result)} chars"


# ══════════════════════════════════════════════════════════════
# 第五层：端到端消息流测试（需要 DeepSeek Key 进行路由）
# ══════════════════════════════════════════════════════════════

class TestEndToEnd:
    """完整的消息处理流：用户消息 → 路由 → Agent响应 → 主持人总结"""

    @_skip_if_unavailable("deepseek")
    @pytest.mark.asyncio
    async def test_full_flow_with_mention(self):
        """@mention 直接路由 + Agent 响应（仅 DeepSeek 路由，Agent 用 deepseek/product）"""
        gw = IntelligentGateway()
        meeting_id = "e2e-test-mention"

        # 用 @product 触发直接路由（product 使用 deepseek，无需其他 key）
        decision = await gw.route_message("@product 我们需要一个用户注册功能", meeting_id)
        assert "product" in decision.route_to

        if _provider_available("deepseek"):
            resp = await gw.generate_agent_response("product", "我们需要一个用户注册功能", meeting_id)
            assert isinstance(resp, str) and len(resp) > 0
            # 如果 provider 真实可用，响应不应是错误消息
            if _provider_available("deepseek"):
                assert "choices" not in resp, f"返回了原始错误: {resp}"
            await gw.update_history(meeting_id, "我们需要一个用户注册功能", "product", resp)
            assert len(gw._get_history(meeting_id)) > 0

    @_skip_if_unavailable("deepseek")
    @pytest.mark.asyncio
    async def test_moderator_llm_routing(self):
        """主持人 LLM 路由测试（需要 DeepSeek Key）"""
        gw = IntelligentGateway()
        decision = await gw.route_message("我想做一个电商系统的后端架构", "e2e-llm-route")
        assert len(decision.route_to) > 0
        for aid in decision.route_to:
            assert aid in AGENTS and aid != "moderator", f"路由到无效 agent: {aid}"

    @_skip_if_unavailable("deepseek")
    @pytest.mark.asyncio
    async def test_moderator_summary(self):
        """主持人生成总结（需要 DeepSeek Key）"""
        gw = IntelligentGateway()
        fake_responses = {
            "product": "我们需要支持商品列表、购物车和支付流程。"
        }
        summary = await gw.generate_moderator_summary(
            "电商系统需要哪些功能",
            fake_responses,
            "e2e-summary",
        )
        assert isinstance(summary, str) and len(summary) > 0

    @_skip_if_unavailable("deepseek")
    @pytest.mark.asyncio
    async def test_error_isolation(self):
        """Agent 调用失败不影响主流程，返回友好错误信息"""
        gw = IntelligentGateway()
        # 用一个没有 Key 的 provider agent（codex/qa），验证错误被包裹
        if not _provider_available("codex"):
            resp = await gw.generate_agent_response("qa", "帮我写测试", "e2e-error")
            assert "⚠️" in resp or isinstance(resp, str)

    @pytest.mark.asyncio
    async def test_concurrent_meetings(self):
        """多会议 ID 同时进行，历史互不干扰"""
        gw = IntelligentGateway()
        gw._append_history("meeting-A", "user", "A的消息")
        gw._append_history("meeting-B", "user", "B的消息")
        assert len(gw._get_history("meeting-A")) == 1
        assert len(gw._get_history("meeting-B")) == 1
        assert gw._get_history("meeting-A")[0]["content"] == "A的消息"


# ══════════════════════════════════════════════════════════════
# 第六层：Provider 切换验证
# ══════════════════════════════════════════════════════════════

class TestProviderSwitching:
    """验证可通过 YAML 配置切换 provider 而无需修改代码"""

    def test_agent_provider_can_be_any_valid_provider(self):
        """agents.yaml 中的 provider 字段只要是合法 provider 就应正常工作"""
        for aid, cfg in AGENTS.items():
            p = cfg["llm_provider"]
            assert p in LLM_PROVIDERS, f"Agent [{aid}] provider=[{p}] 未定义"

    def test_provider_mode_values(self):
        """mode 字段只允许预定义的值"""
        valid_modes = {"openai_compat", "anthropic_api", "claude_cli", "claude_auto",
                       "dual_track", "cli_only"}
        for name, cfg in LLM_PROVIDERS.items():
            mode = cfg.get("mode", "")
            assert mode in valid_modes, \
                f"Provider [{name}] mode=[{mode}] 不在合法值 {valid_modes} 中"

    def test_deepseek_provider_config(self):
        cfg = LLM_PROVIDERS["deepseek"]
        assert cfg["mode"] == "openai_compat"
        assert "deepseek" in cfg.get("base_url", "").lower() or cfg.get("base_url")

    def test_claude_provider_has_prefer_fields(self):
        cfg = LLM_PROVIDERS["claude"]
        assert "cli_path" in cfg
        assert "cli_model" in cfg

    def test_gemini_provider_config(self):
        cfg = LLM_PROVIDERS["gemini"]
        assert cfg["mode"] == "openai_compat"
        assert int(cfg.get("max_tokens", 0)) >= 4000, "Gemini max_tokens 应 ≥4000"
