"""
test_output_parser_robustness.py — FILE 块解析的三种静默丢文件失效模式

守 `output_parser.parse_claude_output_to_files`（S3）。三种失效模式都是
**丢文件但不报错**，历史上靠人工验收才发现：

1. 漏写 `<!-- /FILE -->` → 下一个块被吞进上一个文件
   实战：pain-radar 坑 8（2026-05-16），T06a 两个块拼进一个文件，
   `test_radar.py` 744 行手工删到 597 行才干净
2. 输出被 max_tokens 截断 → 末块无闭合标签，正则完全不匹配，文件凭空消失
   实战：mini-ledger §3（2026-05-21），fallback 单 call 少产出 T04/T05
3. 同一路径出现多次 → 后者静默覆盖前者
   实战：pain-radar 坑 4，两份 `radar.py` 路径冲突

设计取向：**尽力恢复 + stderr 告警，不判失败**。判失败要多烧一次 LLM 调用
且未必更好；但必须可见 —— 不可见正是这三条能潜伏三个月的原因。
"""

from __future__ import annotations

from output_parser import parse_claude_output_to_files


def _block(path: str, body: str, close: bool = True) -> str:
    tail = "<!-- /FILE -->\n" if close else ""
    return f"<!-- FILE: {path} -->\n{body}\n{tail}"


class TestHappyPath:
    def test_two_clean_blocks(self):
        raw = _block("a.py", "print(1)") + _block("b.py", "print(2)")
        out = parse_claude_output_to_files(raw)
        assert set(out) == {"a.py", "b.py"}
        assert "print(1)" in out["a.py"] and "print(2)" in out["b.py"]

    def test_no_warning_on_clean_input(self, capsys):
        parse_claude_output_to_files(_block("a.py", "x = 1"))
        assert "⚠️" not in capsys.readouterr().err

    def test_code_fence_stripped(self):
        raw = _block("a.py", "```python\nprint(1)\n```")
        assert parse_claude_output_to_files(raw)["a.py"].strip() == "print(1)"

    def test_pure_comment_becomes_empty(self):
        raw = _block("src/backend/__init__.py", "<!-- empty – marks package -->")
        assert parse_claude_output_to_files(raw)["src/backend/__init__.py"] == ""


class TestMissingCloseTag:
    """失效模式 1：漏写闭合标签，下一个块被吞（pain-radar 坑 8）。"""

    RAW = (
        "<!-- FILE: tests/test_radar.py -->\n"
        "def test_a():\n    assert True\n"
        "<!-- FILE: tests/test_output.py -->\n"
        "def test_b():\n    assert True\n"
        "<!-- /FILE -->\n"
    )

    def test_both_files_recovered(self):
        out = parse_claude_output_to_files(self.RAW)
        assert set(out) == {"tests/test_radar.py", "tests/test_output.py"}

    def test_no_residual_marker_in_content(self):
        """坑 8 的直接症状：残留标签进 .py → SyntaxError。"""
        out = parse_claude_output_to_files(self.RAW)
        for path, content in out.items():
            assert "<!-- FILE:" not in content, f"{path} 仍含残留 marker"

    def test_contents_are_split_correctly(self):
        out = parse_claude_output_to_files(self.RAW)
        assert "test_a" in out["tests/test_radar.py"]
        assert "test_b" not in out["tests/test_radar.py"]
        assert "test_b" in out["tests/test_output.py"]

    def test_warns(self, capsys):
        parse_claude_output_to_files(self.RAW)
        err = capsys.readouterr().err
        assert "残留 FILE marker" in err and "tests/test_radar.py" in err

    def test_three_way_pileup(self):
        raw = (
            "<!-- FILE: a.py -->\nA\n"
            "<!-- FILE: b.py -->\nB\n"
            "<!-- FILE: c.py -->\nC\n"
            "<!-- /FILE -->\n"
        )
        out = parse_claude_output_to_files(raw)
        assert set(out) == {"a.py", "b.py", "c.py"}
        assert out["a.py"].strip() == "A"
        assert out["c.py"].strip() == "C"


class TestTruncatedOutput:
    """失效模式 2：max_tokens 截断，末块无闭合标签 → 正则完全不匹配。

    CLI 路径拿不到 API 的 stop_reason，只能从「声明数 vs 恢复数」反推。
    """

    RAW = _block("a.py", "ok") + "<!-- FILE: b.py -->\ndef half("

    def test_truncated_block_is_not_silently_dropped(self, capsys):
        out = parse_claude_output_to_files(self.RAW)
        assert set(out) == {"a.py"}  # b.py 确实拿不到 —— 但必须有告警
        err = capsys.readouterr().err
        assert "声明了 2 个 FILE 块但只恢复出 1 个" in err
        assert "max_tokens" in err

    def test_complete_output_does_not_warn(self, capsys):
        parse_claude_output_to_files(_block("a.py", "ok") + _block("b.py", "ok"))
        assert "只恢复出" not in capsys.readouterr().err


class TestDuplicatePath:
    """失效模式 3：同一路径两次，后者静默覆盖（pain-radar 坑 4）。"""

    def test_warns_on_duplicate(self, capsys):
        raw = _block("radar.py", "V1") + _block("radar.py", "V2 longer content")
        out = parse_claude_output_to_files(raw)
        assert out["radar.py"].strip() == "V2 longer content"  # 保持既有行为
        err = capsys.readouterr().err
        assert "出现多次" in err and "radar.py" in err


class TestReExportIdentity:
    """common.py 必须真 re-export，不能再留第二份实现。

    2026-08-16 之前 common.py 自己留了逐字重复的一份，而 docstring 却声称
    是 re-export 聚合入口 —— 修一处漏一处，下次重构就把 bug 带回来。
    """

    def test_common_reexports_same_object(self):
        import common
        import output_parser
        assert common.parse_claude_output_to_files is (
            output_parser.parse_claude_output_to_files
        )
        assert common.write_output_atomic is output_parser.write_output_atomic

    def test_role_runner_uses_same_parser(self):
        import engine.role_runner as rr
        import output_parser
        assert rr.parse_claude_output_to_files is (
            output_parser.parse_claude_output_to_files
        )
