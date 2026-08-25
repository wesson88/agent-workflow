"""
.claude/tests/engine/test_injection_fingerprint.py — 注入指纹（P0.1）

立项：[[编排器改造-立项-2026-08-19]] §P0.1。

两半，缺一不可：

1. **解析器行为**（TestClassify / TestParseBlocks / TestFingerprint）——
   信封归类、字数、边界、降级标记。
2. **信封契约**（TestEnvelopeRegistry）—— 全仓扫描所有 `=== … ===` 字面量，
   与冻结注册表逐条对齐。新增/改动注入点必须登记，否则红。

第 2 条是这个文件存在的主要理由。指纹是被动解析 —— 一旦有人加了第 8 种
信封形状而不登记，指纹对那块内容**静默失明**，而这正是 P0.1 要治的病
（归因文档 9 条「机制存在但没接上，且无告警」）。用测试把「大家恰好都用
同一种信封」从巧合变成契约。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

from engine import llm as llm_mod
from engine.injection_fingerprint import (
    classify_envelope,
    fingerprint,
    format_unknown_warning,
    parse_blocks,
)


CLAUDE_ROOT = Path(__file__).resolve().parent.parent.parent   # .../.claude/


# ════════════════════════════════════════════════════════════════════
# 1. 信封契约：全仓扫描 vs 冻结注册表
# ════════════════════════════════════════════════════════════════════

_OPEN_RE = re.compile(r"===[ \t]\S")
_BRACE_RE = re.compile(r"\{[^{}]*\}")

# 处置类型：
#   emit    —— 真注入点，example 必须归类为 expect_kind
#   closer  —— 闭合标记（`=== END ===`），parse_blocks 视为块结束
#   consume —— 读信封的一侧（find/split），不产出
#   doc     —— docstring / 提示文案里的字面提及
#   wrapper —— llm._call_cli 的 stdin inline 包装。在 call_llm 采集指纹**之后**
#              才拼出来，因此永不进解析器；登记在此以免被当成漏网新形状
_REGISTRY: tuple[tuple[str, str, str, str, str], ...] = (
    # (相对路径, 归一模板, 处置, 期望 kind, 渲染样例)
    ("engine/ability_loader.py", "=== {} ===", "emit", "rule_ref",
     "=== [[F-技术主管#6. 可用技能索引]] ==="),
    ("engine/ability_loader.py", "=== Skill (wikilink:[[{}]] · full) ===", "emit", "skill_wikilink",
     "=== Skill (wikilink:[[M1-频谱能量分配]] · full) ==="),
    # 已退役：`engine/role_loader.py` 的 `=== Skill: {vault相对路径} ===`（静态
    # skill_refs）。2026-08-25 随字段废弃拆除产出点，故注册表里不再有它 ——
    # 若它回来了，`test_scan_matches_registry` 会以「扫到未注册的产出点」报红。
    ("engine/skill_trigger.py", "=== Skill (auto-trigger:{} · {}): [[{}]] ===", "emit", "skill_trigger",
     "=== Skill (auto-trigger:keyword · full): [[M1-频谱能量分配]] ==="),
    ("skills/prompt_builder.py", "=== Skill: [[{}]] ===", "emit", "skill_task",
     "=== Skill: [[F-模块拆分]] ==="),
    ("skills/dev_backend/main.py", "=== Skill 引用: [[{}]]", "emit", "skill_cite",
     "=== Skill 引用: [[F1-fetch响应检查]] (F1-fetch响应检查.md) ==="),
    ("skills/dev_frontend/main.py", "=== Skill 引用: [[{}]]", "emit", "skill_cite",
     "=== Skill 引用: [[F1-fetch响应检查]] (F1-fetch响应检查.md) ==="),
    ("skills/input_reader.py", "=== {} ===", "emit", "input_file",
     "=== 模块清单.md ==="),
    ("skills/common.py", "=== {} ===", "emit", "input_file",
     "=== 模块清单.md ==="),
    ("skills/archivist/main.py", "=== {} ===", "emit", "input_file",
     "=== 复盘-2026-08-19.md ==="),
    ("skills/graduator/main.py", "=== 角色-{}.md ===", "emit", "input_file",
     "=== 角色-技术主管.md ==="),
    ("skills/technical_lead/main.py", "=== {}（仅末轮决议） ===", "emit", "input_file",
     "=== 讨论日志.md（仅末轮决议） ==="),
    ("skills/archivist/main.py", "=== END ===", "closer", "", "=== END ==="),
    ("skills/graduator/main.py", "=== END ===", "closer", "", "=== END ==="),
    ('engine/ability_loader.py', '=== Skill")', "consume", "", ""),
    ("engine/ability_loader.py", "=== ... ===", "doc", "", ""),
    ("engine/ability_loader.py", "=== [[产物schema#7. ...]] ===", "doc", "", ""),
    ("engine/role_runner.py", "=== 文件名 ===", "doc", "", ""),
    ("skills/archivist/main.py", "=== 文件名 ===", "doc", "", ""),
    ("engine/llm.py", "=== 用户输入 ===", "wrapper", "", ""),
    ("engine/llm.py", "=== 系统指令（必须严格遵守，覆盖默认助手行为）===", "wrapper", "", ""),
)


def _scan_envelope_sites() -> set[tuple[str, str]]:
    """扫 engine/ + skills/ 全部 .py，返回 {(相对路径, 归一模板)}。

    归一 = 把 f-string 里的 `{expr}` 塞成 `{}`，只留形状。变量改名不动模板，
    新增形状必动 —— 这是本注册表想要的敏感度。
    """
    found: set[tuple[str, str]] = set()
    for sub in ("engine", "skills"):
        for p in sorted((CLAUDE_ROOT / sub).rglob("*.py")):
            if "__pycache__" in p.parts or p.name == "injection_fingerprint.py":
                continue
            rel = p.relative_to(CLAUDE_ROOT).as_posix()
            for line in p.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("#") or not _OPEN_RE.search(line):
                    continue
                for m in re.finditer(r"===[ \t]", line):
                    seg = line[m.start():]
                    close = re.search(r"[ \t]===", seg[3:])
                    seg = seg[:3 + close.end()] if close else seg.split("\\n")[0].rstrip("\"' ")
                    found.add((rel, _BRACE_RE.sub("{}", seg)))
    return found


class TestEnvelopeRegistry:
    """信封形状 = 受测契约，不是巧合。"""

    def test_scan_matches_registry(self):
        found = _scan_envelope_sites()
        registered = {(r[0], r[1]) for r in _REGISTRY}
        new = found - registered
        gone = registered - found
        assert not new, (
            "发现未登记的 `=== … ===` 信封形状：\n  "
            + "\n  ".join(f"{p}  →  {t!r}" for p, t in sorted(new))
            + "\n\n注入指纹是被动解析：未登记的形状 → 该块内容在 audit.jsonl 里"
              "静默消失。请二选一：\n"
              "  (a) 复用既有信封形状（首选，见 injection_fingerprint 模块 docstring 的表）\n"
              "  (b) 在 injection_fingerprint.classify_envelope 加判别分支，"
              "并把这一行登记进本文件的 _REGISTRY"
        )
        assert not gone, (
            "注册表里有已消失的信封形状（注入点被删/改而注册表未同步）：\n  "
            + "\n  ".join(f"{p}  →  {t!r}" for p, t in sorted(gone))
        )

    @pytest.mark.parametrize(
        "path,expect_kind,example",
        [(r[0], r[3], r[4]) for r in _REGISTRY if r[2] == "emit"],
        ids=[f"{r[0]}::{r[3]}" for r in _REGISTRY if r[2] == "emit"],
    )
    def test_registered_emitters_classify(self, path, expect_kind, example):
        """每个注入点的真实渲染形态都必须归类到声明的 kind（不能落 unknown）。"""
        label = re.match(r"^===[ \t]*(.*?)[ \t]*===$", example).group(1)
        got = classify_envelope(label)
        assert got["kind"] == expect_kind, f"{path}: {example!r} → {got}"

    def test_closers_are_not_blocks(self):
        text = "=== a.md ===\nbody\n=== END ===\ntrailing noise\n"
        blocks = parse_blocks(text)
        assert [b["kind"] for b in blocks] == ["input_file"]
        assert blocks[0]["chars"] == len("body")


# ════════════════════════════════════════════════════════════════════
# 2. 归类
# ════════════════════════════════════════════════════════════════════

class TestClassify:
    def test_auto_trigger_carries_tier_and_reason(self):
        got = classify_envelope("Skill (auto-trigger:keyword · pointer): [[M3-Plate与Pre-delay]]")
        assert got == {
            "kind": "skill_trigger", "name": "M3-Plate与Pre-delay",
            "tier": "pointer", "reason": "keyword",
        }

    def test_skill_path_form_is_unknown_not_a_dead_kind(self):
        """`=== Skill: X ===` 里 X 是 wikilink → skill_task；是路径 → unknown。

        路径形态原属 `skill_ref`（role_loader 的静态 skill_refs），2026-08-25
        随字段废弃拆除产出点。**故意降为 unknown 而不是删掉整个分支**：
        unknown 会在 call_llm 侧打 stderr + audit warn，所以旧形态一旦复活
        （vault 回滚 / 旧脚本 / 误改）会立刻被喊出来；若归给一个已不存在的
        kind，audit 里就会出现一条查不到产出点的记录 —— 那正是本模块开头
        「宁可报看不懂也不默默归错类」要防的事。
        """
        assert classify_envelope("Skill: [[F-模块拆分]]")["kind"] == "skill_task"
        assert classify_envelope("Skill: 20-知识/角色技能/se/x.md")["kind"] == "unknown"
        # unknown 的 name 保留整条 label，便于从 audit 反查是谁写的
        assert classify_envelope("Skill: 20-知识/x.md")["name"] == "Skill: 20-知识/x.md"

    def test_skill_ref_kind_fully_retired(self):
        """`skill_ref` 不在任何 kind 集合里 —— 防"删了产出点忘了删枚举"。"""
        from engine.injection_fingerprint import SKILL_KINDS, ALL_KINDS
        assert "skill_ref" not in SKILL_KINDS
        assert "skill_ref" not in ALL_KINDS

    def test_rule_ref_section_vs_whole(self):
        assert classify_envelope("[[F-技术主管#6. 索引]]")["tier"] == "section"
        assert classify_envelope("[[F-技术主管]]")["tier"] == "whole"

    def test_unknown_shape(self):
        assert classify_envelope("用户输入")["kind"] == "unknown"
        assert classify_envelope("某种全新格式")["kind"] == "unknown"

    def test_filename_needs_extension(self):
        """无扩展名的短语不算输入文件 —— 否则新形状会被静默吞成 input_file。"""
        assert classify_envelope("模块清单.md")["kind"] == "input_file"
        assert classify_envelope("模块清单")["kind"] == "unknown"


# ════════════════════════════════════════════════════════════════════
# 3. 分块 / 字数 / 降级标记
# ════════════════════════════════════════════════════════════════════

class TestParseBlocks:
    def test_chars_counts_payload_only(self):
        blocks = parse_blocks("=== a.md ===\n12345\n=== b.md ===\n678\n")
        assert [(b["name"], b["chars"]) for b in blocks] == [("a.md", 5), ("b.md", 3)]

    def test_empty_payload_flagged(self):
        """信封在、正文空 = 最典型的沉默失效形态。"""
        blocks = parse_blocks("=== Skill: [[X]] ===\n\n=== a.md ===\nbody\n")
        assert blocks[0]["flags"] == ["empty"]
        assert "flags" not in blocks[1]

    def test_truncation_marker_flagged(self):
        blocks = parse_blocks("=== a.md ===\nhead\n\n⚠️ [总量截断] 已达上限\n")
        assert blocks[0]["flags"] == ["truncated"]

    def test_per_skill_truncation_flagged(self):
        blocks = parse_blocks(
            "=== Skill (auto-trigger:keyword · full): [[M1-频谱能量分配]] ===\n"
            "正文\n⚠️ [截断警告] 单张超限\n"
        )
        assert blocks[0]["kind"] == "skill_trigger"
        assert blocks[0]["flags"] == ["truncated"]

    def test_bare_closer_ends_block(self):
        blocks = parse_blocks("=== a.md ===\nbody\n===\nnot in any block\n")
        assert len(blocks) == 1 and blocks[0]["chars"] == len("body")

    def test_tail_flag_marks_upper_bound(self):
        """段尾未闭合的块，chars 会把后续框架文案算进来 → 标 tail 说明是上界。

        2026-08-24 首次真链路比对靠这条差异暴露：离线基线只喂到 skill 段结束
        （3028），真链路 user_prompt 后面还有 939 字产出要求（3967）。
        """
        blocks = parse_blocks("=== a.md ===\nbody\n\n## 产出要求\n后面这些不属于 a.md\n")
        assert blocks[0]["tail"] is True
        assert blocks[0]["chars"] > len("body")

    def test_no_tail_when_properly_closed(self):
        blocks = parse_blocks("=== a.md ===\nbody\n===\n## 产出要求\n无关正文\n")
        assert "tail" not in blocks[0]
        assert blocks[0]["chars"] == len("body")

    def test_only_last_block_gets_tail(self):
        blocks = parse_blocks("=== a.md ===\nA\n=== b.md ===\nB\n尾部\n")
        assert "tail" not in blocks[0]
        assert blocks[1]["tail"] is True

    def test_no_envelope_returns_empty(self):
        assert parse_blocks("普通正文，没有信封\n") == []
        assert parse_blocks("") == []


class TestFingerprint:
    # static 段**没有信封**。这不是省事，是 P0.1 的实测结论：9/9 角色的
    # system prompt 注入恒为 0 chars —— 唯一声称走 static 的静态 skill_refs
    # 实测 0/14 生效，已于 2026-08-25 废弃拆除。本 fixture 因此兼作回归守卫：
    # 哪天 static 里又冒出信封，test_static_segment_has_no_envelopes 会红。
    SYS = "## 角色：技术主管\n主体正文若干，无任何 === 信封 ===\n"
    USER = (
        "=== [[F-技术主管#6. 可用技能索引]] ===\n规则章节\n\n"
        "=== Skill (auto-trigger:keyword · full): [[M1-频谱能量分配]] ===\n"
        "细则正文若干\n\n"
        "=== Skill (auto-trigger:keyword · pointer): [[M2-人声慢启动压缩]] ===\n"
        "只有指针\n\n"
        "=== 模块清单.md ===\n上游产物\n"
    )

    def _fp(self):
        return fingerprint({"static": self.SYS, "user": self.USER})

    def test_static_segment_has_no_envelopes(self):
        """「skill 进的是 system 还是 user」—— 立项要回答的问题，实测答案：全在 user。"""
        fp = self._fp()
        assert {b["seg"] for b in fp["blocks"]} == {"user"}

    def test_segment_attribution(self):
        fp = fingerprint({"static": self.SYS, "user": self.USER})
        by_seg = {b["name"]: b["seg"] for b in fp["blocks"]}
        assert by_seg["M1-频谱能量分配"] == "user"
        assert by_seg["模块清单.md"] == "user"

    def test_counts_and_chars_aggregate(self):
        fp = self._fp()
        assert fp["counts"] == {"rule_ref": 1, "skill_trigger": 2, "input_file": 1}
        assert fp["chars"]["skill_trigger"] == len("细则正文若干") + len("只有指针")
        assert fp["unknown"] == 0
        assert "degraded" not in fp

    def test_tier_distinguishes_pointer_from_full(self):
        fp = self._fp()
        tiers = {b["name"]: b["tier"] for b in fp["blocks"] if b["kind"] == "skill_trigger"}
        assert tiers == {"M1-频谱能量分配": "full", "M2-人声慢启动压缩": "pointer"}

    def test_degraded_surfaced(self):
        """降级标记用**还有产出点**的那两个（截断）。

        原用例喂 `[SKILL MISSING:`，那是 `_resolve_skill_refs` 的标记，随
        skill_refs 废弃一并移除 —— 留着测一个没人会写的标记等于测空气。
        """
        fp = fingerprint({"user":
            "=== Skill (auto-trigger:keyword · full): [[M1-频谱能量分配]] ===\n"
            "正文\n\n⚠️ [总量截断] 已达上限\n"
        })
        assert fp["degraded"] == [
            {"kind": "skill_trigger", "name": "M1-频谱能量分配", "flags": ["truncated"]}
        ]

    def test_retired_skill_ref_shape_counts_as_unknown(self):
        """旧的静态 skill_refs 形态若复活 → 计入 unknown，不静默通过。"""
        fp = fingerprint({"static": "=== Skill: 20-知识/角色技能/se/x.md ===\n技能全文\n"})
        assert fp["unknown"] == 1
        assert fp["counts"] == {"unknown": 1}


# ════════════════════════════════════════════════════════════════════
# 4. 接线：llm.py 采集 → audit.jsonl
# ════════════════════════════════════════════════════════════════════

@pytest.fixture
def audit_path(tmp_path, monkeypatch) -> Path:
    p = tmp_path / "audit.jsonl"
    monkeypatch.setattr(llm_mod, "_AUDIT_JSONL_PATH", p)
    return p


def _events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


class TestLlmWiring:
    def test_collect_from_four_segments(self, audit_path):
        fp = llm_mod._injection_fingerprint(
            "=== a.md ===\nS\n",
            "=== [[F-x#1. y]] ===\nD1\n",
            "=== [[F-z#2. w]] ===\nD2\n",
            "=== c.md ===\nU\n",
        )
        assert {b["seg"] for b in fp["blocks"]} == {
            "static", "dynamic_own", "dynamic_upstream", "user",
        }
        assert fp["counts"] == {"rule_ref": 2, "input_file": 2}

    def test_unknown_envelope_warns_to_stderr_and_audit(self, audit_path, capsys):
        fp = llm_mod._injection_fingerprint("=== 某种全新格式 ===\nbody\n", "", "", "")
        assert fp["unknown"] == 1
        assert "无法归类" in capsys.readouterr().err
        warns = [e for e in _events(audit_path) if e["reason"] == "injection_envelope_unknown"]
        assert len(warns) == 1
        assert warns[0]["labels"] == ["某种全新格式"]
        assert warns[0]["level"] == "warn"

    def test_collector_failure_degrades_to_none(self, audit_path, capsys, monkeypatch):
        """仪表本身不许成为新故障点。"""
        import engine.injection_fingerprint as fp_mod
        monkeypatch.setattr(
            fp_mod, "fingerprint", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        assert llm_mod._injection_fingerprint("=== a.md ===\nx\n", "", "", "") is None
        assert "指纹采集失败" in capsys.readouterr().err

    def test_call_llm_passes_injection_to_adapter(self, audit_path, monkeypatch):
        """call_llm 是唯一采集点 → 三条轨道都必须收到 injection。"""
        seen: dict = {}

        def _fake_cli(cli_cfg, system_prompt, user_prompt, print_stream, **kw):
            seen.update(kw)
            return "ok"

        monkeypatch.setattr(llm_mod, "_call_cli", _fake_cli)
        monkeypatch.setattr(llm_mod, "_resolve_track", lambda cfg: "cli")
        monkeypatch.setattr(
            llm_mod, "get_provider",
            lambda name: {"mode": "cli_only", "cli": {"path": "claude"}, "context_window": 200000},
        )
        llm_mod.call_llm(
            "=== Skill (wikilink:[[F-模块拆分]] · full) ===\n技能全文\n",
            "=== 模块清单.md ===\n上游\n",
            model="fake", print_stream=False, role_name="测试角色",
        )
        assert seen["injection"]["counts"] == {"skill_wikilink": 1, "input_file": 1}

    def test_cli_llm_call_event_carries_injection(self, audit_path, tmp_path):
        """端到端：真子进程走完 _call_cli，injection 落进 llm_call 事件。

        复用 test_f7_cli_heartbeat 的假 CLI 手法（stream-json 吐两行即退）。
        """
        fake_cli = tmp_path / "fake_cli.py"
        fake_cli.write_text(
            "import sys\n"
            "sys.stdin.read()\n"
            'print(\'{"type": "assistant", "message": {"content": '
            '[{"type": "text", "text": "PONG"}]}}\')\n'
            'print(\'{"type": "result", "result": "PONG", '
            '"usage": {"input_tokens": 7, "output_tokens": 3}}\')\n',
            encoding="utf-8",
        )
        injection = fingerprint({"user": "=== Skill: [[F-模块拆分]] ===\n正文\n"})
        out = llm_mod._call_cli(
            {
                "path": sys.executable,
                "extra_args": [str(fake_cli)],
                "output_format": "stream-json",
                "use_system_prompt_flag": False,
            },
            "system", "ping", print_stream=False,
            role_name="测试角色", model_name="fake-model", injection=injection,
        )
        assert out == "PONG"
        calls = [e for e in _events(audit_path) if e.get("reason") == "llm_call"]
        assert len(calls) == 1
        assert calls[0]["injection"]["counts"] == {"skill_task": 1}
        assert calls[0]["injection"]["blocks"][0]["name"] == "F-模块拆分"

    def test_no_injection_key_when_none(self, audit_path, tmp_path):
        """injection=None（采集失败/老调用方）不得往事件里塞空键。"""
        fake_cli = tmp_path / "fake_cli.py"
        fake_cli.write_text(
            "import sys\nsys.stdin.read()\n"
            'print(\'{"type": "result", "result": "X"}\')\n',
            encoding="utf-8",
        )
        llm_mod._call_cli(
            {
                "path": sys.executable,
                "extra_args": [str(fake_cli)],
                "output_format": "stream-json",
                "use_system_prompt_flag": False,
            },
            "system", "ping", print_stream=False, injection=None,
        )
        calls = [e for e in _events(audit_path) if e.get("reason") == "llm_call"]
        assert "injection" not in calls[0]


class TestWarningText:
    def test_message_names_both_remedies(self):
        fp = fingerprint({"user": "=== 新格式甲 ===\nx\n=== 新格式乙 ===\ny\n"})
        msg = format_unknown_warning(fp)
        assert "新格式甲" in msg and "新格式乙" in msg
        assert "classify_envelope" in msg and "_REGISTRY" in msg


# ════════════════════════════════════════════════════════════════════
# 5. 真实产出点回归：拿真加载器的输出过一遍解析器
# ════════════════════════════════════════════════════════════════════

class TestAgainstRealProducers:
    """不是造样例，而是让真产出点跑一遍 —— 模板漂移时这里先红。"""

    def test_skill_trigger_head_literal_unchanged(self):
        """skill_trigger 的 head 拼法是最复杂的一种（reason + tier + stem 三段），
        用字面断言锁住 —— 模板漂移时这里先红，而不是等 audit 里悄悄多出 unknown。"""
        src = (CLAUDE_ROOT / "engine" / "skill_trigger.py").read_text(encoding="utf-8")
        assert 'f"=== Skill (auto-trigger:{m.reason} · {tier}): [[{m.path.stem}]] ==="' in src

    def test_read_input_files_real_output(self, tmp_path):
        """input_reader.read_input_files 真跑一次（含截断分支）。"""
        from input_reader import read_input_files

        f = tmp_path / "模块清单.md"
        f.write_text("A" * 200, encoding="utf-8")
        text = read_input_files([f], max_chars_per_file=50)
        blocks = parse_blocks(text)
        assert len(blocks) == 1
        assert blocks[0]["kind"] == "input_file"
        assert blocks[0]["name"] == "模块清单.md"
        assert blocks[0]["flags"] == ["truncated"], (
            "截断标记必须被指纹抓到 —— 否则「输入被裁掉一半」这件事在 audit 里不可见"
        )

    def test_retired_skill_ref_emitter_stays_gone(self):
        """role_loader 不得再产出静态 skill_refs 的信封与降级标记。

        与上面的 literal 断言同款思路，但方向相反：那条锁「模板不许漂」，
        这条锁「产出点不许回来」。2026-08-25 拆除后，若谁把 inline 逻辑改回去，
        `test_scan_matches_registry` 会因未注册而红、本条会因字面残留而红 ——
        两道都指向同一个动作，是刻意的：这不是 lint 洁癖，而是那条路径实测
        0/14 生效，复活即等于重建一个沉默失效。
        """
        src = (CLAUDE_ROOT / "engine" / "role_loader.py").read_text(encoding="utf-8")
        # 只看**代码行**：注释里为留档而复述旧形态是允许的，甚至是必要的
        # （废弃依据就写在那儿）。把注释一起断言会逼人删掉历史，本末倒置。
        code = "\n".join(
            ln for ln in src.splitlines()
            if not ln.lstrip().startswith("#")
        )
        for literal in (
            '"\\n\\n## 引用技能（来自 skill_refs）\\n\\n"',
            "[SKILL MISSING:",
            "[SKILL READ ERROR:",
            "def _resolve_skill_refs",
        ):
            assert literal not in code, f"静态 skill_refs 产出点复活了：{literal}"

    def test_deprecated_field_still_declared_gets_warned(self, tmp_path, capsys):
        """字段还留在 frontmatter → 必须喊，不许静默忽略。

        这是废弃动作本身的验收点：一个仍被声明、却已无消费者的字段，正是
        本项目在治的「沉默失效」形态（第 10 例就是 skill_refs 自己）。
        """
        from engine.role_loader import _warn_deprecated_skill_refs

        note = tmp_path / "角色-测试.md"
        _warn_deprecated_skill_refs({"skill_refs": ["20-知识/a.md", "20-知识/b.md"]}, note)
        err = capsys.readouterr().err
        assert "角色-测试.md" in err and "2 条" in err and "不会生效" in err

        _warn_deprecated_skill_refs({"skill_refs": []}, note)
        _warn_deprecated_skill_refs({}, note)
        assert capsys.readouterr().err == "", "空 [] 与缺字段都不该告警（否则 27 个角色天天刷屏）"
