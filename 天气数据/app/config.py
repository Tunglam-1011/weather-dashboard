"""全局配置。

所有可调参数都集中在这个文件里，改行为只改这里。
其余模块只读取，不就地修改 —— 这样交接时看一个文件就知道系统能调什么。
"""

from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------- 路径

APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent
WEB_DIR = ROOT_DIR / "web"
DATA_DIR = ROOT_DIR / "data"
DB_PATH = DATA_DIR / "weather.db"
DASHBOARD_PATH = WEB_DIR / "dashboard.html"

# ---------------------------------------------------------------- 版本

# 改动 db.py 里的表结构时把这个数字 +1，并在 HANDOFF.md 里写一条迁移说明。
SCHEMA_VERSION = 2

# ---------------------------------------------------------------- 服务

HOST = "127.0.0.1"
# 8765 留给同级项目「天气盘策略\监控」：它的虚拟订单存在 8765 这个 origin 的
# localStorage 里，换端口就等于换一份空存储。本看板固定用 8766（被占用时往后试）。
PORT = 8766
PORT_TRIES = 10  # 端口被占用时往后试几个

# ---------------------------------------------------------------- 同步

POLL_INTERVAL = 60         # 调度心跳（秒）。只是查"谁到期"，不发请求
LOOKBACK_HOURS = 24        # 每次同步回看 24 小时：机器睡一晚、上游延迟入库都能自动补回
#   24 小时 ≈ 48 条，远低于单次 400 条上限；重复记录走主键忽略，不会重复写。
BACKFILL_DAYS = 21         # 首次启动回填天数。上游只保留约 30 天，21 天留余量
BACKFILL_CHUNK_HOURS = 192 # 单次回填跨度。384 条 < 上游 400 条上限，不触顶
MIN_FETCH_GAP = 55         # 两次拉取的最小间隔，尊重上游 Cache-control: max-age=60
STATION_SPACING = 0.6      # 逐站串行拉取时的间隔，不并发轰炸
BACKFILL_STEP_SLEEP = 1.2  # 回填翻页之间的间隔

# ---------------------------------------------------------------- 各站刷新节奏
# 数据源的更新频率差得很远，统一按 5 分钟全刷是浪费：
#   香港快照约 10 分钟一更；METAR 半小时一报（部分机场一小时）；后台轮询 5 分钟
# 心跳一次，谁"到期"谁才被拉。
HKO_REFRESH_INTERVAL = 60     # 香港快照约 10 分钟一更，1 分钟问一次：发布后最快 1 分钟入库
HKO_EXTREME_INTERVAL = 300    # 官方当日极值变化慢，5 分钟抓一次就够，省一半请求
METAR_REFRESH_INTERVAL = 1200 # METAR 默认 20 分钟（半小时数据的 2/3）
MAX_EXTRA_PER_ROUND = 2       # 每轮最多顺带刷新几个"到期"的非当前城市，封顶延迟

# ---------------------------------------------------------------- 归档保留

# 归档不是无限增长的：默认只保留最近 90 天的观测，超期自动清理。
# 设为 0 表示永久保留（自己负责空间）。
# 注意这是「默认值」：界面上改的保留天数存在库的 meta 表（keep_days），
# 后台每天的自动清理以 meta 为准 —— 两边读同一个来源，不会今天改成 365、
# 明天又被悄悄清回 90。
ARCHIVE_KEEP_DAYS = 90

# ---------------------------------------------------------------- 自动备份

# 归档是这些历史的唯一副本：METAR 上游只留约 30 天、香港根本没有历史，
# 库文件一旦损坏，超过回填窗口的数据就永久丢了。所以后台定期在线备份
# 一份到 data/backups/，只保留最近几份。设 0 关闭。
BACKUP_INTERVAL_DAYS = 7
BACKUP_KEEP_COPIES = 4

# 单条观测的粗略字节数，仅用于界面上估算占用
BYTES_PER_OBS_ESTIMATE = 120

# ---------------------------------------------------------------- 缓存

SERIES_CACHE_TTL = 30      # 折线接口缓存。页面切区间时秒响应
STATIONS_CACHE_TTL = 10    # 城市列表缓存，要较快反映同步结果
DAILY_CACHE_TTL = 30

# ---------------------------------------------------------------- 口径

GAP_THRESHOLD = 5400       # 相邻两点间隔超过 90 分钟就判为断点，曲线断开不插值
STALE_AFTER = 5400         # 超过 90 分钟没有新报 → 数据滞后
SAMPLE_INTERVAL = 1800     # 兜底采样间隔（30 分钟），用于估算当日应有报数
DEFAULT_TZ = "Asia/Shanghai"

# ---------------------------------------------------------------- 上游

UPSTREAM_BASE = "https://aviationweather.gov/api/data"

# 香港天文台开放数据（无需 key，带 Access-Control-Allow-Origin: *）
# 两个文件都是"最新快照"，每次覆盖，没有历史 —— 这也是香港只能往后积累的原因。
HKO_BASE = "https://data.weather.gov.hk/weatherAPI/hko_data/regional-weather"
HKO_TEMP_CSV = "latest_1min_temperature.csv"          # 1 分钟气温，0.1°C
HKO_EXTREME_CSV = "latest_since_midnight_maxmin.csv"  # 当日 00:00 起最高/最低
HKO_DEFAULT_PLACE = "HK Observatory"                   # 天文台总部 = 官方记录站
HKO_MIN_INTERVAL = 60       # 同一站两次拉取的最小间隔，香港数据每分钟更新

# 区域气象站门户（TimeSeries 弹窗的数据源）。天文台没有公开 5 分钟级历史，
# 但这里能拿到"过去 24 小时"的逐小时气温，用来补当天早些时候的空白。
HKO_PORTAL_BASE = "https://www.hko.gov.hk/wxinfo/awsgis"
HKO_HOURLY_CSV = "animate_J1.csv"   # 逐小时气温，24 小时 × 39 站
HKO_STATION_CODE = "hko"            # 门户里天文台总部的站码（小写）

HTTP_TIMEOUT = 25
HTTP_RETRIES = 2
# 香港的两个 CSV 只有 1~2 KB，直连亚秒级；超时给短一点，
# 网络不好时快速失败交给下一轮，别让手动刷新干等半分钟。
HKO_HTTP_TIMEOUT = 8
HKO_HTTP_RETRIES = 1
USER_AGENT = "weather-obs-board/1.0 (personal local weather dashboard)"

# ---------------------------------------------------------------- 默认城市

# 默认城市。站名统一用英文（上游 stationinfo 的 site 去掉 INTL 之类后缀），
# 中文只出现在界面文案里，不出现在数据里 —— 避免中英站名混排。
#
# provider 决定数据从哪来：
#   metar — aviationweather.gov，机场站，30 分钟整数摄氏度，能回填 21 天
#   hko   — 香港天文台开放数据，1 分钟 0.1 摄氏度，无历史（只能往后积累）
DEFAULT_STATIONS = (
    {"icao": "ZSPD", "name": "Shanghai Pudong", "tz": DEFAULT_TZ, "sort_order": 10},
    {"icao": "ZGGG", "name": "Guangzhou Baiyun", "tz": DEFAULT_TZ, "sort_order": 20},
    {
        "icao": "HKO", "name": "Hong Kong", "tz": "Asia/Hong_Kong",
        "provider": "hko", "place": "HK Observatory",
        "lat": 22.3019, "lon": 114.1742, "country": "HK",
        "sort_order": 25,
    },
)

# 新增城市按顺序取色，前端也读这个顺序（/api/stations 返回 color 字段）
COLOR_PALETTE = (
    "#5B9DFF",
    "#FF8A4C",
    "#4ADE80",
    "#C084FC",
    "#F472B6",
    "#FBBF24",
    "#22D3EE",
    "#A3E635",
)

# 前端可选的时间区间 -> 回溯小时数（"today" 特殊处理，从当地 00:00 起算）
RANGE_PRESETS = {
    "today": 1,
    "24h": 24,
    "48h": 48,
    "7d": 24 * 7,
    "21d": 24 * 21,
}
