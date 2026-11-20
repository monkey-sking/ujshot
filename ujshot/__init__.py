"""ujshot — iOS simulator UI walkthrough automation.

Tap through screens, screenshot each one, capture the accessibility tree and
Vision OCR text, then build an HTML gallery. Runs as a human tool or a CI gate.

Design constraints that shaped this package:

* No project coupling. Nothing here knows about a particular app. Bundle id,
  default device, seed path and container subpath all come from ``ujshot.toml``
  (or CLI flags), so the same tool works in any repo.
* macOS only. It drives ``xcrun simctl`` and ``axe``; OCR uses Vision.
* Degrade, never crash. Missing axe / missing OCR / missing Pillow each disable
  exactly one feature and say so, instead of failing the whole run.
"""

__version__ = "0.1.0"
