"""Debrief 2.9: the deeper reading of a review (the `depth` key). Works only on what review.json keeps, so saved
reviews get it when they are re-rendered, with no requests. It only adds a key: no existing number is changed.

  chains          per corner, the steps of a corner that moved together over the clean laps (correlation only)
  costs           what a slower exit went with along the following straight, up to the next brake point
  same_mistakes   the same kind of mistake at the same corner in earlier saved reviews of this track and car
  baseline        this race's typical lap against earlier races in similar conditions only
  ref_match       (from the engine, new reviews only) how well the main comparison lap's conditions match, and why
                  that lap (2.36: the track temperatures side by side)
  gap             (2.36) the gap to the main comparison lap split into what conditions may explain and the rest,
                  only where your own earlier races give a defensible link; fuel is said to be unsplittable

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
DRY_WETNESS = 5.0                            # at or under this (0-100) a track is called dry in words
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
            "session": analysis_plus._num(g(lap, "sessionType", "session_type")),
            "track_temp_forecast": bool(g(lap, "trackTempForecast"))}


def context_of(laps):
    """The conditions and session type of a set of Garage 61 laps (medians; the commonest session type)."""
    rows = [conditions_of(l) for l in laps or [] if isinstance(l, dict)]
    out = {k: analysis_plus._median([r[k] for r in rows]) for k in ("track_temp_c", "air_temp_c", "wetness")}
    temps = [r for r in rows if r["track_temp_c"] is not None]
    out["track_temp_forecast"] = bool(temps) and all(r["track_temp_forecast"] for r in temps)   # 2.36: forecast, not measured
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


def why_similar(me, other):
    """2.36: "Similar conditions to your race: track 31 C vs your 33 C, both dry. Debrief prefers a comparison lap
    like that." Only with both track temperatures known; wetness is added when both are known. None otherwise."""
    a, b = analysis_plus._num((me or {}).get("track_temp_c")), analysis_plus._num((other or {}).get("track_temp_c"))
    if a is None or b is None:
        return None
    text = (f"track {b:.0f} C vs your forecast track temperature of {a:.0f} C" if (me or {}).get("track_temp_forecast")
            else f"track {b:.0f} C vs your {a:.0f} C")
    wa, wb = analysis_plus._num((me or {}).get("wetness")), analysis_plus._num((other or {}).get("wetness"))
    if wa is not None and wb is not None:
        text += ", both dry" if max(wa, wb) <= DRY_WETNESS else f", track wetness {wb:.0f}% vs your {wa:.0f}%"
    return f"Similar conditions to your race: {text}. Debrief prefers a comparison lap like that."


def pick_quickest(cands, me=None, lap_time=None):
    """2.36: the main comparison when no lap is quicker than yours: conditions like yours first, then the quickest. With
    no conditions known on either side (every lap "unknown") that is the quickest, as it always was. `cands` is sorted
    quickest first."""
    if not me:
        return cands[0]
    rank = {"similar": 0, "unknown": 1, "different": 2}
    return min(cands, key=lambda c: (rank[compare_conditions(me, conditions_of(c))], lap_time(c)))


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
    out = {"status": state, "switched": switched, "same_session": same_kind,
           "sentence": words + ("; it was swapped in for a lap set in different conditions." if switched else ".")}
    # 2.36: the figures behind the choice, for the page's plain reason and the gap line (depth.gap_note)
    out["mine"] = {k: me.get(k) for k in ("track_temp_c", "air_temp_c", "wetness", "track_temp_forecast")}
    out["theirs"] = {k: other.get(k) for k in ("track_temp_c", "air_temp_c", "wetness")}
    out["ref_t"] = analysis_plus._num(engine.g(main, "lap_time", "lapTime"))
    why = why_similar(me, other) if state == "similar" else None
    if why:
        out["why"] = why
    return out


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


# ---------------------------------------------------------------- 2.36 the gap to the comparison lap, split honestly
MIN_TEMP_RACES = 8          # races (this one and earlier ones here, dry, with a track temperature) a temperature link needs ...
MIN_TEMP_SPREAD_C = 8.0     # ... spread over at least this many degrees
MIN_TEMP_RHO = 0.8          # ... and ranked at least this strongly together (Spearman). Six or seven races (never fewer)
STRONG_TEMP_RACES = 6       # ... are enough only at STRONG_TEMP_RHO: |rho| 0.8 over 6 races is not significant (p about 0.1)
STRONG_TEMP_RHO = 0.9
MIN_GAP_S = 0.05            # x time scale: a smaller gap isn't worth splitting
MIN_COND_S = 0.03           # x time scale: a conditions share smaller than this isn't said


def temp_sensitivity(data, earlier):
    """How your own typical clean lap moved with track temperature in this race and earlier dry races at this track and
    car: {"s_per_c", "n", "rho", "lo", "hi"} (seconds per degree C, the range of temperatures seen), or None unless there
    are MIN_TEMP_RACES races spread over MIN_TEMP_SPREAD_C degrees that rank together at MIN_TEMP_RHO (or STRONG_TEMP_RACES
    races at STRONG_TEMP_RHO). The slope is the Theil-Sen one (the median of pairwise slopes), so one odd race can't tilt it.
    A hint drawn from your own history, with fuel, tyres and your own progress mixed in: never a measurement of the
    conditions alone."""
    xs, ys = [], []
    for d in [data] + list(earlier or []):
        c = d.get("conditions") if isinstance(d, dict) else None
        if not isinstance(c, dict):
            continue
        t, w, typical = analysis_plus._num(c.get("track_temp_c")), analysis_plus._num(c.get("wetness")), _typical(d)
        if t is None or w is None or w > SIMILAR_WETNESS or typical is None or c.get("track_temp_forecast"):
            continue                      # a forecast temperature isn't a measurement of that race
        xs.append(t)
        ys.append(typical)
    n = len(xs)
    if n < STRONG_TEMP_RACES or max(xs) - min(xs) < MIN_TEMP_SPREAD_C:
        return None
    rho = engine.spearman(xs, ys)
    if rho is None or abs(rho) < (MIN_TEMP_RHO if n >= MIN_TEMP_RACES else STRONG_TEMP_RHO):
        return None
    from . import profile   # lazy: profile is not needed by the rest of this module
    slope = profile.theil_sen(xs, ys)
    if slope is None or slope == 0 or (slope > 0) != (rho > 0):
        return None
    return {"s_per_c": float(slope), "n": n, "rho": round(float(rho), 3), "lo": min(xs), "hi": max(xs)}


def gap_note(data, earlier):
    """The lap-time gap between your best lap and the main comparison lap, split into what the conditions may explain
    and the rest, or None when there is nothing defensible to say. Needs: the comparison lap quicker by a real margin;
    both track temperatures and both wetnesses known, both dry (a wet-dry difference has no basis here); and a
    temperature link in your own earlier races (temp_sensitivity), both temperatures inside the range it was seen over.
    Fuel is never split: Garage 61 has no fuel load for the other lap, so the line says so rather than guessing.
    Correlational words throughout ("may", "a hint")."""
    rm = data.get("ref_match") if isinstance(data.get("ref_match"), dict) else None
    best = data.get("best") if isinstance(data.get("best"), dict) else None
    refs = [r for r in data.get("refs") or [] if isinstance(r, dict) and r.get("main") and not r.get("rival")]
    if not rm or not best or not refs:
        return None
    main = refs[0]
    mt, bt, ref_t = analysis_plus._num(main.get("t")), analysis_plus._num(best.get("t")), analysis_plus._num(rm.get("ref_t"))
    if mt is None or bt is None or ref_t is None or abs(mt - ref_t) > 0.0005:
        return None                  # the main lap changed since conditions were read: don't pair them
    ts = analysis_plus.time_scale(data.get("median"))
    gap = bt - mt
    if gap < MIN_GAP_S * ts:
        return None
    mine, theirs = rm.get("mine") or {}, rm.get("theirs") or {}
    ta, tb = analysis_plus._num(mine.get("track_temp_c")), analysis_plus._num(theirs.get("track_temp_c"))
    wa, wb = analysis_plus._num(mine.get("wetness")), analysis_plus._num(theirs.get("wetness"))
    if None in (ta, tb, wa, wb) or max(wa, wb) > SIMILAR_WETNESS or mine.get("track_temp_forecast"):
        return None                     # (a forecast track temperature can't back a split of the gap)
    sens = temp_sensitivity(data, earlier)
    if not sens or not (sens["lo"] - 2 <= min(ta, tb) and max(ta, tb) <= sens["hi"] + 2):
        return None
    explained = -sens["s_per_c"] * (tb - ta)      # what driving at their temperature would take off your lap
    if explained < MIN_COND_S * ts:
        return None
    part = min(explained, gap)
    name = str(main.get("name") or "the comparison driver").split(" ")[0]
    way = "slower" if sens["s_per_c"] > 0 else "quicker"
    rest = gap - part
    sentence = (f"Of the {gap:.2f} s between your best lap and {name}'s, about {part:.2f} s may be the conditions "
                f"(track {tb:.0f} C vs your {ta:.0f} C; in {sens['n']} of your own dry races here, laps ran about "
                f"{abs(sens['s_per_c']) * 10:.2f} s {way} for every 10 C warmer, which is a hint, not a measurement). "
                "Fuel can't be split out: Garage 61 doesn't have the other lap's fuel load. "
                + (f"The other {rest:.2f} s is what is left once the conditions are taken out." if rest >= 0.005 else
                   "That could account for all of it."))
    return {"gap_s": round(gap, 3), "conditions_s": round(part, 3), "rest_s": round(rest, 3), "fuel": "unknown",
            "track_c": [round(ta, 1), round(tb, 1)], "n": sens["n"], "rho": sens["rho"], "sentence": sentence}


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
           "ref_match": data.get("ref_match") if isinstance(data.get("ref_match"), dict) else None,
           "gap": part(lambda: gap_note(data, earlier), None)}
    if not any(out.values()):
        return None
    out["version"] = VERSION
    return out
