"""青少年模式（非成人模式）：内容判定、成人密码与会话。

安全边界：这是家长控制，不是对抗性安全。能直接读写 data/library.db
或 config.json 的人可以绕过（详见 README）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from typing import Any, Dict, List, Optional

from . import config, db

DEFAULT_USERNAME = "sysadmin"
DEFAULT_PASSWORD = "changeme"

DEFAULT_BLOCKED_GENRES = ["恐怖", "恐怖片", "惊悚", "情色", "色情", "成人", "血腥"]
DEFAULT_BLOCKED_CERTS = ["R", "NC-17", "TV-MA", "18+", "18", "III", "R18", "R18+", "X"]
DEFAULT_BLOCKED_TITLE_KEYWORDS = ["色情", "情色", "三级", "成人版", "无码"]

KV_PASSWORD = "kids_password"
KV_PASSWORD_DEFAULT = "kids_password_default"

COOKIE_NAME = "ml_adult"
SESSION_TTL = 8 * 3600

_PBKDF2_ITERATIONS = 200_000
_LOGIN_MAX_FAILS = 5
_LOGIN_LOCK_SECONDS = 60


# ------------------------------------------------------------------ 策略
def policy() -> Dict[str, Any]:
    """从 config.json 的 kids 段读取过滤策略（缺省用标准档）。"""
    k = (config.load().get("kids") or {})

    def _list(key: str, default: List[str]) -> List[str]:
        v = k.get(key, default)
        if not isinstance(v, list):
            return list(default)
        return [str(x).strip() for x in v if str(x).strip()]

    return {
        "enabled": bool(k.get("enabled", True)),
        "blocked_genres": _list("blocked_genres", DEFAULT_BLOCKED_GENRES),
        "blocked_certifications": _list("blocked_certifications",
                                        DEFAULT_BLOCKED_CERTS),
        "blocked_title_keywords": _list("blocked_title_keywords",
                                        DEFAULT_BLOCKED_TITLE_KEYWORDS),
        "block_adult_flag": bool(k.get("block_adult_flag", True)),
    }


def _norm_cert(cert: Any) -> str:
    return re.sub(r"\s+", "", str(cert or "")).upper()


def _genres_of(it: Dict[str, Any]) -> List[str]:
    g = it.get("genres")
    if isinstance(g, list):
        return [str(x).strip() for x in g if str(x).strip()]
    if isinstance(g, str) and g:
        try:
            arr = json.loads(g)
        except (json.JSONDecodeError, TypeError):
            return [g.strip()]
        if isinstance(arr, list):
            return [str(x).strip() for x in arr if str(x).strip()]
    return []


def reasons_of_brief(it: Dict[str, Any]) -> List[str]:
    """返回该条目在青少年模式被隐藏的原因；空列表 = 可见。

    判定顺序：手动屏蔽 > 手动放行 > 自动规则。
    """
    if it.get("blocked"):
        return ["已屏蔽"]
    if it.get("allowed"):
        return []
    pol = policy()
    reasons: List[str] = []
    if pol["block_adult_flag"] and it.get("adult"):
        reasons.append("成人内容")
    cert = _norm_cert(it.get("certification"))
    if cert:
        blocked_certs = {_norm_cert(c) for c in pol["blocked_certifications"]}
        if cert in blocked_certs:
            reasons.append("分级 %s" % cert)
    genres = _genres_of(it)
    blocked_genres = {str(g).strip() for g in pol["blocked_genres"]}
    for g in genres:
        if g in blocked_genres:
            reasons.append("类型：%s" % g)
    title = ("%s %s" % (it.get("title") or "", it.get("original_title") or "")).lower()
    for kw in pol["blocked_title_keywords"]:
        if str(kw).lower() in title:
            reasons.append("标题含「%s」" % kw)
            break
    return reasons


def is_hidden(it: Dict[str, Any]) -> bool:
    return bool(reasons_of_brief(it))


# ------------------------------------------------------------------ 密码
def _hash_password(password: str, salt: bytes, iterations: int) -> str:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return base64.b64encode(dk).decode("ascii")


def _store_password(password: str, is_default: bool) -> None:
    salt = secrets.token_bytes(16)
    payload = {
        "algo": "pbkdf2_sha256",
        "iterations": _PBKDF2_ITERATIONS,
        "salt": base64.b64encode(salt).decode("ascii"),
        "hash": _hash_password(password, salt, _PBKDF2_ITERATIONS),
    }
    db.set_kv(KV_PASSWORD, json.dumps(payload))
    db.set_kv(KV_PASSWORD_DEFAULT, "1" if is_default else "0")


def ensure_password() -> None:
    """首次运行生成初始密码的哈希（明文不入库、不入配置文件）。"""
    if not db.get_kv(KV_PASSWORD):
        _store_password(DEFAULT_PASSWORD, True)


def password_is_default() -> bool:
    return db.get_kv(KV_PASSWORD_DEFAULT, "1") == "1"


def _verify(password: str) -> bool:
    raw = db.get_kv(KV_PASSWORD)
    if not raw:
        return False
    try:
        p = json.loads(raw)
        salt = base64.b64decode(p.get("salt") or "")
        iterations = int(p.get("iterations") or _PBKDF2_ITERATIONS)
        expect = str(p.get("hash") or "")
    except (ValueError, TypeError, json.JSONDecodeError):
        return False
    got = _hash_password(password or "", salt, iterations)
    return hmac.compare_digest(got, expect)


def verify_login(username: str, password: str) -> bool:
    if str(username or "").strip() != DEFAULT_USERNAME:
        return False
    return _verify(password)


def change_password(old: str, new: str) -> Dict[str, Any]:
    if not _verify(old):
        return {"ok": False, "error": "bad_old", "message": "原密码不正确"}
    if len(new or "") < 6:
        return {"ok": False, "error": "too_short",
                "message": "新密码至少 6 位"}
    if new == DEFAULT_PASSWORD:
        return {"ok": False, "error": "weak",
                "message": "新密码不能与初始密码相同"}
    _store_password(new, False)
    return {"ok": True}


# ------------------------------------------------------------------ 登录限流
_login_lock = threading.Lock()
_login_fails: Dict[str, List[float]] = {}  # ip -> [失败次数, 解锁时间]


def login_locked(ip: str) -> int:
    """返回还需等待的秒数；0 表示可登录。"""
    with _login_lock:
        rec = _login_fails.get(str(ip))
        if not rec:
            return 0
        left = rec[1] - time.time()
        if left > 0:
            return int(left) + 1
        _login_fails.pop(str(ip), None)
        return 0


def record_login_failure(ip: str) -> None:
    with _login_lock:
        rec = _login_fails.setdefault(str(ip), [0, 0.0])
        rec[0] += 1
        if rec[0] >= _LOGIN_MAX_FAILS:
            rec[0] = 0
            rec[1] = time.time() + _LOGIN_LOCK_SECONDS


def clear_login_failures(ip: str) -> None:
    with _login_lock:
        _login_fails.pop(str(ip), None)


# ------------------------------------------------------------------ 会话
_sessions_lock = threading.Lock()
_sessions: Dict[str, float] = {}  # token -> 过期时间戳


def new_session() -> str:
    token = secrets.token_urlsafe(32)
    with _sessions_lock:
        now = time.time()
        for k, exp in list(_sessions.items()):
            if exp <= now:
                _sessions.pop(k, None)
        _sessions[token] = now + SESSION_TTL
    return token


def is_adult(token: Optional[str]) -> bool:
    if not token:
        return False
    with _sessions_lock:
        exp = _sessions.get(token)
        if not exp:
            return False
        if exp <= time.time():
            _sessions.pop(token, None)
            return False
        return True


def drop_session(token: Optional[str]) -> None:
    if not token:
        return
    with _sessions_lock:
        _sessions.pop(token, None)


def session_cookie(token: str) -> str:
    return ("%s=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d"
            % (COOKIE_NAME, token, SESSION_TTL))


def clear_cookie() -> str:
    return "%s=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0" % COOKIE_NAME


def token_from_cookie(cookie_header: Optional[str]) -> Optional[str]:
    for part in str(cookie_header or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == COOKIE_NAME and v:
            return v
    return None


def enabled() -> bool:
    return bool(policy().get("enabled", True))


def mode_of(cookie_header: Optional[str]) -> str:
    """当前模式：kid（默认）或 adult。"""
    if not enabled():
        return "adult"
    return "adult" if is_adult(token_from_cookie(cookie_header)) else "kid"
