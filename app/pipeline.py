"""End-to-end preparation pipeline: measure -> plan -> render -> re-measure."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from .measurement import Loudness, is_silence, measure
from .planner import SILENCE_PLAN, GainPlan, plan_gain
from .render import render_wav
from .validation import WavInfo


class PipelineError(RuntimeError):
    pass


@dataclass
class FileReport:
    name: str
    channels: int
    sample_rate: int
    frames: int
    before: Loudness
    after: Loudness
    gain: GainPlan
    silent: bool

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "channels": self.channels,
            "sample_rate": self.sample_rate,
            "frames": self.frames,
            "silent": self.silent,
            "before": self.before.to_dict(),
            "after": self.after.to_dict(),
            "gain": self.gain.to_dict(),
        }


def _check_cancel(event: asyncio.Event | None) -> None:
    if event is not None and event.is_set():
        from .ffmpeg_runner import FFmpegCancelled

        raise FFmpegCancelled("cancelled")


async def run_pipeline(
    inputs: list[WavInfo],
    output_paths: list[Path],
    *,
    mode: str,
    target_lufs: float,
    max_true_peak_dbtp: float,
    cancel_event: asyncio.Event,
) -> dict:
    """Run either per-file or whole-group preparation. Returns report dict."""
    # Stage 1: per-file pre-measurement (also used as group fallback evidence).
    before_per_file: list[Loudness] = []
    silent_flags: list[bool] = []
    for info in inputs:
        _check_cancel(cancel_event)
        silent = await is_silence(info.path, cancel_event=cancel_event)
        before = await measure([info.path], cancel_event=cancel_event)
        if not silent and before.integrated_lufs is None:
            raise PipelineError(
                f"{info.original_name}: non-silent material has no finite loudness"
            )
        silent_flags.append(silent)
        before_per_file.append(before)

    if mode == "file":
        plans: list[GainPlan] = []
        for info, silent, before in zip(inputs, silent_flags, before_per_file):
            if silent:
                plans.append(SILENCE_PLAN)
            else:
                plans.append(plan_gain(before, target_lufs, max_true_peak_dbtp))
        group_before = group_after = None
    elif mode == "group":
        _check_cancel(cancel_event)
        group_before = await measure(
            [i.path for i in inputs], cancel_event=cancel_event
        )
        if group_before.integrated_lufs is None:
            if all(silent_flags):
                shared = SILENCE_PLAN
            else:
                raise PipelineError(
                    "concatenated group is non-silent but has no finite loudness"
                )
        else:
            shared = plan_gain(group_before, target_lufs, max_true_peak_dbtp)
        plans = [shared for _ in inputs]
    else:
        raise PipelineError(f"unknown mode {mode!r}")

    # Stage 2: render each file with a fixed linear gain.
    for info, out_path, plan in zip(inputs, output_paths, plans):
        _check_cancel(cancel_event)
        await render_wav(info, out_path, plan, cancel_event=cancel_event)

    # Stage 3: real post-render measurement.
    _check_cancel(cancel_event)
    after_per_file: list[Loudness] = []
    for out_path, silent in zip(output_paths, silent_flags):
        after = await measure([out_path], cancel_event=cancel_event)
        if not silent and after.integrated_lufs is None:
            raise PipelineError("post-render measurement has no finite loudness")
        after_per_file.append(after)

    if mode == "group":
        group_after = await measure(output_paths, cancel_event=cancel_event)

    reports = [
        FileReport(
            name=info.original_name,
            channels=info.channels,
            sample_rate=info.sample_rate,
            frames=info.frames,
            before=before,
            after=after,
            gain=plan,
            silent=silent,
        ).to_dict()
        for info, before, after, plan, silent in zip(
            inputs, before_per_file, after_per_file, plans, silent_flags
        )
    ]

    report: dict = {
        "mode": mode,
        "target_lufs": target_lufs,
        "max_true_peak_dbtp": max_true_peak_dbtp,
        "files": reports,
    }
    if mode == "group":
        report["group"] = {
            "before": group_before.to_dict(),
            "after": group_after.to_dict(),
            "gain": plans[0].to_dict(),
        }
    return report
