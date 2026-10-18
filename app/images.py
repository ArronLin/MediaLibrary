"""海报下载与缓存：内容寻址文件名，跨库跨机器复用。"""
from __future__ import annotations

import hashlib
import os
import re
import socket
import urllib.error
import urllib.request
from typing import Optional, Tuple

from .paths import BACKDROP_DIR, POSTER_DIR

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
DOUBAN_REFERER = "https://movie.douban.com/"


def tmdb_poster_name(tmdb_id: int) -> str:
    return "tmdb_%s.jpg" % tmdb_id


def douban_poster_name(url: str) -> str:
    return "douban_%s.jpg" % hashlib.md5(url.encode("utf-8")).hexdigest()[:16]


def web_poster_name(key) -> str:
    return "web_%s.jpg" % hashlib.md5(str(key).encode("utf-8")).hexdigest()[:16]


def save_poster_bytes(data: bytes, name: str) -> Optional[str]:
    """把已下载的图片字节落盘为海报，返回相对 data/ 的路径。"""
    if not data or len(data) < 2048:
        return None
    dest = POSTER_DIR / name
    if dest.exists() and dest.stat().st_size > 2048:
        return "%s/%s" % (POSTER_DIR.name, name)
    tmp = dest.with_name(name + ".part")
    try:
        tmp.write_bytes(data)
        os.replace(str(tmp), str(dest))
        return "%s/%s" % (POSTER_DIR.name, name)
    except OSError:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        return None


def fetch_web_poster(title: str, year=None, key=None) -> Optional[str]:
    """兜底：用图片搜索找一张竖版相关图当海报。"""
    from . import imagesearch
    hit = imagesearch.find_poster(title, year)
    if not hit:
        return None
    data, _url, _w, _h = hit
    return save_poster_bytes(data, web_poster_name(key or title))


def download(url: str, dest_dir, name: str, referer: Optional[str] = None,
             timeout: float = 20.0) -> Optional[str]:
    """下载到 dest_dir/name，返回相对 data/ 的路径（如 posters/xxx.jpg）。"""
    if not url:
        return None
    dest = dest_dir / name
    if dest.exists() and dest.stat().st_size > 2048:
        return "%s/%s" % (dest_dir.name, name)
    headers = {"User-Agent": UA, "Accept": "image/*,*/*"}
    if referer:
        headers["Referer"] = referer
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
        if len(data) < 512:
            return None
        tmp.write_bytes(data)
        os.replace(str(tmp), str(dest))     # 原子替换，避免半张图
        return "%s/%s" % (dest_dir.name, name)
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError):
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        return None


def fetch_poster(url: str, source: str, key, timeout: float = 20.0) -> Optional[str]:
    """source=tmdb|douban；key 为 tmdb_id 或图片 URL。"""
    if source == "tmdb":
        name = tmdb_poster_name(int(key))
        return download(url, POSTER_DIR, name, timeout=timeout)
    name = douban_poster_name(str(key))
    return download(url, POSTER_DIR, name, referer=DOUBAN_REFERER, timeout=timeout)


def fetch_backdrop(url: str, key) -> Optional[str]:
    if not url:
        return None
    return download(url, BACKDROP_DIR, "bd_%s.jpg" % key)


def _hue(text: str) -> int:
    h = hashlib.md5(text.encode("utf-8")).hexdigest()
    return int(h[:4], 16) % 360


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def placeholder_svg(title: str, subtitle: str = "") -> str:
    """运行时生成占位海报：双色渐变 + 首字，永不 404、零磁盘占用。"""
    title = (title or "?").strip()
    hue = _hue(title)
    c1 = "hsl(%d,42%%,26%%)" % hue
    c2 = "hsl(%d,48%%,14%%)" % ((hue + 42) % 360)
    initial = title[0] if title else "?"
    lines = []
    # 标题按 8 字折行，最多 3 行
    chunk = [title[i:i + 8] for i in range(0, min(len(title), 24), 8)]
    for i, seg in enumerate(chunk):
        lines.append(
            '<text x="150" y="%d" text-anchor="middle" font-size="17" fill="#e8e8ee" '
            'font-family="Microsoft YaHei,PingFang SC,sans-serif" opacity="0.92">%s</text>'
            % (300 + i * 24, _esc(seg)))
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="450" '
        'viewBox="0 0 300 450">'
        '<defs><linearGradient id="g" x1="0" y1="0" x2="0.6" y2="1">'
        '<stop offset="0%%" stop-color="%s"/><stop offset="100%%" stop-color="%s"/>'
        '</linearGradient></defs>'
        '<rect width="300" height="450" fill="url(#g)"/>'
        '<text x="150" y="215" text-anchor="middle" font-size="96" fill="#ffffff" '
        'opacity="0.16" font-family="Microsoft YaHei,PingFang SC,sans-serif">%s</text>'
        '%s'
        '<text x="150" y="%d" text-anchor="middle" font-size="12" fill="#9aa0b5" '
        'font-family="Microsoft YaHei,sans-serif">%s</text>'
        '</svg>'
    ) % (c1, c2, _esc(initial), "".join(lines), 300 + len(chunk) * 24 + 18,
         _esc(subtitle or "暂无海报"))
