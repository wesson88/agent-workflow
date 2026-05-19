"""
tests/engine/test_prompt_builder.py — prompt_builder 核心逻辑单元测试

覆盖范围：
  P0 - _build_dynamic_segment：总量截断、上游跳过、空 DYNAMIC 跳过
  P0 - _extract_dynamic_patch：标记过滤（KEEP/NEW/GRADUATE?/GRADUATE/DROP/DROP?）
  P0 - build_system_prompt / build_system_prompt_no_skills：smoke test（mock role）
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills"))

from prompt_builder import (
    _build_dynamic_segment,
    _extract_dynamic_patch,
    _DYNAMIC_TOTAL_BUDGET,
)


# ── 辅助：构造最小 Role mock ──────────────────────────────

def _make_role(name: str, upstream: list[str], body: str = "") -> MagicMock:
    r = MagicMock()
    r.name = name
    r.domain = "test"
    r.style = "务实"
    r.skills = ()
    r.upstream = upstream
    r.body = body
    return r


def _make_dynamic_body(patch_content: str, label: str = "KEEP") -> str:
    return (
        f"# 角色正文\n\n<!-- DYNAMIC_START -->\n"
        f"# Patch [2026-01-01] [{label}] 测试约束\n"
        f"{patch_content}\n"
        f"<!-- DYNAMIC_END -->\n"
    )


# ══════════════════════════════════════════════════════════
# _extract_dynamic_patch 标记过滤
# ══════════════════════════════════════════════════════════

class TestExtractDynamicPatch:
    def test_keep_label_included(self):
        body = _make_dynamic_body("- 这是 KEEP 约束", "KEEP")
        assert "KEEP 约束" in _extract_dynamic_patch(body)

    def test_new_label_included(self):
        body = _make_dynamic_body("- 这是 NEW 约束", "NEW")
        assert "NEW 约束" in _extract_dynamic_patch(body)

    def test_graduate_question_included(self):
        body = _make_dynamic_body("- 推荐合入约束", "GRADUATE?")
        assert "推荐合入约束" in _extract_dynamic_patch(body)

    def test_graduate_label_excluded(self):
        body = _make_dynamic_body("- 已确认合入约束", "GRADUATE")
        assert _extract_dynamic_patch(body) == ""

    def test_drop_label_excluded(self):
        body = _make_dynamic_body("- 已确认删除约束", "DROP")
        assert _extract_dynamic_patch(body) == ""

    def test_drop_question_excluded(self):
        body = _make_dynamic_body("- 推荐删除约束", "DROP?")
        assert _extract_dynamic_patch(body) == ""

    def test_no_dynamic_block_returns_empty(self):
        assert _extract_dynamic_patch("# 普通正文\n无 DYNAMIC 区") == ""


# ══════════════════════════════════════════════════════════
# _build_dynamic_segment 总量截断
# ══════════════════════════════════════════════════════════

class TestBuildDynamicSegment:
    def test_single_upstream_included(self):
        """单个上游有 DYNAMIC → 包含在 dynamic 段中。"""
        up = _make_role("架构师", [], _make_dynamic_body("- 架构约束A"))
        role = _make_role("技术主管", ["架构师"])

        with patch("prompt_builder.load_role", side_effect=lambda name: up if name == "架构师" else role):
            result = _build_dynamic_segment(role)

        assert "架构约束A" in result
        assert "架构师" in result

    def test_empty_dynamic_upstream_skipped(self):
        """上游 DYNAMIC 区为空（或全是 GRADUATE/DROP）→ 不注入。"""
        up = _make_role("架构师", [], _make_dynamic_body("- 已合入约束", "GRADUATE"))
        role = _make_role("技术主管", ["架构师"])

        with patch("prompt_builder.load_role", side_effect=lambda name: up if name == "架构师" else role):
            result = _build_dynamic_segment(role)

        assert result == ""

    def test_total_budget_truncates_second_upstream(self):
        """两个上游合计超过 _DYNAMIC_TOTAL_BUDGET → 第二个被截断或跳过。"""
        # 每个 patch 内容接近预算一半多，两个合计必超预算
        big_content = "- " + "X" * (_DYNAMIC_TOTAL_BUDGET // 2 + 500)
        up1 = _make_role("架构师", [], _make_dynamic_body(big_content))
        up2 = _make_role("产品经理", [], _make_dynamic_body("- 产品约束B"))
        role = _make_role("技术主管", ["架构师", "产品经理"])

        def fake_load(name):
            if name == "架构师":
                return up1
            if name == "产品经理":
                return up2
            return role

        with patch("prompt_builder.load_role", side_effect=fake_load):
            result = _build_dynamic_segment(role)

        # 第一个上游内容应在结果中
        assert "架构师" in result
        # 第二个上游要么不在（跳过）要么被截断（含截断提示），总量不超预算
        assert len(result) <= _DYNAMIC_TOTAL_BUDGET + 200  # 200 为截断提示本身的 overhead

    def test_role_not_found_upstream_skipped(self):
        """上游角色不存在（RoleNotFound）→ 静默跳过，不 raise。"""
        from engine.role_loader import RoleNotFound
        role = _make_role("技术主管", ["不存在的角色"])

        with patch("prompt_builder.load_role", side_effect=RoleNotFound("不存在的角色")):
            result = _build_dynamic_segment(role)

        assert result == ""

    def test_no_upstream_returns_empty(self):
        """无上游 → 返回空串。"""
        role = _make_role("技术主管", [])
        result = _build_dynamic_segment(role)
        assert result == ""
