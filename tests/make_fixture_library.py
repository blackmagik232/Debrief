"""Fabricate a small library of plausible review.json files for testing the library, season, goals
and prep pages. Every file is marked meta.test_data = true.

    python3 tests/make_fixture_library.py <library folder> [--goals]

Two combos (Charlotte Roval / Ford Mustang GT4 and Indianapolis road course / Porsche 911 GT3 R),
two seasons (2026 Season 3 and 2026 Season 4). Some reviews use the v2 keys (combo_ids, irating,
season, consistency, technique, result); some are old-format (none of them), to prove back-compat.
Deterministic (seeded), so tests can rely on the numbers.
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

ROVAL = {"track": "Charlotte Motor Speedway (Roval 2019)", "car": "Ford Mustang GT4", "track_id": 468,
         "car_id": 190, "L": 3600.0, "base": 85.6,
         "corners": [("C1", 210), ("C2", 735), ("C3", 1205), ("C4", 1485), ("C5", 2505), ("C6", 3465)]}
INDY = {"track": "Indianapolis Motor Speedway (Road Course)", "car": "Porsche 911 GT3 R (992)", "track_id": 380,
        "car_id": 152, "L": 3925.0, "base": 83.9,
        "corners": [("C1", 420), ("C2", 980), ("C3", 1460), ("C4", 1900), ("C5", 2650), ("C6", 3300), ("C7", 3700)]}
S3 = {"id": 34, "name": "2026 Season 3", "shortName": "2026S3"}
S4 = {"id": 35, "name": "2026 Season 4", "shortName": "2026S4"}

# (combo, iso start, improvement s, v2?, season, irating, result or None)
RACES = [
    (INDY, "2026-07-02T19:05:00+00:00", 0.00, False, S3, None, None),
    (INDY, "2026-07-16T19:05:00+00:00", 0.35, False, S3, None, None),
    (INDY, "2026-08-06T19:05:00+00:00", 0.55, True, S3, 1042, {"start_pos": 14, "finish_pos": 9, "field_size": 22, "incidents": 4, "irating_before": 1042, "irating_after": 1071, "sr_before": 2.81, "sr_after": 2.86}),
    (ROVAL, "2026-08-20T23:30:00+00:00", 0.00, False, S3, None, None),
    (INDY, "2026-09-03T19:05:00+00:00", 0.80, True, S3, 1071, {"start_pos": 11, "finish_pos": 12, "field_size": 20, "incidents": 8, "irating_before": 1071, "irating_after": 1058, "sr_before": 2.86, "sr_after": 2.71}),
    (ROVAL, "2026-09-10T23:30:00+00:00", 0.42, True, S3, 1058, None),
    (INDY, "2026-09-24T19:05:00+00:00", 0.70, True, S4, 1066, {"start_pos": 10, "finish_pos": 6, "field_size": 21, "incidents": 2, "irating_before": 1066, "irating_after": 1102, "sr_before": 2.74, "sr_after": 2.93, "sof": 1380}),
    (ROVAL, "2026-10-01T23:30:00+00:00", 0.61, True, S4, 1102, {"start_pos": 9, "finish_pos": 7, "field_size": 18, "incidents": 2, "irating_before": 1102, "irating_after": 1117, "sr_before": 2.93, "sr_after": 3.02}),
    (INDY, "2026-10-08T19:05:00+00:00", 1.02, True, S4, 1117, None),
]


def fake_review(combo, started, gain, v2, season, irating, result, rng):
    base = combo["base"] - gain
    n = 20
    laps = []
    for i in range(1, n + 1):
        t = base + abs(rng.gauss(0.9, 0.45)) + (0.012 * i)
        laps.append({"n": i, "t": round(t, 3), "clean": rng.random() > 0.2, "sectors": None})
    best = min(laps, key=lambda l: l["t"])
    ts = sorted(l["t"] for l in laps)
    median = ts[len(ts) // 2]
    corners = []
    avails = [abs(rng.gauss(0.09, 0.05)) + 0.01 for _ in combo["corners"]]
    order = sorted(range(len(avails)), key=lambda i: -avails[i])
    for i, (cid, apex) in enumerate(combo["corners"]):
        apex = apex + rng.randint(-15, 15)
        mn = 60 + rng.random() * 30
        corners.append({
            "id": cid, "apex_m": float(apex), "start_m": float(max(0, apex - 300)), "end_m": float(apex + 220),
            "avail": round(avails[i], 4), "rank": order.index(i) + 1, "iqr": round(avails[i] * 1.8, 3),
            "median_t": 8.0, "best_t": 7.6, "best_lap": best["n"], "p25_t": 7.8, "ref_gap": None,
            "summary": {"min_mph": mn, "peak_brake": 80.0, "brake_on": float(apex - 110), "full_thr": float(apex + 60),
                        "abs_s": 0.2, "coast_s": 0.4, "exit_mph": mn + 22, "gear": 3},
            "drivers": [{"metric": "min_mph", "label": "minimum speed", "unit": "mph", "r": -0.62, "slope": -0.03,
                         "n": 14, "p25": mn - 1.5, "p75": mn + 1.6, "median": mn},
                        {"metric": "brake_on", "label": "brake point", "unit": "m", "r": -0.55, "slope": -0.004,
                         "n": 14, "p25": float(apex - 118), "p75": float(apex - 104), "median": float(apex - 110)}]
            if avails[i] > 0.08 else [],
            "coaching": "", "per_lap": {str(best["n"]): {"t": 7.6, "min_mph": mn + 1, "gear": 3}}, "refs": {}})
    d = {"meta": {"driver": "Alex Driver", "event_id": "FIXTURE" + started[:10].replace("-", ""),
                  "car": combo["car"], "track": combo["track"], "started_at": started, "session": "Race",
                  "generated_at": started, "requests": 0, "test_data": True},
         "L": combo["L"], "step": 10, "laps": laps, "best": {"n": best["n"], "t": best["t"]},
         "median": median, "std_clean": round(0.25 + rng.random() * 0.35, 3), "optimal": None,
         "trend_per10": round(rng.uniform(-0.1, 0.35), 3), "corners": corners,
         "counts": {"analysed": n, "clean": sum(l["clean"] for l in laps), "unclean": [], "slow": [], "excluded": 0},
         "refs": [], "findings": [], "plan": []}
    if v2:
        d["version"] = 2
        d["combo_ids"] = {"track_id": combo["track_id"], "car_id": combo["car_id"]}
        d["irating"] = irating
        d["season"] = season
        sc = rng.randint(55, 88)
        d["consistency"] = {"score": sc, "std": d["std_clean"], "within_05": rng.randint(40, 80),
                            "within_10": rng.randint(70, 95),
                            "label": "Consistent" if sc >= 70 else "Up and down", "by_corner": [], "summary": ""}
        top = sorted(corners, key=lambda c: c["rank"])[:3]
        d["technique"] = [{"id": c["id"], "apex_m": c["apex_m"],
                           "best": {**c["summary"], "min_mph": c["summary"]["min_mph"] + 1.2},
                           "typical": dict(c["summary"]),
                           "ref": {**c["summary"], "min_mph": c["summary"]["min_mph"] + 2.5, "brake_on": c["summary"]["brake_on"] + 8},
                           "vs_ref": {"min_mph": -2.5, "brake_on": -8.0, "peak_brake": 0.0, "full_thr": 0.0,
                                      "coast_s": 0.0, "exit_mph": 0.0, "gear": 0},
                           "notes": ["On your quicker laps you tended to carry more minimum speed here."]} for c in top]
        if result:
            from debrief import results
            d["result"] = results.validate(result)
    return d


def main(folder: str, with_goals: bool = False) -> list:
    rng = random.Random(61)
    folder = Path(folder)
    made = []
    for combo, started, gain, v2, season, ir, res in RACES:
        from debrief.library import slugify
        dname = f"{started[:10]}_{slugify(combo['track'])}_{slugify(combo['car'])}_fx{started[5:7]}{started[8:10]}"
        d = folder / "reviews" / dname
        d.mkdir(parents=True, exist_ok=True)
        data = fake_review(combo, started, gain, v2, season, ir, res, rng)
        (d / "review.json").write_text(json.dumps(data), encoding="utf-8")
        if res and v2:
            (d / "result.json").write_text(json.dumps(data["result"]), encoding="utf-8")
        (d / "index.html").write_text("<!doctype html><meta charset=utf-8><title>Test review</title>"
                                      "<p style='font-family:sans-serif'>Test fixture review (no full page).</p>",
                                      encoding="utf-8")
        made.append(dname)
    if with_goals:
        from debrief import goals
        goals.save_goal(folder, {"track": ROVAL["track"], "car": ROVAL["car"], "track_id": 468, "car_id": 190,
                                 "target_s": "1:23.300", "note": "Top-5 pace in the GT4 Challenge"})
        goals.save_goal(folder, {"track": INDY["track"], "car": INDY["car"], "target_s": 82.5})
    return made


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    out = main(sys.argv[1], "--goals" in sys.argv)
    from debrief import library
    print(f"{len(out)} fixture reviews written; library: {library.rebuild(sys.argv[1])}")
