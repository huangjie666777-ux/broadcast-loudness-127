"""Fixed linear gain planning (no dynamics, no limiting)."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .measurement import Loudness, db_to_linear, linear_to_db


@dataclass(frozen=True)
class GainPlan:
    gain_db: float
    gain_linear: float
    peak_limited: bool
    applied: bool

    def to_dict(self) -> dict[str, float | bool]:
        return {
            "gain_db": round(self.gain_db, 3) if self.applied else 0.0,
            "gain_linear": round(self.gain_linear, 6) if self.applied else 1.0,
            "peak_limited": self.peak_limited,
            "applied": self.applied,
        }


def plan_gain(measured: Loudness, target_lufs: float, max_tp: float) -> GainPlan:
    """Choose min(gain-to-target, gain-allowed-by-true-peak ceiling).

    Pure silence (no finite loudness) is never gain adjusted.
    Non-silent material without a finite loudness reading is invalid.
    """
    loud = measured.integrated_lufs
    peak = measured.true_peak_dbtp
    if loud is None:
        # Caller distinguishes pure silence separately; finite peak with no
        # integrated value means unmeasurable non-silent material -> reject.
        raise ValueError("non-finite integrated loudness on non-silent material")
    if peak is None or not math.isfinite(peak):
        raise ValueError("non-finite true peak on non-silent material")

    to_target = target_lufs - loud  # may be negative (would mean turning down)
    headroom = max_tp - peak
    limited = headroom < to_target
    gain_db = min(to_target, headroom)
    return GainPlan(gain_db, db_to_linear(gain_db), limited, True)


SILENCE_PLAN = GainPlan(0.0, 1.0, False, False)


def predicted_post_tp(peak: float | None, plan: GainPlan) -> float | None:
    if peak is None:
        return None
    return peak + (linear_to_db(plan.gain_linear) if plan.applied else 0.0)
