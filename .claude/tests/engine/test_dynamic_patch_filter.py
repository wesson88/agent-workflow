"""
test_dynamic_patch_filter.py — B4 DYNAMIC 补丁 label 过滤（P10.5）。

覆盖：
- 无 DYNAMIC 区 → 空串
- 无 Patch header（老格式）→ 保持行级过滤（去 markdown 注释）
- 有 Patch header：只保留 KEEP / GRADUATE? 状态的补丁块
- NEW / DROP? 状态的补丁块被剥除
- 混合场景：多个补丁块只保留 KEEP
- HTML 注释被剥除
- markdown 注释在无 Patch header 场景被剥除
"""

from __future__ import annotations

import sys
from pathlib import Path

_SKILLS_DIR = Path(__file__).resolve().parent.parent.parent / "skills"
sys.path.insert(0, str(_SKILLS_DIR))


def _body_with_dynamic(dynamic_content: str) -> str:
    """构造含 DYNAMIC 区的角色 body。"""
    return (
        "# 角色\n\n"
        "## 1. 定位\n\n"
        "介绍角色的静态段。\n\n"
        "## 7. 运行时补丁\n\n"
        "<!-- DYNAMIC_START -->\n"
        f"{dynamic_content}\n"
        "<!-- DYNAMIC_END -->\n"
    )


class TestExtractDynamicPatch:
    def test_no_dynamic_marker_returns_empty(self):
        from common import _extract_dynamic_patch
        assert _extract_dynamic_patch("no markers here") == ""

    def test_empty_dynamic_returns_empty(self):
        from common import _extract_dynamic_patch
        body = _body_with_dynamic("")
        assert _extract_dynamic_patch(body).strip() == ""

    def test_no_patch_header_keeps_content_without_md_comments(self):
        from common import _extract_dynamic_patch
        body = _body_with_dynamic(
            "这是一段旧格式约束\n"
            "# 这是 md 注释，应被剥离\n"
            "另一段有效约束\n"
        )
        result = _extract_dynamic_patch(body)
        assert "这是一段旧格式约束" in result
        assert "另一段有效约束" in result
        assert "md 注释" not in result

    def test_keep_label_preserved(self):
        from common import _extract_dynamic_patch
        body = _body_with_dynamic(
            "# Patch [2026-05-06T23:49Z] [KEEP] F2 — 静默 catch\n"
            "捕获异常时不打印 error.\n"
            "验收：浏览器控制台无红色 Error.\n"
        )
        result = _extract_dynamic_patch(body)
        assert "[KEEP]" in result
        assert "F2 — 静默 catch" in result
        assert "浏览器控制台" in result

    def test_new_label_stripped(self):
        from common import _extract_dynamic_patch
        body = _body_with_dynamic(
            "# Patch [2026-07-01T10:00Z] [NEW] B99 — 试验性约束\n"
            "此约束未实战，不该进 prompt.\n"
        )
        result = _extract_dynamic_patch(body)
        assert "[NEW]" not in result
        assert "试验性约束" not in result
        assert result.strip() == ""

    def test_drop_label_stripped(self):
        from common import _extract_dynamic_patch
        body = _body_with_dynamic(
            "# Patch [2026-06-01T10:00Z] [DROP?] B1 — 废弃约束\n"
            "此约束已经无效.\n"
        )
        result = _extract_dynamic_patch(body)
        assert "[DROP?]" not in result
        assert "废弃约束" not in result

    def test_graduate_label_preserved(self):
        from common import _extract_dynamic_patch
        body = _body_with_dynamic(
            "# Patch [2026-06-15T10:00Z] [GRADUATE?] B7 — 待晋升\n"
            "验证中的约束仍应引导 LLM.\n"
        )
        result = _extract_dynamic_patch(body)
        assert "[GRADUATE?]" in result
        assert "B7 — 待晋升" in result

    def test_mixed_patches_only_keeps_kept_labels(self):
        from common import _extract_dynamic_patch
        body = _body_with_dynamic(
            "# Patch [2026-05-06T23:49Z] [KEEP] F2 — 静默 catch\n"
            "约束内容 A\n"
            "\n"
            "# Patch [2026-07-01T10:00Z] [NEW] B99 — 试验\n"
            "约束内容 B（不该出现）\n"
            "\n"
            "# Patch [2026-06-15T10:00Z] [GRADUATE?] B7 — 待晋升\n"
            "约束内容 C\n"
            "\n"
            "# Patch [2026-06-01T10:00Z] [DROP?] B1 — 废弃\n"
            "约束内容 D（不该出现）\n"
        )
        result = _extract_dynamic_patch(body)
        assert "约束内容 A" in result
        assert "约束内容 B" not in result
        assert "约束内容 C" in result
        assert "约束内容 D" not in result
        assert "[KEEP]" in result
        assert "[GRADUATE?]" in result
        assert "[NEW]" not in result
        assert "[DROP?]" not in result

    def test_html_comment_stripped(self):
        from common import _extract_dynamic_patch
        body = _body_with_dynamic(
            "<!-- 元角色不接收自身补丁 -->\n"
        )
        result = _extract_dynamic_patch(body)
        assert result.strip() == ""

    def test_only_last_dynamic_pair_matters(self):
        """DYNAMIC marker 在文档中出现多次（如 §说明段字面引用）时，取最后一对。"""
        from common import _extract_dynamic_patch
        body = (
            "# 角色\n\n"
            "## 3.1 说明\n\n"
            "在 `<!-- DYNAMIC_START -->` 和 `<!-- DYNAMIC_END -->` 之间...\n\n"
            "## 7. 运行时补丁\n\n"
            "<!-- DYNAMIC_START -->\n"
            "# Patch [2026-05-06T23:49Z] [KEEP] F2 — 静默 catch\n"
            "真正的约束\n"
            "<!-- DYNAMIC_END -->\n"
        )
        result = _extract_dynamic_patch(body)
        assert "真正的约束" in result
        assert "说明" not in result
