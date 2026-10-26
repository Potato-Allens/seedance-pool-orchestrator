# RUNBOOK · 运维手册

面向「池服务 + 多出口分片」这套部署的日常操作与故障排查。

---

## 1. 组件总览

| 组件 | 入口 | 作用 |
|---|---|---|
| 池服务 | `python src/server/app.py` | HTTP API、任务队列、账号租约、成片落盘 |
| 分片 worker | `python src/client/batch_worker.py` | 从池里租号，在本机出口生成，再回写 |
| 分片守护 | `python src/client/fleet.py` | 每个出口起一个分片，心跳超时就重启 |
| 批量驱动 | `python src/client/batch_driver.py` | 向池 API 提交任务并下载成片 |
| 统计 | `src/tools/pool_stats.py` | 每 N 分钟写一行 CSV 快照 |
| 清理 | `src/tools/media_reaper.py` | 按保留期删除中间文件与成片缓存 |
| 孤儿回收 | `src/tools/release_orphans.py` | 把没有被任务引用的 `busy` 账号放回 `ready` |

## 2. 部署

### 2.1 systemd（Linux）

```ini
[Unit]
Description=seedance pool
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/seedance-pool/src/server
Environment=SPO_PORT=8788
Environment=SPO_HOST=0.0.0.0
Environment=SPO_MAX_WORKERS=32
Environment=SPO_PER_KEY_CONCURRENCY=96
Environment=SPO_AUTO_CLAIM=1
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/seedance-pool/.venv/bin/python -u app.py
Restart=always
RestartSec=3
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
```

### 2.2 Windows 分片（计划任务）

长驻进程不要用交互式窗口（会被回收）。用计划任务 + 批处理：

```bat
@echo off
set PATH=%PATH%;%LOCALAPPDATA%\Microsoft\WinGet\Links
cd /d E:\seedance-pool
python -u src\client\fleet.py
```

`schtasks /create /tn seedance-fleet /tr "cmd /c E:\seedance-pool\run_fleet.bat" /sc onstart /ru SYSTEM /rl HIGHEST`

> `WinGet\Links` 必须显式加进 PATH，否则 SYSTEM 上下文找不到 `ffmpeg` / `ffprobe`。

## 3. 重启顺序（重要）

重启池服务会让**所有 `busy` 账号被刷回 `ready`**（启动恢复逻辑），
如果本机分片还在跑，就会出现**双租**：同一个号被两边同时使用，双方都扣分、都失败。

正确顺序：

```
1. 停本机分片（停守护 + 杀分片进程）
2. 回收孤儿租约：python src/tools/release_orphans.py
3. 重启池服务：systemctl restart seedance-pool
4. 等 /healthz ok
5. 起本机分片
```

## 4. 故障对照表

| 症状 | 根因 | 处置 |
|---|---|---|
| `capflow ... over limit, force terminate` | 账号维度配额闸门 | 该号冷却 900s，换号重试（0 扣费） |
| `13002 request_timeout` | 规划阶段超时 | 换号或同号重试一次 |
| `13002 not found workspace` | workspace/space 失效 | 重新 bootstrap，仍失败则弃号 |
| `GetMaterial ... timeout` | 上游素材服务抖动 | 直接重试 |
| `asset not found` | 换号后复用了旧号的参考图 asset | 换号后必须重传接缝图 |
| 单段只有 0.8~4s | 模型提前收尾 | 同号重试一次；仍短就换号补段 |
| 成片 < 20s | 段数不足 | 提高 `SPO_MAX_SECONDS` 对应段数上限，或降门槛补拍 |
| 任务长期 `waiting_for_account` | 池里没有余额足够的号 | 导入账号 / 降低 `min_credit` |
| 429 `Too Many Requests` | 单 key 并发上限 | 调 `SPO_PER_KEY_CONCURRENCY` 或加 key |
| 磁盘涨满 | 中间段未清理 | 调短 `media_reaper` 保留期 |

## 5. 容量调优

经验值（单池）：

| 参数 | 保守 | 激进 | 备注 |
|---|---|---|---|
| `SPO_MAX_WORKERS` | 16 | 44 | 超过 ~40 后平台侧单段耗时明显上升 |
| 单出口分片线程 | 8 | 12 | 同一出口超过 ~12 线程 capflow 明显增多 |
| 账号余额门槛 | 120 | 80 | 降到 80 可多榨出每号最后 60 分 |
| 单段请求秒数 | 15 | 15 | 请求 30s 也只会出 8s 左右，不要浪费 |

判断「是平台慢还是我们慢」的方法：**暂停全部分片 15 分钟**，只留池服务。
若完成数仍接近 0，说明是平台侧问题；若明显回升，说明本机并发过高。

## 6. 数据与清理

- 成片：`OUT_DIR`（默认 `./outputs`）
- 中间段：与成片同目录，命名 `<job>.s0.mp4`、`<job>.s1.mp4`
- 池库：`src/server/data/pool.db`
- 日志：`LOG_DIR`（默认 `./logs`）

清理策略建议：

```
中间段保留 45 分钟；成片保留到下载完成后再删服务端副本。
```

## 7. 安全

- 池服务默认只监听内网；对外必须挂反向代理 + TLS。
- `SPO_ADMIN_TOKEN` 首次启动会随机生成并写入 `data/admin_token.txt`，请立刻轮换。
- 账号 cookie 等同凭据，`pool.db` 必须 600 权限，禁止提交到仓库。
- 不要把出口代理的用户名/密码写进代码，用环境变量。
