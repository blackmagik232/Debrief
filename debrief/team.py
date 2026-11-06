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
from datetime import datetime, timedelta, timezone
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


# 2.18: the skipped note says what to do next
NO_MATCH_NOTE = ("Couldn't match this track or car to Garage 61, so it was skipped. Garage 61 may name it differently "
                 "or not list it yet. Next step: review your next race here with Review last race (a new review "
                 "usually keeps Garage 61's own track and car numbers), then press Refresh team data again. "
                 "Your own reviews of this track still work.")


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


def plausible(t, anchor, slow=1.5) -> bool:
    """Drop laps that can't be a real full lap compared with the anchor time (e.g. a 11 s partial lap).
    0.6 x anchor still keeps a teammate 30%+ quicker than the typical lap. 2.20.1: the teammate lists pass slow=2.5 so a
    teammate who is simply slower is never dropped for pace; only clearly broken laps go."""
    return anchor is None or 0.6 * anchor <= t <= slow * anchor


# ---------------------------------------------------------------- 2.20.1: no teammate vanishes silently
def team_roster(teams, slug) -> dict:
    """{driver slug: display name} of the team's members when Garage 61's team list names them, else {}.
    No extra request: it only reads the list Debrief already has."""
    out = {}
    for t in teams or []:
        if not isinstance(t, dict) or str(g(t, "slug", "id")) != str(slug):
            continue
        for m in t.get("members") or []:
            if not isinstance(m, dict):
                continue
            d = m.get("user") if isinstance(m.get("user"), dict) else m
            d = d.get("driver") if isinstance(d.get("driver"), dict) else d
            sl = g(d, "slug")
            if sl:
                out[str(sl)] = driver_of({"driver": d})["name"]
    return out


def norm_name(s) -> str:
    """A name for matching: no accents, no case, no spaces or dashes ("Kenta Young" == "kenta-young" == "KENTÁ YOUNG")."""
    import unicodedata
    t = unicodedata.normalize("NFKD", str(s or ""))
    return "".join(ch for ch in t if ch.isalnum() and not unicodedata.combining(ch)).lower()


def match_members(members, drivers, me_slug="", me_name="") -> tuple[dict, list]:
    """Match the team's member names (Settings) to Garage 61 drivers by name or short name, ignoring case and
    accents. drivers = {slug: display name}. Returns ({member: slug, "me" for you}, [members nobody matched])."""
    found, left = {}, []
    for m in members or []:
        k = norm_name(m)
        if k and (k == norm_name(me_name) or k == norm_name(me_slug)):
            found[m] = "me"
            continue
        hit = next((sl for sl, nm in drivers.items() if k and k in (norm_name(nm), norm_name(sl))), None)
        if hit is None:
            left.append(m)
        else:
            found[m] = hit
    return found, left


def _lt(t) -> str:
    t = float(t)
    return f"{int(t // 60)}:{t % 60:06.3f}"


def gather_mates(first, fetch_more, my_slug, my_best, roster=None, members=None, my_name="", group="", log=None):
    """Best usable lap per driver in the group. first = the group=driver answer (one best lap per driver);
    fetch_more() = ONE request for all their laps, made only when someone's best was rejected or a roster member has
    no row. members = the team's names from Settings (2.21): a member with no row is named with a reason, and every
    mate carries "member" True/False (all True when there are no members). Returns (mates {slug: {"slug","name","t",
    "lap","member"}}, reasons [{"name","text"}]): everyone left out or shown with a substitute lap gets a reason."""
    roster = {k: v for k, v in (roster or {}).items() if k and k != my_slug}
    members = list(members or [])
    laps = list(first or [])
    firsts = {}
    for l in laps:
        d = driver_of(l)
        if d["slug"] and d["slug"] != my_slug:
            firsts.setdefault(d["slug"], l)
    _, want = match_members(members, {**roster, **{k: driver_of(v)["name"] for k, v in firsts.items()}}, my_slug, my_name)
    failed = False
    if fetch_more is not None and (any(not usable(l) for l in firsts.values()) or any(k not in firsts for k in roster)
                                   or want):
        try:
            have = {g(l, "id") for l in laps if g(l, "id") is not None}
            laps += [l for l in (fetch_more() or []) if g(l, "id") is None or g(l, "id") not in have]
        except Exception as e:  # noqa: BLE001
            failed = True
            if log:
                log(f"  Couldn't load everyone's laps: {type(e).__name__}: {e}")
    by, names, unnamed = {}, dict(roster), 0
    for l in laps:
        d = driver_of(l)
        if not d["slug"]:
            unnamed += 1
            continue
        if d["slug"] == my_slug:
            continue
        by.setdefault(d["slug"], []).append(l)
        names[d["slug"]] = d["name"]
    matched, missing = match_members(members, names, my_slug, my_name)
    member_slugs = {v for v in matched.values() if v != "me"}
    if log:   # the raw counts behind every named person, so the log shows why someone is missing
        for sl, ls in by.items():
            flags = sorted({k for l in ls for k in _BAD_FLAGS if g(l, k)})
            log(f"  Garage 61 rows for {names[sl]}: {len(ls)} returned, {sum(1 for l in ls if usable(l))} usable"
                + (f", flags seen: {', '.join(flags)}" if flags else ""))
        for m in missing:
            if _DISCOVERED and norm_name(m) not in _DISCOVERED:
                log(f"  Garage 61 rows for {m}: 0 returned at this track (no laps from this name in the recent-tracks search)")
            else:
                log(f"  Garage 61 rows for {m}: 0 laps at this track and car")
    bests = [min(app.lap_time(l) for l in ls if usable(l)) for ls in by.values() if any(usable(l) for l in ls)]
    anchor = anchor_for(bests, my_best)
    mates, reasons = {}, []
    for sl, ls in by.items():
        nm = names[sl]
        good = sorted((l for l in ls if usable(l)), key=app.lap_time)
        fit = [l for l in good if plausible(app.lap_time(l), anchor, 2.5)]
        if not good:
            reasons.append({"name": nm, "text": "best lap was partial or unclean, and none of their laps here were clean"})
            continue
        if not fit:
            reasons.append({"name": nm, "text": f"quickest lap ({_lt(app.lap_time(good[0]))}) looked broken "
                            "(far from a normal lap), so it was left out"})
            continue
        pick = fit[0]
        mates[sl] = {"slug": sl, "name": nm, "t": app.lap_time(pick), "lap": pick,
                     "member": (sl in member_slugs) if members else True}
        f = firsts.get(sl)
        if f is not None and f is not pick and (not usable(f) or not plausible(app.lap_time(f), anchor, 2.5)):
            reasons.append({"name": nm, "text": "best lap was partial or unclean, used their best clean lap instead"})
    where = f"in {group}" if group else "in the group"
    for m in missing:
        reasons.append({"name": m, "text": ("laps couldn't be loaded this time. Try Refresh again." if failed else
                        f"no laps came back from Garage 61 {where} at this track (in any car of this class). If they have driven it, "
                        "check their laps are shared with the group in Garage 61."), "member": True})
    named = {norm_name(r["name"]) for r in reasons}
    for sl, nm in roster.items():
        if sl not in by and norm_name(nm) not in named:
            reasons.append({"name": nm, "text": "is on your team but Garage 61 returned no laps for them. Ask them to "
                            "share laps and telemetry with the team in Garage 61." if not failed else
                            "laps couldn't be loaded this time. Try again."})
    if unnamed:
        reasons.append({"name": "A teammate", "text": f"Garage 61 sent {unnamed} lap(s) with no driver name, so "
                        "they couldn't be matched to anyone and were left out"})
    return mates, reasons


def reason_text(r) -> str:
    n, t = str(r.get("name") or "A teammate"), str(r.get("text") or "")
    return f"{n} {t}" if t.startswith("is on your team") else f"{n}: {t}"


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
            if k == my_key or (r.get("class") != mine.get("class") and r.get("class") and mine.get("class")):
                continue
            p = people.setdefault(k, {"key": k, "name": r["name"], "combos": [], "ahead": 0, "behind": 0,
                                      "corners_you": 0, "corners_them": 0})
            gap = round(mine["t"] - r["t"], 3)
            entry = {"track": c["track"], "track_short": c["track_short"], "car": c["car"],
                     "you": mine["t"], "them": r["t"], "gap": gap, "corners": None,
                     "them_car": r.get("car"), "diff_car": bool(r.get("diff_car"))}
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
def can_refresh_teams(api) -> bool:
    """The client's teams() takes refresh= (the real one does; some stand-ins don't)."""
    import inspect
    try:
        return "refresh" in inspect.signature(api.teams).parameters
    except (AttributeError, TypeError, ValueError):
        return False


def membership_for(api, slug=None, log=print) -> tuple[str, list | None]:
    """(brand.MEMBER / NOT_MEMBER / UNKNOWN, the teams list or None) from api.teams(), which the client
    caches for a day. UNKNOWN when the list couldn't be loaded: callers then carry on as before."""
    slug = slug or brand.GROUP_SLUG
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
    status = brand.membership(teams, slug)
    if status == brand.NOT_MEMBER and can_refresh_teams(api):
        # the list is cached for a day: someone who just joined would keep seeing the join panel, so a
        # "not a member" answer is checked once more against Garage 61 (1 request) before it's believed
        try:
            fresh = api.teams(refresh=True)
        except Exception as e:  # noqa: BLE001
            try:
                log(f"  Couldn't re-check your Garage 61 teams: {e}")
            except Exception:  # noqa: BLE001
                pass
            fresh = None
        if fresh is not None:
            teams = [t for t in fresh if isinstance(t, dict)]
            status = brand.membership(teams, slug)
    return status, teams


def resolve_group(teams, slug=None, log=None) -> dict:
    """Which Garage 61 team the laps come from, self-correcting (2.21). Only ever a team you are in:
    1 the slug asked for (Settings), 2 the team named like "Overdriver Academy", 3 the earlier default
    (midfield-menace), 4 your only team. teams None = couldn't be read: the slug is kept, nothing is claimed.
    Returns {"slug","name","why","ok","teams":[names]}."""
    slug = slug or brand.GROUP_SLUG
    mine = [t for t in (teams or []) if isinstance(t, dict) and (t.get("slug") or t.get("id"))]
    names = [str(t.get("name") or t.get("slug") or t.get("id")) for t in mine]

    def pick(t, why):
        sl = str(t.get("slug") or t.get("id"))
        out = {"slug": sl, "name": brand.team_name_for(mine, sl), "why": why, "ok": True, "teams": names}
        if log:
            log(f"  Garage 61 team used for laps: {sl} ({why})")
        return out
    if teams is None:
        return {"slug": slug, "name": brand.team_name_for(None, slug), "why": "your teams couldn't be read, so the saved one is kept",
                "ok": True, "teams": []}
    want = norm_name(slug)
    t = next((t for t in mine if norm_name(t.get("slug") or t.get("id")) == want), None)
    if t:
        return pick(t, "the group set in Settings")
    t = next((t for t in mine if norm_name(brand.GROUP_NAME) in (norm_name(t.get("name")), norm_name(t.get("slug") or t.get("id")))), None)
    if t:
        return pick(t, f"you are in a team named {brand.GROUP_NAME}")
    t = next((t for t in mine if norm_name(t.get("slug") or t.get("id")) == norm_name(brand.TEAM_SLUG)), None)
    if t:
        return pick(t, "the earlier default team you are in")
    if len(mine) == 1:
        return pick(mine[0], "the only team you are in")
    if log:
        log(f"  No Garage 61 team fits {slug}. You are in: {', '.join(names) or 'no teams'}")
    return {"slug": slug, "name": brand.team_name_for(None, slug), "why": "none of your teams fits", "ok": False, "teams": names}


def no_group_note(g) -> str:
    return (f"None of your Garage 61 teams matches {g['slug']}. " + (f"You are in: {', '.join(g['teams'])}. " if g["teams"] else
            "You are not in any Garage 61 team. ") + "Pick the right one in Settings > Team.")


def team_info(slug=None, teams=None) -> dict:
    """{"slug", "name"} for a data dict: the display name, never the bare slug."""
    slug = slug or brand.GROUP_SLUG
    return {"slug": slug, "name": brand.team_name_for(teams, slug)}


# ---------------------------------------------------------------- 2.21: the team, the group, and the combos to look at
def settings_of(cfg) -> dict:
    """{"name", "members", "slug"} from the settings (defaults: Midfield Menace, its three drivers, Overdriver Academy)."""
    cfg = cfg or {}
    return {"name": str(cfg.get("team_name") or brand.TEAM_NAME), "slug": str(cfg.get("team_slug") or brand.GROUP_SLUG),
            "members": (brand.parse_members(cfg.get("team_members")) if "team_members" in cfg else list(brand.MEMBERS))
            if cfg else []}   # no settings at all (a bare call): no fixed team, everyone in the group counts


def prepare_combos(folder) -> list[dict]:
    """With no reviews yet: the track and car picked on Prepare, so the Team page still has something to compare."""
    try:
        from . import series
        c = series.load_choice(folder)
    except Exception:  # noqa: BLE001
        return []
    if not c.get("track") or not c.get("car"):
        return []
    return [{"track": c["track"], "car": c["car"], "track_id": None, "car_id": None, "races": 0, "best": None,
             "best_date": None, "last": "", "from_prepare": True}]


def _slim(r) -> dict:
    return {k: r.get(k) for k in ("key", "name", "t", "gap", "irating", "date")}


def _combo_notes_failed(c, e) -> str:
    return (f"Garage 61 didn't answer for {library.short_track_name(c['track'])} / {c['car']}. "
            "Try Refresh again later. The other tracks are fine.")


COVERAGE_NOTE = ("Garage 61's lap search needs a track, so this list is built from your races, the series boards and "
                 "the tracks loaded before. A track nobody here has touched yet can't be listed.")


def _ckey(track, car) -> tuple:
    return (norm_name(track), norm_name(car))


def stored_combos(folder, cars=None) -> list[dict]:
    """Track and car combos the team has laps at that are already on this PC: the last Team page (every combo it
    listed, loaded or not) and the saved weekly boards (any driver other than you). No requests."""
    out = []
    try:
        prev = json.loads((Path(folder) / "team" / "team.json").read_text(encoding="utf-8"))
        for c in (prev.get("combos") if isinstance(prev, dict) else None) or []:
            if isinstance(c, dict) and c.get("track") and c.get("car"):
                who = [r.get("name") for r in (c.get("board") or []) + (c.get("academy") or [])
                       if isinstance(r, dict) and r.get("key") != "me" and r.get("name")] or list(c.get("who") or [])
                out.append({"track": c["track"], "car": c["car"], "track_id": c.get("track_id"), "car_id": c.get("car_id"),
                            "races": c.get("races") or 0, "best": None, "best_date": None,
                            "last": str(c.get("last_date") or ""), "who": who, "source": "the last Team page"})
    except (OSError, ValueError):
        pass
    try:
        from . import leaderboards
        for sd in leaderboards._seasons(Path(folder)):
            for f in sorted(sd.glob("*/w*.json")):
                d = leaderboards._read(f)
                if not isinstance(d, dict) or not d.get("track"):
                    continue
                for car in sorted({str(r.get("car")) for r in d.get("board") or [] if isinstance(r, dict) and r.get("car")}):
                    who = [r.get("name") for r in d["board"] if isinstance(r, dict) and r.get("car") == car
                           and r.get("key") != "me" and r.get("name")]
                    if who:
                        out.append({"track": d["track"], "car": car, "track_id": d.get("track_id"), "car_id": None,
                                    "races": 0, "best": None, "best_date": None,
                                    "last": str(d.get("fetched_at") or "")[:10], "who": who, "source": "the series boards"})
    except Exception:  # noqa: BLE001  (no weekly boards: nothing to add)
        pass
    return out


_DISCOVERED: set = set()      # normalised names of every driver seen in the last discovery (for the 0-rows log text)
def _ids(items) -> list:
    out = []
    for t in items or []:
        i = g(t, "id") if isinstance(t, dict) else None
        if i is not None and i not in out:
            out.append(i)
    return out


DISCOVER_TRACKS_PER_REQUEST = 10     # Garage 61: "Cannot specify more than 10 tracks"
DISCOVER_MAX_REQUESTS = 4            # 2.23.1: a hard cap per Refresh
DISCOVER_DAYS = 60


def _track_limit(text, default):
    """"Cannot specify more than 7 tracks" -> 7 (never above the default, never below 1)."""
    import re
    m = re.search(r"more than (\d+) tracks?", str(text or ""), re.I)
    return max(1, min(default, int(m.group(1)))) if m else default


def candidate_track_ids(api, folder, mine=None, days=DISCOVER_DAYS, now=None, first=None) -> list:
    """2.23.1: which tracks to ask Garage 61 about, newest first and without repeats: your own tracks from the last
    `days` days of synced sessions, your reviewed races, the track picked on Prepare (this week's series), then the
    tracks in the last Team page. No requests."""
    out = []

    def add(i):
        if i is not None and i not in out:
            out.append(i)
    for i in first or []:   # tracks the team's own statistics say it drove (newest first)
        add(i)
    cutoff = ((now or datetime.now(timezone.utc)).timestamp() - days * 86400)
    try:
        from . import sync
        st = sync.Store.for_api(api)
        rows = sorted(st.sessions(), key=lambda s: str(s.get("start") or ""), reverse=True)
        for s in rows:
            d = sync._parse(s.get("start"))
            if d is not None and d.timestamp() >= cutoff:
                add(s.get("track_id"))
    except Exception:  # noqa: BLE001  (no synced history: other sources below)
        pass
    for c in mine or []:
        add(c.get("track_id"))
    try:
        from . import series
        ch = series.load_choice(folder)
        if ch.get("track"):
            add(app.id_for_name(api.cached_items("tracks") or [], ch["track"]))
    except Exception:  # noqa: BLE001
        pass
    # tracks any teammate was seen at (the last Team page and the saved series boards), newest first
    try:
        titems = api.cached_items("tracks") or []
        for c in sorted(stored_combos(folder), key=lambda c: str(c.get("last") or ""), reverse=True):
            tid = c.get("track_id")
            if tid is None and titems:
                tid = app.id_for_name(titems, c["track"])
            add(tid)
    except Exception:  # noqa: BLE001
        pass
    return out


def discover_combos(api, slug, members, my_slug="", my_name="", log=print, track_ids=None, member_slugs=None):
    """The group's laps at the candidate tracks (2.23.1). Garage 61 needs `tracks` and allows at most 10 per request,
    so the candidates (newest first) go in chunks of 10, at most DISCOVER_MAX_REQUESTS requests per Refresh; if
    Garage 61 names a lower limit the chunks shrink to it. Returns ([combos with "who", source "garage61"], status
    line). Never raises. Cached 10 minutes like find_laps."""
    fetch = getattr(api, "laps_page", None)
    fallback = "Garage 61 couldn't list the team's tracks; showing tracks Debrief already knows."
    ids = list(track_ids or [])
    if not callable(fetch):
        return [], fallback
    if not ids:
        log("  Garage 61 team discovery: no recent tracks to search yet")
        return [], "No recent tracks to search yet; showing tracks Debrief already knows."
    cached = getattr(api, "_cached", None)
    state = {"sent": 0, "limit": DISCOVER_TRACKS_PER_REQUEST}
    main_cap = DISCOVER_MAX_REQUESTS - (1 if member_slugs else 0)    # one request kept for a member nobody has a row for
    pool = ids[main_cap * DISCOVER_TRACKS_PER_REQUEST:]            # candidates the normal passes will not reach
    ids = ids[:main_cap * DISCOVER_TRACKS_PER_REQUEST]

    def err_text(e):
        return ((getattr(e, "detail", "") or "") + " " + (str(e) or type(e).__name__)).replace("\n", " ").strip()[:200]

    def one(tids):
        params = dict(teams=[slug], group="none", limit=1000, tracks=list(tids), age=-2)

        def go():
            state["sent"] += 1
            return list(fetch(**params)[0])
        key = "discover?" + json.dumps(params, sort_keys=True, default=str)
        return cached(key, go, max_age_s=600) if callable(cached) else go()

    rows, seen_ids, searched = [], set(), []
    queue = [ids[i:i + state["limit"]] for i in range(0, len(ids), state["limit"])]
    failed = None
    while queue and state["sent"] < main_cap:
        ch = queue.pop(0)
        log(f"  Garage 61 team discovery: asking for {len(ch)} tracks (request {state['sent'] + 1} of at most "
            f"{DISCOVER_MAX_REQUESTS})")
        try:
            got = one(ch)
        except Exception as e:  # noqa: BLE001
            msg = err_text(e)
            lim = _track_limit(msg, state["limit"])
            if getattr(e, "status", None) == 400 and lim < state["limit"] and len(ch) > lim:
                log(f"  Garage 61 allows only {lim} tracks per request; using chunks of {lim}")
                state["limit"] = lim
                rest = ch + [t for q in queue for t in q]
                queue = [rest[i:i + lim] for i in range(0, len(rest), lim)]
                continue
            failed = e
            log(f"  Garage 61 team discovery failed: {getattr(e, 'status', None) or type(e).__name__} {msg}")
            break
        searched += ch
        log(f"  Garage 61 team discovery: {len(got)} laps came back")
        for l in got:
            i = g(l, "id") if isinstance(l, dict) else None
            if i is not None and i in seen_ids:
                continue
            seen_ids.add(i)
            rows.append(l)
    pool = [t for q in queue for t in q] + pool
    if queue:
        log(f"  Garage 61 team discovery: {sum(len(q) for q in queue)} older tracks were not searched this time")
    # a member nobody has a row for yet: ask for just their laps (drivers filter) on the next tracks not yet searched,
    # only while under the request cap
    if failed is None and member_slugs:
        got_names = {norm_name(driver_of(l)["name"]) for l in rows if isinstance(l, dict)} | \
                    {norm_name(driver_of(l)["slug"]) for l in rows if isinstance(l, dict)}
        for nm, sl in member_slugs.items():
            if state["sent"] >= DISCOVER_MAX_REQUESTS or not pool:
                break
            if norm_name(nm) in got_names or norm_name(sl) in got_names or sl == my_slug:
                continue
            ch, pool = pool[:state["limit"]], pool[state["limit"]:]
            log(f"  Garage 61 team discovery: no laps yet for {nm}; asking for their laps at {len(ch)} more tracks")
            try:
                def go2(ch=ch, sl=sl):
                    state["sent"] += 1
                    return list(fetch(teams=[slug], drivers=[sl], group="none", limit=1000, tracks=list(ch), age=-2)[0])
                key = "discover?" + json.dumps({"d": sl, "t": ch}, sort_keys=True, default=str)
                got = cached(key, go2, max_age_s=600) if callable(cached) else go2()
            except Exception as e:  # noqa: BLE001
                log(f"  Garage 61 member search for {nm} failed: {getattr(e, 'status', None) or type(e).__name__} {err_text(e)}")
                continue
            searched += ch
            log(f"  Garage 61 team discovery: {len(got)} laps came back for {nm}")
            for l in got:
                i = g(l, "id") if isinstance(l, dict) else None
                if i is not None and i in seen_ids:
                    continue
                seen_ids.add(i)
                rows.append(l)
    log(f"  Garage 61 team discovery: {state['sent']} requests sent, {len(searched)} tracks searched, {len(rows)} laps in total")
    items = [l for l in rows if isinstance(l, dict)]
    if not items:
        return [], (fallback if failed is not None else
                    f"No team laps found at the {len(searched)} recent tracks searched; showing tracks Debrief already knows.")
    found, per = {}, {}
    names = {}
    _DISCOVERED.clear()
    for l in items:
        d = driver_of(l)
        _DISCOVERED.update(x for x in (norm_name(d["name"]), norm_name(d["slug"])) if x)
        tr, car = g(l, "track"), g(l, "car")
        if not (isinstance(tr, dict) and isinstance(car, dict) and g(tr, "name") and g(car, "name")):
            continue
        v = g(tr, "variant")
        tname = g(tr, "name") + (f" ({v})" if v else "")
        is_me = bool(my_slug) and d["slug"] == my_slug
        who = my_name if is_me else d["name"]
        names[d["slug"]] = who
        k = _ckey(tname, g(car, "name"))
        c = found.setdefault(k, {"track": tname, "car": g(car, "name"), "track_id": g(tr, "id"), "car_id": g(car, "id"),
                                 "races": 0, "best": None, "best_date": None, "last": "", "who": set(), "source": "garage61"})
        c["last"] = max(c["last"], str(lap_day(l) or ""))
        if not is_me:
            c["who"].add(who)
        p = per.setdefault(d["slug"], [0, set()])
        p[0] += 1
        p[1].add(k)
    for m in members or []:
        sl = next((sl for sl, n in names.items() if norm_name(n) == norm_name(m) or norm_name(sl) == norm_name(m)), None)
        laps_n, combos_n = (per[sl][0], len(per[sl][1])) if sl else (0, 0)
        log(f"  Discovery: {m}: {laps_n} laps, {combos_n} track and car combinations")
    out = []
    for c in found.values():
        c["who"] = sorted(c["who"])
        out.append(c)
    return out, f"Found {len(out)} tracks where the team has laps on Garage 61."


def merge_combos(*lists) -> list[dict]:
    """One row per track and car. Your own combos come first in each list's order; the rest follow, newest first.
    A row keeps every source's driver names ("who")."""
    seen, out = {}, []
    for lst in lists:
        for c in lst:
            k = _ckey(c["track"], c["car"])
            if k in seen:
                t = seen[k]
                t["who"] = sorted(set(t.get("who") or []) | set(c.get("who") or []))
                t["track_id"] = t.get("track_id") or c.get("track_id")
                t["car_id"] = t.get("car_id") or c.get("car_id")
                t["last"] = max(str(t.get("last") or ""), str(c.get("last") or ""))
                continue
            c = dict(c)
            c["who"] = sorted(set(c.get("who") or []))
            seen[k] = c
            out.append(c)
    return sorted(out, key=lambda c: str(c.get("last") or ""), reverse=True)


def who_note(entry, members, my_name="") -> str:
    """"Josh, Kenta · you haven't driven this": which team members have a lap here."""
    names = [r.get("name") for r in (entry.get("board") or []) if isinstance(r, dict) and r.get("key") != "me"]
    if not entry.get("loaded", True):
        names = [n for n in (entry.get("who") or []) if any(norm_name(n) == norm_name(m) for m in members)] \
            if members else list(entry.get("who") or [])
    first = [str(n).split()[0] for n in names if n]
    mine = bool(entry.get("races")) or any(r.get("key") == "me" for r in (entry.get("board") or []) if isinstance(r, dict))
    bits = ", ".join(first) if first else "no teammate laps found yet"
    return bits + ("" if mine else " \u00b7 you haven't driven this")


def _ctx(api, team_slug, cfg, log):
    st = settings_of(cfg)
    me = api.me() or {}
    my_slug = g(me, "slug", default="") or ""
    my_name = " ".join(filter(None, [g(me, "firstName", "firstname"), g(me, "lastName", "lastname")])) or my_slug or "You"
    team_slug = team_slug or st["slug"] or brand.GROUP_SLUG
    mem, teams_list = membership_for(api, team_slug, log)
    grp = resolve_group(teams_list, team_slug, log)
    team_slug = grp["slug"]
    if teams_list is not None:
        mem = brand.membership(teams_list, team_slug) if grp["ok"] else brand.NOT_MEMBER
    group_name = brand.team_name_for(teams_list, team_slug) if grp["ok"] else grp["name"]
    notes = [] if grp["ok"] else [no_group_note(grp)]
    not_member = mem == brand.NOT_MEMBER
    if not_member:
        log(f"You're not in the {group_name} group on Garage 61, so no team laps were loaded.")
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
    return {"st": st, "members": st["members"], "my_slug": my_slug, "my_name": my_name, "team_slug": team_slug,
            "mem": mem, "teams": teams_list, "grp": grp, "group_name": group_name, "not_member": not_member,
            "roster": team_roster(teams_list, team_slug), "notes": notes, "ids_for": ids_for,
            "track_items": api.cached_items("tracks") or []}


def _load_entry(api, ctx, c, seen, log, label="") -> dict:
    short = library.short_track_name(c["track"])
    log(f"{short} \u00b7 {c['car']}: loading teammates' best laps{label}...")
    entry = {"track": c["track"], "track_short": short, "car": c["car"], "track_id": c["track_id"],
             "car_id": c["car_id"], "races": c["races"], "last_date": library.local_day(c["last"]) or None,
             "board": [], "academy": [], "kings": None, "note": None, "mate_notes": [], "loaded": True}
    try:   # 2.21: one combination failing never costs the others
        _build_combo(api, entry, c, ctx["ids_for"], ctx["track_items"], ctx["team_slug"], ctx["not_member"],
                     ctx["my_slug"], ctx["my_name"], ctx["members"], ctx["group_name"], ctx["roster"], seen, log)
    except Exception as e:  # noqa: BLE001
        import traceback
        log(f"  {short} \u00b7 {c['car']} failed ({type(e).__name__}: {e}):\n" + traceback.format_exc())
        entry["note"] = _combo_notes_failed(c, e)
        entry["failed"] = True
    entry["who"] = [r["name"] for r in entry["board"] + entry["academy"] if r.get("key") != "me"]
    entry["who_note"] = who_note(entry, ctx["members"])
    return entry


SLOW_NOTE = "Garage 61 asked Debrief to slow down; open a track to load it."
TEAM_PACE_S = 3.0


def merge_combos_keep(*lists) -> list[dict]:
    """Your own combos from several sources, one per track and car, newest first."""
    seen, out = {}, []
    for lst in lists:
        for c in lst:
            k = _ckey(c["track"], c["car"])
            if k in seen:
                t = seen[k]
                t["races"] = max(t.get("races") or 0, c.get("races") or 0)
                t["track_id"] = t.get("track_id") or c.get("track_id")
                t["car_id"] = t.get("car_id") or c.get("car_id")
                if str(c.get("last") or "") > str(t.get("last") or ""):
                    t["last"] = c["last"]
                if c.get("best") is not None and (t.get("best") is None or c["best"] < t["best"]):
                    t["best"], t["best_date"] = c["best"], c.get("best_date")
                continue
            seen[k] = dict(c)
            out.append(seen[k])
    return sorted(out, key=lambda c: str(c.get("last") or "")[:19], reverse=True)


def _unloaded_entry(c, members, note=None) -> dict:
    e = {"track": c["track"], "track_short": library.short_track_name(c["track"]), "car": c["car"],
         "track_id": c.get("track_id"), "car_id": c.get("car_id"), "races": c.get("races") or 0,
         "last_date": library.local_day(c.get("last")) or (str(c.get("last") or "") or None),
         "board": [], "academy": [], "kings": None, "mate_notes": [], "loaded": False, "who": list(c.get("who") or []),
         "note": note or "Not loaded yet. In the Team view of the window, pick this track and press Load this track."}
    e["who_note"] = who_note(e, members)
    return e


def _same_group(weekly, slug, name, mem):
    """2.23.1: the weekly boards describe the group the Team page resolved, never the built-in default slug."""
    if isinstance(weekly, dict):
        weekly["team"] = {"slug": slug, "name": name}
        if weekly.get("membership") in (None, brand.UNKNOWN) and mem:
            weekly["membership"] = mem
    return weekly


def _assemble(ctx, out_combos, api, start_req, folder, notes, cfg, log) -> dict:
    seen = {}
    for c in out_combos:
        for r in (c.get("board") or []) + (c.get("academy") or []):
            if isinstance(r, dict) and r.get("key") not in (None, "me"):
                seen[r["key"]] = r.get("name") or r["key"]
    members, grp, group_name = ctx["members"], ctx["grp"], ctx["group_name"]
    for k, n_ in (ctx.get("extra_seen") or {}).items():
        seen.setdefault(k, n_)
    total, loaded = len(out_combos), sum(1 for c in out_combos if c.get("loaded", True))
    matched, unmatched = match_members(members, {**ctx["roster"], **seen}, ctx["my_slug"], ctx["my_name"])
    member_rows = []
    for m in members:
        row = {"name": m, "key": matched.get(m), "is_me": matched.get(m) == "me", "matched": m in matched}
        if m not in matched:
            n_s = max(ctx.get("searched_tracks") or 0, loaded)
            row["problem"] = (f"No laps found for {m} at the {n_s} tracks searched. If they have driven elsewhere, open "
                              "that track; if Garage 61 shows them under another name, change it in Settings > Team."
                              if (seen or ctx["roster"]) and not ctx["not_member"] and n_s else
                              f"Not seen yet: no {group_name} laps have been loaded, so this name couldn't be matched.")
        member_rows.append(row)
    if unmatched and not ctx["not_member"]:
        n_s = max(ctx.get("searched_tracks") or 0, loaded)
        log(f"  No laps found for {', '.join(unmatched)} at the {n_s} tracks searched" if n_s else
            "  Team members not seen yet: " + ", ".join(unmatched))
    cov = (f"{total} tracks \u00b7 {loaded} loaded \u00b7 open a track to load it" if loaded < total
           else f"{total} track{'s' if total != 1 else ''} \u00b7 all loaded")
    data = {
        "version": 2,
        "team": {"slug": ctx["team_slug"], "name": group_name},          # the Garage 61 group (kept for older readers)
        "group": {"slug": ctx["team_slug"], "name": group_name, "why": grp["why"], "ok": grp["ok"]},
        "laps_from": f"Laps come from {group_name} on Garage 61." if grp["ok"] else no_group_note(grp),
        "team_name": ctx["st"]["name"],
        "members": member_rows,
        "membership": ctx["mem"],
        "me": {"name": ctx["my_name"], "key": "me"},
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "combos": out_combos,
        "coverage": {"total": total, "loaded": loaded, "note": cov},
        "h2h": head_to_heads([c for c in out_combos if c.get("loaded", True)]),
        "requests": api.requests_made - start_req,
        "notes": notes,
        "settings_stamp": settings_stamp(cfg),
        "section_notes": [],
    }
    try:
        from . import leaderboards
        data["weekly"] = leaderboards.view(folder)   # saved snapshots only: no requests
        _same_group(data["weekly"], ctx["team_slug"], group_name, ctx["mem"])
    except Exception as e:  # noqa: BLE001
        import traceback
        log("  Couldn't load this week's leaderboards:\n" + traceback.format_exc())
        data["section_notes"].append(f"Couldn't load this week's leaderboards: {type(e).__name__}. "
                                     "Press Refresh this week to try again.")
    return data


def build(api, folder, team_slug, log=print, max_combos=MAX_COMBOS, cfg=None) -> dict:
    """List every track and car the team is known to have laps at (2.21), load the full detail of the newest
    max_combos, and leave the rest to load on demand (load_combo). Nothing is dropped without being counted."""
    max_combos = max(1, min(MAX_COMBOS, int(max_combos)))
    start_req = api.requests_made
    if hasattr(api, "min_gap") and isinstance(api.min_gap, (int, float)):   # 2.23.1: start slower (3 s apart)
        api.min_gap = max(api.min_gap, TEAM_PACE_S)
        if isinstance(getattr(api, "base_gap", None), (int, float)):
            api.base_gap = max(api.base_gap, TEAM_PACE_S)
    ctx = _ctx(api, team_slug, cfg, log)
    notes = list(ctx["notes"])
    mine = merge_combos_keep(session_combos(api), library_combos(folder))
    if not mine:
        mine = prepare_combos(folder)
        if mine:
            notes.append("No race reviews yet, so this page uses the track and car picked on Prepare. Review a race "
                         "and it will use your own tracks and cars.")
        else:
            log("No reviews in your library yet.")
    info_roster = {}
    try:
        info = api.team_info(ctx["team_slug"]) if callable(getattr(api, "team_info", None)) and not ctx["not_member"] else None
        info_roster = roster_from_info(info)
        if info is not None:
            log(f"  Garage 61 team info: {len(info_roster)} members listed")
    except Exception as e:  # noqa: BLE001
        log(f"  Garage 61 team info unavailable: {getattr(e, 'status', None) or type(e).__name__}")
    if info_roster:
        ctx["roster"] = {**info_roster, **ctx["roster"]}
    stats, stats_note = team_stats(api, ctx, log)
    scombos = stats_combos(stats, ctx["my_slug"], ctx["my_name"]) if stats else []
    if stats_note:
        notes.append(stats_note)
    for r in stats or []:
        if r["slug"] and r["slug"] != ctx["my_slug"]:
            ctx.setdefault("extra_seen", {})[r["slug"]] = r["name"]
    races = race_groups(api, ctx, log)
    cand = candidate_track_ids(api, folder, mine, first=[c["track_id"] for c in scombos])
    log(f"Team: {len(cand)} candidate tracks to search for teammates (your last {DISCOVER_DAYS} days, the series, the last page)")
    found, disc_note = ([], "Garage 61 couldn't list the team's tracks; showing tracks Debrief already knows.") \
        if ctx["not_member"] else discover_combos(api, ctx["team_slug"], ctx["members"], ctx["my_slug"], ctx["my_name"], log,
                                                  track_ids=cand, member_slugs=_member_slugs(ctx))
    ctx["searched_tracks"] = len(cand)
    allc = order_combos(mine, merge_combos(scombos, found, stored_combos(folder)))
    notes.append(COVERAGE_NOTE)
    prev = {}
    try:
        for c in (json.loads((Path(folder) / "team" / "team.json").read_text(encoding="utf-8")).get("combos") or []):
            if isinstance(c, dict) and c.get("loaded", True) and c.get("track"):
                prev[_ckey(c["track"], c["car"])] = c
    except (OSError, ValueError, AttributeError):
        pass
    out_combos, seen = [], {}
    slowed = False
    for i, c in enumerate(allc):
        if 0 < i < max_combos and getattr(api, "rate_limited", 0) >= 2:
            if not slowed:
                slowed = True
                log("Garage 61 asked Debrief to slow down twice; the remaining tracks are left to open on demand.")
            out_combos.append(_unloaded_entry(c, ctx["members"], SLOW_NOTE))
        elif i < max_combos:
            out_combos.append(_load_entry(api, ctx, c, seen, log, f" ({i + 1} of {min(max_combos, len(allc))})"))
        elif _ckey(c["track"], c["car"]) in prev:       # loaded on demand earlier: kept, said so
            e = dict(prev[_ckey(c["track"], c["car"])])
            e["note"] = (e.get("note") or "") + " Loaded earlier; press Load this track to update it."
            e["loaded"] = True
            out_combos.append(e)
        else:
            out_combos.append(_unloaded_entry(c, ctx["members"]))
    if slowed:
        notes.append(SLOW_NOTE)
    data = _assemble(ctx, out_combos, api, start_req, folder, notes, cfg, log)
    data["races"] = races
    data["member_stats"] = member_records(stats, ctx["members"], ctx["my_slug"]) if stats else []
    data["discovery"] = disc_note
    log(f"Team hub ready. Garage 61 requests used: {data['requests']}.")
    return data


def load_combo(api, folder, track, car, team_slug=None, log=print, cfg=None) -> dict:
    """Load one track and car in full ("Load this track") and put it into the saved team page. Same cost as one
    combination in a refresh (cached laps cost nothing). Returns the new team data (not yet written)."""
    start_req = api.requests_made
    p = Path(folder) / "team" / "team.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("Refresh team data once first, then load a track.")
    combos = [c for c in data.get("combos") or [] if isinstance(c, dict)]
    k = _ckey(track, car)
    idx = next((i for i, c in enumerate(combos) if _ckey(c.get("track"), c.get("car")) == k), None)
    if idx is None:
        raise ValueError("That track and car isn't on the Team page. Refresh team data first.")
    ctx = _ctx(api, team_slug, cfg, log)
    old = combos[idx]
    c = {"track": old["track"], "car": old["car"], "track_id": old.get("track_id"), "car_id": old.get("car_id"),
         "races": old.get("races") or 0, "best": None, "best_date": None, "last": str(old.get("last_date") or "")}
    mine = next((m for m in library_combos(folder) if _ckey(m["track"], m["car"]) == k), None)
    if mine:
        c = mine
    combos[idx] = _load_entry(api, ctx, c, {}, log)
    out = _assemble(ctx, combos, api, start_req, folder, [n for n in data.get("notes") or [] if isinstance(n, str)], cfg, log)
    out["checked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return out


def _member_slugs(ctx) -> dict:
    """{member name: Garage 61 slug} for team members whose slug the group roster tells us."""
    out = {}
    for m in ctx["members"]:
        for sl, nm in (ctx.get("roster") or {}).items():
            if sl != ctx["my_slug"] and (norm_name(nm) == norm_name(m) or norm_name(sl) == norm_name(m)):
                out[m] = sl
    return out


STATS_DAYS = 90


def roster_from_info(info) -> dict:
    """{slug: name} from GET /teams/{slug} when it lists members (same shapes as team_roster)."""
    return team_roster([dict(info, slug="x")] if isinstance(info, dict) else [], "x")


def team_stats(api, ctx, log, now=None) -> tuple:
    """2.23.1: what the team drove in the last STATS_DAYS days, from Garage 61's team statistics (one request, no
    lap search). Returns (rows [{slug,name,track_id,car_id,track,car,day,laps,secs}], note). rows is None when
    Garage 61 would not say; the exact status and message are logged and the page note says so. Never raises."""
    fn = getattr(api, "team_statistics", None)
    if ctx["not_member"] or not callable(fn):
        return None, ""
    start = ((now or datetime.now(timezone.utc)) - timedelta(days=STATS_DAYS)).strftime("%Y-%m-%dT00:00:00Z")
    try:
        raw = list(fn(ctx["team_slug"], start) or [])
    except Exception as e:  # noqa: BLE001
        st = getattr(e, "status", None)
        msg = ((getattr(e, "detail", "") or "") + " " + (str(e) or type(e).__name__)).replace("\n", " ").strip()[:200]
        log(f"  Garage 61 team statistics refused: {st or type(e).__name__} {msg}")
        return None, (f"Garage 61 wouldn't share the team's driving statistics ({st or type(e).__name__}), so Debrief "
                      "searched the tracks you drove instead.")
    raw = [r for r in raw if isinstance(r, dict)]
    log(f"  Garage 61 team statistics: {len(raw)} rows" + (f"; first row has keys {sorted(raw[0])}" if raw else ""))
    tn = {g(t, "id"): g(t, "name") for t in (api.cached_items("tracks") or []) if isinstance(t, dict)}
    cn = {g(c, "id"): g(c, "name") for c in (api.cached_items("cars") or []) if isinstance(c, dict)}
    rows = []
    for r in raw:
        who = g(r, "driver", "user", "member")
        d = driver_of({"driver": who}) if isinstance(who, dict) else {"slug": str(who or ""), "name": str(who or "")}
        tr, car = g(r, "track", "trackId", "track_id"), g(r, "car", "carId", "car_id")
        tid, cid = (g(tr, "id") if isinstance(tr, dict) else tr), (g(car, "id") if isinstance(car, dict) else car)
        tname = (g(tr, "name") if isinstance(tr, dict) else None) or tn.get(tid) or (f"Track {tid}" if tid is not None else None)
        cname = (g(car, "name") if isinstance(car, dict) else None) or cn.get(cid) or (f"Car {cid}" if cid is not None else None)
        if isinstance(tr, dict) and g(tr, "variant"):
            tname = f"{tname} ({g(tr, 'variant')})"
        if tid is None or not d["slug"] and not d["name"]:
            continue
        secs = g(r, "time", "timeDriven", "seconds", "duration")
        rows.append({"slug": d["slug"], "name": d["name"], "track_id": tid, "car_id": cid, "track": tname, "car": cname,
                     "day": str(g(r, "day", "date", "startDate", "start", default="") or "")[:10],
                     "laps": int(g(r, "lapCount", "laps", "lapsDriven", default=0) or 0),
                     "secs": float(secs) if isinstance(secs, (int, float)) and not isinstance(secs, bool) else 0.0})
    return rows, ""


def stats_combos(rows, my_slug, my_name) -> list[dict]:
    """The team's track and car combinations from statistics rows, newest first, with who drove each."""
    found = {}
    for r in rows or []:
        if r["car"] is None:
            continue
        k = (r["track_id"], r["car_id"])
        c = found.setdefault(k, {"track": r["track"], "car": r["car"], "track_id": r["track_id"], "car_id": r["car_id"],
                                 "races": 0, "best": None, "best_date": None, "last": "", "who": set(),
                                 "source": "garage61 statistics"})
        c["last"] = max(c["last"], r["day"])
        if r["slug"] != my_slug:
            c["who"].add(r["name"])
    out = []
    for c in found.values():
        c["who"] = sorted(c["who"])
        out.append(c)
    return sorted(out, key=lambda c: c["last"], reverse=True)


def member_records(rows, members, my_slug="") -> list[dict]:
    """Per team member from statistics: tracks, cars, laps, hours driven (before any lap is loaded)."""
    out = []
    for m in members or []:
        mine = [r for r in rows or [] if norm_name(r["name"]) == norm_name(m) or norm_name(r["slug"]) == norm_name(m)]
        tr = {}
        for r in mine:
            e = tr.setdefault((r["track"], r["car"]), {"track": r["track"], "car": r["car"], "laps": 0, "day": ""})
            e["laps"] += r["laps"]
            e["day"] = max(e["day"], r["day"])
        out.append({"name": m, "tracks": len({r["track"] for r in mine}), "cars": len({r["car"] for r in mine if r["car"]}),
                    "laps": sum(r["laps"] for r in mine), "hours": round(sum(r["secs"] for r in mine) / 3600, 1),
                    "days": STATS_DAYS,
                    "combos": sorted(tr.values(), key=lambda e: e["day"], reverse=True)[:30]})
    return out


def race_groups(api, ctx, log, n=2, now=None) -> list[dict]:
    """2.23.1: for your newest `n` races (events in your synced sessions), who from the team was in the SAME race, in
    any car: /laps?event=..&tracks=..&teams=..&unclean=true, one request each, no car filter. Race laps with incidents
    count. Never raises."""
    if ctx["not_member"]:
        return []
    try:
        from . import sync
        rows = sync.Store.for_api(api).sessions()
    except Exception:  # noqa: BLE001
        return []
    events, seen = [], set()
    for s_ in sorted(rows, key=lambda r: str(r.get("start") or ""), reverse=True):
        ev, tid = s_.get("event"), s_.get("track_id")
        if ev and tid is not None and ev not in seen:
            seen.add(ev)
            events.append(s_)
        if len(events) >= n:
            break
    today = (now or datetime.now()).date().isoformat()
    out = []
    for s_ in events:
        if getattr(api, "rate_limited", 0) >= 2:
            break
        short = library.short_track_name(s_.get("track") or f"Track {s_['track_id']}")
        try:
            laps = list(api.find_laps(event=s_["event"], tracks=[s_["track_id"]], teams=[ctx["team_slug"]],
                                      unclean=True, group="driver", limit=200))
        except Exception as e:  # noqa: BLE001
            log(f"  Same-race search failed for {short}: {type(e).__name__}: {e}")
            continue
        cars = _cars_by_id(api)
        drivers = {}
        for l in sorted(laps, key=lambda l: app.lap_time(l) or 1e9):
            d = driver_of(l)
            if not d["slug"] or not app.lap_time(l):
                continue
            is_me = d["slug"] == ctx["my_slug"]
            if d["slug"] in drivers:
                continue
            cid, cname = _car_of(l)
            drivers[d["slug"]] = {"key": "me" if is_me else d["slug"], "name": ctx["my_name"] if is_me else d["name"],
                                  "car": cname or None, "class": _class_label(cars, cid, cname) if (cid or cname) else None,
                                  "t": app.lap_time(l), "is_me": is_me, "clean": usable(l)}
        day = library.local_day(s_.get("start")) or str(s_.get("start") or "")[:10]
        log(f"  Same race at {short} ({day}): {len(drivers)} drivers: " +
            ", ".join(f"{v['name']} ({v['car'] or 'car not named'})" for v in drivers.values()))
        for sl, v in drivers.items():
            if not v["is_me"]:
                ctx.setdefault("extra_seen", {})[sl] = v["name"]
        out.append({"event": str(s_["event"]), "track": s_.get("track"), "track_short": short, "date": day,
                    "title": (f"Tonight at {short}" if day == today else f"{day} at {short}"),
                    "drivers": sorted(drivers.values(), key=lambda v: v["t"])})
    return out


def session_combos(api, days=DISCOVER_DAYS * 6) -> list[dict]:
    """Your own synced sessions as track/car combos, newest first (no requests). This is what puts tonight's race
    on the Team page even before it has a saved review."""
    try:
        from . import sync
        rows = sync.Store.for_api(api).sessions()
    except Exception:  # noqa: BLE001
        return []
    out = {}
    for s in sorted(rows, key=lambda r: str(r.get("start") or ""), reverse=True):
        if not (s.get("track") and s.get("car") and s.get("track_id") is not None):
            continue
        k = (s.get("track_id"), s.get("car_id"))
        c = out.setdefault(k, {"track": s["track"], "car": s["car"], "track_id": s["track_id"], "car_id": s.get("car_id"),
                               "races": 0, "best": None, "best_date": None, "last": str(s.get("start") or "")})
        c["races"] += 1
    return list(out.values())


def _tkey(c):
    return ("id", c["track_id"]) if c.get("track_id") is not None else ("n", norm_name(library.short_track_name(c["track"])))


def order_combos(owner, others) -> list[dict]:
    """2.23.1: your own newest sessions first, then the other known tracks; one row per TRACK (the team view compares
    drivers at a track, whatever car each drove), keeping the first (your newest) car for it."""
    owner = sorted(owner, key=lambda c: str(c.get("last") or "")[:19], reverse=True)
    seen, out = set(), []
    for c in owner + list(others):
        names = {("n", norm_name(library.short_track_name(c["track"])))}
        if c.get("track_id") is not None:
            names.add(("id", c["track_id"]))
        if names & seen:
            continue
        seen |= names
        out.append(c)
    return out


def settings_stamp(cfg) -> str:
    """A short fingerprint of the team settings a page was built with, so a change can mark the page out of date."""
    st = settings_of(cfg)
    return "|".join([st["name"], st["slug"]] + st["members"])


_CLASS_KEYS = ("category", "class", "carClass", "classId", "carClassId", "categories")


def _cars_by_id(api) -> dict:
    try:
        return {g(c, "id"): c for c in (api.cars() or []) if isinstance(c, dict)}
    except Exception:  # noqa: BLE001
        return {}


def _class_label(cars, cid, cname="") -> str:
    """The car's class/category from Garage 61's car list (GT3, GT4...); the car's own name when the list has none."""
    item = cars.get(cid) or {}
    for k in _CLASS_KEYS:
        v = item.get(k)
        if v not in (None, "", [], {}):
            if isinstance(v, dict):
                v = g(v, "name") or json.dumps(v, sort_keys=True, default=str)
            elif isinstance(v, list):
                v = ", ".join(str(x.get("name") if isinstance(x, dict) else x) for x in v)
            return str(v)
    return str(cname or item.get("name") or (f"car {cid}" if cid is not None else "unknown car"))


def _car_of(lap):
    car = g(lap, "car")
    if isinstance(car, dict):
        return g(car, "id"), (g(car, "name") or "")
    return None, ""


def _log_rows(log, rows, members):
    """2.23.1: per driver, how many rows came back in all the searches and which tracks and cars they were at."""
    by = {}
    for l in rows:
        d = driver_of(l)
        if not d["slug"]:
            continue
        e = by.setdefault(d["slug"], {"name": d["name"], "n": 0, "tracks": set(), "cars": set()})
        e["n"] += 1
        tr = g(l, "track")
        if isinstance(tr, dict) and g(tr, "name"):
            e["tracks"].add(g(tr, "name"))
        if _car_of(l)[1]:
            e["cars"].add(_car_of(l)[1])
    for e in by.values():
        log(f"  Garage 61 total for {e['name']}: {e['n']} rows in all searches; tracks: "
            f"{', '.join(sorted(e['tracks'])) or 'not named'}; cars: {', '.join(sorted(e['cars'])) or 'not named'}")


def _build_combo(api, entry, c, ids_for, track_items, team_slug, not_member, my_slug, my_name, members, group_name,
                 roster_g61, seen_drivers, log):
    tid, cid = ids_for(c)
    entry["track_id"], entry["car_id"] = tid, cid
    if tid is None or cid is None:
        entry["note"] = NO_MATCH_NOTE
        return
    # not on the team: zero team-lap requests, only your own laps (your board row and telemetry)
    # 2.23.1: teammates are searched by TRACK only (no car filter, no class filter): in GT3/GT4 each driver has
    # another car, and a teammate may have driven a class you did not. Every lap is kept and grouped by class below.
    cars_by_id = _cars_by_id(api)
    my_cls = _class_label(cars_by_id, cid, c["car"])
    first = [] if not_member else list(api.find_laps(tracks=[tid], teams=[team_slug], group="driver", age=-2, limit=100))
    raw_rows, fetch_failed = list(first), False
    if not not_member:   # all their laps (one more request, cached 10 min): the best lap in each class, race laps included
        try:
            have = {g(l, "id") for l in raw_rows if g(l, "id") is not None}
            raw_rows += [l for l in api.find_laps(tracks=[tid], teams=[team_slug], group="none", age=-2, limit=1000)
                         if g(l, "id") is None or g(l, "id") not in have]
        except Exception as e:  # noqa: BLE001
            fetch_failed = True
            log(f"  Couldn't load everyone's laps: {type(e).__name__}: {e}")
    by_cls = {}
    for l in sorted(raw_rows, key=lambda l: app.lap_time(l) or 1e9):
        lcid, lcar = _car_of(l)
        by_cls.setdefault(_class_label(cars_by_id, lcid, lcar) if (lcid is not None or lcar) else my_cls, []).append(l)
    team_laps = by_cls.get(my_cls, [])
    my_laps = api.find_laps(tracks=[tid], cars=[cid], drivers=["me"], group="none", age=-2, limit=1000)
    anchor = anchor_for([app.lap_time(l) for l in my_laps if usable(l)], c["best"])
    my_ok = sorted((l for l in my_laps if usable(l) and plausible(app.lap_time(l), anchor)), key=app.lap_time)
    my_api = my_ok[0] if my_ok else None
    my_best = c["best"] or (app.lap_time(my_api) if my_api else None)
    mates, reasons = {}, []
    if not not_member:
        found, reasons = gather_mates(team_laps, None, my_slug, my_best, roster_g61, members=members, my_name=my_name,
                                      group=group_name, log=log)
        groups = [(my_cls, found)]
        for cls in sorted(k for k in by_cls if k != my_cls):
            f2, _r = gather_mates(by_cls[cls], None, my_slug, my_best, None, members=members, my_name=my_name, log=None)
            groups.append((cls, f2))
        elsewhere = {m["name"] for _c, f in groups[1:] for m in f.values()}
        reasons = [r for r in reasons if r["name"] not in elsewhere]
        if fetch_failed:
            reasons.append({"name": "A teammate", "text": "some laps couldn't be loaded this time. Try Refresh again."})
        for cls, fnd in groups:
            for sl, m in fnd.items():
                l = m["lap"]
                seen_drivers[sl] = m["name"]
                lcid, lcar = _car_of(l)
                mates[sl if sl not in mates else f"{sl}|{cls}"] = {
                    "key": sl if sl not in mates else f"{sl}|{cls}", "name": m["name"], "t": m["t"], "is_me": False,
                    "member": m["member"], "date": lap_day(l), "irating": g(l, "driverRating"), "_lap": g(l, "id"),
                    "car": lcar or None, "diff_car": bool(lcid is not None and lcid != cid), "class": cls,
                    "other_class": cls != my_cls}
        _log_rows(log, raw_rows, members)
        for l in team_laps:
            d = driver_of(l)
            if d["slug"] and d["slug"] != my_slug:
                seen_drivers.setdefault(d["slug"], d["name"])
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
    # the team's board has the team's members and you; other drivers in the group are listed apart (never mixed in)
    team_mates = {k: m for k, m in mates.items() if m["member"]}
    others = {k: m for k, m in mates.items() if not m["member"]}
    if mine:
        mine["class"], mine["car"] = my_cls, c["car"]
    rows_all = list(team_mates.values()) + ([mine] if mine else [])
    board = []
    # positions and gaps only inside one class; your own class first, then the others A to Z
    for cls in sorted({r["class"] for r in rows_all}, key=lambda k: (k != my_cls, k)):
        grp = sorted((r for r in rows_all if r["class"] == cls), key=lambda r: r["t"])
        for pos, r in enumerate(grp, 1):
            r["pos"] = pos
            r["gap"] = round(r["t"] - grp[0]["t"], 3)  # always >= 0
            if cls != my_cls and not any(x["is_me"] for x in grp):
                r["note"] = "You haven't driven this class here."
        board += grp
    entry["academy"] = [dict(_slim({**m, "gap": None}), car=m.get("car"), diff_car=m.get("diff_car")) for m in sorted(others.values(), key=lambda m: m["t"])]
    entry["mate_notes"] = [reason_text(r) for r in reasons]
    if not team_mates:
        entry["note"] = brand.join_title(group_name) + "." if not_member else (
            "No teammate lap here yet, so there is nothing to compare. They appear once a teammate sets a clean lap in this "
            "car at this track." if not reasons else
            "No teammate lap could be used here. Reasons are below.")
    # corner kings: one lap of yours + the fastest teammates, MAX_TEL_DRIVERS in all
    entries = []
    mates_all = [r for r in board if not r["is_me"] and r.get("class") == my_cls]
    cap = MAX_TEL_DRIVERS - (1 if my_ok else 0)
    mates_sorted = mates_all[:cap]
    if len(mates_all) > cap:
        left = ", ".join(r["name"] for r in mates_all[cap:])
        entry["kings_note"] = (f"Corners are compared for the {cap} quickest teammates only ({left} left out) "
                               "to keep Garage 61 requests down.")
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
            else:
                entry["mate_notes"].append(reason_text({"name": r["name"], "text": "lap time only, they aren't "
                                                        "sharing telemetry with the team"}))
    tl = next((g(it, "lap_length", "lapLength", "length") for it in track_items if g(it, "id") == tid), None)
    try:
        entry["kings"] = corner_kings(entries, tl)
    except Exception:  # noqa: BLE001  (one odd telemetry file shouldn't stop the hub)
        import traceback
        log("  Couldn't work out corner kings here:\n" + traceback.format_exc())
        entry["kings_note"] = "Couldn't compare corners here. Details were saved to debrief.log."
    if entry["kings"] is None and mates_sorted and not entry.get("kings_note"):
        entry["kings_note"] = "Not enough telemetry from these laps to compare corners."
    elif entry["kings"] is None and mates_sorted:
        entry["kings_note"] += " Not enough telemetry from these laps to compare corners."
    for r in board:
        r.pop("_lap", None)
    entry["board"] = board
    try:
        entry["learn"] = learn_for_combo(entry)
    except Exception:  # noqa: BLE001  (a hint is never worth breaking the hub)
        entry["learn"] = []


NO_SHARED = "no shared track yet"
PICK_SERIES = "pick a series on Prepare"
NO_TELEMETRY = "no telemetry to compare corners yet"


def roster(data) -> list:
    """2.19.2: every teammate the saved team data names, with what is missing for each, so the Team page can list
    them by name even when no series is picked. [{"key", "name", "shared", "missing": [str]}], by name. Words only: no
    statistic is changed. 'shared' = tracks and cars where both of you have a best lap."""
    if not isinstance(data, dict):
        return []
    me = (data.get("me") or {}).get("key") or "me"
    people = {}
    for c in data.get("combos") or []:
        board = [r for r in (c.get("board") or []) if isinstance(r, dict) and r.get("key") is not None]
        have_me = any(r["key"] == me for r in board)
        kings = c.get("kings") if isinstance(c.get("kings"), dict) else {}
        kdrivers = kings.get("drivers") or []
        for r in board:
            if r["key"] == me:
                continue
            p = people.setdefault(r["key"], {"key": r["key"], "name": str(r.get("name") or r["key"]), "shared": 0,
                                             "corners": False})
            if have_me:
                p["shared"] += 1
                if me in kdrivers and r["key"] in kdrivers:
                    p["corners"] = True
    wk = data.get("weekly") if isinstance(data.get("weekly"), dict) else {}
    for sr in wk.get("series") or []:
        for w in (sr.get("weeks") or []) if isinstance(sr, dict) else []:
            for r in (w.get("board") or []) if isinstance(w, dict) else []:
                if isinstance(r, dict) and r.get("key") not in (None, me) and r["key"] not in people:
                    people[r["key"]] = {"key": r["key"], "name": str(r.get("name") or r["key"]), "shared": 0,
                                        "corners": False}
    series_picked = bool([x for x in (wk.get("series") or []) if isinstance(x, dict)])
    out = []
    for p in people.values():
        missing = []
        if not p["shared"]:
            missing.append(NO_SHARED)
        elif not p["corners"]:
            missing.append(NO_TELEMETRY)
        if not series_picked:
            missing.append(PICK_SERIES)
        out.append({"key": p["key"], "name": p["name"], "shared": p["shared"], "missing": missing})
    out.sort(key=lambda p: (p["name"].lower(), str(p["key"])))
    return out


def _embed(obj) -> str:
    from . import report
    return report._embed(obj)  # escapes <, > and & so names can't break the script element


def _unit() -> str:
    try:
        from .desktop import settings_io
        return settings_io.units()
    except Exception:  # noqa: BLE001
        return "mph"


def _in_unit(data, unit):
    """2.21: the sentences in the learn rows say speeds in Settings > Units' unit (the raw numbers stay as measured)."""
    from . import units
    for c in data.get("combos") or []:
        for r in (c.get("learn") or []) if isinstance(c, dict) else []:
            if isinstance(r, dict) and isinstance(r.get("text"), str):
                r["text"] = units.convert_text(r["text"], unit)
    data["units"] = units.norm(unit)


def write(folder, data, unit=None) -> Path:
    from . import report
    try:
        _in_unit(data, unit or _unit())
    except Exception:  # noqa: BLE001  (words in the wrong unit are better than no page)
        data.setdefault("units", "mph")
    try:   # the join words name the Garage 61 group (from the data), not a brand constant
        gname = ((data.get("group") or {}).get("name")) or ((data.get("team") or {}).get("name")) or brand.GROUP_NAME
        data["brand"] = {**brand.as_dict(), "join_title": brand.join_title(gname), "join_text": brand.join_text(gname)}
    except Exception:  # noqa: BLE001
        pass
    out = Path(folder) / "team"
    out.mkdir(parents=True, exist_ok=True)
    tj = out / "team.json"
    tmpj = tj.with_name("team.json.tmp")
    try:
        data["roster"] = roster(data)       # 2.19.2: teammates by name, with what's missing, even with no series
    except Exception:  # noqa: BLE001  (a list of names is never worth breaking the hub)
        data["roster"] = []
    try:
        from . import members
        data["cards"] = members.cards(data)     # 2.22: the Members strip (saved data only)
    except Exception:  # noqa: BLE001
        data["cards"] = []
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


def mark_stale(library_folder, cfg) -> bool:
    """2.21: after a change to the team settings, say on the saved page that it was built with the old ones.
    No requests. True when the page was changed."""
    folder = Path(library_folder)
    try:
        data = json.loads((folder / "team" / "team.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict) or data.get("settings_stamp") in (None, settings_stamp(cfg)):
        return False
    data["stale"] = "Team settings changed. Press Refresh team data to see the new team."
    write(folder, data)
    return True


def render_saved(library_folder, cfg=None) -> Path:
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
    if isinstance(data.get("team"), dict) and data["team"].get("slug"):   # the Team page's own group, not the default
        _same_group(data["weekly"], data["team"]["slug"], data["team"].get("name"), data.get("membership"))
    if cfg is not None and data.get("settings_stamp") not in (None, settings_stamp(cfg)):
        data["stale"] = "Team settings changed. Press Refresh team data to see the new team."
    elif cfg is not None:
        data.pop("stale", None)
    wk = data["weekly"] if isinstance(data["weekly"], dict) else {}
    if wk.get("membership") in (brand.MEMBER, brand.NOT_MEMBER):   # the newest check wins
        data["membership"] = wk["membership"]
        if isinstance(wk.get("team"), dict):
            data["team"] = wk["team"]
    return write(folder, data)
