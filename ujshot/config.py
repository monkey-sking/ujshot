"""Config discovery — everything project-specific lives here, not in the runner.

Lookup order (first hit wins, later sources override earlier ones):

1. built-in defaults
2. ``ujshot.toml`` found by walking up from the current directory
3. environment variables (``UJSHOT_BUNDLE``, ``UJSHOT_DEVICE``, ``UJSHOT_OUT``)
4. CLI flags (applied by the caller)

The file schema is deliberately flat so it can be parsed without a toml
dependency on Python 3.9 (stdlib ``tomllib`` only exists from 3.11):

.. code-block:: toml

    [app]
    bundle_id = "com.example.app"
    state_path = "Documents/MyApp/state.json"   # relative to the data container

    [device]
    name = "iPhone 18 Pro Max"
    appearance = "dark"

    [paths]
    out = "ujshot-out"

    [board]
    script = ""        # path to an optional review-board script
    port = 8766
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, Optional

CONFIG_NAME = "ujshot.toml"


@dataclass
class Config:
    bundle_id: str = ""
    state_path: str = ""              # relative to the app data container
    device: str = "iPhone 18 Pro Max"
    appearance: str = "dark"
    out_dir: str = "ujshot-out"
    flow_dir: str = "flows"
    board_script: str = ""
    board_port: int = 8766
    danger_words: list = field(default_factory=list)   # 覆盖默认的"会改数据"文案
    raw: Dict[str, str] = field(default_factory=dict)
    source: Optional[str] = None      # which ujshot.toml we loaded

    def describe(self) -> str:
        bits = [f"device={self.device}"]
        if self.bundle_id:
            bits.append(f"bundle={self.bundle_id}")
        bits.append(f"out={self.out_dir}")
        if self.source:
            bits.append(f"config={os.path.relpath(self.source)}")
        else:
            bits.append("config=(none)")
        return " · ".join(bits)


def find_config(start: str = ".") -> Optional[str]:
    """Walk up from *start* looking for ujshot.toml. Stops at the filesystem root."""
    here = os.path.abspath(start)
    while True:
        candidate = os.path.join(here, CONFIG_NAME)
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(here)
        if parent == here:
            return None
        here = parent


_SECTION_RE = re.compile(r"^\s*\[\s*([A-Za-z0-9_.-]+)\s*\]\s*$")
_KV_RE = re.compile(r"""^\s*([A-Za-z0-9_.-]+)\s*=\s*(.+?)\s*$""")
_STRIP = ('"', "'")


def parse_flat_toml(text: str) -> Dict[str, str]:
    """Parse the flat ``[section] key = value`` subset we use.

    A full TOML parser is overkill and would add a dependency on 3.9, so this
    handles exactly strings / ints / bools, which is all the config schema
    allows. Unknown syntax is ignored rather than fatal: a broken config must
    not block screenshots.
    """
    out: Dict[str, str] = {}
    section = ""
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip() if "#" in raw_line else raw_line.rstrip()
        if not line.strip():
            continue
        m = _SECTION_RE.match(line)
        if m:
            section = m.group(1)
            continue
        m = _KV_RE.match(line)
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in _STRIP:
            value = value[1:-1]
        out[f"{section}.{key}" if section else key] = value
    return out


def load(start: str = ".") -> Config:
    cfg = Config()
    path = find_config(start)
    if path:
        try:
            with open(path, encoding="utf-8") as f:
                flat = parse_flat_toml(f.read())
        except OSError:
            flat = {}
        cfg.raw = flat
        cfg.source = path
        base = os.path.dirname(path)          # relative paths resolve next to the config
        cfg.bundle_id = flat.get("app.bundle_id", cfg.bundle_id)
        cfg.state_path = flat.get("app.state_path", cfg.state_path)
        cfg.device = flat.get("device.name", cfg.device)
        cfg.appearance = flat.get("device.appearance", cfg.appearance)
        if "paths.out" in flat:
            cfg.out_dir = os.path.join(base, flat["paths.out"])
        if "paths.flow_dir" in flat:
            cfg.flow_dir = os.path.join(base, flat["paths.flow_dir"])
        if flat.get("board.script"):
            cfg.board_script = os.path.join(base, flat["board.script"])
        if flat.get("board.port"):
            try:
                cfg.board_port = int(flat["board.port"])
            except ValueError:
                pass
        if flat.get("safety.danger_words"):
            # 逗号分隔即可，避免为列表写完整 TOML 解析
            cfg.danger_words = [w.strip() for w in flat["safety.danger_words"].split(",") if w.strip()]
    else:
        base = os.getcwd()

    if os.environ.get("UJSHOT_BUNDLE"):
        cfg.bundle_id = os.environ["UJSHOT_BUNDLE"]
    if os.environ.get("UJSHOT_DEVICE"):
        cfg.device = os.environ["UJSHOT_DEVICE"]
    if os.environ.get("UJSHOT_OUT"):
        cfg.out_dir = os.environ["UJSHOT_OUT"]

    if not os.path.isabs(cfg.out_dir):
        cfg.out_dir = os.path.join(base, cfg.out_dir)
    if not os.path.isabs(cfg.flow_dir):
        cfg.flow_dir = os.path.join(base, cfg.flow_dir)
    return cfg


def resolve_flow(cfg: Config, flow: str) -> str:
    """Flow paths may be absolute, cwd-relative, or bare names inside flow_dir."""
    if os.path.isabs(flow):
        return flow
    if os.path.exists(flow):
        return os.path.abspath(flow)
    for suffix in ("", ".json"):
        candidate = os.path.join(cfg.flow_dir, flow + suffix)
        if os.path.exists(candidate):
            return candidate
    return flow
