"""Synthetic multi-track fixtures for Debrief 2.7 (M3/M4 calibration): three track shapes, generated on
demand, deterministic, offline. Not real data (real Garage 61 exports belong in tests/fixtures_real/).

    shape("oval")    ~1.3 km, two banked turns with only a few mph scrubbed off, ~28-35 s laps, no braking
    shape("street")  ~2.6 km, 9 corners, ~60-75 s laps
    shape("long")    ~21 km, 17 corners, ~7-8 minute laps

Each shape is a dict {name, L, apexes: [(m, apex m/s)], vmax, hz, width} that `race()` turns into Garage 61
lap objects + telemetry CSVs the engine reads (same column set as tests/fixtures_a/synth.py). Laps are
built by stepping distance at `hz` samples per second, so lap times come out of the speed profile.

race(shape, n, seed, noise, slow_laps, unclean, session, multiclass)
    noise      per-corner apex-speed jitter in m/s (relative scatter is noise / apex speed)
    slow_laps  {lap_n: (at_m, factor)}: speed is multiplied by (1 - factor) for ±hold_m around at_m (a hold-up)
    hold_s     instead of a factor, an injected time loss in seconds spread over the hold window
Returns (meta, laps, tel) like synth.race(). Lap 1 is never special.
"""
from __future__ import annotations

import csv
import io

import numpy as np

SHAPES = {
    # a 1.33 km oval: two long banked turns the car takes flat (speed dips ~2-3 mph, no braking)
    "oval": {"name": "Synth Oval", "L": 1330.0, "vmax": 48.0, "hz": 60,
             "apexes": [(330, 46.5), (995, 46.3)], "width": 220.0, "lift": True, "ref_t": 30.0},
    # a 2.6 km street circuit, 9 corners, 60-75 s laps
    "street": {"name": "Synth Street", "L": 2600.0, "vmax": 55.0, "hz": 60,
               "apexes": [(150, 18.0), (420, 24.0), (700, 15.0), (1000, 28.0), (1250, 20.0), (1550, 17.0),
                          (1850, 26.0), (2150, 14.0), (2420, 22.0)], "width": 110.0, "lift": False, "ref_t": 66.0},
    # a 21 km road course, 17 corners spread along it, 7-8 minute laps
    "long": {"name": "Synth Nordschleife", "L": 21000.0, "vmax": 56.0, "hz": 30,
             "apexes": [(500, 25.0), (1500, 32.0), (2600, 20.0), (3900, 36.0), (5100, 28.0), (6300, 22.0),
                        (7400, 40.0), (8800, 30.0), (10100, 26.0), (11400, 34.0), (12700, 21.0), (14000, 38.0),
                        (15300, 29.0), (16700, 24.0), (18000, 35.0), (19200, 27.0), (20400, 31.0)],
             "width": 260.0, "lift": False, "ref_t": 450.0},
}


def shape(name: str) -> dict:
    return dict(SHAPES[name])


def profile(sh: dict, d, jitter: dict):
    """Speed (m/s) along the lap: vmax everywhere, dipping to each apex speed over `width` m either side."""
    L, vmax, width = sh["L"], sh["vmax"], sh["width"]
    v = np.full_like(d, vmax, dtype=float)
    for k, (a, va) in enumerate(sh["apexes"]):
        va2 = va + jitter.get(k, 0.0)
        dd = np.abs(d - a)
        dd = np.minimum(dd, L - dd)
        v = np.minimum(v, va2 + (vmax - va2) * np.clip(dd / width, 0, 1) ** 1.3)
    return v


def lap_csv(sh: dict, jitter=None, slow_at=None, slow=0.0, hold_s=None, hold_m=None):
    """One lap's telemetry CSV and its lap time. slow_at/slow: speed x (1 - slow) within ±hold_m of slow_at;
    hold_s: instead, lose about hold_s seconds in that window (the factor is worked out from the window)."""
    jitter = jitter or {}
    L, hz = sh["L"], sh["hz"]
    dgrid = np.linspace(0, L, int(L) + 1)
    vg = profile(sh, dgrid, jitter)
    if slow_at is not None:
        hm = hold_m if hold_m is not None else max(40.0, 0.03 * L)
        m = np.abs(dgrid - slow_at) < hm
        if hold_s is not None:
            # time through the window at the current speed, then the factor that adds hold_s to it
            t_win = float(np.sum(1.0 / vg[m]))   # 1 m cells
            slow = 1.0 - t_win / (t_win + hold_s)
        vg[m] *= (1 - slow)
    t, d, rows = 0.0, 0.0, []
    vprev = float(vg[-2])
    lift = sh.get("lift")
    while d < L:
        v = float(np.interp(d, dgrid, vg))
        acc = (v - vprev) * hz
        b = 0.0 if lift else min(1.0, max(0.0, -acc / 12.0))
        th = 1.0 if acc >= -0.5 and b < 0.05 else (0.6 if lift else 0.0)
        rows.append(dict(Speed=v, LapDistPct=d / L, Brake=b, Throttle=th, Gear=3 if v < 35 else 5,
                         ABSActive="False", LatAccel=0.0, LongAccel=acc, SteeringWheelAngle=0.0, RPM=6000,
                         Lat=35.0 + d * 1e-6, Lon=-80.0))
        vprev = v
        d += v / hz
        t += 1 / hz
    cols = list(rows[0])
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for r in rows:
        w.writerow([r[c] for c in cols])
    return buf.getvalue(), t


def race(shape_name: str, n: int = 16, seed: int = 1, noise: float = 0.6, slow_laps=None, unclean=(),
         session: str = "Race", multiclass: bool = False, fuel: float | None = None):
    """(meta, laps, tel) for `n` laps of one shape. noise: apex-speed jitter (m/s). slow_laps: {n: (at_m, factor)}
    or {n: (at_m, {"hold_s": s})}. fuel: litres per lap written as fuelUsed/fuelLevel on each lap."""
    sh = shape(shape_name)
    rng = np.random.default_rng(seed)
    laps, tel = [], {}
    level = 60.0
    for i in range(1, n + 1):
        jit = {k: rng.normal(0, noise) for k in range(len(sh["apexes"]))}
        kw = {}
        if slow_laps and i in slow_laps:
            at, how = slow_laps[i]
            kw = dict(slow_at=at, **({"hold_s": how["hold_s"]} if isinstance(how, dict) else {"slow": how}))
        txt, t = lap_csv(sh, jit, **kw)
        lid = f"lap{i}"
        lap = {"id": lid, "lapTime": round(t, 3), "lapNumber": i, "clean": i not in unclean,
               "_start": 1000.0 + i * (t + 2), "startTime": "2026-10-01T18:00:00Z"}
        if fuel:
            lap["fuelUsed"] = fuel
            lap["fuelLevel"] = round(level - fuel * i, 2)
        laps.append(lap)
        tel[lid] = txt
    meta = {"driver": "T", "event_id": f"E-{shape_name}", "track": sh["name"], "car": "Car",
            "started_at": "2026-10-01T18:00:00Z", "session": session, "multiclass": multiclass}
    return meta, laps, tel
