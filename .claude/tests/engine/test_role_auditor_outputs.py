"""
tests/engine/test_role_auditor_outputs.py — role_auditor 产物审计（PM PRD 越界）单测

monkeypatch role_auditor.main 的 VAULT_ROOT / append_audit / set_role_status
捕获 audit entries + state 调用，不真写 vault / runtime_state / audit.jsonl。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from role_auditor import main as ra_mod


# ── fixture：tmp vault + 捕获 audit / state 调用 ────────────
@pytest.fixture
def tmp_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    captured_audits: list[dict] = []
    captured_state_calls: list[dict] = []

    def fake_append_audit(entry: dict) -> None:
        captured_audits.append(entry)

    def fake_set_role_status(role: str, **kwargs) -> None:
        captured_state_calls.append({"role": role, **kwargs})

    monkeypatch.setattr(ra_mod, "VAULT_ROOT", tmp_path)
    monkeypatch.setattr(ra_mod, "append_audit", fake_append_audit)
    monkeypatch.setattr(ra_mod, "set_role_status", fake_set_role_status)

    ns = type("VaultNS", (), {})()
    ns.path = tmp_path
    ns.audits = captured_audits
    ns.state_calls = captured_state_calls
    return ns


def _write_prd(vault: Path, project: str, content: str) -> Path:
    p = vault / "10-项目" / project / "PRD.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


_CLEAN_PRD = """# 产品需求文档 - mini-ledger

## 1. 产品定位
一句话描述：个人记账小工具。
目标用户：个人。

## 2. 核心场景（用户故事）
作为用户，我希望能记录支出，以便了解花销。

## 3. 功能清单
| 模块 | 功能点 | 优先级 | 验收标准 |
|------|--------|--------|---------|
| 记账 | 添加一笔 | P0 | 用户能新增一笔记录 |

## 4. 非功能性需求
- 性能：单用户

## 5. 明确不做的事
- 不做多用户同步

## 6. 待确认项
- 是否需要导出 CSV？

## 7. 参考资料
- [business_brief.md](inputs/business_brief.md)
"""


# ── _detect_pm_overflow ──────────────────────────────────
class TestDetectOverflow:
    def test_clean_prd_no_hits(self, tmp_path: Path):
        prd = tmp_path / "PRD.md"
        prd.write_text(_CLEAN_PRD, encoding="utf-8")
        hits = ra_mod._detect_pm_overflow(prd)
        assert hits == []

    def test_missing_prd_no_error(self, tmp_path: Path):
        assert ra_mod._detect_pm_overflow(tmp_path / "nope.md") == []

    def test_api_table_header_detected(self, tmp_path: Path):
        prd = tmp_path / "PRD.md"
        prd.write_text(
            _CLEAN_PRD + "\n## 4. 接口\n| 方法 | 路径 | 用途 |\n| GET | /api/foo | 取数据 |\n",
            encoding="utf-8",
        )
        hits = ra_mod._detect_pm_overflow(prd)
        pids = {h["pattern_id"] for h in hits}
        assert "api_table_header" in pids
        assert "api_method_row" in pids

    def test_ddl_field_detected(self, tmp_path: Path):
        prd = tmp_path / "PRD.md"
        prd.write_text(
            _CLEAN_PRD + "\n## schema\nid INTEGER PK\nname TEXT NOT NULL UNIQUE\n",
            encoding="utf-8",
        )
        hits = ra_mod._detect_pm_overflow(prd)
        assert any(h["pattern_id"] == "ddl_field" for h in hits)

    def test_schema_table_header_detected(self, tmp_path: Path):
        prd = tmp_path / "PRD.md"
        prd.write_text(
            _CLEAN_PRD + "\n## schema\n| 字段 | 类型 | 约束 |\n",
            encoding="utf-8",
        )
        hits = ra_mod._detect_pm_overflow(prd)
        assert any(h["pattern_id"] == "schema_table_header" for h in hits)

    def test_framework_choice_detected(self, tmp_path: Path):
        prd = tmp_path / "PRD.md"
        prd.write_text(
            _CLEAN_PRD + "\n## 技术选型\n后端：Flask 或 FastAPI\n图表：Chart.js 或 ECharts\n",
            encoding="utf-8",
        )
        hits = ra_mod._detect_pm_overflow(prd)
        assert any(h["pattern_id"] == "framework_choice" for h in hits)

    def test_task_split_detected(self, tmp_path: Path):
        prd = tmp_path / "PRD.md"
        prd.write_text(
            _CLEAN_PRD
            + "\n## 任务拆分\n| # | 任务 | 角色 |\n| T1 | 建模 | 后端 |\n| T2 | 表单 | 前端 |\n",
            encoding="utf-8",
        )
        hits = ra_mod._detect_pm_overflow(prd)
        pids = {h["pattern_id"] for h in hits}
        assert "task_split_header" in pids
        assert "task_id_row" in pids

    def test_hit_has_line_and_snippet(self, tmp_path: Path):
        prd = tmp_path / "PRD.md"
        prd.write_text("L1\nL2 | 方法 | 路径 |\nL3\n", encoding="utf-8")
        hits = ra_mod._detect_pm_overflow(prd)
        assert hits[0]["line"] == 2
        assert "方法" in hits[0]["snippet"]


# ── _run_pm_output_audit ─────────────────────────────────
class TestRunAudit:
    def test_no_projects_returns_2(self, tmp_vault):
        # vault 没建 10-项目/ 目录
        rc = ra_mod._run_pm_output_audit(dry_run=False)
        assert rc == 2

    def test_empty_projects_returns_2(self, tmp_vault):
        (tmp_vault.path / "10-项目").mkdir()
        rc = ra_mod._run_pm_output_audit(dry_run=False)
        assert rc == 2

    def test_clean_prd_no_audit_no_state_increment(self, tmp_vault):
        _write_prd(tmp_vault.path, "clean-proj", _CLEAN_PRD)
        rc = ra_mod._run_pm_output_audit(dry_run=False)
        assert rc == 0
        assert tmp_vault.audits == []
        assert tmp_vault.state_calls == []

    def test_overflow_writes_audit_and_increments(self, tmp_vault):
        _write_prd(
            tmp_vault.path, "bad-proj",
            _CLEAN_PRD + "\n## API\n| 方法 | 路径 |\n| GET | /api/x |\n",
        )
        rc = ra_mod._run_pm_output_audit(dry_run=False)
        assert rc == 0

        assert len(tmp_vault.audits) == 1
        e = tmp_vault.audits[0]
        assert e["type"] == "pm_output_overflow"
        assert e["project"] == "bad-proj"
        assert e["role"] == "产品经理"
        assert e["audited_by"] == ra_mod.ROLE
        assert e["hit_count"] >= 2
        assert "api_table_header" in e["patterns"]
        assert isinstance(e["hits"], list)
        assert all("pattern_id" in h and "line" in h for h in e["hits"])

        # set_role_status 单次 +1（不是 +N）
        assert len(tmp_vault.state_calls) == 1
        c = tmp_vault.state_calls[0]
        assert c["role"] == "产品经理"
        assert c["increment_consecutive_failures"] is True

    def test_dry_run_does_not_write(self, tmp_vault):
        _write_prd(
            tmp_vault.path, "bad-proj",
            _CLEAN_PRD + "\n| 方法 | 路径 |\n",
        )
        rc = ra_mod._run_pm_output_audit(dry_run=True)
        assert rc == 0
        assert tmp_vault.audits == []
        assert tmp_vault.state_calls == []

    def test_multiple_projects_independent(self, tmp_vault):
        _write_prd(tmp_vault.path, "clean-proj", _CLEAN_PRD)
        _write_prd(
            tmp_vault.path, "bad-proj-a",
            _CLEAN_PRD + "\n| 方法 | 路径 |\n",
        )
        _write_prd(
            tmp_vault.path, "bad-proj-b",
            _CLEAN_PRD + "\nid INTEGER PK\n",
        )
        rc = ra_mod._run_pm_output_audit(dry_run=False)
        assert rc == 0

        assert len(tmp_vault.audits) == 2
        projs = sorted(e["project"] for e in tmp_vault.audits)
        assert projs == ["bad-proj-a", "bad-proj-b"]
        # 每个越界项目独立 +1
        assert len(tmp_vault.state_calls) == 2

    def test_single_audit_entry_lists_all_patterns(self, tmp_vault):
        """同一 PRD 含多类越界时合并到 1 entry，patterns 列出所有命中的 pattern_id。"""
        _write_prd(
            tmp_vault.path, "multi-bad",
            _CLEAN_PRD
            + "\n| 方法 | 路径 |\n"
            + "id INTEGER PK\n"
            + "后端框架：Flask 或 FastAPI\n",
        )
        rc = ra_mod._run_pm_output_audit(dry_run=False)
        assert rc == 0

        # 1 entry 1 state +1（不是 3 entries 3 +1）
        assert len(tmp_vault.audits) == 1
        assert len(tmp_vault.state_calls) == 1
        e = tmp_vault.audits[0]
        assert "api_table_header" in e["patterns"]
        assert "ddl_field" in e["patterns"]
        assert "framework_choice" in e["patterns"]


# ── 外迁 skill 治理 + trigger 完整性 lint ────────────────────
class TestSkillTriggerValid:
    """_skill_trigger_valid：判断 skill frontmatter 的 trigger 字段是否合法。"""

    def test_always_true_valid(self):
        assert ra_mod._skill_trigger_valid({"trigger": {"always": True}})

    def test_keywords_non_empty_valid(self):
        assert ra_mod._skill_trigger_valid(
            {"trigger": {"keywords": ["kw1"], "always": False}}
        )

    def test_file_patterns_non_empty_valid(self):
        assert ra_mod._skill_trigger_valid(
            {"trigger": {"file_patterns": ["src/**/*.py"], "always": False}}
        )

    def test_no_trigger_field_invalid(self):
        assert not ra_mod._skill_trigger_valid({"type": "skill"})

    def test_trigger_not_dict_invalid(self):
        assert not ra_mod._skill_trigger_valid({"trigger": "invalid"})

    def test_all_empty_invalid(self):
        assert not ra_mod._skill_trigger_valid(
            {"trigger": {"keywords": [], "file_patterns": [], "always": False}}
        )

    def test_keywords_all_whitespace_invalid(self):
        assert not ra_mod._skill_trigger_valid(
            {"trigger": {"keywords": ["", "  "], "always": False}}
        )


def _write_role(vault: Path, filename: str, frontmatter_yaml: str, body: str = "") -> Path:
    """在 tmp vault 的 00-系统/角色基因/ 下写一个角色文件。"""
    p = vault / "00-系统" / "角色基因" / filename
    p.parent.mkdir(parents=True, exist_ok=True)
    content = f"---\n{frontmatter_yaml}\n---\n\n{body or '# 角色：测试\\n\\n## 1. 核心\\n测试'}\n\n<!-- DYNAMIC_START -->\n<!-- DYNAMIC_END -->\n"
    p.write_text(content, encoding="utf-8")
    return p


def _write_skill(vault: Path, rel: str, frontmatter_yaml: str) -> Path:
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\n{frontmatter_yaml}\n---\n\n## 核心约束\n测试\n", encoding="utf-8")
    return p


_FM_BASE = (
    "domain: 测域\nmodel: claude-sonnet-4-6\nmax_tokens: 4096\nstyle: 测\n"
    "aliases: []\nupstream: []\ndownstream: []\nmonitors: []\n"
    "inputs: []\noutputs: []\ntools: []"
)


class TestMeasureRoleOutsourcedSkills:
    """_measure_role 的外迁 skill lint（2026-08-25 起口径 = 整个 skill 目录）。

    改动前本类叫 TestMeasureRoleSkillRefs，量的是 frontmatter `skill_refs` 声明。
    该字段废弃后两件事同时变了：
      ① 扫描源 = `20-知识/角色技能/{domain}/{角色}/` 目录，声明不再参与；
      ② `skill_refs_max` 软上限连同 `skill_refs_over_limit` 字段一并删除。
    """

    def test_no_skill_dir_zero_count_no_gaps(self, tmp_vault):
        role_path = _write_role(
            tmp_vault.path, "角色-无 skill.md", f"role: 无 skill\n{_FM_BASE}",
        )
        m = ra_mod._measure_role(role_path)
        assert m["skill_file_count"] == 0
        assert m["skill_trigger_gaps"] == []
        assert "skill_refs_over_limit" not in m, "废弃字段不该复活"
        assert "skill_refs_count" not in m

    def test_all_triggers_valid_no_gaps(self, tmp_vault):
        for i in range(3):
            _write_skill(
                tmp_vault.path, f"20-知识/角色技能/有 skill/S{i}.md",
                f"type: skill\ntrigger:\n  keywords:\n    - kw{i}",
            )
        role_path = _write_role(
            tmp_vault.path, "角色-有 skill.md", f"role: 有 skill\n{_FM_BASE}",
        )
        m = ra_mod._measure_role(role_path)
        assert m["skill_file_count"] == 3
        assert m["skill_trigger_gaps"] == []

    def test_undeclared_skills_are_also_checked(self, tmp_vault):
        """口径修正的正控：目录里的每一张都查，不看有没有被声明过。

        实测缘由：UI设计师 frontmatter 只声明 2 张，目录里有 17 张 ——
        另外 15 张的 trigger 在旧口径下从来没被查过。
        """
        _write_skill(
            tmp_vault.path, "20-知识/角色技能/欠声明/好的.md",
            "type: skill\ntrigger:\n  keywords:\n    - kw",
        )
        _write_skill(
            tmp_vault.path, "20-知识/角色技能/欠声明/坏的.md", "type: skill",
        )
        role_path = _write_role(
            tmp_vault.path, "角色-欠声明.md", f"role: 欠声明\n{_FM_BASE}",
        )
        m = ra_mod._measure_role(role_path)
        assert m["skill_file_count"] == 2
        assert len(m["skill_trigger_gaps"]) == 1
        assert "坏的.md" in m["skill_trigger_gaps"][0]

    def test_count_alone_never_reports_an_issue(self, tmp_vault):
        """数量上限已删 —— 22 张也不报警。

        这条是**刻意的回归守卫**：`skill_refs_max = 5` 的依据是「上限 = 现最高值」，
        自认循环论证；改扫目录后实测编曲 22 / 混音师 21 / UI设计师 17，5 会对
        10 个角色里 8 个报警。没有数据能支撑「一个角色该有几张」，所以宁可无阈值。
        谁想加回来，得先带依据，并让这条测试红。
        """
        for i in range(22):
            _write_skill(
                tmp_vault.path, f"20-知识/角色技能/很多/S{i}.md",
                "type: skill\ntrigger:\n  always: true",
            )
        role_path = _write_role(
            tmp_vault.path, "角色-很多.md", f"role: 很多\n{_FM_BASE}",
        )
        m = ra_mod._measure_role(role_path)
        assert m["skill_file_count"] == 22
        assert m["skill_trigger_gaps"] == []
        report = ra_mod._format_measurements([m])
        assert "[SHRINK?]" not in report
        assert "软上限" not in report

    def test_missing_trigger_gap_says_never_reaches_prompt(self, tmp_vault):
        """废弃后 trigger 是唯一通道 → 告警必须说清后果，不只说「缺失」。"""
        _write_skill(
            tmp_vault.path, "20-知识/角色技能/触发器空/S_no_trigger.md",
            "type: skill",
        )
        role_path = _write_role(
            tmp_vault.path, "角色-触发器空.md", f"role: 触发器空\n{_FM_BASE}",
        )
        m = ra_mod._measure_role(role_path)
        assert m["skill_file_count"] == 1
        assert len(m["skill_trigger_gaps"]) == 1
        gap = m["skill_trigger_gaps"][0]
        assert "S_no_trigger.md" in gap
        assert "永不进 prompt" in gap

    def test_underscore_and_dot_files_skipped(self, tmp_vault):
        """`_通用` 类前缀与隐藏文件不参与（与 discover_role_skills 口径一致）。"""
        _write_skill(
            tmp_vault.path, "20-知识/角色技能/跳过/_模板.md", "type: skill",
        )
        _write_skill(
            tmp_vault.path, "20-知识/角色技能/跳过/S.md",
            "type: skill\ntrigger:\n  always: true",
        )
        role_path = _write_role(
            tmp_vault.path, "角色-跳过.md", f"role: 跳过\n{_FM_BASE}",
        )
        m = ra_mod._measure_role(role_path)
        assert m["skill_file_count"] == 1
        assert m["skill_trigger_gaps"] == []

    def test_format_measurements_includes_trigger_gap(self, tmp_vault):
        _write_skill(
            tmp_vault.path, "20-知识/角色技能/报告/S_bad.md", "type: skill",
        )
        role_path = _write_role(
            tmp_vault.path, "角色-报告.md", f"role: 报告\n{_FM_BASE}",
        )
        m = ra_mod._measure_role(role_path)
        report = ra_mod._format_measurements([m])
        assert "外迁 skill 触发器不合法" in report
        assert "S_bad.md" in report


# ── 2026-08-13 新增三条 lint 的正控 ────────────────────────────────────
# 缘由：2026-08-13 审计报告的 4 个严重问题里，3 个空壳角色 + 产品经理
# upstream: null 全部逃过了程序层检测（章节标题存在即算数 / null 被当作
# 「字段存在」）。这三条 lint 就是堵这两类盲区的，必须有正控。

_BIZ_FM = (
    "role: {role}\ndomain: music\nmodel: claude-sonnet-4-6\nmax_tokens: 4096\n"
    "style: 测\naliases: []\nupstream: []\ndownstream: []\nmonitors: []\n"
    "inputs: []\noutputs: []\ntools: []\nversion: {version}"
)


def _biz_body(sections: dict[int, str]) -> str:
    """按 {章节号: 正文} 生成业务角色 §1-§8 正文。"""
    titles = {
        1: "核心使命", 2: "输入与输出", 3: "职责范围",
        4: "职责边界", 5: "执行工作流", 6: "质量原则",
    }
    out = ["# 角色：测试"]
    for n in range(1, 7):
        # 默认填充必须明显超过 LIMITS["min_section_chars"]（40），否则合规样本
        # 会被自己的阈值判成空壳 —— 这里给到 60+ chars 留足余量
        filler = "这里是一段足够长的正文内容，用来确保本章节在剥离全部空白与 HTML 注释之后，有效字符数仍然明显超过空壳判定的下限要求。"
        out.append(f"\n## {n}. {titles[n]}\n\n{sections.get(n, filler)}")
    out.append("\n## 7. 运行时补丁（控制区）")
    return "\n".join(out)


class TestLintSectionNonEmpty:
    """业务角色 §1-§6 有效正文不足 min_section_chars → ERROR_HOLLOW。"""

    def test_html_comment_only_section_is_hollow(self, tmp_vault):
        body = _biz_body({3: "<!-- W2-W3 起草 -->"})
        role_path = _write_role(
            tmp_vault.path, "角色-空壳.md",
            _BIZ_FM.format(role="空壳", version="1.0.0"), body,
        )
        m = ra_mod._measure_role(role_path)
        assert m["prompt_whitelist_level"] == "ERROR_HOLLOW"
        assert "§3(0 chars)" in "; ".join(m["prompt_whitelist_issues"])

    def test_full_sections_not_hollow(self, tmp_vault):
        role_path = _write_role(
            tmp_vault.path, "角色-完整.md",
            _BIZ_FM.format(role="完整", version="1.0.0"), _biz_body({}),
        )
        m = ra_mod._measure_role(role_path)
        assert m["prompt_whitelist_level"] == "OK"

    def test_meta_role_exempt_from_hollow(self, tmp_vault):
        """元角色不受 §1-§6 白名单约束（规范 §3.5.2），不应判 ERROR_HOLLOW。"""
        fm = _BIZ_FM.format(role="元空", version="1.0.0").replace(
            "domain: music", "domain: 元")
        role_path = _write_role(
            tmp_vault.path, "角色-元空.md", fm,
            _biz_body({3: "<!-- 待起草 -->"}) + "\n\n## 8. 版本历史\n- v1.0.0 (2026-01-01): 初版",
        )
        m = ra_mod._measure_role(role_path)
        assert m["prompt_whitelist_level"] != "ERROR_HOLLOW"


class TestLintListFieldType:
    """规范 §2.2：声明为 list 的字段值为 null / 标量 → 报违规。"""

    def test_null_upstream_flagged(self, tmp_vault):
        fm = _BIZ_FM.format(role="空上游", version="1.0.0").replace(
            "upstream: []", "upstream: null")
        role_path = _write_role(tmp_vault.path, "角色-空上游.md", fm, _biz_body({}))
        m = ra_mod._measure_role(role_path)
        assert any("upstream" in v and "null" in v for v in m["list_field_violations"])
        # null 不是「缺字段」，旧检测确实看不见 —— 正是本 lint 要堵的盲区
        assert "upstream" not in m["missing_required"]
        assert "list 字段类型违规" in ra_mod._format_measurements([m])

    def test_scalar_instead_of_list_flagged(self, tmp_vault):
        fm = _BIZ_FM.format(role="标量", version="1.0.0").replace(
            "tools: []", "tools: obsidian_read")
        role_path = _write_role(tmp_vault.path, "角色-标量.md", fm, _biz_body({}))
        m = ra_mod._measure_role(role_path)
        assert any("tools" in v and "不是 list" in v for v in m["list_field_violations"])

    def test_proper_lists_clean(self, tmp_vault):
        role_path = _write_role(
            tmp_vault.path, "角色-干净.md",
            _BIZ_FM.format(role="干净", version="1.0.0"), _biz_body({}),
        )
        assert ra_mod._measure_role(role_path)["list_field_violations"] == []


class TestLintVersionConsistency:
    """规范 §3.4a：frontmatter version 必须等于 §8 里的 semver 最大值。"""

    def _with_v8(self, body: str, entries: str) -> str:
        return body + "\n\n## 8. 版本历史\n" + entries

    def test_drift_flagged(self, tmp_vault):
        body = self._with_v8(_biz_body({}), "- v0.3.0 (2026-07-11): 改 model\n- v0.2.0 (2026-05-25): 初版")
        role_path = _write_role(
            tmp_vault.path, "角色-漂移.md",
            _BIZ_FM.format(role="漂移", version="0.2.0"), body,
        )
        m = ra_mod._measure_role(role_path)
        assert m["version_mismatch"] is True
        assert m["section8_max_version"] == "0.3.0"
        assert "版本漂移" in ra_mod._format_measurements([m])

    def test_consistent_not_flagged(self, tmp_vault):
        body = self._with_v8(_biz_body({}), "- v0.3.0 (2026-07-11): 改 model\n- v0.2.0 (2026-05-25): 初版")
        role_path = _write_role(
            tmp_vault.path, "角色-一致.md",
            _BIZ_FM.format(role="一致", version="0.3.0"), body,
        )
        assert ra_mod._measure_role(role_path)["version_mismatch"] is False

    def test_ascending_and_descending_both_work(self, tmp_vault):
        """取最大值判定，与排列方向无关（旧文件可能仍是升序）。"""
        asc = self._with_v8(_biz_body({}), "- v0.2.0 (2026-05-25): 初版\n- v0.3.0 (2026-07-11): 改 model")
        role_path = _write_role(
            tmp_vault.path, "角色-升序.md",
            _BIZ_FM.format(role="升序", version="0.3.0"), asc,
        )
        assert ra_mod._measure_role(role_path)["version_mismatch"] is False

    def test_table_format_works(self, tmp_vault):
        body = self._with_v8(
            _biz_body({}),
            "| 版本 | 日期 | 说明 |\n|---|---|---|\n| 1.2.0 | 2026-08-01 | 新 |\n| 1.1.0 | 2026-07-01 | 旧 |",
        )
        role_path = _write_role(
            tmp_vault.path, "角色-表格.md",
            _BIZ_FM.format(role="表格", version="1.1.0"), body,
        )
        m = ra_mod._measure_role(role_path)
        assert m["version_mismatch"] is True
        assert m["section8_max_version"] == "1.2.0"


class TestSectionSplitSkipsCodeFence:
    """_split_sections 必须跳过 fenced code block。

    回归：创意发散者 §3 内嵌产物模板，模板自身用 `## 1.` ~ `## 6.` 当标题。
    不跳围栏时模板会覆盖外层同号章节，实测 §1 的 180 chars 被误记为 21。
    """

    def test_embedded_template_does_not_clobber_sections(self):
        body = (
            "## 1. 核心使命\n\n" + "真实内容" * 30 + "\n\n"
            "## 3. 职责范围\n\n产出模板如下：\n\n"
            "```markdown\n## 1. 核心机会点\n{占位}\n\n## 2. 目标用户\n{占位}\n```\n\n"
            "## 4. 职责边界\n\n边界内容\n"
        )
        counts = ra_mod._section_char_counts(body)
        assert counts["1"] > 100, "§1 被围栏内的 `## 1.` 覆盖了"
        assert "2" not in counts, "围栏内的 `## 2.` 不应产生章节"
        assert counts["3"] > 50
