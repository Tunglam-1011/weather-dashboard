"""存储层：只懂 SQL。

边界约定：
  - 不发起任何网络请求（那是 source.py 的事）
  - 不判断"哪天算今天"（那是 tzutil.py 的事，本模块只调用它把 obs_time 换算成 local_date）
  - 不组装 HTTP 响应（那是 api.py 的事）

为什么用 SQLite 而不是 JSONL：
  (icao, obs_time) 做主键 + INSERT OR IGNORE = 天生幂等，重复拉取零代价；
  每日最高温是带索引的一句 GROUP BY；
  WAL 模式下读线程（HTTP）与写线程（同步）互不阻塞，断电自动回滚不会留半行脏数据。
"""

from __future__ import annotations

import sqlite3
import threading
import time

from . import config, naming
from .tzutil import to_local_date

_local = threading.local()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS station (
    icao        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    name_en     TEXT,
    lat         REAL,
    lon         REAL,
    elev        REAL,
    country     TEXT,
    state       TEXT,
    tz          TEXT NOT NULL DEFAULT 'Asia/Shanghai',
    provider    TEXT NOT NULL DEFAULT 'metar',
    place       TEXT,
    enabled     INTEGER NOT NULL DEFAULT 1,
    sort_order  INTEGER NOT NULL DEFAULT 100,
    created_at  INTEGER NOT NULL DEFAULT 0,
    note        TEXT
);

CREATE TABLE IF NOT EXISTS observation (
    icao        TEXT NOT NULL,
    obs_time    INTEGER NOT NULL,
    local_date  TEXT NOT NULL,
    temp_c      REAL,
    dewp_c      REAL,
    wdir        INTEGER,
    wspd        REAL,
    visib       TEXT,
    altim       REAL,
    raw_ob      TEXT,
    fetched_at  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (icao, obs_time)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_obs_day ON observation (icao, local_date, obs_time);

-- 官方当日极值。只有 hko 这类"官方自己算极值"的数据源才会写这张表：
-- 官方逐分钟在算，我们每 5 分钟抓一次快照，峰值完全可能落在两次抓取之间，
-- 所以日最高以这里为准，我们采到的点只负责画曲线。
CREATE TABLE IF NOT EXISTS daily_extreme (
    icao        TEXT NOT NULL,
    local_date  TEXT NOT NULL,
    max_temp    REAL,
    min_temp    REAL,
    obs_time    INTEGER,
    updated_at  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (icao, local_date)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS sync_state (
    icao            TEXT PRIMARY KEY,
    last_attempt_at INTEGER DEFAULT 0,
    last_success_at INTEGER DEFAULT 0,
    last_obs_time   INTEGER DEFAULT 0,
    last_count      INTEGER DEFAULT 0,
    last_error      TEXT,
    fail_streak     INTEGER DEFAULT 0,
    next_retry_at   INTEGER DEFAULT 0,
    backfill_done   INTEGER DEFAULT 0,
    backfill_cursor TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


# ---------------------------------------------------------------- 连接


def get_conn() -> sqlite3.Connection:
    """每个线程一条连接。http.server 是多线程的，不能共用连接。"""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        return conn

    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(config.DB_PATH), timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # journal_mode 是数据库级的持久设置，只在 init_db 里设一次。
    # 每个新连接都执行会去抢排他锁，多线程下会互相等，表现为启动卡住。
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=8000")
    _local.conn = conn
    return conn


def init_db() -> None:
    conn = get_conn()
    conn.execute("PRAGMA journal_mode=WAL")  # 持久设置，只需一次
    conn.executescript(_SCHEMA)
    _migrate_columns()
    conn.execute(
        "INSERT OR REPLACE INTO meta (k, v) VALUES ('schema_version', ?)",
        (str(config.SCHEMA_VERSION),),
    )
    _ensure_default_stations()
    _ensure_hko_station()
    _ensure_hko_history()
    _migrate_station_names()
    checkpoint()


def _ensure_hko_history() -> None:
    """一次性让香港站重跑回填。

    早期版本以为天文台没有历史，把 backfill_done 直接标了 1。后来发现「逐日提取」
    这套按月 JSON 可以拿到每日最高/最低，所以要把它重置一次，让同步线程去补。
    """
    conn = get_conn()
    if conn.execute("SELECT v FROM meta WHERE k = 'hko_history'").fetchone():
        return
    conn.execute(
        "UPDATE sync_state SET backfill_done = 0"
        " WHERE icao IN (SELECT icao FROM station WHERE provider = 'hko')"
    )
    conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('hko_history', '1')")


def _migrate_columns() -> None:
    """老库补列。SQLite 没有 ADD COLUMN IF NOT EXISTS，只能先查 table_info。"""
    conn = get_conn()
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(station)").fetchall()}
    for column, ddl in (
        ("provider", "ALTER TABLE station ADD COLUMN provider TEXT NOT NULL DEFAULT 'metar'"),
        ("place", "ALTER TABLE station ADD COLUMN place TEXT"),
    ):
        if column not in existing:
            conn.execute(ddl)


def _ensure_hko_station() -> None:
    """给已有库补上香港站（一次性）。

    为什么单独做：_ensure_default_stations 只在空库时插入，已有数据的库
    永远不会走那条路。用 meta 标记保证只补一次，用户删掉后不会再被加回来。
    """
    conn = get_conn()
    if conn.execute("SELECT v FROM meta WHERE k = 'hko_added'").fetchone():
        return
    spec = next((s for s in config.DEFAULT_STATIONS if s.get("provider") == "hko"), None)
    if spec is not None and not get_station(spec["icao"]):
        conn.execute(
            "INSERT OR IGNORE INTO station"
            " (icao, name, tz, provider, place, lat, lon, country, enabled, sort_order, created_at)"
            " VALUES (?, ?, ?, 'hko', ?, ?, ?, ?, 1, ?, ?)",
            (spec["icao"], spec["name"], spec.get("tz", config.DEFAULT_TZ),
             spec.get("place"), spec.get("lat"), spec.get("lon"), spec.get("country"),
             spec.get("sort_order", 100), int(time.time())),
        )
    conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('hko_added', '1')")


def _migrate_station_names() -> None:
    """一次性把历史站名统一成英文并去掉 INTL 之类后缀。

    为什么需要迁移：早期版本默认城市用的是中文名（上海 / 广州），后来新增
    的城市却是上游原样英文名（"Shenzhen/Boan Intl"），列表里中英混排、
    还拖着 INTL 尾巴。这里统一按 naming.clean_site_name 洗一遍。

    只跑一次（meta 里打标记），之后用户通过 PUT /api/stations 改的名字
    不会被启动时覆盖 —— 那才是真的会气人。
    """
    conn = get_conn()
    row = conn.execute("SELECT v FROM meta WHERE k = 'names_en'").fetchone()
    if row and row["v"] == "1":
        return

    for station in conn.execute("SELECT icao, name, name_en FROM station").fetchall():
        # name_en 存的是上游原文，优先拿它当清洗源；没有就用当前 name
        raw = station["name_en"] or station["name"] or station["icao"]
        clean = naming.clean_site_name(raw, station["icao"])
        if clean != station["name"] or station["name_en"] is None:
            conn.execute(
                "UPDATE station SET name = ?, name_en = ? WHERE icao = ?",
                (clean, raw, station["icao"]),
            )

    conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('names_en', '1')")


def _ensure_default_stations() -> None:
    conn = get_conn()
    count = conn.execute("SELECT COUNT(*) FROM station").fetchone()[0]
    if count:
        return
    now = int(time.time())
    for spec in config.DEFAULT_STATIONS:
        conn.execute(
            "INSERT OR IGNORE INTO station"
            " (icao, name, tz, provider, place, lat, lon, country, enabled, sort_order, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (spec["icao"], spec["name"], spec.get("tz", config.DEFAULT_TZ),
             spec.get("provider", "metar"), spec.get("place"),
             spec.get("lat"), spec.get("lon"), spec.get("country"),
             spec.get("sort_order", 100), now),
        )


# ---------------------------------------------------------------- 站点


def list_stations(include_disabled: bool = False) -> list[dict]:
    conn = get_conn()
    sql = "SELECT * FROM station"
    if not include_disabled:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY sort_order, icao"
    rows = conn.execute(sql).fetchall()
    out = []
    for index, row in enumerate(rows):
        item = dict(row)
        item["enabled"] = bool(item["enabled"])
        # 颜色按排序位次分配，新增城市自动拿到下一个颜色
        item["color"] = config.COLOR_PALETTE[index % len(config.COLOR_PALETTE)]
        out.append(item)
    return out


def get_station(icao: str) -> dict | None:
    row = get_conn().execute(
        "SELECT * FROM station WHERE icao = ?", (icao.upper(),)
    ).fetchone()
    if row is None:
        return None
    item = dict(row)
    item["enabled"] = bool(item["enabled"])
    return item


def upsert_station(icao: str, **fields) -> None:
    """新增或更新站点。只更新传进来的字段。"""
    conn = get_conn()
    icao = icao.upper()
    exists = get_station(icao) is not None

    if not exists:
        conn.execute(
            "INSERT INTO station (icao, name, tz, enabled, sort_order, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                icao,
                fields.get("name") or icao,
                fields.get("tz") or config.DEFAULT_TZ,
                1 if fields.get("enabled", True) else 0,
                int(fields.get("sort_order", 100)),
                int(time.time()),
            ),
        )
        exists = True

    allowed = ("name", "name_en", "lat", "lon", "elev", "country", "state",
               "tz", "enabled", "sort_order", "note", "provider", "place")
    updates = {k: v for k, v in fields.items() if k in allowed}
    if updates:
        if "enabled" in updates:
            updates["enabled"] = 1 if updates["enabled"] else 0
        assignments = ", ".join(f"{key} = ?" for key in updates)
        conn.execute(
            f"UPDATE station SET {assignments} WHERE icao = ?",
            (*updates.values(), icao),
        )


def delete_station(icao: str, purge: bool = False) -> None:
    """默认只删站点行，保留历史观测 —— 重新添加该站时历史自动恢复。"""
    conn = get_conn()
    icao = icao.upper()
    conn.execute("DELETE FROM station WHERE icao = ?", (icao,))
    if purge:
        conn.execute("DELETE FROM observation WHERE icao = ?", (icao,))
    conn.execute("DELETE FROM sync_state WHERE icao = ?", (icao,))


def rebuild_local_dates(icao: str) -> int:
    """改了站点时区后必须调用，否则历史日切会错位。"""
    conn = get_conn()
    station = get_station(icao)
    if station is None:
        return 0
    tz = station["tz"]
    rows = conn.execute(
        "SELECT obs_time, local_date FROM observation WHERE icao = ?", (icao.upper(),)
    ).fetchall()
    changed = 0
    for row in rows:
        want = to_local_date(row["obs_time"], tz)
        if want != row["local_date"]:
            conn.execute(
                "UPDATE observation SET local_date = ? WHERE icao = ? AND obs_time = ?",
                (want, icao.upper(), row["obs_time"]),
            )
            changed += 1
    return changed


# ---------------------------------------------------------------- 观测写入


def insert_observations(records: list[dict], tz_map: dict[str, str]) -> int:
    """写入观测，返回真正新增的条数。重复记录被主键静默忽略。"""
    if not records:
        return 0
    conn = get_conn()
    now = int(time.time())
    inserted = 0
    conn.execute("BEGIN")
    try:
        for rec in records:
            icao = rec["icao"]
            tz = tz_map.get(icao) or config.DEFAULT_TZ
            local_date = to_local_date(rec["obs_time"], tz)
            cur = conn.execute(
                "INSERT OR IGNORE INTO observation"
                " (icao, obs_time, local_date, temp_c, dewp_c, wdir, wspd, visib, altim, raw_ob, fetched_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    icao,
                    rec["obs_time"],
                    local_date,
                    rec.get("temp"),
                    rec.get("dewp"),
                    rec.get("wdir"),
                    rec.get("wspd"),
                    rec.get("visib"),
                    rec.get("altim"),
                    rec.get("raw"),
                    now,
                ),
            )
            inserted += cur.rowcount
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return inserted


# ---------------------------------------------------------------- 官方日极值


def upsert_daily_extreme(icao: str, local_date: str, max_temp: float | None,
                         min_temp: float | None, obs_time: int) -> None:
    """写入官方当日极值（覆盖式，官方会随时间不断上修）。"""
    conn = get_conn()
    conn.execute(
        "INSERT INTO daily_extreme (icao, local_date, max_temp, min_temp, obs_time, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(icao, local_date) DO UPDATE SET"
        "   max_temp = excluded.max_temp,"
        "   min_temp = excluded.min_temp,"
        "   obs_time = excluded.obs_time,"
        "   updated_at = excluded.updated_at",
        (icao.upper(), local_date, max_temp, min_temp, int(obs_time), int(time.time())),
    )


def query_daily_extremes(icaos: list[str], dates: list[str]) -> dict[tuple[str, str], dict]:
    """取 (icao, date) -> 官方极值。给每日聚合做覆盖用。"""
    if not icaos or not dates:
        return {}
    conn = get_conn()
    marks_i = ",".join("?" for _ in icaos)
    marks_d = ",".join("?" for _ in dates)
    rows = conn.execute(
        f"SELECT icao, local_date, max_temp, min_temp, obs_time, updated_at"
        f" FROM daily_extreme WHERE icao IN ({marks_i}) AND local_date IN ({marks_d})",
        (*icaos, *dates),
    ).fetchall()
    return {(row["icao"], row["local_date"]): dict(row) for row in rows}


# ---------------------------------------------------------------- 查询


def recent_obs_times(icao: str, limit: int = 48) -> list[int]:
    """最近 limit 条观测的时间戳（降序）。给自适应刷新间隔算报文节奏用。"""
    rows = get_conn().execute(
        "SELECT obs_time FROM observation WHERE icao = ? ORDER BY obs_time DESC LIMIT ?",
        (icao.upper(), int(limit)),
    ).fetchall()
    return [row["obs_time"] for row in rows]


def query_series(icaos: list[str], from_ts: int, to_ts: int) -> list[dict]:
    """折线数据。只取有温度的观测，按站点 + 时间升序。"""
    if not icaos:
        return []
    conn = get_conn()
    marks = ",".join("?" for _ in icaos)
    rows = conn.execute(
        f"SELECT icao, obs_time, temp_c, dewp_c, wdir, wspd, raw_ob"
        f" FROM observation"
        f" WHERE icao IN ({marks}) AND obs_time >= ? AND obs_time <= ?"
        f"   AND temp_c IS NOT NULL"
        f" ORDER BY icao, obs_time",
        (*icaos, from_ts, to_ts),
    ).fetchall()
    return [dict(row) for row in rows]


def query_daily_rows(icaos: list[str], dates: list[str]) -> list[dict]:
    """每日聚合的原始观测（含温度为空的行，用于统计覆盖率）。"""
    if not icaos or not dates:
        return []
    conn = get_conn()
    marks_i = ",".join("?" for _ in icaos)
    marks_d = ",".join("?" for _ in dates)
    rows = conn.execute(
        f"SELECT icao, local_date, obs_time, temp_c FROM observation"
        f" WHERE icao IN ({marks_i}) AND local_date IN ({marks_d})"
        f" ORDER BY icao, local_date, obs_time",
        (*icaos, *dates),
    ).fetchall()
    return [dict(row) for row in rows]


def query_observations(icao: str, limit: int = 20, offset: int = 0) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT obs_time, temp_c, dewp_c, wdir, wspd, visib, altim, raw_ob"
        " FROM observation WHERE icao = ? ORDER BY obs_time DESC LIMIT ? OFFSET ?",
        (icao.upper(), limit, offset),
    ).fetchall()
    return [dict(row) for row in rows]


def newest_observation(icao: str) -> dict | None:
    row = get_conn().execute(
        "SELECT * FROM observation WHERE icao = ? ORDER BY obs_time DESC LIMIT 1",
        (icao.upper(),),
    ).fetchone()
    return dict(row) if row else None


def obs_stats(icao: str | None = None) -> dict:
    conn = get_conn()
    if icao:
        row = conn.execute(
            "SELECT COUNT(*) AS n, MIN(obs_time) AS oldest, MAX(obs_time) AS newest"
            " FROM observation WHERE icao = ?",
            (icao.upper(),),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) AS n, MIN(obs_time) AS oldest, MAX(obs_time) AS newest FROM observation"
        ).fetchone()
    return {
        "count": row["n"] or 0,
        "oldest": row["oldest"],
        "newest": row["newest"],
    }


def total_observation_count() -> int:
    return get_conn().execute("SELECT COUNT(*) FROM observation").fetchone()[0]


def export_rows(icao: str | None, from_ts: int, to_ts: int) -> list[dict]:
    conn = get_conn()
    sql = (
        "SELECT icao, obs_time, local_date, temp_c, dewp_c, wdir, wspd, visib, altim, raw_ob"
        " FROM observation WHERE obs_time >= ? AND obs_time <= ?"
    )
    params: list = [from_ts, to_ts]
    if icao:
        sql += " AND icao = ?"
        params.append(icao.upper())
    sql += " ORDER BY icao, obs_time"
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


# ---------------------------------------------------------------- 同步状态


def get_sync_state(icao: str) -> dict:
    row = get_conn().execute(
        "SELECT * FROM sync_state WHERE icao = ?", (icao.upper(),)
    ).fetchone()
    if row is None:
        return {
            "icao": icao.upper(),
            "last_attempt_at": 0,
            "last_success_at": 0,
            "last_obs_time": 0,
            "last_count": 0,
            "last_error": None,
            "fail_streak": 0,
            "next_retry_at": 0,
            "backfill_done": 0,
            "backfill_cursor": None,
        }
    return dict(row)


def set_sync_state(icao: str, **fields) -> None:
    conn = get_conn()
    icao = icao.upper()
    conn.execute("INSERT OR IGNORE INTO sync_state (icao) VALUES (?)", (icao,))
    if not fields:
        return
    assignments = ", ".join(f"{key} = ?" for key in fields)
    conn.execute(
        f"UPDATE sync_state SET {assignments} WHERE icao = ?",
        (*fields.values(), icao),
    )


def hot_backup(target) -> None:
    """热备份：服务运行中也能安全复制出一个完整库。"""
    get_conn().execute("VACUUM INTO ?", (str(target),))


# ---------------------------------------------------------------- 归档维护


def db_size_bytes() -> int:
    """库文件实际占用（含 WAL）。"""
    total = 0
    for suffix in ("", "-wal", "-shm"):
        path = config.DB_PATH.with_name(config.DB_PATH.name + suffix)
        try:
            if path.exists():
                total += path.stat().st_size
        except OSError:
            pass
    return total


def checkpoint() -> None:
    """把 WAL 落回主库并截断，避免界面上的"库体积"被 WAL 虚高。

    TRUNCATE 需要没有其他连接持有读事务，拿不到就退回 PASSIVE（至少把数据落盘）。
    """
    conn = get_conn()
    for mode in ("TRUNCATE", "PASSIVE"):
        try:
            conn.execute(f"PRAGMA wal_checkpoint({mode})")
            return
        except sqlite3.Error:
            continue


def prune_observations(keep_days: int, now: int | None = None) -> dict:
    """删掉超出保留期的观测。keep_days <= 0 表示永久保留，直接不动。"""
    if not keep_days or keep_days <= 0:
        return {"deleted": 0, "kept": total_observation_count(), "skipped": True}

    cutoff = (now or int(time.time())) - keep_days * 86400
    conn = get_conn()
    before = total_observation_count()
    conn.execute("DELETE FROM observation WHERE obs_time < ?", (cutoff,))
    # 官方极值表和观测同寿命，否则会留下没有曲线的孤立日线
    conn.execute("DELETE FROM daily_extreme WHERE obs_time < ?", (cutoff,))
    deleted = before - total_observation_count()

    # 只删不回收等于没省空间：删完立刻 VACUUM 并把 WAL 截断，界面上的体积才会真的变小。
    # 库本身有保留期封顶（几 MB），VACUUM 是毫秒级操作，所以只要执行清理就整理一次。
    vacuumed = False
    try:
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        vacuumed = True
    except sqlite3.Error:
        pass

    return {
        "deleted": deleted,
        "kept": before - deleted,
        "cutoff": cutoff,
        "keepDays": keep_days,
        "vacuumed": vacuumed,
        "sizeBytes": db_size_bytes(),
        "skipped": False,
    }


def archive_summary(keep_days: int) -> dict:
    """给界面用的归档概况：条数、体积、增长速率、保留策略。

    单条平均字节数用实际库体积除以条数算出来，而不是写死常量 ——
    这样界面上的"每日增量 / 封顶体积"始终是真实值。
    """
    stats = obs_stats()
    stations = max(1, len(list_stations()))
    size = db_size_bytes()
    per_obs = (size / stats["count"]) if stats["count"] else config.BYTES_PER_OBS_ESTIMATE
    # 每日增量按「归档总量 ÷ 覆盖天数」算，不按站点数 × 48 估——
    # 香港一天 140+ 条、METAR 一天 48 条，一刀切两头都不准
    span_days = 1.0
    if stats["oldest"] and stats["newest"] and stats["newest"] > stats["oldest"]:
        span_days = max(1.0, (stats["newest"] - stats["oldest"]) / 86400)
    per_day = int(stats["count"] / span_days * per_obs)
    return {
        "count": stats["count"],
        "oldest": stats["oldest"],
        "newest": stats["newest"],
        "sizeBytes": size,
        "bytesPerObs": int(per_obs),
        "keepDays": keep_days,
        "stationCount": stations,
        "bytesPerDayEstimate": per_day,
        "bytesPerYearEstimate": per_day * 365,
        "boundedBytesEstimate": (keep_days * per_day) if keep_days else None,
    }


def meta_get(key: str, default: str | None = None) -> str | None:
    row = get_conn().execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
    return row["v"] if row else default


def meta_set(key: str, value: str) -> None:
    get_conn().execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)", (key, str(value)))


def keep_days_setting() -> int:
    """归档保留天数（运行时可改）。config 里的是默认值，界面上通过
    /api/archive/prune 改的值存在 meta：后台自动清理和接口展示都以这里为准，
    否则界面改成 365、后台仍按 90 清，第二天数据就被悄悄删掉。"""
    value = meta_get("keep_days")
    try:
        return max(0, int(value)) if value is not None else config.ARCHIVE_KEEP_DAYS
    except ValueError:
        return config.ARCHIVE_KEEP_DAYS
