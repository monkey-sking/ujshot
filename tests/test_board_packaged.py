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
