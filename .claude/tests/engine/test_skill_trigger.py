"""
tests/engine/test_skill_trigger.py — skill keyword 触发器单测

覆盖：
- extract_core_section：抽 `## 核心约束` 章节 / fallback 全文 / 同级标题边界
- match_skill：always / keywords / file_patterns / 缺失字段 / 大小写
- discover_role_skills：多 skill 部分命中 / 目录不存在 / 跨角色不误命中
- render_triggered_block：渲染 / 空命中 / 截断
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.skill_trigger import (
    extract_core_section,
    match_skill,
    discover_role_skills,
    render_triggered_block,
)


def _write(p: Path, content: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def _skill_md(*, trigger: dict | None = None, body: str = "## 核心约束\n禁止裸取。\n") -> str:
    """快速造一个 skill 文件内容（含可选 trigger frontmatter）。"""
    if trigger is None:
        return f"---\ntype: skill\nrole: 后端工程师\n---\n\n{body}"

    lines = ["type: skill", "role: 后端工程师", "trigger:"]
    for k, v in trigger.items():
        if isinstance(v, list):
            lines.append(f"  {k}:")
            for item in v:
                lines.append(f"    - {item!r}")
        else:
            lines.append(f"  {k}: {v}")
    fm = "\n".join(lines)
    return f"---\n{fm}\n---\n\n{body}"


# ── 1. extract_core_section ─────────────────────────────────────────
class TestExtractCoreSection:
    def test_hits_core_section(self):
        text = (
            "# B5 标题\n\n"
            "## 核心约束\n"
            "禁止 None 取值。\n\n"
            "## 强制写法\n"
            "if row is None: raise\n"
        )
        out = extract_core_section(text)
        assert "## 核心约束" in out
        assert "禁止 None 取值" in out
        assert "强制写法" not in out  # 同级标题切断
        assert "if row is None" not in out

    def test_fallback_when_no_core_section(self):
        text = "# B5\n\n## 强制写法\n代码示例。\n"
        out = extract_core_section(text)
        assert out == text  # fallback 全文

    def test_h3_core_section_still_hits(self):
        """h3 级标题含『核心约束』也应命中（与 wikilink 一致：包含即命中）。"""
        text = "# B5\n\n### 核心约束\n禁止 X。\n\n### 别的\n.\n"
        out = extract_core_section(text)
        assert "禁止 X" in out
        assert "别的" not in out

    def test_h2_section_stops_at_next_h1(self):
        text = "## 核心约束\nA\n\n# 新页\nB\n"
        out = extract_core_section(text)
        assert "A" in out
        assert "B" not in out  # 更高级标题切断

    def test_section_keeps_subheadings(self):
        """## 核心约束 内部可包含 h3 子标题，应一并保留。"""
        text = (
            "## 核心约束\n"
            "### 子点 1\nA\n"
            "### 子点 2\nB\n\n"
            "## 强制写法\nC\n"
        )
        out = extract_core_section(text)
        assert "子点 1" in out and "A" in out
        assert "子点 2" in out and "B" in out
        assert "强制写法" not in out and "C" not in out


# ── 2. match_skill ───────────────────────────────────────────────────
class TestMatchSkill:
    def test_always_true_bypasses_others(self, tmp_path: Path):
        sk = _write(tmp_path / "B5.md", _skill_md(trigger={"always": True}))
        ok, reason = match_skill(sk, "完全无关 task 文本")
        assert ok is True
        assert reason == "always"

    def test_keyword_hits_in_task_text(self, tmp_path: Path):
        sk = _write(tmp_path / "B6.md", _skill_md(trigger={"keywords": ["StaticFiles"]}))
        ok, reason = match_skill(sk, "用 StaticFiles 挂载 /static 目录")
        assert ok is True
        assert "keyword:StaticFiles" in reason

    def test_keyword_hits_in_upstream_text(self, tmp_path: Path):
        sk = _write(tmp_path / "B1.md", _skill_md(trigger={"keywords": ["os.environ"]}))
        ok, _ = match_skill(
            sk,
            task_text="实现配置加载",
            upstream_text="技术栈：使用 os.environ 读 ENV",
        )
        assert ok is True

    def test_keyword_case_insensitive(self, tmp_path: Path):
        sk = _write(tmp_path / "B7.md", _skill_md(trigger={"keywords": ["FastAPI"]}))
        ok, _ = match_skill(sk, "用 fastapi 搭后端")
        assert ok is True

    def test_keyword_no_match_returns_false(self, tmp_path: Path):
        sk = _write(tmp_path / "B1.md", _skill_md(trigger={"keywords": ["redis"]}))
        ok, reason = match_skill(sk, "处理本地文件")
        assert ok is False
        assert reason == ""

    def test_file_pattern_hits(self, tmp_path: Path):
        sk = _write(tmp_path / "B6.md", _skill_md(trigger={"file_patterns": ["**/static/*.css"]}))
        code_root = tmp_path / "project_code"
        _write(code_root / "src/backend/static/style.css", "body {}")
        ok, reason = match_skill(sk, "任意 task", project_code_root=code_root)
        assert ok is True
        assert "file_pattern" in reason

    def test_file_pattern_skipped_when_code_root_none(self, tmp_path: Path):
        sk = _write(tmp_path / "B6.md", _skill_md(trigger={"file_patterns": ["**/*.py"]}))
        ok, _ = match_skill(sk, "任意 task", project_code_root=None)
        assert ok is False

    def test_trigger_missing_is_fail_closed(self, tmp_path: Path):
        """trigger 字段缺失 → 视为不加载（与 plan 决策一致）。"""
        sk = _write(tmp_path / "B_old.md", "---\ntype: skill\n---\n\n## 核心约束\n.\n")
        ok, reason = match_skill(sk, "任何 task")
        assert ok is False
        assert reason == "no-trigger"

    def test_trigger_all_empty_no_match(self, tmp_path: Path):
        """trigger 存在但 always=false + keywords/patterns 空 → 不命中。"""
        sk = _write(tmp_path / "B.md", _skill_md(trigger={
            "always": False, "keywords": [], "file_patterns": [],
        }))
        ok, _ = match_skill(sk, "任意 task")
        assert ok is False

    def test_no_frontmatter_is_fail_closed(self, tmp_path: Path):
        """裸 markdown 没有 --- 围栏 → fm={} → trigger 缺失 → 不命中。"""
        sk = _write(tmp_path / "B_bare.md", "# B 内容直接写")
        ok, _ = match_skill(sk, "任意 task")
        assert ok is False


# ── 3. discover_role_skills ─────────────────────────────────────────
class TestDiscoverRoleSkills:
    def test_partial_hits(self, tmp_path: Path):
        role_dir = tmp_path / "后端工程师"
        _write(role_dir / "B1.md", _skill_md(trigger={"keywords": ["redis"]}))
        _write(role_dir / "B5.md", _skill_md(trigger={"always": True}))
        _write(role_dir / "B6.md", _skill_md(trigger={"keywords": ["StaticFiles"]}))
        hits = discover_role_skills(role_dir, task_text="用 StaticFiles 挂载")
        assert {p.stem for p, _ in hits} == {"B5", "B6"}

    def test_missing_dir_returns_empty(self, tmp_path: Path):
        hits = discover_role_skills(tmp_path / "不存在", "task")
        assert hits == []

    def test_results_sorted_by_filename(self, tmp_path: Path):
        role_dir = tmp_path / "后端工程师"
        _write(role_dir / "B6.md", _skill_md(trigger={"always": True}))
        _write(role_dir / "B1.md", _skill_md(trigger={"always": True}))
        _write(role_dir / "B5.md", _skill_md(trigger={"always": True}))
        hits = discover_role_skills(role_dir, "task")
        assert [p.stem for p, _ in hits] == ["B1", "B5", "B6"]

    def test_does_not_scan_other_role_dirs(self, tmp_path: Path):
        """只扫指定 role_dir，其他角色目录不误命中。"""
        backend_dir = tmp_path / "后端工程师"
        frontend_dir = tmp_path / "前端工程师"
        _write(backend_dir / "B5.md", _skill_md(trigger={"always": True}))
        _write(frontend_dir / "F1.md", _skill_md(trigger={"always": True}))
        hits = discover_role_skills(backend_dir, "task")
        assert [p.stem for p, _ in hits] == ["B5"]


# ── 4. render_triggered_block ───────────────────────────────────────
class TestRenderTriggeredBlock:
    def test_basic_render(self, tmp_path: Path):
        sk = _write(
            tmp_path / "B5.md",
            _skill_md(trigger={"always": True}, body="## 核心约束\n禁止裸取 row[0]。\n"),
        )
        block, loaded = render_triggered_block([(sk, "always")])
        assert "## 自动触发技能" in block
        assert "auto-trigger:always" in block
        assert "[[B5]]" in block
        assert "禁止裸取" in block
        assert loaded == ["B5"]

    def test_empty_hits_returns_empty(self):
        block, loaded = render_triggered_block([])
        assert block == ""
        assert loaded == []

    def test_truncates_long_skill(self, tmp_path: Path):
        long_body = "## 核心约束\n" + "x" * 5000 + "\n"
        sk = _write(tmp_path / "B.md", _skill_md(trigger={"always": True}, body=long_body))
        block, _ = render_triggered_block([(sk, "always")], max_chars_per_skill=500)
        assert "截断" in block
        assert "x" * 5001 not in block

    def test_fallback_to_full_body_when_no_core_section(self, tmp_path: Path):
        body = "# B 内容\n\n## 别的章节\n仅此而已。\n"
        sk = _write(tmp_path / "B.md", _skill_md(trigger={"always": True}, body=body))
        block, _ = render_triggered_block([(sk, "always")])
        assert "仅此而已" in block

    def test_total_char_budget_caps_accumulation(self, tmp_path: Path, capsys):
        """命中过多 skill 时 total_char_budget 兜底，超 budget 的跳过 + 警告。"""
        skills = []
        # 5 个 skill 各 1500 字符核心内容，total=7500 < 10000 全装入
        # 把 budget 设 5000：前 3 装入（4500），第 4-5 跳过
        for i in range(5):
            body = f"## 核心约束\n" + ("约束" * 750) + "\n"  # ~1500 字符
            sk = _write(
                tmp_path / f"B{i}.md",
                _skill_md(trigger={"always": True}, body=body),
            )
            skills.append((sk, "always"))
        block, loaded = render_triggered_block(skills, total_char_budget=5000)
        # 前 3 装入（每个 ~1500，累计 4500 < 5000；加第 4 个会超）
        assert len(loaded) <= 4  # 取决于实际字符数；至少不是全 5 个
        err = capsys.readouterr().err
        assert "total_char_budget" in err
        assert "用满" in err
