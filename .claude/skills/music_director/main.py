"""
music_director/main.py — 音乐总监执行入口（音乐域 L2-A 起步 + L3 收尾汇编节点）

双模式：

**首次模式**（链路第 1 步，inputs 只有创作简报）：
  输入：10-项目/music/{project}/inputs/创作简报.md
  输出：创作 vision.md + 指令/给制作人.md

**汇编模式**（链路末步，下游产物已全产）：
  输入：作曲产 Suno-prompt.md + 编曲/和声/混音/母带 4 份 Suno 补丁
       + 创作 vision.md（一致性参照）
       + 可选 inputs/take反馈.md（触发反馈分诊）
  输出：final-Suno-prompt.md（user 真正复制到 Suno 的最终版本，Style ≤ 1000 char）
       + 可选 反馈分诊.md（按反馈解析路由到对应角色 retry）

汇编模式工程契约：
  - Style 段 ≤ 1000 char（JS String.length = Python len()，硬上限）
  - post-write 实测 final_style_char_count + audit 留痕
  - 汇编次序参 vault 规则 [[Suno-汇编次序]]
  - 反馈格式参 vault 规则 [[Suno-take-反馈格式]]

CLI：
  python .claude/skills/music_director/main.py --task "..." --project myproj
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import (
    parse_args, resolve_project, build_system_prompt, read_input_files,
    write_output_atomic, parse_claude_output_to_files,
    call_claude, append_audit, utc_now, render_required_outputs,
    load_rule_block, load_genre_skill_block, load_genre_primitive_block,
)
from engine import (
    set_role_status, role_is_blocked,
    resolve_path,
)
from engine.role_loader import load_role

ROLE = "音乐总监"

# Style 段约定与 1000 硬上限统一从 role_runner 取（2026-09-03）。
# 此前这里有一份 `_STYLE_BLOCK_RE` 副本 + 字面量 `1000`，与 role_runner 的
# `_SUNO_STYLE_HARD_LIMIT` 各写各的；再加上规则文档
# `00-系统/规则/music/Suno-UI-字符上限.md`（0 条 rule_refs 指向、谁都没读过），
# 同一个数在三处存在。改一处漏两处的形状，先把代码这两处并了。
from engine.role_runner import (  # noqa: E402
    _SUNO_STYLE_HARD_LIMIT, is_suno_prompt_output, measure_style_chars,
)

# 超限即判失败的那一份产物。只有它 —— 它是 user 直接复制进 Suno 的终稿。
# 上游的 `Suno-prompt.md` 是基线，总监本来就会收窄它，在那儿拦等于把正常
# 中间态判成失败。
_BLOCKING_ARTIFACT = "final-Suno-prompt.md"

# 汇编模式 5 必要文件（命中即进汇编模式）
_AGGREGATION_REQUIRED = (
    "Suno-prompt.md",
    "编曲-Suno补丁.md",
    "和声-Suno补丁.md",
    "混音-Suno-retry补丁.md",
    "母带-Suno-retry补丁.md",
)


def _measure_style_chars(output_files: dict[str, str]) -> tuple[str | None, int | None]:
    """从本轮写出的 Suno prompt 产物抽 Style 段。返回 (文件相对路径, 字符数)。

    没有该类产物 → (None, None)；有产物但抽不出代码块 → (路径, None)。
    **两种情况必须分开**：原实现都返回 `None`，调用方无从区分，于是「产物在但
    正文是 2 字节垃圾」与「本轮压根没这个产物」走同一条静默路径。
    """
    for rel_path, content in output_files.items():
        if not is_suno_prompt_output(rel_path):
            continue
        return rel_path, measure_style_chars(content)
    return None, None


def _detect_aggregation_mode(project: str) -> tuple[bool, list[Path]]:
    """检测是否进汇编模式：5 必要文件全到位即进。

    返回 (is_aggregation, aggregation_inputs)：
    - aggregation_inputs 含 5 必要文件 + 创作 vision + 可选 take反馈
    """
    base = f"10-项目/music/{project}"
    required = [resolve_path(f"{base}/{fname}", project) for fname in _AGGREGATION_REQUIRED]
    if not all(p.exists() for p in required):
        return False, []

    optional = [
        resolve_path(f"{base}/创作 vision.md", project),
        resolve_path(f"{base}/inputs/take反馈.md", project),
    ]
    inputs = required + [p for p in optional if p.exists()]
    return True, inputs


def _has_take_feedback(aggregation_inputs: list[Path]) -> bool:
    return any(p.name == "take反馈.md" for p in aggregation_inputs)


def _run_first_pass(project: str, task: str, role_def) -> int:
    """L2-A 首次模式：读创作简报 → 产 vision + 给制作人指令。"""
    input_paths = [resolve_path(p, project) for p in role_def.inputs]
    output_rels = [p.replace("{project}", project) for p in role_def.outputs]

    existing_inputs = [p for p in input_paths if p.exists()]
    if not existing_inputs:
        print(
            f"[{ROLE}] 输入全部缺失：{[str(p) for p in input_paths]}。"
            f"请先在 inputs/ 放置创作简报。",
            file=sys.stderr,
        )
        set_role_status(
            ROLE, status="failed",
            increment_consecutive_failures=True, increment_error=True,
            enforce_transition=False,
        )
        append_audit({
            "timestamp": utc_now(), "role": ROLE, "project": project,
            "task": task, "result": "failed", "error": "missing_inputs",
            "mode": "first_pass",
        })
        return 2

    print(
        f"[{ROLE}] [首次模式] 读取到 {len(existing_inputs)} 份输入："
        f"{[p.name for p in existing_inputs]}",
        flush=True,
    )

    system_prompt = build_system_prompt(ROLE, project=project)
    context = read_input_files(input_paths)
    # 拼 rule_block 之前的项目侧文本。封闭规则：**召回只看项目侧文本，规则文本
    # 与已注入的参考资料不参与召回**。两条通道都扫这个，详见
    # ability_loader.assemble_user_context 同位注释的实测。
    project_text = context

    rule_block, source_hint = load_rule_block(role_def.rule_refs)
    print(f"[{ROLE}] rule_refs 注入：{source_hint}")
    if rule_block:
        context = context + "\n\n" + rule_block

    # primitive 在 skill 之前：它带着「该流派全部角色技能的索引」，是下面挑
    # skill wikilink 的依据（见 user_prompt 里的 B1 约束）。顺序即阅读顺序 ——
    # 但**索引节不参与 skill 召回**，否则「菜单」会被读成「点过的菜」。
    prim_block, prim_hint = load_genre_primitive_block(ROLE, task, project_text)
    print(f"[{ROLE}] 流派 primitive：{prim_hint}")
    if prim_block:
        context = context + "\n\n" + prim_block

    skill_block, skill_hint = load_genre_skill_block(ROLE, task, project_text)
    print(f"[{ROLE}] skill_trigger：{skill_hint}")
    if skill_block:
        context = context + "\n\n" + skill_block

    user_prompt = (
        f"项目名：`{project}`（写文件时把路径里的 `{{project}}` 占位符替换为本值）\n\n"
        f"{context}\n\n---\n"
        f"本轮总监诉求：{task or '（未提供，请基于创作简报综合推导）'}\n\n"
        "请综合简报内容，作为音乐总监给出：\n"
        "1. `创作 vision.md` — 流派统一意图 / 情感主轴 / 风格锚域 / 跨角色契约要点\n"
        "2. `指令/给制作人.md` — 总监对制作人的明确交底（项目统筹边界 / 流派配比 / "
        "质量节点 / probational 角色 promote 或 dormant 决策）\n\n"
        "两份产物均按角色基因 §10 输出契约 + 产物schema §1/§2 章节要求落盘。\n\n"
        "**Skill wikilink 显式约束（B1 按需加载机制）**：在 `创作 vision.md` 和 `指令/给制作人.md` "
        "里，若你需要某 skill 文档传达统一意图（例如 `[[D1-R&B-三条工程铁律与鉴别法]]`、"
        "`[[Ar6-R&B-Fusion配比]]`），请显式写出 wikilink（格式 `[[X<编号>-<流派>-<标题>]]`）。"
        "下游制作人扇出指令时会进一步在 `给{下游}.md` 里挑选对应该下游目录的 skill wikilink。"
        "未写时退化到流派 keyword 触发整套 skill 兜底。可参考 [[F-{流派}]] §8 的 skill 索引按需挑选。"
        f"{render_required_outputs(output_rels)}"
    )

    return _call_and_write(
        system_prompt, user_prompt, task, project,
        audit_extras={"mode": "first_pass"},
    )


def _run_aggregation(
    project: str, task: str, aggregation_inputs: list[Path], role_def,
) -> int:
    """汇编模式：聚合 5 文档 + 反馈 → final-Suno-prompt + 可选反馈分诊。"""
    has_feedback = _has_take_feedback(aggregation_inputs)

    print(
        f"[{ROLE}] [汇编模式] 5 必要文件全到位 + vision；"
        f"反馈解析：{'触发' if has_feedback else '未触发（无 take反馈.md）'}",
        flush=True,
    )

    output_rels = [f"10-项目/music/{project}/final-Suno-prompt.md"]
    if has_feedback:
        output_rels.append(f"10-项目/music/{project}/反馈分诊.md")

    system_prompt = build_system_prompt(ROLE, project=project)
    context = read_input_files(aggregation_inputs)
    project_text = context   # 同 first_pass：两条通道都只扫项目侧

    rule_block, source_hint = load_rule_block(role_def.rule_refs)
    print(f"[{ROLE}] rule_refs 注入（汇编模式）：{source_hint}")
    if rule_block:
        context = context + "\n\n" + rule_block

    prim_block, prim_hint = load_genre_primitive_block(ROLE, task, project_text)
    print(f"[{ROLE}] 流派 primitive（汇编模式）：{prim_hint}")
    if prim_block:
        context = context + "\n\n" + prim_block

    skill_block, skill_hint = load_genre_skill_block(ROLE, task, project_text)
    print(f"[{ROLE}] skill_trigger（汇编模式）：{skill_hint}")
    if skill_block:
        context = context + "\n\n" + skill_block

    fb_section = ""
    if has_feedback:
        fb_section = (
            "\n\n**反馈分诊任务（user 已提供 take 反馈）**：\n"
            "请按 vault 规则 [[Suno-执行.schema#§5.3 总监汇编节点路径（降级 / 跨会话 / 严肃返工）]] 解析反馈：\n"
            "1. 识别反馈点的角色归属（音色 → 作曲/混音 / 段间律动 → 编曲 / 和声层 → "
            "和声编写 / 响度 → 母带 / 跨角色 → 列出全部相关角色）\n"
            "2. 判断每个反馈点是否触发返工（重大偏离 vision → 返工；小幅偏好 → user 自行调）\n"
            "3. 若触发返工，给目标角色起草 retry 指令（明确：调什么 / 不动什么 / 验收口径）\n"
            "4. 产 `反馈分诊.md`，含「反馈逐条分诊表 + 触发的 retry 指令清单」"
        )

    user_prompt = (
        f"项目名：`{project}`（写文件时把路径里的 `{{project}}` 占位符替换为本值）\n\n"
        f"{context}\n\n---\n"
        f"本轮汇编诉求：{task or '（未提供，请按默认汇编次序合成 final-Suno-prompt）'}\n\n"
        "作为音乐总监收尾汇编，请按 vault 规则 [[Suno-汇编次序]] 产 `final-Suno-prompt.md`：\n\n"
        "**汇编原则**：\n"
        "1. **基底**：作曲 Suno-prompt.md 的 Style 段 + Lyrics 段是基底\n"
        "2. **下游补丁吸收**：编曲 / 和声 / 混音 / 母带 补丁中**确实需要修改 Style 或 Lyrics 的**条目，"
        "按以下优先级合入基底：\n"
        "   - 编曲段间 arrangement → 改 Lyrics inline tag（优先级最高，结构性变更）\n"
        "   - 和声层增补 → 改 Lyrics 对应 section inline tag（次优先级）\n"
        "   - 混音方向 → 改 Style（仅吸收非数字化方向词，如 mid-forward / round 等）\n"
        "   - 母带响度方向 → 改 Style（同上，仅方向词）\n"
        "3. **补丁声明「无需 patch」者直接跳过**\n"
        "4. **Style 段硬上限 1000 char**（Suno v4.5 JS String.length = Python len()）：\n"
        "   - 合入后用内部数一遍，超 1000 char 必须按优先级裁剪："
        "Vocal + No-list 不可删 / Production 段最先砍 / Fusion 第 1 句可短化\n"
        "   - **目标 ≤ 950 char**（留 50 余量给 user 微调）\n"
        "5. **Lyrics 段不受 1000 char 限制**，但应保持 Suno v4.5 inline tag 语法规范\n\n"
        "**输出结构**：\n"
        "- `final-Suno-prompt.md` 含 §1 使用指引 + §2 Style 段（``` 包裹）+ §3 Lyrics 段（``` 包裹）"
        "+ §4 汇编决策追溯（每条补丁吸收/跳过的理由）+ §5 字符数自计明细"
        f"{fb_section}\n"
        f"{render_required_outputs(output_rels)}"
    )

    return _call_and_write(
        system_prompt, user_prompt, task, project,
        audit_extras={
            "mode": "aggregation",
            "has_feedback": has_feedback,
        },
    )


def _call_and_write(
    system_prompt, user_prompt, task: str, project: str,
    audit_extras: dict,
) -> int:
    """LLM 调用 + 解析 FILE 块 + 落盘 + audit。两模式共享路径。"""
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
            **audit_extras,
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
            **audit_extras,
        })
        return 1

    written = []
    for rel_path, content in output_files.items():
        rel_resolved = rel_path.replace("{project}", project)
        dest = resolve_path(rel_resolved, project)
        write_output_atomic(dest, content)
        print(f"[{ROLE}] 写入: {dest}")
        written.append(rel_resolved)

    extra_audit: dict = dict(audit_extras)
    # 2026-09-03 去掉 `measure_style` 开关，改按**本轮实际写了什么**判定。
    # 原先只有 aggregation 调用点传 True；量不量取决于调用点记不记得传，
    # 与产物本身无关 —— 与 `commit_and_push(paths=None)` 同一类形状。
    style_path, style_chars = _measure_style_chars(output_files)
    if style_path is not None:
        style_oversized = (
            style_chars is not None and style_chars > _SUNO_STYLE_HARD_LIMIT
        )
        if style_chars is not None:
            marker = (f"⚠️ 超 {_SUNO_STYLE_HARD_LIMIT}"
                      if style_oversized else "✅")
            print(f"[{ROLE}] final Style 段字符数（Python len()）: "
                  f"{style_chars} {marker}")
        else:
            print(
                f"[{ROLE}] ⚠️ 产出了 {style_path} 但抽不出 Style 段"
                f"（正文无 ``` 代码块）—— 该产物大概率不可用，audit 的 "
                f"final_style_char_count 记为 null 不代表合规。",
                file=sys.stderr,
            )
        extra_audit.update({
            "final_style_path": style_path,
            "final_style_char_count": style_chars,
            "final_style_oversized": style_oversized,
        })

        if style_oversized and Path(style_path).name == _BLOCKING_ARTIFACT:
            # 2026-09-03：只拦总监的终稿，不拦中间产物。
            #
            # 依据：`final-Suno-prompt.md` 是 user 直接复制进 Suno 的那一份，
            # 超 1000 就是 Suno 侧截断 —— 拿到手才发现。而作曲的
            # `Suno-prompt.md` 是基线，总监下游本来就会收窄（实测
            # 1025→909 / 2314→868），在那儿拦等于把正常中间态判成失败。
            #
            # 文件**已经落盘**才拦：那份超限稿是排查依据（要看超在哪、砍哪段），
            # 删掉它等于把 LLM 这一轮的产出丢了。落盘 + 失败 = 证据留下、闸门关上。
            print(
                f"[{ROLE}] ❌ {style_path} 的 Style 段 {style_chars} char "
                f"超硬上限 {_SUNO_STYLE_HARD_LIMIT}。这是 user 直接复制进 Suno "
                f"的终稿，超限即被 Suno 截断。文件已落盘供排查，但本步判失败。"
                f"按 [[Suno-汇编次序]] 的优先级裁剪后重跑。",
                file=sys.stderr,
            )
            set_role_status(
                ROLE, status="failed",
                increment_consecutive_failures=True, increment_error=True,
                enforce_transition=False,
            )
            append_audit({
                "timestamp": utc_now(), "role": ROLE, "project": project,
                "task": task, "result": "failed", "error": "style_oversized",
                "outputs": written, **extra_audit,
            })
            return 1

    set_role_status(ROLE, status="success", reset_counters=True)
    set_role_status(ROLE, status="idle")
    append_audit({
        "timestamp": utc_now(), "role": ROLE, "project": project,
        "task": task, "result": "success", "outputs": written,
        **extra_audit,
    })
    print(f"[{ROLE}] 完成，输出：{written}")
    return 0


def main() -> int:
    args = parse_args()
    task = (args.task or "").strip()
    project = resolve_project(args)

    if role_is_blocked(ROLE):
        print(f"[{ROLE}] status=blocked，跳过。", file=sys.stderr)
        return 1

    set_role_status(ROLE, status="busy", enforce_transition=False)

    role_def = load_role(ROLE)

    # 模式检测：汇编模式优先（5 必要文件齐才进），否则走首次模式
    is_aggregation, aggregation_inputs = _detect_aggregation_mode(project)

    if is_aggregation:
        return _run_aggregation(project, task, aggregation_inputs, role_def)
    return _run_first_pass(project, task, role_def)


if __name__ == "__main__":
    sys.exit(main())
