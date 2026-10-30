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

from . import app, brand, engine, fsutil, library
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
    for d in library.review_summaries(folder):   # row index: no review.json is parsed unless it changed
        track, car = d.get("track"), d.get("car")
        if not track or not car:
            continue
        best = d.get("clean_best")   # None when every lap was flagged or the "best" is a partial lap
        c = out.setdefault((track, car), {"track": track, "car": car, "track_id": None, "car_id": None,
                                          "races": 0, "best": None, "best_date": None, "last": ""})
        c["races"] += 1
        c["track_id"] = c["track_id"] or d.get("track_id")
        c["car_id"] = c["car_id"] or d.get("car_id")
        started = str(d.get("started_at") or "")
        c["last"] = max(c["last"], started)
        if best is not None and (c["best"] is None or best < c["best"]):
            c["best"], c["best_date"] = best, library.local_day(started) or str(d.get("dir") or "")[:10]
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


def _r1(x):
    return None if x is None else round(float(x), 1)


def corner_facts(R, corner) -> dict:
    """How one lap took one corner, relative to the corner's apex (so the numbers survive small
    corner-position differences between laps). From the already-resampled lap: no requests.
    brake_before_apex_m = apex - brake point (None on a lift-only corner),
    full_thr_after_apex_m = full-throttle point - apex."""
    try:
        m = engine.corner_metrics(R, corner)
    except Exception:  # noqa: BLE001  (odd telemetry: no facts for this corner)
        m = None
    if not m:
        return {"brake_before_apex_m": None, "min_mph": None, "exit_mph": None,
                "full_thr_after_apex_m": None, "gear": None}
    apex = float(corner["apex_m"])
    return {"brake_before_apex_m": _r1(apex - m["brake_on"]) if m.get("brake_on") is not None else None,
            "min_mph": _r1(m.get("min_mph")), "exit_mph": _r1(m.get("exit_mph")),
            "full_thr_after_apex_m": _r1(m["full_thr"] - apex) if m.get("full_thr") is not None else None,
            "gear": int(m["gear"]) if m.get("gear") is not None else None}


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
        row["metrics"] = {x["key"]: corner_facts(x["R"], by_id[cid]) for x in parsed}
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


# ---------------------------------------------------------------- membership (2.6)
def membership_for(api, slug=None, log=print) -> tuple[str, list | None]:
    """(brand.MEMBER / NOT_MEMBER / UNKNOWN, the teams list or None) from api.teams(), which the client
    caches for a day. UNKNOWN when the list couldn't be loaded: callers then carry on as before."""
    slug = slug or brand.TEAM_SLUG
    try:
        teams = api.teams()
    except Exception as e:  # noqa: BLE001  (unknown membership never stops a page)
        try:
            log(f"  Couldn't check your Garage 61 teams: {e}")
        except Exception:  # noqa: BLE001
            pass
        return brand.UNKNOWN, None
    if teams is None:
        return brand.UNKNOWN, None
    teams = [t for t in teams if isinstance(t, dict)]
    return brand.membership(teams, slug), teams


def team_info(slug=None, teams=None) -> dict:
    """{"slug", "name"} for a data dict: the display name, never the bare slug."""
    slug = slug or brand.TEAM_SLUG
    return {"slug": slug, "name": brand.team_name_for(teams, slug)}


# ---------------------------------------------------------------- build
def build(api, folder, team_slug, log=print, max_combos=MAX_COMBOS) -> dict:
    max_combos = max(1, min(MAX_COMBOS, int(max_combos)))
    start_req = api.requests_made
    notes = []
    me = api.me() or {}
    my_slug = g(me, "slug", default="") or ""
    my_name = " ".join(filter(None, [g(me, "firstName", "firstname"), g(me, "lastName", "lastname")])) or my_slug or "You"
    team_slug = team_slug or brand.TEAM_SLUG
    mem, teams_list = membership_for(api, team_slug, log)
    team_name = brand.team_name_for(teams_list, team_slug)
    not_member = mem == brand.NOT_MEMBER
    if not_member:
        log(f"You're not on the {team_name} team on Garage 61, so no team laps were loaded.")

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
        # not on the team: zero team-lap requests, only your own laps (your board row and telemetry)
        team_laps = [] if not_member else api.find_laps(tracks=[tid], cars=[cid], teams=[team_slug],
                                                         group="driver", age=-2, limit=100)
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
            entry["note"] = brand.JOIN_TITLE + "." if not_member else "No teammate laps here yet."
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
        try:
            entry["learn"] = learn_for_combo(entry)
        except Exception:  # noqa: BLE001  (a hint is never worth breaking the hub)
            entry["learn"] = []
    data = {
        "version": 1,
        "team": {"slug": team_slug, "name": team_name},
        "membership": mem,
        "me": {"name": my_name, "key": "me"},
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "combos": out_combos,
        "h2h": head_to_heads(out_combos),
        "requests": api.requests_made - start_req,
        "notes": notes,
    }
    try:
        from . import leaderboards
        data["weekly"] = leaderboards.view(folder)   # saved snapshots only: no requests
    except Exception:  # noqa: BLE001
        pass
    log(f"Team hub ready. Garage 61 requests used: {data['requests']}.")
    return data


def _embed(obj) -> str:
    from . import report
    return report._embed(obj)  # escapes <, > and & so names can't break the script element


def write(folder, data) -> Path:
    from . import report
    out = Path(folder) / "team"
    out.mkdir(parents=True, exist_ok=True)
    tj = out / "team.json"
    tmpj = tj.with_name("team.json.tmp")
    tmpj.write_text(json.dumps(data), encoding="utf-8")
    fsutil.replace(tmpj, tj)
    html = report.fill(TPL.read_text(encoding="utf-8"), {"/*TEAM*/null": _embed(data)})
    page = out / "index.html"
    tmp = page.with_name("index.html.tmp")
    tmp.write_text(html, encoding="utf-8")
    fsutil.replace(tmp, page)
    return page


# ---------------------------------------------------------------- who to learn from (2.5, no requests)
LEARN_GATE_S = 0.05        # a teammate must be at least this much quicker through the corner
SIMILAR_PACE_PCT = 1.5     # "similar pace": their full best lap within 1.5 % of yours
MAX_LEARN = 5
HOW_MIN = {"brake_before_apex_m": 5.0, "min_mph": 1.5, "full_thr_after_apex_m": 10.0}
BASIS_NOTE = "Both are best laps: this compares your best driving, not your typical lap."


def _first(name) -> str:
    s = str(name or "").strip()
    return s.split()[0] if s else "Your teammate"


def _ordinal(n) -> str:
    n = int(n)
    suf = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"


def _about(x, step=5) -> int:
    return max(step, int(round(abs(x) / step) * step))


def _how(mine: dict, theirs: dict) -> dict:
    """Facts whose difference passes the thresholds. Each value is [you, them]."""
    out = {}
    for k, gate in HOW_MIN.items():
        a, b = (mine or {}).get(k), (theirs or {}).get(k)
        if a is not None and b is not None and abs(a - b) >= gate:
            out[k] = [a, b]
    a, b = (mine or {}).get("gear"), (theirs or {}).get("gear")
    if a is not None and b is not None and a != b:
        out["gear"] = [a, b]
    return out


def _how_text(how: dict) -> str | None:
    bits = []
    if "brake_before_apex_m" in how:
        you, them = how["brake_before_apex_m"]
        bits.append(f"braked about {_about(you - them)} m {'later' if them < you else 'earlier'}")
    if "min_mph" in how:
        you, them = how["min_mph"]
        d = round(abs(them - you))
        bits.append(f"carried about {max(1, d)} mph {'more' if them > you else 'less'} through the slowest point")
    if "full_thr_after_apex_m" in how:
        you, them = how["full_thr_after_apex_m"]
        bits.append(f"were back on full throttle about {_about(you - them)} m "
                    f"{'sooner' if them < you else 'later'} after the apex")
    if "gear" in how:
        you, them = how["gear"]
        bits.append(f"were in {_ordinal(them)} gear at the slowest point (you were in {_ordinal(you)})")
    if not bits:
        return None
    return bits[0] if len(bits) == 1 else ", ".join(bits[:-1]) + " and " + bits[-1]


def _lap_t(combo, kings, key):
    for r in combo.get("board") or []:
        if r.get("key") == key and _num(r.get("t")):
            return float(r["t"])
    ts = [z.get("times", {}).get(key) for z in kings.get("corners") or []]
    return float(sum(ts)) if ts and all(isinstance(t, (int, float)) for t in ts) else None


def _learn_rows(combo: dict, pairs: list) -> list:
    """pairs: [(name shown, apex_m, kings corner row)]."""
    kings = combo.get("kings") or {}
    if "me" not in (kings.get("drivers") or []):
        return []
    names = kings.get("names") or {}
    my_lap = _lap_t(combo, kings, "me")
    out = []
    for label, apex, kc in pairs:
        times = kc.get("times") or {}
        me_t = times.get("me")
        if not isinstance(me_t, (int, float)):
            continue
        cands = []
        for k, t in times.items():
            if k == "me" or not isinstance(t, (int, float)) or t > me_t - LEARN_GATE_S + 1e-9:
                continue
            their_lap = _lap_t(combo, kings, k)
            lap_gap = round((my_lap / their_lap - 1) * 100, 2) if my_lap and their_lap else None
            cands.append({"key": k, "gap": round(me_t - t, 3), "t": t, "lap_gap_pct": lap_gap,
                          "similar": lap_gap is not None and abs(lap_gap) <= SIMILAR_PACE_PCT})
        if not cands:
            continue
        cands.sort(key=lambda c: (-c["gap"], c["key"]))
        similar = [c for c in cands if c["similar"]]
        primary = similar[0] if similar else cands[0]
        rest = [c for c in cands if c is not primary]
        alt = rest[0] if rest else None
        metrics = kc.get("metrics") or {}
        how = _how(metrics.get("me"), metrics.get(primary["key"]))
        name = names.get(primary["key"]) or primary["key"]
        text = f"{_first(name)}'s best lap was {primary['gap']:.2f} s quicker than yours through {label}."
        facts = _how_text(how)
        if facts:
            text += f" On that lap they {facts}."
        text += " " + BASIS_NOTE
        out.append({"corner": label, "apex_m": round(float(apex), 1) if apex is not None else None,
                    "team_corner": kc.get("id"),
                    "teammate": {"key": primary["key"], "name": name},
                    "gap_s": primary["gap"], "your_t": round(float(me_t), 3), "their_t": round(float(primary["t"]), 3),
                    "basis": "best laps", "similar_pace": primary["similar"], "lap_gap_pct": primary["lap_gap_pct"],
                    "how": how, "text": text,
                    "alt": ({"key": alt["key"], "name": names.get(alt["key"]) or alt["key"], "gap_s": alt["gap"]}
                            if alt else None)})
    out.sort(key=lambda r: -r["gap_s"])
    return out[:MAX_LEARN]


def learn_for_combo(combo: dict) -> list:
    """Who to learn from on a team combo, by the team's own corner names (team.json per combo `learn`)."""
    kings = (combo or {}).get("kings") or {}
    return _learn_rows(combo, [(kc.get("id"), kc.get("apex_m"), kc) for kc in kings.get("corners") or []])


def _combo_for(team_data: dict, review: dict):
    m = (review or {}).get("meta") or {}
    ids = review.get("combo_ids") if isinstance(review.get("combo_ids"), dict) else {}
    want = {"track": m.get("track"), "car": m.get("car"), "track_id": ids.get("track_id"), "car_id": ids.get("car_id")}
    if not want["track"] or not want["car"]:
        return None
    for c in (team_data or {}).get("combos") or []:
        if isinstance(c, dict) and library.same_combo(want, c):
            return c
    return None


def learn_from(team_data: dict, review: dict) -> list[dict]:
    """Corners where a teammate's best lap was quicker than yours, matched to the review's corners by apex
    (spec 0.4: prep.match_corners). Pure: no requests. [] when there's nothing to compare."""
    from . import prep
    if not isinstance(team_data, dict) or not isinstance(review, dict):
        return []
    combo = _combo_for(team_data, review)
    if not combo:
        return []
    kings = combo.get("kings") or {}
    if "me" not in (kings.get("drivers") or []):
        return []
    mine = [c for c in review.get("corners") or [] if isinstance(c, dict) and _num(c.get("apex_m")) is not None]
    theirs = [c for c in kings.get("corners") or [] if isinstance(c, dict) and _num(c.get("apex_m")) is not None]
    if not mine or not theirs:
        return []
    L = _num(review.get("L")) or _num(kings.get("L"))
    canon = prep.match_corners([(0, mine), (1, theirs)], L)
    pairs = [(k["per"][0].get("id"), k["per"][0].get("apex_m"), k["per"][1]) for k in canon if 0 in k["per"] and 1 in k["per"]]
    return _learn_rows(combo, pairs)


def learn_from_saved(library_folder, review: dict) -> list[dict]:
    """learn_from against <library>/team/team.json. No requests; [] when there's no team page yet."""
    try:
        data = json.loads((Path(library_folder) / "team" / "team.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    try:
        return learn_from(data, review)
    except Exception:  # noqa: BLE001  (an odd team.json never stops a review)
        return []


def render_saved(library_folder) -> Path:
    """Rebuild the team page from team.json plus the saved weekly snapshots. No requests."""
    from . import leaderboards
    folder = Path(library_folder)
    try:
        data = json.loads((folder / "team" / "team.json").read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError
    except (OSError, ValueError):
        data = {"version": 1, "team": team_info(), "membership": brand.UNKNOWN, "me": {"name": "You", "key": "me"},
                "generated_at": None, "combos": [], "h2h": [], "requests": 0, "notes": []}
    data.setdefault("membership", brand.UNKNOWN)
    tm = data.get("team") if isinstance(data.get("team"), dict) else {}
    if not tm.get("slug") or not tm.get("name") or tm.get("name") == tm.get("slug"):
        data["team"] = team_info(tm.get("slug") or None, None)
    for c in data.get("combos") or []:
        if isinstance(c, dict) and "learn" not in c:
            try:
                c["learn"] = learn_for_combo(c)
            except Exception:  # noqa: BLE001
                c["learn"] = []
    data["weekly"] = leaderboards.view(folder)
    wk = data["weekly"] if isinstance(data["weekly"], dict) else {}
    if wk.get("membership") in (brand.MEMBER, brand.NOT_MEMBER):   # the newest check wins
        data["membership"] = wk["membership"]
        if isinstance(wk.get("team"), dict):
            data["team"] = wk["team"]
    return write(folder, data)
