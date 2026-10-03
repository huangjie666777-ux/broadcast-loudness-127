from __future__ import annotations

import io
import json
import subprocess
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.main import app, manager


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _post(client, paths, **extra):
    files = [
        ("files", (Path(p).name, Path(p).read_bytes(), "audio/wav"))
        for p in paths
    ]
    data = {"mode": "file", "target_lufs": "-23", "max_true_peak_dbtp": "-2"}
    data.update(extra)
    return client.post("/jobs", files=files, data=data)


def _wait(client, job_id, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        meta = client.get(f"/jobs/{job_id}").json()
        if meta["status"] in {"completed", "failed", "cancelled", "interrupted"}:
            return meta
        time.sleep(0.2)
    raise AssertionError("job timed out")


def test_file_mode_loudness_and_peak_limit(client, samples_dir):
    resp = _post(client, [samples_dir / "quiet_a.wav"])
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job_id"]
    meta = _wait(client, job_id)
    assert meta["status"] == "completed", meta

    report = client.get(f"/jobs/{job_id}/report").json()
    f = report["files"][0]
    assert f["before"]["integrated_lufs"] is not None
    assert abs(f["after"]["integrated_lufs"] - (-23.0)) < 0.6
    assert f["after"]["true_peak_dbtp"] <= -2.0 + 0.2
    assert f["gain"]["applied"] is True

    # high peak material must be peak limited
    resp = _post(
        client, [samples_dir / "high_peak.wav"],
        mode="file", target_lufs="-9", max_true_peak_dbtp="-6",
    )
    job_id = resp.json()["job_id"]
    meta = _wait(client, job_id)
    assert meta["status"] == "completed", meta
    report = client.get(f"/jobs/{job_id}/report").json()
    f = report["files"][0]
    assert f["gain"]["peak_limited"] is True
    assert f["after"]["true_peak_dbtp"] <= -2.0 + 0.2


def test_silence_not_gained(client, samples_dir):
    resp = _post(client, [samples_dir / "silence.wav"])
    job_id = resp.json()["job_id"]
    meta = _wait(client, job_id)
    assert meta["status"] == "completed", meta
    f = client.get(f"/jobs/{job_id}/report").json()["files"][0]
    assert f["silent"] is True
    assert f["gain"]["applied"] is False
    assert f["after"]["true_peak_dbtp"] is None


def test_group_mode_shared_gain(client, samples_dir):
    resp = _post(
        client,
        [samples_dir / "quiet_a.wav", samples_dir / "quiet_b.wav"],
        mode="group",
    )
    job_id = resp.json()["job_id"]
    meta = _wait(client, job_id)
    assert meta["status"] == "completed", meta
    report = client.get(f"/jobs/{job_id}/report").json()
    assert report["mode"] == "group"
    gains = {round(f["gain"]["gain_db"], 6) for f in report["files"]}
    assert len(gains) == 1
    g = report["group"]
    assert g["before"]["integrated_lufs"] is not None
    assert abs(g["after"]["integrated_lufs"] - (-23.0)) < 0.6
    assert g["gain"]["gain_db"] == report["files"][0]["gain"]["gain_db"]


def test_channel_mismatch_rejected(client, samples_dir, tmp_path):
    mono = tmp_path / "m.wav"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-i", str(samples_dir / "quiet_a.wav"),
         "-ac", "1", "-c:a", "pcm_s16le", str(mono)],
        check=True, capture_output=True,
    )
    resp = _post(client, [mono, samples_dir / "quiet_b.wav"], mode="group")
    assert resp.status_code == 422


def test_bad_format_rejected(tmp_path, client):
    bad = tmp_path / "b.wav"
    bad.write_bytes(b"RIFFxxxxWAVEnotaudio")
    resp = _post(client, [bad])
    assert resp.status_code == 422

    import subprocess
    mp3 = tmp_path / "a.mp3"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=1", str(mp3)],
        check=True, capture_output=True,
    )
    files = [("files", ("a.mp3", mp3.read_bytes(), "audio/mpeg"))]
    resp = client.post(
        "/jobs", files=files,
        data={"mode": "file", "target_lufs": "-23", "max_true_peak_dbtp": "-2"},
    )
    assert resp.status_code == 422


def test_target_range_validation(client, samples_dir):
    resp = _post(client, [samples_dir / "quiet_a.wav"], target_lufs="-5")
    assert resp.status_code == 422
    resp = _post(client, [samples_dir / "quiet_a.wav"], max_true_peak_dbtp="0")
    assert resp.status_code == 422


def test_download_zip(client, samples_dir):
    resp = _post(client, [samples_dir / "quiet_a.wav"])
    job_id = resp.json()["job_id"]
    _wait(client, job_id)
    r = client.get(f"/jobs/{job_id}/download")
    assert r.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = zf.namelist()
    assert "report.json" in names
    wavs = [n for n in names if n.endswith(".wav")]
    assert len(wavs) == 1
    report = json.loads(zf.read("report.json"))
    assert report["files"][0]["frames"] > 0


def test_cancel_kills_job(client, samples_dir):
    import subprocess
    # One long (60s) mono file; cancel while ffmpeg is actually measuring.
    d = Path(config.DATA_DIR) / "cancel-samples"
    d.mkdir(exist_ok=True)
    p = d / "long.wav"
    if not p.exists():
        subprocess.run(
            ["ffmpeg", "-nostdin", "-y", "-f", "lavfi",
             "-i", "sine=frequency=300:duration=60:sample_rate=48000",
             "-c:a", "pcm_s16le", str(p)],
            check=True, capture_output=True,
        )
    r = _post(client, [p], mode="file")
    target = r.json()["job_id"]
    assert client.post(f"/jobs/{target}/cancel").json()["cancel_requested"] is True
    meta = _wait(client, target, timeout=60)
    assert meta["status"] == "cancelled"
    assert client.get(f"/jobs/{target}/download").status_code == 409
    assert (config.JOBS_DIR / target / "result.zip").exists() is False


def test_restart_marks_interrupted():
    # Simulate a job directory left in "running" state by a crashed process.
    import json
    from app.tasks import JobManager

    jid = "deadbeef" * 4
    job_dir = config.JOBS_DIR / jid
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "outputs").mkdir(exist_ok=True)
    (job_dir / "outputs" / "partial.wav").write_bytes(b"partial")
    meta = {
        "job_id": jid, "status": "running", "mode": "file",
        "target_lufs": -23, "max_true_peak_dbtp": -2, "files": ["x.wav"],
    }
    (job_dir / "meta.json").write_text(json.dumps(meta))
    JobManager().startup_recover()
    recovered = json.loads((job_dir / "meta.json").read_text())
    assert recovered["status"] == "interrupted"
    assert not (job_dir / "outputs").exists()
    assert not (job_dir / "result.zip").exists()
