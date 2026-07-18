"""
engine/workflow.py — 工作流模板加载与角色路由

vault `00-系统/工作流模板/工作流-*.md` 是声明式的链路定义，
本模块负责解析、向 run_chain.py 暴露线性步骤序列。

Phase 3a：仅支持线性 chain（每步一个角色，依次执行）。
Phase 4 LangGraph 落地后会扩展 parallel / discussion-loop 等 step 类型，
本模块的 schema 已预留对应 type 字段，遇到未实现类型抛 NotImplementedError。

外部 API：
- load_workflow(name) -> WorkflowTemplate
- list_workflows() -> list[WorkflowTemplate]
- role_to_skill_dir(name_or_alias) -> str   （vault 角色名 → .claude/skills/<dir>）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from .config import VAULT_ROOT, PROJECT_ROOT
from .obsidian_io import read_note, split_frontmatter
from .role_loader import load_role


WORKFLOW_TEMPLATE_DIR = "00-系统/工作流模板"
_WORKFLOW_PREFIX = "工作流-"


# ── 数据类 ──────────────────────────────────────────────
@dataclass(frozen=True)
class WorkflowStep:
    """工作流的一个步骤。

    type=linear: 单角色顺序执行（Phase 3a 起支持）
    type=discussion: 多角色辩论（Phase 4b 起支持）
    type=brainstorm-loop: 3 角色多轮脑暴 + readiness 决策路由（T2.6 起支持）
    type=parallel: 多角色并发（未实现）
    type=discussion-loop: 多角色循环对话（已被 type=discussion 取代）

    Phase 5 新增字段（层三/层四）：
      post_compress: 角色完成后压缩指定输出文件（层三 haiku 钩子）
        格式: {target_chars: 8000, outputs: ["10-项目/{project}/指令/给后端.md"]}
      pre_flight: 执行前用 haiku 评估复杂度，决定是否拆分（层四）
        格式: {instruction_file: "10-项目/{project}/指令/给后端.md", split_limit_lines: 400}
      auto_split: true 时自动触发 pre_flight（需同时提供 pre_flight 配置）
    """
    type: str = "linear"

    # type=linear 字段
    role: str | None = None

    # type=discussion / parallel / brainstorm-loop 字段
    roles: tuple[str, ...] = ()                # 参与者列表（中文角色名）
    name: str | None = None                    # 议题名（脑暴笔记文件名 + display）
    moderator: str | None = None               # 主持人（None = 第一个参与者主持）
    max_rounds: int = 5                        # 讨论最大轮数（每轮 = 一个角色发言）
    topic_template: str | None = None          # 议题模板（可引用 {project} 等占位符)

    # type=brainstorm-loop 字段（T2.6）
    audit_rounds: tuple[int, ...] = ()         # 触发 scribe 审计模式的轮次（默认 (3, 6)）
    start_round: int = 1                       # 起始轮次（重启时由 round_state.json 覆盖）
    readiness_threshold: int = 85              # prd_readiness 触达即 ready
    context_warn_tokens: int = 30000           # 下轮 input 估算超此值打 WARN

    # 层三：post_compress haiku 钩子
    post_compress: dict | None = None

    # 层四：pre_flight 复杂度评估 + 自动拆分
    pre_flight: dict | None = None
    auto_split: bool = False

    # 工作流层条件跳过（v2026-05-15 新增）：
    # 格式：{"frontmatter_eq": {"file": "...路径...", "key": "...", "value": "..."}}
    # 节点执行前评估，命中即跳过 subprocess 调用（对称 dev_*/main.py 内部跳过的上移版）
    skip_if: dict | None = None

    # 契约参数化 override（P5b 2026-07-04 新增）：workflow yaml 显式声明的
    # contract_overrides，供 role_loader 在契约化角色加载时替换 outputs/inputs。
    # 结构：{"output_contract": {"artifacts_pattern": "module_manifest", ...},
    #        "input_contract": {...}}
    # None（默认）→ role_loader 走 P5a 影子模式（用 fields.default 展开 + assert）；
    # 传入 → role_loader 用 overrides 覆盖 field 值并选 template（参见规范 §11.3）。
    # 校验（run_chain 层 + role_loader 层双护栏）：override 引用不存在的 field
    # 或 template → raise ContractSchemaError（fail-closed）。
    contract_overrides: dict | None = None

    # P8.3 human_gate step 类型（模块化开发工作流用）：
    # - gate：介入 gate 名，唯一识别值。已知：select_module（从模块清单 ready 集选一个）
    # - manifest_path：模块清单.md vault 相对路径（含 {project} 占位符）
    # - prompt：显式给用户的说明文本（None 时 select_module 用默认模板）
    gate: str | None = None
    manifest_path: str | None = None
    prompt: str | None = None

    # P8.4 module_development_loop step 用：engineer subprocess 的 contract_overrides
    # （通常设为 {"input_contract": {"task_source": "module_manifest"}}）；
    # 与顶层 contract_overrides 分开命名以避免 TL step 的 output_contract 覆盖串味
    engineer_contract_overrides: dict | None = None

    # 兜底未识别字段
    extras: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_yaml(cls, data: Any) -> "WorkflowStep":
        # 简形式：字符串就是角色名（线性步骤）
        if isinstance(data, str):
            return cls(type="linear", role=data)
        # 详细形式：字典
        if isinstance(data, dict):
            t = str(data.get("type", "linear"))
            if t == "linear":
                role = data.get("role")
                if not role:
                    raise ValueError(f"linear step 缺少 role 字段: {data}")
                # 解析层三/层四配置
                post_compress = data.get("post_compress") or None
                pre_flight = data.get("pre_flight") or None
                auto_split = bool(data.get("auto_split", False))
                skip_if = data.get("skip_if") or None
                contract_overrides = data.get("contract_overrides") or None
                if contract_overrides is not None and not isinstance(
                    contract_overrides, dict
                ):
                    raise ValueError(
                        f"linear step '{role}' 的 contract_overrides 必须是 dict，"
                        f"实际：{type(contract_overrides).__name__}"
                    )
                return cls(
                    type="linear",
                    role=str(role),
                    post_compress=post_compress,
                    pre_flight=pre_flight,
                    auto_split=auto_split,
                    skip_if=skip_if,
                    contract_overrides=contract_overrides,
                )
            if t == "discussion":
                roles = tuple(str(r) for r in (data.get("roles") or data.get("participants") or ()))
                if not roles:
                    raise ValueError(f"discussion step 缺少 roles/participants: {data}")
                return cls(
                    type="discussion",
                    roles=roles,
                    name=str(data.get("name") or "未命名讨论"),
                    moderator=(str(data["moderator"]) if data.get("moderator") else None),
                    max_rounds=int(data.get("max_rounds", 5)),
                    topic_template=(str(data["topic_template"]) if data.get("topic_template") else None),
                )
            if t == "brainstorm-loop":
                roles = tuple(str(r) for r in (data.get("roles") or ()))
                if len(roles) != 3:
                    raise ValueError(
                        f"brainstorm-loop step 必须恰好 3 角色（发散者/质询者/记录员）："
                        f"{data}"
                    )
                audit_raw = data.get("audit_rounds")
                if audit_raw is None:
                    audit_rounds: tuple[int, ...] = (3, 6)
                else:
                    audit_rounds = tuple(int(r) for r in audit_raw)
                return cls(
                    type="brainstorm-loop",
                    roles=roles,
                    name=str(data.get("name") or "创意脑暴"),
                    max_rounds=int(data.get("max_rounds", 8)),
                    audit_rounds=audit_rounds,
                    start_round=int(data.get("start_round", 1)),
                    readiness_threshold=int(data.get("readiness_threshold", 85)),
                    context_warn_tokens=int(data.get("context_warn_tokens", 30000)),
                )
            if t == "human_gate":
                gate = data.get("gate")
                if not gate:
                    raise ValueError(
                        f"human_gate step 缺少 gate 字段（识别名，如 select_module）：{data}"
                    )
                manifest_path = data.get("manifest_path")
                if str(gate) == "select_module" and not manifest_path:
                    raise ValueError(
                        f"human_gate select_module step 缺少 manifest_path（模块清单.md）：{data}"
                    )
                return cls(
                    type="human_gate",
                    gate=str(gate),
                    manifest_path=str(manifest_path) if manifest_path else None,
                    name=str(data.get("name") or f"human_gate:{gate}"),
                    prompt=str(data["prompt"]) if data.get("prompt") else None,
                )
            if t == "module_development_loop":
                manifest_path = data.get("manifest_path")
                if not manifest_path:
                    raise ValueError(
                        f"module_development_loop step 缺少 manifest_path："
                        f"{data}"
                    )
                engineer_ovr = data.get("engineer_contract_overrides") or None
                if engineer_ovr is not None and not isinstance(engineer_ovr, dict):
                    raise ValueError(
                        f"engineer_contract_overrides 必须是 dict，实际："
                        f"{type(engineer_ovr).__name__}"
                    )
                return cls(
                    type="module_development_loop",
                    name=str(data.get("name") or "模块化开发循环"),
                    manifest_path=str(manifest_path),
                    engineer_contract_overrides=engineer_ovr,
                )
            # 已知但未实现的类型 —— 保留数据，运行时由 build_graph 抛 NotImplementedError
            roles = tuple(str(r) for r in (data.get("roles") or ()))
            extras = {k: v for k, v in data.items() if k not in ("type", "role", "roles")}
            return cls(type=t, roles=roles, extras=extras)
        raise ValueError(f"工作流步骤必须是字符串或字典：{data!r}")


@dataclass(frozen=True)
class WorkflowTemplate:
    name: str
    description: str
    domain: str
    halt_on_failure: bool
    steps: tuple[WorkflowStep, ...]
    note_path: Path
    body: str = field(repr=False)
    frontmatter: dict = field(repr=False)

    # linear_role_names() 已删除（2026-07-18 评审）：Phase 3a 遗物，全仓无调用者，
    # 且"非 linear 抛 NotImplementedError"语义与 build_graph 冲突，留着误导。


# ── 加载 ────────────────────────────────────────────────
def _template_dir() -> Path:
    return VAULT_ROOT / WORKFLOW_TEMPLATE_DIR


def _build_template(note_path: Path) -> WorkflowTemplate:
    content = read_note(note_path)
    fm, body = split_frontmatter(content)
    name = fm.get("name") or note_path.stem.removeprefix(_WORKFLOW_PREFIX)
    raw_steps = fm.get("steps") or []
    if not isinstance(raw_steps, list):
        raise ValueError(f"{note_path} 的 steps 必须是 list，实际：{type(raw_steps).__name__}")
    steps = tuple(WorkflowStep.from_yaml(s) for s in raw_steps)
    return WorkflowTemplate(
        name=str(name),
        description=str(fm.get("description", "")),
        domain=str(fm.get("domain", "")),
        halt_on_failure=bool(fm.get("halt_on_failure", True)),
        steps=steps,
        note_path=note_path,
        body=body,
        frontmatter=fm,
    )


@lru_cache(maxsize=1)
def _index() -> dict[str, Path]:
    """name → note_path 索引；进程内缓存。"""
    idx: dict[str, Path] = {}
    d = _template_dir()
    if not d.is_dir():
        return idx
    for note in d.glob(f"{_WORKFLOW_PREFIX}*.md"):
        try:
            content = read_note(note)
            fm, _ = split_frontmatter(content)
        except Exception:
            continue
        name = fm.get("name") or note.stem.removeprefix(_WORKFLOW_PREFIX)
        if name not in idx:
            idx[str(name)] = note
    return idx


def invalidate_cache() -> None:
    _index.cache_clear()


def load_workflow(name: str) -> WorkflowTemplate:
    idx = _index()
    note = idx.get(name)
    if note is None:
        raise KeyError(
            f"未找到工作流模板 '{name}'。"
            f"已配置：{sorted(idx.keys())}（vault: {_template_dir()}）"
        )
    return _build_template(note)


def list_workflows() -> list[WorkflowTemplate]:
    out: list[WorkflowTemplate] = []
    for note in _template_dir().glob(f"{_WORKFLOW_PREFIX}*.md"):
        try:
            out.append(_build_template(note))
        except Exception as e:
            print(f"⚠️ 跳过工作流模板 {note.name}：{e}")
    out.sort(key=lambda w: w.name)
    return out


# ── 角色名 → skill 目录映射 ───────────────────────────
def role_to_skill_dir(name_or_alias: str) -> str:
    """把 vault 角色名（中文）解析为 .claude/skills/ 下的子目录名（英文）。

    依赖角色 frontmatter 的 aliases 字段：找到第一个对应实际目录的别名。
    """
    role = load_role(name_or_alias)
    skills_root = PROJECT_ROOT / ".claude" / "skills"
    candidates = [role.name, *role.aliases]
    for cand in candidates:
        if (skills_root / cand).is_dir():
            return cand
    raise ValueError(
        f"未找到角色 '{role.name}' 对应的 skill 目录。"
        f"已尝试：{candidates}（在 {skills_root} 下）"
    )
