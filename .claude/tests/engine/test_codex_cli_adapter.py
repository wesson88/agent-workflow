import json
import pytest

from engine import llm
from engine.llm import _codex_disabled_mcp_args, _read_codex_json, get_provider, is_provider_available


def test_codex_json_extracts_final_text_and_usage():
    events = [
        {"type": "thread.started", "thread_id": "x"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "FILE: a.md\nhello"}},
        {"type": "turn.completed", "usage": {"input_tokens": 12, "cached_input_tokens": 3, "output_tokens": 5}},
    ]
    lines = [(json.dumps(event) + "\n").encode() for event in events]
    chunks, usage = _read_codex_json(lines, False)
    assert chunks == ["FILE: a.md\nhello"]
    assert usage == {"input_tokens": 12, "output_tokens": 5, "cache_read_input_tokens": 3}


@pytest.mark.parametrize("kind", ["command_execution", "file_change", "mcp_tool_call", "web_search"])
def test_codex_json_rejects_tool_actions(kind):
    lines = [(json.dumps({"type": "item.started", "item": {"type": kind}}) + "\n").encode()]
    with pytest.raises(RuntimeError, match="tool action"):
        _read_codex_json(lines, False)


def test_codex_provider_is_registered():
    assert get_provider("codex-cli")["cli"]["output_format"] == "codex-json"
    assert is_provider_available("codex-cli")


def test_codex_disables_configured_mcp_servers(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text(
        '[mcp_servers.bgm-mcp]\ncommand = "python"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    assert _codex_disabled_mcp_args() == [
        "-c", "mcp_servers.bgm-mcp.enabled=false",
    ]


def test_codex_mcp_config_parse_failure_is_blocking(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text("[broken", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    with pytest.raises(RuntimeError, match="Cannot verify Codex MCP configuration"):
        _codex_disabled_mcp_args()


def test_codex_rejects_mcp_names_that_need_quoting(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text(
        '[mcp_servers."other.server"]\ncommand = "python"\n', encoding="utf-8"
    )
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    with pytest.raises(RuntimeError, match="Unsupported Codex MCP server name"):
        _codex_disabled_mcp_args()


def test_codex_model_and_reasoning_effort_use_cli_config_override():
    args = llm._cli_model_args({
        "model": "gpt-6-luna", "effort": "medium", "output_format": "codex-json"
    })
    assert args == ["--model", "gpt-6-luna", "-c", 'model_reasoning_effort="medium"']


def test_other_cli_keeps_existing_effort_flag():
    assert llm._cli_model_args({"model": "claude-sonnet-5", "effort": "high", "output_format": "stream-json"}) == [
        "--model", "claude-sonnet-5", "--effort", "high"
    ]


@pytest.mark.parametrize("key,model", [
    ("codex-luna", "gpt-6-luna"),
    ("codex-terra", "gpt-6.1-terra"),
    ("codex-sol", "gpt-6.1-sol"),
    ("codex-sol-high", "gpt-6.1-sol"),
    ("codex-astra", "gpt-6-astra"),
])
def test_codex_role_provider_aliases_are_registered(key, model):
    provider = get_provider(key)
    assert provider["mode"] == "cli_only"
    assert provider["cli"]["model"] == model
    assert provider["cli"]["output_format"] == "codex-json"
    # Every alias must preserve the read-only, tool-disabled Codex invocation.
    assert provider["cli"]["extra_args"] == get_provider("codex-cli")["cli"]["extra_args"]
    assert provider["cli"]["use_system_prompt_flag"] is False
