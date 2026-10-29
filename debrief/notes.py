"""Track cards: short, hand-written notes for each track layout.

One small JSON file per layout in <library>/track-notes/. Notes belong to the track, not to a series
or a car, so a card follows the track into every series that visits it. Telemetry numbers (brake
points, gears, weak corners) are never stored here: the Prepare page works them out from your laps.

Five optional fields, one or two lines each, plus an optional line per car:
    lap1      Lap 1 / the start
    hotspots  where people crash
    passing   where to pass and defend
    limits    track limits and kerbs
    focus     what to work on this week (up to 3 lines)
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import fsutil

SCHEMA = 1
FIELDS = ("lap1", "hotspots", "passing", "limits", "focus")
LABELS = {"lap1": "Lap 1 / start", "hotspots": "Crash hotspots", "passing": "Passing spots",
          "limits": "Track limits / kerbs", "focus": "Focus this week"}
MAX_FIELD = 300
MAX_FOCUS_LINES = 3
MAX_CAR_NOTE = 200
MAX_CARS = 20


def track_key(name: str) -> str:
    """A file-safe key for a track layout. Years are dropped ('Roval 2019' and 'Roval 2025' share a card),
    and 'Track - Config' and 'Track (Config)' give the same key."""
    s = str(name or "").lower()
    s = re.sub(r"\b(19|20)\d\d\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    s = re.sub(r"-+", "-", s)
    return s[:120] or "unknown-track"


def folder(library_folder) -> Path:
    return Path(library_folder) / "track-notes"


def _path(library_folder, key: str) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,119}", key or ""):
        raise ValueError("That track wasn't found.")
    return folder(library_folder) / f"{key}.json"


def empty(track: str) -> dict:
    return {"schema": SCHEMA, "key": track_key(track), "track": str(track or "").strip(), "fields": {k: "" for k in FIELDS},
            "car_notes": {}, "updated": None, "season": None}


def load(library_folder, track: str) -> dict:
    """The card for a track (an empty one if none is saved). Never raises for a bad file."""
    key = track_key(track)
    out = empty(track)
    try:
        d = json.loads(_path(library_folder, key).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    if not isinstance(d, dict):
        return out
    f = d.get("fields") if isinstance(d.get("fields"), dict) else {}
    out["fields"] = {k: str(f.get(k) or "") for k in FIELDS}
    cn = d.get("car_notes") if isinstance(d.get("car_notes"), dict) else {}
    out["car_notes"] = {str(k): str(v) for k, v in cn.items() if isinstance(v, str) and v.strip()}
    out["updated"] = d.get("updated") if isinstance(d.get("updated"), str) else None
    out["season"] = d.get("season") if isinstance(d.get("season"), str) else None
    if isinstance(d.get("track"), str) and d["track"].strip():
        out["track"] = d["track"]
    out["read_only"] = isinstance(d.get("schema"), int) and d["schema"] > SCHEMA
    return out


def has_notes(card: dict) -> bool:
    return any((card.get("fields") or {}).get(k) for k in FIELDS) or bool(card.get("car_notes"))


def clean(raw) -> dict:
    """Validate a card from the window. Raises ValueError with a friendly message."""
    if not isinstance(raw, dict):
        raise ValueError("Bad request.")
    track = raw.get("track")
    if not isinstance(track, str) or not track.strip() or len(track) > 200:
        raise ValueError("Pick a track for the card.")
    fields = raw.get("fields") if isinstance(raw.get("fields"), dict) else {}
    out = {}
    for k in FIELDS:
        v = fields.get(k)
        if v in (None, ""):
            out[k] = ""
            continue
        if not isinstance(v, str):
            raise ValueError(f"{LABELS[k]} must be text.")
        v = "\n".join(line.strip() for line in v.replace("\r", "").split("\n") if line.strip())
        if len(v) > MAX_FIELD:
            raise ValueError(f"Keep {LABELS[k]} short: up to {MAX_FIELD} characters.")
        if k == "focus" and len(v.split("\n")) > MAX_FOCUS_LINES:
            raise ValueError(f"Pick at most {MAX_FOCUS_LINES} things to focus on.")
        out[k] = v
    cars = {}
    cn = raw.get("car_notes")
    if cn not in (None, ""):
        if not isinstance(cn, dict) or len(cn) > MAX_CARS:
            raise ValueError("Bad request.")
        for car, note in cn.items():
            if not isinstance(car, str) or not car.strip() or len(car) > 200:
                raise ValueError("Bad request.")
            if note in (None, ""):
                continue
            if not isinstance(note, str):
                raise ValueError("Car notes must be text.")
            note = " ".join(note.split())
            if len(note) > MAX_CAR_NOTE:
                raise ValueError(f"Keep car notes short: up to {MAX_CAR_NOTE} characters.")
            if note:
                cars[car.strip()] = note
    season = raw.get("season")
    season = season.strip()[:40] if isinstance(season, str) and season.strip() else None
    return {"track": track.strip(), "fields": out, "car_notes": cars, "season": season}


def save(library_folder, card: dict) -> dict:
    """Save a cleaned card (see clean). An all-empty card deletes the file."""
    key = track_key(card["track"])
    p = _path(library_folder, key)
    existing = load(library_folder, card["track"])
    if existing.get("read_only"):
        raise ValueError("This card was saved by a newer Debrief. Update Debrief to edit it.")
    d = {"schema": SCHEMA, "key": key, "track": card["track"], "fields": {k: card["fields"].get(k, "") for k in FIELDS},
         "car_notes": dict(card.get("car_notes") or {}), "season": card.get("season") or existing.get("season"),
         "updated": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")}
    if not has_notes(d):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        return empty(card["track"])
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=p.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1, ensure_ascii=False)
        fsutil.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return load(library_folder, card["track"])


def all_cards(library_folder) -> list:
    """Every saved card, newest first (for 'which tracks have notes')."""
    out = []
    for p in folder(library_folder).glob("*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and isinstance(d.get("track"), str):
            out.append({"key": p.stem, "track": d["track"], "updated": d.get("updated")})
    out.sort(key=lambda c: c.get("updated") or "", reverse=True)
    return out
