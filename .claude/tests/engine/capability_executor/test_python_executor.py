"""
test_python_executor.py — python_executor 分支覆盖（不真起 subprocess）。

覆盖：
- 成功 rc=0：返回 ok，artifact_paths 有内容
- 失败 rc≠0：ok=False，error 含 exit_code
- TimeoutExpired：exit_code=-1
- OSError（subprocess 启动失败）：exit_code=-2
- runtime.working_dir 不存在：exit_code=-2
- runtime.entry 脚本不存在：exit_code=-2
- 模板变量 {{input_name}} 正确渲染
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from engine.capability_executor.executors.python_executor import PythonExecutor


def _make_manifest(entry: str, working_dir: str, timeout_s: int = 30) -> dict:
    return {
        "id": "web-scraper/crawl",
        "version": "0.1.0",
        "source": "test",
        "runtime": {
            "type": "python",
            "entry": entry,
            "timeout_s": timeout_s,
            "working_dir": working_dir,
        },
        "triggers": ["test"],
        "inputs": [{"name": "url", "type": "url", "required": True}],
        "outputs": [
            {"name": "result", "type": "file",
             "path_pattern": "10-项目/{project}/交付物/scrape.json"},
        ],
        "audit": {"log_to": "20-知识/能力注册表/web-scraper/调用日志/{ts}-{project}.md"},
    }


class TestPythonExecutorSuccess:
    def test_rc0_returns_ok(self, tmp_path, monkeypatch):
        wd = tmp_path / "tools"
        wd.mkdir()
        (wd / "scrape.py").write_text("print('ok')", encoding="utf-8")

        m = _make_manifest("scrape.py --url {{url}}", str(wd))

        fake_proc = MagicMock(returncode=0, stdout="artifact: /path\n", stderr="")
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: fake_proc)

        result = PythonExecutor().invoke(m, {"url": "https://example.com"}, "demo")
        assert result.exit_code == 0
        assert result.ok is True

    def test_rc_nonzero_ok_false(self, tmp_path, monkeypatch):
        wd = tmp_path / "tools"
        wd.mkdir()
        (wd / "scrape.py").write_text("import sys; sys.exit(3)", encoding="utf-8")

        m = _make_manifest("scrape.py", str(wd))
        fake_proc = MagicMock(returncode=3, stdout="", stderr="crashed")
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: fake_proc)

        result = PythonExecutor().invoke(m, {}, "demo")
        assert result.exit_code == 3
        assert result.ok is False
        assert "exit_code=3" in result.error
        assert "crashed" in result.error


class TestPythonExecutorFailure:
    def test_timeout_returns_neg1(self, tmp_path, monkeypatch):
        wd = tmp_path / "tools"
        wd.mkdir()
        (wd / "scrape.py").write_text("pass", encoding="utf-8")

        m = _make_manifest("scrape.py", str(wd), timeout_s=5)

        def _raise_timeout(*a, **kw):
            raise subprocess.TimeoutExpired(cmd="python scrape.py", timeout=5)

        monkeypatch.setattr(subprocess, "run", _raise_timeout)
        result = PythonExecutor().invoke(m, {}, "demo")
        assert result.exit_code == -1
        assert "timeout after 5s" in result.error

    def test_oserror_returns_neg2(self, tmp_path, monkeypatch):
        wd = tmp_path / "tools"
        wd.mkdir()
        (wd / "scrape.py").write_text("pass", encoding="utf-8")

        m = _make_manifest("scrape.py", str(wd))

        def _raise_oserror(*a, **kw):
            raise OSError("no exec")

        monkeypatch.setattr(subprocess, "run", _raise_oserror)
        result = PythonExecutor().invoke(m, {}, "demo")
        assert result.exit_code == -2
        assert "启动失败" in result.error

    def test_missing_working_dir_returns_neg2(self, tmp_path):
        m = _make_manifest("scrape.py", str(tmp_path / "nonexistent"))
        result = PythonExecutor().invoke(m, {}, "demo")
        assert result.exit_code == -2
        assert "working_dir" in result.error

    def test_missing_entry_script_returns_neg2(self, tmp_path):
        wd = tmp_path / "tools"
        wd.mkdir()
        # 不建 scrape.py
        m = _make_manifest("scrape.py", str(wd))
        result = PythonExecutor().invoke(m, {}, "demo")
        assert result.exit_code == -2
        assert "脚本不存在" in result.error

    def test_empty_entry_returns_neg2(self, tmp_path):
        wd = tmp_path / "tools"
        wd.mkdir()
        m = _make_manifest("", str(wd))
        result = PythonExecutor().invoke(m, {}, "demo")
        assert result.exit_code == -2
        assert "entry 为空" in result.error


class TestPythonExecutorTemplateRender:
    def test_input_template_rendered_in_argv(self, tmp_path, monkeypatch):
        wd = tmp_path / "tools"
        wd.mkdir()
        (wd / "scrape.py").write_text("pass", encoding="utf-8")

        m = _make_manifest("scrape.py --url {{url}}", str(wd))
        captured_argv: list = []

        def _capture(argv, **kw):
            captured_argv.extend(argv)
            return MagicMock(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", _capture)
        PythonExecutor().invoke(m, {"url": "https://example.com"}, "demo")
        assert "https://example.com" in captured_argv
