# log-metrics

多格式日志解析与按**事件时间**滚动的指标引擎。只依赖 Python 标准库，无第三方依赖。

## 能力概览

| 需求 | 实现 |
| --- | --- |
| nginx combined | `logm.core.parse_nginx`；request 被截断成 `"GET /a 200 12"` 时，状态码取引号外的真实字段 |
| JSON Lines | `logm.core.parse_app`；带时区的 ISO 8601 统一转 UTC |
| 传统 syslog | `logm.core.parse_syslog`；英文月份缩写 + 配置基准年份（默认 2026），时区由 `--syslog-tz` 指定 |
| 事件时间 | 所有时间转 UTC 后按 epoch 对齐，与文件行顺序无关 |
| 水位线 / 迟到 | 每个输入流独立维护水位线；落后超过 `--allowed-lateness`（默认 30s）的记 `too_late`，不进入窗口 |
| 滚动 / 滑动窗口 | `--window 1m`；`--window 1m --step 30s` |
| 指标 | 请求数、状态码分类、错误率（5xx/总数）、独立 IP（IPv4 /24、IPv6 /64）、Top N 路径 |
| 多格式合并 | 多个文件一起传；每个窗口按 `nginx` / `app` / `syslog` 分别给出指标并汇总 |
| 解析失败 | 按格式计数并保留前 N 行原文，绝不静默丢弃 |
| 退出码 | 0 全部解析成功；1 存在 unparsed 行；2 参数错误 |

窗口区间是左闭右开 `[start, start+window)`，以 UTC epoch 对齐；滑动窗口下同一事件会进入所有包含它的窗口。

## 运行

```powershell
# 注意：gen_samples.py 会重写三个样例日志，仅在需要重建样例时运行
# python gen_samples.py
python -m logm.cli access.log --window 1m
python -m logm.cli app.jsonl --window 1m
python -m logm.cli syslog.log --window 1m
python -m logm.cli access.log app.jsonl syslog.log --window 5m
python -m logm.cli app.jsonl --window 1m --step 30s --allowed-lateness 5s
python -m pytest tests -q
```

参数：

```
FILE...                       一个或多个日志文件
-w/--window DURATION          窗口长度，1m、5m、90s、1m30s
-s/--step DURATION            滑动步长，省略即滚动窗口
--allowed-lateness DURATION   允许迟到，默认 30s
--top N                       每窗口 Top N 路径，默认 5
--format {auto,nginx,app,syslog}  默认按文件名/内容识别
--syslog-year YEAR            syslog 缺失年份，默认 2026
--syslog-tz OFFSET            syslog 时区，默认 UTC（+08:00 等）
--unparsed-samples N          未解析/迟到样例条数，默认 5
-o/--output FILE              写入文件而非 stdout
```

## 三个日志的实际结果（`--window 1m`，滚动窗口）

### access.log（nginx，退出码 1）

其中 `/b` 那行请求文本是 `"POST /b 200 8"`，真实的 201 来自引号外字段；第 5 行是坏行，计入 unparsed。

```json
{
  "file": "access.log",
  "format": "nginx",
  "mode": "tumbling",
  "windows": [
    {"window_start": "2026-02-02T02:00:00Z", "window_end": "2026-02-02T02:01:00Z",
     "requests": 2, "status_counts": {"200": 1, "201": 1}, "errors": 0,
     "error_rate": 0.0, "unique_ips": 1,
     "top_paths": [{"path": "/a", "count": 1}, {"path": "/b", "count": 1}]},
    {"window_start": "2026-02-02T02:01:00Z", "window_end": "2026-02-02T02:02:00Z",
     "requests": 1, "status_counts": {"500": 1}, "errors": 1,
     "error_rate": 1.0, "unique_ips": 1,
     "top_paths": [{"path": "/c", "count": 1}]},
    {"window_start": "2026-02-02T02:02:00Z", "window_end": "2026-02-02T02:03:00Z",
     "requests": 2, "status_counts": {"200": 2}, "errors": 0,
     "error_rate": 0.0, "unique_ips": 1,
     "top_paths": [{"path": "/d", "count": 1}, {"path": "/e", "count": 1}]}
  ],
  "unparsed": {"total": 1, "by_format": {"nginx": 1, "app": 0, "syslog": 0},
               "samples_by_format": {"nginx": [
                 {"file": "access.log", "line": 5,
                  "text": "this line is broken and should be counted as unparsed"}]}},
  "too_late": {"count": 0}
}
```

（实际输出中每个窗口还有按来源格式拆分的 `formats.nginx`，内容与上面汇总一致。）

### app.jsonl（JSON Lines，退出码 0）

原始时间均为 `+08:00`，转 UTC 后都落在 `02:00` 这一分钟；4 条全部接受，`too_late` 为 0。

```json
{
  "file": "app.jsonl",
  "format": "app",
  "mode": "tumbling",
  "windows": [
    {"window_start": "2026-02-02T02:00:00Z", "window_end": "2026-02-02T02:01:00Z",
     "requests": 4, "status_counts": {}, "errors": 0, "error_rate": 0.0,
     "unique_ips": 0, "top_paths": []}
  ],
  "unparsed": {"total": 0, "by_format": {"nginx": 0, "app": 0, "syslog": 0}},
  "too_late": {"count": 0}
}
```

### syslog.log（传统 syslog，退出码 0）

无年份，按默认基准年 2026 补全；无时区，默认按 UTC 解释（可用 `--syslog-tz +08:00` 调整）。

```json
{
  "file": "syslog.log",
  "format": "syslog",
  "mode": "tumbling",
  "windows": [
    {"window_start": "2026-02-02T10:00:00Z", "window_end": "2026-02-02T10:01:00Z",
     "requests": 1, "status_counts": {}, "errors": 0, "error_rate": 0.0,
     "unique_ips": 0, "top_paths": []},
    {"window_start": "2026-02-02T10:01:00Z", "window_end": "2026-02-02T10:02:00Z",
     "requests": 1, "status_counts": {}, "errors": 0, "error_rate": 0.0,
     "unique_ips": 0, "top_paths": []}
  ],
  "unparsed": {"total": 0, "by_format": {"nginx": 0, "app": 0, "syslog": 0}},
  "too_late": {"count": 0}
}
```

完整原始 JSON 可用 `python -m logm.cli <file> --window 1m` 复现。

## 迟到的那条记录落在哪个窗口

`app.jsonl` 按文件顺序是：

| 行 | 原始 ts (+08:00) | UTC | 说明 |
| --- | --- | --- | --- |
| 1 | `10:00:02.100` | `02:00:02.100Z` | — |
| 2 | `10:00:31.000` | `02:00:31.000Z` | 先把水位线推到 31.000s |
| 3 | `10:00:05.400` | `02:00:05.400Z` | 落后水位线 **25.6s**（迟到记录） |
| 4 | `10:00:01.900` | `02:00:01.900Z` | 落后 **29.1s**（乱序记录） |

按默认允许迟到 30s，第 3、4 行都**未被判为 too_late**，被正确归入各自的事件时间窗口 —— 也就是 **`2026-02-02T02:00:00Z` ~ `02:01:00Z` 这个窗口**，该窗口 `requests = 4`。也就是说迟到 25.6 秒的那条记录（`msg = "middle"`）落在**第一个一分钟窗口**，而不是它到达时水位线所在的后续窗口，因为它的 UTC 事件时间 `02:00:05.400Z` 属于 `[02:00:00, 02:01:00)`。

作为对照，把允许迟到收紧到 5s：

```powershell
python -m logm.cli app.jsonl --window 1m --step 30s --allowed-lateness 5s
```

此时第 3、4 行超过 5s 被判 `too_late`（`too_late.count = 2`，样例里给出 `lateness_seconds` 25.6 与 29.1），只剩下第 1、2 行进入窗口；滑动窗口输出三个窗口 `01:59:30`（1 条）、`02:00:00`（2 条）、`02:00:30`（1 条）。

## 三格式合并（`--window 5m`，退出码 1）

```powershell
python -m logm.cli access.log app.jsonl syslog.log --window 5m
```

- `2026-02-02T02:00:00Z`–`02:05:00Z`：9 条（nginx 5、app 4），错误 1，错误率 `0.111111`；其中 nginx 自身错误率 `0.2`。
- `2026-02-02T10:00:00Z`–`10:05:00Z`：2 条（syslog 2，按 UTC 解释）。
- `unparsed.total = 1`（access.log 第 5 行），`too_late.count = 0`。

## 测试

```powershell
python -m pytest tests -q
```

- `tests/test_parsers.py` — 三种格式解析、被截断 request 的状态码、ISO→UTC、syslog 年份补全、unparsed 上报。
- `tests/test_windows.py` — 事件时间归窗、乱序、迟到边界（恰好 30s 接受 / 31s 拒绝）、滚动与滑动窗口边界、错误率、Top N、/24 归一。
- `tests/test_cli.py` — 退出码 0/1/2、三格式合并标注、迟到记录落窗、`-o` 输出。

## 设计说明

- **解析失败不丢弃**：`parse_lines` 收集 `{file, format, line, text}`；CLI 按格式汇总到 `unparsed.by_format`，并保留 `samples_by_format` 与全局 `samples`。
- **水位线按流隔离**：每个输入文件单独维护 `max(ts)`，避免一个时区偏移更大的文件把另一个文件的事件误判为迟到。
- **迟到判定**：`max_ts - event_ts > allowed_lateness` 才算 too_late；恰好等于允许迟到量的记录仍进入窗口。
- **空行**既不算 parsed 也不算 unparsed。
