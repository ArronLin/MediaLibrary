"""LLM 兜底：给「换成任何搜索词都搜不到」的条目补几个候选片名。

它只做一件事：产出**候选搜索词**（含年份）。绝不让它直接产出 TMDB ID
或元数据 —— 模型编造 ID 的风险太高，而搜索词猜错最多是搜不到：
候选词仍要过 `client.search()` + `_score()` 的既有打分管线，
分数不达标就照样判 unidentified，不会把错片写进库。

触发条件（全部满足才会调用）：
  * TMDB 检索（含跨语言重搜）+ 豆瓣都没命中
  * 海报 web 兜底也没救回来
  * 条目未 locked
  * 本次刮削任务还没试过 LLM（避免重试时反复烧钱）

典型场景：港台译名与 TMDB 官方译名完全无交集 ——
《007：你死我活》的港版叫《生死关头》，
任何从文件名派生的查询串都撞不上，只有「007 系列第 8 部」这类
外部知识才能把两者对上。
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from . import config

# 模型可能把代码块围栏一起返回
_RE_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.I)

_PROMPT = """你是一个影视资料库的辅助识别助手。下面有一个视频文件的原始文件名，
常规搜索引擎（TMDB / 豆瓣）都找不到它。

请根据文件名（含其中的语言标签、年份、译名、系列编号等所有线索）推断
这部影片**最可能的官方片名**，以便拿去 TMDB 检索。

要求：
- 只输出 JSON，不要任何解释文字
- 字段：candidates（字符串数组，1~5 个）、year（整数或 null）
- candidates 里放不同语言/不同写法的片名，越可能命中越靠前；
  文件名里已有的名字也要带上
- 影片是系列作品时，可以给出「系列名 + 序号」的形式
- 确实毫无线索时返回 {{"candidates": [], "year": null}}

文件名：{raw}
已解析出的信息：{parsed}"""


def _cfg() -> Dict[str, Any]:
    c = (config.load() or {}).get("llm") or {}
    return c if isinstance(c, dict) else {}


def enabled() -> bool:
    c = _cfg()
    return bool(c.get("enabled") and str(c.get("api_key") or "").strip()
                and str(c.get("model") or "").strip())


def _base_url() -> str:
    u = str(_cfg().get("base_url") or "https://api.openai.com/v1").rstrip("/")
    if not u:
        u = "https://api.openai.com/v1"
    return u


def _extract(text: str) -> Optional[Dict[str, Any]]:
    """从模型输出里抠出 JSON，容忍代码块围栏和多余文字。"""
    if not text:
        return None
    s = _RE_FENCE.sub(" ", text).strip()
    for chunk in (s,               # 整体
                  s[s.find("{"):s.rfind("}") + 1] if "{" in s else ""):
        if not chunk:
            continue
        try:
            obj = json.loads(chunk)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _clean_candidates(v: Any) -> List[str]:
    out: List[str] = []
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, (list, tuple)):
        return out
    for x in v:
        if isinstance(x, dict):
            # 容错：{"title": "..."} 这种形状
            x = x.get("title") or x.get("name") or ""
        if not isinstance(x, str):
            continue
        t = re.sub(r"\s+", " ", x).strip(" .-_·:：,，、\"'")
        # 太短的、明显是技术标签的不要
        if len(t) < 2 or len(t) > 80:
            continue
        if re.search(r"1080p|720p|2160p|x26[45]|bluray|webrip|web-dl|mkv|mp4",
                     t, re.I):
            continue
        if t not in out:
            out.append(t)
    return out[:5]


def suggest(payload: Dict[str, Any]) -> Dict[str, Any]:
    """返回 {"queries": [...], "year": int|None}；失败一律返回空。

    payload 用 sync._payload_from_item() 的产物（含 cn_title / en_title /
    year / queries），另外需要 raw 文件名才能让模型看到全部线索。
    """
    if not enabled():
        return {"queries": [], "year": None}
    c = _cfg()
    raw = (payload or {}).get("raw") or (payload or {}).get("title") or ""
    parsed = {k: (payload or {}).get(k) for k in
              ("title", "cn_title", "en_title", "year", "queries", "kind")
              if (payload or {}).get(k)}
    prompt = _PROMPT.format(raw=raw or "（未知）",
                            parsed=json.dumps(parsed, ensure_ascii=False))
    body = {
        "model": str(c.get("model")),
        "messages": [
            {"role": "system", "content": "你只输出 JSON。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": float(c.get("temperature") if c.get("temperature")
                             is not None else 0.2),
        "max_tokens": int(c.get("max_tokens") or 400),
    }
    req = urllib.request.Request(
        _base_url().rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + str(c.get("api_key") or "").strip(),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(
                req, timeout=float(c.get("timeout") or 30)) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        # 静默失败：兜底挂了不该中断正常刮削流程
        return {"queries": [], "year": None}

    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return {"queries": [], "year": None}
    obj = _extract(text if isinstance(text, str) else "")
    if not obj:
        return {"queries": [], "year": None}
    year = obj.get("year")
    if not isinstance(year, int) or not (1900 <= year <= 2100):
        year = None
    return {"queries": _clean_candidates(obj.get("candidates")), "year": year}
