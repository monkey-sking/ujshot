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
    src = open(reviewboard.__file__, encoding="utf-8").read()
    for bad in ("onejot", "OneJot", "一记", "com.7fei.OneJot", "Scripts/ui-review-board"):
        assert bad not in src, f"评审板里还留着项目专属内容：{bad}"


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
