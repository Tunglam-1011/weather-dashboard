"""每日聚合：只懂"一天怎么切、最高温怎么算、数据定没定稿"。

术语红线（改文案前务必读一遍）：
    这里算出来的叫【观测最高温】，不是"当日极值"。
    METAR 只有整数摄氏度、30 分钟一条，真实峰值完全可能落在两次观测之间。
    所以对外一律说"观测到的最高温"，并同时给出覆盖率让人自己判断可信度。
"""

from __future__ import annotations

from . import config
from .tzutil import day_start_epoch, next_day_start, to_local_date


def median_interval(obs_times: list[int]) -> int:
    """该站的实际采样间隔（取中位数）。有的站每小时一报，有的半小时一报。"""
    if len(obs_times) < 4:
        return config.SAMPLE_INTERVAL
    diffs = [b - a for a, b in zip(obs_times, obs_times[1:]) if 0 < b - a < 6 * 3600]
    if not diffs:
        return config.SAMPLE_INTERVAL
    diffs.sort()
    mid = len(diffs) // 2
    value = diffs[mid] if len(diffs) % 2 else (diffs[mid - 1] + diffs[mid]) // 2
    return max(300, int(value))


def detect_gaps(obs_times: list[int]) -> list[list[int]]:
    """相邻两条间隔超过阈值的，就是断点。曲线在这里断开，绝不插值。"""
    gaps = []
    for a, b in zip(obs_times, obs_times[1:]):
        if b - a > config.GAP_THRESHOLD:
            gaps.append([a, b])
    return gaps


def _expected_count(day_start: int, is_today: bool, now: int, interval: int) -> int:
    span = 86400
    if is_today:
        span = max(600, min(86400, now - day_start))
    return max(1, int(round(span / interval)))


def coverage_ratio(day_start: int, end: int, obs_times: list[int]) -> float:
    """曲线覆盖率 = 有时间轴覆盖的比例，不是"条数达标率"。

    为什么不用条数：同一天可能混着两种粒度（香港早先按小时回填、之后 5 分钟采样），
    中位间隔会被带偏，算出"29 条 / 应有 60 条 = 48%"这种没意义的数。
    这里改成把时间轴切成段，只有跨度超过 GAP_THRESHOLD（90 分钟）的窟窿才算没覆盖——
    和画曲线时"断点处断开"的判定完全一致，看到的和算出来的是同一件事。
    """
    span = max(1, end - day_start)
    if not obs_times:
        return 0.0
    threshold = config.GAP_THRESHOLD
    covered = 0
    prev = day_start
    for stamp in obs_times:
        if stamp <= prev:
            continue
        gap = stamp - prev
        if gap <= threshold:
            covered += gap
        prev = stamp
    tail = end - prev
    if 0 <= tail <= threshold:
        covered += tail
    return round(min(1.0, covered / span), 3)


def _classify(*, is_today: bool, obs_count: int, expected: int, coverage: float,
              gaps: list[list[int]], latest_temp: float | None,
              max_temp: float | None, sampled_max: float | None = None,
              last_obs: int, now: int) -> str:
    if max_temp is None:
        return "empty"
    if obs_count == 0:
        # 只有官方极值、没有观测点（例如刚接入还没攒到当天数据）
        return "stale" if is_today else "final"
    if is_today:
        if now - last_obs > config.STALE_AFTER:
            return "stale"
        # rising 只和【采样最大值】比：官方极值可能高于任何采样点（逐分钟统计
        # vs 定时快照），拿它比会把还在升温的上午误判成"已见顶"
        rising_ref = sampled_max if sampled_max is not None else max_temp
        if latest_temp is not None and latest_temp >= rising_ref:
            return "rising"
        return "settled"
    big_gap = any(end - start > 3 * 3600 for start, end in gaps)
    if coverage < 0.5 or big_gap:
        return "partial"
    return "final"


def build_daily(rows: list[dict], tz_map: dict[str, str], now: int,
                extremes: dict[tuple[str, str], dict] | None = None) -> list[dict]:
    """把原始观测打平成"每站每天一行"的聚合结果。

    extremes 是官方给出的当日极值（目前只有香港天文台提供）。存在时以它为准：
    官方逐分钟在算，而我们只是定时采样，峰值完全可能落在两次采样之间。
    """
    extremes = extremes or {}
    buckets: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        buckets.setdefault((row["icao"], row["local_date"]), []).append(row)

    # 有官方极值但没有观测点的日子（例如刚接入、还没攒到数据）也要能出日线
    for (icao, date) in extremes:
        buckets.setdefault((icao, date), [])

    out: list[dict] = []
    for (icao, date), items in buckets.items():
        items.sort(key=lambda item: item["obs_time"])
        tz = tz_map.get(icao) or config.DEFAULT_TZ
        today = to_local_date(now, tz)
        is_today = date == today

        obs_times = [item["obs_time"] for item in items]
        temps = [(item["obs_time"], item["temp_c"]) for item in items
                 if item["temp_c"] is not None]

        sampled_max = max((temp for _, temp in temps), default=None)
        sampled_min = min((temp for _, temp in temps), default=None)
        max_temp = sampled_max
        min_temp = sampled_min

        official = extremes.get((icao, date))
        max_source = "sampled"
        if official and official.get("max_temp") is not None:
            max_temp = official["max_temp"]          # 官方值优先
            max_source = "official"
            if official.get("min_temp") is not None:
                min_temp = official["min_temp"]

        # 首现/末现只在"官方值恰好也被采到"时才有意义，否则指向的是另一个温度
        max_times = [stamp for stamp, temp in temps if temp == max_temp]
        latest_temp = temps[-1][1] if temps else None

        interval = median_interval(obs_times)
        day_start = day_start_epoch(date, tz)
        day_end = next_day_start(date, tz)
        expected = _expected_count(day_start, is_today, now, interval)
        # 覆盖率按时间算（和曲线断点判定一致），条数只作为"缺几报"的参考
        end = min(now, day_end) if is_today else day_end
        coverage = coverage_ratio(day_start, end, obs_times)
        gaps = detect_gaps(obs_times)

        out.append({
            "icao": icao,
            "date": date,
            "dayStart": day_start,
            "dayEnd": next_day_start(date, tz),
            "maxTemp": max_temp,
            "minTemp": min_temp,
            "sampledMax": sampled_max,
            "sampledMin": sampled_min,
            "maxSource": max_source,
            "maxTimes": max_times,
            "maxTimeFirst": max_times[0] if max_times else None,
            "maxTimeLast": max_times[-1] if max_times else None,
            "maxCount": len(max_times),
            "latestTemp": latest_temp,
            "firstObs": obs_times[0] if obs_times else None,
            "lastObs": obs_times[-1] if obs_times else None,
            "obsCount": len(items),
            "expectedObs": expected,
            "missingObs": max(0, expected - len(items)),
            "coverage": coverage,
            "sampleInterval": interval,
            "gaps": gaps,
            "isToday": is_today,
            "status": _classify(
                is_today=is_today,
                obs_count=len(items),
                expected=expected,
                coverage=coverage,
                gaps=gaps,
                latest_temp=latest_temp,
                max_temp=max_temp,
                sampled_max=sampled_max,
                last_obs=obs_times[-1] if obs_times else 0,
                now=now,
            ),
        })

    out.sort(key=lambda item: (item["icao"], item["date"]))
    return out


def build_series_points(rows: list[dict]) -> tuple[list[list], list[list], int]:
    """把某站的观测转成 [[epoch, temp], ...] + 断点 + 采样间隔。

    返回紧凑数组，前端可以不加转换直接喂 SVG。
    """
    points = [[row["obs_time"], row["temp_c"]] for row in rows if row["temp_c"] is not None]
    times = [row["obs_time"] for row in rows]
    return points, detect_gaps(times), median_interval(times)
