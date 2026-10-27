"""接口层：只懂路由与序列化。

约定（改接口前先读）：
  - 成功 {"ok": true, "serverTime": <epoch>, "data": ...}
  - 失败 {"ok": false, "error": {"code": "...", "message": "..."}}
  - 时间一律 epoch 秒（UTC），前端乘 1000 即可
  - 时序一律 [[epoch, temp], ...] 且按时间升序，可直接喂 SVG
  - 变更类接口同时接受 JSON body 和 query 参数（Windows 下 curl 打引号很痛）
"""

from __future__ import annotations

import csv
import io
import json
import math
import platform
import time
from datetime import datetime, timedelta, timezone

from . import config, daily, db, source, tzutil
from .tzutil import day_start_epoch, next_day_start, now_ts, to_local_date

_cache: dict[str, tuple[float, int, dict]] = {}


class Raw:
    """非 JSON 响应（CSV / JSONL 导出）。"""

    def __init__(self, text: str, content_type: str = "text/plain; charset=utf-8",
                 headers: dict | None = None) -> None:
        self.text = text
        self.content_type = content_type
        self.headers = headers or {}


def clear_cache() -> None:
    _cache.clear()


def _cached(key: str, ttl: int, producer):
    stamp = time.time()
    # 过期即淘汰，不能只在读时判 TTL：series 的键带时间戳，上游断更或
    # --no-sync 时没有 clear_cache 兜底，旧条目会无限堆积（payload 很大）
    for stale in [k for k, v in _cache.items() if stamp - v[0] >= v[1]]:
        del _cache[stale]
    hit = _cache.get(key)
    if hit and stamp - hit[0] < hit[1]:
        return hit[2]
    value = producer()
    _cache[key] = (stamp, ttl, value)
    return value


def ok(data) -> dict:
    return {"ok": True, "serverTime": now_ts(), "data": data}


def bad(code: str, message: str) -> dict:
    return {"ok": False, "error": {"code": code, "message": message}}


# ---------------------------------------------------------------- 参数工具


def _param(query: dict, body: dict | None, key: str, default=None):
    if body and key in body and body[key] is not None:
        return body[key]
    value = query.get(key)
    if value is None or value == "":
        return default
    return value


def _as_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _stations_map(include_disabled: bool = True) -> dict[str, dict]:
    return {item["icao"]: item for item in db.list_stations(include_disabled=include_disabled)}


def _resolve_icaos(query: dict, body: dict | None = None) -> list[str]:
    raw = _param(query, body, "icao") or _param(query, body, "icaos") or "all"
    if isinstance(raw, (list, tuple)):
        raw = ",".join(str(item) for item in raw)
    raw = str(raw).strip()
    if raw.lower() in ("", "all", "*"):
        return [item["icao"] for item in db.list_stations()]
    return [part.strip().upper() for part in raw.split(",") if part.strip()]


def _axis_tz(icaos: list[str]) -> str:
    """时间轴用哪个时区。当前所有城市都在东八区，取第一个站即可。"""
    stations = _stations_map()
    for icao in icaos:
        station = stations.get(icao)
        if station:
            return station.get("tz") or config.DEFAULT_TZ
    return config.DEFAULT_TZ


def _next_date(date_str: str) -> str:
    year, month, day = (int(part) for part in date_str.split("-"))
    return (datetime(year, month, day) + timedelta(days=1)).strftime("%Y-%m-%d")


def _dates_between(from_ts: int, to_ts: int, tz_name: str) -> list[str]:
    """区间覆盖的自然日列表，用于查官方极值。"""
    return [day["date"] for day in _days_between(from_ts, to_ts, tz_name)]


def _days_between(from_ts: int, to_ts: int, tz_name: str) -> list[dict]:
    out: list[dict] = []
    date_str = to_local_date(from_ts, tz_name)
    guard = 0
    while guard < 400:
        start = day_start_epoch(date_str, tz_name)
        if start > to_ts:
            break
        out.append({"date": date_str, "start": start,
                    "end": next_day_start(date_str, tz_name)})
        date_str = _next_date(date_str)
        guard += 1
    return out


def _resolve_range(query: dict, body: dict | None, tz_name: str) -> tuple[int, int, str]:
    range_key = str(_param(query, body, "range", "") or "").lower()
    now = now_ts()

    if range_key == "today":
        today = to_local_date(now, tz_name)
        # 横轴铺满一整天（00:00 → 23:59:59），曲线本身只画到最新一条观测
        return day_start_epoch(today, tz_name), next_day_start(today, tz_name) - 1, range_key

    if range_key in config.RANGE_PRESETS:
        from_ts = now - config.RANGE_PRESETS[range_key] * 3600
        return from_ts, now, range_key

    from_ts = _as_int(_param(query, body, "from"), 0)
    to_ts = _as_int(_param(query, body, "to"), 0)
    if from_ts and to_ts:
        return from_ts, to_ts, "custom"
    return now - config.RANGE_PRESETS["48h"] * 3600, now, "48h"


# ---------------------------------------------------------------- 处理器


def _health(query, body) -> tuple[int, dict]:
    worker = None
    try:
        from .sync import get_worker

        worker = get_worker()
    except Exception:
        pass
    stats = db.obs_stats()
    return 200, ok({
        "python": platform.python_version(),
        "schemaVersion": config.SCHEMA_VERSION,
        "tzDegraded": tzutil.is_degraded(),
        "dbPath": str(config.DB_PATH),
        "archive": stats,
        "archiveSizeBytes": db.db_size_bytes(),
        "keepDays": db.keep_days_setting(),
        "stationCount": len(db.list_stations()),
        "sync": worker.snapshot() if worker else {"running": False, "phase": "off"},
    })


def _stations(query, body) -> tuple[int, dict]:
    include_disabled = _as_bool(_param(query, body, "all"), False)

    def produce():
        stations = db.list_stations(include_disabled=include_disabled)
        icaos = [item["icao"] for item in stations]
        now = now_ts()

        date_axis = _axis_tz(icaos)
        now_date = to_local_date(now, date_axis)
        yesterday = _prev_date(now_date)

        rows = db.query_daily_rows(icaos, [yesterday, now_date]) if icaos else []
        extremes = db.query_daily_extremes(icaos, [yesterday, now_date]) if icaos else {}
        tz_map = {item["icao"]: item.get("tz") or config.DEFAULT_TZ for item in stations}
        aggregates = daily.build_daily(rows, tz_map, now, extremes)
        by_key = {(item["icao"], item["date"]): item for item in aggregates}

        out = []
        for station in stations:
            icao = station["icao"]
            tz = station.get("tz") or config.DEFAULT_TZ
            latest = db.newest_observation(icao)
            stats = db.obs_stats(icao)
            sync_state = db.get_sync_state(icao)

            latest_payload = None
            if latest:
                latest_payload = {
                    "obsTime": latest["obs_time"],
                    "temp": latest["temp_c"],
                    "dewp": latest["dewp_c"],
                    "wdir": latest["wdir"],
                    "wspd": latest["wspd"],
                    "rawOb": latest["raw_ob"],
                    "ageSec": max(0, now - latest["obs_time"]),
                }

            out.append({
                "icao": icao,
                "name": station["name"],
                # 上游 site 原文，仅供核对/导出；故意不叫 nameEn —— 它带着
                # "… Intl" 后缀，任何界面直接渲染都会把刚洗掉的尾巴加回来。
                "siteRaw": station.get("name_en"),
                "lat": station.get("lat"),
                "lon": station.get("lon"),
                "country": station.get("country"),
                "provider": station.get("provider") or "metar",
                "place": station.get("place"),
                "tz": tz,
                "enabled": station["enabled"],
                "sortOrder": station.get("sort_order"),
                "color": station["color"],
                "latest": latest_payload,
                "today": by_key.get((icao, to_local_date(now, tz))),
                "yesterday": by_key.get((icao, _prev_date(to_local_date(now, tz)))),
                "archive": {
                    "obsCount": stats["count"],
                    "oldest": stats["oldest"],
                    "newest": stats["newest"],
                },
                "sync": {
                    "lastSuccessAt": sync_state["last_success_at"],
                    "lastError": sync_state["last_error"],
                    "backfillDone": bool(sync_state["backfill_done"]),
                    "failStreak": sync_state["fail_streak"],
                },
            })
        return out

    ttl = config.STATIONS_CACHE_TTL
    return 200, ok(_cached(f"stations:{include_disabled}", ttl, produce))


def _prev_date(date_str: str) -> str:
    year, month, day = (int(part) for part in date_str.split("-"))
    return (datetime(year, month, day) - timedelta(days=1)).strftime("%Y-%m-%d")


def _station_info(query, body) -> tuple[int, dict]:
    icao = str(_param(query, body, "icao", "") or "").strip().upper()
    if len(icao) != 4 or not icao.isalnum():
        return 400, bad("INVALID_ICAO", "ICAO 代码应为 4 位字母数字，例如 ZBAA")
    info, status = source.fetch_station_info(icao)
    if info is None:
        # stationinfo 查不到时上游返回空 body；合法 ICAO 一定有记录，
        # 所以 empty 和 not_found 都按"站号无效"处理，只有真网络错误才是 503。
        if status in ("not_found", "empty"):
            return 404, bad("INVALID_ICAO", f"查不到站点 {icao}，请确认 ICAO 代码")
        return 503, bad("UPSTREAM_UNAVAILABLE", f"校验 {icao} 时上游不可用（{status}）")
    return 200, ok(info)


def _create_station(query, body) -> tuple[int, dict]:
    provider = str(_param(query, body, "provider", "metar") or "metar").strip().lower()
    if provider != "metar":
        return _create_hko_station(query, body)

    icao = str(_param(query, body, "icao", "") or "").strip().upper()
    if len(icao) != 4 or not icao.isalnum():
        return 400, bad("INVALID_ICAO", "ICAO 代码应为 4 位字母数字，例如 ZBAA")
    if db.get_station(icao):
        return 409, bad("ALREADY_EXISTS", f"{icao} 已在城市列表中")

    force = _as_bool(_param(query, body, "force"), False)
    info, status = source.fetch_station_info(icao)
    if info is None and not force:
        if status in ("not_found", "empty"):
            return 400, bad("INVALID_ICAO", f"查不到站点 {icao}，请确认 ICAO 代码（可加 force=1 强制录入）")
        return 503, bad("UPSTREAM_UNAVAILABLE",
                        f"校验 {icao} 时上游不可用（{status}），可加 force=1 强制录入")

    # 站名默认取上游 site 的清洗版（英文、无 INTL 尾巴）；
    # 显式传 name= 时按原样保存，方便自定义成任何想要的名字。
    name = _param(query, body, "name") or (info or {}).get("site_name") or icao
    tz = _param(query, body, "tz") or config.DEFAULT_TZ

    max_order = max((item.get("sort_order") or 0) for item in db.list_stations(True)) if db.list_stations(True) else 0
    db.upsert_station(
        icao,
        name=name,
        name_en=(info or {}).get("site"),
        lat=(info or {}).get("lat"),
        lon=(info or {}).get("lon"),
        elev=(info or {}).get("elev"),
        country=(info or {}).get("country"),
        state=(info or {}).get("state"),
        tz=tz,
        enabled=True,
        sort_order=_as_int(_param(query, body, "sort_order"), max_order + 10),
        note=_param(query, body, "note"),
    )
    clear_cache()

    try:
        from .sync import get_worker

        worker = get_worker()
        if worker:
            worker.schedule_backfill(icao)
    except Exception:
        pass

    payload = {
        "icao": icao,
        "name": name,
        "created": True,
        "filledFrom": "stationinfo" if info else "manual",
        "hint": f"正在后台回填近 {config.BACKFILL_DAYS} 天历史，约 20 秒后可在 /api/daily 查看",
    }
    if info is None:
        payload["warning"] = "上游校验失败，站点信息可能不完整"
    return 201, ok(payload)


def _create_hko_station(query, body) -> tuple[int, dict]:
    """新增香港站。**只采用天文台总部**（尖沙咀）这一个站。

    为什么不让选其他自动站：香港的官方气温记录就是天文台总部，赤鱲角（机场）、
    跑马地、启德跑道公园这些站点的读数彼此能差 1–3°C，混着用等于口径不统一。
    所以 place 固定为 HK Observatory，传别的直接拒绝，而不是悄悄忽略。
    """
    code = str(_param(query, body, "icao", "") or "HKO").strip().upper()
    if not (3 <= len(code) <= 4) or not code.isalnum():
        return 400, bad("INVALID_CODE", "香港站代码用 3 位字母，默认 HKO")
    if db.get_station(code):
        return 409, bad("ALREADY_EXISTS", f"{code} 已在城市列表中")

    wanted = str(_param(query, body, "place") or config.HKO_DEFAULT_PLACE).strip()
    if wanted.lower() != config.HKO_DEFAULT_PLACE.lower():
        return 400, bad(
            "PLACE_NOT_ALLOWED",
            f"香港只采用天文台总部站（{config.HKO_DEFAULT_PLACE}），不接受 {wanted}",
        )
    matched = config.HKO_DEFAULT_PLACE

    name = _param(query, body, "name") or "Hong Kong"
    max_order = max((item.get("sort_order") or 0 for item in db.list_stations(True)), default=0)
    db.upsert_station(
        code,
        name=name,
        tz=str(_param(query, body, "tz") or "Asia/Hong_Kong"),
        provider="hko",
        place=matched,
        lat=22.3019,
        lon=114.1742,
        country="HK",
        enabled=True,
        sort_order=_as_int(_param(query, body, "sort_order"), max_order + 10),
        note=_param(query, body, "note"),
    )
    clear_cache()
    return 201, ok({
        "icao": code,
        "name": name,
        "provider": "hko",
        "place": matched,
        "created": True,
        "filledFrom": "hko",
        "hint": "已回填近 21 天每日最高/最低；逐时曲线从下一轮同步开始积累",
    })


def _update_station(query, body, icao: str) -> tuple[int, dict]:
    station = db.get_station(icao)
    if station is None:
        return 404, bad("NOT_FOUND", f"{icao} 不在城市列表中")

    fields: dict = {}
    for key in ("name", "tz", "sort_order", "note"):
        value = _param(query, body, key)
        if value is not None:
            fields[key] = value
    if _param(query, body, "enabled") is not None:
        fields["enabled"] = _as_bool(_param(query, body, "enabled"), True)
    if not fields:
        return 400, bad("NO_FIELDS", "没有可更新的字段")

    tz_changed = "tz" in fields and fields["tz"] != station.get("tz")
    db.upsert_station(icao, **fields)
    if tz_changed:
        db.rebuild_local_dates(icao)
    clear_cache()
    return 200, ok({"icao": icao, "updated": sorted(fields.keys()),
                    "tzRebuilt": tz_changed})


def _delete_station(query, body, icao: str) -> tuple[int, dict]:
    station = db.get_station(icao)
    if station is None:
        return 404, bad("NOT_FOUND", f"{icao} 不在城市列表中")
    purge = _as_bool(_param(query, body, "purge"), False)
    db.delete_station(icao, purge=purge)
    clear_cache()
    return 200, ok({"icao": icao, "deleted": True, "purged": purge,
                    "hint": "默认保留历史观测，重新添加该站可恢复" if not purge else "历史观测已一并删除"})


def _series(query, body) -> tuple[int, dict]:
    icaos = _resolve_icaos(query, body)
    if not icaos:
        return 200, ok({"unit": "C", "series": [], "days": [], "scale": {"tempMin": 0, "tempMax": 30}})

    tz_name = _axis_tz(icaos)
    from_ts, to_ts, range_key = _resolve_range(query, body, tz_name)

    def produce():
        stations = _stations_map()
        rows = db.query_series(icaos, from_ts, to_ts)
        buckets: dict[str, list[dict]] = {icao: [] for icao in icaos}
        for row in rows:
            buckets.setdefault(row["icao"], []).append(row)

        # 官方极值（目前只有香港）要参与"区间最高"的计算：
        # 我们每 5 分钟采一次，官方逐分钟在算，区间最高完全可能比我们采到的高。
        span_dates = _dates_between(from_ts, to_ts, tz_name)
        extremes = db.query_daily_extremes(icaos, span_dates) if span_dates else {}

        series = []
        all_temps: list[float] = []
        for icao in icaos:
            station = stations.get(icao) or {"name": icao, "color": config.COLOR_PALETTE[0]}
            rows_for = buckets.get(icao, [])
            # 点结构 [epoch, temp, dewp, wdir, wspd] —— 前两位画线，后三位喂 hover 提示
            points = [
                [row["obs_time"], row["temp_c"], row["dewp_c"], row["wdir"], row["wspd"]]
                for row in rows_for if row["temp_c"] is not None
            ]
            times = [row["obs_time"] for row in rows_for]
            gaps = daily.detect_gaps(times)
            interval = daily.median_interval(times)

            temps = [point[1] for point in points]
            peak = max(temps) if temps else None
            peak_time = points[temps.index(peak)][0] if temps else None
            peak_source = "sampled"

            official_max = max(
                (item["max_temp"] for item in extremes.values()
                 if item.get("icao") == icao and item.get("max_temp") is not None),
                default=None,
            )
            if official_max is not None and (peak is None or official_max > peak):
                peak = official_max
                peak_time = None          # 官方只给数值，不给出现时刻
                peak_source = "official"

            if peak is not None:
                all_temps.append(peak)
            all_temps.extend(temps)
            series.append({
                "icao": icao,
                "name": station.get("name") or icao,
                "color": station.get("color"),
                "provider": station.get("provider") or "metar",
                "points": points,
                "gaps": gaps,
                "sampleInterval": interval,
                "obsCount": len(points),
                "first": points[0][0] if points else None,
                "last": points[-1][0] if points else None,
                "rangeMax": peak,
                "rangeMaxSource": peak_source,
                "rangeMin": min(temps) if temps else None,
                "rangeMaxTime": peak_time,
            })

        if all_temps:
            # floor/ceil 而不是 int()：负温时 int 向零截断，上下界会少扩一格
            scale = {"tempMin": math.floor(min(all_temps)) - 1,
                     "tempMax": math.ceil(max(all_temps)) + 1}
        else:
            scale = {"tempMin": 0, "tempMax": 30}

        return {
            "unit": "C",
            "tz": tz_name,
            "range": range_key,
            "from": from_ts,
            "to": to_ts,
            "now": now_ts(),
            "days": _days_between(from_ts, to_ts, tz_name),
            "series": series,
            "scale": scale,
        }

    # 键里把时间量化到分钟：24h/48h/7d/21d 的 from/to 每秒都在变，
    # 不量化这些档位永远命不中缓存（TTL 形同虚设）。命中方最多看到
    # 右缘不到 1 分钟的旧窗口，对 30 分钟级的观测无感。
    key = f"series:{','.join(icaos)}:{from_ts // 60}:{to_ts // 60}"
    return 200, ok(_cached(key, config.SERIES_CACHE_TTL, produce))


def _daily(query, body) -> tuple[int, dict]:
    icaos = _resolve_icaos(query, body)
    if not icaos:
        return 200, ok({"tz": config.DEFAULT_TZ, "rows": []})

    tz_name = _axis_tz(icaos)
    days = max(1, min(120, _as_int(_param(query, body, "days"), 14)))
    dates = tzutil.recent_dates(tz_name, days)

    def produce():
        stations = _stations_map()
        tz_map = {icao: (stations.get(icao) or {}).get("tz") or config.DEFAULT_TZ for icao in icaos}
        rows = db.query_daily_rows(icaos, dates)
        extremes = db.query_daily_extremes(icaos, dates)
        aggregates = daily.build_daily(rows, tz_map, now_ts(), extremes)
        for item in aggregates:
            station = stations.get(item["icao"]) or {}
            item["name"] = station.get("name") or item["icao"]
            item["color"] = station.get("color")
        return {"tz": tz_name, "days": days, "dates": dates, "rows": aggregates}

    key = f"daily:{','.join(icaos)}:{days}:{dates[-1] if dates else ''}"
    return 200, ok(_cached(key, config.DAILY_CACHE_TTL, produce))


def _observations(query, body) -> tuple[int, dict]:
    icaos = _resolve_icaos(query, body)
    if not icaos:
        return 400, bad("NO_STATION", "缺少 icao 参数")
    icao = icaos[0]
    limit = max(1, min(500, _as_int(_param(query, body, "limit"), 20)))
    offset = max(0, _as_int(_param(query, body, "offset"), 0))
    rows = db.query_observations(icao, limit=limit, offset=offset)
    station = _stations_map().get(icao) or {}
    tz_name = station.get("tz") or config.DEFAULT_TZ
    out = [{
        "obsTime": row["obs_time"],
        "timeLabel": datetime.fromtimestamp(row["obs_time"], tzutil.get_tz(tz_name)).strftime("%m-%d %H:%M"),
        "temp": row["temp_c"],
        "dewp": row["dewp_c"],
        "wdir": row["wdir"],
        "wspd": row["wspd"],
        "visib": row["visib"],
        "altim": row["altim"],
        "rawOb": row["raw_ob"],
    } for row in rows]
    return 200, ok({"icao": icao, "tz": tz_name, "limit": limit, "offset": offset, "rows": out})


def _active_station(query, body) -> tuple[int, dict]:
    """当前打开的城市。GET 读取，带 icao 时写入。

    后台轮询只同步这个城市 —— 页面看哪个，就只更新哪个，其余按需切换时再拉。
    """
    from .sync import get_active_icao, set_active_icao

    icao = str(_param(query, body, "icao", "") or "").strip().upper()
    if not icao:
        return 200, ok({"icao": get_active_icao()})
    if db.get_station(icao) is None:
        return 404, bad("NOT_FOUND", f"{icao} 不在城市列表中")
    set_active_icao(icao)
    return 200, ok({"icao": icao, "hint": "后台轮询只同步这个城市"})


def _sync(query, body) -> tuple[int, dict]:
    from .sync import get_active_icao, get_worker

    worker = get_worker()
    if worker is None:
        return 503, bad("NO_WORKER", "同步线程未启动（可能以 --no-sync 运行）")
    icao = str(_param(query, body, "icao", "") or "").strip().upper() or None
    if icao is None:
        # 没指定就同步"当前打开的城市"，和后台轮询保持一致
        icao = get_active_icao()
    # 手动刷新也回看 24 小时：用户点刷新往往正是因为看到曲线断了
    hours = _as_int(_param(query, body, "hours"), config.LOOKBACK_HOURS)
    force = _as_bool(_param(query, body, "force"), False)
    result = worker.sync_all(only=icao, hours=hours, force=force, reason="manual", wait=8)
    clear_cache()
    if not result.get("ok") and result.get("reason") == "busy":
        return 202, ok({"queued": False, "reason": "busy",
                        "hint": "已有同步在进行，稍后再试"})
    return 200, ok(result)


def _export(query, body) -> tuple[int, Raw]:
    icaos = _resolve_icaos(query, body)
    icao = icaos[0] if len(icaos) == 1 else None
    fmt = str(_param(query, body, "format", "csv") or "csv").lower()
    download = _as_bool(_param(query, body, "download"), False)
    days = _as_int(_param(query, body, "days"), 0)
    now = now_ts()

    from_ts = _as_int(_param(query, body, "from"), 0) or 0
    if days > 0:
        from_ts = now - days * 86400
    to_ts = _as_int(_param(query, body, "to"), 0) or now
    rows = db.export_rows(icao, from_ts, to_ts)

    stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y%m%d")
    scope = icao or "all"
    headers = {"Content-Disposition": f'attachment; filename="weather-obs-{scope}-{stamp}.{"jsonl" if fmt in ("jsonl", "json") else "csv"}"'} if download else {}

    if fmt in ("jsonl", "json"):
        lines = []
        for row in rows:
            row = dict(row)
            row["isoUtc"] = datetime.fromtimestamp(row["obs_time"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            lines.append(json.dumps(row, ensure_ascii=False))
        return 200, Raw("\n".join(lines) + "\n", "application/x-ndjson; charset=utf-8", headers)

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["icao", "obs_time", "iso_utc", "local_date", "temp_c", "dewp_c",
                     "wdir", "wspd", "visib", "altim", "raw_ob"])
    for row in rows:
        writer.writerow([
            row["icao"], row["obs_time"],
            datetime.fromtimestamp(row["obs_time"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            row["local_date"], row["temp_c"], row["dewp_c"], row["wdir"], row["wspd"],
            row["visib"], row["altim"], row["raw_ob"],
        ])
    return 200, Raw(buffer.getvalue(), "text/csv; charset=utf-8", headers)


# ---------------------------------------------------------------- 归档维护


def _archive(query, body) -> tuple[int, dict]:
    db.checkpoint()  # 先把 WAL 截断，下面的体积才是真实占用
    summary = db.archive_summary(db.keep_days_setting())
    summary["lastPruneAt"] = _as_int(db.meta_get("last_prune_at", "0"), 0)
    summary["dbPath"] = str(config.DB_PATH)
    return 200, ok(summary)


def _archive_prune(query, body) -> tuple[int, dict]:
    keep_days = _as_int(_param(query, body, "keep_days"), db.keep_days_setting())
    # 持久化到 meta：后台每天的自动清理读这里。不存的话界面改成 365、
    # 第二天仍被按 90 清掉，等于白改。
    db.meta_set("keep_days", str(keep_days))
    result = db.prune_observations(keep_days)
    db.meta_set("last_prune_at", str(now_ts()))
    clear_cache()
    return 200, ok(result)


# ---------------------------------------------------------------- 路由


def handle(method: str, path: str, query: dict, body: dict | None):
    path = path.rstrip("/") or "/"

    try:
        if path == "/api/health" and method == "GET":
            return _health(query, body)
        if path == "/api/stations":
            if method == "GET":
                return _stations(query, body)
            if method == "POST":
                return _create_station(query, body)
        if path.startswith("/api/stations/"):
            icao = path.split("/", 3)[3].upper()
            # 3 位留给 HKO 这类非机场站（天文台不是 ICAO 机场码）
            if not (3 <= len(icao) <= 4):
                return 400, bad("INVALID_ICAO", "路径里的站代码应为 3–4 位")
            if method == "POST":
                return _update_station(query, body, icao)
            if method == "DELETE":
                return _delete_station(query, body, icao)
        if path == "/api/stationinfo" and method == "GET":
            return _station_info(query, body)
        if path == "/api/series" and method == "GET":
            return _series(query, body)
        if path == "/api/daily" and method == "GET":
            return _daily(query, body)
        if path == "/api/observations" and method == "GET":
            return _observations(query, body)
        if path == "/api/sync" and method == "POST":
            return _sync(query, body)
        if path == "/api/active":
            return _active_station(query, body)
        if path == "/api/export" and method == "GET":
            return _export(query, body)
        if path == "/api/archive" and method == "GET":
            return _archive(query, body)
        if path == "/api/archive/prune" and method == "POST":
            return _archive_prune(query, body)
        return 404, bad("NOT_FOUND", f"没有这个接口：{method} {path}")
    except Exception as exc:  # 单个接口崩了不能拖垮服务
        import traceback

        traceback.print_exc()
        return 500, bad("INTERNAL", f"{type(exc).__name__}: {exc}")
