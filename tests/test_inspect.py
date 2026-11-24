"""Tests for AX/OCR element inspection, marker rendering, regex verdicts, and live board APIs."""

from __future__ import annotations

import json
import os
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from ujshot import board, cli, device, dispatcher, inspect as ujinspect, ocr, reviewboard, runner


def _sample_ax_tree():
    return [
        {
            "type": "Application",
            "AXLabel": "SampleApp",
            "frame": {"x": 0, "y": 0, "width": 400, "height": 800},
            "children": [
                {
                    "type": "Group",
                    "frame": {"x": 0, "y": 100, "width": 400, "height": 120},
                    "children": [
                        {
                            "type": "StaticText",
                            "AXLabel": "Five Elephant",
                            "frame": {"x": 20, "y": 120, "width": 120, "height": 24},
                        },
                        {
                            "type": "StaticText",
                            "AXLabel": "−€7.40",
                            "AXUniqueId": "tx.amount",
                            "frame": {"x": 300, "y": 120, "width": 80, "height": 24},
                        },
                        {
                            "type": "StaticText",
                            "AXLabel": "09:12",
                            "frame": {"x": 320, "y": 150, "width": 60, "height": 18},
                        },
                    ],
                },
                {
                    "type": "Group",
                    "AXLabel": "Tab Bar",
                    "frame": {"x": 0, "y": 720, "width": 400, "height": 80},
                    "children": [
                        {
                            "type": "RadioButton",
                            "AXLabel": "Activity",
                            "AXValue": 1,
                            "AXUniqueId": "tab.activity",
                            "frame": {"x": 150, "y": 730, "width": 100, "height": 54},
                            "children": [
                                {
                                    "type": "Image",
                                    "AXLabel": "chart column",
                                    "AXUniqueId": "chart.bar.fill",
                                    "frame": {"x": 184, "y": 740, "width": 32, "height": 24},
                                }
                            ],
                        },
                        {
                            "type": "RadioButton",
                            "AXLabel": "Home",
                            "AXValue": 0,
                            "AXUniqueId": "tab.home",
                            "frame": {"x": 40, "y": 730, "width": 100, "height": 54},
                        },
                    ],
                },
            ],
        }
    ]


# --------------------------------------------------------------------------- device


def test_frame_of_supports_v2_dict_and_legacy_axframe():
    assert device.frame_of({"frame": {"x": 12, "y": 34.5, "width": 100, "height": 22}}) == (12.0, 34.5, 100.0, 22.0)
    assert device.frame_of({"AXFrame": "{{10, 20}, {300, 44.5}}"}) == (10.0, 20.0, 300.0, 44.5)
    assert device.frame_of({}) is None


def test_find_axe_prefers_env_and_discovers_npx_cache(tmp_path, monkeypatch):
    fake_env = tmp_path / "custom-axe"
    fake_env.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    fake_env.chmod(0o755)

    monkeypatch.setenv("UJSHOT_AXE", str(fake_env))
    assert device.find_axe() == str(fake_env)

    monkeypatch.delenv("UJSHOT_AXE", raising=False)
    monkeypatch.delenv("FIXKIT_AXE", raising=False)
    monkeypatch.setattr(device.shutil, "which", lambda _name: None)
    monkeypatch.setattr(device.os.path, "isfile", lambda p: False if str(p).startswith("/opt/") or str(p).startswith("/usr/") else os.path.exists(p) and not os.path.isdir(p))
    monkeypatch.setattr(device.Path, "home", classmethod(lambda cls: tmp_path))

    older = tmp_path / ".npm" / "_npx" / "aaa" / "node_modules" / "xcodebuildmcp" / "bundled" / "axe"
    newer = tmp_path / ".npm" / "_npx" / "bbb" / "node_modules" / "xcodebuildmcp" / "bundled" / "axe"
    for p in (older, newer):
        p.parent.mkdir(parents=True)
        p.write_text("#!/bin/sh\n", encoding="utf-8")
        p.chmod(0o755)
    os.utime(older, (1000, 1000))
    os.utime(newer, (2000, 2000))

    assert device.find_axe() == str(newer)


# --------------------------------------------------------------------------- inspect AX & OCR


def test_element_at_ax_pin_and_box_modes():
    tree = _sample_ax_tree()
    # Pin inside tx.amount (x=340/400=0.85, y=132/800=0.165)
    pin_res = ujinspect.element_at_ax(tree, 0.85, 0.165)
    assert pin_res is not None
    assert pin_res["element"]["identifier"] == "tx.amount"
    assert pin_res["element"]["label"] == "−€7.40"
    assert [n["label"] for n in pin_res["nearby"]] == ["Five Elephant"]
    assert pin_res["element"]["normFrame"] == {"x": 0.75, "y": 0.15, "w": 0.2, "h": 0.03}

    # Pin inside nested Image inside RadioButton ("within")
    tab_res = ujinspect.element_at_ax(tree, 200 / 400, 750 / 800)
    assert tab_res is not None
    assert tab_res["element"]["type"] == "Image"
    assert tab_res["element"]["identifier"] == "chart.bar.fill"
    assert tab_res["within"] is not None
    assert tab_res["within"]["type"] == "RadioButton"
    assert tab_res["within"]["label"] == "Activity"

    # Box covering tx.amount
    box_res = ujinspect.element_at_ax(tree, 0.70, 0.14, 0.28, 0.05)
    assert box_res is not None
    assert box_res["element"]["identifier"] == "tx.amount"
    assert len(box_res["matches"]) >= 1


def test_element_at_ocr_supports_legacy_and_wh_formats():
    legacy_ocr = {
        "lines": [
            {"t": "Coffee", "x": 0.05, "y": 0.15, "cx": 0.15, "cy": 0.165},
            {"t": "−€7.40", "x": 0.75, "y": 0.15, "cx": 0.85, "cy": 0.165},
        ]
    }
    res_legacy = ujinspect.element_at_ocr(legacy_ocr, 0.85, 0.165)
    assert res_legacy is not None
    assert [h["text"] for h in res_legacy["hits"]] == ["−€7.40"]
    assert [n["text"] for n in res_legacy["nearby"]] == ["Coffee"]

    wh_ocr = {
        "lines": [
            {"t": "Coffee", "x": 0.05, "y": 0.15, "w": 0.20, "h": 0.03, "cx": 0.15, "cy": 0.165},
            {"t": "−€7.40", "x": 0.75, "y": 0.15, "w": 0.20, "h": 0.03, "cx": 0.85, "cy": 0.165},
        ]
    }
    res_box = ujinspect.element_at_ocr(wh_ocr, 0.04, 0.14, 0.92, 0.05)
    assert res_box is not None
    assert [h["text"] for h in res_box["hits"]] == ["Coffee", "−€7.40"]

    line = ujinspect.format_inspection_line(None, res_legacy)
    assert 'OCR "−€7.40"' in line
    assert 'near "Coffee"' in line


def test_render_annotated_image_creates_copy_without_mutating_source(tmp_path):
    Image = pytest.importorskip("PIL.Image")

    src = tmp_path / "src.png"
    dst = tmp_path / "refs" / "marked.png"
    Image.new("RGB", (400, 800), (20, 24, 30)).save(src)
    orig_bytes = src.read_bytes()

    out = ujinspect.render_annotated_image(
        src,
        dst,
        {"kind": "pin", "x": 0.85, "y": 0.165},
        element_norm_frame={"x": 0.75, "y": 0.15, "w": 0.2, "h": 0.03},
    )
    assert out == dst and dst.is_file()
    assert src.read_bytes() == orig_bytes
    assert dst.read_bytes() != orig_bytes

    dst_box = tmp_path / "refs" / "box.png"
    out_box = ujinspect.render_annotated_image(
        src,
        dst_box,
        {"kind": "box", "x": 0.1, "y": 0.2, "w": 0.4, "h": 0.1},
    )
    assert out_box == dst_box and dst_box.is_file()


# --------------------------------------------------------------------------- board + dispatcher


def _minimal_png_bytes() -> bytes:
    """Create a valid 1x1 PNG using stdlib only so tests don't require Pillow."""
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00\x12\x12\x16")
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def test_board_persists_meta_sidecars_merges_and_inspects(tmp_path):
    try:
        import PIL  # noqa: F401
        has_pil = True
    except ImportError:
        has_pil = False

    run_out = tmp_path / "run_out"
    (run_out / "screens").mkdir(parents=True)
    (run_out / "trees").mkdir(parents=True)
    (run_out / "ocr").mkdir(parents=True)
    (run_out / "screens" / "01-home.png").write_bytes(_minimal_png_bytes())
    (run_out / "trees" / "01-home.json").write_text(
        json.dumps(_sample_ax_tree(), ensure_ascii=False), encoding="utf-8"
    )
    (run_out / "ocr" / "01-home.json").write_text(
        json.dumps({"lines": [{"t": "Five Elephant", "x": 0.05, "y": 0.15, "w": 0.3, "h": 0.03, "cx": 0.2, "cy": 0.165}]}),
        encoding="utf-8",
    )

    bdir = tmp_path / "board"
    shots = [SimpleNamespace(id="01-home", title="首页", verdict="ok", expect=[], expect_absent=[], text="")]
    board.copy_shots(str(bdir), str(run_out), shots, as_after=True, round_build="250", appearance="dark")

    assert (bdir / ".meta" / "after" / "250" / "01-home.ax.json").is_file()
    assert (bdir / ".meta" / "after" / "250" / "01-home.ocr.json").is_file()

    (bdir / "annotations.json").write_text(
        json.dumps({
            "version": 1,
            "directory": "board",
            "annotations": [{
                "id": "U1",
                "image": "01-home.png",
                "appearance": "dark",
                "raisedIn": "250",
                "author": "user",
                "kind": "pin",
                "x": 0.85,
                "y": 0.165,
                "w": 0,
                "h": 0,
                "text": "金额颜色不对",
                "status": "open",
            }],
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    rows_plain = board.read(str(bdir))
    assert "inspection" not in rows_plain[0]

    rows_insp = board.read(str(bdir), with_inspect=True)
    assert "#tx.amount" in rows_insp[0]["elementHint"]
    assert rows_insp[0]["inspection"]["ax"]["element"]["identifier"] == "tx.amount"
    assert rows_insp[0]["screenshotPath"].endswith(os.path.join("after", "250", "01-home.png"))

    prompt = dispatcher.build_prompt(
        bdir,
        tmp_path,
        json.loads((bdir / "annotations.json").read_text(encoding="utf-8"))["annotations"],
        appearance="dark",
        image="01-home.png",
    )
    assert "定位线索（AX / OCR）：" in prompt
    assert "#tx.amount" in prompt
    if has_pil:
        assert "红框标注图：" in prompt
        assert (bdir / ".dispatch-logs" / "refs" / "U1-dark-250.png").is_file()
    else:
        assert "红框标注图：" not in prompt

    # Merge copies .meta sidecars too
    dst_board = tmp_path / "merged"
    board.merge(src_dir=str(bdir), dst_dir=str(dst_board))
    assert (dst_board / ".meta" / "after" / "250" / "01-home.ax.json").is_file()


def test_queue_to_codex_passes_safe_flag_syntax(monkeypatch):
    seen = {}

    def fake_run(cmd, capture_output, text, timeout):
        seen["cmd"] = list(cmd)
        return SimpleNamespace(returncode=0, stdout="queued", stderr="")

    monkeypatch.setattr(dispatcher.subprocess, "run", fake_run)
    ok, msg = dispatcher.queue_to_codex(
        "thread-123",
        "- leading dash prompt",
        resume=True,
        codex_bin="/usr/local/bin/codex",
    )
    assert ok is True
    assert msg == "queued"
    assert seen["cmd"] == [
        "/usr/local/bin/codex",
        "queue",
        "--thread=thread-123",
        "--message=- leading dash prompt",
        "--resume",
    ]


# --------------------------------------------------------------------------- reviewboard HTTP


def test_reviewboard_inspect_endpoint_and_element_hint_preservation(tmp_path):
    bdir = tmp_path / "board"
    meta_dir = ujinspect.meta_dir_for(bdir, appearance="dark", as_after=False)
    meta_dir.mkdir(parents=True)
    (meta_dir / "01-home.ax.json").write_text(json.dumps(_sample_ax_tree()), encoding="utf-8")
    (bdir / "01-home.png").write_bytes(b"png")
    (bdir / "annotations.json").write_text(
        json.dumps({
            "version": 1,
            "directory": "board",
            "annotations": [{
                "id": "U1",
                "image": "01-home.png",
                "appearance": "dark",
                "author": "user",
                "kind": "pin",
                "x": 0.85,
                "y": 0.165,
                "w": 0,
                "h": 0,
                "text": "check color",
                "elementHint": 'StaticText "−€7.40" #tx.amount',
                "status": "open",
            }],
        }),
        encoding="utf-8",
    )

    reg = reviewboard.BoardRegistry(tmp_path / "boards.json")
    reg.add(bdir, "demo")
    sim = SimpleNamespace(available=False, status=lambda: {"available": False})
    handler = reviewboard.make_handler(reg, sim)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/inspect",
            data=json.dumps({
                "image": "01-home.png",
                "appearance": "dark",
                "kind": "pin",
                "x": 0.85,
                "y": 0.165,
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            res = json.loads(resp.read().decode("utf-8"))
        assert res["ok"] is True
        assert "#tx.amount" in res["summary"]

        # POST /api/annotations preserves elementHint when client omits it
        save_req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/annotations",
            data=json.dumps({
                "annotations": [{
                    "id": "U1",
                    "image": "01-home.png",
                    "appearance": "dark",
                    "author": "user",
                    "kind": "pin",
                    "x": 0.85,
                    "y": 0.165,
                    "w": 0,
                    "h": 0,
                    "text": "check color",
                    "status": "fixed",
                }]
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(save_req, timeout=3) as resp:
            saved = json.loads(resp.read().decode("utf-8"))
        assert saved["annotations"][0]["elementHint"] == 'StaticText "−€7.40" #tx.amount'
    finally:
        srv.shutdown()


def test_reviewboard_codex_queue_endpoint(tmp_path, monkeypatch):
    bdir = tmp_path / "board"
    meta_dir = ujinspect.meta_dir_for(bdir, appearance="dark", as_after=False)
    meta_dir.mkdir(parents=True)
    (meta_dir / "01-home.ax.json").write_text(json.dumps(_sample_ax_tree()), encoding="utf-8")
    (bdir / "01-home.png").write_bytes(_minimal_png_bytes())
    (bdir / "annotations.json").write_text(
        json.dumps({
            "version": 1,
            "directory": "board",
            "annotations": [{
                "id": "U1",
                "image": "01-home.png",
                "appearance": "dark",
                "author": "user",
                "kind": "pin",
                "x": 0.85,
                "y": 0.165,
                "w": 0,
                "h": 0,
                "text": "check color",
                "status": "open",
            }],
        }),
        encoding="utf-8",
    )

    captured = {}

    def fake_queue(thread, prompt, resume=False, codex_bin=""):
        captured["thread"] = thread
        captured["prompt"] = prompt
        captured["resume"] = resume
        return True, "queued ok"

    monkeypatch.setattr(dispatcher, "queue_to_codex", fake_queue)

    reg = reviewboard.BoardRegistry(tmp_path / "boards.json")
    reg.add(bdir, "demo")
    sim = SimpleNamespace(available=False, status=lambda: {"available": False})
    handler = reviewboard.make_handler(reg, sim)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/dispatch/codex-queue",
            data=json.dumps({
                "thread": "my-codex-thread",
                "annIds": ["U1"],
                "image": "01-home.png",
                "appearance": "dark",
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            res = json.loads(resp.read().decode("utf-8"))
        assert res["ok"] is True
        assert res["thread"] == "my-codex-thread"
        assert res["queued"] == 1
        assert captured["thread"] == "my-codex-thread"
        assert "#tx.amount" in captured["prompt"]
    finally:
        srv.shutdown()


# --------------------------------------------------------------------------- runner regex + CLI


def test_evaluate_verdict_supports_regex_rules():
    assert runner.match_text_rule(r"re:[\u4e00-\u9fff]", "Total 支出") is True
    assert runner.match_text_rule(r"re:[\u4e00-\u9fff]", "Total Expense") is False

    v, missing, leaked = runner.evaluate_verdict(
        "Settings CNY 100",
        expect=[r"re:Settings\s+CNY"],
        expect_absent=[r"re:[\u4e00-\u9fff]"],
        diff_pct=12.0,
    )
    assert (v, missing, leaked) == ("ok", [], [])

    v2, _, leaked2 = runner.evaluate_verdict(
        "Settings 另含 1 条",
        expect=["Settings"],
        expect_absent=[r"re:[\u4e00-\u9fff]"],
        diff_pct=12.0,
    )
    assert v2 == "unexpected-text"
    assert leaked2 == [r"re:[\u4e00-\u9fff]"]


def test_cli_check_uses_regex_rules(tmp_path, monkeypatch, capsys):
    img = tmp_path / "shot.png"
    img.write_bytes(b"png")
    monkeypatch.setattr(ocr, "ensure", lambda verbose=False: "/tmp/fake-ocr")
    monkeypatch.setattr(ocr, "recognize", lambda _p: {"lines": [{"t": "Settings 支出"}]})

    rc = cli.main(["check", "--image", str(img), "--expect", "Settings", "--must-not", r"re:[\u4e00-\u9fff]", "--json"])
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["verdict"] == "unexpected-text"
    assert out["leaked"] == [r"re:[\u4e00-\u9fff]"]
