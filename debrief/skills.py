"""Skill scores: where in the corner your typical laps lose time to your own better laps (Debrief 2.3).

Works on the review dict alone (``traces`` on the 10 m grid, ``corners[].per_lap``, ``laps``, ``step``,
``L``), so saved reviews get it when the library refreshes.

What it measures: **how close your typical laps (all clean laps) are to your own better laps (your
quickest quarter of clean laps) in each part of the corner.** It is not absolute pace, and it is not
compared with other drivers.

Each corner is split into fixed stretches of track, the same for every lap, from markers taken off the
better laps:  approach [start, B], braking [B, R], entry [R, A], mid [A, P], exit [P, E]
(B brake point, R brake released, A slowest point, P throttle past 50 %, E corner end).

Garage 61 request budget (spec 1.6): technique_plus, skills, session_review, profile, heat map: **0**
(everything comes from data already downloaded or saved). This module never makes a request.
"""
from __future__ import annotations

import math

import numpy as np

VERSION = 1
CAPS_PM = {"braking": 5.0, "entry": 5.0, "mid": 5.0, "exit": 5.0, "overall": 12.0}
PHASES = ("braking", "entry", "mid", "exit")
ALL_PHASES = ("approach",) + PHASES
VERSION_NOTE = """version 1 + 2.3.0 fixes: overall.loss_s is the real median typical-minus-better time over the
corner zones (brake point to corner end, per lap then median; basis "zone_gap"); phases carry "share" of
the summed phase medians ("where it goes"); a phase with no length is None (not measured)."""

MIN_CLEAN = 6          # clean laps needed to score a review
MIN_REF = 3            # better laps needed
MIN_CORNER_LAPS = 5    # typical laps with per_lap data needed for a corner to count
NOISE_S = 0.01         # trace times are rounded to 0.01 s; smaller losses are treated as no loss
BRAKE_OFF_PCT = 5      # brake below this = released
THR_COMMIT_PCT = 50    # throttle at/over this = committed (and the lift threshold for lift corners)
MIN_PHASE_CELLS = 2    # a phase shorter than this many grid cells gets zero length


# ---------------------------------------------------------------- small helpers
def _num(x):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _r(x, nd=3):
    x = _num(x)
    return None if x is None else round(x, nd)


def _median(xs):
    xs = [x for x in (_num(v) for v in xs) if x is not None]
    return float(np.median(xs)) if xs else None


def _iqr(xs):
    xs = [x for x in (_num(v) for v in xs) if x is not None]
    if len(xs) < 2:
        return None
    return float(np.percentile(xs, 75) - np.percentile(xs, 25))


def score(loss_pm, phase: str):
    """100 * max(0, 1 - loss_pm / CAPS_PM[phase]) as an int; None stays None. Reads CAPS_PM at call time,
    so changing the caps retunes every score (profile.build recomputes from the stored loss_pm)."""
    x = _num(loss_pm)
    if x is None:
        return None
    cap = _num(CAPS_PM.get(phase))
    if not cap or cap <= 0:
        return None
    return int(round(100.0 * max(0.0, 1.0 - x / cap)))


def _lap_dicts(data):
    return [l for l in (data.get("laps") or []) if isinstance(l, dict) and _num(l.get("t")) is not None
            and l.get("n") is not None]


def clean_laps(data) -> list:
    """Clean laps (data.laps with clean: true) as [(n, t)] in lap order."""
    return [(int(l["n"]), float(l["t"])) for l in _lap_dicts(data) if l.get("clean")]


def better_laps(data) -> list:
    """Spec 0.4: clean laps at or under the 25th percentile of clean lap times; the 3 quickest clean laps
    if that gives fewer than 3; [] with fewer than 3 clean laps. Lap numbers in lap order."""
    cl = clean_laps(data)
    if len(cl) < 3:
        return []
    p25 = float(np.percentile([t for _, t in cl], 25))
    out = [(t, n) for n, t in cl if t <= p25 + 1e-9]
    if len(out) < 3:
        out = sorted((t, n) for n, t in cl)[:3]
    return sorted(n for _, n in out)


# ---------------------------------------------------------------- traces
class _Trace:
    """One lap's trace on the review grid, with lap-wrap so corner zones may run across the line."""

    def __init__(self, tr, step, L):
        self.step = float(step)
        self.tt = np.asarray([_num(v) if _num(v) is not None else np.nan for v in tr["tt"]], float)
        self.n = len(self.tt)
        T = _num(tr.get("t"))
        if T is None and self.n > 1:
            T = float(self.tt[-1] + (self.tt[-1] - self.tt[-2]))
        self.T = float(T or 0.0)
        self.L = float(L) if _num(L) else self.n * self.step
        self.d = np.arange(self.n) * self.step
        # close the loop: (L, T) after the last cell, so time is defined on all of [0, L]
        if self.L > self.d[-1]:
            self._d = np.append(self.d, self.L)
            self._tt = np.append(self.tt, self.T)
        else:
            self._d, self._tt = self.d, self.tt
        self.b = np.asarray(tr.get("b") or [], float)
        self.th = np.asarray(tr.get("th") or [], float)

    def time_at(self, dist):
        # bounded wrap (no loop): for_review only builds traces with a finite L > 0 and corners within
        # a lap or two of the line, so the quotient is small
        x = float(dist)
        if not (math.isfinite(x) and self.L > 0):
            raise ValueError("bad distance or lap length")
        k = 0.0
        if x < 0 or x > self.L:
            q = math.floor(x / self.L)
            if x - q * self.L == 0 and q > 0:   # exactly on a later line: the end of the previous lap
                q -= 1
            x -= q * self.L
            k = q * self.T
        return float(np.interp(x, self._d, self._tt)) + k

    def cell(self, dist, arr):
        """Channel value at a grid distance (wrapping round the lap)."""
        if len(arr) == 0:
            return None
        i = int(round(float(dist) / self.step)) % len(arr)
        return float(arr[i])


def _usable_trace(tr):
    """A trace with at least 3 cells whose time stamps are all finite numbers (a saved review with junk in
    a trace leaves that lap out rather than putting NaN in the scores)."""
    if not (isinstance(tr, dict) and isinstance(tr.get("tt"), list) and len(tr["tt"]) >= 3):
        return False
    return all(_num(v) is not None and not isinstance(v, bool) for v in tr["tt"])


def _pos(x):
    """A finite number > 0 (bools are not numbers here), else None."""
    if isinstance(x, bool):
        return None
    x = _num(x)
    return x if x is not None and x > 0 else None


def _median_channel(traces, key, cells, step):
    vals = []
    for c in cells:
        xs = [tr.cell(c * step, getattr(tr, key)) for tr in traces]
        xs = [x for x in xs if x is not None and not math.isnan(x)]
        vals.append(float(np.median(xs)) if xs else None)
    return vals


# ---------------------------------------------------------------- markers
def _markers(c, ref_traces, ref_rows, step):
    """Corner markers on the grid (distances in metres) from the better laps, plus lift flag."""
    snap = lambda x: round(float(x) / step) * step  # noqa: E731
    start, end = snap(c["start_m"]), snap(c["end_m"])
    mins = [_num(m.get("min_at")) for m in ref_rows]
    A = _median(mins)
    A = snap(A if A is not None else c.get("apex_m", (start + end) / 2))
    bos = [_num(m.get("brake_on")) for m in ref_rows]
    have = [x for x in bos if x is not None]
    cells = list(range(int(round(start / step)), int(round(end / step)) + 1))
    bmed = dict(zip(cells, _median_channel(ref_traces, "b", cells, step)))
    thmed = dict(zip(cells, _median_channel(ref_traces, "th", cells, step)))
    iA = int(round(A / step))
    lift = not (len(have) * 2 >= len(ref_rows) and have)
    if not lift:
        B = snap(float(np.median(have)))
        iB = int(round(B / step))
        R = None
        for i in cells:
            if i > iB and bmed.get(i) is not None and bmed[i] < BRAKE_OFF_PCT:
                R = i * step
                break
        if R is None:
            R = A
    else:
        # lift corner: the last cell before A where the better laps' throttle drops below 50 %
        B = None
        pre = [i for i in cells if i <= iA]
        for i0, i1 in zip(pre, pre[1:]):
            a, b = thmed.get(i0), thmed.get(i1)
            if a is not None and b is not None and a >= THR_COMMIT_PCT > b:
                B = i1 * step
        if B is None:
            first = thmed.get(cells[0]) if cells else None
            B = start if (first is not None and first < THR_COMMIT_PCT) else A
        R = B
    P = None
    for i in cells:
        if i > iA and thmed.get(i) is not None and thmed[i] >= THR_COMMIT_PCT:
            P = i * step
            break
    if P is None:
        P = end
    # order: start <= B <= R <= A <= P <= E (clamping)
    B = min(max(B, start), end)
    R = min(max(R, B), end)
    A = min(max(A, R), end)
    P = min(max(P, A), end)
    m = [start, B, R, A, P, end]
    # phases shorter than MIN_PHASE_CELLS cells get zero length (the stretch goes to the next phase;
    # a too-short exit goes to mid)
    short = MIN_PHASE_CELLS * step - 1e-6
    for k in range(1, 5):
        if 0 < m[k] - m[k - 1] < short:
            m[k] = m[k - 1]
    if 0 < m[5] - m[4] < short:
        m[4] = m[5]
    return dict(zip(("start", "B", "R", "A", "P", "E"), [float(x) for x in m])), lift


def _phase_bounds(mk):
    return {"approach": (mk["start"], mk["B"]), "braking": (mk["B"], mk["R"]), "entry": (mk["R"], mk["A"]),
            "mid": (mk["A"], mk["P"]), "exit": (mk["P"], mk["E"])}


def _phase_times(tr, bounds):
    return {p: tr.time_at(b) - tr.time_at(a) for p, (a, b) in bounds.items()}


# ---------------------------------------------------------------- main
def _params():
    return {"ref": "p25_clean", "min_clean": MIN_CLEAN, "min_ref": MIN_REF, "min_corner_laps": MIN_CORNER_LAPS,
            "noise_s": NOISE_S, "brake_off_pct": BRAKE_OFF_PCT, "thr_commit_pct": THR_COMMIT_PCT,
            "min_phase_cells": MIN_PHASE_CELLS, "caps_pm": {k: v for k, v in CAPS_PM.items()}}


def _fail(reason, **extra):
    return {"version": VERSION, "params": _params(), "reason": reason, **extra}


def for_review(data: dict) -> dict | None:
    """The `skills` block. Always returns a dict; when it can't score, {"reason": ...} without
    phases/overall/corners. Reasons: no_traces, no_corners, not_enough_clean_laps, not_enough_better_laps,
    bad_data (the saved review has an impossible grid: lap length or step <= 0, NaN/inf, or values that
    can't be read; the library refresh carries on with the next review)."""
    if not isinstance(data, dict):
        return None
    try:
        return _for_review(data)
    except (ValueError, TypeError, OverflowError, ZeroDivisionError, KeyError, IndexError) as e:
        return _fail("bad_data", detail=f"values in the saved review can't be read ({type(e).__name__})")


def _for_review(data: dict) -> dict:
    traces = data.get("traces") if isinstance(data.get("traces"), dict) else {}
    mine = {k: v for k, v in traces.items() if str(k).startswith("me") and _usable_trace(v)}
    if not mine:
        return _fail("no_traces")
    corners = [c for c in (data.get("corners") or []) if isinstance(c, dict)
               and _num(c.get("start_m")) is not None and _num(c.get("end_m")) is not None
               and _num(c.get("end_m")) > _num(c.get("start_m"))]
    if not corners:
        return _fail("no_corners")
    cl = clean_laps(data)
    if len(cl) < MIN_CLEAN:
        return _fail("not_enough_clean_laps", n_clean=len(cl))
    # M1 (2.3.0 verification): every loop and grid below needs a finite step > 0 and lap length > 0
    raw_step = data.get("step")
    step = 10.0 if raw_step is None else _pos(raw_step)
    raw_L = data.get("L")
    n_cells = max(len(v["tt"]) for v in mine.values())
    L = (n_cells * step if step else None) if raw_L is None else _pos(raw_L)
    if step is None or L is None or L < 3 * step or L / step > 1e6:
        return _fail("bad_data", detail="lap length or grid step is not a usable number",
                     n_clean=len(cl))
    # a corner must sit within a lap of the line and be shorter than a lap, or the wrap is meaningless
    corners = [c for c in corners if -L <= float(c["start_m"]) and float(c["end_m"]) <= 2 * L
               and float(c["end_m"]) - float(c["start_m"]) <= L]
    if not corners:
        return _fail("bad_data", detail="corner positions are outside the lap", n_clean=len(cl))
    trs = {}
    for n, t in cl:
        tr = mine.get(f"me{n}")
        if tr is not None:
            trs[n] = _Trace({**tr, "t": tr.get("t", t)}, step, L)
    typical = [n for n, _ in cl if n in trs]
    if len(typical) < MIN_CLEAN:
        return _fail("no_traces", n_clean=len(cl))   # clean laps are missing their traces
    ref = [n for n in better_laps(data) if n in trs]
    if len(ref) < MIN_REF:
        return _fail("not_enough_better_laps", n_clean=len(cl))
    lap_t = float(np.median([t for _, t in cl]))

    out_corners = []
    for c in corners:
        per = c.get("per_lap") if isinstance(c.get("per_lap"), dict) else {}
        typ = [n for n in typical if isinstance(per.get(str(n)), dict)]
        rl = [n for n in ref if isinstance(per.get(str(n)), dict)]
        if len(typ) < MIN_CORNER_LAPS or len(rl) < MIN_REF:
            continue
        mk, lift = _markers(c, [trs[n] for n in rl], [per[str(n)] for n in rl], step)
        bounds = _phase_bounds(mk)
        ref_pt = [_phase_times(trs[n], bounds) for n in rl]
        ref_t = {p: float(np.median([x[p] for x in ref_pt])) for p in ALL_PHASES}
        lap_loss = {n: {p: v - ref_t[p] for p, v in _phase_times(trs[n], bounds).items()} for n in typ}
        loss_raw = {p: float(np.median([lap_loss[n][p] for n in typ])) for p in ALL_PHASES}
        lens = {p: b - a for p, (a, b) in bounds.items()}
        # M3: a phase with no length in this corner was not measured here: None, not "no loss"
        loss = {p: (None if lens[p] <= 0 else (0.0 if v < NOISE_S else v)) for p, v in loss_raw.items()}
        # M2: the real corner-zone time (braking point to corner end, the four scored phases) per lap
        zone = (mk["B"], mk["E"])
        zone_t = {n: trs[n].time_at(zone[1]) - trs[n].time_at(zone[0]) for n in set(typ) | set(rl)}
        if not all(math.isfinite(v) for v in zone_t.values()) or not all(
                math.isfinite(v) for x in lap_loss.values() for v in x.values()):
            raise ValueError(f"non-finite time in corner {c.get('id')}")
        out_corners.append({
            "id": str(c.get("id", "?")), "lift": bool(lift), "n": len(typ), "n_ref": len(rl),
            "markers": {k: round(v, 1) for k, v in mk.items()},
            "loss": {p: (None if v is None else round(v, 3)) for p, v in loss.items()},
            "loss_raw": {p: round(v, 4) for p, v in loss_raw.items()},
            "ref_t": {p: round(v, 3) for p, v in ref_t.items()},
            "iqr_s": {p: _r(_iqr([lap_loss[n][p] for n in typ]), 4) for p in ALL_PHASES},
            # raw per-lap losses (seconds vs the better laps' median), so the noise gate can be retuned
            "per_lap": {str(n): {p: round(v, 3) for p, v in lap_loss[n].items()} for n in typ},
            "_len": lens, "_zone_t": zone_t, "_typ": typ, "_ref": rl,
        })
    if not out_corners:
        return _fail("no_corners", n_clean=len(cl))

    phases = {}
    for p in PHASES:
        have = [c for c in out_corners if c["_len"][p] > 0]
        if not have:
            # M3: no corner has this phase (e.g. trail braking to the slowest point leaves no
            # "turn-in to apex" stretch): not measured, so no time, no score (the page shows n/a)
            phases[p] = {"loss_s": None, "loss_pm": None, "score": None, "corners": 0, "spread_s": None,
                         "share": None}
            continue
        loss_s = sum(c["loss"][p] for c in have)
        pm = loss_s / lap_t * 1000.0 if lap_t > 0 else None
        spreads = [c["iqr_s"][p] for c in have if c["iqr_s"][p] is not None]
        phases[p] = {"loss_s": round(loss_s, 3), "loss_pm": _r(pm, 2), "score": score(pm, p),
                     "corners": len(have),
                     "spread_s": _r(float(np.median(spreads)), 3) if spreads else None}
    st = sum(c["loss"]["approach"] or 0.0 for c in out_corners)
    sum_s = sum(phases[p]["loss_s"] for p in PHASES if phases[p]["loss_s"] is not None)
    # M2: "where it goes" shares of the summed phase medians (each clamped at 0, against the better laps'
    # median of each phase, so the sum is not itself a lap gap) ...
    for p in PHASES:
        if phases[p]["loss_s"] is not None:
            phases[p]["share"] = round(phases[p]["loss_s"] / sum_s, 3) if sum_s > 0 else 0.0
    # ... and the real gap: per typical lap, its time over every corner zone (brake point to corner end)
    # minus the better laps' median of the same total; the median of that over typical laps
    typ_all = [n for n in typical if all(n in c["_zone_t"] for c in out_corners)]
    ref_all = [n for n in ref if all(n in c["_zone_t"] for c in out_corners)]
    if len(typ_all) >= MIN_CORNER_LAPS and len(ref_all) >= MIN_REF:
        tot_lap = {n: sum(c["_zone_t"][n] for c in out_corners) for n in set(typ_all) | set(ref_all)}
        ref_tot = float(np.median([tot_lap[n] for n in ref_all]))
        gap = float(np.median([tot_lap[n] - ref_tot for n in typ_all]))
        gap_laps = len(typ_all)
    else:   # corners scored from different lap sets: median of the per-corner gaps, summed
        gap = sum(float(np.median([c["_zone_t"][n] for n in c["_typ"]]) - np.median([c["_zone_t"][n] for n in c["_ref"]]))
                  for c in out_corners)
        gap_laps = None
    tot = 0.0 if gap < NOISE_S else gap
    tot_pm = tot / lap_t * 1000.0 if lap_t > 0 else None
    for c in out_corners:
        for k in ("_len", "_zone_t", "_typ", "_ref"):
            c.pop(k, None)
    return {"version": VERSION, "params": _params(), "reason": None,
            "lap_t": round(lap_t, 3), "n_clean": len(cl), "ref_laps": sorted(ref), "typical_laps": typical,
            "phases": phases,
            "straights": {"loss_s": round(st, 3), "loss_pm": _r(st / lap_t * 1000.0 if lap_t > 0 else None, 2)},
            "overall": {"loss_s": round(tot, 3), "loss_pm": _r(tot_pm, 2), "score": score(tot_pm, "overall"),
                        "gap_raw_s": round(gap, 4), "gap_laps": gap_laps, "sum_s": round(sum_s, 3),
                        "basis": "zone_gap"},
            "corners": out_corners}


def compact(block) -> dict | None:
    """The library-row shape: {"v", "n_clean", "lap_t", "loss_pm": {braking, entry, mid, exit, overall}}
    or None when the block can't be used (missing, or has a reason)."""
    if not isinstance(block, dict) or block.get("reason") or not isinstance(block.get("phases"), dict):
        return None
    lp = {}
    for p in PHASES:
        ph = block["phases"].get(p) if isinstance(block["phases"].get(p), dict) else {}
        if isinstance(ph.get("corners"), int) and ph["corners"] <= 0:
            continue        # not measured in any corner (M3): left out, so the profile skips it
        v = _num(ph.get("loss_pm"))
        if v is not None:
            lp[p] = round(v, 2)
    ov = _num((block.get("overall") or {}).get("loss_pm"))
    if ov is not None:
        lp["overall"] = round(ov, 2)
    if not lp:
        return None
    n = _num(block.get("n_clean"))
    return {"v": int(_num(block.get("version")) or 1), "n_clean": int(n) if n is not None else None,
            "lap_t": _r(block.get("lap_t")), "loss_pm": lp}
