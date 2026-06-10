"""
engine/cli_human_gate.py — 人工介入卡点 CLI

CLI（在 vault 项目目录下跑）：

  # 列出所有 pending gates
  python .claude/engine/cli_human_gate.py --project mapapp list

  # 显示某条 gate 详情
  python .claude/engine/cli_human_gate.py --project mapapp show --id gate-20260610-001

  # 通用解决：set_state + patch
  python .claude/engine/cli_human_gate.py --project mapapp resolve --id gate-... \
      --action set_state --patch '{"selected_module_id": "M02"}'

  # 快捷：approve / reject
  python .claude/engine/cli_human_gate.py --project mapapp approve --id gate-... \
      --response "确认进 PRD"

  # 主动 pause / resume / intervene
  python .claude/engine/cli_human_gate.py --project mapapp pause --message "..."
  python .claude/engine/cli_human_gate.py --project mapapp resume
  python .claude/engine/cli_human_gate.py --project mapapp intervene \
      --message "先做定位" --action reroute

Phase B bridge 部署后，本 CLI 可与 LangGraph interrupt/update_state/resume 共存：
- 本 CLI 写 JSON 落盘（pre-LangGraph 主路径）
- LangGraph 主流程在 interrupt 时同时落 JSON，CLI 读 JSON 并调 update_state
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Windows 控制台 utf-8 重配置
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.human_gate import (
    HumanGate, emit_gate, resolve_gate, load_gate, list_gates,
    RESOLUTION_ACTIONS,
)
from engine.config import PROJECT_NAME


# ── 显示 ────────────────────────────────────────────────
def _print_gate_summary(g: HumanGate) -> None:
    print(f"[{g.id}]  status={g.status}  mode={g.mode}  type={g.type}")
    if g.gate:
        print(f"  gate: {g.gate}")
    if g.node:
        print(f"  node: {g.node}")
    print(f"  reason: {g.reason}")
    if g.options:
        print(f"  options:")
        for o in g.options:
            mark = "★ " if g.recommended_option == o.get("id") else "  "
            label = o.get("label", "")
            effect = o.get("effect", "")
            tail = f" — {effect}" if effect else ""
            print(f"    {mark}{o.get('id')}: {label}{tail}")
    elif g.recommended_option:
        print(f"  recommended: {g.recommended_option}")
    if g.suggested_actions:
        print(f"  suggested_actions: {g.suggested_actions}")
    if g.context_refs:
        print(f"  context_refs:")
        for r in g.context_refs:
            print(f"    - {r}")
    if g.user_response:
        print(f"  user_response: {g.user_response}")
    if g.resolution:
        print(f"  resolution: {g.resolution}")
    print(f"  created_at: {g.created_at}")
    if g.resolved_at:
        print(f"  resolved_at: {g.resolved_at}")


# ── 子命令 ──────────────────────────────────────────────
def _cmd_list(args: argparse.Namespace, project: str) -> int:
    status = args.status
    gates = list_gates(project, status=status)
    if not gates:
        suffix = f"（status={status}）" if status else ""
        print(f"项目 '{project}' 无 gate{suffix}。")
        return 0
    print(f"项目 '{project}' 有 {len(gates)} 个 gate{f'（status={status}）' if status else ''}：\n")
    for g in gates:
        _print_gate_summary(g)
        print()
    return 0


def _cmd_show(args: argparse.Namespace, project: str) -> int:
    g = load_gate(project, args.id)
    _print_gate_summary(g)
    return 0


def _cmd_resolve(args: argparse.Namespace, project: str) -> int:
    patch = None
    if args.patch:
        try:
            patch = json.loads(args.patch)
        except json.JSONDecodeError as e:
            print(f"❌ --patch 不是合法 JSON：{e}", file=sys.stderr)
            return 2
    g = resolve_gate(
        project=project,
        gate_id=args.id,
        action=args.action,
        user_response=args.response,
        patch=patch,
        target_node=args.target_node,
    )
    print(f"✅ gate {g.id} resolved with action={g.resolution.get('action')}")
    if g.user_response:
        print(f"   user_response: {g.user_response}")
    return 0


def _cmd_approve(args: argparse.Namespace, project: str) -> int:
    args2 = argparse.Namespace(
        id=args.id, action="approve", response=args.response,
        patch=None, target_node=None,
    )
    return _cmd_resolve(args2, project)


def _cmd_reject(args: argparse.Namespace, project: str) -> int:
    args2 = argparse.Namespace(
        id=args.id, action="reject", response=args.response,
        patch=None, target_node=None,
    )
    return _cmd_resolve(args2, project)


def _cmd_pause(args: argparse.Namespace, project: str) -> int:
    """主动 pause：emit 一条 active gate（reason 含 '暂停' 关键字便于 resume 识别）。"""
    msg = (args.message or "用户主动暂停").strip()
    if "暂停" not in msg:
        msg = f"用户主动暂停 — {msg}"
    g = emit_gate(
        project=project,
        type="human_intervention",
        mode="active",
        reason=msg,
        suggested_actions=["resume（继续）", "abort（终止）"],
    )
    print(f"⏸️  pause gate created: {g.id}")
    print(f"   resume: python .claude/engine/cli_human_gate.py --project {project} resume")
    print(f"   abort:  python .claude/engine/cli_human_gate.py --project {project} resolve --id {g.id} --action abort")
    return 0


def _cmd_resume(args: argparse.Namespace, project: str) -> int:
    """resume：解决最近的 active pause gate（按 created_at 最大）。"""
    pending = list_gates(project, status="pending")
    pauses = [
        g for g in pending
        if g.mode == "active"
        and g.type == "human_intervention"
        and "暂停" in g.reason
    ]
    if not pauses:
        print(f"项目 '{project}' 无未解决的 pause gate。", file=sys.stderr)
        return 1
    target = pauses[-1]  # list_gates 已 sort by name (≈ created order)
    g = resolve_gate(
        project=project,
        gate_id=target.id,
        action="approve",
        user_response=(args.message or "用户恢复"),
    )
    print(f"▶️  pause gate {g.id} resolved (resume).")
    return 0


def _cmd_intervene(args: argparse.Namespace, project: str) -> int:
    """主动介入：emit + 立即 resolve（用户已经决定好了的）。"""
    if args.action not in RESOLUTION_ACTIONS:
        print(f"❌ 未知 action='{args.action}'。已知：{RESOLUTION_ACTIONS}", file=sys.stderr)
        return 2
    g = emit_gate(
        project=project,
        type="human_intervention",
        mode="active",
        reason=args.message,
    )
    patch = None
    if args.patch:
        try:
            patch = json.loads(args.patch)
        except json.JSONDecodeError as e:
            print(f"❌ --patch 不是合法 JSON：{e}", file=sys.stderr)
            return 2
    resolve_gate(
        project=project,
        gate_id=g.id,
        action=args.action,
        user_response=args.message,
        patch=patch,
        target_node=args.target_node,
    )
    print(f"✅ intervene gate {g.id} action={args.action}")
    return 0


# ── 入口 ────────────────────────────────────────────────
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="agent-workflow 人工介入卡点 CLI")
    p.add_argument(
        "--project", default=None,
        help="项目名（默认从 .env / PROJECT_NAME 读，最终默认 'default'）",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="列出所有 gate（默认只列 pending）")
    p_list.add_argument(
        "--status", default="pending",
        choices=["pending", "resolved", "expired", "cancelled", "all"],
        help="筛选状态；'all' = 不筛选（默认 pending）",
    )
    p_list.set_defaults(handler=_cmd_list)

    p_show = sub.add_parser("show", help="显示 gate 详情")
    p_show.add_argument("--id", required=True)
    p_show.set_defaults(handler=_cmd_show)

    p_resolve = sub.add_parser("resolve", help="通用解决 gate")
    p_resolve.add_argument("--id", required=True)
    p_resolve.add_argument("--action", required=True, choices=RESOLUTION_ACTIONS)
    p_resolve.add_argument("--response", default=None, help="用户回应（自由文本）")
    p_resolve.add_argument("--patch", default=None, help="state patch（JSON 字符串）")
    p_resolve.add_argument("--target-node", default=None, help="reroute 目标节点")
    p_resolve.set_defaults(handler=_cmd_resolve)

    p_approve = sub.add_parser("approve", help="快捷：action=approve")
    p_approve.add_argument("--id", required=True)
    p_approve.add_argument("--response", default=None)
    p_approve.set_defaults(handler=_cmd_approve)

    p_reject = sub.add_parser("reject", help="快捷：action=reject")
    p_reject.add_argument("--id", required=True)
    p_reject.add_argument("--response", default=None)
    p_reject.set_defaults(handler=_cmd_reject)

    p_pause = sub.add_parser("pause", help="主动 pause（emit active gate）")
    p_pause.add_argument("--message", default=None)
    p_pause.set_defaults(handler=_cmd_pause)

    p_resume = sub.add_parser("resume", help="resume 最近 pause")
    p_resume.add_argument("--message", default=None)
    p_resume.set_defaults(handler=_cmd_resume)

    p_int = sub.add_parser("intervene", help="主动介入（emit + 立即 resolve）")
    p_int.add_argument("--message", required=True)
    p_int.add_argument("--action", required=True, choices=RESOLUTION_ACTIONS)
    p_int.add_argument("--patch", default=None)
    p_int.add_argument("--target-node", default=None)
    p_int.set_defaults(handler=_cmd_intervene)

    return p


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    # list --status all → None（不筛）
    if args.cmd == "list" and args.status == "all":
        args.status = None
    project = (args.project or PROJECT_NAME or "default").strip()
    try:
        return args.handler(args, project)
    except FileNotFoundError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
