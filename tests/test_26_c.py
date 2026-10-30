"""Debrief 2.6 (workstream 26-C): friendly for buddies. README, dev/ launchers, updater wording, installer
publisher, .gitignore, template branding, and the window's onboarding, settings, navigation, sessions paging,
banners and keyboard shortcuts.

    python3 tests/test_26_c.py

The window checks run the real index.html / app.js / app.css in Chromium with every /api/ call answered by
this file (no server, no Garage 61). They're skipped when Playwright or its Chromium isn't installed.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
UI = ROOT / "debrief" / "desktop" / "ui"
TPL = ROOT / "debrief" / "templates"
RELEASES = "github.com/blackmagik232/debrief/releases"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------- packaging, repo layout, docs
def test_installer_publisher_is_the_brand_and_upgrade_ids_unchanged():
    from debrief import brand
    iss = _read(ROOT / "packaging" / "debrief.iss")
    pub = re.search(r"^AppPublisher=(.+)$", iss, re.M).group(1).strip()
    assert pub == brand.PUBLISHER == "Midfield Menace", pub
    # upgrades from 2.5.0 must still install over the old copy: same AppId and install folder
    assert "AppId={{14DA940F-67E3-4A45-8F42-4FC631C59763}" in iss
    assert re.search(r"^DefaultDirName=\{localappdata\}\\Programs\\\{#AppName\}$", iss, re.M)
    assert '#define AppName "Debrief"' in iss
    spec = _read(ROOT / "packaging" / "debrief.spec")
    assert f"StringStruct('CompanyName', '{brand.PUBLISHER}')" in spec
    assert "Overdriver" not in iss and "'Overdriver'" not in spec


def test_source_launchers_live_in_dev():
    names = ["Setup.bat", "Check connection.bat", "Review last race.bat", "Edit settings.bat",
             "Install shortcut.bat", "Open library.bat", "Debrief.vbs"]
    for n in names:
        assert not (ROOT / n).exists(), n
        assert (ROOT / "dev" / n).is_file(), n
    assert _read(ROOT / "dev" / "README.md").startswith("For running from source only.")
    for n in names:
        if n.endswith(".bat"):
            assert 'cd /d "%~dp0.."' in _read(ROOT / "dev" / n), n      # run from the folder above dev
    vbs = _read(ROOT / "dev" / "Debrief.vbs")
    assert "GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))" in vbs
    assert "\\dev\\Debrief.vbs" in _read(ROOT / "dev" / "Install shortcut.bat")
    # nothing else still points at the old root copies
    for p in [ROOT / ".github" / "workflows" / "build.yml", ROOT / "packaging" / "debrief.iss", ROOT / "packaging" / "debrief.spec"]:
        if p.exists():
            assert not re.search(r"(?<![\\/])(Setup|Install shortcut|Review last race)\.bat|Debrief\.vbs", _read(p)), p


def test_readme_for_buddies():
    s = _read(ROOT / "README.md")
    assert s.splitlines()[0] == ("**Just want to use Debrief? Get the installer (step 1 below). "
                                 "Everything else here is for developers.**")
    assert RELEASES in s and "No GitHub account needed." in s and "Don't use the green Code button" in s
    assert "private" not in s.split("## Keeping your token safe")[0].lower().replace("privately", "")
    assert "no background polling" not in s.lower() and "There's no background polling" not in s
    assert "Sync each time Debrief opens" in s and "Check for updates when Debrief opens" in s
    assert "2.2.1" not in s
    assert "**Skills**" in s
    assert "## Midfield Menace on Garage 61" in s and "invite you to the team on Garage 61" in s
    assert "still work if you ever need them" not in s          # the .bat files aren't recommended any more
    assert "Ctrl+R" in s


def test_updater_strings_point_at_the_public_releases():
    s = _read(ROOT / "debrief" / "updater.py")
    assert "may be private" not in s
    assert ("Couldn't find Debrief's releases on GitHub. Try again later, or download the newest installer from "
            in s and RELEASES + "." in s)
    for m in re.finditer(r"release page", s):
        assert RELEASES in s[m.start():m.start() + 80], s[m.start() - 40:m.start() + 80]


def test_gitignore_secrets_and_nothing_tracked_is_ignored():
    gi = _read(ROOT / ".gitignore").splitlines()
    for pat in (".env*", "*.pem", "*.key", "*.ed25519", "signing*", "track-notes/", "team/"):
        assert pat in gi, pat
    try:
        out = subprocess.run(["git", "ls-files", "-i", "-c", "--exclude-standard"], cwd=ROOT, capture_output=True,
                             text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        print("  (git not available: tracked-file check skipped)")
        return
    if out.returncode == 0:
        assert out.stdout.strip() == "", out.stdout


# ---------------------------------------------------------------- templates
def test_templates_are_branded():
    rv, lib, prep, team = (_read(TPL / n) for n in ("review.html", "library.html", "prep.html", "team.html"))
    assert "<title>Midfield Menace · Race Review</title>" in rv
    assert "<title>Midfield Menace · Library</title>" in lib
    assert "<title>Midfield Menace · Prep</title>" in prep
    assert "<title>Midfield Menace · Team hub</title>" in team
    # share card footer: MIDFIELD MENACE · DEBRIEF (from the brand, upper-cased)
    assert "x.fillText((TEAM_NAME+' · Debrief').toUpperCase(),W-64,H-48)" in rv
    assert "GARAGE 61 TELEMETRY" not in rv
    # the Team page never shows a bare slug as its heading
    assert '<h1 id="title">Midfield Menace</h1>' in team
    assert "TEAM.name.trim().toLowerCase()!==String(TEAM.slug||'').toLowerCase()" in team
    # join panels when Garage 61 says you're not on the team
    for s in (team, prep):
        assert "membership==='not_member'" in s and "join_title" in s and "join_text" in s
    for s in (rv, lib, prep, team):
        assert "debrief-key" in s                       # Ctrl+R / Ctrl+, from inside a page reach the window
        assert "Midfield Menace" in s


def test_team_template_join_panel_in_browser():
    b = _browser()
    if not b:
        return
    data = {"version": 1, "team": {"slug": "midfield-menace", "name": "midfield-menace"}, "me": {"name": "You", "key": "me"},
            "membership": "not_member", "combos": [], "requests": 0,
            "brand": {"team_name": "Midfield Menace", "join_title": "Join Midfield Menace on Garage 61",
                      "join_text": "Ask a teammate to invite you.", "team_url": "https://garage61.net"}}
    html = _read(TPL / "team.html").replace("window.TEAM=/*TEAM*/null;", "window.TEAM=" + json.dumps(data) + ";")
    pg = b.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.route("**/fonts.g*/**", lambda r: r.abort())
    try:
        pg.set_content(html)
        info = pg.evaluate("""() => ({title: document.title, h1: document.getElementById('title').textContent,
            join: !document.getElementById('join').hidden, jt: document.getElementById('joinTitle').textContent,
            kings: document.getElementById('kings').hidden, h2h: document.getElementById('h2h').hidden})""")
        assert info == {"title": "Midfield Menace · Team hub", "h1": "Midfield Menace", "join": True,
                        "jt": "Join Midfield Menace on Garage 61", "kings": True, "h2h": True}, info
        assert not errs, errs
    finally:
        pg.close()


# ---------------------------------------------------------------- window (static)
def test_window_static_bits():
    js, html = _read(UI / "app.js"), _read(UI / "index.html")
    assert "<title>Debrief · Midfield Menace</title>" in html
    assert "let teamSlug='midfield-menace'" not in js and "api('brand')" in js
    # one heartbeat source: the worker; the main thread pings every 5 s only if the worker can't start
    assert js.count("setInterval(ping,5000)") == 1 and "catch(e){setInterval(ping,5000)}" in js
    # first screen loads side by side
    assert "await Promise.all([loadSettings(),loadBrand(),loadReviews(),loadSessions()])" in js
    assert "Set up Settings first." not in js and "Finish setup on the welcome screen first." in js
    assert "Waiting for Garage 61's email? Close Debrief. These steps will be here when you come back." in html
    assert "Pause sync (carries on next time)" in html
    assert ">All sessions<" in html and ">Saved reviews<" in html and ">Past reviews<" not in html
    assert html.count("request a personal API token") >= 2          # guide and Settings say the same thing
    assert 'id="pIracing" hidden' in html                           # credentials panel hidden; keys still work
    assert 'id="btnCheck">Save and check<' in html and "Save token" not in html and ">Check connection<" not in html
    assert 'data-nav="sessions">Open Sessions<' in html
    assert 'title="Review last race (Ctrl+R)"' in html and 'title="Settings (Ctrl+,)"' in html


# ---------------------------------------------------------------- window (browser, mocked API)
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


BASE = "http://debrief.test/s3cret/"


def _sess(i):
    return {"key": f"k{i}", "event": f"EV{i}", "track_id": 1, "car_id": 2, "track": "Roval", "car": "GT4",
            "type": 3, "session": "Race", "start": f"2026-10-{(i % 27) + 1:02d}T12:00:00Z", "laps": 10,
            "clean_laps": 9, "best": 85.0 + i / 100, "best_clean": 85.0 + i / 100, "review": None}


ALL_SESS = [_sess(i) for i in range(150)]


def _api(token=True, reviews=True, **over):
    groups = [{"track": "Roval", "car": "GT4", "reviews": [
        {"dir": "2026-10-20_old", "date": "2026-10-20", "started_at": "2026-10-20T12:00:00Z", "best": "1:26.000", "session": "Race", "laps": 12},
        {"dir": "2026-10-28_new", "date": "2026-10-28", "started_at": "2026-10-28T12:00:00Z", "best": "1:25.500", "session": "Race", "laps": 14}]},
        {"track": "Spa", "car": "GT3", "reviews": [
        {"dir": "2026-10-25_spa", "date": "2026-10-25", "started_at": "2026-10-25T12:00:00Z", "best": "2:18.000", "session": "Race", "laps": 9}]}]
    a = {"settings": {"ok": True, "token_saved": token, "token_hint": "…abcd", "version": "2.6.0", "installed": True,
                      "data_folder": "C:/x", "team_slug": "midfield-menace", "max_reference_laps": 2},
         "brand": {"app_name": "Debrief", "publisher": "Midfield Menace", "team_slug": "midfield-menace",
                   "team_name": "Midfield Menace", "team_short": "MM", "team_url": "https://garage61.net",
                   "join_title": "Join Midfield Menace on Garage 61", "join_text": "Ask a teammate to invite you."},
         "reviews": {"ok": True, "folder": "C:/lib", "groups": groups if reviews else []},
         "status": {"state": "idle"}, "sync": {"ok": True, "skipped": True}, "update": {"ok": True, "skipped": True},
         "pages": {"ok": True, "prep": []},
         "teams": {"ok": True, "token": token, "team_slug": "midfield-menace", "teams": [{"slug": "other", "name": "Other"}]},
         "weekly": {"ok": True, "available": True, "schedule": True, "membership": "not_member", "plan": {"series": []}}}
    a.update(over)
    return a


def _window(api, viewport=(1280, 860)):
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
            return route.fulfill(body=_read(UI / "index.html"), content_type="text/html")
        if path in ("static/app.js", "static/app.css"):
            return route.fulfill(body=_read(UI / path[7:]), content_type="text/javascript" if path.endswith(".js") else "text/css")
        if path.startswith("lib/"):
            return route.fulfill(body="<!doctype html><title>Page</title><body>page</body>", content_type="text/html")
        if path.startswith("api/"):
            name = path[4:]
            body = json.loads(req.post_data or "{}") if req.method == "POST" else None
            calls.append((req.method, name, qs, body))
            if name == "sessions":
                q = dict(x.split("=", 1) for x in qs.split("&") if "=" in x)
                off, lim = int(q.get("offset", 0)), int(q.get("limit", 60))
                return route.fulfill(body=json.dumps({"ok": True, "token": True, "sessions": ALL_SESS[off:off + lim],
                                                      "total": len(ALL_SESS), "summary": {"synced": True, "sessions": 150, "laps": 1500, "combos": 1,
                                                                                        "last_sync": "2026-10-29T10:00:00Z"}}),
                                     content_type="application/json")
            v = api.get(name, {"ok": True})
            if name == "settings" and req.method == "POST":     # a save answers {ok, settings}
                v = {"ok": True, "settings": api["settings"]}
            if callable(v):
                v = v(body)
            return route.fulfill(body=json.dumps(v), content_type="application/json")
        return route.fulfill(status=404, body="")
    pg.route("http://debrief.test/**", handle)
    pg.route("**/fonts.g*/**", lambda r: r.abort())
    pg.goto(BASE)
    return ctx, pg, errs, calls


def test_window_first_run_gate_and_check_messages():
    st = {"n": 0}

    def status(_):
        st["n"] += 1
        return st["status"]
    api = _api(token=False, reviews=False, status=status)
    st["status"] = {"state": "idle"}
    r = _window(api)
    if not r:
        return
    ctx, pg, errs, calls = r
    try:
        pg.wait_for_function("() => !document.getElementById('vWelcome').hidden")
        btn = pg.evaluate("() => { const b = document.getElementById('btnReview'); return [b.getAttribute('aria-disabled'), b.title] }")
        assert btn == ["true", "Finish step 3 first."], btn
        # pressing it (or Ctrl+R) starts nothing without a token
        pg.click("#btnReview", force=True)          # aria-disabled: still clickable, so the tooltip shows
        pg.keyboard.press("Control+r")
        pg.wait_for_timeout(200)
        assert not [c for c in calls if c[0] == "POST" and c[1] == "review"], calls
        assert pg.evaluate("() => document.title") == "Debrief · Midfield Menace"
        # a check that fails for another reason shows the server's message, not "paste it again"
        api["settings"] = dict(api["settings"], token_saved=True)
        api["check"] = {"ok": True, "id": 1}
        st["status"] = {"state": "error", "kind": "check", "id": 1, "error": "Couldn't reach Garage 61. Check your internet connection.", "steps": []}
        pg.fill("#gTok", "tok123")
        pg.click("#gTokSave")
        pg.wait_for_function("() => document.getElementById('gTokMsg').classList.contains('bad')")
        assert pg.text_content("#gTokMsg") == "Couldn't reach Garage 61. Check your internet connection."
        # a 401 does say to paste it again
        api["check"] = {"ok": True, "id": 2}
        st["status"] = {"state": "error", "kind": "check", "id": 2, "error": "Garage 61 rejected the token (401). Paste it again in Settings.", "steps": []}
        pg.fill("#gTok", "tok456")
        pg.click("#gTokSave")
        pg.wait_for_function("() => /paste it again/.test(document.getElementById('gTokMsg').textContent)")
        # success: the new wording, and info-level lines stay neutral
        api["check"] = {"ok": True, "id": 3}
        st["status"] = {"state": "done", "kind": "check", "id": 3, "result": {"lines": [
            "Token works. Account: me", {"text": "Telemetry download: NOT available for your account", "level": "info"}]}}
        pg.fill("#gTok", "tok789")
        pg.click("#gTokSave")
        pg.wait_for_function("() => document.getElementById('gTokMsg').textContent.startsWith('Connection works')")
        assert pg.text_content("#gTokMsg") == ("Connection works. Debrief is downloading your history in the background. "
                                               "You can review your last race right now.")
        cls = pg.evaluate("() => [...document.querySelectorAll('#gChkOut li')].map(l => l.className)")
        assert cls == ["", "info"], cls
        assert pg.evaluate("() => document.getElementById('btnReview').getAttribute('aria-disabled')") == "false"
        assert not errs, errs
    finally:
        ctx.close()


def test_window_opens_on_last_race_shortcuts_and_banners():
    api = _api(review=lambda body: {"ok": False, "error": "Garage 61 is busy."})
    r = _window(api)
    if not r:
        return
    ctx, pg, errs, calls = r
    try:
        pg.wait_for_function("() => !document.getElementById('lastRace').hidden", timeout=10000)
        assert pg.text_content("#lrWhat") == "Roval · GT4"
        nav = pg.evaluate("() => [...document.querySelectorAll('[aria-current=page]')].map(b => b.dataset.nav)")
        assert nav == ["library"], nav
        # Season / Skills are tabs of the Library page; their deep links still work
        pg.click(".navbtn[data-nav=season]")
        pg.wait_for_function("() => document.getElementById('frame').src.endsWith('#season')")
        assert pg.evaluate("() => document.getElementById('libGroup').classList.contains('on')")
        pg.click(".navbtn[data-nav=library]")
        pg.wait_for_function("() => !document.getElementById('lastRace').hidden")
        pg.click("#lrOpen")
        pg.wait_for_function("() => document.getElementById('frame').src.includes('2026-10-28_new')")
        # Ctrl+, = Settings; the extras start folded and the iRacing panel isn't shown
        pg.keyboard.press("Control+Comma")
        pg.wait_for_function("() => !document.getElementById('vSettings').hidden")
        st = pg.evaluate("""() => ({extras: document.getElementById('pExtras').open, ir: document.getElementById('pIracing').hidden,
            team: document.getElementById('teamLine').textContent, adv: document.getElementById('teamAdv').open})""")
        assert st["extras"] is False and st["ir"] is True and st["adv"] is False, st
        pg.wait_for_function("() => document.getElementById('teamLine').textContent.includes('not on the team yet')")
        assert pg.text_content("#teamLine") == "Team: Midfield Menace \u2014 not on the team yet (join on Garage 61)"
        # Ctrl+R = Review last race (and not a reload); the error banner stays until it's closed
        pg.evaluate("() => { window.__alive = 1 }")
        pg.keyboard.press("Control+r")
        pg.wait_for_function("() => !document.getElementById('banner').hidden")
        assert pg.evaluate("() => window.__alive") == 1
        assert [c for c in calls if c[0] == "POST" and c[1] == "review"]
        assert pg.text_content("#bannerText") == "Garage 61 is busy."
        pg.wait_for_timeout(5600)
        assert pg.evaluate("() => !document.getElementById('banner').hidden")
        pg.click("#bannerClose")
        assert pg.evaluate("() => document.getElementById('banner').hidden")
        # the init loads ran side by side: brand, settings, reviews and sessions all asked before status
        first = [c[1] for c in calls if c[1] not in ("ping", "bye")][:6]
        assert {"brand", "settings", "reviews", "sessions"} <= set(first[:4]), first
        assert not errs, errs
    finally:
        ctx.close()


def test_window_sessions_page_by_page_and_team_join():
    r = _window(_api())
    if not r:
        return
    ctx, pg, errs, calls = r
    try:
        pg.wait_for_function("() => !document.getElementById('lastRace').hidden", timeout=10000)
        pg.click(".navbtn[data-nav=sessions]")
        pg.wait_for_function("() => document.querySelectorAll('#sessList .item.sess').length === 60")
        assert pg.text_content("#sessCount") == "Showing 60 of 150"
        pg.click("#sessMore")
        pg.wait_for_function("() => document.querySelectorAll('#sessList .item.sess').length === 120")
        pg.click("#sessMore")
        pg.wait_for_function("() => document.querySelectorAll('#sessList .item.sess').length === 150")
        assert pg.evaluate("() => document.getElementById('sessMoreRow').hidden")
        qs = [c[2] for c in calls if c[1] == "sessions"]
        assert "offset=60&limit=60" in qs and "offset=120&limit=60" in qs, qs
        assert all("limit=" in q for q in qs), qs                       # never everything at once
        # Team: not on the team -> the join panel (no team page saved yet in this mock)
        pg.click(".navbtn[data-nav=team]")
        pg.wait_for_function("() => !document.getElementById('teamJoin').hidden", timeout=10000)
        assert pg.text_content("#teamTitle") == "Midfield Menace"
        assert pg.text_content("#teamJoin .joinTitle") == "Join Midfield Menace on Garage 61"
        assert not errs, errs
    finally:
        ctx.close()


def test_window_job_error_offers_sessions():
    st = {"s": {"state": "idle"}}
    api = _api(review={"ok": True, "id": 9}, status=lambda _: st["s"])
    r = _window(api)
    if not r:
        return
    ctx, pg, errs, calls = r
    try:
        pg.wait_for_function("() => !document.getElementById('lastRace').hidden", timeout=10000)
        st["s"] = {"state": "error", "kind": "review", "id": 9, "error": "No new race found.", "steps": []}
        pg.click("#btnReview")
        pg.wait_for_function("() => !document.getElementById('jobErr').hidden")
        btns = pg.evaluate("() => [...document.querySelectorAll('#jobErr button')].map(b => b.textContent)")
        assert btns == ["Try again", "Open Sessions", "Settings"], btns
        pg.click("#jobErr >> text=Open Sessions")
        pg.wait_for_function("() => !document.getElementById('vSessions').hidden")
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
