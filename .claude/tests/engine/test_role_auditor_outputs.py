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


# ── P6：skill_refs 治理 + trigger 完整性 lint ────────────────
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


class TestMeasureRoleSkillRefs:
    """_measure_role 的 P6 新字段：skill_refs_count / over_limit / trigger_gaps。"""

    def test_no_skill_refs_zero_count_no_gaps(self, tmp_vault):
        role_path = _write_role(
            tmp_vault.path, "角色-无 skill.md",
            "role: 无 skill\ndomain: 元\nmodel: claude-sonnet-4-6\nmax_tokens: 4096\nstyle: 测\naliases: []\nupstream: []\ndownstream: []\nmonitors: []\ninputs: []\noutputs: []\ntools: []",
        )
        m = ra_mod._measure_role(role_path)
        assert m["skill_refs_count"] == 0
        assert not m["skill_refs_over_limit"]
        assert m["skill_trigger_gaps"] == []

    def test_within_limit_all_triggers_valid_no_gaps(self, tmp_vault):
        for i in range(3):
            _write_skill(
                tmp_vault.path, f"20-知识/角色技能/测试/S{i}.md",
                f"type: skill\ntrigger:\n  keywords:\n    - kw{i}",
            )
        role_path = _write_role(
            tmp_vault.path, "角色-有 skill.md",
            "role: 有 skill\ndomain: 元\nmodel: claude-sonnet-4-6\nmax_tokens: 4096\nstyle: 测\naliases: []\nupstream: []\ndownstream: []\nmonitors: []\ninputs: []\noutputs: []\ntools: []\nskill_refs:\n  - 20-知识/角色技能/测试/S0.md\n  - 20-知识/角色技能/测试/S1.md\n  - 20-知识/角色技能/测试/S2.md",
        )
        m = ra_mod._measure_role(role_path)
        assert m["skill_refs_count"] == 3
        assert not m["skill_refs_over_limit"]
        assert m["skill_trigger_gaps"] == []

    def test_over_limit_triggers_flag(self, tmp_vault):
        # 6 个 skill（超软上限 5），每个都有 trigger.always 保证 gaps 为空
        for i in range(6):
            _write_skill(
                tmp_vault.path, f"20-知识/角色技能/测试/S{i}.md",
                "type: skill\ntrigger:\n  always: true",
            )
        refs = "\n".join(f"  - 20-知识/角色技能/测试/S{i}.md" for i in range(6))
        role_path = _write_role(
            tmp_vault.path, "角色-过多 skill.md",
            f"role: 过多\ndomain: 元\nmodel: claude-sonnet-4-6\nmax_tokens: 4096\nstyle: 测\naliases: []\nupstream: []\ndownstream: []\nmonitors: []\ninputs: []\noutputs: []\ntools: []\nskill_refs:\n{refs}",
        )
        m = ra_mod._measure_role(role_path)
        assert m["skill_refs_count"] == 6
        assert m["skill_refs_over_limit"]
        assert m["skill_trigger_gaps"] == []

    def test_missing_trigger_field_gap(self, tmp_vault):
        _write_skill(
            tmp_vault.path, "20-知识/角色技能/测试/S_no_trigger.md",
            "type: skill",
        )
        role_path = _write_role(
            tmp_vault.path, "角色-触发器空.md",
            "role: 触发器空\ndomain: 元\nmodel: claude-sonnet-4-6\nmax_tokens: 4096\nstyle: 测\naliases: []\nupstream: []\ndownstream: []\nmonitors: []\ninputs: []\noutputs: []\ntools: []\nskill_refs:\n  - 20-知识/角色技能/测试/S_no_trigger.md",
        )
        m = ra_mod._measure_role(role_path)
        assert m["skill_refs_count"] == 1
        assert len(m["skill_trigger_gaps"]) == 1
        assert "S_no_trigger.md" in m["skill_trigger_gaps"][0]
        assert "trigger 缺失" in m["skill_trigger_gaps"][0]

    def test_missing_skill_file_gap(self, tmp_vault):
        role_path = _write_role(
            tmp_vault.path, "角色-指向缺失.md",
            "role: 缺失指向\ndomain: 元\nmodel: claude-sonnet-4-6\nmax_tokens: 4096\nstyle: 测\naliases: []\nupstream: []\ndownstream: []\nmonitors: []\ninputs: []\noutputs: []\ntools: []\nskill_refs:\n  - 20-知识/角色技能/测试/NOT_EXISTS.md",
        )
        m = ra_mod._measure_role(role_path)
        assert m["skill_refs_count"] == 1
        assert len(m["skill_trigger_gaps"]) == 1
        assert "NOT_EXISTS.md" in m["skill_trigger_gaps"][0]
        assert "文件缺失" in m["skill_trigger_gaps"][0]

    def test_format_measurements_includes_p6_issues(self, tmp_vault):
        """P6 lint 触发时 _format_measurements 应输出对应 issue 行。"""
        _write_skill(
            tmp_vault.path, "20-知识/角色技能/测试/S_bad.md",
            "type: skill",
        )
        role_path = _write_role(
            tmp_vault.path, "角色-触发器空.md",
            "role: 触发器空\ndomain: 元\nmodel: claude-sonnet-4-6\nmax_tokens: 4096\nstyle: 测\naliases: []\nupstream: []\ndownstream: []\nmonitors: []\ninputs: []\noutputs: []\ntools: []\nskill_refs:\n  - 20-知识/角色技能/测试/S_bad.md",
        )
        m = ra_mod._measure_role(role_path)
        report = ra_mod._format_measurements([m])
        assert "trigger 缺失" in report
        # 数量未超限，[SHRINK?] 不应出现
        assert "[SHRINK?]" not in report

    def test_format_measurements_over_limit_shrink(self, tmp_vault):
        for i in range(6):
            _write_skill(
                tmp_vault.path, f"20-知识/角色技能/测试/T{i}.md",
                "type: skill\ntrigger:\n  always: true",
            )
        refs = "\n".join(f"  - 20-知识/角色技能/测试/T{i}.md" for i in range(6))
        role_path = _write_role(
            tmp_vault.path, "角色-太多.md",
            f"role: 太多\ndomain: 元\nmodel: claude-sonnet-4-6\nmax_tokens: 4096\nstyle: 测\naliases: []\nupstream: []\ndownstream: []\nmonitors: []\ninputs: []\noutputs: []\ntools: []\nskill_refs:\n{refs}",
        )
        m = ra_mod._measure_role(role_path)
        report = ra_mod._format_measurements([m])
        assert "[SHRINK?]" in report
        assert "skill_refs 数量 6" in report
