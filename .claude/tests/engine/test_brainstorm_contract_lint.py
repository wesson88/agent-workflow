"""
test_brainstorm_contract_lint.py — 创意脑暴契约层 lint

两层校验：
A. **DAG 校验**：3 角色 inputs/outputs 连贯性（参 [[test_music_contract_lint]] 范式，
   不调 LLM、不跑 engine 主流程，纯静态分析 frontmatter）
B. **Readiness JSON 校验**：按 [[brainstorm-readiness.schema]] §5 规则集校验
   `brainstorm_readiness.json` 的结构 / 类型 / 一致性
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / ".claude" / "skills"))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / ".claude"))

from engine.brainstorm_lint import (
    validate_readiness_json,
    HARD_GATE_FIELDS, SCORE_FIELDS, DECISION_VALUES,
)


# ═══════════════════════════════════════════════════════════
# A. DAG 校验（3 角色 inputs/outputs 连贯）
# ═══════════════════════════════════════════════════════════

BRAINSTORM_ROLES = ("创意发散者", "创意质询者", "创意记录员")


@pytest.fixture(scope="module")
def brainstorm_roles() -> dict:
    """加载 3 个 brainstorm 角色（在 00-系统/角色基因/ 根目录，非子域）。"""
    from engine.config import VAULT_ROOT
    from engine.role_loader import _build_role, invalidate_cache

    invalidate_cache()
    roles: dict = {}
    base = VAULT_ROOT / "00-系统/角色基因"
    for name in BRAINSTORM_ROLES:
        note = base / f"角色-{name}.md"
        assert note.is_file(), f"角色基因缺失：{note}"
        roles[name] = _build_role(note)
    return roles


def _normalize_path(path: str) -> str:
    return path.replace("\\", "/").replace("{project}", "PROJECT")


def _is_source_input(path: str) -> bool:
    norm = path.replace("\\", "/")
    return "/inputs/" in norm or norm.startswith("00-系统/规则/")


class TestRoleInventory:
    def test_three_roles_loaded(self, brainstorm_roles):
        assert set(brainstorm_roles.keys()) == set(BRAINSTORM_ROLES)

    def test_all_business_general_domain(self, brainstorm_roles):
        """3 角色 domain=通用（T2.7 修正：脑暴 3 角色产业务内容，不是元角色）。"""
        for name, role in brainstorm_roles.items():
            assert role.domain == "通用", (
                f"{name} domain 应为 '通用'（脑暴角色产业务内容），"
                f"实际 {role.domain!r}"
            )

    def test_all_cross_domain(self, brainstorm_roles):
        """3 角色 frontmatter 应声明 domains: [se, music, manhua]（跨域复用）。"""
        for name, role in brainstorm_roles.items():
            domains = role.frontmatter.get("domains", [])
            assert "se" in domains and "music" in domains and "manhua" in domains, (
                f"{name} 应跨 se+music+manhua 三域，实际 domains={domains}"
            )


class TestDAGChain:
    """3 角色 inputs/outputs DAG 连贯（线性链 发散→质询→记录员）。"""

    def test_diverger_inputs_only_sources_or_self_loop(self, brainstorm_roles):
        """发散者 inputs 应仅含源输入 + 自循环（上轮原型 / brief）。"""
        diverger = brainstorm_roles["创意发散者"]
        for inp in diverger.inputs:
            norm = _normalize_path(inp)
            # 允许：源输入 / 上轮原型 / 上轮 brief（记录员产出，自循环）
            allowed = (
                _is_source_input(inp)
                or "产品创意原型.md" in norm
                or "rolling_brief.md" in norm
            )
            assert allowed, f"发散者 inputs 异常：{inp}"

    def test_challenger_requires_diverger_output(self, brainstorm_roles):
        """质询者 inputs 必须含 A 方发散产物（创意发散-R*.md）。"""
        challenger = brainstorm_roles["创意质询者"]
        has_diverge_input = any(
            "创意发散" in _normalize_path(inp) for inp in challenger.inputs
        )
        assert has_diverge_input, (
            f"质询者 inputs 缺 A 方发散产物，实际：{challenger.inputs}"
        )

    def test_scribe_requires_both_diverger_and_challenger_outputs(self, brainstorm_roles):
        """记录员 inputs 必须含 A 方发散 + B 方质询双产物。"""
        scribe = brainstorm_roles["创意记录员"]
        has_diverge = any("创意发散" in _normalize_path(i) for i in scribe.inputs)
        has_challenge = any("创意质询" in _normalize_path(i) for i in scribe.inputs)
        assert has_diverge, f"记录员 inputs 缺 A 方发散产物：{scribe.inputs}"
        assert has_challenge, f"记录员 inputs 缺 B 方质询产物：{scribe.inputs}"

    def test_scribe_outputs_three_artifacts(self, brainstorm_roles):
        """记录员 outputs 必须包含 3 类产物（原型 / readiness / brief）。"""
        scribe = brainstorm_roles["创意记录员"]
        outs = [_normalize_path(o) for o in scribe.outputs]
        expected_suffixes = (
            "产品创意原型.md",
            "brainstorm_readiness.json",
            "rolling_brief.md",
        )
        for suffix in expected_suffixes:
            assert any(suffix in o for o in outs), (
                f"记录员 outputs 缺 {suffix}：{scribe.outputs}"
            )

    def test_upstream_field_matches_dag(self, brainstorm_roles):
        """upstream 字段声明应与 DAG 一致：
        - 发散者 upstream=[]
        - 质询者 upstream 含 '创意发散者'
        - 记录员 upstream 含 '创意发散者' + '创意质询者'
        """
        roles = brainstorm_roles
        upstream = lambda r: r.frontmatter.get("upstream", [])
        assert upstream(roles["创意发散者"]) == [], (
            f"发散者 upstream 应为 []，实际 {upstream(roles['创意发散者'])}"
        )
        assert "创意发散者" in upstream(roles["创意质询者"]), (
            f"质询者 upstream 缺创意发散者，实际 {upstream(roles['创意质询者'])}"
        )
        scribe_up = upstream(roles["创意记录员"])
        assert "创意发散者" in scribe_up and "创意质询者" in scribe_up, (
            f"记录员 upstream 应含两者，实际 {scribe_up}"
        )


class TestReadinessSchemaWikilink:
    """rule_refs 应指向 [[产品创意原型schema]]（按 T2.1 整改后约定）。"""

    def test_three_roles_rule_refs_present(self, brainstorm_roles):
        """3 角色都应有 rule_refs 字段（值可空但应列出）。"""
        for name, role in brainstorm_roles.items():
            assert isinstance(role.rule_refs, tuple), (
                f"{name} rule_refs 类型错：{type(role.rule_refs)}"
            )


# ═══════════════════════════════════════════════════════════
# B. Readiness JSON 校验
# ═══════════════════════════════════════════════════════════

def _valid_readiness() -> dict:
    """完整合法 readiness JSON（schema §1 实例的最小可通过版本）。

    scores 全 3 → sum=30 → readiness=60，未达 85 → ready_for_prd=false → continue。
    """
    return {
        "ready_for_prd": False,
        "prd_readiness": 60,
        "hard_gate": {f: True for f in HARD_GATE_FIELDS},
        "scores": {f: 3 for f in SCORE_FIELDS},
        "blocking_gaps": ["MVP 边界需细化"],
        "next_round_focus": ["MVP 边界"],
        "questions_for_user": [],
        "decision": "continue_discussion",
    }


def _ready_for_prd_readiness() -> dict:
    """完整就绪状态：scores 全 5 → 100，hard_gate 全 true → ready。"""
    return {
        "ready_for_prd": True,
        "prd_readiness": 100,
        "hard_gate": {f: True for f in HARD_GATE_FIELDS},
        "scores": {f: 5 for f in SCORE_FIELDS},
        "blocking_gaps": [],
        "next_round_focus": [],
        "questions_for_user": [],
        "decision": "ready_for_prd",
    }


class TestReadinessHappyPath:
    def test_valid_continue_discussion(self):
        assert validate_readiness_json(_valid_readiness()) == []

    def test_valid_ready_for_prd(self):
        assert validate_readiness_json(_ready_for_prd_readiness()) == []

    def test_valid_ask_user(self):
        data = _valid_readiness()
        data["decision"] = "ask_user"
        data["questions_for_user"] = ["是否必须支持账号体系？"]
        assert validate_readiness_json(data) == []

    def test_valid_stop_low_value(self):
        data = _valid_readiness()
        data["decision"] = "stop_low_value"
        data["next_round_focus"] = []  # stop 时不需要 next_round
        assert validate_readiness_json(data) == []


class TestReadinessRequiredFields:
    def test_missing_top_level_field(self):
        data = _valid_readiness()
        del data["decision"]
        errs = validate_readiness_json(data)
        assert any("缺顶层字段：decision" in e for e in errs)

    def test_missing_hard_gate_field(self):
        data = _valid_readiness()
        del data["hard_gate"]["mvp_scope"]
        errs = validate_readiness_json(data)
        assert any("hard_gate 缺字段：mvp_scope" in e for e in errs)

    def test_missing_score_field(self):
        data = _valid_readiness()
        del data["scores"]["mvp_boundary"]
        errs = validate_readiness_json(data)
        assert any("scores 缺字段：mvp_boundary" in e for e in errs)

    def test_non_dict_input(self):
        errs = validate_readiness_json("not a dict")
        assert any("顶层必须是 dict" in e for e in errs)


class TestReadinessTypes:
    def test_ready_for_prd_must_be_bool(self):
        data = _valid_readiness()
        data["ready_for_prd"] = "false"
        errs = validate_readiness_json(data)
        assert any("ready_for_prd 必须是 bool" in e for e in errs)

    def test_prd_readiness_out_of_range(self):
        data = _valid_readiness()
        data["prd_readiness"] = 150
        errs = validate_readiness_json(data)
        assert any("prd_readiness=150 越界" in e for e in errs)

    def test_score_out_of_range(self):
        data = _valid_readiness()
        data["scores"]["feasibility"] = 7
        errs = validate_readiness_json(data)
        assert any("scores.feasibility=7 越界" in e for e in errs)

    def test_hard_gate_must_be_bool(self):
        data = _valid_readiness()
        data["hard_gate"]["mvp_scope"] = "true"
        errs = validate_readiness_json(data)
        assert any("hard_gate.mvp_scope 必须是 bool" in e for e in errs)

    def test_decision_must_be_in_enum(self):
        data = _valid_readiness()
        data["decision"] = "go_for_it"
        errs = validate_readiness_json(data)
        assert any("decision='go_for_it' 非法" in e for e in errs)

    def test_blocking_gaps_must_be_list_of_str(self):
        data = _valid_readiness()
        data["blocking_gaps"] = ["ok", 42]
        errs = validate_readiness_json(data)
        assert any("blocking_gaps 必须是 list[str]" in e for e in errs)


class TestReadinessConsistency:
    def test_ready_true_with_hard_gate_false_fails(self):
        """§5.3-1：ready_for_prd=true 但 hard_gate 有 false → fail。"""
        data = _ready_for_prd_readiness()
        data["hard_gate"]["mvp_scope"] = False
        errs = validate_readiness_json(data)
        assert any("ready_for_prd=true 但 hard_gate 存在 false" in e for e in errs)

    def test_ready_true_with_low_readiness_fails(self):
        """§5.3-2：ready_for_prd=true 但 prd_readiness < 85 → fail。"""
        data = _ready_for_prd_readiness()
        data["scores"] = {f: 4 for f in SCORE_FIELDS}  # sum=40 → 80
        data["prd_readiness"] = 80
        errs = validate_readiness_json(data)
        assert any("prd_readiness=80 < 85" in e for e in errs)

    def test_decision_ready_but_ready_false_fails(self):
        """§5.3-3：decision=ready_for_prd 但 ready_for_prd=false → fail。"""
        data = _valid_readiness()
        data["decision"] = "ready_for_prd"
        errs = validate_readiness_json(data)
        assert any("decision=ready_for_prd 但 ready_for_prd=false" in e for e in errs)

    def test_ask_user_empty_questions_fails(self):
        """§5.3-4：decision=ask_user 但 questions_for_user 空 → fail。"""
        data = _valid_readiness()
        data["decision"] = "ask_user"
        data["questions_for_user"] = []
        errs = validate_readiness_json(data)
        assert any("decision=ask_user 但 questions_for_user 为空" in e for e in errs)

    def test_continue_empty_next_round_fails(self):
        """§5.3-5：decision=continue_discussion 但 next_round_focus 空 → fail。"""
        data = _valid_readiness()
        data["next_round_focus"] = []
        errs = validate_readiness_json(data)
        assert any("next_round_focus 为空" in e for e in errs)

    def test_readiness_score_mismatch_fails(self):
        """§5.3-6：prd_readiness 与 sum(scores)/50*100 误差 > 2 → fail。"""
        data = _valid_readiness()
        # scores 全 3 → sum=30 → expected=60；故意写 70
        data["prd_readiness"] = 70
        errs = validate_readiness_json(data)
        assert any("误差" in e and "> 2" in e for e in errs)

    def test_readiness_within_tolerance_passes(self):
        """§5.3-6：误差 ≤ 2 允许（整数 round 偏差）。"""
        data = _valid_readiness()
        # scores 全 3 → sum=30 → expected=60；写 62 应通过
        data["prd_readiness"] = 62
        assert validate_readiness_json(data) == []
