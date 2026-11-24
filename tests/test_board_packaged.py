"""The board is now part of ujshot: packaged entry point, no project-local script."""

import json
import os

from ujshot import reviewboard


def test_package_module_runs_as_script(tmp_path, capsys):
    """`python -m ujshot.reviewboard --list` 必须在任何目录下都能跑。"""
    import subprocess
    import sys

    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = subprocess.run([sys.executable, "-m", "ujshot.reviewboard", "--list"],
                         capture_output=True, text=True, env=env, cwd=str(tmp_path))
    assert out.returncode == 0, out.stderr
    payload = json.loads(out.stdout)
    assert "boards" in payload
    assert "registry" in payload


def test_registry_defaults_inside_the_project(tmp_path):
    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True)
    assert reviewboard.project_root(proj) == proj
    assert reviewboard.default_registry(proj) == proj / ".ujshot" / "boards.json"


def test_simulator_controller_has_no_hardcoded_bundle():
    """bundle id 属于项目，不属于工具：默认必须是空。"""
    sim = reviewboard.SimulatorController.__init__
    assert sim.__defaults__[0] is None or sim.__defaults__[0] == ""
    ctrl = reviewboard.SimulatorController(udid=None)
    assert ctrl.bundle_id == ""


def test_no_project_name_leaks_into_the_tool():
    from pathlib import Path
    root = Path(reviewboard.__file__).resolve().parent.parent
    files_to_scan = [
        Path(reviewboard.__file__),
        reviewboard.HTML_ASSET,
        root / "README.md",
        root / "ROADMAP.md",
        root / "pyproject.toml",
        root / "skills" / "ujshot" / "SKILL.md",
        root / "skills" / "ui-review-board" / "SKILL.md",
    ]
    for p in files_to_scan:
        assert p.is_file(), f"缺少内置文件：{p}"
        src = p.read_text(encoding="utf-8")
        for bad in (
            "onejot", "OneJot", "一记", "com.7fei", "LifeOS", "lifeos",
            "Scripts/ui-review-board", "--ui-test-dark", "/Users/",
        ):
            assert bad not in src, f"{p.name} 里还留着项目或个人路径专属内容：{bad}"


def test_appearance_slots_are_isolated(tmp_path):
    """暗色与亮色必须分槽存放：dark 读不到 after/light，light 读不到 dark。"""
    from types import SimpleNamespace
    from ujshot import board

    out = tmp_path / "run_out"
    (out / "screens").mkdir(parents=True)
    (out / "screens" / "01-home.png").write_bytes(b"png-bytes")
    shots = [SimpleNamespace(id="01-home", verdict="ok", expect=[], expect_absent=[], text="")]

    bdir = tmp_path / "board"
    board.copy_shots(str(bdir), str(out), shots, as_after=False, appearance="dark")
    board.copy_shots(str(bdir), str(out), shots, as_after=True, round_build="237", appearance="dark")
    board.copy_shots(str(bdir), str(out), shots, as_after=False, appearance="light")
    board.copy_shots(str(bdir), str(out), shots, as_after=True, round_build="233", appearance="light")

    assert reviewboard.list_before_images(bdir, "dark") == ["01-home.png"]
    assert reviewboard.list_before_images(bdir, "light") == ["01-home.png"]
    assert reviewboard.list_rounds(bdir, "dark") == [{"build": "237", "images": ["01-home.png"]}]
    assert reviewboard.list_rounds(bdir, "light") == [{"build": "233", "images": ["01-home.png"]}]

    board.write_annotations(str(bdir), shots, as_after=False, appearance="dark")
    board.write_annotations(str(bdir), shots, as_after=False, appearance="light")
    anns = json.loads((bdir / "annotations.json").read_text(encoding="utf-8"))["annotations"]
    assert len(anns) == 2
    assert {a["appearance"] for a in anns} == {"dark", "light"}


def test_launch_app_uses_clean_args_and_syncs_appearance(monkeypatch):
    import pytest
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))

    monkeypatch.setattr(reviewboard.subprocess, "run", fake_run)
    monkeypatch.setattr(reviewboard.SimulatorController, "_load_display_info", lambda self: None)

    empty_ctrl = reviewboard.SimulatorController(udid="FAKE-UDID", bundle_id="")
    with pytest.raises(RuntimeError):
        empty_ctrl.launch_app()

    ctrl = reviewboard.SimulatorController(udid="FAKE-UDID", bundle_id="com.example.demo")
    ctrl.launch_app(appearance="light")
    assert calls == [
        ["xcrun", "simctl", "ui", "FAKE-UDID", "appearance", "light"],
        ["xcrun", "simctl", "launch", "FAKE-UDID", "com.example.demo"],
    ]


def test_freeze_png_writes_to_matching_appearance_slot(tmp_path, monkeypatch):
    def fake_run(cmd, **kwargs):
        out_path = cmd[-1]
        with open(out_path, "wb") as f:
            f.write(b"frozen-png")

    monkeypatch.setattr(reviewboard.subprocess, "run", fake_run)
    monkeypatch.setattr(reviewboard.SimulatorController, "_load_display_info", lambda self: None)
    ctrl = reviewboard.SimulatorController(udid="FAKE-UDID", bundle_id="com.example.demo")

    bdir = tmp_path / "board"
    bdir.mkdir()
    dark_name = ctrl.freeze_png(bdir, appearance="dark")
    light_name = ctrl.freeze_png(bdir, appearance="light")

    assert (bdir / dark_name).is_file()
    assert (bdir / "light" / light_name).is_file()
    assert reviewboard.list_before_images(bdir, "dark") == [dark_name]
    assert reviewboard.list_before_images(bdir, "light") == [light_name]


def test_registry_summary_counts_light_only_boards(tmp_path):
    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True)
    bdir = proj / "artifacts" / "light-board"
    (bdir / "light").mkdir(parents=True)
    (bdir / "after" / "light" / "240").mkdir(parents=True)
    (bdir / "light" / "01-home.png").write_bytes(b"png")
    (bdir / "after" / "light" / "240" / "01-home.png").write_bytes(b"png")

    reg = reviewboard.BoardRegistry(proj / ".ujshot" / "boards.json")
    reg.add(bdir)
    summary = reg.summary()
    assert summary[0]["images"] == 1
    assert summary[0]["after"] == 1
    assert summary[0]["cover"] == "01-home.png"


def test_reviewboard_html_asset_extracted_and_hardened():
    assert reviewboard.HTML_ASSET.is_file()
    html = reviewboard.HTML_PAGE
    assert "function esc(" in html
    assert "[亮色]" in html and "[暗色]" in html
    assert "appearance: currentAppearance" in html


def test_reviewboard_html_multi_version_and_redesigned_cards():
    html = reviewboard.HTML_ASSET.read_text(encoding="utf-8")
    for token in (
        "leftVerSelect",
        "rightVerSelect",
        "versionTimeline",
        "curtainStage",
        "function versionsForImage(",
        "function resolveStageVersions(",
        "function swapStageVersions(",
        "function setStageViewMode(",
    ):
        assert token in html, f"缺少多版本对比要素：{token}"
    for token in (
        "st-badge",
        "st-proposed",
        "st-approved",
        "btn-approve",
        "batchApproveBtn",
        "approveScreen",
        "pillUnreviewed",
        "pillOk",
        "isScreenUnreviewed",
        "hiddenStatuses",
        "more-menu",
        "card-actions",
        "resumableJobForAnn",
        "resumeSessionId",
        "logDrawerExpanded",
        "function liveAnn(",
        "persistQueued",
    ):
        assert token in html, f"缺少卡片/批准/状态开关重构要素：{token}"
    assert 'className = "status-row"' not in html
    assert 'id="hideOkToggle"' not in html

