"""Debrief 2.31.0 Help: the full guide (contents, search, Learn sections, Every page, grouped Glossary, keys).
Checks the words in help.js are complete and plain, every "Open ..." button names a real sidebar page, nothing in
Help is ever HTML, and the window in a real browser against the real server (Playwright, skipped when missing).
Run: python3 tests/ci_run.py tests/test_231_help.py"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="debrief-231h-"))
os.environ["DEBRIEF_SETTINGS"] = str(TMP / "settings.toml")
os.environ["DEBRIEF_CACHE_DIR"] = str(TMP / "cache")
os.environ["DEBRIEF_SCHEDULE_DIR"] = str(TMP / "schedule")
os.environ.setdefault("DEBRIEF_UPDATE_STATE", str(TMP / "update.json"))
(TMP / "lib").mkdir()
Path(os.environ["DEBRIEF_SETTINGS"]).write_text(f"library_folder = '{(TMP / 'lib').as_posix()}'\n", encoding="utf-8")

UI = ROOT / "debrief" / "desktop" / "ui"
JS = (UI / "app.js").read_text(encoding="utf-8")
HJS = (UI / "help.js").read_text(encoding="utf-8")
HTML = (UI / "index.html").read_text(encoding="utf-8")
NAVS = set(re.findall(r'class="navbtn[^"]*" data-nav="([a-z]+)"', HTML))


def guide() -> dict | None:
    """help.js's data, read by Node when it is installed (it is on CI and the dev box)."""
    code = "global.window={};require(" + json.dumps(str(UI / "help.js")) + ");process.stdout.write(JSON.stringify(window.DEBRIEF_HELP))"
    try:
        r = subprocess.run(["node", "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        print("  SKIPPED: node not available")
        return None
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def walk(x):
    """every string in the guide"""
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for v in x.values():
            yield from walk(v)
    elif isinstance(x, list):
        for v in x:
            yield from walk(v)


def test_files_and_loading_order():
    assert '<script src="static/help.js"></script>\n<script src="static/app.js"></script>' in HTML, "help.js loads before app.js"
    assert '"help.js"' in (ROOT / "debrief" / "desktop" / "server.py").read_text(encoding="utf-8"), "the server serves help.js"
    assert '("static/help.js", b"DEBRIEF_HELP")' in (ROOT / "debrief" / "desktop" / "__init__.py").read_text(encoding="utf-8"), "the self-test checks it"
    # the old ids the rest of the app and older tests use are all still there
    for i in ('id="dlgHelp"', 'id="helpBody"', 'id="helpClose"', 'id="helpWhatsNew"', 'id="helpFirstRun"', 'id="helpSearch"'):
        assert i in HTML, i
    for k in ("start", "improve", "car", "race", "tour", "numbers", "words", "keys", "credits"):
        assert f'data-help="{k}"' in HTML, k
    # nothing in Help is put in as HTML
    i, j = JS.index("/* 2.31.0 Help:"), JS.index("/* 2.30.4 What's new")
    assert "innerHTML" not in JS[i:j] and "insertAdjacentHTML" not in JS[i:j]
    assert "innerHTML" not in HJS


def test_guide_is_complete_and_plain():
    g = guide()
    if g is None:
        return
    ids = [s["id"] for s in g["sections"]]
    assert ids == ["start", "data", "improve", "car", "race", "numbers"], ids   # 2.35: Where your data comes from
    for s in g["sections"]:
        assert s["title"] and s["group"] and len(s["blocks"]) >= 5, s["id"]
        assert "lede" in s["blocks"][0], s["id"] + " starts with a lede"
    text = " ".join(walk(g))
    # the guide teaches how to get faster and know the car, not only what the buttons do
    for want in ("The Debrief loop", "Pick one thing", "Where the time hides", "Compare two laps", "Traps to avoid",
                 "Weight moves, grip follows", "Pushes or rotates?", "Changing the setup without fooling yourself",
                 "Tyre pressures", "Starts and incidents", "Fuel and stints", "How Debrief checks itself"):
        assert want in text, want
    # bold markers pair up, and there is no HTML or code jargon in the words
    for t in walk(g):
        assert t.count("**") % 2 == 0, t
        assert not re.search(r"<[A-Za-z/!]", t), t
    for bad in ("bootstrap", "Bonferroni", "p-value", "regression", ".py", "API"):
        assert bad not in text, bad
    # every Open ... button goes to a real sidebar page (or the setup checklist / What's new)
    gos = set()

    def find(x):
        if isinstance(x, dict):
            if "go" in x:
                gos.add(x["go"])
                assert x.get("label") or x in g["pages"], x
            if "hgo" in x:
                assert x["hgo"] in ("firstrun", "whatsnew"), x
            for v in x.values():
                find(v)
        elif isinstance(x, list):
            for v in x:
                find(v)
    find(g)
    assert gos and gos <= NAVS, gos - NAVS
    # Every page has one card for every sidebar page
    assert {p["go"] for p in g["pages"]} == NAVS, NAVS ^ {p["go"] for p in g["pages"]}
    for p in g["pages"]:
        assert p["t"] and p["what"] and p["when"], p
    # the glossary's groups name only real words, each once
    words = set(re.findall(r"\['((?:[^'\\]|\\.)*)','", JS[JS.index("words:["):JS.index("keys:[['Ctrl+Shift+M'")]))
    words |= set(re.findall(r"HELP\.words\.push\(\['((?:[^'\\]|\\.)*)','", JS))
    words = {w.replace("\\'", "'") for w in words}
    seen = []
    for name, L in g["groups"]:
        seen += L
        for t in L:
            assert t in words or ("['" + t.replace("'", "\\'") + "','") in JS, f"glossary group {name}: {t} is not a word"
    assert len(seen) == len(set(seen)), "a word is in two groups"


# ---------------------------------------------------------------- the window, in a browser against the real server
def _server():
    from debrief.desktop import server as S
    from debrief import app
    app.LOG_FILE = str(TMP / "debrief.log")
    s = S.DebriefServer(heartbeat_timeout=3600)
    threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
    return s


def test_help_window():
    try:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        b = pw.chromium.launch()
    except Exception:  # noqa: BLE001
        print("  SKIPPED: Playwright/Chromium not available")
        return
    s = _server()
    try:
        for theme in ("light", "dark"):
            pg = b.new_page(viewport={"width": 1366, "height": 800}, color_scheme=theme)
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
            pg.goto(s.url)
            pg.wait_for_timeout(1200)
            pg.evaluate("() => document.querySelectorAll('dialog[open]').forEach(d => d.close())")
            pg.click("#btnHelp")
            pg.wait_for_selector("#dlgHelp[open]")
            # it opens on Start here, as big as the window allows, with the contents marked
            assert pg.get_attribute("[data-help=start]", "aria-pressed") == "true"
            box = pg.locator("#dlgHelp").bounding_box()
            assert box["width"] > 1000 and box["height"] > 700, box
            assert "START HERE" in pg.inner_text("#helpBody .hhead h2").upper()
            assert pg.locator("#helpBody .hstep").count() >= 4
            # every section draws, scrolls inside the window and has no empty blocks
            for k, want in (("improve", "The Debrief loop"), ("car", "Pushes or rotates?"), ("race", "Fuel and stints"),
                            ("numbers", "How Debrief checks itself"), ("tour", "Features in detail"),
                            ("words", "Laps and pace"), ("keys", "While you drive"), ("credits", "Debrief")):
                pg.click(f"[data-help={k}]")
                assert want in pg.text_content("#helpBody"), (k, want)
                assert pg.get_attribute(f"[data-help={k}]", "aria-pressed") == "true"
                assert pg.evaluate("() => document.getElementById('helpBody').scrollTop") == 0
                assert pg.evaluate("() => [...document.querySelectorAll('#helpBody p,#helpBody h3,#helpBody h4,#helpBody dd')].every(e => e.textContent.trim() !== '' || e.closest('.hpara'))")
            # the old texts the other tests look for are all still there
            pg.click("[data-help=keys]")
            keys = pg.text_content("#helpBody")
            for want in ("Ctrl+1 to Ctrl+9", "Ctrl+1: Today", "Ctrl+Enter: Review a session", "F1 or ?: This list of keys", "Ctrl+Shift+M"):
                assert want in keys, want
            pg.click("[data-help=words]")
            words = pg.text_content("#helpBody")
            for want in ("Corner coach", "Best possible lap", "Trail braking", "Pushes, Rotates", "Teammate's lap"):
                assert want in words, want
            assert pg.locator("#helpBody dt", has_text=re.compile(r"^Reference lap$")).count() == 1, "a word shows once"
            # bold comes through as bold, never as stars
            pg.click("[data-help=start]")
            assert "**" not in pg.text_content("#helpBody") and pg.locator("#helpBody b").count() > 3
            # search finds words, keys and guide parts; a result opens its section at that place
            pg.fill("#helpSearch", "trail braking")
            pg.wait_for_selector("#helpBody .hresult")
            assert pg.locator("[data-help][aria-pressed=true]").count() == 0
            assert pg.locator("#helpBody .hresult mark").count() >= 1
            pg.locator("#helpBody .hresult", has_text="Glossary").first.click()
            assert pg.get_attribute("[data-help=words]", "aria-pressed") == "true" and pg.input_value("#helpSearch") == ""
            pg.fill("#helpSearch", "zzqqxx")
            assert "Nothing found" in pg.text_content("#helpBody")
            pg.press("#helpSearch", "Escape")       # Esc clears the search first, the window stays open
            assert pg.evaluate("() => document.getElementById('dlgHelp').open") and pg.input_value("#helpSearch") == ""
            pg.fill("#helpSearch", "Ctrl+Shift+P")
            assert "Ask the coach how your pace is" in pg.text_content("#helpBody")
            # J and ? typed in the search box stay in the search box
            pg.fill("#helpSearch", "")
            pg.type("#helpSearch", "j?")
            assert pg.input_value("#helpSearch") == "j?"
            # an Open button closes Help and opens that page
            pg.click("[data-help=car]")
            pg.locator("#helpBody .hgo[data-nav=setuplab]").first.click()
            pg.wait_for_function("() => !document.getElementById('dlgHelp').open && !document.getElementById('vSetupLab').hidden")
            # F1 opens the keys
            pg.keyboard.press("F1")
            pg.wait_for_function("() => document.getElementById('dlgHelp').open && document.querySelector('[data-help=keys]').getAttribute('aria-pressed') === 'true'")
            pg.keyboard.press("Escape")
            pg.wait_for_function("() => !document.getElementById('dlgHelp').open")
            # a narrow window: the contents become a row of tabs above the text
            pg.set_viewport_size({"width": 760, "height": 700})
            pg.evaluate("() => document.getElementById('btnHelp').click()")
            pg.wait_for_selector("#dlgHelp[open]")
            side, body = pg.locator(".helpside").bounding_box(), pg.locator("#helpBody").bounding_box()
            assert side["y"] + side["height"] <= body["y"] + 1, (side, body)
            pg.keyboard.press("Escape")
            assert not errs, errs
            pg.close()
    finally:
        b.close()
        pw.stop()
        s.shutdown()


if __name__ == "__main__":
    tests = [(n, f) for n, f in list(globals().items()) if n.startswith("test_")]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok    {name}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {name}: {e}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
