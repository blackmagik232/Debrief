"""The RaceCap link (2.7.5): read what the owner's separate RaceCap app already saved about a race.

Debrief only ever READS a race folder chosen in Settings: <library>/Races/<subsession>_<track>/ with scan/*.scan.json,
results.json and laps/*.json. It never opens RaceCap's config.yaml or garage61.yaml (they hold passwords), never writes
there and never talks to iRacing or OBS. Nothing needs RaceCap: with no folder set every function here does nothing.

A race belongs to a review only when Garage 61 says the review's event IS that subsession, or when the person typed the
race ID. The scan's order is "on the road"; official numbers stay the iRacing results'. Every driver's facts are worked
out by the same rules.
"""
from __future__ import annotations

import csv
import io
import json
import math
import os
import re
from datetime import datetime
from pathlib import Path

from .results_csv import clean_name, same_name

MAX_SCAN_BYTES = 80 * 1024 * 1024
MAX_TRACE_BYTES = 8 * 1024 * 1024
MAX_LOOKUPS = 4          # Garage 61 requests at most, per review
MAX_RIVALS = 3
STUCK_S = 20.0           # a pass "stuck" when the same two cars don't swap back this soon
SETBACK_PLACES = 3
_RACE_DIR = re.compile(r"^(\d+)_(.+)$")
_TOKEN_NOISE = {"the", "de", "of", "du", "la", "circuit", "raceway", "international", "speedway", "motorsport", "park", "gp", "grand", "prix"}


# ---------------------------------------------------------------- finding the race
def races_root(folder) -> Path | None:
    if not folder:
        return None
    p = Path(str(folder)).expanduser()
    for c in (p / "Races", p):
        try:
            if c.is_dir() and (c.name.lower() == "races" or any(_RACE_DIR.match(x.name) for x in c.iterdir() if x.is_dir())):
                return c
        except OSError:
            continue
    return None


def _tokens(text) -> set:
    words = set(re.findall(r"[a-z0-9]+", str(text or "").lower()))
    return (words - _TOKEN_NOISE) or words


def same_track(a, b) -> bool:
    """One name holds the other (a layout may be added), or at least half their words agree. Road America isn't Road Atlanta."""
    x, y = _tokens(a), _tokens(b)
    if not x or not y:
        return False
    return x <= y or y <= x or len(x & y) * 2 >= len(x | y)


def race_dirs(root: Path) -> list:
    out = []
    try:
        for d in root.iterdir():
            m = _RACE_DIR.match(d.name)
            if m and d.is_dir():
                out.append((m.group(1), m.group(2), d))
    except OSError:
        return []
    return out


def read_scan(race_dir: Path) -> dict | None:
    """The newest scan of a race folder, or None. A file too big or not a RaceCap scan is never used."""
    try:
        files = sorted((race_dir / "scan").glob("*.scan.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    for f in files:
        try:
            if f.stat().st_size > MAX_SCAN_BYTES:
                continue
            doc = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict) and doc.get("racecap_scan") and isinstance(doc.get("cars"), list):
            return doc
    return None


def read_official(race_dir: Path, subsession) -> dict | None:
    try:
        doc = json.loads((race_dir / "results.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or not doc.get("finish"):
        return None
    return doc if doc.get("subsession") in (None, "") or str(doc.get("subsession")) == str(subsession) else None


def _when(started_at) -> float | None:
    try:
        return datetime.fromisoformat(str(started_at).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def find_race(folder, event_id, track, started_at, lookup, subsession=None, log=None):
    """(race_dir, subsession, why_not). lookup(subsession) -> Garage 61's event id for it, or None."""
    root = races_root(folder)
    if root is None:
        return None, None, "That RaceCap folder has no Races folder."
    rows = race_dirs(root)
    if subsession:
        hit = next((r for r in rows if r[0] == str(subsession)), None)
        return (hit[2], hit[0], None) if hit else (None, None, f"RaceCap has no scanned race {subsession} in that folder.")
    cands = [r for r in rows if same_track(r[1].replace("-", " "), track)]
    if not cands:
        return None, None, "RaceCap has no race at this track."
    at = _when(started_at)

    def distance(r):
        try:
            return abs((r[2].stat().st_mtime) - at) if at else -r[2].stat().st_mtime
        except OSError:
            return math.inf
    cands.sort(key=distance)
    for sub, _slug, d in cands[:MAX_LOOKUPS]:
        try:
            if str(lookup(sub) or "") == str(event_id):
                return d, sub, None
        except Exception as e:  # noqa: BLE001  (Garage 61 is never worth losing the review over)
            if log:
                log(f"  Couldn't ask Garage 61 about RaceCap race {sub}: {type(e).__name__}")
            return None, None, "Garage 61 couldn't be asked which race this is. Type the race ID instead."
    return None, None, "Garage 61 didn't match this review to a race in RaceCap. Type the race ID if you know it."


def find_me(cars, name):
    """The driver's car in the scan: the exact name, else the one car with the same family name and first letter."""
    hits = [c for c in cars if same_name(c.get("name"), name)]
    if len(hits) == 1:
        return hits[0]
    parts = clean_name(name).lower().split()
    if len(parts) < 2:
        return None
    last = [c for c in cars if clean_name(c.get("name")).lower().split()[-1:] == parts[-1:]]
    near = [c for c in last if clean_name(c.get("name")).lower()[:1] == parts[0][:1]]
    return near[0] if len(near) == 1 else None


# ---------------------------------------------------------------- what the scan says about this driver
def _lap_at(car, t) -> int | None:
    lc = {}
    for k, v in (car.get("line_crossings") or {}).items():
        try:
            lc[int(k)] = float(v)
        except (TypeError, ValueError):
            continue
    if not lc:
        return None
    for n in sorted(lc):
        if n >= 1 and t <= lc[n]:
            return n
    return max(max(lc), 1)


def _who(c) -> str:
    c = c if isinstance(c, dict) else {}
    return clean_name(c.get("name")) or ("#" + str(c.get("number") or "?"))


def _r(v, d=2):
    return round(float(v), d) if isinstance(v, (int, float)) and math.isfinite(v) else None


def read_samples(race_dir: Path) -> dict | None:
    """The scan's position readings, for where on the lap things happened. Optional."""
    import gzip
    try:
        for f in (Path(race_dir) / "scan").glob("*.samples.json.gz"):
            if f.stat().st_size <= MAX_SCAN_BYTES:
                doc = json.loads(gzip.decompress(f.read_bytes()).decode("utf-8"))
                return doc if isinstance(doc, dict) else None
    except (OSError, ValueError, EOFError):
        pass
    return None


def build(scan: dict, me_car: dict, subsession, official: dict | None = None, counts: dict | None = None, data: dict | None = None, samples: dict | None = None) -> dict:
    """The review's data["racecap"]. Facts only; "on the road" unless the official results say otherwise."""
    me = str(me_car.get("number"))
    green = _r(scan.get("green"), 3) or 0.0
    lap = lambda t: _lap_at(me_car, t)  # noqa: E731
    # iRacing's running order isn't valid until the first line crossing: on lap 1 a pass or fight has no position
    first_line = None
    try:
        first_line = float((me_car.get("line_crossings") or {}).get("1"))
    except (TypeError, ValueError):
        pass
    # iRacing only gives live data for cars near the replay camera, so a place worked out mid-lap can't be trusted:
    # positions come only from iRacing's own reading at the line (race_analysis.positions).
    known = lambda t, v: None  # noqa: E731
    all_passes = [p for p in scan.get("passes") or [] if isinstance(p, dict) and isinstance(p.get("time"), (int, float))]
    from . import race_analysis
    by_number = {str(c.get("number")): c.get("car_idx") for c in scan.get("cars") or [] if isinstance(c, dict)}
    mine = []
    for p in sorted(all_passes, key=lambda x: x["time"]):
        a, b = str((p.get("passer") or {}).get("number")), str((p.get("passed") or {}).get("number"))
        if me not in (a, b):
            continue
        if samples and race_analysis.real_pass(samples, me_car.get("car_idx"), by_number.get(b if a == me else a), p["time"], a == me) is not True:
            continue        # the replay's readings don't show these two cars swapping on track
        back = any(str((q.get("passer") or {}).get("number")) == b and str((q.get("passed") or {}).get("number")) == a
                   and 0 < q["time"] - p["time"] <= STUCK_S for q in all_passes)
        mine.append({"t": _r(p["time"], 2), "lap": lap(p["time"]), "made": a == me, "other": _who(p.get("passed" if a == me else "passer")),
                     "for_position": known(p["time"], p.get("for_position")), "how": p.get("how") or "on track", "stuck": not back})
    fights = []
    for b in scan.get("battles") or []:
        if not isinstance(b, dict) or me not in (str((b.get("ahead") or {}).get("number")), str((b.get("behind") or {}).get("number"))):
            continue
        ahead = str((b.get("ahead") or {}).get("number")) == me
        fights.append({"from": _r(b.get("from")), "to": _r(b.get("to")), "lap_from": lap(b.get("from") or 0), "lap_to": lap(b.get("to") or 0),
                       "seconds": _r(b.get("seconds"), 1), "for_position": known(b.get("from"), b.get("for_position")), "other": _who(b.get("behind" if ahead else "ahead")),
                       "you_ahead_at_start": ahead, "closest_s": _r(b.get("closest_gap_s")), "passes": len(b.get("passes") or [])})
    incidents = []
    for i in scan.get("incidents") or []:
        if not isinstance(i, dict) or i.get("level") == "trivial":
            continue
        cs = [c for c in i.get("cars") or [] if isinstance(c, dict)]
        you = next((c for c in cs if str(c.get("number")) == me), None)
        if you is None:
            continue
        incidents.append({"t": _r(i.get("from")), "lap": i.get("lap") or lap(i.get("from") or 0), "level": i.get("level"),
                          "what": sorted((you.get("what") or {}).keys()), "others": [_who(c) for c in cs if c is not you],
                          "places_lost": you.get("places_lost"), "out": bool(you.get("out_of_race"))})
    pits = [{"t": _r(p.get("in")), "lap": race_analysis.lap_at_time(samples, me_car.get("car_idx"), p.get("in")) or lap(p.get("in") or 0), "seconds": _r(p.get("seconds"), 1)} for p in scan.get("pit_stops") or []
            if isinstance(p, dict) and str((p.get("car") or {}).get("number")) == me]
    positions = race_analysis.positions(samples, me_car)
    # The scan's grid only ranks the cars it could read at the green, so it isn't a position unless every car was read
    racing = [c for c in scan.get("cars") or [] if isinstance(c, dict) and not c.get("never_on_track")]
    grid = me_car.get("grid") if racing and all(c.get("grid") or c.get("pit_lane_start") for c in racing) else None
    finish = me_car.get("finish")
    is_official = False
    if official:
        fin = [str(x) for x in official.get("finish") or []]
        if me in fin:
            finish, is_official = fin.index(me) + 1, True
        st = (official.get("start") or {}).get(me)
        if isinstance(st, int):
            grid = st
    start_type = scan.get("start_type") if scan.get("start_type") in ("standing", "rolling") else None
    out = {"version": 2, "subsession": str(subsession), "track": scan.get("track"), "number": me, "name": _who(me_car),
           "green": green, "start_type": start_type, "grid": grid, "finish": finish, "official": is_official, "cars": len(scan.get("cars") or []),
           "ended": (me_car.get("ended") or {}).get("how"), "pos_after_lap1": next((x["pos"] for x in positions if x["lap"] == 1), None),
           "positions": positions, "passes": mine, "fights": fights, "incidents": incidents, "pits": pits,
           "field": {"passes": len(all_passes), "battles": len(scan.get("battles") or []),
                     "incidents": sum(1 for i in scan.get("incidents") or [] if isinstance(i, dict) and i.get("level") != "trivial"),
                     "pit_stops": len(scan.get("pit_stops") or [])}}
    by_lap = {x["lap"]: x["pos"] for x in positions}
    for i in incidents:         # places from iRacing's positions either side of the lap, not from the mid-lap estimate
        before = by_lap.get((i["lap"] or 1) - 1) if (i["lap"] or 1) > 1 else grid      # no position exists before the first line
        after = next((by_lap[n] for n in range((i["lap"] or 1) + 1, (i["lap"] or 1) + 3) if n in by_lap), by_lap.get(i["lap"]))
        i["places_lost"] = after - before if isinstance(before, int) and isinstance(after, int) else None
    out["stories"] = stories(out)
    story = race_analysis.build(scan, me_car, out, data or {}, samples)
    out.update(story)
    out["traffic_check"] = traffic_check(out, counts, story["lap_map"])
    return out


def stories(rc: dict) -> list:
    """Whole stories of this driver's race: a setup, what changed, and how it ended. Only what the scan shows."""
    out = []
    where = "officially" if rc.get("official") else "on the road"
    on_track = [p for p in rc["passes"] if p.get("how") == "on track"]
    made = [p for p in on_track if p["made"]]
    last_lap = rc["positions"][-1]["lap"] if rc["positions"] else None
    made = [p for p in made if (p.get("lap") or 0) > 1]
    on_track = [p for p in on_track if (p.get("lap") or 0) > 1]
    for f in sorted(rc["fights"], key=lambda x: (-(x.get("passes") or 0), -(x.get("seconds") or 0))):
        if not f.get("passes") and (f.get("seconds") or 0) < 30:
            continue
        swaps = [p for p in rc["passes"] if p["other"] == f["other"] and f["from"] - 1 <= p["t"] <= f["to"] + 1]
        beats = [{"role": "setup", "lap": f["lap_from"], "text": f"Within a second of {f['other']}" + (f", fighting for P{f['for_position']}." if f["for_position"] else ".")}]
        for p in swaps[:4]:
            beats.append({"role": "turn", "lap": p["lap"], "text": (f"You passed {f['other']}" if p["made"] else f"{f['other']} passed you")
                          + (f" for P{p['for_position']}" if p["for_position"] else "") + ("; it stuck." if p["stuck"] else "; it was swapped straight back.")})
        end = swaps[-1] if swaps else None
        result = ("You were ahead when this fight ended." if end["made"] else f"{f['other']} was ahead when this fight ended.") if end else "No pass in this fight."
        cs = f.get("closest_s")
        closest = "" if cs is None else (" Side by side at the closest." if cs < 0.01 else f" Closest gap {cs:.2f} s.")
        beats.append({"role": "outcome", "lap": f["lap_to"], "text": result + closest})
        out.append({"id": f"fight-{f['from']}", "title": f"You and {f['other']}" + (f" for P{f['for_position']}" if f["for_position"] else (f" on lap {f['lap_from']}" if f["lap_from"] == f["lap_to"] else f", laps {f['lap_from']} to {f['lap_to']}")), "beats": beats})
        if sum(1 for s in out if s["id"].startswith("fight")) >= 3:
            break
    for i in rc["incidents"]:
        lost = i.get("places_lost") or 0
        if lost < SETBACK_PLACES:
            continue
        back = [p for p in made if p["t"] > i["t"]]
        with_ = " with " + ", ".join(i["others"]) if i["others"] else ""
        what = ", ".join(i["what"]) or "incident"
        beats = [{"role": "setup", "lap": i["lap"], "text": f"{what[:1].upper() + what[1:]}{with_}: {lost} places lost."}]
        if i["out"]:
            beats.append({"role": "outcome", "lap": i["lap"], "text": "It ended your race."})
        else:
            if back:
                beats.append({"role": "turn", "lap": back[0]["lap"], "text": f"{len(back)} pass{'es' if len(back) != 1 else ''} after that, starting with {back[0]['other']}."})
            if rc.get("finish"):
                beats.append({"role": "outcome", "lap": last_lap, "text": f"Finished P{rc['finish']} {where}."})
        out.append({"id": f"setback-{i['t']}", "title": "Setback and recovery" if back and not i["out"] else "Setback", "beats": beats})
    grid, fin = rc.get("grid"), rc.get("finish")
    if isinstance(grid, int) and isinstance(fin, int) and abs(grid - fin) >= SETBACK_PLACES:
        up = fin < grid
        beats = [{"role": "setup", "lap": 1, "text": f"Started P{grid}."}]
        if rc.get("pos_after_lap1"):
            beats.append({"role": "turn", "lap": 1, "text": f"P{rc['pos_after_lap1']} after lap 1."})
        n_made, n_lost = len(made), len(on_track) - len(made)
        beats.append({"role": "turn", "lap": None, "text": f"After lap 1: {n_made} on-track pass{'es' if n_made != 1 else ''} made, {n_lost} suffered."})
        beats.append({"role": "outcome", "lap": last_lap, "text": f"Finished P{fin} {where}."})
        out.append({"id": "climb" if up else "slide", "title": f"P{grid} to P{fin}", "beats": beats})
    seen = {}
    for s in out:
        seen[s["title"]] = seen.get(s["title"], 0) + 1
    for s in out:
        lap = s["beats"][0].get("lap") if s["beats"] else None
        if seen[s["title"]] > 1 and lap:
            s["title"] += f" (lap {lap})"
    return out


def traffic_check(rc: dict, counts: dict | None, lap_map: dict | None = None) -> list:
    """For each lap the analysis suspects of traffic: what the scan recorded on it. It labels, it never changes pace numbers."""
    counts = counts if isinstance(counts, dict) else {}
    laps = set()
    for key in ("traffic", "traffic_possible"):
        for n in counts.get(key) or []:
            if isinstance(n, int):
                laps.add(n)
    out = []
    for yours in sorted(laps):
        n = (lap_map or {}).get(str(yours))
        if n is None:       # this lap couldn't be lined up with the replay: say nothing rather than check the wrong lap
            continue
        seen = [f"fighting {f['other']}" for f in rc["fights"] if (f.get("lap_from") or 0) <= n <= (f.get("lap_to") or 0)]
        seen += [("passing " if p["made"] else "passed by ") + p["other"] for p in rc["passes"] if p["lap"] == n]
        row = next((r for r in rc.get("laps") or [] if r.get("lap") == n), {})
        if not seen and (row.get("near_s") or 0) >= 2 and row.get("near"):
            seen = [f"within a second of {row['near']} for {row['near_s']:.0f} s"]
        if not seen and (row.get("traffic_s") or 0) >= 2 and row.get("traffic"):
            seen = [f"near {row['traffic']}, on a different lap, for {row['traffic_s']:.0f} s"]
        seen = list(dict.fromkeys(seen))[:3]
        out.append({"lap": n, "review_lap": yours, "confirmed": bool(seen), "text": ("The replay shows you " + " and ".join(seen) + ".") if seen
                    else "The replay shows no car close to you on this lap, so a mistake is more likely than traffic."})
    return out


# ---------------------------------------------------------------- rival laps
def rival_laps(race_dir: Path, me_number, limit: int = MAX_RIVALS) -> list:
    """Saved lap traces of other cars, as (lap dict for engine.analyze refs, telemetry CSV text)."""
    best = {}
    try:
        files = sorted((Path(race_dir) / "laps").glob("*.json"))
    except OSError:
        return []
    for f in files:
        try:
            if f.stat().st_size > MAX_TRACE_BYTES:
                continue
            tr = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(tr, dict) or str(tr.get("car")) == str(me_number) or tr.get("mode") not in ("clean", "fastest"):
            continue
        t = tr.get("lap_time")
        if not isinstance(t, (int, float)) or t <= 0:
            continue
        rank = (0 if tr.get("mode") == "clean" else 1, 0 if tr.get("source") == "garage61" else 1)
        if str(tr.get("car")) not in best or rank < best[str(tr.get("car"))][0]:
            best[str(tr.get("car"))] = (rank, tr)
    out = []
    for _rank, tr in sorted(best.values(), key=lambda x: x[1]["lap_time"])[:limit]:
        text = trace_csv(tr)
        if text is None:
            continue
        pedals = bool(tr.get("throttle") and tr.get("brake"))
        how = "from Garage 61" if tr.get("source") == "garage61" else "read from the replay"
        lap = {"id": f"racecap:{tr.get('car')}:{tr.get('mode')}", "lapTime": float(tr["lap_time"]), "clean": True,
               "driver_name": clean_name(tr.get("name")) or f"#{tr.get('car')}", "_main": False, "_rival": True,
               "_role": f"Rival in this race, {how}" + ("" if pedals else " (no pedal data)")}
        out.append((lap, text))
    return out


def trace_csv(tr: dict) -> str | None:
    """A RaceCap trace as the telemetry CSV the engine reads (m/s, 0..1 distance, radians)."""
    dist, speed = tr.get("dist"), tr.get("speed")
    if not isinstance(dist, list) or not isinstance(speed, list) or len(dist) < 10 or len(dist) != len(speed):
        return None
    channels = [("LapDistPct", dist, 1.0), ("Speed", speed, 1 / 3.6)]
    for col, key, k in (("Gear", "gear", 1.0), ("RPM", "rpm", 1.0), ("SteeringWheelAngle", "steer", math.pi / 180),
                        ("Throttle", "throttle", 1.0), ("Brake", "brake", 1.0)):
        v = tr.get(key)
        if isinstance(v, list) and len(v) == len(dist):
            channels.append((col, v, k))
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow([c[0] for c in channels])
    for i in range(len(dist)):
        w.writerow(["" if not isinstance(c[1][i], (int, float)) else f"{c[1][i] * c[2]:.6f}" for c in channels])
    return buf.getvalue()


# ---------------------------------------------------------------- used by the review and the window
def lookup_with(api):
    return lambda sub: api.event_for_subsession(sub)


def prepare(folder, event_id, track, started_at, name, api, subsession=None, log=None):
    """Everything for one review, or (None, why). {"dir", "subsession", "scan", "car", "official"}."""
    d, sub, why = find_race(folder, event_id, track, started_at, lookup_with(api) if api is not None else (lambda _s: None),
                            subsession=subsession, log=log)
    if d is None:
        return None, why
    scan = read_scan(d)
    if scan is None:
        return None, f"RaceCap race {sub} has no scan yet. Scan it in RaceCap first."
    if str(scan.get("subsession")) != str(sub):
        return None, f"The scan in race folder {sub} is for another race, so it wasn't used."
    if not same_track(scan.get("track"), track):
        return None, f"RaceCap race {sub} was at {scan.get('track')}, not this track."
    car = find_me(scan["cars"], name)
    if car is None:
        return None, f"Couldn't find {name} in RaceCap race {sub}."
    return {"dir": d, "subsession": sub, "scan": scan, "car": car, "official": read_official(d, sub)}, None


def attach(data: dict, found: dict) -> None:
    """Put the race log on a review dict: no requests, and the review's own numbers don't change."""
    rc = build(found["scan"], found["car"], found["subsession"], found["official"], data.get("counts"), data, read_samples(found["dir"]))
    data["racecap"] = rc
    meta = data.get("meta")
    if rc.get("start_type") and isinstance(meta, dict):
        meta["start_type"] = rc["start_type"]
        lap1 = (data.get("racecraft") or {}).get("lap1") if isinstance(data.get("racecraft"), dict) else None
        if isinstance(lap1, dict):
            lap1["start_type"] = rc["start_type"]


def link_review(folder, review_dir, cfg_folder, api, subsession=None, log=None) -> dict:
    """Link a saved review (the window's button). Re-renders it; the saved analysis stays as it was."""
    from . import results
    d = Path(review_dir)
    data = json.loads((d / "review.json").read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("That review's data file is damaged.")
    meta = data.get("meta") or {}
    from .library import session_kind
    if session_kind(meta.get("session")) != "race":
        raise ValueError("The RaceCap link is for race reviews. RaceCap's scan covers the race, not practice or qualifying.")
    found, why = prepare(cfg_folder, meta.get("event_id"), meta.get("track"), meta.get("started_at"), meta.get("driver"), api,
                         subsession=subsession, log=log)
    if found is None:
        raise ValueError(why)
    results._rerender(d, lambda x: attach(x, found))
    from . import library
    library.rebuild(folder)
    return {"subsession": found["subsession"], "name": clean_name(found["car"].get("name"))}


def refresh_saved(library_folder, data: dict) -> bool:
    """Rework a review's race log from its saved scan (no requests, no iRacing), so new analysis reaches old reviews."""
    from . import replay_scan
    meta = data.get("meta") or {}
    own = replay_scan.find_for_event(library_folder, meta.get("event_id"))
    if own is None:
        return False
    scan = read_scan(own)
    car = find_me(scan["cars"], meta.get("driver")) if scan else None
    if car is None or read_samples(own) is None:     # never swap a good log for a poorer one
        return False
    attach(data, {"dir": own, "subsession": str(scan.get("subsession")), "scan": scan, "car": car, "official": read_official(own, scan.get("subsession"))})
    return True


def valid_folder(v) -> bool:
    return races_root(v) is not None and os.path.isdir(str(Path(str(v)).expanduser()))
