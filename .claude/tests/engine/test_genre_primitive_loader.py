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


class TestSectionKeyCasing:
    """标题键大小写不敏感 + 部分命中必告警（2026-09-03 F-嘻哈 实测）。"""

    @staticmethod
    def _rename(root: Path, genre: str, old: str, new: str) -> None:
        p = root / f"F-{genre}.md"
        t = p.read_text(encoding="utf-8")
        assert old in t, f"造例前提不成立：{old!r} 不在文档里"
        p.write_text(t.replace(old, new), encoding="utf-8")

    def test_大写Fusion也命中(self, music_root):
        """F-嘻哈.md 把第 9 节写成「Fusion 友好度与配比红线」（大写 F），另四份
        都是小写。大小写敏感的旧实现下五份里只有它选中 6 节、少的正是 fusion，
        而返回值与全命中完全相同 → 一路静默。"""
        _prim(music_root, "嘻哈")
        self._rename(music_root, "嘻哈",
                     "## 七、fusion 友好度", "## 9. Fusion 友好度与配比红线")
        block, _ = al.load_genre_primitive_block("音乐总监", "嘻哈 100%")
        assert "好融的搭档：Pop" in block, "大写 Fusion 标题下 fusion 节没进 prompt"
        assert "Fusion 友好度与配比红线" in block

    def test_大写Skill索引节也命中(self, music_root):
        """索引节键靠的是同一种字面巧合，五份恰好都写对了 —— 不靠"现在没有"保证。"""
        _prim(music_root, "嘻哈")
        self._rename(music_root, "嘻哈", "工程参考 skill（1 条",
                     "工程参考 SKILL（1 条")
        block, _ = al.load_genre_primitive_block("音乐总监", "嘻哈 100%")
        assert "下游消费规则" in block
        assert "[[Ar1-嘻哈-占位技能]]" in block

    def test_全命中时无缺节告警(self, music_root, capsys):
        _prim(music_root, "民谣")
        block, _ = al.load_genre_primitive_block("音乐总监", "民谣 100%")
        assert block
        assert "缺 schema 要求的" not in capsys.readouterr().err

    def test_真缺一节时告警且点名缺哪节(self, music_root, capsys):
        """大小写不敏感只解决"写错大小写"，解决不了"整节没写"。后者只能靠告警
        暴露 —— 否则又是一次「看起来在工作，实际没有」。"""
        _prim(music_root, "民谣")
        self._rename(music_root, "民谣",
                     "## 七、fusion 友好度", "## 七、跨界搭配建议")
        block, _ = al.load_genre_primitive_block("音乐总监", "民谣 100%")
        err = capsys.readouterr().err
        assert block, "缺一节仍应注入其余六节，不是整份跳过"
        assert "缺 schema 要求的 1 节" in err
        assert al._PRIMITIVE_FUSION_KEY in err
        # 没缺的键不该被点名
        assert "节奏型" not in err.split("缺 schema 要求的", 1)[1].split("——", 1)[0]

    def test_中文键不受lower影响(self, music_root):
        """`.lower()` 对中文是恒等 —— 加了大小写不敏感不该动摇原有的中文键。"""
        _prim(music_root, "民谣")
        _, sections, _, missing = al._select_primitive_payload(
            (music_root / "F-民谣.md").read_text(encoding="utf-8"))
        assert missing == []
        assert len(sections) == 7


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
        """依据写在模块常量注释里（2026-09-03 五份必需三块 3715-7009 /
        两份最坏 13780）。这两个数与 render_triggered_block 那三个自陈
        『拍脑袋初值』的参数不同源，改动时不要混为一谈。

        2026-09-03 单份上限 7000 → 8000：原依据是四份实测最大 6772，注释写
        「+3.4% 余量够容纳同量级的新流派」—— 第五份 F-嘻哈 补回 fusion 节后
        7009 就越了 9 char。8000 按五份实测最大值重定。
        """
        assert al.MAX_CHARS_PER_PRIMITIVE == 8000
        assert al.TOTAL_PRIMITIVE_BUDGET == 16_000
        src = Path(al.__file__).read_text(encoding="utf-8")
        assert "7009" in src and "13780" in src, "配额依据数字不在源码注释里"

    def test_总额等于两份上限(self):
        """总额不再是独立的魔数：单份 8000 抬上去后，两份必须塞得下，
        否则「两个流派的项目」会因为额度误丢一份（现状 7009+6771=13780，
        14000 只剩 220 char 余量，任何一份再长一点就踩）。"""
        assert (al.TOTAL_PRIMITIVE_BUDGET
                == al.MAX_CHARS_PER_PRIMITIVE * al.MAX_PRIMITIVES_PER_RUN)

    def test_份数上限是显式常量而非字符预算的副作用(self):
        """「不要第三个流派」此前是 TOTAL=14000 **顺带实现**的（两份 13191
        塞得下、第三份必然越额）。那是巧合机制：随 primitive 变胖 / 阈值变动
        而失效，且日志报「额度用尽」，把语义问题伪装成资源不够。"""
        assert al.MAX_PRIMITIVES_PER_RUN == 2


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

    def test_primitive索引节不参与skill召回(self, vault, tmp_path):
        """primitive 的「工程参考 skill」索引节是**菜单**，不是「点过的菜」。

        它列着该流派全部角色技能（民谣 29 / R&B 33 条）。让它进 skill 通道的
        haystack 会倒转 B1：总监从索引里挑、下游只拿被挑的那几张 —— 变成
        下游按预算顺序拿走菜单前几项。
        """
        role = self._Role()
        skill_dir = tmp_path / "20-知识" / "角色技能" / "music" / "编曲"
        skill_dir.mkdir(parents=True)
        # 索引节里那张（_BODY 的 `### 编曲` 写的就是 `Ar1-{genre}-占位技能`）
        (skill_dir / "Ar1-民谣-占位技能.md").write_text(
            "---\ntype: skill\n---\n# Ar1\n## 执行细则\n开放调弦。\n",
            encoding="utf-8")
        from engine import wikilink as wl_mod
        wl_mod.invalidate_cache()

        project = "- **流派配比**：民谣 100%\n- **primitive_refs**:\n  - [[F-民谣]]\n"
        ctx, hints = al.assemble_user_context(role, "编曲", project, domain="music")

        # 前提：primitive 真进来了、真带着那张技能的 wikilink（否则测试假绿）
        assert "[[F-民谣]] ===" in ctx
        assert "[[Ar1-民谣-占位技能]]" in ctx, "索引节没进 context，本测试失去意义"
        # 但它不能因此被当成「已点名」拿到完整载荷
        assert "Skill (wikilink:" not in ctx, (
            f"索引节被当成点名了：{hints['skill']}")

    def test_雷鬼单独点名时进得来_排除了额度巧合(self, vault):
        """上一条测试里 F-雷鬼 的缺席必须来自"没被当成点名"，而不是被额度或
        份数上限砍掉的巧合 —— 所以这里单独点它，它一定得真的出现。

        2026-09-03 前这条是「三份一起点、三份都塞得下」。加了
        `MAX_PRIMITIVES_PER_RUN=2` 之后三份必被截到两份，那个造法就分辨不出
        "没点名" 与 "第三份被份数上限拦下" 了 —— 改成点两份（含雷鬼），
        既在上限内、又能坐实雷鬼本身进得来。
        """
        role = self._Role()
        block, hint = al.load_genre_primitive_block(
            role.name, "编曲", "[[F-民谣]] [[F-雷鬼]]")
        assert "[[F-雷鬼]] ===" in block, f"雷鬼本身就进不来，前一条断言不可靠：{hint}"
        assert "丢弃" not in hint and "份数上限" not in hint

    def test_超两份时报份数上限而不是额度用尽(self, vault, capsys):
        """语义问题不许伪装成资源不够：本 fixture 三份合计远小于总额。"""
        role = self._Role()
        block, hint = al.load_genre_primitive_block(
            role.name, "编曲", "[[F-民谣]] [[F-R&B]] [[F-雷鬼]]")
        assert "超份数上限未注入 1 份" in hint
        assert "越额丢弃" not in hint
        err = capsys.readouterr().err
        assert "这不是额度不够" in err


class TestWikilinkBudgetScope:
    """skill 的 wikilink 预算不能花在别角色的技能上。

    2026-08-27 之前 `load_genre_skill_block` 传给 `expand_wikilinks` 的 filter
    只判命名正则，`e.path.parent != role_dir` 在**返回之后**才做，而
    `total_char_budget=12_000` 是在 expand_wikilinks **内部**扣的 —— 别角色的
    技能先被读出来记账、再被丢掉。实测 `纸飞机`/编曲：11046 char 预算里
    6905（63%）花在读完就丢的别角色技能上，总监点名的 7 张只有 2 张拿到细则。
    """

    @pytest.fixture()
    def skills(self, tmp_path, monkeypatch):
        import engine
        from engine import wikilink as wl_mod
        monkeypatch.setattr(engine, "VAULT_ROOT", tmp_path)
        monkeypatch.setattr(wl_mod, "VAULT_ROOT", tmp_path)
        wl_mod.invalidate_cache()
        music = tmp_path / "20-知识" / "角色技能" / "music"
        music.mkdir(parents=True)
        _prim(music, "民谣")          # 域根要有 F-*，否则派生不出流派名
        fat = "占" * 3000            # 每张都够撑满 max_chars_per_link

        def mk(role, name):
            d = music / role
            d.mkdir(exist_ok=True)
            (d / f"{name}.md").write_text(
                f"---\ntype: skill\n---\n# {name}\n## 执行细则\n{fat}\n",
                encoding="utf-8")

        # 别角色 5 张（够单独吃满 12000），本角色 4 张
        for i in range(1, 6):
            mk("音乐总监", f"D{i}-民谣-总监技能{i}")
        for i in range(1, 5):
            mk("编曲", f"Ar{i}-民谣-编曲技能{i}")
        yield tmp_path
        wl_mod.invalidate_cache()

    def test_别角色技能不吃本角色预算(self, skills):
        """点名顺序刻意把别角色的 5 张放在前面 —— 旧实现会被它们吃光预算。"""
        named = (
            "".join(f"[[D{i}-民谣-总监技能{i}]]\n" for i in range(1, 6))
            + "".join(f"[[Ar{i}-民谣-编曲技能{i}]]\n" for i in range(1, 5))
        )
        block, hint = al.load_genre_skill_block("编曲", "编曲", named)
        got = _re_findall_wikilink(block)
        assert got == [f"Ar{i}-民谣-编曲技能{i}" for i in range(1, 5)], (
            f"本角色点名的 4 张没全拿到：{hint} / {got}")
        assert not any(g.startswith("D") for g in got), "别角色的技能混进来了"


def _re_findall_wikilink(block: str) -> list[str]:
    import re
    return re.findall(r"=== Skill \(wikilink:\[\[([^\]]+)\]\] · full\) ===", block)


class TestGenreGate:
    """流派互斥闸门：keyword 路径只放行本项目流派的技能（2026-09-03）。

    实测背景：嘻哈任务混词场景命中 110/123 张，其中 87 张错流派。机制没坏
    （域外「报税软件」对照 0/123），是 123 张技能各自的 `trigger.keywords` 用了
    裸泛词。闸门把「是不是这个流派」的判定从 123 个文件收到 5 份 primitive 上，
    技能自己的 keyword 只决定「这个流派内选哪张」。
    """

    @pytest.fixture()
    def vault(self, tmp_path, monkeypatch):
        import engine
        from engine import wikilink as wl_mod
        monkeypatch.setattr(engine, "VAULT_ROOT", tmp_path)
        monkeypatch.setattr(wl_mod, "VAULT_ROOT", tmp_path)
        wl_mod.invalidate_cache()
        music = tmp_path / "20-知识" / "角色技能" / "music"
        music.mkdir(parents=True)
        # 三份 primitive：民谣 与 嘻哈 的 kw2 刻意各不相同
        _prim(music, "民谣", kw2="folk")
        _prim(music, "嘻哈", kw2="说唱")
        _prim(music, "雷鬼", kw2="Roots Reggae")   # 刻意**不**声明裸 Roots

        d = music / "编曲"
        d.mkdir()
        # 每张技能都声明裸词 `roots` —— 复刻 vault 里 25 张雷鬼技能的现状
        for genre in ("民谣", "嘻哈", "雷鬼"):
            for i in (1, 2):
                (d / f"Ar{i}-{genre}-技能{i}.md").write_text(
                    "---\ntype: skill\ntrigger:\n  keywords:\n"
                    f"    - {genre}\n    - roots\n---\n"
                    f"# Ar{i}-{genre}\n## 执行细则\n细则正文\n",
                    encoding="utf-8")
        yield tmp_path
        wl_mod.invalidate_cache()

    def test_裸词召回的别流派被挡下(self, vault, capsys):
        """任务只提民谣，但三个流派的技能都声明了裸词 `roots`。"""
        task = "写一首民谣，roots 感的木吉他"
        assert al.active_genres(task)[0] == {"民谣"}
        block, hint = al.load_genre_skill_block("编曲", task, "")
        assert "Ar1-民谣" in block and "Ar2-民谣" in block
        assert "嘻哈" not in block and "雷鬼" not in block
        assert "gated=4" in hint
        assert "流派闸门" in capsys.readouterr().err

    def test_雷鬼不声明裸Roots所以不进闸门集(self, vault):
        """真 vault 里 F-雷鬼 只声明 `Roots Reggae`，而 25 张雷鬼技能声明裸
        `Roots`/`roots` —— 判定收到 primitive 上，这 25 张就不再被 `roots` 召回。"""
        got, why = al.active_genres("roots 感的采样")
        assert got == frozenset(), why

    def test_融合项目两个流派都放行(self, vault):
        # 带上 `roots` 让三个流派的技能都命中，才能看出闸门只放行两个
        task = "民谣 60% + 嘻哈 40% 的融合，roots 感的编配"
        assert al.active_genres(task)[0] == {"民谣", "嘻哈"}
        block, hint = al.load_genre_skill_block("编曲", task, "")
        for g in ("民谣", "嘻哈"):
            assert f"Ar1-{g}" in block
        assert "雷鬼" not in block
        assert "gated=2" in hint

    def test_三条证据取并集_显式不一票定音(self, vault):
        """反例来自真项目 `湖向`（R&B + 国风）：vision 的 primitive_refs 只写了
        `[[F-R&B]]`，国风那半边是以 `[[D1-国风-…]]` 技能名点的。若「有显式就只认
        显式」，该项目**全部国风技能会被闸门挡掉** —— 正是本轮在修的那类静默失效。
        """
        got, why = al.active_genres("[[F-嘻哈]] 的项目，参考一点民谣的叙事")
        assert got == {"嘻哈", "民谣"}
        assert "primitive_refs" in why and "keyword" in why

    def test_点名技能自带的流派也算(self, vault):
        """复刻 `湖向` 的形状：primitive_refs 只点一个，另一个靠技能名带出来。"""
        got, why = al.active_genres(
            "[[F-嘻哈]] 主导", "编配参考 [[Ar1-雷鬼-技能1]]")
        assert got == {"嘻哈", "雷鬼"}
        assert "点名技能" in why

    def test_点名技能带的流派不被闸门挡掉自己(self, vault):
        """上游点了 [[Ar1-雷鬼-技能1]]，那 Ar2-雷鬼 也该能靠 keyword 进来 ——
        闸门不该把「上游明确在用的流派」判成外来流派。"""
        block, hint = al.load_genre_skill_block(
            "编曲", "写一首民谣，roots 感", "编配参考 [[Ar1-雷鬼-技能1]]")
        assert "Ar2-雷鬼" in block, hint

    def test_判不出流派时不设闸(self, vault):
        """fail-open：闸门是为了拦「明显不是这个流派的」，不是为了拦「判不出的」。"""
        task = "帮我做一个报税软件的需求分析"
        assert al.active_genres(task)[0] == frozenset()
        block, hint = al.load_genre_skill_block("编曲", task, "")
        assert "gated" not in hint

    def test_wikilink显式点名不受闸门约束(self, vault):
        """「显式 > 隐式」：上游写了 [[Ar1-雷鬼-技能1]] 就是直接指令。"""
        block, hint = al.load_genre_skill_block(
            "编曲", "写一首民谣", "参考 [[Ar1-雷鬼-技能1]]")
        assert "Ar1-雷鬼-技能1" in _re_findall_wikilink(block)

    def test_名字不合规范的技能不被误挡(self, vault):
        """fail-open：解析不出流派段的一律放行，否则闸门自己变成新的静默失效。"""
        (vault / "20-知识" / "角色技能" / "music" / "编曲" / "杂项-通用技巧.md").write_text(
            "---\ntype: skill\ntrigger:\n  keywords:\n    - roots\n---\n"
            "# 杂项\n## 执行细则\n通用\n", encoding="utf-8")
        assert al._skill_genre("杂项-通用技巧") is None
        block, _ = al.load_genre_skill_block("编曲", "写一首民谣，roots 感", "")
        assert "杂项" in block

    def test_全被挡时hint不谎报双路径均空(self, vault):
        """「被闸门挡光了」与「本来就没命中」是两种完全不同的处置。"""
        block, hint = al.load_genre_skill_block(
            "编曲", "写一首嘻哈说唱，roots 感的采样", "")
        # 先坐实本例确实有被挡的
        assert "gated=" in hint
        # 再造一个全被挡的：任务是民谣，但把民谣技能挪走
        d = vault / "20-知识" / "角色技能" / "music" / "编曲"
        for i in (1, 2):
            (d / f"Ar{i}-民谣-技能{i}.md").unlink()
        block, hint = al.load_genre_skill_block("编曲", "写一首民谣，roots 感", "")
        assert block == ""
        assert "双路径均空" in hint and "gated=" in hint
        assert "全是非本项目流派" in hint


class TestReExport:
    def test_common_identity(self):
        from common import load_genre_primitive_block
        assert load_genre_primitive_block is al.load_genre_primitive_block
