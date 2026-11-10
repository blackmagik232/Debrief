"""2.30.0 review round 3: a normal-length swap is seen when iRacing lists every team driver, and the same takeover is never
announced twice after a long repair stall. Scenarios from the independent reviewer's r3_adv.py."""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
sys.modules.setdefault("irsdk", None)

import endu_sim as ES                       # noqa: E402
from debrief import crewchief as cc         # noqa: E402
from debrief import endurance as E          # noqa: E402
from debrief import raceroom as RR          # noqa: E402

STD = [(29, 58, 1, 35), (58, 58, 2, 35), (87, 50, 0, 35)]
FAILS = []


def check(ok, what):
    print(("ok   " if ok else "FAIL ") + what)
    if not ok:
        FAILS.append(what)


def run(stops, pc, listing):
    sim = ES.EnduSim(pc_user=pc, listing=listing)
    for lap, add, to, stall in stops:
        sim.stop(lap, add, stall=stall, to=to)
    base = {"inputs": ES.plan_inputs(), "title": "t"}
    room = RR.RaceRoom(units="kmh", plan_for=lambda tr: base, iam=lambda: ES.DRIVERS[pc], wall=lambda: 1.8e9 + sim.t)
    chief = cc.CrewChief(units="kmh", clock=lambda: sim.t)
    ver, alerts = None, []
    for own, f in sim.run():
        secs = None
        if sim.info_version != ver:
            ver = sim.info_version
            secs = sim.sections()
            chief.set_info(secs, ver)
        o2 = room.pre(own, f, secs, ver, sim.t)
        chief.feed(o2, f, now=sim.t)
        room.post(chief, sim.t, own)
        for a in room.take_alerts():
            alerts.append(a["kind"])
    return alerts


def test_the_swap_stall_rule_sits_below_iracings_swap_timer():
    check(RR.SWAP_STALL_FRAC * E.SWAP_S <= 25.0, "a 25 s swap stall is long enough to count when names are unknown")


def test_a_minimum_length_swap_is_seen_on_every_pc_and_listing():
    for stall in (30, 25):
        stops = [(lap, add, to, stall) for lap, add, to, _ in STD]
        for listing in ("all", "current"):
            for pc in (1, 2):
                al = run(stops, pc, listing)
                check(al == ["swap_15", "swap_5"], f"{stall} s swaps, listing {listing}, {ES.DRIVERS[pc]}: one 15 and one 5 ({al})")


def test_a_long_repair_stall_never_repeats_the_same_takeover():
    stops = [(27, 0, None, 45), (31, 58, 1, 35), (60, 58, 2, 35), (89, 50, 0, 35)]
    for listing in ("all", "current"):
        for pc in (1, 2):
            al = run(stops, pc, listing)
            check(al == ["swap_15", "swap_5"], f"45 s repair at lap 27, listing {listing}, {ES.DRIVERS[pc]}: one 15 and one 5 ({al})")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print("--", name)
            fn()
    print("ALL OK" if not FAILS else f"{len(FAILS)} FAILED")
    sys.exit(1 if FAILS else 0)
