"""Debrief 2.27 "Live data": recordings beside Garage 61 sessions, the "From iRacing" section, Settings > Live data,
Start with Windows (the Run key, behind a stand-in winreg), the tray menu, the --recorder flags and the packaging files.

    python3 tests/ci_run.py tests/test_227_library.py

No network; temp folders from tempfile; the browser test is skipped without Playwright + Chromium.
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import tempfile
import threading
import traceback
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"

TMP = Path(tempfile.mkdtemp(prefix="debrief-227-lib-"))
LIB = TMP / "lib"
(LIB / "reviews").mkdir(parents=True)
ST = TMP / "settings.toml"
ST.write_text(f'token = "test-token-1234567890"\nlibrary_folder = \'{LIB}\'\n', encoding="utf-8")
os.environ.update(DEBRIEF_SETTINGS=str(ST), DEBRIEF_CACHE_DIR=str(TMP / "cache"),
                  DEBRIEF_SCHEDULE_DIR=str(TMP / "sched"), DEBRIEF_UPDATE_STATE=str(TMP / "update.json"))

import requests  # noqa: E402


def _blocked(*a, **k):
    raise requests.ConnectionError("network blocked in tests")


requests.get = _blocked
requests.post = _blocked

import fake_live as F  # noqa: E402
import make_fixture_library as MFL  # noqa: E402
from debrief import liverec, livetray, recordings, report  # noqa: E402

PREFS = {"record_sessions": True, "start_with_windows": True, "library_folder": str(LIB)}
TRACK, CONFIG, CAR = "Charlotte Motor Speedway", "Roval 2019", "Ford Mustang GT4"
START = "2026-10-01T23:30:00+00:00"        # the fixture review's start (MFL.RACES)


def record(kind="race", lib=LIB, seconds=330, sub=89056204, base=None, **kw):
    """Run the scripted sim through a recorder into lib; returns (folder, sim)."""
    sim = F.Sim(kind=kind, sub=sub, track=TRACK, config=CONFIG, car=CAR, laps=5, **kw)
    if base:
        sim.base_utc = base
    rd = F.FakeReader(sim)
    rec = liverec.Recorder(rd, library_folder=lambda: str(lib), use_lock=False, clock=lambda: sim.now, wall=lambda: sim.wall,
                           prefs_fn=lambda: dict(PREFS, library_folder=str(lib)), utc=sim.utc, log=lambda m: None)
    end = sim.t + seconds
    while sim.t < end:
        sim.step(0.05)
        rec.tick()
    rec._end_session("test")
    return rec.finished[0], sim


def g61(start, type_=3, track="Charlotte Motor Speedway (Roval 2019)", car="Ford Mustang GT4", **kw):
    return dict({"key": f"ev|{type_}|{start}", "event": "EV" + start[-8:], "track_id": 468, "car_id": 190, "track": track, "car": car,
                 "type": type_, "session": {1: "Practice", 2: "Qualifying", 3: "Race"}[type_], "start": start, "laps": 12,
                 "clean_laps": 10, "best": 85.2, "best_clean": 85.3}, **kw)


# ================================================================ matching and merging
def test_matched_session_gains_the_recording_and_is_not_listed_twice():
    lib = TMP / "m1"
    record(lib=lib, seconds=330)
    secs = recordings.sections(lib)
    assert len(secs) == 1 and secs[0]["kind"] == 3 and secs[0]["subsession"] == 89056204
    rows = [g61("2026-10-01T23:31:10+00:00"), g61("2026-09-01T23:31:10+00:00")]
    out = recordings.merge_rows(rows, secs)
    assert len(out) == 2, "the recording joined a Garage 61 row: no extra row"
    hit = next(r for r in out if r["start"].startswith("2026-10-01"))
    assert hit["rec"]["id"].endswith("#2") and "fuel" in hit["rec"]["line"] and hit["source"] == "garage61" and not hit.get("recorded_only")
    assert "rec" not in next(r for r in out if r["start"].startswith("2026-09-01"))


def test_unmatched_recording_is_its_own_row_marked_recorded_by_debrief():
    lib = TMP / "m2"
    record(lib=lib, seconds=330)
    secs = recordings.sections(lib)
    cases = {
        "other track": [g61("2026-10-01T23:31:00+00:00", track="Watkins Glen International (Boot)")],
        "other car": [g61("2026-10-01T23:31:00+00:00", car="Porsche 911 GT3 R (992)")],
        "other kind": [g61("2026-10-01T23:31:00+00:00", type_=1)],
        "an hour away": [g61("2026-10-02T00:40:00+00:00")],
        "none at all": [],
    }
    for why, rows in cases.items():
        out = recordings.merge_rows(rows, secs)
        mine = [r for r in out if r.get("recorded_only")]
        assert len(out) == len(rows) + 1 and len(mine) == 1, why
        r = mine[0]
        assert r["source"] == "recorded" and r["type"] == 3 and r["session"] == "Race"
        assert r["car"] == CAR and r["review"] is None and r["event"] is None and r["rec"]["id"] == secs[0]["id"]
    # inside ten minutes either side still joins
    for start in ("2026-10-01T23:21:30+00:00", "2026-10-01T23:39:00+00:00", "2026-10-01T23:45:00+00:00"):
        assert len(recordings.merge_rows([g61(start)], secs)) == 1, start
    assert len(recordings.merge_rows([g61("2026-10-01T23:19:00+00:00")], secs)) == 2


def test_each_side_is_used_once_and_the_nearest_wins():
    lib = TMP / "m3"
    record(lib=lib, seconds=330)
    secs = recordings.sections(lib)
    near, far = g61("2026-10-01T23:30:20+00:00"), g61("2026-10-01T23:38:00+00:00", key="other")
    out = recordings.merge_rows([far, near], secs)
    assert len(out) == 2 and [bool(r.get("rec")) for r in sorted(out, key=lambda r: r["start"])] == [True, False]
    # two recordings of the same hour and one Garage 61 row: one joins, the other stands alone
    secs2 = secs + [dict(secs[0], id="x#2", dir="x", start="2026-10-01T23:35:00+00:00", end="2026-10-01T23:50:00+00:00")]
    out = recordings.merge_rows([near], secs2)
    assert len(out) == 2 and sum(1 for r in out if r.get("recorded_only")) == 1
    ids = [r["rec"]["id"] for r in out]
    assert len(set(ids)) == 2, "never shown twice"


def test_a_subsession_on_the_garage_61_row_is_matched_exactly_first():
    lib = TMP / "m4"
    record(lib=lib, seconds=330)
    secs = recordings.sections(lib)
    row = g61("2026-10-01T23:33:00+00:00", subsession=89056204, track="Some Other Name", car="Another Car")
    assert len(recordings.merge_rows([row], secs)) == 1, "the subsession id joins even when the names differ"
    row2 = dict(row, subsession=1)
    assert len(recordings.merge_rows([row2], secs)) == 2


def test_section_kinds():
    k = recordings.kind_of
    assert [k("Practice"), k("Open Practice"), k("Lone Qualify"), k("Open Qualify"), k("Race"), k("Warmup"), k("Offline Testing"), k("")] == \
        [1, 1, 2, 2, 3, 1, 1, None]


# ================================================================ the words
def test_view_uses_plain_words_and_names_its_source():
    lib = TMP / "v1"
    folder, _ = record(lib=lib, seconds=330)
    sec, sm = recordings.find(lib, f"{folder.name}#2")
    v = recordings.view(sec, sm)
    assert v["title"] == "From iRacing" and "straight from iRacing" in v["lede"] and "Garage 61" in v["lede"]
    heads = [b["h"] for b in v["blocks"]]
    assert heads[:2] == ["Fuel", "Fuel lap by lap"] and "Driver aids" in heads and "Incidents" in heads
    assert "Your position over the race" in heads and "Pit stops, every car" in heads and "Your pit stops" in heads
    fuel = v["blocks"][0]
    assert "3.00 L (0.79 gal)" in fuel["lines"][0], fuel["lines"][0]
    assert fuel["table"]["cols"] == ["Stint", "Laps", "Fuel used", "Per lap"] and len(fuel["table"]["rows"]) == 2
    pits = next(b for b in v["blocks"] if b["h"] == "Pit stops, every car")["table"]["rows"]
    assert pits[0][2] == "1" and pits[0][3] == "3", pits                      # a car with one stop, on lap 3
    assert any("Lap numbers" in n for n in v["notes"])
    text = json.dumps(v)
    assert "mph" not in text and "NaN" not in text
    alone = recordings.standalone_view(sec, sm)
    assert "Garage 61 doesn't have this session" in alone["lede"] and alone["blocks"][0]["h"] == "Laps"
    # a car without aids says so instead of showing an empty table
    folder2, _ = record(lib=lib, seconds=130, aids=False, sub=2)
    sec2, sm2 = recordings.find(lib, f"{folder2.name}#2")
    a = next(b for b in recordings.view(sec2, sm2)["blocks"] if b["h"] == "Driver aids")
    assert "didn't report" in a["lines"][0] and a["table"] is None


def test_view_notes_ibt_off_and_recovered_recordings():
    sm = {"folder": "f", "final": True, "ended_by": "recovered after Debrief or the PC stopped suddenly",
          "disk_logging_note": "iRacing's own telemetry files (.ibt) were switched off during this session."}
    sec = {"num": 0, "type": "Practice", "fuel": {}, "aids": {}, "incidents": {"total": 0}, "pit_stops": []}
    v = recordings.view(sec, sm)
    assert any(".ibt" in n for n in v["notes"]) and any("stopped suddenly" in n for n in v["notes"])
    assert recordings.view(sec, dict(sm, final=False, ended_by=""))["notes"][1].startswith("This recording is still being written")


# ================================================================ reviews
def _review_dir(lib, name="2026-10-01_charlotte_ford_ev1"):
    d = lib / "reviews" / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _fixture_review(session="Race"):
    import random
    d = MFL.fake_review(MFL.ROVAL, START, 0.4, True, MFL.S4, 1100, None, random.Random(7))
    d["meta"]["session"] = session
    return d


def test_a_review_written_after_the_recording_gets_the_section_and_the_page_draws_it():
    lib = TMP / "r1"
    record(lib=lib, seconds=330)
    d = _review_dir(lib)
    report.write(d, _fixture_review())
    data = json.loads((d / "review.json").read_text(encoding="utf-8"))
    fi = data["from_iracing"]
    assert fi["title"] == "From iRacing" and fi["final"] is True and fi["blocks"][0]["h"] == "Fuel"
    page = (d / "index.html").read_text(encoding="utf-8")
    assert 'id="fromIracing"' in page and "Pit stops, every car" in page
    # a practice review of the same combination does not get a race recording
    d2 = _review_dir(lib, "practice")
    report.write(d2, _fixture_review("Practice"))
    assert "from_iracing" not in json.loads((d2 / "review.json").read_text(encoding="utf-8"))
    # no recording folder: nothing added, nothing broken
    other = TMP / "r1b"
    d3 = _review_dir(other)
    report.write(d3, _fixture_review())
    assert "from_iracing" not in json.loads((d3 / "review.json").read_text(encoding="utf-8"))
    # a review that already has the section keeps it when the recording can't be read this time
    shutil.rmtree(lib / "Recorded")
    report.write(d, json.loads((d / "review.json").read_text(encoding="utf-8")))
    assert "from_iracing" in json.loads((d / "review.json").read_text(encoding="utf-8"))


def test_a_review_saved_before_its_recording_gains_the_section_later():
    lib = TMP / "r2"
    d = _review_dir(lib)
    report.write(d, _fixture_review())
    assert "from_iracing" not in json.loads((d / "review.json").read_text(encoding="utf-8"))
    record(lib=lib, seconds=330)
    logs = []
    assert recordings.attach_pending(lib, log=logs.append) == 1, logs
    assert "from_iracing" in json.loads((d / "review.json").read_text(encoding="utf-8"))
    assert recordings.attach_pending(lib) == 0, "done once"


def test_review_page_template_has_the_section_and_draws_only_text():
    t = (ROOT / "debrief" / "templates" / "review.html").read_text(encoding="utf-8")
    assert 'id="fromIracing"' in t and "D.from_iracing" in t
    mark = t.index("D.from_iracing")
    chunk = t[mark:mark + 2500]
    assert "innerHTML" not in chunk and "textContent" in chunk or "h('" in chunk
    assert t.index('id="fromIracing"') < t.index('<details class="deep"'), "up front, not folded away"
    assert report.LAYOUT == "2.28.1", "2.28.1 re-renders saved reviews once (review.html changed); reviews with a recording are rewritten by attach_pending; the rest need no re-render"


# ================================================================ endpoints
_SRV = {}
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def srv():
    if "s" not in _SRV:
        from debrief.desktop import server as S
        s = S.DebriefServer(heartbeat_timeout=3600)
        threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
        _SRV["s"] = s
    return _SRV["s"]


def call(rel, body=None, headers=None):
    h = {"X-Debrief": "1", "Content-Type": "application/json"} if headers is None else headers
    rq = urllib.request.Request(srv().url + rel, data=None if body is None else json.dumps(body).encode("utf-8"),
                                method="GET" if body is None else "POST", headers=h)
    try:
        with OP.open(rq, timeout=20) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}")
        except ValueError:
            return e.code, {}


def _seed_sessions(rows):
    from debrief import app, sync
    root = sync.Store.for_api(app.make_api(app.load_settings())).root
    root.mkdir(parents=True, exist_ok=True)
    (root / "state.json").write_text(json.dumps({"schema": 1, "first_complete": True, "last_success": "2026-10-02T00:00:00+00:00",
                                                 "combos": {}}), encoding="utf-8")
    (root / "sessions.json").write_text(json.dumps({"schema": 1, "sessions": rows, "laps": 24}), encoding="utf-8")


def test_api_sessions_lists_garage_61_and_recorded_rows_once():
    from debrief import sync
    if not hasattr(sync, "Store"):
        return
    record(lib=LIB, seconds=330, sub=111)
    secs = recordings.sections(LIB)
    assert len(secs) == 1
    rows = [g61("2026-10-01T23:31:10+00:00"), g61("2026-09-01T23:31:10+00:00", track="Watkins Glen International (Boot)", car="Porsche 911 GT3 R (992)")]
    _seed_sessions(rows)
    code, j = call("api/sessions")
    assert code == 200 and j["ok"] and j["total"] == 2, j.get("total")
    joined = next(r for r in j["sessions"] if r["start"].startswith("2026-10-01"))
    assert joined["rec"]["id"] == secs[0]["id"] and joined["source"] == "garage61"
    # a recording with nothing to join
    record(lib=LIB, seconds=130, kind="practice", sub=222, base=__import__("datetime").datetime(2026, 11, 3, 18, 0, tzinfo=__import__("datetime").timezone.utc))
    code, j = call("api/sessions")
    assert j["total"] == 3
    own = next(r for r in j["sessions"] if r.get("recorded_only"))
    assert own["session"] == "Practice" and own["source"] == "recorded" and own["review"] is None and own["start"].startswith("2026-11-03")
    assert j["sessions"][0]["recorded_only"], "newest first"
    code, j = call("api/sessions?offset=1&limit=1")
    assert j["total"] == 3 and len(j["sessions"]) == 1 and j["more"] is True
    rid = own["rec"]["id"]
    code, v = call("api/recording?id=" + urllib.request.quote(rid) + "&alone=1")
    assert code == 200 and v["view"]["blocks"][0]["h"] == "Laps" and "Garage 61 doesn't have" in v["view"]["lede"]
    code, v = call("api/recording?id=" + urllib.request.quote(rid))
    assert code == 200 and v["view"]["blocks"][0]["h"] == "Fuel"
    assert call("api/recording?id=nope")[0] == 400 and call("api/recording?id=..%2F..%2Fx%231")[0] == 400
    assert call("api/recording?id=" + urllib.request.quote(rid), headers={})[0] == 404


def test_ui_labels_every_number_with_its_source():
    js = (ROOT / "debrief" / "desktop" / "ui" / "app.js").read_text(encoding="utf-8")
    for want in ("'from iRacing'", "'from Garage 61'", "Recorded by Debrief", "From iRacing: ", "toggleFromIracing"):
        assert want in js, want
    html = (ROOT / "debrief" / "desktop" / "ui" / "index.html").read_text(encoding="utf-8")
    for want in ("Record sessions in the background", "Start with Windows", 'id="recStatus"', 'id="recPath"'):
        assert want in html, want


# ================================================================ settings
def test_settings_toggles_validate_save_and_default_on():
    from debrief.desktop import settings_io
    v = settings_io.public_view()
    assert v["record_sessions"] is True and v["start_with_windows"] is True
    assert v["recordings_folder"] == str(Path(LIB) / "Recorded") and v["recorder"]["text"] in ("Not running",) or v["recorder"]["running"]
    v = settings_io.save({"record_sessions": False, "start_with_windows": False})
    assert v["record_sessions"] is False and v["start_with_windows"] is False
    from debrief import app
    cfg = app.load_settings()
    assert cfg["record_sessions"] is False and cfg["start_with_windows"] is False
    assert liverec.prefs()["record_sessions"] is False
    for bad in ("false", 0, None, "yes"):
        try:
            settings_io.save({"record_sessions": bad})
            raise AssertionError("accepted " + repr(bad))
        except ValueError as e:
            assert "must be on or off" in str(e)
    v = settings_io.save({"record_sessions": True, "start_with_windows": True})
    assert v["record_sessions"] is True and v["start_with_windows"] is True
    assert "token" not in json.dumps(settings_io.live_view()) and set(settings_io.live_view()) == {"record_sessions", "start_with_windows", "ensure_telemetry", "recordings_folder"}


def test_api_settings_and_live_status():
    code, j = call("api/settings", {"record_sessions": False})
    assert code == 200 and j["settings"]["record_sessions"] is False
    code, j = call("api/settings", {"record_sessions": "off"})
    assert code == 400
    call("api/settings", {"record_sessions": True})
    code, j = call("api/live")
    assert code == 200 and j["ok"] and j["settings"]["record_sessions"] is True and j["recorder"]["text"]
    # the window's own recorder is what it reports
    sim = F.Sim()
    sim.connected = False
    rec = liverec.Recorder(F.FakeReader(sim), library_folder=lambda: str(LIB), use_lock=False, log=lambda m: None,
                           prefs_fn=lambda: dict(PREFS))
    rec.has_lock = True
    rec.tick()
    srv().recorder = rec
    try:
        code, j = call("api/live")
        assert j["recorder"]["text"] == "Waiting for iRacing" and j["recorder"]["running"] is True and j["recorder"]["by"] == "window"
        rec.pause()
        rec.tick()
        assert call("api/live")[1]["recorder"]["text"] == "Paused"
        rec.resume()
        code, d = call("api/diagnostics")
        assert "recorder_perf" in d
    finally:
        srv().recorder = None
        liverec.set_paused(False)
    code, j = call("api/live", headers={})
    assert code == 404


def test_status_words_when_no_recorder_runs_or_another_one_does():
    path = liverec.lock_path()
    try:
        path.unlink()
    except OSError:
        pass
    assert liverec.status_view() == {"state": "off", "text": "Not running", "running": False, "by": None}
    lock = liverec.RecorderLock("recorder")
    assert lock.acquire()
    lock.beat({"state": "recording", "text": "Recording Spa practice, 12 laps", "disk_logging": False})
    st = liverec.status_view()
    assert st["running"] and st["text"] == "Recording Spa practice, 12 laps" and st["by"] == "recorder" and st["disk_logging"] is False
    lock.release()
    assert liverec.status_view()["text"] == "Not running"


# ================================================================ start with Windows
class FakeWinreg:
    HKEY_CURRENT_USER = "HKCU"
    KEY_READ, KEY_SET_VALUE, REG_SZ = 1, 2, 1

    def __init__(self, fail=False):
        self.keys, self.fail, self.calls = {}, fail, []

    class _K:
        def __init__(self, vals):
            self.vals = vals

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def CreateKeyEx(self, root, path, res, access):
        if self.fail:
            raise PermissionError(5, "denied")
        self.calls.append(("create", root, path))
        return self._K(self.keys.setdefault((root, path), {}))

    def OpenKey(self, root, path, res=0, access=1):
        if (root, path) not in self.keys:
            raise FileNotFoundError(2, "no key")
        return self._K(self.keys[(root, path)])

    def SetValueEx(self, k, name, res, typ, value):
        self.calls.append(("set", name, value))
        k.vals[name] = (value, typ)

    def QueryValueEx(self, k, name):
        if name not in k.vals:
            raise FileNotFoundError(2, "no value")
        return k.vals[name]

    def DeleteValue(self, k, name):
        if name not in k.vals:
            raise FileNotFoundError(2, "no value")
        self.calls.append(("delete", name))
        del k.vals[name]


def test_run_key_is_written_and_removed_behind_a_fake_registry():
    wr = FakeWinreg()
    cmd = liverec.autostart_command(r"C:\Users\x\AppData\Local\Programs\Debrief\Debrief.exe", frozen=True)
    assert cmd == r'"C:\Users\x\AppData\Local\Programs\Debrief\Debrief.exe" --recorder'
    assert liverec.set_autostart(True, wr, cmd) is True
    assert wr.keys[("HKCU", liverec.RUN_KEY)] == {"Debrief Recorder": (cmd, 1)} and liverec.RUN_KEY.endswith(r"CurrentVersion\Run")
    assert liverec.autostart_value(wr) == cmd
    assert liverec.set_autostart(False, wr) is True and liverec.autostart_value(wr) is None
    assert liverec.set_autostart(False, wr) is True, "removing what is not there is fine"
    assert liverec.set_autostart(True, FakeWinreg(fail=True), cmd) is False
    # from source: pythonw -m debrief.desktop --recorder
    assert liverec.autostart_command(sys.executable, frozen=False).endswith("-m debrief.desktop --recorder")
    # off Windows (no winreg) nothing happens
    assert liverec.set_autostart(True) in (False, True) if os.name == "nt" else liverec.set_autostart(True) is False


def test_the_run_key_follows_settings_and_is_on_after_the_update():
    from debrief.desktop import settings_io
    wr = FakeWinreg()
    settings_io.save({"start_with_windows": True})
    cmd = r'"C:\D\Debrief.exe" --recorder'
    assert liverec.reconcile_autostart(wr, cmd) == "on" and liverec.autostart_value(wr) == cmd
    assert liverec.reconcile_autostart(wr, cmd) == "unchanged"
    assert liverec.reconcile_autostart(wr, r'"C:\E\Debrief.exe" --recorder') == "on", "a moved install is repointed"
    settings_io.save({"start_with_windows": False})
    assert liverec.reconcile_autostart(wr, cmd) == "off" and liverec.autostart_value(wr) is None
    assert liverec.reconcile_autostart(wr, cmd) == "unchanged"
    assert liverec.reconcile_autostart(None) in ("unavailable", "unchanged", "on", "off")
    settings_io.save({"start_with_windows": True})
    # a first run after the update: no start_with_windows key in settings.toml at all means ON
    text = ST.read_text(encoding="utf-8")
    ST.write_text("\n".join(l for l in text.splitlines() if not l.startswith("start_with_windows")) + "\n", encoding="utf-8")
    assert liverec.prefs()["start_with_windows"] is True
    wr2 = FakeWinreg()
    assert liverec.reconcile_autostart(wr2, cmd) == "on" and liverec.load_state().get("autostart_applied") is True


def test_the_api_applies_the_run_key_when_the_switch_changes():
    wr = FakeWinreg()
    orig = liverec._winreg
    liverec._winreg = lambda: wr
    try:
        code, j = call("api/settings", {"start_with_windows": True})
        assert code == 200 and liverec.autostart_value(wr) is not None
        code, j = call("api/settings", {"start_with_windows": False})
        assert code == 200 and liverec.autostart_value(wr) is None
        call("api/settings", {"start_with_windows": True})
    finally:
        liverec._winreg = orig


# ================================================================ tray and flags
def test_tray_menu_has_open_pause_resume_quit():
    calls = []
    sim = F.Sim()
    sim.connected = False
    rec = liverec.Recorder(F.FakeReader(sim), library_folder=lambda: str(LIB), use_lock=False, log=lambda m: None, prefs_fn=lambda: dict(PREFS))
    liverec.set_paused(False)
    m = livetray.TrayModel(rec, lambda: calls.append("open"), lambda: calls.append("quit"))
    assert [l for _i, l in m.items()] == ["Open Debrief", "Pause recording", "Quit recorder"]
    m.activate(livetray.PAUSE)
    assert rec.is_paused() and [l for _i, l in m.items()][1] == "Resume recording"
    m.activate(livetray.PAUSE)
    assert not rec.is_paused()
    m.activate(livetray.OPEN)
    m.activate(livetray.QUIT)
    assert calls == ["open", "quit"]
    rec.tick()
    assert m.tooltip() == "Debrief: Waiting for iRacing" and len(m.tooltip()) <= 127


class FakeWin:
    """win32gui / win32con / win32api stand-ins that record what the tray does."""
    NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
    NIF_ICON, NIF_MESSAGE, NIF_TIP = 2, 1, 4
    MF_STRING, TPM_LEFTALIGN, TPM_RIGHTBUTTON, WM_NULL = 0, 0, 2, 0
    WM_RBUTTONUP, WM_LBUTTONDBLCLK, WM_COMMAND, WM_TIMER, WM_DESTROY = 517, 515, 273, 275, 2
    IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE, IDI_APPLICATION = 1, 16, 64, 32512

    def __init__(self):
        self.log, self.menu = [], []

    class WNDCLASS:
        pass

    def GetModuleHandle(self, n):
        return 1

    def RegisterClass(self, wc):
        self.wc = wc
        return 99

    def CreateWindow(self, *a):
        return 7

    def RegisterWindowMessage(self, n):
        return 0xC000

    def LoadImage(self, *a):
        return 5

    def LoadIcon(self, *a):
        return 6

    def Shell_NotifyIcon(self, what, nid):
        self.log.append(("nim", what, nid[0]))

    def SetTimer(self, *a):
        self.log.append(("timer",))

    def PumpMessages(self):
        self.log.append(("pump",))

    def PostQuitMessage(self, n):
        self.log.append(("quit",))

    def CreatePopupMenu(self):
        return "menu"

    def AppendMenu(self, m, flags, i, label):
        self.menu.append((i, label))

    def GetCursorPos(self):
        return (1, 2)

    def SetForegroundWindow(self, h):
        pass

    def TrackPopupMenu(self, *a):
        self.log.append(("popup",))

    def PostMessage(self, *a):
        pass

    def DefWindowProc(self, *a):
        return 0


def test_tray_window_adds_an_icon_and_routes_clicks():
    w = FakeWin()
    sim = F.Sim()
    sim.connected = False
    rec = liverec.Recorder(F.FakeReader(sim), library_folder=lambda: str(LIB), use_lock=False, log=lambda m: None, prefs_fn=lambda: dict(PREFS))
    opened, quit_ = [], []
    liverec.set_paused(False)
    assert livetray.run(rec, lambda: opened.append(1), quit_cb=lambda: quit_.append(1), gui=w, con=w, api=w) == 0
    assert ("nim", w.NIM_ADD, 7) in w.log and ("pump",) in w.log and ("timer",) in w.log
    proc = w.wc.lpfnWndProc
    proc(7, livetray.WM_TRAY, 0, w.WM_RBUTTONUP)
    assert [l for _i, l in w.menu] == ["Open Debrief", "Pause recording", "Quit recorder"] and ("popup",) in w.log
    proc(7, w.WM_COMMAND, livetray.PAUSE, 0)
    assert rec.is_paused()
    proc(7, w.WM_COMMAND, livetray.PAUSE, 0)
    proc(7, livetray.WM_TRAY, 0, w.WM_LBUTTONDBLCLK)
    assert opened == [1]
    proc(7, w.WM_COMMAND, livetray.QUIT, 0)
    assert ("nim", w.NIM_DELETE, 7) in w.log and ("quit",) in w.log and quit_ == [1]


def test_livetray_imports_without_pywin32():
    code = "import sys; sys.modules['win32gui']=None; sys.modules['win32api']=None; import debrief.livetray as t; print('ok')"
    import subprocess
    r = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), capture_output=True, text=True, timeout=60)
    assert "ok" in r.stdout, r.stderr
    try:
        sys.modules["win32gui"] = None
        sys.modules["win32api"] = None
        sys.modules["win32con"] = None
        try:
            livetray.run(None, lambda: None)
            raise AssertionError("expected ImportError")
        except ImportError:
            pass
    finally:
        for k in ("win32gui", "win32api", "win32con"):
            sys.modules.pop(k, None)


def test_recorder_flags_are_wired_through_main():
    from debrief import desktop
    seen = []
    orig = desktop.run_recorder
    desktop.run_recorder = lambda: seen.append("recorder") or 0
    try:
        assert desktop.main(["--recorder"]) == 0 and seen == ["recorder"]
    finally:
        desktop.run_recorder = orig
    assert desktop.main(["--stop-recorder"]) == 0, "nothing running: nothing to wait for"


def test_run_recorder_without_the_tray_records_until_asked_to_quit():
    from debrief import desktop
    real = liverec.Recorder
    made = []

    class Quick(real):
        def __init__(self, **k):
            sim = F.Sim()
            sim.connected = False
            super().__init__(F.FakeReader(sim), library_folder=lambda: str(LIB), role=k.get("role", "recorder"),
                             lock=liverec.RecorderLock("recorder", path=TMP / "lock-flags.json"), log=lambda m: None,
                             prefs_fn=lambda: dict(PREFS), on_finished=k.get("on_finished"))
            made.append(self)
    liverec.Recorder = Quick
    sys.modules["win32gui"] = None
    sys.modules["win32api"] = None
    sys.modules["win32con"] = None
    try:
        t = threading.Thread(target=lambda: made.append(desktop.run_recorder()), daemon=True)
        t.start()
        import time
        end = time.time() + 5
        while not made and time.time() < end:
            time.sleep(0.05)
        rec = made[0]
        time.sleep(0.4)
        assert rec._thread.is_alive()
        rec._stop.set()
        t.join(8)
        assert not t.is_alive() and made[-1] == 0
    finally:
        liverec.Recorder = real
        for k in ("win32gui", "win32api", "win32con"):
            sys.modules.pop(k, None)


def test_window_hosts_the_recorder_unless_another_one_runs():
    from debrief import desktop
    path = liverec.lock_path()
    try:
        path.unlink()
    except OSError:
        pass
    other = liverec.RecorderLock("recorder")
    assert other.acquire()
    other.beat({"state": "recording", "text": "Recording Spa practice, 3 laps"})
    try:
        assert desktop.host_recorder() is None, "a tray recorder is running: the window does not start a second reader"
    finally:
        other.release()
    rec = desktop.host_recorder()
    try:
        assert rec is not None and rec.role == "window" and rec.has_lock
        assert liverec.status_view()["by"] == "window"
        assert desktop.host_recorder() is None, "and a second window finds the first one's recording"
    finally:
        rec.stop()
    assert liverec.status_view()["running"] is False



# ================================================================ in the window (needs Playwright + Chromium)
def _browser(fn):
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # noqa: BLE001
        print("  (skipped: no Playwright)")
        return
    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch()
            pg = b.new_page(viewport={"width": 1280, "height": 900})
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.route("**/fonts.googleapis.com/**", lambda r: r.abort())
            pg.route("**/fonts.gstatic.com/**", lambda r: r.abort())
            fn(pg)
            b.close()
            assert not errs, errs
    except Exception as e:  # noqa: BLE001
        if "Executable doesn't exist" in str(e):
            print("  (skipped: no Chromium)")
            return
        raise


def test_window_settings_live_data_and_sessions_from_iracing():
    from debrief import sync  # noqa: F401
    record(lib=LIB, seconds=330, sub=333, base=__import__("datetime").datetime(2026, 10, 20, 20, 0, tzinfo=__import__("datetime").timezone.utc))
    _seed_sessions([g61("2026-10-20T20:01:10+00:00")])
    srv().recorder = None

    def go(pg):
        pg.goto(srv().url)
        pg.wait_for_selector(".navbtn[data-nav=settings]", timeout=15000)
        pg.click(".navbtn[data-nav=settings]")
        pg.wait_for_selector("#fLive", timeout=10000)
        assert pg.is_checked("#recOn") and pg.is_checked("#recWin")
        pg.wait_for_function("() => document.getElementById('recStatus').textContent.length > 0", timeout=5000)
        assert pg.text_content("#recStatus") == "Not running"
        assert "Recorded" in pg.text_content("#recPath") and "never deletes" in pg.text_content("#recPath")
        pg.uncheck("#recWin")      # 2.28: a switch saves by itself
        pg.wait_for_function("() => document.getElementById('recMsg').textContent.startsWith('Saved')", timeout=5000)
        assert call("api/settings")[1]["start_with_windows"] is False
        pg.check("#recWin")
        pg.wait_for_function("() => document.getElementById('recMsg').textContent.startsWith('Saved')", timeout=5000)
        # the session list: one row, the Garage 61 numbers labelled, the recording joined to it
        pg.click(".navbtn[data-nav=sessions]")
        pg.wait_for_selector("#sessList .item.sess", timeout=10000)
        rows = pg.locator("#sessList .item.sess")
        assert rows.count() >= 1
        txt = pg.text_content("#sessList")
        assert "from Garage 61" in txt and "From iRacing: fuel" in txt, txt[:400]
        btn = pg.locator("#sessList .item.sess", has_text="(from Garage 61)").locator("button", has_text="From iRacing").first
        btn.click()
        pg.wait_for_selector(".fipanel h4", timeout=5000)
        panel = pg.text_content(".fipanel")
        assert "Fuel" in panel and "Pit stops, every car" in panel and "straight from iRacing" in panel
        btn.click()
        assert pg.locator(".fipanel").count() == 0
    _browser(go)


def test_window_review_page_shows_the_from_iracing_section():
    lib = TMP / "b2"
    record(lib=lib, seconds=330)
    d = _review_dir(lib)
    report.write(d, _fixture_review())

    def go(pg):
        pg.goto((d / "index.html").as_uri())
        pg.wait_for_selector("#fromIracing:not([hidden])", timeout=15000)
        assert pg.text_content("#fiTitle") == "From iRacing"
        heads = pg.eval_on_selector_all("#fiBody h3", "els => els.map(e => e.textContent)")
        assert heads[0] == "Fuel" and "Pit stops, every car" in heads, heads
        assert pg.locator("#fiBody table").count() >= 4
        assert pg.eval_on_selector("#jumpFi", "e => !e.hidden")
    _browser(go)


# ================================================================ packaging
def test_packaging_wires_the_recorder_through():
    spec = (ROOT / "packaging" / "debrief.spec").read_text(encoding="utf-8")
    for mod in ("debrief.livedata", "debrief.liverec", "debrief.recordings", "debrief.livetray", "win32gui"):
        assert mod in spec, mod
    iss = (ROOT / "packaging" / "debrief.iss").read_text(encoding="utf-8")
    assert "RegDeleteValue(HKEY_CURRENT_USER" in iss and "Debrief Recorder" in iss and "CurrentVersion\\Run" in iss
    assert "--stop-recorder" in iss and "DebriefRecorderRunning" in iss and '"--recorder"' in iss
    assert "--recorder" in (ROOT / "packaging" / "launcher.py").read_text(encoding="utf-8")
    from debrief import desktop
    for m in ("livedata", "liverec", "recordings", "livetray"):
        assert m in desktop.FEATURE_MODULES, m
    import importlib
    for m in ("livedata", "liverec", "recordings", "livetray"):
        importlib.import_module("debrief." + m)


def test_the_garage_61_token_is_nowhere_in_the_live_modules():
    for f in ("livedata.py", "liverec.py", "recordings.py", "livetray.py"):
        src = (ROOT / "debrief" / f).read_text(encoding="utf-8")
        for bad in ('["token"]', "make_api", "import requests", "Authorization", "settings_io", "read_secret"):
            assert bad not in src, (f, bad)


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
