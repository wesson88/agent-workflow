"""
role_auditor/main.py — 角色规范师执行入口

作用：
  对照 vault `00-系统/规则/角色基因规范.md` 审计所有 `角色-*.md` 文件，
  输出可操作的偏离清单到 `00-系统/审计报告/角色基因审计-{date}.md`。

  程序层先做可量化测量（字符长度 / frontmatter 字段 / DYNAMIC regex），
  把测量结果连同规范 + 所有角色全文传给 LLM，LLM 负责语义判断（反模式 / 豁免）
  并产出最终报告。

CLI：
  python .claude/skills/role_auditor/main.py [--dry-run] [--role 角色名]
    --dry-run  只打印测量结果，不调 LLM、不写盘
    --role     只审计指定角色（缺省审计全部）
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
    call_claude, append_audit, utc_now,
)
from engine import (
    set_role_status, role_is_blocked,
    VAULT_ROOT, role_genes_dir, resolve_path,
)

ROLE = "角色规范师"

# 不审计自身
SELF_FILENAME = "角色-角色规范师.md"

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

# 长度上限（字符数）
LIMITS = {
    "frontmatter": 800,
    "body_no_dynamic": 5000,
    "single_section": 1500,
    "dynamic": 5000,
    "single_patch": 1200,
}

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

    # 章节序号检查
    found_sections = sorted(int(k) for k in section_chars if k.isdigit())

    # 是否有 DYNAMIC 标记
    has_dynamic_markers = "<!-- DYNAMIC_START -->" in text and "<!-- DYNAMIC_END -->" in text

    # 豁免判断
    is_meta = str(fm.get("domain", "")).strip() == "元"
    is_agent_generated = bool(fm.get("agent_generated", False))
    is_tiny = fm.get("role", "") in ("批判者", "用户体验者")

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
        if issues:
            lines.append(f"\n### {m['role']}（{m['filename']}）")
            for iss in issues:
                lines.append(f"- {iss}")
    return "\n".join(lines)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="角色规范师：审计所有角色基因文件的合规性")
    p.add_argument("--dry-run", action="store_true", help="只打印测量结果，不调 LLM、不写盘")
    p.add_argument("--role", default=None, help="只审计指定角色名（缺省全部）")
    return p.parse_args()


def _today_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d-%H%M")


def main() -> int:
    args = _parse_args()
    dry_run = bool(args.dry_run)
    filter_role = (args.role or "").strip()
    date_stamp = _today_stamp()

    if role_is_blocked(ROLE):
        print(f"[{ROLE}] status=blocked，跳过。", file=sys.stderr)
        return 1

    set_role_status(ROLE, status="busy", enforce_transition=False)

    # 1) 收集角色文件
    rgd = role_genes_dir()
    all_role_files = sorted(rgd.glob("角色-*.md"))
    role_files = [
        f for f in all_role_files
        if f.name != SELF_FILENAME
        and (not filter_role or filter_role in f.stem)
    ]

    if not role_files:
        print(f"[{ROLE}] 没有找到可审计的角色文件。", file=sys.stderr)
        set_role_status(ROLE, status="failed", enforce_transition=False)
        return 2

    print(f"[{ROLE}] 审计 {len(role_files)} 个角色：{[f.name for f in role_files]}")

    # 2) 程序层测量
    measures = [_measure_role(f) for f in role_files]
    measurement_table = _format_measurements(measures)

    print(measurement_table)

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

    # 4) system prompt（角色规范师基因）
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
        f"8. **豁免识别**：元角色（domain=元）/ 极小角色 / 新生角色（agent_generated=true）按规范 §7 豁免条件先检查\n\n"
        f"每个偏离项必须引用规范具体条款（如'规范 §6.1'）+ 建议修复方向。\n\n"
        f"严重度分级：\n"
        f"- **严重**：缺必填字段 / 无 DYNAMIC 标记对 / marker 被字面引用（破坏 regex）\n"
        f"- **警告**：超长 [SHRINK?] / 禁止事项过散 / 全局规则在角色内 / 模糊禁止\n"
        f"- **建议**：字段重复描述 / 角色名不一致 / 越界提及\n\n"
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
        f"## 2. 整体建议\n"
        f"（跨角色共性问题 + 优先处理顺序）\n"
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
