"""Debrief 2.13 window: Practice (the session review and the corner coach) and the live coach using the corner
coach's tips, through the real server (port 0, the session secret and the X-Debrief header), with fake .ibt files
(tests/corner_laps.py: a McLaren 570S GT4 at Zolder with driving differences, tests/fake_ibt.py for another car),
an .ibt playback instead of live iRacing and FakeVoice instead of the Windows voice.

    python3 tests/test_213_page.py

No network, temp folders from tempfile, utf-8 everywhere. Browser checks use Playwright + Chromium and are skipped
when either is missing; DEBRIEF_SHOTS=<folder> also saves screenshots (practice-list-light.png,
session-light.png, session-dark.png).
"""
from __future__ import annotations

import re
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
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
TMP = Path(tempfile.mkdtemp(prefix="debrief-213p-"))
os.environ["DEBRIEF_SETTINGS"] = str(TMP / "settings.toml")
os.environ["DEBRIEF_CACHE_DIR"] = str(TMP / "cache")
os.environ["DEBRIEF_SCHEDULE_DIR"] = str(TMP / "schedule")
os.environ["DEBRIEF_UPDATE_STATE"] = str(TMP / "update.json")

import corner_laps as cl  # noqa: E402
import fake_ibt as fi  # noqa: E402
from debrief import app, ibt, livecoach, voice as vc  # noqa: E402
from debrief.desktop import server as S  # noqa: E402

LIB = TMP / "lib"
IRACING = TMP / "iRacing"
TEL = IRACING / "telemetry"
ENV, PW = {}, {}
CAR, TRACK = "mclaren570sgt4", "zolder gp"


def settings():
    LIB.mkdir(parents=True, exist_ok=True)
    TEL.mkdir(parents=True, exist_ok=True)
    Path(os.environ["DEBRIEF_SETTINGS"]).write_text(
        'token = "g61-test-token-ABCDEFGHIJKLMNOP"\n'
        f"library_folder = '{LIB}'\napi_base = \"http://127.0.0.1:9/api/v1/\"\niracing_folder = '{IRACING}'\n",
        encoding="utf-8")


def make_files():
    """Three evenings of the GT4 at Zolder (the newest is two files: practice, then a lone qualify), one older MX-5
    file at Laguna Seca and one damaged file."""
    if "files" in ENV:
        return ENV["files"]
    TEL.mkdir(parents=True, exist_ok=True)
    f = {}
    f["old"] = cl.make(TEL / f"{CAR}_{TRACK} 2026-10-28 19-00-00.ibt", 10, seed=11, session="Practice", surface_temp=19.0)
    f["mid"] = cl.make(TEL / f"{CAR}_{TRACK} 2026-10-31 18-30-00.ibt", 8, seed=12, offs=(4,), session="Practice")
    f["new1"] = cl.make(TEL / f"{CAR}_{TRACK} 2026-11-02 18-00-00.ibt", 9, seed=13, offs=(5,), session="Practice",
                        subsession=90000001)
    f["new2"] = cl.make(TEL / f"{CAR}_{TRACK} 2026-11-02 18-40-00.ibt", 7, seed=14, session="Lone Qualify",
                        subsession=90000001)
    f["mx5"] = fi.make(TEL / "mx5 mx52016_lagunaseca 2026-10-20 18-00-00.ibt", [92.0, 91.8, 91.9, 91.7, 91.9],
                       car="Global Mazda MX-5 Cup", car_path="mx5 mx52016", track="WeatherTech Raceway Laguna Seca",
                       track_code="lagunaseca")
    (TEL / "broken 2026-10-01 10-00-00.ibt").write_bytes(b"\0" * 400)
    ENV["files"] = f
    return f


def srv():
    if "srv" not in ENV:
        settings()
        app.LOG_FILE = str(TMP / "debrief.log")
        s = S.DebriefServer(heartbeat_timeout=3600)
        threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
        ENV["srv"] = s
        ENV["opener"] = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return ENV["srv"]


def call(rel, body=None, header=True):
    s = srv()
    h = {"X-Debrief": "1"} if header else {}
    data = None if body is None else json.dumps(body).encode("utf-8")
    if data is not None:
        h["Content-Type"] = "application/json"
    rq = urllib.request.Request(s.url + rel, headers=h, data=data, method="POST" if data is not None else "GET")
    try:
        with ENV["opener"].open(rq, timeout=60) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw


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
    code, r = call("api/setuplab/read", {})
    assert code == 200 and r["ok"], r
    st = wait_job()
    assert st["state"] == "done" and st["kind"] == "setuplab", st
    return st


def sessions():
    code, j = call("api/sessions-ibt")
    assert code == 200 and j["ok"], j
    return j


def newest():
    return sessions()["sessions"][0]


# ================================================================ logic around the server
def test_corner_paths_rules():
    day = 86400.0
    ss = [{"car_path": CAR, "track_code": TRACK, "sort": 100 * day, "paths": ["a1", "a2"]},
          {"car_path": CAR, "track_code": TRACK, "sort": 95 * day, "paths": ["b1"]},
          {"car_path": "other", "track_code": TRACK, "sort": 99 * day, "paths": ["x"]},
          {"car_path": CAR, "track_code": TRACK, "sort": 50 * day, "paths": ["c1"]}]
    assert S.corner_paths(ss, CAR, TRACK) == ["b1", "a1", "a2"], "newest files, oldest first, 30 days at most"
    assert S.corner_paths(ss, "MCLAREN570SGT4", "Zolder GP") == ["b1", "a1", "a2"], "names in any case"
    assert S.corner_paths(ss, CAR, TRACK, upto=96 * day) == ["b1"], "up to a session"
    many = [{"car_path": CAR, "track_code": TRACK, "sort": float(i), "paths": [f"f{i}a", f"f{i}b"]} for i in range(20)]
    got = S.corner_paths(many, CAR, TRACK)
    assert len(got) == S.CORNER_FILES and got[-1] == "f19b" and got[0] == "f15a", got
    assert S.corner_paths(ss, "nope", TRACK) == []
    cards = {"cards": [{"id": "C4", "apex_m": 2050.0, "lost_s": 0.21, "voice": "T4: lighter brake"},
                       {"id": "C2", "apex_m": 980.0, "lost_s": 0.14, "voice": None}]}
    assert S.weak_from_cards(cards) == [{"id": "C4", "apex_m": 2050.0, "lost_s": 0.21, "phrase": "T4: lighter brake",
                                         "rule": None, "source": "corner coach"}]


def test_practice_key_follows_versions():
    """2.13 review: a cached session review or corner coach is never reused by a newer Debrief or changed module."""
    import debrief
    from debrief import cornercoach, sessionibt
    f = TMP / "key.ibt"
    f.write_bytes(b"x")
    k0 = S.practice_key("corners", [f])
    assert S.practice_key("corners", [f]) == k0
    for mod, attr, val in ((debrief, "__version__", "9.9.9"), (cornercoach, "VERSION", 999), (sessionibt, "VERSION", 999)):
        old = getattr(mod, attr)
        setattr(mod, attr, val)
        try:
            assert S.practice_key("corners", [f]) != k0, (mod, attr)
        finally:
            setattr(mod, attr, old)
    assert S.practice_key("corners", [f]) == k0


def test_livecoach_phrases_and_abs():
    """The coach takes the corner coach's phrase per focus corner (said after the corner instead of the speed
    words), keeps the old words without one, and a reference built from an .ibt keeps its ABS channel."""
    files = make_files()
    ref = livecoach.build_reference(files["new1"])
    assert ref is not None and len(ref.corners) >= 5, ref
    with ibt.open_ibt(files["new1"]) as ib:
        raws = [livecoach._ibt_lap_raw(ib, x) for x in ibt.laps(ib) if x["clean"] and x["time"]]
    assert raws and all("ABSActive" not in r["_missing"] for r in raws), "BrakeABSactive read as the ABS"
    assert max(float(np.max(r["abs"])) for r in raws) > 0, "the typical laps lean on the ABS"
    c4 = ref.corner(4)
    weak = [{"id": "C4", "apex_m": c4.apex_m, "lost_s": 0.21, "phrase": "T4: lighter brake"},
            {"id": "C2", "apex_m": ref.corner(2).apex_m, "lost_s": 0.14, "phrase": "x" * 200}]
    ws = livecoach.match_weak(ref, weak, top_n=3)
    assert [w.name for w in ws] == ["T4", "T2"] and ws[0].phrase == "T4: lighter brake" and ws[1].phrase is None, ws
    coach = livecoach.Coach(ref, weak, livecoach.Settings(top_n=2))
    assert "T4: lighter brake" in coach.vocabulary()
    assert coach.status()["weak"][0]["phrase"] == "T4: lighter brake"
    # without phrases: the same corners, the old words
    plain = livecoach.Coach(ref, [{"id": "C4", "apex_m": c4.apex_m, "lost_s": 0.21}], livecoach.Settings())
    assert plain.weak[0].phrase is None and "T4: lighter brake" not in plain.vocabulary()
    assert "Brake marker, T4" in plain.vocabulary()
    # 2.13 review: a corner from the corner coach isn't named after the race review; one from a review is
    assert plain.weak[0].review_id == "C4" and plain.weak[0].source is None
    cw = livecoach.match_weak(ref, [dict(weak[0], source="corner coach")])
    assert cw[0].source == "corner coach" and cw[0].review_id is None, cw
    st = livecoach.Coach(ref, [dict(weak[0], source="corner coach")], livecoach.Settings()).status()["weak"][0]
    assert st["source"] == "corner coach" and st["review_id"] is None, st


def test_feedback_says_the_phrase():
    """Drive a lap slower through T4 than the reference: the word after the corner is the corner coach's tip."""
    files = make_files()
    ref = livecoach.build_reference(files["new1"])
    c4 = ref.corner(4)
    st = livecoach.Settings(top_n=1, markers=False, lap_delta=False, update_reference=False)
    coach = livecoach.Coach(ref, [{"id": "C4", "apex_m": c4.apex_m, "lost_s": 0.2, "phrase": "T4: lighter brake"}], st)
    L = ref.length_m
    said, t = [], 0.0
    for lap_n in (1, 2, 3):
        for i in range(0, 4000, 2):
            pct = i / 4000.0
            v = float(np.interp(pct, ref.pct, ref.speed, period=1.0))
            if abs(livecoach._rel_m(pct, c4.apex_pct, L)) < 40:
                v -= 4.0                                 # about 14 km/h down through T4
            t += (L / 4000.0 * 2) / max(v, 5.0)
            s = {"SessionTime": t, "Lap": lap_n, "LapDistPct": pct, "Speed": v, "Brake": 0.0, "Throttle": 1.0,
                 "Gear": 4, "OnPitRoad": False, "PlayerTrackSurface": ibt.ON_TRACK, "CarLeftRight": 1,
                 "IsOnTrack": True, "PlayerCarMyIncidentCount": 0, "SessionNum": 0, "SessionType": "Practice",
                 "LapLastLapTime": -1.0}
            said += [c.text for c in coach.feed(s)]
    assert "T4: lighter brake" in said, said
    assert not any("more speed" in x for x in said), said


def _drive_brake(coach, ref, c, shift_m, laps=(1, 2, 3)):
    """Laps at the reference's speed, 14 km/h down through corner c, braking shift_m metres from the reference's brake
    point (negative: earlier, positive: later, an overshoot). Returns what the coach said."""
    L = ref.length_m
    said, t = [], 0.0
    for lap_n in laps:
        for i in range(0, 4000, 2):
            pct = i / 4000.0
            v = float(np.interp(pct, ref.pct, ref.speed, period=1.0))
            rel = livecoach._rel_m(pct, c.apex_pct, L)
            if abs(rel) < 40:
                v -= 4.0
            on = c.brake_rel_m + shift_m <= rel <= -20.0
            t += (L / 4000.0 * 2) / max(v, 5.0)
            s = {"SessionTime": t, "Lap": lap_n, "LapDistPct": pct, "Speed": v, "Brake": 0.8 if on else 0.0,
                 "Throttle": 0.0 if on else 1.0, "Gear": 4, "OnPitRoad": False, "PlayerTrackSurface": ibt.ON_TRACK,
                 "CarLeftRight": 1, "IsOnTrack": True, "PlayerCarMyIncidentCount": 0, "SessionNum": 0,
                 "SessionType": "Practice", "LapLastLapTime": -1.0}
            said += [x.text for x in coach.feed(s)]
    return said


def test_brake_later_only_after_braking_early():
    """2.13 review: "T4: brake later" only when this lap braked early; after a late-braking overshoot the coach says
    the speed words instead (never "brake later" after braking too late). The rule comes with the phrase, or is
    read from its words."""
    files = make_files()
    ref = livecoach.build_reference(files["new1"])
    c4 = ref.corner(4)
    assert c4.brake_rel_m is not None
    st = livecoach.Settings(top_n=1, markers=False, lap_delta=False, update_reference=False)
    for weak in ({"id": "C4", "apex_m": c4.apex_m, "lost_s": 0.2, "phrase": "T4: brake later", "rule": "brake_later"},
                 {"id": "C4", "apex_m": c4.apex_m, "lost_s": 0.2, "phrase": "T4: brake later"}):
        early = _drive_brake(livecoach.Coach(ref, [weak], st), ref, c4, -30.0)
        assert "T4: brake later" in early, early
        late = _drive_brake(livecoach.Coach(ref, [weak], st), ref, c4, +30.0)
        assert "T4: brake later" not in late and any(x.startswith("T4: more speed") for x in late), late
    # a non brake-point phrase is said either way
    w = {"id": "C4", "apex_m": c4.apex_m, "lost_s": 0.2, "phrase": "T4: lighter brake", "rule": "lighter"}
    assert "T4: lighter brake" in _drive_brake(livecoach.Coach(ref, [w], st), ref, c4, +30.0)
    # "brake earlier" only after braking late
    w = {"id": "C4", "apex_m": c4.apex_m, "lost_s": 0.2, "phrase": "T4: brake earlier", "rule": "brake_earlier"}
    assert "T4: brake earlier" in _drive_brake(livecoach.Coach(ref, [w], st), ref, c4, +30.0)
    said = _drive_brake(livecoach.Coach(ref, [w], st), ref, c4, -30.0)
    assert "T4: brake earlier" not in said and said, said


# ================================================================ the endpoints
def test_sessions_before_and_after_reading():
    srv()
    make_files()
    shutil.rmtree(TMP / "cache" / S.SETUPLAB_CACHE, ignore_errors=True)
    j = sessions()
    assert j["folder_ok"] and j["indexed"] is False and j["sessions"] == [] and j["unread"] == 6, j
    code, r = call("api/session-review?id=x")
    assert code == 400 and "Read your telemetry first" in r["error"], r
    code, r = call("api/corner-coach?car=" + CAR + "&track=" + urllib.parse.quote(TRACK))
    assert code == 400 and "Read your telemetry first" in r["error"], r
    before = {p.name: p.stat().st_mtime_ns for p in TEL.iterdir()}
    read_telemetry()
    j = sessions()
    ss = j["sessions"]
    assert j["indexed"] and j["unread"] == 0 and len(ss) == 4, [(x["date"], x["car"]) for x in ss]
    assert [x["date"][:10] for x in ss] == ["2026-11-02", "2026-10-31", "2026-10-28", "2026-10-20"], "newest first"
    top = ss[0]
    assert top["car_path"] == CAR and len(top["files"]) == 2 and top["session_types"] == ["Practice", "Lone Qualify"], top
    assert top["laps"] > 10 and top["clean_laps"] >= 10 and 85 < top["best"] < 110, top
    assert "paths" not in top, "full paths stay on the server"
    assert ss[-1]["car_path"] == "mx5 mx52016"
    # read only: iRacing's folder unchanged
    assert {p.name: p.stat().st_mtime_ns for p in TEL.iterdir()} == before
    assert call("api/sessions-ibt", header=False)[0] == 404


def test_session_review():
    s = srv()
    read_telemetry()
    top = newest()
    made = s.practice.made
    t0 = time.time()
    code, j = call("api/session-review?" + urllib.parse.urlencode({"id": top["id"]}))
    first = time.time() - t0
    assert code == 200 and j["ok"], j
    R = j["review"]
    assert R["kind"] == "session_review" and R["car_path"] == CAR and len(R["files"]) == 2
    S_ = R["summary"]
    assert S_["best"] == top["best"] and S_["clean_laps"] >= 10 and S_["median"] >= S_["best"]
    assert R["consistency"]["enough"] and "within half a second" in R["consistency"]["text"]
    th = R["theoretical"]
    assert th["enough"] and th["laps_used"] == "clean" and th["possible"] <= S_["best"], th
    assert "Counted from your" in th["text"] and "guide" in th["text"]
    assert any(r["reasons"] == ["off", "incident"] for r in R["laps"]), "the off-track lap and why it isn't counted"
    assert any("warm-up" in (r["reason"] or "") for r in R["laps"])
    assert R["fuel"]["per_lap_l"] and abs(R["fuel"]["per_lap_l"] - cl.FUEL["per_lap"]) < 0.05, R["fuel"]
    assert R["history"] and "last session here" in R["history"]["text"], R["history"]
    assert R["setups"] and R["setups"][0]["label"].startswith("Setup A")
    C = j["corners"]
    assert C["ok"] and C["files"] == 4 and C["sessions"] == 3 and C["laps"] >= 20, {k: C.get(k) for k in ("ok", "files", "laps", "message")}
    ids = [c["id"] for c in C["cards"]]
    assert {"C2", "C4", "C6"} <= set(ids), ids
    lost = [c["lost_s"] for c in C["cards"]]
    assert lost == sorted(lost, reverse=True), "biggest loss first"
    c4 = next(c for c in C["cards"] if c["id"] == "C4")
    assert c4["rule"] == "lighter" and c4["voice"] == "T4: lighter brake" and c4["confidence"] in ("clear", "likely")
    assert any(u["id"] == "C1" for u in C["unclear"]), "T1 is quicker on the same laps with nothing different"
    assert C["summary"]["lines"] and C["phrases"].get("C4") == "T4: lighter brake"
    json.dumps(j, allow_nan=False)
    # the same files again: from memory, nothing worked out again
    t0 = time.time()
    code, j2 = call("api/session-review?" + urllib.parse.urlencode({"id": top["id"]}))
    assert code == 200 and j2["review"] == R and s.practice.made == made + 2, (s.practice.made, made)
    assert time.time() - t0 < max(0.5, first), "the second open is quick"
    # a restart: read back from the cache folder
    s.practice = S.PracticeStore()
    code, j3 = call("api/session-review?" + urllib.parse.urlencode({"id": top["id"]}))
    assert code == 200 and j3["review"] == R and s.practice.made == 0, s.practice.made
    assert list((TMP / "cache" / S.PRACTICE_CACHE).glob("*.json"))
    # a changed file: worked out again
    p = make_files()["new2"]
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    try:
        call("api/session-review?" + urllib.parse.urlencode({"id": top["id"]}))
        assert s.practice.made == 2, s.practice.made
    finally:
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
    # an older session: cards from the files up to then, history from the one before
    old = sessions()["sessions"][1]
    code, j = call("api/session-review?" + urllib.parse.urlencode({"id": old["id"]}))
    assert code == 200 and j["corners"]["files"] == 2 and j["latest"] is False, j["corners"].get("files")
    # one with fewer than 5 clean laps here is still reviewed
    mx = sessions()["sessions"][-1]
    code, j = call("api/session-review?" + urllib.parse.urlencode({"id": mx["id"]}))
    assert code == 200 and j["ok"] and j["corners"]["ok"] is False and j["corners"]["message"], j["corners"]


def test_bad_ids_rejected():
    srv()
    read_telemetry()
    for bad in ("", "nope", "<script>", "x" * 500, "../../etc|passwd|1", "mclaren570sgt4|zolder gp|"):
        code, r = call("api/session-review?" + urllib.parse.urlencode({"id": bad}))
        assert code == 400 and r["ok"] is False and "wasn't found" in r["error"], (bad, r)
    code, r = call("api/session-review")
    assert code == 400
    for q in ("car=&track=x", "car=a%3Cb&track=x", "track=x", "car=" + "a" * 200 + "&track=x"):
        code, r = call("api/corner-coach?" + q)
        assert code == 400 and r["ok"] is False, (q, r)
    assert call("api/session-review?id=x", header=False)[0] == 404
    assert call("api/corner-coach?car=a&track=b", header=False)[0] == 404


def test_corner_coach_endpoint():
    srv()
    read_telemetry()
    code, j = call("api/corner-coach?" + urllib.parse.urlencode({"car": CAR, "track": TRACK}))
    assert code == 200 and j["ok"] and j["car"].lower().startswith("mclaren") and j["report"]["ok"], j
    F = j["focus"]
    assert F and all(f["phrase"] and f["source"] == "corner coach" for f in F), F
    assert F[0]["lost_s"] == max(f["lost_s"] for f in F)
    assert j["report"]["files"] == 4
    code, j = call("api/corner-coach?" + urllib.parse.urlencode({"car": "porsche992cup", "track": "spa"}))
    assert code == 200 and j["ok"] and j["focus"] == [] and j["report"]["ok"] is False, j


# ================================================================ the live coach picks up the cards
class Holding:
    """An .ibt playback that, once played, keeps returning its last sample, so the coach stays on."""

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


def use_fakes(s, source):
    voices = []

    def make_voice(settings):
        v = vc.FakeVoice()
        voices.append(v)
        return v
    s.coach_source_factory = source
    s.coach_voice_factory = make_voice
    return voices


def test_coach_uses_the_cards():
    s = srv()
    read_telemetry()
    files = make_files()
    weak = s.coach_weak_for({"name": "Mclaren 570s GT4", "path": CAR}, {"name": "Zolder Circuit", "code": TRACK})
    assert weak and weak[0]["phrase"] and s.coach_weak_info["source"] == "corner coach", (weak, s.coach_weak_info)
    assert s.coach_weak_info["laps"] >= 20
    # no telemetry for that car: the race reviews, as before
    assert s.coach_weak_for({"name": "Ferrari 296 GT3", "path": "ferrari296gt3"}, {"name": "Zolder", "code": TRACK}) == []
    assert s.coach_weak_info == {"races": 0}
    voices = use_fakes(s, lambda: Holding(files["new2"]))
    assert call("api/coach/start", {})[1]["ok"]
    try:
        end = time.time() + 60
        st = {}
        while time.time() < end:
            st = call("api/coach/status")[1]
            if st.get("state") == "coaching" and st.get("weak"):
                break
            time.sleep(0.1)
        assert st["weak"] and all(w["phrase"] for w in st["weak"]), st
        assert len(st["weak"]) == 2, "top 2 by default"
        assert st["weak"][0]["phrase"] == weak[0]["phrase"]
        assert st["weak_note"] and "Corner coach" in st["weak_note"], st["weak_note"]
        assert voices and any(weak[0]["phrase"] in ph for ph in voices[0].prepared), "the phrase is made ready"
    finally:
        call("api/coach/stop", {})


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
    pg.set_viewport_size({"width": size["width"], "height": min(int(h), 8000)})
    time.sleep(0.3)
    pg.screenshot(path=str(path))
    pg.set_viewport_size(size)


def shots_dir():
    d = os.environ.get("DEBRIEF_SHOTS")
    if not d:
        return None
    Path(d).mkdir(parents=True, exist_ok=True)
    return Path(d)


def test_practice_in_the_window():
    s = srv()
    read_telemetry()
    shots = shots_dir()
    for dark in (False, True):
        r = page(dark=dark, scale=2 if shots else 1)
        if not r:
            return
        ctx, pg, errs = r
        try:
            pg.goto(s.url)
            pg.wait_for_function("() => document.querySelector('.navbtn[data-nav=practice]')")
            names = pg.evaluate("() => [...document.querySelectorAll('.nav .navbtn')].map(b => b.textContent.trim())")
            assert names[names.index("All sessions") + 1] == "Practice", names
            # Practice by key (Ctrl+0): the sessions, newest first
            pg.keyboard.press("Control+5")
            pg.wait_for_selector("#vPractice", state="visible")
            pg.wait_for_selector("#prSessions .prrow")
            rows = pg.evaluate("() => [...document.querySelectorAll('#prSessions .prrow')].map(b => b.textContent)")
            assert len(rows) == 3 and "Practice, Lone Qualify" in rows[0] and "Zolder" in rows[0], rows   # 2.19.2: newest 3
            assert pg.text_content("#prSessions .btn") == "Show all 4"
            assert pg.evaluate("() => document.querySelector('#vPractice').innerHTML.indexOf('id=\"prPlan\"') < document.querySelector('#vPractice').innerHTML.indexOf('id=\"prSessions\"')")
            pg.click("#prSessions .btn")
            assert pg.evaluate("() => document.querySelectorAll('#prSessions .prrow').length") == 4
            assert pg.text_content("#prPill") == "4 sessions"
            assert pg.evaluate("() => document.querySelector('.navbtn[data-nav=practice]').getAttribute('aria-current')") == "page"
            if shots and not dark:
                snap(pg, shots / "practice-list-light.png")
            # open the newest
            pg.click("#prSessions .prrow")
            pg.wait_for_selector("#prCorners .cccard")
            for sec in ("#prHead", "#prTheo", "#prCorners", "#prLaps", "#prFuel", "#prCond", "#prSetups"):
                assert pg.is_visible(sec), sec
            assert "Best lap" in pg.text_content("#prHead") and "last session here" in pg.text_content("#prHead")
            assert pg.locator("#prTheo .prbars li").count() >= 1 and "guide" in pg.text_content("#prTheo")
            cards = pg.evaluate("() => [...document.querySelectorAll('#prCorners .cccard h4')].map(e => e.textContent)")
            assert {"Turn 2", "Turn 4", "Turn 6"} <= set(cards), cards   # 2.18: "Turn N" everywhere, no "(CN)"
            # the best-possible-lap bars name corners the same way as the cards
            segs = pg.evaluate("() => [...document.querySelectorAll('#prTheo .prseg')].map(e => e.textContent)")
            assert segs and all(re.fullmatch(r"Turn \d+", x) for x in segs), segs   # 2.18: no "(CN)" beside it
            assert pg.locator("#prTheo .prseg small.prcid").count() == 0     # 2.18: the numbering note is the hover
            assert pg.locator("#prTheo .prseg[title]").count() == len(segs)
            ct0 = pg.text_content("#prCorners")
            assert not re.search(r"\b[CT]\d+:", ct0), "unclear corners say Turn N too"
            first = pg.text_content("#prCorners .cccard")
            for k in ("What's different", "How to try it", "What it gets you", "Why", "Evidence"):
                assert k in first, (k, first)
            ct = pg.text_content("#prCorners")
            assert "From your last" in ct and "clean laps here" in ct and "No clear pattern yet" in ct and "Braking habits" in ct, ct
            assert "Off track; incident (+2x)" in pg.text_content("#prLaps") and "Yes" in pg.text_content("#prLaps")
            assert "L a lap" in pg.text_content("#prFuel") and "Setup A" in pg.text_content("#prSetups")
            assert pg.evaluate("() => getComputedStyle(document.querySelector('#prReport')).userSelect") != "none"
            assert "—" not in pg.text_content("#vPractice"), "no long dashes in window text"
            if shots:
                snap(pg, shots / f"session-{'dark' if dark else 'light'}.png")
            # Setup Lab for this car
            pg.click("#prSetupLab")
            pg.wait_for_selector("#vSetupLab", state="visible")
            pg.wait_for_selector("#slSetups")
            assert pg.evaluate("() => document.querySelector('#slCombos .pkbtn.on').textContent").startswith("Circuit Zolder")
            # back to the session and on to the coach
            pg.keyboard.press("Control+5")
            pg.wait_for_selector("#prSessions .prrow")
            pg.click("#prSessions .prrow")
            pg.wait_for_selector("#prCoach")
            pg.click("#prCoach")
            pg.wait_for_selector("#vCoach", state="visible")
            pg.wait_for_selector("#coPlan li")
            plan = pg.text_content("#coPlan")
            assert "From the Corner coach" in plan and "T4: lighter brake" in plan and "Turn 4" in plan, plan
            # back from a session to the list
            pg.keyboard.press("Control+5")
            pg.wait_for_selector("#prSessions .prrow")
            pg.click("#prSessions .prrow >> nth=1")
            pg.wait_for_selector("#prReport .slhead")
            pg.click("#prBack")
            pg.wait_for_selector("#prList", state="visible")
            # Help lists the key and the words
            pg.click("#btnHelp")
            pg.click("[data-help=keys]")
            keys = pg.text_content("#helpBody")
            assert "Ctrl+5: Practice" in keys and "Ctrl+1 to Ctrl+9" in keys, keys
            pg.click("[data-help=words]")
            assert "Corner coach" in pg.text_content("#helpBody") and "Best possible lap" in pg.text_content("#helpBody")
            pg.click("#helpClose")
            assert not errs, errs
        finally:
            ctx.close()


def test_coach_page_with_corner_coach():
    """The Coach page names the corner coach's focus corners "Turn 4 (from the Corner coach)" and shows the tip."""
    s = srv()
    read_telemetry()
    files = make_files()
    shots = shots_dir()
    r = page(scale=2 if shots else 1)
    if not r:
        return
    ctx, pg, errs = r
    use_fakes(s, lambda: Holding(files["new2"]))
    try:
        pg.goto(s.url)
        pg.wait_for_function("() => document.querySelector('.navbtn[data-nav=coach]')")
        pg.keyboard.press("Control+3")
        pg.wait_for_selector("#vCoach", state="visible")
        pg.wait_for_function("() => document.getElementById('coText').textContent === 'Coach off'")
        pg.click("#coStart")
        pg.wait_for_function("() => document.getElementById('coText').textContent.startsWith('Coaching')", timeout=30000)
        pg.wait_for_function("() => document.querySelectorAll('#coWeak li').length > 0", timeout=30000)
        weak = pg.text_content("#coWeak")
        assert re.search(r"Turn \d+ \(from the Corner coach\)", weak) and "in your race review" not in weak, weak
        assert 'the coach says: "T' in weak, weak
        assert "Corner coach" in pg.text_content("#coWeakNote")
        assert pg.text_content("#vCoach .comain h3") == "Status"
        txt = pg.text_content("#vCoach")
        assert "\u2014" not in txt and "\u2013" not in txt, "no long dashes on the Coach page"
        if shots:
            snap(pg, shots / "coach213-light.png")
        pg.click("#coStart")
        pg.wait_for_function("() => document.getElementById('coText').textContent === 'Coach off'")
        assert not errs, errs
    finally:
        call("api/coach/stop", {})
        ctx.close()


def test_practice_before_reading():
    """No index yet: the page says so and offers the read button."""
    s = srv()
    make_files()
    old = s.setuplab.load
    s.setuplab.load = lambda path, folder=None: (None, frozenset())
    with s.practice.lock:
        s.practice.list_key = None
    r = page()
    if not r:
        s.setuplab.load = old
        return
    ctx, pg, errs = r
    try:
        pg.goto(s.url)
        pg.wait_for_function("() => document.querySelector('.navbtn[data-nav=practice]')")
        pg.click(".navbtn[data-nav=practice]")
        pg.wait_for_selector("#prSessions .emptystate")      # 2.28: one empty-state style
        assert "Not read yet" in pg.text_content("#prSessions") and pg.is_enabled("#prRead")
        assert pg.text_content("#prPill") == "Not read yet"
        assert not errs, errs
    finally:
        ctx.close()
        s.setuplab.load = old


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
