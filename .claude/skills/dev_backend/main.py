"""
dev_backend/main.py — 后端工程师执行入口（Phase 2b vault-based）

输入（vault）：
  - 10-项目/{project}/指令/给后端.md   技术主管下发的任务
  - 10-项目/{project}/系统设计.md       系统设计
  - 10-项目/{project}/PRD.md            产品需求
  - 00-系统/规则/技术栈.md               技术栈

输出：
  - src/backend/                  ← 项目仓内（不进 vault）
  - tests/backend/                ← 项目仓内（不进 vault）
  - 10-项目/{project}/API契约.md  ← vault 内

CLI：
  python .claude/skills/dev_backend/main.py --task "..." --project myproj
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
    project_dir, rules_dir, resolve_path, PROJECT_ROOT,
    VAULT_ROOT,
    expand_wikilinks, invalidate_wikilink_cache,
    discover_role_skills, render_triggered_block,
    load_role,
)
from engine.config import project_code_root as _project_code_root

ROLE = "后端工程师"


_NO_BACKEND_SIGNALS = ("无后端任务", "no backend", "纯前端", "frontend-only")

# 后端 skill wikilink 识别：匹配 stem `B<N>-描述` 或完整路径末尾 `.../B<N>-...`
# 注意：filter 在 wl.target 字符串上做匹配，target 已剥离 [[]] 和 #section / |alias
_BACKEND_SKILL_RE = re.compile(r"(?:^|/)B\d+-")


def _load_task_skills(
    task_text: str,
    upstream_text: str = "",
    project: str = "",
) -> tuple[str, list[str], list[str]]:
    """组合 wikilink 显式 ∪ keyword 触发器隐式两条路径，按需注入 backend skill。

    返回 (skill_block, loaded_targets, unresolved_targets)
      skill_block: 待注入 user_prompt 的文本（双路径都空时为空串）
      loaded_targets: 成功加载的 skill 标识列表（wikilink target + keyword stem）
      unresolved_targets: wikilink 路径里写错找不到的 target（warn 不 fail）

    设计要点：
    - wikilink 路径：filter 按 `B<N>-` 命名规约（保持向后兼容）
    - keyword 路径：扫 vault `20-知识/角色技能/se/后端工程师/` 下声明了
      frontmatter.trigger 的 skill；trigger 缺失 = fail-closed 不加载
    - 去重：wikilink 已加载的 stem 不再 keyword 重复注入
    - upstream_text + project 用于扩大 keyword 匹配上下文与 file_patterns 扫码
    """
    # ── 1. wikilink 显式路径（保留原逻辑）─────────────────────
    try:
        result = expand_wikilinks(
            task_text, VAULT_ROOT,
            filter=lambda wl: bool(_BACKEND_SKILL_RE.search(wl.target)),
            max_chars_per_link=3000,
            total_char_budget=12_000,
            max_depth=0,
            on_unresolved="warn",
        )
    except Exception as e:
        # DuplicateStemError 等命名规则破坏 → 不阻断 task，但要让人看见
        print(
            f"[{ROLE}] ⚠️ wikilink 展开失败（{type(e).__name__}: {e}），"
            f"本 task 不注入 skill。",
            file=sys.stderr,
        )
        return "", [], []

    wikilink_loaded: list[str] = []
    wikilink_parts: list[str] = []
    for e in result.expansions:
        if e.reason == "ok" and e.content:
            wikilink_loaded.append(e.wikilink.target)
            wikilink_parts.append(
                f"=== Skill 引用: [[{e.wikilink.target}]] "
                f"({e.path.name if e.path else '?'}) ===\n{e.content}"
            )

    wikilink_block = ""
    if wikilink_parts:
        wikilink_block = (
            "\n\n## 相关技能（按 task 正文 wikilink 加载）\n\n"
            + "\n\n".join(wikilink_parts)
            + "\n"
        )

    # ── 2. keyword 触发器路径 ────────────────────────────────
    role_dir = VAULT_ROOT / "20-知识" / "角色技能" / "se" / "后端工程师"
    code_root: Path | None = None
    if project:
        try:
            code_root = _project_code_root(project)
        except Exception:
            code_root = None  # PROJECT_CODE_ROOT 未配置或不可达 → 跳过 file_patterns
    hits = discover_role_skills(role_dir, task_text, upstream_text, code_root)

    # 去重：wikilink 已加载的 stem 不再 keyword 重复注入
    wikilink_stems = {
        t.rsplit("/", 1)[-1].removesuffix(".md") for t in wikilink_loaded
    }
    dedup_hits = [(p, r) for p, r in hits if p.stem not in wikilink_stems]
    keyword_block, keyword_loaded = render_triggered_block(dedup_hits)

    # ── 3. 合并 + 日志 ──────────────────────────────────────
    skill_block = wikilink_block + keyword_block
    print(
        f"[{ROLE}] skill 触发：wikilink={len(wikilink_loaded)} "
        f"keyword={len(keyword_loaded)} union={len(wikilink_loaded) + len(keyword_loaded)}"
    )

    return skill_block, wikilink_loaded + keyword_loaded, result.unresolved


def _task_marker(project: str, task_label: str):
    """单任务完成 marker：subprocess retry 时跳过已成功 task。

    每个 task_label（如 `给后端-T01`）一个 marker，整轮 success 时统一清理。
    """
    safe = task_label.replace("/", "_").replace("\\", "_")
    return VAULT_ROOT / "00-系统" / ".runtime-state" / f"dev_backend.{project}.{safe}.done"


def _collect_task_files(proj_dir, role_prefix: str, tech_stack):
    """收集任务文件列表。

    优先使用按任务编号拆分的文件（给后端-T01.md、给后端-T02.md ...）。
    若不存在则降级到整体文件（给后端.md / 给后端-压缩.md）。
    若 fallback 文件含"无后端"信号词，返回空列表（让外层走 idle 跳过路径）。

    返回 (task_files: list[Path], use_split: bool)
    """
    instr_dir = proj_dir / "指令"
    # 按编号拆分的任务文件（P1 新格式）
    split_files = sorted(instr_dir.glob(f"{role_prefix}-T*.md"))
    if split_files:
        return split_files, True
    # 降级：整体文件（旧格式兼容）
    compressed = instr_dir / f"{role_prefix}-压缩.md"
    original = instr_dir / f"{role_prefix}.md"
    single = compressed if compressed.exists() else original
    if not single.exists():
        return [], False
    head = single.read_text(encoding="utf-8", errors="replace")[:1000].lower()
    if any(sig in head for sig in _NO_BACKEND_SIGNALS):
        return [], False
    return [single], False


def main() -> int:
    args = parse_args()
    task = (args.task or "").strip()
    project = resolve_project(args)

    if role_is_blocked(ROLE):
        print(f"[{ROLE}] status=blocked，跳过。", file=sys.stderr)
        return 1

    set_role_status(ROLE, status="busy", enforce_transition=False)

    proj_dir = project_dir(project)
    tech_stack = rules_dir() / "技术栈.md"

    task_files, use_split = _collect_task_files(proj_dir, "给后端", tech_stack)

    if not task_files:
        # 检查是否存在索引文件且明确说明无后端任务（对称 dev_frontend 的跳过逻辑）
        index_file = proj_dir / "指令" / "给后端-索引.md"
        if index_file.exists():
            index_content = index_file.read_text(encoding="utf-8")
            if any(s.lower() in index_content.lower() for s in _NO_BACKEND_SIGNALS):
                print(f"[{ROLE}] ℹ️ 索引文件说明本项目无后端任务，跳过。")
                set_role_status(ROLE, status="success", reset_counters=True)
                set_role_status(ROLE, status="idle")
                append_audit({
                    "timestamp": utc_now(), "role": ROLE, "project": project,
                    "task": task, "result": "skipped", "reason": "no_backend_tasks",
                })
                return 0
        print(
            f"[{ROLE}] 必需输入缺失：{proj_dir}/指令/给后端*.md。请先跑技术主管。",
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
        })
        return 2

    if use_split:
        print(f"[{ROLE}] 📋 按任务拆分模式，共 {len(task_files)} 个任务文件")
    else:
        # § 15 层二：整体文件体积告警
        single = task_files[0]
        size = single.stat().st_size if single.exists() else 0
        if size > 30 * 1024:
            print(
                f"[{ROLE}] ⚠️ {single.name} 体积 {size // 1024}KB 超过 30KB 约束。"
                f"建议技术主管切换为按任务拆分输出。",
                file=sys.stderr,
            )

    system_prompt = build_system_prompt(ROLE, project=project)
    written_all: list[str] = []

    # 批处理多 task 前刷新 wikilink stem 索引（vault 可能被 git pull 更新）
    invalidate_wikilink_cache()

    # §3.1 rule_refs 章节级注入：F-后端#6 工程参考 skill 索引（一次性加载，跨 task 复用）。
    # frontmatter 声明了 rule_refs 但 main.py 不消费 = "声明已写、实施未补" 反模式。
    # 音乐域 commit 77d1244 已修同类问题；SE 域本次补齐 TL/后端/前端。
    role_def = load_role(ROLE)
    rule_block, source_hint = load_rule_block(role_def.rule_refs)
    print(f"[{ROLE}] rule_refs 注入：{source_hint}")

    for task_file in task_files:
        task_label = task_file.stem  # 如 "给后端-T02"

        # subprocess retry 跳过：已 marker 即认为本 task 之前已成功落盘
        marker = _task_marker(project, task_label)
        if marker.exists():
            print(f"[{ROLE}] ⏩ {task_label} 已完成（marker 存在），跳过重跑")
            continue

        print(f"[{ROLE}] ▶ 执行任务：{task_label}")

        # 每个任务只加载自己的指令文件 + 技术栈（最小上下文）
        context = read_input_files([task_file, tech_stack])
        if rule_block:
            context = context + "\n\n" + rule_block

        # 按 task 正文里的 wikilink + skill frontmatter trigger 关键词双路径注入 skill
        # （仅 backend skill `B<N>-...`）。双路径都空时 skill_block="" 不注入兜底集。
        task_text = task_file.read_text(encoding="utf-8")
        skill_block, loaded_skills, unresolved = _load_task_skills(
            task_text, upstream_text=context, project=project,
        )
        if loaded_skills:
            print(f"[{ROLE}] 📚 task 引用 skill: {', '.join(loaded_skills)}")
        if unresolved:
            print(
                f"[{ROLE}] ⚠️ task 引用了 {len(unresolved)} 个未解析 wikilink: "
                f"{', '.join(unresolved)}（已跳过，task 继续）",
                file=sys.stderr,
            )

        # 注入已生成文件列表，防止后续任务重新发明架构
        prior_files = [p for p in written_all if p.startswith("src/backend/") or p.startswith("tests/")]
        prior_context = ""
        if prior_files:
            prior_context = (
                "\n**已生成文件（保持架构一致，禁止重新设计已有模块）**：\n"
                + "\n".join(f"  - {p}" for p in prior_files)
                + "\n"
            )

        # API契约.md 只在最后一个任务写入，避免多次覆盖
        is_last_task = (task_file == task_files[-1])
        required = [
            "src/backend/main.py",
            "src/backend/<module>.py",
            "tests/backend/test_<module>.py",
        ]
        api_hint = ""
        if is_last_task:
            required.append(f"10-项目/{project}/API契约.md")
        else:
            api_hint = f"  - **不要**输出 `10-项目/{project}/API契约.md`（留给最后一个任务统一输出）\n"

        user_prompt = (
            f"项目名：`{project}`\n\n"
            f"{context}"
            f"{skill_block}\n\n"
            f"{prior_context}"
            "---\n"
            f"本轮任务：{task or f'实现 {task_label} 中的后端代码'}\n\n"
            "请按指令清单完整实现后端：\n"
            "  - 后端代码：路径以 `src/backend/...` 开头（项目仓内）\n"
            "  - 测试代码：路径以 `tests/backend/...` 开头\n"
            f"{api_hint}"
            "技术栈严格按 `00-系统/规则/技术栈.md`，禁止引入未授权依赖。\n"
            "所有 API 必须含输入校验、鉴权、结构化日志。\n"
            + render_required_outputs(required)
            + "\n上面是路径**示例**；请根据指令清单中的实际模块产出对应文件，每个文件用一个 FILE 块。"
        )

        try:
            raw_output = call_claude(system_prompt, user_prompt, ROLE)
        except Exception as e:
            err_str = str(e)
            error_phase = "llm_call"
            if any(k in err_str.lower() for k in ["context", "token", "length", "limit"]):
                error_phase = "output_overflow"
                print(
                    f"[{ROLE}] ⚠️ 疑似输出超限（{task_label}）。任务文件过大，"
                    f"建议进一步拆分。",
                    file=sys.stderr,
                )
            print(f"[{ROLE}] Claude API 调用失败（{task_label}）：{e}", file=sys.stderr)
            set_role_status(
                ROLE, status="failed",
                increment_consecutive_failures=True, increment_error=True,
                enforce_transition=False,
            )
            append_audit({
                "timestamp": utc_now(), "role": ROLE, "project": project,
                "task": task_label, "result": "failed", "error": err_str,
                "error_phase": error_phase,
            })
            return 1

        output_files = parse_claude_output_to_files(raw_output)
        if not output_files:
            dest = PROJECT_ROOT / "src" / "backend" / f"{task_label}_output.py"
            write_output_atomic(dest, raw_output)
            written_all.append(str(dest))
            print(f"[{ROLE}] 未检测到 FILE 标签，降级写入 {dest}")
        else:
            for rel_path, content in output_files.items():
                rel_resolved = rel_path.replace("{project}", project)
                dest = resolve_path(rel_resolved, project)
                write_output_atomic(dest, content)
                print(f"[{ROLE}] 写入: {dest}")
                written_all.append(rel_resolved)

        # task 成功（含降级写入）后写 marker
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(f"done at {utc_now()}\nlabel: {task_label}\n",
                              encoding="utf-8")
        except OSError as e:
            print(f"[{ROLE}] ⚠️ 写 {marker.name} 失败（{e}），retry 时会重跑此 task",
                  file=sys.stderr)

    # 整轮成功，清理所有 task marker
    for task_file in task_files:
        m = _task_marker(project, task_file.stem)
        if m.exists():
            try:
                m.unlink()
            except OSError:
                pass

    set_role_status(
        ROLE, status="success",
        reset_counters=True, last_output_path="src/backend/",
    )
    set_role_status(ROLE, status="idle")
    append_audit({
        "timestamp": utc_now(), "role": ROLE, "project": project,
        "task": task, "result": "success", "outputs": written_all,
    })
    print(f"[{ROLE}] 完成，输出：{written_all}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
