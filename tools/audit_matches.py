"""审核刮削结果：找出「库里年份」和「文件名年份」对不上的条目。

这类条目大概率刮错了（例如 `96 Minutes (2025)` 被认成 2011 年的同名片）。
列出后可以：
    python -m tools.audit_matches              # 只看
    python -m tools.audit_matches --fix        # 重置为待刮削，下次增量更新时重刮
    python -m tools.audit_matches --fix --run  # 重置后立即跑一次增量更新
"""
from __future__ import annotations

import argparse
import sys

from app import db, sync


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix", action="store_true", help="重置为待刮削")
    ap.add_argument("--run", action="store_true", help="重置后立即同步")
    ap.add_argument("--gap", type=int, default=1, help="年份差超过多少算可疑（默认 1）")
    args = ap.parse_args()

    lib = db.active_library_id()
    if not lib:
        print("没有激活的媒体库")
        return 1

    rows = db.suspicious_items(lib, args.gap)
    print("可疑条目 %d 个（年份差 > %d 年）\n" % (len(rows), args.gap))
    for r in rows:
        print("  %-34s 库里=%s 文件名=%s  置信度=%s  %s" % (
            (r["title"] or "")[:34], r["year"], r["year_hint"],
            r["confidence"], r["source"]))

    if not args.fix:
        return 0
    if not rows:
        print("\n没有需要修复的条目")
        return 0

    ids = [r["id"] for r in rows]
    n = db.requeue_items(ids)
    print("\n已重置 %d 个条目为待刮削" % n)
    if args.run:
        sync.start(lib, full=False)
        print("已启动增量更新，可在网页上看进度")
    else:
        print("点一次「增量更新」即可重新刮削")
    return 0


if __name__ == "__main__":
    sys.exit(main())
