# -*- coding: utf-8 -*-
"""命令行入口：按事件时间把多种格式的日志聚合成窗口指标。

示例::

    python -m logm.cli access.log app.jsonl syslog.log --window 5m
    python -m logm.cli app.jsonl --window 1m --step 30s --allowed-lateness 10s
    python -m logm.cli access.log --window 1m --format nginx --top 3

退出码：0 全部解析成功；1 存在 unparsed 行；2 参数错误。
"""
from __future__ import annotations

import argparse
import json
import sys

from .core import (
    DEFAULT_SAMPLES,
    DEFAULT_SYSLOG_YEAR,
    aggregate,
    detect_format,
    parse_duration,
    parse_lines,
    parse_utc_offset,
)

FORMAT_CHOICES = ("auto", "nginx", "app", "syslog")


class _ArgumentParser(argparse.ArgumentParser):
    """argparse 默认用 status 2 退出，与要求一致；这里只统一文案。"""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write("%s: error: %s\n" % (self.prog, message))
        raise SystemExit(2)


def build_parser():
    parser = _ArgumentParser(
        prog="python -m logm.cli",
        description="多格式日志解析与按事件时间滚动的指标聚合",
    )
    parser.add_argument("files", nargs="+", metavar="FILE",
                        help="一个或多个日志文件")
    parser.add_argument("-w", "--window", required=True, metavar="DURATION",
                        help="窗口长度，例如 1m、5m、90s")
    parser.add_argument("-s", "--step", default=None, metavar="DURATION",
                        help="滑动窗口步长，例如 30s；省略则为滚动窗口")
    parser.add_argument("--allowed-lateness", default="30s", metavar="DURATION",
                        help="允许迟到时长（默认 30s），超过的记为 too_late")
    parser.add_argument("--top", dest="top_n", type=int, default=5,
                        metavar="N", help="每个窗口输出前 N 个路径（默认 5）")
    parser.add_argument("--format", dest="fmt", choices=FORMAT_CHOICES,
                        default="auto", help="强制使用某种格式，默认按文件名/内容识别")
    parser.add_argument("--syslog-year", type=int, default=DEFAULT_SYSLOG_YEAR,
                        metavar="YEAR", help="syslog 缺失年份的补全值（默认 %d）"
                        % DEFAULT_SYSLOG_YEAR)
    parser.add_argument("--syslog-tz", default="UTC", metavar="OFFSET",
                        help="syslog 本地时间时区，如 +08:00（默认 UTC）")
    parser.add_argument("--unparsed-samples", type=int, default=DEFAULT_SAMPLES,
                        metavar="N", help="每种来源保留的未解析样例数（默认 %d）"
                        % DEFAULT_SAMPLES)
    parser.add_argument("-o", "--output", default=None, metavar="FILE",
                        help="把 JSON 写入文件，默认写到标准输出")
    return parser


def _read_lines(path):
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read().splitlines()


def _resolve_format(path, lines, forced):
    if forced and forced != "auto":
        return forced
    fmt = detect_format(path, lines)
    if fmt is None:
        raise ValueError(
            "无法识别日志格式: %s（请用 --format 指定 nginx/app/syslog）" % path)
    return fmt


def run(args):
    try:
        window = parse_duration(args.window)
        step = parse_duration(args.step) if args.step else None
        allowed_lateness = parse_duration(args.allowed_lateness)
        syslog_tz = parse_utc_offset(args.syslog_tz)
    except ValueError as exc:
        raise ValueError("参数错误: %s" % exc)
    if args.top_n < 0:
        raise ValueError("参数错误: --top 不能为负数")
    if args.unparsed_samples < 0:
        raise ValueError("参数错误: --unparsed-samples 不能为负数")
    if args.syslog_year < 1 or args.syslog_year > 9999:
        raise ValueError("参数错误: --syslog-year 超出范围")

    entries = []
    errors = []
    files = []
    for path in args.files:
        try:
            lines = _read_lines(path)
        except OSError as exc:
            raise ValueError("无法读取文件 %s: %s" % (path, exc))
        try:
            fmt = _resolve_format(path, lines, args.fmt)
        except ValueError as exc:
            raise ValueError("参数错误: %s" % exc)

        before = len(errors)
        parsed = parse_lines(lines, fmt, syslog_year=args.syslog_year,
                             syslog_tz=syslog_tz, errors=errors, stream=path)
        entries.extend(parsed)
        files.append({
            "file": path,
            "format": fmt,
            "parsed": len(parsed),
            "unparsed": len(errors) - before,
        })

    result = aggregate(
        entries,
        window=window,
        step=step,
        allowed_lateness=allowed_lateness,
        top_n=args.top_n,
        too_late_samples=args.unparsed_samples,
    )

    # 按格式汇总 unparsed；样例也按格式分开保留前 N 条。
    by_format = {}
    samples_by_format = {}
    for fmt in ("nginx", "app", "syslog"):
        by_format[fmt] = 0
        samples_by_format[fmt] = []
    for err in errors:
        fmt = err.get("format")
        if fmt is None:
            fmt = next((item["format"] for item in files
                        if item["file"] == err.get("file")), "unknown")
        by_format[fmt] = by_format.get(fmt, 0) + 1
        bucket = samples_by_format.setdefault(fmt, [])
        if len(bucket) < args.unparsed_samples:
            bucket.append({"file": err.get("file"),
                           "line": err.get("line"),
                           "text": err.get("text")})

    result["files"] = files
    result["unparsed"] = {
        "total": len(errors),
        "by_format": by_format,
        "samples_by_format": samples_by_format,
        "samples": [dict(err) for err in errors[:args.unparsed_samples]],
    }
    return result


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # --help 为 0，参数错误为 2
        return exc.code if isinstance(exc.code, int) else 2

    try:
        result = run(args)
    except ValueError as exc:
        sys.stderr.write("%s\n" % exc)
        return 2

    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        try:
            with open(args.output, "w", encoding="utf-8") as handle:
                handle.write(text + "\n")
        except OSError as exc:
            sys.stderr.write("无法写入 %s: %s\n" % (args.output, exc))
            return 2
    else:
        sys.stdout.write(text + "\n")
    return 1 if result["unparsed"]["total"] else 0


if __name__ == "__main__":
    sys.exit(main())
