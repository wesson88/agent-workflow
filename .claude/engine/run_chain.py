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
from engine.git_sync import sync_after_run
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

    try:
        summary = (
            f"工作流：{template.name}\n"
            f"任务：{args.task}\n"
            f"成功：{succeeded}\n"
            f"失败：{failed or '无'}\n"
            f"跳过：{skipped or '无'}"
        )
        url = sync_after_run(project=project, summary=summary)
        if url:
            print(f"\n📌 审阅入口：{url}")
    except Exception as e:
        print(f"\n⚠️ git_sync 失败（不影响产出文件）：{e}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
