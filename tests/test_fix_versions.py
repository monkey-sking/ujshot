"""Per-issue fix versions: one screenshot, several issues, several builds."""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ujshot import board  # noqa: E402


def _board(tmp_path, anns):
    root = str(tmp_path / "b")
    os.makedirs(root)
    with open(os.path.join(root, "annotations.json"), "w", encoding="utf-8") as f:
        json.dump({"version": 1, "directory": "b", "annotations": anns}, f,
                  ensure_ascii=False)
    return root


def _ann(id, image="shot.png", status="fixed", aiReply="", text=""):
    return {"id": id, "image": image, "author": "user", "kind": "pin",
            "x": 0.1, "y": 0.2, "w": 0, "h": 0, "text": text,
            "status": status, "aiReply": aiReply}


def test_fixed_in_reads_build_from_ai_reply():
    assert board.fixed_in(_ann("U1", aiReply="Build 234：改好了")) == "234"


def test_fixed_in_falls_back_to_text():
    assert board.fixed_in(_ann("U1", text="已在 Build 235 修")) == "235"


def test_fixed_in_empty_when_nobody_recorded_it():
    # 空字符串是有意义的答案：说明没人记，而不是可以猜一个
    assert board.fixed_in(_ann("U1")) == ""


def test_fixed_in_prefers_explicit_field():
    a = _ann("U1", aiReply="Build 234：x")
    a["fixedIn"] = "241"
    assert board.fixed_in(a) == "241"


def test_one_image_several_builds(tmp_path):
    # 真实案例：同一张原始问题图上两个问题分别修在两个版本
    root = _board(tmp_path, [
        _ann("U2", image="fab.png", status="accepted", aiReply="Build 234：搜索按钮"),
        _ann("U36", image="fab.png", status="accepted", aiReply="Build 235：底部横排"),
    ])
    builds = board.fix_builds(root)
    assert builds["fab.png"] == ["234", "235"]


def test_fix_builds_skips_unrecorded(tmp_path):
    root = _board(tmp_path, [
        _ann("U7", image="a.png", aiReply="Build 234：x"),
        _ann("U11", image="a.png"),                      # 未记录
    ])
    assert board.fix_builds(root) == {"a.png": ["234"]}


def test_read_exposes_fixed_in(tmp_path):
    root = _board(tmp_path, [_ann("U2", aiReply="Build 234：x")])
    rows = board.read(root)
    assert rows[0]["fixedIn"] == "234"


def test_read_can_filter_by_image(tmp_path):
    root = _board(tmp_path, [
        _ann("U1", image="a.png", aiReply="Build 1：x"),
        _ann("U2", image="b.png", aiReply="Build 2：y"),
    ])
    assert [r["id"] for r in board.read(root, statuses=None)] == ["U1", "U2"]
    assert board.fix_builds(root, image="b.png") == {"b.png": ["2"]}
