"""2.9.1: reading a rival's lap from the replay, against a stand-in iRacing. Run: python tests/test_291_lap.py"""
from __future__ import annotations

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
TMP = Path(tempfile.mkdtemp(prefix="debrief-291-"))
os.environ["DEBRIEF_SETTINGS"] = str(TMP / "settings.toml")
os.environ["DEBRIEF_CACHE_DIR"] = str(TMP / "cache")
os.environ["DEBRIEF_UPDATE_STATE"] = str(TMP / "update.json")

import fake_iracing as fk  # noqa: E402
from debrief import racecap_link as rl, replay_scan as rs, report, rival_read as rr  # noqa: E402

L = 4000.0
N = 400           # cells of a saved trace: L / step


class Sim(fk.FakeSim):
    def __init__(self, clock, **kw):
        super().__init__(clock, **kw)
        self.session_num = 2


class Api:
    def __init__(self, ev):
        self.ev = ev

    def event_for_subsession(self, sub):
        return self.ev


_n = [0]


def me_trace(t):
    return {"tt": [round(t * i / N, 2) for i in range(N)], "v": [100] * N, "b": [0] * N, "th": [100] * N, "g": [4] * N, "t": t, "label": "Your lap"}


def scanned(driver="J Williams"):
    """A saved race review with a comparison lap of its own, and a saved scan of it."""
    _n[0] += 1
    lib = TMP / f"lib{_n[0]}"
    d = lib / "reviews" / "r1"
    d.mkdir(parents=True)
    data = {"meta": {"driver": driver, "track": "Test Track", "car": "F4", "session": "Race", "event_id": "EV1", "started_at": "2026-11-02T00:00:00+00:00"},
            "L": L, "step": 10.0, "median": 84.5, "laps": [{"n": n, "t": 84.5, "clean": True} for n in range(1, 6)], "best": {"n": 1, "t": 84.5},
            "corners": [], "style": {}, "counts": {"traffic": [3]},
            "traces": {"me1": me_trace(84.5), "ref0": {**me_trace(84.0), "label": "Teammate"}},
            "refs": [{"key": "ref0", "name": "Teammate", "t": 84.0, "rating": 2000, "role": "Teammate", "main": True, "style": {}}],
            "segments": {"names": ["A", "B"], "cuts": [0.0, 2000.0, L], "laps": {"1": [42.2, 42.3]}, "refs": {"ref0": [42.0, 42.0]},
                         "eligible": [1], "best": [{"t": 42.2, "lap": 1}, {"t": 42.3, "lap": 1}], "possible": None, "basis": "corners"}}
    report.write(d, data)
    clock = fk.FakeClock()
    rs.for_review(lib, d, api=Api("EV1"), sim=Sim(clock), clock=clock)
    return lib, d


def run(lib, d, car="93", api=None, stop=None, make=Sim, at=250.0):
    clock = fk.FakeClock()
    sim = make(clock)
    sim.t = at
    sim.car, sim.group, sim.fixed = 4, 2, 1
    try:
        return rr.read_for_review(lib, d, car, api=api, sim=sim, clock=clock, should_stop=stop), sim, clock
    except SystemExit as e:
        e.sim, e.clock = sim, clock
        raise


def is_back(sim, clock):
    clock.sleep(1.0)        # the seek lands
    assert (sim.session_num, sim.car, sim.group, sim.speed) == (2, 4, 2, 0), (sim.session_num, sim.car, sim.group, sim.speed)
    assert abs(sim.t - 250.0) < 1.0, sim.t
    assert sim.cam_now() == 1


def refused(lib, d, word, **kw):
    """Refused in plain words, with the review, its page and the laps folder as they were."""
    review, page = (d / "review.json").read_bytes(), (d / "index.html").read_bytes()
    laps = rs.find_for_event(lib, "EV1") / "laps"
    held = {p.name: p.read_bytes() for p in laps.glob("*")} if laps.exists() else {}
    try:
        run(lib, d, **kw)
    except SystemExit as e:
        assert word in str(e.code), e.code
        assert (d / "review.json").read_bytes() == review and (d / "index.html").read_bytes() == page
        assert ({p.name: p.read_bytes() for p in laps.glob("*")} if laps.exists() else {}) == held
        return e
    raise AssertionError("not refused")


def test_reads_the_fastest_clean_lap_and_adds_it_as_a_rival():
    lib, d = scanned()
    before = json.loads((d / "review.json").read_text(encoding="utf-8"))
    out, sim, clock = run(lib, d, "93")
    is_back(sim, clock)
    assert out["car"] == "93" and out["review"] == "r1"
    assert ("cam_car", "93", 2) in sim.calls                     # the camera went to them
    assert not any(c[0] == "focus" for c in sim.calls)

    scan = rl.read_scan(rs.find_for_event(lib, "EV1"))
    car = next(c for c in scan["cars"] if c["number"] == "93")
    lap = rr.pick_lap(scan, car, 84.5)
    assert lap["lap"] != 1 and lap["lap"] not in rr.dirty_laps(scan, car, rr.lap_windows(car)) and out["lap"] == lap["lap"]

    tr = json.loads((rs.find_for_event(lib, "EV1") / "laps" / "93_clean.json").read_text(encoding="utf-8"))
    assert tr["car"] == "93" and tr["mode"] == "clean" and tr["source"] == "iracing" and tr["throttle"] is None and tr["brake"] is None
    assert tr["dist"][0] < 0.02 and tr["dist"][-1] > 0.98 and tr["speed"][0] > 100     # km/h
    assert abs(tr["lap_time"] - lap["time"]) < 0.01

    # racecap_link reads it, so a new review gets the same lap
    laps = rl.rival_laps(rs.find_for_event(lib, "EV1"), "45")
    assert [x[0]["id"] for x in laps] == ["racecap:93:clean"] and laps[0][0]["_rival"] and not laps[0][0]["_main"]

    after = json.loads((d / "review.json").read_text(encoding="utf-8"))
    new = after["refs"][-1]
    assert len(after["refs"]) == 2 and new["rival"] and new["main"] is False and new["key"] == "ref1"
    assert "no pedal data" in new["role"] and new["name"] == "F Meyn" and abs(new["t"] - lap["time"]) < 0.01
    t = after["traces"]["ref1"]
    assert t["no_pedals"] is True and len(t["tt"]) == len(after["traces"]["me1"]["tt"])
    assert set(after["traces"]["me1"]) - {"label"} <= set(t)
    assert abs(sum(after["segments"]["refs"]["ref1"]) - new["t"]) < 0.05

    # the main comparison stays, nothing else moves
    assert after["refs"][0] == before["refs"][0] and after["traces"]["ref0"] == before["traces"]["ref0"]
    assert after["laps"] == before["laps"] and after["best"] == before["best"] and after["corners"] == before["corners"]
    assert after.get("insights") == before.get("insights") and after["racecap"] == before["racecap"]
    assert (after.get("insights") or {}).get("time_to_find") == (before.get("insights") or {}).get("time_to_find")
    assert all(not r.get("rival") for r in after["refs"] if r.get("main"))
    assert 'F Meyn' in (d / "index.html").read_text(encoding="utf-8")


def test_a_review_with_no_other_comparison_keeps_no_main_and_no_coaching():
    lib, d = scanned()
    data = json.loads((d / "review.json").read_text(encoding="utf-8"))
    data["refs"], data["segments"]["refs"] = [], {}
    data["traces"].pop("ref0")
    report.write(d, data)
    before = json.loads((d / "review.json").read_text(encoding="utf-8"))
    run(lib, d, "93")
    after = json.loads((d / "review.json").read_text(encoding="utf-8"))
    assert len(after["refs"]) == 1 and after["refs"][0]["rival"] and not after["refs"][0]["main"]
    assert after.get("insights") == before.get("insights")
    assert [r for r in after["refs"] if not r.get("rival")] == []


def test_reading_the_same_driver_again_replaces_their_lap():
    lib, d = scanned()
    run(lib, d, "93")
    run(lib, d, "93")
    after = json.loads((d / "review.json").read_text(encoding="utf-8"))
    assert [r["key"] for r in after["refs"]] == ["ref0", "ref1"] and sorted(k for k in after["traces"] if k.startswith("ref")) == ["ref0", "ref1"]


def test_a_replay_already_sitting_near_the_lap_still_waits_for_the_seek():
    lib, d = scanned()
    scan = rl.read_scan(rs.find_for_event(lib, "EV1"))
    lap = rr.pick_lap(scan, next(c for c in scan["cars"] if c["number"] == "93"), 84.5)
    assert run(lib, d, "93", at=lap["t0"] - 1.0)[0]["lap"] == lap["lap"]


def test_pedals_are_never_kept_even_when_the_stand_in_offers_them():
    lib, d = scanned()
    run(lib, d, "58")        # the stand-in's PLAYER car: its read_lap gives throttle and brake
    tr = json.loads((rs.find_for_event(lib, "EV1") / "laps" / "58_clean.json").read_text(encoding="utf-8"))
    assert tr["throttle"] is None and tr["brake"] is None
    assert json.loads((d / "review.json").read_text(encoding="utf-8"))["traces"]["ref1"]["no_pedals"] is True


def test_choices_put_the_cars_you_fought_most_first_and_never_you():
    lib, d = scanned()
    scan = rl.read_scan(rs.find_for_event(lib, "EV1"))
    me = rl.find_me(scan["cars"], "J Williams")
    cars = rr.choices(lib, d)["cars"]
    assert cars and all(c["car"] != "45" for c in cars)
    secs = [c["seconds"] for c in cars]
    assert secs == sorted(secs, reverse=True)
    made = {"cars": [me, {"number": "1", "name": "Far Away", "finish": 99, "line_crossings": {"0": 100, "1": 180, "2": 260, "3": 340}, "lap_times": {"1": 80, "2": 80, "3": 80}},
                     {"number": "2", "name": "Long Fight", "finish": 99, "line_crossings": {"0": 100, "1": 180, "2": 260, "3": 340}, "lap_times": {"1": 80, "2": 80, "3": 80}},
                     {"number": "3", "name": "Short Fight", "finish": 99, "line_crossings": {"0": 100, "1": 180, "2": 260, "3": 340}, "lap_times": {"1": 80, "2": 80, "3": 80}}],
            "battles": [{"ahead": {"number": "45"}, "behind": {"number": "3"}, "seconds": 5}, {"ahead": {"number": "2"}, "behind": {"number": "45"}, "seconds": 90}]}
    assert [c["car"] for c in rr.candidates(made, me)] == ["2", "3"]
    run(lib, d, cars[0]["car"])
    assert rr.choices(lib, d)["cars"][0]["read"] is True


def test_dirty_laps_are_not_clean():
    car = {"number": "7", "line_crossings": {str(n): 100.0 + 80 * n for n in range(0, 6)}, "lap_times": {str(n): 80.0 - n for n in range(1, 6)}}
    car["line_crossings"]["5"] = 100.0 + 80 * 5 - 15       # lap 5 is the quickest, and has an incident
    car["lap_times"]["5"] = 65.0
    scan = {"incidents": [{"from": 450.0, "to": 452.0, "cars": [{"number": "7"}]}], "pit_stops": [{"car": {"number": "7"}, "in": 350.0, "out": 360.0}]}
    wins = rr.lap_windows(car)
    assert rr.dirty_laps(scan, car, wins) == {1, 4, 5}    # opening lap, pit stop, incident
    assert rr.pick_lap(scan, car)["lap"] == 2             # lap 5 was quicker, but not clean
    assert rr.pick_lap({"incidents": [{"from": 0, "to": 9999, "cars": [{"number": "7"}]}]}, car) is None
    assert rr.pick_lap({}, {**car, "lap_times": {"2": 80.0}}, median=120.0) is None      # far off the review's pace: a new review would drop it


def test_wrong_replay_and_wrong_choices_are_refused_before_anything_plays():
    lib, d = scanned()
    class Other(Sim):
        def info(self):
            return {**super().info(), "subsession": 123}
    e = refused(lib, d, "isn't this review's race", make=Other)
    assert not any(c[0] in ("seek", "cam_car") for c in e.sim.calls)
    e = refused(lib, d, "isn't this review's race", api=Api("OTHER"))
    assert not any(c[0] in ("seek", "cam_car") for c in e.sim.calls)
    run(lib, d, "93", api=Api(None))     # Garage 61 not knowing is no refusal: the race ID matched
    refused(lib, d, "your own car", car="45")
    refused(lib, d, "isn't in this race", car="4242")
    class NoRace(Sim):
        def info(self):
            i = super().info()
            return {**i, "sessions": [s for s in i["sessions"] if s["num"] != 2]}
    refused(lib, d, "no race session", make=NoRace)
    data = json.loads((d / "review.json").read_text(encoding="utf-8"))
    data["meta"]["track"] = "Elsewhere Ring"
    report.write(d, data)
    refused(lib, d, "isn't this review's race")


def test_needs_a_saved_scan_and_a_race():
    _n[0] += 1
    lib = TMP / f"lib{_n[0]}"
    d = lib / "reviews" / "r1"
    d.mkdir(parents=True)
    data = {"meta": {"driver": "J Williams", "track": "Test Track", "session": "Race", "event_id": "EV9"}, "L": L, "step": 10.0, "median": 84.5,
            "laps": [], "corners": [], "style": {}, "counts": {}, "traces": {"me1": me_trace(84.5)}, "refs": []}
    report.write(d, data)
    for call in (lambda: rr.choices(lib, d), lambda: rr.read_for_review(lib, d, "93", sim=object())):
        try:
            call()
        except SystemExit as e:
            assert "Scan this race first" in str(e.code)
        else:
            raise AssertionError("not refused")
    lib, d = scanned()
    data = json.loads((d / "review.json").read_text(encoding="utf-8"))
    data["meta"]["session"] = "Practice"
    report.write(d, data)
    try:
        rr.choices(lib, d)
    except SystemExit as e:
        assert "race reviews" in str(e.code)
    else:
        raise AssertionError("not refused")


def test_stop_saves_nothing_and_puts_everything_back():
    lib, d = scanned()
    n = [0]

    def stop():
        n[0] += 1
        return n[0] > 200          # part way through the lap
    e = refused(lib, d, "Stopped", stop=stop)
    is_back(e.sim, e.clock)
    assert ("cam_car", "93", 2) in e.sim.calls


def test_a_failed_read_leaves_the_review_alone_and_the_replay_back():
    lib, d = scanned()

    class Frozen(Sim):        # the replay never gets through the lap
        def set_speed(self, speed):
            super().set_speed(0)
    e = refused(lib, d, "didn't play through", make=Frozen)
    is_back(e.sim, e.clock)

    class Jumpy(Sim):         # someone moves the replay
        def read_lap(self, i):
            x = super().read_lap(i)
            if not hasattr(self, "n"):
                self.n = 0
            self.n += 1
            if self.n > 500:
                x["session_time"] -= 60
            return x
    e = refused(lib, d, "jumped", make=Jumpy)
    is_back(e.sim, e.clock)

    class Broken(Sim):        # the connection goes
        def read_lap(self, i):
            raise OSError("gone")
    e = refused(lib, d, "Reading the lap stopped", make=Broken)
    is_back(e.sim, e.clock)

    class NoLand(Sim):
        def seek(self, s, t):
            self.calls.append(("seek", s, t))
    refused(lib, d, "didn't move", make=NoLand)

    class Holes(Sim):         # readings that don't show the whole lap
        def read_lap(self, i):
            x = super().read_lap(i)
            if 0.4 < (x["pct"] or 0) < 0.6:
                x["pct"] = -1.0
            return x
    refused(lib, d, "couldn't be used", make=Holes)


def test_speed_is_only_taken_from_the_camera_car():
    lib, d = scanned()

    class Wanders(Sim):       # the camera will not hold the rival
        def cam_car(self, number, group, camera=0):
            super().cam_car(number, group, camera)
            self.wander = number == "93"

        def read_lap(self, i):
            x = super().read_lap(i)
            if getattr(self, "wander", False) and self.t > 300:
                x.update(cam_car_idx=4, speed=999.0)
            return x
    e = refused(lib, d, "didn't stay on", make=Wanders)
    is_back(e.sim, e.clock)
    meta = {"car": "1", "name": "A"}
    rows = [(100.0 + i / 10, i / 1000, 0.0, 6000, 4, None if i % 10 == 0 else 50.0) for i in range(1000)]
    tr = rr.build_trace(rows, 4000.0, meta, 99.9)
    assert max(tr["speed"]) < 200 and tr["throttle"] is None


def test_a_review_that_will_not_save_keeps_the_old_lap_file():
    lib, d = scanned()
    run(lib, d, "93")
    path = rs.find_for_event(lib, "EV1") / "laps" / "93_clean.json"
    good = path.read_bytes()
    keep = rr.add_to_compare
    rr.add_to_compare = lambda *a: (_ for _ in ()).throw(ValueError("disk"))
    try:
        refused(lib, d, "couldn't be saved")
    finally:
        rr.add_to_compare = keep
    assert path.read_bytes() == good


def test_real_connection_reads_speed_only_on_camera():
    class Ir:
        vals = {"CamCarIdx": 2, "CarIdxLapDistPct": [0.1, 0.2, 0.3], "Speed": 50.0, "ReplaySessionNum": 2, "ReplaySessionTime": 9.0,
                "CarIdxSteer": [1, 2, 3], "CarIdxRPM": [1, 2, 3], "CarIdxGear": [1, 2, 3], "CamGroupNumber": 4, "CamCameraNumber": 7}
        calls = []

        def __getitem__(self, k):
            return self.vals[k]

        def freeze_var_buffer_latest(self):
            pass

        unfreeze_var_buffer_latest = freeze_var_buffer_latest

        def cam_switch_num(self, *a):
            self.calls.append(a)
    sim = rs.RealSim.__new__(rs.RealSim)
    sim.ir = Ir()
    assert sim.read_lap(2)["speed"] == 50.0 and sim.read_lap(1)["speed"] is None
    assert sim.read_lap(1)["pct"] == 0.2 and "throttle" not in sim.read_lap(2)
    assert sim.read()["cam_num"] == 7 and sim.read()["cam_group"] == 4
    sim.cam_car("93", 4, 7)
    assert Ir.calls == [("93", 4, 7)]


def test_the_window_can_list_read_and_stop():
    from debrief import app
    from debrief.desktop import server as S
    lib, d = scanned()
    Path(os.environ["DEBRIEF_SETTINGS"]).write_text(f"library_folder = '{lib}'\napi_base = \"http://127.0.0.1:9/api/v1/\"\n", encoding="utf-8")
    app.LOG_FILE = str(TMP / "debrief.log")
    srv = S.DebriefServer(heartbeat_timeout=3600)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def post(rel, body):
        rq = urllib.request.Request(srv.url + rel, data=json.dumps(body).encode("utf-8"), method="POST",
                                    headers={"X-Debrief": "1", "Content-Type": "application/json"})
        try:
            with opener.open(rq, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return json.loads(e.read().decode("utf-8"))
    seen, keep = {}, rr.read_for_review

    def stand_in(folder, review, car, api=None, progress=None, should_stop=None):
        seen["car"] = car
        while not should_stop():
            time.sleep(0.02)
        raise SystemExit("Stopped. Nothing was saved and your review is unchanged. Your replay is back where it was.")
    rr.read_for_review = stand_in
    try:
        cars = post("api/rival-choices", {"review": "r1"})
        assert cars["ok"] and cars["cars"]
        assert not post("api/rival-choices", {"review": "../x"})["ok"]
        assert not post("api/rival-read", {"review": "r1"})["ok"]
        assert post("api/rival-read", {"review": "r1", "car": cars["cars"][0]["car"]})["ok"]
        assert post("api/replay-scan-stop", {})["ok"]
        for _ in range(200):
            if not srv.jobs.running:
                break
            time.sleep(0.02)
        assert not srv.jobs.running and srv.jobs.st["state"] == "error" and "Stopped" in srv.jobs.st["error"] and seen["car"] == cars["cars"][0]["car"]
    finally:
        rr.read_for_review = keep
        srv.shutdown()
    ui = ROOT / "debrief" / "desktop" / "ui"
    assert 'id="fbRival"' in (ui / "index.html").read_text(encoding="utf-8") and "rival-read" in (ui / "app.js").read_text(encoding="utf-8")


if __name__ == "__main__":
    fails = 0
    for k, fn in list(globals().items()):
        if k.startswith("test_"):
            try:
                fn()
                print("ok", k)
            except Exception:  # noqa: BLE001
                import traceback
                fails += 1
                print("FAIL", k)
                traceback.print_exc()
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"{fails} failed" if fails else "all passed")
    sys.exit(1 if fails else 0)
