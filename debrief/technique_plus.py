"""Corner technique detail for Debrief 2.3 "Know your driving": how you come off the brake, how you
pick up the throttle and (when the telemetry has it) how much you correct the steering, corner by corner.

    build(laps, clean, corners, per, best, L)  engine time; written to review.json as `technique_plus`
                                              by analysis_plus.extend (new reviews only)
    spread_for_review(data)                   review-dict only: brake-point spread per corner from
                                              corners[].per_lap, so OLD reviews get it too (`brake_spread`)
    link(xs, ys)                              noise-gated "this metric goes with quicker laps" test

Everything works on the engine's 5 m grid (R) inside each corner window [start_m, end_m]. Every number
that feeds a label or a note is stored per lap (`per_lap`) together with the thresholds used (`params`),
so labels can be retuned later without re-downloading anything.

Wording stays correlational ("on your quicker laps you tended to..."), never causal.

Garage 61 request budget (spec 1.6): technique_plus, brake_spread -> 0 requests. Nothing here touches
the network; it only reads telemetry the engine already downloaded or a saved review.json.
"""
from __future__ import annotations

import numpy as np

from . import engine

VERSION = 1

# thresholds (stored in the block as `params`)
BRAKE_ON_PCT = engine.BRAKE_ON_PCT   # 5 %: pedal above this counts as braking (same as the engine)
RELEASE_FRAC = 0.8                   # release starts at the first cell under 80 % of the peak
MIN_PEAK_PCT = 20.0                  # a stop under 20 % peak pedal is a brush, not a braking zone
PICKUP_PCT = 10.0                    # throttle at or above this counts as picked up
FULL_PCT = 95.0                      # ... and this as flat
LIFT_HYST_PCT = 10.0                 # a drop of this many points (with hysteresis) is one lift
STEER_REV_DEG = 3.0                  # a steering swing of this many degrees counts as one reversal
STEER_START_FRAC = 0.3               # steering window starts where |st| first reaches 30 % of its peak
STEER_UNWIND_FRAC = 0.7              # "unwound" = |st| back under 70 % of its peak after the slowest point
STEER_AFTER_M = 150.0                # steering window runs to 150 m after the slowest point
MIN_LAPS = 5                         # clean laps a corner needs before typical/better are given

PARAMS = {"brake_on_pct": BRAKE_ON_PCT, "release_frac": RELEASE_FRAC, "pickup_pct": PICKUP_PCT,
          "full_pct": FULL_PCT, "lift_hyst_pct": LIFT_HYST_PCT, "steer_rev_deg": STEER_REV_DEG,
          "min_laps": MIN_LAPS}

# brake spread labels (IQR of the brake point over clean laps, metres)
SPREAD_LOCKED_M = 5.0
SPREAD_STEADY_M = 12.0
SPREAD_VARIES_M = 25.0
SPREAD_MIN_LAPS = 6
SPREAD_PARAMS = {"locked_m": 5, "steady_m": 12, "varies_m": 25, "min_laps": SPREAD_MIN_LAPS}

BRAKE_KEYS = ("peak_at_m", "release_start_m", "release_end_m", "trail_m", "off_vs_apex_m", "trail_share")
THROTTLE_KEYS = ("pickup_m", "pickup_vs_apex_m", "ramp_m", "ramp_pct_per_10m", "lifts", "tv_excess_pct")
STEER_KEYS = ("steer_peak_deg", "steer_corr", "unwind_vs_apex_m")
LINK_METRICS = (("trail_m", "length of your brake release"),
                ("off_vs_apex_m", "where you come off the brake"),
                ("pickup_vs_apex_m", "where you pick up the throttle"),
                ("ramp_m", "how long you take to get to full throttle"),
                ("lifts", "lifts on the way to full throttle"),
                ("steer_corr", "steering corrections"))
NOT_ENOUGH = "Not enough clean laps through this corner to say."
SAME = "Your braking release and throttle pickup here look the same on your quicker and slower laps."


# ---------------------------------------------------------------- small helpers
def _r(v, d=1):
    return None if v is None else round(float(v), d)


def _med(vals, d=1):
    vals = [v for v in vals if v is not None]
    return round(float(np.median(vals)), d) if vals else None


def _num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    return v if np.isfinite(v) else None


def _round5(x):
    return int(round(abs(x) / 5.0) * 5)


def shape_label(shape):
    """"quick" (pedal dropped fast), "long tail" (still on it late in the release) or "gradual"."""
    if not shape or None in shape:
        return None
    if shape[0] < 0.4:
        return "quick"
    if shape[2] > 0.35:
        return "long tail"
    return "gradual"


def count_lifts(th, hyst=LIFT_HYST_PCT):
    """Drops of at least `hyst` points from the running high; the throttle has to come back up by `hyst`
    from the low before another drop counts (hysteresis, so pedal noise isn't counted)."""
    th = [float(v) for v in th]
    if not th:
        return 0
    lifts, hi, lo, lifting = 0, th[0], th[0], False
    for v in th[1:]:
        if not lifting:
            if v > hi:
                hi = v
            elif hi - v >= hyst:
                lifts += 1
                lifting, lo = True, v
        else:
            lo = min(lo, v)
            if v >= lo + hyst:
                lifting, hi = False, v
    return lifts


def count_reversals(x, swing=STEER_REV_DEG):
    """Sign changes of the steering rate where the wheel moved at least `swing` degrees each way
    (zig-zag with hysteresis). A clean turn-in then unwind reads 1."""
    x = [float(v) for v in x]
    if len(x) < 2:
        return 0
    ref, ext, direction, n = x[0], x[0], 0, 0
    for v in x[1:]:
        if direction == 0:
            if v - ref >= swing:
                direction, ext = 1, v
            elif ref - v >= swing:
                direction, ext = -1, v
        elif direction == 1:
            if v > ext:
                ext = v
            elif ext - v >= swing:
                n, direction, ext = n + 1, -1, v
        else:
            if v < ext:
                ext = v
            elif v - ext >= swing:
                n, direction, ext = n + 1, 1, v
    return n


# ---------------------------------------------------------------- noise-gated link (spec 0.4)
def link(xs, ys) -> dict | None:
    """Is metric xs linked to corner time ys strongly enough to coach on? Uses the engine's shared gate
    (engine.pattern, added in 2.2.2 for finding H3): Spearman rho over at least 10 clean laps, |rho| above
    the Bonferroni-corrected critical value for 7 metrics (never below 0.5; stricter than the spec's
    max(0.5, 2.6/sqrt(n-1))), and effect = |slope| * (p75 - p25) >= 0.03 s.
    Returns {"r", "n", "slope", "effect_s", "better": "higher"|"lower"} or None."""
    pairs = [(float(x), float(y)) for x, y in zip(xs, ys) if _num(x) is not None and _num(y) is not None]
    if len(pairs) < engine.PATTERN_MIN_LAPS:
        return None
    x = [p[0] for p in pairs]
    y = [p[1] for p in pairs]
    hit = engine.pattern(x, y)
    if not hit:
        return None
    rho, slope, n = hit
    effect = abs(slope) * (engine.pct(x, 75) - engine.pct(x, 25))
    return {"r": round(float(rho), 3), "n": int(n), "slope": round(float(slope), 5), "effect_s": round(float(effect), 3),
            "better": "higher" if slope < 0 else "lower"}


# ---------------------------------------------------------------- one lap through one corner
def _window(X, c):
    d = X["d"]
    m = (d >= c["start_m"]) & (d <= c["end_m"])
    if m.sum() < 3:
        return None
    return np.where(m)[0]


def lap_corner(R, c, ch=None) -> dict | None:
    """Raw technique numbers for one lap through one corner (None if the window is empty).
    ch = engine.lap_channels(R) (which channels this lap really has)."""
    ch = ch or engine.lap_channels(R)
    X = engine._looped(R)
    idx = _window(X, c)
    if idx is None:
        return None
    d, v = X["d"], X["v"]
    apex = c["apex_m"]
    near = idx[np.abs(d[idx] - apex) <= 120]
    if len(near) == 0:
        near = idx
    i_min = int(near[np.argmin(v[near])])            # same slowest point as engine.corner_metrics
    min_at = float(d[i_min])
    last = int(idx[-1])
    out = {"min_at": round(min_at, 1)}

    # braking
    braking = None
    i_rel_end = None
    if ch.get("brake"):
        b = X["b"]
        pre = idx[d[idx] <= apex + 40]
        i_b = engine.brake_onset(b, pre)               # the engine's main stop, exactly
        if i_b is not None:
            seg = np.arange(i_b, max(i_b, i_min) + 1)
            k_peak = int(seg[np.argmax(b[seg])])
            peak = float(b[k_peak])
            if peak >= MIN_PEAK_PCT:
                after = np.arange(k_peak + 1, last + 1)
                rs = next((int(i) for i in after if b[i] < RELEASE_FRAC * peak), None)
                re_ = next((int(i) for i in after if b[i] < BRAKE_ON_PCT), None)
                held = re_ is None
                end_m = float(c["end_m"])
                re_m = end_m if held else float(d[re_])
                rs_m = float(d[rs]) if rs is not None else re_m
                rs_m = min(rs_m, re_m)
                i_rel_end = last if held else re_
                trail = re_m - rs_m
                bo_m = float(d[i_b])
                span = re_m - bo_m
                if trail > 0:
                    shape = [float(np.interp(rs_m + f * trail, d, b)) / peak for f in (0.25, 0.5, 0.75)]
                else:   # stepped straight off: nothing left at any point of the "release"
                    shape = [float(b[re_]) / peak if re_ is not None else 1.0] * 3
                shape = [round(min(1.0, max(0.0, s)), 2) for s in shape]
                braking = {"brake_on_m": round(bo_m, 1), "peak_pct": round(peak, 1), "peak_at_m": round(float(d[k_peak]), 1),
                           "release_start_m": round(rs_m, 1), "release_end_m": round(re_m, 1), "held": held,
                           "trail_m": round(trail, 1), "off_vs_apex_m": round(re_m - min_at, 1),
                           "trail_share": round(trail / span, 2) if span > 0 else None,
                           "shape": shape, "shape_label": shape_label(shape)}
    out["braking"] = braking

    # throttle
    throttle = None
    if ch.get("throttle"):
        th = X["th"]
        i0 = max(i_min, i_rel_end if i_rel_end is not None else i_min)
        cells = np.arange(i0, last + 1)
        ip = next((int(i) for i in cells if th[i] >= PICKUP_PCT), None)
        if ip is not None:
            ifl = next((int(i) for i in np.arange(ip, last + 1) if th[i] >= FULL_PCT), None)
            ramp = float(d[ifl] - d[ip]) if ifl is not None else None
            seg = th[ip:(ifl if ifl is not None else last) + 1]
            tv = float(np.sum(np.abs(np.diff(seg))) - (seg[-1] - seg[0])) if len(seg) > 1 else 0.0
            throttle = {"pickup_m": round(float(d[ip]), 1), "pickup_vs_apex_m": round(float(d[ip]) - min_at, 1),
                        "ramp_m": _r(ramp), "ramp_pct_per_10m": (round(85.0 / max(ramp, engine.STEP) * 10, 1)
                                                                 if ramp is not None else None),
                        "lifts": int(count_lifts(seg)), "tv_excess_pct": round(max(0.0, tv), 1)}
    out["throttle"] = throttle

    # steering (never from a lap whose steering channel is flat or missing: it is zero-filled)
    steering = None
    if ch.get("steering") and "st" in X:
        st_abs = np.abs(X["st"])
        peak = float(st_abs[idx].max())
        if peak > 0:
            i0 = next((int(i) for i in idx if st_abs[i] >= STEER_START_FRAC * peak), int(idx[0]))
            i1 = min(len(d) - 1, i_min + int(STEER_AFTER_M / engine.STEP))
            corr = count_reversals(X["st"][i0:i1 + 1]) if i1 > i0 else 0
            iu = next((int(i) for i in range(i_min + 1, last + 1) if st_abs[i] < STEER_UNWIND_FRAC * peak), None)
            steering = {"steer_peak_deg": round(peak, 1), "steer_corr": int(corr),
                        "unwind_vs_apex_m": round(float(d[iu]) - min_at, 1) if iu is not None else None}
    out["steering"] = steering
    return out


def _flat(m):
    """One lap's numbers as the flat per_lap row stored in review.json."""
    row = {}
    b, t, s = m.get("braking") or {}, m.get("throttle") or {}, m.get("steering") or {}
    for k in ("trail_m", "off_vs_apex_m", "release_start_m", "release_end_m", "peak_at_m", "trail_share"):
        row[k] = b.get(k)
    row["shape_label"] = b.get("shape_label")
    row["held"] = b.get("held") if b else None
    for k in THROTTLE_KEYS:
        row[k] = t.get(k)
    for k in STEER_KEYS:
        row[k] = s.get(k)
    return row


def _agg_braking(rows):
    rows = [r for r in rows if r]
    if not rows:
        return None
    out = {k: _med([r.get(k) for r in rows], 2 if k == "trail_share" else 1) for k in BRAKE_KEYS}
    shape = [_med([r["shape"][i] for r in rows if r.get("shape")], 2) for i in range(3)]
    out["shape"] = shape if None not in shape else None
    out["shape_label"] = shape_label(out["shape"])
    out["held"] = sum(bool(r.get("held")) for r in rows) * 2 > len(rows)
    out["n"] = len(rows)
    return out


def _agg(rows, keys):
    rows = [r for r in rows if r]
    if not rows:
        return None
    out = {k: _med([r.get(k) for r in rows]) for k in keys}
    for k in ("lifts", "steer_corr"):   # counts stay whole numbers when the median is one
        if isinstance(out.get(k), float) and out[k].is_integer():
            out[k] = int(out[k])
    out["n"] = len(rows)
    return out


# ---------------------------------------------------------------- notes
def _link_note(lk, typ, bet):
    m, hi = lk["metric"], lk["better"] == "higher"
    tb, bb = (typ or {}).get("braking") or {}, (bet or {}).get("braking") or {}
    tt, bt = (typ or {}).get("throttle") or {}, (bet or {}).get("throttle") or {}

    def about(a, b, unit="m"):
        return f" (about {b:.0f} {unit} against {a:.0f} {unit})" if a is not None and b is not None else ""
    if m == "trail_m":
        return (f"On your quicker laps here you tended to ease off the brake over a {'longer' if hi else 'shorter'} stretch"
                f"{about(tb.get('trail_m'), bb.get('trail_m'))}.")
    if m == "off_vs_apex_m":
        return ("On your quicker laps you tended to stay on the brake a little closer to the slowest point." if hi else
                "On your quicker laps you tended to be off the brake a little earlier, before the slowest point.")
    if m == "pickup_vs_apex_m":
        x = bt.get("pickup_vs_apex_m")
        tail = f" (about {x:.0f} m after it)" if x is not None and x >= 0 else ""
        return (f"On your quicker laps you tended to pick up the throttle sooner after the slowest point{tail}." if not hi else
                f"On your quicker laps you tended to wait a little longer after the slowest point before picking up the throttle{tail}.")
    if m == "ramp_m":
        return (f"On your quicker laps you tended to get to full throttle over a {'longer, smoother' if hi else 'shorter'} stretch"
                f"{about(tt.get('ramp_m'), bt.get('ramp_m'))}.")
    if m == "lifts" and not hi:
        return "Laps where you didn't lift once you'd started on the throttle were usually quicker here."
    if m == "steer_corr" and not hi:
        return "Laps with fewer steering corrections were usually quicker here."
    return None


def notes_for(links, typ, bet):
    out = []
    for lk in links:
        s = _link_note(lk, typ, bet)
        if s and s not in out:
            out.append(s)
    if out:
        return out
    bits = []
    tt, bt = (typ.get("throttle") or {}), (bet.get("throttle") or {})
    a, b = tt.get("pickup_vs_apex_m"), bt.get("pickup_vs_apex_m")
    if a is not None and b is not None and abs(a - b) >= 10:
        bits.append(f"starts the throttle {_round5(a - b)} m {'later' if a > b else 'sooner'}")
    tb, bb = (typ.get("braking") or {}), (bet.get("braking") or {})
    a, b = tb.get("trail_m"), bb.get("trail_m")
    if a is not None and b is not None and abs(a - b) >= 10:
        bits.append(f"eases off the brake over a {_round5(a - b)} m {'longer' if a > b else 'shorter'} stretch")
    if bits:
        return ["Compared with your better laps, your typical lap " + " and ".join(bits) + "."]
    return [SAME]


# ---------------------------------------------------------------- build (engine time)
def channels_of(laps) -> dict:
    """brake / throttle / steering / gps present on at least half the analysed laps."""
    chs = [engine.lap_channels(x["R"]) for x in laps if x.get("R") is not None]
    if not chs:
        return {"brake": False, "throttle": False, "steering": False, "gps": False}
    return {k: sum(bool(c.get(k)) for c in chs) * 2 >= len(chs) for k in ("brake", "throttle", "steering", "gps")}


def build(laps, clean, corners, per, best, L) -> dict | None:
    """laps: engine lap dicts (n, t, R ...); clean: the engine's clean subset; corners: engine corner_out;
    per: corner id -> lap n -> corner_metrics; best: the engine's best lap dict; L: lap length (m)."""
    corners = [c for c in corners or [] if isinstance(c, dict) and c.get("id") is not None]
    if len(corners) < 2:
        return {"version": VERSION, "corners": [], "reason": "no_corners"}
    laps = [x for x in laps or [] if x.get("R") is not None]
    ch_all = channels_of(laps)
    ch = {k: ch_all[k] for k in ("brake", "throttle", "steering")}
    lap_ch = {x["n"]: engine.lap_channels(x["R"]) for x in laps}
    clean_ns = {x["n"] for x in clean or []}
    out_corners = []
    for c in corners:
        cid = c["id"]
        rows = (per or {}).get(cid) or {}
        raw = {}
        for x in laps:
            if x["n"] not in rows:   # no corner reading (broken telemetry was dropped by the engine)
                continue
            lc = dict(lap_ch[x["n"]])
            lc["steering"] = lc.get("steering") and ch["steering"]
            m = lap_corner(x["R"], c, lc)
            if m:
                raw[x["n"]] = m
        cl = [n for n in sorted(raw) if n in clean_ns]
        n_clean = len(cl)
        no_brake = [n for n in cl if raw[n]["braking"] is None]
        lift = bool(ch["brake"] and cl and len(no_brake) * 2 > len(cl))
        entry = {"id": cid, "apex_m": _r(c.get("apex_m")), "n_clean": n_clean, "lift": lift,
                 "braking": None, "throttle": None, "steering": None, "links": [], "notes": [],
                 "per_lap": {str(n): _flat(raw[n]) for n in sorted(raw)}}
        if n_clean < MIN_LAPS:
            entry["notes"] = [NOT_ENOUGH]
            out_corners.append(entry)
            continue
        ts = [rows[n]["t"] for n in cl]
        p25 = float(np.percentile(ts, 25))
        better = [n for n in cl if rows[n]["t"] <= p25 + 1e-9]
        typ, bet = {}, {}
        if ch["brake"] and not lift:
            typ["braking"] = _agg_braking([raw[n]["braking"] for n in cl])
            bet["braking"] = _agg_braking([raw[n]["braking"] for n in better])
            if typ["braking"]:
                entry["braking"] = {"typical": typ["braking"], "better": bet["braking"]}
        if ch["throttle"]:
            typ["throttle"] = _agg([raw[n]["throttle"] for n in cl], THROTTLE_KEYS)
            bet["throttle"] = _agg([raw[n]["throttle"] for n in better], THROTTLE_KEYS)
            if typ["throttle"]:
                entry["throttle"] = {"typical": typ["throttle"], "better": bet["throttle"]}
        if ch["steering"]:
            ts_ = _agg([raw[n]["steering"] for n in cl], STEER_KEYS)
            if ts_:
                entry["steering"] = {"typical": ts_, "better": _agg([raw[n]["steering"] for n in better], STEER_KEYS)}
        entry["better_laps"] = better
        links = []
        for metric, label in LINK_METRICS:
            if metric == "steer_corr" and not ch["steering"]:
                continue
            xs, ys = [], []
            for n in cl:
                v = entry["per_lap"][str(n)].get(metric)
                if v is not None:
                    xs.append(v)
                    ys.append(rows[n]["t"])
            lk = link(xs, ys)
            if lk:
                links.append({"metric": metric, "label": label, **lk})
        links.sort(key=lambda z: -abs(z["r"]))
        entry["links"] = links[:2]
        entry["notes"] = notes_for(entry["links"], typ, bet)
        out_corners.append(entry)
    return {"version": VERSION, "step_m": engine.STEP, "channels": ch, "params": dict(PARAMS),
            "corners": out_corners, "reason": None}


# ---------------------------------------------------------------- brake spread (review dict, old reviews too)
def spread_label(iqr_m):
    if iqr_m <= SPREAD_LOCKED_M:
        return "locked"
    if iqr_m <= SPREAD_STEADY_M:
        return "steady"
    if iqr_m <= SPREAD_VARIES_M:
        return "varies"
    return "all over"


def spread_for_review(data: dict) -> dict | None:
    """Brake-point spread per corner from corners[].per_lap[n].brake_on over clean laps (all laps if the
    review has no clean flags). Needs SPREAD_MIN_LAPS laps with a brake point; lift corners have none and
    are left out. None when the review has no corners at all."""
    if not isinstance(data, dict) or not isinstance(data.get("corners"), list):
        return None
    laps = [l for l in data.get("laps") or [] if isinstance(l, dict)]
    flagged = any("clean" in l for l in laps)
    clean = {str(l.get("n")) for l in laps if l.get("clean")} if flagged else None
    out = []
    for c in data["corners"]:
        if not isinstance(c, dict) or not isinstance(c.get("per_lap"), dict):
            continue
        vals = {}
        for n, m in c["per_lap"].items():
            if not isinstance(m, dict) or (clean is not None and str(n) not in clean):
                continue
            b = _num(m.get("brake_on"))
            if b is not None:
                vals[str(n)] = round(b, 1)
        if len(vals) < SPREAD_MIN_LAPS:
            continue
        a = np.array(list(vals.values()), float)
        iqr = float(np.percentile(a, 75) - np.percentile(a, 25))
        out.append({"id": c.get("id"), "n": len(a), "iqr_m": round(iqr, 1), "std_m": round(float(np.std(a, ddof=1)), 1),
                    "median_m": round(float(np.median(a)), 1), "label": spread_label(iqr),
                    "laps": dict(sorted(vals.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0))})
    return {"version": VERSION, "corners": out, "params": dict(SPREAD_PARAMS)}
