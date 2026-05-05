"""
engine/run_chain.py — 工作流模板驱动的链路执行器（Phase 3a）

链路从 vault `00-系统/工作流模板/工作流-*.md` 读取，不再硬编码。
角色名（中文）通过 `engine.workflow.role_to_skill_dir` 自动映射到
`.claude/skills/<英文目录>/main.py`。

CLI：
    python .claude/engine/run_chain.py --task "..." --project myproj
    python .claude/engine/run_chain.py --task "..." --workflow 技术开发
    python .claude/engine/run_chain.py --task "..." --start-from 架构师
    python .claude/engine/run_chain.py --task "..." --end-at 技术主管
    python .claude/engine/run_chain.py --task "..." --skip-git
    python .claude/engine/run_chain.py --list-workflows

Phase 4：LangGraph 引擎接管 parallel / discussion-loop 等步骤类型；
本文件继续负责 type=linear 的简单链路。
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
from engine.workflow import load_workflow, list_workflows, role_to_skill_dir
from engine.role_loader import load_role, RoleNotFound

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
        "--halt-on-failure", action=argparse.BooleanOptionalAction, default=None,
        help="单个角色失败时是否中断后续（默认沿用模板的 halt_on_failure）",
    )
    parser.add_argument(
        "--list-workflows", action="store_true",
        help="列出 vault 中所有可用的工作流模板后退出",
    )
    return parser.parse_args()


# ── 链路构造 ────────────────────────────────────────────
def _normalize(name_or_alias: str) -> str:
    """把任意 alias 解析为角色 frontmatter 的 role 字段（中文名）。"""
    return load_role(name_or_alias).name


def _slice_chain(chain: list[str], start_from: str | None, end_at: str | None) -> list[str]:
    """按 start-from / end-at 截取链路；接受中文或英文别名输入。"""
    if start_from:
        target = _normalize(start_from)
        try:
            idx = chain.index(target)
        except ValueError:
            raise SystemExit(
                f"❌ --start-from='{start_from}' 不在工作流链路中。"
                f"链路：{chain}"
            )
        chain = chain[idx:]
    if end_at:
        target = _normalize(end_at)
        try:
            idx = chain.index(target)
        except ValueError:
            raise SystemExit(
                f"❌ --end-at='{end_at}' 不在工作流链路中。链路：{chain}"
            )
        chain = chain[: idx + 1]
    return chain


# ── 执行单步 ────────────────────────────────────────────
def run_step(role_name: str, task: str, project: str) -> int:
    """执行一个角色：把 vault 中文角色名映射到 skill 目录，subprocess 调 main.py。"""
    skill_dir = role_to_skill_dir(role_name)
    cmd = [
        sys.executable,
        str(PROJECT_ROOT / ".claude" / "skills" / skill_dir / "main.py"),
        "--task", task,
        "--project", project,
    ]
    print(f"\n{'=' * 60}\n▶ 运行 {role_name} ({skill_dir})  项目={project}\n{'=' * 60}")
    env = os.environ.copy()
    env["PROJECT"] = project
    env["TASK"] = task
    return subprocess.run(cmd, env=env).returncode


# ── 主流程 ──────────────────────────────────────────────
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

    try:
        chain = template.linear_role_names()
    except NotImplementedError as e:
        raise SystemExit(f"❌ {e}")

    # 规范化为中文角色名（兼容用户写英文别名的模板）
    chain = [_normalize(r) for r in chain]
    chain = _slice_chain(chain, args.start_from, args.end_at)

    halt_on_failure = (
        args.halt_on_failure
        if args.halt_on_failure is not None
        else template.halt_on_failure
    )

    print(f"工作流：{template.name}（{template.description}）")
    print(f"链路：{' → '.join(chain)}")
    print(f"项目：{project}")
    print(f"任务：{args.task}")
    print(f"halt_on_failure：{halt_on_failure}")

    failed: list[str] = []
    succeeded: list[str] = []
    skipped: list[str] = []
    for i, role_name in enumerate(chain):
        rc = run_step(role_name, args.task, project)
        if rc != 0:
            print(f"\n❌ {role_name} 失败（exit={rc}）")
            failed.append(role_name)
            if halt_on_failure:
                skipped = chain[i + 1:]
                print(f"中断后续（--no-halt-on-failure 可关闭）；跳过：{skipped}")
                break
        else:
            print(f"\n✅ {role_name} 完成")
            succeeded.append(role_name)

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
        summary = (
            f"工作流：{template.name}\n"
            f"任务：{args.task}\n"
            f"链路：{' → '.join(chain)}\n"
            f"失败：{failed or '无'}"
        )
        url = sync_after_run(project=project, summary=summary)
        if url:
            print(f"\n📌 审阅入口：{url}")
    except Exception as e:
        print(f"\n⚠️ git_sync 失败（不影响产出文件）：{e}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
