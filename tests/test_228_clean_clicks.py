"""Debrief 2.28 "Clean clicks": the window is never blocked, every long job can be cancelled, a missing network fails in
seconds, the last review opens at once, Today offers the week's race, the driving view has a way out, the review page is
tidier (nothing removed) and Settings saves switches by themselves.

    python3 tests/test_228_clean_clicks.py

Two kinds of checks:
  * the real server (port 0, the session secret, the X-Debrief header) with stand-in jobs, a stand-in Garage 61 that
    drops or never answers, and a small library: the job layer, the offline timing, api/last-race, Today's week card;
  * the real index.html / app.js / app.css in Chromium with every /api/ call answered here (no server): the window.
The browser checks are skipped when Playwright or its Chromium isn't installed. No network, temp folders from tempfile,
utf-8 everywhere.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
UI = Path(os.environ.get("DEBRIEF_UI_DIR") or ROOT / "debrief" / "desktop" / "ui")   # DEBRIEF_UI_DIR: measure an older window

TMP = Path(tempfile.mkdtemp(prefix="debrief-228-"))
LIB = TMP / "lib"
os.environ.update(DEBRIEF_SETTINGS=str(TMP / "settings.toml"), DEBRIEF_CACHE_DIR=str(TMP / "cache"),
                  DEBRIEF_SCHEDULE_DIR=str(TMP / "schedule"), DEBRIEF_UPDATE_STATE=str(TMP / "update.json"))

import make_fixture_library as MFL  # noqa: E402
from debrief import app  # noqa: E402
from debrief.desktop import server as S  # noqa: E402

ENV, PW = {}, {}
BASE = "http://debrief.test/s3cret/"


# ================================================================ the real server
def write_settings(api_base="http://127.0.0.1:9/api/v1/", **extra):
    LIB.mkdir(parents=True, exist_ok=True)
    lines = ['token = "g61-test-token-ABCDEFGHIJKLMNOP"', f"library_folder = '{LIB}'", f'api_base = "{api_base}"']
    lines += [f"{k} = {v}" for k, v in extra.items()]
    Path(os.environ["DEBRIEF_SETTINGS"]).write_text("\n".join(lines) + "\n", encoding="utf-8")


def srv():
    if "srv" not in ENV:
        write_settings()
        MFL.main(str(LIB))
        app.LOG_FILE = str(TMP / "debrief.log")
        s = S.DebriefServer(heartbeat_timeout=3600)
        threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
        ENV["srv"] = s
        ENV["opener"] = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return ENV["srv"]


def call(rel, body=None, timeout=60):
    s = srv()
    h = {"X-Debrief": "1"}
    data = None if body is None else json.dumps(body).encode("utf-8")
    if data is not None:
        h["Content-Type"] = "application/json"
    rq = urllib.request.Request(s.url + rel, headers=h, data=data, method="POST" if data is not None else "GET")
    try:
        with ENV["opener"].open(rq, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw


def status():
    return call("api/status")[1]


def wait_state(pred, timeout=20.0):
    end = time.time() + timeout
    st = {}
    while time.time() < end:
        st = status()
        if pred(st):
            return st
        time.sleep(0.05)
    raise AssertionError(f"the job never reached the wanted state: {st}")


def idle():
    """Wait until no job runs (a test that leaves one behind must not hurt the next)."""
    wait_state(lambda st: st.get("state") != "running", 30)


class SlowJob:
    """A stand-in for cmd_review / cmd_prep / cmd_team / cmd_weekly: reports a step every 50 ms until the job is
    cancelled (the progress report raises) or `seconds` have passed."""

    def __init__(self, seconds=30.0):
        self.seconds, self.steps, self.started = seconds, 0, threading.Event()

    def __call__(self, cfg, *a, progress=None, **k):
        end = time.time() + self.seconds
        while time.time() < end:
            self.steps += 1
            self.started.set()
            if progress:
                progress(min(0.9, 0.02 + self.steps / 400), f"Step {self.steps}")
            time.sleep(0.05)
        raise SystemExit("The stand-in job ran out of time.")


def with_slow(names, seconds=30.0):
    """Swap app.cmd_<name> for a SlowJob; returns (jobs, restore)."""
    old = {n: getattr(app, "cmd_" + n) for n in names}
    jobs = {n: SlowJob(seconds) for n in names}
    for n in names:
        setattr(app, "cmd_" + n, jobs[n])

    def restore():
        for n, f in old.items():
            setattr(app, "cmd_" + n, f)
    return jobs, restore


# ---------------------------------------------------------------- 2: cancel and quick failure
def test_cancel_stops_review_prep_team_and_weekly():
    srv()
    jobs, restore = with_slow(["review", "prep", "team", "weekly"])
    try:
        cases = [("review", "api/review", {}), ("prep", None, {"track_id": 1, "car_id": 2}), ("team", None, {}), ("weekly", None, {})]
        for kind, route, params in cases:
            if route:
                code, r = call(route, params)
                assert code == 200 and r["ok"], r
            else:
                r = ENV["srv"].jobs.start(kind, params)
                assert r["ok"], r
            assert jobs[kind].started.wait(5), kind
            t0 = time.time()
            code, r = call("api/job-stop", {})
            assert code == 200 and r == {"ok": True, "stopping": True}, (kind, r)
            st = wait_state(lambda s: s.get("state") != "running", 5)
            took = time.time() - t0
            assert st["state"] == "error" and st["cancelled"] is True and st["kind"] == kind, st
            assert st["error"] == S.CANCELLED_MSG and "stand-in" not in st["error"], st
            assert took < 2.0, f"{kind} took {took:.1f}s to stop"
            # a stopped job is gone: the next one starts at once
            assert call("api/job-stop", {})[1] == {"ok": True, "stopping": False}
    finally:
        restore()
        idle()


def test_a_cancel_for_one_job_never_stops_the_next():
    srv()
    jobs, restore = with_slow(["review"])
    try:
        call("api/review", {})
        assert jobs["review"].started.wait(5)
        call("api/job-stop", {})
        wait_state(lambda s: s.get("state") != "running", 5)
        jobs["review"].started.clear()
        call("api/review", {})
        assert jobs["review"].started.wait(5)
        time.sleep(0.3)
        assert status()["state"] == "running", "the earlier Cancel carried over to the next job"
        call("api/job-stop", {})
        wait_state(lambda s: s.get("state") != "running", 5)
    finally:
        restore()
        idle()


def test_sync_is_not_cancelled_by_job_stop():
    """Cancel is for review, prep, team and weekly jobs; a sync has its own Pause and job-stop leaves it alone."""
    s = srv()
    assert S.CANCELLABLE == ("review", "prep", "team", "teamload", "weekly")
    assert s.jobs.cancel() is False
    assert "sync" not in S.CANCELLABLE and "check" not in S.CANCELLABLE


def _drop_server(mode):
    """A stand-in Garage 61 that closes every connection at once ("drop") or accepts and never answers ("hole")."""
    ls = socket.socket()
    ls.bind(("127.0.0.1", 0))
    ls.listen(50)
    held = []

    def run():
        while True:
            try:
                c, _ = ls.accept()
            except OSError:
                return
            if mode == "drop":
                c.close()
            else:
                held.append(c)
    threading.Thread(target=run, daemon=True).start()
    return ls, held


def test_slow_first_answer_is_not_called_offline():
    """A Garage 61 that accepts but is slow to answer (busy, a heavy first /laps call) is up, not offline: the
    quick first timeout must not turn that into "can't reach Garage 61". Once it really goes away, the review
    still ends with the offline message."""
    ls, held = _drop_server("hole")
    port = ls.getsockname()[1]
    write_settings(api_base=f"http://127.0.0.1:{port}/api/v1/")
    os.environ["DEBRIEF_CACHE_DIR"] = str(TMP / "cache-offline-slow")
    try:
        srv()
        code, r = call("api/review", {})
        assert code == 200 and r["ok"], r
        time.sleep(9)   # past the 6 s quick read timeout
        st = wait_state(lambda x: True, 5)
        assert st.get("state") == "running", f"a slow (not offline) Garage 61 was given up on: {st}"
        ls.close()
        for c in held:
            c.close()
        st = wait_state(lambda x: x.get("state") != "running", 60)
        assert st["state"] == "error" and "can't reach Garage 61" in st["error"], st
    finally:
        try:
            ls.close()
        except OSError:
            pass
        for c in held:
            c.close()
        write_settings()
        os.environ["DEBRIEF_CACHE_DIR"] = str(TMP / "cache")
    idle()


def test_offline_review_fails_fast_with_open_saved_reviews():
    for mode, limit in (("drop", 12.0),):
        ls, held = _drop_server(mode)
        port = ls.getsockname()[1]
        write_settings(api_base=f"http://127.0.0.1:{port}/api/v1/")
        cache = TMP / f"cache-offline-{mode}"
        os.environ["DEBRIEF_CACHE_DIR"] = str(cache)
        try:
            s = srv()
            t0 = time.time()
            code, r = call("api/review", {})
            assert code == 200 and r["ok"], r
            st = wait_state(lambda x: x.get("state") != "running", 40)
            took = time.time() - t0
            assert st["state"] == "error" and not st.get("cancelled"), st
            assert "can't reach Garage 61" in st["error"], st
            assert took <= limit, f"offline review ({mode}) took {took:.1f}s, limit {limit}s"
            ENV.setdefault("offline", {})[mode] = round(took, 1)
            print(f"  offline review failed in {took:.1f} s ({mode})")
        finally:
            ls.close()
            for c in held:
                c.close()
            write_settings()
            os.environ["DEBRIEF_CACHE_DIR"] = str(TMP / "cache")
    idle()


def test_the_quick_first_timeout_is_only_for_the_app_jobs():
    from debrief.g61 import Garage61
    assert Garage61.FIRST_TIMEOUT == (4, 6)
    api = app.make_api({"token": "t-1234567890", "cache_dir": str(TMP / "c2"), "api_base": "http://127.0.0.1:9/api/v1/"})
    assert api.first_pending is True
    # a plain Garage61 (sync, tests) keeps its normal waits
    assert Garage61("t-1234567890", TMP / "c3", base_url="http://127.0.0.1:9/api/v1/").first_pending is False


# ---------------------------------------------------------------- 3: instant last review
def test_last_race_instant_when_the_saved_review_is_the_newest():
    srv()
    code, j = call("api/last-race")
    assert code == 200 and j["ok"], j
    assert j["saved"]["dir"].endswith("_fx1008") and j["instant"] is True, j
    assert j["saved"]["event"] == "FIXTURE20261008"


def _put_store(sessions):
    cfg = app.load_settings()
    api = app.make_api(cfg)
    from debrief import sync
    root = Path(api.cache) / "sync"
    root.mkdir(parents=True, exist_ok=True)
    (root / "sessions.json").write_text(json.dumps({"schema": sync.SCHEMA, "sessions": sessions}), encoding="utf-8")


def test_last_race_not_instant_when_a_newer_race_was_synced():
    srv()
    _put_store([{"key": "ev|3|2026-10-20T19:00:00Z", "event": "NEWER1", "track_id": 380, "car_id": 152, "type": 3,
                 "start": "2026-10-20T19:00:00Z", "laps": 20},
                {"key": "ev|3|2026-10-08T19:05:00Z", "event": "FIXTURE20261008", "track_id": 380, "car_id": 152, "type": 3,
                 "start": "2026-10-08T19:05:00Z", "laps": 20}])
    j = call("api/last-race")[1]
    assert j["instant"] is False and j["newest_synced"]["event"] == "NEWER1", j
    # the saved review is the same race as the newest synced one: instant again
    _put_store([{"key": "ev|3|2026-10-08T19:05:00Z", "event": "FIXTURE20261008", "track_id": 380, "car_id": 152, "type": 3,
                 "start": "2026-10-08T19:05:00Z", "laps": 20}])
    assert call("api/last-race")[1]["instant"] is True
    _put_store([])


def test_last_race_check_finds_a_newer_race_and_says_nothing_when_offline():
    s = srv()
    real = app.find_last_event
    # real folders end with the last six characters of the event id; the fixture's end "_fx1008", so give one that rule
    src = next((LIB / "reviews").glob("*_fx1008"))
    twin = src.with_name(src.name[:-6] + "261008")
    shutil.copytree(src, twin)
    try:
        app.find_last_event = lambda api, sessions, cache_dir=None, library_folder=None: ("NEWEREVT", 380, 152)
        j = call("api/last-race?check=1")[1]
        assert j["checked"] is True and j["newer"] == {"event": "NEWEREVT", "track_id": 380, "car_id": 152, "session_type": 3}, j
        app.find_last_event = lambda api, sessions, cache_dir=None, library_folder=None: ("FIXTURE20261008", 380, 152)
        j = call("api/last-race?check=1")[1]
        assert j["checked"] is True and j["newer"] is None, j     # that race is already reviewed
        from debrief.g61 import Garage61Error
        def boom(*a, **k):
            raise Garage61Error("Debrief can't reach Garage 61. Check your internet connection, then try again.")
        app.find_last_event = boom
        j = call("api/last-race?check=1")[1]
        assert j["ok"] and j["newer"] is None and j["checked"] is False and "can't reach" in j["error"], j
    finally:
        app.find_last_event = real
        shutil.rmtree(twin, ignore_errors=True)
    assert s


# ---------------------------------------------------------------- 4: this week's race
def test_week_race_states():
    from debrief.desktop import home
    cfg = {"library_folder": str(TMP / "wk")}
    Path(cfg["library_folder"]).mkdir(parents=True, exist_ok=True)
    from debrief import schedule as _sc
    _real0 = _sc.load_current
    _sc.load_current = lambda *a, **k: None
    try:
        assert home.week_race(cfg) == {"state": "pick", "why": "no_schedule"}
    finally:
        _sc.load_current = _real0
    # a loaded schedule with one series, nothing starred and nothing prepared yet: pick a series
    from debrief import schedule, series
    from datetime import date, timedelta
    mon = date.today() - timedelta(days=date.today().weekday())
    sched = {"season": "2026 Season 4", "series": [{"id": "gt4", "name": "GT4 Fixed", "category": "Road", "cars": ["McLaren 570S GT4"],
                                                      "weeks": [{"week": 1, "season_week": 1, "start": mon.isoformat(), "track": "Zolder Grand Prix"}]}],
             "weeks": [{"week": 1, "start": mon.isoformat()}]}
    real = schedule.load_current
    schedule.load_current = lambda *a, **k: sched
    try:
        assert home.week_race(cfg) == {"state": "pick", "why": "nothing_chosen"}
        series.save_choice(cfg["library_folder"], {"series": "gt4", "car": "McLaren 570S GT4", "week": 1})
        w = home.week_race(cfg)
        assert w["state"] == "ready" and w["series"] == {"id": "gt4", "name": "GT4 Fixed"} and w["track"] == "Zolder Grand Prix" \
            and w["car"] == "McLaren 570S GT4" and w["week"] == 1 and w["how"] == "last", w
        # a track picked with no series (the last thing prepared) is offered as it is
        series.save_choice(cfg["library_folder"], {"track": "Spa-Francorchamps", "car": "Porsche 911 GT3 R"})
        w = home.week_race(cfg)
        assert w["state"] == "ready" and w["series"] is None and w["track"] == "Spa-Francorchamps", w
    finally:
        schedule.load_current = real


# ================================================================ the window (browser, mocked API)
def browser():
    if "b" in PW:
        return PW["b"]
    try:
        from playwright.sync_api import sync_playwright
        PW["pw"] = sync_playwright().start()
        PW["b"] = PW["pw"].chromium.launch()
    except Exception:  # noqa: BLE001
        print("  SKIPPED: Playwright/Chromium not available")
        PW["b"] = None
    return PW["b"]


GROUPS = [{"track": "Zolder", "car": "McLaren 570S GT4", "reviews": [
    {"dir": "2026-11-01_zolder_gt4_aa11", "date": "2026-11-01", "started_at": "2026-11-01T19:00:00Z", "best": "1:35.000",
     "session": "Race", "laps": 12},
    {"dir": "2026-10-20_zolder_gt4_bb22", "date": "2026-10-20", "started_at": "2026-10-20T19:00:00Z", "best": "1:35.400",
     "session": "Race", "laps": 12}]}]
STEPS = [{"id": "token", "done": True, "title": "Connect Garage 61", "text": "Saved.", "action": None},
         {"id": "folder", "done": True, "title": "Find your iRacing telemetry", "text": "Found.", "action": None},
         {"id": "telemetry", "done": True, "title": "Read your telemetry", "text": "Read.", "action": None},
         {"id": "session", "done": True, "title": "Drive 5 clean laps in one session", "text": "Yes.", "action": None}]
FIRSTRUN = {"ok": True, "steps": STEPS, "done": 4, "total": 4, "next": None, "all_done": True, "dismissed": False, "show": False}
ONE = {"source": "plan", "title": "Turn 6: carry about 101 km/h", "sureness": "sure", "where": "Zolder · McLaren 570S GT4",
       "lines": [["The check", "Slowest speed in Turn 6 99 km/h or more"]], "why": "x",
       "action": {"label": "Coach this", "go": "coach"}}
WEEK = {"state": "ready", "how": "starred", "series": {"id": "gt4", "name": "GT4 Fixed"}, "track": "Zolder Grand Prix",
        "short": "Zolder", "car": "McLaren 570S GT4", "week": 7, "start": "2026-11-09"}
TODAY = {"ok": True, "one": ONE, "last_race": {"dir": "2026-11-01_zolder_gt4_aa11", "track": "Zolder", "car": "McLaren 570S GT4",
                                                "date": "2026-11-01", "session": "Race", "best": "1:35.000", "verdict": "Your laps are steady.", "fix": None},
         "last_practice": None, "reviews": 2, "sessions": 0, "setup": FIRSTRUN, "setup_done": True, "has_data": True, "week_race": WEEK}


def make_api(**over):
    a = {"settings": {"ok": True, "token_saved": True, "token_hint": "…abcd", "version": "2.28.0", "installed": True, "first_name": "Sam",
                      "data_folder": "C:/x", "max_reference_laps": 2, "units": "mph", "sync_on_open": True, "check_updates": True,
                      "record_sessions": True, "start_with_windows": True, "include_practice": False, "sync_telemetry": "best",
                      "salutation": "", "iracing_folder": "", "racecap_library": "", "team_cards_folder": "", "share_my_cards": False},
         "brand": {"app_name": "Debrief"}, "reviews": {"ok": True, "folder": "C:/lib", "groups": GROUPS},
         "status": {"state": "idle"}, "sync": {"ok": True, "skipped": True}, "update": {"ok": True, "skipped": True},
         "pages": {"ok": True, "prep": []}, "firstrun": FIRSTRUN, "today": TODAY,
         "last-race": {"ok": True, "saved": None, "instant": False, "newest_synced": None},
         "sessions": {"ok": True, "token": True, "summary": {"synced": True, "sessions": 3, "laps": 30, "combos": 1, "last_sync": "2026-11-08T10:00:00Z"},
                      "sessions": [{"key": f"k{i}", "event": f"E{i}", "type": t, "session": s, "track": "Zolder", "car": "McLaren 570S GT4",
                                    "start": f"2026-11-0{i}T19:00:00Z", "laps": 12, "best": 95.0, "best_clean": 95.2, "track_id": 1, "car_id": 2}
                                   for i, (t, s) in enumerate([(3, "Race"), (2, "Qualifying"), (1, "Practice")], 1)], "total": 3}}
    a.update(over)
    return a


def window(api, viewport=(1280, 860), hold=(), init_script=None):
    """The real window with every /api/ call answered from `api` (a value, or a function (method, qs, body) -> value).
    hold: api names whose answer is kept back until the test calls rel.release(name). Returns (ctx, page, errs, calls, ctl)."""
    b = browser()
    if not b:
        return None
    ctx = b.new_context(viewport={"width": viewport[0], "height": viewport[1]})
    if init_script:
        ctx.add_init_script(init_script)
    pg = ctx.new_page()
    errs, calls, held = [], [], {}
    pg.on("pageerror", lambda e: errs.append(str(e)))

    def answer(name, req, route):
        v = api.get(name, {"ok": True})
        if callable(v):
            body = None
            try:
                body = json.loads(req.post_data) if req.post_data else None
            except ValueError:
                body = None
            v = v(req.method, req.url.partition("?")[2], body)
        route.fulfill(body=json.dumps(v), content_type="application/json")

    def handle(route):
        req = route.request
        rel = req.url[len(BASE):]
        path, _, qs = rel.partition("?")
        if path in ("", "index.html"):
            return route.fulfill(body=(UI / "index.html").read_text(encoding="utf-8"), content_type="text/html")
        if path == "static/tokens.css":
            return route.fulfill(body=S.design_tokens(), content_type="text/css")
        if path in ("static/app.js", "static/app.css"):
            return route.fulfill(body=(UI / path[7:]).read_text(encoding="utf-8"),
                                 content_type="text/javascript" if path.endswith(".js") else "text/css")
        if path.startswith("lib/") or path.startswith("sample/"):
            return route.fulfill(body="<!doctype html><title>Page</title><body>page</body>", content_type="text/html")
        if path.startswith("api/"):
            name = path[4:]
            calls.append((req.method, name, qs, req.post_data))
            if name in hold:
                held[name] = (req, route)
                return None
            return answer(name, req, route)
        return route.fulfill(status=404, body="")
    pg.route("http://debrief.test/**", handle)
    pg.route("**/fonts.g*/**", lambda r: r.abort())

    class Ctl:
        @staticmethod
        def release(name):
            req, route = held.pop(name)
            answer(name, req, route)
    pg.goto(BASE)
    return ctx, pg, errs, calls, Ctl


def visible_views(pg):
    return pg.evaluate("() => [...document.querySelectorAll('main > section')].filter(s => !s.hidden).map(s => s.id)")


def open_page(pg, nav):
    pg.click(f".navbtn[data-nav={nav}]")


def banner_text(pg):
    return pg.evaluate("() => { const b = document.getElementById('banner'); return b.hidden ? '' : b.textContent }")


RUNNING = {"state": "running", "kind": "review", "id": 5, "title": "Reviewing your last race", "frac": 0.3, "message": "Loading laps...",
           "steps": ["Connecting to Garage 61..."], "started": time.time() - 12}


def settle(pg, ms=400):
    pg.wait_for_timeout(ms)


# ---------------------------------------------------------------- 1: never blocked
def test_while_a_job_runs_nothing_that_only_reads_or_saves_is_blocked():
    saved = []

    def settings_post(method, qs, body):
        if method == "POST":
            saved.append(body)
            return {"ok": True, "settings": dict(make_api()["settings"], **{k: v for k, v in (body or {}).items() if k in ("units", "sync_on_open", "include_practice", "max_reference_laps")})}
        return make_api()["settings"]
    r = window(make_api(status=dict(RUNNING), settings=settings_post, sync={"ok": False, "error": "Debrief is busy"}))
    if not r:
        return
    ctx, pg, errs, calls, ctl = r
    try:
        pg.wait_for_function("() => document.getElementById('vJob') && !document.getElementById('vJob').hidden")
        # browsing: every page opens, the filters filter, nothing shows the old "Debrief is busy" refusal
        open_page(pg, "sessions")
        pg.wait_for_selector("#sessList .item")
        assert pg.locator("#sessList .item").count() == 3
        for f, n in (("3", 1), ("2", 1), ("1", 1), ("all", 3)):
            pg.click(f"#sessFilter [data-f='{f}']")
            assert pg.locator("#sessList .item").count() == n, f
        open_page(pg, "today")
        pg.wait_for_selector("#tdTitle:has-text('Turn 6')")
        open_page(pg, "settings")
        # settings: a switch saves at once while the review runs
        pg.select_option("#unitsSel", "kmh")
        pg.wait_for_function("() => /Saved/.test(document.getElementById('unitsMsg').textContent)")
        assert {"units": "kmh"} in saved, saved
        pg.uncheck("#syncOpen")
        pg.wait_for_function("() => /Saved/.test(document.getElementById('syncOptMsg').textContent)")
        assert {"sync_on_open": False, "sync_telemetry": "best"} in saved, saved
        # opening a saved review is not blocked either
        pg.click(".rev[data-dir='2026-10-20_zolder_gt4_bb22']")
        pg.wait_for_function("() => !document.getElementById('vFrame').hidden")
        assert "busy" not in banner_text(pg).lower(), banner_text(pg)
        assert not errs, errs
    finally:
        ctx.close()


def test_an_action_that_has_to_wait_says_why_looks_disabled_and_starts_by_itself():
    state = {"s": dict(RUNNING)}
    started = []

    def sync(method, qs, body):
        started.append(body)
        return {"ok": True, "id": 9}
    r = window(make_api(status=lambda m, q, b: state["s"], sync=sync))
    if not r:
        return
    ctx, pg, errs, calls, ctl = r
    try:
        pg.wait_for_function("() => !document.getElementById('vJob').hidden")
        open_page(pg, "sessions")
        pg.wait_for_selector("#btnSync")
        started.clear()
        pg.click("#btnSync")
        txt = banner_text(pg)
        assert re.search(r"Queued: Sync starts when the review finishes", txt), txt
        msg = pg.text_content("#sessMsg")
        assert re.search(r"Waiting for the review to finish( \u2014 (a few seconds|about \d+ (s|min)))?", msg), msg
        assert pg.get_attribute("#btnSync", "aria-disabled") == "true" and "waiting" in pg.get_attribute("#btnSync", "class")
        assert "Waiting for the review" in pg.get_attribute("#btnSync", "title")
        assert not started, "the sync must wait for the review"
        # the review ends: the sync starts by itself and the waiting look goes
        state["s"] = {"state": "done", "kind": "review", "id": 5, "frac": 1, "result": {}, "steps": [], "message": ""}
        pg.wait_for_function("() => document.getElementById('btnSync').getAttribute('aria-disabled') !== 'true'", timeout=10000)
        settle(pg, 300)
        assert started, "the queued sync never started"
        assert not errs, errs
    finally:
        ctx.close()


def test_review_last_race_while_another_job_runs_is_never_silent():
    team = {"state": "running", "kind": "team", "id": 7, "title": "Updating the team hub", "frac": 0.5, "message": "Working", "steps": [], "started": time.time() - 20}
    r = window(make_api(status=team))
    if not r:
        return
    ctx, pg, errs, calls, ctl = r
    try:
        pg.wait_for_function("() => !document.getElementById('vJob').hidden")
        open_page(pg, "today")
        pg.click("#btnReview")
        txt = banner_text(pg)
        assert "Queued: Review starts when the team refresh finishes" in txt, txt
        assert pg.get_attribute("#btnReview", "aria-disabled") == "true" and "waiting" in pg.get_attribute("#btnReview", "class")
        assert not any(c[1] == "review" for c in calls), calls
    finally:
        ctx.close()


def test_job_view_offers_cancel_for_review_prep_team_and_weekly_and_it_calls_job_stop():
    for kind, title in (("review", "Reviewing your last race"), ("prep", "Building your track prep page"), ("team", "Updating the team hub"),
                        ("weekly", "Updating this week's team leaderboards")):
        st = {"state": "running", "kind": kind, "id": 3, "title": title, "frac": 0.2, "message": "Working", "steps": ["Step"], "started": time.time()}
        r = window(make_api(status=st, **{"job-stop": {"ok": True, "stopping": True}}))
        if not r:
            return
        ctx, pg, errs, calls, ctl = r
        try:
            pg.wait_for_function("() => !document.getElementById('vJob').hidden")
            assert pg.is_visible("#jobStop") and pg.text_content("#jobStop") == "Cancel", kind
            pg.click("#jobStop")
            pg.wait_for_function("() => /Stopping/.test(document.getElementById('jobNow').textContent)")
            assert any(c[0] == "POST" and c[1] == "job-stop" for c in calls), (kind, calls)
            assert not errs, errs
        finally:
            ctx.close()


def test_stopped_and_failed_jobs_offer_open_saved_reviews_then_try_again():
    for cancelled, err in ((False, "Debrief can't reach Garage 61. Check your internet connection, then try again."),
                           (True, "You stopped it. Nothing was changed.")):
        st = {"state": "error", "kind": "review", "id": 4, "title": "Reviewing your last race", "frac": 0.02, "message": "Connecting...",
              "steps": [], "error": err, "cancelled": cancelled}
        seq = iter([dict(RUNNING)])
        r = window(make_api(status=lambda m, q, b: next(seq, st)))
        if not r:
            return
        ctx, pg, errs, calls, ctl = r
        try:
            pg.wait_for_function("() => !document.getElementById('vJob').hidden && !document.getElementById('jobErr').hidden")
            btns = pg.evaluate("() => [...document.querySelectorAll('#jobErr button')].filter(b => !b.hidden).map(b => b.textContent.trim())")
            assert btns == ["Open saved reviews", "Try again"], btns
            assert "primary" in pg.get_attribute("#jobSaved", "class") and "primary" not in pg.get_attribute("#jobErr [data-act=retry]", "class")
            assert "Open Sessions" not in pg.inner_text("#jobErr")
            if cancelled:
                assert pg.text_content("#jobErrTitle") == "You stopped it"
            pg.click("#jobSaved")
            pg.wait_for_function("() => !document.getElementById('vFrame').hidden")
            assert pg.get_attribute(".rev.active", "data-dir") == "2026-11-01_zolder_gt4_aa11"
            assert not errs, errs
        finally:
            ctx.close()


def test_an_error_with_no_saved_reviews_makes_try_again_the_main_button():
    st = {"state": "error", "kind": "review", "id": 4, "title": "x", "frac": 0, "message": "", "steps": [], "error": "Garage 61 didn't accept that token."}
    seq = iter([dict(RUNNING)])
    r = window(make_api(status=lambda m, q, b: next(seq, st), reviews={"ok": True, "folder": "C:/lib", "groups": []}))
    if not r:
        return
    ctx, pg, errs, calls, ctl = r
    try:
        pg.wait_for_function("() => !document.getElementById('jobErr').hidden")
        assert pg.is_hidden("#jobSaved") and "primary" in pg.get_attribute("#jobErr [data-act=retry]", "class")
        assert pg.is_visible("#jobSettings")      # the token is the problem: Settings is offered
    finally:
        ctx.close()


# ---------------------------------------------------------------- 3: instant last review
def test_review_last_race_opens_the_saved_review_at_once_and_offers_a_newer_race():
    saved = {"dir": "2026-11-01_zolder_gt4_aa11", "started_at": "2026-11-01T19:00:00Z", "event": "E1"}

    def last(method, qs, body):
        if "check=1" in qs:
            return {"ok": True, "checked": True, "newer": {"event": "E9", "track_id": 1, "car_id": 2, "session_type": 3}}
        return {"ok": True, "saved": saved, "instant": True, "newest_synced": None}
    r = window(make_api(**{"last-race": last}, review={"ok": True, "id": 11}))
    if not r:
        return
    ctx, pg, errs, calls, ctl = r
    try:
        pg.wait_for_function("() => !document.getElementById('vToday').hidden")
        n0 = len(calls)
        t0 = time.time()
        pg.click("#btnReview")
        pg.wait_for_function("() => !document.getElementById('vFrame').hidden")
        assert time.time() - t0 < 2.0
        assert pg.get_attribute(".rev.active", "data-dir") == saved["dir"]
        assert not any(c[0] == "POST" and c[1] == "review" for c in calls[n0:]), "an instant review must not start a job"
        pg.wait_for_function("() => document.getElementById('bannerAct') && !document.getElementById('banner').hidden")
        assert "Newer race found" in banner_text(pg) and pg.text_content("#bannerAct") == "Review it"
        pg.click("#bannerAct")
        pg.wait_for_function("() => !document.getElementById('vJob').hidden")
        posts = [c for c in calls if c[0] == "POST" and c[1] == "review"]
        assert posts and json.loads(posts[-1][3]) == {"event": "E9", "session_type": 3, "track_id": 1, "car_id": 2}, posts
        assert not errs, errs
    finally:
        ctx.close()


def test_review_last_race_runs_the_job_when_nothing_is_saved_or_a_newer_race_is_synced():
    r = window(make_api(**{"last-race": {"ok": True, "saved": None, "instant": False, "newest_synced": None}}, review={"ok": True, "id": 11}))
    if not r:
        return
    ctx, pg, errs, calls, ctl = r
    try:
        pg.wait_for_function("() => !document.getElementById('vToday').hidden")
        pg.click("#btnReview")
        pg.wait_for_function("() => !document.getElementById('vJob').hidden")
        assert any(c[0] == "POST" and c[1] == "review" for c in calls)
    finally:
        ctx.close()


def test_a_saved_review_with_no_newer_race_shows_no_banner():
    saved = {"dir": "2026-11-01_zolder_gt4_aa11", "started_at": "2026-11-01T19:00:00Z", "event": "E1"}
    r = window(make_api(**{"last-race": lambda m, q, b: {"ok": True, "saved": saved, "instant": True, "checked": True, "newer": None}}))
    if not r:
        return
    ctx, pg, errs, calls, ctl = r
    try:
        pg.wait_for_function("() => !document.getElementById('vToday').hidden")
        pg.click("#btnReview")
        pg.wait_for_function("() => !document.getElementById('vFrame').hidden")
        settle(pg, 500)
        assert "Newer race" not in banner_text(pg)
        assert any(c[1] == "last-race" and "check=1" in c[2] for c in calls), "Garage 61 should be checked in the background"
    finally:
        ctx.close()


NOTOKEN_SETTINGS = dict(make_api()["settings"], token_saved=False, token_hint="")


def test_review_without_a_token_scrolls_to_the_token_box_and_says_so():
    r = window(make_api(settings=NOTOKEN_SETTINGS, reviews={"ok": True, "folder": "C:/lib", "groups": []}, firstrun={"ok": True, "steps": STEPS[1:] + [
        {"id": "token", "done": False, "title": "Connect Garage 61", "text": "Paste.", "action": "settings_token"}], "done": 3, "total": 4,
        "next": "token", "all_done": False, "dismissed": False, "show": True}, today={"ok": True, "one": {"source": "none"}, "has_data": False,
                                                                                    "setup": None, "setup_done": False, "week_race": {"state": "pick"}}),
               viewport=(1280, 720))
    if not r:
        return
    ctx, pg, errs, calls, ctl = r
    try:
        pg.wait_for_function("() => !document.getElementById('vWelcome').hidden")
        # the token field shows without scrolling, above the telemetry steps
        box = pg.evaluate("() => { const r = document.getElementById('gTok').getBoundingClientRect(); return [r.top, r.bottom] }")
        assert 0 <= box[0] and box[1] <= 720, box
        order = pg.evaluate("() => { const g = document.getElementById('guide'), f = document.getElementById('firstRun'); "
                            "return f.hidden ? 'hidden' : (g.compareDocumentPosition(f) & Node.DOCUMENT_POSITION_FOLLOWING ? 'token first' : 'telemetry first') }")
        assert order in ("token first", "hidden"), order
        # click Review last race: it says what to do, scrolls to the token box and flashes it
        pg.evaluate("() => document.getElementById('vWelcome').scrollTo ? 0 : 0")
        pg.click("#btnReview", force=True)
        assert "Paste your Garage 61 token first" in pg.text_content("#gTokMsg"), pg.text_content("#gTokMsg")
        assert "Paste your Garage 61 token first" in banner_text(pg)
        assert pg.evaluate("() => document.getElementById('gs3').classList.contains('flash')")
        assert pg.evaluate("() => document.activeElement.id") == "gTok"
        # from another page it takes you there
        open_page(pg, "practice")
        pg.click("#btnReview", force=True)
        pg.wait_for_function("() => !document.getElementById('vWelcome').hidden")
        assert "Paste your Garage 61 token first" in pg.text_content("#gTokMsg")
        assert not errs, errs
    finally:
        ctx.close()


def test_review_without_a_token_from_settings_flashes_the_settings_token_box():
    r = window(make_api(settings=NOTOKEN_SETTINGS, reviews={"ok": True, "folder": "C:/lib", "groups": GROUPS}))
    if not r:
        return
    ctx, pg, errs, calls, ctl = r
    try:
        pg.wait_for_function("() => document.getElementById('btnReview')")
        open_page(pg, "settings")
        pg.click("#btnReview", force=True)
        assert pg.evaluate("() => document.getElementById('fToken').classList.contains('flash')")
        assert "Paste your Garage 61 token first" in pg.text_content("#tokMsg")
    finally:
        ctx.close()


# ---------------------------------------------------------------- 4: one-click Prepare
PREPARE = {"ok": True, "token": True, "schedule": {"season": "2026 Season 4", "series": 2, "weeks": 12, "first_day": "2026-09-01",
                                                   "last_day": "2026-11-30", "current_week": 7, "out_of_date": False, "warnings": []},
           "current_week": 7, "mine": [], "choice": {"series": "gt4", "car": "McLaren 570S GT4", "week": 7, "follow_now": True},
           "plan": None, "suggestions": [], "all": [], "chosen": [],
           "catalog": {"current_week": 7, "tracks": ["Zolder Grand Prix", "Spa-Francorchamps"],
                       "driven": [{"name": "McLaren 570S GT4", "uses": 4}, {"name": "Porsche 911 GT3 R", "uses": 2}],
                       "cars": [{"name": "McLaren 570S GT4", "class": "GT4"}, {"name": "Porsche 911 GT3 R", "class": "GT3"},
                                {"name": "Ferrari 296 GT3", "class": "GT3"}, {"name": "Mazda MX-5", "class": "Sports car"}],
                       "series": [{"id": "gt4", "name": "GT4 Fixed", "category": "Road", "cars": ["McLaren 570S GT4"], "starred": True, "suggested": False,
                                   "weeks": [{"no": 7, "week": 7, "start": "2026-11-09", "track": "Zolder Grand Prix", "short": "Zolder", "left": True}]},
                                  {"id": "gt3", "name": "GT3 Open", "category": "Road", "cars": ["Porsche 911 GT3 R", "Ferrari 296 GT3"], "starred": False,
                                   "suggested": False, "weeks": [{"no": 7, "week": 7, "start": "2026-11-09", "track": "Spa-Francorchamps", "short": "Spa", "left": True}]}]}}
FOCUS = {"ok": True, "week": None, "race": None, "series": None}


def test_this_weeks_race_card_prepares_in_one_click():
    sent = []

    def choice(method, qs, body):
        sent.append(body)
        return {"ok": True}
    r = window(make_api(prepare=PREPARE, prepfocus=FOCUS, **{"prepare-choice": choice}))
    if not r:
        return
    ctx, pg, errs, calls, ctl = r
    try:
        pg.wait_for_selector("#tdWeekGo")
        assert "GT4 Fixed" in pg.text_content("#tdWeekBody") and "Zolder" in pg.text_content("#tdWeekBody")
        assert pg.text_content("#tdWeekGo") == "Prepare this race"
        assert pg.locator("#tdWeekBody button").count() == 1
        pg.click("#tdWeekGo")
        pg.wait_for_function("() => !document.getElementById('vPrep').hidden")
        assert sent and sent[0]["series"] == "gt4" and sent[0]["week"] == 7 and sent[0]["follow_now"] is True, sent
        # Prepare is open on that series, week and car
        pg.wait_for_function("() => document.getElementById('pkSeries').value === 'GT4 Fixed'")
        assert pg.input_value("#pkCar") == "McLaren 570S GT4"
        assert pg.input_value("#pkTrack") == "Zolder Grand Prix"
        assert not errs, errs
    finally:
        ctx.close()


def test_this_weeks_race_card_offers_pick_your_series_when_nothing_is_known():
    r = window(make_api(prepare=dict(PREPARE, choice={}), prepfocus=FOCUS,
                        today=dict(TODAY, week_race={"state": "pick", "why": "nothing_chosen"})))
    if not r:
        return
    ctx, pg, errs, calls, ctl = r
    try:
        pg.wait_for_selector("#tdWeekGo")
        assert pg.text_content("#tdWeekGo") == "Pick your series" and pg.locator("#tdWeekBody button").count() == 1
        pg.click("#tdWeekGo")
        pg.wait_for_function("() => !document.getElementById('vPrep').hidden")
    finally:
        ctx.close()


def test_prepare_car_list_follows_the_series_with_show_all_cars():
    r = window(make_api(prepare=PREPARE, prepfocus=FOCUS, **{"prepare-choice": {"ok": True}}))
    if not r:
        return
    ctx, pg, errs, calls, ctl = r
    try:
        open_page(pg, "prep")
        pg.wait_for_function("() => document.getElementById('pkSeries').value === 'GT4 Fixed'")
        pg.click("#pkCar")
        opts = pg.evaluate("() => [...document.querySelectorAll('#pkCarList [role=option]')].map(o => o.textContent)")
        assert any("McLaren 570S GT4" in o for o in opts) and not any("Ferrari" in o or "Mazda" in o for o in opts), opts
        assert any("Show all cars" in o for o in opts), opts
        pg.click("#pkCarList >> text=Show all cars")
        opts = pg.evaluate("() => [...document.querySelectorAll('#pkCarList [role=option]')].map(o => o.textContent)")
        assert any("Ferrari 296 GT3" in o for o in opts) and any("Mazda MX-5" in o for o in opts), opts
        assert not errs, errs
    finally:
        ctx.close()


# ---------------------------------------------------------------- 5: driving view
def test_driving_view_has_a_visible_exit_and_escape_leaves_it():
    r = window(make_api(**{"coach/status": {"ok": True, "running": False, "state": "off"}}),
               init_script="try{localStorage.setItem('debrief-coach-drive','1')}catch(e){}")
    if not r:
        return
    ctx, pg, errs, calls, ctl = r
    try:
        open_page(pg, "coach")
        pg.wait_for_function("() => document.body.classList.contains('codriving')")
        assert pg.is_visible("#cdExit") and "Exit driving view" in pg.text_content("#cdExit")
        # the sidebar is folded away: the exit is the way out; Esc does the same
        pg.keyboard.press("Escape")
        pg.wait_for_function("() => !document.body.classList.contains('codriving')")
        assert pg.is_visible(".side") and pg.is_visible("#coFull")
        # and the button
        pg.click("#coDriveBtn")
        pg.wait_for_function("() => document.body.classList.contains('codriving')")
        pg.click("#cdExit")
        pg.wait_for_function("() => !document.body.classList.contains('codriving')")
        assert not errs, errs
    finally:
        ctx.close()


# ---------------------------------------------------------------- 6: the frame bar's More menu
def test_more_menu_holds_the_moved_tools_and_they_still_work():
    api = make_api(result={"ok": True, "result": None}, **{"racecap": {"ok": True}})
    r = window(api)
    if not r:
        return
    ctx, pg, errs, calls, ctl = r
    try:
        pg.click(".rev[data-dir='2026-11-01_zolder_gt4_aa11']")
        pg.wait_for_function("() => !document.getElementById('frameBar').hidden")
        assert pg.is_visible("#fbResult") and pg.text_content("#fbResult") == "Add race result"
        assert pg.is_visible("#fbMore") and pg.get_attribute("#fbMore", "aria-expanded") == "false"
        for i in ("fbMarkers", "fbScan", "fbRival", "fbTeamLap", "fbDiscord"):
            assert pg.is_hidden("#" + i), i + " should be inside the closed menu"
        # keyboard: Enter opens it on the first item, arrows move, Esc closes and returns to the button
        pg.focus("#fbMore")
        pg.keyboard.press("Enter")
        assert pg.get_attribute("#fbMore", "aria-expanded") == "true"
        items = pg.evaluate("() => [...document.querySelectorAll('#fbMoreMenu button')].filter(b => !b.hidden).map(b => b.textContent.trim())")
        assert items == ["Resolve markers", "Scan this race", "Read a rival's lap", "Compare with a teammate's lap", "Post to Discord"], items
        assert pg.evaluate("() => document.activeElement.id") == "fbMarkers"
        pg.keyboard.press("ArrowDown")
        assert pg.evaluate("() => document.activeElement.id") == "fbScan"
        pg.keyboard.press("End")
        assert pg.evaluate("() => document.activeElement.id") == "fbDiscord"
        pg.keyboard.press("Escape")
        assert pg.is_hidden("#fbMoreMenu") and pg.evaluate("() => document.activeElement.id") == "fbMore"
        # each tool still does what it did
        pg.click("#fbMore")
        pg.click("#fbScan")
        assert pg.is_visible("#dlgScan") and pg.is_hidden("#fbMoreMenu")
        pg.keyboard.press("Escape")
        pg.click("#fbMore")
        pg.click("#fbRival")
        assert pg.is_visible("#dlgRival")
        pg.keyboard.press("Escape")
        pg.click("#fbMore")
        pg.click("#fbMarkers")
        settle(pg, 300)
        assert any(c[1] == "markers" for c in calls), [c[1] for c in calls]
        pg.click("#fbMore")
        pg.click("#fbTeamLap")
        settle(pg, 300)
        assert pg.is_hidden("#fbMoreMenu")
        # a click outside closes it
        pg.click("#fbMore")
        pg.click("#main", position={"x": 5, "y": 400})
        assert pg.is_hidden("#fbMoreMenu")
        # the other pages (Season digest) keep a More menu only with something in it
        assert not errs, errs
    finally:
        ctx.close()


def test_the_review_frame_bar_has_two_visible_buttons_and_more():
    r = window(make_api(result={"ok": True, "result": None}))
    if not r:
        return
    ctx, pg, errs, calls, ctl = r
    try:
        pg.click(".rev[data-dir='2026-11-01_zolder_gt4_aa11']")
        pg.wait_for_function("() => !document.getElementById('frameBar').hidden")
        vis = pg.evaluate("() => [...document.querySelectorAll('#frameBar .row > button, #frameBar .row > .morewrap > button')].filter(b => b.offsetParent !== null).map(b => b.textContent.trim())")
        assert vis == ["Add race result", "More ▾"], vis
    finally:
        ctx.close()


# ---------------------------------------------------------------- 7: polish
def test_empty_states_use_one_small_style_and_say_a_problem_once():
    ibt = {"ok": True, "folder": "C:/Users/x/Documents/iRacing/telemetry", "folder_ok": False, "indexed": False, "unread": None,
           "running": False, "sessions": []}
    r = window(make_api(**{"sessions-ibt": ibt, "plan": {"ok": False}, "setuplab/combos": dict(ibt, combos=[])}))
    if not r:
        return
    ctx, pg, errs, calls, ctl = r
    try:
        open_page(pg, "practice")
        pg.wait_for_selector("#prSessions .emptystate")
        st = pg.evaluate("""() => { const t = document.querySelector('#prSessions .emptystate .estitle'); const cs = getComputedStyle(t);
            return {size: cs.fontSize, weight: cs.fontWeight, upper: cs.textTransform, text: t.textContent, btns: document.querySelectorAll('#prSessions .emptystate button').length,
                    sentences: document.querySelectorAll('#prSessions .emptystate p.note').length} }""")
        assert st["size"] == "16px" and int(st["weight"]) >= 600 and st["upper"] == "none", st
        assert st["text"] == "Debrief can't find your telemetry folder" and st["btns"] == 1 and st["sentences"] == 1, st
        # said once: the rest of the Practice page doesn't repeat the sentence
        said = pg.evaluate("() => (document.getElementById('vPractice').innerText.match(/find your telemetry folder/gi) || []).length")
        assert said == 1, said
        assert pg.evaluate("() => !document.querySelector('#vPractice .pvtrack')")
        assert not errs, errs
    finally:
        ctx.close()


def test_hit_targets():
    r = window(make_api())
    if not r:
        return
    ctx, pg, errs, calls, ctl = r
    try:
        open_page(pg, "settings")
        pg.wait_for_selector("#recOn")
        small = pg.evaluate("""() => [...document.querySelectorAll('#vSettings .check')].filter(e => e.offsetParent !== null)
            .map(e => [e.querySelector('input').id, Math.round(e.getBoundingClientRect().height)]).filter(([, h]) => h < 32)""")
        assert not small, small
        # the whole label is clickable: the text toggles the box
        was = pg.is_checked("#recOn")
        pg.click("#fLive label[for=recOn] span")
        assert pg.is_checked("#recOn") != was
        open_page(pg, "today")
        pg.wait_for_selector(".tdlinks .linkbtn")
        sizes = pg.evaluate("() => [...document.querySelectorAll('.tdlinks .linkbtn')].map(b => Math.round(b.getBoundingClientRect().height))")
        assert sizes and min(sizes) >= 28, sizes
    finally:
        ctx.close()


def measure_today(hold_today):
    """y of the cards below the #1 thing at first paint and after Today's answer arrives; returns (before, after)."""
    r = window(make_api(), hold=("today", "firstrun") if hold_today else ())
    if not r:
        return None
    ctx, pg, errs, calls, ctl = r
    try:
        pg.wait_for_function("() => !document.getElementById('vToday').hidden")
        settle(pg, 400)
        ys = lambda: pg.evaluate("() => ['tdRace', 'tdPrac'].map(i => Math.round(document.getElementById(i).getBoundingClientRect().top))")
        before = ys()
        ctl.release("firstrun")
        ctl.release("today")
        pg.wait_for_function("() => document.getElementById('tdTitle').textContent.startsWith('Turn 6')")
        settle(pg, 500)
        after = ys()
        return before, after, errs
    finally:
        ctx.close()


def test_today_does_not_jump_when_its_answer_arrives():
    m = measure_today(True)
    if not m:
        return
    before, after, errs = m
    print(f"  Today: the cards below sit at y={before} before the answer and y={after} after it")
    assert before == after, (before, after)
    assert not errs, errs


def test_today_shows_grey_bars_not_loading_text_and_reserves_the_setup_strip():
    r = window(make_api(), hold=("today", "firstrun"))
    if not r:
        return
    ctx, pg, errs, calls, ctl = r
    try:
        pg.wait_for_function("() => !document.getElementById('vToday').hidden")
        assert pg.locator("#vToday .skel").count() >= 3
        assert "Loading" not in pg.inner_text("#vToday")
        h0 = pg.evaluate("() => document.getElementById('tdSetupLine').getBoundingClientRect().height")
        assert h0 >= 30, h0
        ctl.release("firstrun")
        ctl.release("today")
        pg.wait_for_function("() => document.getElementById('tdTitle').textContent.startsWith('Turn 6')")
        assert pg.locator("#vToday .skel").count() == 0
        assert pg.evaluate("() => document.getElementById('tdSetupLine').getBoundingClientRect().height") == h0
    finally:
        ctx.close()


def test_the_history_sync_paused_toast_is_gone():
    js = (UI / "app.js").read_text(encoding="utf-8")
    assert "History sync paused for this" not in js


def test_type_scale_has_six_sizes_and_the_shell_uses_only_them():
    tok = S.design_tokens()
    sizes = dict(re.findall(r"--fs-(\d+):(\d+)px", tok))
    assert sizes == {"11": "11", "13": "13", "14": "14", "16": "16", "21": "21", "46": "46"}, sizes
    css = (UI / "app.css").read_text(encoding="utf-8")
    plain = sorted(set(re.findall(r"font-size:(\d+(?:\.\d+)?)px", css)))
    assert not [p for p in plain if float(p) <= 25], plain
    assert "font-size:var(--fs-46)" in css and re.search(r"h1\{font-size:var\(--fs-46\)", css)
    for odd in ("10.5px", "12.5px", "17px", "18px", "25px"):
        assert "font-size:" + odd not in css


def test_the_library_pages_share_the_window_look():
    lib = (ROOT / "debrief" / "templates" / "library.html").read_text(encoding="utf-8")
    assert re.search(r"h1\{[^}]*font-size:var\(--fs-46\)[^}]*text-transform:uppercase", lib)
    assert ".wrap{max-width:860px" in lib
    head = lib.split("<header>", 1)[1].split("</header>", 1)[0]
    assert head.index("<h1>") < head.index('class="chiprow"'), "the pill row sits under the title"
    css = (UI / "app.css").read_text(encoding="utf-8")
    assert ".col{max-width:860px" in css and re.search(r"h1\{font-size:var\(--fs-46\);text-transform:uppercase", css)


def test_keyboard_theme_radios_move_with_arrows_and_the_brand_is_not_a_tab_stop():
    r = window(make_api())
    if not r:
        return
    ctx, pg, errs, calls, ctl = r
    try:
        pg.focus("[data-theme-set=auto]")
        pg.keyboard.press("ArrowRight")
        assert pg.evaluate("() => document.activeElement.dataset.themeSet") == "light"
        assert pg.get_attribute("[data-theme-set=light]", "aria-checked") == "true"
        pg.keyboard.press("ArrowRight")
        assert pg.evaluate("() => document.activeElement.dataset.themeSet") == "dark"
        pg.keyboard.press("ArrowLeft")
        assert pg.evaluate("() => document.activeElement.dataset.themeSet") == "light"
        # Tab through the sidebar: nothing in the brand block ever takes focus, and every stop shows a ring
        pg.focus("body")
        seen = []
        for _ in range(14):
            pg.keyboard.press("Tab")
            seen.append(pg.evaluate("() => { const a = document.activeElement; return [a.closest('.brand') ? 'brand' : a.id || a.className || a.tagName, "
                                    "getComputedStyle(a).outlineStyle] }"))
        assert not any(x[0] == "brand" for x in seen), seen
        assert all(x[1] != "none" for x in seen if x[0] != "BODY"), seen
    finally:
        ctx.close()


# ---------------------------------------------------------------- 8: Settings
def test_settings_save_switches_by_themselves_and_keep_save_for_typed_text():
    saved = []

    def settings(method, qs, body):
        if method == "POST":
            saved.append(body)
            return {"ok": True, "settings": make_api()["settings"]}
        return make_api()["settings"]
    r = window(make_api(settings=settings))
    if not r:
        return
    ctx, pg, errs, calls, ctl = r
    try:
        open_page(pg, "settings")
        pg.wait_for_selector("#recOn")
        # the section links jump to each section
        links = pg.evaluate("() => [...document.querySelectorAll('#setNav [data-jump]')].map(b => b.textContent.trim())")
        assert links == ["Account", "Reviews", "Units", "Live data", "History sync", "Coach", "Updates", "Team and extras", "About"], links
        pg.click("#setNav [data-jump=fUpdate]")
        top = pg.evaluate("() => { const t = document.getElementById('fUpdate').getBoundingClientRect().top, m = document.getElementById('vSettings').getBoundingClientRect().top; return t - m }")
        assert top < 140, top
        pg.click("#setNav [data-jump=pExtras]")
        assert pg.evaluate("() => document.getElementById('pExtras').open")
        # Save buttons only where text is typed
        sb = pg.evaluate("() => [...document.querySelectorAll('#vSettings button[type=submit]')].map(b => b.closest('form').id + ':' + b.textContent.trim())")
        for gone in ("fUnits", "fLive", "fSync", "fUpdate"):
            assert not [x for x in sb if x.startswith(gone + ":")], (gone, sb)
        assert "fOpts:Save" in sb and "fTelemetry:Save" in sb, sb
        # a switch saves at once and shows the tick
        saved.clear()
        pg.uncheck("#recWin")
        pg.wait_for_function("() => /Saved/.test(document.getElementById('recMsg').textContent)")
        assert saved == [{"record_sessions": True, "start_with_windows": False}], saved
        assert pg.evaluate("() => getComputedStyle(document.getElementById('recMsg'), '::before').content").startswith('"\u2713')
        pg.check("#updAuto")
        pg.select_option("#refs", "3")
        pg.wait_for_function("() => /Saved/.test(document.getElementById('optMsg').textContent)")
        assert {"max_reference_laps": 3, "include_practice": False} in saved, saved
        assert not errs, errs
    finally:
        ctx.close()


def test_leaving_settings_with_unsaved_text_asks_first_and_a_switch_never_eats_it():
    r = window(make_api(settings=lambda m, q, b: ({"ok": True, "settings": make_api()["settings"]} if m == "POST" else make_api()["settings"])))
    if not r:
        return
    ctx, pg, errs, calls, ctl = r
    dialogs = []

    def on_dialog(d):
        dialogs.append(d.message)
        d.dismiss() if len(dialogs) == 1 else d.accept()
    pg.on("dialog", on_dialog)
    try:
        open_page(pg, "settings")
        pg.wait_for_selector("#salut")
        pg.fill("#salut", "Sir")
        # a switch elsewhere saves without wiping the typed name
        pg.check("#syncTel") if not pg.is_checked("#syncTel") else pg.uncheck("#syncTel")
        pg.wait_for_function("() => /Saved/.test(document.getElementById('syncOptMsg').textContent)")
        assert pg.input_value("#salut") == "Sir"
        # leaving asks: Stay keeps you here with the text
        pg.click(".navbtn[data-nav=today]")
        assert dialogs and "How Debrief addresses you" in dialogs[0], dialogs
        assert not pg.is_hidden("#vSettings") and pg.input_value("#salut") == "Sir"
        # Leave goes
        pg.click(".navbtn[data-nav=today]")
        assert len(dialogs) == 2 and pg.is_hidden("#vSettings")
        # after saving the text, leaving doesn't ask
        open_page(pg, "settings")
        pg.fill("#salut", "Boss")
        pg.click("#btnSalut")
        pg.wait_for_function("() => /Saved/.test(document.getElementById('optMsg').textContent)")
        pg.click(".navbtn[data-nav=today]")
        assert len(dialogs) == 2
        assert not errs, errs
    finally:
        ctx.close()


# ---------------------------------------------------------------- real server + real window: a long job blocks nothing
def test_real_server_a_running_review_blocks_no_filter_save_or_browse():
    s = srv()
    b = browser()
    if not b:
        return
    jobs, restore = with_slow(["review"])
    ctx = b.new_context(viewport={"width": 1280, "height": 860})
    pg = ctx.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.route("**/api/sync", lambda r: r.fulfill(body='{"ok":true,"skipped":true}', content_type="application/json"))
    pg.route("**/api/update*", lambda r: r.fulfill(body='{"ok":true,"skipped":true}', content_type="application/json"))
    try:
        pg.goto(s.url)
        pg.wait_for_selector("#btnReview")
        call("api/review", {})
        assert jobs["review"].started.wait(5)
        pg.reload()
        pg.wait_for_function("() => !document.getElementById('vJob').hidden", timeout=15000)
        assert pg.is_visible("#jobStop") and pg.text_content("#jobStop") == "Cancel"
        pg.click(".navbtn[data-nav=sessions]")
        pg.wait_for_selector("#sessFilter")
        for f in ("3", "2", "1", "all"):
            pg.click(f"#sessFilter [data-f='{f}']")
        assert "busy" not in banner_text(pg).lower()
        pg.click(".navbtn[data-nav=settings]")
        pg.select_option("#unitsSel", "kmh")
        pg.wait_for_function("() => /Saved/.test(document.getElementById('unitsMsg').textContent)")
        code, st = call("api/settings")
        assert st["units"] == "kmh", st
        pg.select_option("#unitsSel", "mph")
        pg.wait_for_function("() => /Saved/.test(document.getElementById('unitsMsg').textContent)")
        pg.click(".rev >> nth=0")
        assert "busy" not in banner_text(pg).lower()
        # Cancel on the progress screen stops it and shows the way out
        pg.click(".navbtn.busy, #btnReview")
        pg.wait_for_function("() => !document.getElementById('vJob').hidden")
        pg.click("#jobStop")
        pg.wait_for_function("() => !document.getElementById('jobErr').hidden", timeout=10000)
        assert pg.text_content("#jobErrTitle") == "You stopped it"
        assert pg.is_visible("#jobSaved") and pg.is_visible("#jobErr [data-act=retry]")
        assert not errs, errs
    finally:
        restore()
        ctx.close()
        idle()


# ---------------------------------------------------------------- the review page: reordered, nothing removed
def _review_page(tmpdir):
    """A saved review (with a race result and no tips, so the cards share one sentence) rendered by the real template."""
    from debrief import report
    lib = Path(tmpdir) / "lib"
    names = MFL.main(str(lib))
    return lib, names


def test_review_page_order_and_nothing_removed():
    b = browser()
    if not b:
        return
    from debrief import report
    lib = TMP / "revlib"
    names = MFL.main(str(lib))
    d = lib / "reviews" / names[-3]
    data = json.loads((d / "review.json").read_text(encoding="utf-8"))
    # a result, and fixes whose "what to watch" is the same sentence on every card (as insights.watch_text writes it)
    from debrief import insights as INS
    data["result"] = {"start_pos": 9, "finish_pos": 6, "field_size": 20, "incidents": 2}
    fx = [{"corner": c["id"], "gain": 0.3 - 0.02 * i, "basis": "own", "best_lap": 8} for i, c in enumerate(data["corners"][:4])]
    for f in fx:
        f["watch"] = INS.watch_text(f)
    data["insights"] = {"version": 2, "verdict": ["Your laps are steady, so the quickest gain is matching your better laps through C3 and C5."],
                        "fix": fx, "bad": [], "good": [], "tips": {}, "more_corners": 0}
    old_dir = TMP / "revold"
    old_dir.mkdir(exist_ok=True)

    def render(page_html_dir, template_text):
        html = template_text
        return html
    pages = {}
    for label, template in (("new", None),):
        out = d / "index.html"
        report.write(d, data)
        pages[label] = (d / "index.html").read_text(encoding="utf-8")
    ctx = b.new_context(viewport={"width": 1280, "height": 900})
    pg = ctx.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    try:
        pg.goto((d / "index.html").as_uri())
        pg.wait_for_selector("#stats .stat")
        order = pg.evaluate("""() => { const pos = id => { const e = document.getElementById(id); return e ? e.getBoundingClientRect().top + scrollY : null };
            return {title: pos('title'), verdict: pos('verdict'), result: pos('resultPanel'), stats: pos('stats'), story: pos('story'), fix: pos('fixList')} }""")
        assert order["title"] < order["verdict"] < order["result"] < order["stats"], order
        assert order["story"] is not None and order["story"] < order["fix"], order
        assert pg.evaluate("() => document.getElementById('verdict').closest('header') !== null"), "the 'what to work on' sentence belongs under the title"
        assert pg.evaluate("() => document.getElementById('verdict').textContent.length > 10")
        # the same sentence on every card is said once above them, the full text stays in each card's fold
        shared = pg.evaluate("() => document.getElementById('fixSame').hidden ? '' : document.getElementById('fixSame').textContent")
        cards = pg.evaluate("() => [...document.querySelectorAll('#fixList .fix')].map(c => ({head: c.querySelector('h3').textContent, folds: c.querySelectorAll('details.fsame p').length, nums: /look about the same on quicker/.test(c.textContent)}))")
        assert shared and "spread thin" in shared, shared
        assert len(cards) >= 3 and all(c["folds"] == 1 and c["nums"] for c in cards), cards
        full = pg.evaluate("() => (document.getElementById('fixList').textContent.match(/keeping one turn-in point/g) || []).length")
        assert full == len(cards), (full, len(cards))
        assert not errs, errs
    finally:
        ctx.close()


def test_review_page_keeps_every_section_and_number():
    """Nothing removed: render the same review with the 2.27 template and the new one; every section id and every
    number in the old page is still in the new one."""
    b = browser()
    if not b:
        return
    import subprocess
    from debrief import report
    old_tpl = subprocess.run(["git", "show", "main:debrief/templates/review.html"], cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8")
    if old_tpl.returncode != 0 or not old_tpl.stdout:
        print("  SKIPPED: no main branch to compare with")
        return
    lib = TMP / "revlib2"
    names = MFL.main(str(lib))
    d = lib / "reviews" / names[-1]
    data = json.loads((d / "review.json").read_text(encoding="utf-8"))
    data["result"] = {"start_pos": 9, "finish_pos": 6, "field_size": 20, "incidents": 2}
    report.write(d, data)
    new_html = (d / "index.html").read_text(encoding="utf-8")
    old_html = new_html
    # the old page = the old template around the same data (the data block is the only thing that differs between templates)
    m_new = re.search(r"window\.D\s*=\s*", new_html)
    tpl_new = (ROOT / "debrief" / "templates" / "review.html").read_text(encoding="utf-8")
    assert m_new and "/*DATA*/null" in tpl_new
    payload = new_html[new_html.index("/*DATA*/") if "/*DATA*/" in new_html else 0:]
    # simplest faithful way: swap the template text for the old one and let report.write render it again
    real = (ROOT / "debrief" / "templates" / "review.html")
    backup = real.read_text(encoding="utf-8")
    try:
        real.write_text(old_tpl.stdout, encoding="utf-8")
        report._TEMPLATE_CACHE = None if hasattr(report, "_TEMPLATE_CACHE") else None
        report.write(d, data)
        old_html = (d / "index.html").read_text(encoding="utf-8")
    finally:
        real.write_text(backup, encoding="utf-8")
    ctx = b.new_context(viewport={"width": 1280, "height": 900})
    out = {}
    try:
        for label, html in (("old", old_html), ("new", new_html)):
            f = TMP / f"rv-{label}.html"
            f.write_text(html, encoding="utf-8")
            pg = ctx.new_page()
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.goto(f.as_uri())
            pg.wait_for_selector("#stats .stat")
            pg.evaluate("() => document.querySelectorAll('details').forEach(d => d.open = true)")
            out[label] = pg.evaluate("() => ({ids: [...document.querySelectorAll('[id]')].map(e => e.id), text: document.body.textContent})")
            assert not errs, (label, errs)
            pg.close()
    finally:
        ctx.close()
    gone = [i for i in out["old"]["ids"] if i not in set(out["new"]["ids"])]
    assert not gone, "ids missing from the new review page: %s" % gone
    nums_old = re.findall(r"-?\d+(?:[.:]\d+)*", out["old"]["text"])
    pool = out["new"]["text"]
    missing = sorted({n for n in nums_old if n not in pool})
    assert not missing, "numbers on the old review page that the new one lacks: %s" % missing[:20]


def test_layout_is_bumped_and_saved_reviews_are_refreshed_once():
    from debrief import report
    assert report.LAYOUT == "2.28.0"
    lib = TMP / "layoutlib"
    MFL.main(str(lib))
    (lib / ".layout").write_text("2.18.0", encoding="utf-8")
    n = report.refresh_all(str(lib))
    assert n >= 1 and (lib / ".layout").read_text(encoding="utf-8").strip() == "2.28.0"
    assert (lib / ".backup").is_dir() or any(lib.glob(".backup*")), "reviews are backed up before the re-render"
    assert report.refresh_all(str(lib)) == 0           # once per layout


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
