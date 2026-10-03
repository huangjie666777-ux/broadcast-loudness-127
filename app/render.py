"""渲染：施加固定线性增益并导出 24 位 PCM WAV，保持采样率/声道/帧数。"""
from __future__ import annotations

import subprocess

from .measure import FFMPEG


def render(input_path: str, output_path: str, gain_db: float, job=None) -> None:
    cmd = [
        FFMPEG, "-hide_banner", "-nostats", "-y",
        "-i", input_path,
        "-af", f"volume={gain_db:.4f}dB",
        "-ar", "48000",
        "-c:a", "pcm_s24le",
        output_path,
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if job is not None:
        job.register_process(proc)
    try:
        _, stderr = proc.communicate()
    finally:
        if job is not None:
            job.unregister_process(proc)
    if proc.returncode != 0:
        detail = stderr.decode("utf-8", "replace")[-500:]
        raise RuntimeError(f"FFmpeg 渲染失败（退出码 {proc.returncode}）: {detail}")
