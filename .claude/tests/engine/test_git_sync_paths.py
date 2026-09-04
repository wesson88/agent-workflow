"""git_sync 的 add 范围：工作流只提交它自己动过的文件。

背景（2026-09-03）：`sync_after_run` 一直没给 `commit_and_push` 传 paths，
于是走 `add -A`。而 vault `.gitignore` 把工作流的正常产出**全部**排除
（`10-项目/` / `99-临时/` / `00-系统/.runtime-state/` / `98-待办/`）—— `add -A`
在正常运行时能扫到的只剩用户自己没提交的活，然后以 `agent: <project> — 工作流：…`
的名义提交掉。本文件锁住修法：
  - `paths=None` 直接抛（危险动作在结构上不可表达，与 AGENT_BRANCH 常量同源）
  - `changed_since` 用 porcelain 前后差集界定范围，跑之前就脏的一律不碰
"""

from __future__ import annotations

import subprocess

import pytest

from engine import git_sync as gs


def _git(root, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=check, capture_output=True, text=True, encoding="utf-8",
    )


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    """一个真 git 仓当 vault，已有一个 commit + 一份 .gitignore。"""
    root = tmp_path / "vault"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t.t")
    _git(root, "config", "user.name", "t")
    # 复刻真 vault 的关键 ignore 项：工作流产出目录不入仓
    (root / ".gitignore").write_text(
        "10-项目/\n99-临时/\n00-系统/.runtime-state/\n98-待办/\n",
        encoding="utf-8",
    )
    (root / "20-知识").mkdir()
    (root / "20-知识" / "已有笔记.md").write_text("原文\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    monkeypatch.setattr(gs, "VAULT_ROOT", root)
    return root


class TestDirtyPaths:
    def test_干净仓返回空(self, repo):
        assert gs.dirty_paths() == set()

    def test_中文路径不被八进制转义(self, repo):
        """porcelain 默认把非 ASCII 路径整条加引号 + `\\346\\210\\221` 式转义，
        直接拿去 `git add` 加不上。vault 路径几乎全是中文，这条是必须的。"""
        p = repo / "20-知识" / "新中文笔记.md"
        p.write_text("x\n", encoding="utf-8")
        got = gs.dirty_paths()
        assert got == {"20-知识/新中文笔记.md"}
        # 坐实前提：这个字符串真的能拿去 add（八进制转义版本 add 不上）
        _git(repo, "add", "--", next(iter(got)))
        staged = _git(repo, "diff", "--cached", "--name-only", "-z").stdout
        assert "20-知识/新中文笔记.md" in staged.split("\0")

    def test_新目录展开到文件级(self, repo):
        """`-uall`：否则 git 只报一个目录名，用户新建目录里工作流写了一个文件
        就会整目录被卷进来。"""
        d = repo / "20-知识" / "新目录"
        d.mkdir()
        (d / "a.md").write_text("a\n", encoding="utf-8")
        (d / "b.md").write_text("b\n", encoding="utf-8")
        assert gs.dirty_paths() == {"20-知识/新目录/a.md", "20-知识/新目录/b.md"}

    def test_gitignore内的产出不算脏(self, repo):
        """工作流正常产出全在 ignore 目录里 —— 这正是 `add -A` 只剩用户改动可扫的原因。"""
        (repo / "10-项目" / "proj").mkdir(parents=True)
        (repo / "10-项目" / "proj" / "PRD.md").write_text("prd\n", encoding="utf-8")
        assert gs.dirty_paths() == set()

    def test_改名两端都算(self, repo):
        _git(repo, "mv", "20-知识/已有笔记.md", "20-知识/改名后.md")
        assert gs.dirty_paths() == {"20-知识/已有笔记.md", "20-知识/改名后.md"}


class TestChangedSince:
    def test_只报本轮新增的(self, repo):
        (repo / "20-知识" / "用户在写.md").write_text("draft\n", encoding="utf-8")
        before = gs.dirty_paths()
        (repo / "20-知识" / "工作流产出.md").write_text("out\n", encoding="utf-8")
        assert gs.changed_since(before) == ["20-知识/工作流产出.md"]

    def test_跑之前就脏的即使本轮也改了也不碰(self, repo):
        """宁可漏提交也不覆盖用户在编辑的文件 —— 漏了 `git add` 一下就行，
        卷走了得从 reflog 捞。"""
        p = repo / "20-知识" / "已有笔记.md"
        p.write_text("用户改了一半\n", encoding="utf-8")
        before = gs.dirty_paths()
        assert before == {"20-知识/已有笔记.md"}
        p.write_text("用户改了一半\n工作流又追加\n", encoding="utf-8")
        assert gs.changed_since(before) == []

    def test_全程干净时为空(self, repo):
        before = gs.dirty_paths()
        assert gs.changed_since(before) == []


class TestPathsRequired:
    def test_None直接抛(self, repo):
        with pytest.raises(ValueError, match="必须显式传 paths"):
            gs.commit_and_push("msg", None)

    def test_空列表返回False且不produce_commit(self, repo, monkeypatch):
        called: list[tuple] = []
        monkeypatch.setattr(gs, "_run", lambda *a, **k: called.append(a))
        assert gs.commit_and_push("msg", []) is False
        assert called == [], "空 paths 不该碰 git（连切分支都不该）"

    def test_不再存在add_dash_A调用(self):
        """回归：源码里不该再有无路径的 `add -A`。"""
        from pathlib import Path
        src = Path(gs.__file__).read_text(encoding="utf-8")
        assert '"add", "-A"' not in src

    def test_sync_after_run的paths是必填(self):
        import inspect
        sig = inspect.signature(gs.sync_after_run)
        p = sig.parameters["paths"]
        assert p.default is inspect.Parameter.empty, "paths 有默认值 = 又能忘传"
        assert p.kind is inspect.Parameter.KEYWORD_ONLY


class TestCommitScope:
    def test_只add传进来的路径(self, repo, monkeypatch):
        """核心回归：另一个脏文件在同一次调用里不该被提交。"""
        monkeypatch.setattr(gs, "ensure_on_agent_branch", lambda: None)
        monkeypatch.setattr(gs, "_run", _recording_run(repo))
        (repo / "20-知识" / "工作流产出.md").write_text("out\n", encoding="utf-8")
        (repo / "20-知识" / "用户在写.md").write_text("draft\n", encoding="utf-8")

        assert gs.commit_and_push(
            "msg", ["20-知识/工作流产出.md"], rebase_main=False) is True

        committed = [
            f for f in _git(
                repo, "show", "--name-only", "--pretty=format:", "-z", "HEAD",
            ).stdout.split("\0") if f
        ]
        assert committed == ["20-知识/工作流产出.md"]
        # 用户手上的活还在工作区，没被卷走
        assert gs.dirty_paths() == {"20-知识/用户在写.md"}


def _recording_run(root):
    """真跑 git，但把 push 变成 no-op（测试仓无 remote）。"""
    def run(*args, check=True, capture=False):
        if args and args[0] == "push":
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.run(
            ["git", "-C", str(root), *args],
            check=check, capture_output=capture, text=True, encoding="utf-8",
        )
    return run
