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
