"""Debrief 2.38 "Coach, explained": Coach this (one corner he picks becomes the coach's one thing, from Practice,
a race review's Fix first card or Prepare), the one Voice switch (off: the coach starts muted, still counting), the
Coach page's Pick / Drive / Check steps and the Help chapter "Using the coach".

Through the real server (port 0, the X-Debrief header) with fake .ibt files (tests/corner_laps.py: a McLaren 570S GT4
at Zolder), an .ibt playback instead of live iRacing and FakeVoice instead of the Windows voice.

    python3 tests/test_238_coach.py

No network, temp folders from tempfile, utf-8 everywhere."""
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
TMP = Path(tempfile.mkdtemp(prefix="debrief-238-"))
os.environ["DEBRIEF_SETTINGS"] = str(TMP / "settings.toml")
os.environ["DEBRIEF_CACHE_DIR"] = str(TMP / "cache")
os.environ["DEBRIEF_SCHEDULE_DIR"] = str(TMP / "schedule")
os.environ["DEBRIEF_UPDATE_STATE"] = str(TMP / "update.json")

import corner_laps as cl  # noqa: E402
from debrief import app, livecoach, practiceplan as pp, voice as vc  # noqa: E402
from debrief.desktop import server as S  # noqa: E402

LIB = TMP / "lib"
IRACING = TMP / "iRacing"
TEL = IRACING / "telemetry"
ENV = {}
CAR, TRACK = "mclaren570sgt4", "zolder gp"
UI = ROOT / "debrief" / "desktop" / "ui"


def settings():
    LIB.mkdir(parents=True, exist_ok=True)
    TEL.mkdir(parents=True, exist_ok=True)
    Path(os.environ["DEBRIEF_SETTINGS"]).write_text(
        f"library_folder = '{LIB}'\napi_base = \"http://127.0.0.1:9/api/v1/\"\niracing_folder = '{IRACING}'\n",
        encoding="utf-8")


def srv():
    if "srv" not in ENV:
        settings()
        app.LOG_FILE = str(TMP / "debrief.log")
        s = S.DebriefServer(heartbeat_timeout=3600)
        threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
        ENV["srv"] = s
        ENV["opener"] = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return ENV["srv"]


def call(rel, body=None):
    s = srv()
    h = {"X-Debrief": "1"}
    data = None if body is None else json.dumps(body).encode("utf-8")
    if data is not None:
        h["Content-Type"] = "application/json"
    rq = urllib.request.Request(s.url + rel, headers=h, data=data, method="POST" if data is not None else "GET")
    try:
        with ENV["opener"].open(rq, timeout=120) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw


def wait_job(timeout=180):
    end = time.time() + timeout
    while time.time() < end:
        _c, st = call("api/status")
        if st.get("state") != "running":
            return st
        time.sleep(0.2)
    raise AssertionError("the job didn't finish")


def telemetry():
    """Two evenings of the GT4 at Zolder, read once."""
    if "read" in ENV:
        return
    TEL.mkdir(parents=True, exist_ok=True)
    ENV["old"] = cl.make(TEL / f"{CAR}_{TRACK} 2026-10-28 19-00-00.ibt", 12, seed=11, session="Practice")
    cl.make(TEL / f"{CAR}_{TRACK} 2026-10-31 18-30-00.ibt", 12, seed=12, session="Practice")
    code, r = call("api/setuplab/read", {})
    assert code == 200 and r["ok"], r
    st = wait_job()
    assert st["state"] == "done", st
    ENV["read"] = True


def report():
    code, j = call("api/corner-coach?" + urllib.parse.urlencode({"car": CAR, "track": TRACK}))
    assert code == 200 and j["ok"], j
    return j.get("report") or j


def no_long_dash(obj, where):
    t = json.dumps(obj, ensure_ascii=False)
    assert "—" not in t and "–" not in t, f"long dash in {where}"


# ================================================================ practiceplan: find_corner and plan_for_corner
CORNERS = [{"id": f"C{k}", "name": f"T{k}", "apex_m": 300.0 + 500 * k, "start_m": 150.0 + 500 * k,
            "end_m": 450.0 + 500 * k} for k in range(1, 6)]


def card(cid, rule, nums, lost=0.2, conf="likely"):
    return {"corner": "T" + cid[1:], "id": cid, "rule": rule, "secondary": None, "lost_s": lost,
            "confidence": conf, "how": "Try it.", "why": "Because.", "evidence": "4 best laps vs 8 typical laps.",
            "voice": f"T{cid[1:]}: x", "numbers": {k: {"best": b, "typical": t, "z": 3.0} for k, (b, t) in nums.items()}}


def rep(cards=(), unclear=(), laps=14, per_corner=None):
    return {"ok": True, "laps": laps, "cards": list(cards), "unclear": list(unclear), "fine": [], "corners": CORNERS,
            "per_corner": per_corner or {}, "info": {"car": "McLaren 570S GT4", "track": "Zolder", "length_m": 3980.0}}


def test_find_corner():
    R = rep([card("C2", "brake_later", {"brake_m": (-125.0, -140.0)})])
    assert pp.find_corner(R, apex_m=1290.0) == {"id": "C2", "corner": "T2", "apex_m": 1300.0, "by": "apex"}
    assert pp.find_corner(R, apex_m=1300.0 + pp.PICK_NEAR_M + 5) is None, "too far from any apex: no guess"
    assert pp.find_corner(R, corner="T4")["id"] == "C4" and pp.find_corner(R, corner="t4")["by"] == "name"
    assert pp.find_corner(R, corner="C3")["id"] == "C3"
    assert pp.find_corner(R, corner="T9") is None and pp.find_corner(R) is None
    assert pp.find_corner(None, corner="T1") is None and pp.find_corner({"corners": "bad"}, apex_m=3) is None
    # the apex wins over a name that disagrees (Garage 61's and iRacing's names can differ)
    assert pp.find_corner(R, corner="T1", apex_m=2300.0)["id"] == "C4"


def test_plan_for_corner():
    R = rep([card("C2", "brake_later", {"brake_m": (-125.0, -140.0)}, lost=0.3),
             card("C4", "brake_later", {"brake_m": (-110.0, -130.0)}, lost=0.1)])
    usual = pp.make_plan(R)
    assert usual["id"] == "C2", "the usual pick is the biggest loss"
    p = pp.plan_for_corner(R, "C4")
    assert p and p["id"] == "C4" and p["kind"] == "focus" and p["picked"] and p["reason"].startswith("You picked T4"), p
    assert p["confidence"] == "likely" and p["success_rule"], "never more sure than its card"
    no_long_dash(p, "plan_for_corner")
    # no card there: a consistency experiment when its best laps are clearly quicker
    pc = {"C3": {"numbers": {"t": {"best": 5.10, "typical": 5.40}}}}
    R2 = rep(unclear=[{"id": "C3", "corner": "T3", "lost_s": 0.3, "gap_s": 0.3, "best_laps": 4, "typical_laps": 8},
                      {"id": "C5", "corner": "T5", "lost_s": 0.5, "gap_s": 0.5, "best_laps": 4, "typical_laps": 8}],
             per_corner=dict(pc, C5={"numbers": {"t": {"best": 4.0, "typical": 4.5}}}))
    p = pp.plan_for_corner(R2, "C3")
    assert p and p["kind"] == "consistency" and p["id"] == "C3" and p["experiment"], p
    # nothing it could count, too few laps, a bad report: None (the server says why)
    assert pp.plan_for_corner(R2, "C1") is None
    assert pp.plan_for_corner(rep([card("C2", "brake_later", {"brake_m": (-125.0, -140.0)})], laps=3), "C2") is None
    assert pp.plan_for_corner({"ok": False}, "C2") is None and pp.plan_for_corner(None, "C2") is None


def test_combo_by_names():
    ss = [{"car": "McLaren 570S GT4", "track": "Circuit Zolder - Grand Prix", "car_path": CAR, "track_code": TRACK}]
    assert S._combo_by_names(ss, "mclaren 570s gt4", "Circuit Zolder Grand Prix") == (CAR, TRACK)
    assert S._combo_by_names(ss, "McLaren 570S GT4", "Circuit Zolder") == (CAR, TRACK), "layout on one side only"
    assert S._combo_by_names(ss, "Porsche 718 Cayman GT4", "Circuit Zolder") == (None, None)
    assert S._combo_by_names(ss, "", "x") == (None, None) and S._combo_by_names([], "a", "b") == (None, None)


# ================================================================ the server: api/plan/pick
def test_pick_before_reading():
    s = srv()
    old = s.setuplab.load
    s.setuplab.load = lambda path, folder=None: (None, frozenset())
    with s.practice.lock:
        s.practice.list_key = None
    try:
        code, r = call("api/plan/pick", {"car": CAR, "track": TRACK, "corner": "T2"})
        assert code == 200 and r["ok"] and r["picked"] is False and "Read your telemetry" in r["message"], r
    finally:
        s.setuplab.load = old
        with s.practice.lock:
            s.practice.list_key = None


def test_pick_by_ids_and_by_names():
    s = srv()
    telemetry()
    R = report()
    rows = [c for c in R.get("cards") or []] + [u for u in R.get("unclear") or []]
    assert rows, f"the fake laps should give the corner coach something: {str(R)[:300]}"
    usual = call("api/plan?" + urllib.parse.urlencode({"car": CAR, "track": TRACK}))[1]["plan"]
    # pick a corner other than the usual one when there is one
    pick = next((c for c in rows if c["id"] != usual.get("id")), rows[0])
    code, r = call("api/plan/pick", {"car": CAR, "track": TRACK, "corner": pick["id"]})
    assert code == 200 and r["ok"], r
    if r["picked"]:
        P = r["plan"]
        assert P["id"] == pick["id"] and P["picked"] and r["car_path"] == CAR and r["track_code"] == TRACK, r
        cur = call("api/plan?" + urllib.parse.urlencode({"car": CAR, "track": TRACK}))[1]["plan"]
        assert cur["plan_id"] == P["plan_id"], "the picked corner is now the one thing"
        assert s.coach_focus_for({"path": CAR}, {"code": TRACK}, 3980.0) is not None, "the coach would watch it"
        assert "Your one thing is now" in r["message"]
    else:
        assert "can't watch" in r["message"], r
    no_long_dash(r, "api/plan/pick")
    # by a race review's names and the corner's place on the lap
    ss = call("api/sessions-ibt")[1]["sessions"]
    names = {"car_name": ss[0]["car"], "track_name": ss[0]["track"]}
    apex = next(c["apex_m"] for c in R["corners"] if c["id"] == pick["id"])
    code, r2 = call("api/plan/pick", dict(names, corner="T99", apex_m=apex + 20))
    assert code == 200 and r2["ok"] and r2["car_path"] == CAR, r2
    assert r2["picked"] == r["picked"], (r, r2)
    if r2["picked"]:
        assert r2["plan"]["id"] == pick["id"] and r2["plan"]["matched_by"] == "apex", r2


def test_pick_misses_in_plain_words():
    srv()
    telemetry()
    before = call("api/plan?" + urllib.parse.urlencode({"car": CAR, "track": TRACK}))[1]["plan"]["plan_id"]
    code, r = call("api/plan/pick", {"car": CAR, "track": TRACK, "corner": "T99"})
    assert code == 200 and r["ok"] and not r["picked"] and "couldn't find" in r["message"], r
    code, r = call("api/plan/pick", {"car_name": "Porsche 718 Cayman GT4", "track_name": "Spa", "corner": "T1"})
    assert code == 200 and not r["picked"] and "No telemetry files of Porsche 718 Cayman GT4 at Spa" in r["message"], r
    code, r = call("api/plan/pick", {"car": CAR, "track": TRACK})
    assert not r["picked"] and r["message"] == "Pick a corner.", r
    code, r = call("api/plan/pick", {"car": CAR, "track": TRACK, "corner": 5, "apex_m": "x"})
    assert code == 200 and not r["picked"], r
    code, r = call("api/plan/pick", {"car": CAR, "track": TRACK, "apex_m": True})
    assert code == 200 and not r["picked"], "a bool is not a position"
    after = call("api/plan?" + urllib.parse.urlencode({"car": CAR, "track": TRACK}))[1]["plan"]["plan_id"]
    assert after == before, "a miss keeps the current one thing"


# ================================================================ the Voice switch
class Holding:
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


def coach_until(pred, timeout=90):
    end = time.time() + timeout
    st = {}
    while time.time() < end:
        st = call("api/coach/status")[1]
        if pred(st):
            return st
        time.sleep(0.1)
    raise AssertionError(f"coach status never got there: {str(st)[:400]}")


def test_voice_switch():
    s = srv()
    telemetry()
    code, r = call("api/coach/settings", {"settings": {"voice_on": False}})
    assert code == 200 and r["ok"] and r["settings"]["voice_on"] is False, r
    code, r = call("api/coach/settings", {"settings": {"voice_on": "no"}})
    assert code == 400 and not r["ok"], "on or off only"
    s.coach_source_factory = lambda: Holding(ENV["old"])
    s.coach_voice_factory = lambda settings: vc.FakeVoice()
    assert call("api/coach/start", {})[1]["ok"]
    try:
        st = coach_until(lambda st: st.get("running") and st.get("muted"))
        assert st["settings"]["voice_on"] is False and "Voice off" in json.dumps(st), str(st)[:400]
        # on again while it runs: it talks at once; off again: muted at once
        assert call("api/coach/settings", {"settings": {"voice_on": True}})[1]["ok"]
        coach_until(lambda st: st.get("running") and not st.get("muted"))
        assert call("api/coach/settings", {"settings": {"voice_on": False}})[1]["ok"]
        coach_until(lambda st: st.get("running") and st.get("muted"))
    finally:
        call("api/coach/stop", {})
        call("api/coach/settings", {"settings": {"voice_on": True}})
    # a new start with the voice on begins unmuted, as before
    assert call("api/coach/start", {})[1]["ok"]
    try:
        st = coach_until(lambda st: st.get("running"))
        assert not st.get("muted"), "voice on: unmuted"
    finally:
        call("api/coach/stop", {})


# ================================================================ the window: steps, Coach this, Help
def test_window_has_the_steps_and_buttons():
    html = (UI / "index.html").read_text(encoding="utf-8")
    js = (UI / "app.js").read_text(encoding="utf-8")
    help_js = (UI / "help.js").read_text(encoding="utf-8")
    review = (ROOT / "debrief" / "templates" / "review.html").read_text(encoding="utf-8")
    for i in ("coSteps", "csPick", "csDrive", "csCheck", "csStart", "coVoiceOn", "csGuide", "coMore"):
        assert f'id="{i}"' in html, i
    # every element the old page had is still there (folded, not removed)
    for i in ("crPanel", "cnCoach", "ovPanel", "coRehearse", "coStart", "coMute", "coVoice", "coSet"):
        assert f'id="{i}"' in html, i
    more = html[html.index('id="coMore"'):html.index('id="coVoice"')]
    assert all(f'id="{i}"' in more for i in ("crPanel", "cnCoach", "ovPanel", "coRehearse")), "optional tools folded"
    assert html.index('id="coSteps"') < html.index('id="coStart"'), "the steps come first"
    assert "api/plan/pick" not in js and "post('plan/pick'" in js
    assert "debrief-coach-this" in js and "debrief-coach-this" in review and "Coach this" in review
    assert "function coStepsRender" in js and "coStepsRender(st)" in js and "openHelp('coach')" in js
    assert "ccCard(c,R.car_path" in js and "coachThisBtn(Object.assign({car_name" in js
    assert "{id: 'coach', group: 'Learn', title: 'Using the coach'" in help_js
    sec = help_js[help_js.index("{id: 'coach'"):help_js.index("{id: 'numbers'")]
    for w in ("Pick", "Drive", "Check", "Coach this", "Voice", "7 of your last 10 laps", "More coach tools"):
        assert w in sec, w
    assert "—" not in sec and "–" not in sec, "no long dashes in the guide"
    assert re.search(r"voice_on:\s*bool\s*=\s*True", (ROOT / "debrief" / "livecoach.py").read_text(encoding="utf-8"))


def test_prepare_corners_carry_their_place():
    src = (ROOT / "debrief" / "prep.py").read_text(encoding="utf-8")
    assert '"apex_m": w.get("apex_m")' in src


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
    if "srv" in ENV:
        ENV["srv"].shutdown()
        ENV["srv"].server_close()
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
