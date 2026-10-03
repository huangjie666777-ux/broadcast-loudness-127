"""Subprocess execution with global registry for cancellation/shutdown."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Sequence

from . import config


class FFmpegCancelled(Exception):
    """Raised when an ffmpeg/ffprobe process is terminated by cancellation."""


class FFmpegError(RuntimeError):
    def __init__(self, cmd: Sequence[str], code: int, stderr: str) -> None:
        super().__init__(f"command failed ({code}): {' '.join(cmd)}\n{stderr[-2000:]}")
        self.cmd = list(cmd)
        self.code = code
        self.stderr = stderr


_active: set[asyncio.subprocess.Process] = set()


def terminate_all() -> None:
    for proc in list(_active):
        with contextlib.suppress(ProcessLookupError):
            proc.kill()


async def run(
    cmd: Sequence[str],
    *,
    cancel_event: asyncio.Event | None = None,
    stdin_data: bytes | None = None,
) -> tuple[bytes, bytes]:
    """Run a command; kill the actual process when cancel_event is set."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE if stdin_data is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _active.add(proc)
    cancel_task: asyncio.Task[None] | None = None

    async def _watch_cancel() -> None:
        assert cancel_event is not None
        await cancel_event.wait()
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()

    try:
        if cancel_event is not None:
            cancel_task = asyncio.create_task(_watch_cancel())
        stdout, stderr = await proc.communicate(input=stdin_data)
        if cancel_event is not None and cancel_event.is_set() and proc.returncode != 0:
            raise FFmpegCancelled("cancelled")
        if proc.returncode != 0:
            raise FFmpegError(cmd, proc.returncode or -1, stderr.decode("utf-8", "replace"))
        return stdout, stderr
    finally:
        _active.discard(proc)
        if cancel_task is not None:
            cancel_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cancel_task


async def run_ffmpeg(
    args: Sequence[str], *, cancel_event: asyncio.Event | None = None
) -> str:
    _, stderr = await run(
        [config.FFMPEG_BIN, "-hide_banner", "-nostdin", "-y", *args],
        cancel_event=cancel_event,
    )
    return stderr.decode("utf-8", "replace")


async def run_ffprobe(args: Sequence[str]) -> tuple[bytes, bytes]:
    return await run([config.FFPROBE_BIN, "-hide_banner", *args])
