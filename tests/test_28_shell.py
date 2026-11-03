"""Debrief 2.8 window (shell) polish: shared tokens, the sidebar owning navigation, the loading bar, request
counts only in Settings > About, the sample review, the remembered window size, the setup gate, keys, sync dot.

    python3 tests/test_28_shell.py

Windows test rules: no SIGALRM, no fixed temp paths, utf-8 opens, CRLF normalised, Playwright waits poll for
state. No network: the server runs on 127.0.0.1 with a throwaway settings file and Garage 61 is never reached.
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
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="debrief-28-"))
os.environ["DEBRIEF_SETTINGS"] = str(TMP / "settings.toml")
os.environ["DEBRIEF_CACHE_DIR"] = str(TMP / "cache")
os.environ["DEBRIEF_SCHEDULE_DIR"] = str(TMP / "schedule")
os.environ["DEBRIEF_UPDATE_STATE"] = str(TMP / "update.json")

from debrief import app, engine, library, report  # noqa: E402
from debrief import desktop as D  # noqa: E402
from debrief.desktop import server as S  # noqa: E402
from debrief.desktop import settings_io  # noqa: E402

UI = ROOT / "debrief" / "desktop" / "ui"
TPL = ROOT / "debrief" / "templates"
LIB = TMP / "lib"
ENV, PW = {}, {}


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8").replace("\r\n", "\n")


def settings(token=True):
    LIB.mkdir(parents=True, exist_ok=True)
    line = 'token = "g61-test-token-ABCDEFGHIJKLMNOP"\n' if token else ""
    Path(os.environ["DEBRIEF_SETTINGS"]).write_text(
        f"{line}library_folder = '{LIB}'\napi_base = \"http://127.0.0.1:9/api/v1/\"\n", encoding="utf-8")


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
    if data:
        h["Content-Type"] = "application/json"
    rq = urllib.request.Request(s.url + rel, headers=h, data=data, method="POST" if data else "GET")
    try:
        with ENV["opener"].open(rq, timeout=30) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def tokens(html: str) -> str:
    css = html.split("<style>", 1)[1]
    return re.sub(r"/\*.*?\*/", "", css[:css.index("*{box-sizing")], flags=re.S).strip()


# ================================================================ tokens
def test_shell_and_pages_share_one_set_of_tokens():
    sets = {n: tokens(read(TPL / f"{n}.html")) for n in ("library", "prep", "team", "review")}
    assert len(set(sets.values())) == 1, "the page templates' tokens have drifted apart"
    served = re.sub(r"/\*.*?\*/", "", S.design_tokens(), flags=re.S).strip()
    assert served == sets["library"]
    # the fallback looks the same too, and app.css only uses tokens the sheet defines
    assert S.FALLBACK_TOKENS.split() == sets["library"].split()
    defined = set(re.findall(r"(--[a-z0-9-]+)\s*:", served))
    used = set(re.findall(r"var\((--[a-z0-9-]+)", read(UI / "app.css")))
    assert used <= defined, sorted(used - defined)
    assert 'data-theme="dark"' in served and "prefers-color-scheme: dark" in served


# ================================================================ request counts
def test_request_counts_only_in_settings_about():
    html = read(UI / "index.html")
    about = re.search(r'<p class="note" id="aboutRequests">.*?</p>', html, re.S)
    assert about and "about six requests" in about.group(0)
    rest = html.replace(about.group(0), "")
    for fine in ("and request a personal access token", "looks at each request by hand", "issues on request to individual"):
        rest = rest.replace(fine, "")    # asking for a token, not a count of Garage 61 requests
    assert not re.search(r"\brequests?\b", rest), re.findall(r".{40}\brequests?\b.{20}", rest)
    js = read(UI / "app.js")
    shown = [m for m in re.findall(r"'[^'\n]*\brequests?\b[^'\n]*'", js) if "replace(/" not in m and "share-request" not in m]
    assert shown == ["' request'"], shown        # only "Your last history sync used N request(s)." in About
    assert "aboutLastSync" in js and "'Your last history sync used '" in js


# ================================================================ bridge
def test_bridge_adds_shell_class_and_keys():
    out = S.with_bridge(b"<html><head><title>x</title></head><body>hi</body></html>")
    assert out.index(b"debrief-in-shell") < out.index(b"</head>") < out.index(b"debrief-frame-bridge") < out.rindex(b"</body>")
    assert b".in-shell .tabs{display:none" in out and b"'in-shell'" in out
    assert S.with_bridge(b"no head at all").startswith(b"no head at all")
    # the bridge's keys never fire while typing
    assert "closest('input,select,textarea,[contenteditable]')" in S.FRAME_BRIDGE
    assert "(e.ctrlKey||e.metaKey)&&!e.altKey&&!e.shiftKey&&/^[0-9]$/" in S.FRAME_BRIDGE


# ================================================================ sample review
def test_sample_review_is_labelled_and_stays_out_of_the_library():
    code, h, body = call("sample/index.html", header=False)
    assert code == 200 and "sandbox" in h["Content-Security-Policy"]
    assert b"debrief-sample-label" in body and b"made-up race" in body and b"None of it is your driving" in body
    assert b"Sample Driver" in body and b"debrief-frame-bridge" in body
    label = body.index(b"debrief-sample-label")
    assert body.rfind(b"<body", 0, label) >= 0, "the label is inside the page body"
    assert S.sample_page() is S.sample_page(), "made once per run"
    code, _, raw = call("api/reviews")
    assert all("ample" not in json.dumps(g) for g in json.loads(raw)["groups"])
    assert not (LIB / "reviews" / "sample").exists()


# ================================================================ window size
def test_window_size_validation_and_memory():
    for bad in (None, [], {}, {"width": "900", "height": 700}, {"width": True, "height": 700}, {"width": 100, "height": 700},
                {"width": 900, "height": 99999}):
        assert settings_io.clean_window_size(bad) is None, bad
        assert settings_io.save_window_size(bad) is False
    f = settings_io._window_file()
    f.unlink(missing_ok=True)
    assert settings_io.window_size() == settings_io.WINDOW_DEFAULT
    code, _, raw = call("api/window", {"width": 1111.0, "height": 777})
    assert code == 200 and json.loads(raw)["ok"] is True
    assert settings_io.window_size() == (1111, 777)
    code, _, raw = call("api/window", {"width": -5, "height": 777})
    assert code == 200 and json.loads(raw)["ok"] is False and settings_io.window_size() == (1111, 777)
    assert call("api/window", {"width": 900, "height": 700}, header=False)[0] == 404
    f.write_text("{not json", encoding="utf-8")
    assert settings_io.window_size() == settings_io.WINDOW_DEFAULT
    # the size saved is the size Edge is opened at
    settings_io.save_window_size({"width": 1234, "height": 876})
    seen = {}
    old_edge, old_popen = D.find_edge, D.subprocess.Popen
    old_here = app.HERE
    try:
        D.find_edge = lambda: "edge.exe"
        D.subprocess.Popen = lambda args, **kw: seen.update(args=args)
        app.HERE = TMP
        assert D.open_window("http://127.0.0.1:1/x/") == "edge"
    finally:
        D.find_edge, D.subprocess.Popen, app.HERE = old_edge, old_popen, old_here
    assert "--window-size=1234,876" in seen["args"], seen


# ================================================================ in the browser
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


def make_reviews():
    """Three real review pages, oldest to newest, and the real library page."""
    if "reviews" in ENV:
        return
    ENV["reviews"] = True
    import numpy as np
    r = np.random.default_rng(3)
    laps, tel = [], {}
    for i in range(1, 9):
        text, t = S._sample_lap(np, r)
        laps.append({"id": f"l{i}", "lapTime": round(t, 3), "lapNumber": i, "clean": True, "_start": 1000.0 + i * (t + 2),
                     "startTime": "2026-01-01T18:00:00Z"})
        tel[f"l{i}"] = text
    for day in ("2026-03-01", "2026-03-02", "2026-03-03"):
        meta = {"driver": "T", "event_id": "E" + day, "track": "Test Track", "car": "Car", "started_at": day + "T18:00:00Z",
                "session": "Race", "multiclass": False}
        data = engine.analyze(meta, [dict(x) for x in laps], tel, [], {}, log=lambda m: None)
        out = LIB / "reviews" / (day + "_test")
        out.mkdir(parents=True, exist_ok=True)
        report.write(out, data)
    library.rebuild(LIB)


def page(viewport=(1280, 860), dark=False):
    b = browser()
    if not b:
        return None
    ctx = b.new_context(viewport={"width": viewport[0], "height": viewport[1]}, color_scheme="dark" if dark else "light")
    pg = ctx.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    # no background sync or update check in these tests
    pg.route("**/api/sync", lambda r: r.fulfill(body='{"ok":true,"skipped":true}', content_type="application/json"))
    pg.route("**/api/update*", lambda r: r.fulfill(body='{"ok":true,"skipped":true}', content_type="application/json"))
    return ctx, pg, errs


def frame_of(pg):
    return next(f for f in pg.frames if f != pg.main_frame)


def test_setup_gate_and_sample_review():
    srv()
    saved = LIB / "reviews"
    hold = TMP / "reviews-held"
    if saved.exists():
        shutil.move(str(saved), str(hold))
    settings(token=False)
    r = page()
    if not r:
        settings()
        return
    ctx, pg, errs = r
    try:
        pg.goto(srv().url)
        pg.wait_for_function("() => !document.getElementById('guide').hidden")
        gated = pg.evaluate("() => [...document.querySelectorAll('.navbtn[data-nav]')].map(b => [b.dataset.nav, b.getAttribute('aria-disabled')])")
        assert dict(gated)["library"] == "true" and dict(gated)["team"] == "true" and dict(gated)["settings"] is None, gated
        # 2.18: only Garage 61's pages wait for the token; the telemetry pages open straight away
        for free in ("practice", "setuplab", "coach", "racing", "cars", "prep"):
            assert dict(gated)[free] in (None, "false"), (free, gated)
        pg.click(".navbtn[data-nav=practice]")
        pg.wait_for_function("() => !document.getElementById('vPractice').hidden")
        pg.click(".navbtn[data-nav=coach]")
        pg.wait_for_function("() => !document.getElementById('vCoach').hidden")
        # the setup checklist's Open Practice goes to Practice, not back to Welcome
        pg.click("#btnHelp")
        pg.click("#helpFirstRun")
        pg.wait_for_function("() => !document.getElementById('vWelcome').hidden")
        pg.wait_for_selector("#firstRun:not([hidden]) .frstep")
        assert pg.text_content("#frTitle") == "Start with your telemetry"
        assert pg.locator("#frList .frstep").count() == 5          # its Garage 61 step is the guide below
        if pg.locator("#frList button[data-fr=practice]").count():
            pg.click("#frList button[data-fr=practice]")
            pg.wait_for_function("() => !document.getElementById('vPractice').hidden")
        # a click and a key both stay on the steps
        pg.click(".navbtn[data-nav=team]", force=True)
        pg.wait_for_function("() => document.getElementById('bannerText').textContent.startsWith('Finish the setup steps')")
        assert pg.is_visible("#vWelcome") and not pg.is_visible("#vTeam")
        pg.keyboard.press("Control+3")
        assert pg.is_visible("#vWelcome")
        # Settings stays open
        pg.click(".navbtn[data-nav=settings]")
        pg.wait_for_function("() => !document.getElementById('vSettings').hidden")
        about = pg.text_content("#aboutRequests")
        assert "about six requests" in about, about
        pg.click(".navbtn[data-nav=library]", force=True)
        pg.wait_for_function("() => !document.getElementById('vWelcome').hidden")
        # the sample review, labelled in the window and on the page
        pg.click("#btnSample")
        pg.wait_for_function("() => !document.getElementById('vFrame').hidden && document.getElementById('loadBar').hidden", timeout=30000)
        assert "made-up race" in pg.text_content("#fbText") and pg.text_content("#fbPill") == "Sample"
        assert pg.is_visible("#fbSampleBack") and not pg.is_visible("#fbResult") and not pg.is_visible("#fbScan")
        fr = frame_of(pg)
        fr.wait_for_selector("#debrief-sample-label")
        assert "made-up race" in fr.text_content("#debrief-sample-label")
        pg.wait_for_function("() => document.title.includes('Sample review')")
        pg.click("#fbSampleBack")
        pg.wait_for_function("() => !document.getElementById('vWelcome').hidden")
        assert not errs, errs
    finally:
        ctx.close()
        settings()
        if hold.exists():
            if saved.exists():
                shutil.rmtree(saved)
            shutil.move(str(hold), str(saved))


def nav_label(pg):
    return pg.evaluate("() => { const a = document.querySelector('.navbtn.active'); return a ? a.dataset.nav : null }")


def wait_nav(pg, name):
    pg.wait_for_function("(n) => { const a = document.querySelector('.navbtn.active'); return a && a.dataset.nav === n }", arg=name)


def test_sidebar_owns_navigation_keys_and_loading_bar():
    srv()
    make_reviews()
    srv().reviews.__init__()
    r = page()
    if not r:
        return
    ctx, pg, errs = r
    try:
        pg.goto(srv().url)
        pg.wait_for_function("() => !document.getElementById('vFrame').hidden")
        names = pg.evaluate("() => [...document.querySelectorAll('.nav .navbtn')].map(b => b.textContent.trim())")
        assert names == ["Progress", "Season", "Skills", "Goals", "All sessions", "Practice", "Racing", "Car vs car", "Prepare", "Team", "Setup Lab", "Coach", "Settings", "Help"], names
        wait_nav(pg, "library")
        # the page's own tabs are gone inside the window...
        fr = frame_of(pg)
        fr.wait_for_selector("nav.tabs", state="attached")
        fr.wait_for_function("() => getComputedStyle(document.querySelector('nav.tabs')).display === 'none' && document.body.classList.contains('in-shell')")
        # ...and still there when the page is opened on its own
        p2 = ctx.new_page()
        p2.goto(srv().url + "lib/index.html")
        assert p2.is_visible("nav.tabs")
        p2.close()
        # Season and Skills switch the same page without a reload, and the sidebar follows
        fr.evaluate("() => { window.__same = 1 }")
        pg.click(".navbtn[data-nav=season]")
        fr.wait_for_function("() => { const t = document.getElementById('tab-season'); return t && !t.hidden }")
        wait_nav(pg, "season")
        assert fr.evaluate("() => window.__same") == 1, "switching tabs reloaded the page"
        # Ctrl+number, from the window and from inside the page
        for key, nav, sel in (("1", "library", None), ("2", "season", None), ("3", "skills", None), ("4", "goals", "#vGoals"),
                              ("5", "sessions", "#vSessions"), ("6", "prep", "#vPrep"), ("7", "team", None),
                              ("8", "setuplab", "#vSetupLab"), ("9", "coach", "#vCoach")):
            pg.keyboard.press("Control+" + key)
            wait_nav(pg, nav)
            if sel:
                pg.wait_for_selector(sel, state="visible")
        pg.keyboard.press("Control+1")
        wait_nav(pg, "library")
        pg.wait_for_function("() => !document.getElementById('vFrame').hidden")
        frame_of(pg).locator("body").click(position={"x": 5, "y": 5})
        pg.keyboard.press("Control+2")
        wait_nav(pg, "season")
        # the goal charts: reached from Goals, hidden tab or not
        pg.keyboard.press("Control+4")
        pg.wait_for_selector("#btnGoalCharts", state="visible")
        pg.click("#btnGoalCharts")
        frame_of(pg).wait_for_function("() => { const t = document.getElementById('tab-goals'); return t && !t.hidden }")
        wait_nav(pg, "goals")
        pg.click("#fbGoals")
        pg.wait_for_selector("#vGoals", state="visible")
        # J / K: older and newer, with the bar on while a slow review opens
        release = threading.Event()
        def slow(route):
            release.wait(10)
            route.continue_()
        pg.route("**/lib/reviews/*2026-03-03*/index.html*", slow)
        pg.focus("#btnGoalCharts")
        pg.keyboard.press("j")                     # not on a review yet: opens the newest
        pg.wait_for_function("() => !document.getElementById('loadBar').hidden")
        assert pg.text_content("#loadText") == "Opening this review..."
        release.set()
        pg.wait_for_function("() => document.getElementById('loadBar').hidden && !document.getElementById('vFrame').hidden")
        active = "() => { const a = document.querySelector('.rev.active'); return a && a.dataset.dir }"
        assert pg.evaluate(active) == "2026-03-03_test"
        pg.keyboard.press("k")                     # already the newest
        pg.wait_for_function("() => document.getElementById('bannerText').textContent === \"That's your newest review.\"")
        pg.keyboard.press("j")
        pg.wait_for_function("() => document.querySelector('.rev.active').dataset.dir === '2026-03-02_test'")
        pg.wait_for_function("() => document.getElementById('loadBar').hidden")
        # from inside the review page, through the bridge
        frame_of(pg).locator("body").click(position={"x": 5, "y": 5})
        pg.keyboard.press("j")
        pg.wait_for_function("() => document.querySelector('.rev.active').dataset.dir === '2026-03-01_test'")
        # J in a text box types, and a dialog stops the keys
        pg.click("#btnHelp")
        pg.wait_for_selector("#dlgHelp[open]")
        pg.keyboard.press("k")
        pg.keyboard.press("Control+6")
        assert pg.evaluate(active) == "2026-03-01_test" and nav_label(pg) is None
        pg.click("[data-help=keys]")
        keys = pg.text_content("#helpBody")
        assert "Ctrl+1 to Ctrl+9" in keys and "J:" in keys and "K:" in keys
        pg.click("#helpClose")
        pg.keyboard.press("Control+4")
        pg.wait_for_selector("#goalNote", state="visible")
        pg.fill("#goalNote", "")
        pg.focus("#goalNote")
        pg.keyboard.press("j")
        assert pg.input_value("#goalNote") == "j" and nav_label(pg) == "goals"
        assert not errs, errs
    finally:
        ctx.close()


def test_dot_pill_and_themes():
    srv()
    r = page(dark=True)
    if not r:
        return
    ctx, pg, errs = r
    try:
        pg.goto(srv().url)
        pg.wait_for_function("() => !document.getElementById('syncStrip').hidden")
        col = "(s) => getComputedStyle(document.querySelector(s)).backgroundColor"
        var = "(n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim()"
        dot = pg.evaluate("() => { const e = document.querySelector('#syncStrip'); e.classList.remove('warn','busy'); return getComputedStyle(e.querySelector('.dot')).backgroundColor }")
        assert dot == "rgb(139, 148, 157)", dot    # --muted, dark
        assert pg.evaluate(var, "--bg") == "#0e1114"
        # an empty pill takes no room, a filled one does
        shown = pg.evaluate("() => { const p = document.getElementById('teamStatus'); p.textContent = ''; const a = getComputedStyle(p).display; p.textContent = 'x'; return [a, getComputedStyle(p).display] }")
        assert shown[0] == "none" and shown[1] != "none", shown
        # the page in the frame follows the window's theme
        pg.click("[data-theme-set=light]")
        assert pg.evaluate(var, "--bg") == "#f3f4f6"
        assert pg.evaluate(col, "body") == "rgb(243, 244, 246)"
        fr = frame_of(pg)
        fr.wait_for_function("() => document.documentElement.getAttribute('data-theme') === 'light'")
        assert fr.evaluate(var, "--bg") == "#f3f4f6"
        pg.click("[data-theme-set=auto]")
        assert not errs, errs
    finally:
        ctx.close()


def test_resizing_the_window_saves_its_size():
    srv()
    r = page(viewport=(1000, 700))
    if not r:
        return
    ctx, pg, errs = r
    try:
        posted = []
        pg.on("request", lambda q: posted.append(json.loads(q.post_data)) if q.url.endswith("api/window") else None)
        pg.goto(srv().url)
        pg.wait_for_function("() => !document.getElementById('vFrame').hidden || !document.getElementById('vWelcome').hidden")
        pg.set_viewport_size({"width": 1100, "height": 720})
        deadline = time.time() + 8
        while not posted and time.time() < deadline:
            pg.wait_for_timeout(100)
        assert posted, "no size was sent"
        assert posted[-1]["width"] > 0 and posted[-1]["height"] > 0, posted
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
