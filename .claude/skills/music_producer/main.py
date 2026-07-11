"""
music_producer/main.py — 制作人执行入口（音乐域 L2-A 起步）

输入（vault，来源：角色 frontmatter `inputs` 字段）：
  - 10-项目/music/{project}/inputs/创作简报.md
  - 10-项目/music/{project}/创作 vision.md
  - 10-项目/music/{project}/指令/给制作人.md

输出（vault，来源：角色 frontmatter `outputs` 字段）：
  - 10-项目/music/{project}/制作计划.md
  - 10-项目/music/{project}/指令/给{角色}.md  ← 扇出，按 downstream 每角色一份

CLI：
  python .claude/skills/music_producer/main.py --task "..." --project myproj
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import (
    parse_args, resolve_project, build_system_prompt, read_input_files,
    write_output_atomic, parse_claude_output_to_files,
    call_claude, append_audit, utc_now, render_required_outputs,
    load_rule_block, load_genre_skill_block,
)
from engine import (
    set_role_status, role_is_blocked,
    resolve_path,
)
from engine.role_loader import load_role

ROLE = "制作人"


def _parse_dormant_roles(upstream_text: str, candidates: list[str]) -> set[str]:
    """从上游文本（vision / 指令-给制作人 / 简报 的拼接）里识别被总监判 dormant 的下游角色。

    格式约定：某个章节的 probational / dormant 决策表里 `| <角色> | ... | **dormant** | ...`。
    宽松匹配：任一 candidate 名字出现在含 `**dormant**` 的行即视为 dormant，避免格式漂移。
    总监可能把 dormant 表放 vision 或 指令 任一位置（湖向放 vision；陪伴我们的人放 指令），
    所以本函数扫拼接文本而非单文件。

    "active（轻介入）" 一类表达算 active，不会误伤 —— 只匹配含 `**dormant**` 精确粗体的行。

    跨域说明：dormant 语义目前仅音乐域实现（改法 A · 隔离在 music_producer 里）。
    后续其他域若需类似能力，需**在建域时同步接入**这条链路（producer 级角色本地解析 filter）。
    参见 `20-知识/项目记录/音乐L3实战-非SE机制差异-2026-07-11.md` 附录。
    """
    dormant: set[str] = set()
    for line in upstream_text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.split("|")]
        # 标准 markdown 表格 split 后首尾是空串：['', 角色, 决策, 依据, ...]
        if len(cells) < 4:
            continue
        # 决策通常在第 2-3 列（0-indexed after empty start），扫这个窄窗口找 "dormant"
        # 明文字符串（不依赖粗体，因 director LLM 每次可能加粗 role 或 decision）。
        # 依据列不扫，避免"归编曲"这类 role 名被误判。
        decision_window = " ".join(cells[2:4]).lower()
        if "dormant" not in decision_window:
            continue
        # 角色列可能被加粗为 `**<角色>**`，剥 `**` 后再等值比对
        role_clean = cells[1].replace("**", "").strip()
        for name in candidates:
            if name == role_clean:
                dormant.add(name)
    return dormant


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
    downstream = list(role_def.downstream)

    # 验上游产物：vision.md 必须存在（音乐总监跑过）
    vision_path = next(
        (p for p in input_paths if p.name == "创作 vision.md"),
        None,
    )
    if vision_path is None or not vision_path.exists():
        print(
            f"[{ROLE}] 上游缺失：未找到 `创作 vision.md`。请先跑音乐总监。",
            file=sys.stderr,
        )
        set_role_status(
            ROLE, status="failed",
            increment_consecutive_failures=True, increment_error=True,
            enforce_transition=False,
        )
        append_audit({
            "timestamp": utc_now(), "role": ROLE, "project": project,
            "task": task, "result": "failed", "error": "missing_vision",
        })
        return 2

    # 依上游 dormant 声明本地过滤 downstream，避免为不参与本项目的角色扇出无效指令。
    # 扫全部 producer 输入（vision + 指令/给制作人 + 简报），因为总监可能把 dormant 表
    # 放在 vision.md 或 指令/给制作人.md 任一位置（湖向放 vision；陪伴我们的人放 指令）。
    upstream_text = ""
    for p in input_paths:
        if p.exists():
            upstream_text += p.read_text(encoding="utf-8") + "\n"
    dormant_roles = _parse_dormant_roles(upstream_text, downstream)
    active_downstream = [r for r in downstream if r not in dormant_roles]
    if dormant_roles:
        print(
            f"[{ROLE}] vision 判 dormant：{sorted(dormant_roles)}；"
            f"active downstream={active_downstream}",
            flush=True,
        )
    else:
        print(
            f"[{ROLE}] 上游 vision 已就位；downstream={active_downstream}",
            flush=True,
        )

    # 扇出输出清单：制作计划 + 每个 active 下游一份指令（dormant 不扇出）
    output_rels = [
        f"10-项目/music/{project}/制作计划.md",
    ] + [
        f"10-项目/music/{project}/指令/给{role}.md" for role in active_downstream
    ]

    system_prompt = build_system_prompt(ROLE, project=project)
    context = read_input_files(input_paths)

    rule_block, source_hint = load_rule_block(role_def.rule_refs)
    print(f"[{ROLE}] rule_refs 注入：{source_hint}")
    if rule_block:
        context = context + "\n\n" + rule_block

    skill_block, skill_hint = load_genre_skill_block(ROLE, task, context)
    print(f"[{ROLE}] skill_trigger：{skill_hint}")
    if skill_block:
        context = context + "\n\n" + skill_block

    fanout_list = "\n".join(f"  - 给 {r}" for r in active_downstream)
    dormant_note = (
        f"（本项目 vision 已判 dormant：{sorted(dormant_roles)}；这些角色本轮**不再扇出指令**，"
        "其 idiom 约束请翻译进 `final-Suno-prompt.md` 或制作计划文本层。）\n\n"
        if dormant_roles else ""
    )
    user_prompt = (
        f"项目名：`{project}`（写文件时把路径里的 `{{project}}` 占位符替换为本值）\n\n"
        f"{context}\n\n---\n"
        f"本轮制作人诉求：{task or '（未提供，请基于上游 vision + 简报综合推导）'}\n\n"
        "作为制作人，请产出：\n"
        f"1. `制作计划.md` — 项目统筹层（流派配比 / 时间线 / 角色调度 / 质量节点 / "
        f"probational 角色决策 promote/dormant）\n"
        f"2. **扇出 {len(active_downstream)} 份指令** — 每个 active 下游一份独立 FILE 块（缺一不可）：\n"
        f"{fanout_list}\n\n"
        f"{dormant_note}"
        "**重要**：每份指令的具体内容要按对应下游角色的职责定制化，不要复制粘贴。\n\n"
        "**Skill wikilink 显式约束（B1 按需加载机制）**：每份 `指令/给{角色}.md` 里，"
        "若你判断本任务需要该下游参考特定 skill（例如 R&B 作曲必读 [[C2-R&B-三全音代换]]"
        " + [[C3-R&B-经典进行2-5-1-4]]），请在该指令文档里**显式写出对应 skill 的 wikilink**"
        "（格式 `[[X<编号>-<流派>-<标题>]]`，必须落在该下游角色目录下，不可跨角色目录引用）。"
        "下游 main.py 会自动展开 wikilink 加载全文到 prompt context；未写时退化到流派"
        " keyword 触发整套 skill 兜底。给指令文档里写明的 skill 越精准，下游 LLM input token"
        "越省、知识越聚焦。可参考 [[F-{流派}]] §8 的 skill 索引按需挑选。"
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

    # 软告警：扇出文件数不达 active downstream（dormant 已过滤，不计入期望）
    fanout_written = [w for w in written if "/指令/给" in w]
    if len(fanout_written) < len(active_downstream):
        print(
            f"[{ROLE}] ⚠️ 扇出指令数不足：期望 {len(active_downstream)}，"
            f"实际 {len(fanout_written)}（{fanout_written}）",
            file=sys.stderr,
        )

    set_role_status(ROLE, status="success", reset_counters=True)
    set_role_status(ROLE, status="idle")
    append_audit({
        "timestamp": utc_now(), "role": ROLE, "project": project,
        "task": task, "result": "success", "outputs": written,
        "fanout_count": len(fanout_written),
        "fanout_expected": len(active_downstream),
        "dormant_skipped": sorted(dormant_roles),
    })
    print(f"[{ROLE}] 完成，输出：{written}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
