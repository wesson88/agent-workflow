"""
test_b1_b2_b5_b6.py — B1/B2/B5/B6 P10.5 优化补测（B3/B4 已单独测过）。

- B1: build_system_prompt_ex 3-tuple + call_llm 3-tuple 支持
- B2: read_input_files 进程内 cache（同文件重复读命中）
- B5: sandbox.check_path_within lru_cache（同 rel + patterns 命中）
- B6: 已单独测过（test_engine_audit.py）
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SKILLS_DIR = Path(__file__).resolve().parent.parent.parent / "skills"
sys.path.insert(0, str(_SKILLS_DIR))


# ── B1 build_system_prompt_ex + call_llm 3-tuple ───────────────
class TestB1SystemPromptTuple3:
    def test_build_system_prompt_returns_2tuple_backward_compat(self, tmp_path, monkeypatch):
        """build_system_prompt 保持向后兼容：返回 2-tuple。"""
        from engine import config as engine_config
        monkeypatch.setattr(engine_config, "VAULT_ROOT", tmp_path)
        # 使用 mock role 避免 vault 依赖
        from common import build_system_prompt, build_system_prompt_ex
        import common as common_mod

        from engine.role_loader import Role
        role = Role(
            name="fake", aliases=(), note_path=tmp_path / "x.md",
            domain="se", skills=(), style="", model="c",
            max_tokens=1024, tools=(), version="0.0.0",
            upstream=(), downstream=(), monitors=(),
            inputs=(), outputs=(),
            skill_refs=(), rule_refs=(), body="# fake\n\n## 1. x\n",
            frontmatter={}, capability_refs=(),
        )
        monkeypatch.setattr(common_mod, "load_role", lambda *a, **kw: role)
        monkeypatch.setattr(
            common_mod, "_extract_role_prompt_sections",
            lambda body, domain: ("core", "meta_full"),
        )

        result = build_system_prompt("fake")
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_build_system_prompt_ex_returns_3tuple(self, tmp_path, monkeypatch):
        """B1 新增 build_system_prompt_ex 返回 3-tuple。"""
        from engine import config as engine_config
        monkeypatch.setattr(engine_config, "VAULT_ROOT", tmp_path)
        from common import build_system_prompt_ex
        import common as common_mod

        from engine.role_loader import Role
        role = Role(
            name="fake", aliases=(), note_path=tmp_path / "x.md",
            domain="se", skills=(), style="", model="c",
            max_tokens=1024, tools=(), version="0.0.0",
            upstream=(), downstream=(), monitors=(),
            inputs=(), outputs=(),
            skill_refs=(), rule_refs=(), body="# fake\n\n## 1. x\n",
            frontmatter={}, capability_refs=(),
        )
        monkeypatch.setattr(common_mod, "load_role", lambda *a, **kw: role)
        monkeypatch.setattr(
            common_mod, "_extract_role_prompt_sections",
            lambda body, domain: ("core", "meta_full"),
        )

        result = build_system_prompt_ex("fake")
        assert isinstance(result, tuple)
        assert len(result) == 3
        static, dynamic_own, dynamic_upstream = result
        assert isinstance(static, str)
        assert isinstance(dynamic_own, str)
        assert isinstance(dynamic_upstream, str)


class TestB1CallLlmTupleDispatch:
    """call_llm 支持 str / 2-tuple / 3-tuple 三种入参。"""

    def test_tuple_length_2_normalized_to_upstream(self, monkeypatch):
        """2-tuple 归一：dynamic 段视为 upstream（老 API 语义保持）。"""
        from engine import llm as llm_mod
        captured: dict = {}

        # mock _audit_token_budget + get_provider + track，让 call_llm 不真调 LLM
        def _fake_audit(model, static, dynamic, user, **kw):
            captured["static"] = static
            captured["dynamic_combined"] = dynamic

        monkeypatch.setattr(llm_mod, "_audit_token_budget", _fake_audit)
        monkeypatch.setattr(
            llm_mod, "get_provider",
            lambda m: {"api": {"kind": "openai_compat", "key_env": "X"}, "mode": "api-only"},
        )
        monkeypatch.setattr(llm_mod, "_resolve_track", lambda cfg: "api")
        monkeypatch.setattr(
            llm_mod, "_call_openai_compat",
            lambda cfg, prompt, user, mt, ps, **kw: "fake",
        )

        llm_mod.call_llm(("static-content", "dynamic-content"), "user", model="fake")
        assert captured["static"] == "static-content"
        assert captured["dynamic_combined"] == "dynamic-content"

    def test_tuple_length_3(self, monkeypatch):
        """3-tuple：dynamic_own 和 dynamic_upstream 各自分派。"""
        from engine import llm as llm_mod
        captured: dict = {}

        def _fake_audit(model, static, dynamic, user, **kw):
            captured["static"] = static
            captured["dynamic_combined"] = dynamic

        def _fake_anthropic(cfg, s, do, du, u, mt, ps, **kwargs):
            captured["dynamic_own"] = do
            captured["dynamic_upstream"] = du
            return "fake"

        monkeypatch.setattr(llm_mod, "_audit_token_budget", _fake_audit)
        monkeypatch.setattr(
            llm_mod, "get_provider",
            lambda m: {"api": {"kind": "anthropic", "key_env": "X"}, "mode": "api-only"},
        )
        monkeypatch.setattr(llm_mod, "_resolve_track", lambda cfg: "api")
        monkeypatch.setattr(llm_mod, "_call_anthropic_sdk", _fake_anthropic)

        llm_mod.call_llm(
            ("static-content", "own-content", "upstream-content"),
            "user",
            model="fake",
        )
        assert captured["dynamic_own"] == "own-content"
        assert captured["dynamic_upstream"] == "upstream-content"

    def test_invalid_tuple_length_raises(self, monkeypatch):
        from engine import llm as llm_mod
        monkeypatch.setattr(
            llm_mod, "get_provider",
            lambda m: {"api": {"kind": "anthropic", "key_env": "X"}, "mode": "api-only"},
        )
        monkeypatch.setattr(llm_mod, "_resolve_track", lambda cfg: "api")

        with pytest.raises(ValueError, match="必须为 2 或 3"):
            llm_mod.call_llm(("a", "b", "c", "d"), "user", model="fake")


# ── B2 read_input_files 进程内缓存 ─────────────────────────────
class TestB2ReadInputCache:
    def test_second_read_hits_cache(self, tmp_path):
        from common import _read_file_cached, invalidate_file_cache
        invalidate_file_cache()

        f = tmp_path / "input.md"
        f.write_text("first content", encoding="utf-8")

        c1 = _read_file_cached(f)
        assert c1 == "first content"

        # 磁盘改内容但 mtime 不变（不改文件）→ cache 命中
        c2 = _read_file_cached(f)
        assert c2 == "first content"

    def test_mtime_change_invalidates_cache(self, tmp_path):
        import os
        import time
        from common import _read_file_cached, invalidate_file_cache
        invalidate_file_cache()

        f = tmp_path / "input.md"
        f.write_text("v1", encoding="utf-8")
        assert _read_file_cached(f) == "v1"

        time.sleep(0.01)
        f.write_text("v2", encoding="utf-8")
        # 强制更新 mtime
        os.utime(f, None)
        assert _read_file_cached(f) == "v2"

    def test_missing_file_returns_placeholder(self, tmp_path):
        from common import _read_file_cached
        result = _read_file_cached(tmp_path / "nonexistent.md")
        assert "不存在" in result

    def test_invalidate_clears_all(self, tmp_path):
        from common import _read_file_cached, invalidate_file_cache
        invalidate_file_cache()

        f = tmp_path / "input.md"
        f.write_text("v1", encoding="utf-8")
        assert _read_file_cached(f) == "v1"

        # 改内容但 mtime 不动 → 通常不命中，靠 invalidate
        f.write_text("v2", encoding="utf-8")
        # 显式清 cache
        invalidate_file_cache()
        assert _read_file_cached(f) == "v2"


# ── B5 sandbox check_path_within lru_cache ─────────────────
class TestB5SandboxCache:
    def test_repeated_check_same_args(self, tmp_path):
        from engine.capability_executor.sandbox import (
            check_path_within, invalidate_cache,
        )
        invalidate_cache()

        allowed = ["10-项目/*/交付物/"]
        # 同 rel + 同 patterns 多次调用应无副作用
        for _ in range(50):
            assert check_path_within(
                "10-项目/demo/交付物/x.json", allowed, vault_root=tmp_path,
            )

    def test_cache_info_hits_grow(self, tmp_path):
        from engine.capability_executor.sandbox import (
            _match_rel_against_patterns, check_path_within, invalidate_cache,
        )
        invalidate_cache()

        allowed = ["10-项目/*/交付物/"]
        check_path_within("10-项目/demo/交付物/x.json", allowed, vault_root=tmp_path)
        info1 = _match_rel_against_patterns.cache_info()
        check_path_within("10-项目/demo/交付物/x.json", allowed, vault_root=tmp_path)
        info2 = _match_rel_against_patterns.cache_info()
        assert info2.hits > info1.hits  # 二次调用命中 cache

    def test_invalidate_resets_cache(self, tmp_path):
        from engine.capability_executor.sandbox import (
            _match_rel_against_patterns, check_path_within, invalidate_cache,
        )
        check_path_within("10-项目/x/交付物/a.md", ["10-项目/*/交付物/"], vault_root=tmp_path)
        invalidate_cache()
        info = _match_rel_against_patterns.cache_info()
        assert info.currsize == 0
