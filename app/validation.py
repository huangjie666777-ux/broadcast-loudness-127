"""Strict validation of uploaded WAV files using ffmpeg only."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from . import config
from .ffmpeg_runner import FFmpegError, run_ffmpeg


class ValidationError(ValueError):
    pass


@dataclass(frozen=True)
class WavInfo:
    path: Path
    original_name: str
    sample_rate: int
    channels: int
    sample_bits: int
    frames: int

    @property
    def duration(self) -> float:
        return self.frames / self.sample_rate


_INPUT_HEADER_RE = re.compile(r"Input #\d+,\s*([^,]+),")
_AUDIO_STREAM_RE = re.compile(
    r"Stream #\S+?: Audio:\s*([\w_]+)\s*(?:\([^)]*\))?\s*,\s*(\d+) Hz,\s*([\w]+),"
)
_BITS_RE = re.compile(r",\s*(s16|s24|s32)(?:\s*\((\d+) bit\))?,")
_TIME_RE = re.compile(r"time=(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")


def _safe_original_name(name: str) -> str:
    # Never accept host paths: strip directory components from the client name.
    base = Path(name.replace("\\", "/")).name.strip() or "upload.wav"
    if not re.fullmatch(r"[^/\\\x00]{1,128}", base) or base in {".", ".."}:
        raise ValidationError("invalid file name")
    return base


async def probe_wav(path: Path, original_name: str) -> WavInfo:
    safe_name = _safe_original_name(original_name)
    try:
        stderr = await run_ffmpeg(["-i", str(path), "-f", "null", "-"])
    except FFmpegError as exc:
        raise ValidationError(f"{safe_name}: unreadable or unsupported media") from exc

    header = _INPUT_HEADER_RE.search(stderr)
    if not header or header.group(1).strip() != "wav":
        raise ValidationError(f"{safe_name}: only WAV container accepted")
    # Input stream lines live between the "Input #" block and "Stream mapping".
    lines = stderr.splitlines()
    start = next(
        (i for i, line in enumerate(lines) if line.startswith(f"Input #0,")),
        None,
    )
    end = next(
        (i for i, line in enumerate(lines) if "Stream mapping:" in line),
        len(lines),
    )
    input_streams = [
        line
        for line in lines[start:end] if start is not None
        if line.strip().startswith("Stream #0:")
    ]
    audio_lines = [line for line in input_streams if ": Audio:" in line]
    if len(input_streams) != 1 or len(audio_lines) != 1:
        raise ValidationError(f"{safe_name}: exactly one audio stream required")
    line = audio_lines[0]
    m = _AUDIO_STREAM_RE.search(line)
    if not m:
        raise ValidationError(f"{safe_name}: cannot parse audio stream")
    codec, sample_rate_s, layout = m.group(1), m.group(2), m.group(3)
    bits_m = _BITS_RE.search(line)
    if bits_m:
        bits = int(bits_m.group(2)) if bits_m.group(2) else {"s16": 16, "s24": 24, "s32": 24}[bits_m.group(1)]
    else:
        bits = 0
    if codec not in {"pcm_s16le", "pcm_s24le"} or bits not in config.ALLOWED_SAMPLE_BITS:
        raise ValidationError(f"{safe_name}: only 16/24-bit little-endian PCM accepted")
    sample_rate = int(sample_rate_s)
    if sample_rate != config.ALLOWED_SAMPLE_RATE:
        raise ValidationError(f"{safe_name}: only 48 kHz accepted")
    channels = {"mono": 1, "stereo": 2}.get(layout)
    if channels is None:
        raise ValidationError(f"{safe_name}: only mono or stereo accepted")

    # Exact frame count from a full decode; duration from the progress line.
    times = _TIME_RE.findall(stderr)
    if not times:
        raise ValidationError(f"{safe_name}: cannot determine duration")
    hh, mm, ss = times[-1]
    duration = int(hh) * 3600 + int(mm) * 60 + float(ss)
    if duration <= 0 or duration > config.MAX_DURATION_SECONDS + 0.02:
        raise ValidationError(
            f"{safe_name}: duration must be between 0 and {config.MAX_DURATION_SECONDS:.0f}s"
        )
    frames = round(duration * sample_rate)
    return WavInfo(path, safe_name, sample_rate, channels, bits, frames)


def assert_consistent_channel_count(items: list[WavInfo]) -> None:
    if len({item.channels for item in items}) != 1:
        raise ValidationError("all files in one batch must share the same channel count")
