"""Synthetic Garage 61 race for tests/test_fix_a.py: 60 Hz telemetry CSVs on a 3 km loop with 4 corners.
Corner speed profiles are circular (a corner near the line brakes across it). Offline, deterministic."""
import csv
import io

import numpy as np

L = 3000.0
APEXES = [(400, 22.0), (1100, 30.0), (1700, 18.0), (2500, 35.0)]  # (m, apex m/s)


def profile(d, jitter, apexes):
    v = np.full_like(d, 70.0)
    for k, (a, va) in enumerate(apexes):
        va2 = va + jitter.get(k, 0.0)
        dd = np.abs(d - a)
        dd = np.minimum(dd, L - dd)
        v = np.minimum(v, va2 + (70.0 - va2) * np.clip(dd / 160.0, 0, 1) ** 1.3)
    return v


def lap_csv(jitter=None, hz=60, slow_at=None, slow=0.0, drop_cols=(), apexes=None):
    jitter = jitter or {}
    apexes = apexes or APEXES
    dgrid = np.linspace(0, L, 3001)
    vg = profile(dgrid, jitter, apexes)
    if slow_at is not None:
        m = np.abs(dgrid - slow_at) < 60
        vg[m] *= (1 - slow)
    t, d, rows = 0.0, 0.0, []
    vprev = float(vg[-2])
    while d < L:
        v = float(np.interp(d, dgrid, vg))
        acc = (v - vprev) * hz
        b = min(1.0, max(0.0, -acc / 12.0))
        th = 1.0 if acc >= -0.5 and b < 0.05 else 0.0
        rows.append(dict(Speed=v, LapDistPct=d / L, Brake=b, Throttle=th, Gear=3 if v < 35 else 5,
                         ABSActive="False", LatAccel=0.0, LongAccel=acc, SteeringWheelAngle=0.0, RPM=6000,
                         Lat=35.0 + d * 1e-6, Lon=-80.0))
        vprev = v
        d += v / hz
        t += 1 / hz
    cols = [c for c in rows[0] if c not in drop_cols]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for r in rows:
        w.writerow([r[c] for c in cols])
    return buf.getvalue(), t


def race(n=20, seed=1, unclean=(), slow_laps=None, noise=0.6, drop_cols=(), apexes=None):
    rng = np.random.default_rng(seed)
    laps, tel = [], {}
    for i in range(1, n + 1):
        jit = {k: rng.normal(0, noise) for k in range(len(apexes or APEXES))}
        kw = {}
        if slow_laps and i in slow_laps:
            kw = dict(slow_at=slow_laps[i][0], slow=slow_laps[i][1])
        txt, t = lap_csv(jit, drop_cols=drop_cols, apexes=apexes, **kw)
        lid = f"lap{i}"
        laps.append({"id": lid, "lapTime": round(t, 3), "lapNumber": i, "clean": i not in unclean,
                     "_start": 1000.0 + i * 100, "startTime": "2026-10-01T18:00:00Z"})
        tel[lid] = txt
    meta = {"driver": "T", "event_id": "E", "track": "Synth", "car": "Car", "started_at": "2026-10-01T18:00:00Z"}
    return meta, laps, tel
