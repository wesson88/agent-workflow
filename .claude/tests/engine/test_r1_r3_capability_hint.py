"""
test_r1_r3_capability_hint.py — R1（措辞加强）+ R3（task 命中 → user_prompt hint）。

M4 实战驱动（2026-07-08）：
- R1：_render_capability_summary_cached header 从"按需 invoke"改成"优先 invoke，
  不 invoke 需 justify"
- R3：analyze_task_for_capability_hint(role, task_text, project) —— task 文本命中
  role.capability_refs 里 manifest.triggers 时返回 hint 段供 user_prompt 尾部 append

覆盖：
- R1 header 新措辞含关键短语；老措辞已彻底移除
- R3 无 capability_refs / 空 task_text / 无命中 → 返回 ""
- R3 单 trigger 命中 → hint 含 capability id + 匹配词 + invoke 命令
- R3 多 trigger 命中 → hint 列出全部
- R3 无效 manifest 静默跳过（不阻断有效的）
- R3 project 参数正确渲染进 invoke 命令
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
    # 清 lru_cache 免影响其他测试
    from common import invalidate_capability_summary_cache
    invalidate_capability_summary_cache()
    ml.invalidate_cache()
    return tmp_path


def _write_manifest(vault: Path, root: str, manifest: dict) -> None:
    p = vault / "20-知识" / "能力注册表" / root / "manifest.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")


def _manifest(id_: str, triggers: list[str]) -> dict:
    return {
        "id": id_,
        "version": "1.0.0",
        "source": "test",
        "runtime": {"type": "node", "entry": "x.js"},
        "triggers": triggers,
        "inputs": [{"name": "brief", "type": "text", "required": True}],
        "outputs": [
            {"name": "html", "type": "file",
             "path_pattern": "10-项目/{project}/交付物/x.html"},
        ],
        "audit": {"log_to": "20-知识/能力注册表/huashu-design/调用日志/{ts}-{project}.md"},
    }


# ── R1 措辞加强 ─────────────────────────────────────────────
class TestR1WordingStrengthened:
    def test_summary_header_contains_strong_wording(self, tmp_vault):
        _write_manifest(
            tmp_vault, "huashu-design",
            _manifest("huashu-design/render-prototype", ["交互原型", "HTML 原型"]),
        )
        from common import _render_capability_summary_cached
        out = _render_capability_summary_cached(("[[huashu-design/manifest]]",), "demo")
        # R1 新措辞的关键短语
        assert "优先 invoke" in out
        assert "请在你的输出里" in out and "明确说明理由" in out
        assert "工程实现" in out  # 强调稳定/可复用

    def test_summary_header_no_soft_wording(self, tmp_vault):
        _write_manifest(
            tmp_vault, "huashu-design",
            _manifest("huashu-design/render-prototype", ["交互原型"]),
        )
        from common import _render_capability_summary_cached
        out = _render_capability_summary_cached(("[[huashu-design/manifest]]",), "demo")
        # R1 老措辞的软标记应该已经完全移除
        assert "你可按需 invoke" not in out
        assert "不是总要调" not in out


# ── R3 task 命中 → hint 段 ─────────────────────────────────
class TestR3TaskCapabilityHint:
    def test_no_capability_refs_returns_empty(self, tmp_vault):
        from common import analyze_task_for_capability_hint
        role = _make_role(capability_refs=())
        assert analyze_task_for_capability_hint(role, "任何任务") == ""

    def test_empty_task_text_returns_empty(self, tmp_vault):
        _write_manifest(
            tmp_vault, "huashu-design",
            _manifest("huashu-design/render-prototype", ["交互原型"]),
        )
        from common import analyze_task_for_capability_hint
        role = _make_role(capability_refs=("[[huashu-design/manifest]]",))
        assert analyze_task_for_capability_hint(role, "") == ""

    def test_no_trigger_match_returns_empty(self, tmp_vault):
        _write_manifest(
            tmp_vault, "huashu-design",
            _manifest("huashu-design/render-prototype", ["交互原型", "App demo"]),
        )
        from common import analyze_task_for_capability_hint
        role = _make_role(capability_refs=("[[huashu-design/manifest]]",))
        task_text = "写一个 CRUD API 处理用户注册和登录"
        assert analyze_task_for_capability_hint(role, task_text) == ""

    def test_single_trigger_match_returns_hint(self, tmp_vault):
        _write_manifest(
            tmp_vault, "huashu-design",
            _manifest(
                "huashu-design/render-prototype",
                ["交互原型", "App demo", "HTML 原型"],
            ),
        )
        from common import analyze_task_for_capability_hint
        role = _make_role(capability_refs=("[[huashu-design/manifest]]",))
        task_text = "本任务：产出一份 HTML 原型 供演示"
        hint = analyze_task_for_capability_hint(role, task_text, project="huashu-demo")
        assert "能力匹配提示" in hint
        assert "huashu-design/render-prototype" in hint
        assert "HTML 原型" in hint  # 命中的触发词
        assert "engine.capability_executor.invoke" in hint  # invoke 命令
        assert "huashu-demo" in hint  # project 已渲染

    def test_multi_trigger_match_lists_all(self, tmp_vault):
        _write_manifest(
            tmp_vault, "huashu-design",
            _manifest(
                "huashu-design/render-prototype",
                ["交互原型", "HTML 原型", "App demo"],
            ),
        )
        from common import analyze_task_for_capability_hint
        role = _make_role(capability_refs=("[[huashu-design/manifest]]",))
        task_text = "做一个交互原型：iOS App demo，HTML 原型 优先"
        hint = analyze_task_for_capability_hint(role, task_text, project="p")
        # 3 个触发词都应命中
        assert "交互原型" in hint
        assert "HTML 原型" in hint
        assert "App demo" in hint

    def test_invalid_manifest_skipped_silently(self, tmp_vault):
        # 一个有效 + 一个无效
        _write_manifest(
            tmp_vault, "huashu-design",
            _manifest("huashu-design/render-prototype", ["交互原型"]),
        )
        _write_manifest(
            tmp_vault, "broken-cap",
            {"id": "broken-cap/x", "version": "bad"},  # 缺必填字段
        )
        from common import analyze_task_for_capability_hint
        role = _make_role(capability_refs=(
            "[[huashu-design/manifest]]",
            "[[broken-cap/manifest]]",
        ))
        hint = analyze_task_for_capability_hint(role, "本任务：做一个交互原型")
        # 有效那个正常出现，无效那个静默跳过
        assert "huashu-design/render-prototype" in hint
        assert "broken-cap" not in hint

    def test_justify_wording_present(self, tmp_vault):
        """hint 结尾必须含"若不 invoke 请说明理由"提示，与 R1 语气统一。"""
        _write_manifest(
            tmp_vault, "huashu-design",
            _manifest("huashu-design/render-prototype", ["交互原型"]),
        )
        from common import analyze_task_for_capability_hint
        role = _make_role(capability_refs=("[[huashu-design/manifest]]",))
        hint = analyze_task_for_capability_hint(role, "做交互原型")
        assert "若你决定不 invoke" in hint
        assert "一句话说明理由" in hint

    def test_default_project_placeholder(self, tmp_vault):
        """未传 project → 保留 {project} 占位符（跟 _render_capability_summary 一致）。"""
        _write_manifest(
            tmp_vault, "huashu-design",
            _manifest("huashu-design/render-prototype", ["交互原型"]),
        )
        from common import analyze_task_for_capability_hint
        role = _make_role(capability_refs=("[[huashu-design/manifest]]",))
        hint = analyze_task_for_capability_hint(role, "做交互原型")
        assert "{project}" in hint
