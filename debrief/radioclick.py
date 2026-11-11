"""The radio squelch (2.31): a short click before and after each message the engineer says. Made here, in code, from noise
and a sine: no recording, nothing to license. Deterministic (the same bytes every time), a few milliseconds of work.

    wrap(x, sr) -> numpy float samples: click-in, a breath of silence, the message, click-out
"""
from __future__ import annotations

import numpy as np

OPEN_MS, CLOSE_MS, GAP_MS = 55, 40, 35
LEVEL = 0.22             # well under speech (speech is levelled to about -20 dBFS RMS, the click is a short peak)
_CACHE: dict = {}


def _burst(sr, ms, tone_hz, seed, decay):
    n = max(8, int(sr * ms / 1000.0))
    rng = np.random.RandomState(seed)
    noise = rng.uniform(-1.0, 1.0, n)
    noise = np.concatenate([[0.0], np.diff(noise)])           # first difference: a high-passed hiss
    i = np.arange(n)
    env = np.exp(-i / (n * decay)) * np.minimum(1.0, i / max(1, int(0.002 * sr)))
    env[-max(1, int(0.004 * sr)):] *= np.linspace(1.0, 0.0, max(1, int(0.004 * sr)))
    blip = np.sin(2 * np.pi * tone_hz * i / sr) * np.exp(-i / (n * 0.35))
    x = (0.65 * noise + 0.5 * blip) * env
    peak = float(np.max(np.abs(x))) or 1.0
    return LEVEL * x / peak


def click_open(sr):
    k = ("o", sr)
    if k not in _CACHE:
        _CACHE[k] = _burst(sr, OPEN_MS, 1500.0, 31, 0.45)
    return _CACHE[k]


def click_close(sr):
    k = ("c", sr)
    if k not in _CACHE:
        _CACHE[k] = _burst(sr, CLOSE_MS, 1050.0, 77, 0.30)
    return _CACHE[k]


def wrap(x, sr):
    gap = np.zeros(int(sr * GAP_MS / 1000.0))
    return np.concatenate([click_open(sr), gap, np.asarray(x, dtype=np.float64), gap, click_close(sr)])
