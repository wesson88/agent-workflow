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


# ══════════════════════════════════════════════════════════
# _filter_self_dynamic 自身 DYNAMIC 区过滤
# ══════════════════════════════════════════════════════════

from prompt_builder import _filter_self_dynamic


class TestFilterSelfDynamic:
    """body 中角色自身 DYNAMIC 区按 KEEP/NEW/GRADUATE? 过滤；其余删除。"""

    def test_keep_patch_preserved(self):
        body = _make_dynamic_body("- KEEP 约束内容", "KEEP")
        result = _filter_self_dynamic(body)
        assert "<!-- DYNAMIC_START -->" in result
        assert "[KEEP]" in result
        assert "KEEP 约束内容" in result

    def test_new_patch_preserved(self):
        body = _make_dynamic_body("- NEW 候选约束", "NEW")
        result = _filter_self_dynamic(body)
        assert "[NEW]" in result
        assert "NEW 候选约束" in result

    def test_graduate_question_preserved(self):
        body = _make_dynamic_body("- 推荐合入", "GRADUATE?")
        result = _filter_self_dynamic(body)
        assert "[GRADUATE?]" in result

    def test_drop_question_filtered(self):
        body = _make_dynamic_body("- 推荐删除约束", "DROP?")
        result = _filter_self_dynamic(body)
        # DROP? 是唯一 patch → 整 DYNAMIC 区删除
        assert "<!-- DYNAMIC_START -->" not in result
        assert "DROP?" not in result

    def test_drop_filtered(self):
        body = _make_dynamic_body("- 已确认删除", "DROP")
        result = _filter_self_dynamic(body)
        assert "<!-- DYNAMIC_START -->" not in result

    def test_graduate_filtered(self):
        body = _make_dynamic_body("- 临时态待 graduator", "GRADUATE")
        result = _filter_self_dynamic(body)
        assert "<!-- DYNAMIC_START -->" not in result

    def test_mixed_keep_and_drop_only_keep_remains(self):
        body = (
            "# 角色正文\n\n<!-- DYNAMIC_START -->\n"
            "# Patch [2026-01-01] [KEEP] 保留\n"
            "- KEEP 约束\n"
            "# Patch [2026-01-02] [DROP?] 应删\n"
            "- DROP 约束\n"
            "<!-- DYNAMIC_END -->\n"
        )
        result = _filter_self_dynamic(body)
        assert "[KEEP]" in result
        assert "KEEP 约束" in result
        assert "[DROP?]" not in result
        assert "DROP 约束" not in result

    def test_no_dynamic_region_unchanged(self):
        body = "# 角色正文\n\n## 1. 使命\n内容\n## 2. 边界\n内容"
        result = _filter_self_dynamic(body)
        assert result.strip() == body.strip()

    def test_empty_dynamic_region_removed(self):
        body = (
            "# 角色正文\n\n<!-- DYNAMIC_START -->\n"
            "# 此区由复盘 agent 自动维护\n"
            "<!-- DYNAMIC_END -->\n"
        )
        result = _filter_self_dynamic(body)
        # 全是注释行 → 没有有效 patch → 整段删除
        assert "<!-- DYNAMIC_START -->" not in result


# ══════════════════════════════════════════════════════════
# _strip_version_history §8 版本历史剥除
# ══════════════════════════════════════════════════════════

from prompt_builder import _strip_version_history


class TestStripVersionHistory:
    """§8 版本历史是 vault 内 review 用，对 LLM 行为无指导，统一剥除省 token。"""

    def test_basic_strip(self):
        body = (
            "## 1. 使命\n内容\n\n"
            "## 7. 运行时补丁\n<!-- DYNAMIC_START --><!-- DYNAMIC_END -->\n\n"
            "## 8. 版本历史\n\n"
            "- v1.5.0 (2026-05-24): some change\n"
            "- v1.4.0 (2026-05-11): earlier change\n"
        )
        result = _strip_version_history(body)
        assert "## 8. 版本历史" not in result
        assert "v1.5.0" not in result
        assert "## 1. 使命" in result
        assert "## 7. 运行时补丁" in result

    def test_no_section_8_unchanged(self):
        body = "## 1. 使命\n内容\n\n## 2. 范围\n内容"
        result = _strip_version_history(body)
        assert result.strip() == body.strip()

    def test_section_8_at_very_end(self):
        body = "## 1. 使命\n内容\n\n## 8. 历史\n- v0.1.0"
        result = _strip_version_history(body)
        assert "## 8." not in result
        assert "v0.1.0" not in result

    def test_section_8_with_section_9_below(self):
        """§9+ 异常区也一并剥除（按规范 §8 应是末节）。"""
        body = (
            "## 1. 使命\n内容\n\n"
            "## 8. 版本历史\n- v1.0\n\n"
            "## 9. 附录\n额外内容\n"
        )
        result = _strip_version_history(body)
        assert "## 8." not in result
        assert "## 9." not in result
        assert "额外内容" not in result

    def test_section_8_variant_title(self):
        body = "## 1. 使命\n内容\n\n## 8. Version History\n- v1.0"
        result = _strip_version_history(body)
        assert "## 8." not in result

    def test_does_not_match_section_8_inline(self):
        """正文里出现 "§8" 或 "8." 字面字符串不应被误判（必须是行首 `## 8.`）。"""
        body = "## 1. 使命\n参考 §8 的版本号\n8. 是个数字\n\n## 2. 范围\n内容"
        result = _strip_version_history(body)
        assert "## 1. 使命" in result
        assert "## 2. 范围" in result
        assert "参考 §8 的版本号" in result
