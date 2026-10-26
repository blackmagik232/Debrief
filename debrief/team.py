"""Team hub: leaderboards, corner kings and head-to-heads with your Garage 61 team (Midfield Menace).

Only runs when you click "Refresh" in the Team view (or run `python -m debrief team`).

Garage 61 request budget (worst case, cold cache, max_combos = 6):
    me()                       1   (cached for a day)
    teams()                    1   (cached for a day; gives the team's display name)
    tracks() + cars()          2   only if an old review has no stored ids and the lists were never cached
    per track/car (up to 6):
        teammates' best laps   1   find_laps(teams=[slug], group="driver")      cached 10 min
        your laps there        1   find_laps(drivers=["me"])  (finds your best lap's id)  cached 10 min
        teammates' full laps   1   only if a teammate's "best" came back partial/unclean   cached 10 min
        your telemetry        <=2  your fastest lap; one retry if it has none   cached forever
        teammates' telemetry  <=4  MAX_TEL_DRIVERS - 1 fastest teammates        cached forever
    => 4 + 6 * 9 = 58 requests at most. A second refresh within 10 minutes costs 0; later refreshes
       cost about 2 per track/car plus telemetry only for laps that are new since last time.
    (Retries are on top of this: the Garage 61 client retries a request after a 429, a 5xx or a
     network error, up to its 5 attempts in all, so a flaky connection can add requests, bounded by 5x.)

Sign conventions (tested in tests/test_desktop.py):
    leaderboard gap        = driver's best - P1's best           (always >= 0)
    corner margin          = second fastest - king               (>= 0, the king's advantage)
    corner your_gap        = your time - king's time             (positive = you were slower)
    head-to-head gap       = your best - teammate's best         (negative = you are quicker)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from . import app, engine, fsutil, library
from .engine import g

TPL = Path(__file__).resolve().parent / "templates" / "team.html"
MAX_TEL_DRIVERS = 5
MAX_COMBOS = 6
_BAD_FLAGS = ("incomplete", "missing", "pitlane", "pitIn", "pitOut", "discontinuity")


# ---------------------------------------------------------------- library side (no requests)
def _num(x):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return x if np.isfinite(x) else None


def library_combos(folder) -> list[dict]:
    """Track/car combos from the review library, newest race first, with your best lap at each."""
    out = {}
    for p in (Path(folder) / "reviews").glob("*/review.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(d, dict):
            continue
        m = d.get("meta") or {}
        track, car = m.get("track"), m.get("car")
        if not track or not car:
            continue
        best = library.clean_best(d)   # None when every lap was flagged or the "best" is a partial lap
        ids = d.get("combo_ids") if isinstance(d.get("combo_ids"), dict) else {}
        c = out.setdefault((track, car), {"track": track, "car": car, "track_id": None, "car_id": None,
                                          "races": 0, "best": None, "best_date": None, "last": ""})
        c["races"] += 1
        c["track_id"] = c["track_id"] or ids.get("track_id")
        c["car_id"] = c["car_id"] or ids.get("car_id")
        started = str(m.get("started_at") or "")
        c["last"] = max(c["last"], started)
        if best is not None and (c["best"] is None or best < c["best"]):
            c["best"], c["best_date"] = best, library.local_day(started) or p.parent.name[:10]
    return sorted(out.values(), key=lambda c: c["last"], reverse=True)


# ---------------------------------------------------------------- helpers
def driver_of(lap) -> dict:
    d = g(lap, "driver", default={}) or {}
    name = " ".join(filter(None, [g(d, "firstName", "firstname"), g(d, "lastName", "lastname")])).strip()
    return {"slug": g(d, "slug", default="") or "", "name": name or g(d, "slug", default="Teammate")}


def usable(lap) -> bool:
    """A complete, clean, timed lap (no pit, tow or partial laps)."""
    t = app.lap_time(lap)
    if not t or t <= 0 or not app.is_clean(lap):
        return False
    return not any(g(lap, k) for k in _BAD_FLAGS)


def anchor_for(times, my_best=None):
    """Typical full-lap time to judge laps against: the median of the candidate laps plus your best
    (when you have one). Anchoring on the median rather than your best keeps much quicker teammates in."""
    xs = [float(t) for t in times if t and t > 0] + ([float(my_best)] if my_best else [])
    return float(np.median(xs)) if xs else None


def plausible(t, anchor) -> bool:
    """Drop laps that can't be a real full lap compared with the anchor time (e.g. a 11 s partial lap).
    0.6 x anchor still keeps a teammate 30%+ quicker than the typical lap."""
    return anchor is None or 0.6 * anchor <= t <= 1.5 * anchor


def lap_day(lap):
    return library.local_day(g(lap, "startTime", "start_time")) or None


def choose_length(raws_and_times, track_length):
    """Lap length in metres, the same way engine.analyze picks it: median telemetry estimate,
    snapped to Garage 61's track length when that is within 10% (km, m or miles)."""
    est = float(np.median([engine.lap_length_from(r, t) for r, t in raws_and_times]))
    if track_length:
        for mult in (1.0, 1000.0, 1609.344):
            cand = float(track_length) * mult
            if est and abs(cand - est) / est < 0.10:
                return cand
    return est


def corner_kings(entries, track_length=None):
    """entries: [{"key", "name", "t", "csv", "is_me"}]. Corners come from the fastest lap
    (engine.detect_corners after engine.resample); each lap is split with engine.build_segments,
    the same segment logic as the race review, so segment times add up to the lap time."""
    parsed = []
    for e in entries:
        try:
            raw = engine.parse_csv(e["csv"])
        except (ValueError, IndexError):
            continue
        if len(raw["v"]) <= 100:
            continue
        parsed.append({**e, "raw": raw})
    if len(parsed) < 2:
        return None
    L = choose_length([(x["raw"], x["t"]) for x in parsed], track_length)
    for x in parsed:
        x["R"] = engine.resample(x["raw"], x["t"], L)
    fastest = min(parsed, key=lambda x: x["t"])
    corners = engine.detect_corners(fastest["R"])
    if not corners:
        return None
    laps = [{"n": i + 1, "t": x["t"], "R": x["R"], "clean": True} for i, x in enumerate(parsed)]
    seg = engine.build_segments(corners, laps, [], L)
    if not seg:
        return None
    by_id = {c["id"]: c for c in corners}
    me = next((x for x in parsed if x.get("is_me")), None)
    out = []
    for j, cid in enumerate(seg["names"]):
        times = {x["key"]: float(seg["laps"][str(i + 1)][j]) for i, x in enumerate(parsed)}
        order = sorted(times, key=lambda k: times[k])
        king, second = order[0], order[1]
        row = {"id": cid, "apex_m": round(by_id[cid]["apex_m"], 1),
               "from_m": round(seg["cuts"][j], 1), "to_m": round(seg["cuts"][j + 1], 1),
               "king": king, "king_t": round(times[king], 3),
               "second": second, "margin": round(times[second] - times[king], 3),  # >= 0
               "times": {k: round(v, 3) for k, v in times.items()},
               "your_gap": None}
        if me is not None:
            row["your_gap"] = round(times[me["key"]] - times[king], 3)  # positive = you were slower
        out.append(row)
    names = {x["key"]: x["name"] for x in parsed}
    wins = {}
    for r in out:
        wins[r["king"]] = wins.get(r["king"], 0) + 1
    return {"L": round(L, 1), "fastest": fastest["key"], "drivers": [x["key"] for x in parsed],
            "names": names, "corners": out, "wins": wins}


def head_to_heads(combos, my_key="me"):
    """Your best vs each teammate's best on every combo you both have a time at.
    gap = your best - their best (negative = you are quicker)."""
    people = {}
    for c in combos:
        board = {r["key"]: r for r in c["board"]}
        mine = board.get(my_key)
        if not mine:
            continue
        kings = c.get("kings") or {}
        for k, r in board.items():
            if k == my_key:
                continue
            p = people.setdefault(k, {"key": k, "name": r["name"], "combos": [], "ahead": 0, "behind": 0,
                                      "corners_you": 0, "corners_them": 0})
            gap = round(mine["t"] - r["t"], 3)
            entry = {"track": c["track"], "track_short": c["track_short"], "car": c["car"],
                     "you": mine["t"], "them": r["t"], "gap": gap, "corners": None}
            if gap < 0:
                p["ahead"] += 1
            elif gap > 0:
                p["behind"] += 1
            if kings and my_key in kings.get("drivers", []) and k in kings["drivers"]:
                yw = sum(1 for z in kings["corners"] if z["times"][my_key] < z["times"][k])
                tw = sum(1 for z in kings["corners"] if z["times"][k] < z["times"][my_key])
                entry["corners"] = {"you": yw, "them": tw}
                p["corners_you"] += yw
                p["corners_them"] += tw
            p["combos"].append(entry)
    out = []
    for p in people.values():
        gaps = [e["gap"] for e in p["combos"]]
        p["avg_gap"] = round(float(np.mean(gaps)), 3) if gaps else None
        p["shared"] = len(gaps)
        out.append(p)
    out.sort(key=lambda p: (-p["shared"], p["name"]))
    return out


# ---------------------------------------------------------------- build
def build(api, folder, team_slug, log=print, max_combos=MAX_COMBOS) -> dict:
    max_combos = max(1, min(MAX_COMBOS, int(max_combos)))
    start_req = api.requests_made
    notes = []
    me = api.me() or {}
    my_slug = g(me, "slug", default="") or ""
    my_name = " ".join(filter(None, [g(me, "firstName", "firstname"), g(me, "lastName", "lastname")])) or my_slug or "You"
    team_name = team_slug
    for t in api.teams():
        if g(t, "slug") == team_slug:
            team_name = g(t, "name", default=team_slug)
            break
    else:
        notes.append(f"Your Garage 61 account doesn't list a team called \"{team_slug}\". "
                     "Pick your team in Settings.")

    combos_lib = library_combos(folder)
    if not combos_lib:
        log("No reviews in your library yet, so there are no tracks to compare.")
    picked = combos_lib[:max_combos]
    if len(combos_lib) > max_combos:
        notes.append(f"Showing your {max_combos} most recent track and car combinations "
                     f"({len(combos_lib) - max_combos} older ones left out to keep Garage 61 traffic light).")
    lists = {}

    def ids_for(c):
        tid, cid = c["track_id"], c["car_id"]
        if tid is None:
            lists.setdefault("tracks", api.tracks())   # cached 7 days
            tid = app.id_for_name(lists["tracks"], c["track"])
        if cid is None:
            lists.setdefault("cars", api.cars())
            cid = app.id_for_name(lists["cars"], c["car"])
        return tid, cid

    track_items = api.cached_items("tracks") or []
    out_combos = []
    for i, c in enumerate(picked):
        short = library.short_track_name(c["track"])
        log(f"{short} · {c['car']}: loading teammates' best laps ({i + 1} of {len(picked)})...")
        tid, cid = ids_for(c)
        entry = {"track": c["track"], "track_short": short, "car": c["car"], "track_id": tid, "car_id": cid,
                 "races": c["races"], "last_date": library.local_day(c["last"]) or None,
                 "board": [], "kings": None, "note": None}
        out_combos.append(entry)
        if tid is None or cid is None:
            entry["note"] = "Couldn't match this track or car to Garage 61, so it was skipped."
            continue
        team_laps = api.find_laps(tracks=[tid], cars=[cid], teams=[team_slug], group="driver", age=-2, limit=100)
        my_laps = api.find_laps(tracks=[tid], cars=[cid], drivers=["me"], group="none", age=-2, limit=1000)
        anchor = anchor_for([app.lap_time(l) for l in my_laps if usable(l)], c["best"])
        my_ok = sorted((l for l in my_laps if usable(l) and plausible(app.lap_time(l), anchor)), key=app.lap_time)
        my_api = my_ok[0] if my_ok else None
        my_best = c["best"] or (app.lap_time(my_api) if my_api else None)
        if any(not usable(l) for l in team_laps if driver_of(l)["slug"] not in ("", my_slug)):
            # someone's "best" was a partial or unclean lap: ask once for all their laps instead (1 request)
            team_laps = team_laps + api.find_laps(tracks=[tid], cars=[cid], teams=[team_slug], group="none",
                                                  age=-2, limit=1000)
        anchor = anchor_for([app.lap_time(l) for l in team_laps
                             if usable(l) and driver_of(l)["slug"] not in ("", my_slug)], my_best)
        mates = {}
        for l in team_laps:
            d = driver_of(l)
            if not d["slug"] or d["slug"] == my_slug or not usable(l):
                continue
            t = app.lap_time(l)
            if not plausible(t, anchor) or (d["slug"] in mates and mates[d["slug"]]["t"] <= t):
                continue
            mates[d["slug"]] = {"key": d["slug"], "name": d["name"], "t": t, "is_me": False,
                                "date": lap_day(l), "irating": g(l, "driverRating"), "_lap": g(l, "id")}
        # your best: the quicker of your reviewed races and anything on Garage 61 (practice counts too)
        mine = None
        cands = []
        if c["best"] is not None:
            cands.append((c["best"], "your reviewed races", c["best_date"]))
        if my_api is not None:
            cands.append((app.lap_time(my_api), "Garage 61", lap_day(my_api)))
        if cands:
            t, src, day = min(cands, key=lambda z: z[0])
            mine = {"key": "me", "name": my_name, "t": t, "is_me": True, "date": day, "source": src,
                    "irating": g(my_api, "driverRating") if my_api else None}
        board = sorted(list(mates.values()) + ([mine] if mine else []), key=lambda r: r["t"])
        p1 = board[0]["t"] if board else None
        for pos, r in enumerate(board, 1):
            r["pos"] = pos
            r["gap"] = round(r["t"] - p1, 3)  # always >= 0
        if not mates:
            entry["note"] = "No teammate laps here yet."
        # corner kings: one lap of yours + the fastest teammates, MAX_TEL_DRIVERS in all
        entries = []
        mates_sorted = [r for r in board if not r["is_me"]][:MAX_TEL_DRIVERS - (1 if my_ok else 0)]
        if mates_sorted and (my_ok or len(mates_sorted) >= 2):
            log(f"  Telemetry for up to {len(mates_sorted) + (1 if my_ok else 0)} best laps (laps saved earlier are reused)...")
            # your lap: the fastest one, else one already saved, else the next fastest (<= 2 requests)
            top = my_ok[:8]
            saved = [l for l in top[1:] if api.has_csv(g(l, "id"))]
            tries = (top[:1] + saved[:1] + [l for l in top[1:] if l not in saved][:1]) if top else []
            asked = 0
            for l in tries:
                if not api.has_csv(g(l, "id")):
                    if asked >= 2:
                        continue
                    asked += 1
                csv = api.lap_csv(g(l, "id"))
                if csv:
                    entries.append({"key": "me", "name": my_name, "t": app.lap_time(l), "csv": csv, "is_me": True})
                    entry["my_tel_lap"] = {"t": app.lap_time(l), "date": lap_day(l)}
                    break
            for r in mates_sorted:
                csv = api.lap_csv(r["_lap"])
                if csv:
                    entries.append({"key": r["key"], "name": r["name"], "t": r["t"], "csv": csv, "is_me": False})
        tl = next((g(it, "lap_length", "lapLength", "length") for it in track_items if g(it, "id") == tid), None)
        try:
            entry["kings"] = corner_kings(entries, tl)
        except Exception:  # noqa: BLE001  (one odd telemetry file shouldn't stop the hub)
            import traceback
            log("  Couldn't work out corner kings here:\n" + traceback.format_exc())
        if entry["kings"] is None and mates_sorted:
            entry["kings_note"] = "Not enough telemetry from these laps to compare corners."
        for r in board:
            r.pop("_lap", None)
        entry["board"] = board
    data = {
        "version": 1,
        "team": {"slug": team_slug, "name": team_name},
        "me": {"name": my_name, "key": "me"},
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "combos": out_combos,
        "h2h": head_to_heads(out_combos),
        "requests": api.requests_made - start_req,
        "notes": notes,
    }
    log(f"Team hub ready. Garage 61 requests used: {data['requests']}.")
    return data


def _embed(obj) -> str:
    from . import report
    return report._embed(obj)  # escapes <, > and & so names can't break the script element


def write(folder, data) -> Path:
    from . import report
    out = Path(folder) / "team"
    out.mkdir(parents=True, exist_ok=True)
    (out / "team.json").write_text(json.dumps(data), encoding="utf-8")
    html = report.fill(TPL.read_text(encoding="utf-8"), {"/*TEAM*/null": _embed(data)})
    page = out / "index.html"
    tmp = page.with_name("index.html.tmp")
    tmp.write_text(html, encoding="utf-8")
    fsutil.replace(tmp, page)
    return page
