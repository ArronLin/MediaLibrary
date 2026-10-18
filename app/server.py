"""HTTP 服务：路由、静态文件、SSE。

零第三方依赖：http.server.ThreadingHTTPServer + 手写路由表。
"""
from __future__ import annotations

import json
import os
import queue
import re
import socket
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import config, db, franchise, images, paths, player, sync

ROUTES = []  # type: List[Tuple[str, re.Pattern, Callable]]

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
}


def route(method: str, pattern: str):
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), fn))
        return fn
    return deco


def _brief(it: Dict[str, Any]) -> Dict[str, Any]:
    poster = it.get("poster_path")
    return {
        "id": it["id"],
        "title": it.get("title") or "",
        "original_title": it.get("original_title") or "",
        "year": it.get("year"),
        "kind": it["kind"],
        "poster": ("/img/" + poster) if poster else
                  "/api/placeholder?title=" + urllib.parse.quote(it.get("title") or "?"),
        "rating": it.get("rating"),
        "status": it.get("status"),
        "source": it.get("source"),
        "confidence": it.get("confidence"),
        "genres": json.loads(it["genres"]) if it.get("genres") else [],
        "file_count": it.get("file_count") or 0,
        "ep_count": it.get("ep_count") or 0,
        "runtime": it.get("runtime"),
        "bytes": it.get("total_bytes") or 0,
        "mtime": it.get("last_mtime") or 0,
        "locked": bool(it.get("locked")),
        "hidden": bool(it.get("hidden")),
        "franchise_id": it.get("franchise_id"),
        "series_group": it.get("series_group") or "",
        "collection_id": it.get("collection_id"),
        "collection_name": it.get("collection_name") or "",
        "added_at": it.get("added_at") or it.get("created_at") or "",
    }


def _backdrop_src(it: Dict[str, Any]) -> str:
    p = it.get("backdrop_path")
    if not p:
        return ""
    if str(p).startswith("http"):
        return "/api/backdrop/%d" % it["id"]
    return "/img/" + str(p)


def _active_lib() -> Optional[Dict[str, Any]]:
    lid = db.active_library_id()
    return db.get_library(lid) if lid else None


class Handler(BaseHTTPRequestHandler):
    server_version = "MediaLibrary/1.0"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------ io
    def log_message(self, fmt, *args):  # 静音默认日志
        pass

    def _send(self, body: bytes, ctype: str, code: int = 200,
              cache: str = "no-store") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def json(self, obj: Any, code: int = 200) -> None:
        self._send(json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", code)

    def err(self, msg: str, code: int = 400, error: str = "bad_request") -> None:
        self.json({"ok": False, "error": error, "message": msg}, code)

    def body(self) -> Dict[str, Any]:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return {}
            raw = self.rfile.read(n)
            return json.loads(raw.decode("utf-8", "replace")) if raw else {}
        except (ValueError, json.JSONDecodeError):
            return {}

    @property
    def client_ip(self) -> str:
        return self.client_address[0] if self.client_address else ""

    # ------------------------------------------------------------ verbs
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        q = urllib.parse.parse_qs(parsed.query)
        if path.startswith("/img/"):
            return self._serve_img(path)
        if method == "GET" and not path.startswith("/api/"):
            return self._serve_static(path)
        for m, rx, fn in ROUTES:
            if m != method:
                continue
            hit = rx.match(path)
            if hit:
                try:
                    return fn(self, hit, q)
                except Exception as exc:  # 兜底，避免线程崩溃无响应
                    import traceback
                    traceback.print_exc()
                    return self.err("服务端异常: %s" % exc, 500, "internal")
        return self.err("未找到: %s" % path, 404, "not_found")

    # ------------------------------------------------------------ static
    def _serve_static(self, path: str) -> None:
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (paths.WEB_DIR / rel).resolve()
        try:
            target.relative_to(paths.WEB_DIR.resolve())
        except ValueError:
            return self.err("非法路径", 403)
        if not target.is_file():
            return self.err("未找到", 404, "not_found")
        data = target.read_bytes()
        self._send(data, MIME.get(target.suffix.lower(), "application/octet-stream"),
                   cache="no-cache")

    def _serve_img(self, path: str) -> None:
        parts = path.split("/")
        if len(parts) != 4 or parts[2] not in ("posters", "backdrops"):
            return self.err("非法图片路径", 404)
        name = os.path.basename(parts[3])
        base = paths.POSTER_DIR if parts[2] == "posters" else paths.BACKDROP_DIR
        target = base / name
        if not target.is_file():
            return self.err("图片不存在", 404)
        self._send(target.read_bytes(),
                   MIME.get(target.suffix.lower(), "image/jpeg"),
                   cache="public, max-age=604800, immutable")


# ==================================================================== 基础
@route("GET", r"/api/health")
def health(h: Handler, m, q):
    cfg = config.load()
    h.json({
        "ok": True,
        "python": sys.version.split()[0],
        "db": str(paths.DB_PATH.name),
        "tmdb_key_set": bool((cfg.get("tmdb") or {}).get("api_key")),
        "players": [os.path.basename(p) for p in player.detect(cfg)],
        "sync": sync.status(),
    })


@route("GET", r"/api/stats")
def stats(h: Handler, m, q):
    lib = _active_lib()
    h.json({"ok": True, "stats": db.stats(lib["id"] if lib else None),
            "library": lib})


@route("GET", r"/api/genres")
def genres(h: Handler, m, q):
    lib = _active_lib()
    if not lib:
        return h.json({"ok": True, "genres": []})
    h.json({"ok": True, "genres": db.genre_counts(lib["id"])})


# ==================================================================== 系列
def _franchise_brief(f: Dict[str, Any]) -> Dict[str, Any]:
    poster = f.get("poster_path")
    backdrop = f.get("backdrop_path")
    return {
        "id": f["id"],
        "name": f.get("name") or "未命名系列",
        "source": f.get("source"),
        "collection_id": f.get("collection_id"),
        "poster": ("/img/" + poster) if poster else
                  "/api/placeholder?title=" + urllib.parse.quote(f.get("name") or "?"),
        "backdrop": ("/img/" + backdrop) if backdrop else "",
        "overview": f.get("overview") or "",
        "item_count": f.get("item_count") or 0,
        "min_year": f.get("min_year"),
        "max_year": f.get("max_year"),
    }


def _super_child_order() -> Dict[str, List[str]]:
    """大系列名 -> config 里子系列的顺序（前端分节时按这个顺序排）。"""
    out: Dict[str, List[str]] = {}
    for sp in franchise.super_series_config():
        out[str(sp.get("name"))] = [
            str(c.get("name") or "").strip() or "未命名子系列"
            for c in (sp.get("children") or [])]
    return out


@route("GET", r"/api/franchises")
def franchises(h: Handler, m, q):
    lib = _active_lib()
    if not lib:
        return h.json({"ok": True, "franchises": []})
    rows = db.list_franchises(lib["id"])
    h.json({"ok": True, "franchises": [_franchise_brief(f) for f in rows]})


@route("GET", r"/api/franchises/(\d+)")
def franchise_detail(h: Handler, m, q):
    fid = int(m.group(1))
    f = db.get_franchise(fid)
    if not f:
        return h.err("系列不存在", 404)
    members = [_brief(dict(r)) for r in db.franchise_members(fid)]
    out = _franchise_brief(f)
    out["members"] = members
    # 大系列（source=super）：把成员按子系列分节，前端分块渲染
    if str(f.get("source") or "") == "super":
        seen: Dict[str, List[Dict[str, Any]]] = {}
        for m in members:
            g = m.get("series_group") or "其他"
            seen.setdefault(g, []).append(m)
        wanted = _super_child_order().get(str(f.get("name") or ""), [])
        names = [n for n in wanted if n in seen]
        names += [n for n in seen if n not in names]
        out["groups"] = [{"name": n, "members": seen[n]} for n in names]
        out["child_count"] = len(out["groups"])
    h.json({"ok": True, "franchise": out})


@route("POST", r"/api/franchises/rebuild")
def franchise_rebuild(h: Handler, m, q):
    lib = _active_lib()
    if not lib:
        return h.err("请先添加媒体库")
    res = franchise.rebuild(lib["id"], sync._franchise_client())
    h.json({"ok": True, **res})


# ==================================================================== 媒体库
@route("GET", r"/api/libraries")
def libraries(h: Handler, m, q):
    libs = db.list_libraries()
    h.json({"ok": True, "libraries": libs, "active_id": db.active_library_id()})


@route("POST", r"/api/libraries")
def add_library(h: Handler, m, q):
    b = h.body()
    path = (b.get("path") or "").strip().strip('"')
    if not path:
        return h.err("请填写媒体库目录")
    path = os.path.normpath(path)
    if not os.path.isdir(path):
        return h.err("目录不存在或不可访问：%s" % path)
    key = paths.normalize_root_key(path)
    exist = db.find_library_by_root(key)
    if exist:
        return h.json({"ok": True, "id": exist["id"], "existed": True})
    name = (b.get("name") or "").strip() or os.path.basename(path.rstrip("\\/"))
    lid = db.add_library(name, path, key)
    if not db.get_kv("active_library_id"):
        db.set_kv("active_library_id", str(lid))
    h.json({"ok": True, "id": lid})


@route("PATCH", r"/api/libraries/(\d+)")
def edit_library(h: Handler, m, q):
    lid = int(m.group(1))
    lib = db.get_library(lid)
    if not lib:
        return h.err("媒体库不存在", 404)
    b = h.body()
    name = b.get("name")
    path = (b.get("path") or "").strip().strip('"') or None
    if path:
        path = os.path.normpath(path)
        if not os.path.isdir(path):
            return h.err("目录不存在或不可访问")
        db.update_library(lid, name=name, root_path=path,
                          root_key=paths.normalize_root_key(path))
    elif name:
        db.update_library(lid, name=name)
    h.json({"ok": True})


@route("POST", r"/api/libraries/(\d+)/activate")
def activate_library(h: Handler, m, q):
    lid = int(m.group(1))
    if not db.get_library(lid):
        return h.err("媒体库不存在", 404)
    db.set_kv("active_library_id", str(lid))
    h.json({"ok": True, "stats": db.stats(lid)})


@route("DELETE", r"/api/libraries/(\d+)")
def remove_library(h: Handler, m, q):
    lid = int(m.group(1))
    db.delete_library(lid)
    h.json({"ok": True})


@route("POST", r"/api/folders/pick")
def pick_folder(h: Handler, m, q):
    """在本机弹出目录选择框（仅限本机请求）。"""
    if not player.is_local(h.client_ip):
        return h.err("目录选择只能在运行服务的主机上操作", 403, "remote_forbidden")
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        chosen = filedialog.askdirectory(title="选择媒体库目录")
        root.destroy()
        return h.json({"ok": True, "path": chosen or ""})
    except Exception as exc:
        return h.err("无法弹出目录选择框：%s" % exc, 500, "picker_failed")


# ==================================================================== 条目
@route("GET", r"/api/items")
def list_items(h: Handler, m, q):
    lib = _active_lib()
    if not lib:
        return h.json({"ok": True, "items": [], "total": 0, "library": None})
    rows = db.list_item_briefs(lib["id"], include_hidden=False)
    items = [_brief(r) for r in rows]
    kind = (q.get("kind") or [""])[0]
    if kind in ("movie", "series", "collection"):
        items = [i for i in items if i["kind"] == kind]
    kw = (q.get("q") or [""])[0].strip().lower()
    if kw:
        items = [i for i in items if kw in i["title"].lower()
                 or kw in i["original_title"].lower()]
    sort = (q.get("sort") or ["added"])[0]
    if sort == "rating":
        items.sort(key=lambda i: (-(i["rating"] or 0), i["title"]))
    elif sort == "title":
        items.sort(key=lambda i: i["title"])
    elif sort == "year":
        items.sort(key=lambda i: -(i["year"] or 0))
    h.json({"ok": True, "items": items, "total": len(items), "library": lib})


@route("GET", r"/api/items/(\d+)")
def item_detail(h: Handler, m, q):
    iid = int(m.group(1))
    it = db.get_item(iid)
    if not it:
        return h.err("条目不存在", 404)
    lib = db.get_library(it["library_id"]) or {}
    files = db.files_for_item(iid)
    detail = _brief(it)
    detail.update({
        "overview": it.get("overview") or "",
        "overview_en": it.get("overview_en") or "",
        "backdrop": _backdrop_src(it),
        "cast": json.loads(it["cast_json"]) if it.get("cast_json") else [],
        "director": it.get("director"),
        "tmdb_id": it.get("tmdb_id"),
        "imdb_id": it.get("imdb_id"),
        "poster_src": it.get("poster_src"),
        "root_path": lib.get("root_path"),
        "files": [{
            "id": f["id"], "rel_path": f["rel_path"], "size": f["size"],
            "season": f["season"], "episode": f["episode"],
            "ep_title": f["ep_title"], "role": f["role"],
            "ext": f["ext"],
        } for f in files],
        "seasons": db.seasons_for_item(iid),
        "episodes": db.episodes_for_item(iid),
    })
    h.json({"ok": True, "item": detail})


@route("POST", r"/api/items/(\d+)/play")
def play_item(h: Handler, m, q):
    iid = int(m.group(1))
    it = db.get_item(iid)
    if not it:
        return h.err("条目不存在", 404)
    b = h.body()
    file_id = b.get("file_id")
    files = db.files_for_item(iid)
    target = None
    if file_id:
        target = next((f for f in files if f["id"] == int(file_id)), None)
    if not target:
        mains = [f for f in files if f["role"] == "main"] or files
        target = mains[0] if mains else None
    if not target:
        return h.err("该条目没有可用视频文件", 404, "no_file")

    lib = db.get_library(it["library_id"]) or {}
    abs_path = paths.join_rel(lib.get("root_path") or "", target["rel_path"])

    cfg = config.load()
    allow_remote = bool((cfg.get("player") or {}).get("allow_remote_play"))
    if not player.is_local(h.client_ip) and not allow_remote:
        return h.json({
            "ok": False, "reason": "remote_play_disabled",
            "message": "播放只能在运行服务的主机上执行",
            "path": abs_path, "title": it.get("title"),
        }, 403)

    res = player.play(abs_path, cfg)
    if res.get("ok"):
        return h.json({"ok": True, "player": res.get("player"),
                       "path": abs_path})
    return h.json(dict(res, path=abs_path), 500)


@route("POST", r"/api/items/remove")
def remove_items(h: Handler, m, q):
    b = h.body()
    ids = [int(i) for i in (b.get("ids") or []) if str(i).isdigit()]
    if not ids:
        return h.err("未选择条目")
    entries = []
    for iid in ids:
        it = db.get_item(iid)
        if not it:
            continue
        for f in db.files_for_item(iid):
            entries.append({"rel_path": f["rel_path"],
                            "rel_path_key": f["rel_path_key"],
                            "title": it.get("title")})
    lib = _active_lib()
    if lib:
        db.add_ignored(lib["id"], entries)
    db.delete_items(ids)
    h.json({"ok": True, "removed": len(ids), "files": len(entries)})


@route("GET", r"/api/ignored")
def list_ignored(h: Handler, m, q):
    lib = _active_lib()
    if not lib:
        return h.json({"ok": True, "items": []})
    h.json({"ok": True, "items": db.list_ignored(lib["id"])})


@route("POST", r"/api/ignored/restore")
def restore_ignored(h: Handler, m, q):
    lib = _active_lib()
    if not lib:
        return h.err("没有激活的媒体库")
    keys = h.body().get("keys") or []
    n = db.restore_ignored(lib["id"], [str(k) for k in keys])
    h.json({"ok": True, "restored": n})


@route("POST", r"/api/items/(\d+)/rebind")
def rebind_item(h: Handler, m, q):
    iid = int(m.group(1))
    b = h.body()
    tid = b.get("tmdb_id")
    if not tid and b.get("url"):
        mm = re.search(r"/(\d{2,9})(?:[-/?]|$)", str(b["url"]))
        tid = mm.group(1) if mm else None
    if not str(tid or "").isdigit():
        return h.err("请填写 TMDB ID 或作品链接")
    try:
        ok = sync.rebind(iid, int(tid))
    except Exception as exc:
        return h.err("改绑失败：%s" % exc, 500, "rebind_failed")
    h.json({"ok": bool(ok)})


@route("POST", r"/api/items/(\d+)/refresh")
def refresh_item(h: Handler, m, q):
    iid = int(m.group(1))
    it = db.get_item(iid)
    if not it:
        return h.err("条目不存在", 404)
    if h.body().get("reset"):
        # 刮错了：先按文件名抹掉上次的错误结果，再重新刮
        it = sync.reset_item_from_files(iid) or it
    db.update_item(iid, status="pending", locked=0)
    # 入队由 rescrape_item 负责（state='running' 防止 worker 抢占重复刮削）
    threading.Thread(target=sync.rescrape_item, args=(iid,), daemon=True).start()
    h.json({"ok": True, "queued": True})


@route("POST", r"/api/items/(\d+)/reveal")
def reveal_item(h: Handler, m, q):
    iid = int(m.group(1))
    it = db.get_item(iid)
    if not it:
        return h.err("条目不存在", 404)
    if not player.is_local(h.client_ip):
        return h.err("只能在运行服务的主机上定位文件", 403, "remote_forbidden")
    files = db.files_for_item(iid)
    if not files:
        return h.err("没有文件")
    lib = db.get_library(it["library_id"]) or {}
    ok = player.reveal(paths.join_rel(lib.get("root_path") or "", files[0]["rel_path"]))
    h.json({"ok": ok})


@route("POST", r"/api/items/(\d+)/hide")
def hide_item(h: Handler, m, q):
    iid = int(m.group(1))
    hidden = bool(h.body().get("hidden", True))
    db.update_item(iid, hidden=1 if hidden else 0)
    h.json({"ok": True})


# ==================================================================== 同步
@route("POST", r"/api/sync")
def start_sync(h: Handler, m, q):
    b = h.body()
    lib = _active_lib()
    lid = int(b.get("library_id") or (lib["id"] if lib else 0))
    if not lid or not db.get_library(lid):
        return h.err("请先添加媒体库")
    if sync.is_running():
        return h.json({"ok": False, "error": "sync_running",
                       "message": "已有同步任务在执行"}, 409)
    res = sync.start(lid, bool(b.get("full")))
    h.json(res, 202 if res.get("ok") else 409)


@route("GET", r"/api/sync/status")
def sync_status(h: Handler, m, q):
    h.json({"ok": True, "status": sync.status(), "last_run": db.last_run()})


@route("POST", r"/api/sync/retry-failed")
def retry_failed(h: Handler, m, q):
    lib = _active_lib()
    b = h.body()
    n = db.reset_failed(lib["id"] if lib else None,
                        bool(b.get("unidentified")))
    h.json({"ok": True, "count": n})


@route("GET", r"/api/events")
def events(h: Handler, m, q):
    h.close_connection = True  # SSE 无 Content-Length，必须显式关闭连接
    h.send_response(200)
    h.send_header("Content-Type", "text/event-stream; charset=utf-8")
    h.send_header("Cache-Control", "no-cache")
    h.send_header("Connection", "close")
    h.end_headers()
    qq = sync.subscribe()
    try:
        qq.put_nowait({"type": "hello", "status": sync.status()})
        while True:
            try:
                evt = qq.get(timeout=15)
                h.wfile.write(("data: %s\n\n" % json.dumps(evt, ensure_ascii=False))
                              .encode("utf-8"))
            except queue.Empty:
                h.wfile.write(b": ping\n\n")
            h.wfile.flush()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    finally:
        sync.unsubscribe(qq)


# ==================================================================== 配置
@route("GET", r"/api/config")
def get_config(h: Handler, m, q):
    h.json({"ok": True, "config": config.public_view(),
            "players": player.detect(config.load())})


@route("POST", r"/api/config")
def set_config(h: Handler, m, q):
    b = h.body()
    patch: Dict[str, Any] = {}
    if "tmdb" in b and isinstance(b["tmdb"], dict):
        t = {k: v for k, v in b["tmdb"].items() if v is not None}
        if "api_key" in t and "*" in str(t["api_key"]):
            t.pop("api_key")          # 前端回传的打码值不落库
        patch["tmdb"] = t
    for key in ("player", "scan", "sync", "server", "douban"):
        if key in b and isinstance(b[key], dict):
            patch[key] = b[key]
    cfg = config.save(patch)
    h.json({"ok": True, "config": config.public_view(), "saved": list(patch)})


@route("GET", r"/api/backdrop/(\d+)")
def backdrop_img(h: Handler, m, q):
    """背景图懒加载：首次访问才下载并缓存，避免拖慢批量刮削。"""
    iid = int(m.group(1))
    it = db.get_item(iid)
    if not it:
        return h.err("条目不存在", 404)
    p = it.get("backdrop_path")
    if not p:
        return h.err("没有背景图", 404)
    if str(p).startswith("http"):
        local = images.fetch_backdrop(p, it.get("tmdb_id") or iid)
        if not local:
            return h.err("背景图下载失败", 404)
        db.update_item(iid, backdrop_path=local)
        p = local
    target = paths.BACKDROP_DIR / os.path.basename(str(p))
    if not target.is_file():
        return h.err("背景图不存在", 404)
    h._send(target.read_bytes(), "image/jpeg",
            cache="public, max-age=604800, immutable")


# ==================================================================== 占位图
@route("GET", r"/api/placeholder")
def placeholder(h: Handler, m, q):
    title = (q.get("title") or ["?"])[0]
    sub = (q.get("sub") or [""])[0]
    svg = images.placeholder_svg(title, sub)
    h._send(svg.encode("utf-8"), "image/svg+xml",
            cache="public, max-age=86400")


# ==================================================================== 启动
def _lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def _pick_port(host: str, start: int, end: int) -> Optional[int]:
    """探测空闲端口，全部占用时返回 None。

    注意：Windows 上 SO_REUSEADDR 会允许绑定到已被占用的端口（端口劫持），
    所以这里**不能**设置该选项，必须让 bind 在端口被占用时正常失败。
    """
    for p in range(start, end + 1):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind((host if host != "0.0.0.0" else "", p))
            s.close()
            return p
        except OSError:
            s.close()
            continue
    return None


def _seed_library(cfg: Dict[str, Any]) -> None:
    if db.list_libraries():
        return
    seed = cfg.get("seed_library") or {}
    path = (seed.get("path") or "").strip()
    if path and os.path.isdir(path):
        key = paths.normalize_root_key(path)
        if not db.find_library_by_root(key):
            lid = db.add_library(seed.get("name") or os.path.basename(path), path, key)
            db.set_kv("active_library_id", str(lid))


def main() -> None:
    paths.ensure_dirs()
    db.init_db()
    cfg = config.load()
    _seed_library(cfg)

    host = (cfg.get("server") or {}).get("host") or "0.0.0.0"
    start_port = int((cfg.get("server") or {}).get("port") or 8000)
    scan_to = int((cfg.get("server") or {}).get("port_scan_to")
                  or start_port + 10)
    port = _pick_port(host, start_port, scan_to)
    if port is None:
        print("=" * 58)
        print("  [错误] 端口 %d-%d 全部被占用，无法启动" % (start_port, scan_to))
        print("  请关闭其它实例，或修改 config.json 里的 server.port")
        print("=" * 58)
        raise SystemExit(1)

    try:
        httpd = ThreadingHTTPServer((host, port), Handler)
    except OSError as exc:
        print("=" * 58)
        print("  [错误] 无法监听 %s:%d —— %s" % (host, port, exc))
        print("  常见原因：端口被占用，或防火墙/权限限制")
        print("=" * 58)
        raise SystemExit(1)
    httpd.daemon_threads = True

    print("=" * 58)
    print("  影视库海报墙  MediaLibrary")
    print("  本机访问 : http://127.0.0.1:%d" % port)
    if host == "0.0.0.0":
        print("  局域网   : http://%s:%d" % (_lan_ip(), port))
    print("  媒体库   : %s" % (db.list_libraries() or "尚未配置"))
    print("  按 Ctrl+C 停止")
    print("=" * 58)

    # 双击 run.bat 启动时自动打开浏览器（由 run.bat 设置 MEDIA_OPEN=1）
    if os.environ.get("MEDIA_OPEN") == "1":
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(
            "http://127.0.0.1:%d" % port)).start()

    if (cfg.get("sync") or {}).get("auto_on_start"):
        lid = db.active_library_id()
        if lid and not sync.is_running():
            sync.start(lid)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
