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


LAYOUT = "2.2.2"   # bump when review.html changes so saved reviews are re-rendered (no requests needed)


def write(out_dir: Path, data: dict) -> Path:
    out_dir = Path(out_dir)
    try:   # the coaching summary at the top of the page; never worth losing the review over
        from . import insights
        data["insights"] = insights.build(data)
    except Exception:  # noqa: BLE001
        data.pop("insights", None)
    html = fill((TPL / "review.html").read_text(encoding="utf-8"), {"/*DATA*/null": _embed(data)})
    # each file is written to a temp file and swapped in, so a crash or a forced close mid-write leaves
    # the old file whole (review.json is the only copy of an analysed review)
    atomic_write(out_dir / "review.json", json.dumps(data))
    page = out_dir / "index.html"
    atomic_write(page, html)
    return page


def atomic_write(path: Path, text: str) -> None:
    """Write text to path via a temp file in the same folder + fsutil.replace (never a half-written file)."""
    import os
    import tempfile
    from . import fsutil
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        fsutil.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def refresh_all(library_folder, log=None, should_stop=None, done=None) -> int:
    """Re-render every saved review with the current page layout, from its review.json (no requests).
    Runs once per layout version (marker file .layout in the library). Returns how many were redone.
    should_stop() -> True pauses it between reviews (the marker isn't written, so it carries on later);
    done is a set of review folders already redone in this run, so a resumed run skips them."""
    lib = Path(library_folder)
    marker = lib / ".layout"
    try:
        if marker.read_text(encoding="utf-8").strip() == LAYOUT:
            return 0
    except OSError:
        pass
    n = 0
    done = done if done is not None else set()
    for p in sorted((lib / "reviews").glob("*/review.json")):
        if p.parent.name in done:
            continue
        if should_stop and should_stop():
            return n
        try:
            before = p.stat().st_mtime_ns
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("laps"):
                if p.stat().st_mtime_ns == before:   # a review saved meanwhile is already in the new layout
                    write(p.parent, data)
                    n += 1
            done.add(p.parent.name)
        except Exception as e:  # noqa: BLE001  (one odd review mustn't stop the rest)
            if log:
                log(f"  Couldn't refresh {p.parent.name}: {e}")
    try:
        lib.mkdir(parents=True, exist_ok=True)
        atomic_write(marker, LAYOUT)
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
