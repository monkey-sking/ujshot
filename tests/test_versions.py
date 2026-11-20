"""Version stamping: read the installed app's Info.plist, record it on the board."""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ujshot import board, device  # noqa: E402


def _make_app(tmp_path, short="0.1.0", build="237"):
    import plistlib
    app = tmp_path / "Demo.app"
    app.mkdir()
    with open(app / "Info.plist", "wb") as f:
        plistlib.dump({"CFBundleShortVersionString": short,
                       "CFBundleVersion": build}, f)
    return str(app)


def test_app_version_reads_installed_plist(tmp_path):
    app = _make_app(tmp_path, "0.1.0", "237")
    v = device.app_version("unused-udid", "com.example.app", app)
    assert v["short"] == "0.1.0"
    assert v["build"] == "237"
    assert v["label"] == "0.1.0 (237)"


def test_app_version_never_guesses_when_plist_missing(tmp_path):
    # 读不到就留空，不能编一个版本号塞进证据里
    v = device.app_version("no-such-udid", "com.example.app", str(tmp_path / "nope.app"))
    assert v["short"] == "" and v["build"] == "" and v["label"] == ""


def test_app_version_label_with_only_build(tmp_path):
    import plistlib
    app = tmp_path / "B.app"
    app.mkdir()
    with open(app / "Info.plist", "wb") as f:
        plistlib.dump({"CFBundleVersion": "42"}, f)
    v = device.app_version("u", "b", str(app))
    assert v["label"] == "42"


def test_write_versions_creates_batch_label(tmp_path):
    bd = str(tmp_path / "board")
    os.makedirs(bd)
    data = board.write_versions(bd, "after", "0.1.0 (237)")
    assert data["after"] == "0.1.0 (237)"
    on_disk = json.load(open(os.path.join(bd, "versions.json"), encoding="utf-8"))
    assert on_disk["after"] == "0.1.0 (237)"


def test_write_versions_per_image_override_does_not_clobber_batch(tmp_path):
    bd = str(tmp_path / "board")
    os.makedirs(bd)
    board.write_versions(bd, "after", "0.1.0 (237)")
    data = board.write_versions(bd, "after", "0.1.0 (236)", image="old.png")
    assert data["after"] == "0.1.0 (237)"          # 整批标签不变
    assert data["overrides"]["after"]["old.png"] == "0.1.0 (236)"  # 单张例外生效


def test_write_versions_rejects_unknown_side(tmp_path):
    with pytest.raises(ValueError):
        board.write_versions(str(tmp_path), "middle", "x")


def test_versions_survive_annotation_wipe(tmp_path):
    # 版本和批注分开存：清空批注不该把"这是哪个 Build"一起抹掉
    import plistlib  # noqa: F401
    bd = str(tmp_path / "board")
    os.makedirs(bd)
    board.write_versions(bd, "before", "0.1.0 (233)")
    with open(os.path.join(bd, "annotations.json"), "w", encoding="utf-8") as f:
        json.dump({"version": 1, "directory": "board", "annotations": []}, f)
    data = json.load(open(os.path.join(bd, "versions.json"), encoding="utf-8"))
    assert data["before"] == "0.1.0 (233)"
