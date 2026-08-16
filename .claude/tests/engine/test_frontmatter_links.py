"""
test_frontmatter_links.py — 产物 frontmatter 链接字段规范化 + 体检

背景见 engine/frontmatter_links.py docstring 与 vault
[[待办清单严重项梳理-2026-08-16]] S1。

覆盖：
- A 类（多链接未加引号 / YAML 挂）→ 改写成 block list
- B 类（单链接未加引号 / 嵌套 list）→ 补引号，含 block list 项
- C 类（多链接塞一个字符串）→ 改写成 block list
- 安全边界：散文夹链接不改写（pain-radar 真实案例）
- 幂等 / 无 frontmatter / 只动第一个块
- 语义断言：规范化后 yaml.safe_load 真能拿到链接**列表**
- check_frontmatter：双 frontmatter 块（纸飞机真实案例）+ C 类残留
"""

from __future__ import annotations

import yaml

from engine.frontmatter_links import check_frontmatter, normalize_frontmatter_links


def _fm(content: str) -> dict:
    """取规范化后 frontmatter 的解析结果。"""
    head = content.split("---", 2)[1]
    return yaml.safe_load(head)


class TestCClassMultiLinkString:
    """C 类：`key: "[[a]], [[b]]"` 解析成一个字符串。"""

    def test_rewrites_to_block_list(self):
        raw = '---\ntype: composition\nupstream: "[[创作 vision]], [[词作]]"\n---\n正文\n'
        out, fixes = normalize_frontmatter_links(raw)
        assert len(fixes) == 1
        assert 'upstream:\n  - "[[创作 vision]]"\n  - "[[词作]]"' in out

    def test_semantic_result_is_a_list(self):
        """最关键的一条：改完之后 YAML 真的读出两个链接而不是一个字符串。"""
        raw = '---\nupstream: "[[a]], [[b]]"\n---\n'
        before = yaml.safe_load(raw.split("---", 2)[1])
        assert before["upstream"] == "[[a]], [[b]]"  # 错值：一个字符串

        out, _ = normalize_frontmatter_links(raw)
        assert _fm(out)["upstream"] == ["[[a]]", "[[b]]"]

    def test_three_links_and_chinese_separator(self):
        raw = "---\nupstream: '[[a]]、[[b]]，[[c]]'\n---\n"
        out, fixes = normalize_frontmatter_links(raw)
        assert _fm(out)["upstream"] == ["[[a]]", "[[b]]", "[[c]]"]
        assert len(fixes) == 1

    def test_real_case_xiguanshizi(self):
        """西关十字 曲作.md 实际错值（upstream + downstream 各一处）。"""
        raw = (
            "---\n"
            "type: composition\n"
            "project: 西关十字\n"
            'upstream: "[[创作 vision]], [[词作]]"\n'
            'downstream: "[[Suno-prompt]], [[角色-编曲]], [[角色-和声编写]]"\n'
            "---\n# 曲作\n"
        )
        out, fixes = normalize_frontmatter_links(raw)
        assert len(fixes) == 2
        fm = _fm(out)
        assert fm["upstream"] == ["[[创作 vision]]", "[[词作]]"]
        assert fm["downstream"] == ["[[Suno-prompt]]", "[[角色-编曲]]", "[[角色-和声编写]]"]
        assert fm["project"] == "西关十字"  # 其它字段不受影响


class TestAClassUnquotedMultiLink:
    """A 类：未加引号的多链接 —— 原文 YAML 直接解析失败。"""

    def test_original_fails_to_parse(self):
        raw = "---\nupstream: [[a]], [[b]], [[c]]\n---\n"
        try:
            yaml.safe_load(raw.split("---", 2)[1])
        except yaml.YAMLError:
            pass
        else:  # pragma: no cover
            raise AssertionError("前提失效：A 类原文应当解析失败")

    def test_rewrite_makes_it_parseable(self):
        raw = "---\nupstream: [[a]], [[b]], [[c]]\n---\n"
        out, fixes = normalize_frontmatter_links(raw)
        assert len(fixes) == 1
        assert _fm(out)["upstream"] == ["[[a]]", "[[b]]", "[[c]]"]


class TestBClassUnquotedSingleLink:
    """B 类：`key: [[a]]` 被读成嵌套 list。"""

    def test_original_is_nested_list(self):
        assert yaml.safe_load("upstream: [[a]]") == {"upstream": [["a"]]}

    def test_scalar_gets_quoted(self):
        out, fixes = normalize_frontmatter_links("---\nupstream: [[a]]\n---\n")
        assert _fm(out)["upstream"] == "[[a]]"
        assert len(fixes) == 1

    def test_block_list_item_gets_quoted(self):
        raw = "---\ndownstream:\n  - [[a]]\n  - [[b]]\n---\n"
        out, fixes = normalize_frontmatter_links(raw)
        assert len(fixes) == 2
        assert _fm(out)["downstream"] == ["[[a]]", "[[b]]"]


class TestSafetyBoundary:
    """只改**纯链接**的值 —— 夹带散文的一律不动。"""

    def test_prose_with_links_untouched(self):
        """pain-radar/PRD.md 真实案例：这是句子不是链接列表。"""
        raw = (
            "---\n"
            "source: 综合 [[business_brief]] + [[brainstorm-spider-tools]] v0.0–v0.3\n"
            "---\n"
        )
        out, fixes = normalize_frontmatter_links(raw)
        assert fixes == []
        assert out == raw

    def test_prose_between_links_untouched(self):
        raw = "---\nnote: 参考 [[a]] 与 [[b]] 的对照\n---\n"
        out, fixes = normalize_frontmatter_links(raw)
        assert fixes == []
        assert out == raw

    def test_non_link_values_untouched(self):
        raw = "---\ntype: composition\nversion: 1.1.0\ntags: [a, b]\n---\n"
        out, fixes = normalize_frontmatter_links(raw)
        assert fixes == []
        assert out == raw


class TestIdempotenceAndScope:
    def test_correct_form_is_untouched(self):
        raw = '---\nupstream: "[[a]]"\ndownstream:\n  - "[[b]]"\n---\n'
        out, fixes = normalize_frontmatter_links(raw)
        assert fixes == []
        assert out == raw

    def test_idempotent(self):
        raw = '---\nupstream: "[[a]], [[b]]"\n---\n'
        once, _ = normalize_frontmatter_links(raw)
        twice, fixes = normalize_frontmatter_links(once)
        assert fixes == []
        assert twice == once

    def test_no_frontmatter_untouched(self):
        raw = "# 标题\n\nupstream: [[a]], [[b]]\n"
        out, fixes = normalize_frontmatter_links(raw)
        assert fixes == []
        assert out == raw

    def test_body_content_untouched(self):
        """正文里的 `---` 分隔线与相似文本不被当 frontmatter 处理。"""
        raw = (
            '---\nupstream: "[[a]], [[b]]"\n---\n'
            "# 正文\n\n---\n\nupstream: [[c]], [[d]]\n"
        )
        out, fixes = normalize_frontmatter_links(raw)
        assert len(fixes) == 1
        assert "upstream: [[c]], [[d]]" in out  # 正文原样保留

    def test_body_preserved_exactly(self):
        body = "# 曲作\n\n## 1. 段落\n\n内容 [[a]], [[b]] 不动\n"
        raw = f'---\nupstream: "[[a]], [[b]]"\n---\n{body}'
        out, _ = normalize_frontmatter_links(raw)
        assert out.endswith(body)


class TestCheckFrontmatter:
    def test_clean_file_reports_nothing(self):
        raw = '---\ntype: composition\nupstream: "[[a]]"\n---\n正文\n'
        assert check_frontmatter(raw) == []

    def test_double_frontmatter_detected(self):
        """纸飞机 Suno-prompt.md 真实结构：第 2 块整个落进正文。"""
        raw = (
            "---\nstatus: finalized\nversion: v5\n---\n"
            "---\nproject: 纸飞机\nrole: 作曲\n---\n"
        )
        problems = check_frontmatter(raw)
        assert any("第 2 个 frontmatter 块" in p for p in problems)

    def test_c_class_residue_reported(self):
        """散文夹多链接不自动改写，但要告警出来让人判断。"""
        raw = "---\nsource: 综合 [[a]] + [[b]] v0.3\n---\n"
        out, fixes = normalize_frontmatter_links(raw)
        assert fixes == []
        problems = check_frontmatter(out)
        assert any("C 类" in p for p in problems)

    def test_unparseable_frontmatter_reported(self):
        raw = "---\nkey: [unclosed\n---\n"
        problems = check_frontmatter(raw)
        assert any("YAML 解析失败" in p for p in problems)

    def test_normalized_a_class_is_clean(self):
        """A 类走完规范化后，体检应当零问题。"""
        out, _ = normalize_frontmatter_links("---\nupstream: [[a]], [[b]]\n---\n")
        assert check_frontmatter(out) == []
