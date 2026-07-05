"""
test_invoke_cli.py — invoke.py CLI + invoke_capability 端到端（mock executor）。

覆盖：
- 成功路径：manifest_loader → sandbox → executor → audit_writer 全 pass → return 0
- capability rc ≠ 0 → return 1（audit 已写）
- manifest 缺失 → return 2
- 必填 input 缺失 → return 2
- 未知 runtime.type（http/mcp 未实现）→ return 2
- input 里 file_ref 越出沙箱 → return 2
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from engine.capability_executor.base import ExecutorResult
from engine.capability_executor.invoke import main as invoke_main


@pytest.fixture
def tmp_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from engine import config as engine_config
    from engine.capability_executor import audit_writer as aw
    from engine.capability_executor import manifest_loader as ml
    monkeypatch.setattr(engine_config, "VAULT_ROOT", tmp_path)
    monkeypatch.setattr(aw, "VAULT_ROOT", tmp_path)
    monkeypatch.setattr(ml, "VAULT_ROOT", tmp_path)
    return tmp_path


def _write_manifest(vault: Path, root: str, manifest: dict) -> None:
    p = vault / "20-知识" / "能力注册表" / root / "manifest.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")


_BASE_MANIFEST = {
    "id": "test-cap/hello",
    "version": "0.1.0",
    "source": "test",
    "runtime": {"type": "python", "entry": "scrape.py"},
    "triggers": ["test"],
    "inputs": [{"name": "url", "type": "url", "required": True}],
    "outputs": [
        {"name": "r", "type": "file",
         "path_pattern": "10-项目/{project}/交付物/scrape.json"},
    ],
    "audit": {"log_to": "20-知识/能力注册表/test-cap/调用日志/{ts}-{project}.md"},
}


class TestInvokeCLI:
    def test_success_return_0(self, tmp_vault, monkeypatch, capsys):
        _write_manifest(tmp_vault, "test-cap", _BASE_MANIFEST)

        fake_exec = MagicMock()
        fake_exec.invoke.return_value = ExecutorResult(
            exit_code=0, duration_s=0.5, stdout="ok", stderr="",
        )
        from engine.capability_executor import executors as ex
        monkeypatch.setattr(ex, "get_executor", lambda t: fake_exec)
        from engine.capability_executor import invoke as inv
        monkeypatch.setattr(inv, "get_executor", lambda t: fake_exec)

        rc = invoke_main([
            "--id", "test-cap/hello",
            "--project", "demo",
            "--input", "url=https://example.com",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "exit_code    : 0" in out

    def test_capability_rc_nonzero_returns_1(self, tmp_vault, monkeypatch):
        _write_manifest(tmp_vault, "test-cap", _BASE_MANIFEST)
        fake_exec = MagicMock()
        fake_exec.invoke.return_value = ExecutorResult(
            exit_code=3, duration_s=0.1, stdout="", stderr="crashed",
            error="exit_code=3: crashed",
        )
        from engine.capability_executor import invoke as inv
        monkeypatch.setattr(inv, "get_executor", lambda t: fake_exec)

        rc = invoke_main([
            "--id", "test-cap/hello",
            "--project", "demo",
            "--input", "url=https://example.com",
        ])
        assert rc == 1

    def test_manifest_missing_returns_2(self, tmp_vault, capsys):
        rc = invoke_main([
            "--id", "no-such/thing",
            "--project", "demo",
            "--input", "url=https://example.com",
        ])
        assert rc == 2
        err = capsys.readouterr().err
        assert "前置错误" in err

    def test_required_input_missing_returns_2(self, tmp_vault, capsys):
        _write_manifest(tmp_vault, "test-cap", _BASE_MANIFEST)
        rc = invoke_main([
            "--id", "test-cap/hello",
            "--project", "demo",
            # 无 --input url
        ])
        assert rc == 2
        err = capsys.readouterr().err
        assert "url" in err

    def test_unknown_runtime_type_returns_2(self, tmp_vault, capsys):
        m = json.loads(json.dumps(_BASE_MANIFEST))
        m["runtime"]["type"] = "http"  # 枚举允许但 P9 未实现
        _write_manifest(tmp_vault, "test-cap", m)

        rc = invoke_main([
            "--id", "test-cap/hello",
            "--project", "demo",
            "--input", "url=https://example.com",
        ])
        assert rc == 2
        err = capsys.readouterr().err
        assert "http" in err or "未实现" in err

    def test_file_ref_out_of_sandbox_returns_2(self, tmp_vault, capsys):
        m = json.loads(json.dumps(_BASE_MANIFEST))
        m["inputs"] = [
            {"name": "input_file", "type": "file_ref", "required": True},
        ]
        _write_manifest(tmp_vault, "test-cap", m)

        rc = invoke_main([
            "--id", "test-cap/hello",
            "--project", "demo",
            "--input", "input_file=99-临时/malicious.md",
        ])
        assert rc == 2
        err = capsys.readouterr().err
        assert "越出" in err or "sandbox" in err.lower()
