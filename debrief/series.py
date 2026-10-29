"""Your series: which iRacing series the Prepare page follows.

Garage 61 doesn't record which series a race belonged to, so Debrief suggests series by matching
your synced race sessions (car + track + race week) against the loaded season schedule:

  * a series is pre-ticked only when it's the only match for at least 2 races in different weeks
  * other matches are shown unticked
  * you tick the final list; Debrief never changes it on its own

The list is saved by series name as well as id, so it carries over to next season's schedule.
No Garage 61 requests: everything comes from the synced history on this PC.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import fsutil, schedule

SCHEMA = 1
MAX_SERIES = 40


def _path(library_folder) -> Path:
    return Path(library_folder) / "my-series.json"


def load(library_folder) -> dict:
    """{"chosen": bool, "series": [{"id", "name"}], "updated": iso|None}. chosen=False until you've saved once."""
    try:
        d = json.loads(_path(library_folder).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"chosen": False, "series": [], "updated": None}
    if not isinstance(d, dict):
        return {"chosen": False, "series": [], "updated": None}
    items = [x for x in d.get("series") or [] if isinstance(x, dict) and isinstance(x.get("name"), str)]
    return {"chosen": True, "series": [{"id": str(x.get("id") or schedule.slug(x["name"])), "name": x["name"]} for x in items],
            "updated": d.get("updated") if isinstance(d.get("updated"), str) else None}


def save(library_folder, ids: list, sched) -> dict:
    """Save your series (ids from the loaded schedule). Unknown ids are an error."""
    if not isinstance(ids, list) or len(ids) > MAX_SERIES or not all(isinstance(i, str) for i in ids):
        raise ValueError("Bad request.")
    keep = []
    prev = {x["id"]: x for x in load(library_folder)["series"]}
    for i in dict.fromkeys(ids):
        s = schedule.find_series(sched, i)
        if s is None and i in prev:          # from an older season: keep it as it was
            keep.append(prev[i])
            continue
        if s is None:
            raise ValueError("That series isn't in the loaded schedule.")
        keep.append({"id": s["id"], "name": s["name"]})
    d = {"schema": SCHEMA, "series": keep,
         "updated": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")}
    p = _path(library_folder)
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
    return load(library_folder)


def resolve(mine: dict, sched) -> list:
    """Your saved series as they appear in the loaded schedule (matched by id, then by name), in order.
    Series not running this season come back with "missing": True."""
    out = []
    for x in mine.get("series") or []:
        s = schedule.find_series(sched, x["id"])
        if s is None:
            s = next((y for y in (sched or {}).get("series") or [] if schedule.norm(y["name"]) == schedule.norm(x["name"])), None)
        out.append(s if s is not None else {"id": x["id"], "name": x["name"], "missing": True})
    return out


def _session_day(s) -> date | None:
    try:
        return datetime.fromisoformat(str(s.get("start") or "").replace("Z", "+00:00")[:19]).date()
    except ValueError:
        return None


def suggest(sched, sessions: list, today: date | None = None) -> list:
    """Series your recent races match. Each: {"id", "name", "races", "weeks", "sure", "shared"}.
    Only race sessions (type 3) from this season count."""
    if not sched:
        return []
    a, b = schedule.season_range(sched)
    if a is None:
        return []
    today = today or datetime.now(timezone.utc).date()
    lo = a - timedelta(days=7)   # a race in the off-week before week 1 still counts for week 1
    hits: dict = {}
    for s in sessions or []:
        if s.get("type") != 3:
            continue
        day = _session_day(s)
        if day is None or not lo <= day < b or day > today:
            continue
        wk = schedule.week_on(sched, day) or 1
        cands = []
        for ser in sched.get("series") or []:
            w = next((w for w in ser["weeks"] if w["week"] == wk), None)
            if w is None or not schedule.same_track(s.get("track"), w["track"]):
                continue
            if ser["cars"] and not any(schedule.same_car(s.get("car"), c) for c in ser["cars"]):
                continue
            cands.append(ser)
        for ser in cands:
            h = hits.setdefault(ser["id"], {"id": ser["id"], "name": ser["name"], "races": 0, "weeks": set(),
                                            "unique_races": 0, "category": ser.get("category"),
                                            "multiclass": ser.get("multiclass", False)})
            h["races"] += 1
            h["weeks"].add(wk)
            if len(cands) == 1:
                h["unique_races"] += 1
    out = []
    for h in hits.values():
        weeks = sorted(h.pop("weeks"))
        uniq = h.pop("unique_races")
        h["weeks"] = weeks
        h["shared"] = uniq < h["races"]          # some races also matched another series (same car)
        h["sure"] = uniq >= 2 and len(weeks) >= 2
        out.append(h)
    out.sort(key=lambda h: (not h["sure"], -h["races"], h["name"].lower()))
    return out
