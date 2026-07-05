"""
test_network_sandbox.py — A3（P10.5）sandbox.network 强制拦截。

覆盖：
- disabled → env 注入无效 proxy（HTTP_PROXY / HTTPS_PROXY 等 8 个变量）
- read_only → env 不改（PoC 阶段：仅 audit 记录）
- enabled → env 不改
- 缺 sandbox 字段 → 默认 disabled（规范 §3.2）
- apply_network_sandbox 不 mutate 传入 env
- python/shell/node executor 都真调 helper（正向依赖验证）
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

import pytest

from engine.capability_executor.executors._common import (
    _NETWORK_BLOCK_ENV,
    apply_network_sandbox,
)


class TestApplyNetworkSandbox:
    def test_disabled_injects_invalid_proxy(self):
        m = {"sandbox": {"network": "disabled"}}
        env = {"PATH": "/usr/bin"}
        result = apply_network_sandbox(env, m)
        assert result["HTTP_PROXY"] == "http://127.0.0.1:1"
        assert result["HTTPS_PROXY"] == "http://127.0.0.1:1"
        assert result["ALL_PROXY"] == "http://127.0.0.1:1"
        # 小写变体也拦
        assert result["http_proxy"] == "http://127.0.0.1:1"
        assert result["https_proxy"] == "http://127.0.0.1:1"
        # NO_PROXY 清空（否则 * 全豁免绕过拦截）
        assert result["NO_PROXY"] == ""
        assert result["no_proxy"] == ""

    def test_read_only_leaves_env_untouched(self):
        m = {"sandbox": {"network": "read_only"}}
        env = {"PATH": "/usr/bin"}
        result = apply_network_sandbox(env, m)
        assert "HTTP_PROXY" not in result
        assert "HTTPS_PROXY" not in result

    def test_enabled_leaves_env_untouched(self):
        m = {"sandbox": {"network": "enabled"}}
        env = {"PATH": "/usr/bin"}
        result = apply_network_sandbox(env, m)
        assert "HTTP_PROXY" not in result

    def test_missing_sandbox_defaults_to_disabled(self):
        """规范 §3.2：sandbox.network 缺省默认 disabled。"""
        m = {}
        env = {"PATH": "/usr/bin"}
        result = apply_network_sandbox(env, m)
        assert result["HTTP_PROXY"] == "http://127.0.0.1:1"

    def test_missing_network_key_in_sandbox_defaults_to_disabled(self):
        m = {"sandbox": {"allowed_paths": ["10-项目/"]}}
        env = {"PATH": "/usr/bin"}
        result = apply_network_sandbox(env, m)
        assert result["HTTP_PROXY"] == "http://127.0.0.1:1"

    def test_does_not_mutate_input_env(self):
        """apply_network_sandbox 返回**新** dict，不 mutate 传入 env。"""
        m = {"sandbox": {"network": "disabled"}}
        env = {"PATH": "/usr/bin"}
        result = apply_network_sandbox(env, m)
        assert "HTTP_PROXY" not in env  # 原 env 未被改
        assert result is not env

    def test_preserves_other_env_vars(self):
        m = {"sandbox": {"network": "disabled"}}
        env = {"PATH": "/usr/bin", "PYTHONIOENCODING": "utf-8", "MY_KEY": "x"}
        result = apply_network_sandbox(env, m)
        assert result["PATH"] == "/usr/bin"
        assert result["PYTHONIOENCODING"] == "utf-8"
        assert result["MY_KEY"] == "x"

    def test_network_block_env_covers_all_urllib_vars(self):
        """cover Python urllib.request.getproxies_environment 识别的 6 个标准变量 + no_proxy."""
        assert "HTTP_PROXY" in _NETWORK_BLOCK_ENV
        assert "HTTPS_PROXY" in _NETWORK_BLOCK_ENV
        assert "ALL_PROXY" in _NETWORK_BLOCK_ENV
        assert "http_proxy" in _NETWORK_BLOCK_ENV
        assert "https_proxy" in _NETWORK_BLOCK_ENV
        assert "all_proxy" in _NETWORK_BLOCK_ENV
        assert "NO_PROXY" in _NETWORK_BLOCK_ENV
        assert "no_proxy" in _NETWORK_BLOCK_ENV


class TestExecutorsUseNetworkSandbox:
    """三个 executor 都调 apply_network_sandbox 传给 subprocess.run 的 env。"""

    def _make_manifest(self, network: str, working_dir: str, entry: str) -> dict:
        return {
            "id": "test-cap/x",
            "version": "0.1.0",
            "source": "test",
            "runtime": {
                "type": "python",
                "entry": entry,
                "working_dir": working_dir,
            },
            "triggers": ["t"],
            "inputs": [{"name": "u", "type": "url", "required": True}],
            "outputs": [{"name": "r", "type": "file",
                         "path_pattern": "10-项目/{project}/x.json"}],
            "sandbox": {"network": network},
            "audit": {"log_to": "20-知识/能力注册表/test-cap/调用日志/{ts}-{project}.md"},
        }

    def test_python_executor_disabled_injects_proxy(self, tmp_path, monkeypatch):
        from engine.capability_executor.executors.python_executor import PythonExecutor

        wd = tmp_path / "tools"
        wd.mkdir()
        (wd / "x.py").write_text("pass", encoding="utf-8")

        captured_env: dict = {}

        def _capture(argv, **kw):
            captured_env.update(kw.get("env", {}))
            return MagicMock(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", _capture)
        m = self._make_manifest("disabled", str(wd), "x.py")
        PythonExecutor().invoke(m, {"u": "x"}, "demo")
        assert captured_env.get("HTTP_PROXY") == "http://127.0.0.1:1"
        assert captured_env.get("HTTPS_PROXY") == "http://127.0.0.1:1"

    def test_python_executor_enabled_no_proxy_inject(self, tmp_path, monkeypatch):
        from engine.capability_executor.executors.python_executor import PythonExecutor

        wd = tmp_path / "tools"
        wd.mkdir()
        (wd / "x.py").write_text("pass", encoding="utf-8")

        captured_env: dict = {}

        def _capture(argv, **kw):
            captured_env.update(kw.get("env", {}))
            return MagicMock(returncode=0, stdout="", stderr="")

        # 清 os.environ 里可能的 HTTP_PROXY，模拟干净环境
        monkeypatch.delenv("HTTP_PROXY", raising=False)
        monkeypatch.delenv("HTTPS_PROXY", raising=False)
        monkeypatch.setattr(subprocess, "run", _capture)
        m = self._make_manifest("enabled", str(wd), "x.py")
        PythonExecutor().invoke(m, {"u": "x"}, "demo")
        # enabled 时 executor 不注 HTTP_PROXY（保留系统原样）
        assert "HTTP_PROXY" not in captured_env

    def test_shell_executor_disabled_injects_proxy(self, tmp_path, monkeypatch):
        from engine.capability_executor.executors.shell_executor import ShellExecutor

        wd = tmp_path / "tools"
        wd.mkdir()

        captured_env: dict = {}

        def _capture(argv, **kw):
            captured_env.update(kw.get("env", {}))
            return MagicMock(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", _capture)
        m = self._make_manifest("disabled", str(wd), "echo hi")
        m["runtime"]["type"] = "shell"
        ShellExecutor().invoke(m, {"u": "x"}, "demo")
        assert captured_env.get("HTTP_PROXY") == "http://127.0.0.1:1"

    def test_node_executor_disabled_injects_proxy(self, tmp_path, monkeypatch):
        from engine.capability_executor.executors.node_executor import NodeExecutor

        wd = tmp_path / "tools"
        wd.mkdir()
        (wd / "x.js").write_text("console.log('ok')", encoding="utf-8")

        captured_env: dict = {}

        def _capture(argv, **kw):
            captured_env.update(kw.get("env", {}))
            return MagicMock(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(
            "engine.capability_executor.executors.node_executor.shutil.which",
            lambda _: "/usr/bin/node",
        )
        monkeypatch.setattr(subprocess, "run", _capture)
        m = self._make_manifest("disabled", str(wd), "x.js")
        m["runtime"]["type"] = "node"
        NodeExecutor().invoke(m, {"u": "x"}, "demo")
        assert captured_env.get("HTTP_PROXY") == "http://127.0.0.1:1"
