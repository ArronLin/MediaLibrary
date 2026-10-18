"""播放器探测与调起。

要点（Windows）：
- 列表传参 + shell=False，绝不手工加引号、绝不用 cmd /c start（UNC 会被 cmd 解析）
- DETACHED_PROCESS 避免弹黑窗、不阻塞后端
"""
from __future__ import annotations

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

_PLAYER_NAMES = ("xmp.exe", "PotPlayer64.exe", "PotPlayerMini64.exe",
                 "vlc.exe", "mpv.exe")

_COMMON_DIRS = [
    os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                 "Thunder Network", "Xmp", "program"),
    os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                 "Thunder Network", "Xmp", "program"),
    os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                 "Thunder Network", "Thunder", "Program"),
    os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                 "DAUM", "PotPlayer"),
    os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                 "DAUM", "PotPlayer"),
    os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "VideoLAN", "VLC"),
    os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                 "VideoLAN", "VLC"),
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "PotPlayer"),
]

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


def detect(cfg: Optional[Dict[str, Any]] = None) -> List[str]:
    """返回按优先级排列的可用播放器路径。

    注册表枚举开销大且 /api/health、/api/config 每次都会触发，
    因此按 chain 配置做 60s TTL 缓存。
    """
    chain = tuple(((cfg or {}).get("player") or {}).get("chain") or [])
    now = time.monotonic()
    with _detect_lock:
        if (_detect_cache["key"] == chain
                and now - _detect_cache["at"] < 60.0):
            return list(_detect_cache["result"])
    found: List[str] = []
    for p in chain:
        p = os.path.expandvars(os.path.expanduser(str(p)))
        if os.path.isfile(p) and p not in found:
            found.append(p)
    for d in _COMMON_DIRS:
        if not d or not os.path.isdir(d):
            continue
        for name in _PLAYER_NAMES:
            p = os.path.join(d, name)
            if os.path.isfile(p) and p not in found:
                found.append(p)
    # 注册表兜底（仅 Windows）
    if sys.platform == "win32":
        for p in _from_registry():
            if os.path.isfile(p) and p not in found:
                found.append(p)
    with _detect_lock:
        _detect_cache.update(key=chain, at=now, result=list(found))
    return found


def _from_registry() -> List[str]:
    out = []
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
                            for name in _PLAYER_NAMES:
                                for sub2 in ("", "program", "Program", "App"):
                                    cand = os.path.join(loc, sub2, name)
                                    if os.path.isfile(cand):
                                        out.append(cand)
                    except OSError:
                        continue
        except OSError:
            continue
    return out


def play(abs_path: str, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """调起播放器；返回 {ok, player} 或 {ok:False, error}。"""
    if not os.path.isfile(abs_path):
        return {"ok": False, "error": "file_not_found",
                "message": "文件不存在或共享盘未连接"}

    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    if sys.platform == "win32":
        flags |= CREATE_NO_WINDOW

    for exe in detect(cfg):
        try:
            subprocess.Popen(
                [exe, abs_path],
                cwd=os.path.dirname(exe),
                close_fds=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags if sys.platform == "win32" else 0,
            )
            return {"ok": True, "player": os.path.basename(exe)}
        except OSError:
            continue

    # 最后兜底：交给系统文件关联
    try:
        if sys.platform == "win32":
            os.startfile(abs_path)  # noqa: S606
        else:
            subprocess.Popen(["xdg-open", abs_path])
        return {"ok": True, "player": "system-default"}
    except OSError as exc:
        return {"ok": False, "error": "spawn_failed", "message": str(exc)}


def reveal(abs_path: str) -> bool:
    """在资源管理器中定位文件。"""
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(abs_path)])
            return True
    except OSError:
        pass
    return False
