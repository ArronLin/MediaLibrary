"""TMDB 客户端：限速、重试、搜索打分。

重要：TMDB 主域名 api.themoviedb.org 在部分网络下不可达，
必须使用可配置的 base_url（默认 https://api.tmdb.org/3）。
"""
from __future__ import annotations

import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional

UA = "MediaLibrary/1.0 (Windows; local)"


class TmdbError(Exception):
    pass


class ApiKeyError(TmdbError):
    """key 无效：必须中止整批，不能静默重试。"""


class RateLimiter:
    """令牌桶，多线程共享。"""

    def __init__(self, rps: float = 3.5):
        self.interval = 1.0 / max(0.2, rps)
        self.next_at = 0.0
        self._lock = __import__("threading").Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self.next_at:
                delay = self.next_at - now
            else:
                delay = 0.0
            self.next_at = max(now, self.next_at) + self.interval
        if delay > 0:
            time.sleep(delay)


_RE_TRAIL_YEAR = re.compile(r"[\s(（]*\b(?:19|20)\d{2}\b[\s)）]*$")


def _sim(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    a = a.lower().strip()
    b = b.lower().strip()
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


class Tmdb:
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg or {}
        self.base = str(self.cfg.get("base_url") or "https://api.tmdb.org/3").rstrip("/")
        self.image_base = str(self.cfg.get("image_base")
                              or "https://image.tmdb.org/t/p").rstrip("/")
        self.key = str(self.cfg.get("api_key") or "").strip()
        self.auth_mode = str(self.cfg.get("auth_mode") or "query").lower()
        self.lang = str(self.cfg.get("language") or "zh-CN")
        self.fallback_lang = str(self.cfg.get("fallback_language") or "en-US")
        self.timeout = float(self.cfg.get("timeout") or 12)
        self.limiter = RateLimiter(float(self.cfg.get("rate_limit_rps") or 3.5))

    # ------------------------------------------------------------ http
    def _request(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self.key:
            raise ApiKeyError("未配置 TMDB API Key")
        p = dict(params or {})
        headers = {"User-Agent": UA, "Accept": "application/json"}
        if self.auth_mode == "bearer":
            headers["Authorization"] = "Bearer " + self.key
        else:
            p["api_key"] = self.key
        url = "%s%s?%s" % (self.base, path, urllib.parse.urlencode(p))

        last_err = None
        for attempt in range(3):
            self.limiter.wait()
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8", "replace"))
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    raise ApiKeyError("TMDB API Key 无效或已失效（401）")
                if e.code == 404:
                    raise TmdbError("not_found")
                if e.code == 429:
                    ra = e.headers.get("Retry-After") if e.headers else None
                    wait = float(ra) if ra and ra.isdigit() else 2.0 * (attempt + 1)
                    time.sleep(min(wait, 20))
                    last_err = e
                    continue
                if 500 <= e.code < 600:
                    time.sleep(1.5 * (attempt + 1))
                    last_err = e
                    continue
                raise TmdbError("HTTP %s" % e.code)
            except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
                last_err = e
                time.sleep(1.2 * (attempt + 1))
            except json.JSONDecodeError as e:
                last_err = e
                time.sleep(0.8)
        raise TmdbError("请求失败: %s" % (last_err,))

    def poster_url(self, path: Optional[str], size: str = "w500") -> Optional[str]:
        if not path:
            return None
        return "%s/%s%s" % (self.image_base, size, path)

    # ------------------------------------------------------------ search
    def _candidate(self, raw: Dict[str, Any], kind: str) -> Dict[str, Any]:
        if kind == "movie":
            title = raw.get("title") or raw.get("original_title") or ""
            orig = raw.get("original_title") or ""
            date = raw.get("release_date") or ""
        else:
            title = raw.get("name") or raw.get("original_name") or ""
            orig = raw.get("original_name") or ""
            date = raw.get("first_air_date") or ""
        year = None
        if len(date) >= 4 and date[:4].isdigit():
            year = int(date[:4])
        return {"tmdb_id": raw.get("id"), "title": title, "original_title": orig,
                "year": year, "popularity": raw.get("popularity") or 0,
                "poster_path": raw.get("poster_path")}

    def _sim_of(self, parsed: Dict[str, Any], cand: Dict[str, Any],
                query: str = "") -> float:
        cn = parsed.get("cn_title") or ""
        en = parsed.get("en_title") or ""
        sim = max(_sim(cn, cand["title"]), _sim(cn, cand["original_title"]),
                  _sim(en, cand["title"]), _sim(en, cand["original_title"]))
        # 用降级变体搜到的结果，也要和变体本身比一次
        # （「给阿嬷的情书 潮汕话」→ 用「给阿嬷的情书」命中）
        if query:
            sim = max(sim, _sim(query, cand["title"]),
                      _sim(query, cand["original_title"]))
        return sim

    def _score(self, parsed: Dict[str, Any], cand: Dict[str, Any], rank: int,
               query: str = "") -> float:
        sim = self._sim_of(parsed, cand, query)
        py, cy = parsed.get("year"), cand.get("year")
        if py and cy:
            d = abs(py - cy)
            if d == 0:
                year_score = 1.0
            elif d <= 1:
                year_score = 0.5
            elif d <= 3:
                year_score = 0.0
            else:
                # 年份差得远：不是硬排除（文件名里的年份本身也可能是错的，
                # 例如 Alien.1973 其实是 1979），但要压到「正确年份的候选」之下
                year_score = -0.15
        else:
            year_score = 0.5
        rank_score = 1.0 / (1 + rank)
        # 片名完全一致是很强的信号。TMDB 的搜索结果按热度排，系列名会被
        # 排在续作后面（「美国队长」第 1 名是《美国队长3》人气 35.4，
        # 第 2 名才是 2011 年的《美国队长》17.6）。此时 sim 1.0 vs 0.889
        # 只差 0.05 分、而 rank 差 0.10 分，精确匹配反而输给续作。
        # 故 sim≈1 时把 rank 权重抬满，让「标题完全一致」不被热度盖掉。
        if sim >= 0.95:
            rank_score = 1.0
        return 0.45 * sim + 0.35 * year_score + 0.20 * rank_score

    def search(self, parsed: Dict[str, Any], kind: str = "movie") -> Optional[Dict]:
        """按 query 序列搜索并选出最可信的一条。

        两轮：
          ① 年份作为接口参数（TMDB 按年份硬过滤）—— 最准，
             `365 Days` + year=2020 能直接命中《黑帮大佬和我的365日》
          ② 退回纯文本查询（年份拼在字符串里）—— 年份本身可能不准时兜底
        """
        queries = parsed.get("queries") or [parsed.get("title") or ""]
        endpoint = "/search/movie" if kind == "movie" else "/search/tv"
        year = parsed.get("year")
        year_param = "year" if kind == "movie" else "first_air_date_year"
        best: Optional[Dict[str, Any]] = None

        def consider(results, q: str, min_sim: float = 0.0) -> None:
            nonlocal best
            for rank, raw in enumerate(results[:8]):
                cand = self._candidate(raw, kind)
                sim = self._sim_of(parsed, cand, q)
                # 带年份过滤的那一轮，TMDB 会放宽片名匹配返回一堆无关结果，
                # 片名对不上就丢掉，否则「异形 1973」会被认成《黄蜂女来袭》
                if min_sim and sim < min_sim:
                    continue
                # 纯文本轮（无年份参数）里，TMDB 的模糊搜索会拿热门片凑数：
                # 「Hunter Return To Justice 2002」（TMDB 尚未收录）曾匹配到
                # 相差 18 年的《Monster Hunter》还拿了 0.65 分。年份差得远
                # 且片名又不是精确匹配的候选直接淘汰——文件名年份本身可能
                # 是错的，所以只有「标题精确一致」才豁免年份审查。
                py, cy = parsed.get("year"), cand.get("year")
                if py and cy and abs(py - cy) > 5 and sim < 0.9:
                    continue
                sc = self._score(parsed, cand, rank, q)
                # 同分时按 TMDB 热度决胜：同名的《美国队长》有 2011 和 1990
                # 两版，sim 都是 1.0，靠 popularity 稳定地取到 2011 那一部
                if (best is None or sc > best["score"]
                        or (sc == best["score"]
                            and (cand["popularity"] or 0)
                            > (best.get("popularity") or 0))):
                    best = dict(cand, score=sc, query=q)

        def run(qs, extra=None, lang: Optional[str] = None,
                min_sim: float = 0.0) -> bool:
            """跑一批查询，返回是否已经足够可信可以停。"""
            for q in qs:
                if not q:
                    continue
                params = {"query": q, "language": lang or self.lang,
                          "include_adult": "false"}
                if extra:
                    params.update(extra)
                try:
                    data = self._request(endpoint, params)
                except TmdbError:
                    continue
                consider(data.get("results") or [], q, min_sim)
                if best and best["score"] >= 0.75:
                    return True
            return False

        # 第一轮：带年份参数，用去掉年份的裸标题
        if year:
            bare = []
            for q in queries:
                b = _RE_TRAIL_YEAR.sub("", q).strip()
                if b and b not in bare:
                    bare.append(b)
            if run(bare[:3], {year_param: str(year)}, min_sim=0.55):
                return best if best["score"] >= 0.35 else None

        # 第二轮：纯文本查询
        run(queries, {})

        # 第三轮：换语言重搜。zh-CN 检索只回中文标题，而
        #   original_title 也可能是中文 —— 《96分钟：列车爆炸案》的
        #   original_title 是繁体「96分鐘」，与英文文件名「96 Minutes」
        #   相似度只有 0.286，前两轮全被过滤。用 en-US 再搜一次才能对上。
        # 只有前两轮没拿到高置信结果时才跑，正常情况不增加请求。
        alt = "zh-CN" if self.lang != "zh-CN" else "en-US"
        if not best or best["score"] < 0.72:
            if year:
                run(bare[:3], {year_param: str(year)}, lang=alt,
                    min_sim=0.55)
            run(queries, lang=alt)

        if not best or best["score"] < 0.35:
            return None
        return best

    # ------------------------------------------------------------ details
    def details(self, tmdb_id: int, kind: str) -> Dict[str, Any]:
        path = "/movie/%d" % tmdb_id if kind == "movie" else "/tv/%d" % tmdb_id
        data = self._request(path, {"language": self.lang,
                                    "append_to_response": "credits,external_ids"})
        out = self._normalize(data, kind)
        if not out.get("overview"):
            try:
                alt = self._request(path, {"language": self.fallback_lang,
                                           "append_to_response": "credits,external_ids"})
                out["overview_en"] = (alt.get("overview") or "").strip()
            except TmdbError:
                pass
        if kind == "tv":
            out["seasons"] = [{
                "season_number": s.get("season_number"),
                "name": s.get("name"),
                "overview": s.get("overview"),
                "poster_path": self.poster_url(s.get("poster_path")),
                "episode_count": s.get("episode_count"),
            } for s in (data.get("seasons") or []) if (s.get("season_number") or 0) >= 0]
        return out

    def season(self, tmdb_id: int, number: int) -> List[Dict[str, Any]]:
        data = self._request("/tv/%d/season/%d" % (tmdb_id, number),
                             {"language": self.lang})
        return [{
            "season_number": number,
            "episode_number": e.get("episode_number"),
            "name": e.get("name"),
            "overview": e.get("overview"),
            "still_path": self.poster_url(e.get("still_path"), "w300"),
            "air_date": e.get("air_date"),
            "runtime": e.get("runtime"),
        } for e in (data.get("episodes") or [])]

    def any_poster(self, tmdb_id: int, kind: str,
                   languages: str = "zh,en,null") -> Optional[str]:
        """取任意语言的海报（详情接口的 poster_path 为空时兜底）。"""
        path = ("/movie/%d/images" if kind == "movie" else "/tv/%d/images") % tmdb_id
        try:
            data = self._request(path, {"include_image_language": languages})
        except TmdbError:
            return None
        posters = data.get("posters") or []
        if not posters:
            return None
        posters.sort(key=lambda p: (-(p.get("vote_average") or 0),
                                    -(p.get("width") or 0)))
        return self.poster_url(posters[0].get("file_path"), "w500")

    def collection(self, collection_id: int) -> Dict[str, Any]:
        """系列（合集）详情：名称、简介、封面、成员列表。"""
        data = self._request("/collection/%d" % int(collection_id),
                             {"language": self.lang})
        parts = data.get("parts") or []
        return {
            "id": data.get("id"),
            "name": data.get("name") or "",
            "overview": (data.get("overview") or "").strip(),
            "poster_path": self.poster_url(data.get("poster_path"), "w500"),
            "backdrop_path": self.poster_url(data.get("backdrop_path"), "w1280"),
            "parts": [{"tmdb_id": p.get("id"),
                       "title": p.get("title") or p.get("name") or "",
                       "year": int(p["release_date"][:4])
                       if str(p.get("release_date") or "")[:4].isdigit() else None,
                       "poster_path": self.poster_url(p.get("poster_path"), "w500")}
                      for p in parts],
        }

    def collection_of(self, tmdb_id: int, kind: str = "movie") -> Optional[Dict[str, Any]]:
        """轻量查询某条目所属系列，只取 belongs_to_collection，不重搜。

        旧版本入库时没保存 collection 字段，用它做一次性回填。
        """
        if kind != "movie":
            return None
        try:
            data = self._request("/movie/%d" % int(tmdb_id), {"language": self.lang})
        except TmdbError:
            return None
        col = data.get("belongs_to_collection") or {}
        if not col.get("id"):
            return None
        return {
            "collection_id": col.get("id"),
            "collection_name": col.get("name") or "",
            "collection_poster": self.poster_url(col.get("poster_path"), "w500"),
            "collection_backdrop": self.poster_url(col.get("backdrop_path"), "w1280"),
        }

    def _normalize(self, d: Dict[str, Any], kind: str) -> Dict[str, Any]:
        if kind == "movie":
            title = d.get("title") or d.get("original_title") or ""
            orig = d.get("original_title") or ""
            date = d.get("release_date") or ""
            runtime = d.get("runtime")
        else:
            title = d.get("name") or d.get("original_name") or ""
            orig = d.get("original_name") or ""
            date = d.get("first_air_date") or ""
            ert = d.get("episode_run_time") or []
            runtime = ert[0] if ert else None
        year = int(date[:4]) if date[:4].isdigit() else None
        credits = d.get("credits") or {}
        cast = [{"name": c.get("name"), "character": c.get("character"),
                 "profile": self.poster_url(c.get("profile_path"), "w185")}
                for c in (credits.get("cast") or [])[:8]]
        directors = [c.get("name") for c in (credits.get("crew") or [])
                     if c.get("job") in ("Director",)]
        ext = d.get("external_ids") or {}
        col = d.get("belongs_to_collection") or {}
        return {
            "tmdb_id": d.get("id"),
            "title": title,
            "original_title": orig,
            "year": year,
            "overview": (d.get("overview") or "").strip(),
            "overview_en": "",
            "poster_path": self.poster_url(d.get("poster_path")),
            "backdrop_path": self.poster_url(d.get("backdrop_path"), "w1280"),
            "rating": round(float(d.get("vote_average") or 0), 1) or None,
            "runtime": runtime,
            "genres": [g.get("name") for g in (d.get("genres") or [])],
            "cast": cast,
            "director": " / ".join(directors[:2]) if directors else None,
            "imdb_id": ext.get("imdb_id"),
            "collection_id": col.get("id"),
            "collection_name": col.get("name"),
            "collection_poster": self.poster_url(col.get("poster_path"), "w500"),
            "collection_backdrop": self.poster_url(col.get("backdrop_path"), "w1280"),
        }
