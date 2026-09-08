"""Muxage ffmpeg : waterfall WebM + WAV Kiwi → MP4 H.264 / AAC + vignette."""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


def _ffmpeg() -> str:
    bin_path = shutil.which("ffmpeg")
    if not bin_path:
        raise RuntimeError("ffmpeg introuvable dans le PATH")
    return bin_path


def mux_screencast(video_webm: Path, audio_wav: Path, dest_mp4: Path) -> Path | None:
    if not video_webm.exists():
        return None
    dest_mp4.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        _ffmpeg(),
        "-y",
        "-i",
        str(video_webm),
    ]
    has_wav = audio_wav.is_file() and audio_wav.stat().st_size > 64
    if has_wav:
        cmd += ["-i", str(audio_wav), "-shortest", "-c:a", "aac", "-b:a", "96k"]
    else:
        cmd += ["-an"]
    cmd += [
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-movflags",
        "+faststart",
        str(dest_mp4),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        return dest_mp4
    except subprocess.CalledProcessError as exc:
        log.warning("ffmpeg mux : %s", exc.stderr[-400:] if exc.stderr else exc)
        return None


def thumbnail(video: Path, dest_jpg: Path, at_s: int = 45) -> Path | None:
    if not video.exists():
        return None
    cmd = [
        _ffmpeg(),
        "-y",
        "-ss",
        str(at_s),
        "-i",
        str(video),
        "-frames:v",
        "1",
        "-q:v",
        "4",
        str(dest_jpg),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        return dest_jpg
    except subprocess.CalledProcessError:
        return None
