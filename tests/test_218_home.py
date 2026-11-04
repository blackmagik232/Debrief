"""Debrief 2.18 lane C: home, navigation and polish.

    python3 tests/test_218_home.py

- Today's "Your #1 thing right now": which answer it picks (debrief/today.py) and api/today (debrief/desktop/home.py)
- one sureness scale (Sure / Likely / A hint / A guess) mapped from every page's own words
- the sidebar: grouped, Ctrl+number in its order (Racing and Car vs car have keys), F1 / ? show the keys, Ctrl+R
  no longer starts a review (Ctrl+Enter does), and it fits and scrolls at 1093x614, 1280x720 and 1366x768
- debug text (exception names, request counts, the sample-lap file, event ids, "? → ?") said plainly, with the
  originals under a details fold
- a returning user sees no "Getting set up" box and no "New here?" toast
The window checks run the real index.html / app.js / app.css in Chromium with every /api/ call answered here
(no server). They're skipped when Playwright or its Chromium isn't installed.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
UI = ROOT / "debrief" / "desktop" / "ui"

from debrief import today  # noqa: E402

PLAN = {"ok": True, "car_path": "mclaren570sgt4", "track_code": "zolder gp", "car": "McLaren 570S GT4",
        "track": "Zolder", "plan": {"kind": "focus", "text": "T6: carry about 101 km/h at the slowest point",
                                    "how": "Come off the brake a little earlier", "check": "Slowest speed in T6 99 km/h or more",
                                    "confidence": "clear", "from_laps": 40}}
FIX_REVIEW = {"dir": "2026-11-01_spa", "track": "Spa", "car": "Porsche 911 GT3 R", "date": "2026-11-01",
              "started_at": "2026-11-01T19:00:00Z", "session": "Race", "best": "2:18.000",
              "verdict": "The quickest gain is matching your better laps through C3.",
              "fix": {"corner": "C3", "gain": 0.31, "why": "Your typical lap loses 0.31 s here.", "tip": "Brake 5 m later"}}
PRACTICE = {"id": "a|b|1", "track": "Zolder", "car": "McLaren 570S GT4", "date": "2026-10-30T18:00:00", "best": 94.9,
            "clean_laps": 11, "laps": 13}


# ---------------------------------------------------------------- the #1 thing
def test_plan_is_the_one_thing():
    c = today.pick(plan=PLAN, review=FIX_REVIEW, practice=dict(PRACTICE, date="2026-11-02T18:00:00"))
    assert c["source"] == "plan" and c["title"].startswith("Turn 6: carry"), c
    assert c["sureness"] == "sure" and c["action"]["go"] == "coach" and c["action"]["car_path"] == "mclaren570sgt4", c
    assert ["The check", "Slowest speed in Turn 6 99 km/h or more"] in c["lines"], c["lines"]
    assert "Practice page shows the same one thing" in c["why"]


def test_a_newer_race_elsewhere_wins_over_the_plan():
    c = today.pick(plan=PLAN, review=FIX_REVIEW, practice=PRACTICE)        # practice Oct 30, race at Spa Nov 1
    assert c["source"] == "review" and c["title"] == "C3: Brake 5 m later", c
    assert c["action"] == {"label": "Open the review", "go": "review", "dir": "2026-11-01_spa"}
    # the same track as the plan: the plan stays (the pages agree)
    same = dict(FIX_REVIEW, track="Zolder")
    assert today.pick(plan=PLAN, review=same, practice=PRACTICE)["source"] == "plan"


def test_review_then_racing_then_plan_step_then_setup():
    assert today.pick(review=FIX_REVIEW)["source"] == "review"
    habit = today.racing_habit({"tracks": [{"track": "Zolder", "corners": [
        {"label": "Turn 3", "pattern": "gain", "n": 4}, {"label": "the start to Turn 1", "pattern": "lose", "n": 5}],
        "bands": [{"label": "all races", "pattern": None, "n": 6}]}]})
    assert habit == {"track": "Zolder", "where": "the start to Turn 1", "pattern": "lose", "races": 5, "corner": True}, habit
    c = today.pick(review=dict(FIX_REVIEW, fix=None), racing=habit)
    assert c["source"] == "racing" and c["title"] == "Lap 1: you usually lose places between the start and Turn 1.", c
    assert c["sureness"] == "likely" and c["action"]["go"] == "racing"
    step = {"ok": True, "plan": {"kind": "laps", "text": "Drive 5 clean laps first.", "check": "5 clean laps"}}
    assert today.pick(plan=step)["source"] == "plan" and today.pick(plan=step)["action"]["go"] == "practice"
    setup = {"steps": [{"id": "token", "done": True, "title": "Connect Garage 61"},
                       {"id": "voice", "done": False, "title": "Test the coach's voice", "text": "Click Test voice.",
                        "action": "test_voice"}]}
    c = today.pick(setup=setup)
    assert c["source"] == "setup" and c["title"] == "Test the coach's voice" and c["action"]["step"] == "test_voice", c
    assert today.pick()["source"] == "none"
    # odd inputs count as missing
    assert today.pick(plan="x", review=[1], racing=3, practice=None, setup={"steps": "no"})["source"] == "none"


def test_last_race_and_practice_cards():
    row = {"dir": "d1", "track": "Spa", "car": "GT3", "date": "2026-11-01", "started_at": "", "session": "Race", "best": "2:18.000"}
    data = {"insights": {"verdict": "Go faster in C3.", "fix": [{"corner": "C3", "gain": 0.2, "why": "w", "tips": ["Brake later", "x"]}]}}
    r = today.last_race(row, data)
    assert r["verdict"] == "Go faster in C3." and r["fix"] == {"corner": "C3", "gain": 0.2, "why": "w", "tip": "Brake later"}, r
    assert today.last_race(row, None)["fix"] is None and today.last_race({}, data) is None
    assert today.last_practice(dict(PRACTICE, paths=["C:/x.ibt"])) and "paths" not in today.last_practice(dict(PRACTICE, paths=[]))


def test_one_sureness_scale():
    assert today.SURE_WORDS == {"sure": "Sure", "likely": "Likely", "hint": "A hint", "guess": "A guess"}
    cases = [("corner", "clear", "sure"), ("corner", "likely", "likely"), ("corner", "thin", "hint"), ("corner", "unclear", "guess"),
             ("setup", "reading", "sure"), ("setup", "low-medium", "hint"), ("setup", "guess", "guess"),
             ("idea", "worth a try", "hint"), ("idea", "a guess", "guess"),
             ("cars", "unlikely to be chance", "likely"), ("cars", "likely", "hint"), ("cars", "not enough to tell", None),
             ("racing", "lose", "likely"), ("nope", "clear", None)]
    for kind, v, want in cases:
        assert today.sureness(kind, v) == want, (kind, v)
    # the window uses the same words for the same levels
    js = (UI / "app.js").read_text(encoding="utf-8")
    assert "const SURE={sure:['Sure','ok'],likely:['Likely',''],hint:['A hint','no'],guess:['A guess','no']};" in js
    for old in ("'Clear pattern'", "'Few laps'", "'Experiment'", "'From a reading'", "'Worth a try','ok'", "'Low to medium'"):
        assert old not in js, old
    assert "clear:['Sure','ok'" in js and "thin:['A hint','no'" in js and "unclear:['A guess','no'" in js
    assert "reading:['Sure','ok'" in js and "'worth a try':['A hint','no'" in js and "return['Likely',' ok']" in js


def test_setup_done_rule():
    assert not today.setup_done(None)
    assert today.setup_done({"all_done": True})
    assert today.setup_done({"steps": [{"id": "token", "done": True}, {"id": "voice", "done": False}]})
    assert today.setup_done({"steps": [{"id": "session", "done": True}]})
    assert not today.setup_done({"steps": [{"id": "folder", "done": True}, {"id": "telemetry", "done": True}]})


def test_api_today_from_the_server_parts(tmp_path=None):
    """home.get_today with a stand-in handler: every part used, and a part that fails is left out."""
    import tempfile
    from debrief.desktop import home
    tmp = Path(tmp_path or tempfile.mkdtemp())
    rv = tmp / "reviews" / "2026-11-01_spa"
    rv.mkdir(parents=True)
    (rv / "review.json").write_text(json.dumps({"insights": {"verdict": "V.", "fix": [{"corner": "C3", "gain": 0.3, "tips": ["Brake later"]}]}}),
                                    encoding="utf-8")
    sent = {}

    class Srv:
        class reviews:   # noqa: N801
            @staticmethod
            def list(lib):
                return [{"track": "Spa", "car": "GT3", "reviews": [dict(FIX_REVIEW, dir="2026-11-01_spa")]}]

        @staticmethod
        def practice_sessions(cfg):
            raise RuntimeError("telemetry index broken")

    class H:
        server = Srv

        def _cfg(self):
            return {"cache_dir": str(tmp / "cache")}

        def _library_folder(self):
            return tmp

        def _plan_reply(self, cfg, q):
            raise AssertionError("no plan without practice")

        def _json(self, obj, code=200):
            sent.update(obj)

    import debrief.app as A
    said = []
    old = A.say
    A.say = said.append
    try:
        home.get_today(H(), {})
    finally:
        A.say = old
    assert sent["ok"] and sent["one"]["source"] == "review" and sent["one"]["title"] == "C3: Brake later", sent
    assert sent["last_race"]["dir"] == "2026-11-01_spa" and sent["last_practice"] is None and sent["has_data"], sent
    assert any("practice part was left out" in s for s in said), said
    try:
        home.get_today(H(), {"x": "1"})
        raise AssertionError("an unknown parameter is refused")
    except ValueError:
        pass


# ---------------------------------------------------------------- the sidebar, statically
def test_sidebar_groups_and_keys_in_order():
    html = (UI / "index.html").read_text(encoding="utf-8")
    nav = html[html.index('<nav class="nav"'):html.index("</nav>")]
    pages = re.findall(r'<button type="button" class="navbtn" data-nav="([a-z]+)"[^>]*aria-keyshortcuts="Control\+(\d)"', nav)
    assert [p for p, _ in pages] == ["today", "prep", "coach", "sessions", "practice", "racing", "setuplab", "cars", "library", "team"], pages
    assert [k for _, k in pages] == list("1234567890"), pages
    subs = re.findall(r'class="navbtn sub" data-nav="([a-z]+)"', nav)
    assert subs == ["season", "skills", "goals"], subs
    groups = re.findall(r'<span class="navgroup"[^>]*>([^<]+)</span>', nav)
    assert groups == ["Before you drive", "After you drive", "Car", "Over time", "Team"], groups
    # Settings, Help and the theme stay outside the part that scrolls
    foot = html[html.index('<div class="sidefoot">'):html.index("</aside>")]
    assert 'data-nav="settings"' in foot and 'id="btnHelp"' in foot and 'data-theme-set="dark"' in foot
    assert html.index('id="btnReview"') < html.index('id="sideScroll"') < html.index('id="revlist"') < html.index('class="sidefoot"')
    assert "Ctrl+R" not in html and 'title="Review last race (Ctrl+Enter)"' in html
    js = (UI / "app.js").read_text(encoding="utf-8")
    assert "const PAGE_KEYS=[...document.querySelectorAll('.nav .navbtn[data-nav]:not(.sub)')]" in js
    from debrief.desktop import server as S
    assert "m='reviewgo'" in S.FRAME_BRIDGE and "m='help'" in S.FRAME_BRIDGE


def test_library_page_heading_names_its_tab():
    lib = (ROOT / "debrief" / "templates" / "library.html").read_text(encoding="utf-8")
    assert "const TAB_NAMES={overview:'Progress',season:'Season',skills:'Skills',goals:'Goals'};" in lib
    assert "function showTab(name){name=tabOf(name);tabHead(name);" in lib


# ---------------------------------------------------------------- window (browser, mocked API)
_PW = {}
BASE = "http://debrief.test/s3cret/"


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


GROUPS = [{"track": "Zolder", "car": "McLaren 570S GT4", "reviews": [
    {"dir": f"2026-10-{d:02d}_z", "date": f"2026-10-{d:02d}", "started_at": f"2026-10-{d:02d}T19:00:00Z", "best": "1:35.000",
     "session": "Race", "laps": 12} for d in range(28, 8, -1)]},
    {"track": "Spa", "car": "Porsche 911 GT3 R", "reviews": [
        {"dir": f"2026-09-{d:02d}_s", "date": f"2026-09-{d:02d}", "started_at": f"2026-09-{d:02d}T19:00:00Z", "best": "2:18.000",
         "session": "Race", "laps": 9} for d in range(28, 10, -2)]}]
FIRSTRUN = {"ok": True, "steps": [{"id": "token", "done": True, "title": "Connect Garage 61", "text": "Saved.", "action": None},
                                  {"id": "folder", "done": True, "title": "Find your iRacing telemetry", "text": "Found.", "action": None},
                                  {"id": "telemetry", "done": True, "title": "Read your telemetry", "text": "Read.", "action": None},
                                  {"id": "session", "done": True, "title": "Drive 5 clean laps in one session", "text": "Yes.", "action": None},
                                  {"id": "voice", "done": False, "title": "Test the coach's voice", "text": "Click Test voice.", "action": "test_voice"}],
            "done": 4, "total": 5, "next": "voice", "all_done": False, "dismissed": False, "show": True}


def _api(**over):
    one = today.pick(plan=PLAN, practice=dict(PRACTICE, date="2026-11-02T18:00:00"))
    a = {"settings": {"ok": True, "token_saved": True, "token_hint": "…abcd", "version": "2.18.0", "installed": True,
                      "first_name": "Sam", "data_folder": "C:/x", "team_slug": "midfield-menace", "max_reference_laps": 2},
         "brand": {"app_name": "Debrief", "team_slug": "midfield-menace", "team_name": "Midfield Menace"},
         "reviews": {"ok": True, "folder": "C:/lib", "groups": GROUPS},
         "status": {"state": "idle"}, "sync": {"ok": True, "skipped": True}, "update": {"ok": True, "skipped": True},
         "pages": {"ok": True, "prep": []}, "firstrun": FIRSTRUN, "review": {"ok": False, "error": "Garage 61 is busy."},
         "today": {"ok": True, "one": one, "last_race": today.last_race(GROUPS[0]["reviews"][0] | {"track": "Zolder", "car": "McLaren 570S GT4"},
                                                                         {"insights": {"verdict": "Your laps are steady."}}),
                   "last_practice": dict(PRACTICE, date="2026-11-02T18:00:00"), "reviews": 30, "sessions": 4,
                   "setup": FIRSTRUN, "setup_done": True, "has_data": True}}
    a.update(over)
    return a


def _window(api, viewport=(1280, 860), fresh_storage=True):
    b = _browser()
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
        if path in ("static/app.js", "static/app.css"):
            return route.fulfill(body=(UI / path[7:]).read_text(encoding="utf-8"),
                                 content_type="text/javascript" if path.endswith(".js") else "text/css")
        if path.startswith("lib/"):
            return route.fulfill(body="<!doctype html><title>Page</title><body>page</body>", content_type="text/html")
        if path.startswith("api/"):
            name = path[4:]
            calls.append((req.method, name, qs))
            v = api.get(name, {"ok": True})
            return route.fulfill(body=json.dumps(v), content_type="application/json")
        return route.fulfill(status=404, body="")
    pg.route("http://debrief.test/**", handle)
    pg.route("**/fonts.g*/**", lambda r: r.abort())
    pg.goto(BASE)
    return ctx, pg, errs, calls


def _visible_view(pg):
    return pg.evaluate("() => [...document.querySelectorAll('main > section')].filter(s => !s.hidden).map(s => s.id)")


def test_window_today_keys_and_returning_user():
    r = _window(_api())
    if not r:
        return
    ctx, pg, errs, calls = r
    try:
        pg.wait_for_function("() => !document.getElementById('vToday').hidden && document.getElementById('tdTitle').textContent.startsWith('Turn 6')")
        assert pg.text_content("#vToday h1") == "Today" and pg.text_content("#tdSure") == "Sure"
        assert pg.text_content("#tdEyebrow") == "Welcome back, Sam"
        assert pg.get_attribute(".navbtn[data-nav=today]", "aria-current") == "page"
        assert "Zolder · McLaren 570S GT4" in pg.text_content("#tdRace") and "Your laps are steady." in pg.text_content("#tdRace")
        assert "Best 1:34.900" in pg.text_content("#tdPrac") and "11 clean laps" in pg.text_content("#tdPrac")
        # a returning user: no "Getting set up" box and no "New here?" toast; the step left is one quiet line
        pg.wait_for_function("() => !document.getElementById('tdSetupLine').hidden")
        assert pg.evaluate("() => document.getElementById('firstRun').hidden")
        assert "Test the coach's voice" in pg.text_content("#tdSetupLine")
        pg.wait_for_timeout(300)
        assert "New here" not in (pg.text_content("#bannerText") or "")
        # the keys follow the sidebar
        keys = pg.evaluate("() => window.debriefUi.pageKeys()")
        side = pg.evaluate("() => [...document.querySelectorAll('.nav .navbtn:not(.sub)')].map(b => b.dataset.nav)")
        assert keys == side[:10], (keys, side)
        for key, view in (("6", "vRacing"), ("8", "vCars"), ("4", "vSessions"), ("7", "vSetupLab"), ("1", "vToday")):
            pg.keyboard.press("Control+" + key)
            pg.wait_for_function(f"() => !document.getElementById('{view}').hidden")
        # F1 and ? show the key list
        pg.keyboard.press("F1")
        pg.wait_for_function("() => document.getElementById('dlgHelp').open")
        txt = pg.text_content("#helpBody")
        for want in ("Ctrl+Enter: Review last race", "Ctrl+1: Today", "Ctrl+6: Racing", "Ctrl+8: Car vs car", "Ctrl+0: Team",
                     "F1 or ?: This list of keys", "Ctrl+Shift+M"):
            assert want in txt, (want, txt)
        pg.keyboard.press("Escape")
        pg.wait_for_function("() => !document.getElementById('dlgHelp').open")
        pg.keyboard.press("Shift+Slash")
        pg.wait_for_function("() => document.getElementById('dlgHelp').open && document.querySelector('[data-help=keys]').getAttribute('aria-pressed') === 'true'")
        pg.keyboard.press("Escape")
        # Ctrl+R starts nothing (and doesn't reload): it says where Review last race went; Ctrl+Enter starts it
        pg.evaluate("() => { window.__alive = 1 }")
        pg.keyboard.press("Control+r")
        pg.wait_for_function("() => /Ctrl\\+Enter/.test(document.getElementById('bannerText').textContent)")
        assert pg.evaluate("() => window.__alive") == 1
        assert not [c for c in calls if c[0] == "POST" and c[1] == "review"]
        pg.keyboard.press("Control+Enter")
        pg.wait_for_function("() => document.getElementById('bannerText').textContent === 'Garage 61 is busy.'")
        assert [c for c in calls if c[0] == "POST" and c[1] == "review"]
        # Season and Skills are under Progress; their own sidebar entries light up
        pg.click(".navbtn[data-nav=skills]")
        pg.wait_for_function("() => document.querySelector('.navbtn[data-nav=skills]').getAttribute('aria-current') === 'page'")
        assert not errs, errs
    finally:
        ctx.close()


def test_window_plain_text():
    r = _window(_api())
    if not r:
        return
    ctx, pg, errs, _calls = r
    try:
        pg.wait_for_function("() => window.debriefUi")
        P = lambda t: pg.evaluate("t => window.debriefUi.plainText(t)", t)  # noqa: E731
        assert P("broken 2026-10-01 10-00-00.ibt: The telemetry file couldn't be read (IbtError).") == \
            "broken 2026-10-01 10-00-00.ibt: The telemetry file couldn't be read."
        assert P("The start report couldn't be worked out (KeyError).") == "The start report couldn't be worked out."
        assert P("speaking failed (pywintypes.com_error)") == "speaking failed (pywintypes.com_error)"   # not a Python class name pattern
        assert P("Requests used: 5") == "" and P("Saved one sample lap record to C:/x/sample_lap.json (handy...)") == ""
        assert P("Library folder: C:\\Users\\me\\Documents\\Debrief Reviews") == ""
        assert P("Loading event EV0123...") == "Loading your race..."
        assert P("Weekly leaderboards ready. Garage 61 requests used: 12.") == "Weekly leaderboards ready."
        assert P("Token works. Account: Sam") == "Token works. Account: Sam"
        L = pg.evaluate("() => window.debriefUi.plainLines(['Token works.', 'Requests used: 6'])")
        assert L["shown"] == ["Token works."] and L["changed"] == ["Token works.", "Requests used: 6"], L
        assert pg.evaluate("() => window.debriefUi.plainLines(['Token works.']).changed") is None
        N = pg.evaluate("() => window.debriefUi.fileNotes(['a.ibt: The telemetry file couldn\\'t be read (IbtError).', 'Some replay scans couldn\\'t be read.'])")
        assert N == {"notes": ["Some replay scans couldn't be read."], "files": ["a.ibt: The telemetry file couldn't be read (IbtError)."]}, N
        assert pg.evaluate("() => window.debriefUi.sure('hint')") == "A hint"
        assert not errs, errs
    finally:
        ctx.close()


def test_window_check_lines_fold_the_debug_text():
    api = _api(check={"ok": True, "id": 7})
    r = _window(api)
    if not r:
        return
    ctx, pg, errs, _calls = r
    try:
        pg.wait_for_function("() => !document.getElementById('vToday').hidden")
        api["status"] = {"state": "done", "kind": "check", "id": 7, "result": {"lines": [
            "Token works. Account: Sam", "Saved one sample lap record to C:/x/cache/sample_lap.json (handy if something needs fixing).",
            "Library folder: C:/lib", "Requests used: 5"]}}
        pg.keyboard.press("Control+Comma")
        pg.wait_for_function("() => !document.getElementById('vSettings').hidden")
        pg.click("#btnCheck")
        pg.wait_for_function("() => !document.getElementById('chkOut').hidden")
        shown = pg.text_content("#chkOut")
        assert "Token works" in shown and "sample lap" not in shown and "Requests used" not in shown and "C:/lib" not in shown, shown
        assert pg.evaluate("() => document.getElementById('chkDbg').tagName") == "DETAILS"
        assert "Requests used: 5" in pg.text_content("#chkDbg")
        assert not errs, errs
    finally:
        ctx.close()


def test_window_sidebar_fits_small_windows():
    for vp in ((1093, 614), (1280, 720), (1366, 768)):
        r = _window(_api(), viewport=vp)
        if not r:
            return
        ctx, pg, errs, _calls = r
        try:
            pg.wait_for_function("() => !document.getElementById('vToday').hidden")
            bad = pg.evaluate("""() => { const H = innerHeight, out = [];
              for (const sel of ['#btnReview', '.footnav [data-nav=settings]', '#btnHelp', '.sidefoot .seg', '.brand']) {
                const e = document.querySelector(sel), r = e.getBoundingClientRect();
                if (r.top < 0 || r.bottom > H || r.height < 20 || e.scrollHeight > e.clientHeight + 2) out.push(sel + ' ' + r.top + '-' + r.bottom) }
              const lbl = document.querySelector('#btnReview .lbl').getBoundingClientRect(), b = document.getElementById('btnReview').getBoundingClientRect();
              if (lbl.bottom > b.bottom || lbl.top < b.top) out.push('label clipped');
              return out }""")
            assert not bad, (vp, bad)
            # the page list and saved reviews scroll together; the window itself never scrolls
            sc = pg.evaluate("() => { const s = document.getElementById('sideScroll'); return [s.scrollHeight > s.clientHeight, getComputedStyle(s).overflowY] }")
            assert sc == [True, "auto"], (vp, sc)
            pg.evaluate("() => { const s = document.getElementById('sideScroll'); s.scrollTop = s.scrollHeight }")
            last = pg.evaluate("() => { const r = [...document.querySelectorAll('#revlist .rev')].pop().getBoundingClientRect(); return [r.bottom, document.querySelector('.sidefoot').getBoundingClientRect().top] }")
            assert last[0] <= last[1] + 1, (vp, last)
            assert pg.evaluate("() => [getComputedStyle(document.body).overflow, getComputedStyle(document.documentElement).overflow]") == ["clip", "clip"]
            assert not errs, errs
        finally:
            ctx.close()


def test_window_before_any_data_shows_the_setup_steps():
    fr = dict(FIRSTRUN, steps=[dict(s, done=s["id"] == "token") for s in FIRSTRUN["steps"]], done=1, next="folder")
    fr["steps"][1]["action"] = "settings_folder"
    td = {"ok": True, "one": today.pick(setup=fr), "last_race": None, "last_practice": None, "reviews": 0, "sessions": 0,
          "setup": fr, "setup_done": today.setup_done(fr), "has_data": False}
    assert td["setup_done"] is True          # the token is saved, but with no data yet the steps are still the page
    r = _window(_api(today=td, firstrun=fr, reviews={"ok": True, "folder": "C:/lib", "groups": []},
                     settings={"ok": True, "token_saved": False, "version": "2.18.0"}))
    if not r:
        return
    ctx, pg, errs, _calls = r
    try:
        # no token and no reviews: the welcome steps (onboarding); with a token, Today is the setup steps
        pg.wait_for_function("() => !document.getElementById('vWelcome').hidden")
    finally:
        ctx.close()
    r = _window(_api(today=td, firstrun=fr, reviews={"ok": True, "folder": "C:/lib", "groups": []}))
    ctx, pg, errs, _calls = r
    try:
        pg.wait_for_function("() => !document.getElementById('vToday').hidden && !document.getElementById('firstRun').hidden")
        assert pg.evaluate("() => document.getElementById('firstRun').closest('#vToday') !== null")
        assert pg.evaluate("() => document.getElementById('tdOne').hidden && document.querySelector('#vToday .tdgrid').hidden")
        assert pg.text_content("#frPill") == "1 of 5 done" and pg.evaluate("() => document.getElementById('tdSetupLine').hidden")
        assert "A few steps" in pg.text_content("#tdLede")
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
