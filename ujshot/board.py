"""Optional bridge to an external screenshot review board (human annotation UI).

ujshot's own gallery is read-only on purpose. When a person needs to *draw boxes
on the screenshot* and hand the findings back to an agent, that is a different
tool. This module speaks the small, documented contract those boards share:

    <board-dir>/                 # "before" images
    <board-dir>/after/           # "after" images, same file names
    <board-dir>/annotations.json # {"version":1,"directory":…,"annotations":[…]}

An annotation is::

    {"id","image","target":"before"|"after","author":"user"|"ai",
     "kind":"box"|"pin","x","y","w","h",      # normalised 0…1
     "text","reply","status":"open"|"approved"|"fixed"|"accepted"|"rejected"|"ignored",
     "createdAt","aiReply","rejectReason"}

Nothing here requires a specific board implementation: ``--board-script`` points
at any program that serves that directory layout and accepts the same JSON.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime

MARK = "ujshot"          # our annotations carry source=<MARK> so re-runs can replace them
RELEVANT = ("open", "rejected", "fixed")


def _log(msg: str) -> None:
    print(msg, flush=True)


def prepare(board_dir: str) -> str:
    os.makedirs(board_dir, exist_ok=True)
    return board_dir


def copy_shots(board_dir: str, outdir: str, shots, as_after=False,
               baseline_dir=None, before_map=None):
    """Copy this run's screenshots into the board layout.

    Both sides must share file names or the board cannot pair them, so the name
    is always the flow's shot id — never the original file name.
    """
    dest = os.path.join(board_dir, "after") if as_after else board_dir
    os.makedirs(dest, exist_ok=True)
    placed, seeded = [], []
    for s in shots:
        src = os.path.join(outdir, "screens", f"{s.id}.png")
        if not os.path.exists(src):
            continue
        shutil.copy(src, os.path.join(dest, f"{s.id}.png"))
        placed.append(f"{s.id}.png")

    if baseline_dir and os.path.isdir(baseline_dir):
        mapping = before_map or {}
        for s in shots:
            src_name = mapping.get(s.id, f"{s.id}.png")
            src = os.path.join(baseline_dir, src_name)
            dst = os.path.join(board_dir, f"{s.id}.png")
            if os.path.exists(src) and not os.path.exists(dst):
                shutil.copy(src, dst)
                seeded.append(f"{s.id}.png ← {src_name}")
    return placed, seeded


def write_annotations(board_dir: str, shots, as_after=False, when: str = "") -> int:
    """Turn verdicts into board annotations. Never touches a human's entries."""
    target = "after" if as_after else "before"
    path = os.path.join(board_dir, "annotations.json")
    base = {"version": 1, "directory": os.path.basename(board_dir), "annotations": []}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data.get("annotations"), list):
                base = data
        except (OSError, json.JSONDecodeError):
            pass

    kept = [a for a in base.get("annotations", []) if a.get("source") != MARK]
    made = []
    for s in shots:
        expect = "、".join(s.expect)
        absent = "、".join(s.expect_absent)
        if s.verdict == "ok":
            text = "自动核对通过"
            if expect:
                text += f"：找到「{expect}」"
            if absent:
                text += f"；未出现禁止词（{absent}）"
            status = "accepted"
        else:
            text = f"自动判定 {s.verdict}"
            if s.verdict == "expect-missing":
                text += f"：找不到「{expect}」"
            elif s.verdict == "unexpected-text":
                text += f"：出现禁止词（{absent}）"
            elif s.verdict == "no-visible-change":
                text += "：与上一屏像素差 < 0.5%，点击可能无反应"
            elif s.verdict == "ocr-unavailable":
                text += "：OCR 不可用，文字断言未执行（不算通过）"
            text += f"；OCR：{(s.text or '')[:120]}"
            status = "open"
        made.append({
            "id": f"AI-{s.id}", "image": f"{s.id}.png", "target": target,
            "author": "ai", "kind": "pin",
            "x": 0.5, "y": 0.06, "w": 0, "h": 0,
            "text": text, "reply": "", "status": status,
            "createdAt": when or datetime.now().astimezone().isoformat(timespec="seconds"),
            "source": MARK,
        })

    payload = {
        "version": base.get("version", 1),
        "directory": os.path.basename(board_dir),
        "updatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "annotations": kept + made,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return len(made)


def read(board_dir: str, statuses=RELEVANT):
    """Read back what a human left open/rejected, so an agent can act on it."""
    path = os.path.join(board_dir, "annotations.json")
    if not os.path.exists(path):
        raise FileNotFoundError(f"没有 annotations.json：{path}")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    rows = []
    for a in data.get("annotations", []):
        if statuses and a.get("status") not in statuses:
            continue
        rows.append({
            "id": a.get("id"), "image": a.get("image"),
            "target": a.get("target", "before"), "author": a.get("author"),
            "kind": a.get("kind"), "status": a.get("status"),
            "text": a.get("text", ""), "rejectReason": a.get("rejectReason", ""),
            "box": {"x": a.get("x"), "y": a.get("y"), "w": a.get("w"), "h": a.get("h")},
        })
    return rows


def _free_port(preferred: int) -> int:
    for port in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"没有空闲端口（从 {preferred} 起试了 20 个）")


def serve(board_dir: str, script: str, port: int = 8766, open_browser=True, udid=None):
    """Launch an external board script for this directory. Returns its URL."""
    if not script:
        raise RuntimeError("没有配置 review board 脚本（ujshot.toml 的 [board] script 或 --board-script）")
    if not os.path.exists(script):
        raise RuntimeError(f"review board 脚本不存在：{script}")
    free = _free_port(port)
    cmd = [sys.executable, script, board_dir, "--port", str(free)]
    if udid:
        cmd += ["--sim-udid", udid]
    if not open_browser:
        cmd += ["--no-open"]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    time.sleep(1.2)
    url = f"http://127.0.0.1:{free}"
    try:
        with open(os.path.join(board_dir, ".board-url"), "w") as f:
            f.write(url + "\n")
    except OSError:
        pass
    return url


def known_url(board_dir: str):
    """A board started earlier for this directory, if it is still answering."""
    path = os.path.join(board_dir, ".board-url")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            url = f.read().strip()
        if not url:
            return None
        with urllib.request.urlopen(url.rstrip("/") + "/api/state", timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if os.path.realpath(data.get("dir", "")) == os.path.realpath(board_dir):
            return url
    except Exception:
        pass
    return None


def push_annotations(url: str, board_dir: str) -> bool:
    """POST the file we just wrote so a live board repaints without a restart."""
    if not url:
        return False
    try:
        with open(os.path.join(board_dir, "annotations.json"), encoding="utf-8") as f:
            anns = json.load(f).get("annotations", [])
        body = json.dumps({"annotations": anns}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url.rstrip("/") + "/api/annotations", data=body,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        with urllib.request.urlopen(req, timeout=8) as resp:
            return resp.status == 200
    except Exception:
        return False
