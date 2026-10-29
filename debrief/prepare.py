"""The Prepare page's data: your series, this week's and next week's tracks, your history there and the
track cards. Reads only files on this PC (the saved schedule, the synced history, the library); never
sends a request.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from . import notes, schedule, series


def _when(s):
    return str(s.get("start") or "")


def history_at(sessions: list, track: str, cars: list) -> dict:
    """Your synced sessions at a track (any of the series' cars): counts, best clean lap, last visit,
    and the Garage 61 ids to build a prep page with."""
    rows = [s for s in sessions if schedule.same_track(s.get("track"), track)]
    in_car = [s for s in rows if not cars or any(schedule.same_car(s.get("car"), c) for c in cars)]
    use = in_car or []
    races = [s for s in use if s.get("type") == 3]
    bests = [s["best_clean"] for s in use if isinstance(s.get("best_clean"), (int, float))]
    last = max(use, key=_when) if use else None
    any_last = max(rows, key=_when) if rows else None
    return {"sessions": len(use), "races": len(races), "best": min(bests) if bests else None,
            "last": (last or {}).get("start"), "car": (last or {}).get("car"),
            "track_id": (last or any_last or {}).get("track_id"), "car_id": (last or {}).get("car_id"),
            "other_car_sessions": len(rows) - len(use)}


def _ids_from_lists(api, track: str, cars: list):
    """Garage 61 ids for a schedule track/car from the cached lists (no requests). (None, None) if unknown."""
    if api is None:
        return None, None
    from .app import item_name
    tracks = api.cached_items("tracks") or []
    car_items = api.cached_items("cars") or []
    tid = next((t.get("id") for t in tracks if isinstance(t, dict) and schedule.same_track(item_name(t), track)), None)
    cid = None
    for c in cars or []:
        cid = next((x.get("id") for x in car_items if isinstance(x, dict) and schedule.same_car(item_name(x), c)), None)
        if cid:
            break
    return tid, cid


def _week_view(ser, wk_no, sessions, library_folder, api):
    w = next((w for w in ser["weeks"] if w["week"] == wk_no), None)
    if w is None:
        return None
    hist = history_at(sessions, w["track"], ser.get("cars") or [])
    tid, cid = hist["track_id"], hist["car_id"]
    if tid is None or cid is None:
        lt, lc = _ids_from_lists(api, w["track"], ser.get("cars") or [])
        tid, cid = tid or lt, cid or lc
    card = notes.load(library_folder, w["track"])
    return {"week": w["week"], "start": w["start"], "track": w["track"], "length": w.get("length"),
            "race_time": w.get("race_time"), "conditions": w.get("conditions"), "history": hist,
            "track_id": tid, "car_id": cid, "card": card, "has_notes": notes.has_notes(card)}


def view(cfg: dict, api=None, sessions: list | None = None, today: date | None = None) -> dict:
    today = today or datetime.now(timezone.utc).date()
    lib = cfg["library_folder"]
    sched = schedule.load_current(today)
    mine = series.load(lib)
    out = {"ok": True, "schedule": schedule.summary(sched), "chosen": mine["chosen"], "mine": [],
           "suggestions": [], "all": [], "token": api is not None}
    if not sched:
        return out
    sessions = sessions or []
    wk = schedule.week_on(sched, today)
    a, _ = schedule.season_range(sched)
    if wk is None:
        wk = 1 if a and today < a else None
    for s in series.resolve(mine, sched):
        row = {"id": s["id"], "name": s["name"], "missing": bool(s.get("missing"))}
        if not s.get("missing"):
            row.update({"category": s.get("category"), "cars": s.get("cars") or [], "license": s.get("license"),
                        "classes": s.get("classes") or [], "multiclass": s.get("multiclass", False),
                        "setup": s.get("setup"), "frequency": s.get("frequency"),
                        "this_week": _week_view(s, wk, sessions, lib, api) if wk else None,
                        "next_week": _week_view(s, wk + 1, sessions, lib, api) if wk else None,
                        "season": [{"week": w["week"], "start": w["start"], "track": w["track"],
                                    "length": w.get("length"), "has_notes": notes.has_notes(notes.load(lib, w["track"]))}
                                   for w in s["weeks"]]})
        out["mine"].append(row)
    out["suggestions"] = series.suggest(sched, sessions, today)
    for s in sched.get("series") or []:
        w = next((w for w in s["weeks"] if w["week"] == wk), None) if wk else None
        out["all"].append({"id": s["id"], "name": s["name"], "category": s.get("category"),
                           "license": s.get("license"), "cars": s.get("cars") or [],
                           "multiclass": s.get("multiclass", False), "setup": s.get("setup"),
                           "track": (w or {}).get("track")})
    out["current_week"] = wk
    return out
