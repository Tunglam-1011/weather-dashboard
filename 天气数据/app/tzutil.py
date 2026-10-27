"""时区工具。

为什么要有这个文件：Windows 上的 Python 默认不带 IANA 时区库（tzdata 是可选包），
直接 ZoneInfo("Asia/Shanghai") 会抛 ZoneInfoNotFoundError，整个程序第一时间就崩。

所以策略是：能用 zoneinfo 就用（有 tzdata 时能正确处理夏令时），
用不了就退回固定偏移表。中国 1991 年后不再使用夏令时，
Asia/Shanghai 的固定 +8 与 IANA 结果完全等价，所以对现有城市没有任何精度损失。

警告：如果以后接入欧美站点而本机又没有 tzdata，固定偏移会在夏令时切换后错一小时。
      解决办法：pip install tzdata，或者给该站手工指定正确的 tz 名。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache

# 常见时区的固定偏移兜底（单位：小时）。只在 zoneinfo 不可用时生效。
_FALLBACK_HOURS = {
    "Asia/Shanghai": 8,
    "Asia/Chongqing": 8,
    "Asia/Harbin": 8,
    "Asia/Hong_Kong": 8,
    "Asia/Macau": 8,
    "Asia/Taipei": 8,
    "Asia/Singapore": 8,
    "Asia/Kuala_Lumpur": 8,
    "Asia/Manila": 8,
    "Asia/Perth": 8,
    "Asia/Tokyo": 9,
    "Asia/Seoul": 9,
    "Asia/Bangkok": 7,
    "Asia/Jakarta": 7,
    "Asia/Ho_Chi_Minh": 7,
    "Asia/Kolkata": 5.5,
    "Asia/Karachi": 5,
    "Asia/Dubai": 4,
    "Europe/London": 0,
    "Europe/Lisbon": 0,
    "Europe/Paris": 1,
    "Europe/Berlin": 1,
    "Europe/Madrid": 1,
    "Europe/Rome": 1,
    "Europe/Moscow": 3,
    "America/New_York": -5,
    "America/Chicago": -6,
    "America/Denver": -7,
    "America/Los_Angeles": -8,
    "America/Sao_Paulo": -3,
    "Australia/Sydney": 10,
    "Pacific/Auckland": 12,
    "UTC": 0,
    "Etc/UTC": 0,
}

# 是否发生过降级（一旦为 True，启动日志里会提示一次）
_degraded = False
_warned: set[str] = set()


@lru_cache(maxsize=128)
def get_tz(name: str):
    """拿到 tzinfo。zoneinfo 优先，失败退回固定偏移。"""
    global _degraded
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        _degraded = True
        hours = _FALLBACK_HOURS.get(name)
        if hours is None:
            hours = 8  # 兜底：当作东八区
            if name not in _warned:
                _warned.add(name)
                print(f"[tz] 未知时区 {name!r}，已按 UTC+8 处理，请手工确认。")
        return timezone(timedelta(hours=hours), name)


def is_degraded() -> bool:
    """是否没装上 tzdata（只影响欧美夏令时站点）。"""
    return _degraded


def now_ts() -> int:
    return int(time.time())


def to_local_date(epoch: int, tz_name: str) -> str:
    """epoch 秒 -> 该时区下的 'YYYY-MM-DD'。"""
    return datetime.fromtimestamp(epoch, get_tz(tz_name)).strftime("%Y-%m-%d")


def day_start_epoch(date_str: str, tz_name: str) -> int:
    """'YYYY-MM-DD' -> 该时区当天 00:00 的 epoch 秒。"""
    year, month, day = (int(part) for part in date_str.split("-"))
    return int(datetime(year, month, day, tzinfo=get_tz(tz_name)).timestamp())


def fmt_hm(epoch: int, tz_name: str) -> str:
    """epoch 秒 -> 当地 'HH:MM'。"""
    return datetime.fromtimestamp(epoch, get_tz(tz_name)).strftime("%H:%M")


def fmt_md(epoch: int, tz_name: str) -> str:
    return datetime.fromtimestamp(epoch, get_tz(tz_name)).strftime("%m-%d")


def today_str(tz_name: str, at: int | None = None) -> str:
    return to_local_date(at if at is not None else now_ts(), tz_name)


def recent_dates(tz_name: str, days: int, at: int | None = None) -> list[str]:
    """返回最近 days 个自然日（含今天），升序。"""
    base = at if at is not None else now_ts()
    out: list[str] = []
    for offset in range(days - 1, -1, -1):
        out.append(to_local_date(base - offset * 86400, tz_name))
    # 去重（夏令时切换日可能出现重复，中国不会）
    seen: list[str] = []
    for d in out:
        if d not in seen:
            seen.append(d)
    return seen


def next_day_start(date_str: str, tz_name: str) -> int:
    """当天 00:00 之后的下一个自然日 00:00。日界归属左闭右开。"""
    year, month, day = (int(part) for part in date_str.split("-"))
    nxt = datetime(year, month, day, tzinfo=get_tz(tz_name)) + timedelta(days=1)
    return int(nxt.timestamp())
