"""验证多 Provider 配置脚本"""
import shutil
from config import AGENTS, LLM_PROVIDERS

DUAL_TRACK_MODES = {"dual_track", "cli_only", "claude_auto", "claude_cli"}

print("=== 所有 Provider 状态 ===")
for name, cfg in LLM_PROVIDERS.items():
    mode = cfg.get("mode", "openai_compat")
    if mode in DUAL_TRACK_MODES:
        api_ok  = bool(cfg.get("api_key"))
        cli_ok  = bool(shutil.which(cfg.get("cli_path", ""))) if cfg.get("cli_path") else False
        prefer  = cfg.get("prefer", "auto")
        fmt     = cfg.get("cli_output_format", "plain")
        if prefer == "api":
            active = "API ✓" if api_ok else "❌ API Key 未填写"
        elif prefer == "cli":
            active = "CLI ✓" if cli_ok else ("API降级 ✓" if api_ok else "❌ 两条路径均不可用")
        else:
            active = f"API ✓" if api_ok else (f"CLI ✓" if cli_ok else "❌ 两条路径均不可用")
        print(
            f"  [{name}]  mode={mode}  prefer={prefer}  cli_fmt={fmt}\n"
            f"    api={'✓' if api_ok else '✗'}  cli={'✓' if cli_ok else '✗'}  → {active}"
        )
    else:
        key_ok = bool(cfg.get("api_key"))
        model  = cfg.get("model", "?")
        status = "✓ Key已配置" if key_ok else "❌ Key未填写"
        print(f"  [{name}]  mode={mode}  model={model}  {status}")

print()
print("=== Agent -> Provider 映射 ===")
for v in AGENTS.values():
    provider = v["llm_provider"]
    mode = LLM_PROVIDERS.get(provider, {}).get("mode", "?")
    print(f"  {v['id']:<12} {v['avatar']}  {v['name']:<10} -> {provider} ({mode})")

print()
print("=== 双轨 Provider 激活路径 ===")
from gateway import IntelligentGateway
gw = IntelligentGateway()
for name, cfg in LLM_PROVIDERS.items():
    if cfg.get("mode", "") in DUAL_TRACK_MODES:
        router = gw._get_router(name)
        print(f"  [{name}] 实际使用: {router.active_label}")
