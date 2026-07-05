"""
dev_frontend/main.py — 前端工程师执行入口（Phase 2b vault-based）

输入（vault）：
  - 10-项目/{project}/指令/给前端.md   技术主管下发的任务
  - 10-项目/{project}/PRD.md            产品需求
  - 10-项目/{project}/系统设计.md       系统设计
  - 10-项目/{project}/API契约.md        后端 API（若已有）
  - 00-系统/规则/技术栈.md               技术栈

输出：
  - src/frontend/                 ← 项目仓内（不进 vault）
  - tests/frontend/               ← 项目仓内（不进 vault）

CLI：
  python .claude/skills/dev_frontend/main.py --task "..." --project myproj
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import (
    parse_args, resolve_project, resolve_module_id, render_module_focus_hint,
    build_system_prompt, read_input_files,
    write_output_atomic, parse_claude_output_to_files,
    call_claude, append_audit, utc_now, render_required_outputs,
    load_rule_block, load_skill_block,
)
from engine import (
    set_role_status, role_is_blocked,
    project_dir, rules_dir, resolve_path, PROJECT_ROOT,
    VAULT_ROOT,
    expand_wikilinks, invalidate_wikilink_cache,
    load_role,
)

ROLE = "前端工程师"


_NO_FRONTEND_SIGNALS = ("无前端任务", "无前端 mvp", "不包含前端", "no frontend", "mvp 阶段无前端")

# 前端 skill wikilink 识别：匹配 stem `F<N>-描述` 或完整路径末尾 `.../F<N>-...`
import re as _re
_FRONTEND_SKILL_RE = _re.compile(r"(?:^|/)F\d+-")


def _load_task_skills(task_text: str) -> tuple[str, list[str], list[str]]:
    """从 task 正文解析 frontend skill wikilink，按需展开为可注入 prompt 的文本块。

    返回 (skill_block, loaded_targets, unresolved_targets)
    设计与 dev_backend._load_task_skills 对称，前缀改为 F<N>-。
    """
    try:
        result = expand_wikilinks(
            task_text, VAULT_ROOT,
            filter=lambda wl: bool(_FRONTEND_SKILL_RE.search(wl.target)),
            max_chars_per_link=3000,
            total_char_budget=12_000,
            max_depth=0,
            on_unresolved="warn",
        )
    except Exception as e:
        print(
            f"[{ROLE}] ⚠️ wikilink 展开失败（{type(e).__name__}: {e}），"
            f"本 task 不注入 skill。",
            file=sys.stderr,
        )
        return "", [], []

    loaded: list[str] = []
    parts: list[str] = []
    for e in result.expansions:
        if e.reason == "ok" and e.content:
            loaded.append(e.wikilink.target)
            parts.append(
                f"=== Skill 引用: [[{e.wikilink.target}]] "
                f"({e.path.name if e.path else '?'}) ===\n{e.content}"
            )

    skill_block = ""
    if parts:
        skill_block = (
            "\n\n## 相关技能（按 task 正文 wikilink 加载）\n\n"
            + "\n\n".join(parts)
            + "\n"
        )

    return skill_block, loaded, result.unresolved


def _task_marker(project: str, task_label: str) -> Path:
    """单任务完成 marker：subprocess retry 时跳过已成功 task。"""
    safe = task_label.replace("/", "_").replace("\\", "_")
    return VAULT_ROOT / "00-系统" / ".runtime-state" / f"dev_frontend.{project}.{safe}.done"


def _resolve_module_task_files(proj_dir: Path, module_id: str) -> list[Path]:
    """P8.7：模块化模式输入源 `模块/{module_id}-*.md`（与 dev_backend 对称）。

    抽出便于单元测试锁定 glob 语义。返回按 name 排序的 Path 列表；未找到 → 空 list。
    """
    return sorted((proj_dir / "模块").glob(f"{module_id}-*.md"))


def _collect_task_files(proj_dir, role_prefix: str):
    """收集前端任务文件列表，优先按编号拆分，降级到整体文件。

    若降级到的 single 文件（给前端.md / 给前端-压缩.md）含跳过信号词
    （技术主管 fallback 写入的"无前端"自然语言），视为空列表——让外层
    fallback 到信号词检测分支，避免硬跑前端任务。
    """
    instr_dir = proj_dir / "指令"
    split_files = sorted(instr_dir.glob(f"{role_prefix}-T*.md"))
    if split_files:
        return split_files, True
    compressed = instr_dir / f"{role_prefix}-压缩.md"
    original = instr_dir / f"{role_prefix}.md"
    single = compressed if compressed.exists() else original
    if not single.exists():
        return [], False
    head = single.read_text(encoding="utf-8", errors="replace")[:1000].lower()
    if any(sig in head for sig in _NO_FRONTEND_SIGNALS):
        return [], False
    return [single], False


def main() -> int:
    args = parse_args()
    task = (args.task or "").strip()
    project = resolve_project(args)
    module_id = resolve_module_id(args)  # P8.6：模块化 workflow 时非 None

    if role_is_blocked(ROLE):
        print(f"[{ROLE}] status=blocked，跳过。", file=sys.stderr)
        return 1

    set_role_status(ROLE, status="busy", enforce_transition=False)

    proj_dir = project_dir(project)
    tech_stack = rules_dir() / "技术栈.md"

    if module_id:
        # P8.7：模块化模式输入源改为 `模块/{module_id}-*.md`（TL 走 module_manifest
        # 分支产出的模块详情文件），不再读 legacy `给前端-T0N.md`。与 dev_backend 对称。
        module_task_files = _resolve_module_task_files(proj_dir, module_id)
        if not module_task_files:
            print(
                f"[{ROLE}] 必需输入缺失：{proj_dir}/模块/{module_id}-*.md。"
                f"请先跑技术主管（module_manifest 模式）产出模块详情。",
                file=sys.stderr,
            )
            set_role_status(
                ROLE, status="failed",
                increment_consecutive_failures=True, increment_error=True,
                enforce_transition=False,
            )
            append_audit({
                "timestamp": utc_now(), "role": ROLE, "project": project,
                "task": task, "result": "failed",
                "error": "missing_module_file", "module_id": module_id,
            })
            return 2
        task_files = module_task_files
        use_split = True  # 单模块作为 1 个 task，走"多任务"路径的通用循环
    else:
        task_files, use_split = _collect_task_files(proj_dir, "给前端")

    if not task_files:
        # 检查是否存在索引文件且明确说明无前端任务
        index_file = proj_dir / "指令" / "给前端-索引.md"
        if index_file.exists():
            index_content = index_file.read_text(encoding="utf-8")
            no_frontend_signals = ["无前端任务", "无前端 MVP", "不包含前端", "no frontend", "MVP 阶段无前端"]
            if any(s.lower() in index_content.lower() for s in no_frontend_signals):
                print(f"[{ROLE}] ℹ️ 索引文件说明本项目无前端任务，跳过。")
                set_role_status(ROLE, status="success", reset_counters=True)
                set_role_status(ROLE, status="idle")
                append_audit({
                    "timestamp": utc_now(), "role": ROLE, "project": project,
                    "task": task, "result": "skipped", "reason": "no_frontend_tasks",
                })
                return 0
        print(
            f"[{ROLE}] 必需输入缺失：{proj_dir}/指令/给前端*.md。请先跑技术主管。",
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

    # API契约.md：后端产出，前端只读；文件不存在时跳过（不阻断）
    api_contract = proj_dir / "API契约.md"

    # §3.1 rule_refs 章节级注入：F-前端#6 工程参考 skill 索引（跨 task 复用）。
    # 与 dev_backend 同范式（音乐域 commit 77d1244 同思路）。
    role_def = load_role(ROLE)
    rule_block, source_hint = load_rule_block(role_def.rule_refs)
    print(f"[{ROLE}] rule_refs 注入：{source_hint}")

    for task_file in task_files:
        task_label = task_file.stem
        print(f"[{ROLE}] ▶ 执行任务：{task_label}")

        # subprocess retry 跳过：已 marker 即认为本 task 之前已成功落盘
        marker = _task_marker(project, task_label)
        if marker.exists():
            print(f"[{ROLE}] ⏩ {task_label} 已完成（marker 存在），跳过重跑")
            continue

        # 每个任务加载指令文件 + 技术栈 + API契约（若存在）
        extra_inputs = [f for f in [api_contract] if f.exists()]
        if extra_inputs:
            print(f"[{ROLE}] 📄 注入 API契约.md（{api_contract.stat().st_size} bytes）")
        context = read_input_files([task_file, tech_stack, *extra_inputs])
        if rule_block:
            context = context + "\n\n" + rule_block

        # D3 双路径：wikilink 显式 ∪ keyword 触发，按 stem 去重 union
        task_text = task_file.read_text(encoding="utf-8")
        skill_block, skill_hint = load_skill_block(
            ROLE, task_text, upstream_text="", domain="se",
        )
        if skill_block:
            print(f"[{ROLE}] 📚 skill_trigger（双路径）：{skill_hint}")
        else:
            print(f"[{ROLE}] skill_trigger：{skill_hint}")

        # 注入已生成文件列表，防止后续任务重新发明架构
        prior_files = [p for p in written_all if p.startswith("src/frontend/") or p.startswith("tests/")]
        prior_context = ""
        if prior_files:
            prior_context = (
                "\n**已生成文件（保持架构一致，禁止重新设计已有模块）**：\n"
                + "\n".join(f"  - {p}" for p in prior_files)
                + "\n"
            )

        # P8.6：模块化模式下加"单模块聚焦"约束段 + 追加进度流/测试报告要求
        module_focus = render_module_focus_hint(module_id, project)
        base_required = [
            "src/frontend/index.html",
            "src/frontend/app.js",
            "src/frontend/styles/main.css",
            "tests/frontend/test_<x>.js",
        ]
        if module_id:
            base_required.extend([
                f"10-项目/{project}/进度/{module_id}-progress.md",
                f"10-项目/{project}/测试报告/{module_id}.md",
            ])

        user_prompt = (
            f"项目名：`{project}`\n\n"
            f"{module_focus}"
            f"{context}"
            f"{skill_block}\n\n"
            f"{prior_context}"
            "---\n"
            f"本轮任务：{task or f'实现 {task_label} 中的前端代码'}\n\n"
            "请按指令清单完整实现前端：\n"
            "  - 入口 HTML + 主 JS：`src/frontend/index.html`、`src/frontend/app.js`\n"
            "  - 公共组件：`src/frontend/components/...`\n"
            "  - 页面：`src/frontend/pages/...`\n"
            "  - 状态管理：`src/frontend/store/...`\n"
            "  - 样式：`src/frontend/styles/...`\n"
            "  - 测试：`tests/frontend/...`\n\n"
            "技术栈严格按 `00-系统/规则/技术栈.md`；需含全局 Error Boundary、"
            "loading/error 状态、响应式适配。\n"
            + render_required_outputs(base_required)
            + "\n上面是路径**示例**；请根据指令清单中的实际功能划分产出对应文件，每个文件用一个 FILE 块。"
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
            dest = PROJECT_ROOT / "src" / "frontend" / f"{task_label}_index.html"
            write_output_atomic(dest, raw_output)
            written_all.append(str(dest))
            print(f"[{ROLE}] 未检测到 FILE 标签，降级写入 {dest}")
        else:
            for rel_path, content in output_files.items():
                rel_path = rel_path.replace("{project}", project)
                dest = resolve_path(rel_path, project)
                write_output_atomic(dest, content)
                print(f"[{ROLE}] 写入: {dest}")
                written_all.append(rel_path)

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
        reset_counters=True, last_output_path="src/frontend/",
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
