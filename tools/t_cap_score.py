# -*- coding: utf-8 -*-
"""验证 _score() rank 权重修复，并回归几个已知用例。"""
import sys, io, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.parser import parse_name
from app.tmdb import Tmdb
from app import config

c = Tmdb(config.load().get("tmdb") or {})

print("=" * 74)
print("A. 本地算分对比（不走网络结果，只影响排序）")
print("=" * 74)
fname = "[美国队长].Captain.America.BD.1080P.x265.10bit.DD5.1.英国双语.内封特效中英双字-FFansBD.mkv"
parsed = parse_name(fname)
print("parse_name(%s)" % fname)
print("  ->", {k: parsed.get(k) for k in ("title", "cn_title", "en_title", "year", "kind", "queries")})
res = c._request("/search/movie", {"query": parsed["cn_title"] or parsed["en_title"], "language": c.lang}) or {}
print("-" * 74)
print("  %-6s %-26s %-6s %-9s %-7s %-6s %s"
      % ("rank", "title", "year", "tmdb_id", "pop", "sim", "score"))
for rank, raw in enumerate((res.get("results") or [])[:6]):
    cand = c._candidate(raw, "movie")
    sim = c._sim_of(parsed, cand, parsed["cn_title"] or parsed["en_title"])
    print("  %-6d %-26s %-6s %-9d %-7.1f %-6.3f %.4f"
          % (rank, cand["title"][:26], cand.get("year"), cand["tmdb_id"],
             cand["popularity"] or 0, sim,
             c._score(parsed, cand, rank, parsed["cn_title"] or parsed["en_title"])))
print("-" * 74)
print("  期望：《美国队长》(2011, pop 17.6) 分数高于《美国队长3》(2016, pop 35.4)")
print()

print("=" * 74)
print("B. 端到端检索回归（走网络）")
print("=" * 74)
CASES = [
    ("[美国队长].Captain.America.BD.1080P.x265.10bit.DD5.1.英国双语.内封特效中英双字-FFansBD.mkv", "movie", 1771, 2011),
    ("96 Minutes (2025) [1080p] [WEBRip] [x265] [10bit].mp4", "movie", 1271895, 2025),
    ("365 Days (2020) [1080p].mkv", "movie", 664413, 2020),
    ("Alien.1979.2160p.UHD.BluRay.mkv", "movie", 348, 1979),
    ("Stranger.Things.S01E01.1080p.mkv", "tv", 66732, 2016),
    ("Apollo13.1995.阿波罗十三号.国英音轨.双语字幕.HR-HDTV.1024X576.x264-人人影视制作.mkv", "movie", 568, 1995),
]
ok = fail = 0
for fname, kind, want_id, want_year in CASES:
    p = parse_name(fname)
    got = c.search(p, kind)
    if not got:
        print("  [FAIL] %-52s -> None" % fname[:52])
        fail += 1
        continue
    good = (got["tmdb_id"] == want_id and got.get("year") == want_year)
    ok += good
    fail += (not good)
    print("  [%s] %-52s" % ("OK  " if good else "BAD ", fname[:52]))
    print("         -> %s (%s) tmdb=%d score=%.3f  %s"
          % (got["title"][:26], got.get("year"), got["tmdb_id"],
             got.get("score", 0), "" if good else "期望 tmdb=%d/%d" % (want_id, want_year)))

print("-" * 74)
print("通过 %d / %d" % (ok, ok + fail))
sys.exit(0 if fail == 0 else 1)
