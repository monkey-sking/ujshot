"""The executor: walks a flow, drives the device, records everything.

Two rules that come from real failures on this project (2026-11-20 walkthrough)
and are worth keeping in any port:

1. **Never trust the AX tree alone.** ``axe describe-ui`` intermittently returns
   only the app root node. Every lookup therefore falls back to OCR coordinates.
2. **A shot is a claim, not a screenshot.** ``expect`` / ``expectAbsent`` and a
   pixel diff decide ``verdict``; a file that exists but shows the previous
   screen must not count as evidence.
"""

from __future__ import annotations

import json
import os
import sys
import time

from . import device as dev
from . import ocr
from .flow import Flow
from .types import Device, RunResult, Step

DEFAULT_DANGER = (
    # 通用「会改数据」的按钮文案。命中即跳过，除非步骤显式写 dangerous: false。
    # 换语言/换项目可用 ujshot.toml 的 [safety] danger_words 覆盖。
    "删除", "移除", "清空", "清除", "重置", "恢复默认",
    "退出登录", "注销", "解绑", "取消订阅",
    "确认支付", "立即购买", "提交订单",
)


def log(msg: str) -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    sys.stderr.write(f"  ! {msg}\n")


def pixel_diff(a: str, b: str):
    """Percent of differing pixels (>8 grey levels). None when Pillow is absent."""
    try:
        from PIL import Image, ImageChops
    except ImportError:
        return None
    try:
        ia, ib = Image.open(a).convert("RGB"), Image.open(b).convert("RGB")
    except Exception:
        return None
    if ia.size != ib.size:
        return 100.0
    hist = ImageChops.difference(ia, ib).convert("L").histogram()
    return round(sum(hist[8:]) / (ia.size[0] * ia.size[1]) * 100, 2)


def is_blank(png: str, max_colors: int = 40) -> bool:
    """Near-solid image = desktop / black / white splash, not a real screen.

    Home screen and a normal app screen both have hundreds of colours; a blank
    or single-colour frame has a handful. Used only when OCR found no text at
    all, so the cost of a wrong guess is low.
    """
    try:
        from PIL import Image
    except ImportError:
        return False
    try:
        im = Image.open(png).convert("RGB")
    except Exception:
        return False
    colors = im.getcolors(maxcolors=100000)
    if colors is None:          # more colours than the cap → definitely not blank
        return False
    return len(colors) < max_colors


class Runner:
    def __init__(self, axe: str, udid: str, flow: Flow, outdir: str,
                 bundle: str = "", wait_scale: float = 1.0,
                 danger_words=None):
        self.axe = axe
        self.udid = udid
        self.flow = flow
        self.out = outdir
        self.bundle = bundle
        self.scale = max(0.2, float(wait_scale))
        self.danger = tuple(danger_words or DEFAULT_DANGER)

        self.screens = os.path.join(outdir, "screens")
        self.trees = os.path.join(outdir, "trees")
        self.ocrs = os.path.join(outdir, "ocr")
        for d in (self.screens, self.trees, self.ocrs):
            os.makedirs(d, exist_ok=True)

        self.result = RunResult(flow=flow.name, outdir=outdir,
                                device=Device(name="", udid=udid))
        self.prev_png = None
        self.size = dev.display_size(axe, udid)

    # ------------------------------------------------------------------ 动作
    def wait(self, seconds: float) -> None:
        time.sleep(seconds * self.scale)

    def probe_png(self) -> str:
        """Probe shots go to the temp dir so they never pollute the evidence."""
        return os.path.join(os.environ.get("TMPDIR", "/tmp"), f"ujshot-probe-{self.udid[:8]}.png")

    def grab(self, path: str) -> bool:
        return dev.screenshot(self.udid, path)

    def screen_info(self):
        p = self.probe_png()
        return ocr.recognize(p) if self.grab(p) else None

    def screen_text(self) -> str:
        return ocr.text_of(self.screen_info())

    def tap_point(self, x, y, wait=1.4) -> None:
        dev.tap_point(self.axe, self.udid, x, y)
        self.wait(wait)

    def tap_ocr(self, needle: str, wait=1.6, ymin=0.0, ymax=1.0) -> bool:
        w, h = self.size
        for text, cx, cy in ocr.lines_of(self.screen_info()):
            if not (ymin <= cy <= ymax):
                continue
            if needle == text.strip() or needle in text:
                self.tap_point(cx * w, cy * h, wait)
                return True
        return False

    def nodes(self):
        _, nodes = dev.ax_tree(self.axe, self.udid)
        return nodes

    def tap(self, spec: dict, wait=2.0):
        """id → id prefix → label → label contains → OCR → (x,y). Returns (ok, how)."""
        if "x" in spec and "y" in spec:
            self.tap_point(spec["x"], spec["y"], wait)
            return True, "xy"

        ident, label = spec.get("id"), spec.get("label")
        if ident:
            nodes = self.nodes()
            if dev.find(nodes, ident=ident):
                return dev.tap_id(self.axe, self.udid, ident), f"id:{ident}"
            if dev.find(nodes, prefix=ident):
                hit = dev.find(nodes, prefix=ident)
                key = hit.get("AXUniqueId")
                dev.tap_id(self.axe, self.udid, key)
                self.wait(wait)
                return True, f"id~{ident}"
        if label:
            node = dev.find(self.nodes(), label=label) or dev.find(self.nodes(), contains=label)
            if node:
                fr = dev.frame_of(node)
                if fr:
                    self.tap_point(fr[0] + fr[2] / 2, fr[1] + fr[3] / 2, wait)
                    return True, f"label:{label}"
            if self.tap_ocr(label, wait):
                return True, f"ocr:{label}"
        return False, "not-found"

    def swipe(self, y1: float, y2: float, wait=1.4) -> None:
        w, h = self.size
        dev.swipe(self.axe, self.udid, w // 2, y1 * h, w // 2, y2 * h)
        self.wait(wait)

    def back(self) -> bool:
        if dev.find(self.nodes(), ident="BackButton"):
            dev.tap_id(self.axe, self.udid, "BackButton")
            self.wait(1.6)
            return True
        w, h = self.size
        dev.swipe(self.axe, self.udid, 6, h // 2, w * 0.75, h // 2)
        self.wait(1.4)
        return True

    def restart(self) -> bool:
        if not self.bundle:
            warn("没有 bundle id，跳过重启（流程从当前界面继续）")
            return False
        dev.terminate(self.udid, self.bundle)
        self.wait(0.8)
        if not dev.launch(self.udid, self.bundle, self.flow.launch_args):
            # 起不来就停：继续截图只会拍到桌面，那种"证据"比没有更糟
            raise dev.ToolError(
                f"App 起不来：{self.bundle}。先确认已安装"
                f"（xcrun simctl install <UDID> <X.app>），或用 --app 指定构建产物。"
            )
        for _ in range(20):
            self.wait(1.2)
            text = self.screen_text()
            if text:
                return True
            # 首次启动可能弹系统授权，点掉
            self.tap_ocr("使用App时允许", 1.2) or self.tap_ocr("允许", 1.2)
        return False

    # ------------------------------------------------------------------ 截图
    def shot(self, spec: dict) -> Step:
        sid = spec["id"]
        title = spec.get("title") or sid
        png = os.path.join(self.screens, f"{sid}.png")
        step = Step(id=sid, title=title, note=spec.get("note", ""),
                    parent=spec.get("parent"),
                    expect=list(spec.get("expect") or []),
                    expect_absent=list(spec.get("expectAbsent") or []))

        if not self.grab(png):
            step.ok, step.error = False, "screenshot failed"
            self.result.failures += 1
            self.result.steps.append(step)
            log(f"  ✗ 截图失败 {sid}")
            return step

        if self.prev_png and os.path.exists(self.prev_png):
            step.diff_pct = pixel_diff(self.prev_png, png)
        self.prev_png = png

        raw, nodes = dev.ax_tree(self.axe, self.udid)
        step.nodes = len(nodes)
        if raw is not None:
            with open(os.path.join(self.trees, f"{sid}.json"), "w", encoding="utf-8") as f:
                json.dump(raw, f, ensure_ascii=False)

        info = ocr.recognize(png)
        if info:
            with open(os.path.join(self.ocrs, f"{sid}.json"), "w", encoding="utf-8") as f:
                json.dump(info, f, ensure_ascii=False)
        text = ocr.text_of(info)
        step.text = text[:400]

        verdict = "ok"
        if not text.strip() and is_blank(png):
            # 认不出字 + 近纯色：多半不是 App 界面（桌面/黑屏/白屏/启动闪屏）。
            # 这种"证据"比没有更糟，必须判失败而不是 ok。
            verdict = "blank-or-not-app"
        elif step.expect or step.expect_absent:
            if not info:
                # 不能把「OCR 没跑起来」当成通过，也不能当成缺文案
                verdict = "ocr-unavailable"
            else:
                missing = [e for e in step.expect if e not in text]
                leaked = [e for e in step.expect_absent if e in text]
                if missing:
                    verdict = "expect-missing"
                elif leaked:
                    verdict = "unexpected-text"

        if verdict == "ok" and step.diff_pct is not None and step.diff_pct < 0.5:
            verdict = "no-visible-change"

        step.verdict = verdict
        step.ok = verdict == "ok"
        if not step.ok:
            self.result.failures += 1

        self.result.steps.append(step)
        flag = "" if step.ok else f"  ⚠ {verdict}"
        log(f"  ✔ {sid}  nodes={step.nodes} diff={step.diff_pct}%{flag}")
        return step

    # ------------------------------------------------------------------ 流程
    def run(self) -> RunResult:
        self.restart()
        if self.flow.start_tab:
            self.tap({"id": self.flow.start_tab}, 2.2)

        for index, spec in enumerate(self.flow.steps, 1):
            op = spec["op"]
            label = spec.get("id") or spec.get("title") or f"{op}#{index}"
            getattr(self, f"_op_{op}")(spec, label)
        return self.result

    # 每个 op 一个方法，加新 op 只改 here + flow.OPS
    def _op_shot(self, spec, label):
        self.shot(spec)

    def _op_tap(self, spec, label):
        if spec.get("dangerous") or any(w in str(spec.get("label", "")) for w in self.danger):
            self.result.steps.append(Step(id=label, title="跳过危险点击", verdict="skipped"))
            log(f"  · 跳过危险点击 {label}")
            return
        target = {k: spec[k] for k in ("id", "label", "x", "y") if k in spec}
        ok, how = self.tap(target, spec.get("wait", 2.0))
        optional = bool(spec.get("optional"))
        step = Step(id=label, title=f"点击 {label}", ok=ok or optional, how=how,
                    verdict=None if ok else ("optional-missing" if optional else "tap-failed"))
        self.result.steps.append(step)
        log(f"  · 点击 {label} → {'ok' if ok else ('不存在（可选）' if optional else 'FAIL')} ({how})")
        if not ok and not optional:
            self.result.failures += 1

    def _op_swipe(self, spec, label):
        self.swipe(spec.get("from", 0.75), spec.get("to", 0.32), spec.get("wait", 1.4))
        self.result.steps.append(Step(id=label, title=f"滑动 {label}"))

    def _op_type(self, spec, label):
        ok = dev.type_text(self.axe, self.udid, spec["text"])
        self.wait(0.8)
        self.result.steps.append(Step(id=label, title=f"输入 {spec['text'][:20]}", ok=ok))
        if not ok:
            self.result.failures += 1

    def _op_clipboard(self, spec, label):
        """非 ASCII 文本走剪贴板（模拟器没有中文键盘）。"""
        ok = dev.copy_clipboard(self.axe, self.udid, spec["text"])
        if ok and spec.get("paste", True):
            dev.key_combo(self.axe, self.udid, [227], 9)  # Cmd+V
            self.wait(0.8)
        self.result.steps.append(Step(id=label, title="粘贴文本", ok=ok))
        if not ok:
            self.result.failures += 1

    def _op_key(self, spec, label):
        if spec.get("modifiers"):
            ok = dev.key_combo(self.axe, self.udid, spec["modifiers"], int(spec["code"]))
        else:
            r = dev.run([self.axe, "key", str(int(spec["code"])), "--udid", self.udid], timeout=60)
            ok = r.returncode == 0
        self.wait(0.4)
        self.result.steps.append(Step(id=label, title=f"按键 {spec['code']}", ok=ok))
        if not ok:
            self.result.failures += 1

    def _op_wait(self, spec, label):
        self.wait(float(spec.get("seconds", 1.5)))

    def _op_expect(self, spec, label):
        text = self.screen_text()
        hit = all(k in text for k in spec["text"])
        self.result.steps.append(Step(id=label, title=f"期望 {spec['text']}", ok=hit))
        log(f"  ? 期望 {spec['text']} → {'在' if hit else '不在'}")
        if not hit:
            self.result.failures += 1

    def _op_launch(self, spec, label):
        if spec.get("args") is not None:
            self.flow.launch_args = list(spec["args"])
        self.restart()
        self.result.steps.append(Step(id=label, title="重启 App"))

    def _op_back(self, spec, label):
        self.back()
        self.result.steps.append(Step(id=label, title="返回"))

    def _op_ensure(self, spec, label):
        """确保某段文案在屏幕上；不在就依次点 then 里的目标再试。

        用于穿过**已知缺陷**（比如点 chip 后 TabView 跳回首页），让流程能继
        续走，而不是把缺陷写进流程或加一堆 sleep。
        """
        want = list(spec["text"])
        recovery = spec.get("then") or []
        if isinstance(recovery, dict):
            recovery = [recovery]
        ok = False
        rounds = int(spec.get("retries", 3))
        for _ in range(rounds):
            if all(k in self.screen_text() for k in want):
                ok = True
                break
            if not recovery:
                self.wait(1.0)
                continue
            for target in recovery:
                self.tap({k: target[k] for k in ("id", "label", "x", "y") if k in target},
                         spec.get("wait", 2.2))
        self.result.steps.append(Step(id=label, title=f"确保出现 {want}", ok=ok))
        log(f"  ? 确保 {'/'.join(want)} → {'在' if ok else '不在'}")
        if not ok:
            self.result.failures += 1
