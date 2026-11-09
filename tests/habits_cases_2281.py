"""Review shapes for the 2.28.1 habits tests: hand-made rich reviews (every habit fires) and a few built by the
engine from the synthetic track fixtures. Used by test_2281_habits.py and by tools that made golden_2281_old.json
(the habits output of 2.28.0, so the test can show no number was lost).

Hand-made cases are plain dicts; engine cases need `debrief` importable (whichever copy is first on sys.path).
"""
from __future__ import annotations

import copy

TURNS = [("Turn 1", 100, 220), ("Turn 2", 480, 560), ("Turn 3", 900, 1010), ("Turn 4", 1300, 1380),
         ("Turn 5", 1700, 1790), ("Turn 6", 2000, 2100), ("Turn 7", 2400, 2480), ("Turn 8", 2700, 2780)]


def _corner(cid, s, e, avail):
    return {"id": cid, "apex_m": (s + e) / 2, "start_m": float(s), "end_m": float(e), "best_t": 8.0, "median_t": 8.0 + avail,
            "p25_t": 8.1, "avail": avail, "iqr": 0.1, "ref_gap": None, "best_lap": 3, "summary": {"gear": 3, "min_mph": 60.0}}


def rich(start_type=None, fuel=True, trend_raw=0.6):
    laps = []
    for i in range(1, 17):
        t = 138.6 + 0.1 * (i % 3)
        if i == 1:
            t = 146.712
        if i in (5, 9):
            t = 150.4
        laps.append({"n": i, "t": t, "clean": i not in (1, 3, 12, 5, 9, 7)})
    avail = [0.05, 0.02, 0.18, 0.04, 0.12, 0.2, 0.03, 0.01]
    corners = [_corner(c, s, e, a) for (c, s, e), a in zip(TURNS, avail)]
    d = {"L": 3000.0, "laps": laps, "best": {"n": 2, "t": 138.601, "clean": True}, "median": 138.601, "refs": [],
         "counts": {"unclean": [3, 12], "slow": [5, 9], "traffic": [7], "traffic_possible": [5],
                    "traffic_detail": [{"n": 7, "lost": 1.2, "at_m": 2500.0}],
                    "traffic_possible_detail": [{"n": 5, "lost": 0.83, "at_m": 1500.0}, {"n": 1, "lost": 0.9, "at_m": 150.0}],
                    "clean": 10, "analysed": 16, "excluded": 0, "pit": []},
         "slow_cut": 1.0, "trend_total": trend_raw, "trend_per10": trend_raw / 13.0 * 10.0,
         "consistency": {"score": 40, "label": "Scattered", "std": 1.1, "within_05": 20.0, "within_s": 0.5, "n": 10,
                         "off_pace_n": 2, "off_pace": [5, 9],
                         "by_corner": [{"id": "Turn 1", "iqr": 0.04}, {"id": "Turn 2", "iqr": 0.05}, {"id": "Turn 3", "iqr": 0.35},
                                       {"id": "Turn 4", "iqr": 0.03}, {"id": "Turn 5", "iqr": 0.2}, {"id": "Turn 6", "iqr": 0.17},
                                       {"id": "Turn 7", "iqr": 0.02}, {"id": "Turn 8", "iqr": 0.02}]},
         "corners": corners,
         "technique": [{"id": c[0], "best": {"brake_on": 500.0, "min_mph": 60.0, "coast_s": 0.5, "gear": 3},
                        "typical": {"brake_on": 500.0, "min_mph": 60.0, "coast_s": 0.5, "gear": 3}, "ref": None} for c in TURNS],
         "brake_spread": {"corners": [
             {"id": "Turn 1", "n": 14, "iqr_m": 3.0, "label": "locked"}, {"id": "Turn 3", "n": 14, "iqr_m": 4.0, "label": "locked"},
             {"id": "Turn 4", "n": 14, "iqr_m": 16.0, "label": "varies"}, {"id": "Turn 5", "n": 14, "iqr_m": 30.0, "label": "all over"},
             {"id": "Turn 6", "n": 14, "iqr_m": 2.0, "label": "locked"}, {"id": "Turn 7", "n": 5, "iqr_m": 40.0, "label": "all over"}]},
         "stint": {"fuel_s_per_lap": 0.147 if fuel else None,
                   "corner_drift": [{"id": "Turn 6", "early_t": 8.0, "late_t": 8.21, "delta_t": 0.21, "delta_mph": -2.0},
                                    {"id": "Turn 3", "early_t": 9.0, "late_t": 9.12, "delta_t": 0.12, "delta_mph": -1.0},
                                    {"id": "Turn 8", "early_t": 7.0, "late_t": 7.02, "delta_t": 0.02, "delta_mph": 0.0}]},
         "racecraft": {"lap1": {"n": 1, "in_analysis": True, "start_type": start_type,
                                "segments": [{"id": "Turn 1", "lost_s": 3.2}, {"id": "Turn 2", "lost_s": 1.1}]}},
         }
    if fuel:
        # same arithmetic as analysis_plus.fuel_corrected_trend: raw + fuel x laps from first to last clean lap
        from debrief import analysis_plus
        d["trend_fuel"] = analysis_plus.fuel_corrected_trend(d)
    ph = {"braking": 0.02, "entry": 0.03, "mid": 0.04, "exit": 0.28}
    d["skills"] = {"version": 1, "reason": None, "lap_t": 138.6,
                   "phases": {p: {"loss_s": v, "loss_pm": v / 138.6 * 1000, "corners": 8} for p, v in ph.items()},
                   "overall": {"loss_s": 0.5, "loss_pm": 3.6, "basis": "zone_gap"},
                   "corners": [{"id": c[0], "loss": {"braking": 0.0, "entry": 0.0, "mid": 0.01 if c[0] != "Turn 3" else 0.08,
                                                     "exit": {"Turn 2": 0.12, "Turn 6": 0.1, "Turn 5": 0.05}.get(c[0], 0.0)}}
                               for c in TURNS if c[0] != "Turn 7"]}
    return d


def clean_race():
    laps = [{"n": i, "t": 90.0 + (0.1 * (i % 3)), "clean": True} for i in range(1, 13)]
    return {"laps": laps, "best": {"n": 2, "t": 90.0, "clean": True}, "median": 90.1, "refs": [],
            "counts": {"unclean": [], "slow": [], "traffic": [], "clean": 12}, "trend_total": 0.05,
            "consistency": {"score": 80, "label": "Very consistent", "std": 0.1, "within_05": 90.0, "within_s": 0.5, "n": 12,
                            "by_corner": [{"id": "C1", "iqr": 0.04}, {"id": "C2", "iqr": 0.05}, {"id": "C3", "iqr": 0.05}]},
         "corners": [_corner("C1", 100, 200, 0.01), _corner("C2", 400, 500, 0.01), _corner("C3", 700, 800, 0.02)],
            "technique": [], "brake_spread": {"corners": [{"id": "C1", "n": 12, "iqr_m": 3.0, "label": "locked"},
                                                           {"id": "C2", "n": 12, "iqr_m": 4.0, "label": "locked"}]}}


def flat_oval_fuel():
    d = clean_race()
    d["trend_total"] = -0.001
    d["trend_per10"] = -0.001
    d["stint"] = {"fuel_s_per_lap": 0.0}
    d["trend_fuel"] = {"total": -0.001, "per10": -0.001, "fuel_s_per_lap": 0.0, "corrected": True}
    return d


HAND = {"rich_unknown_start": lambda: rich(None), "rich_rolling": lambda: rich("rolling"),
        "rich_nofuel": lambda: rich(None, fuel=False, trend_raw=0.9), "clean_race": clean_race, "flat_fuel": flat_oval_fuel}


def fade_slow(n, at=1000, step=0.1):
    return {i: (at, {"hold_s": step * (i - 1)}) for i in range(2, n + 1)}


ENGINE = {
    "oval_n12": dict(shape="oval", n=12),
    "street_n12": dict(shape="street", n=12),
    "long_n10": dict(shape="long", n=10),
    "street_n12_fuel": dict(shape="street", n=12, fuel=1.9),
    "street_fade_nofuel": dict(shape="street", n=12, slow_laps=fade_slow(12)),
    "street_fade_fuel": dict(shape="street", n=12, fuel=1.9, slow_laps=fade_slow(12)),
    "street_flags": dict(shape="street", n=12, unclean=(6, 7), slow_laps={4: (1000, {"hold_s": 3.0})}),
}


def engine_case(name):
    import fixtures_tracks as ft
    from debrief import engine, report
    spec = dict(ENGINE[name])
    shape = spec.pop("shape")
    meta, laps, tel = ft.race(shape, **spec)
    d = engine.analyze(meta, laps, tel, [], {}, log=lambda m: None)
    report._extras(d, None)
    return d


def all_cases():
    for k, f in HAND.items():
        yield k, copy.deepcopy(f())
    for k in ENGINE:
        yield k, engine_case(k)
