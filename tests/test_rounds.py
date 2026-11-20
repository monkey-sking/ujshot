"""Rounds: the same issue fixed twice must keep both attempts."""

import json
import os

from ujshot import board


class Shot:
    def __init__(self, sid):
        self.id = sid


def _fake_run(root, sid, payload):
    d = os.path.join(root, "run", "screens")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"{sid}.png"), "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + payload)
    return os.path.join(root, "run")


def test_rounds_keep_both_attempts(tmp_path):
    board_dir = str(tmp_path / "board")
    root = str(tmp_path)

    out1 = _fake_run(root, "01-fab", b"first-attempt-234")
    board.copy_shots(board_dir, out1, [Shot("01-fab")], as_after=True, round_build="234")

    out2 = _fake_run(root, "01-fab", b"second-attempt-235")
    board.copy_shots(board_dir, out2, [Shot("01-fab")], as_after=True, round_build="235")

    first = os.path.join(board_dir, "after", "234", "01-fab.png")
    second = os.path.join(board_dir, "after", "235", "01-fab.png")
    assert os.path.exists(first), "第一轮的图不能被第二轮覆盖掉"
    assert os.path.exists(second)
    assert open(first, "rb").read().endswith(b"first-attempt-234")
    assert open(second, "rb").read().endswith(b"second-attempt-235")


def test_round_build_optional_keeps_old_layout(tmp_path):
    board_dir = str(tmp_path / "board")
    out1 = _fake_run(str(tmp_path), "02-settings", b"x")
    board.copy_shots(board_dir, out1, [Shot("02-settings")], as_after=True)
    assert os.path.exists(os.path.join(board_dir, "after", "02-settings.png"))


def test_write_versions_records_round_label(tmp_path):
    board_dir = str(tmp_path / "board")
    os.makedirs(board_dir)

    board.write_versions(board_dir, "after", "0.1.0 (234)", round_build="234")
    board.write_versions(board_dir, "after", "0.1.0 (235)", round_build="235")

    with open(os.path.join(board_dir, "versions.json"), encoding="utf-8") as f:
        data = json.load(f)
    assert data["rounds"] == {"234": "0.1.0 (234)", "235": "0.1.0 (235)"}
    assert data["after"] == "0.1.0 (235)"     # 整批取最新一轮


def test_write_versions_without_round_has_empty_rounds(tmp_path):
    board_dir = str(tmp_path / "board")
    os.makedirs(board_dir)
    data = board.write_versions(board_dir, "before", "0.1.0 (233)")
    assert data["rounds"] == {}
    assert data["before"] == "0.1.0 (233)"
