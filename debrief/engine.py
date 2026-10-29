"""Debrief analysis engine: turns Garage 61 laps + telemetry into a review.

Works for any car and track. Corners are detected from your fastest lap's
speed trace, so nothing is hard-coded to one circuit.
"""
from __future__ import annotations

import io
import statistics as st

import numpy as np

from . import analysis_plus

MPH = 2.236936
STEP = 5.0  # metres per grid cell


def g(d: dict, *names, default=None):
    """Read a field that may be snake_case or camelCase."""
    for n in names:
        if d is None:
            return default
        if n in d and d[n] is not None:
            return d[n]
    return default


# ---------------------------------------------------------------- telemetry
PEDALS = ("Brake", "Throttle")


def parse_csv(text: str) -> dict:
    """One lap of Garage 61 telemetry -> arrays.

    Blank, garbled, NaN or infinite Speed / LapDistPct cells drop that sample (a 0.0 there would look
    like a start/finish crossing or a stop). Other channels: a bad cell is filled from its neighbours.
    A channel the export doesn't have is zero-filled and listed in raw["_missing"], so the analysis
    can leave out the numbers that depend on it instead of reporting zeros.
    Raises ValueError for anything that isn't usable telemetry (no header, no rows, no Speed column)."""
    import csv
    text = (text or "").lstrip("\ufeff").replace("\u00ef\u00bb\u00bf", "")
    reader = list(csv.reader(io.StringIO(text.strip())))
    if not reader or not reader[0]:
        raise ValueError("telemetry CSV is empty")
    head = [h.strip().lstrip("\ufeff") for h in reader[0]]
    cols = {h: i for i, h in enumerate(head)}
    need = ["Speed", "LapDistPct"]
    for n in need:
        if n not in cols:
            raise ValueError(f"telemetry CSV has no {n} column (columns: {', '.join(head[:8])})")
    rows = [r for r in reader[1:] if r and any(c.strip() for c in r)]
    if not rows:
        raise ValueError("telemetry CSV has a header but no samples")
    missing = []
    width = len(head)
    if any(len(r) != width for r in rows):   # short or long rows: pad/trim so columns line up
        rows = [(r + [""] * width)[:width] for r in rows]
    columns = list(zip(*rows))   # one pass to transpose, instead of one pass per column

    def col(name, conv=float, default=0.0):
        if name not in cols:
            missing.append(name)
            return np.full(len(rows), default, dtype=float)
        i = cols[name]
        cells = columns[i] if i < len(columns) else [""] * len(rows)
        if conv is bool:
            return np.array([1.0 if v.strip().lower() == "true" else 0.0 for v in cells])
        try:  # fast path: numpy parses the whole column at once
            out = np.array(cells, dtype=float)
        except ValueError:
            out = np.empty(len(rows))
            for k, v in enumerate(cells):
                try:
                    out[k] = float(v)
                except ValueError:
                    out[k] = np.nan
        out[~np.isfinite(out)] = np.nan
        return out

    raw = {
        "v": col("Speed"), "p": col("LapDistPct"), "lat": col("Lat", default=np.nan), "lon": col("Lon", default=np.nan),
        "b": col("Brake"), "th": col("Throttle"), "gear": col("Gear"), "abs": col("ABSActive", bool),
        "la": col("LatAccel"), "lo": col("LongAccel"), "st": col("SteeringWheelAngle"), "rpm": col("RPM"),
    }
    ok = np.isfinite(raw["v"]) & np.isfinite(raw["p"])
    if ok.sum() < 10:
        raise ValueError("telemetry CSV has fewer than 10 readable samples")
    for k in raw:
        raw[k] = raw[k][ok]
    idx = np.arange(len(raw["v"]))
    for k in ("b", "th", "gear", "la", "lo", "st", "rpm"):
        bad = ~np.isfinite(raw[k])
        if bad.all():
            raw[k] = np.zeros(len(idx))
            missing.append({"b": "Brake", "th": "Throttle", "gear": "Gear"}.get(k, k))
        elif bad.any():
            raw[k][bad] = np.interp(idx[bad], idx[~bad], raw[k][~bad])
    raw["_missing"] = sorted(set(missing))
    return raw


def pedals_missing(raw_or_R) -> bool:
    """True when the lap has no Brake or no Throttle channel (coasting/brake numbers would be fiction)."""
    m = (raw_or_R or {}).get("_missing") or []
    return any(p in m for p in PEDALS)


def coverage(raw: dict) -> float:
    """Share of the lap (0..1) the LapDistPct trace covers, after unwrapping the start/finish line."""
    p = _unwrap(raw["p"])
    return float(np.nanmax(p) - np.nanmin(p)) if len(p) else 0.0


def _unwrap(p):
    """LapDistPct (0..1, wrapping at the line) -> laps travelled, continuous across start/finish.
    Only a drop from near 1 to near 0 counts as crossing the line, so a glitch mid-lap doesn't add a lap."""
    orig = np.asarray(p, float)
    p = orig.copy()
    if not len(p):
        return p
    if p[0] > 0.9:
        k = int(np.argmax(orig < 0.5))
        if k > 0:
            p[:k] -= 1.0
    jumps = np.where((np.diff(orig) < -0.5) & (orig[:-1] > 0.8) & (orig[1:] < 0.2))[0]
    for j in jumps:
        if p[j] > 0.5:   # (the leading wrap handled above was already shifted down)
            p[j + 1:] += 1.0
    return p


def lap_length_from(raw: dict, lap_time: float) -> float:
    n = len(raw["v"])
    dt = lap_time / max(1, n - 1)
    return float(np.sum(raw["v"]) * dt)


def resample(raw: dict, lap_time: float, L: float) -> dict:
    """Put one lap on a common distance grid (STEP metres)."""
    n = len(raw["v"])
    t = np.linspace(0, lap_time, n)
    p = _unwrap(raw["p"])
    d = p * L
    d = np.maximum.accumulate(d + np.arange(n) * 1e-6)
    grid = np.arange(0, L, STEP)
    out = {"d": grid}
    for k in ("v", "b", "th", "la", "lo", "st", "rpm"):
        out[k] = np.interp(grid, d, raw[k])
    for k in ("lat", "lon"):
        ok = np.isfinite(raw[k]) & (np.abs(raw[k]) > 1e-9)
        out[k] = np.interp(grid, d[ok], raw[k][ok]) if ok.sum() > 10 else np.zeros_like(grid)
    tt = np.interp(grid, d, t)
    tt -= tt[0]
    end = np.interp(L, d, t) - np.interp(0, d, t)
    if end > 0:
        tt *= lap_time / end
    out["tt"] = tt
    idx = np.clip(np.searchsorted(d, grid), 0, n - 1)
    out["gear"] = raw["gear"][idx]
    out["abs"] = raw["abs"][idx]
    out["v"] = out["v"] * MPH
    out["b"] = np.clip(out["b"] * 100, 0, 100)
    out["th"] = np.clip(out["th"] * 100, 0, 100)
    out["la"] = out["la"] / 9.81
    out["lo"] = out["lo"] / 9.81
    out["st"] = out["st"] * 57.2958
    out["T"] = float(lap_time)
    out["_missing"] = list(raw.get("_missing") or [])
    return out


def tt_at(R, d):
    return float(np.interp(d, R["d"], R["tt"]))


# ---------------------------------------------------------------- corners
def smooth(x, k=5):
    if len(x) < k:
        return x
    w = np.ones(k) / k
    return np.convolve(np.pad(x, (k // 2, k // 2), mode="edge"), w, mode="valid")


def detect_corners(R: dict, min_drop_mph=6.0, window_m=150, merge_m=120):
    """Corners from one lap's speed trace. The lap is treated as a loop: the first corner's zone may
    start before the line (start_m < 0) and the last one's may end after it (end_m > L), so a braking
    zone that crosses start/finish isn't cut off."""
    v = smooth(R["v"], 7)
    d = R["d"]
    n = len(v)
    w = int(window_m / STEP)
    look = int(400 / STEP)
    vv = np.concatenate([v, v, v])   # circular view for the look-back/ahead windows
    mins = []
    for i in range(n):
        j = i + n
        if v[i] > vv[j - w:j + w + 1].min() + 1e-9:
            continue
        left = vv[j - look:j + 1].max()
        right = vv[j:j + look].max()
        if min(left, right) - v[i] >= min_drop_mph:
            if mins and d[i] - d[mins[-1]] < merge_m:
                if v[i] < v[mins[-1]]:
                    mins[-1] = i
                continue
            mins.append(i)
    if len(mins) >= 2 and (d[mins[0]] + (n * STEP) - d[mins[-1]]) < merge_m:   # same corner either side of the line
        if v[mins[0]] <= v[mins[-1]]:
            mins.pop()
        else:
            mins.pop(0)
    if not mins:
        return []
    L = float(n * STEP)
    # zone boundaries at the speed peak between neighbouring corners (going round the loop)
    bounds = []
    for a, b in zip(mins, mins[1:]):
        bounds.append(float(d[a + int(np.argmax(v[a:b + 1]))]))
    seg = np.concatenate([v[mins[-1]:], v[:mins[0] + 1]])
    k = int(np.argmax(seg))
    wrap = float(d[mins[-1] + k]) if mins[-1] + k < n else float(d[mins[-1] + k - n]) + L   # peak across the line
    lo = [wrap - L] + bounds
    hi = bounds + [wrap]
    corners = []
    for k, i in enumerate(mins):
        start = max(lo[k], float(d[i]) - 350.0)
        end = min(hi[k], float(d[i]) + 250.0)
        corners.append({"id": f"C{k + 1}", "apex_m": float(d[i]), "start_m": start, "end_m": end})
    return corners


BRAKE_ON_PCT = 5.0   # pedal above this counts as braking
BRAKE_GAP_M = 30.0   # a release shorter than this inside one braking zone (trail-brake modulation) is ignored


def brake_onset(b, pre):
    """Index where the main braking zone into this corner starts.

    Braking cells (pedal > BRAKE_ON_PCT) are grouped into stops, bridging short releases
    (< BRAKE_GAP_M) so trail-brake modulation stays one stop. The stop with the most braking
    (sum of pedal %) is the main one, so a small tap after a big stop (a chicane) or a quick
    re-application while trail braking isn't mistaken for the brake point.
    Returns None if the pedal never goes above BRAKE_ON_PCT here (a lift-only corner).
    """
    on = [k for k in range(len(pre)) if b[pre[k]] > BRAKE_ON_PCT]
    if not on:
        return None
    gap = max(1, int(round(BRAKE_GAP_M / STEP)))
    stops, cur = [], [on[0]]
    for k in on[1:]:
        if k - cur[-1] > gap:
            stops.append(cur)
            cur = []
        cur.append(k)
    stops.append(cur)
    main = max(stops, key=lambda ks: sum(b[pre[k]] for k in ks))
    return pre[main[0]]


def _looped(R):
    """The lap with one copy of its last/first 600 m added before/after it (distance and time shifted by
    one lap), so corner windows can run across the start/finish line. Cached on R."""
    if "_loop" in R:
        return R["_loop"]
    d, tt = R["d"], R["tt"]
    L = float(d[-1] + STEP)
    T = float(R.get("T") or (tt[-1] + (tt[-1] - tt[-2] if len(tt) > 1 else 0)))
    k = min(len(d), int(600 / STEP))
    out = {}
    for key in ("v", "b", "th", "gear", "abs"):
        out[key] = np.concatenate([R[key][-k:], R[key], R[key][:k]])
    out["d"] = np.concatenate([d[-k:] - L, d, d[:k] + L])
    out["tt"] = np.concatenate([tt[-k:] - T, tt, tt[:k] + T])
    R["_loop"] = out
    return out


def corner_metrics(R, c):
    X = _looped(R)
    d = X["d"]
    a, b, apex = c["start_m"], c["end_m"], c["apex_m"]
    m = (d >= a) & (d <= b)
    if m.sum() < 3:
        return None
    idx = np.where(m)[0]
    near = idx[np.abs(d[idx] - apex) <= 120]
    if len(near) == 0:
        near = idx
    i_min = near[np.argmin(X["v"][near])]
    pedals = not pedals_missing(R)
    pre = idx[d[idx] <= apex + 40]
    bo = brake_onset(X["b"], pre) if pedals else None
    post = idx[d[idx] > d[i_min]]
    ft = next((i for i in post if X["th"][i] > 95), None) if pedals else None
    cell_t = np.diff(X["tt"], append=X["tt"][-1])
    i_exit = min(len(d) - 1, i_min + int(150 / STEP))
    miss = R.get("_missing") or []
    return {
        "t": float(np.interp(b, d, X["tt"]) - np.interp(a, d, X["tt"])),
        "min_mph": float(X["v"][i_min]),
        "min_at": float(d[i_min]),
        "gear": None if "Gear" in miss else int(X["gear"][i_min]),
        "peak_brake": float(X["b"][idx].max()) if pedals else None,
        "brake_on": float(d[bo]) if bo is not None else None,
        "full_thr": float(d[ft]) if ft is not None else None,
        "abs_s": None if "ABSActive" in miss else float(cell_t[idx][X["abs"][idx] > 0.5].sum()),
        "coast_s": float(cell_t[idx][(X["th"][idx] < 5) & (X["b"][idx] < 5)].sum()) if pedals else None,
        "exit_mph": float(X["v"][i_exit]),
    }


# ---------------------------------------------------------------- stats helpers
def corr(x, y):
    """Pearson r and least-squares slope of y on x (None, None, n with fewer than 6 points or no spread)."""
    x, y = np.asarray(x, float), np.asarray(y, float)
    ok = ~(np.isnan(x) | np.isnan(y))
    x, y = x[ok], y[ok]
    if len(x) < 6 or x.std() == 0 or y.std() == 0:
        return None, None, len(x)
    r = float(np.corrcoef(x, y)[0, 1])
    slope = float(np.polyfit(x, y, 1)[0])
    return r, slope, len(x)


def _ranks(a):
    """Ranks 1..n with ties given their average rank."""
    a = np.asarray(a, float)
    order = np.argsort(a, kind="mergesort")
    r = np.empty(len(a))
    r[order] = np.arange(1, len(a) + 1)
    vals, inv, cnt = np.unique(a, return_inverse=True, return_counts=True)
    if len(vals) < len(a):
        sums = np.bincount(inv, weights=r)
        r = (sums / cnt)[inv]
    return r


def spearman(x, y):
    """Spearman rank correlation (robust to one odd lap). None with fewer than 6 points or no spread."""
    x, y = np.asarray(x, float), np.asarray(y, float)
    ok = ~(np.isnan(x) | np.isnan(y))
    x, y = x[ok], y[ok]
    if len(x) < 6 or len(np.unique(x)) < 2 or len(np.unique(y)) < 2:
        return None
    return float(np.corrcoef(_ranks(x), _ranks(y))[0, 1])


# A "what your quicker laps did" pattern has to clear all of these (H3: with 7 metrics tested per corner
# and |r| >= 0.5, pure noise produced a pattern on 65% of corners at 10 laps).
PATTERN_MIN_LAPS = 10
PATTERN_METRICS = 7          # metrics tested per corner (Bonferroni correction)
PATTERN_ALPHA = 0.05         # chance per corner that noise alone shows a pattern
PATTERN_MIN_EFFECT_S = 0.03  # quick-quarter vs slow-quarter difference in the metric must be worth this much time
_Z_CRIT = 2.69               # two-sided normal quantile for 0.05 / 7


# Exact critical |Spearman r| under the null (permutation distribution, 400k draws per n) at
# 0.05 / 7 two-sided, for 10..40 laps. Above 40 laps the Fisher-z approximation is accurate.
_R_CRIT = {10: 0.806, 11: 0.773, 12: 0.741, 13: 0.714, 14: 0.692, 15: 0.675, 16: 0.656, 17: 0.637, 18: 0.62,
           19: 0.602, 20: 0.591, 21: 0.575, 22: 0.563, 23: 0.55, 24: 0.542, 25: 0.53, 26: 0.519, 27: 0.512,
           28: 0.504, 29: 0.492, 30: 0.486, 31: 0.477, 32: 0.471, 33: 0.464, 34: 0.456, 35: 0.448, 36: 0.443,
           37: 0.438, 38: 0.431, 39: 0.427, 40: 0.421}


def pattern_r_needed(n: int) -> float:
    """|Spearman r| needed with n laps so that, across the 7 metrics tested in a corner, pure noise shows a
    pattern on fewer than 5% of corners (Bonferroni). Never below 0.5, so a pattern is always a strong one."""
    if n < PATTERN_MIN_LAPS:
        return 1.0
    crit = _R_CRIT.get(n)
    if crit is None:
        crit = float(np.tanh(_Z_CRIT * np.sqrt(1.06 / (n - 3))))
    return max(0.5, crit)


def pattern(xs, ys):
    """(rho, slope, n) when metric xs and corner time ys are linked strongly enough to coach on, else None."""
    n = len(xs)
    if n < PATTERN_MIN_LAPS:
        return None
    rho = spearman(xs, ys)
    if rho is None or abs(rho) < pattern_r_needed(n):
        return None
    _, slope, _ = corr(xs, ys)
    if slope is None or abs(slope) * (pct(xs, 75) - pct(xs, 25)) < PATTERN_MIN_EFFECT_S:
        return None
    return rho, slope, n


def pct(a, q):
    return float(np.percentile(np.asarray(a, float), q))


def lap_style(R):
    dt = np.diff(R["tt"], append=R["tt"][-1])
    T = dt.sum() or 1
    comb = np.hypot(R["la"], R["lo"])
    sr = np.abs(np.diff(R["st"], append=R["st"][-1])) / np.maximum(dt, 1e-3)
    miss = R.get("_missing") or []
    out = {
        "full_thr_pct": float(dt[R["th"] >= 98].sum() / T * 100),
        "brake_pct": float(dt[R["b"] >= 5].sum() / T * 100),
        "coast_s": float(dt[(R["th"] < 5) & (R["b"] < 5)].sum()),
        "overlap_s": float(dt[(R["th"] > 10) & (R["b"] > 10)].sum()),
        "g_over_1": float(dt[comb > 1.0].sum() / T * 100),
        "g95": float(np.percentile(comb, 95)),
        "steer_rms": float(np.sqrt(np.mean(np.clip(sr, 0, 2000) ** 2))),
        "top_mph": float(R["v"].max()),
    }
    if pedals_missing(R):   # no pedal channel: these would all read as "coasting"
        for k in ("full_thr_pct", "brake_pct", "coast_s", "overlap_s"):
            out[k] = None
    if "LatAccel" in miss or "LongAccel" in miss:
        out["g_over_1"] = out["g95"] = None
    if "SteeringWheelAngle" in miss:
        out["steer_rms"] = None
    return out


# ---------------------------------------------------------------- main
def detect_traffic(clean: list, median: float, window_m: float = 400.0) -> list:
    """Clean laps that lost their time in one short stretch of track, in a way your other clean laps
    didn't: in a multi-class race that's usually traffic (lifting for a faster class, or stuck behind
    a slower one), not your driving. Returns [{"n", "at_m", "lost"}].
    Needs at least 5 clean laps; a lap must be at least 0.3 s off the median to be considered."""
    if len(clean) < 5:
        return []
    TT = np.array([x["R"]["tt"] for x in clean], dtype=float)
    ref = np.median(TT, axis=0)
    w = max(2, int(window_m / STEP))
    if TT.shape[1] <= w + 1:
        return []
    D = TT - ref                       # time lost (+) or gained (-) vs the typical lap, along the lap
    W = D[:, w:] - D[:, :-w]           # time lost inside each window_m stretch
    med_w = np.median(W, axis=0)
    mad = np.median(np.abs(W - med_w), axis=0)
    out = []
    for i, x in enumerate(clean):
        excess = x["t"] - median
        if excess < 0.3:
            continue
        j = int(np.argmax(W[i]))
        top = float(W[i][j])
        if top >= max(0.25, 0.5 * excess) and top > med_w[j] + 4 * max(float(mad[j]), 0.03):
            out.append({"n": x["n"], "at_m": float(x["R"]["d"][j + w // 2]), "lost": round(top, 2)})
    return out


PIT_FLAGS = ("pitIn", "pitOut", "pitlane", "pit_in", "pit_out", "pit_lane")
PARTIAL_FLAGS = ("incomplete", "missing")
MIN_COVERAGE = 0.9   # a lap's telemetry must cover at least 90% of the lap


def slow_cut_for(median: float) -> float:
    """How far off the median lap a clean lap has to be to count as off-pace: about 1 s on an 85 s lap,
    scaled with lap length (0.5 s at least), so short ovals and the Nordschleife are judged alike."""
    return max(0.5, 0.012 * float(median or 0))


def _safe_float(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if np.isfinite(f) and f > 0 else None


def _flag(lap, names):
    return any(bool(g(lap, k, default=False)) for k in names)


def is_full_ref(rl: dict, raw: dict, median: float) -> str | None:
    """Why a comparison lap can't be used as one (None = it's fine): partial, pit or implausible laps."""
    lt = _safe_float(g(rl, "lap_time", "lapTime"))
    if lt is None:
        return "no lap time"
    if _flag(rl, PARTIAL_FLAGS) or _flag(rl, PIT_FLAGS):
        return "marked partial or pit lap"
    if len(raw["v"]) <= 100 or coverage(raw) < MIN_COVERAGE:
        return "telemetry covers only part of the lap"
    if median and not (0.75 * median <= lt <= 1.25 * median):
        return f"{lt:.1f} s is not a full lap next to your {median:.1f} s typical lap"
    return None


def _lap_numbers(my_laps):
    """lap id -> lap number. Garage 61's numbers when every lap has a unique one; otherwise the laps are
    numbered by start time across the whole session (laps without telemetry included, so the numbers
    still match iRacing and Garage 61)."""
    rows = [l for l in my_laps if isinstance(l, dict) and g(l, "id") is not None]
    nums = []
    for l in rows:
        v = g(l, "lap_number", "lapNumber", "lap")
        nums.append(int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) or str(v).isdigit() else None)
    if rows and None not in nums and len(set(nums)) == len(nums):
        return {g(l, "id"): n for l, n in zip(rows, nums)}
    order = sorted(range(len(rows)), key=lambda i: (rows[i].get("_start") is None, rows[i].get("_start") or 0, i))
    return {g(rows[i], "id"): k + 1 for k, i in enumerate(order)}


def analyze(meta: dict, my_laps: list, my_tel: dict, refs: list, ref_tel: dict, track_length: float | None = None, log=None) -> dict:
    """
    meta: {driver, car, track, event_name, started_at, ...}
    my_laps: Garage 61 lap objects for the race session (you)
    my_tel: lap_id -> CSV text
    refs: lap objects for comparison laps (teammates / followed drivers)
    ref_tel: lap_id -> CSV text

    Lap buckets (counts.*): partial (not a full lap: never best/PB/comparison), pit (pit in/out laps),
    excluded (more than 25% off the median: tows, spins), unclean (flagged by Garage 61, ALL full laps),
    slow (clean but more than slow_cut off the median), traffic (multi-class, lost time in one stretch).
    """
    warned = False
    numbers = _lap_numbers(my_laps or [])
    laps = []
    for l in my_laps or []:
        lt = _safe_float(g(l, "lap_time", "lapTime"))
        lid = g(l, "id")
        if not lt or lid not in my_tel:
            continue
        try:
            raw = parse_csv(my_tel[lid])
        except (ValueError, IndexError) as e:
            if log and not warned:
                log(f"  Skipping a lap whose telemetry couldn't be read: {e}")
                warned = True
            continue
        laps.append({"lap": l, "id": lid, "n": numbers.get(lid), "start": l.get("_start"), "t": float(lt),
                     "clean": bool(g(l, "clean", default=True)), "raw": raw})
    if len(laps) < 2:
        raise ValueError("not enough laps with readable telemetry in this race.")
    laps.sort(key=lambda x: x["n"])
    # partial laps (e.g. the 7 s "lap" at the finish line): Garage 61 marks them incomplete or missing;
    # anything under 75% of the median lap time, or telemetry covering only part of the lap, can't be a full lap
    n_in = len(laps)
    laps = [x for x in laps if not _flag(x["lap"], PARTIAL_FLAGS)]
    flagged = n_in - len(laps)
    if laps:
        med_all = st.median(x["t"] for x in laps)
        laps = [x for x in laps if x["t"] >= 0.75 * med_all and len(x["raw"]["v"]) > 100
                and coverage(x["raw"]) >= MIN_COVERAGE]
    partial = n_in - flagged - len(laps)
    if log and (flagged or partial):
        log(f"  Left out {flagged + partial} partial lap(s) ({flagged} marked incomplete by Garage 61, "
            f"{partial} too short or with telemetry for only part of the lap).")
    if len(laps) < 2:
        raise ValueError("not enough full laps in this race.")
    # Garage 61 flags on every full lap, before anything else is left out (a spin lap that's 60% off the
    # pace still has to show up as an incident)
    unclean_all = sorted(x["n"] for x in laps if not x["clean"])
    pit = sorted(x["n"] for x in laps if _flag(x["lap"], PIT_FLAGS))
    laps = [x for x in laps if x["n"] not in set(pit)]
    if len(laps) < 2:
        raise ValueError("not enough full laps in this race.")
    n_total = len(laps)
    # drop tow / spin laps: far slower than the median
    med_t = st.median(x["t"] for x in laps)
    excluded_laps = sorted(x["n"] for x in laps if x["t"] >= med_t * 1.25)
    laps = [x for x in laps if x["t"] < med_t * 1.25]
    excluded = n_total - len(laps)
    if len(laps) < 2:
        raise ValueError("not enough full laps in this race.")
    est = float(np.median([lap_length_from(x["raw"], x["t"]) for x in laps]))
    L = est
    if track_length and est > 0:
        for mult in (1.0, 1000.0, 1609.344):
            cand = (_safe_float(track_length) or 0) * mult
            if cand and abs(cand - est) / est < 0.10:
                L = cand
                break
    for x in laps:
        x["R"] = resample(x["raw"], x["t"], L)
    missing = sorted(set().union(*[set(x["raw"].get("_missing") or []) for x in laps]))
    if missing and log:
        log(f"  Telemetry has no {', '.join(missing)} channel(s): the numbers that need them are left out.")

    times = np.array([x["t"] for x in laps])
    nums = [x["n"] for x in laps]
    median = float(np.median(times))
    slow_cut = slow_cut_for(median)
    unclean = [x["n"] for x in laps if not x["clean"]]
    # multi-class races: a "clean" lap can still have lost time to traffic. Checked on every clean lap
    # (before the off-pace cut), so a lap that lost more than the cut to one lapping car is traffic, not a mistake.
    traffic = detect_traffic([x for x in laps if x["clean"]], median) if meta.get("multiclass") else []
    tn = {t["n"] for t in traffic}
    slow = [x["n"] for x in laps if x["clean"] and x["t"] > median + slow_cut and x["n"] not in tn]
    outlier = sorted(set(unclean) | set(slow) | tn)
    clean = [x for x in laps if x["n"] not in outlier]
    # laps that count for pace and consistency: Garage 61 clean, not traffic (off-pace laps included,
    # because they're part of how consistent you were)
    pace = [x for x in laps if x["clean"] and x["n"] not in tn]
    pool = clean or [x for x in laps if x["clean"]] or laps
    best = min(pool, key=lambda x: x["t"])
    clean_t = np.array([x["t"] for x in clean]) if clean else times

    # trend over clean laps
    if len(clean) >= 5:
        sl, ic = np.polyfit([x["n"] for x in clean], clean_t, 1)
    else:
        sl, ic = 0.0, float(np.mean(times))

    # official sectors
    sec_names, sectors = [], {}
    s0 = g(best["lap"], "sectors", default=[]) or []
    if s0 and all(g(s, "sector_time", "sectorTime") for s in s0):
        ns = len(s0)
        sec_names = [f"S{i + 1}" for i in range(ns)]
        for x in laps:
            ss = g(x["lap"], "sectors", default=[]) or []
            vals = [None if g(s, "incomplete", default=False) else _safe_float(g(s, "sector_time", "sectorTime")) for s in ss]
            sectors[x["n"]] = vals if len(vals) == ns and all(vals) else None
    # sector cut distances on best lap
    sec_cuts = []
    if sec_names:
        cum = np.cumsum(sectors.get(best["n"]) or [])
        for c in cum[:-1]:
            sec_cuts.append(float(np.interp(c, best["R"]["tt"], best["R"]["d"])))
    sec_fade = {}
    if sec_names and len(clean) >= 5:
        for i, nm in enumerate(sec_names):
            pts = [(x["n"], sectors[x["n"]][i]) for x in clean if sectors.get(x["n"])]
            if len(pts) >= 5:
                sec_fade[nm] = float(np.polyfit([p[0] for p in pts], [p[1] for p in pts], 1)[0] * 10)
    # best possible lap from sectors: the same laps as the segments' best possible lap (Garage 61 clean,
    # not traffic), so the two numbers agree on which laps count
    optimal = None
    if sec_names:
        valid = [sectors[x["n"]] for x in (pace or clean) if sectors.get(x["n"])]
        if len(valid) >= 2:
            optimal = float(sum(min(v[i] for v in valid) for i in range(len(sec_names))))
            if optimal >= best["t"] - 0.005:
                optimal = None

    # corners
    corners = detect_corners(best["R"])
    per = {c["id"]: {} for c in corners}
    for x in laps:
        for c in corners:
            m = corner_metrics(x["R"], c)
            if m:
                per[c["id"]][x["n"]] = m
    # sanity gate: a corner time under a quarter of that corner's median is broken telemetry, not driving
    dropped = []
    for cid, rows in per.items():
        if len(rows) >= 3:
            cm = float(np.median([m["t"] for m in rows.values()]))
            for n in [n for n, m in rows.items() if m["t"] < 0.25 * cm or m["t"] <= 0]:
                del rows[n]
                dropped.append((cid, n))
    if dropped and log:
        log(f"  Ignored {len(dropped)} corner reading(s) with broken telemetry.")

    # reference laps (never a partial, pit or implausible lap)
    ref_out = []
    for rl in refs or []:
        lid = g(rl, "id")
        lt = _safe_float(g(rl, "lap_time", "lapTime"))
        if lid not in ref_tel or not lt:
            continue
        try:
            raw = parse_csv(ref_tel[lid])
            why = is_full_ref(rl, raw, median)
            if why:
                if log:
                    log(f"  Left out a comparison lap ({why}).")
                continue
            RR = resample(raw, float(lt), L)
        except (ValueError, IndexError) as e:
            if log:
                log(f"  Left out a comparison lap whose telemetry couldn't be read: {e}")
            continue
        drv = g(rl, "driver", default={}) or {}
        name = g(rl, "driver_name", "driverName") or " ".join(
            filter(None, [g(drv, "firstname", "firstName"), g(drv, "lastname", "lastName")])) or "Reference"
        ref_out.append({"id": lid, "name": name, "t": float(lt), "R": RR,
                        "rating": g(rl, "driver_rating", "driverRating"),
                        "corners": {c["id"]: corner_metrics(RR, c) for c in corners},
                        "style": lap_style(RR), "role": rl.get("_role", ""), "main": bool(rl.get("_main"))})

    ref_out.sort(key=lambda r: 0 if r["main"] else 1)
    if ref_out and not any(r["main"] for r in ref_out):
        ref_out[0]["main"] = True   # the main comparison was left out: the next one takes its place

    # corner summaries + coaching
    clean_ns = {x["n"] for x in clean}
    corner_out = []
    for c in corners:
        rows = per[c["id"]]
        cl = {n: m for n, m in rows.items() if n in clean_ns}
        ts = [m["t"] for m in rows.values()]
        if len(ts) < 3:
            continue
        cts = [m["t"] for m in cl.values()] or ts
        med = float(np.median(ts))
        cmed = float(np.median(cts))
        avail = max(0.0, cmed - pct(cts, 25))
        iqr = pct(cts, 75) - pct(cts, 25)
        best_n = min(cl or rows, key=lambda n: (cl or rows)[n]["t"])   # clean laps first (fallback: all full laps)
        src = list((cl or rows).values())   # "you, typically": the same laps as technique.typical
        summ = {k: float(np.median([m[k] for m in src if m.get(k) is not None]))
                if any(m.get(k) is not None for m in src) else None
                for k in ("min_mph", "peak_brake", "brake_on", "full_thr", "abs_s", "coast_s", "exit_mph")}
        gears = [m["gear"] for m in src if m.get("gear") is not None]
        summ["gear"] = int(st.mode(gears)) if gears else None
        # what made YOUR laps faster here (rank correlation, corrected for testing 7 metrics, and big
        # enough to matter; see pattern())
        drivers = []
        for k, unit, label in (("min_mph", "mph", "minimum speed"), ("exit_mph", "mph", "exit speed"),
                               ("peak_brake", "%", "peak brake"), ("brake_on", "m", "brake point"),
                               ("full_thr", "m", "full-throttle point"), ("abs_s", "s", "time on ABS"),
                               ("coast_s", "s", "coasting")):
            xs = [cl[n][k] for n in cl if cl[n].get(k) is not None]
            ys = [cl[n]["t"] for n in cl if cl[n].get(k) is not None]
            hit = pattern(xs, ys)
            if hit:
                r, slope, n = hit
                drivers.append({"metric": k, "label": label, "unit": unit, "r": r, "slope": slope, "n": n,
                                "r_needed": round(pattern_r_needed(n), 3), "method": "spearman",
                                "p25": pct(xs, 25), "p75": pct(xs, 75), "median": float(np.median(xs))})
        drivers.sort(key=lambda z: -abs(z["r"]))
        coach = coaching_text(c, summ, drivers[:2], ref_out, iqr)
        ref_gap = None
        main = next((r for r in ref_out if r.get("main")), ref_out[0] if ref_out else None)
        if main and main["corners"].get(c["id"]):
            ref_gap = pct(cts, 25) - main["corners"][c["id"]]["t"]  # your better laps vs their lap
        corner_out.append({**c, "median_t": med, "typical_t": cmed, "best_t": rows[best_n]["t"], "best_lap": best_n,
                           "p25_t": pct(cts, 25), "avail": avail, "iqr": iqr, "ref_gap": ref_gap, "summary": summ,
                           "drivers": drivers[:3], "coaching": coach,
                           "per_lap": {str(n): {k: (round(v, 3) if isinstance(v, float) else v) for k, v in m.items()}
                                       for n, m in rows.items()},
                           "refs": {r["name"]: r["corners"].get(c["id"]) for r in ref_out}})
    ranked = sorted(corner_out, key=lambda c: -c["avail"])
    for i, c in enumerate(ranked):
        c["rank"] = i + 1

    # heatmap: corner zone time minus your best in that zone (clean laps; all laps if none), per lap
    def zone_best(cid):
        rows = per[cid]
        cl = [m["t"] for n, m in rows.items() if n in clean_ns]
        return min(cl or [m["t"] for m in rows.values()])
    zb = {c["id"]: zone_best(c["id"]) for c in corner_out}
    heat = {"laps": [x["n"] for x in laps], "zones": [c["id"] for c in corner_out],
            "values": [[round(per[c["id"]][x["n"]]["t"] - zb[c["id"]], 3)
                        if x["n"] in per[c["id"]] else None for c in corner_out] for x in laps]}

    style = {x["n"]: lap_style(x["R"]) for x in laps}
    style_trend = None
    sl_laps = [x for x in laps if x["n"] in clean_ns] or laps
    if len(sl_laps) >= 6:
        k3 = len(sl_laps) // 3
        early = [style[x["n"]] for x in sl_laps[:k3]]
        late = [style[x["n"]] for x in sl_laps[-k3:]]

        def avg(arr, k):
            vals = [a[k] for a in arr if a.get(k) is not None]
            return float(np.mean(vals)) if vals else None
        style_trend = {k: [avg(early, k), avg(late, k)] for k in style[best["n"]]}
        style_trend = {k: v for k, v in style_trend.items() if None not in v} or None

    # gg for best lap and first ref
    def gg(R):
        b = {}
        for la, lo in zip(np.round(R["la"] * 10) / 10, np.round(R["lo"] * 10) / 10):
            b[(la, lo)] = b.get((la, lo), 0) + 1
        return [{"la": float(k[0]), "lo": float(k[1]), "n": n} for k, n in b.items()]

    span = (max(nums) - min(nums)) if nums else 0
    findings = build_findings(laps, best, median, unclean_all, slow, excluded, sl, span, optimal, ranked, ref_out,
                              clean_n=len(clean), pit=pit, slow_cut=slow_cut, excluded_laps=excluded_laps)
    if traffic:
        findings.insert(1, "Multi-class race: lap(s) " + ", ".join(f"{t['n']} (~{t['lost']:.2f}s around {t['at_m']:.0f} m)" for t in traffic) +
                        " lost time in one short stretch, most likely traffic, so they're left out of your pace and consistency numbers.")
    plan = build_plan(ranked, sl, span, slow, ref_out, unclean=unclean_all)

    segments = build_segments(corner_out, laps, ref_out, L, eligible_ns=[x["n"] for x in pace])

    trace = lambda R: {"tt":[round(v, 2) for v in R["tt"][::2]], "v": [int(round(v)) for v in R["v"][::2]],
                       "b": [int(round(v)) for v in R["b"][::2]], "th": [int(round(v)) for v in R["th"][::2]],
                       "g": [int(v) for v in R["gear"][::2]]}
    data = {
        "meta": meta, "L": L, "step": STEP * 2,
        "laps": [{"n": x["n"], "t": x["t"], "clean": x["n"] not in outlier,
                  "sectors": sectors.get(x["n"])} for x in laps],
        # clean=False: Garage 61 flagged every lap, so this "best" is for display only (never a PB/goal)
        "best": {"n": best["n"], "t": best["t"], "clean": bool(best["clean"])}, "median": median,
        "std_clean": float(np.std(clean_t, ddof=1)) if clean and len(clean_t) > 2 else None,
        "slow_cut": round(slow_cut, 2),
        "optimal": optimal, "sector_names": sec_names, "sector_cuts": sec_cuts, "sector_fade": sec_fade,
        "trend_per10": float(sl * 10), "trend_total": float(sl * span), "trend_line": [float(ic + sl * n) for n in nums], "outliers": outlier,
        "corners": corner_out, "heat": heat,
        "style": {str(k): v for k, v in style.items()}, "style_trend": style_trend,
        "channels_missing": missing,
        "gg_best": gg(best["R"]), "gg_ref": gg(ref_out[0]["R"]) if ref_out else None,
        "map": ([[round(float(a), 6), round(float(b), 6)] for a, b in zip(best["R"]["lat"][::2], best["R"]["lon"][::2])]
                if np.any(np.abs(best["R"]["lat"]) > 1e-6) else []),
        "traces": {**{f"me{x['n']}": {**trace(x["R"]), "t": x["t"], "label": f"Your lap {x['n']}"} for x in laps},
                   **{f"ref{i}": {**trace(r["R"]), "t": r["t"], "label": r["name"], "role": r["role"]}
                      for i, r in enumerate(ref_out)}},
        "refs": [{"key": f"ref{i}", "name": r["name"], "t": r["t"], "rating": r["rating"], "role": r["role"],
                  "main": r["main"], "style": r["style"]} for i, r in enumerate(ref_out)],
        "counts": {"analysed": len(laps), "clean": len(clean), "unclean": unclean_all, "slow": slow, "excluded": excluded,
                   "excluded_laps": excluded_laps, "pit": pit,
                   "traffic": [t["n"] for t in traffic], "traffic_detail": traffic, "multiclass": bool(meta.get("multiclass")),
                   "partial": flagged + partial},
        "findings": findings, "plan": plan,
        "segments": segments,
    }
    # v2 extras (stint, technique, consistency, best vs worst, race context); guarded inside
    try:
        analysis_plus.extend(data, laps=laps, clean=clean, best=best, corners=corner_out, per=per,
                             refs=ref_out, raw_laps=my_laps, log=log, pace=pace, slow_cut=slow_cut)
    except Exception as e:  # belt and braces: extras must never cost the user the review
        if log:
            log(f"  Skipped the extra analysis ({type(e).__name__}: {e}).")
    return data


def build_segments(corner_out, laps, ref_out, L, eligible_ns=None):
    """Split the lap into one contiguous segment per corner so segment times add up to the lap time.

    Additive output (schema key `segments`):
      names[]            corner ids, one per segment
      cuts[]             segment boundaries in metres, 0 .. L (len = names + 1)
      laps{n: [t...]}    your segment times per lap
      refs{refK: [t...]} comparison laps' segment times
      eligible[]         lap numbers Garage 61 marked clean, minus traffic laps (eligible_ns when given),
                         used for the best possible lap
      best[{t, lap}]     fastest segment among eligible laps and which lap it came from
      possible           sum of best segments (None if fewer than 2 eligible laps)
    """
    if not corner_out:
        return None
    cs = sorted(corner_out, key=lambda c: c["apex_m"])
    cuts = [0.0]
    for a, b in zip(cs, cs[1:]):
        cuts.append(float(min(max((a["end_m"] + b["start_m"]) / 2, 0.0), L)))
    cuts.append(float(L))

    def split(R):
        tt = [tt_at(R, d) for d in cuts]
        tt[-1] = float(R["tt"][-1]) + (float(R["tt"][-1]) - float(R["tt"][-2])) * (L - float(R["d"][-1])) / STEP
        return [round(tt[i + 1] - tt[i], 3) for i in range(len(cs))]

    per = {}
    for x in laps:
        s = split(x["R"])
        # scale so the segments add up to the official lap time
        tot = sum(s)
        if tot > 0:
            s = [round(v * x["t"] / tot, 3) for v in s]
        per[str(x["n"])] = s
    # a segment under a quarter of its median is broken telemetry: never let it into the best possible lap
    med_seg = [float(np.median([per[k][j] for k in per])) for j in range(len(cs))]
    broken = {k for k, s in per.items() if any(s[j] < 0.25 * med_seg[j] for j in range(len(cs)))}
    refs = {}
    for i, r in enumerate(ref_out):
        s = split(r["R"])
        tot = sum(s)
        refs[f"ref{i}"] = [round(v * r["t"] / tot, 3) for v in s] if tot > 0 else s
    if eligible_ns is not None:
        eligible = [n for n in eligible_ns if str(n) in per]
    else:
        eligible = [x["n"] for x in laps if x["clean"]]
    eligible = [n for n in eligible if str(n) not in broken] or [x["n"] for x in laps if str(x["n"]) not in broken] \
        or [x["n"] for x in laps]
    best, possible = [], None
    for j in range(len(cs)):
        n = min(eligible, key=lambda n: per[str(n)][j])
        best.append({"t": per[str(n)][j], "lap": n})
    if len(eligible) >= 2:
        possible = round(sum(b["t"] for b in best), 3)
    return {"names": [c["id"] for c in cs], "cuts": cuts, "laps": per, "refs": refs,
            "eligible": eligible, "best": best, "possible": possible}


# ---------------------------------------------------------------- words
def fmt_t(s):
    m = int(s // 60)
    return f"{m}:{s - m * 60:06.3f}"


def coaching_text(c, summ, drivers, refs, iqr=None):
    parts = []
    apex = float(c.get("apex_m") or 0.0)   # brake points are given as metres before the apex (a corner just
    # past the line brakes before it, where the lap distance is negative)
    tend = "On your quicker laps here you tended to"
    phr = {
        "min_mph": lambda z: (f"{tend} carry more minimum speed (r {z['r']:+.2f}). You reached {z['p75']:.0f} mph on a quarter of your laps; aim for that."
                              if z["r"] < 0 else f"{tend} carry a little less minimum speed (r {z['r']:+.2f}), which usually means a tidier entry. Try about {z['p25']:.0f} mph."),
        "exit_mph": lambda z: (f"{tend} come out faster (r {z['r']:+.2f}). Aim for {z['p75']:.0f} mph 150 m past the apex."
                               if z["r"] < 0 else f"Laps with more exit speed weren't quicker here (r {z['r']:+.2f}); on your quicker laps the time came from earlier in the corner."),
        "peak_brake": lambda z: (f"{tend} brake more lightly (r {z['r']:+.2f}). Try capping the pedal near {z['p25']:.0f}% instead of {z['median']:.0f}%."
                                 if z["r"] > 0 else f"{tend} brake a bit harder (r {z['r']:+.2f}), around {z['p75']:.0f}% peak."),
        "brake_on": lambda z: (f"{tend} brake later (r {z['r']:+.2f}). Try braking about {apex - z['p75']:.0f} m before the apex "
                               f"instead of {apex - z['median']:.0f} m."
                               if z["r"] < 0 else f"{tend} brake a little earlier (r {z['r']:+.2f}), about {apex - z['p25']:.0f} m before the apex."),
        "full_thr": lambda z: (f"{tend} reach full throttle sooner (r {z['r']:+.2f}). Try to be flat by about {z['p25']:.0f} m."
                               if z["r"] > 0 else f"{tend} pick up full throttle a little later and more cleanly (r {z['r']:+.2f})."),
        "abs_s": lambda z: (f"{tend} spend less time on ABS (r {z['r']:+.2f}). Ease the peak pressure so it barely triggers."
                            if z["r"] > 0 else f"Laps with more time on ABS weren't slower here (r {z['r']:+.2f})."),
        "coast_s": lambda z: (f"{tend} coast less between brake and throttle (r {z['r']:+.2f})."
                              if z["r"] > 0 else f"{tend} take a short coast before the throttle (r {z['r']:+.2f})."),
    }
    for z in drivers:
        parts.append(phr[z["metric"]](z))
    if not parts:
        parts.append("Nothing in your own laps clearly links to the time here (the differences between laps are within what chance "
                     "alone would give). Use Compare laps to see where your laps differ here.")
    main = next((r for r in refs if r.get("main")), refs[0] if refs else None)
    if main:
        rc = main["corners"].get(c["id"])
        if rc:
            who = (main['name'] or "The comparison lap").split(' ')[0]
            bits = [f"{who} carries {rc['min_mph']:.0f} mph at the slowest point (you, typically: {summ['min_mph']:.0f})"]
            if rc.get("peak_brake") is not None and summ.get("peak_brake") is not None:
                bits.append(f"peaks at {rc['peak_brake']:.0f}% brake (you: {summ['peak_brake']:.0f}%)")
            if rc.get("gear") is not None and summ.get("gear") is not None and rc["gear"] != summ["gear"]:
                bits.append(f"is in gear {rc['gear']} (you: {summ['gear']}); if they drive a different car that may just be the gearing")
            parts.append("For comparison, " + ", ".join(bits) + ".")
    return " ".join(parts)


def build_findings(laps, best, median, unclean, slow, excluded, sl, span, optimal, ranked, refs,
                   clean_n=None, pit=(), slow_cut=1.0, excluded_laps=()):
    f = []
    if clean_n is None:
        clean_n = len(laps) - len(set(unclean) | set(slow))
    kind = "Best clean lap" if best.get("clean") else "Best lap (every lap was flagged, so it doesn't count as a personal best)"
    f.append(f"{len(laps)} full laps analysed ({clean_n} clean). {kind} {fmt_t(best['t'])} on lap {best['n']}, median {fmt_t(median)}.")
    if pit:
        f.append(f"Pit stop: lap(s) {', '.join(map(str, pit))} left out of your pace numbers.")
    if excluded:
        which = f" (lap(s) {', '.join(map(str, excluded_laps))})" if excluded_laps else ""
        f.append(f"{excluded} lap(s){which} were left out as tow or spin laps (more than 25% off the median).")
    if optimal:
        f.append(f"Your best sectors from clean laps add up to {fmt_t(optimal)}, {best['t'] - optimal:.2f}s under your best lap.")
    if unclean:
        f.append(f"Flagged unclean by Garage 61 (track limits or incident): lap(s) {', '.join(map(str, unclean))}.")
    if slow:
        f.append(f"More than {slow_cut:.1f} s off your median: lap(s) {', '.join(map(str, slow))}.")
    total = sl * span
    if abs(total) >= 0.2 and span >= 4:
        f.append(f"Clean laps got about {abs(total):.2f}s {'slower' if total > 0 else 'quicker'} from start to finish "
                 "(tyres, fuel and track conditions all play a part).")
    top = [c for c in ranked[:2] if c["avail"] >= 0.05]
    if top:
        f.append("Most time on the table: " + " and ".join(f"{c['id']} (~{c['avail']:.2f}s a lap)" for c in top) + ".")
    main = next((r for r in refs if r.get("main")), refs[0] if refs else None)
    if main:
        f.append(f"Main comparison lap: {main['name']}, {fmt_t(main['t'])} ({best['t'] - main['t']:+.2f}s vs your best).")
    return f


def build_plan(ranked, sl, span, outlier, refs, unclean=()):
    """outlier: the off-pace (slow) laps to point the driver at. Lap 1 is left out (standing or rolling
    start), at most 5 are listed, and Garage 61-flagged laps get their own line."""
    plan = []
    for c in ranked[:3]:
        if c["avail"] < 0.04:
            continue
        plan.append({"title": f"{c['id']} (apex ~{c['apex_m']:.0f} m)", "gain": f"≈{c['avail']:.2f}s per lap",
                     "text": c["coaching"]})
    in_plan = {c["id"] for c in ranked[:3] if c["avail"] >= 0.04}
    gaps = [c for c in ranked if c.get("ref_gap") is not None and c["id"] not in in_plan and c["ref_gap"] >= 0.2]
    if gaps:
        c = max(gaps, key=lambda c: c["ref_gap"])
        lead = ("Your laps here are consistent, so the gap is in how you drive it rather than in mistakes. "
                if c.get("iqr") is not None and c["iqr"] < 0.08 else "")
        plan.append({"title": f"{c['id']}: your biggest gap to the comparison lap", "gain": f"≈{c['ref_gap']:.2f}s per lap",
                     "text": lead + c["coaching"]})
    total = sl * span
    if total >= 0.3 and span >= 6:
        plan.append({"title": "Hold your pace later in the race", "gain": f"≈{total:.2f}s by the end",
                     "text": "Your clean laps slowed as the race went on. Some of that is tyres, fuel and track; to check how much is you, "
                             "compare an early and a late lap in Compare laps and look for earlier braking or a later throttle."})
    off = [n for n in sorted(set(outlier or [])) if n != 1]
    if off:
        more = f" (and {len(off) - 5} more)" if len(off) > 5 else ""
        plan.append({"title": "Look at your off laps", "gain": "",
                     "text": f"{'Lap' if len(off) == 1 else 'Laps'} {', '.join(map(str, off[:5]))}{more} cost the most time. "
                             "Open them in Compare laps to see where they went wrong."})
    unclean = sorted(set(unclean or []))
    if unclean:
        plan.append({"title": "Flagged laps", "gain": "",
                     "text": f"Garage 61 flagged {len(unclean)} lap(s) for track limits or an incident"
                             + (f": {', '.join(map(str, unclean[:8]))}" + (" ..." if len(unclean) > 8 else "") if len(unclean) < 12 else "")
                             + ". They're left out of your corner numbers."})
    return plan
