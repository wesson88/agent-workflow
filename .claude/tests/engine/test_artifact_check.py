"""
test_artifact_check.py — 产物注册表 v0.3 校验模式 + v0.4 fail 全量化

覆盖：
- check_mode：默认 fail（v0.4）/ off / warn / 非法值降级默认
- _gaps 消费端：命中 / 缺失(blocking) / {role} 绑定消费者 / {n} glob /
  未注册(advisory) / 注册表缺失(advisory)
- _gaps 产出端：通配 glob 命中 / lint 分发(advisory)
- optional 声明（`?` 后缀）：缺失降 advisory 不拦；_parse_artifact_decl 解析
- run_check：off 短路 / 角色无声明 no-op / audit 事件含 blocking+advisory
- invoke_role 接线：fail 模式消费缺口 → permanent_failed rc=-3 不起 subprocess；
  warn 模式照常；fail 模式产出缺口 → success 降 failed；
  contract_overrides 非空 → 检查整体跳过
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
        (None, "fail"),          # v0.4 全量化默认
        ("off", "off"), ("warn", "warn"), ("fail", "fail"),
        ("FAIL", "fail"), ("bogus", "fail"),
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
        assert _gaps("架构师", ("PRD",), "demo", phase="consume") == ([], [])

    def test_missing_is_blocking(self, check_vault):
        _write(check_vault / "00-系统" / "产物注册表" / "se" / "PRD.md", _entry())
        blocking, advisory = _gaps("架构师", ("PRD",), "demo", phase="consume")
        assert len(blocking) == 1 and "实例缺失" in blocking[0]
        assert advisory == []

    def test_role_placeholder_binds_consumer(self, check_vault):
        _write(check_vault / "00-系统" / "产物注册表" / "music" / "给音乐角色.md",
               _entry(artifact="给音乐角色", domain="music",
                      tpl="{proj_root}/指令/给{role}.md", producer="制作人"))
        _write(check_vault / "10-项目" / "music" / "湖向" / "指令" / "给作曲.md", "指令")
        assert _gaps("作曲", ("给音乐角色",), "湖向", phase="consume") == ([], [])
        blocking, _ = _gaps("作词", ("给音乐角色",), "湖向", phase="consume")
        assert len(blocking) == 1 and "给作词.md" in blocking[0]

    def test_n_placeholder_globs(self, check_vault):
        _write(check_vault / "00-系统" / "产物注册表" / "se" / "给后端任务卡.md",
               _entry(artifact="给后端任务卡",
                      tpl="{proj_root}/指令/给后端-T{n}.md", producer="技术主管"))
        blocking, _ = _gaps("后端工程师", ("给后端任务卡",), "demo", phase="consume")
        assert len(blocking) == 1
        _write(check_vault / "10-项目" / "demo" / "指令" / "给后端-T01.md", "任务")
        assert _gaps("后端工程师", ("给后端任务卡",), "demo",
                     phase="consume") == ([], [])

    def test_unregistered_id_is_advisory(self, check_vault):
        _write(check_vault / "00-系统" / "产物注册表" / "se" / "PRD.md", _entry())
        blocking, advisory = _gaps("架构师", ("不存在",), "demo", phase="consume")
        assert blocking == []
        assert len(advisory) == 1 and "未注册" in advisory[0]

    def test_registry_absent_is_advisory(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ar, "VAULT_ROOT", tmp_path)
        monkeypatch.setattr(ac, "VAULT_ROOT", tmp_path)
        ar.invalidate_cache()
        blocking, advisory = _gaps("架构师", ("PRD",), "demo", phase="consume")
        assert blocking == []
        assert len(advisory) == 1 and "检查跳过" in advisory[0]
        ar.invalidate_cache()


class TestProduceGaps:
    def test_wildcard_glob_any_instance(self, check_vault):
        _write(check_vault / "00-系统" / "产物注册表" / "music" / "给音乐角色.md",
               _entry(artifact="给音乐角色", domain="music",
                      tpl="{proj_root}/指令/给{role}.md", producer="制作人"))
        blocking, _ = _gaps("制作人", ("给音乐角色",), "湖向", phase="produce")
        assert len(blocking) == 1 and "实例缺失" in blocking[0]
        _write(check_vault / "10-项目" / "music" / "湖向" / "指令" / "给编曲.md", "x")
        assert _gaps("制作人", ("给音乐角色",), "湖向", phase="produce") == ([], [])

    def test_lint_dispatch_is_advisory(self, check_vault, monkeypatch):
        _write(check_vault / "00-系统" / "产物注册表" / "se" / "PRD.md",
               _entry(extra="lint: nonempty\n"))
        _write(check_vault / "10-项目" / "demo" / "PRD.md", "   ")
        monkeypatch.setitem(
            ac._LINTS, "nonempty",
            lambda content: ["内容为空"] if not content.strip() else [],
        )
        blocking, advisory = _gaps("产品经理", ("PRD",), "demo", phase="produce")
        assert blocking == []
        assert len(advisory) == 1 and "lint(nonempty)" in advisory[0]

    def test_unknown_lint_name_is_advisory(self, check_vault):
        _write(check_vault / "00-系统" / "产物注册表" / "se" / "PRD.md",
               _entry(extra="lint: ghost\n"))
        _write(check_vault / "10-项目" / "demo" / "PRD.md", "内容")
        blocking, advisory = _gaps("产品经理", ("PRD",), "demo", phase="produce")
        assert blocking == []
        assert len(advisory) == 1 and "未在 _LINTS 注册" in advisory[0]


class TestOptional:
    """v0.4：`?` 后缀声明缺失 → advisory 不拦。"""

    def test_optional_missing_is_advisory(self, check_vault):
        _write(check_vault / "00-系统" / "产物注册表" / "music" / "反馈分诊.md",
               _entry(artifact="反馈分诊", domain="music",
                      tpl="{proj_root}/反馈分诊.md", producer="音乐总监"))
        blocking, advisory = _gaps(
            "音乐总监", ("反馈分诊",), "湖向", phase="produce",
            optional=frozenset({"反馈分诊"}),
        )
        assert blocking == []
        assert len(advisory) == 1 and "optional，不拦" in advisory[0]

    def test_required_still_blocks_alongside_optional(self, check_vault):
        _write(check_vault / "00-系统" / "产物注册表" / "music" / "创作vision.md",
               _entry(artifact="创作vision", domain="music",
                      tpl="{proj_root}/创作 vision.md", producer="音乐总监"))
        _write(check_vault / "00-系统" / "产物注册表" / "music" / "反馈分诊.md",
               _entry(artifact="反馈分诊", domain="music",
                      tpl="{proj_root}/反馈分诊.md", producer="音乐总监"))
        blocking, advisory = _gaps(
            "音乐总监", ("创作vision", "反馈分诊"), "湖向", phase="produce",
            optional=frozenset({"反馈分诊"}),
        )
        assert len(blocking) == 1 and "创作vision" in blocking[0]
        assert len(advisory) == 1 and "反馈分诊" in advisory[0]

    def test_parse_artifact_decl(self):
        from engine.role_loader import _parse_artifact_decl

        ids, optional = _parse_artifact_decl(
            ["[[创作vision]]", "[[反馈分诊]]?", "[[final-Suno-prompt]] ?"])
        assert ids == ("创作vision", "反馈分诊", "final-Suno-prompt")
        assert optional == frozenset({"反馈分诊", "final-Suno-prompt"})
        assert _parse_artifact_decl(None) == ((), frozenset())


class _StubRole:
    name = "架构师"
    consumes = ("PRD",)
    produces = ()
    optional_consumes = frozenset()
    optional_produces = frozenset()


class TestRunCheck:
    def test_off_short_circuits(self, monkeypatch):
        monkeypatch.setenv("AGENT_ARTIFACT_CHECK", "off")
        assert run_check("consume", "架构师", "demo") == ("off", [])

    def test_no_declarations_noop(self, check_vault, monkeypatch):
        class Bare:
            name = "无声明"
            consumes = ()
            produces = ()
            optional_consumes = frozenset()
            optional_produces = frozenset()

        monkeypatch.setenv("AGENT_ARTIFACT_CHECK", "warn")
        monkeypatch.setattr("engine.role_loader.load_role", lambda n: Bare())
        mode, issues = run_check("consume", "无声明", "demo")
        assert mode == "warn" and issues == []

    def test_warn_emits_audit_with_split(self, check_vault, monkeypatch):
        monkeypatch.setenv("AGENT_ARTIFACT_CHECK", "warn")
        _write(check_vault / "00-系统" / "产物注册表" / "se" / "PRD.md", _entry())
        monkeypatch.setattr("engine.role_loader.load_role", lambda n: _StubRole())
        events: list[dict] = []
        monkeypatch.setattr("engine.audit.append_audit", events.append)
        mode, blocking = run_check("consume", "架构师", "demo")
        assert mode == "warn" and len(blocking) == 1
        assert events and events[0]["type"] == "artifact_check"
        assert events[0]["blocking"] == blocking
        assert events[0]["advisory"] == []

    def test_fail_returns_only_blocking(self, check_vault, monkeypatch):
        """optional 缺失不出现在 run_check 返回值里（fail 也不拦）。"""
        monkeypatch.setenv("AGENT_ARTIFACT_CHECK", "fail")
        _write(check_vault / "00-系统" / "产物注册表" / "se" / "PRD.md", _entry())

        class OptRole:
            name = "架构师"
            consumes = ("PRD",)
            produces = ()
            optional_consumes = frozenset({"PRD"})
            optional_produces = frozenset()

        monkeypatch.setattr("engine.role_loader.load_role", lambda n: OptRole())
        mode, blocking = run_check("consume", "架构师", "demo")
        assert mode == "fail" and blocking == []


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

    def test_contract_overrides_skip_all_checks(self, fake_skill, monkeypatch):
        """契约参数化调用（module_manifest 等）→ 消费/产出检查整体跳过。"""
        from engine.role_invoke import RoleInvocation, invoke_role

        def boom(phase, role, project):
            raise AssertionError("contract_overrides 调用不应触发 run_check")

        monkeypatch.setattr("engine.artifact_check.run_check", boom)
        result = invoke_role(RoleInvocation(
            role="后端工程师", task="t", project="p",
            contract_overrides={"input_contract": {"task_source": "module_manifest"}},
        ))
        assert result.ok and len(fake_skill) == 1
