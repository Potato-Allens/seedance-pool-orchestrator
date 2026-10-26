# Pippit / Dreamina Seedance 2.5 · 全量协议逆向（2026-10 版）

> 基线：`https://www.pippit.ai`（API 走 web 域，成片 CDN 走 `*.pippitcdn-us.com` / `tos*-up-share-*.tiktokcdn-us.com`）
> 本文件由实现代码 `pippit.py` / `refs.py` / `worker.py` 反推，所有结论均来自实测，未验证项已标注。

---

## 0. 通用请求层

所有 `/api/biz/**`、`/commerce/**`、`/lv/**` 请求共用一套签名头：

```
Content-Type: application/json
Appid: 573081
X-SUBJECT-APPID: 578071
User-Agent: <Chrome UA>
sign: md5("9e2c|" + path[-7:] + "|7|5.8.0|" + ts + "||11ac")
sign-ver: 1
device-time: <ts 毫秒>
pf: 7
appvr: 5.8.0
```

要点：

- `path[-7:]` 是**路径最后 7 个字符**（含前导 `/`），query 不参与
- `ts` 与 `device-time` 必须是同一毫秒值
- `submit_run` 只需要 Cookie + JSON body，不需要 msToken / X-Bogus
- Cookie 关键项：`sessionid`、`sid_guard`、`uid_tt`、`sid_tt`、`ttwid`、`_tea_web_id`
- **出口 IP 必须是美区**，直连非美区访问 CDN 会 403

---

## 1. 账号注册与 340 分礼品额度（2026-10-18 验证）

### 1.1 邀请落地页

```
GET /i/INVITE_CODE
```
下发 Cookie：`_business_invite_code=INVITE_CODE`、`_business_invite_info`、`ttwid`、`_tea_web_id`、`_v2_spipe_web_id`。
非美区会 302 到 `?invite_code=...&is_invited=1`。

### 1.2 发验证码

```
POST /passport/web/email/send_code/
{"email":"...","password":"...","type":1,"aid":573081}
```

### 1.3 注册即登录

```
POST /passport/web/email/register_verify_login/
{"email":"...","password":"...","code":"123456","type":1,"aid":573081,
 "ect_type":1,"birthday":"1995-01-01","force_user_region":"US",
 "biz_param":{"invite_code":"INVITE_CODE"}}
```

新号带 **340 分**礼品额度（`gift_credit`），当前池内全部账号都是这条链产物。

### 1.4 领分任务

```
GET  /api/biz/v1/reward_task/list?task_type=1&version=v2
POST /api/biz/v1/reward_task/finish   {"task_id": <id>}
POST /api/biz/v1/reward_task/claim    {"task_id": <id>}
```

- 只处理 `task_status in (0,1)` 且 `task_id != 20002` 的任务
- 每个任务 finish + claim 都要调，串行间隔 ≥ 1.2s，否则风控
- **任务状态会变成 2（已领）**；`status=2` 说明当天已领完，等次日重置（未验证重置时点）

---

## 2. 会话 bootstrap

```
GET /home?lang=en        （SSR HTML）
```

从 HTML 正则提取 `workspace_id` 与 `space_id`：

```
r'\\?"workspace_id\\?"\s*:\s*\\?"?(\d+)'
r'\\?"space_id\\?"\s*:\s*\\?"?(\d+)'
```

后续 `submit_run` 的 `user_info` 必须带这两个 ID，否则报 `13002 not found workspace`。

---

## 3. 积分与权益

```
POST /commerce/v1/benefits/user_credit   {}
```

返回 `vip_credit / gift_credit / purchase_credit`（单位：分），`total` 为三者之和。

去水印开关：

```
POST /api/biz/v1/user/update_user_setting
{"user_settings":{"aigc_watermark_ban_enabled":true}}
```

---

## 4. 模型清单

```
POST /api/biz/v1/agent/get_available_model_list
{"scene":"web_unified_agent","source_model_key":""}
```

Seedance 2.5 条目（节选）：

```json
{"key":"Seedance_2.5","name":"Dreamina Seedance 2.5",
 "supported_duration_list":[{"label":"Any Duration","value":0},{"label":"30s","value":2},
                            {"label":"45-60s","value":5},{"label":">60s","value":4}],
 "parameter_config":{"dimensions":[{"key":"resolution","option_list":["1080p","720p","480p"]}]},
 "benefit_tuple":{"resource_id":"cc_biz_video_agent_seedance25"},
 "benefit_map_by_combination":{"Seedance_2.5&1080p":{...},"Seedance_2.5&480p":{...}}}
```

另有 `smart_auto_model_setting.key = "auto"`（默认档），即池子原本一直跑的是 auto 车道。
切 2.5 需要在两处同时改：`settings.selectedModelKey` 与 `client_extra.model_type`。

---

## 5. 提交生成

```
POST /api/biz/v1/agent/submit_run?babi_param=<urlencode(babi)>
```

`babi`：

```json
{"user_message_id":"<uuid>","section_id":"<uuid>","scene_lv1":"ai_agent","scene_lv2":"front_tool",
 "tool_id":"integrated_agent","sub_tool_id":"integrated_agent","edit_type":"integrated_agent",
 "enter_from":"home"}
```

Body：

```json
{
  "message": {
    "role": "user",
    "content": [
      {"type":"data","data":"{\"resolution\":\"2K\",\"video_resolution\":\"720p\",\"video_duration\":30,\"ratio\":3}",
       "sub_type":"biz/general_agent_settings"},
      {"type":"text","data":"<prompt>","sub_type":"plain/text"}
    ],
    "thread_id":"<uuid>","run_id":"<uuid>","message_id":"<uuid>","created_at":<ms>
  },
  "user_info": {"language":"en","consumer_uid":"<uid>","workspace_id":"...","space_id":"...","space_idc":"useast5"},
  "agent_name":"pippit_nest_agent",
  "entrance_from":"home",
  "run_extra":"{\"babi_param\":{...},\"client_extra\":{\"model_type\":\"Seedance_2.5\",...}}"
}
```

### 关键平台怪癖（实测）

- **`video_resolution` 发 `1080p` 会被限到约 19s**；发 `720p`（web 默认）实际仍渲染 1080x1920，但能解锁更长时长
- `ratio` 编码：`9:16=3`、`16:9=1`、`1:1=2`、`3:4=4`、`4:3=5`、`21:9=6`
- `thread_id` 复用 = 在同一 agent 会话里继续对话（未验证能否省掉规划阶段的耗时）
- `video_duration` 只是「请求值」，**平台按请求秒数扣费，但成片长度由 agent 自行决定**

返回：

```json
{"ret":"0","data":{"run":{"run_id":"...","thread_id":"...","state":1}}}
```

`state`：`1=queued / 2=running / 3=completed / 4=failed`

---

## 6. 线程轮询与资产提取

```
POST /api/biz/v1/agent/get_thread
{"scopes":["run_list.entry_list"],"thread_id":"..."}
```

事件顺序：

1. assistant → `tool_call_req: load_skill {SkillName:"direct_generate_video_v2"}`
2. 若干 tool 事件（Generate Creative Ideas / 进入成片生成）
3. tool → `tool_call_resp`（含 `task_id`、`duration_sec`、`OutputPath`）
4. assistant → `biz/x_artifact_loading_part`（含 `{"Video":{...}}`）

提取 `PippitAssetID` 时必须**先剔除 `role=="user"` 的消息**，否则会把用户上传的参考素材 ID 当成成片。

失败判定（`run failed`）：

- `{"success": false, "error": "code=13002, ... err=not found workspace"}`
- `{"success": false, "error": "code=13002, ... limit, force terminate"}`（capflow 配额）
- `state == 4`
- `state == 3` 连续 3 次仍无 asset ⇒ `run finished without asset`

---

## 7. 资产查询与无水印下载

```
POST /api/biz/v1/asset/query
{"Base":{"Client":"web"},"pippit_asset_ids":["<PippitAssetID>"]}
```

取 `Video.no_watermark_video_url.download_url`（无则退回 `origin_url`），然后 `GET` 该 URL 落盘。
CDN 对非美区出口会 403，需要走美区出口或将域名加入代理。

其它资产接口（备用）：

```
POST /api/biz/v1/agent/create_creative_project
POST /api/biz/v1/agent/list_creative_project   {"count":10,"cursor":0}
POST /api/biz/v1/agent/list_user_history       {"scopes":["base"],"user":{"consumer_uid":"..."},"page_token":"1","page_size":30}
POST /api/biz/v1/asset/query_artifact          {"thread_id":"..."}
```

---

## 8. 参考素材上传链（图/视频/音频）

```
POST /lv/v1/asset/prepare_upload_cloud             (init)
POST /lv/v1/asset/prepare_upload_cloud             (+md5/size/file_type) -> STS + UploadHosts + StoreUri + Auth + Sess
POST /lv/v1/asset/prepare_upload_cloud             (flushToken; 用最后一次 STS 签名)
POST https://{UploadHosts[0]}/upload/v1/{StoreUri} Authorization + octet-stream
POST /lv/v1/asset/create_cloud_asset               -> data.cloud_asset.asset_id = EntryID
POST /api/biz/v3/asset/create_v2                   {asset_source_type:3, asset_source_id:EntryID, asset_type:1|2|3}
```

`asset_type`：`1=image / 2=video / 3=audio`。

`submit_run` 里挂素材的 part 形状：

```json
{"type":"data","sub_type":"biz/x_data_upload_image",
 "data":"{\"image\":{\"name\",\"url\",\"metadata\":{\"md5\",\"name\",\"width\",\"height\",\"size\"},\"asset_id\":\"<EntryID>\"}}"}
{"type":"data","sub_type":"biz/x_data_upload_video",
 "data":"{\"video\":{\"name\",\"url\",\"metadata\":{\"md5\",\"name\",\"width\",\"height\",\"duration_ms\"},\"asset_id\":\"<EntryID>\"}}"}
{"type":"data","sub_type":"biz/x_data_upload_audio",
 "data":"{\"audio\":{\"name\",\"url\",\"metadata\":{\"md5\",\"format\",\"duration\",\"size\"},\"asset_id\":\"<EntryID>\",\"security_check_scene\":[]}}"}
```

> `asset_id` 必须是 **EntryID**（create_cloud_asset 返回），不是 PippitAssetID；不要额外包 `pippitAssetId`。
> 上传域名 `tos*-up-share-useast*.tiktokcdn-us.com` 对部分出口会 `ConnectTimeout`，需要换出口或借用服务器代传。

---

## 9. 计费模型（2026-10-26 实测）

| 事实 | 证据 |
|---|---|
| 扣费 = **实际产出秒数 × 4 分/秒** | 请求 15s 出 15.000s → 扣 60；请求 15s 出 3.785s → 扣 15（R143 两个独立样本，互为对照）|
| 旧口径（已作废） | 「请求秒数×4」—— 只在此前「请求≈产出」的样本里碰巧成立 |
| 推论 | 短段不贵（0.95s 只扣 4 分），**贵的是为补足 30s 而多提交的那一次规划时间**；失败（capflow/13002）0 扣费 |
| 与实际成片长度无关 | 同上 |
| **失败的 run 不扣费** | 大量 `capflow/timeout` 失败后账号积分不变 |
| 2×15s 成功 = 120 分 | 340→220，两个不同账号复现 |
| 一条严格 30s 成片实际 115~175 分 | 91 条样本均值 115.4，含短段补拍时偏高 |

推论：**「请求值 = 当前缺口」是唯一不浪费的写法**；失败可以放心重试。

---

## 10. 多段拼接与严格时长闭环

平台单段长度不可控：同一账号请求 15s，实测返回过 1.05 / 0.80 / 5.60 / 8.43 / 15.00 / 20.02 / 25.01 / 28.77s。

因此采用「按缺口循环」：

```
need = 目标秒数
while 已生成 < need - 容差 and 段数 < 6:
    seg_len = round(min(15, need - 已生成))
    生成该段（第 2 段起把上一段最后一帧作为参考图上传）
    d = probe(段)
    take = min(d, need - 已生成)
    if d > take: 裁到 take（先 -c copy，误差 >0.6s 再 libx264 veryfast crf20 重编）
    已生成 += take
concat(所有段) -> 成片
```

效果（改造后连续 18 条）：**30.046 ~ 30.079s**，改造前同批是 18.30 / 21.76 / 26.43 / 34.24 / 39.20s。
段间用末帧参考图后，抽帧对比 14.3s vs 15.7s：人物姿态、道具位置、光照完全连续，无跳切。

---

## 11. 并发天花板（实测，重要）

| 总并发 | 现象 | 吞吐 |
|---|---|---|
| 6~8 | 单段 280~380s，失败率低 | ~20 条/小时/IP |
| 10 | 单段 350~600s | ~24 条/小时 |
| 16 | 首试失败率上升 | ~24 条/小时 |
| 20 | 单段 600~1290s | 8~48 条/小时（波动大）|
| 第 17 条（在 16 之上再加 1） | 连续 3 次 `generate dit material failed over limit` | 直接失败 |

结论：

- 存在**共享的生成配额闸门**（capflow / `cc_biz_pippit_broll_process` 节点）
- 换出口 IP **确实能拿到新的配额**（服务端被限流时，另一台机器的 IP 仍能 552s 出一条）
- 但单 IP 的甜点区约 **6~10 并发**，超过只是把时延从 5 分钟拉到 20 分钟

---

## 12. 实测错误码表

| error_code / 关键字 | 含义 | 处理 |
|---|---|---|
| `13002 ... not found workspace` | workspace/space_id 缺失或失效 | 重新 bootstrap 或换号 |
| `13002 ... limit, force terminate` | capflow 生成配额超限 | 降并发 / 换 IP / 换号重试 |
| `request timeout connect_timeout=50ms(from cp)` | 平台内部物料服务超时 | 直接重试（不扣费）|
| `GetMaterial prod request timeout` | 同上 | 直接重试（不扣费）|
| `run finished without asset` (state=3) | agent 结束但没产出 | 换号重试 |
| `state == 4` | run 失败 | 换号重试 |
| `submit ret != 0` | 提交被拒 | 看 errmsg，通常是风控或参数错 |

---

## 13. 双出口量产架构（当前在跑）

```
账号农场（注册链 1.x）──▶ pool.db（uid/cookies/workspace/space/credit）
                             │
              ┌──────────────┴───────────────┐
   SSH lease  │                              │ HTTP /v1/videos
              ▼                              ▼
   本机 worker（美区出口 A）          池服务器 worker（出口 B）
   单段 280~380s                    单段 600~1300s（被限流）
              └──────────────┬───────────────┘
                             ▼
                  严格 30s / 1080x1920 / 无水印
```

- 租约：`status='ready' and credit>=cost` 原子置 `busy`，完成后回写 credit 并置回 ready
- 崩溃恢复：单实例启动时清空锁文件、把所有 `running` 立即转 `queued`
- 逐单真实成本：`cost = Σ(换号前 credit 差) + (最后一次 credit 差)`

---

## 14. 未验证 / 待办

- `45-60s`、`>60s` 档位能否真正出片（两次测试都被 capflow 打断，不是被时长拒绝）
- ~~`thread_id` 复用能否省掉 agent 规划阶段~~ **已证否（R144）**：往同一 thread 再提交一次，`get_thread` 会立刻返回**上一轮的 asset**，客户端 3.8s 就「完成」且扣 0 分 —— 拿到的是旧视频。生产环境**禁止复用 thread_id**，每个 run 必须新 thread。
- 每日领分任务的重置时点
- 480p 档位是否更快/更省（`benefit_map_by_combination` 里有 480p 权益）
- 单段上限：R148 观测到一次 **20.23s** 单段成功 ⇒ 15s 不是硬上限，20s 档位值得复测


---

## 15. 失败分类与节流模型（R139-R148 实测）

### 15.1 两类失败，语义完全不同

| 症状串 | 归类 | 是否扣分 | 典型耗时 | 处理 |
|---|---|---|---|---|
| `capflow status: 3 ... generate dit material failed over limit, force terminate` | 上游产能闸门 | **0** | 90~200s | 换号重试（不是换 IP 就够）|
| `request_timeout=60000ms ... real_time=59999187us` + `error_code 13002` | 规划请求 60s 超时 | **0** | 60~95s | 换号或同号重试，通常第二次能过 |
| 出片但 `seg too short`（0.0~4s）| 模型提前收尾 | 按实际秒数扣（4 分/秒）| 正常耗时 | 同号重试一次；仍短则收下并补段 |
| `seam ref failed` + `tos19-up-share-useast5.tiktokcdn-us.com ConnectionError` | 参考图上传 CDN 不可达 | 0 | +90~190s | 跳过参考图继续（会损失接缝一致性）|

### 15.2 节流至少是「账号维度」，不是纯 IP

- R142：同一账号 `...4302` 在 13:15:20 与 13:17:37 连续两次被打回，而同 IP 其他线程同期正常出 15.00s。
- R143：三个不同出口（东京 <EGRESS_IP> / 洛杉矶 <EGRESS_IP> / 本机直连）在 6 线程时都出现 capflow。
- 结论：**capflow 先看账号是否被这个 upstream 拉黑/限速，其次才是 IP 并发**。
- 因此失败后必须 `cooldown_until = now + 900`，否则 lease 按 `credit DESC` 排序会把同一个号立刻租回来，白烧 3~5 分钟。

### 15.3 实测速率（R148，13:30-13:50）

| 通道 | 并发 | 段/小时 | 条/小时 | capflow/15min |
|---|---|---|---|---|
| 池服务器 127.0.0.1 | 20 worker | ~168 | ~40 | 6 |
| 本机直连 | 6 | ~24 | ~9 | 偶发 |
| 本机 7897（东京） | 6 | ~18 | ~7 | 偶发 |
| 本机 7899（洛杉矶） | 6 | ~22 | ~8 | 偶发 |
| **合计** | 38 | ~230 | **~64~84** | — |

- 段级耗时：**250~450s**（15s 成片）；20 并发时均值 466s。
- 扣分实测：78,626（13:00）→ 70,869（13:49），49 分钟烧 7,757 分 ≈ 65 条 ≈ **80 条/h**。

### 15.4 出口清单（示例，已脱敏）

| 出口 | 公网 IP | 实测结果 |
|---|---|---|
| 直连 | 本机公网 | 可用 |
| 代理 A（7897） | `<EGRESS_IP>` | 可用，15.000s / 237s / 60 分 |
| 代理 B（7899） | `<EGRESS_IP>` | 可用 |
| 代理 C（7900） | `<EGRESS_IP>` | 可用（与 B 同 /24）|
| 代理 D（7896/7898） | — | TLS 握手失败 / 出口不可用 |`r`n`r`n> 端口、IP、ASN 均已脱敏，实际值通过 `.env` / `PP_PORTS` 自行配置。

### 15.5 守护方式（重要运维结论）

- 交互式 shell 里 `Start-Process` 起的长驻进程会在 ~8 分钟后被外部回收（无崩溃日志、无 traceback）。
- 必须用**系统级守护**（Windows 计划任务 / systemd unit）拉起 shard 进程，交互式 shell 里起的进程不可靠。
- bat 里要显式补 `%LOCALAPPDATA%\Microsoft\WinGet\Links` 到 PATH，否则 SYSTEM 上下文找不到 ffmpeg/ffprobe。

---

## 16. 积分「搁浅」与多点解锁（R161-R165，2026-10-26 实测）

### 16.1 结构性问题
每个农场号固定 340 分；一条 30s 成片约 120 分 ⇒ 跑 2 条后余额 100 分。

旧逻辑三处都要求 `credit >= 120`：
- 本地 `batch_worker.py`：`lease(120)` + `if ctx.credit < 120: release`
- 服务端 `worker.py`：`db.pick_account(cost + 60)`（30s 任务 = 120+60 = **180**）

⇒ 余额 80~179 的号**永远租不到**。16:16 实测 540 号 / 52,819 分：

| 区间 | 号数 | 状态 |
|---|---|---|
| 340+ | 13 | 可用 |
| 200-339 | 18 | 可用 |
| 120-199 | 57 | 服务端被 180 门槛挡住 |
| 80-119 | 280 | 本地/服务端都租不到 |
| 40-79 | 107 | 同上 |
| 0-39 | 65 | 同上 |

可动用只有 ~17k / 52.8k ≈ **32%**，其余 2/3 是死分。

### 16.2 已上线修复
1. `batch_worker.py` 新增 `SPO_MIN_CREDIT`（默认 **80**），`switch()/handoff()/worker()` 统一。
2. 新增 `Ctx.handoff()`：余额 <60 时**不算失败**地释放当前号并租新号继续拍下一段。
   原来 `gen()` 里是 `break` → 15s 单段拼不到 20s 被删除 ⇒ 那 60 分纯亏。
3. 段内换号后**必须重传接缝参考图**：parts 里的 asset id 属于旧号 workspace，
   否则报 `convert path ... to asset ID failed: code=1 message=[asset not found]`
   （p7897 分片连续失败的真正原因）。
4. `batch_worker.py` 新增 `--order asc|desc`：`asc` = 先吃低分号（sweeper 分片）。
5. R164 请求秒数按余额封顶：`seg_len <= (credit-5)/5.4`（产出最多超请求 ~33%）。
6. 服务端 `worker.py`：`pick_account(cost+60)` → `pick_account(cost)` → 再放宽到
   `cost<100 ? cost : 80`；多段循环里 `credit<60` 由 `break` 改为 `_switch_account()` 续拍；
   服务端同样加余额封顶。

### 16.3 出口实测（推翻旧的三分片假设）
| 代理端口 | 出口公网 IP | 备注 |
|---|---|---|
| 0（直连） | `<EGRESS_IP>` | 出口 A |
| 7897 | `<EGRESS_IP>` | **与直连同一出口** |
| 7899 | `<EGRESS_IP>` | 出口 B |
| 7900 | `<EGRESS_IP>` | 出口 B（同段） |
| 7901 | `<EGRESS_IP>` | 出口 C（登录取号专用，勿跑量） |

=> 原「3 分片 ×10 线程」实际是 **20 线程挤在 1 个出口 IP** 上，capflow 必然高。
建议布局：按出口 IP 拆分分片（每出口 8~10 线程），余额高低分别用 `desc` / `asc` 顺序消费。

### 16.4 每日领分任务：证否
老号（credit=100）`reward_task/list?task_type=1&version=v2` 只剩 4 条已领完任务
（authorizepush / linkemail / downloadorexport / downloadorexportanimage），
task_type 0/2 返回 0 条，3~6 返回 ret=2 ⇒ **没有可重复领取的每日额度**，复充只能靠新号。

### 16.5 模型成本矩阵实测（R162，单段请求 15s，wire 720p，单号 100 分）
| 模型 | wire | 产出 | 扣分 | 分/秒 | 输出 | 备注 |
|---|---|---|---|---|---|---|
| Seedance_2.5 | 720p | 15.000s | 60 | **4.00** | 1080x1920 | 当前主力，最低价 |
| Seedance_2.0_fast | 720p | 15.000s | 60 | **4.00** | 1080x1920 | 同价 |
| seedance_1.0 | 720p | 15.000s | 60 | **4.00** | 1080x1920 | 同价 |
| Seedance_2.0_mini_lite | 720p | 7.767s | 31 | 3.99 | 1080x1920 | 描述里 "7 credits/s" 与实测不符 |
| Seedance_2.0 | 720p | 7.600s | 56 | 7.37 | 1080x1920 | 更贵，别用 |
| Seedance_2.0_mini | 720p | 6.900s | 70 | 10.14 | 1080x1920 | 最贵，别用 |
| Seedance_2.5 | 480p | 11.967s | 74 | 6.18 | 1080x1920 | 480p 不便宜，输出仍是 1080x1920 |

结论：**计费只有 4 分/秒 这一个地板价**，2.5 / 2.0_fast / 1.0 / mini_lite 全部同价；
2.5 画质最好 ⇒ **成本套利不存在，继续跑 2.5@720p 就是最优解**。
2.0（非 fast）与 mini 明显更贵（7.4 / 10.1 分/秒），任何「换模型省钱」的假设都证否。

### 16.6 低分 sweeper 实测（17:00，已验证闭环）
- `--order asc` 分片（LA .100，8 线程）连续命中 80~85 分的号：
  `seg1 ok 11.33s (453s)` → `handoff account, credit=85` → `handoff ok, continuing seg2` → `seam ref ok seg1`。
- 证明「80 分号只出一个 11~14s 首段，再交给富裕号续拍」这条链能闭环，搁浅积分第一次被变成画面。
- 接缝修复同样验证：`switched account, credit=290` 之后紧接 `re-uploaded seam ref for new account`。

### 16.7 待验证
- 40-79 的 107 个号是否值得把 `SPO_MIN_CREDIT` 降到 60（配 `_cap` 出 6~10s 段）。
- 平台侧单段耗时从 16:00 前的 400~700s 涨到 17:00 的 700s+（服务端单任务均 1674s），
  是否为全局负载/账号质量导致，需继续观察后再决定要不要降并发。

---

## 17. 画幅代码表实测（R166，2026-10-26 17:20）

用 `RATIO_CODE["code_N"] = N` 直接注入原始 ratio 值，跑 5s 请求后 ffprobe 实际输出帧尺寸：

| 代码 | 实测输出 | 真实画幅 | 旧代码表认为 |
|---|---|---|---|
| 3 | 1080x1920 | 9:16 | 9:16 ✅ |
| 2 | 1920x1080 | **16:9** | 1:1 ❌ |
| 4 | 1440x1080 | **4:3** | 3:4 ❌ |
| 5 | 1080x1440 | **3:4** | 4:3 ❌ |
| 6 | 1080x1080 | **1:1** | 21:9 ❌ |
| 1 | — | 无效（13002） | 16:9 ❌ |
| 13 | — | 无效（两次 13002 / 超时） | 未用 |

正确映射（已写入 pippit.py 与 pool 的 RATIO_CODE）：
```
RATIO_CODE = {"9:16": 3, "16:9": 2, "1:1": 6, "4:3": 4, "3:4": 5}
```
- 生产一直用 9:16=3，所以**没有受到影响**；但 pool API 以前的 16:9/1:1 请求其实是错的
  （16:9→1 直接 13002，1:1→2 实际出 16:9）。
- app.py 的 ratio 白名单同步放宽到 9:16 / 16:9 / 1:1 / 4:3 / 3:4。
- 附带实测：**请求 5s 也会被 agent 拉到 10.5~15.0s**，计费仍按实际产出 4 分/秒
  （53 分 / 13.33s、41 分 / 10.5s），进一步印证「请求秒数只是提示」。

## 18. 平台侧延迟（17:00 观测，待定论）
- 16:00 前：单段 400~700s，服务端 32 worker 时约 217 段/h、完成 75~90 条/h。
- 17:00 后：单段涨到 700~1300s；44 worker 仍只有 110 段/h、完成 12~25 条/h；
  服务端单任务均耗时 2624s（30 分钟级）。
- 对照实验：17:04 暂停本机 28 线程（只留服务端 44 个任务）15 分钟 → 仅完成 1 条，
  说明**不是本机并发挤压**，而是平台侧整体变慢/对新号限速。
- 5s 小请求在新号上 280~330s 就能完成，说明「新号 + 短请求」仍然较快；
  慢的是 30s 多段任务（每段被拉长 + 短段导致段数变多）。
