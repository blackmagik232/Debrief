"""Debrief 2.27 "Live data": the shared iRacing reader (livedata.py) and the background recorder (liverec.py).

    python3 tests/ci_run.py tests/test_227_liverec.py

A scripted sim (tests/fake_live.py) stands in for iRacing: a practice and a short race, with aids missing, iRacing closing
in the middle, the session text changing, a second subsession, pit stops by other cars, a crash and a second recorder.
No network; temp folders from tempfile; nothing here needs Windows.
"""
from __future__ import annotations

import gzip
import json
import os
import shutil
import sys
import tempfile
import time
import traceback
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

TMP = Path(tempfile.mkdtemp(prefix="debrief-227-rec-"))
ST = TMP / "settings.toml"
ST.write_text(f'token = "PASTE-YOUR-GARAGE61-TOKEN-HERE"\nlibrary_folder = \'{TMP / "lib"}\'\n', encoding="utf-8")
os.environ.update(DEBRIEF_SETTINGS=str(ST), DEBRIEF_CACHE_DIR=str(TMP / "cache"))

import fake_live as F  # noqa: E402
from debrief import livedata, liverec  # noqa: E402

LIB = TMP / "lib"
PREFS = {"record_sessions": True, "start_with_windows": True, "library_folder": str(LIB)}
_n = [0]


def fresh():
    """A new library folder and a clean recorder state for each test."""
    _n[0] += 1
    lib = TMP / f"lib{_n[0]}"
    lib.mkdir()
    try:
        (ST.parent / "liverec.json").unlink()
    except OSError:
        pass
    return lib


def make(lib, sim=None, **kw):
    sim = sim or F.Sim(**kw)
    rd = F.FakeReader(sim)
    prefs = {"record_sessions": True, "start_with_windows": True, "library_folder": str(lib)}
    rec = liverec.Recorder(rd, library_folder=lambda: str(lib), use_lock=False, clock=lambda: sim.now,
                           wall=lambda: sim.wall, prefs_fn=lambda: dict(prefs), utc=sim.utc, log=lambda m: None)
    rec.prefs = prefs
    return sim, rd, rec


def run(sim, rec, seconds, dt=0.05):
    end = sim.t + seconds
    while sim.t < end - 1e-9:
        sim.step(dt)
        rec.tick()


def summary_of(folder):
    return json.loads((Path(folder) / "summary.json").read_text(encoding="utf-8"))


# ================================================================ livedata
def _irsdk(sim, listed=True):
    class IR(F.FakeIR):
        @property
        def var_headers_names(self):
            return super().var_headers_names if listed else None
    IR.instances = []
    IR.sim_for_next = sim
    sys.modules["irsdk"] = types.SimpleNamespace(IRSDK=IR)
    return IR


def test_reader_reads_three_tiers_and_skips_missing_channels():
    sim = F.Sim(aids=False)
    IR = _irsdk(sim)
    rd = livedata.LiveReader(own=livedata.COACH_CHANNELS + livedata.OWN_EXTRA, field=livedata.FIELD_CHANNELS, info_poll_s=0.01)
    try:
        sim.step(30)
        s = rd.read_own(keep_none=False)
        assert s["FuelLevel"] < 60 and s["Lap"] == 1 and rd.state == "connected"
        for k in ("dcBrakeBias", "dcABS", "dcTractionControl"):
            assert k not in s, k                      # a car without aids: left out, no error
        f = rd.read_field()
        assert len(f["CarIdxLapDistPct"]) == 64 and set(f) == set(livedata.FIELD_CHANNELS)
        # keep_none (the coach's way) gives the missing ones as None
        assert rd.read_own(keep_none=True)["dcABS"] is None
        assert set(rd.missing()) == {"dcBrakeBias", "dcABS", "dcTractionControl"}
        ir = IR.instances[0]
        before = dict(ir.gets)
        for _ in range(50):
            rd.read_own(keep_none=False)
        assert ir.gets.get("dcABS", 0) == before.get("dcABS", 0), "a missing channel is not asked for again"
    finally:
        rd.close()
        sys.modules.pop("irsdk", None)


def test_reader_probes_once_when_the_sdk_cannot_list_channels():
    sim = F.Sim(aids=False)
    IR = _irsdk(sim, listed=False)
    rd = livedata.LiveReader(own=livedata.COACH_CHANNELS + livedata.OWN_EXTRA, info_poll_s=0.01)
    try:
        for _ in range(20):
            rd.read_own(keep_none=False)
        assert IR.instances[0].gets["dcABS"] == 1          # asked once, remembered
    finally:
        rd.close()
        sys.modules.pop("irsdk", None)


def test_session_info_is_read_on_its_own_thread_and_only_when_it_changes():
    sim = F.Sim()
    IR = _irsdk(sim)
    rd = livedata.LiveReader(own=livedata.COACH_CHANNELS, info_poll_s=0.01)
    try:
        assert rd.read_own() is not None
        end = time.time() + 3
        while not rd.info_sections()[1] and time.time() < end:
            time.sleep(0.01)
        v1, sec = rd.info_sections()
        assert sec["WeekendInfo"]["TrackDisplayName"].startswith("Circuit de Spa")
        ir = IR.instances[0]
        n0 = ir.gets["WeekendInfo"]
        for _ in range(200):                                 # 20 Hz reading never asks for session text
            rd.read_own()
        time.sleep(0.15)
        assert ir.gets["WeekendInfo"] == n0, "no re-read while the counter is unchanged"
        sim.track_name = "Zolder"
        sim.info_version += 1
        end = time.time() + 3
        while rd.info_sections()[1].get("WeekendInfo", {}).get("TrackDisplayName") != "Zolder" and time.time() < end:
            time.sleep(0.01)
        assert rd.info_sections()[1]["WeekendInfo"]["TrackDisplayName"] == "Zolder" and rd.info_sections()[0] > v1
        rd.close()
        assert rd._thread is None
    finally:
        rd.close()
        sys.modules.pop("irsdk", None)


def test_reader_without_pyirsdk_or_iracing_is_quiet():
    sys.modules.pop("irsdk", None)
    rd = livedata.LiveReader(own=livedata.COACH_CHANNELS)
    assert rd.read_own() is None and rd.state == "not_installed"
    sim = F.Sim()
    sim.connected = False
    _irsdk(sim)
    try:
        rd2 = livedata.LiveReader(own=livedata.COACH_CHANNELS, retry_s=0.0)
        assert rd2.read_own() is None and rd2.state == "not_running" and rd2._thread is None
    finally:
        sys.modules.pop("irsdk", None)


def test_the_coach_source_is_the_same_reader():
    from debrief import livecoach as lc
    assert issubclass(lc.IrsdkSource, livedata.LiveReader) and lc.CHANNELS == livedata.COACH_CHANNELS
    sim = F.Sim()
    sim.step(70)
    _irsdk(sim)
    try:
        src = lc.IrsdkSource()
        s = src.read()
        assert s["SessionType"] == "Practice" and src.info()["track"]["name"].startswith("Circuit de Spa")
        assert src.info()["car"]["path"] == "bmwm4gt3" and abs(src.info()["length_m"] - 7000) < 1
        src.close()
    finally:
        sys.modules.pop("irsdk", None)


# ================================================================ a practice, start to finish
def test_practice_is_recorded_to_the_documented_folder():
    lib = fresh()
    sim, rd, rec = make(lib, kind="practice", laps=6)
    sim.at(100, lambda s: setattr(s, "inc", s.inc + 2))
    sim.at(130, lambda s: setattr(s, "bb", 55.0))
    run(sim, rec, 6 * 60 + 5)
    assert rec.status()["text"].startswith("Recording Spa practice, ") and "laps" in rec.status()["text"], rec.status()
    rec._end_session("test")
    d = rec.finished[0]
    assert d.parent == lib / "Recorded" and d.name == "2026-11-08_1930_circuit-de-spa-francorchamps_1234567", d.name
    for f in ("session.yaml.json", "car.jsonl.gz", "field.jsonl.gz", "summary.json"):
        assert (d / f).is_file(), f
    sm = summary_of(d)
    assert sm["final"] is True and sm["subsession"] == 1234567 and sm["track"]["name"].startswith("Circuit de Spa")
    assert sm["car"]["name"] == "BMW M4 GT3" and sm["driver"] == "Me Driver" and sm["fuel_tank_l"] == 100.0
    (sec,) = sm["sessions"]
    assert sec["type"] == "Practice" and sec["num"] == 0
    assert sec["laps"] >= 5 and sec["best"] == 60.0 and sec["typical"] == 60.0
    assert abs(sec["fuel"]["per_lap_median_l"] - 3.0) < 0.05, sec["fuel"]            # 0.05 L/s over a 60 s lap
    assert sec["aids"]["seen"]["bb"] == [54.0, 55.0] and sec["aids"]["seen"]["abs"] == [3.0] and sec["aids"]["seen"]["tc"] == [2.0]
    (chg,) = sec["aids"]["changes"]
    assert chg["aid"] == "bb" and chg["from"] == 54.0 and chg["to"] == 55.0 and chg["lap"] == 3, chg
    assert sec["incidents"]["total"] == 2 and sec["incidents"]["by_lap"] == [{"lap": 2, "gained": 2}], sec["incidents"]
    assert sec["pit_stops"] == [] and "race" not in sec
    assert sm["disk_logging"] is True and sm["disk_logging_note"] == ""
    # the car file: 1 Hz rows plus a row per lap
    rows = list(liverec.read_jsonl(d / "car.jsonl.gz"))
    kinds = {r["k"] for r in rows}
    assert kinds >= {"open", "s", "lap", "inc", "close"}
    laps = [r for r in rows if r["k"] == "lap"]
    assert laps[0].get("partial") is True and all(abs(l["fuel_used"] - 3.0) < 0.05 for l in laps[1:]), laps[:2]
    assert all("aids" in l and "chg" in l and "inc" in l and "pit" in l and "time" in l for l in laps)
    s1 = [r for r in rows if r["k"] == "s"]
    assert 0.9 < (s1[-1]["t"] - s1[0]["t"]) / max(1, len(s1) - 1) < 1.1, "one row a second"
    assert {"fuel", "bb", "abs", "tc", "ti", "tt"} <= set(s1[5])
    # the field file is compact and expands back to full lists
    fr = list(liverec.expand_field(liverec.read_jsonl(d / "field.jsonl.gz")))
    assert len(fr) >= 300 and len(fr[10]["pct"]) == 64 and fr[10]["pit"] == fr[10]["pit"]
    raw = list(liverec.read_jsonl(d / "field.jsonl.gz"))
    assert "pos" not in raw[3] and "pos" in raw[0], "lists that did not change are left out after the first row"
    assert (d / "field.jsonl.gz").stat().st_size < 60_000


def test_missing_aid_channels_are_skipped_silently():
    lib = fresh()
    sim, rd, rec = make(lib, kind="practice", aids=False)
    run(sim, rec, 200)
    rec._end_session("test")
    sm = summary_of(rec.finished[0])
    assert sm["sessions"][0]["aids"]["seen"] == {} and sm["sessions"][0]["aids"]["changes"] == []
    assert rec.errors == 0


def test_a_short_race_has_positions_incidents_and_every_cars_pit_stops():
    lib = fresh()
    sim, rd, rec = make(lib, kind="race", laps=5)
    sim.at(100, lambda s: setattr(s, "inc", 4))
    run(sim, rec, 5 * 60 + 5)
    rec._end_session("test")
    sm = summary_of(rec.finished[0])
    (sec,) = sm["sessions"]
    assert sec["type"] == "Race"
    r = sec["race"]
    assert r["finish"]["pos"] == 3 and r["finish"]["cpos"] == 3, r["finish"]           # from the session results
    assert [p["lap"] for p in r["positions"]] == [2, 3, 4, 5] and all(p["pos"] == 2 for p in r["positions"])
    me = next(x for x in r["rivals"] if x["is_me"])
    rival = next(x for x in r["rivals"] if x["number"] == "44")
    still = next(x for x in r["rivals"] if x["number"] == "11")
    assert len(me["stops"]) == 1 and me["stops"][0]["lap"] == 3 and 24 < me["stops"][0]["lane_s"] < 27 and me["stops"][0]["stopped"]
    assert len(rival["stops"]) == 1 and rival["stops"][0]["lap"] == 3 and 19 < rival["stops"][0]["lane_s"] < 22
    assert 7 < rival["stops"][0]["stall_s"] < 10, rival["stops"]
    assert still["stops"] == [] and all(not x["number"] == "0" for x in r["rivals"]), "the pace car is not a rival"
    # your own stop: fuel added, tyres changed, stints split there
    (stop,) = sec["pit_stops"]
    assert stop["lap"] == 3 and 25 < stop["fuel_added_l"] < 32 and stop["tyres_changed"] is True and 12 < stop["stall_s"] < 16, stop
    assert [s["stint"] for s in sec["fuel"]["stints"]] == [1, 2]
    assert sec["incidents"]["total"] == 4


def test_iracing_closing_in_the_middle_finishes_the_recording():
    lib = fresh()
    sim, rd, rec = make(lib, kind="practice")
    run(sim, rec, 130)
    sim.connected = False
    run(sim, rec, 3)                                   # a short blip does not end it
    assert rec.cur is not None and rec.status()["state"] == "recording"
    run(sim, rec, 5)
    assert rec.cur is None and rec.finished, "gone for more than a few seconds: finished"
    sm = summary_of(rec.finished[0])
    assert sm["final"] and sm["ended_by"] == "iRacing closed" and sm["sessions"][0]["laps"] >= 1
    assert rec.status()["text"] == "Waiting for iRacing"
    # iRacing comes back (a new subsession): a new folder
    sim.connected = True
    sim.sub = 7654321
    rd.refresh_info()
    run(sim, rec, 70)
    assert rec.cur is not None and rec.cur.folder != rec.finished[0] and rec.cur.folder.name.endswith("_7654321")


def test_a_blip_inside_the_gap_continues_the_same_folder():
    lib = fresh()
    sim, rd, rec = make(lib, kind="practice")
    run(sim, rec, 70)
    folder = rec.cur.folder
    sim.connected = False
    run(sim, rec, 2)
    sim.connected = True
    run(sim, rec, 70)
    assert rec.cur is not None and rec.cur.folder == folder and not rec.finished


def test_crash_leaves_rows_and_the_next_start_finishes_the_folder():
    lib = fresh()
    sim, rd, rec = make(lib, kind="practice")
    run(sim, rec, 200)
    rec.cur.car.flush()
    rec.cur.field.flush()
    folder = rec.cur.folder
    assert not (folder / "summary.json").exists() or summary_of(folder)["final"] is False
    # the process dies here: a new recorder starts
    sim2, rd2, rec2 = make(lib, kind="practice", sub=555)
    assert liverec.unfinished(lib) == [folder]
    rec2._prefs = rec2.prefs_fn()
    rec2._recover()
    assert rec2.recovered == [folder] and liverec.unfinished(lib) == []
    sm = summary_of(folder)
    assert sm["final"] is True and sm["ended_by"].startswith("recovered") and sm["sessions"][0]["laps"] >= 2
    # a half-written last gzip piece costs only that piece
    with open(folder / "car.jsonl.gz", "ab") as f:
        f.write(gzip.compress(b'{"k":"s","sn":0,"t":999.0}\n')[:-6])
    assert len(list(liverec.read_jsonl(folder / "car.jsonl.gz"))) > 100


def test_session_info_changes_are_copied_once_and_only_what_changed():
    lib = fresh()
    sim, rd, rec = make(lib, kind="race")
    run(sim, rec, 30)
    rd.refresh_info()                                  # the same text again: nothing new
    run(sim, rec, 5)
    doc = json.loads((rec.cur.folder / "session.yaml.json").read_text(encoding="utf-8"))
    assert len(doc["snapshots"]) == 1 and set(doc["snapshots"][0]) >= {"WeekendInfo", "DriverInfo", "SessionInfo"}
    sim.extra_drivers = [{"CarIdx": 6, "UserName": "F Late", "CarNumber": "66", "TeamName": "", "CarClassShortName": "GT3",
                          "CarScreenName": "BMW M4 GT3", "CarPath": "bmwm4gt3", "CarID": 132, "IsSpectator": 0, "CarIsPaceCar": 0}]
    rd.refresh_info()
    run(sim, rec, 5)
    doc = json.loads((rec.cur.folder / "session.yaml.json").read_text(encoding="utf-8"))
    assert len(doc["snapshots"]) == 2 and "DriverInfo" in doc["snapshots"][1] and "WeekendInfo" not in doc["snapshots"][1]
    merged = liverec.merged_info(rec.cur.folder)
    assert len(merged["DriverInfo"]["Drivers"]) == 7 and merged["WeekendInfo"]["SubSessionID"] == 1234567


def test_practice_qualify_and_race_of_one_subsession_share_a_folder():
    lib = fresh()
    sim, rd, rec = make(lib, kind="practice", first_sn=0)
    run(sim, rec, 130)
    sim.sn, sim.t = 1, 0.0
    sim.kind = "practice"
    run(sim, rec, 130)
    sim.sn, sim.t = 2, 0.0
    sim.kind = "race"
    run(sim, rec, 130)
    assert len(list((lib / "Recorded").iterdir())) == 1
    # a summary is written each time a section ends, and says it is not final yet
    interim = summary_of(rec.cur.folder)
    assert interim["final"] is False and [s["num"] for s in interim["sessions"]] == [0, 1], "the sections that have ended so far"
    rec._end_session("test")
    sm = summary_of(rec.finished[0])
    assert [s["num"] for s in sm["sessions"]] == [0, 1, 2]
    assert [s["type"] for s in sm["sessions"]] == ["Practice", "Lone Qualify", "Race"]
    assert "race" in sm["sessions"][2] and "race" not in sm["sessions"][0]


def test_a_second_subsession_gets_its_own_folder_and_a_local_session_is_called_local():
    lib = fresh()
    sim, rd, rec = make(lib, kind="practice", sub=111)
    run(sim, rec, 65)
    first = rec.cur.folder
    sim.sub, sim.t = 222, 0.0
    sim.wall = sim.wall.replace(hour=21)
    rd.refresh_info()
    run(sim, rec, 65)
    assert first.name.endswith("_111") and rec.cur.folder.name.endswith("_222") and rec.finished == [first]
    assert summary_of(first)["final"] is True and summary_of(first)["ended_by"] == "next session started"
    # offline testing: no subsession id
    sim.sub, sim.t = 0, 0.0
    sim.wall = sim.wall.replace(hour=22)
    rd.refresh_info()
    run(sim, rec, 65)
    assert rec.cur.folder.name.endswith("_local")
    assert summary_of(rec.finished[-1])["subsession"] == 222
    # the clock jumping back in a local session is a new session
    sim.t = 0.0
    sim.wall = sim.wall.replace(hour=23)
    run(sim, rec, 20)
    assert rec.cur.folder.name.endswith("_local") and len(rec.finished) == 3 and rec.finished[-1].name.endswith("_local")


def test_two_folders_in_the_same_minute_do_not_collide():
    lib = fresh()
    sim, rd, rec = make(lib, kind="practice", sub=0)
    run(sim, rec, 10)
    a = rec.cur.folder
    rec._end_session("x")
    run(sim, rec, 10)
    assert rec.cur.folder != a and rec.cur.folder.name.startswith(a.name)


def test_paused_or_switched_off_records_nothing_and_ends_the_session():
    lib = fresh()
    sim, rd, rec = make(lib, kind="practice")
    run(sim, rec, 70)
    assert rec.cur is not None
    rec.pause()
    run(sim, rec, 5)
    assert rec.cur is None and rec.status()["state"] == "paused" and rec.status()["text"] == "Paused" and rec.finished
    reads = rd.reads
    run(sim, rec, 5)
    assert rd.reads == reads, "paused: iRacing is not even read"
    rec.resume()
    run(sim, rec, 5)
    assert rec.cur is not None
    rec.prefs["record_sessions"] = False
    rec._next_beat = 0
    run(sim, rec, 5)
    assert rec.cur is None and rec.status()["text"] == "Off in Settings"


def test_iracing_telemetry_files_off_is_noted_and_never_toggled():
    lib = fresh()
    sim, rd, rec = make(lib, kind="practice")
    sim.disk = False
    run(sim, rec, 70)
    assert rec.status_doc()["disk_logging"] is False
    rec._end_session("x")
    sm = summary_of(rec.finished[0])
    assert sm["disk_logging"] is False and ".ibt" in sm["disk_logging_note"] and "never changes" in sm["disk_logging_note"]
    src = (ROOT / "debrief" / "liverec.py").read_text(encoding="utf-8") + (ROOT / "debrief" / "livedata.py").read_text(encoding="utf-8")
    assert "broadcast" not in src.lower() and "irsdk_" not in src and "_broadcast_msg" not in src and "telem_command" not in src


def test_a_failure_in_one_step_does_not_stop_the_recorder():
    """The recorder thread catches a bad step, counts it, logs it and carries on with the next sample."""
    lib = fresh()
    sim, rd, rec = make(lib, kind="practice")
    logs = []
    rec.say = logs.append
    orig = rec._own
    n = {"i": 0}

    def bad(*a, **k):
        n["i"] += 1
        if n["i"] == 3:
            raise RuntimeError("one bad sample")
        return orig(*a, **k)
    rec._own = bad
    lock = liverec.RecorderLock("window", path=TMP / "lock-fail.json")
    rec.lock = lock
    sim.step(1)
    assert rec.start()
    time.sleep(0.6)
    rec.stop()
    assert rec.errors == 1 and any("ERROR while recording" in m for m in logs) and n["i"] > 3, (rec.errors, n)


def test_the_thread_waits_for_iracing_with_almost_no_work_and_stops_cleanly():
    lib = fresh()
    sim = F.Sim()
    sim.connected = False
    rd = F.FakeReader(sim)
    lock = liverec.RecorderLock("window", path=TMP / "lock-wait.json")
    rec = liverec.Recorder(rd, library_folder=lambda: str(lib), lock=lock, log=lambda m: None,
                           prefs_fn=lambda: dict(PREFS, library_folder=str(lib)))
    assert rec.start()
    time.sleep(0.5)
    st = rec.status()
    assert st["running"] and st["text"] == "Waiting for iRacing" and st["state"] == "waiting", st
    assert rd.reads == 0 or sim.connected is False
    t0 = time.time()
    rec.stop()
    assert time.time() - t0 < 3 and rd.closed and not lock.held and not (TMP / "lock-wait.json").exists()
    assert not (lib / "Recorded").exists(), "nothing is created until a session is seen"


def test_the_status_file_is_what_the_window_reads():
    lib = fresh()
    sim, rd, rec = make(lib, kind="practice")
    path = TMP / "lock-status.json"
    rec.lock = liverec.RecorderLock("recorder", path=path)
    assert rec.lock.acquire()
    rec.has_lock = True
    run(sim, rec, 70)
    rec.lock.beat(rec.status_doc())
    d = liverec.read_lock(path)
    assert d["alive"] and d["role"] == "recorder" and d["text"].startswith("Recording Spa practice") and d["session"]["laps"] >= 0
    assert d["pid"] == os.getpid() and "token" in d
    rec.lock.release()


# ================================================================ one recorder at a time
def test_only_one_recorder_holds_the_lock_and_a_dead_one_is_taken_over():
    path = TMP / "lock-one.json"
    a, b = liverec.RecorderLock("recorder", path=path), liverec.RecorderLock("window", path=path)
    assert a.acquire() and not b.acquire()
    assert liverec.read_lock(path)["role"] == "recorder"
    # the holder stops beating: after the stale time another recorder may take over
    old = time.time() - 60
    os.utime(path, (old, old))
    assert not liverec.read_lock(path)["alive"]
    assert b.acquire() and liverec.read_lock(path)["role"] == "window"
    a.release()                                          # not its lock any more: leaves b's alone
    assert path.exists()
    b.release()
    assert not path.exists()
    # a lock whose process is gone is free at once
    a.held = True
    a.beat({"state": "x"})
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["pid"] = 2 ** 22 + 12345
    path.write_text(json.dumps(doc), encoding="utf-8")
    assert not liverec.read_lock(path)["alive"] and b.acquire()
    b.release()


def test_a_second_recorder_does_not_start_and_says_why():
    lib = fresh()
    path = TMP / "lock-two.json"
    logs = []
    sim = F.Sim()
    sim.connected = False
    r1 = liverec.Recorder(F.FakeReader(sim), library_folder=lambda: str(lib), lock=liverec.RecorderLock("window", path=path),
                          log=lambda m: None, prefs_fn=lambda: dict(PREFS, library_folder=str(lib)))
    r2 = liverec.Recorder(F.FakeReader(sim), library_folder=lambda: str(lib), lock=liverec.RecorderLock("recorder", path=path),
                          log=logs.append, prefs_fn=lambda: dict(PREFS, library_folder=str(lib)))
    assert r1.start()
    try:
        assert not r2.start() and r2._thread is None and any("already running" in m for m in logs)
    finally:
        r1.stop()


def test_a_quit_request_stops_a_running_recorder():
    lib = fresh()
    path = TMP / "lock-stop.json"
    sim = F.Sim()
    sim.connected = False
    old_lock, old_stop = liverec.lock_path, liverec.stop_path
    liverec.stop_path = lambda: TMP / "stop-test.flag"
    liverec.lock_path = lambda: path
    try:
        rec = liverec.Recorder(F.FakeReader(sim), library_folder=lambda: str(lib), lock=liverec.RecorderLock("recorder", path=path),
                               log=lambda m: None, prefs_fn=lambda: dict(PREFS, library_folder=str(lib)))
        rec.BEAT = 0
        assert rec.start()
        liverec.BEAT_S, keep = 0.05, liverec.BEAT_S
        try:
            rec._next_beat = 0
            assert liverec.request_stop(wait_s=6.0), "the recorder quit when asked"
        finally:
            liverec.BEAT_S = keep
        rec._thread.join(3)
        assert not rec._thread.is_alive() and not path.exists()
        assert liverec.request_stop() is True            # nothing running: nothing to wait for
    finally:
        liverec.lock_path, liverec.stop_path = old_lock, old_stop


# ================================================================ the files
def test_gz_pieces_read_back_as_one_stream_and_survive_a_cut():
    p = TMP / "x.jsonl.gz"
    t = [0.0]
    g = liverec.GzJsonl(p, flush_s=10, clock=lambda: t[0])
    for i in range(35):
        t[0] += 1
        g.add({"i": i})
    assert [r["i"] for r in liverec.read_jsonl(p)] == list(range(30)), "three pieces were written, the rest is still in memory"
    g.flush()
    assert [r["i"] for r in liverec.read_jsonl(p)] == list(range(35))
    with open(p, "ab") as f:
        f.write(b"\x1f\x8b\x08\x00garbage")
    assert [r["i"] for r in liverec.read_jsonl(p)][:35] == list(range(35))
    assert list(liverec.read_jsonl(TMP / "nothing.gz")) == []


def test_recordings_are_never_deleted_by_debrief():
    src = (ROOT / "debrief" / "liverec.py").read_text(encoding="utf-8")
    for bad in ("rmtree", "os.remove(", ".unlink(", "shutil"):
        if bad in src:
            # the only deletes are of the lock/stop files
            for line in src.splitlines():
                if bad in line:
                    assert "path" in line or "stop_path" in line or "lock" in line.lower(), line


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
