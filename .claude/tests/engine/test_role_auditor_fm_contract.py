"""
tests/engine/test_role_auditor_fm_contract.py
    —— frontmatter 软上限的契约字段排除（2026-08-15 改动）

背景：规范 §11 鼓励 output_contract / input_contract 契约化，但 §4 的 frontmatter
软上限把契约模板一并计入 —— **越遵循 §11 的角色越必然违反 §4**
（2026-08-13 审计 [[角色基因劣化对比-2026-08-13]] §2.2「越合规越超标」已定性）。

本次改动两件事：
  1. LIMITS["frontmatter"] 800 → 2000（实测校准，见 main.py LIMITS 注释）
  2. 新增 _split_fm_contract：契约字段拆出单独计数，不计入软上限

覆盖：切分口径的边界 / 计数字段的守恒 / 阈值判定改用治理口径 / 表格渲染附注。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from role_auditor import main as ra_mod


# ══════════════════════════════════════════════════════════════
#  _split_fm_contract —— 切分口径
# ══════════════════════════════════════════════════════════════

_NO_CONTRACT = """role: 批判者
domain: 元
model: claude-opus-5
aliases:
  - critic
  - 挑刺者
upstream: []
downstream: []"""

_OUTPUT_CONTRACT = """role: 技术主管
domain: se
output_contract:
  parameterizable: true
  fields:
    mode:
      type: str
      default: legacy
  templates:
    legacy:
      - 10-项目/{project}/指令/给后端.md
tools: []"""

_BOTH_CONTRACTS = """role: 前端工程师
input_contract:
  parameterizable: true
  fields:
    side:
      type: str
output_contract:
  parameterizable: true
  templates:
    modular:
      - 10-项目/{project}/模块/{module}.md
version: 1.2.0"""


def test_无契约字段时原文一字不动():
    kept, dropped = ra_mod._split_fm_contract(_NO_CONTRACT)
    assert kept == _NO_CONTRACT
    assert dropped == ""


def test_拆出_output_contract():
    kept, dropped = ra_mod._split_fm_contract(_OUTPUT_CONTRACT)
    assert "output_contract" not in kept
    assert "parameterizable" not in kept
    # 非契约键必须完整保留
    assert "role: 技术主管" in kept
    assert "domain: se" in kept
    assert "tools: []" in kept
    # 契约块必须完整落到 dropped
    assert "output_contract:" in dropped
    assert "templates:" in dropped
    assert "给后端.md" in dropped


def test_拆出_input_contract_与两者并存():
    kept, dropped = ra_mod._split_fm_contract(_BOTH_CONTRACTS)
    assert "input_contract" not in kept
    assert "output_contract" not in kept
    assert "role: 前端工程师" in kept
    assert "version: 1.2.0" in kept
    assert "input_contract:" in dropped
    assert "output_contract:" in dropped


def test_契约块的缩进行与列表项不被误判为顶层键():
    """`  - 10-项目/...` 与 `    type: str` 必须归属 contract，不能漏回 kept。

    这是切分口径的核心：顶层键在列 0，缩进行 / `- ` 行归属当前键。
    """
    kept, _ = ra_mod._split_fm_contract(_OUTPUT_CONTRACT)
    for leaked in ("parameterizable", "default: legacy", "给后端.md", "type: str"):
        assert leaked not in kept, f"契约子行 {leaked!r} 漏回了计数部分"


def test_契约键在开头():
    src = "output_contract:\n  parameterizable: true\nrole: X\ndomain: se"
    kept, dropped = ra_mod._split_fm_contract(src)
    assert "output_contract" not in kept
    assert "role: X" in kept and "domain: se" in kept
    assert "parameterizable" in dropped


def test_契约键在末尾():
    src = "role: X\ndomain: se\ninput_contract:\n  parameterizable: true"
    kept, dropped = ra_mod._split_fm_contract(src)
    assert kept == "role: X\ndomain: se"
    assert "input_contract" in dropped


def test_只有契约键():
    src = "output_contract:\n  parameterizable: true"
    kept, dropped = ra_mod._split_fm_contract(src)
    assert kept == ""
    assert "output_contract" in dropped


def test_首个顶层键之前的内容归入计数部分():
    """正常 frontmatter 不该有这种内容，但不能因此丢字符。"""
    src = "# 一行注释\nrole: X\noutput_contract:\n  parameterizable: true"
    kept, dropped = ra_mod._split_fm_contract(src)
    assert "# 一行注释" in kept
    assert "role: X" in kept
    assert "output_contract" in dropped


def test_键名相似但不是契约键的不被误伤():
    """`output_contracts` / `my_output_contract` 不在封闭枚举内，必须保留。"""
    src = "role: X\noutput_contracts: 1\nmy_output_contract: 2\ncontract: 3"
    kept, dropped = ra_mod._split_fm_contract(src)
    assert kept == src
    assert dropped == ""


# ══════════════════════════════════════════════════════════════
#  阈值常量 —— 锁死，防止无据回改
# ══════════════════════════════════════════════════════════════

def test_frontmatter_阈值为_2000():
    """2026-08-15 实测校准：27 个角色 frontmatter 合计 24,090 字符，
    真正进 prompt 仅 3,158（13%）—— 该阈值衡量可维护性而非 token 成本。
    改这个数需要新的实测依据（项目 CLAUDE.md「阈值来源必须显式声明」）。"""
    assert ra_mod.LIMITS["frontmatter"] == 2000


def test_契约键是封闭枚举():
    assert ra_mod._CONTRACT_KEYS == frozenset({"output_contract", "input_contract"})


# ══════════════════════════════════════════════════════════════
#  _measure_role —— 计数守恒与阈值判定
# ══════════════════════════════════════════════════════════════

_BODY = """
# 角色：测试角色

## 1. 核心使命
用于单测的最小角色骨架，正文长度不参与本文件的断言。

## 2. 输入输出
参见 frontmatter `inputs` / `outputs`。

## 3. 职责
做测试要求的事，不越界。

## 4. 边界
不产出本文件断言之外的内容。

## 6. 工作流
读输入 → 产出 → 结束。
"""


def _write_role(tmp_path: Path, fm: str) -> Path:
    p = tmp_path / "角色-测试角色.md"
    p.write_text(f"---\n{fm}\n---\n{_BODY}", encoding="utf-8")
    return p


def test_无契约角色的三个计数字段一致(tmp_path: Path):
    m = ra_mod._measure_role(_write_role(tmp_path, _NO_CONTRACT))
    assert m["frontmatter_contract_chars"] == 0
    assert m["frontmatter_chars"] == m["frontmatter_chars_total"]


def test_有契约角色的计数守恒(tmp_path: Path):
    """治理口径 + 契约口径 == 原总计。少一个字符都说明切分丢了内容。"""
    m = ra_mod._measure_role(_write_role(tmp_path, _OUTPUT_CONTRACT))
    assert m["frontmatter_contract_chars"] > 0
    assert m["frontmatter_chars"] + m["frontmatter_contract_chars"] == m["frontmatter_chars_total"]


def test_契约撑爆总长但治理口径不超时不报超限(tmp_path: Path):
    """**本次改动的核心行为**：契约模板把总长顶过 2000，但治理口径仍在限内 →
    不应再报 fm_over_limit。这正是 2026-08-13 审计所说「越合规越超标」的解法。"""
    padding = "\n".join(f"      - 10-项目/{{project}}/模块/m{i:03d}.md" for i in range(120))
    fm = (
        "role: 前端工程师\ndomain: se\n"
        "output_contract:\n  parameterizable: true\n  templates:\n    modular:\n"
        + padding
    )
    m = ra_mod._measure_role(_write_role(tmp_path, fm))

    assert m["frontmatter_chars_total"] > ra_mod.LIMITS["frontmatter"], "前提不成立：总长没超限"
    assert m["frontmatter_chars"] <= ra_mod.LIMITS["frontmatter"]
    assert m["fm_over_limit"] is False


def test_治理口径真超限时仍然报超限(tmp_path: Path):
    """排除契约不等于放弃治理：非契约字段自己撑爆，照样要报。"""
    fm = "role: X\ndomain: se\nskills:\n" + "\n".join(
        f"  - 技能条目占位文本用于把 frontmatter 撑过两千字符上限-{i:03d}" for i in range(60)
    )
    m = ra_mod._measure_role(_write_role(tmp_path, fm))

    assert m["frontmatter_contract_chars"] == 0
    assert m["frontmatter_chars"] > ra_mod.LIMITS["frontmatter"]
    assert m["fm_over_limit"] is True


# ══════════════════════════════════════════════════════════════
#  _format_measurements —— 表格附注
# ══════════════════════════════════════════════════════════════

@pytest.fixture
def measure(tmp_path: Path):
    """从真实 _measure_role 派生测量字典再按需覆盖。

    不手写字面量：_format_measurements 读十几个键，手写的 fixture 一旦漏键就是
    KeyError（本文件初版正因此挂了 3 个用例），而且测量 schema 演进后会假过。
    """
    def _make(**over) -> dict:
        m = ra_mod._measure_role(_write_role(tmp_path, _NO_CONTRACT))
        m.update(over)
        return m
    return _make


def test_契约非零时表格附注契约字符数(measure):
    out = ra_mod._format_measurements([measure(
        frontmatter_chars=1000, frontmatter_contract_chars=1281, frontmatter_chars_total=2281)])
    assert "1000 (+1281契约)" in out


def test_契约为零时表格不附注(measure):
    m = measure(frontmatter_chars=900, frontmatter_contract_chars=0)
    out = ra_mod._format_measurements([m])
    assert "契约)" not in out
    assert "| 900 " in out


def test_缺字段时不炸(measure):
    """旧测量字典（无 frontmatter_contract_chars 键）不应让渲染抛 KeyError。

    渲染侧用的是 `m.get(...) or 0`，这条锁死那个 get —— 改成下标访问就会挂。
    """
    m = measure()
    del m["frontmatter_contract_chars"]
    out = ra_mod._format_measurements([m])
    assert "契约)" not in out
