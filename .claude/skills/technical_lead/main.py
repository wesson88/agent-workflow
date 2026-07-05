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
sys.path.insert(0, str(Path(__file__).resolve().parent))  # 让 sibling-file 如 _module_manifest_prompt 可 import

from common import (
    parse_args, resolve_project, build_system_prompt, read_input_files,
    write_output_atomic, parse_claude_output_to_files,
    call_claude, append_audit, utc_now, render_required_outputs,
    enforce_output_limits, load_rule_block,
)
from prompt_builder import build_system_prompt_no_skills, build_task_skill_block
from _module_manifest_prompt import build_module_manifest_user_prompt
from engine import (
    set_role_status, role_is_blocked,
    project_dir, rules_dir, resolve_path,
    VAULT_ROOT,
    load_role, RoleNotFound,
)
from engine.llm import call_llm
from engine.wikilink import parse_wikilinks

ROLE = "技术主管"

_VALID_PROJECT_TYPES = ("backend-only", "frontend-only", "full-stack")

# 脑暴笔记轮次标题：`### 第 N 轮 · 角色名`
_ROUND_HEADING_RE = re.compile(r"^### 第\s*(\d+)\s*轮\b", re.MULTILINE)

# 末轮决议合计上限（多份脑暴拼接后的总体积，防止极端情况打爆 prompt）
_DISCUSSION_LAST_ROUND_MAX_CHARS = 30 * 1024

# §3.2 章节级输入：TL 派活只需系统设计中「模块结构 / 接口契约 / 错误处理 / 任务约束 / 技术映射」。
# 跨项目章节命名差异大（3 样本观测：pain-radar / mini-ledger / _quiz-game + 2026-06-09 重跑），
# 用关键词 any-match 兜底；全部未命中时 _extract_sections 回退全文 + warning，兼容历史项目。
# 单文件均值 ~14-18KB → 章节裁剪后 ~5-9KB，单次省 ~3-5K input tokens。
_SYS_DESIGN_SECTIONS_FOR_TL = [
    "模块划分", "服务边界", "业务域", "业务领域",     # 模块结构（业务域 / 领域驱动两种说法）
    "前后端模块", "模块依赖", "目录布局",            # 模块边界 / 目录约定
    "数据流", "数据模型",                            # 数据契约
    "API 契约", "API契约", "接口", "外部依赖",       # 接口契约（含外部依赖与契约）
    "错误处理", "韧性", "失败模式", "降级",          # 错误处理（多命名变体）
    "任务粒度", "交付下游", "关键约束",              # 任务约束
    "技术栈映射", "技术栈",                          # 技术约束
]


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


def _done_marker(side: str) -> Path:
    """任务轮 done marker：子进程超时 retry 时跳过已成功的轮次。

    side ∈ {"后端", "前端"}，前后端各维护独立 marker，互不影响。
    """
    return VAULT_ROOT / "00-系统" / ".runtime-state" / f"技术主管.{side}_done"


def _plan_cache_path(project: str, side: str) -> Path:
    """Plan call 结果缓存：subprocess retry 时跳过重新调 LLM。

    缓存键含 project + side，多项目/前后端并行不互相覆盖。
    对应轮次整轮成功后清理（见 main 末尾 marker 清理处）。
    """
    return VAULT_ROOT / "00-系统" / ".runtime-state" / f"技术主管.plan-{side}-{project}.json"


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

# ── 二次拆分阈值 ────────────────────────────────────────────────────────────
_DETAIL_SPLIT_CHARS = 8_000   # 单 detail 文件正文超此体积触发二次拆分
_DETAIL_SPLIT_HOURS = 4       # estimate_hours 超此阈值触发二次拆分
_SPLIT_MODEL = "claude-haiku-4-5"  # 二次拆分用轻量模型，节约成本


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


def _split_oversized_detail(
    system_prompt: tuple[str, str],
    base_prompt: str,
    project: str,
    proj_dir: Path,
    side: str,
    tid: str,
    title: str,
    original_content: str,
    written: list[str],
) -> list[str]:
    """用轻量模型把一个过大/过长的 detail 文件二次拆分为多个子任务文件。

    side ∈ {"后端", "前端"}；函数同时负责更新对应侧的索引文件。
    返回新写入的相对路径列表（不含原 tid 路径，已由调用方跳过写盘）。
    拆分失败时返回空列表，调用方继续使用原 content 写盘。
    """
    prefix = f"给{side}"
    sub_ids = [f"{tid}a", f"{tid}b", f"{tid}c", f"{tid}d"]
    sub_targets = "\n".join(
        f"  `10-项目/{project}/指令/{prefix}-{sid}.md`" for sid in sub_ids[:2]
    ) + f"\n  ...（最多拆成 {tid}a / {tid}b / {tid}c / {tid}d，视实际工作量）"

    split_prompt = (
        base_prompt
        + f"## 需要二次拆分的任务：{tid} — {title}\n\n"
        + "以下是该任务的**原始细节文档**（已超过体积或工时阈值，需要拆分）：\n\n"
        + f"```markdown\n{original_content[:6000]}\n```\n\n"
        + "请把上述任务拆分为 **2-4 个子任务**，每个子任务工作量 ≤ 4 小时。\n"
        + "每个子任务输出一个 FILE 块，命名规则：\n"
        + sub_targets + "\n\n"
        + "每个子任务文件 frontmatter **必须**包含以下字段（将用于索引 patch）：\n"
        + "```yaml\n"
        + "---\n"
        + f"type: task-detail\ntask_id: {tid}a\ntitle: <子任务标题>\n"
        + "role: " + ("后端工程师" if side == "后端" else "前端工程师") + "\n"
        + f"from: 技术主管\nto: {side}工程师\n"
        + "estimate_hours: 2\ndepends_on: []\nunblocks: []\ncreated: <date>\n"
        + "---\n"
        + "```\n\n"
        + "功能描述、接口/模块、输入输出、验收标准各写一节。\n"
        + "**只输出 FILE 块，不要对话性文字，不要重复原索引文件。**\n"
    )
    print(f"[{ROLE}] ✂️ 二次拆分 {tid}（体积/工时超阈值）→ 子任务，使用 {_SPLIT_MODEL}...")
    try:
        split_raw = call_llm(system_prompt, split_prompt, model=_SPLIT_MODEL, max_tokens=2048)
    except Exception as e:
        print(f"[{ROLE}] ⚠️ 二次拆分 call 失败 ({tid}): {e}，保留原文件", file=sys.stderr)
        return []

    sub_files = parse_claude_output_to_files(split_raw)
    if not sub_files:
        print(f"[{ROLE}] ⚠️ 二次拆分无 FILE 输出 ({tid})，保留原文件", file=sys.stderr)
        return []

    LIMIT_CHARS = 30 * 1024
    new_written: list[str] = []
    # 顺序收集子任务 frontmatter 信息，供索引 patch 使用
    sub_meta: list[dict] = []  # [{id, title, estimate_hours, depends_on}]

    for rel_path, content in sub_files.items():
        rel_resolved = rel_path.replace("{project}", project)
        dest = resolve_path(rel_resolved, project)
        content = enforce_output_limits(content, ROLE, dest.name, LIMIT_CHARS)
        write_output_atomic(dest, content)
        print(f"[{ROLE}] ✅ 二次拆分子任务写入: {dest}")
        new_written.append(rel_resolved)

        # 从写入内容解析 frontmatter，补充索引行信息
        fm = _parse_frontmatter_fields(content, ("task_id", "title", "estimate_hours", "depends_on"))
        sub_meta.append({
            "id": fm.get("task_id") or re.search(rf"{re.escape(prefix)}-(.+?)\.md$", rel_resolved).group(1) if re.search(rf"{re.escape(prefix)}-(.+?)\.md$", rel_resolved) else tid + "?",
            "title": fm.get("title") or "（子任务）",
            "hours": fm.get("estimate_hours") or "-",
            "depends": fm.get("depends_on") or "[]",
        })

    # ── 精准 patch 索引文件：把旧 tid 行替换为真实子任务行 ──────────
    index_path = proj_dir / "指令" / f"{prefix}-索引.md"
    if new_written and index_path.exists():
        try:
            index_text = index_path.read_text(encoding="utf-8")
            sub_rows = "\n".join(
                f"| {m['id']} | {m['title']} | {m['hours']}h | {m['depends']} |"
                for m in sub_meta
            )
            patched = re.sub(
                r"\|\s*" + re.escape(tid) + r"\s*\|[^\n]*",
                sub_rows,
                index_text,
                count=1,
            )
            if patched != index_text:
                write_output_atomic(index_path, patched)
                print(f"[{ROLE}] 📋 {prefix}-索引 patch：{tid} → {[m['id'] for m in sub_meta]}")
            else:
                print(f"[{ROLE}] ℹ️ {prefix}-索引中未找到 {tid} 表格行，跳过 patch",
                      file=sys.stderr)
        except OSError as e:
            print(f"[{ROLE}] ⚠️ 索引 patch 失败 ({e})，索引可能不同步", file=sys.stderr)

    return new_written


_FM_FIELD_RE = re.compile(r"^(\w+):\s*(.+)$", re.MULTILINE)


def _parse_frontmatter_fields(content: str, fields: tuple[str, ...]) -> dict[str, str]:
    """从 markdown 文本中提取 frontmatter 指定字段，返回 {field: value} dict。

    只取第一个 `---...---` 块；找不到字段时对应 key 不出现在结果中。
    """
    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
    if not fm_match:
        return {}
    body = fm_match.group(1)
    result: dict[str, str] = {}
    for m in _FM_FIELD_RE.finditer(body):
        key, val = m.group(1), m.group(2).strip()
        if key in fields:
            result[key] = val
    return result


# ── Plan 任务规整 + 索引模板（2026-05-21 方案 A）────────────────────────
# 背景：mini-ledger 实战中 LLM 在 index_md_body JSON 字符串里写未转义 ASCII `"`
# 导致 Plan call JSON 解析失败 → 回退单 call 又被 max_tokens 截断丢任务。
# 修法：Plan 只让 LLM 输出 tasks[]（含 depends_on），索引文件由编排引擎模板生成。

def _normalize_task(task: object) -> dict | None:
    """规整 Plan call 输出的单个任务条目；返回 None 表示该条目无效应被跳过。

    容忍：depends_on 是字符串 "T01, T02" 时自动拆为 list；estimate_hours 是
    字符串数字时自动转 int；任何字段类型不对都降级为合理默认值。
    必填：id + title，缺一个则视为无效任务。
    """
    if not isinstance(task, dict):
        return None
    tid = str(task.get("id") or "").strip()
    title = str(task.get("title") or "").strip()
    if not tid or not title:
        return None
    summary = str(task.get("summary") or "").strip()
    try:
        hours = int(task.get("estimate_hours") or 0)
    except (TypeError, ValueError):
        hours = 0

    raw_deps = task.get("depends_on")
    if isinstance(raw_deps, list):
        deps = [str(d).strip() for d in raw_deps if str(d).strip()]
    elif isinstance(raw_deps, str):
        # LLM 偶尔不遵守 schema 给 "T01, T02" 字符串，宽松拆分
        deps = [s.strip() for s in raw_deps.split(",") if s.strip()]
    else:
        deps = []

    return {
        "id": tid,
        "title": title,
        "summary": summary,
        "estimate_hours": hours,
        "depends_on": deps,
    }


def _render_index_md(
    side: str,
    project: str,
    tasks: list[dict],
    skip_reason: str = "",
) -> str:
    """根据规整后的任务列表生成「给{side}-索引.md」正文。

    tasks=[] 时输出 "# 无{side}任务" stub（含 LLM 给出的 skip_reason）。
    决议速查表不在此渲染（决议归 `脑暴-*.md` / `系统设计.md`，索引保持薄）。
    保持 `| id | 标题 | 工时(h) | depends_on |` 列格式以兼容 _split_oversized_detail 的索引 patch 正则。
    """
    role_name = "后端工程师" if side == "后端" else "前端工程师"
    date_str = utc_now()[:10]  # YYYY-MM-DD

    if not tasks:
        reason = skip_reason or "（LLM 判定本项目无该侧业务，未给出具体理由）"
        return (
            f"---\n"
            f"type: task-index\n"
            f"project: {project}\n"
            f"role: {role_name}\n"
            f"status: skipped\n"
            f"generated: {date_str}\n"
            f"---\n\n"
            f"# 无{side}任务\n\n"
            f"**判定理由**：{reason}\n\n"
            f"若判定有误，请改「给技术主管.md」frontmatter 中的 `project_type` 字段，"
            f"或重跑 `--start-from 技术主管`。\n"
        )

    rows: list[str] = []
    for t in tasks:
        deps_text = ", ".join(t["depends_on"]) if t["depends_on"] else "—"
        rows.append(f"| {t['id']} | {t['title']} | {t['estimate_hours']} | {deps_text} |")
    rows_text = "\n".join(rows)
    total_hours = sum(t["estimate_hours"] for t in tasks)
    task_count = len(tasks)
    detail_links = " / ".join(f"[[给{side}-{t['id']}]]" for t in tasks[:5])
    if len(tasks) > 5:
        detail_links += " / …"

    return (
        f"---\n"
        f"type: task-index\n"
        f"project: {project}\n"
        f"role: {role_name}\n"
        f"status: ready\n"
        f"generated: {date_str}\n"
        f"---\n\n"
        f"# {project} {side}任务索引\n\n"
        f"> 共 {task_count} 个任务，总工时 {total_hours} h。\n"
        f"> 详细任务规格见：{detail_links}\n"
        f"> 架构决议详见 [[系统设计]] 及 [[脑暴-架构评审]]，本索引不复述。\n\n"
        f"## 任务清单\n\n"
        f"| id | 标题 | 工时(h) | depends_on |\n"
        f"|----|------|---------|------------|\n"
        f"{rows_text}\n\n"
        f"**总工时：{total_hours} h**\n"
    )


def _run_pass_split(
    side: str,
    system_prompt: tuple[str, str],
    base_prompt: str,
    project: str,
    proj_dir: Path,
) -> tuple[bool, list[str]]:
    """通用 Plan + Detail × N 拆分，支持后端/前端两侧。

    side ∈ {"后端", "前端"}。
    返回 (success, written_relpaths)。
    success=False 表示 Plan 解析失败或 detail 调用失败，调用方应回退原单 call。
    written_relpaths 已成功落盘的相对路径（即使最终 False，索引已写入也会在列表里）。
    """
    LIMIT_CHARS = 30 * 1024
    model = _resolve_role_model()
    prefix = f"给{side}"
    role_name = "后端工程师" if side == "后端" else "前端工程师"

    # ── Plan 缓存命中：subprocess retry 时跳过 LLM 重调 ────
    plan_cache = _plan_cache_path(project, side)
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

    plan = cached_plan

    # ── Plan call：只列任务大纲（索引正文由引擎模板生成）─────
    # 方案 A（2026-05-21）：剥离 index_md_body 字段。让 LLM 在 JSON 字符串里
    # 写整段 markdown 太脆弱（未转义 `"` 即崩），索引交给模板渲染更稳。
    plan_prompt = base_prompt + (
        f"**本轮只列{side}任务大纲（不写任务细节、不写索引正文 — 索引由编排引擎模板生成）**。"
        f"请输出**单个 JSON 块**：\n\n"
        "```json\n"
        "{\n"
        '  "tasks": [\n'
        '    {"id": "T01", "title": "...", "summary": "1-3 句，含核心交付 + 关键依赖 + [[skill]] wikilink", '
        '"estimate_hours": 3, "depends_on": []},\n'
        '    {"id": "T02", "title": "...", "summary": "...", '
        '"estimate_hours": 2, "depends_on": ["T01"]}\n'
        "  ]\n"
        "}\n"
        "```\n\n"
        f"**无{side}业务时的合法出口**：`{{\"tasks\": [], "
        f"\"skip_reason\": \"<≤ 100 字的判定理由>\"}}`。\n"
        "约束：\n"
        f"- 任务 id 形如 `T01`/`T02`/`T05a`/`T05b`，对应文件 `10-项目/{project}/指令/{prefix}-{{id}}.md`\n"
        "- JSON 必须可被 `json.loads` 解析；**字符串值内不要嵌套 markdown 或 ``` 围栏**\n"
        "- estimate_hours 为整数（1-8），超过 4 小时的任务**必须**在 summary 中说明拆法\n"
        "- depends_on 是任务 id 数组（如 `[\"T01\", \"T02\"]`），无依赖填 `[]`，**不要写成字符串**\n"
        "- 不需要复述架构决议 / 依赖图 / 速查表（引擎模板会处理表头与统计行）\n"
        "\n"
        "**派活 skill wikilink 约束（B1 D4 推广）**：\n"
        f"- 在每个任务的 `summary` 字段里**显式写出**该任务依赖的 skill `[[skill 文件名]]` wikilink\n"
        "- skill wikilink **只能从上文 §6 工程参考 skill 索引（F-技术主管 / F-后端 / F-前端）中挑**\n"
        f"- 命名规约：`TL<N>-标题` 技术主管域 / `B<N>-标题` 后端域 / `F<N>-标题` 前端域\n"
        "- **禁止编造文件名**——不在索引里出现的 wikilink 一律不写\n"
        "- 不确定时优先不写 wikilink（下游有 keyword 触发器兜底）\n"
        "- summary 示例：`T01 实现用户登录 API，含 POST /login + JWT 签发，依赖 [[B7-FastAPI共享资源async]] + [[B5-空集守卫]]`\n"
    )

    if plan is None:
        print(f"[{ROLE}] 📋 {side} Plan call（任务大纲，max_tokens=2048）...")
        try:
            plan_raw = call_llm(system_prompt, plan_prompt, model=model, max_tokens=2048)
        except Exception as e:
            print(f"[{ROLE}] ⚠️ {side} Plan call 异常：{e}，回退单 call", file=sys.stderr)
            return False, []

        try:
            plan_json_str = _extract_json_block(plan_raw)
            plan = json.loads(plan_json_str)
        except Exception as e:
            print(f"[{ROLE}] ⚠️ {side} Plan JSON 解析失败：{e}，回退单 call", file=sys.stderr)
            return False, []

        # 缓存 plan
        try:
            plan_cache.parent.mkdir(parents=True, exist_ok=True)
            plan_cache.write_text(
                json.dumps(plan, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"[{ROLE}] 💾 {side} Plan 缓存已落盘: {plan_cache.name}")
        except OSError as e:
            print(f"[{ROLE}] ⚠️ {side} Plan 缓存写入失败（{e}），retry 时会重跑 Plan call",
                  file=sys.stderr)

    tasks_raw = plan.get("tasks")
    if not isinstance(tasks_raw, list):
        print(f"[{ROLE}] ⚠️ {side} Plan tasks 非 list（{type(tasks_raw).__name__}），回退单 call",
              file=sys.stderr)
        return False, []

    # 规整 + 过滤无效任务（缺 id/title 直接 skip，宽松接受 depends_on 字符串）
    tasks_clean: list[dict] = []
    for raw in tasks_raw:
        norm = _normalize_task(raw)
        if norm is None:
            print(f"[{ROLE}] ⚠️ 跳过无效任务条目：{raw!r}", file=sys.stderr)
            continue
        tasks_clean.append(norm)

    skip_reason = str(plan.get("skip_reason") or "").strip()
    written: list[str] = []

    # ── 写索引文件（模板生成，不再读 LLM 的 index_md_body）─────
    index_md = _render_index_md(side, project, tasks_clean, skip_reason)
    index_dest = proj_dir / "指令" / f"{prefix}-索引.md"
    index_md = enforce_output_limits(index_md, ROLE, index_dest.name, LIMIT_CHARS)
    write_output_atomic(index_dest, index_md)
    written.append(f"10-项目/{project}/指令/{prefix}-索引.md")
    print(f"[{ROLE}] ✅ {side}索引写入（模板生成）: {index_dest}")

    if not tasks_clean:
        print(f"[{ROLE}] ℹ️ {side} Plan tasks 为空（无{side}业务），跳过 detail call")
        return True, written

    # ── Detail call × N：逐个任务细化 ──────────────────────
    task_summary_block = "\n".join(
        f"- {t['id']}: {t['title']} — {t['summary']}"
        for t in tasks_clean
    )

    for task in tasks_clean:
        tid = task["id"]
        title = task["title"]
        summary = task["summary"]
        estimate_hours = task["estimate_hours"]

        rel_target = f"10-项目/{project}/指令/{prefix}-{tid}.md"
        # subprocess retry 友好：已存在的 detail 直接跳过
        dest_check = resolve_path(rel_target, project)
        if dest_check.exists() and dest_check.stat().st_size > 200:
            print(f"[{ROLE}] ⏩ {side} Detail ({tid}) 已存在 "
                  f"({dest_check.stat().st_size} chars)，跳过重跑")
            written.append(rel_target)
            continue

        # ── skill_refs 动态裁剪：解析 summary wikilink，注入当前任务相关 skill ─
        # 扫描 summary 里的 [[BN-xxx]] / [[FN-xxx]] wikilink，只注入命中的 skill，
        # 把每 detail call 的 system prompt 体积从全量降到只含相关技能。
        task_wikilinks = [wl.target for wl in parse_wikilinks(summary)]
        skill_block = build_task_skill_block(task_wikilinks, side) if task_wikilinks else ""
        if skill_block:
            # 将 skill 块注入 system prompt dynamic 段
            detail_system_prompt = (system_prompt[0], system_prompt[1] + "\n\n" + skill_block)
        else:
            detail_system_prompt = system_prompt
        detail_prompt = base_prompt + (
            f"## 本轮{side}任务清单（参考，便于校准依赖与不重不漏）\n"
            f"{task_summary_block}\n\n---\n\n"
            f"## 本次只产出**一个{side}任务**的细节：{tid} — {title}\n\n"
            f"摘要：{summary}\n\n"
            "文件 frontmatter **必须**包含以下字段：\n"
            "```yaml\n"
            "---\n"
            "type: task-detail\n"
            f"task_id: {tid}\n"
            f"title: {title}\n"
            f"role: {role_name}\n"
            "from: 技术主管\n"
            f"to: {role_name}\n"
            "estimate_hours: <整数>\n"
            "depends_on: []\n"
            "unblocks: []\n"
            "created: <date>\n"
            "---\n"
            "```\n"
            "正文包含：功能描述、模块/接口、输入输出、验收标准\n"
            "- 工作量 ≤ 4 小时；若已是二次拆分子任务请在描述中注明\n"
            f"**只输出这一个 FILE 块，不要重复索引、不要输出其它任务文件，"
            "不要在 FILE 块外写任何对话性文字**。\n"
            + render_required_outputs([rel_target])
        )

        print(f"[{ROLE}] 📝 {side} Detail call: {tid} — {title}（max_tokens=1536）...")
        try:
            detail_raw = call_llm(
                detail_system_prompt, detail_prompt,
                model=model, max_tokens=1536,
            )
        except Exception as e:
            print(f"[{ROLE}] ❌ {side} Detail call 失败 ({tid}): {e}", file=sys.stderr)
            return False, written

        output_files = parse_claude_output_to_files(detail_raw)
        if not output_files:
            dest = resolve_path(rel_target, project)
            if dest.exists() and dest.stat().st_size > 200:
                print(
                    f"[{ROLE}] ℹ️ {side} Detail ({tid}) 未走 FILE 块但目标文件已存在 "
                    f"({dest.stat().st_size} chars)，疑似 LLM 工具产出，保留: {dest}",
                    file=sys.stderr,
                )
                written.append(rel_target)
                continue
            content = enforce_output_limits(detail_raw, ROLE, dest.name, LIMIT_CHARS)
            write_output_atomic(dest, content)
            print(f"[{ROLE}] ⚠️ {side} Detail ({tid}) 无 FILE 标签，raw 直写: {dest}",
                  file=sys.stderr)
            written.append(rel_target)
            continue

        for rel_path, content in output_files.items():
            rel_resolved = rel_path.replace("{project}", project)
            dest = resolve_path(rel_resolved, project)
            is_instruction = (
                (f"给{side}" in dest.name)
                and dest.suffix == ".md"
                and "索引" not in dest.name
            )
            if is_instruction:
                content = enforce_output_limits(content, ROLE, dest.name, LIMIT_CHARS)

            # ── 二次拆分触发：体积 或 工时超阈值 ─────────────────────────
            needs_split = is_instruction and (
                len(content) > _DETAIL_SPLIT_CHARS
                or estimate_hours > _DETAIL_SPLIT_HOURS
            )
            if needs_split:
                print(
                    f"[{ROLE}] ⚠️ {dest.name} 触发二次拆分 "
                    f"（体积={len(content)} chars, estimate_hours={estimate_hours}）"
                )
                sub_written = _split_oversized_detail(
                    system_prompt, base_prompt, project, proj_dir,
                    side, tid, title, content, written,
                )
                if sub_written:
                    written.extend(sub_written)
                    print(f"[{ROLE}] ✅ 二次拆分完成 {tid} → {sub_written}")
                    continue
                else:
                    print(f"[{ROLE}] ⚠️ 二次拆分失败，回退写原文件: {dest.name}",
                          file=sys.stderr)

            write_output_atomic(dest, content)
            print(f"[{ROLE}] 写入: {dest}")
            written.append(rel_resolved)

    return True, written


# 向后兼容别名（旧 marker 路径 → 新统一接口）
def _run_backend_pass_split(
    system_prompt: tuple[str, str],
    base_prompt: str,
    project: str,
    proj_dir: Path,
) -> tuple[bool, list[str]]:
    """向后兼容包装，实际调用通用 _run_pass_split('后端', ...)。"""
    return _run_pass_split("后端", system_prompt, base_prompt, project, proj_dir)


def _run_module_manifest_mode(
    *,
    project: str,
    task: str,
    proj_dir: Path,
    system_prompt: tuple[str, str],
    base_prompt: str,
) -> int:
    """P8.7 module_manifest 分支：单 call 产出 模块清单.md + 模块/{id}-{slug}.md。

    与 legacy_directives 分支的差异：
    - 单 call 产出（不走 Plan+Detail×N；模块清单.md 本身就是 Plan 结构）
    - 产出 `模块清单.md` + `模块/{id}-{slug}.md`（一次给完，靠 FILE 块）
    - 前后端综合拆解（yaml node.role 区分），不看 project_type

    fail 路径：
    - LLM 未返回 FILE 块 → 落 raw dump 到 `指令/技术主管-raw-dump.md` + failed
    - 缺关键产物 `模块清单.md` → failed（下游 manifest_validator 会 fail-closed）
    """
    from common import warn_if_no_files

    LIMIT_CHARS = 30 * 1024
    user_prompt = build_module_manifest_user_prompt(project, base_prompt)

    print(f"[{ROLE}] 📝 单 call 产出模块清单 + 模块详情（module_manifest 模式）...")
    try:
        raw_output = call_claude(system_prompt, user_prompt, ROLE)
    except Exception as e:
        print(f"[{ROLE}] Claude API 调用失败：{e}", file=sys.stderr)
        set_role_status(
            ROLE, status="failed",
            increment_consecutive_failures=True, increment_error=True,
            enforce_transition=False,
        )
        append_audit({
            "timestamp": utc_now(), "role": ROLE, "project": project,
            "task": task, "result": "failed",
            "mode": "module_manifest", "error": str(e),
        })
        return 1

    output_files = parse_claude_output_to_files(raw_output)
    if not output_files:
        warn_if_no_files(raw_output, ROLE)
        dump = proj_dir / "指令" / "技术主管-raw-dump.md"
        write_output_atomic(dump, raw_output)
        set_role_status(
            ROLE, status="failed",
            increment_consecutive_failures=True, increment_error=True,
            enforce_transition=False,
        )
        append_audit({
            "timestamp": utc_now(), "role": ROLE, "project": project,
            "task": task, "result": "failed",
            "mode": "module_manifest", "error": "no_file_blocks",
            "raw_dump": str(dump),
        })
        return 3

    written: list[str] = []
    manifest_written = False
    for rel_path, content in output_files.items():
        rel_resolved = rel_path.replace("{project}", project)
        dest = resolve_path(rel_resolved, project)
        rel_posix = rel_resolved.replace("\\", "/")
        # 模块清单不压缩（yaml block 结构必须保真）；模块详情走 LIMIT_CHARS
        if dest.name == "模块清单.md":
            enforced = content
            manifest_written = True
        elif "/模块/" in rel_posix:
            enforced = enforce_output_limits(content, ROLE, dest.name, LIMIT_CHARS)
        else:
            enforced = content
        write_output_atomic(dest, enforced)
        print(f"[{ROLE}] 写入: {dest}")
        written.append(rel_resolved)

    if not manifest_written:
        print(
            f"[{ROLE}] ❌ 未产出模块清单.md（LLM 输出了 {len(output_files)} 个 FILE 块但缺关键产物）",
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
            "mode": "module_manifest", "error": "manifest_missing",
            "written": written,
        })
        return 4

    set_role_status(ROLE, status="success", reset_counters=True)
    set_role_status(ROLE, status="idle")
    append_audit({
        "timestamp": utc_now(), "role": ROLE, "project": project,
        "task": task, "result": "success",
        "mode": "module_manifest", "outputs": written,
    })
    print(f"[{ROLE}] 完成（module_manifest 模式），输出：{written}")
    return 0


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
    # Plan+Detail 拆分使用不含 skill_refs 全量内联的版本，skill 按任务动态注入
    system_prompt_no_skills = build_system_prompt_no_skills(ROLE, project=project)

    # Phase 4c-3 + 2026-05-16 token 优化：脑暴笔记只读末轮裁决段，
    # 前几轮 critic/PM/前端发言对 TL 派活无价值，剥除节约 ~5K tokens。
    discussion_logs = sorted((proj_dir).glob("脑暴-*.md")) if proj_dir.is_dir() else []
    # §3.2 章节级输入：系统设计走 sections 裁剪（关键词清单见 _SYS_DESIGN_SECTIONS_FOR_TL）。
    # to_lead 是架构师专门写给 TL 的精简版、tech_stack 是全局规则，不做章节裁剪。
    sys_design_entry = {"path": sys_design, "sections": _SYS_DESIGN_SECTIONS_FOR_TL}
    inputs = [to_lead, sys_design_entry, tech_stack]
    context = read_input_files(inputs)

    # §3.1 rule_refs 章节级注入：frontmatter rule_refs 声明的 F-* 索引段（TL 派活
    # 需看到下游后端/前端可用 skill 清单）。音乐域 commit 77d1244 同思路；架构师
    # 仍用本地 _load_rule_block（实战 5+ 项目，保稳定不切换）。
    role_def = load_role(ROLE)
    rule_block, source_hint = load_rule_block(role_def.rule_refs)
    print(f"[{ROLE}] rule_refs 注入：{source_hint}")
    if rule_block:
        context = context + "\n\n" + rule_block

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

    # ── artifacts_pattern 分支：module_manifest 走单 call 模式（P8.7）─────
    # 契约通过 env AGENT_CONTRACT_OVERRIDES 从 workflow 层透传
    from common import _read_env_contract_overrides
    _overrides = _read_env_contract_overrides() or {}
    _artifacts_pattern = (
        (_overrides.get("output_contract") or {}).get(
            "artifacts_pattern", "legacy_directives"
        )
    )
    if _artifacts_pattern == "module_manifest":
        return _run_module_manifest_mode(
            project=project, task=task, proj_dir=proj_dir,
            system_prompt=system_prompt, base_prompt=base_prompt,
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

    # 读取「给技术主管.md」frontmatter 中的 project_type，驱动对称跳过
    project_type, type_source = _read_project_type(to_lead)
    print(f"[{ROLE}] 🏷️ project_type={project_type}（来源：{type_source}）")
    if type_source == "default_full_stack":
        print(f"[{ROLE}] ℹ️ 「给技术主管.md」frontmatter 未声明 project_type，"
              f"默认按 full-stack 跑两轮。若实际为单端项目，请改 frontmatter "
              f"加 `project_type: backend-only` 或 `frontend-only` 后重跑。")

    for side in ("后端", "前端"):
        user_prompt = backend_prompt if side == "后端" else frontend_prompt

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

        # ── done marker 检测：子进程超时 retry 时跳过已成功轮次 ────────
        marker = _done_marker(side)
        side_index = proj_dir / "指令" / f"给{side}-索引.md"
        existing_tasks = sorted((proj_dir / "指令").glob(f"给{side}-T*.md"))
        if marker.exists() and side_index.exists() and existing_tasks:
            print(f"[{ROLE}] ⏩ {side}轮已完成（marker 存在 + {len(existing_tasks)} 个任务卡），跳过重跑")
            for p in [side_index, *existing_tasks]:
                written.append(f"10-项目/{project}/指令/{p.name}")
            continue

        # ── 优先 Plan + Detail × N（2026-05-16 治理）─────────────────
        # 前后端均走拆分路径，避免单次大 call 在 CLI 子进程模式死锁。
        ok, new_files = _run_pass_split(
            side, system_prompt_no_skills, base_prompt, project, proj_dir,
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
                print(f"[{ROLE}] ⚠️ 写 {side}_done marker 失败（{e}），retry 时会重跑",
                      file=sys.stderr)
            continue

        # Plan+Detail 失败，回退原单 call
        print(
            f"[{ROLE}] ⚠️ {side} Plan/Detail 路径失败（已写入 {len(new_files)} 个文件，"
            f"将由单 call 覆盖重写），回退原单 call",
            file=sys.stderr,
        )

        print(f"[{ROLE}] 📝 生成{side}任务（单 call 兜底）...")
        try:
            raw_output = call_claude(system_prompt, user_prompt, ROLE)
        except Exception as e:
            print(f"[{ROLE}] Claude API 调用失败（{side}）：{e}", file=sys.stderr)
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
            fallback_name = f"给{side}.md"
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
                    (f"给{side}" in dest.name)
                    and dest.suffix == ".md"
                    and "索引" not in dest.name
                )
                if is_instruction:
                    content = enforce_output_limits(content, ROLE, dest.name, LIMIT_CHARS)
                write_output_atomic(dest, content)
                print(f"[{ROLE}] 写入: {dest}")
                written.append(rel_resolved)

        # 单 call 成功后也写 marker
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(f"done at {utc_now()}\nfiles: {len(written)}\nmode: single-call\n",
                              encoding="utf-8")
        except OSError as e:
            print(f"[{ROLE}] ⚠️ 写 {side}_done marker 失败（{e}），retry 时会重跑{side}轮",
                  file=sys.stderr)

    # 整轮成功，清理所有 marker + plan 缓存
    for p in (
        _done_marker("后端"), _done_marker("前端"),
        _plan_cache_path(project, "后端"), _plan_cache_path(project, "前端"),
    ):
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
