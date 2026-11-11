"""Debrief 2.31 Race Room as the Race Engineer's page: not connected, connected in practice (never says it "waits for the
race"), qualifying, race, crew, the radio transcript and pinned message, a typed question answered, a narrow window with no
sideways scroll, keyboard use and no console errors. Playwright + Chromium (skipped when missing); the API is mocked and the
pictures come from the real Engineer fed by a scripted car.
Run: python3 tests/ci_run.py tests/test_231_engineer_ui.py"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.modules.setdefault("irsdk", None)
import test_230_roomui as R230  # noqa: E402
from test_231_engineer import Run  # noqa: E402

UI = ROOT / "debrief" / "desktop" / "ui"
BASE = R230.BASE


def eng_state(kind="practice", role="driver", laps=3):
    r = Run(kind, role=role, lap_s=60.0)
    r.leave_pits()
    r.laps(laps)
    st = json.loads(json.dumps(r.eng.state()))
    st["connected"] = True
    return st


def room_of(eng, state="waiting"):
    return {"state": state, "text": "x", "role": {"role": "driver"}, "engineer": eng}


def window(room, running=True, viewport=(1280, 860), ask=None):
    b = R230._browser()
    if not b:
        return None
    ctx = b.new_context(viewport={"width": viewport[0], "height": viewport[1]})
    pg = ctx.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" and "fonts" not in m.text and "Failed to load resource" not in m.text else None)
    posted = []

    def handle(route):
        req = route.request
        rel = req.url[len(BASE):]
        path, _, _qs = rel.partition("?")
        if path in ("", "index.html"):
            return route.fulfill(body=(UI / "index.html").read_text(encoding="utf-8"), content_type="text/html")
        if path.startswith("static/"):
            f = UI / path[7:]
            if not f.is_file():
                return route.fulfill(status=404, body="")
            return route.fulfill(body=f.read_text(encoding="utf-8"), content_type="text/javascript" if path.endswith(".js") else "text/css")
        if path.startswith("api/"):
            name = path[4:]
            if name == "raceroom" and req.method == "POST":
                body = json.loads(req.post_data or "{}")
                posted.append(body)
                if body.get("action") == "ask" and ask:
                    return route.fulfill(body=json.dumps(ask(body)), content_type="application/json")
            if name == "raceroom":
                return route.fulfill(body=json.dumps({"ok": True, "running": running, "iam": "Ben Baker", "room": room}), content_type="application/json")
            return route.fulfill(body=json.dumps({"ok": True}), content_type="application/json")
        return route.fulfill(status=404, body="")
    pg.route("http://debrief.test/**", handle)
    pg.route("**/fonts.g*/**", lambda r: r.abort())
    pg.goto(BASE)
    pg.wait_for_function("() => typeof window.DebriefRoom === 'object'")
    pg.evaluate("() => document.querySelector('.navbtn[data-nav=room]').click()")
    return ctx, pg, errs, posted


def _all_text(pg):
    return pg.evaluate("() => document.getElementById('vRoom').innerText")


def test_not_connected_says_waiting():
    r = window(room_of({"connected": False}))
    if not r:
        return
    ctx, pg, errs, _ = r
    try:
        pg.wait_for_function("() => document.getElementById('rrText').textContent.includes('Waiting for iRacing')")
        assert pg.evaluate("() => document.getElementById('enWrap').hidden")
        assert R230._noscroll(pg)
        assert not errs, errs
    finally:
        ctx.close()


def test_practice_is_live_and_never_waits_for_a_race():
    st = eng_state("practice")
    r = window(room_of(st))
    if not r:
        return
    ctx, pg, errs, _ = r
    try:
        pg.wait_for_function("() => !document.getElementById('enWrap').hidden")
        pg.wait_for_function("() => !document.getElementById('enPractice').hidden")
        txt = _all_text(pg).lower()
        assert "waits for the race" not in txt and "not a race" not in txt, txt[:400]
        assert "practice" in pg.text_content("#enKind").lower()
        assert pg.evaluate("() => document.getElementById('enQuali').hidden")
        assert "Live" in pg.text_content("#rrPill")
        assert pg.evaluate("() => document.querySelectorAll('#enLaps tr').length") >= 1
        assert R230._noscroll(pg)
        assert not errs, errs
    finally:
        ctx.close()


def test_qualifying_shows_traffic_and_state():
    st = eng_state("lone_qualify")
    r = window(room_of(st))
    if not r:
        return
    ctx, pg, errs, _ = r
    try:
        pg.wait_for_function("() => !document.getElementById('enQuali').hidden")
        assert "qualifying" in pg.text_content("#enKind").lower()
        assert pg.evaluate("() => document.querySelectorAll('#enQCards .crcard').length") >= 4
        assert "track position" in pg.text_content("#enQNote")
        assert not errs, errs
    finally:
        ctx.close()


def test_race_keeps_the_race_room():
    rs = R230.sim_state(1, 3200.0)
    st = eng_state("race")
    room = dict(rs)
    room["engineer"] = st
    r = window(room)
    if not r:
        return
    ctx, pg, errs, _ = r
    try:
        pg.wait_for_function("() => !document.getElementById('rrBoard').hidden")
        assert pg.evaluate("() => document.getElementById('enQuali').hidden && document.getElementById('enPractice').hidden")
        assert "Driving" in pg.text_content("#rrPill")
        assert not errs, errs
    finally:
        ctx.close()


def test_crew_is_text_only_and_narrow_has_no_sideways_scroll():
    st = eng_state("practice", role="crew")
    r = window(room_of(st), viewport=(390, 800))
    if not r:
        return
    ctx, pg, errs, _ = r
    try:
        pg.wait_for_function("() => !document.getElementById('enWrap').hidden")
        assert "text only" in pg.text_content("#enTalk").lower()
        assert R230._noscroll(pg)
        assert not errs, errs
    finally:
        ctx.close()


def test_transcript_and_pinned_message():
    st = eng_state("practice")
    st["pinned"] = {"text": "Box this lap, fuel is critical."}
    st["transcript"] = [
        {"id": 3, "clock": "12:04", "who": "engineer", "text": "Two laps of fuel left.", "spoken": True, "urgent": True},
        {"id": 2, "clock": "12:01", "who": "you", "text": "how's the fuel"},
        {"id": 1, "clock": "11:50", "who": "coach", "text": "Brake a touch later at turn 3."},
        {"id": 0, "clock": "11:40", "who": "engineer", "text": "Quiet zone, held back.", "spoken": False, "why": "you were braking"}]
    r = window(room_of(st))
    if not r:
        return
    ctx, pg, errs, _ = r
    try:
        pg.wait_for_function("() => !document.getElementById('enPin').hidden")
        assert "Box this lap" in pg.text_content("#enPin")
        assert pg.evaluate("() => document.querySelectorAll('#enLog li').length") == 4
        first = pg.evaluate("() => document.querySelector('#enLog li').textContent")
        assert "Two laps of fuel left" in first and "12:04" in first
        assert "Not said: you were braking" in pg.text_content("#enLog")
        pg.click("#enCoachCues")                                  # hide coaching cues
        assert pg.evaluate("() => document.querySelectorAll('#enLog li').length") == 3
        assert not errs, errs
    finally:
        ctx.close()


def test_typed_question_is_answered_by_keyboard():
    st = eng_state("practice")
    st["transcript"] = []
    after = dict(st)
    after["transcript"] = [{"id": 2, "clock": "12:02", "who": "engineer", "text": "Six laps of fuel.", "spoken": True},
                           {"id": 1, "clock": "12:02", "who": "you", "text": "how's the fuel"}]

    def ask(body):
        assert body.get("text") == "how's the fuel"
        return {"ok": True, "intent": "fuel", "answer": "Six laps of fuel.", "room": room_of(after)}
    r = window(room_of(st), ask=ask)
    if not r:
        return
    ctx, pg, errs, posted = r
    try:
        pg.wait_for_function("() => !document.getElementById('enWrap').hidden")
        pg.focus("#enQ")
        pg.keyboard.type("how's the fuel")
        pg.keyboard.press("Enter")
        pg.wait_for_function("() => document.getElementById('enLog').textContent.includes('Six laps of fuel')")
        assert any(p.get("action") == "ask" for p in posted)
        assert pg.evaluate("() => document.getElementById('enQ').value") == ""
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
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {name}: {e!r}")
    b = R230._PW.get("b")
    if b:
        b.close()
        R230._PW["pw"].stop()
    print("ALL OK" if not failed else f"{failed} FAILED")
    sys.exit(1 if failed else 0)
