"""
test_rolling_brief_lint.py — rolling_brief.md 静态校验测试

依据 [[rolling-brief.schema]] v0.1.0 §7：
- 7.1 必填 9 节
- 7.3 条目子字段（source + confidence）
- 7.4 强制 confidence（§1 / §7 必须 high）
- 7.5 §5 已否决方向强制 reason
- 7.6 source 前缀允许列表
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT / ".claude"))

from engine.rolling_brief_lint import validate_rolling_brief


def _valid_brief() -> str:
    """所有规则都满足的 9 节 brief。"""
    return """# Rolling Brief — R2

> 更新时间：2026-06-13
> 产出：[[角色-创意记录员]]

## 1. 用户已确认事实

- 首版面向通勤用户。
  source: user_answer-R2
  confidence: high

## 2. LLM 推断

- 通勤用户更关心路线异常，而非完整地图浏览。
  source: 创意质询-R2.md#MVP 缩小建议
  confidence: medium

## 3. 已做决策

- MVP 暂不做完整导航。
  source: 创意记录员-R2
  confidence: medium

## 4. 已保留方向

- 通勤路线异常提醒
  source: 创意质询-R2.md#值得保留的方向
  confidence: medium

## 5. 已否决方向

- 全功能地图导航
  source: 创意质询-R1.md#应该砍掉的方向
  reason: 范围过大，且与成熟竞品正面竞争
  confidence: high

## 6. 关键争议

- 是否需要离线地图？
  source: 创意发散-R2.md vs 创意质询-R2.md
  confidence: medium

## 7. 已回答问题

- 是否支持账号体系？答：不需要
  source: user_answer-R2
  confidence: high

## 8. 未回答问题

- 首版更重视通勤导航，还是地点搜索？
  source: 创意记录员-R2
  confidence: medium

## 9. 下一轮焦点

- MVP 边界
  source: brainstorm_readiness-R2#next_round_focus
  confidence: medium
"""


class TestRequiredSections:
    """§7.1 必填 9 节按顺序齐全"""

    def test_valid_brief_passes(self):
        errs = validate_rolling_brief(_valid_brief())
        assert errs == [], f"Valid brief should pass, got: {errs}"

    def test_missing_section_1_fails(self):
        text = _valid_brief().replace("## 1. 用户已确认事实", "## 1.X 误名")
        errs = validate_rolling_brief(text)
        assert any("[§7.1]" in e and "1. 用户已确认事实" in e for e in errs)

    def test_missing_section_5_fails(self):
        text = _valid_brief().replace("## 5. 已否决方向", "## 5.X 误名")
        errs = validate_rolling_brief(text)
        assert any("[§7.1]" in e and "5. 已否决方向" in e for e in errs)

    def test_missing_section_9_fails(self):
        text = _valid_brief().replace("## 9. 下一轮焦点", "## 9.X 误名")
        errs = validate_rolling_brief(text)
        assert any("[§7.1]" in e and "9. 下一轮焦点" in e for e in errs)


class TestItemSubfields:
    """§7.3 每条目必须有 source + confidence"""

    def test_item_missing_source_fails(self):
        text = _valid_brief().replace(
            "- 通勤路线异常提醒\n"
            "  source: 创意质询-R2.md#值得保留的方向\n"
            "  confidence: medium",
            "- 通勤路线异常提醒\n  confidence: medium",
        )
        errs = validate_rolling_brief(text)
        assert any("[§7.3]" in e and "缺 source" in e for e in errs)

    def test_item_missing_confidence_fails(self):
        text = _valid_brief().replace(
            "- 通勤路线异常提醒\n"
            "  source: 创意质询-R2.md#值得保留的方向\n"
            "  confidence: medium",
            "- 通勤路线异常提醒\n  source: 创意质询-R2.md#值得保留的方向",
        )
        errs = validate_rolling_brief(text)
        assert any("[§7.3]" in e and "缺 confidence" in e for e in errs)

    def test_invalid_confidence_value_fails(self):
        text = _valid_brief().replace(
            "  source: 创意质询-R2.md#值得保留的方向\n  confidence: medium",
            "  source: 创意质询-R2.md#值得保留的方向\n  confidence: 高",
            1,
        )
        errs = validate_rolling_brief(text)
        assert any("[§7.3]" in e and "非法" in e for e in errs)


class TestRequireHighConfidence:
    """§7.4 §1 用户事实 / §7 已回答问题 confidence 必须 high"""

    def test_section1_medium_fails(self):
        text = _valid_brief().replace(
            "- 首版面向通勤用户。\n"
            "  source: user_answer-R2\n"
            "  confidence: high",
            "- 首版面向通勤用户。\n"
            "  source: user_answer-R2\n"
            "  confidence: medium",
        )
        errs = validate_rolling_brief(text)
        assert any("[§7.4]" in e and "1. 用户已确认事实" in e for e in errs)

    def test_section7_low_fails(self):
        text = _valid_brief().replace(
            "- 是否支持账号体系？答：不需要\n"
            "  source: user_answer-R2\n"
            "  confidence: high",
            "- 是否支持账号体系？答：不需要\n"
            "  source: user_answer-R2\n"
            "  confidence: low",
        )
        errs = validate_rolling_brief(text)
        assert any("[§7.4]" in e and "7. 已回答问题" in e for e in errs)


class TestRequireReason:
    """§7.5 §5 已否决方向必须有 reason 子字段"""

    def test_section5_missing_reason_fails(self):
        text = _valid_brief().replace(
            "- 全功能地图导航\n"
            "  source: 创意质询-R1.md#应该砍掉的方向\n"
            "  reason: 范围过大，且与成熟竞品正面竞争\n"
            "  confidence: high",
            "- 全功能地图导航\n"
            "  source: 创意质询-R1.md#应该砍掉的方向\n"
            "  confidence: high",
        )
        errs = validate_rolling_brief(text)
        assert any("[§7.5]" in e for e in errs)


class TestSourcePrefix:
    """§7.6 source 前缀必须匹配允许列表"""

    @pytest.mark.parametrize("source", [
        "idea.md",
        "user_answer-R1",
        "user_answer-R12",
        "创意发散-R1.md",
        "创意发散-R3.md#章节",
        "创意质询-R5.md",
        "创意记录员-R2",
        "创意记录员-R2#决策",
        "brainstorm_readiness-R2",
        "brainstorm_readiness-R2#next_round_focus",
        "产品创意原型-R2",
    ])
    def test_valid_source_prefix(self, source):
        text = _valid_brief().replace(
            "  source: 创意发散-R2.md vs 创意质询-R2.md",
            f"  source: {source}",
        )
        errs = validate_rolling_brief(text)
        prefix_errs = [e for e in errs if "[§7.6]" in e]
        assert prefix_errs == [], f"Source {source!r} should pass, got: {prefix_errs}"

    def test_invalid_source_prefix_fails(self):
        text = _valid_brief().replace(
            "  source: 创意质询-R2.md#值得保留的方向",
            "  source: 随便瞎写-R2.md",
        )
        errs = validate_rolling_brief(text)
        assert any("[§7.6]" in e for e in errs)

    def test_multi_source_one_invalid_fails(self):
        text = _valid_brief().replace(
            "  source: 创意发散-R2.md vs 创意质询-R2.md",
            "  source: 创意发散-R2.md vs 瞎编",
        )
        errs = validate_rolling_brief(text)
        assert any("[§7.6]" in e for e in errs)

    def test_multi_source_all_valid_passes(self):
        text = _valid_brief().replace(
            "  source: 创意发散-R2.md vs 创意质询-R2.md",
            "  source: 创意发散-R2.md, 创意质询-R2.md, idea.md",
        )
        errs = validate_rolling_brief(text)
        prefix_errs = [e for e in errs if "[§7.6]" in e]
        assert prefix_errs == [], f"All valid multi-source should pass, got: {prefix_errs}"


class TestEmptySection:
    """空节（无 list item）应允许（§7.2 是软规则，不在 lint）"""

    def test_empty_section_passes(self):
        text = _valid_brief().replace(
            "## 6. 关键争议\n\n"
            "- 是否需要离线地图？\n"
            "  source: 创意发散-R2.md vs 创意质询-R2.md\n"
            "  confidence: medium\n",
            "## 6. 关键争议\n\n",
        )
        errs = validate_rolling_brief(text)
        assert errs == [], f"Empty §6 should pass, got: {errs}"
