"""Generate demo WAV samples: quiet material, high-peak material, silence.

Run: .venv/bin/python -m scripts.make_samples
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from app import config

SAMPLES_DIR = config.BASE_DIR / "samples"


def _ffmpeg(args: list[str]) -> None:
    subprocess.run(
        [config.FFMPEG_BIN, "-hide_banner", "-nostdin", "-y", *args],
        check=True,
        capture_output=True,
    )


def make() -> None:
    SAMPLES_DIR.mkdir(exist_ok=True)
    # Quiet stereo speech-like noise at about -34 LUFS / -30 dBTP.
    _ffmpeg([
        "-f", "lavfi",
        "-i", "anoisesrc=color=brown:amplitude=0.08:duration=8:sample_rate=48000",
        "-af", "aformat=channel_layouts=stereo",
        "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2",
        str(SAMPLES_DIR / "quiet_a.wav"),
    ])
    _ffmpeg([
        "-f", "lavfi",
        "-i", "anoisesrc=color=pink:amplitude=0.05:duration=6:sample_rate=48000",
        "-af", "aformat=channel_layouts=stereo",
        "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2",
        str(SAMPLES_DIR / "quiet_b.wav"),
    ])
    # Quiet-but-high-peak mono tone: raising it toward the (test) target is
    # blocked by the true peak ceiling, so peak limiting kicks in.
    _ffmpeg([
        "-f", "lavfi",
        "-i", "sine=frequency=440:sample_rate=48000:duration=5",
        "-af", "volume=-3dB",
        "-c:a", "pcm_s24le", "-ar", "48000", "-ac", "1",
        str(SAMPLES_DIR / "high_peak.wav"),
    ])
    # Pure digital silence.
    _ffmpeg([
        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
        "-t", "3", "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2",
        str(SAMPLES_DIR / "silence.wav"),
    ])


if __name__ == "__main__":
    make()
    print(f"samples written to {SAMPLES_DIR}")
