"""SQLite 访问层：建表、连接管理、DAO。

线程模型：每线程一个连接（threading.local），全局写锁串行化写入。
注意：绝不在持有事务时发起网络请求。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from .paths import DB_PATH, ensure_dirs

_local = threading.local()
_write_lock = threading.RLock()

SCHEMA_VERSION = 3

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS library (
  id          INTEGER PRIMARY KEY,
  name        TEXT NOT NULL,
  root_path   TEXT NOT NULL,
  root_key    TEXT NOT NULL UNIQUE,
  created_at  TEXT NOT NULL,
  last_scan_at TEXT
);

CREATE TABLE IF NOT EXISTS franchise (
  id             INTEGER PRIMARY KEY,
  library_id     INTEGER NOT NULL REFERENCES library(id) ON DELETE CASCADE,
  key            TEXT NOT NULL,
  name           TEXT NOT NULL,
  source         TEXT NOT NULL DEFAULT 'tmdb',
  collection_id  INTEGER,
  poster_path    TEXT,
  backdrop_path  TEXT,
  overview       TEXT,
  sort_year      INTEGER,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL,
  UNIQUE(library_id, key)
);

CREATE TABLE IF NOT EXISTS item (
  id             INTEGER PRIMARY KEY,
  library_id     INTEGER NOT NULL REFERENCES library(id) ON DELETE CASCADE,
  group_key      TEXT NOT NULL,
  kind           TEXT NOT NULL CHECK(kind IN ('movie','series','collection')),
  title          TEXT NOT NULL,
  original_title TEXT,
  year           INTEGER,
  overview       TEXT,
  overview_en    TEXT,
  poster_path    TEXT,
  poster_src     TEXT,
  backdrop_path  TEXT,
  rating         REAL,
  runtime        INTEGER,
  genres         TEXT,
  cast_json      TEXT,
  director       TEXT,
  tmdb_id        INTEGER,
  imdb_id        TEXT,
  collection_id  INTEGER,
  collection_name TEXT,
  franchise_id   INTEGER REFERENCES franchise(id) ON DELETE SET NULL,
  series_group   TEXT,
  year_hint      INTEGER,
  source         TEXT NOT NULL DEFAULT 'local',
  confidence     REAL DEFAULT 0,
  status         TEXT NOT NULL DEFAULT 'pending'
                 CHECK(status IN ('pending','ok','failed','unidentified')),
  locked         INTEGER NOT NULL DEFAULT 0,
  hidden         INTEGER NOT NULL DEFAULT 0,
  adult          INTEGER NOT NULL DEFAULT 0,
  certification  TEXT,
  kid_blocked    INTEGER NOT NULL DEFAULT 0,
  kid_allowed    INTEGER NOT NULL DEFAULT 0,
  kid_meta_at    TEXT,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL,
  UNIQUE(library_id, group_key)
);

CREATE TABLE IF NOT EXISTS media_file (
  id            INTEGER PRIMARY KEY,
  library_id    INTEGER NOT NULL REFERENCES library(id) ON DELETE CASCADE,
  item_id       INTEGER REFERENCES item(id) ON DELETE CASCADE,
  rel_path      TEXT NOT NULL,
  rel_path_key  TEXT NOT NULL,
  size          INTEGER NOT NULL,
  mtime         REAL NOT NULL,
  ext           TEXT NOT NULL,
  season        INTEGER,
  episode       INTEGER,
  ep_title      TEXT,
  role          TEXT NOT NULL DEFAULT 'main' CHECK(role IN ('main','extra')),
  ord           INTEGER DEFAULT 0,
  missing       INTEGER NOT NULL DEFAULT 0,
  miss_count    INTEGER NOT NULL DEFAULT 0,
  last_seen_run INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  UNIQUE(library_id, rel_path_key)
);

CREATE TABLE IF NOT EXISTS ignored_path (
  library_id   INTEGER NOT NULL REFERENCES library(id) ON DELETE CASCADE,
  rel_path_key TEXT NOT NULL,
  rel_path     TEXT NOT NULL,
  title        TEXT,
  removed_at   TEXT NOT NULL,
  PRIMARY KEY (library_id, rel_path_key)
);

CREATE TABLE IF NOT EXISTS season (
  item_id        INTEGER NOT NULL REFERENCES item(id) ON DELETE CASCADE,
  season_number  INTEGER NOT NULL,
  name           TEXT,
  overview       TEXT,
  poster_path    TEXT,
  episode_count  INTEGER,
  PRIMARY KEY (item_id, season_number)
);

CREATE TABLE IF NOT EXISTS episode (
  item_id        INTEGER NOT NULL REFERENCES item(id) ON DELETE CASCADE,
  season_number  INTEGER NOT NULL,
  episode_number INTEGER NOT NULL,
  name           TEXT,
  overview       TEXT,
  still_path     TEXT,
  air_date       TEXT,
  runtime        INTEGER,
  PRIMARY KEY (item_id, season_number, episode_number)
);

CREATE TABLE IF NOT EXISTS sync_run (
  id           INTEGER PRIMARY KEY,
  library_id   INTEGER,
  started_at   TEXT NOT NULL,
  finished_at  TEXT,
  status       TEXT NOT NULL DEFAULT 'running'
               CHECK(status IN ('running','done','failed','aborted')),
  files_seen   INTEGER DEFAULT 0,
  added        INTEGER DEFAULT 0,
  updated      INTEGER DEFAULT 0,
  removed      INTEGER DEFAULT 0,
  moved        INTEGER DEFAULT 0,
  scraped_ok   INTEGER DEFAULT 0,
  scraped_fail INTEGER DEFAULT 0,
  message      TEXT
);

CREATE TABLE IF NOT EXISTS scrape_task (
  item_id       INTEGER PRIMARY KEY REFERENCES item(id) ON DELETE CASCADE,
  query         TEXT,
  state         TEXT NOT NULL DEFAULT 'queued'
                CHECK(state IN ('queued','running','done','failed','skipped')),
  attempts      INTEGER NOT NULL DEFAULT 0,
  last_error    TEXT,
  next_retry_at TEXT,
  updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS http_cache (
  cache_key  TEXT PRIMARY KEY,
  body       TEXT NOT NULL,
  status     INTEGER,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
  key   TEXT PRIMARY KEY,
  value TEXT
);

CREATE INDEX IF NOT EXISTS idx_item_lib     ON item(library_id, status, hidden);
CREATE INDEX IF NOT EXISTS idx_item_tmdb    ON item(kind, tmdb_id);
CREATE INDEX IF NOT EXISTS idx_file_item    ON media_file(item_id);
CREATE INDEX IF NOT EXISTS idx_file_seen    ON media_file(library_id, last_seen_run);
CREATE INDEX IF NOT EXISTS idx_task_ready   ON scrape_task(state, next_retry_at);
"""


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        ensure_dirs()
        conn = sqlite3.connect(str(DB_PATH), timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        _local.conn = conn
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """幂等迁移：老库缺的列在这里补上（CREATE TABLE IF NOT EXISTS 不会加列）。"""
    have = {r[1] for r in conn.execute("PRAGMA table_info(item)").fetchall()}
    for col, decl in (
        ("collection_id", "INTEGER"),
        ("collection_name", "TEXT"),
        ("franchise_id", "INTEGER"),
        # 从文件名解析出的年份。item.year 会被刮削结果覆盖（可能刮错），
        # year_hint 只写一次、永不覆盖，作为重新刮削时的可靠线索。
        ("year_hint", "INTEGER"),
        # 大系列（超英/自定义合集）里的子系列名。item.franchise_id 指向大系列，
        # series_group 记住它属于哪个子系列，前端据此分节展示。
        ("series_group", "TEXT"),
        # 青少年模式：TMDB adult 标记、分级、人工屏蔽/放行、分级补全时间
        ("adult", "INTEGER NOT NULL DEFAULT 0"),
        ("certification", "TEXT"),
        ("kid_blocked", "INTEGER NOT NULL DEFAULT 0"),
        ("kid_allowed", "INTEGER NOT NULL DEFAULT 0"),
        ("kid_meta_at", "TEXT"),
    ):
        if col not in have:
            conn.execute("ALTER TABLE item ADD COLUMN %s %s" % (col, decl))
    conn.execute("CREATE INDEX IF NOT EXISTS idx_item_franchise"
                 " ON item(library_id, franchise_id)")
    conn.commit()


def init_db() -> None:
    with _write_lock:
        conn = connect()
        conn.executescript(SCHEMA_SQL)
        conn.commit()
        _migrate(conn)
        set_kv("schema_version", str(SCHEMA_VERSION))
    purge_http_cache()


def purge_http_cache() -> None:
    """清理 http_cache 过期行（原先只增不删，库文件缓慢膨胀）。"""
    try:
        execute("DELETE FROM http_cache WHERE expires_at<=?", (now_iso(),))
    except Exception:
        pass  # 清理失败不影响启动


def rows(sql: str, params: Iterable = ()) -> List[sqlite3.Row]:
    return list(connect().execute(sql, tuple(params)).fetchall())


def row(sql: str, params: Iterable = ()) -> Optional[sqlite3.Row]:
    return connect().execute(sql, tuple(params)).fetchone()


def execute(sql: str, params: Iterable = ()) -> int:
    with _write_lock:
        conn = connect()
        cur = conn.execute(sql, tuple(params))
        conn.commit()
        return cur.lastrowid


# ---------------------------------------------------------------- kv
def get_kv(key: str, default: Optional[str] = None) -> Optional[str]:
    r = row("SELECT value FROM kv WHERE key=?", (key,))
    return r["value"] if r else default


def set_kv(key: str, value: str) -> None:
    execute(
        "INSERT INTO kv(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


# ---------------------------------------------------------------- library
def list_libraries() -> List[Dict[str, Any]]:
    out = []
    for r in rows("SELECT * FROM library ORDER BY id"):
        d = dict(r)
        st = row(
            "SELECT COUNT(*) c FROM item WHERE library_id=? AND hidden=0", (r["id"],)
        )
        fl = row(
            "SELECT COUNT(*) c, COALESCE(SUM(size),0) s FROM media_file "
            "WHERE library_id=? AND missing=0",
            (r["id"],),
        )
        d["item_count"] = st["c"] if st else 0
        d["file_count"] = fl["c"] if fl else 0
        d["total_bytes"] = fl["s"] if fl else 0
        out.append(d)
    return out


def get_library(lib_id: int) -> Optional[Dict[str, Any]]:
    r = row("SELECT * FROM library WHERE id=?", (lib_id,))
    return dict(r) if r else None


def find_library_by_root(root_key: str) -> Optional[Dict[str, Any]]:
    r = row("SELECT * FROM library WHERE root_key=?", (root_key,))
    return dict(r) if r else None


def add_library(name: str, root_path: str, root_key: str) -> int:
    return execute(
        "INSERT INTO library(name, root_path, root_key, created_at) VALUES(?,?,?,?)",
        (name, root_path, root_key, now_iso()),
    )


def update_library(lib_id: int, name: Optional[str] = None,
                   root_path: Optional[str] = None,
                   root_key: Optional[str] = None) -> None:
    sets, params = [], []
    if name is not None:
        sets.append("name=?")
        params.append(name)
    if root_path is not None:
        sets.append("root_path=?")
        params.append(root_path)
    if root_key is not None:
        sets.append("root_key=?")
        params.append(root_key)
    if not sets:
        return
    params.append(lib_id)
    execute("UPDATE library SET %s WHERE id=?" % ",".join(sets), params)


def delete_library(lib_id: int) -> None:
    execute("DELETE FROM library WHERE id=?", (lib_id,))
    if get_kv("active_library_id") == str(lib_id):
        set_kv("active_library_id", "")


def touch_library_scan(lib_id: int) -> None:
    execute("UPDATE library SET last_scan_at=? WHERE id=?", (now_iso(), lib_id))


def active_library_id() -> Optional[int]:
    v = get_kv("active_library_id", "")
    try:
        lid = int(v) if v else 0
    except (TypeError, ValueError):
        lid = 0
    if lid and get_library(lid):
        return lid
    libs = list_libraries()
    if libs:
        set_kv("active_library_id", str(libs[0]["id"]))
        return libs[0]["id"]
    return None


# ---------------------------------------------------------------- item
def get_item(item_id: int) -> Optional[Dict[str, Any]]:
    r = row("SELECT * FROM item WHERE id=?", (item_id,))
    return dict(r) if r else None


def item_by_group(library_id: int, group_key: str) -> Optional[Dict[str, Any]]:
    r = row(
        "SELECT * FROM item WHERE library_id=? AND group_key=?",
        (library_id, group_key),
    )
    return dict(r) if r else None


def create_item(library_id: int, group_key: str, kind: str, title: str,
                year: Optional[int] = None,
                original_title: Optional[str] = None) -> int:
    ts = now_iso()
    return execute(
        "INSERT INTO item(library_id, group_key, kind, title, original_title, year,"
        " year_hint, status, created_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?,'pending',?,?)",
        (library_id, group_key, kind, title, original_title, year, year, ts, ts),
    )


def update_item(item_id: int, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = now_iso()
    sets = ",".join("%s=?" % k for k in fields)
    execute("UPDATE item SET %s WHERE id=?" % sets,
            list(fields.values()) + [item_id])


def delete_items(item_ids: List[int]) -> None:
    if not item_ids:
        return
    marks = ",".join("?" * len(item_ids))
    execute("DELETE FROM item WHERE id IN (%s)" % marks, item_ids)


def set_kid_flags(item_ids: List[int], *, blocked: Optional[bool] = None,
                  allowed: Optional[bool] = None) -> int:
    """成人模式人工覆盖：屏蔽 / 放行。两者互斥，设置一个会清掉另一个。"""
    if not item_ids or (blocked is None and allowed is None):
        return 0
    sets, params = [], []
    if blocked is not None:
        sets.append("kid_blocked=?")
        params.append(1 if blocked else 0)
        if blocked:
            sets.append("kid_allowed=0")
    if allowed is not None:
        sets.append("kid_allowed=?")
        params.append(1 if allowed else 0)
        if allowed:
            sets.append("kid_blocked=0")
    sets.append("updated_at=?")
    params.append(now_iso())
    marks = ",".join("?" * len(item_ids))
    params.extend(item_ids)
    with _write_lock:
        conn = connect()
        cur = conn.execute("UPDATE item SET %s WHERE id IN (%s)"
                           % (",".join(sets), marks), params)
        conn.commit()
        return cur.rowcount


def kid_meta_pending(library_id: int) -> int:
    """统计已有 TMDB ID 但还没补全分级信息的条目数。"""
    r = row(
        "SELECT COUNT(*) c FROM item WHERE library_id=? AND tmdb_id IS NOT NULL"
        " AND status='ok' AND kid_meta_at IS NULL",
        (library_id,))
    return r["c"] if r else 0


def kid_meta_targets(library_id: int, limit: int = 0) -> List[Dict[str, Any]]:
    sql = (
        "SELECT id, tmdb_id, kind, title FROM item WHERE library_id=?"
        " AND tmdb_id IS NOT NULL AND status='ok' AND kid_meta_at IS NULL"
        " ORDER BY id"
    )
    if limit:
        sql += " LIMIT %d" % int(limit)
    return [dict(r) for r in rows(sql, (library_id,))]


def list_items(library_id: int, include_hidden: bool = False) -> List[Dict[str, Any]]:
    sql = (
        "SELECT i.*, (SELECT COUNT(*) FROM media_file f "
        "  WHERE f.item_id=i.id AND f.missing=0 AND f.role='main') AS file_count, "
        "  (SELECT COUNT(*) FROM media_file f2 "
        "  WHERE f2.item_id=i.id AND f2.missing=0) AS all_file_count, "
        "  (SELECT COUNT(*) FROM media_file f3 "
        "  WHERE f3.item_id=i.id AND f3.missing=0 AND f3.episode IS NOT NULL) AS ep_count, "
        # 最近一个视频文件的修改时间（秒），用于"最近修改"排序
        "  (SELECT MAX(f4.mtime) FROM media_file f4 "
        "  WHERE f4.item_id=i.id AND f4.missing=0) AS last_mtime, "
        # 条目总体积，用于"体积"排序
        "  (SELECT SUM(f5.size) FROM media_file f5 "
        "  WHERE f5.item_id=i.id AND f5.missing=0) AS total_bytes "
        "FROM item i WHERE i.library_id=?"
    )
    params = [library_id]
    if not include_hidden:
        sql += " AND i.hidden=0"
    # 注意：这里不能过滤 missing 宽限期的幽灵条目——_apply_groups 靠本函数
    # 按 group_key 复用已有条目，过滤掉会让重新出现的文件建出重复条目
    sql += " ORDER BY i.id DESC"
    return [dict(r) for r in rows(sql, params)]


# 海报墙只需这些列（排除 overview/overview_en/cast_json 等长文本，
# 全量返回会让每次刷新多传几百 KB）
_BRIEF_COLS = (
    "id", "title", "original_title", "year", "kind", "poster_path",
    "rating", "status", "source", "confidence", "genres", "runtime",
    "locked", "hidden", "franchise_id", "series_group",
    "collection_id", "collection_name", "created_at",
    "adult", "certification", "kid_blocked", "kid_allowed", "kid_meta_at",
)


def list_item_briefs(library_id: int, include_hidden: bool = False) -> List[Dict[str, Any]]:
    """list_items 的轻量版：只取海报墙需要的列 + 各计数子查询。"""
    cols = ", ".join("i." + c for c in _BRIEF_COLS)
    sql = (
        f"SELECT {cols},"
        "  (SELECT COUNT(*) FROM media_file f "
        "  WHERE f.item_id=i.id AND f.missing=0 AND f.role='main') AS file_count, "
        "  (SELECT COUNT(*) FROM media_file f3 "
        "  WHERE f3.item_id=i.id AND f3.missing=0 AND f3.episode IS NOT NULL) AS ep_count, "
        "  (SELECT MAX(f4.mtime) FROM media_file f4 "
        "  WHERE f4.item_id=i.id AND f4.missing=0) AS last_mtime, "
        "  (SELECT SUM(f5.size) FROM media_file f5 "
        "  WHERE f5.item_id=i.id AND f5.missing=0) AS total_bytes "
        "FROM item i WHERE i.library_id=?"
    )
    params = [library_id]
    if not include_hidden:
        sql += " AND i.hidden=0"
    # 文件全部处于 missing 宽限期的幽灵条目不上海报墙
    # （空壳卡无内容可播；宽限期过后 delete_orphan_items 会真正删除）
    sql += (" AND (i.kind='collection' OR EXISTS "
            "(SELECT 1 FROM media_file fb WHERE fb.item_id=i.id AND fb.missing=0))")
    sql += " ORDER BY i.id DESC"
    return [dict(r) for r in rows(sql, params)]


# ---------------------------------------------------------------- franchise
def genre_counts(library_id: int) -> List[Dict[str, Any]]:
    """统计该库各类型的条目数（genres 是 JSON 数组文本）。"""
    tally: Dict[str, int] = {}
    for r in rows("SELECT genres FROM item WHERE library_id=? AND hidden=0"
                  " AND genres IS NOT NULL", (library_id,)):
        try:
            arr = json.loads(r["genres"])
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(arr, list):
            continue
        for g in arr:
            g = str(g or "").strip()
            if g:
                tally[g] = tally.get(g, 0) + 1
    out = [{"name": k, "count": v} for k, v in tally.items()]
    out.sort(key=lambda x: (-x["count"], x["name"]))
    return out


def upsert_franchise(library_id: int, key: str, name: str,
                     source: str = "tmdb", collection_id: Optional[int] = None,
                     poster_path: Optional[str] = None,
                     backdrop_path: Optional[str] = None,
                     overview: Optional[str] = None,
                     sort_year: Optional[int] = None) -> int:
    """按 (library_id, key) 幂等写入系列，返回 id。"""
    ts = now_iso()
    with _write_lock:
        conn = connect()
        conn.execute(
            "INSERT INTO franchise(library_id, key, name, source, collection_id,"
            " poster_path, backdrop_path, overview, sort_year, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(library_id, key) DO UPDATE SET"
            "  name=excluded.name, source=excluded.source,"
            "  collection_id=COALESCE(excluded.collection_id, franchise.collection_id),"
            "  poster_path=COALESCE(excluded.poster_path, franchise.poster_path),"
            "  backdrop_path=COALESCE(excluded.backdrop_path, franchise.backdrop_path),"
            "  overview=COALESCE(excluded.overview, franchise.overview),"
            "  sort_year=COALESCE(excluded.sort_year, franchise.sort_year),"
            "  updated_at=excluded.updated_at",
            (library_id, key, name, source, collection_id, poster_path,
             backdrop_path, overview, sort_year, ts, ts),
        )
        conn.commit()
        r = conn.execute("SELECT id FROM franchise WHERE library_id=? AND key=?",
                         (library_id, key)).fetchone()
        return int(r["id"])


def set_item_franchise(item_id: int, franchise_id: Optional[int]) -> None:
    execute("UPDATE item SET franchise_id=?, updated_at=? WHERE id=?",
            (franchise_id, now_iso(), item_id))


def clear_item_franchises(library_id: int) -> None:
    execute("UPDATE item SET franchise_id=NULL WHERE library_id=?", (library_id,))
    execute("UPDATE item SET series_group=NULL WHERE library_id=?", (library_id,))


def set_item_series_group(item_id: int, group: Optional[str]) -> None:
    execute("UPDATE item SET series_group=?, updated_at=? WHERE id=?",
            (group or None, now_iso(), item_id))


def clear_item_series_groups(library_id: int) -> None:
    execute("UPDATE item SET series_group=NULL WHERE library_id=?", (library_id,))


def suspicious_items(library_id: int, gap: int = 1) -> List[Dict[str, Any]]:
    """库里年份与文件名年份对不上的条目 —— 大概率是刮错了。"""
    sql = (
        "SELECT id, title, year, year_hint, status, source, confidence, tmdb_id"
        " FROM item WHERE library_id=? AND hidden=0 AND locked=0"
        " AND year IS NOT NULL AND year_hint IS NOT NULL"
        " AND ABS(year - year_hint) > ? ORDER BY ABS(year - year_hint) DESC"
    )
    return [dict(r) for r in rows(sql, (library_id, gap))]


def requeue_items(item_ids: List[int]) -> int:
    """把指定条目重置为待刮削，并让它们的刮削任务重新排队。"""
    if not item_ids:
        return 0
    marks = ",".join("?" * len(item_ids))
    with _write_lock:
        conn = connect()
        cur = conn.execute(
            "UPDATE item SET status='pending' WHERE id IN (%s)" % marks, item_ids)
        conn.execute(
            "UPDATE scrape_task SET state='queued', attempts=0, next_retry_at=NULL"
            " WHERE item_id IN (%s)" % marks, item_ids)
        conn.commit()
        return cur.rowcount


def delete_empty_franchises(library_id: int) -> int:
    with _write_lock:
        conn = connect()
        cur = conn.execute(
            "DELETE FROM franchise WHERE library_id=? AND id NOT IN "
            "(SELECT franchise_id FROM item WHERE library_id=?"
            " AND franchise_id IS NOT NULL)", (library_id, library_id))
        conn.commit()
        return cur.rowcount


def list_franchises(library_id: int) -> List[Dict[str, Any]]:
    sql = (
        "SELECT f.*, "
        "  (SELECT COUNT(*) FROM item i WHERE i.franchise_id=f.id AND i.hidden=0"
        "     AND EXISTS (SELECT 1 FROM media_file fm"
        "                 WHERE fm.item_id=i.id AND fm.missing=0)) AS item_count,"
        "  (SELECT COUNT(*) FROM item i WHERE i.franchise_id=f.id AND i.hidden=0"
        "     AND i.poster_path IS NOT NULL) AS poster_count,"
        "  (SELECT MIN(i.year) FROM item i WHERE i.franchise_id=f.id) AS min_year,"
        "  (SELECT MAX(i.year) FROM item i WHERE i.franchise_id=f.id) AS max_year"
        " FROM franchise f WHERE f.library_id=?"
        " ORDER BY item_count DESC, f.name"
    )
    return [dict(r) for r in rows(sql, (library_id,))]


def get_franchise(fid: int) -> Optional[Dict[str, Any]]:
    sql = (
        "SELECT f.*, "
        "  (SELECT COUNT(*) FROM item i WHERE i.franchise_id=f.id AND i.hidden=0"
        "     AND EXISTS (SELECT 1 FROM media_file fm"
        "                 WHERE fm.item_id=i.id AND fm.missing=0)) AS item_count,"
        "  (SELECT COUNT(*) FROM item i WHERE i.franchise_id=f.id AND i.hidden=0"
        "     AND i.poster_path IS NOT NULL) AS poster_count,"
        "  (SELECT MIN(i.year) FROM item i WHERE i.franchise_id=f.id) AS min_year,"
        "  (SELECT MAX(i.year) FROM item i WHERE i.franchise_id=f.id) AS max_year"
        " FROM franchise f WHERE f.id=?"
    )
    r = row(sql, (fid,))
    return dict(r) if r else None


def franchise_members(fid: int) -> List[Dict[str, Any]]:
    sql = (
        "SELECT i.*, (SELECT COUNT(*) FROM media_file f "
        "  WHERE f.item_id=i.id AND f.missing=0 AND f.role='main') AS file_count, "
        "  (SELECT COUNT(*) FROM media_file f3 "
        "  WHERE f3.item_id=i.id AND f3.missing=0 AND f3.episode IS NOT NULL) AS ep_count "
        "FROM item i WHERE i.franchise_id=? AND i.hidden=0"
        # 文件全部 missing（宽限期幽灵）的成员不进系列展开视图
        " AND EXISTS (SELECT 1 FROM media_file fm"
        "             WHERE fm.item_id=i.id AND fm.missing=0)"
        " ORDER BY i.year IS NULL, i.year, i.title"
    )
    return [dict(r) for r in rows(sql, (fid,))]


# ---------------------------------------------------------------- media_file
def upsert_file(library_id: int, rel_path: str, rel_path_key: str, size: int,
                mtime: float, ext: str, item_id: Optional[int],
                season: Optional[int], episode: Optional[int],
                ep_title: Optional[str], role: str, ord_: int,
                run_id: int) -> int:
    ts = now_iso()
    with _write_lock:
        conn = connect()
        cur = conn.execute(
            "INSERT INTO media_file(library_id, item_id, rel_path, rel_path_key, size,"
            " mtime, ext, season, episode, ep_title, role, ord, missing, miss_count,"
            " last_seen_run, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0,0,?,?,?)"
            " ON CONFLICT(library_id, rel_path_key) DO UPDATE SET"
            "  item_id=excluded.item_id, rel_path=excluded.rel_path,"
            "  size=excluded.size, mtime=excluded.mtime,"
            "  season=excluded.season, episode=excluded.episode,"
            "  ep_title=excluded.ep_title, role=excluded.role, ord=excluded.ord,"
            "  missing=0, miss_count=0, last_seen_run=excluded.last_seen_run,"
            "  updated_at=excluded.updated_at",
            (library_id, item_id, rel_path, rel_path_key, size, mtime, ext,
             season, episode, ep_title, role, ord_, run_id, ts, ts),
        )
        conn.commit()
        return cur.lastrowid


def upsert_files_batch(library_id: int, files: List[Dict[str, Any]],
                       run_id: int) -> None:
    """整轮扫描的文件入库合并为单个事务（原先每文件一次 commit，
    全量同步 500+ 文件就是 500+ 次事务）。

    单行失败（如同步期间条目被用户移除导致外键悬挂）自动回退为
    逐行执行并跳过坏行，不影响其余文件。
    """
    if not files:
        return
    ts = now_iso()
    params = [
        (library_id, f["item_id"], f["rel_path"], f["rel_path_key"],
         f["size"], f["mtime"], f["ext"], f["season"], f["episode"],
         f["ep_title"], f["role"], f["ord"], run_id, ts, ts)
        for f in files
    ]
    with _write_lock:
        conn = connect()
        try:
            conn.executemany(
                "INSERT INTO media_file(library_id, item_id, rel_path,"
                " rel_path_key, size, mtime, ext, season, episode, ep_title,"
                " role, ord, missing, miss_count, last_seen_run, created_at,"
                " updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0,0,?,?,?)"
                " ON CONFLICT(library_id, rel_path_key) DO UPDATE SET"
                "  item_id=excluded.item_id, rel_path=excluded.rel_path,"
                "  size=excluded.size, mtime=excluded.mtime,"
                "  season=excluded.season, episode=excluded.episode,"
                "  ep_title=excluded.ep_title, role=excluded.role,"
                "  ord=excluded.ord, missing=0, miss_count=0,"
                "  last_seen_run=excluded.last_seen_run,"
                "  updated_at=excluded.updated_at",
                params,
            )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            sql = (
                "INSERT INTO media_file(library_id, item_id, rel_path,"
                " rel_path_key, size, mtime, ext, season, episode, ep_title,"
                " role, ord, missing, miss_count, last_seen_run, created_at,"
                " updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0,0,?,?,?)"
                " ON CONFLICT(library_id, rel_path_key) DO UPDATE SET"
                "  item_id=excluded.item_id, rel_path=excluded.rel_path,"
                "  size=excluded.size, mtime=excluded.mtime,"
                "  season=excluded.season, episode=excluded.episode,"
                "  ep_title=excluded.ep_title, role=excluded.role,"
                "  ord=excluded.ord, missing=0, miss_count=0,"
                "  last_seen_run=excluded.last_seen_run,"
                "  updated_at=excluded.updated_at"
            )
            for p in params:
                try:
                    conn.execute(sql, p)
                except sqlite3.IntegrityError:
                    continue  # 条目已被删除，文件由 ignored 列表兜底
            conn.commit()


def files_for_item(item_id: int) -> List[Dict[str, Any]]:
    return [dict(r) for r in rows(
        "SELECT * FROM media_file WHERE item_id=? AND missing=0 "
        "ORDER BY season, episode, ord, rel_path", (item_id,))]


def file_by_id(file_id: int) -> Optional[Dict[str, Any]]:
    r = row("SELECT * FROM media_file WHERE id=?", (file_id,))
    return dict(r) if r else None


def mark_missing(library_id: int, run_id: int) -> int:
    """把本轮没见到的文件标记 missing，返回新增标记数量。

    注意：这里不能加 `AND missing=0`。否则已标记的行 miss_count 永远停在 1，
    purge_missing 的「连续两轮确认」就形同虚设。已 missing 的行要允许继续累加。
    """
    with _write_lock:
        conn = connect()
        cur = conn.execute(
            "UPDATE media_file SET missing=1, miss_count=miss_count+1, updated_at=?"
            " WHERE library_id=? AND last_seen_run<>?",
            (now_iso(), library_id, run_id),
        )
        conn.commit()
        return cur.rowcount


def purge_missing(library_id: int, threshold: int = 2) -> int:
    with _write_lock:
        conn = connect()
        cur = conn.execute(
            "DELETE FROM media_file WHERE library_id=? AND missing=1 AND miss_count>=?",
            (library_id, threshold),
        )
        conn.commit()
        return cur.rowcount


def delete_orphan_items(library_id: int) -> int:
    """删除已经没有存活文件的条目。

    安全阀：不能只看「有没有 missing=0 的文件」——media_file.item_id 是
    ON DELETE CASCADE，条目一删文件行也跟着没了，等于跳过两轮确认。
    共享盘瞬时抖动（一次扫描少返回若干文件）就会把条目和已刮削的元数据
    一次性清空。所以还要排除「仍在宽限期内」的条目：
    只要它还有 miss_count<2 的缺失文件，就再等一轮。
    """
    with _write_lock:
        conn = connect()
        cur = conn.execute(
            "DELETE FROM item WHERE library_id=? AND id NOT IN "
            "(SELECT DISTINCT item_id FROM media_file "
            " WHERE library_id=? AND item_id IS NOT NULL AND missing=0)"
            " AND id NOT IN "
            "(SELECT DISTINCT item_id FROM media_file "
            " WHERE library_id=? AND item_id IS NOT NULL"
            "   AND missing=1 AND miss_count<2)",
            (library_id, library_id, library_id),
        )
        conn.commit()
        return cur.rowcount


# ---------------------------------------------------------------- ignored
def add_ignored(library_id: int, entries: List[Dict[str, str]]) -> None:
    if not entries:
        return
    ts = now_iso()
    with _write_lock:
        conn = connect()
        conn.executemany(
            "INSERT INTO ignored_path(library_id, rel_path_key, rel_path, title,"
            " removed_at) VALUES(?,?,?,?,?)"
            " ON CONFLICT(library_id, rel_path_key) DO UPDATE SET"
            "  rel_path=excluded.rel_path, title=excluded.title,"
            "  removed_at=excluded.removed_at",
            [(library_id, e["rel_path_key"], e["rel_path"], e.get("title"), ts)
             for e in entries],
        )
        conn.commit()


def list_ignored(library_id: int) -> List[Dict[str, Any]]:
    return [dict(r) for r in rows(
        "SELECT * FROM ignored_path WHERE library_id=? ORDER BY removed_at DESC",
        (library_id,))]


def ignored_keys(library_id: int) -> set:
    return {r["rel_path_key"] for r in rows(
        "SELECT rel_path_key FROM ignored_path WHERE library_id=?", (library_id,))}


def restore_ignored(library_id: int, keys: List[str]) -> int:
    if not keys:
        return 0
    marks = ",".join("?" * len(keys))
    with _write_lock:
        conn = connect()
        cur = conn.execute(
            "DELETE FROM ignored_path WHERE library_id=? AND rel_path_key IN (%s)"
            % marks, [library_id] + keys)
        conn.commit()
        return cur.rowcount


# ---------------------------------------------------------------- sync_run
def start_run(library_id: Optional[int]) -> int:
    purge_http_cache()
    with _write_lock:
        conn = connect()
        conn.execute(
            "UPDATE sync_run SET status='failed', finished_at=?,"
            " message=COALESCE(message,'僵尸锁自动释放')"
            " WHERE status='running' AND started_at < datetime('now','localtime','-2 hours')",
            (now_iso(),),
        )
        cur = conn.execute(
            "INSERT INTO sync_run(library_id, started_at, status) VALUES(?,?,'running')",
            (library_id, now_iso()),
        )
        conn.commit()
        return cur.lastrowid


def running_run() -> Optional[Dict[str, Any]]:
    r = row("SELECT * FROM sync_run WHERE status='running' ORDER BY id DESC LIMIT 1")
    return dict(r) if r else None


def finish_run(run_id: int, **fields) -> None:
    fields["finished_at"] = now_iso()
    sets = ",".join("%s=?" % k for k in fields)
    execute("UPDATE sync_run SET %s WHERE id=?" % sets,
            list(fields.values()) + [run_id])


def last_run() -> Optional[Dict[str, Any]]:
    r = row("SELECT * FROM sync_run ORDER BY id DESC LIMIT 1")
    return dict(r) if r else None


# ---------------------------------------------------------------- scrape_task
def enqueue_scrape(item_id: int, query: str = "",
                   state: str = "queued") -> None:
    """state 默认 'queued'（等待 worker 认领）；
    手动重刮路径传 'running' 立即占住任务，避免同步 worker 抢占造成并发刮削。"""
    execute(
        "INSERT INTO scrape_task(item_id, query, state, attempts, updated_at)"
        " VALUES(?,?,?,?,0)"
        " ON CONFLICT(item_id) DO UPDATE SET state=excluded.state, attempts=0,"
        "  last_error=NULL, next_retry_at=NULL, query=excluded.query,"
        "  updated_at=excluded.updated_at",
        (item_id, query, state, now_iso()),
    )


def next_task() -> Optional[Dict[str, Any]]:
    r = row(
        "SELECT t.*, i.library_id, i.kind, i.title, i.year, i.locked, i.status"
        " FROM scrape_task t JOIN item i ON i.id=t.item_id"
        " WHERE t.state IN ('queued','failed') AND t.attempts<5"
        "   AND (t.next_retry_at IS NULL OR t.next_retry_at<=?)"
        "   AND i.locked=0 AND i.status IN ('pending','failed','unidentified')"
        " ORDER BY (t.state='queued') DESC, t.item_id LIMIT 1",
        (now_iso(),),
    )
    return dict(r) if r else None


def claim_task() -> Optional[Dict[str, Any]]:
    """原子领取一个待刮削任务（多线程安全）。"""
    with _write_lock:
        r = row(
            "SELECT t.*, i.library_id, i.kind, i.title, i.year, i.locked, i.status"
            " FROM scrape_task t JOIN item i ON i.id=t.item_id"
            " WHERE t.state IN ('queued','failed') AND t.attempts<5"
            "   AND (t.next_retry_at IS NULL OR t.next_retry_at<=?)"
            "   AND i.locked=0 AND i.status IN ('pending','failed','unidentified')"
            " ORDER BY (t.state='queued') DESC, t.item_id LIMIT 1",
            (now_iso(),))
        if not r:
            return None
        conn = connect()
        cur = conn.execute(
            "UPDATE scrape_task SET state='running', updated_at=?"
            " WHERE item_id=? AND state IN ('queued','failed')",
            (now_iso(), r["item_id"]))
        conn.commit()
        if cur.rowcount == 0:
            return None
        d = dict(r)
        d["state"] = "running"
        return d


def set_task_state(item_id: int, state: str, error: Optional[str] = None,
                   retry_at: Optional[str] = None, bump: bool = False) -> None:
    execute(
        "UPDATE scrape_task SET state=?, last_error=?, next_retry_at=?,"
        " attempts=attempts+?, updated_at=? WHERE item_id=?",
        (state, error, retry_at, 1 if bump else 0, now_iso(), item_id),
    )


def pending_count(library_id: Optional[int] = None) -> int:
    if library_id:
        r = row(
            "SELECT COUNT(*) c FROM scrape_task t JOIN item i ON i.id=t.item_id"
            " WHERE i.library_id=? AND t.state IN ('queued','running','failed')",
            (library_id,))
    else:
        r = row("SELECT COUNT(*) c FROM scrape_task"
                " WHERE state IN ('queued','running','failed')")
    return r["c"] if r else 0


def reset_failed(library_id: Optional[int] = None,
                 include_unidentified: bool = False) -> int:
    with _write_lock:
        conn = connect()
        states = "('failed')" if not include_unidentified else "('failed','skipped')"
        if library_id:
            if include_unidentified:
                conn.execute(
                    "UPDATE item SET status='pending' WHERE library_id=?"
                    " AND status='unidentified' AND locked=0", (library_id,))
            cur = conn.execute(
                "UPDATE scrape_task SET state='queued', attempts=0, next_retry_at=NULL"
                " WHERE state IN %s AND item_id IN"
                " (SELECT id FROM item WHERE library_id=?)" % states, (library_id,))
        else:
            cur = conn.execute(
                "UPDATE scrape_task SET state='queued', attempts=0, next_retry_at=NULL"
                " WHERE state IN %s" % states)
        conn.commit()
        return cur.rowcount


def enqueue_all_pending(library_id: int, force: bool = False) -> int:
    """把该库所有未完成（或强制全部）条目重新入队。"""
    with _write_lock:
        conn = connect()
        if force:
            conn.execute(
                "UPDATE item SET status='pending', updated_at=?"
                " WHERE library_id=? AND locked=0", (now_iso(), library_id))
        # 注意：不含 unidentified —— 未识别条目不应在每次增量时反复重试，
        # 需要重试时由用户显式点「重试未识别」触发
        targets = conn.execute(
            "SELECT id FROM item WHERE library_id=? AND locked=0"
            " AND status IN ('pending','failed')", (library_id,)
        ).fetchall()
        ts = now_iso()
        conn.executemany(
            "INSERT INTO scrape_task(item_id, state, attempts, updated_at)"
            " VALUES(?,'queued',0,?) ON CONFLICT(item_id) DO UPDATE SET"
            " state='queued', attempts=0, next_retry_at=NULL, updated_at=excluded.updated_at",
            [(t["id"], ts) for t in targets])
        conn.commit()
        return len(targets)


# ---------------------------------------------------------------- season/episode
def replace_seasons(item_id: int, seasons: List[Dict[str, Any]],
                    episodes: List[Dict[str, Any]]) -> None:
    with _write_lock:
        conn = connect()
        conn.execute("DELETE FROM season WHERE item_id=?", (item_id,))
        conn.execute("DELETE FROM episode WHERE item_id=?", (item_id,))
        for s in seasons:
            conn.execute(
                "INSERT INTO season(item_id, season_number, name, overview,"
                " poster_path, episode_count) VALUES(?,?,?,?,?,?)",
                (item_id, s.get("season_number"), s.get("name"),
                 s.get("overview"), s.get("poster_path"), s.get("episode_count")))
        for e in episodes:
            conn.execute(
                "INSERT INTO episode(item_id, season_number, episode_number, name,"
                " overview, still_path, air_date, runtime) VALUES(?,?,?,?,?,?,?,?)",
                (item_id, e.get("season_number"), e.get("episode_number"),
                 e.get("name"), e.get("overview"), e.get("still_path"),
                 e.get("air_date"), e.get("runtime")))
        conn.commit()


def seasons_for_item(item_id: int) -> List[Dict[str, Any]]:
    return [dict(r) for r in rows(
        "SELECT * FROM season WHERE item_id=? ORDER BY season_number", (item_id,))]


def episodes_for_item(item_id: int) -> List[Dict[str, Any]]:
    return [dict(r) for r in rows(
        "SELECT * FROM episode WHERE item_id=? ORDER BY season_number, episode_number",
        (item_id,))]


# ---------------------------------------------------------------- http_cache
def cache_get(key: str) -> Optional[str]:
    r = row("SELECT body FROM http_cache WHERE cache_key=? AND expires_at>?",
            (key, now_iso()))
    return r["body"] if r else None


def cache_put(key: str, body: str, status: int, ttl_seconds: int) -> None:
    from datetime import timedelta

    exp = (datetime.now() + timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds")
    execute(
        "INSERT INTO http_cache(cache_key, body, status, created_at, expires_at)"
        " VALUES(?,?,?,?,?) ON CONFLICT(cache_key) DO UPDATE SET"
        " body=excluded.body, status=excluded.status, expires_at=excluded.expires_at",
        (key, body, status, now_iso(), exp),
    )


def stats(library_id: Optional[int]) -> Dict[str, Any]:
    if not library_id:
        return {"items": 0, "movies": 0, "series": 0, "files": 0,
                "size": 0, "unidentified": 0, "pending": 0, "franchises": 0}
    q = lambda sql: row(sql, (library_id,))  # noqa: E731
    return {
        "items": q("SELECT COUNT(*) c FROM item WHERE library_id=? AND hidden=0")["c"],
        "movies": q("SELECT COUNT(*) c FROM item WHERE library_id=? AND kind='movie'")["c"],
        "series": q("SELECT COUNT(*) c FROM item WHERE library_id=? AND kind='series'")["c"],
        "files": q("SELECT COUNT(*) c FROM media_file WHERE library_id=? AND missing=0")["c"],
        "size": q("SELECT COALESCE(SUM(size),0) c FROM media_file"
                  " WHERE library_id=? AND missing=0")["c"],
        "unidentified": q("SELECT COUNT(*) c FROM item WHERE library_id=?"
                          " AND status='unidentified'")["c"],
        "pending": pending_count(library_id),
        "franchises": q("SELECT COUNT(*) c FROM franchise WHERE library_id=?")["c"],
    }


def dump_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)
