"""
technical_lead/main.py — 技术主管执行入口（Phase 2b vault-based）

输入（vault）：
  - 10-项目/{project}/指令/给技术主管.md   架构师下发的任务
  - 10-项目/{project}/系统设计.md          系统设计
  - 00-系统/规则/技术栈.md                  技术栈

输出（vault）：
  - 10-项目/{project}/指令/给后端.md
  - 10-项目/{project}/指令/给前端.md

CLI：
  python .claude/skills/technical_lead/main.py --task "..." --project myproj
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import (
    parse_args, resolve_project, build_system_prompt, read_input_files,
    write_output_atomic, parse_claude_output_to_files,
    call_claude, append_audit, utc_now, render_required_outputs,
    enforce_output_limits,
)
from engine import (
    set_role_status, role_is_blocked,
    project_dir, rules_dir, resolve_path,
    VAULT_ROOT,
    load_role, RoleNotFound,
)
from engine.llm import call_llm

ROLE = "技术主管"

_VALID_PROJECT_TYPES = ("backend-only", "frontend-only", "full-stack")

# 脑暴笔记轮次标题：`### 第 N 轮 · 角色名`
_ROUND_HEADING_RE = re.compile(r"^### 第\s*(\d+)\s*轮\b", re.MULTILINE)

# 末轮决议合计上限（多份脑暴拼接后的总体积，防止极端情况打爆 prompt）
_DISCUSSION_LAST_ROUND_MAX_CHARS = 30 * 1024


def _extract_last_round_text(content: str) -> str | None:
    """从脑暴笔记中抽出最大 N 的 `### 第 N 轮 ...` 段。

    返回 None 表示笔记未使用 `### 第 N 轮` 结构（调用方应回退读全文）。
    """
    matches = list(_ROUND_HEADING_RE.finditer(content))
    if not matches:
        return None
    last = max(matches, key=lambda m: int(m.group(1)))
    start = last.start()
    rest = content[last.end():]
    next_round = _ROUND_HEADING_RE.search(rest)
    end = last.end() + next_round.start() if next_round else len(content)
    return content[start:end].strip()


def _backend_done_marker() -> Path:
    """后端轮 done marker：子进程超时 retry 时用来跳过已成功的后端轮。"""
    return VAULT_ROOT / "00-系统" / ".runtime-state" / "技术主管.backend_done"


def _plan_cache_path(project: str) -> Path:
    """Plan call 结果缓存：subprocess retry 时跳过重新调 LLM。

    缓存键含 project 名，多项目并行/切换不互相覆盖。
    backend pass 整轮成功后清理（见 main 末尾 marker 清理处）。
    """
    return VAULT_ROOT / "00-系统" / ".runtime-state" / f"技术主管.plan-{project}.json"


def _read_project_type(to_lead_path: Path) -> tuple[str, str]:
    """读取「给技术主管.md」frontmatter 中的 project_type 字段。

    返回 (project_type, source)；source ∈ {"frontmatter", "default_full_stack"}。
    架构师未声明时默认 full-stack（向后兼容，旧项目行为不变）。
    """
    try:
        text = to_lead_path.read_text(encoding="utf-8")
    except OSError:
        return "full-stack", "default_full_stack"

    # 匹配 frontmatter 块：开头 --- 到下一个 ---
    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not fm_match:
        return "full-stack", "default_full_stack"

    fm_body = fm_match.group(1)
    type_match = re.search(r"^project_type:\s*([a-z-]+)", fm_body, re.MULTILINE)
    if not type_match:
        return "full-stack", "default_full_stack"

    declared = type_match.group(1).strip()
    if declared not in _VALID_PROJECT_TYPES:
        print(f"[{ROLE}] ⚠️ project_type='{declared}' 不在合法集 {_VALID_PROJECT_TYPES}，"
              f"按 full-stack 处理", file=sys.stderr)
        return "full-stack", "default_full_stack"
    return declared, "frontmatter"


def _write_skip_stub(proj_dir: Path, side: str, project_type: str) -> Path:
    """写「给{side}-索引.md」stub，触发 dev_{side} 的 idle 跳过。

    side ∈ {"后端", "前端"}；project_type 用于落档可观测性。
    """
    dest = proj_dir / "指令" / f"给{side}-索引.md"
    other = "前端" if side == "后端" else "后端"
    content = (
        f"---\n"
        f"type: task-index\n"
        f"role: {side}工程师\n"
        f"decided_by: project_type-frontmatter\n"
        f"project_type: {project_type}\n"
        f"decided_at: {utc_now()}\n"
        f"---\n\n"
        f"# 无{side}任务\n\n"
        f"本项目 `project_type={project_type}`，仅含{other}业务，无{side}实现。\n\n"
        "若架构师判定有误，请改「给技术主管.md」frontmatter 中的 `project_type` 字段"
        f"后重跑 `--start-from 技术主管`。\n"
    )
    write_output_atomic(dest, content)
    return dest


# ── 后端轮 Plan + Detail × N 拆分（2026-05-16 治理）─────────────
# 背景：sonnet-4-6 单 call 输入 25K+ tokens、输出 8K tokens 在 CLI 子进程
# 模式下出现 600s 无任何流式输出的死锁。拆为多个小 call 后单次输入/输出
# 都更小，且任一 detail 卡死不污染已成功项。

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)


def _extract_json_block(text: str) -> str:
    """从 LLM 输出里抠出 JSON 块。优先取 ```json``` 围栏，否则取首个 {...} 平衡区间。"""
    m = _JSON_FENCE_RE.search(text)
    if m:
        return m.group(1).strip()
    start = text.find("{")
    if start == -1:
        raise ValueError("未找到 JSON 起始 `{`")
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise ValueError("JSON 括号未配平")


def _resolve_role_model() -> str:
    """读取 ROLE 角色配置里的 model 名（用于 Plan/Detail call）。"""
    try:
        return load_role(ROLE).model
    except RoleNotFound:
        return "claude-sonnet-4-6"


def _run_backend_pass_split(
    system_prompt: tuple[str, str],
    base_prompt: str,
    project: str,
    proj_dir: Path,
) -> tuple[bool, list[str]]:
    """拆 backend 单 call 为 Plan + Detail × N。

    返回 (success, written_relpaths)。
    success=False 表示 Plan 解析失败或 detail 调用失败，调用方应回退原单 call。
    written_relpaths 已成功落盘的相对路径（即使最终 False，索引已写入也会在列表里）。
    """
    LIMIT_CHARS = 30 * 1024
    model = _resolve_role_model()

    # ── Plan 缓存命中：subprocess retry 时跳过 LLM 重调 ────
    plan_cache = _plan_cache_path(project)
    cached_plan: dict | None = None
    if plan_cache.exists():
        try:
            cached_plan = json.loads(plan_cache.read_text(encoding="utf-8"))
            if isinstance(cached_plan, dict) and isinstance(cached_plan.get("tasks"), list):
                print(f"[{ROLE}] ⏩ 命中 Plan 缓存（{plan_cache.name}），跳过 Plan call")
            else:
                cached_plan = None
        except Exception as e:
            print(f"[{ROLE}] ⚠️ Plan 缓存解析失败（{e}），回退实时 Plan call",
                  file=sys.stderr)
            cached_plan = None

    if cached_plan is not None:
        plan = cached_plan
    else:
        plan = None  # 实际 plan call 在下面

    # ── Plan call：列任务大纲 + 索引正文 ────────────────────
    plan_prompt = base_prompt + (
        "**本轮只列任务大纲 + 索引正文，不写任务细节**。请输出**单个 JSON 块**：\n\n"
        "```json\n"
        "{\n"
        '  "tasks": [\n'
        '    {"id": "T01", "title": "...", "summary": "1-3 句，含核心交付 + 关键依赖"},\n'
        '    {"id": "T02", "title": "...", "summary": "..."}\n'
        "  ],\n"
        '  "index_md_body": "<索引 markdown 正文（含 frontmatter / 任务表 / 依赖图 / 决议速查）>"\n'
        "}\n"
        "```\n\n"
        "**无后端业务时的合法出口**：`{\"tasks\": [], \"index_md_body\": "
        "\"# 无后端任务\\n\\n判定理由：...\"}`。\n"
        "约束：\n"
        f"- 任务 id 形如 `T01`/`T02`/`T05a`/`T05b`，写入文件路径 `10-项目/{project}/指令/给后端-{{id}}.md`\n"
        "- JSON 必须可被 `json.loads` 解析；index_md_body 不要嵌套 ``` 围栏\n"
        "- index_md_body 自身视为一个完整 markdown 文件正文（写入「给后端-索引.md」）\n"
    )

    if plan is None:
        print(f"[{ROLE}] 📋 Plan call（任务大纲，max_tokens=2048）...")
        try:
            plan_raw = call_llm(system_prompt, plan_prompt, model=model, max_tokens=2048)
        except Exception as e:
            print(f"[{ROLE}] ⚠️ Plan call 异常：{e}，回退单 call", file=sys.stderr)
            return False, []

        try:
            plan_json_str = _extract_json_block(plan_raw)
            plan = json.loads(plan_json_str)
        except Exception as e:
            print(f"[{ROLE}] ⚠️ Plan JSON 解析失败：{e}，回退单 call", file=sys.stderr)
            return False, []

        # 缓存 plan（subprocess retry 时跳过 LLM 重调）
        try:
            plan_cache.parent.mkdir(parents=True, exist_ok=True)
            plan_cache.write_text(
                json.dumps(plan, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"[{ROLE}] 💾 Plan 缓存已落盘: {plan_cache.name}")
        except OSError as e:
            print(f"[{ROLE}] ⚠️ Plan 缓存写入失败（{e}），retry 时会重跑 Plan call",
                  file=sys.stderr)

    tasks = plan.get("tasks")
    index_body = (plan.get("index_md_body") or "").strip()
    if not isinstance(tasks, list):
        print(f"[{ROLE}] ⚠️ Plan tasks 非 list（{type(tasks).__name__}），回退单 call",
              file=sys.stderr)
        return False, []

    written: list[str] = []

    # ── 写索引文件 ──────────────────────────────────────────
    if index_body:
        index_dest = proj_dir / "指令" / "给后端-索引.md"
        index_body = enforce_output_limits(index_body, ROLE, index_dest.name, LIMIT_CHARS)
        write_output_atomic(index_dest, index_body)
        rel = f"10-项目/{project}/指令/给后端-索引.md"
        written.append(rel)
        print(f"[{ROLE}] ✅ 索引写入: {index_dest}")
    else:
        print(f"[{ROLE}] ⚠️ Plan 未提供 index_md_body，跳过索引落盘", file=sys.stderr)

    if not tasks:
        print(f"[{ROLE}] ℹ️ Plan tasks 为空（无后端业务），跳过 detail call")
        return True, written

    # ── Detail call × N：逐个任务细化 ──────────────────────
    task_summary_block = "\n".join(
        f"- {t.get('id', '?')}: {t.get('title', '')} — {t.get('summary', '')}"
        for t in tasks if isinstance(t, dict)
    )

    for task in tasks:
        if not isinstance(task, dict):
            print(f"[{ROLE}] ⚠️ 跳过非 dict 任务条目：{task!r}", file=sys.stderr)
            continue
        tid = str(task.get("id") or "").strip()
        title = str(task.get("title") or "").strip()
        summary = str(task.get("summary") or "").strip()
        if not tid or not title:
            print(f"[{ROLE}] ⚠️ 跳过缺 id/title 的任务：{task!r}", file=sys.stderr)
            continue

        rel_target = f"10-项目/{project}/指令/给后端-{tid}.md"
        # subprocess retry 友好：已存在的 detail 直接跳过，避免每次 retry 重跑所有 task
        dest_check = resolve_path(rel_target, project)
        if dest_check.exists() and dest_check.stat().st_size > 200:
            print(f"[{ROLE}] ⏩ Detail ({tid}) 已存在 "
                  f"({dest_check.stat().st_size} chars)，跳过重跑")
            written.append(rel_target)
            continue
        detail_prompt = base_prompt + (
            "## 本轮任务清单（参考，便于校准依赖与不重不漏）\n"
            f"{task_summary_block}\n\n---\n\n"
            f"## 本次只产出**一个任务**的细节：{tid} — {title}\n\n"
            f"摘要：{summary}\n\n"
            "文件必须包含：\n"
            "- frontmatter（type/project/task_id/title/from/to/estimate_hours/depends_on/unblocks/created）\n"
            "- 功能描述、模块/接口、输入输出、验收标准\n"
            "- 工作量 ≤ 4 小时；若已是 5x 拆分子任务请说明拆法\n"
            "**只输出这一个 FILE 块，不要重复索引、不要输出其它任务文件，"
            "不要在 FILE 块外写任何对话性文字**。\n"
            + render_required_outputs([rel_target])
        )

        print(f"[{ROLE}] 📝 Detail call: {tid} — {title}（max_tokens=1536）...")
        try:
            detail_raw = call_llm(
                system_prompt, detail_prompt,
                model=model, max_tokens=1536,
            )
        except Exception as e:
            print(f"[{ROLE}] ❌ Detail call 失败 ({tid}): {e}", file=sys.stderr)
            return False, written

        output_files = parse_claude_output_to_files(detail_raw)
        if not output_files:
            dest = resolve_path(rel_target, project)
            # 情况 A：CLI 模式 LLM 用了 Write 工具直接写盘（违反 OUTPUT_FORMAT_SPEC，
            # 但已通过 yaml --disallowed-tools 阻断；这里是历史防御，保留以防再发生）
            if dest.exists() and dest.stat().st_size > 200:
                print(
                    f"[{ROLE}] ℹ️ Detail ({tid}) 未走 FILE 块但目标文件已存在 "
                    f"({dest.stat().st_size} chars)，疑似 LLM 工具产出，保留: {dest}",
                    file=sys.stderr,
                )
                written.append(rel_target)
                continue
            # 情况 B：raw 全文直写（兜底防 silent skip 丢内容）
            content = enforce_output_limits(detail_raw, ROLE, dest.name, LIMIT_CHARS)
            write_output_atomic(dest, content)
            print(f"[{ROLE}] ⚠️ Detail ({tid}) 无 FILE 标签，raw 直写: {dest}",
                  file=sys.stderr)
            written.append(rel_target)
            continue

        for rel_path, content in output_files.items():
            rel_resolved = rel_path.replace("{project}", project)
            dest = resolve_path(rel_resolved, project)
            is_instruction = (
                ("给后端" in dest.name or "给前端" in dest.name)
                and dest.suffix == ".md"
                and "索引" not in dest.name
            )
            if is_instruction:
                content = enforce_output_limits(content, ROLE, dest.name, LIMIT_CHARS)
            write_output_atomic(dest, content)
            print(f"[{ROLE}] 写入: {dest}")
            written.append(rel_resolved)

    return True, written


def main() -> int:
    args = parse_args()
    task = (args.task or "").strip()
    project = resolve_project(args)

    if role_is_blocked(ROLE):
        print(f"[{ROLE}] status=blocked，跳过。", file=sys.stderr)
        return 1

    set_role_status(ROLE, status="busy", enforce_transition=False)

    proj_dir = project_dir(project)
    to_lead = proj_dir / "指令" / "给技术主管.md"
    sys_design = proj_dir / "系统设计.md"
    tech_stack = rules_dir() / "技术栈.md"

    missing = [p for p in (to_lead, sys_design) if not p.exists()]
    if missing:
        print(
            f"[{ROLE}] 必需输入缺失：{[str(p) for p in missing]}。请先跑架构师。",
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
            "missing": [str(p) for p in missing],
        })
        return 2

    # 上游补丁：架构师的 DYNAMIC 区域已在 build_system_prompt 内自动注入
    system_prompt = build_system_prompt(ROLE, project=project)

    # Phase 4c-3 + 2026-05-16 token 优化：脑暴笔记只读末轮裁决段，
    # 前几轮 critic/PM/前端发言对 TL 派活无价值，剥除节约 ~5K tokens。
    discussion_logs = sorted((proj_dir).glob("脑暴-*.md")) if proj_dir.is_dir() else []
    inputs = [to_lead, sys_design, tech_stack]
    context = read_input_files(inputs)

    discussion_hint = ""
    discussion_parts: list[str] = []
    for log_path in discussion_logs:
        try:
            log_text = log_path.read_text(encoding="utf-8")
        except OSError as e:
            print(f"[{ROLE}] ⚠️ 读取 {log_path.name} 失败：{e}，跳过", file=sys.stderr)
            continue
        last_round = _extract_last_round_text(log_text)
        if last_round is None:
            print(f"[{ROLE}] ⚠️ {log_path.name} 未识别 `### 第 N 轮` 结构，回退读全文",
                  file=sys.stderr)
            last_round = log_text
        discussion_parts.append(
            f"=== {log_path.name}（仅末轮决议） ===\n{last_round}\n==="
        )

    if discussion_parts:
        joined = "\n\n".join(discussion_parts)
        if len(joined) > _DISCUSSION_LAST_ROUND_MAX_CHARS:
            print(
                f"[{ROLE}] ⚠️ 末轮决议合计 {len(joined)} chars 超 "
                f"{_DISCUSSION_LAST_ROUND_MAX_CHARS}，硬截断。",
                file=sys.stderr,
            )
            joined = joined[:_DISCUSSION_LAST_ROUND_MAX_CHARS] + (
                f"\n\n⚠️ [discussion 截断] 末轮决议合计超 "
                f"{_DISCUSSION_LAST_ROUND_MAX_CHARS} 字符，已截断。"
            )
        context = context + "\n\n" + joined
        names = "、".join(p.name for p in discussion_logs)
        discussion_hint = (
            f"\n**注意**：上面已包含讨论笔记（{names}）的**末轮裁决段**"
            f"（架构师收尾决议清单 / 总结）。请把这些**已收敛的决策**直接落到"
            f"给后端/给前端的实施约束中，**不要重新发起讨论里已收敛过的争论**。"
            f"完整讨论历史未注入（节约 token），如需查阅请直接读 vault 原笔记。\n"
        )

    base_prompt = (
        f"项目名：`{project}`\n\n"
        f"{context}\n\n---\n"
        f"本轮任务：{task or '按架构设计拆分开发任务'}\n"
        f"{discussion_hint}\n"
        "每个任务必须有：功能描述、对应模块/接口、输入输出、验收标准\n"
        "单任务工作量 ≤ 4 小时，超过须再拆分\n"
        "每个文件只包含该任务自身的描述 + 验收标准 + 必要的接口约束\n"
        "禁止在单个文件中汇总所有任务\n\n"
    )

    backend_prompt = base_prompt + (
        "**本次只输出后端任务文件**，不要输出任何前端内容。\n"
        "每个任务单独一个 FILE 块，按编号命名：\n"
        f"  `10-项目/{project}/指令/给后端-T01.md`、`给后端-T02.md` ...\n\n"
        "**无后端业务时的合法出口**：如果项目本身无服务端业务"
        "（静态站 / 纯前端 SPA / 浏览器扩展），\n"
        f"允许只输出一份 `10-项目/{project}/指令/给后端-索引.md`，内容首行写 `# 无后端任务`，\n"
        "并简述判定理由（≤ 100 字）。**不要凑后端任务**。\n"
        + render_required_outputs([f"10-项目/{project}/指令/给后端-索引.md"])
    )

    frontend_prompt = base_prompt + (
        "**本次只输出前端任务文件**，不要输出任何后端内容。\n"
        "标注与后端的协作关系（API 契约、数据流）。\n"
        "每个任务单独一个 FILE 块，按编号命名：\n"
        f"  `10-项目/{project}/指令/给前端-T01.md`、`给前端-T02.md` ...\n\n"
        "**无前端业务时的合法出口**：如果项目本身无浏览器/移动端 UI"
        "（CLI / 库 / 工具 / 数据管线 / 后台 job / API-only 服务），\n"
        f"允许只输出一份 `10-项目/{project}/指令/给前端-索引.md`，内容首行写 `# 无前端任务`，\n"
        "并简述判定理由（≤ 100 字）。**不要凑前端任务**——拼凑出来的前端任务会让\n"
        "下游 dev_frontend 浪费 token 并误导用户。\n"
        + render_required_outputs([f"10-项目/{project}/指令/给前端-索引.md"])
    )

    LIMIT_CHARS = 30 * 1024
    written = []
    marker = _backend_done_marker()

    # 读取「给技术主管.md」frontmatter 中的 project_type，驱动对称跳过
    project_type, type_source = _read_project_type(to_lead)
    print(f"[{ROLE}] 🏷️ project_type={project_type}（来源：{type_source}）")
    if type_source == "default_full_stack":
        print(f"[{ROLE}] ℹ️ 「给技术主管.md」frontmatter 未声明 project_type，"
              f"默认按 full-stack 跑两轮。若实际为单端项目，请改 frontmatter "
              f"加 `project_type: backend-only` 或 `frontend-only` 后重跑。")

    for pass_name, user_prompt in [("后端任务", backend_prompt), ("前端任务", frontend_prompt)]:
        side = "后端" if pass_name == "后端任务" else "前端"

        # ── project_type 驱动的对称跳过 ────────────────────────────────
        should_skip = (
            (side == "前端" and project_type == "backend-only")
            or (side == "后端" and project_type == "frontend-only")
        )
        if should_skip:
            dest = _write_skip_stub(proj_dir, side, project_type)
            print(f"[{ROLE}] ⏭️ 跳过{side}轮（project_type={project_type}），写 stub: {dest.name}")
            written.append(f"10-项目/{project}/指令/给{side}-索引.md")
            continue

        # ── 后端轮：检测 done marker，子进程超时 retry 时跳过重跑 ────────
        if pass_name == "后端任务":
            existing = sorted((proj_dir / "指令").glob("给后端-T*.md"))
            backend_index = proj_dir / "指令" / "给后端-索引.md"
            if marker.exists() and backend_index.exists() and existing:
                print(f"[{ROLE}] ⏩ 后端轮已完成（marker 存在 + {len(existing)} 个任务卡），跳过重跑")
                for p in [backend_index, *existing]:
                    rel = f"10-项目/{project}/指令/{p.name}"
                    written.append(rel)
                continue

        # ── 后端轮：优先 Plan + Detail × N（2026-05-16 治理）─────────
        # 避免 sonnet 单次 25K 输入 + 8K 输出在 CLI 子进程模式下的 600s 死锁。
        # 拆为 plan (2K out) + detail × N (1.5K out)；任一失败回退原单 call。
        if pass_name == "后端任务":
            ok, new_files = _run_backend_pass_split(
                system_prompt, base_prompt, project, proj_dir,
            )
            if ok:
                written.extend(new_files)
                try:
                    marker.parent.mkdir(parents=True, exist_ok=True)
                    marker.write_text(
                        f"done at {utc_now()}\nfiles: {len(new_files)}\nmode: plan+detail\n",
                        encoding="utf-8",
                    )
                except OSError as e:
                    print(f"[{ROLE}] ⚠️ 写 backend_done marker 失败（{e}），retry 时会重跑",
                          file=sys.stderr)
                continue
            print(
                f"[{ROLE}] ⚠️ Plan/Detail 路径失败（已写入 {len(new_files)} 个文件，"
                f"将由单 call 覆盖重写），回退原单 call",
                file=sys.stderr,
            )

        print(f"[{ROLE}] 📝 生成{pass_name}...")
        try:
            raw_output = call_claude(system_prompt, user_prompt, ROLE)
        except Exception as e:
            print(f"[{ROLE}] Claude API 调用失败（{pass_name}）：{e}", file=sys.stderr)
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
            # 降级：整体写入对应文件
            fallback_name = "给后端.md" if "后端" in pass_name else "给前端.md"
            dest = proj_dir / "指令" / fallback_name
            enforced = enforce_output_limits(raw_output, ROLE, dest.name, LIMIT_CHARS)
            write_output_atomic(dest, enforced)
            written.append(f"10-项目/{project}/指令/{fallback_name}")
            print(f"[{ROLE}] 未检测到 FILE 标签，降级写入 {dest}")
        else:
            for rel_path, content in output_files.items():
                rel_resolved = rel_path.replace("{project}", project)
                dest = resolve_path(rel_resolved, project)
                is_instruction = (
                    ("给后端" in dest.name or "给前端" in dest.name)
                    and dest.suffix == ".md"
                    and "索引" not in dest.name
                )
                if is_instruction:
                    content = enforce_output_limits(content, ROLE, dest.name, LIMIT_CHARS)
                write_output_atomic(dest, content)
                print(f"[{ROLE}] 写入: {dest}")
                written.append(rel_resolved)

        # 后端轮成功后写 marker（前端轮超时 retry 时复用已有产出）
        if pass_name == "后端任务":
            try:
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text(f"done at {utc_now()}\nfiles: {len(written)}\n",
                                  encoding="utf-8")
            except OSError as e:
                print(f"[{ROLE}] ⚠️ 写 backend_done marker 失败（{e}），retry 时会重跑后端轮",
                      file=sys.stderr)

    # 整轮成功，清理 marker + plan 缓存（下次重跑自然失效）
    for p in (marker, _plan_cache_path(project)):
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass

    set_role_status(ROLE, status="success", reset_counters=True)
    set_role_status(ROLE, status="idle")
    append_audit({
        "timestamp": utc_now(), "role": ROLE, "project": project,
        "task": task, "result": "success", "outputs": written,
    })
    print(f"[{ROLE}] 完成，输出：{written}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
