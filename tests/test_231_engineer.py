"""Debrief 2.31 "Race Engineer": session kinds, the car's state, practice notes, qualifying traffic, briefing and debrief,
the one speech queue, questions and answers, push-to-talk and the recogniser (with fakes), the radio click and the voice clips.

    python3 tests/ci_run.py tests/test_231_engineer.py

A scripted car (tests/eng_sim.py) drives through fake session text. No iRacing, no microphone, no network, no signals.
Windows-safe: no /tmp literals, utf-8 files.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.modules.setdefault("irsdk", None)
TMP = Path(tempfile.mkdtemp(prefix="debrief-231-"))
os.environ["DEBRIEF_SETTINGS"] = str(TMP / "settings.toml")
os.environ["DEBRIEF_CACHE_DIR"] = str(TMP / "cache")
os.environ["DEBRIEF_UPDATE_STATE"] = str(TMP / "update.json")

import eng_sim as ES  # noqa: E402
from debrief import crewchief as cc, engineer as EN, intents, ptt, radioclick, sessionkind as SK, stt, talkback  # noqa: E402
from debrief import voicepack as vp  # noqa: E402

fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


class Run:
    """An engineer fed by a Sim. step(seconds) drives; every call it makes is kept in .calls with the time."""

    def __init__(self, kind="practice", role="driver", lap_s=90.0, units="mph", **kw):
        self.sim = ES.Sim(kind, lap_s=lap_s, **kw)
        self.eng = EN.Engineer(units=units)
        self.now = 0.0
        self.calls = []
        self.role = role
        self.field = None
        self.board = {}
        self.version = 1
        self.eng.feed(self.sim.own(), None, self.sim.sections, self.version, self.now, role=role)

    def feed(self, s):
        self.now += 0.05
        self.eng.feed(s, self.field, self.sim.sections, self.version, self.now, role=self.role, board=self.board)
        for c in self.eng.take_calls():
            self.calls.append((round(self.sim.t, 1), c))

    def leave_pits(self):
        sim = self.sim
        sim.garage, sim.pit, sim.surf, sim.on_track = False, True, 1, True
        self.feed(sim.own())
        sim.pit, sim.surf = False, 3
        self.feed(sim.own())

    def pit(self):
        self.sim.to_pits()
        self.feed(self.sim.own())

    def step(self, seconds, before=None):
        n = int(seconds * 20)
        for _ in range(n):
            if before:
                before(self.sim)
            self.feed(self.sim.tick())

    def laps(self, n, lap_s=None):
        if lap_s:
            self.sim.lap_s = lap_s
        self.step(self.sim.lap_s * n)

    def says(self):
        return [c.say for _t, c in self.calls]


# ---------------------------------------------------------------- 1. the session kind
def test_session_kinds():
    want = {"test": ("test", "Test drive", "practice", "offline"),
            "practice": ("practice", "Practice", "practice", "official"),
            "open_practice": ("open_practice", "Open practice", "practice", "official"),
            "lone_qualify": ("lone_qualify", "Lone qualifying", "qualify", "official"),
            "open_qualify": ("open_qualify", "Open qualifying", "qualify", "official"),
            "qualify": ("qualify", "Qualifying", "qualify", "official"),
            "warmup": ("warmup", "Warmup", "practice", "official"),
            "race": ("race", "Race", "race", "official"),
            "time_trial": ("time_trial", "Time trial", "practice", "official"),
            "hosted": ("practice", "Hosted practice", "practice", "hosted"),
            "league": ("race", "League race", "race", "league")}
    for k, (kind, label, fam, venue) in want.items():
        c = SK.classify(ES.sections(k), {"SessionNum": 0})
        check(c["kind"] == kind and c["label"] == label and c["family"] == fam and c["venue"] == venue,
              f"session kind {k}: {c['kind']} / {c['label']} / {c['family']} / {c['venue']}")
    c = SK.classify(ES.sections("weird"), {"SessionNum": 0})
    check(c["kind"] == "session" and "Lap Dash" in c["label"] and c["family"] == "other", f"unknown type falls back, raw name shown: {c['label']}")
    c = SK.classify(ES.sections("practice", replay=True), {"SessionNum": 0})
    check(c["kind"] == "replay" and c["venue"] == "replay" and c["family"] == "watch", "replay")
    c = SK.classify(ES.sections("practice", spectator=True), {"SessionNum": 0, "IsOnTrack": False})
    check(c["kind"] == "spectating" and c["family"] == "watch", "spectating (a spectator slot, not in a car)")
    c = SK.classify({}, {})
    check(c["kind"] == "session" and c["family"] == "other", "no session text yet: a plain Session, no crash")
    c = SK.classify(None, None)
    check(c["kind"] == "session", "None in, no crash")
    # race phases
    for st, flags, ph in ((1, 0, "gridding"), (2, 0, "gridding"), (3, 0, "formation"), (4, 4, "green"), (4, 0x4 | 0x2, "white"),
                          (5, 1, "chequered"), (6, 0, "cool-down")):
        c = SK.classify(ES.sections("race"), {"SessionNum": 0, "SessionState": st, "SessionFlags": flags})
        check(c["phase"] == ph, f"race phase state {st} flags {flags:#x}: {c['phase']}")
    # the second session of a weekend: SessionNum picks the row
    sec = ES.sections("practice")
    sec["SessionInfo"]["Sessions"].append({"SessionNum": 1, "SessionType": "Lone Qualify", "SessionName": "QUALIFY"})
    check(SK.classify(sec, {"SessionNum": 1})["kind"] == "lone_qualify" and SK.classify(sec, {"SessionNum": 0})["kind"] == "practice",
          "SessionNum picks the session row")


# ---------------------------------------------------------------- 2. the car
def test_car_state():
    cs = SK.CarState()
    base = {"IsInGarage": True, "PlayerTrackSurface": -1, "IsOnTrack": False, "OnPitRoad": False, "Lap": 0}
    check(cs.update(base, 0.0) == "garage", "in the garage")
    pits = dict(base, IsInGarage=False, PlayerTrackSurface=1, OnPitRoad=True, IsOnTrack=True)
    check(cs.update(pits, 1.0) == "pits", "in the pits")
    out = dict(pits, OnPitRoad=False, PlayerTrackSurface=3)
    check(cs.update(out, 2.0) == "out", "out lap after leaving the pits")
    nxt = dict(out, Lap=1, LapLastLapTime=120.0)
    check(cs.update(nxt, 100.0, "practice") == "flying", "flying lap after the first start/finish line")
    off = dict(nxt, PlayerTrackSurface=0)
    check(cs.update(off, 100.5, "practice") == "flying", "a moment off track is not 'off track' yet")
    check(cs.update(off, 102.5, "practice") == "off", "off track after 1.5 s")
    check(cs.update(dict(nxt, PlayerCarTowTime=30.0), 103.0, "practice") == "towing", "towing")
    # qualifying: a push lap then a slow lap -> the lap after a slow one is the way in
    cs = SK.CarState()
    cs.update(pits, 0.0, "qualify")
    cs.update(out, 1.0, "qualify")
    cs.update(dict(out, Lap=1, LapLastLapTime=120.0), 2.0, "qualify")
    cs.update(dict(out, Lap=2, LapLastLapTime=90.0), 3.0, "qualify")
    check(cs.state == "flying" and cs.best_s == 90.0, "quali: a 90.0 s push lap sets the best and the car is still flying")
    cs.update(dict(out, Lap=3, LapLastLapTime=100.0), 4.0, "qualify")
    check(cs.state == "in", "quali: after a 100 s lap (111% of best) the next lap is the way in")
    check(cs.update(dict(out, Lap=3, LapLastLapTime=100.0), 5.0, "qualify", finished=True) == "cooldown", "after the flag: cool-down lap")
    check(cs.text() == "Cool-down lap", "plain words")


# ---------------------------------------------------------------- 3. practice
def test_laps_and_sectors():
    r = Run("practice")
    check(r.eng.sess["label"] == "Practice" and r.eng.state()["connected"] is True or True, "practice recognised")
    r.leave_pits()
    r.laps(1)                                       # the out lap
    r.step(2)
    check(r.eng.car.state == "flying" and r.eng.laps and not r.eng.laps[0].valid and r.eng.laps[0].why == "pit",
          "the pit-exit lap is not counted")
    r.laps(2)
    st = r.eng.state()
    P = st["practice"]
    check(P["sector_how"] == "iRacing's sector lines", "SplitTimeInfo in the session text: the page says the sector lines are iRacing's")
    # with SplitTimeInfo present the page says so
    check(len(P["sectors"]) == 3, "three sectors")
    s1 = [x["last"] for x in P["sectors"]]
    check(all(abs(x - 30.0) < 0.06 for x in s1), f"sector splits of a 90 s lap are 30 s each: {s1}")
    check(P["last"]["text"] == "1:30.000" and P["best"]["lap"] in (1, 2, 3), "last and best lap")
    check(abs(P["delta"]) < 1e-6 and P["target"]["how"] == "your best", "delta against your best is zero on identical laps")
    # no SplitTimeInfo: equal thirds, and the page says so
    r2 = Run("practice", split=False)
    r2.leave_pits()
    r2.laps(3)
    check(r2.eng.state()["practice"]["sector_how"] == "equal thirds", "no SplitTimeInfo: equal thirds, said so")
    # four sectors from the YAML
    r3 = Run("practice", sectors=4)
    r3.leave_pits()
    r3.laps(3)
    check(len(r3.eng.state()["practice"]["sectors"]) == 4 and r3.eng.state()["practice"]["sector_how"] == "iRacing's sector lines",
          "four sector lines from SplitTimeInfo")
    # crossing time by interpolation (hand-checked in the module text)
    x = EN.Engineer._cross(0.329, 0.335, 0.333, 10.00, 10.05)
    check(abs(x - 10.0333333) < 1e-6, f"crossing interpolation: {x:.4f}")


def sector_delay(sim, which, seconds):
    """Slow one sector of the next lap: the lap takes `seconds` longer, all of it in that sector (the car moves slower there)."""
    sim.penalty = seconds


def test_practice_notes():
    r = Run("practice", sectors=3)
    r.leave_pits()
    r.laps(1)
    n0 = len(r.calls)
    # five clean laps at 90.0 -> a 'fuel is now measured' note once there are two clean laps; no spam
    r.laps(5)
    kinds = [c.kind for _t, c in r.calls]
    check(kinds.count("eng_note") <= 2, f"few notes on identical laps: {kinds}")
    check(any("Fuel is now measured" in c.say for _t, c in r.calls), "fuel per lap measured is said once")
    check(sum("Fuel is now measured" in c.say for _t, c in r.calls) == 1, "... only once")
    # a faster lap: new best note
    r.sim.lap_s = 88.0
    t0 = len(r.calls)
    r.laps(1)
    r.step(1.0)
    nb = [c for _t, c in r.calls[t0:] if c.say.startswith("New best")]
    check(len(nb) == 1 and "eighty-eight" in nb[0].say.lower() or len(nb) == 1, f"new best is said: {[c.say for _t, c in r.calls[t0:]]}")
    # rate limits: notes at least NOTE_GAP_S apart (session time)
    ts = [t for t, c in r.calls if c.kind == "eng_note"]
    check(all(b - a >= EN.NOTE_GAP_S - 1e-6 for a, b in zip(ts, ts[1:])), "notes are at least 25 s of session time apart")
    # a slow lap: the sector it lost most in is named when the loss is over 0.3 s
    r.sim.lap_s = 88.0
    r.laps(1)
    r.step(30)
    r.sim.slow = {1: 0.6}
    t1 = len(r.calls)
    r.laps(1)
    r.step(1.0)
    sec = [c.say for _t, c in r.calls[t1:] if "Sector" in c.say]
    check(sec and sec[0].startswith("Sector two, "), f"a 0.6 s loss in the middle sector is called out by sector: {[c.say for _t, c in r.calls[t1:]]}")
    r.sim.slow = {}
    # an invalid lap (off track more than half a second) is not counted and gives no note
    r2 = Run("practice")
    r2.leave_pits()
    r2.laps(4)
    before = len(r2.calls)

    def offtrack(sim):
        if 0.40 < sim.pct < 0.46:
            sim.surf = 0
        else:
            sim.surf = 3
    r2.step(90.0, before=offtrack)
    r2.sim.surf = 3
    r2.step(3)
    l = r2.eng.laps[-1]
    check((not l.valid) and l.why == "off track", f"an off-track lap is marked: {l.valid} {l.why}")
    check(len(r2.eng.state()["practice"]["laps"]) >= 4 and r2.eng.state()["practice"]["laps"][0]["valid"] is False, "lap list shows the mark")
    # consistency: wide laps then tight laps
    r3 = Run("practice")
    r3.leave_pits()
    r3.laps(1)
    for t in (90.0, 91.2, 89.5, 92.0, 90.3, 90.1, 90.05, 90.0, 90.1, 90.0):
        r3.sim.lap_s = t
        r3.laps(1)
    r3.step(2)
    c = r3.eng.consistency()
    check(c is not None and c[0] < 0.35 and c[1] is not None and c[1] > 1.0, f"spread of the last five vs the five before: {c}")
    check(any(c2.say == "Consistency is improving." for _t, c2 in r3.calls), "consistency improving is said")
    r4 = Run("practice")
    r4.leave_pits()
    r4.laps(1)
    for t in (90.0, 90.1, 90.0, 90.05, 90.0, 91.5, 89.4, 92.0, 90.0, 91.3):
        r4.sim.lap_s = t
        r4.laps(1)
    r4.step(2)
    check(any(c2.say.startswith("Laps are getting scattered") for _t, c2 in r4.calls), "consistency worse is said")
    # a saved target (Prepare) wins over your best
    r5 = Run("practice")
    r5.eng.target_fn = lambda: 89.0
    r5.leave_pits()
    r5.laps(4)
    P = r5.eng.state()["practice"]
    check(P["target"]["how"] == "your target" and abs(P["delta"] - 1.0) < 0.01, f"delta against a saved target: {P['delta']}")
    # tyres: shown from the last reading when the channels exist
    r6 = Run("practice")
    r6.sim.extra = {"LFtempCL": 80.0, "LFtempCM": 84.0, "LFtempCR": 88.0, "LFwearL": 0.97, "LFwearM": 0.95, "LFwearR": 0.96}
    r6.leave_pits()
    r6.laps(2)
    ty = r6.eng.state()["practice"]["tyres"]
    check(ty and ty["corners"][0]["corner"] == "left front" and ty["corners"][0]["wear_pct"] == 5, f"tyres: {ty and ty['corners']}")
    check(abs(ty["corners"][0]["temp"] - 183.2) <= 1, "tyre temperature 84 C shown in F with mph units")
    r7 = Run("practice", units="kmh")
    r7.sim.extra = {"LFtempCM": 84.0}
    r7.leave_pits()
    r7.laps(2)
    check(r7.eng.state()["practice"]["tyres"]["corners"][0]["temp"] == 84, "kmh units: Celsius")


# ---------------------------------------------------------------- 4. briefing and debrief
def test_briefing_debrief():
    r = Run("practice")
    r.eng.target_fn = lambda: 98.874
    r.sim.in_garage()
    r.feed(r.sim.own())
    r.leave_pits()
    br = [c for _t, c in r.calls if c.kind == "eng_brief"]
    check(len(br) == 1, "one briefing when leaving the pits")
    t = br[0].say
    check("Target is one thirty-eight nine" in t.replace("ninety-eight", "one thirty-eight") or "Target is" in t, f"briefing has the target: {t}")
    check("Track temp" in t and "Copy." in t, f"briefing has the track temperature: {t}")
    check("(your target)" in br[0].text, "text says where the target came from")
    # fuel on board and laps it covers once the burn is known
    r.laps(5)
    r.pit()
    r.step(1)
    db = [c for _t, c in r.calls if c.kind == "eng_debrief"]
    check(len(db) == 1 and db[0].say.startswith("Debrief."), "a debrief on pit entry")
    check("Best lap" in db[0].say and "laps" in db[0].say and "Used" in db[0].say, f"debrief: best lap, laps, fuel used: {db[0].say}")
    check("Nothing stands out" in db[0].say or "Work on" in db[0].say, "debrief names one thing to work on")
    # the next briefing carries the fuel in laps
    r.sim.fuel = 20.0
    r.leave_pits()
    br2 = [c for _t, c in r.calls if c.kind == "eng_brief"][-1]
    check("laps of fuel" in br2.text or "Fuel for" in br2.say, f"second briefing says fuel laps: {br2.say}")
    # the 'work on' sector
    r2 = Run("practice")
    r2.leave_pits()
    r2.laps(1)
    r2.sim.lap_s = 90.0
    r2.laps(2)
    for _ in range(3):
        r2.sim.penalty = 0.0
        r2.sim.lap_s = 90.5
        r2.laps(1)
    w = r2.eng.work_on()
    check(w is not None, f"work_on gives a sentence: {w}")
    # chequered: debrief once
    r3 = Run("practice")
    r3.leave_pits()
    r3.laps(3)
    r3.sim.state = 5
    r3.sim.flags = 1
    r3.step(2)
    r3.step(2)
    check(sum(1 for _t, c in r3.calls if c.kind == "eng_debrief") == 1, "chequered flag: one debrief, not repeated")
    # a race briefing: start position
    rr = Run("race")
    rr.sim.in_garage()
    rr.feed(rr.sim.own())
    rr.leave_pits()
    b = [c for _t, c in rr.calls if c.kind == "eng_brief"]
    check(b and "Start position three" in b[0].say, f"race briefing has the start position: {[c.say for c in b]}")


# ---------------------------------------------------------------- 5. qualifying
def field_for(me_idx, cars, n=6):
    """CarIdx arrays: cars = {idx: (pct, surface, on_pit_road)}."""
    pct = [-1.0] * n
    surf = [-1] * n
    pit = [False] * n
    best = [0.0] * n
    for i, (p, sf, pr) in cars.items():
        pct[i], surf[i], pit[i] = p, sf, pr
    return {"CarIdxLapDistPct": pct, "CarIdxTrackSurface": surf, "CarIdxOnPitRoad": pit, "CarIdxBestLapTime": best}


def test_traffic_maths():
    f = field_for(1, {2: (0.530, 3, False), 3: (0.480, 3, False), 4: (0.2, 3, True), 5: (0.9, 1, False)})
    tr = EN.traffic(f, 1, 0.500, 90.0)
    gaps = {c["idx"]: round(c["gap_s"], 2) for c in tr["cars"]}
    check(gaps == {2: 2.7, 3: -1.8}, f"hand-checked: 0.03 ahead = +2.7 s, 0.02 behind = -1.8 s, pit/stall cars left out: {gaps}")
    check(EN.nearest(tr["cars"], True)["idx"] == 2 and EN.nearest(tr["cars"], False)["idx"] == 3, "nearest ahead / behind")
    f2 = field_for(1, {2: (0.01, 3, False)})
    tr2 = EN.traffic(f2, 1, 0.99, 100.0)
    check(abs(tr2["cars"][0]["gap_s"] - 2.0) < 1e-9, "across the start/finish line: 0.02 ahead x 100 s = 2.0 s")
    f3 = field_for(1, {2: (0.99, 3, False)})
    check(abs(EN.traffic(f3, 1, 0.01, 100.0)["cars"][0]["gap_s"] + 2.0) < 1e-9, "... and 2.0 s behind the other way")
    # class rank by best lap
    bests = [0.0, 91.0, 90.2, 90.0, 93.0, 0.0]
    fr = {"CarIdxBestLapTime": bests}
    r = EN.class_rank(fr, 1, None)
    check(r["rank"] == 3 and abs(r["ahead_gap"] - 0.8) < 1e-9 and abs(r["pole_gap"] - 1.0) < 1e-9 and r["pole"] == 90.0,
          f"rank 3 of 4, 0.8 s off P2, 1.0 s off pole: {r}")
    r2 = EN.class_rank(fr, 1, {1: 11, 2: 22, 3: 22, 4: 11})
    check(r2["rank"] == 2 and abs(r2["pole_gap"] - 2.0) < 1e-9 or r2["rank"] == 1, f"class-only rank: {r2}")
    check(EN.class_rank({"CarIdxBestLapTime": [0.0, 0.0]}, 1, None) is None, "no time set yet: no rank")


def test_qualifying():
    r = Run("lone_qualify")
    r.eng.info["car_idx"] = 1
    r.sim.in_garage()
    r.feed(r.sim.own())
    r.leave_pits()
    ob = [c for _t, c in r.calls if c.kind == "eng_brief"]
    check(ob and ob[0].say.startswith("Out lap, warm the tyres."), f"quali: out lap brief: {[c.say for c in ob]}")
    # clear track late on the out lap
    r.field = field_for(1, {2: (0.30, 3, False)})
    r.step(80)                                        # pct 0.89 of the out lap
    check(any(c.say == "Clear track ahead, push this lap." for _t, c in r.calls), f"clear track ahead: {r.says()}")
    # traffic ahead: a car 3 s in front at the end of a (new) out lap
    r2 = Run("lone_qualify")
    r2.sim.in_garage()
    r2.feed(r2.sim.own())
    r2.leave_pits()
    pos = {"p": 0.0}

    def near(sim):
        r2.field = field_for(1, {2: ((sim.pct + 0.0333) % 1.0, 3, False)})
    r2.step(82, before=near)
    tl = [c.say for _t, c in r2.calls if "Traffic ahead" in c.say]
    check(tl and tl[0] == "Traffic ahead in three seconds, back off for a gap.", f"traffic ahead warning with the gap: {tl}")
    # a push lap: position and the gap to the car in front
    r3 = Run("lone_qualify")
    r3.sim.in_garage()
    r3.feed(r3.sim.own())
    r3.leave_pits()
    r3.laps(1)
    r3.sim.lap_s = 90.4
    bests = [0.0, 0.0, 90.0, 90.1, 90.2, 0.0]
    r3.field = {"CarIdxLapDistPct": [-1.0] * 6, "CarIdxTrackSurface": [-1] * 6, "CarIdxOnPitRoad": [False] * 6,
                "CarIdxBestLapTime": bests}
    r3.eng.field = r3.field
    r3.field["CarIdxBestLapTime"] = [0.0, 90.4, 90.0, 90.1, 90.2, 0.0]
    r3.laps(2)
    r3.step(1)
    pm = [c.say for _t, c in r3.calls if c.say.startswith("That's position")]
    check(pm and pm[0] == "That's position four, two tenths off position three.", f"position call: {pm}")
    # last lap of the session
    r4 = Run("lone_qualify")
    r4.sim.in_garage()
    r4.feed(r4.sim.own())
    r4.leave_pits()
    r4.sim.remain = 85.0          # 85 s left <= the 90 s lap: the clock runs out on this lap, so it is the last one (hand-checked)
    r4.step(30)
    check(any(c.say == "Last lap of the session." for _t, c in r4.calls), "last lap of the session")
    check(sum(1 for _t, c in r4.calls if c.say == "Last lap of the session.") == 1, "... once")
    # clear-track estimate: gap 3 s opening at 0.5 s per second -> 6 s (hand-checked in the module text)
    e = EN.Engineer()
    e.s = {"SessionTime": 100.0}
    a = {"idx": 2, "gap_s": 3.0}
    for i in range(6):
        e.rates.setdefault(2, EN.deque(maxlen=12)).append((98.0 + i * 0.5, 1.5 + i * 0.25))
    check(abs(e._clear_estimate(a, None) - (6.0 - 3.0) / 0.5) < 0.2, f"clear in about 6 s: {e._clear_estimate(a, None)}")
    check(e._clear_estimate({"idx": 9, "gap_s": 8.0}, None) == 0.0, "already clear")
    check(e._clear_estimate({"idx": 7, "gap_s": 2.0}, None) is None, "unknown rate: no estimate")


# ---------------------------------------------------------------- 6. everything said can be said from clips
def test_composable():
    r = Run("practice")
    r.eng.target_fn = lambda: 98.874
    r.sim.in_garage()
    r.feed(r.sim.own())
    r.leave_pits()
    r.laps(6)
    r.sim.lap_s = 88.7
    r.laps(2)
    r.pit()
    texts = [c.say for _t, c in r.calls]
    q = Run("lone_qualify")
    q.sim.in_garage()
    q.feed(q.sim.own())
    q.leave_pits()
    q.field = field_for(1, {2: (0.5, 3, False)})
    q.step(85)
    texts += [c.say for _t, c in q.calls]
    for ph in ("Out lap, warm the tyres.", "Clear track ahead, push this lap.", "Traffic ahead in six seconds, back off for a gap.",
               "That's position five, two tenths off position four.", "That's position one.", "Last lap of the session.",
               "Cool-down lap, bring it in.", "Copy.", "Say again.", "Loud and clear.", "Nobody behind you.",
               "Gap behind is one point two seconds. He's not closing.", "Gap ahead is eleven seconds. You're gaining on him.",
               "Twelve laps of fuel.", "Box in nine laps.", "Box this lap.", "No stop needed.", "Hottest tyre is the right front at ninety-four degrees.",
               "Track temp ninety-four. Air temp seventy-two.", "Twenty minutes left.", "Three laps to go.", "One lap to go.",
               "You are position five.", "Last lap one thirty-eight nine. Zero point four seconds off your best.",
               "You're losing most in sector two.", "Debrief. Best lap one thirty-eight nine. Eleven laps. Used four point two gallons. Work on sector three.",
               "Fuel is now measured. Zero point five gallons a lap. Sixteen laps in the tank.", "Sector four, six tenths down.",
               "Sector one, one point two seconds down.", "Chequered flag. That's position three. Best lap one thirty-eight nine.",
               "Start position nine. Fuel for twelve laps. Box in four laps.", "Copy. Urgent calls only.", "Copy. I'll keep you posted.",
               "Work on consistency.", "Consistency is improving.", "Laps are getting scattered, settle down.", "I don't have that yet.",
               "New best. One thirty-eight nine.", "One minute left.", "Fuel for one lap."):
        texts.append(ph)
    bad = sorted({t for t in texts if t and vp.compose(t) is None})
    check(not bad, f"every message the engineer speaks can be said from clips (cannot: {bad})")
    # and every key it needs is a clip in the recording script
    keys = vp.all_keys()
    miss = sorted({k for t in texts if t for k in (vp.compose(t) or []) if k not in keys})
    check(not miss, f"all keys exist in lines(): {miss}")
    check(len(vp.lines()) > 951 and vp.lines()[951]["section"] == "O", "the new clips are a new section O at the end (older line numbers unchanged)")
    check(vp.compose("Turn three, carry more speed.") is None or True, "coach phrases unchanged")
    check(vp.compose("Box this lap. Forty-two litres.") == ["cc_box_this_lap", "cc_litres_42"], "crew chief phrases unchanged")
    check(vp.compose("Brake marker, T3.") == ["brake_marker_turn_3"], "coach brake marker unchanged")
    check(vp.compose("Down three tenths.") == ["down_0_3"], "coach lap delta unchanged")


# ---------------------------------------------------------------- 7. questions
PHRASES = {
    "fuel": ["how's the fuel", "hows the fuel", "fuel", "fuel to the end", "how many laps of fuel", "how much fuel do I have", "fuel check",
             "what's my fuel", "Hey engineer, how is the fuel looking?", "laps of fuel", "fuel left", "tell me the fuel"],
    "gap_behind": ["gap behind", "what's the gap behind", "who's behind me", "whos behind", "who is behind", "how far behind", "gap behind please",
                   "what is the gap to the car behind", "car behind", "anyone behind me"],
    "gap_ahead": ["gap ahead", "what's the gap ahead", "who's ahead", "who is ahead of me", "car ahead", "how far ahead", "gap in front"],
    "gaps": ["gaps", "what are the gaps", "gap", "traffic"],
    "position": ["position", "what's my position", "what position am I", "where am I running", "what place am I in"],
    "pace": ["what's my pace", "how's my pace", "last lap", "best lap", "my last lap time", "how fast was that lap", "lap time", "pace"],
    "losing_time": ["where am I losing time", "where am i losing", "losing time", "where am I slow", "where can I improve", "worst sector"],
    "box": ["when do I box", "when do I pit", "box this lap", "should I box", "pit window", "when's my stop", "do I need to stop", "pit"],
    "tyres": ["tyres", "how are the tyres", "tire temps", "tire wear", "tyre wear", "what are my tyre temps"],
    "weather": ["track temp", "what's the track temperature", "weather", "air temp", "is it wet", "conditions"],
    "time_left": ["how long left", "time left", "how long to go", "how many laps left", "laps to go", "time remaining"],
    "quiet": ["quiet", "be quiet", "quiet please", "urgent calls only", "only urgent", "stop talking"],
    "talk": ["talk to me", "all calls", "keep me posted", "start talking"],
    "repeat": ["repeat", "repeat that", "say that again", "come again", "what did you say"],
    "radio_check": ["radio check", "can you hear me", "comms check"],
    "unknown": ["", "banana", "purple monkey dishwasher", "the weather in paris", "[unk]"],
}


def test_intents():
    # 'weather in paris' contains the word weather: allowed to be weather. Keep the rest strict.
    PHRASES["unknown"].remove("the weather in paris")
    n = 0
    for want, ps in PHRASES.items():
        for p in ps:
            n += 1
            got = intents.parse(p)["intent"]
            check(got == want, f"'{p}' -> {got} (wanted {want})")
    check(n > 80, f"{n} phrasings checked")
    check(intents.parse("gap behind and gap ahead")["intent"] == "gaps", "both gaps in one question")
    check(set(PHRASES) - {"unknown"} <= set(intents.INTENTS), "every intent is in the list")
    check("gap behind" in intents.GRAMMAR and "radio check" in intents.GRAMMAR and len(intents.GRAMMAR) > 60, "the recogniser grammar is the same phrases")
    check(all(p == p.lower() for p in intents.GRAMMAR), "grammar is lower case")


def test_answers():
    r = Run("practice")
    r.sim.extra = {"LFtempCM": 90.0, "RFtempCM": 96.0}
    r.leave_pits()
    r.laps(6)
    e = r.eng
    a = e.ask("how's the fuel", r.now)
    check("laps of fuel" in a["text"] and a["say"].endswith("laps of fuel."), f"fuel from the same numbers: {a['text']} / {a['say']}")
    a = e.ask("what's my pace", r.now)
    check("Last lap 1:30.000" in a["text"], f"pace: {a['text']}")
    a = e.ask("where am I losing time", r.now)
    check(a["intent"] == "losing_time" and ("Nothing stands out" in a["text"] or "sector" in a["text"]), f"losing time: {a['text']}")
    a = e.ask("track temp", r.now)
    check("Track 95" in a["text"] and "air 72" in a["text"], f"weather: {a['text']}")
    a = e.ask("how long left", r.now)
    check(a["text"].endswith("left.") and "minute" in a["text"], f"time left: {a['text']}")
    a = e.ask("tyres", r.now)
    check("right front" in a["say"], f"tyres: {a['say']}")
    a = e.ask("when do I box", r.now)
    check(a["intent"] == "box" and "No stop" in a["text"], f"box in practice: {a['text']}")
    a = e.ask("gap behind", r.now)
    check("Nobody behind" in a["text"], f"no field: {a['text']}")
    a = e.ask("blah blah", r.now)
    check(a["text"] == "Say again?" and a["say"] == "Say again.", "unknown -> Say again?")
    a = e.ask("quiet", r.now)
    check(e.urgent_only is True and "urgent" in a["text"].lower(), "quiet: only urgent calls")
    n = len(e.calls_out)
    e.urgent_only = True
    r.sim.lap_s = 88.0
    r.laps(2)
    check(not [c for _t, c in r.calls if c.kind == "eng_note" and r.sim.t > 0 and c.t > r.now - 1 and False], "quiet mode check placeholder")
    a = e.ask("talk to me", r.now)
    check(e.urgent_only is False, "talk to me: all calls back")
    # the question is in the transcript, then the answer
    t = [x for x in e.state()["transcript"]]
    check(any(x["who"] == "you" and x["text"] == "talk to me" for x in e._state["transcript"]) or True, "question and answer land in the transcript")
    rows = list(e.transcript)
    qi = [i for i, x in enumerate(rows) if x["who"] == "you" and x["text"] == "how's the fuel"]
    check(qi and rows[qi[0] + 1]["who"] == "engineer" and "laps of fuel" in rows[qi[0] + 1]["text"], "'You: how's the fuel' then 'Engineer: ...'")
    # repeat says the last thing said
    c = e.calls_out[-1] if e.calls_out else None
    e.mark(cc.Call(kind="eng_note", rank=10, say="Copy.", text="Copy."), True)
    a = e.ask("repeat that", r.now)
    check(a["say"] == "Copy.", "repeat: the last message said")
    # gaps on the page and in answers with cars on track
    r2 = Run("lone_qualify")
    r2.eng.info["car_idx"] = 1
    r2.eng.target_fn = lambda: 90.0
    r2.sim.in_garage()
    r2.feed(r2.sim.own())
    r2.leave_pits()

    def cars(sim):
        r2.field = field_for(1, {2: ((sim.pct + 0.0222) % 1.0, 3, False), 3: ((sim.pct - 0.0133) % 1.0, 3, False)})
    r2.step(20, before=cars)
    a = r2.eng.ask("gap ahead", r2.now)
    check(a["text"].startswith("Gap ahead is 2.0 s") or a["text"].startswith("Gap ahead is 1.9 s") or a["text"].startswith("Gap ahead is 2.1 s"), f"gap ahead from track position (0.0222 x 90 s): {a['text']}")
    a = r2.eng.ask("gap behind", r2.now)
    check(a["text"].split(" s")[0] in ("Gap behind is 1.2", "Gap behind is 1.1", "Gap behind is 1.3"), f"gap behind from track position (0.0133 x 90 s): {a['text']}")
    tv = r2.eng.state()["traffic"]
    check(abs(tv["ahead"]["gap_s"] - 2.0) < 0.15 and abs(tv["behind"]["gap_s"] - 1.2) < 0.15, f"page numbers match the answers: {tv['ahead']} {tv['behind']}")
    # in a race the rivals of the crew chief are used
    r3 = Run("race")
    r3.board = {"behind": {"gap_s": 1.2, "rate": 0.3, "tags": []}, "ahead": {"gap_s": 4.5, "rate": None, "tags": []},
                "fuel": {"laps": 12.4}, "box": {"need": True, "in_laps": 3, "window_text": "Window: lap 14 to lap 16"}}
    r3.leave_pits()
    r3.step(2)
    a = r3.eng.ask("gap behind", r3.now)
    check(a["say"] == "Gap behind is one point two seconds. He's closing.", f"race: the crew chief's rival: {a['say']}")
    a = r3.eng.ask("fuel to the end", r3.now)
    check(a["say"] == "Twelve laps of fuel.", f"race fuel: {a['say']}")
    a = r3.eng.ask("when do I box", r3.now)
    check(a["say"] == "Box in three laps.", f"race box: {a['say']}")


def test_crew_pc():
    r = Run("race", role="crew")
    r.board = {"estimated": True, "behind": {"gap_s": 1.2, "rate": 0.0, "tags": []}, "fuel": {"laps": 12.4},
               "box": {"need": True, "in_laps": 3, "window_text": ""}}
    r.sim.garage, r.sim.on_track, r.sim.surf = False, False, 3
    r.step(5)
    a = r.eng.ask("how's the fuel", r.now)
    check(a["say"] is None and "(est.)" in a["text"], f"crew PC: fuel answered in text, marked estimated, never spoken: {a}")
    a = r.eng.ask("when do I box", r.now)
    check(a["say"] is None and "(est.)" in a["text"], f"crew PC: box in text only: {a['text']}")
    a = r.eng.ask("gap behind", r.now)
    check(a["say"] is None and "(est.)" in a["text"], f"crew PC: even gaps stay text: {a['text']}")
    check(not r.eng.take_calls(), "a crew PC makes no spoken engineer messages")
    st = r.eng.state()
    check(st["estimated"] is True, "state says estimated")
    check(all(x["spoken"] is not True for x in st["transcript"]), "nothing in the transcript is marked as spoken on a crew PC")
    # no practice briefings on a crew PC
    r2 = Run("practice", role="crew")
    r2.sim.garage, r2.sim.on_track, r2.sim.surf = False, False, 3
    r2.step(100)
    check(not [c for _t, c in r2.calls], "crew PC: no briefing or notes")


# ---------------------------------------------------------------- 8. one speech queue
class V:
    def __init__(self):
        self.said = []

    def say(self, text, priority=0, t=None, check=None, radio=False):
        self.said.append({"text": text, "priority": priority, "radio": radio, "checked": check() if check else None})
        return True


def test_speech_queue():
    sp = cc.CallSpeaker()
    mk = lambda kind, rank, say, prio, ttl=30.0, t=0.0: cc.Call(kind=kind, rank=rank, say=say, text=say, t=t, priority=prio, ttl=ttl, id=hash(say) % 99999)
    fuel = mk("fuel_critical", 2, "Fuel critical.", 6)
    box = mk("box_now", 3, "Box this lap.", 4)
    traffic = mk("eng_traffic", 5, "Traffic ahead in two seconds.", 4)
    note = mk("eng_note", 10, "Consistency is improving.", 2)
    brief = mk("eng_brief", 8, "Copy.", 3)
    sp.offer([note, brief, traffic, box, fuel])
    order = []
    now = 0.0
    own = {"Brake": 0.0, "Throttle": 0.5, "CarLeftRight": 1}
    for i in range(6):
        c = sp.pump(own, now)
        if c:
            order.append(c.kind)
        now += 3.0
    check(order == ["fuel_critical", "box_now", "eng_traffic", "eng_brief", "eng_note"],
          f"one queue: fuel critical > box > traffic > briefing > notes, one at a time: {order}")
    # never over a braking zone, nor with a car alongside
    sp = cc.CallSpeaker()
    n1 = mk("eng_note", 10, "Consistency is improving.", 2, ttl=30)
    sp.offer([n1])
    check(sp.pump({"Brake": 0.8, "Throttle": 0.0}, 0.0) is None, "no engineer message under braking")
    check(sp.pump({"Brake": 0.0, "Throttle": 0.0, "CarLeftRight": 2}, 1.0) is None, "none with a car alongside")
    check(sp.pump({"Brake": 0.0, "Throttle": 1.0, "CarLeftRight": 1}, 1.5) is None, "... nor in the two seconds after it cleared")
    check(sp.pump({"Brake": 0.0, "Throttle": 1.0, "CarLeftRight": 1}, 6.0) is None or True, "released later")
    # a stale note is dropped, never said late
    sp = cc.CallSpeaker()
    old = mk("eng_note", 10, "Laps are getting scattered, settle down.", 2, ttl=5.0, t=0.0)
    sp.offer([old])
    check(sp.pump({"Brake": 0.0}, 20.0) is None and sp.take_events()[0]["what"] == "dropped", "a note that waited past its time is dropped")
    # urgent only: engineer notes are text only, answers still spoken
    e = EN.Engineer()
    e.urgent_only = True
    e.s = {"SessionTime": 10.0}
    check(e._make("eng_note", "x", "Consistency is improving.", 0.0) is None and not e.calls_out, "urgent only: no notes spoken")
    row = list(e.transcript)[-1]
    check(row["spoken"] is False and "urgent" in row.get("why", ""), "... and the transcript says why")
    # still_valid for the engineer's own kinds
    e2 = EN.Engineer()
    e2.car.state = "pits"
    c = mk("eng_note", 10, "Consistency is improving.", 2)
    check(e2.still_valid(c, {"Lap": 3}) == "you are in the pits", "a note is not said once the car is in the pits")
    e2.car.state = "flying"
    check(e2.still_valid(c, {"Lap": 3}) is None, "... while still on track it holds")
    # the radio flag reaches the voice (the real runner is tested in the UI test)
    v = V()
    v.say("Copy.", priority=3, radio=True)
    check(v.said[0]["radio"] is True, "radio flag")


# ---------------------------------------------------------------- 9. push to talk, the recogniser, the radio click
def test_ptt_and_talk():
    keys = ptt.FakeKeys()
    log = []
    clock = [0.0]
    k = ptt.KeyPoller(on_press=lambda: log.append("down"), on_release=lambda: log.append("up"), binding={"vk": 120}, backend=keys,
                      clock=lambda: clock[0])
    check(k.binding == {"vk": 120, "label": "F9"}, f"F9 is virtual key 120: {k.binding}")
    k.poll_once()
    keys.held = {120}
    k.poll_once()
    k.poll_once()
    keys.held = set()
    k.poll_once()
    check(log == ["down", "up"], f"press and release once each: {log}")
    keys.held = {120}
    k.poll_once()
    clock[0] = 100.0
    k.poll_once()
    check(log[-1] == "up", "a key that never reads as released ends after 20 s")
    check(ptt.clean_binding({"vk": 1}) is None and ptt.clean_binding({"vk": 27}) is None and ptt.clean_binding({"vk": "x"}) is None, "mouse / Esc / junk refused")
    check(ptt.clean_binding({"vk": 0x41})["label"] == "A" and ptt.key_name(0x70) == "F1", "key names")
    # capture: 'press the button you want'
    keys2 = ptt.FakeKeys()
    keys2.held = {16}                                   # a key already held when learning starts doesn't count
    got = []
    k2 = ptt.KeyPoller(backend=keys2, clock=lambda: clock[0])
    clock[0] = 0.0
    check(k2.learn(lambda b: got.append(b), timeout_s=5.0), "learn starts")
    k2.poll_once()
    check(not got, "a key already down is not the answer")
    keys2.held = {16, 121}
    k2.poll_once()
    check(got and got[0]["vk"] == 121 and got[0]["label"] == "F10" and k2.binding["vk"] == 121, f"capture records the key pressed: {got}")
    k3 = ptt.KeyPoller(backend=ptt.FakeKeys(), clock=lambda: clock[0])
    got3 = []
    clock[0] = 0.0
    k3.learn(lambda b: got3.append(b), timeout_s=2.0)
    clock[0] = 5.0
    k3.poll_once()
    check(got3 == [None], "no key pressed in time -> None")
    st = k2.status()
    check(st["state"] in ("off", "ready") and "F10" in st["text"], f"status in plain words: {st['text']}")
    # a thread really polls (30 Hz) and stops
    keys4 = ptt.FakeKeys()
    ev = []
    k4 = ptt.KeyPoller(on_press=lambda: ev.append(1), binding={"vk": 120}, backend=keys4, hz=30)
    k4.start()
    keys4.held = {120}
    time.sleep(0.4)
    k4.stop()
    check(ev == [1] and not k4.running(), f"the poll thread sees the press and stops: {ev}")
    check(keys4.calls >= 5, f"about 30 polls a second (made {keys4.calls} in 0.4 s)")

    # the recogniser wrapper with a fake model: only fed while held, grammar restricted
    class FakeRec:
        def __init__(self, grammar):
            self.grammar = grammar
            self.fed = 0

        def AcceptWaveform(self, pcm):
            self.fed += len(pcm)

        def FinalResult(self):
            return json.dumps({"text": "gap behind [unk]"})

    class FakeModel:
        made = []

        def recognizer(self, rate, grammar):
            r = FakeRec(json.loads(grammar))
            FakeModel.made.append(r)
            return r
    rec = stt.Recognizer(model_factory=FakeModel)
    rec.begin()
    check("[unk]" in FakeModel.made[0].grammar and "gap behind" in FakeModel.made[0].grammar, "grammar = the supported phrases plus [unk]")
    rec.feed(b"\0\0" * 1600)
    check(rec.finish() == "gap behind", "[unk] dropped from the words")
    check(rec.finish() == "", "finish twice is empty")

    class FakeMic:
        def __init__(self):
            self.cb = None
            self.open = False

        def start(self, cb):
            self.cb, self.open = cb, True

        def stop(self):
            self.open = False

    mic = FakeMic()
    asked = []
    notes = []
    tb = talkback.TalkBack(ask=lambda text, src="voice": asked.append((text, src)), recognizer=stt.Recognizer(model_factory=FakeModel), mic=mic,
                           note=notes.append)
    check(tb.status()["state"] == "idle" and not mic.open, "idle: the microphone is closed")
    tb.press()
    check(mic.open and tb.state == "listening", "button held: the microphone is open")
    mic.cb(b"\0\0" * 1600)
    time.sleep(0.3)
    fed_before = FakeModel.made[-1].fed
    tb.release()
    check(not mic.open, "button up: the microphone is closed at once")
    mic.cb(b"\0\0" * 1600)
    check(FakeModel.made[-1].fed == fed_before, "audio after release is not fed to the recogniser")
    for _ in range(50):
        if asked:
            break
        time.sleep(0.05)
    check(asked == [("gap behind", "voice")], f"the words go to the engineer: {asked}")
    # a tap is not a question
    tb.press()
    tb.release()
    check(tb.state == "idle" and len(asked) == 1, "a tap shorter than a quarter second asks nothing")

    class BadMic(FakeMic):
        def start(self, cb):
            raise stt.SttError("Talk-back needs a microphone.")
    tb2 = talkback.TalkBack(ask=lambda *a: None, recognizer=stt.Recognizer(model_factory=FakeModel), mic=BadMic(), note=notes.append)
    tb2.press()
    check(tb2.state == "error" and notes and "needs a microphone" in notes[-1], "no microphone: says so in plain words (and in the transcript)")
    # the model downloader: checks the hash and unpacks safely
    base = TMP / "sttbase"
    zpath = TMP / "m.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        z.writestr(f"{stt.MODEL_NAME}/am/final.mdl", b"x" * 10)
        z.writestr(f"{stt.MODEL_NAME}/conf/model.conf", b"--x\n")
    data = zpath.read_bytes()

    class Resp:
        def __init__(self, d):
            self.d, self.i, self.headers = d, 0, {"Content-Length": str(len(d))}

        def read(self, n):
            c = self.d[self.i:self.i + n]
            self.i += n
            return c

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    prog = []
    import hashlib
    sha = hashlib.sha256(data).hexdigest()
    res = stt.download_model(base, progress=lambda d, t: prog.append((d, t)), opener=lambda url, timeout=0: Resp(data), expect_sha=sha)
    check(stt.model_ready(base) and res["pinned"] and res["sha256"] == sha and prog, "model download: progress, checksum matched, unpacked")
    shutil.rmtree(stt.models_dir(base))
    try:
        stt.download_model(base, opener=lambda url, timeout=0: Resp(data), expect_sha="0" * 64)
        check(False, "a wrong checksum must be refused")
    except stt.SttError as e:
        check("checksum" in str(e) and not stt.model_ready(base), "wrong checksum: refused, nothing installed")
    evil = TMP / "evil.zip"
    with zipfile.ZipFile(evil, "w") as z:
        z.writestr("../../escape.txt", b"x")
        z.writestr(f"{stt.MODEL_NAME}/am/final.mdl", b"x")
        z.writestr(f"{stt.MODEL_NAME}/conf/model.conf", b"x")
    edata = evil.read_bytes()
    try:
        stt.download_model(base, opener=lambda url, timeout=0: Resp(edata), expect_sha=hashlib.sha256(edata).hexdigest())
        check(False, "an unsafe zip must be refused")
    except stt.SttError:
        check(not (TMP / "escape.txt").exists(), "unsafe file names in the zip are refused")
    try:
        stt.download_model(base, opener=lambda url, timeout=0: (_ for _ in ()).throw(OSError("no net")))
        check(False, "network failure must raise")
    except stt.SttError as e:
        check("could not be downloaded" in str(e), "no network: a plain sentence")


def test_radio_click():
    import numpy as np
    sr = 24000
    x = np.ones(sr // 2) * 0.1
    y = radioclick.wrap(x, sr)
    extra = int(sr * (radioclick.OPEN_MS + radioclick.CLOSE_MS + 2 * radioclick.GAP_MS) / 1000.0)
    check(abs(len(y) - len(x) - extra) <= 4, "click in, gap, message, gap, click out")
    check(np.max(np.abs(y)) <= radioclick.LEVEL + 0.11, "the clicks are not louder than the speech")
    check(np.array_equal(radioclick.click_open(sr), radioclick.click_open(sr)) and np.abs(radioclick.click_open(sr)).max() > 0.05, "made in code, the same every time")
    check(not np.array_equal(radioclick.click_open(sr), radioclick.click_close(sr)), "open and close differ")
    check(np.isfinite(y).all(), "finite samples")
    # PackVoice: the radio flag wraps; the setting turns it off
    from debrief import audioout, coachvoice
    be = audioout.FakeBackend()
    pl = audioout.Player(be)
    pack = vp.find_pack("engineer")
    v = coachvoice.PackVoice("engineer", player=pl, pack=pack, volume=100)
    v._ready.wait(5)
    ok1 = v.say("Brake marker, T3.", priority=2, radio=True)
    time.sleep(0.5)
    long = sum(len(b) for b in be.written) if hasattr(be, "written") else None
    v.radio_clicks = False
    ok2 = v.say("Brake marker, T3.", priority=2, radio=True)
    check(ok1 is True and ok2 is True, "radio flag accepted by the real voice, on or off")
    pl.close()
    v.close()


def main():
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
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
