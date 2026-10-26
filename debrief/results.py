"""Race results you type in (finish position, incidents, iRating/SR change...), per review.

Stored in <review dir>/result.json and copied into review.json as data["result"] so the review page
can show it. Nothing here talks to Garage 61 or iRacing.

Sign conventions for the derived fields (all "after minus before" / "start minus finish"):
  irating_change   = irating_after - irating_before   (positive = iRating gained)
  sr_change        = sr_after - sr_before             (positive = safety rating gained)
  positions_gained = start_pos - finish_pos           (positive = finished ahead of where you started)
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import fsutil

FILE = "result.json"
NOTES_MAX = 500

# field -> (kind, min, max, label)
FIELDS = {
    "start_pos": ("int", 1, 200, "Starting position"),
    "finish_pos": ("int", 1, 200, "Finishing position"),
    "field_size": ("int", 1, 200, "Cars in the race"),
    "incidents": ("int", 0, 999, "Incidents"),
    "irating_before": ("int", 0, 20000, "iRating before"),
    "irating_after": ("int", 0, 20000, "iRating after"),
    "sr_before": ("float", 0.0, 4.99, "Safety rating before"),
    "sr_after": ("float", 0.0, 4.99, "Safety rating after"),
    "sof": ("int", 0, 20000, "Strength of field"),
    "laps_led": ("int", 0, 2000, "Laps led"),
}
_DIR_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$")


def _atomic_json(path: Path, obj) -> None:
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2)
        fsutil.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _review_dir(folder, review_dirname: str) -> Path:
    name = str(review_dirname or "")
    if not _DIR_OK.match(name) or ".." in name:
        raise ValueError("That review couldn't be found.")
    d = Path(folder) / "reviews" / name
    if not (d / "review.json").is_file():
        raise ValueError("That review couldn't be found.")
    return d


def _value(key, raw):
    kind, lo, hi, label = FIELDS[key]
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if isinstance(raw, bool):
        raise ValueError(f"{label} should be a number.")
    try:
        v = float(str(raw).strip().replace(",", ".")) if isinstance(raw, str) else float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{label} should be a number.")
    if not math.isfinite(v):
        raise ValueError(f"{label} should be a number.")
    if kind == "int":
        if not v.is_integer():
            raise ValueError(f"{label} should be a whole number.")
        v = int(v)
    else:
        v = round(v, 2)
    if not (lo <= v <= hi):
        raise ValueError(f"{label} should be between {lo} and {hi}.")
    return v


def validate(fields: dict) -> dict:
    """Clean user input. finish_pos is required; everything else is optional. Unknown keys are ignored.
    Raises ValueError with a plain message."""
    if not isinstance(fields, dict):
        raise ValueError("Result details are missing.")
    out = {}
    for k in FIELDS:
        v = _value(k, fields.get(k))
        if v is not None:
            out[k] = v
    if "finish_pos" not in out:
        raise ValueError("Enter your finishing position (1 or more).")
    fs = out.get("field_size")
    if fs is not None:
        for k in ("finish_pos", "start_pos"):
            if k in out and out[k] > fs:
                raise ValueError(f"{FIELDS[k][3]} can't be higher than the number of cars ({fs}).")
    notes = fields.get("notes")
    notes = "" if notes is None else str(notes).strip()
    if len(notes) > NOTES_MAX:
        raise ValueError(f"Keep notes under {NOTES_MAX} characters.")
    if notes:
        out["notes"] = notes
    # derived, explicit signs (see module docstring)
    if "irating_before" in out and "irating_after" in out:
        out["irating_change"] = out["irating_after"] - out["irating_before"]
    if "sr_before" in out and "sr_after" in out:
        out["sr_change"] = round(out["sr_after"] - out["sr_before"], 2)
    if "start_pos" in out:
        out["positions_gained"] = out["start_pos"] - out["finish_pos"]
    return out


def load(review_dir: Path) -> dict | None:
    """<review_dir>/result.json, or None if missing or unreadable."""
    p = Path(review_dir) / FILE
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def _rerender(d: Path, mutate) -> None:
    """Load review.json, apply mutate(data), and re-render via report.write. report.write is run into a
    scratch folder and the two files are then moved into place, so a crash never leaves a half-written
    review.json behind."""
    from . import report
    data = json.loads((d / "review.json").read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("That review's data file is damaged.")
    mutate(data)
    tmp = Path(tempfile.mkdtemp(prefix=".rerender-", dir=str(d)))
    try:
        report.write(tmp, data)
        fsutil.replace(tmp / "review.json", d / "review.json")
        fsutil.replace(tmp / "index.html", d / "index.html")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def save(folder, review_dirname: str, fields: dict) -> dict:
    """Validate, write result.json, set data["result"] in review.json, re-render the review page and
    rebuild the library. Returns the saved result."""
    from . import library
    d = _review_dir(folder, review_dirname)
    res = validate(fields)
    res["saved_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _atomic_json(d / FILE, res)

    def put(data):
        data["result"] = res
    _rerender(d, put)
    library.rebuild(folder)
    return res


def delete(folder, review_dirname: str) -> bool:
    """Remove a saved result. True if there was one."""
    from . import library
    d = _review_dir(folder, review_dirname)
    had = (d / FILE).exists()
    if had:
        (d / FILE).unlink()
    data = json.loads((d / "review.json").read_text(encoding="utf-8"))
    if isinstance(data, dict) and "result" in data:
        had = True
        _rerender(d, lambda x: x.pop("result", None))
    if had:
        library.rebuild(folder)
    return had
