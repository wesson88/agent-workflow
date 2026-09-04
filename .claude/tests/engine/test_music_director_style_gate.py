"""音乐总监终稿 Style 超限闸门（P0-3 ③，2026-09-03）。

此前 `style_oversized` 量出来只写进 audit，`set_role_status(status="success")`
无条件执行 —— 所有产物检查都是**事后记账，不是准入门槛**。实测靠总监自觉在
下游收窄（1025→909 / 2314→868），机制上没有任何东西保证这件事发生。

本次只把终稿变成门槛：
- `final-Suno-prompt.md` 超 1000 → 判失败（它是 user 直接复制进 Suno 的那份）
- 作曲的 `Suno-prompt.md` 超限 → 只告警（基线，总监下游本来就会收窄）
- 文件**已落盘**才拦：超限稿是排查依据，删掉等于把这一轮产出丢了
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SKILLS = Path(__file__).resolve().parents[2] / "skills"


@pytest.fixture()
def director(monkeypatch, tmp_path):
    """加载 music_director.main，把落盘 / 状态 / audit 全部接管。"""
    sys.path.insert(0, str(_SKILLS))
    spec = importlib.util.spec_from_file_location(
        "md_main_under_test", _SKILLS / "music_director" / "main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    state: dict = {"audit": [], "status": [], "written": {}}
    monkeypatch.setattr(mod, "append_audit", state["audit"].append)
    monkeypatch.setattr(
        mod, "set_role_status",
        lambda role, **kw: state["status"].append(kw.get("status")))
    monkeypatch.setattr(mod, "resolve_path", lambda rel, project: tmp_path / rel)
    monkeypatch.setattr(
        mod, "write_output_atomic",
        lambda dest, content: state["written"].__setitem__(str(dest), content))
    mod._state = state
    return mod


def _canned(rel: str, style: str) -> str:
    return (
        f"<!-- FILE: {rel} -->\n"
        f"# Suno\n\n## Style\n\n```\n{style}\n```\n"
        f"<!-- /FILE -->\n"
    )


class TestFinalStyleGate:
    def test_终稿超限判失败(self, director, monkeypatch, capsys):
        style = "x" * (director._SUNO_STYLE_HARD_LIMIT + 1)
        monkeypatch.setattr(
            director, "call_claude",
            lambda s, u, r: _canned("10-项目/music/{project}/final-Suno-prompt.md", style))

        rc = director._call_and_write("sys", "usr", "任务", "p", audit_extras={})

        assert rc == 1
        audit = director._state["audit"][-1]
        assert audit["result"] == "failed"
        assert audit["error"] == "style_oversized"
        assert audit["final_style_char_count"] == len(style)
        assert audit["final_style_oversized"] is True
        assert "failed" in director._state["status"]
        assert "success" not in director._state["status"]
        assert "超硬上限" in capsys.readouterr().err

    def test_判失败前文件已落盘(self, director, monkeypatch):
        """超限稿是排查依据（要看超在哪、砍哪段），不能因为拦就丢掉。"""
        style = "x" * (director._SUNO_STYLE_HARD_LIMIT + 500)
        monkeypatch.setattr(
            director, "call_claude",
            lambda s, u, r: _canned("10-项目/music/{project}/final-Suno-prompt.md", style))

        assert director._call_and_write("s", "u", "t", "p", audit_extras={}) == 1
        assert any("final-Suno-prompt.md" in k
                   for k in director._state["written"]), "拦了但文件没落盘"

    def test_刚好卡上限不算超(self, director, monkeypatch):
        style = "x" * director._SUNO_STYLE_HARD_LIMIT
        monkeypatch.setattr(
            director, "call_claude",
            lambda s, u, r: _canned("10-项目/music/{project}/final-Suno-prompt.md", style))

        assert director._call_and_write("s", "u", "t", "p", audit_extras={}) == 0
        assert director._state["audit"][-1]["final_style_oversized"] is False

    def test_中间产物超限不拦(self, director, monkeypatch):
        """作曲的 Suno-prompt.md 是基线，总监下游本来就会收窄
        （实测 1025→909 / 2314→868）。在这里拦 = 把正常中间态判成失败。"""
        style = "x" * (director._SUNO_STYLE_HARD_LIMIT + 1)
        monkeypatch.setattr(
            director, "call_claude",
            lambda s, u, r: _canned("10-项目/music/{project}/Suno-prompt.md", style))

        rc = director._call_and_write("s", "u", "t", "p", audit_extras={})
        assert rc == 0
        audit = director._state["audit"][-1]
        assert audit["result"] == "success"
        assert audit["final_style_oversized"] is True, "不拦不等于不记账"

    def test_抽不出Style段不判失败只告警(self, director, monkeypatch, capsys):
        """`None` 不是「超限」；拿不到数该喊，但不该按超限处理。"""
        monkeypatch.setattr(
            director, "call_claude",
            lambda s, u, r: ("<!-- FILE: 10-项目/music/{project}/final-Suno-prompt.md -->\n"
                             "x\n<!-- /FILE -->\n"))

        rc = director._call_and_write("s", "u", "t", "p", audit_extras={})
        assert rc == 0
        assert director._state["audit"][-1]["final_style_char_count"] is None
        assert "抽不出 Style 段" in capsys.readouterr().err

    def test_没有Suno产物时不进这条路径(self, director, monkeypatch):
        """first-pass 产的是 vision / 指令，不该冒出 final_style_* 字段。"""
        monkeypatch.setattr(
            director, "call_claude",
            lambda s, u, r: ("<!-- FILE: 10-项目/music/{project}/创作 vision.md -->\n"
                             "# vision\n<!-- /FILE -->\n"))

        assert director._call_and_write("s", "u", "t", "p", audit_extras={}) == 0
        assert "final_style_char_count" not in director._state["audit"][-1]
