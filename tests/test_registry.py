"""Board registry: lives in the project, stores relative paths, never loses boards."""

from __future__ import annotations

import json
from pathlib import Path

from ujshot import reviewboard


def _make_project(root: Path) -> Path:
    (root / ".git").mkdir(parents=True)
    return root


def test_project_root_found_from_subdir(tmp_path):
    proj = _make_project(tmp_path / "proj")
    deep = proj / "artifacts" / "shots"
    deep.mkdir(parents=True)
    assert reviewboard.project_root(deep) == proj


def test_registry_lives_in_project(tmp_path):
    proj = _make_project(tmp_path / "proj")
    assert reviewboard.default_registry(proj) == proj / ".ujshot" / "boards.json"


def test_registry_falls_back_to_home_without_project(tmp_path, monkeypatch):
    monkeypatch.setattr(reviewboard.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    lonely = tmp_path / "loose"
    lonely.mkdir()
    assert reviewboard.default_registry(lonely) == tmp_path / "home" / ".ujshot" / "boards.json"


def test_paths_stored_relative_to_project(tmp_path):
    proj = _make_project(tmp_path / "proj")
    board_dir = proj / "artifacts" / "ui-shots" / "cur"
    board_dir.mkdir(parents=True)

    reg = reviewboard.BoardRegistry(proj / ".ujshot" / "boards.json")
    reg.add(board_dir)

    raw = json.loads((proj / ".ujshot" / "boards.json").read_text(encoding="utf-8"))
    assert raw["boards"][0]["dir"] == "artifacts/ui-shots/cur", "仓库内路径必须存相对路径"
    again = reviewboard.BoardRegistry(proj / ".ujshot" / "boards.json")
    assert again.boards[0]["dir"] == str(board_dir.resolve())


def test_outside_project_keeps_absolute(tmp_path):
    proj = _make_project(tmp_path / "proj")
    outside = tmp_path / "elsewhere" / "shots"
    outside.mkdir(parents=True)
    reg = reviewboard.BoardRegistry(proj / ".ujshot" / "boards.json")
    reg.add(outside)
    raw = json.loads((proj / ".ujshot" / "boards.json").read_text(encoding="utf-8"))
    assert raw["boards"][0]["dir"] == str(outside.resolve())


def test_drop_missing_keeps_registry_when_everything_is_gone(tmp_path):
    """全都没了多半是刚 clone（artifacts/ 是 gitignore 的）：不能顺手清空清单。"""
    proj = _make_project(tmp_path / "proj")
    reg_path = proj / ".ujshot" / "boards.json"
    reg_path.parent.mkdir(parents=True)
    reg_path.write_text(json.dumps({"version": 1, "boards": [
        {"name": "a", "dir": "artifacts/a"},
        {"name": "b", "dir": "artifacts/b"},
    ]}), encoding="utf-8")

    reg = reviewboard.BoardRegistry(reg_path)
    gone = reg.drop_missing()

    assert sorted(gone) == ["a", "b"]
    kept = json.loads(reg_path.read_text(encoding="utf-8"))["boards"]
    assert len(kept) == 2, "全缺失时清单应原样保留，等截图回来再显示"


def test_drop_missing_removes_only_the_dead_one(tmp_path):
    proj = _make_project(tmp_path / "proj")
    alive = proj / "artifacts" / "alive"
    alive.mkdir(parents=True)
    reg_path = proj / ".ujshot" / "boards.json"
    reg_path.parent.mkdir(parents=True)
    reg_path.write_text(json.dumps({"version": 1, "boards": [
        {"name": "alive", "dir": "artifacts/alive"},
        {"name": "dead", "dir": "artifacts/dead"},
    ]}), encoding="utf-8")

    reg = reviewboard.BoardRegistry(reg_path)
    gone = reg.drop_missing()

    assert gone == ["dead"]
    assert [b["name"] for b in reg.boards] == ["alive"]


def test_forget_removes_only_the_registration(tmp_path):
    proj = _make_project(tmp_path / "proj")
    board_dir = proj / "artifacts" / "x"
    board_dir.mkdir(parents=True)
    reg = reviewboard.BoardRegistry(proj / ".ujshot" / "boards.json")
    reg.add(board_dir)

    assert reg.forget("x") is True
    assert reg.boards == []
    assert board_dir.is_dir(), "移除登记不许删磁盘上的截图"
    assert reg.forget("x") is False
