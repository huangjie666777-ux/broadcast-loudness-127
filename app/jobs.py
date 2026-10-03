"""后台任务管理：并发限制、取消（终止 FFmpeg 进程）、持久化与重启恢复。"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import uuid
from pathlib import Path

from . import audio, gain as gain_mod, measure, render

DATA_DIR = Path("data/jobs")
MAX_WORKERS = 2  # 并发任务上限

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_INTERRUPTED = "interrupted"


class CancelledError(Exception):
    pass


class Job:
    def __init__(self, job_id: str, mode: str, target_lufs: float, peak_limit_dbtp: float):
        self.id = job_id
        self.mode = mode  # "per_file" | "group"
        self.target_lufs = target_lufs
        self.peak_limit_dbtp = peak_limit_dbtp
        self.status = STATUS_QUEUED
        self.error = None
        self.report = None
        self.dir = DATA_DIR / job_id
        self.input_dir = self.dir / "input"
        self.output_dir = self.dir / "output"
        self._procs = set()
        self._lock = threading.Lock()
        self._cancelled = False

    # ---- 子进程跟踪：取消时终止实际 FFmpeg 进程 ----
    def register_process(self, proc: subprocess.Popen) -> None:
        with self._lock:
            if self._cancelled:
                proc.kill()
                return
            self._procs.add(proc)

    def unregister_process(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs.discard(proc)

    def check_cancelled(self) -> None:
        if self._cancelled:
            raise CancelledError()

    def cancel(self) -> bool:
        with self._lock:
            if self.status in (STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED, STATUS_INTERRUPTED):
                return False
            self._cancelled = True
            procs = list(self._procs)
        for proc in procs:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        if self.status == STATUS_QUEUED:
            self.status = STATUS_CANCELLED
            self._persist()
        return True

    def _persist(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        meta = {
            "id": self.id,
            "mode": self.mode,
            "target_lufs": self.target_lufs,
            "peak_limit_dbtp": self.peak_limit_dbtp,
            "status": self.status,
            "error": self.error,
        }
        (self.dir / "job.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "mode": self.mode,
            "target_lufs": self.target_lufs,
            "peak_limit_dbtp": self.peak_limit_dbtp,
            "status": self.status,
            "error": self.error,
            "report": self.report if self.status == STATUS_COMPLETED else None,
        }


class JobManager:
    def __init__(self, data_dir: Path = DATA_DIR, max_workers: int = MAX_WORKERS):
        self.data_dir = data_dir
        self.jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._sema = threading.Semaphore(max_workers)
        self._shutdown = False

    # ---- 重启恢复：完成结果可查，未完成标为中断 ----
    def load_from_disk(self) -> None:
        if not self.data_dir.exists():
            return
        for job_dir in sorted(self.data_dir.iterdir()):
            meta_path = job_dir / "job.json"
            if not meta_path.is_file():
                continue
            meta = json.loads(meta_path.read_text())
            job = Job(meta["id"], meta["mode"], meta["target_lufs"], meta["peak_limit_dbtp"])
            job.status = meta["status"]
            job.error = meta.get("error")
            if job.status in (STATUS_QUEUED, STATUS_RUNNING):
                job.status = STATUS_INTERRUPTED
                job.error = "服务重启，任务中断"
                job._persist()
            if job.status == STATUS_COMPLETED:
                report_path = job_dir / "output" / "report.json"
                if report_path.is_file():
                    job.report = json.loads(report_path.read_text())
                else:
                    job.status = STATUS_INTERRUPTED
                    job._persist()
            self.jobs[job.id] = job

    def create_job(self, files, mode: str, target_lufs: float, peak_limit_dbtp: float) -> Job:
        # 真实格式验证（不信任扩展名/Content-Type），原件只读保存，绝不修改
        infos = [(name, audio.parse_wav(data, name)) for name, data in files]
        audio.validate_batch(infos)
        gain_mod.validate_targets(target_lufs, peak_limit_dbtp)

        job = Job(uuid.uuid4().hex[:12], mode, target_lufs, peak_limit_dbtp)
        job.input_dir.mkdir(parents=True, exist_ok=True)
        job.output_dir.mkdir(parents=True, exist_ok=True)
        for name, data in files:
            safe_name = Path(name).name or "audio.wav"
            (job.input_dir / safe_name).write_bytes(data)
        job._persist()
        with self._lock:
            self.jobs[job.id] = job
        threading.Thread(target=self._run_guarded, args=(job,), daemon=True).start()
        return job

    def get(self, job_id: str):
        with self._lock:
            return self.jobs.get(job_id)

    def cancel(self, job_id: str):
        job = self.get(job_id)
        if job is not None:
            job.cancel()
        return job

    def shutdown(self) -> None:
        """关闭服务：回收所有子进程。"""
        self._shutdown = True
        with self._lock:
            jobs = list(self.jobs.values())
        for job in jobs:
            if job.status in (STATUS_QUEUED, STATUS_RUNNING):
                job.cancel()

    # ---- 执行管线：测量 -> 增益规划 -> 渲染 -> 复测 ----
    def _run_guarded(self, job: Job) -> None:
        with self._sema:
            if self._shutdown:
                return
            try:
                job.check_cancelled()
                job.status = STATUS_RUNNING
                job._persist()
                self._run(job)
                job.status = STATUS_COMPLETED
            except CancelledError as exc:
                job.status = STATUS_CANCELLED
                job.error = "任务已取消"
                shutil.rmtree(job.output_dir, ignore_errors=True)
                job.output_dir.mkdir(exist_ok=True)
            except Exception as exc:  # 失败不开放半份结果
                if job._cancelled:
                    job.status = STATUS_CANCELLED
                    job.error = "任务已取消"
                else:
                    job.status = STATUS_FAILED
                    job.error = str(exc)
                shutil.rmtree(job.output_dir, ignore_errors=True)
                job.output_dir.mkdir(exist_ok=True)
            finally:
                job._persist()

    def _run(self, job: Job) -> None:
        inputs = sorted(job.input_dir.glob("*.wav"))
        if not inputs:
            raise RuntimeError("任务缺少输入文件")
        paths = [str(p) for p in inputs]

        if job.mode == "group":
            # 整组：按顺序拼接测量，整组共用同一增益，保留相对音量
            group_meas = measure.measure(paths, job)
            plan = gain_mod.plan_gain(group_meas, job.target_lufs, job.peak_limit_dbtp)
            plans = [plan] * len(paths)
            befores = [group_meas] * len(paths)
        else:
            plans = []
            befores = []
            for path in paths:
                job.check_cancelled()
                m = measure.measure([path], job)
                befores.append(m)
                plans.append(gain_mod.plan_gain(m, job.target_lufs, job.peak_limit_dbtp))

        job.check_cancelled()
        files_report = []
        for path, plan, before in zip(paths, plans, befores):
            job.check_cancelled()
            src = Path(path)
            info = audio.parse_wav(src.read_bytes(), src.name)
            out_path = job.output_dir / src.name
            render.render(str(src), str(out_path), plan.gain_db, job)
            job.check_cancelled()
            # 实际复测，不用目标值冒充测量
            after = measure.measure([str(out_path)], job)
            out_info = audio.parse_wav(out_path.read_bytes(), src.name)
            if (out_info.frames != info.frames or out_info.channels != info.channels
                    or out_info.sample_rate != info.sample_rate):
                raise RuntimeError(f"{src.name}: 输出帧数/声道/采样率与输入不一致")
            if out_info.bits_per_sample != 24:
                raise RuntimeError(f"{src.name}: 输出不是 24 位 PCM")
            files_report.append({
                "file": src.name,
                "frames": info.frames,
                "channels": info.channels,
                "sample_rate": info.sample_rate,
                "before": {
                    "integrated_lufs": before.integrated_lufs,
                    "true_peak_dbtp": before.true_peak_dbtp,
                },
                "after": {
                    "integrated_lufs": after.integrated_lufs,
                    "true_peak_dbtp": after.true_peak_dbtp,
                },
                "gain_db": round(plan.gain_db, 4),
                "peak_limited": plan.peak_limited,
                "silent": plan.silent,
            })

        report = {
            "job_id": job.id,
            "mode": job.mode,
            "target_lufs": job.target_lufs,
            "peak_limit_dbtp": job.peak_limit_dbtp,
            "files": files_report,
        }
        if job.mode == "group":
            report["group_before"] = {
                "integrated_lufs": befores[0].integrated_lufs,
                "true_peak_dbtp": befores[0].true_peak_dbtp,
            }
            report["group_gain_db"] = round(plans[0].gain_db, 4)
        job.report = report
        (job.output_dir / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2))
