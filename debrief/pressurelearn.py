"""Pressure learner (2.15): cold starting pressure, hot pressure and lap pace by track temperature, from Setup Lab's
index of his own telemetry files.

    learn(idx, car_path, track_code)  -> report (per 5 °C track-temperature band, plus a hot-pressure prediction)
    predict_hot(model, cold_front, cold_rear, track_temp) -> {"front", "rear", "miss_kpa"} or None
    runs(records)                     -> one row per run (a stint with enough pace laps)

A run is one stint (Setup Lab's stints inside a file) with at least MIN_RUN_LAPS pace laps (setuplab.pace_laps:
clean, not the out lap or the first flying lap, not in traffic, not a slow outlier). Its numbers: the cold
(starting) pressures from the setup in that file, the median live hot pressure on its laps from lap HOT_POS on
(setuplab's hot-pressure spot: the fastest point of each lap), the median pace lap, and the track temperature.

Honest statistics:
  - "your fastest runs were on X cold" is said only for one car + track, in one 5 °C band, with at least
    MIN_BAND_RUNS runs on at least two different cold pressures, when a shuffle test passes. The unit shuffled is
    the session, not the run: runs of one pressure driven one after the other on the same day share that day's track,
    rubber and form, so they count once (a "block": consecutive sessions on one pressure on one day). Each pressure
    needs at least MIN_BLOCKS blocks and the pressures must be driven back and forth (not one block each, one after
    the other); otherwise nothing is tested and the band says the day can't be separated from the pressure. The cold
    pressures are shuffled between blocks, the fastest group is picked again in every shuffle (so picking the best
    one is part of the null), and the real gap must beat 95 % of the shuffles and be at least MIN_GAP_S. Otherwise
    the band just lists what was run.
  - Pace is never compared across tracks.
  - The hot-pressure prediction (per car + track, else per car) is a straight line per axle,
    hot - cold = a + c x track temperature (c only when the runs span MIN_TEMP_SPREAD °C), from at least
    MIN_PRED_RUNS runs. Its "typical miss" is measured on runs left out one at a time (never on the runs that made
    the line).
Nothing here raises for bad input; everything is JSON-ready.
"""
from __future__ import annotations

import math
from datetime import datetime
from itertools import combinations

import numpy as np

from . import ibt, setuplab

BAND_C = 5.0                # track temperature band width (°C)
MIN_RUN_LAPS = 3            # pace laps a stint needs to count as a run
MIN_BAND_RUNS = 2           # runs in a band before anything is said about it
MIN_GAP_S = 0.10            # s a lap: a smaller gap is never called a difference
ALPHA = 0.05                # shuffle test level
N_SHUFFLE = 5000            # random shuffles when there are too many to list (fixed seed)
SEED = 215
MIN_PRED_RUNS = 4           # runs with hot pressures before a prediction is made
MIN_TEMP_SPREAD = 3.0       # °C of spread before the temperature term is learned
MIN_BLOCKS = 2              # separate blocks (sessions, back and forth) each pressure needs before it is compared
COLD_MARGIN = 2.0           # kPa: a cold pressure further than this outside the runs' own is a stretch
HOT_POS = setuplab.HOT_POS  # hot pressures from this lap of a stint on

PARAMS = {k: v for k, v in globals().items() if k.isupper() and isinstance(v, (int, float)) and not isinstance(v, bool)}
CORNERS = ("LF", "RF", "LR", "RR")
_CK = {"LF": "LeftFront", "RF": "RightFront", "LR": "LeftRear", "RR": "RightRear"}


def _r(v, d=1):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, d) if math.isfinite(f) else None


def cold_pressures(setup: dict) -> dict:
    """{corner: kPa} starting (cold) pressures from a CarSetup, wherever the car keeps them."""
    out = {}
    for k, v in ibt.flatten(setup or {}).items():
        parts = k.split(".")
        if len(parts) >= 2 and parts[-1] in ("StartingPressure", "ColdPressure"):
            for c, ck in _CK.items():
                if parts[-2] == ck and c not in out:
                    x = ibt.number(v)
                    if x is not None:
                        out[c] = x
    return out


def _records(idx) -> list:
    if isinstance(idx, dict):
        return [r for r in idx.get("files") or [] if isinstance(r, dict) and r.get("ok", True)]
    return [r for r in (idx or []) if isinstance(r, dict)]


def _cold_key(cold: dict):
    if len(cold) != 4:
        return None
    return tuple(int(round(cold[c])) for c in CORNERS)


def cold_text(key) -> str:
    if key is None:
        return "unknown cold pressures"
    lf, rf, lr, rr = key
    if lf == rf == lr == rr:
        return f"{lf} kPa cold"
    if lf == rf and lr == rr:
        return f"front {lf}, rear {lr} kPa cold"
    return f"{lf}/{rf}/{lr}/{rr} kPa cold (LF/RF/LR/RR)"


def runs(records: list) -> list:
    """One row per run: {"file", "stint", "when", "sort", "car_path", "car", "track_code", "track", "temp",
    "cold": {corner: kPa}, "cold_key", "hot": {corner: kPa} (may be empty), "laps", "times", "median"}."""
    out = []
    try:
        keep, _d = setuplab.pace_laps(records)
    except Exception:  # noqa: BLE001
        return out
    by_file = {r["file"]: r for r in records if r.get("file")}
    groups = {}
    for f, x in keep:
        groups.setdefault((f, x.get("stint")), []).append(x)
    for (f, st), xs in groups.items():
        if len(xs) < MIN_RUN_LAPS:
            continue
        r = by_file.get(f) or {}
        det = r.get("detail") or {}
        hot = {}
        hl = [x for x in det.get("laps") or [] if x.get("stint") == st and x.get("clean") and x.get("press")
              and (x.get("pos") or 0) >= HOT_POS]
        for c in CORNERS:
            v = [x["press"][c] for x in hl if c in x["press"]]
            if v:
                hot[c] = round(float(np.median(v)), 1)
        temp = det.get("surface_temp")
        if temp is None:
            temp = (r.get("track") or {}).get("surface_temp")
        cold = cold_pressures(r.get("setup") or {})
        times = [float(x["time"]) for x in xs]
        out.append({"file": f, "stint": st, "when": r.get("when") or "", "sort": r.get("sort") or 0,
                    "car_path": (r.get("car") or {}).get("path") or "", "car": (r.get("car") or {}).get("name") or "",
                    "track_code": (r.get("track") or {}).get("code") or "", "track": (r.get("track") or {}).get("name") or "",
                    "temp": _r(temp), "cold": cold, "cold_key": _cold_key(cold), "hot": hot, "laps": len(times),
                    "times": [round(t, 3) for t in times], "median": round(float(np.median(times)), 3)})
    out.sort(key=lambda x: (x["sort"], x["file"], x["stint"] or 0))
    return out


def _band(t):
    lo = math.floor(float(t) / BAND_C) * BAND_C
    return lo, lo + BAND_C


def band_text(lo, hi) -> str:
    return f"{lo:.0f} to {hi:.0f} °C"


def _labelings(labels: list, rng):
    """Every distinct reassignment of the group labels to runs (group sizes kept), or N_SHUFFLE random ones."""
    n = len(labels)
    sizes = {}
    for g in labels:
        sizes[g] = sizes.get(g, 0) + 1
    count = math.factorial(n)
    for s in sizes.values():
        count //= math.factorial(s)
    if count <= N_SHUFFLE:
        groups = list(sizes)

        def rec(rest, i):
            if i == len(groups) - 1:
                yield {j: groups[i] for j in rest}
                return
            for pick in combinations(sorted(rest), sizes[groups[i]]):
                for tail in rec(rest - set(pick), i + 1):
                    d = {j: groups[i] for j in pick}
                    d.update(tail)
                    yield d
        for d in rec(set(range(n)), 0):
            yield [d[j] for j in range(n)]
        return
    arr = np.array(labels, dtype=object)
    yield list(labels)
    for _ in range(N_SHUFFLE - 1):
        yield list(rng.permutation(arr))


def _fastest_gap(meds: list, labels: list) -> tuple:
    """(gap s a lap: other runs' mean minus the fastest group's mean, fastest group)."""
    by = {}
    for m, g in zip(meds, labels):
        by.setdefault(g, []).append(m)
    best = min(by, key=lambda g: (float(np.mean(by[g])), str(g)))
    others = [m for m, g in zip(meds, labels) if g != best]
    if not others:
        return 0.0, best
    return float(np.mean(others) - np.mean(by[best])), best


def shuffle_test(meds: list, labels: list) -> dict:
    """Is the fastest cold-pressure group really faster? Run medians, group labels -> {"gap", "best", "p", "n"}."""
    gap, best = _fastest_gap(meds, labels)
    rng = np.random.default_rng(SEED)
    gs = [(_fastest_gap(meds, lab)[0]) for lab in _labelings(list(labels), rng)]
    tot = len(gs)
    ge = sum(int(g >= gap - 1e-12) for g in gs)
    top = max(gs) if gs else gap
    p_min = sum(int(g >= top - 1e-12) for g in gs) / max(1, tot)    # the smallest p these runs could ever give
    return {"gap": gap, "best": best, "p": ge / max(1, tot), "n": tot, "p_min": p_min}


def _day(r: dict) -> str:
    try:
        s = float(r.get("sort") or 0)
        if s > 0:
            return datetime.fromtimestamp(s).date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        pass
    return str(r.get("file") or "")


def blocks(rs: list) -> list:
    """Runs in time order grouped into blocks: consecutive runs on the same cold pressure on the same day (one
    session, or sessions straight after each other). -> [{"label", "median" (mean of its runs), "day", "runs"}]."""
    out = []
    for r in sorted(rs, key=lambda x: (x.get("sort") or 0, x.get("file") or "", x.get("stint") or 0)):
        d = _day(r)
        if out and out[-1]["label"] == r["cold_key"] and out[-1]["day"] == d:
            out[-1]["runs"].append(r)
        else:
            out.append({"label": r["cold_key"], "day": d, "runs": [r]})
    for b in out:
        b["median"] = float(np.mean([r["median"] for r in b["runs"]]))
    return out


def back_and_forth(bl: list) -> tuple:
    """(ok, why) for comparing pressures across these blocks: each pressure in MIN_BLOCKS blocks or more, and the
    pressures not driven one block each after the other."""
    labels = [b["label"] for b in bl]
    per = {}
    for g in labels:
        per[g] = per.get(g, 0) + 1
    switches = sum(1 for a, b in zip(labels, labels[1:]) if a != b)
    if min(per.values()) >= MIN_BLOCKS and switches >= len(per):
        return True, ""
    days = {}
    for b in bl:
        days.setdefault(b["label"], set()).add(b["day"])
    sets = list(days.values())
    apart = all(not (a & c) for i, a in enumerate(sets) for c in sets[i + 1:])
    if apart:
        return False, ("each pressure was driven on different days, so the day can't be separated from the pressure: "
                       "drive both on the same day, back and forth (A, B, A, B), to compare them")
    return False, ("each pressure was driven in one go, one after the other, so getting quicker as you drive can't "
                   "be separated from the pressure: drive them back and forth (A, B, A, B) to compare them")


def _band_report(rs: list) -> dict:
    lo, hi = _band(rs[0]["temp"])
    rows = [{"when": r["when"], "temp": r["temp"], "cold": cold_text(r["cold_key"]),
             "hot": {c: v for c, v in r["hot"].items()}, "laps": r["laps"], "median": r["median"]} for r in rs]
    out = {"band": band_text(lo, hi), "lo": lo, "hi": hi, "runs": rows, "groups": [], "finding": None, "test": None,
           "text": ""}
    usable = [r for r in rs if r["cold_key"] is not None]
    groups = {}
    for r in usable:
        groups.setdefault(r["cold_key"], []).append(r)
    for k, g in sorted(groups.items(), key=lambda kv: float(np.mean([r["median"] for r in kv[1]]))):
        hot = {c: round(float(np.median([r["hot"][c] for r in g if c in r["hot"]])), 1)
               for c in CORNERS if any(c in r["hot"] for r in g)}
        out["groups"].append({"cold": cold_text(k), "runs": len(g), "median": round(float(np.mean([r["median"] for r in g])), 3),
                              "hot": hot})
    listing = "; ".join(f"{g['cold']}: {g['runs']} run{'s' if g['runs'] != 1 else ''}, typical lap "
                        f"{g['median']:.2f} s" for g in out["groups"])
    if len(rs) < MIN_BAND_RUNS:
        out["text"] = (f"At {out['band']} track: only {len(rs)} run so far ({listing or 'no cold pressures recorded'}). "
                       f"Not enough to tell which pressure is quicker.")
        return out
    if len(groups) < 2:
        out["text"] = (f"At {out['band']} track: {len(rs)} runs, all on {out['groups'][0]['cold'] if out['groups'] else 'unknown pressures'}"
                       f". Try another cold pressure to learn which is quicker.")
        return out
    bl = blocks(usable)
    ok, why_not = back_and_forth(bl)
    if not ok:
        out["test"] = {"blocks": len(bl), "runs": len(usable), "tested": False}
        out["text"] = f"At {out['band']} track: {listing}. Can't compare them yet: {why_not}."
        return out
    meds = [b["median"] for b in bl]
    labels = [b["label"] for b in bl]
    t = shuffle_test(meds, labels)
    out["test"] = {"gap_s": round(t["gap"], 3), "p": round(t["p"], 4), "shuffles": t["n"], "runs": len(usable),
                   "blocks": len(bl), "tested": True, "p_min": round(t["p_min"], 4)}
    best = groups[t["best"]]
    hot = out["groups"][0]["hot"] if out["groups"] and out["groups"][0]["cold"] == cold_text(t["best"]) else {}
    if t["p"] < ALPHA and t["gap"] >= MIN_GAP_S:
        fh = [hot[c] for c in ("LF", "RF") if c in hot]
        rh = [hot[c] for c in ("LR", "RR") if c in hot]
        hot_txt = ""
        if fh and rh:
            f_, r_ = float(np.mean(fh)), float(np.mean(rh))
            hot_txt = (f" (hot about {f_:.0f})" if abs(f_ - r_) < 1.5 else f" (hot about {f_:.0f} front, {r_:.0f} rear)")
        later = all(r["sort"] >= max(o["sort"] for o in usable if o["cold_key"] != t["best"]) for r in best)
        out["finding"] = {"cold": cold_text(t["best"]), "hot": hot, "gap_s": round(t["gap"], 3), "p": round(t["p"], 4),
                          "runs": len(best), "of": len(usable), "blocks": len(bl), "driven_later": bool(later)}
        out["text"] = (f"At {out['band']} track your fastest runs were on {cold_text(t['best'])}{hot_txt}: about "
                       f"{t['gap']:.1f} s a lap quicker than your other runs ({len(best)} of {len(usable)} runs, "
                       f"driven back and forth in {len(bl)} goes; a shuffle test across those goes says this is "
                       f"unlikely to be luck).")
        if later:
            out["text"] += (" Those runs were also your latest, so part of it may be you getting quicker: "
                            "go back to the other pressure for a run to be sure.")
    else:
        if t["p_min"] >= ALPHA:
            why = ("with this few goes no gap could pass the shuffle test: about 4 goes on each pressure, back and "
                   "forth, are needed")
        elif t["gap"] < MIN_GAP_S:
            why = f"the gap is only {t['gap']:.2f} s"
        else:
            why = "a gap this size turns up often when the pressures are shuffled between runs"
        out["text"] = (f"At {out['band']} track: {listing}. No clear winner yet ({why}).")
    return out


def _fit_axle(temps, rises, use_temp: bool):
    X = np.column_stack([np.ones(len(temps))] + ([np.asarray(temps, float)] if use_temp else []))
    beta, *_ = np.linalg.lstsq(X, np.asarray(rises, float), rcond=None)
    return beta


def predict_model(rs: list, basis: str) -> dict | None:
    """Per-axle line hot - cold = a + c x temp from runs with hot and cold pressures; leave-one-out typical miss."""
    use = [r for r in rs if r["temp"] is not None and len(r["cold"]) == 4 and all(c in r["hot"] for c in CORNERS)]
    if len(use) < MIN_PRED_RUNS:
        return None
    temps = [r["temp"] for r in use]
    use_temp = (max(temps) - min(temps)) >= MIN_TEMP_SPREAD
    cf = [float(np.mean([r["cold"][c] for c in ("LF", "RF")])) for r in use]
    cr = [float(np.mean([r["cold"][c] for c in ("LR", "RR")])) for r in use]
    out = {"basis": basis, "runs": len(use), "uses_temp": use_temp, "temp_range": [min(temps), max(temps)],
           "cold_range": {"front": [min(cf), max(cf)], "rear": [min(cr), max(cr)]}, "axles": {}, "miss_kpa": None}
    errs = []
    for axle, cs in (("front", ("LF", "RF")), ("rear", ("LR", "RR"))):
        rises = [float(np.mean([r["hot"][c] - r["cold"][c] for c in cs])) for r in use]
        beta = _fit_axle(temps, rises, use_temp)
        out["axles"][axle] = {"a": round(float(beta[0]), 3), "c": round(float(beta[1]), 4) if use_temp else 0.0}
        for i in range(len(use)):
            tt = temps[:i] + temps[i + 1:]
            rr = rises[:i] + rises[i + 1:]
            b = _fit_axle(tt, rr, use_temp)
            pred = b[0] + (b[1] * temps[i] if use_temp else 0.0)
            errs.append(abs(pred - rises[i]))
    out["miss_kpa"] = max(1, int(math.ceil(float(np.median(errs)))))
    out["worst_kpa"] = round(float(max(errs)), 1)
    return out


def predict_hot(model: dict | None, cold_front: float, cold_rear: float, track_temp: float | None) -> dict | None:
    """Predicted hot pressures (kPa) for these cold ones at this track temperature, with the typical miss."""
    try:
        if not model:
            return None
        t = float(track_temp) if track_temp is not None else float(np.mean(model["temp_range"]))
        out = {}
        for axle, cold in (("front", cold_front), ("rear", cold_rear)):
            a = model["axles"][axle]
            out[axle] = round(float(cold) + a["a"] + a["c"] * t, 1)
        out["miss_kpa"] = model["miss_kpa"]
        out["outside_range"] = not (model["temp_range"][0] - 2 <= t <= model["temp_range"][1] + 2)
        cold_out = []
        for axle, cold in (("front", cold_front), ("rear", cold_rear)):
            rg = (model.get("cold_range") or {}).get(axle)
            if rg and not (rg[0] - COLD_MARGIN <= float(cold) <= rg[1] + COLD_MARGIN):
                cold_out.append(axle)
        out["cold_outside"] = cold_out
        return out
    except Exception:  # noqa: BLE001
        return None


def _predict_text(model: dict | None, rs: list, where: str) -> str:
    if not model:
        n = len([r for r in rs if r["hot"] and r["cold"]])
        return (f"Hot pressure prediction: needs {MIN_PRED_RUNS} runs with hot pressures ({where} has {n}).")
    last = rs[-1]
    cf = float(np.mean([last["cold"][c] for c in ("LF", "RF")]))
    cr = float(np.mean([last["cold"][c] for c in ("LR", "RR")]))
    p = predict_hot(model, cf, cr, last["temp"])
    s = (f"From {model['runs']} runs ({where}): at {last['temp']:.0f} °C track, {cf:.0f} kPa cold at the front comes "
         f"up to about {p['front']:.0f} kPa hot, and {cr:.0f} at the rear to about {p['rear']:.0f} (typical miss "
         f"about {model['miss_kpa']} kPa, checked on runs left out).")
    if not model["uses_temp"]:
        s += f" Your runs were all within {MIN_TEMP_SPREAD:.0f} °C, so the effect of track temperature isn't learned yet."
    return s


def learn(idx, car_path: str, track_code: str | None = None) -> dict:
    """The pressure learner for one car (+ track). Never raises.

    {"car", "track", "runs": n, "bands": [band report], "by_car": [{"band", "runs", "tracks"}],
     "prediction": model | None, "prediction_text", "text": [lines], "params"}
    band report: {"band", "lo", "hi", "runs": [...], "groups": [{"cold", "runs", "median", "hot"}],
                  "finding": {"cold", "hot", "gap_s", "p", "runs", "of", "driven_later"} | None, "test", "text"}"""
    out = {"car": "", "car_path": car_path, "track": "", "track_code": track_code, "runs": 0, "bands": [],
           "by_car": [], "prediction": None, "prediction_text": "", "text": [], "params": PARAMS}
    try:
        recs = [r for r in _records(idx)
                if str((r.get("car") or {}).get("path") or "").lower() == str(car_path or "").lower()]
        all_runs = []
        for code in sorted({str((r.get("track") or {}).get("code") or "").lower() for r in recs}):
            all_runs += runs([r for r in recs if str((r.get("track") or {}).get("code") or "").lower() == code])
        mine = [r for r in all_runs if not track_code or r["track_code"].lower() == str(track_code).lower()]
        out["runs"] = len(mine)
        if recs:
            out["car"] = (recs[-1].get("car") or {}).get("name") or car_path
        if mine:
            out["track"] = mine[-1]["track"]
        bands = {}
        for r in mine:
            if r["temp"] is not None:
                bands.setdefault(_band(r["temp"]), []).append(r)
        if track_code:
            out["bands"] = [_band_report(rs) for _k, rs in sorted(bands.items())]
        byc = {}
        for r in all_runs:
            if r["temp"] is not None:
                b = byc.setdefault(_band(r["temp"]), {"runs": 0, "tracks": set()})
                b["runs"] += 1
                b["tracks"].add(r["track"] or r["track_code"])
        out["by_car"] = [{"band": band_text(*k), "runs": v["runs"], "tracks": sorted(v["tracks"])}
                         for k, v in sorted(byc.items())]
        model = predict_model(mine, "this car and track") if track_code else None
        where = "this car and track"
        if model is None:
            model = predict_model(all_runs, "this car, all tracks")
            where = "this car, all tracks" if model else where
        out["prediction"] = model
        out["prediction_text"] = _predict_text(model, mine or all_runs, where) if (mine or all_runs) else (
            f"Hot pressure prediction: needs {MIN_PRED_RUNS} runs with hot pressures.")
        lines = []
        if not mine:
            lines.append(f"No runs yet (a run is a stint with at least {MIN_RUN_LAPS} counted laps).")
        else:
            lines += [b["text"] for b in out["bands"]]
            if not any(b["finding"] for b in out["bands"]):
                lines.append(f"Not enough runs to learn the quickest pressure yet: that needs at least "
                             f"{MIN_BAND_RUNS} runs in the same {BAND_C:.0f} °C band on different cold pressures, "
                             f"and a gap a shuffle test can't explain.")
        lines.append(out["prediction_text"])
        out["text"] = lines
        return out
    except Exception as e:  # noqa: BLE001
        out["text"] = [f"The pressure learner could not read these runs ({str(e)[:120]})."]
        return out
