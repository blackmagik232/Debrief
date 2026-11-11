"""Debrief 2.30.4 What's new: the CHANGELOG reader, the release gate, shown once after an update (never on a new
install), Help > What's new, and the dialog in a browser (Playwright, skipped when missing; the API is mocked).
Run: python3 tests/ci_run.py tests/test_2304_whatsnew.py"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from debrief import whatsnew as W, __version__  # noqa: E402
from debrief import app  # noqa: E402

UI = ROOT / "debrief" / "desktop" / "ui"
BASE = "http://debrief.test/s3cret/"

SAMPLE = """# Changelog

## 9.1.0 "Shiny"

Plain words for you, long enough to count as a real note.

- **Bold thing** does a thing
  with a wrapped line.
  - a sub point with `code`
- Second point.

### For the record

- `debrief/x.py` changed. Not proven yet: everything.

## 9.0.0

Old entry with no record part at all, but it is still long enough.
"""


def test_entries_split_user_and_record():
    es = W.entries(SAMPLE)
    assert [e["version"] for e in es] == ["9.1.0", "9.0.0"]
    assert es[0]["name"] == "Shiny"
    assert "x.py" not in es[0]["user"] and "x.py" in es[0]["record"]
    assert es[1]["record"] == "" and es[1]["user"].startswith("Old entry")


def test_blocks_are_text_only_and_join_wrapped_lines():
    b = W.blocks(W.entry_for("9.1.0", SAMPLE)["user"])
    assert [x["kind"] for x in b] == ["p", "li", "li", "li"]
    assert b[1]["parts"][0] == ["Bold thing", "b"] and b[1]["parts"][-1][0].endswith("with a wrapped line.")
    assert b[2]["depth"] == 1 and ["code", "code"] in b[2]["parts"]
    # nothing is ever HTML: a tag in the changelog comes through as text
    t = W.blocks("<img src=x onerror=alert(1)> hi there")
    assert t[0]["parts"] == [["<img src=x onerror=alert(1)> hi there", ""]]


def test_release_gate():
    assert W.problems(SAMPLE, "9.1.0") == []
    assert any("9.2.0" in p for p in W.problems(SAMPLE, "9.2.0"))         # bumped the version, no entry
    thin = SAMPLE.replace("Plain words for you, long enough to count as a real note.\n\n- **Bold thing** does a thing\n"
                          "  with a wrapped line.\n  - a sub point with `code`\n- Second point.\n", "Hi.\n")
    assert any("no What's new text" in p for p in W.problems(thin, "9.1.0"))
    leaky = SAMPLE.replace("Second point.", "Changed `debrief/y.py`.")
    assert any("code files" in p for p in W.problems(leaky, "9.1.0"))


def test_this_release_has_whats_new():
    """The real gate: the CHANGELOG at the top must be for this version, with user text."""
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert W.problems(text, __version__) == [], W.problems(text, __version__)


def test_release_notes_have_user_part_first():
    n = W.release_notes(SAMPLE, "9.1.0")
    assert n.startswith("## What's new in Debrief 9.1.0 \"Shiny\"")
    assert n.index("Bold thing") < n.index("### For the record") < n.index("x.py")


def test_shown_once_after_update_never_on_new_install():
    with tempfile.TemporaryDirectory() as d:
        old = app.HERE
        app.HERE = Path(d)
        try:
            # brand-new install: nothing shown, version recorded
            assert W.should_show("9.1.0", has_setup=False) is False
            assert W.should_show("9.1.0", has_setup=True) is False
            # an update to a newer version: shown once
            assert W.should_show("9.2.0", has_setup=True) is True
            W.mark_seen("9.2.0")
            assert W.should_show("9.2.0", has_setup=True) is False
            # someone who already had Debrief set up before What's new existed: shown
            os.remove(Path(d) / "whatsnew.json")
            assert W.should_show("9.2.0", has_setup=True) is True
            # a broken file counts as not seen, and is never an error
            (Path(d) / "whatsnew.json").write_text("{nope", encoding="utf-8")
            assert W.should_show("9.2.0", has_setup=True) is True
        finally:
            app.HERE = old


def test_payload_marks_seen_when_shown():
    with tempfile.TemporaryDirectory() as d:
        old = app.HERE
        app.HERE = Path(d)
        try:
            W.mark_seen("0.0.1")
            p = W.payload(__version__, auto=True, has_setup=True)
            assert p["show"] is True and p["blocks"]
            assert W.payload(__version__, auto=True, has_setup=True)["show"] is False
            assert W.payload(__version__, auto=False, has_setup=True)["show"] is True   # Help asks: always
        finally:
            app.HERE = old


# ---------------------------------------------------------------- the dialog in a browser
def _page(api_whatsnew):
    try:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        b = pw.chromium.launch()
    except Exception:  # noqa: BLE001
        print("  SKIPPED: Playwright/Chromium not available")
        return None
    ctx = b.new_context(viewport={"width": 1280, "height": 860})
    pg = ctx.new_page()
    errs, asked = [], []
    pg.on("pageerror", lambda e: errs.append(str(e)))

    def handle(route):
        rel = route.request.url[len(BASE):]
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
            if path == "api/whatsnew":
                asked.append(qs)
                return route.fulfill(body=json.dumps(api_whatsnew(qs)), content_type="application/json")
            return route.fulfill(body=json.dumps({"ok": True}), content_type="application/json")
        return route.fulfill(status=404, body="")
    pg.route("http://debrief.test/**", handle)
    pg.route("**/fonts.g*/**", lambda r: r.abort())
    pg.goto(BASE)
    return pw, b, ctx, pg, errs, asked


def _close(r):
    pw, b, ctx = r[0], r[1], r[2]
    ctx.close()
    b.close()
    pw.stop()


def _answer(show):
    e = W.entry_for("9.1.0", SAMPLE)
    return lambda qs: {"ok": True, "show": show if "auto=1" in qs else True, "version": "9.1.0", "name": "Shiny",
                       "blocks": W.blocks(e["user"])}


def test_dialog_opens_after_update_and_closes():
    r = _page(_answer(True))
    if not r:
        return
    pg, errs, asked = r[3], r[4], r[5]
    try:
        pg.wait_for_function("() => document.getElementById('dlgNew').open", timeout=8000)
        assert pg.text_content("#newTitle") == 'What\'s new in Debrief 9.1.0 "Shiny"'
        assert pg.locator("#newBody > ul > li").count() == 2
        assert pg.locator("#newBody li ul li code").count() == 1
        assert pg.locator("#newBody b").first.text_content() == "Bold thing"
        assert "x.py" not in pg.text_content("#newBody")
        pg.click("#newOk")
        assert not pg.evaluate("() => document.getElementById('dlgNew').open")
        assert any("auto=1" in q for q in asked)
        assert not errs, errs
    finally:
        _close(r)


def test_not_shown_when_already_seen_but_help_opens_it():
    r = _page(_answer(False))
    if not r:
        return
    pg, errs = r[3], r[4]
    try:
        pg.wait_for_timeout(1500)
        assert not pg.evaluate("() => document.getElementById('dlgNew').open")
        pg.evaluate("() => document.getElementById('btnHelp').click()")
        pg.wait_for_function("() => document.getElementById('dlgHelp').open")
        pg.click("#helpWhatsNew")
        pg.wait_for_function("() => document.getElementById('dlgNew').open")
        assert not pg.evaluate("() => document.getElementById('dlgHelp').open")
        assert "Second point." in pg.text_content("#newBody")
        assert not errs, errs
    finally:
        _close(r)


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
