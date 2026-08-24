"""
tests/engine/test_skill_relevance_tiering.py
    —— skill 按相关度排序 + 分级载荷（2026-08-17 改造）

## 改造前实测的三处坏点（138 张 skill 全量测）

| 层 | 病 | 实测 |
|---|---|---|
| 载荷 | 只抽 `## 核心约束` | 114 张有该段，**中位 111 chars**；`## 详细规则`(102) / `## 反例`(91) 永不进 prompt |
| 载荷 | 缺该段就**回退全文** | 24 张中位 5113 chars，撞 3000 上限后吃满预算 |
| 排序 | `sorted(glob)` 文件名字典序 | `se/UI设计师` 17 张里 **12 张被字母表挤掉** |
| 判据 | 首个 keyword 命中即 `return True` | 命中 1 个裸泛词 == 命中 8 个精准词 |

## 改造后的契约（本文件锁住）

1. `score_skill` 评估全部维度，产出命中明细 + 稀有度，**不短路**
2. `discover_role_skills*` 按相关度降序；全等时文件名升序（可复现）
3. `render_triggered_block` 两遍填预算：先给每个命中 skill 指针载荷，
   再按相关度把指针升级为完整载荷 —— **升级几个由预算决定，无 top-N 阈值**
4. 指针载荷**绝不回退全文**（这是 24 张外部导入 skill 吃满预算的病根）

⚠️ 本文件不含任何数字判定阈值 —— 排序全是序数比较。唯二数字
（`max_chars_per_skill` / `total_char_budget`）沿用改造前既有值，非新增。
"""

from __future__ import annotations

import collections
import os
from pathlib import Path

import pytest

from engine.obsidian_io import split_frontmatter
from engine.skill_trigger import (
    SkillMatch,
    discover_role_skills,
    discover_role_skills_scored,
    extract_full_payload,
    extract_pointer_payload,
    render_triggered_block,
    score_skill,
)

VAULT = Path(os.environ.get("VAULT_ROOT", r"D:\MarkDown\memory\adam"))

# skill 正文的 canonical 四段骨架（`20-知识/角色技能/**` 114/138 遵守）
_BODY = """# {name}

## 核心约束

{core}

## 详细规则

{rules}

## 反例

{anti}

## 来源

内部沉淀，2026-08-17。
"""


def _write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _skill(
    dir_: Path,
    stem: str,
    *,
    keywords: list[str] | None = None,
    always: bool = False,
    file_patterns: list[str] | None = None,
    core: str = "核心一句话。",
    rules: str = "细则甲。细则乙。",
    anti: str = "反例丙。",
    body: str | None = None,
) -> Path:
    fm = ["type: skill", "trigger:"]
    if always:
        fm.append("  always: true")
    if keywords:
        fm.append("  keywords:")
        fm.extend(f"    - {k!r}" for k in keywords)
    if file_patterns:
        fm.append("  file_patterns:")
        fm.extend(f"    - {p!r}" for p in file_patterns)
    text = body if body is not None else _BODY.format(
        name=stem, core=core, rules=rules, anti=anti,
    )
    return _write(dir_ / f"{stem}.md", "---\n" + "\n".join(fm) + "\n---\n\n" + text)


def _order(dir_: Path, task: str, upstream: str = "") -> list[str]:
    return [m.path.stem for m in discover_role_skills_scored(dir_, task, upstream)]


# ══════════════════════════════════════════════════════════════
#  1. score_skill —— 全维度评估，不再短路
# ══════════════════════════════════════════════════════════════

class TestScoreSkillDimensions:
    def test_命中多个keyword全部记录(self, tmp_path: Path):
        """改造前首个命中即 return，命中强度信息全丢。"""
        sk = _skill(tmp_path, "S", keywords=["alpha", "beta", "gamma"])
        m = score_skill(sk, "任务涉及 alpha 与 beta 两项")
        assert set(m.task_hits) == {"alpha", "beta"}
        assert "gamma" not in m.task_hits

    def test_task与upstream命中分开统计(self, tmp_path: Path):
        sk = _skill(tmp_path, "S", keywords=["alpha", "beta"])
        m = score_skill(sk, task_text="做 alpha", upstream_text="上游提到 beta")
        assert m.task_hits == ("alpha",)
        assert m.upstream_hits == ("beta",)

    def test_同词两处出现只记task不双计(self, tmp_path: Path):
        sk = _skill(tmp_path, "S", keywords=["alpha"])
        m = score_skill(sk, task_text="做 alpha", upstream_text="上游也说 alpha")
        assert m.task_hits == ("alpha",)
        assert m.upstream_hits == ()

    def test_keyword去重不重复计数(self, tmp_path: Path):
        """同一个词大小写不同写两遍，只算一次（否则可刷分）。"""
        sk = _skill(tmp_path, "S", keywords=["Alpha", "alpha", "ALPHA"])
        m = score_skill(sk, "做 alpha")
        assert len(m.task_hits) == 1

    def test_keyword与file_pattern可同时命中(self, tmp_path: Path):
        """改造前 keyword 命中就短路，file_pattern 维度永不评估。"""
        code = tmp_path / "code"
        _write(code / "src/app.py", "x = 1")
        sk = _skill(tmp_path / "d", "S", keywords=["alpha"], file_patterns=["**/*.py"])
        m = score_skill(sk, "做 alpha", project_code_root=code)
        assert m.task_hits == ("alpha",)
        assert m.pattern_hits == ("**/*.py",)

    def test_df稀有度进入排序键(self, tmp_path: Path):
        sk = _skill(tmp_path, "S", keywords=["shared"])
        rare = score_skill(sk, "用 shared", keyword_df=collections.Counter({"shared": 1}))
        common = score_skill(sk, "用 shared", keyword_df=collections.Counter({"shared": 9}))
        assert rare.task_df_sum == 1 and common.task_df_sum == 9
        assert rare.rank_key > common.rank_key  # 稀有词更相关

    def test_未命中与无trigger的返回契约不变(self, tmp_path: Path):
        miss = score_skill(_skill(tmp_path, "A", keywords=["redis"]), "本地文件")
        assert miss.matched is False and miss.reason == ""
        bare = _write(tmp_path / "B.md", "---\ntype: skill\n---\n\n正文\n")
        assert score_skill(bare, "任意").reason == "no-trigger"


# ══════════════════════════════════════════════════════════════
#  2. 排序 —— 纯序数，无阈值
# ══════════════════════════════════════════════════════════════

class TestRanking:
    def test_命中词多的排前(self, tmp_path: Path):
        d = tmp_path / "r"
        _skill(d, "Z-one", keywords=["alpha"])
        _skill(d, "A-three", keywords=["alpha", "beta", "gamma"])
        # 文件名字典序会把 A-three 排前 —— 故意让相关度与字典序同向不可区分，
        # 改用 Z/A 反向命名验证真的是按相关度而非文件名。
        assert _order(d, "alpha beta gamma 都涉及")[0] == "A-three"
        _skill(d, "A-zero", keywords=["alpha"])
        order = _order(d, "alpha beta gamma 都涉及")
        assert order[0] == "A-three", f"命中 3 词的应排首位，实得 {order}"

    def test_命中数相同时稀有词排前(self, tmp_path: Path):
        """污染场景核心：`soul` 被 7 张 skill 共同声明 → df 高 → 不携带区分信息。

        实测依据：`music/编曲` 234 条 keyword 声明只有 64 个不同词，
        `soul` / `trap-soul` / `rhythm and blues` 各被 7 张 skill 声明。
        """
        d = tmp_path / "r"
        # generic 被 3 张声明（df=3）；specific 只被 1 张声明（df=1）
        _skill(d, "A-generic1", keywords=["generic"])
        _skill(d, "A-generic2", keywords=["generic"])
        _skill(d, "A-generic3", keywords=["generic"])
        _skill(d, "Z-specific", keywords=["specific"])
        order = _order(d, "任务同时提到 generic 和 specific")
        assert order[0] == "Z-specific", (
            f"df=1 的独有词应排在 df=3 的共享词之前，实得 {order}"
        )

    def test_always排最前(self, tmp_path: Path):
        """依据：模块既定原则「显式声明 > 隐式」；作者声明恒适用的不该被预算挤掉。"""
        d = tmp_path / "r"
        _skill(d, "Z-always", always=True)
        _skill(d, "A-kw", keywords=["alpha", "beta"])
        assert _order(d, "alpha beta")[0] == "Z-always"

    def test_task命中强于upstream命中(self, tmp_path: Path):
        d = tmp_path / "r"
        _skill(d, "A-up", keywords=["upword"])
        _skill(d, "Z-task", keywords=["taskword"])
        order = _order(d, task="做 taskword", upstream="上游 upword")
        assert order[0] == "Z-task", f"task 命中应强于 upstream，实得 {order}"

    def test_相关度全等时按文件名升序(self, tmp_path: Path):
        """可复现性：保持改造前的字典序语义作为最终 tiebreak。"""
        d = tmp_path / "r"
        for stem in ("B6", "B1", "B5"):
            _skill(d, stem, always=True)
        assert _order(d, "任意") == ["B1", "B5", "B6"]

    def test_兼容版返回相同顺序(self, tmp_path: Path):
        d = tmp_path / "r"
        _skill(d, "A-one", keywords=["alpha"])
        _skill(d, "Z-three", keywords=["alpha", "beta", "gamma"])
        task = "alpha beta gamma"
        assert [p.stem for p, _ in discover_role_skills(d, task)] == _order(d, task)


# ══════════════════════════════════════════════════════════════
#  3. 分级载荷
# ══════════════════════════════════════════════════════════════

class TestPayloadTiers:
    def test_指针取核心约束(self):
        body = _BODY.format(name="S", core="就这一句。", rules="细则。", anti="反例。")
        ptr = extract_pointer_payload(body)
        assert "就这一句" in ptr
        assert "细则" not in ptr and "反例" not in ptr

    def test_指针无核心约束时回lead_in(self):
        body = "# S 标题\n\n一句话定位。\n\n## 详细规则\n\n大量细则……\n"
        ptr = extract_pointer_payload(body)
        assert "一句话定位" in ptr
        assert "大量细则" not in ptr

    def test_指针绝不回退全文(self):
        """关键回归：改造前 `extract_core_section` 回退全文，24 张外部导入 skill
        中位 5113 chars 直接吃满预算，把后面按字典序排的全挤掉。"""
        body = "## 别的章节\n\n" + "很长的正文。" * 500
        ptr = extract_pointer_payload(body)
        assert len(ptr) < 200, f"指针载荷不得回退全文，实得 {len(ptr)} chars"

    def test_完整载荷拼三段(self):
        body = _BODY.format(name="S", core="核心。", rules="细则。", anti="反例。")
        full = extract_full_payload(body)
        assert "核心。" in full and "细则。" in full and "反例。" in full

    def test_完整载荷不含来源段(self):
        """`## 来源` 是给人看的溯源元信息（100 张有），不是给 LLM 的可执行知识。"""
        body = _BODY.format(name="S", core="核心。", rules="细则。", anti="反例。")
        assert "内部沉淀" not in extract_full_payload(body)

    def test_完整载荷无结构时回退全文(self):
        body = "# S\n\n## 随便一段\n\n仅此而已。\n"
        assert "仅此而已" in extract_full_payload(body)

    # ── 2026-08-24 白名单改黑名单（见 _SECTION_BOILERPLATE 上方依据）──
    def test_完整载荷保留非模板章节名(self):
        """核心回归：SE 工程红线用另一套章节名，白名单时代只送 8–16%。

        `强制写法`（代码示例）与 `验收 gate`（自审动作）必须在载荷里 ——
        这两段丢了，B1/B5/B6/B7/F1/TL1/TL2 就只剩标题加一句话。
        """
        body = (
            "# B1 — 环境变量必须运行期读取\n\n"
            "## 核心约束\n\n模块级快照即违规。\n\n"
            "## 失败机理\n\nimport 时点快照。\n\n"
            "## 强制写法\n\n```python\nos.environ['X']\n```\n\n"
            "## 验收 gate（双重）\n\ngrep 自审。\n\n"
            "## 跨项目证据\n\nhuashu-demo。\n\n"
            "## 来源\n\n内部沉淀。\n"
        )
        full = extract_full_payload(body)
        for kept in ("模块级快照即违规", "import 时点快照", "os.environ",
                     "grep 自审", "huashu-demo", "B1 — 环境变量"):
            assert kept in full, f"丢了应保留的内容：{kept}"
        assert "内部沉淀" not in full, "`## 来源` 仍须剔除"

    def test_样板段的子段一并剔除(self):
        body = "# S\n\n## 正文\n\nA\n\n## 来源\n\nB\n\n### 二手来源\n\nC\n"
        full = extract_full_payload(body)
        assert "A" in full
        assert "B" not in full and "C" not in full

    def test_精确匹配不误杀含关键词的正文段(self):
        """全库有 `## 来源与失效管理` × 2 —— 「失效管理」是可执行内容。

        包含匹配会连它一起剔除，故用精确相等 + 剥 `N. ` 编号前缀。
        """
        body = "# S\n\n## 来源与失效管理\n\n失效后回滚。\n\n## 7. 版本历史\n\nv1.0\n"
        full = extract_full_payload(body)
        assert "失效后回滚" in full, "`来源与失效管理` 是正文，不得剔除"
        assert "v1.0" not in full, "`7. 版本历史` 剥编号后应命中样板名单"

    def test_代码围栏内的标题不参与切分(self):
        """`bf3af04` 的教训：SE skill 的 `强制写法` 段全是代码块。

        围栏里写 `## 来源` 只是示例文本，不能触发剔除。
        """
        body = (
            "# S\n\n## 强制写法\n\n"
            "```markdown\n## 来源\n这行在围栏里\n```\n\n"
            "围栏后的正文。\n"
        )
        full = extract_full_payload(body)
        assert "这行在围栏里" in full and "围栏后的正文" in full

    def test_h3_同名不剔除(self):
        """全库 `### 历史延续` / `### …密度感的来源` 各 1 处是正文 → 只对 h2 生效。"""
        body = "# S\n\n## 详细规则\n\n### 历史延续\n\n保留我。\n"
        assert "保留我" in extract_full_payload(body)


# ══════════════════════════════════════════════════════════════
#  4. 两遍填预算
# ══════════════════════════════════════════════════════════════

class TestTwoPassBudget:
    def test_全部命中都至少拿到指针(self, tmp_path: Path):
        """目标：技能池存得多不被惩罚。改造前预算满了后面整张丢弃。

        规模刻意选成「完整载荷装不下、指针装得下」：12 张 × 细则 1800 chars
        ≈ 21.6K > 12K 预算，而 12 张指针 ≈ 0.3K。若第 1 遍用完整载荷（改造前
        行为）就只装得下约 6 张 —— 本断言即失败。经 mutation 验证。
        """
        d = tmp_path / "r"
        for i in range(12):
            _skill(d, f"S{i:02d}", always=True, core=f"论点 {i}。", rules="细则。" * 600)
        hits = discover_role_skills_scored(d, "任意")
        block, loaded = render_triggered_block(hits)
        assert len(loaded) == 12, f"12 张命中应全部至少有指针，实得 {len(loaded)}"
        for i in range(12):
            assert f"论点 {i}。" in block

    def test_高相关拿完整低相关只拿指针(self, tmp_path: Path):
        d = tmp_path / "r"
        # 每张细则 ~2400 chars → 12000 预算只够升级少数几张
        for i in range(8):
            _skill(d, f"S{i}", keywords=[f"k{i}"], core=f"论点 {i}。", rules="细则。" * 400)
        # 只有 S0 命中，其余靠 always... 改用命中数造相关度差
        d2 = tmp_path / "r2"
        _skill(d2, "HIGH", keywords=["a", "b", "c"], core="高相关论点。", rules="高细则。" * 400)
        _skill(d2, "LOW", keywords=["a"], core="低相关论点。", rules="低细则。" * 400)
        block, loaded = render_triggered_block(
            discover_role_skills_scored(d2, "a b c"), total_char_budget=2000,
        )
        assert loaded == ["HIGH", "LOW"]
        assert "高细则。" in block, "高相关的应升级为完整载荷"
        assert "低细则。" not in block, "低相关的应停在指针"
        assert "低相关论点。" in block, "低相关的指针必须仍在"

    def test_缺核心约束的大skill不再挤掉后续(self, tmp_path: Path):
        """UI设计师 12/17 被字典序挤掉的直接回归。

        按真实规模复刻：`se/UI设计师` 17 张里 15 张缺 `## 核心约束`，中位 5113
        chars。改造前它们回退全文 → 各撞 3000 上限 → **4 张就吃满 12000 预算** →
        字典序在后的全丢。改造后指针阶段各只占 lead-in（几十字），全都进得来。

        故意让 4 张巨型 skill 的文件名字典序全部排在小 skill 之前。
        """
        d = tmp_path / "r"
        for i in range(4):
            _skill(d, f"A-huge{i}", always=True,
                   body=f"# 巨型 {i}\n\n" + "无结构正文。" * 900)
        for i in range(5):
            _skill(d, f"B{i}", always=True, core=f"论点 {i}。")
        block, loaded = render_triggered_block(discover_role_skills_scored(d, "任意"))
        assert len(loaded) == 9, (
            f"9 张都该至少有指针；改造前 4 张巨型就吃满预算。实得 {loaded}"
        )
        for i in range(5):
            assert f"论点 {i}。" in block, f"小 skill B{i} 的指针被挤掉了"

    def test_指针阶段超预算时按相关度保高分(self, tmp_path: Path):
        d = tmp_path / "r"
        _skill(d, "Z-high", keywords=["a", "b"], core="高相关。" * 60)
        _skill(d, "A-low", keywords=["a"], core="低相关。" * 60)
        block, loaded = render_triggered_block(
            discover_role_skills_scored(d, "a b"), total_char_budget=300,
        )
        assert loaded == ["Z-high"], f"预算只够一张时应保高相关，实得 {loaded}"

    def test_指针阶段超预算会告警(self, tmp_path: Path, capsys):
        d = tmp_path / "r"
        for i in range(4):
            _skill(d, f"S{i}", always=True, core="很长的论点。" * 50)
        render_triggered_block(
            discover_role_skills_scored(d, "任意"), total_char_budget=400,
        )
        err = capsys.readouterr().err
        assert "total_char_budget" in err and "用满" in err

    def test_单skill上限同时约束两级载荷(self, tmp_path: Path):
        """指针段本身超限（有人把整份规则塞进 `## 核心约束`）也必须截断。"""
        d = tmp_path / "r"
        _skill(d, "S", always=True, core="核心。" * 2000, rules="细则。")
        block, _ = render_triggered_block(
            discover_role_skills_scored(d, "任意"), max_chars_per_skill=500,
        )
        assert "截断" in block
        assert len(block) < 2000

    def test_块头汇报完整与指针张数(self, tmp_path: Path):
        d = tmp_path / "r"
        _skill(d, "S1", always=True, core="甲。", rules="细则。" * 400)
        _skill(d, "S2", always=True, core="乙。", rules="细则。" * 400)
        block, _ = render_triggered_block(
            discover_role_skills_scored(d, "任意"), total_char_budget=1500,
        )
        assert "张完整载荷" in block and "张仅核心约束" in block

    def test_平手组被预算切断时告警(self, tmp_path: Path, capsys):
        """不许静默：相关度全等的一组被预算切断，等于按文件名任意选择。

        实测触发场景：`music/编曲` 7 张 R&B skill 的 rank_key 全等
        `(0, 1, -7, 0, 0, 0, 4)`（都只命中 `Soul`，df=7），嘻哈任务下
        Ar1/Ar2/Ar3 拿完整载荷纯属字典序靠前。危害不对称 —— 改造前落选者各
        损失 ~120 字论点句，改造后落选者损失整份细则、当选者拿到的可能是错的。
        """
        d = tmp_path / "r"
        for i in range(4):
            _skill(d, f"S{i}", keywords=["shared"], core=f"论点 {i}。", rules="细则。" * 300)
        render_triggered_block(
            discover_role_skills_scored(d, "任务提到 shared"), total_char_budget=3000,
        )
        err = capsys.readouterr().err
        assert "相关度全等" in err and "字典序" in err
        assert "任务性 keyword" in err, "告警必须指出根因与修法"

    def test_平手组全部升级时不告警(self, tmp_path: Path, capsys):
        """预算够全组升级 → 没有任意选择发生 → 不该噪声。"""
        d = tmp_path / "r"
        for i in range(3):
            _skill(d, f"S{i}", keywords=["shared"], core=f"论点 {i}。", rules="细则。")
        render_triggered_block(discover_role_skills_scored(d, "任务提到 shared"))
        assert "相关度全等" not in capsys.readouterr().err

    def test_兼容2tuple入参保持传入顺序(self, tmp_path: Path):
        """改造前调用方传 (path, reason) —— 此时不重排，按 discover 已排好的序。"""
        d = tmp_path / "r"
        a = _skill(d, "A", always=True, core="甲。")
        b = _skill(d, "B", always=True, core="乙。")
        _, loaded = render_triggered_block([(b, "always"), (a, "always")])
        assert loaded == ["B", "A"]

    def test_空命中返回空(self):
        assert render_triggered_block([]) == ("", [])


# ══════════════════════════════════════════════════════════════
#  5. 真实数据守卫
# ══════════════════════════════════════════════════════════════

def _dir_keyword_df(d: Path) -> tuple[dict[str, list[str]], collections.Counter]:
    kw: dict[str, list[str]] = {}
    for f in sorted(d.glob("*.md")):
        if f.name.startswith((".", "_")):
            continue
        fm, _ = split_frontmatter(f.read_text(encoding="utf-8"))
        ks = ((fm or {}).get("trigger") or {}).get("keywords") or []
        if isinstance(ks, list) and ks:
            kw[f.stem] = [str(x) for x in ks if isinstance(x, str)]
    df: collections.Counter = collections.Counter()
    for ks in kw.values():
        for low in {x.lower() for x in ks}:
            df[low] += 1
    return kw, df


@pytest.mark.skipif(not VAULT.is_dir(), reason="vault 不可达")
class TestVaultDataGuard:
    SKILL_ROOT = VAULT / "20-知识" / "角色技能"

    def test_不是真空(self):
        n = len(list(self.SKILL_ROOT.rglob("*.md")))
        assert n >= 100, f"扫不到 skill 库（{n} 张），守卫失效"

    def test_se域排序对真实数据生效(self):
        """se 域 keyword 本就任务性（df>1 计数为 0），排序应把多词命中的顶上来。"""
        d = self.SKILL_ROOT / "se" / "架构师"
        if not d.is_dir():
            pytest.skip("架构师 skill 目录不存在")
        task = (
            "依赖锁定策略：pip freeze 生成 requirements.txt；前端用 npm ci。"
            "降级路径独占覆盖：主路径失败时走 fallback 分支。"
        )
        ms = discover_role_skills_scored(d, task)
        assert ms, "架构师在 deps 场景应有命中"
        assert ms == sorted(
            ms, key=lambda m: (tuple(-v for v in m.rank_key), m.path.name),
        ), "返回顺序必须已按相关度降序"

    def test_不可区分skill组数量不得增长(self):
        """**不是质量阈值，是防回归上限**（当前实测值当天花板）。

        依据：2026-08-17 全量测 —— 131 张有 keywords 的 skill 里 **76 张
        （58%）没有任何独有 keyword**，分布在 21 个「keyword 签名完全相同」
        的组里。最大一组是 `music/混音师` 的 7 张 R&B skill（M1 频谱能量分配 /
        M2 人声慢启动压缩 / M3 Plate与Pre-delay / M4 立体声宽度墙 /
        M5 Bass包络 / M6 Sidechain / M7 陷阱清单）—— 它们声明了完全一样的
        流派标签集合，在触发器眼里是同一个东西，**任何排序机制都无从区分**。

        根因：音乐域建设时 keyword 写成了「这张 skill 属于哪个流派」而不是
        「什么任务需要这张 skill」。se 域没有此问题（keyword 本就任务性）。

        本守卫只防继续变差；治理（给每张补任务性 keyword）挂 98-待办。
        """
        indistinct = 0
        with_kw = 0
        groups: list[tuple[str, int]] = []
        for d in sorted(self.SKILL_ROOT.rglob("*")):
            if not d.is_dir():
                continue
            kw, df = _dir_keyword_df(d)
            if len(kw) < 2:
                continue
            with_kw += len(kw)
            indistinct += sum(
                1 for ks in kw.values()
                if not any(df[x.lower()] == 1 for x in ks)
            )
            sig: dict[frozenset, list[str]] = {}
            for stem, ks in kw.items():
                sig.setdefault(frozenset(x.lower() for x in ks), []).append(stem)
            for v in sig.values():
                if len(v) > 1:
                    groups.append((str(d.relative_to(self.SKILL_ROOT)), len(v)))

        assert with_kw >= 100, f"样本过小（{with_kw}），守卫失效"
        assert indistinct <= 76, (
            f"「无独有 keyword」的 skill 增至 {indistinct}（2026-08-17 基线 76）。"
            f"新 skill 必须带至少 1 个本目录独有的任务性 keyword，"
            f"否则触发器无法把它与同组区分。同签名组：{sorted(groups)}"
        )
        assert len(groups) <= 21, (
            f"keyword 签名完全相同的组增至 {len(groups)}（基线 21）：{sorted(groups)}"
        )
