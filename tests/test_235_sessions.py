"""Debrief 2.35 "Review a session": the big button (and Ctrl+Enter) opens All sessions with the newest session highlighted,
every row has its own Review (a race opens or builds its race review, practice and qualifying open in Practice), the PC's
newest race has a card, an empty list says what to do, and Help leads with the after-you-drive steps and says where data comes from.

    python3 tests/test_235_sessions.py

The real index.html / app.js / help.js / app.css in Chromium with every /api/ call answered here (no server). Skipped without
Playwright + Chromium. No network, utf-8 everywhere.
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
UI = ROOT / "debrief" / "desktop" / "ui"
BASE = "http://debrief.test/s3cret/"
PW = {}

from debrief.desktop import server as S  # noqa: E402


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
     "session": "Race", "laps": 12}]}]
STEPS = [{"id": "token", "done": True, "title": "Connect Garage 61", "text": "Saved.", "action": None},
         {"id": "record", "done": True, "title": "Record", "text": "On.", "action": None}]
FIRSTRUN = {"ok": True, "steps": STEPS, "done": 2, "total": 2, "next": None, "all_done": True, "dismissed": False, "show": False}
TODAY = {"ok": True, "one": {"source": "none"}, "last_race": None, "last_practice": None, "reviews": 1, "sessions": 3, "setup": FIRSTRUN,
         "setup_done": True, "has_data": True, "week_race": {"state": "pick"}}
WHATSNEW = {"ok": True, "show": False}
START_P = "2026-11-03T19:00:00+00:00"
EPOCH_P = datetime.fromisoformat(START_P).astimezone(timezone.utc).timestamp()


def row(i, type_, session, start, review=None, **kw):
    return dict({"key": f"k{i}", "event": f"E{i}", "type": type_, "session": session, "track": "Zolder", "car": "McLaren 570S GT4",
                 "start": start, "laps": 12, "best": 95.0, "best_clean": 95.2, "track_id": 1, "car_id": 2, "review": review}, **kw)


# the newest one (practice, Nov 3) is deliberately NOT first, so the highlight is by date, not by position
SESSIONS = [row(1, 3, "Race", "2026-11-01T19:00:00+00:00", review="2026-11-01_zolder_gt4_aa11"),
            row(3, 1, "Practice", START_P),
            row(2, 3, "Race", "2026-11-02T19:00:00+00:00")]


ZOLDER, CAR = "Zolder Grand Prix", "McLaren 570S GT4"
# a realistic telemetry index: a Monza file 10 minutes before (wrong track), a Zolder race file an hour later (wrong kind of
# session), another car at Zolder, and the real practice file that started 5 minutes before Garage 61's start
IBT = [{"id": "pc-monza", "track": "Autodromo Nazionale Monza", "car": CAR, "sort": EPOCH_P - 600, "session_types": ["Practice"], "subsession": 111},
       {"id": "pc-zolder-race", "track": ZOLDER, "car": CAR, "sort": EPOCH_P + 3600, "session_types": ["Race"], "subsession": 222},
       {"id": "pc-other-car", "track": ZOLDER, "car": "Porsche 911 GT3 R", "sort": EPOCH_P - 120, "session_types": ["Practice"], "subsession": 333},
       {"id": "pc-practice-1", "track": ZOLDER, "car": CAR, "sort": EPOCH_P - 300, "session_types": ["Practice"], "subsession": 444}]


def make_api(**over):
    a = {"settings": {"ok": True, "token_saved": True, "token_hint": "…abcd", "version": "2.35.0", "installed": True, "first_name": "Sam",
                      "data_folder": "C:/x", "max_reference_laps": 2, "units": "mph", "sync_on_open": True, "check_updates": True,
                      "record_sessions": True, "start_with_windows": True, "include_practice": False, "sync_telemetry": "best",
                      "salutation": "", "iracing_folder": "", "racecap_library": "", "team_cards_folder": "", "share_my_cards": False},
         "brand": {"app_name": "Debrief"}, "reviews": {"ok": True, "folder": "C:/lib", "groups": GROUPS},
         "status": {"state": "idle"}, "sync": {"ok": True, "skipped": True}, "update": {"ok": True, "skipped": True},
         "pages": {"ok": True, "prep": []}, "firstrun": FIRSTRUN, "today": TODAY, "whatsnew": WHATSNEW,
         "last-race": {"ok": True, "saved": None, "instant": False, "local_newer": False, "newest_synced": None},
         "sessions-ibt": {"ok": True, "folder_ok": True, "indexed": True, "sessions": IBT},
         "session-review": {"ok": False, "error": "stand-in"},
         "review": {"ok": True, "id": 7},
         "sessions": {"ok": True, "token": True, "summary": {"synced": True, "sessions": 3, "laps": 30, "combos": 1,
                                                             "last_sync": "2026-11-08T10:00:00Z"}, "sessions": SESSIONS, "total": 3}}
    a.update(over)
    return a


def window(api, viewport=(1280, 860), ready="vToday"):
    b = browser()
    if not b:
        return None
    ctx = b.new_context(viewport={"width": viewport[0], "height": viewport[1]})
    pg = ctx.new_page()
    errs, calls = [], []
    pg.on("pageerror", lambda e: errs.append(str(e)))

    def handle(route):
        req = route.request
        rel = req.url[len(BASE):]
        path, _, qs = rel.partition("?")
        if path in ("", "index.html"):
            return route.fulfill(body=(UI / "index.html").read_text(encoding="utf-8"), content_type="text/html")
        if path == "static/tokens.css":
            return route.fulfill(body=S.design_tokens(), content_type="text/css")
        if path in ("static/app.js", "static/app.css", "static/help.js"):
            return route.fulfill(body=(UI / path[7:]).read_text(encoding="utf-8"),
                                 content_type="text/javascript" if path.endswith(".js") else "text/css")
        if path.startswith("lib/") or path.startswith("sample/"):
            return route.fulfill(body="<!doctype html><title>Page</title><body>page</body>", content_type="text/html")
        if path.startswith("api/"):
            name = path[4:]
            body = None
            try:
                body = json.loads(req.post_data) if req.post_data else None
            except ValueError:
                body = None
            calls.append((req.method, name, qs, body))
            v = api.get(name, {"ok": True})
            if callable(v):
                v = v(req.method, qs, body)
            return route.fulfill(body=json.dumps(v), content_type="application/json")
        return route.fulfill(status=404, body="")
    pg.route("http://debrief.test/**", handle)
    pg.route("**/fonts.g*/**", lambda r: r.abort())
    pg.goto(BASE)
    pg.wait_for_function("() => !document.getElementById('" + ready + "').hidden", timeout=10000)
    return ctx, pg, errs, calls


def posts(calls, name):
    return [c for c in calls if c[0] == "POST" and c[1] == name]


def newest_state(pg):
    return pg.evaluate("""() => { const rows = [...document.querySelectorAll('#sessList .item.sess')];
        const n = rows.filter(r => r.classList.contains('newest'));
        return {rows: rows.length, newest: n.length, idx: rows.indexOf(n[0]), text: n[0] ? n[0].textContent : '',
                focus: document.activeElement && document.activeElement.classList.contains('revbtn') && n[0] && n[0].contains(document.activeElement)} }""")


def test_button_is_review_a_session():
    r = window(make_api())
    if not r:
        return
    ctx, pg, errs, calls = r
    try:
        assert pg.text_content("#btnReview .lbl") == "Review a session"
        assert pg.get_attribute("#btnReview", "aria-keyshortcuts") == "Control+Enter"
        assert "Ctrl+Enter" in pg.get_attribute("#btnReview", "title") and "Ctrl+Enter" in pg.get_attribute("#btnReview", "aria-label")
        assert "Review last race" not in pg.evaluate("() => document.body.textContent")
        assert not errs, errs
    finally:
        ctx.close()


def test_button_and_ctrl_enter_open_sessions_with_the_newest_highlighted():
    r = window(make_api())
    if not r:
        return
    ctx, pg, errs, calls = r
    try:
        for how in ("button", "keys"):
            pg.click(".navbtn[data-nav=today]")
            pg.wait_for_function("() => !document.getElementById('vToday').hidden")
            if how == "button":
                pg.click("#btnReview")
            else:
                pg.keyboard.press("Control+Enter")
            pg.wait_for_function("() => !document.getElementById('vSessions').hidden && document.querySelectorAll('#sessList .item.sess').length === 3")
            st = newest_state(pg)
            assert st["newest"] == 1 and "Practice" in st["text"] and "Newest" in st["text"], (how, st)
            pg.wait_for_function("() => document.activeElement && document.activeElement.classList.contains('revbtn')")
            assert newest_state(pg)["focus"], how
            assert not posts(calls, "review"), "opening the page must not start a review"
        # the filter is reset so the newest is always on show
        pg.click("#sessFilter [data-f='3']")
        pg.click("#btnReview")
        pg.wait_for_function("() => document.querySelector('#sessFilter [data-f=all]').getAttribute('aria-checked') === 'true'")
        assert not errs, errs
    finally:
        ctx.close()


def test_every_row_has_one_review_button():
    r = window(make_api())
    if not r:
        return
    ctx, pg, errs, calls = r
    try:
        pg.click(".navbtn[data-nav=sessions]")
        pg.wait_for_function("() => document.querySelectorAll('#sessList .item.sess').length === 3")
        labels = pg.evaluate("() => [...document.querySelectorAll('#sessList .item.sess')].map(r => [...r.querySelectorAll('.revbtn')].map(b => b.textContent))")
        assert labels == [["Review"], ["Review"], ["Review"]], labels
        assert pg.evaluate("() => [...document.querySelectorAll('#sessList .revbtn')].every(b => b.offsetHeight >= 32 && b.getAttribute('aria-label'))")
    finally:
        ctx.close()


def test_race_row_review_opens_or_builds_the_race_review():
    r = window(make_api())
    if not r:
        return
    ctx, pg, errs, calls = r
    try:
        pg.click(".navbtn[data-nav=sessions]")
        pg.wait_for_function("() => document.querySelectorAll('#sessList .item.sess').length === 3")
        # a race with a saved review opens it at once, with no job
        pg.click("#sessList .item.sess >> nth=0 >> .revbtn")
        pg.wait_for_function("() => !document.getElementById('vFrame').hidden && document.getElementById('frame').src.includes('2026-11-01_zolder_gt4_aa11')")
        assert not posts(calls, "review")
        # a race with none is built from its row
        pg.click(".navbtn[data-nav=sessions]")
        pg.wait_for_function("() => !document.getElementById('vSessions').hidden && document.querySelectorAll('#sessList .item.sess').length === 3")
        pg.click("#sessList .item.sess >> nth=2 >> .revbtn")
        pg.wait_for_function("() => !document.getElementById('vJob').hidden")
        assert posts(calls, "review")[-1][3] == {"event": "E2", "session_type": 3, "track_id": 1, "car_id": 2}, posts(calls, "review")
        assert not errs, errs
    finally:
        ctx.close()


def test_practice_row_review_opens_practice():
    r = window(make_api())
    if not r:
        return
    ctx, pg, errs, calls = r
    try:
        pg.click("#btnReview")
        pg.wait_for_function("() => document.querySelectorAll('#sessList .item.sess').length === 3")
        pg.click("#sessList .item.newest .revbtn")
        pg.wait_for_function("() => !document.getElementById('vPractice').hidden")
        pg.wait_for_function("() => /stand-in/.test(document.getElementById('prReport').textContent)")
        assert any(c[1] == "session-review" and "id=pc-practice-1" in c[2] for c in calls), [c[1:3] for c in calls]
        assert not posts(calls, "review")
        assert not errs, errs
    finally:
        ctx.close()


def test_practice_row_never_opens_a_different_session():
    wrong = [x for x in IBT if x["id"] in ("pc-monza", "pc-zolder-race", "pc-other-car")]
    r = window(make_api(**{"sessions-ibt": {"ok": True, "folder_ok": True, "indexed": True, "sessions": wrong}}))
    if not r:
        return
    ctx, pg, errs, calls = r
    try:
        pg.click("#btnReview")
        pg.wait_for_function("() => document.querySelectorAll('#sessList .item.sess').length === 3")
        pg.click("#sessList .item.newest .revbtn")
        pg.wait_for_function("() => /No telemetry file on this PC matches/.test(document.getElementById('bannerText').textContent)")
        assert pg.evaluate("() => document.getElementById('vPractice').hidden")
        assert not any(c[1] == "session-review" for c in calls) and not posts(calls, "review")
        # the Garage 61 review is offered, not started
        assert pg.text_content("#bannerAct") == "Review from Garage 61"
        pg.click("#bannerAct")
        pg.wait_for_function("() => !document.getElementById('vJob').hidden")
        assert posts(calls, "review")[-1][3] == {"event": "E3", "session_type": 1, "track_id": 1, "car_id": 2}
        assert not errs, errs
    finally:
        ctx.close()


def recorded_race():
    return {"key": "rec:x#1", "event": None, "track_id": None, "car_id": None, "type": 3, "session": "Race", "track": "Zolder",
            "car": CAR, "start": START_P, "laps": 9, "best": 95.0, "best_clean": 95.5, "review": None, "source": "recorded",
            "recorded_only": True, "rec": {"id": "x#1", "line": "", "final": True}}


def test_recorded_only_race_row_is_built_from_that_race_on_this_pc():
    ibt = {"ok": True, "folder_ok": True, "indexed": True, "sessions": IBT + [
        {"id": "pc-race", "track": ZOLDER, "car": CAR, "sort": EPOCH_P + 60, "session_types": ["Race"], "subsession": 98765}]}
    r = window(make_api(sessions={"ok": True, "token": True, "summary": {"synced": True}, "sessions": [recorded_race()], "total": 1},
                        **{"sessions-ibt": ibt}))
    if not r:
        return
    ctx, pg, errs, calls = r
    try:
        pg.click("#btnReview")
        pg.wait_for_function("() => document.querySelectorAll('#sessList .revbtn').length === 1")
        pg.click("#sessList .revbtn")
        pg.wait_for_function("() => !document.getElementById('vJob').hidden")
        assert posts(calls, "review")[-1][3] == {"local": True, "subsession": 98765}, posts(calls, "review")
        assert not errs, errs
    finally:
        ctx.close()


def test_recorded_only_race_row_with_a_no_subsession_file_still_builds_that_race():
    ibt = {"ok": True, "folder_ok": True, "indexed": True, "sessions": IBT + [
        {"id": "pc-ai", "track": ZOLDER, "car": CAR, "sort": EPOCH_P + 60, "session_types": ["Race"], "subsession": None,
         "event_id": "irabc123"}]}
    r = window(make_api(sessions={"ok": True, "token": True, "summary": {"synced": True}, "sessions": [recorded_race()], "total": 1},
                        **{"sessions-ibt": ibt}))
    if not r:
        return
    ctx, pg, errs, calls = r
    try:
        pg.click("#btnReview")
        pg.wait_for_function("() => document.querySelectorAll('#sessList .revbtn').length === 1")
        pg.click("#sessList .revbtn")
        pg.wait_for_function("() => !document.getElementById('vJob').hidden")
        assert posts(calls, "review")[-1][3] == {"local": True, "pc_event": "irabc123"}, posts(calls, "review")
        assert not errs, errs
    finally:
        ctx.close()


def test_recorded_only_race_row_without_a_matching_file_says_so():
    none = {"ok": True, "folder_ok": True, "indexed": True, "sessions": [x for x in IBT if x["id"] in ("pc-monza", "pc-other-car", "pc-practice-1")]}
    r = window(make_api(sessions={"ok": True, "token": True, "summary": {"synced": True}, "sessions": [recorded_race()], "total": 1},
                        **{"sessions-ibt": none}))
    if not r:
        return
    ctx, pg, errs, calls = r
    try:
        pg.click("#btnReview")
        pg.wait_for_function("() => document.querySelectorAll('#sessList .revbtn').length === 1")
        pg.click("#sessList .revbtn")
        pg.wait_for_function("() => /Garage 61 doesn.t have this race/.test(document.getElementById('bannerText').textContent)")
        assert not posts(calls, "review")
        assert not errs, errs
    finally:
        ctx.close()


def test_a_new_garage_61_user_with_nothing_synced_builds_the_last_race_from_garage_61():
    steps = [STEPS[0]]          # no telemetry recording: no PC race to offer
    r = window(make_api(sessions={"ok": True, "token": True, "summary": {"synced": False}, "sessions": [], "total": 0},
                        firstrun=dict(FIRSTRUN, steps=steps, done=1, total=1)))
    if not r:
        return
    ctx, pg, errs, calls = r
    try:
        pg.click("#btnReview")
        pg.wait_for_function("() => !document.getElementById('vJob').hidden")
        assert not (posts(calls, "review")[-1][3] or {}), posts(calls, "review")
        assert "straight from Garage 61" in pg.evaluate("() => document.getElementById('bannerText').textContent")
        assert not errs, errs
    finally:
        ctx.close()


def test_a_synced_empty_list_stays_on_the_hint():
    r = window(make_api(sessions={"ok": True, "token": True, "summary": {"synced": True}, "sessions": [], "total": 0},
                        firstrun=dict(FIRSTRUN, steps=[STEPS[0]], done=1, total=1)))
    if not r:
        return
    ctx, pg, errs, calls = r
    try:
        pg.click("#btnReview")
        pg.wait_for_selector("#sessEmpty")
        pg.wait_for_timeout(300)
        assert not posts(calls, "review") and not pg.evaluate("() => document.getElementById('vSessions').hidden")
    finally:
        ctx.close()


def test_empty_list_says_what_to_do_in_one_line():
    r = window(make_api(sessions={"ok": True, "token": True, "summary": None, "sessions": [], "total": 0}, **{"last-race": {"ok": True, "local_newer": False}}))
    if not r:
        return
    ctx, pg, errs, calls = r
    try:
        pg.click("#btnReview")
        pg.wait_for_selector("#sessEmpty")
        t = pg.text_content("#sessEmpty p")
        assert "Drive with telemetry on" in t and "Garage 61" in t and t.count(".") == 2, t
        assert pg.text_content("#sessEmpty button") == "Sync now"
        pg.click("#sessEmpty button")
        assert posts(calls, "sync")
        assert not errs, errs
    finally:
        ctx.close()


def test_newest_race_on_this_pc_has_a_review_button():
    r = window(make_api(**{"last-race": {"ok": True, "saved": None, "instant": False, "local_newer": True, "newest_synced": None}}))
    if not r:
        return
    ctx, pg, errs, calls = r
    try:
        pg.click("#btnReview")
        pg.wait_for_selector("#sessPc:not([hidden]) #sessPcReview")
        assert "Newest race on this PC" in pg.text_content("#sessPc")
        pg.click("#sessPcReview")
        pg.wait_for_function("() => !document.getElementById('vJob').hidden")
        assert posts(calls, "review")[-1][3] == {"local": True}
        assert not errs, errs
    finally:
        ctx.close()


def test_no_token_still_reviews_the_newest_race_from_this_pc():
    s = make_api()["settings"] | {"token_saved": False, "token_hint": ""}
    steps = [{"id": "token", "done": False, "title": "Connect Garage 61", "text": "Paste.", "action": "settings_token"}, STEPS[1]]
    r = window(make_api(settings=s, reviews={"ok": True, "folder": "C:/lib", "groups": []},
                        firstrun=dict(FIRSTRUN, steps=steps, done=1, all_done=False, show=True)), ready="vWelcome")
    if not r:
        return
    ctx, pg, errs, calls = r
    try:
        pg.wait_for_function("() => document.getElementById('btnReview').getAttribute('aria-disabled') === 'false'")
        pg.click("#btnReview")
        pg.wait_for_function("() => !document.getElementById('vJob').hidden")
        assert posts(calls, "review")[-1][3] == {"local": True}
        assert not errs, errs
    finally:
        ctx.close()


def test_help_leads_with_review_a_session_and_explains_data_sources():
    r = window(make_api())
    if not r:
        return
    ctx, pg, errs, calls = r
    try:
        pg.keyboard.press("F1")
        pg.wait_for_function("() => document.getElementById('dlgHelp').open")
        pg.click("#dlgHelp [data-help=start]")
        first = pg.evaluate("() => { const b = document.getElementById('helpBody'); const e = b.querySelector('header').nextElementSibling; "
                            "return {cls: e.className, h: e.querySelector('h3').textContent, text: e.textContent, "
                            "go: [...e.querySelectorAll('[data-nav]')].map(x => x.dataset.nav)} }")
        assert "hbox" in first["cls"] and first["h"] == "Review a session", first
        t = first["text"]
        order = [t.index(w) for w in ("Get out of the car", "Press Review a session (Ctrl+Enter)", "Pick the session", "What to work on",
                                      "Show the numbers", "One thing this session", "Add comparison laps")]
        assert order == sorted(order), order
        assert "Coach this" in t and "Without Garage 61 there is no list" in t and "newest race from this PC" in t
        assert "sessions" in first["go"] and "practice" in first["go"] and "settings" in first["go"], first
        body = pg.text_content("#helpBody")
        assert "Review last race" not in body
        assert "when to connect" in body.lower() or "Connect it when you want to compare" in body
        # the data-sources section
        pg.click("#dlgHelp [data-help=data]")
        pg.wait_for_function("() => document.querySelector('#helpBody h2').textContent === 'Where your data comes from'")
        rows = pg.evaluate("() => [...document.querySelectorAll('#helpBody table tbody tr')].map(r => [...r.children].map(c => c.textContent))")
        assert len(rows) == 7, rows
        flat = " | ".join(" / ".join(r) for r in rows)
        for want in ("Your race review, result, corners, setup", "Fuel", "the telemetry file when there is no recording", "Your PC", "Settings > Live data", "Garage 61", "Team pages and leaderboards"):
            assert want in flat, (want, flat)
        assert "When to connect Garage 61" in pg.text_content("#helpBody")
        assert "The result and fuel on a review say where their numbers came from" in pg.text_content("#helpBody")
        assert "Each section of a review" not in pg.text_content("#helpBody")
        # the Help search finds it
        pg.fill("#helpSearch", "leaderboards")
        pg.wait_for_function("() => document.querySelectorAll('#helpBody .hresult').length > 0")
        assert not errs, errs
    finally:
        ctx.close()


def open_result_form(pg):
    pg.click(".navbtn[data-nav=sessions]")
    pg.wait_for_function("() => document.querySelectorAll('#sessList .item.sess').length === 3")
    pg.click("#sessList .item.sess >> nth=0 >> .revbtn")
    pg.wait_for_function("() => !document.getElementById('vFrame').hidden && !document.getElementById('fbResult').hidden")
    pg.click("#fbResult")
    pg.wait_for_function("() => document.getElementById('dlgResult').open")


CSV_REPLY = {"ok": True, "matched": {"name": "Sam", "class": "", "fields": {"finish_pos": 3, "field_size": 20, "start_pos": 5}}}


def load_csv(pg):
    pg.set_input_files("#resCsv", {"name": "results.csv", "mimeType": "text/csv", "buffer": b"x"})
    pg.wait_for_function("() => /Filled in for Sam/.test(document.getElementById('resCsvMsg').textContent)")


def saved_fields(calls):
    return posts(calls, "result")[-1][3]["fields"]


def test_result_from_a_results_file_is_saved_as_results_file_until_edited():
    r = window(make_api(**{"result-csv": CSV_REPLY, "result": {"ok": True, "result": {}}}))
    if not r:
        return
    ctx, pg, errs, calls = r
    try:
        open_result_form(pg)
        load_csv(pg)
        pg.click("#resSave")
        pg.wait_for_function("() => !document.getElementById('dlgResult').open")
        f = saved_fields(calls)
        assert f["source"] == "results_file" and f["finish_pos"] == 3, f
        # the same file, then one number changed by hand: it is yours now
        open_result_form(pg)
        load_csv(pg)
        pg.fill("#r_finish_pos", "4")
        pg.click("#resSave")
        pg.wait_for_function("() => !document.getElementById('dlgResult').open")
        assert saved_fields(calls)["source"] == "you", saved_fields(calls)
        # typed by hand from scratch: yours
        open_result_form(pg)
        pg.fill("#r_finish_pos", "6")
        pg.click("#resSave")
        pg.wait_for_function("() => !document.getElementById('dlgResult').open")
        assert saved_fields(calls)["source"] == "you"
        assert not errs, errs
    finally:
        ctx.close()


def test_unchanged_replay_scan_prefill_is_saved_as_scan_not_you():
    r = window(make_api(result={"ok": True, "result": None, "scan": {"finish": 5, "cars": 20}}))
    if not r:
        return
    ctx, pg, errs, calls = r
    try:
        open_result_form(pg)
        assert pg.input_value("#r_finish_pos") == "5"
        pg.click("#resSave")
        pg.wait_for_function("() => !document.getElementById('dlgResult').open")
        assert saved_fields(calls)["source"] == "scan", saved_fields(calls)
        open_result_form(pg)
        pg.fill("#r_finish_pos", "6")
        pg.click("#resSave")
        pg.wait_for_function("() => !document.getElementById('dlgResult').open")
        assert saved_fields(calls)["source"] == "you"
        assert not errs, errs
    finally:
        ctx.close()


def test_resaving_an_unchanged_result_keeps_its_source_and_overall_numbers():
    old = {"finish_pos": 2, "field_size": 10, "source": "iracing", "overall_pos": 4, "overall_size": 30}
    r = window(make_api(result={"ok": True, "result": old, "scan": None}))
    if not r:
        return
    ctx, pg, errs, calls = r
    try:
        open_result_form(pg)
        pg.click("#resSave")
        pg.wait_for_function("() => !document.getElementById('dlgResult').open")
        f = saved_fields(calls)
        assert f["source"] == "iracing" and f["overall_pos"] == 4 and f["overall_size"] == 30, f
        open_result_form(pg)
        pg.fill("#r_finish_pos", "3")
        pg.click("#resSave")
        pg.wait_for_function("() => !document.getElementById('dlgResult').open")
        f = saved_fields(calls)
        assert f["source"] == "you" and "overall_pos" not in f, f
        assert not errs, errs
    finally:
        ctx.close()


def test_every_other_review_last_race_is_gone_from_the_window():
    for f in ("index.html", "app.js", "help.js"):
        txt = (UI / f).read_text(encoding="utf-8")
        assert "Review last race" not in txt, f


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
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
