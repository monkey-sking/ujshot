#!/usr/bin/env python3
"""Review board: annotate UI screenshots with a human, feed findings back to an agent.

One service hosts many boards under ``/b/<name>/``; the board list lives in the
project (``<repo>/.ujshot/boards.json``).

Usage:
    ujshot board --serve <image_dir>        # 或 ujshot-board <image_dir>
    ujshot-board --list                     # 看已登记的板子
"""

from __future__ import annotations

import argparse
import datetime
import json
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import urllib.parse
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from . import dispatcher as _dispatcher
except ImportError:  # pragma: no cover
    import dispatcher as _dispatcher  # type: ignore[no-redef]

try:
    from . import device as _device
except ImportError:  # pragma: no cover
    import device as _device  # type: ignore[no-redef]

try:
    from . import inspect as _inspect
except ImportError:  # pragma: no cover
    import inspect as _inspect  # type: ignore[no-redef]

try:
    from . import ocr as _ocr
except ImportError:  # pragma: no cover
    import ocr as _ocr  # type: ignore[no-redef]

from . import config

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


class SimulatorController:
    """Thin bridge to a booted iOS Simulator using xcrun simctl + AXe CLI."""

    def __init__(self, udid: Optional[str] = None, bundle_id: str = ""):
        # bundle id 不再写死：项目通过 --sim-bundle / ujshot.toml 的 [app] bundle_id 给
        self.bundle_id = bundle_id
        self.axe_bin = _device.find_axe()
        self.udid: Optional[str] = udid or self._detect_booted_udid()
        self.name: str = ""
        self.width: int = 402
        self.height: int = 874
        self._frame_lock = threading.Lock()
        self._input_lock = threading.Lock()
        if self.udid and self.axe_bin:
            self._load_display_info()

    @property
    def available(self) -> bool:
        return bool(self.udid and self.axe_bin)

    def _detect_booted_udid(self) -> Optional[str]:
        try:
            out = subprocess.check_output(
                ["xcrun", "simctl", "list", "devices", "booted", "-j"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=4,
            )
            data = json.loads(out)
            for devs in data.get("devices", {}).values():
                for d in devs:
                    if d.get("state") == "Booted" and d.get("udid"):
                        self.name = d.get("name", "iOS Simulator")
                        return d["udid"]
        except Exception:
            pass
        return None

    def _load_display_info(self) -> None:
        if not self.udid or not self.axe_bin:
            return
        try:
            out = subprocess.check_output(
                [self.axe_bin, "--output", "json", "displays", "--udid", self.udid],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            data = json.loads(out)
            res = data.get("data", {}).get("result", {})
            screen = res.get("activeScreen", {})
            if res.get("name"):
                self.name = res["name"]
            if screen.get("width"):
                self.width = int(screen["width"])
            if screen.get("height"):
                self.height = int(screen["height"])
        except Exception:
            pass

    def status(self) -> Dict[str, Any]:
        return {
            "available": self.available,
            "udid": self.udid,
            "name": self.name or "iOS Simulator",
            "width": self.width,
            "height": self.height,
            "axe": bool(self.axe_bin),
        }

    def capture_jpeg(self) -> Optional[bytes]:
        if not self.udid:
            return None
        tmp_path = Path(tempfile.gettempdir()) / f"ui-review-live-{self.udid}.jpg"
        with self._frame_lock:
            try:
                subprocess.run(
                    ["xcrun", "simctl", "io", self.udid, "screenshot", "--type=jpeg", str(tmp_path)],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=4,
                )
                return tmp_path.read_bytes()
            except Exception:
                return None

    def freeze_png(self, directory: Path, appearance: str = "dark") -> str:
        if not self.udid:
            raise RuntimeError("No booted simulator")
        target_dir = (directory / "light") if appearance == "light" else directory
        target_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%H%M%S")
        name = f"live-{stamp}.png"
        idx = 1
        while (target_dir / name).exists():
            name = f"live-{stamp}-{idx}.png"
            idx += 1
        target = target_dir / name
        with self._frame_lock:
            subprocess.run(
                ["xcrun", "simctl", "io", self.udid, "screenshot", str(target)],
                check=True,
                stderr=subprocess.DEVNULL,
                timeout=6,
            )
        return name

    def capture_meta(self, directory: Path, image_name: str, appearance: str = "dark") -> Dict[str, bool]:
        """Persist `.ax.json` (sync) and `.ocr.json` (background) sidecars for a frozen shot."""
        app_mode = "light" if appearance == "light" else "dark"
        stem = Path(image_name).stem
        meta_dir = _inspect.meta_dir_for(directory, appearance=app_mode, as_after=False)
        ax_saved = False
        if self.available and self.udid and self.axe_bin:
            try:
                raw, _ = _device.ax_tree(self.axe_bin, self.udid, retries=1, timeout=5)
                if raw is not None:
                    meta_dir.mkdir(parents=True, exist_ok=True)
                    tmp = meta_dir / f".{stem}.ax.json.tmp"
                    tmp.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
                    os.replace(tmp, meta_dir / f"{stem}.ax.json")
                    ax_saved = True
            except Exception:
                pass

        img_dir = (directory / "light") if app_mode == "light" else directory
        img_path = img_dir / image_name
        ocr_started = False
        if img_path.is_file():
            try:
                if _ocr.available():
                    ocr_started = True

                    def _bg_ocr() -> None:
                        try:
                            info = _ocr.recognize(str(img_path))
                            if info:
                                meta_dir.mkdir(parents=True, exist_ok=True)
                                tmp = meta_dir / f".{stem}.ocr.json.tmp"
                                tmp.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
                                os.replace(tmp, meta_dir / f"{stem}.ocr.json")
                        except Exception:
                            pass

                    threading.Thread(target=_bg_ocr, daemon=True).start()
            except Exception:
                pass

        return {"ax": ax_saved, "ocrPending": ocr_started}

    def tap(self, x_norm: float, y_norm: float) -> None:
        if not self.available:
            raise RuntimeError("Simulator or axe not available")
        x = max(1, min(self.width - 1, round(x_norm * self.width)))
        y = max(1, min(self.height - 1, round(y_norm * self.height)))
        with self._input_lock:
            subprocess.run(
                [self.axe_bin, "tap", "-x", str(x), "-y", str(y), "--udid", self.udid],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=6,
            )

    def swipe(self, x1_norm: float, y1_norm: float, x2_norm: float, y2_norm: float) -> None:
        if not self.available:
            raise RuntimeError("Simulator or axe not available")
        x1 = max(1, min(self.width - 1, round(x1_norm * self.width)))
        y1 = max(1, min(self.height - 1, round(y1_norm * self.height)))
        x2 = max(1, min(self.width - 1, round(x2_norm * self.width)))
        y2 = max(1, min(self.height - 1, round(y2_norm * self.height)))
        with self._input_lock:
            subprocess.run(
                [
                    self.axe_bin, "swipe",
                    "--start-x", str(x1), "--start-y", str(y1),
                    "--end-x", str(x2), "--end-y", str(y2),
                    "--duration", "0.28",
                    "--udid", self.udid,
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=8,
            )

    def type_text(self, text: str) -> None:
        if not self.available:
            raise RuntimeError("Simulator or axe not available")
        with self._input_lock:
            subprocess.run(
                [self.axe_bin, "type", text, "--udid", self.udid],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=6,
            )

    def press_home(self) -> None:
        if not self.available:
            raise RuntimeError("Simulator or axe not available")
        with self._input_lock:
            subprocess.run(
                [self.axe_bin, "button", "home", "--udid", self.udid],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=6,
            )

    def launch_app(self, appearance: Optional[str] = None) -> None:
        if not self.udid:
            raise RuntimeError("No booted simulator")
        if not self.bundle_id:
            raise RuntimeError("No bundle id configured (set [app] bundle_id in ujshot.toml or pass --sim-bundle)")
        if appearance in ("dark", "light"):
            subprocess.run(
                ["xcrun", "simctl", "ui", self.udid, "appearance", appearance],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=6,
            )
        subprocess.run(
            ["xcrun", "simctl", "launch", self.udid, self.bundle_id],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=6,
        )


def natural_key(name: str) -> List[Any]:
    return [int(tok) if tok.isdigit() else tok.lower() for tok in re.split(r"(\d+)", name)]


def list_images(directory: Path) -> List[str]:
    files = [
        p.name
        for p in directory.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS and not p.name.startswith(".")
    ]
    return sorted(files, key=natural_key)


def round_sort_key(name: str) -> Tuple[int, int, str]:
    """「234」这种纯数字构建号按数值排在前，`_local` 之类排在最后。"""
    return (0, int(name), "") if re.fullmatch(r"\d+", name) else (1, 0, name)


APPEARANCE_DIRS = ("light", "dark")


def list_before_images(directory: Path, appearance: str = "dark") -> List[str]:
    """返回指定外观下的改前图列表：暗色在根目录，亮色在 light/ 子目录。"""
    target = (directory / "light") if appearance == "light" else directory
    return list_images(target) if target.is_dir() else []


def list_rounds(directory: Path, appearance: str = "dark") -> List[Dict[str, Any]]:
    """改后图可以分轮且按外观分槽：
        暗色（默认，向后兼容）：after/<build>/*.png 或 after/*.png
        亮色：                 after/light/<build>/*.png 或 after/light/*.png

    返回按轮次排好序的 [{"build", "images"}]，供前端做轮次选择。
    """
    after = (directory / "after" / "light") if appearance == "light" else (directory / "after")
    rounds: List[Dict[str, Any]] = []
    if not after.is_dir():
        return rounds
    root_imgs = list_images(after)
    if root_imgs:
        rounds.append({"build": "", "images": root_imgs})
    subdirs = [
        p for p in after.iterdir()
        if p.is_dir() and not p.name.startswith(".")
        and (appearance == "light" or p.name not in APPEARANCE_DIRS)
    ]
    for sub in sorted(subdirs, key=lambda p: round_sort_key(p.name)):
        imgs = list_images(sub)
        if imgs:
            rounds.append({"build": sub.name, "images": imgs})
    return rounds


def after_union(rounds: List[Dict[str, Any]]) -> List[str]:
    """所有轮次里出现过的文件名（去重）。只用来回答"这张图有没有改后版本"。"""
    seen: Dict[str, None] = {}
    for r in rounds:
        for n in r["images"]:
            seen.setdefault(n, None)
    return sorted(seen, key=natural_key)


def iso_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------- 一块服务，多块板子
REGISTRY_VERSION = 1


class BoardRegistry:
    """板子清单，落在磁盘上，重启还在。

    清单跟着项目走（`<项目>/.ujshot/boards.json`），里面存的是**相对项目根的路径**：
    仓库换个位置、换台机器 clone，清单照样指向正确目录。项目外的板子存绝对路径。
    """

    def __init__(self, path: Optional[Path] = None, root: Optional[Path] = None) -> None:
        self.path = path
        self.root = root or (project_root(path.parent) if path else None)
        self.boards: List[Dict[str, str]] = []
        self.load()

    # ---- 路径：仓库内用相对，仓库外用绝对
    def _store_path(self, directory: Any) -> str:
        p = Path(directory).expanduser().resolve()
        if self.root:
            try:
                return p.relative_to(self.root).as_posix()
            except ValueError:
                pass
        return str(p)

    def _read_path(self, stored: str) -> Optional[str]:
        raw = Path(stored).expanduser()
        if raw.is_absolute():
            return str(raw.resolve())
        if not self.root:
            return str(raw.resolve())
        return str((self.root / raw).resolve())

    # ---- 读写
    def load(self) -> None:
        if not self.path or not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        rows = raw.get("boards") if isinstance(raw, dict) else None
        if not isinstance(rows, list):
            return
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            stored = str(row.get("dir") or "").strip()
            if not stored:
                continue
            d = self._read_path(stored)
            if not d or d in seen:
                continue
            seen.add(d)
            self.boards.append({
                "name": str(row.get("name") or Path(d).name),
                "dir": d,
                "stored": self._store_path(d),
            })
        self._unique_names()

    def save(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            rows = []
            for b in self.boards:
                stored = self._store_path(b["dir"])
                b["stored"] = stored
                rows.append({"name": b["name"], "dir": stored})
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(
                json.dumps({"version": REGISTRY_VERSION, "boards": rows},
                           ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass

    # ---- 维护
    def _unique_names(self) -> None:
        used = set()
        for b in self.boards:
            base = b["name"] or "board"
            name, i = base, 2
            while name in used:
                name = f"{base}-{i}"
                i += 1
            used.add(name)
            b["name"] = name

    def add(self, directory: Any, name: str = "") -> Dict[str, str]:
        resolved = str(Path(directory).expanduser().resolve())
        for b in self.boards:
            if Path(b["dir"]).resolve() == Path(resolved):
                if name and b["name"] != name:
                    b["name"] = self._free_name(name, ignore=b)
                    self.save()
                return b
        entry = {"name": self._free_name(name or Path(resolved).name), "dir": resolved,
                 "stored": self._store_path(resolved)}
        self.boards.append(entry)
        self.save()
        return entry

    def _free_name(self, wanted: str, ignore: Optional[Dict[str, str]] = None) -> str:
        base = wanted or "board"
        name, i = base, 2
        taken = {b["name"] for b in self.boards if b is not ignore}
        while name in taken:
            name = f"{base}-{i}"
            i += 1
        return name

    def get(self, name: str) -> Optional[Dict[str, str]]:
        for b in self.boards:
            if b["name"] == name:
                return b
        return None

    def forget(self, name: str) -> bool:
        """从清单里移除一块板（只删登记，不动磁盘上的截图）。"""
        before = len(self.boards)
        self.boards = [b for b in self.boards if b["name"] != name]
        if len(self.boards) != before:
            self.save()
            return True
        return False

    def default(self) -> Optional[Dict[str, str]]:
        return self.boards[0] if self.boards else None

    def drop_missing(self) -> List[str]:
        """记住的目录被删了就别再列出来，返回被清掉的板名。

        全部目录都不在时不写盘：多半是刚 clone 下来还没跑过走查（artifacts/ 是
        gitignore 的），此时把清单清空等于下次 clone 回来板子全没了。
        """
        gone = [b["name"] for b in self.boards if not Path(b["dir"]).is_dir()]
        if gone and len(gone) == len(self.boards):
            return gone
        if gone:
            self.boards = [b for b in self.boards if Path(b["dir"]).is_dir()]
            self.save()
        return gone

    def summary(self) -> List[Dict[str, Any]]:
        out = []
        for b in self.boards:
            d = Path(b["dir"])
            dark_before = list_before_images(d, "dark") if d.is_dir() else []
            light_before = list_before_images(d, "light") if d.is_dir() else []
            before_all = sorted(set(dark_before) | set(light_before), key=natural_key)
            after_dark = after_union(list_rounds(d, "dark")) if d.is_dir() else []
            after_light = after_union(list_rounds(d, "light")) if d.is_dir() else []
            after_all = sorted(set(after_dark) | set(after_light), key=natural_key)
            cover = dark_before[0] if dark_before else (light_before[0] if light_before else "")
            out.append({
                "name": b["name"], "dir": b["dir"],
                # 菜单里显示的路径用相对项目根的形式（artifacts/…），太长会挤掉板名
                "path": b.get("stored") or b["dir"],
                "images": len(before_all),
                "after": len(after_all),
                "annotations": len(load_annotations(d).get("annotations", [])) if d.is_dir() else 0,
                "cover": cover,
            })
        return out


def load_annotations(directory: Path) -> Dict[str, Any]:
    path = directory / "annotations.json"
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("annotations"), list):
                if not isinstance(data.get("screenChecks"), dict):
                    data["screenChecks"] = {}
                return data
        except (OSError, json.JSONDecodeError):
            pass
    return {
        "version": 1,
        "directory": directory.name,
        "updatedAt": iso_now(),
        "annotations": [],
        "screenChecks": {},
    }


VERSIONS_NAME = "versions.json"


def load_versions(directory: Path) -> Dict[str, Any]:
    """哪个版本拍的这个板子。改前/改后可能来自不同 Build，必须分开记。"""
    path = directory / VERSIONS_NAME
    data: Dict[str, Any] = {"before": "", "after": "",
                            "overrides": {"before": {}, "after": {}}, "rounds": {}}
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data["before"] = str(raw.get("before", "") or "")
                data["after"] = str(raw.get("after", "") or "")
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
    return data


def version_for(versions: Dict[str, Any], image: str, side: str) -> str:
    override = (versions.get("overrides") or {}).get(side) or {}
    return str(override.get(image) or versions.get(side) or "")


def save_annotations(
    directory: Path,
    annotations: List[Dict[str, Any]],
    screen_checks: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if screen_checks is None:
        existing = load_annotations(directory)
        screen_checks = existing.get("screenChecks") if isinstance(existing.get("screenChecks"), dict) else {}
    payload = {
        "version": 1,
        "directory": directory.name,
        "updatedAt": iso_now(),
        "annotations": annotations,
        "screenChecks": screen_checks,
    }
    target = directory / "annotations.json"
    fd, tmp_name = tempfile.mkstemp(prefix=".annotations-", suffix=".json", dir=str(directory))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp_name, target)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return payload


HTML_ASSET = Path(__file__).resolve().parent / "assets" / "reviewboard.html"
HTML_PAGE = HTML_ASSET.read_text(encoding="utf-8")


NO_BOARDS_PAGE = """<!DOCTYPE html>
<html lang="zh-Hans"><head><meta charset="utf-8" /><title>还没有评审板</title>
<style>
  body { margin:0; height:100vh; display:flex; align-items:center; justify-content:center;
         background:#0b0d11; color:#e5e7eb;
         font:14px/1.6 -apple-system, BlinkMacSystemFont, "PingFang SC", sans-serif; }
  .box { max-width: 560px; padding: 28px 30px; }
  h1 { font-size: 16px; margin: 0 0 10px; font-weight: 600; }
  p { color: #9ca3af; font-size: 13px; margin: 0 0 14px; }
  code { background:#111827; padding: 2px 6px; border-radius: 5px; font-size: 12.5px; }
  pre { background:#111827; border:1px solid #1f2937; border-radius:9px; padding:12px 14px;
        font-size:12.5px; overflow:auto; }
</style></head>
<body><div class="box">
  <h1>还没有登记任何评审板</h1>
  <p>把截图目录交给服务，它就记住这块板，刷新页面就能看：</p>
  <pre>ujshot board --serve &lt;截图目录&gt;</pre>
  <p>或者用固定端口起一个服务、之后随时追加板子：</p>
  <pre>curl -X POST http://127.0.0.1:8766/api/boards \\
  -H 'Content-Type: application/json' \\
  -d '{"dir":"artifacts/some-board","name":"some-board"}'</pre>
</div></body></html>
"""


def make_handler(registry: "BoardRegistry", sim: SimulatorController,
                 dispatch_mgr: Optional[Any] = None):
    mgr = dispatch_mgr or _dispatcher.DispatchManager()

    class ReviewBoardHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def _send_json(self, obj: Any, status: int = HTTPStatus.OK) -> None:
            raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _send_html(self) -> None:
            body = HTML_ASSET.read_bytes() if HTML_ASSET.is_file() else HTML_PAGE.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_file(self, path: Path) -> None:
            ctype, _ = mimetypes.guess_type(str(path))
            raw = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", ctype or "application/octet-stream")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _resolve(self):
            """URL → (板子目录, 板块内路径)。

            /b/<板名>/…   → 明确指定是哪一块板
            /…           → 默认板（清单里第一块），老脚本不用改
            """
            path = urllib.parse.urlparse(self.path).path
            if path.startswith("/b/"):
                rest = path[3:]
                name, _, tail = rest.partition("/")
                entry = registry.get(urllib.parse.unquote(name))
                if not entry:
                    return None, "/__missing_board__"
                return Path(entry["dir"]), "/" + tail
            entry = registry.default()
            if not entry:
                return None, "/__no_boards__"
            return Path(entry["dir"]), path

        # ------------------------------------------------------------------ GET
        def do_GET(self) -> None:
            directory, path = self._resolve()

            # 板子清单本身不属于任何一块板
            if path == "/api/boards":
                self._send_json({"boards": registry.summary(),
                                 "default": (registry.default() or {}).get("name", "")})
                return

            if path in ("/__no_boards__", "/__missing_board__"):
                body = NO_BOARDS_PAGE.encode("utf-8")
                self.send_response(HTTPStatus.OK if path == "/__no_boards__" else HTTPStatus.NOT_FOUND)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if path in ("/", "/index.html"):
                self._send_html()
                return

            if path == "/api/sim/status":
                self._send_json(sim.status())
                return

            if path == "/api/sim/frame":
                raw = sim.capture_jpeg()
                if not raw:
                    self.send_error(HTTPStatus.SERVICE_UNAVAILABLE)
                    return
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(raw)
                return

            if path == "/api/agents":
                proj = project_root(directory) or directory
                specs = _dispatcher.discover_agents(proj)
                self._send_json({
                    "project": str(proj),
                    "agents": [s.to_dict() for s in specs],
                    "jobs": mgr.list_jobs(directory),
                })
                return

            if path.startswith("/api/dispatch/"):
                job_id = urllib.parse.unquote(path[len("/api/dispatch/"):]).strip("/")
                if hasattr(mgr, "_ensure_loaded"):
                    mgr._ensure_loaded(directory)
                job = mgr.get_job(job_id, include_logs=True)
                if job is None:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                self._send_json(job)
                return

            if path == "/api/state":
                data = load_annotations(directory)
                rounds = list_rounds(directory, "dark")
                light_rounds = list_rounds(directory, "light")
                versions = load_versions(directory)
                entry = next((b for b in registry.boards
                              if Path(b["dir"]) == directory.resolve()), {})
                self._send_json({
                    "dir": str(directory),
                    "name": entry.get("name", ""),
                    "images": list_before_images(directory, "dark"),
                    # 改后图可能有多轮（after/234/、after/235/）：这里给全部轮次，
                    # 前端要能回答"这个问题一共修了几次、每次是哪张图"。
                    "afterRounds": rounds,
                    "afterImages": after_union(rounds),
                    # 亮色外观独立分槽（light/ 与 after/light/<build>/），与暗色不混排
                    "lightImages": list_before_images(directory, "light"),
                    "lightAfterRounds": light_rounds,
                    "lightAfterImages": after_union(light_rounds),
                    "annotations": data.get("annotations", []),
                    "screenChecks": data.get("screenChecks", {}),
                    "updatedAt": data.get("updatedAt", ""),
                    "versions": versions,
                })
                return

            if path.startswith("/images/") or path.startswith("/after/"):
                is_after = path.startswith("/after/")
                prefix = "/after/" if is_after else "/images/"
                base_dir = (directory / "after") if is_after else directory
                name = urllib.parse.unquote(path[len(prefix):])
                if ".." in name or "\\" in name or not name:
                    self.send_error(HTTPStatus.BAD_REQUEST)
                    return
                img_path: Optional[Path] = None
                parts = [p for p in name.split("/") if p]
                if is_after and len(parts) == 3 and parts[0] == "light":
                    candidate = (base_dir / "light" / parts[1] / parts[2]).resolve()
                    if candidate.parent.parent.parent == base_dir.resolve() and candidate.is_file():
                        img_path = candidate
                elif is_after and len(parts) == 2 and parts[0] == "light":
                    for r in reversed(list_rounds(directory, "light")):
                        candidate = (base_dir / "light" / r["build"] / parts[1]).resolve() if r["build"] \
                            else (base_dir / "light" / parts[1]).resolve()
                        if candidate.is_file():
                            img_path = candidate
                            break
                elif is_after and len(parts) == 2:
                    candidate = (base_dir / parts[0] / parts[1]).resolve()
                    if candidate.parent.parent == base_dir.resolve() and candidate.is_file():
                        img_path = candidate
                elif is_after and len(parts) == 1:
                    for r in reversed(list_rounds(directory, "dark")):
                        candidate = (base_dir / r["build"] / parts[0]).resolve() if r["build"] \
                            else (base_dir / parts[0]).resolve()
                        if candidate.is_file():
                            img_path = candidate
                            break
                elif not is_after and len(parts) == 2 and parts[0] == "light":
                    candidate = (base_dir / "light" / parts[1]).resolve()
                    if candidate.parent.parent == base_dir.resolve() and candidate.is_file():
                        img_path = candidate
                elif len(parts) == 1:
                    candidate = (base_dir / parts[0]).resolve()
                    if candidate.parent == base_dir.resolve() and candidate.is_file():
                        img_path = candidate
                if img_path is None:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                self._send_file(img_path)
                return

            self.send_error(HTTPStatus.NOT_FOUND)

        # ----------------------------------------------------------------- POST
        def do_POST(self) -> None:
            directory, path = self._resolve()
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length > 0 else b"{}"
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return

            if path == "/api/boards":
                target = str(body.get("dir") or "").strip()
                if not target:
                    self._send_json({"ok": False, "error": "需要 dir"}, status=HTTPStatus.BAD_REQUEST)
                    return
                p = Path(target).expanduser()
                if not p.is_dir():
                    self._send_json({"ok": False, "error": f"目录不存在：{p}"},
                                    status=HTTPStatus.BAD_REQUEST)
                    return
                entry = registry.add(p, str(body.get("name") or "").strip())
                self._send_json({"ok": True, "board": entry, "boards": registry.summary()})
                return

            if path in ("/__no_boards__", "/__missing_board__"):
                self.send_error(HTTPStatus.NOT_FOUND)
                return

            if path == "/api/dispatch/codex-queue":
                proj = project_root(directory) or directory
                thread = str(body.get("thread") or "").strip()
                ann_ids = body.get("annIds") or []
                appearance = "light" if body.get("appearance") == "light" else "dark"
                image = str(body.get("image") or "")
                resume = bool(body.get("resume", False))
                if not thread:
                    self._send_json({"ok": False, "error": "需要指定 Codex 会话 ID 或名称"}, status=HTTPStatus.BAD_REQUEST)
                    return
                data = load_annotations(directory)
                id_set = {str(x) for x in ann_ids}
                chosen = [a for a in data.get("annotations", []) if str(a.get("id") or "") in id_set]
                if not chosen:
                    self._send_json({"ok": False, "error": "没有可投递的批注"}, status=HTTPStatus.BAD_REQUEST)
                    return
                prompt = _dispatcher.build_prompt(
                    directory, proj, chosen, appearance=appearance, image=image
                )
                ok, msg = _dispatcher.queue_to_codex(thread, prompt, resume=resume)
                if not ok:
                    self._send_json({"ok": False, "error": msg or "codex queue 失败"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                    return
                self._send_json({
                    "ok": True,
                    "thread": thread,
                    "queued": len(chosen),
                    "annIds": [a.get("id") for a in chosen],
                    "message": msg,
                })
                return

            if path == "/api/dispatch":
                proj = project_root(directory) or directory
                agent_id = str(body.get("agent") or "").strip()
                ann_ids = body.get("annIds") or []
                appearance = "light" if body.get("appearance") == "light" else "dark"
                image = str(body.get("image") or "")
                specs = {s.id: s for s in _dispatcher.discover_agents(proj)}
                spec = specs.get(agent_id)
                if spec is None:
                    self._send_json({"ok": False, "error": f"未知 Agent：{agent_id}"}, status=HTTPStatus.BAD_REQUEST)
                    return
                data = load_annotations(directory)
                id_set = {str(x) for x in ann_ids}
                chosen = [a for a in data.get("annotations", []) if str(a.get("id") or "") in id_set]
                try:
                    job = mgr.enqueue(
                        directory, proj, spec, chosen,
                        appearance=appearance, image=image,
                        resume_session_id=str(body.get("resumeSessionId") or "").strip(),
                        follow_up=str(body.get("followUp") or ""),
                    )
                except ValueError as exc:
                    self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                    return
                self._send_json({"ok": True, "job": job.to_dict(include_logs=True)})
                return

            if path.startswith("/api/dispatch/") and path.endswith("/cancel"):
                job_id = urllib.parse.unquote(path[len("/api/dispatch/"):-len("/cancel")]).strip("/")
                ok = mgr.cancel(job_id)
                self._send_json({"ok": ok, "job": mgr.get_job(job_id, include_logs=True)})
                return

            if path == "/api/annotations":
                anns = body.get("annotations")
                if not isinstance(anns, list):
                    self._send_json({"ok": False, "error": "annotations must be a list"}, status=HTTPStatus.BAD_REQUEST)
                    return
                disk_by_id = {
                    str(item.get("id")): item
                    for item in load_annotations(directory).get("annotations", [])
                    if isinstance(item, dict) and item.get("id")
                }
                merged_anns: List[Dict[str, Any]] = []
                for raw_item in anns:
                    if not isinstance(raw_item, dict):
                        continue
                    item = dict(raw_item)
                    if item.get("author") != "ai" and not str(item.get("aiReply") or "").strip():
                        legacy_reply = str(item.get("reply") or "").strip()
                        if legacy_reply:
                            item["aiReply"] = legacy_reply
                    old = disk_by_id.get(str(item.get("id") or ""))
                    if old:
                        if not str(item.get("aiReply") or "").strip() and str(old.get("aiReply") or "").strip():
                            item["aiReply"] = old["aiReply"]
                        if not item.get("fixedIn") and old.get("fixedIn"):
                            item["fixedIn"] = old["fixedIn"]
                        if not str(item.get("elementHint") or "").strip() and str(old.get("elementHint") or "").strip():
                            item["elementHint"] = old["elementHint"]
                    else:
                        # 旧缓存页面新建批注可能仍传 status="approved" 且无 aiReply，统一归为 open（待处理）
                        if item.get("status") == "approved" and not str(item.get("aiReply") or "").strip():
                            item["status"] = "open"
                    merged_anns.append(item)
                raw_checks = body.get("screenChecks")
                screen_checks = raw_checks if isinstance(raw_checks, dict) else None
                saved = save_annotations(directory, merged_anns, screen_checks=screen_checks)
                self._send_json({
                    "ok": True,
                    "updatedAt": saved["updatedAt"],
                    "annotations": saved["annotations"],
                    "screenChecks": saved.get("screenChecks", {}),
                })
                return

            if path == "/api/versions":
                payload = load_versions(directory)
                if "before" in body:
                    payload["before"] = str(body["before"] or "")
                if "after" in body:
                    payload["after"] = str(body["after"] or "")
                try:
                    tmp = directory / f".{VERSIONS_NAME}.tmp"
                    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                                   encoding="utf-8")
                    os.replace(tmp, directory / VERSIONS_NAME)
                except OSError as exc:
                    self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                    return
                self._send_json({"ok": True, "versions": payload})
                return

            if path == "/api/inspect":
                app_mode = "light" if body.get("appearance") == "light" else "dark"
                build = str(body.get("build") or "")
                result = _inspect.inspect_annotation(
                    directory, body, appearance=app_mode, build=build
                )
                self._send_json({"ok": True, **result})
                return

            try:
                if path == "/api/sim/tap":
                    sim.tap(float(body["xNorm"]), float(body["yNorm"]))
                    self._send_json({"ok": True})
                    return
                if path == "/api/sim/swipe":
                    sim.swipe(float(body["x1Norm"]), float(body["y1Norm"]), float(body["x2Norm"]), float(body["y2Norm"]))
                    self._send_json({"ok": True})
                    return
                if path == "/api/sim/type":
                    sim.type_text(str(body.get("text", "")))
                    self._send_json({"ok": True})
                    return
                if path == "/api/sim/home":
                    sim.press_home()
                    self._send_json({"ok": True})
                    return
                if path == "/api/sim/launch":
                    app_mode = str(body.get("appearance") or "").strip() or None
                    sim.launch_app(appearance=app_mode)
                    self._send_json({"ok": True})
                    return
                if path == "/api/sim/freeze":
                    app_mode = "light" if body.get("appearance") == "light" else "dark"
                    img_name = sim.freeze_png(directory, appearance=app_mode)
                    try:
                        meta = sim.capture_meta(directory, img_name, appearance=app_mode)
                    except Exception:
                        meta = {"ax": False, "ocrPending": False}
                    self._send_json({
                        "ok": True,
                        "image": img_name,
                        "appearance": app_mode,
                        "meta": meta,
                        "images": list_before_images(directory, "dark"),
                        "lightImages": list_before_images(directory, "light"),
                    })
                    return
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                return

            self.send_error(HTTPStatus.NOT_FOUND)

    return ReviewBoardHandler


def pick_port(preferred: int) -> int:
    for port in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"No free port near {preferred}")


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


REGISTRY_NAME = "boards.json"


def project_root(start: Optional[Path] = None) -> Optional[Path]:
    """往上找项目根：认 .git 或 ujshot.toml。找不到就返回 None。"""
    cur = (start or Path.cwd()).resolve()
    for _ in range(24):
        if (cur / ".git").exists() or (cur / "ujshot.toml").is_file():
            return cur
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


def default_registry(start: Optional[Path] = None) -> Path:
    """清单跟着项目走：落在 <项目>/.ujshot/boards.json。"""
    root = project_root(start)
    if root:
        return root / ".ujshot" / REGISTRY_NAME
    return Path.home() / ".ujshot" / REGISTRY_NAME


LEGACY_REGISTRY = Path.home() / ".ujshot" / REGISTRY_NAME


def migrate_legacy_registry(target: Path) -> int:
    """旧版清单在 ~/.ujshot/。项目版还没有时把它搬过来，别让板子凭空消失。"""
    if target.is_file() or not LEGACY_REGISTRY.is_file():
        return 0
    legacy = BoardRegistry(LEGACY_REGISTRY)
    legacy.drop_missing()
    if not legacy.boards:
        return 0
    fresh = BoardRegistry(target)
    fresh.root = project_root(target.parent) or fresh.root
    moved = 0
    for b in legacy.boards:
        fresh.add(b["dir"], b["name"])
        moved += 1
    try:
        LEGACY_REGISTRY.rename(LEGACY_REGISTRY.with_suffix(".json.migrated"))
    except OSError:
        pass
    return moved


def probe_service(port: int, host: str = "127.0.0.1") -> Optional[Dict[str, Any]]:
    """这个端口上是不是已经有一个我们在跑的服务？是就返回它的板子清单。"""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/boards", timeout=1.2) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if isinstance(data, dict) and isinstance(data.get("boards"), list):
            data["port"] = port
            return data
    except Exception:
        return None
    return None


def find_service(preferred: int) -> Optional[Dict[str, Any]]:
    """从 preferred 往后找 20 个端口，看有没有已经在跑的服务。"""
    for p in range(preferred, preferred + 20):
        found = probe_service(p)
        if found is not None:
            return found
    return None


def attach(port: int, directory: Path, name: str = "") -> Optional[Dict[str, Any]]:
    """把一块板登记进已在运行的服务，返回 {"name", "dir"}。"""
    import urllib.request
    body = json.dumps({"dir": str(directory), "name": name or directory.name}).encode("utf-8")
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/boards", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("board") if data.get("ok") else None
    except Exception:
        return None


def write_board_url(directory: Path, url: str) -> None:
    """ujshot 的 known_url 读这个文件；分板后必须写带前缀的完整地址。"""
    try:
        (directory / ".board-url").write_text(url + "\n", encoding="utf-8")
    except OSError:
        pass


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        prog="ujshot-board",
        description="Review board — one service hosts many screenshot boards")
    parser.add_argument("image_dir", nargs="?",
                        help="截图目录（可省：省了就读已登记的板子 / 只起服务）")
    parser.add_argument("--port", type=int, default=None,
                        help="服务端口（默认读 ujshot.toml 的 board.port，再不行 8766）")
    parser.add_argument("--name", default="", help="板名（默认取目录名）")
    parser.add_argument("--registry", default=None,
                        help="板子清单文件（默认 <项目>/.ujshot/boards.json，没有项目根时用 ~/.ujshot/）")
    parser.add_argument("--list", action="store_true", help="列出已登记的板子后退出")
    parser.add_argument("--sim-udid", default=None, help="模拟器 UDID（实时操作模式用）")
    parser.add_argument("--sim-bundle", default="", help="实时操作模式下要启动的 App bundle id")
    parser.add_argument("--no-open", action="store_true", help="不自动开浏览器")
    args = parser.parse_args(argv)

    cfg = config.load()
    if args.port is None:
        args.port = cfg.board_port or 8766
    if not args.sim_bundle:
        args.sim_bundle = cfg.bundle_id or ""

    registry_path = Path(args.registry).expanduser() if args.registry else default_registry()
    if not args.registry:
        migrate_legacy_registry(registry_path)

    registry = BoardRegistry(registry_path)

    if args.list:
        gone = registry.drop_missing()
        print(json.dumps({"registry": str(registry.path), "boards": registry.summary(),
                          "dropped": gone}, ensure_ascii=False, indent=1))
        return

    directory: Optional[Path] = None
    if args.image_dir:
        directory = Path(args.image_dir).expanduser().resolve()
        if not directory.is_dir():
            raise SystemExit(f"Directory not found: {directory}")

    running = find_service(args.port)
    if running is not None:
        port = running["port"]
        if directory is not None:
            entry = attach(port, directory, args.name)
            if entry:
                url = f"http://127.0.0.1:{port}/b/{urllib.parse.quote(entry['name'])}/"
                write_board_url(directory, url)
                print(f"已登记到运行中的服务：{entry['name']} → {url}", flush=True)
                after = probe_service(port) or {}
                total = len(after.get("boards") or running.get("boards") or [])
                print(f"（服务端口 {port}，共 {total} 块板）", flush=True)
                if not args.no_open:
                    webbrowser.open(url)
                return
        print(f"端口 {port} 上已有服务在跑；要加板子请带目录参数再执行一次。", flush=True)
        print(f"  已有：{', '.join(b['name'] for b in running.get('boards') or []) or '（空）'}", flush=True)
        return

    if directory is not None:
        entry = registry.add(directory, args.name)
        print(f"板名：{entry['name']}  {entry['dir']}", flush=True)
    registry.drop_missing()
    if not registry.boards:
        print("清单里还没有任何板子，页面会告诉你下一步怎么做。", flush=True)

    sim = SimulatorController(udid=args.sim_udid, bundle_id=args.sim_bundle)
    port = pick_port(args.port)
    server = ReusableThreadingHTTPServer(("127.0.0.1", port), make_handler(registry, sim))
    base = f"http://127.0.0.1:{port}"
    sim_tag = f", sim: {sim.name} ({sim.width}x{sim.height})" if sim.available else ", sim: off"
    default = registry.default()
    default_url = f"{base}/b/{urllib.parse.quote(default['name'])}/" if default else base
    print(f"UI Review Board 服务已起：{base}{sim_tag}", flush=True)
    for b in registry.summary():
        print(f"  {b['name']:<24s} {b['images']}+{b['after']} 图  {b['annotations']:>3d} 批注  {b['dir']}", flush=True)
    print(f"默认：{default_url}", flush=True)
    if default:
        write_board_url(Path(default["dir"]), default_url)
    if not args.no_open:
        webbrowser.open(default_url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
