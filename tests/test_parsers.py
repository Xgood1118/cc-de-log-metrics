# -*- coding: utf-8 -*-
"""三种日志格式的解析测试。"""
import json
import os
import sys
import unittest
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from logm.core import (
    parse_app,
    parse_line,
    parse_lines,
    parse_nginx,
    parse_syslog,
    parse_utc_offset,
)


class NginxTest(unittest.TestCase):
    def test_truncated_request_uses_status_outside_quotes(self):
        # request 里的 "200" 只是被截断的请求文本，真实状态码是引号外的 201。
        e = parse_nginx(
            '10.0.0.2 - - [02/Feb/2026:10:00:05 +0800] '
            '"POST /b 200 8" 201 128')
        self.assertEqual(e["status"], "201")
        self.assertEqual(e["status_code"], 201)
        self.assertEqual(e["method"], "POST")
        self.assertEqual(e["path"], "/b")

    def test_time_is_converted_to_utc(self):
        e = parse_nginx(
            '10.0.0.1 - - [02/Feb/2026:10:00:01 +0800] "GET /a 200 12" 200 512')
        self.assertEqual(e["ts"], datetime(2026, 2, 2, 2, 0, 1,
                                           tzinfo=timezone.utc))
        self.assertEqual(e["time"], "02/Feb/2026:10:00:01 +0800")
        self.assertEqual(e["status_code"], 200)
        self.assertEqual(e["bytes"], 512)

    def test_negative_offset(self):
        e = parse_nginx(
            '1.2.3.4 - - [02/Feb/2026:10:00:01 -0500] "GET /a" 200 1 "-" "UA"')
        self.assertEqual(e["ts"], datetime(2026, 2, 2, 15, 0, 1,
                                           tzinfo=timezone.utc))
        self.assertEqual(e["user_agent"], "UA")

    def test_broken_lines(self):
        self.assertIsNone(parse_nginx("this line is broken"))
        self.assertIsNone(parse_nginx(""))
        self.assertIsNone(parse_nginx(
            '1.2.3.4 - - [02/Bogus/2026:10:00:01 +0800] "GET /a" 200 1'))

    def test_dash_fields(self):
        e = parse_nginx('1.2.3.4 - - [02/Feb/2026:10:00:01 +0000] "-" - -')
        self.assertIsNone(e["status_code"])
        self.assertIsNone(e["method"])
        self.assertIsNone(e["path"])
        self.assertIsNone(e["bytes"])


class AppJsonTest(unittest.TestCase):
    def test_iso_with_offset_becomes_utc(self):
        e = parse_app('{"ts": "2026-02-02T10:00:02.100+08:00", "level": "INFO"}')
        self.assertEqual(e["ts"], datetime(2026, 2, 2, 2, 0, 2, 100000,
                                           tzinfo=timezone.utc))

    def test_iso_z_and_other_offsets(self):
        z = parse_app('{"ts": "2026-02-02T02:00:02.100Z"}')
        plus = parse_app('{"ts": "2026-02-02T02:00:02.100+00:00"}')
        west = parse_app('{"ts": "2026-02-01T21:00:02.100-05:00"}')
        self.assertEqual(z["ts"], plus["ts"])
        self.assertEqual(z["ts"], west["ts"])

    def test_naive_timestamp_rejected(self):
        self.assertIsNone(parse_app('{"ts": "2026-02-02T10:00:02"}'))

    def test_invalid_json(self):
        self.assertIsNone(parse_app("{not json"))
        self.assertIsNone(parse_app("[1, 2, 3]"))
        self.assertIsNone(parse_app('{"level": "INFO"}'))

    def test_optional_metric_fields(self):
        e = parse_app(json.dumps({
            "ts": "2026-02-02T10:00:02+08:00", "status": 503,
            "path": "/x", "ip": "10.0.0.7"}))
        self.assertEqual(e["status_code"], 503)
        self.assertEqual(e["path"], "/x")
        self.assertEqual(e["ip"], "10.0.0.7")


class SyslogTest(unittest.TestCase):
    def test_month_abbrev_and_year(self):
        e = parse_syslog("Feb  2 10:00:03 host1 sshd[123]: Accepted password")
        self.assertEqual(e["ts"], datetime(2026, 2, 2, 10, 0, 3,
                                           tzinfo=timezone.utc))
        self.assertEqual(e["host"], "host1")
        self.assertEqual(e["program"], "sshd")
        self.assertEqual(e["pid"], 123)
        self.assertEqual(e["message"], "Accepted password")
        self.assertEqual(e["time"], "Feb  2 10:00:03")

    def test_configured_year_and_zone(self):
        e = parse_syslog("Dec 31 23:59:59 host1 app: bye", year=2025,
                         tz=parse_utc_offset("+08:00"))
        self.assertEqual(e["ts"], datetime(2025, 12, 31, 15, 59, 59,
                                           tzinfo=timezone.utc))

    def test_pri_prefix_and_ip_host(self):
        e = parse_syslog("<34>Feb  2 10:00:03 10.1.2.3 postfix: up")
        self.assertEqual(e["ip"], "10.1.2.3")
        self.assertEqual(e["program"], "postfix")

    def test_broken(self):
        self.assertIsNone(parse_syslog("2026-02-02T10:00:03Z not syslog"))
        self.assertIsNone(parse_syslog("Feb 32 10:00:03 host app: bad day"))


class ParseLinesTest(unittest.TestCase):
    def test_unparsed_is_reported_not_dropped(self):
        lines = [
            '10.0.0.1 - - [02/Feb/2026:10:00:01 +0800] "GET /a 200 12" 200 512',
            "this line is broken and should be counted as unparsed",
            "",
            '10.0.0.2 - - [02/Feb/2026:10:00:05 +0800] "POST /b" 201 1',
        ]
        errors = []
        entries = parse_lines(lines, "nginx", errors=errors, stream="access.log")
        self.assertEqual(len(entries), 2)
        # 空行不算 unparsed
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["line"], 2)
        self.assertEqual(errors[0]["format"], "nginx")
        self.assertIn("broken", errors[0]["text"])
        self.assertEqual({e["stream"] for e in entries}, {"access.log"})

    def test_each_format_through_parse_line(self):
        self.assertEqual(parse_line("Feb  2 10:00:03 h a: b", "syslog")["format"],
                         "syslog")
        self.assertEqual(parse_line('{"ts":"2026-02-02T00:00:00Z"}', "app")["format"],
                         "app")
        self.assertIsNone(parse_line("nope", "app"))
        with self.assertRaises(ValueError):
            parse_line("x", "nope")


if __name__ == "__main__":
    unittest.main()
