"""Debrief 2.3 workstream 23-A tests: technique detail (technique_plus), brake-point spread, engine channel
presence / lap start times, and the new insights items.

    python3 tests/test_23_a.py

Synthetic data only; no network (a test counts socket connections to prove it).
"""
from __future__ import annotations

import copy
import json
import os
import random
import re
import socket
import sys
import tempfile
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "fixtures_a"))
sys.path.insert(0, str(ROOT / "tests"))
_T = tempfile.mkdtemp(prefix="debrief-23a-")
for _k in ("DEBRIEF_SETTINGS", "DEBRIEF_CACHE_DIR", "DEBRIEF_SCHEDULE_DIR", "DEBRIEF_UPDATE_STATE"):
    os.environ.setdefault(_k, os.path.join(_T, _k.lower()))
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
warnings.simplefilter("ignore")

import numpy as np  # noqa: E402

import synth  # noqa: E402
from debrief import analysis_plus as ap  # noqa: E402
from debrief import engine, insights  # noqa: E402
from debrief import technique_plus as tp  # noqa: E402

FIX = ROOT / "tests" / "fixtures_23_a"
FORBIDDEN = re.compile(r"\b(will|will gain|costing|because|you should|the reason)\b", re.I)
C = {"id": "C1", "start_m": 200.0, "apex_m": 500.0, "end_m": 750.0}
_RACES = {}


# ---------------------------------------------------------------- synthetic R arrays (5 m grid)
def make_R(brake=None, throttle=None, steer=None, L=1000.0, T=60.0, missing=()):
    """One lap on the engine's 5 m grid with a single corner (apex 500 m, 40 mph). brake/throttle/steer are
    functions of distance (percent / degrees)."""
    d = np.arange(0, L, engine.STEP)
    v = 70.0 - 30.0 * np.clip(1 - np.abs(d - 500.0) / 250.0, 0, 1)
    b = np.array([brake(x) for x in d], float) if brake else np.zeros_like(d)
    th = np.array([throttle(x) for x in d], float) if throttle else np.where((d < 300) | (d > 600), 100.0, 0.0)
    st = np.array([steer(x) for x in d], float) if steer else np.zeros_like(d)
    tt = np.cumsum(np.r_[0, engine.STEP / (v[:-1] / engine.MPH)])
    tt *= T / (tt[-1] + engine.STEP / (v[-1] / engine.MPH))
    return {"d": d, "v": v, "b": b, "th": th, "st": st, "tt": tt, "gear": np.full_like(d, 3.0),
            "abs": np.zeros_like(d), "la": np.zeros_like(d), "lo": np.zeros_like(d), "rpm": np.zeros_like(d),
            "lat": np.full_like(d, 35.0), "lon": np.full_like(d, -80.0), "T": T, "_missing": list(missing)}


def trail(x, end=490.0):     # on at 300, peak 90 by 320, held to 380, then eased off linearly until `end`
    if x < 300 or x > end:
        return 0.0
    if x < 320:
        return 90.0 * (x - 300) / 20
    if x <= 380:
        return 90.0
    return 90.0 * (end - x) / (end - 380)


def step_brake(x):           # on at 300, full 90 until 380, straight off
    return 90.0 if 300 <= x <= 380 else 0.0


def ramp(x, dip=False):      # off the throttle 300..520, then a straight ramp to flat at 620
    if 300 <= x < 520:
        return 0.0
    if 520 <= x < 620:
        v = 100.0 * (x - 520) / 100
        if dip and 560 <= x < 575:
            v -= 15.0
        return max(0.0, v)
    return 100.0


def race(**kw):
    key = json.dumps(kw, sort_keys=True)
    if key not in _RACES:
        meta, laps, tel = synth.race(**kw)
        _RACES[key] = engine.analyze(meta, laps, tel, [], {})
    return copy.deepcopy(_RACES[key])


# ---------------------------------------------------------------- braking
def test_trail_brake_vs_step_release():
    t = tp.lap_corner(make_R(brake=trail, throttle=ramp), C)["braking"]
    s = tp.lap_corner(make_R(brake=step_brake, throttle=ramp), C)["braking"]
    assert t["trail_m"] > s["trail_m"] + 40, (t, s)
    assert t["shape_label"] == "gradual" and s["shape_label"] == "quick", (t["shape"], s["shape"])
    assert t["peak_at_m"] == 320.0 and t["brake_on_m"] == 305.0
    assert t["off_vs_apex_m"] < 0                       # off the brake before the slowest point
    assert 0 < t["trail_share"] <= 1


def test_release_after_apex_is_positive():
    b = tp.lap_corner(make_R(brake=lambda x: trail(x, end=560.0), throttle=lambda x: ramp(x) if x >= 560 or x < 300 else 0.0), C)["braking"]
    assert b["off_vs_apex_m"] > 0 and b["release_end_m"] > 500


def test_brake_held_to_end_of_window():
    b = tp.lap_corner(make_R(brake=lambda x: 50.0 if x >= 300 else 0.0), C)["braking"]
    assert b["held"] is True and b["release_end_m"] == C["end_m"]


def test_brush_of_brake_is_lift():
    b = tp.lap_corner(make_R(brake=lambda x: 12.0 if 350 <= x <= 400 else 0.0), C)["braking"]
    assert b is None


# ---------------------------------------------------------------- throttle
def test_throttle_dip_counts_one_lift():
    th = tp.lap_corner(make_R(brake=trail, throttle=lambda x: ramp(x, dip=True)), C)["throttle"]
    assert th["lifts"] == 1 and th["tv_excess_pct"] > 0


def test_monotone_ramp_no_lifts():
    th = tp.lap_corner(make_R(brake=trail, throttle=ramp), C)["throttle"]
    assert th["lifts"] == 0 and th["tv_excess_pct"] == 0
    assert th["pickup_m"] == 530.0 and th["pickup_vs_apex_m"] == 30.0      # first cell >= 10 %
    assert th["ramp_m"] == 85.0 and th["ramp_pct_per_10m"] == 10.0            # 10 % at 530 m, 95 % at 615 m


def test_lift_count_hysteresis():
    assert tp.count_lifts([10, 30, 50, 45, 55, 80, 95]) == 0         # 5-point wobble isn't a lift
    assert tp.count_lifts([10, 50, 38, 36, 60, 45, 90]) == 2
    assert tp.count_lifts([10, 50, 39, 41, 39, 41, 90]) == 1         # one lift, noise around the low


# ---------------------------------------------------------------- steering
def test_steering_corrections_counted_when_present():
    smooth = lambda x: 90.0 * max(0.0, 1 - abs(x - 480) / 200)                         # one turn in, one unwind
    saw = lambda x: smooth(x) + (6.0 if int(x / 20) % 2 else -6.0) * (380 <= x <= 560)  # sawing at the wheel
    a = tp.lap_corner(make_R(brake=trail, throttle=ramp, steer=smooth), C)["steering"]
    b = tp.lap_corner(make_R(brake=trail, throttle=ramp, steer=saw), C)["steering"]
    assert a["steer_corr"] == 1 and b["steer_corr"] > 3, (a, b)
    assert a["steer_peak_deg"] == 90.0 and a["unwind_vs_apex_m"] is not None


def test_steering_column_missing_gives_no_steering():
    d = race(n=12, drop_cols=("SteeringWheelAngle",))
    assert d["channels"]["steering"] is False and d["technique_plus"]["channels"]["steering"] is False
    assert all(c["steering"] is None for c in d["technique_plus"]["corners"])
    assert all(r["steer_corr"] is None for c in d["technique_plus"]["corners"] for r in c["per_lap"].values())


def test_flat_steering_column_same_as_missing():
    d = race(n=12)                    # synth writes SteeringWheelAngle = 0.0 on every sample
    assert d["channels"]["steering"] is False
    assert all(c["steering"] is None for c in d["technique_plus"]["corners"])
    R = make_R(steer=lambda x: 0.2)   # under 0.5 deg of movement is flat too
    assert engine.lap_channels(R)["steering"] is False


def test_lap_channels_from_missing_list_and_flatness():
    R = make_R(brake=trail, throttle=ramp, missing=["Brake"])
    ch = engine.lap_channels(R)
    assert ch["brake"] is False and ch["throttle"] is True and ch["gps"] is True
    assert engine.lap_channels(make_R(throttle=lambda x: 100.0))["throttle"] is False   # flat = not there


# ---------------------------------------------------------------- engine additions
def test_engine_channels_and_starts():
    d = race(n=12)
    assert d["channels"] == {"brake": True, "throttle": True, "steering": False, "gps": True}
    assert [l["start"] for l in d["laps"]][:3] == [1100.0, 1200.0, 1300.0]
    assert "technique_plus" in ap.KEYS and d["technique_plus"]["version"] == tp.VERSION
    json.dumps(d)   # serialisable


def test_engine_start_none_when_unknown():
    meta, laps, tel = synth.race(n=6)
    for l in laps:
        l.pop("_start")
    d = engine.analyze(meta, laps, tel, [], {})
    assert all(l["start"] is None for l in d["laps"])


def test_brake_channel_missing():
    d = race(n=12, drop_cols=("Brake",))
    assert d["channels"]["brake"] is False and d["technique_plus"]["channels"]["brake"] is False
    for c in d["technique_plus"]["corners"]:
        assert c["braking"] is None
        assert not any("brake" in n.lower() for n in c["notes"] if "Not enough" not in n and "look the same" not in n)
    assert all(not c["corners"] for c in [tp.spread_for_review(d)])   # brake_on is None everywhere


def test_technique_plus_block_shape():
    d = race(n=14)
    b = d["technique_plus"]
    assert b["reason"] is None and b["step_m"] == 5 and b["params"]["min_laps"] == 5
    c = b["corners"][0]
    for k in ("id", "apex_m", "n_clean", "lift", "braking", "throttle", "steering", "links", "notes", "per_lap"):
        assert k in c
    assert set(c["braking"]) == {"typical", "better"}
    for k in tp.BRAKE_KEYS + ("shape", "shape_label"):
        assert k in c["braking"]["typical"]
    for k in tp.THROTTLE_KEYS:
        assert k in c["throttle"]["typical"]
    assert len(c["per_lap"]) == 14 and isinstance(c["per_lap"]["1"]["lifts"], int)
    for c in b["corners"]:
        for note in c["notes"]:
            assert not FORBIDDEN.search(note), note


def test_not_enough_clean_laps():
    d = race(n=12, unclean=tuple(range(1, 9)))      # 4 clean laps
    for c in d["technique_plus"]["corners"]:
        assert c["braking"] is None and c["throttle"] is None
        assert c["notes"] == [tp.NOT_ENOUGH] and c["per_lap"]   # raw values still kept


def test_fewer_than_two_corners():
    assert tp.build([], [], [{"id": "C1", "start_m": 0, "apex_m": 50, "end_m": 100}], {}, None, 1000) == \
        {"version": 1, "corners": [], "reason": "no_corners"}


def test_extend_guard_keeps_review_when_build_fails():
    orig = tp.build
    tp.build = lambda *a, **k: 1 / 0
    try:
        logs = []
        data = {}
        ap.extend(data, laps=[], clean=[], best={"n": 1}, corners=[], per={}, refs=[], raw_laps=[], log=logs.append, L=1000)
        assert data["technique_plus"] is None and any("technique detail" in m for m in logs)
    finally:
        tp.build = orig


def test_link_notes_from_links():
    typ = {"braking": {"trail_m": 60.0}, "throttle": {"pickup_vs_apex_m": 30.0}}
    bet = {"braking": {"trail_m": 85.0}, "throttle": {"pickup_vs_apex_m": 12.0}}
    n = tp.notes_for([{"metric": "trail_m", "better": "higher"}], typ, bet)
    assert n == ["On your quicker laps here you tended to ease off the brake over a longer stretch (about 85 m against 60 m)."]
    n = tp.notes_for([{"metric": "pickup_vs_apex_m", "better": "lower"}], typ, bet)
    assert "sooner after the slowest point (about 12 m after it)" in n[0]
    assert tp.notes_for([{"metric": "lifts", "better": "lower"}], typ, bet)[0].startswith("Laps where you didn't lift")
    n = tp.notes_for([], typ, bet)
    assert n[0].startswith("Compared with your better laps, your typical lap starts the throttle 20 m later")
    assert tp.notes_for([], typ, typ) == [tp.SAME]
    for s in n:
        assert not FORBIDDEN.search(s)


def test_planted_link_in_build():
    """15 clean laps where a longer brake release goes with a quicker corner: build() finds the link."""
    rng = np.random.default_rng(3)
    laps, per = [], {"C1": {}, "C2": {}}
    corners = [dict(C), {"id": "C2", "start_m": 800.0, "apex_m": 900.0, "end_m": 990.0}]
    for n in range(1, 16):
        end = 430.0 + 5 * n                       # release ends 435..505 m: trail grows with n
        R = make_R(brake=lambda x, e=end: trail(x, end=e), throttle=lambda x, e=end: 0.0 if 300 <= x < max(520, e + 10) else ramp(x))
        laps.append({"n": n, "t": 60.0, "R": R, "clean": True})
        per["C1"][n] = {"t": 8.0 - 0.004 * (end - 430) + rng.normal(0, 0.004)}
        per["C2"][n] = {"t": 4.0}
    out = tp.build(laps, laps, corners, per, laps[0], 1000.0)
    c1 = out["corners"][0]
    assert c1["links"] and c1["links"][0]["metric"] in ("trail_m", "off_vs_apex_m"), c1["links"]
    lk = next(l for l in c1["links"] if l["metric"] == "trail_m")
    assert lk["better"] == "higher" and lk["effect_s"] >= 0.03 and lk["n"] == 15
    assert "longer stretch" in " ".join(c1["notes"]) or "closer to the slowest point" in " ".join(c1["notes"])


# ---------------------------------------------------------------- link() noise gate
def test_link_null_false_rate():
    rng = np.random.default_rng(11)
    hits = sum(tp.link(rng.normal(0, 10, 12), rng.normal(5, 0.2, 12)) is not None for _ in range(2000))
    assert hits / 2000 < 0.05, hits


def test_link_planted_effect_found():
    rng = np.random.default_rng(5)
    found, rhos = 0, []
    for _ in range(50):
        x = rng.uniform(0, 40, 14)
        y = 5.0 - 0.003 * x + rng.normal(0, 0.012, 14)    # effect ~0.06 s over the IQR, rho ~0.85
        rhos.append(abs(engine.spearman(x, y)))
        lk = tp.link(x, y)
        if lk:
            found += 1
            assert lk["better"] == "higher" and lk["effect_s"] >= 0.03 and lk["r"] < 0
    assert 0.7 < float(np.median(rhos)) < 0.95 and found >= 35, (found, np.median(rhos))


def test_link_needs_ten_laps_and_effect():
    x = list(range(9))
    assert tp.link(x, [5 - 0.01 * v for v in x]) is None          # 9 laps
    x = list(range(12))
    assert tp.link(x, [5 - 0.0001 * v for v in x]) is None        # perfect rank link, tiny effect
    assert tp.link(x, [5 - 0.01 * v for v in x])["better"] == "higher"


# ---------------------------------------------------------------- brake spread (old reviews)
def old_review():
    """A 2.2.1-format review.json (make_fixture_library) with per-lap brake points filled in."""
    import make_fixture_library as mfl
    d = mfl.fake_review(mfl.ROVAL, "2026-08-20T23:30:00+00:00", 0.0, False, mfl.S3, None, None, random.Random(61))
    for l in d["laps"]:
        l["clean"] = True
    spreads = {"C1": 2.0, "C2": 8.0, "C3": 20.0, "C4": 40.0, "C5": 2.0, "C6": 2.0}
    for c in d["corners"]:
        base = c["apex_m"] - 110
        c["per_lap"] = {str(n): {"t": 7.6, "brake_on": base + spreads[c["id"]] * ((n % 4) - 1.5) / 1.5}
                        for n in range(1, 21)}
    d["corners"][4]["per_lap"] = {str(n): {"t": 7.6, "brake_on": 100.0 + n} for n in range(1, 6)}   # 5 laps only
    d["corners"][5]["per_lap"] = {str(n): {"t": 7.6, "brake_on": None} for n in range(1, 21)}       # lift corner
    assert "technique_plus" not in d
    return d


def test_spread_labels_on_old_review():
    s = tp.spread_for_review(old_review())
    got = {c["id"]: c for c in s["corners"]}
    assert set(got) == {"C1", "C2", "C3", "C4"}                     # C5 (5 laps) and C6 (lift) left out
    assert [got[k]["label"] for k in ("C1", "C2", "C3", "C4")] == ["locked", "steady", "varies", "all over"]
    assert got["C3"]["n"] == 20 and len(got["C3"]["laps"]) == 20 and got["C3"]["std_m"] > 0
    assert s["params"] == {"locked_m": 5, "steady_m": 12, "varies_m": 25, "min_laps": 6}


def test_spread_label_thresholds():
    assert [tp.spread_label(v) for v in (5.0, 5.1, 12.0, 12.1, 25.0, 25.1)] == \
        ["locked", "steady", "steady", "varies", "varies", "all over"]


def test_spread_uses_clean_laps_only_and_tolerates_junk():
    d = old_review()
    for l in d["laps"][:15]:
        l["clean"] = False                                            # 5 clean laps left
    assert tp.spread_for_review(d)["corners"] == []
    assert tp.spread_for_review({}) is None and tp.spread_for_review({"corners": "x"}) is None
    assert tp.spread_for_review({"corners": [None, {"id": "C1", "per_lap": {"1": None}}]})["corners"] == []


# ---------------------------------------------------------------- insights
def _ins_base():
    d = old_review()
    d["consistency"] = None
    return d


def test_brake_spread_item_needs_n8_and_varies():
    d = _ins_base()
    out = insights.build(d)
    item = next(b for b in out["bad"] if b["key"] == "brake_spread")
    assert item["corners"] == ["C4", "C3"]                            # worst IQR first; steady/locked left out
    assert item["text"].startswith("Your brake point moved around in C4 (about ") and "m between laps) and C3." in item["text"]
    assert item["detail"].startswith("Middle half of your clean laps' brake points spanned")
    d["brake_spread"] = {"corners": [{"id": "C3", "n": 7, "iqr_m": 30.0, "label": "all over"},
                                     {"id": "C4", "n": 12, "iqr_m": 11.0, "label": "steady"}]}
    assert not any(b["key"] == "brake_spread" for b in insights.build(d)["bad"])


def test_brake_markers_good_item():
    d = _ins_base()
    d["brake_spread"] = {"corners": [{"id": "C1", "n": 12, "iqr_m": 3.0, "label": "locked"},
                                     {"id": "C2", "n": 12, "iqr_m": 4.0, "label": "locked"},
                                     {"id": "C5", "n": 12, "iqr_m": 18.0, "label": "varies"}]}
    g = next(x for x in insights.build(d)["good"] if x["key"] == "brake_markers")
    assert g["text"] == "You braked at the same marker lap after lap in C1 and C2." and g["corners"] == ["C1", "C2"]
    d["brake_spread"]["corners"][1]["label"] = "steady"
    assert not any(x["key"] == "brake_markers" for x in insights.build(d)["good"])


def _skills(exit_s, total_s, lap_t=85.0):
    pm = lambda s: s / lap_t * 1000
    other = (total_s - exit_s) / 3
    ph = {p: {"loss_s": other, "loss_pm": pm(other)} for p in ("braking", "entry", "mid")}
    ph["exit"] = {"loss_s": exit_s, "loss_pm": pm(exit_s)}
    return {"version": 1, "reason": None, "lap_t": lap_t, "phases": ph, "overall": {"loss_s": total_s, "loss_pm": pm(total_s)}}


def test_phase_item_threshold():
    d = _ins_base()
    d["skills"] = _skills(0.21, 0.465)
    it = next(b for b in insights.build(d)["bad"] if b["key"] == "phase")
    assert it["text"] == "Most of the gap between your typical and better laps was on corner exits (about 0.21 s a lap)."
    assert it["cost"] == 0.21
    d["skills"] = _skills(0.15, 0.465)                 # 1.76 per-mille: under 2.0
    assert not any(b["key"] == "phase" for b in insights.build(d)["bad"])
    d["skills"] = _skills(0.21, 0.6)                   # 35 % of the gap: under 40 %
    assert not any(b["key"] == "phase" for b in insights.build(d)["bad"])
    d["skills"] = {"version": 1, "reason": "not_enough_clean_laps"}
    assert not any(b["key"] == "phase" for b in insights.build(d)["bad"])


def test_fix_tips_get_technique_note_max_three():
    d = race(n=14)
    note = "On your quicker laps here you tended to ease off the brake over a longer stretch (about 85 m against 60 m)."
    for c in d["technique_plus"]["corners"]:
        c["notes"] = [note]
    for c in d["corners"]:
        c["avail"] = 0.3
    out = insights.build(d)
    assert out["fix"]
    for f in out["fix"]:
        assert len(f["tips"]) <= 3 and f["tips"][-1] == note


def test_steering_note_hidden_without_channel():
    d = race(n=14)
    for c in d["technique_plus"]["corners"]:
        c["notes"] = ["Laps with fewer steering corrections were usually quicker here."]
    for c in d["corners"]:
        c["avail"] = 0.3
    assert d["technique_plus"]["channels"]["steering"] is False
    out = insights.build(d)
    assert not any("steering" in t.lower() for f in out["fix"] for t in f["tips"])
    d["technique_plus"]["channels"]["steering"] = True
    out = insights.build(d)
    assert any("steering" in t.lower() for f in out["fix"] for t in f["tips"])


def test_new_insight_text_is_correlational():
    d = _ins_base()
    d["skills"] = _skills(0.21, 0.465)
    out = insights.build(d)
    for it in out["bad"] + out["good"]:
        if it["key"] in ("brake_spread", "brake_markers", "phase"):
            assert not FORBIDDEN.search(it["text"] + " " + it.get("detail", "")), it


NEW_KEYS = {"brake_spread", "brake_markers", "phase"}


def test_old_review_unchanged_except_new_items():
    import make_fixture_library as mfl
    rng = random.Random(61)
    for combo, started, gain, v2, season, ir, res in mfl.RACES:
        d = mfl.fake_review(combo, started, gain, v2, season, ir, res, rng)
        a = insights.build(copy.deepcopy(d))
        b = insights.build({**copy.deepcopy(d), "brake_spread": {"corners": []}})   # new items switched off
        strip = lambda o: {**o, "bad": [x for x in o["bad"] if x["key"] not in NEW_KEYS],
                           "good": [x for x in o["good"] if x["key"] not in NEW_KEYS]}
        assert strip(a) == b
    d = old_review()
    a = insights.build(copy.deepcopy(d))
    assert any(x["key"] == "brake_spread" for x in a["bad"])
    b = insights.build({**copy.deepcopy(d), "brake_spread": {"corners": []}})
    strip = lambda o: {**o, "bad": [x for x in o["bad"] if x["key"] not in NEW_KEYS],
                       "good": [x for x in o["good"] if x["key"] not in NEW_KEYS]}
    assert strip(a) == b


# ---------------------------------------------------------------- request budget: 0
def test_no_network():
    calls = []
    orig = socket.socket.connect

    def spy(self, *a, **k):
        calls.append(a)
        raise OSError("network blocked in test")
    socket.socket.connect = spy
    try:
        d = race(n=12)
        tp.spread_for_review(d)
        insights.build(d)
        tp.build([], [], [], {}, None, 1000)
    finally:
        socket.socket.connect = orig
    assert calls == []


# ---------------------------------------------------------------- day-1 fixtures match the real output
def _keys(o, prefix=""):
    out = set()
    if isinstance(o, dict):
        for k, v in o.items():
            if k in ("per_lap", "laps") and isinstance(v, dict):
                v = next(iter(v.values()), None)
                k = k + "[*]"
            out.add(prefix + k)
            out |= _keys(v, prefix + k + ".")
    elif isinstance(o, list) and o and isinstance(o[0], dict):
        out |= _keys(o[0], prefix + "[]")
    return out


def test_fixtures_match_real_shapes():
    d = race(n=14)
    s = json.loads((FIX / "sample_technique_plus.json").read_text())
    real, fx = _keys(d["technique_plus"]), _keys(s)
    assert real <= fx, sorted(real - fx)
    assert {"channels.steering", "corners.[]steering"} <= fx
    bs = json.loads((FIX / "sample_brake_spread.json").read_text())
    assert _keys(tp.spread_for_review(old_review())) <= _keys(bs)
    ex = json.loads((FIX / "sample_engine_extras.json").read_text())
    assert set(ex["channels"]) == set(d["channels"]) and set(ex["laps"][0]) == set(d["laps"][0])


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except Exception as e:  # noqa: BLE001
                fails += 1
                import traceback
                traceback.print_exc()
                print("FAIL", name, e)
    print(f"{fails} failed" if fails else "all passed")
    sys.exit(1 if fails else 0)
