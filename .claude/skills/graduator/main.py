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
  python .claude/skills/graduator/main.py --task "..." [--target X] [--dry-run]
    --task     本次晋升说明（必填，记录到 audit）
    --target   治理对象（可重复 / 逗号分隔 / 'all'）；缺省扫 5 个工作角色
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
    call_claude, append_audit, utc_now, parse_targets,
)
from engine import (
    set_role_status, role_is_blocked,
    role_genes_dir, resolve_path,
)

ROLE = "晋升者"

# 跨域配置（v1.1.0）：晋升者跨域差异**完全在路径**，治理逻辑（GRADUATE/DROP marker 处理）
# 跨域同款。SE 域 = 角色基因无 domain 子目录；music 域 = 角色基因含 music/ 子目录
DOMAIN_CONFIGS: dict[str, dict] = {
    "se": {
        "worker_roles": ("产品经理", "架构师", "技术主管", "后端工程师", "前端工程师"),
        "role_gene_dir_segment": "",
    },
    "music": {
        "worker_roles": (
            "音乐总监", "制作人", "作词", "作曲",
            "编曲", "和声编写", "混音师", "母带工程师",
        ),
        "role_gene_dir_segment": "music",
    },
}


def _resolve_domain(args) -> str:
    """domain 解析：① --domain 显式 ② --target 命中某 domain worker_roles 推导 ③ fallback se。"""
    explicit = (getattr(args, "domain", None) or "").strip()
    if explicit:
        return explicit
    # --target 推导：若 target 命中某 domain 的 worker_roles，用该 domain
    raw_targets = getattr(args, "target", None) or []
    if raw_targets:
        flat = ",".join(raw_targets).replace(" ", "")
        tokens = [t for t in flat.split(",") if t and t != "all"]
        for d, cfg in DOMAIN_CONFIGS.items():
            if any(t in r for r in cfg["worker_roles"] for t in tokens):
                return d
    return "se"


def _role_gene_path(rgd: Path, domain: str, role: str) -> Path:
    cfg = DOMAIN_CONFIGS.get(domain, DOMAIN_CONFIGS["se"])
    seg = cfg.get("role_gene_dir_segment", "")
    return (rgd / seg / f"角色-{role}.md") if seg else (rgd / f"角色-{role}.md")


def _role_gene_rel(domain: str, role: str) -> str:
    """角色基因相对路径（写入 FILE 块 / target 匹配用）。"""
    cfg = DOMAIN_CONFIGS.get(domain, DOMAIN_CONFIGS["se"])
    seg = cfg.get("role_gene_dir_segment", "")
    return f"00-系统/角色基因/{seg}/角色-{role}.md" if seg else f"00-系统/角色基因/角色-{role}.md"


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
    p = argparse.ArgumentParser(description="晋升者：把用户已确认的 GRADUATE/DROP 补丁固化到主体（跨域 v1.1.0）")
    p.add_argument("--task", required=True, help="本次晋升说明（写入 audit）")
    p.add_argument(
        "--domain", default=None,
        help=f"域（{'/'.join(DOMAIN_CONFIGS.keys())}）；缺省按 --target 推导 / fallback se",
    )
    p.add_argument(
        "--target", action="append", default=None,
        help="治理对象（可重复 / 逗号分隔 / 'all'）；缺省扫 domain 的全部工作角色",
    )
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
    targets = parse_targets(args.target)   # None = 全部 WORKER_ROLES

    # domain 解析（v1.1.0 跨域）
    domain = _resolve_domain(args)
    if domain not in DOMAIN_CONFIGS:
        print(f"[{ROLE}] 未知 domain={domain}，已知：{list(DOMAIN_CONFIGS.keys())}", file=sys.stderr)
        return 2
    WORKER_ROLES_DOMAIN = DOMAIN_CONFIGS[domain]["worker_roles"]

    if role_is_blocked(ROLE):
        print(f"[{ROLE}] status=blocked，跳过。", file=sys.stderr)
        return 1

    set_role_status(ROLE, status="busy", enforce_transition=False)

    # 1) 按 target 过滤要扫的工作角色
    scope_roles = (
        WORKER_ROLES_DOMAIN if targets is None
        else tuple(r for r in WORKER_ROLES_DOMAIN if any(t in r for t in targets))
    )
    if not scope_roles:
        print(
            f"[{ROLE}] --target={sorted(targets)} 不匹配任何工作角色（{list(WORKER_ROLES_DOMAIN)}），"
            f"无可处理。", file=sys.stderr,
        )
        set_role_status(ROLE, status="success", reset_counters=True)
        set_role_status(ROLE, status="idle")
        return 0
    if targets is not None:
        print(f"[{ROLE}] 🎯 target 过滤命中 {len(scope_roles)} 个角色：{list(scope_roles)}")
    print(f"[{ROLE}] domain={domain} / worker_roles 全集={list(WORKER_ROLES_DOMAIN)}")

    # 2) 扫描 scope 内的工作角色（按 domain 路径），找待执行标记
    rgd = role_genes_dir()
    workplans: list[tuple[str, Path, str, list[dict]]] = []  # (role, path, text, patches)
    for role_name in scope_roles:
        path = _role_gene_path(rgd, domain, role_name)
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
            "domain": domain,
        })
        return 0

    print(f"[{ROLE}] 待执行计划（domain={domain}）：")
    total = 0
    for role_name, _path, _text, patches in workplans:
        graduates = [p for p in patches if p["label"] == "GRADUATE"]
        drops = [p for p in patches if p["label"] == "DROP"]
        total += len(patches)
        print(f"  - {_role_gene_rel(domain, role_name)}：{len(graduates)} 条 GRADUATE / {len(drops)} 条 DROP")
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
            "domain": domain,
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

        role_gene_rel = _role_gene_rel(domain, role_name)
        user_prompt = (
            f"# 工作目标\n本次晋升说明（domain={domain}）：{task}\n\n"
            f"角色：**{role_name}**\n"
            f"路径：`{role_gene_rel}`\n"
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
            f"5. **承载分离判断**（规范 §6.8 + §10）：融入 [GRADUATE] 后，估算目标章节字符数。若 ≥ 1500 chars 或单条补丁正文 ≥ 1200 chars，**应外迁到 skill 文件**而非全文塞进主体：\n"
            f"   - 主体只留\"一句话核心约束 + skill 指针\"（如 \"详见 skill `B5-空集守卫.md`\"）\n"
            f"   - **不要**动角色 frontmatter 的 refs 类字段：`skill_refs` 已废弃，"
            f"外迁 skill 靠自己的 `trigger` 被扫目录召回，不需要在角色里登记\n"
            f"   - 新建 skill 文件输出为额外 FILE 块（路径 `20-知识/角色技能/{domain}/{role_name}/<patch_id>-<短标题>.md`"
            f"——**域段 `{domain}` 不可省**，省了就落在引擎扫描根之外、永远进不了 prompt），"
            f"frontmatter 必含 type: skill / role / patch_id **以及 `trigger.keywords`**；"
            f"正文含核心约束 + 详细规则 + grep gate + 跨项目证据\n"
            f"   - ⚠️ `trigger.keywords` 是硬性的：**没有它这份 skill 永远不会进任何 prompt**"
            f"（外迁即等于删除）。keyword 要写「什么任务需要它」而不是「它属于哪类」，"
            f"且必须与同目录其它 skill 有区分度 —— 全同的 keyword 集合会让触发器只能靠文件名字典序挑\n"
            f"6. 输出 FILE 块（角色笔记必填 + 可选多份 skill 文件）：\n\n"
            f"<!-- FILE: {role_gene_rel} -->\n"
            f"（整份重写后内容；超限时主体已收窄为「一句话约束 + skill 指针」）\n"
            f"<!-- /FILE -->\n\n"
            f"<!-- FILE: 20-知识/角色技能/{domain}/{role_name}/<patch_id>-<短标题>.md -->\n"
            f"（仅在判定为外迁时输出；否则省略本块）\n"
            f"<!-- /FILE -->\n\n"
            f"**绝对不要**：\n"
            f"- 触碰其它无关章节 / 修改自己（晋升者）的角色笔记 / 合并多份角色到一份输出\n"
            f"- 输出 `20-知识/角色技能/{domain}/{role_name}/` 以外路径的 FILE 块（其它路径会被引擎拒写）\n"
            f"- 主体未超 1500 / 补丁正文未超 1200 时强行外迁（产生零碎短 skill 文件违反规范 §10.6 反例）"
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
        target = role_gene_rel
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

        # 5) 收集可选 skill 文件：仅允许 `20-知识/角色技能/{domain}/{role_name}/` 子树
        #
        # ⚠️ 2026-08-25 修：原为 `20-知识/角色技能/{role_name}/`，**缺 domain 段**。
        # 引擎的扫描根是 `ability_loader` 里的
        # `VAULT_ROOT/20-知识/角色技能/{domain}/{role_name}/`，所以按老前缀写出来的
        # skill 文件落在扫描根之外 —— 生成即失联，永远进不了 prompt。
        # 该缺陷在 skill_refs 废弃前被掩盖（名义上还有声明这条路），废弃后 trigger
        # 是唯一通道，路径写错就等于白写。vault 里不存在任何无域段的角色技能目录，
        # 说明这条路径此前从未真正产出过文件（或产出后被人工搬过）。
        skill_prefix = f"20-知识/角色技能/{domain}/{role_name}/"
        skill_files: list[tuple[str, str]] = []
        rejected_paths: list[str] = []
        for rel_path, content in files.items():
            if rel_path == target:
                continue
            if rel_path.startswith(skill_prefix) and rel_path.endswith(".md"):
                skill_files.append((rel_path, content))
            else:
                rejected_paths.append(rel_path)
        for rp in rejected_paths:
            print(
                f"[{ROLE}] ⚠️  拒写非法路径 {rp}（只接受角色文件本身 + `{skill_prefix}*.md`）",
                file=sys.stderr,
            )

        # 6) 审计 diff 摘要：原文与新文的字符增量、DYNAMIC 区前后大小
        old_dyn = _last_dynamic_body(text) or ""
        new_dyn = _last_dynamic_body(new_content) or ""
        body_old_len = len(text) - len(old_dyn)
        body_new_len = len(new_content) - len(new_dyn)
        print(
            f"[{ROLE}] {role_name} diff 摘要："
            f"主体 {body_old_len} → {body_new_len} 字符（Δ {body_new_len - body_old_len:+d}）；"
            f"DYNAMIC {len(old_dyn.strip())} → {len(new_dyn.strip())} 字符（Δ {len(new_dyn.strip()) - len(old_dyn.strip()):+d}）"
            f"{'；新建 skill ' + str(len(skill_files)) + ' 份' if skill_files else ''}",
            file=sys.stderr,
        )

        # 7) 写盘（角色文件 + 可选 skill 文件）
        dest = resolve_path(target, project=None)
        write_output_atomic(dest, new_content)
        print(f"[{ROLE}] ✅ 写入 {dest}")
        written.append(target)

        for sk_path, sk_content in skill_files:
            sk_dest = resolve_path(sk_path, project=None)
            write_output_atomic(sk_dest, sk_content)
            print(f"[{ROLE}] ✅ 写入 skill {sk_dest}")
            written.append(sk_path)

    set_role_status(ROLE, status="success", reset_counters=True)
    set_role_status(ROLE, status="idle")
    append_audit({
        "timestamp": utc_now(), "role": ROLE, "project": "*",
        "task": task, "result": "success" if written else "all_skipped",
        "domain": domain,
        "outputs": written, "skipped": skipped,
        "patch_count": total,
    })
    print(
        f"\n[{ROLE}] 完成。成功：{written or '（无）'}；跳过：{skipped or '（无）'}"
    )
    return 0 if written or not skipped else 1


if __name__ == "__main__":
    sys.exit(main())
