"""The Prepare page's data: your series, this week's and next week's tracks, your history there and the
track cards. Reads only files on this PC (the saved schedule, the synced history, the library); never
sends a request.
"""
from __future__ import annotations

import statistics as st
from datetime import date, datetime, timedelta, timezone

from . import library, notes, schedule, series

PLAN_FLAGS = 3          # at most this many weeks are flagged as needing the most prep
STALE_DAYS = 180        # raced here, but this long ago


def _when(s):
    return str(s.get("start") or "")


def history_at(sessions: list, track: str, cars: list, matcher=None) -> dict:
    """Your synced sessions at a track (any of the series' cars): counts, best clean lap, last visit,
    and the Garage 61 ids to build a prep page with. Matching is strict (by Garage 61 id where the cached
    lists name one, else by name): another layout's or another car's laps never count."""
    m = matcher or schedule.Matcher()
    rows = [s for s in sessions if isinstance(s, dict) and m.track(s, track)]
    in_car = [s for s in rows if not cars or any(m.car(s, c) for c in cars)]
    use = in_car or []
    races = [s for s in use if s.get("type") == 3]
    bests = [s["best_clean"] for s in use if isinstance(s.get("best_clean"), (int, float))]
    last = max(use, key=_when) if use else None
    any_last = max(rows, key=_when) if rows else None
    return {"sessions": len(use), "races": len(races), "best": min(bests) if bests else None,
            "last": (last or {}).get("start"), "car": (last or {}).get("car"),
            "track_id": (last or any_last or {}).get("track_id"), "car_id": (last or {}).get("car_id"),
            "other_car_sessions": len(rows) - len(use)}


def matcher_for(api):
    """A schedule.Matcher over Garage 61's cached track and car lists (no requests; names only if none)."""
    if api is None:
        return schedule.Matcher()
    try:
        from .app import item_name
        return schedule.Matcher(api.cached_items("tracks") or [], api.cached_items("cars") or [], item_name)
    except Exception:  # noqa: BLE001  (no lists saved yet: match by name)
        return schedule.Matcher()


def _ids_from_lists(api, track: str, cars: list, matcher=None):
    """Garage 61 ids for a schedule track/car from the cached lists (no requests). None for either one
    that's unknown or ambiguous (two layouts or cars fit the name): a wrong prep page is worse than none."""
    if api is None and matcher is None:
        return None, None
    m = matcher or matcher_for(api)
    tid = m.track_id(track)
    cid = None
    for c in cars or []:
        cid = m.car_id(c)
        if cid is not None:
            break
    return tid, cid


def _race_length(cfg, sched, track, cars, week):
    """The race length from the schedule week: strategy.race_length(week), which is exactly what
    strategy.prefill(...)["race"] gives, without reading the library's reviews for every week (2.4.0
    review L4). No requests, no store. The full prefill is loaded on demand by api/strategy."""
    try:
        from . import strategy
    except ImportError:   # strategy.py not merged yet
        return None
    try:
        if callable(getattr(strategy, "race_length", None)):
            return strategy.race_length(week)
        return strategy.prefill(cfg, None, sched, track, cars, week).get("race")
    except Exception:  # noqa: BLE001  (a fuel hint is never worth breaking the Prepare page)
        return None


def _shared_count(cfg, track, me_slug) -> int:
    if not cfg:
        return 0
    try:
        return len(notes.shared_for(cfg, track, me_slug))
    except Exception:  # noqa: BLE001
        return 0


def warmups(ser, week, starred, today: date | None = None) -> list:
    """Your other starred series that race this track earlier: extra laps before this week's race. Latest first, at most 3.
    With today, weeks already over are left out: you can't go back and race them."""
    out = []
    for o in starred:
        if o.get("id") == ser.get("id") or o.get("name", "").replace(" - Fixed", "") == ser.get("name", "").replace(" - Fixed", ""):
            continue
        for w in o.get("weeks") or []:
            if w.get("start") and w["start"] < week["start"] and w.get("season_week", 1) and schedule.same_track(w.get("track"), week.get("track")):
                if today and date.fromisoformat(w["start"]) + timedelta(days=7) <= today:
                    continue
                out.append({"series": o.get("name"), "start": w["start"]})
    return sorted(out, key=lambda x: x["start"], reverse=True)[:3]


def _week_view(ser, wk_no, sessions, library_folder, api, matcher=None, cfg=None, sched=None, me_slug=None):
    w = next((w for w in ser["weeks"] if w.get("season_week", w["week"]) == wk_no), None)
    if w is None:
        return None
    m = matcher or matcher_for(api)
    cars = schedule.week_cars(ser, w)
    hist = history_at(sessions, w["track"], cars, m)
    tid, cid = hist["track_id"], hist["car_id"]
    if tid is None or cid is None:
        lt, lc = _ids_from_lists(api, w["track"], cars, m)
        tid, cid = tid or lt, cid or lc
    card = notes.load(library_folder, w["track"])
    return {"week": w["week"], "start": w["start"], "track": w["track"], "length": w.get("length"), "cars": cars,
            "race_time": w.get("race_time"), "conditions": w.get("conditions"), "history": hist,
            "track_id": tid, "car_id": cid, "card": card, "has_notes": notes.has_notes(card),
            "shared_notes": _shared_count(cfg, w["track"], me_slug),
            "strategy": _race_length(cfg, sched, w["track"], cars, w) if cfg else None}


# ---------------------------------------------------------------- season plan
_STATUS = {"new": "New to you", "other_car": "New in this car", "practised": "Practised, not raced",
           "raced": "Raced here", "no_history": "No history"}


def _pace(rows, sessions, wet: set) -> dict | None:
    """Best and typical lap from reviews and synced sessions in one set of cars. Rows are one track. Wet races are
    kept apart: they'd drag the typical lap, and the driver may want them on a rain-possible week. Typical comes
    from reviews only (a synced session has no median). None when no lap counts."""
    wet_days = {str(r.get("date") or "")[:10] for r in rows if r.get("dir") in wet}
    cands = [{"t": r["best"], "date": r.get("date"), "car": r.get("car"), "wet": r.get("dir") in wet}
             for r in rows if r.get("best_clean", True) and isinstance(r.get("best"), (int, float))]
    cands += [{"t": s["best_clean"], "date": str(s.get("start") or "")[:10], "car": s.get("car"), "wet": str(s.get("start") or "")[:10] in wet_days}
              for s in sessions if isinstance(s.get("best_clean"), (int, float)) and s["best_clean"] > 0]
    if cands:
        floor = 0.6 * st.median(c["t"] for c in cands)     # a "best" can be a partial lap
        cands = [c for c in cands if c["t"] >= floor]
    if not cands:
        return None
    dry = [c for c in cands if not c["wet"]]
    b = min(dry or cands, key=lambda c: c["t"])
    races = [r for r in rows if library.is_race(r) and library._norm(r.get("car")) == library._norm(b["car"])]
    meds = [r["median"] for r in races if r.get("dir") not in wet and isinstance(r.get("median"), (int, float))]
    wet_rows = [r for r in rows if library.is_race(r) and r.get("dir") in wet and r.get("best_clean", True)
                and library._norm(r.get("car")) == library._norm(b["car"])]
    return {"best": round(b["t"], 3), "best_date": b["date"], "car": b["car"], "best_wet": b["wet"],
            "typical": round(st.median(meds), 3) if meds else None, "typical_races": len(meds),
            "wet_races": len(wet_rows), "wet_best": min((r["best"] for r in wet_rows), default=None)}


def _local_day(v) -> str:
    """A timestamp's date on this PC's calendar: a race on the evening of the 2nd is in UTC already the 3rd."""
    from datetime import datetime
    text = str(v or "")
    try:
        d = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return (d.astimezone() if d.tzinfo else d).date().isoformat()
    except ValueError:
        return text[:10]


def _past_at(rows, sessions, track, cars, m, wet) -> dict:
    """Everything you've done at one track: status for these cars, pace (labelled when it's another car's)."""
    here = [r for r in rows if m.track(r, track)]
    in_car = [r for r in here if not cars or any(m.car(r, c) for c in cars)]
    sess_here = [s for s in sessions if m.track(s, track)]
    sess_car = [s for s in sess_here if not cars or any(m.car(s, c) for c in cars)]
    # a review and a synced session are usually the same race, so these are never added together
    races = max(sum(1 for r in in_car if library.is_race(r)), sum(1 for s in sess_car if s.get("type") == 3))
    visits = max(len(in_car), len(sess_car))
    other = len(here) + len(sess_here) > 0 and not visits
    status = "raced" if races else "practised" if visits else "other_car" if other else "new"
    pace = _pace(here if other else in_car, sess_here if other else sess_car, wet)
    if pace:
        pace["other_car"] = other
    use = (here + sess_here) if other else (in_car + sess_car)
    last = max((_local_day(x.get("started_at") or x.get("start") or x.get("date")) for x in use), default=None)
    src = next((x for x in in_car + sess_car if x.get("track_id") is not None and x.get("car_id") is not None), {})
    return {"status": status, "races": races, "sessions": visits, "pace": pace, "last": last or None,
            "track_id": src.get("track_id"), "car_id": src.get("car_id")}


def _past_text(p: dict) -> str:
    if p["status"] == "raced":
        return f"{p['races']} race{'' if p['races'] == 1 else 's'} here on record."
    if p["status"] == "practised":
        return f"{p['sessions']} session{'' if p['sessions'] == 1 else 's'} here on record, no race yet."
    if p["status"] == "other_car":
        return "You've driven here, but not in this series' cars."
    return "No laps on record here."


def _plan_warmups(ser, w, others, today) -> list:
    out = []
    cars = {schedule.norm(c) for c in schedule.week_cars(ser, w)}
    for x in warmups(ser, w, others, today):
        o = next((o for o in others if o.get("name") == x["series"]), {})
        ow = next((y for y in o.get("weeks") or [] if y.get("start") == x["start"]), {})
        out.append({**x, "week": ow.get("week"),
                    "same_cars": bool(cars & {schedule.norm(c) for c in schedule.week_cars(o, ow)})})
    return out


def _flag_rank(r: dict, today: date):
    """(tier, no same-car warm-up first, soonest first), or None when this race needs no flag. Rules only."""
    tier = {"new": 0, "other_car": 1, "practised": 2}.get(r["status"])
    if tier is None and r["status"] == "raced" and r["last"]:
        try:
            if (today - date.fromisoformat(r["last"])).days >= STALE_DAYS:
                tier = 3
        except ValueError:
            pass
    if tier is None:
        return None
    return tier, any(x["same_cars"] for x in r["warmups"]), r["start"]


def _flag_reason(r: dict, today: date) -> str:
    first = {"new": "New to you", "other_car": "You've driven here, but not in these cars",
             "practised": "You've never raced here in these cars"}.get(r["status"])
    if first is None:
        first = f"You last raced here {(today - date.fromisoformat(r['last'])).days // 30} months ago"
    same = [x for x in r["warmups"] if x["same_cars"]]
    if same:
        return f"{first}. You can get earlier laps in {same[0]['series']}."
    if r["warmups"]:
        return f"{first}. Earlier laps only in different cars, in {r['warmups'][0]['series']}."
    return f"{first}, and none of your other series race here earlier."


def plan(library_folder, sched, mine: dict, sessions: list, matcher=None, today: date | None = None) -> dict:
    """The rest of the season, week by week, for your own series only. Facts from the schedule, your saved reviews
    and your synced sessions; no targets and no requests. A series you've never driven the cars of shows no history
    and no flags: Debrief can't tell which cars you own, and flagging every week of it would say nothing."""
    today = today or datetime.now(timezone.utc).date()
    m = matcher or schedule.Matcher()
    from . import prep
    rows = [r for r in library.list_reviews(library_folder) if not r.get("test_data")]
    sessions = [s for s in sessions or [] if isinstance(s, dict)]
    out = {"state": "ok", "season": (sched or {}).get("season"), "weeks": [], "flagged": [], "series": [],
           "reviews": len(rows), "synced_sessions": len(sessions), "note": None}
    resolved = series.resolve(mine, sched)
    live = [s for s in resolved if not s.get("missing")]
    out["series"] = [{"id": s["id"], "name": s["name"], "missing": True,
                      "note": f"{s['name']} isn't in the loaded schedule."} for s in resolved if s.get("missing")]
    if not live:
        out["state"] = "no_series"
        return out
    if not rows and not sessions:
        out["note"] = "No saved reviews or synced sessions yet, so Debrief has no history to compare."
    current = schedule.week_on(sched, today)
    wet = prep.wet_dirs(library_folder, [r for r in rows if library.is_race(r)])
    weeks, races = {}, []
    for ser in live:
        left = [w for w in ser.get("weeks") or [] if series.week_left(w, current, today)]
        season_cars = {c for w in ser.get("weeks") or [] for c in schedule.week_cars(ser, w)}
        driven = (not season_cars or any(m.car(x, c) for x in rows + sessions for c in season_cars))
        if not driven and (rows or sessions):
            out["series"].append({"id": ser["id"], "name": ser["name"], "no_history": True,
                                  "note": f"No laps on record in the cars of {ser['name']}. If you don't own them yet, "
                                          "that's why its weeks show no history."})
        for w in left:
            cars = schedule.week_cars(ser, w)
            past = _past_at(rows, sessions, w["track"], cars, m, wet)
            if past["status"] == "new" and (not driven or not (rows or sessions)):
                past["status"] = "no_history"
            # a new track: the car you actually drive, not the first one the series lists
            mine_first = [c for c in cars if any(m.car(x, c) for x in list(rows) + list(sessions))]
            tid, cid = _ids_from_lists(None, w["track"], mine_first + [c for c in cars if c not in mine_first], m)
            tid, cid = past.pop("track_id") or tid, past.pop("car_id") or cid
            no = w.get("season_week", w["week"])
            row = {"series_id": ser["id"], "series": ser["name"], "track": w["track"], "week": w["week"],
                   "season_week": no, "start": w["start"], **series.week_facts(w), **past,
                   "status_text": _STATUS[past["status"]], "detail": _past_text(past),
                   "warmups": _plan_warmups(ser, w, live, today), "flag": False, "flag_reason": None,
                   "track_id": tid, "car_id": cid, "_driven": driven}
            races.append(row)
            weeks.setdefault(no, {"season_week": no, "start": w["start"], "now": no == current, "flag": False,
                                  "races": []})["races"].append(row)
    # the same track in two of your series the same week is one thing to prepare, so one flag between them
    ranked = sorted(((_flag_rank(r, today), r) for r in races if r["_driven"]), key=lambda x: x[0] or (9,))
    seen = {}
    for rank, r in ranked:
        if rank is None:
            continue
        key = (r["season_week"], schedule.norm(r["track"]))
        if key not in seen:
            if len(seen) >= PLAN_FLAGS:
                continue
            seen[key] = r
            out["flagged"].append({"season_week": r["season_week"], "track": r["track"], "series": r["series"]})
        r["flag"], r["flag_reason"] = True, _flag_reason(r, today)
        weeks[r["season_week"]]["flag"] = True
    for r in races:
        r.pop("_driven")
    out["weeks"] = [weeks[k] for k in sorted(weeks)]
    if not out["weeks"]:
        out["state"] = "season_over"
    return out


def view(cfg: dict, api=None, sessions: list | None = None, today: date | None = None) -> dict:
    today = today or datetime.now(timezone.utc).date()
    lib = cfg["library_folder"]
    sched = schedule.load_current(today)
    mine = series.load(lib)
    out = {"ok": True, "schedule": schedule.summary(sched), "chosen": mine["chosen"], "mine": [],
           "suggestions": [], "all": [], "token": api is not None}
    if not sched:
        return out
    sessions = [s for s in sessions or [] if isinstance(s, dict)]
    m = matcher_for(api)
    wk = schedule.week_on(sched, today)
    a, _ = schedule.season_range(sched)
    if wk is None:
        wk = 1 if a and today < a else None
    me = notes.author_from_cache(cfg) if cfg.get("team_cards_folder") else None
    extra = {"cfg": cfg, "sched": sched, "me_slug": (me or {}).get("slug")}
    others = [x for x in series.resolve(mine, sched) if not x.get("missing")]
    for s in series.resolve(mine, sched):
        row = {"id": s["id"], "name": s["name"], "missing": bool(s.get("missing"))}
        if not s.get("missing"):
            row.update({"category": s.get("category"), "cars": s.get("cars") or [], "license": s.get("license"),
                        "classes": s.get("classes") or [], "multiclass": s.get("multiclass", False),
                        "setup": s.get("setup"), "frequency": s.get("frequency"),
                        "this_week": _week_view(s, wk, sessions, lib, api, m, **extra) if wk else None,
                        "next_week": _week_view(s, wk + 1, sessions, lib, api, m, **extra) if wk else None,
                        "season": [{"week": w["week"], "season_week": w.get("season_week", w["week"]), "start": w["start"], "track": w["track"], "warmups": warmups(s, w, others),
                                    "length": w.get("length"), "has_notes": notes.has_notes(notes.load(lib, w["track"]))}
                                   for w in s["weeks"]]})
        out["mine"].append(row)
    out["suggestions"] = series.suggest(sched, sessions, today, m)
    for s in sched.get("series") or []:
        w = next((w for w in s["weeks"] if w.get("season_week", w["week"]) == wk), None) if wk else None
        out["all"].append({"id": s["id"], "name": s["name"], "category": s.get("category"),
                           "license": s.get("license"), "cars": s.get("cars") or [],
                           "multiclass": s.get("multiclass", False), "setup": s.get("setup"),
                           "track": (w or {}).get("track")})
    out["current_week"] = wk
    try:
        out["plan"] = plan(lib, sched, mine, sessions, m, today)
    except Exception:  # noqa: BLE001  (the plan must never take the rest of the Prepare page down with it)
        out["plan"] = {"state": "error"}
    return out
