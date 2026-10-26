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


def write(out_dir: Path, data: dict) -> Path:
    out_dir = Path(out_dir)
    (out_dir / "review.json").write_text(json.dumps(data), encoding="utf-8")
    html = fill((TPL / "review.html").read_text(encoding="utf-8"), {"/*DATA*/null": _embed(data)})
    page = out_dir / "index.html"
    page.write_text(html, encoding="utf-8")
    return page


def render_library(rows: list, extra=None) -> str:
    """Library page with rows and extra (goals/season) embedded. Both markers are filled in one pass
    over the template, so marker-like text inside the data is never substituted again."""
    tpl = (TPL / "library.html").read_text(encoding="utf-8")
    return fill(tpl, {"/*ROWS*/[]": _embed(rows), "/*EXTRA*/null": _embed(extra)})


def fill(tpl: str, values: dict) -> str:
    """Replace each marker in the template exactly once, in a single pass (data never re-scanned)."""
    import re
    return re.sub("|".join(re.escape(k) for k in values), lambda m: values.pop(m.group(0), m.group(0)), tpl)
