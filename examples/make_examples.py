"""生成示例 WAV：纯静音、高峰值、普通电平（48kHz 立体声 PCM16）。"""
import math
import struct
import wave
from pathlib import Path

RATE = 48000
OUT = Path(__file__).parent


def write_wav(name: str, frames: list[tuple[int, int]]):
    with wave.open(str(OUT / name), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"".join(struct.pack("<hh", l, r) for l, r in frames))
    print("written", OUT / name)


def sine(freq: float, amp: float, seconds: float):
    n = int(RATE * seconds)
    return [(int(amp * 32767 * math.sin(2 * math.pi * freq * i / RATE)),
             int(amp * 32767 * math.sin(2 * math.pi * freq * i / RATE))) for i in range(n)]


write_wav("silence.wav", [(0, 0)] * RATE * 3)                 # 纯静音 3s
write_wav("hot_peak.wav", sine(1000, 0.999, 3))               # 接近 0 dBFS 高峰值
write_wav("normal.wav", sine(440, 0.1, 3))                    # 普通电平

# 低响度但高峰值：静音中一个满幅瞬态，用于演示峰值受限
transient = [(0, 0)] * RATE * 3
transient[RATE] = (32767, 32767)
write_wav("transient.wav", transient)
