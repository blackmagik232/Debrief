"""Unit tests for debrief.analysis_plus (and the engine's brake-point detection).

Run:  python3 -m pytest tests/test_analysis.py    or    python3 tests/test_analysis.py
Synthetic inputs only, no network. The sign-convention tests are the important ones.
"""
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import numpy as np  # noqa: E402

from debrief import analysis_plus as ap  # noqa: E402
from debrief import engine  # noqa: E402


def M(t, brake_on=100.0, peak_brake=80.0, min_mph=60.0, full_thr=300.0, coast_s=0.2, exit_mph=90.0, gear=3, abs_s=0.0):
    return {"t": t, "brake_on": brake_on, "peak_brake": peak_brake, "min_mph": min_mph, "full_thr": full_thr,
            "coast_s": coast_s, "exit_mph": exit_mph, "gear": gear, "abs_s": abs_s}


def lap(n, t, fuel_used=2.0, fuel_level=None, clean=True):
    return {"n": n, "t": t, "clean": clean, "lap": {"fuelUsed": fuel_used, "fuelLevel": fuel_level if fuel_level is not None else 60 - 2 * n}}


# ---------------------------------------------------------------- stint
def test_deg_positive_when_getting_slower():
    laps = [lap(n, 90 + 0.1 * n) for n in range(1, 13)]
    s = ap.stint(laps, laps, {}, [])
    assert abs(s["deg_s_per_lap"] - 0.1) < 1e-6
    assert s["deg_s_per_lap"] > 0


def test_deg_negative_when_getting_quicker():
    laps = [lap(n, 90 - 0.05 * n) for n in range(1, 13)]
    s = ap.stint(laps, laps, {}, [])
    assert s["deg_s_per_lap"] < 0


def test_fuel_correction_adds_back_fuel_gain():
    # flat raw pace while burning fuel means the car was actually losing pace underneath
    laps = [lap(n, 90.0, fuel_used=2.0) for n in range(1, 13)]
    s = ap.stint(laps, laps, {}, [])
    fx = 2.0 * ap.FUEL_KG_PER_L * ap.FUEL_S_PER_KG_PER_90S * (90.0 / 90.0)
    assert abs(s["fuel_s_per_lap"] - fx) < 1e-4
    assert abs(s["fuel_corrected_deg"] - (0.0 + fx)) < 1e-4
    assert s["fuel_corrected_deg"] > s["deg_s_per_lap"]


def test_fuel_effect_example_from_docstring():
    v = ap.fuel_effect_s_per_lap(1.9, 85.0)
    assert 0.035 < v < 0.045
    assert ap.fuel_effect_s_per_lap(None, 85) is None
    assert ap.fuel_effect_s_per_lap(0, 85) is None


def test_no_fuel_data_gives_none_correction():
    laps = [{"n": n, "t": 90 + 0.1 * n, "clean": True, "lap": {}} for n in range(1, 13)]
    s = ap.stint(laps, laps, {}, [])
    assert s["fuel_corrected_deg"] is None and s["deg_s_per_lap"] > 0


def test_phases_split_into_thirds():
    laps = [lap(n, 90 + n) for n in range(1, 11)]  # 10 laps -> 3 / 4 / 3
    s = ap.stint(laps, laps, {}, [])
    assert [p["name"] for p in s["phases"]] == ["Early", "Middle", "Late"]
    assert s["phases"][0]["laps"] == [1, 2, 3] and s["phases"][2]["laps"] == [8, 9, 10]
    assert s["phases"][1]["laps"] == [4, 5, 6, 7]
    assert s["phases"][0]["best_t"] == 91 and s["phases"][2]["avg_t"] == 99


def test_corner_drift_sign_late_minus_early():
    laps = [lap(n, 90.0) for n in range(1, 10)]
    # corner gets slower and has less min speed late on
    per = {"C1": {n: M(5.0 + (0.3 if n >= 7 else 0.0), min_mph=60.0 - (4 if n >= 7 else 0)) for n in range(1, 10)}}
    s = ap.stint(laps, laps, per, ["C1"])
    d = s["corner_drift"][0]
    assert d["delta_t"] > 0 and abs(d["delta_t"] - 0.3) < 1e-6
    assert d["delta_mph"] < 0 and abs(d["delta_mph"] + 4) < 1e-6
    assert abs(d["late_t"] - d["early_t"] - d["delta_t"]) < 1e-6


def test_too_few_laps_is_safe():
    laps = [lap(1, 90), lap(2, 91)]
    s = ap.stint(laps, laps, {}, [])
    assert s["deg_s_per_lap"] is None and s["phases"] == [] and s["corner_drift"] == []
    assert isinstance(s["summary"], str)


# ---------------------------------------------------------------- technique
def test_vs_ref_is_typical_minus_ref():
    corners = [{"id": "C1", "apex_m": 500.0}]
    # typical: brake at 100 m, 60 mph. ref: brakes at 120 m (later), carries 63 mph
    per = {"C1": {n: M(5.0, brake_on=100.0, min_mph=60.0, full_thr=300.0) for n in range(1, 6)}}
    ref = {"name": "Jake Morrow", "main": True, "corners": {"C1": M(4.8, brake_on=120.0, min_mph=63.0, full_thr=280.0)}}
    out = ap.technique(corners, per, set(range(1, 6)), 1, [ref])[0]
    assert out["vs_ref"]["brake_on"] == -20.0     # negative = you brake EARLIER
    assert out["vs_ref"]["min_mph"] == -3.0       # negative = you're slower at the apex
    assert out["vs_ref"]["full_thr"] == 20.0      # positive = you're flat LATER
    joined = " ".join(out["notes"])
    assert "brake 20 m earlier than Jake" in joined
    assert "3 mph less" in joined
    assert "full throttle 20 m later" in joined


def test_typical_uses_clean_laps_only_and_median():
    corners = [{"id": "C1", "apex_m": 500.0}]
    per = {"C1": {1: M(5, min_mph=50), 2: M(5, min_mph=60), 3: M(5, min_mph=61), 4: M(5, min_mph=62), 5: M(9, min_mph=10)}}
    out = ap.technique(corners, per, {1, 2, 3, 4}, 2, [])[0]
    assert out["typical"]["min_mph"] == 60.5
    assert out["best"]["min_mph"] == 60.0
    assert out["ref"] is None and out["vs_ref"] is None


def test_technique_gear_is_mode():
    rows = [M(5, gear=3), M(5, gear=3), M(5, gear=4)]
    assert ap.typical_metrics(rows)["gear"] == 3


def test_missing_brake_point_is_none_not_crash():
    corners = [{"id": "C1", "apex_m": 500.0}]
    per = {"C1": {n: M(5, brake_on=None) for n in range(1, 4)}}
    ref = {"name": "A B", "main": True, "corners": {"C1": M(5, brake_on=200.0)}}
    out = ap.technique(corners, per, {1, 2, 3}, 1, [ref])[0]
    assert out["typical"]["brake_on"] is None and out["vs_ref"]["brake_on"] is None


# ---------------------------------------------------------------- consistency
def test_consistency_formula():
    assert ap.consistency_score(0.0, 100.0) == 100
    assert ap.consistency_score(1.2, 0.0) == 0
    assert ap.consistency_score(5.0, 0.0) == 0          # clamps
    assert ap.consistency_score(0.6, 50.0) == round(100 * (0.6 * 0.5 + 0.4 * 0.5))
    assert ap.score_label(80) == "Very consistent" and ap.score_label(60) == "Consistent"
    assert ap.score_label(40) == "Up and down" and ap.score_label(10) == "Scattered"


def test_consistency_within_percentages_relative_to_best():
    clean = [{"n": i + 1, "t": t} for i, t in enumerate([90.0, 90.3, 90.5, 90.9, 91.5])]
    c = ap.consistency(clean, [], {})
    assert c["within_05"] == 60.0     # 90.0, 90.3, 90.5
    assert c["within_10"] == 80.0     # + 90.9
    assert abs(c["std"] - float(np.std([90.0, 90.3, 90.5, 90.9, 91.5], ddof=1))) < 1e-3
    assert 0 <= c["score"] <= 100


def test_consistency_iqr_labels():
    corners = [{"id": "C1"}, {"id": "C2"}]
    per = {"C1": {n: M(5.0 + 0.01 * n) for n in range(1, 6)}, "C2": {n: M(5.0 + 0.3 * n) for n in range(1, 6)}}
    clean = [{"n": n, "t": 90 + 0.1 * n} for n in range(1, 6)]
    c = ap.consistency(clean, corners, per)
    lab = {b["id"]: b["label"] for b in c["by_corner"]}
    assert lab["C1"] == "Locked in" and lab["C2"] == "All over"


def test_consistency_needs_three_laps():
    assert ap.consistency([{"n": 1, "t": 90}, {"n": 2, "t": 91}], [], {}) is None


# ---------------------------------------------------------------- best vs worst
def test_best_worst_delta_positive_means_worst_lost_time():
    clean = [{"n": 1, "t": 90.0}, {"n": 2, "t": 91.0}, {"n": 3, "t": 90.5}]
    corners = [{"id": "C1"}, {"id": "C2"}]
    per = {"C1": {1: M(10.0, brake_on=200, min_mph=60), 2: M(10.8, brake_on=188, min_mph=57), 3: M(10.4)},
           "C2": {1: M(8.0), 2: M(7.9), 3: M(8.0)}}
    bw = ap.best_worst(clean, corners, per)
    assert bw["best_n"] == 1 and bw["worst_n"] == 2 and abs(bw["gap"] - 1.0) < 1e-9
    z = {x["id"]: x for x in bw["zones"]}
    assert abs(z["C1"]["delta_t"] - 0.8) < 1e-9       # worst - best, positive = lost
    assert abs(z["C2"]["delta_t"] + 0.1) < 1e-9       # worst lap was quicker here
    assert bw["zones"][0]["id"] == "C1"               # sorted by delta_t desc
    assert "Braked 12 m earlier" in z["C1"]["changes"]
    assert "3 mph less at the slowest point" in z["C1"]["changes"]
    assert bw["basis"] == "corner zones"


def test_best_worst_uses_segments_when_present():
    clean = [{"n": 1, "t": 90.0}, {"n": 2, "t": 91.0}]
    corners = [{"id": "C1"}, {"id": "C2"}]
    per = {"C1": {1: M(10.0), 2: M(10.1)}, "C2": {1: M(8.0), 2: M(8.0)}}
    seg = {"names": ["C1", "C2"], "laps": {"1": [45.0, 45.0], "2": [45.6, 45.4]}}
    bw = ap.best_worst(clean, corners, per, seg)
    z = {x["id"]: x["delta_t"] for x in bw["zones"]}
    assert abs(z["C1"] - 0.6) < 1e-9 and abs(z["C2"] - 0.4) < 1e-9
    assert abs(sum(z.values()) - bw["gap"]) < 1e-9 and bw["basis"] == "segments"


def test_change_sentences_directions():
    best = M(10, brake_on=200, peak_brake=70, min_mph=60, full_thr=400, coast_s=0.2, exit_mph=90, gear=3)
    worse = M(11, brake_on=215, peak_brake=90, min_mph=62, full_thr=380, coast_s=0.5, exit_mph=86, gear=2)
    s = ap.change_sentences(best, worse)
    assert "Braked 15 m later" in s
    assert "Harder on the brake (90% vs 70%)" in s
    assert "2 mph more at the slowest point" in s
    assert "Gear 2 instead of 3 at the slowest point" in s
    assert "0.3 s more coasting" in s
    assert "Full throttle 20 m sooner" in s
    assert "4 mph slower on the exit" in s


def test_best_worst_none_with_one_lap():
    assert ap.best_worst([{"n": 1, "t": 90}], [], {}) is None


# ---------------------------------------------------------------- context + extend
def test_race_context():
    raw = [{"lapNumber": 2, "_start": 200, "driverRating": 1500, "trackTemp": 30, "airTemp": 20, "trackWetness": 0,
            "trackUsage": 40, "track": {"id": 7}, "car": {"id": 9}, "season": {"id": 1, "name": "S1", "shortName": "s1"}},
           {"lapNumber": 1, "_start": 100, "driverRating": 1490, "trackTemp": 28, "airTemp": 20, "trackWetness": 0,
            "trackUsage": 40, "track": {"id": 7}, "car": {"id": 9}}]
    combo, ir, season, cond = ap.race_context(raw)
    assert combo == {"track_id": 7, "car_id": 9}
    assert ir == 1490                       # first lap of the race
    assert cond["track_temp_c"] == 29.0 and cond["air_temp_c"] == 20.0
    assert ap.race_context([]) == ({"track_id": None, "car_id": None}, None, None, None)


def test_extend_guards_failures():
    data = {}
    logs = []
    # corners malformed -> technique etc. fail, but extend must not raise and keys must exist
    ap.extend(data, laps=[], clean=[], best={"n": 1}, corners=[{"no_id": 1}], per={}, refs=[], raw_laps=[], log=logs.append)
    assert data["version"] == 2
    for k in ap.KEYS:
        assert k in data
    assert data["technique"] is None and logs


# ---------------------------------------------------------------- engine brake point
def test_brake_onset_bridges_trail_brake_and_ignores_taps():
    STEP = engine.STEP
    n = 80
    b = np.zeros(n)
    b[10:30] = 80          # main stop starts at cell 10
    b[30:33] = 2           # short release (15 m)
    b[33:38] = 20          # re-application while trail braking
    b[50:52] = 30          # small tap after a long gap (chicane)
    pre = np.arange(n)
    assert engine.brake_onset(b, pre) == 10
    assert engine.brake_onset(np.zeros(n), pre) is None
    assert int(round(engine.BRAKE_GAP_M / STEP)) >= 2



# ---------------------------------------------------------------- engine on a real race (mock Garage 61 data)
MOCKDATA = os.environ.get("DEBRIEF_MOCKDATA", "/home/claude/devtools/mockdata")
MOCK_EVENT = "01M5V47FTZXFBM4XN23S9GMSCN"   # has a 6.9 s partial lap 23 that Garage 61 marks incomplete


def mock_race(event=MOCK_EVENT, all_unclean=False):
    """engine.analyze() on a race from the mock Garage 61 data, prepared the way app.cmd_review does.
    None when the mock data isn't on this machine."""
    import json
    from datetime import datetime
    from debrief import app
    f = os.path.join(MOCKDATA, "data.json")
    if not os.path.exists(f):
        return None
    d = json.load(open(f, encoding="utf-8"))
    laps, tel = [], {}
    for l in d["laps"]:
        if l.get("event") != event or l.get("sessionType") != 3:
            continue
        p = os.path.join(MOCKDATA, "csv", l["id"] + ".csv")
        if not os.path.exists(p):
            continue
        l = dict(l)
        l["clean"] = False if all_unclean else app.is_clean(l)
        l["_start"] = datetime.fromisoformat(l["startTime"].replace("Z", "+00:00")).timestamp()
        laps.append(l)
        tel[l["id"]] = open(p, encoding="utf-8").read()
    meta = {"driver": "Alex Driver", "event_id": event, "track": "Charlotte Motor Speedway (Roval 2019)",
            "car": "Ford Mustang GT4", "started_at": min(l["startTime"] for l in laps), "session": "Race"}
    return engine.analyze(meta, laps, tel, [], {}, track_length=None)


def test_partial_laps_dropped_and_corner_bests_from_clean_laps():
    data = mock_race()
    if data is None:
        print("  SKIPPED: mock data not found")
        return
    ns = [l["n"] for l in data["laps"]]
    assert 23 not in ns and 23 not in data["heat"]["laps"], ns          # the 6.9 s partial lap is gone
    assert data["counts"]["partial"] >= 1
    clean = {l["n"] for l in data["laps"] if l["clean"]}
    for c in data["corners"]:
        assert "23" not in c["per_lap"], c["id"]
        assert c["best_t"] > 0.5, (c["id"], c["best_t"])               # never a 0.0 s "best" from a partial lap
        assert c["best_lap"] in clean, (c["id"], c["best_lap"])          # per-corner best comes from clean laps
        assert abs(c["best_t"] - min(c["per_lap"][str(n)]["t"] for n in clean if str(n) in c["per_lap"])) < 1e-3
    assert data["best"]["clean"] is True and data["best"]["t"] > 80


def test_all_laps_unclean_marks_best_not_clean():
    data = mock_race(all_unclean=True)
    if data is None:
        print("  SKIPPED: mock data not found")
        return
    assert data["best"]["clean"] is False        # display only: never a PB / goal / team time
    assert 23 not in [l["n"] for l in data["laps"]]
    assert all(c["best_t"] > 0.5 for c in data["corners"])


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except Exception as e:  # noqa: BLE001
                fails += 1
                print("FAIL", name, type(e).__name__, e)
    print(f"{fails} failed" if fails else "all passed")
    sys.exit(1 if fails else 0)
