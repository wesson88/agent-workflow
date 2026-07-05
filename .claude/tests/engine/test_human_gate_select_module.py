"""
test_human_gate_select_module.py — P8.3 human_gate select_module node 单元测试

覆盖：
- WorkflowStep.from_yaml 解析 type=human_gate + gate + manifest_path
- make_human_gate_node 场景：
  A. resolved gate 消费（module_id 有效 / 无效 / done → 下一轮）
  B. pending gate 存在 → halt
  C. 无 gate + ready 集空 → 死锁 fail
  D. 无 gate + ready 集非空 → emit_gate + halt
- 全 done manifest → 无需选择直接 succeeded
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from engine.human_gate import HumanGate, save_gate
from engine.workflow import WorkflowStep


def _write_manifest(vault_dir: Path, project: str, nodes_yaml: str) -> Path:
    """在 tmp vault 的 10-项目/{project}/ 下写模块清单.md。"""
    proj_dir = vault_dir / "10-项目" / project
    proj_dir.mkdir(parents=True, exist_ok=True)
    path = proj_dir / "模块清单.md"
    content = (
        f"---\n"
        f"type: module-manifest\n"
        f"project: {project}\n"
        f"---\n\n"
        f"# 模块清单\n\n"
        f"## 结构化（DAG）\n\n"
        f"```yaml\n"
        f"{nodes_yaml}"
        f"```\n"
    )
    path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture
def tmp_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """把 VAULT_ROOT 换成 tmp_path；返回 (vault_path, project)。"""
    from engine import config as engine_config
    from engine import human_gate as hg
    from engine.graph import human_gate_node as hgn
    from engine import manifest_render as mr

    monkeypatch.setattr(engine_config, "VAULT_ROOT", tmp_path)
    monkeypatch.setattr(hg, "project_dir", lambda p: tmp_path / "10-项目" / p)
    monkeypatch.setattr(hgn, "VAULT_ROOT", tmp_path)
    ns = type("VN", (), {})()
    ns.path = tmp_path
    ns.project = "demo"
    return ns


# ── WorkflowStep.from_yaml ──────────────────────────────────
class TestFromYamlHumanGate:
    def test_select_module_parses(self):
        step = WorkflowStep.from_yaml({
            "type": "human_gate",
            "gate": "select_module",
            "manifest_path": "10-项目/{project}/模块清单.md",
        })
        assert step.type == "human_gate"
        assert step.gate == "select_module"
        assert "模块清单.md" in step.manifest_path

    def test_missing_gate_raises(self):
        with pytest.raises(ValueError, match="缺少 gate 字段"):
            WorkflowStep.from_yaml({
                "type": "human_gate",
                "manifest_path": "10-项目/{project}/模块清单.md",
            })

    def test_select_module_missing_manifest_raises(self):
        with pytest.raises(ValueError, match="缺少 manifest_path"):
            WorkflowStep.from_yaml({
                "type": "human_gate",
                "gate": "select_module",
            })


# ── make_human_gate_node 场景 ───────────────────────────────
_NODES_TWO_READY = """\
nodes:
  - { id: T01, role: backend, title: 登录 API, depends_on: [], status: pending, estimate_hours: 3 }
  - { id: T02, role: backend, title: 验证码, depends_on: [T01], status: pending, estimate_hours: 2 }
  - { id: T03, role: frontend, title: 登录表单, depends_on: [], status: pending, estimate_hours: 3 }
"""

_NODES_ALL_DONE = """\
nodes:
  - { id: T01, role: backend, title: 登录 API, depends_on: [], status: done, estimate_hours: 3 }
  - { id: T02, role: backend, title: 验证码, depends_on: [T01], status: done, estimate_hours: 2 }
"""

_NODES_EMPTY_READY = """\
nodes:
  - { id: T01, role: backend, title: 卡在 in_progress, depends_on: [], status: in_progress, estimate_hours: 3 }
"""


def _make_step(manifest_path="10-项目/{project}/模块清单.md"):
    return WorkflowStep.from_yaml({
        "type": "human_gate",
        "gate": "select_module",
        "manifest_path": manifest_path,
        "name": "选模块",
    })


class TestSelectModuleScenarios:
    def test_all_done_returns_succeeded_without_selection(self, tmp_vault):
        _write_manifest(tmp_vault.path, tmp_vault.project, _NODES_ALL_DONE)
        from engine.graph.human_gate_node import make_human_gate_node
        step = _make_step()
        node = make_human_gate_node(step, halt_on_failure=True)
        state = {"project": tmp_vault.project, "task": ""}
        patch = node(state)
        assert patch["succeeded"] == ["选模块"]
        assert patch["selected_module_id"] is None

    def test_new_gate_emitted_and_halted(self, tmp_vault):
        _write_manifest(tmp_vault.path, tmp_vault.project, _NODES_TWO_READY)
        from engine.graph.human_gate_node import make_human_gate_node
        from engine.human_gate import list_gates
        step = _make_step()
        node = make_human_gate_node(step, halt_on_failure=True)
        state = {"project": tmp_vault.project, "task": ""}
        patch = node(state)
        assert patch.get("halted") is True
        gates = list_gates(tmp_vault.project, status="pending")
        assert len(gates) == 1
        g = gates[0]
        assert g.gate == "select_module"
        opt_ids = [o["id"] for o in g.options]
        assert "T01" in opt_ids
        assert "T03" in opt_ids
        # T02 依赖 T01 未 done，不进 ready 集
        assert "T02" not in opt_ids

    def test_pending_gate_causes_halt_no_new_gate(self, tmp_vault):
        """已有 pending gate 时不重复落盘，node 仅 halt。"""
        _write_manifest(tmp_vault.path, tmp_vault.project, _NODES_TWO_READY)
        from engine.graph.human_gate_node import make_human_gate_node
        from engine.human_gate import emit_gate, list_gates
        emit_gate(
            project=tmp_vault.project,
            type="human_gate",
            mode="passive",
            reason="test",
            gate="select_module",
            options=[{"id": "T01", "label": "T01"}, {"id": "T03", "label": "T03"}],
        )
        step = _make_step()
        node = make_human_gate_node(step, halt_on_failure=True)
        state = {"project": tmp_vault.project, "task": ""}
        patch = node(state)
        assert patch.get("halted") is True
        assert len(list_gates(tmp_vault.project, status="pending")) == 1

    def test_resolved_gate_consumed(self, tmp_vault):
        """已 resolved gate → 消费 user_response → succeeded + selected_module_id。"""
        _write_manifest(tmp_vault.path, tmp_vault.project, _NODES_TWO_READY)
        from engine.graph.human_gate_node import make_human_gate_node
        from engine.human_gate import emit_gate, resolve_gate
        g = emit_gate(
            project=tmp_vault.project,
            type="human_gate",
            mode="passive",
            reason="test",
            gate="select_module",
            options=[{"id": "T01", "label": "T01"}, {"id": "T03", "label": "T03"}],
        )
        resolve_gate(
            project=tmp_vault.project,
            gate_id=g.id,
            action="approve",
            user_response="T03",
        )
        step = _make_step()
        node = make_human_gate_node(step, halt_on_failure=True)
        state = {"project": tmp_vault.project, "task": ""}
        patch = node(state)
        assert patch["succeeded"] == ["选模块"]
        assert patch["selected_module_id"] == "T03"

    def test_resolved_gate_with_invalid_module_id_fails(self, tmp_vault):
        _write_manifest(tmp_vault.path, tmp_vault.project, _NODES_TWO_READY)
        from engine.graph.human_gate_node import make_human_gate_node
        from engine.human_gate import emit_gate, resolve_gate
        g = emit_gate(
            project=tmp_vault.project,
            type="human_gate",
            mode="passive",
            reason="test",
            gate="select_module",
            options=[{"id": "T01", "label": "T01"}],
        )
        resolve_gate(
            project=tmp_vault.project,
            gate_id=g.id,
            action="approve",
            user_response="T99",  # 无效
        )
        step = _make_step()
        node = make_human_gate_node(step, halt_on_failure=True)
        state = {"project": tmp_vault.project, "task": ""}
        patch = node(state)
        assert "选模块" in patch.get("failed", [])
        assert patch.get("halted") is True

    def test_resolved_gate_pointing_to_done_starts_new_round(self, tmp_vault):
        """resolved gate 指向已 done 模块（用户切了下一轮）→ 落新 pending gate。"""
        _write_manifest(tmp_vault.path, tmp_vault.project, textwrap.dedent("""\
            nodes:
              - { id: T01, role: backend, title: A, depends_on: [], status: done, estimate_hours: 1 }
              - { id: T02, role: backend, title: B, depends_on: [], status: pending, estimate_hours: 1 }
            """))
        from engine.graph.human_gate_node import make_human_gate_node
        from engine.human_gate import emit_gate, resolve_gate, list_gates
        g = emit_gate(
            project=tmp_vault.project,
            type="human_gate",
            mode="passive",
            reason="prev round",
            gate="select_module",
            options=[{"id": "T01", "label": "T01"}],
        )
        resolve_gate(
            project=tmp_vault.project,
            gate_id=g.id,
            action="approve",
            user_response="T01",  # 现在 T01 已 done
        )
        step = _make_step()
        node = make_human_gate_node(step, halt_on_failure=True)
        state = {"project": tmp_vault.project, "task": ""}
        patch = node(state)
        # 应落新 pending gate + halt
        assert patch.get("halted") is True
        new_gates = list_gates(tmp_vault.project, status="pending")
        assert len(new_gates) == 1
        assert new_gates[0].id != g.id

    def test_empty_ready_and_no_gate_fails(self, tmp_vault):
        """无 gate + ready 集空（in_progress 卡住）→ 死锁 fail。"""
        _write_manifest(tmp_vault.path, tmp_vault.project, _NODES_EMPTY_READY)
        from engine.graph.human_gate_node import make_human_gate_node
        step = _make_step()
        node = make_human_gate_node(step, halt_on_failure=True)
        state = {"project": tmp_vault.project, "task": ""}
        patch = node(state)
        assert "选模块" in patch.get("failed", [])
        assert patch.get("halted") is True

    def test_manifest_missing_fails(self, tmp_vault):
        from engine.graph.human_gate_node import make_human_gate_node
        step = _make_step()
        node = make_human_gate_node(step, halt_on_failure=True)
        state = {"project": tmp_vault.project, "task": ""}
        patch = node(state)
        assert "选模块" in patch.get("failed", [])

    def test_upstream_halt_skipped(self, tmp_vault):
        _write_manifest(tmp_vault.path, tmp_vault.project, _NODES_TWO_READY)
        from engine.graph.human_gate_node import make_human_gate_node
        step = _make_step()
        node = make_human_gate_node(step, halt_on_failure=True)
        state = {"project": tmp_vault.project, "task": "", "halted": True}
        patch = node(state)
        assert "选模块" in patch.get("skipped", [])
        assert "succeeded" not in patch


class TestHumanGateNodeFactoryValidation:
    def test_unknown_gate_raises(self, tmp_vault):
        from engine.graph.human_gate_node import make_human_gate_node
        step = WorkflowStep.from_yaml({
            "type": "human_gate",
            "gate": "select_module",  # 让 from_yaml 通过
            "manifest_path": "10-项目/{project}/模块清单.md",
        })
        # 强行改成未知 gate（绕过 from_yaml 验证）
        from dataclasses import replace
        rogue_step = replace(step, gate="unknown_gate")
        with pytest.raises(NotImplementedError, match="human_gate.gate="):
            make_human_gate_node(rogue_step, halt_on_failure=True)
