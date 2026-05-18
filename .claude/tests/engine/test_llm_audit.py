"""
.claude/tests/engine/test_llm_audit.py — token 入口护栏单测

覆盖 `engine.llm._audit_token_budget` 的两道护栏：
- 护栏 1：system 单独阈值（WARN / RAISE）
- 护栏 2：总量阈值
    - 无 input_budget → 走 context_window 比例
    - 有 input_budget → 走显式 token 数（RAISE = input_budget，WARN = 60% 该值）
"""

from __future__ import annotations

import pytest

from engine import llm as llm_mod
from engine.llm import _audit_token_budget


@pytest.fixture
def mock_tokens(monkeypatch):
    """mock token_counter，让测试可控指定 system / user / cw token 数。

    使用方式：调用 set_tokens(static=, dynamic=, user=, cw=)
    """
    counts = {"static": 0, "dynamic": 0, "user": 0, "cw": 200_000}

    def fake_count(text, model):
        # 按文本中字符数 (a/b/c) 当作 token 计数标记，方便手工构造
        # 实际不用：直接用 closure 里的字段
        return counts.get(text, 0)

    def fake_cw(model):
        return counts["cw"]

    from engine import token_counter
    monkeypatch.setattr(token_counter, "count_tokens", fake_count)
    monkeypatch.setattr(token_counter, "get_context_window", fake_cw)

    def setter(*, static=0, dynamic=0, user=0, cw=200_000):
        counts["static"] = static
        counts["dynamic"] = dynamic
        counts["user"] = user
        counts["cw"] = cw

    return setter


def _run(static="static", dynamic="dynamic", user="user", **kwargs):
    """方便调用 _audit_token_budget，文本字符串作为 mock 的 key。"""
    return _audit_token_budget("claude-sonnet-4-6", static, dynamic, user, **kwargs)


# ── 护栏 1：system 单独阈值 ───────────────────────────────
class TestSystemThreshold:
    def test_under_warn_is_silent(self, mock_tokens, capsys):
        mock_tokens(static=3000, dynamic=2000, user=1000)
        _run()
        assert "system prompt" not in capsys.readouterr().err

    def test_warn_below_raise(self, mock_tokens, capsys):
        # 8K + 2K = 10K，> WARN 8K，< RAISE 20K
        mock_tokens(static=8500, dynamic=2000, user=1000)
        _run()
        err = capsys.readouterr().err
        assert "system prompt 偏大" in err
        assert "10500" in err

    def test_above_raise_raises(self, mock_tokens):
        # 15K + 6K = 21K > 20K RAISE
        mock_tokens(static=15000, dynamic=6000, user=1000)
        with pytest.raises(RuntimeError) as ei:
            _run()
        assert "system prompt 过大" in str(ei.value)


# ── 护栏 2 (a)：无 input_budget，走 context_window 比例 ──
class TestRatioBudget:
    def test_under_warn_ratio_is_silent(self, mock_tokens, capsys):
        # 总 30K / 200K = 15% < 50%
        mock_tokens(static=5000, dynamic=2000, user=23000, cw=200_000)
        _run()
        err = capsys.readouterr().err
        assert "input token 偏高" not in err

    def test_warn_ratio_triggers(self, mock_tokens, capsys):
        # 总 120K / 200K = 60% ≥ 50% WARN; 60% < 85% RAISE
        mock_tokens(static=5000, dynamic=2000, user=113000, cw=200_000)
        _run()
        err = capsys.readouterr().err
        assert "input token 偏高" in err
        assert "60.0%" in err

    def test_raise_ratio_triggers(self, mock_tokens):
        # 总 180K / 200K = 90% ≥ 85% RAISE
        mock_tokens(static=5000, dynamic=2000, user=173000, cw=200_000)
        with pytest.raises(RuntimeError) as ei:
            _run()
        assert "input token 总量触顶" in str(ei.value)


# ── 护栏 2 (b)：有 input_budget，走显式预算 ──────────────
class TestExplicitBudget:
    def test_under_warn_silent(self, mock_tokens, capsys):
        # 总 40K，budget=80K → WARN at 48K，未触发
        mock_tokens(static=5000, dynamic=2000, user=33000, cw=200_000)
        _run(input_budget=80000)
        err = capsys.readouterr().err
        assert "input token" not in err

    def test_warn_threshold_at_60_percent(self, mock_tokens, capsys):
        # 总 50K，budget=80K → WARN at 48K，触发；50K < 80K，不 RAISE
        mock_tokens(static=5000, dynamic=2000, user=43000, cw=200_000)
        _run(input_budget=80000)
        err = capsys.readouterr().err
        assert "接近角色预算" in err

    def test_raise_at_budget(self, mock_tokens):
        # 总 85K ≥ 80K budget → RAISE
        mock_tokens(static=5000, dynamic=2000, user=78000, cw=200_000)
        with pytest.raises(RuntimeError) as ei:
            _run(input_budget=80000)
        msg = str(ei.value)
        assert "超角色预算" in msg
        assert "80000" in msg

    def test_explicit_budget_bypasses_ratio_logic(self, mock_tokens, capsys):
        """有 input_budget 时，即便总量超 context_window 95%，
        只要在 budget 内也不应触发 RAISE。"""
        # 总 70K，cw=80K → ratio 87.5% 本会 raise；但 budget=100K 兜底
        mock_tokens(static=5000, dynamic=2000, user=63000, cw=80_000)
        _run(input_budget=100_000)
        # 不抛错，且只走 budget 路径 → 70K < 60K warn 阈值（100K*0.6=60K）→ 触发 warn
        err = capsys.readouterr().err
        assert "接近角色预算" in err
        # 但不应有 ratio 路径的 "input token 偏高/触顶" 字样
        assert "input token 偏高" not in err
        assert "input token 总量触顶" not in err

    def test_zero_budget_falls_back_to_ratio(self, mock_tokens, capsys):
        """budget=0 当作未声明，回退 ratio 路径。"""
        mock_tokens(static=5000, dynamic=2000, user=113000, cw=200_000)
        _run(input_budget=0)
        err = capsys.readouterr().err
        # ratio 路径触发 WARN
        assert "input token 偏高" in err
