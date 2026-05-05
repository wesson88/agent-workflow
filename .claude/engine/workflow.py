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
    type=parallel: 多角色并发（未实现）
    type=discussion-loop: 多角色循环对话（已被 type=discussion 取代）
    """
    type: str = "linear"

    # type=linear 字段
    role: str | None = None

    # type=discussion / parallel 字段
    roles: tuple[str, ...] = ()                # 参与者列表（中文角色名）
    name: str | None = None                    # 议题名（脑暴笔记文件名 + display）
    moderator: str | None = None               # 主持人（None = 第一个参与者主持）
    max_rounds: int = 5                        # 讨论最大轮数（每轮 = 一个角色发言）
    topic_template: str | None = None          # 议题模板（可引用 {project} 等占位符）

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
                return cls(type="linear", role=str(role))
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

    def linear_role_names(self) -> list[str]:
        """提取所有步骤的角色名（按声明顺序）。

        Phase 3a 只允许 type=linear；遇到其他类型抛 NotImplementedError，
        提示用户该工作流需要 Phase 4 才能跑。
        """
        names: list[str] = []
        for s in self.steps:
            if s.type != "linear":
                raise NotImplementedError(
                    f"工作流 '{self.name}' 含 type='{s.type}' 步骤，"
                    f"Phase 3a 仅支持 type='linear'。"
                    f"该步骤需要 Phase 4 LangGraph 编排引擎才能执行。"
                )
            if not s.role:
                raise ValueError(f"工作流 '{self.name}' 有 linear 步骤未指定 role")
            names.append(s.role)
        return names


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
