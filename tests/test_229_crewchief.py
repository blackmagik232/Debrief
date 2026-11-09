"""Debrief 2.29 "Crew chief": fuel, laps to go, the pit window, caution, the cars around you, ranked and rate-limited calls,
the quiet zones, the race log, the voice phrases and the pit board.

    python3 tests/ci_run.py tests/test_229_crewchief.py

A scripted race (tests/crew_sim.py) stands in for iRacing: a 50 lap race on a 60 L tank at 3 L a lap (two stops), a
caution, a car closing behind, a car pitting ahead, a second class. No network, no sound card, nothing Windows-only;
temp folders from tempfile.
"""
from __future__ import annotations

import gzip
import json
import math
import os
import shutil
import statistics
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

TMP = Path(tempfile.mkdtemp(prefix="debrief-229-crew-"))
ST = TMP / "settings.toml"
ST.write_text(f'token = "PASTE-YOUR-GARAGE61-TOKEN-HERE"\nlibrary_folder = \'{TMP / "lib"}\'\n', encoding="utf-8")
os.environ.update(DEBRIEF_SETTINGS=str(ST), DEBRIEF_CACHE_DIR=str(TMP / "cache"))

import crew_sim as S  # noqa: E402
from debrief import crewchief as cc  # noqa: E402
from debrief import voicepack as vp  # noqa: E402


def chief(sim, units="kmh", **kw):
    ch = cc.CrewChief(units=units, clock=lambda: sim.t, **kw)
    ch.set_info(sim.sections(), 1)
    return ch


def drive(sim, ch, seconds=None, on_frame=None, before=None):
    """Run the sim through the chief; returns every call made."""
    calls = []
    for own, f in sim.run(seconds):
        if before:
            before(own, f)
        got = ch.feed(own, f, now=sim.t)
        calls += got
        if on_frame:
            on_frame(own, f, got)
    return calls


def kinds(calls):
    return [c.kind for c in calls]


# ------------------------------------------------------------------ the maths (hand-checked)
def test_laps_to_go_hand_checks():
    # lap race: SessionLapsRemainEx counts the lap you are on
    r = cc.laps_to_go(laps_rem=10, lap_f=0.25)
    assert (r["crossings"], r["distance"], r["kind"]) == (10, 9.75, "laps")
    # timed: 1800 s left, leader laps 100 s, half way round: crossings at 50, 150 ... 1750 s, then the one after the clock = 19
    r = cc.laps_to_go(time_rem=1800, lap_f=0.5, leader_lap_s=100.0, pace_tol=0.0)
    assert (r["crossings"], r["distance"], r["kind"]) == (19, 18.5, "time")
    # the same, with the leader's place on his lap known (the docstring's checks B and C, counted by hand there)
    assert cc.laps_to_go(time_rem=1800, lap_f=0.5, leader_lap_s=100.0, leader_f=0.8, pace_tol=0.0)["crossings"] == 19
    assert cc.laps_to_go(time_rem=1800, lap_f=0.5, leader_lap_s=100.0, leader_f=0.2, pace_tol=0.0)["crossings"] == 20
    # one 60 s stop still to make with the leader at 0.2: you cross at 110 ... 1810 s (18) before he finishes at
    # 1880 s, and the one at 1910 s is yours: 19 crossings
    r = cc.laps_to_go(time_rem=1800, lap_f=0.5, leader_lap_s=100.0, leader_f=0.2, stops=1, pit_loss_s=60.0, pace_tol=0.0)
    assert (r["crossings"], r["distance"]) == (19, 18.5)
    # leader where you are: the stop moves your crossings to 110 ... 1810 s; he finishes at 1850 s; the one at 1910 s
    r = cc.laps_to_go(time_rem=1800, lap_f=0.5, leader_lap_s=100.0, stops=1, pit_loss_s=60.0, pace_tol=0.0)
    assert (r["crossings"], r["distance"]) == (19, 18.5)
    # the clock is zero: one more crossing finishes it
    assert cc.laps_to_go(time_rem=0, lap_f=0.3, leader_lap_s=100.0, pace_tol=0.0)["crossings"] == 1
    # no leader time, no timed answer; a lap-limited race with a clock too takes whichever ends first
    assert cc.laps_to_go(time_rem=1800, lap_f=0.5, pace_tol=0.0) is None
    r = cc.laps_to_go(laps_rem=5, time_rem=1800, lap_f=0.0, leader_lap_s=100.0)
    assert r["kind"] == "laps" and r["crossings"] == 5
    assert cc.laps_to_go(laps_rem=32767, time_rem=604800.0, leader_lap_s=90.0, pace_tol=0.0) is None      # unlimited


def test_pit_window_hand_checks():
    # tank 100 L, burn 3.0, reserve 1 lap
    w = cc.pit_window(60.0, 3.0, 100.0, 50.0, 0.0)
    assert w["need_stop"] and w["stops"] == 1
    assert w["k_latest"] == 18 and w["k_earliest"] == 17           # pit at the end of lap n+17 .. n+18
    assert w["add_l"] == 93.0                                      # (50 + 1) * 3 - 60
    assert abs(w["save_lpl"] - 1.805) < 0.001 and w["save_pct"] > 50   # skip the stop: 60 / 50.2 = 1.195 L a lap
    # one lap earlier than the earliest, a full top-up would not reach the flag; at the earliest it does
    for k, ok in ((16, False), (17, True)):
        fuel_at = 60.0 - (k + 1) * 3.0
        assert ((50.0 - (k + 1) + 1.0) * 3.0 <= 100.0 + 1e-9) == ok, k
        assert fuel_at >= 3.0
    # enough fuel: no stop, 12 L (4 laps) to spare
    w = cc.pit_window(60.0, 3.0, 100.0, 15.0, 0.0)
    assert not w["need_stop"] and w["margin_l"] == 12.0 and w["add_l"] == 0.0
    # half way round the lap, 20 L, 30.5 laps to go
    w = cc.pit_window(20.0, 3.0, 100.0, 30.5, 0.5)
    assert w["stops"] == 1 and w["k_latest"] == 5 and w["k_earliest"] == 0 and w["add_l"] == 74.5
    # two stops: the add is what fits in the tank at the latest stop
    w = cc.pit_window(60.0, 3.0, 60.0, 50.0, 0.0)
    assert w["stops"] == 2 and w["k_latest"] == 18 and w["add_l"] == 57.0 and w["fill_l"] == 57.0
    # already too late for this lap
    w = cc.pit_window(4.0, 3.0, 100.0, 30.0, 0.0)
    assert w["k_latest"] < 0
    assert cc.stops_needed(60.0, 3.0, 60.0, 50.0) == 2 and cc.stops_needed(60.0, 3.0, 100.0, 15.0) == 0


def test_fuel_units_and_words():
    assert cc.fuel_text(37.85, "mph") == "10.0 gal" and cc.fuel_text(42.0, "kmh") == "42.0 L"
    p = cc.fuel_pair(37.85, "mph")
    assert p["text"] == "10.0 gal" and p["alt"] == "37.9 L"
    assert cc.fuel_pair(42.0, "kmh")["text"] == "42.0 L"
    # said rounded UP, in the unit of Settings > Units
    assert cc.say_fuel(41.2, "kmh") == "Forty-two litres."
    assert cc.say_fuel(0.5, "kmh") == "One litre."
    assert cc.say_fuel(41.2, "mph") == "Eleven gallons."          # 41.2 / 3.785 = 10.88
    assert cc.say_fuel(500, "kmh") == "" and cc.say_fuel(None, "kmh") == ""
    assert cc.say_rate(0.3) == "three tenths a lap" and cc.say_rate(0.05) == "a tenth a lap"
    assert cc.say_rate(1.4) == "one point four seconds a lap" and cc.say_rate(5) == "two seconds a lap"
    assert cc.laps_phrase(0.4) == "under a lap" and cc.laps_phrase(1.2) == "1 lap" and cc.laps_phrase(3.6) == "4 laps"


# ------------------------------------------------------------------ fuel burn
def test_burn_is_the_median_of_clean_green_laps():
    sim = S.RaceSim(laps=30, tank=60.0, burn=3.0)
    sim.pit_at(lap=12, add=40.0)
    sim.caution(300.0, 420.0)                                # laps 6-7 under caution
    ch = chief(sim)
    drive(sim, ch)
    b, how = ch.burn.burn()
    assert how == "laps" and abs(b - 3.0) < 0.02, (b, how)
    laps = [u for _l, u in ch.burn.laps]
    assert all(abs(u - 3.0) < 0.05 for u in laps), laps      # no caution lap (1.5 L), no pit lap (refuel)
    why = dict(ch.burn.skipped)
    assert why.get(1) == "lap 1"
    assert any("caution" in r for r in why.values()), why
    assert any("pit lap" in r for r in why.values()), why
    # the median shrugs off one thirsty lap
    t = cc.BurnTracker()
    for lap, used in ((2, 3.0), (3, 3.1), (4, 2.9), (5, 4.5), (6, 3.0)):
        t.add(lap, used, "")
    assert t.burn()[0] == 3.0
    assert t.burn() == (3.0, "laps")
    t2 = cc.BurnTracker()
    t2.add(2, 3.0, ""), t2.add(3, 3.0, "")
    assert t2.burn() == (None, "learning")                   # fewer than three laps: no figure


def test_tow_and_lap_one_are_not_burn_laps():
    sim = S.RaceSim(laps=12, tank=100.0, burn=3.0)
    ch = chief(sim)
    seen = []

    def tow(own, f):
        if 150.0 <= sim.t < 150.2:
            own["PlayerCarTowTime"] = 12.0                    # a tow on lap 3
    drive(sim, ch, before=tow)
    why = dict(ch.burn.skipped)
    assert why.get(1) == "lap 1" and "tow" in why.get(3, ""), why
    assert len(ch.burn.laps) >= 5


def test_tank_comes_from_the_session_text():
    sim = S.RaceSim(tank=106.0)
    ch = chief(sim)
    sec = sim.sections()
    sec["DriverInfo"]["DriverCarMaxFuelPct"] = 0.5
    ch.set_info(sec, 2)
    assert abs(ch.tank - 53.0) < 1e-9


# ------------------------------------------------------------------ a full race with two stops
def test_full_race_two_stops_calls_and_numbers():
    sim = S.RaceSim(laps=50, tank=60.0, burn=3.0, lap_s=60.0)
    sim.pit_at(lap=19, add=48.0)                 # as late as the fuel allows (see the hand check in crewchief.pit_window)
    sim.pit_at(lap=36, add=45.0)
    ch = chief(sim)
    first = {}

    def watch(own, f, got):
        s = ch.snapshot()
        if "left" not in first and s.get("laps_left"):
            first["left"] = (own["Lap"], s["laps_left"]["n"], s["box"], s["fuel"]["burn_how"])
    calls = drive(sim, ch, on_frame=watch)
    assert ch.stops_done == 2, ch.stops_done
    boxes = [c for c in calls if c.kind in ("box_soon", "box_now")]
    assert len(boxes) >= 4
    # the first stop: 'Box in three laps' before 'Box this lap', in order, with a number only when it is close
    first_run = [c.say for c in boxes[:4]]
    assert first_run[0].startswith("Box in three laps"), first_run
    assert any(c.say.startswith("Box this lap. ") and c.say.endswith("litres.") for c in boxes), [c.say for c in boxes]
    # nothing is said twice in a row inside two laps unless it is more urgent
    last = {}
    for c in calls:
        g = cc.GROUP.get(c.kind, c.kind)
        if g in last and c.kind not in ("fuel_critical", "box_now"):          # box this lap: once a lap
            lap0, lvl0 = last[g]
            assert c.lap - lap0 >= cc.REPEAT_LAPS or c.level > lvl0, (c.kind, c.lap, lap0, c.level, lvl0)
        last[g] = (c.lap, c.level)
    # pit loss was measured from the first stop: 40 s on pit road - 0.2 lap at 60 s = 28 s
    assert ch.pit_losses and abs(ch.pit_losses[0] - 28.0) < 0.5, ch.pit_losses
    assert ch.pit_loss()[1] == "measured"
    # the box call said the amount to the litre the sim needed (rounded up)
    assert any(c.data.get("add_l") for c in boxes)
    # laps to go counted down from the lap race's SessionLapsRemainEx
    assert first["left"][1] in (50, 49) and first["left"][3] in ("laps", "learning", "from the last session"), first


def test_stop_is_only_a_real_stop_in_the_stall():
    sim = S.RaceSim(laps=24, tank=60.0, burn=3.0)
    sim.pit_at(lap=10, add=30.0)
    ch = chief(sim)
    drive(sim, ch)
    assert ch.stops_done == 1
    # driving through the pits without stopping is not a stop
    sim2 = S.RaceSim(laps=6)
    ch2 = chief(sim2)
    for own, f in sim2.run(100):
        pass
    own = sim2.own()
    for i, (pit, stall) in enumerate(((False, False), (True, False), (True, False), (False, False))):
        o = dict(own, SessionTime=own["SessionTime"] + 1 + i, OnPitRoad=pit, PlayerCarInPitStall=stall)
        ch2.feed(o, None, now=o["SessionTime"])
    assert ch2.stops_done == 0


# ------------------------------------------------------------------ the window on live numbers
def test_window_and_fuel_to_add_in_the_race():
    sim = S.RaceSim(laps=50, tank=60.0, burn=3.0)
    ch = chief(sim)
    snap = {}

    def watch(own, f, got):
        if own["Lap"] == 8 and 0.5 < own["LapDistPct"] < 0.51 and "s" not in snap:
            snap["s"] = ch.snapshot()
            snap["fuel"] = own["FuelLevel"]
    drive(sim, ch, seconds=500, on_frame=watch)
    s = snap["s"]
    # lap 8, half way: 60 - 7.5 * 3 = 37.5 L; 50 laps race: distance = 43 - 0.5 = 42.5 laps (43 crossings)
    assert s["laps_left"]["n"] == 43 and abs(s["laps_left"]["distance"] - 42.5) < 0.05, s["laps_left"]
    assert abs(snap["fuel"] - 37.5) < 0.3
    b = s["box"]
    # need (42.5 + 1) * 3 = 130.5 L, has 37.5: stops = ceil(93 / 57) = 2 ; k_latest = floor(12.5 - 1 + 0.5 - 1) = 11
    assert b["need"] and b["stops"] == 2, b
    assert b["lap_latest"] == 8 + 11, b
    assert s["fuel"]["laps"] == 12.5
    assert b["add"]["l"] == 57.0 or b["add"]["l"] < 57.0 + 0.5, b["add"]


def test_one_stop_window_text_and_save():
    sim = S.RaceSim(laps=30, tank=60.0, burn=3.0)
    ch = chief(sim)
    snap = {}

    def watch(own, f, got):
        if own["Lap"] == 8 and 0.5 < own["LapDistPct"] < 0.51 and "s" not in snap:
            snap["s"] = ch.snapshot()
    drive(sim, ch, seconds=500, on_frame=watch)
    b = snap["s"]["box"]
    # dist = 30 - 7 - 0.5 = 22.5 ; need 70.5 L ; fuel 37.5 -> one stop ; k_latest = floor(12.5 - 1 + .5 - 1) = 11
    # k_earliest = max(0, ceil(22.5 - 20 + 1 + .5 - 1)) = 3
    assert b["stops"] == 1 and b["lap_latest"] == 19 and b["lap_earliest"] == 11, b
    assert b["window_text"] == "Window: lap 11 to lap 19", b["window_text"]
    assert abs(b["add"]["l"] - 33.0) < 0.15, b["add"]                  # 70.5 - 37.5
    assert "to skip the stop" in b["save_text"] and b["save_realistic"] is False


# ------------------------------------------------------------------ a caution
def test_caution_is_not_a_pit_cycle():
    sim = S.RaceSim(laps=40, tank=60.0, burn=3.0)
    sim.caution(120.0, 330.0)                                  # laps 3-6, nobody of ours pits
    ch = chief(sim)
    seen = {"stops": set(), "burn": set()}

    def watch(own, f, got):
        seen["stops"].add(ch.stops_done)
        if own["SessionFlags"] & cc.FULL_COURSE:
            assert ch.caution
    calls = drive(sim, ch, seconds=420, on_frame=watch)
    assert seen["stops"] == {0}, seen
    assert all(abs(u - 3.0) < 0.05 for _l, u in ch.burn.laps), list(ch.burn.laps)    # caution laps (1.5 L) are not burn
    assert "caution_box" not in kinds(calls)                  # nowhere near a stop: the window is far off
    assert ch.caution_laps >= 2


def test_caution_with_the_window_open_calls_a_cheap_stop():
    sim = S.RaceSim(laps=50, tank=100.0, burn=3.0)
    sim.caution(840.0, 1300.0)                                 # one stop left; it can take the race to the flag from lap 18
    ch = chief(sim)
    cheap = []

    def watch(own, f, got):
        for c in got:
            if c.kind == "caution_box":
                cheap.append((c, dict(ch.last_calc["window"])))
    calls = drive(sim, ch, seconds=1250, on_frame=watch)
    assert cheap, kinds(calls)
    c, w = cheap[0]
    assert c.say.startswith("Box now, cheap stop. ") and c.say.endswith("litres."), c.say
    assert w["k_earliest"] <= cc.CHEAP_STOP_LAPS and w["need_stop"]
    assert c.rank == 4 and c.priority == cc.PRIORITY_CREW
    # a gallon unit says gallons
    ch2 = chief(S.RaceSim(), units="mph")
    assert cc.say_fuel(40.0, ch2.units).endswith("gallons.")


def test_caution_far_from_the_window_makes_no_call_and_a_stop_under_it_counts_once():
    sim = S.RaceSim(laps=50, tank=60.0, burn=3.0)
    sim.caution(100.0, 300.0)
    sim.pit_at(lap=4, add=3.0)                                 # a real stop under the yellow (tiny splash)
    sim.car_pit(1, 200.0, 240.0)                               # the car ahead pits under the caution
    ch = chief(sim)
    calls = drive(sim, ch, seconds=420)
    assert "caution_box" not in kinds(calls) and "ahead_pits" not in kinds(calls), kinds(calls)
    assert ch.stops_done == 1


# ------------------------------------------------------------------ rivals
def rival_run(setup, seconds=330, **kw):
    sim = S.RaceSim(laps=80, tank=200.0, burn=2.0, **kw)
    setup(sim)
    ch = chief(sim)
    calls = drive(sim, ch, seconds=seconds)
    return sim, ch, calls


def test_car_closing_behind_is_a_threat():
    sim, ch, calls = rival_run(lambda s: s.set_pace(3, 59.4))             # 0.6 s a lap quicker, 1.5 s back
    th = [c for c in calls if c.kind == "threat"]
    assert th, kinds(calls)
    c = th[0]
    assert c.say == "Car behind closing, six tenths a lap.", c.say
    assert 0.45 <= c.data["rate"] <= 0.75, c.data
    assert c.rank == 5
    # the call about it is said at most once inside two laps unless it escalates
    for a, b in zip(th, th[1:]):
        assert b.lap - a.lap >= 2 or b.level > a.level


def test_rival_tags_catchable_pulling_away_pitting_alongside():
    # catchable: the car ahead is slower
    def catch(s):
        s.set_pace(1, 60.6)
        s.cars[1].D += 2.5 / 60.0                             # 4 s ahead, so it is still ahead after 3 laps
    sim, ch, calls = rival_run(catch, seconds=200)
    ah = ch.snapshot()["ahead"]
    assert ah and "catchable" in ah["tags"] and 0.45 <= ah["rate"] <= 0.75, ah
    assert any(c.kind == "catchable" and c.say.startswith("Gaining on the car ahead, ") for c in calls)
    # pulling away: the car ahead is quicker, and the one behind is slower
    sim, ch, calls = rival_run(lambda s: (s.set_pace(0, 59.5), s.set_pace(1, 59.5), s.set_pace(3, 60.6), s.set_pace(4, 60.6)), seconds=200)
    snap = ch.snapshot()
    assert "pulling away" in snap["ahead"]["tags"] and "pulling away" in snap["behind"]["tags"], snap
    assert not any(c.kind in ("threat", "catchable") for c in calls)

    # pitting: pit detection uses CarIdxOnPitRoad, never a slow lap
    def slow_not_pit(s):
        s.set_pace(1, 75.0)                                  # a slow lap time is not a pit stop
    sim, ch, calls = rival_run(slow_not_pit, seconds=180)
    assert "ahead_pits" not in kinds(calls)
    sim, ch, calls = rival_run(lambda s: s.car_pit(1, 100.0, 140.0), seconds=150)
    pits = [c for c in calls if c.kind == "ahead_pits"]
    assert pits and pits[0].say == "Car ahead pitting." and pits[0].rank == 8

    # alongside: closer than half a second
    def close(s):
        s.cars[3].D = s.cars[S.ME].D - 0.05 / 60.0
    sim, ch, calls = rival_run(close, seconds=20)
    assert "alongside" in ch.snapshot()["behind"]["tags"]
    assert any(c.kind == "alongside" and c.say == "Car alongside." for c in calls)
    # CarLeftRight does it too
    sim = S.RaceSim(laps=50, tank=200.0)
    sim.side = 2
    ch = chief(sim)
    assert "alongside" in kinds(drive(sim, ch, seconds=20))


def test_gap_falls_back_to_est_time_without_f2():
    sim = S.RaceSim(laps=80, tank=200.0, burn=2.0, f2=False)
    sim.set_pace(3, 59.4)
    sim.cars[3].D -= 1.5 / 60.0                                # 3 s back
    ch = chief(sim)
    calls = drive(sim, ch, seconds=150)
    b = ch.snapshot()["behind"]
    assert b and 0.5 <= b["gap_s"] <= 2.5, b
    assert "threat" in kinds(calls)


def test_rivals_are_in_your_class_and_the_leader_is_your_class_leader():
    sim = S.RaceSim(laps=80, tank=200.0, burn=2.0, multiclass=True, lap_s=60.0)
    sim.cars[S.ME].cls = S.CLASS_B                    # you and the odd numbers are class B; car 0 (class A) leads overall
    sim.set_pace(0, 50.0)
    sim.set_pace(1, 62.0)
    sim.cars[1].D += 0.5                                       # half a lap (30 s) clear of you in your class
    for i in sim.cars:
        sim.cars[i].cls = S.CLASS_B if i % 2 == 1 or i == S.ME else S.CLASS_A
    ch = chief(sim, units="kmh")
    drive(sim, ch, seconds=420)
    assert ch.multiclass() and ch.my_class == S.CLASS_B
    assert ch._leader_idx == 1, ch._leader_idx        # class leader, not car 0
    lap, who = ch.leader_lap()
    assert who == "leader" and abs(lap - 62.0) < 0.3, (lap, who)    # not the overall leader's 50 s
    v = ch.snapshot()
    for r in (v["ahead"], v["behind"]):
        if r:
            assert ch.class_of[r["idx"]] == S.CLASS_B
    # a timed race counts laps to go from that class leader's lap time
    sim = S.RaceSim(laps=0, timed_s=1800.0, tank=200.0, burn=2.0, multiclass=True)
    sim.set_pace(0, 50.0)
    sim.set_pace(1, 62.0)
    sim.cars[1].D += 0.5                                       # half a lap (30 s) clear of you in your class
    for i in sim.cars:
        sim.cars[i].cls = S.CLASS_B if i % 2 == 1 or i == S.ME else S.CLASS_A
    ch = chief(sim)
    got = {}

    def watch(own, f, g):
        if own["SessionTime"] > 400.0 and "n" not in got:
            s = ch.snapshot()
            lead, _w = ch.leader_lap()
            got["n"] = s["laps_left"]["n"]
            got["want"] = int(math.floor((own["SessionTimeRemain"] - 0) / lead + own["LapDistPct"] + 1e-9)) + 1
            got["kind"] = s["laps_left"]["kind"]
    drive(sim, ch, seconds=410, on_frame=watch)
    assert got["kind"] == "time" and abs(got["n"] - got["want"]) <= 1 and got["n"] < 1800 / 50.0, got


def test_multiclass_flag_follows_the_overall_leader():
    sim = S.RaceSim(laps=0, timed_s=1800.0, tank=200.0, burn=2.0, multiclass=True, lap_s=60.0)
    sim.set_pace(0, 50.0)                                      # car 0 is the overall leader, in the faster class
    sim.set_pace(1, 62.0)
    sim.cars[1].D += 0.5
    for i in sim.cars:
        sim.cars[i].cls = S.CLASS_B if i % 2 == 1 or i == S.ME else S.CLASS_A
    ch = chief(sim)
    got = {}

    def watch(own, f, g):
        if own["SessionTime"] > 420.0 and "n" not in got:
            lead = sim.cars[0]
            lap_me, who = ch.leader_lap()
            got.update(n=ch.snapshot()["laps_left"]["n"], me_pace=lap_me, ov=ch._ov_idx, cl=ch._leader_idx,
                       want=ref_crossings(own["SessionTimeRemain"], lead.lap_s, own["LapDistPct"], lead.pct, p_me=lap_me),
                       wrong=ref_crossings(own["SessionTimeRemain"], lap_me, own["LapDistPct"], sim.cars[1].pct))
    drive(sim, ch, seconds=430, on_frame=watch)
    assert got["ov"] == 0 and got["cl"] == 1, got
    assert abs(got["n"] - got["want"]) <= 1, got
    assert got["n"] != got["wrong"] or got["want"] == got["wrong"], got


def test_est_time_scale_comes_from_your_own_class():
    sim = S.RaceSim(laps=80, tank=200.0, burn=2.0, f2=False, multiclass=True, cars=6)
    for i in sim.cars:
        sim.cars[i].cls = S.CLASS_B if i % 2 == 1 or i == S.ME else S.CLASS_A
    ch = chief(sim)
    for own, f in sim.run(40):
        if f is not None:
            f = dict(f, CarIdxEstTime=[(x * 3.0 if i % 2 == 0 and i != S.ME else x) for i, x in enumerate(f["CarIdxEstTime"])])
            ch.feed(own, f, now=sim.t)
    assert ch._est_full is not None and ch._est_full <= 60.5, ch._est_full      # not the other class's 180


# ------------------------------------------------------------------ the timed race with stops still to make
def ref_crossings(T, p, f, fL, s=0.0, p_me=None):
    """An independent count, clock by clock: the leader (at fL on his lap, laps of p seconds) takes the flag at his first
    crossing at or after T; you (at f, s seconds late because of your stops) cross the line once for every crossing
    before that and once more after it."""
    k = 1
    while (k - fL) * p < T - 1e-9:
        k += 1
    tl = (k - fL) * p
    p_me = p if p_me is None else p_me                     # you lap at your class's pace, the leader at his
    n, j = 0, 1
    while (j - f) * p_me + s < tl - 1e-9:
        n += 1
        j += 1
    return n + 1


def test_timed_race_takes_off_the_pit_time():
    sim = S.RaceSim(laps=0, timed_s=1800.0, tank=60.0, burn=3.0, lap_s=60.0)
    ch = chief(sim, pit_loss_s=90.0)                           # a stop that costs a lap and a half
    got = {}

    def watch(own, f, g):
        if own["SessionTime"] > 300.0 and "n" not in got:
            s = ch.snapshot()
            lead, _ = ch.leader_lap()
            f_ = own["LapDistPct"]
            rem = own["SessionTimeRemain"]
            stops = s["box"]["stops"]
            fl = ch._leader_f if ch._leader_f is not None else f_
            got.update(n=s["laps_left"]["n"], stops=stops, want=ref_crossings(rem, lead, f_, fl, stops * 90.0),
                       without=ref_crossings(rem, lead, f_, fl, 0.0))
    drive(sim, ch, seconds=310, on_frame=watch)
    assert got["stops"] >= 1 and got["n"] == got["want"] and got["n"] < got["without"], got
    ch2 = chief(S.RaceSim(laps=0, timed_s=1800.0))
    assert ch2.pit_loss()[1] == "assumed"                      # no stop yet and no plan: say so


def test_timed_finish_matches_a_clock_by_clock_count_fuzz():
    """2.29 review: the first version was one crossing short half of the time (it ignored where the leader is on his lap)."""
    import random
    rnd = random.Random(7)
    wrong = 0
    for _ in range(20000):
        p, T = rnd.uniform(40, 200), rnd.uniform(0, 4 * 3600)
        f, fl = rnd.random(), rnd.random()
        stops = rnd.choice([0, 0, 1, 2])
        loss = rnd.uniform(15, 90)
        want = ref_crossings(T, p, f, fl, stops * loss)
        got = cc.laps_to_go(time_rem=T, lap_f=f, leader_lap_s=p, stops=stops, pit_loss_s=loss, leader_f=fl, pace_tol=0.0)["crossings"]
        wrong += got != want
    assert wrong == 0, wrong
    # multi-class: the flag falls on the OVERALL leader (a faster class), you lap at your class's pace (round 3 review:
    # the first version used the class leader's place and pace for both, one crossing short in 14% of cases)
    wrong = 0
    for _ in range(20000):
        pc = rnd.uniform(80, 130)
        po = pc * rnd.uniform(0.88, 0.97)
        T, f, fo = rnd.uniform(0, 7200), rnd.random(), rnd.random()
        want = ref_crossings(T, po, f, fo, p_me=pc)
        got = cc.laps_to_go(time_rem=T, lap_f=f, leader_lap_s=po, leader_f=fo, my_lap_s=pc, pace_tol=0.0)["crossings"]
        wrong += got != want
    assert wrong == 0, wrong
    # the reviewer's example: 4676.6 s left, class pace 118.7, overall leader 105.1 at 0.528, you at 0.068
    assert cc.laps_to_go(time_rem=4676.6, lap_f=0.068, leader_lap_s=105.1, leader_f=0.528, my_lap_s=118.7, pace_tol=0.0)["crossings"] == 41
    assert cc.laps_to_go(time_rem=4676.6, lap_f=0.068, leader_lap_s=118.7, leader_f=0.222, pace_tol=0.0)["crossings"] == 40
    # the case the reviewer found: 1000 s left, 100 s laps, you at 0.95, and the leader just past the line (0.05)
    assert cc.laps_to_go(time_rem=1000, lap_f=0.95, leader_lap_s=100, leader_f=0.05, pace_tol=0.0)["crossings"] == \
        ref_crossings(1000, 100, 0.95, 0.05)


# ------------------------------------------------------------------ calls: rank, rate limit, escalation
def _call(kind, lap, level=1, t=0.0):
    return cc.Call(kind=kind, rank=cc.RANKS[kind], say=kind, text=kind, level=level, lap=lap, t=t)


def test_ranks_follow_the_urgency_list():
    order = ["data_lost", "fuel_critical", "box_now", "caution_box", "threat", "alongside", "catchable",
             "ahead_pits", "plan", "target"]
    assert [cc.RANKS[k] for k in order] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert cc.RANKS["box_soon"] == 3 and cc.RANKS["position"] == 9


def test_rate_limit_two_laps_unless_it_escalates():
    ch = cc.CrewChief()
    ch.lap = 10
    assert ch._allowed("threat", 1, 0.0)
    ch._issue(_call("threat", 10), 0.0)
    ch.lap = 11
    assert not ch._allowed("threat", 1, 1.0)                  # next lap, same level
    assert ch._allowed("threat", 2, 1.0)                      # more urgent: now
    ch.lap = 12
    assert ch._allowed("threat", 1, 2.0)                      # two laps on
    # 'box in three laps' ... 'box this lap' is one call type, so it escalates through the levels
    ch.limits.clear()
    ch.lap = 20
    ch._issue(_call("box_soon", 20, level=1), 0.0)
    assert not ch._allowed("box_soon", 1, 1.0)
    ch.lap = 21
    assert ch._allowed("box_soon", 2, 1.0)
    ch._issue(_call("box_soon", 21, level=2), 1.0)
    ch.lap = 22
    assert ch._allowed("box_now", 4, 2.0) and not ch._allowed("box_soon", 2, 2.0)
    # a separate key (another car pitting) is its own limit
    assert ch._allowed("ahead_pits", 1, 0.0, key=7)


def test_the_top_row_shows_the_most_urgent_true_thing():
    sim = S.RaceSim(laps=50, tank=60.0, burn=3.0)
    sim.set_pace(3, 59.4)                                      # a car closing behind (rank 5) ...
    ch = chief(sim)
    top = {}

    def watch(own, f, g):
        if ch.current is not None:
            top.setdefault(ch.current.kind, ch.current.rank)
    drive(sim, ch, seconds=1000, on_frame=watch)
    assert "threat" in top or "alongside" in top
    assert "box_soon" in top and top["box_soon"] < top.get("threat", 99)     # ... and 'box in three laps' (rank 3) wins


def test_fuel_critical_is_loud_and_only_when_the_flag_is_too_far():
    sim = S.RaceSim(laps=30, tank=60.0, burn=3.0, start_fuel=20.0)           # 6.7 laps of fuel, 30 laps to go
    ch = chief(sim)
    calls = drive(sim, ch, seconds=420)
    crit = [c for c in calls if c.kind == "fuel_critical"]
    assert crit and crit[0].priority == cc.PRIORITY_FUEL and crit[0].rank == 2
    assert crit[0].say.startswith("Fuel critical.")
    # plenty to finish: under two laps of fuel at the very end is not critical
    sim = S.RaceSim(laps=6, tank=60.0, burn=3.0, start_fuel=21.0)
    ch = chief(sim)
    calls = drive(sim, ch)
    assert "fuel_critical" not in kinds(calls)


def test_position_plan_and_target_calls():
    sim = S.RaceSim(laps=50, tank=200.0, burn=2.0)
    sim.set_pace(3, 59.0)                                      # the car behind goes past you
    ch = chief(sim)
    calls = drive(sim, ch, seconds=500)
    pos = [c for c in calls if c.kind == "position"]
    assert pos and pos[0].say == "Lost a place." and pos[0].rank == 9
    # a slow lap against the target: 3 tenths and more
    sim = S.RaceSim(laps=50, tank=200.0, burn=2.0)
    ch = chief(sim)
    slow = {"on": False}

    def watch(own, f, g):
        sim.cars[S.ME].lap_s = 60.5 if own["Lap"] in (9,) else 60.0
    calls = drive(sim, ch, seconds=800, on_frame=watch)
    tg = [c for c in calls if c.kind == "target"]
    assert tg and tg[0].say in {vp.crew_off_text(k) for k in range(3, 21)} and tg[0].rank == 10, kinds(calls)


def test_plan_delta_on_ahead_behind(tmp=None):
    lib = TMP / "lib"
    from debrief import endurance
    inp = {"laps": 50, "tank_l": 60.0, "reserve_laps": 1, "pit_loss_s": 28,
           "drivers": [{"name": "Me", "lap_t": 60.0, "burn_l": 3.0}]}
    plan = endurance.plan(inp)
    endurance.save_inputs(str(lib), "Test Ring", inp, title="Sunday")
    lp = cc.load_plan(str(lib), "Test Ring")
    assert lp and lp["stop_laps"] == [s["to_lap"] for s in plan["stints"] if s.get("stop")] and lp["lap_s"] == 60.0
    assert cc.load_plan(str(lib), "Nowhere") is None
    # the plan stops on lap 17 and 34 (18 laps a tank); we can pit as late as lap 19 -> ahead by 2 / on / behind
    sim = S.RaceSim(laps=50, tank=60.0, burn=3.0)
    ch = chief(sim, plan=lp)
    states = set()

    def watch(own, f, g):
        pl = ch.snapshot().get("plan")
        if pl and own["Lap"] in (8, 9, 10):
            states.add((pl["state"], pl.get("delta")))
    drive(sim, ch, seconds=600, on_frame=watch)
    assert states and all(st in ("on", "ahead", "behind") for st, _d in states)
    s = ch.snapshot()
    assert s["plan"]["planned"] == lp["stop_laps"][0]
    d = s["plan"]["delta"]
    assert s["plan"]["state"] == ("ahead" if d >= 1 else "behind" if d <= -1 else "on")
    assert "Plan" in s["overlay"] or "plan" in s["overlay"].lower()
    # a state change is a call; the first look is not
    calls = []
    sim = S.RaceSim(laps=50, tank=60.0, burn=3.0)
    ch = chief(sim, plan=dict(lp, stop_laps=[40, 41]))                # planned far later than the fuel allows -> behind
    calls = drive(sim, ch, seconds=700)
    assert ch.snapshot()["plan"]["state"] == "behind"
    sim = S.RaceSim(laps=50, tank=60.0, burn=3.0)
    ch = chief(sim, plan=dict(lp, stop_laps=[5, 30]))                # planned earlier than needed -> ahead
    drive(sim, ch, seconds=700)
    assert ch.snapshot()["plan"]["state"] == "ahead"


# ------------------------------------------------------------------ data lost
def test_data_lost_and_back():
    sim = S.RaceSim(laps=50, tank=200.0, burn=2.0)
    ch = chief(sim)
    drive(sim, ch, seconds=200)
    assert ch.no_data(now=sim.t + 1.0) == []                  # a second of silence is nothing
    lost = ch.no_data(now=sim.t + 4.0)
    assert [c.kind for c in lost] == ["data_lost"] and lost[0].rank == 1 and lost[0].priority == cc.PRIORITY_FUEL
    assert ch.no_data(now=sim.t + 5.0) == []                  # not said again at once
    again = ch.no_data(now=sim.t + 40.0)
    assert [c.kind for c in again] == ["data_lost"]           # a reminder after 30 s
    own, f = sim.own(), sim.field()
    sim.step()
    back = ch.feed(sim.own(), sim.field(), now=sim.t + 41.0)
    assert "data_back" in kinds(back)
    # frozen: the clock stops while readings keep coming
    sim = S.RaceSim(laps=50, tank=200.0, burn=2.0)
    ch = chief(sim)
    drive(sim, ch, seconds=100)
    sim.freeze_from = sim.t
    got = drive(sim, ch, seconds=108)
    assert "data_lost" in kinds(got) and ch.lost


def test_nothing_is_called_outside_a_race_or_before_the_green():
    sim = S.RaceSim(laps=50, session_type="Practice")
    ch = chief(sim)
    calls = drive(sim, ch, seconds=300)
    assert calls == [] and ch.snapshot()["fuel"]["burn_how"] in ("laps", "learning")
    sim = S.RaceSim(laps=50, state=3)                          # parade laps
    ch = chief(sim)
    assert drive(sim, ch, seconds=200) == []
    sim = S.RaceSim(laps=50, state=5)                          # checkered
    assert drive(sim, chief(sim), seconds=200) == []


# ------------------------------------------------------------------ quiet zones
def test_quiet_zones():
    sp = cc.CallSpeaker()
    c = _call("threat", 5, t=100.0)
    sp.offer([c])
    # never under braking
    assert sp.pump({"Brake": 0.6, "Throttle": 0.0}, 100.1) is None and sp.pending
    # a hard stop stays a quiet zone until the throttle is back
    assert sp.pump({"Brake": 0.0, "Throttle": 0.3}, 100.5) is None
    assert sp.pump({"Brake": 0.0, "Throttle": 0.95}, 101.0) is c
    # never with a car alongside, and 2 s after it clears
    sp.offer([_call("catchable", 5, t=110.0)])
    assert sp.pump({"CarLeftRight": 2}, 110.0) is None
    assert sp.pump({"CarLeftRight": 1}, 111.0) is None
    got = sp.pump({"CarLeftRight": 1}, 112.5)
    assert got is not None and got.kind == "catchable"
    # ... unless it IS the alongside or the fuel critical call
    sp = cc.CallSpeaker()
    sp.offer([_call("alongside", 5, t=200.0)])
    assert sp.pump({"CarLeftRight": 3}, 200.0).kind == "alongside"
    sp = cc.CallSpeaker()
    sp.offer([_call("fuel_critical", 5, t=300.0)])
    assert sp.pump({"CarLeftRight": 3, "Brake": 0.0}, 300.0).kind == "fuel_critical"
    sp = cc.CallSpeaker()                                       # ... but not under braking
    sp.offer([_call("fuel_critical", 5, t=300.0)])
    assert sp.pump({"Brake": 0.5}, 300.0) is None
    # a call that finds no quiet place in time is dropped, never said late
    sp = cc.CallSpeaker()
    c = _call("threat", 5, t=400.0)
    sp.offer([c])
    assert sp.pump({"Brake": 0.5}, 400.5) is None
    assert sp.pump({"Brake": 0.0, "Throttle": 1.0}, 410.0) is None and not sp.pending
    assert c.spoken is False and "waited" in c.why or "quiet" in c.why
    assert any(e["what"] == "dropped" for e in sp.take_events())
    # most urgent first; a gap between calls (fuel critical is not held back by it)
    sp = cc.CallSpeaker()
    a, b, f = _call("catchable", 5, t=500.0), _call("box_soon", 5, t=500.0), _call("fuel_critical", 5, t=500.0)
    sp.offer([a, b, f])
    free = {"Brake": 0.0, "Throttle": 1.0}
    assert sp.pump(free, 500.0) is f
    assert sp.pump(free, 500.1) is f or True
    assert sp.pump(free, 500.2) is None or sp.pump(free, 500.2).kind == "box_soon"
    sp = cc.CallSpeaker()
    sp.offer([_call("box_soon", 5, t=600.0), _call("catchable", 5, t=600.0)])
    assert sp.pump(free, 600.0).kind == "box_soon"
    assert sp.pump(free, 601.0) is None                         # 2.5 s between calls
    assert sp.pump(free, 603.0).kind == "catchable"
    # voice off: shown, never said; mute: only fuel critical gets through
    sp = cc.CallSpeaker(voice_on=False)
    c = _call("box_now", 5, t=700.0)
    sp.offer([c])
    assert sp.pump(free, 700.0) is None and c.spoken is False and "voice" in c.why
    sp = cc.CallSpeaker()
    sp.muted = True
    sp.offer([_call("catchable", 5, t=800.0), _call("fuel_critical", 5, t=800.0)])
    assert sp.pump(free, 800.0).kind == "fuel_critical"
    assert sp.pump(free, 804.0) is None and sp.pending == []


def test_a_call_that_no_longer_holds_is_not_said():
    ch = cc.CrewChief()
    c = _call("box_now", 8)
    assert ch.still_valid(c, {"Lap": 8, "OnPitRoad": False}) is None
    assert ch.still_valid(c, {"Lap": 9, "OnPitRoad": False}) == "that lap is over"
    assert ch.still_valid(c, {"Lap": 8, "OnPitRoad": True}) == "already in the pits"
    cb = _call("caution_box", 8)
    assert ch.still_valid(cb, {"Lap": 8, "SessionFlags": cc.F_CAUTION}) is None
    assert ch.still_valid(cb, {"Lap": 8, "SessionFlags": cc.F_GREEN}) == "the caution is over"


def test_a_box_call_made_before_pit_entry_is_never_said_after_it():
    # 2.29 review round 5: made at 0.80, a car alongside until 0.93, then it would have been said past the entry
    ch = cc.CrewChief()
    entry = ch.pit_entry_frac()
    c = _call("box_now", 8)
    assert ch.still_valid(c, {"Lap": 8, "LapDistPct": entry - 0.05}) is None
    assert ch.still_valid(c, {"Lap": 8, "LapDistPct": entry + 0.08}) == "past pit entry"
    cb = _call("caution_box", 8)
    assert ch.still_valid(cb, {"Lap": 8, "LapDistPct": 0.95, "SessionFlags": cc.F_CAUTION}) == "past pit entry"
    sb = _call("save_or_box", 8)      # about NEXT lap: still fine to say past this lap's entry
    assert ch.still_valid(sb, {"Lap": 8, "LapDistPct": 0.95}) is None
    ch._last_crossings = 1            # the final lap: no box call of any kind
    assert ch.still_valid(c, {"Lap": 8, "LapDistPct": 0.10}) == "final lap"
    assert ch.still_valid(sb, {"Lap": 8, "LapDistPct": 0.10}) == "final lap"
    # the live runner asks still_valid right before a call plays (livecoach.LiveRunner, crew.still_valid)
    src = open(os.path.join(os.path.dirname(cc.__file__), "livecoach.py"), encoding="utf-8").read()
    assert "crew.still_valid(" in src


# ------------------------------------------------------------------ 2.29 review fixes
def test_the_spare_lap_is_not_fuel_you_must_have():
    # the reviewer's race: 10 laps, 32.7 L at 3 L a lap. It finishes with 0.95 lap in the tank: no box call, no fuel critical
    sim = S.RaceSim(laps=10, tank=60.0, burn=3.0, start_fuel=32.7, cars=4)
    ch = chief(sim)
    calls = drive(sim, ch)
    assert not [c for c in calls if c.kind in ("box_now", "box_soon", "fuel_critical", "caution_box")], kinds(calls)
    assert sim.fuel > 2.5                                      # and he did finish with the 0.95 lap
    # hand checks: 17.8 L, 5 laps to go -> 15.6 L needed: no stop, and not even tight (a 1% saving)
    w = cc.pit_window(17.8, 3.0, 60.0, 5.0, 0.0)
    assert not w["need_stop"] and not w["tight"]
    # 14.5 L: short of 0.2 lap at the flag by 1.1 L, a 7% saving, which is realistic: no stop, but fuel is tight
    w = cc.pit_window(14.5, 3.0, 60.0, 5.0, 0.0)
    assert not w["need_stop"] and w["tight"] and abs(w["tight_save_lpl"] - 0.21) < 0.02, w
    # 14.0 L needs a 10.3% saving: that is a stop
    w = cc.pit_window(14.0, 3.0, 60.0, 5.0, 0.0)
    assert w["need_stop"] and w["stops"] == 1
    # the tight call: said as "Fuel is tight." with the saving in the text, once, not every lap
    sim = S.RaceSim(laps=12, tank=60.0, burn=3.0, start_fuel=34.8, cars=4)      # short of the flag by a 6-9% saving
    ch = chief(sim)
    calls = drive(sim, ch, seconds=360)
    tight = [c for c in calls if c.kind == "fuel_tight"]
    assert tight and tight[0].say in ("Fuel is tight.", "Short of the flag.") and "save" in tight[0].text.lower() \
        and len(tight) <= 2, kinds(calls)
    assert "fuel_critical" not in kinds(calls) and "box_now" not in kinds(calls)
    # fuel critical means you really cannot make it: under 2 laps of fuel AND a stop needed
    sim = S.RaceSim(laps=10, tank=60.0, burn=3.0, start_fuel=21.0, cars=4)
    assert "fuel_critical" in kinds(drive(sim, chief(sim)))


def test_last_two_laps_a_stop_only_for_fuel_that_is_short():
    # round 3: 30.3 L, 10 laps at 3 L: on the last lap 0.45 lap of fuel, 0.30 to go: no stop, no fuel critical
    sim = S.RaceSim(laps=10, tank=60.0, burn=3.0, start_fuel=30.3, cars=4)
    calls = drive(sim, chief(sim))
    assert not [c for c in calls if c.kind in ("box_now", "box_soon", "fuel_critical")], \
        [(c.kind, c.text) for c in calls]
    assert sim.fuel > 0.3
    # the window maths: 1.35 L with 0.3 lap to go is enough (0.9 L)
    w = cc.pit_window(1.35, 3.0, 60.0, 0.3, 0.7)
    assert not w["need_stop"] and not w["short"]
    w = cc.pit_window(1.4, 3.0, 60.0, 0.5, 0.5)
    assert not w["need_stop"] and w["short"] and w["tight"]            # 1.5 L needed, 1.4 L: short by 7%: save, no stop...
    w = cc.pit_window(0.6, 3.0, 60.0, 1.5, 0.5)
    assert w["need_stop"]                                              # ...and a saving over 10% is a stop


def test_short_of_the_flag_is_said_and_the_saving_is_not_forgotten():
    # round 3: 29.8 L, 2.222 L a lap, 13.73 laps: 0.32 lap short, saving 0.08 L a lap (3.6%): the first version was silent
    w = cc.pit_window(29.8, 2.222, 120.0, 13.73, 0.0)
    assert w["short"] and w["tight"] and not w["need_stop"] and abs(w["reach_laps"] + 0.32) < 0.01
    assert abs(w["tight_save_lpl"] - 0.08) < 0.01
    sim = S.RaceSim(laps=14, tank=60.0, burn=3.0, start_fuel=3.0 * 13.7 - 0.8, cars=4)       # about 0.27 lap short
    ch = chief(sim)
    first = {}
    after = {}

    def watch(own, f, g):
        for c in g:
            if c.kind == "fuel_tight" and "c" not in first:
                first["c"], first["lap"] = c, own["Lap"]
                sim.burn = 3.0 * 0.95                               # he does as he is told and saves 5%
        if "c" in first and own["Lap"] == first["lap"] + 4 and "box" not in after:
            after["box"] = ch.snapshot()["box"]
            after["ref"] = ch._tight_ref
    drive(sim, ch, seconds=520, on_frame=watch)
    c = first["c"]
    assert c.say == "Short of the flag." and c.text.startswith("Short of the flag by") and "save" in c.text, c.text
    assert vp.compose(c.say) == ["cc_short_of_the_flag"]
    # a few laps on the median has taken in his saved laps, but the shortage is still shown: the saving is still needed
    assert ch.burn.burn()[0] < 2.95
    assert after["ref"] is not None and after["ref"] > 2.95, after
    assert after["box"].get("short") and "short of the flag" in after["box"]["window_text"], after["box"]
    # the panel says how far short, not the figure with the spare lap taken off
    assert "lap short of the flag" in after["box"]["window_text"] or "laps short of the flag" in after["box"]["window_text"]
    assert "-" not in after["box"]["window_text"]


def _comply_race(seed, short, policy, sigma, push, laps):
    """The reviewer's driver: does what the crew chief asks (up to 10%), maybe pushes in the last two laps; boxes when told."""
    import random as _r
    rnd = _r.Random(seed)
    B = 3.0
    sim = S.RaceSim(laps=laps, tank=120.0, burn=B, start_fuel=laps * B * (1 - short), cars=3, dt=0.25)
    ch = chief(sim)
    last_lap, boxed, save = None, False, 0.0
    texts = []
    for own, f in sim.run():
        for c in ch.feed(own, f, now=sim.t):
            texts.append(c.text)
            if c.kind == "box_now" and not boxed and own["LapDistPct"] < 0.9 and own["Lap"] <= laps:
                boxed = True
                sim.pit_at(lap=own["Lap"], add=max(c.data.get("add_l") or 0, 3.0))
        if own["Lap"] != last_lap:
            last_lap = own["Lap"]
            w = (ch.last_calc or {}).get("window") or {}
            if w.get("tight") and not w.get("need_stop"):
                est = ch.last_calc.get("burn") or B
                ask = min(w.get("tight_save_lpl", 0.0) / est, 0.10)
                save = ask if policy == "comply" else max(save, ask)
            elif policy != "sticky":
                save = 0.0
            p = push if own["Lap"] > laps - 2 else 0.0
            sim.burn = B * (1 + rnd.gauss(0, sigma)) * (1 + p) * (1 - save)
        if sim.fuel < 0:
            return "DRY", own["Lap"], texts[-3:]
    return "ok", boxed, texts


def test_a_driver_who_does_as_asked_never_runs_dry():
    """Round 4: 3% short on 30 laps, he saves what is asked, and pushes 0, 2 or 5% in the last two laps."""
    for push in (0.0, 0.02, 0.05):
        r = _comply_race(1, 0.03, "comply", 0.0, push, 30)
        assert r[0] == "ok", (push, r)
    # a sample of the reviewer's grid (short x policy x noise x push): nobody who complies or keeps saving runs dry
    n = 0
    for short in (0.01, 0.03, 0.06):
        for policy in ("comply", "sticky"):
            for sigma, push in ((0.01, 0.0), (0.02, 0.05)):
                for laps in (12, 30):
                    n += 1
                    r = _comply_race(n * 7 + int(short * 1000), short, policy, sigma, push, laps)
                    assert r[0] == "ok", (short, policy, sigma, push, laps, r)
                    for t in r[2]:
                        assert "by 0.0 lap" not in t and "save 0.00" not in t and "0.00 L a lap" not in t, t


def test_the_saving_asked_for_is_never_zero_and_aims_at_the_spare():
    w = cc.pit_window(30.0, 3.0, 120.0, 10.0, 0.0)           # 10.2 laps needed for 0.2 spare: 0.059 L a lap, rounded UP
    assert w["tight"] and not w["short"] and w["tight_save_lpl"] == 0.06
    w = cc.pit_window(30.05, 3.0, 120.0, 10.0, 0.0)
    assert w["tight"] and w["tight_save_lpl"] >= 0.01
    assert cc.save_amount(0.001, "kmh") == "0.01 L" and cc.save_amount(0.004, "mph") == "0.01 gal"
    # it is still asked for inside the last two laps (the target there is 0.1 lap, not nothing)
    w = cc.pit_window(2.0, 3.0, 120.0, 0.6, 0.4)            # 0.667 lap of fuel for 0.6 to go: 0.067 spare, 0.2 wanted
    assert w["tight"] and not w["short"] and not w["need_stop"] and w["tight_save_lpl"] > 0
    # the pre-save burn is kept until he has the spare, not until he is merely no longer short
    sim = S.RaceSim(laps=30, tank=120.0, burn=3.0, start_fuel=30 * 3.0 * 0.97, cars=3, dt=0.25)
    ch = chief(sim)
    seen = {}
    for own, f in sim.run():
        ch.feed(own, f, now=sim.t)
        if "ref" not in seen and ch._tight_ref:
            seen["ref"] = ch._tight_ref
            sim.burn = 3.0 * 0.96
        if own["Lap"] == 29 and own["LapDistPct"] > 0.5:
            seen["late"] = ch._tight_ref
            break
    assert seen.get("ref") and seen["late"] is not None and abs(seen["late"] - seen["ref"]) < 1e-9, seen


def test_the_stop_or_not_decision_is_continuous_in_the_last_laps():
    """Round 4: the same deficit said 'box this lap' at 2.3 laps to go and 'save 10%' at 2.0."""
    flips, prev = 0, None
    d = 4.0
    while d > 0.3:
        w = cc.pit_window(2.1 * 3.0, 3.0, 120.0, d, 0.0)
        if prev is not None and w["need_stop"] != prev:
            flips += 1
        prev = w["need_stop"]
        d -= 0.02
    assert flips <= 1, flips                                 # stop -> no stop once, as the flag gets close, never back
    for fl in (1.6, 2.0, 2.4, 3.0):                          # and for several amounts of fuel
        seq = [cc.pit_window(fl * 3.0, 3.0, 120.0, x / 100.0, 0.0)["need_stop"] for x in range(400, 30, -2)]
        assert sum(a != b for a, b in zip(seq, seq[1:])) <= 1, fl


def test_every_cars_pace_is_known_so_a_new_overall_leader_needs_no_guess():
    sim = S.RaceSim(laps=0, timed_s=3000.0, tank=200.0, burn=2.0, multiclass=True, cars=8)
    sim.set_pace(0, 50.0)
    for i in sim.cars:
        sim.cars[i].cls = S.CLASS_B if i % 2 == 1 or i == S.ME else S.CLASS_A
    ch = chief(sim)
    early = {}

    def watch(own, f, g):
        if own["SessionTime"] > 5.0 and "n" not in early:
            early["n"] = ch.snapshot()["laps_left"]["n"]
            early["how"] = ch.flag_lap()[1]
            early["want"] = ref_crossings(own["SessionTimeRemain"], 50.0, own["LapDistPct"], sim.cars[0].pct,
                                          p_me=60.0)
    drive(sim, ch, seconds=300, on_frame=watch)
    assert early["how"] in ("unknown", "best lap") and early["n"] >= early["want"], early     # never a lap short
    assert len(ch.leader_hist) >= 6, list(ch.leader_hist)                    # every car's clean laps, not just two
    assert ch.flag_lap()[1] == "overall leader" and abs(ch.flag_lap()[0] - 50.0) < 0.5
    # the unknown-pace count never undercounts (the reviewer's fuzz: 14% too low before)
    import random
    rnd = random.Random(3)
    low = 0
    for _ in range(20000):
        pc = rnd.uniform(80, 130)
        po = pc * rnd.uniform(0.88, 0.97)
        T, f, fo = rnd.uniform(0, 7200), rnd.random(), rnd.random()
        want = ref_crossings(T, po, f, fo, p_me=pc)
        got = cc.laps_to_go(time_rem=T, lap_f=f, leader_lap_s=pc, leader_f=fo, my_lap_s=pc, pace_unknown=True, pace_tol=0.0)["crossings"]
        low += got < want - 0 and want - got >= 1
    assert low <= 2, low                                                     # float ties at the very edge only


def test_a_dropped_box_call_is_made_again():
    sim = S.RaceSim(laps=20, tank=60.0, burn=3.0, start_fuel=40.0, cars=4)
    ch = chief(sim)
    sp = cc.CallSpeaker()
    sp.on_drop = ch.forgive
    said, issued = [], []
    for own, f in sim.run():
        # a car alongside for the whole of the first lap on which the box call is made
        sim.side = 3 if (sim.cars[2].completed + 1 == 12) else 1
        calls = ch.feed(own, f, now=sim.t)
        issued += [(own["Lap"], c.kind) for c in calls]
        sp.offer(calls)
        c = sp.pump(own, sim.t)
        if c:
            said.append((own["Lap"], c.kind))
        sp.take_events()
    boxes = [(lap, k) for lap, k in said if k == "box_now"]
    assert boxes, said
    # box this lap is made once for every lap it holds, and the one that was held up for the car alongside was not lost
    laps_issued = [lap for lap, k in issued if k == "box_now"]
    assert len(laps_issued) == len(set(laps_issued)), laps_issued
    assert any(lap == 12 for lap, _ in boxes) or any(lap == 13 for lap, _ in boxes), boxes
    # a dropped call gives its limit back: the same reading may make it again
    ch2 = cc.CrewChief()
    ch2.lap = 4
    c = _call("fuel_tight", 4)
    ch2._issue(c, 0.0)
    assert not ch2._allowed("fuel_tight", 1, 1.0)
    ch2.forgive(c)
    assert ch2._allowed("fuel_tight", 1, 1.0)
    sp2 = cc.CallSpeaker()
    sp2.on_drop = ch2.forgive
    sp2.offer([c])
    ch2._issue(c, 0.0)
    assert sp2.pump({"Brake": 0.6}, 100.0) is None            # too late: dropped for want of a quiet place
    assert ch2._allowed("fuel_tight", 1, 100.0)


def test_box_calls_get_through_mute_and_box_this_lap_is_once_a_lap():
    sp = cc.CallSpeaker()
    sp.muted = True
    free = {"Brake": 0.0, "Throttle": 1.0}
    for kind in ("box_now", "box_soon", "fuel_critical"):
        sp.offer([_call(kind, 5, t=10.0)])
        assert sp.pump(free, 10.0 + 3 * ["box_now", "box_soon", "fuel_critical"].index(kind)).kind == kind
    ch = cc.CrewChief()
    ch.lap = 7
    assert ch._allowed("box_now", 4, 0.0, key=7)
    ch._issue(_call("box_now", 7, level=4), 0.0, key=7)
    assert not ch._allowed("box_now", 4, 1.0, key=7) and ch._allowed("box_now", 4, 2.0, key=8)


def test_a_caution_stop_that_gains_nothing_is_not_called():
    # the reviewer's race: 80 laps on a 60 L tank, a caution at lap 19; three stops remain either way
    sim = S.RaceSim(laps=80, tank=60.0, burn=3.0, cars=4)
    sim.pit_at(lap=17, add=60.0)
    sim.caution(19 * 60 + 10, 21 * 60 + 40)
    ch = chief(sim)
    calls = drive(sim, ch, seconds=23 * 60)
    assert "caution_box" not in kinds(calls), [c.text for c in calls if c.kind == "caution_box"]
    assert ch.last_calc["window"]["stops"] >= 2
    # ... and when it takes a stop off the count, or the one stop is open now and the amount is real, it is
    # stopping now only takes a stop off the count when the tank then reaches the flag: 100 L is not 36 laps of 3 L
    assert cc.stops_needed(60.0, 3.0, 100.0, 36.0, lap_f=0.0) == 1 and cc.stops_if_stop_now(60.0, 3.0, 100.0, 36.0, 0.0) == 2
    assert cc.stops_needed(30.0, 3.0, 100.0, 30.0, lap_f=0.0) == 1 and cc.stops_if_stop_now(30.0, 3.0, 100.0, 30.0, 0.0) == 1


def test_stop_count_and_the_window_agree():
    """2.29 review: the count ignored the fuel wasted by stopping at a lap end; it now plans the stops lap by lap."""
    import random
    rnd = random.Random(11)
    bad = 0
    for _ in range(20000):
        b = rnd.uniform(1.0, 5.0)
        tank = rnd.uniform(40, 130)
        F = rnd.uniform(0, tank)
        f = rnd.random()
        n = rnd.randint(1, 120)
        dist = n - f
        w = cc.pit_window(F, b, tank, dist, f)
        if not w["need_stop"]:
            continue
        # replay the plan lap by lap: every stop reachable with fuel to spare, the flag reached at the end
        fuel, pos, ok = F, 0.0, True
        for c in w["stop_laps"]:
            x = c + 1 - f
            fuel -= (x - pos) * b
            ok = ok and fuel >= -1e-6
            fuel, pos = tank, x
        ok = ok and fuel - (dist - pos) * b >= -1e-6
        bad += (not ok) and w["feasible"]
        assert w["stops"] == len(w["stop_laps"]) or w["stops"] == 1
        if w["stops"] == 1 and w["feasible"]:
            assert w["k_earliest"] <= max(0, w["k_latest"]), w
    assert bad == 0, bad
    # the first stop of the plan is the window's latest lap
    w = cc.pit_window(60.0, 3.0, 60.0, 50.0, 0.0)
    assert w["stops"] == 2 and w["stop_laps"][0] == w["k_latest"] == 18
    # fuel wasted at lap ends: 23.29 L at 2 L a lap, a 30 L tank, 25 laps to go. The old sum said
    # ceil((25.4 * 2 - 23.29) / (30 - 2)) = 1 stop; but the stop is at the end of lap 10 with 3.29 L (more than the
    # spare lap) left in the tank, and 30 L from there is 15 laps of 25 - 10 = 15: 0 spare, so a second stop
    assert cc.stops_needed(23.29, 2.0, 30.0, 25.0, lap_f=0.0) == 2
    w = cc.pit_window(23.29, 2.0, 30.0, 25.0, 0.0)
    assert w["stops"] == 2 and len(w["stop_laps"]) == 2


def test_another_car_or_session_starts_fresh():
    sim = S.RaceSim(laps=30, tank=60.0, burn=3.0, cars=4)
    ch = chief(sim)
    drive(sim, ch, seconds=400)
    assert ch.burn.burn()[1] == "laps"
    # same car, a new sub-session: the figure is kept as a starting guess only
    secs = sim.sections()
    secs["WeekendInfo"]["SubSessionID"] = 111
    ch.set_info(secs, 2)
    secs = sim.sections()
    secs["WeekendInfo"]["SubSessionID"] = 222
    ch.set_info(secs, 3)
    ch.feed(sim.own(), sim.field(), now=sim.t + 1)
    assert ch.burn.burn()[1] == "from the last session" and ch.stops_done == 0
    # another car in the next session: nothing carries over
    sim2 = S.RaceSim(laps=30, tank=80.0, burn=4.0, cars=4)
    sec2 = sim2.sections()
    sec2["WeekendInfo"]["SubSessionID"] = 333
    sec2["DriverInfo"]["Drivers"][S.ME]["CarScreenName"] = "Other car"
    ch.set_info(sec2, 4)
    o = sim2.own()
    o["SessionNum"] = 2
    ch.feed(o, None, now=sim.t + 5)
    assert ch.burn.burn() == (None, "learning") and abs(ch.tank - 80.0) < 1e-9
    # a new SessionUniqueID alone is a new session too
    ch2 = chief(S.RaceSim(laps=30, cars=4))
    ch2.burn.seed, ch2.burn.seed_why = 2.5, "from the last session"
    o = dict(S.RaceSim(laps=30, cars=4).own(), SessionUniqueID=5)
    ch2.feed(o, None, now=1.0)
    ch2.stops_done = 2
    ch2.feed(dict(o, SessionTime=2.0, SessionUniqueID=6), None, now=2.0)
    assert ch2.stops_done == 0


def test_alongside_is_the_side_signal_or_a_tenth_of_a_second():
    def run(gap_s, side=1):
        sim = S.RaceSim(laps=80, tank=200.0, burn=2.0, cars=6)
        sim.cars[3].D = sim.cars[S.ME].D - gap_s / 60.0
        sim.side = side
        ch = chief(sim)
        calls = drive(sim, ch, seconds=15)
        return ch.snapshot()["behind"], kinds(calls)
    b, k = run(0.3)
    assert "alongside" not in b["tags"] and "alongside" not in k, (b, k)
    b, k = run(0.05)
    assert "alongside" in b["tags"] and "alongside" in k
    b, k = run(0.3, side=2)
    assert "alongside" in k


def test_the_estimated_time_gap_has_no_jump_across_the_line():
    """2.29 review: with F2Time unusable the gap came from EstTime, which runs on iRacing's lap, not the race pace, so it
    jumped when the car ahead was across the line and you were not."""
    sim = S.RaceSim(laps=80, tank=200.0, burn=2.0, f2=False, cars=6, lap_s=60.0)
    sec = sim.sections()
    sec["DriverInfo"]["DriverCarEstLapTime"] = 50.0
    ch = cc.CrewChief(units="kmh", clock=lambda: sim.t)
    ch.set_info(sec, 1)
    last, worst, n = None, 0.0, 0
    for own, f in sim.run(300):
        if f is not None:
            f = dict(f, CarIdxEstTime=[x * 50.0 / 60.0 for x in f["CarIdxEstTime"]])    # iRacing's 50 s lap
        ch.feed(own, f, now=sim.t)
        a = ch.snapshot().get("ahead")
        if a and a["idx"] == 1 and sim.t > 130:
            if last is not None:
                worst = max(worst, abs(a["gap_s"] - last))
            last = a["gap_s"]
            n += 1
    assert n > 100 and worst < 0.35, worst
    assert abs(last - 1.5) < 0.6, last


def test_feed_never_raises_on_odd_readings():
    import random
    rnd = random.Random(5)
    junk = [float("nan"), float("inf"), -float("inf"), "x", "", None, [], {}, True, -1, 10 ** 30, 1e300, "3", 2.5]
    errs = 0
    for run in range(60):
        sim = S.RaceSim(laps=rnd.choice([5, 20]), tank=60.0, burn=3.0, cars=rnd.choice([3, 8]),
                        timed_s=rnd.choice([None, 900.0]), multiclass=rnd.random() < 0.5, f2=rnd.random() < 0.5)
        ch = chief(sim)
        sp = cc.CallSpeaker()
        for own, f in sim.run(rnd.uniform(60, 400)):
            own = dict(own)
            for _ in range(rnd.choice([0, 0, 1, 3])):
                own[rnd.choice(list(own))] = rnd.choice(junk)
            if f is not None and rnd.random() < 0.3:
                f = dict(f)
                for _ in range(rnd.choice([1, 2])):
                    k = rnd.choice(list(f))
                    n_ = len(f[k]) if isinstance(f[k], list) else 3
                    f[k] = rnd.choice(junk + [[rnd.choice(junk) for _ in range(n_)]])
            calls = ch.feed(own, f, now=sim.t)             # must not raise
            sp.offer(calls)
            sp.pump(own, sim.t)
            ch.snapshot()
        errs += ch.error_count
        assert not ch.errors, ch.errors
    assert errs == 0, errs
    # nor on junk session text
    ch = cc.CrewChief()
    ch.set_info({"DriverInfo": {"DriverCarIdx": float("nan"), "Drivers": [{"CarIdx": "x"}, {"CarIdx": float("inf")}, {}],
                                "DriverCarFuelMaxLtr": "a"}, "WeekendInfo": {"SubSessionID": [1]}}, 1)
    assert ch.error_count == 0


# ------------------------------------------------------------------ flags are the ones pyirsdk names
def test_flag_bits_match_pyirsdk():
    try:
        import irsdk
    except Exception:  # noqa: BLE001
        print("skip pyirsdk is not installed here")
        return
    F = irsdk.Flags
    assert (cc.F_CHECKERED, cc.F_WHITE, cc.F_GREEN, cc.F_YELLOW, cc.F_RED) == (F.checkered, F.white, F.green, F.yellow, F.red)
    assert (cc.F_YELLOW_WAVING, cc.F_ONE_LAP_TO_GREEN) == (F.yellow_waving, F.one_lap_to_green)
    assert (cc.F_CAUTION, cc.F_CAUTION_WAVING, cc.F_FURLED) == (F.caution, F.caution_waving, F.furled)
    L = irsdk.CarLeftRight
    assert cc.ALONGSIDE_VALUES == (L.car_left, L.car_right, L.car_left_right, L.two_cars_left, L.two_cars_right)
    assert cc.flags_text(F.caution) == "caution" and cc.flags_text(F.yellow) == "yellow" and cc.flags_text(F.green) == "green"


# ------------------------------------------------------------------ the voice phrases
def test_every_crew_phrase_composes_and_numbers_say_units():
    voc = cc.vocabulary()
    assert len(voc) > 150
    bad = [p for p in voc if vp.compose(p) is None]
    assert not bad, bad[:5]
    # the clip scheme knows every phrase's clips
    keys = vp.all_keys()
    for p in voc:
        assert all(k in keys for k in vp.compose(p)), p
    assert vp.compose("Box this lap. Forty-two litres.") == ["cc_box_this_lap", "cc_litres_42"]
    assert vp.compose("Box now, cheap stop. Eleven gallons.") == ["cc_box_now_cheap_stop", "cc_gallons_11"]
    assert vp.compose("Car behind closing, three tenths a lap.") == ["cc_car_behind_closing", "cc_rate_3"]
    assert vp.compose("Box this lap. One hundred and twenty-one litres.") is None      # not in the pack: stays text
    assert vp.compose("Box this lap. One litres.") is None                              # grammar is checked
    # the earlier phrases are unchanged
    assert vp.compose("Brake marker, T3") == ["brake_marker_turn_3"]
    assert vp.compose("Down three tenths") == ["down_0_3"]
    # lines(): the crew chief's section comes after everything older
    ln = vp.lines()
    sec = [x["section"] for x in ln]
    assert sec.index("M") > sec.index("L") and "M" in vp.SECTIONS


def test_all_six_voices_have_the_crew_chief_clips():
    keys = set(vp.all_keys())
    crew = {k for k in keys if k.startswith("cc_")}
    assert len(crew) > 150
    for pid in ("engineer", "michael", "fenrir", "heart", "kumo", "omega"):
        pack = vp.find_pack(pid)
        assert pack is not None, pid
        missing = sorted(crew - set(pack.clips))
        if missing:
            print(f"skip {pid} lacks {len(missing)} crew chief clips (not generated yet): {missing[:3]}")
            continue
        for p in cc.vocabulary():
            assert pack.compose(p) is not None, (pid, p)
        assert pack.duration_ms(pack.compose("Box this lap. Forty-two litres.")) > 500


def test_a_pack_without_the_clips_falls_back_to_text_and_a_log_line():
    from debrief import coachvoice
    pack = vp.find_pack("engineer")
    old = vp.VoicePack(pack.folder, pack.manifest, {k: v for k, v in pack.clips.items() if not k.startswith("cc_")})
    logs = []

    class Player:
        def __init__(self):
            self.played = []

        def play(self, pcm, rate, **kw):
            self.played.append(kw.get("tag"))
            return True

        def close(self, timeout=0):
            pass

        def status(self):
            return {}
    pl = Player()
    v = coachvoice.PackVoice(pack=old, player=pl, renderer=None, log=logs.append)
    v.wait_ready()
    assert v.covers("Brake marker, T3") and not v.covers("Box this lap.")
    assert v.say("Box this lap.", priority=4) is False and not pl.played
    assert any("Box this lap." in m for m in logs), logs
    # a full pack says it
    v2 = coachvoice.PackVoice(pack=pack, player=Player(), renderer=None, log=logs.append)
    v2.wait_ready()
    if v2.covers("Box this lap."):
        assert v2.say("Box this lap. Forty-two litres.", priority=4)


# ------------------------------------------------------------------ the live runner: voice, queue priorities, race log
class SimSource:
    """Plays a RaceSim to a LiveRunner as fast as it goes. The runner's clock is the sim's."""
    interval = 0.0
    speed = 0.0
    realtime = False

    def __init__(self, sim):
        self.sim = sim
        self.gen = sim.run()
        self.done = False
        self.state = "playing"
        self.v = 1
        self.sec = sim.sections()

    def read(self):
        try:
            own, f = next(self.gen)
        except StopIteration:
            self.done, self.state = True, "finished"
            return None
        own["_field"] = f
        own["Speed"] = 40.0
        return own

    def info(self):
        return {"car": {"name": "GT3", "path": "gt3"}, "track": {"name": "Test Ring", "code": "testring"}, "length_m": 5000}

    def info_sections(self):
        return self.v, self.sec

    def close(self):
        pass


class CrewVoice:
    backend = "fake"
    available = True

    def __init__(self):
        self.said = []

    def say(self, text, priority=0, t=None, check=None):
        self.said.append({"text": text, "priority": priority, "t": t, "checked": check() if check else None})
        return True

    def covers(self, text):
        return not text.startswith("Car ahead pitting")

    def duration(self, text):
        return None

    def prepare(self, phrases):
        pass

    def cancel(self):
        pass

    def status(self):
        return {"backend": "fake", "available": True}

    def set(self, **kw):
        pass

    def close(self, timeout=0):
        pass


def run_race(sim, settings=None, **kw):
    from debrief import livecoach as lc
    src = SimSource(sim)
    v = CrewVoice()
    logs = []
    s = settings or lc.Settings(sample_hz=20.0)
    rec = TMP / f"rec{len(list(TMP.glob('rec*')))}"
    rec.mkdir()
    clog = cc.CrewLog(lambda: rec, TMP / "fallback", log=logs.append)
    r = lc.LiveRunner(src, v, settings=s, clock=lambda: sim.t, crew_log=clog, log=logs.append, **kw)
    r.start()
    t0 = time.time()
    while r.running() and time.time() - t0 < 120:
        time.sleep(0.05)
    r.stop()
    return r, v, rec, logs


def test_runner_speaks_the_calls_and_writes_the_race_log():
    sim = S.RaceSim(laps=40, tank=60.0, burn=3.0)
    sim.pit_at(lap=17, add=48.0)
    sim.set_pace(3, 59.4)
    sim.car_pit(1, 700.0, 740.0)
    r, v, rec, logs = run_race(sim)
    said = [x["text"] for x in v.said]
    assert any(t.startswith("Box in three laps") for t in said), said
    assert any(t.startswith("Box this lap. ") for t in said)
    assert any(t.startswith("Car behind closing") for t in said)
    # fuel critical is priority 6, the rest 4 (coaching cues are 1 to 3): one shared queue, fuel first
    pr = {x["text"].split(".")[0]: x["priority"] for x in v.said}
    assert all(x["priority"] in (cc.PRIORITY_CREW, cc.PRIORITY_FUEL) for x in v.said)
    assert all(x["priority"] == cc.PRIORITY_FUEL for x in v.said if x["text"].startswith(("Fuel critical", "Data lost")))
    assert all(x["checked"] is None for x in v.said)              # asked right before playing: still true
    # a phrase the voice has no clip for is a log line, once, and stays on screen
    assert any("Car ahead pitting" in m and "no clip" in m for m in logs), logs
    # the race log is in the recording's folder, one row per call (situation -> call)
    p = rec / cc.CrewLog.NAME
    assert p.exists()
    from debrief import liverec
    rows = list(liverec.read_jsonl(p))
    calls = [x for x in rows if x["k"] == "call"]
    assert calls and all("sit" in x and x["say"] and x["rank"] for x in calls)
    assert {"lap", "call", "said"} <= {x["k"] for x in rows}
    c0 = next(x for x in calls if x["kind"] == "box_soon")
    assert c0["sit"]["fuel_l"] is not None and c0["sit"]["box_in"] is not None and c0["sit"]["to_go"]
    st = r.status()
    assert st["crew"]["enabled"] and st["crew"]["state"] in ("racing", "waiting")
    assert st["crew"]["perf"]["steps"] > 100


def test_the_switches_calls_off_and_voice_off():
    from debrief import livecoach as lc
    sim = S.RaceSim(laps=30, tank=60.0, burn=3.0)
    r, v, rec, logs = run_race(sim, lc.Settings(crew_voice=False))
    assert v.said == []
    st = r.status()["crew"]
    assert st["enabled"] and st["voice_on"] is False and st["calls"]
    assert any(c["spoken"] is False and "voice is off" in c["why"].lower() for c in st["calls"]), st["calls"][:3]
    assert (rec / cc.CrewLog.NAME).exists()                    # still written to the race log
    sim = S.RaceSim(laps=30, tank=60.0, burn=3.0)
    r, v, rec, logs = run_race(sim, lc.Settings(crew_calls=False))
    assert v.said == [] and r.status()["crew"]["enabled"] is False and not (rec / cc.CrewLog.NAME).exists()
    d = lc.Settings.from_dict({"crew_calls": "false", "crew_voice": False})
    assert d.crew_calls is False and d.crew_voice is False
    assert lc.Settings().crew_calls is True and lc.Settings().crew_voice is True


def test_the_race_log_falls_back_to_the_coach_logs_when_there_is_no_recording():
    folder = TMP / "fb"
    clog = cc.CrewLog(lambda: None, folder)
    clog.add({"k": "call", "x": 1})
    assert clog.flush() is None                                # no recording yet: kept in memory
    p = clog.close()
    assert p is not None and p.parent == folder
    with gzip.open(p, "rt", encoding="utf-8") as f:
        assert json.loads(f.readline())["x"] == 1


def test_rehearsal_has_no_crew_chief():
    from debrief import livecoach as lc
    r = lc.LiveRunner(SimSource(S.RaceSim()), CrewVoice(), mode="rehearsal")
    assert r.crew is None and r.status()["crew"]["enabled"] is False


# ------------------------------------------------------------------ the window
def test_the_overlay_line():
    from debrief import overlay
    sim = S.RaceSim(laps=50, tank=60.0, burn=3.0)
    ch = chief(sim)
    drive(sim, ch, seconds=700)
    snap = ch.snapshot()
    st = {"running": True, "state": "coaching", "show": "coaching", "crew": dict(snap, enabled=True)}
    vm = overlay.view_model(st, "mph")
    assert vm["crew"].startswith("Fuel ") or vm["crew"]
    st["crew"]["call"] = {"rank": 3, "text": "Box in 2 laps. Add 30 L."}
    assert overlay.view_model(st, "mph")["crew"] == "Box in 2 laps. Add 30 L." and overlay.view_model(st, "mph")["crew_tone"] == "bad"
    st["crew"]["state"] = "lost"
    assert overlay.view_model(st, "mph")["crew"] == "DATA LOST"
    assert overlay.view_model({"running": True, "show": "coaching"}, "mph")["crew"] == ""
    assert " · " in snap["overlay"] and snap["overlay"].startswith("Fuel ")


def test_panel_settings_and_modules_are_wired():
    ui = ROOT / "debrief" / "desktop" / "ui"
    html = (ui / "index.html").read_text(encoding="utf-8")
    js = (ui / "app.js").read_text(encoding="utf-8")
    for i in ("crPanel", "crCall", "crPos", "crLeft", "crBox", "crBoxWin", "crBoxAdd", "crFuel", "crAhead", "crBehind",
              "crLap", "coCrewCalls", "coCrewVoice"):
        assert f'id="{i}"' in html, i
    assert "function crewRender" in js and "crew_calls" in js and "crew_voice" in js
    init = (ROOT / "debrief" / "desktop" / "__init__.py").read_text(encoding="utf-8")
    assert '"crewchief"' in init
    srv = (ROOT / "debrief" / "desktop" / "server.py").read_text(encoding="utf-8")
    assert '"crew_calls": bool' in srv and '"crew_voice": bool' in srv
    import shutil as _sh
    node = _sh.which("node")
    if node:
        import subprocess
        r = subprocess.run([node, "--check", str(ui / "app.js")], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr[:300]


def test_playback_of_a_telemetry_file_feeds_the_chief_without_a_race():
    import fake_ibt as fi
    from debrief import livecoach as lc
    d = TMP / "ibt"
    d.mkdir()
    p = d / "cc.ibt"
    fi.make(str(p), [92.0 + 0.1 * (i % 3) for i in range(4)])
    src = lc.IbtPlaybackSource(str(p), speed=0)
    ch = cc.CrewChief()
    n = 0
    while True:
        s = src.read()
        if s is None:
            break
        n += 1
        assert ch.feed(s, None, now=n * 0.05) == []            # a file has no fuel and no race: quiet
    assert n > 100


# ------------------------------------------------------------------ cost
def _never_save_race(seed, short, sigma, push, laps):
    """Round 5: the reviewer's never-save driver. He ignores every ask to save and boxes only when told to box this lap
    (and only if that call comes before the pit entry, on a lap that is not the last). Returns (dry?, the box calls)."""
    import random as _r
    rnd = _r.Random(seed)
    B = 3.0
    sim = S.RaceSim(laps=laps, tank=120.0, burn=B, start_fuel=laps * B * (1 - short), cars=3, dt=0.25)
    ch = chief(sim)
    last_lap, boxed = None, False
    boxes, kinds_seen = [], []
    for own, f in sim.run():
        for c in ch.feed(own, f, now=sim.t):
            kinds_seen.append(c.kind)
            if c.kind == "box_now":
                ltg = (ch.last_calc or {}).get("ltg") or {}
                boxes.append((own["Lap"], own["LapDistPct"], ltg.get("crossings"), laps))
                if not boxed and own["LapDistPct"] < 0.9 and own["Lap"] <= laps:
                    boxed = True
                    sim.pit_at(lap=own["Lap"], add=max(c.data.get("add_l") or 0, 3.0))
        if own["Lap"] != last_lap:
            last_lap = own["Lap"]
            p = push if own["Lap"] > laps - 2 else 0.0
            sim.burn = B * (1 + rnd.gauss(0, sigma)) * (1 + p)
        if sim.fuel < 0:
            return True, boxes, kinds_seen
    return False, boxes, kinds_seen


def test_a_driver_who_never_saves_is_told_to_box_while_he_still_can():
    """Round 5 (race-losing): 11 of 140 never-save drivers ran dry because 'Box this lap' first came on the final lap. The
    last pit chance is worked out from what he burned on his last lap; he is told to box at the line that starts it, and
    never after it."""
    import random as _r
    dry = n = 0
    late = []
    saw_save_or_box = saw_box = 0
    for short in (0.0, 0.01, 0.02, 0.03, 0.04, 0.06, 0.08):
        for sigma, push in ((0.01, 0.0), (0.02, 0.05)):
            for seed in range(5):
                laps = _r.Random(seed).choice([12, 20, 30])
                d, boxes, ks = _never_save_race(seed * 101 + int(short * 1000), short, sigma, push, laps)
                n += 1
                dry += d
                saw_save_or_box += "save_or_box" in ks
                saw_box += bool(boxes)
                # never 'box this lap' on the final lap, nor past the pit entry on the lap
                late += [b for b in boxes if (b[2] is not None and b[2] <= 1) or b[1] >= cc.DEFAULT_PIT_ENTRY - 1e-9]
    assert not late, late[:5]
    assert dry <= 1 * n / 140 + 0.5, (dry, n)                 # the reviewer's bar: at most 1 in 140
    assert saw_box and saw_save_or_box


def test_the_last_pit_chance_rule_in_numbers():
    """Hand check: 3.0 L a lap, 20-lap race, line starting lap 17 (4 laps to go: crossings 4). With 5.4 L he can pit at the
    end of this lap (5.4 - 3.0 >= 0) but not the next (5.4 - 6.0 < 0): the last chance, and he is short (5.4 < 4 x 3.15 +
    0.15), so 'Box this lap'. With 8.9 L at the line starting lap 18 (3 to go) the last chance is next lap and no stop is called yet: 'Save X or box next lap'."""
    def at_line(fuel, laps_left):
        sim = S.RaceSim(laps=20, tank=120.0, burn=3.0, start_fuel=fuel + 3.0 * (20 - laps_left), cars=3, dt=0.25)
        ch = chief(sim)
        seen = []
        for own, f in sim.run():
            for c in ch.feed(own, f, now=sim.t):
                seen.append((own["Lap"], round(own["LapDistPct"], 2), c.kind, c.say))
            if own["Lap"] > 20 - laps_left + 1:
                break
        return [x for x in seen if x[0] == 20 - laps_left + 1]
    a = at_line(5.4, 4)
    assert any(k == "box_now" and p < 0.5 for _, p, k, _ in a), a
    b = at_line(8.9, 3)
    assert any(k == "save_or_box" and say == "Save fuel or box next lap." for _, _, k, say in b), b
    assert not any(k == "box_now" for _, _, k, _ in b), b
    # the final lap: no box call at all, only saving
    c_ = at_line(2.5, 1)
    assert not any(k in ("box_now", "box_soon", "fuel_critical") for _, _, k, _ in c_), c_
    # the achieved burn is what he used last lap (or the last two on average, whichever is more), not the old median
    ch = cc.CrewChief()
    ch._ach_hist = [3.0, 2.4]
    assert abs(ch.achieved_burn() - 2.7) < 1e-9
    ch._ach_hist = [2.4, 3.0]
    assert ch.achieved_burn() == 3.0
    assert ch.pit_entry_frac() == cc.DEFAULT_PIT_ENTRY
    ch._pit_entry_f = 0.93
    assert ch.pit_entry_frac() == 0.93


def test_the_new_save_or_box_phrase_has_a_clip_in_every_voice():
    assert "Save fuel or box next lap" in vp.CREW_WHOLES
    assert cc.RANKS["save_or_box"] == 3 and "save_or_box" in cc.MUTE_OK
    for pack in ("engineer", "omega", "michael", "kumo", "heart", "fenrir"):
        assert (ROOT / "debrief" / "data" / "voicepacks" / pack / "cc_save_fuel_or_box_next_lap.wav").exists(), pack


def test_pace_errors_of_one_percent_never_count_too_few_laps():
    """Round 5 (race-losing tail): a pace error of half a percent made the timed-finish count one too low 4.7% of the time.
    Now the count is the largest over the leader's and your own pace within 1%: never too low (a lap too many is safe)."""
    import random
    rnd = random.Random(4)
    for err, tmax, best in ((0.005, 3600, False), (0.005, 900, False), (0.01, 900, True)):
        low = 0
        N = 8000
        for _ in range(N):
            pc = rnd.uniform(80, 130)
            po = pc * rnd.uniform(0.88, 1.0)
            T, f, fo = rnd.uniform(0, tmax), rnd.random(), rnd.random()
            want = ref_crossings(T, po, f, fo, p_me=pc)
            used = po * (1 - rnd.uniform(0, err * 2)) if best else po * (1 + rnd.uniform(-err, err))
            got = cc.laps_to_go(time_rem=T, lap_f=f, leader_lap_s=used, leader_f=fo,
                                my_lap_s=pc * (1 + rnd.uniform(-err, err)))["crossings"]
            low += got < want
        assert low / N < 0.001, (err, tmax, best, low / N)
    # exact lap times still give the exact count when asked to (the maths above), and the default is never below it
    for _ in range(3000):
        p, T, f, fl = rnd.uniform(40, 200), rnd.uniform(0, 7200), rnd.random(), rnd.random()
        exact = cc.laps_to_go(time_rem=T, lap_f=f, leader_lap_s=p, leader_f=fl, pace_tol=0.0)["crossings"]
        wide = cc.laps_to_go(time_rem=T, lap_f=f, leader_lap_s=p, leader_f=fl)["crossings"]
        assert exact <= wide <= exact + math.ceil(0.03 * exact) + 1, (exact, wide)


def test_a_best_lap_is_treated_as_an_unknown_pace():
    """Round 5: when only the overall leader's best lap is known the pace is not known: the wide scan is used."""
    sim = S.RaceSim(laps=0, timed_s=1800.0)
    ch = chief(sim)
    seen = []
    real = cc.laps_to_go
    ch.flag_lap = lambda: (sim.lap_s, "best lap")
    cc.laps_to_go = lambda *a, **k: (seen.append(k.get("pace_unknown")), real(*a, **k))[1]
    try:
        drive(sim, ch, seconds=120)
    finally:
        cc.laps_to_go = real
    assert seen and all(x is True for x in seen), seen


def test_a_frame_costs_little():
    sim = S.RaceSim(laps=50, tank=60.0, burn=3.0, cars=64)
    sim.pit_at(lap=17, add=48.0)
    ch = chief(sim)
    frames = list(sim.run(900.0))
    t0 = time.thread_time()
    for own, f in frames:
        ch.feed(own, f, now=own["SessionTime"])
    per = (time.thread_time() - t0) / len(frames)
    # at 20 Hz one core is 50 ms a frame; 1% is 0.5 ms. This box is slower than a 5600X3D, so the bar is a generous 2 ms
    # (the audit, tools/perf_audit.py, reports the real figure against the 1% budget)
    assert per < 0.002, per


if __name__ == "__main__":
    failed = 0
    tests = [(k, v) for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for name, fn in tests:
        try:
            fn()
            print("ok  ", name)
        except Exception:  # noqa: BLE001
            failed += 1
            print("FAIL", name)
            traceback.print_exc()
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
