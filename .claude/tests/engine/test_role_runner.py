"""
test_role_runner.py — 声明驱动角色执行器 PoC（架构演进第 3 步）

策略：真实 vault 角色（母带工程师）+ 一次性 fixture 项目 + canned LLM 输出。
状态机/审计 monkeypatch 捕获，不触真实 .runtime-state / audit.jsonl。

覆盖：
- 八步流水行为等价核心点：指令内容进 context（v0.2 迁移回归的守卫）、
  rule_refs 注入、输出清单 = 声明 outputs、FILE 块写盘、audit success
- 注册表正文产出指引（D2 红利）
- dormant 识别 → 降级 scenario
- blocked 短路 / 无 FILE 块 → failed
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from engine.config import VAULT_ROOT

ROLE = "母带工程师"
PROJECT = "_runner-unit-test"


@pytest.fixture
def runner_env(monkeypatch):
    """fixture 项目 + 捕获钩子。真实 vault 只读（角色基因/注册表），
    项目目录一次性建删（10-项目 已 gitignore）。"""
    import engine.role_runner as rr

    proj = VAULT_ROOT / "10-项目" / "music" / PROJECT
    (proj / "指令").mkdir(parents=True, exist_ok=True)
    (proj / "指令" / f"给{ROLE}.md").write_text(
        "# 给母带工程师\n目标响度走流媒体基线，保持混音动态意图。",
        encoding="utf-8",
    )
    (proj / "创作 vision.md").write_text("# vision\n温暖民谣", encoding="utf-8")
    (proj / "Suno-prompt.md").write_text("# Suno\nfolk ballad", encoding="utf-8")
    (proj / "混音评估.md").write_text("# 混音评估\n低频略糊，其余达标", encoding="utf-8")

    captured: dict = {"status": [], "audit": []}
    monkeypatch.setattr(rr, "role_is_blocked", lambda name: False)
    monkeypatch.setattr(
        rr, "set_role_status",
        lambda name, **kw: captured["status"].append((name, kw.get("status"))),
    )
    monkeypatch.setattr(rr, "append_audit", captured["audit"].append)

    canned = (
        f"<!-- FILE: 10-项目/music/{{project}}/母带规格.md -->\n"
        f"# 母带规格\n流媒体响度基线\n<!-- /FILE -->\n"
        f"<!-- FILE: 10-项目/music/{{project}}/母带-Suno-retry补丁.md -->\n"
        f"无需 retry patch\n<!-- /FILE -->\n"
    )

    def fake_call(system_prompt, user_prompt, role_name):
        captured["system"] = system_prompt
        captured["user"] = user_prompt
        return canned

    monkeypatch.setattr(rr, "call_claude", fake_call)
    yield captured
    shutil.rmtree(proj, ignore_errors=True)


class TestRunRole:
    def test_full_pipeline(self, runner_env):
        from engine.role_runner import run_role

        result = run_role(ROLE, "常规母带", PROJECT)
        assert result.ok and result.returncode == 0
        # 声明 outputs 全部写盘 + RoleResult 携带
        spec = VAULT_ROOT / "10-项目" / "music" / PROJECT / "母带规格.md"
        patch = VAULT_ROOT / "10-项目" / "music" / PROJECT / "母带-Suno-retry补丁.md"
        assert spec.exists() and patch.exists()
        assert result.outputs and len(result.outputs) == 2

        user = runner_env["user"]
        # 指令内容真实进 context（v0.2 迁移回归守卫：{role} 已绑定）
        assert "目标响度走流媒体基线" in user
        assert "混音评估" in user
        # 输出清单 = 声明 outputs（{project} 已绑定）
        assert f"10-项目/music/{PROJECT}/母带规格.md" in user
        assert f"10-项目/music/{PROJECT}/母带-Suno-retry补丁.md" in user
        # 注册表正文产出指引（母带规格 已注册；补丁类未注册回退通用句）
        assert "最终交付规格" in user or "响度" in user
        # 状态机 + 审计
        assert ("母带工程师", "busy") in runner_env["status"]
        assert ("母带工程师", "success") in runner_env["status"]
        audit = runner_env["audit"][-1]
        assert audit["result"] == "success" and audit["runner"] == "in_process"

    def test_dormant_degrades(self, runner_env):
        from engine.role_runner import run_role

        instr = VAULT_ROOT / "10-项目" / "music" / PROJECT / "指令" / f"给{ROLE}.md"
        instr.write_text("本项目状态：dormant（不发布，不启动母带）", encoding="utf-8")
        result = run_role(ROLE, "", PROJECT)
        assert result.ok
        user = runner_env["user"]
        assert "dormant" in user and "严禁伪造完整业务内容" in user

    def test_blocked_short_circuit(self, monkeypatch):
        import engine.role_runner as rr
        from engine.role_runner import run_role

        monkeypatch.setattr(rr, "role_is_blocked", lambda name: True)
        result = run_role(ROLE, "t", PROJECT)
        assert result.status == "failed" and result.returncode == 1
        assert "blocked" in (result.error or "")

    def test_fanout_expansion_and_dormant_filter(self):
        from engine.role_runner import _expand_fanout_outputs, _parse_dormant_roles

        text = (
            "| 角色 | 决策 | 依据 |\n"
            "| **混音师** | **dormant** | Suno 一体出 |\n"
            "| 母带工程师 | dormant | 同上 |\n"
            "| 作词 | active | - |\n"
        )
        assert _parse_dormant_roles(
            text, ["作词", "混音师", "母带工程师"]
        ) == {"混音师", "母带工程师"}

        class FanRole:
            name = "制作人"
            downstream = ("作词", "作曲", "混音师")
            outputs = ("x/制作计划.md", "x/指令/给{role}.md")

        import tempfile
        from pathlib import Path as P
        with tempfile.TemporaryDirectory() as d:
            f = P(d) / "vision.md"
            f.write_text(text, encoding="utf-8")
            rels, dormant = _expand_fanout_outputs(
                FanRole(), ["x/制作计划.md", "x/指令/给{role}.md"], [f],
            )
        assert dormant == {"混音师"}
        assert rels == ["x/制作计划.md", "x/指令/给作词.md", "x/指令/给作曲.md"]

    def test_no_file_blocks_fails(self, runner_env, monkeypatch):
        import engine.role_runner as rr
        from engine.role_runner import run_role

        monkeypatch.setattr(rr, "call_claude", lambda s, u, r: "没有产出块的闲聊")
        result = run_role(ROLE, "t", PROJECT)
        assert result.status == "failed" and result.returncode == 1
        assert "no_file_blocks" in (result.error or "")
        audit = runner_env["audit"][-1]
        assert audit["result"] == "failed"


class TestSunoStyleMeasurement:
    """Style 段实测收编回归锁。

    历史背景（2026-07-26 CLI 壳瘦身时发现）：该实测原来只在
    music_composer/main.py（post-write 兜底），批量收编切 runner 后
    生产路径 audit 静默丢失 style_char_count / style_oversized 字段
    （凌晨四点 2026-07-25 实跑作曲 audit 实证）。本类锁定收编版行为。
    """

    def test_measure_helper(self):
        from engine.role_runner import _measure_suno_style_chars

        style = "acoustic folk, warm male smoky vocal, reggae offbeat"
        files = {
            "10-项目/music/x/曲作.md": "# 曲作\n正文",
            "10-项目/music/x/Suno-prompt.md": f"# Suno\n```\n{style}\n```\n尾注",
        }
        assert _measure_suno_style_chars(files) == len(style)
        # Suno-prompt.md 无 ``` 代码块 → None（不误报 0）
        assert _measure_suno_style_chars(
            {"10-项目/music/x/Suno-prompt.md": "没有代码块"}
        ) is None
        # 仅按文件名精确匹配：final-Suno-prompt.md（总监汇编产物）不在测量范围
        assert _measure_suno_style_chars(
            {"10-项目/music/x/final-Suno-prompt.md": "```\nabc\n```"}
        ) is None

    def test_composer_audit_carries_style_fields(self, monkeypatch):
        """作曲（outputs 声明含 Suno-prompt.md）经 runner 跑完，audit 必须带
        has_suno_prompt / style_char_count / style_oversized 三字段。"""
        import shutil as _shutil

        import engine.role_runner as rr
        from engine.role_runner import run_role

        proj = VAULT_ROOT / "10-项目" / "music" / PROJECT
        (proj / "指令").mkdir(parents=True, exist_ok=True)
        (proj / "指令" / "给作曲.md").write_text("# 给作曲\n写一首民谣", encoding="utf-8")
        (proj / "词作.md").write_text("# 词作\n歌词正文", encoding="utf-8")

        captured: dict = {"audit": []}
        monkeypatch.setattr(rr, "role_is_blocked", lambda name: False)
        monkeypatch.setattr(rr, "set_role_status", lambda name, **kw: None)
        monkeypatch.setattr(rr, "append_audit", captured["audit"].append)
        style = "warm folk ballad, male vocal, fingerpicked guitar"
        canned = (
            "<!-- FILE: 10-项目/music/{project}/曲作.md -->\n# 曲作\n<!-- /FILE -->\n"
            "<!-- FILE: 10-项目/music/{project}/Suno-prompt.md -->\n"
            f"# Suno\n```\n{style}\n```\n<!-- /FILE -->\n"
        )
        monkeypatch.setattr(rr, "call_claude", lambda s, u, r: canned)
        try:
            result = run_role("作曲", "写歌", PROJECT)
            assert result.ok
            audit = captured["audit"][-1]
            assert audit["has_suno_prompt"] is True
            assert audit["style_char_count"] == len(style)
            assert audit["style_oversized"] is False
        finally:
            _shutil.rmtree(proj, ignore_errors=True)


class TestFrontmatterLinkNormalization:
    """落盘前 frontmatter 链接规范化（治理三层的"主循环层"）。

    根因见 engine/frontmatter_links.py docstring：LLM 连 frontmatter 一起产出，
    引擎原样落盘，写错不报错但 Dataview 静默读到错值。
    """

    def test_contract_reaches_prompt(self, runner_env):
        """提示词层：frontmatter 契约必须进 user_prompt（不能再指望域 rule_refs）。"""
        from engine.role_runner import run_role

        run_role(ROLE, "常规母带", PROJECT)
        user = runner_env["user"]
        assert "frontmatter 硬约束" in user
        assert '禁止 `upstream: "[[a]], [[b]]"`' in user

    def test_c_class_output_is_normalized_on_disk(self, monkeypatch):
        """主循环层：LLM 产出 C 类写法 → 落盘时已被改成 block list。"""
        import shutil as _shutil

        import yaml

        import engine.role_runner as rr
        from engine.role_runner import run_role

        proj = VAULT_ROOT / "10-项目" / "music" / PROJECT
        (proj / "指令").mkdir(parents=True, exist_ok=True)
        (proj / "指令" / "给作曲.md").write_text("# 给作曲\n写一首民谣", encoding="utf-8")
        (proj / "词作.md").write_text("# 词作\n歌词正文", encoding="utf-8")

        captured: dict = {"audit": []}
        monkeypatch.setattr(rr, "role_is_blocked", lambda name: False)
        monkeypatch.setattr(rr, "set_role_status", lambda name, **kw: None)
        monkeypatch.setattr(rr, "append_audit", captured["audit"].append)
        canned = (
            "<!-- FILE: 10-项目/music/{project}/曲作.md -->\n"
            "---\n"
            "type: composition\n"
            'upstream: "[[创作 vision]], [[词作]]"\n'
            "---\n# 曲作\n<!-- /FILE -->\n"
            "<!-- FILE: 10-项目/music/{project}/Suno-prompt.md -->\n"
            "# Suno\n<!-- /FILE -->\n"
        )
        monkeypatch.setattr(rr, "call_claude", lambda s, u, r: canned)
        try:
            assert run_role("作曲", "写歌", PROJECT).ok
            written = (proj / "曲作.md").read_text(encoding="utf-8")
            fm = yaml.safe_load(written.split("---", 2)[1])
            # 落盘后是两个链接的 list，而不是一个字符串
            assert fm["upstream"] == ["[[创作 vision]]", "[[词作]]"]
            assert fm["type"] == "composition"
            audit = captured["audit"][-1]
            assert len(audit["frontmatter_link_fixes"]) == 1
            assert "曲作.md" in audit["frontmatter_link_fixes"][0]
            assert audit["frontmatter_problems"] == []
        finally:
            _shutil.rmtree(proj, ignore_errors=True)

    def test_clean_output_records_no_fixes(self, runner_env):
        """正确写法不该被改，audit 两个字段留空（避免噪声）。"""
        from engine.role_runner import run_role

        run_role(ROLE, "常规母带", PROJECT)
        audit = runner_env["audit"][-1]
        assert audit["frontmatter_link_fixes"] == []
        assert audit["frontmatter_problems"] == []


class TestMaterialDirScan:
    """素材目录扫描输入源（SE 收编批 1 · 原 PM collect_input_docs 收编）。"""

    def test_material_dir_scan_rules(self, tmp_path):
        """素材目录扫描约定（原 PM collect_input_docs）：排除 + 置顶排序。"""
        from engine.role_runner import _scan_material_dir

        (tmp_path / "zeta.md").write_text("z", encoding="utf-8")
        (tmp_path / "business_brief.md").write_text("brief", encoding="utf-8")
        (tmp_path / "alpha.md").write_text("a", encoding="utf-8")
        (tmp_path / "README.md").write_text("readme", encoding="utf-8")
        (tmp_path / "x.example.md").write_text("ex", encoding="utf-8")
        (tmp_path / ".hidden.md").write_text("h", encoding="utf-8")
        (tmp_path / "empty.md").write_text("  \n", encoding="utf-8")

        names = [p.name for p in _scan_material_dir(tmp_path)]
        assert names == ["business_brief.md", "alpha.md", "zeta.md"]
        assert _scan_material_dir(tmp_path / "不存在") == []

    def test_pm_e2e_material_scan_and_declared_inputs(self, monkeypatch):
        """PM（SE 收编批 1）经 runner：素材扫描进 context + 参考资料强制段 +
        声明 inputs（技术栈/PRD-输出模板）真被消费（修 T2.7 起两处声明-实施漂移）。"""
        import shutil as _shutil

        import engine.role_runner as rr
        from engine.role_runner import run_role

        pm_project = "_runner-pm-unit-test"
        proj = VAULT_ROOT / "10-项目" / pm_project
        (proj / "inputs").mkdir(parents=True, exist_ok=True)
        (proj / "inputs" / "business_brief.md").write_text(
            "# 业务简报\n做一个待办清单 API", encoding="utf-8",
        )
        (proj / "inputs" / "会议纪要.md").write_text(
            "# 纪要\n优先做增删改查", encoding="utf-8",
        )

        captured: dict = {"audit": []}
        monkeypatch.setattr(rr, "role_is_blocked", lambda name: False)
        monkeypatch.setattr(rr, "set_role_status", lambda name, **kw: None)
        monkeypatch.setattr(rr, "append_audit", captured["audit"].append)
        canned = (
            "<!-- FILE: 10-项目/{project}/PRD.md -->\n# PRD\n正文\n<!-- /FILE -->\n"
        )

        def fake_call(system_prompt, user_prompt, role_name):
            captured["user"] = user_prompt
            return canned

        monkeypatch.setattr(rr, "call_claude", fake_call)
        try:
            result = run_role("产品经理", "做待办 API", pm_project, domain="se")
            assert result.ok
            user = captured["user"]
            # 素材扫描进 context（business_brief 置顶由扫描顺序保证）
            assert "做一个待办清单 API" in user
            assert "优先做增删改查" in user
            # 参考资料章节强制段 + 相对链接（原 PM source_list 语义）
            assert "参考资料（Source Materials）" in user
            assert "- `business_brief.md` → `inputs/business_brief.md`" in user
            # 声明 inputs 真被消费：技术栈 + PRD-输出模板（旧 main.py 两处漂移的修复守卫）
            assert "=== 技术栈.md ===" in user
            assert "=== PRD-输出模板.md ===" in user
            audit = captured["audit"][-1]
            assert audit["result"] == "success"
            assert audit["outputs"] == [f"10-项目/{pm_project}/PRD.md"]
            assert audit["runner"] == "in_process"
        finally:
            _shutil.rmtree(proj, ignore_errors=True)

    def test_pm_missing_input_permanent_fail(self, monkeypatch):
        """素材目录空 + task 无效（占位值）→ rc=2 permanent（原 PM missing_input 语义）。"""
        import shutil as _shutil

        import engine.role_runner as rr
        from engine.role_runner import run_role

        pm_project = "_runner-pm-unit-test"
        proj = VAULT_ROOT / "10-项目" / pm_project
        (proj / "inputs").mkdir(parents=True, exist_ok=True)

        captured: dict = {"audit": []}
        monkeypatch.setattr(rr, "role_is_blocked", lambda name: False)
        monkeypatch.setattr(rr, "set_role_status", lambda name, **kw: None)
        monkeypatch.setattr(rr, "append_audit", captured["audit"].append)
        monkeypatch.setattr(
            rr, "call_claude",
            lambda *a: (_ for _ in ()).throw(AssertionError("不应触发 LLM 调用")),
        )
        try:
            result = run_role("产品经理", "", pm_project)
            assert result.returncode == 2 and result.status == "permanent_failed"
            assert "missing_input" in (result.error or "")
            assert captured["audit"][-1]["error"] == "missing_input"
        finally:
            _shutil.rmtree(proj, ignore_errors=True)


class TestAdapterDomainWiring:
    """域 adapter 生产接线（批 1 捆绑项）：模板 domain → adapter 目录名归一化。"""

    def test_adapter_domain_normalization(self):
        from engine.graph.build import _adapter_domain

        assert _adapter_domain("技术开发") == "se"   # SE 模板沿用中文域名
        assert _adapter_domain("music") == "music"
        assert _adapter_domain("se") == "se"
        assert _adapter_domain("") is None
        assert _adapter_domain("  ") is None
