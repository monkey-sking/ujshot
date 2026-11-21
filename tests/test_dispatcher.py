"""Tests for the local agent dispatcher and serial per-repo queue."""

import sys
import time
from pathlib import Path

from ujshot import dispatcher


def test_discover_agents_loads_toml_override(tmp_path):
    (tmp_path / "ujshot.toml").write_text(
        '[agents.custom]\nlabel = "Custom Bot"\ncmd = ["' + sys.executable + '", "-c", "print(1)"]\nstdin = false\n',
        encoding="utf-8",
    )
    specs = {s.id: s for s in dispatcher.discover_agents(tmp_path)}
    assert "custom" in specs
    assert specs["custom"].label == "Custom Bot"
    assert specs["custom"].available is True
    assert specs["custom"].source == "config"


def test_build_prompt_includes_exact_screenshot_and_closed_loop_rules(tmp_path):
    bdir = tmp_path / "board"
    shot = bdir / "after" / "light" / "233" / "01-home.png"
    shot.parent.mkdir(parents=True)
    shot.write_bytes(b"png")

    prompt = dispatcher.build_prompt(
        bdir,
        tmp_path,
        [{
            "id": "U9",
            "image": "01-home.png",
            "appearance": "light",
            "raisedIn": "233",
            "kind": "box",
            "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4,
            "text": "按钮太挤",
            "rejectReason": "左边还是贴边",
        }],
        appearance="light",
        image="01-home.png",
    )
    assert str(shot) in prompt
    assert "--appearance light" in prompt
    assert "左边还是贴边" in prompt
    assert "不要执行 `git commit`" in prompt


def test_serial_queue_runs_jobs_sequentially_and_streams_logs(tmp_path):
    bdir = tmp_path / "board"
    bdir.mkdir()
    marker = tmp_path / "order.txt"
    py_script = (
        "import sys, time, pathlib; "
        "prompt = sys.stdin.read(); "
        "tag = 'J1' if 'U1' in prompt else 'J2'; "
        f"p = pathlib.Path({str(marker)!r}); "
        "p.open('a').write(tag + ':start\\n'); "
        "print('hello-' + tag, flush=True); "
        "time.sleep(0.15); "
        "p.open('a').write(tag + ':end\\n')"
    )
    spec = dispatcher.AgentSpec(
        id="fake", label="Fake", cmd=[sys.executable, "-c", py_script], stdin=True, available=True
    )
    mgr = dispatcher.DispatchManager()
    j1 = mgr.enqueue(bdir, tmp_path, spec, [{"id": "U1", "image": "a.png", "text": "fix 1"}])
    j2 = mgr.enqueue(bdir, tmp_path, spec, [{"id": "U2", "image": "a.png", "text": "fix 2"}])

    deadline = time.time() + 5
    while time.time() < deadline:
        s1 = mgr.get_job(j1.id)
        s2 = mgr.get_job(j2.id)
        if s1["status"] == "done" and s2["status"] == "done":
            break
        time.sleep(0.05)

    s1 = mgr.get_job(j1.id, include_logs=True)
    s2 = mgr.get_job(j2.id, include_logs=True)
    assert s1["status"] == "done"
    assert s2["status"] == "done"
    assert any("hello-J1" in line for line in s1["logLines"])
    assert any("hello-J2" in line for line in s2["logLines"])
    assert Path(s1["logPath"]).is_file()
    # Strictly serial: J1 ends before J2 starts
    assert marker.read_text(encoding="utf-8").splitlines() == [
        "J1:start", "J1:end", "J2:start", "J2:end"
    ]


def test_stream_json_lines_formatted_into_readable_progress():
    mgr = dispatcher.DispatchManager()
    assert mgr._format_display_lines('{"type":"system","subtype":"init","model":"claude-sonnet","session_id":"abcdef123456"}') == [
        "[会话] 已连接 model=claude-sonnet id=abcdef12"
    ]
    tool_evt = (
        '{"type":"assistant","message":{"content":['
        '{"type":"tool_use","name":"Read","input":{"file_path":"App/Sources/Foo.swift"}},'
        '{"type":"text","text":"准备修改对齐间距"}'
        ']}}'
    )
    assert mgr._format_display_lines(tool_evt) == [
        "[工具] Read · App/Sources/Foo.swift",
        "[回复] 准备修改对齐间距",
    ]
    assert mgr._format_display_lines('{"type":"result","result":"已完成修改","num_turns":4}') == [
        "[结果] 已完成修改（4 轮）"
    ]
    assert mgr._format_display_lines('{"type":"user","message":{}}') == []
    assert mgr._format_display_lines("plain stdout line") == ["plain stdout line"]


def test_session_id_persisted_and_resumed_across_restart(tmp_path):
    bdir = tmp_path / "board"
    bdir.mkdir()
    py_script = (
        "import sys; "
        "args = sys.argv[1:]; "
        "_ = sys.stdin.read(); "
        "print('{\"type\":\"system\",\"subtype\":\"init\",\"model\":\"test\",\"session_id\":\"sess-12345678\"}', flush=True); "
        "print('args=' + '|'.join(args), flush=True)"
    )
    spec = dispatcher.AgentSpec(
        id="fake",
        label="Fake",
        cmd=[sys.executable, "-c", py_script, "fresh"],
        resume_cmd=[sys.executable, "-c", py_script, "resume", "{session_id}"],
        stdin=True,
        available=True,
    )
    mgr1 = dispatcher.DispatchManager()
    j1 = mgr1.enqueue(bdir, tmp_path, spec, [{"id": "U34", "image": "a.png", "text": "fix"}])
    deadline = time.time() + 5
    while time.time() < deadline and mgr1.get_job(j1.id)["status"] not in ("done", "failed"):
        time.sleep(0.05)
    assert mgr1.get_job(j1.id)["sessionId"] == "sess-12345678"
    assert (bdir / ".dispatch-logs" / "jobs.json").is_file()

    # Simulate a brand-new DispatchManager after service restart
    mgr2 = dispatcher.DispatchManager()
    loaded = mgr2.list_jobs(bdir)
    assert len(loaded) == 1
    assert loaded[0]["sessionId"] == "sess-12345678"

    j2 = mgr2.enqueue(
        bdir, tmp_path, spec,
        [{"id": "U34", "image": "a.png", "text": "fix"}],
        resume_session_id=loaded[0]["sessionId"],
    )
    deadline = time.time() + 5
    while time.time() < deadline and mgr2.get_job(j2.id)["status"] not in ("done", "failed"):
        time.sleep(0.05)
    d2 = mgr2.get_job(j2.id, include_logs=True)
    assert d2["status"] == "done"
    assert d2["resumedFrom"] == "sess-12345678"
    assert any("args=resume|sess-12345678" in line for line in d2["logLines"])
