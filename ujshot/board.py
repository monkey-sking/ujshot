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
import urllib.parse
import urllib.request
from datetime import datetime

MARK = "ujshot"          # our annotations carry source=<MARK> so re-runs can replace them
RELEVANT = ("open", "rejected", "fixed")
VERSIONS_NAME = "versions.json"


def _log(msg: str) -> None:
    print(msg, flush=True)


def prepare(board_dir: str) -> str:
    os.makedirs(board_dir, exist_ok=True)
    return board_dir


def copy_shots(board_dir: str, outdir: str, shots, as_after=False,
               baseline_dir=None, before_map=None, round_build="",
               appearance: str = "dark"):
    """Copy this run's screenshots into the board layout.

    Both sides must share file names or the board cannot pair them, so the name
    is always the flow's shot id — never the original file name.

    ``round_build`` puts the after images in their own round directory
    (``after/234/``): the same issue fixed twice keeps both attempts instead of
    the second run overwriting the first.

    ``appearance`` separates dark (default root + ``after/<build>/``) from light
    (``light/`` + ``after/light/<build>/``) so dark and light screenshots never
    overwrite or mix with each other.
    """
    is_light = appearance == "light"
    if as_after:
        dest = os.path.join(board_dir, "after", "light") if is_light else os.path.join(board_dir, "after")
        if round_build:
            dest = os.path.join(dest, round_build)
    else:
        dest = os.path.join(board_dir, "light") if is_light else board_dir
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
        base_dest = os.path.join(board_dir, "light") if is_light else board_dir
        os.makedirs(base_dest, exist_ok=True)
        for s in shots:
            src_name = mapping.get(s.id, f"{s.id}.png")
            src = os.path.join(baseline_dir, src_name)
            dst = os.path.join(base_dest, f"{s.id}.png")
            if os.path.exists(src) and not os.path.exists(dst):
                shutil.copy(src, dst)
                seeded.append(f"{s.id}.png ← {src_name}")
    return placed, seeded


def write_annotations(board_dir: str, shots, as_after=False, when: str = "",
                      appearance: str = "dark") -> int:
    """Turn verdicts into board annotations. Never touches a human's entries."""
    target = "after" if as_after else "before"
    app = "light" if appearance == "light" else "dark"
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

    kept = [
        a for a in base.get("annotations", [])
        if not (a.get("source") == MARK and (a.get("appearance") or "dark") == app)
    ]
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
            "id": f"AI-{s.id}" if app == "dark" else f"AI-{s.id}-light",
            "image": f"{s.id}.png", "target": target,
            "appearance": app,
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
            "target": a.get("target", "before"),
            "appearance": a.get("appearance", "dark"),
            "author": a.get("author"),
            "kind": a.get("kind"), "status": a.get("status"),
            "text": a.get("text", ""), "rejectReason": a.get("rejectReason", ""),
            # 必须在剥离 aiReply 之前算，否则下游拿到的是空值
            "fixedIn": fixed_in(a),
            "box": {"x": a.get("x"), "y": a.get("y"), "w": a.get("w"), "h": a.get("h")},
        })
    return rows


BUILD_RE = __import__("re").compile(r"Build\s*(\d+)", __import__("re").I)


def fixed_in(annotation: dict) -> str:
    """Which build fixed this specific issue.

    Explicit ``fixedIn`` wins; otherwise scan ``aiReply`` for "Build 234：…",
    which is how the replies have been written so far. Returning "" is
    meaningful: it says nobody recorded it yet, which is exactly what a
    reviewer needs to see rather than a guessed number.
    """
    explicit = str(annotation.get("fixedIn") or "").strip()
    if explicit:
        digits = "".join(c for c in explicit if c.isdigit())
        return digits or explicit
    blob = f"{annotation.get('aiReply') or ''} {annotation.get('text') or ''}"
    m = BUILD_RE.search(blob)
    return m.group(1) if m else ""


def fix_builds(board_dir: str, image: str = ""):
    """{image: [build, …]} — one original screenshot often carries several
    issues fixed in different builds, so a single per-image version is a lie."""
    path = os.path.join(board_dir, "annotations.json")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        anns = json.load(f).get("annotations", [])
    out = {}
    for a in anns:
        if image and a.get("image") != image:
            continue
        b = fixed_in(a)
        if not b:
            continue
        out.setdefault(a.get("image", ""), set()).add(b)
    return {k: sorted(v, key=lambda x: (len(x), x)) for k, v in out.items()}


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


def _probe_service(port: int):
    """Is a board service already listening here? Returns its /api/boards payload."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/boards", timeout=1.2) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if isinstance(data, dict) and isinstance(data.get("boards"), list):
            return data
    except Exception:
        return None
    return None


def _register(port: int, board_dir: str):
    """Ask a running service to host this directory. Returns {"name","dir"} or None."""
    body = json.dumps({"dir": os.path.abspath(board_dir),
                       "name": os.path.basename(os.path.abspath(board_dir))}).encode("utf-8")
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/boards", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("board") if data.get("ok") else None
    except Exception:
        return None


def serve(board_dir: str, script: str, port: int = 8766, open_browser=True, udid=None):
    """Make sure a board service hosts *board_dir*; return this board's URL.

    One service, many boards: if something is already listening we register the
    directory with it instead of spawning a second process on a second port.
    Boards live under ``/b/<name>/`` so two boards never share image paths.
    """
    board_dir = os.path.abspath(board_dir)

    # 1) 已有服务 → 直接登记，不新起进程
    for p in range(port, port + 20):
        found = _probe_service(p)
        if found is None:
            continue
        entry = _register(p, board_dir)
        if entry:
            url = f"http://127.0.0.1:{p}/b/{urllib.parse.quote(entry['name'])}/"
            with open(os.path.join(board_dir, ".board-url"), "w") as f:
                f.write(url + "\n")
            return url
        raise RuntimeError(f"端口 {p} 上已有板子服务，但登记失败")

    # 2) 没有服务 → 起一个（内置评审板，不用项目里放脚本）
    free = _free_port(port)
    if script:
        if not os.path.exists(script):
            raise RuntimeError(f"review board 脚本不存在：{script}")
        cmd = [sys.executable, script, board_dir, "--port", str(free)]
    else:
        cmd = [sys.executable, "-m", "ujshot.reviewboard", board_dir, "--port", str(free)]
    if udid:
        cmd += ["--sim-udid", udid]
    if not open_browser:
        cmd += ["--no-open"]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    time.sleep(1.5)
    # 服务按板名挂前缀：读它自己写下的地址，别在这里猜
    url = f"http://127.0.0.1:{free}"
    path = os.path.join(board_dir, ".board-url")
    if os.path.exists(path):
        with open(path) as f:
            got = f.read().strip()
        if got:
            url = got
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


def write_versions(board_dir: str, side: str, label: str, image: str = "",
                   round_build: str = "") -> dict:
    """Record which build a batch of screenshots came from.

    Versions live beside the annotations, not inside them: clearing annotations
    must not erase "this was Build 237". ``image`` targets a single exception.

    With ``round_build`` the label is also filed under ``rounds`` — after images
    now live in ``after/<build>/``, so each round needs its own label.
    """
    path = os.path.join(board_dir, VERSIONS_NAME)
    data = {"before": "", "after": "",
            "overrides": {"before": {}, "after": {}}, "rounds": {}}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                data.update({k: raw.get(k, data[k]) for k in ("before", "after")})
                ov = raw.get("overrides") or {}
                if isinstance(ov, dict):
                    data["overrides"] = {
                        "before": dict(ov.get("before") or {}),
                        "after": dict(ov.get("after") or {}),
                    }
                rounds = raw.get("rounds") or {}
                if isinstance(rounds, dict):
                    data["rounds"] = {str(k): str(v) for k, v in rounds.items()}
        except (OSError, json.JSONDecodeError):
            pass
    if side not in ("before", "after"):
        raise ValueError("side 必须是 before 或 after")
    if image:
        data["overrides"].setdefault(side, {})[image] = label
    else:
        data[side] = label
    if round_build and side == "after":
        data["rounds"][round_build] = label
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return data


# --------------------------------------------------------------------------- 迁移

def _http_get(url: str, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read()


def snapshot(src_dir: str = "", src_url: str = ""):
    """Read a board's full state, from disk or from a *running* board.

    Reading through HTTP matters when the board has unsaved edits in the
    browser: ``/api/state`` returns what the page is actually showing, while
    ``annotations.json`` only has what was already persisted.
    """
    if src_url:
        raw = _http_get(src_url.rstrip("/") + "/api/state")
        data = json.loads(raw.decode("utf-8"))
        return {
            "dir": data.get("dir", ""),
            "images": data.get("images", []),
            "after": data.get("afterImages", []),
            "annotations": data.get("annotations", []),
        }
    if not src_dir:
        raise ValueError("需要 src_dir 或 src_url")
    path = os.path.join(src_dir, "annotations.json")
    anns = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            anns = json.load(f).get("annotations", [])

    def _imgs(sub=""):
        d = os.path.join(src_dir, sub) if sub else src_dir
        if not os.path.isdir(d):
            return []
        return sorted(n for n in os.listdir(d)
                      if os.path.isfile(os.path.join(d, n))
                      and n.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
                      and not n.startswith("."))

    rounds = []
    if src_url:
        for r in data.get("afterRounds") or []:
            rounds.append({"build": r.get("build", ""), "images": r.get("images") or []})
    else:
        after_dir = os.path.join(src_dir, "after")
        flat = _imgs("after")
        if flat:
            rounds.append({"build": "", "images": flat})
        if os.path.isdir(after_dir):
            for name in sorted(os.listdir(after_dir)):
                sub = os.path.join(after_dir, name)
                if os.path.isdir(sub) and not name.startswith(".") and name not in ("light", "dark"):
                    imgs = _imgs(os.path.join("after", name))
                    if imgs:
                        rounds.append({"build": name, "images": imgs})

    after_all = []
    for r in rounds:
        for n in r["images"]:
            if n not in after_all:
                after_all.append(n)
    return {"dir": os.path.abspath(src_dir), "images": _imgs(), "after": after_all,
            "rounds": rounds, "annotations": anns}


def _copy_round_image(src_dir, src_url, name, build, dst_dir):
    dst = os.path.join(dst_dir, "after", build, name) if build \
        else os.path.join(dst_dir, "after", name)
    if os.path.exists(dst):
        return False
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if src_url:
        prefix = "/after/" + (urllib.parse.quote(build) + "/" if build else "")
        try:
            data = _http_get(src_url.rstrip("/") + prefix + urllib.parse.quote(name))
        except Exception:
            return False
        with open(dst, "wb") as f:
            f.write(data)
        return True
    src = os.path.join(src_dir, "after", build, name) if build \
        else os.path.join(src_dir, "after", name)
    if not os.path.exists(src):
        return False
    shutil.copy2(src, dst)
    return True


def _copy_image(src_dir, src_url, name, sub, dst_dir):
    dst = os.path.join(dst_dir, sub, name) if sub else os.path.join(dst_dir, name)
    if os.path.exists(dst):
        return False
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if src_url:
        prefix = "/after/" if sub else "/images/"
        try:
            data = _http_get(src_url.rstrip("/") + prefix + urllib.parse.quote(name))
        except Exception:
            return False
        with open(dst, "wb") as f:
            f.write(data)
        return True
    src = os.path.join(src_dir, sub, name) if sub else os.path.join(src_dir, name)
    if not os.path.exists(src):
        return False
    shutil.copy2(src, dst)
    return True


def merge(src_dir: str, dst_dir: str, src_url: str = "", prefer: str = "skip"):
    """Pull another board's images + human annotations into *dst*.

    Annotation ids are human-friendly and therefore collide across boards
    (both boards start at U1). A colliding id whose content differs is renamed
    ``<id>@<source>`` so nothing is silently overwritten and the provenance
    stays visible in the UI.

    ``prefer`` decides what happens when the *same* image already has an
    annotation with the same text: ``skip`` (default) keeps the destination's,
    ``src`` replaces it.
    """
    src = snapshot(src_dir=src_dir, src_url=src_url)
    os.makedirs(dst_dir, exist_ok=True)
    stat = {"images": 0, "after": 0, "added": 0, "renamed": 0, "duplicate": 0}

    for name in src["images"]:
        stat["images"] += 1 if _copy_image(src["dir"] if not src_url else "", src_url, name, "", dst_dir) else 0
    for r in src.get("rounds") or []:
        for name in r["images"]:
            stat["after"] += 1 if _copy_round_image(
                src["dir"] if not src_url else "", src_url, name, r["build"], dst_dir) else 0

    path = os.path.join(dst_dir, "annotations.json")
    base = {"version": 1, "directory": os.path.basename(dst_dir), "annotations": []}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data.get("annotations"), list):
                base = data
        except (OSError, json.JSONDecodeError):
            pass

    existing = base["annotations"]
    by_id = {a.get("id"): a for a in existing}
    seen_text = {(a.get("image"), a.get("text"), a.get("x"), a.get("y")) for a in existing}
    tag = os.path.basename(src["dir"].rstrip("/")) or "imported"

    for a in src["annotations"]:
        key = (a.get("image"), a.get("text"), a.get("x"), a.get("y"))
        if key in seen_text:
            if prefer != "src":
                stat["duplicate"] += 1
                continue
            existing = [b for b in existing if (b.get("image"), b.get("text"),
                                                b.get("x"), b.get("y")) != key]
            for b in list(by_id):
                if by_id[b] not in existing:
                    by_id.pop(b, None)
        item = dict(a)
        item.setdefault("importedFrom", tag)
        if item.get("id") in by_id:
            item["id"] = f"{item.get('id')}@{tag}"
            stat["renamed"] += 1
        existing.append(item)
        by_id[item["id"]] = item
        seen_text.add(key)
        stat["added"] += 1

    payload = {
        "version": base.get("version", 1),
        "directory": os.path.basename(dst_dir),
        "updatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "annotations": existing,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    stat["source"] = src["dir"]
    return stat
