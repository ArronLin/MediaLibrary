"""兜底搜图：TMDB / 豆瓣都拿不到海报时，用国内图片搜索找一张相关图片当海报。

只在内置图源全部失败时才走这里，所以可以接受慢一点（每条 1~4 秒）。
选图偏保守：必须竖版、够大、长宽比接近海报；宁可返回 None 让前端用占位图，
也不要贴一张不相干的横图上去。

数据源（按顺序尝试，任一可用即可）：
  1. 360 图片 JSON 接口 —— 直接带 width/height，可在下载前就筛掉横图
  2. 必应图片 HTML   —— 只给 URL，需要下载后用文件头解析宽高
百度图片接口会返回 {"antiFlag":1,"message":"Forbid spider access"}，不可用。
"""
from __future__ import annotations

import json
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import List, Optional, Tuple

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

MAX_BYTES = 8 * 1024 * 1024
MIN_W, MIN_H = 180, 260
RATIO_LO, RATIO_HI = 1.12, 2.30

_RE_MURL = re.compile(r'&quot;murl&quot;:&quot;(.*?)&quot;')

_throttle_lock = threading.Lock()
_next_at = 0.0
_SEARCH_INTERVAL = 0.9      # 全局节流，避免连续请求被降级成通用结果


def _throttle() -> None:
    global _next_at
    with _throttle_lock:
        now = time.monotonic()
        delay = max(0.0, _next_at - now)
        _next_at = max(now, _next_at) + _SEARCH_INTERVAL
    if delay:
        time.sleep(delay)


def _http_get(url: str, referer: Optional[str] = None,
              timeout: float = 10.0, limit: int = MAX_BYTES) -> Optional[bytes]:
    headers = {
        "User-Agent": UA,
        "Accept": "image/*,text/html,application/json,*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if referer:
        headers["Referer"] = referer
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(limit)
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError,
            ValueError):
        return None


# ------------------------------------------------------------------ 图片头
def _dims(data: bytes) -> Optional[Tuple[int, int]]:
    """从图片头解析宽高，支持 JPEG / PNG / GIF。"""
    if not data or len(data) < 24:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        w = int.from_bytes(data[16:20], "big")
        h = int.from_bytes(data[20:24], "big")
        return (w, h) if w and h else None
    if data[:3] == b"GIF":
        return (int.from_bytes(data[6:8], "little"),
                int.from_bytes(data[8:10], "little"))
    if data[:2] == b"\xff\xd8":
        i, n = 2, len(data)
        while i + 9 < n:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            seg_len = int.from_bytes(data[i + 2:i + 4], "big")
            if seg_len < 2:
                return None
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h = int.from_bytes(data[i + 5:i + 7], "big")
                w = int.from_bytes(data[i + 7:i + 9], "big")
                return (w, h) if w and h else None
            i += 2 + seg_len
    return None


def _score(w: int, h: int) -> float:
    """竖版海报评分：长宽比越接近 1.5、像素越多越好。"""
    if w < MIN_W or h < MIN_H:
        return 0.0
    ratio = h / float(w)
    if not (RATIO_LO <= ratio <= RATIO_HI):
        return 0.0
    aspect = 1.0 - min(abs(ratio - 1.5) / 1.0, 1.0)
    size = min((w * h) / 600000.0, 1.0)
    return 0.65 * aspect + 0.35 * size


def _num(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


# ------------------------------------------------- 相关性加权（避免搜到活动海报）
# 曾经把「PE 圆桌派 · 线上投资沙龙」这种清华活动海报当成《圆桌派》封面。
# 光看尺寸判断不出来，只能靠结果标题和图片域名做二次筛选。
_RE_BAD_TITLE = re.compile(
    r"沙龙|招聘|邀请函|模板|素材|设计稿|报名|门票|促销|优惠券|"
    r"直播预告|公开课|课程|讲座|峰会|论坛|海报设计|视频封面|公众号|"
    r"二维码|扫码|广告|宣传单|易拉宝|展板|"
    r"热线|电话|地址|微信|订购|加盟|招商|官网|官网首页|旗舰店|专卖", re.I)
# 专业影视图库 / 官方 CDN，命中基本就是真海报
_RE_GOOD_HOST = re.compile(
    r"(doubanio\.com|douban\.com|tmdb\.org|themoviedb\.org|imdb\.com|"
    r"mtime\.com|maoyan\.com|piaofang|ent\.sina|img\d*\.ykimg\.com)", re.I)
_RE_POSTER_PATH = re.compile(r"(poster|cover|p\d{6,})", re.I)


def _adjust(url: str, title: str = "") -> float:
    """在尺寸分之上做 ±0.3 的相关性修正。"""
    adj = 0.0
    if title and _RE_BAD_TITLE.search(title):
        adj -= 0.45                      # 活动/模板类图片，基本可以排除
    if _RE_GOOD_HOST.search(url):
        adj += 0.22
    if _RE_POSTER_PATH.search(url):
        adj += 0.08
    return adj


# ------------------------------------------------------------------ 数据源
def _from_360(query: str, pages: int = 2) -> List[Tuple[float, str, int, int]]:
    """360 图片：直接带宽高，只收竖版。返回 [(score, url, w, h)]。"""
    out: List[Tuple[float, str, int, int]] = []
    for page in range(pages):
        _throttle()
        url = ("https://image.so.com/j?q=%s&pn=%d&rn=20&src=srp"
               % (urllib.parse.quote(query), page * 20))
        raw = _http_get(url, referer="https://image.so.com/", timeout=10)
        if not raw:
            continue
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            continue
        for it in data.get("list") or []:
            w, h = _num(it.get("width")), _num(it.get("height"))
            sc = _score(w, h)
            if sc <= 0:
                continue
            img = (it.get("img") or it.get("thumb") or it.get("_thumb") or "").strip()
            if not img.startswith("http") or img.lower().endswith(".gif"):
                continue
            title = str(it.get("title") or it.get("alt") or "")
            if title and _RE_BAD_TITLE.search(title):
                continue                  # 活动/模板类图片直接丢
            sc = max(0.01, sc + _adjust(img, title))
            out.append((sc, img, w, h))
    out.sort(key=lambda x: -x[0])
    return out


def _bing_urls(query: str, limit: int = 10) -> List[str]:
    _throttle()
    url = ("https://cn.bing.com/images/search?q=%s&form=HDRSC2&first=1"
           % urllib.parse.quote(query))
    raw = _http_get(url, timeout=10)
    if not raw:
        return []
    txt = raw.decode("utf-8", "replace")
    out: List[str] = []
    for u in _RE_MURL.findall(txt):
        u = u.replace("&amp;", "&").strip()
        if not u.startswith("http"):
            continue
        low = u.lower()
        if low.endswith(".gif") or low.endswith(".svg"):
            continue
        if u not in out:
            out.append(u)
        if len(out) >= limit:
            break
    return out


def _from_bing(query: str, budget: int) -> List[Tuple[float, str, int, int]]:
    """必应：只给 URL，下载后解析宽高。"""
    out: List[Tuple[float, str, int, int]] = []
    for u in _bing_urls(query)[:budget]:
        data = _http_get(u, referer="https://cn.bing.com/", timeout=8)
        if not data or len(data) < 2048:
            continue
        wh = _dims(data)
        if not wh:
            continue
        sc = _score(wh[0], wh[1])
        if sc > 0:
            out.append((max(0.01, sc + _adjust(u)), u, wh[0], wh[1]))
    out.sort(key=lambda x: -x[0])
    return out


_RE_SEASON_TAIL = re.compile(
    r"[\s·\-—_]*((?:第\s*[一二三四五六七八九十百零\d]{1,3}\s*[季部])|"
    r"(?:season\s*\d{1,2})|(?:(?<![a-z])s\d{1,2}))\s*$", re.I)


def _titles(title: str) -> List[str]:
    """片名变体：`圆桌派 第八季` 也要能用 `圆桌派` 搜一次。"""
    out = [title]
    cur = title
    for _ in range(2):
        nxt = _RE_SEASON_TAIL.sub("", cur).strip(" ·-—_")
        if nxt == cur or len(nxt) < 2:
            break
        cur = nxt
        if cur not in out:
            out.append(cur)
    return out


def _queries(title: str, year: Optional[int],
             kinds: Tuple[str, ...]) -> List[str]:
    qs: List[str] = []
    for t in _titles(title):
        if year:
            qs.append("%s %d 海报" % (t, year))
        for suffix in kinds:
            qs.append(("%s %s" % (t, suffix)).strip())
        qs.append(t)
    seen, out = set(), []
    for q in qs:
        if q and q not in seen:
            seen.add(q)
            out.append(q)
    return out


def find_poster(title: str, year: Optional[int] = None,
                kinds: Tuple[str, ...] = ("海报", "电影 海报", "剧照"),
                max_try: int = 6) -> Optional[Tuple[bytes, str, int, int]]:
    """返回 (图片字节, 来源 URL, 宽, 高)；找不到返回 None。"""
    title = (title or "").strip()
    if len(title) < 2:
        return None

    candidates: List[Tuple[float, str, int, int]] = []
    for q in _queries(title, year, kinds):
        candidates.extend(_from_360(q))
        if candidates and candidates[0][0] >= 0.72:
            break
        if len(candidates) >= max_try:
            break

    if not candidates:
        # 360 没结果再试必应，预算控制得小一点
        for q in _queries(title, year, kinds)[:2]:
            candidates.extend(_from_bing(q, budget=4))
            if candidates and candidates[0][0] >= 0.72:
                break

    if not candidates:
        return None

    candidates.sort(key=lambda x: -x[0])
    tried = 0
    for sc, url, w, h in candidates:
        tried += 1
        if tried > max_try:
            break
        data = _http_get(url, referer="https://image.so.com/", timeout=10)
        if not data or len(data) < 2048:
            continue
        if data[:3] == b"GIF":      # 一律存成 .jpg，GIF 会显示异常
            continue
        real = _dims(data)
        if not real:
            continue
        rw, rh = real
        if _score(rw, rh) <= 0:
            continue
        return (data, url, rw, rh)
    return None
