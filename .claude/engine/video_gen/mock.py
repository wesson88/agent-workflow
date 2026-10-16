"""mock adapter：直接造占位素材，用于测试与整链空跑（不调任何生成服务、零成本）。

每条期望 glob 的 `*` 填 `01` 生成一个文件；有 ffmpeg 时造真实可解码的测试图/测试视频/静音，
这样下游（样片审阅抽帧、配音实测时长、合成）也能真跑。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .base import GenResult, GenTask, register_adapter

VIDEO_S = 3
VOICE_S = 1.5


def _ffmpeg(args: list[str]) -> bool:
    ff = shutil.which("ffmpeg")
    if not ff:
        return False
    return subprocess.run([ff, "-v", "error", "-y", *args], capture_output=True).returncode == 0


def make_placeholder(path: Path, seconds: float | None = None, with_audio: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ext = path.suffix.lower()
    ok = False
    if ext in (".png", ".jpg", ".jpeg", ".webp"):
        ok = _ffmpeg(["-f", "lavfi", "-i", "testsrc=size=640x360:rate=1", "-frames:v", "1", str(path)])
    elif ext in (".mp4", ".mov"):
        dur = seconds or VIDEO_S
        audio = (["-f", "lavfi", "-i", f"sine=frequency=440:duration={dur}",
                  "-af", "loudnorm=I=-14:TP=-2", "-c:a", "aac", "-shortest"] if with_audio else [])
        ok = _ffmpeg(["-f", "lavfi", "-i", f"testsrc=size=640x360:rate=24:duration={dur}",
                      *audio, "-pix_fmt", "yuv420p", str(path)])
    elif ext in (".wav", ".mp3"):
        ok = _ffmpeg(["-f", "lavfi", "-i", f"anullsrc=r=44100:cl=mono", "-t", str(seconds or VOICE_S), str(path)])
    elif ext == ".srt":
        path.write_text("1\n00:00:00,000 --> 00:00:01,000\n（mock）\n", encoding="utf-8")
        ok = True
    if not ok:
        path.write_bytes(b"mock")


def run(task: GenTask, root: Path) -> GenResult:
    for pat in task.expected:
        if any(root.glob(pat)):
            continue
        # 合成成片带一段已归一到 -14 LUFS 的音轨，下游响度实测才有东西可测
        make_placeholder(root / pat.replace("*", "01"), task.params.get("seconds"),
                         with_audio=task.kind == "compose")
    return GenResult("done", (), "mock 占位素材")


register_adapter("mock", run)
