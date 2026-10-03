"""EBUR128 响度与真实峰值测量（调用 FFmpeg，不用 RMS/采样峰值替代）。"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

FFMPEG = "ffmpeg"

_I_RE = re.compile(r"^\s*I:\s+(-?[\d.]+|-inf)\s+LUFS", re.M)
_TP_RE = re.compile(r"^\s*Peak:\s+(-?[\d.]+|-inf)\s+dBFS", re.M)


@dataclass
class Measurement:
    integrated_lufs: float | None  # 非有限值（如纯静音 -inf）为 None
    true_peak_dbtp: float | None


def _to_float(token: str) -> float | None:
    value = float(token)
    return value if value == value and abs(value) != float("inf") else None


def parse_ebur128_summary(stderr: str) -> Measurement:
    """取输出中最后一个 Summary（整段测量结果）。"""
    i_matches = _I_RE.findall(stderr)
    tp_matches = _TP_RE.findall(stderr)
    if not i_matches or not tp_matches:
        raise RuntimeError("无法从 FFmpeg 输出解析 EBU R128 测量结果")
    return Measurement(_to_float(i_matches[-1]), _to_float(tp_matches[-1]))


def measure(paths: list[str], job=None) -> Measurement:
    """测量单个文件，或按顺序拼接多个文件后整体测量（不平均 LUFS）。"""
    if len(paths) == 1:
        cmd = [FFMPEG, "-hide_banner", "-nostats", "-i", paths[0]]
    else:
        concat = "concat:" + "|".join(paths)
        cmd = [FFMPEG, "-hide_banner", "-nostats", "-i", concat]
    cmd += ["-filter_complex", "ebur128=peak=true", "-f", "null", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if job is not None:
        job.register_process(proc)
    try:
        _, stderr = proc.communicate()
    finally:
        if job is not None:
            job.unregister_process(proc)
    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg 测量失败（退出码 {proc.returncode}）")
    return parse_ebur128_summary(stderr.decode("utf-8", "replace"))
