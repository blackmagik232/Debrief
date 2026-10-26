"""Lap-time goals per track and car, stored in <library folder>/goals.json.

A goal is {"id", "track", "car", "track_id", "car_id", "target_s", "note", "created_at", "updated_at"}.
track/car are the full names used in reviews (e.g. "Charlotte Motor Speedway (Roval 2019)");
track_id/car_id are optional Garage 61 ids. Goals never touch Garage 61.
"""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import fsutil

FILE = "goals.json"
NOTE_MAX = 500
NAME_MAX = 200
TARGET_MIN_S, TARGET_MAX_S = 10.0, 3600.0   # a lap between 10 seconds and an hour


def _path(folder) -> Path:
    return Path(folder) / FILE


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write(folder, goals: list) -> None:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    p = _path(folder)
    fd, tmp = tempfile.mkstemp(prefix=".goals.", suffix=".tmp", dir=str(folder))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "goals": goals}, f, indent=2)
        fsutil.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load(folder) -> list:
    """All goals (oldest first). A missing or damaged goals.json reads as no goals."""
    p = _path(folder)
    if not p.exists():
        return []
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = d.get("goals") if isinstance(d, dict) else d
    out = []
    for gl in items or []:
        if isinstance(gl, dict) and gl.get("id") and isinstance(gl.get("target_s"), (int, float)):
            out.append(gl)
    return out


def _opt_id(v, label):
    if v in (None, ""):
        return None
    if isinstance(v, bool):
        raise ValueError(f"{label} should be a number.")
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{label} should be a number.")
    if not f.is_integer() or f < 0:
        raise ValueError(f"{label} should be a whole number.")
    return int(f)


def parse_time(v) -> float:
    """Seconds from 83.5, "83.5", "1:23.500" or "1:23,5". Raises ValueError for anything else."""
    if isinstance(v, bool) or v is None:
        raise ValueError("Enter a target lap time, like 1:23.500.")
    if isinstance(v, (int, float)):
        f = float(v)
    else:
        s = str(v).strip().replace(",", ".")
        m = re.fullmatch(r"(?:(\d+):)?(\d+(?:\.\d+)?)", s)
        if not m:
            raise ValueError("Enter the target lap time like 1:23.500.")
        f = float(m.group(2)) + (int(m.group(1)) * 60 if m.group(1) else 0)
        if m.group(1) and float(m.group(2)) >= 60:
            raise ValueError("Seconds should be under 60 in a time like 1:23.500.")
    if not math.isfinite(f) or not (TARGET_MIN_S <= f <= TARGET_MAX_S):
        raise ValueError("That target lap time doesn't look right (it should be between 0:10 and 60:00).")
    return round(f, 3)


def validate(fields: dict) -> dict:
    if not isinstance(fields, dict):
        raise ValueError("Goal details are missing.")
    track = str(fields.get("track") or "").strip()
    car = str(fields.get("car") or "").strip()
    if not track or not car:
        raise ValueError("Pick a track and a car for the goal.")
    if len(track) > NAME_MAX or len(car) > NAME_MAX:
        raise ValueError("Track or car name is too long.")
    note = fields.get("note")
    note = "" if note is None else str(note).strip()
    if len(note) > NOTE_MAX:
        raise ValueError(f"Keep the note under {NOTE_MAX} characters.")
    return {"track": track, "car": car, "track_id": _opt_id(fields.get("track_id"), "Track id"),
            "car_id": _opt_id(fields.get("car_id"), "Car id"),
            "target_s": parse_time(fields.get("target_s")), "note": note}


def save_goal(folder, fields: dict) -> dict:
    """Add a goal, or update one when fields["id"] matches an existing goal. Returns the saved goal.
    Raises ValueError with a plain message for bad input (or an id that no longer exists)."""
    clean = validate(fields)
    goals = load(folder)
    gid = str(fields.get("id") or "").strip()
    now = _now()
    if gid:
        for gl in goals:
            if gl["id"] == gid:
                gl.update(clean)
                gl["updated_at"] = now
                _write(folder, goals)
                return gl
        raise ValueError("That goal no longer exists. It may have been deleted.")
    gl = {"id": uuid.uuid4().hex[:12], **clean, "created_at": now, "updated_at": now}
    goals.append(gl)
    _write(folder, goals)
    return gl


def delete_goal(folder, goal_id) -> bool:
    goals = load(folder)
    keep = [gl for gl in goals if gl["id"] != str(goal_id)]
    if len(keep) == len(goals):
        return False
    _write(folder, keep)
    return True


def matches(goal: dict, row: dict) -> bool:
    """Same combo rule as library.pb_for: ids when both sides have them, else names. A goal's track
    may also be written as the short name ("Roval")."""
    from .library import same_combo, _norm
    if same_combo(goal, row):
        return True
    if None not in (goal.get("track_id"), goal.get("car_id"), row.get("track_id"), row.get("car_id")):
        return False
    return (_norm(goal.get("track")) in (_norm(row.get("track")), _norm(row.get("track_short")))
            and _norm(goal.get("car")) == _norm(row.get("car")))


def for_combo(folder, track, car, track_id=None, car_id=None) -> dict | None:
    probe = {"track": track, "car": car, "track_id": track_id, "car_id": car_id}
    from .library import short_track_name
    probe["track_short"] = short_track_name(track)
    hits = [gl for gl in load(folder) if matches(gl, probe)]
    return hits[-1] if hits else None


def progress(folder, rows: list | None = None) -> list:
    """[{goal, best, gap, history: [{date, best}], achieved, races, best_date, best_path, first_best, pct}]

    gap = best - target in seconds (positive = still that much to find; <= 0 = achieved).
    history = each reviewed race's best lap at the combo, oldest first.
    pct = share of the way from your first reviewed best to the target (0..1), None without history."""
    from . import library
    if rows is None:
        rows = library.list_reviews(folder)
    out = []
    for gl in load(folder):
        # races whose best isn't clean (every lap flagged, or a partial lap) never count toward a goal
        mine = sorted([r for r in rows if matches(gl, r) and r.get("best_clean", True)], key=library._ts)
        hist = [{"date": r["date"], "best": r["best"], "path": r["path"]} for r in mine]
        best_r = min(mine, key=lambda r: r["best"]) if mine else None
        best = best_r["best"] if best_r else None
        gap = round(best - gl["target_s"], 3) if best is not None else None
        first = mine[0]["best"] if mine else None
        pct = None
        if best is not None:
            if best <= gl["target_s"]:
                pct = 1.0
            elif first is not None and first > gl["target_s"]:
                pct = max(0.0, min(1.0, (first - best) / (first - gl["target_s"])))
        out.append({"goal": gl, "best": best, "gap": gap, "history": hist,
                    "achieved": best is not None and best <= gl["target_s"], "races": len(mine),
                    "best_date": best_r["date"] if best_r else None, "best_path": best_r["path"] if best_r else None,
                    "first_best": first, "pct": pct})
    return out
