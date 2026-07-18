"""
test_ingest_check.py — 第三方 skill 入库 stem 冲突预检

覆盖：
- 候选 stem 与已有文件冲突 → 返回冲突路径 / CLI exit 2
- 无冲突 → 空列表 / CLI exit 0
- 候选接受裸 stem / 文件名 / 完整路径三种形态
- --scan 存量扫描：有碰撞列出、无碰撞 exit 0
- 预检视角与解析器一致：10-项目/ 下的同名文件不构成冲突（stem 索引排除）
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def ingest_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from engine import wikilink as wl_mod

    skill_dir = tmp_path / "20-知识" / "角色技能" / "se" / "UI设计师"
    skill_dir.mkdir(parents=True)
    (skill_dir / "ui_DesignTaste.md").write_text("# x", encoding="utf-8")

    proj_dir = tmp_path / "10-项目" / "demo"
    proj_dir.mkdir(parents=True)
    (proj_dir / "PRD.md").write_text("# prd", encoding="utf-8")

    monkeypatch.setattr(wl_mod, "VAULT_ROOT", tmp_path)
    wl_mod.invalidate_cache()
    yield tmp_path
    wl_mod.invalidate_cache()


class TestCheckStemConflict:
    def test_conflict_detected(self, ingest_vault):
        from engine.ingest_check import check_stem_conflict
        conflicts = check_stem_conflict("ui_DesignTaste.md")
        assert len(conflicts) == 1
        assert conflicts[0].stem == "ui_DesignTaste"

    def test_no_conflict(self, ingest_vault):
        from engine.ingest_check import check_stem_conflict
        assert check_stem_conflict("ui_BrandKit_v2") == []

    def test_accepts_bare_stem_and_full_path(self, ingest_vault):
        from engine.ingest_check import check_stem_conflict
        assert check_stem_conflict("ui_DesignTaste")  # 裸 stem
        assert check_stem_conflict(r"D:\downloads\ui_DesignTaste.md")  # 外部路径

    def test_project_dir_excluded_like_resolver(self, ingest_vault):
        """10-项目/ 不进 stem 索引 → 与之同名不构成冲突（与解析器视角一致）。"""
        from engine.ingest_check import check_stem_conflict
        assert check_stem_conflict("PRD.md") == []

    def test_empty_candidate_raises(self, ingest_vault):
        from engine.ingest_check import check_stem_conflict
        with pytest.raises(ValueError):
            check_stem_conflict("")


class TestScanAndCli:
    def test_scan_finds_collision(self, ingest_vault):
        from engine.ingest_check import scan_vault_collisions
        # 造一个碰撞：另一目录放同 stem 文件
        other = ingest_vault / "20-知识" / "角色技能" / "se" / "前端工程师"
        other.mkdir(parents=True)
        (other / "ui_DesignTaste.md").write_text("# y", encoding="utf-8")

        collisions = scan_vault_collisions()
        assert "ui_DesignTaste" in collisions
        assert len(collisions["ui_DesignTaste"]) == 2

    def test_scan_clean(self, ingest_vault):
        from engine.ingest_check import scan_vault_collisions
        assert scan_vault_collisions() == {}

    def test_cli_conflict_exit_2(self, ingest_vault, capsys):
        from engine.ingest_check import main
        rc = main(["--candidate", "ui_DesignTaste.md"])
        assert rc == 2
        assert "冲突" in capsys.readouterr().err

    def test_cli_ok_exit_0(self, ingest_vault, capsys):
        from engine.ingest_check import main
        rc = main(["--candidate", "brand_new_skill"])
        assert rc == 0
        assert "无冲突" in capsys.readouterr().out

    def test_cli_scan_exit_codes(self, ingest_vault):
        from engine.ingest_check import main
        assert main(["--scan"]) == 0
        other = ingest_vault / "20-知识" / "规则备份"
        other.mkdir(parents=True)
        (other / "ui_DesignTaste.md").write_text("# y", encoding="utf-8")
        assert main(["--scan"]) == 2
