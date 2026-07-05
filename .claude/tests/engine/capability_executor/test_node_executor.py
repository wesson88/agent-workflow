"""
test_node_executor.py — node_executor 分支覆盖（mock subprocess，不要求真装 Node.js）。

覆盖：
- node 在 PATH：rc=0 成功；rc≠0 失败
- node 不在 PATH：exit_code=-2 + error 提示装 Node
- runtime.working_dir 不存在：exit_code=-2
- runtime.entry 脚本不存在：exit_code=-2
- 与 python_executor 结构对称（同款 sandbox / audit / timeout 复用）
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from engine.capability_executor.executors.node_executor import NodeExecutor


def _make_manifest(entry: str, working_dir: str, timeout_s: int = 30) -> dict:
    return {
        "id": "huashu-design/render",
        "version": "1.0.0",
        "source": "test",
        "runtime": {
            "type": "node",
            "entry": entry,
            "timeout_s": timeout_s,
            "working_dir": working_dir,
        },
        "triggers": ["test"],
        "inputs": [{"name": "brief", "type": "text", "required": True}],
        "outputs": [
            {"name": "html", "type": "file",
             "path_pattern": "10-项目/{project}/交付物/prototype.html"},
        ],
        "audit": {"log_to": "20-知识/能力注册表/huashu-design/调用日志/{ts}-{project}.md"},
    }


class TestNodeExecutorMissingNode:
    def test_node_not_in_path_returns_neg2(self, monkeypatch):
        monkeypatch.setattr(
            "engine.capability_executor.executors.node_executor.shutil.which",
            lambda _: None,
        )
        m = _make_manifest("render.js", "/tmp/nonexistent")
        result = NodeExecutor().invoke(m, {"brief": "x"}, "demo")
        assert result.exit_code == -2
        assert "node" in result.error.lower()


class TestNodeExecutorSuccess:
    def test_rc0_returns_ok(self, tmp_path, monkeypatch):
        wd = tmp_path / "huashu-tools"
        wd.mkdir()
        (wd / "render.js").write_text("console.log('ok')", encoding="utf-8")

        monkeypatch.setattr(
            "engine.capability_executor.executors.node_executor.shutil.which",
            lambda _: "/usr/bin/node",
        )

        m = _make_manifest("render.js --brief {{brief}}", str(wd))
        fake_proc = MagicMock(returncode=0, stdout="artifact done", stderr="")
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: fake_proc)

        result = NodeExecutor().invoke(m, {"brief": "test"}, "demo")
        assert result.exit_code == 0
        assert result.ok is True

    def test_rc_nonzero_ok_false(self, tmp_path, monkeypatch):
        wd = tmp_path / "wd"
        wd.mkdir()
        (wd / "render.js").write_text("process.exit(1)", encoding="utf-8")
        monkeypatch.setattr(
            "engine.capability_executor.executors.node_executor.shutil.which",
            lambda _: "/usr/bin/node",
        )
        m = _make_manifest("render.js", str(wd))
        fake_proc = MagicMock(returncode=1, stdout="", stderr="broken")
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: fake_proc)

        result = NodeExecutor().invoke(m, {"brief": "x"}, "demo")
        assert result.exit_code == 1
        assert result.ok is False
        assert "broken" in result.error


class TestNodeExecutorFailure:
    def test_missing_working_dir_returns_neg2(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "engine.capability_executor.executors.node_executor.shutil.which",
            lambda _: "/usr/bin/node",
        )
        m = _make_manifest("render.js", str(tmp_path / "no-such"))
        result = NodeExecutor().invoke(m, {"brief": "x"}, "demo")
        assert result.exit_code == -2
        assert "working_dir" in result.error

    def test_missing_entry_script_returns_neg2(self, tmp_path, monkeypatch):
        wd = tmp_path / "wd"
        wd.mkdir()
        monkeypatch.setattr(
            "engine.capability_executor.executors.node_executor.shutil.which",
            lambda _: "/usr/bin/node",
        )
        m = _make_manifest("render.js", str(wd))
        result = NodeExecutor().invoke(m, {"brief": "x"}, "demo")
        assert result.exit_code == -2
        assert "脚本不存在" in result.error

    def test_timeout_returns_neg1(self, tmp_path, monkeypatch):
        wd = tmp_path / "wd"
        wd.mkdir()
        (wd / "render.js").write_text("setTimeout(() => {}, 1e9)", encoding="utf-8")
        monkeypatch.setattr(
            "engine.capability_executor.executors.node_executor.shutil.which",
            lambda _: "/usr/bin/node",
        )
        m = _make_manifest("render.js", str(wd), timeout_s=5)

        def _raise_timeout(*a, **kw):
            raise subprocess.TimeoutExpired(cmd="node render.js", timeout=5)

        monkeypatch.setattr(subprocess, "run", _raise_timeout)
        result = NodeExecutor().invoke(m, {"brief": "x"}, "demo")
        assert result.exit_code == -1
        assert "timeout" in result.error


class TestRegistryDispatch:
    def test_get_executor_node_dispatches_to_node_executor(self):
        from engine.capability_executor.executors import get_executor
        ex = get_executor("node")
        assert isinstance(ex, NodeExecutor)

    def test_get_executor_python_still_returns_python_executor(self):
        from engine.capability_executor.executors import get_executor
        from engine.capability_executor.executors.python_executor import PythonExecutor
        assert isinstance(get_executor("python"), PythonExecutor)
