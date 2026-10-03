"""Runtime configuration loaded from environment variables."""

from __future__ import annotations

import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("LOUDNESS_DATA_DIR", BASE_DIR / ".data"))

MAX_FILES = 5
MAX_DURATION_SECONDS = 60.0
ALLOWED_SAMPLE_RATE = 48000
ALLOWED_CHANNELS = (1, 2)
ALLOWED_SAMPLE_BITS = (16, 24)
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

TARGET_MIN, TARGET_MAX = -23.0, -9.0
TP_MIN, TP_MAX = -6.0, -1.0

MAX_CONCURRENT_JOBS = int(os.environ.get("LOUDNESS_MAX_CONCURRENT", "2"))
FFMPEG_BIN = os.environ.get("FFMPEG_BIN", "ffmpeg")
FFPROBE_BIN = os.environ.get("FFPROBE_BIN", "ffprobe")

JOBS_DIR = DATA_DIR / "jobs"


def ensure_dirs() -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
