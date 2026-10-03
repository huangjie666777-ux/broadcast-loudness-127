"""固定线性增益规划：不压缩、不限幅。"""
from __future__ import annotations

from dataclasses import dataclass

from .measure import Measurement

MIN_TARGET_LUFS = -23.0
MAX_TARGET_LUFS = -9.0
MIN_PEAK_DBTP = -6.0
MAX_PEAK_DBTP = -1.0


class GainPlanningError(ValueError):
    pass


@dataclass
class GainPlan:
    gain_db: float
    peak_limited: bool
    silent: bool


def validate_targets(target_lufs: float, peak_limit_dbtp: float) -> None:
    if not (MIN_TARGET_LUFS <= target_lufs <= MAX_TARGET_LUFS):
        raise GainPlanningError(f"目标响度须在 {MIN_TARGET_LUFS}..{MAX_TARGET_LUFS} LUFS 之间")
    if not (MIN_PEAK_DBTP <= peak_limit_dbtp <= MAX_PEAK_DBTP):
        raise GainPlanningError(f"峰值上限须在 {MIN_PEAK_DBTP}..{MAX_PEAK_DBTP} dBTP 之间")


def plan_gain(measurement: Measurement, target_lufs: float, peak_limit_dbtp: float) -> GainPlan:
    """增益取达标所需值与峰值上限允许值中较小者；纯静音不增益。"""
    loudness = measurement.integrated_lufs
    peak = measurement.true_peak_dbtp
    if peak is None:
        # 真实峰值非有限（-inf）即纯静音：不增益，保持原样
        return GainPlan(gain_db=0.0, peak_limited=False, silent=True)
    if loudness is None:
        raise GainPlanningError("素材非纯静音但无有限响度，拒绝处理")
    gain_for_target = target_lufs - loudness
    if peak is None:
        gain_for_peak = float("inf")
    else:
        gain_for_peak = peak_limit_dbtp - peak
    gain_db = min(gain_for_target, gain_for_peak)
    return GainPlan(gain_db=gain_db, peak_limited=gain_for_peak < gain_for_target, silent=False)
