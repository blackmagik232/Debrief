"""Debrief 2.18 lane G: team and sharing that explain themselves.

  - teammate cards are about the teammate ("Alex brakes at ...", never "you"), their header gives the gap between
    the two drivers, and a headline says who is quicker overall and where (cornercoach, teamref.headline);
  - the teammate laps Debrief already has for a car and track, and how to get a teammate's file
    (teamref.laps_on_hand, GET api/teamref/laps), and a race review finds its Practice session
    (GET api/teamref/for-review);
  - "Couldn't match this track or car to Garage 61" gives a next step (team.NO_MATCH_NOTE);
  - Share card saves into the Downloads folder and says where, never over a file (POST api/share-save);
  - the Car vs car dropdown names a one-car class "(1 car, nothing to compare)" (carcompare.option_label);
  - in the window: Refresh this week says why it can't be pressed beside it, with a link; Car vs car's dropdown
    with nothing to compare is disabled; the session page has the one-click "Compare with a teammate's lap".

    python3 tests/test_218_team.py

No network (the Garage 61 address is a closed local port), temp folders only. Browser checks use Playwright and
Chromium and are skipped when either is missing.
"""
from __future__ import annotations

import json
import os
import struct
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.modules.setdefault("irsdk", None)
TMP = Path(tempfile.mkdtemp(prefix="debrief-218g-"))
os.environ["DEBRIEF_SETTINGS"] = str(TMP / "settings.toml")
os.environ["DEBRIEF_CACHE_DIR"] = str(TMP / "cache")
os.environ["DEBRIEF_SCHEDULE_DIR"] = str(TMP / "schedule")
os.environ["DEBRIEF_UPDATE_STATE"] = str(TMP / "update.json")

import corner_laps as cl  # noqa: E402
from debrief import app, carcompare as CC, cornercoach as cc, team, teamref as tr  # noqa: E402
from debrief.desktop import server as S  # noqa: E402

LIB = TMP / "lib"
IRACING = TMP / "iRacing"
TEL = IRACING / "telemetry"
MATE = TMP / "from discord"
DOWN = TMP / "Downloads"
CAR, ZOL = "mclaren570sgt4", "zolder gp"
MATE_NAME = "Alex O'Neil"
REVIEW = "2026-11-03_zolder_race"
ENV, PW = {}, {}


# ================================================================ pure logic
def files():
    if "files" not in ENV:
        TEL.mkdir(parents=True, exist_ok=True)
        MATE.mkdir(parents=True, exist_ok=True)
        ENV["files"] = {
            "mine": [cl.make(TEL / f"{CAR}_{ZOL} 2026-10-31 18-30-00.ibt", 12, seed=12, session="Practice"),
                     cl.make(TEL / f"{CAR}_{ZOL} 2026-11-03 18-00-00.ibt", 12, seed=21, session="Practice")],
            "mate": cl.make(MATE / f"{CAR}_{ZOL} 2026-11-02 20-00-00.ibt", 6, seed=5, name=MATE_NAME)}
    return ENV["files"]


YOU = __import__("re").compile(r"\byou\b|\byour best\b", __import__("re").I)


def _no_you(text):
    """A teammate's sentence says nothing about "you" except "your typical lap(s)"."""
    return not YOU.search(text.replace("your typical", "").replace("your teammate", ""))


def test_reference_cards_are_about_the_teammate():
    f = files()
    r = tr.load_reference(f["mate"], CAR, ZOL)
    assert r["ok"], r["reason"]
    assert r["cornercoach"]["driver"] == MATE_NAME, "the corner coach lap carries the teammate's name"
    rep = cc.analyze_files([str(p) for p in f["mine"]], reference=r["cornercoach"])
    cards = rep["reference_cards"]
    assert rep["ok"] and cards, rep["message"]
    for c in cards:
        assert c["different"].startswith("Alex ") or c["different"].startswith("on Alex's lap"), c["different"]
        assert _no_you(c["different"]), c["different"]
        assert c["gap_text"].startswith("Alex ") and "quicker than your typical lap" in c["gap_text"], c["gap_text"]
        assert "best laps" not in c["gap_text"]
        assert c["driver"] == MATE_NAME
        assert f"{MATE_NAME}'s best lap was about" in c["text"], "the card's text still names the teammate in full"
    # a lap kept before 2.18 has no "driver": the name comes from its label
    old = dict(r["cornercoach"])
    old.pop("driver")
    assert cc.ref_driver(old) == MATE_NAME
    rep2 = cc.analyze_files([str(p) for p in f["mine"]], reference=old)
    assert [c["different"] for c in rep2["reference_cards"]] == [c["different"] for c in cards]
    # no name at all: "your teammate"
    anon = dict(old, label="your teammate's best lap")
    assert cc.ref_driver(anon) == ""
    rep3 = cc.analyze_files([str(p) for p in f["mine"]], reference=anon)
    for c in rep3["reference_cards"]:
        assert c["different"].startswith(("your teammate ", "on your teammate's lap")), c["different"]
    # own cards are unchanged: "on your best laps you ..."
    for c in rep["cards"]:
        assert c["gap_text"].startswith("best laps "), c["gap_text"]
        assert c["driver"] is None


def test_every_rule_is_said_in_the_third_person():
    D = {}
    for k in cc.METRICS:
        D[k] = {"best": 20.0, "typical": 30.0, "diff": -10.0, "z": 9.0, "se": 1.0, "n_best": 1, "n_typical": 9}
    D["brake_m"] = {"best": -60.0, "typical": -70.0, "diff": 10.0, "z": 9.0}
    D["pickup_m"] = {"best": -5.0, "typical": 20.0, "diff": 25.0, "z": 9.0}
    D["min_kmh"] = {"best": 104.0, "typical": 100.0, "diff": 4.0, "z": 9.0}
    D["entry_kmh"] = {"best": 180.0, "typical": 180.0, "diff": 0.0, "z": 0.0}
    D["coast_s"] = {"best": 0.2, "typical": 0.8, "diff": -0.6, "z": 9.0}
    P = {"abs": True, "abs_onset_pct": 90.0, "slip_method": "wheel speeds"}
    for rule in ("brake_later", "brake_earlier", "lighter", "trail", "coast", "release", "throttle", "carry",
                 "spin", "lock"):
        diff, how, why = cc._phrase(rule, D, P, "Sam Lee's best lap", driver="Sam Lee")
        assert diff and _no_you(diff), (rule, diff)
        assert diff.startswith(("Sam ", "on Sam's lap")), (rule, diff)
        assert " instead of " not in diff, (rule, diff)
        own = cc._phrase(rule, D, P)[0]
        assert own.startswith("on your best laps"), (rule, own)
    assert cc._phrase("carry", D, P, "x", driver="Sam Lee")[0].startswith("Sam arrives at the same speed as your typical lap")
    assert cc._phrase("lighter", D, P, "x", driver="Sam Lee")[0].startswith("Sam brakes at 20% where your typical lap has 30%")
    assert cc.first_name("Alex O'Neil: Jr") == "Alex" and cc.first_name("") == ""


def test_headline():
    cards = [{"corner": "T4", "lost_s": 0.21}, {"corner": "T7", "lost_s": 0.30}, {"corner": "T1", "lost_s": 0.06}]
    s = tr.headline("Alex O'Neil", 98.4, 98.9, cards)
    assert s.startswith("Alex is quicker overall: their best lap is 0.50 s faster than yours (1:38.400 vs 1:38.900)"), s
    assert s.endswith("Most of it is in T7 and T4."), s
    s = tr.headline("Alex O'Neil", 99.2, 98.9, cards[:1])
    assert s.startswith("You are quicker overall: your best lap is 0.30 s faster than Alex's"), s
    assert "Alex is still quicker than your typical laps in T4." in s, s
    assert tr.headline("Alex", 98.9, 98.9, []).startswith("Dead level")
    assert tr.headline("", 98.4, 98.9, []).startswith("Your teammate is quicker overall")
    assert tr.headline("Alex", None, 98.9, cards) == "" and tr.headline("Alex", 98.0, 0, cards) == ""


def test_laps_on_hand_and_how_to_get_a_file():
    entry = {"summary": {"driver": MATE_NAME, "lap_text": "1:38.874", "lap_time": 98.874}, "kept": True}
    board = [{"key": "me", "name": "Swazi Williams", "t": 98.5, "is_me": True},
             {"key": "b", "name": "Sam Lee", "t": 99.1, "is_me": False},
             {"key": "a", "name": "Jo Park", "t": 98.2, "is_me": False}, {"key": "z", "name": "Bad", "t": None}]
    L = tr.laps_on_hand(entry, board)
    assert [x["driver"] for x in L] == [MATE_NAME, "Jo Park", "Sam Lee"], L
    assert L[0]["kind"] == "telemetry" and L[0]["in_use"] and "Kept" in L[0]["note"]
    assert L[1]["kind"] == "garage61" and not L[1]["usable"] and "ask Jo for the telemetry file" in L[1]["note"]
    assert L[1]["lap_text"] == "1:38.200"
    assert tr.laps_on_hand(None, None) == []
    assert "Documents\\iRacing\\telemetry" in tr.HOW_TO_GET and "never a program" in tr.HOW_TO_GET
    for x in L:
        json.dumps(x)


def test_garage61_skip_note_says_what_to_do():
    n = team.NO_MATCH_NOTE
    assert n.startswith("Couldn't match this track or car to Garage 61, so it was skipped.")
    assert "Next step:" in n and "Refresh team data" in n
    src = (ROOT / "debrief" / "team.py").read_text(encoding="utf-8")
    assert 'entry["note"] = NO_MATCH_NOTE' in src


def test_option_label():
    assert CC.option_label({"track": "Zolder", "class": "GT4", "cars": ["McLaren"]}) == "Zolder, GT4 (1 car, nothing to compare)"
    assert CC.option_label({"track": "Zolder", "class": "GT4", "cars": ["A", "B"]}) == "Zolder, GT4 (2 cars)"
    assert "nothing to compare" in CC.option_label({"track": "Zolder", "class": "GT4", "cars": []})


def png(w=40, h=20) -> bytes:
    raw = b"".join(b"\x00" + bytes([(x * 7) % 256 for x in range(w * 3)]) for _ in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    body = chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw, 0))
    data = b"\x89PNG\r\n\x1a\n" + body + chunk(b"IEND", b"")
    return data + b"\x00" * max(0, 1100 - len(data)) if len(data) < 1100 else data


def test_share_file_name_and_never_overwrite():
    assert S.share_file_name("2026-11-03_Zolder GP_McLaren 570S_share.png") == "2026-11-03_zolder-gp_mclaren-570s_share.png"
    assert S.share_file_name("../../evil\\name") == "evil-name_share.png"
    assert S.share_file_name("") == "debrief_share.png"
    S.SHARE_DIR = str(TMP / "sharetest")
    try:
        a = S.save_share_png(b"one", "x")
        b = S.save_share_png(b"two", "x")
        assert a.name == "x_share.png" and b.name == "x_share-2.png", (a, b)
        assert a.read_bytes() == b"one" and b.read_bytes() == b"two"
    finally:
        S.SHARE_DIR = None


# ================================================================ the server
def settings():
    LIB.mkdir(parents=True, exist_ok=True)
    TEL.mkdir(parents=True, exist_ok=True)
    Path(os.environ["DEBRIEF_SETTINGS"]).write_text(
        'token = "g61-test-token-ABCDEFGHIJKLMNOP"\n'
        f"library_folder = '{LIB}'\napi_base = \"http://127.0.0.1:9/api/v1/\"\niracing_folder = '{IRACING}'\n",
        encoding="utf-8")


def srv():
    if "srv" not in ENV:
        settings()
        app.LOG_FILE = str(TMP / "debrief.log")
        S.SHARE_DIR = str(DOWN)
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
    rq = urllib.request.Request(s.url + rel, headers=h, data=data, method="POST" if data is not None else "GET")
    try:
        with ENV["opener"].open(rq, timeout=180) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        b = e.read()
        try:
            return e.code, json.loads(b)
        except ValueError:
            return e.code, b


def wait_job(timeout=240):
    end = time.time() + timeout
    while time.time() < end:
        _c, st = call("api/status")
        if st.get("state") != "running":
            return st
        time.sleep(0.2)
    raise AssertionError("the job didn't finish")


def ready():
    srv()
    files()
    if "read" not in ENV:
        code, r = call("api/setuplab/read", {})
        assert code == 200 and r["ok"], r
        st = wait_job()
        assert st["state"] == "done", st
        ENV["read"] = True


def q(**kw):
    return "?" + urllib.parse.urlencode(kw)


def review(car="McLaren 570S GT4", track="Circuit Zolder (Grand Prix)", name=REVIEW):
    d = LIB / "reviews" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "review.json").write_text(json.dumps({"meta": {"car": car, "track": track, "driver": "Swazi Williams"}}),
                                   encoding="utf-8")
    (d / "index.html").write_text("<!doctype html><title>Zolder</title><p>review</p>", encoding="utf-8")
    return d


def upload_mate():
    s = srv()
    data = files()["mate"].read_bytes()
    qs = urllib.parse.urlencode({"name": "alex.ibt", "car": CAR, "track": ZOL, "keep": "0"})
    rq = urllib.request.Request(s.url + "api/teamref/upload?" + qs, data=data, method="POST",
                                headers={"X-Debrief": "1", "Content-Type": "application/octet-stream"})
    with ENV["opener"].open(rq, timeout=180) as r:
        j = json.loads(r.read())
    assert j["ok"] and j["accepted"], j
    return j


def test_teamref_laps_and_cards_headline():
    ready()
    call("api/teamref/forget", {"car": CAR, "track": ZOL})
    code, j = call("api/teamref/laps" + q(car=CAR, track=ZOL))
    assert code == 200 and j["ok"] and j["laps"] == [] and "Documents\\iRacing\\telemetry" in j["how"], j
    # the team page's Garage 61 times for this car and track are listed too
    (LIB / "team").mkdir(parents=True, exist_ok=True)
    (LIB / "team" / "team.json").write_text(json.dumps({"combos": [
        {"track": "Circuit Zolder - Grand Prix", "car": "McLaren 570S GT4",
         "board": [{"key": "s", "name": "Sam Lee", "t": 98.1, "is_me": False}]},
        {"track": "Lime Rock Park", "car": "McLaren 570S GT4", "board": [{"key": "x", "name": "Nope", "t": 60.0}]}]}),
        encoding="utf-8")
    code, j = call("api/teamref/laps" + q(car=CAR, track=ZOL))
    assert [x["driver"] for x in j["laps"]] == ["Sam Lee"] and j["team_page"], j
    upload_mate()
    code, j = call("api/teamref/laps" + q(car=CAR, track=ZOL))
    assert [x["kind"] for x in j["laps"]] == ["telemetry", "garage61"] and j["laps"][0]["driver"] == MATE_NAME, j
    code, c = call("api/teamref/cards" + q(car=CAR, track=ZOL))
    assert code == 200 and c["ok"], c
    assert c["headline"] and ("Alex is quicker overall" in c["headline"] or "You are quicker overall" in c["headline"]
                              or c["headline"].startswith("Dead level")), c["headline"]
    assert isinstance(c["my_best"], (int, float))
    for card in c["cards"]:
        assert card["different"].startswith(("Alex ", "on Alex's lap")), card["different"]
    code, j = call("api/teamref/laps" + q(car=CAR, track=ZOL), header=False)
    assert code == 404, "the X-Debrief header is needed"


def test_review_finds_its_practice_session():
    ready()
    review()
    code, j = call("api/teamref/for-review" + q(dir=REVIEW))
    assert code == 200 and j["ok"] and j["session"] and j["session"].startswith(CAR + "|" + ZOL), j
    review(car="Ford Mustang GT4", name="2026-11-01_mustang")
    code, j = call("api/teamref/for-review" + q(dir="2026-11-01_mustang"))
    assert j["ok"] and j["session"] is None and "no telemetry of yours" in j["message"], j
    code, j = call("api/teamref/for-review" + q(dir="../etc"))
    assert code == 400 and not j["ok"], j


def test_share_save_endpoint():
    srv()
    data = png()
    code, j = call("api/share-save" + q(name="2026-11-03_zolder_mclaren_share.png"), raw=data, ctype="image/png")
    assert code == 200 and j["ok"], j
    p = Path(j["path"])
    assert p.parent == DOWN and p.name == "2026-11-03_zolder_mclaren_share.png" and p.read_bytes() == data, j
    code, j2 = call("api/share-save" + q(name="2026-11-03_zolder_mclaren_share.png"), raw=data, ctype="image/png")
    assert j2["ok"] and Path(j2["path"]).name == "2026-11-03_zolder_mclaren_share-2.png", "never over a file"
    assert p.read_bytes() == data
    code, j3 = call("api/share-save" + q(name="x"), raw=b"MZ" + b"\x00" * 2000, ctype="image/png")
    assert code == 400 and not j3["ok"], "not a PNG: refused"
    code, j4 = call("api/share-save" + q(name="x"), raw=data, ctype="application/octet-stream")
    assert code == 400 and not j4["ok"]
    code, _ = call("api/share-save", raw=data, ctype="image/png", header=False)
    assert code == 404
    assert sorted(x.name for x in DOWN.iterdir()) == ["2026-11-03_zolder_mclaren_share-2.png",
                                                     "2026-11-03_zolder_mclaren_share.png"]


# ================================================================ the window
def _browser():
    if "browser" in PW:
        return PW["browser"]
    PW["browser"] = None
    try:
        from playwright.sync_api import sync_playwright
        PW["pw"] = sync_playwright().start()
        PW["browser"] = PW["pw"].chromium.launch()
    except Exception as e:  # noqa: BLE001
        print("  (browser checks skipped:", str(e).splitlines()[0][:120], ")")
    return PW["browser"]


def _page():
    b = _browser()
    if not b:
        return None
    ctx = b.new_context(viewport={"width": 1093, "height": 614})
    pg = ctx.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.route("**/fonts.googleapis.com/**", lambda r: r.abort())
    pg.route("**/fonts.gstatic.com/**", lambda r: r.abort())
    pg.goto(srv().url)
    pg.wait_for_function("() => document.readyState === 'complete'")
    return pg, ctx, errs


def test_window_weekly_reason_cars_dropdown_and_session_jump():
    ready()
    r = _page()
    if not r:
        return
    pg, ctx, errs = r
    try:
        # Team > This week: no series picked: the reason is beside the button, with a link to Prepare
        pg.evaluate("() => document.querySelector('.navbtn[data-nav=team]').click()")
        pg.wait_for_function("() => !document.getElementById('weeklyWhy').hidden || !document.getElementById('fbWeeklyWhy').hidden",
                             timeout=20000)
        why = pg.evaluate("() => [document.getElementById('weeklyWhyText').textContent, document.getElementById('fbWeeklyWhyText').textContent]")
        assert any(w.startswith("No series picked yet, and none is saved. Pick one on Prepare") for w in why), why   # 2.21 wording
        assert pg.evaluate("() => document.getElementById('btnWeekly').disabled && document.getElementById('fbWeekly').disabled")
        vis = "weeklyWhyGo" if pg.evaluate("() => !document.getElementById('weeklyWhy').hidden") else "fbWeeklyWhyGo"
        assert pg.text_content("#" + vis) == "Open Prepare"
        pg.click("#" + vis)
        pg.wait_for_function("() => !document.getElementById('vPrep').hidden", timeout=10000)
        # Car vs car: only the McLaren's telemetry here (no races): a one-car class, said so, nothing to pick
        pg.evaluate("() => document.querySelector('.navbtn[data-nav=cars]').click()")
        pg.wait_for_function("() => document.getElementById('ccPick').options.length > 0", timeout=60000)
        o = pg.evaluate("() => [...document.getElementById('ccPick').options].map(x => [x.textContent, x.disabled])")
        assert o and all(("nothing to compare" in t or t in ("No races yet", "Nothing to compare yet")) for t, _d in o), o
        assert pg.evaluate("() => document.getElementById('ccPick').disabled"), "nothing to compare: disabled"
        # a race review: Compare with a teammate's lap opens the Practice session at its teammate panel
        review()
        pg.evaluate("() => document.querySelector('.navbtn[data-nav=practice]').click()")
        pg.wait_for_selector(".prrow", timeout=20000)
        pg.click(".prrow")
        pg.wait_for_selector("#prTeamJump", timeout=60000)
        assert pg.text_content("#prTeamJump") == "Compare with a teammate's lap"
        pg.wait_for_function("() => document.querySelector('#prTeam .teamhow')", timeout=60000)
        assert "Documents\\iRacing\\telemetry" in pg.text_content("#prTeam .teamhow")
        hl = pg.wait_for_selector("#prTeamHead", timeout=60000)
        assert "quicker overall" in hl.text_content() or "Dead level" in hl.text_content()
        t = pg.text_content("#prTeam .teamcards")
        assert "km/h" not in t, "teammate cards in mph"
        heads = pg.evaluate("() => [...document.querySelectorAll('#prTeam .cclost')].map(x => x.textContent)")
        assert all(x.startswith("Alex ") for x in heads if x), heads
        assert not errs, errs
    finally:
        ctx.close()


# ================================================================ run
if __name__ == "__main__":
    tests = [(k, v) for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    bad = 0
    for name, fn in tests:
        try:
            fn()
            print("ok  ", name)
        except Exception:  # noqa: BLE001
            bad += 1
            print("FAIL", name)
            traceback.print_exc()
    try:
        if PW.get("browser"):
            PW["browser"].close()
            PW["pw"].stop()
    except Exception:  # noqa: BLE001
        pass
    try:
        if "srv" in ENV:
            ENV["srv"].shutdown()
    except Exception:  # noqa: BLE001
        pass
    print(f"\n{len(tests) - bad} of {len(tests)} passed")
    sys.exit(1 if bad else 0)
