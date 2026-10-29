"""Practice and qualifying reviews: runs, progress, targets for next session, and the qualifying lap
strip (Debrief 2.3). Review dict only: works on saved reviews too.

Runs are split at: (a) a gap in lap numbers (an out lap, in lap or pit lap sat between), (b) a break
in lap start times longer than 1.6 x the previous lap + 30 s (when both starts are known), and
(c) the fuel level going up by 1 L or more (a refuel; when the review has per-lap fuel).

Garage 61 request budget (spec 1.6): technique_plus, skills, session_review, profile, heat map: **0**.
This module never makes a request.
"""
from __future__ import annotations

import math

import numpy as np

VERSION = 1
MIN_RUN_LAPS = 3        # a run needs this many laps for a median
MIN_TREND_CLEAN = 5     # clean laps in a run for a trend
MIN_PROGRESS_CLEAN = 3  # clean laps per run for the progress line
MIN_TARGET_CLEAN = 5    # clean laps in a corner (and in the run used) for a target
MIN_GAIN_S = 0.03       # smaller gains are within lap-to-lap noise
MAX_TARGETS = 5
REFUEL_L = 1.0
BREAK_FACTOR, BREAK_PAD_S = 1.6, 30.0
PUSH_WITHIN = 0.01      # quali: later laps within 1 % of best are "push" laps


def _num(x):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _r(x, nd=3):
    x = _num(x)
    return None if x is None else round(x, nd)


def _median(xs):
    xs = [x for x in (_num(v) for v in xs) if x is not None]
    return float(np.median(xs)) if xs else None


def _slope(xs, ys):
    if len(xs) < 2 or len(set(xs)) < 2:
        return None
    return float(np.polyfit(np.asarray(xs, float), np.asarray(ys, float), 1)[0])


def _kind(data):
    s = str(((data.get("meta") or {}) if isinstance(data.get("meta"), dict) else {}).get("session") or "").strip().lower()
    return {"practice": "practice", "qualifying": "qualifying", "qualify": "qualifying"}.get(s)


def _fuel(data):
    st = data.get("stint") if isinstance(data.get("stint"), dict) else {}
    out = {}
    for x in st.get("laps") or []:
        if isinstance(x, dict) and x.get("n") is not None and _num(x.get("fuel_level")) is not None:
            out[int(x["n"])] = float(x["fuel_level"])
    return out


def split_runs(laps, fuel):
    """laps: [{n, t, clean, start?}] in lap order. Returns [[lap dicts]] and the boundary rule of each."""
    runs, why = [], []
    for k, l in enumerate(laps):
        rule = None
        if k == 0:
            rule = "start"
        else:
            p = laps[k - 1]
            if int(l["n"]) - int(p["n"]) > 1:
                rule = "gap"
            else:
                s0, s1, tp = _num(p.get("start")), _num(l.get("start")), _num(p.get("t"))
                if s0 is not None and s1 is not None and tp is not None and s1 - s0 > BREAK_FACTOR * tp + BREAK_PAD_S:
                    rule = "break"
                elif int(p["n"]) in fuel and int(l["n"]) in fuel and fuel[int(l["n"])] - fuel[int(p["n"])] >= REFUEL_L:
                    rule = "refuel"
        if rule:
            runs.append([l])
            why.append(rule)
        else:
            runs[-1].append(l)
    return runs, why


def _run_block(i, run, rule, fuel):
    ts = [float(l["t"]) for l in run]
    cl = [l for l in run if l.get("clean")]
    pool = cl or run
    best = min(pool, key=lambda l: float(l["t"]))
    out = {"id": i, "laps": [int(l["n"]) for l in run], "boundary": rule,
           "best_t": _r(best["t"]), "best_lap": int(best["n"]), "best_clean": bool(best.get("clean")),
           "median_t": None, "clean": len(cl), "trend_s_per_lap": None,
           "fuel_start_l": _r(fuel.get(int(run[0]["n"])), 1)}
    if len(run) >= MIN_RUN_LAPS:
        out["median_t"] = _r(_median([float(l["t"]) for l in pool]))
    if len(cl) >= MIN_TREND_CLEAN:
        out["trend_s_per_lap"] = _r(_slope([int(l["n"]) for l in cl], [float(l["t"]) for l in cl]), 4)
    out["_ts"] = ts
    return out


def _corner_time(c, n):
    m = (c.get("per_lap") or {}).get(str(n)) if isinstance(c.get("per_lap"), dict) else None
    return _num(m.get("t")) if isinstance(m, dict) else None


def _progress(runs_laps, corners):
    ok = [r for r in runs_laps if sum(1 for l in r if l.get("clean")) >= MIN_PROGRESS_CLEAN]
    if len(ok) < 2:
        return None
    first = [l for l in ok[0] if l.get("clean")]
    last = [l for l in ok[-1] if l.get("clean")]
    fm, lm = _median([l["t"] for l in first]), _median([l["t"] for l in last])
    cs = []
    for c in corners:
        a = _median([_corner_time(c, l["n"]) for l in first])
        b = _median([_corner_time(c, l["n"]) for l in last])
        if a is None or b is None:
            continue
        cs.append({"id": str(c.get("id", "?")), "first_t": _r(a), "last_t": _r(b), "delta_t": _r(b - a)})
    return {"first_median_t": _r(fm), "last_median_t": _r(lm), "delta_t": _r(lm - fm),
            "first_run": runs_laps.index(ok[0]) + 1,
            "last_run": runs_laps.index(ok[-1]) + 1, "n_first": len(first), "n_last": len(last),
            "corners": cs}


def _targets(runs_laps, all_laps, corners):
    """Times your better laps already managed this session, per corner (latest run with >= 5 clean laps,
    or the whole session)."""
    pool_run = None
    for r in reversed(runs_laps):
        if sum(1 for l in r if l.get("clean")) >= MIN_TARGET_CLEAN:
            pool_run = r
            break
    pool = [l for l in (pool_run or all_laps) if l.get("clean")]
    out = []
    for c in corners:
        rows = [(int(l["n"]), _corner_time(c, l["n"])) for l in pool]
        rows = [(n, t) for n, t in rows if t is not None]
        if len(rows) < MIN_TARGET_CLEAN:
            continue
        ts = [t for _, t in rows]
        p25 = float(np.percentile(ts, 25))
        better = sorted([(t, n) for n, t in rows if t <= p25 + 1e-9])
        if len(better) < 3:
            better = sorted((t, n) for n, t in rows)[:3]
        target = float(np.median([t for t, _ in better]))
        typ = float(np.median(ts))
        gain = typ - target
        if gain < MIN_GAIN_S:
            continue
        per = c.get("per_lap") or {}
        bl = [per.get(str(n)) or {} for _, n in better]
        gears = [int(_num(m.get("gear"))) for m in bl if _num(m.get("gear")) is not None]
        out.append({"corner": str(c.get("id", "?")), "target_t": _r(target), "typical_t": _r(typ),
                    "gain_s": _r(gain), "from_laps": sorted(n for _, n in better), "n": len(rows),
                    "brake_on": _r(_median([m.get("brake_on") for m in bl]), 1),
                    "min_mph": _r(_median([m.get("min_mph") for m in bl]), 1),
                    "full_thr": _r(_median([m.get("full_thr") for m in bl]), 1),
                    "gear": max(set(gears), key=gears.count) if gears else None,
                    "source": "run" if pool_run else "session"})
    out.sort(key=lambda x: -x["gain_s"])
    return out[:MAX_TARGETS]


def _quali(runs_laps, runs_out, data):
    laps = [l for r in runs_laps for l in r]
    if not laps:
        return None
    full = [l for l in laps if l.get("clean")] or laps
    best = min(full, key=lambda l: float(l["t"]))
    ri = next(i for i, r in enumerate(runs_laps) if best in r)
    run = runs_laps[ri]
    bt = float(best["t"])
    bi = run.index(best)
    rows, pushes = [], []
    for k, l in enumerate(run):
        t = float(l["t"])
        if k < bi:
            role = "warm-up"
        elif k == bi:
            role = "best"
        elif t <= bt * (1 + PUSH_WITHIN):
            role = "push"
            pushes.append(t)
        else:
            role = "cool"
        rows.append({"n": int(l["n"]), "t": _r(t), "index": k + 1, "delta_t": _r(t - bt), "role": role,
                     "clean": bool(l.get("clean"))})
    names = data.get("sector_names") if isinstance(data.get("sector_names"), list) else []
    sg = []
    if names and isinstance(best.get("sectors"), list) and len(best["sectors"]) == len(names):
        for i, nm in enumerate(names):
            others = [_num(l["sectors"][i]) for l in run if l is not best and isinstance(l.get("sectors"), list)
                      and len(l["sectors"]) == len(names)]
            others = [x for x in others if x is not None and x > 0]
            mine = _num(best["sectors"][i])
            sg.append({"name": str(nm), "best_lap_t": _r(mine), "others_best_t": _r(min(others)) if others else None})
    return {"run": ri + 1, "best_lap": int(best["n"]), "best_index": bi + 1, "push_within": PUSH_WITHIN,
            "laps": rows, "second_push_delta_t": _r(min(pushes) - bt) if pushes else None,
            "sector_gain": sg}


def for_review(data: dict) -> dict | None:
    """The `session_review` block for practice and qualifying reviews; None for races (or unknown)."""
    if not isinstance(data, dict):
        return None
    k = _kind(data)
    if not k:
        return None
    laps = sorted([l for l in (data.get("laps") or []) if isinstance(l, dict) and l.get("n") is not None
                   and _num(l.get("t")) is not None], key=lambda l: int(l["n"]))
    fuel = _fuel(data)
    runs_laps, why = split_runs(laps, fuel) if laps else ([], [])
    runs = [_run_block(i + 1, r, why[i], fuel) for i, r in enumerate(runs_laps)]
    for r in runs:
        r.pop("_ts", None)
    params = {"min_run_laps": MIN_RUN_LAPS, "min_trend_clean": MIN_TREND_CLEAN,
              "min_progress_clean": MIN_PROGRESS_CLEAN, "min_target_clean": MIN_TARGET_CLEAN,
              "min_gain_s": MIN_GAIN_S, "refuel_l": REFUEL_L, "break_factor": BREAK_FACTOR,
              "break_pad_s": BREAK_PAD_S, "push_within": PUSH_WITHIN}
    out = {"version": VERSION, "kind": k, "params": params, "runs": runs, "progress": None, "targets": [],
           "quali": None}
    if len(laps) < 3:
        return out
    corners = [c for c in (data.get("corners") or []) if isinstance(c, dict)]
    out["progress"] = _progress(runs_laps, corners)
    if k == "practice":
        out["targets"] = _targets(runs_laps, laps, corners)
    else:
        out["quali"] = _quali(runs_laps, runs, data)
    return out
