"""
engine/run_chain.py — 多角色链路顺序执行器（Phase 2b）

替代旧的 .claude/script/optimize_all.py：
- 不读写 status.json（角色状态全在 vault frontmatter）
- 不做自愈补丁（Phase 4 的复盘 agent 接管）
- 失败即停（默认）；可关闭以便部分角色失败时仍把已有产出推到 agent 分支供审阅
- 跑完自动调 engine.git_sync.sync_after_run 推送 + 开/复用 PR（可禁用）

CLI：
    python .claude/engine/run_chain.py --task "..." --project myproj
    python .claude/engine/run_chain.py --task "..." --start-from chief_architect
    python .claude/engine/run_chain.py --task "..." --end-at technical_lead
    python .claude/engine/run_chain.py --task "..." --skip-git

LangGraph 版本（含讨论循环）会在 Phase 4 加入 engine/graph/。
"""

from __future__ import annotations

import argparse
import os
import subprocess
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

from engine.config import PROJECT_ROOT, PROJECT_NAME
from engine.git_sync import sync_after_run

DEFAULT_CHAIN = [
    "product_manager",
    "chief_architect",
    "technical_lead",
    "dev_backend",
    "dev_frontend",
]


def parse_chain_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="顺序运行 vault-based 工作流链路")
    parser.add_argument("--task", required=True, help="任务描述")
    parser.add_argument(
        "--project", default=None,
        help="项目名（缺省从 PROJECT env / .env 读取，最终默认 'default'）",
    )
    parser.add_argument(
        "--start-from", choices=DEFAULT_CHAIN, default=None,
        help="从哪个角色开始（默认从头）",
    )
    parser.add_argument(
        "--end-at", choices=DEFAULT_CHAIN, default=None,
        help="跑到哪个角色为止（默认跑完）",
    )
    parser.add_argument(
        "--skip-git", action="store_true",
        help="跑完后不调 git_sync.sync_after_run（本地试跑用）",
    )
    parser.add_argument(
        "--halt-on-failure", action=argparse.BooleanOptionalAction, default=True,
        help="单个角色失败时是否中断后续（默认中断）",
    )
    return parser.parse_args()


def select_chain(start_from: str | None, end_at: str | None) -> list[str]:
    chain = list(DEFAULT_CHAIN)
    if start_from:
        chain = chain[chain.index(start_from):]
    if end_at:
        chain = chain[: chain.index(end_at) + 1]
    return chain


def run_skill(skill: str, task: str, project: str) -> int:
    cmd = [
        sys.executable,
        str(PROJECT_ROOT / ".claude" / "skills" / skill / "main.py"),
        "--task", task,
        "--project", project,
    ]
    print(f"\n{'=' * 60}\n▶ 运行 {skill}（项目={project}）\n{'=' * 60}")
    env = os.environ.copy()
    env["PROJECT"] = project
    env["TASK"] = task
    return subprocess.run(cmd, env=env).returncode


def main() -> int:
    args = parse_chain_args()
    project = (
        args.project
        or os.environ.get("PROJECT")
        or PROJECT_NAME
        or "default"
    ).strip()

    chain = select_chain(args.start_from, args.end_at)
    print(f"链路：{' → '.join(chain)}")
    print(f"项目：{project}")
    print(f"任务：{args.task}")

    failed: list[str] = []
    succeeded: list[str] = []
    skipped: list[str] = []
    for i, skill in enumerate(chain):
        rc = run_skill(skill, args.task, project)
        if rc != 0:
            print(f"\n❌ {skill} 失败（exit={rc}）")
            failed.append(skill)
            if args.halt_on_failure:
                skipped = chain[i + 1:]
                print(f"中断后续步骤（--no-halt-on-failure 可关闭此行为）；跳过：{skipped}")
                break
        else:
            print(f"\n✅ {skill} 完成")
            succeeded.append(skill)

    print(f"\n{'=' * 60}")
    print("汇总")
    print(f"  尝试：{len(succeeded) + len(failed)} / {len(chain)}")
    print(f"  成功：{succeeded}")
    if failed:
        print(f"  失败：{failed}")
    if skipped:
        print(f"  未跑：{skipped}")

    if args.skip_git:
        print("（--skip-git 已设置，不推送）")
        return 1 if failed else 0

    try:
        summary = f"任务：{args.task}\n链路：{' → '.join(chain)}\n失败：{failed or '无'}"
        url = sync_after_run(project=project, summary=summary)
        if url:
            print(f"\n📌 审阅入口：{url}")
    except Exception as e:
        print(f"\n⚠️ git_sync 失败（不影响产出文件）：{e}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
