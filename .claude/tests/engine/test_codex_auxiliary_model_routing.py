from unittest.mock import Mock

import common
import engine.llm as llm


def test_common_missing_role_falls_back_to_codex(monkeypatch):
    def missing_role(_name):
        raise common.RoleNotFound("missing")

    call = Mock(return_value="OK")
    monkeypatch.setattr(common, "load_role", missing_role)
    monkeypatch.setattr(common, "_llm_call_llm", call)
    assert common.call_llm_for_role("system", "user", "missing") == "OK"
    assert call.call_args.kwargs["model"] == "codex-sol"


def test_document_compression_uses_utility_model(monkeypatch):
    call = Mock(return_value="short")
    monkeypatch.setattr(llm, "call_llm", call)
    assert common.compress_to_limit("long content", "a.md", 5, max_retries=1) == "short"
    assert call.call_args.kwargs["model"] == "codex-luna"


def test_common_missing_role_uses_configurable_fallback(monkeypatch):
    def missing_role(_name):
        raise common.RoleNotFound("missing")

    call = Mock(return_value="OK")
    monkeypatch.setattr(common, "load_role", missing_role)
    monkeypatch.setattr(common, "_llm_call_llm", call)
    monkeypatch.setattr(common, "ROLE_FALLBACK_MODEL", "codex-terra")
    common.call_llm_for_role("system", "user", "missing")
    assert call.call_args.kwargs["model"] == "codex-terra"
