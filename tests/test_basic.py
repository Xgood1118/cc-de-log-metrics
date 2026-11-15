# -*- coding: utf-8 -*-
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from logm.core import count_by_minute, parse_nginx


class ParseTest(unittest.TestCase):
    def test_nginx_line(self):
        e = parse_nginx('10.0.0.1 - - [02/Feb/2026:10:00:01 +0800] "GET /a 200 12" 200 512')
        self.assertEqual(e["method"], "GET")
        self.assertEqual(e["status"], "200")

    def test_broken_line(self):
        self.assertIsNone(parse_nginx("this line is broken"))


class BucketTest(unittest.TestCase):
    def test_buckets(self):
        entries = [
            {"time": "02/Feb/2026:10:00:01"},
            {"time": "02/Feb/2026:10:00:05"},
            {"time": "02/Feb/2026:10:01:02"},
        ]
        b = count_by_minute(entries)
        self.assertEqual(len(b), 2)
        self.assertEqual(list(b.values())[0], 2)


if __name__ == "__main__":
    unittest.main()
