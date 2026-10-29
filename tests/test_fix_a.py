"""Regression tests for the Debrief 2.2.2 analysis fixes (team A).

    python3 tests/test_fix_a.py

One test per finding id (review 1-analysis.md: C*, H*, M*, L*, N*; test run tests/analysis/FINDINGS.md: A*).
Synthetic telemetry only (tests/fixtures_a/synth.py); no network.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "fixtures_a"))
_T = tempfile.mkdtemp(prefix="debrief-fixa-")
for _k in ("DEBRIEF_SETTINGS", "DEBRIEF_CACHE_DIR", "DEBRIEF_SCHEDULE_DIR", "DEBRIEF_UPDATE_STATE"):
    os.environ.setdefault(_k, os.path.join(_T, _k.lower()))
warnings.simplefilter("ignore")

import numpy as np  # noqa: E402

import synth  # noqa: E402
from debrief import analysis_plus as ap  # noqa: E402
from debrief import app, engine, insights, library, prep  # noqa: E402

_CACHE = {}


def race(**kw):
    key = json.dumps(kw, sort_keys=True, default=str)
    if key not in _CACHE:
        _CACHE[key] = synth.race(**kw)
    meta, laps, tel = _CACHE[key]
    return dict(meta), [dict(l) for l in laps], dict(tel)


def run(meta, laps, tel, refs=(), ref_tel=None, **kw):
    return engine.analyze(meta, laps, tel, list(refs), ref_tel or {}, **kw)


def strict_json(d):
    json.dumps(d, allow_nan=False, default=str)


class FakeApi:
    def __init__(self, laps):
        self._laps = laps

    def teams(self):
        return [{"slug": "mm"}]

    def find_laps(self, **p):
        return [dict(l) for l in self._laps]


# ---------------------------------------------------------------- Critical
def test_C1_partial_laps_never_best_or_comparison():
    # my best: the 6.9 s partial lap at the line never counts
    mine = [{"id": f"m{i}", "lapTime": 85.0 + i * 0.1} for i in range(10)] + \
           [{"id": "m23", "lapTime": 6.9}, {"id": "m24", "lapTime": 7.1, "incomplete": True},
            {"id": "pit", "lapTime": 70.0, "pitIn": True}]
    assert app.full_lap_best(mine) == 85.0
    # pick_refs: a teammate's 6.4 s partial "best" (flagged or not) is never picked
    api = FakeApi([{"id": "t1", "lapTime": 84.2, "clean": True, "driver": {"slug": "a"}},
                   {"id": "t2", "lapTime": 6.4, "clean": True, "incomplete": True, "driver": {"slug": "b"}},
                   {"id": "t4", "lapTime": 6.6, "clean": True, "driver": {"slug": "d"}},
                   {"id": "t3", "lapTime": 85.0, "clean": True, "driver": {"slug": "c"}}])
    for my_best in (85.0, 6.9):
        refs = app.pick_refs(api, 1, 1, my_best, "me", 3)
        assert {r["id"] for r in refs} <= {"t1", "t3"}, refs
    # analyze: a partial comparison lap (6.4 s, 7% of the lap) is left out, not compared against
    meta, laps, tel = race(n=15)
    lines = tel["lap1"].splitlines()
    part = "\n".join(lines[: int(len(lines) * 0.07)])
    full, t = synth.lap_csv({})
    refs = [{"id": "R", "lapTime": 6.4, "driver": {"firstName": "Mate"}, "_main": True},
            {"id": "R2", "lapTime": round(t, 3), "driver": {"firstName": "Good"}},
            {"id": "R3", "lapTime": round(t, 3), "driver": {"firstName": "Pit"}, "pitOut": True}]
    d = run(meta, laps, tel, refs, {"R": part, "R2": full, "R3": full})
    assert [r["name"] for r in d["refs"]] == ["Good"] and d["refs"][0]["main"], d["refs"]
    for c in d["corners"]:
        assert c["ref_gap"] is None or abs(c["ref_gap"]) < 2, (c["id"], c["ref_gap"])
    # a full-length lap time with telemetry for only half the lap is not a comparison either
    half = "\n".join(full.splitlines()[: len(full.splitlines()) // 2])
    d = run(meta, laps, tel, [{"id": "H", "lapTime": round(t, 3), "_main": True}], {"H": half})
    assert d["refs"] == []
    # one of my own laps whose telemetry covers half the lap never becomes best or a corner best
    meta, laps, tel = race(n=12)
    tel["lap4"] = "\n".join(tel["lap4"].splitlines()[:1800])
    d = run(meta, laps, tel)
    assert 4 not in [l["n"] for l in d["laps"]] and d["counts"]["partial"] == 1


def test_C2_blank_or_garbled_cells_do_not_corrupt_a_lap():
    meta, laps, tel = race(n=12)
    good = run(meta, laps, tel)
    for col in ("LapDistPct", "Speed"):
        for val in ("", "nan", "garbage", "inf"):
            t2 = dict(tel)
            lines = t2["lap5"].splitlines()
            k = lines[0].split(",").index(col)
            for i in (len(lines) // 3, len(lines) // 2):
                cells = lines[i].split(",")
                cells[k] = val
                lines[i] = ",".join(cells)
            t2["lap5"] = "\n".join(lines)
            bad = run(meta, laps, t2)
            for cg, cb in zip(good["corners"], bad["corners"]):
                a, b = cg["per_lap"]["5"], cb["per_lap"]["5"]
                assert abs(a["t"] - b["t"]) < 0.05, (col, val, cg["id"], a["t"], b["t"])
                assert abs(a["min_mph"] - b["min_mph"]) < 1.0, (col, val, cg["id"])
                assert abs(cg["best_t"] - cb["best_t"]) < 0.05
            assert abs(good["segments"]["possible"] - bad["segments"]["possible"]) < 0.05
            strict_json(bad)
    # a mid-lap LapDistPct glitch (0.5 -> 0.0 -> 0.5) is not a start/finish crossing
    raw = {"p": np.array([0.1, 0.3, 0.5, 0.0, 0.55, 0.8, 0.99])}
    p = engine._unwrap(raw["p"])
    assert p.max() < 1.0
    # a real crossing (lap starting just before the line) still unwraps
    p = engine._unwrap(np.array([0.98, 0.99, 0.01, 0.4, 0.9]))
    assert list(np.round(p, 2)) == [-0.02, -0.01, 0.01, 0.4, 0.9]


def test_C2_sanity_gate_drops_impossible_corner_times():
    # a broken lap whose corner time is under a quarter of the corner median never sets a best
    meta, laps, tel = race(n=12)
    d = run(meta, laps, tel)
    for c in d["corners"]:
        ts = [m["t"] for m in c["per_lap"].values()]
        assert min(ts) > 0.25 * float(np.median(ts))


# ---------------------------------------------------------------- High
def test_H1_consistency_sees_mistake_laps():
    slow = {i: (1100, 0.25) for i in (3, 5, 7, 9, 11, 13, 15, 17)}
    meta, laps, tel = race(n=20, noise=0.3, slow_laps=slow)
    d = run(meta, laps, tel)
    assert set(slow) <= set(d["counts"]["slow"])
    c = d["consistency"]
    assert c["n"] == 20 and c["off_pace_n"] == 8 and c["score"] < 70, c
    assert "8 of 20" in c["summary"]
    good = {g["key"] for g in insights.build(d)["good"]}
    assert not {"consistent", "locked"} & good, good
    # std_clean covers the mistake laps too
    assert d["std_clean"] > 0.5
    # without mistakes the same driver is still consistent
    meta, laps, tel = race(n=20, noise=0.3)
    d = run(meta, laps, tel)
    assert d["consistency"]["score"] >= 70 and d["consistency"]["off_pace_n"] == 0


def test_H2_spin_lap_still_counts_as_unclean():
    meta, laps, tel = race(n=20)
    med = sorted(l["lapTime"] for l in laps)[10]
    laps[14]["lapTime"] = round(med * 1.6, 3)
    laps[14]["clean"] = False
    d = run(meta, laps, tel)
    assert d["counts"]["unclean"] == [15] and d["counts"]["excluded_laps"] == [15], d["counts"]
    ins = insights.build(d)
    assert "clean" not in {g["key"] for g in ins["good"]}
    assert any("15" in f for f in d["findings"] if "tow or spin" in f)
    # an old review (no excluded_laps) that left laps out never claims "Clean race"
    old = dict(d, counts={k: v for k, v in d["counts"].items() if k not in ("excluded_laps",)})
    old["counts"]["unclean"] = []
    assert "clean" not in {g["key"] for g in insights.build(old)["good"]}


def _noise_hits(n, trials, rng):
    hits = 0
    for _ in range(trials):
        y = rng.normal(0, 0.2, n)
        if any(engine.pattern(list(rng.normal(0, 10, n)), list(y)) for _ in range(engine.PATTERN_METRICS)):
            hits += 1
    return hits / trials


def test_H3_coaching_patterns_are_not_noise():
    rng = np.random.default_rng(7)
    for n in (10, 15, 20, 30):
        rate = _noise_hits(n, 1500, rng)
        assert rate < 0.065, (n, rate)       # ~5% target; 1500 trials gives about +-1.1% sampling error
    # under 10 laps nothing is ever claimed
    assert engine.pattern(list(range(9)), list(range(9))) is None
    # a real, strong link is still found
    x = rng.normal(0, 10, 20)
    y = 0.02 * x + rng.normal(0, 0.03, 20)
    hit = engine.pattern(list(x), list(y))
    assert hit and hit[0] > 0.8
    # ...but not one too small to matter (0.001 s per unit over a ~13-unit spread)
    y = 0.0005 * x + rng.normal(0, 0.0001, 20)
    assert engine.pattern(list(x), list(y)) is None
    # end to end: pure-noise corners rarely get a "driver"
    meta, laps, tel = race(n=15, seed=11)
    d = run(meta, laps, tel)
    drv = [z for c in d["corners"] for z in c["drivers"]]
    assert all(abs(z["r"]) >= z["r_needed"] and z["n"] >= 10 for z in drv)
    # prep ignores the weak patterns old reviews kept
    assert not prep._strong({"r": -0.55, "n": 10}) and prep._strong({"r": -0.9, "n": 10})


def test_H4_pit_laps_are_not_mistakes():
    meta, laps, tel = race(n=20)
    med = sorted(l["lapTime"] for l in laps)[10]
    laps[9]["lapTime"] = round(med * 1.12, 3)
    laps[9]["pitIn"] = True
    laps[10]["lapTime"] = round(med * 1.18, 3)
    laps[10]["pitOut"] = True
    d = run(meta, laps, tel)
    assert d["counts"]["pit"] == [10, 11] and not ({10, 11} & set(d["counts"]["slow"]))
    assert 10 not in [l["n"] for l in d["laps"]]
    assert any("Pit stop" in f and "10, 11" in f for f in d["findings"])
    ins = insights.build(d)
    assert "offpace" not in {b["key"] for b in ins["bad"]}
    assert any("Pit stop" in v for v in ins["verdict"])
    assert not any("10" in p["text"] for p in d["plan"] if p["title"] == "Look at your off laps")


# ---------------------------------------------------------------- Medium
def test_M1_A4_all_unclean_wording_and_plan():
    meta, laps, tel = race(n=12, unclean=list(range(1, 13)))
    d = run(meta, laps, tel)
    assert d["best"]["clean"] is False and d["std_clean"] is None
    assert "Best clean lap" not in d["findings"][0] and "Best lap" in d["findings"][0]
    assert not any(p["title"] == "Look at your off laps" for p in d["plan"])
    assert any(p["title"] == "Flagged laps" for p in d["plan"])


def test_M2_tips_copy_your_quick_laps_not_a_slow_corner_of_the_best_lap():
    meta, laps, tel = race(n=20, noise=1.5)
    d = run(meta, laps, tel)
    ins = insights.build(d)
    for t in d["technique"]:
        assert t["quick"] and t["quick"]["n"] >= 2
        assert all("your best lap" not in s for s in ins["tips"].get(t["id"], []))
        if not t["best_quick"]:   # best lap slower than usual here: no "On your best lap you..." note
            assert not any(n.startswith("On your best lap") for n in t["notes"])
    # old reviews (no technique.quick): computed from per_lap, or no tips when the best lap was slow there
    tech = {"id": "C1", "typical": {"min_mph": 50.0}, "best": {"min_mph": 40.0, "t": 11.0}}
    corner = {"id": "C1", "avail": 0.2, "p25_t": 10.0,
              "per_lap": {str(n): {"t": 10.0 + 0.1 * n, "min_mph": 60.0 - n} for n in range(1, 9)}}
    out = insights._with_quick(tech, corner, {str(n) for n in range(1, 9)})
    assert out["quick"]["min_mph"] > 57 and any("more" in s for s in insights.corner_tips(out))
    assert insights.corner_tips({"typical": {"min_mph": 50.0}, "best": {"min_mph": 40.0}, "best_quick": False}) == []


def test_M3_heatmap_baseline_is_clean_laps():
    meta, laps, tel = race(n=12)
    txt, t = synth.lap_csv({1: 8.0})
    tel["lap5"] = txt
    laps[4]["lapTime"] = round(t, 3)
    laps[4]["clean"] = False
    d = run(meta, laps, tel)
    j = d["heat"]["zones"].index("C2")
    clean_vals = [row[j] for row, n in zip(d["heat"]["values"], d["heat"]["laps"]) if n != 5 and row[j] is not None]
    assert abs(min(clean_vals)) < 1e-6, clean_vals
    assert max(clean_vals) < 0.5


def test_M4_corner_braking_across_the_line():
    apexes = [(60, 20.0), (1100, 30.0), (1700, 18.0), (2500, 35.0)]
    meta, laps, tel = race(n=12, apexes=apexes)
    d = run(meta, laps, tel)
    c1 = min(d["corners"], key=lambda c: c["apex_m"])
    assert c1["apex_m"] < 100 and c1["start_m"] < 0, c1
    others = [c["summary"]["peak_brake"] for c in d["corners"] if c is not c1]
    assert c1["summary"]["peak_brake"] >= min(others) - 5
    assert c1["summary"]["brake_on"] < 0       # braking starts before the line
    assert abs(sum(d["segments"]["laps"]["3"]) - d["laps"][2]["t"]) < 0.01
    # brake-point coaching is given as metres before the apex, never a negative lap distance
    import re
    z = {"metric": "brake_on", "r": -0.9, "p25": -120.0, "p75": -80.0, "median": -100.0}
    txt = engine.coaching_text(c1, c1["summary"], [z], [])
    assert "before the apex" in txt and not re.search(r"-\d+ m", txt), txt


def test_M5_missing_pedal_channels_are_not_coasting():
    meta, laps, tel = race(n=12, drop_cols=("Throttle", "Brake"))
    logs = []
    d = run(meta, laps, tel, log=logs.append)
    assert d["channels_missing"] == ["Brake", "Throttle"] and any("no Brake" in m for m in logs)
    s = d["style"][str(d["best"]["n"])]
    assert s["coast_s"] is None and s["full_thr_pct"] is None
    c = d["corners"][0]["summary"]
    assert c["coast_s"] is None and c["peak_brake"] is None and c["min_mph"] > 0
    ins = insights.build(d)
    assert "coast" not in {b["key"] for b in ins["bad"]}
    strict_json(d)


def test_M6_lap_numbers_match_garage61_when_telemetry_is_missing():
    meta, laps, tel = race(n=10)
    for l in laps:
        l.pop("lapNumber")
    del tel["lap1"], tel["lap4"]
    d = run(meta, laps, tel)
    assert [l["n"] for l in d["laps"]] == [2, 3, 5, 6, 7, 8, 9, 10]


def test_M7_season_and_prep_ignore_unclean_bests():
    rows = [{"date": "2026-10-01", "started_at": "", "season": {"name": "S4"}, "combo": "A|B", "track_short": "A",
             "car": "B", "best": 84.9, "best_clean": True, "path": "p1", "median": 85.5, "track": "A"},
            {"date": "2026-10-08", "started_at": "", "season": {"name": "S4"}, "combo": "A|B", "track_short": "A",
             "car": "B", "best": 6.9, "best_clean": False, "path": "p2", "median": 85.5, "track": "A"}]
    assert library.season_summary(rows)["seasons"][0]["combos"][0]["best"] == 84.9
    h = prep._history(rows)
    assert h["best"] == 84.9 and h["median_of_bests"] == 84.9 and h["trend_s_per_race"] is None
    assert [r["best_clean"] for r in h["races_list"]] == [True, False]


def test_M8_offpace_threshold_scales_with_lap_length():
    assert abs(engine.slow_cut_for(85) - 1.02) < 0.01
    assert engine.slow_cut_for(30) == 0.5
    assert abs(engine.slow_cut_for(480) - 5.76) < 0.01
    assert abs(insights._scale(30) - 0.333) < 0.01 and insights._scale(90) == 1.0
    meta, laps, tel = race(n=12)
    assert run(meta, laps, tel)["slow_cut"] > 0


def test_M9_coaching_is_correlational():
    banned = ("didn't help", "isn't what's costing", "Brake later or get back", "it's scrubbing speed",
              "focus on the entry instead")
    src = (ROOT / "debrief" / "engine.py").read_text() + (ROOT / "debrief" / "insights.py").read_text()
    for b in banned:
        assert b not in src, b
    # every coaching sentence built from a pattern stays "on your quicker laps..." or "laps with..."
    z = {"r": 0.9, "p25": 1.0, "p75": 2.0, "median": 1.5}
    for k in ("exit_mph", "abs_s"):
        txt = engine.coaching_text({"id": "C1", "apex_m": 500.0}, {}, [{**z, "metric": k}], [])
        assert txt.startswith("On your quicker laps") or txt.startswith("Laps with"), txt


# ---------------------------------------------------------------- Low
def test_L1_A5_skip_warning_logged_every_review():
    for _ in range(2):
        meta, laps, tel = race(n=6)
        tel["lap3"] = "garbage\n1,2"
        logs = []
        run(meta, laps, tel, log=logs.append)
        assert any("Skipping" in m for m in logs)


def test_L2_summary_typical_matches_technique_typical():
    meta, laps, tel = race(n=15, unclean=(2, 3))
    d = run(meta, laps, tel)
    tech = {t["id"]: t for t in d["technique"]}
    for c in d["corners"]:
        assert abs(c["summary"]["min_mph"] - tech[c["id"]]["typical"]["min_mph"]) < 0.01


def test_L3_best_possible_lap_uses_one_rule():
    meta, laps, tel = race(n=12, slow_laps={6: (1100, 0.3)}, unclean=(9,))
    for l in laps:
        l["sectors"] = [{"sectorTime": l["lapTime"] / 2}, {"sectorTime": l["lapTime"] / 2}]
    d = run(meta, laps, tel)
    assert 6 in d["counts"]["slow"]
    # segments and sectors both use Garage 61-clean laps, off-pace included, flagged laps not
    assert 6 in d["segments"]["eligible"] and 9 not in d["segments"]["eligible"]
    pace_ns = {l["lapNumber"] for l in laps if l["clean"]}
    assert set(d["segments"]["eligible"]) == pace_ns
    # the sector "best possible lap" takes the off-pace lap's quick first sector too (before 2.2.2 it didn't)
    laps[5]["sectors"] = [{"sectorTime": 20.0}, {"sectorTime": laps[5]["lapTime"] - 20.0}]
    d = run(meta, laps, tel)
    s2 = min(l["sectors"][1]["sectorTime"] for l in laps if l["clean"])
    assert d["optimal"] is not None and abs(d["optimal"] - (20.0 + s2)) < 1e-6, d["optimal"]


def test_L6_on_track_ignores_partial_bests():
    class Api(FakeApi):
        def cars(self, refresh=False):
            return [{"id": 7, "name": "Car"}]

    api = Api([{"id": "a", "lapTime": 6.4, "incomplete": True, "event": "E", "car": 7, "driver": {"slug": "x"}},
               {"id": "b", "lapTime": 85.0, "event": "E", "car": 7, "driver": {"slug": "x"}}])
    got = app.on_track(api, 1, "E", "me", 7)
    assert got["drivers"][0]["best"] == 85.0


def test_L7_plan_leaves_out_lap1_and_caps_the_list():
    ranked = []
    p = engine.build_plan(ranked, 0.0, 10, [1, 3, 4, 5, 6, 7, 8, 9], [])
    txt = next(x["text"] for x in p if x["title"] == "Look at your off laps")
    assert txt.startswith("Laps 3, 4, 5, 6, 7 (and 2 more)"), txt
    assert not any(x["title"] == "Look at your off laps" for x in engine.build_plan([], 0, 10, [1], []))


def test_L8_library_ref_gap_only_for_clean_best():
    d = {"meta": {"track": "T", "car": "C", "started_at": "2026-10-01T18:00:00+00:00"},
         "laps": [{"n": 1, "t": 90.0, "clean": False}, {"n": 2, "t": 91.0, "clean": False}],
         "best": {"n": 1, "t": 90.0, "clean": False}, "median": 90.5,
         "refs": [{"t": 89.0, "main": True}], "corners": []}
    folder = Path(tempfile.mkdtemp()) / "reviews" / "x"
    folder.mkdir(parents=True)
    (folder / "review.json").write_text(json.dumps(d))
    assert library._row(folder / "review.json")["ref_gap"] is None
    d["best"]["clean"] = True
    (folder / "review.json").write_text(json.dumps(d))
    assert library._row(folder / "review.json")["ref_gap"] == 1.0


def test_N4_oval_kept_in_short_name():
    assert library.short_track_name("Charlotte Motor Speedway (Oval)") != library.short_track_name(
        "Charlotte Motor Speedway (Roval 2019)")
    assert library.short_track_name("Charlotte Motor Speedway (Oval)").endswith("Oval")


# ---------------------------------------------------------------- test-run findings
def test_A1_header_only_or_blank_comparison_csv_is_skipped():
    meta, laps, tel = race(n=6)
    for bad in ("Speed,LapDistPct\n", "   \n", "", "Speed,LapDistPct\n1,0.1\n"):
        d = run(meta, laps, tel, [{"id": "R", "lapTime": 56.0, "driverName": "Ref", "_main": True}], {"R": bad})
        assert d["refs"] == []
    for bad in ("Speed,LapDistPct\n", " \n", ""):
        try:
            engine.parse_csv(bad)
            raise AssertionError("expected ValueError")
        except ValueError:
            pass


def test_A2_nan_speed_in_my_lap_is_ignored():
    meta, laps, tel = race(n=6)
    rows = tel["lap3"].split("\n")
    for i in (100, 101, 500):
        p = rows[i].split(",")
        p[0] = "nan"
        rows[i] = ",".join(p)
    tel["lap3"] = "\n".join(rows)
    d = run(meta, laps, tel)
    assert 3 in [l["n"] for l in d["laps"]]
    strict_json(d)


def test_A3_big_traffic_loss_is_traffic_not_a_mistake():
    meta, laps, tel = race(n=20, noise=0.3, slow_laps={7: (1950, 0.45), 13: (1950, 0.55)})
    d = run({**meta, "multiclass": True}, laps, tel)
    t = {l["n"]: l["t"] - d["median"] for l in d["laps"]}
    assert t[13] > d["slow_cut"], t[13]                      # loses more than the off-pace cut
    assert {7, 13} <= set(d["counts"]["traffic"]) and not ({7, 13} & set(d["counts"]["slow"])), d["counts"]
    # not multi-class: the same laps are off-pace laps
    d = run(meta, laps, tel)
    assert 13 in d["counts"]["slow"] and d["counts"]["traffic"] == []


# ---------------------------------------------------------------- lead request: salutation setting
def test_salutation_from_settings():
    assert app.normalise_salutation("  Sir ") == "Sir"
    assert app.normalise_salutation(None) == "" and app.normalise_salutation(True) == ""
    assert app.normalise_salutation("x" * 40) == "x" * 20
    assert app.normalise_salutation(7) == "7"
    src = (ROOT / "debrief" / "app.py").read_text()
    assert '"Sir"' not in src and 'normalise_salutation(cfg.get("salutation", ""))' in src
    p = Path(tempfile.mkdtemp()) / "settings.toml"
    p.write_text('token = "x"\nsalutation = "  Coach  "\n', encoding="utf-8")
    old = os.environ.get("DEBRIEF_SETTINGS")
    os.environ["DEBRIEF_SETTINGS"] = str(p)
    try:
        cfg = app.load_settings()
    finally:
        if old is None:
            os.environ.pop("DEBRIEF_SETTINGS", None)
        else:
            os.environ["DEBRIEF_SETTINGS"] = old
    assert cfg["salutation"] == "Coach"


# ---------------------------------------------------------------- insights interface (2.2.2)
def test_insights_detail_laps_corners_and_fix_numbers():
    slow = {i: (1700, 0.2) for i in (4, 8, 12)}
    meta, laps, tel = race(n=20, noise=1.0, unclean=(5, 9), slow_laps=slow)
    full, t = synth.lap_csv({k: 2.0 for k in range(4)})
    refs = [{"id": "R", "lapTime": round(t, 3), "driver": {"firstName": "Ann", "lastName": "Smith"}, "_main": True}]
    d = run(meta, laps, tel, refs, {"R": full})
    ins = insights.build(d)
    strict_json(ins)
    assert 1 <= len(ins["fix"]) <= insights.MAX_FIX == 5
    for f in ins["fix"]:
        assert set(f["numbers"]) >= {"best", "typical", "ref"}
        for side in ("best", "typical", "ref"):
            if f["numbers"][side] is not None:
                assert set(f["numbers"][side]) == {"brake_on", "min_mph", "exit_mph", "full_thr", "coast_s", "gear"}
        assert f["numbers"]["typical"] is not None and f["numbers"]["ref"] is not None
        assert set(f["times"]) == {"best_t", "typical_t", "p25_t", "ref_t"} and f["times"]["ref_t"] is not None
        assert f["corners"] == [f["corner"]] and isinstance(f.get("detail"), str)
    items = ins["bad"] + ins["good"]
    assert items and all({"key", "title", "text", "cost"} <= set(i) for i in items)
    keyed = {i["key"]: i for i in items}
    assert keyed["unclean"]["laps"] == [5, 9] and "2 of" in keyed["unclean"]["detail"]
    if "offpace" in keyed:
        assert {4, 12} <= set(keyed["offpace"]["laps"]) and keyed["offpace"]["detail"]
        assert not ({5, 9} & set(keyed["offpace"]["laps"]))     # flagged laps are "unclean", not off-pace
    for i in items:
        if "laps" in i:
            assert all(isinstance(n, int) for n in i["laps"])
        if "corners" in i:
            assert all(isinstance(c, str) for c in i["corners"])
    # up to 5 fix items
    corners = [{"id": f"C{i}", "avail": 0.1 * i, "apex_m": 100.0 * i} for i in range(1, 8)]
    out = insights.build({"laps": [], "best": {"n": 1, "t": 90.0}, "median": 90.0, "corners": corners})
    assert len(out["fix"]) == 5 and out["more_corners"] == 2
    # an old review without technique or per-lap data still builds (numbers None, no crash)
    assert all(f["numbers"]["best"] is None for f in out["fix"])
    strict_json(out)


def test_full_review_json_is_strict_and_renders():
    from debrief import report
    meta, laps, tel = race(n=12, unclean=(3,))
    d = run(meta, laps, tel)
    strict_json(d)
    out = Path(tempfile.mkdtemp()) / "rev"
    out.mkdir()
    page = report.write(out, d)
    assert page.exists() and json.loads((out / "review.json").read_text())["insights"]["fix"] is not None


if __name__ == "__main__":
    failed = 0
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        try:
            fn()
            print(f"ok    {name}")
        except Exception:  # noqa: BLE001
            import traceback
            failed += 1
            print(f"FAIL  {name}")
            traceback.print_exc()
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
