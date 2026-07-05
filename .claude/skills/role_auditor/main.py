"""
role_auditor/main.py — 角色审计器执行入口

作用：
  对照 vault `00-系统/规则/角色基因规范.md` 审计所有 `角色-*.md` 文件，
  输出可操作的偏离清单到 `00-系统/审计报告/角色基因审计-{date}.md`。

  程序层先做可量化测量（字符长度 / frontmatter 字段 / DYNAMIC regex），
  把测量结果连同规范 + 所有角色全文传给 LLM，LLM 负责语义判断（反模式 / 豁免）
  并产出最终报告。

  历史称呼：2026-06-10 前称"角色规范师"，对齐 engine `role_auditor` 改名为
  "角色审计器"；vault 角色基因 frontmatter aliases 保留旧名兼容历史复盘文档。

CLI：
  python .claude/skills/role_auditor/main.py [--dry-run] [--target X [--target Y]]
    --dry-run    只打印测量结果，不调 LLM、不写盘
    --target     治理对象选择（可重复 / 逗号分隔 / "all"）
                 - 不传 = 全部角色（除审计者本身）
                 - --target 后端工程师 = 单个
                 - --target 后端,前端 = 多个
"""

from __future__ import annotations

import argparse
import re
import sys
import yaml
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import (
    build_system_prompt, read_input_files,
    write_output_atomic, parse_claude_output_to_files,
    call_claude, append_audit, utc_now, parse_targets,
)
from engine import (
    set_role_status, role_is_blocked,
    VAULT_ROOT, role_genes_dir, resolve_path,
)

ROLE = "角色审计器"

# 不审计自身
SELF_FILENAME = "角色-角色审计器.md"

# 规范文档路径（vault 相对）
SPEC_REL = "00-系统/规则/角色基因规范.md"

# 报告输出目录
AUDIT_DIR_REL = "00-系统/审计报告"

# frontmatter 必填字段（来自规范 §2.1 / §2.2）
REQUIRED_FIELDS = {
    "role", "domain", "model", "max_tokens", "style",
    "aliases", "upstream", "downstream", "monitors",
    "inputs", "outputs", "tools",
}

# frontmatter 禁止字段（来自规范 §2.4）
FORBIDDEN_FIELDS = {
    "responsibilities", "职责", "forbidden", "禁止事项",
    "workflow", "description", "prompt_template",
}

# 长度上限（字符数）+ 数量上限
# ⚠️ 阈值来源硬约束（见项目 CLAUDE.md「阈值来源必须显式声明」）
LIMITS = {
    # 依据：**初值，无数据支持**。规范 §4 长度软上限章节沿用 5A-1（DYNAMIC 5000）
    # 与 role_auditor 首版实施同期设定，未做实测校准。~1400 tokens/5000 chars 是
    # 快估算（假设中文 3.5 chars/token）。等大型角色实战暴露具体膨胀点后校准。
    "frontmatter": 800,
    "body_no_dynamic": 5000,
    "single_section": 1500,
    "dynamic": 5000,
    "single_patch": 1200,
    # P6 新增：角色 skill_refs 数量软上限（触发 [SHRINK?]）
    # 依据：**推导逻辑 + 实测参考**。当前实测：架构师 5 skill / 后端 4 / TL 2 / 前端 1，
    # 上限 = 现最高值（架构师 5），高于此值提示治理域过宽应拆分。
    # 命中不阻塞 load_role，仅 LLM 报告标记。等音乐域 9+ 角色实战后再评估上调。
    "skill_refs_max": 5,
}


# P6 canonical skill 触发器 schema（对齐 skill_trigger.py::match_skill 读取契约）
# 一个 skill 文件的 frontmatter 至少要满足以下三条之一，否则 fail-closed 不召回：
#   - trigger.keywords ≥ 1 项
#   - trigger.file_patterns ≥ 1 项
#   - trigger.always: true
def _skill_trigger_valid(skill_fm: dict) -> bool:
    """判断 skill frontmatter 的 trigger 字段是否合法（能被 skill_trigger 召回）。

    对齐 `.claude/engine/skill_trigger.py::match_skill` 的读取逻辑；命名规范
    见 [[角色基因规范#§11]] / [[capability注册表机制-立项-2026-07-02#§11.4]]。
    """
    trigger = skill_fm.get("trigger") if isinstance(skill_fm, dict) else None
    if not isinstance(trigger, dict):
        return False
    if trigger.get("always") is True:
        return True
    keywords = trigger.get("keywords")
    if isinstance(keywords, list) and any(isinstance(k, str) and k.strip() for k in keywords):
        return True
    file_patterns = trigger.get("file_patterns")
    if isinstance(file_patterns, list) and any(
        isinstance(p, str) and p.strip() for p in file_patterns
    ):
        return True
    return False


# PM 角色 PRD.md 越界 pattern（来源：[[PM越界-PRD写下游内容]]）
# 命中表示 PM 越界写了应属架构师 / TL 的内容（schema / API 表 / 框架推荐 / 任务拆分）。
# pattern 设计取舍：宁可短期误报（如「待确认项」里的「Vue 还是 React」），命中后人工
# review；不放过实际越界。下个新项目实战后再精化。
PM_OVERFLOW_PATTERNS: tuple[tuple[str, str, str], ...] = (
    ("api_table_header",
     r"\|\s*方法\s*\|\s*路径\s*\|",
     "API endpoint 表头"),
    ("api_method_row",
     r"\|\s*(GET|POST|PUT|DELETE|PATCH)\s*\|\s*`?/[\w/{}:?=&_\-\.]+`?\s*\|",
     "API 方法/路径表格行（method | path 两栏）"),
    ("ddl_field",
     r"(INTEGER\s+PK|TEXT\s+NOT\s+NULL\s+UNIQUE|TEXT\s+NOT\s+NULL|REAL\s+NOT\s+NULL|VARCHAR\(\d+\))",
     "DDL 字段类型"),
    ("schema_table_header",
     r"\|\s*字段\s*\|\s*类型\s*\|",
     "schema 字段表头"),
    ("framework_choice",
     r"(Flask\s*(vs|/|或)\s*FastAPI|FastAPI\s*(vs|/|或)\s*Flask|React\s*(vs|/|或)\s*Vue|Vue\s*(vs|/|或)\s*React|Chart\.js\s*(vs|/|或)\s*ECharts)",
     "框架选型推荐"),
    ("task_split_header",
     r"\|\s*#?\s*\|\s*任务\s*\|\s*角色\s*\|",
     "任务拆分表头（# / 任务 / 角色）"),
    ("task_id_row",
     r"^\|\s*T\d+[a-z]?\s*\|.+\|\s*(后端|前端|架构)[\w\s\-]*\|",
     "T<n> 任务分派表格行"),
)


def _detect_pm_overflow(prd_path: Path) -> list[dict]:
    """正则扫 PRD.md，返回命中越界 pattern 的 hit 列表。

    每条 hit：{pattern_id, desc, line, snippet}。
    无 PRD.md（PM 尚未跑）或读取失败 → 返回空列表，不视为错误。
    """
    if not prd_path.is_file():
        return []
    try:
        content = prd_path.read_text(encoding="utf-8")
    except OSError as e:
        print(f"[{ROLE}] ⚠️ 读 {prd_path.name} 失败：{e}", file=sys.stderr)
        return []

    hits: list[dict] = []
    lines = content.splitlines()
    for pattern_id, regex, desc in PM_OVERFLOW_PATTERNS:
        pat = re.compile(regex, re.MULTILINE)
        for m in pat.finditer(content):
            # 算行号：从 match 起始位置反推
            line_no = content[: m.start()].count("\n") + 1
            snippet = lines[line_no - 1].strip() if line_no <= len(lines) else ""
            hits.append({
                "pattern_id": pattern_id,
                "desc": desc,
                "line": line_no,
                "snippet": snippet[:200],
            })
    return hits


def _run_pm_output_audit(*, dry_run: bool = False) -> int:
    """扫 vault 下所有 10-项目/*/PRD.md，命中越界即写 audit.jsonl + 递增 PM consecutive_failures。

    返回值（CLI 状态码）：
      0 — 所有 PRD 合规，无命中
      0 — 命中但非 dry_run，已记录（state 已递增）
      0 — dry_run，仅打印不写盘
      2 — 没有可审计的 PRD（vault 下 10-项目 为空）
    """
    projects_root = VAULT_ROOT / "10-项目"
    if not projects_root.is_dir():
        print(f"[{ROLE}] vault {projects_root} 不存在，跳过产物审计", file=sys.stderr)
        return 2

    prd_paths = sorted(projects_root.glob("*/PRD.md"))
    if not prd_paths:
        print(f"[{ROLE}] 未在 {projects_root} 下找到任何 PRD.md", file=sys.stderr)
        return 2

    overflow_count = 0
    total_hits = 0
    for prd in prd_paths:
        project = prd.parent.name
        hits = _detect_pm_overflow(prd)
        if not hits:
            print(f"[{ROLE}] ✅ {project}/PRD.md 合规（无越界 pattern 命中）")
            continue

        overflow_count += 1
        total_hits += len(hits)
        pattern_ids = sorted({h["pattern_id"] for h in hits})
        print(
            f"[{ROLE}] ⚠️ {project}/PRD.md 命中 {len(hits)} 个越界（{len(pattern_ids)} 类）：" + ", ".join(pattern_ids),
            file=sys.stderr,
        )
        for h in hits[:5]:
            print(f"    line {h['line']}  [{h['pattern_id']}]  {h['snippet']}", file=sys.stderr)
        if len(hits) > 5:
            print(f"    …（还有 {len(hits) - 5} 条，详见 audit.jsonl）", file=sys.stderr)

        if dry_run:
            continue

        # 写 audit.jsonl
        try:
            rel = prd.relative_to(VAULT_ROOT).as_posix()
        except ValueError:
            rel = str(prd)
        append_audit({
            "timestamp": utc_now(),
            "type": "pm_output_overflow",
            "role": "产品经理",
            "project": project,
            "prd_path": rel,
            "hit_count": len(hits),
            "patterns": pattern_ids,
            "hits": hits,
            "audited_by": ROLE,
        })

        # 递增 PM consecutive_failures（一次跑一次性 +1，不按 pattern 数累加）
        set_role_status(
            "产品经理",
            increment_consecutive_failures=True,
            enforce_transition=False,
        )

    print(
        f"[{ROLE}] 产物审计完成：扫 {len(prd_paths)} 个 PRD，"
        f"{overflow_count} 个越界，共 {total_hits} 处命中"
        + ("（dry_run，未写盘）" if dry_run else "")
    )
    return 0

_DYNAMIC_RE = re.compile(
    r"<!-- DYNAMIC_START -->(.*?)<!-- DYNAMIC_END -->",
    re.DOTALL,
)

_PATCH_HEADER_RE = re.compile(
    r"^#\s*Patch\s*\[([^\]]+)\]\s*\[([^\]]+)\]\s*(.+?)\s*$",
    re.MULTILINE,
)

# 章节标题 regex（§1-§8）
_SECTION_RE = re.compile(r"^##\s+(\d+)\.\s+", re.MULTILINE)

# vault stem 唯一性扫描排除路径前缀（与 engine.wikilink.resolve_target 的索引对齐）
# - 10-项目/<proj>/：项目产出 PRD/系统设计 等多项目同名是常态，命名规则豁免
# - 99-临时/：临时区不参与 wikilink，按 vault命名规则.md §2.9 豁免
# - .runtime-state/：运行时状态文件，不是 vault 笔记
_STEM_SCAN_EXCLUDES = ("10-项目/", "99-临时/", ".runtime-state/")


def _is_domain_rule_adapter(rel_posix: str) -> bool:
    """跨域适配器路径模板：`00-系统/规则/<domain>/<adapter>.md`（vault命名规则 §2.11）。

    与 engine.wikilink._is_domain_rule_adapter 同步：domain 子目录下的同名 stem
    是开闭原则的设计意图，stem 扫描应跳过。
    """
    parts = rel_posix.split("/")
    return (
        len(parts) >= 4
        and parts[0] == "00-系统"
        and parts[1] == "规则"
    )


def _parse_frontmatter(text: str) -> tuple[dict, str, int]:
    """从 markdown 文件提取 frontmatter。

    返回 (frontmatter_dict, body_text, frontmatter_char_count)。
    frontmatter_dict 为空表示无 frontmatter。
    """
    if not text.startswith("---"):
        return {}, text, 0
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text, 0
    fm_raw = text[3:end].strip()
    body = text[end + 4:].lstrip("\n")
    try:
        fm = yaml.safe_load(fm_raw) or {}
    except Exception:
        fm = {}
    return fm, body, len(text[:end + 4])


def _last_dynamic_body(text: str) -> str:
    ms = list(_DYNAMIC_RE.finditer(text))
    return ms[-1].group(1) if ms else ""


def _section_char_counts(body: str) -> dict[str, int]:
    """切 §1-§N 章节，返回每章字符数（不含标题行本身）。"""
    lines = body.split("\n")
    sections: dict[str, list[str]] = {}
    current = None
    for line in lines:
        m = _SECTION_RE.match(line)
        if m:
            current = m.group(1)
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    return {k: len("\n".join(v)) for k, v in sections.items()}


def _split_patches(dynamic_body: str) -> list[str]:
    """把 DYNAMIC 区域按 '# Patch' 行切成独立补丁块。"""
    chunks: list[str] = []
    buf: list[str] = []
    for line in dynamic_body.split("\n"):
        if line.strip().startswith("# Patch"):
            if buf:
                chunks.append("\n".join(buf))
            buf = [line]
        else:
            buf.append(line)
    if buf:
        chunks.append("\n".join(buf))
    return [c for c in chunks if c.strip()]


def _check_dynamic_marker_literal(body_no_dynamic: str) -> bool:
    """检查正文（不含 DYNAMIC 区域本身）是否字面引用了 DYNAMIC_START marker。

    规范 §6.4：字面引用（包括反引号包裹的 inline code）会破坏 regex 解析。
    """
    # 用 `` ` `` 包裹或直接出现的 DYNAMIC_START（但不是在 DYNAMIC 区域注释行里）
    return bool(re.search(r"`?<!--\s*DYNAMIC_START\s*-->`?", body_no_dynamic))


def _measure_role(path: Path) -> dict:
    """对单个角色文件做可量化测量，返回测量字典。"""
    text = path.read_text(encoding="utf-8")
    fm, body, fm_chars = _parse_frontmatter(text)

    dynamic_body = _last_dynamic_body(body)

    # 去掉 DYNAMIC 区域计算 body 长度
    body_no_dynamic = _DYNAMIC_RE.sub(
        "<!-- DYNAMIC_START --><!-- DYNAMIC_END -->", body
    )
    body_no_dynamic_chars = len(body_no_dynamic)

    section_chars = _section_char_counts(body_no_dynamic)
    max_section = max(section_chars.values(), default=0)
    max_section_id = max(section_chars, key=section_chars.get, default="?") if section_chars else "?"

    # 检查 DYNAMIC 区域内各 patch 大小
    patches = _split_patches(dynamic_body)
    oversized_patches = [
        (i + 1, len(p))
        for i, p in enumerate(patches)
        if len(p) > LIMITS["single_patch"]
    ]

    # 检查 patch 标题格式
    patch_titles = _PATCH_HEADER_RE.findall(dynamic_body)
    patch_count = len(patch_titles)

    # 检查 DYNAMIC marker 是否被字面引用（含转义版）
    marker_literal_in_body = _check_dynamic_marker_literal(
        _DYNAMIC_RE.sub("", body)  # 去掉 DYNAMIC 区域后扫正文
    )

    # frontmatter 字段检查
    present = set(fm.keys())
    missing_required = REQUIRED_FIELDS - present
    present_forbidden = FORBIDDEN_FIELDS & present

    # P6：skill_refs 数量 + 引用 skill 文件的 trigger 完整性
    # skill_refs 列表本身长度（软上限 LIMITS["skill_refs_max"]）
    skill_refs_raw = fm.get("skill_refs") if isinstance(fm, dict) else None
    if isinstance(skill_refs_raw, list):
        skill_refs_paths = [str(x).strip() for x in skill_refs_raw if x]
    elif isinstance(skill_refs_raw, str):
        skill_refs_paths = [skill_refs_raw.strip()] if skill_refs_raw.strip() else []
    else:
        skill_refs_paths = []
    skill_refs_count = len(skill_refs_paths)
    skill_refs_over_limit = skill_refs_count > LIMITS["skill_refs_max"]

    # 引用 skill 文件的 trigger 完整性：缺 trigger.keywords / file_patterns / always
    # 的 skill 会被 skill_trigger.discover_role_skills fail-closed 跳过；本 lint 提前
    # 暴露，防止"角色声明 skill_refs 但触发器机制静默失效"。
    skill_trigger_gaps: list[str] = []
    for rel in skill_refs_paths:
        skill_path = VAULT_ROOT / rel
        if not skill_path.is_file():
            # 缺文件与 §10.7 load_role fallback 一致：不 fail_closed，仅记录
            skill_trigger_gaps.append(f"{rel}（文件缺失）")
            continue
        try:
            skill_text = skill_path.read_text(encoding="utf-8")
        except OSError as e:
            skill_trigger_gaps.append(f"{rel}（读取失败：{e}）")
            continue
        skill_fm, _, _ = _parse_frontmatter(skill_text)
        if not _skill_trigger_valid(skill_fm):
            skill_trigger_gaps.append(f"{rel}（trigger 缺失或不完整）")

    # 章节序号检查
    found_sections = sorted(int(k) for k in section_chars if k.isdigit())

    # 是否有 DYNAMIC 标记
    has_dynamic_markers = "<!-- DYNAMIC_START -->" in text and "<!-- DYNAMIC_END -->" in text

    # 豁免判断
    is_meta = str(fm.get("domain", "")).strip() == "元"
    is_agent_generated = bool(fm.get("agent_generated", False))
    is_tiny = fm.get("role", "") in ("批判者", "用户体验者")

    # T2.7 白名单契约 lint
    # 业务角色（domain != 元）必须 §1-§6 完整 + §7 是运行时补丁 + §8 是版本历史
    # 元角色仅检查 DYNAMIC marker + 版本历史段存在
    # ⚠️ 直接扫原 body（不依赖 body_no_dynamic），避免 §6.4 DYNAMIC marker 滥用
    # 反模式（字面引用 marker 让 _DYNAMIC_RE 非贪婪匹配吞掉中间章节）干扰本 lint
    import re as _re
    _TOP_SECTION_RE = _re.compile(r"^##\s+(\d+)\.\s+(.+)$", _re.MULTILINE)
    top_sections: dict[int, str] = {}
    for m in _TOP_SECTION_RE.finditer(body):
        top_sections[int(m.group(1))] = m.group(2).strip()

    prompt_whitelist_issues: list[str] = []
    if is_meta:
        # 元角色：DYNAMIC marker 已在 has_dynamic_markers 检查；只查版本历史存在
        has_version = any("版本历史" in title for title in top_sections.values())
        if not has_version:
            prompt_whitelist_issues.append("元角色缺『版本历史』章节")
    else:
        # 业务角色严格 §1-§6 完整
        missing_business_sections = [n for n in range(1, 7) if n not in top_sections]
        if missing_business_sections:
            prompt_whitelist_issues.append(
                f"业务角色 §1-§6 缺章：缺 §{missing_business_sections}"
            )
        # §7 应该是"运行时补丁"标题
        s7_title = top_sections.get(7, "")
        if s7_title and "运行时补丁" not in s7_title and "控制区" not in s7_title:
            prompt_whitelist_issues.append(
                f"业务角色 §7 标题应为『运行时补丁（控制区）』，实际：『{s7_title}』"
            )
        # §8 应该是"版本历史"
        s8_title = top_sections.get(8, "")
        if s8_title and "版本历史" not in s8_title:
            prompt_whitelist_issues.append(
                f"业务角色 §8 标题应为『版本历史』，实际：『{s8_title}』"
            )

    # 新业务角色更严格：agent_generated=true 不允许 prompt_whitelist 任何不合规
    prompt_whitelist_level = "OK"
    if prompt_whitelist_issues:
        if not is_meta and is_agent_generated:
            prompt_whitelist_level = "ERROR_NEW"
        elif not is_meta:
            prompt_whitelist_level = "WARN_NORMALIZE"
        else:
            prompt_whitelist_level = "WARN_META"

    return {
        "filename": path.name,
        "role": fm.get("role", path.stem),
        "domain": fm.get("domain", ""),
        "version": fm.get("version", ""),
        "agent_generated": is_agent_generated,
        "is_meta": is_meta,
        "is_tiny": is_tiny,
        # lengths
        "frontmatter_chars": fm_chars,
        "body_no_dynamic_chars": body_no_dynamic_chars,
        "dynamic_chars": len(dynamic_body),
        "max_section_chars": max_section,
        "max_section_id": max_section_id,
        "section_chars": section_chars,
        # field checks
        "missing_required": sorted(missing_required),
        "present_forbidden": sorted(present_forbidden),
        # structure
        "found_sections": found_sections,
        "has_dynamic_markers": has_dynamic_markers,
        # DYNAMIC content
        "patch_count": patch_count,
        "oversized_patches": oversized_patches,
        "marker_literal_in_body": marker_literal_in_body,
        # limits exceeded
        "fm_over_limit": fm_chars > LIMITS["frontmatter"],
        "body_over_limit": body_no_dynamic_chars > LIMITS["body_no_dynamic"],
        "section_over_limit": max_section > LIMITS["single_section"],
        "dynamic_over_limit": len(dynamic_body) > LIMITS["dynamic"],
        # T2.7 prompt 白名单契约 lint
        "prompt_whitelist_issues": prompt_whitelist_issues,
        "prompt_whitelist_level": prompt_whitelist_level,
        # P6：skill_refs 治理 lint
        "skill_refs_count": skill_refs_count,
        "skill_refs_over_limit": skill_refs_over_limit,
        "skill_trigger_gaps": skill_trigger_gaps,
    }


def _format_measurements(measures: list[dict]) -> str:
    """把测量结果格式化为人类可读的 markdown 表格，注入 user prompt。"""
    lines = [
        "# 程序层测量结果（字符长度 / 字段合规 / DYNAMIC 合规）",
        "",
        "| 角色 | domain | FM字符 | 正文字符 | 最大章节字符(§) | DYNAMIC字符 | 超限 | 缺必填 | 有禁止字段 | DYNAMIC对 | 补丁数 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for m in measures:
        over = []
        if m["fm_over_limit"]:
            over.append("FM")
        if m["body_over_limit"]:
            over.append("正文")
        if m["section_over_limit"]:
            over.append(f"§{m['max_section_id']}")
        if m["dynamic_over_limit"]:
            over.append("DYNAMIC")
        if m["oversized_patches"]:
            over.append(f"P{m['oversized_patches']}")

        lines.append(
            f"| {m['role']} | {m['domain']} "
            f"| {m['frontmatter_chars']} "
            f"| {m['body_no_dynamic_chars']} "
            f"| {m['max_section_chars']}(§{m['max_section_id']}) "
            f"| {m['dynamic_chars']} "
            f"| {' '.join(over) or '—'} "
            f"| {', '.join(m['missing_required']) or '—'} "
            f"| {', '.join(m['present_forbidden']) or '—'} "
            f"| {'✓' if m['has_dynamic_markers'] else '✗'} "
            f"| {m['patch_count']} |"
        )

    lines.append("")
    lines.append("## 详细异常")
    for m in measures:
        issues: list[str] = []
        if m["missing_required"]:
            issues.append(f"缺必填字段：{m['missing_required']}")
        if m["present_forbidden"]:
            issues.append(f"含禁止字段：{m['present_forbidden']}")
        if not m["has_dynamic_markers"]:
            issues.append("缺 DYNAMIC 标记对（<!-- DYNAMIC_START/END -->）")
        if m["marker_literal_in_body"]:
            issues.append("正文字面引用了 DYNAMIC_START marker（破坏 regex）")
        if m["oversized_patches"]:
            for idx, size in m["oversized_patches"]:
                issues.append(f"DYNAMIC 第 {idx} 条 patch 超限：{size} > {LIMITS['single_patch']} chars")
        # P6: skill_refs 治理
        if m.get("skill_refs_over_limit"):
            issues.append(
                f"skill_refs 数量 {m['skill_refs_count']} > 软上限 "
                f"{LIMITS['skill_refs_max']} → 建议 [SHRINK?]（收敛到 `_通用/` 或拆角色）"
            )
        for gap in m.get("skill_trigger_gaps", []):
            issues.append(f"skill_refs 引用的 skill trigger 缺失：{gap}")
        if issues:
            lines.append(f"\n### {m['role']}（{m['filename']}）")
            for iss in issues:
                lines.append(f"- {iss}")

    # T2.7 白名单契约 lint 段落
    lines.append("")
    lines.append("## T2.7 prompt 白名单契约 lint")
    lines.append("")
    lines.append("| 角色 | domain | 等级 | 问题 |")
    lines.append("|---|---|---|---|")
    for m in measures:
        level = m.get("prompt_whitelist_level", "OK")
        issues = m.get("prompt_whitelist_issues", [])
        if level == "OK":
            continue
        badge = {
            "ERROR_NEW": "🔴 ERROR",
            "WARN_NORMALIZE": "🟡 WARN",
            "WARN_META": "🔵 INFO",
        }.get(level, level)
        lines.append(
            f"| {m['role']} | {m['domain']} | {badge} | {'; '.join(issues)} |"
        )
    return "\n".join(lines)


def _scan_vault_stem_uniqueness() -> dict[str, list[Path]]:
    """扫 vault 全 .md 文件，按 stem 分组返回重名项。

    与 engine.wikilink.resolve_target 的索引逻辑对齐：排除 _STEM_SCAN_EXCLUDES
    下的文件。命名规则要求"角色 / 工作流 / 规则 / skill / 项目记录等命名空间
    stem 全 vault 唯一"，违反时 wikilink 解析会抛 DuplicateStemError。
    本扫描在审计阶段提前暴露这类冲突，避免运行时崩溃。

    返回 dict: stem → [path1, path2, ...]，只包含重名（len >= 2）的 stem。
    无重名 → 空 dict。
    """
    from collections import defaultdict
    groups: dict[str, list[Path]] = defaultdict(list)
    for p in VAULT_ROOT.rglob("*.md"):
        try:
            rel = p.relative_to(VAULT_ROOT).as_posix()
        except ValueError:
            continue
        if any(rel.startswith(prefix) for prefix in _STEM_SCAN_EXCLUDES):
            continue
        if _is_domain_rule_adapter(rel):
            continue
        groups[p.stem].append(p)
    return {stem: sorted(paths) for stem, paths in groups.items() if len(paths) >= 2}


def _format_stem_uniqueness(dupes: dict[str, list[Path]]) -> str:
    """把 stem 重名清单格式化为 markdown，注入到审计报告。"""
    excludes = "、".join(_STEM_SCAN_EXCLUDES) + "、00-系统/规则/<域>/（跨域适配器）"
    if not dupes:
        return (
            "# Vault stem 唯一性扫描\n\n"
            f"✅ 未发现 stem 重名（已排除：{excludes}）"
        )
    lines = [
        "# Vault stem 唯一性扫描",
        "",
        f"⚠️ 发现 {len(dupes)} 组 stem 重名（违反 vault命名规则.md，"
        f"wikilink 命中会抛 DuplicateStemError）",
        "",
    ]
    for stem in sorted(dupes):
        paths = dupes[stem]
        lines.append(f"## `{stem}.md`（{len(paths)} 处）")
        for p in paths:
            try:
                rel = p.relative_to(VAULT_ROOT).as_posix()
            except ValueError:
                rel = str(p)
            lines.append(f"- `{rel}`")
        lines.append("")
    lines.append(
        "**修复方向**：重命名为唯一 stem，或在 wikilink 处用完整路径消歧"
        "（如 `[[20-知识/角色技能/架构师/A1-代码量预算分账]]`）。"
    )
    return "\n".join(lines)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="角色审计器：审计指定 / 全部角色基因文件")
    p.add_argument("--dry-run", action="store_true", help="只打印测量结果，不调 LLM、不写盘")
    p.add_argument(
        "--target", action="append", default=None,
        help="治理对象（可重复 / 逗号分隔 / 'all'）；缺省审计全部角色",
    )
    p.add_argument(
        "--audit-outputs", action="store_true",
        help="切换到产物审计模式（扫 10-项目/*/PRD.md 越界 pattern），不跑角色基因审计",
    )
    return p.parse_args()


def _today_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d-%H%M")


def main() -> int:
    args = _parse_args()

    # 产物审计模式：与角色基因审计正交，独立路径不动 ROLE 状态机
    # （产物审计是治理 vault 产物的产物，不影响角色审计器自己的 busy/idle）
    if getattr(args, "audit_outputs", False):
        return _run_pm_output_audit(dry_run=bool(args.dry_run))

    dry_run = bool(args.dry_run)
    targets = parse_targets(args.target)   # None = 全部
    date_stamp = _today_stamp()

    if role_is_blocked(ROLE):
        print(f"[{ROLE}] status=blocked，跳过。", file=sys.stderr)
        return 1

    set_role_status(ROLE, status="busy", enforce_transition=False)

    # 1) 收集角色文件（rglob：支持 music/ 等域子目录；2026-05-24 D-9 全子目录方案）
    rgd = role_genes_dir()
    all_role_files = sorted(rgd.rglob("角色-*.md"))
    role_files = [
        f for f in all_role_files
        if f.name != SELF_FILENAME
        and (targets is None or any(t in f.stem for t in targets))
    ]

    if not role_files:
        print(f"[{ROLE}] 没有找到可审计的角色文件。", file=sys.stderr)
        set_role_status(ROLE, status="failed", enforce_transition=False)
        return 2

    print(f"[{ROLE}] 审计 {len(role_files)} 个角色：{[f.name for f in role_files]}")

    # 2) 程序层测量
    measures = [_measure_role(f) for f in role_files]
    measurement_table = _format_measurements(measures)

    # 2b) Vault stem 唯一性扫描（与角色审计正交：扫整个 vault，不受 --target 限制）
    stem_dupes = _scan_vault_stem_uniqueness()
    stem_table = _format_stem_uniqueness(stem_dupes)

    print(measurement_table)
    print()
    print(stem_table)

    if dry_run:
        print(f"[{ROLE}] --dry-run 模式，未调用 LLM、未写盘。")
        set_role_status(ROLE, status="success", reset_counters=True)
        set_role_status(ROLE, status="idle")
        append_audit({
            "timestamp": utc_now(), "role": ROLE, "project": "*",
            "result": "dry_run", "roles_checked": len(role_files),
        })
        return 0

    # 3) 规范文档 + 所有角色全文
    spec_path = VAULT_ROOT / SPEC_REL
    inputs = [spec_path] + role_files
    context = read_input_files(inputs)

    # 4) system prompt（角色审计器基因）
    system_prompt = build_system_prompt(ROLE, project=None)

    # 5) 报告路径
    audit_dir = VAULT_ROOT / AUDIT_DIR_REL
    audit_dir.mkdir(parents=True, exist_ok=True)
    report_rel = f"{AUDIT_DIR_REL}/角色基因审计-{date_stamp}.md"

    # 6) user prompt
    role_list = "\n".join(f"  - {m['role']}（{m['filename']}）" for m in measures)
    user_prompt = (
        f"# 审计任务\n\n"
        f"对照 `{SPEC_REL}` 审计以下 {len(role_files)} 个角色基因文件：\n{role_list}\n\n"
        f"# 程序层预计算（已完成，供你参考）\n\n{measurement_table}\n\n"
        f"---\n\n"
        f"{stem_table}\n\n"
        f"---\n\n"
        f"# 输入文件全文（规范 + 各角色）\n\n{context}\n\n"
        f"---\n\n"
        f"# 你的任务\n\n"
        f"按角色基因第 3-5 节，对每个角色做语义层审计（程序层测量已完成，你只需做语义判断）：\n\n"
        f"1. **字段重复**（规范 §6.1）：frontmatter 值是否在正文中重复描述\n"
        f"2. **禁止事项过散**（规范 §6.2）：§4 边界规则是否散落在正文其他节\n"
        f"3. **全局规则在角色内**（规范 §6.3）：技术栈 / 架构规则等是否直接写在角色而非引用\n"
        f"4. **DYNAMIC 区滥用**（规范 §6.4）：已 GRADUATE 补丁是否仍残留；DYNAMIC 是否长期堆积\n"
        f"5. **模糊禁止**（规范 §6.5）：§4 边界规则是否缺乏可 grep 的硬约束\n"
        f"6. **角色名不一致**（规范 §6.6）：引用其他角色时是否混用别名 / 中文名\n"
        f"7. **越界改他角色定义**（规范 §6.7）：是否在角色 X 里定义修改角色 Y 的逻辑\n"
        f"8. **技能未外迁**（规范 §6.8）：§6 / 单 patch 超规范上限且含可独立 grep gate / 反例 / 代码块 / 跨角色复用规则\n"
        f"9. **豁免识别**：元角色（domain=元）/ 极小角色 / 新生角色（agent_generated=true）按规范 §7 豁免条件先检查\n\n"
        f"每个偏离项必须引用规范具体条款（如'规范 §6.1'）+ 建议修复方向。\n\n"
        f"严重度分级：\n"
        f"- **严重**：缺必填字段 / 无 DYNAMIC 标记对 / marker 被字面引用（破坏 regex）\n"
        f"- **警告**：超长 [SHRINK?] / 单 patch 超长 / 禁止事项过散 / 全局规则在角色内 / 模糊禁止\n"
        f"- **建议**：字段重复描述 / 角色名不一致 / 越界提及\n\n"
        f"---\n\n"
        f"# [SPLIT?] 建议（强制：超限角色必须给出结构化外迁建议）\n\n"
        f"对每个 §6 超 1500 chars / 单 patch 超 1200 chars 的角色，按规范 §10「Skill 引用机制」**逐条**列出可外迁段落，每条 `[SPLIT?]` 必须包含 4 个字段：\n\n"
        f"1. **source**：源段落定位（如 `角色-X.md §6 步骤 3 子项 (1)` 或 `DYNAMIC patch [YYYY-MM-DD][KEEP] Xn`）\n"
        f"2. **size**：估算字符数（让用户判断收益）\n"
        f"3. **target**：建议 skill 文件路径（如 `20-知识/角色技能/{{角色}}/{{patch_id}}-{{标题}}.md`，跨角色共享用 `_通用/`）\n"
        f"4. **rationale**：为什么这段值得外迁（含可独立 grep gate / 反例 / 跨角色复用 等）\n\n"
        f"已 split 的角色（frontmatter 含 skill_refs）：若仍超限，新建议必须不与现有 skill_refs 重复\n"
        f"未超限的角色：不要发明 [SPLIT?] 建议\n"
        f"已被规范 §7 豁免的元角色 / 极小角色：豁免内不下 [SPLIT?]\n\n"
        f"---\n\n"
        f"# 输出（强制格式）\n\n"
        f"**你必须且只能输出一个 FILE 块**，内容是完整审计报告。FILE 块外不能有任何其他文字。\n\n"
        f"<!-- FILE: {report_rel} -->\n"
        f"---\n"
        f"type: audit\n"
        f"created: {utc_now()}\n"
        f"roles_audited: {len(role_files)}\n"
        f"---\n\n"
        f"# 角色基因审计报告 - {date_stamp}\n\n"
        f"## 0. 健康评分\n"
        f"（写实际数字）\n"
        f"- 完全合规角色：X / {len(role_files)}\n"
        f"- 严重问题：X 项\n"
        f"- 警告：X 项（[SHRINK?]：X 个）\n"
        f"- 建议：X 项\n\n"
        f"## 1. 分角色审计\n"
        f"（每个角色一节，引用上表的实际测量数字）\n\n"
        f"### 角色：<role>（<filename>）\n"
        f"**程序层测量**：FM N字符 / 正文 N字符 / DYNAMIC N字符\n"
        f"**偏离项**：\n"
        f"- [严重|警告|建议] 规范 §X.X：<偏离描述> → 建议：<修复方向>\n"
        f"若无偏离：合规\n\n"
        f"## 2. [SPLIT?] 外迁建议（结构化）\n"
        f"（只对超限角色出条目；未超限的不要凑数）\n\n"
        f"### 角色：<role>\n"
        f"- **[SPLIT?]** source: <段落定位> | size: ~N chars | target: `20-知识/角色技能/<角色>/<id>-<标题>.md` | rationale: <一句话>\n"
        f"- ...\n\n"
        f"## 3. 整体建议\n"
        f"（跨角色共性问题 + 优先处理顺序；包括规范文档本身是否需要更新）\n"
        f"<!-- /FILE -->\n"
    )

    # 7) 调用 LLM
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
            "timestamp": utc_now(), "role": ROLE,
            "result": "failed", "error": str(e),
        })
        return 1

    # 8) 写盘
    output_files = parse_claude_output_to_files(raw_output)
    written: list[str] = []

    if not output_files:
        dest = audit_dir / f"角色基因审计-{date_stamp}.md"
        write_output_atomic(dest, raw_output)
        written.append(str(dest))
        print(f"[{ROLE}] 未检测到 FILE 标签，降级写入 {dest}")
    else:
        for rel_path, content in output_files.items():
            # 安全防线：只允许写入审计报告，不允许修改角色文件
            if "角色基因" in rel_path and "审计" not in rel_path:
                print(f"[{ROLE}] ⚠️  拒绝写入角色文件 {rel_path}（审计者只读）", file=sys.stderr)
                continue
            dest = resolve_path(rel_path, project=None)
            write_output_atomic(dest, content)
            print(f"[{ROLE}] 写入: {dest}")
            written.append(rel_path)

    set_role_status(ROLE, status="success", reset_counters=True)
    set_role_status(ROLE, status="idle")
    append_audit({
        "timestamp": utc_now(), "role": ROLE,
        "result": "success", "outputs": written,
        "roles_audited": len(role_files),
    })
    print(f"[{ROLE}] 完成。报告：{written}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
