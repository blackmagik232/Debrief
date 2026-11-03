"""Corner coach (2.13): per-corner advice in plain words from your own iRacing telemetry (.ibt files).

For each corner it compares your own best laps through that corner with your typical laps, finds the ONE
clearest difference in how you drove it (and at most one more), and says how to find the time:

    "T4: you lose about 0.25 s here on a typical lap. What's different: on your best laps you brake at 85%
     instead of 100% ... How to try it: ... What it gets you: ... Evidence: 6 best laps vs 14 typical laps."

plus a short voice phrase for the live coach ("T4: lighter brake, hold it in") and a one-line "why".

Public API (all JSON-friendly, nothing raises for bad input):
    analyze_files(paths, reference=None)  -> report dict (see analyze_laps)
    load_laps(path_or_paths)              -> (laps, info): clean laps on the corner model's 5 m grid
    analyze_laps(laps, reference=None, info=None) -> report dict
    phases(R, corner)                     -> per-lap phase numbers for one corner (or None)
    compare(laps, corners)                -> per-corner comparison rows (cards, unclear, ok)
    braking_summary(laps, corners)        -> session-level braking habits in plain words
    lap_from_raw(raw, lap_time, length_m, label) -> a lap (e.g. a teammate's Garage 61 lap) to pass as reference

The corner model is Debrief's own (engine.resample on the 5 m grid, engine.detect_corners on your fastest lap,
engine.brake_onset for the brake point): the same C1..Cn numbering the reviews and the live coach use. The spoken
name is "T<n>" for corner C<n>, as livecoach does.

Honesty rules:
  - own laps only: best = your fastest third through that corner (at least 3), typical = the rest (at least 3);
    at least MIN_LAPS clean laps in total. An optional reference lap (a teammate's) gives separate, clearly
    labelled cards, never mixed with your own.
  - a card only when the typical lap loses at least MIN_LOST_S in that corner AND a phase number differs by more
    than its rule threshold AND by more than the lap-to-lap noise (difference of medians > Z_LIKELY x its
    standard error, spread from the median absolute deviation). Otherwise "no clear pattern yet".
  - nothing is said that the numbers do not show; every card carries the numbers both ways and the lap counts.

Note on "time lost": best laps are picked by being fastest in that corner, so the gap is partly lap-to-lap
scatter. It is what your typical lap gives away against your own best, not a promise of lap time.
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from . import engine, ibt

VERSION = 1
KMH = 3.6
STEP = engine.STEP

# ---------------------------------------------------------------- thresholds (all in one place)
MIN_LAPS = 6                # clean laps needed in total
MIN_GROUP = 3               # laps needed in each of best / typical
BEST_FRAC = 1.0 / 3.0       # best = fastest third through the corner
MIN_LOST_S = 0.05           # a corner needs at least this much typical-vs-best loss for a card
OFFPACE_FRAC = 1.07         # clean laps slower than 107% of the median lap are left out (traffic, a moment)
MIN_COVER = 0.95            # a lap's LapDistPct must cover this much of the track

BRAKE_ON_PCT = engine.BRAKE_ON_PCT  # 5 %: pedal above this counts as braking (same as the engine)
FULL_BRAKE_PCT = 95.0       # "all the way"
RELEASE_FRAC = 0.8          # release starts at the last cell at >= 80% of the peak before the brake comes off
STEER_FRAC = 0.3            # turn-in: steering reaches 30% of its peak in the corner ...
STEER_MIN_DEG = 5.0         # ... and at least this many degrees
COAST_PCT = 5.0             # brake and throttle both under this = coasting ...
COAST_MIN_KMH = 30.0        # ... above this speed
PICKUP_PCT = 20.0           # throttle above this (for 3 cells, 15 m) = picked up
PICKUP_HOLD = 3
FULL_THR_PCT = 95.0
ABS_ZONE_SHARE = 0.30       # a braking zone "relies on ABS" when ABS is active over this share of it
SPIN_FRAC = 0.06            # rear wheels this much faster than their usual ratio to car speed = wheelspin
SPIN_RPM_FRAC = 0.06        # (no wheel speeds) RPM / speed this much above the gear's usual ratio
LOCK_FRAC = 0.12            # a front wheel this much slower than its usual ratio under braking = lock-up
LOCK_MIN_CELLS = 2          # ... for at least 2 grid cells (10 m)

# rules: the practical size a difference must reach (best vs typical medians)
R_BRAKE_M = 10.0            # brake point moved by >= 10 m
R_PEAK_PCT = 8.0            # peak pedal lower by >= 8 points ...
R_PEAK_TYP = 95.0           # ... while typical laps press to >= 95 %
R_ABS_SHARE = 0.15          # ... or ABS share lower by >= 0.15 (with typical ABS share >= ABS_ZONE_SHARE)
R_COAST_S = 0.25            # coasting shorter by >= 0.25 s
R_TRAIL_PCT = 5.0           # brake still on at turn-in higher by >= 5 points
R_RELEASE_S = 0.20          # brake let off over >= 0.2 s longer
R_PICKUP_M = 15.0           # throttle picked up >= 15 m earlier
R_MIN_KMH = 3.0             # >= 3 km/h more at the slowest point ...
R_ENTRY_KMH = 3.0           # ... with entry speed within 3 km/h
R_SLIP_SHARE = 0.10         # wheelspin / lock-up share lower by >= 0.10

Z_LIKELY = 2.0              # difference of medians > 2 standard errors = beyond the lap-to-lap noise
Z_CLEAR = 3.0               # > 3 standard errors and CLEAR_LAPS laps = "clear"
CLEAR_LAPS = 12
THIN_LAPS = 9               # fewer laps than this = "thin" even when the difference passes
# smallest spread assumed per metric, so a perfectly repeatable number can't make a tiny difference "significant"
NOISE_FLOOR = {"brake_m": 3.0, "peak_pct": 2.0, "abs_share": 0.03, "coast_s": 0.04, "trail_pct": 2.0,
               "release_s": 0.04, "pickup_m": 4.0, "min_kmh": 0.8, "entry_kmh": 0.8, "exit_kmh": 0.8,
               "spin_share": 0.03, "lock_share": 0.03, "full_s": 0.04, "t": 0.01, "release_end_m": 3.0,
               "full_thr_s": 0.04}

CONFIDENCE_RULE = (f"clear: difference beyond {Z_CLEAR:g}x the lap-to-lap noise with {CLEAR_LAPS}+ laps; "
                   f"likely: beyond {Z_LIKELY:g}x; thin: passes but with fewer than {THIN_LAPS} laps "
                   f"(or a single reference lap)")

PARAMS = {k: v for k, v in globals().items() if k.isupper() and isinstance(v, (int, float)) and not isinstance(v, bool)}

CHANNELS = ("Lap", "LapLastLapTime", "SessionNum", "OnPitRoad", "PlayerTrackSurface", "PlayerCarMyIncidentCount",
            "IsOnTrack", "LapDistPct", "Speed", "Brake", "Throttle", "Gear", "RPM", "SteeringWheelAngle",
            "BrakeABSactive", "LFspeed", "RFspeed", "LRspeed", "RRspeed")
WHEELS = ("LFspeed", "RFspeed", "LRspeed", "RRspeed")


# ---------------------------------------------------------------- small helpers
def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _r(v, d=2):
    v = _num(v)
    return None if v is None else round(v, d)


def _round_to(x, step):
    return int(round(float(x) / step) * step)


def _med(vals):
    vals = [v for v in (_num(x) for x in vals) if v is not None]
    return float(np.median(vals)) if vals else None


def _kmh(mph):
    return float(mph) / engine.MPH * KMH


def _words(s: str) -> int:
    return len([w for w in s.replace(",", " ").replace(":", " ").split() if w])


# ---------------------------------------------------------------- reading laps
def _raw(cols: dict, a: int, b: int) -> dict:
    """engine.parse_csv's raw shape for samples a..b (missing channels zero-filled and listed in _missing).
    ABS comes from BrakeABSactive (the .ibt name)."""
    n = b - a
    names = {"v": "Speed", "p": "LapDistPct", "b": "Brake", "th": "Throttle", "gear": "Gear", "abs": "BrakeABSactive",
             "la": "LatAccel", "lo": "LongAccel", "st": "SteeringWheelAngle", "rpm": "RPM"}
    raw, missing = {}, []
    for k, nm in names.items():
        col = cols.get(nm)
        if col is None:
            missing.append("ABSActive" if nm == "BrakeABSactive" else nm)
            raw[k] = np.zeros(n)
        else:
            raw[k] = np.asarray(col[a:b], float)
    raw["lat"] = np.full(n, np.nan)
    raw["lon"] = np.full(n, np.nan)
    raw["_missing"] = sorted(set(missing))
    for w in WHEELS:
        if cols.get(w) is not None:
            raw[w] = np.asarray(cols[w][a:b], float)
    return raw


def _grid(raw: dict, lap_time: float, L: float) -> dict:
    """One lap on the engine's 5 m grid (engine.resample), plus wheel speeds (mph, like v) on the same grid."""
    R = engine.resample(raw, float(lap_time), float(L))
    if all(w in raw for w in WHEELS):
        n = len(raw["v"])
        d = engine._unwrap(raw["p"]) * L
        d = np.maximum.accumulate(d + np.arange(n) * 1e-6)
        for w in WHEELS:
            R[w] = np.interp(R["d"], d, raw[w]) * engine.MPH
    R["_wheels"] = all(w in R for w in WHEELS)
    R["_abs"] = "ABSActive" not in R["_missing"]
    R["_steer"] = "SteeringWheelAngle" not in R["_missing"] and float(np.ptp(R["st"])) > engine.STEER_FLAT_DEG
    _bases(R)
    return R


def _bases(R):
    """Each lap's own usual wheel/car speed ratios (rear tyres run a little faster than the car under drive),
    and RPM per mph in each gear, so wheelspin and lock-up are measured against this lap's normal."""
    v = R["v"]
    ok = v > 25.0                                        # mph
    R["_base"] = {}
    if R.get("_wheels") and ok.sum() > 20:
        rear = (R["LRspeed"] + R["RRspeed"]) / 2.0
        front = np.minimum(R["LFspeed"], R["RFspeed"])
        R["_base"]["rear"] = float(np.median(rear[ok] / v[ok]))
        R["_base"]["front"] = float(np.median(front[ok] / v[ok]))
    gears = {}
    if "RPM" not in R["_missing"] and "Gear" not in R["_missing"]:
        for gr in np.unique(R["gear"]):
            m = ok & (R["gear"] == gr) & (R["th"] > 95)
            if gr > 0 and m.sum() >= 5:
                gears[int(gr)] = float(np.median(R["rpm"][m] / v[m]))
    R["_base"]["rpm"] = gears


def _length_m(ib) -> float | None:
    km = ib.track().get("length_km")
    if km and km > 0.1:
        return km * 1000.0
    if ib.has("LapDist"):
        v = ib.col("LapDist")
        return float(np.nanmax(v)) if len(v) else None
    return None


def load_laps(src) -> tuple:
    """Clean laps from one .ibt path or a list of them, as (laps, info).

    A lap: {"file", "lap", "time", "session", "R"} with R on the engine's 5 m grid. Only clean laps (ibt.laps: no
    pit road, no off-track, no incident, timed, not race lap 1) that cover the track. Files of a different car or
    track than the first readable one are skipped (listed in info["skipped"]). Never raises for a bad file."""
    paths = src if isinstance(src, (list, tuple)) else [src]
    laps, info = [], {"files": [], "skipped": [], "car": None, "track": None, "length_m": None,
                      "wheel_speeds": False, "abs_channel": False}
    combo = None
    for p in paths:
        name = Path(str(p)).name
        try:
            with ibt.open_ibt(p) as ib:
                car, tr = ib.car(), ib.track()
                key = (str(car.get("path") or "").lower(), str(tr.get("code") or "").lower())
                if combo is None:
                    combo = key
                    info["car"], info["track"] = car.get("name") or car.get("path"), tr.get("name")
                    info["length_m"] = _length_m(ib)
                elif key != combo:
                    info["skipped"].append({"file": name, "why": "a different car or track"})
                    continue
                L = info["length_m"]
                if not L or L < 100:
                    info["skipped"].append({"file": name, "why": "track length unknown"})
                    combo = None if not laps else combo
                    continue
                cols = {nm: ib.col(nm) for nm in CHANNELS if ib.has(nm)}
                if "Speed" not in cols or "LapDistPct" not in cols:
                    info["skipped"].append({"file": name, "why": "no speed or lap distance channel"})
                    continue
                n_clean = 0
                for x in ibt.laps(ib):
                    if not x["clean"] or not x["time"]:
                        continue
                    raw = _raw(cols, x["start"], x["end"])
                    if len(raw["v"]) < 100 or engine.coverage(raw) < MIN_COVER or engine.pedals_missing(raw):
                        continue
                    R = _grid(raw, x["time"], L)
                    laps.append({"file": name, "lap": x["lap"], "time": float(x["time"]), "session": x["session"],
                                 "R": R})
                    n_clean += 1
                info["files"].append({"file": name, "clean_laps": n_clean,
                                      "session": ", ".join(sorted(set(x["session"] for x in laps
                                                                      if x["file"] == name and x["session"])))})
        except Exception as e:  # noqa: BLE001
            info["skipped"].append({"file": name, "why": f"could not be read ({str(e)[:120]})"})
    info["wheel_speeds"] = bool(laps) and all(x["R"]["_wheels"] for x in laps)
    info["abs_channel"] = bool(laps) and all(x["R"]["_abs"] for x in laps)
    return laps, info


def lap_from_raw(raw: dict, lap_time: float, length_m: float, label: str = "Reference lap") -> dict | None:
    """A reference lap from engine.parse_csv's raw shape (a Garage 61 CSV, a teammate's lap). None if unusable."""
    try:
        if not lap_time or not length_m or len(raw.get("v", [])) < 100 or engine.coverage(raw) < MIN_COVER:
            return None
        return {"file": None, "lap": None, "time": float(lap_time), "session": "", "label": str(label),
                "R": _grid(raw, float(lap_time), float(length_m))}
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- one lap through one corner
def _loop(R):
    """R with its last/first 600 m copied before/after (as engine._looped) for every grid channel. Cached."""
    if "_cc_loop" in R:
        return R["_cc_loop"]
    d, tt = R["d"], R["tt"]
    n = len(d)
    L = float(d[-1] + STEP)
    T = float(R.get("T") or tt[-1])
    k = min(n, int(600 / STEP))
    out = {}
    for key, a in R.items():
        if isinstance(a, np.ndarray) and a.shape == d.shape and key not in ("d", "tt"):
            out[key] = np.concatenate([a[-k:], a, a[:k]])
    out["d"] = np.concatenate([d[-k:] - L, d, d[:k] + L])
    out["tt"] = np.concatenate([tt[-k:] - T, tt, tt[:k] + T])
    out["dt"] = np.diff(out["tt"], append=out["tt"][-1])
    R["_cc_loop"] = out
    return out


def phases(R, corner) -> dict | None:
    """Phase numbers for one lap (R: one lap on the 5 m grid, as load_laps gives) through one corner (an
    engine.detect_corners dict). Distances are metres from the corner's apex on your fastest lap (a fixed place
    on track, so "10 m later" is a marker you can see); negative = before it. None if the corner can't be read.

    brake_m       brake point (engine.brake_onset: the main stop, pedal > 5 %)        None for a lift/flat corner
    peak_pct      highest pedal %;  full_s  time at > 95 %
    abs_share     share of the braking zone with ABS working (None without the channel)
    release_m/_s  from the last firm pedal (>= 80 % of peak) to < 5 %;  release_end_m  where the brake is off
    turnin_m      where steering reaches 30 % of its peak;  trail_pct  brake % there (0 = off before turn-in)
    coast_s       brake and throttle both < 5 % above 30 km/h, from brake release to throttle pick-up
    min_kmh/min_m slowest point;  entry_kmh  fastest point before the brake point
    pickup_m      throttle > 20 % (held 15 m) after the brake comes off;  full_thr_s  from there to > 95 %
    exit_kmh      speed at the engine's exit distance after the apex;  gear  at the slowest point
    spin_share    share of the exit (throttle > 20 %) with wheelspin;  lock_share/lock_events under braking
    slip_method   "wheel speeds" | "rpm" | None;  lock_method  "wheel speeds" | "abs" | None
    t             time through the corner zone (s)"""
    try:
        return _phases(R, corner)
    except Exception:  # noqa: BLE001
        return None


def _phases(R, c):
    X = _loop(R)
    d, v, b, th, dt = X["d"], X["v"], X["b"], X["th"], X["dt"]
    a0, a1, apex = float(c["start_m"]), float(c["end_m"]), float(c["apex_m"])
    m = (d >= a0) & (d <= a1)
    if m.sum() < 5:
        return None
    idx = np.where(m)[0]
    last = int(idx[-1])
    near = idx[np.abs(d[idx] - apex) <= 120]
    if len(near) == 0:
        near = idx
    i_min = int(near[np.argmin(v[near])])
    out = {"t": float(np.interp(a1, d, X["tt"]) - np.interp(a0, d, X["tt"])),
           "min_kmh": _kmh(v[i_min]), "min_m": float(d[i_min]) - apex,
           "gear": int(X["gear"][i_min]) if "Gear" not in R["_missing"] else None}
    ex = apex + float(c.get("exit_at_m") or engine.exit_offset_m(apex, a1))
    out["exit_kmh"] = _kmh(np.interp(ex, d, v))

    # braking
    pre = idx[d[idx] <= apex + 40]
    bo = engine.brake_onset(b, pre)
    re_ = None
    keys = ("brake_m", "peak_pct", "full_s", "abs_share", "release_m", "release_s", "release_end_m", "lock_share",
            "lock_events")
    out.update({k: None for k in keys})
    if bo is not None:
        bo = int(bo)
        after = np.arange(bo, last + 1)
        off = next((int(i) for i in after[1:] if b[i] < BRAKE_ON_PCT), None)
        # a brief re-application inside the zone (< 30 m gap) stays the same stop, as engine.brake_onset bridges
        gap = max(1, int(round(engine.BRAKE_GAP_M / STEP)))
        while off is not None:
            nxt = next((int(i) for i in range(off, min(last, off + gap) + 1) if b[i] > BRAKE_ON_PCT), None)
            if nxt is None:
                break
            off = next((int(i) for i in range(nxt, last + 1) if b[i] < BRAKE_ON_PCT), None)
        re_ = off if off is not None else last
        zone = np.arange(bo, re_ + 1)
        peak = float(b[zone].max())
        firm = [int(i) for i in zone if b[i] >= RELEASE_FRAC * peak]
        rs = firm[-1] if firm else bo
        zt = float(dt[zone].sum()) or 1e-9
        out.update({"brake_m": float(d[bo]) - apex, "peak_pct": peak,
                    "full_s": float(dt[zone][b[zone] > FULL_BRAKE_PCT].sum()),
                    "abs_share": (float(dt[zone][X["abs"][zone] > 0.5].sum()) / zt) if R.get("_abs") else None,
                    "release_m": float(d[re_] - d[rs]), "release_s": float(X["tt"][re_] - X["tt"][rs]),
                    "release_end_m": float(d[re_]) - apex})
        out["entry_kmh"] = _kmh(v[idx[0]:bo + 1].max()) if bo >= idx[0] else _kmh(v[bo])
    else:
        out["entry_kmh"] = _kmh(v[idx[0]:i_min + 1].max())

    # turn-in and trail braking
    out["turnin_m"] = out["trail_pct"] = None
    if R.get("_steer") and "st" in X:
        st = np.abs(X["st"])
        lo = bo if bo is not None else int(idx[0])
        seg = np.arange(lo, i_min + 1)
        pk = float(st[idx].max())
        thr = max(STEER_MIN_DEG, STEER_FRAC * pk)
        ti = next((int(i) for i in seg if st[i] >= thr), None)
        if ti is not None:
            out["turnin_m"] = float(d[ti]) - apex
            out["trail_pct"] = float(b[ti]) if b[ti] >= BRAKE_ON_PCT else 0.0

    # throttle pick-up, coasting
    s0 = re_ if re_ is not None else (int(next((i for i in idx if th[i] < PICKUP_PCT), idx[0])))
    pu = None
    for i in range(s0, last - PICKUP_HOLD + 2):
        if all(th[j] > PICKUP_PCT for j in range(i, i + PICKUP_HOLD)):
            pu = i
            break
    out["pickup_m"] = float(d[pu]) - apex if pu is not None else None
    if pu is not None:
        fu = next((int(i) for i in range(pu, last + 1) if th[i] >= FULL_THR_PCT), None)
        out["full_thr_s"] = float(X["tt"][fu] - X["tt"][pu]) if fu is not None else None
    else:
        out["full_thr_s"] = None
    cz = np.arange(s0, (pu if pu is not None else last) + 1)
    coast = (b[cz] < COAST_PCT) & (th[cz] < COAST_PCT) & (v[cz] / engine.MPH * KMH > COAST_MIN_KMH)
    out["coast_s"] = float(dt[cz][coast].sum())

    # wheelspin on exit, lock-up under braking
    base = R.get("_base") or {}
    out["spin_share"] = out["slip_method"] = None
    ez = np.arange(i_min, last + 1)
    ez = ez[th[ez] > PICKUP_PCT]
    vz = np.maximum(v, 1e-3)
    if R.get("_wheels") and "rear" in base:
        out["slip_method"] = "wheel speeds"
        if len(ez):
            rear = (X["LRspeed"][ez] + X["RRspeed"][ez]) / 2.0
            spin = rear / vz[ez] > base["rear"] * (1 + SPIN_FRAC)
            out["spin_share"] = float(dt[ez][spin].sum() / max(dt[ez].sum(), 1e-9))
        else:
            out["spin_share"] = 0.0
    elif base.get("rpm"):
        out["slip_method"] = "rpm"
        if len(ez):
            g = X["gear"][ez].astype(int)
            ref = np.array([base["rpm"].get(int(x), np.nan) for x in g])
            ok = np.isfinite(ref)
            spin = ok & (X["rpm"][ez] / vz[ez] > ref * (1 + SPIN_RPM_FRAC))
            out["spin_share"] = float(dt[ez][spin].sum() / max(dt[ez][ok].sum(), 1e-9)) if ok.any() else None
        else:
            out["spin_share"] = 0.0
    out["lock_method"] = None
    if bo is not None:
        zone = np.arange(bo, re_ + 1)
        zone = zone[v[zone] > 20.0]                          # mph: a wheel at walking pace says nothing
        if R.get("_wheels") and "front" in base:
            out["lock_method"] = "wheel speeds"
            if len(zone):
                fr = np.minimum(X["LFspeed"][zone], X["RFspeed"][zone]) / vz[zone]
                lk = fr < base["front"] * (1 - LOCK_FRAC)
                out["lock_share"] = float(dt[zone][lk].sum() / max(dt[zone].sum(), 1e-9))
                runs, cur = 0, 0
                for x in lk:
                    cur = cur + 1 if x else 0
                    if cur == LOCK_MIN_CELLS:
                        runs += 1
                out["lock_events"] = runs
            else:
                out["lock_share"], out["lock_events"] = 0.0, 0
        elif out["abs_share"] is not None:
            out["lock_method"] = "abs"
    return out


# ---------------------------------------------------------------- comparing best vs typical
def _spread(vals, key):
    a = np.asarray(vals, float)
    mad = float(np.median(np.abs(a - np.median(a)))) * 1.4826 if len(a) else 0.0
    return max(mad, NOISE_FLOOR.get(key, 0.0))


def diff_test(best_vals, typ_vals, key) -> dict | None:
    """Difference of medians (best - typical) and how many standard errors it is.
    None with fewer than MIN_GROUP readings on either side."""
    bb = [v for v in (_num(x) for x in best_vals) if v is not None]
    tt = [v for v in (_num(x) for x in typ_vals) if v is not None]
    if len(bb) < MIN_GROUP or len(tt) < MIN_GROUP:
        return None
    mb, mt = float(np.median(bb)), float(np.median(tt))
    se = 1.2533 * math.sqrt(_spread(bb, key) ** 2 / len(bb) + _spread(tt, key) ** 2 / len(tt))
    diff = mb - mt
    z = abs(diff) / se if se > 0 else (99.0 if diff else 0.0)
    return {"best": mb, "typical": mt, "diff": diff, "z": min(99.0, z), "se": se,
            "n_best": len(bb), "n_typical": len(tt)}


def _rank_strength(rows, key):
    """|Spearman r| between this metric and corner time over every lap (0 when it can't be worked out)."""
    xs = [r.get(key) for r in rows]
    ys = [r.get("t") for r in rows]
    pairs = [(float(x), float(y)) for x, y in zip(xs, ys) if _num(x) is not None and _num(y) is not None]
    if len(pairs) < 6:
        return 0.0
    rho = engine.spearman([p[0] for p in pairs], [p[1] for p in pairs])
    return abs(rho) if rho is not None else 0.0


def split(rows) -> tuple:
    """(best, typical) rows by corner time: best = fastest third (at least MIN_GROUP), typical = the rest."""
    rows = sorted([r for r in rows if _num(r.get("t")) is not None], key=lambda r: r["t"])
    k = max(MIN_GROUP, int(round(len(rows) * BEST_FRAC)))
    return rows[:k], rows[k:]


def _five(x):
    return max(5, _round_to(abs(x), 5))


def _rules(D, P):
    """The rules that fire. D: metric -> diff_test result (best - typical). P: flags (abs channel present ...).
    Returns [(rule, metric, strength_key)]. Each rule also needs the noise gate (z >= Z_LIKELY) on its metric."""
    fired = []

    def ok(k):
        x = D.get(k)
        return x is not None and x["z"] >= Z_LIKELY

    x = D.get("brake_m")
    if ok("brake_m") and x["diff"] >= R_BRAKE_M:
        fired.append(("brake_later", "brake_m"))
    elif ok("brake_m") and x["diff"] <= -R_BRAKE_M:
        fired.append(("brake_earlier", "brake_m"))
    pk, ab = D.get("peak_pct"), D.get("abs_share")
    lighter_peak = ok("peak_pct") and pk["typical"] >= R_PEAK_TYP and pk["diff"] <= -R_PEAK_PCT
    lighter_abs = (ok("abs_share") and ab["typical"] >= ABS_ZONE_SHARE and ab["diff"] <= -R_ABS_SHARE
                   and pk is not None and pk["typical"] >= R_PEAK_TYP and pk["diff"] < 0)
    if lighter_peak or lighter_abs:
        fired.append(("lighter", "peak_pct" if lighter_peak else "abs_share"))
    co, tr = D.get("coast_s"), D.get("trail_pct")
    trail = ok("trail_pct") and tr["diff"] >= R_TRAIL_PCT
    if ok("coast_s") and co["diff"] <= -R_COAST_S:
        fired.append(("trail" if (tr is not None and tr["diff"] >= R_TRAIL_PCT) else "coast", "coast_s"))
    elif trail:
        fired.append(("trail", "trail_pct"))
    x = D.get("release_s")
    if ok("release_s") and x["diff"] >= R_RELEASE_S:
        fired.append(("release", "release_s"))
    x = D.get("pickup_m")
    if ok("pickup_m") and x["diff"] <= -R_PICKUP_M:
        fired.append(("throttle", "pickup_m"))
    mn, en = D.get("min_kmh"), D.get("entry_kmh")
    explained = {r for r, _k in fired} & {"brake_later", "brake_earlier", "lighter", "trail", "release"}
    if (ok("min_kmh") and mn["diff"] >= R_MIN_KMH and en is not None and abs(en["diff"]) <= R_ENTRY_KMH
            and not explained):              # a fallback: when another rule already explains the speed, that one is said
        fired.append(("carry", "min_kmh"))
    x = D.get("spin_share")
    if ok("spin_share") and x["diff"] <= -R_SLIP_SHARE:
        fired.append(("spin", "spin_share"))
    x = D.get("lock_share")
    if ok("lock_share") and x["diff"] <= -R_SLIP_SHARE:
        fired.append(("lock", "lock_share"))
    return fired


FAMILY = {"brake_later": "point", "brake_earlier": "point", "lighter": "pressure", "lock": "pressure",
          "trail": "release", "coast": "release", "release": "release", "carry": "release",
          "throttle": "throttle", "spin": "throttle"}
VOICE = {"brake_later": "brake later", "brake_earlier": "brake earlier", "lighter": "lighter brake",
         "trail": "hold it in", "coast": "no coasting", "release": "ease off slower", "throttle": "throttle earlier",
         "carry": "carry more speed", "spin": "smoother throttle", "lock": "smoother brake"}
VOICE_ALONE = {"trail": "trail the brake in"}


def _phrase(rule, D, P, label=None):
    """(what's different, how to try it, why) for one rule, from the numbers."""
    def g(k, w="best"):
        return D[k][w] if D.get(k) else None
    who = "on your best laps" if not label else f"on {label}"
    if rule in ("brake_later", "brake_earlier"):
        n = _five(D["brake_m"]["diff"])
        b, t = abs(g("brake_m")), abs(g("brake_m", "typical"))
        later = rule == "brake_later"
        diff = (f"{who} you start braking about {n} m {'later' if later else 'earlier'} "
                f"({_round_to(b, 5)} m before the apex instead of {_round_to(t, 5)} m)")
        cars = max(1, int(round(n / 4.5)))
        how = (f"brake about {n} m {'later' if later else 'earlier'}: pick a marker {n} m "
               f"{'past' if later else 'before'} your current one (about {cars} car length{'s' if cars > 1 else ''}), "
               f"and keep the same pressure.")
        why = ("Every metre you stay flat out before braking is time you don't spend slowing down." if later else
               "Braking a touch earlier lets you get the car slowed and turned instead of running out of road.")
        return diff, how, why
    if rule == "lighter":
        x = _round_to(g("peak_pct"), 5)
        t = _round_to(g("peak_pct", "typical"), 5)
        diff = f"{who} you brake at {x}% instead of {t}%"
        if D.get("abs_share") and P.get("abs") and D["abs_share"]["z"] >= Z_LIKELY and D["abs_share"]["diff"] < 0:
            diff += (f" (the ABS is working {int(round(100 * g('abs_share')))}% of the braking instead of "
                     f"{int(round(100 * g('abs_share', 'typical')))}%)")
        onset = P.get("abs_onset_pct")
        below = onset is not None and g("peak_pct") < onset - 2
        how = (f"same braking marker. Press firmly but not all the way: about {x}%"
               + (f", just short of where the ABS starts working (about {_round_to(onset, 5)}% on this car)."
                  if below else "."))
        less_abs = D.get("abs_share") and D["abs_share"]["z"] >= Z_LIKELY and D["abs_share"]["diff"] <= -R_ABS_SHARE
        if P.get("abs") and (below or less_abs):
            why = "Braking at 100% makes the ABS work, and that stops you no faster."
        elif P.get("abs"):
            why = "Past the point where the ABS steps in, more pedal stops you no faster."
        else:
            why = "Past a certain point more pedal only overloads the tyres, and that stops you no faster."
        return diff, how, why
    if rule == "trail":
        y = _five(g("trail_pct")) if g("trail_pct") is not None else 5
        tt = g("trail_pct", "typical")
        co = D.get("coast_s")
        some = "a little" if y < 25 else "some"
        bits = f"keep {some} brake on into the turn (about {y}% as you turn in"
        bits += f", against {_round_to(tt, 5)}%)" if tt is not None and tt >= BRAKE_ON_PCT else ")"
        if co and co["z"] >= Z_LIKELY and co["typical"] - co["best"] >= 0.1:
            bits += (f" instead of coasting ({co['typical']:.1f} s off both pedals on typical laps, "
                     f"{co['best']:.1f} s on your best)")
        how = f"hold {some} brake (about {y}%) as you turn in, and let it go as you reach the apex."
        why = "A little brake while turning keeps weight on the front tyres, so the car turns in instead of running wide."
        return f"{who} you {bits}", how, why
    if rule == "coast":
        co = D["coast_s"]
        diff = (f"{who} you spend {co['best']:.1f} s with both feet off the pedals instead of {co['typical']:.1f} s "
                f"(between letting go of the brake and starting on the throttle)")
        how = "as the brake comes off, go straight to a little throttle. Don't wait with both feet off."
        why = "With both feet off, the car is neither slowing nor speeding up: the slowest way through."
        return diff, how, why
    if rule == "release":
        z = g("release_s")
        diff = f"{who} you let the brake off over {z:.1f} s instead of {g('release_s', 'typical'):.1f} s"
        how = f"don't jump off the brake: ease it off over about {z:.1f} s as you turn in."
        why = "Letting the brake off gradually keeps weight on the front tyres while you turn."
        return diff, how, why
    if rule == "throttle":
        n = _five(D["pickup_m"]["diff"])
        b = g("pickup_m")
        where = ("at the apex" if abs(b) <= 15 else
                 f"about {_round_to(abs(b), 5)} m {'before' if b < 0 else 'after'} the apex")
        diff = (f"{who} you pick up the throttle about {n} m earlier ({where} instead of "
                f"{_round_to(abs(g('pickup_m', 'typical')), 5)} m {'before' if g('pickup_m', 'typical') < 0 else 'after'} it)")
        if where == "at the apex":
            diff = (f"{who} you pick up the throttle about {n} m earlier (at the apex instead of "
                    f"{_round_to(abs(g('pickup_m', 'typical')), 5)} m after it)")
        how = f"start feeding the throttle about {n} m earlier, {where}. Gently at first, then more as you unwind the wheel."
        why = "The sooner you're back on the power, the faster you are all the way down the next straight."
        return diff, how, why
    if rule == "carry":
        mn, en = D["min_kmh"], D["entry_kmh"]
        diff = (f"{who} you arrive at the same speed ({_round_to(en['best'], 1)} vs {_round_to(en['typical'], 1)} km/h) "
                f"but carry {_round_to(mn['diff'], 1)} km/h more at the slowest point")
        re_, pk = D.get("release_end_m"), D.get("peak_pct")
        if re_ and re_["z"] >= Z_LIKELY and re_["diff"] <= -5:
            diff += f", and let go of the brake about {_five(re_['diff'])} m earlier"
            how = "same braking marker, but come off the brake a little earlier so the car rolls faster through the middle."
        elif pk and pk["z"] >= Z_LIKELY and pk["diff"] <= -R_PEAK_PCT:
            diff += f", and brake at about {_round_to(pk['best'], 5)}% instead of {_round_to(pk['typical'], 5)}%"
            how = (f"same braking marker, but press less hard (about {_round_to(pk['best'], 5)}%) so you don't "
                   f"slow down more than the corner needs.")
        else:
            how = ("same braking marker, but brake a little less so you don't slow down more than the corner needs. "
                   "Your numbers don't show exactly where the difference comes from, so try it gently.")
        why = "Speed at the slowest point carries through the whole exit."
        return diff, how, why
    if rule == "spin":
        m = P.get("slip_method") or "wheel speeds"
        diff = (f"{who} the rear wheels spin for {int(round(100 * g('spin_share')))}% of the exit instead of "
                f"{int(round(100 * g('spin_share', 'typical')))}% (measured from {m})")
        how = "squeeze the throttle on more slowly out of the corner; if the rear starts to spin, ease back a little instead of lifting."
        why = "Spinning wheels push less than gripping ones, so a smoother pedal gets you out faster."
        return diff, how, why
    if rule == "lock":
        diff = (f"{who} a front wheel locks for {int(round(100 * g('lock_share')))}% of the braking instead of "
                f"{int(round(100 * g('lock_share', 'typical')))}% (measured from wheel speeds)")
        how = "press firmly at first, then ease off a little as the car slows; if a front locks, release slightly."
        why = "A locked tyre slides instead of gripping, so it stops you slower and won't steer."
        return diff, how, why
    return "", "", ""


def _gets(D, lost, label=None):
    mn, ex = D.get("min_kmh"), D.get("exit_kmh")
    bits = []
    if mn:
        dv = mn["best"] - mn["typical"]
        if dv >= 1:
            bits.append(f"about {_round_to(dv, 1)} km/h more through the middle "
                        f"({_round_to(mn['best'], 1)} vs {_round_to(mn['typical'], 1)} km/h)")
        elif dv <= -1:
            bits.append(f"about {_round_to(-dv, 1)} km/h less at the slowest point "
                        f"({_round_to(mn['best'], 1)} vs {_round_to(mn['typical'], 1)} km/h), but quicker overall")
        else:
            bits.append(f"about the same speed through the middle ({_round_to(mn['best'], 1)} km/h)")
    if ex:
        dv = ex["best"] - ex["typical"]
        if abs(dv) >= 1:
            bits.append(f"{_round_to(abs(dv), 1)} km/h {'more' if dv > 0 else 'less'} on the way out "
                        f"({_round_to(ex['best'], 1)} vs {_round_to(ex['typical'], 1)} km/h)")
    bits.append(f"about {lost:.2f} s a lap")
    return ", ".join(bits[:-1]) + (" and " if len(bits) > 1 else "") + bits[-1]


def _confidence(z, n_total, ref=False):
    if ref:
        return "thin"
    if n_total < THIN_LAPS:
        return "thin"
    if z >= Z_CLEAR and n_total >= CLEAR_LAPS:
        return "clear"
    return "likely"


def _voice(name, rules):
    if not rules:
        return None
    one = VOICE_ALONE.get(rules[0], VOICE[rules[0]]) if len(rules) == 1 else VOICE[rules[0]]
    if len(rules) > 1:
        two = f"{VOICE[rules[0]]}, {VOICE[rules[1]]}"
        if _words(two) <= 5:
            return f"{name}: {two}"
    return f"{name}: {one}"


METRICS = ("brake_m", "peak_pct", "full_s", "abs_share", "release_s", "release_end_m", "trail_pct", "coast_s",
           "pickup_m", "full_thr_s", "min_kmh", "entry_kmh", "exit_kmh", "spin_share", "lock_share", "t")


def _card(c, name, lost, D, fired, rows_all, nb, nt, P, label=None, ref=False):
    strength = {r: (_rank_strength(rows_all, k) if rows_all else 0.0, D[k]["z"]) for r, k in fired}
    order = sorted(fired, key=lambda rk: (strength[rk[0]][0], strength[rk[0]][1]), reverse=True)
    primary = order[0]
    second = next((rk for rk in order[1:] if FAMILY[rk[0]] != FAMILY[primary[0]]), None)
    used = [primary] + ([second] if second else [])
    parts = [_phrase(r, D, P, label) for r, _k in used]
    diff = parts[0][0] + (", and " + parts[1][0].split(" you ", 1)[-1] if len(parts) > 1 else "")
    how = parts[0][1][0].upper() + parts[0][1][1:]
    if len(parts) > 1:
        how += " " + parts[1][1][0].upper() + parts[1][1][1:]
    gets = _gets(D, lost, label)
    z = D[primary[1]]["z"]
    conf = _confidence(z, nb + nt, ref)
    if ref:
        evidence = f"{label} (1 lap) vs your {nt} typical laps."
        head = f"{name}: against {label}, your typical lap loses about {lost:.2f} s here."
    else:
        evidence = f"{nb} best laps vs {nt} typical laps."
        head = f"{name}: you lose about {lost:.2f} s here on a typical lap."
    text = (f"{head} What's different: {diff}. How to try it: {how} What it gets you: {gets}. Evidence: {evidence}")
    voice = _voice(name, [r for r, _k in used])
    nums = {k: {"best": _r(D[k]["best"], 3), "typical": _r(D[k]["typical"], 3), "z": _r(D[k]["z"], 2)}
            for k in METRICS if D.get(k)}
    return {"corner": name, "id": c["id"], "apex_m": _r(c["apex_m"], 1), "lost_s": _r(lost, 3),
            "rule": primary[0], "secondary": second[0] if second else None,
            "different": diff, "how": how, "gets": gets, "why": parts[0][2], "evidence": evidence,
            "best_laps": nb, "typical_laps": nt,
            "confidence": conf, "confidence_rule": CONFIDENCE_RULE if not ref else
            "a single reference lap: its own lap-to-lap noise can't be measured, so this is always thin",
            "voice": voice, "numbers": nums, "text": text, "reference": label if ref else None}


def compare(laps, corners, flags=None, prefix: str = "T") -> dict:
    """Per corner: best vs typical through that corner, the rules that fire, and a card.
    Returns {"cards": [...biggest loss first], "unclear": [...], "fine": [...], "per_corner": {id: {...}}}."""
    flags = flags or {}
    out = {"cards": [], "unclear": [], "fine": [], "per_corner": {}}
    for k, c in enumerate(corners):
        name = f"{prefix}{k + 1}"
        rows = []
        for x in laps:
            p = phases(x["R"], c)
            if p is not None:
                p["_lap"] = (x.get("file"), x.get("lap"))
                rows.append(p)
        pc = {"id": c["id"], "name": name, "laps": len(rows)}
        out["per_corner"][c["id"]] = pc
        if len(rows) < MIN_LAPS:
            pc["status"] = "too few laps"
            continue
        best, typ = split(rows)
        if len(typ) < MIN_GROUP:
            pc["status"] = "too few laps"
            continue
        lost = float(np.median([r["t"] for r in typ]) - np.median([r["t"] for r in best]))
        pc.update({"lost_s": _r(lost, 3), "best_laps": len(best), "typical_laps": len(typ)})
        D = {m: diff_test([r.get(m) for r in best], [r.get(m) for r in typ], m) for m in METRICS}
        pc["numbers"] = {m: {"best": _r(D[m]["best"], 3), "typical": _r(D[m]["typical"], 3), "z": _r(D[m]["z"], 2)}
                         for m in METRICS if D.get(m)}
        if lost < MIN_LOST_S:
            pc["status"] = "fine"
            out["fine"].append({"corner": name, "id": c["id"], "lost_s": _r(lost, 3)})
            continue
        fired = _rules(D, flags)
        if not fired:
            pc["status"] = "no clear pattern"
            out["unclear"].append({"corner": name, "id": c["id"], "lost_s": _r(lost, 3),
                                   "best_laps": len(best), "typical_laps": len(typ),
                                   "text": (f"{name}: you lose about {lost:.2f} s here on a typical lap, but there's "
                                            f"no clear pattern yet in what you do differently on your best laps "
                                            f"({len(best)} best vs {len(typ)} typical laps). It may just be "
                                            f"lap-to-lap scatter.")})
            continue
        card = _card(c, name, lost, D, fired, rows, len(best), len(typ), flags)
        pc["status"] = "card"
        out["cards"].append(card)
    out["cards"].sort(key=lambda x: -(x["lost_s"] or 0))
    out["unclear"].sort(key=lambda x: -(x["lost_s"] or 0))
    return out


def compare_reference(laps, ref_lap, corners, flags=None, prefix: str = "T") -> list:
    """Cards against ONE reference lap (e.g. a teammate's), clearly labelled and always 'thin'. Your typical laps =
    all your clean laps. Rules use their thresholds only (one lap has no measurable noise)."""
    label = str(ref_lap.get("label") or "the reference lap")
    out = []
    for k, c in enumerate(corners):
        name = f"{prefix}{k + 1}"
        rows = [p for p in (phases(x["R"], c) for x in laps) if p is not None]
        rp = phases(ref_lap["R"], c)
        if rp is None or len(rows) < MIN_GROUP:
            continue
        lost = float(np.median([r["t"] for r in rows]) - rp["t"])
        if lost < MIN_LOST_S:
            continue
        D = {}
        for m in METRICS:
            tv = [r.get(m) for r in rows if _num(r.get(m)) is not None]
            if _num(rp.get(m)) is None or len(tv) < MIN_GROUP:
                continue
            mt = float(np.median(tv))
            D[m] = {"best": float(rp[m]), "typical": mt, "diff": float(rp[m]) - mt, "z": 99.0, "se": 0.0,
                    "n_best": 1, "n_typical": len(tv)}
        f2 = dict(flags or {})
        f2["abs"] = f2.get("abs") and ref_lap["R"].get("_abs")
        fired = _rules(D, f2)
        if fired:
            out.append(_card(c, name, lost, D, fired, None, 1, len(rows), f2, label=label, ref=True))
    out.sort(key=lambda x: -(x["lost_s"] or 0))
    return out


# ---------------------------------------------------------------- session-level braking habits
ABS_ONSET_SHARE = 0.5       # the pedal level where the ABS is working at least half the time = "where it steps in"
PARAMS["ABS_ONSET_SHARE"] = ABS_ONSET_SHARE


def abs_onset_pct(laps) -> float | None:
    """The pedal % (5 % steps) from which the ABS is working more than ABS_ONSET_SHARE of the time (pedal readings in
    a 10-point window from there), from every lap's braking above 30 km/h. None without the ABS channel or when it never steps in."""
    bs, ab = [], []
    for x in laps or []:
        R = x.get("R") or {}
        if not R.get("_abs"):
            return None
        m = (R["b"] > BRAKE_ON_PCT) & (R["v"] / engine.MPH * KMH > COAST_MIN_KMH)
        bs.append(R["b"][m])
        ab.append(R["abs"][m] > 0.5)
    if not bs:
        return None
    b, a = np.concatenate(bs), np.concatenate(ab)
    for lo in range(5, 100, 5):                      # a 10-point window, so a few readings per level are enough
        m = (b >= lo) & (b < lo + 10)
        if m.sum() >= 8 and a[m].mean() > ABS_ONSET_SHARE:
            return float(lo)
    return None


def braking_summary(laps, corners) -> dict:
    """ABS reliance, lock-ups, trail braking, coasting and full-pedal habit over every lap and corner, in plain words."""
    zones, abs_heavy, full, trail, lock_ev, lock_m = 0, 0, 0, [], 0, None
    coast_per_lap = []
    abs_known = False
    for x in laps:
        cl = 0.0
        for c in corners:
            p = phases(x["R"], c)
            if p is None:
                continue
            cl += p.get("coast_s") or 0.0
            if p.get("brake_m") is None:
                continue
            zones += 1
            if p.get("abs_share") is not None:
                abs_known = True
                abs_heavy += p["abs_share"] > ABS_ZONE_SHARE
            full += (p.get("peak_pct") or 0) >= FULL_BRAKE_PCT
            if p.get("trail_pct") is not None:
                trail.append(p["trail_pct"])
            if p.get("lock_method"):
                lock_m = p["lock_method"]
            lock_ev += int(p.get("lock_events") or 0)
        coast_per_lap.append(cl)
    out = {"laps": len(laps), "braking_zones": zones, "abs_method": "BrakeABSactive" if abs_known else None,
           "lock_method": lock_m, "lines": []}
    if not laps or not zones:
        out["lines"].append("Not enough clean laps with braking to say anything about your braking yet.")
        return out
    out["abs_share"] = round(abs_heavy / zones, 3) if abs_known else None
    out["abs_onset_pct"] = abs_onset_pct(laps) if abs_known else None
    out["full_share"] = round(full / zones, 3)
    out["trail_share"] = round(sum(t >= BRAKE_ON_PCT for t in trail) / len(trail), 3) if trail else None
    out["trail_median_pct"] = _r(_med([t for t in trail if t >= BRAKE_ON_PCT]), 1) if trail else None
    out["coast_s_per_lap"] = _r(_med(coast_per_lap), 2)
    out["lockups"] = lock_ev if lock_m == "wheel speeds" else None
    out["lockups_per_lap"] = _r(lock_ev / len(laps), 2) if lock_m == "wheel speeds" else None
    L = out["lines"]
    L.append(f"You press the brake all the way (95%+) in {int(round(100 * out['full_share']))}% of braking zones.")
    if abs_known:
        s = out["abs_share"]
        L.append(f"The ABS does a lot of the work (active for over {int(ABS_ZONE_SHARE * 100)}% of the stop) in "
                 f"{int(round(100 * s))}% of your braking zones" +
                 (": you lean on it heavily. Pressing a little less usually stops you just as fast." if s >= 0.5 else
                  ": a fair amount." if s >= 0.25 else ". You mostly brake below the point where it steps in."))
        if out["abs_onset_pct"] is not None:
            L.append(f"On this car the ABS starts working from about {out['abs_onset_pct']:.0f}% pedal.")
    else:
        L.append("This telemetry has no ABS channel, so ABS use can't be measured.")
    if lock_m == "wheel speeds":
        L.append(f"Front wheel lock-ups (from wheel speeds): {lock_ev} in {len(laps)} laps"
                 + (", none worth worrying about." if lock_ev == 0 else "."))
    elif lock_m == "abs":
        L.append("No wheel-speed channels, so lock-ups can't be counted directly; ABS activity above is the guide.")
    if trail:
        ts = out["trail_share"]
        L.append(f"You still have some brake on as you turn in at {int(round(100 * ts))}% of corners"
                 + (f" (about {out['trail_median_pct']:.0f}% pedal)" if out["trail_median_pct"] else "")
                 + (": you trail brake as a habit." if ts >= 0.6 else
                    ": sometimes." if ts >= 0.3 else ". You mostly finish braking before turning."))
    else:
        L.append("No steering channel, so trail braking can't be measured.")
    L.append(f"Coasting (both feet off between braking and throttle): about {out['coast_s_per_lap']:.1f} s per lap.")
    return out


# ---------------------------------------------------------------- whole analysis
def analyze_laps(laps, reference=None, info=None, prefix: str = "T") -> dict:
    """Corner coach report for clean laps of one car and track (load_laps). Never raises.

    {"ok": bool, "message": str, "version", "laps": n, "laps_used": [{"file","lap","time"}...], "off_pace": [...],
     "corners": [{"id","name","apex_m","start_m","end_m"}], "cards": [card...], "unclear": [...], "fine": [...],
     "reference_cards": [card...], "reference": label|None, "summary": braking_summary(), "method": {...},
     "info": load_laps info}
    card: {"corner","id","apex_m","lost_s","rule","secondary","different","how","gets","why","evidence",
           "best_laps","typical_laps","confidence","confidence_rule","voice","numbers","text","reference"}"""
    info = dict(info or {})
    base = {"ok": False, "version": VERSION, "laps": 0, "laps_used": [], "off_pace": [], "corners": [], "cards": [],
            "unclear": [], "fine": [], "reference_cards": [], "reference": None, "summary": None, "info": info,
            "method": {}, "params": PARAMS}
    try:
        laps = [x for x in (laps or []) if isinstance(x, dict) and isinstance(x.get("R"), dict)
                and _num(x.get("time"))]
        if laps:
            med = float(np.median([x["time"] for x in laps]))
            base["off_pace"] = [{"file": x.get("file"), "lap": x.get("lap"), "time": _r(x["time"], 3)}
                                for x in laps if x["time"] > med * OFFPACE_FRAC]
            laps = [x for x in laps if x["time"] <= med * OFFPACE_FRAC]
        base["laps"] = len(laps)
        base["laps_used"] = [{"file": x.get("file"), "lap": x.get("lap"), "time": _r(x["time"], 3),
                              "session": x.get("session")} for x in laps]
        if len(laps) < MIN_LAPS:
            have = f"Only {len(laps)} clean lap{'s' if len(laps) != 1 else ''}" if laps else "No clean laps"
            base["message"] = (f"{have}. The corner coach needs at least {MIN_LAPS} to compare your best laps "
                               f"with your typical ones.")
            return base
        fastest = min(laps, key=lambda x: x["time"])
        R = fastest["R"]
        L = float(R["d"][-1] + STEP)
        corners = engine.detect_corners(R, L=L)
        base["corners"] = [{"id": c["id"], "name": f"{prefix}{k + 1}", "apex_m": _r(c["apex_m"], 1),
                            "start_m": _r(c["start_m"], 1), "end_m": _r(c["end_m"], 1)} for k, c in enumerate(corners)]
        if not corners:
            base["message"] = "No corners found on your fastest lap (a flat-out track?)."
            return base
        flags = {"abs": all(x["R"].get("_abs") for x in laps),
                 "slip_method": ("wheel speeds" if all(x["R"].get("_wheels") for x in laps) else
                                 "rpm" if all((x["R"].get("_base") or {}).get("rpm") for x in laps) else None)}
        flags["abs_onset_pct"] = abs_onset_pct(laps) if flags["abs"] else None
        res = compare(laps, corners, flags, prefix)
        base.update({k: res[k] for k in ("cards", "unclear", "fine")})
        base["per_corner"] = res["per_corner"]
        base["summary"] = braking_summary(laps, corners)
        base["method"] = {"corners_from": f"your fastest lap ({fastest.get('file')}, lap {fastest.get('lap')}, "
                                          f"{fastest['time']:.3f} s)",
                          "best": "fastest third of your laps through each corner (at least 3)",
                          "typical": "the rest (at least 3)", "abs": "BrakeABSactive" if flags["abs"] else None,
                          "wheelspin": flags["slip_method"],
                          "lockup": "wheel speeds" if flags["slip_method"] == "wheel speeds" else
                          ("abs" if flags["abs"] else None),
                          "abs_onset_pct": flags["abs_onset_pct"], "confidence": CONFIDENCE_RULE}
        if reference is not None and isinstance(reference.get("R") if isinstance(reference, dict) else None, dict):
            base["reference"] = str(reference.get("label") or "the reference lap")
            base["reference_cards"] = compare_reference(laps, reference, corners, flags, prefix)
        base["ok"] = True
        n = len(base["cards"])
        base["message"] = (f"{n} corner{'s' if n != 1 else ''} with a clear thing to try, from {len(laps)} clean laps."
                           if n else f"No corner shows a clear pattern yet from {len(laps)} clean laps.")
        return base
    except Exception as e:  # noqa: BLE001
        base["ok"] = False
        base["message"] = f"The corner coach could not read these laps ({str(e)[:160]})."
        return base


def analyze_files(paths, reference=None, prefix: str = "T") -> dict:
    """analyze_laps over the clean laps of one or more .ibt files (one car and track). Never raises."""
    try:
        laps, info = load_laps(paths)
    except Exception as e:  # noqa: BLE001
        laps, info = [], {"error": str(e)[:160]}
    out = analyze_laps(laps, reference=reference, info=info, prefix=prefix)
    return out


def voice_phrases(report: dict, top_n: int = 3) -> dict:
    """{"C4": "T4: lighter brake, hold it in", ...} for the live coach: the biggest-loss cards first."""
    out = {}
    for c in (report or {}).get("cards") or []:
        if c.get("voice") and len(out) < top_n:
            out[c["id"]] = c["voice"]
    return out
