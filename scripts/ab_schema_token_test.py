"""
ab_schema_token_test.py — schema 化 token 节省三路径实测

三路径对照（A0 / A1 / B）：
  A0 历史现状：无 rule_refs 注入（W3 P0c 实施前 / 本会话改造前）
  A1 当前实现：rule_refs 章节级注入（本会话补完 8 skill 实施）
  B  假想对照：全文件 schema 注入（每角色全量读 schema）

测 3 角色：音乐总监 / 作曲 / 编曲（schema 引用最多 + 链路代表）
对照维度：注入 chars / 净 token 差（粗估 ~2.5 chars/token 中英混合）

关键问题：
  - A0 → A1：增加多少（"花" token 换上下文）
  - A1 vs B：节省多少（章节注入 vs 全文件）
  - A0 vs B：完全没注入 vs 全文件，两个极端
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / ".claude" / "skills"))
sys.path.insert(0, str(REPO / ".claude"))

from common import build_system_prompt, load_rule_block  # noqa: E402
from engine.role_loader import load_role  # noqa: E402
from engine.config import VAULT_ROOT  # noqa: E402


SCHEMA_PATH = VAULT_ROOT / "00-系统" / "规则" / "music" / "产物schema.md"
PRIMITIVE_SCHEMA_PATH = VAULT_ROOT / "00-系统" / "规则" / "music" / "流派primitive-schema.md"

# token 粗估系数（中英混合）：~2.5 chars/token
CHARS_PER_TOKEN = 2.5


def estimate_tokens(chars: int) -> int:
    return int(chars / CHARS_PER_TOKEN)


def measure_role(role_name: str) -> dict:
    """返回单角色 A/B 两路径下 rule_block / 全文件块的字符数 + token 估算。"""
    role = load_role(role_name)
    static_prompt, dynamic_prompt = build_system_prompt(role_name)
    base_chars = len(static_prompt) + len(dynamic_prompt)

    # A 路径：rule_refs 章节级注入
    a_block, a_hint = load_rule_block(role.rule_refs)
    a_chars = len(a_block)

    # B 路径：全文件 schema 注入（产物schema + 必要时含 primitive schema）
    b_text_parts = []
    if SCHEMA_PATH.exists():
        b_text_parts.append(f"=== 产物schema.md ===\n{SCHEMA_PATH.read_text(encoding='utf-8')}")
    # primitive schema 仅当角色 rule_refs 引用了它时才计入 B（否则 B 也不会读它）
    refs_str = " ".join(role.rule_refs)
    if "流派primitive-schema" in refs_str and PRIMITIVE_SCHEMA_PATH.exists():
        b_text_parts.append(
            f"=== 流派primitive-schema.md ===\n"
            f"{PRIMITIVE_SCHEMA_PATH.read_text(encoding='utf-8')}"
        )
    b_block = "\n\n".join(b_text_parts)
    b_chars = len(b_block)

    return {
        "role": role_name,
        "rule_refs": list(role.rule_refs),
        "rule_refs_count": len(role.rule_refs),
        "base_system_chars": base_chars,
        # A0 = 无注入 / A1 = 章节注入 / B = 全文件
        "A0_chars": 0,
        "A1_chars": a_chars,
        "B_chars": b_chars,
        "A1_hint": a_hint,
        "A0_tokens": 0,
        "A1_tokens": estimate_tokens(a_chars),
        "B_tokens": estimate_tokens(b_chars),
        "A0_to_A1_add_chars": a_chars,
        "A1_to_B_save_chars": b_chars - a_chars,
        "A0_to_B_add_chars": b_chars,
        "A1_vs_B_save_ratio": (
            f"{(b_chars - a_chars) / b_chars * 100:.1f}%" if b_chars > 0 else "n/a"
        ),
    }


def main() -> int:
    roles = ["音乐总监", "作曲", "编曲"]
    results = [measure_role(r) for r in roles]

    print(f"{'='*92}")
    print(f"Schema 化 token 三路径实测（{len(roles)} 角色 / 音乐域）")
    print(f"{'='*92}\n")

    print(f"{'角色':<10} {'rule_refs':<10} {'base sys':<10} "
          f"{'A0 无注入':<12} {'A1 章节':<10} {'B 全文件':<10} "
          f"{'A0→A1 加':<10} {'A1→B 省':<10} {'A1 vs B':<8}")
    print("-" * 108)
    for r in results:
        print(
            f"{r['role']:<10} {r['rule_refs_count']:<10} "
            f"{r['base_system_chars']:<10} "
            f"{r['A0_chars']:<12} {r['A1_chars']:<10} {r['B_chars']:<10} "
            f"{r['A0_to_A1_add_chars']:<10} {r['A1_to_B_save_chars']:<10} "
            f"{r['A1_vs_B_save_ratio']:<8}"
        )

    # 链路汇总
    total_a0 = 0
    total_a1 = sum(r["A1_chars"] for r in results)
    total_b = sum(r["B_chars"] for r in results)
    a0_to_a1 = total_a1 - total_a0
    a1_to_b = total_b - total_a1
    print("-" * 108)
    print(
        f"{'链路合计':<10} {'':<10} {'':<10} "
        f"{total_a0:<12} {total_a1:<10} {total_b:<10} "
        f"{a0_to_a1:<10} {a1_to_b:<10} "
        f"{(a1_to_b / total_b * 100):.1f}%"
    )

    print(f"\n--- token 估算（{CHARS_PER_TOKEN} chars/token，中英混合）---")
    print(f"  A0 无注入  : {estimate_tokens(total_a0):>6} tokens")
    print(f"  A1 章节注入: {estimate_tokens(total_a1):>6} tokens  "
          f"(相比 A0 多花 +{estimate_tokens(a0_to_a1):>6} tokens 换 schema 上下文)")
    print(f"  B  全文件  : {estimate_tokens(total_b):>6} tokens  "
          f"(相比 A1 多花 +{estimate_tokens(a1_to_b):>6} tokens 但 schema 上下文更全)")

    print(f"\n--- 三路径价值密度对比（仅看上下文 信息密度 vs token 成本）---")
    a1_info_per_token = total_a1 / max(1, estimate_tokens(total_a1))
    b_info_per_token = total_b / max(1, estimate_tokens(total_b))
    print(f"  A1 章节注入：{total_a1} chars / {estimate_tokens(total_a1)} tokens = "
          f"{a1_info_per_token:.2f} 相关 chars/token")
    print(f"  B  全文件  ：{total_b} chars / {estimate_tokens(total_b)} tokens = "
          f"{b_info_per_token:.2f} 全文件 chars/token （含 {b_info_per_token - a1_info_per_token:.2f} 不相关）")

    print(f"\n--- 详细 rule_refs ---")
    for r in results:
        print(f"\n[{r['role']}] hint: {r['A1_hint']}")
        for ref in r["rule_refs"]:
            print(f"  • {ref}")

    print(f"\n--- 假设条件 ---")
    print(f"- token 估算系数：{CHARS_PER_TOKEN} chars/token（中英混合粗估）")
    print(f"- A0 路径：无 rule_refs 注入（W3 P0c 实施前 / 本会话改造前真实历史状态）")
    print(f"- A1 路径：rule_refs 章节级 expand（本会话补完 8 skill 实施后当前状态）")
    print(f"- B  路径：全文件 schema 注入（产物schema.md + 流派primitive-schema.md，假想对照）")
    print(f"- base sys：build_system_prompt 返回的 static+dynamic chars（不变量，三路径共享）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
