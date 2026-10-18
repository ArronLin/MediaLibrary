"""播放器探测与调起。

要点（Windows）：
- 列表传参 + shell=False，绝不手工加引号、绝不用 cmd /c start（UNC 会被 cmd 解析）
- DETACHED_PROCESS 避免弹黑窗、不阻塞后端

用户体验模型：
- 用户只关心「本机装了哪些播放器」并选一个默认；其余已装播放器按
  注册表顺序作为兜底。默认播放器调起失败（进程 2 秒内非零退出）时
  自动换下一个，全部失败才报错。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000

_PF = os.environ.get("ProgramFiles", r"C:\Program Files")
_PF86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
_LAD = os.environ.get("LOCALAPPDATA", "")

# 已知播放器注册表（顺序即兜底顺序）
KNOWN_PLAYERS = (
    {"key": "xmp", "name": "迅雷影音", "exe": ("xmp.exe",)},
    {"key": "potplayer", "name": "PotPlayer",
     "exe": ("PotPlayer64.exe", "PotPlayerMini64.exe")},
    {"key": "vlc", "name": "VLC", "exe": ("vlc.exe",)},
    {"key": "mpv", "name": "mpv", "exe": ("mpv.exe",)},
    {"key": "mpc", "name": "MPC-HC", "exe": ("mpc-hc64.exe", "mpc-hc.exe")},
    {"key": "wmplayer", "name": "Windows Media Player", "exe": ("wmplayer.exe",)},
)

_PLAYER_DIRS: Dict[str, tuple] = {
    "xmp": (os.path.join(_PF86, "Thunder Network", "Xmp", "program"),
            os.path.join(_PF, "Thunder Network", "Xmp", "program"),
            os.path.join(_PF86, "Thunder Network", "Thunder", "Program")),
    "potplayer": (os.path.join(_PF, "DAUM", "PotPlayer"),
                  os.path.join(_PF86, "DAUM", "PotPlayer"),
                  os.path.join(_LAD, "Programs", "PotPlayer")),
    "vlc": (os.path.join(_PF, "VideoLAN", "VLC"),
            os.path.join(_PF86, "VideoLAN", "VLC")),
    "mpv": (),
    "mpc": (os.path.join(_PF, "MPC-HC"), os.path.join(_PF86, "MPC-HC")),
    "wmplayer": (os.path.join(_PF86, "Windows Media Player"),
                 os.path.join(_PF, "Windows Media Player")),
}

# 注册表扫描用的 全部 exe 名 → key 映射
_EXE2KEY: Dict[str, str] = {
    name: kp["key"] for kp in KNOWN_PLAYERS for name in kp["exe"]
}

_local_ips = None  # type: Optional[set]


def local_ips() -> set:
    global _local_ips
    if _local_ips is not None:
        return _local_ips
    ips = {"127.0.0.1", "::1", "localhost"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            ips.add(info[4][0])
    except OSError:
        pass
    _local_ips = ips
    return ips


def is_local(addr: str) -> bool:
    if not addr:
        return False
    a = addr.split("%")[0]
    return a in local_ips() or a.startswith("127.")


_detect_lock = threading.Lock()
_detect_cache: Dict[str, Any] = {"key": None, "at": 0.0, "result": []}


def _expand(p: str) -> str:
    return os.path.normpath(os.path.expandvars(os.path.expanduser(str(p))))


def _scan_registry() -> Dict[str, str]:
    """枚举卸载表 InstallLocation，返回 key → exe 路径。"""
    out: Dict[str, str] = {}
    if sys.platform != "win32":
        return out
    try:
        import winreg
    except ImportError:
        return out
    roots = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for hive, path in roots:
        try:
            with winreg.OpenKey(hive, path) as key:
                for i in range(winreg.QueryInfoKey(key)[0]):
                    try:
                        sub = winreg.EnumKey(key, i)
                        with winreg.OpenKey(key, sub) as sk:
                            loc = winreg.QueryValueEx(sk, "InstallLocation")[0]
                            if not loc:
                                continue
                            for name, k in _EXE2KEY.items():
                                if k in out:
                                    continue
                                for sub2 in ("", "program", "Program", "App"):
                                    cand = _expand(os.path.join(loc, sub2, name))
                                    if os.path.isfile(cand):
                                        out[k] = cand
                                        break
                    except OSError:
                        continue
        except OSError:
            continue
    return out


def _resolve(cfg: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """返回按优先级排列的候选列表 [{key, name, path}]。

    顺序：默认播放器（如已安装）→ 其余已知播放器按注册表顺序 →
    自定义/历史路径。自定义路径可覆盖同名已知播放器的探测结果。
    """
    p = (cfg or {}).get("player") or {}
    default = str(p.get("default") or "")
    custom = p.get("custom_paths") or {}
    legacy_chain = [str(x) for x in (p.get("chain") or []) if str(x).strip()]

    found: Dict[str, Dict[str, Any]] = {}

    def put(key: str, name: str, path: str, force: bool = False) -> None:
        if not path or not os.path.isfile(path):
            return
        if key in found and not force:
            return
        found[key] = {"key": key, "name": name, "path": path}

    reg = _scan_registry()
    for kp in KNOWN_PLAYERS:
        k = kp["key"]
        # 自定义路径优先；其次常见目录；最后注册表
        c = _expand(custom.get(k) or "")
        if c and os.path.isfile(c):
            put(k, kp["name"], c, force=True)
            continue
        hit = ""
        for d in _PLAYER_DIRS.get(k, ()):
            for name in kp["exe"]:
                cand = os.path.join(d, name)
                if os.path.isfile(cand):
                    hit = cand
                    break
            if hit:
                break
        if hit:
            put(k, kp["name"], hit)
        elif k in reg:
            put(k, kp["name"], reg[k])

    # 兼容旧配置：chain 里的路径，能认出 key 就补位，认不出就当自定义播放器
    for i, raw in enumerate(legacy_chain):
        path = _expand(raw)
        if not os.path.isfile(path):
            continue
        base = os.path.basename(path).lower()
        k = _EXE2KEY.get(base)
        if k:
            put(k, dict((x["key"], x["name"]) for x in KNOWN_PLAYERS).get(k, base), path)
        else:
            put(f"custom:{i}", os.path.basename(path), path)

    # 兜底链之外的自定义 key（如 "custom"）
    for k, v in custom.items():
        if k in found or k in ("custom",) or not isinstance(v, str):
            continue
        path = _expand(v)
        if os.path.isfile(path):
            put(k, os.path.basename(path), path)

    # 通用自定义路径（高级设置里的单条输入框）
    c = _expand(custom.get("custom") or "")
    if c and os.path.isfile(c):
        name = os.path.basename(c)
        base = name.lower()
        k = _EXE2KEY.get(base)
        if k and k in found:
            found[k]["path"] = c        # 用自定义路径顶掉自动探测结果
        elif k:
            kp_name = next(x["name"] for x in KNOWN_PLAYERS if x["key"] == k)
            put(k, kp_name, c)
        else:
            put("custom", name, c)

    cands = list(found.values())
    # 默认播放器置顶
    if default:
        cands.sort(key=lambda c: c["key"] != default)
    return cands


def detect(cfg: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """本机可用播放器列表（[{key, name, path}]），60s TTL 缓存。"""
    p = (cfg or {}).get("player") or {}
    cache_key = json.dumps(
        {"d": p.get("default") or "", "c": p.get("custom_paths") or {},
         "l": p.get("chain") or []},
        ensure_ascii=False, sort_keys=True)
    now = time.monotonic()
    with _detect_lock:
        if _detect_cache["key"] == cache_key and now - _detect_cache["at"] < 60.0:
            return list(_detect_cache["result"])
    result = _resolve(cfg)
    with _detect_lock:
        _detect_cache.update(key=cache_key, at=now, result=list(result))
    return result


def play(abs_path: str, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """调起播放器；默认播放器打不开就按顺序兜底。

    判定规则：进程 2 秒内非零退出 → 视为该播放器无法播放，换下一个；
    仍在运行或 0 退出（播放器交接给已有实例）→ 视为成功。
    全部失败返回 {ok: False, error: "all_players_failed"}。
    """
    if not os.path.isfile(abs_path):
        return {"ok": False, "error": "file_not_found",
                "message": "文件不存在或共享盘未连接"}

    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    if sys.platform == "win32":
        flags |= CREATE_NO_WINDOW

    cands = detect(cfg)
    if not cands:
        return {"ok": False, "error": "no_player",
                "message": "未检测到可用的视频播放器，请到「设置 → 播放器」检查"}

    tried: List[str] = []
    for i, c in enumerate(cands):
        try:
            proc = subprocess.Popen(
                [c["path"], abs_path],
                cwd=os.path.dirname(c["path"]),
                close_fds=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags if sys.platform == "win32" else 0,
            )
        except OSError as exc:
            tried.append(f'{c["name"]}(启动失败: {exc.strerror or exc})')
            continue
        try:
            code = proc.wait(timeout=2.0)
            if code != 0:
                tried.append(f'{c["name"]}(退出码 {code})')
                continue
        except subprocess.TimeoutExpired:
            code = None  # 仍在运行 = 播放窗口已弹出
        return {"ok": True, "key": c["key"], "player": c["name"],
                "fallback": c["key"] != cands[0]["key"], "tried": tried}

    return {"ok": False, "error": "all_players_failed",
            "message": "所有播放器均无法播放该文件：" + "、".join(tried)}


def reveal(abs_path: str) -> bool:
    """在资源管理器中定位文件。"""
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(abs_path)])
            return True
    except OSError:
        pass
    return False
