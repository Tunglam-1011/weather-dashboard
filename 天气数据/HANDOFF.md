# 天气数据看板 · 交接文档

> 接手这个项目先读完这一份，不用翻代码。
> 最后更新：2026-10-25 ｜ 数据结构版本 `SCHEMA_VERSION = 2`

---

## 1. 一句话定位

**本地运行的气温观测看板**，追踪若干站点每日「观测到的最高气温」，用折线图和每日区间图看走势，城市可随时增删。

它**不是**天气盘策略工具，**不预测**、**不交易**、**不参与任何结算**。上游只有观测数据，任何"明日最高温是多少"的问题这个项目都回答不了。

当前城市：上海浦东 `ZSPD`、广州白云 `ZGGG`、香港 `HKO`（天文台总部，非机场 VHHH）、
北京 `ZBAA`、深圳 `ZGSZ`、东京羽田 `RJTT`（时区 `Asia/Tokyo`）、重庆 `ZUCK`、武汉 `ZHHH`。

**两套数据源，靠 `station.provider` 区分**：`metar`（机场站，整数 °C，可回填 21 天）与 `hko`（天文台，0.1 °C，1 分钟，无历史）。新增城市默认走 `metar`。

**交互约定：一次只看一个城市。** 顶部是单选的城市切换器，没有「全部」，也不做多城曲线叠加——两个气候带的绝对气温画在同一坐标轴上没有可比性，只会互相干扰。要比较两个城市，切换着看，而不是叠在一起。切换结果记在 `localStorage.weatherBoard.city`，下次打开沿用。

---

## 2. 快速启动

**端口约定（别改）**：本项目固定 **8766**。**8765 留给同级项目 `天气盘策略\监控`**——它的虚拟订单存在 8765 这个 origin 的 localStorage 里，换端口就拿不到。
`start.bat` 已经写死 8766；`server.py` 的 `pick_port` 会先探测：如果 8766 上已经跑着同一个看板，**不再启动第二个**（两个实例共用一份库会互相覆盖），而是直接打开现有看板的页面、以退出码 3 结束，`start.bat` 会据此提示 `Already running`。

```bat
双击 start.bat
```

- 自动选解释器（`py -3` → 逐个验证 `where python` 的结果 → 兜底 `python`），起服务并打开浏览器
- 默认地址 `http://127.0.0.1:8766/`，端口被占用会自动 +1
- 不想自动开浏览器：`start.bat n`（调试用）
- **零 pip 依赖**，只用 Python 标准库（3.9+，实测 3.10.7 / 3.13 均可）

命令行用法：

| 命令 | 作用 |
|---|---|
| `python server.py` | 起服务 + 后台同步 |
| `python server.py --open` | 起服务并打开浏览器（start.bat 用的就是这个） |
| `python server.py --no-sync` | 只读已有归档，完全不联网 |
| `python server.py --probe ZSPD` | 自检上游解析，不启动服务 |
| `python server.py --init` | 建库并打印归档统计 |
| `python server.py --backup` | 热备份到 `data/backup-*.db` |
| `python test_pure.py` | 纯函数单测（站名清洗 / 覆盖率 / 状态判定 / 时区 / 上游解析） |

首次启动为缺历史的站点**回填近 21 天**（香港每次启动还会重补过去 24 小时的逐时曲线）；
之后由同步线程按第 4 节的节奏增量拉取（心跳 60 秒，香港 1 分钟、METAR 默认 20 分钟）。

---

## 3. 数据口径与红线

### 3.1 两条数据源，口径不同（改文案前先分清）

| | METAR 站（默认） | 香港 `HKO` |
|---|---|---|
| 来源 | aviationweather.gov | 香港天文台开放数据 |
| 站点 | 机场（如上海浦东 ZSPD） | 天文台总部（尖沙咀），**不是**机场 VHHH |
| 精度 | 整数 °C | **0.1 °C** |
| 频率 | 约 30 分钟 | 1 分钟（我们每 1 分钟问一次快照，源头约 10 分钟才更新一版） |
| 历史 | 可回填 21 天 | 每日极值 21 天 + **今天 00:00 起的逐小时曲线**；5 分钟级历史天文台未公开 |
| 每日最高 | 我们自己采样算 | **官方逐分钟统计，直接取用** |

香港的「今日最高」来自天文台 `latest_since_midnight_maxmin.csv`（当日 00:00 起），
写进 `daily_extreme` 表，界面上带一个 **官方** 角标。这是唯一一个能说「官方值」的城市，
其余城市仍然只能说「观测最高温」。

### 3.2 METAR 的三条硬约束（上游决定的，改不掉）

| 事实 | 后果 |
|---|---|
| 温度是**整数摄氏度** | 真实 21.4°C 显示 21°C，量化误差 ±0.5°C |
| 约 **30 分钟一条**观测 | 真实峰值可能落在两次观测之间，被完全漏掉 |
| 单站最多回推 **400 条 / 约 30 天** | 项目上线前的历史永久拿不到，只能靠本地归档积累 |

### 3.3 因此默认为什么只能说「观测最高温」

**红线：禁止**把 METAR 城市的数字改写成「当日极值」「官方最高温」「结算温度」，也**禁止**拿去和任何合约的结算口径做等价替换。

理由：观测最高温是采样值，不是极值。为此界面上所有相关位置都同时给出**覆盖率**和**状态**（仍在上升 / 已见顶 / 数据滞后），让读者自己判断可信度。改文案时不要把这些限定词删掉。

**覆盖率的定义（2026-10-23 改过，别再改回条数）**：算的是**时间轴覆盖比例**——把 00:00 到现在切成段，只有跨度超过 `GAP_THRESHOLD`（90 分钟）的窟窿算「没覆盖」，和画曲线时断点断开的判定完全一致。
以前用「观测条数 ÷ 按中位间隔推算的应有条数」，一旦同一天混了两种粒度就失真：香港早先按小时回填、之后 5 分钟采样，中位间隔被带偏，29 条被算成「应有 60 条 = 48%」。条数只作为辅助信息放在 `missingObs`（缺几报），不参与百分比。

香港是例外：那一行本来就取自官方极值文件，所以界面上标「官方」，且当官方值高于我们采到的
任何一点时，时间会显示「采样未捕捉到峰值」而不是编一个时刻出来。

### 3.4 存储策略（有上限，不是无限增长）

| 项 | 默认 |
|---|---|
| 保留期 | 默认 **90 天**（`ARCHIVE_KEEP_DAYS`）；**界面改的值存 `meta.keep_days`**，后台每天的自动清理与接口展示都以它为准，`0` = 永久保留 |
| 单条体积 | 实测约 **170 字节**（含原始报文，占大头） |
| METAR 半小时站（每站） | 约 48 条/天 ≈ 8 KB；一小时站（如 ZGSZ / ZHHH / ZUCK）约一半 |
| 香港 `HKO` | 源头约 **10 分钟一更**（上限 ≈144 条/天），实际按开机时长积累 |
| 当前 8 站实测 | 约 **37 KB/天**，90 天封顶约 **3.3 MB** |

清理时会顺带 `VACUUM` + `wal_checkpoint(TRUNCATE)`，界面上看到的体积是**真实回收后**的。
WAL 默认要涨到 1000 页才自动回收，所以每轮同步后主动 checkpoint 一次，否则界面体积会虚高。

**自动备份（2026-10-25 起）**：归档是这些历史的**唯一副本**——METAR 上游只留约 30 天、
香港根本没有历史，库文件损坏 = 永久丢失。同步线程每 `BACKUP_INTERVAL_DAYS`(7) 天自动
`VACUUM INTO` 一份到 `data/backups/`，只保留最近 `BACKUP_KEEP_COPIES`(4) 份，无需停服。

导出：界面右上角 **⇩** → 选 CSV / JSONL 与时间范围 → 下载到本机。也可以直接：

```
GET /api/export?format=csv&days=30&download=1
POST /api/archive/prune?keep_days=90
```

**改保留期前先提醒用户导出**：清理不可恢复。

---

## 4. 架构与数据流

```
web/dashboard.html          ← 单文件自包含看板（内联 CSS + JS + 手写 SVG）
        │  fetch /api/*
        ▼
server.py                   ← 入口：端口探测、静态托管、启动同步线程
        │
        ├──► app/api.py     ← 路由 + TTL 缓存（只读库）
        │            │
        │            ▼
        │      app/daily.py ← 日切分、最高温、状态判定（纯函数）
        │            │
        │            ▼
        │      app/db.py    ← SQLite（WAL，thread-local 连接）
        │            ▲
        │            │ 写
        └──► app/sync.py    ← 守护线程：60 秒心跳轮询 + 首次回填 + 定期备份/清理
                     │
                     ▼
             app/source.py ← aviationweather.gov 客户端
```

### 目录

```
天气数据/
├─ start.bat              启动脚本
├─ server.py              入口 / HTTP / 静态托管 / 命令行
├─ HANDOFF.md             本文档
├─ app/
│  ├─ config.py           所有可调参数（改行为只改这里）
│  ├─ naming.py           站名规范化（统一英文、去 INTL 后缀）
│  ├─ tzutil.py           时区兜底 + 自然日切分
│  ├─ db.py               存储：建表、幂等写入、查询、清理
│  ├─ source.py           上游客户端（HTTP）
│  ├─ sync.py             同步调度 + 历史回填
│  ├─ daily.py            每日聚合 + 状态判定
│  └─ api.py              路由 + 序列化
├─ web/dashboard.html     看板（单文件）
└─ data/weather.db        运行时生成
```

### 两条互不干扰的读写流

- **HTTP 线程**：只读库，永不阻塞在网络 I/O 上。上游挂了页面照样秒开，只是显示滞后提示。
- **同步线程**：只写库。用一把全局锁保证同一时刻只有一条上游请求链在飞。

两者通过 SQLite WAL 解耦。**不要**在 api 层直接调 `source`，也**不要**让 sync 线程返回 HTTP 响应。

一轮同步按 `provider` 分派：`metar` 走 `fetch_metar`（批量写 `observation`），`hko` 走 `_sync_hko`
（一个 1 分钟气温文件写 `observation`，一个当日极值文件写 `daily_extreme`）。两路互不影响，
香港抓失败不会拖累上海。节流按**每个站的** `last_attempt_at` 各算各的，不是全局一个时间戳。

**每轮只同步「当前打开的城市」**（`meta.active_icao`）：页面加载和切换城市时通过
`POST /api/active` 上报，后台心跳一轮就只拉它，不再把 8 个城市全刷一遍。
切换城市时前端会顺手强制同步一次，避免看到几小时前的旧数据。
没有 active（第一次跑、只开服务不开页面）时，才退回全量。

**其余城市按各自的数据节奏"到期才刷"**——不同源的更新频率差得很远，一刀切是浪费：

| 源 | 数据更新频率 | 刷新间隔 |
|---|---|---|
| 香港 `hko` | 快照约 **10 分钟**一更（源头硬上限） | 1 分钟问一次（`HKO_REFRESH_INTERVAL`），发布后最快 1 分钟入库 |
| METAR | 半小时一报 | 默认 20 分钟（`METAR_REFRESH_INTERVAL`） |
| METAR（一小时报的机场） | 一小时 | 按归档实测报文间隔自适应（间隔 × 0.6，钳在 10~60 分钟） |

调度心跳是 **60 秒**（`POLL_INTERVAL`），心跳本身只查"谁到期"，不发请求；
所以把心跳调快不会增加上游压力，只是让到期的站被更早发现。
官方当日极值变化慢，跟天气温 1 分钟拉、极值 5 分钟拉一次（`HKO_EXTREME_INTERVAL`）。

到期 = 距上次尝试超过自己的间隔；**按"逾期倍数"排序**（age ÷ interval）而不是绝对时长——
否则 1 分钟节奏的香港永远排不到 20 分钟节奏的 METAR 前面，会被饿死（踩过）。
每轮最多顺带刷 `MAX_EXTRA_PER_ROUND`(2) 个到期的站。

**手动刷新走单站锁**：`sync_all(only=...)` 只锁这一个站，不抢整轮用的全局锁。
否则正好撞上后台一轮（两三个站、好几秒）就得干等，点刷新像卡死。

### 数据表

| 表 | 用途 | 关键点 |
|---|---|---|
| `station` | 城市配置 | `icao` 主键（3–4 位）；`provider` 决定数据源（`metar` / `hko`）；`place` 是天文台的站名；`enabled` 停用；`tz` 改后必须 `rebuild_local_dates` |
| `observation` | 观测归档 | 主键 `(icao, obs_time)` → 写入天然幂等；`local_date` 是入库时算好的一列，让「每日最高」变成一句 GROUP BY |
| `daily_extreme` | **官方当日极值** | 只有 `hko` 会写；`(icao, local_date)` 主键，每轮覆盖。日最高优先取它，采样点只负责画曲线 |
| `sync_state` | 同步进度 | 回填游标、失败次数、退避时间；`last_attempt_at` 同时充当**每站**的节流依据 |
| `meta` | 杂项 | `schema_version`、`last_prune_at`、`keep_days`（界面改的保留期）、`last_backup_at`、`names_en`、`hko_added`、`active_icao` |

---

## 5. 接口契约

统一响应：成功 `{"ok":true,"serverTime":<epoch>,"data":...}`；失败 `{"ok":false,"error":{"code","message"}}`。
时间一律 **epoch 秒（UTC）**；时序一律 `[[epoch, temp, dewp, wdir, wspd], ...]` 升序。
**单位**：`temp`/`dewp` 是 °C，`wdir` 是度，`wspd` 是**节（knots）**——换算 km/h 用 ×1.852。
（报文里中国站常写 `05007MPS`，但 JSON 的 `wspd` 已经统一换成节了，别再按 m/s 处理。）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查、归档条数、同步状态 |
| GET | `/api/stations` | 城市列表 + 最新观测 + 今日/昨日聚合 + 归档统计 |
| GET | `/api/stationinfo?icao=ZBAA` | 只校验 ICAO，预览站名（`site` 原文 + `site_name` 清洗版）/ 经纬度 / 是否提供 METAR |
| POST | `/api/stations?icao=ZBAA` | **新增城市**（自动补全 + 后台回填 21 天） |
| POST | `/api/stations?provider=hko&icao=HKO` | **新增香港**（固定天文台总部，`place` 传别的会 400） |
| POST | `/api/stations/ZBAA` | 改名 / 改时区 / 停用（`enabled=0`）/ 排序 |
| DELETE | `/api/stations/ZBAA?purge=1` | 删除（默认保留历史观测） |
| GET | `/api/series?icao=ZSPD,ZGGG&range=today` | 折线数据 + 断点 + 日边界 |
| GET | `/api/daily?icao=ZSPD&days=14` | 每日区间与最高温聚合 |
| GET | `/api/observations?icao=ZSPD&limit=20` | 观测明细（含原始报文） |
| POST | `/api/sync?icao=ZSPD&force=1` | 手动触发拉取（不带 `icao` = 当前打开的城市） |
| GET/POST | `/api/active?icao=ZSPD` | 读取 / 设置「当前打开的城市」；后台轮询只同步它 |
| GET | `/api/export?format=csv&days=30` | 导出归档（`download=1` 触发浏览器下载） |
| GET | `/api/archive` | 归档概况（条数 / 体积 / 增长 / 保留期） |
| POST | `/api/archive/prune?keep_days=90` | 清理超期数据并回收磁盘 |

`range` 取值：`today`（当地 00:00 起，横轴铺满整天）、`24h`、`48h`、`7d`、`21d`，也支持 `from`/`to` 自定义 epoch。

### 加城市（最常用）

```bat
curl -X POST "http://127.0.0.1:8766/api/stations?icao=ZBAA"
curl -X POST "http://127.0.0.1:8766/api/stations?icao=ZUUU&name=Chengdu"
curl -X POST "http://127.0.0.1:8766/api/stations?provider=hko&icao=HKO"
curl -X POST "http://127.0.0.1:8766/api/stations/ZBAA?enabled=0"
curl -X DELETE "http://127.0.0.1:8766/api/stations/ZBAA?purge=1"
```

> 如果 curl 报代理错误（`upstream connect failed`），加 `--noproxy '*'`。
> 常见中国机场：北京 `ZBAA`、成都 `ZUUU`、深圳 `ZGSZ`、武汉 `ZHHH`、上海虹桥 `ZSSS`。

变更类接口**同时接受 query 参数和 JSON body**，query 形式对 Windows 命令行更友好。

### 香港怎么接的（和 METAR 完全不同的一条路）

天文台的数据在 `data.weather.gov.hk`，**不需要 key，还带 `Access-Control-Allow-Origin: *`**。
用了四个文件，分工明确：

| 文件 | 内容 | 用途 |
|---|---|---|
| `latest_1min_temperature.csv` | 1 分钟气温，0.1 °C，39 个自动气象站 | 画曲线（每 1 分钟问一次，源头约 10 分钟更新一版，出新版才入库） |
| `latest_since_midnight_maxmin.csv` | 当日 00:00 至今的最高 / 最低 | **今日最高以它为准**，写 `daily_extreme` |
| `cis/dailyExtract/dailyExtract_YYYYMM.xml` | 按月发布的**逐日**数据（到昨天为止） | **回填近 21 天每日最高/最低** |
| `wxinfo/awsgis/animate_J1.csv` | 区域气象站门户的**过去 24 小时逐小时**气温 | **回填今天早先的曲线**（TimeSeries 弹窗用的就是这份） |

**关于「5 分钟级历史」：天文台没有公开。** 门户上点站点看到的曲线就是 `animate_J1.csv`
（逐小时 × 24 小时 × 39 站），不是 5 分钟。所以：
- 今天 00:00 到现在 → 逐小时回填，曲线完整；
- 从现在往后 → 每 1 分钟轮询快照，源头更新一版就入库一条（实际约 10 分钟一条）；
- 想要更细的"未来"粒度，把 `HKO_REFRESH_INTERVAL` 调小即可，代价是请求变多。

**只采用天文台总部（尖沙咀）这一个站。** 赤鱲角（机场）、跑马地、启德跑道公园这些
自动站彼此能差 1–3 °C，混着用等于口径不统一；香港的官方气温记录本来就是总部站。
所以 `place` 固定 `HK Observatory`，接口传别的直接 400（不是悄悄忽略）。

站名用英文，所以不需要 `naming.py` 清洗。今天总部最高 30.7 °C、赤鱲角 30.1 °C——
差 0.6 °C 已经足够跨一个档位，这就是不能用错站的原因。

**历史只有每日粒度**：逐日提取文件给的是每天的官方最高/最低，没有任何逐时曲线。
每日区间图能一次补满 21 天，折线图能补满今天，但更早的逐时细节不存在。

### 站名规范（`app/naming.py`）

**所有城市名统一是英文**，中文只出现在界面文案里，不进数据。上游 `site` 是航空
写法「城市/机场 + 类型词」，入库前统一清洗：

| 上游 `site` | 显示名 |
|---|---|
| `Shanghai/Pudong Intl` | `Shanghai Pudong` |
| `Guangzhou/Baiyun Intl` | `Guangzhou Baiyun` |
| `Beijing Intl` | `Beijing` |
| `Frankfurt/Main Arpt` | `Frankfurt Main` |

规则只有两条：斜杠换空格、丢掉 `INTL / INTERNATIONAL / ARPT / AIRPORT / APT`
这类纯类型词。**不翻译、不缩写、不猜城市**，同一站点每次结果一致。

- 新增城市时 `name` 自动取清洗后的 `site_name`；`/api/stations` 里用 `siteRaw`
  暴露上游原文供核对（故意不叫 `nameEn`：任何界面直接渲染都会把 `Intl` 加回来）。
- 显式传 `name=...` 时**按原样保存**，用于自定义命名，不做清洗。
- 老库在启动时跑一次性迁移（`db._migrate_station_names`，`meta.names_en=1` 标记），
  之后手动改的名字不会被覆盖。

---

## 6. 每日最高温的状态判定

不额外记忆状态，只看两个事实：**最新观测是否等于当日最大值**、**数据是否新鲜**。

| 状态 | 条件 | 界面文案 |
|---|---|---|
| `rising` | 今日 + 最新值 = 最大值 + 距最新观测 ≤ 90 分钟 | 仍在上升（虚线流动） |
| `settled` | 今日 + 最新值 < 最大值 | 已见顶 |
| `stale` | 今日 + 超过 90 分钟无新报 | 数据滞后 |
| `partial` | 历史日 + 覆盖率 < 50% 或存在 >3h 缺口 | 观测不全 |
| `final` | 历史日 + 覆盖率 ≥ 50% | 已定稿 |
| `empty` | 当日无有效观测 | 无数据 |

日最高出现多次时，接口仍会返回 `maxTimeFirst` / `maxTimeLast` / `maxCount`（供外部脚本用），
但**界面上只显示首次时刻**：末现时刻和平持平报数对判断没有帮助，只会把一行字塞满。
还在上升（`rising`）时文案是「HH:MM 达当前最高」，不能说"见顶"——那时最高温就是最新一笔。

改这张表时，`app/daily.py::_classify` 与 `dashboard.html::STATUS_TEXT` 必须同步改。

---

## 7. 已知坑（全部实测过，重踩一次要半天）

1. **上游返回数组是倒序的**（最新在前）。入库前必须按 `obs_time` 升序重排，否则曲线左右颠倒。
2. **`date=` 是窗口的结束锚点**，不是开始。往历史翻页要拿"当前窗口最老那条"当锚点，写反会死循环（代码里加了 `oldest >= anchor - 3600` 的防呆）。
3. **超期返回空 body**（不是 `[]`），日期太老返回 **HTTP 400**。两者都表示"没有更早数据了"，属正常终止；只有超时 / 5xx 才是真错误，需要退避。`fetch_metar` 用 `ok / empty / out_of_range / error` 四态区分。
4. **`visib` 可能是字符串** `"6+"`，列类型必须是 TEXT，不能按 float 解析。
5. **`zoneinfo` 在本机不可用**：Windows 版 Python 没自带 tzdata，`ZoneInfo("Asia/Shanghai")` 会抛 `ZoneInfoNotFoundError`。`tzutil.get_tz()` 做了固定偏移兜底。中国 1991 年后无夏令时，`+8` 与 IANA 等价；**接欧美站点前必须 `pip install tzdata`**，否则夏令时切换后错一小时。
6. **只删不回收等于没省空间**：SQLite 的 DELETE 不缩文件。清理后必须 `VACUUM` + `PRAGMA wal_checkpoint(TRUNCATE)`，否则界面上的体积不会降。同理，WAL 不 checkpoint 会让"库体积"虚高好几倍。
7. **`http.server` 是 HTTP/1.1**：每个响应都必须带 `Content-Length`，否则浏览器 keep-alive 会挂住。
8. **单站 400 条上限**：增量拉取用 `hours=6`，回填用 `hours=192`（384 条不触顶）。别一次要 240 小时以上。
9. **`SyncWorker` 的方法绝对不能叫 `_bootstrap`**（最坑的一个）：`threading.Thread` 内部拿 `_bootstrap` 当线程入口，并靠它去 set `_started` 事件。同名覆盖后 `Thread.start()` 永远等不到事件，**主线程会永久卡死在 `start()`**——表现是服务不监听、也不打印任何东西，但后台同步线程其实已经在正常跑（数据照样回填）。现已改名 `_initial_sync`。
10. **`PRAGMA journal_mode=WAL` 只在 `init_db()` 里设一次**：它是数据库级持久设置，每个新连接都执行会去抢排他锁，多线程下互相等待。
11. **`start.bat` 必须是 CRLF 行尾 + 纯 ASCII**：cmd.exe 用当前代码页解析批处理文件，UTF-8 中文行会被拆成多段当成命令执行（连 `rem` 注释都会被执行），CRLF 缺失会让 `if (...)` 块解析错乱。中文提示一律交给 Python 输出（`sys.stdout.reconfigure(encoding="utf-8")`），批处理只留英文。
12. **批处理里 `%errorlevel%` 在 `if (…)` 块内是解析期展开**：块里先执行 `where` 再判断 `%errorlevel%` 拿到的是块外的旧值。全部分支改用 `goto` 标签。
13. **天文台的实时 CSV 只有「最新快照」，历史分布在两个地方**：`latest_1min_temperature.csv` / `latest_since_midnight_maxmin.csv` 每次覆盖，拿不到过去；`cis/dailyExtract/dailyExtract_YYYYMM.xml`（文件名 .xml，内容是 JSON）按月提供**逐日**最高/最低到昨天；`wxinfo/awsgis/animate_J1.csv` 提供**过去 24 小时逐小时**气温（门户弹窗用的就是它）。**5 分钟级历史天文台没有公开**，别去猜什么 `latest_10min_temperature.csv`（不存在，`latest_10min_wind.csv` 才是真的）。
14. **天文台的极值文件在 00:00（香港时间）清零**：`latest_since_midnight_maxmin.csv` 是「午夜至今」，跨日必须按 `local_date` 重新落一行，别把昨天的极值当成今天的。日期要从 CSV 里的 `202610222130` 解析，别用服务器当前时间。
15. **CSV 带 BOM，数值可能带星号**：首行是 `\ufeff日期时间,…`，直接按字节切列会把表头搞脏；数值列会出现 `24.1*`（数据不完整标记），`float()` 前要 `rstrip("*")`。
16. **天文台站名是英文，但简体/繁体两个版本不是英文**：`…_sc.csv` / `…_uc.csv` 是中文（"赤鱲角"、"天文台"），**不带后缀**的才是英文。抓错文件会导致站名永远匹配不上。
17. **同步节流必须是「每站各算各的」**：原先用一个全局 `_last_fetch` 做 `MIN_FETCH_GAP` 判断，一轮里只有第一个站能过，其余全被判 `too_soon`——城市越多刷得越慢，而且第一站永远是最新的。现在改用 `sync_state.last_attempt_at`。
18. **`wspd` 的单位是节，不是 m/s**：前端曾经用 `wspd * 3.6` 换算，风速被放大近一倍（22 km/h 显示成 47）。正确系数是 **1.852**。同时注意 METAR 风是整数量化（整节或整 m/s），连续几笔一模一样是正常的，不是卡数据——上海 20:00–22:30 连报 `05007MPS` 就是真的一直吹 7 m/s。
19. **不要同时跑两个实例**：两个进程共用同一个 `weather.db`、各自跑一个同步线程，`sync_state` 互相覆盖，表现是「某个城市一直不更新」或「数据忽新忽旧」。现在启动时会先探测目标端口：如果是同一个看板就**直接退出并提示去打开现有页面**，不会再顺着 8765→8766 起第二个（端口被别的程序占用时仍会往后找）。
20. **香港的「更新慢」有一半是源头的**：`latest_1min_temperature.csv` 名字里带 1min，实际约 10 分钟才刷新一次，数据时间戳就是上一个 10 分钟整点；`rhrread` 更旧（整数、时间戳更早）。所以最快也只能做到「发布后 1 分钟内入库」，再快就得等天文台改接口。
21. **挂系统代理的机器上，两个上游的最优路径完全相反（2026-10-25 实测）**：`data.weather.gov.hk` 走代理 6~18 秒/次、直连亚秒级；`aviationweather.gov` 走代理 0.5 秒、直连 16 秒。原先 `http_get` 永远先试系统代理，香港手动刷新一次要串行等两个慢请求 ≈ 35 秒。现在 `source._openers_for` **按域名记忆**「代理 or 直连谁先」（香港域预设直连优先，失败自动翻转），香港 CSV 另用短超时（`HKO_HTTP_TIMEOUT=8s`）与少重试（`HKO_HTTP_RETRIES=1`），且手动刷新不再强制连带抓极值文件（按 `HKO_EXTREME_INTERVAL` 节奏来）。实测：刷新 35s → 3.5s（首次）/ 1.8s（后续）。data.gov.hk 只是数据目录，HKO 数据端点都在 `data.weather.gov.hk`，没有更快的替代接口。

---

## 8. 改动检查清单

### 改之前确认

- [ ] 改的是 `config.py` 里的常量，还是在业务代码里写死了数字？
- [ ] 改 schema 了吗？→ `SCHEMA_VERSION += 1`，写迁移，并更新本文第 4 节
- [ ] 改时区相关？→ 调 `db.rebuild_local_dates(icao)` 重算历史 `local_date`
- [ ] 改接口？→ 同步本文第 5 节 + `dashboard.html` 里对应的 fetch
- [ ] 改状态判定？→ 同步 `daily._classify` 与 `STATUS_TEXT`
- [ ] 碰存储策略？→ 确认 `prune_observations` 仍然 `VACUUM`，并提醒用户先导出

### 改之后自测（5 条，30 秒）

```bat
python test_pure.py                    :: 纯函数单测全绿
python server.py --probe ZSPD          :: 上游能通、解析正常
python server.py --init                :: 库结构正常、归档条数不为 0
python server.py --no-sync             :: 页面能开、曲线出得来
curl "http://127.0.0.1:8766/api/stations"
```

### 调试入口

| 现象 | 先看哪 |
|---|---|
| 页面空白 / 一直转圈 | 浏览器控制台；`/api/health` 是否 200 |
| 曲线不出来但有数据 | `/api/series` 的 `points` 是否为空；`temp_c` 是否为 NULL |
| 今日最高温不对 | `/api/daily` 的 `date`/`isToday`/`status`；时区是否解析成 UTC |
| 归档不涨 | `/api/health` 的 `sync.lastError`；`--probe` 能否连通 |
| 体积不降 | 是否走了 `prune` 的 VACUUM 分支 |
| 香港没数据 | `python server.py --probe HKO` 看两个 CSV 是否可达；`/api/stations` 里 `provider` 是否为 `hko`、`place` 是否拼对 |
| 香港最高温与曲线不符 | 正常：最高取自官方极值文件，曲线是轮询采样点，官方值可以高于曲线峰值（hero 会标「官方」） |

---

## 9. 变更记录

| 日期 | 变更 |
|---|---|
| 2026-10-22 | 初版：METAR 抽帧、SQLite 归档、11 个接口、单文件看板 |
| 2026-10-22 | 归档加 90 天保留期 + 一键导出（CSV/JSONL）+ 清理后 VACUUM 回收 |
| 2026-10-22 | 修复启动失败：`SyncWorker._bootstrap` 覆盖 `Thread._bootstrap` 导致主线程卡死；start.bat 改 CRLF + 纯 ASCII + goto 分支；新增启动分段日志与 `data/server.log` |
| 2026-10-22 | 看板改为**单城视图**：城市改单选、去掉多城叠加，曲线只剩一条（面积填充恢复），概览改为一张横向 hero |
| 2026-10-22 | **站名统一英文**：新增 `app/naming.py` 清洗上游 `site`（去 `INTL`/`ARPT` 后缀、斜杠换空格），默认城市改英文名，老库一次性迁移；hero 去掉重复的英文副名 |
| 2026-10-22 | **接入香港天文台**：`station.provider` 区分数据源（`metar` / `hko`），新增 `daily_extreme` 表存官方当日极值；`SCHEMA_VERSION` → 2；顺带修复同步节流被全局 `_last_fetch` 卡成一站的问题 |
| 2026-10-22 | 香港**锁定天文台总部站**（不接受其他自动站）；用 `dailyExtract_YYYYMM.xml` 回填近 21 天每日极值；修复顶栏刷新按钮（原来同步全部城市、20 秒以上像没反应，改为只同步当前城市并给出结果提示） |
| 2026-10-22 | 概览区重排：当前气温从底部统计行提出，做成「今日最高 / 当前气温」两个并列数字块；修复城市栏在中等宽度被挤成一行一个 chip 的 flex 压缩问题 |
| 2026-10-22 | 概览区注解归位：**每个数字只带自己的注解**（首现/末现/持平/较昨日贴在「今日最高」下方，观测时刻贴在「当前气温」下方），去掉原来挂在右侧、容易被误读成当前气温说明的那一列；状态文案交给标题栏的胶囊，不再重复 |
| 2026-10-22 | 香港曲线补齐：用门户 `animate_J1.csv` 回填**过去 24 小时逐小时**气温，每次启动重跑以自动修复停机缺口；确认天文台未公开 5 分钟级历史，更细的点靠 5 分钟轮询现场积累 |
| 2026-10-22 | 概览去掉「末现 · N 报持平」，只留「HH:MM 见顶 · 较昨日」；**修正风速单位**（节→km/h 应为 ×1.852，原来按 m/s 的 ×3.6 算，风速虚高一倍），静风单独显示 |
| 2026-10-22 | **只同步当前打开的城市**：新增 `meta.active_icao` 与 `/api/active`，页面加载/切换时上报；后台轮询与不带 `icao` 的 `/api/sync` 都只跑这一个，切换城市时前端强制补同步一次 |
| 2026-10-22 | **刷新节奏按数据源自适应**：香港 5 分钟、METAR 默认 20 分钟、小时报的机场按实测间隔 ×0.6 自适应（钳 10~60 分钟）；每轮最多带 2 个"到期"的非当前城市，不再全量轮更 |
| 2026-10-23 | 心跳降到 60 秒、香港提到 1 分钟一拉（源头 10 分钟一更，只能把"发布→入库"压到 1 分钟内）；轮换改为按逾期倍数排序（修香港被 METAR 饿死）；页面打开/切回标签页自动补一次；手动刷新改用**单站锁**不抢全局轮次锁，实测香港 0.26s、METAR 1.3s |
| 2026-10-23 | 折线图的「今日最高」虚线改为静止（去掉 `rising` 流动动画与 `dashmove` keyframes），只剩虚线和标记点 |
| 2026-10-23 | 覆盖率改为**时间覆盖比例**（与断点判定同一套规则），修掉香港「29 条 / 应有 60 条 = 48%」的失真；同步回看窗口 6h → **24h**，重启或手动刷新能把凌晨缺的整段补回来 |
| 2026-10-23 | 启动前先占端口再起同步线程；同端口已有看板时直接退出并提示，杜绝双实例共用库互相覆盖 |
| 2026-10-23 | 明确端口约定：本项目 8766，8765 留给同级「监控」项目；同步更新文档，避免再在 8765 起实例和 start.bat 打架 |
| 2026-10-23 | 修「双击 start.bat 没反应」：已在运行时不再只打印一行就退出，而是**直接打开现有看板的浏览器页面**并返回退出码 3，start.bat 据此提示 `Already running - opened the existing board` |
| 2026-10-27 | 修刷新按钮：`spinning` 原来加在 `<button>` 上，把 26px 的方框一起转了；改成图标包一层 `.ico`，动画只作用在图标上 |
| 2026-10-27 | 再修刷新按钮的旋转中心：`<button>` 的 UA 默认 `padding:1px 6px` 没重置，26px 框里只剩 12px 内容宽，15px 图标从左边溢出、视觉中心右偏 1.5px；补 `padding:0;box-sizing:border-box`，图标换成 SVG（viewBox 中心=视觉中心），实测偏差归零 |
| 2026-10-25 | **修复回填丢失**：新增城市撞上正在跑的同步轮时（`schedule_backfill` 抢不到锁），21 天回填从此由轮询在 `_do_sync_all` 里接管（原先只置 `backfill_done=0`，要等重启才补）；回填失败接入指数退避，不再每 60 秒重打 |
| 2026-10-25 | `/api/series` 缓存键按分钟量化（24h/7d 等档位的 from/to 原先秒级变化，缓存永远不命中）；`_cached` 增加过期淘汰，上游断更或 `--no-sync` 时不再无限堆积大 payload |
| 2026-10-25 | **保留天数持久化到 `meta.keep_days`**：界面改的值后台每日自动清理同样生效（原先次日仍按 config 的 90 清，等于白改）；`/api/health`、`/api/archive` 一并读取 |
| 2026-10-25 | 新增**自动备份**：同步线程每 7 天 `VACUUM INTO` 到 `data/backups/`，保留最近 4 份（归档是唯一副本，上游只留 21–30 天） |
| 2026-10-25 | 修复：顶栏上游错误信息被「最新观测」无条件覆盖；METAR 去重改为保留**更新的**重复报（更正报 COR 原先会被旧值顶掉）；香港 `rising` 判定改与**采样最大值**比较（原先官方极值高于采样点时，升温中的上午会被判「已见顶」） |
| 2026-10-25 | 归档「每日增量」改按**实际入库速率**（总量 ÷ 覆盖天数）估算，不再按「站点数 × 48」一刀切；`server.log` 运行中超 2 MB 自动轮转（原先只在启动时检查）；折线纵轴改 floor/ceil（负温不再向零截断）；删除无人使用的 `VACUUM_THRESHOLD` |
| 2026-10-25 | 清理 8765 残留：本文档示例、看板 curl 示例与离线提示统一为 8766；`start.bat` 中文注释换英文（回归坑 11 的纯 ASCII 规矩）；文档口径对齐实测（香港源头约 10 分钟一更、当前 8 城 ≈37 KB/天） |
| 2026-10-25 | 新增纯函数单测 `test_pure.py`（26 例：站名清洗 / 覆盖率 / 状态判定 / 日聚合 / 时区 / 上游解析）；修数据：东京羽田 `RJTT` 时区由默认 `Asia/Shanghai` 改为 `Asia/Tokyo` 并重建 54 行 `local_date`（添加时没传 tz 吃了默认值，日切分偏一小时） |
| 2026-10-25 | **香港同步提速 35s → 3.5s**：`http_get` 改为**按域名记忆**最优 opener（挂系统代理的机器上香港域直连亚秒级、代理 6~18 秒，而 aviationweather 正好相反）；香港 CSV 用短超时 8s / 重试 1 次；手动刷新不再强制连带抓官方极值（按 5 分钟节奏）。已确认 data.gov.hk 只是目录、HKO 端点无更快的替代来源，见坑 21 |
