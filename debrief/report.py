"""Render review pages and the library home page from templates."""
from __future__ import annotations

import json
from pathlib import Path

TPL = Path(__file__).resolve().parent / "templates"


def _embed(obj) -> str:
    """JSON for a <script> block. <, > and & are written as \\u003c, \\u003e, \\u0026 (same value once
    parsed) so text such as "</script>" or "<!--<script>" in notes or names can't end or upset the
    script element."""
    return (json.dumps(obj, separators=(",", ":"))
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


LAYOUT = "2.2.1"   # bump when review.html changes so saved reviews are re-rendered (no requests needed)


def write(out_dir: Path, data: dict) -> Path:
    out_dir = Path(out_dir)
    try:   # the coaching summary at the top of the page; never worth losing the review over
        from . import insights
        data["insights"] = insights.build(data)
    except Exception:  # noqa: BLE001
        data.pop("insights", None)
    (out_dir / "review.json").write_text(json.dumps(data), encoding="utf-8")
    html = fill((TPL / "review.html").read_text(encoding="utf-8"), {"/*DATA*/null": _embed(data)})
    page = out_dir / "index.html"
    page.write_text(html, encoding="utf-8")
    return page


def refresh_all(library_folder, log=None) -> int:
    """Re-render every saved review with the current page layout, from its review.json (no requests).
    Runs once per layout version (marker file .layout in the library). Returns how many were redone."""
    lib = Path(library_folder)
    marker = lib / ".layout"
    try:
        if marker.read_text(encoding="utf-8").strip() == LAYOUT:
            return 0
    except OSError:
        pass
    n = 0
    for p in sorted((lib / "reviews").glob("*/review.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("laps"):
                write(p.parent, data)
                n += 1
        except Exception as e:  # noqa: BLE001  (one odd review mustn't stop the rest)
            if log:
                log(f"  Couldn't refresh {p.parent.name}: {e}")
    try:
        lib.mkdir(parents=True, exist_ok=True)
        marker.write_text(LAYOUT, encoding="utf-8")
    except OSError:
        pass
    return n


def render_library(rows: list, extra=None) -> str:
    """Library page with rows and extra (goals/season) embedded. Both markers are filled in one pass
    over the template, so marker-like text inside the data is never substituted again."""
    tpl = (TPL / "library.html").read_text(encoding="utf-8")
    return fill(tpl, {"/*ROWS*/[]": _embed(rows), "/*EXTRA*/null": _embed(extra)})


def fill(tpl: str, values: dict) -> str:
    """Replace each marker in the template exactly once, in a single pass (data never re-scanned)."""
    import re
    return re.sub("|".join(re.escape(k) for k in values), lambda m: values.pop(m.group(0), m.group(0)), tpl)
