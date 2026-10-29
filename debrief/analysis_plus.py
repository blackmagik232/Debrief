"""Extra analysis for Debrief v2 reviews: stint and tyre wear, corner technique, consistency score,
best-vs-worst lap diff, plus race context (iRating, season, conditions, combo ids).

engine.analyze() calls extend() once at the end. Everything here is additive: each block is computed
inside its own guard, so a failure logs one line and leaves that key as None instead of killing the
review. Readers must treat every key added here as optional (old reviews don't have them).

No network access happens here; it works only on data the engine already has.

Sign conventions (tested in tests/test_analysis.py):
  stint.deg_s_per_lap        slope of clean lap time vs lap number; POSITIVE = getting slower.
  stint.corner_drift.delta_* late minus early; POSITIVE delta_t = slower late in the race,
                             NEGATIVE delta_mph = less minimum speed late in the race.
  technique.vs_ref[k]        your typical (clean-lap median) MINUS the comparison lap.
                             brake_on / full_thr are distances along the lap, so a NEGATIVE brake_on
                             delta means you brake EARLIER than them, a POSITIVE full_thr delta means
                             you reach full throttle LATER.
  best_worst.zones.delta_t   worst lap MINUS best lap; POSITIVE = time the worst lap lost there.
"""
from __future__ import annotations

import math
import statistics as st

import numpy as np

VERSION = 2
METRICS = ("brake_on", "peak_brake", "min_mph", "full_thr", "coast_s", "exit_mph", "gear")

# Fuel-weight estimate (see fuel_effect_s_per_lap). Petrol is about 0.745 kg per litre, and a common
# sim/real rule of thumb is ~0.03 s of lap time per kg on a ~90 s lap. We scale that by lap length in
# time, i.e. 0.03 * (lap_time / 90) seconds per kg. It's an estimate, not a measurement: the true
# figure depends on the car.
FUEL_KG_PER_L = 0.745
FUEL_S_PER_KG_PER_90S = 0.03


def _g(d, *names, default=None):
    for n in names:
        if isinstance(d, dict) and d.get(n) is not None:
            return d[n]
    return default


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _r(v, d=3):
    return None if v is None else round(float(v), d)


def _median(vals):
    vals = [v for v in vals if v is not None]
    return float(np.median(vals)) if vals else None


def _id_of(v):
    if isinstance(v, dict):
        v = v.get("id")
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- context
def race_context(raw_laps):
    """combo_ids, irating, season and conditions from the Garage 61 lap objects of the race."""
    laps = [l for l in raw_laps or [] if isinstance(l, dict)]

    def order(l):
        n = _g(l, "lapNumber", "lap_number")
        return (_g(l, "_start") or 0, n if isinstance(n, (int, float)) else 0)

    laps.sort(key=order)
    first = laps[0] if laps else {}
    combo = {"track_id": _id_of(_g(first, "track", "trackId", "track_id")),
             "car_id": _id_of(_g(first, "car", "carId", "car_id"))}
    ir = None
    for l in laps:
        v = _num(_g(l, "driverRating", "driver_rating"))
        if v:
            ir = int(round(v))
            break
    season = None
    s = _g(first, "season")
    if isinstance(s, dict) and (s.get("id") is not None or s.get("name")):
        season = {"id": s.get("id"), "name": s.get("name"), "shortName": s.get("shortName") or s.get("short_name")}
    cond = {}
    for key, names in (("track_temp_c", ("trackTemp", "track_temp")), ("air_temp_c", ("airTemp", "air_temp")),
                       ("wetness", ("trackWetness", "track_wetness")), ("track_usage", ("trackUsage", "track_usage"))):
        m = _median([_num(_g(l, *names)) for l in laps])
        cond[key] = _r(m, 1) if m is not None else None
    conditions = cond if any(v is not None for v in cond.values()) else None
    return combo, ir, season, conditions


# ---------------------------------------------------------------- stint
def thirds(seq):
    """Split a sequence into Early / Middle / Late thirds (remainder goes to the middle)."""
    n = len(seq)
    k = n // 3
    if k == 0:
        return []
    return [("Early", seq[:k]), ("Middle", seq[k:n - k]), ("Late", seq[n - k:])]


def slope(xs, ys):
    """Least-squares slope of ys against xs (None with fewer than 3 points or no spread in xs)."""
    if len(xs) < 3 or len(set(xs)) < 2:
        return None
    return float(np.polyfit(np.asarray(xs, float), np.asarray(ys, float), 1)[0])


def fuel_effect_s_per_lap(burn_l_per_lap, lap_time):
    """Estimated lap time each lap GAINS from burning fuel (seconds, positive).

    burn (L/lap) * 0.745 kg/L * 0.03 s/kg * (lap_time / 90 s). Example: 1.9 L/lap on an 85 s lap
    is about 0.040 s per lap.
    """
    if not burn_l_per_lap or not lap_time or burn_l_per_lap <= 0:
        return None
    return burn_l_per_lap * FUEL_KG_PER_L * FUEL_S_PER_KG_PER_90S * (lap_time / 90.0)


def stint(laps, clean, per, corner_ids):
    """laps: engine lap dicts in lap order (keys n, t, lap); clean: the clean subset; per: corner -> n -> metrics."""
    out_laps = []
    for x in laps:
        lap = x.get("lap") or {}
        out_laps.append({"n": x["n"], "t": _r(x["t"]), "fuel_used": _r(_num(_g(lap, "fuelUsed", "fuel_used")), 3),
                         "fuel_level": _r(_num(_g(lap, "fuelLevel", "fuel_level")), 2)})
    pool = sorted(clean, key=lambda x: x["n"])
    phases = []
    if len(pool) >= 6:
        for name, grp in thirds(pool):
            ts = [x["t"] for x in grp]
            phases.append({"name": name, "laps": [x["n"] for x in grp], "avg_t": _r(float(np.mean(ts))),
                           "best_t": _r(min(ts))})
    deg = slope([x["n"] for x in pool], [x["t"] for x in pool]) if len(pool) >= 5 else None
    burns = [b for b in (_num(_g(x.get("lap") or {}, "fuelUsed", "fuel_used")) for x in pool) if b and b > 0.2]
    fuel_fx = fuel_effect_s_per_lap(float(np.median(burns)), float(np.median([x["t"] for x in pool]))) \
        if burns and pool else None
    # Fuel burns off as the race goes on, which makes later laps quicker. Removing that effect means
    # adding it back: corrected slope = raw slope + fuel gain per lap.
    fuel_deg = _r(deg + fuel_fx, 4) if deg is not None and fuel_fx is not None else None
    drift = []
    if len(pool) >= 6:
        k = len(pool) // 3
        early_ns, late_ns = [x["n"] for x in pool[:k]], [x["n"] for x in pool[-k:]]
        for cid in corner_ids:
            rows = per.get(cid) or {}
            e = [rows[n] for n in early_ns if n in rows]
            lt = [rows[n] for n in late_ns if n in rows]
            if not e or not lt:
                continue
            em, lm = _median([m["min_mph"] for m in e]), _median([m["min_mph"] for m in lt])
            et, ltt = _median([m["t"] for m in e]), _median([m["t"] for m in lt])
            drift.append({"id": cid, "early_min_mph": _r(em, 1), "late_min_mph": _r(lm, 1),
                          "delta_mph": _r(lm - em, 1), "early_t": _r(et), "late_t": _r(ltt), "delta_t": _r(ltt - et)})
    summary = stint_summary(deg, fuel_deg, fuel_fx, drift, len(pool))
    return {"laps": out_laps, "phases": phases, "deg_s_per_lap": _r(deg, 4), "fuel_corrected_deg": fuel_deg,
            "fuel_s_per_lap": _r(fuel_fx, 4), "corner_drift": drift, "summary": summary}


def stint_summary(deg, fuel_deg, fuel_fx, drift, n_clean):
    if deg is None:
        return f"Only {n_clean} clean lap(s), which isn't enough to measure how your pace changed over the race."
    parts = []
    if abs(deg) < 0.01:
        parts.append("Your clean laps held steady through the race.")
    else:
        parts.append(f"Your clean laps got about {abs(deg):.2f} s {'slower' if deg > 0 else 'quicker'} per lap as the race went on.")
    if fuel_deg is not None:
        parts.append(f"Allowing for the car getting lighter as fuel burns (an estimate of {fuel_fx:.2f} s a lap), "
                     f"that's roughly {abs(fuel_deg):.2f} s a lap {'slower' if fuel_deg > 0 else 'quicker'} (tyres, track and driving all feed into that).")
    worst = max(drift, key=lambda d: d["delta_t"], default=None)
    if worst and worst["delta_t"] >= 0.05:
        parts.append(f"{worst['id']} faded the most: {worst['delta_t']:.2f} s slower late on"
                     + (f", with {abs(worst['delta_mph']):.0f} mph less at the slowest point." if worst["delta_mph"] <= -1 else "."))
    return " ".join(parts)


# ---------------------------------------------------------------- technique
def typical_metrics(rows):
    """Median of each metric across laps (gear: most common)."""
    if not rows:
        return None
    out = {}
    for k in METRICS:
        vals = [m.get(k) for m in rows if m.get(k) is not None]
        if not vals:
            out[k] = None
        elif k == "gear":
            out[k] = int(st.mode(vals))
        else:
            out[k] = float(np.median(vals))
    return out


def pick(m):
    if not m:
        return None
    return {k: (None if m.get(k) is None else (int(m[k]) if k == "gear" else _r(m[k], 3))) for k in METRICS}


def diff(a, b):
    """a minus b per metric (None where either side is missing)."""
    if not a or not b:
        return None
    return {k: (None if a.get(k) is None or b.get(k) is None else _r(a[k] - b[k], 3)) for k in METRICS}


def quick_metrics(rows_by_n, clean_ns):
    """Median metrics over your clean laps that were in the quickest quarter through this corner
    (corner time at or under the 25th percentile). This is the target for corner tips: your fastest
    whole lap isn't always quick in every corner. None with fewer than 2 such laps."""
    cl = [m for n, m in rows_by_n.items() if n in clean_ns] or list(rows_by_n.values())
    ts = [m["t"] for m in cl if m.get("t") is not None]
    if len(ts) < 4:
        return None
    p25 = float(np.percentile(ts, 25))
    q = [m for m in cl if m.get("t") is not None and m["t"] <= p25 + 1e-9]
    if len(q) < 2:
        return None
    out = typical_metrics(q)
    out["n"] = len(q)
    out["t"] = float(np.median([m["t"] for m in q]))
    return out


def technique_notes(cid, best, typ, ref, ref_name):
    notes = []
    who = (ref_name or "the comparison lap").split(" ")[0]
    if typ and ref:
        d = diff(typ, ref)
        if d["brake_on"] is not None and abs(d["brake_on"]) >= 5:
            notes.append(f"You typically brake {abs(d['brake_on']):.0f} m {'earlier' if d['brake_on'] < 0 else 'later'} than {who}.")
        if d["min_mph"] is not None and abs(d["min_mph"]) >= 1:
            notes.append(f"You carry {abs(d['min_mph']):.0f} mph {'less' if d['min_mph'] < 0 else 'more'} at the slowest point.")
        if d["full_thr"] is not None and abs(d["full_thr"]) >= 10:
            notes.append(f"You get to full throttle {abs(d['full_thr']):.0f} m {'later' if d['full_thr'] > 0 else 'sooner'}.")
        if d["exit_mph"] is not None and abs(d["exit_mph"]) >= 1.5:
            notes.append(f"You come out {abs(d['exit_mph']):.0f} mph {'slower' if d['exit_mph'] < 0 else 'faster'}.")
        if d["coast_s"] is not None and abs(d["coast_s"]) >= 0.1:
            notes.append(f"You spend {abs(d['coast_s']):.1f} s {'longer' if d['coast_s'] > 0 else 'less'} off both pedals.")
        if typ.get("gear") and ref.get("gear") and typ["gear"] != ref["gear"]:
            notes.append(f"You use gear {typ['gear']} at the slowest point, {who} uses {ref['gear']} (different cars can gear differently).")
    if best and typ:
        d = diff(best, typ)
        bits = []
        if d["brake_on"] is not None and abs(d["brake_on"]) >= 5:
            bits.append(f"braked {abs(d['brake_on']):.0f} m {'later' if d['brake_on'] > 0 else 'earlier'}")
        if d["min_mph"] is not None and abs(d["min_mph"]) >= 1:
            bits.append(f"carried {abs(d['min_mph']):.0f} mph {'more' if d['min_mph'] > 0 else 'less'} through the slowest point")
        if d["full_thr"] is not None and abs(d["full_thr"]) >= 10:
            bits.append(f"got to full throttle {abs(d['full_thr']):.0f} m {'sooner' if d['full_thr'] < 0 else 'later'}")
        if bits:
            notes.append("On your best lap you " + _and(bits) + " than usual.")
    if not notes:
        notes.append("Your typical approach here is close to " + (f"{who}'s." if ref else "your quicker laps."))
    return notes


def technique(corners, per, clean_ns, best_n, refs):
    """corners: engine corner_out (ordered); per: corner id -> lap n -> metrics; refs: engine ref_out."""
    main = next((r for r in refs if r.get("main")), refs[0] if refs else None)
    out = []
    for c in corners:
        rows = per.get(c["id"]) or {}
        cl = [m for n, m in rows.items() if n in clean_ns] or list(rows.values())
        typ = typical_metrics(cl)
        best = rows.get(best_n)
        ref = (main or {}).get("corners", {}).get(c["id"]) if main else None
        others = [{"name": r["name"], **pick(r["corners"][c["id"]])} for r in refs
                  if r is not main and r.get("corners", {}).get(c["id"])]
        quick = quick_metrics(rows, clean_ns)
        # "your best lap" notes only where that lap was actually quicker than usual in this corner
        best_quick = bool(best and typ_t(cl) is not None and best.get("t") is not None and best["t"] < typ_t(cl))
        out.append({"id": c["id"], "apex_m": _r(c["apex_m"], 1), "best": pick(best), "typical": pick(typ),
                    "quick": ({**pick(quick), "n": quick["n"], "t": _r(quick["t"])} if quick else None),
                    "best_quick": best_quick,
                    "ref": pick(ref), "ref_name": main["name"] if (main and ref) else None,
                    "vs_ref": diff(typ, ref), "others": others,
                    "notes": technique_notes(c["id"], best if best_quick else None, typ, ref,
                                             main["name"] if main else None)})
    return out


def typ_t(rows):
    ts = [m["t"] for m in rows if m.get("t") is not None]
    return float(np.median(ts)) if ts else None


# ---------------------------------------------------------------- consistency
def iqr(vals):
    if len(vals) < 2:
        return 0.0
    return float(np.percentile(vals, 75) - np.percentile(vals, 25))


def iqr_label(v):
    if v < 0.05:
        return "Locked in"
    if v < 0.12:
        return "Steady"
    if v < 0.25:
        return "Varies"
    return "All over"


def consistency_score(std, within_05):
    """0-100. 60% from the spread of clean laps (0 s std = full marks, 1.2 s or more = none)
    plus 40% from the share of clean laps within 0.5 s of your best."""
    s_std = max(0.0, 1.0 - std / 1.2)
    return int(round(100 * (0.6 * s_std + 0.4 * within_05 / 100.0)))


def score_label(score):
    if score >= 75:
        return "Very consistent"
    if score >= 55:
        return "Consistent"
    if score >= 35:
        return "Up and down"
    return "Scattered"


def consistency(clean, corners, per, pace=None, slow_cut=1.0):
    """Lap-time consistency.

    The score is worked out on `pace` laps when given: every full lap Garage 61 marked clean, minus
    traffic laps, INCLUDING the ones more than slow_cut off the median (a mistake lap is part of how
    consistent you were). Corner spreads use the engine's clean laps.
    off_pace = how many of those laps were more than slow_cut off the median."""
    pool = pace if pace is not None and len(pace) >= 3 else clean
    ts = [x["t"] for x in pool]
    if len(ts) < 3:
        return None
    best = min(ts)
    med = float(np.median(ts))
    std = float(np.std(ts, ddof=1))
    w05 = 100.0 * sum(t - best <= 0.5 for t in ts) / len(ts)
    w10 = 100.0 * sum(t - best <= 1.0 for t in ts) / len(ts)
    off = sorted(x["n"] for x in pool if x["t"] > med + slow_cut)
    score = consistency_score(std, w05)
    clean_ns = {x["n"] for x in clean}
    by_corner = []
    for c in corners:
        vals = [m["t"] for n, m in (per.get(c["id"]) or {}).items() if n in clean_ns]
        if len(vals) >= 3:
            v = iqr(vals)
            by_corner.append({"id": c["id"], "iqr": _r(v), "label": iqr_label(v)})
    label = score_label(score)
    loose = sorted(by_corner, key=lambda b: -b["iqr"])[:2]
    summ = (f"{label}: {len(ts)} clean laps spread by ±{std:.2f} s, and {w05:.0f}% of them were within half a second of your best.")
    if off:
        summ += f" {len(off)} of {len(ts)} were more than {slow_cut:.1f} s off your typical lap."
    if loose and loose[0]["iqr"] >= 0.12:
        summ += " You varied most in " + _and([b["id"] for b in loose if b["iqr"] >= 0.12]) + "."
    return {"score": score, "std": _r(std), "within_05": _r(w05, 1), "within_10": _r(w10, 1), "label": label,
            "by_corner": by_corner, "summary": summ, "n": len(ts), "off_pace": off, "off_pace_n": len(off)}


# ---------------------------------------------------------------- best vs worst
def change_sentences(best, worst):
    """Plain sentences describing how the WORST lap differed from the BEST lap in one corner."""
    out = []
    if not best or not worst:
        return out
    d = {k: (None if best.get(k) is None or worst.get(k) is None else worst[k] - best[k])
         for k in ("brake_on", "peak_brake", "min_mph", "full_thr", "coast_s", "exit_mph", "abs_s", "gear")}
    if d["brake_on"] is not None and abs(d["brake_on"]) >= 5:
        out.append(f"Braked {abs(d['brake_on']):.0f} m {'earlier' if d['brake_on'] < 0 else 'later'}")
    if d["peak_brake"] is not None and abs(d["peak_brake"]) >= 8:
        out.append(f"{'Harder' if d['peak_brake'] > 0 else 'Lighter'} on the brake ({worst['peak_brake']:.0f}% vs {best['peak_brake']:.0f}%)")
    if d["min_mph"] is not None and abs(d["min_mph"]) >= 1:
        out.append(f"{abs(d['min_mph']):.0f} mph {'less' if d['min_mph'] < 0 else 'more'} at the slowest point")
    if d["gear"] is not None and d["gear"] != 0:
        out.append(f"Gear {int(worst['gear'])} instead of {int(best['gear'])} at the slowest point")
    if d["coast_s"] is not None and abs(d["coast_s"]) >= 0.1:
        out.append(f"{abs(d['coast_s']):.1f} s {'more' if d['coast_s'] > 0 else 'less'} coasting")
    if d["abs_s"] is not None and abs(d["abs_s"]) >= 0.1:
        out.append(f"{abs(d['abs_s']):.1f} s {'more' if d['abs_s'] > 0 else 'less'} on ABS")
    if d["full_thr"] is not None and abs(d["full_thr"]) >= 10:
        out.append(f"Full throttle {abs(d['full_thr']):.0f} m {'later' if d['full_thr'] > 0 else 'sooner'}")
    if d["exit_mph"] is not None and abs(d["exit_mph"]) >= 1.5:
        out.append(f"{abs(d['exit_mph']):.0f} mph {'slower' if d['exit_mph'] < 0 else 'faster'} on the exit")
    return out


def best_worst(clean, corners, per, segments=None):
    """Best clean lap against worst clean lap, corner by corner.

    delta_t per zone uses the contiguous lap segments when available (so the zones add up to the lap
    gap), otherwise the corner-zone time. delta_t = worst - best, positive = time lost on the worst lap.
    """
    if len(clean) < 2:
        return None
    b = min(clean, key=lambda x: x["t"])
    w = max(clean, key=lambda x: x["t"])
    if b["n"] == w["n"]:
        return None
    seg = None
    if segments and segments.get("laps"):
        sb, sw = segments["laps"].get(str(b["n"])), segments["laps"].get(str(w["n"]))
        if sb and sw:
            seg = {nm: (sw[j] - sb[j]) for j, nm in enumerate(segments["names"])}
    zones = []
    for c in corners:
        rows = per.get(c["id"]) or {}
        mb, mw = rows.get(b["n"]), rows.get(w["n"])
        if not mb or not mw:
            continue
        dt = seg[c["id"]] if seg and c["id"] in seg else (mw["t"] - mb["t"])
        zones.append({"id": c["id"], "delta_t": _r(dt), "best": pick(mb), "worst": pick(mw),
                      "changes": change_sentences(mb, mw)})
    zones.sort(key=lambda z: -z["delta_t"])
    gap = w["t"] - b["t"]
    lost = [z for z in zones if z["delta_t"] >= 0.05][:2]
    summ = f"Lap {w['n']} was your slowest clean lap, {gap:.2f} s behind lap {b['n']}."
    if lost:
        summ += " Most of it went in " + _and([f"{z['id']} ({z['delta_t']:.2f} s)" for z in lost]) + "."
    return {"best_n": b["n"], "worst_n": w["n"], "best_t": _r(b["t"]), "worst_t": _r(w["t"]), "gap": _r(gap),
            "basis": "segments" if seg else "corner zones", "zones": zones, "summary": summ}


def _and(a):
    a = list(a)
    return a[0] if len(a) == 1 else ", ".join(a[:-1]) + " and " + a[-1] if a else ""


# ---------------------------------------------------------------- entry point
KEYS = ("combo_ids", "irating", "season", "conditions", "stint", "technique", "consistency", "best_worst")


def extend(data, *, laps, clean, best, corners, per, refs, raw_laps, log=None, pace=None, slow_cut=1.0):
    """Add the v2 keys to `data` in place and return it.

    laps     engine lap dicts in lap order (n, t, clean, lap, R ...), after out-laps were dropped
    clean    the engine's clean subset (Garage 61 clean, within slow_cut of the median, not traffic)
    pace     Garage 61 clean laps minus traffic (off-pace laps kept): what the consistency score uses
    slow_cut seconds off the median that count as an off-pace lap
    best     the engine's best lap dict
    corners  engine corner_out list (only corners with enough laps)
    per      corner id -> lap number -> corner_metrics()
    refs     engine ref_out list (comparison laps with per-corner metrics)
    raw_laps Garage 61 lap objects for the race (for iRating, season, conditions)
    """
    data["version"] = VERSION
    for k in KEYS:
        data.setdefault(k, None)

    def guard(name, fn):
        try:
            return fn()
        except Exception as e:  # never let an extra kill the review
            if log:
                log(f"  Skipped the {name} section ({type(e).__name__}: {e}).")
            return None

    ctx = guard("race details", lambda: race_context(raw_laps))
    if ctx:
        data["combo_ids"], data["irating"], data["season"], data["conditions"] = ctx
    clean_ns = {x["n"] for x in clean}
    data["stint"] = guard("stint", lambda: stint(laps, clean, per, [c["id"] for c in corners]))
    data["technique"] = guard("technique", lambda: technique(corners, per, clean_ns, best["n"], refs))
    data["consistency"] = guard("consistency", lambda: consistency(clean, corners, per, pace, slow_cut))
    if pace is not None and len(pace) > 2:   # spread of every Garage 61-clean lap (mistake laps included)
        data["std_clean"] = float(np.std([x["t"] for x in pace], ddof=1))
    data["best_worst"] = guard("best vs worst", lambda: best_worst(clean, corners, per, data.get("segments")))
    return data
