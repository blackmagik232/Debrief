"""Race results you type in (finish position, incidents, iRating/SR change...), per review.

Stored in <review dir>/result.json and copied into review.json as data["result"] so the review page
can show it. Nothing here talks to Garage 61 or iRacing.

Sign conventions for the derived fields (all "after minus before" / "start minus finish"):
  irating_change   = irating_after - irating_before   (positive = iRating gained)
  sr_change        = sr_after - sr_before             (positive = safety rating gained)
  positions_gained = start_pos - finish_pos           (positive = finished ahead of where you started)
  positions_lap1   = start_pos - pos_after_lap1       (positive = places made on lap 1)

2.4 adds pos_after_lap1, pit_stops, penalties, finish_status and incident_laps (a list of
{"lap", "kind", "points"}). Every one is optional, so older result.json files stay valid.
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
    "pos_after_lap1": ("int", 1, 200, "Position after lap 1"),
    "pit_stops": ("int", 0, 50, "Pit stops"),
    "penalties": ("int", 0, 50, "Penalties (drive-throughs etc.)"),
}
# non-numeric fields (2.4)
FINISH_STATUS = ("running", "dnf", "dq", "disconnected")
FINISH_LABEL = "How the race ended"
INCIDENT_KINDS = ("off", "spin", "contact", "other")
INCIDENT_POINTS = (0, 1, 2, 4)
MAX_INCIDENT_LAPS = 30
INCIDENTS_LABEL = "Incidents by lap"


def _status(raw):
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    v = raw.strip().lower() if isinstance(raw, str) else None
    if v not in FINISH_STATUS:
        raise ValueError(f"{FINISH_LABEL} should be one of: running, DNF, DQ or disconnected.")
    return v


def _whole(raw, lo, hi, label):
    if isinstance(raw, bool) or raw is None:
        raise ValueError(f"{label} should be a whole number.")
    try:
        v = float(str(raw).strip()) if isinstance(raw, str) else float(raw)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{label} should be a whole number.")
    if not math.isfinite(v) or not v.is_integer():
        raise ValueError(f"{label} should be a whole number.")
    v = int(v)
    if not lo <= v <= hi:
        raise ValueError(f"{label} should be between {lo} and {hi}.")
    return v


def _incident_laps(raw):
    """[{"lap": 1-2000, "kind": off|spin|contact|other, "points": 0|1|2|4}], at most 30, sorted by lap.
    None for empty. Raises ValueError (plain)."""
    if raw is None or raw == "" or raw == []:
        return None
    if not isinstance(raw, list):
        raise ValueError(f"{INCIDENTS_LABEL}: something went wrong with the list. Try again.")
    if len(raw) > MAX_INCIDENT_LAPS:
        raise ValueError(f"{INCIDENTS_LABEL}: add at most {MAX_INCIDENT_LAPS} incidents.")
    out = []
    for i, e in enumerate(raw, 1):
        if not isinstance(e, dict):
            raise ValueError(f"{INCIDENTS_LABEL}: row {i} is incomplete.")
        lap = _whole(e.get("lap"), 1, 2000, f"{INCIDENTS_LABEL}: the lap in row {i}")
        kind = e.get("kind")
        kind = kind.strip().lower() if isinstance(kind, str) else kind
        if kind not in INCIDENT_KINDS:
            raise ValueError(f"{INCIDENTS_LABEL}: row {i} should be an off, a spin, contact or other.")
        pts = e.get("points", 0)
        pts = 0 if pts is None or (isinstance(pts, str) and not pts.strip()) else \
            _whole(pts, 0, 4, f"{INCIDENTS_LABEL}: the points in row {i}")
        if pts not in INCIDENT_POINTS:
            raise ValueError(f"{INCIDENTS_LABEL}: the points in row {i} should be 0, 1, 2 or 4 (0x, 1x, 2x, 4x).")
        out.append({"lap": lap, "kind": kind, "points": pts})
    out.sort(key=lambda e: e["lap"])
    return out
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
        for k in ("finish_pos", "start_pos", "pos_after_lap1"):
            if k in out and out[k] > fs:
                raise ValueError(f"{FIELDS[k][3]} can't be higher than the number of cars ({fs}).")
    st = _status(fields.get("finish_status"))
    if st is not None:
        out["finish_status"] = st
    il = _incident_laps(fields.get("incident_laps"))
    if il is not None:
        out["incident_laps"] = il
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
    if "start_pos" in out and "pos_after_lap1" in out:
        out["positions_lap1"] = out["start_pos"] - out["pos_after_lap1"]
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
