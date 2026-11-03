"""Fake laps with a known balance, for the 2.15 window tests (tests/test_215_page.py).

Uses the lap builder at the top of tests/test_215_balance.py (a car with known steering: steering = G x curvature +
K x lateral g, plus noise, and a push or rotation added on purpose), loaded without running that file's checks,
and its .ibt writer with the track, setup and track temperature as arguments. Not a test file itself (run_all.py
only runs test_*.py).
"""
from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
_TOP_END = "# ---------------------------------------------------------------- 0. the filter"
_W_START = "def write_ibt(path, laps):"
_W_END = "\n\nf1 = "


def _load() -> dict:
    path = HERE / "test_215_balance.py"
    src = path.read_text(encoding="utf-8")
    ns = {"__name__": "balance_builder", "__file__": str(path)}
    top = src[:src.index(_TOP_END)]
    w = src[src.index(_W_START):src.index(_W_END)]
    w = w.replace("text = fi.session_text()", "text = fi.session_text(**SESSION_KW)")
    w = w.replace("ibt.write_fake(path, text, cols2, tick_rate=RATE)",
                  "cols2.update(EXTRA_COLS(n))\n    ibt.write_fake(path, text, cols2, tick_rate=RATE, start_date=START)")
    exec(compile(top + "\n\n" + w + "\n", str(path), "exec"), ns)   # noqa: S102  (our own test file)
    shutil.rmtree(ns.get("TMP"), ignore_errors=True)                 # the builder's own temp folder isn't used
    return ns


NS = _load()
T3 = NS["CORNERS"][2][0]
T5 = NS["CORNERS"][4][0]


def push_t3_rot_t5(d, kap, ay):
    """16 degrees more steering in the middle of T3 (pushes), 14 less on the way into T5 (rotates)."""
    out = np.zeros_like(d)
    m = np.abs(d - T3) < 18
    out[m] += np.sign(kap[m]) * 16.0
    m = (d > T5 - 70) & (d < T5 - 18)
    out[m] -= np.sign(kap[m]) * 14.0
    return out


def make(path, n_laps=10, seed=1, extra=push_t3_rot_t5, scale=1.0, hot=None, track_temp=None,
         start_date=1_790_000_000, **session):
    """An .ibt of n_laps laps with a known balance. hot: {"LF": kPa, ...} live hot pressures; track_temp: a live
    TrackTempCrew channel (deg C); scale < 1: slower laps. session: fake_ibt.session_text's arguments (track,
    track_code, setup, surface_temp, name ...)."""
    NS["SESSION_KW"] = dict(session)
    NS["START"] = int(start_date)

    def extra_cols(n):
        out = {}
        if hot:
            for c in ("LF", "RF", "LR", "RR"):
                out[f"{c}pressure"] = ("f", np.full(n, float(hot[c]), np.float32))
        if track_temp is not None:
            out["TrackTempCrew"] = ("f", np.full(n, float(track_temp), np.float32))
        return out
    NS["EXTRA_COLS"] = extra_cols
    return NS["write_ibt"](Path(path), NS["session"](seed, n_laps=n_laps, extra=extra, scale=scale))
