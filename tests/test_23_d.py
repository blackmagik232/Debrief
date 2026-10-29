"""Debrief 2.3 shell (workstream 23-D): report.write extras, layout 2.3.0 refresh, Skills page in the window,
practice/qualifying review buttons, feature modules, version and build lists.

    python3 tests/test_23_d.py

The 2.3 data modules (technique_plus, skills, session_review, profile) belong to 23-A and 23-B. These tests
put small stand-ins with the agreed §1.1 shapes (tests/fixtures_23_d/sample_*.json) in their place, so they
pass before and after the merge; one test also runs the real modules when they're present. Never contacts
Garage 61: the server is in-process on 127.0.0.1 and the browser checks are skipped without Playwright.
"""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import types
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"

TMP = Path(tempfile.mkdtemp(prefix="debrief-23d-"))
LIB = TMP / "lib"
(LIB / "reviews").mkdir(parents=True)
ST = TMP / "settings.toml"
ST.write_text(f'token = "PASTE-YOUR-GARAGE61-TOKEN-HERE"\nlibrary_folder = \'{LIB}\'\n', encoding="utf-8")
os.environ.update(DEBRIEF_SETTINGS=str(ST), DEBRIEF_CACHE_DIR=str(TMP / "cache"),
                  DEBRIEF_SCHEDULE_DIR=str(TMP / "sched"), DEBRIEF_UPDATE_STATE=str(TMP / "update.json"))

import requests  # noqa: E402


def _blocked(*a, **k):
    raise requests.ConnectionError("network blocked in tests")


requests.get = _blocked
requests.post = _blocked

import debrief  # noqa: E402
from debrief import insights, library, report  # noqa: E402
import debrief.desktop as desktop  # noqa: E402
from debrief.desktop import server as S  # noqa: E402
import make_fixture_library  # noqa: E402

FX = ROOT / "tests" / "fixtures_23_d"
UI = ROOT / "debrief" / "desktop" / "ui"
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
NEW = ("technique_plus", "skills", "session_review", "profile")


def sample(name):
    return json.loads((FX / f"sample_{name}.json").read_text(encoding="utf-8"))


# ================================================================ stand-in modules (§1.1 shapes)
@contextmanager
def modules(calls=None, fail=(), missing=()):
    """Put stand-ins for the 2.3 modules in sys.modules. calls (a list) records the call order; a name in
    fail raises; a name in missing can't be imported at all."""
    calls = calls if calls is not None else []

    def mk(name, **fns):
        m = types.ModuleType("debrief." + name)
        m.VERSION = 1
        for k, f in fns.items():
            def wrap(*a, _f=f, _k=k, **kw):
                calls.append(name + "." + _k)
                if name in fail:
                    raise RuntimeError(name + " broke")
                return _f(*a, **kw)
            setattr(m, k, wrap)
        return m

    def sess(d):
        kind = {"Practice": "practice", "Qualifying": "qualifying"}.get((d.get("meta") or {}).get("session"))
        if not kind:
            return None
        out = sample("session_review")
        out["kind"] = kind
        return out

    fake = {"technique_plus": mk("technique_plus", spread_for_review=lambda d: sample("brake_spread"),
                                 link=lambda xs, ys: None, build=lambda *a: None),
            "skills": mk("skills", for_review=lambda d: sample("skills"), better_laps=lambda d: [],
                         score=lambda pm, ph: None),
            "session_review": mk("session_review", for_review=sess),
            "profile": mk("profile", build=lambda rows: sample("profile"))}
    saved = {n: sys.modules.get("debrief." + n, Ellipsis) for n in NEW}
    saved_attr = {n: getattr(debrief, n, Ellipsis) for n in NEW}
    try:
        for n in NEW:
            sys.modules["debrief." + n] = None if n in missing else fake[n]
            if n in missing:
                if hasattr(debrief, n):
                    delattr(debrief, n)
            else:
                setattr(debrief, n, fake[n])
        yield calls
    finally:
        for n in NEW:
            if saved[n] is Ellipsis:
                sys.modules.pop("debrief." + n, None)
            else:
                sys.modules["debrief." + n] = saved[n]
            if saved_attr[n] is Ellipsis:
                if hasattr(debrief, n):
                    delattr(debrief, n)
            else:
                setattr(debrief, n, saved_attr[n])


_OLD = {}


def old_review(session="Race"):
    """A 2.2.1-format review (make_fixture_library): no technique_plus/skills/session_review/brake_spread."""
    if "d" not in _OLD:
        import random
        _OLD["d"] = make_fixture_library.fake_review(make_fixture_library.ROVAL, "2026-10-01T23:30:00+00:00", 0.4,
                                                     True, make_fixture_library.S4, 1100, None, random.Random(7))
    d = copy.deepcopy(_OLD["d"])
    d["meta"]["session"] = session
    for k in ("technique_plus", "skills", "session_review", "brake_spread", "insights"):
        assert k not in d
    return d


def written(folder):
    return json.loads((Path(folder) / "review.json").read_text(encoding="utf-8"))


# ================================================================ report.write
def test_write_old_race_adds_skills_and_brake_spread_not_session_review():
    with modules() as calls, tempfile.TemporaryDirectory() as td:
        report.write(td, old_review("Race"))
        d = written(td)
        assert d["skills"] == sample("skills") and d["brake_spread"] == sample("brake_spread")
        assert "session_review" not in d                   # None for races: the key is left out
        assert "insights" in d
        assert calls == ["technique_plus.spread_for_review", "skills.for_review", "session_review.for_review"], calls
        html = (Path(td) / "index.html").read_text(encoding="utf-8")
        assert '"brake_spread":' in html and '"skills":' in html


def test_write_practice_and_qualifying_add_session_review():
    for sess, kind in (("Practice", "practice"), ("Qualifying", "qualifying")):
        with modules(), tempfile.TemporaryDirectory() as td:
            report.write(td, old_review(sess))
            assert written(td)["session_review"]["kind"] == kind


def test_extras_run_before_insights_and_insights_sees_them():
    seen = {}
    real = insights.build

    def spy(data):
        seen.update({k: k in data for k in ("brake_spread", "skills", "session_review")})
        return real(data)
    insights.build = spy
    try:
        with modules(), tempfile.TemporaryDirectory() as td:
            report.write(td, old_review("Practice"))
    finally:
        insights.build = real
    assert seen == {"brake_spread": True, "skills": True, "session_review": True}, seen


def test_failing_skills_drops_only_skills():
    logs = []
    with modules(fail={"skills"}), tempfile.TemporaryDirectory() as td:
        d = old_review("Practice")
        d["skills"] = {"stale": True}                      # an old value never survives a failed rebuild
        report.write(td, d, log=logs.append)
        out = written(td)
        assert "skills" not in out and "brake_spread" in out and "session_review" in out and "insights" in out
        assert (Path(td) / "index.html").is_file()
    assert any("skills" in m and "broke" in m for m in logs), logs


def test_every_extra_failing_still_writes_review():
    with modules(fail=set(NEW)), tempfile.TemporaryDirectory() as td:
        report.write(td, old_review("Practice"))
        out = written(td)
        assert not any(k in out for k in ("skills", "brake_spread", "session_review")) and "insights" in out


def test_missing_modules_write_quietly():
    """A copy without the 2.3 modules (before the merge) writes the review as 2.2.2 did, and logs nothing."""
    logs = []
    with modules(missing=set(NEW)), tempfile.TemporaryDirectory() as td:
        report.write(td, old_review("Practice"), log=logs.append)
        out = written(td)
        assert not any(k in out for k in ("skills", "brake_spread", "session_review"))
    assert logs == [], logs


def test_write_is_still_atomic():
    src = (ROOT / "debrief" / "report.py").read_text(encoding="utf-8")
    body = src[src.index("def write("):src.index("def atomic_write(")]
    assert 'atomic_write(out_dir / "review.json"' in body and "atomic_write(page, html)" in body
    assert ".write_text(" not in body


def test_extras_order_matches_spec():
    assert [k for k, _, _ in report.EXTRAS] == ["brake_spread", "skills", "session_review"]
    assert report.EXTRAS[0][1:] == ("technique_plus", "spread_for_review")


def test_real_modules_when_present():
    """After the merge: the real 23-A/23-B modules on an old review (each guarded, so this never fails a write)."""
    import importlib
    have = []
    for n in ("technique_plus", "skills", "session_review"):
        try:
            importlib.import_module("debrief." + n)
            have.append(n)
        except ImportError:
            pass
    if not have:
        print("  SKIPPED: 2.3 data modules not in this copy yet")
        return
    with tempfile.TemporaryDirectory() as td:
        report.write(td, old_review("Practice"))
        out = written(td)
        if "skills" in have:
            assert isinstance(out.get("skills"), dict)      # always a dict ("reason" when it can't score)
        if "session_review" in have:
            assert (out.get("session_review") or {}).get("kind") == "practice"
        report.write(td, old_review("Race"))
        assert "session_review" not in written(td)


# ================================================================ refresh_all for layout 2.3.0
def _library(n=4):
    lib = Path(tempfile.mkdtemp(prefix="lib", dir=TMP))
    made = make_fixture_library.main(str(lib))[:n]
    for p in (lib / "reviews").iterdir():
        if p.name not in made:
            shutil.rmtree(p)
    (lib / ".layout").write_text("2.2.2", encoding="utf-8")
    return lib, made


def test_layout_is_230():
    assert tuple(int(x) for x in report.LAYOUT.split(".")) >= (2, 3, 0)   # 2.3.0 or later (2.4 bumped it)


def test_refresh_rerenders_once_for_230_and_adds_keys():
    lib, made = _library(4)
    prog = []
    with modules():
        n = report.refresh_all(lib, progress=lambda k, t: prog.append((k, t)))
        assert n == 4
        assert (lib / ".layout").read_text(encoding="utf-8").strip() == report.LAYOUT
        for d in made:
            r = written(lib / "reviews" / d)
            assert "skills" in r and "brake_spread" in r
            assert "/*DATA*/null" not in (lib / "reviews" / d / "index.html").read_text(encoding="utf-8")
        assert report.refresh_all(lib) == 0             # once per layout
    assert prog[0] == (0, 4) and prog[-1] == (4, 4), prog


def test_refresh_pause_resume_keeps_going():
    lib, made = _library(4)
    done, stops = set(), iter([False, False, True])
    with modules():
        n1 = report.refresh_all(lib, should_stop=lambda: next(stops, False), done=done)
        assert n1 == 2 and (lib / ".layout").read_text(encoding="utf-8").strip() == "2.2.2"   # paused: no marker
        n2 = report.refresh_all(lib, done=done)
    assert n2 == 2 and (lib / ".layout").read_text(encoding="utf-8").strip() == report.LAYOUT


def test_refresh_runs_in_background_with_progress():
    """The window starts the refresh on a tracked background thread; status says how far it has got."""
    jobs = S.Jobs()
    gate = threading.Event()

    def fn():
        jobs.set_background_progress("refresh", 3, 10)
        gate.wait(5)
    assert jobs.run_background("refresh", fn)
    time.sleep(0.1)
    snap = jobs.snapshot()
    gate.set()
    assert snap.get("background") == ["refresh"] and snap["background_progress"]["refresh"] == {"done": 3, "total": 10}
    time.sleep(0.1)
    assert "background_progress" not in jobs.snapshot()     # only while it runs
    src = (ROOT / "debrief" / "desktop" / "__init__.py").read_text(encoding="utf-8")
    assert 'srv.jobs.run_background("refresh", _refresh_reviews)' in src and "progress=progress" in src


# ================================================================ modules, version, build lists
def test_feature_modules_spec_and_ci():
    for n in NEW:
        assert n in desktop.FEATURE_MODULES, n
    spec = (ROOT / "packaging" / "debrief.spec").read_text(encoding="utf-8")
    must = spec[spec.index("for _must in"):spec.index("if _must not in")]
    for n in NEW:
        assert f'"debrief.{n}"' in must, n
    ci = (ROOT / ".github" / "workflows" / "build.yml").read_text(encoding="utf-8")
    for x in "abcd":
        assert f"run: python tests/test_23_{x}.py" in ci, x
    assert tuple(int(x) for x in debrief.__version__.split(".")) >= (2, 3, 0)


def test_selftest_passes_with_new_modules():
    """The real selftest, with the 2.3 modules stood in when a copy doesn't have them yet."""
    import io
    import contextlib
    from debrief import app
    home = Path(tempfile.mkdtemp(prefix="home", dir=TMP))
    old = app.HERE
    app.HERE = home
    import importlib
    missing = []
    for n in NEW:
        try:
            importlib.import_module("debrief." + n)
        except ImportError:
            missing.append(n)
    buf = io.StringIO()
    try:
        ctx = modules() if missing else contextlib.nullcontext()
        with ctx, contextlib.redirect_stdout(buf):
            rc = desktop.selftest()
    finally:
        app.HERE = old
    res = json.loads((home / "selftest.json").read_text(encoding="utf-8"))
    # a later release's modules that aren't in this copy yet (before that release's merge) are its own tests' job
    later = {"module:" + n for n in desktop.FEATURE_MODULES[desktop.FEATURE_MODULES.index("profile") + 1:]
             if not (ROOT / "debrief" / (n + ".py")).is_file()}
    assert (rc == 0 and res["ok"]) or (res["failed"] and set(res["failed"]) <= later and not res["error"]), res
    assert res["version"] == debrief.__version__


# ================================================================ server: api/profile
_SRV = {}


def srv():
    if "s" not in _SRV:
        s = S.DebriefServer(heartbeat_timeout=3600)
        threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
        _SRV["s"] = s
    return _SRV["s"]


def get(rel, headers=None):
    rq = urllib.request.Request(srv().url + rel, headers=headers if headers is not None else {"X-Debrief": "1"})
    try:
        with OP.open(rq, timeout=20) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def _fill_lib():
    if not any((LIB / "reviews").iterdir()):
        make_fixture_library.main(str(LIB))


def test_api_profile():
    _fill_lib()
    with modules():
        code, _, body = get("api/profile")
        j = json.loads(body)
        assert code == 200 and j["ok"] and j["available"] and j["n_races"] == 14, j
        assert j["weakest"] == {"group": "all", "phase": "exit"} and j["refreshing"] is None
    code, _, _ = get("api/profile", headers={})             # the API needs the window's header
    assert code == 404


def test_api_profile_without_module_or_broken():
    _fill_lib()
    with modules(missing={"profile"}):
        j = json.loads(get("api/profile")[2])
        assert j["ok"] and not j["available"]
    with modules(fail={"profile"}):
        j = json.loads(get("api/profile")[2])
        assert j["ok"] and not j["available"]


def test_api_profile_reports_refresh_progress():
    s = srv()
    gate = threading.Event()

    def fn():
        s.jobs.set_background_progress("refresh", 5, 40)
        gate.wait(5)
    s.jobs.run_background("refresh", fn)
    time.sleep(0.1)
    try:
        with modules():
            j = json.loads(get("api/profile")[2])
    finally:
        gate.set()
        s.jobs.wait_background(5)
    assert j["refreshing"] == {"done": 5, "total": 40}, j


def test_window_security_model_unchanged():
    code, h, body = get("", headers={})
    csp = h["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "unsafe-eval" not in csp and "frame-ancestors 'none'" in csp
    assert "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]
    html = (UI / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"\son[a-z]+\s*=", html, re.I)       # no inline handlers
    js = (UI / "app.js").read_text(encoding="utf-8")
    assert "eval(" not in js and "new Function" not in js and "innerHTML" not in js
    assert f'sandbox="{S.FRAME_SANDBOX}"' in html and "allow-same-origin" not in S.FRAME_SANDBOX
    _fill_lib()
    code, h, body = get("lib/index.html", headers={})
    assert code == 200 and h["Content-Security-Policy"] == S.LIB_CSP and b"debrief-frame-bridge" in body


# ================================================================ app window (Playwright)
_PW = {}


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


def _window():
    """The real window from the real in-process server (real CSP, real library page in the sandboxed frame)."""
    b = _browser()
    if not b:
        return None
    _fill_lib()
    ctx = b.new_context(viewport={"width": 1280, "height": 860})
    pg = ctx.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    # (fonts are blocked in tests: their failed loads are the only console errors allowed)
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" and "net::ERR_FAILED" not in m.text else None)
    # a frame load replaced by the next click is aborted (net::ERR_ABORTED): harmless; anything else is an error
    pg.on("requestfailed", lambda r: errs.append("failed " + r.url + " " + str(r.failure))
          if "fonts.g" not in r.url and "ERR_ABORTED" not in str(r.failure) else None)
    pg.route("**/fonts.googleapis.com/**", lambda r: r.abort())
    pg.route("**/fonts.gstatic.com/**", lambda r: r.abort())
    pg.goto(srv().url)
    pg.wait_for_function("() => document.querySelectorAll('#revlist .rev').length > 0", timeout=15000)
    return pg, ctx, errs


def test_window_skills_nav_opens_library_skills():
    with modules():
        r = _window()
        if not r:
            return
        pg, ctx, errs = r
        try:
            order = pg.evaluate("() => [...document.querySelectorAll('.nav .navbtn')].map(b => b.dataset.nav)")
            assert order.index("skills") == order.index("season") + 1 == order.index("goals") - 1, order
            pg.click(".navbtn[data-nav=skills]")
            pg.wait_for_function("() => !document.getElementById('vFrame').hidden", timeout=15000)
            pg.wait_for_function("() => /lib\\/index\\.html\\?r=[^#]*#skills$/.test(document.getElementById('frame').src)"
                                 " || document.getElementById('frame').src.endsWith('lib/index.html#skills')")
            cur = pg.evaluate("() => [...document.querySelectorAll('[aria-current=page]')].map(b => b.dataset.nav)")
            assert cur == ["skills"], cur
            # the frame's own page reports where it is (bridge), and that keeps Skills selected
            frame = next(f for f in pg.frames if "lib/index.html" in f.url)
            assert frame.url.endswith("#skills"), frame.url
            # with 23-C's library page (it has the Skills tab), the tab is what shows in the frame
            tab = frame.evaluate("() => { const t = document.getElementById('tab-skills'); return t ? !t.hidden : null }")
            assert tab in (True, None), tab
            if tab is None:
                print("  (library page without the Skills tab yet: tab check skipped)")
            pg.wait_for_function("() => document.getElementById('fbText').textContent.includes('14 races')", timeout=10000)
            bar = pg.evaluate("() => ({bar: !document.getElementById('frameBar').hidden,"
                              " text: document.getElementById('fbText').textContent,"
                              " result: document.getElementById('fbResult').hidden,"
                              " sandbox: document.getElementById('frame').getAttribute('sandbox')})")
            assert bar["bar"] and bar["result"] and "corner exits" in bar["text"], bar
            assert "allow-same-origin" not in bar["sandbox"]
            # Season then Skills again: a hash-only change inside the same page
            pg.click(".navbtn[data-nav=season]")
            pg.wait_for_function("() => document.querySelector('.navbtn[data-nav=season]').getAttribute('aria-current') === 'page'")
            pg.wait_for_function("() => document.getElementById('frameBar').hidden")
            pg.click(".navbtn[data-nav=skills]")
            pg.wait_for_function("() => document.querySelector('.navbtn[data-nav=skills]').getAttribute('aria-current') === 'page'")
            pg.wait_for_function("() => document.getElementById('frame').src.endsWith('#skills')")
            pg.wait_for_timeout(300)
            assert pg.evaluate("() => !document.getElementById('frameBar').hidden")
            # Library leaves Skills
            pg.click(".navbtn[data-nav=library]")
            pg.wait_for_function("() => document.querySelector('.navbtn[data-nav=library]').getAttribute('aria-current') === 'page'")
            assert not [e for e in errs if "Content Security Policy" in e or "Refused" in e], errs
            assert not errs, errs
        finally:
            ctx.close()


def test_window_skills_bar_shows_refresh_progress():
    s = srv()
    gate = threading.Event()
    with modules():
        r = _window()
        if not r:
            return
        pg, ctx, errs = r

        def fn():
            s.jobs.set_background_progress("refresh", 2, 8)
            gate.wait(15)
        s.jobs.run_background("refresh", fn)
        try:
            pg.click(".navbtn[data-nav=skills]")
            pg.wait_for_function("() => !document.getElementById('fbPill').hidden", timeout=10000)
            pill = pg.evaluate("() => document.getElementById('fbPill').textContent")
            assert pill == "Adding scores to older reviews: 2 of 8", pill
            w = pg.evaluate("() => document.querySelector('#fbBar i').style.width")
            assert w == "25%", w
            gate.set()
            s.jobs.wait_background(5)
            # when it finishes, the bar clears and the page reloads with the new scores
            pg.wait_for_function("() => document.getElementById('fbPill').hidden", timeout=10000)
            assert pg.evaluate("() => document.getElementById('frame').src.endsWith('#skills')")
            assert not errs, errs
        finally:
            gate.set()
            ctx.close()


def test_window_session_buttons_say_practice_and_qualifying():
    b = _browser()
    if not b:
        return
    sessions = [
        {"key": "a", "event": "EVP", "track_id": 1, "car_id": 2, "track": "Roval", "car": "GT4", "type": 1, "session": "Practice",
         "start": "2026-10-27T10:00:00Z", "laps": 20, "clean_laps": 18, "best": 85.9, "best_clean": 85.9, "review": None},
        {"key": "b", "event": "EVQ", "track_id": 1, "car_id": 2, "track": "Roval", "car": "GT4", "type": 2, "session": "Qualifying",
         "start": "2026-10-27T11:00:00Z", "laps": 4, "clean_laps": 3, "best": 85.5, "best_clean": 85.5, "review": "2026-10-27_q"},
        {"key": "c", "event": "EVR", "track_id": 1, "car_id": 2, "track": "Roval", "car": "GT4", "type": 3, "session": "Race",
         "start": "2026-10-27T12:00:00Z", "laps": 18, "clean_laps": 15, "best": 85.7, "best_clean": 85.7, "review": None}]
    api = {"settings": {"ok": True, "token_saved": True, "version": "2.3.0", "installed": True, "data_folder": "C:/x"},
           "reviews": {"ok": True, "folder": "C:/lib", "groups": []},
           "sessions": {"ok": True, "token": True, "sessions": sessions,
                        "summary": {"synced": True, "last_sync": "2026-10-28T10:00:00Z", "laps": 42, "sessions": 3, "combos": 1}},
           "status": {"state": "idle"}, "sync": {"ok": True, "skipped": True}, "update": {"ok": True, "skipped": True},
           "pages": {"ok": True, "prep": []}, "review": {"ok": True, "id": 7}}
    posts = []
    base_url = "http://debrief.test/s3cret/"
    ctx = b.new_context(viewport={"width": 1280, "height": 860})
    pg = ctx.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))

    def handle(route):
        rel = route.request.url[len(base_url):].split("?")[0]
        if rel in ("", "index.html"):
            return route.fulfill(body=(UI / "index.html").read_text(encoding="utf-8"), content_type="text/html")
        if rel in ("static/app.js", "static/app.css"):
            return route.fulfill(body=(UI / rel[7:]).read_text(encoding="utf-8"),
                                 content_type="text/javascript" if rel.endswith(".js") else "text/css")
        if rel.startswith("api/"):
            if route.request.method == "POST":
                posts.append((rel[4:], json.loads(route.request.post_data or "{}")))
            return route.fulfill(body=json.dumps(api.get(rel[4:], {"ok": True})), content_type="application/json")
        return route.fulfill(status=404, body="")
    pg.route("http://debrief.test/**", handle)
    pg.route("**/fonts.googleapis.com/**", lambda r: r.abort())
    try:
        pg.goto(base_url)
        pg.wait_for_timeout(300)
        pg.click(".navbtn[data-nav=sessions]")
        pg.wait_for_function("() => document.querySelectorAll('#sessList .item.sess').length === 3")
        labels = pg.evaluate("() => [...document.querySelectorAll('#sessList .item.sess')].map(r =>"
                             " [...r.querySelectorAll('button')].map(b => [b.textContent, b.getAttribute('aria-label')]))")
        assert labels[0] == [["Review practice", None]], labels
        assert labels[1] == [["Open review", None], ["Review again", "Review qualifying again"]], labels
        assert labels[2] == [["Review", None]], labels
        pg.click("#sessList .item.sess >> nth=0 >> text=Review practice")
        pg.wait_for_function("() => !document.getElementById('vJob').hidden")
        assert posts and posts[-1] == ("review", {"event": "EVP", "session_type": 1, "track_id": 1, "car_id": 2}), posts
        assert not errs, errs
    finally:
        ctx.close()


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
    if "s" in _SRV:
        _SRV["s"].shutdown()
    if "pw" in _PW:
        try:
            _PW["b"].close()
            _PW["pw"].stop()
        except Exception:  # noqa: BLE001
            pass
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
