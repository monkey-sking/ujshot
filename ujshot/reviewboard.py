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

from . import config

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


class SimulatorController:
    """Thin bridge to a booted iOS Simulator using xcrun simctl + AXe CLI."""

    def __init__(self, udid: Optional[str] = None, bundle_id: str = ""):
        # bundle id 不再写死：项目通过 --sim-bundle / ujshot.toml 的 [app] bundle_id 给
        self.bundle_id = bundle_id
        self.axe_bin = shutil.which("axe") or ("/opt/homebrew/bin/axe" if os.path.exists("/opt/homebrew/bin/axe") else None)
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

    def freeze_png(self, directory: Path) -> str:
        if not self.udid:
            raise RuntimeError("No booted simulator")
        stamp = datetime.datetime.now().strftime("%H%M%S")
        name = f"live-{stamp}.png"
        idx = 1
        while (directory / name).exists():
            name = f"live-{stamp}-{idx}.png"
            idx += 1
        target = directory / name
        with self._frame_lock:
            subprocess.run(
                ["xcrun", "simctl", "io", self.udid, "screenshot", str(target)],
                check=True,
                stderr=subprocess.DEVNULL,
                timeout=6,
            )
        return name

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

    def launch_app(self) -> None:
        if not self.udid:
            raise RuntimeError("No booted simulator")
        subprocess.run(
            ["xcrun", "simctl", "launch", self.udid, self.bundle_id, "--ui-test-dark"],
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
    """所有轮次里出现过的文件名（去重）。只用来回答\"这张图有没有改后版本\"。"""
    seen: Dict[str, None] = {}
    for r in rounds:
        for n in r["images"]:
            seen.setdefault(n, None)
    return sorted(seen, key=natural_key)


def iso_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------- 一块服务，多块板子
# 之前是"一块板一个进程一个端口"，开第二个对话就多一个没人管的进程，
# 浏览器也不知道别处还有别的板。现在把板子登记在一处，一个服务全托管：
#   GET /b/<板名>/api/state      → 这块板的状态
#   GET /b/<板名>/images/x.png   → 这块板的改前图
#   不带前缀的 /api/state         → 默认板（第一块），老脚本照旧能用
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
            pngs = sorted(d.glob("*.png")) if d.is_dir() else []
            out.append({
                "name": b["name"], "dir": b["dir"],
                # 菜单里显示的路径用相对项目根的形式（artifacts/…），太长会挤掉板名
                "path": b.get("stored") or b["dir"],
                "images": len(list_images(d)) if d.is_dir() else 0,
                "after": len(after_union(list_rounds(d))) if d.is_dir() else 0,
                "annotations": len(load_annotations(d).get("annotations", [])) if d.is_dir() else 0,
                "cover": pngs[0].name if pngs else "",
            })
        return out


def load_annotations(directory: Path) -> Dict[str, Any]:
    path = directory / "annotations.json"
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("annotations"), list):
                return data
        except (OSError, json.JSONDecodeError):
            pass
    return {
        "version": 1,
        "directory": directory.name,
        "updatedAt": iso_now(),
        "annotations": [],
    }


VERSIONS_NAME = "versions.json"


def load_versions(directory: Path) -> Dict[str, Any]:
    """哪个版本拍的这个板子。改前/改后可能来自不同 Build，必须分开记。

    文件格式（都可以省，省了就是没标版本）::

        {"before": "Build 233", "after": "Build 237",
         "overrides": {"before": {"某图.png": "Build 232"}}}

    这是**旁挂**文件而不是塞进 annotations.json：版本属于图，不属于批注，
    清空批注不该把版本信息一起清掉。
    """
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
                # 分轮：after/234/ 这一轮自己来自哪个 Build
                rounds = raw.get("rounds") or {}
                if isinstance(rounds, dict):
                    data["rounds"] = {str(k): str(v) for k, v in rounds.items()}
        except (OSError, json.JSONDecodeError):
            pass
    return data


def version_for(versions: Dict[str, Any], image: str, side: str) -> str:
    override = (versions.get("overrides") or {}).get(side) or {}
    return str(override.get(image) or versions.get(side) or "")


def save_annotations(directory: Path, annotations: List[Dict[str, Any]]) -> Dict[str, Any]:
    payload = {
        "version": 1,
        "directory": directory.name,
        "updatedAt": iso_now(),
        "annotations": annotations,
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


HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-Hans">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>UI 截图联合标注板</title>
<style>
  :root {
    --bg: #0f1115;
    --panel: #161920;
    --card: #1d212b;
    --border: #2a303c;
    --text: #f3f4f6;
    --muted: #9ca3af;
    --ai: #2dd4bf;
    --user: #f97316;
    --fixed: #22c55e;
    --ignored: #6b7280;
    --approved: #38bdf8;
    --danger: #ef4444;
    --ease-out: cubic-bezier(0.23, 1, 0.32, 1);
    --ease-in-out: cubic-bezier(0.77, 0, 0.175, 1);
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC", sans-serif;
    height: 100vh; display: flex; flex-direction: column; overflow: hidden;
  }
  header {
    height: 52px; background: var(--panel); border-bottom: 1px solid var(--border);
    display: flex; align-items: center; justify-content: space-between; padding: 0 16px; gap: 12px;
    flex-shrink: 0; min-width: 0;
  }
  .brand { display: flex; align-items: center; gap: 10px; font-weight: 600; font-size: 14px; min-width: 0; }
  .brand-name {
    font-size: 13px; font-weight: 600; color: var(--text);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 160px;
  }
  /* 板名就是下拉入口：只有一个板名的页面不该多出一排导航 */
  .board-switch { position: relative; }
  .board-btn {
    display: flex; align-items: center; gap: 4px; padding: 3px 8px 3px 10px;
    background: transparent; border-color: transparent; border-radius: 999px;
    max-width: 260px;
  }
  .board-btn:hover { background: var(--card); border-color: var(--border); }
  .board-btn .caret { font-size: 11px; color: var(--muted); line-height: 1; }
  .board-menu {
    position: absolute; top: calc(100% + 6px); left: 0; z-index: 35;
    min-width: 240px; max-width: 420px; padding: 4px;
    background: var(--panel); border: 1px solid #3b4252; border-radius: 10px;
    box-shadow: 0 16px 36px rgba(0,0,0,.6);
  }
  .board-item {
    display: flex; align-items: baseline; gap: 8px; width: 100%;
    padding: 6px 9px; border: none; background: transparent; border-radius: 7px;
    font-size: 12.5px; text-align: left; color: var(--text);
  }
  .board-item:hover { background: var(--card); }
  .board-item.current { color: var(--ai); font-weight: 600; }
  .board-item .meta { margin-left: auto; color: var(--muted); font-size: 11px; white-space: nowrap; }
  .board-item .path { color: var(--muted); font-size: 10.5px; font-family: ui-monospace, SFMono-Regular, monospace; }
  /* 状态筛选做成一条分段控件：数字就是标签，不用额外的"筛选"说明 */
  .seg {
    display: inline-flex; background: var(--bg); border: 1px solid var(--border);
    border-radius: 999px; padding: 2px; gap: 1px;
  }
  .seg .pillbtn {
    border: none; background: transparent; color: var(--muted);
    border-radius: 999px; padding: 3px 9px; font-size: 11px; font-weight: 600;
    font-variant-numeric: tabular-nums; cursor: pointer;
    white-space: nowrap; flex-shrink: 0;
    transition: background .14s var(--ease-out), color .14s var(--ease-out);
  }
  .seg .pillbtn:hover { color: var(--text); }
  .seg .pillbtn.active { background: var(--card); color: var(--text); box-shadow: inset 0 0 0 1px var(--border); }
  .seg .pillbtn.zero { opacity: .38; }
  .seg .pillbtn:active { transform: scale(0.97); }
  .pill {
    font-size: 12px; padding: 3px 9px; border-radius: 999px; background: var(--card);
    border: 1px solid var(--border); color: var(--muted);
  }
  .pill.ai { color: var(--ai); border-color: rgba(45,212,191,.35); }
  .pill.user { color: var(--user); border-color: rgba(249,115,22,.35); }
  .left-tools {
    padding: 9px; border-bottom: 1px solid var(--border);
    display: flex; flex-direction: column; gap: 7px; flex-shrink: 0;
  }
  .left-tools input {
    background: var(--bg); color: var(--text); border: 1px solid var(--border);
    border-radius: 7px; padding: 5px 8px; font: inherit; font-size: 12px; width: 100%;
    transition: border-color .14s var(--ease-out);
  }
  .left-tools input:focus { outline: none; border-color: var(--ai); }
  .left-tools input::placeholder { color: #6b7280; }
  .left-count {
    font-size: 10.5px; color: #6b7280; margin-left: auto;
    font-variant-numeric: tabular-nums; white-space: nowrap;
  }
  .actions { display: flex; align-items: center; gap: 6px; }
  .actions button { padding: 6px 10px; }
  /* 窄窗口下状态条会把右侧按钮盖住（实测 466px 时点「当前图」落在「已驳回」上）：
     换行让它们各占一行，而不是叠在一起 */
  @media (max-width: 1000px) {
    header { height: auto; flex-wrap: wrap; padding: 8px 12px; gap: 8px; }
    header .brand { flex-wrap: wrap; row-gap: 6px; }
    .seg { max-width: 100%; overflow-x: auto; scrollbar-width: none; }
    .seg::-webkit-scrollbar { display: none; }
  }
  button {
    font: inherit; cursor: pointer; border-radius: 8px; border: 1px solid var(--border);
    background: var(--card); color: var(--text); padding: 6px 12px; font-size: 12px;
    white-space: nowrap;
    transition: background .14s var(--ease-out), border-color .14s var(--ease-out),
                transform .12s var(--ease-out);
  }
  button:active { transform: scale(0.97); }
  @media (prefers-reduced-motion: reduce) {
    button, .seg .pillbtn, .ver-tag, .thumb-item { transition: none !important; }
    button:active, .seg .pillbtn:active { transform: none; }
  }
  button:hover { border-color: #4b5563; }
  button.primary { background: var(--ai); color: #06231f; border-color: transparent; font-weight: 600; }
  button.primary:hover { opacity: .92; }
  button.active { border-color: var(--ai); color: var(--ai); }
  main { flex: 1; display: grid; grid-template-columns: 230px 1fr 350px; min-height: 0; }
  aside.left, aside.right {
    background: var(--panel); overflow-y: auto; padding: 10px;
  }
  aside.left { border-right: 1px solid var(--border); display: flex; flex-direction: column; padding: 0; }
  #imageList { flex: 1; overflow-y: auto; padding: 10px; }
  aside.right { border-left: 1px solid var(--border); display: flex; flex-direction: column; padding: 0; overflow: hidden; }
  #annList { flex: 1; overflow-y: auto; padding: 10px; display: flex; flex-direction: column; gap: 10px; }
  .dispatch-bar {
    padding: 8px 10px; border-bottom: 1px solid var(--border);
    display: flex; align-items: center; gap: 6px; flex-shrink: 0;
  }
  .dispatch-bar select {
    background: var(--bg); color: var(--text); border: 1px solid var(--border);
    border-radius: 7px; padding: 4px 6px; font: inherit; font-size: 11.5px;
    max-width: 118px; cursor: pointer;
  }
  .dispatch-bar select:focus { outline: none; border-color: var(--ai); }
  .btn-dispatch {
    background: rgba(45,212,191,.14); border-color: rgba(45,212,191,.42); color: var(--ai);
    padding: 4px 9px; font-size: 11.5px; font-weight: 600; flex-shrink: 0;
  }
  .btn-dispatch:hover { background: rgba(45,212,191,.24); border-color: var(--ai); }
  .btn-dispatch:disabled { opacity: .4; cursor: not-allowed; }
  .job-pill {
    display: inline-flex; align-items: center; gap: 5px; font-size: 11px;
    padding: 2px 8px; border-radius: 999px; font-weight: 600; white-space: nowrap;
    background: rgba(56,189,248,.14); color: #7dd3fc; border: 1px solid rgba(56,189,248,.35);
  }
  .job-pill.queued { background: rgba(251,191,36,.12); color: #fcd34d; border-color: rgba(251,191,36,.35); }
  .job-pill.failed { background: rgba(239,68,68,.14); color: #fca5a5; border-color: rgba(239,68,68,.35); }
  .job-pill.done { background: rgba(34,197,94,.14); color: #86efac; border-color: rgba(34,197,94,.35); }
  .log-drawer {
    border-bottom: 1px solid var(--border); background: #0b0d12;
    padding: 8px 10px; display: flex; flex-direction: column; gap: 6px;
    max-height: 240px; flex-shrink: 0;
  }
  .log-drawer-head {
    display: flex; align-items: center; justify-content: space-between; gap: 6px;
    font-size: 11px; color: var(--muted);
  }
  .log-drawer pre {
    margin: 0; overflow: auto; flex: 1; max-height: 185px;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 10.5px;
    line-height: 1.45; color: #cbd5e1; white-space: pre-wrap; word-break: break-all;
  }
  .thumb-item {
    display: flex; align-items: flex-start; gap: 9px; padding: 8px;
    border-radius: 10px; cursor: pointer; border: 1px solid transparent;
    margin-bottom: 2px;
    transition: background .14s var(--ease-out), border-color .14s var(--ease-out);
  }
  @media (hover: hover) and (pointer: fine) {
    .thumb-item:hover { background: var(--card); }
  }
  .thumb-item.selected { background: var(--card); border-color: var(--ai); }
  .thumb-item img {
    width: 26px; height: 46px; object-fit: cover; border-radius: 4px;
    background: #000; flex-shrink: 0;
  }
  .thumb-meta { min-width: 0; flex: 1; }
  .thumb-name {
    font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    color: var(--text); line-height: 1.35;
  }
  .thumb-item.selected .thumb-name { color: #fff; }
  /* 一行元信息：状态点 + 数字 + 只在有值时出现的版本号 */
  .thumb-line {
    display: flex; align-items: center; gap: 5px; margin-top: 3px;
    font-size: 11px; color: var(--muted); font-variant-numeric: tabular-nums;
  }
  .dot { width: 6px; height: 6px; border-radius: 50%; background: currentColor; flex-shrink: 0; }
  .dot.open { color: var(--user); }
  .dot.fixed { color: #38bdf8; }
  .dot.rejected { color: var(--danger); }
  .dot.discussed { color: #a855f7; }
  .dot.accepted { color: var(--fixed); }
  .thumb-sep { opacity: .35; }
  .stage {
    position: relative; display: flex; flex-direction: column; align-items: center; justify-content: center;
    overflow: auto; padding: 16px; user-select: none;
  }
  .hint-bar {
    position: absolute; top: 10px; left: 50%; transform: translateX(-50%);
    background: rgba(22,25,32,.88); border: 1px solid var(--border); border-radius: 999px;
    padding: 4px 12px; font-size: 12px; color: var(--muted); z-index: 5; pointer-events: none;
  }
  .canvas-wrap {
    position: relative; display: inline-block; line-height: 0; border-radius: 18px;
    box-shadow: 0 20px 50px rgba(0,0,0,.55); cursor: crosshair;
  }
  .canvas-wrap img {
    display: block; max-height: calc(100vh - 108px); max-width: 100%; border-radius: 18px;
    pointer-events: none;
  }
  .canvas-wrap.zoom100 img { max-height: none; width: 402px; }
  .compare-row { display: flex; align-items: flex-start; gap: 14px; }
  .compare-col { display: flex; flex-direction: column; align-items: center; gap: 5px; }
  /* 左侧缩略图里，改前/改后并排一小块，不用点进去就知道有没有对照 */
  .thumb-pair { display: flex; gap: 2px; flex-shrink: 0; }
  .thumb-pair img { width: 26px; height: 46px; object-fit: cover; border-radius: 4px; background: #000; }
  .thumb-pair .only { opacity: .5; }
  .pair-tag {
    font-size: 10px; font-weight: 700; padding: 0 4px; border-radius: 4px;
    line-height: 14px; display: inline-block;
  }
  .pair-tag.both { background: rgba(45,212,191,.18); color: var(--ai); }
  .pair-tag.after-only { background: rgba(56,189,248,.18); color: var(--approved); }
  .pair-tag.before-only { background: rgba(156,163,175,.18); color: var(--muted); }
  /* 改前/改后并排时只需要极短的分隔，不要两行大字标题 */
  .compare-label {
    font-size: 10px; font-weight: 600; letter-spacing: .04em;
    text-transform: uppercase; color: var(--muted);
    display: flex; align-items: center; gap: 6px;
  }
  .compare-label .ver { font-weight: 400; opacity: .7; text-transform: none; letter-spacing: 0; }
  .stage-hint {
    font-size: 12px; color: var(--muted); text-align: center; padding: 4px 0 0;
  }
  .ver-bar {
    display: flex; align-items: center; gap: 5px; flex-wrap: wrap;
    font-size: 11px; cursor: pointer;
  }
  .ver-bar:hover .ver-tag { border-color: #4b5563; }
  .ver-tag {
    padding: 1px 6px; border-radius: 999px; font-weight: 600;
    border: 1px solid var(--border); background: var(--bg); color: var(--muted);
    font-variant-numeric: tabular-nums;
  }
  .ver-tag.before { border-color: rgba(156,163,175,.45); }
  .ver-tag.after { border-color: rgba(34,197,94,.4); color: #86efac; }
  .ver-tag.empty { opacity: .5; font-weight: 400; }
  .ver-badge {
    font-size: 10px; font-weight: 700; padding: 1px 6px; border-radius: 999px;
    background: rgba(56,189,248,.16); color: #7dd3fc; margin-left: 4px;
    font-variant-numeric: tabular-nums;
  }
  .badge.fixed-in { background: rgba(56,189,248,.16); color: #7dd3fc; }
  .badge.unfixed { background: rgba(251,191,36,.16); color: #fcd34d; }
  .compare-label { font-size: 12px; color: var(--muted); font-weight: 600; }
  .after-wrap { position: relative; display: inline-block; line-height: 0; border-radius: 18px; box-shadow: 0 20px 50px rgba(0,0,0,.55); cursor: crosshair; }
  .after-wrap img { display: block; max-height: calc(100vh - 128px); max-width: 100%; border-radius: 18px; pointer-events: none; }
  .after-wrap.zoom100 img { max-height: none; width: 402px; }
  .missing-shot {
    width: 260px; height: min(560px, calc(100vh - 140px));
    border-radius: 18px; border: 1.5px dashed var(--border);
    background: rgba(255,255,255,.02);
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    gap: 8px; padding: 20px; text-align: center; color: var(--muted); font-size: 12.5px; line-height: 1.5;
  }
  .missing-shot strong { color: var(--text); font-size: 13px; }
  .ai-reply-box {
    background: rgba(45,212,191,.09); border: 1px solid rgba(45,212,191,.28);
    border-left: 3px solid var(--ai);
    border-radius: 8px; padding: 8px 10px; font-size: 12.5px; line-height: 1.55; color: #ccfbf1;
    white-space: pre-wrap; word-break: break-word;
  }
  .ai-reply-title { font-size: 11px; color: var(--ai); font-weight: 600; margin-bottom: 4px; display: flex; align-items: center; justify-content: space-between; }
  .note-display {
    background: rgba(255,255,255,.03); border: 1px solid transparent;
    border-radius: 8px; padding: 7px 9px; font-size: 13px; line-height: 1.55; color: var(--text);
    white-space: pre-wrap; word-break: break-word;
  }
  .note-display-head {
    display: flex; align-items: center; justify-content: space-between; margin-bottom: 4px;
    font-size: 11px; color: var(--muted);
  }
  .mini-link {
    background: transparent; border: none; color: var(--muted); font-size: 11px;
    padding: 1px 5px; cursor: pointer; border-radius: 4px;
  }
  .mini-link:hover { color: var(--text); background: rgba(255,255,255,.07); }
  .edit-field-wrap { display: flex; flex-direction: column; gap: 6px; }
  .edit-field-wrap textarea {
    width: 100%; background: var(--bg); color: var(--text); border: 1.5px solid var(--ai);
    border-radius: 8px; padding: 8px 10px; font: inherit; font-size: 13px; line-height: 1.5;
    resize: none; overflow: hidden; min-height: 54px;
  }
  .edit-field-actions { display: flex; justify-content: flex-end; gap: 6px; }
  .edit-field-actions button { padding: 4px 10px; font-size: 11px; }
  .review-bar { display: flex; gap: 6px; margin-top: 2px; }
  .review-bar button { flex: 1; padding: 6px 10px; font-size: 12px; font-weight: 600; border-radius: 8px; }
  .review-bar button.btn-accept { background: rgba(34,197,94,.14); border-color: rgba(34,197,94,.45); color: #86efac; }
  .review-bar button.btn-accept.active { background: #22c55e; border-color: #22c55e; color: #052e16; }
  .review-bar button.btn-reject { background: rgba(239,68,68,.12); border-color: rgba(239,68,68,.42); color: #fca5a5; }
  .review-bar button.btn-reject.active { background: #ef4444; border-color: #ef4444; color: #fff; }
  .reject-box {
    background: rgba(239,68,68,.09); border: 1px solid rgba(239,68,68,.35);
    border-left: 3px solid var(--danger);
    border-radius: 8px; padding: 8px 10px; display: flex; flex-direction: column; gap: 6px;
  }
  .reject-box-title { font-size: 11px; color: #fca5a5; font-weight: 600; display: flex; align-items: center; justify-content: space-between; }
  .reject-box-body { font-size: 12.5px; line-height: 1.55; color: #fee2e2; white-space: pre-wrap; word-break: break-word; }
  .reject-box textarea { border: 1.5px solid rgba(239,68,68,.55); resize: none; overflow: hidden; min-height: 54px; }
  .overlay { position: absolute; inset: 0; border-radius: 18px; }
  .pin {
    position: absolute; transform: translate(-50%, -50%);
    min-width: 24px; height: 24px; padding: 0 6px; border-radius: 999px;
    display: flex; align-items: center; justify-content: center;
    font-size: 11px; font-weight: 700; color: #0b0d11;
    box-shadow: 0 3px 10px rgba(0,0,0,.55), 0 0 0 2px rgba(255,255,255,.85);
    cursor: pointer; z-index: 4; transition: transform .12s;
  }
  .pin:hover, .pin.active { transform: translate(-50%, -50%) scale(1.18); z-index: 6; }
  .pin.ai { background: var(--ai); }
  .pin.user { background: var(--user); color: #fff; }
  .pin.approved { background: var(--approved); color: #061826; }
  .pin.fixed { background: #38bdf8; color: #061826; }
  .pin.accepted { background: var(--fixed); opacity: .65; }
  .pin.rejected { background: var(--danger); color: #fff; }
  .pin.discussed { background: #a855f7; color: #fff; }
  .pin.ignored { background: var(--ignored); color: #fff; opacity: .6; }
  .box-mark {
    position: absolute; border: 2px solid var(--user); background: rgba(249,115,22,.14);
    border-radius: 8px; cursor: pointer; z-index: 3;
  }
  .box-mark.ai { border-color: var(--ai); background: rgba(45,212,191,.13); }
  .box-mark.approved { border-color: var(--approved); background: rgba(56,189,248,.14); }
  .box-mark.fixed { border-color: #38bdf8; background: rgba(56,189,248,.12); }
  .box-mark.accepted { border-color: var(--fixed); background: rgba(34,197,94,.08); opacity: .6; }
  .box-mark.rejected { border-color: var(--danger); background: rgba(239,68,68,.18); }
  .box-mark.discussed { border-color: #a855f7; background: rgba(168,85,247,.14); }
  .box-mark.ignored { border-color: var(--ignored); border-style: dashed; background: transparent; opacity: .55; }
  .box-mark.active { box-shadow: 0 0 0 2px #fff; z-index: 5; }
  .box-tag {
    position: absolute; top: -11px; left: -2px; font-size: 10px; font-weight: 700;
    padding: 1px 6px; border-radius: 999px; background: inherit; color: #0b0d11; line-height: 16px;
  }
  .box-mark.user .box-tag { background: var(--user); color: #fff; }
  .box-mark.ai .box-tag { background: var(--ai); }
  .box-mark.approved .box-tag { background: var(--approved); }
  .box-mark.fixed .box-tag { background: #38bdf8; }
  .box-mark.accepted .box-tag { background: var(--fixed); }
  .box-mark.rejected .box-tag { background: var(--danger); color: #fff; }
  .box-mark.discussed .box-tag { background: #a855f7; color: #fff; }
  .box-mark.ignored .box-tag { background: var(--ignored); color: #fff; }
  .draft-box {
    position: absolute; border: 2px dashed var(--user); background: rgba(249,115,22,.18);
    border-radius: 8px; pointer-events: none; z-index: 7;
  }
  .popover {
    position: fixed; width: 290px; background: var(--panel); border: 1px solid #3b4252;
    border-radius: 12px; padding: 10px; box-shadow: 0 18px 40px rgba(0,0,0,.65); z-index: 30;
  }
  .popover textarea, .card textarea {
    width: 100%; background: var(--bg); color: var(--text); border: 1px solid var(--border);
    border-radius: 8px; padding: 8px; font: inherit; font-size: 13px; resize: vertical;
  }
  .popover textarea:focus, .card textarea:focus { outline: none; border-color: var(--ai); }
  .popover-row { display: flex; justify-content: flex-end; gap: 6px; margin-top: 8px; }
  .card {
    background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 11px;
    display: flex; flex-direction: column; gap: 8px; position: relative;
  }
  .card.settled { opacity: .82; }
  .card.settled:hover, .card.settled.active { opacity: 1; }
  .card.active { border-color: var(--ai); box-shadow: 0 0 0 1px rgba(45,212,191,.4); }
  .card-head { display: flex; align-items: center; justify-content: space-between; gap: 6px; font-size: 12px; }
  .card-id { font-weight: 700; display: flex; align-items: center; gap: 6px; min-width: 0; }
  .card-id > span:first-child { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 135px; }
  .card-right { display: flex; align-items: center; gap: 6px; flex-shrink: 0; margin-left: auto; }
  .ver-meta { font-size: 11px; color: var(--muted); font-variant-numeric: tabular-nums; white-space: nowrap; }
  .badge { font-size: 11px; padding: 2px 7px; border-radius: 999px; font-weight: 600; white-space: nowrap; flex-shrink: 0; line-height: 1.35; }
  .badge.ai { background: rgba(45,212,191,.14); color: var(--ai); }
  /* 只读状态徽章：与可点击按钮在视觉上彻底区分（无边框、胶囊底色、不可点） */
  .st-badge { font-size: 11px; padding: 2px 8px; border-radius: 999px; font-weight: 600; white-space: nowrap; flex-shrink: 0; line-height: 1.35; }
  .st-badge.st-open, .st-badge.st-approved { background: rgba(249,115,22,.16); color: #fdba74; }
  .st-badge.st-fixed { background: rgba(56,189,248,.16); color: #7dd3fc; }
  .st-badge.st-accepted { background: rgba(34,197,94,.16); color: #86efac; }
  .st-badge.st-rejected { background: rgba(239,68,68,.18); color: #fca5a5; }
  .st-badge.st-discussed { background: rgba(168,85,247,.16); color: #d8b4fe; }
  .st-badge.st-ignored { background: rgba(107,114,128,.2); color: #9ca3af; }
  .note-text {
    font-size: 13px; line-height: 1.55; color: var(--text);
    white-space: pre-wrap; word-break: break-word;
  }
  .card-actions { display: flex; gap: 6px; align-items: center; }
  .card-actions button { flex: 1; padding: 6px 10px; font-size: 12px; font-weight: 600; border-radius: 8px; }
  .card-actions button.btn-accept { background: rgba(34,197,94,.16); border-color: rgba(34,197,94,.45); color: #86efac; }
  .card-actions button.btn-accept:hover { background: rgba(34,197,94,.26); }
  .card-actions button.btn-reject { background: rgba(239,68,68,.12); border-color: rgba(239,68,68,.42); color: #fca5a5; }
  .card-actions button.btn-reject:hover { background: rgba(239,68,68,.22); }
  .card-actions button.btn-stop { flex: 0 0 auto; color: #fca5a5; border-color: rgba(239,68,68,.4); background: rgba(239,68,68,.08); }
  .more-wrap { position: relative; }
  .more-btn {
    width: 24px; height: 24px; padding: 0; border-radius: 6px;
    border-color: transparent; background: transparent; color: var(--muted);
    display: inline-flex; align-items: center; justify-content: center; font-size: 14px; line-height: 1;
  }
  .more-btn:hover { background: rgba(255,255,255,.08); color: var(--text); border-color: transparent; }
  .more-menu {
    position: absolute; top: calc(100% + 4px); right: 0; z-index: 25;
    min-width: 132px; padding: 4px; border-radius: 9px;
    background: var(--panel); border: 1px solid #3b4252;
    box-shadow: 0 14px 32px rgba(0,0,0,.6);
    display: flex; flex-direction: column; gap: 2px;
  }
  .more-menu button {
    width: 100%; text-align: left; border: none; background: transparent;
    padding: 6px 9px; border-radius: 6px; font-size: 12px; color: var(--text);
  }
  .more-menu button:hover { background: var(--card); }
  .more-menu button.danger { color: #fca5a5; }
  .more-menu hr { border: none; border-top: 1px solid var(--border); margin: 3px 4px; }
  /* 轮次条：同一问题修了几轮，一行看全。未给结论的轮次只显示构建号 */
  .round-bar { display: flex; flex-wrap: wrap; gap: 4px; margin-top: -1px; }
  .round-chip {
    padding: 2px 8px; font-size: 11px; border-radius: 999px; opacity: .55;
    background: transparent; border-color: var(--border);
  }
  .round-chip.active { opacity: 1; box-shadow: inset 0 0 0 1px currentColor; }
  .round-chip.on-accepted { color: var(--fixed); }
  .round-chip.on-rejected { color: #fca5a5; }
  .round-chip.on-fixed { color: #38bdf8; }
  .round-chip.on-discussed { color: #d8b4fe; }
  .round-chip.on-approved { color: var(--approved); }
  .status-row { display: flex; flex-wrap: wrap; gap: 5px; }
  .status-row button { padding: 4px 8px; font-size: 11px; }
  .status-row button.on-open { border-color: var(--user); color: var(--user); }
  .status-row button.on-approved { border-color: var(--approved); color: var(--approved); }
  .status-row button.on-fixed { border-color: #38bdf8; color: #38bdf8; }
  .status-row button.on-accepted { border-color: var(--fixed); color: var(--fixed); }
  .status-row button.on-rejected { border-color: var(--danger); color: #fca5a5; }
  .status-row button.on-discussed { border-color: #a855f7; color: #d8b4fe; }
  .status-row button.on-ignored { border-color: var(--ignored); color: var(--muted); }
  .status-row button.del { margin-left: auto; color: var(--danger); border-color: rgba(239,68,68,.35); }
  .empty { text-align: center; color: var(--muted); font-size: 13px; padding: 28px 12px; }
  .toast {
    position: fixed; bottom: 16px; left: 50%; transform: translateX(-50%);
    background: #1f2937; color: var(--ai); border: 1px solid rgba(45,212,191,.4);
    padding: 6px 14px; border-radius: 999px; font-size: 12px; opacity: 0; pointer-events: none;
    transition: opacity .2s; z-index: 40;
  }
  .toast.show { opacity: 1; }
  .mode-switch {
    display: inline-flex; background: var(--bg); border: 1px solid var(--border);
    border-radius: 999px; padding: 2px; gap: 2px;
  }
  .mode-switch button {
    border: none; background: transparent; color: var(--muted); border-radius: 999px;
    padding: 4px 12px; font-size: 12px; font-weight: 600;
  }
  .mode-switch button.active {
    background: var(--ai); color: #06231f;
  }
  .live-stage {
    display: none; flex-direction: column; align-items: center; gap: 10px;
  }
  .live-wrap {
    position: relative; display: inline-block; line-height: 0; border-radius: 22px;
    box-shadow: 0 20px 50px rgba(0,0,0,.6), 0 0 0 2px rgba(45,212,191,.45);
    cursor: pointer; user-select: none; overflow: hidden;
  }
  .live-wrap img {
    display: block; max-height: calc(100vh - 165px); max-width: 100%; border-radius: 20px;
    pointer-events: none;
  }
  .tap-ripple {
    position: absolute; width: 34px; height: 34px; border-radius: 999px;
    border: 2px solid var(--ai); background: rgba(45,212,191,.28);
    transform: translate(-50%, -50%) scale(0.4); pointer-events: none;
    animation: tapRipple .42s ease-out forwards;
  }
  @keyframes tapRipple {
    to { transform: translate(-50%, -50%) scale(1.45); opacity: 0; }
  }
  .live-controls {
    display: flex; align-items: center; gap: 8px; flex-wrap: wrap; justify-content: center;
  }
  .live-controls input {
    background: var(--card); color: var(--text); border: 1px solid var(--border);
    border-radius: 8px; padding: 6px 10px; font: inherit; font-size: 12px; width: 170px;
  }
  .live-controls input:focus { outline: none; border-color: var(--ai); }
</style>
</head>
<body>
<header>
  <div class="brand">
    <div class="board-switch">
      <button class="board-btn" id="boardBtn" onclick="toggleBoardMenu(event)" title="切换评审板">
        <span class="brand-name" id="dirLabel" title="">评审板</span><span class="caret">⌄</span>
      </button>
      <div class="board-menu" id="boardMenu" style="display:none;"></div>
    </div>
    <div class="mode-switch" id="modeSwitch" style="display:none;">
      <button id="modeOperateBtn" onclick="setBoardMode('operate')">🖐 操作</button>
      <button id="modeAnnotateBtn" class="active" onclick="setBoardMode('annotate')">✏️ 批注</button>
    </div>
    <div class="seg" id="appearanceSeg" title="一键切换暗色 / 亮色截图，不混排">
      <button class="pillbtn active" id="appDarkBtn" onclick="setAppearance('dark')">🌙 暗色</button>
      <button class="pillbtn" id="appLightBtn" onclick="setAppearance('light')">☀️ 亮色</button>
    </div>
    <div class="seg" id="statusSeg">
      <button class="pillbtn" id="pillOpen" data-filter="open" onclick="toggleStatusFilter('open')" title="待处理">待处理 0</button>
      <button class="pillbtn" id="pillFixed" data-filter="fixed" onclick="toggleStatusFilter('fixed')" title="待验收">待验收 0</button>
      <button class="pillbtn" id="pillRejected" data-filter="rejected" onclick="toggleStatusFilter('rejected')" title="已驳回">已驳回 0</button>
      <button class="pillbtn" id="pillDiscussed" data-filter="discussed" onclick="toggleStatusFilter('discussed')" title="待探讨" style="display:none">待探讨 0</button>
      <button class="pillbtn" id="pillAccepted" data-filter="accepted" onclick="toggleStatusFilter('accepted')" title="已通过">已通过 0</button>
    </div>
  </div>
  <div class="actions">
    <button id="compareBtn" onclick="toggleCompare()" style="display:none;" title="改前/改后对比">🔀</button>
    <button id="zoomBtn" onclick="toggleZoom()" title="100% 缩放">⤢</button>
    <button id="scopeBtn" onclick="toggleScope()" title="只看当前图 / 全部图">当前图</button>
    <button class="primary" onclick="copyForAI()" title="复制批注给 AI">📋</button>
  </div>
</header>
<main>
  <aside class="left" id="leftPane">
    <div class="left-tools">
      <input id="imageSearch" type="text" placeholder="搜索…" oninput="onImageSearch(this.value)" />
      <div class="ver-bar" id="verBar">
        <span class="ver-tag before" id="verBefore">—</span>
        <span class="ver-tag after" id="verAfter">—</span>
        <span class="left-count" id="imageCount"></span>
      </div>
    </div>
    <div id="imageList"></div>
  </aside>
  <section class="stage" id="stage">
    <div class="live-stage" id="liveStage">
      <div class="live-wrap" id="liveWrap">
        <img id="liveImg" alt="Live iOS Simulator" />
      </div>
      <div class="live-controls">
        <input id="simTypeInput" type="text" placeholder="输入文字到模拟器…" onkeydown="if(event.key==='Enter')sendSimText()" />
        <button onclick="sendSimText()">发送</button>
        <button onclick="sendSimHome()">Home</button>
        <button onclick="launchSimApp()" title="按 --sim-bundle 指定的 App">打开 App</button>
      </div>
    </div>
    <div class="compare-row" id="annotateStage">
      <div class="compare-col">
        <div class="compare-label" id="beforeLabel" style="display:none;"></div>
        <div class="canvas-wrap" id="canvasWrap">
          <img id="stageImg" alt="screenshot" />
          <div class="overlay" id="overlay"></div>
        </div>
      </div>
      <div class="compare-col" id="afterCol" style="display:none;">
        <div class="compare-label" id="afterLabel" style="color:var(--fixed);"></div>
        <div class="after-wrap" id="afterWrap">
          <img id="afterImg" alt="after screenshot" />
          <div class="overlay" id="afterOverlay"></div>
        </div>
        <div class="missing-shot" id="afterMissing" style="display:none;"></div>
      </div>
    </div>
    <div class="stage-hint" id="stageHint"></div>
  </section>
  <aside class="right">
    <div class="dispatch-bar" id="dispatchBar">
      <select id="agentSelect" onchange="onAgentChange(this.value)" title="选择要拉起的本地 Agent"></select>
      <button class="btn-dispatch" id="batchDispatchBtn" onclick="dispatchScreen()" title="把本屏待处理与已驳回批注打包交给选中的 Agent 一次修改">🚀 打包派发本屏</button>
      <button class="mini-link" id="logToggleBtn" onclick="toggleLogDrawer()" style="display:none;margin-left:auto;">日志</button>
    </div>
    <div class="log-drawer" id="logDrawer" style="display:none;"></div>
    <div id="annList"></div>
  </aside>
</main>
<div class="popover" id="popover" style="display:none;">
  <div style="font-size:12px;color:var(--muted);margin-bottom:6px;" id="popTitle">新增批注</div>
  <textarea id="popText" rows="3" placeholder="直接写哪里不对、怎么改...（Cmd/Ctrl + Enter 保存）"></textarea>
  <div class="popover-row">
    <button onclick="closePopover()">取消</button>
    <button class="primary" onclick="savePopover()">保存标注</button>
  </div>
</div>
<div class="toast" id="toast">已自动保存</div>

<script>
// ---- 一块服务托管多块板：页面自己的 URL 决定它属于哪块板
//   http://host:8766/b/board-currency/…  → 这块板
//   http://host:8766/…                  → 默认板（清单第一块）
// 页内所有请求都走 api()/imgUrl()，不写死绝对路径，才能跟着前缀走。
const BOARD_PREFIX = (() => {
  const m = /^\/b\/([^/]+)/.exec(location.pathname);
  return m ? "/b/" + m[1] : "";
})();

function api(path) { return BOARD_PREFIX + path; }

// 切到另一块板：只换 URL 前缀，整页重载，别处的 world 不会串
function switchBoard(name) {
  location.href = "/b/" + encodeURIComponent(name) + "/";
}

let state = { dir: "", name: "", images: [], afterImages: [], afterRounds: [], annotations: [], versions: { before: "", after: "", overrides: {}, rounds: {} } };
let currentImage = "";
let selectedId = null;
let showAllScope = false;
let editingNoteIds = new Set();
let openRejectEditors = new Set();
let zoom100 = false;
let allNames = [];   // 改前 ∪ 改后：只列 before 会让"只有改后"的图永远点不到

function matchesFilter(a) {
  // 全板只用顶部那一条状态筛选：它同时管左边列表和右边批注卡。
  if (!statusFilter) return true;
  return bucketOf(a) === statusFilter;
}

function autoSizeTextarea(el) {
  el.style.height = "auto";
  el.style.height = Math.max(54, el.scrollHeight) + "px";
}
let compareMode = true;
let currentAppearance = "dark";
let pendingDraft = null;
let dragStart = null;
let draftEl = null;

function appearanceOf(a) {
  return (a && a.appearance === "light") ? "light" : "dark";
}
function beforeImagesFor(app) {
  const mode = app || currentAppearance;
  return mode === "light" ? (state.lightImages || []) : (state.images || []);
}
function afterRoundsFor(app) {
  const mode = app || currentAppearance;
  return mode === "light" ? (state.lightAfterRounds || []) : (state.afterRounds || []);
}
function afterImagesFor(app) {
  const mode = app || currentAppearance;
  return mode === "light" ? (state.lightAfterImages || []) : (state.afterImages || []);
}
function setAppearance(app) {
  const next = (app === "light") ? "light" : "dark";
  if (currentAppearance === next) return;
  currentAppearance = next;
  selectedId = null;
  closePopover();
  const names = allImageNames(currentAppearance);
  if (names.length && !names.includes(currentImage)) {
    currentImage = names[0];
  } else if (!names.length) {
    currentImage = null;
  }
  render();
}

async function init() {
  const res = await fetch(api("/api/state"));
  state = await res.json();
  const name = state.name || state.dir.split("/").pop() || state.dir;
  const label = document.getElementById("dirLabel");
  label.textContent = name;
  // 板名可点：所有登记过的板都在这里，切过去只换 URL 前缀
  await loadBoardMenu();
  // tooltip 用相对项目根的路径（artifacts/…），绝对路径太长且换机器就变
  label.title = (boardList.boards.find(b => b.name === name) || {}).path || state.dir;
  // 版本标签本身就是入口：点一下直接改，不额外挂「✏️ 版本」按钮
  const bar = document.getElementById("verBar");
  if (bar) { bar.onclick = editVersions; bar.title = "点击设置改前/改后各是哪个 Build"; }
  const darkNames = allImageNames("dark");
  const lightNames = allImageNames("light");
  if (!darkNames.length && lightNames.length) currentAppearance = "light";
  const initialNames = allImageNames(currentAppearance);
  if (initialNames.length > 0 && !initialNames.includes(currentImage)) currentImage = initialNames[0];
  await loadAgentsAndJobs();
  render();
}

// ---- 本地 Agent 派发与实时日志（同仓库串行队列）
let availableAgents = [];
let selectedAgentId = localStorage.getItem("ujshot.agent") || "";
let dispatchJobs = [];
let openLogJobId = null;
let activeLogJob = null;
let dispatchPollTimer = null;
let lastRunningJobIds = new Set();

function activeJobForAnn(annId) {
  return dispatchJobs.find(j => (j.status === "running" || j.status === "queued") && (j.annIds || []).includes(annId)) || null;
}

function selectedAgentObj() {
  return availableAgents.find(a => a.id === selectedAgentId)
    || availableAgents.find(a => a.available)
    || availableAgents[0] || null;
}

function onAgentChange(val) {
  selectedAgentId = val;
  try { localStorage.setItem("ujshot.agent", val); } catch (e) {}
  renderCards();
}

async function loadAgentsAndJobs() {
  try {
    const res = await fetch(api("/api/agents"));
    const data = await res.json();
    availableAgents = data.agents || [];
    dispatchJobs = data.jobs || [];
    const curRunning = new Set(dispatchJobs.filter(j => j.status === "running" || j.status === "queued").map(j => j.id));
    let newlyDone = false;
    for (const id of lastRunningJobIds) {
      if (!curRunning.has(id)) {
        const done = dispatchJobs.find(j => j.id === id);
        if (done && done.status === "done") newlyDone = true;
      }
    }
    lastRunningJobIds = curRunning;
    if (!selectedAgentId || !availableAgents.some(a => a.id === selectedAgentId)) {
      const firstAvail = availableAgents.find(a => a.available);
      selectedAgentId = firstAvail ? firstAvail.id : (availableAgents[0] ? availableAgents[0].id : "");
    }
    if (openLogJobId) {
      try {
        const lr = await fetch(api(`/api/dispatch/${encodeURIComponent(openLogJobId)}`));
        if (lr.ok) activeLogJob = await lr.json();
      } catch (e) {}
    }
    if (newlyDone) {
      const stRes = await fetch(api("/api/state"));
      state = await stRes.json();
      showToast("Agent 修改完成，已自动刷新截图与状态");
      render();
    } else {
      renderDispatchBar();
      renderLogDrawer();
    }
    scheduleDispatchPoll(curRunning.size > 0);
  } catch (e) {
    renderDispatchBar();
  }
}

function scheduleDispatchPoll(hasActive) {
  if (dispatchPollTimer) { clearTimeout(dispatchPollTimer); dispatchPollTimer = null; }
  if (hasActive) {
    dispatchPollTimer = setTimeout(() => { loadAgentsAndJobs().then(() => renderCards()); }, 1500);
  }
}

function screenDispatchableAnns() {
  if (!currentImage) return [];
  return state.annotations.filter(a =>
    a.image === currentImage
    && appearanceOf(a) === currentAppearance
    && (a.status === "open" || a.status === "approved" || a.status === "rejected")
  );
}

function renderDispatchBar() {
  const sel = document.getElementById("agentSelect");
  const btn = document.getElementById("batchDispatchBtn");
  const logBtn = document.getElementById("logToggleBtn");
  if (!sel || !btn) return;
  sel.innerHTML = "";
  const usable = availableAgents.filter(a => a.available);
  if (availableAgents.length === 0) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "未检测到 Agent";
    sel.appendChild(opt);
  } else {
    for (const a of availableAgents) {
      const opt = document.createElement("option");
      opt.value = a.id;
      opt.textContent = a.available ? a.label : `${a.label} (未安装)`;
      opt.disabled = !a.available;
      if (a.id === selectedAgentId) opt.selected = true;
      sel.appendChild(opt);
    }
  }
  const todo = screenDispatchableAnns();
  btn.textContent = todo.length ? `🚀 打包派发本屏 (${todo.length})` : "🚀 打包派发本屏";
  btn.disabled = usable.length === 0 || todo.length === 0;
  if (logBtn) {
    const activeCnt = dispatchJobs.filter(j => j.status === "running" || j.status === "queued").length;
    logBtn.style.display = dispatchJobs.length > 0 ? "inline-block" : "none";
    logBtn.textContent = activeCnt ? `⚡ 执行中 (${activeCnt})` : `日志 (${dispatchJobs.length})`;
  }
}

function toggleLogDrawer() {
  if (openLogJobId) {
    openLogJobId = null;
    activeLogJob = null;
    renderLogDrawer();
    return;
  }
  const latest = dispatchJobs[0];
  if (latest) openJobLog(latest.id);
}

async function openJobLog(jobId) {
  openLogJobId = jobId;
  try {
    const res = await fetch(api(`/api/dispatch/${encodeURIComponent(jobId)}`));
    if (res.ok) activeLogJob = await res.json();
  } catch (e) {}
  renderLogDrawer();
}

function renderLogDrawer() {
  const el = document.getElementById("logDrawer");
  if (!el) return;
  if (!openLogJobId || !activeLogJob) {
    el.style.display = "none";
    el.innerHTML = "";
    return;
  }
  const j = activeLogJob;
  const stMap = { queued: "排队中", running: "执行中", done: "已完成", failed: "失败", cancelled: "已取消" };
  const canStop = j.status === "queued" || j.status === "running";
  el.style.display = "flex";
  el.innerHTML = `
    <div class="log-drawer-head">
      <span class="job-pill ${j.status}">${j.agentLabel} · ${stMap[j.status] || j.status} · ${(j.annIds || []).join(",")}</span>
      <div style="display:flex;gap:4px;">
        ${canStop ? `<button class="mini-link" style="color:#fca5a5" onclick="cancelJob('${j.id}')">停止</button>` : ""}
        <button class="mini-link" onclick="toggleLogDrawer()">收起</button>
      </div>
    </div>
    <pre id="logDrawerPre"></pre>`;
  const pre = document.getElementById("logDrawerPre");
  if (pre) {
    pre.textContent = (j.logLines || []).join("\n") || "等待输出…";
    pre.scrollTop = pre.scrollHeight;
  }
}

async function dispatchAnnotations(annIds) {
  const ag = selectedAgentObj();
  if (!ag || !ag.available) { showToast("本机未检测到可用 Agent CLI"); return; }
  if (!annIds || !annIds.length) { showToast("没有可派发的批注"); return; }
  try {
    const res = await fetch(api("/api/dispatch"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        agent: ag.id,
        annIds,
        image: currentImage || "",
        appearance: currentAppearance
      })
    });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      showToast("派发失败：" + (data.error || res.status));
      return;
    }
    showToast(`已派发给 ${ag.label}（${annIds.join(", ")}）`);
    openLogJobId = data.job.id;
    await loadAgentsAndJobs();
    renderCards();
  } catch (e) {
    showToast("派发请求异常：" + e);
  }
}

function dispatchScreen() {
  const ids = screenDispatchableAnns().map(a => a.id);
  dispatchAnnotations(ids);
}

async function cancelJob(jobId) {
  await fetch(api(`/api/dispatch/${encodeURIComponent(jobId)}/cancel`), { method: "POST" });
  showToast("已请求停止任务");
  await loadAgentsAndJobs();
  renderCards();
}

// ---- 板子切换菜单：服务端登记了哪些板，这里就列哪些
let boardList = { boards: [], default: "" };

async function loadBoardMenu() {
  try {
    const res = await fetch("/api/boards");
    boardList = await res.json();
  } catch (e) {
    boardList = { boards: [], default: "" };
  }
  const menu = document.getElementById("boardMenu");
  if (!menu) return;
  menu.innerHTML = "";
  const current = state.name || "";
  for (const b of boardList.boards) {
    const btn = document.createElement("button");
    btn.className = "board-item" + (b.name === current ? " current" : "");
    btn.innerHTML = `<span>${b.name}</span>`
      + `<span class="path">${b.path || b.dir}</span>`
      + `<span class="meta">${b.images}+${b.after} 图 · ${b.annotations} 批注</span>`;
    btn.title = b.dir;
    btn.onclick = () => switchBoard(b.name);
    menu.appendChild(btn);
  }
  if (boardList.boards.length === 1) {
    const only = document.createElement("div");
    only.className = "board-item";
    only.style.cssText = "color:var(--muted);font-size:11.5px;cursor:default";
    only.textContent = "只有一块板。在别的目录再起一次服务，或 POST /api/boards 登记。";
    menu.appendChild(only);
  }
}

function toggleBoardMenu(ev) {
  if (ev) ev.stopPropagation();
  const menu = document.getElementById("boardMenu");
  if (!menu) return;
  menu.style.display = menu.style.display === "none" ? "block" : "none";
}

document.addEventListener("click", (e) => {
  const menu = document.getElementById("boardMenu");
  if (menu && menu.style.display !== "none" && !e.target.closest(".board-switch")) {
    menu.style.display = "none";
  }
  if (!e.target.closest(".more-wrap")) {
    document.querySelectorAll(".more-menu").forEach(m => { m.style.display = "none"; });
  }
});

function showToast(msg) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove("show"), 1600);
}

async function persist() {
  await fetch(api("/api/annotations"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ annotations: state.annotations })
  });
  showToast("已自动保存到 annotations.json");
}

function nextUserId() {
  let max = 0;
  for (const a of state.annotations) {
    const m = /^U(\d+)$/.exec(a.id || "");
    if (m) max = Math.max(max, parseInt(m[1], 10));
  }
  return "U" + (max + 1);
}

// ---- 截图列表筛选（和右侧「按批注状态筛批注」是两件事，不要混）
let imageQuery = "";
// 单选：null = 全部；否则只留含该状态批注的图。
// 之前做成多选并集，实测很坑：点亮「待处理 0」列表纹丝不动（因为还有别的胶囊生效），
// 看起来像按钮坏了。多选需求交给下面那个显式的「需处理」预设。
let statusFilter = null;

function bucketOf(a) {
  return (a.status === "approved") ? "open" : (a.status || "open");
}

// ---- 版本号：改前/改后可能来自不同 Build，各标各的
function verOf(img, side) {
  const v = state.versions || {};
  const ov = (v.overrides || {})[side] || {};
  if (side === "after") {
    // 改后图分轮时，版本号跟着"当前看的那一轮"走，不是跟着整批走
    const rb = roundBuild(img);
    const roundLabel = rb ? ((v.rounds || {})[rb] || "") : "";
    return ov[img] || roundLabel || v.after || "";
  }
  return ov[img] || v.before || "";
}

async function editVersions() {
  const v = state.versions || {};
  const before = prompt("改前这一批截图是哪个版本？（如 Build 233，留空=不标）", v.before || "");
  if (before === null) return;
  const after = prompt("改后这一批截图是哪个版本？（如 Build 237，留空=不标）", v.after || "");
  if (after === null) return;
  await fetch(api("/api/versions"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ before: before.trim(), after: after.trim() })
  });
  await init();
  showToast("版本号已保存到 versions.json");
}

function toggleStatusFilter(key) {
  statusFilter = (statusFilter === key) ? null : key;
  render();
}

function clearImageFilters() {
  imageQuery = "";
  statusFilter = null;
  const box = document.getElementById("imageSearch");
  if (box) box.value = "";
  render();
}

function onImageSearch(v) {
  imageQuery = (v || "").trim().toLowerCase();
  render();
}

function imageMatches(img) {
  if (statusFilter) {
    const anns = state.annotations.filter(a => a.image === img && appearanceOf(a) === currentAppearance);
    if (!anns.some(a => bucketOf(a) === statusFilter)) return false;
  }
  if (imageQuery) {
    const anns = state.annotations.filter(a => a.image === img && appearanceOf(a) === currentAppearance);
    const hay = (img + " " + anns.map(a => (a.text || "") + " " + (a.id || "")).join(" ")).toLowerCase();
    if (!hay.includes(imageQuery)) return false;
  }
  return true;
}

// 文件名 → 当前外观下有哪些版本。暗色与亮色彻底分槽，不再左右混排。
function pairState(img, app) {
  const mode = app || currentAppearance;
  const hasBefore = beforeImagesFor(mode).includes(img);
  const hasAfter = afterImagesFor(mode).includes(img);
  return { hasBefore, hasAfter };
}

// ---- 改后图分轮：同一个问题修了 234 不满意、235 再修，两轮截图都要在
// 暗色：after/234/、after/235/；亮色：after/light/234/、after/light/235/
let selectedRound = {};   // 图名 → 当前在看哪一轮

function roundsFor(img, app) {
  const mode = app || currentAppearance;
  const out = [];
  for (const r of afterRoundsFor(mode)) {
    if ((r.images || []).includes(img)) out.push(r.build || "");
  }
  return out;
}

function roundBuild(img, app) {
  const rs = roundsFor(img, app);
  if (!rs.length) return "";
  if (selectedRound[img] !== undefined && rs.includes(selectedRound[img])) return selectedRound[img];
  return rs[rs.length - 1];     // 默认看最新一轮
}

function beforeUrl(img, app) {
  const mode = app || currentAppearance;
  return mode === "light"
    ? api(`/images/light/${encodeURIComponent(img)}`)
    : api(`/images/${encodeURIComponent(img)}`);
}

function afterUrl(img, bust, app) {
  const mode = app || currentAppearance;
  const b = roundBuild(img, mode);
  const appSeg = mode === "light" ? "light/" : "";
  const path = b
    ? api(`/after/${appSeg}${encodeURIComponent(b)}/${encodeURIComponent(img)}`)
    : api(`/after/${appSeg}${encodeURIComponent(img)}`);
  return bust ? `${path}?t=${Date.now()}` : path;
}

// ---- 每条问题自己的修复轮次：a.rounds = [{build, status, rejectReason}]
// 同一个问题修了 234 被驳回、235 再修，两轮的结论都留着：
// 卡片上就是一行「234 驳回 / 235 待验收」，点哪颗看哪轮。
const ROUND_LABELS = { approved: "同意修", fixed: "待验收", accepted: "已通过",
                       rejected: "已驳回", discussed: "待探讨", ignored: "忽略" };

function roundsOf(a) {
  const list = Array.isArray(a.rounds) ? a.rounds.slice() : [];
  return list.sort((x, y) => (Number(x.build) || 0) - (Number(y.build) || 0));
}

function activeRound(a) {
  const rs = roundsOf(a);
  if (!rs.length) return null;
  const pick = rs.find(r => String(r.build) === String(roundBuild(a.image)));
  return pick || rs[rs.length - 1];
}

// 把"这一轮"的结论记下来：只有真有多轮时才建 rounds，
// 单轮的板子继续用 a.status，不多存一份会走偏的数据。
function markRound(a, status, reason) {
  const builds = roundsFor(a.image);
  if (builds.length < 2) { a.status = status; if (reason !== undefined) a.rejectReason = reason; return; }
  const build = roundBuild(a.image);
  if (!Array.isArray(a.rounds)) a.rounds = [];
  let row = a.rounds.find(r => String(r.build) === String(build));
  if (!row) { row = { build }; a.rounds.push(row); }
  row.status = status;
  if (reason !== undefined) row.rejectReason = reason;
  // 卡片总状态跟随最新一轮，顶部统计与列表仍看得到这张图的最新结论
  const latest = roundsOf(a)[roundsOf(a).length - 1];
  a.status = (latest && latest.status) || status;
  if (a.status === "rejected") a.rejectReason = (latest && latest.rejectReason) || a.rejectReason || "";
}

// 一个原始问题图，常有好几个问题，而且**分别修在不同 Build**：
//   01-fab-capture-listening.png → U2 修于 234，U36 修于 235
// 所以"这张改后图是哪个版本"根本回答不了问题。版本要分两层：
//   1) 图来自哪个 Build  → versions.json（整批 + 单张 override）
//   2) 哪个问题修在哪个 Build → 批注自己的 aiReply 里就写着「Build 234：…」
// 下面这两个函数读第 2 层，让每张卡、每条缩略图都能说清"哪些问题还没修"。
const BUILD_RE = /Build\s*(\d+)/i;

// 「0.1.0 (237)」→「237」。取最后一组数字：版本号里 0.1.0 的数字在前，构建号总在后面。
// 之前用 /^\D*(\d+)/ 取到的是 "0"（版本号第一位），于是每条都误判成"与整批不同"，
// 明明就是这个 build 修的也写一遍「修于 237」。
function buildNoOf(v) {
  const all = String(v || "").match(/\d+/g);
  return all ? all[all.length - 1] : "";
}

function fixedInOf(a) {
  if (a && a.fixedIn) return String(a.fixedIn).replace(/^\D+/, "");
  const m = BUILD_RE.exec((a.aiReply || "") + " " + (a.text || ""));
  return m ? m[1] : "";
}

function raisedInOf(a) {
  if (a && a.raisedIn) return String(a.raisedIn).replace(/^\D+/, "");
  return "";
}

function hasRoundImage(img, build, app) {
  if (!build) return false;
  const mode = app || currentAppearance;
  return afterRoundsFor(mode).some(r => String(r.build) === String(build) && (r.images || []).includes(img));
}

function urlForBuild(img, build, fallbackSide, bust, app) {
  const mode = app || currentAppearance;
  const appSeg = mode === "light" ? "light/" : "";
  let path;
  if (build && hasRoundImage(img, build, mode)) {
    path = api(`/after/${appSeg}${encodeURIComponent(build)}/${encodeURIComponent(img)}`);
  } else if (fallbackSide === "after" && afterImagesFor(mode).includes(img)) {
    return afterUrl(img, bust, mode);
  } else if (beforeImagesFor(mode).includes(img)) {
    path = beforeUrl(img, mode);
  } else {
    return afterUrl(img, bust, mode);
  }
  return bust ? `${path}?t=${Date.now()}` : path;
}

function verLabelForBuild(img, build, fallbackSide) {
  const v = state.versions || {};
  if (build) {
    const rl = (v.rounds || {})[build];
    if (rl) return rl;
    return `Build ${build}`;
  }
  return verOf(img, fallbackSide);
}

// 舞台左栏 =「提出问题的版本图」，右栏 =「修复问题的版本图」（严格限定在当前外观内）
function stageBuildsFor(img, app) {
  const mode = app || currentAppearance;
  const allAnns = state.annotations.filter(a => a.image === img && appearanceOf(a) === mode);
  const anns = allAnns.filter(matchesFilter);
  const focus = (selectedId && allAnns.find(a => a.id === selectedId)) || anns[0] || allAnns[0] || null;
  const rs = roundsFor(img, mode);
  if (focus && (raisedInOf(focus) || fixedInOf(focus))) {
    const leftB = raisedInOf(focus) || (rs.length ? rs[0] : buildNoOf(verOf(img, "before")));
    let rightB = "";
    let missingRightBuild = "";
    if (selectedRound[img] !== undefined && rs.includes(selectedRound[img]) && String(selectedRound[img]) !== String(leftB)) {
      rightB = selectedRound[img];
    } else {
      const fb = fixedInOf(focus);
      if (fb && hasRoundImage(img, fb, mode)) {
        rightB = fb;
      } else if (fb) {
        // 明确记录了修复版本，但该版本下尚无当前外观截图：给缺失占位，不拿另一外观顶替
        missingRightBuild = fb;
      } else {
        const newer = rs.filter(b => Number(b) > Number(leftB));
        if (newer.length) rightB = newer[newer.length - 1];
      }
    }
    const hasLeft = Boolean((leftB && hasRoundImage(img, leftB, mode)) || beforeImagesFor(mode).includes(img));
    const hasRight = Boolean(rightB && String(rightB) !== String(leftB) && hasRoundImage(img, rightB, mode));
    return { focus, leftBuild: leftB, rightBuild: rightB, missingRightBuild, hasLeft, hasRight };
  }
  const pair = pairState(img, mode);
  const leftB = pair.hasBefore ? buildNoOf(verOf(img, "before")) : (rs[0] || "");
  const rightB = roundBuild(img, mode);
  const hasLeft = pair.hasBefore;
  const hasRight = pair.hasAfter && (!hasLeft || !rightB || String(rightB) !== String(leftB));
  return { focus, leftBuild: leftB, rightBuild: rightB, missingRightBuild: "", hasLeft, hasRight };
}

function fixBuildsOf(img, app) {
  const mode = app || currentAppearance;
  const list = state.annotations.filter(a => a.image === img && appearanceOf(a) === mode).map(fixedInOf).filter(Boolean);
  return [...new Set(list)].sort((a, b) => Number(a) - Number(b));
}

function allImageNames(app) {
  const mode = app || currentAppearance;
  const seen = new Set();
  const out = [];
  for (const n of beforeImagesFor(mode)) { if (!seen.has(n)) { seen.add(n); out.push(n); } }
  for (const n of afterImagesFor(mode)) { if (!seen.has(n)) { seen.add(n); out.push(n); } }
  return out;
}

function render() {
  const darkCount = allImageNames("dark").length;
  const lightCount = allImageNames("light").length;
  const appDarkBtn = document.getElementById("appDarkBtn");
  const appLightBtn = document.getElementById("appLightBtn");
  if (appDarkBtn) {
    appDarkBtn.textContent = darkCount ? `🌙 暗色 ${darkCount}` : "🌙 暗色";
    appDarkBtn.classList.toggle("active", currentAppearance === "dark");
    appDarkBtn.classList.toggle("zero", darkCount === 0);
  }
  if (appLightBtn) {
    appLightBtn.textContent = lightCount ? `☀️ 亮色 ${lightCount}` : "☀️ 亮色";
    appLightBtn.classList.toggle("active", currentAppearance === "light");
    appLightBtn.classList.toggle("zero", lightCount === 0);
  }

  const appAnns = state.annotations.filter(a => appearanceOf(a) === currentAppearance);
  const counts = {
    open: appAnns.filter(a => bucketOf(a) === "open").length,
    fixed: appAnns.filter(a => bucketOf(a) === "fixed").length,
    accepted: appAnns.filter(a => bucketOf(a) === "accepted").length,
    rejected: appAnns.filter(a => bucketOf(a) === "rejected").length,
    discussed: appAnns.filter(a => bucketOf(a) === "discussed").length,
    ignored: appAnns.filter(a => bucketOf(a) === "ignored").length
  };
  for (const [key, elId] of [["open", "pillOpen"], ["fixed", "pillFixed"],
                             ["accepted", "pillAccepted"], ["rejected", "pillRejected"],
                             ["discussed", "pillDiscussed"]]) {
    const el = document.getElementById(elId);
    if (!el) continue;
    const label = {"open": "待处理", "fixed": "待验收", "accepted": "已通过",
                   "rejected": "已驳回", "discussed": "待探讨"}[key];
    el.textContent = `${label} ${counts[key]}`;
    // 单选：只有当前这一颗亮（多选会让「点了没反应」看起来像坏了）
    el.classList.toggle("active", statusFilter === key);
    el.classList.toggle("zero", counts[key] === 0);
    // 「待探讨」是长尾状态：没有就不占地方，有才出现
    if (key === "discussed") el.style.display = counts[key] > 0 ? "" : "none";
  }
  document.getElementById("scopeBtn").textContent = showAllScope ? "全部图" : "当前图";
  document.getElementById("scopeBtn").classList.toggle("active", showAllScope);
  document.getElementById("zoomBtn").classList.toggle("active", zoom100);
  document.getElementById("canvasWrap").classList.toggle("zoom100", zoom100);
  document.getElementById("afterWrap").classList.toggle("zoom100", zoom100);

  // 提出问题的版本图 vs 修复问题的版本图：严格限定在当前外观内
  const sb = currentImage ? stageBuildsFor(currentImage) : { focus: null, leftBuild: "", rightBuild: "", missingRightBuild: "", hasLeft: false, hasRight: false };
  const hasRightSlot = sb.hasRight || Boolean(sb.missingRightBuild);
  const both = sb.hasLeft && hasRightSlot;
  const cmpBtn = document.getElementById("compareBtn");
  cmpBtn.style.display = both ? "inline-block" : "none";
  cmpBtn.classList.toggle("active", both && compareMode);
  const showCompare = both && compareMode;

  const beforeCol = document.getElementById("canvasWrap").parentElement;
  const beforeLabel = document.getElementById("beforeLabel");
  const afterColEl = document.getElementById("afterCol");
  const afterLabel = document.getElementById("afterLabel");
  const afterImg = document.getElementById("afterImg");
  const afterWrap = document.getElementById("afterWrap");
  const afterMissing = document.getElementById("afterMissing");

  const showBeforeCol = sb.hasLeft;
  const showAfterCol = hasRightSlot && (!both || showCompare);
  beforeCol.style.display = showBeforeCol ? "flex" : "none";
  afterColEl.style.display = showAfterCol ? "flex" : "none";

  const leftWord = sb.focus ? "提出问题" : "改前";
  const rightWord = sb.focus ? "修复问题" : "改后";
  const modeCn = currentAppearance === "light" ? "亮色" : "暗色";
  if (both) {
    const vb = verLabelForBuild(currentImage, sb.leftBuild, "before");
    const va = verLabelForBuild(currentImage, sb.rightBuild || sb.missingRightBuild, "after");
    beforeLabel.style.display = "flex";
    beforeLabel.innerHTML = `${leftWord}${vb ? `<span class="ver">${vb}</span>` : ""}`;
    afterLabel.innerHTML = `${rightWord}${va ? `<span class="ver">${va}</span>` : ""}`;
    if (sb.hasRight && showCompare) {
      afterWrap.style.display = "inline-block";
      if (afterMissing) afterMissing.style.display = "none";
      afterImg.src = urlForBuild(currentImage, sb.rightBuild, "after", true);
    } else if (sb.missingRightBuild && showCompare) {
      afterWrap.style.display = "none";
      afterImg.removeAttribute("src");
      if (afterMissing) {
        afterMissing.style.display = "flex";
        afterMissing.innerHTML = `<strong>${va || ("Build " + sb.missingRightBuild)}</strong>`
          + `<span>暂无${modeCn}修复图</span>`
          + `<span style="font-size:11px;opacity:.75">补拍请运行：<br/>ujshot run --appearance ${currentAppearance} --board-as-after</span>`;
      }
    } else {
      afterWrap.style.display = "none";
      if (afterMissing) afterMissing.style.display = "none";
      afterImg.removeAttribute("src");
    }
  } else if (sb.hasLeft) {
    const vb = verLabelForBuild(currentImage, sb.leftBuild, "before");
    beforeLabel.style.display = "flex";
    beforeLabel.innerHTML = `${leftWord}${vb ? `<span class="ver">${vb}</span>` : ""}`;
    afterWrap.style.display = "none";
    if (afterMissing) afterMissing.style.display = "none";
    afterImg.removeAttribute("src");
  } else if (sb.hasRight) {
    const va = verLabelForBuild(currentImage, sb.rightBuild, "after");
    beforeLabel.style.display = "none";
    afterLabel.innerHTML = `${rightWord}${va ? `<span class="ver">${va}</span>` : ""}`;
    afterWrap.style.display = "inline-block";
    if (afterMissing) afterMissing.style.display = "none";
    afterImg.src = urlForBuild(currentImage, sb.rightBuild, "after", true);
  } else {
    beforeLabel.style.display = "none";
    afterWrap.style.display = "none";
    if (afterMissing) afterMissing.style.display = "none";
    afterImg.removeAttribute("src");
  }

  const listEl = document.getElementById("imageList");
  listEl.innerHTML = "";
  allNames = allImageNames(currentAppearance);
  const batchBefore = (state.versions || {}).before || "";
  const batchAfter = (state.versions || {}).after || "";
  let shown = 0;
  for (const img of allNames) {
    if (!imageMatches(img)) continue;
    shown++;
    const anns = state.annotations.filter(a => a.image === img && appearanceOf(a) === currentAppearance);
    const pr = pairState(img, currentAppearance);

    // 两图并排已经说明"有对照"，不再加「改前 + 改后」文字标签
    const thumbs = pr.hasBefore && pr.hasAfter
      ? `<div class="thumb-pair">
           <img src="${beforeUrl(img, currentAppearance)}" loading="lazy" alt="改前" />
           <img src="${afterUrl(img, false, currentAppearance)}" loading="lazy" alt="改后" />
         </div>`
      : `<div class="thumb-pair">
           <img class="${pr.hasAfter ? "only" : ""}" src="${pr.hasBefore ? beforeUrl(img, currentAppearance) : afterUrl(img, false, currentAppearance)}" loading="lazy" alt="${pr.hasBefore ? "改前" : "改后"}" />
         </div>`;

    // 第二行只放"布局解释不了"的东西：状态点 + 数量，必要时补版本/修复跨度
    const counts = {
      rejected: anns.filter(a => a.status === "rejected").length,
      fixed: anns.filter(a => a.status === "fixed").length,
      open: anns.filter(a => a.status === "open" || a.status === "approved").length,
      discussed: anns.filter(a => a.status === "discussed").length,
    };
    let dot = "accepted", label = `${anns.length}`;
    if (counts.rejected) { dot = "rejected"; label = `${counts.rejected}`; }
    else if (counts.fixed) { dot = "fixed"; label = `${counts.fixed}`; }
    else if (counts.open) { dot = "open"; label = `${counts.open}`; }
    else if (counts.discussed) { dot = "discussed"; label = `${counts.discussed}`; }

    // 版本只在"与整批不同"时出现：整批的版本顶端已经写着，重复三遍是噪音
    const vb = pr.hasBefore ? verOf(img, "before") : "";
    const ovAfter = ((state.versions || {}).overrides || {}).after || {};
    // 只对"单张 override"说这句话。分轮的版本是轮次条在讲的事，
    // 这里再写一遍「改后 (234)」就跟轮次胶囊重复了。
    const va = ovAfter[img] || "";
    const oddVer = [];
    if (vb && batchBefore && vb !== batchBefore) oddVer.push(`改前 ${vb.replace(/^.*\(/, "(").replace(/\)$/, ")")}`);
    if (va && batchAfter && va !== batchAfter) oddVer.push(`改后 ${va.replace(/^.*\(/, "(")}`);

    // 修复跨度只有真跨版本（234 / 235）才值得说；单一版本从整批版本就能推出来
    const fixBuilds = fixBuildsOf(img);
    const unfixed = anns.filter(a => a.status !== "accepted" && a.status !== "ignored" && !fixedInOf(a)).length;
    const extra = [];
    // 分轮的板子：轮次目录本身就是"修了几次"的真相，比 aiReply 里的 Build 字样更可靠
    const rounds = roundsFor(img);
    if (rounds.length > 1) {
      extra.push(`${rounds[0]}→${rounds[rounds.length - 1]}`);
    } else if (fixBuilds.length > 1) {
      extra.push(`${fixBuilds[0]}→${fixBuilds[fixBuilds.length - 1]}`);
    } else if (fixBuilds.length === 1 && batchAfter && fixBuilds[0] !== buildNoOf(batchAfter)) {
      // 只有"这一版改后图不代表它"时才写；等于整批版本时写了也是重复
      extra.push(`修于 ${fixBuilds[0]}`);
    }
    if (unfixed) extra.push(`${unfixed} 未修`);
    if (oddVer.length) extra.push(...oddVer);

    const meta = anns.length
      ? `<div class="thumb-line"><span class="dot ${dot}"></span><span>${label}</span>`
        + (extra.length ? `<span class="thumb-sep">·</span><span>${extra.join(" · ")}</span>` : "")
        + `</div>`
      : `<div class="thumb-line"><span style="opacity:.45">—</span>`
        + (extra.length ? `<span class="thumb-sep">·</span><span>${extra.join(" · ")}</span>` : "")
        + `</div>`;

    const div = document.createElement("div");
    div.className = "thumb-item" + (img === currentImage ? " selected" : "");
    div.onclick = () => {
      currentImage = img;
      selectedId = null;
      closePopover();
      if (boardMode === "operate") setBoardMode("annotate", true);
      render();
    };
    div.innerHTML = `
      ${thumbs}
      <div class="thumb-meta">
        <div class="thumb-name" title="${img}">${img.replace(/\.png$/i, "")}</div>
        ${meta}
      </div>`;
    listEl.appendChild(div);
  }

  // 0 张就给一句话 + 一个能点的出口，不再写两行解释
  if (shown === 0) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.innerHTML = `没有匹配的截图<br/><button class="mini-link" onclick="clearImageFilters()">清除筛选</button>`;
    listEl.appendChild(empty);
  }
  const cntEl = document.getElementById("imageCount");
  if (cntEl) {
    cntEl.textContent = shown === allNames.length
      ? `${allNames.length}`
      : `${shown}/${allNames.length}`;
  }
  const vbEl = document.getElementById("verBefore"), vaEl = document.getElementById("verAfter");
  const vbTop = (state.versions || {}).before || "", vaTop = (state.versions || {}).after || "";
  if (vbEl) {
    vbEl.textContent = vbTop || "改前 —";
    vbEl.classList.toggle("empty", !vbTop);
    vbEl.title = vbTop ? `改前这批来自 ${vbTop}（点这里改）` : "还没标改前版本（点这里设置）";
  }
  if (vaEl) {
    vaEl.textContent = vaTop || "改后 —";
    vaEl.classList.toggle("empty", !vaTop);
    vaEl.title = vaTop ? `改后这批来自 ${vaTop}（点这里改）` : "还没标改后版本（点这里设置）";
  }

  const stageImg = document.getElementById("stageImg");
  if (currentImage && sb.hasLeft) {
    stageImg.src = urlForBuild(currentImage, sb.leftBuild, "before", false);
  }
  const hintEl = document.getElementById("stageHint");
  if (hintEl) {
    if (!currentImage || both) hintEl.textContent = "";
    else if (sb.focus && sb.hasLeft && !sb.hasRight) hintEl.textContent = "待修复（尚无新版本修复图）";
    else if (sb.hasRight) hintEl.textContent = "只有改后";
    else hintEl.textContent = "只有改前";
  }
  // 隐藏列的图不参与命中测试：否则点隐藏区会落在看不见的图上
  document.getElementById("afterWrap").style.pointerEvents = (sb.hasRight && showAfterCol) ? "auto" : "none";
  document.getElementById("canvasWrap").style.pointerEvents = showBeforeCol ? "auto" : "none";

  renderOverlay();
  renderDispatchBar();
  renderLogDrawer();
  renderCards();
}

function renderOverlay() {
  const ovBefore = document.getElementById("overlay");
  const ovAfter = document.getElementById("afterOverlay");
  ovBefore.innerHTML = "";
  if (ovAfter) ovAfter.innerHTML = "";
  if (!currentImage) return;
  const sb = stageBuildsFor(currentImage);
  const showAfterCol = sb.hasRight && (!(sb.hasLeft && (sb.hasRight || sb.missingRightBuild)) || compareMode);
  const anns = state.annotations.filter(a => a.image === currentImage && appearanceOf(a) === currentAppearance && matchesFilter(a));
  for (const a of anns) {
    // 标注跟着版本的图片走：只画在它被提出的那个版本图上，避免不同版本的坐标错位
    const rb = raisedInOf(a);
    let ov = null;
    if (rb) {
      if (sb.hasLeft && String(rb) === String(sb.leftBuild)) ov = ovBefore;
      else if (showAfterCol && ovAfter && String(rb) === String(sb.rightBuild)) ov = ovAfter;
      else continue;
    } else {
      ov = (a.target === "after" && ovAfter && showAfterCol) ? ovAfter : ovBefore;
    }
    const cls = `${a.author} ${a.status}` + (a.id === selectedId ? " active" : "");
    if (a.kind === "box" && a.w > 0.01 && a.h > 0.01) {
      const box = document.createElement("div");
      box.className = "box-mark " + cls;
      box.style.left = (a.x * 100) + "%";
      box.style.top = (a.y * 100) + "%";
      box.style.width = (a.w * 100) + "%";
      box.style.height = (a.h * 100) + "%";
      box.innerHTML = `<span class="box-tag">${a.id}</span>`;
      box.onclick = (e) => { e.stopPropagation(); selectAnn(a.id); };
      ov.appendChild(box);
    } else {
      const pin = document.createElement("div");
      pin.className = "pin " + cls;
      pin.style.left = (a.x * 100) + "%";
      pin.style.top = (a.y * 100) + "%";
      pin.textContent = a.id;
      pin.onclick = (e) => { e.stopPropagation(); selectAnn(a.id); };
      ov.appendChild(pin);
    }
  }
}

function renderCards() {
  const container = document.getElementById("annList");
  container.innerHTML = "";

  const scoped = showAllScope
    ? state.annotations.filter(a => appearanceOf(a) === currentAppearance)
    : state.annotations.filter(a => a.image === currentImage && appearanceOf(a) === currentAppearance);

  const items = scoped.filter(matchesFilter);

  if (items.length === 0) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.innerHTML = scoped.length === 0
      ? `当前暂无批注。<br/>直接在中间截图上<b>单击落图钉</b>或<b>拖框圈选</b>即可添加。`
      : `没有匹配的批注<br/><button class="mini-link" onclick="clearImageFilters()">清除筛选</button>`;
    container.appendChild(empty);
    return;
  }

  const STATUS_TEXT = {
    open: "待处理", approved: "待处理", fixed: "待验收",
    accepted: "已通过", rejected: "已驳回", discussed: "待探讨", ignored: "已忽略"
  };

  for (const a of items) {
    const rb = raisedInOf(a);
    const fb = fixedInOf(a);
    const rounds = roundsOf(a);
    const round = activeRound(a);
    const curStatus = (round && round.status) || a.status || "open";
    const curReason = (round && round.rejectReason !== undefined) ? round.rejectReason : a.rejectReason;
    const settled = curStatus === "accepted" || curStatus === "ignored";
    const job = activeJobForAnn(a.id);

    const card = document.createElement("div");
    card.className = "card" + (a.id === selectedId ? " active" : "") + (settled ? " settled" : "");
    card.id = "card-" + a.id;
    card.onclick = () => {
      const app = appearanceOf(a);
      if (app !== currentAppearance) currentAppearance = app;
      if (a.image !== currentImage) currentImage = a.image;
      delete selectedRound[a.image];
      selectedId = a.id;
      render();
    };

    const verText = (rb && fb) ? `${rb} → ${fb}`
      : (fb ? `修于 ${fb}` : (rb ? `Build ${rb}` : ""));
    const imgPrefix = showAllScope ? (a.image.replace(/\.png$/i, "") + " · ") : "";

    const candidateRounds = rounds.length >= 2 ? rounds.map(r => String(r.build)) : [];
    const activeRightB = stageBuildsFor(a.image).rightBuild;
    const roundBar = candidateRounds.length >= 2
      ? `<div class="round-bar">` + candidateRounds.map(b => {
          const row = rounds.find(r => String(r.build) === String(b));
          const st = row && row.status;
          const cls = "round-chip" + (String(b) === String(activeRightB) ? " active" : "")
            + (st ? " on-" + st : "");
          return `<button class="${cls}" data-build="${b}" title="${st ? ROUND_LABELS[st] : "点此查看这一版修复图"}">`
            + `Build ${b || "本地"}${st ? ` · ${ROUND_LABELS[st]}` : ""}</button>`;
        }).join("") + `</div>`
      : "";

    card.innerHTML = `
      <div class="card-head">
        <div class="card-id">
          <span title="${a.id}">${a.id}</span>
          <span class="st-badge st-${curStatus}">${STATUS_TEXT[curStatus] || curStatus}</span>
          ${a.author === "ai" ? `<span class="badge ai">AI</span>` : ""}
        </div>
        <div class="card-right">
          ${(imgPrefix || verText) ? `<span class="ver-meta">${imgPrefix}${verText}</span>` : ""}
          <div class="more-wrap">
            <button class="more-btn" title="更多操作">⋯</button>
            <div class="more-menu" style="display:none;"></div>
          </div>
        </div>
      </div>${roundBar}`;

    // 右上角「⋯」更多菜单：收拢编辑、低频状态切换与删除，不干扰主流程
    const moreBtn = card.querySelector(".more-btn");
    const moreMenu = card.querySelector(".more-menu");
    moreBtn.onclick = (e) => {
      e.stopPropagation();
      const wasOpen = moreMenu.style.display === "flex";
      document.querySelectorAll(".more-menu").forEach(m => { m.style.display = "none"; });
      moreMenu.style.display = wasOpen ? "none" : "flex";
    };
    const addMenuItem = (label, fn, cls) => {
      const btn = document.createElement("button");
      btn.textContent = label;
      if (cls) btn.className = cls;
      btn.onclick = (e) => { e.stopPropagation(); moreMenu.style.display = "none"; fn(); };
      moreMenu.appendChild(btn);
    };
    addMenuItem("编辑批注", () => { editingNoteIds.add(a.id); renderCards(); });
    if (curStatus !== "open" && curStatus !== "approved") {
      addMenuItem("重新打开（待处理）", () => { markRound(a, "approved", ""); persist(); render(); });
    }
    if (curStatus !== "discussed") {
      addMenuItem("标记待探讨", () => { markRound(a, "discussed"); persist(); render(); });
    }
    if (curStatus !== "ignored") {
      addMenuItem("忽略此条", () => { markRound(a, "ignored"); persist(); render(); });
    }
    const agMenu = selectedAgentObj();
    if (settled && agMenu && agMenu.available && !job) {
      addMenuItem(`派给 ${agMenu.label} 重修`, () => { dispatchAnnotations([a.id]); });
    }
    moreMenu.appendChild(document.createElement("hr"));
    addMenuItem("删除批注", () => {
      state.annotations = state.annotations.filter(x => x.id !== a.id);
      if (selectedId === a.id) selectedId = null;
      persist();
      render();
    }, "danger");

    // 轮次胶囊：点哪颗就把舞台、缩略图、这一行的结论一起切到那一轮
    card.querySelectorAll(".round-chip").forEach(chip => {
      chip.onclick = (e) => {
        e.stopPropagation();
        selectedRound[a.image] = chip.dataset.build;
        selectedId = a.id;
        render();
      };
    });

    // 问题描述：正文直接呈现，双击或从「⋯」进入编辑
    if (editingNoteIds.has(a.id) || !(a.text || "").trim()) {
      const wrap = document.createElement("div");
      wrap.className = "edit-field-wrap";
      wrap.onclick = e => e.stopPropagation();
      const txt = document.createElement("textarea");
      txt.value = a.text || "";
      txt.placeholder = "输入问题描述...";
      txt.oninput = () => autoSizeTextarea(txt);
      const act = document.createElement("div");
      act.className = "edit-field-actions";
      const saveB = document.createElement("button");
      saveB.className = "primary";
      saveB.textContent = "保存";
      saveB.onclick = () => {
        a.text = txt.value.trim();
        editingNoteIds.delete(a.id);
        persist();
        renderCards();
      };
      const cancelB = document.createElement("button");
      cancelB.textContent = "取消";
      cancelB.onclick = () => { editingNoteIds.delete(a.id); renderCards(); };
      act.appendChild(cancelB);
      act.appendChild(saveB);
      wrap.appendChild(txt);
      wrap.appendChild(act);
      card.appendChild(wrap);
      setTimeout(() => autoSizeTextarea(txt), 0);
    } else {
      const body = document.createElement("div");
      body.className = "note-text";
      body.textContent = a.text;
      body.title = "双击可编辑";
      body.ondblclick = (e) => { e.stopPropagation(); editingNoteIds.add(a.id); renderCards(); };
      card.appendChild(body);
    }

    if (a.aiReply) {
      const box = document.createElement("div");
      box.className = "ai-reply-box";
      box.innerHTML = `<div class="ai-reply-title"><span>AI 回复</span></div>`;
      const body = document.createElement("div");
      body.textContent = a.aiReply;
      box.appendChild(body);
      card.appendChild(box);
    }

    if (curStatus === "rejected" || openRejectEditors.has(a.id)) {
      const rbox = document.createElement("div");
      rbox.className = "reject-box";
      rbox.onclick = e => e.stopPropagation();
      const isEditingRej = openRejectEditors.has(a.id) || !(curReason || "").trim();
      const rhead = document.createElement("div");
      rhead.className = "reject-box-title";
      rhead.innerHTML = `<span>驳回原因${isEditingRej ? "（输入中）" : ""}</span>`;
      if (!isEditingRej) {
        const eb = document.createElement("button");
        eb.className = "mini-link";
        eb.style.color = "#fca5a5";
        eb.textContent = "修改";
        eb.onclick = () => { openRejectEditors.add(a.id); renderCards(); };
        rhead.appendChild(eb);
      }
      rbox.appendChild(rhead);

      if (isEditingRej) {
        const rtxt = document.createElement("textarea");
        rtxt.id = "rej-txt-" + a.id;
        rtxt.value = curReason || "";
        rtxt.placeholder = "写清楚哪里还不满意、希望怎么改...";
        rtxt.oninput = () => autoSizeTextarea(rtxt);
        const ract = document.createElement("div");
        ract.className = "edit-field-actions";
        const rsave = document.createElement("button");
        rsave.className = "primary";
        rsave.textContent = "保存驳回原因";
        rsave.onclick = () => {
          const reason = rtxt.value.trim();
          markRound(a, "rejected", reason);
          a.rejectReason = reason;
          openRejectEditors.delete(a.id);
          persist();
          renderCards();
        };
        ract.appendChild(rsave);
        rbox.appendChild(rtxt);
        rbox.appendChild(ract);
        setTimeout(() => autoSizeTextarea(rtxt), 0);
      } else {
        const rbody = document.createElement("div");
        rbody.className = "reject-box-body";
        rbody.textContent = curReason;
        rbox.appendChild(rbody);
      }
      card.appendChild(rbox);
    }

    if (a.author === "ai" && a.reply) {
      const urep = document.createElement("div");
      urep.className = "note-display";
      urep.innerHTML = `<div class="note-display-head"><span>你的补充意见</span></div>`;
      const ub = document.createElement("div");
      ub.textContent = a.reply;
      urep.appendChild(ub);
      card.appendChild(urep);
    }

    // 底部操作区：严格按当前状态只显示用户此刻真正需要点的 1～2 个按钮
    const actions = document.createElement("div");
    actions.className = "card-actions";
    if (job) {
      const pill = document.createElement("button");
      pill.className = "job-pill " + job.status;
      pill.textContent = job.status === "queued" ? `⏳ ${job.agentLabel} 排队中` : `⚡ ${job.agentLabel} 执行中…`;
      pill.onclick = (e) => { e.stopPropagation(); openJobLog(job.id); };
      actions.appendChild(pill);
      const stopBtn = document.createElement("button");
      stopBtn.className = "btn-stop";
      stopBtn.textContent = "停止";
      stopBtn.onclick = (e) => { e.stopPropagation(); cancelJob(job.id); };
      actions.appendChild(stopBtn);
    } else if (curStatus === "fixed") {
      const accBtn = document.createElement("button");
      accBtn.className = "btn-accept";
      accBtn.textContent = "✓ 通过";
      accBtn.onclick = (e) => {
        e.stopPropagation();
        markRound(a, "accepted", "");
        openRejectEditors.delete(a.id);
        persist();
        render();
      };
      const rejBtn = document.createElement("button");
      rejBtn.className = "btn-reject";
      rejBtn.textContent = "↩ 驳回";
      rejBtn.onclick = (e) => {
        e.stopPropagation();
        openRejectEditors.add(a.id);
        markRound(a, "rejected");
        persist();
        render();
        setTimeout(() => {
          const el = document.getElementById("rej-txt-" + a.id);
          if (el) { autoSizeTextarea(el); el.focus(); }
        }, 30);
      };
      actions.appendChild(accBtn);
      actions.appendChild(rejBtn);
    } else if (curStatus === "open" || curStatus === "approved" || curStatus === "rejected") {
      const ag = selectedAgentObj();
      if (ag && ag.available) {
        const runBtn = document.createElement("button");
        runBtn.className = "btn-dispatch";
        runBtn.textContent = curStatus === "rejected" ? `🚀 派给 ${ag.label} 重修` : `🚀 派给 ${ag.label}`;
        runBtn.onclick = (e) => { e.stopPropagation(); dispatchAnnotations([a.id]); };
        actions.appendChild(runBtn);
      }
    }
    if (actions.childElementCount > 0) {
      card.appendChild(actions);
    }
    container.appendChild(card);
  }
}

function selectAnn(id) {
  selectedId = id;
  render();
  const el = document.getElementById("card-" + id);
  if (el) el.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function bindCanvasAnnotation(wrapEl, overlayId, targetName) {
  if (!wrapEl) return;
  wrapEl.addEventListener("mousedown", (e) => {
    if (e.button !== 0 || e.target.closest(".pin, .box-mark")) return;
    const rect = wrapEl.getBoundingClientRect();
    dragStart = {
      clientX: e.clientX, clientY: e.clientY,
      x: (e.clientX - rect.left) / rect.width,
      y: (e.clientY - rect.top) / rect.height,
      rect, overlayId, target: targetName
    };
  });
}
bindCanvasAnnotation(document.getElementById("canvasWrap"), "overlay", "before");
bindCanvasAnnotation(document.getElementById("afterWrap"), "afterOverlay", "after");

window.addEventListener("mousemove", (e) => {
  if (!dragStart) return;
  const dx = Math.abs(e.clientX - dragStart.clientX);
  const dy = Math.abs(e.clientY - dragStart.clientY);
  if (dx > 10 || dy > 10) {
    if (!draftEl) {
      draftEl = document.createElement("div");
      draftEl.className = "draft-box";
      document.getElementById(dragStart.overlayId).appendChild(draftEl);
    }
    const r = dragStart.rect;
    const x2 = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
    const y2 = Math.max(0, Math.min(1, (e.clientY - r.top) / r.height));
    const x = Math.min(dragStart.x, x2), y = Math.min(dragStart.y, y2);
    const w = Math.abs(x2 - dragStart.x), h = Math.abs(y2 - dragStart.y);
    draftEl.style.left = (x * 100) + "%";
    draftEl.style.top = (y * 100) + "%";
    draftEl.style.width = (w * 100) + "%";
    draftEl.style.height = (h * 100) + "%";
  }
});

window.addEventListener("mouseup", (e) => {
  if (!dragStart) return;
  const r = dragStart.rect;
  const dx = Math.abs(e.clientX - dragStart.clientX);
  const dy = Math.abs(e.clientY - dragStart.clientY);
  const x2 = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
  const y2 = Math.max(0, Math.min(1, (e.clientY - r.top) / r.height));

  if (dx > 12 || dy > 12) {
    pendingDraft = {
      kind: "box",
      target: dragStart.target,
      x: Number(Math.min(dragStart.x, x2).toFixed(4)),
      y: Number(Math.min(dragStart.y, y2).toFixed(4)),
      w: Number(Math.abs(x2 - dragStart.x).toFixed(4)),
      h: Number(Math.abs(y2 - dragStart.y).toFixed(4)),
    };
  } else {
    pendingDraft = {
      kind: "pin",
      target: dragStart.target,
      x: Number(Math.max(0, Math.min(1, dragStart.x)).toFixed(4)),
      y: Number(Math.max(0, Math.min(1, dragStart.y)).toFixed(4)),
      w: 0, h: 0
    };
  }
  dragStart = null;
  openPopover(e.clientX, e.clientY);
});

function openPopover(cx, cy) {
  const pop = document.getElementById("popover");
  const id = nextUserId();
  const side = pendingDraft.target === "after" ? "新图 · " : "";
  document.getElementById("popTitle").textContent =
    `${id} · ${side}${pendingDraft.kind === "box" ? "框选批注" : "图钉批注"} (${Math.round(pendingDraft.x*100)}%, ${Math.round(pendingDraft.y*100)}%)`;
  pop.style.left = Math.min(window.innerWidth - 310, Math.max(12, cx + 12)) + "px";
  pop.style.top = Math.min(window.innerHeight - 160, Math.max(60, cy + 12)) + "px";
  pop.style.display = "block";
  const ta = document.getElementById("popText");
  ta.value = "";
  setTimeout(() => ta.focus(), 10);
}

function closePopover() {
  document.getElementById("popover").style.display = "none";
  pendingDraft = null;
  if (draftEl) { draftEl.remove(); draftEl = null; }
}

function savePopover() {
  const text = document.getElementById("popText").value.trim();
  if (!text || !pendingDraft) { closePopover(); return; }
  const id = nextUserId();
  const sb = stageBuildsFor(currentImage);
  const clickedBuild = (pendingDraft.target === "after")
    ? (sb.rightBuild || buildNoOf(verOf(currentImage, "after")))
    : (sb.leftBuild || buildNoOf(verOf(currentImage, "before")));
  state.annotations.push({
    id,
    image: currentImage,
    target: "before",
    appearance: currentAppearance,
    raisedIn: clickedBuild || undefined,
    author: "user",
    kind: pendingDraft.kind,
    x: pendingDraft.x,
    y: pendingDraft.y,
    w: pendingDraft.w,
    h: pendingDraft.h,
    text,
    reply: "",
    status: "approved",
    createdAt: new Date().toISOString()
  });
  selectedId = id;
  closePopover();
  persist();
  render();
}

document.getElementById("popText").addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); savePopover(); }
  if (e.key === "Escape") closePopover();
});

window.addEventListener("keydown", (e) => {
  if (["TEXTAREA", "INPUT"].includes(document.activeElement.tagName)) return;
  if (e.key === "Escape") closePopover();
  const navNames = allImageNames(currentAppearance);
  const idx = navNames.indexOf(currentImage);
  if (e.key === "ArrowRight" && idx >= 0 && idx < navNames.length - 1) {
    currentImage = navNames[idx + 1]; selectedId = null; closePopover(); render();
  }
  if (e.key === "ArrowLeft" && idx > 0) {
    currentImage = navNames[idx - 1]; selectedId = null; closePopover(); render();
  }
});

function toggleScope() { showAllScope = !showAllScope; render(); }
function toggleZoom() { zoom100 = !zoom100; render(); }
function toggleCompare() { compareMode = !compareMode; render(); }

async function copyForAI() {
  const active = state.annotations.filter(a => a.status === "open" || a.status === "approved");
  const lines = [
    `请按以下标注修改界面（完整 JSON：\`${state.dir}/annotations.json\`）：`,
    ""
  ];
  for (const a of active) {
    const pos = a.kind === "box"
      ? `框选(${Math.round(a.x*100)}%,${Math.round(a.y*100)}% ${Math.round(a.w*100)}×${Math.round(a.h*100)}%)`
      : `坐标(${Math.round(a.x*100)}%,${Math.round(a.y*100)}%)`;
    const tag = a.status === "approved" ? "[已确认]" : "[待处理]";
    const extra = a.reply ? ` ｜补充：${a.reply}` : "";
    lines.push(`- **${a.id}** ${tag} \`${a.image}\` ${pos}：${a.text}${extra}`);
  }
  if (active.length === 0) lines.push("（当前无待处理或已确认标注）");
  await navigator.clipboard.writeText(lines.join("\n"));
  showToast("已复制 Markdown 批注到剪贴板，直接发给 AI 即可");
}

let boardMode = "annotate";
let simInfo = { available: false };
let liveTimer = null;
let liveFetching = false;
let liveDragStart = null;

async function initSim() {
  try {
    const res = await fetch(api("/api/sim/status"));
    simInfo = await res.json();
    if (simInfo.available) {
      document.getElementById("modeSwitch").style.display = "inline-flex";
    }
  } catch (e) {}
}

function setBoardMode(mode, skipAutoFreeze = false) {
  if (mode === "operate" && !simInfo.available) {
    showToast("未检测到运行中的 iOS 模拟器");
    return;
  }
  if (mode === "annotate" && boardMode === "operate" && !skipAutoFreeze) {
    freezeAndAnnotate();
    return;
  }
  boardMode = mode;
  const isOp = mode === "operate";
  document.getElementById("modeOperateBtn").classList.toggle("active", isOp);
  document.getElementById("modeAnnotateBtn").classList.toggle("active", !isOp);
  document.getElementById("liveStage").style.display = isOp ? "flex" : "none";
  document.getElementById("annotateStage").style.display = isOp ? "none" : "flex";
  if (isOp) {
    closePopover();
    startLivePolling();
  } else {
    stopLivePolling();
  }
}

async function fetchLiveFrame() {
  if (liveFetching || boardMode !== "operate") return;
  liveFetching = true;
  try {
    const res = await fetch(api("/api/sim/frame?t=" + Date.now()));
    if (res.ok) {
      const blob = await res.blob();
      const img = document.getElementById("liveImg");
      const prev = img.dataset.blobUrl;
      const url = URL.createObjectURL(blob);
      img.src = url;
      img.dataset.blobUrl = url;
      if (prev) URL.revokeObjectURL(prev);
    }
  } catch (e) {}
  liveFetching = false;
}

function startLivePolling() {
  stopLivePolling();
  fetchLiveFrame();
  liveTimer = setInterval(fetchLiveFrame, 450);
}

function stopLivePolling() {
  if (liveTimer) { clearInterval(liveTimer); liveTimer = null; }
}

function showTapRipple(wrap, xNorm, yNorm) {
  const r = document.createElement("div");
  r.className = "tap-ripple";
  r.style.left = (xNorm * 100) + "%";
  r.style.top = (yNorm * 100) + "%";
  wrap.appendChild(r);
  setTimeout(() => r.remove(), 450);
}

(function bindLiveTouch() {
  const wrap = document.getElementById("liveWrap");
  wrap.addEventListener("pointerdown", (e) => {
    if (boardMode !== "operate") return;
    const rect = wrap.getBoundingClientRect();
    liveDragStart = {
      px: e.clientX, py: e.clientY,
      x: Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width)),
      y: Math.min(1, Math.max(0, (e.clientY - rect.top) / rect.height)),
    };
  });
  wrap.addEventListener("pointerup", async (e) => {
    if (!liveDragStart || boardMode !== "operate") return;
    const start = liveDragStart;
    liveDragStart = null;
    const rect = wrap.getBoundingClientRect();
    const x2 = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
    const y2 = Math.min(1, Math.max(0, (e.clientY - rect.top) / rect.height));
    const dist = Math.hypot(e.clientX - start.px, e.clientY - start.py);
    if (dist > 14) {
      showToast("滑动中…");
      await fetch(api("/api/sim/swipe"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ x1Norm: start.x, y1Norm: start.y, x2Norm: x2, y2Norm: y2 })
      });
    } else {
      showTapRipple(wrap, x2, y2);
      await fetch(api("/api/sim/tap"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ xNorm: x2, yNorm: y2 })
      });
    }
    setTimeout(fetchLiveFrame, 180);
  });
})();

async function sendSimText() {
  const inp = document.getElementById("simTypeInput");
  const text = inp.value;
  if (!text) return;
  await fetch(api("/api/sim/type"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text })
  });
  inp.value = "";
  showToast("已发送到模拟器");
  setTimeout(fetchLiveFrame, 200);
}

async function sendSimHome() {
  await fetch(api("/api/sim/home"), { method: "POST" });
  setTimeout(fetchLiveFrame, 250);
}

async function launchSimApp() {
  await fetch(api("/api/sim/launch"), { method: "POST" });
  showToast("正在打开 App…");
  setTimeout(fetchLiveFrame, 600);
}

async function freezeAndAnnotate() {
  showToast("正在定格高清画面…");
  const res = await fetch(api("/api/sim/freeze"), { method: "POST" });
  const data = await res.json();
  if (!data.ok) { showToast("定格失败：" + (data.error || "")); return; }
  state.images = data.images || state.images;
  currentImage = data.image;
  selectedId = null;
  setBoardMode("annotate", true);
  render();
  showToast(`已定格当前模拟器画面（${data.image}），直接画框/插图钉批注即可`);
}

init();
initSim();
</script>
</body>
</html>
"""


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
            body = HTML_PAGE.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
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
                # /images/<name>.png            → 暗色改前
                # /images/light/<name>.png      → 亮色改前
                # /after/<name>.png             → 暗色最新一轮
                # /after/234/<name>.png         → 暗色第 234 版
                # /after/light/<name>.png       → 亮色最新一轮
                # /after/light/234/<name>.png   → 亮色第 234 版
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
                # 登记一块新板：不重启服务，刷新页面就能看到
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
                    job = mgr.enqueue(directory, proj, spec, chosen, appearance=appearance, image=image)
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
                saved = save_annotations(directory, anns)
                self._send_json({"ok": True, "updatedAt": saved["updatedAt"]})
                return

            if path == "/api/versions":
                # 版本号是旁挂信息，和批注分开存：清批注不该把"这是哪个 Build"抹掉
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
                    sim.launch_app()
                    self._send_json({"ok": True})
                    return
                if path == "/api/sim/freeze":
                    img_name = sim.freeze_png(directory)
                    self._send_json({"ok": True, "image": img_name, "images": list_images(directory)})
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
    """清单跟着项目走：落在 <项目>/.ujshot/boards.json。

    一块板就是某个 repo 的截图，清单属于这个 repo，换台机器 clone 下来
    还在同一个位置；没有项目根时才退到 ~/.ujshot/。
    """
    root = project_root(start)
    if root:
        return root / ".ujshot" / REGISTRY_NAME
    return Path.home() / ".ujshot" / REGISTRY_NAME


LEGACY_REGISTRY = Path.home() / ".ujshot" / REGISTRY_NAME


def migrate_legacy_registry(target: Path) -> int:
    """旧版清单在 ~/.ujshot/。项目版还没有时把它搬过来，别让板子凭空消失。

    只搬"目录还在"的条目；搬完把旧文件改名留档，免得下次又搬一遍。
    """
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

    # 端口和 bundle 优先用项目自己的 ujshot.toml，免得每个项目各写一遍
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

    # 先看目标端口上是不是已经有一个服务在跑：有就把板子登记进去，
    # 不再起第二个进程、不再抢第二个端口。
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

    # 没有在跑：用清单里已有的板子起服务（新目录也并进去）
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