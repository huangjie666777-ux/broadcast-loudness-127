# 广播响度整备后端

基于 FastAPI + FFmpeg（EBU R128）的响度整备服务：只施加固定线性增益（不压缩、不限幅），
将 WAV 整备到目标响度并受真实峰值上限约束，导出 24 位 PCM WAV 与 JSON 报告。

## 运行

```bash
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8123
```

## 输入约束

- 最多 5 个 WAV，每个 ≤ 60 秒；仅 48kHz、单声道/立体声、PCM 16/24 位（含 WAVE_FORMAT_EXTENSIBLE PCM 子格式）
- 同批文件声道数必须一致；按文件内容真实验证，不信任扩展名/Content-Type
- 仅接受 multipart 上传，不接受任何宿主路径；原件只读保存，绝不修改

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/jobs` | 创建任务：`-F files=@a.wav`（可多个）、`-F mode=per_file|group`、`-F target_lufs=-23`（-23..-9）、`-F peak_limit_dbtp=-1`（-6..-1） |
| GET | `/jobs/{id}` | 查询状态与报告 |
| GET | `/jobs` | 列出全部任务 |
| POST | `/jobs/{id}/cancel` | 取消（会 kill 实际 FFmpeg 进程） |
| GET | `/jobs/{id}/download` | 下载 ZIP（WAV + report.json），仅 completed 可用 |

任务状态：queued / running / completed / failed / cancelled / interrupted（重启时未完成任务的标记）。

## 处理规则

- 测量：FFmpeg `ebur128=peak=true` 整段积分响度 + 真实峰值；整组模式按顺序拼接后整体测量（不平均 LUFS）
- 增益：`min(目标所需增益, 峰值上限允许增益)`；整组共用同一增益保留相对音量
- 纯静音（真实峰值 -inf）不增益；非静音但无有限响度的素材拒绝
- 导出 24 位 PCM WAV，保持采样率/声道/帧数；渲染后实际复测，报告前后测量值（非有限值为 null），不用目标值冒充
- 并发任务数受限（默认 2）；失败/取消不开放半份结果；完成结果落盘，重启可查；关闭服务回收子进程

## 示例

```bash
.venv/bin/python examples/make_examples.py   # 生成 silence/hot_peak/normal/transient 示例
curl -X POST http://127.0.0.1:8123/jobs -F mode=per_file -F target_lufs=-23 -F peak_limit_dbtp=-1 \
     -F files=@examples/normal.wav -F files=@examples/transient.wav
curl http://127.0.0.1:8123/jobs/<id>
curl -o result.zip http://127.0.0.1:8123/jobs/<id>/download
```

## 自测

```bash
.venv/bin/python -m pytest tests -q
```

## 代码结构

- `app/audio.py` WAV 接收与真实格式验证
- `app/measure.py` EBUR128 响度/真实峰值测量（FFmpeg）
- `app/gain.py` 固定线性增益规划
- `app/render.py` 增益渲染与 24 位导出
- `app/jobs.py` 后台任务管理（并发、取消、持久化、重启恢复）
- `app/main.py` HTTP 接口
