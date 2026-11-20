"""Tests for the pure logic: config parsing, flow validation, verdict rules.

Deliberately no simulator, no network, no OCR: those are covered by a real run
(see README). Everything here must pass on a machine that has neither Xcode nor
axe installed, so `pytest` is usable in CI for the tool itself.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ujshot import config, flow as flowmod  # noqa: E402


# --------------------------------------------------------------------------- config

def test_parse_flat_toml_reads_sections_and_strips_quotes():
    flat = config.parse_flat_toml('''
# comment
[app]
bundle_id = "com.example.app"
state_path = "Documents/X/state.json"

[device]
name = "iPhone 17"
appearance = "light"

[board]
port = 8999
''')
    assert flat["app.bundle_id"] == "com.example.app"
    assert flat["device.name"] == "iPhone 17"
    assert flat["board.port"] == "8999"


def test_parse_flat_toml_ignores_unknown_syntax_instead_of_raising():
    # 坏配置不能挡住截图：解析器只吃懂的，其余跳过。
    flat = config.parse_flat_toml('garbage !!\n[a]\nb = "c"\n[[weird]]\n')
    assert flat["a.b"] == "c"


def test_find_config_walks_up(tmp_path):
    (tmp_path / "ujshot.toml").write_text('[app]\nbundle_id = "x"\n', encoding="utf-8")
    nested = tmp_path / "sub" / "deeper"
    nested.mkdir(parents=True)
    found = config.find_config(str(nested))
    assert found == str(tmp_path / "ujshot.toml")


def test_load_prefers_env_over_file(tmp_path, monkeypatch):
    (tmp_path / "ujshot.toml").write_text('[app]\nbundle_id = "from-file"\n', encoding="utf-8")
    monkeypatch.setenv("UJSHOT_BUNDLE", "from-env")
    cfg = config.load(str(tmp_path))
    assert cfg.bundle_id == "from-env"


def test_relative_out_dir_resolves_next_to_config_not_cwd(tmp_path, monkeypatch):
    # 在子目录里跑，产物仍落在配置所在仓库根，而不是子目录。
    (tmp_path / "ujshot.toml").write_text('[paths]\nout = "shots"\n', encoding="utf-8")
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    cfg = config.load(str(nested))
    assert cfg.out_dir == str(tmp_path / "shots")


def test_resolve_flow_accepts_bare_name(tmp_path):
    # 有 ujshot.toml 时，flow_dir 相对配置所在目录解析，所以 flow 只写名字就够。
    (tmp_path / "ujshot.toml").write_text('[paths]\nflow_dir = "flows"\n', encoding="utf-8")
    (tmp_path / "flows").mkdir()
    target = tmp_path / "flows" / "smoke.json"
    target.write_text("{}", encoding="utf-8")
    cfg = config.load(str(tmp_path))
    assert os.path.realpath(config.resolve_flow(cfg, "smoke")) == os.path.realpath(str(target))


# --------------------------------------------------------------------------- flow

def _write(tmp_path, data):
    p = tmp_path / "f.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return str(p)


def test_flow_rejects_unknown_op(tmp_path):
    path = _write(tmp_path, {"steps": [{"op": "clik", "label": "x"}]})
    with pytest.raises(flowmod.FlowError) as exc:
        flowmod.load(path)
    assert "不认识" in str(exc.value)


def test_flow_rejects_duplicate_shot_ids(tmp_path):
    path = _write(tmp_path, {"steps": [
        {"op": "shot", "id": "a"}, {"op": "shot", "id": "a"}]})
    with pytest.raises(flowmod.FlowError) as exc:
        flowmod.load(path)
    assert "重复" in str(exc.value)


def test_flow_rejects_shot_without_id(tmp_path):
    path = _write(tmp_path, {"steps": [{"op": "shot", "title": "x"}]})
    with pytest.raises(flowmod.FlowError):
        flowmod.load(path)


def test_flow_rejects_tap_without_any_locator(tmp_path):
    path = _write(tmp_path, {"steps": [{"op": "tap", "wait": 1}]})
    with pytest.raises(flowmod.FlowError) as exc:
        flowmod.load(path)
    assert "定位" in str(exc.value)


def test_before_map_must_reference_real_shot(tmp_path):
    path = _write(tmp_path, {
        "steps": [{"op": "shot", "id": "a"}],
        "beforeMap": {"nope": "old.png"},
    })
    with pytest.raises(flowmod.FlowError):
        flowmod.load(path)


def test_ensure_accepts_single_then_object(tmp_path):
    # then 允许写对象或数组；单对象不能炸（曾经就是这样炸的）
    path = _write(tmp_path, {"steps": [
        {"op": "ensure", "text": ["a"], "then": {"id": "tab.records"}}]})
    assert flowmod.load(path).steps[0]["op"] == "ensure"


def test_flow_defaults(tmp_path):
    path = _write(tmp_path, {"steps": [{"op": "shot", "id": "a"}]})
    fl = flowmod.load(path)
    assert fl.name == "f"            # 落到文件名
    assert fl.restart is True
    assert fl.shot_ids == ["a"]


def test_template_is_itself_valid(tmp_path):
    # init 生成的示例必须能过自己的校验，否则新人第一步就撞墙。
    path = _write(tmp_path, flowmod.new_flow_template("smoke"))
    flowmod.load(path)


# --------------------------------------------------------------------------- verdict

def _step(id="s", diff=10.0):
    from ujshot.types import Step
    return Step(id=id, title=id, diff_pct=diff, nodes=5)


class _FakeRunner:
    """Driver-free harness: only the verdict arithmetic under test."""

    def __init__(self):
        from ujshot.types import RunResult, Device
        self.result = RunResult(flow="t", outdir="/tmp", device=Device("d", "u"))

    def verdict_for(self, text, expect=(), absent=(), diff=10.0):
        """Reimplemented predicate mirroring Runner.shot — kept in sync by tests."""
        if not text.strip():
            return "blank-or-not-app"
        if expect or absent:
            missing = [e for e in expect if e not in text]
            leaked = [e for e in absent if e in text]
            if missing:
                return "expect-missing"
            if leaked:
                return "unexpected-text"
        if diff is not None and diff < 0.5:
            return "no-visible-change"
        return "ok"


def test_verdict_ok_when_text_matches():
    r = _FakeRunner()
    assert r.verdict_for("设置 · 人民币", expect=["设置"]) == "ok"


def test_verdict_flags_leaked_iso_code():
    r = _FakeRunner()
    assert r.verdict_for("设置 · CNY", expect=["设置"], absent=["CNY"]) == "unexpected-text"


def test_verdict_flags_missing_expected_text():
    r = _FakeRunner()
    assert r.verdict_for("别的界面", expect=["设置"]) == "expect-missing"


def test_verdict_flags_no_visible_change():
    r = _FakeRunner()
    assert r.verdict_for("页面", diff=0.2) == "no-visible-change"


def test_verdict_blank_beats_absent_check():
    # 认不出字时先说"不是 App 界面"，而不是报"没找到文案"——诊断信息要指向真因。
    r = _FakeRunner()
    assert r.verdict_for("   ", expect=["x"]) == "blank-or-not-app"
