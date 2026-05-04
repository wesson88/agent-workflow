"""
git_sync.py — vault 仓库的 git 工作流封装。

约束（来自 vault README + 项目 memory）：
- 所有 push 必须落到 'agent' 分支，禁止直推 main
- 提交后通过 PR + 手动 Merge 进入 main
- gh CLI 可用时自动开 / 复用 PR；不可用时回退为打印链接由用户手动开

注意：这些函数对 vault 仓（VAULT_ROOT）操作，不影响 agent-workflow 工程仓本身。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .config import VAULT_ROOT


AGENT_BRANCH = "agent"
DEFAULT_BASE = "main"


# ── 内部 helper ──────────────────────────────────────────
def _run(*args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    """对 vault 仓执行 git 命令。"""
    cmd = ["git", "-C", str(VAULT_ROOT), *args]
    return subprocess.run(
        cmd,
        check=check,
        capture_output=capture,
        text=True,
        encoding="utf-8",
    )


def _current_branch() -> str:
    return _run("rev-parse", "--abbrev-ref", "HEAD", capture=True).stdout.strip()


def _has_uncommitted() -> bool:
    out = _run("status", "--porcelain", capture=True).stdout
    return bool(out.strip())


def _gh_available() -> bool:
    return shutil.which("gh") is not None


def _gh_authed() -> bool:
    if not _gh_available():
        return False
    return subprocess.run(
        ["gh", "auth", "status"], capture_output=True
    ).returncode == 0


def _origin_url() -> str:
    return _run("remote", "get-url", "origin", capture=True).stdout.strip()


# ── 公共 API ─────────────────────────────────────────────
def ensure_on_agent_branch() -> None:
    """切到 agent 分支；不存在则从 origin/agent（或 main）创建。"""
    if _current_branch() == AGENT_BRANCH:
        return
    # 已存在的本地分支
    branches = _run("branch", "--format=%(refname:short)", capture=True).stdout.split()
    if AGENT_BRANCH in branches:
        _run("switch", AGENT_BRANCH)
        return
    # 尝试从 origin/agent 创建
    _run("fetch", "origin", check=False)
    remote_branches = _run("branch", "-r", "--format=%(refname:short)", capture=True).stdout.split()
    if f"origin/{AGENT_BRANCH}" in remote_branches:
        _run("switch", "-c", AGENT_BRANCH, f"origin/{AGENT_BRANCH}")
    else:
        # 从当前 main 创建并 push -u
        _run("switch", "-c", AGENT_BRANCH)
        _run("push", "-u", "origin", AGENT_BRANCH)


def commit_and_push(
    message: str,
    paths: list[str | Path] | None = None,
    *,
    rebase_main: bool = True,
) -> bool:
    """提交并推送到 origin/agent。

    - paths：限定 add 的子路径（vault 相对或绝对均可）。None 表示 add -A
    - rebase_main：push 前 fetch & rebase origin/main，避免 agent 持续落后
    返回是否真的产生了新 commit（无变更则返回 False）。
    """
    ensure_on_agent_branch()

    if rebase_main:
        try:
            _run("fetch", "origin")
            _run("rebase", f"origin/{DEFAULT_BASE}")
        except subprocess.CalledProcessError:
            print(
                "⚠️ rebase origin/main 出现冲突，自动回退（保持 agent 当前状态）。"
                "请手动检查："
                f"\n   git -C \"{VAULT_ROOT}\" status"
            )
            _run("rebase", "--abort", check=False)

    # add
    if paths:
        for p in paths:
            _run("add", "--", str(p))
    else:
        _run("add", "-A")

    if not _has_uncommitted() and _run(
        "diff", "--cached", "--quiet", check=False
    ).returncode == 0:
        # 无新变更
        return False

    _run("commit", "-m", message)
    _run("push", "origin", AGENT_BRANCH)
    return True


def open_or_update_pr(title: str, body: str, base: str = DEFAULT_BASE) -> str | None:
    """开/复用 PR：agent → base。

    - gh 已登录：尝试 gh pr edit（已存在则更新），否则 gh pr create
    - gh 不可用：解析 origin URL 拼出 compare 链接，打印给用户
    返回 PR URL 或 compare URL，None 表示完全失败。
    """
    if _gh_authed():
        # 已有 PR 则 edit；否则 create
        existing = subprocess.run(
            ["gh", "pr", "list", "--head", AGENT_BRANCH,
             "--base", base, "--json", "number,url"],
            capture_output=True, text=True, encoding="utf-8",
        )
        import json
        try:
            prs = json.loads(existing.stdout) if existing.returncode == 0 else []
        except json.JSONDecodeError:
            prs = []

        if prs:
            number = prs[0]["number"]
            subprocess.run(
                ["gh", "pr", "edit", str(number),
                 "--title", title, "--body", body],
                check=False,
            )
            return prs[0].get("url")
        else:
            create = subprocess.run(
                ["gh", "pr", "create",
                 "--base", base, "--head", AGENT_BRANCH,
                 "--title", title, "--body", body],
                capture_output=True, text=True, encoding="utf-8",
            )
            if create.returncode == 0:
                return create.stdout.strip()
            print(f"⚠️ gh pr create 失败：{create.stderr}")

    # gh 不可用：返回 compare 链接
    url = _origin_url().rstrip("/").removesuffix(".git")
    if url.startswith("git@github.com:"):
        url = "https://github.com/" + url[len("git@github.com:"):]
    if "github.com" in url or "gitee.com" in url:
        compare = f"{url}/compare/{base}...{AGENT_BRANCH}"
        print(f"💡 gh 未安装/未登录。请在浏览器手动开 PR：\n   {compare}")
        return compare
    print("⚠️ 无法识别远端类型，且 gh 不可用。请手动发起 PR。")
    return None


def sync_after_run(
    project: str,
    summary: str,
    *,
    paths: list[str | Path] | None = None,
) -> str | None:
    """每轮 agent 任务结束后调用。

    流程：切 agent → rebase main → commit 改动 → push → 开/更新 PR。
    返回 PR URL（或 compare URL）；如本轮无变更则返回 None。
    """
    pushed = commit_and_push(
        message=f"agent: {project} — {summary}",
        paths=paths,
    )
    if not pushed:
        print(f"ℹ️ {project} 本轮无变更，跳过 PR。")
        return None

    return open_or_update_pr(
        title=f"agent 工作流产出：{project}",
        body=(
            f"## 概要\n{summary}\n\n"
            f"## 项目\n`{project}`\n\n"
            "本 PR 由编排引擎自动维护：每次 agent 跑出新内容会追加到本 PR，"
            "审阅完成后请手动 Merge。"
        ),
    )
