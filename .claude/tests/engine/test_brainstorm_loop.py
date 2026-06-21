"""
test_brainstorm_loop.py — engine/graph/brainstorm.py + workflow.py brainstorm-loop 单元测试 (T2.6)

覆盖：
- WorkflowStep.from_yaml brainstorm-loop 字段解析
- roles 必须 3 角色校验
- BrainstormState init（has_pending 阻塞 / round_state.json 续跑 / 全新）
- round_state.json 读写
- 4 种 decision 路由（continue / ready_for_prd / ask_user / stop_low_value）
- max_rounds 强停
- subprocess 调用传 --round 参数
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / ".claude" / "skills"))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / ".claude"))


# ── fixture ─────────────────────────────────────────────
@pytest.fixture
def fake_vault(tmp_path, monkeypatch):
    """构造 fake vault + project_dir 重定向到 tmp；mock token_counter 避开 tiktoken。"""
    vault = tmp_path / "vault"
    pdir = vault / "10-项目" / "testproj"
    pdir.mkdir(parents=True)
    # 预创建脑暴子目录 + inputs（供预算监控估算）
    (pdir / "脑暴").mkdir()
    (pdir / "inputs").mkdir()
    (pdir / "inputs" / "idea.md").write_text("一个测试 idea", encoding="utf-8")

    import engine.config as config_mod
    import engine.human_gate as hg_mod
    import engine.graph.brainstorm as bs_mod

    def fake_project_dir(project=None):
        name = (project or "default").strip() or "default"
        return vault / "10-项目" / name

    monkeypatch.setattr(config_mod, "project_dir", fake_project_dir)
    monkeypatch.setattr(hg_mod, "project_dir", fake_project_dir)
    monkeypatch.setattr(bs_mod, "project_dir", fake_project_dir)

    # mock token_counter 避开 tiktoken 加载 + Python 3.14 兼容性
    monkeypatch.setattr(
        "engine.token_counter.count_tokens",
        lambda text, model_key: len(text) // 4,
    )
    return vault


@pytest.fixture
def mock_subprocess_succeed(monkeypatch):
    """mock subprocess.run 始终 rc=0；记录调用参数供断言。"""
    calls: list[list[str]] = []

    class FakeCompleted:
        def __init__(self, returncode: int) -> None:
            self.returncode = returncode

    def fake_run(cmd, env=None, timeout=None):
        calls.append(cmd)
        return FakeCompleted(0)

    monkeypatch.setattr("engine.graph.brainstorm.subprocess.run", fake_run)
    return calls


def _write_readiness(vault: Path, project: str, decision: str, prd_readiness: int = 70):
    """写 fake brainstorm_readiness.json 模拟 scribe 输出。"""
    p = vault / "10-项目" / project / "brainstorm_readiness.json"
    p.write_text(
        json.dumps({
            "ready_for_prd": decision == "ready_for_prd",
            "prd_readiness": prd_readiness,
            "decision": decision,
            "blocking_gaps": [],
            "next_round_focus": ["focus-a"],
            "questions_for_user": ["q1?"] if decision == "ask_user" else [],
        }, ensure_ascii=False),
        encoding="utf-8",
    )


# ── 1. WorkflowStep 解析 ────────────────────────────────
def test_workflow_step_brainstorm_loop_parse():
    from engine.workflow import WorkflowStep

    s = WorkflowStep.from_yaml({
        "type": "brainstorm-loop",
        "name": "创意脑暴",
        "roles": ["创意发散者", "创意质询者", "创意记录员"],
        "max_rounds": 8,
        "audit_rounds": [3, 6],
        "readiness_threshold": 85,
        "context_warn_tokens": 30000,
    })
    assert s.type == "brainstorm-loop"
    assert s.name == "创意脑暴"
    assert s.roles == ("创意发散者", "创意质询者", "创意记录员")
    assert s.max_rounds == 8
    assert s.audit_rounds == (3, 6)
    assert s.readiness_threshold == 85
    assert s.context_warn_tokens == 30000
    assert s.start_round == 1


def test_workflow_step_brainstorm_loop_defaults():
    """audit_rounds 缺省默认 (3, 6)；context_warn_tokens 默认 30000。"""
    from engine.workflow import WorkflowStep

    s = WorkflowStep.from_yaml({
        "type": "brainstorm-loop",
        "roles": ["a", "b", "c"],
    })
    assert s.audit_rounds == (3, 6)
    assert s.max_rounds == 8
    assert s.context_warn_tokens == 30000
    assert s.readiness_threshold == 85


def test_workflow_step_brainstorm_loop_role_count_validation():
    """roles 必须恰好 3 个角色，否则 raise ValueError。"""
    from engine.workflow import WorkflowStep

    with pytest.raises(ValueError, match="恰好 3"):
        WorkflowStep.from_yaml({
            "type": "brainstorm-loop",
            "roles": ["a", "b"],
        })

    with pytest.raises(ValueError, match="恰好 3"):
        WorkflowStep.from_yaml({
            "type": "brainstorm-loop",
            "roles": ["a", "b", "c", "d"],
        })


# ── 2. round_state.json 读写 ────────────────────────────
def test_round_state_persist(fake_vault):
    from engine.graph.brainstorm import save_round_state, load_round_state

    assert load_round_state("testproj") is None

    save_round_state("testproj", {
        "loop_name": "创意脑暴",
        "current_round": 3,
        "max_rounds": 8,
        "last_decision": "continue_discussion",
        "round_history": [
            {"round": 1, "decision": "continue_discussion"},
            {"round": 2, "decision": "continue_discussion"},
            {"round": 3, "decision": "continue_discussion"},
        ],
        "finished": False,
    })

    loaded = load_round_state("testproj")
    assert loaded is not None
    assert loaded["current_round"] == 3
    assert loaded["last_decision"] == "continue_discussion"
    assert len(loaded["round_history"]) == 3
    assert "created_at" in loaded
    assert "updated_at" in loaded


# ── 3. init 节点：has_pending 阻塞 ──────────────────────
def test_pending_gate_blocks_orchestrator(fake_vault, monkeypatch):
    """有 pending gate 时 init 返 halted=True + finished=True + finish_reason=pending_gate。"""
    from engine.human_gate import emit_gate
    from engine.graph.brainstorm import _node_init

    # emit 一个 pending gate
    g = emit_gate(
        project="testproj",
        type="human_gate",
        mode="passive",
        gate="brainstorm_readiness",
        reason="test pending",
    )

    state = {
        "project": "testproj",
        "task": "t",
        "loop_name": "创意脑暴",
        "max_rounds": 8,
        "audit_rounds": (3, 6),
        "start_round": 1,
    }
    out = _node_init(state)
    assert out["halted"] is True
    assert out["finished"] is True
    assert out["finish_reason"] == "pending_gate"
    assert out["pending_gate_id"] == g.id


# ── 4. init 节点：续跑（round_state.json 存在） ──────────
def test_init_resumes_from_saved_state(fake_vault):
    """round_state.json 存在 + last_decision=continue → 从 saved.current_round+1 续跑。"""
    from engine.graph.brainstorm import save_round_state, _node_init

    save_round_state("testproj", {
        "loop_name": "创意脑暴",
        "current_round": 2,
        "max_rounds": 8,
        "last_decision": "continue_discussion",
        "round_history": [
            {"round": 1, "decision": "continue_discussion"},
            {"round": 2, "decision": "continue_discussion"},
        ],
        "finished": False,
    })

    state = {
        "project": "testproj",
        "task": "t",
        "loop_name": "创意脑暴",
        "max_rounds": 8,
        "audit_rounds": (3, 6),
        "start_round": 1,
    }
    out = _node_init(state)
    assert out["current_round"] == 3
    assert out["last_decision"] == "continue_discussion"
    assert len(out["round_history"]) == 2


# ── 5. init 节点：全新启动 ──────────────────────────────
def test_init_fresh_start(fake_vault):
    """无 round_state.json + 无 pending → current_round = start_round。"""
    from engine.graph.brainstorm import _node_init

    state = {
        "project": "testproj",
        "task": "t",
        "loop_name": "创意脑暴",
        "max_rounds": 8,
        "audit_rounds": (3, 6),
        "start_round": 1,
    }
    out = _node_init(state)
    assert out["current_round"] == 1
    assert "halted" not in out or not out.get("halted")


# ── 6. run_round 节点：subprocess 调用传 --round ────────
def test_run_round_invokes_three_roles_with_round_param(
    fake_vault, mock_subprocess_succeed, monkeypatch,
):
    """run_round 依次调用 diverger / challenger / scribe，每个传 --round N。"""
    from engine.graph.brainstorm import _node_run_round

    # mock role_to_skill_dir 避开 vault 角色加载
    monkeypatch.setattr(
        "engine.graph.brainstorm.role_to_skill_dir",
        lambda name: {"创意发散者": "brainstorm_diverger",
                      "创意质询者": "brainstorm_challenger",
                      "创意记录员": "brainstorm_scribe"}[name],
    )

    state = {
        "project": "testproj",
        "task": "test-task",
        "loop_name": "创意脑暴",
        "current_round": 2,
        "roles": ("创意发散者", "创意质询者", "创意记录员"),
        "max_rounds": 8,
        "audit_rounds": (3, 6),
    }
    out = _node_run_round(state)
    assert out == {}  # 成功不返 halted

    # 3 角色都被调过；每个 cmd 必含 --round 2
    assert len(mock_subprocess_succeed) == 3
    for cmd in mock_subprocess_succeed:
        assert "--round" in cmd
        idx = cmd.index("--round")
        assert cmd[idx + 1] == "2"
    # 角色顺序：diverger → challenger → scribe
    skill_names = [Path(cmd[1]).parent.name for cmd in mock_subprocess_succeed]
    assert skill_names == ["brainstorm_diverger", "brainstorm_challenger", "brainstorm_scribe"]


# ── 7. evaluate 节点：4 种 decision 路由 ────────────────
def test_decision_routing_continue(fake_vault):
    """decision=continue_discussion → 路由到 next_round（不是 finish）。"""
    from engine.graph.brainstorm import _node_evaluate, _route_after_evaluate

    _write_readiness(fake_vault, "testproj", "continue_discussion", prd_readiness=60)
    state = {
        "project": "testproj",
        "loop_name": "创意脑暴",
        "current_round": 2,
        "max_rounds": 8,
        "context_warn_tokens": 30000,
    }
    out = _node_evaluate(state)
    assert out["last_decision"] == "continue_discussion"
    assert out["pending_gate_id"] is None

    # _route_after_evaluate 应返 next_round（current_round 2 < max 8）
    full_state = {**state, **out}
    route = _route_after_evaluate(full_state)
    assert route == "next_round"


def test_decision_routing_ready_for_prd(fake_vault):
    """decision=ready_for_prd → 路由到 finish_ready_for_prd。"""
    from engine.graph.brainstorm import _node_evaluate, _route_after_evaluate

    _write_readiness(fake_vault, "testproj", "ready_for_prd", prd_readiness=92)
    state = {
        "project": "testproj",
        "loop_name": "创意脑暴",
        "current_round": 3,
        "max_rounds": 8,
        "context_warn_tokens": 30000,
    }
    out = _node_evaluate(state)
    full_state = {**state, **out}
    route = _route_after_evaluate(full_state)
    assert route == "finish_ready_for_prd"


def test_decision_routing_ask_user_captures_gate_id(fake_vault):
    """decision=ask_user 时 evaluate 节点应捕获最近一条 brainstorm_* pending gate id。"""
    from engine.human_gate import emit_gate
    from engine.graph.brainstorm import _node_evaluate, _route_after_evaluate

    _write_readiness(fake_vault, "testproj", "ask_user", prd_readiness=72)

    # 模拟 scribe 已经 emit 了 gate
    g = emit_gate(
        project="testproj",
        type="human_gate",
        mode="passive",
        gate="brainstorm_readiness",
        reason="ask_user from scribe",
    )

    state = {
        "project": "testproj",
        "loop_name": "创意脑暴",
        "current_round": 3,
        "max_rounds": 8,
        "context_warn_tokens": 30000,
    }
    out = _node_evaluate(state)
    assert out["last_decision"] == "ask_user"
    assert out["pending_gate_id"] == g.id

    full_state = {**state, **out}
    route = _route_after_evaluate(full_state)
    assert route == "finish_ask_user"


def test_decision_routing_stop_low_value(fake_vault):
    from engine.graph.brainstorm import _node_evaluate, _route_after_evaluate

    _write_readiness(fake_vault, "testproj", "stop_low_value", prd_readiness=20)
    state = {
        "project": "testproj",
        "loop_name": "创意脑暴",
        "current_round": 2,
        "max_rounds": 8,
        "context_warn_tokens": 30000,
    }
    out = _node_evaluate(state)
    full_state = {**state, **out}
    route = _route_after_evaluate(full_state)
    assert route == "finish_stop_low_value"


# ── 8. max_rounds 强停 ─────────────────────────────────
def test_max_rounds_stop(fake_vault):
    """decision=continue 但 current_round >= max_rounds → finish_max_rounds。"""
    from engine.graph.brainstorm import _node_evaluate, _route_after_evaluate

    _write_readiness(fake_vault, "testproj", "continue_discussion", prd_readiness=60)
    state = {
        "project": "testproj",
        "loop_name": "创意脑暴",
        "current_round": 8,  # ⚠️ 已等于 max_rounds
        "max_rounds": 8,
        "context_warn_tokens": 30000,
    }
    out = _node_evaluate(state)
    full_state = {**state, **out}
    route = _route_after_evaluate(full_state)
    assert route == "finish_max_rounds"


# ── 9. evaluate 节点：readiness 缺失 ───────────────────
def test_evaluate_missing_readiness_returns_halted(fake_vault):
    """readiness.json 不存在 → halted=True / finish_reason=readiness_missing。"""
    from engine.graph.brainstorm import _node_evaluate

    state = {
        "project": "testproj",
        "loop_name": "创意脑暴",
        "current_round": 1,
        "max_rounds": 8,
        "context_warn_tokens": 30000,
    }
    out = _node_evaluate(state)
    assert out["halted"] is True
    assert out["finish_reason"] == "readiness_missing"


# ── 10. 预算监控 ───────────────────────────────────────
def test_budget_monitor_warns_above_threshold(fake_vault, capsys):
    """构造一份超大 idea.md → 下轮估算 > 阈值 → stderr 含 oversized 警告。"""
    from engine.graph.brainstorm import _monitor_next_round_budget

    pdir = fake_vault / "10-项目" / "testproj"
    # 写 200K char 文件，估算 token (len//4) = 50K，超 30K 阈值
    big = "x" * 200000
    (pdir / "inputs" / "idea.md").write_text(big, encoding="utf-8")

    _monitor_next_round_budget(
        project="testproj",
        next_round=2,
        warn_threshold=30000,
    )
    captured = capsys.readouterr()
    assert "input 估算" in captured.err
    assert "30000" in captured.err
