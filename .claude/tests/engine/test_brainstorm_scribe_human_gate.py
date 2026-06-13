"""
test_brainstorm_scribe_human_gate.py — T2.4 brainstorm_scribe 接入 human_gate 测试

覆盖：
- _emit_ask_user_gate：readiness.decision=ask_user 时 emit 一条 passive human_gate
- _collect_resolved_brainstorm_gates：扫已 resolved 的 brainstorm_* gate（前缀过滤）
- _format_resolved_gates_for_context：渲染 markdown 含 user_response + gate.node
- has_pending 集成点：项目有 pending gate 时 scribe 不跑（验证 has_pending 真实返回）
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT / ".claude" / "skills"))
sys.path.insert(0, str(PROJECT_ROOT / ".claude"))


def _load_scribe_main():
    """importlib 加载 brainstorm_scribe/main.py（不跑 main()）。"""
    spec = importlib.util.spec_from_file_location(
        "brainstorm_scribe_main",
        PROJECT_ROOT / ".claude" / "skills" / "brainstorm_scribe" / "main.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def scribe_mod():
    return _load_scribe_main()


@pytest.fixture
def fake_vault(tmp_path, monkeypatch):
    """构造 fake vault + project_dir 重定向到 tmp（沿用 test_human_gate.py 模式）。"""
    vault = tmp_path / "vault"
    (vault / "10-项目" / "testproj").mkdir(parents=True)

    import engine.config as config_mod
    import engine.human_gate as hg_mod

    def fake_project_dir(project=None):
        name = (project or "default").strip() or "default"
        return vault / "10-项目" / name

    monkeypatch.setattr(config_mod, "project_dir", fake_project_dir)
    monkeypatch.setattr(hg_mod, "project_dir", fake_project_dir)
    return vault


# ── _emit_ask_user_gate ─────────────────────────────────────
class TestEmitAskUserGate:
    """T2.4 核心：scribe 在 readiness.decision=ask_user 时 emit passive gate"""

    def test_emit_basic_fields(self, fake_vault, scribe_mod):
        readiness = {
            "decision": "ask_user",
            "questions_for_user": [
                "首版更重视通勤导航，还是地点搜索？",
                "是否必须支持账号体系？",
            ],
            "blocking_gaps": ["MVP 边界未明确"],
        }
        gate = scribe_mod._emit_ask_user_gate(
            project="testproj",
            round_num=2,
            readiness_data=readiness,
            is_audit_round=False,
            output_rels=[
                "10-项目/testproj/产品创意原型.md",
                "10-项目/testproj/brainstorm_readiness.json",
            ],
        )
        assert gate.status == "pending"
        assert gate.mode == "passive"
        assert gate.type == "human_gate"
        assert gate.gate == "brainstorm_readiness"
        assert gate.node == "brainstorm_R2"
        # reason 应包含 questions + blocking_gaps
        assert "首版更重视通勤导航" in gate.reason
        assert "MVP 边界未明确" in gate.reason
        # suggested_actions 应含下一轮 hint
        assert any("--round 3" in s for s in gate.suggested_actions)

    def test_emit_writes_to_disk(self, fake_vault, scribe_mod):
        readiness = {
            "decision": "ask_user",
            "questions_for_user": ["问题1"],
            "blocking_gaps": [],
        }
        gate = scribe_mod._emit_ask_user_gate(
            project="testproj",
            round_num=1,
            readiness_data=readiness,
            is_audit_round=False,
            output_rels=[],
        )
        from engine.human_gate import gates_dir
        p = gates_dir("testproj") / f"{gate.id}.json"
        assert p.is_file()
        data = json.loads(p.read_text(encoding="utf-8"))
        assert data["gate"] == "brainstorm_readiness"
        assert data["node"] == "brainstorm_R1"
        assert data["status"] == "pending"

    def test_audit_round_reason_includes_audit_marker(self, fake_vault, scribe_mod):
        """R3/R6 审计模式 ask_user 时 reason 应注明是审计高影响决策"""
        readiness = {
            "decision": "ask_user",
            "questions_for_user": ["是否确认 MVP 不做账号体系？"],
            "blocking_gaps": [],
        }
        gate = scribe_mod._emit_ask_user_gate(
            project="testproj",
            round_num=3,
            readiness_data=readiness,
            is_audit_round=True,
            output_rels=[],
        )
        assert "审计高影响决策" in gate.reason

    def test_emit_handles_empty_questions(self, fake_vault, scribe_mod):
        """边界：questions_for_user 缺失/空也能 emit（reason 只含 header）"""
        readiness = {"decision": "ask_user"}  # 无 questions / blocking_gaps
        gate = scribe_mod._emit_ask_user_gate(
            project="testproj",
            round_num=1,
            readiness_data=readiness,
            is_audit_round=False,
            output_rels=[],
        )
        assert gate.status == "pending"


# ── _collect_resolved_brainstorm_gates ──────────────────────
class TestCollectResolvedGates:
    """T2.4：扫 resolved brainstorm_* gate 用于下一轮 context 注入"""

    def test_collects_only_resolved_brainstorm(self, fake_vault, scribe_mod):
        from engine.human_gate import emit_gate, resolve_gate

        # 产 3 类 gate：brainstorm(resolved) / brainstorm(pending) / module(resolved)
        g1 = emit_gate(
            project="testproj", type="human_gate", mode="passive",
            gate="brainstorm_readiness", reason="brainstorm R1",
            node="brainstorm_R1",
        )
        resolve_gate(
            project="testproj", gate_id=g1.id, action="approve",
            user_response="选 MVP 方向 A",
        )
        emit_gate(  # pending brainstorm，不应被收集
            project="testproj", type="human_gate", mode="passive",
            gate="brainstorm_readiness", reason="brainstorm R2 pending",
            node="brainstorm_R2",
        )
        g3 = emit_gate(  # resolved 但非 brainstorm
            project="testproj", type="human_gate", mode="passive",
            gate="module_selection", reason="模块选择",
        )
        resolve_gate(
            project="testproj", gate_id=g3.id, action="set_state",
            user_response="M02", patch={"selected_module_id": "M02"},
        )

        result = scribe_mod._collect_resolved_brainstorm_gates("testproj")
        assert len(result) == 1
        assert result[0].id == g1.id
        assert result[0].user_response == "选 MVP 方向 A"

    def test_empty_project_returns_empty_list(self, fake_vault, scribe_mod):
        result = scribe_mod._collect_resolved_brainstorm_gates("testproj")
        assert result == []


# ── _format_resolved_gates_for_context ──────────────────────
class TestFormatResolvedGatesForContext:
    """T2.4：resolved gate 渲染成 LLM 消费的 markdown"""

    def test_format_includes_user_response_and_node(self, fake_vault, scribe_mod):
        from engine.human_gate import emit_gate, resolve_gate

        g = emit_gate(
            project="testproj", type="human_gate", mode="passive",
            gate="brainstorm_readiness", reason="R1 追问 MVP 边界",
            node="brainstorm_R1",
        )
        resolve_gate(
            project="testproj", gate_id=g.id, action="approve",
            user_response="MVP 只做通勤导航，不做完整地图",
        )
        gates = scribe_mod._collect_resolved_brainstorm_gates("testproj")
        rendered = scribe_mod._format_resolved_gates_for_context(gates)

        assert "## 历史用户答复" in rendered
        assert "brainstorm_R1" in rendered
        assert "MVP 只做通勤导航，不做完整地图" in rendered
        assert "R1 追问 MVP 边界" in rendered

    def test_empty_returns_empty_string(self, scribe_mod):
        assert scribe_mod._format_resolved_gates_for_context([]) == ""


# ── has_pending 集成点 ──────────────────────────────────────
class TestHasPendingIntegration:
    """T2.4：scribe 用 has_pending 判定是否阻塞跑下一轮"""

    def test_no_pending_returns_false(self, fake_vault):
        from engine.human_gate import has_pending
        assert has_pending("testproj") is False

    def test_pending_gate_blocks(self, fake_vault):
        from engine.human_gate import emit_gate, has_pending
        emit_gate(
            project="testproj", type="human_gate", mode="passive",
            gate="brainstorm_readiness", reason="R1 pending",
        )
        assert has_pending("testproj") is True

    def test_resolved_does_not_block(self, fake_vault):
        from engine.human_gate import emit_gate, resolve_gate, has_pending
        g = emit_gate(
            project="testproj", type="human_gate", mode="passive",
            gate="brainstorm_readiness", reason="R1",
        )
        resolve_gate(
            project="testproj", gate_id=g.id, action="approve",
            user_response="OK",
        )
        assert has_pending("testproj") is False
