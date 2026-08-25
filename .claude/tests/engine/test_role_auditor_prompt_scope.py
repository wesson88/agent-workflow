"""
tests/engine/test_role_auditor_prompt_scope.py
    —— 正文/章节软上限改用 prompt 口径（2026-08-16 改动）

## 背景

`LIMITS["body_no_dynamic"]` 旧口径 = 整个 body 减 DYNAMIC，**把 §7/§8 也算进去**；
而业务角色走 `common._extract_role_prompt_sections` 严格 §1-§6，
**§8 版本历史一个字不进 system_prompt**。

实测 23 个业务角色：旧口径合计 87264 chars，真进 prompt 61838，虚高 **29%**
（技术主管虚高 48%：6683 计入 / 3491 实注入）。后果是旧口径报 4 个超限
（技术主管 / 前端 / 后端 / 架构师），按真实注入量 **0 个** —— 4 假阳性 / 0 真阳性。

最反常的一点：该指标**惩罚写文档**。给技术主管补 v1.8.0 版本历史（950 chars，
记录治理依据，正是项目 CLAUDE.md 的硬性要求）后，旧指标从 5733 涨到 6683。

## 本次改动

1. `body_no_dynamic` → `prompt_body`（改名，因旧名描述的就是错的量）
2. 判超限与「最大章节」都改量 `_extract_role_prompt_sections` 的返回值
3. 抽取失败降级回旧口径 + `prompt_extract_ok=False`，不崩整轮
4. 四个原本无依据的阈值补实测依据（见 main.py LIMITS 注释）
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from role_auditor import main as ra_mod


# ══════════════════════════════════════════════════════════════
#  fixture：§1-§6 齐全的合规业务角色骨架
# ══════════════════════════════════════════════════════════════

def _body(sec6_pad: str = "", sec8_pad: str = "") -> str:
    return f"""
# 角色：测试角色

## 1. 核心使命
用于单测的最小角色骨架。

## 2. 输入与输出
参见 frontmatter `inputs` / `outputs`。

## 3. 职责范围
做测试要求的事，不越界。

## 4. 职责边界（禁止事项）
不产出本文件断言之外的内容。

## 5. 输入与输出
读输入 → 产出 → 结束，步骤足够长以免被空壳 lint 判为无实质内容。

## 6. 执行工作流
逐步执行并自检，本段用于承载膨胀测试。{sec6_pad}

## 7. 运行时补丁（控制区）

<!-- DYNAMIC_START -->
<!-- DYNAMIC_END -->

## 8. 版本历史
- v0.1.0 (2026-08-16): 初始。{sec8_pad}
"""


def _write(tmp_path: Path, body: str, domain: str = "se", seg: str | None = None) -> Path:
    """写角色基因文件。

    seg 为 None → 落在 tmp_path 根（历史行为，多数用例只关心章节口径，不碰 skill 目录）。
    seg 给了值 → 落在 `00-系统/角色基因/{seg}/`，这是 `_role_skill_dir` 定段的依据
    （2026-08-25 起技能目录段按文件位置解析，不按 frontmatter domain）。
    """
    p = (
        tmp_path / "角色-测试角色.md" if seg is None
        else tmp_path / "00-系统" / "角色基因" / seg / "角色-测试角色.md"
    )
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\nrole: 测试角色\ndomain: {domain}\n---\n{body}", encoding="utf-8")
    return p


# ══════════════════════════════════════════════════════════════
#  核心行为：两种口径必须分开，判超限只认 prompt 口径
# ══════════════════════════════════════════════════════════════

def test_两种口径都要报出且prompt口径更小(tmp_path: Path):
    m = ra_mod._measure_role(_write(tmp_path, _body(sec8_pad="补" * 500)))
    assert m["prompt_extract_ok"] is True
    assert m["prompt_path_used"] == "business_strict"
    assert m["prompt_body_chars"] < m["body_no_dynamic_chars"], "§8 应被排除在 prompt 口径外"


def test_巨大版本历史不再触发正文超限(tmp_path: Path):
    """**本次改动的核心行为**，也是旧口径 4 个假阳性的成因。

    §8 版本历史撑到远超 5000，但 §1-§6 很小 —— 不应报 body_over_limit。
    """
    m = ra_mod._measure_role(_write(tmp_path, _body(sec8_pad="史" * 8000)))

    assert m["body_no_dynamic_chars"] > ra_mod.LIMITS["prompt_body"], "前提不成立：全文没超限"
    assert m["prompt_body_chars"] <= ra_mod.LIMITS["prompt_body"]
    assert m["body_over_limit"] is False


def test_真正进prompt的章节撑爆时仍然报超限(tmp_path: Path):
    """改口径不等于放弃治理：§6 自己撑爆，照样要报。

    与上一条构成正反对照 —— 只有两条同时成立，才说明改的是口径而不是把 lint 关了。
    """
    m = ra_mod._measure_role(_write(tmp_path, _body(sec6_pad="正" * 8000)))

    assert m["prompt_body_chars"] > ra_mod.LIMITS["prompt_body"]
    assert m["body_over_limit"] is True
    assert m["section_over_limit"] is True
    assert m["max_section_id"] == "6"


def test_最大章节不再命中版本历史(tmp_path: Path):
    """旧口径下 4 个 SE 角色的「最大章节」全是 §8（TL 3063 / 前端 2703 /
    后端 2188 / 架构师 2150），即 lint 实际在治理版本历史。"""
    m = ra_mod._measure_role(_write(tmp_path, _body(sec8_pad="史" * 6000)))
    assert m["max_section_id"] != "8"
    assert m["section_over_limit"] is False


# ══════════════════════════════════════════════════════════════
#  降级路径：结构不合规的角色不能把整轮审计带崩
# ══════════════════════════════════════════════════════════════

def test_业务角色缺章时降级而非抛异常(tmp_path: Path):
    broken = "\n# 角色：破的\n\n## 1. 核心使命\n只有一章。\n"
    m = ra_mod._measure_role(_write(tmp_path, broken))

    assert m["prompt_extract_ok"] is False
    assert m["prompt_path_used"].startswith("fallback:")
    assert m["prompt_body_chars"] == m["body_no_dynamic_chars"], "降级后应退回旧口径"


def test_元角色走meta_full路径(tmp_path: Path):
    """元角色全 body 进 prompt（减 DYNAMIC 与版本历史），不是 §1-§6 白名单。"""
    m = ra_mod._measure_role(_write(tmp_path, _body(sec8_pad="史" * 500), domain="元"))
    assert m["prompt_extract_ok"] is True
    assert m["prompt_path_used"] == "meta_full"
    assert m["prompt_body_chars"] < m["body_no_dynamic_chars"], "版本历史应被剥掉"


# ══════════════════════════════════════════════════════════════
#  防回退 + 阈值溯源硬约束
# ══════════════════════════════════════════════════════════════

def test_旧键名已移除(tmp_path: Path):
    """`body_no_dynamic` 这个名字描述的就是错的量，不允许悄悄回来。"""
    assert "body_no_dynamic" not in ra_mod.LIMITS
    assert "prompt_body" in ra_mod.LIMITS


def test_全部阈值都带依据注释():
    """项目 CLAUDE.md 硬约束「阈值来源必须显式声明」的结构性守卫。

    2026-08-16 前 `body_no_dynamic` / `single_section` / `dynamic` /
    `single_patch` 四项全裸无注释 —— 本测试防止再退回去。
    """
    src = Path(ra_mod.__file__).read_text(encoding="utf-8")
    block = re.search(r"^LIMITS = \{(.*?)^\}", src, re.S | re.M)
    assert block, "未找到 LIMITS 定义块"
    lines = block.group(1).split("\n")

    key_re = re.compile(r'\s*"([a-z_]+)":\s*\d+')
    # ⚠️ 搜索范围必须**限定在上一个键之后**。最初写成「从本行向上扫连续注释行」，
    # 而各键之间没有空行 —— 扫描会一路穿进上一个键的注释块，撞到它的「依据」
    # 就算通过。2026-08-16 的 mutation 验证（抹掉 single_patch 的依据）当场
    # 证明该写法空转：注入后测试仍绿。
    # 判据是「依据：」这个**标记**，不是「依据」子串。后者太松：`prompt_body`
    # 的注释块里有多行叙述性地提到「全裸无依据」「无效依据」「重定依据」，
    # 抹掉真正的依据行后子串仍在 —— mutation 验证当场证明该写法空转。
    prev_key_line = -1
    naked: list[str] = []
    for i, ln in enumerate(lines):
        m = key_re.match(ln)
        if not m:
            continue
        own_comments = [
            x for x in lines[prev_key_line + 1: i] if x.strip().startswith("#")
        ]
        if not any("依据：" in c for c in own_comments):
            naked.append(m.group(1))
        prev_key_line = i

    assert not naked, f"以下阈值缺依据注释（违反 CLAUDE.md 硬约束）：{naked}"


def test_依据里不得使用被点名的无效措辞():
    """CLAUDE.md 反例：「经验值」「合理值」「业界标准」「我觉得」等于没说。

    2026-08-16 前 vault 规范 §4 给两个 5000 的解释正是「仍在合理范围」。
    """
    src = Path(ra_mod.__file__).read_text(encoding="utf-8")
    block = re.search(r"^LIMITS = \{(.*?)^\}", src, re.S | re.M).group(1)
    # 只扫「依据：」引导的那一句，避免误伤复述历史错误的叙述性注释
    for line in block.split("\n"):
        if "依据：" not in line:
            continue
        tail = line.split("依据：", 1)[1]
        for bad in ("经验值", "合理值", "业界标准", "我觉得", "感觉"):
            assert bad not in tail, f"依据措辞无效（{bad}）：{line.strip()}"


# ══════════════════════════════════════════════════════════════
#  技能池可区分性 lint（2026-08-17 新增）
# ══════════════════════════════════════════════════════════════

class TestSkillPoolDistinctness:
    """一张 skill 没有「本目录独有」的 keyword → 触发器无法把它与同组区分。

    命中时必然整组一起命中、`rank_key` 全等，谁进 prompt 只能靠文件名字典序
    决胜（= 任意选择）。危害在 2026-08-17 分级载荷改造后放大：落选者从
    「少 ~120 字论点句」变成「少整份细则」，当选者拿到的可能是错流派的参数。

    依据：2026-08-17 全量测 —— 131 张有 keywords 的 skill 里 **76 张（58%）**
    无独有 keyword，全在 music 域 7 个角色；se 域 0 张。
    """

    @staticmethod
    def _mk(tmp: Path, role: str, pool: dict[str, list[str]]) -> Path:
        """在 tmp 下造 `20-知识/角色技能/{domain}/{role}/` 技能池，返回 vault root。"""
        d = tmp / "20-知识" / "角色技能" / "testdom" / role
        d.mkdir(parents=True, exist_ok=True)
        for stem, kws in pool.items():
            fm = ["type: skill", "trigger:", "  keywords:"]
            fm += [f"    - {k!r}" for k in kws]
            (d / f"{stem}.md").write_text(
                "---\n" + "\n".join(fm) + "\n---\n\n## 核心约束\n一句话。\n",
                encoding="utf-8",
            )
        return tmp

    @staticmethod
    def _role_note(tmp: Path, role: str, seg: str = "testdom") -> Path:
        """造出角色基因文件本体 —— 2026-08-25 起技能目录段由它的位置决定。"""
        p = tmp / "00-系统" / "角色基因" / seg / f"角色-{role}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("---\nrole: x\n---\n\n## 1. 核心\n略\n", encoding="utf-8")
        return p

    def _run(self, tmp: Path, pool: dict[str, list[str]], monkeypatch) -> list[str]:
        self._mk(tmp, "测试角色", pool)
        note = self._role_note(tmp, "测试角色")
        monkeypatch.setattr(ra_mod, "VAULT_ROOT", tmp)
        return ra_mod._indistinct_skills_in_pool(note)

    def test_签名完全相同的组全部报出(self, tmp_path: Path, monkeypatch):
        """复刻 `music/编曲` 的 6 张 R&B 同签名。"""
        out = self._run(tmp_path, {
            "Ar1": ["soul", "r&b"], "Ar2": ["soul", "r&b"], "Ar3": ["soul", "r&b"],
        }, monkeypatch)
        assert len(out) == 3, f"3 张同签名都该报出，实得 {out}"
        assert all("签名相同" in x for x in out)

    def test_有独有词的不报(self, tmp_path: Path, monkeypatch):
        """`Ar7-R&B-地域风格差异` 有 10 个独有词（Motown/Stax…），不该被报。"""
        out = self._run(tmp_path, {
            "Ar1": ["soul"], "Ar2": ["soul"], "Ar7": ["soul", "motown"],
        }, monkeypatch)
        assert [x.split("（")[0] for x in out] == ["Ar1", "Ar2"], f"实得 {out}"

    def test_se式任务性keyword全不报(self, tmp_path: Path, monkeypatch):
        """se 域每个 keyword 唯一（实测 df>1 计数为 0）→ 应零报出。"""
        out = self._run(tmp_path, {
            "B1": ["os.environ"], "B5": ["fetchone"], "B6": ["StaticFiles"],
        }, monkeypatch)
        assert out == []

    def test_单张skill不参与判定(self, tmp_path: Path, monkeypatch):
        assert self._run(tmp_path, {"只有一张": ["soul"]}, monkeypatch) == []

    def test_目录不存在返回空不抛错(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(ra_mod, "VAULT_ROOT", tmp_path)
        assert ra_mod._indistinct_skills_in_pool(
            self._role_note(tmp_path, "不存在", seg="nodom")) == []
        assert ra_mod._indistinct_skills_in_pool(
            self._role_note(tmp_path, "空域", seg="")) == []
        assert ra_mod._indistinct_skills_in_pool(
            tmp_path / "00-系统" / "角色基因" / "testdom" / "不带前缀.md") == []
        # 角色文件不在 00-系统/角色基因/ 子树下 → 无法定段，返回 None 而非猜
        assert ra_mod._indistinct_skills_in_pool(tmp_path / "别处" / "角色-流浪.md") == []

    def test_domain与目录段不等时按文件位置解析(self, tmp_path: Path, monkeypatch):
        """SE 角色 `domain: 技术开发` 但目录段是 `se` —— 必须按文件位置走。

        2026-08-25 实测：原实现按 frontmatter `domain` 拼路径，于是一直在找不
        存在的 `20-知识/角色技能/技术开发/`，本 lint 对全部 7 个 SE 角色**从未
        执行过**（se 域真答案恰好也是 0，所以没露馅）。
        """
        self._mk(tmp_path, "后端工程师", {
            "B1": ["soul"], "B2": ["soul"],      # 同签名，正确解析时必报 2 条
        })
        # 角色文件放 se/ 段，frontmatter 却写 domain: 技术开发
        p = tmp_path / "00-系统" / "角色基因" / "se" / "角色-后端工程师.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("---\nrole: 后端工程师\ndomain: 技术开发\n---\n\n## 1. 核心\n略\n",
                     encoding="utf-8")
        # _mk 造的池在 testdom 段，这里要的是 se 段
        d = tmp_path / "20-知识" / "角色技能" / "se" / "后端工程师"
        d.mkdir(parents=True, exist_ok=True)
        for stem in ("B1", "B2"):
            (d / f"{stem}.md").write_text(
                "---\ntype: skill\ntrigger:\n  keywords:\n    - 'soul'\n---\n\n## 核心约束\n略\n",
                encoding="utf-8",
            )
        monkeypatch.setattr(ra_mod, "VAULT_ROOT", tmp_path)
        out = ra_mod._indistinct_skills_in_pool(p)
        assert len(out) == 2, f"按文件位置(se)应扫到 2 张同签名，实得 {out}"

    def test_报告里出现修法指引(self, tmp_path: Path, monkeypatch):
        """lint 不能只报数字 —— 必须给出根因与怎么改，否则读报告的人不知道干什么。

        走真实 `_measure_role` → `_format_measurements` 全链路（不手搓 measure
        dict：那会随字段增删静默失配，本测试第一版就因此 KeyError）。
        """
        self._mk(tmp_path, "测试角色", {"A": ["soul"], "B": ["soul"]})
        monkeypatch.setattr(ra_mod, "VAULT_ROOT", tmp_path)
        m = ra_mod._measure_role(
            _write(tmp_path, _body(), domain="testdom", seg="testdom")
        )
        assert len(m["skill_pool_indistinct"]) == 2, "前提不成立：lint 没报出"
        text = ra_mod._format_measurements([m])
        assert "无任何独有 keyword" in text
        assert "任务性" in text and "字典序" in text
        assert "A（与 B 签名相同）" in text, "必须列出具体是哪几张"
