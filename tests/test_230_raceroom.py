"""Debrief 2.30 "Endurance Race Room": role detection, the stint tracker and swaps, the swap alerts (time zones), estimated
fuel for a crew PC, pit stops of every car, the handover card, the live runner (driver PC and crew PC), the team-race log,
the Endurance review (stats, stops against the plan, traffic, rivals), merging logs from several PCs, the server pages, the
overlay line and the voice clips in all six packs.

    python3 tests/ci_run.py tests/test_230_raceroom.py

A scripted multiclass team race (tests/endu_sim.py): three drivers, three swaps, a caution, a long stop, a late driver, and the
same race read from the PC of the driver in the car, from a crew PC (no fuel channel), and from a PC that has no seat.
No iRacing, no network. Windows-safe: no signals, temp folders, utf-8 files.
"""
from __future__ import annotations

import gzip
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.modules.setdefault("irsdk", None)
TMP = Path(tempfile.mkdtemp(prefix="debrief-230-"))
os.environ["DEBRIEF_SETTINGS"] = str(TMP / "settings.toml")
os.environ["DEBRIEF_CACHE_DIR"] = str(TMP / "cache")
os.environ["DEBRIEF_SCHEDULE_DIR"] = str(TMP / "schedule")
os.environ["DEBRIEF_UPDATE_STATE"] = str(TMP / "update.json")

import crew_sim as S  # noqa: E402
import endu_sim as ES  # noqa: E402
from debrief import crewchief as cc, endurancelog as EL, endureview as EV, liverec, overlay, raceroom as RR, voicepack as vp  # noqa: E402

fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


WALL0 = 1_800_000_000.0
_RUNS = {}


def sim_run(pc_user, key="plain", log=None, **kw):
    """One scripted team race through Race Room and the crew chief, cached by (pc_user, key)."""
    k = (pc_user, key)
    if k in _RUNS and log is None:
        return _RUNS[k]
    sim = ES.standard_sim(pc_user=pc_user, **{a: b for a, b in kw.items() if a in ("listing",)})
    if key == "story":
        sim.caution(2000, 2200)
        sim.car_stop(4, 1000, 1060)
        sim.car_stop(6, 2300, 2360)
    states = {}

    def on_step(room, chief, own, f):
        for at in (500, 1700, 3000, 4600):
            if at not in states and sim.t >= at:
                states[at] = json.loads(json.dumps(room.state()))
    out = ES.run(sim, log=log, on_step=on_step, **{a: b for a, b in kw.items() if a in ("plan", "iam", "tz")})
    res = {"sim": sim, "room": out[0], "chief": out[1], "alerts": out[2], "events": out[3], "states": states}
    if log is None:
        _RUNS[k] = res
    return res


# ------------------------------------------------------------------ small pieces
def test_names_and_times():
    names = ["Alex Able", "Ben Baker", "Cal Cole"]
    check(RR.match_name(names, "ben baker") == "Ben Baker", "name match: same words in any case")
    check(RR.match_name(names, "Ben B. Baker") == "Ben Baker", "name match: a middle initial does not matter")
    check(RR.match_name(names, "Dan Dunn") is None, "name match: a stranger matches nobody")
    check(RR.match_name(["Sam Lee", "Sam Fox"], "Sam") is None, "name match: a first name two people share is not guessed")
    check(RR.clock_text(125) == "2:05" and RR.clock_text(3725) == "1:02:05" and RR.clock_text(None) == "--", "clock text")
    base = RR.local_time(WALL0)
    later = RR.local_time(WALL0, 5.5)
    h0, m0 = map(int, base.split(":"))
    h1, m1 = map(int, later.split(":"))
    check((h1 * 60 + m1 - (h0 * 60 + m0)) % 1440 == 330, "local time shifted by a driver's time zone (+5.5 h = 330 min)")
    check((int(RR.local_time(WALL0, -3).split(":")[0]) - h0) % 24 == 21, "local time shifted back 3 h")


def test_role_detection():
    sim = ES.standard_sim(pc_user=1)
    sim.driver_i = 1                                            # Ben in the car
    info = RR.parse_info(sim.sections())
    own = sim.own()
    r = RR.detect_role(own, info)
    check(r["role"] == "driver" and r["driving"], "role: in the car (IsOnTrack) is the driver")
    sim.driver_i = 0                                            # Alex in the car, this PC is Ben's
    info = RR.parse_info(sim.sections())
    r = RR.detect_role(sim.own(), info)
    check(r["role"] == "crew" and not r["driving"] and r["team_id"] == ES.TEAM_ID, "role: on the team and not in the car is crew (TeamID)")
    sec = sim.sections()
    for d in sec["DriverInfo"]["Drivers"]:
        d["TeamID"] = 0
    r = RR.detect_role(sim.own(), RR.parse_info(sec))
    check(r["role"] != "crew", "role: without a team nobody is crew")
    r = RR.detect_role({}, RR.parse_info({}))
    check(r["role"] == "none", "role: no session text yet is none")
    # a different TeamID is not our team: generic, never by name
    sec = sim.sections()
    for d in sec["DriverInfo"]["Drivers"]:
        if d.get("TeamID"):
            d["TeamID"] = 99999
            d["TeamName"] = "Anything Else"
    r = RR.detect_role(sim.own(), RR.parse_info(sec))
    check(r["role"] == "crew" and r["team_id"] == 99999, "role: any TeamID works, no team name is built in")


def test_swap_alert_timing():
    fired = set()
    check(RR.swap_alert(2400, fired, 1) is None, "swap alert: 40 min out says nothing")
    a = RR.swap_alert(930, fired, 1)
    check(a and a["kind"] == "swap_15" and "fifteen" in a["say"], "swap alert: about 15 minutes out says fifteen")
    check(RR.swap_alert(900, fired, 1) is None, "swap alert: the fifteen is said once")
    check(RR.swap_alert(500, fired, 1) is None, "swap alert: nothing between")
    a = RR.swap_alert(330, fired, 1)
    check(a and a["kind"] == "swap_5" and "five" in a["say"], "swap alert: about 5 minutes out says five")
    check(RR.swap_alert(100, fired, 1) is None, "swap alert: the five is said once")
    f2 = set()
    a = RR.swap_alert(200, f2, 2)
    check(a and a["kind"] == "swap_5" and RR.swap_alert(190, f2, 2) is None, "swap alert: joining late gives only the five, once")
    check(RR.swap_alert(850, f2, 2) is None, "swap alert: the fifteen is never said after the five")
    check(RR.swap_alert(-5, set(), 3) is None and RR.swap_alert(None, set(), 3) is None, "swap alert: unknown or past is silent")
    f3 = set()
    check(RR.swap_alert(930, f3, 3) and RR.swap_alert(930, f3, 4), "swap alert: the next swap gets its own pair")
    check(RR.SWAP_FAR_SAY.rstrip(".") == "Your stint starts in about fifteen minutes" and RR.SWAP_NEAR_SAY.rstrip(".") == "Your stint starts in about five minutes",
          "swap alert: the exact phrases the clips say")


def test_estimated_fuel():
    cf = RR.CrewFuel()
    check(cf.estimate(16.5, 3.0) is None and cf.how == "unknown", "crew fuel: unknown until a stop or a tank is known")
    cf.set(12.0, 80.0, "plan")
    check(abs(cf.estimate(16.5, 3.0) - 66.5) < 1e-9, "crew fuel hand check: 80 - 4.5 laps x 3.0 = 66.5 L")
    check(cf.estimate(11.0, 3.0) == 80.0, "crew fuel: never more than the fuel after the stop")
    check(cf.estimate(40.0, 3.0) == 0.0, "crew fuel: never below zero")
    check(cf.estimate(16.5, None) is None, "crew fuel: no burn, no estimate")


def test_stop_tracker():
    st = RR.StopTracker()
    n = 6

    def fld(pit, surf):
        return {"CarIdxOnPitRoad": pit + [False] * (n - len(pit)), "CarIdxTrackSurface": surf + [3] * (n - len(surf)),
                "CarIdxLapCompleted": [10] * n}
    # car 1 stops: in the lane, in the stall for a while, out
    st.feed(0.0, fld([False, False], []))
    st.feed(1.0, fld([False, True], [3, 2]))
    st.feed(10.0, fld([False, True], [3, 1]))
    st.feed(40.0, fld([False, True], [3, 2]))
    done = st.feed(50.0, fld([False, False], []))
    check(len(done) == 1 and done[0]["idx"] == 1 and abs(done[0]["lane_s"] - 49.0) < 0.2 and abs(done[0]["stall_s"] - 30.0) < 0.2,
          "stops: lane 49 s and 30 s in the stall, seen on the pit-road and stall channels")
    # car 2 drives through (never in a stall): a pass, not a stop
    st.feed(60.0, fld([False, False, True], [3, 3, 2]))
    st.feed(75.0, fld([False, False, False], []))
    check(st.count(2) == 0 and st.passes.get(2) == 1, "stops: driving through is a pass, not a stop")
    # a caution is only a note on the stop
    st.feed(80.0, fld([False, False, False, True], [3, 3, 3, 2]), caution=True)
    st.feed(85.0, fld([False, False, False, True], [3, 3, 3, 1]), caution=True)
    done = st.feed(110.0, fld([False, False, False, False], []))
    check(len(done) == 1 and done[0]["under_caution"] is True, "stops: a stop under a caution is flagged, not invented")
    # nobody pitting means no stop: a caution alone creates nothing
    st2 = RR.StopTracker()
    for t in range(0, 200, 5):
        st2.feed(float(t), fld([False] * n, []), caution=(60 < t < 120))
    check(sum(st2.count(i) for i in range(n)) == 0 and not st2.passes, "stops: a caution on its own is never a pit cycle")
    # without the stall channel a long visit still counts, a short one does not
    st3 = RR.StopTracker()
    f = lambda p: {"CarIdxOnPitRoad": [p] * 1, "CarIdxLapCompleted": [3]}  # noqa: E731
    st3.feed(0.0, f(False))
    st3.feed(1.0, f(True))
    done = st3.feed(41.0, f(False))
    st3.feed(50.0, f(True))
    st3.feed(55.0, f(False))
    check(len(done) == 1 and st3.passes.get(0) == 1, "stops: without the stall channel 40 s is a stop and 5 s is a pass")


def test_handover_card():
    c = RR.handover_card(stop={"lane_s": 62.0, "stall_s": 36.0}, planned={"fuel_add_l": 58.0, "stop_s": 55.0, "tyres": True},
                         driver_from="Alex Able", driver_to="Ben Baker", fuel_before=2.0, fuel_after=60.0, incidents=2,
                         unit="kmh", next_lap_s=60.4, next_burn=2.0)
    check(c["fuel_how"] == "measured" and abs(c["fuel_added"]["l"] - 58.0) < 0.01 if isinstance(c["fuel_added"], dict) and "l" in c["fuel_added"]
          else c["fuel_how"] == "measured", "handover: fuel added is measured when both readings exist")
    check(c["incidents"] == 2 and c["target_lap"] and c["burn_text"], "handover: incidents, target lap and fuel a lap for the next stint")
    p = RR.handover_card(stop=None, planned={"fuel_add_l": 58.0, "stop_s": 55.0}, driver_from="A", driver_to="B", unit="kmh")
    check(p["fuel_how"] == "plan" and "(plan)" in p["text"], "handover: with no reading the fuel is the plan's and says so")
    u = RR.handover_card(stop=None, planned=None, driver_from="A", driver_to="B", unit="kmh")
    check(u["fuel_how"] == "unknown" and u["fuel_added"] is None, "handover: nothing known stays unknown")


def test_plan_delta():
    pv = RR.PlanView({"inputs": ES.plan_inputs(), "title": "t", "saved": "s"})
    check(pv.names == ES.DRIVERS and len(pv.stints) == 4, "plan view: three drivers, four stints")
    on = pv.delta(10.0, pv.time_at_laps(10.0))
    check(on and on["state"] == "on", "plan delta: on plan")
    ahead = pv.delta(30.0, pv.time_at_laps(30.0) + 400)
    check(ahead is None or ahead["state"] in ("behind", "ahead"), "plan delta: a state")
    late = pv.delta(10.0, pv.time_at_laps(10.0) + 300)
    check(late["state"] == "behind" and late["seconds"] < 0 and "Behind plan" in late["text"], "plan delta: five minutes later is behind")
    early = pv.delta(10.0, pv.time_at_laps(10.0) - 300)
    check(early["state"] == "ahead" and "Ahead of plan" in early["text"], "plan delta: five minutes sooner is ahead")
    check(pv.delta(None, 100) is None and pv.delta(5, 0) is None, "plan delta: nothing known, nothing said")


# ------------------------------------------------------------------ the scripted race through Race Room
def test_driver_pc_stints_swaps_and_alerts():
    r = sim_run(1)
    room, alerts = r["room"], r["alerts"]
    check([s["driver"] for s in room.stints_done] == ["Alex Able", "Ben Baker", "Cal Cole"], "driver PC: three stints in order")
    check(all(s["laps"] in (28, 29, 30) for s in room.stints_done), "driver PC: 29 laps a stint (give or take the lap of the swap)")
    check(len(room.swaps) == 3 and all(abs(s["lane_s"] - 60.0) < 1.5 for s in room.swaps), "driver PC: three swaps, the pit lane 60 s each")
    kinds = [(a["kind"], a["driver"]) for a in alerts]
    check(kinds == [("swap_15", "Ben Baker"), ("swap_5", "Ben Baker")], f"alerts on Ben's PC: fifteen then five, once each ({kinds})")
    f, n = [a for a in alerts if a["kind"] == "swap_15"][0], [a for a in alerts if a["kind"] == "swap_5"][0]
    check(840 <= f["eta_s"] <= 960 and 270 <= n["eta_s"] <= 330, f"alert ETAs: about 15 and 5 minutes ({f['eta_s']}, {n['eta_s']})")
    check("fifteen" not in f["text"] and "15 minutes" in f["text"] and "your time" in f["text"], "alert text: on screen in numbers, with the time in his own clock")
    check(r["states"][500]["role"]["role"] == "crew", "Ben's PC while Alex drives: crew")
    check(r["states"][3000]["role"]["role"] == "driver", "Ben's PC once he is in the car: driver")
    check(any(e["k"] == "role" for e in r["events"]), "the role change is an event")


def test_crew_pc_text_only_and_estimated_fuel():
    r = sim_run(1)
    st = r["states"][500]                                         # Ben, watching Alex
    check(st["role"]["role"] == "crew", "crew PC: role crew")
    check(st["fuel"]["estimated"] is True and st["fuel"]["text"].startswith("~"), "crew PC: fuel is labelled estimated on screen (~)")
    check("estimated" in st["fuel"]["basis"], "crew PC: the basis says estimated")
    check(st["board"]["estimated"] is True, "crew PC: the pit board says the numbers are estimated")
    # no fuel channel on this PC at all: the sim's crew PC sends none
    own_none = [k for k in ("FuelLevel", "PitSvFlags") if ES.EnduSim(pc_user=1).own().get(k) is None]
    check(set(own_none) == {"FuelLevel", "PitSvFlags"}, "the crew PC in the sim has no fuel channel")
    drv = r["states"][3000]
    check(drv["fuel"]["estimated"] is False and "~" not in str(drv["fuel"]["text"]), "driver PC: real fuel, not marked estimated")
    # driving Alex's PC in the same race: estimated while Ben drives
    a = sim_run(0)
    mid = a["states"][3000]
    check(mid["role"]["role"] == "crew" and mid["fuel"]["estimated"] is True, "Alex's PC while Ben drives: crew with estimated fuel")
    # an estimate follows the sim's real tank within a lap or two of burn
    sim = ES.standard_sim(pc_user=0)
    for own, f in sim.run(3000.0):
        pass
    est_l = mid["fuel"].get("l")
    check(est_l is not None and abs(est_l - sim.fuel) <= 3 * 2.0, f"crew PC: the estimate ({est_l}) is within three laps of burn of the real tank ({sim.fuel:.1f})")


def test_crew_pc_never_speaks():
    from debrief import livecoach as lc
    check(RR.CREW_NO_VOICE == "crew: text only", "crew PC: the reason shown is 'crew: text only'")
    run = run_live(1, seconds=2200.0, key="a")                      # Ben's PC: crew until lap 29, then driver
    said = [x["text"] for x in run["v"].said]
    check(not any(t in said for t in [c.text for c in run["r"].crew.calls if c.why == RR.CREW_NO_VOICE]),
          "live runner: a call made while crew is never spoken")
    check(all(x["priority"] in (cc.PRIORITY_CREW, cc.PRIORITY_FUEL) for x in run["v"].said), "live runner: only crew-chief priorities")
    check(any(c.why == RR.CREW_NO_VOICE for c in run["r"].crew.calls), "live runner: calls made while crew are marked 'crew: text only'")
    al = [t.rstrip(".") for t in said if t.startswith("Your stint starts")]
    check(al == ["Your stint starts in about fifteen minutes", "Your stint starts in about five minutes"],
          f"live runner: the swap alerts are spoken ({al})")
    st = run["r"].status()
    check(st["room"]["enabled"] and st["room"]["perf"]["steps"] > 100, "live runner: status carries the room and its timing")
    check(run["r"].room_state()["state"] in ("racing", "waiting"), "live runner: room_state for the page")


class SimSource:
    interval = 0.0
    speed = 0.0
    realtime = False

    def __init__(self, sim, seconds=None):
        self.sim = sim
        self.gen = sim.run(seconds)
        self.done = False
        self.state = "playing"

    def read(self):
        try:
            own, f = next(self.gen)
        except StopIteration:
            self.done, self.state = True, "finished"
            return None
        own["_field"] = f
        own["Speed"] = 40.0 if own.get("IsOnTrack") else 0.0
        return own

    def info(self):
        return {"car": {"name": "GT3", "path": "gt3"}, "track": {"name": "Test Ring", "code": "testring"}, "length_m": 5000}

    def info_sections(self):
        return self.sim.info_version, self.sim.sections()

    def close(self):
        pass


class Voice:
    backend = "fake"
    available = True

    def __init__(self):
        self.said = []

    def say(self, text, priority=0, t=None, check=None):
        self.said.append({"text": text, "priority": priority, "t": t})
        return True

    def covers(self, text):
        return True

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


_LIVE = {}


def run_live(pc_user, seconds=None, key="a", library=None, plan=True):
    from debrief import livecoach as lc
    k = (pc_user, seconds, key)
    if k in _LIVE:
        return _LIVE[k]
    sim = ES.standard_sim(pc_user=pc_user)
    v = Voice()
    logs = []
    lib = library or (TMP / f"lib{len(_LIVE)}")
    saved = {"inputs": ES.plan_inputs(), "title": "t", "saved": "s"} if plan else None
    rl = EL.RaceLog(library_fn=lambda: lib, clock=lambda: sim.t, wall=lambda: WALL0)
    clog = cc.CrewLog(lambda: None, TMP / "crewfallback", log=logs.append)
    r = lc.LiveRunner(SimSource(sim, seconds), v, settings=lc.Settings(sample_hz=20.0), clock=lambda: sim.t, crew_log=clog,
                      log=logs.append, room_plan_for=(lambda track: saved) if saved else None, room_iam=lambda: ES.DRIVERS[pc_user],
                      room_log=rl)
    r.start()
    t0 = time.time()
    while r.running() and time.time() - t0 < 180:
        time.sleep(0.05)
    r.stop()
    _LIVE[k] = {"r": r, "v": v, "logs": logs, "lib": lib, "rl": rl, "sim": sim}
    return _LIVE[k]


# ------------------------------------------------------------------ the story: caution, long stop, late driver, re-plan
def test_replan_on_events_and_late_driver():
    sim = ES.standard_sim(pc_user=1)
    sim.caution(900, 1100)
    room = {}

    def on_step(rm, chief, own, f):
        room["rm"] = rm
        if sim.t > 1500 and "delay" not in room:
            room["before"] = (rm.replan_res or {}).get("finish_s")
            room["n"] = rm.set_delay("Cal Cole", 25)
            room["delay"] = True
        if sim.t > 1650 and "after" not in room:
            room["after"] = rm.replan_res
    ES.run(sim, on_step=on_step, seconds=1700.0)
    rm = room["rm"]
    check(room["n"], "late driver: the plan knows Cal Cole")
    check(rm.replan_res is not None and rm.replan_res["stops"] >= 1, "re-plan: made from the live state")
    check(any(e.get("k") == "replan" for e in rm.take_events()) or rm.replan_reason, "re-plan: logged with its reason")
    st = rm.state()["replan"]
    check(st["ok"] and st["options"] is not None and st["finish_text"], "re-plan: the page has the options and the finish")
    check(rm.set_delay("Nobody Here", 5) is None, "late driver: a name not in the plan is refused")


def test_replan_is_throttled():
    sim = ES.standard_sim(pc_user=1)
    calls = {"n": 0}
    from debrief import replan as RP
    orig = RP.replan

    def counting(inputs, st):
        calls["n"] += 1
        return orig(inputs, st)
    RR.RP.replan = counting
    try:
        ES.run(sim, seconds=1800.0)
    finally:
        RR.RP.replan = orig
    laps = 1800 / 60
    check(0 < calls["n"] <= laps * 2.5 + 6, f"re-plan runs about once a lap, not every reading ({calls['n']} in {laps:.0f} laps)")


def test_no_plan_says_so():
    r = sim_run(1, key="noplan", plan=False)
    st = r["room"].state()
    check(st["plan"]["has_plan"] is False and st["plan"]["make"] is True and "No saved stint plan" in st["plan"]["text"],
          "no plan: it says so and offers Make a plan")
    check(r["alerts"] == [] and st["stint"]["driver"], "no plan: no alerts, but the stint tracker still names the driver")
    check(st["replan"]["ok"] is False, "no plan: no re-plan")


def test_listing_all_drivers_same_car():
    r = sim_run(1, key="all", listing="all")
    names = [s["driver"] for s in r["room"].stints_done]
    check(names == ["Alex Able", "Ben Baker", "Cal Cole"], f"driver list 'all' (iRacing lists everyone): swaps still found ({names})")


def test_overlay_line_and_view_model():
    r = sim_run(1)
    ov = r["states"][3000]["overlay"]
    check(ov.startswith("Stint ") and "BOX" in ov or "no stop" in ov, f"overlay line: stint timer first ({ov})")
    cr = r["states"][500]["overlay"]
    check("(est.)" in cr or "BOX" not in cr, f"overlay line on a crew PC marks the box call as estimated ({cr})")
    vm = overlay.view_model({"running": True, "show": "coaching", "state": "running",
                             "room": {"enabled": True, "state": "racing", "overlay": "Stint 41:02 · next Ben 02:15 · BOX L29 (est.)"}})
    check(vm["room"].startswith("Stint 41:02"), "overlay view model carries the Race Room line")
    vm = overlay.view_model({"running": True, "show": "muted", "state": "running", "mute": {"quiet_left": 20},
                             "room": {"enabled": True, "state": "racing", "overlay": "Stint 1:00"}})
    check(vm["room"] == "" and vm["sub"], "overlay: the muted hint takes the line instead")
    vm = overlay.view_model({"running": True, "show": "coaching", "state": "running", "room": {"enabled": True, "state": "waiting", "overlay": "x"}})
    check(vm["room"] == "", "overlay: nothing from Race Room while it is waiting")


# ------------------------------------------------------------------ the race log and the review
def test_log_size_and_rows():
    sim = ES.standard_sim(pc_user=1)
    sim.caution(2000, 2200)
    lib = TMP / "lib_size"
    lg = EL.RaceLog(library_fn=lambda: lib, clock=lambda: sim.t, wall=lambda: WALL0)
    ES.run(sim, log=lg.add, seconds=3600.0)
    lg.close()
    check(lg.path is not None and lg.path.name == EL.NAME and lg.path.parent.parent.name == EL.FOLDER, "log: <library>/Endurance/<day>_<track>/race.jsonl.gz")
    rows = list(liverec.read_jsonl(lg.path))
    ks = {r["k"] for r in rows}
    check({"hdr", "f", "o", "stop", "stint", "swap_alert"} <= ks, f"log: header, frames, own, stops, stints, alerts ({sorted(ks)})")
    frames = [r for r in rows if r["k"] == "f"]
    check(3500 <= len(frames) <= 3700, f"log: one frame a second ({len(frames)} in an hour)")
    mb_h = lg.bytes / 1e6
    check(mb_h < 15.0, f"log: {mb_h:.2f} MB for the hour (budget 15 MB/h)")
    hdr = rows[0]
    check(hdr["k"] == "hdr" and hdr.get("plan") and hdr.get("drivers"), "log: the header holds the plan and every car")
    txt = json.dumps(hdr)
    check("g61" not in txt.lower() and "token" not in txt.lower(), "log: nothing secret in the header")


def make_logs():
    if "logs" in _RUNS:
        return _RUNS["logs"]
    paths = []
    for pcu in (0, 1, 2):
        sim = ES.standard_sim(pc_user=pcu)
        sim.caution(2000, 2200)
        sim.car_stop(4, 1000, 1060)
        sim.car_stop(6, 2300, 2360)
        lib = TMP / f"lib_pc{pcu}"
        lg = EL.RaceLog(library_fn=lambda lib=lib: lib, clock=lambda s=sim: s.t, wall=lambda: WALL0)
        ES.run(sim, log=lg.add)
        lg.close()
        paths.append(lg.path)
    _RUNS["logs"] = paths
    return paths


def test_review_stats_from_three_pcs():
    paths = make_logs()
    logs = [EV.load_log(p) for p in paths]
    m = EV.merge_logs(logs)
    check(m["ok"] and len(m["used"]) == 3 and not m["skipped"], "merge: three teammates' logs joined, none left out")
    r = EV.review(m)
    check(r["ok"] and r["laps"]["count"] >= 85 and not r["laps"]["missing"], f"review: every lap read ({r['laps']})")
    check([s["driver"] for s in r["stints"]] == ES.DRIVERS and not any(s["guessed"] for s in r["stints"]), "review: three stints, named by the logs")
    d = {x["name"]: x for x in r["drivers"]}
    check(set(d) == set(ES.DRIVERS), "review: pace by driver for all three")
    check(d["Cal Cole"]["median"] < d["Alex Able"]["median"] < d["Ben Baker"]["median"], "review: Cal quickest, Ben slowest, as scripted")
    check(all(x["best"] <= x["median"] and x["spread"] is not None for x in r["drivers"]), "review: best, median and spread")
    check(len(r["stops"]) == 3 and all(abs(s["lane_s"] - 60.0) < 1.5 for s in r["stops"]), "review: three stops, 60 s in the lane")
    check(all(s["plan_stop_s"] is not None for s in r["stops"]) and r["stops"][0]["delta_s"] > 0, "review: every stop against the plan's 55 s")
    check(len(r["fuel"]) == 3 and all(f["measured"] for f in r["fuel"]), "review: fuel measured from the drivers' own PCs")
    check(r["rivals"] and any(x["stops"] >= 1 for x in r["rivals"]), "review: what the class rivals did (their stops)")
    rv = {x["idx"]: x for x in r["rivals"]}
    check(4 in rv and rv[4]["stops"] == 1 and 6 in rv and rv[6]["stops"] == 1, "review: the rivals' scripted stops (cars 4 and 6) are counted")
    check("cost_s" in r["traffic"] and isinstance(r["traffic"]["by_class"], list), "review: traffic cost from other classes")
    check(r["positions"] and r["finish"]["cpos"] >= 1, "review: positions over time and the finish")
    check(r["lap_list"] and all(isinstance(x["lap"], int) for x in r["lap_list"]), "review: the lap list")


def test_merge_never_drops_silently():
    paths = make_logs()
    junk = TMP / "race_junk.jsonl.gz"
    junk.write_bytes(b"not a gzip file at all")
    nohdr = TMP / "race_nohdr.jsonl.gz"
    with gzip.open(nohdr, "wt", encoding="utf-8") as f:
        f.write(json.dumps({"k": "f", "t": 1}) + "\n")
    other = TMP / "race_other.jsonl.gz"
    with gzip.open(other, "wt", encoding="utf-8") as f:
        f.write(json.dumps({"k": "hdr", "v": 1, "track": "Elsewhere", "sub": 999, "team_id": 5, "who": "Zed Zane", "drivers": []}) + "\n")
        f.write(json.dumps({"k": "f", "t": 1.0, "lc": [1], "lp": [0.5]}) + "\n")
    logs = [EV.load_log(p) for p in paths] + [EV.load_log(p) for p in (junk, nohdr, other)]
    m = EV.merge_logs(logs)
    check(m["ok"] and len(m["used"]) == 3, "merge: the good logs are still used")
    why = " | ".join(f"{s['who']}: {s['why']}" for s in m["skipped"])
    check(len(m["skipped"]) >= 2, f"merge: bad logs are listed with who and why ({why})")
    check(any(("could not be read" in s["why"] or "header" in s["why"]) for s in m["skipped"]) and any("header" in s["why"] for s in m["skipped"]),
          "merge: an unreadable file and a missing header both say why")
    check(all(s.get("file") for s in m["skipped"]), "merge: each skipped log names its file")
    check(any("left out" in n for n in m["notes"]), "merge: the notes say it in words")
    r = EV.review(m)
    check(r["skipped"] == m["skipped"] and r["notes"], "review: carries the skipped list and the notes to the page")
    # one log alone still works, and a missing teammate's laps come from the frames
    one = EV.review(EV.merge_logs([EV.load_log(paths[0])]))
    check(one["ok"] and one["laps"]["count"] >= 85, "review: a single PC's log is enough (other drivers' laps come from the frames)")
    none = EV.review(EV.merge_logs([]))
    check(not none["ok"] and none["text"], "review: no logs is a plain-words answer")


def test_find_and_review_folder():
    lib = TMP / "lib_find"
    d = lib / "Endurance" / "2026-11-01_test"
    d.mkdir(parents=True)
    paths = make_logs()
    shutil.copy(paths[0], d / "race.jsonl.gz")
    shutil.copy(paths[1], d / "race_ben.jsonl.gz")
    races = EV.find_races(lib)
    check(len(races) == 1 and races[0]["id"] == "Endurance/2026-11-01_test" and len(races[0]["files"]) == 2, "find races: one folder with two logs")
    r = EV.review_folder(d)
    check(r["ok"] and len(r["used"]) == 2, "review folder: both PCs' logs merged")
    check(not EV.review_folder(TMP / "nothing_here")["ok"], "review folder: an empty folder says so")


# ------------------------------------------------------------------ server, voice, files
def test_voice_clips_in_all_six_packs():
    names = ["cc_your_stint_starts_in_about_fifteen_minutes", "cc_your_stint_starts_in_about_five_minutes"]
    got = vp.compose(RR.SWAP_FAR_SAY)
    check(got == [names[0]] or got == names[0] or names[0] in str(got), f"compose: the fifteen-minute phrase maps to its clip ({got})")
    packs = sorted(p.name for p in (ROOT / "debrief" / "data" / "voicepacks").iterdir() if p.is_dir())
    check(len(packs) == 6, f"six voice packs ({packs})")
    for p in packs:
        d = ROOT / "debrief" / "data" / "voicepacks" / p
        have = all((d / f"{n}.wav").stat().st_size > 2000 for n in names)
        man = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        check(have and all(n in json.dumps(man) for n in names), f"voice pack {p}: both swap clips present and in the manifest")
    check(set(RR.SWAP_FAR_SAY.split()) | set(RR.SWAP_NEAR_SAY.split()) >= {"fifteen", "five"}, "phrases use words, so the voice says them right")


def test_server_pages():
    from debrief import app
    from debrief.desktop import server as SV
    lib = TMP / "srvlib"
    lib.mkdir()
    (TMP / "settings.toml").write_text('token = "g61-test-token-ABCDEFGHIJKLMNOP"\n' f"library_folder = '{lib}'\n"
                                       'api_base = "http://127.0.0.1:9/api/v1/"\n', encoding="utf-8")
    app.LOG_FILE = str(TMP / "debrief.log")
    s = SV.DebriefServer(heartbeat_timeout=3600)
    threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(rel, body=None, headers=True):
        h = {"X-Debrief": "1"} if headers else {}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            h["Content-Type"] = "application/json"
        rq = urllib.request.Request(s.url + rel, headers=h, data=data, method="POST" if data is not None else "GET")
        try:
            with op.open(rq, timeout=30) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
    try:
        code, body = call("api/raceroom")
        j = json.loads(body)
        check(code == 200 and j["ok"] and j["running"] is False and j["room"]["state"] == "waiting", "server: Race Room before the coach runs says to start it")
        code, body = call("api/raceroom", headers=False)
        check(code in (400, 401, 403, 404), f"server: the page API needs the Debrief header ({code})")
        code, body = call("api/raceroom", {"action": "iam", "name": "Ben Baker"})
        check(code == 200 and json.loads(body)["iam"] == "Ben Baker", "server: 'I am' is saved")
        code, body = call("api/raceroom")
        check(json.loads(body)["iam"] == "Ben Baker", "server: 'I am' comes back")
        code, body = call("api/raceroom", {"action": "delay", "driver": "Ben Baker", "minutes": 5})
        check(code != 200 or not json.loads(body).get("ok"), "server: a delay with no race running is refused in words")
        code, body = call("static/raceroom.js")
        check(code == 200 and b"DebriefHost" in body, "server: raceroom.js is served")
        code, body = call("api/endurance-review")
        check(json.loads(body)["races"] == [], "server: no team race logs yet is an empty list")
        d = lib / "Endurance" / "2026-11-01_x"
        d.mkdir(parents=True)
        shutil.copy(make_logs()[1], d / "race.jsonl.gz")
        code, body = call("api/endurance-review")
        races = json.loads(body)["races"]
        check(len(races) == 1, "server: the race log is listed")
        code, body = call("api/endurance-review?id=" + urllib.request.quote(races[0]["id"]))
        j = json.loads(body)
        check(code == 200 and j["review"]["ok"] and j["review"]["stints"], "server: one race's review")
        code, body = call("api/endurance-review?id=" + urllib.request.quote("../../etc"))
        check(code != 200 or not json.loads(body).get("ok", True), "server: an id outside the library is refused")
        code, body = call("api/endurance-review?id=" + urllib.request.quote("Endurance/nope"))
        check(code != 200 or not json.loads(body).get("ok", True), "server: an unknown race is refused in words")
    finally:
        try:
            s.shutdown()
        except Exception:  # noqa: BLE001
            pass


def test_files_and_credits():
    import debrief.desktop as D
    mods = getattr(D, "FEATURE_MODULES", ())
    flat = json.dumps(list(mods)) if not isinstance(mods, dict) else json.dumps(mods)
    for m in ("raceroom", "replan", "endurancelog", "endureview"):
        check(m in flat, f"FEATURE_MODULES lists {m}")
    src = (ROOT / "debrief" / "raceroom.py").read_text(encoding="utf-8") + (ROOT / "debrief" / "endureview.py").read_text(encoding="utf-8")
    check("RaceCap" not in src and "racecap" not in src.lower(), "RaceCap is not touched")


def test_no_team_names_built_in():
    src = "".join((ROOT / "debrief" / f).read_text(encoding="utf-8") for f in ("raceroom.py", "replan.py", "endurancelog.py", "endureview.py"))
    ui = (ROOT / "debrief" / "desktop" / "ui" / "raceroom.js").read_text(encoding="utf-8")
    for bad in ("Midfield", "Menace", "Black Magik", "Swazi", "midfield"):
        check(bad not in src and bad not in ui, f"generic teams: no '{bad}' in the Race Room code")


# ------------------------------------------------------------------ round 2: the plan cursor, burn learning, stint names, merge, overlay
def _sampled(sim, saved=None, iam="Cal Cole", join=0.0, every=300):
    """Feeds the sim through Race Room + the crew chief from `join` seconds on (a PC that comes into the race late)."""
    from debrief import crewchief as ccm
    saved = saved or {"inputs": ES.plan_inputs(), "title": "t", "saved": "s"}
    room = RR.RaceRoom(units="kmh", plan_for=lambda tr: saved, iam=lambda: iam, wall=lambda: WALL0 + sim.t)
    chief = ccm.CrewChief(units="kmh", clock=lambda: sim.t)
    alerts, ver, seen = [], None, {}
    for own, f in sim.run():
        if sim.t < join:
            continue
        secs = None
        if sim.info_version != ver:
            ver = sim.info_version
            secs = sim.sections()
            chief.set_info(secs, ver)
        o2 = room.pre(own, f, secs, ver, sim.t)
        chief.feed(o2, f, now=sim.t)
        room.post(chief, sim.t, own)
        alerts += room.take_alerts()
        m = int(sim.t // every)
        if m not in seen:
            sv = room.state().get("stint") or {}
            seen[m] = {"t": sim.t, "cur": sv.get("driver"), "next": (sv.get("next") or {}).get("driver"), "est": room._est_fuel,
                       "real": sim.fuel, "k": room.plan_k, "unsure": bool(room.fuel_unsure)}
    return room, chief, alerts, seen


def test_unplanned_stop_does_not_move_the_plan():
    for pc, who in ((1, "Ben Baker"), (2, "Cal Cole")):
        sim = ES.EnduSim(pc_user=pc)
        sim.stop(10, 0.0, stall=20, lane=50)             # a repair stop: same driver, no fuel
        for lap, add, to in ES.STD_STOPS:
            sim.stop(lap, add, to=to)
        room, chief, alerts, seen = _sampled(sim, iam=who)
        kinds = [(a["kind"], a["driver"]) for a in alerts]
        check(kinds == [("swap_15", who), ("swap_5", who)], f"repair stop at lap 10, {who}'s PC: both swap alerts still come, once ({kinds})")
        after = [v for v in seen.values() if 1100 <= v["t"] <= 1300][0]
        check(after["next"] == ("Ben Baker" if pc == 1 else "Ben Baker") and room.team_stops >= 4, "after the repair stop the next driver is still the plan's (Ben Baker)")
        check(abs(after["est"] - after["real"]) <= 2.5, f"the estimate is not reset by a stop with no fuel ({after['est']:.1f} against {after['real']:.1f} L)")
        check(room.stints_done and [x["laps"] for x in room.stints_done] == [29, 29, 29], "the repair stop does not make a stint")


def test_joining_late_starts_from_the_race():
    for join, want in ((0.0, ["swap_15", "swap_5"]), (1000.0, ["swap_15", "swap_5"]), (2500.0, ["swap_15", "swap_5"]), (3300.0, ["swap_5"])):
        sim = ES.standard_sim(pc_user=2)
        room, chief, alerts, seen = _sampled(sim, join=join)
        check([a["kind"] for a in alerts] == want and all(a["driver"] == "Cal Cole" for a in alerts),
              f"PC joined at {join:.0f} s: Cal gets {want} ({[a['kind'] for a in alerts]})")
        first = seen[min(seen)]
        exp = "Alex Able" if join < 1700 else "Ben Baker"
        check(first["cur"] == exp and first["next"] == ("Ben Baker" if exp == "Alex Able" else "Cal Cole"),
              f"PC joined at {join:.0f} s: driver now {first['cur']}, next {first['next']}")


def test_no_burn_learned_from_estimated_fuel():
    sim = ES.EnduSim(pc_user=1, burns=(0.0, 0.3, 0.0), timed_s=2700.0)
    sim.stop(29, 58, to=1)
    seen = {}

    def on_step(room, chief, own, f):
        L = room.team_laps
        role = room.role.get("role")
        if role == "crew" and L >= 5:
            seen.setdefault("crew_laps", 0)
            seen["crew_laps"] += 0 if not chief.burn.laps else 1
        if L not in seen and L >= 26:
            b, how = chief.burn.burn()
            seen[L] = (role, b, how, len(chief.burn.laps))
    ES.run(sim, on_step=on_step)
    check(seen.get("crew_laps") == 0, "a crew PC learns no burn from its own estimate (nothing in the burn tracker)")
    flip = min(L for L in seen if isinstance(L, int) and seen[L][0] == "driver")
    s3 = seen[flip + 3]
    check(s3[2] == "laps" and abs(s3[1] - 2.3) < 0.05, f"three laps after he takes the car the burn is his real 2.3 L ({s3})")
    check(seen[flip][1] is not None and abs(seen[flip][1] - 2.0) < 0.01 and seen[flip][2] != "laps", "until then the plan's figure stands in, and it says so")


def test_every_swap_is_a_stint_on_every_pc_and_listing():
    for listing in ("all", "current"):
        for pc in (0, 1, None):
            sim = ES.standard_sim(pc_user=pc, listing=listing)
            room, chief, alerts, ev = ES.run(sim)
            laps = [x["laps"] for x in room.stints_done]
            names = [x["driver"] for x in room.stints_done]
            check(laps == [29, 29, 29] and names == ES.DRIVERS, f"listing {listing!r}, PC {pc}: three stints of 29 laps by Alex, Ben, Cal ({laps} {names})")


def test_merge_counts_a_stop_once():
    def log(me, t_in, shift):
        hdr = {"k": "hdr", "sub": 1, "team_id": 7, "team_idx": 0, "me": me,
               "drivers": [{"idx": 0, "name": "A", "class": 1}, {"idx": 1, "name": "X", "class": 1}]}
        frames = [{"k": "f", "t": t + shift, "lc": [t // 60, t // 61], "lp": [(t % 60) / 60, (t % 61) / 61], "po": [1, 2], "cp": [1, 2],
                   "ll": [60.0, 61.0], "pit": [], "sf": []} for t in range(0, 600)]
        stops = [{"k": "stop", "idx": 0, "lap": 5, "t_in": t_in, "t_out": t_in + 50, "lane_s": 50.0, "stall_s": 30, "under_caution": False},
                 {"k": "stop", "idx": 1, "lap": 5, "t_in": t_in + 100, "t_out": t_in + 150, "lane_s": 50.0, "stall_s": 30, "under_caution": False}]
        return {"path": me, "name": me + ".jsonl.gz", "hdr": hdr, "hdrs": [hdr], "frames": frames, "own": [], "events": [], "stops": stops,
                "teamlaps": [], "replans": [], "rows": 1, "error": ""}
    m = EV.merge_logs([log("Alex", 300.4, 0.0), log("Ben", 300.6, 0.2)])
    check([(x["idx"], round(x["t_in"])) for x in m["stops"]] == [(0, 300), (1, 400)], "the same stop read 0.2 s apart by two PCs counts once")
    r = EV.review(m)
    check(len(r["stops"]) == 1 and len(r["stints"]) == 2, "review: one team stop and two stints, no phantom stint")
    rv = {x["idx"]: x for x in r["rivals"]}
    check(rv.get(1, {}).get("stops") == 1, "review: the rival's stop is not doubled")
    m2 = EV.merge_logs([log("Alex", 300.4, 0.0), log("Ben", 330.0, 0.0)])
    check(len([x for x in m2["stops"] if x["idx"] == 0]) == 2, "two stops 30 s apart are still two stops")


def test_overlay_says_est_on_a_crew_pc():
    base = {"running": True, "show": "coaching", "state": "running"}
    line = "Fuel 13.4 laps · Box L17-18 · Plan +1"
    on = dict(base, crew={"enabled": True, "state": "racing", "overlay": line, "call": None, "fuel_estimated": True})
    check(overlay.view_model(on)["crew"].endswith("(est.)"), "crew PC: the fuel line carries the flag (est.)")
    off = dict(base, crew={"enabled": True, "state": "racing", "overlay": line, "call": None, "fuel_estimated": False})
    check("(est.)" not in overlay.view_model(off)["crew"], "driver PC: no est.")
    # words like "latest" and "best" never decide it
    tricky = dict(base, crew={"enabled": True, "state": "racing", "overlay": "Latest best gap 1.2 s", "call": None, "fuel_estimated": False})
    check("(est.)" not in overlay.view_model(tricky)["crew"], "'latest' and 'best' do not trip the est. mark")
    call = {"kind": "alongside", "rank": 2, "text": "Car alongside."}
    al = dict(base, crew={"enabled": True, "state": "racing", "overlay": line, "call": call, "fuel_estimated": True})
    check("(est.)" not in overlay.view_model(al)["crew"], "a call that is not about fuel (car alongside) is not marked est.")
    fc = {"kind": "box_soon", "rank": 3, "text": "Box in three laps."}
    check(overlay.view_model(dict(base, crew={"enabled": True, "state": "racing", "overlay": line, "call": fc, "fuel_estimated": True}))["crew"].endswith("(est.)"),
          "a box call on a crew PC is marked est.")
    # the crew chief sets the flag from Race Room: a crew PC says so in its snapshot
    r = sim_run(1)
    check(r["chief"].snapshot().get("fuel_estimated") in (True, False), "the crew chief's snapshot carries the flag")


S_CASES = {
    "S1 splash L22": [(22, 10, None, 15), (29, 58, 1, 35), (58, 58, 2, 35), (87, 50, 0, 35)],
    "S2 repair L27": [(27, 0, None, 20), (29, 58, 1, 35), (58, 58, 2, 35), (87, 50, 0, 35)],
    "S4 repair L29, swap L31": [(29, 0, None, 20), (31, 58, 1, 35), (60, 58, 2, 35), (89, 50, 0, 35)],
}


def test_unplanned_stops_near_the_plan_stop_never_lose_the_alerts():
    for name, stops in S_CASES.items():
        for listing in ("current", "all"):
            sim = ES.EnduSim(pc_user=2, listing=listing)
            for lap, add, to, stall in stops:
                sim.stop(lap, add, stall=stall, to=to)
            room, chief, alerts, seen = _sampled(sim, iam="Cal Cole", every=100)
            kinds = [a["kind"] for a in alerts if a["driver"] == "Cal Cole"]
            check(kinds == ["swap_15", "swap_5"], f"{name}, listing {listing}: Cal's PC still gets both swap alerts, once each ({kinds})")
            done = list(room.stints_done) + ([dict(room.stint, laps=room.team_laps - room.stint["start_lap"])] if room.stint else [])
            names = [x["driver"] for x in done]          # the stint in progress counts: in S4 the last swap is after the sim's lap 88
            check(names[:3] == ES.DRIVERS, f"{name}, listing {listing}: the stints are Alex, Ben, Cal ({names})")
            first = ES.STD_STOPS[0][0] if name != "S4 repair L29, swap L31" else 31       # Alex drives to the first real swap
            check(done and abs(done[0]["laps"] - first) <= 1, f"{name}, listing {listing}: Alex's stint is {first} laps ({[x['laps'] for x in done]})")
            if listing == "current":
                mid = [v for v in seen.values() if v["cur"] == "Alex Able" and v["k"] == 0 and v["t"] > 1000]
                if name != "S1 splash L22":
                    est_ok = all(v["est"] is None or abs(v["est"] - v["real"]) <= 12 or v["unsure"] for v in mid)
                    check(est_ok, f"{name}: a stop with no driver change does not invent a planned fill")


def test_a_stop_without_a_driver_change_marks_the_fuel_uncertain():
    sim = ES.EnduSim(pc_user=2)
    sim.stop(27, 0, stall=20, lane=50)
    for lap, add, to in ES.STD_STOPS:
        sim.stop(lap, add, to=to)
    box = {}

    def on_step(room, chief, own, f):
        if sim.t > 1700 and "st" not in box:
            box["st"] = json.loads(json.dumps(room.state()))
            box["k"] = room.plan_k
    ES.run(sim, on_step=on_step, seconds=1710.0)
    check(box["k"] == 0, "a repair stop at lap 27: the car is still in the plan's first stint")
    st = box["st"]
    check(st["fuel"]["estimated"] and st["fuel"].get("uncertain"), "crew PC: the estimate is marked uncertain after the unplanned stop")
    check(st["board"]["box"] is None and "uncertain" in st["board"].get("uncertain", ""), "crew PC: no box claim is made from an uncertain estimate")


def test_the_cursor_follows_the_driver_in_the_car():
    # the team keeps Alex out for a second stint (the swap at lap 29 is skipped); the plan said Ben
    sim = ES.EnduSim(pc_user=2)
    for lap, add, to in ((29, 58, None), (58, 58, 1), (87, 50, 2)):
        sim.stop(lap, add, to=to)
    box = {}

    def on_step(room, chief, own, f):
        if 2400 < sim.t < 2500 and "k" not in box:
            sv = room.state()["stint"]
            box.update(k=room.plan_k, cur=sv["driver"], nxt=(sv.get("next") or {}).get("driver"))
    ES.run(sim, on_step=on_step, seconds=2500.0)
    check(box.get("cur") == "Alex Able" and box.get("nxt") == "Ben Baker", f"Alex stays out: next driver is Ben, not the plan's shifted one ({box})")


def test_a_late_driver_moves_the_next_driver_and_the_alerts():
    for pc, who in ((1, "Ben Baker"), (2, "Cal Cole")):
        sim = ES.EnduSim(pc_user=pc)
        for lap, add, to in ((29, 58, 2), (58, 58, 1), (87, 50, 0)):     # the team follows the re-plan: Cal before Ben
            sim.stop(lap, add, to=to)
        done = [False]

        def on_step(room, chief, own, f):
            if not done[0] and sim.t > 100:
                done[0] = True
                room.set_delay("Ben Baker", 60)
                room.force_replan()
        room, chief, alerts, ev = ES.run(sim, on_step=on_step, iam=who)
        got = [(a["kind"], a["driver"]) for a in alerts if a["stop"] == 1]
        if who == "Ben Baker":
            check(got == [], f"Ben is marked 60 minutes late: he is not told his stint starts in 15 or 5 minutes ({got})")
        else:
            check(got == [("swap_15", "Cal Cole"), ("swap_5", "Cal Cole")], f"Ben is late: Cal gets both alerts for the first swap ({got})")


def test_the_saved_plan_is_read_again_mid_race():
    sim = ES.standard_sim(pc_user=2)
    first = {"inputs": ES.plan_inputs(), "title": "one", "saved": "1"}
    second = {"inputs": ES.plan_inputs(names=("Alex Able", "Ben Baker", "Dan Dunn")), "title": "two", "saved": "2"}
    cur = {"v": first}
    from debrief import crewchief as ccm
    room = RR.RaceRoom(units="kmh", plan_for=lambda tr: cur["v"], iam=lambda: "Cal Cole", wall=lambda: WALL0 + sim.t)
    chief = ccm.CrewChief(units="kmh", clock=lambda: sim.t)
    ver, titles = None, []
    for own, f in sim.run(300.0):
        secs = None
        if sim.info_version != ver:
            ver = sim.info_version
            secs = sim.sections()
            chief.set_info(secs, ver)
        if sim.t > 100:
            cur["v"] = second
        o2 = room.pre(own, f, secs, ver, sim.t)
        chief.feed(o2, f, now=sim.t)
        room.post(chief, sim.t, own)
    check(room.plan is not None and room.plan.title == "two" and "Dan Dunn" in room.plan.names, "a plan saved during the race is picked up within 30 s")


def main():
    tests = [(n, f) for n, f in list(globals().items()) if n.startswith("test_") and callable(f)]
    bad = 0
    for name, fn in tests:
        try:
            fn()
        except Exception:  # noqa: BLE001
            import traceback
            bad += 1
            print(f"FAIL {name} raised")
            traceback.print_exc()
    shutil.rmtree(TMP, ignore_errors=True)
    print("ALL OK" if not fails and not bad else f"{fails} checks failed, {bad} tests raised")
    sys.exit(1 if fails or bad else 0)


if __name__ == "__main__":
    main()
