"""FastAPI application: submit/status/cancel/download broadcast loudness jobs."""

from __future__ import annotations

import tempfile
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from . import config
from .ffmpeg_runner import terminate_all
from .tasks import JobManager, read_meta
from .validation import ValidationError


manager = JobManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_dirs()
    manager.startup_recover()
    try:
        yield
    finally:
        await manager.shutdown()
        terminate_all()


app = FastAPI(title="Broadcast Loudness Preparation", version="1.0.0", lifespan=lifespan)


@app.exception_handler(ValidationError)
async def validation_handler(request: Request, exc: ValidationError):
    return JSONResponse(status_code=422, content={"error": str(exc)})


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


async def _save_upload(upload: UploadFile, tmp_dir: Path) -> tuple[str, Path]:
    if not upload.filename:
        raise HTTPException(422, "missing file name")
    fd, name = tempfile.mkstemp(dir=tmp_dir, suffix=".wav")
    dst = Path(name)
    os.close(fd)
    size = 0
    with dst.open("wb") as fh:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > config.MAX_UPLOAD_BYTES:
                fh.close()
                dst.unlink(missing_ok=True)
                raise HTTPException(413, f"{upload.filename}: exceeds upload size limit")
            fh.write(chunk)
    return upload.filename, dst


@app.post("/jobs", status_code=202)
async def create_job(
    files: list[UploadFile] = File(..., description="1-5 WAV files"),
    mode: str = Form("file"),
    target_lufs: float = Form(-23.0),
    max_true_peak_dbtp: float = Form(-2.0),
):
    if mode not in {"file", "group"}:
        raise HTTPException(422, "mode must be 'file' or 'group'")
    if not (config.TARGET_MIN <= target_lufs <= config.TARGET_MAX):
        raise HTTPException(
            422, f"target_lufs must be within [{config.TARGET_MIN}, {config.TARGET_MAX}]"
        )
    if not (config.TP_MIN <= max_true_peak_dbtp <= config.TP_MAX):
        raise HTTPException(
            422,
            f"max_true_peak_dbtp must be within [{config.TP_MIN}, {config.TP_MAX}]",
        )
    if not 1 <= len(files) <= config.MAX_FILES:
        raise HTTPException(422, f"between 1 and {config.MAX_FILES} files required")

    with tempfile.TemporaryDirectory(prefix="upload-") as tmp:
        tmp_dir = Path(tmp)
        saved: list[tuple[str, Path]] = []
        try:
            for upload in files:
                saved.append(await _save_upload(upload, tmp_dir))
            job_id = await manager.create_job(
                saved,
                mode=mode,
                target_lufs=target_lufs,
                max_true_peak_dbtp=max_true_peak_dbtp,
            )
        except ValidationError as exc:
            raise HTTPException(422, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    return {"job_id": job_id, "status": "queued"}


@app.get("/jobs/{job_id}")
async def get_job(job_id: str) -> dict:
    meta = read_meta(job_id)
    if meta is None:
        raise HTTPException(404, "job not found")
    result = dict(meta)
    result["result_available"] = manager.result_zip(job_id) is not None
    return result


@app.post("/jobs/{job_id}/cancel", status_code=200)
async def cancel_job(job_id: str) -> dict:
    meta = read_meta(job_id)
    if meta is None:
        raise HTTPException(404, "job not found")
    ok = await manager.cancel(job_id)
    return {"job_id": job_id, "cancel_requested": ok}


@app.get("/jobs/{job_id}/report")
async def get_report(job_id: str):
    if read_meta(job_id) is None:
        raise HTTPException(404, "job not found")
    path = manager.report_path(job_id)
    if path is None:
        raise HTTPException(409, "report not available")
    return FileResponse(path, media_type="application/json", filename="report.json")


@app.get("/jobs/{job_id}/download")
async def download(job_id: str):
    meta = read_meta(job_id)
    if meta is None:
        raise HTTPException(404, "job not found")
    path = manager.result_zip(job_id)
    if path is None:
        raise HTTPException(409, "result not available")
    return FileResponse(
        path,
        media_type="application/zip",
        filename=f"loudness_{job_id}.zip",
    )
