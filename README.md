# 广播响度整备后端 (Broadcast Loudness Preparation)

基于 FastAPI 的后台服务：接收最多 5 个 WAV，按 EBU R128 测量，施加**固定线性增益**（不压缩、不限幅），导出 24-bit PCM WAV 并打包 JSON 报告。

## 环境

- Python 3.10（依赖见 `requirements.txt`，使用 `.venv/bin/python`）
- FFmpeg 7.0.2（PATH 中的 `ffmpeg`，无需 `ffprobe`）

```bash
.venv/bin/pip install -r requirements.txt   # 已安装可跳过
```

## 启动

```bash
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
# 可选环境变量：LOUDNESS_DATA_DIR、LOUDNESS_MAX_CONCURRENT（默认 2）、FFMPEG_BIN
```

## 输入规则

- 1–5 个 WAV；每个 0–60 秒；整体上传 ≤ 20 MiB。
- 仅 48 kHz、单声道或立体声、16/24-bit 整数 PCM（`pcm_s16le`/`pcm_s24le`）。
- 同批声道数必须一致。格式由 ffmpeg 实际解码校验，文件名中的宿主路径一律剥离，原始文件永不修改。

## 响度处理规则

- 参数：`mode=file|group`，`target_lufs` ∈ [-23, -9]，`max_true_peak_dbtp` ∈ [-6, -1]。
- 测量：ffmpeg `ebur128=peak=true`（integrated loudness 与 true peak/dBTP）；整组模式按提交顺序 `concat` 后测**整段**，不平均各文件 LUFS。
- 增益：`min(达到目标所需增益, 峰值上限允许增益)` 的单一固定线性增益（`volume` 滤镜，定点精度）；整组共用同一增益，保留相对音量；不使用压缩器/限幅器，不用 RMS 或采样峰值替代。
- 纯静音（astats 判定）不施加增益；非静音但无有限响度的素材直接拒绝任务。
- 输出：24-bit PCM WAV，采样率、声道数、帧数保持不变（渲染后复核），并对输出做真实复测；报告中所有数字为实测值，非有限值序列化为 `null`。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/jobs` | multipart 提交：`files`（1–5）、`mode`、`target_lufs`、`max_true_peak_dbtp`，返回 `job_id`（202） |
| GET | `/jobs/{id}` | 查询状态：`queued/running/completed/failed/cancelled/interrupted` |
| POST | `/jobs/{id}/cancel` | 取消：立即 kill 正在运行的 ffmpeg 子进程 |
| GET | `/jobs/{id}/report` | 单独获取 JSON 报告 |
| GET | `/jobs/{id}/download` | 下载 ZIP（处理后的 WAV + `report.json`） |

任务状态持久化在 `LOUDNESS_DATA_DIR/jobs/`：已完成结果重启后仍可查询/下载；服务重启时未完成任务标记为 `interrupted` 并删除半成品；关闭服务时回收全部 ffmpeg 子进程。失败或取消不产生可下载结果。并发由信号量限制（`LOUDNESS_MAX_CONCURRENT`）。

## curl 示例

```bash
# 生成示例（安静素材 / 高峰值素材 / 静音）
.venv/bin/python -m scripts.make_samples

# 整组模式提交
curl -s -X POST http://127.0.0.1:8000/jobs \
  -F files=@samples/quiet_a.wav -F files=@samples/quiet_b.wav \
  -F mode=group -F target_lufs=-23 -F max_true_peak_dbtp=-2

curl -s http://127.0.0.1:8000/jobs/<job_id>
curl -OJ http://127.0.0.1:8000/jobs/<job_id>/download
```

## 报告结构

```json
{
  "mode": "group",
  "target_lufs": -23.0,
  "max_true_peak_dbtp": -2.0,
  "files": [
    {
      "name": "00_quiet_a.wav", "channels": 2, "sample_rate": 48000,
      "frames": 384000, "silent": false,
      "before": {"integrated_lufs": -34.2, "true_peak_dbtp": -23.5},
      "after":  {"integrated_lufs": -23.1, "true_peak_dbtp": -12.4},
      "gain": {"gain_db": 11.1, "gain_linear": 3.589, "peak_limited": false, "applied": true}
    }
  ],
  "group": {
    "before": {"integrated_lufs": -33.9, "true_peak_dbtp": -23.5},
    "after":  {"integrated_lufs": -22.9, "true_peak_dbtp": -12.4},
    "gain":   {"gain_db": 10.9, "gain_linear": 3.508, "peak_limited": false, "applied": true}
  }
}
```

## 测试

```bash
.venv/bin/python -m pytest -q
```

覆盖：格式/声道/参数拒绝、逐文件与整组增益、峰值受限、纯静音不增益、ZIP 下载、运行中取消、重启中断恢复。
