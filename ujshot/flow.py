"""Flow file loading + validation.

A flow is plain JSON so a non-programmer can add a case, and so a diff of a
flow reads like a test plan. Validation is strict about typos (an unknown ``op``
or a missing ``id`` on a shot is a hard error, not a silent skip) because a
typo'd flow that "passes" is worse than one that refuses to run.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

_HAN_RE = re.compile(r"[\u4e00-\u9fff]")
_SHOT_ID_THREE_PART_RE = re.compile(r"^\d{2,3}[a-z]?-[^-/\s]+-[^-/\s]+$")
_SHOT_ID_EN_RE = re.compile(r"^\d{2,3}[a-z]?-[a-z0-9]+(?:-[a-z0-9]+)+$")

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


def load(path: str, shot_id_style: str = "") -> Flow:
    if not os.path.exists(path):
        raise FlowError(f"流程文件不存在：{path}")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        raise FlowError(f"流程文件不是合法 JSON（{path}）：第 {exc.lineno} 行 {exc.msg}") from exc
    validate(data, path, shot_id_style=shot_id_style)
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


def validate_shot_id(sid: str, style: str = "") -> Optional[str]:
    """Validate a single shot.id against the configured naming style."""
    if not isinstance(sid, str) or not sid.strip():
        return "shot id 必须是非空字符串"
    if "/" in sid or "\\" in sid or sid.endswith(".png"):
        return f"shot id 不能包含路径分隔符或 .png 后缀：{sid}"
    style = (style or "").strip().lower()
    if style == "zh":
        if not _SHOT_ID_THREE_PART_RE.match(sid) or not _HAN_RE.search(sid):
            return f"shot id 不符合中文三段规范「编号-页面-状态」（例：01-首页-默认态）：{sid}"
    elif style == "en":
        if not _SHOT_ID_EN_RE.match(sid):
            return f"shot id 不符合英文规范「NN-page-state」（例：01-home-default）：{sid}"
    return None


def validate(data: Dict[str, Any], path: str = "", shot_id_style: str = "") -> None:
    where = f"（{path}）" if path else ""
    if not isinstance(data, dict):
        raise FlowError(f"流程文件顶层必须是对象{where}")
    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        raise FlowError(f"流程文件缺少非空 steps 数组{where}")

    seen = set()
    has_zh_id = False
    has_non_zh_id = False
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
            naming_err = validate_shot_id(sid, style=shot_id_style)
            if naming_err:
                raise FlowError(f"第 {i} 步 {naming_err}{where}")
            if _HAN_RE.search(sid):
                has_zh_id = True
            else:
                has_non_zh_id = True
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

    if has_zh_id and has_non_zh_id:
        raise FlowError(f"同一流程内禁止中英文 shot id 混排{where}（请统一为中文「编号-页面-状态」或统一英文）")

    for sid, before in (data.get("beforeMap") or {}).items():
        if sid not in seen:
            raise FlowError(f"beforeMap 里的 {sid} 不是任何 shot 的 id{where}")


def new_flow_template(name: str) -> Dict[str, Any]:
    return {
        "name": name,
        "note": "写清这条流程要证明什么；note 会显示在图册里",
        "startTab": "tab.home",
        "steps": [
            {"op": "shot", "id": "01-首页-默认态", "title": "首页默认态", "note": "第一屏"},
            {"op": "tap", "label": "设置", "wait": 2.2},
            {"op": "shot", "id": "02-设置-主页", "title": "设置主页",
             "note": "断言标题在，且不该出现的词不出现",
             "expect": ["设置"], "expectAbsent": []},
        ],
    }
