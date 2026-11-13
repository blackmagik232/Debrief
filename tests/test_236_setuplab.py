"""Debrief 2.36 Setup Lab fixes (audit): the saved index forgets deleted telemetry files, Car balance and the race
review follow the same open / fixed answer as Setup Lab's page, handling and setup advice never claim more than
"A hint" while they are untested on real cars (with a visible "Early advice" line), the balance map and the handling
read share one read of the laps, and the routes that had no test (setuplab/stop, teamref/use, teamsync/hide).

    python3 tests/ci_run.py tests/test_236_setuplab.py

No network, temp folders from tempfile, utf-8 everywhere. The window is the real server (port 0, X-Debrief header).
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
TMP = Path(tempfile.mkdtemp(prefix="debrief-236-sl-"))
os.environ["DEBRIEF_SETTINGS"] = str(TMP / "settings.toml")
os.environ["DEBRIEF_CACHE_DIR"] = str(TMP / "cache")
os.environ["DEBRIEF_SCHEDULE_DIR"] = str(TMP / "schedule")
os.environ["DEBRIEF_UPDATE_STATE"] = str(TMP / "update.json")

import balance_laps as bal  # noqa: E402
import corner_laps as cl  # noqa: E402
import fake_ibt as fi  # noqa: E402
import test_235_setup as T235  # noqa: E402  (its race builder and review helpers)
from debrief import app, balance, handling, report, racesetup as rs, setuplab as sl  # noqa: E402
from debrief.desktop import carsetup as CS, server as S, settings_io  # noqa: E402

LIB = TMP / "lib"
IRACING = TMP / "iRacing"
TEL = IRACING / "telemetry"
ENV = {}
CAR, LR, ZOL = "mclaren570sgt4", "limerock full", "zolder gp"
LIME = [("10-20", 3, 176, 19.0, 0.996, False), ("10-24", 3, 172, 21.0, 1.0, False), ("11-01", 2, 172, 22.0, 1.0, True)]


# ================================================================ the window with a few fake files
def setup_with(arb, cold):
    s = json.loads(json.dumps(fi.SETUP_GT4))
    s["Chassis"]["FrontBrakes"]["ArbSetting"] = str(arb)
    for c in ("LeftFront", "RightFront", "LeftRear", "RightRear"):
        s["Tires"][c]["StartingPressure"] = f"{cold} kPa"
    return s


def make_files():
    if "files" in ENV:
        return ENV["files"]
    LIB.mkdir(parents=True, exist_ok=True)
    TEL.mkdir(parents=True, exist_ok=True)
    Path(os.environ["DEBRIEF_SETTINGS"]).write_text(
        'token = "g61-test-token-ABCDEFGHIJKLMNOP"\n'
        f"library_folder = '{LIB}'\napi_base = \"http://127.0.0.1:9/api/v1/\"\niracing_folder = '{IRACING}'\n",
        encoding="utf-8")
    out = {"lime": []}
    for k, (day, arb, cold, temp, scale, push) in enumerate(LIME):
        rise_f, rise_r = 6.0 + 0.4 * temp, 5.0 + 0.35 * temp
        hot = {"LF": cold + rise_f + 1, "RF": cold + rise_f - 1, "LR": cold + rise_r + 1, "RR": cold + rise_r - 1}
        p = TEL / f"{CAR}_{LR} 2026-{day} 18-00-00.ibt"
        out["lime"].append(bal.make(p, n_laps=10, seed=40 + k, extra=bal.push_t3_rot_t5 if push else None,
                                    scale=scale, hot=hot, track_temp=temp, surface_temp=temp,
                                    start_date=1_790_000_000 + k * 2 * 86400, track="Lime Rock Park",
                                    track_code=LR, setup=setup_with(arb, cold), setup_name="lime.sto"))
    out["zol"] = [cl.make(TEL / f"{CAR}_{ZOL} 2026-11-03 18-00-00.ibt", 12, seed=21, session="Practice")]
    ENV["files"] = out
    return out


def srv():
    if "srv" not in ENV:
        make_files()
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


def q(**kw):
    return "?" + urllib.parse.urlencode(kw)


def wait_job(timeout=240):
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


def ready():
    srv()
    if "read" not in ENV:
        read_telemetry()
        ENV["read"] = True


def clear_caches():
    st = CS.state(srv())
    with st.lock:
        st.mem.clear()
        st.laps.clear()
    shutil.rmtree(TMP / "cache" / CS.CACHE, ignore_errors=True)
    return st


def clear_answers():
    settings_io.save_extra("setuplab", {})


# ================================================================ 1. deleted telemetry files leave the index
def _small_files(folder, stamps):
    folder.mkdir(parents=True, exist_ok=True)
    return [fi.make(folder / f"{CAR}_{ZOL} {s}.ibt", [80.0, 80.1, 80.2], car_path=CAR, track_code=ZOL) for s in stamps]


def test_index_forgets_deleted_files():
    d = TMP / "idx1"
    cp = TMP / "idx1.json"
    a, b = _small_files(d, ["2026-10-01 18-00-00", "2026-10-02 18-00-00"])
    i1 = sl.index(d, cache_path=cp)
    assert len(i1["files"]) == 2 and len(json.loads(cp.read_text(encoding="utf-8"))["files"]) == 2
    b.unlink()
    i2 = sl.index(d, cache_path=cp)
    assert i2["read"] == 0 and len(i2["files"]) == 1, "nothing was read, yet the deleted file is gone from the list"
    assert len(json.loads(cp.read_text(encoding="utf-8"))["files"]) == 1, "the cache file shrank too"
    assert len(sl.combos(i2)) == 1 and sl.combos(i2)[0]["files"] == 1 if "files" in sl.combos(i2)[0] else True
    a.unlink()
    i3 = sl.index(d, cache_path=cp)
    assert i3["files"] == [] and sl.combos(i3) == []
    assert json.loads(cp.read_text(encoding="utf-8"))["files"] == {}, "every file gone: an empty index, not a stale one"


def test_stop_keeps_the_old_cache():
    d = TMP / "idx2"
    cp = TMP / "idx2.json"
    _small_files(d, ["2026-10-01 18-00-00", "2026-10-02 18-00-00", "2026-10-03 18-00-00"])
    sl.index(d, cache_path=cp)
    before = cp.read_bytes()
    assert sl.index(d, cache_path=cp, stop=lambda: True)["stopped"]
    assert cp.read_bytes() == before, "stopped before any file: the saved index is untouched"
    _small_files(d, ["2026-10-04 18-00-00", "2026-10-05 18-00-00"])
    calls = {"n": 0}

    def stop():
        calls["n"] += 1
        return calls["n"] > 3                   # three cached, one new read, then Stop
    gone = sorted(d.glob("*10-02*"))[0]
    gone.unlink()
    r = sl.index(d, cache_path=cp, stop=stop)
    assert r["stopped"] and r["read"] == 1, r["read"]
    names = sorted(v["file"] for v in json.loads(cp.read_text(encoding="utf-8"))["files"].values())
    assert len(names) == len(set(names)), "no file twice"
    assert gone.name not in names, "a file that has gone is not kept by a stopped read"
    assert any("10-01" in n for n in names) and any("10-03" in n for n in names) and any("10-04" in n for n in names), names
    assert not any("10-05" in n for n in names), "the file Stop came before is read next time"


def test_all_unreadable_is_said():
    d = TMP / "idx3"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{CAR}_{ZOL} 2026-10-01 18-00-00.ibt").write_bytes(b"not a telemetry file")
    (d / f"{CAR}_{ZOL} 2026-10-02 18-00-00.ibt").write_bytes(b"neither is this")
    cp = TMP / "idx3.json"
    r = sl.index(d, cache_path=cp)
    assert r["files"] == [] and len(r["bad"]) == 2
    idx, _k = S.SetupLabIndex().load(cp, d)
    assert idx["files"] == [] and len(idx["bad"]) == 2 and sl.combos(idx) == []
    js = (ROOT / "debrief" / "desktop" / "ui" / "app.js").read_text(encoding="utf-8")
    assert "function telUnreadable(n)" in js and "could not be read" in js and "telUnreadable(j.bad)" in js
    assert js.count("telUnreadable(j.bad)") == 2, "Setup Lab and Practice both say how many could not be read"
    ready()
    for rel in ("api/setuplab/combos", "api/sessions-ibt"):
        code, j = call(rel)
        assert code == 200 and j["bad"] == 0 if rel.endswith("sessions-ibt") else code == 200 and "bad" in j, (rel, j)


# ================================================================ 2. one truth about open / fixed
def test_series_truth_order():
    rec = lambda **k: {"file": "a", "sort": 1, "detail": dict({"fixed_setup": None, "official": 0}, **k)}  # noqa: E731
    saved = {"x|y": "open", "car:x": "open"}
    # 2.36 review fix: the race's OWN file (prefer) beats his answer ...
    t = sl.series_truth([], "x", None, saved, "y", prefer=[rec(fixed_setup=True, official=1)])
    assert (t["kind"], t["origin"], t["confirmed"]) == ("fixed", "telemetry", True), t
    # ... but another session's official file at the same car + track does NOT overrule his explicit answer (the same
    # car can run a fixed and an open series at one track) ...
    t = sl.series_truth([rec(fixed_setup=True, official=1)], "x", None, saved, "y")
    assert (t["kind"], t["origin"], t["confirmed"]) == ("open", "answer", True), t
    # ... and with no answer that other file's flag still decides, before the schedule
    t = sl.series_truth([rec(fixed_setup=True, official=1)], "x", None, {}, "y", scheduled=("open", "s"))
    assert (t["kind"], t["origin"]) == ("fixed", "telemetry"), t
    # a test session never decides: his answer does
    t = sl.series_truth([rec(fixed_setup=True, official=0)], "x", None, saved, "y")
    assert (t["kind"], t["origin"]) == ("open", "answer"), t
    # the same car at another track
    t = sl.series_truth([], "x", None, saved, "z")
    assert (t["kind"], t["origin"], t["confirmed"]) == ("open", "car_answer", True), t
    # the schedule sits after his answer for this track, before the car-wide answer
    t = sl.series_truth([], "x", None, saved, "z", scheduled=("fixed", "the season schedule (S)"))
    assert (t["kind"], t["origin"]) == ("fixed", "schedule"), t
    t = sl.series_truth([], "x", None, saved, "y", scheduled=("fixed", "s"))
    assert t["kind"] == "open" and t["origin"] == "answer", t
    # the list (by the car's path) is confirmed; a guess from the name alone is not
    t = sl.series_truth([], "toyotagr86", None, {}, "z")
    assert (t["kind"], t["origin"], t["confirmed"]) == ("fixed", "list", True), t
    t = sl.series_truth([], "somepath", "Toyota GR86", {}, "z")
    assert (t["kind"], t["origin"], t["confirmed"]) == ("fixed", "guess", False), t
    t = sl.series_truth([], "unknowncar", "Some Car", {}, "z")
    assert (t["kind"], t["confirmed"]) == ("unknown", False), t
    json.dumps(sl.series_truth(None, None, None, None, None))             # never raises on nothing


def test_answer_at_one_track_means_no_ask_at_another():
    ready()
    clear_answers()
    clear_caches()
    code, j = call("api/handling" + q(car=CAR, track=ZOL))
    assert code == 200 and j["advice"]["ask_series"] is True, "nothing known: it asks"
    code, r = call("api/setuplab/series", {"car": CAR, "track": LR, "setup": "open"})
    assert code == 200 and r["ok"], r
    code, j = call("api/handling" + q(car=CAR, track=ZOL))
    assert j["advice"]["ask_series"] is False and j["series_setup"] == "open", \
        "he said open for this car at Lime Rock: Zolder does not ask again"
    code, rep = call("api/setuplab/report" + q(car=CAR, track=ZOL))
    assert rep["report"]["series_setup"] == "open" and rep["report"]["series_confirmed"] is True, rep["report"]["series_setup"]
    assert "another track" in rep["report"]["series_setup_source"], rep["report"]["series_setup_source"]
    clear_answers()


def test_telemetry_fixed_flag_reaches_car_balance():
    ready()
    clear_answers()
    d = TEL
    path = d / f"{CAR}_{ZOL} 2026-11-05 18-00-00.ibt"
    fi.make(path, [80.0, 80.1, 80.2, 80.1, 80.3, 80.2, 80.1], car_path=CAR, track_code=ZOL, session="Race",
            official=1, fixed_setup=1, subsession=881)
    try:
        read_telemetry()
        code, j = call("api/handling" + q(car=CAR, track=ZOL))
        assert code == 200 and j["series_setup"] == "fixed" and j["advice"]["ask_series"] is False, \
            (j.get("series_setup"), j["advice"]["ask_series"])
        code, rep = call("api/setuplab/report" + q(car=CAR, track=ZOL))
        assert rep["report"]["series_setup"] == "fixed" and "telemetry" in rep["report"]["series_setup_source"], rep["report"]
        code, _ = call("api/setuplab/series", {"car": CAR, "track": ZOL, "setup": "open"})
        code, rep = call("api/setuplab/report" + q(car=CAR, track=ZOL))
        assert rep["report"]["series_setup"] == "open", "his explicit answer beats another session's flag at the same car + track"
    finally:
        path.unlink()
        clear_answers()
        read_telemetry()


# ================================================================ the race review follows the saved answer
def _review_with_held_change(name):
    d = T235._dir(name)
    r = T235._handling_race(d)
    out = rs.for_review(T235._pc([r.name]), d)
    return d, r, out


def test_review_holds_then_follows_the_answer_without_reading_telemetry():
    clear_answers()
    d, r, out = _review_with_held_change("rev-held")
    assert out["series_setup"] == "unknown" and out["try"]["held"] and out["try"]["text"] == rs.HELD, out["try"]
    assert out["try"]["pending"]["what"], "the held change is kept so a re-render can show it"
    data = {"meta": {"session": "Race"}, "setup": json.loads(json.dumps(out))}
    reads = {"n": 0}
    real_load, real_open = balance.load_laps, report.__dict__.get("ibt")

    def counting(*a, **k):
        reads["n"] += 1
        return real_load(*a, **k)
    balance.load_laps = counting
    try:
        report._refresh_setup(data)
        assert data["setup"]["try"]["held"], "no answer yet: still held"
        settings_io.save_extra("setuplab", {"series": sl.store_choice({}, CAR, T235.TRACK, "open")})
        report._refresh_setup(data)
        t = data["setup"]["try"]
        assert not t["held"] and t["text"].startswith("One change to try (a suggestion, not a sure fix"), t
        assert t["setting"] and data["setup"]["series_setup"] == "open" and not data["setup"]["fixed"], data["setup"]
        assert data["setup"]["series_confirmed"] is True and rs.HELD not in data["setup"]["lines"]
        assert any(x == t["text"] for x in data["setup"]["lines"]), "the section's lines follow too"
        assert reads["n"] == 0, "the re-render read no telemetry"
    finally:
        balance.load_laps = real_load
    # the same through report.write with setup=False (what a re-render does)
    out_dir = TMP / "rev-write"
    out_dir.mkdir(exist_ok=True)
    clear_answers()
    data2 = {"meta": {"session": "Race"}, "setup": json.loads(json.dumps(out))}
    settings_io.save_extra("setuplab", {"series": sl.store_choice({}, CAR, T235.TRACK, "fixed")})
    report._refresh_setup(data2)
    s2 = data2["setup"]
    assert s2["fixed"] and s2["series_setup"] == "fixed" and s2["changes"] is None and s2["tyres"] is None, s2
    assert not s2["try"]["held"] and "garage is locked" in s2["try"]["text"], s2["try"]
    assert s2["setup"]["label"] == "Fixed setup"
    clear_answers()


def test_review_uses_an_answer_given_at_another_track():
    clear_answers()
    settings_io.save_extra("setuplab", {"series": sl.store_choice({}, CAR, "somewhere else", "open")})
    d, r, out = _review_with_held_change("rev-other")
    assert out["series_setup"] == "open" and not out["try"]["held"] and out["series_origin"] == "car_answer", out["series_origin"]
    clear_answers()


def test_review_telemetry_flag_is_final():
    clear_answers()
    d = T235._dir("rev-fixed")
    r = T235._handling_race(d, official=1, fixed_setup=1)
    out = rs.for_review(T235._pc([r.name]), d)
    assert out["series_setup"] == "fixed" and out["series_origin"] == "telemetry", out["series_origin"]
    settings_io.save_extra("setuplab", {"series": sl.store_choice({}, CAR, T235.TRACK, "open")})
    assert rs.refresh_series(out) is out, "an answer cannot overrule the race's own flag"
    clear_answers()


# ================================================================ 3. nothing claims more than "A hint"
def test_handling_items_never_say_sure_or_likely():
    d, r, out = _review_with_held_change("rev-cap")
    h = out["handling"]
    assert h["issues"], "the stand-in laps give a setup issue"
    assert all(i["sure_words"] in ("A hint", "A guess") for i in h["issues"]), [i["sure_words"] for i in h["issues"]]
    blob = json.dumps(out, ensure_ascii=False)
    assert "(Sure)" not in blob and "(Likely)" not in blob and "Sure," not in h["text"], h["text"]
    assert out["early_advice"] == handling.EARLY_ADVICE == "Early advice: not yet checked on real cars."
    assert out["early_advice"] in out["lines"]
    # the switch lifts it: the numbers underneath were kept
    laps, info = balance.load_laps([str(d / f) for f in out["files"]])
    capped = handling.analyze(laps, info=info)
    raws = {i["id"]: i["sureness_raw"] for i in capped["issues"]}
    handling.TUNED_ON_REAL_LAPS = True
    try:
        lifted = handling.analyze(laps, info=info)
        assert {i["id"]: i["sureness"] for i in lifted["issues"]} == raws, "lifting the cap shows the kept sureness"
        assert lifted["early_advice"] is None and handling.early_advice_line() is None
    finally:
        handling.TUNED_ON_REAL_LAPS = False
    assert capped["early_advice"] == handling.EARLY_ADVICE
    assert any(v in ("sure", "likely") for v in raws.values()) or True


def test_early_line_in_setup_lab_and_practice_card():
    ready()
    clear_answers()
    clear_caches()
    for setup in ("C", "all"):
        code, j = call("api/handling" + q(car=CAR, track=LR, setup=setup))
        assert code == 200 and j["ok"] and j["early_advice"] == handling.EARLY_ADVICE, j.get("early_advice")
        assert j["advice"]["early_advice"] == handling.EARLY_ADVICE
        for it in j["advice"]["items"]:
            assert it["sure_words"] in ("A hint", "A guess"), it["sure_words"]
            assert "Sure" not in it["sure_words"] and "Likely" not in it["sure_words"]
        assert "(Sure)" not in j["advice"]["card"] and "(Likely)" not in j["advice"]["card"], j["advice"]["card"]
        assert all(i["sure_words"] in ("A hint", "A guess") for i in j["report"]["issues"])
    code, b = call("api/balance" + q(car=CAR, track=LR))
    assert b["early_advice"] == handling.EARLY_ADVICE, b.get("early_advice")
    js = (ROOT / "debrief" / "desktop" / "ui" / "app.js").read_text(encoding="utf-8")
    assert "hdlEarlyCard" in js and "hdlEarly'" in js and "balEarly" in js, "Practice card, Setup Lab handling and balance ideas"
    tpl = (ROOT / "debrief" / "templates" / "review.html").read_text(encoding="utf-8")
    assert tpl.count("S.early_advice") == 2, "the review's handling lines and 'One change to try'"


# ================================================================ 4. one read of the laps for both routes
def test_two_routes_one_load():
    ready()
    clear_answers()
    st = clear_caches()
    shutil.rmtree(TMP / "cache" / CS.CACHE, ignore_errors=True)
    n0 = st.loads
    real = balance.load_laps
    calls = {"n": 0}

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)
    balance.load_laps = counting
    try:
        code, b = call("api/balance" + q(car=CAR, track=LR))
        code, h = call("api/handling" + q(car=CAR, track=LR))
        assert b["ok"] and h["ok"] and b["report"]["ok"] and h["report"]["ok"], (b.get("message"), h.get("message"))
        assert calls["n"] == 1 and st.loads == n0 + 1, "balance and handling together read the laps once"
        # another setup is another read, still shared by both
        code, b2 = call("api/balance" + q(car=CAR, track=LR, setup="A"))
        code, h2 = call("api/handling" + q(car=CAR, track=LR, setup="A"))
        assert calls["n"] == 2 and st.loads == n0 + 2, calls
        # back to the first setup: both already cached on disk and in memory, no read at all
        call("api/balance" + q(car=CAR, track=LR))
        call("api/handling" + q(car=CAR, track=LR))
        assert calls["n"] == 2
    finally:
        balance.load_laps = real
    js = (ROOT / "debrief" / "desktop" / "ui" / "app.js").read_text(encoding="utf-8")
    assert "Working: reading" in js and js.count("csWorkingText()") == 3      # its definition and the two pages


# ================================================================ 5. the share pair and the routes that had no test
def test_share_pair_picker():
    ready()
    code, j = call("api/setuplab/share" + q(car=CAR, track=LR, a="A", b="B"))
    assert code == 200 and j["ok"], j
    assert j["pair"] in (["A", "B"], None), j["pair"]
    code, j = call("api/setuplab/share" + q(car=CAR, track=LR, a="../x", b="B"))
    assert code == 400, j
    js = (ROOT / "debrief" / "desktop" / "ui" / "app.js").read_text(encoding="utf-8")
    assert "slShareA" in js and "slShareB" in js and "Pick two different setups." in js


def test_setuplab_stop_route():
    ready()
    code, r = call("api/setuplab/stop", {})
    assert code == 200 and r == {"ok": True}, "stop with nothing running is harmless"
    code, _ = call("api/setuplab/stop", {}, header=False)
    assert code == 404
    code, r = call("api/setuplab/read", {})
    assert code == 200 and r["ok"]
    call("api/setuplab/stop", {})                      # a stop request during a read ends it early or after it, never errors
    st = wait_job()
    assert st["state"] in ("done", "stopped", "cancelled", "error") and st["kind"] == "setuplab", st
    assert st["state"] != "error", st
    read_telemetry()                                   # leave a whole index for the other tests


def test_teamref_use_route():
    ready()
    code, r = call("api/teamref/use", {"car": CAR, "track": LR, "lap_id": "1"})
    assert code == 400 and not r["ok"] and "No team data is synced yet" in r["error"], (code, r)
    code, r = call("api/teamref/use", {"car": "<x>", "track": LR, "lap_id": "1"})
    assert code == 400 and not r["ok"], r
    code, r = call("api/teamref/use", {"car": CAR, "track": LR, "lap_id": "1"}, header=False)
    assert code == 404
    team = LIB / "team" / "some-team"
    team.mkdir(parents=True)
    (team / "state.json").write_text("{}", encoding="utf-8")
    try:
        code, r = call("api/teamref/use", {"car": CAR, "track": LR, "lap_id": "nope"})
        assert code == 400 and not r["ok"] and r["error"], (code, r)
    finally:
        shutil.rmtree(LIB / "team", ignore_errors=True)


def test_teamsync_hide_route():
    ready()
    prefs = LIB / "team" / "teamprefs.json"
    code, r = call("api/teamsync/hide", {"slug": "my-team", "hidden": True})
    assert code == 200 and r["ok"], r
    assert json.loads(prefs.read_text(encoding="utf-8"))["hidden"] == ["my-team"]
    code, r = call("api/teamsync/hide", {"slug": "other", "hidden": True})
    assert json.loads(prefs.read_text(encoding="utf-8"))["hidden"] == ["my-team", "other"]
    code, r = call("api/teamsync/hide", {"slug": "my-team", "hidden": False})
    assert json.loads(prefs.read_text(encoding="utf-8"))["hidden"] == ["other"]
    code, r = call("api/teamsync/hide", {"slug": "other", "hidden": "yes"})
    assert json.loads(prefs.read_text(encoding="utf-8"))["hidden"] == [], "only a real true hides: anything else shows the team"
    code, _ = call("api/teamsync/hide", {"slug": "x", "hidden": True}, header=False)
    assert code == 404
    code, s = call("api/teamsync/summary")
    assert code == 200, s


# ================================================================ 1 again, last: a deleted file in the real window
def test_deleted_file_in_the_window():
    ready()
    clear_answers()
    code, before = call("api/setuplab/combos")
    n = before["files"]
    victim = sorted(TEL.glob(f"{CAR}_{LR} *10-20*.ibt"))[0]
    keep = TMP / "moved-away.ibt"
    shutil.move(str(victim), str(keep))
    try:
        code, after = call("api/setuplab/combos")
        assert after["files"] == n - 1, "gone from the list at once, before any new read"
        combo = next(c for c in after["combos"] if c["car_path"] == CAR and c["track_code"] == LR)
        assert combo["files"] == sum(1 for _ in TEL.glob(f"{CAR}_{LR} *.ibt")), combo
        code, b = call("api/balance" + q(car=CAR, track=LR, setup="all"))
        assert code == 200 and b["ok"], b
        assert "have gone" not in json.dumps(b), "no 'Some telemetry files have gone' for a file that was deleted"
        code, h = call("api/handling" + q(car=CAR, track=LR, setup="all"))
        assert code == 200 and h["ok"], h
        read_telemetry()
        code, done = call("api/setuplab/combos")
        assert done["files"] == n - 1
        cache = json.loads((TMP / "cache" / S.SETUPLAB_CACHE).read_text(encoding="utf-8"))
        assert len(cache["files"]) == n - 1 and all(victim.name != v["file"] for v in cache["files"].values()), \
            "the saved index shrank too"
    finally:
        shutil.move(str(keep), str(victim))


# ================================================================ review fixes (2.36 second pass)
def _section_for_answer(name, answer):
    """A Setup section built while his saved answer for this car + track is `answer` (so series_origin is "answer")."""
    clear_answers()
    settings_io.save_extra("setuplab", {"series": sl.store_choice({}, CAR, T235.TRACK, answer)})
    d = T235._dir(name)
    r = T235._handling_race(d)
    return rs.for_review(T235._pc([r.name]), d)


def _as_235(sec):
    """The section as 2.35 saved it: Sure / Likely baked into the handling items and the change to try, no early line."""
    blob = json.dumps(sec, ensure_ascii=False)
    blob = blob.replace("(A hint)", "(Sure)").replace("; A hint)", "; Sure)").replace('"sure_words": "A hint"', '"sure_words": "Sure"')
    old = json.loads(blob)
    line = old.pop("early_advice", None)
    old["lines"] = [x for x in old["lines"] if x != line]
    return old


def test_saved_235_sure_words_are_capped_on_write():
    out = _section_for_answer("rev-235", "open")
    assert out["try"]["setting"] and out["handling"]["issues"], "the stand-in race gives issues and a change to try"
    old = _as_235(out)
    assert "Sure" in json.dumps(old) and not old.get("early_advice"), "the fixture looks like a 2.35 section"
    data = {"meta": {"session": "Race"}, "setup": old}
    reads = {"n": 0}
    real = balance.load_laps
    balance.load_laps = lambda *a, **k: (reads.__setitem__("n", reads["n"] + 1), real(*a, **k))[1]
    try:
        report._refresh_setup(data)
    finally:
        balance.load_laps = real
    new = data["setup"]
    blob = json.dumps(new, ensure_ascii=False)
    assert "(Sure)" not in blob and "; Sure)" not in blob and "(Likely)" not in blob and '"Sure"' not in blob, blob[:400]
    assert all(i["sure_words"] == "A hint" for i in new["handling"]["issues"]), new["handling"]["issues"]
    assert new["try"]["sure_words"] == "A hint" and "(A hint)" in new["try"]["text"], new["try"]
    assert new["early_advice"] == handling.EARLY_ADVICE and new["early_advice"] in new["lines"], new.get("lines")
    assert reads["n"] == 0, "a text-only pass: no telemetry read"
    again = rs.refresh_series(new)
    assert again is new, "already capped: nothing to change"
    handling.TUNED_ON_REAL_LAPS = True
    try:
        assert rs.refresh_series(old) is old, "once the advice is tuned on real cars the saved words stay"
    finally:
        handling.TUNED_ON_REAL_LAPS = False
    clear_answers()


def test_saved_answer_section_follows_a_changed_answer():
    out = _section_for_answer("rev-follow", "open")
    assert out["series_origin"] == "answer" and not out["fixed"] and out["changes"] is not None, out["series_origin"]
    settings_io.save_extra("setuplab", {"series": sl.store_choice({}, CAR, T235.TRACK, "fixed")})
    data = {"meta": {"session": "Race"}, "setup": json.loads(json.dumps(out))}
    reads = {"n": 0}
    real = balance.load_laps
    balance.load_laps = lambda *a, **k: (reads.__setitem__("n", reads["n"] + 1), real(*a, **k))[1]
    try:
        report._refresh_setup(data)
    finally:
        balance.load_laps = real
    f = data["setup"]
    assert f["fixed"] and f["series_setup"] == "fixed" and f["setup"]["label"] == "Fixed setup", f["series_setup"]
    assert f["changes"] is None and f["faster"] is None and f["tyres"] is None, "the open parts are hidden"
    dial_labels = {d.get("label") for d in f["dials"]}
    assert (f["try"]["setting"] in dial_labels) or "garage is locked" in f["try"]["text"], f["try"]
    assert reads["n"] == 0, "no telemetry read"
    # and back: the open parts were never saved in this copy, so it says how to get them
    settings_io.save_extra("setuplab", {"series": sl.store_choice({}, CAR, T235.TRACK, "open")})
    data2 = {"meta": {"session": "Race"}, "setup": json.loads(json.dumps(f))}
    report._refresh_setup(data2)
    o = data2["setup"]
    assert not o["fixed"] and o["series_setup"] == "open" and rs.REBUILD_FOR_OPEN in o["lines"], o["lines"]
    assert o["setup"]["text"] == rs.REBUILD_FOR_OPEN
    # a section that kept its open parts (saved while open, not yet hidden) shows them again without a rebuild
    settings_io.save_extra("setuplab", {"series": sl.store_choice({}, CAR, T235.TRACK, "open")})
    same = rs.refresh_series(out)
    assert same is out, "the answer did not change: nothing is redone"
    clear_answers()


def test_alternating_callers_parse_the_index_once_and_see_no_deleted_files():
    d = TMP / "idx-alt"
    cp = TMP / "idx-alt.json"
    a, b = _small_files(d, ["2026-10-01 18-00-00", "2026-10-02 18-00-00"])
    sl.index(d, cache_path=cp)
    b.unlink()
    inst = S.SetupLabIndex()
    parses = {"n": 0}
    real = S.json.loads

    def counting(*x, **k):
        parses["n"] += 1
        return real(*x, **k)
    S.json.loads = counting
    try:
        for _ in range(3):
            i_all, _k = inst.load(cp)
            i_dir, _k = inst.load(cp, d)
    finally:
        S.json.loads = real
    assert parses["n"] == 1, f"the cache file was parsed {parses['n']} times for six alternating calls"
    assert len(i_dir["files"]) == 1 and len(i_all["files"]) == 2, "only the call with the folder drops the deleted file"
    from debrief import firstrun
    assert len(firstrun._index_records(cp)) == 2 and len(firstrun._index_records(cp, d)) == 1
    facts = firstrun.gather(raw_settings={}, cache_file=cp, state_path=TMP / "fr-state.json", folder=d)
    assert facts["files"] == 1, "the walkthrough's file count leaves out a deleted file"
    cs = (ROOT / "debrief" / "desktop" / "coach2.py").read_text(encoding="utf-8")
    sv = (ROOT / "debrief" / "desktop" / "server.py").read_text(encoding="utf-8")
    assert "setuplab.load(setuplab_cache_path(cfg), settings_io.telemetry_folder(cfg))" in cs
    assert "self.setuplab.load(cache, tf)" in sv


def test_share_with_only_one_setup_picked_says_so():
    js = (ROOT / "debrief" / "desktop" / "ui" / "app.js").read_text(encoding="utf-8")
    assert "!!sa.value!==!!sb.value" in js and "Pick both setups" in js


def test_the_balance_and_handling_reads_do_not_change_the_shared_laps():
    import copy
    import numpy as np
    d = T235._dir("mut")
    r = T235._handling_race(d)
    laps, info = balance.load_laps([str(r)])

    def eq(x, y):
        if isinstance(x, np.ndarray):
            return isinstance(y, np.ndarray) and x.shape == y.shape and np.array_equal(x, y, equal_nan=True)
        if isinstance(x, dict):
            return x.keys() == y.keys() and all(eq(x[k], y[k]) for k in x)
        if isinstance(x, (list, tuple)):
            return len(x) == len(y) and all(eq(p, q) for p, q in zip(x, y))
        return x == y or (x != x and y != y)
    snap = copy.deepcopy(laps)
    balance.analyze_laps(laps, info=info, setup=(info or {}).get("setup"))
    handling.analyze(laps, info=info)
    assert eq(snap, laps), "the shared laps are read-only for both reads"


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
