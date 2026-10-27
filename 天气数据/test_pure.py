"""纯函数单测（零依赖，标准库 unittest）。

覆盖的是 HANDOFF.md 里"重踩一次要半天"的那些纯逻辑：站名清洗、覆盖率、
状态判定、日聚合、时区换算、上游 CSV/JSON 解析。跑法：

    python test_pure.py

不碰数据库、不联网。改这些模块前先跑一遍，改完再跑一遍。
"""

from __future__ import annotations

import calendar
import unittest

from app import config, daily, naming, source, tzutil


# ---------------------------------------------------------------- 站名清洗


class TestNaming(unittest.TestCase):
    def test_documented_examples(self):
        # HANDOFF 第 5 节的对照表，一条都不能漂
        self.assertEqual(naming.clean_site_name("Shanghai/Pudong Intl", "X"), "Shanghai Pudong")
        self.assertEqual(naming.clean_site_name("Guangzhou/Baiyun Intl", "X"), "Guangzhou Baiyun")
        self.assertEqual(naming.clean_site_name("Beijing Intl", "X"), "Beijing")
        self.assertEqual(naming.clean_site_name("Frankfurt/Main Arpt", "X"), "Frankfurt Main")

    def test_type_word_variants(self):
        # 带标点的写法也要命中
        self.assertEqual(naming.clean_site_name("Beijing Intl.", "X"), "Beijing")
        self.assertEqual(naming.clean_site_name("Beijing INT'L", "X"), "Beijing")
        self.assertEqual(naming.clean_site_name("Beijing International Airport", "X"), "Beijing")

    def test_fallback(self):
        # 全是类型词 / 空值时不能返回空串，要落回 fallback
        self.assertEqual(naming.clean_site_name("Intl", "ZBAA"), "ZBAA")
        self.assertEqual(naming.clean_site_name(None, "ZGGG"), "ZGGG")
        self.assertEqual(naming.clean_site_name("", ""), "Unknown")


# ---------------------------------------------------------------- 覆盖率 / 断点 / 间隔


class TestCoverage(unittest.TestCase):
    def test_no_obs(self):
        self.assertEqual(daily.coverage_ratio(0, 86400, []), 0.0)

    def test_full_day(self):
        times = list(range(0, 86401, 1800))
        self.assertEqual(daily.coverage_ratio(0, 86400, times), 1.0)

    def test_big_hole(self):
        # 00:00 / 01:00 / 23:00 / 24:00：中间 22 小时是窟窿，只有两端各 1 小时算覆盖
        times = [0, 3600, 23 * 3600, 86400]
        self.assertEqual(daily.coverage_ratio(0, 86400, times), round(7200 / 86400, 3))

    def test_mixed_granularity(self):
        # HANDOFF 里的失真案例：早上按小时回填、下午 5 分钟采样，时间覆盖应接近满
        times = list(range(0, 13 * 3600, 3600)) + list(range(13 * 3600, 18 * 3600 + 1, 300))
        self.assertGreater(daily.coverage_ratio(0, 18 * 3600, times), 0.99)

    def test_stale_tail_not_covered(self):
        # 最后一条之后停更超过 90 分钟：尾巴不算覆盖
        times = list(range(0, 6 * 3600, 1800))
        self.assertLess(daily.coverage_ratio(0, 10 * 3600, times), 0.7)


class TestIntervalsAndGaps(unittest.TestCase):
    def test_median_interval(self):
        self.assertEqual(daily.median_interval([0, 1800, 3600, 5400, 7200]), 1800)
        self.assertEqual(daily.median_interval([0, 3600, 7200, 10800]), 3600)
        # 样本太少 → 兜底默认值
        self.assertEqual(daily.median_interval([0, 3600]), config.SAMPLE_INTERVAL)

    def test_detect_gaps(self):
        self.assertEqual(daily.detect_gaps([0, 100, 200]), [])
        # 阈值是"超过"才断：正好 90 分钟不断
        self.assertEqual(daily.detect_gaps([0, config.GAP_THRESHOLD]), [])
        self.assertEqual(daily.detect_gaps([0, config.GAP_THRESHOLD + 1]),
                         [[0, config.GAP_THRESHOLD + 1]])


# ---------------------------------------------------------------- 状态判定


def classify(**overrides):
    kwargs = dict(is_today=True, obs_count=3, expected=48, coverage=0.9,
                  gaps=[], latest_temp=25.0, max_temp=25.0, sampled_max=25.0,
                  last_obs=1000, now=2000)
    kwargs.update(overrides)
    return daily._classify(**kwargs)


class TestClassify(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(classify(max_temp=None), "empty")

    def test_stale(self):
        self.assertEqual(classify(last_obs=0, now=config.STALE_AFTER + 2), "stale")

    def test_rising_vs_official_max(self):
        # 官方极值 26 高于任何采样点、但最新采样就是采样最大值：仍在上升。
        # 原先拿官方值比，会把升温中的上午误判成"已见顶"。
        self.assertEqual(classify(max_temp=26.0, sampled_max=25.0, latest_temp=25.0), "rising")

    def test_settled(self):
        self.assertEqual(classify(sampled_max=25.0, latest_temp=24.0), "settled")
        # 没有采样最大值时退回 max_temp 本身
        self.assertEqual(classify(sampled_max=None, latest_temp=24.0, max_temp=25.0), "settled")

    def test_only_official_no_obs(self):
        self.assertEqual(classify(obs_count=0, max_temp=26.0), "stale")
        self.assertEqual(classify(obs_count=0, max_temp=26.0, is_today=False), "final")

    def test_history_partial_and_final(self):
        base = dict(is_today=False, obs_count=48, expected=48, last_obs=1000, now=2000)
        self.assertEqual(daily._classify(**base, latest_temp=None, max_temp=30.0,
                                         sampled_max=30.0, coverage=0.3, gaps=[]), "partial")
        self.assertEqual(daily._classify(**base, latest_temp=None, max_temp=30.0,
                                         sampled_max=30.0, coverage=0.9,
                                         gaps=[[0, 4 * 3600]]), "partial")
        self.assertEqual(daily._classify(**base, latest_temp=None, max_temp=30.0,
                                         sampled_max=30.0, coverage=0.9, gaps=[]), "final")


# ---------------------------------------------------------------- 日聚合


class TestBuildDaily(unittest.TestCase):
    TZ = "Asia/Shanghai"

    def _build(self, temps, extremes=None, minutes_after_last=10):
        # now 取"最后一笔观测后 10 分钟"，否则夹具自己就触发 stale 了
        date = "2026-10-24"
        day_start = tzutil.day_start_epoch(date, self.TZ)
        times = [day_start + (k + 1) * 1800 for k in range(len(temps))]
        now = times[-1] + minutes_after_last * 60
        rows = [
            {"icao": "ZSPD", "local_date": date, "obs_time": times[k], "temp_c": t}
            for k, t in enumerate(temps)
        ]
        out = daily.build_daily(rows, {"ZSPD": self.TZ}, now, extremes or {})
        self.assertEqual(len(out), 1)
        return out[0]

    def test_sampled_only(self):
        row = self._build([20.0, 21.0, 23.0, 22.0])
        self.assertEqual(row["maxTemp"], 23.0)
        self.assertEqual(row["maxSource"], "sampled")
        self.assertEqual(row["maxTimeFirst"], row["lastObs"] - 1800)  # 23.0 那一笔
        self.assertEqual(row["isToday"], True)
        self.assertEqual(row["status"], "settled")
    def test_official_overrides(self):
        extreme = {("ZSPD", "2026-10-24"): {
            "icao": "ZSPD", "local_date": "2026-10-24",
            "max_temp": 30.7, "min_temp": 18.2, "obs_time": 0, "updated_at": 0,
        }}
        row = self._build([20.0, 25.0, 25.0], extremes=extreme)
        # 官方值优先；没采到 30.7 就不能编一个时刻出来
        self.assertEqual(row["maxTemp"], 30.7)
        self.assertEqual(row["minTemp"], 18.2)
        self.assertEqual(row["maxSource"], "official")
        self.assertEqual(row["maxTimes"], [])
        self.assertIsNone(row["maxTimeFirst"])
        self.assertEqual(row["sampledMax"], 25.0)
        # 最新采样 == 采样最大值 → 仍在上升（不被官方 30.7 干扰）
        self.assertEqual(row["status"], "rising")

    def test_official_lower_than_sampled_keeps_sampled_max_time(self):
        # 官方只给"到目前为止"的极值，理论上不会低于采样最大值；万一出现，
        # maxTime 仍按采样值算（temps 里 25.0 出现两次 → 首现取较早）
        extreme = {("ZSPD", "2026-10-24"): {
            "icao": "ZSPD", "local_date": "2026-10-24",
            "max_temp": 24.0, "min_temp": None, "obs_time": 0, "updated_at": 0,
        }}
        row = self._build([25.0, 25.0], extremes=extreme)
        self.assertEqual(row["maxTemp"], 24.0)  # 官方优先是硬规则
        self.assertEqual(row["maxTimes"], [])


# ---------------------------------------------------------------- 时区


class TestTzUtil(unittest.TestCase):
    TZ = "Asia/Shanghai"

    def test_day_roundtrip(self):
        start = tzutil.day_start_epoch("2026-10-25", self.TZ)
        self.assertEqual(tzutil.to_local_date(start, self.TZ), "2026-10-25")
        self.assertEqual(tzutil.to_local_date(start - 1, self.TZ), "2026-10-24")
        # 中国无夏令时：次日 00:00 恰好 +86400
        self.assertEqual(tzutil.next_day_start("2026-10-25", self.TZ), start + 86400)

    def test_fmt(self):
        start = tzutil.day_start_epoch("2026-10-25", self.TZ)
        self.assertEqual(tzutil.fmt_hm(start, self.TZ), "00:00")
        self.assertEqual(tzutil.fmt_hm(start + 3600 + 25 * 60, self.TZ), "01:25")

    def test_recent_dates(self):
        start = tzutil.day_start_epoch("2026-10-25", self.TZ)
        dates = tzutil.recent_dates(self.TZ, 3, at=start + 100)
        self.assertEqual(dates, ["2026-10-23", "2026-10-24", "2026-10-25"])


# ---------------------------------------------------------------- 上游解析


class TestHkoParsing(unittest.TestCase):
    def test_time(self):
        stamp = source._parse_hko_time("202610222130")
        expect = calendar.timegm((2026, 10, 22, 21, 30, 0, 0, 0, 0)) - 8 * 3600
        self.assertEqual(stamp, expect)

    def test_time_garbage(self):
        self.assertIsNone(source._parse_hko_time(""))
        self.assertIsNone(source._parse_hko_time("abc"))
        self.assertIsNone(source._parse_hko_time("20261022"))       # 8 位
        self.assertIsNone(source._parse_hko_time("999999999999"))   # 12 位但不是日期

    def test_value(self):
        self.assertEqual(source._parse_hko_value("24.1*"), 24.1)    # 星号 = 数据不完整
        self.assertEqual(source._parse_hko_value(" 25.0 "), 25.0)
        self.assertIsNone(source._parse_hko_value(""))
        self.assertIsNone(source._parse_hko_value("*"))
        self.assertIsNone(source._parse_hko_value("N/A"))


class TestNormalizeRecords(unittest.TestCase):
    def test_filter_dedup_order(self):
        records = [
            {"icao": "ZSPD", "obs_time": 200, "temp": 2},   # 上游倒序：先出现的是新报（COR）
            {"icao": "ZSPD", "obs_time": 200, "temp": 1},   # 旧的重复报，应被丢弃
            {"icao": "ZGGG", "obs_time": 150, "temp": 9},   # 他站，过滤掉
            {"icao": "ZSPD", "obs_time": 100, "temp": 0},
            {"icao": "", "obs_time": 50, "temp": 5},        # 缺站号的按本站保留
        ]
        out = source._normalize_records(records, "zspd")    # 大小写不敏感
        self.assertEqual([r["obs_time"] for r in out], [50, 100, 200])
        self.assertEqual(out[-1]["temp"], 2)                # 更正报（新值）被保留


class TestOpenerPreference(unittest.TestCase):
    """按域名记忆"代理 or 直连谁先"：两个上游的最优路径完全相反（实测），
    一刀切必然慢一边。"""

    def test_seeded_by_measurement(self):
        # 香港的域直连优先（走系统代理要 6~18 秒，直连亚秒级）
        self.assertTrue(source._openers_for("https://data.weather.gov.hk/a.csv")[0][1])
        self.assertTrue(source._openers_for("https://www.hko.gov.hk/wxinfo/b.csv")[0][1])
        # 其余维持旧行为：先走系统代理（aviationweather 直连要 16 秒）
        self.assertFalse(source._openers_for("https://aviationweather.gov/api/data/metar")[0][1])

    def test_remember_flips(self):
        url = "https://example.test/feed.csv"
        try:
            self.assertFalse(source._openers_for(url)[0][1])
            source._remember_opener(url, True)   # 直连成功过 → 下次直连优先
            self.assertTrue(source._openers_for(url)[0][1])
            source._remember_opener(url, False)  # 代理成功过 → 翻回来
            self.assertFalse(source._openers_for(url)[0][1])
        finally:
            source._remember_opener(url, False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
