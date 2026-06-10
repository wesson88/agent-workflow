"""
test_human_gate.py — engine/human_gate.py 单元测试

覆盖：
- emit_gate（被动 / 主动）
- list_gates / has_pending
- resolve_gate（合法 action / set_state + patch / approve / reject / 失败路径）
- new_gate_id 序号递增
- 落盘 schema 与源文档 §9 一致
- 不合法 action / 已 resolved 二次解决报错
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / ".claude" / "skills"))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / ".claude"))


@pytest.fixture
def fake_vault(tmp_path, monkeypatch):
    """构造 fake vault + project_dir 重定向到 tmp。"""
    vault = tmp_path / "vault"
    (vault / "10-项目" / "testproj").mkdir(parents=True)

    # 重定向 engine.config.project_dir → tmp
    import engine.config as config_mod
    import engine.human_gate as hg_mod

    def fake_project_dir(project=None):
        name = (project or "default").strip() or "default"
        return vault / "10-项目" / name

    monkeypatch.setattr(config_mod, "project_dir", fake_project_dir)
    monkeypatch.setattr(hg_mod, "project_dir", fake_project_dir)
    return vault


# ── emit ────────────────────────────────────────────────
def test_emit_gate_passive_module_selection(fake_vault):
    from engine.human_gate import emit_gate, gates_dir

    g = emit_gate(
        project="testproj",
        type="human_gate",
        mode="passive",
        gate="module_selection",
        reason="模块划分完成，需要用户选择本轮开发模块",
        options=[
            {"id": "M01", "label": "账号模块", "effect": "本轮做账号"},
            {"id": "M02", "label": "定位模块", "effect": "本轮做定位"},
        ],
        recommended_option="M02",
        context_refs=["10-项目/testproj/模块划分.json"],
    )
    assert g.status == "pending"
    assert g.mode == "passive"
    assert g.type == "human_gate"
    assert g.gate == "module_selection"
    assert g.id.startswith("gate-")
    assert g.recommended_option == "M02"

    # 落盘
    p = gates_dir("testproj") / f"{g.id}.json"
    assert p.is_file()
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["gate"] == "module_selection"
    assert data["recommended_option"] == "M02"
    assert data["status"] == "pending"
    assert len(data["options"]) == 2
    assert data["context_refs"] == ["10-项目/testproj/模块划分.json"]


def test_emit_gate_passive_quality_failure_with_suggested_actions(fake_vault):
    from engine.human_gate import emit_gate

    g = emit_gate(
        project="testproj",
        type="human_gate",
        mode="passive",
        gate="module_audit_failed",
        reason="模块依赖存在环 M02 → M05 → M02",
        suggested_actions=["回架构师重拆", "用户手动指定边界", "终止工作流"],
    )
    assert g.gate == "module_audit_failed"
    assert g.suggested_actions == ["回架构师重拆", "用户手动指定边界", "终止工作流"]
    assert g.options == []


def test_emit_gate_active_intervention(fake_vault):
    from engine.human_gate import emit_gate

    g = emit_gate(
        project="testproj",
        type="human_intervention",
        mode="active",
        reason="账号模块先不要做，先做定位模块",
    )
    assert g.mode == "active"
    assert g.type == "human_intervention"


# ── list / has_pending ─────────────────────────────────
def test_has_pending_empty(fake_vault):
    from engine.human_gate import has_pending
    assert has_pending("testproj") is False


def test_has_pending_after_emit(fake_vault):
    from engine.human_gate import emit_gate, has_pending, resolve_gate
    assert has_pending("testproj") is False
    g = emit_gate(project="testproj", type="human_gate", mode="passive", reason="x")
    assert has_pending("testproj") is True
    resolve_gate(project="testproj", gate_id=g.id, action="approve")
    assert has_pending("testproj") is False


def test_list_gates_filter_by_status(fake_vault):
    from engine.human_gate import emit_gate, resolve_gate, list_gates
    g1 = emit_gate(project="testproj", type="human_gate", mode="passive", reason="g1")
    g2 = emit_gate(project="testproj", type="human_gate", mode="passive", reason="g2")
    resolve_gate(project="testproj", gate_id=g1.id, action="approve")

    pending = list_gates("testproj", status="pending")
    assert [g.id for g in pending] == [g2.id]

    resolved = list_gates("testproj", status="resolved")
    assert [g.id for g in resolved] == [g1.id]

    all_ = list_gates("testproj", status=None)
    assert sorted(g.id for g in all_) == sorted([g1.id, g2.id])


# ── resolve ─────────────────────────────────────────────
def test_resolve_set_state_with_patch(fake_vault):
    from engine.human_gate import emit_gate, resolve_gate, load_gate

    g = emit_gate(
        project="testproj",
        type="human_gate",
        mode="passive",
        gate="module_selection",
        reason="选模块",
    )
    resolved = resolve_gate(
        project="testproj",
        gate_id=g.id,
        action="set_state",
        user_response="选 M02",
        patch={"selected_module_id": "M02"},
    )
    assert resolved.status == "resolved"
    assert resolved.resolution == {
        "action": "set_state",
        "patch": {"selected_module_id": "M02"},
    }
    assert resolved.user_response == "选 M02"
    assert resolved.resolved_at is not None

    # 重新加载确认落盘
    g2 = load_gate("testproj", g.id)
    assert g2.status == "resolved"
    assert g2.resolution["patch"] == {"selected_module_id": "M02"}


def test_resolve_reroute_with_target_node(fake_vault):
    from engine.human_gate import emit_gate, resolve_gate

    g = emit_gate(project="testproj", type="human_intervention", mode="active",
                  reason="切到定位模块")
    resolved = resolve_gate(
        project="testproj",
        gate_id=g.id,
        action="reroute",
        target_node="step_03_dev_backend",
    )
    assert resolved.resolution["action"] == "reroute"
    assert resolved.resolution["target_node"] == "step_03_dev_backend"


def test_resolve_invalid_action_raises(fake_vault):
    from engine.human_gate import emit_gate, resolve_gate
    g = emit_gate(project="testproj", type="human_gate", mode="passive", reason="x")
    with pytest.raises(ValueError, match="未知 resolution action"):
        resolve_gate(project="testproj", gate_id=g.id, action="bogus")


def test_resolve_already_resolved_raises(fake_vault):
    from engine.human_gate import emit_gate, resolve_gate
    g = emit_gate(project="testproj", type="human_gate", mode="passive", reason="x")
    resolve_gate(project="testproj", gate_id=g.id, action="approve")
    with pytest.raises(ValueError, match="只能解决 status=pending"):
        resolve_gate(project="testproj", gate_id=g.id, action="reject")


def test_load_gate_not_found_raises(fake_vault):
    from engine.human_gate import load_gate
    with pytest.raises(FileNotFoundError):
        load_gate("testproj", "gate-99999999-999")


# ── id 生成 ──────────────────────────────────────────────
def test_new_gate_id_increments_within_day(fake_vault):
    from engine.human_gate import emit_gate
    g1 = emit_gate(project="testproj", type="human_gate", mode="passive", reason="g1")
    g2 = emit_gate(project="testproj", type="human_gate", mode="passive", reason="g2")
    g3 = emit_gate(project="testproj", type="human_gate", mode="passive", reason="g3")
    nums = [int(g.id.split("-")[-1]) for g in [g1, g2, g3]]
    assert nums == [nums[0], nums[0] + 1, nums[0] + 2]


# ── schema 与源文档对齐 ─────────────────────────────────
def test_schema_matches_source_doc_example(fake_vault):
    """落盘 JSON schema 与 元角色与人工介入机制.md §9 示例字段一致。"""
    from engine.human_gate import emit_gate, gates_dir

    g = emit_gate(
        project="testproj",
        type="human_intervention",
        mode="active",
        node="module_selection",
        reason="模块划分完成，需要用户选择本轮开发模块",
        context_refs=[
            "10-项目/testproj/模块划分.json",
            "10-项目/testproj/模块启动建议.md",
        ],
        options=[
            {"id": "M02", "label": "定位模块", "effect": "本轮只拆定位相关任务"},
        ],
        recommended_option="M02",
    )
    data = json.loads((gates_dir("testproj") / f"{g.id}.json").read_text(encoding="utf-8"))
    expected_fields = {
        "id", "project", "type", "mode", "status", "node", "reason",
        "context_refs", "options", "recommended_option",
        "user_response", "resolution", "created_at",
    }
    assert expected_fields.issubset(set(data.keys()))


# ── run_chain 集成 ──────────────────────────────────────
def test_run_chain_blocks_on_pending_gate(fake_vault, monkeypatch, capsys):
    """run_chain.main 在有 pending gate 时应 raise SystemExit(2)。"""
    from engine.human_gate import emit_gate
    emit_gate(
        project="testproj",
        type="human_gate",
        mode="passive",
        gate="module_selection",
        reason="模块划分完成，需要用户选择",
    )

    import engine.run_chain as rc
    monkeypatch.setattr(
        rc, "parse_chain_args",
        lambda: type("Args", (), {
            "list_workflows": False,
            "task": "test",
            "project": "testproj",
            "workflow": "技术开发",
            "start_from": None,
            "end_at": None,
            "skip_git": True,
        })(),
    )

    with pytest.raises(SystemExit) as exc:
        rc.main()
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "pending human_gate" in captured.out
    assert "模块划分完成" in captured.out
