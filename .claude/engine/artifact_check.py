"""
engine/artifact_check.py — 产物注册表 v0.3 校验模式（运行时消费端/产出端检查）

规范：vault `00-系统/规则/产物注册表规范.md` §4 演进表 v0.3。
接线点：role_invoke.invoke_role（消费端在 spawn 前、产出端在 rc==0 后）。

模式（env `AGENT_ARTIFACT_CHECK`，运行期读取）：
- off  ：跳过全部检查
- warn ：缺口打 stderr + audit.jsonl `artifact_check` 事件，不改行为（默认）
- fail ：消费端缺口 → invoke_role 直接 permanent_failed（不起 subprocess）；
         产出端缺口 → RoleResult 降为 failed

占位符绑定（集合封闭，规范 §2b.2——{proj_root} 由注册表解析、{project} 绑
调用项目，此处只处理剩余两个）：
- 消费端：`{role}` → 消费者角色中文名（作曲 读 给作曲.md）；`{n}` → glob `*`
- 产出端：`{role}` / `{n}` → glob `*`（扇出/任务卡产出至少一个实例即算命中）

lint 分发：条目 frontmatter `lint: <名>` → `_LINTS` 查函数执行（file 内容 →
问题列表）。未知名 → warn；lint 抛异常 → warn 不拦。当前无内置 lint，
函数按需在 `_LINTS` 注册。

已知噪声（先 warn 收集信号，正是 P5a 手法的目的）：
- 批判者 consumes PRD/系统设计，但 brainstorm 阶段 PRD 尚未产出 → 每轮 warn。
  遥测积累后区分 required/optional 再谈 fail 全量化；fail 模式当前是显式 opt-in。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Callable

from .config import VAULT_ROOT

_MODE_ENV = "AGENT_ARTIFACT_CHECK"
_VALID_MODES = ("off", "warn", "fail")

# lint 名 → (内容 → 问题列表)。条目 frontmatter `lint:` 引用这里的键。
_LINTS: dict[str, Callable[[str], list[str]]] = {}


def check_mode() -> str:
    """运行期读 env（不做模块级缓存——B1 原则）；非法值降级 warn。"""
    mode = os.environ.get(_MODE_ENV, "warn").strip().lower()
    return mode if mode in _VALID_MODES else "warn"


def _instance_exists(rendered: str) -> bool:
    """渲染路径 → vault 内存在性。含 glob 通配符时至少一个实例即命中。"""
    rel = rendered.replace("\\", "/")
    if "*" in rel:
        try:
            return any(VAULT_ROOT.glob(rel))
        except (ValueError, OSError):
            return False
    return (VAULT_ROOT / Path(rel)).exists()


def _gaps(
    role_name: str,
    artifact_ids: tuple[str, ...],
    project: str,
    *,
    phase: str,
) -> list[str]:
    """对一组 artifact_id 做存在性 + lint 检查，返回缺口消息列表。

    注册表不可用 / 条目未注册 → 视为缺口消息（与影子校验同哲学：
    不抛异常，消息由调用方按模式处置）。
    """
    from .artifact_registry import ArtifactRegistryError, load_registry, load_config

    if not artifact_ids:
        return []
    try:
        registry = load_registry()
        proj_roots = load_config() if registry else {}
    except ArtifactRegistryError as e:
        return [f"{role_name}: 注册表加载失败，{phase} 检查跳过（{e}）"]
    if not registry:
        return [f"{role_name}: 注册表为空/缺失，{phase} 检查跳过"]

    issues: list[str] = []
    for aid in artifact_ids:
        spec = registry.get(aid)
        if spec is None:
            issues.append(f"{role_name}.{phase}: [[{aid}]] 未注册")
            continue
        rendered = spec.resolve(proj_roots, project)
        if phase == "consume":
            rendered = rendered.replace("{role}", role_name)
            rendered = rendered.replace("{n}", "*")
        else:
            rendered = rendered.replace("{role}", "*").replace("{n}", "*")
        if not _instance_exists(rendered):
            issues.append(
                f"{role_name}.{phase}: [[{aid}]] 实例缺失（期望 {rendered}）"
            )
            continue
        if phase == "produce" and spec.lint:
            issues.extend(_run_lint(role_name, aid, spec.lint, rendered))
    return issues


def _run_lint(role_name: str, aid: str, lint_name: str, rendered: str) -> list[str]:
    fn = _LINTS.get(lint_name)
    if fn is None:
        return [f"{role_name}.produce: [[{aid}]] lint '{lint_name}' 未在 _LINTS 注册"]
    if "*" in rendered:
        return []  # 通配产出不做内容 lint（逐实例 lint 留给产出端 v0.4 评估）
    try:
        content = (VAULT_ROOT / rendered).read_text(encoding="utf-8")
        return [f"{role_name}.produce: [[{aid}]] lint({lint_name}): {m}"
                for m in fn(content)]
    except Exception as e:  # lint 是 side channel，异常降级为消息
        return [f"{role_name}.produce: [[{aid}]] lint '{lint_name}' 执行异常：{e}"]


def run_check(phase: str, role_name: str, project: str) -> tuple[str, list[str]]:
    """invoke_role 接线入口。返回 (mode, 缺口列表)；off 或角色无声明 → 空。

    stderr + audit 事件在这里统一发出，调用方只按 mode 决定拦不拦。
    角色解析失败等自身异常静默跳过（该失败由 invoke_role 主链自己报）。
    """
    mode = check_mode()
    if mode == "off":
        return mode, []
    try:
        from .role_loader import load_role
        role = load_role(role_name)
        ids = role.consumes if phase == "consume" else role.produces
        issues = _gaps(role.name, ids, project, phase=phase)
    except Exception:
        return mode, []
    if issues:
        for msg in issues:
            print(f"⚠️ [产物校验:{phase}:{mode}] {msg}", file=sys.stderr)
        try:
            from .audit import append_audit, utc_now
            append_audit({
                "timestamp": utc_now(),
                "type": "artifact_check",
                "phase": phase,
                "role": role_name,
                "project": project,
                "mode": mode,
                "issues": issues,
            })
        except Exception:
            pass
    return mode, issues
