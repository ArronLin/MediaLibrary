# -*- coding: utf-8 -*-
"""试跑大系列归组：只读不动库，打印匹配结果。"""
import sys, io, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, db, franchise

db.init_db()   # 幂等迁移：补 series_group 列
lib = db.rows("SELECT id, name FROM library LIMIT 1")
lib = dict(lib[0]) if lib else None
print("library:", lib)

supers = franchise.super_series_config()
print("config super_series:", [s["name"] for s in supers], "\n")

items = [dict(r) for r in db.rows(
    "SELECT * FROM item WHERE library_id=? AND hidden=0", (lib["id"],))]
print("总条目:", len(items))

for sp in supers:
    print("=" * 74)
    print("大系列: %s" % sp["name"])
    print("=" * 74)
    total = 0
    for child, cname in zip(sp["children"],
                            [str(c.get("name") or "").strip() or "未命名子系列"
                             for c in sp["children"]]):
        hits = [it for it in items if franchise._match_child(it, child)]
        if not hits:
            continue
        total += len(hits)
        print("  【%s】%d 部" % (cname, len(hits)))
        for it in sorted(hits, key=lambda x: x.get("year") or 0):
            print("      %-6s %-24s %-22s coll=%s"
                  % (it["id"], it["title"][:24],
                     (it.get("original_title") or "")[:22], it.get("collection_id")))
    print("  ---- 合计 %d 部\n" % total)

# 误命中检查：没进漫威大系列、但标题/keyword 撞车嫌疑的
print("=" * 74)
print("未匹配但含 Marvel 关键词（防误杀检查）")
print("=" * 74)
for it in items:
    if any(franchise._match_child(it, c) for sp in supers for c in sp["children"]):
        continue
    hay = franchise._blob(it)
    for kw in ("spider", "hulk", "etern", "ant-man", "marvel", "iron", "thor",
               "captain", "avengers", "panther", "strange", "guardian",
               "蜘蛛", "浩克", "复仇", "钢铁", "雷神", "队长", "永恒", "蚁人"):
        if kw in hay:
            print("  ! id=%-5s %-24s %-22s coll=%s  (kw=%s)"
                  % (it["id"], it["title"][:24],
                     (it.get("original_title") or "")[:22],
                     it.get("collection_id"), kw))
            break

# 实际写入一次（rebuild），然后复核
print()
print("=" * 74)
print("执行 franchise.rebuild()")
print("=" * 74)
res = franchise.rebuild(lib["id"], None)
print("结果:", res)

frs = db.rows("SELECT f.id, f.name, f.source, f.collection_id, COUNT(i.id) n"
              " FROM franchise f LEFT JOIN item i ON i.franchise_id=f.id"
              " AND i.hidden=0 WHERE f.library_id=? GROUP BY f.id ORDER BY f.name",
              (lib["id"],))
print("\n系列列表:")
for r in frs:
    print("  %-5s %-30s %-8s coll=%-9s n=%s"
          % (r["id"], r["name"][:30], r["source"], r["collection_id"], r["n"]))

print("\n漫威大系列成员（含子系列名）:")
for r in db.rows(
        "SELECT f.name mega, i.series_group, i.id, i.title, i.year"
        " FROM item i JOIN franchise f ON f.id=i.franchise_id"
        " WHERE f.library_id=? AND f.source='super'"
        " ORDER BY i.series_group, i.year", (lib["id"],)):
    print("  %-22s %-8s %-5s %-24s %s"
          % (r["mega"], r["series_group"], r["id"], r["title"][:24], r["year"]))
