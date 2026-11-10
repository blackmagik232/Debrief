"""Debrief 2.30 Race Room page: loads with no live data ("Waiting for iRacing"), with driver data and with crew data
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


def sim_state(pc_user, at_s):
    """The Race Room picture at about at_s seconds of the scripted race, read on the PC of driver pc_user."""
    key = (pc_user, at_s)
    if key in _STATES:
        return _STATES[key]
    import endu_sim as ES
    sim = ES.standard_sim(pc_user=pc_user)
    box = {}

    def on_step(room, chief, own, f):
        if sim.t >= at_s:
            box["st"] = json.loads(json.dumps(room.state()))
            raise StopIteration

    try:
        ES.run(sim, on_step=on_step)
    except StopIteration:
        pass
    _STATES[key] = box["st"]
    return box["st"]


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


def _window(room, running=True, viewport=(1280, 860), review=REVIEW):
    b = _browser()
    if not b:
        return None
    ctx = b.new_context(viewport={"width": viewport[0], "height": viewport[1]})
    pg = ctx.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" and "fonts" not in m.text and "Failed to load resource" not in m.text else None)
    api = {"raceroom": {"ok": True, "running": running, "iam": "Ben Baker", "room": room},
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
            if name == "endurance-review" and "id=" in qs:
                return route.fulfill(body=json.dumps({"ok": True, "review": review, "id": "x"}), content_type="application/json")
            return route.fulfill(body=json.dumps(api.get(name, {"ok": True})), content_type="application/json")
        return route.fulfill(status=404, body="")
    pg.route("http://debrief.test/**", handle)
    pg.route("**/fonts.g*/**", lambda r: r.abort())
    pg.goto(BASE)
    pg.wait_for_function("() => typeof window.DebriefRoom === 'object'")
    pg.evaluate("() => document.querySelector('.navbtn[data-nav=room]').click()")
    return ctx, pg, errs


def _noscroll(pg):
    return pg.evaluate("() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1")


def test_no_live_data_says_waiting():
    r = _window({"state": "waiting", "text": "Waiting for iRacing.", "role": {"role": "none"}})
    if not r:
        return
    ctx, pg, errs = r
    try:
        pg.wait_for_function("() => !document.getElementById('vRoom').hidden && document.getElementById('rrText').textContent.includes('Waiting for iRacing')")
        assert pg.evaluate("() => document.getElementById('rrBoard').hidden && document.getElementById('rrBody').hidden")
        assert pg.get_attribute(".navbtn[data-nav=room]", "aria-current") == "page"
        assert not errs, errs
    finally:
        ctx.close()


def test_coach_off_offers_start():
    r = _window({"state": "waiting", "text": "x"}, running=False)
    if not r:
        return
    ctx, pg, errs = r
    try:
        pg.wait_for_function("() => !document.getElementById('rrStart').hidden")
        assert "Start the coach" in pg.text_content("#rrText")
        assert not errs, errs
    finally:
        ctx.close()


def test_driver_data():
    st = sim_state(1, 3200.0)                       # Ben is in the car from lap 29 (about 1740 s)
    assert st["role"]["role"] == "driver", st["role"]
    r = _window(st)
    if not r:
        return
    ctx, pg, errs = r
    try:
        pg.wait_for_function("() => !document.getElementById('rrBoard').hidden")
        assert "Ben" in pg.text_content("#rrStint")
        assert pg.text_content("#rrFuelLbl") == "Fuel"
        assert pg.evaluate("() => document.querySelectorAll('#rrRivals tr').length") >= 1
        assert pg.evaluate("() => !!document.querySelector('#rrChart svg path')")
        assert "estimated" not in pg.text_content("#rrFuelLbl").lower()
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
        pg.wait_for_function("() => !document.getElementById('rrBoard').hidden")
        assert "estimated" in pg.text_content("#rrFuelLbl").lower()
        assert "text only" in pg.text_content("#rrCallSub").lower()
        assert _noscroll(pg)
        assert not errs, errs
    finally:
        ctx.close()


def test_no_plan_offers_make_a_plan():
    import endu_sim as ES
    sim = ES.standard_sim(pc_user=1)
    box = {}

    def on_step(room, chief, own, f):
        if sim.t >= 600:
            box["st"] = json.loads(json.dumps(room.state()))
            raise StopIteration
    try:
        ES.run(sim, plan=False, on_step=on_step)
    except StopIteration:
        pass
    r = _window(box["st"])
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


def test_keyboard_tabs_and_review():
    r = _window({"state": "waiting", "text": "Waiting for iRacing.", "role": {"role": "none"}})
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
