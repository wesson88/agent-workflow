"""
scripts/token_report.py — 从 .claude/audit.jsonl 汇总 LLM token 消耗。

用法：
    python scripts/token_report.py                    # 全量
    python scripts/token_report.py --since '2026-07-08T14:00:00Z'
    python scripts/token_report.py --last 100         # 最近 100 条 llm_call
    python scripts/token_report.py --project demo     # 只看 project=demo 时段内（配 --since）

汇总维度：按 role 分组，列出
- calls：调用次数
- input：input_tokens 累计
- output：output_tokens 累计
- cache_read / cache_create：缓存命中 / 创建
- cache_hit_rate：cache_read / (cache_read + input_tokens - cache_read - cache_create) —
  实际口径见 Anthropic 文档；此处近似为 cache_read / (input_tokens)
- elapsed：累计 wallclock 秒
- total：input + output（不扣 cache，纯付费口径需减 cache_read × 0.9）

依赖：type=token_audit + reason=llm_call 事件（由 engine.llm._call_anthropic_sdk 落）。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

AUDIT_PATH = Path(__file__).resolve().parent.parent / ".claude" / "audit.jsonl"


def _parse_ts(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def load_llm_events(
    *, since: str | None = None, tail: int | None = None,
) -> list[dict]:
    if not AUDIT_PATH.is_file():
        raise SystemExit(f"audit.jsonl 不存在：{AUDIT_PATH}")
    since_dt = _parse_ts(since) if since else None
    events: list[dict] = []
    with AUDIT_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") != "token_audit" or entry.get("reason") != "llm_call":
                continue
            if since_dt:
                ts = _parse_ts(entry.get("ts", ""))
                if ts and ts < since_dt:
                    continue
            events.append(entry)
    if tail:
        events = events[-tail:]
    return events


def summarize(events: list[dict]) -> tuple[dict[str, dict], dict]:
    per_role: dict[str, dict] = defaultdict(lambda: {
        "calls": 0, "input": 0, "output": 0,
        "cache_read": 0, "cache_create": 0, "elapsed_s": 0.0,
    })
    grand = dict(per_role["__grand_placeholder__"])
    del per_role["__grand_placeholder__"]

    for e in events:
        role = e.get("role", "(unknown)")
        row = per_role[role]
        row["calls"] += 1
        row["input"] += e.get("input_tokens", 0)
        row["output"] += e.get("output_tokens", 0)
        row["cache_read"] += e.get("cache_read_input_tokens", 0)
        row["cache_create"] += e.get("cache_creation_input_tokens", 0)
        row["elapsed_s"] += e.get("elapsed_s", 0.0)

    for row in per_role.values():
        for k, v in row.items():
            grand[k] = grand.get(k, 0) + v
    return dict(per_role), grand


def print_report(per_role: dict[str, dict], grand: dict, events: list[dict]) -> None:
    if not events:
        print("（未找到 llm_call 事件）")
        return
    ts_first = events[0].get("ts", "?")
    ts_last = events[-1].get("ts", "?")
    print(f"=== Token 汇总 ===  {ts_first} → {ts_last}   ({len(events)} calls)\n")

    header = f"{'role':<20}  {'calls':>5}  {'input':>8}  {'output':>7}  {'cache_R':>8}  {'cache_C':>8}  {'hit%':>5}  {'elapsed':>8}  {'total':>8}"
    print(header)
    print("-" * len(header))

    def _fmt(row: dict) -> str:
        total = row["input"] + row["output"]
        # cache_hit_rate：cache_read / (cache_read + non-cached input)
        # non-cached input = input_tokens - cache_read - cache_create
        # 但 Anthropic API 里 usage.input_tokens **不含** cache 段（另有字段）；
        # 所以简化：hit% = cache_read / (input + cache_read + cache_create)
        denom = row["input"] + row["cache_read"] + row["cache_create"]
        hit = 100 * row["cache_read"] / denom if denom else 0.0
        return (
            f"  {row['calls']:>5}  {row['input']:>8}  {row['output']:>7}  "
            f"{row['cache_read']:>8}  {row['cache_create']:>8}  {hit:>4.1f}%  "
            f"{row['elapsed_s']:>7.1f}s  {total:>8}"
        )

    for role in sorted(per_role, key=lambda r: -per_role[r]["input"] - per_role[r]["output"]):
        row = per_role[role]
        print(f"{role:<20}{_fmt(row)}")
    print("-" * len(header))
    print(f"{'GRAND':<20}{_fmt(grand)}")
    print()
    # 提示付费口径
    saved = grand["cache_read"]
    if saved > 0:
        print(
            f"缓存节省估算：cache_read={saved} tokens × ~90% = "
            f"~{int(saved * 0.9)} tokens 相当于免费（Anthropic 5-min TTL 命中价 0.1×）"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="LLM token 汇总（read audit.jsonl）")
    parser.add_argument("--since", help="ISO 8601 时间戳（如 2026-07-08T14:00:00Z）")
    parser.add_argument("--last", type=int, help="只看最后 N 条 llm_call")
    args = parser.parse_args()

    events = load_llm_events(since=args.since, tail=args.last)
    per_role, grand = summarize(events)
    print_report(per_role, grand, events)
    return 0


if __name__ == "__main__":
    sys.exit(main())
