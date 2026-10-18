"""配置管理：config.json 的加载、默认值合并、写回。"""
from __future__ import annotations

import copy
import json
import threading
from typing import Any, Dict

from .paths import CONFIG_PATH, ensure_dirs

_LOCK = threading.RLock()
_CACHE = None  # type: Dict[str, Any] | None
_CACHE_MTIME = None  # type: float | None

DEFAULT_CONFIG = {
    "version": 1,
    "server": {
        "host": "0.0.0.0",
        "port": 8000,
        "port_scan_to": 8010,
        "trust_proxy": False,
    },
    "tmdb": {
        "base_url": "https://api.tmdb.org/3",
        "image_base": "https://image.tmdb.org/t/p",
        "api_key": "",
        "auth_mode": "query",
        "language": "zh-CN",
        "fallback_language": "en-US",
        "rate_limit_rps": 3.5,
        "timeout": 12,
    },
    "douban": {"enabled": True, "rate_limit_rps": 0.8, "timeout": 10},
    # LLM 兜底：仅当 TMDB（含跨语言重搜）+ 豆瓣都认不出时才触发，
    # 且只让它产出「候选搜索词」，绝不出 TMDB ID —— 候选仍要过
    # client.search() 的打分管线，分数不达标就照样判未识别。
    # 默认关闭；enabled=false 或没填 api_key/model 时完全不发请求。
    "llm": {
        "enabled": False,
        "base_url": "https://api.openai.com/v1",
        "api_key": "",
        "model": "",
        "temperature": 0.2,
        "max_tokens": 400,
        "timeout": 30,
    },
    "poster": {
        # TMDB / 豆瓣都拿不到海报时，用图片搜索兜底找一张竖版相关图
        "web_search": True,
    },
    "player": {
        # 默认播放器 key（对应 player.KNOWN_PLAYERS），空 = 按探测顺序
        "default": "",
        # 手动指定的播放器路径（key "custom" = 通用自定义 exe）
        "custom_paths": {},
        "allow_remote_play": False,
    },
    "scan": {
        "video_ext": [
            "mp4", "mkv", "avi", "rmvb", "rm", "wmv", "mov",
            "flv", "ts", "m2ts", "mpg", "mpeg", "webm",
        ],
        "min_size_mb": 20,
        "max_depth": 6,
        "exclude": [],
    },
    "sync": {"auto_on_start": False, "scrape_workers": 1},
    "ui": {"card_min_width": 150, "default_sort": "added"},
    # 用户自定义「大系列」：把若干 TMDB 子系列 / 单片收进一个总系列，
    # 打开总系列后再按子系列分组展示。例如：
    #   "super_series": [{
    #     "name": "漫威超英电影",
    #     "children": [
    #       {"name": "钢铁侠系列", "collection_ids": [131292],
    #        "keywords": ["Iron Man", "钢铁侠"]},
    #       {"name": "浩克", "keywords": ["Incredible Hulk", "无敌浩克"]}
    #     ]}]
    "super_series": [],
    # 首次运行时可自动注册的默认库；之后以数据库为准
    "seed_library": {"name": "", "path": ""},
}


def _deep_merge(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load(force: bool = False) -> Dict[str, Any]:
    global _CACHE, _CACHE_MTIME
    with _LOCK:
        # 文件被手工改过（或别的进程写过）就重新读，避免改动不生效
        try:
            mtime = CONFIG_PATH.stat().st_mtime if CONFIG_PATH.exists() else None
        except OSError:
            mtime = None
        if _CACHE is not None and not force and mtime == _CACHE_MTIME:
            return _CACHE
        ensure_dirs()
        raw = {}
        if CONFIG_PATH.exists():
            try:
                raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except Exception:
                raw = {}
        cfg = _deep_merge(DEFAULT_CONFIG, raw)
        _CACHE = cfg
        _CACHE_MTIME = mtime
        if not CONFIG_PATH.exists():
            save(cfg)
        return cfg


def save(patch: Dict[str, Any]) -> Dict[str, Any]:
    """深合并写回，返回最新配置。"""
    global _CACHE, _CACHE_MTIME
    with _LOCK:
        current = load()
        merged = _deep_merge(current, patch)
        CONFIG_PATH.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        _CACHE = merged
        try:
            _CACHE_MTIME = CONFIG_PATH.stat().st_mtime
        except OSError:
            _CACHE_MTIME = None
        return merged


def get(path: str, default=None):
    """点号取值，如 get('tmdb.api_key')。"""
    node = load()  # type: Any
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def masked_api_key() -> str:
    key = str(get("tmdb.api_key", "") or "")
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return key[:4] + "*" * 8 + key[-4:]


def _mask(key: str) -> str:
    key = str(key or "")
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return key[:4] + "*" * 8 + key[-4:]


def public_view() -> Dict[str, Any]:
    """返回给前端的配置，敏感字段打码。"""
    cfg = copy.deepcopy(load())
    key = cfg.get("tmdb", {}).get("api_key", "")
    cfg["tmdb"]["api_key"] = _mask(key)
    cfg["tmdb"]["api_key_set"] = bool(key)
    lm = cfg.get("llm") or {}
    lkey = str(lm.get("api_key") or "")
    lm["api_key"] = _mask(lkey)
    lm["api_key_set"] = bool(lkey)
    cfg["llm"] = lm
    return cfg
