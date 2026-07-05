"""
test_capability_summary_render.py — common._render_capability_summary 输出格式。

覆盖：
- 无 capability_refs → 空串
- capability_refs 里的 wikilink 解析成 root（`[[huashu-design/manifest]]` → `huashu-design`）
- manifest 加载失败 → 静默跳过（不抛）
- 输出含 id / version / runtime.type / triggers / invoke CLI
- 每段 ≤ 400 chars（依据：规范 §5.2 关键不变量 + 调用行）
- 集成到 build_system_prompt 时被塞进 static part
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


_SKILLS_DIR = Path(__file__).resolve().parent.parent.parent / "skills"
sys.path.insert(0, str(_SKILLS_DIR))


def _make_role(capability_refs: tuple[str, ...] = ()):
    class FakeRole:
        pass
    r = FakeRole()
    r.capability_refs = capability_refs
    return r


@pytest.fixture
def tmp_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from engine import config as engine_config
    from engine.capability_executor import manifest_loader as ml
    monkeypatch.setattr(engine_config, "VAULT_ROOT", tmp_path)
    monkeypatch.setattr(ml, "VAULT_ROOT", tmp_path)
    return tmp_path


def _write_manifest(vault: Path, root: str, manifest: dict) -> None:
    p = vault / "20-知识" / "能力注册表" / root / "manifest.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")


_VALID = {
    "id": "huashu-design/render-prototype",
    "version": "1.0.0",
    "source": "test",
    "runtime": {"type": "node", "entry": "render.js"},
    "triggers": ["交互原型", "App demo", "产品演示"],
    "inputs": [{"name": "brief", "type": "text", "required": True}],
    "outputs": [
        {"name": "html", "type": "file",
         "path_pattern": "10-项目/{project}/交付物/x.html"},
    ],
    "audit": {"log_to": "20-知识/能力注册表/huashu-design/调用日志/{ts}-{project}.md"},
}


class TestRenderCapabilitySummary:
    def test_empty_capability_refs_returns_empty(self, tmp_vault):
        from common import _render_capability_summary
        r = _make_role(())
        assert _render_capability_summary(r) == ""

    def test_valid_capability_ref_renders_summary(self, tmp_vault):
        _write_manifest(tmp_vault, "huashu-design", _VALID)
        from common import _render_capability_summary
        r = _make_role(("[[huashu-design/manifest]]",))
        out = _render_capability_summary(r, project="demo")
        assert "huashu-design/render-prototype" in out
        assert "v1.0.0" in out
        assert "runtime=node" in out
        assert "交互原型" in out
        assert "python -m engine.capability_executor.invoke" in out
        assert "--project demo" in out

    def test_missing_manifest_silently_skipped(self, tmp_vault):
        # 不写 manifest → load 失败但不抛
        from common import _render_capability_summary
        r = _make_role(("[[nonexistent/manifest]]",))
        assert _render_capability_summary(r) == ""

    def test_multiple_refs_renders_all_valid(self, tmp_vault):
        _write_manifest(tmp_vault, "huashu-design", _VALID)
        web_scraper = dict(_VALID)
        web_scraper["id"] = "web-scraper/crawl"
        web_scraper["runtime"] = {"type": "python", "entry": "scrape.py"}
        web_scraper["triggers"] = ["数据采集"]
        _write_manifest(tmp_vault, "web-scraper", web_scraper)
        from common import _render_capability_summary
        r = _make_role((
            "[[huashu-design/manifest]]",
            "[[web-scraper/manifest]]",
        ))
        out = _render_capability_summary(r, project="demo")
        assert "huashu-design/render-prototype" in out
        assert "web-scraper/crawl" in out

    def test_summary_length_bounded(self, tmp_vault):
        # 极端 triggers 触发字符串上限
        m = dict(_VALID)
        m["triggers"] = ["超长触发词" * 30] * 10
        _write_manifest(tmp_vault, "huashu-design", m)
        from common import _render_capability_summary
        r = _make_role(("[[huashu-design/manifest]]",))
        out = _render_capability_summary(r, project="demo")
        # 单段截断在 400 chars（含 "…" 后缀 → 断言 ≤ 410）
        # 找到 `- **huashu-design` 段：
        section = out.split("- **")[1] if "- **" in out else ""
        assert len(section) <= 410, (
            f"每段应 ≤ 400 chars（含调用行）；实际 {len(section)}"
        )

    def test_project_defaults_to_placeholder(self, tmp_vault):
        _write_manifest(tmp_vault, "huashu-design", _VALID)
        from common import _render_capability_summary
        r = _make_role(("[[huashu-design/manifest]]",))
        out = _render_capability_summary(r)
        assert "--project {project}" in out


class TestBuildSystemPromptIntegration:
    """build_system_prompt 集成：有 capability_refs 时 static 段含摘要。"""

    def test_static_prompt_contains_capability_summary(
        self, tmp_vault, monkeypatch
    ):
        _write_manifest(tmp_vault, "huashu-design", _VALID)
        # 真实前端工程师 role 走 vault 加载会依赖 role md 目录结构；这里只测集成路径
        # 已由 test_role_loader_capability_refs 覆盖字段加载，此处只测集成注入
        from engine.role_loader import Role
        role = Role(
            name="fake", aliases=(), note_path=tmp_vault / "x.md",
            domain="se", skills=(), style="", model="c",
            max_tokens=1024, tools=(), version="0.0.0",
            upstream=(), downstream=(), monitors=(),
            inputs=(), outputs=(),
            skill_refs=(), rule_refs=(), body="# fake\n\n## 1. x\n",
            frontmatter={}, capability_refs=("[[huashu-design/manifest]]",),
        )
        # monkeypatch load_role 返回 fake role
        from common import build_system_prompt
        import common as common_mod
        monkeypatch.setattr(common_mod, "load_role", lambda *a, **kw: role)
        # 也 monkeypatch _extract_role_prompt_sections 避免 domain='se' 走白名单校验
        monkeypatch.setattr(
            common_mod, "_extract_role_prompt_sections",
            lambda body, domain: ("# fake\n\n## 1. x\n", "meta_full"),
        )
        static, _ = build_system_prompt("fake", project="demo")
        assert "可调用能力" in static
        assert "huashu-design/render-prototype" in static
