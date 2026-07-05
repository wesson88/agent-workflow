"""
test_module_dev_loop_reparse.py — P8.7 Round 3 v1 stale nodes_list bug 回归锁定。

**Bug 场景**（2026-07-05 实战 todo-list-api Round 3 v1 暴露）：
- manifest 有 T01=in_progress、T02=pending（T02 depends_on [T01]）
- confirm_module_done gate 已 resolved approve（module_id=T01）
- select_module gate 已 resolved user_response=T01（上一轮 select 遗留）
- 预期：Step C mark T01=done → re-parse → Step D 发现 T01 已 done + selected_module_id=T01
  → 走 `already_done` 分支 → emit 新 select gate（ready=[T02]）+ halt
- Bug（re-parse 前）：Step C mark done 只写文件，内存 nodes_list 里 T01 仍 in_progress
  → Step D `already_done` 检查漏掉 T01 → 走 Step E → dispatch engineer 又跑 T01 一遍
  （浪费一次 LLM cost + 生成 duplicate confirm gate）

**修法**（`module_dev_loop_node.py` L88-108）：
Step C 消费 confirm gate 后 `nodes_list = parse_manifest(manifest_path)` 重读文件。

**回归保护**：本测试用 monkeypatch `_execute_single` 拦截 engineer subprocess 调用，
若被误调即说明 stale bug 复活。
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _write_manifest(vault_dir: Path, project: str, nodes_yaml: str) -> Path:
    proj_dir = vault_dir / "10-项目" / project
    proj_dir.mkdir(parents=True, exist_ok=True)
    path = proj_dir / "模块清单.md"
    content = (
        "---\n"
        "type: module-manifest\n"
        f"project: {project}\n"
        "---\n\n"
        "# 模块清单\n\n"
        "## 结构化（DAG）\n\n"
        "```yaml\n"
        f"{nodes_yaml}"
        "```\n"
    )
    path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture
def tmp_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from engine import config as engine_config
    from engine import human_gate as hg
    from engine.graph import human_gate_node as hgn
    from engine.graph import module_dev_loop_node as mdln

    monkeypatch.setattr(engine_config, "VAULT_ROOT", tmp_path)
    monkeypatch.setattr(hg, "project_dir", lambda p: tmp_path / "10-项目" / p)
    monkeypatch.setattr(hgn, "VAULT_ROOT", tmp_path)
    monkeypatch.setattr(mdln, "VAULT_ROOT", tmp_path)
    ns = type("VN", (), {})()
    ns.path = tmp_path
    ns.project = "demo"
    return ns


def _make_step():
    from engine.workflow import WorkflowStep
    return WorkflowStep.from_yaml({
        "type": "module_development_loop",
        "name": "模块化开发",
        "manifest_path": "10-项目/{project}/模块清单.md",
        "engineer_contract_overrides": {
            "input_contract": {"task_source": "module_manifest"},
        },
    })


_NODES_T01_IN_PROGRESS_T02_DEPENDS = """\
nodes:
  - {id: T01, role: backend, title: 骨架, depends_on: [], status: in_progress, estimate_hours: 3}
  - {id: T02, role: backend, title: 数据模型, depends_on: [T01], status: pending, estimate_hours: 2}
"""


class TestReparseAfterConfirmApprove:
    """Round 3 v1 stale bug 回归保护 —— 核心断言：不重复 dispatch engineer。"""

    def test_confirm_approve_with_stale_select_gate_does_not_redispatch(
        self, tmp_vault, monkeypatch
    ):
        _write_manifest(
            tmp_vault.path, tmp_vault.project,
            _NODES_T01_IN_PROGRESS_T02_DEPENDS,
        )
        from engine.graph.module_dev_loop_node import (
            make_module_development_loop_node,
        )
        from engine.human_gate import emit_gate, list_gates, resolve_gate

        # 上一轮 select gate（选了 T01）已 resolved
        g_sel = emit_gate(
            project=tmp_vault.project,
            type="human_gate",
            mode="passive",
            reason="select_module_last_round",
            gate="select_module",
            options=[{"id": "T01", "label": "骨架"}],
        )
        resolve_gate(
            project=tmp_vault.project,
            gate_id=g_sel.id,
            action="approve",
            user_response="T01",
        )

        # engineer 跑完 T01 → mark T01=in_progress + emit confirm gate（本轮已 approve）
        g_conf = emit_gate(
            project=tmp_vault.project,
            type="human_gate",
            mode="passive",
            reason="module_id=T01",
            gate="confirm_module_done",
            options=[{"id": "approve", "module_id": "T01"}],
        )
        resolve_gate(
            project=tmp_vault.project,
            gate_id=g_conf.id,
            action="approve",
        )

        # 关键 spy：拦截 _execute_single，任何调用都是 stale bug 复活的信号
        exec_calls: list[tuple] = []

        def _fake_execute_single(main_py, subtask, project, env):
            exec_calls.append((main_py, subtask, project, dict(env)))
            return 0  # 假装 engineer 成功

        from engine.graph import module_dev_loop_node as mdln
        monkeypatch.setattr(mdln, "_execute_single", _fake_execute_single)

        step = _make_step()
        node = make_module_development_loop_node(step, halt_on_failure=True)
        patch = node({"project": tmp_vault.project, "task": ""})

        # ── 断言 ────────────────────────────────────
        # 1. 引擎不再重复 dispatch engineer（stale bug 若复活会命中）
        assert exec_calls == [], (
            f"Stale nodes_list 回归：Step C mark done 后 Step D 用 stale "
            f"nodes_list 误把 T01（已 done）当 pending 又 dispatch 了。"
            f"exec_calls={exec_calls}"
        )
        # 2. workflow halt（等待用户选下一模块）
        assert patch.get("halted") is True
        # 3. manifest T01 已 mark done
        from engine.manifest_render import parse_manifest
        manifest_path = (
            tmp_vault.path / "10-项目" / tmp_vault.project / "模块清单.md"
        )
        nodes = parse_manifest(manifest_path)
        by_id = {n["id"]: n for n in nodes}
        assert by_id["T01"]["status"] == "done"
        # 4. 新 select_module gate 落盘（ready 集 = [T02]，因为 T02 依赖 T01 done）
        pending = list_gates(tmp_vault.project, status="pending")
        select_gates = [g for g in pending if g.gate == "select_module"]
        assert len(select_gates) == 1, (
            f"应 emit 一条新 select_module gate；实际 pending={pending}"
        )
        new_gate = select_gates[0]
        # 新 gate 应含 T02 作为 ready 集之一（不含 T01，因为 T01 已 done）
        option_ids = {opt.get("id") for opt in new_gate.options}
        assert "T02" in option_ids
        assert "T01" not in option_ids

    def test_confirm_approve_without_select_gate_still_ok(
        self, tmp_vault, monkeypatch
    ):
        """回归保护同时不破坏原路径：无 select gate 时 Step D 走 `emit + halt` 分支。"""
        _write_manifest(
            tmp_vault.path, tmp_vault.project,
            _NODES_T01_IN_PROGRESS_T02_DEPENDS,
        )
        from engine.graph.module_dev_loop_node import (
            make_module_development_loop_node,
        )
        from engine.human_gate import emit_gate, list_gates, resolve_gate

        g = emit_gate(
            project=tmp_vault.project,
            type="human_gate",
            mode="passive",
            reason="module_id=T01",
            gate="confirm_module_done",
            options=[{"id": "approve", "module_id": "T01"}],
        )
        resolve_gate(
            project=tmp_vault.project,
            gate_id=g.id,
            action="approve",
        )

        exec_calls: list[tuple] = []
        from engine.graph import module_dev_loop_node as mdln
        monkeypatch.setattr(
            mdln, "_execute_single",
            lambda *a, **kw: exec_calls.append(a) or 0,
        )

        step = _make_step()
        node = make_module_development_loop_node(step, halt_on_failure=True)
        patch = node({"project": tmp_vault.project, "task": ""})

        assert exec_calls == []
        assert patch.get("halted") is True
        # T01 mark done
        from engine.manifest_render import parse_manifest
        nodes = parse_manifest(
            tmp_vault.path / "10-项目" / tmp_vault.project / "模块清单.md"
        )
        assert next(n for n in nodes if n["id"] == "T01")["status"] == "done"
        # emit 新 select gate
        pending = list_gates(tmp_vault.project, status="pending")
        assert any(g.gate == "select_module" for g in pending)

    def test_confirm_approve_all_done_returns_succeeded_via_reparse(
        self, tmp_vault
    ):
        """re-parse 后如果所有模块都 done，直接 succeeded（不进 step D）。"""
        # T01=in_progress，approve 后就是唯一模块 → 全 done
        nodes_yaml = (
            "nodes:\n"
            "  - {id: T01, role: backend, title: 单模块, "
            "depends_on: [], status: in_progress, estimate_hours: 1}\n"
        )
        _write_manifest(tmp_vault.path, tmp_vault.project, nodes_yaml)
        from engine.graph.module_dev_loop_node import (
            make_module_development_loop_node,
        )
        from engine.human_gate import emit_gate, resolve_gate

        g = emit_gate(
            project=tmp_vault.project,
            type="human_gate",
            mode="passive",
            reason="module_id=T01",
            gate="confirm_module_done",
            options=[{"id": "approve", "module_id": "T01"}],
        )
        resolve_gate(
            project=tmp_vault.project,
            gate_id=g.id,
            action="approve",
        )

        step = _make_step()
        node = make_module_development_loop_node(step, halt_on_failure=True)
        patch = node({"project": tmp_vault.project, "task": ""})

        assert "模块化开发" in patch.get("succeeded", []), (
            f"re-parse 后应识别所有模块 done → succeeded；实际 patch={patch}"
        )
