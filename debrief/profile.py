"""Skill trends across races (Debrief 2.3): the library's Skills tab.

Input is library rows (``library.list_reviews``) that carry the compact ``skills`` shape
({"v", "n_clean", "lap_t", "loss_pm": {...}}). Scores are **recomputed from loss_pm** with the current
``skills.CAPS_PM``, so retuning the caps re-scores old races without touching any review.

Each race compares your typical laps with your own better laps in that race; lower time lost means your
typical laps were closer to your best driving. Trends are a robust (Theil-Sen) slope and are only given
a direction with enough races over enough time AND when a Mann-Kendall test (two-sided, ties handled,
normal approximation with continuity correction) gives p < 0.05. Race-to-race spread of loss_pm is about
1.5 per-mille, so without the test most "Improving"/"Slipping" words on flat data were noise (2.3.0
verification H1). With 4 races the test can't reach p < 0.05, so a direction needs 5 or more races.

Garage 61 request budget (spec 1.6): technique_plus, skills, session_review, profile, heat map: **0**.
Library refresh for layout 2.3.0: **0**. This module never makes a request.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timezone

import numpy as np

from . import skills

VERSION = 1
PHASES = ("braking", "entry", "mid", "exit", "overall")
MIN_CLEAN = 6
TREND_MIN_RACES = 4
TREND_MIN_DAYS = 14
TREND_MIN_CHANGE_PM = 0.5
TREND_MAX_P = 0.05
TREND_TEST = "mann_kendall"
WEAKEST_LAST = 5


def _num(x):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _r(x, nd=2):
    x = _num(x)
    return None if x is None else round(x, nd)


def _ts(row) -> float:
    from . import library
    return library._ts(row)


def _day(ts: float) -> date:
    return datetime.fromtimestamp(ts, tz=timezone.utc).date()


def theil_sen(xs, ys):
    """Median of pairwise slopes (robust to one odd race). None with fewer than 2 distinct xs."""
    sl = [(ys[j] - ys[i]) / (xs[j] - xs[i]) for i in range(len(xs)) for j in range(i + 1, len(xs))
          if xs[j] != xs[i]]
    return float(np.median(sl)) if sl else None


def mann_kendall(ys):
    """Two-sided Mann-Kendall trend test on ys in time order. Returns (S, p). Ties (equal values) are
    handled with the usual variance correction; p uses the normal approximation with continuity
    correction. p is 1.0 when every value is equal or there are fewer than 3 values."""
    ys = [float(y) for y in ys]
    n = len(ys)
    if n < 3:
        return 0, 1.0
    S = 0
    for i in range(n - 1):
        for j in range(i + 1, n):
            d = ys[j] - ys[i]
            S += (d > 0) - (d < 0)
    counts = {}
    for y in ys:
        counts[y] = counts.get(y, 0) + 1
    var = (n * (n - 1) * (2 * n + 5) - sum(t * (t - 1) * (2 * t + 5) for t in counts.values() if t > 1)) / 18.0
    if var <= 0 or S == 0:
        return S, 1.0
    z = (S - (1 if S > 0 else -1)) / math.sqrt(var)
    return S, float(math.erfc(abs(z) / math.sqrt(2.0)))


def _usable(row):
    s = row.get("skills") if isinstance(row, dict) else None
    if not isinstance(s, dict) or not isinstance(s.get("loss_pm"), dict):
        return None
    n = _num(s.get("n_clean"))
    if n is None or n < MIN_CLEAN:
        return None
    return s


def _class_key(car):
    from . import schedule
    try:
        c = schedule.class_of(car)
    except Exception:
        c = None
    return str(c or car or "Unknown car")


def _trend(races):
    n = len(races)
    if n < TREND_MIN_RACES:
        return None
    t0 = races[0]["_t"]
    xs = [(r["_t"] - t0) / 86400.0 for r in races]
    ys = [r["loss_pm"] for r in races]
    days = xs[-1] - xs[0]
    s = theil_sen(xs, ys)
    if s is None:
        return None
    per_week = s * 7.0
    change = s * days
    mk_s, p = mann_kendall(ys)
    # spec 1.3 + 2.3.0 H1: a direction only with >= 4 races over >= 14 days, a change of >= 0.5 per-mille
    # AND Mann-Kendall p < 0.05 with the same sign; otherwise "steady" (span_ok tells the page whether
    # the window was long enough to judge: "Not enough races yet")
    if (days < TREND_MIN_DAYS or abs(change) < TREND_MIN_CHANGE_PM or p >= TREND_MAX_P
            or (mk_s < 0) != (change < 0)):
        direction = "steady"
    else:
        direction = "improving" if change < 0 else "slipping"
    return {"slope_pm_per_week": _r(per_week, 3), "change_pm": _r(change, 2), "direction": direction,
            "n": n, "days": int(round(days)), "span_ok": days >= TREND_MIN_DAYS,
            "test": TREND_TEST, "p": round(p, 4), "mk_s": int(mk_s)}


def _phase_block(items, phase):
    races = []
    for row, s, t in items:
        v = _num(s["loss_pm"].get(phase))
        if v is None:
            continue
        races.append({"dir": row.get("dir"), "date": row.get("date"), "track_short": row.get("track_short"),
                      "car": row.get("car"), "n_clean": int(_num(s.get("n_clean"))), "loss_pm": round(v, 2),
                      "score": skills.score(v, phase), "_t": t})
    if not races:
        return None
    weekly, seasons = {}, {}
    for r in races:
        y, w, _ = _day(r["_t"]).isocalendar()
        weekly.setdefault(f"{y}-W{w:02d}", []).append(r["loss_pm"])
    for (row, s, t) in items:
        v = _num(s["loss_pm"].get(phase))
        if v is None:
            continue
        nm = (row.get("season") or {}).get("name") if isinstance(row.get("season"), dict) else None
        seasons.setdefault(str(nm or "Unknown season"), []).append(v)
    scored = [r["score"] for r in races if r["score"] is not None]
    out = {"latest": races[-1]["score"], "latest_loss_pm": races[-1]["loss_pm"],
           "best_score": max(scored) if scored else None,
           "trend": _trend(races),
           "weekly": [{"week": k, "loss_pm": _r(float(np.median(v))), "n": len(v)} for k, v in sorted(weekly.items())],
           "seasons": [{"name": k, "loss_pm": _r(float(np.median(v))), "n": len(v)} for k, v in seasons.items()],
           "races": [{k: v for k, v in r.items() if k != "_t"} for r in races]}
    return out


def _group(key, label, items):
    items = sorted(items, key=lambda x: (x[2], str(x[0].get("dir"))))
    phases = {}
    for p in PHASES:
        b = _phase_block(items, p)
        if b:
            phases[p] = b
    return {"key": key, "label": label, "races": len(items), "phases": phases}


def build(rows: list) -> dict:
    """Profile for the library Skills tab (see spec 1.3). Never raises on odd rows; empty input gives
    {"groups": [], "weakest": None, "n_races": 0}."""
    items = []
    for row in rows or []:
        s = _usable(row)
        if s is None:
            continue
        try:
            t = _ts(row)
        except Exception:
            t = 0.0
        items.append((row, s, t))
    out = {"version": VERSION, "caps_pm": {k: v for k, v in skills.CAPS_PM.items()},
           "params": {"min_clean": MIN_CLEAN, "trend_min_races": TREND_MIN_RACES,
                      "trend_min_days": TREND_MIN_DAYS, "trend_min_change_pm": TREND_MIN_CHANGE_PM,
                      "trend_method": "theil_sen", "trend_test": TREND_TEST, "trend_max_p": TREND_MAX_P,
                      "weakest_last": WEAKEST_LAST},
           "groups": [], "weakest": None, "n_races": len(items)}
    if not items:
        return out
    out["groups"].append(_group("all", "All cars", items))
    by_cls = {}
    for it in items:
        by_cls.setdefault(_class_key(it[0].get("car")), []).append(it)
    for k in sorted(by_cls, key=lambda k: (-len(by_cls[k]), k)):
        out["groups"].append(_group("class:" + k, k, by_cls[k]))
    allg = out["groups"][0]["phases"]
    worst, wv = None, None
    for p in ("braking", "entry", "mid", "exit"):
        rs = (allg.get(p) or {}).get("races") or []
        last = [r["loss_pm"] for r in rs[-WEAKEST_LAST:]]
        if not last:
            continue
        m = float(np.median(last))
        if wv is None or m > wv:
            worst, wv = p, m
    if worst is not None:
        out["weakest"] = {"group": "all", "phase": worst, "median_loss_pm": _r(wv)}
    return out
