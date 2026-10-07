"""音乐链 `对我说好` 首跑暴露的四处缺陷（2026-10-07）。

复盘：vault `20-知识/项目记录/音乐域/对我说好-首跑引擎缺陷四连-2026-10-07.md`

一 · F-雷鬼 的 `reggae` 子串命中 `Reggaeton` → 流派闸门 / primitive 加载改整词
二 · 总监首次模式照抄全量 outputs → 只产 vision + 给制作人
三 · FILE 块路径拼错（`对我好`）原样落盘 + 总监按文件猜模式 → 路径校验 + 引擎显式模式信号
四 · Style 抽取取全文首个 ``` → 开围栏锚行首
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from engine.graph.build import _role_occurrences, _slice_steps
from engine.role_runner import measure_style_chars, partition_declared_outputs
from engine.skill_trigger import score_skill
from engine.workflow import WorkflowStep, load_workflow

_SKILLS = Path(__file__).resolve().parents[2] / "skills"


# ── 四 · Style 抽取 ───────────────────────────────────────────
class TestStyleBlockAnchoredAtLineStart:
    def test_正文行内三反引号不算开围栏(self):
        """实测原样：§1 一句话里的 ``` 被当成起点，量出 1491，真实 579。"""
        style = "Colombian reggaeton and cumbia fusion.\n96 BPM, D minor."
        content = (
            "## 1. 使用指引\n\n复制时只取代码块里面的内容，不要带上 ``` 本身。\n\n"
            "| 项 | 值 |\n|---|---|\n| 模型 | v6-wild |\n\n"
            f"## 2. Style 段\n\n```\n{style}\n```\n\n## 3. Lyrics 段\n\n```\n[Verse]\nla\n```\n"
        )
        assert measure_style_chars(content) == len(style)

    def test_带语言标注的围栏照常识别(self):
        assert measure_style_chars("x\n```text\nabc\n```\n") == 3


# ── 一 · 整词匹配 ─────────────────────────────────────────────
@pytest.fixture()
def reggae_primitive(tmp_path):
    p = tmp_path / "F-雷鬼.md"
    p.write_text(
        "---\ntrigger:\n  keywords:\n    - 雷鬼\n    - reggae\n    - Roots Reggae\n---\n# x\n",
        encoding="utf-8",
    )
    return p


class TestWholeWordKeyword:
    def test_reggaeton_不命中_reggae(self, reggae_primitive):
        m = score_skill(reggae_primitive, "现代哥伦比亚 urbano（reggaeton 60 + cumbia 40）",
                        whole_word=True)
        assert not m.matched

    def test_默认仍是子串匹配(self, reggae_primitive):
        """全域技能 keyword 按子串写（api 要命中 apis），默认行为不变。"""
        assert score_skill(reggae_primitive, "Reggaeton").matched

    @pytest.mark.parametrize("text", [
        "roots reggae 骨架", "Reggae, dub", "(reggae)", "民谣雷鬼 fusion",
    ])
    def test_真雷鬼照常命中(self, reggae_primitive, text):
        assert score_skill(reggae_primitive, text, whole_word=True).matched

    def test_上游文本同样整词(self, reggae_primitive):
        m = score_skill(reggae_primitive, "", "dembow 来自 reggaeton", whole_word=True)
        assert not m.matched


# ── 三a · FILE 块路径校验 ─────────────────────────────────────
class TestPartitionDeclaredOutputs:
    DECLARED = [
        "10-项目/music/对我说好/混音评估.md",
        "10-项目/music/对我说好/混音-Suno-retry补丁.md",
    ]

    def test_漏字路径被拒(self):
        """实测原样：第二份产物写成 `对我好/`。"""
        ok, rejected = partition_declared_outputs(
            {
                "10-项目/music/对我说好/混音评估.md": "a",
                "10-项目/music/对我好/混音-Suno-retry补丁.md": "b",
            },
            self.DECLARED, "对我说好",
        )
        assert list(ok) == ["10-项目/music/对我说好/混音评估.md"]
        assert rejected == ["10-项目/music/对我好/混音-Suno-retry补丁.md"]

    def test_project占位符与相对前缀归一(self):
        ok, rejected = partition_declared_outputs(
            {
                "10-项目/music/{project}/混音评估.md": "a",
                "./10-项目/music/对我说好/混音-Suno-retry补丁.md": "b",
            },
            self.DECLARED, "对我说好",
        )
        assert rejected == []
        assert set(ok) == set(self.DECLARED)


# ── 三b · 引擎模式信号 ────────────────────────────────────────
class TestRoleOccurrences:
    def test_音乐制作模板总监两次(self):
        steps = load_workflow("音乐制作").steps
        occ = _role_occurrences(steps)
        director = [occ.get(id(s)) for s in steps if s.role == "音乐总监"]
        assert director == [1, 2]
        # 只出现一次的角色不注入信号
        assert all(id(s) not in occ for s in steps if s.role != "音乐总监")

    def test_裁剪后仍按完整模板计数(self):
        """`--start-from 编曲` 时末尾的总监仍是第 2 次，不能因为裁掉了第 1 次就变成 1。"""
        steps = load_workflow("音乐制作").steps
        occ = _role_occurrences(steps)
        sliced = _slice_steps(steps, "编曲", None)
        assert occ.get(id(sliced[-1])) == 2

    def test_同字段的两个step不撞键(self):
        steps = tuple(WorkflowStep.from_yaml("音乐总监") for _ in range(2))
        assert steps[0] == steps[1]  # 前提：按值相等
        assert sorted(_role_occurrences(steps).values()) == [1, 2]


# ── 二 / 三b · 总监 main.py ──────────────────────────────────
_AGG = ("Suno-prompt.md", "编曲-Suno补丁.md", "和声-Suno补丁.md",
        "混音-Suno-retry补丁.md", "母带-Suno-retry补丁.md")


@pytest.fixture()
def director(monkeypatch, tmp_path):
    sys.path.insert(0, str(_SKILLS))
    spec = importlib.util.spec_from_file_location(
        "md_main_0207", _SKILLS / "music_director" / "main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    state: dict = {"audit": [], "written": {}}
    monkeypatch.setattr(mod, "append_audit", state["audit"].append)
    monkeypatch.setattr(mod, "set_role_status", lambda role, **kw: None)
    monkeypatch.setattr(
        mod, "resolve_path",
        lambda rel, project: tmp_path / rel.replace("{project}", project))
    monkeypatch.setattr(
        mod, "write_output_atomic",
        lambda dest, content: state["written"].__setitem__(str(dest), content))
    monkeypatch.delenv(mod._OCCURRENCE_ENV, raising=False)
    mod._state, mod._root = state, tmp_path
    return mod


def _touch(root: Path, project: str, names) -> None:
    base = root / "10-项目" / "music" / project
    base.mkdir(parents=True, exist_ok=True)
    for n in names:
        (base / n).write_text("x", encoding="utf-8")


class TestDirectorResolveMode:
    def test_信号1即首次_即使5份齐全(self, director, monkeypatch):
        """原先按文件猜：跑完的项目从头重跑，第 1 步直接进汇编。"""
        _touch(director._root, "p", _AGG + ("创作 vision.md",))
        monkeypatch.setenv(director._OCCURRENCE_ENV, "1")
        assert director._resolve_mode("p") == "first_pass"

    def test_信号2且缺件判失败不退回首次(self, director, monkeypatch):
        """实测原样：母带补丁写错目录 → 第 9 步退回首次模式开始重写 vision。"""
        _touch(director._root, "p", _AGG[:4] + ("创作 vision.md",))
        monkeypatch.setenv(director._OCCURRENCE_ENV, "2")
        assert director._resolve_mode("p") == "missing_upstream"

    def test_信号2且齐全进汇编(self, director, monkeypatch):
        _touch(director._root, "p", _AGG)
        monkeypatch.setenv(director._OCCURRENCE_ENV, "2")
        assert director._resolve_mode("p") == "aggregation"

    @pytest.mark.parametrize("files, expected", [
        ((), "first_pass"),
        (_AGG, "aggregation"),
        (_AGG[:4] + ("创作 vision.md",), "missing_upstream"),
    ])
    def test_无信号按文件推断_分不清就停(self, director, files, expected):
        _touch(director._root, "p", files)
        assert director._resolve_mode("p") == expected

    def test_缺件时main返回失败且不调LLM(self, director, monkeypatch):
        _touch(director._root, "p", _AGG[:4] + ("创作 vision.md",))
        monkeypatch.setenv(director._OCCURRENCE_ENV, "2")
        monkeypatch.setattr(director, "parse_args",
                            lambda: SimpleNamespace(task="t", project="p"))
        monkeypatch.setattr(director, "resolve_project", lambda a: "p")
        monkeypatch.setattr(director, "role_is_blocked", lambda r: False)
        monkeypatch.setattr(director, "load_role", lambda r: None)
        monkeypatch.setattr(director, "call_claude",
                            lambda *a: pytest.fail("缺件时不该调 LLM"))
        assert director.main() == 1
        audit = director._state["audit"][-1]
        assert audit["error"] == "aggregation_upstream_missing"
        assert audit["missing"] == ["母带-Suno-retry补丁.md"]


class TestDirectorFirstPassOutputs:
    def test_首次模式只认vision与给制作人(self, director, monkeypatch):
        """角色基因 outputs 列了 4 份；LLM 照单全产时，多出的两份被拒、本步失败。"""
        _touch(director._root, "p/inputs", ("创作简报.md",))
        role_def = SimpleNamespace(
            inputs=["10-项目/music/{project}/inputs/创作简报.md"],
            outputs=[
                "10-项目/music/{project}/创作 vision.md",
                "10-项目/music/{project}/指令/给制作人.md",
                "10-项目/music/{project}/final-Suno-prompt.md",
                "10-项目/music/{project}/反馈分诊.md",
            ],
            rule_refs=[],
        )
        for name, val in {
            "build_system_prompt": lambda *a, **k: "sys",
            "read_input_files": lambda paths: "ctx",
            "load_rule_block": lambda refs: ("", "-"),
            "load_genre_primitive_block": lambda *a: ("", "-"),
            "load_genre_skill_block": lambda *a: ("", "-"),
        }.items():
            monkeypatch.setattr(director, name, val)
        seen_prompt: dict = {}

        def fake_llm(s, u, r):
            seen_prompt["u"] = u
            return "".join(
                f"<!-- FILE: {rel} -->\nx\n<!-- /FILE -->\n" for rel in role_def.outputs)

        monkeypatch.setattr(director, "call_claude", fake_llm)

        assert director._run_first_pass("p", "t", role_def) == 1
        assert "final-Suno-prompt.md" not in seen_prompt["u"]
        assert "反馈分诊.md" not in seen_prompt["u"]
        written = {Path(k).name for k in director._state["written"]}
        assert written == {"创作 vision.md", "给制作人.md"}
        audit = director._state["audit"][-1]
        assert audit["error"] == "undeclared_output_paths"
        assert sorted(Path(r).name for r in audit["rejected_paths"]) == [
            "final-Suno-prompt.md", "反馈分诊.md"]
