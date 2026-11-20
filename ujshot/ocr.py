"""OCR via macOS Vision.

Compiles the bundled Swift helper once into ``~/Library/Caches/ujshot/`` and
shells out to it. If Xcode's toolchain is missing, OCR silently turns itself
off — everything else keeps working, and assertions that depended on text are
reported as "ocr unavailable" instead of a false pass.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

from . import __version__

CACHE_HOME = os.environ.get("UJSHOT_CACHE", os.path.join(
    os.path.expanduser("~"), "Library", "Caches", "ujshot"))
BIN_NAME = "ujshot-ocr"
ASSET = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "ocr.swift")

_available = None      # None = not probed yet
_bin_path = None


def _swiftc() -> str:
    return shutil.which("swiftc") or "/usr/bin/swiftc"


def ensure(verbose: bool = True):
    """Compile the helper if needed. Returns the binary path or None."""
    global _available, _bin_path
    if _available is not None:
        return _bin_path
    os.makedirs(CACHE_HOME, exist_ok=True)
    target = os.path.join(CACHE_HOME, BIN_NAME)
    src_mtime = os.path.getmtime(ASSET) if os.path.exists(ASSET) else 0
    if os.path.exists(target) and os.path.getmtime(target) >= src_mtime:
        _available, _bin_path = True, target
        return target
    if not os.path.exists(ASSET):
        _available = False
        if verbose:
            sys.stderr.write("  ! OCR 资源缺失，跳过文字识别\n")
        return None
    if not os.path.exists(_swiftc()):
        _available = False
        if verbose:
            sys.stderr.write("  ! 没有 swiftc（装 Xcode 命令行工具），跳过文字识别：断言里的文字检查将不可用\n")
        return None
    r = subprocess.run([_swiftc(), "-O", ASSET, "-o", target],
                       capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        _available = False
        if verbose:
            sys.stderr.write(f"  ! OCR 编译失败，跳过文字识别：{r.stderr[-300:]}\n")
        return None
    _available, _bin_path = True, target
    return target


def available() -> bool:
    return ensure(verbose=False) is not None


def recognize(png: str):
    """Return {"file","w","h","lines":[{t,x,y,cx,cy}]} or None."""
    binary = ensure(verbose=False)
    if not binary:
        return None
    try:
        r = subprocess.run([binary, png], capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    for line in r.stdout.strip().splitlines():
        try:
            return json.loads(line)
        except Exception:
            continue
    return None


def text_of(info) -> str:
    if not info:
        return ""
    return " ".join(l.get("t", "") for l in info.get("lines", []))


def lines_of(info):
    """[(text, cx, cy)] normalised centres, ready for a tap."""
    if not info:
        return []
    return [(l.get("t", ""), l.get("cx", 0.5), l.get("cy", 0.5)) for l in info.get("lines", [])]


def version_note() -> str:
    binary = ensure(verbose=False)
    if binary:
        return "OCR: on"
    return "OCR: off（文字断言不可用）"
