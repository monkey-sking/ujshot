"""A board with rounds must survive --import: both attempts, in their own dirs."""

import json
import os

from ujshot import board

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _write(path, payload=PNG):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(payload)


def _make_src(root):
    _write(os.path.join(root, "01-fab.png"))
    _write(os.path.join(root, "after", "234", "01-fab.png"), PNG + b"234")
    _write(os.path.join(root, "after", "235", "01-fab.png"), PNG + b"235")
    _write(os.path.join(root, "after", "235", "02-settings.png"))
    json.dump({"version": 1, "directory": "src", "annotations": [
        {"id": "U1", "image": "01-fab.png", "text": "按钮不美观", "status": "fixed",
         "x": 0.5, "y": 0.2, "rounds": [
             {"build": "234", "status": "rejected", "rejectReason": "还是偏左"},
             {"build": "235", "status": "fixed"},
         ]},
    ]}, open(os.path.join(root, "annotations.json"), "w"), ensure_ascii=False)


def test_snapshot_reports_rounds(tmp_path):
    src = str(tmp_path / "src")
    _make_src(src)
    snap = board.snapshot(src_dir=src)
    builds = [r["build"] for r in snap["rounds"]]
    assert builds == ["234", "235"]
    assert sorted(snap["after"]) == ["01-fab.png", "02-settings.png"]


def test_merge_keeps_each_round_directory(tmp_path):
    src = str(tmp_path / "src")
    dst = str(tmp_path / "dst")
    _make_src(src)

    stat = board.merge(src, dst)

    assert os.path.exists(os.path.join(dst, "after", "234", "01-fab.png"))
    assert os.path.exists(os.path.join(dst, "after", "235", "01-fab.png"))
    assert os.path.exists(os.path.join(dst, "after", "235", "02-settings.png"))
    # 两轮的图内容不同，不能被合并成一张
    assert open(os.path.join(dst, "after", "234", "01-fab.png"), "rb").read().endswith(b"234")
    assert open(os.path.join(dst, "after", "235", "01-fab.png"), "rb").read().endswith(b"235")
    assert stat["after"] == 3

    anns = json.load(open(os.path.join(dst, "annotations.json"), encoding="utf-8"))["annotations"]
    assert anns[0]["rounds"][0]["rejectReason"] == "还是偏左"


def test_merge_flat_layout_still_works(tmp_path):
    src = str(tmp_path / "src")
    dst = str(tmp_path / "dst")
    _write(os.path.join(src, "01-fab.png"))
    _write(os.path.join(src, "after", "01-fab.png"))
    json.dump({"version": 1, "annotations": []},
              open(os.path.join(src, "annotations.json"), "w"))

    stat = board.merge(src, dst)

    assert os.path.exists(os.path.join(dst, "after", "01-fab.png"))
    assert stat["after"] == 1


def test_merge_preserves_light_slots_and_separates_appearance_dedupe(tmp_path):
    src = str(tmp_path / "src")
    dst = str(tmp_path / "dst")
    _write(os.path.join(src, "01-fab.png"), PNG + b"dark-before")
    _write(os.path.join(src, "light", "01-fab.png"), PNG + b"light-before")
    _write(os.path.join(src, "after", "234", "01-fab.png"), PNG + b"dark-234")
    _write(os.path.join(src, "after", "light", "234", "01-fab.png"), PNG + b"light-234")
    with open(os.path.join(src, "annotations.json"), "w", encoding="utf-8") as f:
        json.dump({"version": 1, "annotations": [
            {"id": "U1", "image": "01-fab.png", "appearance": "dark", "text": "对比度低", "x": 0.5, "y": 0.5, "status": "open"},
            {"id": "U2", "image": "01-fab.png", "appearance": "light", "text": "对比度低", "x": 0.5, "y": 0.5, "status": "open"},
        ]}, f, ensure_ascii=False)

    stat = board.merge(src, dst)

    assert os.path.exists(os.path.join(dst, "light", "01-fab.png"))
    assert os.path.exists(os.path.join(dst, "after", "light", "234", "01-fab.png"))
    assert stat["lightImages"] == 1
    assert stat["lightAfter"] == 1
    assert stat["added"] == 2 and stat["duplicate"] == 0


def test_merge_from_url_preserves_dark_and_light_rounds(tmp_path, monkeypatch):
    dst = str(tmp_path / "dst")
    routes = {
        "http://fake:8766/api/state": json.dumps({
            "dir": "/remote/board",
            "images": ["01-home.png"],
            "lightImages": ["01-home.png"],
            "afterImages": ["01-home.png"],
            "lightAfterImages": ["01-home.png"],
            "afterRounds": [{"build": "234", "images": ["01-home.png"]}],
            "lightAfterRounds": [{"build": "235", "images": ["01-home.png"]}],
            "annotations": [{"id": "U1", "image": "01-home.png", "appearance": "light", "text": "亮色边框淡", "x": 0.2, "y": 0.3}],
        }).encode("utf-8"),
        "http://fake:8766/images/01-home.png": PNG + b"url-dark",
        "http://fake:8766/images/light/01-home.png": PNG + b"url-light",
        "http://fake:8766/after/234/01-home.png": PNG + b"url-after-234",
        "http://fake:8766/after/light/235/01-home.png": PNG + b"url-light-235",
    }
    monkeypatch.setattr(board, "_http_get", lambda url, timeout=5: routes[url])

    stat = board.merge(src_dir="", dst_dir=dst, src_url="http://fake:8766")

    assert stat["images"] == 2 and stat["after"] == 2
    assert open(os.path.join(dst, "after", "234", "01-home.png"), "rb").read().endswith(b"url-after-234")
    assert open(os.path.join(dst, "after", "light", "235", "01-home.png"), "rb").read().endswith(b"url-light-235")
