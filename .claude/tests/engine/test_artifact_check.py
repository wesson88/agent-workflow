"""
test_artifact_check.py — 产物注册表 v0.3 校验模式

覆盖：
- check_mode：默认 warn / off / fail / 非法值降级
- _gaps 消费端：命中 / 缺失 / {role} 绑定消费者 / {n} glob / 未注册 / 注册表缺失
- _gaps 产出端：通配 glob 命中 / lint 分发（命中问题 / 未知 lint 名）
- run_check：off 短路 / 角色无声明 no-op
- invoke_role 接线：fail 模式消费缺口 → permanent_failed rc=-3 不起 subprocess；
  warn 模式照常执行；fail 模式产出缺口 → success 降 failed
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engine import artifact_check as ac
from engine import artifact_registry as ar
from engine.artifact_check import check_mode, run_check, _gaps


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


_CONFIG = """---
proj_roots:
  se: "10-项目/{project}"
  music: "10-项目/music/{project}"
---
"""


def _entry(artifact="PRD", domain="se", tpl="{proj_root}/PRD.md",
           fmt="md", producer="产品经理", extra="") -> str:
    return (
        f"---\nartifact: {artifact}\ndomain: {domain}\n"
        f'path_template: "{tpl}"\nformat: {fmt}\nproducer: {producer}\n{extra}---\n\n正文\n'
    )


@pytest.fixture
def check_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ar, "VAULT_ROOT", tmp_path)
    monkeypatch.setattr(ac, "VAULT_ROOT", tmp_path)
    monkeypatch.setattr("engine.obsidian_io.VAULT_ROOT", tmp_path)
    ar.invalidate_cache()
    _write(tmp_path / "00-系统" / "产物注册表" / "_config.md", _CONFIG)
    yield tmp_path
    ar.invalidate_cache()


class TestCheckMode:
    @pytest.mark.parametrize("raw,expected", [
        (None, "warn"), ("off", "off"), ("warn", "warn"), ("fail", "fail"),
        ("FAIL", "fail"), ("bogus", "warn"),
    ])
    def test_modes(self, monkeypatch, raw, expected):
        if raw is None:
            monkeypatch.delenv("AGENT_ARTIFACT_CHECK", raising=False)
        else:
            monkeypatch.setenv("AGENT_ARTIFACT_CHECK", raw)
        assert check_mode() == expected


class TestConsumeGaps:
    def test_hit(self, check_vault):
        _write(check_vault / "00-系统" / "产物注册表" / "se" / "PRD.md", _entry())
        _write(check_vault / "10-项目" / "demo" / "PRD.md", "内容")
        assert _gaps("架构师", ("PRD",), "demo", phase="consume") == []

    def test_missing(self, check_vault):
        _write(check_vault / "00-系统" / "产物注册表" / "se" / "PRD.md", _entry())
        issues = _gaps("架构师", ("PRD",), "demo", phase="consume")
        assert len(issues) == 1 and "实例缺失" in issues[0]

    def test_role_placeholder_binds_consumer(self, check_vault):
        _write(check_vault / "00-系统" / "产物注册表" / "music" / "给音乐角色.md",
               _entry(artifact="给音乐角色", domain="music",
                      tpl="{proj_root}/指令/给{role}.md", producer="制作人"))
        _write(check_vault / "10-项目" / "music" / "湖向" / "指令" / "给作曲.md", "指令")
        assert _gaps("作曲", ("给音乐角色",), "湖向", phase="consume") == []
        issues = _gaps("作词", ("给音乐角色",), "湖向", phase="consume")
        assert len(issues) == 1 and "给作词.md" in issues[0]

    def test_n_placeholder_globs(self, check_vault):
        _write(check_vault / "00-系统" / "产物注册表" / "se" / "给后端任务卡.md",
               _entry(artifact="给后端任务卡",
                      tpl="{proj_root}/指令/给后端-T{n}.md", producer="技术主管"))
        issues = _gaps("后端工程师", ("给后端任务卡",), "demo", phase="consume")
        assert len(issues) == 1
        _write(check_vault / "10-项目" / "demo" / "指令" / "给后端-T01.md", "任务")
        assert _gaps("后端工程师", ("给后端任务卡",), "demo", phase="consume") == []

    def test_unregistered_id(self, check_vault):
        _write(check_vault / "00-系统" / "产物注册表" / "se" / "PRD.md", _entry())
        issues = _gaps("架构师", ("不存在",), "demo", phase="consume")
        assert len(issues) == 1 and "未注册" in issues[0]

    def test_registry_absent_soft_skip(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ar, "VAULT_ROOT", tmp_path)
        monkeypatch.setattr(ac, "VAULT_ROOT", tmp_path)
        ar.invalidate_cache()
        issues = _gaps("架构师", ("PRD",), "demo", phase="consume")
        assert len(issues) == 1 and "检查跳过" in issues[0]
        ar.invalidate_cache()


class TestProduceGaps:
    def test_wildcard_glob_any_instance(self, check_vault):
        _write(check_vault / "00-系统" / "产物注册表" / "music" / "给音乐角色.md",
               _entry(artifact="给音乐角色", domain="music",
                      tpl="{proj_root}/指令/给{role}.md", producer="制作人"))
        issues = _gaps("制作人", ("给音乐角色",), "湖向", phase="produce")
        assert len(issues) == 1 and "实例缺失" in issues[0]
        _write(check_vault / "10-项目" / "music" / "湖向" / "指令" / "给编曲.md", "x")
        assert _gaps("制作人", ("给音乐角色",), "湖向", phase="produce") == []

    def test_lint_dispatch(self, check_vault, monkeypatch):
        _write(check_vault / "00-系统" / "产物注册表" / "se" / "PRD.md",
               _entry(extra="lint: nonempty\n"))
        _write(check_vault / "10-项目" / "demo" / "PRD.md", "   ")
        monkeypatch.setitem(
            ac._LINTS, "nonempty",
            lambda content: ["内容为空"] if not content.strip() else [],
        )
        issues = _gaps("产品经理", ("PRD",), "demo", phase="produce")
        assert len(issues) == 1 and "lint(nonempty)" in issues[0]

    def test_unknown_lint_name(self, check_vault):
        _write(check_vault / "00-系统" / "产物注册表" / "se" / "PRD.md",
               _entry(extra="lint: ghost\n"))
        _write(check_vault / "10-项目" / "demo" / "PRD.md", "内容")
        issues = _gaps("产品经理", ("PRD",), "demo", phase="produce")
        assert len(issues) == 1 and "未在 _LINTS 注册" in issues[0]


class _StubRole:
    name = "架构师"
    consumes = ("PRD",)
    produces = ()


class TestRunCheck:
    def test_off_short_circuits(self, monkeypatch):
        monkeypatch.setenv("AGENT_ARTIFACT_CHECK", "off")
        assert run_check("consume", "架构师", "demo") == ("off", [])

    def test_no_declarations_noop(self, check_vault, monkeypatch):
        class Bare:
            name = "无声明"
            consumes = ()
            produces = ()

        monkeypatch.setenv("AGENT_ARTIFACT_CHECK", "warn")
        monkeypatch.setattr("engine.role_loader.load_role", lambda n: Bare())
        mode, issues = run_check("consume", "无声明", "demo")
        assert mode == "warn" and issues == []

    def test_warn_emits_audit(self, check_vault, monkeypatch):
        monkeypatch.setenv("AGENT_ARTIFACT_CHECK", "warn")
        _write(check_vault / "00-系统" / "产物注册表" / "se" / "PRD.md", _entry())
        monkeypatch.setattr("engine.role_loader.load_role", lambda n: _StubRole())
        events: list[dict] = []
        monkeypatch.setattr("engine.audit.append_audit", events.append)
        mode, issues = run_check("consume", "架构师", "demo")
        assert mode == "warn" and len(issues) == 1
        assert events and events[0]["type"] == "artifact_check"
        assert events[0]["phase"] == "consume"


class TestInvokeRoleWiring:
    @pytest.fixture
    def fake_skill(self, tmp_path, monkeypatch):
        from engine import role_invoke as ri

        skill_dir = tmp_path / ".claude" / "skills" / "dev_backend"
        skill_dir.mkdir(parents=True)
        (skill_dir / "main.py").write_text("print('ok')", encoding="utf-8")
        monkeypatch.setattr(ri, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(
            "engine.workflow.role_to_skill_dir", lambda name: "dev_backend")
        calls: list[dict] = []

        class FakeCompleted:
            returncode = 0

        def fake_run(argv, env=None, timeout=None):
            calls.append({"argv": argv})
            return FakeCompleted()

        monkeypatch.setattr(ri.subprocess, "run", fake_run)
        return calls

    def test_fail_mode_blocks_before_spawn(self, fake_skill, monkeypatch):
        from engine.role_invoke import RoleInvocation, invoke_role

        monkeypatch.setattr(
            "engine.artifact_check.run_check",
            lambda phase, role, project: ("fail", [f"{role}.{phase}: 缺"]),
        )
        result = invoke_role(RoleInvocation(role="后端工程师", task="t", project="p"))
        assert result.status == "permanent_failed"
        assert result.returncode == -3
        assert "消费端产物缺失" in (result.error or "")
        assert fake_skill == []  # subprocess 未启动

    def test_warn_mode_proceeds(self, fake_skill, monkeypatch):
        from engine.role_invoke import RoleInvocation, invoke_role

        monkeypatch.setattr(
            "engine.artifact_check.run_check",
            lambda phase, role, project: ("warn", [f"{role}.{phase}: 缺"]),
        )
        result = invoke_role(RoleInvocation(role="后端工程师", task="t", project="p"))
        assert result.ok and len(fake_skill) == 1

    def test_fail_mode_demotes_success_on_produce_gap(self, fake_skill, monkeypatch):
        from engine.role_invoke import RoleInvocation, invoke_role

        def selective(phase, role, project):
            if phase == "produce":
                return "fail", [f"{role}.produce: 缺"]
            return "fail", []

        monkeypatch.setattr("engine.artifact_check.run_check", selective)
        result = invoke_role(RoleInvocation(role="后端工程师", task="t", project="p"))
        assert result.status == "failed"
        assert result.returncode == 0
        assert "产出端产物缺失" in (result.error or "")
        assert len(fake_skill) == 1
