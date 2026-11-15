# -*- coding: utf-8 -*-
"""生成多格式日志样本。

access.log：nginx combined，时间跨 3 个分钟，含一条格式错误行
app.jsonl：应用日志，时间戳是 ISO，含乱序与迟到 30 秒的记录
syslog.log：传统 syslog，月份是英文缩写

用法：python gen_samples.py
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))

NGINX = [
    '10.0.0.1 - - [02/Feb/2026:10:00:01 +0800] "GET /a 200 12" 200 512',
    '10.0.0.2 - - [02/Feb/2026:10:00:05 +0800] "POST /b 200 8" 201 128',
    '10.0.0.3 - - [02/Feb/2026:10:01:02 +0800] "GET /c 500 4" 500 64',
    '10.0.0.4 - - [02/Feb/2026:10:02:09 +0800] "GET /d 200 22" 200 1024',
    'this line is broken and should be counted as unparsed',
    '10.0.0.5 - - [02/Feb/2026:10:02:10 +0800] "GET /e 200 3" 200 256',
]

APP = [
    {"ts": "2026-02-02T10:00:02.100+08:00", "level": "INFO", "msg": "start", "uid": 1},
    {"ts": "2026-02-02T10:00:31.000+08:00", "level": "ERROR", "msg": "late-from-future", "uid": 2},
    {"ts": "2026-02-02T10:00:05.400+08:00", "level": "INFO", "msg": "middle", "uid": 3},
    {"ts": "2026-02-02T10:00:01.900+08:00", "level": "WARN", "msg": "out of order", "uid": 4},
]

SYSLOG = [
    "Feb  2 10:00:03 host1 sshd[123]: Accepted password for root",
    "Feb  2 10:01:11 host2 kernel: link down",
]


def main():
    with open(os.path.join(HERE, "access.log"), "w", encoding="utf-8") as f:
        f.write("\n".join(NGINX) + "\n")
    with open(os.path.join(HERE, "app.jsonl"), "w", encoding="utf-8") as f:
        for e in APP:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    with open(os.path.join(HERE, "syslog.log"), "w", encoding="utf-8") as f:
        f.write("\n".join(SYSLOG) + "\n")
    print("三种日志已生成")


if __name__ == "__main__":
    main()
