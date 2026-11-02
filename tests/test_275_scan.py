"""2.7.5: the built-in replay scan, against a stand-in iRacing. Run: python tests/test_275_scan.py"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
TMP = Path(tempfile.mkdtemp(prefix="debrief-275s-"))

import fake_iracing as fk  # noqa: E402
from debrief import racecap_link as rl, replay_scan as rs, report  # noqa: E402


class Sim(fk.FakeSim):
    def __init__(self, clock, **kw):
        super().__init__(clock, **kw)
        self.session_num = 2


class Api:
    def __init__(self, ev):
        self.ev = ev

    def event_for_subsession(self, sub):
        if isinstance(self.ev, Exception):
            raise self.ev
        return self.ev


_n = [0]


def review(session="Race", driver="J Williams", track="Test Track"):
    _n[0] += 1
    lib = TMP / f"lib{_n[0]}"
    d = lib / "reviews" / "r1"
    d.mkdir(parents=True)
    data = {"meta": {"driver": driver, "track": track, "car": "F4", "session": session, "event_id": "EV1", "started_at": "2026-11-02T00:00:00+00:00"},
            "laps": [{"n": n, "t": 84.5, "clean": True} for n in range(1, 6)], "best": {"n": 1, "t": 84.5}, "median": 84.5, "corners": [], "style": {}, "counts": {"traffic": [3]}}
    report.write(d, data)
    return lib, d


def run(lib, d, api=None, stop=None):
    clock = fk.FakeClock()
    sim = Sim(clock)
    start = (sim.session_num, sim.t)
    out = rs.for_review(lib, d, api=api, sim=sim, clock=clock, should_stop=stop)
    return out, sim, start


def refused(lib, d, word, **kw):
    try:
        run(lib, d, **kw)
    except SystemExit as e:
        assert word in str(e.code), e.code
        assert not list((Path(lib) / rs.SCANS_DIR).glob("Races/*")) or word == "Scan stopped"
        return
    raise AssertionError("not refused")


def test_scan_adds_the_race_log_and_changes_no_numbers():
    lib, d = review()
    before = json.loads((d / "review.json").read_text(encoding="utf-8"))
    out, sim, start = run(lib, d, api=Api("EV1"))
    after = json.loads((d / "review.json").read_text(encoding="utf-8"))
    assert out["confirmed"] is True and out["subsession"] == "89056204"
    rc = after["racecap"]
    assert rc["start_type"] == "rolling" and rc["passes"] and rc["name"] == "J Williams" and rc["traffic_check"]
    assert after["laps"][0]["t"] == before["laps"][0]["t"] and after["median"] == before["median"] and after["meta"]["start_type"] == "rolling"
    assert sim.speed == 0 and sim.calls[-1] == ("seek", start[0], start[1])      # the replay goes back
    folder = rs.find_for_event(lib, "EV1")
    assert folder and rl.read_scan(folder)["subsession"] == 89056204 and rs.find_for_event(lib, "NOPE") is None


def test_wrong_replay_is_refused_before_anything_plays():
    lib, d = review()
    refused(lib, d, "isn't this review's race", api=Api("OTHER"))
    lib, d = review(track="Elsewhere Ring")
    refused(lib, d, "isn't this review's race")
    lib, d = review(driver="Nobody Here")
    refused(lib, d, "Couldn't find")
    lib, d = review(session="Practice")
    refused(lib, d, "race reviews")


def test_garage61_down_still_scans_but_says_unconfirmed():
    lib, d = review()
    assert run(lib, d, api=Api(RuntimeError("down")))[0]["confirmed"] is None


def test_stop_saves_nothing_and_puts_the_replay_back():
    lib, d = review()
    n = [0]

    def stop():
        n[0] += 1
        return n[0] > 50
    refused(lib, d, "Scan stopped", stop=stop)
    assert "racecap" not in json.loads((d / "review.json").read_text(encoding="utf-8"))
    assert rs.find_for_event(lib, "EV1") is None


def test_interrupted_scan_keeps_the_good_log():
    lib, d = review()
    run(lib, d, api=Api("EV1"))
    good = (d / "review.json").read_bytes()

    class Frozen(Sim):
        def advance(self, s):
            super().advance(s)
            self.t = min(self.t, 200.0)
    clock = fk.FakeClock()
    try:
        rs.for_review(lib, d, api=Api("EV1"), sim=Frozen(clock), clock=clock)
    except SystemExit as e:
        assert "didn't reach the end" in str(e.code)
    else:
        raise AssertionError
    assert (d / "review.json").read_bytes() == good


def test_unconfirmed_race_must_match_lap_times():
    lib, d = review()
    data = json.loads((d / "review.json").read_text(encoding="utf-8"))
    for l in data["laps"]:
        l["t"] = 120.0
    (d / "review.json").write_text(json.dumps(data), encoding="utf-8")
    refused(lib, d, "different race")
    assert rl.same_track("Charlotte Motor Speedway (Roval 2019)", "Charlotte Motor Speedway")
    assert not rl.same_track("Road America", "Road Atlanta") and not rl.same_track("Circuit de Spa", "Circuit de Monaco")


def test_no_iracing_says_what_to_do():
    class Off:
        def connect(self):
            return False
    lib, d = review()
    try:
        rs.for_review(lib, d, sim=Off())
    except SystemExit as e:
        assert "load this race's replay" in str(e.code)
        return
    raise AssertionError


def test_iracing_library_is_there_on_windows():
    if os.name == "nt":
        import irsdk
        import yaml
        assert irsdk and yaml


if __name__ == "__main__":
    fails = 0
    for k, fn in list(globals().items()):
        if k.startswith("test_"):
            try:
                fn()
                print("ok", k)
            except (Exception, SystemExit):  # noqa: BLE001
                import traceback
                fails += 1
                traceback.print_exc()
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(1 if fails else 0)
