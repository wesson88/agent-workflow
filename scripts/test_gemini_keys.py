"""
test_gemini_keys.py — 验证 .env 里两个 Gemini key 是否都通

读 .claude/engine/llm_providers.yaml 拿 Gemini 模型清单，按 key_env 字段
从 .env 取 key，对每个模型用 OpenAI 兼容端点打一次最小 ping。

通过 = HTTP 200 + 拿到非空文本
失败 = 抛任何异常（network / 401 / 404 / quota 等），打印短诊断

退出码：全过 0；任一失败 1
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import yaml
from dotenv import load_dotenv

REPO = Path(__file__).resolve().parent.parent
PROVIDERS = REPO / ".claude" / "engine" / "llm_providers.yaml"
ENV_FILE = REPO / ".env"


def load_gemini_models() -> list[tuple[str, dict]]:
    """从 yaml 里提取所有 gemini-* 且有 api 段的条目。"""
    data = yaml.safe_load(PROVIDERS.read_text(encoding="utf-8"))
    out = []
    for name, cfg in data.items():
        if name.startswith("gemini") and "api" in cfg:
            out.append((name, cfg["api"]))
    return out


def ping(api_cfg: dict, key: str) -> tuple[bool, str, float]:
    """返回 (ok, detail, latency_ms)。"""
    from openai import OpenAI

    client = OpenAI(api_key=key, base_url=api_cfg["base_url"])
    t0 = time.perf_counter()
    try:
        # max_tokens 不能太小：Gemini 2.5/3.x 系列会先吐 reasoning token，
        # 实测 pro 单 "OK" 也要 ~140 total token。设 300 留余量。
        resp = client.chat.completions.create(
            model=api_cfg["model"],
            messages=[{"role": "user", "content": "Reply with just: OK"}],
            max_tokens=300,
        )
        elapsed = (time.perf_counter() - t0) * 1000
        choice = resp.choices[0]
        text = (choice.message.content or "").strip()
        if not text:
            return False, f"空响应 (finish={choice.finish_reason}, usage={resp.usage})", elapsed
        return True, text, elapsed
    except Exception as e:  # noqa: BLE001 — 测试脚本，任何错都算失败
        elapsed = (time.perf_counter() - t0) * 1000
        # 抽 HTTP status / message 关键信息，避免堆栈过长
        msg = str(e)
        if len(msg) > 200:
            msg = msg[:200] + "..."
        return False, f"{type(e).__name__}: {msg}", elapsed


def main() -> int:
    load_dotenv(ENV_FILE)
    models = load_gemini_models()
    if not models:
        print("[error] llm_providers.yaml 中未找到任何 gemini-* 条目", file=sys.stderr)
        return 1

    # 先汇总用到哪些 key_env，提前提示缺失
    needed_keys = {cfg["key_env"] for _, cfg in models}
    missing = [k for k in needed_keys if not os.environ.get(k)]
    if missing:
        print(f"[error] .env 缺以下 key：{', '.join(missing)}", file=sys.stderr)
        return 1

    print(f"测试 {len(models)} 个 Gemini 模型，端点 = {models[0][1]['base_url']}\n")
    print(f"{'模型':<28} {'key_env':<24} {'状态':<6} {'延迟':>8}  响应/错误")
    print("-" * 100)

    all_ok = True
    for name, api_cfg in models:
        key = os.environ[api_cfg["key_env"]]
        ok, detail, ms = ping(api_cfg, key)
        status = "PASS" if ok else "FAIL"
        if not ok:
            all_ok = False
        # 响应/错误截 60 字符
        snippet = detail.replace("\n", " ")
        if len(snippet) > 60:
            snippet = snippet[:60] + "..."
        print(f"{name:<28} {api_cfg['key_env']:<24} {status:<6} {ms:>6.0f}ms  {snippet}")

    print()
    if all_ok:
        print("[OK] 所有 key 都通")
        return 0
    print("[FAIL] 有失败，见上表", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
