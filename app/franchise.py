"""系列归组：把「哈利波特 1~8」「007 系列」「圆桌派 第一~八季」收成一张系列卡。

两条来源：
  1. TMDB 的 belongs_to_collection —— 电影系列最可靠（哈利波特 / 007 / 指环王…）
  2. 剧集同名季合并 —— TMDB 没有 collection 概念，
     把「圆桌派 第一季 / 第二季 …」按去掉季号后的基名归到一起

归组结果写到 franchise 表，成员通过 item.franchise_id 指回来。
只有 ≥2 个成员才建系列，单片不会变成系列。

第三条来源是**用户自定义大系列**（config.json 的 `super_series`）：
把若干 TMDB 子系列 / 单片收进一个总系列，比如「漫威超英电影」里再分
「钢铁侠系列」「雷神系列」。成员 item.franchise_id 指向大系列，
子系列名写在 item.series_group 上，前端据此分节展示。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from . import config, db, images
from .paths import POSTER_DIR

# 季号尾巴：第五季 / 第 2 季 / Season 3 / S01
_RE_SEASON_SUFFIX = re.compile(
    r"[\s·\-—_:：,，]*"
    r"(?:第\s*[一二三四五六七八九十百零\d]{1,3}\s*[季部]|season\s*\d{1,2}|(?<![a-z])s\d{1,2})"
    r"\s*$",
    re.I,
)
# 常见中文数字季号（第八季 -> 8），用于给成员排序
_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
           "八": 8, "九": 9, "十": 10}
_RE_SEASON_NUM = re.compile(r"第\s*([一二三四五六七八九十百零\d]{1,3})\s*[季部]", re.I)


def base_series_title(title: str) -> str:
    """去掉季号后的基名；没有季号时返回空串（说明它本身不是"某一季"）。"""
    t = (title or "").strip()
    if not t:
        return ""
    cur = t
    for _ in range(3):
        nxt = _RE_SEASON_SUFFIX.sub("", cur).strip(" ·-—_")
        if nxt == cur:
            break
        cur = nxt
    if cur and cur != t and len(cur) >= 2:
        return cur
    return ""


def season_no(title: str) -> int:
    m = _RE_SEASON_NUM.search(title or "")
    if not m:
        return 0
    raw = m.group(1)
    if raw.isdigit():
        return int(raw)
    n = 0
    for ch in raw:
        n = n * 10 + _CN_NUM.get(ch, 0)
    return n


def _norm_key(s: str) -> str:
    return re.sub(r"[\s·\-—_：:，,。.（）()\[\]【】]+", "", (s or "").lower())


def rebuild(library_id: int, client=None) -> Dict[str, int]:
    """重建该库的系列归组。返回统计。"""
    db.clear_item_franchises(library_id)
    items = [dict(r) for r in db.rows(
        "SELECT * FROM item WHERE library_id=? AND hidden=0", (library_id,))]

    # ---- 规则 1：TMDB collection（只对电影生效）
    by_collection: Dict[int, List[Dict[str, Any]]] = {}
    for it in items:
        cid = it.get("collection_id")
        if cid and it.get("kind") == "movie":
            by_collection.setdefault(int(cid), []).append(it)

    # ---- 规则 2：剧集同名季
    by_base: Dict[str, List[Dict[str, Any]]] = {}
    for it in items:
        if it.get("kind") != "series":
            continue
        base = base_series_title(it.get("title") or "")
        if base:
            by_base.setdefault(_norm_key(base), []).append(it)

    created = 0
    members_total = 0

    def _apply(key: str, name: str, members: List[Dict[str, Any]],
               source: str, collection_id: Optional[int] = None) -> None:
        nonlocal created, members_total
        if len(members) < 2:
            return
        members.sort(key=lambda m: (season_no(m.get("title") or ""),
                                    m.get("year") or 0, m.get("title") or ""))
        poster, backdrop, overview = None, None, None
        if collection_id and client is not None:
            try:
                col = client.collection(collection_id)
                name = col.get("name") or name
                overview = col.get("overview") or None
                if col.get("poster_path"):
                    poster = images.download(
                        col["poster_path"], POSTER_DIR,
                        "coll_%s.jpg" % collection_id)
                if col.get("backdrop_path"):
                    backdrop = images.download(
                        col["backdrop_path"], POSTER_DIR,
                        "collbd_%s.jpg" % collection_id)
            except Exception:
                pass
        if not poster:
            poster = next((m.get("poster_path") for m in members
                           if m.get("poster_path")), None)
        years = [m.get("year") for m in members if m.get("year")]
        fid = db.upsert_franchise(
            library_id, key, name, source=source, collection_id=collection_id,
            poster_path=poster, backdrop_path=backdrop, overview=overview,
            sort_year=min(years) if years else None)
        for m in members:
            db.set_item_franchise(m["id"], fid)
        created += 1
        members_total += len(members)

    for cid, members in by_collection.items():
        name = next((m.get("collection_name") for m in members
                     if m.get("collection_name")), "") or ("系列 %d" % cid)
        _apply("tmdb:%d" % cid, name, members, "tmdb", collection_id=cid)

    for key, members in by_base.items():
        if len(members) < 2:
            continue
        # 基名取最短的那个成员标题去掉季号后的样子
        name = base_series_title(members[0].get("title") or "") or members[0]["title"]
        _apply("season:" + key, name, members, "season")

    # ---- 规则 3：用户自定义大系列（会覆盖上面给 item.franchise_id 的赋值，
    #      让条目改指大系列；子系列名记到 item.series_group）
    supers = _apply_super_series(library_id, items)
    created += len(supers)
    members_total += sum(s["members"] for s in supers)

    db.delete_empty_franchises(library_id)
    return {"franchises": created, "members": members_total}


# ------------------------------------------------------------ 大系列（用户自定义）
def _ids(v: Any) -> List[int]:
    out = []
    for x in (v or []):
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            continue
    return out


def _blob(it: Dict[str, Any]) -> str:
    """条目上可用于关键词比对的文本，一次性小写化。

    故意不含 collection_name —— TMDB 给《蜘蛛侠：英雄归来》的合集中文名叫
    「蜘蛛侠（复仇者联盟）（系列）」，拿它做包含匹配会把它误分进复仇者联盟。
    合集归属请用 collection_ids 精确匹配。
    """
    return " \n".join([str(it.get("title") or ""),
                       str(it.get("original_title") or "")]).lower()


def _match_child(it: Dict[str, Any], child: Dict[str, Any]) -> bool:
    """判断条目是否落在某个子系列里。优先级：collection_id > tmdb_id > 关键词。"""
    cid = it.get("collection_id")
    if cid and int(cid) in _ids(child.get("collection_ids")):
        return True
    tid = it.get("tmdb_id")
    if tid and int(tid) in _ids(child.get("tmdb_ids")):
        return True
    hay = _blob(it)
    for kw in (child.get("keywords") or []):
        kw = str(kw or "").strip().lower()
        if kw and kw in hay:
            return True
    return False


def super_series_config() -> List[Dict[str, Any]]:
    cfg = config.load() or {}
    out = []
    for sp in (cfg.get("super_series") or []):
        if not isinstance(sp, dict):
            continue
        name = str(sp.get("name") or "").strip()
        kids = [c for c in (sp.get("children") or []) if isinstance(c, dict)]
        if name and kids:
            out.append({"name": name, "children": kids})
    return out


def _apply_super_series(library_id: int,
                        items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按 config.json 的 super_series 重新归组。返回每个大系列的统计。"""
    supers = super_series_config()
    db.clear_item_series_groups(library_id)
    if not supers:
        return []

    # 每个大系列：{子系列名: [item, ...]}，按 config 里 children 的顺序排
    buckets: List[Dict[str, Any]] = []
    for sp in supers:
        names = [str(c.get("name") or "").strip() or "未命名子系列"
                 for c in sp["children"]]
        buckets.append({"sp": sp, "names": names,
                        "children": {n: [] for n in names}})

    for it in items:
        for b in buckets:
            hit = None
            for child, cname in zip(b["sp"]["children"], b["names"]):
                if _match_child(it, child):
                    hit = cname
                    break
            if hit:
                b["children"][hit].append(it)
                break  # 一个条目只进一个大系列

    stats = []
    for b in buckets:
        groups = [(n, ms) for n, ms in b["children"].items() if ms]
        if not groups:
            continue
        flat = [m for _, ms in groups for m in ms]
        years = [m.get("year") for m in flat if m.get("year")]
        poster = next((m.get("poster_path") for m in flat
                       if m.get("poster_path")), None)
        key = "super:%s" % _norm_key(b["sp"]["name"])
        fid = db.upsert_franchise(
            library_id, key, b["sp"]["name"], source="super",
            poster_path=poster, sort_year=min(years) if years else None)
        for cname, ms in groups:
            for m in ms:
                db.set_item_franchise(m["id"], fid)
                db.set_item_series_group(m["id"], cname)
        stats.append({"name": b["sp"]["name"], "children": len(groups),
                      "members": len(flat), "n": len(flat)})
    return stats
