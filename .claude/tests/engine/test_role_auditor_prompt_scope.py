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


def _write(tmp_path: Path, body: str, domain: str = "se") -> Path:
    p = tmp_path / "角色-测试角色.md"
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
