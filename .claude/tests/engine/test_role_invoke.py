"""
test_role_invoke.py — F7 阶段 B：invoke_role 统一接口

覆盖：
- env 组装：PROJECT/TASK、module_id / contract_overrides 的 set-or-pop 语义、extra_env
- round → CLI --round 透传
- 状态映射：rc 0 → success / rc∈{2,3} → permanent_failed / 其他 → failed
- 角色解析失败 / main.py 缺失 → permanent_failed（不起 subprocess）
- mode != subprocess → NotImplementedError（in_process 留给 role_runner）
"""

from __future__ import annotations

import os

import pytest

from engine.role_invoke import RoleInvocation, RoleResult, invoke_role
from engine import role_invoke as ri


@pytest.fixture
def fake_skill(tmp_path, monkeypatch):
    """假 skill 目录 + role_to_skill_dir 定向；返回捕获 subprocess 调用的列表。"""
    skill_dir = tmp_path / ".claude" / "skills" / "dev_backend"
    skill_dir.mkdir(parents=True)
    (skill_dir / "main.py").write_text("print('ok')", encoding="utf-8")
    monkeypatch.setattr(ri, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr("engine.workflow.role_to_skill_dir", lambda name: "dev_backend")

    calls: list[dict] = []

    class FakeCompleted:
        returncode = 0

    def fake_run(argv, env=None, timeout=None):
        calls.append({"argv": argv, "env": env, "timeout": timeout})
        return FakeCompleted()

    monkeypatch.setattr(ri.subprocess, "run", fake_run)
    return calls


class TestEnvAssembly:
    def test_basic_env_and_argv(self, fake_skill):
        result = invoke_role(RoleInvocation(role="后端工程师", task="做事", project="demo"))
        assert result.ok and result.status == "success"
        call = fake_skill[0]
        assert call["env"]["PROJECT"] == "demo"
        assert call["env"]["TASK"] == "做事"
        assert "--task" in call["argv"] and "做事" in call["argv"]
        assert "--round" not in call["argv"]

    def test_module_id_and_overrides_set(self, fake_skill):
        invoke_role(RoleInvocation(
            role="后端工程师", task="t", project="p",
            module_id="T01",
            contract_overrides={"input_contract": {"task_source": "module_manifest"}},
        ))
        env = fake_skill[0]["env"]
        assert env["AGENT_SELECTED_MODULE_ID"] == "T01"
        assert "input_contract" in env["AGENT_CONTRACT_OVERRIDES"]

    def test_stale_env_popped_when_none(self, fake_skill, monkeypatch):
        """os.environ 里的陈旧值必须被 pop（原 nodes.make_role_node 语义）。"""
        monkeypatch.setenv("AGENT_SELECTED_MODULE_ID", "STALE")
        monkeypatch.setenv("AGENT_CONTRACT_OVERRIDES", "{}")
        invoke_role(RoleInvocation(role="后端工程师", task="t", project="p"))
        env = fake_skill[0]["env"]
        assert "AGENT_SELECTED_MODULE_ID" not in env
        assert "AGENT_CONTRACT_OVERRIDES" not in env

    def test_round_and_extra_env(self, fake_skill):
        invoke_role(RoleInvocation(
            role="创意发散者", task="t", project="p",
            round=3, extra_env={"X_CUSTOM": "1"},
        ))
        call = fake_skill[0]
        argv = call["argv"]
        assert argv[argv.index("--round") + 1] == "3"
        assert call["env"]["X_CUSTOM"] == "1"


class TestStatusMapping:
    @pytest.mark.parametrize("rc,expected", [
        (0, "success"), (1, "failed"), (2, "permanent_failed"), (3, "permanent_failed"),
    ])
    def test_rc_to_status(self, fake_skill, monkeypatch, rc, expected):
        class FakeCompleted:
            returncode = rc

        monkeypatch.setattr(ri.subprocess, "run", lambda *a, **kw: FakeCompleted())
        monkeypatch.setattr(ri.time, "sleep", lambda s: None)  # 跳过重试退避
        result = invoke_role(RoleInvocation(role="后端工程师", task="t", project="p"))
        assert result.status == expected
        assert result.returncode == rc


class TestPermanentFailures:
    def test_unknown_role(self, monkeypatch):
        def _raise(name):
            raise ValueError("未知角色")
        monkeypatch.setattr("engine.workflow.role_to_skill_dir", _raise)
        result = invoke_role(RoleInvocation(role="不存在", task="t", project="p"))
        assert result.status == "permanent_failed"
        assert result.returncode == -2
        assert "角色解析失败" in (result.error or "")

    def test_missing_main_py(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ri, "PROJECT_ROOT", tmp_path)  # 空目录，无 main.py
        monkeypatch.setattr("engine.workflow.role_to_skill_dir", lambda n: "ghost")
        result = invoke_role(RoleInvocation(role="ghost", task="t", project="p"))
        assert result.status == "permanent_failed"
        assert "缺 main.py" in (result.error or "")

    def test_in_process_rejects_parameterized_calls(self):
        """PoC 范围：module_id / round / contract_overrides 走 subprocess。"""
        for kwargs in (
            {"module_id": "T01"},
            {"round": 2},
            {"contract_overrides": {"input_contract": {}}},
        ):
            with pytest.raises(NotImplementedError):
                invoke_role(
                    RoleInvocation(role="x", task="t", project="p", **kwargs),
                    mode="in_process",
                )

    def test_in_process_routes_to_role_runner(self, monkeypatch):
        from engine.role_invoke import RoleResult

        calls: list[tuple] = []

        def fake_run_role(role, task, project, *, domain=None):
            calls.append((role, task, project))
            return RoleResult(
                status="success", returncode=0, role=role, elapsed_s=0.1,
                outputs=("10-项目/music/p/母带规格.md",),
            )

        monkeypatch.setattr("engine.role_runner.run_role", fake_run_role)
        result = invoke_role(
            RoleInvocation(role="母带工程师", task="t", project="p"),
            mode="in_process",
        )
        assert result.ok
        assert calls == [("母带工程师", "t", "p")]
        assert result.outputs == ("10-项目/music/p/母带规格.md",)

    def test_unknown_mode_not_implemented(self):
        with pytest.raises(NotImplementedError):
            invoke_role(
                RoleInvocation(role="x", task="t", project="p"),
                mode="teleport",
            )
