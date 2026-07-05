"""
test_se_contract_lint.py — SE 域角色契约层 lint（静态 DAG 校验）

参照 [[test_music_contract_lint]] 范式，校验 vault `00-系统/角色基因/se/` 下 ship
角色的 inputs/outputs 连贯性 + rule_refs 真消费 + skill 映射完整。

不调 LLM、不跑 engine 主流程，纯静态分析 frontmatter + skill main.py 源码。

历史背景（2026-06-09 §3.4 实施时发现）：
  路线图 §3.1 声称"SE 角色 rule_refs 已接通"，但实际只有架构师真正实施了
  load_rule_block 调用，TL/dev_backend/dev_frontend 三个 skill main.py 漏了
  实施 — F-* 索引段从未真正注入 LLM context。

  本 lint 防止"声明已写、实施未补"再发生（音乐域已有同类问题 2026-05-30 触发）。

ship 角色判定：frontmatter.role 字段 ∈ SHIP_ROLE_TO_SKILL 映射键。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from engine.config import VAULT_ROOT, PROJECT_ROOT
from engine.role_loader import _build_role, invalidate_cache


SE_DIR = "00-系统/角色基因/se"

# SE ship 角色 → skill 目录映射（新 ship 角色更新此映射）
SHIP_ROLE_TO_SKILL = {
    "产品经理": "product_manager",
    "架构师": "chief_architect",
    "技术主管": "technical_lead",
    "后端工程师": "dev_backend",
    "前端工程师": "dev_frontend",
}

# 架构师有本地 _load_rule_block（已实战 5+ 项目稳定，2026-05-30 commit 20f2fba
# 切到 common.load_rule_block 统一维护）。本 lint 既接受 common 路径也接受本地路径。
_LOAD_RULE_BLOCK_PATTERNS = (
    "load_rule_block",      # common 公开接口（音乐域 + 本次 SE 修复用）
    "_load_rule_block",     # 历史本地实现（架构师早期 + 兼容）
)


def _load_se_roles() -> dict[str, dict]:
    """加载 se/ 下所有角色，返回 {role_name: {role, ship, ...}}。"""
    invalidate_cache()
    roles: dict[str, dict] = {}
    se_path = VAULT_ROOT / SE_DIR
    assert se_path.is_dir(), f"se 角色目录不存在：{se_path}"
    for note in sorted(se_path.glob("角色-*.md")):
        role = _build_role(note)
        is_ship = role.name in SHIP_ROLE_TO_SKILL
        roles[role.name] = {"role": role, "ship": is_ship}
    return roles


def _is_source_input(path: str) -> bool:
    """源输入：业务简报 / inputs 素材 / 规则文件，不需要在上游 outputs 中。"""
    norm = path.replace("\\", "/")
    return (
        "/inputs/" in norm
        or norm.startswith("00-系统/规则/")
    )


def _normalize_path(path: str) -> str:
    return path.replace("\\", "/").replace("{project}", "PROJECT")


def _build_producer_index(ship_roles: dict[str, dict]) -> dict[str, str]:
    producers: dict[str, str] = {}
    for name, data in ship_roles.items():
        for out in data["role"].outputs:
            producers.setdefault(_normalize_path(out), name)
    return producers


# ── fixture ──────────────────────────────────────────────
@pytest.fixture(scope="module")
def se_roles() -> dict[str, dict]:
    return _load_se_roles()


@pytest.fixture(scope="module")
def ship_roles(se_roles) -> dict[str, dict]:
    return {n: d for n, d in se_roles.items() if d["ship"]}


# ── 测试 ──────────────────────────────────────────────────
class TestRoleInventory:
    """ship 角色与 SHIP_ROLE_TO_SKILL 映射一致。"""

    def test_all_ship_roles_loaded(self, ship_roles):
        missing = [n for n in SHIP_ROLE_TO_SKILL if n not in ship_roles]
        assert not missing, (
            f"SHIP_ROLE_TO_SKILL 中的角色缺失基因文件：{missing}"
        )

    def test_skill_dirs_exist(self):
        for name, skill_dir in SHIP_ROLE_TO_SKILL.items():
            path = PROJECT_ROOT / ".claude" / "skills" / skill_dir / "main.py"
            assert path.is_file(), f"{name} → {skill_dir}/main.py 不存在"


class TestInputsProducerChain:
    """ship 角色 inputs 中的每个产物必须有 ship 上游 outputs 声明（除源输入）。"""

    def test_all_inputs_have_producer(self, ship_roles):
        producers = _build_producer_index(ship_roles)
        broken: list[str] = []
        for name, data in ship_roles.items():
            for inp in data["role"].inputs:
                if _is_source_input(inp):
                    continue
                norm = _normalize_path(inp)
                if norm not in producers:
                    broken.append(f"  {name} ← {inp}（未找到 ship 角色 outputs）")
        assert not broken, (
            "inputs 找不到上游 producer（拼写漂移 / 角色未 ship）：\n"
            + "\n".join(broken)
        )


class TestUpstreamFieldConsistency:
    """upstream 字段声明的角色必须真是 inputs 来源（避免编造假上游）。

    与音乐域 [[test_music_contract_lint]] 同语义：declared ⊆ inputs producer 集合。
    """

    def test_declared_upstream_all_have_producer(self, ship_roles):
        producers = _build_producer_index(ship_roles)
        invalid: list[str] = []
        for name, data in ship_roles.items():
            role = data["role"]
            producer_set: set[str] = set()
            for inp in role.inputs:
                if _is_source_input(inp):
                    continue
                norm = _normalize_path(inp)
                producer = producers.get(norm)
                if producer and producer != name:
                    producer_set.add(producer)
            declared = set(role.upstream)
            fake = declared - producer_set
            if fake:
                invalid.append(
                    f"  {name}: declared upstream {sorted(fake)} 不在 inputs "
                    f"来源 {sorted(producer_set)} 中（编造假上游）"
                )
        assert not invalid, (
            "upstream 字段声明的角色不是真上游：\n" + "\n".join(invalid)
        )


class TestRuleRefsConsumption:
    """角色 frontmatter 声明的 rule_refs 必须被对应 skill main.py 真消费。

    本 lint 是 §3.4 SE contract lint 的核心验证项（2026-06-09 §3.4 实施时
    发现 TL/dev_backend/dev_frontend 三个 skill 漏了 load_rule_block 实施）。

    防 "声明未实施" 再发生：扫每个 ship 角色对应的 skill main.py，必须满足：
      1. import 了 load_rule_block 或本地 _load_rule_block 等价 helper
      2. 调用了 load_rule_block(<role>.rule_refs) 或等价 pattern

    架构师本地 _load_rule_block 已 W3 实战 5+ 项目稳定，本 lint 既接受 common
    路径也接受本地 _load_rule_block 路径。
    """

    @staticmethod
    def _skill_main_text(skill_dir: str) -> str | None:
        path = PROJECT_ROOT / ".claude" / "skills" / skill_dir / "main.py"
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8")

    def test_skills_import_load_rule_block(self, ship_roles):
        """skill main.py 必须 import load_rule_block（防 frontmatter 写了 rule_refs 但实施缺失）。"""
        not_imported: list[str] = []
        for name, data in ship_roles.items():
            if not data["role"].rule_refs:
                continue
            skill_dir = SHIP_ROLE_TO_SKILL[name]
            text = self._skill_main_text(skill_dir) or ""
            if not any(p in text for p in _LOAD_RULE_BLOCK_PATTERNS):
                not_imported.append(
                    f"  {name} → {skill_dir}/main.py 未引用 load_rule_block "
                    f"（frontmatter rule_refs={list(data['role'].rule_refs)}）"
                )
        assert not not_imported, (
            "skill main.py 未引用 load_rule_block，rule_refs 章节注入未实施：\n"
            + "\n".join(not_imported)
        )

    def test_skills_invoke_load_rule_block(self, ship_roles):
        """skill main.py 必须真调用 load_rule_block(<role>.rule_refs)（光 import 不调用等同未实施）。"""
        # 匹配 `load_rule_block(<var>.rule_refs)` 或 `_load_rule_block(<var>.rule_refs)`
        call_re = re.compile(r"_?load_rule_block\s*\(\s*\w+\.rule_refs\b")
        not_invoked: list[str] = []
        for name, data in ship_roles.items():
            if not data["role"].rule_refs:
                continue
            skill_dir = SHIP_ROLE_TO_SKILL[name]
            text = self._skill_main_text(skill_dir) or ""
            if not call_re.search(text):
                not_invoked.append(
                    f"  {name} → {skill_dir}/main.py 未调用 "
                    f"load_rule_block(<role>.rule_refs)"
                )
        assert not not_invoked, (
            "skill main.py 未调用 load_rule_block(role.rule_refs)，章节注入未实施：\n"
            + "\n".join(not_invoked)
        )


class TestSkillRefsConsistency:
    """skill_refs 路径必须真实存在于 vault（防止 frontmatter 写错路径）。"""

    def test_all_skill_refs_resolve(self, ship_roles):
        broken: list[str] = []
        for name, data in ship_roles.items():
            for rel in data["role"].skill_refs:
                path = VAULT_ROOT / rel
                if not path.is_file():
                    broken.append(f"  {name} → {rel}（vault 文件不存在）")
        assert not broken, (
            "skill_refs 引用的 vault 文件不存在：\n" + "\n".join(broken)
        )


class TestFstarIndexExists:
    """F-* 索引文件存在且每个 ship 后端/前端/架构/TL 角色的 rule_refs 至少引用一个 F-*。

    F-* 索引是 §3.1 路线图核心产物，作为下游 LLM 看到的"可用 skill 清单"。
    架构师、TL、后端、前端必须通过 rule_refs 注入 F-*；产品经理不强制。
    """

    F_STAR_FILES = ["F-架构.md", "F-技术主管.md", "F-后端.md", "F-前端.md"]
    ROLES_REQUIRE_F_STAR = {"架构师", "技术主管", "后端工程师", "前端工程师"}

    def test_fstar_index_files_exist(self):
        for f in self.F_STAR_FILES:
            path = VAULT_ROOT / "20-知识" / "角色技能" / "se" / f
            assert path.is_file(), f"F-* 索引缺失：{path}"

    def test_se_roles_reference_fstar(self, ship_roles):
        missing: list[str] = []
        for name in self.ROLES_REQUIRE_F_STAR:
            role = ship_roles[name]["role"]
            has_fstar = any("F-" in ref for ref in role.rule_refs)
            if not has_fstar:
                missing.append(
                    f"  {name}: rule_refs 未引用任何 F-* 索引段；现有 rule_refs="
                    f"{list(role.rule_refs)}"
                )
        assert not missing, (
            "SE ship 角色应通过 rule_refs 引用 F-* 索引（§3.1 路线图）：\n"
            + "\n".join(missing)
        )


class TestContractSchema:
    """契约化角色的 output_contract / input_contract 必须满足规范 §11 + §7.1。

    P4（2026-07-04）：TL output_contract shadow declaration
    P7（2026-07-04）：Backend / Frontend input_contract shadow declaration

    校验维度：
    1. parameterizable=true（§7.1 不可豁免项）
    2. fields ≥ 1 项 + 每项含 type
    3. templates ≥ 1 项 + 每项含 outputs / inputs 列表
    4. templates 里的 fields 占位符必须在 fields 声明中（§11.7 反例 2）
    5. templates.legacy_directives（若存在）展开默认 field values 后
       与 frontmatter.outputs / inputs 语义等价（模块 ID 抽象化后集合相等）
    """

    # role → 契约类型（output_contract 或 input_contract）
    ROLE_CONTRACTS: dict[str, str] = {
        "技术主管": "output_contract",       # P4
        "后端工程师": "input_contract",       # P7
        "前端工程师": "input_contract",       # P7
    }

    @staticmethod
    def _contract_paths_key(contract_type: str) -> str:
        """output_contract → 'outputs'；input_contract → 'inputs'。"""
        return "outputs" if contract_type == "output_contract" else "inputs"

    @staticmethod
    def _declared_paths(role, contract_type: str) -> tuple[str, ...]:
        """从 Role 对象读对应的 outputs 或 inputs 字段。"""
        return role.outputs if contract_type == "output_contract" else role.inputs

    @staticmethod
    def _abstract_module_id(path: str) -> str:
        """T01 / T0N / T{n} / {module_id_template} 归一为 T*。"""
        norm = path.replace("\\", "/").replace("{project}", "PROJECT")
        norm = norm.replace("{module_id_template}", "T{n}")
        return re.sub(r"T(0N|\{n\}|0?\d+)", "T*", norm)

    def _iter_contracts(self, ship_roles):
        """产出 (role_name, contract_type, contract_dict, role_obj) 四元组。"""
        for name, contract_type in self.ROLE_CONTRACTS.items():
            role = ship_roles[name]["role"]
            contract = role.frontmatter.get(contract_type) or {}
            yield name, contract_type, contract, role

    def test_contract_roles_have_contract(self, ship_roles):
        """预期契约化的角色必须真的有对应契约声明。"""
        missing = [
            f"{name}.{ctype}"
            for name, ctype in self.ROLE_CONTRACTS.items()
            if not ship_roles[name]["role"].frontmatter.get(ctype)
        ]
        assert not missing, f"预期契约化角色缺契约声明：{missing}"

    def test_contract_parameterizable(self, ship_roles):
        """契约字段必须显式 parameterizable=true（§7.1 不可豁免项）。"""
        broken: list[str] = []
        for name, ctype, contract, _ in self._iter_contracts(ship_roles):
            if contract.get("parameterizable") is not True:
                broken.append(f"  {name}.{ctype}: parameterizable 缺失或非 true")
        assert not broken, "契约化角色 parameterizable 未声明：\n" + "\n".join(broken)

    def test_contract_fields_and_templates_non_empty(self, ship_roles):
        """§7.1：契约化角色必须同时含 fields ≥ 1 项 + templates ≥ 1 项。"""
        broken: list[str] = []
        for name, ctype, contract, _ in self._iter_contracts(ship_roles):
            if not (contract.get("fields") or {}):
                broken.append(f"  {name}.{ctype}.fields 为空")
            if not (contract.get("templates") or {}):
                broken.append(f"  {name}.{ctype}.templates 为空")
        assert not broken, "契约完整性违反：\n" + "\n".join(broken)

    def test_contract_fields_have_type(self, ship_roles):
        """每个 field 必须声明 type（§11.2 schema 必填）。"""
        broken: list[str] = []
        for name, ctype, contract, _ in self._iter_contracts(ship_roles):
            for field_name, spec in (contract.get("fields") or {}).items():
                if not spec.get("type"):
                    broken.append(f"  {name}.{ctype}.{field_name}: 缺 type")
        assert not broken, "field 缺 type 字段：\n" + "\n".join(broken)

    def test_contract_templates_have_paths_list(self, ship_roles):
        """每个 template 必须含对应的 outputs / inputs 列表。"""
        broken: list[str] = []
        for name, ctype, contract, _ in self._iter_contracts(ship_roles):
            key = self._contract_paths_key(ctype)
            for tpl_name, tpl in (contract.get("templates") or {}).items():
                paths = tpl.get(key)
                if not paths or not isinstance(paths, list):
                    broken.append(
                        f"  {name}.{ctype}.templates.{tpl_name}.{key}: 缺失或非 list"
                    )
        assert not broken, "template 路径结构非法：\n" + "\n".join(broken)

    def test_contract_placeholders_declared_in_fields(self, ship_roles):
        """§11.7 反例 2：templates 里用的 {field_name} 占位符必须在 fields 声明。

        允许 builtin 占位符：由 workflow bindings 提供。
        """
        BUILTIN_PLACEHOLDERS = {
            "project", "date", "n", "current_module_id",
            "title_slug", "ts", "role", "domain",
        }
        placeholder_re = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
        broken: list[str] = []
        for name, ctype, contract, _ in self._iter_contracts(ship_roles):
            declared_fields = set((contract.get("fields") or {}).keys())
            allowed = declared_fields | BUILTIN_PLACEHOLDERS
            key = self._contract_paths_key(ctype)
            for tpl_name, tpl in (contract.get("templates") or {}).items():
                for path in tpl.get(key, []):
                    for placeholder in placeholder_re.findall(str(path)):
                        if placeholder not in allowed:
                            broken.append(
                                f"  {name}.{ctype}.templates.{tpl_name}: 占位符 "
                                f"{{{placeholder}}} 未在 fields 声明"
                            )
        assert not broken, "占位符未声明：\n" + "\n".join(broken)

    def test_legacy_template_matches_declared(self, ship_roles):
        """§11.5 向后兼容：契约化角色的 outputs / inputs 字段应等价于 legacy_directives
        template（或首个 template）+ 默认 field 值展开（模块 ID 抽象化后集合相等）。
        """
        broken: list[str] = []
        for name, ctype, contract, role in self._iter_contracts(ship_roles):
            templates = contract.get("templates") or {}
            legacy = templates.get("legacy_directives") or (
                templates[next(iter(templates))] if templates else None
            )
            if not legacy:
                continue
            key = self._contract_paths_key(ctype)
            template_paths = {
                self._abstract_module_id(p) for p in legacy.get(key, [])
            }
            declared = {
                self._abstract_module_id(p) for p in self._declared_paths(role, ctype)
            }
            missing_in_template = declared - template_paths
            extra_in_template = template_paths - declared
            if missing_in_template or extra_in_template:
                detail: list[str] = []
                if missing_in_template:
                    detail.append(
                        f"    {key} 中未被 template 覆盖：{sorted(missing_in_template)}"
                    )
                if extra_in_template:
                    detail.append(
                        f"    template 展开出 {key} 未声明的路径：{sorted(extra_in_template)}"
                    )
                broken.append(
                    f"  {name}.{ctype} 契约展开与 {key} 不等价：\n" + "\n".join(detail)
                )
        assert not broken, "契约首个 template 与 declared 路径语义不等价：\n" + "\n".join(broken)
