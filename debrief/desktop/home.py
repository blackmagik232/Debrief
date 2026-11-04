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


def get_today(h, q):
    if set(q):
        raise ValueError("Bad request.")
    cfg = h._cfg()
    review, n_reviews = _try("last race", lambda: _newest_review(h), (None, 0))
    ss = _try("practice", lambda: h.server.practice_sessions(cfg)[0], []) or []
    practice = today.last_practice(ss[0]) if ss else None
    plan = _try("practice plan", lambda: h._plan_reply(cfg, {})) if ss else None
    habit = _try("racing", lambda: _racing_cached(h, cfg))
    from .server import setuplab_cache_path
    setup = _try("setup", lambda: app.optional_module("firstrun").walkthrough(cache_file=setuplab_cache_path(cfg)))
    card = today.pick(plan=plan, review=review, racing=habit, practice=practice, setup=setup)
    from .server import _jsonable
    return h._json(_jsonable({
        "ok": True, "one": card, "last_race": review, "last_practice": practice, "reviews": n_reviews,
        "sessions": len(ss), "plan": (plan or {}).get("plan"), "setup": setup,
        "setup_done": today.setup_done(setup), "has_data": bool(review or ss)}))


def get(h, r, q):
    return {"api/today": get_today}[r](h, q)
