"""路径真相源：全项目所有文件路径都从这里派生。

禁止在其它模块里硬编码绝对路径 —— 这是"整个目录可拷贝到任意机器"的前提。
"""
from __future__ import annotations

from pathlib import Path

# app/paths.py -> app/ -> 项目根
BASE_DIR = Path(__file__).resolve().parent.parent
APP_DIR = BASE_DIR / "app"
WEB_DIR = BASE_DIR / "web"

DATA_DIR = BASE_DIR / "data"
CACHE_DIR = DATA_DIR / "cache"
POSTER_DIR = CACHE_DIR / "posters"
BACKDROP_DIR = CACHE_DIR / "backdrops"
LOG_DIR = DATA_DIR / "logs"

DB_PATH = DATA_DIR / "library.db"
CONFIG_PATH = BASE_DIR / "config.json"


def ensure_dirs() -> None:
    for d in (DATA_DIR, CACHE_DIR, POSTER_DIR, BACKDROP_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)


def normalize_root_key(root_path: str) -> str:
    """媒体库根目录的归一化键：大小写不敏感 + 去尾部斜杠。

    避免 `\\\\host\\share` 与 `\\\\host\\share\\` 被当成两个库。
    """
    import os

    p = os.path.abspath(root_path)
    p = p.replace("/", "\\") if os.name == "nt" else p
    p = p.rstrip("\\/")
    if not p:
        p = "\\"
    return os.path.normcase(p).lower()


def join_rel(root_path: str, rel_path: str) -> str:
    """把库根与相对路径拼成可直接播放的绝对路径（不落库）。"""
    import os

    rel = rel_path.replace("/", os.sep).replace("\\", os.sep)
    return os.path.join(root_path, rel)


def long_path(path: str) -> str:
    """Windows 超长路径前缀，仅用于文件系统操作，不要传给播放器。"""
    import os

    if os.name != "nt":
        return path
    if len(path) < 240:
        return path
    if path.startswith("\\\\?\\"):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path
