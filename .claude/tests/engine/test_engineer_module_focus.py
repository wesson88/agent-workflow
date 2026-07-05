"""
test_engineer_module_focus.py — P8.6 resolve_module_id + render_module_focus_hint 单元测试

覆盖：
- resolve_module_id：env 有 / env 空 / args.--module-id / 优先级
- render_module_focus_hint：None → 空串；非空 → 含模块 ID + 项目名 + 进度/测试报告约定
- 集成：Backend/Frontend §6.3 已含"模块化模式"子章节（vault 侧）
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pytest

# 确保 skills/common 可 import
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "skills"))


def _import_common():
    import common
    return common


# ── resolve_module_id ───────────────────────────────────────
class TestResolveModuleId:
    def setup_method(self):
        os.environ.pop("AGENT_SELECTED_MODULE_ID", None)

    def teardown_method(self):
        os.environ.pop("AGENT_SELECTED_MODULE_ID", None)

    def test_none_when_no_source(self):
        common = _import_common()
        assert common.resolve_module_id(None) is None

    def test_env_returns_value(self):
        os.environ["AGENT_SELECTED_MODULE_ID"] = "T01"
        common = _import_common()
        assert common.resolve_module_id(None) == "T01"

    def test_env_whitespace_stripped(self):
        os.environ["AGENT_SELECTED_MODULE_ID"] = "  T05  "
        common = _import_common()
        assert common.resolve_module_id(None) == "T05"

    def test_env_empty_returns_none(self):
        os.environ["AGENT_SELECTED_MODULE_ID"] = ""
        common = _import_common()
        assert common.resolve_module_id(None) is None

    def test_args_module_id_takes_priority(self):
        os.environ["AGENT_SELECTED_MODULE_ID"] = "T99"
        args = argparse.Namespace(module_id="T01")
        common = _import_common()
        assert common.resolve_module_id(args) == "T01"

    def test_args_without_attr_falls_back_env(self):
        os.environ["AGENT_SELECTED_MODULE_ID"] = "T07"
        args = argparse.Namespace(project="demo")  # 无 module_id 字段
        common = _import_common()
        assert common.resolve_module_id(args) == "T07"


# ── render_module_focus_hint ────────────────────────────────
class TestRenderModuleFocusHint:
    def test_none_returns_empty(self):
        common = _import_common()
        assert common.render_module_focus_hint(None, "demo") == ""

    def test_empty_returns_empty(self):
        common = _import_common()
        assert common.render_module_focus_hint("", "demo") == ""

    def test_non_empty_contains_module_id_and_project(self):
        common = _import_common()
        hint = common.render_module_focus_hint("T01", "todo-list")
        assert "T01" in hint
        assert "todo-list" in hint
        assert "单模块聚焦" in hint

    def test_hint_declares_required_outputs(self):
        common = _import_common()
        hint = common.render_module_focus_hint("T03", "demo")
        # 进度流 + 测试报告约定必现
        assert "进度/T03-progress.md" in hint
        assert "测试报告/T03.md" in hint
        # 模块清单读取指引
        assert "模块清单.md" in hint

    def test_hint_forbids_cross_module(self):
        common = _import_common()
        hint = common.render_module_focus_hint("T01", "demo")
        assert "不要输出" in hint
        assert "其他模块" in hint


# ── Vault 侧 §6.3 已加子章节（sanity check） ─────────────────
class TestVaultRoleSections:
    """Backend / Frontend 角色 §6.3 存在且包含关键关键词。"""

    @pytest.fixture
    def role_body(self):
        from engine.config import VAULT_ROOT
        return {
            "backend": (VAULT_ROOT / "00-系统" / "角色基因" / "se" /
                        "角色-后端工程师.md").read_text(encoding="utf-8"),
            "frontend": (VAULT_ROOT / "00-系统" / "角色基因" / "se" /
                         "角色-前端工程师.md").read_text(encoding="utf-8"),
        }

    def test_backend_has_module_mode(self, role_body):
        body = role_body["backend"]
        assert "### 6.3 模块化模式" in body
        assert "AGENT_SELECTED_MODULE_ID" in body
        assert "module_manifest" in body

    def test_frontend_has_module_mode(self, role_body):
        body = role_body["frontend"]
        assert "### 6.3 模块化模式" in body
        assert "AGENT_SELECTED_MODULE_ID" in body

    def test_version_bumped_to_1_8_0(self, role_body):
        assert "1.8.0" in role_body["backend"]
        assert "1.8.0" in role_body["frontend"]
