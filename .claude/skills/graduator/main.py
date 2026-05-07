"""
graduator/main.py — 晋升者执行入口（Phase 4f）

作用：
  扫 vault `00-系统/角色基因/角色-{worker}.md` 里 DYNAMIC 区域**用户已确认**
  （即不带 `?` 的 `[GRADUATE]` / `[DROP]`）的补丁，让 LLM 把它们：
    - GRADUATE：融入主体相应章节
    - DROP：从 DYNAMIC 删掉
  KEEP / NEW / 带 `?` 的标记保持不动。

输入（vault）：
  - 00-系统/角色基因/角色-*.md  5 个工作角色

输出（vault）：
  - 00-系统/角色基因/角色-{role}.md  仅对有待执行标记的角色重写
  - stderr: 每处改动的简短 diff 摘要

CLI：
  python .claude/skills/graduator/main.py --task "..." [--dry-run]
    --task     本次晋升说明（必填，记录到 audit）
    --dry-run  只列待执行标记，不调 LLM、不写盘
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import (
    build_system_prompt, write_output_atomic, parse_claude_output_to_files,
    call_claude, append_audit, utc_now,
)
from engine import (
    set_role_status, role_is_blocked,
    role_genes_dir, resolve_path,
)

ROLE = "晋升者"
WORKER_ROLES = ("产品经理", "架构师", "技术主管", "后端工程师", "前端工程师")


# ── 标记解析 ─────────────────────────────────────────────
# 形如：# Patch [2026-05-06T23:49Z] [GRADUATE] B1 — 短标题
# 抽 `?` 的策略：[GRADUATE] 不带 ? 才算用户确认
_PATCH_HEADER_RE = re.compile(
    r"^(\s*)#\s*Patch\s*\[([^\]]+)\]\s*\[(GRADUATE|DROP|GRADUATE\?|DROP\?|NEW|KEEP)\]\s*(.+?)\s*$",
    re.MULTILINE,
)
_DYNAMIC_RE = re.compile(
    r"<!-- DYNAMIC_START -->(.*?)<!-- DYNAMIC_END -->",
    re.DOTALL,
)


def _last_dynamic_body(text: str) -> str | None:
    matches = list(_DYNAMIC_RE.finditer(text))
    return matches[-1].group(1) if matches else None


def _scan_pending_patches(role_text: str) -> list[dict]:
    """从 DYNAMIC 区域抽出**用户已确认**（无 ?）的 GRADUATE/DROP 补丁。

    返回 [{label: GRADUATE/DROP, timestamp, title, header_line}, ...]
    """
    body = _last_dynamic_body(role_text)
    if body is None:
        return []
    out: list[dict] = []
    for m in _PATCH_HEADER_RE.finditer(body):
        _indent, ts, label, title = m.groups()
        # 只收无 ? 的 GRADUATE/DROP；带 ? 的（GRADUATE?/DROP?）跳过
        if label in ("GRADUATE", "DROP"):
            out.append({
                "label": label,
                "timestamp": ts,
                "title": title.strip(),
                "header_line": m.group(0).strip(),
            })
    return out


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="晋升者：把用户已确认的 GRADUATE/DROP 补丁固化到主体")
    p.add_argument("--task", required=True, help="本次晋升说明（写入 audit）")
    p.add_argument("--dry-run", action="store_true", help="只列待执行标记，不调 LLM、不写盘")
    return p.parse_args()


def _build_role_workplan(role_text: str, role_name: str) -> tuple[list[dict], str]:
    """返回 (patches_to_apply, role_text_full) 对。"""
    patches = _scan_pending_patches(role_text)
    return patches, role_text


def main() -> int:
    args = _parse_args()
    task = (args.task or "").strip()
    dry_run = bool(args.dry_run)

    if role_is_blocked(ROLE):
        print(f"[{ROLE}] status=blocked，跳过。", file=sys.stderr)
        return 1

    set_role_status(ROLE, status="busy", enforce_transition=False)

    # 1) 扫描所有工作角色，找待执行标记
    rgd = role_genes_dir()
    workplans: list[tuple[str, Path, str, list[dict]]] = []  # (role, path, text, patches)
    for role_name in WORKER_ROLES:
        path = rgd / f"角色-{role_name}.md"
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        patches, _ = _build_role_workplan(text, role_name)
        if patches:
            workplans.append((role_name, path, text, patches))

    # 2) 摘要打印
    if not workplans:
        print(f"[{ROLE}] 没有任何角色含**用户已确认**（无 `?`）的 GRADUATE/DROP 标记。")
        print(f"[{ROLE}] 用户需在 vault 内手动把 [GRADUATE?] → [GRADUATE]，[DROP?] → [DROP] 才会触发晋升。")
        set_role_status(ROLE, status="success", reset_counters=True)
        set_role_status(ROLE, status="idle")
        append_audit({
            "timestamp": utc_now(), "role": ROLE, "project": "*",
            "task": task, "result": "noop", "reason": "no_confirmed_markers",
        })
        return 0

    print(f"[{ROLE}] 待执行计划：")
    total = 0
    for role_name, _path, _text, patches in workplans:
        graduates = [p for p in patches if p["label"] == "GRADUATE"]
        drops = [p for p in patches if p["label"] == "DROP"]
        total += len(patches)
        print(f"  - 角色-{role_name}.md：{len(graduates)} 条 GRADUATE / {len(drops)} 条 DROP")
        for p in graduates:
            print(f"      [GRADUATE] [{p['timestamp']}] {p['title']}")
        for p in drops:
            print(f"      [DROP]     [{p['timestamp']}] {p['title']}")
    print(f"[{ROLE}] 共 {total} 条标记待执行（{len(workplans)} 个角色）")

    if dry_run:
        print(f"[{ROLE}] --dry-run 模式，未调用 LLM、未写盘。")
        set_role_status(ROLE, status="success", reset_counters=True)
        set_role_status(ROLE, status="idle")
        append_audit({
            "timestamp": utc_now(), "role": ROLE, "project": "*",
            "task": task, "result": "dry_run", "patch_count": total,
        })
        return 0

    # 3) 对每个有待执行标记的角色，单独跑一次 LLM（避免一个 prompt 太长）
    system_prompt = build_system_prompt(ROLE, project=None)
    written: list[str] = []
    skipped: list[str] = []

    for role_name, path, text, patches in workplans:
        graduates = [p for p in patches if p["label"] == "GRADUATE"]
        drops = [p for p in patches if p["label"] == "DROP"]
        graduate_summary = "\n".join(
            f"- `[GRADUATE]` `[{p['timestamp']}]` {p['title']}"
            for p in graduates
        ) or "（无）"
        drop_summary = "\n".join(
            f"- `[DROP]` `[{p['timestamp']}]` {p['title']}"
            for p in drops
        ) or "（无）"

        user_prompt = (
            f"# 工作目标\n本次晋升说明：{task}\n\n"
            f"角色：**{role_name}**\n"
            f"路径：`00-系统/角色基因/角色-{role_name}.md`\n"
            f"待执行 GRADUATE 标记（{len(graduates)} 条）：\n{graduate_summary}\n\n"
            f"待执行 DROP 标记（{len(drops)} 条）：\n{drop_summary}\n\n"
            f"---\n\n"
            f"# 当前角色笔记全文\n\n=== 角色-{role_name}.md ===\n{text}\n=== END ===\n\n"
            f"---\n\n"
            f"# 你的任务\n\n"
            f"按角色基因第 4 节『重写规则』产出**整份**角色笔记重写：\n\n"
            f"1. 把上面 GRADUATE 的补丁**融入主体**最相关章节（措辞与主体一致）\n"
            f"2. 把上面 DROP 的补丁从 DYNAMIC 区域**删除**\n"
            f"3. KEEP / NEW / 带 `?` 的标记**全部保留**\n"
            f"4. frontmatter `version` 升次版本号；`## 9. 版本历史`（或同等章节）加一行说明\n"
            f"5. 输出整份完整内容到 FILE 块：\n\n"
            f"<!-- FILE: 00-系统/角色基因/角色-{role_name}.md -->\n"
            f"（整份重写后内容）\n"
            f"<!-- /FILE -->\n\n"
            f"**绝对不要**：触碰其它无关章节、修改自己（晋升者）的角色笔记、合并多份角色到一份输出。"
        )

        print(f"\n{'='*60}")
        print(f"[{ROLE}] 处理 角色-{role_name}.md（{len(graduates)} GRAD / {len(drops)} DROP）")
        print(f"{'='*60}")
        try:
            raw = call_claude(system_prompt, user_prompt, ROLE)
        except Exception as e:
            print(f"[{ROLE}] ❌ {role_name} LLM 调用失败：{e}", file=sys.stderr)
            skipped.append(role_name)
            continue

        files = parse_claude_output_to_files(raw)
        target = f"00-系统/角色基因/角色-{role_name}.md"
        if target not in files:
            # 兜底：尝试模糊匹配
            cand = [k for k in files if k.endswith(f"角色-{role_name}.md")]
            if not cand:
                print(f"[{ROLE}] ❌ {role_name} LLM 未输出对应 FILE 块；跳过。", file=sys.stderr)
                skipped.append(role_name)
                continue
            target = cand[0]

        new_content = files[target]

        # 4) 安全护栏：拒绝晋升者修改自己 + 至少要有 DYNAMIC 标记
        if "角色-晋升者" in target:
            print(f"[{ROLE}] ⚠️  拒绝晋升者修改自己；跳过。", file=sys.stderr)
            skipped.append(role_name)
            continue
        if "<!-- DYNAMIC_START -->" not in new_content or "<!-- DYNAMIC_END -->" not in new_content:
            print(f"[{ROLE}] ❌ {role_name} 输出缺 DYNAMIC 标记；拒写、跳过。", file=sys.stderr)
            skipped.append(role_name)
            continue

        # 5) 审计 diff 摘要：原文与新文的字符增量、DYNAMIC 区前后大小
        old_dyn = _last_dynamic_body(text) or ""
        new_dyn = _last_dynamic_body(new_content) or ""
        body_old_len = len(text) - len(old_dyn)
        body_new_len = len(new_content) - len(new_dyn)
        print(
            f"[{ROLE}] {role_name} diff 摘要："
            f"主体 {body_old_len} → {body_new_len} 字符（Δ {body_new_len - body_old_len:+d}）；"
            f"DYNAMIC {len(old_dyn.strip())} → {len(new_dyn.strip())} 字符（Δ {len(new_dyn.strip()) - len(old_dyn.strip()):+d}）",
            file=sys.stderr,
        )

        # 6) 写盘
        dest = resolve_path(target, project=None)
        write_output_atomic(dest, new_content)
        print(f"[{ROLE}] ✅ 写入 {dest}")
        written.append(target)

    set_role_status(ROLE, status="success", reset_counters=True)
    set_role_status(ROLE, status="idle")
    append_audit({
        "timestamp": utc_now(), "role": ROLE, "project": "*",
        "task": task, "result": "success" if written else "all_skipped",
        "outputs": written, "skipped": skipped,
        "patch_count": total,
    })
    print(
        f"\n[{ROLE}] 完成。成功：{written or '（无）'}；跳过：{skipped or '（无）'}"
    )
    return 0 if written or not skipped else 1


if __name__ == "__main__":
    sys.exit(main())
