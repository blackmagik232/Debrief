"""Car vs car (2.18): which car of a class is quicker at a track, judged so a car isn't judged by its drivers.

Where the numbers come from (all read only, nothing is fetched):
  - iRacing's results file (the CSV he adds to a race review; since 2.18 the parsed table is kept beside the review
    as field.json): every driver's car, class, iRating, average and fastest lap, laps and whether they finished;
  - replay scans (Debrief's own and RaceCap's) whose drivers carry a car name (scans made since the scan learned the
    car): every driver's lap times, but no iRating, so those drivers count in the raw numbers only;
  - his own telemetry files (.ibt) in two cars of the class at the track: where each car is faster.

Public API (JSON friendly, nothing raises for bad input):
    collect(library_folder, racecap_folder=None)            -> {"races": [...], "notes": [...]}
    options(races)                                           -> [{"track", "class", "cars", "drivers"}]
    compare(races, track, cls, me=None, cars=None)           -> the class table, verdicts and how it was worked out
    you_vs_field(races, track, car, me)                      -> his race pace against drivers in the same car and band
    where_faster(laps_a, laps_b, name_a, name_b)             -> corner and straight differences from telemetry laps
    season_pick(sched, races, series_id, week)               -> which car has been quickest there, with confidence

Honesty rules:
  - pace is a driver's average race lap from the results (the median lap in a scan). Not counted: a driver who
    didn't finish, under MIN_LAPS laps, or an average over OUTLIER x the class median of that race. Each race's
    class median is taken off, so a hot day doesn't count against the cars that raced that day;
  - "similar drivers": a lap-time slope per 1000 iRating (Theil-Sen, from pairs of drivers in the SAME car, so a
    car's speed can't leak into it) is taken off, and then cars are only compared inside the same iRating band;
  - a sentence saying one car is quicker needs the permutation test (Freedman-Lane, see adjusted_test): N_PERM
    shuffles inside each band (fixed seed) and the gap must be bigger than the biggest gap between ANY two cars in
    all but ALPHA of the shuffles (so five cars don't give ten chances of a false claim), and MIN_DRIVERS drivers with an
    iRating in each car. Otherwise the numbers are shown with "not enough to tell". Sample sizes are always shown.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

import numpy as np

VERSION = 1
FIELD_FILE = "field.json"           # beside review.json: the parsed results table (server.py writes it)
BANDS = ((0, 1500, "under 1500"), (1500, 2500, "1500 to 2500"), (2500, 3500, "2500 to 3500"), (3500, 10 ** 6, "over 3500"))
MIN_DRIVERS = 15                    # drivers with an iRating in each car before a "quicker" sentence
MIN_LAPS = 3
OUTLIER = 1.07                      # average lap over 107 % of the race's class median: not counted
N_PERM = 1000
SEED = 218
ALPHA = 0.05
MIN_SLOPE_DIR = 100                 # iRating apart before a same-car pair counts towards the slope
YOU_MIN = 5                         # drivers to compare with before a percentile
# telemetry
WF_MIN_LAPS = 3
ROUGH_FLOOR = {"t": 0.10, "min_kmh": 3.0, "exit_kmh": 3.0, "top_kmh": 3.0, "brake_m": 10.0}
NOISE_FLOOR = {"t": 0.02, "min_kmh": 0.5, "exit_kmh": 0.5, "top_kmh": 0.5, "brake_m": 6.0}   # brake points sit on a 5 m grid
MIN_STRAIGHT_M = 120


# ---------------------------------------------------------------- names
def class_key(car=None, cls=None) -> str:
    """'GT4' for 'GT4 Class', 'gt4' or a GT4 car name. The car's name wins (iRacing's offline class is the car)."""
    from .schedule import class_of
    k = class_of(car) if car else None
    if k:
        return k
    s = re.sub(r"\bclass\b", "", str(cls or ""), flags=re.I).strip()
    return k or (class_of(s) or s.upper() or "Unknown class")


def car_key(name) -> str:
    from .schedule import norm
    return norm(name)


class _Names:
    """One display name per track / car: names that are the same share the first one seen."""

    def __init__(self, same):
        self.same, self.names = same, []

    def key(self, name) -> str:
        name = " ".join(str(name or "").split()) or "Unknown"
        for n in self.names:
            if self.same(n, name):
                return n
        self.names.append(name)
        return name


def _same_track(a, b):
    from .racecap_link import same_track
    return same_track(a, b)


def _same_car(a, b):
    return car_key(a) == car_key(b)


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


# ---------------------------------------------------------------- collecting races
def _race_from_field(doc, label) -> dict | None:
    if not isinstance(doc, dict) or not isinstance(doc.get("drivers"), list):
        return None
    drivers = []
    for d in doc["drivers"]:
        if not isinstance(d, dict) or not d.get("model"):
            continue
        f = d.get("fields") or {}
        lap = d.get("lap") or {}
        drivers.append({"name": str(d.get("name") or ""), "car": str(d["model"]), "class": class_key(d["model"], d.get("class")),
                        "irating": _num(f.get("irating_before")), "pace": _num(lap.get("avg")), "best": _num(lap.get("best")),
                        "laps": lap.get("laps"), "finished": (f.get("finish_status") or "running") == "running"})
    if not drivers:
        return None
    return {"source": "results", "id": str(doc.get("subsession") or label), "track": str(doc.get("track") or ""),
            "when": str(doc.get("when") or ""), "drivers": drivers}


def _race_from_scan(scan, label) -> dict | None:
    drivers = []
    for c in scan.get("cars") or []:
        if not isinstance(c, dict) or not c.get("car"):
            continue
        laps = [_num(v) for v in (c.get("lap_times") or {}).values()]
        laps = [x for x in laps if x and x > 0]
        best = _num((c.get("best_lap") or {}).get("time"))
        stopped = c.get("stopped_racing") or c.get("ended") or c.get("never_on_track")
        drivers.append({"name": str(c.get("name") or ""), "car": str(c["car"]), "class": class_key(c["car"]),
                        "irating": None, "pace": float(np.median(laps)) if laps else None, "best": best,
                        "laps": c.get("laps_completed"), "finished": not stopped})
    if not drivers:
        return None
    return {"source": "scan", "id": str(scan.get("subsession") or label), "track": str(scan.get("track") or ""),
            "when": "", "drivers": drivers}


def collect(library_folder=None, racecap_folder=None) -> dict:
    """Every race with car names: saved results tables first, then replay scans of races without one."""
    races, notes, seen = [], [], set()
    no_cars = 0
    lib = Path(str(library_folder)) if library_folder else None
    if lib is not None:
        try:
            files = sorted((lib / "reviews").glob("*/" + FIELD_FILE))
        except OSError:
            files = []
        for f in files:
            try:
                r = _race_from_field(json.loads(f.read_text(encoding="utf-8")), f.parent.name)
            except (OSError, ValueError):
                r = None
            if r and r["id"] not in seen:
                seen.add(r["id"])
                races.append(r)
    try:
        from . import racecap_link, replay_scan
        roots = []
        if lib is not None:
            roots.append(replay_scan.scans_root(lib))
        if racecap_folder:
            rr = racecap_link.races_root(racecap_folder)
            if rr is not None:
                roots.append(rr)
        for root in roots:
            if not Path(root).is_dir():
                continue
            for sub, _slug, d in racecap_link.race_dirs(Path(root)):
                if str(sub) in seen:
                    continue
                scan = racecap_link.read_scan(d)
                if not scan:
                    continue
                r = _race_from_scan(scan, d.name)
                if r is None:
                    no_cars += 1
                    continue
                seen.add(r["id"])
                races.append(r)
    except Exception:  # noqa: BLE001
        notes.append("Some replay scans couldn't be read.")
    if no_cars:
        notes.append(f"{no_cars} replay scan{'s' if no_cars != 1 else ''} without car names (made before Debrief "
                     "read them); add the iRacing results file to that race's review instead.")
    return {"races": races, "notes": notes}


def _tracks(races):
    t = _Names(_same_track)
    for r in races:
        r["_track"] = t.key(r["track"])
    return t


def options(races) -> list:
    """Track and class pairs with at least two cars, most drivers first."""
    _tracks(races)
    cars = _Names(_same_car)
    agg = {}
    for r in races:
        for d in r["drivers"]:
            k = (r["_track"], d["class"])
            a = agg.setdefault(k, {"cars": set(), "drivers": 0})
            a["cars"].add(cars.key(d["car"]))
            a["drivers"] += 1
    out = [{"track": k[0], "class": k[1], "cars": sorted(v["cars"]), "drivers": v["drivers"]} for k, v in agg.items()]
    out.sort(key=lambda o: (-(len(o["cars"]) > 1), -o["drivers"], o["track"], o["class"]))
    return out


# ---------------------------------------------------------------- the statistics
def band_of(ir) -> int | None:
    if ir is None:
        return None
    for k, (a, b, _l) in enumerate(BANDS):
        if a <= ir < b:
            return k
    return None


def theil_sen_within(x, y, g) -> float:
    """Median slope of y on x over pairs in the same group g (|dx| >= MIN_SLOPE_DIR). 0 when there are none."""
    x, y, g = np.asarray(x, float), np.asarray(y, float), np.asarray(g)
    slopes = []
    for k in np.unique(g):
        i = np.nonzero(g == k)[0]
        if len(i) < 2:
            continue
        xi, yi = x[i], y[i]
        dx = xi[:, None] - xi[None, :]
        dy = yi[:, None] - yi[None, :]
        m = np.triu(np.abs(dx) >= MIN_SLOPE_DIR, 1)
        if m.any():
            slopes.append(dy[m] / dx[m])
    return float(np.median(np.concatenate(slopes))) if slopes else 0.0


def _pair_stats(y, car, band, n_cars, pairs):
    """Per pair (a, b): band-weighted mean of (mean_a - mean_b) and the weight sum. Bands lacking either car drop."""
    nb = len(BANDS)
    idx = band * n_cars + car
    s = np.bincount(idx, weights=y, minlength=nb * n_cars).reshape(nb, n_cars)
    n = np.bincount(idx, minlength=nb * n_cars).reshape(nb, n_cars).astype(float)
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = np.where(n > 0, s / np.maximum(n, 1), 0.0)
    a, b = pairs[:, 0], pairs[:, 1]
    na, nbb = n[:, a], n[:, b]
    w = np.where((na > 0) & (nbb > 0), na * nbb / np.maximum(na + nbb, 1), 0.0)
    ws = w.sum(axis=0)
    d = (w * (mean[:, a] - mean[:, b])).sum(axis=0) / np.where(ws > 0, ws, 1)
    return d, ws


def adjusted_test(y, car, ir, n_cars, n_perm=N_PERM, seed=SEED) -> dict:
    """The iRating-adjusted comparison for drivers with an iRating. y: race-relative pace (s); car: 0..n_cars-1.
    Returns {"slope_per_1000", "pairs": [{"a", "b", "gap", "z", "p", "weight"}]} (gap = a - b, negative: a quicker).

    The statistic: take off a lap-time slope per iRating point (Theil-Sen from pairs in the same car), then per pair
    of cars the band-weighted mean gap, as a z score. The null ("the car makes no difference"): Freedman-Lane
    shuffles. Fit the model without cars (one slope over everyone, band medians), shuffle its leftovers inside each
    band, add them back and work the statistic out again with the real car labels, so the link between car and
    iRating stays as it is in the field. p is family-wise: the biggest |z| over every pair in each shuffle."""
    y, car, ir = np.asarray(y, float), np.asarray(car, int), np.asarray(ir, float)
    band = np.array([band_of(v) for v in ir], int)
    pairs = np.array([(i, j) for i in range(n_cars) for j in range(i + 1, n_cars)], int).reshape(-1, 2)
    dx = ir[:, None] - ir[None, :]
    far = np.triu(np.abs(dx) >= MIN_SLOPE_DIR, 1)
    same = far & (car[:, None] == car[None, :])
    dxs = np.where(far, dx, 1.0)
    irc = ir - np.median(ir)
    cells = band * n_cars + car
    ucells = np.unique(cells)
    ubands = np.unique(band)

    def slope_of(yv, mask):
        if not mask.any():
            return 0.0
        return float(np.median(((yv[:, None] - yv[None, :]) / dxs)[mask]))

    def stats(yv):
        slope = slope_of(yv, same)
        adj = yv - slope * irc
        d, ws = _pair_stats(adj, car, band, n_cars, pairs)
        r = adj.copy()
        for k in ucells:
            m = cells == k
            r[m] -= np.median(adj[m])
        sc = max(1.4826 * float(np.median(np.abs(r))), 1e-6)
        return slope, d, ws, np.where(ws > 0, d * np.sqrt(ws) / sc, 0.0)

    slope, d, ws, z = stats(y)
    out = {"slope_per_1000": round(slope * 1000, 3), "pairs": []}
    if not len(pairs):
        return out
    ok = ws > 0
    # the model without cars
    b0 = slope_of(y, far)
    fitted = b0 * irc
    for k in ubands:
        m = band == k
        fitted[m] += np.median(y[m] - b0 * irc[m])
    e = y - fitted
    rng = np.random.default_rng(seed)
    groups = [np.nonzero(band == k)[0] for k in ubands]
    exceed = np.zeros(len(pairs))
    es = e.copy()
    for _ in range(int(n_perm)):
        for gi in groups:
            es[gi] = e[rng.permutation(gi)]
        zp = stats(fitted + es)[3]
        exceed += float(np.max(np.abs(zp))) >= np.abs(z) - 1e-12
    p = (exceed + 1) / (n_perm + 1)
    for k, (a, b) in enumerate(pairs):
        out["pairs"].append({"a": int(a), "b": int(b), "gap": round(float(d[k]), 3) if ok[k] else None,
                             "z": round(float(z[k]), 2), "p": round(float(p[k]), 4) if ok[k] else None,
                             "weight": round(float(ws[k]), 2)})
    return out


def _fmt_s(x) -> str:
    return f"{abs(x):.1f} s" if abs(x) >= 0.095 else f"{abs(x):.2f} s"


def _drivers(races, track, cls, cars_only=None):
    """One row per driver and car: race-relative pace averaged over their races, best lap, iRating."""
    _tracks(races)
    names = _Names(_same_car)
    from .results_csv import clean_name
    rows, counts = {}, {"races": 0, "results_races": 0, "scan_races": 0, "left_out": 0}
    for r in races:
        if r["_track"] != track:
            continue
        ds = [d for d in r["drivers"] if d["class"] == cls]
        if cars_only:
            ds = [d for d in ds if any(_same_car(d["car"], c) for c in cars_only)]
        if not ds:
            continue
        counts["races"] += 1
        counts["results_races" if r["source"] == "results" else "scan_races"] += 1
        good = [d for d in ds if d["finished"] and d["pace"] and (d.get("laps") is None or d["laps"] >= MIN_LAPS)]
        if not good:
            counts["left_out"] += len(ds)
            continue
        med = float(np.median([d["pace"] for d in good]))
        good = [d for d in good if d["pace"] <= OUTLIER * med]
        counts["left_out"] += len(ds) - len(good)
        med = float(np.median([d["pace"] for d in good]))
        for d in good:
            car = names.key(d["car"])
            k = (" ".join(clean_name(d["name"]).lower().split()), car)
            row = rows.setdefault(k, {"name": clean_name(d["name"]), "car": car, "deltas": [], "paces": [], "bests": [], "irs": []})
            row["deltas"].append(d["pace"] - med)
            row["paces"].append(d["pace"])
            if d.get("best"):
                row["bests"].append(d["best"])
            if d.get("irating"):
                row["irs"].append(d["irating"])
    out = []
    for row in rows.values():
        out.append({"name": row["name"], "car": row["car"], "delta": float(np.mean(row["deltas"])),
                    "pace": float(np.median(row["paces"])), "best": min(row["bests"]) if row["bests"] else None,
                    "irating": float(np.mean(row["irs"])) if row["irs"] else None, "races": len(row["deltas"])})
    return out, counts


def compare(races, track, cls, me=None, cars=None, n_perm=N_PERM, seed=SEED) -> dict:
    """The class table and the verdicts for one track and class."""
    rows, counts = _drivers(races, track, cls, cars)
    cars_list = sorted({r["car"] for r in rows})
    out = {"track": track, "class": cls, "cars": [], "verdicts": [], "pairs": [], "counts": counts,
           "drivers": len(rows), "with_irating": sum(1 for r in rows if r["irating"]), "slope_per_1000": None,
           "reference": None, "enough": False, "summary": ""}
    if not rows:
        out["summary"] = "No drivers in this class at this track yet."
        return out
    rated = [r for r in rows if r["irating"]]
    by = {c: [r for r in rows if r["car"] == c] for c in cars_list}
    rated_by = {c: [r for r in rated if r["car"] == c] for c in cars_list}
    test = None
    tested = [c for c in cars_list if len(rated_by[c]) >= 2]
    if len(tested) >= 2:
        ix = {c: k for k, c in enumerate(tested)}
        sub = [r for r in rated if r["car"] in ix]
        test = adjusted_test([r["delta"] for r in sub], [ix[r["car"]] for r in sub], [r["irating"] for r in sub],
                             len(tested), n_perm=n_perm, seed=seed)
        out["slope_per_1000"] = test["slope_per_1000"]
    ref = max(tested, key=lambda c: (len(rated_by[c]), c)) if test else None
    out["reference"] = ref
    pair_of = {}
    if test:
        for p in test["pairs"]:
            a, b = tested[p["a"]], tested[p["b"]]
            pair_of[(a, b)] = p
            pair_of[(b, a)] = dict(p, gap=-p["gap"] if p["gap"] is not None else None)
    for c in cars_list:
        rs = by[c]
        n_r = len(rated_by[c])
        row = {"car": c, "drivers": len(rs), "with_irating": n_r, "races": sum(r["races"] for r in rs),
               "median": round(float(np.median([r["pace"] for r in rs])), 3),
               "best": round(min(r["best"] for r in rs if r["best"]), 3) if any(r["best"] for r in rs) else None,
               "gap": None, "p": None, "confidence": "", "reference": c == ref}
        if c == ref:
            row["confidence"] = "the car the others are measured against"
        elif ref and (c, ref) in pair_of and pair_of[(c, ref)]["gap"] is not None:
            p = pair_of[(c, ref)]
            row["gap"], row["p"] = p["gap"], p["p"]
            if min(n_r, len(rated_by[ref])) < MIN_DRIVERS:
                row["confidence"] = "not enough to tell (too few drivers)"
            elif p["p"] < ALPHA:
                row["confidence"] = "clear"
            else:
                row["confidence"] = "not enough to tell"
        else:
            row["confidence"] = "no iRatings to compare" if not n_r else "not enough to tell (too few drivers)"
        out["cars"].append(row)
    out["cars"].sort(key=lambda r: (r["gap"] if r["gap"] is not None else (0 if r["reference"] else 99), r["median"]))
    for (a, b), p in pair_of.items():
        if a < b:
            out["pairs"].append({"a": a, "b": b, "gap": p["gap"], "p": p["p"], "drivers_a": len(rated_by[a]),
                                 "drivers_b": len(rated_by[b])})
            enough = min(len(rated_by[a]), len(rated_by[b])) >= MIN_DRIVERS
            if enough and p["p"] is not None and p["p"] < ALPHA and p["gap"]:
                fast, slow, g = (a, b, -p["gap"]) if p["gap"] < 0 else (b, a, p["gap"])
                nf, ns = len(rated_by[fast]), len(rated_by[slow])
                out["verdicts"].append({"fast": fast, "slow": slow, "gap": round(g, 3), "p": p["p"],
                                        "text": f"The {fast} is about {_fmt_s(g)} a lap quicker than the {slow} here for "
                                                f"drivers of similar iRating ({nf} and {ns} drivers)."})
    out["verdicts"].sort(key=lambda v: -v["gap"])
    out["enough"] = bool(out["verdicts"])
    if len(cars_list) < 2:
        out["summary"] = f"Only one car ({cars_list[0]}) in this class here so far, so there's nothing to compare."
    elif out["verdicts"]:
        out["summary"] = " ".join(v["text"] for v in out["verdicts"])
    elif not rated:
        out["summary"] = ("Not enough to tell: these drivers' iRatings aren't known (replay scans don't have them), so a "
                          "car can't be told apart from the people driving it. Add the iRacing results file to the race "
                          "reviews.")
    else:
        few = [c for c in cars_list if len(rated_by[c]) < MIN_DRIVERS]
        out["summary"] = ("Not enough to tell which car is quicker for similar drivers. "
                          + (f"Each car needs at least {MIN_DRIVERS} drivers with an iRating; "
                             + ", ".join(f"the {c} has {len(rated_by[c])}" for c in few) + "." if few else
                             "The gaps are within what the drivers' own spread could give by chance."))
    if me:
        out["you"] = you_vs_field(races, track, None, me, rows=rows)
    return out


def you_vs_field(races, track, car, me, rows=None, cls=None) -> dict | None:
    """His race pace against drivers in the same car and iRating band: the share of them he was quicker than."""
    from .results_csv import same_name
    if rows is None:
        rows, _c = _drivers(races, track, cls) if cls else ([], None)
    mine = [r for r in rows if any(same_name(r["name"], m) for m in (me if isinstance(me, (list, tuple)) else [me]) if m)]
    if car:
        mine = [r for r in mine if _same_car(r["car"], car)]
    if not mine:
        return None
    out = []
    for m in mine:
        b = band_of(m["irating"])
        others = [r for r in rows if r["car"] == m["car"] and r is not m
                  and (b is None or band_of(r["irating"]) == b)]
        item = {"car": m["car"], "races": m["races"], "band": BANDS[b][2] if b is not None else None,
                "others": len(others), "pace": round(m["pace"], 3), "delta": round(m["delta"], 3), "percentile": None}
        where = f"drivers in the {m['car']}" + (f" rated {BANDS[b][2]}" if b is not None else "")
        if len(others) >= YOU_MIN:
            slower = sum(1 for r in others if r["delta"] > m["delta"]) + 0.5 * sum(1 for r in others if r["delta"] == m["delta"])
            pct = round(100 * slower / len(others))
            item["percentile"] = pct
            item["text"] = (f"Your race pace was quicker than {pct}% of {where} ({len(others)} drivers, "
                            f"{m['races']} race{'s' if m['races'] != 1 else ''} of yours).")
        else:
            item["text"] = (f"Only {len(others)} other {where} so far: too few to place you "
                            f"(at least {YOU_MIN} are needed).")
        out.append(item)
    return {"items": out}


# ---------------------------------------------------------------- telemetry: where each car is faster
def _segments(R0, corners):
    """Corner zones and the straights between them (metres on the lap)."""
    L = float(R0["d"][-1] + 5.0)
    segs = [{"kind": "corner", "c": c, "name": f"Turn {k + 1}", "a": float(c["start_m"]), "b": float(c["end_m"])}
            for k, c in enumerate(corners)]
    out = []
    for k, s in enumerate(segs):
        out.append(s)
        nxt = segs[(k + 1) % len(segs)]
        a, b = s["b"], nxt["a"] + (L if k + 1 == len(segs) else 0.0)
        if b - a >= MIN_STRAIGHT_M:
            out.append({"kind": "straight", "name": f"Straight after Turn {k + 1}", "a": a, "b": b})
    return out


def _seg_numbers(lap, seg):
    from . import cornercoach as cc
    R = lap["R"]
    if seg["kind"] == "corner":
        ph = cc.phases(R, seg["c"])
        if not ph:
            return None
        return {"t": ph.get("t"), "brake_m": ph.get("brake_m"), "min_kmh": ph.get("min_kmh"), "exit_kmh": ph.get("exit_kmh")}
    X = cc._loop(R)
    d = X["d"]
    m = (d >= seg["a"]) & (d <= seg["b"])
    if m.sum() < 3:
        return None
    t = float(np.interp(seg["b"], d, X["tt"]) - np.interp(seg["a"], d, X["tt"]))
    return {"t": t, "top_kmh": cc._kmh(float(X["v"][m].max()))}


def where_faster(laps_a, laps_b, name_a="Car A", name_b="Car B") -> dict:
    """Per corner and straight: time, brake point, slowest speed, exit speed, top speed, from each car's clean
    laps (cornercoach.load_laps shape). A difference is reported only when it is bigger than the lap-to-lap spread
    of both cars (each needs WF_MIN_LAPS clean laps); with fewer laps it's a rough look with fixed floors."""
    from . import engine
    out = {"a": name_a, "b": name_b, "laps_a": len(laps_a or []), "laps_b": len(laps_b or []), "rough": False,
           "rows": [], "lap": None, "note": ""}
    if not laps_a or not laps_b:
        out["note"] = "Needs a clean lap in each car at this track."
        return out
    rough = min(len(laps_a), len(laps_b)) < WF_MIN_LAPS
    out["rough"] = rough
    fastest = min(laps_a + laps_b, key=lambda x: x["time"])
    try:
        corners = engine.detect_corners(fastest["R"], L=float(fastest["R"]["d"][-1] + 5.0))
    except Exception:  # noqa: BLE001
        corners = []
    ta, tb = [x["time"] for x in laps_a], [x["time"] for x in laps_b]
    out["lap"] = {"a_best": round(min(ta), 3), "b_best": round(min(tb), 3), "a_median": round(float(np.median(ta)), 3),
                  "b_median": round(float(np.median(tb)), 3)}
    if not corners:
        out["note"] = "No corners could be found on these laps."
        return out
    for seg in _segments(fastest["R"], corners):
        va = [n for n in (_seg_numbers(x, seg) for x in laps_a) if n]
        vb = [n for n in (_seg_numbers(x, seg) for x in laps_b) if n]
        if not va or not vb:
            continue
        found = {}
        for key in ("t", "brake_m", "min_kmh", "exit_kmh", "top_kmh"):
            xa = [n[key] for n in va if n.get(key) is not None]
            xb = [n[key] for n in vb if n.get(key) is not None]
            if not xa or not xb:
                continue
            ma, mb = float(np.median(xa)), float(np.median(xb))
            if rough:
                bar = ROUGH_FLOOR[key]
            else:
                sa = float(np.std(xa, ddof=1))        # the plain spread: laps of two kinds both count
                sb = float(np.std(xb, ddof=1))
                bar = max(sa, sb, NOISE_FLOOR[key])
            if abs(ma - mb) > bar:
                found[key] = {"a": round(ma, 2), "b": round(mb, 2), "diff": round(ma - mb, 2), "spread": round(bar, 2)}
        if found:
            out["rows"].append({"segment": seg["name"], "kind": seg["kind"], "found": found,
                                "text": _wf_text(seg["name"], found, name_a, name_b)})
    if not out["rows"]:
        out["note"] = ("No difference bigger than your own lap-to-lap spread." if not rough else
                       "No big difference on these laps.")
    return out


def _wf_text(name, f, a, b) -> str:
    bits = []
    if "t" in f:
        d = f["t"]["diff"]
        bits.append(f"the {a if d < 0 else b} is {_fmt_s(d)} quicker through it")
    if "brake_m" in f:
        d = f["brake_m"]["diff"]          # metres from the apex: more negative = earlier
        bits.append(f"you brake about {abs(round(d))} m {'earlier' if d < 0 else 'later'} in the {a}")
    for key, word in (("min_kmh", "slowest point"), ("exit_kmh", "exit"), ("top_kmh", "top speed")):
        if key in f:
            d = f[key]["diff"]
            bits.append(f"the {a if d > 0 else b} is {abs(d):.0f} km/h faster at the {word}")
    return f"{name}: " + "; ".join(bits) + "."


# ---------------------------------------------------------------- the Prepare question
def season_pick(sched, races, series_id, week=None) -> dict | None:
    """Which of a series' cars has been quickest at that week's track. None without the series or the week."""
    from . import schedule
    ser = schedule.find_series(sched, series_id)
    if not ser:
        return None
    w = next((x for x in ser.get("weeks") or [] if week is None or x.get("week") == week), None)
    if not w:
        return None
    cars = schedule.week_cars(ser, w)
    t = _tracks(races)
    track = next((n for n in t.names if _same_track(n, w["track"])), None)
    out = {"series": ser.get("name"), "week": w.get("week"), "track": w["track"], "pick": None, "confidence": "none",
           "text": ""}
    if not track:
        out["text"] = "No races with car names at this track yet."
        return out
    classes = {class_key(c) for c in cars}
    best = None
    for cls in classes:
        res = compare(races, track, cls, cars=cars)
        if res["verdicts"]:
            v = res["verdicts"][0]
            best = {"pick": v["fast"], "confidence": "clear", "text": v["text"], "class": cls}
            break
        if len(res["cars"]) >= 2 and best is None:
            quick = min(res["cars"], key=lambda r: r["median"])
            best = {"pick": None, "confidence": "not enough to tell", "class": cls,
                    "text": f"Not enough to tell. On the raw numbers the {quick['car']} has the quickest typical lap "
                            f"({res['drivers']} drivers), but that isn't adjusted for who drove it."}
    out.update(best or {"text": "Not enough cars of this series here yet."})
    return out
