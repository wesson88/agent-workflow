"""
test_artifact_registry.py — 产物注册表 v0.1 骨架

覆盖：
- fail-closed 校验：artifact≠stem / domain 未声明 / 缺 {proj_root} / 非法 format / 重复注册
- 路径解析：{proj_root} 按域展开 + {project} 替换 / 保留占位符
- coverage_report：角色声明命中/未命中对照
- 配置缺失 → ArtifactRegistryError；注册表目录不存在 → 空 dict
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engine import artifact_registry as ar
from engine.artifact_registry import (
    ArtifactRegistryError,
    coverage_report,
    load_registry,
    resolve_artifact_path,
)


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


_CONFIG = """---
proj_roots:
  se: "10-项目/{project}"
  music: "10-项目/music/{project}"
---
"""


def _entry(artifact="PRD", domain="se", tpl="{proj_root}/PRD.md",
           fmt="md", producer="产品经理", extra="") -> str:
    return (
        f"---\nartifact: {artifact}\ndomain: {domain}\n"
        f'path_template: "{tpl}"\nformat: {fmt}\nproducer: {producer}\n{extra}---\n\n正文\n'
    )


@pytest.fixture
def reg_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ar, "VAULT_ROOT", tmp_path)
    monkeypatch.setattr("engine.obsidian_io.VAULT_ROOT", tmp_path)
    ar.invalidate_cache()
    _write(tmp_path / "00-系统" / "产物注册表" / "_config.md", _CONFIG)
    yield tmp_path
    ar.invalidate_cache()


class TestValidation:
    def test_happy_path(self, reg_vault):
        _write(reg_vault / "00-系统" / "产物注册表" / "se" / "PRD.md", _entry())
        reg = load_registry()
        assert "PRD" in reg
        assert reg["PRD"].producer == "产品经理"

    def test_artifact_must_equal_stem(self, reg_vault):
        _write(reg_vault / "00-系统" / "产物注册表" / "se" / "别名.md",
               _entry(artifact="PRD"))
        with pytest.raises(ArtifactRegistryError, match="必须等于笔记 stem"):
            load_registry()

    def test_unknown_domain(self, reg_vault):
        _write(reg_vault / "00-系统" / "产物注册表" / "video" / "分镜.md",
               _entry(artifact="分镜", domain="video", tpl="{proj_root}/分镜.md"))
        with pytest.raises(ArtifactRegistryError, match="未在 _config.proj_roots"):
            load_registry()

    def test_missing_proj_root_placeholder(self, reg_vault):
        _write(reg_vault / "00-系统" / "产物注册表" / "se" / "PRD.md",
               _entry(tpl="10-项目/{project}/PRD.md"))
        with pytest.raises(ArtifactRegistryError, match="proj_root"):
            load_registry()

    def test_bad_format(self, reg_vault):
        _write(reg_vault / "00-系统" / "产物注册表" / "se" / "PRD.md",
               _entry(fmt="pdf"))
        with pytest.raises(ArtifactRegistryError, match="format"):
            load_registry()

    def test_duplicate_artifact(self, reg_vault):
        _write(reg_vault / "00-系统" / "产物注册表" / "se" / "PRD.md", _entry())
        _write(reg_vault / "00-系统" / "产物注册表" / "music" / "PRD.md",
               _entry(domain="music"))
        with pytest.raises(ArtifactRegistryError, match="重复注册"):
            load_registry()

    def test_config_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ar, "VAULT_ROOT", tmp_path)
        ar.invalidate_cache()
        _write(tmp_path / "00-系统" / "产物注册表" / "se" / "PRD.md", _entry())
        with pytest.raises(ArtifactRegistryError, match="配置缺失"):
            load_registry()
        ar.invalidate_cache()

    def test_registry_dir_absent_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ar, "VAULT_ROOT", tmp_path)
        ar.invalidate_cache()
        assert load_registry() == {}
        ar.invalidate_cache()


class TestResolve:
    def test_per_domain_roots(self, reg_vault):
        _write(reg_vault / "00-系统" / "产物注册表" / "se" / "PRD.md", _entry())
        _write(reg_vault / "00-系统" / "产物注册表" / "music" / "词作.md",
               _entry(artifact="词作", domain="music",
                      tpl="{proj_root}/词作.md", producer="作词"))
        assert resolve_artifact_path("PRD", "demo") == "10-项目/demo/PRD.md"
        assert resolve_artifact_path("词作", "湖向") == "10-项目/music/湖向/词作.md"

    def test_keep_project_placeholder(self, reg_vault):
        _write(reg_vault / "00-系统" / "产物注册表" / "se" / "PRD.md", _entry())
        assert resolve_artifact_path("PRD") == "10-项目/{project}/PRD.md"

    def test_unknown_artifact_keyerror(self, reg_vault):
        with pytest.raises(KeyError, match="未注册"):
            resolve_artifact_path("不存在", "demo")


class TestCoverage:
    def test_registered_vs_unregistered(self, reg_vault, monkeypatch):
        _write(reg_vault / "00-系统" / "产物注册表" / "se" / "PRD.md", _entry())

        class FakeRole:
            name = "架构师"
            inputs = ("10-项目/{project}/PRD.md", "00-系统/规则/se/技术栈.md")
            outputs = ("10-项目/{project}/系统设计.md",)

        monkeypatch.setattr("engine.role_loader.list_roles", lambda: [FakeRole()])
        report = coverage_report()
        assert ("架构师", "10-项目/{project}/PRD.md", "PRD") in report["registered"]
        unreg_paths = [p for _, p in report["unregistered"]]
        assert "00-系统/规则/se/技术栈.md" in unreg_paths
        assert "10-项目/{project}/系统设计.md" in unreg_paths
        assert report["artifact_count"] == 1
