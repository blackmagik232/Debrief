"""Write the 23-C sample review dicts (spec 1.1 shapes) that tests/test_23_c.py renders.

    python3 tests/fixtures_23_c/make_samples.py

The base race comes from the real engine (same synthetic generator as tests/test_fix_d.py). The 2.3 blocks
are added in the agreed shapes:
  skills, session_review   from 23-B's modules when /home/claude/work/23-B is present, otherwise the
                           committed b_sample_*.json files (copied from 23-B's fixtures)
  technique_plus           written here in the spec 1.1 shape (23-A had no sample yet)
  brake_spread             worked out here from corners[].per_lap with the spec 1.2 rule
Outputs (next to this file): sample_review_new.json, sample_review_old.json, sample_review_nomap.json,
sample_review_reason.json, sample_review_practice.json, sample_review_quali.json, sample_review_a.json, sample_review_a_steer.json.
The a_*.json files are copies of 23-A's fixtures; b_*.json are copies of 23-B's.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

B_DIR = Path("/home/claude/work/23-B/debrief")


def _mod(name):
    p = B_DIR / f"{name}.py"
    if not p.exists():
        return None
    # load 23-B's module as debrief.<name> so its relative imports resolve against this copy's package
    import debrief  # noqa: F401
    spec = importlib.util.spec_from_file_location(f"debrief.{name}", p)
    m = importlib.util.module_from_spec(spec)
    sys.modules[f"debrief.{name}"] = m
    spec.loader.exec_module(m)
    return m


def brake_spread(d):
    """Spec 1.2 spread_for_review: corners with >= 6 clean laps that have brake_on."""
    clean = {l["n"] for l in d["laps"] if l.get("clean")}
    out = []
    for c in d["corners"]:
        pl = c.get("per_lap") or {}
        laps = {n: v["brake_on"] for n, v in pl.items() if int(n) in clean and v.get("brake_on") is not None}
        if len(laps) < 6:
            continue
        xs = np.array(list(laps.values()), float)
        iqr = float(np.percentile(xs, 75) - np.percentile(xs, 25))
        lab = "locked" if iqr <= 5 else "steady" if iqr <= 12 else "varies" if iqr <= 25 else "all over"
        out.append({"id": c["id"], "n": len(xs), "iqr_m": round(iqr, 1), "std_m": round(float(np.std(xs, ddof=1)), 1),
                    "median_m": round(float(np.median(xs)), 1), "label": lab, "laps": {k: round(v, 1) for k, v in laps.items()}})
    return {"version": 1, "corners": out, "params": {"locked_m": 5, "steady_m": 12, "varies_m": 25, "min_laps": 6}}


def technique_plus(d, steering=False):
    """A spec 1.1 technique_plus block built from per_lap (plausible values; the page only reads them)."""
    rng = np.random.default_rng(11)
    cs = []
    for k, c in enumerate(d["corners"]):
        pl = c.get("per_lap") or {}
        lift = k == 3
        per = {}
        for n, v in pl.items():
            mn = v.get("min_at") or c["apex_m"]
            per[n] = {"trail_m": None if lift else round(float(60 + rng.normal(0, 12)), 1),
                      "off_vs_apex_m": None if lift else round(float(-25 + rng.normal(0, 8)), 1),
                      "pickup_vs_apex_m": round(float(15 + rng.normal(0, 6)), 1), "ramp_m": round(float(95 + rng.normal(0, 10)), 1),
                      "lifts": int(rng.random() < 0.2), "tv_excess_pct": round(float(abs(rng.normal(4, 2))), 1),
                      "steer_corr": int(rng.integers(0, 3)) if steering else None}
            _ = mn
        brk = None if lift else {
            "typical": {"peak_at_m": c["apex_m"] - 140, "release_start_m": c["apex_m"] - 110, "release_end_m": c["apex_m"] - 25,
                        "trail_m": 60.0, "off_vs_apex_m": -25.0, "trail_share": 0.55, "shape": [0.7, 0.42, 0.2], "shape_label": "gradual"},
            "better": {"peak_at_m": c["apex_m"] - 140, "release_start_m": c["apex_m"] - 115, "release_end_m": c["apex_m"] - 15,
                       "trail_m": 85.0, "off_vs_apex_m": -15.0, "trail_share": 0.62, "shape": [0.74, 0.48, 0.22], "shape_label": "gradual"}}
        thr = {"typical": {"pickup_m": c["apex_m"] + 20, "pickup_vs_apex_m": 20.0, "ramp_m": 100.0, "ramp_pct_per_10m": 8.5,
                           "lifts": 0, "tv_excess_pct": 5.0},
               "better": {"pickup_m": c["apex_m"] + 12, "pickup_vs_apex_m": 12.0, "ramp_m": 90.0, "ramp_pct_per_10m": 9.4,
                          "lifts": 0, "tv_excess_pct": 3.0}}
        steer = {"typical": {"steer_peak_deg": 80.0, "steer_corr": 2, "unwind_vs_apex_m": 30.0},
                 "better": {"steer_peak_deg": 78.0, "steer_corr": 1, "unwind_vs_apex_m": 22.0}} if steering else None
        notes = ([] if lift else ["On your quicker laps here you tended to ease off the brake over a longer stretch (about 85 m against 60 m)."])
        if k == 6:
            brk = thr = None
            notes = ["Not enough clean laps through this corner to say."]
        cs.append({"id": c["id"], "apex_m": c["apex_m"], "n_clean": len(pl), "lift": lift, "braking": brk, "throttle": thr,
                   "steering": steer, "links": [], "notes": notes, "per_lap": per})
    return {"version": 1, "step_m": 5, "channels": {"brake": True, "throttle": True, "steering": steering},
            "params": {"brake_on_pct": 5, "release_frac": 0.8, "pickup_pct": 10, "full_pct": 95, "lift_hyst_pct": 10,
                       "steer_rev_deg": 3, "min_laps": 5}, "corners": cs, "reason": None}


OLD_DROP = ("insights", "consistency", "technique", "best_worst", "stint", "segments", "on_track", "conditions", "season",
            "pb", "technique_plus", "skills", "session_review", "brake_spread", "channels")


def main():
    import test_fix_d as fd
    skills = _mod("skills")
    sess = _mod("session_review")
    race = fd._race(with_ref=True)
    new = copy.deepcopy(race)
    new["brake_spread"] = brake_spread(new)
    new["technique_plus"] = technique_plus(new, steering=True)
    new["channels"] = {"brake": True, "throttle": True, "steering": True, "gps": True}
    new["skills"] = skills.for_review(new) if skills else json.loads((HERE / "b_sample_skills.json").read_text())
    from debrief import insights
    new["insights"] = insights.build(new)
    out = {"sample_review_new.json": new}
    old = copy.deepcopy(race)
    for k in OLD_DROP:
        old.pop(k, None)
    out["sample_review_old.json"] = old
    nomap = copy.deepcopy(new)
    nomap["map"] = []
    out["sample_review_nomap.json"] = nomap
    rs = copy.deepcopy(new)
    rs["skills"] = json.loads((HERE / "b_sample_skills_reason.json").read_text())
    out["sample_review_reason.json"] = rs
    prac = json.loads((HERE / "b_sample_review_practice.json").read_text())
    if sess:
        prac["session_review"] = sess.for_review(prac)
    prac["brake_spread"] = brake_spread(prac)
    out["sample_review_practice.json"] = prac
    q = copy.deepcopy(race)
    q["meta"]["session"] = "Qualifying"
    q["session_review"] = json.loads((HERE / "b_sample_session_quali.json").read_text())
    q["session_review"]["quali"]["best_lap"] = q["best"]["n"]
    out["sample_review_quali.json"] = q
    # 23-A's real engine output (technique_plus, brake_spread, channels, new insights items), plus 23-B's skills
    a = json.loads((HERE / "a_sample_review.json").read_text())
    if skills:
        a["skills"] = skills.for_review(a)
    out["sample_review_a.json"] = a
    a2 = copy.deepcopy(a)   # steering present: the steering-row sample from 23-A on the same corner ids
    st = json.loads((HERE / "a_sample_technique_plus_steering.json").read_text())
    tp = a2["technique_plus"]   # graft 23-A's steering values onto this review's own corners
    tp["channels"] = dict(tp.get("channels") or {}, steering=True)
    st0 = next(c for c in st["corners"] if c.get("steering"))
    lap0 = next(iter(st0["per_lap"].values()))
    for c in tp["corners"]:
        c["steering"] = copy.deepcopy(st0["steering"])
        for v in (c.get("per_lap") or {}).values():
            for k in ("steer_peak_deg", "steer_corr", "unwind_vs_apex_m"):
                v[k] = lap0[k]
    out["sample_review_a_steer.json"] = a2
    for name, d in out.items():
        (HERE / name).write_text(json.dumps(d), encoding="utf-8")
        print("wrote", name, len(json.dumps(d)) // 1024, "KB")


if __name__ == "__main__":
    main()
