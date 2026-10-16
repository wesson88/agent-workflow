"""
test_video_workflow.py — 视频域 W2 工作流编排（2026-10-17）

覆盖随各段落地递增：契约层（in_process 角色进图 / 视频产物注册表）→ 执行层 → 生成循环 → 批判者回路 → 模板端到端。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _fresh_caches():
    """真实 vault 基因/注册表：前后都清缓存——全量跑时有别的用例把角色目录指到临时目录后
    留下空索引（单独跑本文件通过、全量失败的来源）。"""
    from engine.artifact_registry import _load_registry_cached
    from engine.role_loader import invalidate_cache

    invalidate_cache()
    _load_registry_cached.cache_clear()
    yield
    invalidate_cache()
    _load_registry_cached.cache_clear()


class TestContractLayer:
    def test_in_process_role_without_skill_dir_builds_node(self):
        """视频角色无 .claude/skills/ 目录，make_role_node 不再抛错（原来建图即失败）。"""
        from engine.graph.nodes import make_role_node

        node = make_role_node("分镜师", True, domain="video")
        assert node.__name__ == "node_分镜师"

    def test_subprocess_role_without_skill_dir_still_raises(self, monkeypatch):
        import engine.graph.nodes as nodes

        class R:
            executor = "subprocess"

        monkeypatch.setattr(nodes, "load_role", lambda n: R())
        monkeypatch.setattr(nodes, "role_to_skill_dir", lambda n: (_ for _ in ()).throw(ValueError("x")))
        with pytest.raises(ValueError):
            nodes.make_role_node("某角色", True)

    def test_video_registry_resolves_to_video_root(self):
        from engine.artifact_registry import load_config, load_registry

        reg, roots = load_registry(), load_config()
        assert reg["视频创作简报"].resolve(roots, "p") == "10-项目/video/p/inputs/创作简报.md"
        assert reg["创作简报"].domain == "music"          # 音乐域条目不受影响
        assert reg["样片审阅单"].resolve(roots, "p") == "10-项目/video/p/审阅/{n}.审阅.md"

    def test_video_genes_shadow_check_clean(self):
        from engine.artifact_registry import shadow_check_role
        from engine.role_loader import load_role

        for name in ("视频编剧", "分镜师", "美术指导", "配音导演", "prompt工程师", "样片审阅", "剪辑师"):
            r = load_role(name)
            assert shadow_check_role(r.name, r.produces, r.consumes, r.outputs, r.inputs) == [], name


# ══ 执行层 / 审批 / 批判者 / 端到端（W2 第 2-6 段）══════════════════════════
import re
import shutil
from pathlib import Path

import yaml

from engine.config import VAULT_ROOT

P = "_w2-unit-test"
R = f"10-项目/video/{P}"


@pytest.fixture
def proj(tmp_path, monkeypatch):
    """视频项目夹具：vault 项目目录 + 临时 ASSET_ROOT；结束清理（含人工闸目录 10-项目/{P}）。"""
    import engine.config as cfg

    monkeypatch.setattr(cfg, "ASSET_ROOT_TEMPLATE", str(tmp_path / "{domain}" / "{project}"))
    (VAULT_ROOT / R / "inputs").mkdir(parents=True, exist_ok=True)
    (VAULT_ROOT / R / "inputs" / "创作简报.md").write_text("# 简报\n动漫古风 30 秒", encoding="utf-8")
    yield tmp_path / "video" / P
    shutil.rmtree(VAULT_ROOT / R, ignore_errors=True)
    shutil.rmtree(VAULT_ROOT / "10-项目" / P, ignore_errors=True)


def _resolve_all(action="approve"):
    from engine.human_gate import list_gates, resolve_gate

    ids = [g.id for g in list_gates(P, status="pending")]
    for gid in ids:
        resolve_gate(project=P, gate_id=gid, action=action)
    return ids


class TestApproval:
    def test_lifecycle(self, proj):
        from engine.approval import ask_approval
        from engine.human_gate import list_gates, resolve_gate

        assert ask_approval(P, "k", "r") == "pending"
        assert ask_approval(P, "k", "r") == "pending"                 # 不重复落盘
        assert len(list_gates(P)) == 1
        gid = list_gates(P)[0].id
        resolve_gate(project=P, gate_id=gid, action="reject")
        assert ask_approval(P, "k", "r") == "rejected"                # 驳回一次性消费
        assert ask_approval(P, "k", "r") == "pending"                 # 再问
        resolve_gate(project=P, gate_id=list_gates(P, status="pending")[0].id, action="approve")
        assert ask_approval(P, "k", "r") == "approved"
        assert ask_approval(P, "k", "r") == "approved"                # 批准持久


class TestExecutionLayer:
    def test_human_ui_card_gate_then_done(self, proj, monkeypatch):
        from engine.video_gen import GenTask, run_task

        monkeypatch.delenv("VIDEO_GEN_ADAPTER", raising=False)
        t = GenTask("reference", "references", P, "video", ("assets/references/C01@1_三视图.png",), (f"{R}/视觉设定.md",))
        assert run_task(t).status == "pending"
        card = VAULT_ROOT / R / "执行" / "reference-references-a1.md"
        assert card.is_file() and "C01@1_三视图.png" in card.read_text(encoding="utf-8")
        _resolve_all()
        assert run_task(t).status == "pending"                        # approve 了但没放文件 → 补件再问
        (proj / "assets" / "references").mkdir(parents=True)
        (proj / "assets" / "references" / "C01@1_三视图.png").write_bytes(b"x")
        assert run_task(t).status == "done"

    def test_mock_creates_expected(self, proj, monkeypatch):
        from engine.video_gen import GenTask, run_task

        monkeypatch.setenv("VIDEO_GEN_ADAPTER", "mock")
        t = GenTask("keyframe", "G01", P, "video", ("assets/images/G01/S01_img_*.png",))
        assert run_task(t).status == "done"
        assert (proj / "assets/images/G01/S01_img_01.png").is_file()

    def test_bad_kind_rejected(self):
        from engine.video_gen import GenTask

        with pytest.raises(ValueError):
            GenTask("music", "x", P, "video", ("a",))


class TestCriticLoop:
    def _setup(self, monkeypatch, scores, rerun_text="v2"):
        import engine.graph.critic as critic

        out = VAULT_ROOT / R / "剧本.md"
        out.write_text("v1", encoding="utf-8")
        it = iter(scores)
        monkeypatch.setattr(critic, "call_llm", lambda *a, **kw: f"评分: {next(it)}/100\n## 问题单\n- [P1] x · y · z")

        class Res:
            returncode = 0

        def fake_invoke(inv, mode="auto"):
            assert "批判者问题单" in inv.task
            out.write_text(rerun_text, encoding="utf-8")
            return Res()

        monkeypatch.setattr(critic, "invoke_role", fake_invoke)
        return out

    def test_improves_then_passes(self, proj, monkeypatch):
        from engine.graph.critic import review_loop
        from engine.role_invoke import RoleInvocation

        out = self._setup(monkeypatch, [60, 85])
        review_loop(RoleInvocation(role="视频编剧", task="t", project=P), {"max_rounds": 2}, "video")
        assert out.read_text(encoding="utf-8") == "v2"
        assert (VAULT_ROOT / R / "评审" / "视频编剧-r1.md").is_file()

    def test_no_improvement_rolls_back(self, proj, monkeypatch):
        from engine.graph.critic import review_loop
        from engine.role_invoke import RoleInvocation

        out = self._setup(monkeypatch, [60, 55])
        review_loop(RoleInvocation(role="视频编剧", task="t", project=P), {"max_rounds": 2}, "video")
        assert out.read_text(encoding="utf-8") == "v1"


# ── 端到端：真实模板 + mock 执行层 + 假 LLM ───────────────────────────
def _fm(rel):
    from engine.obsidian_io import split_frontmatter
    return split_frontmatter((VAULT_ROOT / rel).read_text(encoding="utf-8"))[0]


def _doc(fm: dict, body: str) -> str:
    return "---\n" + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False) + "---\n\n" + body


GROUPS = [
    {"id": "G01", "shots": [{"id": "S01", "dur": 3}, {"id": "S02", "dur": 3}], "risk": "无", "lipsync": "无"},
    {"id": "G02", "shots": [{"id": "S03", "dur": 4}], "risk": "打斗", "lipsync": "无"},
]


class FakeLLM:
    """按 prompt 里的必产 FILE 清单生成符合契约的产物；G02 关键帧首审判 rework 以覆盖回改路径。"""

    def __init__(self):
        self.calls: list[str] = []
        self.reviews: dict[str, int] = {}

    def content(self, path: str, user: str) -> str:
        name = path.rsplit("/", 1)[-1]
        if name == "视觉设定.md":
            stage = "二段" if (VAULT_ROOT / R / "分镜表.md").is_file() else "一段"
            return _doc({"stage": stage, "references": [
                {"id": "C01@1", "kind": "三视图", "file": "assets/references/C01@1_三视图.png"},
                {"id": "L01", "kind": "场景", "file": "assets/references/L01_设定.png"}]}, "# 视觉设定")
        if name == "分镜表.md":
            return _doc({"groups": GROUPS}, "# 分镜表")
        if name == "配音表单.md":
            return _doc({"lines": [{"shot": "S01", "n": 1, "speaker": "C01@1", "voice_id": "v1",
                                    "offset": 0.5, "text": "来者何人"}]}, "# 配音表单")
        if name == "剪辑时间线.md":
            return _doc({"edl": [{"shot": "S01", "src": "assets/selected/G01_take1.mp4", "in": 0, "out": 3}],
                         "subtitles": True}, "# 剪辑时间线")
        if name.endswith(".审阅.md"):
            gid = name.split(".")[0]
            phase = "keyframe" if "关键帧" in user else "video"
            k = self.reviews[f"{gid}:{phase}"] = self.reviews.get(f"{gid}:{phase}", 0) + 1
            shots = next(g["shots"] for g in GROUPS if g["id"] == gid)
            if gid == "G02" and phase == "keyframe" and k == 1:
                return _doc({"phase": phase, "verdict": "rework"}, "## 回改建议\n- [肢体崩坏] 改远景")
            sel = ([{"shot": s["id"], "file": f"assets/images/{gid}/{s['id']}_img_01.png"} for s in shots]
                   if phase == "keyframe" else
                   [{"shot": s["id"], "file": f"assets/videos/{gid}/{gid}_vid_01.mp4", "in": 0, "out": s["dur"]}
                    for s in shots])
            return _doc({"phase": phase, "verdict": "pass", "selected": sel}, "# 审阅")
        return f"# {name}\n占位内容"

    def __call__(self, system_prompt, user_prompt, role_name, **kw):
        self.calls.append(role_name)
        paths = re.findall(r"<!-- FILE: (.+?) -->", user_prompt)
        return "".join(f"<!-- FILE: {p} -->\n{self.content(p, user_prompt)}\n<!-- /FILE -->\n"
                       for p in dict.fromkeys(paths))


class TestEndToEnd:
    def test_full_chain_mock_two_runs(self, proj, monkeypatch):
        import engine.graph.critic as critic
        import engine.graph.discussion as disc
        import engine.role_runner as rr
        from engine.graph.build import build_graph
        from engine.workflow import load_workflow

        monkeypatch.setenv("VIDEO_GEN_ADAPTER", "mock")
        fake = FakeLLM()
        monkeypatch.setattr(rr, "call_claude", fake)
        monkeypatch.setattr(rr, "role_is_blocked", lambda n: False)
        monkeypatch.setattr(rr, "set_role_status", lambda n, **kw: None)
        monkeypatch.setattr(rr, "append_audit", lambda rec: None)
        monkeypatch.setattr(critic, "call_llm", lambda *a, **kw: "评分: 90/100\n## 问题单\n- 无")
        spoke: list = []
        monkeypatch.setattr(disc, "call_llm", lambda *a, **kw: (spoke.append(kw.get("attachments")), "发言")[1])
        disc._VISION_CACHE.clear()

        graph = build_graph(load_workflow("视频制作"))
        init = {"project": P, "task": "做一条 30 秒古风短片", "workflow_name": "视频制作",
                "succeeded": [], "failed": [], "skipped": [], "halted": False}

        # 第一次：跑到资产确认闸停下
        s1 = graph.invoke(dict(init))
        assert s1["halted"] and not s1["failed"], s1
        assert "资产确认闸" in s1["skipped"]
        assert (proj / "assets/references/L01_设定.png").is_file()
        assert _fm(f"{R}/视觉设定.md")["stage"] == "二段"
        assert fake.calls.count("美术指导") == 2
        n_first = len(fake.calls)

        # 人工批准后重跑：上游零成本跳过，生成循环跑完，成片合成、粗剪评审
        assert len(_resolve_all()) == 1
        s2 = graph.invoke(dict(init))
        assert not s2["failed"] and not s2["halted"], s2
        assert "视频编剧" in s2["skipped"] and "美术指导" in s2["skipped"]   # 已完成角色不再调 LLM
        assert fake.calls[n_first:].count("视频编剧") == 0
        ledger = _fm(f"{R}/执行/生成台账.md")["groups"]
        assert ledger["G01"]["status"] == "done" and ledger["G02"]["status"] == "done"
        assert fake.reviews["G02:keyframe"] == 2                              # 回改一次后通过
        assert (proj / "assets/images/G02/_archive/a1/S03_img_01.png").is_file()
        assert (proj / "assets/selected/S01_key.png").is_file()
        assert (proj / "assets/selected/G01_take1.mp4").is_file()
        assert (proj / "final/master.mp4").is_file() and (proj / "final/master.srt").is_file()
        assert _fm(f"{R}/执行/配音实测时长.md")["overflow"] == []
        assert spoke and spoke[0] and len(spoke[0]) >= 6                     # 观众代表看到了成片抽帧
