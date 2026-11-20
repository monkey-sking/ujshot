"""Shared datatypes. Kept tiny and dependency-free."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Device:
    name: str
    udid: str
    state: str = "Shutdown"
    runtime: str = ""

    def line(self) -> str:
        return f"{self.udid}  {self.state:9s} {self.name}  ({self.runtime})"


@dataclass
class Step:
    """One executed step. `verdict` is only set by shots and taps."""
    id: str
    title: str = ""
    ok: bool = True
    verdict: Optional[str] = None
    diff_pct: Optional[float] = None
    nodes: int = 0
    text: str = ""
    expect: List[str] = field(default_factory=list)
    expect_absent: List[str] = field(default_factory=list)
    note: str = ""
    parent: Optional[str] = None
    how: str = ""
    error: str = ""

    def as_dict(self) -> Dict[str, Any]:
        d = {"id": self.id, "title": self.title, "ok": self.ok}
        for k in ("verdict", "diff_pct", "nodes", "note", "parent", "how", "error"):
            v = getattr(self, k)
            if v not in (None, "", 0) or k == "nodes":
                d[k] = v
        if self.text:
            d["text"] = self.text[:400]
        if self.expect:
            d["expect"] = self.expect
        if self.expect_absent:
            d["expect_absent"] = self.expect_absent
        return d

    @property
    def is_shot(self) -> bool:
        return self.diff_pct is not None or self.nodes > 0


@dataclass
class RunResult:
    flow: str
    outdir: str
    device: Device
    steps: List[Step] = field(default_factory=list)
    failures: int = 0

    @property
    def shots(self) -> List[Step]:
        return [s for s in self.steps if s.is_shot]

    @property
    def anomalies(self) -> List[Step]:
        return [s for s in self.shots if s.verdict != "ok"]

    @property
    def ok(self) -> bool:
        return self.failures == 0
