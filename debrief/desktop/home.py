"""2.18 Today: the window's start page (api/today). It reads what the other pages already worked out and asks
debrief.today which one thing to show. Each part is optional: a part that fails is left out (and logged), so the
page always opens. The Racing part is used only when the Racing page's answer is already cached, so opening Today
never reads every telemetry file."""
from __future__ import annotations

import json
import traceback
from pathlib import Path

from .. import app, today

GET_ROUTES = ("api/today",)


def _try(what, fn, default=None):
    try:
        return fn()
    except ValueError:
        return default
    except Exception:  # noqa: BLE001
        app.say(f"Today: the {what} part was left out:\n" + traceback.format_exc())
        return default


def _newest_review(h):
    lib = h._library_folder()
    if lib is None:
        return None, 0
    groups = h.server.reviews.list(lib)
    rows = [dict(r, track=g.get("track"), car=g.get("car")) for g in groups for r in g.get("reviews") or []]
    if not rows:
        return None, 0
    rows.sort(key=lambda r: (str(r.get("started_at") or r.get("date") or ""), str(r.get("dir"))), reverse=True)
    row = rows[0]
    data = None
    try:
        data = json.loads((Path(lib) / "reviews" / row["dir"] / "review.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    return today.last_race(row, data), len(rows)


def _racing_cached(h, cfg):
    from . import carsetup, racing
    inp = racing.inputs(cfg)
    key = racing.cache_key(inp)
    st = carsetup.state(h.server)
    with st.lock:
        known = key in st.mem
    if not known and not (Path(cfg["cache_dir"]) / racing.CACHE / (key + ".json")).is_file():
        return None
    val, _hit = carsetup.cached(h.server, cfg["cache_dir"], key, lambda: {}, folder_name=racing.CACHE)
    return today.racing_habit((val or {}).get("starts"))


def week_race(cfg):
    """2.28 "This week's race": the series you starred (or the one last prepared) and the track it races this week, so
    Today can open Prepare on it in one click. {"state": "ready", ...} or {"state": "pick"} when nothing is known yet
    (no season schedule, no starred series, nothing prepared before). Reads files only."""
    from datetime import datetime, timezone
    from .. import schedule, series
    lib = cfg["library_folder"]
    sched = schedule.load_current()
    if not sched:
        return {"state": "pick", "why": "no_schedule"}
    day = datetime.now(timezone.utc).date()
    wk = schedule.week_on(sched, day)
    choice = series.load_choice(lib)
    mine = [x for x in series.resolve(series.load(lib), sched) if not x.get("missing")]
    ser = schedule.find_series(sched, choice["series"]) if choice.get("series") else None
    how = "last"
    if ser is None and not choice.get("track") and mine:
        ser, how = mine[0], "starred"
    elif ser is not None and any(x.get("id") == ser.get("id") for x in mine):
        how = "starred"
    if ser is None:
        if choice.get("track"):
            return {"state": "ready", "how": "last", "series": None, "track": choice["track"], "car": choice.get("car"), "week": None}
        return {"state": "pick", "why": "nothing_chosen"}
    w = next((x for x in ser.get("weeks") or [] if x.get("season_week", x.get("week")) == wk), None) if wk else None
    if w is None:
        return {"state": "ready", "how": how, "series": {"id": ser.get("id"), "name": ser.get("name")}, "track": None,
                "car": choice.get("car"), "week": wk}
    from .. import library
    return {"state": "ready", "how": how, "series": {"id": ser.get("id"), "name": ser.get("name")}, "track": w.get("track"),
            "short": library.short_track_name(w.get("track")), "car": choice.get("car"), "week": wk, "start": w.get("start")}


def get_today(h, q):
    if set(q):
        raise ValueError("Bad request.")
    cfg = h._cfg()
    review, n_reviews = _try("last race", lambda: _newest_review(h), (None, 0))
    ss = _try("practice", lambda: h.server.practice_sessions(cfg)[0], []) or []
    practice = today.last_practice(ss[0]) if ss else None
    if ss and cfg.get("night_shift_folder"):   # 2.30.2: last week's unsent sessions to the Zerver, in the background
        from . import siyanqaba as siyq
        _try("night shift folder", lambda: siyq.auto_push(h.server, cfg, ss))
    plan = _try("practice plan", lambda: h._plan_reply(cfg, {})) if ss else None
    habit = _try("racing", lambda: _racing_cached(h, cfg))
    from .server import setuplab_cache_path
    setup = _try("setup", lambda: app.optional_module("firstrun").walkthrough(cache_file=setuplab_cache_path(cfg)))
    card = today.pick(plan=plan, review=review, racing=habit, practice=practice, setup=setup)
    wr = _try("this week's race", lambda: week_race(cfg), {"state": "pick", "why": "error"})
    from .server import _jsonable
    return h._json(_jsonable({
        "ok": True, "one": card, "last_race": review, "last_practice": practice, "reviews": n_reviews,
        "sessions": len(ss), "plan": (plan or {}).get("plan"), "setup": setup,
        "setup_done": today.setup_done(setup), "has_data": bool(review or ss), "week_race": wr}))


def get(h, r, q):
    return {"api/today": get_today}[r](h, q)
