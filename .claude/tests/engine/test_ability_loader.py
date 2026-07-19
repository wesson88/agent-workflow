"""
test_ability_loader.py — 能力加载统一层（架构演进第 4 步）

覆盖：
- common re-export 同一性（append_audit 上收同款手法的守卫）
- 域 adapter：命中注入 / domain 空 / 视角文件缺失
- assemble_user_context：机制 hints 齐全 + 块拼接顺序（base → rule → skill → adapter）
- 失败必告警：rule_refs 全 unresolved → audit ability_load_warn 事件
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engine import ability_loader as al


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


class TestReExport:
    def test_common_identity(self):
        from common import (
            load_genre_skill_block,
            load_rule_block,
            load_skill_block,
        )

        assert load_rule_block is al.load_rule_block
        assert load_genre_skill_block is al.load_genre_skill_block
        assert load_skill_block is al.load_skill_block


class TestDomainAdapter:
    def test_hit(self, tmp_path, monkeypatch):
        import engine
        monkeypatch.setattr(engine, "VAULT_ROOT", tmp_path)
        _write(tmp_path / "00-系统" / "规则" / "music" / "复盘者-视角.md",
               "---\nversion: 1\n---\n# 复盘者音乐域视角\nprobational 清单穷举")
        block, hint = al.load_domain_adapter("复盘者", "music")
        assert "复盘者音乐域视角" in block and "域视角适配（music）" in block
        assert "注入" in hint

    def test_no_domain(self):
        assert al.load_domain_adapter("复盘者", None) == ("", "未声明 domain")

    def test_missing_file(self, tmp_path, monkeypatch):
        import engine
        monkeypatch.setattr(engine, "VAULT_ROOT", tmp_path)
        block, hint = al.load_domain_adapter("复盘者", "video")
        assert block == "" and "无域视角文件" in hint


class _FakeRole:
    name = "复盘者"
    domain = "meta"
    rule_refs = ()


class TestAssemble:
    def test_hints_and_order(self, tmp_path, monkeypatch):
        import engine
        monkeypatch.setattr(engine, "VAULT_ROOT", tmp_path)
        _write(tmp_path / "00-系统" / "规则" / "music" / "复盘者-视角.md",
               "---\n---\n视角内容")
        context, hints = al.assemble_user_context(
            _FakeRole(), "任务", "BASE上下文", domain="music",
        )
        assert set(hints) == {"rule_refs", "skill", "domain_adapter"}
        assert context.startswith("BASE上下文")
        assert "视角内容" in context
        assert context.index("BASE上下文") < context.index("视角内容")

    def test_no_domain_no_adapter(self, tmp_path, monkeypatch):
        import engine
        monkeypatch.setattr(engine, "VAULT_ROOT", tmp_path)
        context, hints = al.assemble_user_context(_FakeRole(), "t", "BASE")
        assert hints["domain_adapter"] == "未声明 domain"
        assert context == "BASE"  # rule 空 + skill 目录不存在 + 无 adapter


class TestFailLoud:
    def test_rule_refs_all_unresolved_emits_audit(self, tmp_path, monkeypatch):
        import engine
        from engine import wikilink as wl_mod
        monkeypatch.setattr(engine, "VAULT_ROOT", tmp_path)
        monkeypatch.setattr(wl_mod, "VAULT_ROOT", tmp_path)
        wl_mod.invalidate_cache()
        events: list[dict] = []
        monkeypatch.setattr("engine.audit.append_audit", events.append)
        block, hint = al.load_rule_block(("[[不存在的规则#§1]]",))
        assert block == "" and "全部展开失败" in hint
        assert events and events[0]["type"] == "ability_load_warn"
        assert events[0]["mechanism"] == "rule_refs"
        wl_mod.invalidate_cache()
