"""One service, many boards: serving a second board must not spawn a 2nd port."""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ujshot import board

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


class _Handler(BaseHTTPRequestHandler):
    """Minimal stand-in for the board service's /api/boards endpoint."""

    boards = []
    registered = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/api/boards":
            body = json.dumps({"boards": _Handler.boards, "default": "x"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(n) or b"{}")
        entry = {"name": "attached", "dir": payload.get("dir", "")}
        _Handler.registered.append(entry)
        body = json.dumps({"ok": True, "board": entry}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def fake_service():
    _Handler.boards = [{"name": "existing", "dir": "/tmp/x", "images": 1, "after": 0, "annotations": 0}]
    _Handler.registered = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield port
    srv.shutdown()


def test_serve_registers_with_running_service(tmp_path, fake_service):
    bdir = tmp_path / "my-board"
    bdir.mkdir()

    url = board.serve(str(bdir), script="", port=fake_service, open_browser=False)

    assert url == f"http://127.0.0.1:{fake_service}/b/attached/"
    assert _Handler.registered and _Handler.registered[0]["dir"] == str(bdir)
    assert (bdir / ".board-url").read_text().strip() == url


def test_serve_errors_when_registration_fails(tmp_path, monkeypatch):
    """A port that answers /api/boards but refuses registration must not be lied about."""
    bdir = tmp_path / "b"
    bdir.mkdir()
    monkeypatch.setattr(board, "_probe_service", lambda p: {"boards": []})
    monkeypatch.setattr(board, "_register", lambda p, d: None)

    with pytest.raises(RuntimeError):
        board.serve(str(bdir), script="", port=8799, open_browser=False)


def test_probe_returns_none_when_nothing_listening():
    assert board._probe_service(8798) is None
