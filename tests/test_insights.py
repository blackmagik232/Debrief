"""Coaching summary tests (2.2.1): fix first / bad habits / good habits, and refreshing saved reviews.

    python3 tests/test_insights.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from debrief import insights, report  # noqa: E402


def corner(cid, avail, iqr=0.05, ref_gap=None):
    return {"id": cid, "apex_m": 100.0, "start_m": 0.0, "end_m": 200.0, "best_t": 10.0, "median_t": 10.0 + avail,
            "p25_t": 10.0, "avail": avail, "iqr": iqr, "ref_gap": ref_gap, "best_lap": 3,
            "summary": {"gear": 3, "min_mph": 60.0}}


def tech(cid, **typ):
    best = {"brake_on": 500.0, "min_mph": 60.0, "full_thr": 700.0, "coast_s": 0.5, "gear": 3, "peak_brake": 90.0}
    return {"id": cid, "best": best, "typical": dict(best, **typ), "ref": None}


def base(**kw):
    laps = [{"n": i, "t": 90.0 + (0.1 * (i % 3)), "clean": True} for i in range(1, 13)]
    d = {"laps": laps, "best": {"n": 2, "t": 90.0, "clean": True}, "median": 90.1, "refs": [],
         "counts": {"unclean": [], "slow": [], "traffic": [], "clean": 12}, "trend_total": 0.05,
         "consistency": {"score": 80, "label": "Very consistent", "std": 0.1,
                         "by_corner": [{"id": "C1", "iqr": 0.04}, {"id": "C2", "iqr": 0.2}, {"id": "C3", "iqr": 0.05}]},
         "corners": [corner("C1", 0.03), corner("C2", 0.21), corner("C3", 0.09)],
         "technique": [tech("C1"), tech("C2", brake_on=485.0, min_mph=57.0), tech("C3", coast_s=0.8, gear=2)]}
    d.update(kw)
    return d


def test_fix_first_ranked_with_tips():
    out = insights.build(base())
    assert [f["corner"] for f in out["fix"]] == ["C2", "C3"]          # C1 (0.03 s) is under the bar
    c2 = out["fix"][0]
    assert c2["gain"] == 0.21 and any("brake 15 m later" in t for t in c2["tips"]), c2["tips"]
    assert any("3 mph more" in t for t in c2["tips"])
    c3 = out["fix"][1]
    assert any("coasting" in t for t in c3["tips"]) and any("gear 3" in t for t in c3["tips"])
    assert out["time_to_find"] == 0.3 and "0.30 s a lap" in out["verdict"][1]


def test_habits_need_thresholds():
    out = insights.build(base())
    bad = {b["key"] for b in out["bad"]}
    good = {g["key"] for g in out["good"]}
    assert bad == {"vary"}, bad                       # only C2 varies enough to mention
    assert {"clean", "steady", "consistent"} <= good, good
    # problems show up once they clear their thresholds
    laps = [{"n": 1, "t": 92.0, "clean": True}] + [{"n": i, "t": 90.1, "clean": i not in (5, 7)} for i in range(2, 13)]
    out = insights.build(base(laps=laps, trend_total=0.6,
                              counts={"unclean": [5, 7], "slow": [1, 9, 11], "traffic": [4], "clean": 9},
                              consistency={"score": 30, "label": "Scattered", "by_corner": []}))
    bad = {b["key"] for b in out["bad"]}
    assert {"unclean", "lap1", "offpace", "traffic", "fade", "scattered"} <= bad, bad
    assert "clean" not in {g["key"] for g in out["good"]}


def test_nothing_to_fix_and_old_reviews():
    out = insights.build(base(corners=[corner("C1", 0.01), corner("C2", 0.02)]))
    assert out["fix"] == [] and "No corner stands out" in out["verdict"][1]
    # an old review with almost nothing in it still gives a summary instead of failing
    out = insights.build({"laps": [], "best": {"n": 1, "t": 80.0}})
    assert out["verdict"] and out["fix"] == [] and out["bad"] == []


def test_comparison_gap_used_when_bigger():
    refs = [{"key": "ref0", "name": "Ann Smith", "t": 89.5, "main": True}]
    d = base(refs=refs, corners=[corner("C1", 0.02, ref_gap=0.4), corner("C2", 0.21)])
    out = insights.build(d)
    assert out["fix"][0]["corner"] == "C1" and out["fix"][0]["basis"] == "ref" and "Ann" in out["fix"][0]["why"]
    assert "+0.50 s against Ann" in out["verdict"][0]


def test_refresh_saved_reviews():
    lib = Path(tempfile.mkdtemp(prefix="debrief-ins-")) / "lib"
    subprocess.run([sys.executable, str(ROOT / "tests" / "make_fixture_library.py"), str(lib)], check=True,
                   capture_output=True)
    revs = sorted((lib / "reviews").glob("*/review.json"))
    assert revs
    n = report.refresh_all(lib)
    assert n == len([p for p in revs if json.loads(p.read_text()).get("laps")])
    assert report.refresh_all(lib) == 0                 # once per layout version
    d = json.loads(revs[0].read_text())
    assert "insights" in d and (revs[0].parent / "index.html").read_text().count('id="coach"') == 1


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
