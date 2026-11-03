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
import re
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
    except (OSError, ValueError, RecursionError):
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


# ---------------------------------------------------------------- the Prepare picker's last choice (2.11)
# Saved beside my-series.json, not in the browser: the window's address changes each launch, and its storage with it.
def _choice_path(library_folder) -> Path:
    return Path(library_folder) / "prepare-choice.json"


def _clean_choice(d) -> dict:
    d = d if isinstance(d, dict) else {}
    def text(k):
        v = d.get(k)
        return v if isinstance(v, str) and v.strip() and len(v) <= 200 and not re.search(r"[\x00-\x1f<>]", v) else None
    wk = d.get("week")
    wk = wk if isinstance(wk, int) and not isinstance(wk, bool) and 0 < wk < 100 else None
    return {"car": text("car"), "series": text("series"), "track": text("track"), "week": wk,
            "follow_now": d.get("follow_now") is True, "season": text("season")}


def load_choice(library_folder) -> dict:
    try:
        return _clean_choice(json.loads(_choice_path(library_folder).read_text(encoding="utf-8")))
    except (OSError, ValueError, RecursionError):
        return _clean_choice(None)


def save_choice(library_folder, d) -> dict:
    """Only ever what the picker shows. It's a convenience, so anything odd in it is dropped, not an error."""
    keep = _clean_choice(d)
    p = _choice_path(library_folder)
    p.parent.mkdir(parents=True, exist_ok=True)
    fsutil.write_text(p, json.dumps({"schema": SCHEMA, **keep}, indent=1, ensure_ascii=False))
    return keep


def resolve(mine: dict, sched) -> list:
    """Your saved series as they appear in the loaded schedule (matched by name, then by id), in order.
    Name first: ids made from duplicate names ('-2') depend on the PDF's order and can point at another
    series next season. Series not running this season come back with "missing": True."""
    out = []
    all_s = (sched or {}).get("series") or []
    used = set()
    for x in mine.get("series") or []:
        named = [y for y in all_s if schedule.norm(y["name"]) == schedule.norm(x["name"]) and id(y) not in used]
        if len(named) == 1:
            s = named[0]
        elif named:   # the same name twice: the id says which one
            s = next((y for y in named if y.get("id") == x["id"]), named[0])
        else:
            s = schedule.find_series(sched, x["id"])
            if s is not None and schedule.norm(s["name"]) != schedule.norm(x["name"]):
                # same id, different name: only trust it when the names are close (a renamed series)
                if not (schedule._tokens(s["name"]) & schedule._tokens(x["name"])):
                    s = None
        if s is not None:
            used.add(id(s))
        out.append(s if s is not None else {"id": x["id"], "name": x["name"], "missing": True})
    return out


def _session_day(s) -> date | None:
    try:
        return datetime.fromisoformat(str(s.get("start") or "").replace("Z", "+00:00")[:19]).date()
    except ValueError:
        return None


def suggest(sched, sessions: list, today: date | None = None, matcher=None) -> list:
    """Series your recent races match. Each: {"id", "name", "races", "weeks", "sure", "shared"}.
    Only race sessions (type 3) inside this season's race weeks count. matcher: a schedule.Matcher built
    from Garage 61's cached track/car lists, so sessions match by id where the names allow it."""
    matcher = matcher or schedule.Matcher()
    if not sched:
        return []
    a, b = schedule.season_range(sched)
    if a is None:
        return []
    today = today or datetime.now(timezone.utc).date()
    lo = a   # races before week 1 belong to the previous season (or its off-week), not to week 1
    hits: dict = {}
    for s in sessions or []:
        if s.get("type") != 3:
            continue
        day = _session_day(s)
        if day is None or not lo <= day < b or day > today:
            continue
        wk = schedule.week_on(sched, day)
        if wk is None:
            continue
        cands = []
        for ser in sched.get("series") or []:
            w = next((w for w in ser["weeks"] if w.get("season_week", w["week"]) == wk), None)
            if w is None or not matcher.track(s, w["track"]):
                continue
            cars_now = schedule.week_cars(ser, w)
            if cars_now and not any(matcher.car(s, c) for c in cars_now):
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


# ---------------------------------------------------------------- what the schedule says about a week
def week_facts(week: dict | None) -> dict:
    """The race's length, rain, start type and cautions, in plain words, from the schedule's own text.
    None means the schedule doesn't say: never read that as dry or as any particular start."""
    text = str((week or {}).get("conditions") or "")
    rain = None
    m = re.search(r"\brain chance\s+(\d+)\s*%", text, re.I)
    word = re.search(r"\brain chance\s+([A-Za-z]+)", text, re.I)
    if re.search(r"\brain possible\b", text, re.I):
        rain = "Rain possible"
    elif m and int(m.group(1)) > 0:
        rain = f"Rain chance {int(m.group(1))}%"
    elif word and word.group(1).lower() not in ("none", "no", "zero"):
        rain = f"Rain chance {word.group(1).lower()}"
    elif m or re.search(r"\brain chance none\b|\ball dry events\b", text, re.I):
        rain = "No rain"
    start = next((w for w in ("Rolling", "Standing") if re.search(rf"\b{w} start\b", text, re.I)), None)
    cautions = next((label for pat, label in ((r"cautions disabled", "No cautions"),
                                              (r"local enforced cautions", "Local enforced cautions"),
                                              (r"local advisory cautions", "Local advisory cautions"),
                                              (r"full course cautions", "Full course cautions"))
                     if re.search(pat, text, re.I)), None)
    return {"length": (week or {}).get("length"), "rain": rain, "start_type": f"{start} start" if start else None,
            "cautions": cautions}


def week_left(week: dict, current: int | None, today: date) -> bool:
    """Is this week still to come, or running now? Series whose weeks start a day or two off the season's go by
    the season week number; between seasons, by the dates. Weeks outside the season are never left."""
    no = week.get("season_week", week.get("week"))
    if not no:
        return False
    if current:
        return no >= current
    try:
        return date.fromisoformat(str(week.get("start"))) + timedelta(days=7) > today
    except ValueError:
        return False


def upcoming_at(sched, mine: dict, track: str, car: str, today: date, limit: int = 3) -> list:
    """Your own series' weeks still to come at this track in this car, soonest first, with week_facts."""
    if not sched:
        return []
    current = schedule.week_on(sched, today)
    out = []
    for ser in resolve(mine, sched):
        if ser.get("missing"):
            continue
        for w in ser.get("weeks") or []:
            if not week_left(w, current, today) or not schedule.same_track(track, w.get("track")):
                continue
            cars = schedule.week_cars(ser, w)
            if cars and not any(schedule.same_car(car, c) for c in cars):
                continue
            no = w.get("season_week", w.get("week"))
            out.append({"series": ser.get("name"), "week": w.get("week"), "start": w.get("start"),
                        "this_week": bool(current) and no == current, **week_facts(w)})
    out.sort(key=lambda x: str(x["start"]))
    return out[:limit]
