"""Synthetic review dicts for tests/test_23_b.py and the sample_*.json fixtures (offline, deterministic).

A 3000 m loop on the review's 10 m trace grid with two corners:
  C1 (apex 800 m)  lift corner: no brake, throttle dips to 30 % before the apex
  C2 (apex 2000 m) braking corner: brake 80 % from apex-150 m, released by apex-30 m, throttle past 50 %
                   at apex+60 m
"""
from __future__ import annotations

import random

import numpy as np

L = 3000.0
STEP = 10
N = int(L / STEP)
DT = 0.25                       # base time per 10 m cell
CORNERS = [("C1", 800.0, True), ("C2", 2000.0, False)]


def _channels():
    d = np.arange(N) * STEP
    b = np.zeros(N)
    th = np.full(N, 100.0)
    for cid, apex, lift in CORNERS:
        if lift:
            th[(d >= apex - 100) & (d <= apex + 30)] = 30.0
        else:
            on = (d >= apex - 150) & (d < apex - 60)
            b[on] = 80.0
            taper = (d >= apex - 60) & (d < apex - 30)
            b[taper] = 80.0 * (apex - 30 - d[taper]) / 30.0
            th[(d >= apex - 150) & (d <= apex + 20)] = 0.0
            ramp = (d > apex + 20) & (d < apex + 100)
            th[ramp] = (d[ramp] - (apex + 20)) / 80.0 * 100.0
    return d, b, th


def corner_window(apex):
    return apex - 350.0, apex + 250.0


def make_review(n_laps=12, extra=None, clean=None, noise=0.0, seed=1, session="Race", lap_t_jitter=None,
                starts=None, fuel=None, first_n=1, lap_numbers=None, sectors=False):
    """extra: {lap_n: (from_m, to_m, seconds)} time added evenly over a stretch on that lap.
    clean: set of clean lap numbers (default all). noise: +/- seconds of uniform noise per cell time stamp.
    lap_t_jitter: {lap_n: s} added to the lap time only (not the trace), to order laps."""
    rng = random.Random(seed)
    extra = extra or {}
    d, b, th = _channels()
    nums = lap_numbers or list(range(first_n, first_n + n_laps))
    clean = set(nums) if clean is None else set(clean)
    traces, laps, per = {}, [], {cid: {} for cid, _, _ in CORNERS}
    for k, n in enumerate(nums):
        cell = np.full(N, DT)
        for (a, z, s) in ([extra[n]] if isinstance(extra.get(n), tuple) else extra.get(n, [])):
            m = (d >= a) & (d < z)
            cell[m] += s / m.sum()
        tt = np.concatenate([[0.0], np.cumsum(cell)[:-1]])
        T = float(tt[-1] + cell[-1])
        if noise:
            tt = tt + np.array([rng.uniform(-noise, noise) for _ in range(N)])
            tt[0] = 0.0
        lt = T + (lap_t_jitter or {}).get(n, 0.0)
        traces[f"me{n}"] = {"tt": [round(float(x), 3 if noise else 2) for x in tt],
                            "v": [80] * N, "b": [int(x) for x in b], "th": [int(x) for x in th],
                            "g": [4] * N, "t": round(lt, 3), "label": f"Your lap {n}"}
        lap = {"n": n, "t": round(lt, 3), "clean": n in clean,
               "sectors": [round(lt / 3, 3)] * 3 if sectors else None}
        if starts is not None:
            lap["start"] = starts[k]
        laps.append(lap)
        for cid, apex, lift in CORNERS:
            s0, s1 = corner_window(apex)
            ct = float(np.interp(s1, d, tt) - np.interp(s0, d, tt))
            per[cid][str(n)] = {"t": round(ct, 3), "min_mph": 60.0, "min_at": apex, "gear": 3,
                                "peak_brake": None if lift else 80.0, "brake_on": None if lift else apex - 150,
                                "full_thr": apex + 100, "abs_s": 0.0, "coast_s": 0.0, "exit_mph": 70.0}
    corners = []
    for cid, apex, lift in CORNERS:
        s0, s1 = corner_window(apex)
        corners.append({"id": cid, "apex_m": apex, "start_m": s0, "end_m": s1, "per_lap": per[cid],
                        "avail": 0.05, "rank": 1})
    best = min(laps, key=lambda l: l["t"])
    data = {"meta": {"session": session, "track": "Synthetic Loop", "car": "Ford Mustang GT4",
                     "started_at": "2026-10-20T19:00:00+00:00", "test_data": True},
            "L": L, "step": STEP, "laps": laps, "best": {"n": best["n"], "t": best["t"], "clean": True},
            "median": float(np.median([l["t"] for l in laps])), "corners": corners, "traces": traces,
            "sector_names": ["S1", "S2", "S3"] if sectors else []}
    if fuel is not None:
        data["stint"] = {"laps": [{"n": n, "t": l["t"], "fuel_used": None, "fuel_level": f}
                                  for n, l, f in zip(nums, laps, fuel)]}
    return data


def exit_loss_review(loss=0.05, n_laps=12, n_better=3, **kw):
    """Typical laps lose `loss` seconds over C2's exit stretch (P..E) against the better laps."""
    apex = 2000.0
    P, E = apex + 60.0, corner_window(apex)[1]
    extra = {n: (P, E, loss) for n in range(n_better + 1, n_laps + 1)}
    return make_review(n_laps=n_laps, extra=extra, **kw)
