"""Zero-invasion AX tree & Vision OCR element inspector + red-box renderer.

Turns a normalised review-board pin or box annotation into concrete code-search
clues (AX element type/label/value/#identifier, enclosing container, same-row
nearby labels, OCR fallback) and optionally draws a red highlight on a copy of
the screenshot for dispatched AI agents.
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from . import device as dev


def _round_sort_key(name: str):
    return (0, int(name), "") if re.fullmatch(r"\d+", name) else (1, 0, name)


def meta_dir_for(
    board_dir: Union[Path, str],
    appearance: str = "dark",
    as_after: bool = False,
    round_build: str = "",
) -> Path:
    """Return the hidden `.meta` directory matching an appearance/round slot."""
    base = Path(board_dir) / ".meta"
    is_light = appearance == "light"
    if not as_after:
        return base / ("light" if is_light else "before")
    sub = base / "after" / "light" if is_light else base / "after"
    rb = str(round_build or "").strip()
    return (sub / rb) if rb else sub


def resolve_meta_files(
    board_dir: Union[Path, str],
    image: str,
    appearance: str = "dark",
    build: str = "",
) -> Dict[str, Optional[Path]]:
    """Locate `.ax.json` and `.ocr.json` sidecars for an image/appearance/build."""
    stem = Path(str(image or "")).stem
    if not stem:
        return {"ax": None, "ocr": None}

    mode = "light" if appearance == "light" else "dark"
    rb = str(build or "").strip()

    dirs: List[Path] = []
    if rb:
        dirs.append(meta_dir_for(board_dir, appearance=mode, as_after=True, round_build=rb))
    dirs.append(meta_dir_for(board_dir, appearance=mode, as_after=False))
    dirs.append(meta_dir_for(board_dir, appearance=mode, as_after=True, round_build=""))

    after_base = meta_dir_for(board_dir, appearance=mode, as_after=True, round_build="")
    if after_base.is_dir():
        subdirs = [
            d.name
            for d in after_base.iterdir()
            if d.is_dir()
            and not d.name.startswith(".")
            and (mode == "light" or d.name not in ("light", "dark"))
        ]
        for sub in reversed(sorted(subdirs, key=_round_sort_key)):
            cand_dir = after_base / sub
            if cand_dir not in dirs:
                dirs.append(cand_dir)

    ax_path: Optional[Path] = None
    ocr_path: Optional[Path] = None
    for d in dirs:
        if ax_path is None:
            cand_ax = d / f"{stem}.ax.json"
            if cand_ax.is_file():
                ax_path = cand_ax
        if ocr_path is None:
            cand_ocr = d / f"{stem}.ocr.json"
            if cand_ocr.is_file():
                ocr_path = cand_ocr
        if ax_path is not None and ocr_path is not None:
            break

    return {"ax": ax_path, "ocr": ocr_path}


def _clean_str(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, bool):
        return "true" if val else "false"
    return str(val).strip()


def _node_fields(node: Dict[str, Any]) -> Tuple[str, str, str, str]:
    ntype = _clean_str(node.get("type") or node.get("role")) or "Element"
    label = _clean_str(node.get("AXLabel") or node.get("label") or node.get("AXTitle"))
    value = _clean_str(node.get("AXValue") or node.get("value"))
    ident = _clean_str(node.get("AXUniqueId") or node.get("identifier"))
    return ntype, label, value, ident


def ax_root_size(tree: Any) -> Optional[Tuple[float, float]]:
    """Infer logical display width/height (in points) from an AX tree."""
    roots = tree if isinstance(tree, list) else ([tree] if isinstance(tree, dict) else [])
    best_app: Optional[Tuple[float, float]] = None
    best_app_area = -1.0
    best_any: Optional[Tuple[float, float]] = None
    best_any_area = -1.0

    for root in roots:
        if not isinstance(root, dict):
            continue
        fr = dev.frame_of(root)
        if fr and fr[2] > 0 and fr[3] > 0:
            area = fr[2] * fr[3]
            ntype = _clean_str(root.get("type") or root.get("role"))
            if ntype == "Application" and area > best_app_area:
                best_app = (fr[2], fr[3])
                best_app_area = area
            elif area > best_any_area:
                best_any = (fr[2], fr[3])
                best_any_area = area

    if best_app is not None:
        return best_app
    if best_any is not None:
        return best_any

    for node in dev.flatten(roots):
        fr = dev.frame_of(node)
        if fr and fr[2] > 0 and fr[3] > 0:
            area = fr[2] * fr[3]
            if area > best_any_area:
                best_any = (fr[2], fr[3])
                best_any_area = area
    return best_any


@dataclass
class _AxEntry:
    idx: int
    depth: int
    parent_idx: Optional[int]
    ancestors: Tuple[int, ...]
    frame: Tuple[float, float, float, float]
    type: str
    label: str
    value: str
    identifier: str

    @property
    def is_named(self) -> bool:
        return bool(self.label or self.value or self.identifier)

    @property
    def is_pressable(self) -> bool:
        return self.is_named and self.type != "Application"

    @property
    def identity_key(self) -> Tuple[str, str, str, str]:
        return (self.type, self.label, self.value, self.identifier)

    @property
    def text_key(self) -> Tuple[str, str, str]:
        return (self.label, self.value, self.identifier)


def _walk_ax(tree: Any) -> List[_AxEntry]:
    roots = tree if isinstance(tree, list) else ([tree] if isinstance(tree, dict) else [])
    entries: List[_AxEntry] = []

    def visit(node: Any, depth: int, parent_idx: Optional[int], ancestors: Tuple[int, ...]) -> None:
        if not isinstance(node, dict):
            return
        fr = dev.frame_of(node)
        next_parent = parent_idx
        next_ancestors = ancestors
        if fr and fr[2] > 0 and fr[3] > 0:
            ntype, label, value, ident = _node_fields(node)
            idx = len(entries)
            entries.append(
                _AxEntry(
                    idx=idx,
                    depth=depth,
                    parent_idx=parent_idx,
                    ancestors=ancestors,
                    frame=fr,
                    type=ntype,
                    label=label,
                    value=value,
                    identifier=ident,
                )
            )
            next_parent = idx
            next_ancestors = ancestors + (idx,)
        for child in node.get("children") or []:
            visit(child, depth + 1, next_parent, next_ancestors)

    for root in roots:
        visit(root, 0, None, ())
    return entries


def _summarize_ax(entry: _AxEntry, sw: float, sh: float) -> Dict[str, Any]:
    fx, fy, fw, fh = entry.frame
    return {
        "type": entry.type,
        "label": entry.label or None,
        "value": entry.value or None,
        "identifier": entry.identifier or None,
        "frame": {
            "x": round(fx, 2),
            "y": round(fy, 2),
            "w": round(fw, 2),
            "h": round(fh, 2),
        },
        "normFrame": {
            "x": round(fx / sw, 4),
            "y": round(fy / sh, 4),
            "w": round(fw / sw, 4),
            "h": round(fh / sh, 4),
        },
    }


def element_at_ax(
    tree: Any,
    x_norm: float,
    y_norm: float,
    w_norm: float = 0.0,
    h_norm: float = 0.0,
    screen_size: Optional[Tuple[float, float]] = None,
) -> Optional[Dict[str, Any]]:
    """Find the pressed AX element, enclosing container (`within`), and same-row `nearby` labels."""
    size = screen_size or ax_root_size(tree)
    if not size or size[0] <= 0 or size[1] <= 0:
        return None
    sw, sh = float(size[0]), float(size[1])
    entries = _walk_ax(tree)
    pressable = [e for e in entries if e.is_pressable]
    if not pressable:
        return None

    is_box = w_norm > 0 and h_norm > 0
    box_matches: List[_AxEntry] = []

    if is_box:
        bx1 = x_norm * sw
        by1 = y_norm * sh
        bx2 = (x_norm + w_norm) * sw
        by2 = (y_norm + h_norm) * sh
        box_area = max(1e-6, (bx2 - bx1) * (by2 - by1))
        scored: List[Tuple[Tuple[int, float, int, float], _AxEntry]] = []
        for e in pressable:
            fx, fy, fw, fh = e.frame
            ix1 = max(fx, bx1)
            iy1 = max(fy, by1)
            ix2 = min(fx + fw, bx2)
            iy2 = min(fy + fh, by2)
            iw = max(0.0, ix2 - ix1)
            ih = max(0.0, iy2 - iy1)
            inter = iw * ih
            if inter <= 0:
                continue
            node_area = max(1e-6, fw * fh)
            cx = fx + fw / 2.0
            cy = fy + fh / 2.0
            center_in_box = 1 if (bx1 <= cx <= bx2 and by1 <= cy <= by2) else 0
            overlap_ratio = inter / max(1.0, min(node_area, box_area))
            scored.append(((center_in_box, round(overlap_ratio, 6), e.depth, -node_area), e))
        if not scored:
            return None
        scored.sort(key=lambda item: item[0], reverse=True)
        best = scored[0][1]
        seen_keys = set()
        for _, e in scored:
            if e.identity_key in seen_keys:
                continue
            seen_keys.add(e.identity_key)
            box_matches.append(e)
            if len(box_matches) >= 6:
                break
    else:
        px = x_norm * sw
        py = y_norm * sh
        hits = [
            e
            for e in pressable
            if e.frame[0] <= px <= e.frame[0] + e.frame[2]
            and e.frame[1] <= py <= e.frame[1] + e.frame[3]
        ]
        if not hits:
            return None
        best = max(hits, key=lambda e: (e.depth, -(e.frame[2] * e.frame[3])))

    by_idx = {e.idx: e for e in entries}
    within_node: Optional[_AxEntry] = None
    for anc_idx in reversed(best.ancestors):
        anc = by_idx.get(anc_idx)
        if anc and anc.is_pressable and anc.identity_key != best.identity_key:
            within_node = anc
            break

    el_fx, el_fy, el_fw, el_fh = best.frame
    el_cx = el_fx + el_fw / 2.0
    el_cy = el_fy + el_fh / 2.0
    excluded_anc = set(best.ancestors)
    excluded_text = {best.text_key}
    if within_node is not None:
        excluded_text.add(within_node.text_key)

    same_parent: List[Tuple[float, _AxEntry]] = []
    other_row: List[Tuple[float, _AxEntry]] = []
    for e in pressable:
        if e.idx == best.idx:
            continue
        if e.idx in excluded_anc or best.idx in e.ancestors:
            continue
        if e.text_key in excluded_text:
            continue
        fx, fy, fw, fh = e.frame
        cx = fx + fw / 2.0
        cy = fy + fh / 2.0
        v_overlap = min(el_fy + el_fh, fy + fh) - max(el_fy, fy)
        if v_overlap <= 0 and abs(cy - el_cy) > max(el_fh, fh, 24.0):
            continue
        dist = math.hypot(cx - el_cx, cy - el_cy)
        if e.parent_idx == best.parent_idx:
            same_parent.append((dist, e))
        else:
            other_row.append((dist, e))

    same_parent.sort(key=lambda item: item[0])
    other_row.sort(key=lambda item: item[0])
    nearby_nodes: List[_AxEntry] = []
    seen_near = set(excluded_text)
    for _, e in same_parent + other_row:
        if e.text_key in seen_near:
            continue
        seen_near.add(e.text_key)
        nearby_nodes.append(e)
        if len(nearby_nodes) >= 6:
            break

    return {
        "element": _summarize_ax(best, sw, sh),
        "within": _summarize_ax(within_node, sw, sh) if within_node else None,
        "nearby": [_summarize_ax(n, sw, sh) for n in nearby_nodes],
        "matches": [_summarize_ax(n, sw, sh) for n in box_matches],
    }


def element_at_ocr(
    ocr_data: Dict[str, Any],
    x_norm: float,
    y_norm: float,
    w_norm: float = 0.0,
    h_norm: float = 0.0,
) -> Optional[Dict[str, Any]]:
    """Find OCR lines at a normalised pin or box, plus same-row nearby text."""
    raw_lines = ocr_data.get("lines") if isinstance(ocr_data, dict) else None
    if not isinstance(raw_lines, list):
        return None

    parsed: List[Dict[str, Any]] = []
    for item in raw_lines:
        if not isinstance(item, dict):
            continue
        t = str(item.get("t") or "").strip()
        if not t:
            continue
        lx = float(item.get("x", 0.0))
        ly = float(item.get("y", 0.0))
        cx = float(item.get("cx", lx))
        cy = float(item.get("cy", ly))
        lw = float(item["w"]) if item.get("w") is not None else max(0.0, (cx - lx) * 2.0)
        lh = float(item["h"]) if item.get("h") is not None else max(0.0, (cy - ly) * 2.0)
        parsed.append({
            "text": t,
            "x": round(lx, 4),
            "y": round(ly, 4),
            "w": round(lw, 4),
            "h": round(lh, 4),
            "cx": round(cx, 4),
            "cy": round(cy, 4),
        })
    if not parsed:
        return None

    is_box = w_norm > 0 and h_norm > 0
    hits: List[Dict[str, Any]] = []
    if is_box:
        bx1, by1, bx2, by2 = x_norm, y_norm, x_norm + w_norm, y_norm + h_norm
        for line in parsed:
            lx, ly, lw, lh = line["x"], line["y"], line["w"], line["h"]
            if max(lx, bx1) < min(lx + max(lw, 1e-4), bx2) and max(ly, by1) < min(ly + max(lh, 1e-4), by2):
                hits.append(line)
        hits.sort(key=lambda l: (round(l["y"], 3), l["x"]))
        hits = hits[:8]
    else:
        pad = 0.015
        in_box = []
        for line in parsed:
            lx, ly, lw, lh = line["x"], line["y"], line["w"], line["h"]
            if (lx - pad) <= x_norm <= (lx + lw + pad) and (ly - pad) <= y_norm <= (ly + lh + pad):
                dist = math.hypot(line["cx"] - x_norm, line["cy"] - y_norm)
                in_box.append((dist, line))
        if in_box:
            in_box.sort(key=lambda item: item[0])
            hits = [item[1] for item in in_box[:4]]
        else:
            nearest = min(parsed, key=lambda l: math.hypot(l["cx"] - x_norm, l["cy"] - y_norm))
            if math.hypot(nearest["cx"] - x_norm, nearest["cy"] - y_norm) <= 0.06:
                hits = [nearest]

    hit_texts = {h["text"] for h in hits}
    target_cx = hits[0]["cx"] if hits else (x_norm + w_norm / 2.0)
    target_cy = hits[0]["cy"] if hits else (y_norm + h_norm / 2.0)

    near_cands: List[Tuple[float, Dict[str, Any]]] = []
    for line in parsed:
        if line["text"] in hit_texts:
            continue
        if abs(line["cy"] - target_cy) <= 0.035:
            near_cands.append((abs(line["cx"] - target_cx), line))
    near_cands.sort(key=lambda item: item[0])

    nearby: List[Dict[str, Any]] = []
    seen_near = set(hit_texts)
    for _, line in near_cands:
        if line["text"] in seen_near:
            continue
        seen_near.add(line["text"])
        nearby.append(line)
        if len(nearby) >= 6:
            break

    if not hits and not nearby:
        return None
    return {"hits": hits, "nearby": nearby}


def _fmt_ax_element(el: Dict[str, Any]) -> str:
    parts = [el.get("type") or "Element"]
    if el.get("label"):
        parts.append(json.dumps(el["label"], ensure_ascii=False))
    if el.get("value") and el.get("value") != el.get("label"):
        parts.append(f'= {json.dumps(el["value"], ensure_ascii=False)}')
    if el.get("identifier"):
        parts.append(f'#{el["identifier"]}')
    return " ".join(parts)


def format_inspection_line(ax_res: Optional[Dict[str, Any]], ocr_res: Optional[Dict[str, Any]]) -> str:
    """Format AX + OCR inspection findings into a concise single-line clue."""
    segments: List[str] = []
    printed: set = set()

    if ax_res and ax_res.get("element"):
        el = ax_res["element"]
        el_str = _fmt_ax_element(el)
        for k in ("label", "value"):
            if el.get(k):
                printed.add(el[k])

        extra_labels: List[str] = []
        for m in (ax_res.get("matches") or [])[1:]:
            cand = m.get("label") or m.get("value") or (f'#{m["identifier"]}' if m.get("identifier") else "")
            if cand and cand not in printed and cand not in extra_labels:
                extra_labels.append(cand)
                printed.add(cand)
            if len(extra_labels) >= 2:
                break
        if extra_labels:
            el_str += " (+ " + ", ".join(json.dumps(x, ensure_ascii=False) for x in extra_labels) + ")"
        segments.append(el_str)

        within = ax_res.get("within")
        if within:
            segments.append(f"within {_fmt_ax_element(within)}")
            for k in ("label", "value"):
                if within.get(k):
                    printed.add(within[k])
    elif ocr_res and ocr_res.get("hits"):
        texts: List[str] = []
        for h in ocr_res["hits"][:3]:
            t = h["text"]
            texts.append(json.dumps(t, ensure_ascii=False))
            printed.add(t)
        if texts:
            segments.append("OCR " + ", ".join(texts))

    near_labels: List[str] = []
    for n in (ax_res or {}).get("nearby") or []:
        cand = n.get("label") or n.get("value") or (f'#{n["identifier"]}' if n.get("identifier") else "")
        if cand and cand not in printed and cand not in near_labels:
            near_labels.append(cand)
    if len(near_labels) < 4:
        for n in (ocr_res or {}).get("nearby") or []:
            cand = n.get("text") or ""
            if cand and cand not in printed and cand not in near_labels:
                near_labels.append(cand)
    if near_labels:
        segments.append("near " + ", ".join(json.dumps(x, ensure_ascii=False) for x in near_labels[:4]))

    return " · ".join(segments)


def _load_json(path: Optional[Path]) -> Any:
    if path is None or not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def inspect_annotation(
    board_dir: Union[Path, str],
    annotation: Dict[str, Any],
    appearance: str = "",
    build: str = "",
) -> Dict[str, Any]:
    """Inspect an annotation against stored `.meta` AX/OCR sidecars."""
    mode = "light" if (appearance or annotation.get("appearance")) == "light" else "dark"
    rb = str(build or annotation.get("raisedIn") or "")
    image = str(annotation.get("image") or "")

    files = resolve_meta_files(board_dir, image, appearance=mode, build=rb)
    ax_data = _load_json(files["ax"])
    ocr_data = _load_json(files["ocr"])

    x = float(annotation.get("x") or 0.0)
    y = float(annotation.get("y") or 0.0)
    kind = str(annotation.get("kind") or "pin")
    w = 0.0 if kind == "pin" else float(annotation.get("w") or 0.0)
    h = 0.0 if kind == "pin" else float(annotation.get("h") or 0.0)

    ax_res = element_at_ax(ax_data, x, y, w, h) if ax_data is not None else None
    ocr_res = element_at_ocr(ocr_data, x, y, w, h) if isinstance(ocr_data, dict) else None

    summary = format_inspection_line(ax_res, ocr_res) or str(annotation.get("elementHint") or "").strip()
    norm_frame = (ax_res or {}).get("element", {}).get("normFrame")
    return {
        "summary": summary,
        "ax": ax_res,
        "ocr": ocr_res,
        "normFrame": norm_frame,
        "sources": {
            "ax": str(files["ax"]) if files["ax"] else "",
            "ocr": str(files["ocr"]) if files["ocr"] else "",
        },
    }


def render_annotated_image(
    src_png: Union[Path, str],
    dst_png: Union[Path, str],
    annotation: Dict[str, Any],
    element_norm_frame: Optional[Dict[str, float]] = None,
) -> Optional[Path]:
    """Render a copy of `src_png` with a clear red marker for the annotation."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None

    src_path = Path(src_png)
    dst_path = Path(dst_png)
    if not src_path.is_file():
        return None

    try:
        base = Image.open(src_path).convert("RGBA")
        w, h = base.size
        if w <= 0 or h <= 0:
            return None
        overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        lw = max(3, int(round(w / 180.0)))

        kind = str(annotation.get("kind") or "pin")
        x_norm = float(annotation.get("x") or 0.0)
        y_norm = float(annotation.get("y") or 0.0)
        w_norm = float(annotation.get("w") or 0.0)
        h_norm = float(annotation.get("h") or 0.0)

        if kind == "box" and w_norm > 0 and h_norm > 0:
            x1 = max(0, min(w - 1, int(round(x_norm * w))))
            y1 = max(0, min(h - 1, int(round(y_norm * h))))
            x2 = max(x1 + 1, min(w, int(round((x_norm + w_norm) * w))))
            y2 = max(y1 + 1, min(h, int(round((y_norm + h_norm) * h))))
            draw.rectangle((x1, y1, x2, y2), fill=(239, 68, 68, 36), outline=(239, 68, 68, 255), width=lw)
        else:
            if isinstance(element_norm_frame, dict):
                ew = float(element_norm_frame.get("w") or 0.0)
                eh = float(element_norm_frame.get("h") or 0.0)
                if 0 < ew * eh < 0.85:
                    ex1 = max(0, min(w - 1, int(round(float(element_norm_frame.get("x") or 0.0) * w))))
                    ey1 = max(0, min(h - 1, int(round(float(element_norm_frame.get("y") or 0.0) * h))))
                    ex2 = max(ex1 + 1, min(w, int(round((float(element_norm_frame.get("x") or 0.0) + ew) * w))))
                    ey2 = max(ey1 + 1, min(h, int(round((float(element_norm_frame.get("y") or 0.0) + eh) * h))))
                    draw.rectangle((ex1, ey1, ex2, ey2), outline=(245, 158, 11, 235), width=max(2, lw - 1))

            px = max(0, min(w - 1, int(round(x_norm * w))))
            py = max(0, min(h - 1, int(round(y_norm * h))))
            radius = max(14, int(round(w * 0.025)))
            draw.ellipse(
                (px - radius, py - radius, px + radius, py + radius),
                outline=(239, 68, 68, 255),
                width=lw,
            )
            dot_r = max(4, lw + 1)
            draw.ellipse((px - dot_r, py - dot_r, px + dot_r, py + dot_r), fill=(239, 68, 68, 255))

        combined = Image.alpha_composite(base, overlay).convert("RGB")
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = dst_path.with_name(f".{dst_path.name}.tmp")
        combined.save(tmp_path, format="PNG")
        os.replace(tmp_path, dst_path)
        return dst_path
    except Exception:
        return None
