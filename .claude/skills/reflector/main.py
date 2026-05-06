"""
reflector/main.py — 复盘者执行入口（Phase 4d）

作用：
  跨多次工作流运行，扫描 vault 已有产出与当前 5 个工作角色的笔记，
  识别重复出现的失败模式与成功套路，把高质量优化建议沉淀到：
    - vault `00-系统/复盘记录/{date}.md`（叙事报告）
    - vault `00-系统/角色基因/角色-{name}.md`（DYNAMIC 区域被新补丁替换；整份重写）

输入（vault）：
  - 10-项目/*/PRD.md / 系统设计.md / 指令/*.md / 脑暴-*.md  按 --days 过滤
  - 00-系统/角色基因/角色-*.md                              5 个工作角色（不含复盘者本身）

CLI：
  python .claude/skills/reflector/main.py --task "..." [--project X] [--days 7]
    --task   复盘焦点（必填，强制写明此次想关注什么）
    --project 仅复盘单个项目（缺省扫所有 10-项目/*）
    --days   时间窗口，默认 14；mtime 超出窗口的产出忽略
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import re

from common import (
    build_system_prompt, read_input_files,
    write_output_atomic, parse_claude_output_to_files,
    call_claude, append_audit, utc_now,
)
from engine import (
    set_role_status, role_is_blocked,
    VAULT_ROOT, role_genes_dir, reflection_dir, resolve_path,
)


_DYNAMIC_PAIR = re.compile(
    r"(<!-- DYNAMIC_START -->)(.*?)(<!-- DYNAMIC_END -->)",
    re.DOTALL,
)


def _splice_dynamic(existing: str, new_full: str) -> tuple[str, str]:
    """硬护栏：把 new_full 里的 DYNAMIC 内容切下来，拼到 existing 的 DYNAMIC 区域里。

    只信 DYNAMIC 区域，non-DYNAMIC 部分保持 byte-identical（防 unicode normalization
    类的隐性改动）。

    返回 (拼接后内容, 简短描述)。若无法定位 DYNAMIC 标记，原样返回 new_full（降级）。
    """
    # 取每份的最后一对 DYNAMIC 标记
    def _last_match(text: str):
        ms = list(_DYNAMIC_PAIR.finditer(text))
        return ms[-1] if ms else None

    new_m = _last_match(new_full)
    old_m = _last_match(existing)

    if not new_m:
        return new_full, "降级：新内容缺 DYNAMIC 标记，整份写入"
    if not old_m:
        return new_full, "降级：原文件缺 DYNAMIC 标记，整份写入"

    new_dynamic_body = new_m.group(2)
    spliced = existing[:old_m.start(2)] + new_dynamic_body + existing[old_m.end(2):]
    return spliced, f"DYNAMIC 区域替换：{len(old_m.group(2).strip())} → {len(new_dynamic_body.strip())} 字符"

ROLE = "复盘者"

# 工作角色：复盘者只补丁这些（不补丁自己）
WORKER_ROLES = ("产品经理", "架构师", "技术主管", "后端工程师", "前端工程师")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="复盘者：跨多次运行识别模式 + 下发角色补丁")
    p.add_argument("--task", required=True, help="复盘焦点（必填）")
    p.add_argument("--project", default=None, help="仅复盘单个项目（缺省全部）")
    p.add_argument("--days", type=int, default=14, help="时间窗口（天），默认 14")
    return p.parse_args()


def _resolve_project(args) -> str | None:
    """复盘者允许 project=None（扫全 vault）。"""
    val = (
        args.project
        or os.environ.get("PROJECT")
        or os.environ.get("PROJECT_NAME")
        or ""
    ).strip()
    return val or None


def _gather_project_outputs(project: str | None, days: int) -> list[Path]:
    """按 mtime 过滤，收集项目产出的 markdown 文件。"""
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    proj_root = VAULT_ROOT / "10-项目"
    if not proj_root.is_dir():
        return []

    if project:
        candidates = [proj_root / project]
    else:
        candidates = sorted(p for p in proj_root.iterdir() if p.is_dir())

    docs: list[Path] = []
    targets = ("PRD.md", "系统设计.md", "API契约.md")
    for proj_dir in candidates:
        if not proj_dir.is_dir():
            continue
        for fname in targets:
            f = proj_dir / fname
            if f.exists() and f.stat().st_mtime >= cutoff:
                docs.append(f)
        instr = proj_dir / "指令"
        if instr.is_dir():
            for f in sorted(instr.glob("*.md")):
                if f.stat().st_mtime >= cutoff:
                    docs.append(f)
        for f in sorted(proj_dir.glob("脑暴-*.md")):
            if f.stat().st_mtime >= cutoff:
                docs.append(f)
    return docs


def _gather_worker_role_notes() -> list[Path]:
    """5 个工作角色当前 .md 全文（用于复盘者了解 DYNAMIC 当前内容）。"""
    rgd = role_genes_dir()
    out: list[Path] = []
    for role in WORKER_ROLES:
        f = rgd / f"角色-{role}.md"
        if f.exists():
            out.append(f)
    return out


def _today_stamp() -> str:
    """复盘记录文件名：YYYY-MM-DD-HHmm（本地时区）。"""
    return datetime.now().strftime("%Y-%m-%d-%H%M")


def main() -> int:
    args = _parse_args()
    task = (args.task or "").strip()
    project = _resolve_project(args)
    days = max(1, int(args.days))
    date_stamp = _today_stamp()

    if role_is_blocked(ROLE):
        print(f"[{ROLE}] status=blocked，跳过。", file=sys.stderr)
        return 1

    set_role_status(ROLE, status="busy", enforce_transition=False)

    # 输入：项目产出 + 工作角色笔记
    project_docs = _gather_project_outputs(project, days)
    role_notes = _gather_worker_role_notes()
    inputs = project_docs + role_notes

    if not project_docs:
        scope = f"项目 {project}" if project else "所有项目"
        msg = (
            f"[{ROLE}] 输入缺失：{scope} 在最近 {days} 天内没有产出文档可复盘。\n"
            f"扩大 --days 或先跑一次工作流再复盘。"
        )
        print(msg, file=sys.stderr)
        set_role_status(
            ROLE, status="failed",
            increment_consecutive_failures=True, increment_error=True,
            enforce_transition=False,
        )
        append_audit({
            "timestamp": utc_now(), "role": ROLE, "project": project or "*",
            "task": task, "result": "failed", "error": "no_inputs_in_window",
        })
        return 2

    print(
        f"[{ROLE}] 复盘范围：{'单项目=' + project if project else '所有项目'} / "
        f"窗口={days}d / 项目文档={len(project_docs)} 份 / 角色笔记={len(role_notes)} 份",
        flush=True,
    )

    # system prompt：复盘者角色基因（含 DYNAMIC 注入机制，但复盘者自身 DYNAMIC 为空）
    system_prompt = build_system_prompt(ROLE, project=project)

    # user prompt
    context = read_input_files(inputs)

    # 必须产出的文件清单（用于约束模型）
    reflection_path = f"00-系统/复盘记录/{date_stamp}.md"
    role_paths_hint = "\n".join(
        f"  - `00-系统/角色基因/角色-{name}.md`"
        f"（仅在你识别出该角色有反复模式需要补丁时输出；最多 3 份）"
        for name in WORKER_ROLES
    )

    user_prompt = (
        f"# 复盘焦点（来自 --task）\n{task}\n\n"
        f"# 复盘范围\n"
        f"- 项目：{project or '所有项目'}\n"
        f"- 时间窗口：最近 {days} 天\n"
        f"- 共扫描 {len(project_docs)} 份项目产出 + {len(role_notes)} 份角色笔记\n\n"
        f"# 输入文件全文\n\n{context}\n\n---\n\n"
        f"# 你的任务\n\n"
        f"基于以上输入，**识别在多次运行中重复出现**的失败模式或成功套路，"
        f"按角色基因第 4 节『复盘记录主报告结构』产出 `{reflection_path}`。\n\n"
        f"**最多**对 3 个工作角色的笔记下发补丁（DYNAMIC 区域更新）。"
        f"角色笔记必须**整份重写**（输出完整的 frontmatter + 1-7 节正文 + DYNAMIC 区域），"
        f"**只**修改 `<!-- DYNAMIC_START -->` 到 `<!-- DYNAMIC_END -->` 之间的内容。\n"
        f"5 个可补丁的工作角色：\n{role_paths_hint}\n\n"
        f"复盘者本身（`角色-复盘者.md`）**禁止**修改（元角色避免反身循环）。\n\n"
        f"---\n\n"
        f"**输出格式（强制）**\n\n"
        f"必须输出 1 个 FILE 块（复盘记录主报告）+ 0-3 个 FILE 块（角色补丁）：\n\n"
        f"<!-- FILE: {reflection_path} -->\n"
        f"（复盘记录正文）\n"
        f"<!-- /FILE -->\n\n"
        f"<!-- FILE: 00-系统/角色基因/角色-<某角色>.md -->\n"
        f"（整份角色笔记，DYNAMIC 区域填入新补丁）\n"
        f"<!-- /FILE -->\n\n"
        f"如果当前没有任何重复模式值得下补丁，不要勉强：只输出复盘记录即可。"
        f"宁可只下发 0 条补丁，也不下发平庸补丁。\n"
    )

    # 调用 Claude
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
            "timestamp": utc_now(), "role": ROLE, "project": project or "*",
            "task": task, "result": "failed", "error": str(e),
        })
        return 1

    # 写盘
    output_files = parse_claude_output_to_files(raw_output)
    if not output_files:
        # 降级：整体写入复盘记录
        dest = reflection_dir() / f"{date_stamp}.md"
        write_output_atomic(dest, raw_output)
        written = [reflection_path]
        patched_roles: list[str] = []
        print(f"[{ROLE}] 未检测到 FILE 标签，降级写入 {dest}")
    else:
        written = []
        patched_roles = []
        for rel_path, content in output_files.items():
            # 安全防线：不允许复盘者修改自己
            if rel_path.endswith("角色-复盘者.md"):
                print(
                    f"[{ROLE}] ⚠️  拒绝复盘者修改自己的角色笔记：{rel_path}",
                    file=sys.stderr,
                )
                continue
            dest = resolve_path(rel_path, project or "default")

            # 硬护栏：写入角色笔记时，仅替换 DYNAMIC 区域，non-DYNAMIC 部分保留原文件字节
            is_role_patch = rel_path.startswith("00-系统/角色基因/角色-")
            if is_role_patch and dest.exists():
                existing = dest.read_text(encoding="utf-8")
                spliced, splice_note = _splice_dynamic(existing, content)
                content = spliced
                print(f"[{ROLE}] 角色补丁 splice：{splice_note}")

            write_output_atomic(dest, content)
            print(f"[{ROLE}] 写入: {dest}")
            written.append(rel_path)
            if is_role_patch:
                role_name = rel_path.split("角色-", 1)[1].rsplit(".md", 1)[0]
                patched_roles.append(role_name)

    set_role_status(ROLE, status="success", reset_counters=True)
    set_role_status(ROLE, status="idle")
    append_audit({
        "timestamp": utc_now(), "role": ROLE, "project": project or "*",
        "task": task, "result": "success",
        "outputs": written,
        "patched_roles": patched_roles,
        "days_window": days,
    })
    print(
        f"[{ROLE}] 完成。复盘记录：{reflection_path}；"
        f"补丁角色：{patched_roles or '（无）'}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
