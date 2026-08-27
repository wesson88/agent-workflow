"""test_genre_primitive_loader.py — 流派 primitive 独立通道（2026-08-26）

覆盖 `ability_loader.load_genre_primitive_block` 与它依赖的两个新 helper。

## 这些测试在防什么

primitive 通道的前身是一次典型「沉默失效」：设计意图写在
[[音乐制作域-Phase1-PRD]] §11.4（简报配比 → keyword 命中 → 只加载点到的流派），
四份 `F-*.md` 都写了合法 `trigger`，但**一份都进不了 prompt** —— 它们在域根，
而三条路全堵（keyword 只扫角色目录 / 正则不认 `F-` / `parent != role_dir`）。
没有任何告警，实测 `成为父亲那年` 7 份指令 0 条技能点名。

所以本文件的断言重心不在「函数遵守契约」，而在**「契约接到了东西上」**：
- 国风真的能被正则认出来（那 13 张技能曾整批走不通 wikilink）
- 索引节真的在 payload 里（整份截断会正好把它切掉）
- 不该拿的角色真的拿不到（范围收窄是本通道存在的前提）
- 该告警的地方真的告警（fail-closed 不等于可以静默）
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engine import ability_loader as al


_FM = """---
type: genre_primitive
domain: music
genre: {genre}
consumed_by:
  - 音乐总监
  - 作曲
  - 编曲
trigger:
  keywords:
    - {genre}
    - {kw2}
---
"""

# 章节顺序刻意还原真实 primitive：索引节在**最后**。
# 这是「按章节选取而非整份截断」的理由 —— 整份截到上限正好把索引节切掉。
_BODY = """
# F-{genre}：核心 idiom 卡片

## 一、节奏型 / 律动

BPM {bpm}。{filler}

## 二、标志性配器

必带 {inst}。{filler}

## 三、调性 / 和声走向

自然小调。{filler}

## 四、主题 / 情感 idiom

叙事。{filler}

## 五、流派红线

红线 1：{redline}

## 六、经典参考

参考曲若干 —— **刻意不注入**：意境素材，角色技能里有更具体的。{filler}

## 七、fusion 友好度

好融的搭档：Pop。{filler}

## 八、工程参考 skill（{n} 条，按 music 域角色聚合）

> **下游消费规则**：本节是 vault 里{genre}域全部 skill 的真实索引。
> 音乐总监 / 制作人派活时只能从本表中挑 skill wikilink，禁止编造文件名。

### 编曲
- [[Ar1-{genre}-占位技能]]

## 与 PRD 体系的关系

**刻意不注入**：维护者视角。{filler}
"""


def _prim(root: Path, genre: str, *, kw2="alt", bpm="70-90", inst="吉他",
          redline="不能错位", n=1, filler="", consumed_by=None) -> Path:
    fm = _FM.format(genre=genre, kw2=kw2)
    if consumed_by is not None:
        lines = ["---", "type: genre_primitive", "domain: music", f"genre: {genre}"]
        if consumed_by:
            lines.append("consumed_by:")
            lines += [f"  - {c}" for c in consumed_by]
        lines += ["trigger:", "  keywords:", f"    - {genre}", f"    - {kw2}", "---"]
        fm = "\n".join(lines) + "\n"
    body = _BODY.format(genre=genre, bpm=bpm, inst=inst, redline=redline,
                        n=n, filler=filler)
    p = root / f"F-{genre}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(fm + body, encoding="utf-8")
    return p


@pytest.fixture()
def music_root(tmp_path, monkeypatch):
    """把 VAULT_ROOT 指到 tmp，返回 `20-知识/角色技能/music/`。"""
    import engine
    monkeypatch.setattr(engine, "VAULT_ROOT", tmp_path)
    root = tmp_path / "20-知识" / "角色技能" / "music"
    root.mkdir(parents=True)
    return root


class TestGenreNameDerivation:
    """修法 A：流派名从域根 F-* 派生，不再硬编码。"""

    def test_derived_from_files(self, music_root):
        _prim(music_root, "民谣")
        _prim(music_root, "国风")
        assert set(al._music_genre_names()) == {"民谣", "国风"}

    def test_ampersand_gets_percent_encoded_alias(self, music_root):
        _prim(music_root, "R&B")
        # Obsidian 部分场景把 `&` 写成 `%26`（原硬编码里的 R%26B 即此）
        assert set(al._music_genre_names()) == {"R&B", "R%26B"}

    def test_国风技能不再被正则漏掉(self, music_root):
        """回归：`F-国风.md` 与 13 张国风技能 2026-06-14 就位，原硬编码正则
        `(?:R&B|R%26B|民谣|雷鬼)` 没跟 —— 那 13 张（覆盖 9 角色里的 7 个）
        从未走通 wikilink 通道，上游显式点名会被 filter 静默丢掉。"""
        _prim(music_root, "国风")
        _prim(music_root, "民谣")
        rx = al._music_skill_re()
        assert rx.search("Ar1-国风-子流派演化谱系")
        assert rx.search("V2-国风-戏曲腔与古风通俗腔")
        assert rx.search("Ma1-民谣-LUFS与DR目标")

    def test_primitive_自身不被技能正则匹配(self, music_root):
        """`F-民谣` 走 primitive 通道，不该被当成角色技能。"""
        _prim(music_root, "民谣")
        assert not al._music_skill_re().search("F-民谣")

    def test_域根无primitive时返回None而非放行全部(self, music_root):
        """放行全部会把 se 域的 skill 拽进 music 通道 —— 命名过滤的语义是
        『只认本域规范』，不是『不过滤』（那是 load_skill_block 的语义）。"""
        assert al._music_genre_names() == ()
        assert al._music_skill_re() is None


class TestConsumedByScope:
    """范围收窄是本通道存在的前提（PRD §11.4 只让三个决策角色读）。"""

    def test_名单内角色拿到(self, music_root):
        _prim(music_root, "民谣")
        block, hint = al.load_genre_primitive_block("音乐总监", "民谣 100%")
        assert "F-民谣" in block and "注入 1 份" in hint

    def test_名单外角色拿不到(self, music_root):
        _prim(music_root, "民谣")
        block, hint = al.load_genre_primitive_block("混音师", "民谣 100%")
        assert block == "" and "不在任何 primitive 的 consumed_by 名单" in hint

    def test_和声编写不在名单(self, music_root):
        """2026-08-26 用户拍板：流派primitive-schema 曾把和声编写列为消费方，
        但它既无对应 rule_ref 也不在 PRD 名单里 → 不算。"""
        _prim(music_root, "民谣")
        block, _ = al.load_genre_primitive_block("和声编写", "民谣 100%")
        assert block == ""

    def test_缺consumed_by时跳过且告警(self, music_root, capsys):
        """fail-closed 但**必告警**：静默跳过会让新加的 primitive 永远不生效，
        那正是本次要修的病，不能在修它的代码里复制一份。"""
        _prim(music_root, "民谣", consumed_by=[])   # 无 consumed_by 字段
        block, hint = al.load_genre_primitive_block("音乐总监", "民谣 100%")
        assert block == ""
        err = capsys.readouterr().err
        assert "未声明 consumed_by" in err and "F-民谣.md" in err


class TestDualPath:
    """wikilink 显式（简报 primitive_refs）∪ keyword 兜底。"""

    def test_显式点名只加载点到的(self, music_root):
        """用户说「50% R&B + 50% 国风」→ 只加载这两份，第三份不进。"""
        for g in ("R&B", "国风", "雷鬼"):
            _prim(music_root, g)
        brief = "- **primitive_refs**:\n  - [[F-R&B]]\n  - [[F-国风]]\n"
        block, hint = al.load_genre_primitive_block("音乐总监", "50% R&B + 50% 国风", brief)
        assert "[[F-R&B]]" in block and "[[F-国风]]" in block
        assert "[[F-雷鬼]]" not in block
        assert "wikilink · sections" in block

    def test_keyword兜底(self, music_root):
        for g in ("民谣", "雷鬼"):
            _prim(music_root, g)
        block, _ = al.load_genre_primitive_block("编曲", "民谣 60% + 雷鬼 40%")
        assert "auto-trigger:keyword:民谣" in block
        assert "auto-trigger:keyword:雷鬼" in block

    def test_显式优先不重复(self, music_root):
        """同一份既被点名又被 keyword 命中 → 只注入一次，走 wikilink 标记。"""
        _prim(music_root, "民谣")
        block, hint = al.load_genre_primitive_block(
            "作曲", "民谣 100%", "- [[F-民谣]]\n")
        assert block.count("[[F-民谣]] ===") == 1
        assert "wikilink · sections" in block
        assert "auto-trigger" not in block

    def test_跨域同名不误取(self, music_root, tmp_path):
        """se 域也有 `F-*.md`，但那是 domain_primitive（按角色分、无 trigger），
        与 music 的 genre_primitive 是**同名不同物**。只按域根 stem 匹配。"""
        _prim(music_root, "民谣")
        se = tmp_path / "20-知识" / "角色技能" / "se"
        se.mkdir(parents=True)
        (se / "F-前端.md").write_text(
            "---\ntype: domain_primitive\nrole: 前端工程师\n---\n# F-前端\n",
            encoding="utf-8")
        block, _ = al.load_genre_primitive_block("音乐总监", "x", "[[F-前端]] [[F-民谣]]")
        assert "[[F-民谣]]" in block and "F-前端" not in block

    def test_双路径均空(self, music_root):
        _prim(music_root, "民谣")
        block, hint = al.load_genre_primitive_block("音乐总监", "写一首歌")
        assert block == "" and "双路径均空" in hint


class TestSectionSelection:
    """按章节选取，不整份截断。"""

    def test_必需三块都在_不需要的不在(self, music_root):
        _prim(music_root, "民谣")
        block, _ = al.load_genre_primitive_block("音乐总监", "民谣 100%")
        for key in ("节奏型", "标志性配器", "调性", "主题", "流派红线",
                    "fusion 友好度", "工程参考 skill"):
            assert key in block, f"必需章节 {key} 缺失"
        assert "经典参考" not in block
        assert "与 PRD 体系的关系" not in block

    def test_索引节受保护_截断时也不丢(self, music_root):
        """索引节在文档里排最后，而截断是取前 N 字符 —— 不单独保住就会正好被切掉。
        它是整条下游链的起点（总监照它派活 → 制作人扇出 → 下游拿 full 载荷），
        所以 idiom 可以截，它不行。"""
        _prim(music_root, "民谣", filler="占" * al.MAX_CHARS_PER_PRIMITIVE)
        raw = (music_root / "F-民谣.md").read_text(encoding="utf-8")
        # 前提坐实：朴素的「整份取前 MAX」确实拿不到索引节
        assert "下游消费规则" not in raw[:al.MAX_CHARS_PER_PRIMITIVE]
        block, _ = al.load_genre_primitive_block("音乐总监", "民谣 100%")
        assert "· truncated)" in block, "本例应触发单份截断"
        assert "下游消费规则" in block, "索引节被截掉了 —— 保护失效"
        assert "禁止编造文件名" in block
        assert "[[Ar1-民谣-占位技能]]" in block, "索引条目本身也必须在"

    def test_索引节本身超上限时保索引丢idiom(self, music_root):
        """宁可只给索引也不给半份 idiom：半份 idiom 会让角色以为拿全了。"""
        _prim(music_root, "民谣", n=999)
        p = music_root / "F-民谣.md"
        t = p.read_text(encoding="utf-8")
        t = t.replace("- [[Ar1-民谣-占位技能]]",
                      "\n".join(f"- [[Ar{i}-民谣-占位技能]]" for i in range(1, 400)))
        p.write_text(t, encoding="utf-8")
        block, _ = al.load_genre_primitive_block("音乐总监", "民谣 100%")
        assert "下游消费规则" in block
        assert "· truncated)" in block

    def test_索引节去重_取带下游消费规则的(self, music_root):
        """F-民谣 / F-雷鬼 各有两个「工程参考 skill」节：旧版只有一句
        『详见各角色 skill』，新版带『下游消费规则』硬约束。取新版。"""
        p = _prim(music_root, "民谣")
        stale = ("\n## 工程参考 skill（按角色分组，共 29 条）\n\n"
                 "> 本 primitive 提供流派 idiom 决策依据。具体工程参数详见各角色 skill。\n\n"
                 "### 编曲\n- [[Ar9-民谣-旧索引残留]]\n")
        t = p.read_text(encoding="utf-8")
        # 插在新版之前，模拟真实文件里的先后顺序
        i = t.index("## 八、工程参考 skill") if "## 八、" in t else t.index("## 八")
        p.write_text(t[:i] + stale.lstrip("\n") + t[i:], encoding="utf-8")
        block, _ = al.load_genre_primitive_block("音乐总监", "民谣 100%")
        assert "下游消费规则" in block
        assert "Ar9-民谣-旧索引残留" not in block

    def test_章节结构偏离时跳过而不回退全文(self, music_root, capsys):
        """注入残片比不注入更误导：角色会以为自己拿到了 idiom。"""
        (music_root / "F-怪.md").write_text(
            "---\ntype: genre_primitive\ndomain: music\ngenre: 怪\n"
            "consumed_by:\n  - 音乐总监\ntrigger:\n  keywords:\n    - 怪\n---\n"
            "# F-怪\n\n## 随便一个不在名单里的标题\n\n正文\n",
            encoding="utf-8")
        block, hint = al.load_genre_primitive_block("音乐总监", "怪 100%")
        assert block == ""
        assert "抽不出「必需三块」任一节" in capsys.readouterr().err

    def test_围栏内标题不参与切分(self, music_root):
        """2026-08-16 那次「章节抽取无围栏感知」实测让音乐域契约注入丢 79%。

        造法要能真正分辨两种实现：把一个**名字在选取清单里**的伪 h2 放进一个
        **不选取**的章节（经典参考）。非围栏感知会从伪标题处另开一节、标题命中
        `节奏型` → 把经典参考的尾巴当 idiom 注进来；围栏感知则整节都不选。
        """
        p = _prim(music_root, "民谣")
        t = p.read_text(encoding="utf-8")
        assert "## 六、经典参考" in t
        t = t.replace(
            "## 六、经典参考\n",
            "## 六、经典参考\n\n```markdown\n## 一、节奏型 / 律动\n"
            "围栏内的伪标题，其后内容不是 idiom\n```\n\n泄漏哨兵ABC\n",
        )
        p.write_text(t, encoding="utf-8")
        block, _ = al.load_genre_primitive_block("音乐总监", "民谣 100%")
        assert "泄漏哨兵ABC" not in block, "围栏内伪标题被当真 → 非选取章节泄漏进来了"


class TestBudget:
    """独立预算：不与角色技能通道抢 total_char_budget=12_000。"""

    def test_单份超上限时截断并标记(self, music_root):
        _prim(music_root, "民谣", filler="占" * al.MAX_CHARS_PER_PRIMITIVE)
        block, _ = al.load_genre_primitive_block("音乐总监", "民谣 100%")
        assert "· truncated)" in block
        assert "（截断：" in block

    def test_越总额时丢弃并告警(self, music_root, capsys):
        pad = "占" * (al.MAX_CHARS_PER_PRIMITIVE // 2)
        for g in ("民谣", "雷鬼", "国风", "R&B"):
            _prim(music_root, g, filler=pad)
        block, hint = al.load_genre_primitive_block(
            "编曲", "民谣 + 雷鬼 + 国风 + R&B 四拼")
        assert "越额丢弃" in hint
        err = capsys.readouterr().err
        assert "总额" in err and str(al.TOTAL_PRIMITIVE_BUDGET) in err

    def test_配额有实测依据(self):
        """依据写在模块常量注释里（四份必需三块 3715-6772 / 两份最坏 13191）。
        这两个数与 render_triggered_block 那三个自陈『拍脑袋初值』的参数不同源，
        改动时不要混为一谈。"""
        assert al.MAX_CHARS_PER_PRIMITIVE == 7000
        assert al.TOTAL_PRIMITIVE_BUDGET == 14_000
        src = Path(al.__file__).read_text(encoding="utf-8")
        assert "6772" in src and "13191" in src, "配额依据数字不在源码注释里"


class TestFingerprint:
    """信封能被仪表认出来，不落 unknown。"""

    @pytest.mark.parametrize("label,tier,via", [
        ("Primitive (wikilink · sections): [[F-国风]]", "sections", "wikilink"),
        ("Primitive (auto-trigger:keyword:民谣 · sections): [[F-民谣]]",
         "sections", "auto-trigger:keyword:民谣"),
        ("Primitive (wikilink · truncated): [[F-R&B]]", "truncated", "wikilink"),
    ])
    def test_classify(self, label, tier, via):
        from engine.injection_fingerprint import classify_envelope
        got = classify_envelope(label)
        assert got["kind"] == "genre_primitive"
        assert got["tier"] == tier and got["reason"] == via

    def test_真实块被解析(self, music_root):
        from engine.injection_fingerprint import parse_blocks
        _prim(music_root, "民谣")
        block, _ = al.load_genre_primitive_block("音乐总监", "民谣 100%")
        kinds = [b["kind"] for b in parse_blocks(block)]
        assert "genre_primitive" in kinds
        assert "unknown" not in kinds

    def test_不算进SKILL_KINDS(self):
        """混进 SKILL_KINDS 会让「skill 占了多少输入」失真 —— 那正是 P1.2
        要盯的数，而 primitive 走的是另一个预算。"""
        from engine.injection_fingerprint import SKILL_KINDS, ALL_KINDS
        assert "genre_primitive" not in SKILL_KINDS
        assert "genre_primitive" in ALL_KINDS


class TestRuleTextNotProjectData:
    """规则文本里的 `[[F-*]]` 是**举例**，不是用户点名。

    2026-08-27 实测的真 bug：`assemble_user_context` 把已拼上 rule_block 的
    `context` 传给 primitive loader，而 `产物schema` §9 编曲方案 §5 的硬约束写着
    「本节列表项必须以 `[[F-{流派名}]]` 开头（如 `[[F-民谣]]` / `[[F-雷鬼]]`）」。
    于是 `纸飞机`（民谣 60% + R&B 40%）的编曲被判定"点名了民谣和雷鬼"，
    F-雷鬼 抢到位置、真需要的 F-R&B 被独立额度挤掉。
    """

    class _Role:
        name = "编曲"
        domain = "music"
        rule_refs = ("[[产物schema#9. 编曲方案]]",)

    @pytest.fixture()
    def vault(self, tmp_path, monkeypatch):
        import engine
        from engine import wikilink as wl_mod
        monkeypatch.setattr(engine, "VAULT_ROOT", tmp_path)
        monkeypatch.setattr(wl_mod, "VAULT_ROOT", tmp_path)
        wl_mod.invalidate_cache()
        music = tmp_path / "20-知识" / "角色技能" / "music"
        music.mkdir(parents=True)
        for g in ("民谣", "R&B", "雷鬼"):
            _prim(music, g)
        rules = tmp_path / "00-系统" / "规则" / "music"
        rules.mkdir(parents=True)
        # 复刻真实规则文本：把 F-* 当**格式示例**写在硬约束里。
        (rules / "产物schema.md").write_text(
            "---\ntype: contract\n---\n\n"
            "## 9. 编曲方案\n\n"
            "§5 流派配比溯源：本节列表项必须以 `[[F-{流派名}]]` wikilink 开头"
            "（如 [[F-民谣]] / [[F-雷鬼]]），标注该元素来自哪份 primitive。\n",
            encoding="utf-8")
        yield tmp_path
        wl_mod.invalidate_cache()

    def test_规则示例不算点名(self, vault):
        """项目正文只点了 民谣 + R&B → 注入这两份；规则里举例的 雷鬼 不能进来。"""
        role = self._Role()
        project = ("- **流派配比**：民谣 60% / R&B 40%\n"
                   "- **primitive_refs**:\n  - [[F-民谣]]\n  - [[F-R&B]]\n")
        ctx, hints = al.assemble_user_context(role, "编曲", project, domain="music")

        # 前提校验：rule_block 真的进了 context 且真的带着 F-雷鬼 举例
        # （否则本测试会因为"规则没注入"而假绿）。
        assert "9. 编曲方案" in ctx, "rule_block 未注入，本测试失去意义"
        assert "[[F-雷鬼]]" in (al.load_rule_block(role.rule_refs)[0] or "")

        assert "[[F-民谣]] ===" in ctx and "[[F-R&B]] ===" in ctx
        assert "[[F-雷鬼]] ===" not in ctx, (
            f"规则文本的举例被当成点名了：{hints['genre_primitive']}")

    def test_三份都塞得下_排除了额度巧合(self, vault):
        """本 fixture 的 primitive 很小，三份合计远小于 TOTAL_PRIMITIVE_BUDGET。

        所以上一条测试里 F-雷鬼 的缺席只可能来自"没被当成点名"，
        不可能是被额度砍掉的巧合 —— 回归时它一定会真的出现。
        """
        role = self._Role()
        block, hint = al.load_genre_primitive_block(
            role.name, "编曲",
            "[[F-民谣]] [[F-R&B]] [[F-雷鬼]]")
        assert "[[F-雷鬼]] ===" in block, f"三份塞不下，前一条测试的断言不可靠：{hint}"
        assert "丢弃" not in hint


class TestReExport:
    def test_common_identity(self):
        from common import load_genre_primitive_block
        assert load_genre_primitive_block is al.load_genre_primitive_block
