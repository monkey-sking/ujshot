"""HTML gallery + steps.json writer.

The gallery is intentionally a single self-contained file with relative image
links, so it can be opened directly, zipped, or attached to a review thread.
No server and no JS framework: a screenshot folder that needs a server to read
is not evidence anyone will look at.
"""

from __future__ import annotations

import html as htmllib
import json
import os

from .types import RunResult

CSS = """
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:#0b0d10;color:#e7eaee;
     font:15px/1.55 -apple-system,"PingFang SC",system-ui,sans-serif}
header{padding:24px 24px 16px;border-bottom:1px solid #1d232b}
h1{margin:0 0 6px;font-size:20px}
.sub{color:#8b95a3;font-size:13px;word-break:break-all}
.badge{display:inline-block;margin-top:8px;padding:2px 10px;border-radius:999px;
       font-size:12px;font-weight:600}
.badge.pass{background:rgba(34,197,94,.16);color:#86efac}
.badge.fail{background:rgba(239,68,68,.16);color:#fca5a5}
main{padding:20px 24px 70px;display:grid;
     grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:16px}
figure{margin:0;background:#12161c;border:1px solid #1d232b;border-radius:14px;overflow:hidden}
figure.bad{border-color:#7a3b3b}
figure img{display:block;width:100%;height:auto;background:#000}
figcaption{padding:10px 12px;font-size:12px;color:#8b95a3;display:flex;
           flex-direction:column;gap:3px}
figcaption b{color:#e7eaee;font-size:13px}
.meta{font-variant-numeric:tabular-nums;opacity:.75}
.note{color:#9fb6d1}
.warn{color:#ff9c7a}
.parent{color:#6f7a88}
.fails{grid-column:1/-1;background:#2a1517;border:1px solid #7a3b3b;
       border-radius:12px;padding:14px 18px}
.fails h2{margin:0 0 8px;font-size:15px;color:#ffb3a1}
.fails ul{margin:0;padding-left:18px}
ul.notes{grid-column:1/-1;background:#12161c;border:1px solid #1d232b;
         border-radius:12px;padding:14px 18px;color:#9fb6d1;font-size:13px}
a{color:inherit;text-decoration:none}
"""


def write(result: RunResult, flow_note: str = "", extra_meta=None) -> str:
    outdir = result.outdir
    steps_path = os.path.join(outdir, "steps.json")
    with open(steps_path, "w", encoding="utf-8") as f:
        json.dump({
            "flow": result.flow,
            "meta": {
                "device": result.device.name,
                "udid": result.device.udid,
                "verdict": "PASS" if result.ok else "FAIL",
                "failures": result.failures,
                **(extra_meta or {}),
            },
            "steps": [s.as_dict() for s in result.steps],
        }, f, ensure_ascii=False, indent=1)

    cards = []
    for s in result.shots:
        bad = s.verdict != "ok"
        bits = [f'<span class="meta">{htmllib.escape(s.id)} · diff {s.diff_pct}% · '
                f'AX {s.nodes} 节点</span>']
        if s.note:
            bits.append(f'<span class="note">{htmllib.escape(s.note)}</span>')
        if bad:
            bits.append(f'<span class="warn">⚠ {htmllib.escape(str(s.verdict))}</span>')
        if s.parent:
            bits.append(f'<span class="parent">↖ 来自 {htmllib.escape(s.parent)}</span>')
        cards.append(
            f'<figure class="{"bad" if bad else ""}">'
            f'<a href="screens/{s.id}.png" target="_blank">'
            f'<img src="screens/{s.id}.png" loading="lazy" alt="{htmllib.escape(s.title)}"></a>'
            f'<figcaption><b>{htmllib.escape(s.title)}</b>{"".join(bits)}</figcaption></figure>')

    blocks = []
    if result.anomalies:
        items = "".join(
            f"<li>{htmllib.escape(s.title)} — {htmllib.escape(str(s.verdict))}"
            + (f"（OCR：{htmllib.escape(s.text[:80])}）" if s.verdict in
               ("expect-missing", "unexpected-text") else "")
            + "</li>"
            for s in result.anomalies)
        blocks.append(f'<section class="fails"><h2>未通过 {len(result.anomalies)} 屏</h2>'
                      f'<ul>{items}</ul></section>')
    if result.failures and not result.anomalies:
        failed = [s for s in result.steps if not s.ok]
        items = "".join(f"<li>{htmllib.escape(s.title or s.id)} — "
                        f"{htmllib.escape(s.verdict or s.error or '失败')}</li>" for s in failed)
        blocks.append(f'<section class="fails"><h2>未通过 {len(failed)} 步</h2>'
                      f'<ul>{items}</ul></section>')
    if flow_note:
        blocks.append(f'<ul class="notes">{htmllib.escape(flow_note)}</ul>')

    verdict_badge = ('<span class="badge pass">PASS</span>' if result.ok
                     else f'<span class="badge fail">FAIL · {result.failures}</span>')
    sub = (f'{htmllib.escape(result.device.name)} · UDID {result.device.udid[:8]}'
           f' · {len(result.shots)} 屏')
    if extra_meta:
        for key in ("when", "seed", "app", "version", "bundle"):
            if extra_meta.get(key):
                sub += f" · {htmllib.escape(str(extra_meta[key]))}"

    page = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{htmllib.escape(result.flow)} · {len(result.shots)} 屏</title>
<style>{CSS}</style></head><body>
<header>
  <h1>{htmllib.escape(result.flow)}</h1>
  <div class="sub">{sub}</div>
  {verdict_badge}
</header>
<main>
{''.join(blocks)}
{''.join(cards)}
</main></body></html>"""
    path = os.path.join(outdir, "index.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)
    return path
