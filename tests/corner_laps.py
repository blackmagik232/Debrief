"""Fake GT4-at-Zolder laps with driving differences, for the 2.13 window tests (tests/test_213_page.py).

Uses the lap builder at the top of tests/test_213_corners.py (its driving model: brake point, minimum speed, pedal
pressure, throttle pick-up per corner), loaded without running that file's checks, with seven corners and a falling
fuel level so a session review looks like a real one. Not a test file itself (run_all.py only runs test_*.py).
"""
from __future__ import annotations

import types
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
_MARK = "# ---------------------------------------------------------------- 1. brake later"


def builder() -> dict:
    """The builder's namespace (make_file, lap_arrays, BASE, jit ...), without running test_213_corners' checks."""
    path = HERE / "test_213_corners.py"
    src = path.read_text(encoding="utf-8")
    ns = {"__name__": "corner_builder", "__file__": str(path)}
    exec(compile(src[:src.index(_MARK)], str(path), "exec"), ns)   # noqa: S102  (our own test file)
    return ns


NS = builder()
NS["APEXES"] = (420.0, 980.0, 1500.0, 2050.0, 2650.0, 3150.0, 3700.0)
NS["VMAX"] = 50.0
VMIN = (21.0, 30.0, 25.0, 18.0, 33.0, 26.0, 23.0)
FUEL = {"start": 60.0, "per_lap": 2.45}
# brake fully off 10 m before the apex (still on at turn-in: trail braking, as on his real laps) and throttle back on
# 5 m after it: about 5 s a lap with both feet off, like his real laps (2.13 review: was 13 s)
RE_M, PU_M = 10.0, 5.0
OFFS: dict = {}          # file name -> lap numbers with an off track and an incident
_real = NS["ibt"]


def _write_fake(path, text, cols, tick_rate=60, start_date=1_790_000_000):
    n = len(cols["Lap"][1])
    lap = np.asarray(cols["Lap"][1], float)
    pct = np.asarray(cols["LapDistPct"][1], float)
    used = np.where(lap > 0, FUEL["per_lap"] * (lap - 1 + pct), 0.0)
    cols["FuelLevel"] = ("f", (FUEL["start"] - used).astype(np.float32))
    cols["SessionTime"] = ("d", np.arange(n) / float(tick_rate))
    cols["CarLeftRight"] = ("i", np.ones(n, np.int32))
    surf = np.asarray(cols["PlayerTrackSurface"][1]).copy()
    inc = np.asarray(cols["PlayerCarMyIncidentCount"][1]).copy()
    for k in OFFS.get(Path(path).name, ()):
        idx = np.nonzero(lap == k)[0]
        if len(idx):
            mid = idx[len(idx) // 2]
            surf[mid: mid + tick_rate] = _real.OFF_TRACK
            inc[mid:] += 2
    cols["PlayerTrackSurface"] = ("i", surf)
    cols["PlayerCarMyIncidentCount"] = ("i", inc)
    return _real.write_fake(path, text, cols, tick_rate=tick_rate, start_date=start_date)


NS["ibt"] = types.SimpleNamespace(write_fake=_write_fake, ON_TRACK=_real.ON_TRACK, OFF_TRACK=_real.OFF_TRACK)


def lap_params(n: int, seed: int = 213) -> list:
    """n laps of seven corners. On a third of the laps (the quick ones) he brakes later into T2, presses the brake
    less hard into T4 (typical laps stamp on it and lean on the ABS) and comes off the brake earlier through T6,
    carrying more speed. T1 is quicker on the same laps with no driving difference (no clear pattern)."""
    rng = np.random.default_rng(seed)
    NS["RNG"] = rng
    base, jit = NS["BASE"], NS["jit"]
    out = []
    for i in range(n):
        best = i % 3 == 0
        cs = [dict(base, vmin=v, re=RE_M, pu=PU_M) for v in VMIN]
        cs[0]["vmin"] += 0.7 if best else 0.0
        cs[1]["bdist"] = 125.0 if best else 140.0
        cs[1]["vmin"] += 0.4 if best else 0.0
        cs[3].update({"peak": 0.80, "vmin": VMIN[3] + 1.0} if best else {"peak": 1.0, "rs": 75.0})
        cs[5].update({"vmin": VMIN[5] + 2.0, "re": RE_M + 10.0} if best else {"re": RE_M})
        lap = [jit(c, timing_only=(k == 0)) for k, c in enumerate(cs)]
        out.append(lap)
    order = rng.permutation(n)
    return [out[k] for k in order]


def make(path, n_laps: int, seed: int = 213, offs=(), **session):
    """An .ibt of n_laps laps (plus the out lap and a short tail) of the McLaren 570S GT4 at Zolder."""
    OFFS[Path(path).name] = list(offs)
    return NS["make_file"](Path(path), lap_params(n_laps, seed), **session)
