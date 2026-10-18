"""一次性回填：从 group_key 里的路径重新解析出年份，写入 item.year_hint。

背景：item.year 会被刮削结果覆盖。如果某次刮错了（例如
`96 Minutes (2025)` 被认成 2011 年的同名片），下次重刮时用错年份
继续搜，只会越错越离谱。year_hint 只写一次、永不覆盖，作为可靠线索。

用法：
    python -m tools.backfill_year            # 补所有 year_hint 为空的
    python -m tools.backfill_year --force    # 全部重算
"""
from __future__ import annotations

import argparse
import sys

from app import db, parser


def _path_of(group_key: str) -> str:
    """从 group_key 取出可用于解析年份的文本。

    电影优先看文件名（`The.Matrix.Reloaded.2003.mkv`），因为所在目录
    常常是合集目录（`The Matrix Collection (1999)`），年份会张冠李戴。
    剧集没有单集年份，退回看目录名。
    """
    if not group_key:
        return ""
    head, _, rest = group_key.partition(":")
    if head == "movie":
        base = rest.rsplit("\\", 1)[-1]
        base = base.rsplit(".", 1)[0] if "." in base else base
        return base or rest
    if head == "series":
        return rest.rsplit(":", 1)[0] if ":" in rest else rest
    return rest


def _years(group_key: str):
    """返回候选文本列表，按可信度从高到低。

    电影只看文件名：目录常常是合集目录或别的片子的目录
    （`末代皇帝 4K ...\\幕后花絮\\菜单.mkv` 里的 1987 和《菜单》无关），
    文件名里没有年份就当作「未知」，宁可不填也别填错。
    剧集没有单集年份，只能看目录名。
    """
    head, _, rest = group_key.partition(":")
    if head == "movie":
        base = rest.rsplit("\\", 1)[-1]
        return [base.rsplit(".", 1)[0] if "." in base else base]
    if head == "series":
        return [rest.rsplit(":", 1)[0] if ":" in rest else rest]
    return [rest]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    sql = "SELECT id, title, year, year_hint, group_key FROM item"
    if not args.force:
        sql += " WHERE year_hint IS NULL"
    rows = [dict(r) for r in db.rows(sql)]
    print("待处理 %d 条" % len(rows))

    hit = miss = changed = 0
    for r in rows:
        y = None
        for text in _years(r["group_key"]):
            y = parser.detect_year(text)
            if y:
                break
        if not y:
            miss += 1
            continue
        hit += 1
        if r.get("year_hint") != y:
            db.execute("UPDATE item SET year_hint=? WHERE id=?", (y, r["id"]))
            changed += 1
        if r.get("year") and abs((r["year"] or 0) - y) > 3:
            print("  年份可疑: %-40s 库里=%s 文件名=%s" % (r["title"][:40], r["year"], y))

    print("\n完成：解析到 %d / 未解析 %d / 实际更新 %d" % (hit, miss, changed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
