"""Flow file loading + validation.

A flow is plain JSON so a non-programmer can add a case, and so a diff of a
flow reads like a test plan. Validation is strict about typos (an unknown ``op``
or a missing ``id`` on a shot is a hard error, not a silent skip) because a
typo'd flow that "passes" is worse than one that refuses to run.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

OPS = {"shot", "tap", "swipe", "type", "wait", "expect", "launch", "back", "ensure",
       "clipboard", "key"}

REQUIRED = {
    "shot": ("id",),
    "tap": (),
    "swipe": (),
    "type": ("text",),
    "wait": (),
    "expect": ("text",),
    "launch": (),
    "back": (),
    "ensure": ("text",),
    "clipboard": ("text",),
    "key": ("code",),
}


class FlowError(ValueError):
    pass


@dataclass
class Flow:
    name: str
    steps: List[Dict[str, Any]] = field(default_factory=list)
    launch_args: List[str] = field(default_factory=list)
    restart: bool = True
    start_tab: Optional[str] = None
    before_map: Dict[str, str] = field(default_factory=dict)
    note: str = ""
    path: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def shot_ids(self) -> List[str]:
        return [s["id"] for s in self.steps if s.get("op") == "shot"]


def load(path: str) -> Flow:
    if not os.path.exists(path):
        raise FlowError(f"流程文件不存在：{path}")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        raise FlowError(f"流程文件不是合法 JSON（{path}）：第 {exc.lineno} 行 {exc.msg}") from exc
    validate(data, path)
    launch = data.get("launch", {}) or {}
    return Flow(
        name=data.get("name") or os.path.splitext(os.path.basename(path))[0],
        steps=data.get("steps", []),
        launch_args=list(launch.get("args") or []),
        restart=bool(launch.get("restart", True)),
        start_tab=data.get("startTab"),
        before_map=data.get("beforeMap") or {},
        note=data.get("note", ""),
        path=os.path.abspath(path),
        raw=data,
    )


def validate(data: Dict[str, Any], path: str = "") -> None:
    where = f"（{path}）" if path else ""
    if not isinstance(data, dict):
        raise FlowError(f"流程文件顶层必须是对象{where}")
    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        raise FlowError(f"流程文件缺少非空 steps 数组{where}")

    seen = set()
    for i, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            raise FlowError(f"第 {i} 步不是对象{where}")
        op = step.get("op")
        if op not in OPS:
            raise FlowError(
                f"第 {i} 步 op={op!r} 不认识{where}；可用：{', '.join(sorted(OPS))}")
        for key in REQUIRED.get(op, ()):
            if key not in step:
                raise FlowError(f"第 {i} 步（{op}）缺少必填字段 {key}{where}")
        if op == "shot":
            sid = step["id"]
            if sid in seen:
                raise FlowError(f"第 {i} 步 shot id 重复：{sid}{where}（id 也是图片文件名）")
            seen.add(sid)
        if op in ("tap", "ensure"):
            recovery = step.get("then") or []
            if isinstance(recovery, dict):
                recovery = [recovery]
            targets = [step] + list(recovery)
            for t in targets:
                if not isinstance(t, dict):
                    continue
                has = any(k in t for k in ("id", "label", "x", "y")) or (op == "ensure")
                if not has:
                    raise FlowError(f"第 {i} 步（{op}）没有任何定位条件（id / label / x+y）{where}")

    for sid, before in (data.get("beforeMap") or {}).items():
        if sid not in seen:
            raise FlowError(f"beforeMap 里的 {sid} 不是任何 shot 的 id{where}")


def new_flow_template(name: str) -> Dict[str, Any]:
    return {
        "name": name,
        "note": "写清这条流程要证明什么；note 会显示在图册里",
        "startTab": "tab.home",
        "steps": [
            {"op": "shot", "id": "01-home", "title": "首页", "note": "第一屏"},
            {"op": "tap", "label": "设置", "wait": 2.2},
            {"op": "shot", "id": "02-settings", "title": "设置页",
             "note": "断言标题在，且不该出现的词不出现",
             "expect": ["设置"], "expectAbsent": []},
        ],
    }
