"""
test_workflow_module_yaml.py — P8.5 项目模块化开发 workflow yaml 加载 + 端到端 build_graph

覆盖：
- load_workflow('项目模块化开发') 从 vault 读到 4 个 steps
- 步骤类型：linear × 3 + module_development_loop × 1
- TL step 含 contract_overrides.output_contract.artifacts_pattern=module_manifest
- module_development_loop step 含 engineer_contract_overrides.input_contract
- build_graph 可编译（含 module_development_loop 分派）
"""

from __future__ import annotations

import pytest

from engine.role_loader import invalidate_cache as invalidate_role_cache
from engine.workflow import invalidate_cache, load_workflow


def setup_function():
    invalidate_cache()
    invalidate_role_cache()  # 防其他测试 monkeypatch VAULT_ROOT 后残留 stale 索引


class TestWorkflowLoad:
    def setup_method(self):
        invalidate_cache()
        invalidate_role_cache()

    def test_workflow_exists(self):
        wf = load_workflow("项目模块化开发")
        assert wf.name == "项目模块化开发"
        assert wf.halt_on_failure is False

    def test_four_steps_correct_types(self):
        wf = load_workflow("项目模块化开发")
        assert len(wf.steps) == 4
        types = [s.type for s in wf.steps]
        assert types == ["linear", "linear", "linear", "module_development_loop"]

    def test_pm_and_arch_no_overrides(self):
        wf = load_workflow("项目模块化开发")
        pm, arch = wf.steps[0], wf.steps[1]
        assert pm.role == "产品经理"
        assert pm.contract_overrides is None
        assert arch.role == "架构师"
        assert arch.contract_overrides is None

    def test_tl_has_module_manifest_override(self):
        wf = load_workflow("项目模块化开发")
        tl = wf.steps[2]
        assert tl.role == "技术主管"
        assert tl.contract_overrides is not None
        assert (
            tl.contract_overrides["output_contract"]["artifacts_pattern"]
            == "module_manifest"
        )

    def test_module_dev_loop_configured(self):
        wf = load_workflow("项目模块化开发")
        loop = wf.steps[3]
        assert loop.type == "module_development_loop"
        assert "模块清单.md" in loop.manifest_path
        assert (
            loop.engineer_contract_overrides["input_contract"]["task_source"]
            == "module_manifest"
        )


class TestBuildGraph:
    def setup_method(self):
        invalidate_cache()
        invalidate_role_cache()

    def test_graph_builds(self):
        from engine.graph.build import build_graph
        wf = load_workflow("项目模块化开发")
        graph = build_graph(wf)
        assert graph is not None
