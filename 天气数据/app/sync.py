"""同步调度：只懂"什么时候去拉、拉到什么程度算完"。

设计要点：
  - 一个守护线程，5 分钟一轮（用 Event.wait 而不是 sleep，可以随时被唤醒或优雅退出）
  - 一把全局锁保证同一时刻只有一条上游请求链在飞 —— 这是"不踩上游限流"的根本保证
  - 首次启动为每个站点回填 history，之后只做增量，靠主键幂等去重
  - HTTP 线程只读库，本线程只写库，两者通过 SQLite WAL 解耦；
    所以上游挂了页面照样秒开，只是显示的数据滞后
"""

from __future__ import annotations

import threading
import time
import traceback

from . import config, db, daily, source
from .tzutil import day_start_epoch, now_ts, to_local_date, today_str


# ---------------------------------------------------------------- 当前城市

# 服务端需要知道"用户正在看哪个城市"：后台每轮只同步它，而不是把所有城市
# 都拉一遍。浏览器在加载 / 切换城市时通过 POST /api/active 上报，存在 meta 里
# 所以重启后依然有效。没人上报（只跑服务不开页面）时退回同步全部。
_ACTIVE_KEY = "active_icao"


def set_active_icao(icao: str | None) -> None:
    db.meta_set(_ACTIVE_KEY, (icao or "").upper())


def get_active_icao() -> str | None:
    value = (db.meta_get(_ACTIVE_KEY, "") or "").strip().upper()
    return value or None


def station_refresh_interval(station: dict) -> int:
    """这个站两次同步之间应该隔多久（秒）。

    节奏跟着数据源走，而不是一刀切：
      - 香港：快照约 10 分钟一更，取一半（5 分钟）保证不漏
      - METAR：看归档里实测的报文间隔——半小时一报的站 20 分钟刷一次就够，
        一小时一报的站 36 分钟才需要刷。没有历史时用默认 20 分钟。
    """
    if (station.get("provider") or "metar").lower() != "metar":
        return config.HKO_REFRESH_INTERVAL

    times = db.recent_obs_times(station["icao"], limit=48)
    times.reverse()  # median_interval 假设升序
    interval = daily.median_interval(times)
    if interval > config.SAMPLE_INTERVAL:
        guess = int(interval * 0.6)  # 比报文节奏密一点，宁可稍早不可漏
    else:
        guess = config.METAR_REFRESH_INTERVAL
    return max(600, min(3600, guess))


def _due_stations(exclude: str | None, now: int) -> list[dict]:
    """除 exclude 外，已经"到期"该刷新的站，逾期最多的排最前。

    到期 = 距上次尝试超过它自己的刷新间隔；还没回填完历史的站永远算到期。
    排序用【逾期倍数】而不是绝对时长：香港间隔 1 分钟、METAR 间隔 20 分钟，
    都刚好到期时（1.0 vs 1.0）才公平，按绝对时长排会把香港永远压到最后——饿死。
    """
    due: list[tuple[float, dict]] = []
    for station in db.list_stations():
        icao = station["icao"]
        if icao == exclude:
            continue
        state = db.get_sync_state(icao)
        if not state["backfill_done"]:
            # 缺历史的最优先；失败退避照样生效，别每 60 秒重打一次 21 天回填
            if now >= int(state["next_retry_at"] or 0):
                due.append((9e9, station))
            continue
        interval = max(1, station_refresh_interval(station))
        age = now - int(state["last_attempt_at"] or 0)
        if age >= interval:
            due.append((age / interval, station))
    due.sort(key=lambda item: -item[0])
    return [station for _ratio, station in due]


class SyncWorker(threading.Thread):
    def __init__(self) -> None:
        super().__init__(daemon=True, name="sync-worker")
        self._stop_event = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        # 单站同步用的锁：手动刷新不该排队等后台整轮（一轮两个站要好几秒）
        self._station_locks: dict[str, threading.Lock] = {}
        self._last_extreme_at: dict[str, float] = {}   # icao -> 上次抓官方极值的时间
        self.state: dict = {
            "started_at": now_ts(),
            "running": False,
            "phase": "starting",
            "backfill": {},      # icao -> {"done": n, "days": n}
            "last_round_at": 0,
            "last_inserted": 0,
            "last_error": None,
            "last_prune": None,
        }

    # ------------------------------------------------------------ 生命周期

    def run(self) -> None:
        self.state["running"] = True
        self.state["phase"] = "bootstrap"
        try:
            # 注意：这个方法不能叫 _bootstrap —— threading.Thread 内部用 _bootstrap
            # 作为线程入口并依赖它去 set _started 事件。一旦同名覆盖，
            # Thread.start() 会永远等不到事件，主线程直接卡死。
            self._initial_sync()
        except Exception:
            self.state["last_error"] = traceback.format_exc(limit=3)

        self.state["phase"] = "idle"
        while not self._stop_event.is_set():
            self._wake.wait(config.POLL_INTERVAL)
            self._wake.clear()
            if self._stop_event.is_set():
                break
            try:
                self.sync_all(reason="poll")
            except Exception:
                self.state["last_error"] = traceback.format_exc(limit=3)
        self.state["running"] = False
        self.state["phase"] = "stopped"

    def stop(self) -> None:
        self._stop_event.set()
        self._wake.set()

    def wake(self) -> None:
        self._wake.set()

    # ------------------------------------------------------------ 对外入口

    def sync_all(self, only: str | None = None, hours: int | None = None,
                 force: bool = False, reason: str = "manual",
                 wait: float = 0.0) -> dict:
        """同步一批站点。

        单站同步（手动刷新）只锁这一个站，不去抢后台整轮的全局锁 —— 否则正好
        撞上一轮（两三个站要好几秒）就得干等。并发抓同一个站最多多一次请求，
        写入是幂等的，可以接受。
        """
        if only:
            lock = self._station_locks.setdefault(only.upper(), threading.Lock())
            acquired = lock.acquire(timeout=wait) if wait > 0 else lock.acquire(blocking=False)
            if not acquired:
                return {"ok": False, "reason": "busy", "results": []}
            try:
                return self._do_sync_all(only, hours, force, reason)
            finally:
                lock.release()

        acquired = self._lock.acquire(timeout=wait) if wait > 0 else self._lock.acquire(blocking=False)
        if not acquired:
            return {"ok": False, "reason": "busy", "results": []}
        try:
            return self._do_sync_all(only, hours, force, reason)
        finally:
            self._lock.release()

    def schedule_backfill(self, icao: str) -> bool:
        """新加城市时后台回填历史，不阻塞接口返回。"""
        station = db.get_station(icao)
        if station is None:
            return False

        def _job() -> None:
            if not self._lock.acquire(blocking=False):
                # 有别的同步在跑，交给下一轮轮询接管
                db.set_sync_state(icao, backfill_done=0)
                return
            try:
                self._backfill_station(station, config.BACKFILL_DAYS)
            except Exception:
                self.state["last_error"] = traceback.format_exc(limit=3)
            finally:
                self._lock.release()

        threading.Thread(target=_job, daemon=True, name=f"backfill-{icao}").start()
        return True

    def snapshot(self) -> dict:
        return {
            "running": self.state["running"],
            "phase": self.state["phase"],
            "backfill": dict(self.state["backfill"]),
            "lastRoundAt": self.state["last_round_at"],
            "lastInserted": self.state["last_inserted"],
            "lastError": self.state["last_error"],
            "lastPrune": self.state["last_prune"],
            "pollInterval": config.POLL_INTERVAL,
            "activeIcao": get_active_icao(),
        }

    # ------------------------------------------------------------ 内部实现

    def _initial_sync(self) -> None:
        # 开机这一轮：当前城市 + 所有"到期"的站（含缺历史的）。刚重启就重跑
        # 刚同步过的站没有意义——按各自节奏到期才补。
        stations = db.list_stations()
        active = get_active_icao()
        current = db.get_station(active) if active else None
        if current is not None:
            now = now_ts()
            stations = [current] + [s for s in _due_stations(active, now)]

        for index, station in enumerate(stations):
            if self._stop_event.is_set():
                return
            icao = station["icao"]
            self._enrich_station(station)
            state = db.get_sync_state(icao)
            provider = (station.get("provider") or "metar").lower()
            try:
                if provider != "metar":
                    # 香港每次启动都重跑一次轻量回填（两个请求）：逐小时文件覆盖过去
                    # 24 小时，能自动补上停机期间的整点，不必等用户发现曲线断了。
                    self._backfill_hko(station, config.BACKFILL_DAYS)
                elif not state["backfill_done"]:
                    self._backfill_station(station, config.BACKFILL_DAYS)
                else:
                    self._sync_one(station, config.LOOKBACK_HOURS, force=True)
            except Exception:
                self.state["last_error"] = traceback.format_exc(limit=3)
            if index < len(stations) - 1:
                time.sleep(config.STATION_SPACING)
        self.state["last_round_at"] = now_ts()

    def _enrich_station(self, station: dict) -> None:
        """补全站点元信息（站名 / 经纬度 / 国家）。每个站每次启动最多查一次。"""
        # 只有 METAR 站需要去 stationinfo 补经纬度；香港站的坐标写在 config 里
        if (station.get("provider") or "metar").lower() != "metar":
            return
        if station.get("lat") is not None and station.get("name_en"):
            return
        info, status = source.fetch_station_info(station["icao"])
        if info is None:
            return
        db.upsert_station(
            station["icao"],
            name_en=info.get("site"),
            lat=info.get("lat"),
            lon=info.get("lon"),
            elev=info.get("elev"),
            country=info.get("country"),
            state=info.get("state"),
        )
        # 默认城市只有中文名，补上英文站名方便核对
        if station["name"] == station["icao"] and info.get("site"):
            db.upsert_station(station["icao"], name=info["site"])

    def _do_sync_all(self, only: str | None, hours: int | None,
                     force: bool, reason: str) -> dict:
        hours = hours or config.LOOKBACK_HOURS
        if only:
            station = db.get_station(only)
            if station is None:
                return {"ok": False, "reason": "unknown_station", "results": []}
            stations = [station]
        else:
            # 节奏感知调度：当前城市每轮必刷（它在屏幕上，1 个请求换实时感）；
            # 其他城市按各自的数据节奏，到期才刷，每轮最多补 MAX_EXTRA_PER_ROUND 个。
            active = get_active_icao()
            current = db.get_station(active) if active else None
            if current is None:
                # 还没人打开页面（或当前城市被删了）：全量跑一轮，保证冷启动有数据
                stations = db.list_stations()
            else:
                now = now_ts()
                stations = [current]
                stations.extend(_due_stations(active, now)[:config.MAX_EXTRA_PER_ROUND])

        results = []
        total_inserted = 0
        for index, station in enumerate(stations):
            if self._stop_event.is_set():
                break
            icao = station["icao"]
            try:
                if not db.get_sync_state(icao)["backfill_done"]:
                    # 回填没完成的站由轮询接管：schedule_backfill 抢不到全局锁时
                    # 只把 backfill_done 置 0，这里不补的话，21 天历史要等到重启。
                    result = self._backfill_station(station, config.BACKFILL_DAYS)
                else:
                    result = self._sync_one(station, hours, force=force)
            except Exception as exc:
                result = {"icao": icao, "status": "error",
                          "error": f"{type(exc).__name__}: {exc}"}
            results.append(result)
            total_inserted += int(result.get("inserted") or 0)
            if index < len(stations) - 1:
                time.sleep(config.STATION_SPACING)

        self.state["last_round_at"] = now_ts()
        self.state["last_inserted"] = total_inserted
        db.checkpoint()
        self._maybe_prune()
        self._maybe_backup()
        return {
            "ok": all(item.get("status") != "error" for item in results),
            "reason": reason,
            "inserted": total_inserted,
            "results": results,
        }

    def _maybe_prune(self) -> None:
        """每天最多清理一次超期归档，让库体积封顶而不是无限增长。"""
        # 保留天数以 meta 里的运行时设置为准（界面可改），config 只是默认值
        keep = db.keep_days_setting()
        if not keep or keep <= 0:
            return
        last = int(db.meta_get("last_prune_at", "0") or 0)
        now = now_ts()
        if now - last < 86400:
            return
        result = db.prune_observations(keep, now)
        db.meta_set("last_prune_at", str(now))
        self.state["last_prune"] = result
        if result.get("deleted"):
            try:
                from . import api
                api.clear_cache()
            except Exception:
                pass

    def _maybe_backup(self) -> None:
        """定期自动备份一份到 data/backups/，只保留最近几份。

        归档是这些历史的唯一副本：METAR 上游只留约 30 天、香港根本没有
        历史，库文件损坏 = 永久丢失。VACUUM INTO 是在线备份，服务不用停，
        几 MB 的库毫秒级完成。
        """
        interval = config.BACKUP_INTERVAL_DAYS * 86400
        if interval <= 0:
            return
        last = int(db.meta_get("last_backup_at", "0") or 0)
        now = now_ts()
        if now - last < interval:
            return
        try:
            folder = config.DATA_DIR / "backups"
            folder.mkdir(parents=True, exist_ok=True)
            db.hot_backup(folder / f"weather-{time.strftime('%Y%m%d-%H%M%S')}.db")
            db.meta_set("last_backup_at", str(now))
            self._prune_backups(folder)
        except Exception:
            self.state["last_error"] = traceback.format_exc(limit=3)

    @staticmethod
    def _prune_backups(folder) -> None:
        backups = sorted(folder.glob("weather-*.db"))
        for old in backups[:-config.BACKUP_KEEP_COPIES] if config.BACKUP_KEEP_COPIES > 0 else []:
            try:
                old.unlink()
            except OSError:
                pass

    def _sync_one(self, station: dict, hours: int, force: bool = False) -> dict:
        icao = station["icao"]
        if (station.get("provider") or "metar").lower() != "metar":
            return self._sync_hko(station, force=force)

        tz = station.get("tz") or config.DEFAULT_TZ
        now = time.time()
        sync_state = db.get_sync_state(icao)

        if not force:
            # 间隔按【每个站】各算各的。原先用一个全局 _last_fetch 判断，
            # 结果一轮里只有第一站能过，其余全被判 too_soon —— 城市越多刷得越慢。
            if now - int(sync_state["last_attempt_at"] or 0) < config.MIN_FETCH_GAP:
                return {"icao": icao, "status": "skipped", "reason": "too_soon", "inserted": 0}
            if sync_state["next_retry_at"] and now < sync_state["next_retry_at"]:
                return {"icao": icao, "status": "skipped", "reason": "backoff", "inserted": 0}

        records, status = source.fetch_metar(icao, hours)
        db.set_sync_state(icao, last_attempt_at=now_ts())

        if status == "error":
            streak = int(sync_state["fail_streak"] or 0) + 1
            delay = min(300, 30 * (2 ** min(streak, 4)))
            db.set_sync_state(icao, last_error=str(records or "upstream error"),
                              fail_streak=streak, next_retry_at=now_ts() + delay)
            return {"icao": icao, "status": "error", "error": str(records),
                    "inserted": 0, "retryIn": delay}

        # empty / out_of_range 都表示"这段窗口没有数据"，不是错误
        inserted = db.insert_observations(records, {icao: tz}) if records else 0
        newest = records[-1]["obs_time"] if records else (sync_state["last_obs_time"] or 0)
        db.set_sync_state(
            icao,
            last_success_at=now_ts(),
            last_obs_time=newest,
            last_count=len(records),
            last_error=None,
            fail_streak=0,
            next_retry_at=0,
        )
        if inserted:
            try:
                from . import api
                api.clear_cache()
            except Exception:
                pass
        return {"icao": icao, "status": "ok" if status == "ok" else status,
                "fetched": len(records), "inserted": inserted}

    def _sync_hko(self, station: dict, force: bool = False) -> dict:
        """香港天文台：一次拉两个 CSV。

        两个数据源分工不同，都要抓：
          - 1 分钟气温表 → 存观测点，负责画曲线
          - 当日极值表   → 存 daily_extreme，负责"今日最高"的权威值
        后者更准：官方逐分钟在算，我们只是定时抓快照，峰值可能落在两次抓取之间。
        """
        icao = station["icao"]
        place = station.get("place") or config.HKO_DEFAULT_PLACE
        tz = station.get("tz") or "Asia/Hong_Kong"
        now = time.time()
        sync_state = db.get_sync_state(icao)

        if not force:
            if now - int(sync_state["last_attempt_at"] or 0) < config.HKO_MIN_INTERVAL:
                return {"icao": icao, "status": "skipped", "reason": "too_soon", "inserted": 0}
            if sync_state["next_retry_at"] and now < sync_state["next_retry_at"]:
                return {"icao": icao, "status": "skipped", "reason": "backoff", "inserted": 0}

        record, status = source.fetch_hko_temperature(place)
        db.set_sync_state(icao, last_attempt_at=now_ts())

        if record is None:
            streak = int(sync_state["fail_streak"] or 0) + 1
            delay = min(300, 30 * (2 ** min(streak, 4)))
            message = f"HKO {place}: {status}"
            db.set_sync_state(icao, last_error=message, fail_streak=streak,
                              next_retry_at=now_ts() + delay)
            return {"icao": icao, "status": "error", "error": message,
                    "inserted": 0, "retryIn": delay}

        inserted = db.insert_observations(
            [{"icao": icao, "obs_time": record["obs_time"],
              "temp": record["temp"], "raw": record["raw"]}],
            {icao: tz},
        )

        # 官方当日极值只随分钟缓慢变化，没必要跟着手动刷新一起抓：
        # 5 分钟节奏足够。原先 force 会连带极值一起抓，一次刷新要串行等
        # 两个上游请求，慢一倍还多。失败不算同步失败——曲线已经拿到了。
        last_extreme = self._last_extreme_at.get(icao, 0.0)
        extreme = None
        extreme_status = "skipped"
        if now - last_extreme >= config.HKO_EXTREME_INTERVAL:
            extreme, extreme_status = source.fetch_hko_extreme(place)
            self._last_extreme_at[icao] = now
        if extreme and extreme.get("tmax") is not None:
            db.upsert_daily_extreme(
                icao,
                to_local_date(extreme["obs_time"], tz),
                extreme.get("tmax"),
                extreme.get("tmin"),
                extreme["obs_time"],
            )

        db.set_sync_state(
            icao,
            last_success_at=now_ts(),
            last_obs_time=record["obs_time"],
            last_count=1,
            last_error=None,
            fail_streak=0,
            next_retry_at=0,
        )
        if inserted:
            try:
                from . import api
                api.clear_cache()
            except Exception:
                pass
        return {"icao": icao, "status": "ok", "fetched": 1, "inserted": inserted,
                "official": bool(extreme), "extremeStatus": extreme_status}

    def _backfill_hko(self, station: dict, days: int) -> dict:
        """香港回填：每日极值（21 天）+ 逐小时曲线（24 小时）。

        两套数据粒度不同，都要拿：
          - `dailyExtract_YYYYMM.xml` → 每天的官方最高/最低（到昨天），补每日区间图
          - `animate_J1.csv`          → 过去 24 小时逐小时气温，补今天早先的曲线
        天文台没有公开 5 分钟级历史，所以更细的点只能靠 1 分钟快照按轮询采样累积。
        """
        icao = station["icao"]
        tz = station.get("tz") or "Asia/Hong_Kong"
        place = station.get("place") or config.HKO_DEFAULT_PLACE
        self.state["phase"] = "backfill"
        count = 0
        hourly_count = 0

        # 逐日提取只有天文台总部这一个站的数据
        if place == config.HKO_DEFAULT_PLACE:
            today = today_str(tz)
            history = source.fetch_hko_history(days, today)
            for date, value in history.items():
                if value.get("tmax") is None and value.get("tmin") is None:
                    continue
                db.upsert_daily_extreme(
                    icao, date, value.get("tmax"), value.get("tmin"),
                    day_start_epoch(date, tz),
                )
                count += 1

            # 逐小时曲线：只补今天和昨天（文件本身就是过去 24 小时）
            hourly, _status = source.fetch_hko_hourly(place)
            if hourly:
                records = [
                    {"icao": icao, "obs_time": item["obs_time"], "temp": item["temp"],
                     "raw": "HKO animate_J1 (hourly)"}
                    for item in hourly
                ]
                hourly_count = db.insert_observations(records, {icao: tz})

        db.set_sync_state(icao, backfill_done=1, last_success_at=now_ts(),
                          last_error=None, fail_streak=0, next_retry_at=0)
        self.state["backfill"][icao] = {
            "done": count, "days": days, "finished": True,
            "hourlyInserted": hourly_count,
            "note": "每日极值 + 过去 24 小时逐小时曲线",
        }
        self.state["phase"] = "idle"
        if count or hourly_count:
            try:
                from . import api
                api.clear_cache()
            except Exception:
                pass
        return {"icao": icao, "status": "ok", "inserted": hourly_count, "backfill": True}

    def _backfill_station(self, station: dict, days: int) -> dict:
        """回填入口：按 provider 分派。两种源的历史形态不同，不能混着写。"""
        if (station.get("provider") or "metar").lower() != "metar":
            return self._backfill_hko(station, days)
        return self._backfill_station_metar(station, days)

    def _backfill_station_metar(self, station: dict, days: int) -> dict:
        """按 date= 结束锚点向前翻页，直到越过 days 或上游说没有了。"""
        icao = station["icao"]
        tz = station.get("tz") or config.DEFAULT_TZ
        anchor = now_ts()
        stop_at = anchor - days * 86400
        total = 0
        self.state["backfill"][icao] = {"done": 0, "days": days}
        self.state["phase"] = "backfill"

        while anchor > stop_at and not self._stop_event.is_set():
            records, status = source.fetch_metar(
                icao, config.BACKFILL_CHUNK_HOURS, anchor_ts=anchor
            )
            db.set_sync_state(icao, last_attempt_at=now_ts())

            if status in ("empty", "out_of_range"):
                break  # 上游没有更早的数据了，正常结束
            if status == "error":
                streak = int(db.get_sync_state(icao)["fail_streak"] or 0) + 1
                delay = min(300, 30 * (2 ** min(streak, 4)))
                db.set_sync_state(icao, last_error=str(records),
                                  fail_streak=streak, next_retry_at=now_ts() + delay)
                return {"icao": icao, "status": "error", "error": str(records),
                        "inserted": total, "retryIn": delay}
            if not records:
                break

            total += db.insert_observations(records, {icao: tz})
            oldest = records[0]["obs_time"]
            self.state["backfill"][icao] = {
                "done": total,
                "days": days,
                "cursor": oldest,
            }
            db.set_sync_state(icao, backfill_cursor=str(oldest))

            if oldest >= anchor - 3600:
                break  # 没往前推进，防止死循环
            anchor = oldest - 1800
            time.sleep(config.BACKFILL_STEP_SLEEP)

        db.set_sync_state(
            icao,
            backfill_done=1,
            last_success_at=now_ts(),
            last_error=None,
            fail_streak=0,
            next_retry_at=0,
        )
        self.state["backfill"][icao] = {"done": total, "days": days, "finished": True}
        self.state["phase"] = "idle"
        if total:
            try:
                from . import api
                api.clear_cache()
            except Exception:
                pass
        return {"icao": icao, "status": "ok", "inserted": total, "backfill": True}


# ---------------------------------------------------------------- 单例

_worker: SyncWorker | None = None


def start_worker() -> SyncWorker:
    global _worker
    if _worker is None:
        _worker = SyncWorker()
        _worker.start()
    return _worker


def get_worker() -> SyncWorker | None:
    return _worker
