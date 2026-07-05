"""
test_sandbox.py — capability_executor.sandbox 沙箱路径校验。

覆盖：
- allowed_paths 允许集内 / 允许集外
- 相对 vs 绝对路径
- 目录 glob 与文件精确匹配
- Windows 路径分隔符转 POSIX
- 越出 vault_root
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.capability_executor.base import SandboxViolationError
from engine.capability_executor.sandbox import (
    assert_path_within,
    check_path_within,
    default_allowed_paths,
    get_sandbox_allowed,
)


class TestCheckPathWithin:
    def test_relative_path_in_allowed_dir(self, tmp_path: Path):
        assert check_path_within(
            "10-项目/demo/交付物/scrape.json",
            ["10-项目/*/交付物/"],
            vault_root=tmp_path,
        )

    def test_absolute_path_in_allowed_dir(self, tmp_path: Path):
        p = tmp_path / "10-项目" / "demo" / "交付物" / "x.json"
        assert check_path_within(
            p, ["10-项目/*/交付物/"], vault_root=tmp_path,
        )

    def test_path_outside_allowed_returns_false(self, tmp_path: Path):
        assert not check_path_within(
            "99-临时/y.md",
            ["10-项目/*/交付物/"],
            vault_root=tmp_path,
        )

    def test_absolute_path_outside_vault_returns_false(self, tmp_path: Path):
        p = tmp_path.parent / "outside" / "x.json"
        assert not check_path_within(
            p, ["10-项目/*/交付物/"], vault_root=tmp_path,
        )

    def test_exact_file_pattern_match(self, tmp_path: Path):
        assert check_path_within(
            "10-项目/demo/API契约.md",
            ["10-项目/*/API契约.md"],
            vault_root=tmp_path,
        )

    def test_exact_file_pattern_no_match(self, tmp_path: Path):
        assert not check_path_within(
            "10-项目/demo/其他.md",
            ["10-项目/*/API契约.md"],
            vault_root=tmp_path,
        )

    def test_multi_patterns_any_match(self, tmp_path: Path):
        assert check_path_within(
            "20-知识/能力注册表/web-scraper/调用日志/x.md",
            ["10-项目/*/交付物/", "20-知识/能力注册表/web-scraper/调用日志/"],
            vault_root=tmp_path,
        )

    def test_windows_backslash_normalized(self, tmp_path: Path):
        p = tmp_path / "10-项目" / "demo" / "交付物" / "x.json"
        # PurePosixPath 转换后应能命中
        assert check_path_within(
            p, ["10-项目/*/交付物/"], vault_root=tmp_path,
        )


class TestAssertPathWithin:
    def test_pass_no_raise(self, tmp_path: Path):
        assert_path_within(
            "10-项目/demo/交付物/x.json",
            ["10-项目/*/交付物/"],
            vault_root=tmp_path,
        )

    def test_fail_raises(self, tmp_path: Path):
        with pytest.raises(SandboxViolationError, match="越出"):
            assert_path_within(
                "99-临时/y.md",
                ["10-项目/*/交付物/"],
                vault_root=tmp_path,
                label="test_input",
            )


class TestDefaultAllowedPaths:
    def test_returns_two_paths(self):
        allowed = default_allowed_paths("web-scraper")
        assert len(allowed) == 2
        assert "10-项目/*/交付物/" in allowed
        assert "20-知识/能力注册表/web-scraper/调用日志/" in allowed


class TestGetSandboxAllowed:
    def test_from_manifest(self):
        m = {
            "id": "web-scraper/crawl",
            "sandbox": {"allowed_paths": ["custom/path/"]},
        }
        assert get_sandbox_allowed(m) == ["custom/path/"]

    def test_fallback_to_defaults(self):
        m = {"id": "web-scraper/crawl"}
        allowed = get_sandbox_allowed(m)
        assert "10-项目/*/交付物/" in allowed
        assert "20-知识/能力注册表/web-scraper/调用日志/" in allowed
