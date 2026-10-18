# -*- coding: utf-8 -*-
"""全量回归：把本轮所有改动一起验一遍，不起常驻服务。

覆盖：
  1. parser：压制组收紧 + 别名拆分（含全库 562 文件行为一致性）
  2. tmdb  ：_score rank 权重 + 跨语言重搜 + 同分热度决胜
  3. sync  ：按文件名重置 + 重新刮削
  4. franchise：大系列归组 + 子系列分组 + API 返回形状
  5. config：super_series / llm 默认值 / API key 打码
  6. server：/api/franchises/{id} 的 groups 形状（直接调函数）
"""
import sys, io, os, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, db, franchise, parser, sync, tmdb, llm

FAILS = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("OK  " if cond else "FAIL", name,
                           ("  -> " + str(detail)) if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


db.init_db()
cfg = config.load()
client = tmdb.Tmdb(cfg.get("tmdb") or {})

print("=" * 78)
print("1. 解析器")
print("=" * 78)
VARIANT_CASES = [
    # (文件名, 期望出现的查询词, 不应出现的查询词)
    ("Ying.Xiong.Cause.-.The.Birth.Of.Hero.2002.DIRECTORS.CUT.1080p.BluRay.x264.AAC5.1-WORLD.mp4",
     ["The Birth Of Hero", "Ying Xiong Cause"], ["WORLD", "DIRECTORS"]),
    ("[蜘蛛侠].Spider-Man.Homecoming.2017.1080p.mkv", [], ["Man Homecoming", "Spider 2017"]),
    ("X-Men.Days.of.Future.Past.2014.mkv", ["X-Men Days of Future Past"], []),
    ("The.Wizard.of.Oz.1939.1080p.BluRay.x264-YTS.BZ.mp4", ["The Wizard of Oz"], ["YTS"]),
    ("[低俗小说].Pulp.Fiction.1994.BDRip.X264-TLF.mkv", ["Pulp Fiction", "低俗小说"], ["TLF"]),
]
for fn, want, avoid in VARIANT_CASES:
    qs = parser.parse_name(fn)["queries"]
    for w in want:
        check("%s -> 含 %r" % (fn[:34], w), any(w == q or q.startswith(w + " ") for q in qs), qs)
    for a in avoid:
        check("%s -> 不含 %r" % (fn[:34], a), not any(a == q or q.endswith(" " + a) for q in qs), qs)

# 全库一致性：新正则与旧正则在所有真实文件名上行为相同
import re as _re
OLD = _re.compile(r"(?<![A-Za-z])[-@](?=[A-Za-z0-9@._]*[A-Za-z])[A-Za-z0-9@._]{2,26}\s*$")
diff = 0
for r in db.rows("SELECT rel_path FROM media_file WHERE library_id=1 AND missing=0"):
    base = r["rel_path"].split("\\")[-1]
    for ext in parser.VIDEO_EXT:
        if base.lower().endswith("." + ext):
            base = base[: -(len(ext) + 1)]
            break
    b = parser.RE_RELEASE_GROUP2.sub(" ", base)
    a = OLD.sub(" ", b)
    b2 = parser.RE_RELEASE_GROUP.sub(" ", b)
    if a != b2:
        diff += 1
check("压制组正则在全库 562 文件上行为一致", diff == 0, "差异 %d 个" % diff)

print()
print("=" * 78)
print("2. TMDB 打分与检索")
print("=" * 78)
SCORE_CASES = [
    ("[美国队长].Captain.America.BD.1080P.x265.10bit.DD5.1.英国双语.内封特效中英双字-FFansBD.mkv", "movie", 1771, 2011),
    ("96 Minutes (2025) [1080p] [WEBRip] [x265] [10bit].mp4", "movie", 1271895, 2025),
    ("365 Days (2020) [1080p].mkv", "movie", 664413, 2020),
    ("Alien.1979.2160p.UHD.BluRay.mkv", "movie", 348, 1979),
    ("Stranger.Things.S01E01.1080p.mkv", "tv", 66732, 2016),
    ("Apollo13.1995.阿波罗十三号.国英音轨.双语字幕.HR-HDTV.1024X576.x264-人人影视制作.mkv", "movie", 568, 1995),
    ("Ying.Xiong.Cause.-.The.Birth.Of.Hero.2002.DIRECTORS.CUT.1080p.BluRay.x264.AAC5.1-WORLD.mp4", "movie", 333799, 2002),
]
for fn, kind, want_id, want_year in SCORE_CASES:
    p = parser.parse_name(fn)
    g = client.search(p, kind)
    ok = bool(g) and g["tmdb_id"] == want_id and g.get("year") == want_year
    check("%s -> %s(%s)" % (fn[:44], want_id, want_year), ok,
          g and "%s(%s) id=%s" % (g.get("title"), g.get("year"), g["tmdb_id"]))

print()
print("=" * 78)
print("3. 库里刮错条目已修正")
print("=" * 78)
for iid, want_id in ((171, 1771), (374, 568), (26, 333799), (16, 680493)):
    it = db.get_item(iid)
    check("item %d -> tmdb=%s" % (iid, want_id), it.get("tmdb_id") == want_id,
          it.get("tmdb_id"))
    check("item %d title=%s" % (iid, db.get_item(iid).get("title")), True)

print()
print("=" * 78)
print("4. 大系列归组")
print("=" * 78)
supers = franchise.super_series_config()
check("config 里有 1 个大系列", len(supers) == 1, [s["name"] for s in supers])
frs = {f["name"]: f for f in db.rows(
    "SELECT f.*, (SELECT COUNT(*) FROM item i WHERE i.franchise_id=f.id"
    " AND i.hidden=0) n FROM franchise f WHERE f.library_id=1")}
mega = [f for f in db.rows("SELECT * FROM franchise WHERE library_id=1"
                           " AND source='super'")]
check("大系列已建", len(mega) == 1, [dict(f)["name"] for f in mega])
if mega:
    fid = mega[0]["id"]
    members = [dict(r) for r in db.rows(
        "SELECT i.*, f.source FROM item i JOIN franchise f ON f.id=i.franchise_id"
        " WHERE i.franchise_id=?", (fid,))]
    check("大系列有 16 个成员", len(members) == 16, len(members))
    groups = {}
    for m in members:
        groups.setdefault(m.get("series_group") or "其他", []).append(m["title"])
    check("分成 9 个子系列", len(groups) == 9, list(groups))
    check("钢铁侠系列有 3 部", len(groups.get("钢铁侠系列", [])) == 3, groups.get("钢铁侠系列"))
    check("美国队长系列含 2011 第一部",
          any("2011" in str(db.get_item(m["id"])["year"]) or
              db.get_item(m["id"])["year"] == 2011 for m in members
              if m.get("series_group") == "美国队长系列"))
    check("成员 series_group 全部非空", all(m.get("series_group") for m in members))
# 子系列卡应已被合并掉
check("原「钢铁侠（系列）」卡已不存在", "钢铁侠（系列）" not in frs, sorted(frs))
check("原「复仇者联盟（系列）」卡已不存在", "复仇者联盟（系列）" not in frs)

print()
print("=" * 78)
print("5. 防误杀：这些不该进漫威大系列")
print("=" * 78)
for iid in (8, 12, 18, 139, 342, 371, 396, 337):
    it = db.get_item(iid)
    is_mega = any(m["id"] == iid for m in
                  db.rows("SELECT i.id FROM item i JOIN franchise f"
                          " ON f.id=i.franchise_id WHERE f.source='super'"))
    check("id=%d %s 不在大系列" % (iid, it["title"][:14]), not is_mega)

print()
print("=" * 78)
print("6. 配置与 API")
print("=" * 78)
check("llm 默认关闭", cfg.get("llm", {}).get("enabled") is False)
check("llm 未配置时不发请求", llm.enabled() is False)
check("super_series 有内容", len(cfg.get("super_series") or []) == 1)
pv = config.public_view()
raw_key = str((cfg.get("tmdb") or {}).get("api_key") or "")
check("tmdb key 已打码", "*" in pv["tmdb"]["api_key"]
      and (not raw_key or raw_key not in pv["tmdb"]["api_key"]))
check("llm key 打码字段存在", "api_key_set" in pv["llm"])

# 系列详情接口的 groups 形状
import app.server as srv
fid = mega[0]["id"] if mega else 0
rows = [dict(r) for r in db.franchise_members(fid)]
seen = {}
for m in rows:
    seen.setdefault(m.get("series_group") or "其他", []).append(srv._brief(m))
order = [c["name"] for c in supers[0]["children"]]
names = [n for n in order if n in seen] + [n for n in seen if n not in order]
check("groups 按 config 顺序排列", names[0] == "钢铁侠系列", names)
check("每个 group 都有 members", all(seen[n] for n in names))

print()
print("=" * 78)
print("7. 未识别条目收敛")
print("=" * 78)
unid = [dict(r) for r in db.rows(
    "SELECT id, title FROM item WHERE library_id=1 AND status='unidentified'")]
check("未识别降为 2 条", len(unid) == 2, [(r["id"], r["title"][:30]) for r in unid])

print()
print("=" * 78)
if FAILS:
    print("失败 %d 项：" % len(FAILS))
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("全部通过 ✓  (%d 项检查)" % 0)
print("=" * 78)
