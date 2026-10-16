"""
media.py — 多模态输入素材准备（2026-10-16 视频域样片审阅引入）

角色基因 `vision_inputs`（相对 ASSET_ROOT，`{n}` 按 module_id 绑定）声明的图/视频，
在这里变成可直接塞进 LLM 请求的图片附件：

- 图片：ffmpeg 缩放到长边 ≤ MAX_EDGE 后转 JPEG（统一格式与体积，png/webp 一样处理）
- 视频：ffprobe 取时长，均匀抽帧（避开首尾 0.1s），每帧标时间码；帧数 = max(FRAMES_PER_VIDEO,
  ⌈时长 / SECONDS_PER_FRAME⌉)——5s 镜头 6 帧、3 分钟成片约 36 帧，长片不至于只剩 6 张
- 上限：单次调用最多 MAX_IMAGES 张，超出截断并在标签里说明——多模态 token 远贵于文本，
  护栏放在素材侧比放在 llm 入口更可控

视频不直接上传原文件：OpenAI 兼容端点（Gemini 走这条）不收视频，抽帧是两条 API 轨道都能吃
的公约数；代价是时序问题（闪烁/抖动）看不全——这正是 R9 样片审阅"画质只预筛、存疑升人工"
的口径来源之一。

ffmpeg 缺失 → MediaUnavailable（调用方判永久失败，不静默把审阅变成"没看图的审阅"）。
"""

from __future__ import annotations

import base64
import math
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv"}

MAX_EDGE = int(os.environ.get("VISION_MAX_EDGE", "768"))
FRAMES_PER_VIDEO = int(os.environ.get("VISION_FRAMES_PER_VIDEO", "6"))
SECONDS_PER_FRAME = float(os.environ.get("VISION_SECONDS_PER_FRAME", "5"))
MAX_IMAGES = int(os.environ.get("VISION_MAX_IMAGES", "48"))


class MediaUnavailable(RuntimeError):
    """素材无法准备（ffmpeg 缺失 / 解码失败）。"""


@dataclass(frozen=True)
class ImageAttachment:
    """一张待发送图片：label 进文本让模型知道这是哪个文件的哪一帧。"""
    label: str
    data: bytes          # JPEG
    mime: str = "image/jpeg"

    def b64(self) -> str:
        return base64.b64encode(self.data).decode("ascii")


def _ffmpeg() -> tuple[str, str]:
    ff, fp = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ff or not fp:
        raise MediaUnavailable("多模态输入需要 ffmpeg/ffprobe 在 PATH 中")
    return ff, fp


def _scale_filter() -> str:
    # 长边缩到 MAX_EDGE，不放大；-2 保证偶数宽高
    return (f"scale='if(gt(iw,ih),min({MAX_EDGE},iw),-2)':"
            f"'if(gt(iw,ih),-2,min({MAX_EDGE},ih))'")


def _grab_frame(ff: str, src: Path, at_s: float | None) -> bytes:
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "f.jpg"
        cmd = [ff, "-v", "error", "-y"]
        if at_s is not None:
            cmd += ["-ss", f"{at_s:.3f}"]
        cmd += ["-i", str(src), "-frames:v", "1", "-vf", _scale_filter(), "-q:v", "4", str(out)]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0 or not out.exists():
            raise MediaUnavailable(f"ffmpeg 取帧失败：{src.name}：{proc.stderr.strip()[:200]}")
        return out.read_bytes()


def _duration_s(fp: str, src: Path) -> float:
    proc = subprocess.run(
        [fp, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(src)],
        capture_output=True, text=True,
    )
    try:
        return float(proc.stdout.strip())
    except ValueError:
        raise MediaUnavailable(f"ffprobe 取时长失败：{src.name}") from None


def frame_times(duration_s: float, n: int) -> list[float]:
    """均匀取 n 帧，避开首尾 0.1s（首帧冻结/尾帧黑场不代表内容）。"""
    if n <= 0 or duration_s <= 0:
        return []
    lo, hi = min(0.1, duration_s / 2), max(duration_s - 0.1, duration_s / 2)
    if n == 1:
        return [round((lo + hi) / 2, 3)]
    step = (hi - lo) / (n - 1)
    return [round(lo + i * step, 3) for i in range(n)]


def frames_for(duration_s: float) -> int:
    """单条视频抽帧数：短镜头保底 FRAMES_PER_VIDEO，长片按 SECONDS_PER_FRAME 一帧。"""
    return max(FRAMES_PER_VIDEO, math.ceil(duration_s / SECONDS_PER_FRAME))


def collect_media(root: Path, entries: list[str]) -> list[Path]:
    """entries 相对 root；`/` 结尾 = 目录（非递归），否则单文件。不存在的跳过。"""
    files: list[Path] = []
    for entry in entries:
        target = root / entry
        if entry.endswith("/"):
            if target.is_dir():
                files.extend(sorted(
                    p for p in target.iterdir()
                    if p.is_file() and p.suffix.lower() in IMAGE_EXTS | VIDEO_EXTS
                ))
        elif target.is_file() and target.suffix.lower() in IMAGE_EXTS | VIDEO_EXTS:
            files.append(target)
    return files


def build_attachments(root: Path, files: list[Path]) -> tuple[list[ImageAttachment], list[str]]:
    """返回 (附件, 截断说明)。附件顺序 = 文件顺序，视频帧按时间升序。"""
    if not files:
        return [], []
    ff, fp = _ffmpeg()
    out: list[ImageAttachment] = []
    dropped: list[str] = []
    for src in files:
        rel = src.relative_to(root).as_posix()
        if len(out) >= MAX_IMAGES:
            dropped.append(rel)
            continue
        if src.suffix.lower() in IMAGE_EXTS:
            out.append(ImageAttachment(label=rel, data=_grab_frame(ff, src, None)))
            continue
        dur = _duration_s(fp, src)
        for t in frame_times(dur, frames_for(dur)):
            if len(out) >= MAX_IMAGES:
                dropped.append(f"{rel} @{t:.1f}s 及之后")
                break
            out.append(ImageAttachment(
                label=f"{rel} @{t:.1f}s / {dur:.1f}s",
                data=_grab_frame(ff, src, t),
            ))
    return out, dropped
