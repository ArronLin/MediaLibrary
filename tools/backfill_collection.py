"""一次性回填：给已有电影条目补 collection_id / collection_name。

老版本入库时没有保存 TMDB 的 belongs_to_collection，
导致「哈利波特」「007」这类电影系列归不出组。
本脚本只补这两个字段，不改动其它元数据，不会触发重新刮削。

用法（在项目根目录）：
    python -m tools.backfill_collection            # 全库
    python -m tools.backfill_collection --limit 20 # 先试 20 条
"""
from __future__ import annotations

import argparse
import sys
import time

from app import config, db, paths, tmdb


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少条（0=全部）")
    ap.add_argument("--force", action="store_true", help="已填过的也重新查")
    args = ap.parse_args()

    cfg = config.load()
    tcfg = dict(cfg.get("tmdb") or {})
    if not tcfg.get("api_key"):
        print("config.json 里没有 tmdb.api_key，退出")
        return 2
    client = tmdb.Tmdb(tcfg)

    where = "kind='movie' AND tmdb_id IS NOT NULL AND hidden=0"
    if not args.force:
        where += " AND (collection_id IS NULL OR collection_id=0)"
    sql = "SELECT id, title, year, tmdb_id FROM item WHERE " + where + " ORDER BY id"
    rows = [dict(r) for r in db.rows(sql)]
    if args.limit:
        rows = rows[: args.limit]

    total = len(rows)
    print("待处理 %d 条" % total)
    if not total:
        return 0

    hit = 0
    miss = 0
    fail = 0
    t0 = time.time()
    for i, r in enumerate(rows, 1):
        try:
            col = client.collection_of(int(r["tmdb_id"]), "movie")
        except tmdb.ApiKeyError as exc:
            print("\nAPI Key 无效，中止：%s" % exc)
            return 3
        except Exception as exc:
            fail += 1
            print("[%d/%d] %s 查询失败：%s" % (i, total, r["title"], exc))
            continue

        if col:
            db.execute(
                "UPDATE item SET collection_id=?, collection_name=? WHERE id=?",
                (col["collection_id"], col["collection_name"], r["id"]))
            hit += 1
            print("[%d/%d] %s -> %s (%d)" % (
                i, total, r["title"], col["collection_name"], col["collection_id"]))
        else:
            miss += 1
            print("[%d/%d] %s 无系列" % (i, total, r["title"]))

    print("\n完成：命中 %d / 无系列 %d / 失败 %d，用时 %.1fs"
          % (hit, miss, fail, time.time() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
