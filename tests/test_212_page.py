"""Debrief 2.12 window: Setup Lab and the Coach, through the real server (port 0, the session secret and the
X-Debrief header), with fake .ibt files (tests/fake_ibt.py), an .ibt playback instead of live iRacing and FakeVoice
instead of the Windows voice.

    python3 tests/test_212_page.py

No network, temp folders from tempfile, utf-8 everywhere. Browser checks use Playwright + Chromium and are skipped
when either is missing; DEBRIEF_SHOTS=<folder> also saves screenshots of both pages in light and dark.
"""
from __future__ import annotations

import copy
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
TMP = Path(tempfile.mkdtemp(prefix="debrief-212p-"))
os.environ["DEBRIEF_SETTINGS"] = str(TMP / "settings.toml")
os.environ["DEBRIEF_CACHE_DIR"] = str(TMP / "cache")
os.environ["DEBRIEF_SCHEDULE_DIR"] = str(TMP / "schedule")
os.environ["DEBRIEF_UPDATE_STATE"] = str(TMP / "update.json")

import fake_ibt as fi  # noqa: E402
from debrief import app, livecoach, voice as vc  # noqa: E402
from debrief.desktop import server as S  # noqa: E402
from debrief.desktop import settings_io  # noqa: E402

LIB = TMP / "lib"
IRACING = TMP / "iRacing"
TEL = IRACING / "telemetry"
ENV, PW = {}, {}
CAR, TRACK = "mclaren570sgt4", "zolder gp"


def settings(iracing=True):
    LIB.mkdir(parents=True, exist_ok=True)
    TEL.mkdir(parents=True, exist_ok=True)
    extra = f"iracing_folder = '{IRACING}'\n" if iracing else f"iracing_folder = '{TMP / 'nowhere'}'\n"
    Path(os.environ["DEBRIEF_SETTINGS"]).write_text(
        'token = "g61-test-token-ABCDEFGHIJKLMNOP"\n'
        f"library_folder = '{LIB}'\napi_base = \"http://127.0.0.1:9/api/v1/\"\n" + extra, encoding="utf-8")


def setup_b():
    s = copy.deepcopy(fi.SETUP_GT4)
    s["Chassis"]["LeftFront"]["Camber"] = "-3.6 deg"
    s["Chassis"]["RightFront"]["Camber"] = "-3.6 deg"
    s["Chassis"]["Rear"]["WingAngle"] = "3.5 deg"
    return s


def make_files():
    """Two setups at Zolder in the GT4 (B is quicker), and one older file at another track."""
    if "files" in ENV:
        return ENV["files"]
    pace_a = [99.6, 99.5, 99.7, 99.4, 99.6, 99.5, 99.6]
    pace_b = [99.1, 99.0, 99.2, 98.9, 99.1, 99.0, 99.1]
    out = []
    # setup B's left front runs 10 kPa over its partner when hot: a real "Try next" change and the pressure card
    press_b = {"LF": 196, "RF": 186, "LR": 184, "RR": 185}
    for stamp, laps, setup, name, press in (("2026-11-01 18-00-00", pace_a, None, "baseline.sto", None),
                                            ("2026-11-01 19-00-00", pace_a, None, "baseline.sto", None),
                                            ("2026-11-02 18-00-00", pace_b, setup_b(), "less camber.sto", press_b),
                                            ("2026-11-02 19-30-00", pace_b, setup_b(), "less camber.sto", press_b)):
        out.append(fi.make(TEL / f"{CAR}_{TRACK} {stamp}.ibt", laps, setup=setup, setup_name=name, car_path=CAR,
                           track_code=TRACK, car="McLaren 570S GT4", track="Circuit Zolder", pressure=press))
    fi.make(TEL / f"mx5 mx52016_lagunaseca 2026-10-20 18-00-00.ibt", [92.0, 91.8, 91.9, 91.7, 91.9], car="Global Mazda MX-5 Cup",
            car_path="mx5 mx52016", track="WeatherTech Raceway Laguna Seca", track_code="lagunaseca")
    (TEL / "broken 2026-10-01 10-00-00.ibt").write_bytes(b"\0" * 400)
    old = time.time() - 3600              # written an hour ago: the coach skips files iRacing may still be writing
    for f in TEL.iterdir():
        os.utime(f, (old, old))
    ENV["files"] = out
    return out


def srv():
    if "srv" not in ENV:
        settings()
        app.LOG_FILE = str(TMP / "debrief.log")
        s = S.DebriefServer(heartbeat_timeout=3600)
        threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
        ENV["srv"] = s
        ENV["opener"] = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return ENV["srv"]


def call(rel, body=None, header=True, raw=None, ctype=None):
    s = srv()
    h = {"X-Debrief": "1"} if header else {}
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode("utf-8"))
    if data is not None:
        h["Content-Type"] = ctype or "application/json"
    method = "POST" if data is not None else "GET"
    rq = urllib.request.Request(s.url + rel, headers=h, data=data, method=method)
    try:
        with ENV["opener"].open(rq, timeout=60) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body)
        except ValueError:
            return e.code, body


def post(rel, body=None):
    return call(rel, body if body is not None else {})


def wait_job(timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        _c, st = call("api/status")
        if st.get("state") != "running":
            return st
        time.sleep(0.2)
    raise AssertionError("the job didn't finish")


def read_telemetry():
    make_files()
    code, r = post("api/setuplab/read")
    assert code == 200 and r["ok"], r
    st = wait_job()
    assert st["state"] == "done" and st["kind"] == "setuplab", st
    return st


# ================================================================ pure helpers
def test_names_match():
    tm, cm = S.track_names_match, S.car_names_match
    assert tm("Circuit Zolder (Grand Prix)", "Zolder Circuit (Grand Prix)")
    assert tm("Circuit Zolder (Grand Prix)", "Zolder Circuit - Grand Prix") and tm("Circuit Zolder", "Zolder Circuit - Grand Prix")
    assert tm("Circuit de Spa-Francorchamps (Grand Prix Pits)", "Spa-Francorchamps - Grand Prix")
    assert not tm("Watkins Glen International (Boot)", "Watkins Glen International - Classic")
    assert not tm("Okayama International Circuit (Full Course)", "Okayama International Circuit - Short")
    assert not tm("Lime Rock Park (Classic)", "Lime Rock Park - Grand Prix")
    assert not tm("Road America (Full Course)", "Circuit Zolder (Grand Prix)") and not tm("", "Zolder")
    assert cm("McLaren 570S GT4", "Mclaren 570s GT4") and cm("McLaren 570S GT4", "", "mclaren570sgt4")
    assert cm("Global Mazda MX-5 Cup", "Mazda MX-5 Cup") and not cm("Porsche 911 GT3 R", "Porsche 911 GT3 Cup")
    assert not cm("BMW M4 GT4", "BMW M4 GT3") and not cm("", "")
    assert S.ibt_track_label({"name": "Zolder Circuit", "config": "Grand Prix"}) == "Zolder Circuit (Grand Prix)"
    combos = [{"car": "Mclaren 570s GT4", "car_path": CAR, "track": "Zolder Circuit - Grand Prix", "track_code": TRACK}]
    assert S.match_setuplab_combo(combos, "McLaren 570S GT4", "Circuit Zolder (Grand Prix)") is combos[0]
    assert S.match_setuplab_combo(combos, "McLaren 570S GT4", "Road America") is None
    assert S._jsonable({"a": float("nan"), "b": [1.5, float("inf")], "c": (1, 2)}) == {"a": None, "b": [1.5, None], "c": [1, 2]}
    import numpy as np
    assert S._jsonable({"n": np.int64(3), "f": np.float32(1.5), "arr": np.array([1, 2])}) == {"n": 3, "f": 1.5, "arr": [1, 2]}


def test_coach_settings_rules():
    assert S.clean_coach_settings({"in_races": True, "top_n": 3, "volume": 40, "rate": -2}) == \
        {"in_races": True, "top_n": 3, "volume": 40, "rate": -2}
    for bad in (None, [], {}, {"in_races": "yes"}, {"top_n": 4}, {"top_n": 0}, {"volume": 101}, {"rate": 2.5},
                {"volume": True}, {"volume": float("nan")}, {"hotkey": "alt+f4"}, {"sample_hz": 60}, {"muted": True}):
        try:
            S.clean_coach_settings(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad!r}")


def test_iracing_folder_setting():
    srv()
    code, r = post("api/settings", {"iracing_folder": str(TMP / "not-there")})
    assert code == 400 and "doesn't exist" in r["error"], r
    code, r = post("api/settings", {"iracing_folder": str(IRACING)})
    assert code == 200 and r["settings"]["iracing_folder"] == str(IRACING)
    assert r["settings"]["telemetry_folder"] == str(TEL) and r["settings"]["telemetry_folder_ok"] is True
    # the telemetry folder itself works too; empty means Documents\iRacing\telemetry
    assert settings_io.telemetry_folder({"iracing_folder": str(TEL)}) == TEL
    assert settings_io.telemetry_folder({}) == app.documents_dir() / "iRacing" / "telemetry"
    assert settings_io.clean_iracing_folder("") == "" and settings_io.clean_iracing_folder(None) == ""
    for bad in (5, "x" * 500, "C:\\a|b"):
        try:
            settings_io.clean_iracing_folder(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


# ================================================================ Setup Lab
def test_setuplab_before_and_after_reading():
    srv()
    make_files()
    shutil.rmtree(TMP / "cache" / S.SETUPLAB_CACHE, ignore_errors=True)
    code, j = call("api/setuplab/combos")
    assert code == 200 and j["ok"] and j["folder_ok"] and j["indexed"] is False and j["combos"] == [], j
    assert j["folder"] == str(TEL) and j["unread"] == 6, j
    code, j = call("api/setuplab/report?car=" + urllib.parse.quote(CAR) + "&track=" + urllib.parse.quote(TRACK))
    assert code == 400 and "Read your telemetry first" in j["error"], j
    before = {p.name for p in TEL.iterdir()}
    stamps = {p.name: p.stat().st_mtime_ns for p in TEL.iterdir()}
    st = read_telemetry()
    assert st["result"]["files"] == 5 and st["result"]["read"] == 6 and st["result"]["bad"] == 1, st["result"]
    assert any("Reading telemetry file" in (st.get("message") or "") or "telemetry file" in x for x in st["steps"] + [st["message"]])
    assert not any(x.startswith("Reading telemetry file ") for x in st["steps"]), "per-file ticks stay out of the steps"
    # read only: nothing in iRacing's folder was added or changed
    assert {p.name for p in TEL.iterdir()} == before and {p.name: p.stat().st_mtime_ns for p in TEL.iterdir()} == stamps
    assert (TMP / "cache" / S.SETUPLAB_CACHE).is_file()
    code, j = call("api/setuplab/combos")
    assert j["indexed"] and j["files"] == 5 and j["bad"] == 1 and j["unread"] == 0, j
    assert [c["car_path"] for c in j["combos"]] == [CAR, "mx5 mx52016"], "newest first"
    # a second read only reads what's new
    st = read_telemetry()
    assert st["result"]["read"] == 0 and st["result"]["cached"] == 6, st["result"]


def test_setuplab_report():
    srv()
    read_telemetry()
    q = "car=" + urllib.parse.quote(CAR) + "&track=" + urllib.parse.quote(TRACK)
    code, j = call("api/setuplab/report?" + q)
    assert code == 200 and j["ok"], j
    R = j["report"]
    assert R["files"] == 4 and [s["id"] for s in R["setups"]] == ["A", "B"], R["setups"]
    assert "baseline.sto" in R["setups"][0]["label"]
    ch = R["diffs"][0]["changes"]
    assert R["diffs"][0]["from"] == "A" and R["diffs"][0]["to"] == "B"
    assert any(c["both_sides"] and c["label"] == "Front camber" for c in ch), [c["text"] for c in ch]
    assert any(c["label"] == "Rear wing" for c in ch)
    assert R["compare"]["verdict"] and len(R["compare"]["setups"]) == 2
    assert R["series_setup"] == "unknown" and R["series_override"] is None
    assert R["suggestions"]["for_setup"] == "B"
    nx = R["suggestions"]["next"]
    assert nx and nx["kind"] == "change" and nx["rule"] == "axle_pressure" and not nx["check"].startswith("Check"), nx
    hp = [i for i in R["suggestions"]["items"] if i["rule"] == "hot_pressure"]
    assert len(hp) == 1 and hp[0]["setting"] == "Hot tyre pressures" and hp[0]["lines"][0].startswith("Now: left front 196"), hp
    assert R["bad_files"] == [], "the broken file isn't this car's: it's counted once, in the telemetry panel"
    json.dumps(R, allow_nan=False)        # plain JSON: no NaN for the window
    # his own choice wins, and is kept per car + track
    code, r = post("api/setuplab/series", {"car": CAR, "track": TRACK, "setup": "fixed"})
    assert code == 200 and r["setup"] == "fixed"
    R = call("api/setuplab/report?" + q)[1]["report"]
    assert R["series_setup"] == "fixed" and R["series_setup_source"] == "you set this" and R["series_override"] == "fixed"
    assert any(i["rule"] == "fixed_series" for i in R["suggestions"]["items"]) and R["suggestions"]["next"] is None
    assert settings_io.load_extra("setuplab")["series"] == {f"{CAR}|{TRACK}": "fixed"}
    assert [c["series_override"] for c in call("api/setuplab/combos")[1]["combos"]] == ["fixed", None]
    code, r = post("api/setuplab/series", {"car": CAR, "track": TRACK, "setup": ""})
    assert code == 200 and settings_io.load_extra("setuplab")["series"] == {}
    # a car + track with no files
    R = call("api/setuplab/report?car=porsche992cup&track=spa")[1]["report"]
    assert R["files"] == 0 and any("No telemetry" in n for n in R["notes"])
    # bad input
    for bad in ("car=&track=x", "car=a%3Cb&track=x", "track=x", "car=" + "a" * 200 + "&track=x", q + "&series=../x"):
        code, r = call("api/setuplab/report?" + bad)
        assert code == 400 and r["ok"] is False, (bad, r)
    for body in ({"car": CAR, "track": TRACK, "setup": "maybe"}, {"car": 5, "track": TRACK, "setup": "open"},
                 {"car": CAR, "setup": "open"}):
        code, r = post("api/setuplab/series", body)
        assert code == 400, (body, r)
    assert call("api/setuplab/combos", header=False)[0] == 404
    assert call("api/setuplab/read", {}, header=False)[0] == 404


def test_setuplab_prepare_link():
    srv()
    read_telemetry()
    code, j = call("api/setuplab/combos?" + urllib.parse.urlencode({"car": "McLaren 570S GT4", "track": "Circuit Zolder (Grand Prix)"}))
    assert code == 200 and j["match"]["car_path"] == CAR and j["match"]["track_code"] == TRACK, j
    code, j = call("api/setuplab/combos?" + urllib.parse.urlencode({"car": "McLaren 570S GT4", "track": "Road America"}))
    assert code == 200 and j["match"] is None
    code, j = call("api/setuplab/combos?" + urllib.parse.urlencode({"car": "<b>", "track": "x"}))
    assert code == 400


def test_setuplab_missing_folder():
    srv()
    settings(iracing=False)
    try:
        code, j = call("api/setuplab/combos")
        assert code == 200 and j["folder_ok"] is False and j["unread"] is None and j["folder"].endswith("nowhere"), j
        code, r = post("api/setuplab/read")
        assert code == 400 and "can't find your telemetry folder" in r["error"] and "Settings" in r["error"], r
    finally:
        settings()


def _check_file(name, data, header=True):
    return call("api/setuplab/check-file?name=" + urllib.parse.quote(name), raw=data, header=header,
                ctype="application/octet-stream")


def test_setuplab_check_file():
    srv()
    tmp_root = Path(tempfile.gettempdir())
    left_before = set(tmp_root.glob("debrief-setupcheck-*"))
    sto = b"\x01\x02iRacing setup" + bytes(range(256)) * 8
    code, r = _check_file("baseline.sto", sto)
    assert code == 200 and r["ok"] and r["check"]["ok"] is True and r["check"]["kind"] == "sto", r
    code, r = _check_file("fast.sto", b"MZ" + b"\0" * 2000 + b"This program cannot be run in DOS mode")
    assert code == 200 and r["check"]["ok"] is False and any("program" in p for p in r["check"]["problems"]), r
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("GT4/zolder.sto", sto)
        z.writestr("readme.txt", "drive fast")
        z.writestr("install.exe", b"MZ" + b"\0" * 100)
        z.writestr("../evil.sto", sto)
    code, r = _check_file("pack.zip", buf.getvalue())
    probs = " ".join(r["check"]["problems"])
    assert code == 200 and r["check"]["ok"] is False and "install.exe" in probs and "outside the folder" in probs, r
    assert "GT4/zolder.sto" in r["check"]["members"]
    code, r = _check_file("setup.exe", b"MZ" + b"\0" * 100)
    assert code == 200 and r["check"]["ok"] is False
    # never left on disk (removed just after the reply is sent), never anywhere near iRacing
    end = time.time() + 5
    while set(tmp_root.glob("debrief-setupcheck-*")) != left_before and time.time() < end:
        time.sleep(0.05)
    assert set(tmp_root.glob("debrief-setupcheck-*")) == left_before
    assert not list(IRACING.rglob("*.sto")) and not list(IRACING.rglob("*.zip"))
    for name in ("", "../x.sto", "a/b.sto", "c:x.sto", ".."):
        code, r = _check_file(name, sto)
        assert code == 400, (name, r)
    assert _check_file("x.sto", sto, header=False)[0] == 404
    code, r = call("api/setuplab/check-file?name=empty.sto", raw=b"", ctype="application/octet-stream")
    assert code == 200 and r["check"]["ok"] is False and "empty" in " ".join(r["check"]["problems"]), r


# ================================================================ Coach
class Holding:
    """An .ibt playback that, once played, keeps returning its last sample (as if parked on track), so the coach
    stays 'coaching' until stopped."""

    def __init__(self, path):
        self.src = livecoach.IbtPlaybackSource(path, speed=0)
        self.interval, self.realtime, self.done, self.state, self.last = 0.0, False, False, "playing", None

    def info(self):
        return self.src.info()

    def read(self):
        s = self.src.read()
        if s is None:
            time.sleep(0.02)
            return dict(self.last) if self.last else None
        self.last = s
        return s

    def close(self):
        self.src.close()


class Waiting:
    """Live iRacing that never starts: read() is always None and the coach waits."""
    interval, realtime, done, state = 0.05, True, False, "not_running"

    def info(self):
        return None

    def read(self):
        return None

    def close(self):
        pass


def coach_threads():
    return [t for t in threading.enumerate() if t.name in ("debrief-livecoach", "debrief-hotkey") and t.is_alive()]


def use_fakes(s, source):
    voices = []

    def make_voice(settings):
        v = vc.FakeVoice()
        v.set(volume=settings.volume, rate=settings.rate)
        voices.append(v)
        return v
    s.coach_source_factory = source
    s.coach_voice_factory = make_voice
    return voices


def test_coach_start_status_mute_stop():
    s = srv()
    files = make_files()
    voices = use_fakes(s, lambda: livecoach.IbtPlaybackSource(files[-1], speed=0))
    code, st = call("api/coach/status")
    assert code == 200 and st["ok"] and st["state"] == "stopped" and st["running"] is False and st["text"] == "Coach off", st
    assert st["hotkey"]["key"] == "Ctrl+Shift+M" and st["settings"]["in_races"] is False and st["settings"]["top_n"] == 2
    code, r = post("api/coach/mute")
    assert code == 400 and "Start the coach first" in r["error"]
    code, r = post("api/coach/start")
    assert code == 200 and r["ok"] and "status" in r, r
    end = time.time() + 60
    while time.time() < end and call("api/coach/status")[1]["running"]:
        time.sleep(0.1)
    st = call("api/coach/status")[1]
    assert st["state"] == "finished" and st["text"] == "Playback finished", st
    assert st["reference"] and st["reference"]["lap_text"] and st["car"] == "McLaren 570S GT4", st
    assert voices and voices[0].prepared, "the phrases were handed to the voice"
    json.dumps(st, allow_nan=False)
    # stopped: what it knew stays on the page
    post("api/coach/stop")
    st = call("api/coach/status")[1]
    assert st["state"] == "stopped" and st["reference"] and not coach_threads()
    assert voices[0].closed, "the voice is closed when the coach stops"
    # mute while running (a playback that keeps going)
    use_fakes(s, lambda: Holding(files[-1]))
    assert post("api/coach/start")[1]["ok"]
    assert post("api/coach/start")[1].get("already") is True, "a second start doesn't make a second coach"
    code, r = post("api/coach/mute")
    assert code == 200 and r["muted"] is True and call("api/coach/status")[1]["muted"] is True
    assert post("api/coach/mute")[1]["muted"] is False
    end = time.time() + 30
    while time.time() < end and call("api/coach/status")[1]["state"] != "coaching":
        time.sleep(0.1)
    st = call("api/coach/status")[1]
    assert st["state"] == "coaching" and st["running"] and st["text"].startswith("Coaching:"), st
    assert len([t for t in threading.enumerate() if t.name == "debrief-livecoach"]) == 1
    code, r = post("api/coach/stop")
    assert code == 200 and r["status"]["running"] is False and not coach_threads()


def test_coach_settings_saved_and_applied():
    s = srv()
    files = make_files()
    use_fakes(s, lambda: Holding(files[-1]))
    code, r = post("api/coach/settings", {"settings": {"in_races": True, "top_n": 1, "volume": 60, "rate": 2, "brake_beep": True}})
    assert code == 200 and r["settings"]["in_races"] is True and r["settings"]["volume"] == 60, r
    assert settings_io.load_extra("coach")["settings"]["top_n"] == 1
    st = call("api/coach/status")[1]
    assert st["settings"]["in_races"] is True and st["settings"]["rate"] == 2 and st["settings"]["brake_beep"] is True
    assert post("api/coach/start")[1]["ok"]
    try:
        with s.coach_lock:
            r_ = s.coach
            v = s.coach_voice
        assert r_.settings.top_n == 1 and r_.settings.in_races and v.volume == 60 and v.rate == 2
        assert post("api/coach/settings", {"settings": {"volume": 30, "lap_delta": False}})[0] == 200
        assert r_.settings.volume == 30 and v.volume == 30 and r_.settings.lap_delta is False and r_.settings.top_n == 1
    finally:
        post("api/coach/stop")
    for bad in ({"settings": {"volume": 300}}, {"settings": {"hotkey": "x"}}, {"settings": "loud"}, {}, {"settings": {"top_n": "2"}}):
        code, r = post("api/coach/settings", bad)
        assert code == 400 and r["ok"] is False, (bad, r)
    post("api/coach/settings", {"settings": {"in_races": False, "top_n": 2, "volume": 100, "rate": 0, "brake_beep": False,
                                             "lap_delta": True}})


def wait_test_voice(gen, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        tv = call("api/coach/status")[1].get("test_voice") or {}
        if tv.get("id") == gen and tv.get("state") != "starting":
            return tv
        time.sleep(0.05)
    raise AssertionError("the test voice didn't finish")


def test_coach_test_voice():
    s = srv()
    voices = use_fakes(s, lambda: Waiting())
    code, r = post("api/coach/test-voice")
    assert code == 200 and r["ok"] and r["pending"] is True, r
    tv = wait_test_voice(r["id"])
    assert tv["state"] == "spoken" and tv["text"] == "Speaking test phrase", tv
    assert voices[-1].texts() == [S.COACH_TEST_TEXT]
    # a slow voice never holds the request: Windows speech can take seconds to start
    made = []

    class SlowVoice(vc.FakeVoice):
        def wait_ready(self, timeout=5.0):
            time.sleep(1.5)
            return True

    def slow(settings):
        v = SlowVoice()
        made.append(v)
        return v
    s.coach_voice_factory = slow
    t0 = time.time()
    code, r = post("api/coach/test-voice")
    assert code == 200 and r["pending"] and time.time() - t0 < 1.0, (r, time.time() - t0)
    assert call("api/coach/status")[1]["test_voice"]["state"] == "starting"
    assert wait_test_voice(r["id"])["state"] == "spoken" and made[-1].texts() == [S.COACH_TEST_TEXT]
    # the voice stays open long enough for slow speech
    assert S.voice_test_close_s(vc.FakeVoice(), -10) > S.voice_test_close_s(vc.FakeVoice(), 0) >= 4.0
    assert S.voice_test_close_s(vc.FakeVoice(), -10) >= 1.5 * vc.speak_seconds(S.COACH_TEST_TEXT, -10)
    voices = use_fakes(s, lambda: Waiting())
    # while coaching, the running voice says it
    assert post("api/coach/start")[1]["ok"]
    try:
        n = len(voices)
        assert post("api/coach/test-voice")[1]["spoken"] is True and len(voices) == n
        assert voices[-1].texts()[-1] == S.COACH_TEST_TEXT
    finally:
        post("api/coach/stop")
    # off Windows with no stand-in: the quiet voice says why
    s.coach_voice_factory = None
    code, r = post("api/coach/test-voice")
    tv = wait_test_voice(r["id"])
    if sys.platform != "win32":
        assert code == 200 and tv["state"] == "silent" and "Windows" in tv["reason"], tv
    assert s.test_voice is not None
    s.stop_coach()
    assert s.test_voice is None


def test_coach_never_crashes_the_server():
    s = srv()

    def boom():
        raise RuntimeError("no iRacing here")
    use_fakes(s, boom)
    code, r = post("api/coach/start")
    assert code == 400 and "couldn't start" in r["error"] and "debrief.log" in r["error"], r
    assert call("api/coach/status")[1]["running"] is False and not coach_threads()

    class Bad(Waiting):
        def read(self):
            raise OSError("telemetry went away")
    use_fakes(s, lambda: Bad())
    assert post("api/coach/start")[1]["ok"]
    time.sleep(0.4)
    st = call("api/coach/status")[1]
    assert st["running"] and "reading telemetry failed" in st["error"], st
    assert call("api/status")[0] == 200, "the server still answers"
    post("api/coach/stop")
    assert not coach_threads()


def test_coach_weak_corners_by_name():
    s = srv()
    rows = [{"car": "McLaren 570S GT4", "track": "Circuit Zolder (Grand Prix)", "session": "Race", "date": "2026-10-30",
             "L": 3980.0, "corners": [{"id": "C2", "apex": 1500.0, "avail": 0.3, "rank": 1}]},
            {"car": "McLaren 570S GT4", "track": "Circuit Zolder (Grand Prix)", "session": "Practice", "date": "2026-10-31",
             "L": 3980.0, "corners": []},
            {"car": "BMW M4 GT4", "track": "Circuit Zolder (Grand Prix)", "session": "Race", "date": "2026-10-29", "corners": []},
            {"car": "McLaren 570S GT4", "track": "Road America (Full Course)", "session": "Race", "date": "2026-10-28", "corners": []}]
    old = s.profile_cache.rows
    s.profile_cache.rows = lambda folder: rows
    try:
        out = s.coach_weak_for({"name": "Mclaren 570s GT4", "path": CAR}, {"name": "Zolder Circuit", "config": "Grand Prix"})
        assert s.coach_weak_info == {"races": 1} and isinstance(out, list), (s.coach_weak_info, out)
        assert s.coach_weak_for({"name": "Ferrari 296 GT3", "path": "ferrari296gt3"}, {"name": "Zolder Circuit"}) == []
        assert s.coach_weak_info == {"races": 0}
        # with a reference and no weak corners, the status says why
        s.coach_last = {"reference": {"lap_time": 99.0, "lap_text": "1:39.000", "text": "1:39.000", "source": {}}, "weak": []}
        st = s.coach_status()
        assert st["weak_note"] and "No saved race reviews" in st["weak_note"], st
    finally:
        s.profile_cache.rows = old
        s.coach_last = None
        s.coach_weak_info = None


def test_shutdown_stops_the_coach():
    """A second server: shutting it down (the window closing, an update) stops the coach and its threads."""
    s2 = S.DebriefServer(heartbeat_timeout=3600)
    t = threading.Thread(target=s2.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    t.start()
    voices = use_fakes(s2, lambda: Waiting())
    assert s2.start_coach()["ok"] and s2.coach.running()
    runner = s2.coach
    s2.shutdown()
    s2.server_close()
    t.join(5)
    assert not runner.running() and s2.coach is None and voices[0].closed and not coach_threads()
    assert s2.start_coach()["ok"] is False, "no coach once Debrief is closing"
    # the update path closes the same way: no new coach while closing
    s3 = S.DebriefServer(heartbeat_timeout=3600)
    use_fakes(s3, lambda: Waiting())
    s3.jobs.closing = True
    assert s3.start_coach()["ok"] is False
    s3.server_close()


def test_watchdog_closes_coach_with_the_window():
    s4 = S.DebriefServer(heartbeat_timeout=0.5, startup_grace=0.5)
    t = threading.Thread(target=s4.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    t.start()
    use_fakes(s4, lambda: Waiting())
    assert s4.start_coach()["ok"]
    runner = s4.coach
    s4.last_ping = time.monotonic() - 10
    threading.Thread(target=s4.watchdog, daemon=True).start()
    t.join(15)
    assert not t.is_alive() and not runner.running() and not coach_threads(), "the window closed: coach stopped"
    s4.server_close()


def test_coach_two_starts_make_one_coach():
    s = srv()
    gate = threading.Event()

    def slow_source():
        gate.wait(5)
        return Waiting()
    use_fakes(s, slow_source)
    res = []
    ts = [threading.Thread(target=lambda: res.append(s.start_coach())) for _ in range(2)]
    for t in ts:
        t.start()
    time.sleep(0.3)
    gate.set()
    for t in ts:
        t.join(10)
    try:
        assert all(r["ok"] for r in res) and sum(1 for r in res if r.get("already")) == 1, res
        time.sleep(0.2)
        assert len([t for t in threading.enumerate() if t.name == "debrief-livecoach" and t.is_alive()]) == 1
    finally:
        s.stop_coach()
    # Stop pressed while a start is still building its coach: the new coach is stopped at once
    gate.clear()
    out = []
    t = threading.Thread(target=lambda: out.append(s.start_coach()))
    t.start()
    time.sleep(0.2)
    s.stop_coach()
    gate.set()
    t.join(10)
    assert out and out[0]["ok"] is False and "stopped while it was starting" in out[0]["error"], out
    assert s.coach is None and not coach_threads()


def test_check_file_saved_under_a_fixed_name():
    srv()
    from debrief import setuplab
    seen = []
    real = setuplab.check_setup_file

    def spy(path, name=None):
        seen.append((Path(path).name, name))
        return real(path, name=name)
    setuplab.check_setup_file = spy
    try:
        code, r = _check_file("my fast setup.exe.sto", b"\x01\x02" * 300)
    finally:
        setuplab.check_setup_file = real
    assert seen == [("upload.sto", "my fast setup.exe.sto")], seen
    assert code == 200 and r["check"]["file"] == "my fast setup.exe.sto" and r["check"]["ok"] is False
    assert any("hides another file type" in p for p in r["check"]["problems"]), r


def test_update_stops_reading_telemetry():
    s5 = S.DebriefServer(heartbeat_timeout=3600)
    threading.Thread(target=s5.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
    s5.exit = lambda code=0: None
    with s5.jobs.lock:
        s5.jobs.st = {"state": "running", "kind": "setuplab", "id": 1}
    s5.jobs.stop_scan.clear()
    s5._close_for_update()
    assert s5.jobs.stop_scan.is_set() and s5.jobs.closing, "an update stops a Setup Lab read (it's only a cache)"
    with s5.jobs.lock:
        s5.jobs.st = {"state": "idle"}


# ================================================================ the window
def browser():
    if "b" not in PW:
        try:
            from playwright.sync_api import sync_playwright
            PW["pw"] = sync_playwright().start()
            PW["b"] = PW["pw"].chromium.launch()
        except Exception:  # noqa: BLE001
            print("  SKIPPED: Playwright/Chromium not available")
            PW["b"] = None
    return PW["b"]


def page(dark=False, width=1320, height=900, scale=1):
    b = browser()
    if not b:
        return None
    ctx = b.new_context(viewport={"width": width, "height": height}, color_scheme="dark" if dark else "light",
                        device_scale_factor=scale)
    pg = ctx.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    pg.route("**/api/sync", lambda r: r.fulfill(body='{"ok":true,"skipped":true}', content_type="application/json"))
    pg.route("**/api/update*", lambda r: r.fulfill(body='{"ok":true,"skipped":true}', content_type="application/json"))
    return ctx, pg, errs


def snap(pg, path):
    """The whole page: the window scrolls inside <main>, so the viewport is made as tall as the page first."""
    pg.evaluate("() => { const b = document.getElementById('banner'); if (b) b.hidden = true }")
    h = pg.evaluate("() => Math.max(900, ...[...document.querySelectorAll('.view')].filter(v => !v.hidden).map(v => v.scrollHeight))")
    size = pg.viewport_size
    pg.set_viewport_size({"width": size["width"], "height": min(int(h), 6000)})
    time.sleep(0.3)
    pg.screenshot(path=str(path))
    pg.set_viewport_size(size)


def shots_dir():
    d = os.environ.get("DEBRIEF_SHOTS")
    if not d:
        return None
    Path(d).mkdir(parents=True, exist_ok=True)
    return Path(d)


def test_pages_in_the_window():
    s = srv()
    read_telemetry()
    files = make_files()
    use_fakes(s, lambda: Holding(files[-1]))
    s.coach_weak_for = lambda car, track: [{"id": "C2", "avg_avail": 0.31}, {"id": "C1", "avg_avail": 0.12}]
    shots = shots_dir()
    try:
        for dark in (False, True):
            r = page(dark=dark, scale=2 if shots else 1)
            if not r:
                return
            ctx, pg, errs = r
            try:
                pg.goto(s.url)
                pg.wait_for_function("() => document.querySelector('.navbtn[data-nav=setuplab]')")
                names = pg.evaluate("() => [...document.querySelectorAll('.nav .navbtn')].map(b => b.textContent.trim())")
                assert names[names.index("Prepare"):][:5] == ["Prepare", "Team", "Setup Lab", "Coach", "Settings"], names
                # Setup Lab by key (Ctrl+8), newest car and track picked, the report drawn
                pg.keyboard.press("Control+8")
                pg.wait_for_selector("#vSetupLab", state="visible")
                pg.wait_for_selector("#slSetups")
                assert "Setup A" in pg.text_content("#slSetups") and "Setup B" in pg.text_content("#slSetups")
                assert "Front camber" in pg.text_content("#slDiffs") and "(both sides)" in pg.text_content("#slDiffs")
                assert pg.text_content("#slCompare") and pg.is_visible("#slNext")
                assert pg.evaluate("() => document.querySelector('#slCombos .pkbtn.on').textContent").startswith("Circuit Zolder")
                assert pg.text_content("#slPill").endswith("files")
                assert pg.is_visible("#slRead") and "first time reads every file" in pg.text_content("#slTel")
                # text can be selected
                assert pg.evaluate("() => getComputedStyle(document.querySelector('#slReport')).userSelect") != "none"
                # the series pill and his own choice
                pg.select_option("#slSeries", "fixed")
                pg.wait_for_function("() => document.querySelector('#slReport').textContent.includes('Fixed setup series')")
                pg.select_option("#slSeries", "")
                pg.wait_for_selector("#slSetups")
                if shots:
                    pg.evaluate("() => document.querySelectorAll('#vSetupLab details').forEach(d => d.open = false)")
                    snap(pg, shots / f"setuplab-{'dark' if dark else 'light'}.png")
                # Coach by key (Ctrl+9): start, cues, mute, stop
                pg.keyboard.press("Control+9")
                pg.wait_for_selector("#vCoach", state="visible")
                pg.wait_for_function("() => document.getElementById('coText').textContent === 'Coach off'")
                pg.click("#coStart")
                pg.wait_for_function("() => document.getElementById('coText').textContent.startsWith('Coaching')", timeout=30000)
                pg.wait_for_function("() => document.querySelectorAll('#coSpoken li:not(.note)').length > 0", timeout=30000)
                assert pg.text_content("#coRef") not in ("", "None yet")
                assert "Turn 2 (C2 in your race review)" in pg.text_content("#coWeak"), pg.text_content("#coWeak")
                assert pg.text_content("#coPill") == "Coaching" and pg.text_content("#vCoach .comain h3") == "Status"
                assert "Tell me what it said" not in pg.text_content("#vCoach")
                assert pg.evaluate("() => document.querySelector('.navbtn[data-nav=coach]').classList.contains('live')")
                pg.click("#coMute")
                pg.wait_for_function("() => document.getElementById('coMute').getAttribute('aria-pressed') === 'true'")
                pg.keyboard.press("Control+Shift+M")
                pg.wait_for_function("() => document.getElementById('coMute').getAttribute('aria-pressed') === 'false'")
                pg.click("#coSet summary")
                assert pg.text_content("#coRateV") in ("slower", "normal", "faster")
                if shots:
                    snap(pg, shots / f"coach-{'dark' if dark else 'light'}.png")
                pg.click("#coTest")
                pg.wait_for_function("() => document.getElementById('coSetMsg').textContent.includes('You should hear')")
                pg.click("#coStart")
                pg.wait_for_function("() => document.getElementById('coText').textContent === 'Coach off'")
                assert not pg.evaluate("() => document.querySelector('.navbtn[data-nav=coach]').classList.contains('live')")
                # with the coach off, Test voice answers at once and the result arrives with the status
                pg.evaluate("() => { document.getElementById('coSetMsg').textContent = '' }")
                pg.click("#coTest")
                pg.wait_for_function("() => document.getElementById('coSetMsg').textContent.includes('You should hear')")
                assert pg.is_enabled("#coTest")
                # no polling once the page is left and the coach is off
                seen = []
                pg.on("request", lambda rq: seen.append(rq.url) if "coach/status" in rq.url else None)
                pg.keyboard.press("Control+8")
                pg.wait_for_selector("#vSetupLab", state="visible")
                time.sleep(2.5)
                assert len(seen) <= 1, seen
                # Help lists the new keys
                pg.click("#btnHelp")
                pg.click("[data-help=keys]")
                keys = pg.text_content("#helpBody")
                assert "Ctrl+1 to Ctrl+9" in keys and "Setup Lab, Coach" in keys and "Ctrl+Shift+M" in keys
                pg.click("#helpClose")
                assert not errs, errs
            finally:
                ctx.close()
                s.stop_coach()
    finally:
        del s.coach_weak_for


def test_check_file_in_the_window():
    s = srv()
    r = page()
    if not r:
        return
    ctx, pg, errs = r
    try:
        bad = TMP / "fast setup.sto"
        bad.write_bytes(b"MZ" + b"\0" * 300)
        pg.goto(s.url)
        pg.wait_for_function("() => document.querySelector('.navbtn[data-nav=setuplab]')")
        pg.click(".navbtn[data-nav=setuplab]")
        pg.click("#slMore summary")
        pg.set_input_files("#slCheckFile", str(bad))
        pg.wait_for_function("() => document.getElementById('slCheckMsg').textContent.includes(\"don't use this file\")")
        assert "Windows program" in pg.text_content("#slCheckOut")
        assert not errs, errs
    finally:
        ctx.close()


if __name__ == "__main__":
    tests = [(n, f) for n, f in list(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok    {name}")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {name}")
            traceback.print_exc()
    if PW.get("b"):
        try:
            PW["b"].close()
            PW["pw"].stop()
        except Exception:  # noqa: BLE001
            pass
    if "srv" in ENV:
        ENV["srv"].shutdown()
        ENV["srv"].server_close()
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
