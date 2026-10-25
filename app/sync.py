"""增量同步引擎：扫描 diff → 重分组 → 刮削队列。

三道防重复刮削的锁：
  1. scrape_task.item_id 主键（同一条目只有一条任务）
  2. item.status='ok' 短路
  3. item.locked=1（手动改绑）永不被自动覆盖
"""
from __future__ import annotations

import json
import queue
import re
import threading
import time
import traceback
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from . import (config, db, douban, franchise, images, parser, paths, player,
               scanner, tmdb)

CJK_RE = re.compile("[\u4e00-\u9fff]")
BACKOFF = [60, 600, 3600, 21600, 86400]

_state_lock = threading.Lock()
_start_lock = threading.Lock()
_thread = None  # type: Optional[threading.Thread]

_state = {
    "running": False,
    "run_id": None,
    "library_id": None,
    "phase": "idle",
    "done": 0,
    "total": 0,
    "current": "",
    "message": "",
    "counts": {},
    "started_at": None,
}

_subscribers: List["queue.Queue"] = []
_sub_lock = threading.Lock()


# ------------------------------------------------------------------ events
def subscribe() -> "queue.Queue":
    q = queue.Queue(maxsize=500)  # type: queue.Queue
    with _sub_lock:
        _subscribers.append(q)
    return q


def unsubscribe(q: "queue.Queue") -> None:
    with _sub_lock:
        if q in _subscribers:
            _subscribers.remove(q)


def publish(event: Dict[str, Any]) -> None:
    with _sub_lock:
        subs = list(_subscribers)
    for q in subs:
        try:
            q.put_nowait(event)
        except queue.Full:
            pass


def _set(**kw) -> None:
    with _state_lock:
        _state.update(kw)


def status() -> Dict[str, Any]:
    with _state_lock:
        return dict(_state)


def is_running() -> bool:
    with _state_lock:
        return bool(_state["running"])


# ------------------------------------------------------------------ helpers
def _norm_title(t: str) -> str:
    return re.sub(r"[\s\-_.·:：,，、]+", "", (t or "").lower())


def reset_item_from_files(item_id: int) -> Optional[Dict[str, Any]]:
    """按文件名重新解析，抹掉上一次（可能是错的）刮削结果，返回更新后的 item。

    为什么需要这一步：TMDB 命中了错误条目时，`_save_metadata` 会把
    错误片名写进 item.title（《美国队长》→「美国队长3」）、
    错误原名写进 item.original_title（→「Captain America: Civil War」），
    `_payload_from_item` 又正是拿这两个字段拼查询串的 —— 于是刷新多少次
    都还在内战那部里打转（96 Minutes 也犯过一样的毛病）。
    所以「重新刮削」前先回到文件名这个唯一的真相来源。
    """
    item = db.get_item(item_id)
    if not item:
        return None
    files = [f for f in db.files_for_item(item_id) if f.get("role") == "main"]
    if not files:
        files = db.files_for_item(item_id)
    if not files:
        return None
    f = sorted(files, key=lambda x: x.get("ord") or 0)[0]
    base = str(f.get("rel_path") or "").replace("/", "\\").split("\\")[-1]
    low = base.lower()
    for ext in parser.VIDEO_EXT:
        if low.endswith("." + ext):
            base = base[: -(len(ext) + 1)]
            break
    p = parser.parse_name(base)
    if not p or not (p.get("title") or ""):
        return item
    patch = {
        "title": p["title"],
        "original_title": p.get("en_title") or None,
        "year": p.get("year"),
        "year_hint": p.get("year"),
        # 抹掉错误绑定关系，否则新查询又会搜回同一部
        "tmdb_id": None, "imdb_id": None,
        "collection_id": None, "collection_name": None,
        "confidence": 0, "status": "pending", "locked": 0,
    }
    db.update_item(item_id, **patch)
    return db.get_item(item_id)


def _payload_from_item(item: Dict[str, Any]) -> Dict[str, Any]:
    title = item.get("title") or ""
    orig = item.get("original_title") or ""
    # 优先从 group_key（文件名/文件夹名）还原「最初的解析标题」。
    # item.title 已被上一次刮削结果覆盖，一旦刮错（Hunter: Return to
    # Justice → 怪物猎人），重刮会拿污染后的标题精确命中错误影片、
    # 错误自我强化。文件名是唯一不会被刮削覆盖的真相来源。
    # 用户手动改绑（locked=1）尊重用户的标题。
    gk = item.get("group_key") or ""
    if (item.get("kind") == "movie" and not item.get("locked")
            and gk.startswith("movie:")):
        fp = parser.parse_name(gk[len("movie:"):])
        if fp.get("title"):
            title = fp["title"]
            orig = fp.get("en_title") or ""
    cn = title if CJK_RE.search(title) else ""
    en = orig
    if not cn:
        en = en or title
    if not en and CJK_RE.search(title):
        en = orig
    # 优先用「从文件名解析出的年份」：item.year 会被上一次刮削结果覆盖，
    # 一旦刮错（96 Minutes 2025 → 2011）就会自我强化、越错越离谱。
    year = item.get("year_hint") or item.get("year")
    strict = bool(item.get("year_hint"))
    queries = parser.build_queries(cn, en, year)
    if not queries:
        queries = [title]
    # raw：LLM 兜底用。文件名里有语言标签、系列编号这些线索，
    # 只给清洗后的 title 会让它错失信息。
    raw = ""
    try:
        files = db.files_for_item(item["id"])
        if files:
            raw = str(files[0].get("rel_path") or "")
    except Exception:
        raw = ""
    return {"cn_title": cn, "en_title": en, "year": year,
            "year_strict": strict, "title": title, "queries": queries,
            "raw": raw, "kind": item.get("kind")}


def _retry_at(attempts: int) -> str:
    secs = BACKOFF[min(attempts, len(BACKOFF) - 1)]
    return (datetime.now() + timedelta(seconds=secs)).isoformat(timespec="seconds")


# ------------------------------------------------------------------ entry
def start(library_id: int, full: bool = False) -> Dict[str, Any]:
    global _thread
    with _start_lock:
        if is_running():
            return {"ok": False, "error": "sync_running"}
        _set(running=True, phase="scanning", done=0, total=0, current="",
             message="", counts={}, library_id=library_id, run_id=None,
             started_at=datetime.now().isoformat(timespec="seconds"))
    _thread = threading.Thread(target=_run, args=(library_id, full),
                               name="sync", daemon=True)
    _thread.start()
    return {"ok": True}


def _run(library_id: int, full: bool) -> None:
    run_id = db.start_run(library_id)
    _set(run_id=run_id)
    lib = db.get_library(library_id) or {}
    root = lib.get("root_path") or ""
    counts = {"files_seen": 0, "added": 0, "updated": 0, "removed": 0,
              "moved": 0, "scraped_ok": 0, "scraped_fail": 0, "franchises": 0}
    publish({"type": "sync_start", "library_id": library_id, "run_id": run_id})
    try:
        if not root or not _root_available(root):
            raise RuntimeError("媒体库目录不可访问：%s" % root)

        cfg = config.load()
        scan_cfg = cfg.get("scan") or {}
        ignored = db.ignored_keys(library_id)

        # 上次进程被强杀会留下 running 的僵尸任务，复位后重新排队
        db.execute("UPDATE scrape_task SET state='queued', updated_at=?"
                   " WHERE state='running'", (db.now_iso(),))

        # ---------- 阶段 A：扫描 ----------
        _set(phase="scanning", message="正在扫描文件…")
        files = scanner.walk(root, {e.lower() for e in scan_cfg.get("video_ext") or []},
                             scan_cfg.get("min_size_mb", 20),
                             scan_cfg.get("max_depth", 6), ignored,
                             exclude=scan_cfg.get("exclude") or [])
        counts["files_seen"] = len(files)
        _set(total=len(files), done=len(files), message="已扫描 %d 个视频文件" % len(files))

        # 安全阀：与"排除规则之后仍在范围内"的旧记录比较，
        # 否则用户主动排除一个大目录会被误判为共享盘掉线
        excl = scanner.compile_excludes(scan_cfg.get("exclude") or [])
        in_scope = sum(1 for r in db.rows(
            "SELECT rel_path FROM media_file WHERE library_id=? AND missing=0",
            (library_id,)) if not scanner.is_excluded(r["rel_path"], excl))
        if in_scope > 50 and len(files) < in_scope * 0.5:
            raise RuntimeError(
                "扫描到的文件数(%d)远少于库内记录(%d)，疑似共享盘掉线，已中止"
                % (len(files), in_scope))

        # ---------- 阶段 B：重分组 ----------
        _set(phase="grouping", message="正在整理条目…")
        groups = scanner.build_groups(root, files)
        created = _apply_groups(library_id, run_id, groups)

        removed = db.mark_missing(library_id, run_id)
        counts["removed"] = removed
        db.purge_missing(library_id, threshold=2)

        # 改名/移动：新条目尝试继承同名孤儿条目的元数据
        adopted = _adopt_orphans(library_id, created)
        db.delete_orphan_items(library_id)

        # ---------- 入队 ----------
        if full:
            db.enqueue_all_pending(library_id, force=True)
        else:
            for item_id, g in created:
                if item_id in adopted:
                    continue
                db.enqueue_scrape(item_id, json.dumps(
                    _payload_from_item(db.get_item(item_id)), ensure_ascii=False))
            db.enqueue_all_pending(library_id, force=False)

        db.touch_library_scan(library_id)
        counts["added"] = len(created) - len(adopted)
        counts["moved"] = len(adopted)

        # ---------- 阶段 C：刮削 ----------
        total_tasks = db.pending_count(library_id)
        _set(phase="scraping", done=0, total=total_tasks,
             message="待刮削 %d 个条目" % total_tasks)
        scraped = _scrape_loop(library_id)
        counts["scraped_ok"] = scraped["ok"]
        counts["scraped_fail"] = scraped["fail"]

        # ---------- 阶段 D：系列归组 ----------
        _set(phase="grouping", message="正在归组系列…")
        try:
            fr = franchise.rebuild(library_id, _franchise_client())
            counts["franchises"] = fr.get("franchises", 0)
        except Exception as exc:
            traceback.print_exc()
            counts["franchises"] = 0
            counts["franchise_error"] = str(exc)[:200]

        db.finish_run(run_id, status="done", files_seen=counts["files_seen"],
                      added=counts["added"], updated=counts["updated"],
                      removed=counts["removed"], moved=counts["moved"],
                      scraped_ok=counts["scraped_ok"],
                      scraped_fail=counts["scraped_fail"],
                      message=scraped.get("message") or "")
        _set(phase="done", running=False, message="同步完成", counts=counts,
             current="")
        publish({"type": "sync_done", "run_id": run_id, "counts": counts,
                 "message": scraped.get("message") or ""})
    except Exception as exc:
        msg = str(exc)
        db.finish_run(run_id, status="failed", message=msg, **counts)
        _set(phase="error", running=False, message=msg, counts=counts, current="")
        publish({"type": "sync_error", "run_id": run_id, "message": msg})
        traceback.print_exc()


def _root_available(root: str) -> bool:
    import os
    try:
        return os.path.isdir(root) and bool(os.listdir(root))
    except OSError:
        return False


def _apply_groups(library_id: int, run_id: int,
                  groups: List[Dict]) -> List[tuple]:
    existing = {i["group_key"]: i for i in
                db.list_items(library_id, include_hidden=True)}
    created: List[tuple] = []
    batch: List[Dict[str, Any]] = []
    for g in groups:
        row = existing.get(g["group_key"])
        if row:
            item_id = row["id"]
            patch = {}
            if not row.get("year") and g.get("year"):
                patch["year"] = g["year"]
            if not row.get("year_hint") and g.get("year"):
                patch["year_hint"] = g["year"]
            # 标题在首次建条目时就被冻结了，之后解析器怎么改进都不会更新 ——
            # 老库里就留着一批 `Return.To.Silent.Hill.2026.1080p.WEB.DL-SyncUP`
            # 这种没剥干净的标题，导致重新刮削也搜不到。
            # 对「没刮成」且没锁定的条目，每次扫描都重新解析一次标题。
            if (row.get("status") in ("pending", "failed", "unidentified")
                    and not row.get("locked")
                    and g.get("title") and g["title"] != row.get("title")):
                patch["title"] = g["title"]
            if patch:
                db.update_item(item_id, **patch)
        else:
            item_id = db.create_item(library_id, g["group_key"], g["kind"],
                                     g["title"] or "未命名", g.get("year"),
                                     g.get("original_title"))
            created.append((item_id, g))
        # 同步进行中用户可能移除了条目；已删除的分组跳过，
        # 否则 upsert 会因外键悬挂让整轮同步报错
        if not db.get_item(item_id):
            continue
        for f in g["files"]:
            batch.append({
                "item_id": item_id, "rel_path": f["rel_path"],
                "rel_path_key": f["rel_path_key"], "size": f["size"],
                "mtime": f["mtime"], "ext": f["ext"],
                "season": f.get("season"), "episode": f.get("episode"),
                "ep_title": f.get("ep_title"),
                "role": f.get("role") or "main", "ord": f.get("ord") or 0,
            })
    db.upsert_files_batch(library_id, batch, run_id)
    return created


def _adopt_orphans(library_id: int, created: List[tuple]) -> set:
    """新条目若与某个失去文件的旧条目同名同类型，继承其元数据，避免重新刮削。"""
    if not created:
        return set()
    orphan_rows = db.rows(
        "SELECT * FROM item WHERE library_id=? AND id NOT IN "
        "(SELECT DISTINCT item_id FROM media_file WHERE library_id=? "
        " AND item_id IS NOT NULL AND missing=0)", (library_id, library_id))
    if not orphan_rows:
        return set()
    pool: Dict[tuple, Dict[str, Any]] = {}
    for r in orphan_rows:
        d = dict(r)
        pool.setdefault((d["kind"], _norm_title(d["title"])), d)
    adopted = set()
    for item_id, g in created:
        key = (g["kind"], _norm_title(g["title"]))
        donor = pool.get(key)
        if not donor or not donor.get("tmdb_id"):
            continue
        db.update_item(
            item_id, original_title=donor.get("original_title"),
            year=donor.get("year") or g.get("year"),
            overview=donor.get("overview"), overview_en=donor.get("overview_en"),
            poster_path=donor.get("poster_path"), poster_src=donor.get("poster_src"),
            backdrop_path=donor.get("backdrop_path"), rating=donor.get("rating"),
            runtime=donor.get("runtime"), genres=donor.get("genres"),
            cast_json=donor.get("cast_json"), director=donor.get("director"),
            tmdb_id=donor.get("tmdb_id"), imdb_id=donor.get("imdb_id"),
            source=donor.get("source") or "tmdb", confidence=donor.get("confidence"),
            status="ok")
        db.delete_items([donor["id"]])
        adopted.add(item_id)
    return adopted


# ------------------------------------------------------------------ scraping
def _scrape_loop(library_id: int) -> Dict[str, Any]:
    """多线程刮削队列。瓶颈是网络延迟，并发能显著缩短首次全量时间。"""
    cfg = config.load()
    client = tmdb.Tmdb(cfg.get("tmdb") or {})   # 限速器线程安全，全局共享
    db_cfg = cfg.get("douban") or {}
    workers = max(1, min(6, int((cfg.get("sync") or {}).get("scrape_workers") or 3)))

    total = db.pending_count(library_id)
    _set(total=total, done=0, phase="scraping")
    lock = threading.Lock()
    stats = {"ok": 0, "fail": 0, "done": 0, "message": ""}
    stop = threading.Event()

    def worker() -> None:
        while not stop.is_set():
            task = db.claim_task()
            if not task:
                return
            item = db.get_item(task["item_id"])
            if not item:
                db.set_task_state(task["item_id"], "skipped")
                continue
            # LLM 兜底只在首次尝试时启用：重试说明是网络/限流问题，
            # 没必要反复花钱。用户手动「重新刮削」时另行放行。
            task["allow_llm"] = (task.get("attempts") or 0) <= 1
            with lock:
                stats["done"] += 1
                done = stats["done"]
            _set(done=done, current=item.get("title") or "")
            publish({"type": "progress", "phase": "scraping", "done": done,
                     "total": total, "current": item.get("title") or ""})
            try:
                result = _scrape_one(item, task, client, db_cfg, cfg)
                if result is None:
                    db.update_item(item["id"], status="unidentified",
                                   source="local", confidence=0)
                    db.set_task_state(item["id"], "skipped")
                else:
                    _save_metadata(item["id"], result)
                    db.set_task_state(item["id"], "done")
                    with lock:
                        stats["ok"] += 1
                publish({"type": "item_updated", "id": item["id"]})
            except tmdb.ApiKeyError as exc:
                db.set_task_state(item["id"], "queued", str(exc))
                db.update_item(item["id"], status="pending")
                with lock:
                    stats["message"] = "TMDB API Key 无效，已中止本次刮削：%s" % exc
                stop.set()
                publish({"type": "sync_error", "message": stats["message"]})
                return
            except Exception as exc:
                with lock:
                    stats["fail"] += 1
                attempts = (task.get("attempts") or 0) + 1
                db.set_task_state(item["id"], "failed", str(exc),
                                  _retry_at(attempts), bump=True)
                db.update_item(item["id"], status="failed")
                publish({"type": "item_updated", "id": item["id"]})

    threads = [threading.Thread(target=worker, name="scrape-%d" % i, daemon=True)
               for i in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return {"ok": stats["ok"], "fail": stats["fail"], "message": stats["message"]}


def _scrape_one(item: Dict[str, Any], task: Dict[str, Any],
                client: tmdb.Tmdb, db_cfg: Dict[str, Any],
                cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    # 载荷每次现算，不要用任务里缓存的那份：
    # 队列里可能躺着几天前入队的任务，里面存的是旧年份/旧片名，
    # 一旦上次刮错就会一直用错线索重试（96 Minutes 2025 → 2011 的死循环）。
    payload = _payload_from_item(item)

    kind = item["kind"]
    if kind == "collection":
        return None

    media_kind = "tv" if kind == "series" else "movie"
    match = client.search(payload, media_kind)
    if not match and media_kind == "movie":
        # 有些条目其实是一部剧的单集（Greatest.Events.of.WWII.in.Colour.E10），
        # 按电影搜不到，退回剧集再试一次
        alt = client.search(payload, "tv")
        if alt and alt.get("score", 0) >= 0.7:
            match, media_kind = alt, "tv"
    if match:
        details = client.details(match["tmdb_id"], media_kind)
        poster = None
        if details.get("poster_path"):
            poster = images.fetch_poster(details["poster_path"], "tmdb",
                                         match["tmdb_id"])
        # 详情接口没有海报时，去 images 接口捞任意语言的海报
        if not poster:
            alt = client.any_poster(match["tmdb_id"], media_kind)
            if alt:
                poster = images.fetch_poster(alt, "tmdb", match["tmdb_id"])
        # 背景图只在打开详情页时懒加载，省掉约 3s/条的下载耗时
        backdrop = None
        out = dict(details)
        out["poster_local"] = poster
        out["backdrop_local"] = backdrop
        out["backdrop_url"] = details.get("backdrop_path") or None
        out["source"] = "tmdb"
        out["confidence"] = round(match["score"], 2)
        out["status"] = "ok"
        out["seasons"] = []
        out["episodes"] = []
        if media_kind == "tv":
            out["seasons"] = details.get("seasons") or []
            numbers = sorted({f["season"] for f in db.files_for_item(item["id"])
                              if f.get("season")})
            eps = []
            for sn in numbers[:3]:
                try:
                    eps.extend(client.season(match["tmdb_id"], sn))
                except tmdb.TmdbError:
                    continue
            out["episodes"] = eps
        # 最后兜底：TMDB 这条确实没有海报，用图片搜索补一张
        if not poster and _web_poster_enabled(cfg):
            poster = images.fetch_web_poster(
                out.get("title") or item.get("title") or "",
                out.get("year"), key=match["tmdb_id"])
            if poster:
                out["poster_local"] = poster
                out["source"] = "tmdb+web"
        return out

    # TMDB 未命中 → 豆瓣兜底
    if (db_cfg or {}).get("enabled", True):
        hit = douban.pick(payload, db_cfg)
        if hit:
            poster = images.fetch_poster(hit["img"], "douban", hit["img"]) if hit.get("img") else None
            if not poster and _web_poster_enabled(cfg):
                poster = images.fetch_web_poster(
                    hit.get("title") or item.get("title") or "",
                    hit.get("year"), key=hit.get("img") or item.get("title"))
            return {
                "title": hit["title"] or item["title"],
                "original_title": hit.get("sub_title") or item.get("original_title"),
                "year": int(hit["year"]) if str(hit.get("year") or "").isdigit() else item.get("year"),
                "overview": "",
                "poster_local": poster,
                "backdrop_local": None,
                "source": "douban",
                "confidence": round(hit.get("score") or 0, 2),
                "status": "ok",
                "rating": None, "runtime": None, "genres": None,
                "cast": None, "director": None, "imdb_id": None,
                "tmdb_id": None,
            }

    # ---- LLM 兜底：TMDB（含跨语言重搜）+ 豆瓣都没命中时才试。
    # 典型场景是港台译名与 TMDB 官方译名完全无交集
    # （《生死关头》 vs 《007：你死我活》），从文件名派生的查询串永远撞不上，
    # 得靠「007 系列第 8 部」这类外部知识搭桥。
    # LLM 只出候选搜索词，绝不出 ID —— 候选仍要过 client.search() 打分管线。
    if task.get("allow_llm") and not item.get("locked"):
        hit = _llm_retry(item, payload, client, media_kind, cfg)
        if hit:
            return hit

    # 全部图源都没命中：至少用图片搜索补一张海报，别让海报墙留白
    if _web_poster_enabled(cfg):
        title = payload.get("cn_title") or payload.get("en_title") or item.get("title") or ""
        poster = images.fetch_web_poster(title, item.get("year"),
                                         key=item.get("group_key"))
        if poster:
            return {
                "poster_local": poster,
                "source": "web",
                "confidence": 0,
                "status": "unidentified",
            }
    return None


def _web_poster_enabled(cfg: Dict[str, Any]) -> bool:
    return bool((cfg.get("poster") or {}).get("web_search", True))


def _llm_retry(item: Dict[str, Any], payload: Dict[str, Any],
               client: tmdb.Tmdb, media_kind: str,
               cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """让 LLM 猜几个候选片名，再拿这些词走一遍正常的 TMDB 检索+打分。

    返回 None 表示猜不中 —— 由调用方继续走海报 web 兜底。
    """
    try:
        from . import llm as _llm
    except Exception:                                  # pragma: no cover
        return None
    if not _llm.enabled():
        return None
    files = db.files_for_item(item["id"])
    raw = ""
    if files:
        raw = str(files[0].get("rel_path") or "")
    ask = dict(payload)
    ask["raw"] = raw
    ask["kind"] = item.get("kind")
    sug = _llm.suggest(ask)
    cands = sug.get("queries") or []
    if not cands:
        return None
    year = sug.get("year") or payload.get("year")
    best = None
    for q in cands:
        cn = q if CJK_RE.search(q) else ""
        en = "" if cn else q
        p2 = {"cn_title": cn, "en_title": en, "title": q, "year": year,
              "queries": parser.build_queries(cn, en, year),
              "year_strict": bool(year)}
        m = client.search(p2, media_kind)
        if not m:
            continue
        # 候选词本身已经经过打分筛选，这里再要求一次下限，
        # 防止 LLM 编个热门片名把冷门条目强行认成它
        if m.get("score", 0) < 0.6 or (best and m["score"] <= best["score"]):
            continue
        best = m
    if not best:
        return None
    details = client.details(best["tmdb_id"], media_kind)
    if not details:
        return None
    poster = None
    if details.get("poster_path"):
        poster = images.fetch_poster(details["poster_path"], "tmdb",
                                     best["tmdb_id"])
    if not poster and _web_poster_enabled(cfg):
        poster = images.fetch_web_poster(
            details.get("title") or item.get("title") or "", details.get("year"),
            key=item.get("group_key"))
    out = details
    out.update({
        "poster_local": poster, "source": "tmdb+llm",
        "confidence": min(0.9, round(best.get("score", 0) * 0.9 + 0.1, 2)),
        "status": "ok", "llm_candidates": cands,
    })
    return out


def _franchise_client() -> Optional[tmdb.Tmdb]:
    """系列归组时用来取 collection 封面；没有 key 就跳过（不阻塞归组）。"""
    cfg = config.load()
    if not (cfg.get("tmdb") or {}).get("api_key"):
        return None
    return tmdb.Tmdb(cfg.get("tmdb") or {})


def _keep_season_meta(item_id: int, new_title: str,
                      new_year) -> tuple:
    """剧集条目按「季」拆开时，别让 TMDB 的剧名把季号抹掉。

    「圆桌派 第八季」刮到的 TMDB 剧名是「圆桌派」，首播年是 2016。
    如果直接覆盖，八季会变成八个同名同年条目，系列归组也就失效了。
    这种情况下保留原来的标题和年份，只补海报/简介/评分。
    """
    row = db.row("SELECT kind, title FROM item WHERE id=?", (item_id,))
    if not row or row["kind"] != "series":
        return new_title, new_year
    old = row["title"] or ""
    if franchise.base_series_title(old) and not franchise.base_series_title(new_title or ""):
        return old, None
    return new_title, new_year


def _save_metadata(item_id: int, r: Dict[str, Any]) -> None:
    title, year = _keep_season_meta(item_id, r.get("title") or "", r.get("year"))
    all_fields = {
        "title": title or None,
        "original_title": r.get("original_title") or None,
        "year": year,
        "overview": r.get("overview") or None,
        "overview_en": r.get("overview_en") or None,
        "poster_path": r.get("poster_local"),
        "poster_src": r.get("source"),
        "backdrop_path": r.get("backdrop_local") or r.get("backdrop_url"),
        "rating": r.get("rating"),
        "runtime": r.get("runtime"),
        "genres": json.dumps(r.get("genres"), ensure_ascii=False) if r.get("genres") else None,
        "cast_json": json.dumps(r.get("cast"), ensure_ascii=False) if r.get("cast") else None,
        "director": r.get("director"),
        "tmdb_id": r.get("tmdb_id"),
        "imdb_id": r.get("imdb_id"),
        "collection_id": r.get("collection_id"),
        "collection_name": r.get("collection_name"),
        "source": r.get("source") or "tmdb",
        "confidence": r.get("confidence") or 0,
        "status": r.get("status") or "ok",
    }
    # 只有 TMDB 结果带 adult 标记；写它的同时记下分级补全时间，
    # 避免存量补全流程再重复请求一遍
    if "adult" in r:
        all_fields["adult"] = 1 if r.get("adult") else 0
        all_fields["certification"] = r.get("certification") or ""
        all_fields["kid_meta_at"] = db.now_iso()
    fields = {k: v for k, v in all_fields.items() if v is not None}
    if r.get("status") == "ok":
        # 完整刮削成功：以本次结果为准，空字段也要覆盖清掉。
        # 否则换绑重刮后，上一部影片的简介/演员表会残留
        # （Hunter: Return to Justice 换对后 overview 还是怪物猎人的）。
        db.update_item(item_id, **all_fields)
    else:
        # 部分结果（如仅补到一张海报）：只更新拿到的字段，别清空旧数据
        db.update_item(item_id, **fields)
    if r.get("seasons") or r.get("episodes"):
        db.replace_seasons(item_id, r.get("seasons") or [], r.get("episodes") or [])


def rescrape_item(item_id: int) -> bool:
    """单条重新刮削（同步执行，用于手动改绑后的即时反馈）。"""
    item = db.get_item(item_id)
    if not item:
        return False
    cfg = config.load()
    client = tmdb.Tmdb(cfg.get("tmdb") or {})
    db.update_item(item_id, status="pending")
    # state='running' 立即占住任务，防止同步 worker 同时认领造成并发刮削
    db.enqueue_scrape(item_id, json.dumps(_payload_from_item(item), ensure_ascii=False),
                      state="running")
    task = {"item_id": item_id, "query": None, "attempts": 0,
            "allow_llm": True}
    try:
        r = _scrape_one(item, task, client, cfg.get("douban") or {}, cfg)
        if r:
            _save_metadata(item_id, r)
            db.set_task_state(item_id, "done")
            return True
        db.update_item(item_id, status="unidentified")
        db.set_task_state(item_id, "skipped")
    except tmdb.ApiKeyError:
        db.set_task_state(item_id, "failed", "API key 无效")
        db.update_item(item_id, status="failed")
    except Exception as exc:
        db.set_task_state(item_id, "failed", str(exc))
        db.update_item(item_id, status="failed")
    return False


def rebind(item_id: int, tmdb_id: int) -> bool:
    """手动改绑 TMDB ID：置 locked=1，之后自动刮削永不覆盖。"""
    item = db.get_item(item_id)
    if not item:
        return False
    cfg = config.load()
    client = tmdb.Tmdb(cfg.get("tmdb") or {})
    kind = "tv" if item["kind"] == "series" else "movie"
    details = client.details(tmdb_id, kind)
    poster = None
    if details.get("poster_path"):
        poster = images.fetch_poster(details["poster_path"], "tmdb", tmdb_id)
    r = dict(details)
    r["poster_local"] = poster
    r["backdrop_local"] = None
    r["backdrop_url"] = details.get("backdrop_path") or None
    r["source"] = "manual"
    r["confidence"] = 1.0
    r["status"] = "ok"
    r["seasons"] = details.get("seasons") or []
    r["episodes"] = []
    if kind == "tv":
        numbers = sorted({f["season"] for f in db.files_for_item(item_id) if f.get("season")})
        for sn in numbers[:3]:
            try:
                r["episodes"].extend(client.season(tmdb_id, sn))
            except tmdb.TmdbError:
                continue
    _save_metadata(item_id, r)
    db.update_item(item_id, locked=1, status="ok", tmdb_id=tmdb_id,
                   source="manual", confidence=1.0)
    db.set_task_state(item_id, "done")
    publish({"type": "item_updated", "id": item_id})
    return True


# ------------------------------------------------------------ 分级补全
# 存量条目入库时还没存 adult/分级，这里联网补一次（只更新这三列，
# 不碰标题/海报等已有元数据）
_kid_meta_lock = threading.Lock()
_kid_meta_state = {
    "running": False,
    "done": 0,
    "total": 0,
    "error": "",
}


def kid_meta_state() -> Dict[str, Any]:
    with _kid_meta_lock:
        return dict(_kid_meta_state)


def start_kid_meta_backfill(library_id: int) -> Dict[str, Any]:
    cfg = config.load()
    if not (cfg.get("tmdb") or {}).get("api_key"):
        return {"ok": False, "error": "no_key",
                "message": "未配置 TMDB API Key，无法补全分级"}
    total = db.kid_meta_pending(library_id)
    with _kid_meta_lock:
        if _kid_meta_state["running"]:
            return {"ok": False, "error": "running",
                    "message": "分级补全正在进行", "total": _kid_meta_state["total"],
                    "done": _kid_meta_state["done"]}
        _kid_meta_state.update({"running": True, "done": 0, "total": total,
                                "error": ""})
    if not total:
        with _kid_meta_lock:
            _kid_meta_state["running"] = False
        return {"ok": True, "total": 0, "message": "分级信息已是最新"}
    threading.Thread(target=_run_kid_meta_backfill, args=(library_id,),
                     daemon=True).start()
    return {"ok": True, "total": total}


def _run_kid_meta_backfill(library_id: int) -> None:
    cfg = config.load()
    client = tmdb.Tmdb(cfg.get("tmdb") or {})
    try:
        targets = db.kid_meta_targets(library_id)
        total = len(targets)
        done = 0
        for it in targets:
            kind = "tv" if it["kind"] == "series" else "movie"
            try:
                r = client.ratings(int(it["tmdb_id"]), kind)
                db.update_item(it["id"],
                               adult=1 if r.get("adult") else 0,
                               certification=r.get("certification") or "",
                               kid_meta_at=db.now_iso())
            except tmdb.ApiKeyError:
                raise
            except tmdb.TmdbError as exc:
                # 单条失败不中断：下一轮补全还会再试
                print("[kids] 分级补全失败 %s: %s" % (it.get("title"), exc))
                continue
            done += 1
            with _kid_meta_lock:
                _kid_meta_state["done"] = done
            if done % 10 == 0 or done == total:
                publish({"type": "kid_meta", "done": done, "total": total})
        publish({"type": "kid_meta", "done": done, "total": total})
        publish({"type": "kid_meta_done", "done": done, "total": total})
        with _kid_meta_lock:
            _kid_meta_state.update({"running": False, "done": done,
                                    "total": total, "error": ""})
    except Exception as exc:
        traceback.print_exc()
        with _kid_meta_lock:
            _kid_meta_state.update({"running": False, "error": str(exc)})
        publish({"type": "kid_meta_done", "done": _kid_meta_state["done"],
                 "total": _kid_meta_state["total"], "error": str(exc)})
