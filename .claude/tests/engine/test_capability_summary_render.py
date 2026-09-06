"""
test_capability_summary_render.py — common._render_capability_summary 输出格式。

覆盖：
- 无 capability_refs → 空串
- capability_refs 里的 wikilink 解析成 root（`[[huashu-design/manifest]]` → `huashu-design`）
- manifest 加载失败 → 静默跳过（不抛）
- 输出含 id / version / runtime.type / triggers / inputs / 调用方式
- 变长段（triggers / inputs）逐段截断，固定的 CALL 模板永不被切
- executor 未实现的 runtime 标「无法调用」而不是给调用样例
- 集成到 build_system_prompt 时被塞进 static part

2026-09-06（3c）：调用方式从 `python -m engine.capability_executor.invoke`
CLI 改成 CALL 块。原因是角色两条轨都执行不了命令（api 轨无 tool use，cli 轨
`--disallowed-tools` 封了 Bash），那条 CLI 从来没有、也不可能被角色执行。
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

    # 两层 lru_cache 必须逐个用例清（2026-09-06 发现）：
    # `_render_capability_summary_cached` 的 key 是 `(refs_tuple, proj_hint)`，
    # 本文件多数用例的 refs 和 project 完全一样，只有 manifest 内容不同 ——
    # 不清 cache 的话，后面的用例拿的是**第一个用例**的渲染结果，断言在别人的
    # 输出上跑。原 `test_summary_length_bounded` 就是这么"通过"的：它想验证
    # 超长 triggers 被截断，实际拿到的是一份 triggers 很短的缓存。
    import common as common_mod
    common_mod.invalidate_capability_summary_cache()
    ml.invalidate_cache()
    yield tmp_path
    common_mod.invalidate_capability_summary_cache()
    ml.invalidate_cache()


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
    "inputs": [
        {"name": "brief", "type": "text", "required": True},
        # 选填 + 带 {project} 默认值：产出路径这类输入角色猜不出来，
        # 摘要必须把默认值原样给出，否则它只能编一个（多半跳出沙箱被拒）
        {"name": "artifact_path", "type": "text", "required": False,
         "default": "10-项目/{project}/交付物/prototype.html"},
    ],
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
        # 调用方式 = CALL 块模板，且必须完整（起始 + 闭合都在）
        assert "<!-- CALL: huashu-design/render-prototype -->" in out
        assert "<!-- /CALL -->" in out
        # inputs 清单：必填无标记、选填带 `?`、默认值里的 {project} 已解析
        assert "brief:text" in out
        assert "artifact_path?:text" in out
        assert "10-项目/demo/交付物/prototype.html" in out

    def test_unimplemented_runtime_marked_uncallable(self, tmp_vault):
        """runtime 在规范枚举内但没 executor（mcp/http）→ 明说无法调用。

        以前这类能力和已实现的共用一句「已注册且工程实现完成 / 请优先 invoke」，
        等于让角色去调一个必然抛 RuntimeMismatchError 的东西 —— 它执行不了，
        最省事的回应就是在产物里写一句"已调用"。
        """
        m = dict(_VALID)
        m["id"] = "bgm-mcp/pipeline"
        m["runtime"] = {"type": "mcp", "entry": "bgm_full_pipeline({{brief}})"}
        _write_manifest(tmp_vault, "bgm-mcp", m)
        from common import _render_capability_summary
        out = _render_capability_summary(
            _make_role(("[[bgm-mcp/manifest]]",)), project="demo",
        )
        assert "无法调用" in out
        assert "<!-- CALL: bgm-mcp/pipeline -->" not in out

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

    def test_long_triggers_truncated_but_call_template_intact(self, tmp_vault):
        """截断必须逐段做，不能整段截。

        整段截断会把末尾的 `<!-- /CALL -->` 切掉，等于给角色一个语法不全的
        样例照着抄 —— 它照抄出来的块解析不出来，而 CALL 块解析失败是
        「声明了 N 个只恢复出 M 个」的 stderr 告警，不是硬失败。
        """
        m = dict(_VALID)
        m["triggers"] = ["超长触发词" * 30] * 10
        _write_manifest(tmp_vault, "huashu-design", m)
        from common import _render_capability_summary
        out = _render_capability_summary(
            _make_role(("[[huashu-design/manifest]]",)), project="demo",
        )
        trig_line = next(ln for ln in out.splitlines() if "triggers:" in ln)
        assert trig_line.endswith("…") and len(trig_line) <= 140
        assert "<!-- CALL: huashu-design/render-prototype -->" in out
        assert "<!-- /CALL -->" in out

    def test_long_inputs_truncated_but_call_template_intact(self, tmp_vault):
        m = dict(_VALID)
        m["inputs"] = [
            {"name": f"输入项{i}" * 5, "type": "text", "required": True}
            for i in range(10)
        ]
        _write_manifest(tmp_vault, "huashu-design", m)
        from common import _render_capability_summary
        out = _render_capability_summary(
            _make_role(("[[huashu-design/manifest]]",)), project="demo",
        )
        in_line = next(ln for ln in out.splitlines() if "inputs:" in ln)
        assert "…" in in_line
        assert "<!-- /CALL -->" in out

    def test_project_defaults_to_placeholder(self, tmp_vault):
        _write_manifest(tmp_vault, "huashu-design", _VALID)
        from common import _render_capability_summary
        r = _make_role(("[[huashu-design/manifest]]",))
        out = _render_capability_summary(r)
        assert "10-项目/{project}/交付物/prototype.html" in out


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
            rule_refs=(), body="# fake\n\n## 1. x\n",
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
