"""豆瓣兜底：只用 subject_suggest 接口（免登录），详情页有反爬不再尝试。

返回海报/片名/年份，无简介 —— 这是当前网络环境下的能力上限。
"""
from __future__ import annotations

import hashlib
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional

from . import db

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
REFERER = "https://movie.douban.com/"
ENDPOINT = "https://movie.douban.com/j/subject_suggest?q="

_last_at = 0.0
_throttle_lock = __import__("threading").Lock()


def _throttle(rps: float) -> None:
    global _last_at
    interval = 1.0 / max(0.2, rps)
    with _throttle_lock:
        now = time.monotonic()
        delay = max(0.0, interval - (now - _last_at))
        _last_at = now + delay
    if delay > 0:
        time.sleep(delay)


def suggest(query: str, cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not query.strip():
        return []
    cache_key = "douban:" + hashlib.sha1(query.encode("utf-8")).hexdigest()
    cached = db.cache_get(cache_key)
    if cached:
        try:
            return json.loads(cached)
        except json.JSONDecodeError:
            pass

    _throttle(float((cfg or {}).get("rate_limit_rps") or 0.8))
    url = ENDPOINT + urllib.parse.quote(query)
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Referer": REFERER, "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=float((cfg or {}).get("timeout") or 10)) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, socket.timeout, TimeoutError, json.JSONDecodeError):
        return []
    items = []
    for d in data or []:
        if d.get("type") not in ("movie", "tv"):
            continue
        items.append({
            "title": d.get("title") or "",
            "sub_title": d.get("sub_title") or "",
            "year": d.get("year") or "",
            "img": d.get("img") or "",
            "type": d.get("type"),
            "id": d.get("id"),
        })
    db.cache_put(cache_key, json.dumps(items, ensure_ascii=False), 200, 86400 * 7)
    return items


def _sim(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


_RE_TAIL_YEAR = __import__("re").compile(r"[\s(（]*\b(?:19|20)\d{2}\b[\s)）]*$")


def _queries_for(parsed: Dict[str, Any]) -> List[str]:
    """豆瓣 suggest 是前缀提示接口，越长的查询越可能一条都不返回。

    `圆桌派 第八季` → 0 条，`圆桌派` → 5 条。
    所以先试「不带年份」的写法，且由短到长，最后才补上带年份的。
    """
    raw = list(parsed.get("queries") or [])
    if not raw:
        raw = [parsed.get("cn_title") or parsed.get("title") or ""]
    bare, withy, out = [], [], []
    for q in raw:
        q = (q or "").strip()
        if not q:
            continue
        (withy if _RE_TAIL_YEAR.search(q) else bare).append(q)
    bare.sort(key=len)                     # 短的先试，命中率更高
    for q in bare + withy:
        if q not in out:
            out.append(q)
    return out[:6]


def pick(parsed: Dict[str, Any], cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """按标题相似度 + 年份挑选最可信的一条。"""
    best, best_score = None, 0.0
    for q in _queries_for(parsed):
        got = suggest(q, cfg)
        if not got:
            continue
        for item in got:
            sim = max(_sim(parsed.get("cn_title") or "", item["title"]),
                      _sim(parsed.get("en_title") or "", item["sub_title"]),
                      _sim(parsed.get("title") or "", item["title"]))
            score = sim
            py = parsed.get("year")
            iy = item["year"]
            if py and iy and str(py) == str(iy):
                score += 0.25
            elif py and iy:
                score -= 0.1
            if score > best_score:
                best, best_score = item, score
        if best_score >= 0.85:
            break
    if best and best_score >= 0.5:
        best = dict(best)
        best["score"] = best_score
        return best
    return None
