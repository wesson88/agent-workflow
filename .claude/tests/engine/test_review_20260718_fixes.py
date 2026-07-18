"""
test_review_20260718_fixes.py — 2026-07-18 架构评审修复的回归锁定

覆盖：
1. P0-1 输出路径沙箱接线：
   - invoke 前置校验：outputs.path_pattern 越出 allowed_paths → exit 2，executor 不执行
   - resolve_artifact_paths 第二道防线：实际落盘且越界的产物 → SandboxViolationError
2. P0-2 reject 死锁修复：
   - reject → 模块 mark blocked + gate 标记 consumed
   - 二次运行不再命中同一 resolved-reject gate（无死锁）
   - _find_active_gate 跳过已消费的 resolved gate
3. 原子写统一：atomic_write_text 遇 Windows 文件锁（PermissionError）指数退避重试
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from engine.capability_executor.base import ExecutorResult, SandboxViolationError


# ══════════════════════════════════════════════════════════
# 1. P0-1 输出路径沙箱
# ══════════════════════════════════════════════════════════
@pytest.fixture
def cap_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from engine import config as engine_config
    from engine.capability_executor import audit_writer as aw
    from engine.capability_executor import manifest_loader as ml
    from engine.capability_executor import sandbox as sb
    from engine.capability_executor.executors import _common as ec
    monkeypatch.setattr(engine_config, "VAULT_ROOT", tmp_path)
    monkeypatch.setattr(aw, "VAULT_ROOT", tmp_path)
    monkeypatch.setattr(ml, "VAULT_ROOT", tmp_path)
    monkeypatch.setattr(sb, "VAULT_ROOT", tmp_path)
    monkeypatch.setattr(ec, "VAULT_ROOT", tmp_path)
    ml.invalidate_cache()
    sb.invalidate_cache()
    yield tmp_path
    ml.invalidate_cache()
    sb.invalidate_cache()


def _write_cap_manifest(vault: Path, root: str, manifest: dict) -> None:
    p = vault / "20-知识" / "能力注册表" / root / "manifest.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")


def _manifest_with_output(path_pattern: str) -> dict:
    return {
        "id": "test-cap/hello",
        "version": "0.1.0",
        "source": "test",
        "runtime": {"type": "python", "entry": "scrape.py"},
        "triggers": ["test"],
        "inputs": [{"name": "url", "type": "url", "required": True}],
        "outputs": [
            {"name": "r", "type": "file", "path_pattern": path_pattern},
        ],
        "audit": {"log_to": "20-知识/能力注册表/test-cap/调用日志/{ts}-{project}.md"},
    }


class TestOutputPathSandbox:
    def test_invoke_rejects_escaping_output_pattern(self, cap_vault, monkeypatch):
        """outputs 越出默认 allowed_paths（10-项目/*/交付物/）→ exit 2 且 executor 未执行。"""
        from engine.capability_executor.invoke import main as invoke_main
        _write_cap_manifest(
            cap_vault, "test-cap",
            _manifest_with_output("99-临时/{project}/escape.json"),
        )
        fake_exec = MagicMock()
        from engine.capability_executor import invoke as inv
        monkeypatch.setattr(inv, "get_executor", lambda t: fake_exec)

        rc = invoke_main([
            "--id", "test-cap/hello",
            "--project", "demo",
            "--input", "url=https://example.com",
        ])
        assert rc == 2
        fake_exec.invoke.assert_not_called()

    def test_invoke_rejects_absolute_output_pattern(self, cap_vault, monkeypatch, tmp_path):
        """绝对路径输出（越出 vault）→ exit 2。"""
        from engine.capability_executor.invoke import main as invoke_main
        evil = str((tmp_path.parent / "evil-out.json").resolve()).replace("\\", "/")
        _write_cap_manifest(
            cap_vault, "test-cap", _manifest_with_output(evil),
        )
        fake_exec = MagicMock()
        from engine.capability_executor import invoke as inv
        monkeypatch.setattr(inv, "get_executor", lambda t: fake_exec)

        rc = invoke_main([
            "--id", "test-cap/hello",
            "--project", "demo",
            "--input", "url=https://example.com",
        ])
        assert rc == 2
        fake_exec.invoke.assert_not_called()

    def test_invoke_allows_inbound_output_pattern(self, cap_vault, monkeypatch):
        """默认允许集内的 outputs 正常通过（回归保护）。"""
        from engine.capability_executor.invoke import main as invoke_main
        _write_cap_manifest(
            cap_vault, "test-cap",
            _manifest_with_output("10-项目/{project}/交付物/scrape.json"),
        )
        fake_exec = MagicMock()
        fake_exec.invoke.return_value = ExecutorResult(
            exit_code=0, duration_s=0.1, stdout="ok", stderr="",
        )
        from engine.capability_executor import invoke as inv
        monkeypatch.setattr(inv, "get_executor", lambda t: fake_exec)

        rc = invoke_main([
            "--id", "test-cap/hello",
            "--project", "demo",
            "--input", "url=https://example.com",
        ])
        assert rc == 0
        fake_exec.invoke.assert_called_once()

    def test_resolve_artifact_paths_rejects_existing_escaped_artifact(self, cap_vault):
        """第二道防线：实际落盘且越界的产物文件 → SandboxViolationError。"""
        from engine.capability_executor.executors._common import resolve_artifact_paths
        manifest = _manifest_with_output("99-临时/{project}/escape.json")
        escaped = cap_vault / "99-临时" / "demo" / "escape.json"
        escaped.parent.mkdir(parents=True, exist_ok=True)
        escaped.write_text("{}", encoding="utf-8")

        with pytest.raises(SandboxViolationError):
            resolve_artifact_paths(manifest, {"url": "x"}, "demo")

    def test_resolve_artifact_paths_allows_inbound_artifact(self, cap_vault):
        """允许集内已落盘产物正常返回（回归保护）。"""
        from engine.capability_executor.executors._common import resolve_artifact_paths
        manifest = _manifest_with_output("10-项目/{project}/交付物/scrape.json")
        ok_file = cap_vault / "10-项目" / "demo" / "交付物" / "scrape.json"
        ok_file.parent.mkdir(parents=True, exist_ok=True)
        ok_file.write_text("{}", encoding="utf-8")

        paths = resolve_artifact_paths(manifest, {"url": "x"}, "demo")
        assert paths == [ok_file]


# ══════════════════════════════════════════════════════════
# 2. P0-2 reject 死锁修复
# ══════════════════════════════════════════════════════════
def _write_module_manifest(vault_dir: Path, project: str, nodes_yaml: str) -> Path:
    proj_dir = vault_dir / "10-项目" / project
    proj_dir.mkdir(parents=True, exist_ok=True)
    path = proj_dir / "模块清单.md"
    content = (
        "---\ntype: module-manifest\n---\n\n# 模块清单\n\n"
        "## 结构化（DAG）\n\n```yaml\n"
        f"{nodes_yaml}"
        "```\n"
    )
    path.write_text(content, encoding="utf-8")
    return path


_NODES_T01_IN_PROGRESS = """\
nodes:
  - {id: T01, role: backend, title: 登录 API, depends_on: [], status: in_progress, estimate_hours: 3}
  - {id: T02, role: frontend, title: 登录表单, depends_on: [], status: pending, estimate_hours: 2}
"""


@pytest.fixture
def mdl_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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


def _make_mdl_step():
    from engine.workflow import WorkflowStep
    return WorkflowStep.from_yaml({
        "type": "module_development_loop",
        "name": "模块化开发",
        "manifest_path": "10-项目/{project}/模块清单.md",
    })


def _emit_and_reject_confirm_gate(project: str, module_id: str):
    """模拟生产 emit（approve/reject 双选项带 module_id）后用户 reject。"""
    from engine.human_gate import emit_gate, resolve_gate
    g = emit_gate(
        project=project,
        type="human_gate",
        mode="passive",
        reason=f"模块 {module_id} 已完成 engineer 跑通，请确认 (module_id={module_id})",
        gate="confirm_module_done",
        options=[
            {"id": "approve", "module_id": module_id},
            {"id": "reject", "module_id": module_id},
        ],
    )
    resolve_gate(project=project, gate_id=g.id, action="reject")
    return g


class TestRejectNoDeadlock:
    def test_reject_marks_blocked_and_consumes_gate(self, mdl_vault):
        from engine.graph.module_dev_loop_node import make_module_development_loop_node
        from engine.human_gate import load_gate
        from engine.manifest_render import parse_manifest

        manifest_path = _write_module_manifest(
            mdl_vault.path, mdl_vault.project, _NODES_T01_IN_PROGRESS
        )
        g = _emit_and_reject_confirm_gate(mdl_vault.project, "T01")

        node = make_module_development_loop_node(_make_mdl_step(), halt_on_failure=True)
        patch = node({"project": mdl_vault.project, "task": ""})

        # 本轮 fail（用户看到 reject 生效）
        assert "模块化开发" in patch.get("failed", [])
        # 模块被置 blocked（与 gate options.reject.effect 声明一致）
        statuses = {
            str(n["id"]): str(n["status"]) for n in parse_manifest(manifest_path)
        }
        assert statuses["T01"] == "blocked"
        # gate 被标记 consumed
        g2 = load_gate(mdl_vault.project, g.id)
        assert (g2.resolution or {}).get("consumed_at")

    def test_second_run_does_not_rehit_rejected_gate(self, mdl_vault):
        """死锁回归：第二次运行不应再命中同一条 resolved-reject gate。

        修复前：每轮重跑都命中同一 reject → fail+halt 永久死锁。
        修复后：T01 blocked、T02 pending → ready=[T02] → 落新 select gate + halt
        （halted 但**不 failed**）。
        """
        from engine.graph.module_dev_loop_node import make_module_development_loop_node

        _write_module_manifest(
            mdl_vault.path, mdl_vault.project, _NODES_T01_IN_PROGRESS
        )
        _emit_and_reject_confirm_gate(mdl_vault.project, "T01")

        node = make_module_development_loop_node(_make_mdl_step(), halt_on_failure=True)
        first = node({"project": mdl_vault.project, "task": ""})
        assert "模块化开发" in first.get("failed", [])

        second = node({"project": mdl_vault.project, "task": ""})
        assert "模块化开发" not in second.get("failed", []), (
            "第二次运行仍 fail —— resolved-reject gate 死锁未修复"
        )
        assert second.get("halted") is True  # 落了新 select gate 等用户选 T02

    def test_find_active_gate_skips_consumed(self, mdl_vault):
        from engine.graph.human_gate_node import _find_active_gate
        from engine.human_gate import emit_gate, mark_gate_consumed, resolve_gate

        g = emit_gate(
            project=mdl_vault.project, type="human_gate", mode="passive",
            reason="r", gate="confirm_module_done",
            options=[{"id": "approve", "module_id": "T01"}],
        )
        resolve_gate(project=mdl_vault.project, gate_id=g.id, action="approve")
        assert _find_active_gate(mdl_vault.project, "confirm_module_done") is not None
        mark_gate_consumed(mdl_vault.project, g.id)
        assert _find_active_gate(mdl_vault.project, "confirm_module_done") is None


# ══════════════════════════════════════════════════════════
# 3. atomic_write_text Windows 锁重试
# ══════════════════════════════════════════════════════════
class TestAtomicWriteRetry:
    def test_retries_on_permission_error(self, tmp_path, monkeypatch):
        from engine import obsidian_io

        dest = tmp_path / "sub" / "note.md"
        real_replace = os.replace
        calls = {"n": 0}

        def flaky_replace(src, dst):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise PermissionError(5, "locked")
            return real_replace(src, dst)

        monkeypatch.setattr(obsidian_io.os, "replace", flaky_replace)
        monkeypatch.setattr(obsidian_io.time, "sleep", lambda s: None)
        out = obsidian_io.atomic_write_text(dest, "内容")
        assert out == dest
        assert dest.read_text(encoding="utf-8") == "内容"
        assert calls["n"] == 3

    def test_raises_after_max_attempts(self, tmp_path, monkeypatch):
        from engine import obsidian_io

        dest = tmp_path / "note.md"

        def always_locked(src, dst):
            raise PermissionError(5, "locked")

        monkeypatch.setattr(obsidian_io.os, "replace", always_locked)
        monkeypatch.setattr(obsidian_io.time, "sleep", lambda s: None)
        with pytest.raises(PermissionError):
            obsidian_io.atomic_write_text(dest, "x")

    def test_writers_delegate_to_unified_impl(self, tmp_path, monkeypatch):
        """audit_writer / manifest_writer / human_gate 的原子写全部走统一实现。"""
        import inspect
        from engine.capability_executor import audit_writer
        from engine import manifest_writer, human_gate
        for mod in (audit_writer, manifest_writer):
            src = inspect.getsource(mod._atomic_write)
            assert "atomic_write_text" in src
        assert "atomic_write_text" in inspect.getsource(human_gate.save_gate)
