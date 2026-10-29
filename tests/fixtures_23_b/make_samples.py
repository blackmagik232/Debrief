"""Write the 23-B sample fixtures (shapes from spec 1.1) for other workstreams to build against.

    python3 tests/fixtures_23_b/make_samples.py

Outputs (next to this file):
  sample_skills.json              skills block (scored)
  sample_skills_reason.json       skills block with a reason (not enough clean laps)
  sample_session_practice.json    session_review for a practice (3 runs, progress, targets)
  sample_session_quali.json       session_review for a qualifying session (quali lap strip)
  sample_library_rows.json        library rows' "skills" values (compact shape) incl. None
  sample_profile.json             profile.build output (EXTRA.skills for library.html)
  sample_review_practice.json     a full synthetic practice review dict carrying skills + session_review
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))
sys.path.insert(0, str(HERE))

import synth  # noqa: E402
from debrief import profile, session_review, skills  # noqa: E402


def mixed_review(session="Race", seed=7, n_laps=16, **kw):
    """Typical laps lose a little in several phases (with noise), better laps don't."""
    rng = random.Random(seed)
    extra = {}
    for n in range(1, n_laps + 1):
        if n in (3, 7, 9, 12):        # better laps
            continue
        extra[n] = [(1850.0, 1970.0, max(0.0, rng.gauss(0.04, 0.02))),   # C2 braking
                    (1970.0, 2000.0, max(0.0, rng.gauss(0.03, 0.02))),   # C2 entry
                    (2060.0, 2250.0, max(0.0, rng.gauss(0.12, 0.04))),   # C2 exit
                    (840.0, 1050.0, max(0.0, rng.gauss(0.06, 0.03)))]    # C1 exit
    return synth.make_review(n_laps=n_laps, extra=extra, session=session, **kw)


def practice_review():
    nums = [2, 3, 4, 5, 6, 7, 9, 10, 11, 12, 13, 14, 15, 17, 18, 19, 20, 21]   # gaps at 8 and 16 (pit)
    rng = random.Random(3)
    extra = {}
    for k, n in enumerate(nums):
        late = k >= 6
        extra[n] = [(2060.0, 2250.0, max(0.0, rng.gauss(0.10 if not late else 0.05, 0.03))),
                    (1850.0, 1970.0, max(0.0, rng.gauss(0.03, 0.015)))]
    for n in (12, 18, 20):
        extra[n] = []
    starts = []
    t = 1792000000.0
    for k, n in enumerate(nums):
        starts.append(round(t, 1))
        t += 75.1 + (400.0 if n in (7, 15) else 0.0)
    fuel = [40.0 - 2.4 * i for i in range(6)] + [38.5 - 2.4 * i for i in range(7)] + [40.0 - 2.4 * i for i in range(5)]
    d = synth.make_review(lap_numbers=nums, extra=extra, session="Practice", starts=starts, fuel=fuel, sectors=True)
    return d


def quali_review():
    jit = {1: 1.2, 2: 0.4, 3: 0.0, 4: 0.3, 5: 2.5}
    d = synth.make_review(n_laps=5, session="Qualifying", lap_t_jitter=jit, sectors=True)
    for l in d["laps"]:
        l["sectors"] = [round(l["t"] * f, 3) for f in (0.33, 0.34, 0.33)]
    d["laps"][3]["sectors"][0] = round(d["laps"][2]["sectors"][0] - 0.1, 3)   # lap 4 had a better S1
    return d


def main():
    out = {}
    race = mixed_review()
    out["sample_skills.json"] = skills.for_review(race)
    out["sample_skills_reason.json"] = skills.for_review(synth.make_review(n_laps=5))
    prac = practice_review()
    out["sample_session_practice.json"] = session_review.for_review(prac)
    out["sample_session_quali.json"] = session_review.for_review(quali_review())
    prac["skills"] = skills.for_review(prac)
    prac["session_review"] = out["sample_session_practice.json"]
    out["sample_review_practice.json"] = prac
    # library rows: 6 races over ~5 weeks, two classes, getting better on exits
    rows = []
    days = ["2026-09-10", "2026-09-17", "2026-09-24", "2026-10-01", "2026-10-08", "2026-10-15"]
    for i, day in enumerate(days):
        car = "Ford Mustang GT4" if i % 2 == 0 else "Porsche 911 GT3 R (992)"
        lp = {"braking": round(1.1 - 0.05 * i, 2), "entry": round(1.4 - 0.02 * i, 2), "mid": 0.6,
              "exit": round(2.9 - 0.3 * i, 2)}
        lp["overall"] = round(sum(lp.values()), 2)
        rows.append({"dir": f"{day}_sample_{i}", "date": day, "started_at": f"{day}T19:00:00+00:00",
                     "track_short": "Roval" if i % 2 == 0 else "Indianapolis", "car": car,
                     "season": {"name": "2026 Season 3" if day < "2026-09-15" else "2026 Season 4"},
                     "skills": {"v": 1, "n_clean": 12 + i, "lap_t": 85.6, "loss_pm": lp}})
    rows.append({"dir": "2026-10-16_old", "date": "2026-10-16", "started_at": "", "track_short": "Roval",
                 "car": "Ford Mustang GT4", "season": None, "skills": None})
    out["sample_library_rows.json"] = [{"dir": r["dir"], "skills": r["skills"]} for r in rows]
    out["sample_profile.json"] = profile.build(rows)
    for name, obj in out.items():
        (HERE / name).write_text(json.dumps(obj, indent=1), encoding="utf-8")
        print("wrote", HERE / name)


if __name__ == "__main__":
    main()
