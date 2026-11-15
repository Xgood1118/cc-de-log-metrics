# -*- coding: utf-8 -*-
"""窗口、事件时间、迟到与错误率的测试。"""
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from logm.core import aggregate, normalize_ip, parse_duration


def ev(ts, fmt="app", status=None, ip=None, path=None, stream="s",
       line=1, raw="raw"):
    """构造一个已解析事件；ts 为 HH:MM:SS.mmm 或 datetime。"""
    if not isinstance(ts, datetime):
        text = "2026-02-02T%s" % ts
        ts = datetime.fromisoformat(
            text.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    return {
        "format": fmt, "ts": ts.astimezone(timezone.utc), "status_code": status,
        "ip": ip, "path": path, "stream": stream, "line": line, "raw": raw,
    }


def starts(result):
    return [w["window_start"] for w in result["windows"]]


class TumblingWindowTest(unittest.TestCase):
    def test_events_bucket_by_event_time_not_file_order(self):
        entries = [
            ev("02:00:05.400"),
            ev("02:00:01.900"),  # 乱序，但在允许迟到内
            ev("02:01:00.000"),  # 恰好落在下一个窗口边界
        ]
        result = aggregate(entries, window=timedelta(minutes=1),
                           step=None, allowed_lateness=timedelta(seconds=30))
        self.assertEqual(starts(result), ["2026-02-02T02:00:00Z",
                                          "2026-02-02T02:01:00Z"])
        self.assertEqual([w["requests"] for w in result["windows"]], [2, 1])
        self.assertEqual(result["too_late"]["count"], 0)
        self.assertEqual(result["mode"], "tumbling")

    def test_window_boundaries_are_half_open(self):
        entries = [
            ev("02:00:00.000"),
            ev("02:00:59.999"),
            ev("02:01:00.000"),
        ]
        result = aggregate(entries, window="1m", allowed_lateness="30s")
        self.assertEqual([w["requests"] for w in result["windows"]], [2, 1])
        first = result["windows"][0]
        self.assertEqual(first["window_start"], "2026-02-02T02:00:00Z")
        self.assertEqual(first["window_end"], "2026-02-02T02:01:00Z")

    def test_sorted_by_window_start(self):
        result = aggregate([ev("02:02:00"), ev("02:00:00"), ev("02:01:00")],
                           window="1m", allowed_lateness="1h")
        self.assertEqual(starts(result), [
            "2026-02-02T02:00:00Z",
            "2026-02-02T02:01:00Z",
            "2026-02-02T02:02:00Z",
        ])


class SlidingWindowTest(unittest.TestCase):
    def test_one_event_lands_in_every_overlapping_window(self):
        # 1m 窗口 / 30s 步长：02:00:45 落在 02:00:00 与 02:00:30 两个窗口；
        # 01:59:30 窗口右边界是 02:00:30，不含它。
        result = aggregate([ev("02:00:45")], window="1m", step="30s",
                           allowed_lateness="30s")
        self.assertEqual(starts(result), [
            "2026-02-02T02:00:00Z",
            "2026-02-02T02:00:30Z",
        ])
        self.assertEqual([w["requests"] for w in result["windows"]], [1, 1])
        self.assertEqual(result["mode"], "sliding")
        self.assertEqual(result["window_seconds"], 60)
        self.assertEqual(result["step_seconds"], 30)

    def test_each_event_in_four_windows_with_two_minute_window(self):
        # 2m 窗口 / 30s 步长：02:00:45 落在 01:59:00、01:59:30、02:00:00、02:00:30。
        result = aggregate([ev("02:00:45")], window="2m", step="30s",
                           allowed_lateness="1h")
        self.assertEqual(starts(result), [
            "2026-02-02T01:59:00Z",
            "2026-02-02T01:59:30Z",
            "2026-02-02T02:00:00Z",
            "2026-02-02T02:00:30Z",
        ])
        self.assertEqual([w["requests"] for w in result["windows"]], [1] * 4)

    def test_exclusive_boundary(self):
        # 02:00:00 恰好是 01:59:00 窗口的右边界（不含），所以不出现在该窗口。
        result = aggregate([ev("02:00:00")], window="1m", step="30s",
                           allowed_lateness="1h")
        self.assertEqual(starts(result), [
            "2026-02-02T01:59:30Z",
            "2026-02-02T02:00:00Z",
        ])

    def test_step_must_not_exceed_window(self):
        with self.assertRaises(ValueError):
            aggregate([ev("02:00:00")], window="1m", step="2m")


class LatenessTest(unittest.TestCase):
    def test_exactly_at_lateness_is_accepted(self):
        entries = [ev("02:00:31"), ev("02:00:01", line=2)]  # 落后 30s
        result = aggregate(entries, window="1m", allowed_lateness="30s")
        self.assertEqual(result["too_late"]["count"], 0)
        self.assertEqual(sum(w["requests"] for w in result["windows"]), 2)

    def test_beyond_lateness_is_too_late(self):
        entries = [ev("02:00:32"), ev("02:00:01", line=2)]  # 落后 31s
        result = aggregate(entries, window="1m", allowed_lateness="30s")
        self.assertEqual(result["too_late"]["count"], 1)
        self.assertEqual(result["too_late"]["by_format"]["app"], 1)
        sample = result["too_late"]["samples"][0]
        self.assertEqual(sample["ts"], "2026-02-02T02:00:01Z")
        self.assertEqual(sample["line"], 2)
        # 迟到事件不进入任何窗口
        self.assertEqual(sum(w["requests"] for w in result["windows"]), 1)

    def test_late_event_still_enters_its_own_window(self):
        # 样例数据：10:00:31 先到，10:00:05.400 之后才到，落后 25.6s，应被接受。
        entries = [
            ev("02:00:02.100"),
            ev("02:00:31.000", line=2),
            ev("02:00:05.400", line=3),
            ev("02:00:01.900", line=4),
        ]
        result = aggregate(entries, window="1m", allowed_lateness="30s")
        self.assertEqual(result["too_late"]["count"], 0)
        self.assertEqual(len(result["windows"]), 1)
        self.assertEqual(result["windows"][0]["requests"], 4)

    def test_watermark_is_per_stream(self):
        # 两个文件各自维护水位线，互不把对方判成迟到。
        entries = [
            ev("02:00:01", stream="a.log", line=1),
            ev("02:05:00", stream="b.log", line=1),
            ev("02:00:02", stream="a.log", line=2),
        ]
        result = aggregate(entries, window="1m", allowed_lateness="30s")
        self.assertEqual(result["too_late"]["count"], 0)

    def test_too_late_sample_limit(self):
        entries = [ev("02:10:00")]
        entries += [ev("02:00:00", line=i) for i in range(2, 12)]
        result = aggregate(entries, window="1m", allowed_lateness="30s",
                           too_late_samples=3)
        self.assertEqual(result["too_late"]["count"], 10)
        self.assertEqual(len(result["too_late"]["samples"]), 3)


class MetricsTest(unittest.TestCase):
    def test_error_rate_and_status_counts(self):
        entries = [
            ev("02:00:01", status=200, path="/a", ip="10.0.0.1"),
            ev("02:00:02", status=500, path="/a", ip="10.0.0.2"),
            ev("02:00:03", status=503, path="/b", ip="10.0.1.9"),
            ev("02:00:04", status=404, path="/b", ip="10.0.2.9"),
        ]
        w = aggregate(entries, window="1m")["windows"][0]
        self.assertEqual(w["requests"], 4)
        self.assertEqual(w["status_counts"], {"200": 1, "404": 1,
                                              "500": 1, "503": 1})
        self.assertEqual(w["errors"], 2)
        self.assertEqual(w["error_rate"], 0.5)
        # /24 归一：10.0.0.1 与 10.0.0.2 是同一网段
        self.assertEqual(w["unique_ips"], 3)

    def test_error_rate_zero_when_no_requests(self):
        result = aggregate([], window="1m", too_late_samples=1)
        self.assertEqual(result["windows"], [])
        self.assertEqual(result["too_late"]["count"], 0)

    def test_top_paths_ordering_and_limit(self):
        entries = [
            ev("02:00:01", status=200, path="/b"),
            ev("02:00:02", status=200, path="/a"),
            ev("02:00:03", status=200, path="/b"),
            ev("02:00:04", status=200, path="/c"),
        ]
        w = aggregate(entries, window="1m", top_n=2)["windows"][0]
        self.assertEqual(w["top_paths"], [{"path": "/b", "count": 2},
                                          {"path": "/a", "count": 1}])

    def test_per_format_split_and_source_label(self):
        entries = [
            ev("02:00:01", fmt="nginx", status=500, path="/x", ip="10.0.0.1"),
            ev("02:00:02", fmt="nginx", status=200, path="/x", ip="10.0.0.2"),
            ev("02:00:03", fmt="app", status=503, path="/y", ip="10.0.0.3"),
            ev("02:00:04", fmt="syslog", ip="10.0.9.9"),
        ]
        w = aggregate(entries, window="1m")["windows"][0]
        self.assertEqual(w["requests"], 4)
        self.assertEqual(list(w["formats"]), ["nginx", "app", "syslog"])
        self.assertEqual(w["formats"]["nginx"]["requests"], 2)
        self.assertEqual(w["formats"]["nginx"]["error_rate"], 0.5)
        self.assertEqual(w["formats"]["app"]["requests"], 1)
        self.assertEqual(w["formats"]["syslog"]["requests"], 1)

    def test_ipv6_uses_64_prefix(self):
        self.assertEqual(normalize_ip("2001:db8::1"), "2001:db8::/64")
        self.assertEqual(normalize_ip("10.9.8.7"), "10.9.8.0/24")
        self.assertIsNone(normalize_ip("not-an-ip"))


class ParameterTest(unittest.TestCase):
    def test_bad_parameters(self):
        for kwargs in ({"window": "0s"}, {"window": "-1m"},
                       {"window": "1m", "step": "0s"},
                       {"window": "1m", "allowed_lateness": "-1s"}):
            with self.assertRaises(ValueError):
                aggregate([], **kwargs)

    def test_duration_parsing(self):
        self.assertEqual(parse_duration("1m"), timedelta(minutes=1))
        self.assertEqual(parse_duration("5m"), timedelta(minutes=5))
        self.assertEqual(parse_duration("90s"), timedelta(seconds=90))
        self.assertEqual(parse_duration("1m30s"), timedelta(seconds=90))
        self.assertEqual(parse_duration("500ms"), timedelta(milliseconds=500))
        with self.assertRaises(ValueError):
            parse_duration("1x")

    def test_result_json_serialisable(self):
        result = aggregate([ev("02:00:01", status=200, path="/a")],
                           window="1m")
        json.dumps(result)


if __name__ == "__main__":
    unittest.main()
