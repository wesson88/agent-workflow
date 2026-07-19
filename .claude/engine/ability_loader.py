"""
engine/ability_loader.py — 能力加载统一层（架构演进第 4 步 · 2026-07-19）

设计：[[架构演进方向-角色接口化与跨域组合-2026-07-18]] 缺口 4 + 缺口 5（域 adapter）。
背景：四套能力附着机制的加载器碎在 skills/common.py 各自静默降级（filter=None
潜伏一个月的根因之一）。本模块：

1. **上收三个 user 侧加载器**（rule_refs 章节注入 / music genre skill 双路径
   / 通用 skill 双路径）——实现自 skills/common.py 原样迁入，common 保留
   re-export 兼容全部 main.py（append_audit → engine.audit 同款手法，P10.5 A1）
2. **统一装配入口** `assemble_user_context`：role_runner（及后续收编角色）的
   user context 单点组装——rule + skill + 域 adapter，逐机制 hint 汇报
3. **失败必告警**：加载器异常降级 / 全部 unresolved 时除 stderr 外落
   audit.jsonl `ability_load_warn` 事件（filter=None 教训：静默降级不可接受）
4. **域 adapter 自动注入**（缺口 5 第一块，原待办 _inject_domain_adapter）：
   调用方声明 domain → 自动注入 `00-系统/规则/{domain}/{角色}-视角.md`
   （存在才注入；通用角色跨域工作的视角适配）

system 侧两机制**不在**本模块：skill_refs（role_loader 加载期 inline 进 body）
与 capability_refs 摘要（build_system_prompt 内）位置语义不同，保持原地。

第三方 skill 兼容硬约束（设计文档）：对外来 skill 禁止 fail-closed schema
校验——宽容降级 + 告警不拒载；`extract_core_section` 无"核心约束"章节时
回退全文 + 3000 char 截断（截断策略升级挂待办）。

拍板记录（2026-07-19）：第三方 skill 目录退出 stem 索引（设计项②）**暂缓**
——现有 genre/SE skill 的 wikilink 显式引用全部走 stem，退出索引需迁移全部
引用为完整路径，收益（撞名防护）已被 ingest_check 预检覆盖大半；等 skill-MCP
（Phase C）注册表化显式 id 时一并迁移，避免两次迁移。
"""

from __future__ import annotations

import sys


def _warn_audit(mechanism: str, role_name: str, detail: str) -> None:
    """失败必告警：stderr 已由调用处打，这里补遥测事件（side channel 不拦主链）。"""
    try:
        from .audit import append_audit, utc_now
        append_audit({
            "timestamp": utc_now(),
            "type": "ability_load_warn",
            "mechanism": mechanism,
            "role": role_name,
            "detail": detail,
        })
    except Exception:
        pass


# ── rule_refs 章节注入（自 skills/common.py 迁入，行为不变）──────────
def load_rule_block(rule_refs: tuple[str, ...] | list[str]) -> tuple[str, str]:
    """按角色 frontmatter `rule_refs` 展开规则章节，拼成可注入 context 的 markdown 块。

    返回 (rule_block, source_hint)：
    - rule_block 形如 ``=== [[产物schema#7. ...]] ===\\n<内容>\\n\\n=== ... ===\\n<内容>``
    - source_hint 给日志用一句话描述（"按章节注入 N/M 段，共 K char" / "rule_refs 空"）
    rule_refs 为空 / 全展开失败时 rule_block 为空字符串，调用方负责回退（如全文件读）。
    """
    from engine.wikilink import expand_wikilinks
    refs = tuple(rule_refs or ())
    if not refs:
        return "", "rule_refs 空"
    refs_text = "\n".join(refs)
    result = expand_wikilinks(
        refs_text,
        filter=lambda wl: True,
        max_chars_per_link=4000,
        total_char_budget=20000,
        on_unresolved="warn",
    )
    parts: list[str] = []
    hit = 0
    for e in result.expansions:
        if e.reason == "ok" and e.content:
            parts.append(f"=== {e.wikilink.raw} ===\n{e.content}")
            hit += 1
    if not parts:
        hint = f"rule_refs 全部展开失败（unresolved={result.unresolved}）"
        _warn_audit("rule_refs", "-", hint)
        return "", hint
    block = "\n\n".join(parts)
    return block, f"按章节注入 {hit}/{len(refs)} 段，共 {result.total_chars} char"


# ── 流派 skill 双路径加载（music 域，自 skills/common.py 迁入）────────
# music skill 命名格式：`{前缀}-{流派}-{标题}`，前缀 R/M/Ma/V/Ar/C/L/Pr/D 之一，
# 流派只识别 R&B / 民谣 / 雷鬼。filter 在 wl.target 上匹配。
import re as _re  # noqa: E402
_MUSIC_SKILL_RE = _re.compile(
    r"(?:^|/)(R|M|Ma|V|Ar|C|L|Pr|D)\d+-(?:R&B|R%26B|民谣|雷鬼)-",
)


def load_genre_skill_block(
    role_name: str,
    task_text: str,
    upstream_text: str = "",
    domain: str = "music",
) -> tuple[str, str]:
    """双路径加载：wikilink 显式 ∪ keyword 触发，按 stem 去重 union。

    vault 路径：`20-知识/角色技能/{domain}/{role_name}/`。
    路径 1（wikilink 显式）按 music 命名正则过滤 + 只保留本角色目录命中；
    路径 2（keyword 兜底）discover_role_skills 按 frontmatter.trigger。
    目录不存在 / 双路径均空 → ("", 原因)，调用方负责跳过。
    """
    from engine import (
        VAULT_ROOT, discover_role_skills, render_triggered_block,
        expand_wikilinks, extract_core_section,
    )
    from engine.obsidian_io import split_frontmatter

    role_dir = VAULT_ROOT / "20-知识" / "角色技能" / domain / role_name
    if not role_dir.is_dir():
        return "", f"skill 目录不存在：{role_dir}"

    wikilink_parts: list[str] = []
    wikilink_loaded: list[str] = []
    wikilink_unresolved: list[str] = []
    haystack = (task_text or "") + "\n" + (upstream_text or "")
    if haystack.strip():
        try:
            result = expand_wikilinks(
                haystack,
                filter=lambda wl: bool(_MUSIC_SKILL_RE.search(wl.target)),
                max_chars_per_link=3000,
                total_char_budget=12_000,
                max_depth=0,
                on_unresolved="warn",
            )
            for e in result.expansions:
                if e.reason != "ok" or not e.content or not e.path:
                    continue
                try:
                    if e.path.parent != role_dir:
                        continue
                except Exception:
                    continue
                raw = e.path.read_text(encoding="utf-8")
                _, body = split_frontmatter(raw)
                core = extract_core_section(body).strip()
                if len(core) > 3000:
                    core = core[:3000] + (
                        f"\n\n…（截断：原文 {len(core)} 字符，本次取前 3000）"
                    )
                wikilink_parts.append(
                    f"=== Skill (wikilink:[[{e.wikilink.target}]]) ===\n{core}"
                )
                wikilink_loaded.append(e.path.stem)
            wikilink_unresolved = list(result.unresolved)
        except Exception as exc:
            msg = f"wikilink 展开失败（{type(exc).__name__}: {exc}），仅走 keyword 路径"
            print(f"[load_genre_skill_block:{role_name}] ⚠️ {msg}。", file=sys.stderr)
            _warn_audit("genre_skill_wikilink", role_name, msg)

    hits = discover_role_skills(role_dir, task_text, upstream_text)
    dedup_hits = [(p, r) for p, r in hits if p.stem not in set(wikilink_loaded)]
    keyword_block, keyword_loaded = render_triggered_block(dedup_hits)
    keyword_body = ""
    if keyword_block:
        idx = keyword_block.find("=== Skill")
        keyword_body = keyword_block[idx:].rstrip() if idx >= 0 else keyword_block.strip()

    if not wikilink_parts and not keyword_body:
        hint = "双路径均空"
        if wikilink_unresolved:
            hint += f"（wikilink unresolved={wikilink_unresolved}）"
        return "", hint

    sections: list[str] = []
    if wikilink_parts:
        sections.append("\n\n".join(wikilink_parts))
    if keyword_body:
        sections.append(keyword_body)

    block = (
        "\n\n## 引用 / 自动触发技能（wikilink ∪ keyword）\n\n"
        + "\n\n".join(sections)
        + "\n"
    )
    hint_parts = [
        f"wikilink={len(wikilink_loaded)}",
        f"keyword={len(keyword_loaded)}",
        f"union={len(wikilink_loaded) + len(keyword_loaded)}",
    ]
    if wikilink_unresolved:
        hint_parts.append(f"unresolved={len(wikilink_unresolved)}")
    return block, " / ".join(hint_parts)


def load_skill_block(
    role_name: str,
    task_text: str,
    upstream_text: str = "",
    domain: str = "se",
    *,
    code_root=None,
) -> tuple[str, str]:
    """通用双路径 skill 加载（D1 推广用，覆盖 SE 域 5 角色）。

    与 `load_genre_skill_block`（music 专用）的区别：
    - wikilink **不过滤**（无 music 命名前缀限制）—— 信任上游显式 `[[skill]]` 引用
    - wikilink **不强制 role_dir 范围** —— 允许跨目录加载（如架构师写 [[B7-...]] 给后端用）
    - 可选 `code_root` 参数：dev_backend / dev_frontend 等需扫项目代码做 file_patterns 时传入
    """
    from engine import (
        VAULT_ROOT, discover_role_skills, render_triggered_block,
        expand_wikilinks, extract_core_section,
    )
    from engine.obsidian_io import split_frontmatter

    role_dir = VAULT_ROOT / "20-知识" / "角色技能" / domain / role_name
    if not role_dir.is_dir():
        return "", f"skill 目录不存在：{role_dir}"

    wikilink_parts: list[str] = []
    wikilink_loaded: list[str] = []
    wikilink_unresolved: list[str] = []
    haystack = (task_text or "") + "\n" + (upstream_text or "")
    if haystack.strip():
        try:
            # 2026-07-18 huashu-demo 回归跑暴露：filter 是必填回调，传 None 会
            # TypeError → 整条 wikilink 显式路径自上线起从未生效。"不过滤"的
            # 正确写法是恒真回调。
            result = expand_wikilinks(
                haystack,
                filter=lambda wl: True,
                max_chars_per_link=3000,
                total_char_budget=12_000,
                max_depth=0,
                on_unresolved="warn",
            )
            for e in result.expansions:
                if e.reason != "ok" or not e.content or not e.path:
                    continue
                raw = e.path.read_text(encoding="utf-8")
                _, body = split_frontmatter(raw)
                core = extract_core_section(body).strip()
                if len(core) > 3000:
                    core = core[:3000] + (
                        f"\n\n…（截断：原文 {len(core)} 字符，本次取前 3000）"
                    )
                wikilink_parts.append(
                    f"=== Skill (wikilink:[[{e.wikilink.target}]]) ===\n{core}"
                )
                wikilink_loaded.append(e.path.stem)
            wikilink_unresolved = list(result.unresolved)
        except Exception as exc:
            msg = f"wikilink 展开失败（{type(exc).__name__}: {exc}），仅走 keyword 路径"
            print(f"[load_skill_block:{role_name}] ⚠️ {msg}。", file=sys.stderr)
            _warn_audit("skill_wikilink", role_name, msg)

    if code_root is not None:
        hits = discover_role_skills(role_dir, task_text, upstream_text, code_root)
    else:
        hits = discover_role_skills(role_dir, task_text, upstream_text)
    dedup_hits = [(p, r) for p, r in hits if p.stem not in set(wikilink_loaded)]
    keyword_block, keyword_loaded = render_triggered_block(dedup_hits)
    keyword_body = ""
    if keyword_block:
        idx = keyword_block.find("=== Skill")
        keyword_body = keyword_block[idx:].rstrip() if idx >= 0 else keyword_block.strip()

    if not wikilink_parts and not keyword_body:
        hint = "双路径均空"
        if wikilink_unresolved:
            hint += f"（wikilink unresolved={wikilink_unresolved}）"
        return "", hint

    sections: list[str] = []
    if wikilink_parts:
        sections.append("\n\n".join(wikilink_parts))
    if keyword_body:
        sections.append(keyword_body)

    block = (
        "\n\n## 引用 / 自动触发技能（wikilink ∪ keyword）\n\n"
        + "\n\n".join(sections)
        + "\n"
    )
    hint_parts = [
        f"wikilink={len(wikilink_loaded)}",
        f"keyword={len(keyword_loaded)}",
        f"union={len(wikilink_loaded) + len(keyword_loaded)}",
    ]
    if wikilink_unresolved:
        hint_parts.append(f"unresolved={len(wikilink_unresolved)}")
    return block, " / ".join(hint_parts)


# ── 域 adapter 自动注入（缺口 5 第一块）─────────────────────────────
def load_domain_adapter(role_name: str, domain: str | None) -> tuple[str, str]:
    """跨域视角适配：`00-系统/规则/{domain}/{role_name}-视角.md` 存在则整文注入。

    返回 (adapter_block, hint)。domain 为空 / 文件不存在 → ("", 原因)。
    通用角色（复盘者/用户体验者/批判者…）在域工作流里靠此获得域视角，
    替代手工缝 prompt。
    """
    if not domain:
        return "", "未声明 domain"
    from engine import VAULT_ROOT
    path = VAULT_ROOT / "00-系统" / "规则" / domain / f"{role_name}-视角.md"
    if not path.is_file():
        return "", f"无域视角文件：{domain}/{role_name}-视角.md"
    try:
        from engine.obsidian_io import split_frontmatter
        _, body = split_frontmatter(path.read_text(encoding="utf-8"))
    except Exception as e:
        msg = f"域视角读取失败（{e}）"
        print(f"[load_domain_adapter:{role_name}] ⚠️ {msg}", file=sys.stderr)
        _warn_audit("domain_adapter", role_name, msg)
        return "", msg
    block = f"\n\n## 域视角适配（{domain}）\n\n{body.strip()}\n"
    return block, f"注入 {domain}/{role_name}-视角.md（{len(body)} char）"


# ── 统一装配入口（role_runner 及后续收编角色的单点）─────────────────
def assemble_user_context(
    role,
    task: str,
    base_context: str,
    *,
    domain: str | None = None,
    code_root=None,
) -> tuple[str, dict[str, str]]:
    """user 侧能力块单点组装：base + rule_refs + skill 双路径 + 域 adapter。

    skill 路由（封闭规则）：role.domain == "music" → genre loader（命名过滤
    + 本角色目录限定）；其余 → 通用 loader（domain="se"）。
    返回 (context, hints)；hints 按机制给一句话（调用方统一打日志）。
    """
    hints: dict[str, str] = {}
    context = base_context

    rule_block, hints["rule_refs"] = load_rule_block(role.rule_refs)
    if rule_block:
        context = context + "\n\n" + rule_block

    if getattr(role, "domain", "") == "music":
        skill_block, hints["skill"] = load_genre_skill_block(
            role.name, task, context,
        )
    else:
        skill_block, hints["skill"] = load_skill_block(
            role.name, task, context, code_root=code_root,
        )
    if skill_block:
        context = context + "\n\n" + skill_block

    adapter_block, hints["domain_adapter"] = load_domain_adapter(role.name, domain)
    if adapter_block:
        context = context + adapter_block

    return context, hints
