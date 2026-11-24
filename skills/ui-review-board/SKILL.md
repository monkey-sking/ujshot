---
name: ui-review-board
description: >-
  Zero-dependency visual UI screenshot annotation and multi-version acceptance board
  (ujshot-board) for human + AI closed-loop UI reviews. Use when reviewing app or
  web screenshots, letting the user drop pins or draw boxes on before/after
  screenshots, comparing multiple build rounds side-by-side or with a curtain
  slider, dispatching local coding agents, and iterating on rejected items until
  accepted.
---

# UI Review Board (`ujshot-board`) — Closed-Loop Visual Acceptance

The visual review board is built into `ujshot` (`ujshot-board` / `ujshot board --serve`). Do **not** copy board scripts into project repositories and do **not** spawn a separate port per task or conversation.

## 1. One Service, Many Boards

- **Single daemon on port 8766** (or `[board] port` in `ujshot.toml`).
- Hosted boards live under `/b/<board-name>/`; the top-left header menu switches between registered boards.
- The board registry is stored per repository at `<repo>/.ujshot/boards.json` (using repo-relative paths, gitignored).
- Running `ujshot-board <dir>` when port 8766 is already listening **registers** `<dir>` into the running service instead of starting a second server.

```bash
# Start service or register a board into the running service
ujshot-board artifacts/review-board --port 8766 --no-open

# List all registered boards
ujshot-board --list

# Read human annotations by status (includes normalized box, appearance, raisedIn, fixedIn)
# Add --inspect to include AX/OCR elementHint, enclosing container, nearby labels, and screenshotPath
ujshot board --read artifacts/review-board --status open,proposed,approved,rejected,fixed --inspect

# Queue open/rejected findings directly into an active Codex thread (with red-box marker PNGs & AX/OCR clues)
ujshot board --read artifacts/review-board --status open,rejected --queue-codex <thread-id>

# Inspect cross-version fix history per image
ujshot board --fixes artifacts/review-board
```

## 2. Directory Layout (Multi-Round + Dark/Light Slots)

```
<board-dir>/
├── 01-screen.png                        # Dark baseline ("before")
├── light/01-screen.png                  # Light baseline (isolated from dark)
├── after/
│   ├── 245/01-screen.png                # Dark Build 245 round (preserved)
│   ├── 246/01-screen.png                # Dark Build 246 round (only saved if changed/fixed)
│   └── light/
│       ├── 245/01-screen.png            # Light Build 245 round
│       └── 246/01-screen.png            # Light Build 246 round
├── annotations.json                     # Annotations + screenChecks (ok / updated)
└── versions.json                        # {"before","after","overrides","rounds"}
```

- **Strict Dark / Light Separation**: Always capture dark (`--appearance dark`) and light (`--appearance light`) into their own slots. The header provides a `🌙 暗色 / ☀️ 亮色` switch; missing rounds in an appearance show an explicit placeholder instead of falling back to the opposite theme.
- **Multi-Version Stage & Curtain Slider**: The stage bar automatically pairs the build where an issue was raised (`raisedIn`) against the build where it was fixed (`fixedIn`), supports arbitrary Left/Right build selection via the version timeline, and offers **Side-by-Side (`1`)**, **Curtain Slider (`2`)**, and **Single Image (`3`)** modes.

## 3. Screen & Annotation Lifecycle (`annotations.json`)

```json
{
  "annotations": [
    {
      "id": "U1",
      "image": "01-screen.png",
      "target": "before",
      "appearance": "dark",
      "raisedIn": "245",
      "fixedIn": "246",
      "author": "user",
      "kind": "box",
      "x": 0.12, "y": 0.34, "w": 0.56, "h": 0.18,
      "text": "问题描述",
      "status": "fixed",
      "aiReply": "Build 246：已按规范修正间距并实拍核验。",
      "rejectReason": "",
      "rounds": [
        {"build": "245", "status": "rejected", "rejectReason": "还是偏左"},
        {"build": "246", "status": "fixed", "aiReply": "Build 246：已贴右对齐。"}
      ]
    }
  ],
  "screenChecks": {
    "dark:02-screen.png": {
      "status": "ok",
      "build": "245",
      "checkedAt": "2026-11-23T01:00:00Z"
    },
    "dark:03-screen.png": {
      "status": "updated",
      "build": "246",
      "prevBuild": "245",
      "diffPct": 6.4,
      "updatedAt": "2026-11-23T02:00:00Z"
    }
  }
}
```

### Annotation Statuses
- `open`: Newly added by the user (or failed `ujshot` OCR/diff check), awaiting fix or solution proposal.
- `proposed`: AI wrote an implementation proposal in `aiReply`, awaiting user approval (**✓ 批准方案** / **✓ 批准本屏**).
- `approved`: User approved the proposal; ready for code implementation.
- `discussed`: Product/UX question requiring interactive 1-on-1 alignment with the user.
- `fixed`: AI fixed the code, captured the new screenshot into `after/<build>/<same-filename>.png`, visually verified the result, and recorded `fixedIn` + `aiReply` (and updated the matching entry in `rounds[]` when multi-round).
- `accepted`: User clicked **✓ 通过** after visual comparison.
- `rejected`: User clicked **↩ 驳回** with a `rejectReason`.

### Screen-Level Statuses (`screenChecks`)
- **待查看 (`unreviewed`)**: Screen has no human annotations and has not been marked OK yet.
- **✓ 没问题 (`ok`)**: Reviewer clicked **✓ 界面没问题**. Future runs automatically skip copying identical screenshots (`diff < 0.25%`) for this screen.
- **↻ 有更新 (`updated`)**: Screen was previously `ok` (or all its annotations were `accepted`), but a later build changed its pixels (`diff >= 0.25%`). The board automatically pairs `prevBuild → build` side-by-side and shows an amber **↻ 有更新** banner until the reviewer clicks **✓ 确认无问题**.

## 4. Agent Closed-Loop Execution Protocol

1. **Read Active Findings**: Run `ujshot board --read <board-dir> --status open,proposed,approved,rejected` (or inspect `annotations.json`).
   - Unambiguous bugs or `approved` items → implement directly.
   - Ambiguous product/design choices → either write a concise proposal in `aiReply` and set `status: "proposed"`, or align with the user one topic per turn.
2. **Re-Capture Matching Appearance**: Run `ujshot run --flow <flow> --appearance <dark|light> --board-dir <board-dir> --board-as-after` so new screenshots land in `after/<build>/` (or `after/light/<build>/`).
3. **Self-Inspect & Update `annotations.json`**:
   - Visually inspect the generated `after/<build>/<image>` PNG yourself to confirm the fix actually took effect.
   - Set `status: "fixed"`, `fixedIn: "<build>"`, and write a factual `aiReply`. If the annotation has a `rounds` array, also append/update the `{ "build": "<build>", "status": "fixed", "aiReply": "..." }` entry so the latest round matches `status`.
4. **Wait for Human Sign-Off**: Do not mark an annotation `accepted` on behalf of the user.
