"""Board merge: id collisions, dedupe, offline vs live source."""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ujshot import board  # noqa: E402


def _png(path, payload=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(payload)


def _board(root, anns, images=(), after=()):
    os.makedirs(root, exist_ok=True)
    for n in images:
        _png(os.path.join(root, n))
    for n in after:
        _png(os.path.join(root, "after", n))
    with open(os.path.join(root, "annotations.json"), "w", encoding="utf-8") as f:
        json.dump({"version": 1, "directory": os.path.basename(root),
                   "annotations": anns}, f, ensure_ascii=False)
    return root


def _ann(id, image="a.png", text="问题", x=0.1, y=0.2, status="open"):
    return {"id": id, "image": image, "author": "user", "kind": "pin",
            "x": x, "y": y, "w": 0, "h": 0, "text": text, "status": status}


def test_snapshot_from_disk(tmp_path):
    root = _board(str(tmp_path / "b"), [_ann("U1")], images=["a.png"], after=["a.png"])
    snap = board.snapshot(src_dir=root)
    assert snap["images"] == ["a.png"] and snap["after"] == ["a.png"]
    assert len(snap["annotations"]) == 1


def test_merge_into_empty_target(tmp_path):
    src = _board(str(tmp_path / "src"), [_ann("U1")], images=["a.png"], after=["a.png"])
    dst = str(tmp_path / "dst")
    stat = board.merge(src_dir=src, dst_dir=dst)
    assert stat["added"] == 1 and stat["renamed"] == 0
    assert os.path.exists(os.path.join(dst, "a.png"))
    assert os.path.exists(os.path.join(dst, "after", "a.png"))
    data = json.load(open(os.path.join(dst, "annotations.json"), encoding="utf-8"))
    assert data["annotations"][0]["importedFrom"] == "src"


def test_id_collision_renames_instead_of_overwriting(tmp_path):
    src = _board(str(tmp_path / "src"), [_ann("U1", text="来源的问题")])
    dst = _board(str(tmp_path / "dst"), [_ann("U1", image="other.png", text="目标的问题")])
    stat = board.merge(src_dir=src, dst_dir=dst)
    assert stat["renamed"] == 1
    ids = {a["id"] for a in json.load(open(os.path.join(dst, "annotations.json"),
                                          encoding="utf-8"))["annotations"]}
    assert ids == {"U1", "U1@src"}


def test_identical_annotation_is_deduped(tmp_path):
    a = _ann("U1")
    src = _board(str(tmp_path / "src"), [a])
    dst = _board(str(tmp_path / "dst"), [dict(a)])
    stat = board.merge(src_dir=src, dst_dir=dst)
    assert stat["added"] == 0 and stat["duplicate"] == 1


def test_prefer_src_replaces_duplicate(tmp_path):
    src = _board(str(tmp_path / "src"), [_ann("U1", status="fixed")])
    dst = _board(str(tmp_path / "dst"), [_ann("U1", status="open")])
    board.merge(src_dir=src, dst_dir=dst, prefer="src")
    anns = json.load(open(os.path.join(dst, "annotations.json"), encoding="utf-8"))["annotations"]
    assert anns[0]["status"] == "fixed"


def test_merge_keeps_existing_images_untouched(tmp_path):
    # 目标板已经有一张同名图（可能人已经看过），不能被来源覆盖
    src = _board(str(tmp_path / "src"), [], images=["a.png"])
    dst = str(tmp_path / "dst")
    _png(os.path.join(dst, "a.png"), b"destination")
    board.merge(src_dir=src, dst_dir=dst)
    with open(os.path.join(dst, "a.png"), "rb") as f:
        assert f.read() == b"destination"


def test_merge_requires_a_source(tmp_path):
    with pytest.raises(ValueError):
        board.merge(src_dir="", dst_dir=str(tmp_path))


def test_copy_shots_skips_unchanged_and_marks_updated(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    from ujshot.types import Step

    bdir = str(tmp_path / "board")
    out245 = str(tmp_path / "out245")
    out246 = str(tmp_path / "out246")
    os.makedirs(os.path.join(out245, "screens"))
    os.makedirs(os.path.join(out246, "screens"))

    for sid, color in [("s1", (20, 20, 20)), ("s2", (30, 30, 30))]:
        Image.new("RGB", (200, 400), color).save(os.path.join(out245, "screens", f"{sid}.png"))

    shots = [Step(id="s1", title="s1", verdict="ok"), Step(id="s2", title="s2", verdict="ok")]
    placed245, _ = board.copy_shots(bdir, out245, shots, as_after=True, round_build="245")
    assert placed245 == ["s1.png", "s2.png"]

    ann_path = os.path.join(bdir, "annotations.json")
    with open(ann_path, "w", encoding="utf-8") as f:
        json.dump({
            "version": 1,
            "directory": "board",
            "annotations": [],
            "screenChecks": {
                "dark:s1.png": {"status": "ok", "build": "245"},
                "dark:s2.png": {"status": "ok", "build": "245"},
            },
        }, f)

    # s1 unchanged in main area (only status bar clock area changed)
    im1 = Image.new("RGB", (200, 400), (20, 20, 20))
    for x in range(40):
        for y in range(10):
            im1.putpixel((x, y), (255, 255, 255))
    im1.save(os.path.join(out246, "screens", "s1.png"))

    # s2 updated in main content area
    im2 = Image.new("RGB", (200, 400), (30, 30, 30))
    for x in range(100):
        for y in range(120, 220):
            im2.putpixel((x, y), (220, 120, 40))
    im2.save(os.path.join(out246, "screens", "s2.png"))

    placed246, _ = board.copy_shots(bdir, out246, shots, as_after=True, round_build="246")
    board.write_annotations(bdir, shots, as_after=True, round_build="246")

    assert placed246 == ["s2.png"]
    assert not os.path.exists(os.path.join(bdir, "after", "246", "s1.png"))
    assert os.path.exists(os.path.join(bdir, "after", "246", "s2.png"))

    after_data = json.load(open(ann_path, encoding="utf-8"))
    checks = after_data["screenChecks"]
    assert checks["dark:s1.png"]["status"] == "ok"
    assert checks["dark:s2.png"]["status"] == "updated"
    assert checks["dark:s2.png"]["prevBuild"] == "245"
    assert checks["dark:s2.png"]["build"] == "246"
