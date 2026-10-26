"""Debrief analysis engine: turns Garage 61 laps + telemetry into a review.

Works for any car and track. Corners are detected from your fastest lap's
speed trace, so nothing is hard-coded to one circuit.
"""
from __future__ import annotations

import io
import math
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
def parse_csv(text: str) -> dict:
    import csv
    text = text.lstrip("\ufeff").replace("\u00ef\u00bb\u00bf", "")
    reader = list(csv.reader(io.StringIO(text.strip())))
    head = [h.strip().lstrip("\ufeff") for h in reader[0]]
    cols = {h: i for i, h in enumerate(head)}
    need = ["Speed", "LapDistPct"]
    for n in need:
        if n not in cols:
            raise ValueError(f"telemetry CSV has no {n} column (columns: {', '.join(head[:8])})")
    rows = [r for r in reader[1:] if r]

    def col(name, conv=float, default=0.0):
        if name not in cols:
            return np.full(len(rows), default, dtype=float)
        i = cols[name]
        out = np.empty(len(rows))
        for k, r in enumerate(rows):
            v = r[i] if i < len(r) else ""
            if conv is bool:
                out[k] = 1.0 if v.strip().lower() == "true" else 0.0
            else:
                try:
                    out[k] = float(v)
                except ValueError:
                    out[k] = default
        return out

    return {
        "v": col("Speed"), "p": col("LapDistPct"), "lat": col("Lat", default=np.nan), "lon": col("Lon", default=np.nan),
        "b": col("Brake"), "th": col("Throttle"), "gear": col("Gear"), "abs": col("ABSActive", bool),
        "la": col("LatAccel"), "lo": col("LongAccel"), "st": col("SteeringWheelAngle"), "rpm": col("RPM"),
    }


def lap_length_from(raw: dict, lap_time: float) -> float:
    n = len(raw["v"])
    dt = lap_time / max(1, n - 1)
    return float(np.sum(raw["v"]) * dt)


def resample(raw: dict, lap_time: float, L: float) -> dict:
    """Put one lap on a common distance grid (STEP metres)."""
    n = len(raw["v"])
    t = np.linspace(0, lap_time, n)
    p = raw["p"].copy()
    # unwrap the start/finish crossing
    if p[0] > 0.9:
        p[: np.argmax(p < 0.5)] -= 1.0
    jumps = np.where(np.diff(p) < -0.5)[0]
    for j in jumps:
        p[j + 1:] += 1.0
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
    v = smooth(R["v"], 7)
    d = R["d"]
    n = len(v)
    w = int(window_m / STEP)
    look = int(400 / STEP)
    mins = []
    for i in range(n):
        lo, hi = max(0, i - w), min(n, i + w + 1)
        if v[i] > v[lo:hi].min() + 1e-9:
            continue
        left = v[max(0, i - look):i + 1].max()
        right = v[i:min(n, i + look)].max()
        if min(left, right) - v[i] >= min_drop_mph:
            if mins and d[i] - d[mins[-1]] < merge_m:
                if v[i] < v[mins[-1]]:
                    mins[-1] = i
                continue
            mins.append(i)
    # zone boundaries at the speed peak between neighbouring corners
    bounds = [0]
    for a, b in zip(mins, mins[1:]):
        bounds.append(a + int(np.argmax(v[a:b + 1])))
    bounds.append(n - 1)
    corners = []
    for k, i in enumerate(mins):
        start = max(float(d[bounds[k]]), float(d[i]) - 350.0)
        end = min(float(d[bounds[k + 1]]), float(d[i]) + 250.0)
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


def corner_metrics(R, c):
    d = R["d"]
    a, b, apex = c["start_m"], c["end_m"], c["apex_m"]
    m = (d >= a) & (d <= b)
    if m.sum() < 3:
        return None
    idx = np.where(m)[0]
    near = idx[np.abs(d[idx] - apex) <= 120]
    if len(near) == 0:
        near = idx
    i_min = near[np.argmin(R["v"][near])]
    pre = idx[d[idx] <= apex + 40]
    bo = brake_onset(R["b"], pre)
    post = idx[d[idx] > d[i_min]]
    ft = next((i for i in post if R["th"][i] > 95), None)
    cell_t = np.diff(R["tt"], append=R["tt"][-1])
    i_exit = min(len(d) - 1, i_min + int(150 / STEP))
    return {
        "t": tt_at(R, b) - tt_at(R, a),
        "min_mph": float(R["v"][i_min]),
        "min_at": float(d[i_min]),
        "gear": int(R["gear"][i_min]),
        "peak_brake": float(R["b"][idx].max()),
        "brake_on": float(d[bo]) if bo is not None else None,
        "full_thr": float(d[ft]) if ft is not None else None,
        "abs_s": float(cell_t[idx][R["abs"][idx] > 0.5].sum()),
        "coast_s": float(cell_t[idx][(R["th"][idx] < 5) & (R["b"][idx] < 5)].sum()),
        "exit_mph": float(R["v"][i_exit]),
    }


# ---------------------------------------------------------------- stats helpers
def corr(x, y):
    x, y = np.asarray(x, float), np.asarray(y, float)
    ok = ~(np.isnan(x) | np.isnan(y))
    x, y = x[ok], y[ok]
    if len(x) < 6 or x.std() == 0 or y.std() == 0:
        return None, None, len(x)
    r = float(np.corrcoef(x, y)[0, 1])
    slope = float(np.polyfit(x, y, 1)[0])
    return r, slope, len(x)


def pct(a, q):
    return float(np.percentile(np.asarray(a, float), q))


def lap_style(R):
    dt = np.diff(R["tt"], append=R["tt"][-1])
    T = dt.sum() or 1
    comb = np.hypot(R["la"], R["lo"])
    sr = np.abs(np.diff(R["st"], append=R["st"][-1])) / np.maximum(dt, 1e-3)
    return {
        "full_thr_pct": float(dt[R["th"] >= 98].sum() / T * 100),
        "brake_pct": float(dt[R["b"] >= 5].sum() / T * 100),
        "coast_s": float(dt[(R["th"] < 5) & (R["b"] < 5)].sum()),
        "overlap_s": float(dt[(R["th"] > 10) & (R["b"] > 10)].sum()),
        "g_over_1": float(dt[comb > 1.0].sum() / T * 100),
        "g95": float(np.percentile(comb, 95)),
        "steer_rms": float(np.sqrt(np.mean(np.clip(sr, 0, 2000) ** 2))),
        "top_mph": float(R["v"].max()),
    }


# ---------------------------------------------------------------- main
def analyze(meta: dict, my_laps: list, my_tel: dict, refs: list, ref_tel: dict, track_length: float | None = None, log=None) -> dict:
    """
    meta: {driver, car, track, event_name, started_at, ...}
    my_laps: Garage 61 lap objects for the race session (you)
    my_tel: lap_id -> CSV text
    refs: lap objects for comparison laps (teammates / followed drivers)
    ref_tel: lap_id -> CSV text
    """
    laps = []
    for l in my_laps:
        lt = g(l, "lap_time", "lapTime")
        lid = g(l, "id")
        if not lt or lid not in my_tel:
            continue
        try:
            raw = parse_csv(my_tel[lid])
        except (ValueError, IndexError) as e:
            if log and not getattr(analyze, "_warned", False):
                log(f"  Skipping a lap whose telemetry couldn't be read: {e}")
                analyze._warned = True
            continue
        num = g(l, "lap_number", "lapNumber", "lap")
        laps.append({"lap": l, "id": lid, "n": int(num) if isinstance(num, (int, float)) or str(num).isdigit() else None,
                     "start": l.get("_start"), "t": float(lt), "clean": bool(g(l, "clean", default=True)), "raw": raw})
    if len(laps) < 2:
        raise ValueError("not enough laps with readable telemetry in this race.")
    # reliable lap numbers: use Garage 61's if unique, otherwise number by start time
    nums = [x["n"] for x in laps]
    if None in nums or len(set(nums)) != len(nums):
        laps.sort(key=lambda x: (x["start"] is None, x["start"] or 0))
        for i, x in enumerate(laps):
            x["n"] = i + 1
    laps.sort(key=lambda x: x["n"])
    # partial laps (e.g. the 7 s "lap" at the finish line): Garage 61 marks them incomplete or missing;
    # anything under 75% of the median lap time can't be a full lap either
    n_in = len(laps)
    laps = [x for x in laps if not g(x["lap"], "incomplete", default=False) and not g(x["lap"], "missing", default=False)]
    flagged = n_in - len(laps)
    if laps:
        med_all = st.median(x["t"] for x in laps)
        laps = [x for x in laps if x["t"] >= 0.75 * med_all]
    partial = n_in - flagged - len(laps)
    if log and (flagged or partial):
        log(f"  Left out {flagged + partial} partial lap(s) ({flagged} marked incomplete by Garage 61, "
            f"{partial} under 75% of the median lap time).")
    if len(laps) < 2:
        raise ValueError("not enough full laps in this race.")
    n_total = len(laps)
    # drop out-laps / partial laps: far slower than the median or very short telemetry
    med_t = st.median(x["t"] for x in laps)
    laps = [x for x in laps if x["t"] < med_t * 1.25 and len(x["raw"]["v"]) > 100]
    excluded = n_total - len(laps)
    if len(laps) < 2:
        raise ValueError("not enough full laps in this race.")
    est = float(np.median([lap_length_from(x["raw"], x["t"]) for x in laps]))
    L = est
    if track_length:
        for mult in (1.0, 1000.0, 1609.344):
            cand = float(track_length) * mult
            if abs(cand - est) / est < 0.10:
                L = cand
                break
    for x in laps:
        x["R"] = resample(x["raw"], x["t"], L)

    times = np.array([x["t"] for x in laps])
    nums = [x["n"] for x in laps]
    median = float(np.median(times))
    unclean = [x["n"] for x in laps if not x["clean"]]
    slow = [x["n"] for x in laps if x["clean"] and x["t"] > median + 1.0]
    outlier = sorted(unclean + slow)
    clean = [x for x in laps if x["n"] not in outlier]
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
            vals = [None if g(s, "incomplete", default=False) else g(s, "sector_time", "sectorTime") for s in ss]
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
    optimal = None
    if sec_names:
        valid = [sectors[x["n"]] for x in clean if sectors.get(x["n"])]
        if valid:
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

    # reference laps
    ref_out = []
    for rl in refs:
        lid = g(rl, "id")
        lt = g(rl, "lap_time", "lapTime")
        if lid not in ref_tel or not lt:
            continue
        try:
            RR = resample(parse_csv(ref_tel[lid]), float(lt), L)
        except ValueError:
            continue
        drv = g(rl, "driver", default={}) or {}
        name = g(rl, "driver_name", "driverName") or " ".join(
            filter(None, [g(drv, "firstname", "firstName"), g(drv, "lastname", "lastName")])) or "Reference"
        ref_out.append({"id": lid, "name": name, "t": float(lt), "R": RR,
                        "rating": g(rl, "driver_rating", "driverRating"),
                        "corners": {c["id"]: corner_metrics(RR, c) for c in corners},
                        "style": lap_style(RR), "role": rl.get("_role", ""), "main": bool(rl.get("_main"))})

    ref_out.sort(key=lambda r: 0 if r["main"] else 1)

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
        summ = {k: float(np.median([m[k] for m in rows.values() if m[k] is not None]))
                if any(m[k] is not None for m in rows.values()) else None
                for k in ("min_mph", "peak_brake", "brake_on", "full_thr", "abs_s", "coast_s", "exit_mph")}
        summ["gear"] = int(st.mode([m["gear"] for m in rows.values()]))
        # what made YOUR laps faster here
        drivers = []
        for k, unit, label in (("min_mph", "mph", "minimum speed"), ("exit_mph", "mph", "exit speed"),
                               ("peak_brake", "%", "peak brake"), ("brake_on", "m", "brake point"),
                               ("full_thr", "m", "full-throttle point"), ("abs_s", "s", "time on ABS"),
                               ("coast_s", "s", "coasting")):
            xs = [cl[n][k] for n in cl if cl[n][k] is not None]
            ys = [cl[n]["t"] for n in cl if cl[n][k] is not None]
            r, slope, n = corr(xs, ys)
            if r is not None and abs(r) >= 0.5 and n >= 10:
                drivers.append({"metric": k, "label": label, "unit": unit, "r": r, "slope": slope, "n": n,
                                "p25": pct(xs, 25), "p75": pct(xs, 75), "median": float(np.median(xs))})
        drivers.sort(key=lambda z: -abs(z["r"]))
        coach = coaching_text(c, summ, drivers[:2], ref_out, iqr)
        ref_gap = None
        main = next((r for r in ref_out if r.get("main")), ref_out[0] if ref_out else None)
        if main and main["corners"].get(c["id"]):
            ref_gap = pct(cts, 25) - main["corners"][c["id"]]["t"]  # your better laps vs their lap
        corner_out.append({**c, "median_t": med, "best_t": rows[best_n]["t"], "best_lap": best_n,
                           "p25_t": pct(cts, 25), "avail": avail, "iqr": iqr, "ref_gap": ref_gap, "summary": summ,
                           "drivers": drivers[:3], "coaching": coach,
                           "per_lap": {str(n): {k: (round(v, 3) if isinstance(v, float) else v) for k, v in m.items()}
                                       for n, m in rows.items()},
                           "refs": {r["name"]: r["corners"].get(c["id"]) for r in ref_out}})
    ranked = sorted(corner_out, key=lambda c: -c["avail"])
    for i, c in enumerate(ranked):
        c["rank"] = i + 1

    # heatmap: corner zone time minus your best in that zone, per lap
    heat = {"laps": [x["n"] for x in laps], "zones": [c["id"] for c in corner_out],
            "values": [[round(per[c["id"]][x["n"]]["t"] - min(m["t"] for m in per[c["id"]].values()), 3)
                        if x["n"] in per[c["id"]] else None for c in corner_out] for x in laps]}

    style = {x["n"]: lap_style(x["R"]) for x in laps}
    style_trend = None
    sl_laps = [x for x in laps if x["n"] in clean_ns] or laps
    if len(sl_laps) >= 6:
        k3 = len(sl_laps) // 3
        early = [style[x["n"]] for x in sl_laps[:k3]]
        late = [style[x["n"]] for x in sl_laps[-k3:]]
        avg = lambda arr, k: float(np.mean([a[k] for a in arr]))
        style_trend = {k: [avg(early, k), avg(late, k)] for k in style[best["n"]]}

    # gg for best lap and first ref
    def gg(R):
        b = {}
        for la, lo in zip(np.round(R["la"] * 10) / 10, np.round(R["lo"] * 10) / 10):
            b[(la, lo)] = b.get((la, lo), 0) + 1
        return [{"la": float(k[0]), "lo": float(k[1]), "n": n} for k, n in b.items()]

    span = (max(nums) - min(nums)) if nums else 0
    findings = build_findings(laps, best, median, unclean, slow, excluded, sl, span, optimal, ranked, ref_out)
    plan = build_plan(ranked, sl, span, outlier, ref_out)

    segments = build_segments(corner_out, laps, ref_out, L)

    trace = lambda R: {"tt":[round(v, 2) for v in R["tt"][::2]], "v": [int(round(v)) for v in R["v"][::2]],
                       "b": [int(round(v)) for v in R["b"][::2]], "th": [int(round(v)) for v in R["th"][::2]],
                       "g": [int(v) for v in R["gear"][::2]]}
    data = {
        "meta": meta, "L": L, "step": STEP * 2,
        "laps": [{"n": x["n"], "t": x["t"], "clean": x["n"] not in outlier,
                  "sectors": sectors.get(x["n"])} for x in laps],
        # clean=False: Garage 61 flagged every lap, so this "best" is for display only (never a PB/goal)
        "best": {"n": best["n"], "t": best["t"], "clean": bool(best["clean"])}, "median": median,
        "std_clean": float(np.std(clean_t, ddof=1)) if len(clean_t) > 2 else None,
        "optimal": optimal, "sector_names": sec_names, "sector_cuts": sec_cuts, "sector_fade": sec_fade,
        "trend_per10": float(sl * 10), "trend_total": float(sl * span), "trend_line": [float(ic + sl * n) for n in nums], "outliers": outlier,
        "corners": corner_out, "heat": heat,
        "style": {str(k): v for k, v in style.items()}, "style_trend": style_trend,
        "gg_best": gg(best["R"]), "gg_ref": gg(ref_out[0]["R"]) if ref_out else None,
        "map": ([[round(float(a), 6), round(float(b), 6)] for a, b in zip(best["R"]["lat"][::2], best["R"]["lon"][::2])]
                if np.any(np.abs(best["R"]["lat"]) > 1e-6) else []),
        "traces": {**{f"me{x['n']}": {**trace(x["R"]), "t": x["t"], "label": f"Your lap {x['n']}"} for x in laps},
                   **{f"ref{i}": {**trace(r["R"]), "t": r["t"], "label": r["name"], "role": r["role"]}
                      for i, r in enumerate(ref_out)}},
        "refs": [{"key": f"ref{i}", "name": r["name"], "t": r["t"], "rating": r["rating"], "role": r["role"],
                  "main": r["main"], "style": r["style"]} for i, r in enumerate(ref_out)],
        "counts": {"analysed": len(laps), "clean": len(clean), "unclean": unclean, "slow": slow, "excluded": excluded,
                   "partial": flagged + partial},
        "findings": findings, "plan": plan,
        "segments": segments,
    }
    # v2 extras (stint, technique, consistency, best vs worst, race context); guarded inside
    try:
        analysis_plus.extend(data, laps=laps, clean=clean, best=best, corners=corner_out, per=per,
                             refs=ref_out, raw_laps=my_laps, log=log)
    except Exception as e:  # belt and braces: extras must never cost the user the review
        if log:
            log(f"  Skipped the extra analysis ({type(e).__name__}: {e}).")
    return data


def build_segments(corner_out, laps, ref_out, L):
    """Split the lap into one contiguous segment per corner so segment times add up to the lap time.

    Additive output (schema key `segments`):
      names[]            corner ids, one per segment
      cuts[]             segment boundaries in metres, 0 .. L (len = names + 1)
      laps{n: [t...]}    your segment times per lap
      refs{refK: [t...]} comparison laps' segment times
      eligible[]         lap numbers Garage 61 marked clean (used for the best possible lap)
      best[{t, lap}]     fastest segment among eligible laps and which lap it came from
      possible           sum of best segments (None if fewer than 2 eligible laps)
    """
    if not corner_out:
        return None
    cs = sorted(corner_out, key=lambda c: c["apex_m"])
    cuts = [0.0]
    for a, b in zip(cs, cs[1:]):
        cuts.append(float((a["end_m"] + b["start_m"]) / 2))
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
    refs = {}
    for i, r in enumerate(ref_out):
        s = split(r["R"])
        tot = sum(s)
        refs[f"ref{i}"] = [round(v * r["t"] / tot, 3) for v in s] if tot > 0 else s
    eligible = [x["n"] for x in laps if x["clean"]] or [x["n"] for x in laps]
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
    tend = "On your quicker laps here you tended to"
    phr = {
        "min_mph": lambda z: (f"{tend} carry more minimum speed (r {z['r']:+.2f}). You reached {z['p75']:.0f} mph on a quarter of your laps; aim for that."
                              if z["r"] < 0 else f"{tend} carry a little less minimum speed (r {z['r']:+.2f}), which usually means a tidier entry. Try about {z['p25']:.0f} mph."),
        "exit_mph": lambda z: (f"{tend} come out faster (r {z['r']:+.2f}). Aim for {z['p75']:.0f} mph 150 m past the apex."
                               if z["r"] < 0 else f"Exit speed didn't help here (r {z['r']:+.2f}); focus on the entry instead."),
        "peak_brake": lambda z: (f"{tend} brake more lightly (r {z['r']:+.2f}). Try capping the pedal near {z['p25']:.0f}% instead of {z['median']:.0f}%."
                                 if z["r"] > 0 else f"{tend} brake a bit harder (r {z['r']:+.2f}), around {z['p75']:.0f}% peak."),
        "brake_on": lambda z: (f"{tend} brake later (r {z['r']:+.2f}). Try about {z['p75']:.0f} m instead of {z['median']:.0f} m."
                               if z["r"] < 0 else f"{tend} brake a little earlier (r {z['r']:+.2f}), around {z['p25']:.0f} m."),
        "full_thr": lambda z: (f"{tend} reach full throttle sooner (r {z['r']:+.2f}). Try to be flat by about {z['p25']:.0f} m."
                               if z["r"] > 0 else f"{tend} pick up full throttle a little later and more cleanly (r {z['r']:+.2f})."),
        "abs_s": lambda z: (f"{tend} spend less time on ABS (r {z['r']:+.2f}). Ease the peak pressure so it barely triggers."
                            if z["r"] > 0 else f"Laps with more ABS time weren't slower here (r {z['r']:+.2f}), so heavy braking isn't what's costing you in this corner."),
        "coast_s": lambda z: (f"{tend} coast less between brake and throttle (r {z['r']:+.2f})."
                              if z["r"] > 0 else f"{tend} take a short coast before the throttle (r {z['r']:+.2f})."),
    }
    for z in drivers:
        parts.append(phr[z["metric"]](z))
    if not parts:
        parts.append("Nothing in your own laps clearly links to the time here. Use Compare laps to see where your laps differ here.")
    main = next((r for r in refs if r.get("main")), refs[0] if refs else None)
    if main:
        rc = main["corners"].get(c["id"])
        if rc:
            bits = [f"{main['name'].split(' ')[0]} carries {rc['min_mph']:.0f} mph at the slowest point (you, typically: {summ['min_mph']:.0f})",
                    f"peaks at {rc['peak_brake']:.0f}% brake (you: {summ['peak_brake']:.0f}%)"]
            if rc["gear"] != summ["gear"]:
                bits.append(f"is in gear {rc['gear']} (you: {summ['gear']}); if they drive a different car that may just be the gearing")
            parts.append("For comparison, " + ", ".join(bits) + ".")
    return " ".join(parts)


def build_findings(laps, best, median, unclean, slow, excluded, sl, span, optimal, ranked, refs):
    f = []
    clean_n = len(laps) - len(unclean) - len(slow)
    f.append(f"{len(laps)} full laps analysed ({clean_n} clean). Best clean lap {fmt_t(best['t'])} on lap {best['n']}, median {fmt_t(median)}.")
    if excluded:
        f.append(f"{excluded} lap(s) were left out as pit, tow or spin laps (more than 25% off the median).")
    if optimal:
        f.append(f"Your best sectors from clean laps add up to {fmt_t(optimal)}, {best['t'] - optimal:.2f}s under your best lap.")
    if unclean:
        f.append(f"Flagged unclean by Garage 61 (track limits or incident): lap(s) {', '.join(map(str, unclean))}.")
    if slow:
        f.append(f"More than a second off your median: lap(s) {', '.join(map(str, slow))}.")
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


def build_plan(ranked, sl, span, outlier, refs):
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
        lead = ("Your laps here are consistent, so the gap is technique rather than mistakes. "
                if c.get("iqr") is not None and c["iqr"] < 0.08 else "")
        plan.append({"title": f"{c['id']}: your biggest gap to the comparison lap", "gain": f"≈{c['ref_gap']:.2f}s per lap",
                     "text": lead + c["coaching"]})
    total = sl * span
    if total >= 0.3 and span >= 6:
        plan.append({"title": "Hold your pace later in the race", "gain": f"≈{total:.2f}s by the end",
                     "text": "Your clean laps slowed as the race went on. Some of that is tyres, fuel and track; to check how much is you, "
                             "compare an early and a late lap in Compare laps and look for earlier braking or a later throttle."})
    if outlier:
        plan.append({"title": "Look at your off laps", "gain": "",
                     "text": f"Laps {', '.join(map(str, outlier))} cost the most time. Open them in Compare laps to see where they went wrong."})
    return plan
