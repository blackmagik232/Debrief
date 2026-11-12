"""Debrief 2.34 "No token needed": race reviews from the telemetry files on this PC.

    python3 tests/ci_run.py tests/test_234_notoken.py

Stand-in .ibt files (tests/fake_ibt.py) in a temp telemetry folder; no network (DEBRIEF_NO_NETWORK) and a stand-in
Garage 61 where one is needed. Checks: finding races, the review built through the same engine and page, the result read
from iRacing's session data, a saved review opening at once, rebuild, Garage 61 taking over the same race without a
duplicate, the server routes without a token, Review last race not skipping a newer race on this PC, and the window.
"""
from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import threading
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
TMP = Path(tempfile.mkdtemp(prefix="debrief-234-"))
LIB, TEL = TMP / "lib", TMP / "telemetry"
LIB.mkdir()
TEL.mkdir()
os.environ.update(DEBRIEF_SETTINGS=str(TMP / "settings.toml"), DEBRIEF_CACHE_DIR=str(TMP / "cache"),
                  DEBRIEF_SCHEDULE_DIR=str(TMP / "schedule"), DEBRIEF_UPDATE_STATE=str(TMP / "update.json"),
                  DEBRIEF_NO_NETWORK="1")
(TMP / "settings.toml").write_text(f"library_folder = '{LIB.as_posix()}'\niracing_folder = '{TMP.as_posix()}'\n"
                                   "sync_on_open = false\n", encoding="utf-8")

import fake_ibt as F  # noqa: E402
from debrief import app, engine, library, localrace, results  # noqa: E402
from debrief.desktop import server as S  # noqa: E402

app.LOG_FILE = str(TMP / "debrief.log")
FAILS = []
SUB = 81234567
RACE = TEL / "mclaren570sgt4_zolder gp 2026-11-10 20-00-00.ibt"
PRAC = TEL / "mclaren570sgt4_zolder gp 2026-11-09 19-00-00.ibt"


def check(ok, what):
    print(("ok   " if ok else "FAIL ") + what)
    if not ok:
        FAILS.append(what)


def files():
    F.make(PRAC, [100.1, 99.8, 99.6, 99.9], session="Practice", start_date=1_794_250_000)
    F.make(RACE, [99.4, 98.8, 98.6, 98.9, 98.7, 99.0, 98.5, 98.8], session="Race", subsession=SUB,
           incidents=(2,), start_date=1_794_340_000)


def test_engine_columns():
    n = 400
    pct = np.linspace(0, 0.999, n)
    v = 40 + 10 * np.sin(pct * 6.28)
    cols = {"Speed": v, "LapDistPct": pct, "Brake": np.clip(np.cos(pct * 12), 0, 1), "Throttle": np.ones(n) * 0.7,
            "Gear": np.full(n, 4.0), "SteeringWheelAngle": np.sin(pct * 9) * 0.3}
    csv = "Speed,LapDistPct,Brake,Throttle,Gear,SteeringWheelAngle\n" + "\n".join(
        ",".join(f"{cols[k][i]:.6f}" for k in ("Speed", "LapDistPct", "Brake", "Throttle", "Gear", "SteeringWheelAngle"))
        for i in range(n))
    a, b = engine.parse_csv(csv), engine.raw_from_columns(cols, n)
    same = all(np.allclose(a[k], b[k], atol=1e-5, equal_nan=True) for k in ("v", "p", "b", "th", "gear", "st", "lat"))
    check(same and a["_missing"] == b["_missing"], "an .ibt lap and a Garage 61 CSV of the same lap read the same")
    try:
        engine.raw_from_columns({"LapDistPct": pct})
        check(False, "no Speed channel is refused")
    except ValueError:
        check(True, "no Speed channel is refused")


def test_races_and_review():
    rs = localrace.races()
    check(len(rs) == 1 and rs[0]["subsession"] == SUB, f"one race found, the practice left out ({len(rs)})")
    check(localrace.event_id(rs[0]) == f"ir{SUB}", "the race's id is ir + its iRacing session number")
    cfg = app.load_settings()
    info = {}
    page = app.cmd_review_local(cfg, open_after=False, info=info)
    d = json.loads((page.parent / "review.json").read_text(encoding="utf-8"))
    m = d["meta"]
    check(info.get("local") and page.is_file() and page.parent.name.endswith(str(SUB)[-6:]),
          f"review page written in a folder named for the race ({page.parent.name})")
    check(m["source"] == "pc" and m["subsession"] == SUB and m["session"] == "Race" and m["requests"] == 0,
          "meta says it came from this PC, which race, and that nothing was downloaded")
    check(len(d["laps"]) == 8 and abs(d["best"]["t"] - 98.5) < 0.01, f"8 laps, best 1:38.500 ({d['best']})")
    check(any(not x.get("clean") for x in d["laps"]), "the lap with an incident is marked not clean")
    rows = library.list_reviews(LIB)
    check(rows and rows[0]["source"] == "pc", "the library row says where the review came from")
    info2 = {}
    app.cmd_review_local(cfg, open_after=False, info=info2)
    check(info2.get("saved") is True, "the same race again opens the saved review at once")
    info3 = {}
    app.cmd_review_local(cfg, open_after=False, rebuild=True, info=info3)
    check(isinstance(info3.get("changes"), list), "Rebuild makes it again from the files")
    return page.parent


def test_result_from_session_info():
    drivers = [{"CarIdx": 0, "UserName": "A", "IRating": 2000, "CarClassID": 1},
               {"CarIdx": 1, "UserName": "B", "IRating": 1500, "CarClassID": 1},
               {"CarIdx": 3, "UserName": "Me", "IRating": 1700, "CarClassID": 1},
               {"CarIdx": 9, "UserName": "Pace", "CarIsPaceCar": 1, "CarClassID": 11}]
    si = {"DriverInfo": {"DriverCarIdx": 3, "Drivers": drivers},
          "SessionInfo": {"Sessions": [{"SessionNum": 2, "SessionType": "Race", "ResultsPositions": [
              {"CarIdx": 0, "Position": 1, "ClassPosition": 0}, {"CarIdx": 3, "Position": 2, "ClassPosition": 1,
                                                                   "Incidents": 4, "LapsLed": 3, "ReasonOutStr": "Running"}]}]},
          "QualifyResultsInfo": {"Results": [{"CarIdx": 3, "Position": 0}, {"CarIdx": 0, "Position": 1}]}}
    r = localrace.result_from({"session_info": si, "race_num": 2})
    k = 1600 / math.log(2)
    sof = int(round(k * math.log(3 / sum(math.exp(-x / k) for x in (2000, 1500, 1700)))))
    check(r.get("finish_pos") == 2 and r.get("start_pos") == 1 and r.get("field_size") == 3,
          f"finish P2, started P1 (iRacing counts the grid from 0), 3 cars without the pace car ({r})")
    check(r.get("incidents") == 4 and r.get("laps_led") == 3 and r.get("finish_status") == "running"
          and r.get("irating_before") == 1700 and r.get("sof") == sof, "incidents, laps led, status, iRating and SOF")
    # multi-class: class position, class field
    drivers2 = drivers[:3] + [{"CarIdx": 5, "UserName": "GT3", "IRating": 3000, "CarClassID": 2}]
    si2 = dict(si, DriverInfo={"DriverCarIdx": 3, "Drivers": drivers2})
    si2["SessionInfo"] = {"Sessions": [{"SessionNum": 2, "SessionType": "Race", "ResultsPositions": [
        {"CarIdx": 5, "Position": 1, "ClassPosition": 0}, {"CarIdx": 3, "Position": 3, "ClassPosition": 1}]}]}
    r2 = localrace.result_from({"session_info": si2, "race_num": 2})
    check(r2.get("finish_pos") == 2 and r2.get("field_size") == 3, f"multi-class: P2 in class of 3 ({r2})")
    r3 = localrace.result_from({"session_info": si, "race_num": 2}, {"race": {"finish": {"pos": 5, "cpos": 5, "incidents": 6}}})
    check(r3["finish_pos"] == 5 and r3["incidents"] == 6, "the recording's finish (it saw the session end) wins")
    r4 = localrace.result_from({"session_info": si, "race_num": 2}, {"race": {"finish": {"pos": 7, "from": "last lap row"}}})
    check(r4["finish_pos"] == 2, "a recording that only saw the last lap row doesn't override the official order")
    check(localrace.result_from({"session_info": {}, "race_num": 0}).get("finish_pos") is None, "nothing known: no finish")


def test_garage61_takes_over(local_dir: Path):
    cfg = app.load_settings()
    m = json.loads((local_dir / "review.json").read_text(encoding="utf-8"))["meta"]
    results.save(LIB, local_dir.name, {"finish_pos": 4, "field_size": 20})
    new = LIB / "reviews" / (local_dir.name[:-6] + "abc123")
    start = app.lap_start_dt({"start_time": m["started_at"]})
    took = app.retire_local_twin(LIB, new, m["track"], m["car"], start)
    check(took == local_dir.name and not local_dir.exists(), "the Garage 61 review replaces the PC review of the race")
    check((LIB / "reviews" / ".backup" / "replaced" / local_dir.name / "review.json").is_file(), "a copy is kept in .backup")
    r = results.load(new)
    check(r and r.get("finish_pos") == 4, "the result is carried over to the Garage 61 review")
    check(app.retire_local_twin(LIB, new, m["track"], m["car"], start) is None, "nothing left to replace the second time")
    # bring the PC review back for the next checks
    app.cmd_review_local(cfg, open_after=False, rebuild=True)


class StubApi:
    def __init__(self, has):
        self.has = has
        self.asked = []

    def event_for_subsession(self, sub):
        self.asked.append(sub)
        return "G61EVENT" if self.has else None


def test_token_but_garage61_missing():
    cfg = app.load_settings()
    for d in (LIB / "reviews").iterdir():
        if d.is_dir() and not d.name.startswith("."):
            import shutil
            shutil.rmtree(d)
    api = StubApi(False)
    s = app._newest_local_unseen(cfg, api, lambda *a: None)
    check(s is not None and api.asked == [SUB], "Garage 61 lacks the newest race: it's reviewed from the PC")
    check(app._newest_local_unseen(cfg, StubApi(True), lambda *a: None) is None, "Garage 61 has it: the usual review")
    app.cmd_review_local(cfg, open_after=False)


def test_server():
    s = S.DebriefServer(heartbeat_timeout=3600)
    threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
    import urllib.request
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(rel, body=None):
        h = {"X-Debrief": "1"}
        data = None if body is None else json.dumps(body).encode()
        if data:
            h["Content-Type"] = "application/json"
        rq = urllib.request.Request(s.url + rel, headers=h, data=data, method="POST" if data is not None else "GET")
        try:
            with op.open(rq, timeout=60) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")
    import urllib.error
    d = next(x for x in (LIB / "reviews").iterdir() if x.is_dir() and not x.name.startswith("."))
    check(S.review_rebuild_params(d) == {"local": True, "subsession": SUB, "rebuild": True}, "rebuild of a PC review stays local")
    check(S.review_upgrade_params(d) == {"upgrade": True, "subsession": SUB}, "Add comparison laps finds the race by its session")
    code, j = call("api/review", {"review": d.name, "upgrade": True})
    check(code == 400 and j.get("setup"), "Add comparison laps without a token asks for Garage 61")
    code, j = call("api/review", {})
    check(code == 200 and j.get("ok"), f"Review last race without a token starts ({j})")
    import time
    end = time.time() + 60
    st = {}
    while time.time() < end:
        st = call("api/status")[1] if call("api/status")[0] == 200 else {}
        if (st.get("job") or st).get("state") in ("done", "error"):
            break
        time.sleep(0.3)
    job = st.get("job") or st
    res = job.get("result") or {}
    check(job.get("state") == "done" and res.get("local") and res.get("saved"), f"it opens the saved PC review ({res})")
    # Review last race doesn't open an older review when a newer race on this PC has none
    code, j = call("api/last-race")
    check(j.get("instant") is True and j.get("local_newer") is False, "the newest race is reviewed: instant")
    F.make(TEL / "mclaren570sgt4_zolder gp 2026-11-12 20-00-00.ibt", [98.9, 98.6, 98.4, 98.7], session="Race",
           subsession=SUB + 5, start_date=1_794_520_000)
    code, j = call("api/last-race")
    check(j.get("instant") is False and j.get("local_newer") is True, "a newer race on this PC: not the old review")
    s.shutdown()


def test_window():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  SKIPPED: Playwright not installed")
        return
    import shutil
    shutil.rmtree(LIB / "reviews", ignore_errors=True)
    library.rebuild(LIB)
    s = S.DebriefServer(heartbeat_timeout=3600)
    threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception:  # noqa: BLE001
            print("  SKIPPED: Chromium not available")
            return
        try:
            pg = b.new_page(viewport={"width": 1400, "height": 900})
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.on("console", lambda msg: errs.append(msg.text) if msg.type == "error" else None)
            pg.goto(s.url)
            pg.wait_for_selector("#gLocal:not([hidden])", timeout=15000)
            check(pg.get_attribute("#btnReview", "aria-disabled") == "false", "no token, telemetry found: Review last race works")
            check("Garage 61 is optional" in pg.text_content("#welcomeText"), "the welcome says Garage 61 is optional")
            pg.click("#gLocal button")
            pg.wait_for_function("() => !document.getElementById('vFrame').hidden", timeout=60000)
            pg.wait_for_function("() => /from your telemetry files/.test(document.getElementById('bannerText').textContent)", timeout=10000)
            fr = pg.frame_locator("#frame")
            fr.locator("body").wait_for(timeout=10000)
            pg.wait_for_timeout(1500)
            body = fr.locator("body").inner_text()
            check("Garage 61 flagged" not in body and "comes from your PC" in body, "the review page speaks of your PC, not Garage 61")
            check(not errs, f"no script errors {errs[:2]}")
            pg.close()
        finally:
            b.close()
            s.shutdown()


if __name__ == "__main__":
    files()
    test_engine_columns()
    d = test_races_and_review()
    test_result_from_session_info()
    test_garage61_takes_over(d)
    test_token_but_garage61_missing()
    test_server()
    test_window()
    print("all passed" if not FAILS else f"{len(FAILS)} failed")
    sys.exit(1 if FAILS else 0)
