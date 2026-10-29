"""Debrief 2.3 workstream B: skills (time loss by corner phase), practice/qualifying reviews, skill
profile across races, and the library hooks.

    python3 tests/test_23_b.py
"""
from __future__ import annotations

import json
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "fixtures_23_b"))

import synth  # noqa: E402
from debrief import library, profile, schedule, session_review, skills  # noqa: E402

FX = ROOT / "tests" / "fixtures_23_b"
FORBIDDEN = re.compile(r"\b(will gain|costing|because|you should|the reason)\b", re.I)


def _corner(s, cid):
    return next(c for c in s["corners"] if c["id"] == cid)


# ---------------------------------------------------------------- skills
def test_better_laps_rule():
    d = synth.make_review(n_laps=12, lap_t_jitter={n: 0.01 * n for n in range(1, 13)})
    assert skills.better_laps(d) == [1, 2, 3]            # p25 of 12 -> laps at or under it
    d = synth.make_review(n_laps=4, lap_t_jitter={1: 0.0, 2: 0.1, 3: 0.2, 4: 0.3})
    assert skills.better_laps(d) == [1, 2, 3]            # p25 gives 1 lap -> 3 quickest
    d = synth.make_review(n_laps=2)
    assert skills.better_laps(d) == []
    d = synth.make_review(n_laps=12, clean={1, 2, 5, 6, 7, 8, 9, 10}, lap_t_jitter={3: -1.0, 4: -1.0})
    assert 3 not in skills.better_laps(d) and 4 not in skills.better_laps(d)   # unclean laps never count


def test_score():
    assert skills.score(0, "exit") == 100
    assert skills.score(2.5, "exit") == 50
    assert skills.score(9.0, "exit") == 0
    assert skills.score(6.0, "overall") == 50
    assert skills.score(None, "mid") is None


def test_exit_loss_lands_in_exit():
    d = synth.exit_loss_review(loss=0.05, n_laps=12)
    s = skills.for_review(d)
    assert s["reason"] is None and s["n_clean"] == 12 and s["ref_laps"] == [1, 2, 3]
    ph = s["phases"]
    assert abs(ph["exit"]["loss_s"] - 0.05) <= 0.01, ph
    for p in ("braking", "entry", "mid"):
        assert ph[p]["loss_s"] <= 0.005, (p, ph)
    assert s["straights"]["loss_s"] <= 0.005
    assert abs(s["overall"]["loss_s"] - sum(ph[p]["loss_s"] for p in skills.PHASES)) < 1e-6
    # phase + approach losses add up to the corner-zone loss (per_lap t, typical vs better laps)
    ref = s["ref_laps"]
    for c in d["corners"]:
        zt = {int(n): m["t"] for n, m in c["per_lap"].items()}
        zone = float(np.median([zt[n] for n in s["typical_laps"]]) - np.median([zt[n] for n in ref]))
        sc = _corner(s, c["id"])
        assert abs(sum(sc["loss_raw"].values()) - zone) <= 0.01, (c["id"], sc["loss_raw"], zone)
    c2 = _corner(s, "C2")
    m = c2["markers"]
    assert m["start"] <= m["B"] <= m["R"] <= m["A"] <= m["P"] <= m["E"]
    assert c2["lift"] is False and m["B"] == 1850.0 and m["A"] == 2000.0
    # raw values stored for retuning
    assert set(c2["per_lap"]) == {str(n) for n in range(1, 13)}
    assert s["params"]["noise_s"] == 0.01 and s["params"]["caps_pm"]["exit"] == 5.0
    assert ph["exit"]["score"] == skills.score(ph["exit"]["loss_pm"], "exit")


def test_lift_corner_has_no_braking_phase():
    s = skills.for_review(synth.exit_loss_review())
    c1 = _corner(s, "C1")
    assert c1["lift"] is True
    assert c1["markers"]["B"] == c1["markers"]["R"]
    assert c1["loss"]["braking"] == 0 and c1["loss_raw"]["braking"] == 0
    assert c1["markers"]["B"] < c1["markers"]["A"]        # lift point found before the slowest point
    assert s["phases"]["braking"]["corners"] == 1          # only C2 has a braking stretch


def test_noise_gate():
    d = synth.make_review(n_laps=14, noise=0.005, seed=4)
    s = skills.for_review(d)
    assert s["reason"] is None
    for c in s["corners"]:
        assert all(v == 0 for v in c["loss"].values()), c["loss"]
    assert s["overall"]["loss_s"] == 0 and s["overall"]["score"] == 100


def test_reasons():
    s = skills.for_review(synth.make_review(n_laps=5))
    assert s["reason"] == "not_enough_clean_laps" and "phases" not in s and "corners" not in s and "overall" not in s
    s = skills.for_review(synth.make_review(n_laps=12, clean={1, 2, 3, 4, 5}))
    assert s["reason"] == "not_enough_clean_laps"
    d = synth.make_review(n_laps=12)
    d.pop("traces")
    assert skills.for_review(d)["reason"] == "no_traces"
    d = synth.make_review(n_laps=12)
    d["corners"] = []
    assert skills.for_review(d)["reason"] == "no_corners"
    assert skills.compact(skills.for_review(synth.make_review(n_laps=5))) is None


def test_old_fixture_reviews():
    lib = Path(tempfile.mkdtemp(prefix="debrief-23b-"))
    try:
        subprocess.run([sys.executable, str(ROOT / "tests" / "make_fixture_library.py"), str(lib)], check=True,
                       capture_output=True)
        revs = sorted((lib / "reviews").glob("*/review.json"))
        assert revs
        for p in revs:
            d = json.loads(p.read_text())
            s = skills.for_review(d)
            assert s["reason"] in (None, "no_traces", "no_corners", "not_enough_clean_laps", "not_enough_better_laps")
            assert session_review.for_review(d) is None   # fixture races
    finally:
        shutil.rmtree(lib, ignore_errors=True)


def test_odd_inputs_degrade():
    assert skills.for_review({})["reason"] == "no_traces"
    d = synth.make_review(n_laps=12)
    d["traces"]["me3"] = {"tt": []}           # one broken trace: that lap is just left out
    s = skills.for_review(d)
    assert s["reason"] is None and 3 not in s["typical_laps"]
    d = synth.make_review(n_laps=12)
    d["corners"][1]["per_lap"] = {}           # corner without per_lap data is skipped
    s = skills.for_review(d)
    assert [c["id"] for c in s["corners"]] == ["C1"]


# ---------------------------------------------------------------- session_review
def test_race_returns_none():
    assert session_review.for_review(synth.make_review(n_laps=12, session="Race")) is None
    assert session_review.for_review(synth.make_review(n_laps=12, session="Session")) is None
    assert session_review.for_review({"meta": {}}) is None


def test_gap_splits_runs():
    nums = [1, 2, 3, 4, 6, 7, 8, 9]
    sr = session_review.for_review(synth.make_review(lap_numbers=nums, session="Practice"))
    assert [r["laps"] for r in sr["runs"]] == [[1, 2, 3, 4], [6, 7, 8, 9]]
    assert [r["boundary"] for r in sr["runs"]] == ["start", "gap"]


def test_fuel_splits_runs():
    fuel = [30.0, 27.6, 25.2, 40.0, 37.6, 35.2, 32.8]
    sr = session_review.for_review(synth.make_review(n_laps=7, session="Practice", fuel=fuel))
    assert [r["laps"] for r in sr["runs"]] == [[1, 2, 3], [4, 5, 6, 7]]
    assert sr["runs"][1]["boundary"] == "refuel" and sr["runs"][1]["fuel_start_l"] == 40.0
    # no fuel data: rule skipped
    sr = session_review.for_review(synth.make_review(n_laps=7, session="Practice"))
    assert len(sr["runs"]) == 1


def test_start_break_splits_runs():
    starts = [1000.0 + 75.0 * k for k in range(4)] + [1000.0 + 75.0 * 4 + 600 + 75.0 * k for k in range(4)]
    sr = session_review.for_review(synth.make_review(n_laps=8, session="Practice", starts=starts))
    assert [r["laps"] for r in sr["runs"]] == [[1, 2, 3, 4], [5, 6, 7, 8]] and sr["runs"][1]["boundary"] == "break"
    # a 100 s pause is under 1.6 x 75 + 30: same run
    starts = [1000.0 + 75.0 * k for k in range(8)]
    starts[4:] = [s + 25 for s in starts[4:]]
    assert len(session_review.for_review(synth.make_review(n_laps=8, session="Practice", starts=starts))["runs"]) == 1


def test_short_runs_and_trend():
    nums = [1, 2, 4, 5, 6, 7, 8, 9]
    sr = session_review.for_review(synth.make_review(lap_numbers=nums, session="Practice",
                                                     lap_t_jitter={n: -0.1 * n for n in nums}))
    r1, r2 = sr["runs"]
    assert r1["median_t"] is None and r1["trend_s_per_lap"] is None     # 2 laps
    assert r2["median_t"] is not None and abs(r2["trend_s_per_lap"] + 0.1) < 1e-3   # 6 clean laps


def test_tiny_session_shape():
    sr = session_review.for_review(synth.make_review(n_laps=2, session="Practice"))
    assert sr["progress"] is None and sr["targets"] == [] and sr["quali"] is None and len(sr["runs"]) == 1


def test_progress_and_targets():
    sr = json.loads((FX / "sample_session_practice.json").read_text())
    fresh = session_review.for_review(__import__("make_samples").practice_review())
    assert fresh == sr                                     # sample fixture is current
    assert len(sr["runs"]) == 3 and sr["progress"]["delta_t"] < 0   # last run quicker
    c2 = next(c for c in sr["progress"]["corners"] if c["id"] == "C2")
    assert c2["delta_t"] < 0
    assert sr["targets"] and all(t["gain_s"] >= 0.03 for t in sr["targets"]) and len(sr["targets"]) <= 5
    t = sr["targets"][0]
    assert t["corner"] == "C2" and t["target_t"] < t["typical_t"] and t["brake_on"] == 1850.0 and t["gear"] == 3
    assert set(t["from_laps"]) <= set(sr["runs"][-1]["laps"])        # latest run with >= 5 clean laps
    # no gain worth showing: no targets
    sr = session_review.for_review(synth.make_review(n_laps=10, session="Practice"))
    assert sr["targets"] == []


def test_quali_roles():
    jit = {1: 1.2, 2: 0.4, 3: 0.0, 4: 0.3, 5: 2.5}
    d = synth.make_review(n_laps=5, session="Qualifying", lap_t_jitter=jit, sectors=True)
    d["laps"][3]["sectors"] = [10.0, 30.0, 35.3]
    q = session_review.for_review(d)["quali"]
    assert q["best_lap"] == 3 and q["best_index"] == 3 and q["run"] == 1
    assert [l["role"] for l in q["laps"]] == ["warm-up", "warm-up", "best", "push", "cool"]
    assert abs(q["second_push_delta_t"] - 0.3) < 1e-6
    s1 = q["sector_gain"][0]
    assert s1["name"] == "S1" and s1["others_best_t"] == 10.0
    # no push lap
    d = synth.make_review(n_laps=3, session="Qualifying", lap_t_jitter={1: 1.0, 2: 0.0, 3: 3.0})
    q = session_review.for_review(d)["quali"]
    assert q["second_push_delta_t"] is None and q["sector_gain"] == []


# ---------------------------------------------------------------- profile
def _row(i, day, loss_exit, car="Ford Mustang GT4", n_clean=12, season="2026 Season 4"):
    lp = {"braking": 1.0, "entry": 1.0, "mid": 0.5, "exit": loss_exit}
    lp["overall"] = round(sum(lp.values()), 2)
    return {"dir": f"{day}_r{i}", "date": day, "started_at": f"{day}T19:00:00+00:00", "track_short": "Roval",
            "car": car, "season": {"name": season}, "skills": {"v": 1, "n_clean": n_clean, "lap_t": 85.0, "loss_pm": lp}}


def test_profile_improving():
    rows = [_row(i, d, x) for i, (d, x) in enumerate([("2026-10-01", 3.0), ("2026-10-08", 2.6),
                                                       ("2026-10-15", 2.1), ("2026-10-22", 1.8)])]
    p = profile.build(rows)
    g = p["groups"][0]
    assert g["key"] == "all" and g["races"] == 4 and p["n_races"] == 4
    tr = g["phases"]["exit"]["trend"]
    assert tr["direction"] == "improving" and tr["n"] == 4 and tr["days"] == 21 and tr["change_pm"] <= -0.5
    assert g["phases"]["mid"]["trend"]["direction"] == "steady"
    ex = g["phases"]["exit"]
    assert [r["loss_pm"] for r in ex["races"]] == [3.0, 2.6, 2.1, 1.8]   # oldest first
    assert ex["latest"] == skills.score(1.8, "exit") and ex["best_score"] == ex["latest"]
    assert len(ex["weekly"]) == 4 and ex["seasons"] == [{"name": "2026 Season 4", "loss_pm": 2.35, "n": 4}]
    assert p["weakest"]["phase"] == "exit"
    slip = profile.build([_row(i, d, x) for i, (d, x) in enumerate([("2026-10-01", 1.0), ("2026-10-08", 1.6),
                                                                     ("2026-10-15", 2.1), ("2026-10-22", 2.4)])])
    assert slip["groups"][0]["phases"]["exit"]["trend"]["direction"] == "slipping"


def test_profile_trend_needs_enough():
    rows = [_row(i, d, x) for i, (d, x) in enumerate([("2026-10-01", 3.0), ("2026-10-08", 2.6), ("2026-10-15", 2.1)])]
    assert profile.build(rows)["groups"][0]["phases"]["exit"]["trend"] is None
    # 4 races within 10 days: never "improving"/"slipping" yet
    rows = [_row(i, d, x) for i, (d, x) in enumerate([("2026-10-01", 3.0), ("2026-10-04", 2.6),
                                                       ("2026-10-07", 2.1), ("2026-10-10", 1.8)])]
    tr = profile.build(rows)["groups"][0]["phases"]["exit"]["trend"]
    assert tr["direction"] == "steady" and tr["span_ok"] is False
    # rows below n_clean 6, without skills, or junk are ignored
    rows = [_row(0, "2026-10-01", 2.0, n_clean=5), {"dir": "x", "skills": None}, {"skills": "bad"}, {}]
    p = profile.build(rows)
    assert p["n_races"] == 0 and p["groups"] == [] and p["weakest"] is None
    assert profile.build([])["groups"] == []


def test_profile_rescored_from_caps():
    rows = [_row(0, "2026-10-01", 2.5)]
    before = json.dumps(rows)
    assert profile.build(rows)["groups"][0]["phases"]["exit"]["latest"] == 50
    old = dict(skills.CAPS_PM)
    try:
        skills.CAPS_PM["exit"] = 10.0
        p = profile.build(rows)
        assert p["groups"][0]["phases"]["exit"]["latest"] == 75 and p["caps_pm"]["exit"] == 10.0
    finally:
        skills.CAPS_PM.clear()
        skills.CAPS_PM.update(old)
    assert json.dumps(rows) == before                       # rows untouched


def test_profile_class_groups():
    rows = [_row(0, "2026-10-01", 2.0, car="Ford Mustang GT4"), _row(1, "2026-10-02", 2.0, car="Porsche 911 GT3 R (992)"),
            _row(2, "2026-10-03", 2.0, car="Porsche 911 GT3 R (992)"), _row(3, "2026-10-04", 2.0, car="Mystery Kart")]
    p = profile.build(rows)
    keys = {g["key"]: g["races"] for g in p["groups"]}
    assert keys["all"] == 4
    assert keys[f"class:{schedule.class_of('Porsche 911 GT3 R (992)')}"] == 2
    assert keys[f"class:{schedule.class_of('Ford Mustang GT4')}"] == 1
    assert keys["class:Mystery Kart"] == 1                 # unknown class: the car name
    orig = schedule.class_of
    try:
        schedule.class_of = lambda car: "Everything"
        assert {g["key"] for g in profile.build(rows)["groups"]} == {"all", "class:Everything"}
    finally:
        schedule.class_of = orig


# ---------------------------------------------------------------- library
def _lib_with_reviews():
    lib = Path(tempfile.mkdtemp(prefix="debrief-23b-lib-"))
    subprocess.run([sys.executable, str(ROOT / "tests" / "make_fixture_library.py"), str(lib)], check=True,
                   capture_output=True)
    return lib


def test_library_rows_and_rebuild():
    lib = _lib_with_reviews()
    try:
        rows = library.list_reviews(lib)
        assert rows and all(r["skills"] is None for r in rows)    # old reviews: no skills yet
        # add a new review with a skills block
        d = synth.exit_loss_review(n_laps=12)
        d["skills"] = skills.for_review(d)
        rd = lib / "reviews" / "2026-10-20_synthetic-loop_ford-mustang-gt4_new"
        rd.mkdir(parents=True)
        (rd / "review.json").write_text(json.dumps(d))
        rows = library.list_reviews(lib)
        new = next(r for r in rows if r["dir"] == rd.name)
        assert new["skills"]["v"] == 1 and new["skills"]["n_clean"] == 12
        assert set(new["skills"]["loss_pm"]) == {"braking", "entry", "mid", "exit", "overall"}
        out = library.rebuild(lib)
        html = out.read_text()
        assert out.exists() and '"n_races":1' in html.replace(" ", "")
    finally:
        shutil.rmtree(lib, ignore_errors=True)


def test_rebuild_survives_profile_error():
    lib = _lib_with_reviews()
    orig = profile.build
    try:
        def boom(rows):
            raise RuntimeError("bad profile")
        profile.build = boom
        out = library.rebuild(lib)
        assert out.exists() and "Test fixture" not in out.read_text()[:50]
    finally:
        profile.build = orig
        shutil.rmtree(lib, ignore_errors=True)


def test_row_with_reason_skills_is_none():
    lib = Path(tempfile.mkdtemp(prefix="debrief-23b-lib-"))
    try:
        d = synth.make_review(n_laps=5)
        d["skills"] = skills.for_review(d)
        rd = lib / "reviews" / "2026-10-20_x"
        rd.mkdir(parents=True)
        (rd / "review.json").write_text(json.dumps(d))
        assert library.list_reviews(lib)[0]["skills"] is None
    finally:
        shutil.rmtree(lib, ignore_errors=True)


# ---------------------------------------------------------------- cross-cutting
def test_no_requests():
    """Request budget: 0. Any socket connection during these calls fails the test."""
    calls = []
    orig = socket.socket.connect

    def spy(self, *a, **k):
        calls.append(a)
        raise OSError("network disabled in test")
    socket.socket.connect = spy
    try:
        d = __import__("make_samples").practice_review()
        skills.for_review(d)
        session_review.for_review(d)
        profile.build([_row(0, "2026-10-01", 2.0)])
    finally:
        socket.socket.connect = orig
    assert calls == []


def test_samples_match_shapes_and_wording():
    names = ["sample_skills.json", "sample_skills_reason.json", "sample_session_practice.json",
             "sample_session_quali.json", "sample_library_rows.json", "sample_profile.json",
             "sample_review_practice.json"]
    for n in names:
        txt = (FX / n).read_text()
        json.loads(txt)
        assert not FORBIDDEN.search(txt), n
    s = json.loads((FX / "sample_skills.json").read_text())
    for k in ("version", "params", "reason", "lap_t", "n_clean", "ref_laps", "typical_laps", "phases",
              "straights", "overall", "corners"):
        assert k in s, k
    for p in skills.PHASES:
        assert set(s["phases"][p]) >= {"loss_s", "loss_pm", "score", "corners", "spread_s"}
    assert set(s["corners"][0]) >= {"id", "lift", "n", "markers", "loss", "loss_raw"}
    r = json.loads((FX / "sample_skills_reason.json").read_text())
    assert r["reason"] == "not_enough_clean_laps" and "phases" not in r
    pr = json.loads((FX / "sample_profile.json").read_text())
    assert set(pr) >= {"version", "caps_pm", "groups", "weakest", "n_races"}
    g = pr["groups"][0]["phases"]["exit"]
    assert set(g) >= {"latest", "best_score", "trend", "races", "weekly", "seasons"}
    q = json.loads((FX / "sample_session_quali.json").read_text())
    assert q["kind"] == "qualifying" and q["quali"]["laps"]


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
