"""
skill_trigger.py — keyword 触发器机制：skill 自动按需召回 + 相关度排序 + 分级载荷

补 wikilink resolver 的洞：现状 `dev_backend/_load_task_skills` 依赖 task 文本
里的 `[[B?-...]]` 显式声明，但 TL 派活时不会主动写。本模块让 skill 自己声明
trigger（keywords / file_patterns / always），loader 扫 task_text + upstream
files + 项目代码自动召回。

设计要点：
- 与 wikilink 并存，调用方自行 union 去重
- trigger 缺失 = fail-closed（不加载）；**显式声明 > 隐式**
- skill 文件正文用 `## 核心约束` / `## 详细规则` / `## 反例` / `## 来源` 分段

## 2026-08-17 改造：从「命中即注入一句话」到「按相关度分级注入」

### 改造前的三处实测坏点（138 张 skill 全量测）

1. **载荷层**：`render_triggered_block` 只抽 `## 核心约束`（114 张有，**中位
   111 chars**），`## 详细规则`(102 张) 与 `## 反例`(91 张) —— 真正可执行的知识
   —— **永不进 prompt**。例：`混音师/M1-R&B-频谱能量分配.md` 全文 2234，进
   prompt 的是 119 字论点句「R&B 混音不是人声顶峰独尊…7 个频段各居其位」，
   具体频段数值全在 `## 详细规则` 里，LLM 拿不到。
   这解释了 M4 实战「角色感知 capability 但不 auto invoke」——给口号不给步骤。
2. **排序层**：`discover_role_skills` 返回 `sorted(glob)` = **文件名字典序**，
   `render_triggered_block` 顺序填预算、满了丢弃剩余。实测 `se/UI设计师` 17 张
   里 **12 张被挤掉**，挤掉依据是字母表（`ui_Brandkit` 挤掉 `ui_Motion`），
   与任务内容无关。
3. **判据层**：`match_skill` 首个 keyword 命中就 `return True`，命中 1 个裸泛词
   与命中 8 个精准词是同一个 `True`。"选得准"这件事没有任何机制承载。

### 改造后

- `score_skill` 评估**全部**维度不再短路，产出 `SkillMatch`（含命中明细 + 稀有度）
- `discover_role_skills` 按相关度降序返回（文件名仅作稳定 tiebreak）
- `render_triggered_block` **两遍填预算**：
  第 1 遍每个命中 skill 给「指针载荷」（`## 核心约束`），保证多存 skill 不被惩罚；
  第 2 遍按相关度顺序把指针升级为「完整载荷」（核心约束 + 详细规则 + 反例），
  预算用尽即止。**由预算决定升级几个，不引入 top-N 阈值。**

⚠️ 排序维度与载荷分级**均为纯序数比较，不含任何权重或数字阈值** —— 刻意如此，
避免再造一个「上限 = 现最高值」的循环论证阈值（2026-08-17 复盘）。当时的反面
样本是 `role_auditor.LIMITS["skill_refs_max"] = 5`（= 当时最多的架构师 5 张），
该阈值已于 2026-08-25 随 skill_refs 废弃一并删除 —— **删掉而不是重新定值**，
因为换成扫目录后没有任何数据能支撑「一个角色该有几张 skill」。

⚠️⚠️ 但 `render_triggered_block` 的三个**预算参数**（`max_chars_per_skill` /
`max_chars_per_pointer` / `total_char_budget`）依据全部不成立，已按项目 CLAUDE.md
明标为拍脑袋初值，待编排器构想讨论后按职责重定 —— 详见该函数 docstring。
排序与分级的**正确性不依赖这三个数的取值**（测试全是序数断言），但**多少真知识
进 prompt 直接由它们决定**，所以这是本改造最大的未结项。
"""

from __future__ import annotations

import fnmatch
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .obsidian_io import split_frontmatter


# skill 正文的 canonical 分段（`20-知识/角色技能/**` 实测 138 张里 114 张遵守）
# 指针载荷只取 §核心约束。
_SECTION_CORE = "核心约束"

# 完整载荷 = **全文减样板段**（2026-08-24 由白名单改黑名单）。
#
# 原实现是白名单 `("核心约束", "详细规则", "反例")` + 「三段全不命中才回退全文」。
# 目的一直是「排除 `## 来源` 这种给人看的溯源元信息」，但白名单把目的写成了
# 手段，代价是**只认音乐 skill 的模板**：
#
#   全库 138 张按白名单命中段数分布 —— 0 段 24 张（回退全文，载荷/全文 94%）/
#   **1 段 9 张（16%）** / 2 段 17 张（88%）/ 3 段 88 张（94%）。
#   命中恰好 1 段比完全不命中更糟：后者回退全文，前者只送那一段。
#   而那 9 张全在 se 域 —— SE 工程红线用另一套章节名
#   （`核心约束 / 失败机理 / 强制写法 / 验收 gate / 跨项目证据`），
#   只命中第一个。实测送达率：B1 109/1295=8% · B5 89/1055=8% ·
#   F1 109/1111=10% · B7 11% · B6 14% · TL2 16%。
#   即 `强制写法` 里的代码示例、`验收 gate` 的自审动作从未进过任何 prompt。
#
# 黑名单直接表达原意，且对域封闭（新域自带章节名默认进载荷，不需加特例）。
# 改前/改后模拟：music 104 张 94%→96%（+2%）· se 34 张 86%→**99%**（+16%）。
#
# 名单依据 —— 全库 1149 个唯一章节标题的频次：
#   `来源` h2 × 100（几乎每张都有，全库约定，唯一有频次结论的一条）
#   `历史` × 3 · `历史记录` × 1 · `版本历史` × 1 · `载体演进` × 1 · `后续观察` × 1
#     ↑ 这 5 个是**语义判断**（溯源/沿革元信息），不是频次结论，明标。
# 匹配用**精确相等**而非包含：全库有 `## 来源与失效管理` × 2 是正文
# （「失效管理」是可执行内容），包含匹配会误杀。标题的 `N. ` 编号前缀先剥。
# 只对 h2 生效：`### 历史延续` / `### Ghost Note（鬼音）：密度感的来源` 各 1 处是正文。
_SECTION_BOILERPLATE = frozenset({
    "来源", "历史", "历史记录", "版本历史", "载体演进", "后续观察",
})
_HEADING_RE = re.compile(r"^(#{1,6}) +(.+?)\s*$")
_NUM_PREFIX_RE = re.compile(r"^\d+(?:\.\d+)*[.、]?\s+")


# ── 1. 章节抽取 ────────────────────────────────────────────────────
def extract_core_section(content: str) -> str:
    """从 skill 正文抽取 `## 核心约束` 章节；未命中则回退全文。

    2026-07-18 评审去重：原本地复制了 30 行同款算法（当时为避免引私有
    API）；wikilink._extract_section 已提为公共 extract_section，直接复用。
    """
    from .wikilink import extract_section
    text, hit = extract_section(content, _SECTION_CORE)
    return text if hit else content


def extract_pointer_payload(content: str) -> str:
    """**指针载荷**：让 LLM 知道"这里有张 skill 及其一句话论点"，不给全文。

    优先级：`## 核心约束` → lead-in（跳过首个 H1 标题后、下一个任意级标题之前）
    → 空串（只渲染 skill 名当指针）。

    与 `extract_core_section` 的差异：本函数**不回退全文**。回退全文正是改造前的
    病根 —— 24 张缺 `## 核心约束` 的外部导入 skill（F-* 流派 4 / F-* SE 5 /
    UI设计师 `ui_*` 15）中位 5113 chars，撞 3000 上限后吃满预算，把后面按字典序
    排的 skill 全挤掉。缺结构的 skill 想拿全文，得靠相关度在第 2 遍升级里赢。

    ⚠️ lead-in 只保证"止于下一个标题"，**不保证短** —— 一份通篇无标题的文档
    lead-in 就是全文。量级由调用侧 `max_chars_per_pointer` 兜底（见
    `render_triggered_block`）。此洞由本次 mutation 测试暴露：改造初版按
    `## ` 切，遇到只有 H1 的巨型文档退化成全文，4 张就吃满 12K 预算。
    """
    from .wikilink import extract_section
    text, hit = extract_section(content, _SECTION_CORE)
    if hit:
        return text.strip()
    lead: list[str] = []
    seen_h1 = False
    for line in content.splitlines():
        if line.startswith("# ") and not seen_h1 and not lead:
            seen_h1 = True  # 文件标题，跳过但继续收后面的引言
            continue
        if line.lstrip().startswith("#") and line.lstrip("#").startswith(" "):
            break  # 任意级后续标题即止
        lead.append(line)
    return "\n".join(lead).strip()


def extract_full_payload(content: str) -> str:
    """**完整载荷**：全文减样板段（`_SECTION_BOILERPLATE`，见其上方依据）。

    2026-08-24 由白名单三段拼接改为黑名单剔除 —— 原实现只认音乐 skill 的
    章节模板，SE 工程红线只送 8–16%。改动依据见 `_SECTION_BOILERPLATE`。

    被剔除的 h2 段连同其子段（h3+）一起丢弃；无样板段的文档即全文。
    **代码围栏内的标题不参与切分**（复用 `iter_lines_with_fence_state`，
    2026-08-16 `bf3af04` 的教训 —— SE skill 的 `强制写法` 段全是代码块，
    不识别围栏会把代码里的 `## xxx` 当成真标题）。
    """
    from .wikilink import iter_lines_with_fence_state

    out: list[str] = []
    drop_level = 0                      # >0：正在丢弃一个 h{drop_level} 段
    for line, in_fence in iter_lines_with_fence_state(content.splitlines(keepends=True)):
        if not in_fence:
            m = _HEADING_RE.match(line)
            if m:
                lvl = len(m.group(1))
                if drop_level and lvl <= drop_level:
                    drop_level = 0      # 同级或更高级标题 → 丢弃段结束
                title = _NUM_PREFIX_RE.sub("", m.group(2).strip())
                if not drop_level and lvl == 2 and title in _SECTION_BOILERPLATE:
                    drop_level = lvl
                    continue
        if drop_level:
            continue
        out.append(line)
    return "".join(out).strip()


# ── 2. 单 skill 评分 ───────────────────────────────────────────────
@dataclass(frozen=True)
class SkillMatch:
    """单个 skill 的触发评估结果（命中与否 + 相关度明细）。

    `matched=False` 时 `reason` 携带未命中原因（`"no-trigger"` / `""`），
    与改造前 `match_skill` 的返回契约一致。
    """

    path: Path
    matched: bool
    reason: str
    always: bool = False
    task_hits: tuple[str, ...] = ()
    upstream_hits: tuple[str, ...] = ()
    pattern_hits: tuple[str, ...] = ()
    # 命中 keyword 在**同目录内**被多少张 skill 共同声明的累加值（df sum）。
    # 越小越独特。见 rank_key ③ 的依据。
    task_df_sum: int = 0
    upstream_df_sum: int = 0

    @property
    def rank_key(self) -> tuple[int, ...]:
        """相关度排序键，**越大越相关**；纯序数，无权重无阈值。

        各维度依据（2026-08-17 实测 138 张 skill）：

        ① `always` —— 本模块既定原则「显式声明 > 隐式」（见模块 docstring）。
           作者声明恒适用的 skill（如 `B5-空集守卫`）不应被预算挤掉。
        ② `len(task_hits)` —— 命中不同 keyword 的**个数**。依据：实测污染项的
           特征是只命中 1 个裸泛词（`Soul` / `Folk` / `Roots`），真相关项命中多个。
           这是唯一有实测支撑的主判别维度。
        ③ `-task_df_sum` —— 命中词的稀有度。依据：`music/编曲` 234 条 keyword
           声明只有 64 个不同词，`soul` / `trap-soul` / `rhythm and blues` /
           `new jack swing` 各被 **7 张** skill 共同声明 —— 一个 df=7 的词无法
           区分这 7 张，它是流派标签不是 skill 选择器。df=1 的词才携带区分信息。
           在 `se/后端工程师`/`架构师`/`技术主管`/`music/作词`/`制作人`（df>1 计数
           为 0）该维度是 no-op，故对既有 baseline 零回归。
        ④ `len(pattern_hits)` —— 项目代码里真存在匹配文件，是结构性证据，
           强于文本提及；但排在 keyword 之后，因 file_patterns 通常粗（`**/*.py`）。
        ⑤⑥ upstream 命中及其稀有度 —— task_text 是本次派活的正文，upstream 是
           上游产物，前者更能代表"现在要干什么"，故整体弱于 ②③。
        ⑦ 字面长度 —— 最弱 tiebreak。长词比短词特异（`new jack swing` vs `dub`），
           但这是启发式而非实测结论，故只用于前六维全等时决胜。
        """
        return (
            1 if self.always else 0,
            len(self.task_hits),
            -self.task_df_sum,
            len(self.pattern_hits),
            len(self.upstream_hits),
            -self.upstream_df_sum,
            sum(len(k) for k in self.task_hits),
        )


def _sort_matches(matches: Iterable[SkillMatch]) -> list[SkillMatch]:
    """按相关度降序；全等时按文件名升序（可复现，且保持改造前的字典序语义）。"""
    return sorted(
        matches,
        key=lambda m: (tuple(-v for v in m.rank_key), m.path.name),
    )


def _collect_rel_paths(project_code_root: Path) -> list[str]:
    """项目代码树的相对路径清单（走一遍，供全部 file_patterns 复用）。

    改造前每个 pattern 各 `rglob("*")` 一次（O(patterns × files)）；因当时
    keyword 命中会短路，多数情况不触发。现在要评估全部维度，故先收集一次。
    """
    out: list[str] = []
    try:
        for actual in project_code_root.rglob("*"):
            if actual.is_file():
                out.append(actual.relative_to(project_code_root).as_posix())
    except OSError as e:
        print(f"[skill_trigger] ⚠️ 扫项目代码失败：{e}", file=sys.stderr)
    return out


def score_skill(
    skill_path: Path,
    task_text: str,
    upstream_text: str = "",
    project_code_root: Path | None = None,
    *,
    keyword_df: Counter | None = None,
    rel_paths: list[str] | None = None,
) -> SkillMatch:
    """评估单个 skill 的触发与相关度，**不短路**（改造前首个命中即 return）。

    `keyword_df`：同目录 keyword → 声明该词的 skill 数。由 `discover_role_skills`
    预计算并传入；单独调用时省略 → 每个命中词按 df=1 计（不影响单 skill 判定）。
    `rel_paths`：项目代码相对路径清单，避免每个 pattern 重扫。
    """
    try:
        content = skill_path.read_text(encoding="utf-8")
    except OSError as e:
        print(f"[skill_trigger] ⚠️ 读 {skill_path.name} 失败：{e}", file=sys.stderr)
        return SkillMatch(path=skill_path, matched=False, reason="")

    fm, _ = split_frontmatter(content)
    trigger = fm.get("trigger") if isinstance(fm, dict) else None
    if not isinstance(trigger, dict):
        return SkillMatch(path=skill_path, matched=False, reason="no-trigger")

    always = trigger.get("always") is True

    # ── keyword：task_text 与 upstream_text 分开统计（前者更代表"现在要干什么"）
    task_low = (task_text or "").lower()
    up_low = (upstream_text or "").lower()
    task_hits: list[str] = []
    upstream_hits: list[str] = []
    keywords = trigger.get("keywords") or []
    if isinstance(keywords, list):
        seen: set[str] = set()
        for kw in keywords:
            if not isinstance(kw, str) or not kw.strip():
                continue
            low = kw.lower()
            if low in seen:
                continue
            seen.add(low)
            if low in task_low:
                task_hits.append(kw)
            elif low in up_low:
                # elif：同一个词在两处都出现时只记 task（避免双计虚高）
                upstream_hits.append(kw)

    df = keyword_df or Counter()
    task_df_sum = sum(max(1, df.get(k.lower(), 1)) for k in task_hits)
    upstream_df_sum = sum(max(1, df.get(k.lower(), 1)) for k in upstream_hits)

    # ── file_patterns
    pattern_hits: list[str] = []
    file_patterns = trigger.get("file_patterns") or []
    if isinstance(file_patterns, list) and file_patterns:
        paths = rel_paths
        if paths is None and project_code_root is not None and project_code_root.is_dir():
            paths = _collect_rel_paths(project_code_root)
        if paths:
            for fp in file_patterns:
                if not isinstance(fp, str):
                    continue
                if any(fnmatch.fnmatch(rel, fp) for rel in paths):
                    pattern_hits.append(fp)

    matched = always or bool(task_hits) or bool(upstream_hits) or bool(pattern_hits)
    if not matched:
        return SkillMatch(path=skill_path, matched=False, reason="")

    # reason 保持改造前格式（`always` / `keyword:X` / `file_pattern:X`），
    # 既有测试与 render 出的 `auto-trigger:...` 标记依赖它。
    if always:
        reason = "always"
    elif task_hits or upstream_hits:
        first = (task_hits or upstream_hits)[0]
        reason = f"keyword:{first}"
    else:
        reason = f"file_pattern:{pattern_hits[0]}"

    return SkillMatch(
        path=skill_path,
        matched=True,
        reason=reason,
        always=always,
        task_hits=tuple(task_hits),
        upstream_hits=tuple(upstream_hits),
        pattern_hits=tuple(pattern_hits),
        task_df_sum=task_df_sum,
        upstream_df_sum=upstream_df_sum,
    )


def match_skill(
    skill_path: Path,
    task_text: str,
    upstream_text: str = "",
    project_code_root: Path | None = None,
) -> tuple[bool, str]:
    """判断单个 skill 是否被触发，返回 (命中, 触发原因日志)。

    向后兼容包装：语义与改造前一致（优先级 always > keywords > file_patterns，
    任一命中即触发；trigger 缺失 = fail-closed）。需要相关度明细请用 `score_skill`。
    """
    m = score_skill(skill_path, task_text, upstream_text, project_code_root)
    return m.matched, m.reason


# ── 3. 角色目录扫描 ────────────────────────────────────────────────
def _skill_files(role_dir: Path) -> list[Path]:
    return [
        p for p in sorted(role_dir.glob("*.md"))
        if not p.name.startswith(".") and not p.name.startswith("_")
    ]


def _keyword_df(files: Iterable[Path]) -> Counter:
    """同目录 keyword 的 document frequency：词 → 声明该词的 skill 数。

    用于 rank_key ③ 稀有度。实测 `music/编曲` 有 34 个 df>1 的词（`soul` ×7），
    `se/后端工程师` 等目录 df>1 计数为 0。
    """
    df: Counter = Counter()
    for p in files:
        try:
            fm, _ = split_frontmatter(p.read_text(encoding="utf-8"))
        except OSError:
            continue
        trigger = fm.get("trigger") if isinstance(fm, dict) else None
        if not isinstance(trigger, dict):
            continue
        kws = trigger.get("keywords") or []
        if not isinstance(kws, list):
            continue
        for low in {k.lower() for k in kws if isinstance(k, str) and k.strip()}:
            df[low] += 1
    return df


def discover_role_skills_scored(
    role_dir: Path,
    task_text: str,
    upstream_text: str = "",
    project_code_root: Path | None = None,
) -> list[SkillMatch]:
    """扫 role_dir 下所有 *.md，返回命中的 `SkillMatch`，**按相关度降序**。

    role_dir 不存在或为空 → 返回空列表（不抛错，便于跨项目跑）。
    """
    if not role_dir.is_dir():
        return []
    files = _skill_files(role_dir)
    if not files:
        return []

    df = _keyword_df(files)
    rel_paths: list[str] | None = None
    if project_code_root is not None and project_code_root.is_dir():
        rel_paths = _collect_rel_paths(project_code_root)

    matches = [
        m for m in (
            score_skill(
                p, task_text, upstream_text, project_code_root,
                keyword_df=df, rel_paths=rel_paths,
            )
            for p in files
        )
        if m.matched
    ]
    return _sort_matches(matches)


def discover_role_skills(
    role_dir: Path,
    task_text: str,
    upstream_text: str = "",
    project_code_root: Path | None = None,
) -> list[tuple[Path, str]]:
    """扫 role_dir 下所有 *.md，按 frontmatter.trigger 过滤命中的 skill。

    返回 [(skill_path, 触发原因), ...]。**2026-08-17 起按相关度降序**（改造前为
    文件名字典序）；相关度全等时仍按文件名升序，保证可复现。
    调用方若需相关度明细（用于分级载荷），请用 `discover_role_skills_scored`。
    """
    return [
        (m.path, m.reason)
        for m in discover_role_skills_scored(
            role_dir, task_text, upstream_text, project_code_root,
        )
    ]


# ── 4. 渲染 skill block（拼成可注入 prompt 的文本）────────────────
def _as_matches(hits: Iterable[tuple[Path, str] | SkillMatch]) -> list[SkillMatch]:
    """兼容两种入参：`SkillMatch`（带相关度）或 `(path, reason)` 2-tuple。

    2-tuple 来自改造前的调用方 —— 此时**保持传入顺序**当作相关度顺序
    （`discover_role_skills` 已排好），不再重排。
    """
    out: list[SkillMatch] = []
    for h in hits:
        if isinstance(h, SkillMatch):
            out.append(h)
        else:
            path, reason = h
            out.append(SkillMatch(path=path, matched=True, reason=reason))
    return out


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n…（截断：原文 {len(text)} 字符，本次取前 {limit}）"


def render_triggered_block(
    hits: Iterable[tuple[Path, str] | SkillMatch],
    *,
    max_chars_per_skill: int = 3000,
    max_chars_per_pointer: int = 500,
    total_char_budget: int = 12_000,
) -> tuple[str, list[str]]:
    """把命中结果渲染成可注入 prompt 的段落，**按相关度分级填预算**。

    两遍填充（2026-08-17 改造）：

    - **第 1 遍 · 指针**：每个命中 skill 给 `## 核心约束`（实测中位 111 chars）。
      保证"角色技能存得多"不被惩罚 —— 22 张 × ~111 ≈ 2.4K，远低于预算。
      第 1 遍就超预算时按相关度截断（保高分）+ stderr 警告。
    - **第 2 遍 · 升级**：按相关度顺序把指针换成完整载荷（核心约束 + 详细规则
      + 反例），装得下就升级，预算用尽即止。**升级几个由预算决定，不设 top-N。**

    ⚠️⚠️ 三个预算参数的依据**全部不成立**，2026-08-17 用户当场驳回。原文与问题：

    - `max_chars_per_skill=3000` / `total_char_budget=12_000`
      原写「沿用改造前既有值，**非本次新增**」——**「既有」不是依据**，与本轮刚
      批倒的 `skill_refs_max = 现最高值` 是同一个循环论证（该阈值已于 2026-08-25
      删除，见模块 docstring）。
      而且更糟：两遍填充让**预算直接决定多少真知识进 prompt**，本改造对这两个数的
      依赖比改造前重得多，却用"非本次新增"把它豁免了审查。来源尚未核实。
    - `max_chars_per_pointer=500`
      原写「数据溯源：114 张 `## 核心约束` 实测 min=42 / 中位=110 / p90=206 /
      p99=298 / max=323，超 500 的 0 张，500 留 54% 余量」。两处硬伤：
      ① **测错了群体** —— 本参数的职责是兜住「无结构文档 lead-in 退化成全文」
         （见 `extract_pointer_payload` 警告），而 114 张有 `## 核心约束` 的
         恰恰是**永不触发这个界**的那批。用不命中的 population 校准另一批的阈值，
         与本轮修掉的 `frontmatter 800`（概念错配）/ `body_no_dynamic`（口径错配）
         同族。
      ② **「54% 余量」无来源** —— 为什么不是 350 / 400 / 1000？因为 500 是整数。
         这是拍脑袋套了一层真实测量当外衣。

    故按项目 CLAUDE.md 第三种合法形式**明标为拍脑袋初值**：
        三值均为初值，无有效依据支持；不阻塞本次改造落地（改造的正确性由
        `tests/engine/test_skill_relevance_tiering.py` 的序数断言保证，不依赖
        这三个数的具体取值），但**必须在编排器整体构想讨论后按职责重定**
        （2026-08-18 排期，已挂 98-待办）。

    重定方向（待讨论，勿当结论）：指针阶段的开销本质是 `命中数 × 指针上限`，
    应当从 `total_char_budget` **推导**而非另设常量 —— 例如「指针阶段最多占预算
    的 1/N，故 pointer 上限 = budget // (N × 命中数)」，这样只剩一个可论证的
    策略比例，不再有独立的绝对值。

    返回 (skill_block, loaded_stems)；loaded_stems 按相关度顺序。
    """
    matches = _as_matches(hits)
    if not matches:
        return "", []

    # ── 读盘一次，算出两级载荷 ────────────────────────────────
    ptr: dict[Path, str] = {}
    full: dict[Path, str] = {}
    usable: list[SkillMatch] = []
    for m in matches:
        try:
            raw = m.path.read_text(encoding="utf-8")
        except OSError as e:
            print(f"[skill_trigger] ⚠️ 读 {m.path.name} 失败：{e}", file=sys.stderr)
            continue
        _, body = split_frontmatter(raw)
        # 两级载荷各有上限。指针必须用**更紧**的 max_chars_per_pointer：
        # 否则一份无结构文档的 lead-in（= 全文）只被 3000 卡住，4 张就吃满
        # 12K 预算 —— 正是改造前 UI设计师 12/17 被挤掉的机制原封不动搬过来。
        ptr[m.path] = _clip(
            extract_pointer_payload(body), min(max_chars_per_pointer, max_chars_per_skill),
        )
        full[m.path] = _clip(extract_full_payload(body), max_chars_per_skill)
        usable.append(m)
    if not usable:
        return "", []

    # ── 第 1 遍：指针 ────────────────────────────────────────
    chosen: list[SkillMatch] = []
    used = 0
    skipped: list[str] = []
    for m in usable:
        cost = len(ptr[m.path])
        if used + cost > total_char_budget:
            skipped.append(m.path.stem)
            continue
        chosen.append(m)
        used += cost
    if skipped:
        print(
            f"[skill_trigger] ⚠️ total_char_budget={total_char_budget} 在指针阶段用满，"
            f"跳过 {len(skipped)} 个低相关 skill：{', '.join(skipped)}",
            file=sys.stderr,
        )
    if not chosen:
        return "", []

    # ── 第 2 遍：按相关度升级为完整载荷 ──────────────────────
    upgraded: set[Path] = set()
    for m in chosen:
        delta = len(full[m.path]) - len(ptr[m.path])
        if delta <= 0:
            upgraded.add(m.path)  # 完整载荷不比指针大（无 详细规则/反例）
            continue
        if used + delta > total_char_budget:
            continue
        upgraded.add(m.path)
        used += delta

    # ── 不许静默：升级名单在「相关度完全相同」的一组里被预算切断时告警 ──
    # 这种切断只能由文件名字典序决胜，等于任意选择。危害不对称：改造前落选者
    # 各损失 ~120 字论点句，改造后落选者损失整份细则、当选者拿到的可能是错的
    # —— 分级载荷放大了选择错误的代价。
    # 实测触发场景：`music/编曲` 7 张 R&B skill 的 rank_key 全等
    # `(0, 1, -7, 0, 0, 0, 4)`（都只命中 `Soul`，df=7），嘻哈任务下 Ar1/Ar2/Ar3
    # 拿到完整载荷纯属字典序靠前。根因是数据侧：76/131 张 skill 没有任何独有
    # keyword（见 tests/engine/test_skill_relevance_tiering.py 数据守卫）。
    tie_groups: dict[tuple[int, ...], list[SkillMatch]] = {}
    for m in chosen:
        tie_groups.setdefault(m.rank_key, []).append(m)
    for key, group in tie_groups.items():
        if len(group) < 2:
            continue
        got = [g.path.stem for g in group if g.path in upgraded]
        lost = [g.path.stem for g in group if g.path not in upgraded]
        if got and lost:
            print(
                f"[skill_trigger] ⚠️ 相关度全等的 {len(group)} 张 skill 被预算切断，"
                f"完整载荷归属由文件名字典序决定（= 任意选择）：\n"
                f"    rank_key={key}\n"
                f"    得完整载荷：{', '.join(got)}\n"
                f"    仅得指针：{', '.join(lost)}\n"
                f"    → 根因是这些 skill 没有互相区分的 keyword，"
                f"请给每张补至少 1 个本目录独有的任务性 keyword。",
                file=sys.stderr,
            )

    # ── 渲染 ─────────────────────────────────────────────────
    parts: list[str] = []
    loaded_stems: list[str] = []
    for m in chosen:
        is_full = m.path in upgraded
        payload = full[m.path] if is_full else ptr[m.path]
        tier = "full" if is_full else "pointer"
        head = f"=== Skill (auto-trigger:{m.reason} · {tier}): [[{m.path.stem}]] ==="
        parts.append(f"{head}\n{payload}" if payload else head)
        loaded_stems.append(m.path.stem)

    n_full = len(upgraded & {m.path for m in chosen})
    skill_block = (
        "\n\n## 自动触发技能（按 frontmatter.trigger 命中，按相关度排序）\n\n"
        f"<!-- {len(chosen)} 张命中：{n_full} 张完整载荷 / "
        f"{len(chosen) - n_full} 张仅核心约束；用 {used}/{total_char_budget} chars -->\n\n"
        + "\n\n".join(parts)
        + "\n"
    )
    return skill_block, loaded_stems
