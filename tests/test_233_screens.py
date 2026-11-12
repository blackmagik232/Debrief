"""Debrief 2.33 "Fits any screen": the sidebar fits a 720 px window, empty pages say each step once, Rebuild all, and a
shorter setup checklist without the voice test.

    python3 tests/ci_run.py tests/test_233_screens.py

Rebuild all through the real server's helpers with a stand-in cmd_review (no network); the window in Chromium against
the real server and the fixture library (skipped when Playwright is missing). utf-8 everywhere, temp folders only.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
TMP = Path(tempfile.mkdtemp(prefix="debrief-233-"))
LIB = TMP / "lib"
LIB.mkdir()
os.environ.update(DEBRIEF_SETTINGS=str(TMP / "settings.toml"), DEBRIEF_CACHE_DIR=str(TMP / "cache"),
                  DEBRIEF_SCHEDULE_DIR=str(TMP / "schedule"), DEBRIEF_UPDATE_STATE=str(TMP / "update.json"),
                  DEBRIEF_NO_NETWORK="1")
(TMP / "settings.toml").write_text(f"token = \"g61-test-token-ABCDEFGHIJKLMNOP\"\nlibrary_folder = '{LIB.as_posix()}'\n"
                                   "api_base = \"http://127.0.0.1:9/api/v1/\"\nsync_on_open = false\n", encoding="utf-8")

import make_fixture_library as MFL  # noqa: E402
from debrief import app, firstrun, profile, today  # noqa: E402
from debrief.desktop import server as S  # noqa: E402

MFL.main(str(LIB))
app.LOG_FILE = str(TMP / "debrief.log")
FAILS = []


def check(ok, what):
    print(("ok   " if ok else "FAIL ") + what)
    if not ok:
        FAILS.append(what)


def test_rebuild_all():
    cfg = app.load_settings()
    items = S.rebuild_all_items(cfg)
    check(len(items) == 9 and all(p.get("rebuild") is True and p.get("event") for _n, p in items),
          f"every fixture review saved without lap traces is listed, as rebuild jobs ({len(items)})")
    dates = [n[:10] for n, _p in items]
    check(dates == sorted(dates, reverse=True), "newest first")
    calls, msgs = [], []
    real = app.cmd_review

    def fake(cfg, event, **kw):
        calls.append((event, kw.get("rebuild")))
        if len(calls) == 2:
            raise ValueError("this race's laps are gone from Garage 61")
        return str(LIB / "reviews" / "x" / "index.html")
    app.cmd_review = fake
    try:
        r = S._rebuild_all(cfg, items[:4], lambda f, m: msgs.append(m))
    finally:
        app.cmd_review = real
    check(len(calls) == 4 and all(rb is True for _e, rb in calls), "each one rebuilt like its own Rebuild button")
    check(len(r["rebuilt"]) == 3 and len(r["failed"]) == 1 and r["total"] == 4, "one that fails is named, the rest carry on")
    check(any("Rebuilding review 2 of 4" in m for m in msgs) and msgs[-1] == "Rebuilt 3 of 4 reviews.", "says where it is")

    calls.clear()

    def offline(cfg, event, **kw):
        calls.append(event)
        raise SystemExit("Debrief can't reach Garage 61. Check your internet connection, then try again.")
    app.cmd_review = offline
    try:
        try:
            S._rebuild_all(cfg, items[:5], lambda f, m: None)
            stopped = None
        except SystemExit as e:
            stopped = str(e)
    finally:
        app.cmd_review = real
    check(len(calls) == 1 and stopped and "Rebuild all stopped there" in stopped,
          "no connection: stops after the first instead of trying every review")

    def cancel(f, m):
        if "review 2" in m:
            raise S.JobCancelled(S.CANCELLED_MSG)
    app.cmd_review = lambda cfg, event, **kw: str(LIB / "x.html")
    try:
        try:
            S._rebuild_all(cfg, items[:4], cancel)
            cancelled = False
        except S.JobCancelled:
            cancelled = True
    finally:
        app.cmd_review = real
    check(cancelled, "Cancel stops it between reviews")
    check("rebuildall" in S.JOB_TITLES and "rebuildall" in S.CANCELLABLE, "a job with a title, and it can be cancelled")
    check("Rebuild all" in profile.why_line({"no_traces": 2}, 2), "the Progress line names Rebuild all")


def test_checklist():
    st = firstrun.steps({})
    check([s["id"] for s in st] == ["folder", "record", "telemetry", "session", "token"], "five steps, no voice test")
    f = {s["id"]: s for s in firstrun.steps({"folder": False, "folder_path": "D:/iRacing/telemetry"})}["folder"]
    check("D:/" not in f["text"] and "D:/iRacing/telemetry" in f["detail"], "the folder path is behind Why?")
    card = today._from_setup({"steps": [f]})
    check(card["why"] == f["detail"], "Today's Why? shows the folder looked in")
    card = today._from_setup({"steps": [{"id": "session", "done": False, "title": "t", "text": "x", "action": "practice"}]})
    check(card["why"] == "The next setup step.", "a step with no detail keeps the plain Why?")


def _close_whats_new(pg):
    """What's new can open after an update (its seen-state file is shared with other tests running at the same time)."""
    pg.wait_for_timeout(600)
    pg.evaluate("() => { const d = document.getElementById('dlgNew'); if (d && d.open) d.close(); }")


def test_window():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  SKIPPED: Playwright not installed")
        return
    s = S.DebriefServer(heartbeat_timeout=3600)
    threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
    fit = ("(()=>{const n=document.querySelector('.nav'),sc=n.closest('.sidescroll')||n.parentElement;"
           "return n.getBoundingClientRect().bottom<=sc.getBoundingClientRect().bottom+1})()")
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception:  # noqa: BLE001
            print("  SKIPPED: Chromium not available")
            return
        try:
            for w, hgt in ((1280, 720), (1280, 860), (1920, 1080)):
                pg = b.new_page(viewport={"width": w, "height": hgt})
                errs = []
                pg.on("pageerror", lambda e: errs.append(str(e)))
                pg.goto(s.url)
                pg.wait_for_selector('.navbtn[data-nav="library"]', timeout=15000)
                _close_whats_new(pg)
                for nav in ("today", "coach", "room", "library", "season", "skills", "goals", "team"):
                    btn = pg.locator(f'.nav .navbtn[data-nav="{nav}"]')
                    if not btn.is_visible():
                        parent = btn.get_attribute("data-parent")
                        check(hgt < 860 and bool(parent), f"{w}x{hgt}: {nav} is folded under {parent}")
                        pg.locator(f'.nav .navbtn[data-nav="{parent}"]').click()
                        pg.wait_for_timeout(300)
                        check(btn.is_visible(), f"{w}x{hgt}: opening {parent} shows {nav}")
                    btn.click()
                    pg.wait_for_timeout(400)
                    check(pg.evaluate(fit), f"{w}x{hgt}: the whole sidebar fits on {nav}")
                if hgt >= 860:
                    check(pg.locator('.nav .navbtn[data-nav="season"]').is_visible() and
                          not pg.evaluate("document.body.classList.contains('navtight')"),
                          f"{w}x{hgt}: everything fits, so nothing is folded")
                if hgt < 860:
                    pg.locator('.nav .navbtn[data-nav="today"]').click()
                    pg.wait_for_timeout(300)
                    check(not pg.locator('.nav .navbtn[data-nav="season"]').is_visible() and
                          "hassub" in (pg.locator('.nav .navbtn[data-nav="library"]').get_attribute("class") or ""),
                          "on Today the Progress pages are folded, and Progress is marked")
                # Racing with nothing yet: one card, each step once
                pg.locator('.nav .navbtn[data-nav="racing"]').click()
                pg.wait_for_selector("#rcStarts .emptycard", timeout=10000)
                txt = pg.inner_text("#vRacing")
                check(txt.count("Scan this race") == 1 and pg.locator("#rcInc").is_hidden(),
                      f"{w}x{hgt}: Racing says each step once, in one card")
                pg.locator('.nav .navbtn[data-nav="cars"]').click()
                pg.wait_for_selector("#ccTable .emptycard", timeout=15000)
                pg.wait_for_timeout(500)
                check(pg.inner_text("#vCars").count("drive the other car at the same track") == 1,
                      f"{w}x{hgt}: Car vs car says each step once")
                check(not errs, f"{w}x{hgt}: no script errors {errs[:2]}")
                pg.close()
            # Rebuild all from the Skills page: asks first, with the count
            pg = b.new_page(viewport={"width": 1400, "height": 900})
            pg.goto(s.url)
            pg.wait_for_selector('.navbtn[data-nav="library"]', timeout=15000)
            _close_whats_new(pg)
            pg.locator('.nav .navbtn[data-nav="library"]').click()
            pg.locator('.nav .navbtn[data-nav="skills"]').click()
            fr = pg.frame_locator("#frame")
            fr.locator("button.rball").first.wait_for(timeout=15000)
            fr.locator("button.rball").first.click()
            pg.wait_for_function("()=>/^Rebuild 9 reviews\\?/.test(document.getElementById('bannerText').textContent)", timeout=5000)
            check(pg.locator("#bannerAct").text_content() == "Rebuild all", "Skills > Rebuild all asks first, with the count")
            pg.close()
        finally:
            b.close()
            s.shutdown()


if __name__ == "__main__":
    test_rebuild_all()
    test_checklist()
    test_window()
    print("all passed" if not FAILS else f"{len(FAILS)} failed")
    sys.exit(1 if FAILS else 0)
