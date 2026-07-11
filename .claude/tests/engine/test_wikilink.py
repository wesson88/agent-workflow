"""
tests/engine/test_wikilink.py — wikilink resolver/expander 单测

四层覆盖：parse / resolve / load / expand
依赖：conftest.py 已把 .claude/ 加入 sys.path
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from engine import wikilink as wl_mod
from engine.wikilink import (
    Wikilink, parse_wikilinks, resolve_target, load_wikilink,
    expand_wikilinks, DuplicateStemError, invalidate_cache,
)


# ── 通用 fixture：临时 vault + cache 失效 ─────────────────
@pytest.fixture
def tmp_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """造一个临时 vault 并指向 wikilink 模块的 VAULT_ROOT。
    每次 fixture 调用都会 invalidate_cache()，避免跨测试串扰。
    """
    monkeypatch.setattr(wl_mod, "VAULT_ROOT", tmp_path)
    invalidate_cache()
    yield tmp_path
    invalidate_cache()


def _write(p: Path, content: str = "# placeholder\n") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


# ── 1. parse_wikilinks（纯字符串）────────────────────────
class TestParse:
    def test_basic_single(self):
        wls = parse_wikilinks("see [[X]] please")
        assert len(wls) == 1
        wl = wls[0]
        assert wl.target == "X"
        assert wl.section is None
        assert wl.alias is None
        assert wl.raw == "[[X]]"

    def test_section_only(self):
        wls = parse_wikilinks("[[X#§3.2 失败模式]]")
        assert wls[0].target == "X"
        assert wls[0].section == "§3.2 失败模式"
        assert wls[0].alias is None

    def test_alias_only(self):
        wls = parse_wikilinks("[[X|short]]")
        assert wls[0].target == "X"
        assert wls[0].section is None
        assert wls[0].alias == "short"

    def test_section_and_alias(self):
        wls = parse_wikilinks("[[X#part|alias]]")
        assert wls[0].target == "X"
        assert wls[0].section == "part"
        assert wls[0].alias == "alias"

    def test_full_path(self):
        wls = parse_wikilinks("[[20-知识/角色技能/B5-空集守卫]]")
        assert wls[0].target == "20-知识/角色技能/B5-空集守卫"

    def test_multiple_in_one_text(self):
        wls = parse_wikilinks("第一 [[A]] 第二 [[B#sec]] 第三 [[C|alias]]")
        assert [w.target for w in wls] == ["A", "B", "C"]
        assert wls[1].section == "sec"
        assert wls[2].alias == "alias"

    def test_embedded_keeps_bang_in_raw(self):
        wls = parse_wikilinks("see ![[X]] inline")
        assert wls[0].target == "X"
        assert wls[0].raw == "![[X]]"

    def test_block_ref_degrades(self, capsys):
        """`[[X#^id]]` 不支持 block ref；降级为普通 section，并打 warn。"""
        wls = parse_wikilinks("[[X#^block-1]]")
        assert wls[0].section == "^block-1"
        captured = capsys.readouterr()
        assert "block reference" in captured.err

    def test_empty_inner_skipped(self):
        wls = parse_wikilinks("text [[]] more")
        assert wls == []

    def test_newline_breaks_link(self):
        """跨行 wikilink 不应被识别（防止把段落误解析）。"""
        wls = parse_wikilinks("[[X\nY]]")
        assert wls == []

    def test_span_records_correct_position(self):
        text = "hello [[X]] world"
        wls = parse_wikilinks(text)
        s, e = wls[0].span
        assert text[s:e] == "[[X]]"


# ── 2. resolve_target（vault stem 索引）──────────────────
class TestResolve:
    def test_full_path_hit(self, tmp_vault: Path):
        _write(tmp_vault / "00-系统/角色基因/角色-后端工程师.md")
        p = resolve_target("00-系统/角色基因/角色-后端工程师")
        assert p is not None
        assert p.name == "角色-后端工程师.md"

    def test_full_path_with_md_suffix(self, tmp_vault: Path):
        _write(tmp_vault / "00-系统/规则/技术栈.md")
        p = resolve_target("00-系统/规则/技术栈.md")
        assert p is not None

    def test_full_path_miss_returns_none(self, tmp_vault: Path):
        assert resolve_target("does/not/exist") is None

    def test_full_path_out_of_vault_returns_none(self, tmp_vault: Path):
        # 越界尝试 → None（不抛错，由调用方处理）
        assert resolve_target("../../etc/passwd") is None

    def test_stem_unique_hit(self, tmp_vault: Path):
        _write(tmp_vault / "20-知识/角色技能/后端工程师/B5-空集守卫.md")
        p = resolve_target("B5-空集守卫")
        assert p is not None
        assert p.stem == "B5-空集守卫"

    def test_stem_miss(self, tmp_vault: Path):
        _write(tmp_vault / "20-知识/X.md")  # stem 是 X
        assert resolve_target("Nonexistent") is None

    def test_stem_duplicate_raises(self, tmp_vault: Path):
        _write(tmp_vault / "20-知识/A/dup.md")
        _write(tmp_vault / "20-知识/B/dup.md")
        with pytest.raises(DuplicateStemError) as ei:
            resolve_target("dup")
        msg = str(ei.value)
        assert "dup" in msg
        assert "重名" in msg

    def test_project_outputs_excluded_from_stem_index(self, tmp_vault: Path):
        """`10-项目/<project>/PRD.md` 的 stem 不进索引，必须用完整路径。"""
        _write(tmp_vault / "10-项目/proj-a/PRD.md")
        _write(tmp_vault / "10-项目/proj-b/PRD.md")
        # 两个 PRD 都没进 stem 索引：resolve 应该返回 None（而不是 raise DuplicateStem）
        assert resolve_target("PRD") is None
        # 但完整路径可以解析
        p = resolve_target("10-项目/proj-a/PRD")
        assert p is not None
        assert p.parent.name == "proj-a"

    def test_runtime_state_excluded(self, tmp_vault: Path):
        _write(tmp_vault / "00-系统/.runtime-state/技术主管.backend_done")
        # 这是 .md 文件之外的扩展名，本来不在索引；保险起见再确认
        assert resolve_target("技术主管.backend_done") is None

    def test_temp_dir_excluded(self, tmp_vault: Path):
        _write(tmp_vault / "99-临时/draft.md")
        assert resolve_target("draft") is None

    def test_single_domain_rule_kept_in_stem_index(self, tmp_vault: Path):
        """P0 修：`00-系统/规则/<domain>/<x>.md` 若无跨域同名，bare stem 可解析。

        回归 [[音乐L3实战-非SE机制差异-2026-07-11#R1]]：原实现无差别排除所有
        domain rule 子目录，导致音乐 `创作简报.schema.md` 等 rule_refs `[[X#...]]` 全 `未解析`。
        """
        p = _write(tmp_vault / "00-系统/规则/music/创作简报.schema.md")
        got = resolve_target("创作简报.schema")
        assert got == p

    def test_cross_domain_adapter_excluded_from_stem_index(self, tmp_vault: Path):
        """跨域同名 adapter（≥ 2 domain 都有同 stem）仍从 stem 索引排除。

        保留原设计意图：`复盘者-视角.md` 在 music/ 和 se/ 都有，bare stem 无法解析，
        必须用完整路径 `[[00-系统/规则/<domain>/复盘者-视角]]` 消歧。
        """
        _write(tmp_vault / "00-系统/规则/music/复盘者-视角.md")
        _write(tmp_vault / "00-系统/规则/se/复盘者-视角.md")
        assert resolve_target("复盘者-视角") is None

    def test_domain_rule_plus_non_rule_namesake_kept(self, tmp_vault: Path):
        """domain rule 下有 X.md，同时 vault 别处有另一份 X.md → 走 DuplicateStem 治理路径。

        不因为 domain rule 匹配就静默排除；命名规则要求 stem 全 vault 唯一，
        重名由 resolve_target 抛 DuplicateStemError 强制人工介入。
        """
        _write(tmp_vault / "00-系统/规则/music/X.md")
        _write(tmp_vault / "20-知识/X.md")
        with pytest.raises(DuplicateStemError):
            resolve_target("X")


# ── 3. load_wikilink（章节抽取 + 截断）────────────────────
class TestLoad:
    def test_load_full_content(self, tmp_vault: Path):
        p = _write(tmp_vault / "X.md", "# 标题\n\n正文 abc\n")
        wl = Wikilink(raw="[[X]]", target="X", section=None, alias=None, span=(0, 0))
        out = load_wikilink(wl, p)
        assert "正文 abc" in out

    def test_load_section_hit(self, tmp_vault: Path):
        content = (
            "# 总览\n通用内容\n\n"
            "## 第二章\n第二章内容\n\n"
            "## 第三章\n第三章内容\n"
        )
        p = _write(tmp_vault / "Doc.md", content)
        wl = Wikilink(raw="[[Doc#第二章]]", target="Doc", section="第二章",
                     alias=None, span=(0, 0))
        out = load_wikilink(wl, p)
        assert "第二章内容" in out
        assert "第三章内容" not in out

    def test_load_section_miss_falls_back_to_full(self, tmp_vault: Path, capsys):
        p = _write(tmp_vault / "Doc.md", "# 标题\n正文\n")
        wl = Wikilink(raw="[[Doc#不存在]]", target="Doc", section="不存在",
                     alias=None, span=(0, 0))
        out = load_wikilink(wl, p)
        assert "正文" in out
        captured = capsys.readouterr()
        assert "未命中" in captured.err

    def test_load_truncates_long_content(self, tmp_vault: Path):
        p = _write(tmp_vault / "Big.md", "x" * 10_000)
        wl = Wikilink(raw="[[Big]]", target="Big", section=None, alias=None, span=(0, 0))
        out = load_wikilink(wl, p, max_chars=500)
        assert "截断" in out
        # 截断头部仍是原始内容
        assert out.startswith("x" * 500)


# ── 4. expand_wikilinks（主入口）─────────────────────────
class TestExpand:
    def test_filter_skip_records_reason(self, tmp_vault: Path):
        _write(tmp_vault / "X.md", "content")
        result = expand_wikilinks(
            "see [[X]]", tmp_vault,
            filter=lambda wl: False,
        )
        assert len(result.expansions) == 1
        assert result.expansions[0].reason == "filter_skip"
        assert result.expansions[0].content is None
        assert result.total_chars == 0

    def test_filter_true_expands(self, tmp_vault: Path):
        _write(tmp_vault / "X.md", "actual content here")
        result = expand_wikilinks(
            "see [[X]]", tmp_vault,
            filter=lambda wl: True,
        )
        assert len(result.expansions) == 1
        exp = result.expansions[0]
        assert exp.reason == "ok"
        assert exp.content is not None
        assert "actual content" in exp.content
        assert result.total_chars > 0

    def test_filter_target_prefix(self, tmp_vault: Path):
        _write(tmp_vault / "B5.md", "skill B5")
        _write(tmp_vault / "Other.md", "not a skill")
        result = expand_wikilinks(
            "see [[B5]] and [[Other]]", tmp_vault,
            filter=lambda wl: re.match(r"^B\d+", wl.target) is not None,
        )
        reasons = [(e.wikilink.target, e.reason) for e in result.expansions]
        assert ("B5", "ok") in reasons
        assert ("Other", "filter_skip") in reasons

    def test_unresolved_warn(self, tmp_vault: Path, capsys):
        result = expand_wikilinks(
            "[[NoSuch]]", tmp_vault,
            filter=lambda wl: True,
            on_unresolved="warn",
        )
        assert result.expansions[0].reason == "unresolved"
        assert result.unresolved == ["NoSuch"]
        assert "未解析" in capsys.readouterr().err

    def test_unresolved_raise(self, tmp_vault: Path):
        with pytest.raises(FileNotFoundError):
            expand_wikilinks(
                "[[NoSuch]]", tmp_vault,
                filter=lambda wl: True,
                on_unresolved="raise",
            )

    def test_unresolved_skip_no_warn(self, tmp_vault: Path, capsys):
        result = expand_wikilinks(
            "[[NoSuch]]", tmp_vault,
            filter=lambda wl: True,
            on_unresolved="skip",
        )
        assert result.expansions[0].reason == "unresolved"
        # skip 模式不打 warn
        assert "未解析" not in capsys.readouterr().err

    def test_duplicate_stem_raises_through(self, tmp_vault: Path):
        _write(tmp_vault / "a/dup.md")
        _write(tmp_vault / "b/dup.md")
        with pytest.raises(DuplicateStemError):
            expand_wikilinks(
                "[[dup]]", tmp_vault,
                filter=lambda wl: True,
            )

    def test_depth_zero_no_recurse(self, tmp_vault: Path):
        _write(tmp_vault / "A.md", "links to [[B]]")
        _write(tmp_vault / "B.md", "B content")
        result = expand_wikilinks(
            "[[A]]", tmp_vault,
            filter=lambda wl: True,
            max_depth=0,
        )
        # A 被展开 + B 触发 max_depth 标记
        reasons = {e.wikilink.target: e.reason for e in result.expansions}
        assert reasons["A"] == "ok"
        assert reasons["B"] == "max_depth"

    def test_depth_one_recurses(self, tmp_vault: Path):
        _write(tmp_vault / "A.md", "links to [[B]]")
        _write(tmp_vault / "B.md", "B content")
        result = expand_wikilinks(
            "[[A]]", tmp_vault,
            filter=lambda wl: True,
            max_depth=1,
        )
        reasons = {e.wikilink.target: e.reason for e in result.expansions}
        assert reasons["A"] == "ok"
        assert reasons["B"] == "ok"

    def test_cycle_detected(self, tmp_vault: Path):
        _write(tmp_vault / "A.md", "back to [[B]]")
        _write(tmp_vault / "B.md", "back to [[A]]")
        result = expand_wikilinks(
            "[[A]]", tmp_vault,
            filter=lambda wl: True,
            max_depth=3,
        )
        # 应触发 cycle，记录在 cycles_detected
        assert result.cycles_detected, "环未检测到"
        # 至少有一个 expansion 的 reason 是 cycle
        assert any(e.reason == "cycle" for e in result.expansions)

    def test_budget_exhausted(self, tmp_vault: Path):
        _write(tmp_vault / "A.md", "x" * 5000)
        _write(tmp_vault / "B.md", "y" * 5000)
        result = expand_wikilinks(
            "[[A]] [[B]]", tmp_vault,
            filter=lambda wl: True,
            max_chars_per_link=10_000,
            total_char_budget=1000,   # 严格预算
        )
        # 至少一个会因为预算被截短或标 budget
        reasons = [e.reason for e in result.expansions]
        # A 应该消费完预算，B 应该标 budget（或被截短到几乎为空）
        assert "budget" in reasons or sum(
            len(e.content or "") for e in result.expansions
        ) <= 1000 + 200  # 容忍截断提示语开销

    def test_full_path_wikilink_resolves(self, tmp_vault: Path):
        _write(tmp_vault / "10-项目/proj-a/PRD.md", "PRD A content")
        result = expand_wikilinks(
            "see [[10-项目/proj-a/PRD]]", tmp_vault,
            filter=lambda wl: True,
        )
        exp = result.expansions[0]
        assert exp.reason == "ok"
        assert "PRD A" in exp.content

    def test_project_output_stem_wikilink_unresolved(self, tmp_vault: Path, capsys):
        """裸 `[[PRD]]` 不应解析到 10-项目 下任何 PRD.md。"""
        _write(tmp_vault / "10-项目/proj-a/PRD.md", "PRD A")
        _write(tmp_vault / "10-项目/proj-b/PRD.md", "PRD B")
        result = expand_wikilinks(
            "see [[PRD]]", tmp_vault,
            filter=lambda wl: True,
        )
        assert result.expansions[0].reason == "unresolved"
