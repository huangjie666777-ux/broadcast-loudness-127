"""EBU R128 measurement using the ffmpeg ebur128 filter.

Integrated loudness (I) and true peak (dBTP) come from a single analysis
pass over the whole program. For group mode the inputs are concatenated in
submission order, so the result describes the joined program rather than an
average of per-file LUFS values.
"""

from __future__ import annotations
import asyncio
import math
import re
from dataclasses import dataclass
from pathlib import Path

from . import config
from .ffmpeg_runner import run_ffmpeg


_I_RE = re.compile(r"I:\s*(-?inf|-?\d+(?:\.\d+)?) LUFS")
_TP_RE = re.compile(r"(?:Peak|True peak):\s*(-?inf|-?\d+(?:\.\d+)?) dBFS")


@dataclass(frozen=True)
class Loudness:
    integrated_lufs: float | None
    true_peak_dbtp: float | None

    def to_dict(self) -> dict[str, float | None]:
        return {
            "integrated_lufs": _json_float(self.integrated_lufs),
            "true_peak_dbtp": _json_float(self.true_peak_dbtp),
        }


def _json_float(value: float | None) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, 3)


def _parse_number(token: str) -> float | None:
    if token == "-inf" or token == "inf":
        return None
    return float(token)


def parse_ebur128(stderr: str) -> Loudness:
    blocks = [b for b in stderr.split("Summary:") if "Integrated loudness:" in b]
    if not blocks:
        raise RuntimeError("ebur128 summary not found in ffmpeg output")
    block = blocks[-1]
    loud_token = peak_token = None
    in_loudness = in_tp = False
    for line in block.splitlines():
        stripped = line.strip()
        if stripped.startswith("Integrated loudness:"):
            in_loudness, in_tp = True, False
            continue
        if stripped.startswith("True peak:"):
            in_loudness, in_tp = False, True
            continue
        if stripped.startswith("Loudness range:"):
            in_loudness = False
            continue
        if in_loudness and loud_token is None:
            m = _I_RE.search(line)
            if m:
                loud_token = m.group(1)
        if in_tp:
            m = _TP_RE.search(line)
            if m:
                peak_token = m.group(1)
                break
    if loud_token is None or peak_token is None:
        raise RuntimeError(f"failed to parse ebur128 summary: {block[-800:]}")
    loud = _parse_number(loud_token)
    peak = _parse_number(peak_token)
    return Loudness(loud, peak)


async def measure(
    paths: list[Path], *, cancel_event: asyncio.Event | None = None
) -> Loudness:
    """Measure one file or a sequence concatenated in order."""
    if not paths:
        raise ValueError("at least one path required")
    args: list[str] = []
    for path in paths:
        args += ["-i", str(path)]
    if len(paths) == 1:
        graph = "ebur128=peak=true:framelog=verbose"
    else:
        spec = "".join(f"[{i}:a]" for i in range(len(paths)))
        graph = f"{spec}concat=n={len(paths)}:v=0:a=1,ebur128=peak=true:framelog=verbose"
    args += ["-filter_complex", graph, "-f", "null", "-"]
    stderr = await run_ffmpeg(args, cancel_event=cancel_event)
    return parse_ebur128(stderr)


async def is_silence(
    path: Path, *, cancel_event: asyncio.Event | None = None
) -> bool:
    """Detect pure digital silence via astats (no audible content heuristic)."""
    args = [
        "-i", str(path),
        "-af", "astats=measure_overall=Peak_level+RMS_level:measure_perchannel=0:reset=0",
        "-f", "null", "-",
    ]
    stderr = await run_ffmpeg(args, cancel_event=cancel_event)
    peaks = re.findall(r"Peak level dB:\s*(-?inf|-?\d+(?:\.\d+)?)", stderr)
    if not peaks:
        raise RuntimeError("astats peak not found")
    return all(tok == "-inf" for tok in peaks)


def db_to_linear(db: float) -> float:
    return 10.0 ** (db / 20.0)


def linear_to_db(linear: float) -> float:
    return 20.0 * math.log10(linear)
