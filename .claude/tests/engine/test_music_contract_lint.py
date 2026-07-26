"""
test_music_contract_lint.py — 音乐域角色契约层 lint（静态 DAG 校验）

校验 vault `00-系统/角色基因/music/` 下 ship 角色的 inputs/outputs 连贯性。
不调 LLM、不跑 engine 主流程，纯静态分析 frontmatter。

W5 首项目实战会自然触发完整端到端测试，本 lint 只保契约层 DAG
（捕获拼写漂移 / 上游产物路径与下游 inputs 不对齐 / upstream 字段与
inputs 推断不一致 等静态错误）。

ship vs probational 判定：frontmatter.status 含 "probational" → probational
（Phase 1 dormant，不参与 DAG 校验）。

源输入路径约定：
  - `10-项目/music/{project}/inputs/创作简报.md` — 用户原始简报
  - `00-系统/规则/music/*.md` — 规则文件
  二者不要求在上游音乐角色的 outputs 中。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from engine.config import VAULT_ROOT, PROJECT_ROOT
from engine.role_loader import _build_role, invalidate_cache


MUSIC_DIR = "00-系统/角色基因/music"

# 8 ship 音乐角色 → skill 目录映射（W3 P0c 收尾后冻结；新 ship 角色更新此映射）
SHIP_ROLE_TO_SKILL = {
    "音乐总监": "music_director",
    "制作人": "music_producer",
    "作词": "music_lyricist",
    "作曲": "music_composer",
    "和声编写": "music_vocal_arranger",
    "编曲": "music_arranger",
    "混音师": "music_mixing_engineer",
    "母带工程师": "music_mastering_engineer",
}


def _load_music_roles() -> dict[str, dict]:
    """加载 music/ 下所有角色，返回 {role_name: {role, ship, ...}}。"""
    invalidate_cache()
    roles: dict[str, dict] = {}
    music_path = VAULT_ROOT / MUSIC_DIR
    assert music_path.is_dir(), f"music 角色目录不存在：{music_path}"
    for note in sorted(music_path.glob("角色-*.md")):
        role = _build_role(note)
        status = str(role.frontmatter.get("status", "")).strip()
        is_probational = "probational" in status
        roles[role.name] = {
            "role": role,
            "ship": not is_probational,
            "status": status,
        }
    return roles


def _is_source_input(path: str) -> bool:
    """源输入：用户原始简报或规则文件，不需要在上游 outputs 中。"""
    norm = path.replace("\\", "/")
    return (
        "/inputs/" in norm
        or norm.startswith("00-系统/规则/")
    )


def _normalize_path(path: str) -> str:
    """规范化路径：去 {project} 占位符 + 统一斜杠。"""
    return path.replace("\\", "/").replace("{project}", "PROJECT")


def _expand_role_template(path: str, role_names: list[str]) -> list[str]:
    """展开 `{角色}` 模板路径为多个具体路径（按下游角色名替换）。

    无 `{角色}` 占位符 → 原路径单元素列表。
    """
    if "{角色}" not in path:
        return [path]
    return [path.replace("{角色}", name) for name in role_names]


def _build_producer_index(ship_roles: dict[str, dict]) -> dict[str, str]:
    """{normalized_path: producer_role_name}；制作人含 {角色} 模板自动展开。"""
    role_names = list(ship_roles.keys())
    producers: dict[str, str] = {}
    for name, data in ship_roles.items():
        for out in data["role"].outputs:
            for expanded in _expand_role_template(out, role_names):
                norm = _normalize_path(expanded)
                producers.setdefault(norm, name)
    return producers


# ── fixture ──────────────────────────────────────────────
@pytest.fixture(scope="module")
def music_roles() -> dict[str, dict]:
    return _load_music_roles()


@pytest.fixture(scope="module")
def ship_roles(music_roles) -> dict[str, dict]:
    return {n: d for n, d in music_roles.items() if d["ship"]}


# ── 测试 ──────────────────────────────────────────────────
class TestRoleInventory:
    """ship/probational 分布与 W3 P0c 完成状态一致。"""

    def test_loaded_at_least_8_ship_roles(self, ship_roles):
        # W2 4 核心 + W3 P0c 4 后续核心 = 8 ship
        assert len(ship_roles) >= 8, (
            f"音乐域 ship 角色应 ≥ 8（W2+W3 P0c），实际 {len(ship_roles)}："
            f"{sorted(ship_roles)}"
        )

    def test_w2_4_cores_present(self, ship_roles):
        for name in ("音乐总监", "制作人", "作词", "作曲"):
            assert name in ship_roles, f"W2 核心角色缺失：{name}"

    def test_w3_4_cores_present(self, ship_roles):
        for name in ("和声编写", "编曲", "混音师", "母带工程师"):
            assert name in ship_roles, f"W3 P0c 后续核心角色缺失：{name}"

    def test_probational_dormant(self, music_roles):
        """probational 角色存在但 status 含 probational。"""
        for name in ("录音师", "MIDI编程", "A&R"):
            assert name in music_roles, f"probational 角色缺失：{name}"
            assert not music_roles[name]["ship"], (
                f"{name} 应为 probational，实际 status={music_roles[name]['status']!r}"
            )


class TestInputsProducerChain:
    """ship 角色 inputs 中的每个产物必须在某 ship 上游 outputs 中（除源输入 / 规则）。"""

    def test_all_inputs_have_producer(self, ship_roles):
        producers = _build_producer_index(ship_roles)

        broken: list[str] = []
        for name, data in ship_roles.items():
            for inp in data["role"].inputs:
                if _is_source_input(inp):
                    continue
                norm = _normalize_path(inp)
                if norm not in producers:
                    broken.append(
                        f"  {name} ← {inp}（未找到 ship 角色 outputs 中声明此产物）"
                    )

        assert not broken, (
            "inputs 找不到上游 producer（拼写漂移 / 角色未 ship）：\n"
            + "\n".join(broken)
        )

    def test_instruction_files_produced_by_producer(self, ship_roles):
        """指令/给X.md 类输入文件必须由制作人 outputs 覆盖。"""
        # 制作人 outputs 含 `指令/给-{角色}.md` 通配模板，对所有下游有效
        producer = ship_roles.get("制作人")
        assert producer, "制作人角色缺失"
        producer_outs = [_normalize_path(o) for o in producer["role"].outputs]
        has_instruction_template = any(
            "指令/" in o and ("给-" in o or "给{" in o) for o in producer_outs
        )
        assert has_instruction_template, (
            f"制作人 outputs 应含通配指令模板（如 指令/给-{{角色}}.md），"
            f"实际：{producer_outs}"
        )

        # 所有 ship 下游角色 inputs 中的"指令/给X.md"视为合法（由制作人覆盖）


class TestSourceBriefReferences:
    """W2 4 核心必须引用源简报 inputs/创作简报.md。"""

    def test_w2_cores_reference_brief(self, ship_roles):
        brief_path = "10-项目/music/{project}/inputs/创作简报.md"
        for name in ("音乐总监", "制作人", "作词", "作曲"):
            inputs = ship_roles[name]["role"].inputs
            assert brief_path in inputs, (
                f"{name}（W2 核心）应引用源简报 `{brief_path}`，"
                f"实际 inputs：{inputs}"
            )

    def test_w3_cores_do_not_reference_brief(self, ship_roles):
        """W3 4 后续核心不直接引用简报（由上游 W2 角色消化转译）。"""
        brief_path = "10-项目/music/{project}/inputs/创作简报.md"
        for name in ("和声编写", "编曲", "混音师", "母带工程师"):
            inputs = ship_roles[name]["role"].inputs
            assert brief_path not in inputs, (
                f"{name}（W3 后续核心）不应直接引用源简报，"
                f"应由 W2 上游消化转译；实际 inputs：{inputs}"
            )


class TestUpstreamFieldConsistency:
    """upstream 字段声明的角色必须真是 inputs 来源（避免编造假上游）。

    **语义边界**：upstream = "直接调度上游"（数据流主导依赖），不要求列所有 inputs
    producer。例如作词 inputs 含 vision（音乐总监产）和给作词指令（制作人产），但
    upstream 只声明 [制作人]，因为 vision 是经制作人转译注入的间接依赖。

    本测试仅校验反向：declared ⊆ inputs producer 集合（声明的都真有来源），
    不要求 inputs producer 全集 ⊆ declared（那是设计取舍不是错误）。
    """

    def test_declared_upstream_all_have_producer(self, ship_roles):
        producers = _build_producer_index(ship_roles)

        invalid: list[str] = []
        for name, data in ship_roles.items():
            role = data["role"]
            # 从 inputs 推断的"可作为上游"集合
            producer_set: set[str] = set()
            for inp in role.inputs:
                if _is_source_input(inp):
                    continue
                norm = _normalize_path(inp)
                producer = producers.get(norm)
                if producer and producer != name:
                    producer_set.add(producer)

            declared = set(role.upstream)
            fake_upstream = declared - producer_set
            if fake_upstream:
                invalid.append(
                    f"  {name}: declared upstream {sorted(fake_upstream)} "
                    f"不在 inputs 来源 {sorted(producer_set)} 中（编造假上游）"
                )

        assert not invalid, (
            "upstream 字段声明的角色不是真上游：\n" + "\n".join(invalid)
        )


class TestRuleRefsConsumption:
    """角色 frontmatter 声明的 rule_refs 必须被对应 skill main.py 真消费。

    历史背景（2026-05-30 发现）：W3 P0c 给 8 音乐角色 frontmatter 加了 rule_refs
    章节级注入声明，但 skill main.py 端 4 天都没补对应的 load_rule_block 调用
    实施 —— 整套 "schema 化省 token" 机制在"声明已写、实施未补"状态运行了 4 天，
    首项目 8 LLM 产物全靠 LLM 自身音乐知识凑出来，schema 章节内容从未真注入。

    本 lint 防 "声明未实施" 再发生：扫每个 ship 角色对应的 skill main.py，
    必须同时满足：
      1. import 了 `load_rule_block`（from common import ... load_rule_block ...）
      2. 调用了 `load_rule_block(role_def.rule_refs)` 或等价 pattern

    2026-07-26 CLI 壳瘦身后的边界：`executor: in_process` 角色的 main.py 已
    瘦为 invoke_role 薄壳，rule_refs 消费单点在 engine.role_runner（run_role →
    ability_loader.assemble_user_context 无条件注入，engine 侧测试覆盖），
    main.py 文本扫描对这类角色不再适用 → 跳过。仍走 subprocess 的角色
    （音乐总监）继续受本 lint 约束。

    架构师（chief_architect）有自己的本地 `_load_rule_block`（实战 5+ 项目稳定），
    本测试不覆盖 SE 域；后续 SE 域产物 schema 化时另写 test_se_contract_lint。
    """

    @staticmethod
    def _skill_main_text(skill_dir: str) -> str | None:
        path = PROJECT_ROOT / ".claude" / "skills" / skill_dir / "main.py"
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8")

    def test_ship_roles_with_rule_refs_have_skill(self, ship_roles):
        """每个 ship 角色都应该有 SHIP_ROLE_TO_SKILL 映射 + 对应 skill 目录。"""
        missing: list[str] = []
        for name in ship_roles:
            skill_dir = SHIP_ROLE_TO_SKILL.get(name)
            if skill_dir is None:
                missing.append(f"  {name}: 缺 SHIP_ROLE_TO_SKILL 映射")
                continue
            if self._skill_main_text(skill_dir) is None:
                missing.append(f"  {name} → {skill_dir}/main.py 不存在")
        assert not missing, (
            "ship 角色与 skill 目录映射不全（新 ship 角色需更新 SHIP_ROLE_TO_SKILL）：\n"
            + "\n".join(missing)
        )

    def test_skills_import_load_rule_block(self, ship_roles):
        """skill main.py 必须 import load_rule_block（防止 frontmatter 写了 rule_refs 但实施缺失）。"""
        not_imported: list[str] = []
        for name, data in ship_roles.items():
            if not data["role"].rule_refs:
                # 角色 frontmatter 无 rule_refs → 不强制 import（W3 P0c 后 8 ship 全有）
                continue
            if data["role"].executor == "in_process":
                # rule_refs 消费单点在 engine.role_runner（见类 docstring）
                continue
            skill_dir = SHIP_ROLE_TO_SKILL.get(name)
            if not skill_dir:
                continue
            text = self._skill_main_text(skill_dir) or ""
            if "load_rule_block" not in text:
                not_imported.append(
                    f"  {name} → {skill_dir}/main.py 未 import load_rule_block "
                    f"（frontmatter rule_refs={list(data['role'].rule_refs)}）"
                )
        assert not not_imported, (
            "skill main.py 未 import load_rule_block，rule_refs 章节注入未实施：\n"
            + "\n".join(not_imported)
        )

    def test_skills_invoke_load_rule_block(self, ship_roles):
        """skill main.py 必须真调用 load_rule_block(role_def.rule_refs)（光 import 不调用等同未实施）。"""
        # 匹配 `load_rule_block(role_def.rule_refs)` 或 `load_rule_block(<其他>.rule_refs)`
        # 允许变量名灵活（如 role_def / role / fm 等），但要求 .rule_refs 属性访问
        call_re = re.compile(r"load_rule_block\s*\(\s*\w+\.rule_refs\b")

        not_invoked: list[str] = []
        for name, data in ship_roles.items():
            if not data["role"].rule_refs:
                continue
            if data["role"].executor == "in_process":
                # rule_refs 消费单点在 engine.role_runner（见类 docstring）
                continue
            skill_dir = SHIP_ROLE_TO_SKILL.get(name)
            if not skill_dir:
                continue
            text = self._skill_main_text(skill_dir) or ""
            if not call_re.search(text):
                not_invoked.append(
                    f"  {name} → {skill_dir}/main.py 未调用 "
                    f"load_rule_block(<role>.rule_refs)"
                )
        assert not not_invoked, (
            "skill main.py 未调用 load_rule_block(role_def.rule_refs)，章节注入未实施：\n"
            + "\n".join(not_invoked)
        )


class TestDAGReachability:
    """从 W2 核心可达所有 W3 ship 角色的终态产物。"""

    def test_terminal_products_reachable(self, ship_roles):
        """终态产物（母带规格 / 母带-Suno-retry补丁）能从源简报 BFS 到达。"""
        # 初始可达：W2 角色（直接消费源简报）的 outputs（含模板展开）
        role_names = list(ship_roles.keys())
        reachable: set[str] = set()
        for name in ("音乐总监", "制作人", "作词", "作曲"):
            for out in ship_roles[name]["role"].outputs:
                for expanded in _expand_role_template(out, role_names):
                    reachable.add(_normalize_path(expanded))

        # 索引：角色 → inputs 的非源 normalized 集合
        role_inputs: dict[str, set[str]] = {}
        for name, data in ship_roles.items():
            role_inputs[name] = {
                _normalize_path(inp)
                for inp in data["role"].inputs
                if not _is_source_input(inp)
            }

        # 迭代：若某角色所有 non-source inputs 都 reachable，则其 outputs 加入 reachable
        changed = True
        while changed:
            changed = False
            for name, inputs in role_inputs.items():
                if not inputs.issubset(reachable):
                    continue
                for out in ship_roles[name]["role"].outputs:
                    for expanded in _expand_role_template(out, role_names):
                        norm = _normalize_path(expanded)
                        if norm not in reachable:
                            reachable.add(norm)
                            changed = True

        # 终态产物必须在 reachable 中
        terminal_outs = ship_roles["母带工程师"]["role"].outputs
        for out in terminal_outs:
            for expanded in _expand_role_template(out, role_names):
                norm = _normalize_path(expanded)
                assert norm in reachable, (
                    f"终态产物 {out} 不可达；DAG 有断链。reachable={sorted(reachable)}"
                )
