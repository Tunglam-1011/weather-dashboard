"""上游客户端：只懂 HTTP。

数据源：aviationweather.gov METAR 接口（免费、无需 key、只读）

本文件存在的核心原因是这一条事实：
    上游响应里没有任何 Access-Control-Allow-Origin 头，
    浏览器直连必然被跨域拦截，所以必须由本地服务代转。

三个必须理解的接口行为（都已实测，改代码前先看这里）：

1) 返回的数组是【倒序】的（最新在前）。入库前必须按 obs_time 升序重排，
   否则曲线会左右颠倒。
2) 参数 date= 表示【窗口的结束锚点】，不是开始。想往前翻历史，
   要拿当前窗口里最老的 obs_time 当作下一次的锚点。
3) 超出保留期时返回【空 body】（不是 []，也不是错误 JSON）；
   日期太老时返回 HTTP 400。两者都表示"没有更早的数据了"，属正常终止；
   只有超时 / 5xx 才算真错误，需要退避重试。
"""

from __future__ import annotations

import calendar
import datetime
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config, naming

# 两种 opener：默认（走系统代理）与直连。**谁先谁后按域名记忆**，不能一刀切——
# 实测同一台挂了系统代理的机器上，两个上游的最优路径完全相反：
#   data.weather.gov.hk 走代理 6~18 秒、直连亚秒级（HKO 服务器在墙内直连很快）
#   aviationweather.gov 走代理 0.5 秒、直连 16 秒（美国站点反而要靠代理）
# 所以每个 host 记住"上次哪个成功"，失败自动翻转；初始值按实测预设。
_OPENER_DEFAULT = urllib.request.build_opener()
_OPENER_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))
# 这些域名直连优先（已实测更快）；其余先走系统代理，维持旧行为
_DIRECT_FIRST_HOSTS = {"data.weather.gov.hk", "www.hko.gov.hk"}


def _openers_for(url: str) -> list[tuple]:
    """按 host 给出 (opener, 是否直连) 的尝试顺序。"""
    host = urllib.parse.urlsplit(url).hostname or ""
    if host in _DIRECT_FIRST_HOSTS:
        return [(_OPENER_DIRECT, True), (_OPENER_DEFAULT, False)]
    return [(_OPENER_DEFAULT, False), (_OPENER_DIRECT, True)]


def _remember_opener(url: str, direct: bool) -> None:
    """记住这个 host 当前可用的路径，下次先试它。"""
    host = urllib.parse.urlsplit(url).hostname or ""
    if not host:
        return
    if direct:
        _DIRECT_FIRST_HOSTS.add(host)
    else:
        _DIRECT_FIRST_HOSTS.discard(host)


def _url(path: str, params: dict) -> str:
    return f"{config.UPSTREAM_BASE}/{path}?{urllib.parse.urlencode(params)}"


def _fetch_once(url: str, opener, timeout: int) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": config.USER_AGENT,
            "Accept": "application/json, text/plain, */*",
        },
    )
    with opener.open(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def http_get(url: str, timeout: int | None = None, retries: int | None = None) -> tuple[str, str]:
    """返回 (状态, body)。

    状态取值：
      ok            拿到内容
      empty         上游返回空（超出保留期）
      out_of_range  上游返回 400/404（日期太老 / 站点不存在）
      error         网络或服务端真错误，body 是错误描述

    timeout / retries 不传就用全局默认；香港的小 CSV 传更短的值，
    避免代理路径异常时手动刷新等上半分钟。
    """
    effective_timeout = config.HTTP_TIMEOUT if timeout is None else timeout
    attempts = (config.HTTP_RETRIES if retries is None else retries) + 1
    last_error = ""
    for attempt in range(attempts):
        for opener, is_direct in _openers_for(url):
            try:
                body = _fetch_once(url, opener, effective_timeout)
                _remember_opener(url, is_direct)  # 哪条路通了记哪条，下次先试
                if not body.strip():
                    return "empty", ""
                return "ok", body
            except urllib.error.HTTPError as exc:
                if exc.code in (400, 404):
                    return "out_of_range", f"HTTP {exc.code}"
                last_error = f"HTTP {exc.code}"
            except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
                last_error = str(getattr(exc, "reason", exc))
            except Exception as exc:  # 兜底，绝不让单次抓取崩掉整个同步线程
                last_error = f"{type(exc).__name__}: {exc}"
        if attempt < attempts - 1:
            time.sleep(1.5 * (attempt + 1))
    return "error", last_error or "unknown error"


# ---------------------------------------------------------------- METAR


def _parse_record(raw: dict) -> dict | None:
    obs_time = raw.get("obsTime")
    if not obs_time:
        return None
    visib = raw.get("visib")
    return {
        "icao": (raw.get("icaoId") or "").upper(),
        "obs_time": int(obs_time),
        "temp": raw.get("temp"),
        "dewp": raw.get("dewp"),
        "wdir": raw.get("wdir"),
        # 注意：上游 wdir 是度，wspd 是【节】（knots），不是 m/s。
        # 报文里可能是 MPS（如 05007MPS），但 JSON 字段已经统一换成节了。
        # 换算 km/h 用 ×1.852。
        "wspd": raw.get("wspd"),
        "visib": None if visib is None else str(visib),  # 可能是 '6+' 这种字符串
        "altim": raw.get("altim"),
        "raw": raw.get("rawOb"),
    }


def _normalize_records(records: list[dict], icao: str) -> list[dict]:
    """过滤他站记录；同一 obs_time 去重；按时间升序返回。

    上游数组是倒序（最新在前），所以同一时间戳出现两条时，【先遍历到的那条
    更新】（比如发过更正报 COR）。必须用 setdefault 保留先出现的——
    原先用赋值覆盖，留下的是较旧的那条，更正会被丢掉。
    """
    wanted = icao.upper()
    deduped: dict[int, dict] = {}
    for rec in records:
        if rec["icao"] and rec["icao"] != wanted:
            continue
        deduped.setdefault(rec["obs_time"], rec)
    return sorted(deduped.values(), key=lambda item: item["obs_time"])


def fetch_metar(icao: str, hours: int, anchor_ts: int | None = None) -> tuple[list[dict], str]:
    """拉一个站点的 METAR。返回 (按 obs_time 升序的记录, 状态)。"""
    params = {"ids": icao.upper(), "format": "json", "hours": int(hours)}
    if anchor_ts:
        params["date"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(anchor_ts)))

    status, body = http_get(_url("metar", params))
    if status != "ok":
        return [], status

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return [], "error"
    if not isinstance(payload, list):
        return [], "error"

    records = [rec for rec in (_parse_record(item) for item in payload) if rec]
    return _normalize_records(records, icao), "ok"


# ---------------------------------------------------------------- 站点信息


def fetch_station_info(icao: str) -> tuple[dict | None, str]:
    """校验 ICAO 并取站点元信息。查无此站时返回 (None, 'not_found')。"""
    status, body = http_get(_url("stationinfo", {"ids": icao.upper(), "format": "json"}))
    if status == "out_of_range":
        return None, "not_found"
    if status != "ok":
        return None, status

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None, "error"
    if not isinstance(payload, list) or not payload:
        return None, "not_found"

    for item in payload:
        code = (item.get("icaoId") or item.get("id") or "").upper()
        if code == icao.upper():
            return {
                "icao": code,
                # site 保留上游原文（"Shanghai/Pudong Intl"），
                # site_name 是洗干净后给看板用的（"Shanghai Pudong"）
                "site": item.get("site"),
                "site_name": naming.clean_site_name(item.get("site"), code),
                "iata": item.get("iataId"),
                "wmo": item.get("wmoId"),
                "lat": item.get("lat"),
                "lon": item.get("lon"),
                "elev": item.get("elev"),
                "country": item.get("country"),
                "state": item.get("state"),
                "has_metar": "METAR" in (item.get("siteType") or []),
            }, "ok"
    return None, "not_found"


# ---------------------------------------------------------------- 香港天文台

# HKO 的 CSV 全部是「最新快照」，没有历史，只能每隔几分钟抓一次往后积累。
# 时间字段形如 202610222130，固定香港时间（UTC+8，无夏令时）。
_HKO_TZ_OFFSET = 8 * 3600


def _parse_hko_time(text: str) -> int | None:
    """'202610222130' -> epoch 秒。"""
    digits = "".join(ch for ch in (text or "") if ch.isdigit())
    if len(digits) != 12:
        return None
    try:
        stamp = time.strptime(digits, "%Y%m%d%H%M")
    except ValueError:
        return None
    # timegm 把 struct 当成 UTC 解析，再减掉香港固定偏移，结果与本机时区无关
    return int(calendar.timegm(stamp) - _HKO_TZ_OFFSET)


def _parse_hko_value(text: str) -> float | None:
    """数值列。上游会给 '24.1*' 这种带星号的，星号表示数据不完整，值本身仍可用。"""
    cleaned = (text or "").strip().rstrip("*").strip()
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _read_hko_csv(filename: str) -> tuple[list[list[str]], str]:
    """抓一个 HKO CSV，返回 (去掉表头的数据行, 状态)。

    只有小几 KB 的快照文件：短超时 + 少重试，失败快速交给下一轮。
    """
    status, body = http_get(f"{config.HKO_BASE}/{filename}",
                            timeout=config.HKO_HTTP_TIMEOUT,
                            retries=config.HKO_HTTP_RETRIES)
    if status != "ok":
        return [], status

    lines = [line for line in body.replace("\r\n", "\n").split("\n") if line.strip()]
    if len(lines) < 2:
        return [], "empty"
    # 第一行是表头（带 BOM），丢掉
    rows = []
    for line in lines[1:]:
        parts = [part.strip() for part in line.split(",")]
        if len(parts) >= 3:
            rows.append(parts)
    return rows, "ok"


def list_hko_places() -> tuple[list[str], str]:
    """当前可用的 HKO 自动气象站名单（英文站名）。"""
    rows, status = _read_hko_csv(config.HKO_TEMP_CSV)
    places = [row[1] for row in rows if row[1]]
    return places, status


def fetch_hko_temperature(place: str) -> tuple[dict | None, str]:
    """取某站的当前气温（1 分钟更新，0.1°C）。"""
    rows, status = _read_hko_csv(config.HKO_TEMP_CSV)
    if status != "ok":
        return None, status

    wanted = (place or config.HKO_DEFAULT_PLACE).strip().lower()
    for row in rows:
        if row[1].strip().lower() != wanted:
            continue
        obs_time = _parse_hko_time(row[0])
        temp = _parse_hko_value(row[2])
        if obs_time is None or temp is None:
            return None, "empty"
        return {
            "obs_time": obs_time,
            "temp": temp,
            "raw": f"HKO {row[0]} {row[1]} {row[2]}",
        }, "ok"
    # 站名拼错 / 该站已下线
    return None, "not_found"


def fetch_hko_extreme(place: str) -> tuple[dict | None, str]:
    """取某站「当日 00:00 至今」的最高 / 最低气温（官方口径，0.1°C）。

    这个值比我们自己采样算出来的更权威：官方是逐分钟在算，我们只是每 5 分钟
    抓一次快照，峰值完全可能落在两次抓取之间。
    """
    rows, status = _read_hko_csv(config.HKO_EXTREME_CSV)
    if status != "ok":
        return None, status

    wanted = (place or config.HKO_DEFAULT_PLACE).strip().lower()
    for row in rows:
        if row[1].strip().lower() != wanted:
            continue
        if len(row) < 4:
            return None, "empty"
        obs_time = _parse_hko_time(row[0])
        tmax = _parse_hko_value(row[2])
        tmin = _parse_hko_value(row[3])
        if obs_time is None:
            return None, "empty"
        return {"obs_time": obs_time, "tmax": tmax, "tmin": tmin}, "ok"
    return None, "not_found"


# ---------------------------------------------------------------- 香港历史

# 逐日提取数据（"Daily Extract of Meteorological Observations"）。
# 文件名是 .xml，内容其实是 JSON；按月一个文件，包含该月 1 号到【昨天】的逐日数据。
# 列顺序：[日, 平均气压, 最高气温, 平均气温, 最低气温, 平均露点, 平均湿度, 平均云量, 降雨]
_HKO_DAILY_URL = "https://www.hko.gov.hk/cis/dailyExtract/dailyExtract_{year}{month:02d}.xml"
_HKO_DAILY_COL_DAY = 0
_HKO_DAILY_COL_MAX = 2
_HKO_DAILY_COL_MIN = 4


def fetch_hko_daily_month(year: int, month: int) -> dict[str, dict]:
    """取某个月的逐日最高/最低。返回 {'YYYY-MM-DD': {'tmax','tmin'}}。

    末尾会有 'Mean/Total' / 'Normal' 两行汇总，靠"日必须是两位数字"过滤掉。
    """
    status, body = http_get(_HKO_DAILY_URL.format(year=year, month=month))
    if status != "ok":
        return {}
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return {}

    out: dict[str, dict] = {}
    for block in (payload.get("stn") or {}).get("data") or []:
        block_month = block.get("month") or month
        for row in block.get("dayData") or []:
            if len(row) <= _HKO_DAILY_COL_MIN:
                continue
            day = str(row[_HKO_DAILY_COL_DAY] or "").strip()
            if not day.isdigit():
                continue  # Mean/Total、Normal 这类汇总行
            key = f"{year:04d}-{int(block_month):02d}-{int(day):02d}"
            out[key] = {
                "tmax": _parse_hko_value(row[_HKO_DAILY_COL_MAX]),
                "tmin": _parse_hko_value(row[_HKO_DAILY_COL_MIN]),
            }
    return out


def fetch_hko_history(days: int, until_date: str) -> dict[str, dict]:
    """取截至 until_date（不含）往前 days 天的逐日极值。

    跨月时自动抓两个月并合并 —— 月中接入只会多花一次请求。
    """
    year, month, _ = (int(part) for part in until_date.split("-"))
    months: list[tuple[int, int]] = [(year, month)]
    start_date = datetime.date.fromisoformat(until_date) - datetime.timedelta(days=days)
    if (start_date.year, start_date.month) != (year, month):
        months.append((start_date.year, start_date.month))

    merged: dict[str, dict] = {}
    for y, m in months:
        merged.update(fetch_hko_daily_month(y, m))

    # 只保留窗口内、且早于 until_date 的日期（今天的数据用实时极值文件，更准）
    lo = start_date.strftime("%Y-%m-%d")
    return {date: value for date, value in merged.items() if lo <= date < until_date}


# ---------------------------------------------------------------- 香港逐小时

# 区域气象站门户的动画数据：过去 24 小时、逐小时、39 个站，格式
#   202610212300,28.7,hko     ← 时间(香港), 气温, 站码
# 弹窗 TimeSeries 用的就是这一份。它是天文台公开数据里唯一带时间回溯的温度序列，
# 但只有【小时】粒度 —— 5 分钟级历史天文台没有发布。
def fetch_hko_hourly(place: str) -> tuple[list[dict], str]:
    """取香港天文台过去 24 小时的逐小时气温（升序）。"""
    if (place or config.HKO_DEFAULT_PLACE) != config.HKO_DEFAULT_PLACE:
        return [], "not_found"

    status, body = http_get(f"{config.HKO_PORTAL_BASE}/{config.HKO_HOURLY_CSV}")
    if status != "ok":
        return [], status

    code = config.HKO_STATION_CODE.lower()
    by_time: dict[int, float] = {}
    for line in body.replace("\r\n", "\n").split("\n"):
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 3 or parts[2].lower() != code:
            continue
        obs_time = _parse_hko_time(parts[0])
        temp = _parse_hko_value(parts[1])
        if obs_time is None or temp is None:
            continue
        by_time[obs_time] = temp

    return [{"obs_time": stamp, "temp": temp} for stamp, temp in sorted(by_time.items())], "ok"


# ---------------------------------------------------------------- 自检


def probe(icao: str) -> None:
    """命令行自检：python server.py --probe ZSPD / --probe HKO"""
    print(f"[probe] 站点 {icao.upper()}")
    if icao.upper() == "HKO":
        from .tzutil import fmt_hm

        places, status = list_hko_places()
        print(f"[probe] 天文台站点表: {status} / {len(places)} 个自动气象站")
        record, status = fetch_hko_temperature(config.HKO_DEFAULT_PLACE)
        if record:
            print(f"[probe] 当前气温 {record['temp']}°C"
                  f"（{fmt_hm(record['obs_time'], 'Asia/Hong_Kong')} 香港时间）")
        else:
            print(f"[probe] 当前气温: {status}")
        extreme, status = fetch_hko_extreme(config.HKO_DEFAULT_PLACE)
        if extreme:
            print(f"[probe] 当日极值 最高 {extreme['tmax']} / 最低 {extreme['tmin']}")
        else:
            print(f"[probe] 当日极值: {status}")

        hourly, status = fetch_hko_hourly(config.HKO_DEFAULT_PLACE)
        if hourly:
            print(f"[probe] 过去 24h 逐小时 {len(hourly)} 点，"
                  f"最近 {hourly[-1]['temp']}°C（{fmt_hm(hourly[-1]['obs_time'], 'Asia/Hong_Kong')}）")
        else:
            print(f"[probe] 逐小时序列: {status}")
        return

    info, status = fetch_station_info(icao)
    if info is None:
        print(f"[probe] stationinfo: {status} → 无效 ICAO 或网络不可用")
        return
    print(f"[probe] stationinfo: {info['site']} → 显示名 {info['site_name']} / {info['country']} "
          f"({info['lat']}, {info['lon']}) METAR={info['has_metar']}")

    records, status = fetch_metar(icao, config.LOOKBACK_HOURS)
    print(f"[probe] metar 近 {config.LOOKBACK_HOURS}h: {status} / {len(records)} 条")
    if not records:
        return
    first, last = records[0], records[-1]
    fmt = "%Y-%m-%d %H:%M"
    print(f"[probe] 窗口 {time.strftime(fmt, time.gmtime(first['obs_time']))}Z"
          f" → {time.strftime(fmt, time.gmtime(last['obs_time']))}Z (UTC)")
    print(f"[probe] 最新一条: {last['temp']}°C / 露点 {last['dewp']} / {last['raw']}")
