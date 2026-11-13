"""Races against AI cars (2.35): a light touch.

An AI race gets one small "vs AI" tag and nothing more: no strength of field, no iRating against AI, no filter, no
extra page. The telemetry counts like any other race.

    ai_count(session_info)  -> number of AI cars in the .ibt session info (pace car and spectators left out)
    is_vs_ai(data_or_meta)  -> True when a review (or its meta, or a library row) is marked as a race against AI
    TAG                     -> the words on the tag

What a review records: meta["ai_cars"] (how many AI cars) and meta["vs_ai"] (True when there were any). The recorder's
summary carries the same count as "ai_cars". Everything here is pure and never raises.
"""
from __future__ import annotations

TAG = "vs AI"
TAG_HELP = "You raced against AI cars. Strength of field and iRating don't apply, so they are not shown."
# result fields that mean nothing against AI
HIDDEN_RESULT_KEYS = ("sof", "irating_before", "irating_after", "irating_change")


def ai_count(session_info) -> int:
    """AI cars in DriverInfo.Drivers (CarIsAI); the pace car and spectators are never counted."""
    try:
        drivers = ((session_info or {}).get("DriverInfo") or {}).get("Drivers") or []
        n = 0
        for d in drivers:
            if not isinstance(d, dict) or d.get("CarIsPaceCar") or d.get("IsSpectator"):
                continue
            if d.get("CarIsAI"):
                n += 1
        return n
    except Exception:  # noqa: BLE001
        return 0


def _meta_of(x) -> dict:
    if not isinstance(x, dict):
        return {}
    m = x.get("meta")
    return m if isinstance(m, dict) else x


def is_vs_ai(data_or_meta) -> bool:
    """True when the review's meta (or a row / summary with the same keys) says AI cars were in the race.
    Old reviews have neither key: False."""
    try:
        m = _meta_of(data_or_meta)
        if m.get("vs_ai") is True:
            return True
        if m.get("vs_ai") is False:
            return False
        n = m.get("ai_cars")
        return isinstance(n, (int, float)) and not isinstance(n, bool) and n > 0
    except Exception:  # noqa: BLE001
        return False
