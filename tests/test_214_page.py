"""Debrief 2.14 window: the Practice loop ("This session's one thing", its score, plan history), the coach watching
the plan's corner, wheel button mute and the first-run checklist, through the real server (port 0, the session
secret and the X-Debrief header), with fake .ibt files (tests/corner_laps.py: a McLaren 570S GT4 at Zolder), an .ibt
playback instead of live iRacing, FakeVoice instead of the Windows voice and a stand-in joystick backend.

    python3 tests/test_214_page.py

No network, temp folders from tempfile, utf-8 everywhere. Browser checks use Playwright + Chromium and are skipped
when either is missing; DEBRIEF_SHOTS=<folder> also saves screenshots (practice-plan-light.png,
practice-plan-dark.png, coach214-light.png, firstrun-light.png) at scale 2.
"""
from __future__ import annotations

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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
TMP = Path(tempfile.mkdtemp(prefix="debrief-214p-"))
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
ENV, PW = {}, {}
CAR, TRACK = "mclaren570sgt4", "zolder gp"


def settings():
    LIB.mkdir(parents=True, exist_ok=True)
    TEL.mkdir(parents=True, exist_ok=True)
    Path(os.environ["DEBRIEF_SETTINGS"]).write_text(
        'token = "g61-test-token-ABCDEFGHIJKLMNOP"\n'
        f"library_folder = '{LIB}'\napi_base = \"http://127.0.0.1:9/api/v1/\"\niracing_folder = '{IRACING}'\n",
        encoding="utf-8")


def make_old():
    """Two evenings of the GT4 at Zolder: what the first plan is made from."""
    if "old" in ENV:
        return ENV["old"]
    TEL.mkdir(parents=True, exist_ok=True)
    f = {"a": cl.make(TEL / f"{CAR}_{TRACK} 2026-10-28 19-00-00.ibt", 12, seed=11, session="Practice"),
         "b": cl.make(TEL / f"{CAR}_{TRACK} 2026-10-31 18-30-00.ibt", 12, seed=12, session="Practice")}
    ENV["old"] = f
    return f


def make_new():
    """The session driven after the plan was made."""
    if "new" not in ENV:
        ENV["new"] = cl.make(TEL / f"{CAR}_{TRACK} 2026-11-03 18-00-00.ibt", 12, seed=21, session="Practice")
    return ENV["new"]


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


def read_telemetry():
    code, r = call("api/setuplab/read", {})
    assert code == 200 and r["ok"], r
    st = wait_job()
    assert st["state"] == "done" and st["kind"] == "setuplab", st
    return st


def sessions():
    code, j = call("api/sessions-ibt")
    assert code == 200 and j["ok"], j
    return j["sessions"]


def q(**kw):
    return "?" + urllib.parse.urlencode(kw)


def no_long_dash(obj, where):
    t = json.dumps(obj, ensure_ascii=False)
    assert "—" not in t and "–" not in t, f"long dash in {where}"


def stage_scored():
    """The plan made from the two old evenings, then a newer session scored on it (once)."""
    if "scored" in ENV:
        return ENV["scored"]
    srv()
    make_old()
    read_telemetry()
    code, j = call("api/plan")
    assert code == 200 and j["ok"] and j["plan"], j
    first = j["plan"]
    make_new()
    read_telemetry()
    new = sessions()[0]
    assert new["date"].startswith("2026-11-03"), new
    code, rv = call("api/session-review" + q(id=new["id"]))
    assert code == 200 and rv["ok"], rv
    ENV["scored"] = (first, new, rv)
    return ENV["scored"]


# ================================================================ plans through the server
def test_plan_before_reading():
    s = srv()
    old = s.setuplab.load
    s.setuplab.load = lambda path: (None, frozenset())
    with s.practice.lock:
        s.practice.list_key = None
    try:
        code, j = call("api/plan")
        assert code == 200 and j["ok"] and j["plan"] is None and not j["indexed"] and "Read your telemetry" in j["message"], j
    finally:
        s.setuplab.load = old
        with s.practice.lock:
            s.practice.list_key = None


def test_plan_made_and_kept():
    srv()
    make_old()
    read_telemetry()
    code, j = call("api/plan")
    assert code == 200 and j["ok"] and j["car_path"] == CAR and j["track_code"] == TRACK, j
    P = j["plan"]
    assert P["kind"] in ("focus", "consistency", "laps_first", "none"), P
    assert P["kind"] in ("focus", "consistency"), f"24 clean laps of fake driving should give a plan: {P['text']}"
    assert P["zone"] and P["success_rule"] and P["check"] and P["from_laps"] >= 12, P
    assert P["after_date"].startswith("2026-10-31") and P["car_name"] and P["track_name"], P
    if P["kind"] == "consistency":
        assert P["experiment"] and P["text"].startswith("Try this and see")
    no_long_dash(j, "api/plan")
    # asked again: the same plan (kept until it is scored or he picks another)
    code, j2 = call("api/plan" + q(car=CAR, track=TRACK))
    assert j2["plan"]["plan_id"] == P["plan_id"], (j2["plan"]["plan_id"], P["plan_id"])
    # saved under <settings folder>/practice
    f = pp.plan_path(S.plans_folder(), CAR, TRACK)
    assert f.exists() and f.parent == TMP / "practice", f
    assert json.loads(f.read_text(encoding="utf-8"))["plans"][0]["plan_id"] == P["plan_id"]


def test_pick_another():
    srv()
    make_old()
    read_telemetry()
    code, j = call("api/plan" + q(car=CAR, track=TRACK))
    before = j["plan"]
    code, r = call("api/plan/new", {"car": CAR, "track": TRACK})
    assert code == 200 and r["ok"] and r["plan"]["plan_id"] != before["plan_id"], r
    nxt = r["plan"]
    if nxt["id"] == before["id"]:
        assert "same one again" in nxt["reason"], nxt["reason"]
    hist = call("api/plan/history" + q(car=CAR, track=TRACK))[1]["history"]
    assert hist[0]["plan_id"] == nxt["plan_id"] and hist[1]["plan_id"] == before["plan_id"], hist


def test_auto_score_on_the_session_page():
    first, new, rv = stage_scored()
    o = rv["one_thing"]
    assert o and o["just_scored"] and o["plan"]["score"], o
    sc = o["plan"]["score"]
    assert sc["session"] == new["id"] and sc["auto"] is True and sc["laps"] >= 8, sc
    assert sc["text"].startswith("You hit it on ") or sc["verdict"] == "never", sc["text"]
    assert sc["verdict"] in ("worth", "slower", "unclear", "too_few", "never", "n/a"), sc
    assert sc["shuffles"] in (None, pp.SHUFFLE_N) and sc.get("z") is None, sc
    assert sc["next"]["action"] in ("keep", "next") and o["next_plan"]["plan_id"] != o["plan"]["plan_id"]
    no_long_dash(rv["one_thing"], "one_thing")
    # opened again: the saved score, not scored twice
    code, rv2 = call("api/session-review" + q(id=new["id"]))
    o2 = rv2["one_thing"]
    assert o2 and not o2["just_scored"] and o2["plan"]["plan_id"] == o["plan"]["plan_id"], o2
    # an older session: no score for it
    old = sessions()[1]
    code, rv3 = call("api/session-review" + q(id=old["id"]))
    assert rv3["one_thing"] is None, rv3["one_thing"]
    # the Practice page now shows the next plan, with the last one's report card
    code, j = call("api/plan" + q(car=CAR, track=TRACK))
    assert j["plan"]["plan_id"] == o["next_plan"]["plan_id"] and j["plan"].get("score") is None, j["plan"]
    assert j["last"]["plan_id"] == o["plan"]["plan_id"] and j["last"]["score"]["text"] == sc["text"], j["last"]
    if sc["next"]["action"] == "keep" and o["plan"]["kind"] == "focus":
        assert j["plan"]["repeat"] and j["plan"]["id"] == o["plan"]["id"], "keep: the same corner again"
    H = j["history"]
    assert H[0]["score"] is None and any(h["score"] and h["score"]["session"] == new["id"] for h in H), H


def test_score_rules():
    first, new, rv = stage_scored()
    # the new plan was made after the newest session: that session can't score it
    code, r = call("api/plan/score" + q(session=new["id"]), {})
    assert code == 400 and "before the plan was made" in r["error"], r
    code, r = call("api/plan/score", {"session": "nope|x|1"})
    assert code == 400 and "wasn't found" in r["error"], r
    code, r = call("api/plan/score", {})
    assert code == 400 and r["ok"] is False, r
    # a newer session scores the waiting plan when asked (POST, not only on the session page)
    later = cl.make(TEL / f"{CAR}_{TRACK} 2026-11-04 18-00-00.ibt", 10, seed=31, session="Practice")
    read_telemetry()
    sid = sessions()[0]["id"]
    assert "2026-11-04" in sessions()[0]["date"], sessions()[0]
    code, r = call("api/plan/score" + q(session=sid), {})
    assert code == 200 and r["ok"] and r["score"]["session"] == sid and r["score"]["auto"] is False, r
    assert r["next_plan"]["plan_id"] != r["plan"]["plan_id"]
    code, r2 = call("api/plan/score" + q(session=sid), {})
    assert code == 400, "the same session can't score the next plan too"
    code, rv = call("api/session-review" + q(id=sid))
    assert rv["one_thing"] and not rv["one_thing"]["just_scored"], rv["one_thing"]
    ENV["later"] = later


def test_bad_inputs():
    srv()
    code, _ = call("api/plan", header=False)
    assert code == 404
    code, r = call("api/plan" + q(car="bad<car>", track=TRACK))
    assert code == 400 and r["error"] == "Pick a car and track.", r
    code, r = call("api/plan" + q(car=CAR))
    assert code == 400, r
    code, r = call("api/plan" + q(car="porsche992cup", track="spa"))
    assert code == 200 and r["plan"] is None and "No telemetry sessions of this car" in r["message"], r
    code, r = call("api/plan/new", {"car": "x\x01", "track": "y"})
    assert code == 400, r
    code, r = call("api/plan/history" + q(car="bad|car", track=TRACK))
    assert code == 400, r
    # a damaged plans file never stops the page
    f = pp.plan_path(S.plans_folder(), "ferrari296gt3", "spa")
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("{not json", encoding="utf-8")
    assert pp.load_plans(S.plans_folder(), "ferrari296gt3", "spa") == []


# ================================================================ the coach watches the plan's corner
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


def coach_until(pred, timeout=90):
    end = time.time() + timeout
    st = {}
    while time.time() < end:
        st = call("api/coach/status")[1]
        if pred(st):
            return st
        time.sleep(0.1)
    raise AssertionError(f"coach status never got there: {str(st)[:400]}")


def test_coach_watches_the_plan():
    s = srv()
    stage_scored()
    cur = call("api/plan" + q(car=CAR, track=TRACK))[1]["plan"]
    assert cur["kind"] in ("focus", "consistency"), cur
    voices = use_fakes(s, lambda: Holding(make_new()))
    assert call("api/coach/start", {})[1]["ok"]
    try:
        st = coach_until(lambda st: st.get("state") == "coaching" and st.get("focus"))
        F = st["focus"]
        assert F["id"] == cur["id"] and F["active"] and F["corner"] == cur["corner"], F
        assert s.coach_focus_info["plan_id"] == cur["plan_id"]
        st = coach_until(lambda st: (st.get("focus") or {}).get("measured", 0) >= 3)
        F = st["focus"]
        assert F["progress"] == f"Hit {F['hits']} of {F['measured']} so far", F
        voc = pp.cue_vocabulary(cur)
        assert voc and voices and any(v in ph for ph in voices[0].prepared for v in voc), "the plan's cues are made ready"
    finally:
        call("api/coach/stop", {})
    st = call("api/coach/status")[1]
    assert st["state"] == "stopped" and st["focus"] and st["focus"]["measured"] >= 3, "the last drive's count stays"
    # no plan for a car: no focus
    assert s.coach_focus_for({"path": "ferrari296gt3"}, {"code": "spa"}, 7000.0) is None


# ================================================================ wheel button mute
class FakeBackend:
    def __init__(self):
        self.devs = {1: {"name": "Simucube wheel", "mid": 0x16D0, "pid": 0x0D5A}}
        self.masks = {1: 0}
        self.lock = threading.Lock()

    def devices(self):
        with self.lock:
            return [dict(v, id=k, buttons=32) for k, v in self.devs.items()]

    def buttons(self, dev):
        with self.lock:
            return self.masks.get(dev) if dev in self.devs else None

    def press(self, button, down=True):
        with self.lock:
            bit = 1 << (button - 1)
            self.masks[1] = (self.masks[1] | bit) if down else (self.masks[1] & ~bit)


def wheel_until(pred, timeout=10):
    end = time.time() + timeout
    w = {}
    while time.time() < end:
        w = call("api/coach/status")[1]["wheel"]
        if pred(w):
            return w
        time.sleep(0.05)
    raise AssertionError(f"wheel status never got there: {w}")


def tap(be, button):
    be.press(button)
    time.sleep(0.25)
    be.press(button, False)
    time.sleep(0.2)


def test_wheel_mute():
    s = srv()
    from debrief.desktop import settings_io
    if sys.platform != "win32":
        s.stop_coach()
        s.wheel, s.wheel_backend_factory = None, None
        code, r = call("api/coach/wheel-learn", {})
        assert code == 400 and "can't be read on this computer" in r["error"], r
    be = FakeBackend()
    s.stop_coach()
    s.wheel, s.wheel_backend_factory = None, (lambda: be)
    w = call("api/coach/status")[1]["wheel"]
    assert w["state"] == "not_set" and w["binding"] is None and w["text"] == "No wheel button chosen yet.", w
    code, r = call("api/coach/wheel-learn", {})
    assert code == 200 and r["ok"] and r["wheel"]["state"] == "learning", r
    time.sleep(0.2)
    tap(be, 7)
    w = wheel_until(lambda w: w["binding"] is not None)
    assert w["label"] == "Button 7 on Simucube wheel" and "Saved" in w["note"], w
    assert settings_io.load_extra("coach")["wheel"]["button"] == 7
    assert not s.wheel.running(), "only listened for Learn: stopped again while the coach is off"
    # a timeout says so
    s.wheel_note = ""
    s.wheel.learn(on_learned=s._wheel_learned, timeout_s=1.0)
    w = wheel_until(lambda w: "No button was pressed" in (w.get("note") or ""), timeout=8)
    assert w["binding"]["button"] == 7, "a timed-out Learn keeps the saved button"
    # with the coach on: the button mutes and unmutes it
    use_fakes(s, lambda: Holding(make_new()))
    assert call("api/coach/start", {})[1]["ok"]
    try:
        coach_until(lambda st: st.get("running") and st.get("state") in ("coaching", "no_reference"))
        assert s.wheel.running(), "started with the coach"
        w = wheel_until(lambda w: w["state"] == "ready")
        assert "mutes and unmutes" in w["text"], w
        tap(be, 7)
        coach_until(lambda st: st.get("muted") is True, timeout=10)
        time.sleep(0.35)                                   # past the debounce
        tap(be, 7)
        coach_until(lambda st: st.get("muted") is False, timeout=10)
        tap(be, 3)                                         # another button does nothing
        assert call("api/coach/status")[1]["muted"] is False
    finally:
        call("api/coach/stop", {})
    assert not s.wheel.running(), "stopped with the coach"
    code, r = call("api/coach/wheel-clear", {})
    assert code == 200 and r["wheel"]["binding"] is None and "wheel" not in settings_io.load_extra("coach"), r
    # closing Debrief stops the poller even mid-Learn
    call("api/coach/wheel-learn", {})
    assert s.wheel.running()
    s.stop_coach()
    assert not s.wheel.running()


# ================================================================ first-run checklist
def test_firstrun():
    s = srv()
    make_old()
    read_telemetry()
    code, j = call("api/firstrun")
    assert code == 200 and j["ok"] and j["total"] == 6, j
    by = {x["id"]: x for x in j["steps"]}
    assert by["token"]["done"] and by["folder"]["done"] and by["telemetry"]["done"] and by["session"]["done"], by
    assert by["record"]["done"] and "telemetry files (.ibt)" in by["record"]["text"], by["record"]   # 2.18
    if not by["voice"]["done"]:
        assert j["next"] == "voice" and by["voice"]["action"] == "test_voice" and j["show"], j
        use_fakes(s, lambda: Holding(make_new()))
        s.stop_coach()
        code, r = call("api/coach/test-voice", {})
        assert code == 200 and r["ok"], r
        end = time.time() + 20
        while time.time() < end and not call("api/firstrun")[1]["all_done"]:
            time.sleep(0.1)
    j = call("api/firstrun")[1]
    assert j["all_done"] and not j["show"] and j["text"] == "You're all set.", j
    code, j = call("api/firstrun/dismiss", {})
    assert code == 200 and j["dismissed"], j
    code, j = call("api/firstrun/dismiss", {"dismissed": False})
    assert code == 200 and not j["dismissed"], j
    st = json.loads((TMP / "firstrun.json").read_text(encoding="utf-8"))
    assert st == {"voice_tested": True, "dismissed": False}, st
    code, _ = call("api/firstrun", header=False)
    assert code == 404


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


def no_dash_text(pg, sel):
    t = pg.text_content(sel)
    assert "—" not in t and "–" not in t, f"long dash in {sel}"


def test_practice_plan_in_the_window():
    s = srv()
    first, new, rv = stage_scored()
    shots = shots_dir()
    for dark in (False, True):
        r = page(dark=dark, scale=2 if shots else 1)
        if not r:
            return
        ctx, pg, errs = r
        try:
            pg.goto(s.url)
            pg.wait_for_function("() => document.querySelector('.navbtn[data-nav=practice]')")
            pg.keyboard.press("Control+0")
            pg.wait_for_selector("#vPractice", state="visible")
            pg.wait_for_selector("#prPlan .ptmain")
            top = pg.text_content("#prPlan")
            assert "This session's one thing" in top and "From " in top and "The check" in top, top
            assert pg.is_visible("#ptCoach") and pg.is_visible("#ptAnother")
            assert pg.text_content("#ptConf") in ("Clear pattern", "Likely", "Few laps", "Experiment"), pg.text_content("#ptConf")
            last = pg.text_content("#ptLast")
            assert "Last session" in last and "Next: " in last and ("You hit it on" in last or "didn't get" in last), last
            assert not __import__("re").search(r"\bT\d+\b", pg.text_content("#ptBody")), "corners read Turn N"
            assert pg.locator("#ptHistList li").count() >= 1
            no_dash_text(pg, "#prPlan")
            if shots:
                pg.evaluate("() => document.getElementById('ptHist').open = true")
                snap(pg, shots / f"practice-plan-{'dark' if dark else 'light'}.png")
            if dark:
                continue
            # the scored session shows its score
            pg.click(f"#prSessions .prrow[data-id='{new['id']}']")
            pg.wait_for_selector("#prOneThing .ptmain")
            sec = pg.text_content("#prOneThing")
            assert "Your one thing" in sec and ("You hit it on" in sec or "didn't get" in sec), sec
            no_dash_text(pg, "#prOneThing")
            # Coach this: to the Coach page with the plan's focus
            pg.click("#prBack")
            pg.wait_for_selector("#prPlan .ptmain")
            pg.click("#ptCoach")
            pg.wait_for_selector("#vCoach", state="visible")
            pg.wait_for_selector("#coFocus:not([hidden])")
            prog = pg.text_content("#coFocusProg")
            assert "Turn" in pg.text_content("#coFocusText"), pg.text_content("#coFocusText")
            # not started, or (this plan was driven with the coach earlier in these tests) the last drive's count
            assert prog == "Not started" or (prog.startswith("Hit ") and pg.text_content("#coFocusPill") == "Last drive"), prog
            # Pick another
            pg.keyboard.press("Control+0")
            pg.wait_for_selector("#prPlan .ptmain")
            pg.click("#ptAnother")
            pg.wait_for_function("() => /\\b(ok|bad)$/.test(document.getElementById('ptMsg').className)", timeout=60000)
            assert pg.get_attribute("#ptMsg", "class").endswith("ok"), pg.text_content("#ptMsg")
            # Help has the new words
            pg.click("#btnHelp")
            pg.click("[data-help=words]")
            words = pg.text_content("#helpBody")
            assert "One thing this session" in words and "Experiment" in words and "Wheel button mute" in words
            pg.click("#helpClose")
            assert not errs, errs
        finally:
            ctx.close()


def test_coach_page_with_focus_and_wheel():
    s = srv()
    stage_scored()
    shots = shots_dir()
    be = FakeBackend()
    s.stop_coach()
    s.wheel, s.wheel_backend_factory = None, (lambda: be)
    from debrief.desktop import settings_io
    data = settings_io.load_extra("coach")
    data["wheel"] = {"device": 1, "button": 7, "name": "Simucube wheel", "mid": 0x16D0, "pid": 0x0D5A}
    settings_io.save_extra("coach", data)
    use_fakes(s, lambda: Holding(make_new()))
    r = page(scale=2 if shots else 1)
    if not r:
        return
    ctx, pg, errs = r
    try:
        pg.goto(s.url)
        pg.wait_for_function("() => document.querySelector('.navbtn[data-nav=coach]')")
        pg.keyboard.press("Control+9")
        pg.wait_for_selector("#vCoach", state="visible")
        pg.wait_for_function("() => document.getElementById('coText').textContent === 'Coach off'")
        pg.evaluate("() => document.getElementById('coSet').open = true")
        pg.wait_for_function("() => document.getElementById('coWheelText').textContent.includes('Button 7 on Simucube wheel')")
        pg.click("#coStart")
        pg.wait_for_function("() => document.getElementById('coText').textContent.startsWith('Coaching')", timeout=30000)
        pg.wait_for_function("() => { const m = /^Hit \\d+ of (\\d+) so far$/.exec(document.getElementById('coFocusProg').textContent);"
                             " return !!m && +m[1] >= 3 && document.getElementById('coFocusPill').textContent === 'Watching' }",
                             timeout=60000)
        assert pg.text_content("#coFocusPill") == "Watching"
        assert "mutes and unmutes" in pg.text_content("#coWheelText")
        no_dash_text(pg, "#vCoach")
        if shots:
            snap(pg, shots / "coach214-light.png")
        pg.click("#coStart")
        pg.wait_for_function("() => document.getElementById('coText').textContent === 'Coach off'")
        assert not errs, errs
    finally:
        call("api/coach/stop", {})
        ctx.close()
        data = settings_io.load_extra("coach")
        data.pop("wheel", None)
        settings_io.save_extra("coach", data)


def test_firstrun_in_the_window():
    s = srv()
    make_old()
    shots = shots_dir()
    st = TMP / "firstrun.json"
    keep = st.read_text(encoding="utf-8") if st.exists() else None
    st.write_text("{}", encoding="utf-8")                  # voice not tested yet, not hidden
    r = page(scale=2 if shots else 1)
    if not r:
        return
    ctx, pg, errs = r
    try:
        pg.goto(s.url)
        pg.wait_for_selector("#firstRun:not([hidden]) .frstep")
        assert pg.evaluate("() => !document.getElementById('vWelcome').hidden"), "no reviews yet: the welcome page"
        assert pg.evaluate("() => document.getElementById('firstRun').closest('#vWelcome') !== null")
        w = call("api/firstrun")[1]
        assert w["total"] == 6 and not dict((x["id"], x["done"]) for x in w["steps"])["voice"], w
        assert pg.text_content("#frPill") == f"{w['done']} of 6 done", pg.text_content("#frPill")
        assert pg.locator("#frList .frstep.done").count() == w["done"]
        btn = pg.locator("#frList button[data-fr=test_voice]")
        assert btn.count() == 1 and btn.text_content() == "Test voice"     # 2.18: the button it names is right there
        no_dash_text(pg, "#firstRun")
        if shots:
            snap(pg, shots / "firstrun-light.png")
        btn.click()
        pg.wait_for_selector("#vCoach", state="visible")
        assert pg.evaluate("() => document.getElementById('coSet').open")
        pg.wait_for_function("() => document.getElementById('coSetMsg').textContent !== ''")   # it pressed Test voice
        time.sleep(1)
        st.write_text("{}", encoding="utf-8")              # the voice was heard: forget it, for the rest of the test
        # hide, then back from Help
        pg.goto(s.url)
        pg.wait_for_selector("#firstRun:not([hidden])")
        pg.click("#frHide")
        pg.wait_for_selector("#firstRun", state="hidden")
        assert json.loads(st.read_text(encoding="utf-8"))["dismissed"] is True
        pg.click("#btnHelp")
        pg.click("#helpFirstRun")
        pg.wait_for_selector("#firstRun:not([hidden])")
        assert json.loads(st.read_text(encoding="utf-8"))["dismissed"] is False
        assert not errs, errs
    finally:
        ctx.close()
        if keep is not None:
            st.write_text(keep, encoding="utf-8")


# ================================================================ 2.14 review: N3, N4, N5 through the server
def test_review_coach_follows_a_new_plan():
    """N3: a running coach switches to a plan made while it runs (Pick another), keyed by plan_id."""
    s = srv()
    stage_scored()
    cur = call("api/plan" + q(car=CAR, track=TRACK))[1]["plan"]
    use_fakes(s, lambda: Holding(make_new()))
    assert call("api/coach/start", {})[1]["ok"]
    try:
        st = coach_until(lambda st: (st.get("focus") or {}).get("plan_id") == cur["plan_id"])
        code, r = call("api/plan/new", {"car": CAR, "track": TRACK})
        assert code == 200 and r["ok"] and r["plan"]["plan_id"] != cur["plan_id"], r
        new_id = r["plan"]["plan_id"]
        st = coach_until(lambda st: (st.get("focus") or {}).get("plan_id") == new_id, timeout=30)
        assert s.coach_focus_info["plan_id"] == new_id, s.coach_focus_info
    finally:
        call("api/coach/stop", {})


def test_review_auto_score_waits_for_a_measured_lap():
    """N4: a newer session with no measured lap through the focus corner doesn't use up the plan's score."""
    srv()
    stage_scored()
    cur = call("api/plan" + q(car=CAR, track=TRACK))[1]["plan"]
    assert cur["kind"] in ("focus", "consistency") and not cur.get("score"), cur
    blind = dict(cur, success_rule=dict(cur["success_rule"], key="not_a_phase_number"))
    assert pp.save_plan(S.plans_folder(), CAR, TRACK, blind)
    cl.make(TEL / f"{CAR}_{TRACK} 2026-11-06 18-00-00.ibt", 8, seed=41, session="Practice")
    read_telemetry()
    sid = sessions()[0]["id"]
    assert "2026-11-06" in sessions()[0]["date"], sessions()[0]
    code, rv = call("api/session-review" + q(id=sid))
    assert code == 200 and rv["ok"] and rv["one_thing"] is None, rv["one_thing"]
    now = pp.load_plans(S.plans_folder(), CAR, TRACK)[0]
    assert now["plan_id"] == cur["plan_id"] and not now.get("score"), "not scored: still waiting"
    assert pp.save_plan(S.plans_folder(), CAR, TRACK, {k: v for k, v in cur.items() if k != "score"})
    code, rv = call("api/session-review" + q(id=sid))
    o = rv["one_thing"]
    assert o and o["just_scored"] and o["plan"]["plan_id"] == cur["plan_id"] and o["plan"]["score"]["measured"] >= 1, o


def test_review_coach_json_writes_dont_clash():
    """N5: the wheel button and the coach settings saved at the same moment both stay in coach.json."""
    s = srv()
    from debrief.desktop import settings_io
    s.stop_coach()
    errs = []

    def wheel(k):
        try:
            s._wheel_learned({"device": 1, "button": 1 + k % 30, "name": "W", "mid": 1, "pid": 2,
                              "label": f"Button {1 + k % 30} on W"})
        except Exception as e:  # noqa: BLE001
            errs.append(e)

    def settings_(k):
        code, r = call("api/coach/settings", {"settings": {"volume": 50 + k % 40}})
        if code != 200:
            errs.append(r)
    ts = []
    for k in range(12):
        ts += [threading.Thread(target=wheel, args=(k,)), threading.Thread(target=settings_, args=(k,))]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    data = settings_io.load_extra("coach")
    assert not errs, errs
    assert "wheel" in data and "settings" in data and 50 <= data["settings"]["volume"] < 90, data
    call("api/coach/wheel-clear", {})


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
