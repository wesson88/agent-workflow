"""
ab_se_token_test.py — SE 域 schema/skill 注入 token 三路径实测

对照路径（SE 域版，参 scripts/ab_schema_token_test.py 音乐域版本）：
  A0 历史 / 极端：无 rule_refs 也无 skill 注入（只 base system prompt）
  A1 当前实现：rule_refs 章节级注入（指向 F-*#6 工程参考 skill 段）
  B  上界对照：角色 skill 目录全文件注入（`20-知识/角色技能/{域}/{角色}/*.md` 全读）

注：B 臂原按 frontmatter `skill_refs` 声明取文件。该字段 2026-08-25 废弃后改为
扫目录 —— 顺带修掉一个测量偏差：声明是**欠声明**的（UI设计师声明 2 张而目录
17 张），按声明测出来的"全量上界"其实不是上界。

测 4 角色：架构师 / 技术主管 / 后端工程师 / 前端工程师（短期路线图 §3.3 指定）
对照维度：注入 chars / 净 token 差（粗估 ~2.5 chars/token 中英混合）

关键问题：
  - A0 → A1：rule_refs 章节注入花了多少 token 换上下文
  - A1 vs B：章节注入 vs 全 skill 文件，差距多大
  - 是否达到路线图判定线（节省 < 15% / 15-30% / > 30%）

骨架版（W3.3 P0）：先跑通三路径 chars 测量与汇总；后续可加：
  - 各角色 input 文件（PRD / 系统设计）的 sections vs 全文对比
  - skill_block per-skill token breakdown
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


# token 粗估系数（中英混合）：~2.5 chars/token
CHARS_PER_TOKEN = 2.5

# 路线图 §3.3 判定线（节省比例）
SAVE_THRESHOLDS = {
    "low": 0.15,
    "high": 0.30,
}


def estimate_tokens(chars: int) -> int:
    return int(chars / CHARS_PER_TOKEN)


def _role_skill_files(domain: str, role_name: str) -> list[Path]:
    """角色外迁 skill 目录下的 *.md（B 路径用）。目录不存在 → 空列表。"""
    d = VAULT_ROOT / "20-知识" / "角色技能" / domain / role_name
    if not d.is_dir():
        return []
    return [
        p for p in sorted(d.glob("*.md"))
        if p.is_file() and not p.name.startswith((".", "_"))
    ]


def _read_skill_file(path: Path) -> tuple[int, str]:
    """读 skill 文件全文。返回 (chars, source_note)。"""
    rel = path.relative_to(VAULT_ROOT).as_posix()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return 0, f"err:{rel}({e})"
    return len(text), f"ok:{rel}"


def measure_role(role_name: str) -> dict:
    """返回单角色三路径下 rule_block / skill 文件全量块的字符数 + token 估算。"""
    role = load_role(role_name)
    static_prompt, dynamic_prompt = build_system_prompt(role_name)
    base_chars = len(static_prompt) + len(dynamic_prompt)

    # A1 路径：rule_refs 章节级注入
    a1_block, a1_hint = load_rule_block(role.rule_refs)
    a1_chars = len(a1_block)

    # B 路径：角色 skill 目录全文件注入
    skill_files = _role_skill_files(role.domain, role.name)
    b_total = 0
    b_notes: list[str] = []
    for p in skill_files:
        chars, note = _read_skill_file(p)
        b_total += chars
        b_notes.append(note)

    save_ratio = (b_total - a1_chars) / b_total if b_total > 0 else 0.0

    return {
        "role": role_name,
        "rule_refs": list(role.rule_refs),
        "rule_refs_count": len(role.rule_refs),
        "skill_files": [p.relative_to(VAULT_ROOT).as_posix() for p in skill_files],
        "skill_files_count": len(skill_files),
        "base_system_chars": base_chars,
        "A0_chars": 0,
        "A1_chars": a1_chars,
        "B_chars": b_total,
        "A1_hint": a1_hint,
        "B_notes": b_notes,
        "A0_tokens": 0,
        "A1_tokens": estimate_tokens(a1_chars),
        "B_tokens": estimate_tokens(b_total),
        "A0_to_A1_add_chars": a1_chars,
        "A1_to_B_save_chars": b_total - a1_chars,
        "A1_vs_B_save_ratio": save_ratio,
        "A1_vs_B_save_ratio_str": f"{save_ratio * 100:.1f}%" if b_total > 0 else "n/a",
    }


def _verdict(ratio: float) -> str:
    """按路线图 §3.3 判定标准给结论。"""
    if ratio < SAVE_THRESHOLDS["low"]:
        return "低收益（< 15%）— 仅保留索引 + wikilink 约束即可"
    if ratio < SAVE_THRESHOLDS["high"]:
        return "中收益（15-30%）— 继续推进章节级输入和任务级 skill 裁剪"
    return "高收益（> 30%）— 可考虑系统化 schema 化 SE 产物契约"


def main() -> int:
    roles = ["架构师", "技术主管", "后端工程师", "前端工程师"]
    results: list[dict] = []
    for r in roles:
        try:
            results.append(measure_role(r))
        except Exception as e:
            print(f"⚠️ 角色 [{r}] 测量失败：{e}", file=sys.stderr)

    if not results:
        print("没有成功测量的角色，退出。", file=sys.stderr)
        return 1

    print(f"{'='*108}")
    print(f"SE 域 schema/skill 注入 token 三路径实测（{len(results)} 角色）")
    print(f"{'='*108}\n")

    header = (
        f"{'角色':<10} {'rule#':<6} {'skill#':<7} {'base sys':<10} "
        f"{'A0 无注入':<10} {'A1 章节':<10} {'B 全文件':<10} "
        f"{'A0→A1 加':<10} {'A1→B 省':<10} {'A1 vs B':<8}"
    )
    print(header)
    print("-" * len(header))
    for r in results:
        print(
            f"{r['role']:<10} {r['rule_refs_count']:<6} {r['skill_files_count']:<7} "
            f"{r['base_system_chars']:<10} "
            f"{r['A0_chars']:<10} {r['A1_chars']:<10} {r['B_chars']:<10} "
            f"{r['A0_to_A1_add_chars']:<10} {r['A1_to_B_save_chars']:<10} "
            f"{r['A1_vs_B_save_ratio_str']:<8}"
        )

    # 链路汇总
    total_a0 = 0
    total_a1 = sum(r["A1_chars"] for r in results)
    total_b = sum(r["B_chars"] for r in results)
    a0_to_a1 = total_a1 - total_a0
    a1_to_b = total_b - total_a1
    save_ratio = (a1_to_b / total_b) if total_b > 0 else 0.0
    print("-" * len(header))
    print(
        f"{'链路合计':<10} {'':<6} {'':<7} {'':<10} "
        f"{total_a0:<10} {total_a1:<10} {total_b:<10} "
        f"{a0_to_a1:<10} {a1_to_b:<10} "
        f"{save_ratio * 100:.1f}%"
    )

    print(f"\n--- token 估算（{CHARS_PER_TOKEN} chars/token，中英混合）---")
    print(f"  A0 无注入  : {estimate_tokens(total_a0):>6} tokens")
    print(f"  A1 章节注入: {estimate_tokens(total_a1):>6} tokens  "
          f"(相比 A0 多花 +{estimate_tokens(a0_to_a1):>6} tokens 换 schema 上下文)")
    print(f"  B  全文件  : {estimate_tokens(total_b):>6} tokens  "
          f"(相比 A1 多花 +{estimate_tokens(a1_to_b):>6} tokens 但 skill 上下文全量)")

    print(f"\n--- 判定（路线图 §3.3）---")
    print(f"  A1 vs B 节省：{save_ratio * 100:.1f}%")
    print(f"  结论：{_verdict(save_ratio)}")

    print(f"\n--- 角色详细 rule_refs / 外迁 skill ---")
    for r in results:
        print(f"\n[{r['role']}]")
        print(f"  A1 hint：{r['A1_hint']}")
        if r["rule_refs"]:
            print(f"  rule_refs（{r['rule_refs_count']}）：")
            for ref in r["rule_refs"]:
                print(f"    • {ref}")
        else:
            print(f"  rule_refs：（空）")
        if r["skill_files"]:
            print(f"  外迁 skill（{r['skill_files_count']}）：")
            for rel, note in zip(r["skill_files"], r["B_notes"]):
                tag = "✗" if note.startswith("err:") else "✓"
                print(f"    {tag} {rel}")
        else:
            print(f"  外迁 skill：（目录空或不存在）")

    print(f"\n--- 假设条件 ---")
    print(f"- token 估算系数：{CHARS_PER_TOKEN} chars/token（中英混合粗估）")
    print(f"- A0：base system prompt only，无 rule_refs 章节注入也无 skill 全文加载")
    print(f"- A1：rule_refs 章节级 expand（当前实现）；指向 F-*#6 工程参考 skill 段")
    print(f"- B ：角色 skill 目录全文件注入（上界对照，每个 skill .md 全文 concat）")
    print(f"- base sys：build_system_prompt 返回的 static+dynamic chars（不变量，三路径共享）")
    print(f"- skill 目录：{VAULT_ROOT / '20-知识' / '角色技能'}/{{域}}/{{角色}}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
