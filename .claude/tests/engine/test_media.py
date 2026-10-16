"""
test_media.py — 多模态输入（2026-10-16 视频域样片审阅引入）

覆盖：
- engine.media：抽帧时间点、目录收集、图片/视频 → 附件（真 ffmpeg，lavfi 造素材；无 ffmpeg 跳过）、张数上限截断
- engine.llm：两条 API 轨道的 content parts 构造；provider 未声明 vision / 非 API 轨道显式报错
- engine.role_runner：真实基因 样片审阅 按组调用——本组素材作为附件发出、说明段列标签；
  素材为空时不发图且明示"未看图"；纯文本角色调用签名不变
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from engine.config import VAULT_ROOT

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="需要 ffmpeg/ffprobe")
PROJECT = "_media-unit-test"


def _make_image(path: Path, size: str = "1600x900") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=1",
         "-frames:v", "1", str(path)],
        check=True,
    )


def _make_video(path: Path, seconds: int = 3) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
         f"testsrc=size=640x360:rate=24:duration={seconds}", "-pix_fmt", "yuv420p", str(path)],
        check=True,
    )


class TestFrameTimes:
    def test_even_spacing_avoids_edges(self):
        from engine.media import frame_times

        ts = frame_times(5.0, 6)
        assert len(ts) == 6 and ts[0] == 0.1 and ts[-1] == 4.9
        assert all(b > a for a, b in zip(ts, ts[1:]))

    def test_degenerate(self):
        from engine.media import frame_times

        assert frame_times(0, 6) == [] and frame_times(5.0, 0) == []
        assert frame_times(2.0, 1) == [1.0]


@needs_ffmpeg
class TestBuildAttachments:
    def test_image_scaled_and_video_sampled(self, tmp_path):
        from engine.media import FRAMES_PER_VIDEO, build_attachments, collect_media

        _make_image(tmp_path / "assets" / "references" / "C01@1_三视图.png")
        _make_video(tmp_path / "assets" / "videos" / "G01" / "G01_vid_01.mp4")
        files = collect_media(tmp_path, ["assets/references/", "assets/videos/G01/", "assets/missing/"])
        assert [f.name for f in files] == ["C01@1_三视图.png", "G01_vid_01.mp4"]
        atts, dropped = build_attachments(tmp_path, files)
        assert not dropped and len(atts) == 1 + FRAMES_PER_VIDEO
        assert atts[0].label == "assets/references/C01@1_三视图.png"
        assert atts[1].label.startswith("assets/videos/G01/G01_vid_01.mp4 @0.1s /")
        assert all(a.data[:2] == b"\xff\xd8" for a in atts)   # JPEG
        assert len(atts[0].data) < 300_000                       # 1600 宽已缩到长边 768

    def test_cap_truncates_with_note(self, tmp_path, monkeypatch):
        import engine.media as media

        monkeypatch.setattr(media, "MAX_IMAGES", 2)
        for i in range(3):
            _make_image(tmp_path / f"r{i}.png", size="64x64")
        files = media.collect_media(tmp_path, ["r0.png", "r1.png", "r2.png"])
        atts, dropped = media.build_attachments(tmp_path, files)
        assert len(atts) == 2 and dropped == ["r2.png"]


def test_missing_ffmpeg_raises(tmp_path, monkeypatch):
    import engine.media as media

    monkeypatch.setattr(media.shutil, "which", lambda name: None)
    (tmp_path / "a.png").write_bytes(b"x")
    with pytest.raises(media.MediaUnavailable):
        media.build_attachments(tmp_path, [tmp_path / "a.png"])


class TestLlmContent:
    ATT = None

    @classmethod
    def setup_class(cls):
        from engine.media import ImageAttachment

        cls.ATT = ImageAttachment(label="assets/x.png @1.0s", data=b"\xff\xd8abc")

    def test_no_attachments_keeps_plain_string(self):
        from engine.llm import _anthropic_user_content, _openai_user_content

        assert _anthropic_user_content("hi", None) == "hi"
        assert _openai_user_content("hi", []) == "hi"

    def test_anthropic_parts(self):
        from engine.llm import _anthropic_user_content

        parts = _anthropic_user_content("审阅", [self.ATT])
        assert parts[0] == {"type": "text", "text": "审阅"}
        assert parts[1]["text"] == "[附件 1] assets/x.png @1.0s"
        assert parts[2]["type"] == "image" and parts[2]["source"]["media_type"] == "image/jpeg"

    def test_openai_parts(self):
        from engine.llm import _openai_user_content

        parts = _openai_user_content("审阅", [self.ATT])
        assert parts[2]["type"] == "image_url"
        assert parts[2]["image_url"]["url"].startswith("data:image/jpeg;base64,")

    def test_provider_without_vision_rejected(self, monkeypatch):
        import engine.llm as llm

        monkeypatch.setattr(llm, "get_provider", lambda m: {"mode": "api_only", "api": {"kind": "openai_compat"}})
        monkeypatch.setattr(llm, "_resolve_track", lambda cfg: "api")
        with pytest.raises(ValueError, match="api.vision"):
            llm.call_llm("s", "u", model="x", attachments=[self.ATT])

    def test_cli_track_rejected(self, monkeypatch):
        import engine.llm as llm

        monkeypatch.setattr(llm, "get_provider", lambda m: {"mode": "cli_only", "api": {"vision": True}})
        monkeypatch.setattr(llm, "_resolve_track", lambda cfg: "cli")
        with pytest.raises(ValueError, match="API 轨道"):
            llm.call_llm("s", "u", model="x", attachments=[self.ATT])

    def test_real_gemini_provider_declares_vision(self):
        from engine.llm import get_provider

        assert get_provider("gemini-3.1-pro-preview")["api"]["vision"] is True


class TestRunRoleVision:
    """真实基因 样片审阅 v0.2.0：vision_inputs → 附件。"""

    @pytest.fixture
    def env(self, tmp_path, monkeypatch):
        import engine.config as cfg
        import engine.role_runner as rr

        monkeypatch.setattr(cfg, "ASSET_ROOT_TEMPLATE", str(tmp_path / "{domain}" / "{project}"))
        proj = VAULT_ROOT / "10-项目" / "video" / PROJECT
        proj.mkdir(parents=True, exist_ok=True)
        (proj / "视觉设定.md").write_text("# 视觉设定\n## 身份卡\nC01@1：黑长发", encoding="utf-8")
        (proj / "分镜表.md").write_text("# 分镜表\n| S01 | G01 |", encoding="utf-8")
        captured: dict = {}
        monkeypatch.setattr(rr, "role_is_blocked", lambda name: False)
        monkeypatch.setattr(rr, "set_role_status", lambda name, **kw: None)
        monkeypatch.setattr(rr, "append_audit", lambda rec: None)

        def fake_call(system_prompt, user_prompt, role_name, **kw):
            captured["user"] = user_prompt
            captured["attachments"] = kw.get("attachments")
            return ("<!-- FILE: 10-项目/video/{project}/审阅/G01.审阅.md -->\n"
                    "# G01 审阅\n<!-- /FILE -->\n")

        monkeypatch.setattr(rr, "call_claude", fake_call)
        yield tmp_path / "video" / PROJECT, captured
        shutil.rmtree(proj, ignore_errors=True)

    @needs_ffmpeg
    def test_group_review_sends_group_media(self, env):
        from engine.media import FRAMES_PER_VIDEO
        from engine.role_runner import run_role

        root, captured = env
        _make_image(root / "assets" / "references" / "C01@1_三视图.png", size="320x180")
        _make_video(root / "assets" / "videos" / "G01" / "G01_vid_01.mp4", seconds=2)
        _make_video(root / "assets" / "videos" / "G02" / "G02_vid_01.mp4", seconds=2)  # 别组不该进
        result = run_role("样片审阅", "生成审阅", PROJECT, module_id="G01")
        assert result.ok, result.error
        atts = captured["attachments"]
        assert len(atts) == 1 + FRAMES_PER_VIDEO
        assert not any("G02" in a.label for a in atts)
        assert "## 视觉附件（7 张" in captured["user"]
        assert "[附件 2] assets/videos/G01/G01_vid_01.mp4 @0.1s" in captured["user"]

    def test_empty_media_sends_no_images_and_says_so(self, env):
        from engine.role_runner import run_role

        _, captured = env
        result = run_role("样片审阅", "生成审阅", PROJECT, module_id="G01")
        assert result.ok, result.error
        assert captured["attachments"] is None
        assert "未看图，待复审" in captured["user"]


def test_frames_scale_with_duration():
    from engine.media import FRAMES_PER_VIDEO, frames_for

    assert frames_for(5.0) == FRAMES_PER_VIDEO          # 短镜头保底
    assert frames_for(180.0) == 36                       # 3 分钟成片每 5s 一帧


class TestDiscussionDomainAdapter:
    """讨论节点域适配（2026-10-16）：真实 用户体验者 基因 + 真实 video adapter（观众代表）。"""

    @pytest.fixture
    def video_proj(self, tmp_path, monkeypatch):
        import engine.config as cfg
        import engine.graph.discussion as disc

        monkeypatch.setattr(cfg, "ASSET_ROOT_TEMPLATE", str(tmp_path / "{domain}" / "{project}"))
        disc._VISION_CACHE.clear()
        proj = VAULT_ROOT / "10-项目" / "video" / PROJECT
        proj.mkdir(parents=True, exist_ok=True)
        (proj / "分镜表.md").write_text("# 分镜表\n| S01 | 钩子镜：剑光破雾 |", encoding="utf-8")
        yield tmp_path / "video" / PROJECT
        shutil.rmtree(proj, ignore_errors=True)

    @staticmethod
    def _state(domain):
        return {
            "project": PROJECT, "task": "端到端串看", "topic": "评审成片",
            "participants": ("用户体验者",), "moderator": None, "max_rounds": 1,
            "discussion_name": "t", "domain": domain, "messages": [],
        }

    @needs_ffmpeg
    def test_video_domain_injects_adapter_docs_and_frames(self, video_proj):
        from engine.graph.discussion import _build_speaker_prompt

        _make_video(video_proj / "final" / "master.mp4", seconds=40)
        system, user, atts = _build_speaker_prompt(self._state("video"), "用户体验者")
        assert "域视角适配（video）" in system and "观众代表" in system
        assert "剑光破雾" in user                         # adapter context_docs 生效
        assert "PRD.md" not in user                       # 不再走 SE 默认三件套
        assert len(atts) == 8 and atts[0].label.startswith("final/master.mp4 @0.1s")
        assert "## 视觉附件（8 张" in user

    def test_video_domain_without_master_says_not_seen(self, video_proj):
        from engine.graph.discussion import _build_speaker_prompt

        _, user, atts = _build_speaker_prompt(self._state("video"), "用户体验者")
        assert atts == [] and "未看图，待复审" in user

    def test_no_domain_keeps_old_behavior(self, video_proj):
        from engine.graph.discussion import _build_speaker_prompt

        system, user, atts = _build_speaker_prompt(self._state(None), "用户体验者")
        assert "域视角适配" not in system and atts == []
        assert "视觉附件" not in user

    def test_speak_passes_attachments_only_when_present(self, video_proj, monkeypatch):
        import engine.graph.discussion as disc
        from engine.media import ImageAttachment

        seen: list = []
        monkeypatch.setattr(disc, "call_llm", lambda *a, **kw: (seen.append(kw.get("attachments")), "发言")[1])
        att = ImageAttachment(label="final/master.mp4 @0.1s / 40.0s", data=b"\xff\xd8")
        monkeypatch.setattr(disc, "_build_speaker_prompt", lambda st, sp: ("s", "u", [att]))
        st = {**self._state("video"), "next_speaker": "用户体验者", "current_round": 0}
        disc._node_speak(st)
        monkeypatch.setattr(disc, "_build_speaker_prompt", lambda st, sp: ("s", "u", []))
        disc._node_speak(st)
        assert seen == [[att], None]
