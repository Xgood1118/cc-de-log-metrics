# -*- coding: utf-8 -*-
"""命令行入口：python -m logm.cli access.log"""
import json
import sys

from .core import count_by_minute, parse_lines


def main(argv=None):
    argv = argv or sys.argv[1:]
    if not argv:
        sys.stderr.write("需要日志文件\n")
        return 2
    with open(argv[0], "r", encoding="utf-8") as f:
        lines = f.readlines()
    entries = parse_lines(lines)
    buckets = count_by_minute(entries)
    print(json.dumps({"parsed": len(entries), "buckets": buckets}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
