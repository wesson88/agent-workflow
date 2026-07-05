"""
role_loader.py — 把 00-系统/角色基因/ 下的角色笔记加载成 Role 对象。

支持按"中文角色名"或"aliases 中任意别名"查找（兼容旧 skill_id 如
chief_architect / dev_backend，引擎切换 vault 后无需大规模改 main.py）。

DYNAMIC_START/END 标记**保留在** body 中，由 build_system_prompt 端拼接。
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from .config import VAULT_ROOT, role_genes_dir
from .obsidian_io import read_note, split_frontmatter


class RoleNotFound(KeyError):
    pass


class ContractSchemaError(ValueError):
    """契约声明或与 outputs/inputs 的等价关系违反规范 §11 / §7.1。

    P5a 影子模式：契约化角色（output_contract / input_contract）
    加载时 raise（fail-closed），保证\"契约展开 = 硬编码 outputs\"不变量。
    未契约化的角色不受影响。
    """
    pass


@dataclass(frozen=True)
class ResolvedContract:
    """契约解析结果（P5a 影子模式：仅用于 assert 校验，不驱动真实产出）。

    P5a：role_loader 加载契约化角色时用 fields 的 default 值展开
    首选 template（legacy_directives 优先，否则第一个），产出 outputs 供
    assert 与 frontmatter.outputs 语义等价。

    P5b：将来 workflow 传入 contract_overrides 时用此结构替换 role.outputs。
    """
    template_name: str                    # 使用的 template（legacy_directives 优先）
    field_values: dict                    # 展开时用的 field values（default 优先）
    outputs: tuple[str, ...]              # 展开后的路径（保留 {project} / {n} 等 workflow bindings 占位符）


@dataclass(frozen=True)
class Role:
    """角色"定义"，纯静态。

    运行时状态（status / last_run / consecutive_failures / error_count
    / last_output_path）拆到 `00-系统/.runtime-state/<role>.json`，
    通过 engine.state 模块读写。Role 对象不再持有这些字段。
    """
    # 标识
    name: str                          # frontmatter.role，中文角色名
    aliases: tuple[str, ...]           # frontmatter.aliases
    note_path: Path                    # 角色笔记的绝对路径

    # 元数据
    domain: str
    skills: tuple[str, ...]
    style: str
    model: str
    max_tokens: int
    tools: tuple[str, ...]
    version: str

    # 关系图
    upstream: tuple[str, ...]          # 数据流上游
    downstream: tuple[str, ...]        # 数据流下游
    monitors: tuple[str, ...]          # 监控的下游（可触发补丁）

    # 输入输出（含 {project} 占位符的 vault 路径模板）
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]

    # 外迁的技能引用（vault 相对路径），load_role 时已 inline 拼到 body 末尾
    skill_refs: tuple[str, ...]

    # 规则章节按需引用（wikilink 字符串），形如 "[[架构分解规则#§3 分解步骤]]"。
    # **不**拼进 body（避免膨胀 system_prompt 触发 audit 阈值）；调用方自己
    # 用 engine.wikilink.expand_wikilinks 展开后注入到 user_prompt 的 context。
    # 与 skill_refs 的差异：skill 全文进 system 用于稳定方法论；
    # rule_refs 按章节进 user 用于任务相关的规则节选。
    rule_refs: tuple[str, ...]

    # 笔记正文（含 DYNAMIC 区域 + 已 inline 的 skill 内容）
    body: str

    # 完整 frontmatter（debug/扩展用）
    frontmatter: dict = field(repr=False)

    # token 预算 override（可选，单位 tokens）；缺省 None 则走 engine.llm 默认窗口百分比
    # 用法：角色 frontmatter 显式声明 `budget_input_tokens: 80000`，engine.llm 入口
    # 护栏会按此值做 RAISE（warn = 60% × 此值），代替 _TOTAL_RAISE_RATIO 百分比
    budget_input_tokens: int | None = None

    # 契约解析结果（P5a 影子模式）——契约化角色加载时展开首选 template + assert
    # 与 outputs/inputs 语义等价（抽象化后集合相等）。P5b 起 workflow contract_overrides
    # 会驱动真实 outputs 替换；P5a 阶段仅用于 fail-closed 校验。
    resolved_output_contract: ResolvedContract | None = None
    resolved_input_contract: ResolvedContract | None = None

    # P10 能力引用（capability manifest wikilink，形如 `[[huashu-design/manifest]]`）。
    # **不**进 body inline；由 common.build_system_prompt 走 _render_capability_summary
    # 生成 ≤ 400 chars 摘要 + 调用方式后拼进 system_prompt（规范 §5.2 关键不变量）。
    capability_refs: tuple[str, ...] = ()

    @property
    def all_names(self) -> tuple[str, ...]:
        """name + aliases 的并集，用于查找匹配。"""
        return (self.name, *self.aliases)


# ── 内部：从笔记构造 Role ─────────────────────────────────
def _seq(value, default=()) -> tuple[str, ...]:
    """把 frontmatter 字段规范化成 str 元组。None / 缺失 → 默认。"""
    if value is None:
        return tuple(default)
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(x) for x in value if x is not None)
    return (str(value),)


def _resolve_skill_refs(refs: tuple[str, ...], vault_root: Path) -> str:
    """读取每个 skill 文件，去掉自身 frontmatter，用分隔符拼成单段。

    缺失文件 → 占位 `[SKILL MISSING: <path>]` + stderr 警告，不 fail。
    """
    if not refs:
        return ""
    parts: list[str] = []
    for ref in refs:
        rel = ref.strip()
        if not rel:
            continue
        path = (vault_root / rel).resolve()
        if not path.is_file():
            print(f"⚠️ skill_refs 缺文件：{rel}", file=sys.stderr)
            parts.append(f"=== Skill: {rel} ===\n[SKILL MISSING: {rel}]")
            continue
        try:
            raw = path.read_text(encoding="utf-8")
        except Exception as e:
            print(f"⚠️ skill_refs 读失败 {rel}：{e}", file=sys.stderr)
            parts.append(f"=== Skill: {rel} ===\n[SKILL READ ERROR: {e}]")
            continue
        _, sk_body = split_frontmatter(raw)
        parts.append(f"=== Skill: {rel} ===\n{sk_body.strip()}")
    if not parts:
        return ""
    return "\n\n## 引用技能（来自 skill_refs）\n\n" + "\n\n".join(parts)


# ── 契约解析（P5a 影子模式）─────────────────────────────
_CONTRACT_BUILTIN_PLACEHOLDERS = frozenset({
    "project", "date", "n", "current_module_id",
    "title_slug", "ts", "role", "domain",
})
_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def _abstract_module_id(path: str) -> str:
    """将 T01 / T0N / T{n} / {module_id_template} 归一为 T*。

    与 tests/engine/test_se_contract_lint.py::TestOutputContractSchema
    使用同款抽象化，使 outputs 字段允许保留历史 T01/T0N 冗余写法而不误判。
    """
    norm = path.replace("\\", "/").replace("{project}", "PROJECT")
    norm = norm.replace("{module_id_template}", "T{n}")
    return re.sub(r"T(0N|\{n\}|0?\d+)", "T*", norm)


def _resolve_contract(
    contract: dict,
    contract_type: str,
    role_name: str,
    overrides: dict | None = None,
) -> ResolvedContract:
    """按 contract 声明选择 template 并展开 field 值。

    P5a 影子模式（overrides=None）：用 fields.default 展开首个/legacy_directives
    template；产出用于 assert 与硬编码 outputs 语义等价的路径集合。

    P5b 写入模式（overrides=dict）：优先用 overrides 覆盖 fields.default，
    template 选择走 `artifacts_pattern` 约定（overrides > fields.default > legacy）；
    resolved outputs 由调用方（_build_role）真替换 role.outputs。

    Template 选择优先级（规范 §11.3 约定；泛化后不依赖固定字段名）：
    1. overrides 中任一 selector field（values 集合 ⊆ templates keys 的 enum field）
    2. selector field 的 default 值（若指向存在的 template）
    3. templates 里有 'legacy_directives' → 选之
    4. 首个 template

    "Selector field" 惯例：契约里若某 enum field 的 values 恰好等于/子集 templates
    keys（如 TL.artifacts_pattern.values = [legacy_directives, module_manifest] =
    templates keys；Backend.task_source 同款），它就自动被识别为 template 选择器。
    这允许 output_contract 用 `artifacts_pattern`、input_contract 用 `task_source`
    等语义化命名，无需强制统一。

    校验：
    - parameterizable 必须 = True
    - fields ≥ 1、templates ≥ 1
    - template 里的 outputs/inputs 是非空 list
    - 占位符必须在 fields 或 builtin 集合中（§11.7 反例 2）
    - overrides 中的 field / template 必须在契约声明中（§11.3 决策规则）
    """
    if contract.get("parameterizable") is not True:
        raise ContractSchemaError(
            f"{role_name}.{contract_type}: parameterizable 必须显式 = true"
        )
    fields = contract.get("fields") or {}
    templates = contract.get("templates") or {}
    if not fields:
        raise ContractSchemaError(
            f"{role_name}.{contract_type}: fields 为空（§7.1 不可豁免项）"
        )
    if not templates:
        raise ContractSchemaError(
            f"{role_name}.{contract_type}: templates 为空（§7.1 不可豁免项）"
        )

    overrides = overrides or {}
    # 校验 overrides 里所有 key 必须在 fields 声明（§11.3 决策规则）
    for override_key in overrides:
        if override_key not in fields:
            raise ContractSchemaError(
                f"{role_name}.{contract_type}: contract_overrides 中 field "
                f"'{override_key}' 未在契约 fields 声明；已声明字段："
                f"{sorted(fields.keys())}"
            )

    # 识别 selector fields：values 集合 ⊆ templates keys 的 enum field
    template_keys = set(templates.keys())
    selector_fields: list[str] = []
    for fname, fspec in fields.items():
        if not isinstance(fspec, dict):
            continue
        if fspec.get("type") != "enum":
            continue
        values = fspec.get("values") or []
        if values and set(str(v) for v in values).issubset(template_keys):
            selector_fields.append(fname)

    # 选 template：override selector > selector default > legacy_directives > first
    template_name: str | None = None
    for sf in selector_fields:
        if sf in overrides:
            template_name = str(overrides[sf])
            break
    if template_name is None:
        for sf in selector_fields:
            spec = fields[sf]
            if isinstance(spec, dict) and "default" in spec:
                default_val = str(spec["default"])
                if default_val in templates:
                    template_name = default_val
                    break
    if template_name is None:
        template_name = (
            "legacy_directives" if "legacy_directives" in templates
            else next(iter(templates))
        )

    if template_name not in templates:
        raise ContractSchemaError(
            f"{role_name}.{contract_type}: template '{template_name}' 未在契约 "
            f"templates 声明；已声明模板：{sorted(templates.keys())}"
        )
    template = templates[template_name]
    key = "outputs" if contract_type == "output_contract" else "inputs"
    paths = template.get(key)
    if not paths or not isinstance(paths, list):
        raise ContractSchemaError(
            f"{role_name}.{contract_type}.templates.{template_name}.{key}: "
            f"缺失或非 list"
        )

    # 校验占位符 declared（§11.7 反例 2）
    declared = set(fields.keys()) | _CONTRACT_BUILTIN_PLACEHOLDERS
    for path in paths:
        for placeholder in _PLACEHOLDER_RE.findall(str(path)):
            if placeholder not in declared:
                raise ContractSchemaError(
                    f"{role_name}.{contract_type}.templates.{template_name}."
                    f"{key}: 占位符 {{{placeholder}}} 未在 fields 声明"
                )

    # 收集 field values：overrides > fields.default（其它保留占位符字面）
    field_values: dict[str, Any] = {}
    for fname, spec in fields.items():
        if isinstance(spec, dict) and "default" in spec:
            field_values[fname] = spec["default"]
    for fname, fval in overrides.items():
        field_values[fname] = fval

    resolved: list[str] = []
    for path in paths:
        expanded = str(path)
        for fname, fval in field_values.items():
            expanded = expanded.replace(f"{{{fname}}}", str(fval))
        resolved.append(expanded)

    return ResolvedContract(
        template_name=template_name,
        field_values=field_values,
        outputs=tuple(resolved),
    )


def _assert_contract_matches_declared(
    contract_type: str,
    resolved: ResolvedContract,
    declared_paths: tuple[str, ...],
    role_name: str,
) -> None:
    """assert 契约展开与 frontmatter 声明的 outputs/inputs 语义等价。

    比对方式：module_id 抽象化后集合相等（允许 outputs 字段保留 T01/T0N
    的历史冗余写法，同时接受 template 里的 T{n} / {module_id_template}）。

    §11.5 向后兼容硬约束：契约化角色的 outputs 字段应等价于 templates 首个
    template + 默认 field 值展开。不等价即代表 spec 漂移，fail-closed。
    """
    declared_abstract = {_abstract_module_id(p) for p in declared_paths}
    resolved_abstract = {_abstract_module_id(p) for p in resolved.outputs}
    if declared_abstract == resolved_abstract:
        return
    missing_in_template = declared_abstract - resolved_abstract
    extra_in_template = resolved_abstract - declared_abstract
    detail: list[str] = []
    if missing_in_template:
        detail.append(
            f"declared 中未被 template 展开覆盖：{sorted(missing_in_template)}"
        )
    if extra_in_template:
        detail.append(
            f"template 展开出 declared 未声明：{sorted(extra_in_template)}"
        )
    field_key = contract_type.replace("_contract", "s")  # output_contract → outputs
    raise ContractSchemaError(
        f"{role_name}.{contract_type}: 契约展开与 {field_key} 字段不等价"
        f"（抽象化 T* 后集合不同）\n  " + "\n  ".join(detail)
    )


def _build_role(
    note_path: Path,
    contract_overrides: dict | None = None,
) -> Role:
    """构造 Role 对象。

    contract_overrides（P5b 起）：workflow 层显式注入的契约参数：
        {
            "output_contract": {"artifacts_pattern": "module_manifest", ...},
            "input_contract": {...}
        }
    - 传入且角色声明了对应契约 → resolved outputs/inputs 替换 role.outputs / role.inputs
    - 传入但角色未声明对应契约 → raise ContractSchemaError（防止误用）
    - 未传入且角色有契约 → 影子模式（P5a 行为，assert 契约展开与硬编码 outputs 等价）
    - 未传入且角色无契约 → baseline 行为不变
    """
    content = read_note(note_path)
    fm, body = split_frontmatter(content)
    if not fm.get("role"):
        raise ValueError(f"{note_path} 缺少 frontmatter.role 字段")
    skill_refs = _seq(fm.get("skill_refs"))
    skill_block = _resolve_skill_refs(skill_refs, VAULT_ROOT) if skill_refs else ""
    body_with_skills = body + ("\n\n" + skill_block if skill_block else "")
    rule_refs = _seq(fm.get("rule_refs"))
    capability_refs = _seq(fm.get("capability_refs"))
    declared_outputs = _seq(fm.get("outputs"))
    declared_inputs = _seq(fm.get("inputs"))

    role_name = str(fm["role"])
    overrides = contract_overrides or {}
    out_overrides = overrides.get("output_contract")
    in_overrides = overrides.get("input_contract")

    resolved_out = None
    out_contract_fm = fm.get("output_contract")
    if isinstance(out_contract_fm, dict) and out_contract_fm:
        resolved_out = _resolve_contract(
            out_contract_fm, "output_contract", role_name, overrides=out_overrides
        )
        if out_overrides:
            # P5b 写入模式：resolved 替换硬编码 outputs
            declared_outputs = resolved_out.outputs
        else:
            # P5a 影子模式：assert 契约展开与 outputs 等价
            _assert_contract_matches_declared(
                "output_contract", resolved_out, declared_outputs, role_name
            )
    elif out_overrides:
        raise ContractSchemaError(
            f"{role_name}: contract_overrides.output_contract 传入但角色未声明 "
            f"output_contract；请先按规范 §11 补契约声明或移除 overrides"
        )

    resolved_in = None
    in_contract_fm = fm.get("input_contract")
    if isinstance(in_contract_fm, dict) and in_contract_fm:
        resolved_in = _resolve_contract(
            in_contract_fm, "input_contract", role_name, overrides=in_overrides
        )
        if in_overrides:
            declared_inputs = resolved_in.outputs
        else:
            _assert_contract_matches_declared(
                "input_contract", resolved_in, declared_inputs, role_name
            )
    elif in_overrides:
        raise ContractSchemaError(
            f"{role_name}: contract_overrides.input_contract 传入但角色未声明 "
            f"input_contract；请先按规范 §11 补契约声明或移除 overrides"
        )

    return Role(
        name=role_name,
        aliases=_seq(fm.get("aliases")),
        note_path=note_path,
        domain=str(fm.get("domain", "")),
        skills=_seq(fm.get("skills")),
        style=str(fm.get("style", "")),
        model=str(fm.get("model", "claude-sonnet-4-6")),
        max_tokens=int(fm.get("max_tokens", 4096)),
        tools=_seq(fm.get("tools")),
        version=str(fm.get("version", "0.0.0")),
        upstream=_seq(fm.get("upstream")),
        downstream=_seq(fm.get("downstream")),
        monitors=_seq(fm.get("monitors")),
        inputs=declared_inputs,
        outputs=declared_outputs,
        skill_refs=skill_refs,
        capability_refs=capability_refs,
        rule_refs=rule_refs,
        body=body_with_skills,
        frontmatter=fm,
        budget_input_tokens=(int(fm["budget_input_tokens"])
                             if fm.get("budget_input_tokens") else None),
        resolved_output_contract=resolved_out,
        resolved_input_contract=resolved_in,
    )


# ── 公共 API ─────────────────────────────────────────────
@lru_cache(maxsize=1)
def _index() -> dict[str, Path]:
    """name/alias → note_path 的索引。

    单进程内缓存；如笔记被外部修改后想刷新，调用 invalidate_cache()。
    """
    idx: dict[str, Path] = {}
    for note in role_genes_dir().rglob("角色-*.md"):
        try:
            content = read_note(note)
            fm, _ = split_frontmatter(content)
        except Exception:
            continue
        name = fm.get("role")
        if not name:
            continue
        for key in (name, *(_seq(fm.get("aliases")) or ())):
            if key in idx and idx[key] != note:
                # 重名告警，但保留先到的（按 sorted 顺序）
                continue
            idx[key] = note
    return idx


def invalidate_cache() -> None:
    """清空 role_loader 的索引缓存（适合写入 frontmatter 后调用）。"""
    _index.cache_clear()


def load_role(
    name_or_alias: str,
    contract_overrides: dict | None = None,
) -> Role:
    """按名或别名加载角色。

    contract_overrides（P5b 起）：workflow 层传入的契约参数字典。
    结构：`{"output_contract": {"artifacts_pattern": ...}, "input_contract": {...}}`
    详见 `_build_role` docstring。
    """
    idx = _index()
    note = idx.get(name_or_alias)
    if note is None:
        available = sorted(set(idx.keys()))
        raise RoleNotFound(
            f"未找到角色 '{name_or_alias}'。已知名称/别名：{available}"
        )
    return _build_role(note, contract_overrides=contract_overrides)


def list_roles() -> list[Role]:
    """加载 vault 中所有角色笔记，按角色名排序。"""
    seen: set[Path] = set()
    roles: list[Role] = []
    for note in role_genes_dir().rglob("角色-*.md"):
        if note in seen:
            continue
        seen.add(note)
        try:
            roles.append(_build_role(note))
        except Exception as e:
            # 容错：解析失败的笔记跳过，不阻塞整体
            print(f"⚠️ 跳过角色笔记 {note.name}：{e}")
    roles.sort(key=lambda r: r.name)
    return roles
