# -*- coding: utf-8 -*-
"""日志解析与按事件时间聚合的指标计算。

支持三种日志格式：

* ``nginx``：nginx combined 格式（含被截断的 request 字段）；
* ``app``：JSON Lines，时间戳为带时区的 ISO 8601；
* ``syslog``：传统 syslog，月份为英文缩写且不含年份。

所有事件时间在内部统一表示为带 UTC 时区的 :class:`datetime.datetime`。
窗口按 UTC epoch 对齐，聚合只使用事件时间，与文件中的行顺序无关；
迟到判定则按每个输入流（文件）的到达顺序维护水位线。
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
from collections import Counter
from datetime import datetime, timedelta, timezone

#: 传统 syslog 没有年份，按这个基准年份补全。
DEFAULT_SYSLOG_YEAR = 2026

#: 每个格式默认保留的 unparsed / too_late 原文样例数。
DEFAULT_SAMPLES = 5

#: 支持的格式名。
FORMATS = ("nginx", "app", "syslog")

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_US_PER_SECOND = 1_000_000
_US_PER_DAY = 86_400 * _US_PER_SECOND

#: nginx combined：
#:   ip - user [time] "request" status bytes "referer" "user-agent"
#: 注意 status 必须取引号外那个字段：样例里的 request 被写成了
#: "GET /a 200 12"，其中的 200 只是请求文本的一部分。
PATTERN = re.compile(
    r'^(?P<ip>\S+) \S+ \S+ \[(?P<time>[^\]]+)\] '
    r'"(?P<request>(?:\\.|[^"\\])*)" '
    r'(?P<status>\d{3}|-) (?P<bytes>\d+|-)'
    r'(?: "(?P<referer>(?:\\.|[^"\\])*)" '
    r'"(?P<user_agent>(?:\\.|[^"\\])*)")?'
    r'\s*$'
)

_NGINX_TIME_RE = re.compile(
    r'^(?P<day>\d{2})/(?P<month>[A-Za-z]{3})/(?P<year>\d{4}):'
    r'(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2}) '
    r'(?P<sign>[+-])(?P<off_hour>\d{2})(?P<off_minute>\d{2})$'
)

# 传统 syslog：可带 <PRI> 前缀；日期中的日可能是一位或两位。
_SYSLOG_RE = re.compile(
    r'^(?:<\d{1,3}>)?'
    r'(?P<month>[A-Za-z]{3})\s+(?P<day>\d{1,2})\s+'
    r'(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})\s+'
    r'(?P<host>\S+)\s+(?P<rest>.*)$'
)

_SYSLOG_TAG_RE = re.compile(
    r'^(?P<program>[^\s:\[\]]+)(?:\[(?P<pid>\d+)\])?:\s?(?P<message>.*)$'
)

_DURATION_PART_RE = re.compile(r'(\d+)(ms|us|s|m|h|d|w)')
_DURATION_UNITS = {
    "us": 1,
    "ms": 1_000,
    "s": _US_PER_SECOND,
    "m": 60 * _US_PER_SECOND,
    "h": 3_600 * _US_PER_SECOND,
    "d": _US_PER_DAY,
    "w": 7 * _US_PER_DAY,
}


# ---------------------------------------------------------------------------
# 时间与时长工具
# ---------------------------------------------------------------------------

def _to_us(value):
    """把 timedelta 转成整数微秒。"""
    if not isinstance(value, timedelta):
        raise TypeError("需要 datetime.timedelta")
    return (value.days * _US_PER_DAY
            + value.seconds * _US_PER_SECOND
            + value.microseconds)


def parse_duration(text):
    """解析 ``30s`` / ``1m`` / ``1m30s`` / ``500ms`` / 纯数字秒 为 timedelta。

    只接受非负整数与单位组合；0 及负值、未知单位会抛 ``ValueError``。
    """
    if isinstance(text, timedelta):
        value = text
    else:
        raw = str(text).strip().lower()
        if not raw:
            raise ValueError("时长不能为空")
        if raw.isdigit():
            raw += "s"
        if not re.fullmatch(r'(?:\d+(?:us|ms|s|m|h|d|w))+', raw):
            raise ValueError("无法解析时长 %r（示例：30s、1m、1m30s）" % text)
        total = 0
        for number, unit in _DURATION_PART_RE.findall(raw):
            total += int(number) * _DURATION_UNITS[unit]
        value = timedelta(microseconds=total)
    if _to_us(value) <= 0:
        raise ValueError("时长必须大于 0")
    return value


def parse_utc_offset(text):
    """解析 ``UTC`` / ``Z`` / ``+08:00`` / ``-0700`` 为 :class:`timezone`。"""
    raw = str(text).strip().upper()
    if raw in ("UTC", "GMT", "Z", "+00:00", "+0000", "-00:00", "-0000"):
        return timezone.utc
    m = re.fullmatch(r'([+-])(\d{2}):?(\d{2})', raw)
    if not m:
        raise ValueError("无法解析时区偏移 %r（示例：UTC、+08:00、-0700）" % text)
    hours, minutes = int(m.group(2)), int(m.group(3))
    if hours > 23 or minutes > 59:
        raise ValueError("时区偏移超出范围: %r" % text)
    delta = timedelta(hours=hours, minutes=minutes)
    if m.group(1) == "-":
        delta = -delta
    return timezone(delta)


def _iso_utc(dt):
    """输出稳定的 UTC ISO 8601 字符串（以 Z 结尾）。"""
    utc = dt.astimezone(timezone.utc)
    text = utc.strftime("%Y-%m-%dT%H:%M:%S")
    if utc.microsecond:
        text += "." + ("%06d" % utc.microsecond).rstrip("0")
    return text + "Z"


def _seconds(us):
    """整数微秒转成 JSON 友好的秒（整数优先）。"""
    return us // _US_PER_SECOND if us % _US_PER_SECOND == 0 else us / _US_PER_SECOND


def _parse_month(text):
    month = _MONTHS.get(text.lower())
    if month is None:
        raise ValueError("未知月份缩写: %r" % text)
    return month


def _parse_nginx_time(text):
    """把 ``02/Feb/2026:10:00:01 +0800`` 转成 UTC datetime。"""
    m = _NGINX_TIME_RE.match(text.strip())
    if not m:
        raise ValueError("无法解析 nginx 时间: %r" % text)
    month = _parse_month(m.group("month"))
    offset = timedelta(hours=int(m.group("off_hour")),
                       minutes=int(m.group("off_minute")))
    if offset >= timedelta(hours=24):
        raise ValueError("时区偏移超出范围: %r" % text)
    if m.group("sign") == "-":
        offset = -offset
    local = datetime(
        int(m.group("year")), month, int(m.group("day")),
        int(m.group("hour")), int(m.group("minute")), int(m.group("second")),
        tzinfo=timezone(offset),
    )
    return local.astimezone(timezone.utc)


def _coerce_status(value):
    """取出合法的 HTTP 状态码，否则返回 None。"""
    if isinstance(value, bool):
        return None
    try:
        code = int(value)
    except (TypeError, ValueError):
        return None
    return code if 100 <= code <= 599 else None


# ---------------------------------------------------------------------------
# 三种格式的解析器
# ---------------------------------------------------------------------------

def parse_nginx(line):
    """解析一行 nginx combined 日志；失败返回 None。"""
    if not isinstance(line, str):
        return None
    text = line.rstrip("\r\n").strip()
    if not text:
        return None
    m = PATTERN.match(text)
    if not m:
        return None
    try:
        ts = _parse_nginx_time(m.group("time"))
    except (ValueError, OverflowError):
        return None

    request = m.group("request")
    method = path = protocol = None
    if request and request != "-":
        parts = request.split()
        if parts:
            method = parts[0]
        if len(parts) > 1:
            path = parts[1]
        if len(parts) > 2:
            protocol = parts[2]

    status_raw = m.group("status")
    status_code = _coerce_status(status_raw) if status_raw != "-" else None
    size = m.group("bytes")
    return {
        "format": "nginx",
        "ts": ts,
        "time": m.group("time"),
        "ip": m.group("ip"),
        "method": method,
        "path": path,
        "protocol": protocol,
        "status": status_raw,
        "status_code": status_code,
        "bytes": int(size) if size != "-" else None,
        "referer": m.group("referer"),
        "user_agent": m.group("user_agent"),
        "raw": text,
    }


def parse_app(line):
    """解析一行 JSON Lines 应用日志；时间统一转成 UTC。"""
    if not isinstance(line, str):
        return None
    text = line.rstrip("\r\n").strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None

    ts_raw = None
    for key in ("ts", "timestamp", "time", "@timestamp"):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            ts_raw = value.strip()
            break
    if ts_raw is None:
        return None
    try:
        # Python 3.11+ 的 fromisoformat 直接支持 Z；这里兼容旧写法。
        ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None or ts.utcoffset() is None:
        return None
    ts = ts.astimezone(timezone.utc)

    status_value = obj.get("status", obj.get("status_code"))
    path = obj.get("path", obj.get("uri"))
    ip = obj.get("ip", obj.get("remote_addr", obj.get("client_ip")))
    if path is not None and not isinstance(path, str):
        path = str(path)
    return {
        "format": "app",
        "ts": ts,
        "time": ts_raw,
        "ip": ip if isinstance(ip, str) else None,
        "method": obj.get("method") if isinstance(obj.get("method"), str) else None,
        "path": path or None,
        "status": str(status_value) if status_value is not None else None,
        "status_code": _coerce_status(status_value),
        "level": obj.get("level"),
        "message": obj.get("msg", obj.get("message")),
        "raw": text,
    }


def parse_syslog(line, year=DEFAULT_SYSLOG_YEAR, tz=timezone.utc):
    """解析传统 syslog；年份用 ``year`` 补全，``tz`` 是该时间所在时区。"""
    if not isinstance(line, str):
        return None
    text = line.rstrip("\r\n").strip()
    if not text:
        return None
    m = _SYSLOG_RE.match(text)
    if not m:
        return None
    try:
        month = _parse_month(m.group("month"))
        ts = datetime(
            int(year), month, int(m.group("day")),
            int(m.group("hour")), int(m.group("minute")), int(m.group("second")),
            tzinfo=tz,
        ).astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None

    rest = m.group("rest").strip()
    tag = _SYSLOG_TAG_RE.match(rest)
    program = pid = None
    message = rest
    if tag:
        program = tag.group("program")
        pid = int(tag.group("pid")) if tag.group("pid") else None
        message = tag.group("message")

    host = m.group("host")
    try:
        ip = host if ipaddress.ip_address(host) else None
    except ValueError:
        ip = None
    # 保留原始 syslog 时间文本（去掉可选的 <PRI> 前缀）。
    time_text = "%s %s %s:%s:%s" % (
        m.group("month"), m.group("day").rjust(2),
        m.group("hour"), m.group("minute"), m.group("second"))
    return {
        "format": "syslog",
        "ts": ts,
        "time": time_text,
        "year": int(year),
        "ip": ip,
        "host": host,
        "program": program,
        "pid": pid,
        "message": message,
        "path": None,
        "status": None,
        "status_code": None,
        "raw": text,
    }


#: 格式名 -> 解析函数（syslog 额外参数由 parse_line 处理）。
_PARSERS = {
    "nginx": parse_nginx,
    "app": parse_app,
    "syslog": parse_syslog,
}


def parse_line(line, fmt="nginx", syslog_year=DEFAULT_SYSLOG_YEAR,
               syslog_tz=timezone.utc):
    """按指定格式解析一行；失败返回 None。"""
    if fmt not in _PARSERS:
        raise ValueError("未知日志格式: %r" % fmt)
    if fmt == "syslog":
        return parse_syslog(line, year=syslog_year, tz=syslog_tz)
    return _PARSERS[fmt](line)


def parse_lines(lines, fmt="nginx", syslog_year=DEFAULT_SYSLOG_YEAR,
                syslog_tz=timezone.utc, errors=None, stream=None):
    """解析多行日志。

    空行会被忽略；解析失败的非空行不会静默丢弃——传入 ``errors`` 列表时，
    会把 ``{"file", "line", "text"}`` 追加进去。
    """
    out = []
    for line_no, raw in enumerate(lines, 1):
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        text = raw.rstrip("\r\n")
        if not text.strip():
            continue
        entry = parse_line(text, fmt, syslog_year=syslog_year,
                           syslog_tz=syslog_tz)
        if entry is None:
            if errors is not None:
                errors.append({"file": stream, "format": fmt,
                               "line": line_no, "text": text})
            continue
        entry["format"] = fmt
        entry["line"] = line_no
        entry["raw"] = text
        entry["stream"] = stream
        out.append(entry)
    return out


def detect_format(path, lines=None):
    """根据文件名（必要时看内容）猜测日志格式；无法判断返回 None。"""
    name = os.path.basename(path).lower()
    if name.endswith((".jsonl", ".json")):
        return "app"
    if "syslog" in name:
        return "syslog"
    if "access" in name or "nginx" in name:
        return "nginx"

    if lines:
        sample = [line for line in lines if line.strip()][:20]
        if sample:
            best_fmt, best_score = None, 0
            # 先试特征最强的 JSON，再 nginx，最后 syslog。
            for fmt in ("app", "nginx", "syslog"):
                score = sum(1 for line in sample
                            if parse_line(line, fmt) is not None)
                if score > best_score:
                    best_fmt, best_score = fmt, score
            if best_score:
                return best_fmt
    return None


# ---------------------------------------------------------------------------
# 按事件时间聚合
# ---------------------------------------------------------------------------

def _new_bucket():
    return {
        "requests": 0,
        "errors": 0,
        "status_counts": Counter(),
        "ips": set(),
        "paths": Counter(),
    }


def normalize_ip(value):
    """把 IP 归一成网段：IPv4 取 /24，IPv6 取 /64；非 IP 返回 None。"""
    if not value:
        return None
    try:
        addr = ipaddress.ip_address(str(value).strip())
    except ValueError:
        return None
    if addr.version == 4:
        return str(ipaddress.ip_network("%s/24" % addr, strict=False))
    return str(ipaddress.ip_network("%s/64" % addr, strict=False))


def _add_to_bucket(bucket, entry):
    bucket["requests"] += 1
    status = entry.get("status_code")
    if status is not None:
        bucket["status_counts"][status] += 1
        if 500 <= status <= 599:
            bucket["errors"] += 1
    ip_key = normalize_ip(entry.get("ip"))
    if ip_key:
        bucket["ips"].add(ip_key)
    path = entry.get("path")
    if path:
        bucket["paths"][path] += 1


def _error_rate(errors, total):
    if not total:
        return 0.0
    return round(errors / total, 6)


def _top_paths(counter, top_n):
    if top_n <= 0:
        return []
    ordered = sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    return [{"path": path, "count": count} for path, count in ordered[:top_n]]


def _bucket_json(bucket, top_n):
    return {
        "requests": bucket["requests"],
        "status_counts": {
            str(code): bucket["status_counts"][code]
            for code in sorted(bucket["status_counts"])
        },
        "errors": bucket["errors"],
        "error_rate": _error_rate(bucket["errors"], bucket["requests"]),
        "unique_ips": len(bucket["ips"]),
        "top_paths": _top_paths(bucket["paths"], top_n),
    }


def _window_starts(ts_us, window_us, step_us):
    """返回包含该时刻的所有窗口起点（epoch 微秒）。窗口区间为 [start, start+window)。"""
    last = ts_us // step_us
    first = (ts_us - window_us) // step_us + 1
    return [index * step_us for index in range(first, last + 1)]


def _coerce_duration(value, label):
    """把 timedelta / ``"1m"`` 形式的字符串统一成 timedelta。"""
    if isinstance(value, timedelta):
        return value
    try:
        return parse_duration(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("%s无效: %s" % (label, exc))


def aggregate(entries, window, step=None, allowed_lateness=timedelta(seconds=30),
              top_n=5, too_late_samples=DEFAULT_SAMPLES):
    """按事件时间把事件放进滚动/滑动窗口并计算指标。

    ``window`` 是窗口长度，``step`` 是步长；省略 ``step`` 表示滚动窗口
    （等价于步长等于窗口长度）。窗口起点按 UTC epoch 对齐。

    迟到判定按每个 ``stream``（通常是文件路径）分别维护水位线：
    当前流观察到的最大事件时间为 ``max_ts``，若
    ``max_ts - event_ts > allowed_lateness``，该事件计入 too_late，
    不进入任何窗口。事件时间恰好落后允许迟到量的记录仍会被接受。

    返回 ``{"windows": [...], "too_late": {...}}``。
    """
    window_us = _to_us(_coerce_duration(window, "窗口长度"))
    if step is None:
        step_us = window_us
    else:
        step_us = _to_us(_coerce_duration(step, "窗口步长"))
    lateness_us = _to_us(_coerce_duration(allowed_lateness, "允许迟到"))
    if window_us <= 0:
        raise ValueError("窗口长度必须大于 0")
    if step_us <= 0:
        raise ValueError("窗口步长必须大于 0")
    if step_us > window_us:
        raise ValueError("窗口步长不能大于窗口长度")
    if lateness_us < 0:
        raise ValueError("允许迟到不能为负数")
    if top_n < 0:
        raise ValueError("Top N 不能为负数")
    if too_late_samples < 0:
        raise ValueError("too_late 样例数不能为负数")

    # 按到达顺序分组成输入流；不同文件/来源之间互不干扰水位线。
    streams = {}
    formats_seen = set()
    for entry in entries:
        fmt = entry.get("format", "unknown")
        formats_seen.add(fmt)
        streams.setdefault(entry.get("stream", ""), []).append(entry)

    windows = {}
    too_late = {
        "count": 0,
        "by_format": {fmt: 0 for fmt in formats_seen},
        "samples": [],
    }

    for stream_entries in streams.values():
        max_ts_us = None
        for entry in stream_entries:
            ts = entry.get("ts")
            if not isinstance(ts, datetime):
                continue
            if ts.tzinfo is None or ts.utcoffset() is None:
                ts = ts.replace(tzinfo=timezone.utc)
            ts_us = _to_us(ts.astimezone(timezone.utc) - _EPOCH)

            if max_ts_us is not None and ts_us < max_ts_us - lateness_us:
                fmt = entry.get("format", "unknown")
                too_late["count"] += 1
                too_late["by_format"][fmt] = too_late["by_format"].get(fmt, 0) + 1
                if len(too_late["samples"]) < too_late_samples:
                    too_late["samples"].append({
                        "format": fmt,
                        "file": entry.get("stream"),
                        "line": entry.get("line"),
                        "ts": _iso_utc(ts),
                        "lateness_seconds": round(
                            (max_ts_us - ts_us) / _US_PER_SECOND, 6),
                        "text": entry.get("raw", ""),
                    })
                continue

            max_ts_us = ts_us if max_ts_us is None else max(max_ts_us, ts_us)

            for start_us in _window_starts(ts_us, window_us, step_us):
                state = windows.get(start_us)
                if state is None:
                    state = {"total": _new_bucket(), "formats": {}}
                    windows[start_us] = state
                _add_to_bucket(state["total"], entry)
                fmt = entry.get("format", "unknown")
                bucket = state["formats"].get(fmt)
                if bucket is None:
                    bucket = _new_bucket()
                    state["formats"][fmt] = bucket
                _add_to_bucket(bucket, entry)

    window_list = []
    for start_us in sorted(windows):
        state = windows[start_us]
        formats = state["formats"]
        ordered_formats = [fmt for fmt in FORMATS if fmt in formats]
        ordered_formats += sorted(fmt for fmt in formats if fmt not in FORMATS)
        item = {
            "window_start": _iso_utc(_EPOCH + timedelta(microseconds=start_us)),
            "window_end": _iso_utc(
                _EPOCH + timedelta(microseconds=start_us + window_us)),
        }
        item.update(_bucket_json(state["total"], top_n))
        item["formats"] = {
            fmt: _bucket_json(formats[fmt], top_n) for fmt in ordered_formats
        }
        window_list.append(item)

    return {
        "window_seconds": _seconds(window_us),
        "step_seconds": _seconds(step_us),
        "mode": "tumbling" if step_us == window_us else "sliding",
        "windows": window_list,
        "too_late": too_late,
    }


def count_by_minute(entries):
    """按分钟聚合请求数（兼容旧接口）。

    有 ``ts`` 时按事件时间归到 UTC 分钟；否则沿用旧行为，
    截取 ``time`` 字符串的前 17 个字符。
    """
    buckets = {}
    for entry in entries:
        ts = entry.get("ts")
        if isinstance(ts, datetime):
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            minute = ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
        else:
            minute = entry["time"][:17]
        buckets[minute] = buckets.get(minute, 0) + 1
    return buckets
