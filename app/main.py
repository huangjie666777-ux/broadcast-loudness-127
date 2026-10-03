"""HTTP 层：任务创建、查询、取消、结果 ZIP 下载。仅接受 multipart 上传，不接受宿主路径。"""
from __future__ import annotations

import io
import zipfile
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

from .audio import AudioValidationError
from .gain import GainPlanningError
from .jobs import JobManager, STATUS_COMPLETED

manager = JobManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    manager.load_from_disk()
    yield
    manager.shutdown()  # 关闭服务时回收子进程


app = FastAPI(title="广播响度整备服务", lifespan=lifespan)


@app.post("/jobs", status_code=201)
async def create_job(
    files: list[UploadFile] = File(...),
    mode: str = Form("per_file"),
    target_lufs: float = Form(-23.0),
    peak_limit_dbtp: float = Form(-1.0),
):
    if mode not in ("per_file", "group"):
        raise HTTPException(422, "mode 须为 per_file 或 group")
    payloads = []
    for f in files:
        data = await f.read()
        payloads.append((f.filename or "audio.wav", data))
    try:
        job = manager.create_job(payloads, mode, target_lufs, peak_limit_dbtp)
    except (AudioValidationError, GainPlanningError) as exc:
        raise HTTPException(422, str(exc))
    return job.to_dict()


@app.get("/jobs")
async def list_jobs():
    return [j.to_dict() for j in manager.jobs.values()]


@app.get("/jobs/{job_id}")
async def get_job(job_id: str):
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    return job.to_dict()


@app.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    if not job.cancel():
        raise HTTPException(409, f"任务已处于终态：{job.status}")
    return job.to_dict()


@app.get("/jobs/{job_id}/download")
async def download(job_id: str):
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    if job.status != STATUS_COMPLETED:
        raise HTTPException(409, f"结果不可用（状态：{job.status}）")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(job.output_dir.iterdir()):
            zf.write(path, path.name)
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="job_{job_id}.zip"'},
    )
