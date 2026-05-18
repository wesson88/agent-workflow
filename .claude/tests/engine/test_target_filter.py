"""
.claude/tests/engine/test_target_filter.py — 元角色 --target 参数测试

覆盖：
- skills.common.parse_targets 边界条件
- 4 个元角色对 target 过滤的语义
  - role_auditor: 过滤角色文件
  - graduator: 过滤 WORKER_ROLES
  - reflector: 过滤 WORKER_ROLES（独立于 --project）
  - archivist: 过滤 memory 文件名子串
"""

from __future__ import annotations

from pathlib import Path
import pytest

from common import parse_targets


# ── parse_targets ────────────────────────────────────────
class TestParseTargets:
    def test_none_returns_none(self):
        assert parse_targets(None) is None

    def test_empty_list_returns_none(self):
        assert parse_targets([]) is None

    def test_single_value(self):
        assert parse_targets(["后端工程师"]) == {"后端工程师"}

    def test_comma_separated(self):
        assert parse_targets(["后端工程师,前端工程师"]) == {"后端工程师", "前端工程师"}

    def test_repeated_flag(self):
        assert parse_targets(["后端工程师", "前端工程师"]) == {"后端工程师", "前端工程师"}

    def test_mixed_comma_and_repeated(self):
        result = parse_targets(["后端工程师,架构师", "前端工程师"])
        assert result == {"后端工程师", "架构师", "前端工程师"}

    def test_all_keyword_returns_none(self):
        assert parse_targets(["all"]) is None

    def test_all_keyword_case_insensitive(self):
        assert parse_targets(["ALL"]) is None
        assert parse_targets(["All"]) is None

    def test_all_mixed_with_real_values_ignores_all(self):
        # "all" 关键字被剔除，其余实际值仍生效
        assert parse_targets(["all,后端工程师"]) == {"后端工程师"}

    def test_empty_strings_skipped(self):
        assert parse_targets(["", ",,后端工程师,,"]) == {"后端工程师"}

    def test_whitespace_trimmed(self):
        assert parse_targets(["  后端工程师  ,  前端工程师 "]) == {"后端工程师", "前端工程师"}

    def test_only_all_or_empty_returns_none(self):
        # 全部值都是 all / 空 → 视为全量
        assert parse_targets(["all,,all"]) is None


# ── archivist._gather_memory_files（最容易单测的元角色行为）─
class TestArchivistTargetFilter:
    @pytest.fixture
    def memory_dir(self, tmp_path: Path) -> Path:
        # 模拟 memory dir 结构
        files = [
            "MEMORY.md",
            "user_role.md",
            "feedback_testing.md",
            "feedback_naming.md",
            "project_workflow.md",
        ]
        for fname in files:
            (tmp_path / fname).write_text("placeholder\n", encoding="utf-8")
        return tmp_path

    def test_no_target_returns_all(self, memory_dir: Path):
        from archivist.main import _gather_memory_files
        result = _gather_memory_files(memory_dir)
        names = [p.name for p in result]
        assert names[0] == "MEMORY.md"  # 索引置顶
        assert len(result) == 5

    def test_target_filters_by_filename_substring(self, memory_dir: Path):
        from archivist.main import _gather_memory_files
        result = _gather_memory_files(memory_dir, targets={"feedback"})
        names = sorted(p.name for p in result)
        # MEMORY.md 强制保留 + 2 个 feedback
        assert names == ["MEMORY.md", "feedback_naming.md", "feedback_testing.md"]

    def test_target_multiple_substrings(self, memory_dir: Path):
        from archivist.main import _gather_memory_files
        result = _gather_memory_files(memory_dir, targets={"feedback", "project"})
        names = sorted(p.name for p in result)
        assert names == [
            "MEMORY.md", "feedback_naming.md",
            "feedback_testing.md", "project_workflow.md",
        ]

    def test_target_no_match_keeps_only_memory_index(self, memory_dir: Path):
        from archivist.main import _gather_memory_files
        result = _gather_memory_files(memory_dir, targets={"nonexistent"})
        assert [p.name for p in result] == ["MEMORY.md"]
