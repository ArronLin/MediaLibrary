# -*- coding: utf-8 -*-
"""构建绿色安装包。

产物：dist/MediaLibrary-setup.zip
内含：app + web + runtime(内嵌 Python) + run.bat + install.bat + app.ico
      + config.example.json + README.md
绝不打包 data/（用户数据不进安装包）；
绝不打包 config.json（个人配置含 API Key 与内网路径，只带 example 模板）。

用法：
    python tools/build_installer.py            # 打包
    python tools/build_installer.py --runtime  # 缺 runtime 时自动下载内嵌 Python
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
STAGE_NAME = "MediaLibrary"
RUNTIME_ZIP_URL = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip"

COPY_DIRS = ("app", "web")
COPY_FILES = ("run.bat", "install.bat", "app.ico", "config.example.json", "README.md")


def ensure_runtime(download: bool) -> None:
    rt = ROOT / "runtime" / "python.exe"
    if rt.is_file():
        return
    if not download:
        sys.exit("[ERROR] runtime\\python.exe 不存在。"
                 "先运行 --runtime 下载内嵌 Python，或在 run.bat 里改用系统 Python。")
    import urllib.request
    tmp = ROOT / "data" / "_python_embed.zip"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    print("下载内嵌 Python:", RUNTIME_ZIP_URL)
    urllib.request.urlretrieve(RUNTIME_ZIP_URL, tmp)
    (ROOT / "runtime").mkdir(exist_ok=True)
    with zipfile.ZipFile(tmp) as zf:
        zf.extractall(ROOT / "runtime")
    tmp.unlink()
    _pth = ROOT / "runtime" / "python312._pth"
    content = _pth.read_text()
    if ".." not in content.splitlines():
        _pth.write_text(content.replace(".", ".\n..", 1))
    print("内嵌 Python 就绪（python312._pth 已追加 '..'）")


def stage() -> Path:
    if DIST.exists():
        shutil.rmtree(DIST)
    pkg = DIST / STAGE_NAME
    pkg.mkdir(parents=True)
    for d in COPY_DIRS:
        shutil.copytree(ROOT / d, pkg / d,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for f in COPY_FILES:
        src = ROOT / f
        if src.is_file():
            shutil.copy2(src, pkg / f)
        else:
            print(f"  [警告] 缺少 {f}，跳过")
    # runtime 单独复制（体积大）
    shutil.copytree(ROOT / "runtime", pkg / "runtime",
                    ignore=shutil.ignore_patterns("__pycache__"))
    return pkg


def make_zip(pkg: Path) -> Path:
    zpath = DIST / "MediaLibrary-setup.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        # zip 内顶层带 MediaLibrary/ 目录，解压即得
        for p in pkg.rglob("*"):
            zf.write(p, str(Path(STAGE_NAME) / p.relative_to(pkg)))
    return zpath


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runtime", action="store_true",
                    help="runtime 不存在时自动下载内嵌 Python")
    args = ap.parse_args()

    ensure_runtime(args.runtime)

    # 完整性自检
    import subprocess
    r = subprocess.run([str(ROOT / "runtime" / "python.exe"), "-c",
                        "import app.server"], cwd=ROOT,
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit("[ERROR] 内嵌 Python 无法导入 app.server:\n" + r.stderr)

    print("组装安装目录 ...")
    pkg = stage()
    zpath = make_zip(pkg)
    mb = zpath.stat().st_size / 1048576
    print(f"完成: {zpath}  ({mb:.1f} MB)")
    print(f"安装目录副本: {pkg}")


if __name__ == "__main__":
    main()
