"""
graph/module_dev_loop_node.py — P8.4 module_development_loop step 类型

状态机（每次 run_chain 进 node 走一段，无 Python while loop）：

  Step A: parse_manifest → 失败即 fail + halt
  Step B: 汇总状态
    - all done (无 pending/in_progress) → succeeded（全流程完成）
    - 有未完成 → step C
  Step C: 找 latest confirm_module_done gate
    - pending → halt
    - resolved approve → mark done → step D
    - resolved reject → fail + halt
    - 无 → step D
  Step D: 找 latest select_module gate
    - pending → halt
    - resolved → 消费 module_id → step E
    - 无 → emit select_module + halt
  Step E: 从 module_id 找 module.role
    - backend / frontend → step F
    - 其他 → fail
  Step F: subprocess dispatch engineer
    - rc≠0 → fail
    - rc==0 → mark in_progress + emit confirm_module_done + halt

任何 halt 出去后由用户 CLI `cli_human_gate.py resolve --id <gate>` 处理，
再重跑 workflow 就是下一段状态转移。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from ..config import PROJECT_ROOT, VAULT_ROOT
from ..human_gate import HumanGate, emit_gate, list_gates, mark_gate_consumed
from ..manifest_render import compute_ready_set, parse_manifest, render_summary
from ..manifest_writer import ManifestWriteError, mark_status
from ..workflow import WorkflowStep, role_to_skill_dir
from .human_gate_node import (
    _build_select_module_options,
    _emit_pending_and_halt,
    _find_active_gate,
    _resolve_manifest_path,
)
from .nodes import _execute_single
from .state import ProjectState


_CONFIRM_GATE = "confirm_module_done"
_SELECT_GATE = "select_module"


def make_module_development_loop_node(step: WorkflowStep, halt_on_failure: bool):
    """工厂函数：把 type=module_development_loop 的 step 包装为主图 node。"""
    if not step.manifest_path:
        raise ValueError(
            "module_development_loop step 缺 manifest_path（工厂时也应校验）"
        )
    display_name = step.name or "模块化开发循环"
    manifest_template = step.manifest_path
    engineer_overrides = step.engineer_contract_overrides

    def node(state: ProjectState) -> dict:
        if state.get("halted"):
            print(f"\n⏭️  跳过 {display_name}（上游 halt）")
            return {"skipped": [display_name]}

        project = state["project"]
        manifest_path = _resolve_manifest_path(manifest_template, project)

        # Step A: parse manifest
        try:
            nodes_list = parse_manifest(manifest_path)
        except Exception as e:
            print(f"\n❌ {display_name} 加载 manifest 失败：{e}")
            return _fail(display_name)

        # Step B: 汇总状态
        summary = render_summary(nodes_list)
        counts = summary["counts"]
        undone = counts.get("pending", 0) + counts.get("in_progress", 0)
        if undone == 0 and counts.get("done", 0) > 0:
            print(f"\n✅ {display_name} 全部模块 done（{counts.get('done', 0)}）")
            return {"succeeded": [display_name]}

        # Step C: latest confirm_module_done gate
        confirm_gate = _find_active_gate(project, _CONFIRM_GATE)
        if confirm_gate:
            step_c_result = _handle_confirm_gate(
                confirm_gate, manifest_path, display_name
            )
            if step_c_result is not None:
                return step_c_result
            # else 已 mark done：重新 parse manifest 反映刚落盘的状态更新。
            # 若不重读，内存 nodes_list 里被 mark 的模块仍是旧 status，Step D
            # `already_done` 检查会漏 → 刚 done 的模块被再次 dispatch。
            # P8.7 Round 3 v1 实战暴露（2026-07-05）。
            nodes_list = parse_manifest(manifest_path)
            summary = render_summary(nodes_list)
            counts = summary["counts"]
            undone = counts.get("pending", 0) + counts.get("in_progress", 0)
            if undone == 0 and counts.get("done", 0) > 0:
                print(f"\n✅ {display_name} 全部模块 done（{counts.get('done', 0)}）")
                return {"succeeded": [display_name]}

        # Step D: latest select_module gate
        select_gate = _find_active_gate(project, _SELECT_GATE)
        if select_gate and select_gate.status == "pending":
            print(
                f"\n⏸  {display_name} 已落 pending select_module gate "
                f"{select_gate.id}，本轮 halt。请 CLI resolve。"
            )
            return {"halted": True, "skipped": [display_name]}

        selected_module_id = None
        if select_gate and select_gate.status == "resolved":
            selected_module_id = (select_gate.user_response or "").strip()
            # 已 done 的选择 → 视为进入下一轮，清掉这条 select，emit 新的
            already_done = {
                str(n["id"]) for n in nodes_list
                if str(n.get("status", "")).strip() == "done"
            }
            if selected_module_id in already_done:
                ready_nodes = compute_ready_set(nodes_list)
                if not ready_nodes:
                    print(
                        f"\n❌ {display_name} ready 集为空（可能全部 blocked）"
                    )
                    return _fail(display_name)
                return _emit_pending_and_halt(
                    project, _SELECT_GATE, display_name,
                    ready_nodes, manifest_path,
                )

        if not selected_module_id:
            ready_nodes = compute_ready_set(nodes_list)
            if not ready_nodes:
                print(
                    f"\n❌ {display_name} ready 集为空、"
                    f"blocked={summary['blocked_ids']}"
                )
                return _fail(display_name)
            return _emit_pending_and_halt(
                project, _SELECT_GATE, display_name,
                ready_nodes, manifest_path,
            )

        # Step E: 找 module role
        target = None
        for n in nodes_list:
            if str(n.get("id", "")).strip() == selected_module_id:
                target = n
                break
        if target is None:
            print(
                f"\n❌ {display_name} selected_module_id='{selected_module_id}' "
                f"不在 manifest 里"
            )
            return _fail(display_name)
        module_role = str(target.get("role", "")).strip()
        if module_role not in ("backend", "frontend"):
            print(
                f"\n❌ {display_name} 模块 {selected_module_id} role='{module_role}' "
                f"不支持 dispatch（仅 backend/frontend）"
            )
            return _fail(display_name)

        # Step F: subprocess dispatch
        return _dispatch_engineer(
            state, target, module_role, engineer_overrides,
            manifest_path, display_name, halt_on_failure,
        )

    node.__name__ = "node_module_development_loop"
    return node


def _handle_confirm_gate(
    confirm_gate: HumanGate,
    manifest_path: Path,
    display_name: str,
) -> dict | None:
    """处理 confirm_module_done gate。

    返回：
    - dict：本轮 node 应返回的 patch（halt / fail / succeeded）
    - None：已消费此 gate（approve→mark done），落到下一 step
    """
    if confirm_gate.status == "pending":
        print(
            f"\n⏸  {display_name} 等 confirm_module_done gate "
            f"{confirm_gate.id}。请 CLI resolve（approve/reject）。"
        )
        return {"halted": True, "skipped": [display_name]}

    if confirm_gate.status != "resolved":
        return None

    resolution = confirm_gate.resolution or {}
    action = str(resolution.get("action", "")).strip()
    module_id = ""
    # gate 里 metadata.module_id 存在 options[0] 或 context_refs
    for opt in confirm_gate.options or []:
        if opt.get("id") == "approve":
            module_id = str(opt.get("module_id", "") or "").strip()
    if not module_id:
        # fallback 从 reason 里搜 "module_id=..."
        r = confirm_gate.reason or ""
        if "module_id=" in r:
            module_id = r.split("module_id=")[-1].split()[0].strip()

    if action == "approve":
        if not module_id:
            print(
                f"\n❌ confirm_gate {confirm_gate.id} approve 但 module_id 缺失"
            )
            _consume_gate(confirm_gate)
            return _fail(display_name)
        try:
            mark_status(manifest_path, module_id, "done")
        except ManifestWriteError as e:
            print(f"\n❌ mark_status(done) 失败：{e}")
            return _fail(display_name)
        _consume_gate(confirm_gate)
        print(f"\n✅ 模块 {module_id} 标记为 done")
        return None

    if action == "reject":
        # 2026-07-18 评审修复：原实现只 fail 不改状态，模块停在 in_progress，
        # 且 resolved-reject gate 永远是"最新 active gate"→ 每轮重跑都命中
        # 同一条 reject → 永久死锁。现改为：
        #   1. 模块 mark blocked（与 gate options.reject.effect 声明一致），
        #      ready 集自动排除，用户修完代码后手动改 manifest status 解锁
        #   2. gate 标记 consumed，下轮不再命中
        # 本轮仍 fail+halt（让用户明确看到 reject 生效）。
        if module_id:
            try:
                mark_status(manifest_path, module_id, "blocked")
                print(f"\n❌ 用户 reject 模块 {module_id} → 已 mark blocked"
                      f"（修复后请手动把 manifest 里该模块 status 改回 pending）")
            except ManifestWriteError as e:
                print(f"\n⚠️ reject 后 mark_status(blocked) 失败：{e}")
        else:
            print(f"\n❌ 用户 reject 模块 (unknown)，无法定位 module_id，"
                  f"请手动检查 manifest")
        _consume_gate(confirm_gate)
        return _fail(display_name)

    # 其他 resolution action（skip_node / abort / ...）：同样消费，防重复命中
    print(f"\n❌ confirm_gate {confirm_gate.id} action='{action}' 未识别")
    _consume_gate(confirm_gate)
    return _fail(display_name)


def _consume_gate(gate: HumanGate) -> None:
    """标记 gate 已消费；失败仅告警不阻断（消费标记是防死锁辅助，非主链）。"""
    try:
        mark_gate_consumed(gate.project, gate.id)
    except Exception as e:
        print(f"⚠️ mark_gate_consumed({gate.id}) 失败：{e}", flush=True)


def _dispatch_engineer(
    state: ProjectState,
    module: dict,
    module_role: str,
    engineer_overrides: dict | None,
    manifest_path: Path,
    display_name: str,
    halt_on_failure: bool,
) -> dict:
    """Step F：subprocess dispatch engineer + 成功后 emit confirm_module_done。"""
    module_id = str(module["id"]).strip()
    skill_dir = "dev_backend" if module_role == "backend" else "dev_frontend"
    main_py = PROJECT_ROOT / ".claude" / "skills" / skill_dir / "main.py"
    if not main_py.is_file():
        print(f"\n❌ engineer skill 缺 main.py：{main_py}")
        return _fail(display_name)

    env = os.environ.copy()
    env["PROJECT"] = state["project"]
    env["TASK"] = state.get("task", "")
    env["AGENT_SELECTED_MODULE_ID"] = module_id
    if engineer_overrides:
        env["AGENT_CONTRACT_OVERRIDES"] = json.dumps(
            engineer_overrides, ensure_ascii=False
        )
    else:
        env.pop("AGENT_CONTRACT_OVERRIDES", None)

    subtask = state.get("task", "") + f" [模块 {module_id}]"
    print(
        f"\n{'=' * 60}\n"
        f"▶ 运行 engineer({module_role}) 模块 {module_id} — {module.get('title', '')}\n"
        f"{'=' * 60}"
    )
    rc = _execute_single(main_py, subtask, state["project"], env)
    if rc != 0:
        print(f"\n❌ engineer({module_role}) 模块 {module_id} 失败 rc={rc}")
        return _fail(display_name)

    # 成功 → mark in_progress + emit confirm_module_done gate
    try:
        mark_status(manifest_path, module_id, "in_progress")
    except ManifestWriteError as e:
        print(f"\n❌ mark_status(in_progress) 失败：{e}")
        return _fail(display_name)

    g = emit_gate(
        project=state["project"],
        type="human_gate",
        mode="passive",
        reason=f"模块 {module_id} 已完成 engineer 跑通，请确认 (module_id={module_id})",
        node=display_name,
        gate=_CONFIRM_GATE,
        context_refs=[
            str(manifest_path.relative_to(VAULT_ROOT).as_posix())
        ],
        options=[
            {
                "id": "approve",
                "label": f"确认 {module_id} 完成 → mark done",
                "module_id": module_id,
                "effect": "manifest 里 status=done，进入下一轮 select",
            },
            {
                "id": "reject",
                "label": f"退回 {module_id} → mark blocked",
                "module_id": module_id,
                "effect": "本轮 workflow fail，需人工修 code 后重跑",
            },
        ],
        recommended_option="approve",
        suggested_actions=["approve", "reject"],
    )
    print(
        f"\n⏸  emit confirm_module_done gate {g.id}\n"
        f"  approve：python .claude/engine/cli_human_gate.py --project "
        f"{state['project']} resolve --id {g.id} --action approve\n"
        f"  reject：python .claude/engine/cli_human_gate.py --project "
        f"{state['project']} resolve --id {g.id} --action reject"
    )
    return {
        "halted": True,
        "skipped": [display_name],
        "selected_module_id": module_id,
        "module_role": module_role,
    }


def _fail(display_name: str) -> dict:
    return {"failed": [display_name], "halted": True}
