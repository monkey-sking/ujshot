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
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime

from . import inspect as _inspect

MARK = "ujshot"          # our annotations carry source=<MARK> so re-runs can replace them
RELEVANT = ("open", "proposed", "approved", "rejected", "fixed")
VERSIONS_NAME = "versions.json"
UNCHANGED_DIFF_THRESHOLD = 0.25   # 忽略顶部状态栏后像素差 < 0.25% 视为画面未发生变化


def _log(msg: str) -> None:
    print(msg, flush=True)


def prepare(board_dir: str) -> str:
    os.makedirs(board_dir, exist_ok=True)
    return board_dir


def _round_sort_key(name: str):
    return (0, int(name), "") if re.fullmatch(r"\d+", name) else (1, 0, name)


def _latest_existing_shot(board_dir: str, image_name: str, appearance: str = "dark",
                          exclude_round: str = ""):
    """Find the most recent existing screenshot on the board for pixel comparison.

    Returns ``(path, build_label)`` or ``(None, "")``.
    """
    is_light = appearance == "light"
    after_base = os.path.join(board_dir, "after", "light") if is_light else os.path.join(board_dir, "after")
    if os.path.isdir(after_base):
        subdirs = [
            d for d in os.listdir(after_base)
            if os.path.isdir(os.path.join(after_base, d))
            and not d.startswith(".")
            and (is_light or d not in ("light", "dark"))
            and str(d) != str(exclude_round)
        ]
        for sub in reversed(sorted(subdirs, key=_round_sort_key)):
            cand = os.path.join(after_base, sub, image_name)
            if os.path.isfile(cand):
                return cand, sub
        if exclude_round != "":
            flat = os.path.join(after_base, image_name)
            if os.path.isfile(flat):
                return flat, ""
    before_base = os.path.join(board_dir, "light") if is_light else board_dir
    cand_before = os.path.join(before_base, image_name)
    if os.path.isfile(cand_before):
        return cand_before, ""
    return None, ""


def _screen_pixel_diff(path_a: str, path_b: str, crop_status_bar: bool = True):
    """Compare two screenshots, masking out the top 5.5% iOS status bar (clock/battery)."""
    try:
        from PIL import Image, ImageChops
    except ImportError:
        return None
    try:
        ia, ib = Image.open(path_a).convert("RGB"), Image.open(path_b).convert("RGB")
    except Exception:
        return None
    if ia.size != ib.size:
        return 100.0
    w, h = ia.size
    top = int(h * 0.055) if (crop_status_bar and h > 100) else 0
    ca, cb = ia.crop((0, top, w, h)), ib.crop((0, top, w, h))
    denom = ca.size[0] * ca.size[1]
    if denom <= 0:
        return 0.0
    hist = ImageChops.difference(ca, cb).convert("L").histogram()
    return round(sum(hist[8:]) / denom * 100, 2)


def _load_board_json(board_dir: str) -> dict:
    path = os.path.join(board_dir, "annotations.json")
    base = {"version": 1, "directory": os.path.basename(board_dir), "annotations": [], "screenChecks": {}}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("annotations"), list):
                if not isinstance(data.get("screenChecks"), dict):
                    data["screenChecks"] = {}
                base = data
        except (OSError, json.JSONDecodeError):
            pass
    return base


def _has_active_human_issue(annotations: list, image_name: str, appearance: str) -> bool:
    app = "light" if appearance == "light" else "dark"
    for a in annotations:
        if a.get("image") != image_name or (a.get("appearance") or "dark") != app:
            continue
        if a.get("source") == MARK:
            continue
        st = a.get("status") or "open"
        rounds = a.get("rounds")
        if isinstance(rounds, list) and rounds:
            st = rounds[-1].get("status") or st
        if st in ("open", "proposed", "approved", "rejected", "fixed", "discussed"):
            return True
    return False


def copy_shots(board_dir: str, outdir: str, shots, as_after=False,
               baseline_dir=None, before_map=None, round_build="",
               appearance: str = "dark", force_all: bool = False):
    """Copy this run's screenshots into the board layout.

    Both sides must share file names or the board cannot pair them, so the name
    is always the flow's shot id — never the original file name.

    When ``as_after=True`` and ``round_build`` is set (multi-round mode) and
    ``force_all=False``:
    - If a screen already exists in an earlier round/baseline, has no active
      human annotation, and its pixel diff (excluding the status bar clock) is
      below ``UNCHANGED_DIFF_THRESHOLD`` (0.25%), we **skip copying** the
      redundant identical screenshot so ``after/<build>/`` only stores screens
      that actually changed or had issues fixed.
    - If a screen was previously marked ``status="ok"`` (or all its previous
      human annotations were already ``accepted``) and its pixel diff is
      ``>= UNCHANGED_DIFF_THRESHOLD``, we copy the new screenshot and transition
      its ``screenChecks`` entry to ``status="updated"`` ("界面有更新") so the
      reviewer is alerted to re-check it.
    """
    is_light = appearance == "light"
    app = "light" if is_light else "dark"
    if as_after:
        dest = os.path.join(board_dir, "after", "light") if is_light else os.path.join(board_dir, "after")
        if round_build:
            dest = os.path.join(dest, round_build)
    else:
        dest = os.path.join(board_dir, "light") if is_light else board_dir
    os.makedirs(dest, exist_ok=True)

    board_data = _load_board_json(board_dir)
    anns = board_data.get("annotations", [])
    screen_checks = dict(board_data.get("screenChecks") or {})
    checks_dirty = False
    meta_dir = _inspect.meta_dir_for(
        board_dir, appearance=app, as_after=as_after, round_build=round_build
    )

    placed, seeded, skipped_unchanged, marked_updated = [], [], [], []
    for s in shots:
        img_name = f"{s.id}.png"
        src = os.path.join(outdir, "screens", img_name)
        if not os.path.exists(src):
            continue

        if as_after and round_build and not force_all:
            prev_path, prev_build = _latest_existing_shot(
                board_dir, img_name, appearance=app, exclude_round=round_build
            )
            if prev_path:
                diff = _screen_pixel_diff(prev_path, src)
                has_active = _has_active_human_issue(anns, img_name, app)
                check_key = f"{app}:{img_name}"
                cur_check = screen_checks.get(check_key) or {}
                cur_check_status = cur_check.get("status")
                human_anns = [
                    a for a in anns
                    if a.get("image") == img_name
                    and (a.get("appearance") or "dark") == app
                    and a.get("source") != MARK
                ]
                was_settled_ok = (cur_check_status == "ok") or (
                    bool(human_anns) and not has_active
                )

                if diff is not None and diff < UNCHANGED_DIFF_THRESHOLD and not has_active:
                    # 画面没动且没有待处理/待验收批注 → 不再重复存新截图
                    skipped_unchanged.append(img_name)
                    continue

                if diff is not None and diff >= UNCHANGED_DIFF_THRESHOLD and was_settled_ok and not has_active:
                    # 之前已经确认「界面没问题」或全部通过，但其他改动连带影响了该界面 → 自动标为「界面有更新」
                    screen_checks[check_key] = {
                        "status": "updated",
                        "build": str(round_build),
                        "prevBuild": str(cur_check.get("build") or prev_build or ""),
                        "diffPct": diff,
                        "updatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
                    }
                    checks_dirty = True
                    marked_updated.append(f"{img_name} ({diff}%)")

        shutil.copy(src, os.path.join(dest, img_name))
        placed.append(img_name)

        ax_src = os.path.join(outdir, "trees", f"{s.id}.json")
        ocr_src = os.path.join(outdir, "ocr", f"{s.id}.json")
        if os.path.isfile(ax_src) or os.path.isfile(ocr_src):
            meta_dir.mkdir(parents=True, exist_ok=True)
            if os.path.isfile(ax_src):
                shutil.copy(ax_src, str(meta_dir / f"{s.id}.ax.json"))
            if os.path.isfile(ocr_src):
                shutil.copy(ocr_src, str(meta_dir / f"{s.id}.ocr.json"))

    if checks_dirty:
        board_data["screenChecks"] = screen_checks
        board_data["updatedAt"] = datetime.now().astimezone().isoformat(timespec="seconds")
        ann_path = os.path.join(board_dir, "annotations.json")
        with open(ann_path, "w", encoding="utf-8") as f:
            json.dump(board_data, f, ensure_ascii=False, indent=2)
            f.write("\n")

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
    copy_shots.last_skipped = skipped_unchanged  # type: ignore[attr-defined]
    copy_shots.last_updated = marked_updated     # type: ignore[attr-defined]
    return placed, seeded


def write_annotations(board_dir: str, shots, as_after=False, when: str = "",
                      appearance: str = "dark", round_build: str = "") -> int:
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

    shot_images = {f"{s.id}.png" for s in shots}
    kept = [
        a for a in base.get("annotations", [])
        if not (
            a.get("source") == MARK
            and (a.get("appearance") or "dark") == app
            and a.get("image") in shot_images
        )
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
        entry = {
            "id": f"AI-{s.id}" if app == "dark" else f"AI-{s.id}-light",
            "image": f"{s.id}.png", "target": target,
            "appearance": app,
            "author": "ai", "kind": "pin",
            "x": 0.5, "y": 0.06, "w": 0, "h": 0,
            "text": text, "reply": "", "status": status,
            "createdAt": when or datetime.now().astimezone().isoformat(timespec="seconds"),
            "source": MARK,
        }
        if round_build:
            entry["raisedIn"] = str(round_build)
        made.append(entry)

    payload = {
        "version": base.get("version", 1),
        "directory": os.path.basename(board_dir),
        "updatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "annotations": kept + made,
        "screenChecks": base.get("screenChecks", {}),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return len(made)


def read(board_dir: str, statuses=RELEVANT, with_inspect: bool = False):
    """Read back what a human left open/rejected, so an agent can act on it."""
    path = os.path.join(board_dir, "annotations.json")
    if not os.path.exists(path):
        raise FileNotFoundError(f"没有 annotations.json：{path}")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if with_inspect:
        from . import dispatcher as _dispatcher
        from pathlib import Path
    rows = []
    for a in data.get("annotations", []):
        if statuses and a.get("status") not in statuses:
            continue
        item = {
            "id": a.get("id"), "image": a.get("image"),
            "target": a.get("target", "before"),
            "appearance": a.get("appearance", "dark"),
            "author": a.get("author"),
            "kind": a.get("kind"), "status": a.get("status"),
            "text": a.get("text", ""),
            "aiReply": a.get("aiReply", ""),
            "rejectReason": a.get("rejectReason", ""),
            # 必须在剥离 aiReply 之前算，否则下游拿到的是空值
            "fixedIn": fixed_in(a),
            "box": {"x": a.get("x"), "y": a.get("y"), "w": a.get("w"), "h": a.get("h")},
        }
        if a.get("elementHint"):
            item["elementHint"] = a["elementHint"]
        if with_inspect:
            mode = "light" if (a.get("appearance") or "dark") == "light" else "dark"
            rb = str(a.get("raisedIn") or "")
            insp = _inspect.inspect_annotation(board_dir, a, appearance=mode, build=rb)
            if insp.get("summary"):
                item["elementHint"] = insp["summary"]
            item["inspection"] = {
                "ax": insp.get("ax"),
                "ocr": insp.get("ocr"),
                "normFrame": insp.get("normFrame"),
            }
            shot = _dispatcher.resolve_screenshot_path(
                Path(board_dir), str(a.get("image") or ""), mode, rb
            )
            item["screenshotPath"] = str(shot)
        rows.append(item)
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
            raw = json.load(f)
        anns = raw.get("annotations", [])
        checks = raw.get("screenChecks", {})
        body = json.dumps({"annotations": anns, "screenChecks": checks}, ensure_ascii=False).encode("utf-8")
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
        rounds = [
            {"build": r.get("build", ""), "images": list(r.get("images") or [])}
            for r in (data.get("afterRounds") or [])
        ]
        if not rounds and data.get("afterImages"):
            rounds = [{"build": "", "images": list(data.get("afterImages") or [])}]
        light_rounds = [
            {"build": r.get("build", ""), "images": list(r.get("images") or [])}
            for r in (data.get("lightAfterRounds") or [])
        ]
        if not light_rounds and data.get("lightAfterImages"):
            light_rounds = [{"build": "", "images": list(data.get("lightAfterImages") or [])}]
        return {
            "dir": data.get("dir", ""),
            "images": list(data.get("images") or []),
            "lightImages": list(data.get("lightImages") or []),
            "after": list(data.get("afterImages") or []),
            "lightAfter": list(data.get("lightAfterImages") or []),
            "rounds": rounds,
            "lightRounds": light_rounds,
            "annotations": list(data.get("annotations") or []),
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

    def _collect_rounds(rel_base, skip=()):
        out_rounds = []
        base_path = os.path.join(src_dir, rel_base)
        flat = _imgs(rel_base)
        if flat:
            out_rounds.append({"build": "", "images": flat})
        if os.path.isdir(base_path):
            for name in sorted(os.listdir(base_path)):
                sub = os.path.join(base_path, name)
                if os.path.isdir(sub) and not name.startswith(".") and name not in skip:
                    imgs = _imgs(os.path.join(rel_base, name))
                    if imgs:
                        out_rounds.append({"build": name, "images": imgs})
        return out_rounds

    rounds = _collect_rounds("after", skip=("light", "dark"))
    light_rounds = _collect_rounds(os.path.join("after", "light"))

    def _union_names(rlist):
        seen = []
        for r in rlist:
            for n in r["images"]:
                if n not in seen:
                    seen.append(n)
        return seen

    return {
        "dir": os.path.abspath(src_dir),
        "images": _imgs(),
        "lightImages": _imgs("light"),
        "after": _union_names(rounds),
        "lightAfter": _union_names(light_rounds),
        "rounds": rounds,
        "lightRounds": light_rounds,
        "annotations": anns,
    }


def _copy_round_image(src_dir, src_url, name, build, dst_dir, appearance: str = "dark"):
    is_light = appearance == "light"
    base_parts = ["after", "light"] if is_light else ["after"]
    if build:
        base_parts.append(build)
    dst = os.path.join(dst_dir, *base_parts, name)
    if os.path.exists(dst):
        return False
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if src_url:
        app_seg = "light/" if is_light else ""
        build_seg = (urllib.parse.quote(build) + "/") if build else ""
        prefix = f"/after/{app_seg}{build_seg}"
        try:
            data = _http_get(src_url.rstrip("/") + prefix + urllib.parse.quote(name))
        except Exception:
            return False
        with open(dst, "wb") as f:
            f.write(data)
        return True
    src = os.path.join(src_dir, *base_parts, name)
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
        if sub == "light":
            prefix = "/images/light/"
        elif sub:
            prefix = "/after/"
        else:
            prefix = "/images/"
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
    stat = {"images": 0, "after": 0, "lightImages": 0, "lightAfter": 0,
            "added": 0, "renamed": 0, "duplicate": 0}
    disk_src = src["dir"] if not src_url else ""

    for name in src["images"]:
        stat["images"] += 1 if _copy_image(disk_src, src_url, name, "", dst_dir) else 0
    for name in src.get("lightImages") or []:
        if _copy_image(disk_src, src_url, name, "light", dst_dir):
            stat["images"] += 1
            stat["lightImages"] += 1
    for r in src.get("rounds") or []:
        for name in r["images"]:
            stat["after"] += 1 if _copy_round_image(
                disk_src, src_url, name, r["build"], dst_dir, appearance="dark") else 0
    for r in src.get("lightRounds") or []:
        for name in r["images"]:
            if _copy_round_image(disk_src, src_url, name, r["build"], dst_dir, appearance="light"):
                stat["after"] += 1
                stat["lightAfter"] += 1

    if disk_src:
        src_meta = os.path.join(disk_src, ".meta")
        if os.path.isdir(src_meta):
            dst_meta = os.path.join(dst_dir, ".meta")
            for root_dir, _, files in os.walk(src_meta):
                rel_dir = os.path.relpath(root_dir, src_meta)
                for fname in files:
                    if not fname.endswith(".json"):
                        continue
                    src_file = os.path.join(root_dir, fname)
                    dst_file = os.path.join(dst_meta, rel_dir, fname) if rel_dir != "." else os.path.join(dst_meta, fname)
                    if not os.path.exists(dst_file):
                        os.makedirs(os.path.dirname(dst_file), exist_ok=True)
                        shutil.copy2(src_file, dst_file)

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

    def _ann_key(entry):
        return (
            entry.get("image"),
            entry.get("appearance") or "dark",
            entry.get("text"),
            entry.get("x"),
            entry.get("y"),
        )

    existing = base["annotations"]
    by_id = {a.get("id"): a for a in existing}
    seen_text = {_ann_key(a) for a in existing}
    tag = os.path.basename(src["dir"].rstrip("/")) or "imported"

    for a in src["annotations"]:
        key = _ann_key(a)
        if key in seen_text:
            if prefer != "src":
                stat["duplicate"] += 1
                continue
            existing = [b for b in existing if _ann_key(b) != key]
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
