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

system 侧机制**不在**本模块：capability_refs 摘要（build_system_prompt 内）
位置语义不同，保持原地。（另一个曾在此列的 skill_refs 已于 2026-08-25 废弃 ——
实测 0/14 生效，见 role_loader._warn_deprecated_skill_refs 的依据段。）

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
# music skill 命名格式：`{前缀}{数字}-{流派}-{标题}`，前缀 R/M/Ma/V/Ar/C/L/Pr/D
# 之一。filter 在 wl.target 上匹配。**流派名不硬编码** —— 见 _music_genre_names。
import re as _re  # noqa: E402

_MUSIC_SKILL_PREFIXES = "R|M|Ma|V|Ar|C|L|Pr|D"


def _music_domain_root(domain: str = "music"):
    from engine import VAULT_ROOT
    return VAULT_ROOT / "20-知识" / "角色技能" / domain


def _music_genre_names(domain: str = "music") -> tuple[str, ...]:
    """域根 `F-{流派}.md` 派生的流派名，长者优先（正则 alternation 需要）。

    2026-08-26 改为派生。原为硬编码 `(?:R&B|R%26B|民谣|雷鬼)`，而 `F-国风.md`
    与 13 张国风角色技能 2026-06-14 就位后**正则没跟** —— 实测 100 张 music
    技能里 14 张不匹配，其中 13 张全是国风（覆盖 9 个角色里的 7 个）。后果是
    上游显式点名 `[[Ar1-国风-子流派演化谱系]]` 会被 filter 静默丢掉，只能回落
    keyword 竞争、拿 pointer 而非 full 载荷。派生之后「往域根加一份 primitive」
    即自动带上该流派，不必再改引擎（封闭形式，见 CLAUDE.md 约定）。

    不加缓存：每次一个小目录 glob，相对同一次调用里的多次文件读盘可忽略；
    而缓存会让「刚加的 F-* 本进程内不生效」变成一种新的静默失效。
    """
    names: set[str] = set()
    try:
        for p in _music_domain_root(domain).glob("F-*.md"):
            if p.name.startswith("."):
                continue
            genre = p.stem[2:]
            if not genre:
                continue
            names.add(genre)
            if "&" in genre:
                # Obsidian 在部分场景把 `&` 写成 `%26`（原硬编码里的 R%26B 即此）
                names.add(genre.replace("&", "%26"))
    except OSError:
        pass
    return tuple(sorted(names, key=len, reverse=True))


def _music_skill_re(domain: str = "music"):
    """`{前缀}{数字}-{流派}-` 匹配器。域根无 primitive → None。

    返回 None 时调用方应**跳过 wikilink 路径**而不是放行全部：music 通道的
    命名过滤语义是「只认本域命名规范的技能」，放行全部会把 se 域的 skill
    也拽进来（`load_skill_block` 才是那个"不过滤"的通道）。
    """
    genres = _music_genre_names(domain)
    if not genres:
        return None
    alt = "|".join(_re.escape(g) for g in genres)
    return _re.compile(rf"(?:^|/)({_MUSIC_SKILL_PREFIXES})\d+-(?:{alt})-")


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
        VAULT_ROOT, discover_role_skills_scored, render_triggered_block,
        expand_wikilinks, extract_full_payload,
    )
    from engine.obsidian_io import split_frontmatter

    role_dir = VAULT_ROOT / "20-知识" / "角色技能" / domain / role_name
    if not role_dir.is_dir():
        return "", f"skill 目录不存在：{role_dir}"

    wikilink_parts: list[str] = []
    wikilink_loaded: list[str] = []
    wikilink_unresolved: list[str] = []
    haystack = (task_text or "") + "\n" + (upstream_text or "")
    skill_re = _music_skill_re(domain)
    if skill_re is None:
        msg = (f"域根 {_music_domain_root(domain)} 无 F-*.md，无法派生流派名 → "
               f"wikilink 路径本轮跳过（仅走 keyword）")
        print(f"[load_genre_skill_block:{role_name}] ⚠️ {msg}。", file=sys.stderr)
        _warn_audit("genre_skill_wikilink", role_name, msg)
    if haystack.strip() and skill_re is not None:
        try:
            result = expand_wikilinks(
                haystack,
                filter=lambda wl: bool(skill_re.search(wl.target)),
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
                # 2026-08-17：wikilink 显式点名 = 最强相关度信号 → 完整载荷。
                # 见 load_skill_block 同位注释。
                core = extract_full_payload(body).strip()
                if len(core) > 3000:
                    core = core[:3000] + (
                        f"\n\n…（截断：原文 {len(core)} 字符，本次取前 3000）"
                    )
                wikilink_parts.append(
                    f"=== Skill (wikilink:[[{e.wikilink.target}]] · full) ===\n{core}"
                )
                wikilink_loaded.append(e.path.stem)
            wikilink_unresolved = list(result.unresolved)
        except Exception as exc:
            msg = f"wikilink 展开失败（{type(exc).__name__}: {exc}），仅走 keyword 路径"
            print(f"[load_genre_skill_block:{role_name}] ⚠️ {msg}。", file=sys.stderr)
            _warn_audit("genre_skill_wikilink", role_name, msg)

    # scored 版保留相关度明细（改造前的 2-tuple 会在去重这步丢掉排序信息）。
    _scored = discover_role_skills_scored(role_dir, task_text, upstream_text)
    _loaded = set(wikilink_loaded)
    dedup_hits = [m for m in _scored if m.path.stem not in _loaded]
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


# ── 流派 primitive 独立通道（music 域 · 2026-08-26）─────────────────────
#
# ## 为什么是独立通道而不是塞进 load_genre_skill_block
#
# primitive（`F-{流派}.md`）与角色技能（`{前缀}{数字}-{流派}-{标题}.md`）是两种
# 东西：前者是**流派 idiom 卡 + 该流派全部角色技能的真实索引**，后者是工程细则。
# 让它们抢同一个 `total_char_budget=12_000`，实测代价是 34 次 full→pointer 降级
# （2026-08-26 量 9 角色全开）／ 7 次（限定 PRD §11.4 的 3 角色范围）。每份
# primitive 4500-6772 char，两份就吃掉那个预算的一半以上 —— 挤掉的正是本项目
# 真正需要的角色技能。故本通道自带配额，与角色技能通道互不侵占。
#
# ## 为什么之前一份都进不了 prompt
#
# 设计意图见 [[音乐制作域-Phase1-PRD]] §11.4：简报 §3 配比 → keyword 命中 →
# 只加载点到的那几个 primitive → 角色读 idiom 按配比组装。但四份 primitive 都在
# `20-知识/角色技能/music/` **域根**，而三条路全堵：
#   ① keyword：`discover_role_skills_scored(role_dir)` 只扫 `{域}/{角色}/`，不扫域根
#   ② wikilink 正则：`F-民谣` 无数字、`F` 不在前缀表（已由 _music_skill_re 分离修）
#   ③ wikilink 目录：`if e.path.parent != role_dir: continue`
# 成因有据：`角色-制作人.md` v0.3.0（2026-07-11）「拆 rule_refs 里 3 条 F-* 硬编码，
# 流派 skill 全部改走 skill_trigger keyword 通道」—— 那次迁移拆掉了能用的通道，
# 换上一条对这个文件位置无效的通道，且无任何告警。典型「沉默失效」。
#
# 连带后果：primitive 的索引节明写「音乐总监 / 制作人派活时**只能从本表中挑
# skill wikilink 写入指令文档，禁止编造文件名**」—— 这句约束一直没到过总监手上。
# 实测 `成为父亲那年` 7 份指令 0 条技能点名，全链退化成 keyword 猜。

# 配额依据（2026-08-26 实测四份 primitive 的「必需三块」体量，去重后）：
#   F-R&B 6772 ／ F-国风 6419 ／ F-雷鬼 5244 ／ F-民谣 3715
#   单份最大 6772 → 上限 7000（+3.4% 余量，够容纳同量级的新流派）
#   两份最坏 6772+6419=13191 → 总额 14000
# 实测 7 个走流程项目**全是 1-2 流派**；≥3 流派简报 schema 另有附加要求，
# 三份最坏 18435 会越额 → 告警并截，不静默。
# （对比：render_triggered_block 那三个预算参数的 docstring 自陈「拍脑袋初值，
#   无有效依据」。这里的两个数有实测来源，不要在后续改动中把它们混为一谈。）
#
# ⚠️ **调大 TOTAL_PRIMITIVE_BUDGET 会降低召回精度** —— 这是个真实耦合，不是巧合：
# 2026-08-26 实测 18 个（项目 × 消费角色）组合，注入结果 18/18 等于简报声明的流派，
# 但拆开看只有 **14 个是 keyword 召回本身就准**，另 **4 个（湖向 作曲/编曲、
# 纸飞机 作曲/编曲）是召回多了一份、恰好被本额度砍掉**。根因是上游产物用
# **否定语境**提到别的流派名，而 keyword 是纯子串匹配、读不出「不要这个」：
#   湖向（R&B+国风）vision 的「反锚点（明确避开）」写「❌ 一类 city folk 抒情（民谣骨架，
#     不是我们要的 R&B groove）」→ 命中 `folk` / `民谣` → 召回 F-民谣
#   纸飞机（民谣+R&B）黑名单表格写「Dub 风格延时 | 儿歌要近距亲密感」→ 命中 `Dub`
#     → 召回 F-雷鬼
# 那 4 例里错流派的 rank_key 恰好低于真流派，所以被砍的是错的那份。**这不保证**：
# 若某轮上游把错流派提得更密，它就会排到前面、把真流派挤掉。
# 想要确定性，正解是在简报 §3 写 `primitive_refs` 显式点名（走 wikilink 路径，
# 不参与 keyword 竞争），**不是**把额度调大。
MAX_CHARS_PER_PRIMITIVE = 7000
TOTAL_PRIMITIVE_BUDGET = 14_000

# 「必需三块」的章节标题关键词。**按标题子串匹配、不按编号**：四份 primitive
# 编号风格不一（F-R&B/F-国风 用汉字「一、二、」，F-民谣/F-雷鬼 用「1. 2.」），
# 且 F-国风 在中间插了「子流派演化与年代」使后续编号整体偏移。
_PRIMITIVE_IDIOM_KEYS = ("节奏型", "标志性配器", "调性", "主题", "流派红线")
_PRIMITIVE_FUSION_KEY = "fusion 友好度"
_PRIMITIVE_INDEX_KEY = "工程参考 skill"
# 索引节去重的偏好标记：F-民谣 / F-雷鬼 各有**两个**「工程参考 skill」节
# （旧版只有一句「详见各角色 skill」／新版带「下游消费规则」硬约束）。取新版。
_PRIMITIVE_INDEX_PREFER = "下游消费规则"

# 刻意不取的章节：经典参考 / 子流派演化与年代 / 与 PRD 体系的关系。
# 前两者是意境对齐素材（角色技能里已有更具体的），后者是维护者视角。
# 不取整份也**不按前缀截断**：primitive 把索引节排在最后（F-R&B 在第八节），
# 整份截到 7000 正好把它切掉 —— 而那节是总监挑技能写 wikilink 的唯一依据。


def _primitive_files(domain: str = "music") -> list:
    """域根 `F-*.md`，**非递归**（不进角色子目录）。"""
    root = _music_domain_root(domain)
    if not root.is_dir():
        return []
    return sorted(
        p for p in root.glob("F-*.md")
        if not p.name.startswith(".") and not p.name.startswith("_")
    )


def _h2_sections(text: str) -> list[tuple[str, str]]:
    """按 h2 切段，返回 [(标题, 含标题的整段)]。**围栏内的标题不切**。

    复用 `iter_lines_with_fence_state` —— 2026-08-16 那次「章节抽取无围栏感知」
    实测让音乐域契约注入丢 79%，同一个坑不踩第二次。
    （实查四份 primitive 当前围栏内 0 处 h2，但 primitive 是要长期手写维护的
      文档，加围栏示例是迟早的事，不靠"现在没有"来保证。）
    """
    from engine.wikilink import iter_lines_with_fence_state
    lines = text.splitlines(keepends=True)
    out: list[tuple[str, str]] = []
    title: str | None = None
    buf: list[str] = []
    for line, in_fence in iter_lines_with_fence_state(lines):
        if not in_fence and line.startswith("## "):
            if title is not None:
                out.append((title, "".join(buf)))
            title = line[3:].strip()
            buf = [line]
        elif title is not None:
            buf.append(line)
    if title is not None:
        out.append((title, "".join(buf)))
    return out


def _select_primitive_payload(
    text: str, max_chars: int = MAX_CHARS_PER_PRIMITIVE,
) -> tuple[str, list[str], bool]:
    """取 primitive 的「必需三块」：idiom 五节 + fusion 友好度 + 工程参考 skill 索引。

    返回 (payload, 命中的章节标题, 是否截断)。一节都没命中 → ("", [], False)，
    调用方告警并跳过 —— 一份连 idiom 都抽不出的 primitive 说明章节结构已偏离
    [[流派primitive-schema]]，注入残片比不注入更误导（角色会以为自己拿到了 idiom）。

    **索引节受保护，永不被截**：它在文档里排在最后（F-R&B 在第八节），而截断
    是取前 N 字符 —— 若不单独保住，`max_chars` 一收紧就正好把它切掉。而它是
    整条下游链的起点：节内明写「只能从本表中挑 skill wikilink，禁止编造文件名」，
    总监靠它派活、制作人靠它扇出、下游角色靠由此产生的 wikilink 拿 full 载荷。
    索引节本身体量有界（实测 690-1855 char），保它不会挤走多少 idiom。
    """
    secs = _h2_sections(text)
    if not secs:
        return "", [], False

    body_parts: list[tuple[str, str]] = []
    for title, body in secs:
        if any(k in title for k in _PRIMITIVE_IDIOM_KEYS) or _PRIMITIVE_FUSION_KEY in title:
            body_parts.append((title, body))

    # 索引节可能有多个（F-民谣 / F-雷鬼 各有旧版 + 带「下游消费规则」的新版）
    idx_cands = [(t, b) for t, b in secs if _PRIMITIVE_INDEX_KEY in t]
    index_sec: tuple[str, str] | None = None
    if idx_cands:
        index_sec = next(
            (x for x in idx_cands if _PRIMITIVE_INDEX_PREFER in x[1]), idx_cands[-1],
        )

    if not body_parts and index_sec is None:
        return "", [], False

    index_text = index_sec[1].rstrip() if index_sec else ""
    body_text = "\n".join(b.rstrip() for _, b in body_parts)

    truncated = False
    room = max_chars - len(index_text)
    if room < 0:
        # 索引节本身就超上限：保它、丢 body（宁可只给索引也不给半份 idiom）
        body_text, truncated = "", True
    elif len(body_text) > room:
        body_text = body_text[:room] + (
            f"\n\n…（截断：idiom / fusion 段原 {len(body_text)} 字符，"
            f"本次取前 {room}；索引节受保护未截）")
        truncated = True

    payload = "\n".join(x for x in (body_text, index_text) if x)
    titles = [t for t, _ in body_parts] + ([index_sec[0]] if index_sec else [])
    return payload, titles, truncated


def _primitive_consumers(path) -> tuple[tuple[str, ...], str]:
    """读 primitive 的 `consumed_by`。返回 (消费角色, 问题描述)。

    字段缺失 → 返回 ((), 原因)：**fail-closed 但必告警**。范围收窄是本通道的
    要点（PRD §11.4 只让 音乐总监 / 作曲 / 编曲 读 primitive），所以不能"缺失
    即全放"；但静默跳过会让新加的 primitive 永远不生效 —— 那正是本次要修的
    病，不能在修它的代码里复制一份。
    """
    from engine.obsidian_io import split_frontmatter
    try:
        fm, _ = split_frontmatter(path.read_text(encoding="utf-8"))
    except OSError as e:
        return (), f"读取失败：{e}"
    if not isinstance(fm, dict):
        return (), "无 frontmatter"
    raw = fm.get("consumed_by")
    if raw is None:
        return (), "未声明 consumed_by（本通道 fail-closed：不声明即不注入）"
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return (), f"consumed_by 类型非法（{type(raw).__name__}）"
    names = tuple(str(x).strip() for x in raw if str(x).strip())
    if not names:
        return (), "consumed_by 为空列表"
    return names, ""


def load_genre_primitive_block(
    role_name: str,
    task_text: str,
    upstream_text: str = "",
    domain: str = "music",
) -> tuple[str, str]:
    """流派 primitive 双路径加载，**独立预算**。返回 (block, hint)。

    - 路径 1（显式）：上游文本里的 `[[F-{流派}]]`（简报 §3 `primitive_refs` 字段）
      —— 用户点名要哪几个流派，就只加载那几个，零猜测
    - 路径 2（兜底）：`trigger.keywords` 命中简报里的流派名 / 子流派名 / 代表艺人
      —— 实测音乐总监侧 7/7 恰好等于简报声明的流派（作曲/编曲 7/11，多召回来自
      上游产物里提到的其他流派名）

    只对 `consumed_by` 声明的角色注入。域根无 primitive / 本角色不在消费名单 /
    双路径均空 → ("", 原因)。
    """
    from engine import score_skill
    from engine.skill_trigger import _keyword_df
    from engine.wikilink import parse_wikilinks

    prims = _primitive_files(domain)
    if not prims:
        return "", f"域根无 F-*.md：{_music_domain_root(domain)}"

    # ── 消费角色过滤 ──────────────────────────────────────────
    scoped: list = []
    for p in prims:
        consumers, problem = _primitive_consumers(p)
        if problem:
            msg = f"{p.name} {problem} → 本轮不注入"
            print(f"[load_genre_primitive_block:{role_name}] ⚠️ {msg}", file=sys.stderr)
            _warn_audit("genre_primitive_scope", role_name, msg)
            continue
        if role_name in consumers:
            scoped.append(p)
    if not scoped:
        return "", f"{role_name} 不在任何 primitive 的 consumed_by 名单"

    by_stem = {p.stem: p for p in scoped}

    # ── 路径 1：显式 wikilink（简报 primitive_refs）───────────────
    haystack = (task_text or "") + "\n" + (upstream_text or "")
    explicit: list = []
    for wl in parse_wikilinks(haystack):
        # 只按 stem 匹配本域根的 primitive：不走 resolve_target，避免撞上
        # se 域同名的 `F-前端` / `F-架构`（两域 `F-` 前缀同名不同物 ——
        # se 是 domain_primitive 按角色分、无 trigger；music 是 genre_primitive
        # 按流派分、有 trigger）。
        target = wl.target.rsplit("/", 1)[-1]
        p = by_stem.get(target)
        if p is not None and p not in explicit:
            explicit.append(p)

    # ── 路径 2：keyword 兜底（去掉已显式点名的）─────────────────
    rest = [p for p in scoped if p not in explicit]
    df = _keyword_df(rest)
    hits = [m for m in (score_skill(p, task_text, upstream_text, None, keyword_df=df)
                        for p in rest) if m.matched]
    hits.sort(key=lambda m: (tuple(-v for v in m.rank_key), m.path.name))

    ordered: list[tuple] = [(p, "wikilink") for p in explicit]
    ordered += [(m.path, f"auto-trigger:{m.reason}") for m in hits]
    if not ordered:
        return "", "双路径均空（简报未点名 primitive_refs，且无流派 keyword 命中）"

    # ── 渲染 + 独立预算 ──────────────────────────────────────
    parts: list[str] = []
    loaded: list[str] = []
    dropped: list[str] = []
    used = 0
    for path, via in ordered:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            msg = f"读 {path.name} 失败：{e}"
            print(f"[load_genre_primitive_block:{role_name}] ⚠️ {msg}", file=sys.stderr)
            _warn_audit("genre_primitive_read", role_name, msg)
            continue
        payload, sections, truncated = _select_primitive_payload(text)
        if not payload:
            msg = (f"{path.name} 抽不出「必需三块」任一节（章节结构偏离 "
                   f"流派primitive-schema）→ 跳过，不回退全文")
            print(f"[load_genre_primitive_block:{role_name}] ⚠️ {msg}", file=sys.stderr)
            _warn_audit("genre_primitive_sections", role_name, msg)
            continue
        tier = "truncated" if truncated else "sections"
        if truncated:
            msg = (f"{path.name} 必需三块超单份上限 {MAX_CHARS_PER_PRIMITIVE}，"
                   f"idiom / fusion 段已截（索引节受保护未截）")
            print(f"[load_genre_primitive_block:{role_name}] ⚠️ {msg}", file=sys.stderr)
            _warn_audit("genre_primitive_truncated", role_name, msg)
        if used + len(payload) > TOTAL_PRIMITIVE_BUDGET:
            dropped.append(path.stem)
            continue
        used += len(payload)
        loaded.append(path.stem)
        parts.append(
            f"=== Primitive ({via} · {tier}): [[{path.stem}]] ===\n"
            f"（本节为流派 idiom + fusion 友好度 + 该流派角色技能索引；"
            f"命中章节：{' / '.join(sections)}）\n{payload}"
        )

    if dropped:
        msg = (f"primitive 总额 {TOTAL_PRIMITIVE_BUDGET} 用尽，丢弃 "
               f"{len(dropped)} 份：{dropped}（已注入 {loaded}，共 {used} char）。"
               f"若被丢的是本项目真需要的流派，**不要调大额度**（会同时放宽错流派，"
               f"见常量注释的耦合说明）—— 在简报 §3 写 `primitive_refs: [[F-xxx]]` "
               f"显式点名，走 wikilink 路径不参与 keyword 竞争。"
               f"若被丢的本就是错流派（上游用反锚点 / 黑名单提到它），忽略本条")
        print(f"[load_genre_primitive_block:{role_name}] ⚠️ {msg}", file=sys.stderr)
        _warn_audit("genre_primitive_budget", role_name, msg)

    if not parts:
        return "", "全部 primitive 被跳过或越额丢弃（详见 stderr / audit）"

    block = (
        "\n\n## 流派 primitive（按简报配比加载）\n\n"
        "> 下面每份 primitive 的「工程参考 skill」索引节是**该流派全部角色技能的\n"
        "> 真实清单**。派活写 skill wikilink 时只能从索引里挑，不要编造文件名。\n\n"
        + "\n\n".join(parts)
        + "\n"
    )
    hint = f"注入 {len(loaded)} 份（{'/'.join(loaded)}），共 {used} char"
    if dropped:
        hint += f"；越额丢弃 {len(dropped)} 份（{'/'.join(dropped)}）"
    return block, hint


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
        VAULT_ROOT, discover_role_skills_scored, render_triggered_block,
        expand_wikilinks, extract_full_payload,
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
                # 2026-08-17：wikilink 是**最强相关度信号** —— 任务文本直接点名了
                # 这张 skill，比 keyword 命中确定得多。故给完整载荷（核心约束 +
                # 详细规则 + 反例），不再只给 111 字的 `## 核心约束` 论点句。
                # 上限仍 3000，与改造前一致（body 来自全文读盘，不是 e.content）。
                core = extract_full_payload(body).strip()
                if len(core) > 3000:
                    core = core[:3000] + (
                        f"\n\n…（截断：原文 {len(core)} 字符，本次取前 3000）"
                    )
                wikilink_parts.append(
                    f"=== Skill (wikilink:[[{e.wikilink.target}]] · full) ===\n{core}"
                )
                wikilink_loaded.append(e.path.stem)
            wikilink_unresolved = list(result.unresolved)
        except Exception as exc:
            msg = f"wikilink 展开失败（{type(exc).__name__}: {exc}），仅走 keyword 路径"
            print(f"[load_skill_block:{role_name}] ⚠️ {msg}。", file=sys.stderr)
            _warn_audit("skill_wikilink", role_name, msg)

    # scored 版保留相关度明细，render 侧才能按相关度分级 —— 用 2-tuple 会在
    # 这一步把排序信息丢掉（改造前即如此）。
    _scored = discover_role_skills_scored(
        role_dir, task_text, upstream_text, code_root if code_root is not None else None,
    )
    _loaded = set(wikilink_loaded)
    dedup_hits = [m for m in _scored if m.path.stem not in _loaded]
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
    music 域额外走一遍 primitive 通道（独立预算，按 `consumed_by` 收范围 ——
    2026-08-26 只有 音乐总监 / 作曲 / 编曲 声明消费，其余角色自然跳过）。
    返回 (context, hints)；hints 按机制给一句话（调用方统一打日志）。
    """
    hints: dict[str, str] = {}
    context = base_context

    rule_block, hints["rule_refs"] = load_rule_block(role.rule_refs)
    if rule_block:
        context = context + "\n\n" + rule_block

    if getattr(role, "domain", "") == "music":
        # primitive 先于 skill：它携带该流派角色技能的索引，是挑 skill 的依据。
        # ⚠️ 传 `base_context` 而不是 `context`（= 已拼上 rule_block 的那个）：
        # primitive 的显式路径把上游文本里的 `[[F-xxx]]` 当"用户点名了这个流派"。
        # 而 rule_refs 注入的是**指令文本**，里面的 F-* 是**举例**不是点名 ——
        # 2026-08-27 实测：`产物schema` §9 编曲方案 §5 的硬约束写着「本节列表项必须以
        # `[[F-{流派名}]]` 开头（如 `[[F-民谣]]` / `[[F-雷鬼]]`）」，于是 `纸飞机`
        # （民谣 60% + R&B 40%）的编曲被判定"显式点名了民谣和雷鬼"，F-雷鬼 抢到位置、
        # 真正需要的 F-R&B 被额度挤掉。规则文本不是项目数据，不能当点名依据。
        prim_block, hints["genre_primitive"] = load_genre_primitive_block(
            role.name, task, base_context,
        )
        if prim_block:
            context = context + "\n\n" + prim_block
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
