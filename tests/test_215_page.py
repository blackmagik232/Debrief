"""Debrief 2.15 window: "How the car handles" (balance map, setup ideas), "Tyre pressures over time" (bands, the
hot-pressure prediction), "Share what changed" (copy only), and a teammate's lap on the session page and the Coach
page, through the real server (port 0, the session secret and the X-Debrief header).

Fake files: a McLaren GT4 at Lime Rock with a known push in the middle of T3 and a rotation into T5
(tests/balance_laps.py), on three setups and two cold pressures at several track temperatures; and the GT4 at
Zolder (tests/corner_laps.py) for the Practice session page, with a teammate's file from another driver. The coach
runs on an .ibt playback with FakeVoice.

    python3 tests/test_215_page.py

No network, temp folders from tempfile, utf-8 everywhere. Browser checks use Playwright + Chromium and are skipped
when either is missing; DEBRIEF_SHOTS=<folder> also saves screenshots (setuplab215-light.png,
setuplab215-dark.png, teammate-light.png) at scale 2.
"""
from __future__ import annotations

import json
import os
import re
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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
TMP = Path(tempfile.mkdtemp(prefix="debrief-215p-"))
os.environ["DEBRIEF_SETTINGS"] = str(TMP / "settings.toml")
os.environ["DEBRIEF_CACHE_DIR"] = str(TMP / "cache")
os.environ["DEBRIEF_SCHEDULE_DIR"] = str(TMP / "schedule")
os.environ["DEBRIEF_UPDATE_STATE"] = str(TMP / "update.json")

import balance_laps as bal  # noqa: E402
import corner_laps as cl  # noqa: E402
import fake_ibt as fi  # noqa: E402
from debrief import app, livecoach, voice as vc  # noqa: E402
from debrief.desktop import carsetup as CS, server as S  # noqa: E402

LIB = TMP / "lib"
IRACING = TMP / "iRacing"
TEL = IRACING / "telemetry"
MATE = TMP / "from discord"
ENV, PW = {}, {}
CAR, LR, ZOL = "mclaren570sgt4", "limerock full", "zolder gp"
MATE_NAME = "Alex O'Neil"


def settings():
    LIB.mkdir(parents=True, exist_ok=True)
    TEL.mkdir(parents=True, exist_ok=True)
    Path(os.environ["DEBRIEF_SETTINGS"]).write_text(
        'token = "g61-test-token-ABCDEFGHIJKLMNOP"\n'
        f"library_folder = '{LIB}'\napi_base = \"http://127.0.0.1:9/api/v1/\"\niracing_folder = '{IRACING}'\n",
        encoding="utf-8")


def setup_with(arb, cold):
    s = json.loads(json.dumps(fi.SETUP_GT4))
    s["Chassis"]["FrontBrakes"]["ArbSetting"] = str(arb)
    for c in ("LeftFront", "RightFront", "LeftRear", "RightRear"):
        s["Tires"][c]["StartingPressure"] = f"{cold} kPa"
    return s


# (date, front bar, cold kPa, track temperature, pace scale (< 1 slower), the push/rotation added)
LIME = [("10-20", 3, 176, 19.0, 0.996, False), ("10-22", 3, 176, 26.5, 0.996, False),
        ("10-24", 3, 172, 21.0, 1.0, False), ("10-26", 3, 172, 28.0, 1.0, False),
        ("10-28", 3, 176, 23.0, 0.996, False), ("10-30", 3, 172, 24.0, 1.0, False),
        ("11-01", 2, 172, 22.0, 1.0, True), ("11-02", 2, 172, 25.0, 1.0, True)]


def make_files():
    if "files" in ENV:
        return ENV["files"]
    TEL.mkdir(parents=True, exist_ok=True)
    MATE.mkdir(parents=True, exist_ok=True)
    out = {"lime": []}
    for k, (day, arb, cold, temp, scale, push) in enumerate(LIME):
        rise_f, rise_r = 6.0 + 0.4 * temp, 5.0 + 0.35 * temp
        hot = {"LF": cold + rise_f + 1, "RF": cold + rise_f - 1, "LR": cold + rise_r + 1, "RR": cold + rise_r - 1}
        p = TEL / f"{CAR}_{LR} 2026-{day} 18-00-00.ibt"
        out["lime"].append(bal.make(p, n_laps=10, seed=40 + k, extra=bal.push_t3_rot_t5 if push else None,
                                    scale=scale, hot=hot, track_temp=temp, surface_temp=temp,
                                    start_date=1_790_000_000 + k * 2 * 86400, track="Lime Rock Park",
                                    track_code=LR, setup=setup_with(arb, cold), setup_name="lime.sto"))
    out["zol"] = [cl.make(TEL / f"{CAR}_{ZOL} 2026-10-31 18-30-00.ibt", 12, seed=12, session="Practice"),
                  cl.make(TEL / f"{CAR}_{ZOL} 2026-11-03 18-00-00.ibt", 12, seed=21, session="Practice")]
    out["mate"] = cl.make(MATE / f"{CAR}_{ZOL} 2026-11-02 20-00-00.ibt", 6, seed=5, name=MATE_NAME)
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


def call(rel, body=None, header=True):
    s = srv()
    h = {"X-Debrief": "1"} if header else {}
    data = None if body is None else json.dumps(body).encode("utf-8")
    if data is not None:
        h["Content-Type"] = "application/json"
    rq = urllib.request.Request(s.url + rel, headers=h, data=data, method="POST" if data is not None else "GET")
    try:
        with ENV["opener"].open(rq, timeout=180) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw


def upload(data: bytes, name, car=CAR, track=ZOL, keep=False):
    s = srv()
    qs = urllib.parse.urlencode({"name": name, "car": car, "track": track, "keep": "1" if keep else "0"})
    rq = urllib.request.Request(s.url + "api/teamref/upload?" + qs, data=data, method="POST",
                                headers={"X-Debrief": "1", "Content-Type": "application/octet-stream"})
    try:
        with ENV["opener"].open(rq, timeout=180) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def wait_job(timeout=240):
    end = time.time() + timeout
    while time.time() < end:
        _c, st = call("api/status")
        if st.get("state") != "running":
            return st
        time.sleep(0.2)
    raise AssertionError("the job didn't finish")


def ready():
    """Server, files and Setup Lab's index (read once)."""
    srv()
    make_files()
    if "read" not in ENV:
        code, r = call("api/setuplab/read", {})
        assert code == 200 and r["ok"], r
        st = wait_job()
        assert st["state"] == "done" and st["kind"] == "setuplab", st
        ENV["read"] = True


def q(**kw):
    return "?" + urllib.parse.urlencode(kw)


def no_long_dash(obj, where):
    t = json.dumps(obj, ensure_ascii=False)
    assert "—" not in t and "–" not in t, f"long dash in {where}"


def no_temp_left():
    left = [p for p in Path(tempfile.gettempdir()).glob("debrief-teamref-*")]
    assert not left, f"temp folders left behind: {left}"


# ================================================================ the balance map
def test_balance_endpoint_and_cache():
    s = ready() or srv()
    st = CS.state(s)
    with st.lock:
        st.mem.clear()
    shutil.rmtree(TMP / "cache" / CS.CACHE, ignore_errors=True)
    made = st.made
    code, j = call("api/balance" + q(car=CAR, track=LR))
    assert code == 200 and j["ok"], j
    R = j["report"]
    assert not j["cached"] and st.made == made + 1, "worked out the first time"
    assert [x["id"] for x in j["setups"]] == ["C", "B", "A"] and j["setup"] == "C", j["setups"]
    assert j["files"] == 2 and R["ok"] and R["laps"] >= 12, (j["files"], R["message"])
    t3 = next(c for c in R["cells"] if c["corner"] == "T3" and c["phase"] == "mid")
    t5 = next(c for c in R["cells"] if c["corner"] == "T5" and c["phase"] == "entry")
    assert t3["label"] == "push" and t5["label"] == "rotate", (t3, t5)
    assert "pushes in the middle of slow corners (T3)" in R["summary"], R["summary"]
    ideas = R["setup_ideas"]
    mid = next(i for i in ideas if i["phase"] == "mid")
    assert mid["change"]["text"].startswith("Front anti-roll bar 2 → 1"), mid
    assert ideas[0]["change"] and ideas[0]["change"]["to"], "the first idea is a change that can be made"
    ent = next(i for i in ideas if i["phase"] == "entry")
    # 2.18: front bar 2 -> 3 would only undo his change from B to C (3 -> 2), which the laps don't show slower
    assert ent["change"] is None and "already at its lowest" in ent["text"] and ent["undo"] \
        and "would undo the change you made from Setup B to Setup C" in ent["text"], ent
    assert j["last_change"] == {"from": "Setup B", "to": "Setup C", "verdict": j["last_change"]["verdict"], "slower": False}
    assert j["laps_note"] and "Which was faster counts" in j["laps_note"], j["laps_note"]
    assert ideas[0]["confidence"] in ("worth a try", "a guess") and "compare again" in ideas[0]["then"]
    assert R["cross_check"] and "params" not in R, "small: no parameter dump"
    no_long_dash(R, "balance report")
    # asked again: from memory
    code, j2 = call("api/balance" + q(car=CAR, track=LR))
    assert j2["cached"] and st.made == made + 1 and j2["report"]["summary"] == R["summary"]
    # Debrief restarted (memory empty): from the cache folder, not worked out again
    with st.lock:
        st.mem.clear()
    code, j3 = call("api/balance" + q(car=CAR, track=LR))
    assert j3["cached"] and st.made == made + 1, "read back from the cache folder"
    assert list((TMP / "cache" / CS.CACHE).glob("balance-*.json")), "saved under the cache folder"
    # a changed file: worked out again
    f = make_files()["lime"][-1]
    os.utime(f, ns=(f.stat().st_atime_ns, f.stat().st_mtime_ns + 1_000_000_000))
    code, j4 = call("api/balance" + q(car=CAR, track=LR))
    assert not j4["cached"] and st.made == made + 2
    # a new Debrief version: worked out again
    old = CS.VERSION
    CS.VERSION = old + 100
    try:
        code, j5 = call("api/balance" + q(car=CAR, track=LR))
        assert not j5["cached"] and st.made == made + 3
    finally:
        CS.VERSION = old
    # another setup, and all setups together
    code, ja = call("api/balance" + q(car=CAR, track=LR, setup="A"))
    assert ja["setup"] == "A" and ja["files"] == 3 and ja["report"]["ok"], ja.get("report", {}).get("message")
    assert not [c for c in ja["report"]["findings"] if c["corner"] == "T3"], "setup A has no push added at T3"
    code, jall = call("api/balance" + q(car=CAR, track=LR, setup="all"))
    assert jall["setup"] == "all" and jall["files"] == 8
    assert any("more than one setup" in n for n in jall["report"]["notes"]), jall["report"]["notes"]


def test_balance_fixed_series():
    ready()
    code, r = call("api/setuplab/series", {"car": CAR, "track": LR, "setup": "fixed"})
    assert code == 200 and r["ok"], r
    try:
        code, j = call("api/balance" + q(car=CAR, track=LR))
        I = j["report"]["setup_ideas"]
        # 2.18: no garage change; this car has no in-car dial channels, so each finding gets driving advice
        assert I and all(i["change"] is None and "The setup is locked" in i["text"] and "adapt the driving" in i["text"]
                         for i in I), I
        assert j["report"]["series_setup"] == "fixed" and j["last_change"] is None, j.get("last_change")
    finally:
        call("api/setuplab/series", {"car": CAR, "track": LR, "setup": ""})


def test_balance_bad_inputs():
    ready()
    for path in ("api/balance" + q(car=CAR, track=LR, setup="../A"), "api/balance" + q(car="<x>", track=LR),
                 "api/balance" + q(track=LR), "api/pressure" + q(car=CAR)):
        code, j = call(path)
        assert code == 400 and not j["ok"], (path, j)
    code, j = call("api/balance" + q(car=CAR, track="nowhere"))
    assert code == 200 and j["report"] is None and j["setups"] == [], j
    code, _ = call("api/balance" + q(car=CAR, track=LR), header=False)
    assert code == 404, "no X-Debrief header: not found"


# ================================================================ tyre pressures over time
def test_pressure_endpoint_and_predict():
    s = ready() or srv()
    code, j = call("api/pressure" + q(car=CAR, track=LR))
    assert code == 200 and j["ok"], j
    R = j["report"]
    assert R["runs"] == 8 and len(R["bands"]) == 3, (R["runs"], [b["band"] for b in R["bands"]])
    assert [b["band"] for b in R["bands"]] == ["15 to 20 °C", "20 to 25 °C", "25 to 30 °C"]
    assert j["can_predict"] and R["prediction"]["uses_temp"] and R["prediction"]["runs"] == 8, R["prediction"]
    assert "typical miss" in R["prediction_text"], R["prediction_text"]
    no_long_dash(R, "pressure report")
    code, j2 = call("api/pressure" + q(car=CAR, track=LR))
    assert j2["cached"], "asked again: cached"
    # the prediction: 172 cold at 25 °C comes up to about 172 + 6 + 10 front, 172 + 5 + 8.75 rear
    code, p = call("api/pressure/predict", {"car": CAR, "track": LR, "cold_front": 172, "cold_rear": "172",
                                            "track_temp": 25})
    assert code == 200 and p["ok"], p
    P = p["prediction"]
    assert abs(P["front"] - 188) <= 2 and abs(P["rear"] - 185.75) <= 2 and P["miss_kpa"] >= 1, P
    assert p["text"].startswith("At 25 °C track: front about") and "typical miss" in p["text"], p["text"]
    code, p2 = call("api/pressure/predict", {"car": CAR, "track": LR, "cold_front": 172, "cold_rear": 172,
                                             "track_temp": 45})
    assert "stretch" in p2["text"], p2["text"]
    code, p4 = call("api/pressure/predict", {"car": CAR, "track": LR, "cold_front": 230, "cold_rear": 172,
                                             "track_temp": 25})
    assert p4["prediction"]["cold_outside"] == ["front"] and "outside what they show" in p4["text"] \
        and "rear pressure" not in p4["text"], p4["text"]
    assert "outside what they show" not in p["text"], p["text"]
    code, p3 = call("api/pressure/predict", {"car": CAR, "track": LR, "cold_front": 172, "cold_rear": 172})
    assert code == 200 and p3["ok"], "track temperature is optional"
    for bad, word in (({"cold_front": "abc", "cold_rear": 172}, "number"), ({"cold_front": 9999, "cold_rear": 172}, "between"),
                      ({"cold_rear": 172}, "Fill in"), ({"cold_front": 172, "cold_rear": 172, "track_temp": "nan"}, "between"),
                      ({"cold_front": 172, "cold_rear": 172, "track_temp": 500}, "between")):
        code, r = call("api/pressure/predict", dict({"car": CAR, "track": LR}, **bad))
        assert code == 400 and word in r["error"], (bad, r)
    code, r = call("api/pressure/predict", {"car": CAR, "track": ZOL, "cold_front": 172, "cold_rear": 172})
    assert code in (200, 400), r      # Zolder: the car's other runs may or may not be enough; never a crash
    code, r = call("api/pressure/predict", {"car": "fordmustanggt4", "track": ZOL, "cold_front": 172, "cold_rear": 172})
    assert code == 400 and "Not enough runs" in r["error"], r


# ================================================================ share what changed
def test_share_text():
    ready()
    code, j = call("api/setuplab/share" + q(car=CAR, track=LR))
    assert code == 200 and j["ok"] and j["ready"], j
    assert j["pair"] == ["B", "C"], j["pair"]
    t = j["text"]
    assert "Lime Rock Park" in t and "Setup C vs B" in t and "anti-roll bar 3 → 2" in t, t
    assert "lime.sto" not in t and "Swazi" not in t and ".ibt" not in t, "no names or file names"
    assert j["markdown"].startswith("**") and "- " in j["markdown"] and len(t) <= 1500
    no_long_dash(j, "share")
    code, j2 = call("api/setuplab/share" + q(car=CAR, track=LR, a="A", b="B"))
    assert j2["pair"] == ["A", "B"] and "Setup B vs A" in j2["text"], j2["text"]
    code, j3 = call("api/setuplab/share" + q(car=CAR, track=LR, a="a; rm", b="B"))
    assert code == 400, j3
    # only a share text: nothing was sent anywhere (no Discord post, no staged message)
    s = srv()
    assert not getattr(s.discord_stage, "items", None), "nothing staged for Discord"


# ================================================================ a teammate's lap
def test_teammate_upload_and_refusals():
    s = ready() or srv()
    files = make_files()
    raw = files["mate"].read_bytes()
    before = sorted(os.listdir(MATE))
    code, r = upload(raw, files["mate"].name)
    assert code == 200 and r["ok"] and r["accepted"], r
    T = r["teammate"]
    assert T["driver"] == MATE_NAME and T["label"].startswith(MATE_NAME + "'s best lap (1:") and not T["kept"], T
    assert T["short"] == MATE_NAME + "'s lap" and not T["coach_on"], T
    assert sorted(os.listdir(MATE)) == before, "his own copy untouched"
    assert not (TMP / "cache" / CS.TEAM_DIR).exists(), "nothing kept unless he ticks Keep"
    no_temp_left()
    code, j = call("api/teamref" + q(car=CAR, track=ZOL))
    assert j["teammate"]["label"] == T["label"], j
    # refusals
    other = TMP / "fordmustanggt4_zolder gp 2026-11-02 20-00-00.ibt"
    fi.make(other, [99.0, 98.9, 99.1], car="Ford Mustang GT4", car_path="fordmustanggt4", name="Alex")
    code, r = upload(other.read_bytes(), other.name)
    assert code == 200 and not r["accepted"] and "different car (Ford Mustang GT4)" in r["reason"], r
    code, r = upload(b"MZ" + b"\x90" * 5000 + b"This program cannot be run in DOS mode" + b"\0" * 5000, "fast_lap.ibt")
    assert code == 200 and not r["accepted"] and "Windows program" in r["reason"], r
    code, r = upload(raw[:8192], "lap.ibt.exe")
    assert code == 200 and not r["accepted"] and "not a telemetry file" in r["reason"], r
    for bad in ("../../evil.ibt", "..\\..\\evil.ibt", "C:\\Windows\\evil.ibt", "a/b.ibt", "..", "x\x00.ibt", "", "a<b>.ibt"):
        code, r = upload(raw[:2048], bad)
        assert code == 400 and not r["ok"], (bad, r)
    code, r = upload(raw[:2048], "lap.ibt", track="<x>")
    assert code == 400, r
    old = CS.MAX_TEAM_BODY
    CS.MAX_TEAM_BODY = 1000
    try:
        code, r = upload(b"\0" * 2000, "big.ibt")
        assert code == 400 and "far bigger" in r["error"], r
    finally:
        CS.MAX_TEAM_BODY = old
    no_temp_left()
    # the refusals did not replace the good lap
    code, j = call("api/teamref" + q(car=CAR, track=ZOL))
    assert j["teammate"]["label"] == T["label"]
    # the server still answers after a refused upload
    assert call("api/status")[0] == 200
    assert not list(Path(tempfile.gettempdir()).glob("debrief-teamref-*"))


def test_teammate_cards_keep_and_forget():
    s = ready() or srv()
    files = make_files()
    code, r = upload(files["mate"].read_bytes(), files["mate"].name)
    assert r["accepted"], r
    code, j = call("api/teamref/cards" + q(car=CAR, track=ZOL))
    assert code == 200 and j["ok"], j
    assert j["message"].startswith(f"Your typical laps against {MATE_NAME}'s lap"), j["message"]
    for c in j["cards"]:
        assert f"{MATE_NAME}'s best lap" in c["text"] and c["confidence"] == "thin", c
    assert not list((TMP / "cache").rglob("*teamcards*")), "cards against a teammate stay in memory"
    # keep: only the worked-out lap is saved, not the file
    code, r = upload(files["mate"].read_bytes(), files["mate"].name, keep=True)
    assert r["accepted"] and r["teammate"]["kept"], r
    kept = list((TMP / "cache" / CS.TEAM_DIR).glob("*.json"))
    assert len(kept) == 1, kept
    data = json.loads(kept[0].read_text(encoding="utf-8"))
    assert set(data) == {"version", "car_path", "track_code", "summary", "livecoach", "cornercoach"}, set(data)
    assert files["mate"].read_bytes()[:64] not in kept[0].read_bytes(), "not the file itself"
    # Debrief restarted: the kept lap comes back
    st = CS.state(s)
    with st.lock:
        st.team.clear()
    code, j = call("api/teamref" + q(car=CAR, track=ZOL))
    assert j["teammate"] and j["teammate"]["kept"] and j["teammate"]["driver"] == MATE_NAME, j
    e = CS._team_get(s, app.load_settings(), CAR, ZOL)
    assert isinstance(e["livecoach"], livecoach.Reference) and e["livecoach"].corners, "a working Reference again"
    assert abs(e["livecoach"].lap_time - j["teammate"]["lap_time"]) < 1e-3
    code, j = call("api/teamref/cards" + q(car=CAR, track=ZOL))
    assert j["ok"] and j["message"].startswith("Your typical laps against"), j
    # a new file picked without Keep never deletes the kept lap: it stays on disk and the answer says so
    code, r = upload(files["mate"].read_bytes(), files["mate"].name)
    assert r["accepted"] and not r["teammate"]["kept"] and r["still_kept"] and MATE_NAME in r["still_kept"], r
    assert len(list((TMP / "cache" / CS.TEAM_DIR).glob("*.json"))) == 1, "the kept lap is still there"
    # the Keep tick on a lap already loaded: saves it, and unticking removes the saved lap
    code, r = call("api/teamref/keep", {"car": CAR, "track": ZOL, "keep": True})
    assert r["ok"] and r["teammate"]["kept"], r
    code, r = upload(files["mate"].read_bytes(), files["mate"].name, keep=True)
    assert r["accepted"] and r["teammate"]["kept"] and not r["still_kept"], r
    code, r = call("api/teamref/keep", {"car": CAR, "track": ZOL, "keep": False})
    assert r["ok"] and not r["teammate"]["kept"] and not list((TMP / "cache" / CS.TEAM_DIR).glob("*.json")), r
    code, r = call("api/teamref/keep", {"car": CAR, "track": ZOL, "keep": True})
    assert r["ok"] and len(list((TMP / "cache" / CS.TEAM_DIR).glob("*.json"))) == 1, r
    code, r = call("api/teamref/keep", {"car": CAR, "track": LR, "keep": True})
    assert code == 400 and "Pick a teammate" in r["error"], r
    # forget: memory and the kept lap
    code, r = call("api/teamref/forget", {"car": CAR, "track": ZOL})
    assert r["ok"] and not list((TMP / "cache" / CS.TEAM_DIR).glob("*.json"))
    code, j = call("api/teamref" + q(car=CAR, track=ZOL))
    assert j["teammate"] is None
    code, r = call("api/teamref/coach", {"car": CAR, "track": ZOL, "on": True})
    assert code == 400 and "Pick a teammate" in r["error"], r


class Holding:
    """An .ibt playback that, once played, keeps returning its last sample, so the coach stays on."""

    def __init__(self, path):
        self.src = livecoach.IbtPlaybackSource(path, speed=0)
        self.interval, self.realtime, self.done, self.state, self.last = 0.0, False, False, "playing", None

    def info(self):
        return self.src.info()

    def read(self):
        x = self.src.read()
        if x is None:
            time.sleep(0.02)
            return dict(self.last) if self.last else None
        self.last = x
        return x

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


def coach_until(pred, timeout=90):
    end = time.time() + timeout
    st = {}
    while time.time() < end:
        st = call("api/coach/status")[1]
        if pred(st):
            return st
        time.sleep(0.1)
    raise AssertionError(f"coach status never got there: {str(st)[:400]}")


def test_coach_against_teammate():
    s = ready() or srv()
    files = make_files()
    code, r = upload(files["mate"].read_bytes(), files["mate"].name)
    lap_time = r["teammate"]["lap_time"]
    use_fakes(s, lambda: Holding(files["zol"][-1]))
    # off by default: his own best lap
    st = call("api/coach/status")[1]
    assert st["teammate"]["on"] is False
    assert call("api/coach/start", {})[1]["ok"]
    try:
        st = coach_until(lambda st: st.get("state") == "coaching")
        assert st["reference"]["source"]["kind"] != "teammate" and not st["teammate"]["in_use"], st["reference"]["source"]
    finally:
        call("api/coach/stop", {})
    code, r = call("api/teamref/coach", {"car": CAR, "track": ZOL, "on": True})
    assert r["ok"] and r["on"] and not r["restart"], r
    voices = use_fakes(s, lambda: Holding(files["zol"][-1]))
    assert call("api/coach/start", {})[1]["ok"]
    try:
        st = coach_until(lambda st: st.get("state") == "coaching")
        R = st["reference"]
        assert R["source"]["kind"] == "teammate" and R["source"]["driver"] == MATE_NAME, R["source"]
        assert abs(R["lap_time"] - lap_time) < 1e-3, (R["lap_time"], lap_time)
        T = st["teammate"]
        assert T["on"] and T["in_use"] and T["short"] == f"{MATE_NAME}'s lap", T
        st = coach_until(lambda st: (st.get("last_lap") or {}).get("time"))
        assert st["reference"]["source"]["kind"] == "teammate", "his own laps don't replace their lap"
        assert voices and voices[0].prepared, "the voice got its phrases"
        assert s.coach.settings.update_reference is True, "his settings are left alone"
    finally:
        call("api/coach/stop", {})
    # another car + track: the teammate's lap is not used there
    with CS.state(s).lock:
        CS.state(s).coach_on = (CAR, LR)
    use_fakes(s, lambda: Holding(files["zol"][-1]))
    assert CS.coach_reference(s, livecoach.Settings()) is None, "no teammate lap for Lime Rock"
    with CS.state(s).lock:
        CS.state(s).coach_on = None
    code, r = call("api/teamref/coach", {"car": CAR, "track": ZOL, "on": False})
    assert r["ok"] and call("api/coach/status")[1]["teammate"]["on"] is False


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
    pg.evaluate("() => { const b = document.getElementById('banner'); if (b) b.hidden = true }")
    h = pg.evaluate("() => Math.max(900, ...[...document.querySelectorAll('.view')].filter(v => !v.hidden).map(v => v.scrollHeight))")
    size = pg.viewport_size
    pg.set_viewport_size({"width": size["width"], "height": min(int(h), 9000)})
    time.sleep(0.3)
    pg.screenshot(path=str(path))
    pg.set_viewport_size(size)


def shots_dir():
    d = os.environ.get("DEBRIEF_SHOTS")
    if not d:
        return None
    Path(d).mkdir(parents=True, exist_ok=True)
    return Path(d)


def no_dash_text(pg, sel):
    t = pg.text_content(sel)
    assert "—" not in t and "–" not in t, f"long dash in {sel}"


def open_lime(pg, s):
    pg.goto(s.url)
    pg.wait_for_function("() => document.querySelector('.navbtn[data-nav=setuplab]')")
    pg.keyboard.press("Control+8")
    pg.wait_for_selector("#vSetupLab", state="visible")
    pg.wait_for_selector("#slCombos button")
    pg.click("#slCombos button:has-text('Lime Rock Park')")
    pg.wait_for_selector("#slBalance .baltbl", timeout=60000)
    pg.wait_for_selector("#slPress .presstbl", timeout=60000)


def test_setuplab_in_the_window():
    s = ready() or srv()
    shots = shots_dir()
    for dark in (False, True):
        r = page(dark=dark, scale=2 if shots else 1)
        if not r:
            return
        ctx, pg, errs = r
        try:
            open_lime(pg, s)
            sec = pg.text_content("#slBalance")
            assert "How the car handles" in sec and "Turn 3" in sec and not re.search(r"\bT\d+\b", sec), sec[:400]
            cells = pg.locator("#slBalance .baltbl td .bal")
            assert cells.count() == 15, cells.count()
            t3 = pg.text_content("#slBalance .baltbl tbody tr:nth-child(3) td:nth-child(3)")
            assert t3.startswith("Pushes"), t3
            t5 = pg.text_content("#slBalance .baltbl tbody tr:nth-child(5) td:nth-child(2)")
            assert t5.startswith("Rotates"), t5
            words = {pg.text_content(f"#slBalance .baltbl td:nth-child({k}) .bal").split(" likely")[0] for k in (2, 3, 4)}
            assert words <= {"Pushes", "Rotates", "Neutral", "Not clear yet", "Not measured"}, words
            assert "Setup ideas" in sec and ("Worth a try" in sec or "A guess" in sec) and "compare again" in sec, sec
            assert "Front anti-roll bar 2 → 1" in sec, sec
            assert pg.is_visible("#slBalance .ballegend") and pg.is_visible("#slBalance .balsmall")
            ps = pg.text_content("#slPress")
            assert "Tyre pressures over time" in ps and "20 to 25 °C" in ps and "Hot pressure from cold" in ps, ps[:300]
            pg.fill("#prCF", "172")
            pg.fill("#prCR", "172")
            pg.fill("#prTT", "25")
            pg.click("#pressForm button[type=submit]")
            pg.wait_for_selector("#prPredOut:not([hidden])")
            assert pg.text_content("#prPredOut").startswith("At 25 °C track: front about")
            pg.click("#slShare")
            pg.wait_for_selector("#shareText")
            txt = pg.input_value("#shareText")
            assert "Setup C vs B" in txt and "anti-roll bar 3 → 2" in txt, txt
            assert pg.input_value("#shareMd").startswith("**")
            assert "never posts" in pg.text_content("#slDiffs")
            msg = pg.text_content("#slShareMsg")
            assert msg.startswith("Copied the Discord version") or msg.startswith("Couldn't copy by itself"), msg
            for sel in ("#slBalance", "#slPress", "#slDiffs"):
                no_dash_text(pg, sel)
            # the balance on another setup
            pg.select_option("#balSetup", "A")
            pg.wait_for_function("() => document.getElementById('balSetup') && document.getElementById('balSetup').value === 'A'"
                                 " && !document.getElementById('slBalance').classList.contains('busy')", timeout=60000)
            if shots:
                pg.select_option("#balSetup", "C")
                pg.wait_for_function("() => !!document.getElementById('balSetup') && document.getElementById('balSetup').value === 'C' && "
                                     "!document.getElementById('slBalance').classList.contains('busy')", timeout=60000)
                pg.wait_for_selector("#slBalance .baltbl")
                pg.evaluate("() => { const d = document.querySelector('#slBalance .balhow'); if (d) d.open = true }")
                snap(pg, shots / f"setuplab215-{'dark' if dark else 'light'}.png")
            if not dark:
                pg.click("#btnHelp")
                pg.click("[data-help=words]")
                words = pg.text_content("#helpBody")
                assert "Pushes, Rotates" in words and "Teammate's lap" in words and "Share what changed" in words
                pg.click("#helpClose")
            assert not errs, errs
        finally:
            ctx.close()


def test_teammate_in_the_window():
    s = ready() or srv()
    files = make_files()
    shots = shots_dir()
    call("api/teamref/forget", {"car": CAR, "track": ZOL})
    r = page(scale=2 if shots else 1)
    if not r:
        return
    ctx, pg, errs = r
    try:
        pg.goto(s.url)
        pg.wait_for_function("() => document.querySelector('.navbtn[data-nav=practice]')")
        pg.keyboard.press("Control+0")
        pg.wait_for_selector("#vPractice", state="visible")
        pg.wait_for_selector("#prSessions .prrow")
        pg.click("#prSessions .prrow:has-text('Zolder')")
        pg.wait_for_selector("#prTeam #prTeamPick")
        assert "Compare with a teammate's lap" in pg.text_content("#prTeam")
        # a wrong car is refused in plain words
        other = TMP / "fordmustanggt4_zolder gp 2026-11-02 20-00-00.ibt"
        if not other.exists():
            fi.make(other, [99.0, 98.9, 99.1], car="Ford Mustang GT4", car_path="fordmustanggt4", name="Alex")
        pg.set_input_files("#prTeamFile", str(other))
        pg.wait_for_function("() => /bad$/.test(document.getElementById('prTeamMsg').className)", timeout=60000)
        assert "different car" in pg.text_content("#prTeamMsg")
        pg.set_input_files("#prTeamFile", str(files["mate"]))
        pg.wait_for_function("() => document.querySelector('#prTeam .teamnow')", timeout=60000)
        assert MATE_NAME in pg.text_content("#prTeam .teamnow")
        pg.wait_for_function("() => !document.querySelector('#prTeam .teamcards').textContent.startsWith('Comparing')", timeout=60000)
        box = pg.text_content("#prTeam")
        assert "against " + MATE_NAME + "'s lap" in box, box[:500]
        if pg.locator("#prTeam .cccard").count():
            assert pg.text_content("#prTeam .cccard .teamey") == f"Against {MATE_NAME}'s lap"
        no_dash_text(pg, "#prTeam")
        if shots:
            pg.evaluate("() => document.getElementById('prTeam').scrollIntoView()")
            snap(pg, shots / "teammate-light.png")
        # the Keep tick now saves the lap in use, and unticking removes it
        assert not pg.is_checked("#prTeamKeep")
        pg.check("#prTeamKeep")
        pg.wait_for_function("() => /Kept for this car and track/.test(document.getElementById('prTeamMsg').textContent)")
        assert "Kept for this car and track" in pg.text_content("#prTeam .teamnow")
        assert len(list((TMP / "cache" / CS.TEAM_DIR).glob("*.json"))) == 1
        pg.uncheck("#prTeamKeep")
        pg.wait_for_function("() => /Not kept/.test(document.getElementById('prTeamMsg').textContent)")
        assert not list((TMP / "cache" / CS.TEAM_DIR).glob("*.json"))
        # the Coach page: the option, off by default
        use_fakes(s, lambda: Holding(files["zol"][-1]))
        pg.click("#prBack")
        pg.click("#prSessions .prrow:has-text('Zolder')")
        pg.wait_for_selector("#prCoach")
        pg.click("#prCoach")
        pg.wait_for_selector("#vCoach", state="visible")
        pg.wait_for_selector("#coTeam #coTeamOn")
        assert not pg.is_checked("#coTeamOn")
        assert pg.text_content("label[for=coTeamOn]") == f"Coach against {MATE_NAME}'s lap"
        pg.check("#coTeamOn")
        pg.wait_for_function("() => document.getElementById('coTeamNote').textContent.startsWith('On.')")
        pg.click("#coStart")
        pg.wait_for_function("() => document.getElementById('coText').textContent.startsWith('Coaching')", timeout=30000)
        pg.wait_for_function(f"() => document.getElementById('coRef').textContent.startsWith({json.dumps(MATE_NAME)})", timeout=30000)
        assert "Against " + MATE_NAME + "'s lap" in pg.text_content("#coRefNote")
        no_dash_text(pg, "#vCoach")
        pg.click("#coStart")
        pg.wait_for_function("() => document.getElementById('coText').textContent === 'Coach off'")
        pg.uncheck("#coTeamOn")
        pg.wait_for_function("() => document.getElementById('coTeamNote').textContent.startsWith('Off')")
        assert not errs, errs
    finally:
        call("api/coach/stop", {})
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
