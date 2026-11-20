"""Thin wrappers over `xcrun simctl` and `axe`. No policy here — just commands.

Every function raises ``ToolError`` with an actionable message when the
underlying tool is missing or the device is gone, so callers can decide whether
to degrade or abort.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time

from .types import Device


class ToolError(RuntimeError):
    pass


def run(args, timeout=240):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, "", f"timeout after {timeout}s")
    except FileNotFoundError as exc:
        raise ToolError(f"找不到命令：{args[0]}（{exc}）") from exc


def which_axe() -> str:
    found = shutil.which("axe")
    if found:
        return found
    for guess in ("/opt/homebrew/bin/axe", "/usr/local/bin/axe"):
        if os.path.exists(guess):
            return guess
    raise ToolError(
        "找不到 axe。装法：brew install cameroncooke/axe/axe\n"
        "  或者 `axe init` 后按提示安装。没有 axe 就无法读取 AX 树，只剩坐标点击。"
    )


def require_xcrun() -> None:
    if not shutil.which("xcrun"):
        raise ToolError("找不到 xcrun：需要安装 Xcode 命令行工具（xcode-select --install）。")


# --------------------------------------------------------------------------- 设备

def list_devices() -> list:
    require_xcrun()
    r = run(["xcrun", "simctl", "list", "devices", "available", "-j"])
    if r.returncode != 0:
        raise ToolError("读不到模拟器列表（CoreSimulatorService 不可用？先打开一次 Simulator.app）")
    try:
        data = json.loads(r.stdout)["devices"]
    except Exception as exc:
        raise ToolError(f"模拟器列表解析失败：{exc}") from exc
    rows = []
    for runtime, devices in data.items():
        for d in devices:
            if d.get("isAvailable") is False:
                continue
            rows.append(Device(
                name=d.get("name", ""),
                udid=d["udid"],
                state=d.get("state", "Shutdown"),
                runtime=runtime.split(".")[-1] if runtime else "",
            ))
    return rows


def resolve_device(target: str) -> str:
    """Device name or UDID → UDID. Duplicate names prefer the booted one."""
    if not target:
        raise ToolError("没有指定设备，也没在 ujshot.toml 里配置 device.name")
    if re.fullmatch(r"[0-9A-Fa-f][0-9A-Fa-f-]{30,}", target):
        return target
    rows = list_devices()
    matches = [d for d in rows if d.name == target]
    if not matches:
        names = sorted({d.name for d in rows})
        raise ToolError(f"找不到模拟器「{target}」。可用：{', '.join(names) or '（无）'}")
    booted = [d for d in matches if d.state == "Booted"]
    return (booted or matches)[0].udid


def ensure_booted(udid: str) -> bool:
    """Boot if needed. Returns True when it had to boot."""
    r = run(["xcrun", "simctl", "list", "devices", "-j"])
    try:
        for devices in json.loads(r.stdout)["devices"].values():
            for d in devices:
                if d["udid"] == udid and d.get("state") == "Booted":
                    return False
    except Exception:
        pass
    run(["xcrun", "simctl", "boot", udid], timeout=180)
    run(["xcrun", "simctl", "bootstatus", udid, "-b"], timeout=300)
    return True


def display_size(axe: str, udid: str, default=(440, 956)):
    r = run([axe, "displays", "--udid", udid], timeout=120)
    m = re.search(r"(\d+)×(\d+) pt", r.stdout or "")
    return (int(m.group(1)), int(m.group(2))) if m else default


def screenshot(udid: str, path: str) -> bool:
    """simctl, not `axe screenshot`: axe occasionally returns a stale frame."""
    if os.path.exists(path):
        os.remove(path)
    r = run(["xcrun", "simctl", "io", udid, "screenshot", path], timeout=180)
    return r.returncode == 0 and os.path.exists(path)


def prepare(udid: str, appearance: str = "dark", freeze_status_bar: bool = True) -> None:
    if appearance in ("dark", "light"):
        run(["xcrun", "simctl", "ui", udid, "appearance", appearance], timeout=60)
    if freeze_status_bar:
        run(["xcrun", "simctl", "status_bar", udid, "override",
             "--time", "9:41", "--batteryState", "charged", "--batteryLevel", "100",
             "--cellularMode", "active", "--cellularBars", "4", "--wifiBars", "3"], timeout=60)


def install(udid: str, app_path: str) -> None:
    if not os.path.isdir(app_path):
        raise ToolError(f"App 路径不存在：{app_path}")
    r = run(["xcrun", "simctl", "install", udid, app_path], timeout=300)
    if r.returncode != 0:
        raise ToolError(f"安装失败：{r.stderr[-500:]}")


def terminate(udid: str, bundle: str) -> None:
    if bundle:
        run(["xcrun", "simctl", "terminate", udid, bundle], timeout=60)


def launch(udid: str, bundle: str, args=None) -> bool:
    if not bundle:
        raise ToolError("没有 bundle id：请在 ujshot.toml 的 [app] bundle_id 或 --bundle 指定")
    r = run(["xcrun", "simctl", "launch", udid, bundle] + list(args or []), timeout=120)
    return r.returncode == 0


def app_container(udid: str, bundle: str) -> str:
    r = run(["xcrun", "simctl", "get_app_container", udid, bundle, "data"], timeout=60)
    if r.returncode != 0:
        raise ToolError(f"取不到 App 数据容器（App 装了吗？）：{r.stderr[-300:]}")
    return r.stdout.strip()


# --------------------------------------------------------------------------- 交互

def tap_point(axe: str, udid: str, x: float, y: float, post_delay: float = 0.5) -> bool:
    r = run([axe, "tap", "-x", str(int(x)), "-y", str(int(y)),
             "--udid", udid, "--post-delay", str(post_delay)], timeout=120)
    return r.returncode == 0


def tap_id(axe: str, udid: str, ident: str, post_delay: float = 0.6) -> bool:
    r = run([axe, "tap", "--id", ident, "--udid", udid,
             "--post-delay", str(post_delay)], timeout=120)
    return r.returncode == 0


def swipe(axe: str, udid: str, x1, y1, x2, y2) -> bool:
    r = run([axe, "swipe", "--start-x", str(int(x1)), "--start-y", str(int(y1)),
             "--end-x", str(int(x2)), "--end-y", str(int(y2)), "--udid", udid], timeout=120)
    return r.returncode == 0


def type_text(axe: str, udid: str, text: str) -> bool:
    """US keyboard only. Non-ASCII needs the clipboard route (see README)."""
    r = run([axe, "type", text, "--udid", udid], timeout=120)
    return r.returncode == 0


def copy_clipboard(axe: str, udid: str, text: str) -> bool:
    try:
        r = subprocess.run([axe, "clipboard", "copy", "--stdin", "--udid", udid],
                           input=text, capture_output=True, text=True, timeout=60)
        return r.returncode == 0
    except Exception:
        return False


def key_combo(axe: str, udid: str, modifiers, key: int) -> bool:
    r = run([axe, "key-combo", "--modifiers", ",".join(str(m) for m in modifiers),
             "--key", str(key), "--udid", udid], timeout=60)
    return r.returncode == 0


# --------------------------------------------------------------------------- AX 树

def flatten(node, acc=None) -> list:
    acc = acc if acc is not None else []
    if isinstance(node, list):
        for x in node:
            flatten(x, acc)
        return acc
    if not isinstance(node, dict):
        return acc
    acc.append(node)
    for child in node.get("children") or []:
        flatten(child, acc)
    return acc


def ax_tree(axe: str, udid: str, retries: int = 3):
    """Return (raw, nodes). axe sometimes returns only the app root — retry.

    Callers must treat an empty/short tree as "unknown", not "nothing on screen":
    that distinction is why every flow keeps an OCR fallback.
    """
    best = []
    best_raw = None
    for _ in range(max(1, retries)):
        r = run([axe, "describe-ui", "--udid", udid], timeout=240)
        if r.returncode != 0 or not r.stdout.strip():
            time.sleep(0.6)
            continue
        try:
            data = json.loads(r.stdout)
        except Exception:
            time.sleep(0.6)
            continue
        nodes = flatten(data)
        if len(nodes) > len(best):
            best, best_raw = nodes, data
        if len(nodes) > 3:
            return data, nodes
        time.sleep(0.6)
    return best_raw, best


def frame_of(node):
    raw = (node or {}).get("AXFrame") or ""
    nums = re.findall(r"-?\d+\.?\d*", raw)
    if len(nums) != 4:
        return None
    return tuple(float(v) for v in nums)


def find(nodes, ident=None, label=None, contains=None, prefix=None):
    for n in nodes:
        i = n.get("AXUniqueId") or ""
        l = n.get("AXLabel") or ""
        if ident and i == ident:
            return n
        if prefix and i.startswith(prefix):
            return n
        if label and l == label:
            return n
        if contains and contains in l:
            return n
    return None


def warn(msg: str) -> None:
    sys.stderr.write(f"  ! {msg}\n")
