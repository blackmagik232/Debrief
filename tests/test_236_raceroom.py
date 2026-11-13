"""Debrief 2.36 "Race Room": the weekend pit board from iRacing's live numbers (roomboard), the window's own live link
that needs no coach (roomlive), the strategy crew written as rules, the page's API (peek, auto open) and the coach no
longer writing a second team-race log. Stand-ins only: the scripted team race (endu_sim) and a solo practice (crew_sim).
Run: python3 tests/ci_run.py tests/test_236_raceroom.py"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.modules.setdefault("irsdk", None)
TMP = Path(tempfile.mkdtemp(prefix="debrief-236-"))
os.environ["DEBRIEF_SETTINGS"] = str(TMP / "settings.toml")
os.environ["DEBRIEF_CACHE_DIR"] = str(TMP / "cache")
os.environ["DEBRIEF_SCHEDULE_DIR"] = str(TMP / "schedule")
os.environ["DEBRIEF_UPDATE_STATE"] = str(TMP / "update.json")
os.environ["DEBRIEF_NO_NETWORK"] = "1"

from debrief import roomboard as RB, roomlive  # noqa: E402

FAILS = []


def check(cond, what):
    print(("ok   " if cond else "FAIL ") + what)
    if not cond:
        FAILS.append(what)


class SimSource:
    state = "connected"

    def __init__(self, sim, sections):
        self.sim, self.sections, self.it = sim, sections, sim.run(None)

    def read(self):
        try:
            own, f = next(self.it)
        except StopIteration:
            return None
        own = dict(own)
        own["_field"] = f
        return own

    def info_sections(self):
        return self.sections()

    def close(self):
        pass


def team_live(pc_user=1, until=3200.0, log=None):
    import endu_sim as ES
    sim = ES.standard_sim(pc_user=pc_user)
    saved = {"inputs": ES.plan_inputs(), "title": "Test plan", "saved": "now"}
    rl = roomlive.RoomLive(source=SimSource(sim, lambda: (sim.info_version, sim.sections())), plan_for=lambda t: saved,
                           iam=lambda: ES.DRIVERS[pc_user], clock=lambda: sim.t, wall=lambda: 1.8e9 + sim.t, log=log)
    while rl.tick():
        if sim.t >= until:
            break
    rl._state_t = -1e9
    rl.tick()
    return rl, sim


def practice_live(until=900.0, session_type="Practice"):
    import crew_sim as CS
    sim = CS.RaceSim(laps=40, tank=60, burn=2.0, lap_s=60.0, cars=10, session_type=session_type)
    rl = roomlive.RoomLive(source=SimSource(sim, lambda: (1, sim.sections())), clock=lambda: sim.t)
    while rl.tick():
        if sim.t >= until:
            break
    rl._state_t = -1e9
    rl.tick()
    return rl, sim


# ------------------------------------------------------------------ pure parts
def test_session_words():
    for typ, kind, label in (("Practice", "practice", "Practice"), ("Open Qualify", "qualify", "Qualifying (open)"),
                             ("Lone Qualify", "qualify", "Qualifying (lone)"), ("Race", "race", "Race"),
                             ("Warmup", "warmup", "Warm-up"), ("Offline Testing", "practice", "Test drive"), ("", "none", "No session")):
        check(RB.session_kind(typ) == kind and RB.session_label(typ) == label, f"session words: {typ!r} -> {kind}, {label}")


def test_read_tags_follow_the_weekend_rules():
    base = {"role": "behind", "gap_s": 3.0, "rate": 0.8, "laps_to_contact": 3.5, "tags": []}
    check(RB.read_tag(base) == "threat", "a car behind closing to contact within 6 laps is a threat")
    check(RB.read_tag(dict(base, gap_s=1.0)) == "on track together", "within 1.5 s is on track together")
    check(RB.read_tag(dict(base, tags=["pitting"])) == "in the pits", "a car in the pits says so")
    check(RB.read_tag(dict(base, role="ahead", laps_to_contact=12.0), laps_left=20) == "catchable", "ahead and caught before the flag: catchable")
    check(RB.read_tag(dict(base, role="ahead", laps_to_contact=30.0), laps_left=20) != "catchable", "not caught before the flag: not catchable")
    check(RB.read_tag(dict(base, role="ahead", rate=-0.4, laps_to_contact=None)) == "pulling away", "ahead and opening: pulling away")
    check(RB.read_tag(dict(base, laps_to_contact=9.0)) == "closing slowly", "behind, closing, far off: closing slowly")
    check(RB.read_tag(dict(base, rate=-0.2, laps_to_contact=None)) == "safe", "behind and dropping back: safe")


def test_stints_and_pace():
    laps = [{"lap": i, "s": 60.0 + 0.1 * i, "driver": "A", "stint": 1, "pit": i == 10, "slow": False} for i in range(1, 11)]
    laps += [{"lap": i, "s": 61.0, "driver": "B", "stint": 2, "pit": i == 11, "slow": False} for i in range(11, 21)]
    laps.append({"lap": 21, "s": 75.0, "driver": "B", "stint": 2, "pit": False, "slow": False})      # a spin
    st = RB.stints(laps)
    check(len(st) == 2 and st[0]["driver"] == "A" and st[1]["from"] == 11, "a stint each for driver A and B")
    check(abs(st[0]["trend"] - 0.1) < 1e-6, f"driver A gets slower by 0.1 s a lap ({st[0]['trend']})")
    check(st[1]["clean"] == 9, "the pit lap and the spin are left out of B's clean laps")
    p = RB.pace(laps, None, None, {})
    check(p["ref"] == 61.0, f"reference pace: the last five clean laps ({p['ref']})")
    ch = RB.lap_chart(laps, p)
    check(len(ch["laps"]) == 21 and not ch["laps"][-1]["clean"] and ch["drivers"] == ["A", "B"], "the lap chart fades the spin and keeps both drivers")


def test_fuel_bar_in_laps():
    snap = {"fuel": {"laps": 6.6, "text": "13 L"}, "box": {"need": True, "stops": 1, "lap_latest": 58, "lap_earliest": 54,
                                                          "add_text": "Add 30 L", "in_laps": 5}}
    b = RB.fuel_bar(snap, 53, 37, 29)
    check(b["ok"] and b["run_out"] == 59 and b["finish"] == 90 and (b["open"], b["close"]) == (54, 58), "the bar: dry at 59, flag at 90, window 54 to 58")
    check("Stop between lap 54 and lap 58" in b["text"] and "Add 30 L" in b["text"], "the bar says the window and what to add")
    b = RB.fuel_bar({"fuel": {"laps": 20.0}, "box": {"need": False, "text": "No stop needed."}}, 80, 10, 60)
    check(b["ok"] and "No more stops" in b["text"] and "margin of 10 laps" in b["text"], "no stop: the margin at the flag")
    b = RB.fuel_bar({"fuel": {}}, 3, 30, 0)
    check(not b["ok"] and "Learning" in b["text"], "no burn yet: says it is learning")
    check(RB.fuel_bar(snap, 53, 37, 29, est=True)["text"].startswith("Estimated"), "a crew PC's bar says Estimated")


def test_strategy_crew_rules():
    bar = {"ok": True, "need": True, "open": 54, "close": 55, "now": 53, "stops": 1, "add_text": "Add 30 L",
           "save_text": "Save 0.1 L a lap to skip the stop", "save_realistic": True, "run_out": 56, "finish": 90}
    riv = [{"role": "behind", "num": "7", "name": "R", "gap_s": 2.0, "rate": 0.6, "laps_to_contact": 3.0, "read": "threat"},
           {"role": "ahead", "num": "9", "name": "Q", "gap_s": 8.0, "rate": 0.0, "read": "in the pits"}]
    d = {x["name"]: x for x in RB.strategy_crew("race", bar=bar, rivals=riv, pace_={"ref": 60.0, "vs_neigh": 0.7},
                                                 stint_rows=[{"clean": 6, "trend": 0.08, "sd": 0.2}], caution=True)}
    check(set(d) == {"Pit and fuel", "Rivals", "Pace and tyres", "Race engineer"}, "four desks")
    check(d["Pit and fuel"]["state"] == "act" and any("Caution" in x for x in d["Pit and fuel"]["lines"]), "caution in the window: box now")
    check(any("#9 ahead is in the pits" in x for x in d["Pit and fuel"]["lines"]), "a car ahead pitting is named")
    check(d["Rivals"]["state"] == "act" and "#7" in " ".join(d["Rivals"]["lines"]), "a threat behind is named, act now")
    check(d["Pace and tyres"]["state"] == "watch" and any("fading" in x for x in d["Pace and tyres"]["lines"]), "a fading stint is watched")
    check(d["Race engineer"]["state"] == "act" and d["Race engineer"]["head"] == d["Pit and fuel"]["head"], "the engineer picks the most urgent desk")
    d2 = {x["name"]: x for x in RB.strategy_crew("race", bar=bar, call={"text": "Box this lap.", "rank": 2})}
    check(d2["Race engineer"]["head"] == "Box this lap.", "a crew chief call wins the engineer's line")
    d3 = {x["name"]: x for x in RB.strategy_crew("practice", fuel_laps=1.4)}
    check(d3["Pit and fuel"]["state"] == "act" and d3["Pit and fuel"]["head"] == "Box this lap", "practice: about a lap of fuel left says box")


def test_waiting_board_has_every_tile():
    w = RB.waiting()
    check(w["state"] == "waiting" and len(w["tiles"]) == 8 and all(t["big"] == "--" for t in w["tiles"]), "waiting: eight tiles, all --")


# ------------------------------------------------------------------ the live link with stand-ins
def test_team_race_board():
    rows = []
    rl, sim = team_live(log=rows.append)
    st = rl.state()
    check(st["state"] == "live" and st["session"]["kind"] == "race" and st["session"]["label"] == "Race", "team race: live, a race")
    check(st["team"] and "room" in st, "team race: the team panels come with it")
    keys = [t["key"] for t in st["tiles"]]
    check(keys == ["pos", "left", "box", "ahead", "behind", "last", "target", "time"], f"the weekend's eight tiles ({keys})")
    check(st["tiles"][0]["big"].startswith("P"), "position tile shows a place")
    check(any(r.get("me") for r in st["rivals"]) and all(r.get("read") for r in st["rivals"]), "rivals include our row and a read on each")
    check([s["driver"] for s in st["stints"]][:2] == ["Alex Able", "Ben Baker"], "stints by driver")
    check(st["fuel_bar"]["ok"] and st["fuel_bar"]["now"] and st["fuel_bar"]["finish"], "the pit and fuel bar is worked out")
    check(len(st["crew"]) == 4 and st["call"] and st["call"].get("text"), "four desks and a call")
    check(st["positions"]["series"] and any(s["me"] for s in st["positions"]["series"]), "the position chart has our car")
    check(any(r.get("k") == "stop" for r in rows), "the team-race log gets the stops")
    check(rl.peek()["connected"] and rl.peek()["session"]["kind"] == "race", "peek: connected, in a race")


def test_crew_pc_is_estimated():
    rl, _ = team_live(pc_user=0)
    st = rl.state()
    check(st["role"]["role"] == "crew", "Alex's PC is crew while Ben drives")
    box = next(t for t in st["tiles"] if t["key"] == "box")
    check("Estimated" in box["sub"] or st["fuel_bar"].get("estimated"), "crew PC: the box and fuel are marked estimated")


def test_practice_board():
    rl, _ = practice_live()
    st = rl.state()
    check(st["session"]["kind"] == "practice" and not st["team"] and "room" not in st, "practice: no team panels")
    keys = [t["key"] for t in st["tiles"]]
    check(keys == ["session", "pos", "best", "last", "laps", "fuel", "ref", "time"], f"practice tiles ({keys})")
    check(st["rivals_kind"] == "timesheet" and any(r["me"] for r in st["rivals"]), "practice: the timesheet around us")
    check(st["stints"] and st["stints"][0]["laps"] >= 10, "practice laps are kept (the Race Room keeps race laps only)")
    fuel = next(t for t in st["tiles"] if t["key"] == "fuel")
    check("laps" in fuel["big"], f"practice fuel in laps ({fuel['big']})")


def test_qualifying_label():
    rl, _ = practice_live(until=300.0, session_type="Open Qualify")
    st = rl.state()
    check(st["session"]["label"] == "Qualifying (open)" and st["session"]["kind"] == "qualify", "qualifying is named")


def test_no_iracing_is_waiting():
    class Dead:
        state = "not_running"

        def read(self):
            return None

        def close(self):
            pass
    rl = roomlive.RoomLive(source=Dead())
    check(rl.tick() is False, "no iRacing: nothing read")
    st = rl.state()
    check(st["state"] == "waiting" and "Waiting for iRacing" in st["text"] and not rl.peek()["connected"], "no iRacing: the waiting board")


def test_thread_starts_and_stops():
    class Dead:
        state = "not_running"

        def read(self):
            return None

        def close(self):
            pass
    rl = roomlive.RoomLive(source=Dead())
    rl.start()
    check(rl.running(), "the live link runs on its own thread")
    rl.stop(2.0)
    check(not rl.running(), "and stops")


def test_cpu_is_light():
    import time
    rl, sim = None, None
    import endu_sim as ES
    sim = ES.standard_sim(pc_user=1)
    rl = roomlive.RoomLive(source=SimSource(sim, lambda: (sim.info_version, sim.sections())), clock=lambda: sim.t)
    n, c0 = 0, time.process_time()
    while rl.tick() and sim.t < 1800:
        n += 1
    per = (time.process_time() - c0) / max(1, n)
    check(per * roomlive.RATE_HZ < 0.05, f"the board costs under 5% of one core at {roomlive.RATE_HZ:.0f} readings a second here ({per * 1000:.2f} ms each)")


# ------------------------------------------------------------------ the window
def test_server():
    from debrief import app
    from debrief.desktop import server as SV
    lib = TMP / "lib"
    lib.mkdir(exist_ok=True)
    (TMP / "settings.toml").write_text(f"library_folder = '{lib}'\n" 'api_base = "http://127.0.0.1:9/api/v1/"\n', encoding="utf-8")
    app.LOG_FILE = str(TMP / "debrief.log")
    rl, _ = practice_live(until=600.0)
    rl.start = lambda: True
    s = SV.DebriefServer(heartbeat_timeout=3600)
    s.roomlive_factory = lambda: rl
    threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(rel, body=None):
        h = {"X-Debrief": "1"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            h["Content-Type"] = "application/json"
        rq = urllib.request.Request(s.url + rel, headers=h, data=data, method="POST" if data is not None else "GET")
        try:
            with op.open(rq, timeout=30) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")
    try:
        code, j = call("api/raceroom?peek=1")
        check(code == 200 and j["connected"] and j["session"]["kind"] == "practice" and j["auto_open"] is True, "peek: practice, opens by itself by default")
        code, j = call("api/raceroom")
        check(code == 200 and j["board"]["session"]["label"] == "Practice" and j["coach"] is False, "the board with no coach running")
        code, j = call("api/raceroom", {"action": "auto_open", "on": False})
        check(code == 200 and j["auto_open"] is False, "switch off opening by itself")
        code, j = call("api/raceroom?peek=1")
        check(j["auto_open"] is False, "... and it stays off")
        code, j = call("api/raceroom", {"action": "replan"})
        check(code == 400 and "race is running" in j["error"], "work it out again outside a race is refused in words")
        kw = s._crew_kwargs("live")
        check(kw.get("room_log") is None, "the coach no longer writes a second team-race log")
    finally:
        s.shutdown()
        s.server_close()
    check(s.roomlive is None, "closing Debrief stops the Race Room's live link")


if __name__ == "__main__":
    tests = [(n, f) for n, f in list(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        try:
            fn()
        except Exception:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            FAILS.append(name)
            print(f"FAIL {name} (raised)")
    print(f"{len(FAILS)} failed" if FAILS else "all passed")
    sys.exit(1 if FAILS else 0)
