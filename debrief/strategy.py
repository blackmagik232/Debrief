"""Fuel and pit strategy (Debrief 2.4 "Race smarter").

Four parts, none of which contacts Garage 61:
  review_block(laps, raw_laps, clean, L)   engine time: fuel use per lap, fuel level and pit stops of one race
  block_from_review(data)                  the same fuel numbers for an older review (from stint.laps)
  prefill(cfg, store, sched, track, cars, week)   starting numbers for the fuel planner (library, sync store,
                                           schedule week and <library>/strategy.json only)
  plan(inputs)                             pure arithmetic: laps, fuel needed, stops, stints and pit windows

Field names. Garage 61 lap objects are read with both spellings: fuelUsed / fuel_used, fuelLevel /
fuel_level and pitIn / pitOut / pitlane (plus pit_in / pit_out / pit_lane). None of these is confirmed
against a real payload yet (check <cache>/sample_lap.json, written by Check connection). Every one is
optional: without fuel fields the review block is None, and without pit flags the pit part is None.

Units: litres (_l), seconds (_s or t). The pit loss is the in-lap plus the out-lap minus two typical
laps, so it INCLUDES the time spent refuelling.
"""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

VERSION = 1
FILE = "strategy.json"
SCHEMA = 1

FUEL_USED = ("fuelUsed", "fuel_used")
FUEL_LEVEL = ("fuelLevel", "fuel_level")
PIT_IN = ("pitIn", "pit_in")
PIT_OUT = ("pitOut", "pit_out")
PIT_LANE = ("pitlane", "pit_lane", "pitLane")
MIN_BURN_L = 0.2          # a lap using less than this is a glitch (or a lap under caution with no data)
MIN_BURN_LAPS = 3         # fewer laps with fuel use: no burn figure
FEW_BURN_LAPS = 5         # fewer than this: the planner warns
MAX_PIT_LOSS_S = 300.0
SYNC_BURN_LAPS = 50
GAL_L = 3.785

NO_FUEL_NOTE = ("Debrief couldn't find fuel use for this car and track yet. Type it in, or check the fuel use "
                "in the garage.")


# ---------------------------------------------------------------- small helpers
def _g(d, names):
    if not isinstance(d, dict):
        return None
    for n in names:
        if d.get(n) is not None:
            return d[n]
    return None


def _num(v):
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _r(v, d=3):
    return None if v is None else round(float(v), d)


def _truthy(lap, names) -> bool:
    return any(bool(lap.get(n)) for n in names) if isinstance(lap, dict) else False


def _lap_t(l):
    t = _num(_g(l, ("lap_time", "lapTime")))
    return t if t and t > 0 else None


def _numbers(raw_laps):
    from .engine import _lap_numbers
    return _lap_numbers([l for l in raw_laps or [] if isinstance(l, dict)])


def _ceil1(x):
    """Round UP to 0.1 (with a little slack so 40.0000001 stays 40.0)."""
    return math.ceil(round(x * 10, 6)) / 10.0


def _median(vals):
    vals = [v for v in vals if v is not None]
    return float(np.median(vals)) if vals else None


# ---------------------------------------------------------------- fuel use
def burn_from(used: dict, level: dict) -> dict | None:
    """Fuel use per lap from {lap n: fuel used} (preferred) or {lap n: fuel level} (drops between
    consecutive laps). Returns the `burn` block or None with fewer than MIN_BURN_LAPS usable laps."""
    vals = {n: v for n, v in used.items() if v is not None and v > MIN_BURN_L}
    source = "fuel_used"
    if vals:
        med = float(np.median(list(vals.values())))
        vals = {n: v for n, v in vals.items() if v < 3 * med}
    if len(vals) < MIN_BURN_LAPS:
        vals, source = {}, "fuel_level"
        ns = sorted(n for n, v in level.items() if v is not None)
        drops = {}
        for a, b in zip(ns, ns[1:]):
            if b == a + 1:
                d = level[a] - level[b]
                if d > 0:
                    drops[b] = d           # a refuel (negative) never counts
        if drops:
            med = float(np.median(list(drops.values())))
            vals = {n: v for n, v in drops.items() if MIN_BURN_L < v < 3 * med}
    if len(vals) < MIN_BURN_LAPS:
        return None
    arr = np.array(list(vals.values()), float)
    return {"median_l": _r(float(np.median(arr)), 3), "p90_l": _r(float(np.percentile(arr, 90)), 3),
            "n": len(vals), "source": source, "laps": {str(n): _r(v, 3) for n, v in sorted(vals.items())}}


def _level_block(levels: list, totals: list) -> dict | None:
    """levels: fuel levels in lap order; totals: level + used where both are known (fuel at the lap start)."""
    if not levels:
        return None
    seen = [v for v in levels + totals if v is not None]
    return {"start_l": _r(levels[0], 2), "end_l": _r(levels[-1], 2), "max_seen_l": _r(max(seen), 2)}


# ---------------------------------------------------------------- pit stops
def pit_kind(lap) -> str | None:
    i, o = _truthy(lap, PIT_IN), _truthy(lap, PIT_OUT)
    if i and o:
        return "through"
    if i:
        return "in"
    if o:
        return "out"
    if _truthy(lap, PIT_LANE):
        return "lane"
    return None


def pit_stops(raw_laps, median_clean) -> dict | None:
    """Pit laps of one session and the time each stop took against two typical laps.
    None when no lap carries a pit flag."""
    nums = _numbers(raw_laps)
    rows = []
    for l in raw_laps or []:
        if not isinstance(l, dict):
            continue
        k = pit_kind(l)
        n = nums.get(l.get("id"))
        if k and n is not None:
            rows.append({"n": int(n), "t": _r(_lap_t(l)), "kind": k})
    if not rows:
        return None
    rows.sort(key=lambda r: r["n"])
    # pitlane-only data: two laps in a row in the lane are the in-lap and the out-lap
    for a, b in zip(rows, rows[1:]):
        if a["kind"] == "lane" and b["kind"] == "lane" and b["n"] == a["n"] + 1:
            a["kind"], b["kind"] = "in", "out"
    by_n = {r["n"]: r for r in rows}
    losses, stops = [], 0
    for r in rows:
        if r["kind"] == "in":
            stops += 1
            o = by_n.get(r["n"] + 1)
            if o and o["kind"] == "out" and r["t"] and o["t"] and median_clean:
                losses.append(r["t"] + o["t"] - 2 * median_clean)
        elif r["kind"] == "through":
            stops += 1
            if r["t"] and median_clean:
                losses.append(r["t"] - median_clean)
    losses = [v for v in losses if 0 <= v <= MAX_PIT_LOSS_S]
    return {"laps": rows, "loss_s": _r(_median(losses), 1), "n": stops,
            "losses": [_r(v, 1) for v in losses], "median_clean_t": _r(median_clean)}


# ---------------------------------------------------------------- review blocks
def review_block(laps, raw_laps, clean, L=None) -> dict | None:
    """Engine time. laps: the analysed engine laps (n, t, lap = Garage 61 object; pit, partial and tow laps
    already left out); raw_laps: every Garage 61 lap of the session; clean: the engine's clean subset."""
    used, level = {}, {}
    for x in laps or []:
        lap = x.get("lap") or {}
        n = x.get("n")
        if n is None:
            continue
        used[int(n)] = _num(_g(lap, FUEL_USED))
        level[int(n)] = _num(_g(lap, FUEL_LEVEL))
    nums = _numbers(raw_laps)
    raw_rows = sorted(((nums.get(l.get("id")), l) for l in raw_laps or [] if isinstance(l, dict)
                       and nums.get(l.get("id")) is not None), key=lambda p: p[0])
    lv_all = [(_num(_g(l, FUEL_LEVEL)), _num(_g(l, FUEL_USED))) for _, l in raw_rows] or \
             [(level.get(n), used.get(n)) for n in sorted(level)]
    have_used = any(v is not None for v in used.values()) or any(u is not None for _, u in lv_all)
    have_level = any(v is not None for v in level.values()) or any(v is not None for v, _ in lv_all)
    if not have_used and not have_level:
        return None
    burn = burn_from(used, level)
    levels = [v for v, _ in lv_all if v is not None]
    totals = [v + u for v, u in lv_all if v is not None and u is not None and u > 0]
    med = _median([x["t"] for x in clean or []]) or _median([x["t"] for x in laps or []])
    pit = pit_stops(raw_laps, med)
    note = None if burn else f"Fewer than {MIN_BURN_LAPS} laps had a usable fuel reading."
    return {"version": VERSION, "burn": burn, "level": _level_block(levels, totals), "pit": pit, "note": note}


def block_from_review(data) -> dict | None:
    """The fuel part of review_block for a saved review, from stint.laps (v2 reviews). pit is None:
    old reviews don't keep their pit laps."""
    stint = (data or {}).get("stint") if isinstance(data, dict) else None
    rows = [x for x in (stint or {}).get("laps") or [] if isinstance(x, dict) and x.get("n") is not None] \
        if isinstance(stint, dict) else []
    used = {int(x["n"]): _num(x.get("fuel_used")) for x in rows}
    level = {int(x["n"]): _num(x.get("fuel_level")) for x in rows}
    if all(v is None for v in used.values()) and all(v is None for v in level.values()):
        return None
    burn = burn_from(used, level)
    ns = sorted(level)
    levels = [level[n] for n in ns if level[n] is not None]
    totals = [level[n] + used[n] for n in ns if level[n] is not None and used.get(n) and used[n] > 0]
    note = None if burn else f"Fewer than {MIN_BURN_LAPS} laps had a usable fuel reading."
    return {"version": VERSION, "burn": burn, "level": _level_block(levels, totals), "pit": None, "note": note}


# ---------------------------------------------------------------- strategy.json (tank size, pit loss)
def _path(library_folder) -> Path:
    return Path(library_folder) / FILE


def load_cars(library_folder) -> dict:
    """<library>/strategy.json as {"schema": 1, "cars": {name: {"tank_l", "pit_loss_s": {track_key: s}, "updated"}}}.
    A missing or unreadable file gives an empty one (never raises)."""
    out = {"schema": SCHEMA, "cars": {}}
    try:
        d = json.loads(_path(library_folder).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return out
    if not isinstance(d, dict) or not isinstance(d.get("cars"), dict):
        return out
    for name, c in d["cars"].items():
        if not isinstance(name, str) or not isinstance(c, dict):
            continue
        tank = _num(c.get("tank_l"))
        pl = c.get("pit_loss_s") if isinstance(c.get("pit_loss_s"), dict) else {}
        out["cars"][name] = {"tank_l": tank if tank and 1 <= tank <= 200 else None,
                             "pit_loss_s": {str(k): float(v) for k, v in pl.items()
                                            if _num(v) is not None and 0 <= _num(v) <= MAX_PIT_LOSS_S},
                             "updated": c.get("updated") if isinstance(c.get("updated"), str) else None}
    return out


def _write(p: Path, obj) -> None:
    from . import fsutil
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=p.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(obj, indent=1, ensure_ascii=False))
            f.flush()
            os.fsync(f.fileno())
        fsutil.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _car_entry(cars: dict, car: str):
    """The saved name for a car: the exact name, else one that schedule.same_car says is the same car."""
    if car in cars:
        return car
    from .schedule import same_car
    return next((k for k in cars if same_car(k, car)), None)


def save_car(library_folder, car: str, tank_l=None, pit_loss_s=None, track: str | None = None) -> dict:
    """Save a car's tank size and/or its pit loss at one track. Returns that car's saved entry.
    Raises ValueError("plain message") for bad input."""
    car = " ".join(str(car or "").split())
    if not car or len(car) > 120:
        raise ValueError("Pick the car first.")
    tank = None
    if tank_l is not None and tank_l != "":
        tank = _num(tank_l)
        if tank is None or not 1 <= tank <= 200:
            raise ValueError("The tank size must be between 1 and 200 litres.")
    loss = None
    if pit_loss_s is not None and pit_loss_s != "":
        loss = _num(pit_loss_s)
        if loss is None or not 0 <= loss <= MAX_PIT_LOSS_S:
            raise ValueError("The pit stop time must be between 0 and 300 seconds.")
        if not str(track or "").strip():
            raise ValueError("Pick the track for that pit stop time.")
    if tank is None and loss is None:
        raise ValueError("Type a tank size or a pit stop time to save.")
    doc = load_cars(library_folder)
    key = _car_entry(doc["cars"], car) or car
    entry = doc["cars"].setdefault(key, {"tank_l": None, "pit_loss_s": {}, "updated": None})
    if tank is not None:
        entry["tank_l"] = round(tank, 1)
    if loss is not None:
        from .notes import track_key
        entry["pit_loss_s"][track_key(track)] = round(loss, 1)
    entry["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _write(_path(library_folder), doc)
    return {"car": key, **entry}


# ---------------------------------------------------------------- prefill (no requests)
def race_length(week) -> dict:
    """{"laps", "minutes", "source", "text"} from a schedule week's `length` ("15 laps", "40 mins",
    "1 hour 30 mins"). Both None without a week or a length."""
    text = (week or {}).get("length") if isinstance(week, dict) else None
    out = {"laps": None, "minutes": None, "source": None, "text": text if isinstance(text, str) else None}
    if not isinstance(text, str) or not text.strip():
        return out
    s = text.lower()
    m = re.search(r"(\d+)\s*laps?\b", s)
    if m:
        out["laps"], out["source"] = int(m.group(1)), "schedule"
        return out
    h = re.search(r"(\d+)\s*(?:hours?|hrs?|h)\b", s)
    mm = re.search(r"(\d+)\s*(?:mins?|minutes?|m)\b", s)
    if h or mm:
        out["minutes"] = (int(h.group(1)) * 60 if h else 0) + (int(mm.group(1)) if mm else 0)
        out["source"] = "schedule"
    return out


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:  # noqa: BLE001  (a broken store or file never stops the planner opening)
        return default


def _day_label(row) -> str:
    day = str(row.get("date") or "")[:10]
    try:
        d = datetime.strptime(day, "%Y-%m-%d")
        return f"{d.strftime('%b')} {d.day}"
    except ValueError:
        return day or "an earlier race"


def _is_race(row) -> bool:
    s = row.get("session")
    return s is None or str(s).strip().lower().startswith("race")


def _review(cfg, row):
    p = Path(cfg["library_folder"]) / "reviews" / str(row.get("dir") or "") / "review.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    return d if isinstance(d, dict) else None


def _primary_car(cars, rows):
    """The car to plan for: the one you raced here most recently, else the first one listed."""
    for r in rows:
        return r.get("car")
    return cars[0] if cars else None


def prefill(cfg: dict, store, sched, track: str, cars: list, week: dict | None) -> dict:
    """Starting numbers for the fuel planner, each with where it came from. No requests: reads the
    library, the sync store (store may be None), the schedule week and <library>/strategy.json."""
    from . import library, schedule
    from .notes import track_key
    cfg = cfg if isinstance(cfg, dict) else {}
    cars = [str(c) for c in (cars or []) if str(c or "").strip()]
    track = str(track or "")
    out = {"race": race_length(week), "lap_t": None, "burn": None, "tank": None, "pit_loss": None,
           "reserve_laps": 1.0, "note": None}

    rows = []
    if cfg.get("library_folder"):
        rows = _safe(lambda: library.list_reviews(cfg["library_folder"], _pbs=False), []) or []
    here = [r for r in rows if schedule.same_track(r.get("track"), track)
            and (not cars or any(schedule.same_car(r.get("car"), c) for c in cars))]
    races = [r for r in here if _is_race(r)]
    car = _primary_car(cars, races or here)
    track_ids = {r["track_id"] for r in here if r.get("track_id") is not None}
    car_ids = {r["car_id"] for r in here if r.get("car_id") is not None}

    # synced sessions here (ids from the library, else names)
    sessions = _safe(lambda: store.sessions(), []) if store is not None else []
    sessions = [s for s in sessions or [] if isinstance(s, dict)]
    mine = []
    for s in sessions:
        t_ok = s.get("track_id") in track_ids or schedule.same_track(s.get("track"), track)
        c_ok = s.get("car_id") in car_ids or not cars or any(schedule.same_car(s.get("car"), c) for c in cars)
        if t_ok and c_ok:
            mine.append(s)
            if s.get("track_id") is not None:
                track_ids.add(s["track_id"])
            if s.get("car_id") is not None:
                car_ids.add(s["car_id"])
    car_only = {s.get("car_id") for s in sessions if s.get("car_id") is not None
                and (s.get("car_id") in car_ids or (car and schedule.same_car(s.get("car"), car)))}

    def synced(tids, cids):
        if store is None:
            return []
        laps = []
        for t in tids:
            for c in cids:
                d = _safe(lambda: store.laps(t, c), {}) or {}
                laps += [l for l in (d.values() if isinstance(d, dict) else []) if isinstance(l, dict)]
        return laps

    def by_start(l):
        return str(l.get("startTime") or l.get("start_time") or "")

    here_laps = synced(track_ids, car_ids)

    # lap time
    meds = [r.get("median") for r in races[:3] if _num(r.get("median"))]
    if meds:
        out["lap_t"] = {"value": _r(_median(meds), 3), "n": len(meds),
                        "source": f"median clean lap, your last {len(meds)} race{'s' if len(meds) != 1 else ''} here"}
    else:
        bests = [_num(s.get("best_clean")) for s in mine if _num(s.get("best_clean"))]
        if bests:
            out["lap_t"] = {"value": _r(min(bests) * 1.01, 3), "n": len(bests), "estimated": True,
                            "source": "estimated from your best lap"}

    # fuel use
    for r in races or here:
        d = _safe(lambda: _review(cfg, r))
        if not d:
            continue
        blk = d.get("strategy") if isinstance(d.get("strategy"), dict) else _safe(lambda: block_from_review(d))
        b = (blk or {}).get("burn") if isinstance(blk, dict) else None
        if isinstance(b, dict) and _num(b.get("median_l")):
            out["burn"] = {"value": _num(b["median_l"]), "p90": _num(b.get("p90_l")), "n": b.get("n"),
                           "source": f"your race on {_day_label(r)}"}
            break
    if out["burn"] is None and here_laps:
        race_laps = [l for l in here_laps if _race_session(l)]
        race_laps.sort(key=by_start)
        vals = [v for v in (_num(_g(l, FUEL_USED)) for l in race_laps) if v is not None and v > MIN_BURN_L]
        vals = vals[-SYNC_BURN_LAPS:]
        if vals:
            med = float(np.median(vals))
            vals = [v for v in vals if v < 3 * med]
        if len(vals) >= MIN_BURN_LAPS:
            out["burn"] = {"value": _r(float(np.median(vals)), 3), "p90": _r(float(np.percentile(vals, 90)), 3),
                           "n": len(vals), "source": f"your last {len(vals)} race laps here"}
    if out["burn"] is None:
        out["note"] = NO_FUEL_NOTE

    # tank
    saved = _safe(lambda: load_cars(cfg["library_folder"]), {"cars": {}}) if cfg.get("library_folder") \
        else {"cars": {}}
    key = _car_entry(saved["cars"], car) if car else None
    entry = saved["cars"].get(key) if key else None
    if entry and entry.get("tank_l"):
        out["tank"] = {"value": entry["tank_l"], "source": "saved for this car", "estimated": False}
    else:
        tops = [v + u for v, u in ((_num(_g(l, FUEL_LEVEL)), _num(_g(l, FUEL_USED))) for l in synced(
            {s.get("track_id") for s in sessions if s.get("car_id") in car_only and s.get("track_id") is not None}
            | track_ids, car_only | car_ids)) if v is not None and u is not None and u >= 0]
        if tops:
            out["tank"] = {"value": math.ceil(max(tops) * 2 - 1e-9) / 2.0, "estimated": True,
                           "source": "estimated: the most fuel seen in this car (check it in the garage)"}

    # pit loss
    tk = track_key(track) if track else None
    if entry and tk and tk in (entry.get("pit_loss_s") or {}):
        out["pit_loss"] = {"value": entry["pit_loss_s"][tk], "source": "saved for this car at this track", "n": None}
    else:
        losses = []
        if here_laps:
            losses += _synced_pit_losses(here_laps)
        for r in here:
            d = _safe(lambda: _review(cfg, r))
            p = ((d or {}).get("strategy") or {}).get("pit") if isinstance((d or {}).get("strategy"), dict) else None
            if isinstance(p, dict):
                losses += [v for v in (p.get("losses") or ([p.get("loss_s")] if p.get("loss_s") is not None else []))
                           if _num(v) is not None]
        losses = [float(v) for v in losses if 0 <= float(v) <= MAX_PIT_LOSS_S]
        if losses:
            n = len(losses)
            out["pit_loss"] = {"value": _r(_median(losses), 1), "n": n,
                               "source": f"{n} pit stop{'s' if n != 1 else ''} at this track"}
    return out


def _race_session(l) -> bool:
    from .sync import lap_session_type
    return lap_session_type(l) == 3


def _synced_pit_losses(laps) -> list:
    """Pit stop losses from synced laps, grouped by event + session (the same rule as review_block)."""
    from .sync import lap_clean, lap_secs
    groups = {}
    for l in laps:
        groups.setdefault((str(l.get("event")), str(l.get("sessionType") or l.get("session_type"))), []).append(l)
    out = []
    for grp in groups.values():
        if not any(pit_kind(l) for l in grp):
            continue
        ts = [lap_secs(l) for l in grp if lap_clean(l) and not pit_kind(l) and lap_secs(l)]
        med = _median(ts)
        p = pit_stops(grp, med) if med else None
        out += (p or {}).get("losses") or []
    return out


# ---------------------------------------------------------------- plan (pure)
def _in(inputs, key, lo, hi, label, required=False, whole=False):
    v = inputs.get(key)
    if v is None or v == "":
        if required:
            raise ValueError(f"Type the {label}.")
        return None
    f = _num(v)
    if f is None:
        raise ValueError(f"The {label} must be a number.")
    if whole and abs(f - round(f)) > 1e-9:
        raise ValueError(f"The {label} must be a whole number.")
    if not lo <= f <= hi:
        raise ValueError(f"The {label} must be between {lo:g} and {hi:g}.")
    return int(round(f)) if whole else f


def plan(inputs: dict) -> dict:
    """Fuel and stops for one race. inputs: laps (1-2000) or minutes (1-1440) with lap_t (10-1200 s),
    burn_l (0.05-20 L a lap), tank_l (1-200 L), pit_loss_s (0-300, optional), reserve_laps (0-5, default 1),
    use_p90 (default True: plan on p90 when it is given). Optional, for the warnings: burn_n (laps behind
    the burn figure), tank_estimated (bool). Raises ValueError("plain message")."""
    if not isinstance(inputs, dict):
        raise ValueError("Fill in the race details first.")
    laps = _in(inputs, "laps", 1, 2000, "number of laps", whole=True)
    minutes = None if laps is not None else _in(inputs, "minutes", 1, 1440, "race length in minutes")
    if laps is None and minutes is None:
        raise ValueError("Type the race length in laps or minutes.")
    lap_t = _in(inputs, "lap_t", 10, 1200, "lap time in seconds", required=minutes is not None)
    burn = _in(inputs, "burn_l", 0.05, 20, "fuel use per lap (litres)", required=True)
    # the window sends "p90" (2.4.0 sent "p90_l", the prefill's own name, which used to be ignored): accept both
    p90_key = "p90" if inputs.get("p90") not in (None, "") else "p90_l"
    p90 = _in(inputs, p90_key, 0.05, 20, "higher fuel use per lap (litres)")
    tank = _in(inputs, "tank_l", 1, 200, "tank size (litres)", required=True)
    pit_loss = _in(inputs, "pit_loss_s", 0, 300, "pit stop time in seconds")
    reserve = _in(inputs, "reserve_laps", 0, 5, "number of spare laps of fuel")
    reserve = 1.0 if reserve is None else reserve
    use_p90 = inputs.get("use_p90", True)
    if not isinstance(use_p90, bool):
        raise ValueError("Choose whether to plan on your thirstier laps.")
    burn_n = _num(inputs.get("burn_n"))
    warnings = []
    if use_p90 and p90 is not None and p90 > burn:
        burn = p90
    if minutes is not None:
        laps = int(math.floor(minutes * 60.0 / lap_t + 1e-9)) + 1
        warnings.append("Timed race: if the leader is quicker than you, you may run one lap less.")
    fuel_needed = _ceil1((laps + reserve) * burn)
    lpt = int(math.floor((tank - reserve * burn) / burn + 1e-9))
    if lpt < 1:
        raise ValueError("The tank is too small for one lap at that fuel use.")
    stints, windows = [], []
    if fuel_needed <= tank + 1e-9:
        stops, start_fuel = 0, fuel_needed
        stints.append({"from_lap": 1, "to_lap": laps, "laps": laps, "fuel_l": fuel_needed})
    else:
        stops = int(math.ceil(laps / lpt)) - 1
        start_fuel = tank
        per = int(math.ceil(laps / (stops + 1)))      # even split; per <= lpt because stops + 1 >= laps / lpt
        ends, lap0 = [], 1                            # ends[k] = last lap of stint k (stop k+1 is after it)
        for k in range(stops + 1):
            n = per if k < stops else laps - per * stops
            ends.append(lap0 + n - 1)
            lap0 += n
        # Fuel per stop (H1, 2.4.0 review). Fuel burned depends only on laps driven, so for stop k the amount
        # that gets you from the previous stop's fill to the next planned stop (or the flag) with the reserve
        # is the same whichever lap inside the window you pit on:
        #   add_k = (next_k - prev_k + reserve) * burn - level_after_prev
        # The window is every lap where that add is safe, assuming the earlier stops were made on their
        # planned laps (the level after them is what the plan says, never "a full tank" unless it was):
        #   close: you still arrive with the reserve in the tank  -> prev + floor((level - reserve*burn)/burn)
        #   open:  the add fits without spilling over the tank    -> prev + ceil((level + add - tank)/burn)
        # Because nothing spills, total fuel put in is the same wherever you stop inside the windows, so
        # moving any stop within its window never leaves a later stint short.
        stints.append({"from_lap": 1, "to_lap": ends[0], "laps": ends[0], "fuel_l": round(tank, 1)})
        level, prev = tank, 0                         # level just after the last fill, and the lap it was made
        for k in range(1, stops + 1):                 # stop k is after lap ends[k-1]; stint k runs to ends[k]
            at, nxt = ends[k - 1], ends[k]
            add = max(0.0, _ceil1((nxt - prev + reserve) * burn - level))
            # open: the add fits in the tank without spilling, so nothing is lost and later stops stay right
            w_open = max(1, prev + 1, nxt - lpt, prev + int(math.ceil((level + add - tank) / burn - 1e-9)))
            # close: you still arrive with the reserve in the tank (from the planned fill, not a full tank)
            w_close = min(laps - 1, prev + int(math.floor((level - reserve * burn) / burn + 1e-9)))
            if not w_open <= at <= w_close:           # can't happen; never show a window that isn't safe
                w_open = w_close = at
            windows.append({"stop": k, "open": int(w_open), "close": int(w_close), "at_lap": at,
                            "fuel_add_l": round(add, 1)})
            level, prev = min(tank, level - (at - prev) * burn + add), at
            stints.append({"from_lap": at + 1, "to_lap": nxt, "laps": nxt - at, "fuel_l": round(level, 1)})
    pit_time = 0.0 if stops == 0 else (round(stops * pit_loss, 1) if pit_loss is not None else None)
    if burn_n is not None and burn_n < FEW_BURN_LAPS:
        warnings.append(f"The fuel use comes from only {int(burn_n)} lap{'s' if int(burn_n) != 1 else ''}: "
                        "check it in the garage.")
    if inputs.get("tank_estimated") is True:
        warnings.append("The tank size is an estimate: check it in the garage.")
    if stops and pit_loss is None:
        warnings.append("Pit stop time unknown: type it in to see the time spent in the pits.")
    return {"laps": laps, "fuel_needed_l": fuel_needed, "stops": stops, "start_fuel_l": round(start_fuel, 1),
            "stints": stints, "windows": windows, "pit_time_s": pit_time, "laps_per_tank": lpt,
            "burn_l": round(burn, 3), "warnings": warnings}
