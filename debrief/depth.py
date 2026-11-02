"""Debrief 2.9: the deeper reading of a review (the `depth` key). Works only on what review.json keeps, so saved
reviews get it when they are re-rendered, with no requests. It only adds a key: no existing number is changed.

  chains          per corner, the steps of a corner that moved together over the clean laps (correlation only)
  costs           what a slower exit went with along the following straight, up to the next brake point
  same_mistakes   the same kind of mistake at the same corner in earlier saved reviews of this track and car
  baseline        this race's typical lap against earlier races in similar conditions only
  ref_match       (from the engine, new reviews only) how well the main comparison lap's conditions match

Every result clears a gate or is left out. Links use the same rank-correlation gate as the engine's "what made
your laps faster" patterns, and must also survive taking the trend through the race out (a drying track or a
lightening car moves every number together, which would look like a link).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np

from . import analysis_plus, engine

VERSION = 1
# stages of a corner in driving order: key, what higher reads as, what lower reads as, "bad" direction at chain end
STAGES = (("brake_on", "braking later", "braking earlier", 0),
          ("min_mph", "a higher minimum speed", "a lower minimum speed", -1),
          ("full_thr", "later full throttle", "earlier full throttle", 1),
          ("exit_mph", "a higher exit speed", "a lower exit speed", -1),
          ("t", "more time through the corner", "less time through the corner", 1))
MIN_EFFECT_S = engine.PATTERN_MIN_EFFECT_S   # x time scale
MIN_EFFECT_M = 5.0                           # x distance scale; the corner metrics are read on a 5 m grid
MIN_EFFECT_MPH = 0.5
MIN_STRAIGHT_M = 150.0                       # x distance scale
SIMILAR_TRACK_C = 8.0                        # physical, so not scaled by lap length
SIMILAR_AIR_C = 8.0
SIMILAR_WETNESS = 10.0
MIN_BASELINE_LAPS = 3
REF_GAP_S = 0.8                              # main comparison lap's target gap on a 90 s lap
EARLIER_LIMIT = 12                           # newest earlier reviews of this track and car read
# a mistake: this much slower than the typical clean lap through the corner (x time scale), and 3 IQRs
MISTAKE_MIN_S = 0.5
MISTAKE_IQRS = 3.0
APEX_MATCH_M = 75.0                          # x distance scale: the same corner in two reviews
SAME_MIN_EVENTS = 3                          # in total, across at least this race and one earlier


def _nums(xs, ys, ns):
    keep = [(x, y, n) for x, y, n in zip(xs, ys, ns) if analysis_plus._num(x) is not None
            and analysis_plus._num(y) is not None]
    return [k[0] for k in keep], [k[1] for k in keep], [k[2] for k in keep]


def _detrend(ys, ns):
    y, n = np.asarray(ys, float), np.asarray(ns, float)
    if len(set(ns)) < 3:
        return y
    return y - np.polyval(np.polyfit(n, y, 1), n)


def _link(xs, ys, ns):
    """(rho, slope) when xs and ys clear the pattern gate both as they are and with the race's trend removed."""
    n = len(xs)
    if n < engine.PATTERN_MIN_LAPS:
        return None
    need = engine.pattern_r_needed(n)
    rho = engine.spearman(xs, ys)
    if rho is None or abs(rho) < need:
        return None
    rd = engine.spearman(_detrend(xs, ns), _detrend(ys, ns))
    if rd is None or abs(rd) < need or (rd > 0) != (rho > 0):
        return None
    _, slope, _ = engine.corr(_detrend(xs, ns), _detrend(ys, ns))
    return None if slope is None else (rho, slope)


def _clean_ns(data):
    return [l["n"] for l in data.get("laps") or [] if isinstance(l, dict) and l.get("clean") and l.get("n") is not None]


def _scales(data):
    return analysis_plus.time_scale(data.get("median")), analysis_plus.dist_scale(data.get("L"))


def _corners(data):
    return [c for c in data.get("corners") or [] if isinstance(c, dict) and isinstance(c.get("per_lap"), dict)]


# ---------------------------------------------------------------- 1. chains
def _min_effect(key, ts, ds):
    return {"t": MIN_EFFECT_S * ts, "min_mph": MIN_EFFECT_MPH, "exit_mph": MIN_EFFECT_MPH}.get(key, MIN_EFFECT_M * ds)


def corner_chain(c, ns, ts, ds):
    """The longest run of 2+ linked steps in one corner, turned to read towards the bad end, or None."""
    rows = {n: c["per_lap"][str(n)] for n in ns if isinstance(c["per_lap"].get(str(n)), dict)}
    links = []
    for (a, *_), (b, *_) in zip(STAGES, STAGES[1:]):
        xs, ys, nn = _nums([rows[n].get(a) for n in rows], [rows[n].get(b) for n in rows], list(rows))
        hit = _link(xs, ys, nn) if xs else None
        if hit and abs(hit[1]) * (engine.pct(xs, 75) - engine.pct(xs, 25)) < _min_effect(b, ts, ds):
            hit = None
        links.append({"from": a, "to": b, "rho": round(hit[0], 3), "n": len(xs)} if hit else None)
    best, run = [], []
    for i, l in enumerate(links + [None]):
        if l:
            run.append(i)
        else:
            if len(run) > len(best):
                best = run
            run = []
    if len(best) < 2:
        return None
    # direction per stage, from the bad end of the run backwards along the signs of the links
    idx = list(range(best[0], best[-1] + 2))
    want = {idx[-1]: STAGES[idx[-1]][3]}
    for i in reversed(best):
        want[i] = want[i + 1] * (1 if links[i]["rho"] > 0 else -1)
    first = STAGES[idx[0]][0]
    vals = {n: rows[n].get(first) for n in rows if analysis_plus._num(rows[n].get(first)) is not None}
    if len(vals) < engine.PATTERN_MIN_LAPS:
        return None
    mid = float(np.median(list(vals.values())))
    bad = [rows[n]["t"] for n, v in vals.items() if (v - mid) * want[idx[0]] > 0]
    good = [rows[n]["t"] for n, v in vals.items() if (v - mid) * want[idx[0]] < 0]
    if len(bad) < 3 or len(good) < 3:
        return None
    worth = float(np.median(bad) - np.median(good))
    if worth < MIN_EFFECT_S * ts:     # the run doesn't go with a slower corner, so it isn't a mistake chain
        return None
    words = [STAGES[i][1] if want[i] > 0 else STAGES[i][2] for i in idx]
    n = min(links[i]["n"] for i in best)
    cid = str(c.get("id"))
    return {"corner": cid, "steps": words, "links": [links[i] for i in best], "n": n, "worth_s": round(worth, 3),
            "sentence": f"Over {n} clean laps through {cid}, these went together: " + ", then ".join(words) +
                        f". The laps at that end took about {worth:.2f} s longer through the corner."}


def chains(data):
    ts, ds = _scales(data)
    ns = _clean_ns(data)
    out = [x for x in (corner_chain(c, ns, ts, ds) for c in _corners(data)) if x]
    return sorted(out, key=lambda x: -x["worth_s"])


# ---------------------------------------------------------------- 2. cost to the next brake point
def _timer(data, n):
    """time_at(x) for lap n: x metres along the lap, past the line into lap n+1 when x > L. None without traces."""
    traces = data.get("traces") if isinstance(data.get("traces"), dict) else {}
    tr, nx = traces.get(f"me{n}"), traces.get(f"me{n + 1}")
    step, L = analysis_plus._num(data.get("step")), analysis_plus._num(data.get("L"))
    if not (isinstance(tr, dict) and step and L and isinstance(tr.get("tt"), list) and len(tr["tt"]) > 10
            and analysis_plus._num(tr.get("t"))):
        return None

    def at(t_row, x):
        d = np.arange(len(t_row["tt"])) * step
        return float(np.interp(x, np.append(d, L), np.append(np.asarray(t_row["tt"], float), float(t_row["t"]))))

    def time_at(x):
        if x <= L:
            return at(tr, x)
        if not (isinstance(nx, dict) and isinstance(nx.get("tt"), list) and len(nx["tt"]) > 10 and nx.get("t")):
            return None
        return float(tr["t"]) + at(nx, x - L)
    return time_at


def _median_of(rows, key):
    return analysis_plus._median([analysis_plus._num(r.get(key)) for r in rows])


def costs(data):
    ts, ds = _scales(data)
    L = analysis_plus._num(data.get("L"))
    cs = sorted((c for c in _corners(data) if analysis_plus._num(c.get("apex_m")) is not None), key=lambda c: c["apex_m"])
    ns = _clean_ns(data)
    if len(cs) < 2 or not L:
        return []
    clean_set = set(ns)
    out = []
    for i, c in enumerate(cs):
        nxt = cs[(i + 1) % len(cs)]
        rows = [c["per_lap"][str(n)] for n in ns if isinstance(c["per_lap"].get(str(n)), dict)]
        nrows = [nxt["per_lap"][str(n)] for n in ns if isinstance(nxt["per_lap"].get(str(n)), dict)]
        min_at = _median_of(rows, "min_at")
        if min_at is None:
            continue
        a = min_at + float(c.get("exit_at_m") or 150.0)   # where exit speed is read (150 m on reviews before 2.7)
        brakes = [analysis_plus._num(r.get("brake_on")) for r in nrows]
        brakes = [b for b in brakes if b is not None]
        # the earliest clean brake point: up to there every clean lap was still on the straight
        b = min(brakes) if brakes else analysis_plus._num(nxt.get("start_m"))
        if b is None:
            continue
        if b < a:
            b += L
        length = b - a
        if length < MIN_STRAIGHT_M * ds or length > L / 2:
            continue
        cut = [a, a + length / 3, a + 2 * length / 3, b]
        xs, parts, nn = [], [], []
        for n in ns:
            row = c["per_lap"].get(str(n))
            ex = analysis_plus._num(row.get("exit_mph")) if isinstance(row, dict) else None
            tm = _timer(data, n)
            if ex is None or tm is None or (b > L and n + 1 not in clean_set):
                continue
            pts = [tm(x) for x in cut]
            if None in pts:
                continue
            xs.append(ex)
            parts.append([pts[k + 1] - pts[k] for k in range(3)])
            nn.append(n)
        if len(xs) < engine.PATTERN_MIN_LAPS:
            continue
        total = [sum(p) for p in parts]
        hit = _link(xs, total, nn)
        if not hit or hit[1] >= 0:     # physics: a slower exit should go with more time, or there is nothing to say
            continue
        spread = engine.pct(xs, 75) - engine.pct(xs, 25)
        cost = -hit[1] * spread
        if spread <= 0 or cost < MIN_EFFECT_S * ts:
            continue
        rx = _detrend(xs, nn)
        thirds = [round(-float(np.polyfit(rx, _detrend([p[k] for p in parts], nn), 1)[0]) * spread, 3) for k in range(3)]
        cid, nid = str(c.get("id")), str(nxt.get("id"))
        out.append({"corner": cid, "next": nid, "n": len(xs), "rho": round(hit[0], 3), "length_m": round(length),
                    "s_per_mph": round(-hit[1], 4), "spread_mph": round(spread, 1), "cost_s": round(cost, 3),
                    "thirds_s": thirds,
                    "sentence": f"Leaving {cid} about {spread:.1f} mph slower (your usual lap-to-lap spread there) went "
                                f"with about {cost:.2f} s more over the {length:.0f} m to the {nid} brake point."})
    return sorted(out, key=lambda x: -x["cost_s"])


# ---------------------------------------------------------------- conditions and comparison laps
def conditions_of(lap):
    g = analysis_plus._g
    return {"track_temp_c": analysis_plus._num(g(lap, "trackTemp", "track_temp")),
            "air_temp_c": analysis_plus._num(g(lap, "airTemp", "air_temp")),
            "wetness": analysis_plus._num(g(lap, "trackWetness", "track_wetness")),
            "session": analysis_plus._num(g(lap, "sessionType", "session_type"))}


def context_of(laps):
    """The conditions and session type of a set of Garage 61 laps (medians; the commonest session type)."""
    rows = [conditions_of(l) for l in laps or [] if isinstance(l, dict)]
    out = {k: analysis_plus._median([r[k] for r in rows]) for k in ("track_temp_c", "air_temp_c", "wetness")}
    kinds = [r["session"] for r in rows if r["session"] is not None]
    out["session"] = max(set(kinds), key=kinds.count) if kinds else None
    return out


def compare_conditions(a, b):
    """"similar", "different" or "unknown": unknown when wetness or track temperature is missing on either side."""
    if not isinstance(a, dict) or not isinstance(b, dict):
        return "unknown"
    limits = (("wetness", SIMILAR_WETNESS), ("track_temp_c", SIMILAR_TRACK_C), ("air_temp_c", SIMILAR_AIR_C))
    known = 0
    for key, lim in limits:
        x, y = analysis_plus._num(a.get(key)), analysis_plus._num(b.get(key))
        if x is None or y is None:
            continue
        known += key != "air_temp_c"
        if abs(x - y) > lim:
            return "different"
    return "similar" if known == 2 else "unknown"


def pick_main(quicker, my_best, me=None, lap_time=None):
    """The main comparison lap among laps quicker than yours: conditions first, then the session type, then the
    closest to a target gap that scales with the lap (0.8 s on a 90 s lap). With no `me`, the target gap alone."""
    target = my_best - REF_GAP_S * analysis_plus.time_scale(my_best)

    def rank(c):
        gap = abs(lap_time(c) - target)
        if not me:
            return (0, 0, gap)
        cond = {"similar": 0, "unknown": 1, "different": 2}[compare_conditions(me, conditions_of(c))]
        kind = conditions_of(c)["session"]
        return (cond, 0 if kind is None or me.get("session") is None or kind == me["session"] else 1, gap)
    return min(quicker, key=rank)


def _swap_base(a, b):
    """Swap the words before " (" in two comparison roles, keeping each lap's own gap text."""
    ra, rb = str(a.get("_role") or ""), str(b.get("_role") or "")
    if " (" in ra and " (" in rb:
        a["_role"] = rb.split(" (", 1)[0] + " (" + ra.split(" (", 1)[1]
        b["_role"] = ra.split(" (", 1)[0] + " (" + rb.split(" (", 1)[1]


def match_refs(refs, my_laps):
    """For the laps already chosen: when the main one is in different conditions and another (not a rival, quicker
    than your typical lap) is in similar ones, that other becomes the main comparison. Returns what to tell him,
    or None with no comparison lap or no conditions to go on. Changes only `_main` and `_role` on the laps."""
    me = context_of(my_laps)
    pool = [r for r in refs or [] if isinstance(r, dict) and not r.get("_rival")]
    main = next((r for r in pool if r.get("_main")), None)
    if main is None:
        return None
    state = compare_conditions(me, conditions_of(main))
    switched = False
    if state == "different":
        times = [analysis_plus._num(engine.g(l, "lap_time", "lapTime")) for l in my_laps or [] if isinstance(l, dict)]
        typical = analysis_plus._median([t for t in times if t])
        for r in pool:
            t = analysis_plus._num(engine.g(r, "lap_time", "lapTime"))
            if r is not main and t and typical and t < typical and compare_conditions(me, conditions_of(r)) == "similar":
                main["_main"], r["_main"] = False, True
                _swap_base(main, r)
                main, state, switched = r, "similar", True
                break
    other = conditions_of(main)
    same_kind = None if me["session"] is None or other["session"] is None else me["session"] == other["session"]
    if state == "unknown" and same_kind is None:
        return None
    words = {"similar": "Your main comparison lap was set in conditions like yours",
             "different": "Your main comparison lap was set in different conditions from yours, so read its gaps with care",
             "unknown": "The conditions of your main comparison lap aren't known"}[state]
    if same_kind is False:
        words += ", and in a different type of session"
    return {"status": state, "switched": switched, "same_session": same_kind,
            "sentence": words + ("; it was swapped in for a lap set in different conditions." if switched else ".")}


# ---------------------------------------------------------------- 4 and 5. earlier saved reviews
def library_folder():
    """The library in the settings file, or None. Read directly: the settings loader stops the program on a typo,
    which must never happen while a page is being written."""
    try:
        import tomllib

        from . import app
        p = Path(os.environ.get("DEBRIEF_SETTINGS") or (app.HERE / "settings.toml"))
        lib = tomllib.loads(p.read_text(encoding="utf-8")).get("library_folder")
        return os.path.expandvars(os.path.expanduser(lib)) if lib else None
    except Exception:  # noqa: BLE001  (no settings: no comparison with earlier races)
        return None


def _kind(d):
    from . import library
    return library.session_kind((d.get("meta") or {}).get("session"))


def earlier_reviews(folder, data, limit=EARLIER_LIMIT):
    """Saved reviews of the same track, car and kind of session that started before this one, newest first."""
    from . import library
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    ids = data.get("combo_ids") if isinstance(data.get("combo_ids"), dict) else {}
    me = {"track_id": ids.get("track_id"), "car_id": ids.get("car_id"), "track": meta.get("track"),
          "car": meta.get("car"), "started_at": meta.get("started_at")}
    if not folder or not me["started_at"]:
        return []
    t_me = library._ts(me)
    rows = [r for r in library.review_summaries(folder) if r.get("started_at") and library.same_combo(me, r)
            and library._ts(r) < t_me]
    rows.sort(key=library._ts, reverse=True)
    out = []
    for r in rows:
        if len(out) >= limit:
            break
        try:
            d = json.loads((Path(folder) / "reviews" / r["dir"] / "review.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        m = d.get("meta") if isinstance(d, dict) else None
        if not isinstance(m, dict) or m.get("test_data") or _kind(d) != _kind(data):
            continue
        if meta.get("event_id") and m.get("event_id") == meta.get("event_id"):
            continue
        out.append(d)
    return out


def _date(d):
    from . import library
    started = (d.get("meta") or {}).get("started_at")
    return library.local_day(started) or str(started or "")[:10]


def _typical(d):
    ts = [analysis_plus._num(l.get("t")) for l in d.get("laps") or [] if isinstance(l, dict) and l.get("clean")]
    ts = [t for t in ts if t]
    return float(np.median(ts)) if len(ts) >= MIN_BASELINE_LAPS else None


MIN_BASELINE_RACES = 2


def baseline(data, earlier):
    me = _typical(data)
    cond = data.get("conditions")
    if me is None or not isinstance(cond, dict) or not earlier:
        return None
    similar, left_out = [], []
    for d in earlier:
        t = _typical(d)
        state = compare_conditions(cond, d.get("conditions"))
        if state == "similar" and t is not None:
            similar.append({"date": _date(d), "typical": round(t, 3)})
        else:
            left_out.append({"date": _date(d), "why": "different conditions" if state == "different" else
                             ("too few clean laps" if t is None else "conditions not known")})
    if not similar:
        if not any(x["why"] == "different conditions" for x in left_out):
            return None
        return {"n": 0, "left_out": left_out, "sentence":
                "No earlier race here was in conditions like this one, so your lap times aren't compared with them."}
    if len(similar) < MIN_BASELINE_RACES:     # one earlier race says nothing about what's normal
        return None
    delta = me - float(np.median([s["typical"] for s in similar]))
    n = len(similar)
    nwords = "race" if n == 1 else "races"
    way = "about the same as" if abs(delta) < 0.05 else f"{abs(delta):.2f} s {'slower' if delta > 0 else 'quicker'} than"
    out = f"Your typical clean lap was {way} in {n} earlier {nwords} here in similar track temperature and wetness."
    if left_out:
        out += f" {len(left_out)} other race{'' if len(left_out) == 1 else 's'} left out (different conditions, or too little to go on)."
    return {"n": n, "delta_s": round(delta, 3), "similar": similar, "left_out": left_out, "sentence": out}


KIND_WORDS = {"late_brake": "braking later than usual", "early_brake": "braking earlier than usual",
              "low_min": "a much lower minimum speed", "late_throttle": "much later full throttle",
              "low_exit": "a much lower exit speed"}


def mistake_kind(m, typ, ds):
    """What stood out on a lap that lost time in a corner, in driving order, or None."""
    def dev(k):
        a, b = analysis_plus._num(m.get(k)), typ.get(k)
        return None if a is None or b is None else a - b
    d = dev("brake_on")
    if d is not None and d >= 15 * ds:
        return "late_brake"
    if d is not None and d <= -15 * ds:
        return "early_brake"
    d = dev("min_mph")
    if d is not None and d <= -5:
        return "low_min"
    d = dev("full_thr")
    if d is not None and d >= 25 * ds:
        return "late_throttle"
    d = dev("exit_mph")
    return "low_exit" if d is not None and d <= -4 else None


def mistakes_of(d):
    """[{apex, corner, kind, n, lost}]: laps that lost clearly more than the normal spread in one corner. Not the
    first lap reviewed (the opening scrap), nor a lap that may have lost its time to traffic."""
    ts, ds = _scales(d)
    laps = [l.get("n") for l in d.get("laps") or [] if isinstance(l, dict) and l.get("n") is not None]
    counts = d.get("counts") if isinstance(d.get("counts"), dict) else {}
    skip = set(counts.get("traffic") or []) | set(counts.get("traffic_possible") or [])
    if laps:
        skip.add(min(laps))
    ns = _clean_ns(d)
    out = []
    for c in _corners(d):
        rows = [c["per_lap"][str(n)] for n in ns if isinstance(c["per_lap"].get(str(n)), dict)]
        tt = [analysis_plus._num(r.get("t")) for r in rows]
        tt = [t for t in tt if t]
        apex = analysis_plus._num(c.get("apex_m"))
        if len(tt) < MIN_BASELINE_LAPS or apex is None:
            continue
        typ_t = float(np.median(tt))
        cut = max(MISTAKE_MIN_S * ts, MISTAKE_IQRS * (engine.pct(tt, 75) - engine.pct(tt, 25)))
        typ = {k: _median_of(rows, k) for k in ("brake_on", "min_mph", "full_thr", "exit_mph")}
        for key, m in c["per_lap"].items():
            t = analysis_plus._num(m.get("t")) if isinstance(m, dict) else None
            if t is None or t < typ_t + cut or not key.lstrip("-").isdigit() or int(key) in skip:
                continue
            kind = mistake_kind(m, typ, ds)
            if kind:
                out.append({"apex": apex, "corner": str(c.get("id")), "kind": kind, "n": int(key), "lost": round(t - typ_t, 2)})
    return out


def same_mistakes(data, earlier):
    """The same kind of mistake at the same corner here and in earlier reviews in similar conditions, 3+ in all."""
    ds = _scales(data)[1]
    mine = mistakes_of(data)
    if not mine:
        return []
    out, seen = [], set()
    for ev in mine:
        key = (ev["corner"], ev["kind"])
        if key in seen:
            continue
        seen.add(key)
        here = sum(1 for e in mine if (e["corner"], e["kind"]) == key)
        races = []
        for d in earlier:
            if compare_conditions(data.get("conditions"), d.get("conditions")) != "similar":
                continue
            hits = [e for e in mistakes_of(d) if e["kind"] == ev["kind"] and abs(e["apex"] - ev["apex"]) <= APEX_MATCH_M * ds]
            if hits:
                races.append({"date": _date(d), "count": len(hits)})
        total = here + sum(r["count"] for r in races)
        if races and total >= SAME_MIN_EVENTS:
            n = len(races) + 1
            out.append({"corner": ev["corner"], "kind": ev["kind"], "here": here, "total": total, "races": races,
                        "sentence": f"{ev['corner']}: time lost with {KIND_WORDS[ev['kind']]} {total} times over "
                                    f"{n} races in similar conditions, {here} of them in this one."})
    return sorted(out, key=lambda x: -x["total"])


# ---------------------------------------------------------------- entry point
def for_review(data, folder=None):
    """The `depth` block, or None when there is nothing the data supports saying. Each part fails on its own."""
    if not isinstance(data, dict) or not isinstance(data.get("laps"), list):
        return None

    def part(fn, default):
        try:
            return fn()
        except Exception:  # noqa: BLE001  (an extra is never worth losing the review over)
            return default
    earlier = part(lambda: earlier_reviews(folder or library_folder(), data), [])
    out = {"chains": part(lambda: chains(data), []), "costs": part(lambda: costs(data), []),
           "same_mistakes": part(lambda: same_mistakes(data, earlier), []),
           "baseline": part(lambda: baseline(data, earlier), None),
           "ref_match": data.get("ref_match") if isinstance(data.get("ref_match"), dict) else None}
    if not any(out.values()):
        return None
    out["version"] = VERSION
    return out
