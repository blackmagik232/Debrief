"""Debrief 2.30 live re-plan: the rest of the race from where it is now (debrief/replan.py). The hand checks in the module's
text are asserted here; the cost (24 h, 6 drivers) is checked with a loose bound.

    python3 tests/ci_run.py tests/test_230_replan.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from debrief import replan as R  # noqa: E402

fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


# tank 80 L, spare 1 lap, A B C all 100 s a lap and 3.0 L a lap, pit lane 25 s, fuel 5 L/s (a swap keeps the car 30 s)
INP = {"minutes": None, "laps": 30, "tank_l": 80, "reserve_laps": 1, "pit_lane_s": 25, "fuel_rate_lps": 5, "tyre_s": 0,
       "tyre_stints": 0, "fair_share": False, "stints_in_row": 1,
       "drivers": [{"name": n, "lap_t": 100, "burn_l": 3.0} for n in "ABC"]}


def go(**k):
    st = {"crossings": 30, "driver": "A", "fuel_l": 41, "lap_f": 0}
    st.update(k)
    return R.replan(INP, st)


def main():
    # 1. 12 laps for A, then B for 18: 30 x 100 + 55
    r = go()
    check(r["finish_s"] == 3055.0 and r["stops"] == 1, "hand check 1: 30 to go, 41 L: one stop, 3055 s")
    check([(s["driver"], s["laps"]) for s in r["stints"]] == [("A", 12), ("B", 18)], "hand check 1: A 12 laps then B 18")
    # 2. no stop at all
    r = go(crossings=20, fuel_l=80)
    check(r["finish_s"] == 2000.0 and r["stops"] == 0 and r["options"] == [], "hand check 2: 20 to go, full tank: no stop, 2000 s")
    # 3. the stop, and the saving that drops it
    r = go(crossings=27, fuel_l=80)
    check(r["finish_s"] == 2755.0 and r["stops"] == 1, "hand check 3: 27 to go: A 25 laps, stop 55 s, B 2 laps = 2755 s")
    o = r["options"][0]
    check(o["key"] == "save" and o["save_pct"] == 5 and abs(o["finish_s"] - 2710.8) < 0.01 and o["better"] and abs(o["delta_s"] + 44.2) < 0.06,
          "hand check 3: saving 5% (the smallest that drops the stop) finishes 2710.8 s, 44.2 s quicker")
    # 4. half way round the lap
    r = go(lap_f=0.5)
    check(r["finish_s"] == 3005.0 and [(s["driver"], s["laps"]) for s in r["stints"]] == [("A", 13), ("B", 17)],
          "hand check 4: half way round the lap: 3005 s (A 13 laps, B 17)")
    # 5. not enough fuel to reach the line: pit now
    r = go(fuel_l=3.5)
    check(r["finish_s"] == 3110.0 and r["stops"] == 2 and r["stints"][0]["laps"] == 0 and any("pit now" in n for n in r["notes"]),
          "hand check 5: 3.5 L left: pit now, two stops, 3110 s, and it says so")
    # a driver who is late is skipped, and the plan says it works the next one in
    r = R.replan(INP, {"crossings": 30, "driver": "A", "fuel_l": 41, "lap_f": 0, "delays": {"B": 60}})
    check(r["stints"][1]["driver"] == "C", "a driver who is 60 minutes late is not given the next stint (C takes it)")
    # an unknown fuel: planned from the next stint, said in words
    r = R.replan(INP, {"crossings": 30})
    check(not r["from_state"] and any("isn't known" in n for n in r["notes"]), "no fuel or driver: planned from a fresh start and says so")
    # nothing left to plan
    try:
        R.replan(INP, {})
        check(False, "no race left raises")
    except ValueError as e:
        check("how much race is left" in str(e), "no distance: a plain-words error")
    # pace and burn ratios scale every driver
    slow = go(pace_ratio=1.04)
    check(slow["finish_s"] > 3055.0 and any("Lap times scaled" in n for n in slow["notes"]), "a 4% slower race moves the finish and says why")
    # drift: when a re-plan is due
    check(R.drift({}, None, 5, False, 0, 1.0, 1.0) == "first look", "drift: the first look")
    last = {"lap": 5, "caution": False, "stops": 0, "pace": 1.0, "burn": 1.0}
    check(R.drift({}, last, 5, False, 0, 1.0, 1.0) is None, "drift: nothing changed, nothing to do")
    check(R.drift({}, last, 6, False, 0, 1.0, 1.0) == "new lap", "drift: once a lap")
    check(R.drift({}, last, 5, True, 0, 1.0, 1.0) == "caution began", "drift: a caution begins")
    check(R.drift({}, dict(last, caution=True), 5, False, 0, 1.0, 1.0) == "caution ended", "drift: a caution ends")
    check(R.drift({}, last, 5, False, 1, 1.0, 1.0) == "a stop was made", "drift: a stop was made")
    check(R.drift({}, last, 5, False, 0, 1.04, 1.0) == "pace changed", "drift: pace moved 4%")
    check(R.drift({}, last, 5, False, 0, 1.01, 1.02) is None, "drift: small changes are ignored")
    check(R.drift({}, last, 5, False, 0, 1.0, 1.05) == "fuel use changed", "drift: fuel use moved 5%")
    # cost: 24 h, 6 drivers, options included (the budget is 200 ms; the bound here is loose for slow CI machines)
    drivers = [{"name": f"Driver {k}", "lap_t": 98 + k * 0.4, "burn_l": 3.1 + k * 0.05, "tz": k} for k in range(6)]
    big = {"minutes": 1440, "tank_l": 120, "pit_lane_s": 32, "fuel_rate_lps": 2.6, "tyre_s": 22, "tyre_stints": 1,
           "drivers": drivers, "reserve_laps": 1, "fair_share": True}
    st = {"time_left_s": 12 * 3600.0, "elapsed_s": 12 * 3600.0, "driver": "Driver 2", "fuel_l": 60.0, "lap_f": 0.4,
          "pace_ratio": 1.01, "burn_ratio": 1.02, "stints_on_tyres": 1, "delays": {"Driver 3": 20}}
    worst = 0.0
    for _ in range(3):
        t0 = time.perf_counter()
        res = R.replan(big, st)
        worst = max(worst, time.perf_counter() - t0)
    check(res["stops"] > 5 and len(res["options"]) >= 1, "24 h x 6 drivers: a real plan with options")
    check(worst < 1.0, f"24 h x 6 drivers re-plan worst {worst * 1000:.0f} ms (budget 200 ms on the target PC; under 1 s here)")
    print("ALL OK" if not fails else f"{fails} FAILED")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
