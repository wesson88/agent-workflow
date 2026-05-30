"""
music_composer/main.py — 作曲执行入口（音乐域 L2-B 终点产 Suno-prompt）

输入（vault，来源：角色 frontmatter `inputs` 字段）：
  - 10-项目/music/{project}/指令/给作曲.md
  - 10-项目/music/{project}/词作.md
  - 10-项目/music/{project}/创作 vision.md
  - 10-项目/music/{project}/inputs/创作简报.md

输出（vault，来源：角色 frontmatter `outputs` 字段）：
  - 10-项目/music/{project}/曲作.md
  - 10-项目/music/{project}/Suno-prompt.md  ← L2-B 终点产物

CLI：
  python .claude/skills/music_composer/main.py --task "..." --project myproj
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import (
    parse_args, resolve_project, build_system_prompt, read_input_files,
    write_output_atomic, parse_claude_output_to_files,
    call_claude, append_audit, utc_now, render_required_outputs,
    load_rule_block,
)
from engine import (
    set_role_status, role_is_blocked,
    resolve_path,
)
from engine.role_loader import load_role

ROLE = "作曲"

# Suno-prompt.md 中 Style 段约定为第一个 ``` ... ``` 三反引号代码块
_STYLE_BLOCK_RE = re.compile(r"```[^\n]*\n(.*?)\n```", re.DOTALL)


def _measure_suno_style_chars(output_files: dict[str, str]) -> int | None:
    """从 Suno-prompt.md 抽 Style 段（首个 ``` 代码块），返回 Python len()。

    Suno v4.5 Style 字段用 JavaScript String.length 计数（= Python len()），硬上限 1000。
    LLM 容易低估自己输出的字符数（W5 L2-B 实测：自报 1090，实际 1507；偏差 +38%）。
    工程层 post-write 实测落 audit，让下游/复盘能 grep 出超限事件。
    """
    for rel_path, content in output_files.items():
        if "Suno-prompt.md" not in rel_path:
            continue
        m = _STYLE_BLOCK_RE.search(content)
        if not m:
            return None
        return len(m.group(1))
    return None


def main() -> int:
    args = parse_args()
    task = (args.task or "").strip()
    project = resolve_project(args)

    if role_is_blocked(ROLE):
        print(f"[{ROLE}] status=blocked，跳过。", file=sys.stderr)
        return 1

    set_role_status(ROLE, status="busy", enforce_transition=False)

    role_def = load_role(ROLE)
    input_paths = [resolve_path(p, project) for p in role_def.inputs]
    output_rels = [p.replace("{project}", project) for p in role_def.outputs]

    # 上游硬约束：指令/给作曲.md + 词作.md 必须同时存在
    required_upstream = {"给作曲.md", "词作.md"}
    missing = [
        p.name for p in input_paths
        if p.name in required_upstream and not p.exists()
    ]
    if missing:
        print(
            f"[{ROLE}] 上游缺失：{missing}。请先跑制作人扇出 + 作词。",
            file=sys.stderr,
        )
        set_role_status(
            ROLE, status="failed",
            increment_consecutive_failures=True, increment_error=True,
            enforce_transition=False,
        )
        append_audit({
            "timestamp": utc_now(), "role": ROLE, "project": project,
            "task": task, "result": "failed", "error": f"missing_upstream:{missing}",
        })
        return 2

    existing_inputs = [p for p in input_paths if p.exists()]
    print(
        f"[{ROLE}] 上游 {len(existing_inputs)}/{len(input_paths)} 就位："
        f"{[p.name for p in existing_inputs]}",
        flush=True,
    )

    system_prompt = build_system_prompt(ROLE, project=project)
    context = read_input_files(input_paths)

    rule_block, source_hint = load_rule_block(role_def.rule_refs)
    print(f"[{ROLE}] rule_refs 注入：{source_hint}")
    if rule_block:
        context = context + "\n\n" + rule_block

    user_prompt = (
        f"项目名：`{project}`（写文件时把路径里的 `{{project}}` 占位符替换为本值）\n\n"
        f"{context}\n\n---\n"
        f"本轮作曲诉求：{task or '（未提供，请基于上游词作 + vision + 简报综合推导）'}\n\n"
        "作为作曲，请同时产出 **2 份** FILE 块：\n\n"
        "1. `曲作.md` — 旋律设计 / 和弦走向 / 段落结构 / 调性 / 流派 idiom 描述\n"
        "   - 给下游编曲 / 和声编写消费的乐理层文档\n"
        "   - 按词作的段落结构对齐（同 section 数）\n\n"
        "2. `Suno-prompt.md` — Suno 双层架构产物：\n"
        "   - **Style 段**（全局参数）：流派配比 / BPM / 调性 / 主唱音色 / no-list（排除的元素）\n"
        "   - **Lyrics 段**：嵌入 inline meta-tag（`[Intro - ...]` / `[Verse 1 - ...]` / "
        "`[Transition - ...]`）控制段间 arrangement evolution\n"
        "   - 关键工程契约：Style 不塞段间描述（Suno 会忽略）；段间演进必须走 Lyrics inline tag\n\n"
        "**Style 字符数硬约束（v4.5 实测）**：\n"
        "- Suno 用 JavaScript String.length（= Python len()）计数，Style 字段硬上限 1000 char\n"
        "- **本次目标：Style 段 ≤ 950 char**（留 50 char 余量给 user 微调）\n"
        "- LLM 容易低估自己输出的字符数。**落盘前在内部数一遍**：把 Style 块逐字符数完，超 950 必删减重写\n"
        "- 删减优先级：Production 段（混音细节）可整段并入 Instruments 简短形容词；"
        "Vocal + No-list 必须保留全量（音色筛选 + 雷区清单是工具层硬约束）；"
        "Fusion 第 1 句可去掉「Progressive fusion across the track — ...」长串，并入"
        "Lyrics inline tag 控制\n\n"
        "**重要**：两份产物各为独立 FILE 块，缺一不可。Suno-prompt.md 是 L2-B 终点产物，"
        "用户复制到 Suno Style 输入框 ≤ 1000 char 是硬性卡口，违反则用户无法使用。"
        f"{render_required_outputs(output_rels)}"
    )

    try:
        raw_output = call_claude(system_prompt, user_prompt, ROLE)
    except Exception as e:
        print(f"[{ROLE}] LLM 调用失败：{e}", file=sys.stderr)
        set_role_status(
            ROLE, status="failed",
            increment_consecutive_failures=True, increment_error=True,
            enforce_transition=False,
        )
        append_audit({
            "timestamp": utc_now(), "role": ROLE, "project": project,
            "task": task, "result": "failed", "error": str(e),
        })
        return 1

    output_files = parse_claude_output_to_files(raw_output)
    if not output_files:
        print(
            f"[{ROLE}] 未检测到 FILE 块。原始输出长度 {len(raw_output)}。",
            file=sys.stderr,
        )
        set_role_status(
            ROLE, status="failed",
            increment_consecutive_failures=True, increment_error=True,
            enforce_transition=False,
        )
        append_audit({
            "timestamp": utc_now(), "role": ROLE, "project": project,
            "task": task, "result": "failed", "error": "no_file_blocks",
        })
        return 1

    written = []
    for rel_path, content in output_files.items():
        rel_resolved = rel_path.replace("{project}", project)
        dest = resolve_path(rel_resolved, project)
        write_output_atomic(dest, content)
        print(f"[{ROLE}] 写入: {dest}")
        written.append(rel_resolved)

    # 软告警：核对 Suno-prompt.md 必产
    has_suno_prompt = any("Suno-prompt.md" in w for w in written)
    if not has_suno_prompt:
        print(
            f"[{ROLE}] ⚠️ 未产出 Suno-prompt.md（L2-B 终点产物缺失）。"
            f"实际 outputs: {written}",
            file=sys.stderr,
        )

    # Style 段字符数实测（LLM 自估不可靠，工程层兜底）
    style_char_count = _measure_suno_style_chars(output_files)
    style_oversized = style_char_count is not None and style_char_count > 1000
    if style_char_count is not None:
        marker = "⚠️ 超 1000" if style_oversized else "✅"
        print(f"[{ROLE}] Suno Style 段字符数（Python len()）: {style_char_count} {marker}")

    set_role_status(ROLE, status="success", reset_counters=True)
    set_role_status(ROLE, status="idle")
    append_audit({
        "timestamp": utc_now(), "role": ROLE, "project": project,
        "task": task, "result": "success", "outputs": written,
        "has_suno_prompt": has_suno_prompt,
        "style_char_count": style_char_count,
        "style_oversized": style_oversized,
    })
    print(f"[{ROLE}] 完成，输出：{written}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
