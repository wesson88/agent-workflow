"""
music_mastering_engineer/main.py — 母带工程师执行入口（音乐域 L3 终点节点）

输入（vault，来源：角色 frontmatter `inputs` 字段）：
  - 10-项目/music/{project}/指令/给母带工程师.md      ← 可能含 dormant 声明
  - 10-项目/music/{project}/创作 vision.md
  - 10-项目/music/{project}/Suno-prompt.md
  - 10-项目/music/{project}/混音评估.md

输出（vault，来源：角色 frontmatter `outputs` 字段）：
  - 10-项目/music/{project}/母带规格.md
  - 10-项目/music/{project}/母带-Suno-retry补丁.md

特性：dormant 状态识别 — 制作人扇出的「给母带工程师.md」可能因项目不发布而声明 dormant；
本角色 user_prompt 引导 LLM 识别上游 dormant 状态并按降级输出。

CLI：
  python .claude/skills/music_mastering_engineer/main.py --task "..." --project myproj
"""

from __future__ import annotations

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

ROLE = "母带工程师"

# dormant 识别：在「指令/给母带工程师.md」开头若干字符内匹配明示 dormant 关键词
_DORMANT_KEYWORDS = ("dormant", "本项目状态：dormant", "本项目不启动母带", "不启动")


def _detect_dormant(instruction_path: Path) -> bool:
    """读「指令/给母带工程师.md」开头判断是否明示 dormant。"""
    if not instruction_path.exists():
        return False
    try:
        head = instruction_path.read_text(encoding="utf-8")[:2000]
    except Exception:
        return False
    return any(kw in head for kw in _DORMANT_KEYWORDS)


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

    # 上游硬约束：指令/给母带工程师.md + 混音评估.md
    required_upstream = {"给母带工程师.md", "混音评估.md"}
    missing = [
        p.name for p in input_paths
        if p.name in required_upstream and not p.exists()
    ]
    if missing:
        print(
            f"[{ROLE}] 上游缺失：{missing}。请先跑制作人扇出 + 混音师。",
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

    # dormant 识别（制作人决策传递）
    instruction_path = next(
        (p for p in input_paths if p.name == "给母带工程师.md"), None,
    )
    is_dormant = _detect_dormant(instruction_path) if instruction_path else False
    if is_dormant:
        print(f"[{ROLE}] 上游指令明示 dormant，将走降级输出路径。", flush=True)

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

    if is_dormant:
        scenario_prompt = (
            "**重要：上游「指令/给母带工程师.md」明示本项目 dormant**。\n"
            "请按 dormant 状态降级输出：\n"
            "1. `母带规格.md` — 仅产 dormant 状态说明（确认 dormant 决策 + 未来 promote 触发条件）\n"
            "2. `母带-Suno-retry补丁.md` — 声明「dormant 项目无 Suno retry 需求」即可\n\n"
            "**严禁伪造完整母带规格内容**。dormant 不等于 dropped，文档保留以便未来 promote。"
        )
    else:
        scenario_prompt = (
            "请同时产出 **2 份** FILE 块：\n\n"
            "1. `母带规格.md` — 响度规范 / 动态范围 / 平台标准 / 后期处理建议\n"
            "   - **给方向不给数字**（角色基因 style 约束）：例「LUFS 区间偏 -14 适合流媒体」而非具体 dB\n"
            "   - 不破坏混音意图（混音评估 §X 提到的方向必须沿用）\n"
            "   - 不越界混音层决策（频段 / 立体声 / 动态控制）\n\n"
            "2. `母带-Suno-retry补丁.md` — 若 Suno take 整体响度 / 动态 / 平台合规偏离 vision，"
            "给出 Suno 第 2/3 take 的 Style 段调整建议：\n"
            "   - 例：`+ louder mastering, modern streaming loudness`\n"
            "   - 若 Suno take 1 母带层已达标，可声明「无需 retry patch」"
        )

    user_prompt = (
        f"项目名：`{project}`（写文件时把路径里的 `{{project}}` 占位符替换为本值）\n\n"
        f"{context}\n\n---\n"
        f"本轮母带诉求：{task or '（未提供，请基于上游混音评估 + 指令综合推导）'}\n\n"
        f"{scenario_prompt}\n\n"
        "两份产物各为独立 FILE 块，缺一不可。"
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

    set_role_status(ROLE, status="success", reset_counters=True)
    set_role_status(ROLE, status="idle")
    append_audit({
        "timestamp": utc_now(), "role": ROLE, "project": project,
        "task": task, "result": "success", "outputs": written,
        "is_dormant": is_dormant,
    })
    print(f"[{ROLE}] 完成（{'dormant 降级' if is_dormant else '正常'}），输出：{written}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
