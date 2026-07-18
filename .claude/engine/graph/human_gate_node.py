"""
graph/human_gate_node.py — P8.3 human_gate step 类型工厂

支持的 gate 语义：
- **select_module**：从模块清单 ready 集选一个 module → 更新 state.selected_module_id

行为（幂等 + resumable）：
- 首次进入 node → emit_gate 落盘 pending gate，state 置 halted=True
- 用户 CLI resolve gate（`resolve_gate --user_response T01`）后再跑 workflow
- 二次进入 node → 找到 resolved gate → 从 user_response 读 module_id
  → state.selected_module_id 更新，不 halted，下游继续

关键不变量：
- 一次执行只落一条 pending gate（重复调用不重复落盘）
- 已 resolved gate 中 user_response 必须是 ready 集里的 module_id，否则 raise
"""

from __future__ import annotations

from pathlib import Path

from ..config import VAULT_ROOT
from ..human_gate import (
    HumanGate,
    emit_gate,
    gate_is_consumed,
    list_gates,
)
from ..manifest_render import compute_ready_set, parse_manifest, render_summary
from ..workflow import WorkflowStep
from .state import ProjectState


_ALREADY_SELECTED_STATUS = frozenset({"resolved"})


def _resolve_manifest_path(manifest_template: str, project: str) -> Path:
    """把 human_gate step 的 manifest_path 里的 {project} 替换掉，返回绝对路径。"""
    rel = manifest_template.replace("{project}", project)
    return (VAULT_ROOT / rel).resolve()


def _find_active_gate(project: str, gate: str) -> HumanGate | None:
    """找最新的 pending 或"未消费的 resolved" gate（按 created_at 倒序）。

    - pending → 已落盘等用户处理
    - resolved 且未消费 → 用户已选好，本轮 node 应消费
    - resolved 且已消费（resolution.consumed_at 存在）→ 跳过
      （2026-07-18 评审修复：否则 resolved-reject gate 会永久是"最新
      active gate"，每轮重跑都命中 → fail+halt 死锁）
    - None → 需要新落一条 pending gate
    """
    gates = list_gates(project)
    filtered = [
        g for g in gates
        if g.gate == gate
        and (
            g.status == "pending"
            or (g.status == "resolved" and not gate_is_consumed(g))
        )
    ]
    if not filtered:
        return None
    filtered.sort(key=lambda g: g.created_at, reverse=True)
    return filtered[0]


def _build_select_module_options(ready_nodes: list[dict]) -> list[dict]:
    """把 ready 集节点转成 human_gate.options 结构。"""
    options: list[dict] = []
    for n in ready_nodes:
        nid = str(n.get("id", "")).strip()
        title = str(n.get("title", "")).strip()
        role = str(n.get("role", "")).strip()
        est = n.get("estimate_hours")
        options.append({
            "id": nid,
            "label": f"[{role}] {nid} — {title}"
                     + (f"（预估 {est}h）" if est else ""),
            "effect": f"engineer 将实现 {nid}",
        })
    return options


def make_human_gate_node(step: WorkflowStep, halt_on_failure: bool):
    """工厂函数：把 type=human_gate 的 WorkflowStep 包装成主图 node。

    当前支持的 gate：
    - select_module（P8.3）：从 manifest ready 集选一个 module id
    """
    gate = step.gate
    manifest_template = step.manifest_path
    display_name = step.name or f"human_gate:{gate}"

    if gate != "select_module":
        raise NotImplementedError(
            f"human_gate.gate='{gate}' 暂未实现（P8.3 仅支持 select_module）"
        )

    def node(state: ProjectState) -> dict:
        if state.get("halted"):
            print(f"\n⏭️  跳过 {display_name}（上游 halt）")
            return {"skipped": [display_name]}

        project = state["project"]
        manifest_path = _resolve_manifest_path(manifest_template, project)
        try:
            nodes_list = parse_manifest(manifest_path)
        except Exception as e:
            print(f"\n❌ {display_name} 加载模块清单失败：{e}")
            patch = {"failed": [display_name]}
            if halt_on_failure:
                patch["halted"] = True
            return patch

        summary = render_summary(nodes_list)
        counts = summary["counts"]
        # 全 done → 循环终止；返回 state 标记
        pending_count = counts.get("pending", 0) + counts.get("in_progress", 0)
        if pending_count == 0 and counts.get("done", 0) > 0:
            print(f"\n✅ 所有模块 done（{counts.get('done', 0)}），select_module 无需选择")
            return {
                "succeeded": [display_name],
                "selected_module_id": None,
                "manifest_path": str(manifest_path.relative_to(VAULT_ROOT).as_posix()),
            }

        ready_nodes = compute_ready_set(nodes_list)
        active = _find_active_gate(project, gate)

        # 场景 A：已有 resolved gate → 消费 user_response
        if active and active.status == "resolved":
            module_id = (active.user_response or "").strip()
            valid_ids = {str(n["id"]) for n in ready_nodes}
            done_ids = {
                str(n["id"]) for n in nodes_list
                if str(n.get("status", "")).strip() == "done"
            }
            in_progress_ids = {
                str(n["id"]) for n in nodes_list
                if str(n.get("status", "")).strip() == "in_progress"
            }
            # 允许消费 ready 集里的选择，或已 in_progress（重启工作流场景）
            if module_id not in valid_ids and module_id not in in_progress_ids:
                if module_id in done_ids:
                    print(
                        f"\nℹ️  resolved gate {active.id} 指向已 done 模块 {module_id}，"
                        f"进入下一轮（不消费）"
                    )
                    # 落一个新 pending gate 继续下一轮
                    return _emit_pending_and_halt(
                        project, gate, display_name, ready_nodes, manifest_path
                    )
                print(
                    f"\n❌ resolved gate {active.id} 中 user_response='{module_id}' "
                    f"不在 ready 集 {sorted(valid_ids)} 也不在 in_progress "
                    f"{sorted(in_progress_ids)}"
                )
                patch = {"failed": [display_name]}
                if halt_on_failure:
                    patch["halted"] = True
                return patch
            print(
                f"\n✅ {display_name} 消费 resolved gate {active.id} → module_id={module_id}"
            )
            return {
                "succeeded": [display_name],
                "selected_module_id": module_id,
                "manifest_path": str(manifest_path.relative_to(VAULT_ROOT).as_posix()),
            }

        # 场景 B：已有 pending gate → 本轮 halt（等用户 resolve）
        if active and active.status == "pending":
            print(
                f"\n⏸  {display_name} 已落 pending gate {active.id}"
                f"（未 resolved），本轮 halt。请：\n"
                f"  python .claude/engine/cli_human_gate.py --project {project} "
                f"list\n"
                f"  python .claude/engine/cli_human_gate.py --project {project} "
                f"resolve --id {active.id} --action approve --user-response T0N"
            )
            return {"halted": True, "skipped": [display_name]}

        # 场景 C：无 gate → ready 集空 → 死锁报告
        if not ready_nodes:
            print(
                f"\n❌ {display_name} ready 集为空且有未完成模块 "
                f"（pending/in_progress={pending_count}），"
                f"blocked={summary['blocked_ids']}"
            )
            patch = {"failed": [display_name]}
            if halt_on_failure:
                patch["halted"] = True
            return patch

        # 场景 D：新起 gate → emit + halt
        return _emit_pending_and_halt(
            project, gate, display_name, ready_nodes, manifest_path
        )

    node.__name__ = f"node_hg_{gate}"
    return node


def _emit_pending_and_halt(
    project: str,
    gate: str,
    display_name: str,
    ready_nodes: list[dict],
    manifest_path: Path,
) -> dict:
    """落一条 pending gate + 通知用户 + halt 本轮。"""
    options = _build_select_module_options(ready_nodes)
    reason = f"从模块清单 ready 集选一个模块开始开发（共 {len(ready_nodes)} 个可选）"
    ctx = [str(manifest_path.relative_to(VAULT_ROOT).as_posix())]
    g = emit_gate(
        project=project,
        type="human_gate",
        mode="passive",
        reason=reason,
        node=display_name,
        gate=gate,
        context_refs=ctx,
        options=options,
        recommended_option=options[0]["id"] if options else None,
        suggested_actions=["approve"],
    )
    ids = [opt["id"] for opt in options]
    print(
        f"\n⏸  {display_name} 已落 pending gate {g.id}\n"
        f"  ready 集：{ids}\n"
        f"  resolve：\n"
        f"    python .claude/engine/cli_human_gate.py --project {project} "
        f"resolve --id {g.id} --action approve --user-response <module_id>"
    )
    return {"halted": True, "skipped": [display_name]}
