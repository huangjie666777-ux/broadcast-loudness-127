from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_TMP = tempfile.mkdtemp(prefix="loudness-test-")
os.environ["LOUDNESS_DATA_DIR"] = _TMP

from app import config  # noqa: E402

config.DATA_DIR = Path(_TMP)
config.JOBS_DIR = Path(_TMP) / "jobs"
config.MAX_CONCURRENT_JOBS = 2

from scripts.make_samples import make as make_samples  # noqa: E402

make_samples()


@pytest.fixture
def samples_dir() -> Path:
    return config.BASE_DIR / "samples"
