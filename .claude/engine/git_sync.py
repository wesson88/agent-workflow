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
from collections.abc import Sequence
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


def dirty_paths() -> set[str]:
    """vault 当前所有未提交路径（含 untracked、含改名两端），仓库相对 posix 形式。

    用途见 `changed_since` —— 工作流只提交**它自己动过**的文件，不能把用户手上
    的活一起卷走。

    `-z` + `core.quotepath=false`：vault 路径几乎全是中文，porcelain 默认会把
    非 ASCII 整条路径加引号并做 `\\346\\210\\221` 式八进制转义，直接拿去 `git add`
    是加不上的。`-uall` 让新目录展开到文件级而不是只报一个目录名（否则「用户新建
    的目录里工作流写了一个文件」会整目录进来）。
    """
    out = _run(
        "-c", "core.quotepath=false",
        "status", "--porcelain", "-z", "-uall", capture=True,
    ).stdout
    fields = [f for f in out.split("\0") if f]
    paths: set[str] = set()
    i = 0
    while i < len(fields):
        rec = fields[i]
        i += 1
        if len(rec) < 4:
            continue
        xy, path = rec[:2], rec[3:]
        paths.add(path)
        if "R" in xy or "C" in xy:
            # 改名 / 复制：紧跟的下一个字段是**源**路径，两端都得算动过
            if i < len(fields):
                paths.add(fields[i])
                i += 1
    return paths


def changed_since(before: set[str]) -> list[str]:
    """本轮新出现的未提交路径 = 现在脏的 − 跑之前就脏的。

    2026-09-03 收窄 `add -A` 的实现基础。原先 `sync_after_run` 不传 paths →
    `commit_and_push` 走 `add -A`，而 vault 的 `.gitignore` 把工作流的正常产出
    **全部**排除在外（`10-项目/` / `99-临时/` / `00-系统/.runtime-state/` /
    `98-待办/`）—— 也就是说 `add -A` 在正常运行时能扫到的，**只剩用户自己没提交
    的活**，然后以 `agent: <project> — 工作流：…` 的名义提交掉。

    刻意用 porcelain 差集而不是 audit 里的 `outputs`：audit 是带 buffer 的
    （`_BUFFER_FLUSH_THRESHOLD`，靠 atexit 落盘），run_chain 在同进程内读不到本轮
    条目；且 subprocess 模式的角色产出根本不回到 graph state。git 自己的脏路径
    集合是唯一不依赖这两条链路的事实来源。

    **跑之前就脏的一律不碰**，即使本轮也改了它：宁可漏提交也不覆盖用户在编辑的
    文件 —— 漏了用户 `git add` 一下就行，卷走了得从 reflog 捞。
    """
    return sorted(dirty_paths() - before)


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
    paths: Sequence[str | Path],
    *,
    rebase_main: bool = True,
) -> bool:
    """提交并推送到 origin/agent。

    - paths：限定 add 的子路径（vault 相对或绝对均可）。**必填**，空列表 =
      本轮无可提交内容，直接返回 False
    - rebase_main：push 前 fetch & rebase origin/main，避免 agent 持续落后
    返回是否真的产生了新 commit（无变更则返回 False）。

    2026-09-03 去掉 `paths=None → add -A` 那条兜底路径，改为 `None` 直接抛。
    理由与 `AGENT_BRANCH` 常量同源：真正的护栏是让危险动作**在结构上无法表达**，
    而不是靠每个调用方记得传参。实测 `sync_after_run` 就没传（run_chain:183），
    于是每轮工作流都在 `add -A`；而 vault 的 `.gitignore` 排掉了工作流的全部正常
    产出，`add -A` 能扫到的只剩用户自己没提交的活。详见 `changed_since`。
    """
    if paths is None:
        raise ValueError(
            "commit_and_push 必须显式传 paths —— 不再支持 `add -A` 兜底。"
            "本轮改动路径用 git_sync.changed_since(before) 取（before 由跑之前的 "
            "dirty_paths() 快照得到）。"
            "若确实要提交整个工作区，显式传那些路径。"
        )
    if not paths:
        return False

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
    for p in paths:
        _run("add", "--", str(p))

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
    paths: Sequence[str | Path],
) -> str | None:
    """每轮 agent 任务结束后调用。

    流程：切 agent → rebase main → commit 改动 → push → 开/更新 PR。
    返回 PR URL（或 compare URL）；如本轮无变更则返回 None。

    `paths` **必填**（2026-09-03）：调用方负责界定"本轮动了什么"。run_chain 用
    `dirty_paths()` 前后快照差集（见 changed_since）。
    """
    pushed = commit_and_push(
        message=f"agent: {project} — {summary}",
        paths=paths,
    )
    if not pushed:
        print(f"ℹ️ {project} 本轮无可提交改动，跳过 PR。")
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
