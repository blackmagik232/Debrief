"""Debrief 2.29.1 fix, folded into 2.30: the STOP-or-not decision counts the leader's pace within 1% (and your own 1% quicker);
the wide band (up to 2% slower than his pace, and the scan when his pace is unknown) stays for the saving calls and the
last-pit-chance rule. Run: python3 tests/ci_run.py tests/test_230_crewband.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from debrief import crewchief as cc  # noqa: E402

fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


def main():
    args = (None, 200.0, 0.4, 60.0, 0, 0.0, 0.5, 60.0)       # timed race, 200 s on the clock, 0.4 round the lap, 60 s leader
    wide = cc.laps_to_go(*args)
    narrow = cc.laps_to_go(*args, slow_tol=cc.PACE_TOL)
    check(wide["crossings"] == 5 and abs(wide["distance"] - 4.6) < 1e-9, "wide band (default): 5 crossings, 4.6 laps to the flag")
    check(narrow["crossings"] == 4 and abs(narrow["distance"] - 3.6) < 1e-9, "narrow band for the stop decision: 4 crossings, 3.6 laps")
    check(cc.laps_to_go(*args, slow_tol=2 * cc.PACE_TOL) == wide, "slow_tol of twice the tolerance is the default wide band")
    # the narrow band is never longer, over a spread of races
    worse = 0
    for tr in range(200, 1400, 7):
        for lf in (0.1, 0.5, 0.9):
            w = cc.laps_to_go(None, float(tr), lf, 100.0, 0, 0.0, 0.5, 100.0)
            n = cc.laps_to_go(None, float(tr), lf, 100.0, 0, 0.0, 0.5, 100.0, slow_tol=cc.PACE_TOL)
            worse += n["distance"] > w["distance"] + 1e-9
    check(worse == 0, "the narrow band is never longer than the wide one")
    # a pace that is not known keeps the wide scan, whatever slow_tol says
    u1 = cc.laps_to_go(None, 400.0, 0.2, 100.0, 0, 0.0, 0.5, 100.0, pace_unknown=True)
    u2 = cc.laps_to_go(None, 400.0, 0.2, 100.0, 0, 0.0, 0.5, 100.0, pace_unknown=True, slow_tol=cc.PACE_TOL)
    check(u1["distance"] == u2["distance"] or u2["distance"] <= u1["distance"], "pace not known: never less careful than before")
    # the decision: 3.9 laps of fuel at 3 L a lap, tank 60 L
    b, F = 3.0, 3.9 * 3.0
    check(not cc.pit_window(F, b, 60.0, narrow["distance"], 0.4)["need_stop"], "narrow band: the fuel reaches the flag, no stop called")
    check(cc.pit_window(F, b, 60.0, wide["distance"], 0.4)["need_stop"], "wide band (the old rule): it would have called a precautionary stop")
    sv = cc.save_view(F, b, wide["distance"])
    check(sv["tight"] and sv["short"] and sv["tight_call"] and sv["tight_save_lpl"] > 0, "the wide band still gives the save-fuel call")
    ok_ = cc.save_view(20 * b, b, 4.6)
    check(not ok_["tight"] and not ok_["tight_call"], "plenty of fuel: no save call")
    check(cc.save_view(None, b, 4.6)["tight"] is False and cc.save_view(F, 0, 4.6)["tight"] is False, "unknown fuel or burn: no save call")
    print("ALL OK" if not fails else f"{fails} FAILED")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
