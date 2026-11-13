"""Debrief 2.30 Race Room page (2.36: the live board, laid out like the 8 Nov weekend screen, with no coach): loads with no live data ("Waiting for iRacing"), with driver data and with crew data
(taken from the scripted team-race simulator), the Race review tab, no console errors, keyboard use and no sideways scroll
at a narrow width. Browser checks use Playwright + Chromium (skipped when missing); the API is mocked.
Run: python3 tests/ci_run.py tests/test_230_roomui.py"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.modules.setdefault("irsdk", None)
UI = ROOT / "debrief" / "desktop" / "ui"
BASE = "http://debrief.test/s3cret/"
_PW = {}
_STATES = {}


def _browser():
    if "b" in _PW:
        return _PW["b"]
    try:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        b = pw.chromium.launch()
    except Exception:  # noqa: BLE001
        print("  SKIPPED: Playwright/Chromium not available")
        _PW["b"] = None
        return None
    _PW["pw"], _PW["b"] = pw, b
    return b


def sim_state(pc_user, at_s, plan=True, practice=False):
    """2.36: the Race Room board at about at_s seconds, read on the PC of driver pc_user through the window's own live link
    (roomlive) fed by the scripted team race (or a solo practice from crew_sim)."""
    key = (pc_user, at_s, plan, practice)
    if key in _STATES:
        return _STATES[key]
    from debrief import roomlive
    if practice:
        import crew_sim as CS
        sim = CS.RaceSim(laps=40, tank=60, burn=2.0, lap_s=60.0, cars=10, session_type="Practice")
        sections = lambda: (1, sim.sections())          # noqa: E731
        plan_for, iam = None, (lambda: "")
    else:
        import endu_sim as ES
        sim = ES.standard_sim(pc_user=pc_user)
        sections = lambda: (sim.info_version, sim.sections())          # noqa: E731
        saved = {"inputs": ES.plan_inputs(), "title": "Test plan", "saved": "now"}
        plan_for = (lambda track: saved) if plan else None
        who = ES.DRIVERS[pc_user]
        iam = lambda: who          # noqa: E731

    class Src:
        state = "connected"

        def __init__(self):
            self.it = sim.run(None)

        def read(self):
            try:
                own, f = next(self.it)
            except StopIteration:
                return None
            own = dict(own)
            own["_field"] = f
            return own

        def info_sections(self):
            return sections()

        def close(self):
            pass
    rl = roomlive.RoomLive(source=Src(), plan_for=plan_for, iam=iam, clock=lambda: sim.t, wall=lambda: 1.8e9 + sim.t)
    while rl.tick():
        if sim.t >= at_s:
            break
    rl._state_t = -1e9
    rl.tick()
    st = json.loads(json.dumps(rl.state()))
    _STATES[key] = st
    return st


REVIEW = {"ok": True, "track": "Test Track", "team": "Test Team", "who": "Ben Baker", "used": ["race.jsonl.gz"], "skipped": [],
          "notes": ["Left out nothing."], "laps": {"count": 80, "missing": [], "from_frames": 0},
          "finish": {"cpos": 3, "pos": 9, "laps": 88, "best_cpos": 2},
          "drivers": [{"name": "Alex Able", "stints": 1, "laps": 29, "clean": 27, "median": 60.1, "best": 59.4, "spread": 0.4, "fade": 0.3, "drive_s": 1800}],
          "stints": [{"n": 1, "driver": "Alex Able", "from_lap": 1, "to_lap": 29, "guessed": False, "median": 60.1, "best": 59.4, "spread": 0.4, "fade_total": 0.3}],
          "stops": [{"n": 1, "lap": 29, "lane_s": 62.0, "stall_s": 35.0, "plan_stop_s": 55.0, "plan_swap": True, "delta_s": 7.0, "long": False}],
          "fuel": [{"stint": 1, "driver": "Alex Able", "measured": False, "text": "No fuel reading: nobody's log from this stint has the car's fuel."}],
          "traffic": {"laps": 4, "cost_s": 9.0, "by_class": [{"class": "GT3", "cost_s": 9.0}], "note": "An estimate."},
          "rivals": [{"idx": 3, "num": "7", "name": "Rival One", "me": False, "laps": 88, "final_pos": 1, "stops": 3, "stop_laps": [28, 57, 86],
                      "avg_lane_s": 40.5, "under_caution": 0, "median_lap": 60.0, "best_lap": 59.0}]}


def _window(board, viewport=(1280, 860), review=REVIEW, peek=None, start=None):
    b = _browser()
    if not b:
        return None
    ctx = b.new_context(viewport={"width": viewport[0], "height": viewport[1]})
    pg = ctx.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" and "fonts" not in m.text and "Failed to load resource" not in m.text else None)
    api = {"raceroom": {"ok": True, "running": True, "coach": False, "iam": "Ben Baker", "auto_open": True, "board": board},
           "raceroom?peek=1": peek or {"ok": True, "connected": False, "session": None, "auto_open": True},
           "endurance-review": {"ok": True, "races": [{"id": "Endurance/2026-11-01_x", "title": "2026-11-01_x", "files": ["race.jsonl.gz"], "mtime": 1}],
                                "review": review, "id": "Endurance/2026-11-01_x"}}

    def handle(route):
        req = route.request
        rel = req.url[len(BASE):]
        path, _, qs = rel.partition("?")
        if path in ("", "index.html"):
            return route.fulfill(body=(UI / "index.html").read_text(encoding="utf-8"), content_type="text/html")
        if path.startswith("static/"):
            f = UI / path[7:]
            if not f.is_file():
                return route.fulfill(status=404, body="")
            return route.fulfill(body=f.read_text(encoding="utf-8"),
                                 content_type="text/javascript" if path.endswith(".js") else "text/css")
        if path.startswith("api/"):
            name = path[4:]
            if name == "raceroom" and qs == "peek=1":
                return route.fulfill(body=json.dumps(api["raceroom?peek=1"]), content_type="application/json")
            if name == "endurance-review" and "id=" in qs:
                return route.fulfill(body=json.dumps({"ok": True, "review": review, "id": "x"}), content_type="application/json")
            return route.fulfill(body=json.dumps(api.get(name, {"ok": True})), content_type="application/json")
        return route.fulfill(status=404, body="")
    pg.route("http://debrief.test/**", handle)
    pg.route("**/fonts.g*/**", lambda r: r.abort())
    pg.goto(BASE)
    pg.wait_for_function("() => typeof window.DebriefRoom === 'object'")
    if start is None:
        pg.evaluate("() => document.querySelector('.navbtn[data-nav=room]').click()")
    return ctx, pg, errs


def _noscroll(pg):
    return pg.evaluate("() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1")


def _waiting():
    from debrief import roomboard
    return json.loads(json.dumps(roomboard.waiting("Waiting for iRacing.", "Open iRacing and join a session: this page fills in by itself.")))


def test_no_live_data_shows_the_whole_board_waiting():
    r = _window(_waiting())
    if not r:
        return
    ctx, pg, errs = r
    try:
        pg.wait_for_function("() => !document.getElementById('vRoom').hidden && document.getElementById('rrText').textContent.includes('Waiting for iRacing')")
        assert pg.text_content("#rrPill") == "Waiting for iRacing"
        assert pg.evaluate("() => document.querySelectorAll('#rrTiles .rrtile').length") == 8
        assert pg.evaluate("() => [...document.querySelectorAll('#rrTiles .rrtile b')].every(b => b.textContent === '--')")
        assert pg.evaluate("() => document.querySelectorAll('#rrCrew .rrdesk').length") == 0
        assert pg.is_visible("#rrRivals") and pg.is_visible("#rrStints")
        assert pg.evaluate("() => document.getElementById('rrBody').hidden")
        assert pg.get_attribute(".navbtn[data-nav=room]", "aria-current") == "page"
        assert not pg.query_selector("#rrStart"), "no Start the coach button: the page needs no coach"
        assert not errs, errs
    finally:
        ctx.close()


def test_driver_data():
    st = sim_state(1, 3200.0)                       # Ben is in the car from lap 29 (about 1740 s)
    assert st["role"]["role"] == "driver", st["role"]
    assert st["session"]["kind"] == "race" and st["team"]
    r = _window(st)
    if not r:
        return
    ctx, pg, errs = r
    try:
        pg.wait_for_function("() => document.querySelectorAll('#rrCrew .rrdesk').length === 4")
        assert pg.text_content("#rrSess") == "Race" and "Live" in pg.text_content("#rrPill")
        assert pg.evaluate("() => document.querySelectorAll('#rrTiles .rrtile').length") == 8
        assert pg.text_content("#rrTiles [data-k=pos] b").startswith("P")
        assert pg.evaluate("() => document.querySelectorAll('#rrRivals tr').length") >= 2
        assert pg.evaluate("() => !!document.querySelector('#rrRivals tr.us')")
        assert pg.evaluate("() => document.querySelectorAll('#rrStints tr').length") >= 2          # Alex's stint and Ben's
        assert pg.evaluate("() => document.querySelectorAll('#rrLapChart rect').length") > 10
        assert pg.evaluate("() => !!document.querySelector('#rrChart svg path.me')")
        assert pg.evaluate("() => document.querySelectorAll('#rrFuelBar .seg').length") >= 2
        assert "Ben" in pg.text_content("#rrStint")                  # the team panels show in a team race
        assert "estimated" not in pg.text_content("#rrPfNote").lower()
        assert pg.text_content("#rrCall").strip() not in ("", "--")
        assert _noscroll(pg)
        assert not errs, errs
    finally:
        ctx.close()


def test_crew_data_is_estimated_and_narrow_has_no_sideways_scroll():
    st = sim_state(0, 3200.0)                       # Alex watches from the pit: crew while Ben drives
    assert st["role"]["role"] == "crew", st["role"]
    r = _window(st, viewport=(390, 800))
    if not r:
        return
    ctx, pg, errs = r
    try:
        pg.wait_for_function("() => document.querySelectorAll('#rrTiles .rrtile').length === 8 && document.getElementById('rrPill').textContent.includes('crew')")
        assert "estimated" in (pg.text_content("#rrPfNote") + pg.text_content("#rrTiles [data-k=box]")).lower()
        assert _noscroll(pg)
        assert not errs, errs
    finally:
        ctx.close()


def test_practice_shows_the_timesheet():
    st = sim_state(None, 900.0, practice=True)
    assert st["session"]["kind"] == "practice" and not st["team"]
    r = _window(st)
    if not r:
        return
    ctx, pg, errs = r
    try:
        pg.wait_for_function("() => document.getElementById('rrSess').textContent === 'Practice'")
        assert pg.text_content("#rrRvTitle") == "Timesheet"
        assert "Best" in pg.text_content("#rrRvHead")
        assert pg.text_content("#rrTiles [data-k=session] b") == "Practice"
        assert pg.text_content("#rrTiles [data-k=best] b") not in ("", "--")
        assert pg.evaluate("() => document.getElementById('rrBody').hidden")          # no team panels in practice
        assert pg.evaluate("() => document.querySelectorAll('#rrCrew .rrdesk').length") == 4
        assert not errs, errs
    finally:
        ctx.close()


def test_no_plan_offers_make_a_plan():
    st = sim_state(1, 600.0, plan=False)
    r = _window(st)
    if not r:
        return
    ctx, pg, errs = r
    try:
        pg.wait_for_function("() => !document.getElementById('rrNoPlan').hidden")
        pg.click("#rrMake")
        pg.wait_for_function("() => !document.getElementById('vPrep').hidden")
        assert not errs, errs
    finally:
        ctx.close()


def test_opens_by_itself_once_per_session():
    peek = {"ok": True, "connected": True, "auto_open": True, "session": {"key": "4242:1", "kind": "practice", "label": "Practice"}}
    r = _window(_waiting(), peek=peek, start="today")
    if not r:
        return
    ctx, pg, errs = r
    try:
        pg.wait_for_function("() => !document.getElementById('vRoom').hidden", timeout=10000)
        pg.evaluate("() => document.querySelector('.navbtn[data-nav=coach]').click()")
        pg.wait_for_function("() => !document.getElementById('vCoach').hidden")
        pg.wait_for_timeout(4000)                     # the next peeks: the same session does not pull him back
        assert pg.evaluate("() => document.getElementById('vRoom').hidden")
        assert not errs, errs
    finally:
        ctx.close()


def test_switched_off_stays_put():
    peek = {"ok": True, "connected": True, "auto_open": False, "session": {"key": "4242:2", "kind": "race", "label": "Race"}}
    r = _window(_waiting(), peek=peek, start="today")
    if not r:
        return
    ctx, pg, errs = r
    try:
        pg.wait_for_timeout(4500)
        assert pg.evaluate("() => document.getElementById('vRoom').hidden")
        assert not errs, errs
    finally:
        ctx.close()


def test_keyboard_tabs_and_review():
    r = _window(_waiting())
    if not r:
        return
    ctx, pg, errs = r
    try:
        pg.wait_for_function("() => !document.getElementById('vRoom').hidden")
        pg.focus("#rrTabRev")
        pg.keyboard.press("Enter")
        pg.wait_for_function("() => document.getElementById('rrRevPick').options.length > 0 && !document.getElementById('rrRevGo').disabled")
        pg.focus("#rrRevGo")
        pg.keyboard.press("Enter")
        pg.wait_for_function("() => document.getElementById('rrRevBody').textContent.includes('Pace by driver')")
        txt = pg.text_content("#rrRevBody")
        assert "Alex Able" in txt and "Rival One" in txt and "GT3" in txt
        assert _noscroll(pg)
        pg.set_viewport_size({"width": 360, "height": 700})
        assert _noscroll(pg)
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
            import traceback
            failed += 1
            print(f"FAIL  {name}")
            traceback.print_exc()
    if "pw" in _PW:
        try:
            _PW["b"].close()
            _PW["pw"].stop()
        except Exception:  # noqa: BLE001
            pass
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
