"""WAV 接收与真实格式验证：仅接受 48kHz PCM16/24 单声道或立体声，时长<=60s。"""
from __future__ import annotations

import struct
from dataclasses import dataclass

MAX_FILES = 5
MAX_DURATION_SEC = 60.0
REQUIRED_RATE = 48000
ALLOWED_CHANNELS = (1, 2)
ALLOWED_BITS = (16, 24)


class AudioValidationError(ValueError):
    pass


@dataclass
class WavInfo:
    sample_rate: int
    channels: int
    bits_per_sample: int
    frames: int
    duration: float


def parse_wav(data: bytes, name: str = "?") -> WavInfo:
    """解析 RIFF/WAVE 头并严格校验，不信任 Content-Type 或扩展名。"""
    if len(data) < 44 or data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise AudioValidationError(f"{name}: 不是有效的 RIFF/WAVE 文件")
    pos = 12
    fmt = None
    fmt_body = b""
    data_size = None
    while pos + 8 <= len(data):
        chunk_id = data[pos:pos + 4]
        chunk_size = struct.unpack_from("<I", data, pos + 4)[0]
        body = pos + 8
        if chunk_id == b"fmt ":
            if chunk_size < 16 or body + 16 > len(data):
                raise AudioValidationError(f"{name}: fmt 块损坏")
            fmt = struct.unpack_from("<HHIIHH", data, body)
            fmt_body = data[body:body + chunk_size]
        elif chunk_id == b"data":
            data_size = min(chunk_size, len(data) - body)
        pos = body + chunk_size + (chunk_size & 1)
    if fmt is None:
        raise AudioValidationError(f"{name}: 缺少 fmt 块")
    if data_size is None:
        raise AudioValidationError(f"{name}: 缺少 data 块")
    audio_format, channels, rate, _byte_rate, block_align, bits = fmt
    if audio_format == 0xFFFE and len(fmt_body) >= 40:
        # WAVE_FORMAT_EXTENSIBLE：仅接受 PCM 子格式
        audio_format = struct.unpack_from("<H", fmt_body, 24)[0]
    if audio_format != 1:
        raise AudioValidationError(f"{name}: 仅接受未压缩 PCM（format={audio_format}）")
    if rate != REQUIRED_RATE:
        raise AudioValidationError(f"{name}: 采样率须为 48kHz（实际 {rate}Hz）")
    if channels not in ALLOWED_CHANNELS:
        raise AudioValidationError(f"{name}: 仅接受单声道或立体声（实际 {channels} 声道）")
    if bits not in ALLOWED_BITS:
        raise AudioValidationError(f"{name}: 仅接受 16/24 位 PCM（实际 {bits} 位）")
    if block_align != channels * bits // 8 or block_align == 0:
        raise AudioValidationError(f"{name}: 块对齐与格式不符")
    frames = data_size // block_align
    duration = frames / rate
    if frames == 0:
        raise AudioValidationError(f"{name}: 文件不含音频帧")
    if duration > MAX_DURATION_SEC + 1e-9:
        raise AudioValidationError(f"{name}: 时长 {duration:.2f}s 超过 60s 上限")
    return WavInfo(rate, channels, bits, frames, duration)


def validate_batch(infos: list[tuple[str, WavInfo]]) -> int:
    """校验整批：数量、声道数一致。返回声道数。"""
    if not infos:
        raise AudioValidationError("至少需要 1 个 WAV 文件")
    if len(infos) > MAX_FILES:
        raise AudioValidationError(f"最多 {MAX_FILES} 个文件（收到 {len(infos)} 个）")
    channels = infos[0][1].channels
    for name, info in infos:
        if info.channels != channels:
            raise AudioValidationError(f"{name}: 声道数 {info.channels} 与同批其他文件不一致")
    return channels
