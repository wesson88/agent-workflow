"""
test_contract_overrides_env.py — P8.2 subprocess 透传 contract_overrides 单元测试

覆盖：
- common._read_env_contract_overrides：env 缺失 / 空 / 合法 JSON dict / 非法 JSON / 非 dict
- graph/nodes.make_role_node：contract_overrides 透传到 subprocess env
- build_system_prompt 自动读 env → 传给 load_role（integration path）
"""

from __future__ import annotations

import json
import os

import pytest

from engine.role_loader import invalidate_cache


def _import_common():
    """按需 import skills.common（避免 module-level side effect 干扰 collection）。"""
    import common
    return common


# ── _read_env_contract_overrides ────────────────────────────
class TestReadEnvOverrides:
    def setup_method(self):
        invalidate_cache()
        os.environ.pop("AGENT_CONTRACT_OVERRIDES", None)

    def teardown_method(self):
        os.environ.pop("AGENT_CONTRACT_OVERRIDES", None)

    def test_missing_env_returns_none(self):
        common = _import_common()
        assert common._read_env_contract_overrides() is None

    def test_empty_env_returns_none(self):
        common = _import_common()
        os.environ["AGENT_CONTRACT_OVERRIDES"] = ""
        assert common._read_env_contract_overrides() is None

    def test_whitespace_env_returns_none(self):
        common = _import_common()
        os.environ["AGENT_CONTRACT_OVERRIDES"] = "   "
        assert common._read_env_contract_overrides() is None

    def test_valid_json_dict_returns_parsed(self):
        common = _import_common()
        payload = {
            "output_contract": {"artifacts_pattern": "module_manifest"},
        }
        os.environ["AGENT_CONTRACT_OVERRIDES"] = json.dumps(payload)
        assert common._read_env_contract_overrides() == payload

    def test_invalid_json_returns_none_with_warning(self, capsys):
        common = _import_common()
        os.environ["AGENT_CONTRACT_OVERRIDES"] = "{not valid json"
        assert common._read_env_contract_overrides() is None
        err = capsys.readouterr().err
        assert "AGENT_CONTRACT_OVERRIDES" in err
        assert "解析失败" in err

    def test_non_dict_json_returns_none(self, capsys):
        common = _import_common()
        os.environ["AGENT_CONTRACT_OVERRIDES"] = "[1, 2, 3]"
        assert common._read_env_contract_overrides() is None
        assert "顶层应为 dict" in capsys.readouterr().err


# ── build_system_prompt 通过 env 应用 override ───────────────
class TestBuildSystemPromptWithEnvOverride:
    """真实 vault load_role + env override → role.outputs 被替换。

    通过检测 TL system prompt 里是否含 module_manifest 特有路径来验证透传成功。
    """

    def setup_method(self):
        invalidate_cache()
        os.environ.pop("AGENT_CONTRACT_OVERRIDES", None)

    def teardown_method(self):
        os.environ.pop("AGENT_CONTRACT_OVERRIDES", None)

    def test_no_env_shadow_mode(self):
        """无 env → 影子模式，role.outputs 保持硬编码。"""
        from engine.role_loader import load_role
        common = _import_common()
        # 直接调 load_role 用 common 里同款读取，两者应一致
        overrides = common._read_env_contract_overrides()
        role = load_role("技术主管", contract_overrides=overrides)
        assert "10-项目/{project}/指令/给后端-T{n}.md" in role.outputs
        assert not any("模块清单.md" in p for p in role.outputs)

    def test_env_module_manifest_override_applied(self):
        """env 塞 module_manifest → role.outputs 走模块清单形态。"""
        from engine.role_loader import load_role
        common = _import_common()
        os.environ["AGENT_CONTRACT_OVERRIDES"] = json.dumps({
            "output_contract": {"artifacts_pattern": "module_manifest"},
        })
        overrides = common._read_env_contract_overrides()
        role = load_role("技术主管", contract_overrides=overrides)
        assert any("模块清单.md" in p for p in role.outputs)
        assert not any("给后端-T01.md" in p for p in role.outputs)

    def test_env_backend_input_override_applied(self):
        """env 塞 input_contract → Backend role.inputs 走模块清单形态。"""
        from engine.role_loader import load_role
        common = _import_common()
        os.environ["AGENT_CONTRACT_OVERRIDES"] = json.dumps({
            "input_contract": {"task_source": "module_manifest"},
        })
        overrides = common._read_env_contract_overrides()
        role = load_role("后端工程师", contract_overrides=overrides)
        assert any("模块清单.md" in p for p in role.inputs)
        assert not any("给后端-T01.md" in p for p in role.inputs)


# ── graph/nodes.make_role_node 透传 env ──────────────────────
class TestMakeRoleNodeEnvPropagation:
    """通过 monkeypatch role_invoke.subprocess.run 检查 env 里是否有 AGENT_CONTRACT_OVERRIDES（F7 阶段 B 后执行收敛于此）。"""

    def setup_method(self):
        invalidate_cache()
        os.environ.pop("AGENT_CONTRACT_OVERRIDES", None)

    def teardown_method(self):
        os.environ.pop("AGENT_CONTRACT_OVERRIDES", None)

    def test_no_overrides_env_absent(self, monkeypatch):
        from engine.graph import nodes
        captured: dict = {}

        def fake_run(cmd, env=None, timeout=None):
            captured["env"] = env
            r = type("R", (), {"returncode": 0})()
            return r
        from engine import role_invoke
        monkeypatch.setattr(role_invoke.subprocess, "run", fake_run)

        # make_role_node 依赖 role_to_skill_dir，直接调 _execute_single 更简单
        node = nodes.make_role_node("技术主管", halt_on_failure=False)
        state = {"project": "demo", "task": "test task"}
        node(state)
        assert "AGENT_CONTRACT_OVERRIDES" not in captured["env"]

    def test_overrides_passed_via_env(self, monkeypatch):
        from engine.graph import nodes
        captured: dict = {}

        def fake_run(cmd, env=None, timeout=None):
            captured["env"] = env
            r = type("R", (), {"returncode": 0})()
            return r
        from engine import role_invoke
        monkeypatch.setattr(role_invoke.subprocess, "run", fake_run)

        overrides = {"output_contract": {"artifacts_pattern": "module_manifest"}}
        node = nodes.make_role_node(
            "技术主管", halt_on_failure=False, contract_overrides=overrides,
        )
        state = {"project": "demo", "task": "test task"}
        node(state)
        env_val = captured["env"].get("AGENT_CONTRACT_OVERRIDES")
        assert env_val is not None
        assert json.loads(env_val) == overrides

    def test_stale_env_cleared_when_step_has_no_overrides(self, monkeypatch):
        """外部 process 已 export 过 AGENT_CONTRACT_OVERRIDES，但本 step 无 override
        → 必须清空 env（防止上一 workflow 的 override 泄漏到本 step）。
        """
        from engine.graph import nodes
        captured: dict = {}

        def fake_run(cmd, env=None, timeout=None):
            captured["env"] = env
            r = type("R", (), {"returncode": 0})()
            return r
        from engine import role_invoke
        monkeypatch.setattr(role_invoke.subprocess, "run", fake_run)

        os.environ["AGENT_CONTRACT_OVERRIDES"] = json.dumps({"stale": "value"})
        node = nodes.make_role_node("技术主管", halt_on_failure=False)
        state = {"project": "demo", "task": "test task"}
        node(state)
        assert "AGENT_CONTRACT_OVERRIDES" not in captured["env"]
