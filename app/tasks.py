"""Background job manager: queue, persistence, cancellation, zip results."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import config
from .ffmpeg_runner import FFmpegCancelled
from .pipeline import run_pipeline
from .validation import WavInfo, assert_consistent_channel_count, probe_wav


TERMINAL_STATES = {"completed", "failed", "cancelled", "interrupted"}


def _job_dir(job_id: str) -> Path:
    return config.JOBS_DIR / job_id


def _meta_path(job_id: str) -> Path:
    return _job_dir(job_id) / "meta.json"


def _atomic_json(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    os.replace(tmp, path)


def read_meta(job_id: str) -> dict | None:
    path = _meta_path(job_id)
    if not path.exists():
        return None
    return json.loads(path.read_text())


@dataclass
class _Live:
    task: asyncio.Task
    cancel_event: asyncio.Event


class JobManager:
    def __init__(self) -> None:
        self._semaphore = asyncio.Semaphore(config.MAX_CONCURRENT_JOBS)
        self._live: dict[str, _Live] = {}

    # ------------------------------------------------------------------ setup
    def startup_recover(self) -> None:
        """Mark unfinished jobs interrupted; never expose partial artifacts."""
        config.ensure_dirs()
        for job_dir in sorted(config.JOBS_DIR.glob("*")):
            meta_file = job_dir / "meta.json"
            if not meta_file.exists():
                continue
            try:
                meta = json.loads(meta_file.read_text())
            except json.JSONDecodeError:
                continue
            if meta.get("status") not in TERMINAL_STATES:
                self._discard_artifacts(job_dir)
                meta["status"] = "interrupted"
                meta["error"] = "service restarted before completion"
                meta["updated_at"] = time.time()
                _atomic_json(meta_file, meta)

    async def shutdown(self) -> None:
        live = list(self._live.values())
        for live_job in live:
            live_job.cancel_event.set()
        # Give killed subprocesses a moment, then mark interrupted on disk.
        await asyncio.sleep(0.2)
        for job_id, live_job in list(self._live.items()):
            live_job.task.cancel()
            meta = read_meta(job_id)
            if meta and meta.get("status") not in TERMINAL_STATES:
                meta["status"] = "interrupted"
                meta["error"] = "service shutting down"
                meta["updated_at"] = time.time()
                _atomic_json(_meta_path(job_id), meta)

    @staticmethod
    def _discard_artifacts(job_dir: Path) -> None:
        for name in ("outputs", "result.zip.tmp"):
            target = job_dir / name
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            elif target.exists():
                target.unlink(missing_ok=True)
        report = job_dir / "report.json"
        if report.exists():
            report.unlink()

    # ---------------------------------------------------------------- submit
    async def create_job(
        self,
        saved_files: list[tuple[str, Path]],
        *,
        mode: str,
        target_lufs: float,
        max_true_peak_dbtp: float,
    ) -> str:
        if not 1 <= len(saved_files) <= config.MAX_FILES:
            raise ValueError(f"1..{config.MAX_FILES} files required")
        job_id = uuid.uuid4().hex
        job_dir = _job_dir(job_id)
        (job_dir / "originals").mkdir(parents=True)

        infos: list[WavInfo] = []
        used_names: set[str] = set()
        for index, (original_name, tmp_path) in enumerate(saved_files):
            info = await probe_wav(tmp_path, original_name)
            safe = f"{index:02d}_" + info.original_name
            if safe in used_names:
                safe = f"{index:02d}_{uuid.uuid4().hex[:8]}_" + info.original_name
            used_names.add(safe)
            final_path = job_dir / "originals" / safe
            shutil.move(str(tmp_path), final_path)
            infos.append(
                WavInfo(
                    final_path, info.original_name, info.sample_rate,
                    info.channels, info.sample_bits, info.frames,
                )
            )
        assert_consistent_channel_count(infos)

        meta = {
            "job_id": job_id,
            "status": "queued",
            "mode": mode,
            "target_lufs": target_lufs,
            "max_true_peak_dbtp": max_true_peak_dbtp,
            "files": [i.original_name for i in infos],
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        _atomic_json(_meta_path(job_id), meta)

        event = asyncio.Event()
        task = asyncio.create_task(self._run(job_id, infos, mode, target_lufs, max_true_peak_dbtp, event))
        self._live[job_id] = _Live(task, event)
        task.add_done_callback(lambda _t, jid=job_id: self._live.pop(jid, None))
        return job_id

    # --------------------------------------------------------------- execute
    async def _run(
        self,
        job_id: str,
        infos: list[WavInfo],
        mode: str,
        target_lufs: float,
        max_true_peak_dbtp: float,
        event: asyncio.Event,
    ) -> None:
        job_dir = _job_dir(job_id)
        async with self._semaphore:
            meta = read_meta(job_id)
            if meta is None:
                return
            if event.is_set():
                meta["status"] = "cancelled"
                meta["updated_at"] = time.time()
                _atomic_json(_meta_path(job_id), meta)
                return
            meta["status"] = "running"
            meta["updated_at"] = time.time()
            _atomic_json(_meta_path(job_id), meta)
            outputs_dir = job_dir / "outputs"
            outputs_dir.mkdir(exist_ok=True)
            output_paths = [
                outputs_dir / info.path.name for info in infos
            ]
            try:
                report = await run_pipeline(
                    infos,
                    output_paths,
                    mode=mode,
                    target_lufs=target_lufs,
                    max_true_peak_dbtp=max_true_peak_dbtp,
                    cancel_event=event,
                )
                _atomic_json(job_dir / "report.json", report)
                self._write_zip(job_id, infos, report)
                meta = read_meta(job_id)
                assert meta is not None
                meta["status"] = "completed"
                meta["updated_at"] = time.time()
                _atomic_json(_meta_path(job_id), meta)
                shutil.rmtree(outputs_dir, ignore_errors=True)
            except FFmpegCancelled:
                self._discard_artifacts(job_dir)
                meta = read_meta(job_id)
                if meta is not None and meta["status"] not in TERMINAL_STATES:
                    meta["status"] = "cancelled"
                    meta["updated_at"] = time.time()
                    _atomic_json(_meta_path(job_id), meta)
            except asyncio.CancelledError:
                self._discard_artifacts(job_dir)
                meta = read_meta(job_id)
                if meta is not None and meta["status"] not in TERMINAL_STATES:
                    meta["status"] = "interrupted"
                    meta["error"] = "service shutting down"
                    meta["updated_at"] = time.time()
                    _atomic_json(_meta_path(job_id), meta)
                raise
            except Exception as exc:  # noqa: BLE001
                self._discard_artifacts(job_dir)
                meta = read_meta(job_id)
                if meta is not None:
                    meta["status"] = "failed"
                    meta["error"] = str(exc)[:500]
                    meta["updated_at"] = time.time()
                    _atomic_json(_meta_path(job_id), meta)

    def _write_zip(self, job_id: str, infos: list[WavInfo], report: dict) -> None:
        job_dir = _job_dir(job_id)
        tmp_zip = job_dir / "result.zip.tmp"
        with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for info in infos:
                zf.write(job_dir / "outputs" / info.path.name, arcname=info.path.name)
            zf.writestr("report.json", json.dumps(report, ensure_ascii=False, indent=2))
        os.replace(tmp_zip, job_dir / "result.zip")

    # ----------------------------------------------------------------- query
    async def cancel(self, job_id: str) -> bool:
        meta = read_meta(job_id)
        if meta is None:
            return False
        live = self._live.get(job_id)
        if live is not None and meta["status"] not in TERMINAL_STATES:
            live.cancel_event.set()
            return True
        return meta["status"] not in TERMINAL_STATES

    def result_zip(self, job_id: str) -> Path | None:
        path = _job_dir(job_id) / "result.zip"
        return path if path.exists() else None

    def report_path(self, job_id: str) -> Path | None:
        path = _job_dir(job_id) / "report.json"
        return path if path.exists() else None
