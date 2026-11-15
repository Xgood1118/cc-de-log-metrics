# -*- coding: utf-8 -*-
"""CLI 端到端测试：使用仓库自带的三个日志文件（只读，不修改）。"""
import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_cli(*args):
    proc = subprocess.run(
        [sys.executable, "-m", "logm.cli"] + list(args),
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
    )
    return proc


def run_json(*args):
    proc = run_cli(*args)
    return proc, json.loads(proc.stdout)


class ExitCodeTest(unittest.TestCase):
    def test_unparsed_exits_1(self):
        proc = run_cli("access.log", "--window", "1m")
        self.assertEqual(proc.returncode, 1)

    def test_all_parsed_exits_0(self):
        self.assertEqual(run_cli("app.jsonl", "--window", "1m").returncode, 0)
        self.assertEqual(run_cli("syslog.log", "--window", "1m").returncode, 0)

    def test_bad_arguments_exit_2(self):
        self.assertEqual(run_cli().returncode, 2)
        self.assertEqual(run_cli("access.log").returncode, 2)  # 缺 --window
        self.assertEqual(run_cli("access.log", "--window", "nope").returncode, 2)
        self.assertEqual(run_cli("access.log", "--window", "1m",
                                 "--step", "2m").returncode, 2)
        self.assertEqual(run_cli("missing.log", "--window", "1m").returncode, 2)
        self.assertEqual(run_cli("access.log", "--window", "1m",
                                 "--format", "bogus").returncode, 2)


class ReportTest(unittest.TestCase):
    def test_access_window_metrics_and_unparsed(self):
        proc, data = run_json("access.log", "--window", "1m")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(data["unparsed"]["total"], 1)
        self.assertEqual(data["unparsed"]["by_format"]["nginx"], 1)
        self.assertIn("broken", data["unparsed"]["samples_by_format"]["nginx"][0]["text"])

        windows = data["windows"]
        self.assertEqual([w["window_start"] for w in windows], [
            "2026-02-02T02:00:00Z",
            "2026-02-02T02:01:00Z",
            "2026-02-02T02:02:00Z",
        ])
        self.assertEqual([w["requests"] for w in windows], [2, 1, 2])
        # /b 的真实状态码 201 来自引号外字段
        self.assertEqual(windows[0]["status_counts"], {"200": 1, "201": 1})
        self.assertEqual(windows[1]["errors"], 1)
        self.assertEqual(windows[1]["error_rate"], 1.0)

    def test_syslog_year_completion(self):
        proc, data = run_json("syslog.log", "--window", "1m")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual([w["window_start"] for w in data["windows"]], [
            "2026-02-02T10:00:00Z",
            "2026-02-02T10:01:00Z",
        ])

    def test_app_late_record_lands_in_first_window(self):
        proc, data = run_json("app.jsonl", "--window", "1m")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(data["too_late"]["count"], 0)
        self.assertEqual(len(data["windows"]), 1)
        # 4 条记录：02.100、31.000、05.400、01.900，都在 02:00 窗口
        self.assertEqual(data["windows"][0]["requests"], 4)

    def test_merged_formats_are_labelled(self):
        proc, data = run_json("access.log", "app.jsonl", "syslog.log",
                              "--window", "5m")
        self.assertEqual(proc.returncode, 1)
        by_format = {}
        for window in data["windows"]:
            for fmt, stats in window["formats"].items():
                by_format[fmt] = by_format.get(fmt, 0) + stats["requests"]
        self.assertEqual(by_format, {"nginx": 5, "app": 4, "syslog": 2})
        self.assertEqual([f["format"] for f in data["files"]],
                         ["nginx", "app", "syslog"])

    def test_sliding_window_output(self):
        proc, data = run_json("app.jsonl", "--window", "1m", "--step", "30s",
                              "--allowed-lateness", "10s")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(data["mode"], "sliding")
        self.assertEqual(data["window_seconds"], 60)
        self.assertEqual(data["step_seconds"], 30)
        # 10:00:31 先到，10:00:05.4 / 10:00:01.9 迟到超过 10s，计入 too_late
        self.assertEqual(data["too_late"]["count"], 2)
        self.assertEqual([w["window_start"] for w in data["windows"]], [
            "2026-02-02T01:59:30Z",
            "2026-02-02T02:00:00Z",
            "2026-02-02T02:00:30Z",
        ])
        # 仅 02.100 与 31.000 被接受；01:59:30 窗口只含前者。
        self.assertEqual([w["requests"] for w in data["windows"]], [1, 2, 1])

    def test_output_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "report.json")
            proc = run_cli("access.log", "--window", "1m", "--output", out)
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(proc.stdout, "")
            with open(out, encoding="utf-8") as handle:
                data = json.load(handle)
            self.assertEqual(len(data["windows"]), 3)


if __name__ == "__main__":
    unittest.main()
