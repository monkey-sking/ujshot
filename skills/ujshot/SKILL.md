---
name: ujshot
description: >-
  Automates iOS Simulator UI walkthroughs and visual review boards: taps through
  screens, captures screenshots, records the accessibility tree and Vision OCR
  text, asserts on-screen copy, and feeds multi-round dark/light screenshots into
  the built-in review board (ujshot-board). Use when asked for iOS simulator
  screenshots, UI walkthroughs ("走查" / "截图" / "点一遍看图"), before/after visual
  evidence, OCR copy assertions, or human+AI closed-loop UI acceptance.
---

# ujshot — iOS Simulator UI Walkthrough & Visual Acceptance

`ujshot` is a zero-script iOS Simulator walkthrough and visual review tool. Flows are declarative JSON files; every run produces an HTML gallery with machine verdicts (`steps.json`) and can optionally feed screenshots directly into the built-in multi-round review board (`ujshot-board`).

## 1. Installation & Health Check

Requires macOS + Xcode Simulator + [AXe](https://github.com/cameroncooke/AXe).

```bash
brew install cameroncooke/axe/axe
# Install globally with Pillow for pixel-diff & unchanged-screen detection:
uv tool install -e /path/to/ujshot --with pillow
# or: pipx install "/path/to/ujshot[diff]"

ujshot doctor        # Verify xcrun, swiftc, axe, Vision OCR, Pillow, and ujshot.toml
ujshot devices       # List booted simulators
```

## 2. Project Configuration (`ujshot.toml`)

Place `ujshot.toml` at the repository root (or run `ujshot init`). `ujshot` searches upward from the current working directory, so commands work from any subdirectory.

```toml
[app]
bundle_id = "com.example.app"
# Optional: deterministic seed file path inside the app data container
state_path = ""

[device]
name = "iPhone 18 Pro Max"
appearance = "dark"

[paths]
out = "artifacts/ui-shots"
flow_dir = "flows"

[board]
port = 8766
# Enforce unified screenshot naming language: "zh" (01-首页-默认态) or "en" (01-home-default)
shot_id_style = "zh"
```

## 3. Authoring Flows (`<flow_dir>/*.json`)

Add or update scenarios by editing JSON files—never write ad-hoc Python/shell `axe` scripts.

```json
{
  "name": "smoke",
  "note": "Core tabs walkthrough",
  "startTab": "tab.home",
  "steps": [
    { "op": "shot", "id": "01-首页-默认态", "title": "首页", "expect": ["首页"] },
    { "op": "tap", "label": "设置", "wait": 2.0 },
    { "op": "shot", "id": "02-设置-主页", "title": "设置", "expect": ["设置"], "expectAbsent": ["TODO", "CNY"] }
  ]
}
```

### Flow Authoring Rules (Battle-Tested Best Practices)
- **Unified `shot.id` Naming (`NN-页面-状态`)**: `shot.id` becomes the PNG filename (`<id>.png`) and binds before/after comparisons and annotations on the review board. Use 2-digit numbers + optional letter suffix (`01-首页-默认态`, `07b-弹窗-详情菜单`), without `.png`. Do not mix Chinese and English IDs in the same board.
- **Regex Assertions (`re:`)**: In `expect`, `expectAbsent`, `op: "expect"`, `op: "ensure"`, and `ujshot check`, prefix any rule with `re:` to match a Python regular expression (e.g., `"expectAbsent": ["re:[\\u4e00-\\u9fff]"]` to catch leaked CJK characters in English screenshots).
- **Target Resolution Order for `tap`**:
  `AXUniqueId exact` → `AXUniqueId prefix` → `AXLabel exact` → `AXLabel contains` → **Vision OCR coordinates fallback**.
  Prefer `id` or `label`; avoid hardcoded `(x, y)` coordinates (which break across device sizes).
- **Non-ASCII / CJK Text Input**: Use `{"op": "clipboard", "text": "中文内容", "paste": true}` (Simulators lack a CJK software keyboard for `type`).
- **Passing Through Known Defects**: Use `{"op": "ensure", "text": "期望文案", "then": {"label": "重试入口"}}` to recover and continue the flow instead of hardcoding workarounds.
- **Data Safety**: Taps matching destructive words (`删除`, `清空`, `重置`, `退出登录`, etc.) are automatically skipped (`verdict: "skipped"`) unless `dangerous: false` is explicitly set. Use `optional: true` for cleanup taps that may or may not be present.

## 4. Machine Verdicts (`steps.json`)

Every `shot` produces a machine verdict. **Any non-`ok` verdict exits with code `1`**, making `ujshot run` a drop-in CI gate:

| Verdict | Meaning | Fails Run? |
|---|---|---|
| `ok` | Screenshot captured, all `expect` found, no `expectAbsent` leaked, visible change from previous shot | No |
| `no-visible-change` | Pixel diff vs. previous step `< 0.5%` (tap likely had no effect) | **Yes** |
| `expect-missing` | Required string in `expect` was not recognized by Vision OCR | **Yes** |
| `unexpected-text` | Forbidden string in `expectAbsent` appeared on screen | **Yes** |
| `ocr-unavailable` | Text assertions were requested but Vision OCR helper could not run (never silently passes) | **Yes** |
| `blank-or-not-app` | Zero OCR text + near-solid color frame (black/white splash or home screen) | **Yes** |
| `skipped` / `optional-missing` | Dangerous tap skipped or optional element absent | No |

### Offline Assertion on Existing Images (No Simulator Needed)
```bash
ujshot check --image path/to/shot.png --expect 设置 --must-not CNY,USD
```

## 5. Feeding the Built-In Review Board

Always use **one unified board directory per project** (e.g. `artifacts/review-board`) served on a single port (`8766`). Each new build automatically lands in `after/<build>/` (read from the installed app's `Info.plist` `CFBundleVersion`).

```bash
# Run dark & light walkthroughs and feed into the review board
ujshot run --flow smoke --appearance dark  --board-dir artifacts/review-board --board-as-after --serve
ujshot run --flow smoke --appearance light --board-dir artifacts/review-board --board-as-after --serve
```

- **Strict Dark / Light Slot Isolation**: Dark screenshots go to root & `after/<build>/`; light screenshots go to `light/` & `after/light/<build>/`. Never mix dark and light screenshots in the same appearance slot.
- **Smart Deduplication & Auto-Update Detection**:
  - When feeding `after/<build>/`, screens whose pixel diff vs. the previous round (excluding the top 5.5% iOS status bar clock/battery) is `< 0.25%` and have no active human annotations are **automatically skipped** so unchanged screens do not clutter the round history (pass `--board-force-all` to override).
  - If a screen was previously confirmed **「✓ 界面没问题」** (`status: "ok"`) or all its prior annotations were `accepted`, and another change caused its pixels to change (`>= 0.25%`), `ujshot` saves the new screenshot and automatically marks the screen as **「↻ 界面有更新」** (`status: "updated"`) so the reviewer is alerted to re-verify it.

## 6. Evidence Discipline

- Never claim a UI change is "done" based on compilation alone; attach fresh `ujshot` screenshots and `verdict: "ok"` receipts.
- Store evidence under repository-relative artifact paths (e.g. `artifacts/`), not `/tmp`.
