# -*- coding: utf-8 -*-
"""日志解析与指标聚合。

骨架：只解析 nginx combined 格式，按分钟计数，按文件顺序累加。
"""
import re

PATTERN = re.compile(
    r'([0-9.]+) - - \[([^\]]+)\] "([A-Z]+) ([^ ]+) [^"]*" ([0-9]+) ([0-9]+)')


def parse_nginx(line):
    m = PATTERN.match(line.strip())
    if not m:
        return None
    return {"ip": m.group(1), "time": m.group(2), "method": m.group(3),
            "path": m.group(4), "status": m.group(5)}


def parse_lines(lines):
    out = []
    for line in lines:
        e = parse_nginx(line)
        if e:
            out.append(e)
    return out


def count_by_minute(entries):
    """按分钟聚合请求数。时间字段形如 02/Jan/2026:10:00:01 +0800。"""
    buckets = {}
    for e in entries:
        minute = e["time"][:17]
        buckets[minute] = buckets.get(minute, 0) + 1
    return buckets
