import array
import math
import wave
from pathlib import Path

import pytest

from app import audio, gain, measure, render

RATE = 48000


def make_wav(path: Path, amp: float, seconds: float = 1.0, channels: int = 2,
             sampwidth: int = 2, rate: int = RATE):
    n = int(rate * seconds)
    samples = array.array("h", (int(amp * 32767 * math.sin(2 * math.pi * 440 * i / rate))
                                for i in range(n) for _ in range(channels)))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(sampwidth)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())
    return path


def test_parse_valid(tmp_path):
    p = make_wav(tmp_path / "a.wav", 0.1)
    info = audio.parse_wav(p.read_bytes(), "a.wav")
    assert info.sample_rate == 48000 and info.channels == 2 and info.bits_per_sample == 16
    assert info.frames == 48000


def test_reject_wrong_rate(tmp_path):
    p = make_wav(tmp_path / "b.wav", 0.1, rate=44100)
    with pytest.raises(audio.AudioValidationError):
        audio.parse_wav(p.read_bytes(), "b.wav")


def test_reject_too_long(tmp_path):
    p = make_wav(tmp_path / "c.wav", 0.1, seconds=61)
    with pytest.raises(audio.AudioValidationError):
        audio.parse_wav(p.read_bytes(), "c.wav")


def test_reject_not_wav():
    with pytest.raises(audio.AudioValidationError):
        audio.parse_wav(b"not a wav at all.....................", "x.wav")


def test_batch_channel_mismatch(tmp_path):
    mono = make_wav(tmp_path / "m.wav", 0.1, channels=1)
    stereo = make_wav(tmp_path / "s.wav", 0.1, channels=2)
    infos = [("m.wav", audio.parse_wav(mono.read_bytes(), "m.wav")),
             ("s.wav", audio.parse_wav(stereo.read_bytes(), "s.wav"))]
    with pytest.raises(audio.AudioValidationError):
        audio.validate_batch(infos)


def test_measure_sine(tmp_path):
    p = make_wav(tmp_path / "tone.wav", 0.5, seconds=2.0)
    m = measure.measure([str(p)])
    assert m.integrated_lufs is not None and -20 < m.integrated_lufs < 0
    assert m.true_peak_dbtp is not None and m.true_peak_dbtp < 0


def test_measure_silence_is_null(tmp_path):
    p = make_wav(tmp_path / "sil.wav", 0.0)
    m = measure.measure([str(p)])
    # 纯静音：真实峰值非有限 -> null；响度为 EBU R128 下限 -70 LUFS
    assert m.true_peak_dbtp is None
    assert m.integrated_lufs is None or m.integrated_lufs <= -70.0


def test_gain_plan_peak_limited():
    m = measure.Measurement(integrated_lufs=-30.0, true_peak_dbtp=-2.0)
    plan = gain.plan_gain(m, target_lufs=-23.0, peak_limit_dbtp=-1.0)
    assert plan.peak_limited and plan.gain_db == pytest.approx(1.0)


def test_gain_plan_silence_no_gain():
    plan = gain.plan_gain(measure.Measurement(None, None), -23.0, -1.0)
    assert plan.silent and plan.gain_db == 0.0


def test_gain_plan_rejects_nonfinite_nonsilent():
    with pytest.raises(gain.GainPlanningError):
        gain.plan_gain(measure.Measurement(None, -5.0), -23.0, -1.0)


def test_render_and_remeasure(tmp_path):
    src = make_wav(tmp_path / "in.wav", 0.1, seconds=1.0)
    out = tmp_path / "out.wav"
    before = measure.measure([str(src)])
    plan = gain.plan_gain(before, -23.0, -1.0)
    render.render(str(src), str(out), plan.gain_db)
    info = audio.parse_wav(out.read_bytes(), "out.wav")
    assert info.bits_per_sample == 24 and info.frames == 48000 and info.channels == 2
    after = measure.measure([str(out)])
    assert abs(after.integrated_lufs - (before.integrated_lufs + plan.gain_db)) < 0.3
