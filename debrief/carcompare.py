"""Car vs car (2.18): which car of a class is quicker at a track, judged so a car isn't judged by its drivers.

Where the numbers come from (all read only, nothing is fetched):
  - iRacing's results file (the CSV he adds to a race review; since 2.18 the parsed table is kept beside the review
    as field.json when he presses Save): every driver's car, class, iRating, average and fastest lap, laps and
    whether they finished, plus the race's track, subsession and date from the file's header;
  - replay scans (Debrief's own and RaceCap's) whose drivers carry a car name (scans made since the scan learned the
    car): every driver's lap times, but no iRating, so those drivers count in the raw numbers only;
  - telemetry laps in two cars of the class at the track (his own files, or a teammate's lap kept on the Coach
    page): where he was faster in each car.

Public API (JSON friendly, nothing raises for bad input):
    collect(library_folder, racecap_folder=None)            -> {"races": [...], "notes": [...]}
    options(races)                                           -> [{"track", "class", "cars", "drivers"}]
    compare(races, track, cls, me=None, cars=None, period=)  -> the class table, verdicts and how it was worked out
    you_vs_field(races, track, car, me)                      -> his race pace against drivers in the same car and band
    where_faster(laps_a, laps_b, name_a, name_b)             -> corner and straight differences from telemetry laps
    pick_period(races, track, cls, mode)                     -> the races of one season (one BoP period), or all

Honesty rules:
  - pace is a driver's average race lap from the results (the median lap in a scan). Counted only: a driver whose
    status is "Running" and who completed at least FINISH_SHARE of the class winner's laps, MIN_LAPS laps or more,
    and an average no slower than OUTLIER x that race's class median;
  - one model for every driver in every race: pace = race + car + slope x iRating + iRating band (a robust fit,
    Huber weights), so a race full of one car can't make that car look slow or quick: the race's own speed is
    worked out together with the cars, from the drivers who raced each other;
  - a sentence saying one car is quicker needs the permutation test (Freedman-Lane, see adjusted_test): the model
    without cars is fitted, each driver's share of what it leaves over is shuffled between drivers of the same
    iRating band (N_PERM shuffles, fixed seed), and the gap must beat the biggest gap between ANY two cars in all
    but ALPHA of the shuffles; and MIN_DRIVERS drivers with an iRating in each car, in iRating bands both cars have.
    Otherwise the numbers are shown with "not enough to tell". Sample sizes are always shown;
  - by default only one season's races are pooled (iRacing changes the cars' balance between seasons).
"""
from __future__ import annotations

import json
import math
import re
from datetime import date, timedelta
from itertools import combinations
from pathlib import Path

import numpy as np

VERSION = 2
FIELD_FILE = "field.json"           # beside review.json: the parsed results table (server.py writes it on Save)
BANDS = ((0, 1500, "under 1500"), (1500, 2500, "1500 to 2500"), (2500, 3500, "2500 to 3500"), (3500, 10 ** 6, "over 3500"))
MIN_DRIVERS = 15                    # drivers with an iRating in each car (in shared bands) before a "quicker" sentence
MIN_LAPS = 3
FINISH_SHARE = 0.90                 # laps completed against the class winner's before a driver counts as a finisher
OUTLIER = 1.07                      # average lap over 107 % of the race's class median: not counted
N_PERM = 1000
SEED = 218
ALPHA = 0.05                        # "unlikely to be chance"
LIKELY = 0.20                       # "likely": chance would give a gap this big less than 1 time in 5
HUBER = 1.345
FIT_ITERS = 3                       # one plain fit, then two re-weightings
YOU_MIN = 5                         # drivers to compare with before a percentile
SEASON_WEEKS = 13                   # without a schedule: the 13 weeks up to the newest race
CAVEAT = "Car choice and driver skill can't be fully separated."
# telemetry
WF_MIN_LAPS = 5                     # laps in each car before "is quicker"; fewer is a rough look
WF_ROUGH_TOP = 3                    # a rough look shows at most this many places
WF_PERMS = 2000
WF_MIN_VALUES = 3                   # laps with a number in each car before a measure is tested
ROUGH_FLOOR = {"t": 0.10, "min_kmh": 3.0, "exit_kmh": 3.0, "top_kmh": 3.0, "brake_m": 10.0}
NOISE_FLOOR = {"t": 0.02, "min_kmh": 0.5, "exit_kmh": 0.5, "top_kmh": 0.5, "brake_m": 6.0}   # brake points sit on a 5 m grid
MIN_STRAIGHT_M = 120
WF_KEYS = ("t", "brake_m", "min_kmh", "exit_kmh", "top_kmh")


# ---------------------------------------------------------------- names
def class_key(car=None, cls=None) -> str:
    """'GT4' for 'GT4 Class', 'gt4' or a GT4 car name. The car's name wins (iRacing's offline class is the car)."""
    from .schedule import class_of
    k = class_of(car) if car else None
    if k:
        return k
    s = re.sub(r"\bclass\b", "", str(cls or ""), flags=re.I).strip()
    return class_of(s) or s.upper() or "Unknown class"


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
    """The same venue AND the same layout (strict: 'Grand Prix' is not 'Short')."""
    from .schedule import same_track
    return same_track(a, b)


def _same_car(a, b):
    return car_key(a) == car_key(b)


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _day(s) -> date | None:
    try:
        return date.fromisoformat(str(s or "").strip()[:10])
    except ValueError:
        return None


# ---------------------------------------------------------------- collecting races
def _finishers(drivers, laps_of, running_of):
    """Who counts as a finisher: status Running and FINISH_SHARE of the class winner's laps."""
    best = {}
    for d in drivers:
        n = laps_of(d)
        if n is not None:
            best[d["class"]] = max(best.get(d["class"], 0), n)
    for d in drivers:
        n, top = laps_of(d), best.get(d["class"])
        d["finished"] = bool(running_of(d) and n is not None and top and n >= FINISH_SHARE * top)


def _race_from_field(doc, label) -> dict | None:
    if not isinstance(doc, dict) or not isinstance(doc.get("drivers"), list):
        return None
    drivers = []
    for d in doc["drivers"]:
        if not isinstance(d, dict) or not d.get("model"):
            continue
        f = d.get("fields") or {}
        lap = d.get("lap") or {}
        out = d.get("out")
        status = str(out if out is not None else f.get("finish_status") or "").strip().lower()
        cid = str(d.get("cust_id") or "").strip()
        drivers.append({"name": str(d.get("name") or ""), "id": cid if re.fullmatch(r"-?\d{1,12}", cid) else "",
                        "car": str(d["model"]), "class": class_key(d["model"], d.get("class")),
                        "irating": _num(f.get("irating_before")), "pace": _num(lap.get("avg")), "best": _num(lap.get("best")),
                        "laps": lap.get("laps") if isinstance(lap.get("laps"), int) else None, "_running": status == "running"})
    if not drivers:
        return None
    _finishers(drivers, lambda d: d["laps"], lambda d: d["_running"])
    for d in drivers:
        d.pop("_running")
    race = doc.get("race") or {}
    return {"source": "results", "id": str(race.get("subsession") or doc.get("subsession") or label),
            "track": str(doc.get("track") or race.get("track") or ""), "when": str(doc.get("when") or race.get("when") or ""),
            "drivers": drivers}


def _race_from_scan(scan, label) -> dict | None:
    drivers = []
    for c in scan.get("cars") or []:
        if not isinstance(c, dict) or not c.get("car"):
            continue
        laps = [_num(v) for v in (c.get("lap_times") or {}).values()]
        laps = [x for x in laps if x and x > 0]
        best = _num((c.get("best_lap") or {}).get("time"))
        stopped = c.get("stopped_racing") or c.get("ended") or c.get("never_on_track")
        n = c.get("laps_completed")
        drivers.append({"name": str(c.get("name") or ""), "id": "", "car": str(c["car"]), "class": class_key(c["car"]),
                        "irating": None, "pace": float(np.median(laps)) if laps else None, "best": best,
                        "laps": n if isinstance(n, int) else None, "_running": not stopped})
    if not drivers:
        return None
    _finishers(drivers, lambda d: d["laps"], lambda d: d["_running"])
    for d in drivers:
        d.pop("_running")
    return {"source": "scan", "id": str(scan.get("subsession") or label), "track": str(scan.get("track") or ""),
            "when": "", "drivers": drivers}


def _review_links(lib: Path) -> dict:
    """{review folder name: {"subsession", "track", "when"}} from the reviews' own files (read only when needed)."""
    out = {}
    try:
        files = sorted((lib / "reviews").glob("*/review.json"))
    except OSError:
        return out
    for f in files:
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict):
            continue
        meta = doc.get("meta") or {}
        sub = str((doc.get("racecap") or {}).get("subsession") or "")
        out[f.parent.name] = {"subsession": sub, "track": str(meta.get("track") or ""), "when": str(meta.get("started_at") or "")}
    return out


def collect(library_folder=None, racecap_folder=None) -> dict:
    """Every race with car names, one entry per subsession: saved results tables first, then replay scans of races
    without one. A scan's track name has no layout, so it takes the layout from the review of the same race."""
    races, notes, seen = [], [], set()
    no_cars = 0
    lib = Path(str(library_folder)) if library_folder else None
    links = None
    if lib is not None:
        try:
            files = sorted((lib / "reviews").glob("*/" + FIELD_FILE))
        except OSError:
            files = []
        for f in files:
            try:
                doc = json.loads(f.read_text(encoding="utf-8"))
                r = _race_from_field(doc, f.parent.name)
            except (OSError, ValueError):
                r = None
            if r is None:
                continue
            if r["id"] == f.parent.name:             # an older table without the race's id: the review knows it
                links = _review_links(lib) if links is None else links
                sub = (links.get(f.parent.name) or {}).get("subsession")
                if sub:
                    r["id"] = sub
            if r["id"] in seen:
                continue
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
        by_sub = None
        for root in roots:
            if not Path(root).is_dir():
                continue
            for sub, _slug, d in racecap_link.race_dirs(Path(root)):
                if str(sub) in seen:
                    continue
                scan = racecap_link.read_scan(d)
                if not scan:
                    continue
                r = _race_from_scan(scan, str(sub))
                if r is None:
                    no_cars += 1
                    continue
                if r["id"] in seen:
                    continue
                if by_sub is None:
                    links = _review_links(lib) if (links is None and lib is not None) else (links or {})
                    by_sub = {v["subsession"]: v for v in links.values() if v.get("subsession")}
                rv = by_sub.get(r["id"]) or by_sub.get(str(sub))
                if rv:
                    if rv.get("track") and _same_venue(rv["track"], r["track"]):
                        r["track"] = rv["track"]          # the layout comes from the review
                    r["when"] = rv.get("when") or ""
                seen.update({r["id"], str(sub)})
                races.append(r)
    except Exception:  # noqa: BLE001
        notes.append("Some replay scans couldn't be read.")
    if no_cars:
        notes.append(f"{no_cars} replay scan{'s' if no_cars != 1 else ''} without car names (made before Debrief "
                     "read them); add the iRacing results file to that race's review instead.")
    return {"races": races, "notes": notes}


def _same_venue(a, b) -> bool:
    from .schedule import split_track
    return _same_track(split_track(a)[0], split_track(b)[0]) or _same_track(a, b)


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


# ---------------------------------------------------------------- one season at a time
def seasons() -> list:
    """[(name, first day, day after the last week)] from the saved and bundled iRacing schedules."""
    out = []
    try:
        from . import schedule
        have = set()
        for s in schedule.saved() + schedule._bundled():
            name = s.get("season")
            if name in have:
                continue
            have.add(name)
            a, b = schedule.season_range(s)
            if a and b:
                out.append((str(name or "Season"), a, b))
    except Exception:  # noqa: BLE001
        pass
    return out


def pick_period(races, track, cls, mode="season", season_list=None) -> tuple:
    """(races, info). mode "season": only the races at this track and class in the iRacing season of the newest one
    (from the schedule; without one, the SEASON_WEEKS weeks up to it), so a balance change between seasons can't
    mix two different cars. Races without a date are left out then. mode "all": every race."""
    _tracks(races)
    here = [r for r in races if r["_track"] == track and any(d["class"] == cls for d in r["drivers"])]
    info = {"mode": "all", "label": "all races", "from": None, "to": None, "races_all": len(here), "races": len(here),
            "undated": sum(1 for r in here if not _day(r.get("when")))}
    dated = [_day(r.get("when")) for r in here if _day(r.get("when"))]
    if mode != "season" or not dated:
        if mode == "season":
            info["label"] = "all races (their dates aren't known)"
        return races, info
    newest = max(dated)
    win = next(((n, a, b) for n, a, b in (season_list if season_list is not None else seasons()) if a <= newest < b), None)
    if win:
        name, a, b = win
        label = f"{name} ({_fmt_day(a)} to {_fmt_day(b - timedelta(days=1))})"
    else:
        a, b = newest - timedelta(weeks=SEASON_WEEKS) + timedelta(days=1), newest + timedelta(days=1)
        label = f"the {SEASON_WEEKS} weeks to {_fmt_day(newest)}"
    here_ids = {id(r) for r in here}
    keep_ids = {id(r) for r in here if _day(r.get("when")) and a <= _day(r.get("when")) < b}
    kept = [r for r in races if id(r) in keep_ids or id(r) not in here_ids]
    info.update(mode="season", label=label, **{"from": a.isoformat(), "to": (b - timedelta(days=1)).isoformat()},
                races=len(keep_ids))
    return kept, info


def _fmt_day(d: date) -> str:
    return f"{d.day} {d.strftime('%b')} {d.year}"


# ---------------------------------------------------------------- the statistics
def band_of(ir) -> int | None:
    if ir is None:
        return None
    for k, (a, b, _l) in enumerate(BANDS):
        if a <= ir < b:
            return k
    return None


def _fit(X, y, iters=FIT_ITERS):
    """A robust least-squares fit (Huber weights). (coefficients, leftovers)."""
    w = np.ones(len(y))
    coef = np.zeros(X.shape[1])
    for it in range(iters):
        sw = np.sqrt(w)
        coef = np.linalg.lstsq(X * sw[:, None], y * sw, rcond=None)[0]
        r = y - X @ coef
        if it == iters - 1:
            return coef, r
        s = max(1.4826 * float(np.median(np.abs(r - np.median(r)))), 1e-6)
        u = np.abs(r) / (HUBER * s)
        w = np.where(u <= 1, 1.0, 1.0 / np.maximum(u, 1e-12))
    return coef, y - X @ coef


def _design(race, x, band, car, n_races, n_cars, with_cars):
    """Columns: one per race, one per iRating band (but the lowest one there), the iRating slope, one per car (but the
    first). Every race has its own level, so the race's speed is fitted together with the cars."""
    cols = [np.eye(n_races)[race]]
    ub = sorted(set(band.tolist()))
    for b in ub[1:]:
        cols.append((band == b).astype(float)[:, None])
    cols.append(x[:, None])
    k_slope = sum(c.shape[1] for c in cols) - 1
    k_car = None
    if with_cars and n_cars > 1:
        k_car = k_slope + 1
        cols.append(np.eye(n_cars)[car][:, 1:])
    return np.hstack(cols), k_slope, k_car


def adjusted_test(y, car, ir, race, driver, n_cars, n_perm=N_PERM, seed=SEED) -> dict:
    """The iRating-adjusted comparison, one row per driver per race (drivers with an iRating). y: average lap (s);
    car: 0..n_cars-1; race: the race's number; driver: the driver's number (one per person and car).
    Returns {"slope_per_1000", "pairs": [{"a", "b", "gap", "z", "p", "weight", "n_a", "n_b"}]} (gap = a - b,
    negative: a quicker; None when the two cars never met in a way that tells them apart).

    The model: pace = race + iRating band + slope x iRating + car, fitted robustly. The statistic per pair of cars:
    the car gap over its spread (the spread of drivers' own leftovers, for that pair's driver counts). The null
    ("the car makes no difference"): Freedman-Lane. Fit the model without cars, split what it leaves over into
    each driver's average and the race-to-race rest, shuffle the drivers' averages between drivers of the same
    iRating band, add back and fit again with the real car labels, so the link between car, iRating and the races
    stays as it is in the field. p is family-wise: the biggest |z| over every pair in each shuffle."""
    y, car, ir = np.asarray(y, float), np.asarray(car, int), np.asarray(ir, float)
    race, driver = np.asarray(race, int), np.asarray(driver, int)
    _, race = np.unique(race, return_inverse=True)
    _, driver = np.unique(driver, return_inverse=True)
    n_races, n_drv = int(race.max()) + 1, int(driver.max()) + 1
    band = np.array([band_of(v) for v in ir], int)
    x = (ir - np.median(ir)) / 1000.0
    cnt = np.bincount(driver, minlength=n_drv).astype(float)
    d_ir = np.bincount(driver, weights=ir, minlength=n_drv) / cnt
    d_band = np.array([band_of(v) for v in d_ir], int)
    d_car = np.zeros(n_drv, int)
    d_car[driver] = car
    pairs = [(i, j) for i in range(n_cars) for j in range(i + 1, n_cars)]
    X, k_slope, k_car = _design(race, x, band, car, n_races, n_cars, True)
    X0, _ks0, _ = _design(race, x, band, car, n_races, n_cars, False)
    # which pairs the races can tell apart (the contrast lies in the design's row space)
    proj = np.linalg.pinv(X) @ X
    ok, cvec = [], []
    for a, b in pairs:
        c = np.zeros(X.shape[1])
        if k_car is not None:
            if a:
                c[k_car + a - 1] += 1
            if b:
                c[k_car + b - 1] -= 1
        cvec.append(c)
        ok.append(k_car is not None and bool(np.allclose(c @ proj, c, atol=1e-6)))
    C = np.array(cvec).reshape(len(pairs), X.shape[1])
    ok = np.array(ok, bool)
    # driver counts per pair: drivers in iRating bands both cars have
    na, nb = np.zeros(len(pairs)), np.zeros(len(pairs))
    for k, (a, b) in enumerate(pairs):
        shared = set(d_band[d_car == a].tolist()) & set(d_band[d_car == b].tolist())
        sh = np.isin(d_band, list(shared))
        na[k], nb[k] = np.sum(sh & (d_car == a)), np.sum(sh & (d_car == b))
    denom = np.sqrt(1.0 / np.maximum(na, 1) + 1.0 / np.maximum(nb, 1))
    pa, pb = np.array([a for a, _b in pairs], int), np.array([b for _a, b in pairs], int)
    car_drv = [np.nonzero(d_car == c)[0] for c in range(n_cars)]

    def mad(v):
        return 1.4826 * float(np.median(np.abs(v - np.median(v)))) if len(v) else 0.0

    def stats(yv):
        """Each car's own spread (a car driven by a mixed bunch isn't held to a tidy car's spread), so the gap over
        sqrt(spread_a^2 / n_a + spread_b^2 / n_b), as in Welch's test."""
        coef, r = _fit(X, yv)
        dr = np.bincount(driver, weights=r, minlength=n_drv) / cnt
        pooled = max(mad(dr), 1e-6)
        sc = np.array([max(mad(dr[g]), 0.5 * pooled) if len(g) >= 3 else pooled for g in car_drv])
        se = np.sqrt(sc[pa] ** 2 / np.maximum(na, 1) + sc[pb] ** 2 / np.maximum(nb, 1)) if len(pairs) else np.zeros(0)
        gap = C @ coef
        z = np.where(ok & (na > 0) & (nb > 0), gap / np.maximum(se, 1e-9), 0.0)
        return coef, gap, z

    coef, gap, z = stats(y)
    out = {"slope_per_1000": round(float(coef[k_slope]), 3), "pairs": []}
    if not pairs:
        return out
    c0, e = _fit(X0, y)
    fitted = y - e
    ebar = np.bincount(driver, weights=e, minlength=n_drv) / cnt
    within = e - ebar[driver]
    groups = [np.nonzero(d_band == k)[0] for k in np.unique(d_band)]
    rng = np.random.default_rng(seed)
    exceed = np.zeros(len(pairs))
    perm = np.arange(n_drv)
    testable = ok & (na > 0) & (nb > 0)
    for _ in range(int(n_perm) if testable.any() else 0):
        for g in groups:
            perm[g] = g[rng.permutation(len(g))]
        zp = stats(fitted + ebar[perm][driver] + within)[2]
        exceed += float(np.max(np.abs(zp[testable]))) >= np.abs(z) - 1e-12
    p = (exceed + 1) / (int(n_perm) + 1)
    for k, (a, b) in enumerate(pairs):
        t = bool(testable[k])
        out["pairs"].append({"a": a, "b": b, "gap": round(float(gap[k]), 3) if t else None,
                             "z": round(float(z[k]), 2), "p": round(float(p[k]), 4) if t else None,
                             "weight": round(float(1 / denom[k] ** 2), 2), "n_a": int(na[k]), "n_b": int(nb[k])})
    return out


def _fmt_s(x) -> str:
    return f"{abs(x):.1f} s" if abs(x) >= 0.095 else f"{abs(x):.2f} s"


def _person(d) -> str:
    """Who a driver is: iRacing's customer id when the results file has it (never shown), else the name as written,
    so 'John Smith' and 'John Smith2' (two members) stay two people."""
    if d.get("id"):
        return "id:" + d["id"]
    return "name:" + " ".join(str(d.get("name") or "").lower().split())


def _observations(races, track, cls, cars_only=None):
    """One row per driver per race at this track and class (finishers only), and the counts."""
    _tracks(races)
    names = _Names(_same_car)
    from .results_csv import clean_name
    obs, counts = [], {"races": 0, "results_races": 0, "scan_races": 0, "left_out": 0}
    for ri, r in enumerate(races):
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
        if good:
            med = float(np.median([d["pace"] for d in good]))
            good = [d for d in good if d["pace"] <= OUTLIER * med]
        counts["left_out"] += len(ds) - len(good)
        for d in good:
            obs.append({"race": ri, "person": _person(d), "name": clean_name(d["name"]), "car": names.key(d["car"]),
                        "pace": float(d["pace"]), "best": d.get("best"), "irating": d.get("irating")})
    return obs, counts


def _race_levels(obs):
    """Each race's own speed, fitted together with the cars (no iRating needed): {race: level}."""
    if not obs:
        return {}
    races = sorted({o["race"] for o in obs})
    cars = sorted({o["car"] for o in obs})
    ri = np.array([races.index(o["race"]) for o in obs])
    ci = np.array([cars.index(o["car"]) for o in obs])
    X = np.hstack([np.eye(len(races))[ri], np.eye(len(cars))[ci][:, 1:]])
    coef, _r = _fit(X, np.array([o["pace"] for o in obs]))
    return {r: float(coef[k]) for k, r in enumerate(races)}


def _drivers(races, track, cls, cars_only=None):
    """One row per driver and car: raw pace and best lap, pace against the race's own level, iRating, races."""
    obs, counts = _observations(races, track, cls, cars_only)
    level = _race_levels(obs)
    rows = {}
    for o in obs:
        row = rows.setdefault((o["person"], o["car"]), {"name": o["name"], "person": o["person"], "car": o["car"],
                                                        "deltas": [], "paces": [], "bests": [], "irs": []})
        row["deltas"].append(o["pace"] - level[o["race"]])
        row["paces"].append(o["pace"])
        if o.get("best"):
            row["bests"].append(o["best"])
        if o.get("irating"):
            row["irs"].append(o["irating"])
    out = []
    for row in rows.values():
        out.append({"name": row["name"], "person": row["person"], "car": row["car"], "delta": float(np.mean(row["deltas"])),
                    "pace": float(np.median(row["paces"])), "best": min(row["bests"]) if row["bests"] else None,
                    "irating": float(np.mean(row["irs"])) if row["irs"] else None, "races": len(row["deltas"])})
    return out, counts, obs


def _confidence(p, n_a, n_b) -> str:
    if min(n_a, n_b) < MIN_DRIVERS:
        return "not enough to tell (too few drivers)"
    if p < ALPHA:
        return "unlikely to be chance"
    if p < LIKELY:
        return "likely"
    return "not enough to tell"


def compare(races, track, cls, me=None, cars=None, n_perm=N_PERM, seed=SEED, period="all", season_list=None) -> dict:
    """The class table and the verdicts for one track and class. period "season" pools one season only."""
    races, pinfo = pick_period(races, track, cls, period, season_list)
    rows, counts, obs = _drivers(races, track, cls, cars)
    cars_list = sorted({r["car"] for r in rows})
    out = {"track": track, "class": cls, "cars": [], "verdicts": [], "pairs": [], "counts": counts, "period": pinfo,
           "drivers": len(rows), "with_irating": sum(1 for r in rows if r["irating"]), "slope_per_1000": None,
           "reference": None, "enough": False, "summary": "", "caveat": CAVEAT}
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
        sub = [o for o in obs if o["irating"] and o["car"] in ix]
        dk = {}
        for o in sub:
            dk.setdefault((o["person"], o["car"]), len(dk))
        test = adjusted_test([o["pace"] for o in sub], [ix[o["car"]] for o in sub], [o["irating"] for o in sub],
                             [o["race"] for o in sub], [dk[(o["person"], o["car"])] for o in sub], len(tested),
                             n_perm=n_perm, seed=seed)
        out["slope_per_1000"] = test["slope_per_1000"]
    ref = max(tested, key=lambda c: (len(rated_by[c]), c)) if test else None
    out["reference"] = ref
    pair_of = {}
    if test:
        for p in test["pairs"]:
            a, b = tested[p["a"]], tested[p["b"]]
            pair_of[(a, b)] = p
            pair_of[(b, a)] = dict(p, gap=-p["gap"] if p["gap"] is not None else None, n_a=p["n_b"], n_b=p["n_a"])
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
            row["confidence"] = _confidence(p["p"], p["n_a"], p["n_b"])
        elif ref and (c, ref) in pair_of:
            row["confidence"] = "can't be compared (never in the same races)"
        else:
            row["confidence"] = "no iRatings to compare" if not n_r else "not enough to tell (too few drivers)"
        out["cars"].append(row)
    out["cars"].sort(key=lambda r: (r["gap"] if r["gap"] is not None else (0 if r["reference"] else 99), r["median"]))
    for (a, b), p in pair_of.items():
        if a < b:
            out["pairs"].append({"a": a, "b": b, "gap": p["gap"], "p": p["p"], "drivers_a": p["n_a"], "drivers_b": p["n_b"],
                                 "confidence": _confidence(p["p"], p["n_a"], p["n_b"]) if p["p"] is not None else ""})
            enough = min(p["n_a"], p["n_b"]) >= MIN_DRIVERS
            if enough and p["p"] is not None and p["p"] < ALPHA and p["gap"]:
                fast, slow, g = (a, b, -p["gap"]) if p["gap"] < 0 else (b, a, p["gap"])
                nf, ns = (p["n_a"], p["n_b"]) if fast == a else (p["n_b"], p["n_a"])
                out["verdicts"].append({"fast": fast, "slow": slow, "gap": round(g, 3), "p": p["p"],
                                        "text": f"Drivers of similar iRating were about {_fmt_s(g)} a lap quicker in the "
                                                f"{fast} than in the {slow} ({nf} and {ns} drivers in the same iRating "
                                                f"bands)."})
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
        rows = _drivers(races, track, cls)[0] if cls else []
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


# ---------------------------------------------------------------- telemetry: where he was faster in each car
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


def wf_test(Ma, Mb, floors, n_perm=WF_PERMS, seed=SEED, alpha=ALPHA) -> dict:
    """Which measures differ between two sets of laps. Ma, Mb: laps x measures (NaN: not measured on that lap);
    floors: the smallest difference worth reporting per measure.

    A permutation test with the step-down maxT adjustment (Westfall and Young, the shuffle version of Holm's
    correction), so the chance of even one false row in a whole comparison stays at alpha: whole laps are shuffled
    between the two cars (every split when there are few enough, else n_perm seeded ones), each measure's Welch t
    is worked out again, and a measure counts only when its |t| beats the biggest |t| of the measures not yet
    passed in all but alpha of the shuffles, AND the medians differ by more than its floor.
    Returns {"p": [...], "found": [bool, ...], "diff": [median a - median b, ...]}."""
    Ma, Mb = np.asarray(Ma, float), np.asarray(Mb, float)
    na, m = Ma.shape
    nb = Mb.shape[0]
    floors = np.asarray(floors, float)
    M = np.vstack([Ma, Mb])
    V = ~np.isnan(M)
    M0 = np.where(V, M, 0.0)
    eps = (0.25 * floors) ** 2                         # a measure on a grid (brake points) never has zero spread

    def tstat(G):
        """G: shuffles x laps, 1 for laps counted as car A. Welch t per shuffle and measure."""
        H = 1.0 - G
        ca, cb = G @ V, H @ V
        sa, sb = G @ M0, H @ M0
        qa, qb = G @ (M0 ** 2), H @ (M0 ** 2)
        with np.errstate(invalid="ignore", divide="ignore"):
            ma, mb = sa / ca, sb / cb
            va = (qa - ca * ma ** 2) / np.maximum(ca - 1, 1)
            vb = (qb - cb * mb ** 2) / np.maximum(cb - 1, 1)
            t = (ma - mb) / np.sqrt(np.maximum(va, 0) / ca + np.maximum(vb, 0) / cb + eps * (1 / ca + 1 / cb))
        good = (ca >= WF_MIN_VALUES) & (cb >= WF_MIN_VALUES)
        return np.where(good & np.isfinite(t), t, 0.0)

    g0 = np.zeros((1, na + nb))
    g0[0, :na] = 1
    t0 = np.abs(tstat(g0)[0])
    with np.errstate(all="ignore"):
        diff = np.array([np.nanmedian(Ma[:, k]) - np.nanmedian(Mb[:, k]) if V[:na, k].any() and V[na:, k].any()
                         else np.nan for k in range(m)])
    n = na + nb
    if math.comb(n, na) <= n_perm:
        G = np.zeros((math.comb(n, na), n))
        for r, idx in enumerate(combinations(range(n), na)):
            G[r, list(idx)] = 1
        exact = True
    else:
        rng = np.random.default_rng(seed)
        G = np.zeros((int(n_perm), n))
        for r in range(int(n_perm)):
            G[r, rng.permutation(n)[:na]] = 1
        exact = False
    T = np.abs(tstat(G))
    order = np.argsort(-t0)
    p = np.ones(m)
    prev = 0.0
    # successive maxima over the measures not yet passed (step-down)
    run = np.zeros(len(G))
    tail = np.zeros((len(G), m))
    for j in range(m - 1, -1, -1):
        run = np.maximum(run, T[:, order[j]])
        tail[:, j] = run
    for j, k in enumerate(order):
        if t0[k] == 0:
            continue
        c = float(np.sum(tail[:, j] >= t0[k] - 1e-12))
        pj = c / len(G) if exact else (c + 1) / (len(G) + 1)
        prev = max(prev, pj)
        p[k] = prev
    found = (p <= alpha) & np.isfinite(diff) & (np.abs(np.nan_to_num(diff)) > floors)
    return {"p": p.tolist(), "found": found.tolist(), "diff": diff.tolist()}


def where_faster(laps_a, laps_b, name_a="Car A", name_b="Car B", n_perm=WF_PERMS, seed=SEED) -> dict:
    """Per corner and straight: time, brake point, slowest speed, exit speed, top speed, from each car's clean
    laps (cornercoach.load_laps shape). With WF_MIN_LAPS laps in each car a difference is reported only when a
    shuffle test says it is unlikely to be chance, counting every measure in every corner at once (wf_test); with
    fewer laps it is a rough look: the biggest differences over fixed floors, at most WF_ROUGH_TOP, called
    "possibly"."""
    from . import engine
    out = {"a": name_a, "b": name_b, "laps_a": len(laps_a or []), "laps_b": len(laps_b or []), "rough": False,
           "rows": [], "lap": None, "note": "", "checks": 0}
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
    segs = _segments(fastest["R"], corners)
    checks = []                                # (segment number, measure)
    cols_a, cols_b = [], []
    for si, seg in enumerate(segs):
        va = [_seg_numbers(x, seg) or {} for x in laps_a]
        vb = [_seg_numbers(x, seg) or {} for x in laps_b]
        for key in WF_KEYS:
            xa = [_num(n.get(key)) for n in va]
            xb = [_num(n.get(key)) for n in vb]
            if not any(v is not None for v in xa) or not any(v is not None for v in xb):
                continue
            checks.append((si, key))
            cols_a.append([np.nan if v is None else v for v in xa])
            cols_b.append([np.nan if v is None else v for v in xb])
    out["checks"] = len(checks)
    if not checks:
        out["note"] = "No corners could be measured on these laps."
        return out
    Ma, Mb = np.array(cols_a, float).T, np.array(cols_b, float).T
    found_by = {}
    if rough:
        with np.errstate(all="ignore"):
            diff = np.nanmedian(Ma, axis=0) - np.nanmedian(Mb, axis=0)
        for k, (si, key) in enumerate(checks):
            if np.isfinite(diff[k]) and abs(diff[k]) > ROUGH_FLOOR[key]:
                found_by.setdefault(si, {})[key] = {"a": round(float(np.nanmedian(Ma[:, k])), 2),
                                                    "b": round(float(np.nanmedian(Mb[:, k])), 2),
                                                    "diff": round(float(diff[k]), 2), "size": abs(diff[k]) / ROUGH_FLOOR[key]}
        keep = sorted(found_by, key=lambda si: -max(v["size"] for v in found_by[si].values()))[:WF_ROUGH_TOP]
        found_by = {si: found_by[si] for si in sorted(keep)}
    else:
        res = wf_test(Ma, Mb, [NOISE_FLOOR[key] for _si, key in checks], n_perm=n_perm, seed=seed)
        for k, (si, key) in enumerate(checks):
            if res["found"][k]:
                found_by.setdefault(si, {})[key] = {"a": round(float(np.nanmedian(Ma[:, k])), 2),
                                                    "b": round(float(np.nanmedian(Mb[:, k])), 2),
                                                    "diff": round(float(res["diff"][k]), 2), "p": round(res["p"][k], 4)}
    for si, found in found_by.items():
        for v in found.values():
            v.pop("size", None)
        seg = segs[si]
        out["rows"].append({"segment": seg["name"], "kind": seg["kind"], "found": found,
                            "text": _wf_text(seg["name"], found, name_a, name_b, rough)})
    if not out["rows"]:
        out["note"] = ("No difference that a fair test says is more than chance." if not rough else
                       "No big difference on these laps.")
    return out


def _wf_text(name, f, a, b, rough=False) -> str:
    may = "possibly " if rough else ""
    bits = []
    if "t" in f:
        d = f["t"]["diff"]
        bits.append(f"{may}{_fmt_s(d)} quicker through it in the {a if d < 0 else b}")
    if "brake_m" in f:
        d = f["brake_m"]["diff"]          # metres from the apex: more negative = earlier
        bits.append(f"braking {may}about {abs(round(d))} m {'earlier' if d < 0 else 'later'} in the {a}")
    for key, word in (("min_kmh", "slowest point"), ("exit_kmh", "exit"), ("top_kmh", "top speed")):
        if key in f:
            d = f[key]["diff"]
            bits.append(f"{may}{abs(d):.0f} km/h faster at the {word} in the {a if d > 0 else b}")
    return f"{name}: " + "; ".join(bits) + "."
