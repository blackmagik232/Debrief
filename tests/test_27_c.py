"""Debrief 2.7.0 "Trust", workstream C: lap-length-scaled thresholds (M4), calibration fixtures (M3),
practice / qualifying vs race separation, honest labels, and the single-class traffic detector.

Run:  python3 tests/ci_run.py tests/test_27_c.py      (plain script: no pytest needed, Windows-safe)
Synthetic data only (tests/fixtures_tracks), no network, no /tmp paths (tempfile), utf-8 everywhere.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import numpy as np  # noqa: E402

import fixtures_tracks as ft  # noqa: E402
from debrief import analysis_plus as ap  # noqa: E402
from debrief import engine, insights, library, prep, profile, racecraft, report  # noqa: E402

_CACHE = {}


def review(shape, **kw):
    """engine.analyze output for one shape (cached per argument set: the long track takes a second)."""
    key = (shape, tuple(sorted((k, json.dumps(v, sort_keys=True, default=str)) for k, v in kw.items())))
    if key not in _CACHE:
        meta, laps, tel = ft.race(shape, **kw)
        _CACHE[key] = engine.analyze(meta, laps, tel, [], {}, log=lambda m: None)
    return _CACHE[key]


class Tmp:
    def __enter__(self):
        self.p = Path(tempfile.mkdtemp(prefix="debrief-27c-"))
        return self.p

    def __exit__(self, *a):
        shutil.rmtree(self.p, ignore_errors=True)


# ================================================================ M3: three track shapes
def test_shapes_have_plausible_lap_times_and_corner_counts():
    oval, street, long_ = review("oval", n=8), review("street", n=8), review("long", n=6)
    assert 28 <= oval["median"] <= 35, oval["median"]
    assert 60 <= street["median"] <= 75, street["median"]
    assert 7 * 60 <= long_["median"] <= 8 * 60, long_["median"]
    assert 0 <= len(oval["corners"]) <= 2, [c["apex_m"] for c in oval["corners"]]
    assert 8 <= len(street["corners"]) <= 10, [c["apex_m"] for c in street["corners"]]
    assert len(long_["corners"]) >= 15, len(long_["corners"])
    # the corners found sit on the apexes the fixture put there (within the corner window)
    for d, name in ((street, "street"), (long_, "long")):
        want = [a for a, _ in ft.shape(name)["apexes"]]
        for c in d["corners"]:
            assert min(abs(c["apex_m"] - a) for a in want) <= 40, (name, c["apex_m"])


def test_scales_are_anchored_on_a_90s_4km_lap():
    assert ap.time_scale(90.0) == 1.0 and ap.dist_scale(4000.0) == 1.0
    assert abs(ap.time_scale(30.0) - 1 / 3) < 1e-6 and ap.time_scale(450.0) == 5.0
    assert ap.time_scale(10.0) == 0.25 and ap.time_scale(3600.0) == 6.0          # clamps
    assert ap.dist_scale(1300.0) == 0.6 and ap.dist_scale(21000.0) == 1.5         # clamps
    assert ap.time_scale(None) == 1.0 and ap.dist_scale(0) == 1.0 and ap.time_scale(float("nan")) == 1.0
    assert ap.traffic_window_m(4000.0) == 400.0 and ap.traffic_window_m(1300.0) == 200.0
    assert ap.traffic_window_m(21000.0) == 1500.0
    # every review says which scales it used
    for name in ("oval", "street", "long"):
        d = review(name, n=8 if name != "long" else 6)
        assert abs(d["scales"]["time"] - ap.time_scale(d["median"])) < 1e-3
        assert abs(d["scales"]["dist"] - ap.dist_scale(d["L"])) < 1e-3
        assert abs(d["consistency"]["scale"] - ap.time_scale(d["consistency"]["n"] and d["median"])) < 0.05
        assert abs(d["consistency"]["within_s"] - 0.5 * d["consistency"]["scale"]) < 1e-3


# ================================================================ M4: consistency comparable across shapes
def test_consistency_labels_comparable_for_the_same_relative_scatter():
    rng = np.random.default_rng(7)
    z = rng.normal(0, 1, 14)
    z_corner = rng.normal(0, 1, 14)
    out = {}
    for med in (30.0, 66.0, 450.0):
        k = ap.time_scale(med)
        ts = [med * (1 + 0.004 * v) for v in z]             # 0.4% lap-time scatter on every track
        clean = [{"n": i + 1, "t": t} for i, t in enumerate(ts)]
        per = {"C1": {i + 1: {"t": 0.08 * med * (1 + 0.02 * v)} for i, v in enumerate(z_corner)}}
        c = ap.consistency(clean, [{"id": "C1"}], per, pace=clean, slow_cut=engine.slow_cut_for(med))
        out[med] = (c["score"], c["label"], c["by_corner"][0]["label"], round(c["within_05"]))
        assert abs(c["within_s"] - 0.5 * k) < 1e-3
    vals = list(out.values())
    assert all(v == vals[0] for v in vals), out
    # the label cut-offs themselves scale
    for k in (1 / 3, 1.0, 5.0):
        assert ap.iqr_label(0.04 * k, k) == "Locked in" and ap.iqr_label(0.10 * k, k) == "Steady"
        assert ap.iqr_label(0.20 * k, k) == "Varies" and ap.iqr_label(0.30 * k, k) == "All over"
    assert ap.iqr_label(0.10) == "Steady" and ap.consistency_score(0.6, 50.0) == ap.consistency_score(3.0, 50.0, 5.0)
    # insights' corner-spread habits use the same scale (a 0.2 s IQR is "varies" on a 90 s lap, "locked" on a 7.5 min one)
    base = {"laps": [{"n": i, "t": 90.0, "clean": True} for i in range(1, 13)], "best": {"n": 1, "t": 90.0},
            "counts": {"clean": 12}, "corners": [{"id": "C1", "avail": 0.0}, {"id": "C2", "avail": 0.0}],
            "consistency": {"score": 80, "by_corner": [{"id": "C1", "iqr": 0.2}, {"id": "C2", "iqr": 0.2}]}}
    keys = lambda d: ({b["key"] for b in insights.build(d)["bad"]}, {g["key"] for g in insights.build(d)["good"]})
    bad, good = keys(dict(base, median=90.0))
    assert "vary" in bad and "locked" not in good
    bad, good = keys(dict(base, median=450.0))
    assert "vary" not in bad and "locked" in good


# ================================================================ M4: traffic on every shape
def test_traffic_fires_on_a_2s_holdup_on_the_long_track_and_0_4s_on_the_oval():
    for name, hold, n in (("long", 2.0, 8), ("oval", 0.4, 14)):
        at = ft.shape(name)["L"] * 0.55
        mc = review(name, n=n, noise=0.3, slow_laps={5: (at, {"hold_s": hold})}, multiclass=True)
        assert mc["counts"]["traffic"] == [5] and mc["counts"]["traffic_possible"] == [], (name, mc["counts"])
        det = mc["counts"]["traffic_detail"][0]
        assert abs(det["lost"] - hold) < 0.25 * hold and abs(det["at_m"] - at) < 0.1 * ft.shape(name)["L"], det
        assert det["possible"] is False and 5 not in mc["counts"]["slow"]
        assert any("most likely traffic" in f for f in mc["findings"])
        # single-class: the same lap is "possibly traffic" and stays in the pace numbers
        sc = review(name, n=n, noise=0.3, slow_laps={5: (at, {"hold_s": hold})}, multiclass=False)
        assert sc["counts"]["traffic"] == [] and sc["counts"]["traffic_possible"] == [5], (name, sc["counts"])
        assert sc["counts"]["traffic_possible_detail"][0]["possible"] is True
        assert any("possibly traffic" in f for f in sc["findings"])
        assert 5 in {l["n"] for l in sc["laps"] if l["clean"]} or 5 in sc["counts"]["slow"]
        assert sc["consistency"]["n"] == mc["consistency"]["n"] + 1    # multi-class leaves the lap out, single-class keeps it
    # the single-class gate is stricter: the same loss on 40% of the laps is a habit, not traffic
    at = ft.shape("street")["L"] * 0.55
    rep = review("street", n=10, noise=0.3, slow_laps={n: (at, {"hold_s": 1.0}) for n in (2, 4, 6, 8)})
    assert rep["counts"]["traffic_possible"] == [], rep["counts"]
    # racecraft and insights carry the label through (street: the 1 s hold-up also makes the lap off-pace)
    sc = review("street", n=14, noise=0.3, slow_laps={5: (ft.shape("street")["L"] * 0.55, {"hold_s": 1.0})})
    assert sc["counts"]["traffic_possible"] == [5] and 5 in sc["counts"]["slow"], sc["counts"]
    rc = racecraft.build(sc)
    row = next(r for r in rc["timeline"] if r["n"] == 5)
    assert row.get("traffic_possible") is True and "traffic" not in row["tags"] and "slow" in row["tags"]
    assert any("possibly traffic" in s for s in rc["summary"]), rc["summary"]
    # ... and an off-pace habit names the lap as possibly traffic rather than a mistake
    sc2 = review("street", n=14, noise=0.3, slow_laps={5: (ft.shape("street")["L"] * 0.55, {"hold_s": 1.0}),
                                                       9: (ft.shape("street")["L"] * 0.2, 0.3)})
    off = next((i for i in insights.build(sc2)["bad"] if i["key"] == "offpace"), None)
    if off:
        assert "possibly traffic" in off["text"], off["text"]
    ins = insights.build(sc)
    assert any(i["key"] == "traffic" and "ossibly" in i["title"] for i in ins["look"]), ins["look"]
    assert not any(i["key"] == "traffic" for i in ins["bad"])


# ================================================================ M4: corner windows
def test_exit_speed_window_never_crosses_into_the_next_corner():
    for name, n in (("street", 8), ("long", 6)):
        d = review(name, n=n)
        cs = sorted(d["corners"], key=lambda c: c["apex_m"])
        assert cs, name
        for i, c in enumerate(cs):
            x = c["apex_m"] + c["exit_at_m"]
            assert 20.0 <= c["exit_at_m"] <= 150.0 * 1.5 + 1e-6, c["exit_at_m"]
            assert x <= c["end_m"] + 1e-6, (name, c["id"], x, c["end_m"])
            nxt = cs[(i + 1) % len(cs)]
            start = nxt["start_m"] + (d["L"] if i == len(cs) - 1 else 0.0)
            assert x < start, (name, c["id"], x, start)
            assert c["start_m"] <= c["apex_m"] <= c["end_m"]
            # the zone windows were scaled by this track's distance scale
            ds = d["scales"]["dist"]
            assert c["apex_m"] - c["start_m"] <= engine.ZONE_BEFORE_M * ds + 1e-6
            assert c["end_m"] - c["apex_m"] <= engine.ZONE_AFTER_M * ds + 1e-6
    assert engine.exit_offset_m(1000.0, 1250.0) == 150.0          # 4 km anchor: as it was
    assert engine.exit_offset_m(1000.0, 1100.0) == 60.0           # a short corner: 60% of the way to its end
    assert engine.exit_offset_m(1000.0, 1010.0) == 20.0           # never under 20 m
    assert abs(engine.exit_offset_m(1000.0, 2000.0, 1.5) - 225.0) < 1e-9
    # the coaching text quotes the window it used, not a fixed 150 m
    c = {"id": "C1", "apex_m": 100.0, "exit_at_m": 78.0}
    drv = {"metric": "exit_mph", "r": -0.8, "p75": 90.0, "p25": 80.0, "median": 85.0}
    txt = engine.coaching_text(c, {"min_mph": 50.0}, [drv], [])
    assert "78 m past the apex" in txt and "150 m" not in txt


def test_corner_detection_windows_scale_with_lap_length():
    d = review("street", n=8)
    R = next(iter(d["traces"].values()))   # (only to show the signature; the real check is below)
    assert R
    meta, laps, tel = ft.race("street", n=3)
    raw = engine.parse_csv(tel["lap1"])
    Rr = engine.resample(raw, laps[0]["lapTime"], d["L"])
    scaled = engine.detect_corners(Rr, L=d["L"])
    fixed = engine.detect_corners(Rr, window_m=150, merge_m=120)
    assert len(scaled) == 9 and len(fixed) >= 8
    # at the anchor the defaults are the old numbers
    assert engine.CORNER_WINDOW_M == 150 and engine.CORNER_MERGE_M == 120 and engine.ZONE_BEFORE_M == 350


# ================================================================ ovals: no corners, still a review
def test_oval_without_braking_zones_still_renders_by_sector():
    d = review("oval", n=8)
    assert d["corners"] == [] and d["heat"]["zones"] == [] and d["segments"] is not None
    assert d["no_corners"] and "No distinct braking zones" in d["no_corners"]["note"]
    assert d["no_corners"]["basis"] == "equal thirds" and d["segments"]["basis"] == "sectors"
    assert d["segments"]["names"] == ["S1", "S2", "S3"] and len(d["segments"]["cuts"]) == 4
    assert d["segments"]["possible"] is not None and d["segments"]["possible"] <= d["best"]["t"] + 1e-6
    assert any("No distinct braking zones" in f for f in d["findings"])
    assert d["plan"] == [] or all("C" not in p["title"][:2] for p in d["plan"])
    assert d["technique_plus"]["reason"] == "no_corners" and d["consistency"] is not None
    # official sectors win when Garage 61 has them
    meta, laps, tel = ft.race("oval", n=8)
    for l in laps:
        l["sectors"] = [{"sectorTime": l["lapTime"] * 0.3}, {"sectorTime": l["lapTime"] * 0.7}]
    d2 = engine.analyze(meta, laps, tel, [], {})
    assert d2["no_corners"]["basis"] == "official sectors" and d2["segments"]["names"] == ["S1", "S2"]
    assert len(d2["segments"]["cuts"]) == 3 and 0 < d2["segments"]["cuts"][1] < d2["L"]
    # the page renders it (report.write) with the note and without a corner table
    with Tmp() as f:
        out = f / "reviews" / "oval"
        out.mkdir(parents=True)
        page = report.write(out, json.loads(json.dumps(d, default=float)))
        html = page.read_text(encoding="utf-8")
        assert "No distinct braking zones" in html and "Best sectors added up" in html
        saved = json.loads((out / "review.json").read_text(encoding="utf-8"))
        assert saved["insights"]["verdict"] and saved["skills"]["reason"] == "no_corners"


# ================================================================ honest labels
def test_best_sectors_added_up_and_other_caveats_in_the_rendered_review():
    d = review("street", n=10, fuel=2.0)
    assert d["trend_fuel"] and d["trend_fuel"]["corrected"] is True and d["stint"]["fuel_s_per_lap"] > 0
    assert abs(d["trend_fuel"]["total"] - (d["trend_total"] + d["stint"]["fuel_s_per_lap"] * 9)) < 1e-3
    assert "estimate" in d["stint"]["summary"] and "not a measurement" in d["stint"]["summary"]
    with Tmp() as f:
        out = f / "reviews" / "street"
        out.mkdir(parents=True)
        html = report.write(out, json.loads(json.dumps(d, default=float))).read_text(encoding="utf-8")
    tpl = (ROOT / "debrief" / "templates" / "review.html").read_text(encoding="utf-8")
    assert "Best sectors added up" in html and "<h2>Best possible lap</h2>" not in tpl
    assert "A guide, not a target" in tpl and "no one can drive this lap in one go" in tpl
    assert "Fuel adjustment is an estimate" in tpl and "scaled to this track" in tpl
    assert "count race sessions only" in tpl and "Include practice and qualifying" in tpl
    assert "possibly traffic" in tpl and "Worth a look" in tpl
    assert "The real figure depends on the car" in tpl
    for f_ in d["findings"]:
        if "add up to" in f_:
            assert "a guide, not a lap anyone can drive" in f_, f_
    # nothing prescriptive: the numbers are what the quicker laps did
    src = (ROOT / "debrief" / "engine.py").read_text(encoding="utf-8") + (ROOT / "debrief" / "prep.py").read_text(encoding="utf-8")
    code = "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("#") and "2.7:" not in ln)
    assert "aim for" not in code.lower(), "prescriptive wording crept back in"
    for c in d["corners"]:
        assert "aim for" not in c["coaching"].lower() and "Aim for" not in c["coaching"]
    drv = {"metric": "min_mph", "r": -0.8, "p75": 62.0, "p25": 55.0, "median": 58.0, "n": 12}
    t = prep._target(drv, 100.0)
    assert t and t[0].startswith("your quicker laps were around") and "aim" not in t[0]


def test_fade_says_whether_it_is_fuel_corrected():
    base = {"laps": [{"n": i, "t": 90.0, "clean": True} for i in range(1, 13)], "best": {"n": 1, "t": 90.0},
            "median": 90.0, "counts": {"clean": 12}, "trend_total": 0.6, "corners": []}
    it = next(i for i in insights.build(base)["bad"] if i["key"] == "fade")
    assert "no fuel data" in it["text"] and "0.6 s" in it["text"] and "Measured" not in it["text"]
    with_fuel = dict(base, stint={"fuel_s_per_lap": 0.04}, trend_fuel={"total": 1.04, "fuel_s_per_lap": 0.04, "corrected": True})
    it = next(i for i in insights.build(with_fuel)["bad"] if i["key"] == "fade")
    assert it["text"].startswith("Measured: your clean laps got about 0.6 s slower") and "no fuel data" not in it["text"] and "about 1.0 s" in it["text"]
    # a review whose raw trend is flat but whose fuel-corrected trend fades still gets the item
    flat = dict(base, trend_total=0.05, stint={"fuel_s_per_lap": 0.04}, trend_fuel={"total": 0.45, "fuel_s_per_lap": 0.04, "corrected": True})
    assert any(i["key"] == "fade" for i in insights.build(flat)["bad"])


def test_lap1_is_a_habit_only_when_the_start_type_is_known():
    d = json.loads(json.dumps(review("street", n=10), default=float))
    d["laps"][0]["t"] = d["median"] + 3.0      # a slow lap 1
    d["racecraft"] = racecraft.build(d)
    assert d["racecraft"]["lap1"]["start_type"] is None
    ins = insights.build(d)
    assert not any(i["key"] == "lap1" for i in ins["bad"])
    it = next(i for i in ins["look"] if i["key"] == "lap1")
    assert "doesn't know what kind of start" in it["costs"] and it["laps"] == [1]
    for word in ("standing", "rolling"):
        assert word not in it["text"]
    d["result"] = {"finish_pos": 3, "start_type": "Rolling"}
    d["racecraft"] = racecraft.build(d)
    assert d["racecraft"]["lap1"]["start_type"] == "rolling"
    it = next(i for i in insights.build(d)["bad"] if i["key"] == "lap1")
    assert "rolling start" in it["text"] and "doesn't know" not in (it["costs"] or "")
    d["result"]["start_type"] = "no idea"
    assert racecraft.build(d)["lap1"]["start_type"] is None


# ================================================================ practice / qualifying vs race
def _lib_review(folder, name, d, session, started, best_shift=0.0, event=None):
    d = json.loads(json.dumps(d, default=float))
    d["meta"] = dict(d["meta"], session=session, started_at=started, event_id=event or name)
    if best_shift:
        for l in d["laps"]:
            l["t"] += best_shift
        d["best"]["t"] += best_shift
        d["median"] += best_shift
    out = Path(folder) / "reviews" / name
    out.mkdir(parents=True, exist_ok=True)
    (out / "review.json").write_text(json.dumps(d, allow_nan=False), encoding="utf-8")
    return d


def test_practice_rows_are_kept_but_excluded_from_pbs_and_trends_by_default():
    d = review("street", n=8)
    with Tmp() as f:
        _lib_review(f, "2026-09-01_race1", d, "Race", "2026-09-01T18:00:00Z", 0.0)
        _lib_review(f, "2026-09-05_practice", d, "Practice", "2026-09-05T18:00:00Z", -2.0)    # quicker: no fuel, no traffic
        _lib_review(f, "2026-09-06_qualifying", d, "Qualifying", "2026-09-06T18:00:00Z", -1.5)
        _lib_review(f, "2026-09-08_race2", d, "Race", "2026-09-08T18:00:00Z", -0.4)
        rows = {r["dir"]: r for r in library.list_reviews(f)}
        assert len(rows) == 4 and {r["session"] for r in rows.values()} == {"Race", "Practice", "Qualifying"}
        assert library.is_race(rows["2026-09-01_race1"]) and not library.is_race(rows["2026-09-05_practice"])
        assert library.is_race({"session": None}) and library.is_race({"session": "race"})
        # race 2 is a PB against race 1 even though practice and qualifying went quicker in between
        r2 = rows["2026-09-08_race2"]["pb"]
        assert r2["is_pb"] is True and r2["previous_date"] == "2026-09-01" and abs(r2["delta"] + 0.4) < 1e-3
        p = rows["2026-09-05_practice"]["pb"]
        assert p["is_pb"] is False and p["first_time"] is False and p["session_excluded"] is True
        assert p["previous_best"] == rows["2026-09-01_race1"]["best"] and abs(p["delta"] + 2.0) < 1e-3
        # with the flag, every session counts: race 2 is no longer a PB, practice was
        assert rows["2026-09-08_race2"]["pb_all"]["is_pb"] is False
        assert rows["2026-09-05_practice"]["pb_all"]["is_pb"] is True
        inc = {r["dir"]: r for r in library.list_reviews(f, include_practice=True)}
        assert inc["2026-09-05_practice"]["pb"]["is_pb"] is True and inc["2026-09-08_race2"]["pb"]["is_pb"] is False
        # pb_for on a fresh practice review: never a PB, measured against the race best
        new = json.loads(json.dumps(d, default=float))
        new["meta"] = dict(new["meta"], session="Qualifying", started_at="2026-09-10T18:00:00Z", event_id="Q")
        new["best"]["t"] -= 3.0
        pb = library.pb_for(f, new)
        assert pb["is_pb"] is False and pb["session_excluded"] is True and pb["previous_best"] == rows["2026-09-08_race2"]["best"]
        pb_inc = library.pb_for(f, new, include_practice=True)
        assert pb_inc["is_pb"] is True and pb_inc["previous_best"] == rows["2026-09-05_practice"]["best"]
        new["meta"]["session"] = "Race"
        assert library.pb_for(f, new)["is_pb"] is True and library.pb_for(f, new)["previous_best"] == rows["2026-09-08_race2"]["best"]
        # season summary, prep combos / history and the skills profile: race sessions only by default
        ss = library.season_summary(list(rows.values()))
        assert ss["sessions"] == "race" and ss["seasons"][0]["races"] == 2
        assert library.season_summary(list(rows.values()), include_practice=True)["seasons"][0]["races"] == 4
        combo = prep.combos(f)[0]
        assert combo["races"] == 2 and abs(combo["best"] - rows["2026-09-08_race2"]["best"]) < 1e-6
        assert prep.combos(f, include_practice=True)[0]["races"] == 4
        hist = prep.build(None, f, None, None, None, log=lambda m: None, track=d["meta"]["track"], car=d["meta"]["car"])
        assert hist["history"]["races"] == 2 and hist["practice_sessions"] == 2 and hist["sessions"] == "race"
        assert abs(hist["history"]["best"] - rows["2026-09-08_race2"]["best"]) < 1e-6
        hist_all = prep.build(None, f, None, None, None, log=lambda m: None, track=d["meta"]["track"],
                              car=d["meta"]["car"], include_practice=True)
        assert hist_all["history"]["races"] == 4 and hist_all["sessions"] == "all"
        assert abs(hist_all["history"]["best"] - rows["2026-09-05_practice"]["best"]) < 1e-6
        sk_rows = [dict(r, skills={"v": 1, "n_clean": 8, "lap_t": 70.0, "loss_pm": {"braking": 1.0, "entry": 1.0, "mid": 1.0, "exit": 1.0, "overall": 2.0}})
                   for r in rows.values()]
        pr = profile.build(sk_rows)
        assert pr["n_races"] == 2 and pr["sessions"] == "race" and pr["n_practice_skipped"] == 2
        assert profile.build(sk_rows, include_practice=True)["n_races"] == 4
        # the library page carries the toggle, the rule and the practice rows
        page = library.rebuild(f)
        html = page.read_text(encoding="utf-8")
        assert "Include practice and qualifying" in html and "count race sessions only" in html
        assert '"session":"Practice"' in html and '"pb_all":' in html and '"n_practice":2' in html
        _browser_check(page)


def _browser_check(page):
    """Load the library page in headless Chromium when Playwright is around: no console errors, the race count
    leaves practice out, and the toggle brings the practice rows (with their chip) into the table."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  (playwright not installed: page check skipped)")
        return
    try:
        pw = sync_playwright().start()
        b = pw.chromium.launch()
    except Exception:  # noqa: BLE001
        print("  (Playwright could not start: page check skipped)")
        return
    try:
        ctx = b.new_context(viewport={"width": 1280, "height": 900})
        ctx.route("**/fonts.googleapis.com/**", lambda r: r.abort())
        ctx.route("**/fonts.gstatic.com/**", lambda r: r.abort())
        pg = ctx.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append("pageerror: " + str(e)))
        pg.on("console", lambda m: errs.append("console: " + m.text) if m.type == "error" and "fonts.g" not in m.text
              and "fonts.g" not in (m.location or {}).get("url", "") else None)
        pg.goto(page.resolve().as_uri())
        pg.wait_for_function("() => document.querySelectorAll('#list tbody tr').length > 0", timeout=15000)
        r = pg.evaluate("() => ({count: document.getElementById('count').textContent, rows: document.querySelectorAll('#list tbody tr').length,"
                        " chips: document.querySelectorAll('#list .sesschip').length, notes: document.getElementById('libnotes').textContent,"
                        " checked: document.getElementById('incprac').checked})")
        assert not errs, errs
        assert r["rows"] == 2 and r["chips"] == 0 and r["checked"] is False, r
        assert "2 race reviews" in r["count"] and "2 practice/qualifying hidden" in r["count"], r["count"]
        assert "race sessions only" in r["notes"]
        pg.click("#incprac")
        pg.wait_for_function("() => document.getElementById('incprac').checked && document.querySelectorAll('#list .sesschip').length === 2", timeout=15000)
        r = pg.evaluate("() => ({rows: document.querySelectorAll('#list tbody tr').length, pbs: document.querySelectorAll('#list .pbchip').length,"
                        " count: document.getElementById('count').textContent})")
        assert not errs, errs
        assert r["rows"] == 4 and "4 reviews" in r["count"] and r["pbs"] >= 1, r
        ctx.close()
        _sandbox_check(b, page)
    finally:
        try:
            b.close()
            pw.stop()
        except Exception:  # noqa: BLE001
            pass


def _sandbox_check(b, page):
    """2.7.1 (M4): the page as the app serves it: over HTTP with the library CSP sandbox (opaque origin, where
    localStorage throws). Ticking the box shows the practice rows, unticking hides them, with no reload and no
    console errors; the flag rides in the hash and the tabs still switch."""
    import http.server
    import threading
    from debrief.desktop.server import LIB_CSP
    body = page.read_bytes()

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Security-Policy", LIB_CSP)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        ctx = b.new_context(viewport={"width": 1280, "height": 900})
        ctx.route("**/fonts.googleapis.com/**", lambda r: r.abort())
        ctx.route("**/fonts.gstatic.com/**", lambda r: r.abort())
        pg = ctx.new_page()
        errs, loads = [], []
        pg.on("pageerror", lambda e: errs.append("pageerror: " + str(e)))
        pg.on("console", lambda m: errs.append("console: " + m.text) if m.type == "error" and "fonts.g" not in m.text
              and "fonts.g" not in (m.location or {}).get("url", "") else None)
        pg.on("load", lambda: loads.append(1))
        pg.goto(f"http://127.0.0.1:{srv.server_address[1]}/library/index.html")
        pg.wait_for_function("() => document.querySelectorAll('#list tbody tr').length > 0", timeout=15000)
        assert pg.evaluate("() => { try { localStorage.setItem('x', '1'); return 'stored' } catch (e) { return 'throws' } }") == "throws"
        rows = "() => ({rows: document.querySelectorAll('#list tbody tr').length, chips: document.querySelectorAll('#list .sesschip').length," \
               " checked: document.getElementById('incprac').checked, hash: location.hash, count: document.getElementById('count').textContent," \
               " tab: [...document.querySelectorAll('[id^=tab-]')].filter(e => !e.hidden).map(e => e.id).join()})"
        r = pg.evaluate(rows)
        assert r["rows"] == 2 and r["chips"] == 0 and not r["checked"], r
        pg.click("#incprac")
        pg.wait_for_function("() => document.querySelectorAll('#list .sesschip').length === 2", timeout=15000)
        r = pg.evaluate(rows)
        assert r["rows"] == 4 and r["checked"] and r["hash"] == "#overview&incprac" and "4 reviews" in r["count"], r
        # tabs still switch, and the flag survives a tab link (which drops it from the hash)
        pg.click(".tabs a[data-tab=season]")
        pg.wait_for_function("() => !document.getElementById('tab-season').hidden && location.hash === '#season&incprac'", timeout=15000)
        pg.click(".tabs a[data-tab=overview]")
        pg.wait_for_function("() => !document.getElementById('tab-overview').hidden", timeout=15000)
        r = pg.evaluate(rows)
        assert r["rows"] == 4 and r["checked"] and r["tab"] == "tab-overview", r
        # a reload keeps the choice (it is in the hash, not in storage)
        pg.reload()
        pg.wait_for_function("() => document.querySelectorAll('#list tbody tr').length > 0", timeout=15000)
        r = pg.evaluate(rows)
        assert r["rows"] == 4 and r["checked"], r
        n_loads = len(loads)
        pg.click("#incprac")
        pg.wait_for_function("() => document.querySelectorAll('#list .sesschip').length === 0", timeout=15000)
        r = pg.evaluate(rows)
        assert r["rows"] == 2 and not r["checked"] and "incprac" not in r["hash"] and "2 practice/qualifying hidden" in r["count"], r
        pg.click(".tabs a[data-tab=skills]")
        pg.wait_for_function("() => !document.getElementById('tab-skills').hidden && location.hash === '#skills'", timeout=15000)
        assert len(loads) == n_loads, "the toggle reloaded the page"
        assert not errs, errs
        ctx.close()
    finally:
        srv.shutdown()


def test_review_page_shows_worth_a_look_and_possibly_traffic():
    """The rendered review (headless Chromium when Playwright is around): the neutral list and the single-class
    traffic label are on the page, with no console errors."""
    d = json.loads(json.dumps(review("street", n=14, noise=0.3,
                                     slow_laps={5: (ft.shape("street")["L"] * 0.55, {"hold_s": 1.0})}), default=float))
    d["laps"][0]["t"] = d["median"] + 3.0
    d.pop("racecraft", None)
    with Tmp() as f:
        out = f / "reviews" / "street"
        out.mkdir(parents=True)
        page = report.write(out, d)
        saved = json.loads((out / "review.json").read_text(encoding="utf-8"))
        assert any(i["key"] == "lap1" for i in saved["insights"]["look"])
        assert any(i["key"] == "traffic" for i in saved["insights"]["look"])
        try:
            from playwright.sync_api import sync_playwright
            pw = sync_playwright().start()
            b = pw.chromium.launch()
        except Exception:  # noqa: BLE001
            print("  (Playwright not available: page check skipped)")
            return
        try:
            ctx = b.new_context(viewport={"width": 1280, "height": 900})
            ctx.route("**/fonts.googleapis.com/**", lambda r: r.abort())
            ctx.route("**/fonts.gstatic.com/**", lambda r: r.abort())
            pg = ctx.new_page()
            errs = []
            pg.on("pageerror", lambda e: errs.append("pageerror: " + str(e)))
            pg.on("console", lambda m: errs.append("console: " + m.text) if m.type == "error" and "fonts.g" not in m.text
                  and "fonts.g" not in (m.location or {}).get("url", "") else None)
            pg.goto(page.resolve().as_uri())
            pg.wait_for_function("() => document.querySelectorAll('#lookList li').length > 0", timeout=15000)
            r = pg.evaluate("() => ({look: document.getElementById('lookPanel').hidden, items: document.querySelectorAll('#lookList li').length,"
                            " body: document.body.innerText, fails: [...document.querySelectorAll('.secfail')].map(e => e.textContent).join('|')})")
            assert not errs, errs
            assert r["look"] is False and r["items"] >= 2 and not r["fails"], r["fails"]
            for txt in ("Worth a look", "possibly traffic", "Best sectors added up", "know what kind of start",
                        "count race sessions only"):
                assert txt in r["body"], txt
            assert "Best possible lap" not in r["body"]
            ctx.close()
        finally:
            b.close()
            pw.stop()


# ================================================================ nothing regressed at the anchor
def test_fixture_a_synth_loop_still_finds_its_four_corners():
    from fixtures_a import synth
    meta, laps, tel = synth.race(n=12)
    d = engine.analyze(meta, laps, tel, [], {})
    assert len(d["corners"]) == 4 and d["no_corners"] is None and d["segments"]["basis"] == "corners"
    assert set(c["id"] for c in d["corners"]) == {"C1", "C2", "C3", "C4"}
    assert "scales" in d and 0.6 <= d["scales"]["dist"] <= 1.0


# ================================================================ runner
if __name__ == "__main__":
    import io
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, io.UnsupportedOperation):
        pass
    tests = [(k, v) for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for name, fn in tests:
        t0 = time.time()
        try:
            fn()
            print(f"ok    {name} ({time.time() - t0:.1f}s)")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {name}")
            traceback.print_exc()
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
