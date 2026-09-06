"""
test_capability_call_block.py — CALL 块解析 + role_runner 代执行（3c）

守两件事：

1. `output_parser.parse_claude_output_to_calls` —— 与 FILE 块同款的鲁棒性要求。
   最要命的一条是 **FILE 块内部的 CALL 标记不能被当成调用请求**：角色在产物
   正文里举例说明"怎么写 CALL 块"是完全合理的写法，不挖 FILE 块就会把一份
   《能力调用说明》文档描述的调用真的执行一遍。

2. `role_runner._run_capability_calls` 的 fail-closed —— 角色只能调自己
   `capability_refs` 里声明过的能力。这道闸是防越权，不是防手滑：摘要注入会
   把能力列表给角色看，但角色可能把别处见过的 capability id 写进 CALL 块。

设计取向沿用 FILE 块：**格式错的块照样返回并记 audit，不在解析层静默丢弃**。
静默丢弃 = 角色以为调了、实际没调、产物里看不出来 —— 正是本仓一直在修的失效型。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from engine.capability_executor.base import (
    CapabilityExecutorError,
    ExecutorResult,
    RuntimeMismatchError,
)
from output_parser import parse_claude_output_to_calls


def _call(cid: str, body: str, close: bool = True) -> str:
    tail = "<!-- /CALL -->\n" if close else ""
    return f"<!-- CALL: {cid} -->\n{body}\n{tail}"


def _file(path: str, body: str) -> str:
    return f"<!-- FILE: {path} -->\n{body}\n<!-- /FILE -->\n"


class TestParseHappyPath:
    def test_single_block(self):
        raw = _call("web-scraper/crawl", "url: https://example.com")
        calls = parse_claude_output_to_calls(raw)
        assert len(calls) == 1
        assert calls[0].capability_id == "web-scraper/crawl"
        assert calls[0].inputs == {"url": "https://example.com"}
        assert calls[0].error is None

    def test_multiline_block_scalar(self):
        """huashu-design 的 design_brief 是 3-5 段，这是选 YAML 而非 k:v 的原因。"""
        raw = _call(
            "huashu-design/render-prototype",
            "design_brief: |\n"
            "  第一段。\n"
            "  第二段。\n"
            "artifact_path: 10-项目/demo/交付物/prototype.html",
        )
        (call,) = parse_claude_output_to_calls(raw)
        assert call.error is None
        assert call.inputs["design_brief"] == "第一段。\n第二段。\n"
        assert call.inputs["artifact_path"] == "10-项目/demo/交付物/prototype.html"

    def test_mixed_scalar_types(self):
        raw = _call("x/y", "a: 1\nb: 2.5\nc: true\nd: text")
        (call,) = parse_claude_output_to_calls(raw)
        assert call.inputs == {"a": 1, "b": 2.5, "c": True, "d": "text"}

    def test_empty_body_is_not_an_error(self):
        """必填输入缺失由 invoke_capability 报，不在解析层预判。"""
        (call,) = parse_claude_output_to_calls(_call("x/y", ""))
        assert call.inputs == {} and call.error is None

    def test_two_blocks(self):
        raw = _call("a/one", "k: 1") + _call("b/two", "k: 2")
        assert [c.capability_id for c in parse_claude_output_to_calls(raw)] == [
            "a/one", "b/two",
        ]

    def test_no_warning_on_clean_input(self, capsys):
        parse_claude_output_to_calls(_call("x/y", "k: v"))
        assert "⚠️" not in capsys.readouterr().err

    def test_no_call_blocks_returns_empty(self):
        assert parse_claude_output_to_calls(_file("a.md", "正文")) == []


class TestFileBlockIsolation:
    """FILE 块内部的 CALL 标记是产物正文，不是调用请求。"""

    def test_call_inside_file_block_is_ignored(self):
        raw = _file(
            "20-知识/能力调用说明.md",
            "示例：\n" + _call("huashu-design/render-prototype", "design_brief: x"),
        )
        assert parse_claude_output_to_calls(raw) == []

    def test_call_outside_file_block_still_parsed(self):
        raw = _file("a.md", "正文") + _call("x/y", "k: v")
        (call,) = parse_claude_output_to_calls(raw)
        assert call.capability_id == "x/y"

    def test_documented_example_does_not_leak_into_real_call(self):
        """一份说明文档 + 一个真调用：只执行后者。"""
        raw = (
            _file("doc.md", _call("evil/should-not-run", "k: v"))
            + _call("real/should-run", "k: v")
        )
        assert [c.capability_id for c in parse_claude_output_to_calls(raw)] == [
            "real/should-run",
        ]


class TestParseFailureModes:
    def test_unclosed_block_warns_and_is_dropped(self, capsys):
        calls = parse_claude_output_to_calls(_call("x/y", "k: v", close=False))
        assert calls == []
        err = capsys.readouterr().err
        assert "声明了 1 个 CALL 块但只恢复出 0 个" in err

    def test_invalid_yaml_is_reported_not_raised(self):
        (call,) = parse_claude_output_to_calls(_call("x/y", "k: [unclosed"))
        assert call.error is not None
        assert "YAML" in call.error
        assert call.inputs == {}

    def test_non_mapping_body_reported(self):
        (call,) = parse_claude_output_to_calls(_call("x/y", "- a\n- b"))
        assert call.error is not None and "映射" in call.error

    def test_non_scalar_value_reported(self):
        """executor 把输入插进命令行模板，list/dict 塞不进去。"""
        (call,) = parse_claude_output_to_calls(_call("x/y", "k:\n  - a\n  - b"))
        assert call.error is not None and "标量" in call.error
        assert "k" in call.error


# ── role_runner 代执行 ────────────────────────────────────────
def _role(name: str = "UI设计师", refs: tuple[str, ...] = ()):
    return SimpleNamespace(name=name, capability_refs=refs)


def _ok_result(paths: list[Path] | None = None) -> ExecutorResult:
    return ExecutorResult(
        exit_code=0, duration_s=0.5, stdout="", stderr="",
        artifact_paths=paths or [],
    )


@pytest.fixture
def run_calls():
    from engine.role_runner import _run_capability_calls
    return _run_capability_calls


class TestFailClosed:
    def test_no_refs_declared_rejects_everything(self, run_calls, capsys):
        raw = _call("huashu-design/render-prototype", "design_brief: x")
        (rec,) = run_calls(_role(refs=()), raw, "demo")
        assert rec["result"] == "rejected"
        assert "未声明 capability_refs" in rec["error"]
        assert "⚠️" in capsys.readouterr().err

    def test_undeclared_capability_rejected(self, run_calls):
        raw = _call("web-scraper/crawl", "url: https://example.com")
        (rec,) = run_calls(
            _role(refs=("[[huashu-design/manifest]]",)), raw, "demo",
        )
        assert rec["result"] == "rejected"
        assert "不在" in rec["error"] and "声明集" in rec["error"]

    def test_malformed_block_rejected_not_silently_dropped(self, run_calls):
        (rec,) = run_calls(
            _role(refs=("[[huashu-design/manifest]]",)),
            _call("huashu-design/render-prototype", "k: [unclosed"),
            "demo",
        )
        assert rec["result"] == "rejected" and "YAML" in rec["error"]

    def test_no_calls_returns_empty(self, run_calls):
        assert run_calls(_role(refs=("[[x/manifest]]",)), "无调用块", "demo") == []


class TestDeclaredCapabilityRuns:
    def _patch(self, monkeypatch, fn):
        import engine.capability_executor.invoke as inv
        monkeypatch.setattr(inv, "invoke_capability", fn)

    def test_declared_capability_invoked(self, run_calls, monkeypatch):
        seen = {}

        def fake(cid, project, inputs, **kw):
            seen.update(cid=cid, project=project, inputs=inputs)
            return _ok_result()

        self._patch(monkeypatch, fake)
        raw = _call(
            "huashu-design/render-prototype",
            "design_brief: 深色待办 App\nartifact_path: 10-项目/demo/交付物/p.html",
        )
        (rec,) = run_calls(_role(refs=("[[huashu-design/manifest]]",)), raw, "demo")

        assert rec["result"] == "success"
        assert seen["cid"] == "huashu-design/render-prototype"
        assert seen["project"] == "demo"
        assert seen["inputs"]["design_brief"] == "深色待办 App"

    def test_executor_error_is_failed_not_raised(self, run_calls, monkeypatch):
        """runtime 没 executor（mcp/http）时 invoke 抛，整轮不能崩。"""
        def boom(cid, project, inputs, **kw):
            raise RuntimeMismatchError("runtime.type='mcp' 未实现 executor")

        self._patch(monkeypatch, boom)
        (rec,) = run_calls(
            _role(refs=("[[bgm-mcp/manifest]]",)),
            _call("bgm-mcp/pipeline", "input_path: x.mp3"),
            "demo",
        )
        assert rec["result"] == "failed"
        assert "RuntimeMismatchError" in rec["error"]

    def test_nonzero_exit_is_failed(self, run_calls, monkeypatch):
        self._patch(monkeypatch, lambda *a, **k: ExecutorResult(
            exit_code=2, duration_s=0.1, stdout="", stderr="boom",
            error="脚本退出码 2",
        ))
        (rec,) = run_calls(
            _role(refs=("[[huashu-design/manifest]]",)),
            _call("huashu-design/render-prototype", "design_brief: x"),
            "demo",
        )
        assert rec["result"] == "failed" and rec["exit_code"] == 2

    def test_artifacts_recorded_relative_to_vault(self, run_calls, monkeypatch):
        from engine.config import VAULT_ROOT
        art = VAULT_ROOT / "10-项目" / "demo" / "交付物" / "prototype.html"
        self._patch(monkeypatch, lambda *a, **k: _ok_result([art]))
        (rec,) = run_calls(
            _role(refs=("[[huashu-design/manifest]]",)),
            _call("huashu-design/render-prototype", "design_brief: x"),
            "demo",
        )
        assert rec["artifacts"] == ["10-项目/demo/交付物/prototype.html"]

    def test_capability_error_subclasses_all_caught(self, run_calls, monkeypatch):
        def boom(*a, **k):
            raise CapabilityExecutorError("任意前置错误")

        self._patch(monkeypatch, boom)
        (rec,) = run_calls(
            _role(refs=("[[huashu-design/manifest]]",)),
            _call("huashu-design/render-prototype", "design_brief: x"),
            "demo",
        )
        assert rec["result"] == "failed"
