"""Debrief 2.2.2 integration tests: what the integration pass added on top of the four fix teams' work.

    python3 tests/test_integration.py

- the app window's sandboxed iframe and the postMessage bridge (no reaching into the frame)
- the "How Debrief addresses you" setting, and the last-update outcome message
- review.html rendering the 2.2.2 analysis fields (nulls, pit laps, off-pace count, missing channels,
  corners across the start/finish line, signed heat-map values, the L4 labels)
- debrief.log rotation and "~" for the home folder
- Garage 61 request pacing shared by every client in the process, and stop_event waits

Never contacts Garage 61, iRacing or GitHub. The browser checks use Playwright's Chromium and are skipped
(not failed) when either is missing.
"""
from __future__ import annotations

import copy
import json
import os
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"

TMP = Path(tempfile.mkdtemp(prefix="debrief-integ-"))
LIB = TMP / "lib"
(LIB / "reviews").mkdir(parents=True)
ST = TMP / "settings.toml"
ST.write_text(f'token = "test-token-123456"\napi_base = "http://127.0.0.1:9/api/v1"\n'
              f'library_folder = "{LIB.as_posix()}"\nsync_on_open = false\ncheck_updates = false\n', encoding="utf-8")
os.environ.update(DEBRIEF_SETTINGS=str(ST), DEBRIEF_CACHE_DIR=str(TMP / "cache"),
                  DEBRIEF_SCHEDULE_DIR=str(TMP / "sched"), DEBRIEF_UPDATE_STATE=str(TMP / "update.json"))

import requests  # noqa: E402


def _blocked(*a, **k):
    raise requests.ConnectionError("network blocked in tests")


requests.get = _blocked
requests.post = _blocked

from debrief import app, engine, g61, insights, report  # noqa: E402
from debrief.desktop import server as S  # noqa: E402

UI = ROOT / "debrief" / "desktop" / "ui"
TPL = ROOT / "debrief" / "templates"
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_SRV = {}


def srv():
    if "s" not in _SRV:
        s = S.DebriefServer(heartbeat_timeout=3600)
        threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
        _SRV["s"] = s
    return _SRV["s"]


def call(rel, body=None):
    data = json.dumps(body).encode() if body is not None else None
    rq = urllib.request.Request(srv().url + rel, data=data, method="POST" if data is not None else "GET",
                                headers={"X-Debrief": "1", "Content-Type": "application/json"})
    try:
        with OP.open(rq, timeout=20) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


# ================================================================ fixtures
_BASE = {}


def race():
    """A synthetic race through the real engine (the shape of test_fix_d's generator)."""
    if "d" not in _BASE:
        import test_fix_d
        d = test_fix_d._race()
        d["insights"] = insights.build(d)   # as cmd_review does
        _BASE["d"] = d
    return copy.deepcopy(_BASE["d"])


def page(d, name):
    out = TMP / "pages" / name
    out.mkdir(parents=True, exist_ok=True)
    report.write(out, d)
    return out / "index.html"


_PW = {}


def browser():
    """A shared headless Chromium, or None (the test is skipped) when Playwright or its browser is missing."""
    if "b" in _PW:
        return _PW["b"]
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  SKIPPED: playwright not installed")
        _PW["b"] = None
        return None
    try:
        pw = sync_playwright().start()
    except Exception:  # noqa: BLE001
        print("  SKIPPED: Playwright could not start")
        _PW["b"] = None
        return None
    try:
        b = pw.chromium.launch()
    except Exception:  # noqa: BLE001
        print("  SKIPPED: no Chromium for Playwright")
        pw.stop()
        _PW["b"] = None
        return None
    _PW["pw"], _PW["b"] = pw, b
    return b


CSP_HOOK = ("addEventListener('securitypolicyviolation',e=>console.error('CSP-VIOLATION '+e.violatedDirective+' '"
            "+e.blockedURI),true);")


def new_page(b, scheme="light"):
    ctx = b.new_context(viewport={"width": 1280, "height": 900}, color_scheme=scheme)
    ctx.add_init_script(CSP_HOOK)
    ctx.route("**/fonts.googleapis.com/**", lambda r: r.abort())
    ctx.route("**/fonts.gstatic.com/**", lambda r: r.abort())
    pg = ctx.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append("pageerror: " + str(e)))
    pg.on("console", lambda m: errs.append("console: " + m.text) if m.type == "error" and "fonts.g" not in m.text
          and "fonts.g" not in (m.location or {}).get("url", "") else None)
    return ctx, pg, errs


# ================================================================ M1 (C): sandboxed frame + bridge
def test_iframe_is_sandboxed_exactly_like_the_server_header():
    html = (UI / "index.html").read_text(encoding="utf-8")
    want = '<iframe id="frame" title="Review" sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox allow-downloads"></iframe>'
    assert want in html
    assert "allow-same-origin" not in html
    assert f'sandbox="{S.FRAME_SANDBOX}"' in html
    assert S.LIB_CSP.startswith("sandbox " + S.FRAME_SANDBOX)


def test_window_never_reaches_into_the_frame():
    js = (UI / "app.js").read_text(encoding="utf-8")
    assert "contentDocument" not in js
    assert not re.search(r"contentWindow\s*\.\s*location", js)
    assert "location.reload" not in js and "location.replace" not in js
    # the bridge listener trusts our own frame by e.source (a sandboxed page's origin is "null")
    assert "e.source!==f.contentWindow" in js and not re.search(r"e\.origin\s*[!=]==?", js)
    assert "type:'debrief-theme'" in js and "'debrief-frame'" in js


def test_window_has_no_inline_handlers_or_eval():
    html = (UI / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"<[^>]+\son[a-z]+\s*=", html), "inline event handler in index.html"
    js = (UI / "app.js").read_text(encoding="utf-8")
    assert "eval(" not in js and "new Function" not in js and not re.search(r"setTimeout\(\s*['\"]", js)


# ================================================================ salutation setting
def test_salutation_field_in_settings():
    html = (UI / "index.html").read_text(encoding="utf-8")
    assert re.search(r'<input id="salut"[^>]*maxlength="20"', html)
    assert "How Debrief addresses you" in html
    js = (UI / "app.js").read_text(encoding="utf-8")
    assert "salutation:sal" in js and "$('salut').value=" in js


def test_salutation_saved_through_api_and_read_by_reviews():
    st, j = call("api/settings", {"salutation": "  Sir  "})
    assert st == 200 and j["ok"] and j["settings"]["salutation"] == "Sir", j
    assert 'salutation = "Sir"' in ST.read_text(encoding="utf-8")
    assert app.load_settings()["salutation"] == "Sir"
    st, j = call("api/settings", {"salutation": "x" * 21})
    assert st == 400 and "20" in j["error"], j
    st, j = call("api/settings", {"salutation": "<b>"})
    assert st == 400, j
    st, j = call("api/settings", {"salutation": ""})
    assert st == 200 and j["settings"]["salutation"] == ""
    assert app.load_settings()["salutation"] == ""


# ================================================================ update outcome
def test_last_update_outcome_reaches_the_window_even_when_auto_check_is_off():
    s = srv()
    s.update_outcome = {"ok": False, "message": "The last update didn't finish.", "log": "x"}
    try:
        st, j = call("api/update?auto=1")   # check_updates = false in this settings file
        assert st == 200 and j.get("skipped") and j["last_update"]["ok"] is False, j
    finally:
        s.update_outcome = None
    js = (UI / "app.js").read_text(encoding="utf-8")
    assert "u.ok!==false" in js and "banner(m,true" in js


# ================================================================ review page: A's 2.2.2 fields
def test_review_template_wording():
    t = (TPL / "review.html").read_text(encoding="utf-8")
    assert "pit or spin" not in t                       # pit laps are counted on their own now
    assert "within 0.5 s of your median'" not in t      # L4: the two "within 0.5 s" measures say their base
    assert "Within 0.5 s of your best lap" in t and "within half a second of your median lap" in t
    assert "['+'+v.toFixed(3)+' s vs your best here'" not in t   # heat map: signed values
    assert "off_pace_n" in t and "channels_missing" in t and "c.pit" in t


def _nasty(d):
    """2.2.2 data at its edges: null pedal numbers, a pit lap, an off-pace count, a corner across the line."""
    d = copy.deepcopy(d)
    for k in d["style"]:
        for f in ("coast_s", "full_thr_pct", "brake_pct", "overlap_s"):
            d["style"][k][f] = None
    for c in d["corners"]:
        c.setdefault("summary", {})
        c["summary"]["gear"] = None
        c["summary"]["peak_brake"] = None
        for m in (c.get("per_lap") or {}).values():
            m["peak_brake"] = None; m["coast_s"] = None; m["gear"] = None
    d["corners"][0]["start_m"] = -60.0
    for t in d.get("technique") or []:
        for key in ("best", "typical", "quick"):
            if isinstance(t.get(key), dict):
                t[key]["coast_s"] = None; t[key]["gear"] = None; t[key]["peak_brake"] = None
    d["channels_missing"] = ["Brake", "Throttle"]
    d["counts"]["pit"] = [9]
    d["counts"]["excluded"] = 1
    d["counts"]["excluded_laps"] = [12]
    if isinstance(d.get("consistency"), dict):
        d["consistency"]["off_pace_n"] = 3
    # an unclean lap can beat the clean-lap baseline in the heat map
    if isinstance(d.get("heat"), dict) and d["heat"].get("values"):
        d["heat"]["values"][0][0] = -0.123
    d["insights"] = insights.build(d)
    return d


def test_review_renders_real_insights_and_edge_values():
    d = race()
    assert d["insights"].get("version") == 2 and d["insights"]["fix"], d["insights"]
    assert all("numbers" in f and "times" in f for f in d["insights"]["fix"])
    p_real = page(d, "real")
    p_nasty = page(_nasty(d), "nasty")
    assert "pit or spin" not in p_nasty.read_text(encoding="utf-8")
    b = browser()
    if b is None:
        return
    for scheme in ("light", "dark"):
        for p in (p_real, p_nasty):
            ctx, pg, errs = new_page(b, scheme)
            pg.goto(p.resolve().as_uri())
            pg.wait_for_function("() => document.querySelectorAll('#fixList .fix').length > 0", timeout=15000)
            r = pg.evaluate("""() => ({
                fails: [...document.querySelectorAll('.secfail, #failNote:not([hidden])')].map(e => e.textContent).join(' | '),
                body: document.body.innerText,
                fixHead: [...document.querySelectorAll('#fixList table.fnums thead th')].map(t => t.textContent),
                cards: document.querySelectorAll('#fixList .fix').length})""")
            ctx.close()
            assert not errs, (p.parent.name, scheme, errs)
            assert not r["fails"], (p.parent.name, r["fails"])
            for bad in ("NaN", "undefined", "Invalid Date", "null s", "null m"):
                assert bad not in r["body"], (p.parent.name, bad)
            assert 1 <= r["cards"] <= 5
            assert "Quicker laps" in r["fixHead"], r["fixHead"]    # numbers.quick from the real insights
            if p is p_nasty:
                body = r["body"]
                assert "Pit stop on lap 9" in body
                assert "lap 12 was far off the pace" in body
                assert "Off-pace laps" in body and "3 of" in body
                assert "brake pedal and throttle" in body       # channels_missing in plain words


def test_heat_map_tooltip_is_signed():
    b = browser()
    if b is None:
        return
    d = _nasty(race())
    p = page(d, "heat")
    ctx, pg, errs = new_page(b)
    pg.goto(p.resolve().as_uri())
    pg.wait_for_function("() => document.querySelector('#heatChart svg rect')", timeout=15000)
    pg.evaluate("() => { const d = document.getElementById('deep'); if (d && d.tagName === 'DETAILS') d.open = true }")
    pg.locator("#heatChart svg rect").first.dispatch_event("pointerenter")
    pg.locator("#heatChart svg rect").first.dispatch_event("mouseenter")
    pg.locator("#heatChart svg rect").first.hover(force=True)
    pg.wait_for_timeout(200)
    tip = pg.evaluate("() => document.body.innerText")
    ctx.close()
    assert not errs, errs
    assert "−0.123 s vs your best clean lap here" in tip and "+-0.123" not in tip


# ================================================================ app window end to end (short)
def test_app_window_bridge_theme_and_reload_in_browser():
    b = browser()
    if b is None:
        return
    d = race()
    d["meta"]["salutation"] = "Sir"
    rv = LIB / "reviews" / "2026-10-27_integ"
    rv.mkdir(parents=True, exist_ok=True)
    report.write(rv, d)
    from debrief import library
    library.rebuild(str(LIB))
    s = srv()
    ctx, pg, errs = new_page(b)
    try:
        pg.goto(s.url)
        pg.wait_for_function("() => document.querySelectorAll('.rev').length > 0", timeout=20000)
        pg.click(".rev[data-dir='2026-10-27_integ']")
        # the window learns the page's title only through the bridge
        pg.wait_for_function("() => document.title.startsWith('Debrief · ')", timeout=20000)
        fr = pg.frame_locator("#frame")
        fr.locator("#fixList .fix").first.wait_for(timeout=20000)
        # the window can't look inside; the page can't look out
        assert pg.evaluate("() => { try { return document.getElementById('frame').contentDocument === null } catch (e) { return true } }")
        assert pg.frames[1].evaluate("() => { try { return String(parent.document.title) } catch (e) { return 'blocked' } }") == "blocked"
        # theme over postMessage
        pg.click("[data-theme-set=dark]")
        pg.wait_for_function("() => true")
        pg.wait_for_timeout(300)
        assert fr.locator("html").get_attribute("data-theme") == "dark"
        pg.click("[data-theme-set=auto]")
        pg.wait_for_timeout(300)
        assert fr.locator("html").get_attribute("data-theme") is None
        # 'Show me in Compare' works inside the sandbox
        fr.locator("#fixList .fix button:has-text('Show me in Compare')").first.click()
        pg.wait_for_timeout(400)
        assert "Showing" in fr.locator("#zoomInfo").inner_text()
        # race result -> reloadFrame sets src (a fresh load), the frame bar updates
        pg.click("#fbResult")
        pg.fill("#r_finish_pos", "5")
        pg.click("#resSave")
        pg.wait_for_function("() => document.getElementById('fbPill').textContent === 'P5'", timeout=10000)
        pg.wait_for_function("() => document.getElementById('frame').src.includes('?r=')", timeout=5000)
        fr.locator("#fixList .fix").first.wait_for(timeout=20000)
        # library -> season (hash) -> library: the window follows the frame
        pg.click(".side [data-nav=season]")
        fr.locator("#tab-season").wait_for(state="visible", timeout=10000)
        pg.click(".side [data-nav=library]")
        fr.locator("#tab-overview").wait_for(state="visible", timeout=10000)
        assert pg.evaluate("() => document.querySelector('[data-nav=library]').getAttribute('aria-current')") == "page"
    finally:
        ctx.close()
    assert not errs, errs   # includes CSP-VIOLATION lines from the hook


# ================================================================ L7 (A): debrief.log
def test_log_rotates_at_about_a_megabyte_and_keeps_three():
    log = TMP / "logs" / "debrief.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    old, mx = app.LOG_FILE, app.LOG_MAX_BYTES
    app.LOG_FILE, app.LOG_MAX_BYTES = str(log), 2000
    try:
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            for i in range(400):
                app.say(f"line {i:04d} " + "x" * 40)
        files = sorted(p.name for p in log.parent.iterdir())
        assert files == ["debrief.log", "debrief.log.1", "debrief.log.2"], files
        assert all(p.stat().st_size <= 2000 + 100 for p in log.parent.iterdir())
        assert "line 0399" in log.read_text(encoding="utf-8")
        assert "line 0000" not in "".join(p.read_text(encoding="utf-8") for p in log.parent.iterdir())
    finally:
        app.LOG_FILE, app.LOG_MAX_BYTES = old, mx
    assert app.LOG_MAX_BYTES == 1_000_000 and app.LOG_BACKUPS == 2


def test_log_writes_tilde_for_the_home_folder():
    log = TMP / "logs2" / "debrief.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    old_env = os.environ.get("USERPROFILE")
    os.environ["USERPROFILE"] = r"C:\Users\Swazi"
    old = app.LOG_FILE
    app.LOG_FILE = str(log)
    try:
        app.say(r"Review saved to C:\Users\Swazi\Documents\Debrief Reviews\x\index.html")
        app.say("Settings in c:/users/swazi/AppData/Local/Debrief")
        app.say(r"Not a match: C:\Users\SwaziB\x")
        app.say("Home: " + str(Path.home()) + "/x")
    finally:
        app.LOG_FILE = old
        if old_env is None:
            os.environ.pop("USERPROFILE", None)
        else:
            os.environ["USERPROFILE"] = old_env
    t = log.read_text(encoding="utf-8")
    assert r"~\Documents\Debrief Reviews" in t and "~/AppData/Local" in t, t
    assert r"C:\Users\SwaziB\x" in t          # a different user folder that merely starts the same
    assert "Swazi\\" not in t.replace(r"SwaziB\x", "")
    assert str(Path.home()) + "/" not in t and "Home: ~/x" in t


# ================================================================ L5 (C -> B): shared pacing, stop_event
def test_pacing_is_shared_by_every_client_in_the_process():
    a = g61.Garage61("tok-123456", TMP / "c1", min_gap_s=0.3, base_url="http://127.0.0.1:9/api/v1/", log=lambda m: None)
    b = g61.Garage61("tok-654321", TMP / "c2", min_gap_s=0.3, base_url="http://127.0.0.1:9/api/v1/", log=lambda m: None)
    stamps = []
    lk = threading.Lock()

    def go(c):
        for _ in range(4):
            c._wait()
            with lk:
                stamps.append(time.monotonic())
    ts = [threading.Thread(target=go, args=(c,)) for c in (a, b)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    stamps.sort()
    gaps = [y - x for x, y in zip(stamps, stamps[1:])]
    assert len(stamps) == 8 and min(gaps) >= 0.25, gaps   # 8 requests from 2 clients: still ~0.3 s apart


def test_stop_event_ends_a_long_wait_quickly():
    ev = threading.Event()
    c = g61.Garage61("tok-123456", TMP / "c3", min_gap_s=0, base_url="http://127.0.0.1:9/api/v1/",
                     log=lambda m: None, stop_event=ev)
    threading.Timer(0.2, ev.set).start()
    t0 = time.monotonic()
    try:
        c._sleep(60)
        raise AssertionError("not interrupted")
    except g61.Interrupted:
        pass
    assert time.monotonic() - t0 < 1.0
    # without a stop event or should_stop, a short wait is a plain sleep
    c2 = g61.Garage61("tok-123456", TMP / "c4", min_gap_s=0, base_url="http://127.0.0.1:9/api/v1/", log=lambda m: None)
    t0 = time.monotonic(); c2._sleep(0.05)
    assert time.monotonic() - t0 >= 0.04


# ================================================================ runner
if __name__ == "__main__":
    failed = 0
    tests = [(n, f) for n, f in list(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        t0 = time.time()
        try:
            fn()
            print(f"ok    {name} ({time.time() - t0:.1f}s)")
        except Exception:  # noqa: BLE001
            import traceback
            failed += 1
            print(f"FAIL  {name}")
            traceback.print_exc()
    if _PW.get("b"):
        _PW["b"].close()
        _PW["pw"].stop()
    if "s" in _SRV:
        _SRV["s"].shutdown()
        _SRV["s"].server_close()
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
