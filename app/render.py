"""Apply fixed linear gain and render 24-bit PCM WAV, then verify."""

from __future__ import annotations

import asyncio
from pathlib import Path

from .ffmpeg_runner import run_ffmpeg
from .planner import GainPlan
from .validation import WavInfo, probe_wav


class RenderError(RuntimeError):
    pass


async def render_wav(
    src: WavInfo,
    dst: Path,
    plan: GainPlan,
    *,
    cancel_event: asyncio.Event | None = None,
) -> None:
    """Render with a single linear volume; sample rate/channels/frames kept."""
    gain = plan.gain_linear if plan.applied else 1.0
    args = [
        "-i", str(src.path),
        "-af", f"volume={gain:.10f}:precision=fixed",
        "-c:a", "pcm_s24le",
        "-ar", str(src.sample_rate),
        "-ac", str(src.channels),
        str(dst),
    ]
    await run_ffmpeg(args, cancel_event=cancel_event)
    info = await probe_wav(dst, dst.name)
    if info.sample_rate != src.sample_rate or info.channels != src.channels:
        raise RenderError("output format parameters changed")
    if abs(info.frames - src.frames) > 1:
        raise RenderError(f"frame count changed: {src.frames} -> {info.frames}")
