"""C1, the analysis interface (2.30.1): the one door Siyanqaba (Swazi's offline engine on the Zerver) uses for every
number. See CONTRACT.md. Pure analysis: numpy and Debrief's own modules only, no window, sound, network or irsdk.

Every name here wraps a Debrief function that the app itself uses, so a number Siyanqaba shows is the number Debrief
shows. Changing a name, an argument or a returned key here is a contract change: bump C1_VERSION here and in
CONTRACT.md, and the contract test (tests/test_2301_c1.py) must pass on both sides.

    review_session(paths, history=None, race_minutes=None, race_laps=None)  one session, as the window shows it
    read_laps(path)            the laps in one .ibt: time, how it was timed, pit, off track, incidents, start lap
    corner_cards(paths)        per-corner advice that passed the shuffle (permutation) test
    habit_p(times, values)     shuffle-test p for a pedal habit (coasting, both pedals)
    detect_corners / build_segments   the corner and segment maths, for callers holding resampled laps
    session_id(paths)          the id Debrief, the quick-tap file and the Zerver all use for one session
    corner_key(apex_pct, ...)  match a corner between sessions by where it is on the lap, never by its number
    THRESHOLDS                 every cut-off the claims depend on
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from . import cornercoach, engine, ibt, insights, sessionibt
from . import setuplab as _sl

C1_VERSION = "1"

# corners are the same corner in two sessions when their apexes are within max(CORNER_MATCH_M, CORNER_MATCH_FRAC
# of the lap) (the same tolerance as the 2.20 corner notes)
CORNER_MATCH_M = 60.0
CORNER_MATCH_FRAC = 0.02

THRESHOLDS = {
    "review_min_laps": sessionibt.MIN_LAPS,          # consistency, segment spread and best possible lap
    "near_best_s": sessionibt.NEAR_BEST_S,
    "slow_mad_k": _sl.MAD_K,                         # a lap over median + k x MAD of its run is "slow"
    "slow_fallback_ratio": _sl.TRAFFIC,              # ... or over this x the median when the MAD is 0
    "max_fuel_lap_l": sessionibt.MAX_FUEL_LAP_L,
    "fuel_margin_laps": sessionibt.FUEL_MARGIN_LAPS,
    "card_min_laps": cornercoach.MIN_LAPS,
    "card_thin_laps": cornercoach.THIN_LAPS,
    "card_brake_min_laps": cornercoach.MIN_BRAKE_LAPS,
    "card_perm_n": cornercoach.PERM_N,
    "card_p_likely": cornercoach.P_LIKELY,
    "card_p_clear": cornercoach.P_CLEAR,
    "habit_min_laps": insights.LUCK_MIN_LAPS,
    "habit_alpha": insights.LUCK_ALPHA,
    "corner_match_m": CORNER_MATCH_M,
    "corner_match_frac": CORNER_MATCH_FRAC,
}


def review_session(paths, history=None, race_minutes=None, race_laps=None) -> dict:
    """sessionibt.review(): laps (valid / clean and why), consistency, best possible lap, segment spread, fuel,
    stints, notes and the thresholds used. Never raises for a bad file."""
    return sessionibt.review(list(paths or []), history=history, race_minutes=race_minutes, race_laps=race_laps)


def read_laps(path) -> list:
    """ibt.laps() for one file (opened read-only)."""
    ib = ibt.open_ibt(path)
    try:
        return ibt.laps(ib)
    finally:
        close = getattr(ib, "close", None)
        if callable(close):
            close()


def corner_cards(paths) -> dict:
    """cornercoach.analyze_files(): {"cards", "unclear", "fine", "corners", "per_corner", ...}. A card exists only
    when its rule passed the permutation test (p under P_LIKELY). Never raises."""
    return cornercoach.analyze_files(list(paths or []))


def habit_p(times, values):
    """insights.luck_p(): shuffle-test p, or None with fewer than LUCK_MIN_LAPS laps."""
    return insights.luck_p(times, values)


detect_corners = engine.detect_corners
build_segments = engine.build_segments


def session_id(paths) -> str:
    """16 hex characters naming one session: the file names and sizes, sorted. The same on the gaming PC and on
    the Zerver once the files are copied (names and sizes don't change in a copy)."""
    parts = []
    for p in paths or []:
        p = Path(p)
        try:
            size = p.stat().st_size
        except OSError:
            size = -1
        parts.append(f"{p.name}|{size}")
    return hashlib.sha256("\n".join(sorted(parts)).encode("utf-8")).hexdigest()[:16]


def corner_tolerance_m(length_m) -> float:
    return max(CORNER_MATCH_M, CORNER_MATCH_FRAC * float(length_m or 0.0))


def same_corner(apex_pct_a, apex_pct_b, length_m) -> bool:
    """True when two apexes (lap fractions 0..1) are the same corner on a lap of length_m metres."""
    if apex_pct_a is None or apex_pct_b is None or not length_m:
        return False
    d = abs(float(apex_pct_a) - float(apex_pct_b)) % 1.0
    d = min(d, 1.0 - d) * float(length_m)
    return d <= corner_tolerance_m(length_m)


def corner_key(apex_pct, corners, length_m):
    """The id ("C3") of the corner in `corners` ([{"id", "apex_pct"}]) at apex_pct, or None."""
    best, best_d = None, None
    for c in corners or []:
        a = c.get("apex_pct")
        if a is None or not same_corner(apex_pct, a, length_m):
            continue
        d = min(abs(float(a) - float(apex_pct)) % 1.0, 1.0 - abs(float(a) - float(apex_pct)) % 1.0)
        if best_d is None or d < best_d:
            best, best_d = c.get("id"), d
    return best
