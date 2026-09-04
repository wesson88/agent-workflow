"""
engine/run_chain.py — 工作流模板驱动的链路执行器（Phase 4a：LangGraph）

链路从 vault `00-系统/工作流模板/工作流-*.md` 读取，由 LangGraph StateGraph
执行（替代 Phase 3a 的子进程串行循环）。

CLI：
    python .claude/engine/run_chain.py --task "..." --project myproj
    python .claude/engine/run_chain.py --task "..." --workflow 技术开发
    python .claude/engine/run_chain.py --task "..." --start-from 架构师
    python .claude/engine/run_chain.py --task "..." --end-at 技术主管
    python .claude/engine/run_chain.py --task "..." --skip-git
    python .claude/engine/run_chain.py --list-workflows

Phase 4a：每个角色一个 LangGraph node，subprocess 包装现有 main.py（行为等价）。
Phase 4b：加讨论循环节点（架构师 ↔ 技术主管），需 in-process 重构 main.py 核心逻辑。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Windows 控制台 utf-8 重配置（emoji + 中文显示）
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

# 让脚本能独立运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.config import PROJECT_NAME
from engine.git_sync import changed_since, dirty_paths, sync_after_run
from engine.human_gate import list_gates as list_human_gates
from engine.workflow import load_workflow, list_workflows
from engine.graph import build_graph

DEFAULT_WORKFLOW = "技术开发"


# ── CLI ─────────────────────────────────────────────────
def parse_chain_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="按 vault 工作流模板顺序执行角色链路")
    parser.add_argument(
        "--workflow", default=DEFAULT_WORKFLOW,
        help=f"工作流模板名（vault 00-系统/工作流模板/工作流-<name>.md，默认 '{DEFAULT_WORKFLOW}'）",
    )
    parser.add_argument("--task", help="任务描述（必填，除非 --list-workflows）")
    parser.add_argument(
        "--project", default=None,
        help="项目名（缺省从 PROJECT env / .env 读取，最终默认 'default'）",
    )
    parser.add_argument(
        "--start-from", default=None,
        help="从哪个角色开始（接受中文名或英文别名，默认从头）",
    )
    parser.add_argument(
        "--end-at", default=None,
        help="跑到哪个角色为止（接受中文名或英文别名，默认跑完）",
    )
    parser.add_argument(
        "--skip-git", action="store_true",
        help="跑完后不调 git_sync.sync_after_run（本地试跑用）",
    )
    parser.add_argument(
        "--list-workflows", action="store_true",
        help="列出 vault 中所有可用的工作流模板后退出",
    )
    return parser.parse_args()


# ── 列出模板 ────────────────────────────────────────────
def _print_workflows() -> int:
    workflows = list_workflows()
    if not workflows:
        print("（vault 00-系统/工作流模板/ 下未找到任何 工作流-*.md）")
        return 1
    print("已配置的工作流模板：\n")
    for w in workflows:
        steps_preview = " → ".join(s.role or s.type for s in w.steps)
        print(f"  • {w.name}  [{w.domain}]")
        print(f"    {w.description}")
        print(f"    链路：{steps_preview}")
        print()
    return 0


# ── 主流程 ──────────────────────────────────────────────
def main() -> int:
    args = parse_chain_args()

    if args.list_workflows:
        return _print_workflows()

    if not args.task:
        raise SystemExit("❌ --task 必填（除非使用 --list-workflows）")

    project = (
        args.project
        or os.environ.get("PROJECT")
        or PROJECT_NAME
        or "default"
    ).strip()

    # T1.2 (2026-06-10)：主流程入口扫 pending human_gates。
    # Phase B bridge 之前的折中实现：文件落盘 + 轮询；命中 pending 立即退出，
    # 提示用户用 CLI 解决后再 run_chain。
    # Phase B 之后切 LangGraph interrupt（schema 向后兼容，CLI 共存）。
    pending = list_human_gates(project, status="pending")
    if pending:
        print(f"❌ 项目 '{project}' 有 {len(pending)} 个 pending human_gate，"
              f"必须先解决后再 run_chain：")
        for g in pending:
            tail = f"  [gate={g.gate}]" if g.gate else ""
            print(f"  - [{g.id}] {g.reason}{tail}")
        print()
        print(f"用以下命令解决：")
        print(f"  python .claude/engine/cli_human_gate.py --project {project} list")
        print(f"  python .claude/engine/cli_human_gate.py --project {project} show --id <gate-id>")
        print(f"  python .claude/engine/cli_human_gate.py --project {project} approve --id <gate-id>")
        raise SystemExit(2)

    # 加载工作流模板
    try:
        template = load_workflow(args.workflow)
    except KeyError as e:
        raise SystemExit(f"❌ {e}")

    # 构建 LangGraph
    try:
        graph = build_graph(template, start_from=args.start_from, end_at=args.end_at)
    except (NotImplementedError, ValueError) as e:
        raise SystemExit(f"❌ {e}")

    print(f"工作流：{template.name}（{template.description}）")
    print(f"项目：{project}")
    print(f"任务：{args.task}")
    print(f"halt_on_failure：{template.halt_on_failure}")
    print(f"引擎：LangGraph StateGraph")

    initial_state = {
        "project": project,
        "task": args.task,
        "workflow_name": template.name,
        "succeeded": [],
        "failed": [],
        "skipped": [],
        "halted": False,
    }

    # 跑之前的 vault 脏路径快照。工作流只提交**它自己动过**的文件 —— 跑之前就脏
    # 的一律不碰（哪怕本轮也改了它）。见 git_sync.changed_since 的依据。
    # 取不到（vault 不是 git 仓 / git 不可用）时置 None，跑完时按"无法界定范围"
    # 跳过 git 同步，而不是回落成 add -A。
    try:
        dirty_before: set[str] | None = dirty_paths()
    except Exception as e:
        dirty_before = None
        print(f"⚠️ 取 vault 脏路径快照失败，本轮跑完将跳过 git 同步：{e}")

    final_state = graph.invoke(initial_state)

    succeeded = final_state.get("succeeded", [])
    failed = final_state.get("failed", [])
    skipped = final_state.get("skipped", [])
    total = len(succeeded) + len(failed) + len(skipped)

    print(f"\n{'=' * 60}")
    print("汇总")
    print(f"  总步数：{total}")
    print(f"  成功：{succeeded}")
    if failed:
        print(f"  失败：{failed}")
    if skipped:
        print(f"  跳过：{skipped}")

    if args.skip_git:
        print("（--skip-git 已设置，不推送）")
        return 1 if failed else 0

    if dirty_before is None:
        print("（未能界定本轮改动范围，跳过 git 同步；产出文件不受影响）")
        return 1 if failed else 0

    try:
        summary = (
            f"工作流：{template.name}\n"
            f"任务：{args.task}\n"
            f"成功：{succeeded}\n"
            f"失败：{failed or '无'}\n"
            f"跳过：{skipped or '无'}"
        )
        touched = changed_since(dirty_before)
        if not touched:
            # vault 的 .gitignore 排掉了 10-项目/ 等工作流正常产出目录，所以
            # 「本轮没有可提交路径」是常态而非异常，不当失败处理。
            print("\nℹ️ 本轮未产生 vault 可提交改动（产出落在 gitignore 目录内），"
                  "跳过 git 同步。")
            return 1 if failed else 0
        print(f"\n本轮改动 {len(touched)} 个路径，仅提交这些：")
        for rel in touched:
            print(f"  · {rel}")
        url = sync_after_run(project=project, summary=summary, paths=touched)
        if url:
            print(f"\n📌 审阅入口：{url}")
    except Exception as e:
        print(f"\n⚠️ git_sync 失败（不影响产出文件）：{e}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
