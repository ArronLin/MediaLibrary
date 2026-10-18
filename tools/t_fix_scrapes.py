# -*- coding: utf-8 -*-
"""把刮错的条目按文件名重置后重新刮削。

用法： python tools/t_fix_scrapes.py [电影名关键词 ...]
"""
import sys, io, os, time, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, db, franchise, sync, tmdb

db.init_db()
franchise.rebuild(1, None)   # 应用大系列归组

cfg = config.load()
client = tmdb.Tmdb(cfg.get("tmdb") or {})

# 默认处理用户报告过的两条
TARGETS = sys.argv[1:] or ["美国队长", "阿波罗", "Apollo"]


def find_items(kws):
    rows = [dict(r) for r in db.rows(
        "SELECT * FROM item WHERE library_id=1 AND hidden=0")]
    out = []
    for r in rows:
        blob = " ".join(str(r.get(k) or "") for k in
                        ("title", "original_title", "rel_path"))
        fpaths = [f.get("rel_path") or "" for f in db.files_for_item(r["id"])]
        blob = blob + " " + " ".join(fpaths)
        low = blob.lower()
        if any(k.lower() in low for k in kws):
            out.append((r, fpaths))
    return out


for r, fpaths in find_items(TARGETS):
    print("=" * 78)
    print("处理 id=%s  title=%s  year=%s  tmdb=%s"
          % (r["id"], r["title"], r.get("year"), r.get("tmdb_id")))
    for fp in fpaths:
        print("    文件: %s" % fp)
    before = (r["title"], r.get("year"), r.get("tmdb_id"), r.get("poster_path"))

    new = sync.reset_item_from_files(r["id"])
    print("  重置后 -> title=%s orig=%s year=%s year_hint=%s status=%s"
          % (new.get("title"), new.get("original_title"), new.get("year"),
             new.get("year_hint"), new.get("status")))
    ok = sync.rescrape_item(r["id"])
    after = db.get_item(r["id"])
    print("  重刮 %s -> title=%s year=%s tmdb=%s coll=%s/%s"
          % ("成功" if ok else "失败", after.get("title"), after.get("year"),
             after.get("tmdb_id"), after.get("collection_id"),
             after.get("collection_name")))
    print("  前后对比: %s  ==>  %s" % (before, (after.get("title"),
          after.get("year"), after.get("tmdb_id"), after.get("poster_path"))))
    time.sleep(1.0)

print()
print("=" * 78)
print("复核：漫威大系列")
print("=" * 78)
franchise.rebuild(1, None)
for row in db.rows(
        "SELECT i.series_group g, i.id, i.title, i.year, i.tmdb_id"
        " FROM item i JOIN franchise f ON f.id=i.franchise_id"
        " WHERE f.library_id=1 AND f.source='super'"
        " ORDER BY i.series_group, i.year"):
    print("  %-14s %-5s %-26s %s tmdb=%s"
          % (row["g"], row["id"], row["title"][:26], row["year"], row["tmdb_id"]))

print()
print("复核：全库可疑条目（库里年份 vs 文件名年份）")
for row in db.suspicious_items(1, gap=1):
    print("  id=%-5s %-26s lib=%s  hint=%s  tmdb=%s"
          % (row["id"], row["title"][:26], row["year"], row["year_hint"],
             row["tmdb_id"]))
