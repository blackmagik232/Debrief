"""Debrief 2.35: the Setup section on the race review page (racesetup.for_review, worked out in report.write once,
drawn in review.html).

    python3 tests/ci_run.py tests/test_235_setup_page.py

Stand-in .ibt files (tests/fake_ibt.py); no network; temp folders. Browser tests are skipped without Playwright.
"""
from __future__ import annotations

import copy
import json
import os
import random
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

TMP = Path(tempfile.mkdtemp(prefix="debrief-235-setuppage-"))
ST = TMP / "settings.toml"
ST.write_text(f"library_folder = '{(TMP / 'lib').as_posix()}'\n", encoding="utf-8")
os.environ.update(DEBRIEF_SETTINGS=str(ST), DEBRIEF_CACHE_DIR=str(TMP / "cache"),
                  DEBRIEF_SCHEDULE_DIR=str(TMP / "sched"), DEBRIEF_UPDATE_STATE=str(TMP / "update.json"))

import fake_ibt as fi  # noqa: E402
import make_fixture_library as MFL  # noqa: E402
from debrief import localrace, report  # noqa: E402

CAR, TRACK = "mclaren570sgt4", "zolder gp"
START = "2026-11-02T19:00:00+00:00"


def _laps(base, n, seed):
    rng = random.Random(seed)
    return [round(base + rng.uniform(-0.05, 0.05), 3) for _ in range(n)]


def _tel(name, fixed=False):
    d = TMP / name
    d.mkdir(parents=True, exist_ok=True)
    fi.make(d / f"{CAR}_{TRACK} 2026-11-01 18-00-00.ibt", _laps(99.5, 7, 1), car_path=CAR, track_code=TRACK)
    s = copy.deepcopy(fi.SETUP_GT4)
    if fixed:
        s["Chassis"]["InCarDials"]["BrakePressureBias"] = "52.0%"
    else:
        s["Chassis"]["LeftFront"]["Camber"] = "-3.6 deg"
        s["Chassis"]["RightFront"]["Camber"] = "-3.6 deg"
    race = fi.make(d / f"{CAR}_{TRACK} 2026-11-02 19-00-00.ibt", _laps(99.0, 10, 2), car_path=CAR, track_code=TRACK,
                   session="Race", official=1, fixed_setup=1 if fixed else 0, subsession=777001, setup=s, fuel=(40.0, 2.6))
    return d, race.name


def _review(lib, tel, race, strategy=None):
    data = MFL.fake_review(MFL.ROVAL, START, 0.4, True, MFL.S4, 1100, None, random.Random(7))
    data["meta"].update(session="Race", source="pc", files=[race], car="McLaren 570S GT4", track="Zolder Circuit (Grand Prix)")
    if strategy:
        data["strategy"] = strategy
    d = TMP / lib / "reviews" / "2026-11-02_zolder_mclaren_1"
    d.mkdir(parents=True, exist_ok=True)
    old = localrace.telemetry_folder
    localrace.telemetry_folder = lambda: tel
    try:
        report.write(d, data)
    finally:
        localrace.telemetry_folder = old
    return d


def test_a_race_review_gets_its_setup_once_and_a_rerender_never_reads_again():
    tel, race = _tel("open")
    d = _review("l1", tel, race)
    data = json.loads((d / "review.json").read_text(encoding="utf-8"))
    S = data["setup"]
    assert S and S["ok"] and S["files"] == [race] and not S["fixed"], S
    import debrief.racesetup as rs
    real = rs.for_review
    rs.for_review = lambda *a, **k: (_ for _ in ()).throw(AssertionError("read again"))
    try:
        report.write(d, data)                       # a re-render keeps the saved section
        report.write(d, dict(data, setup=None))     # "no file found" is kept too
        x = dict(data)
        x.pop("setup")
        report.write(d, x, setup=False)             # refresh_all's re-render reads nothing
        assert "setup" not in x
    finally:
        rs.for_review = real
    # no telemetry folder: nothing stored, tried again next time
    y = dict(data)
    y.pop("setup")
    old = localrace.telemetry_folder
    localrace.telemetry_folder = lambda: None
    try:
        report.write(d, y)
    finally:
        localrace.telemetry_folder = old
    assert "setup" not in y


def _browser(fn, width=1280):
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # noqa: BLE001
        print("  (skipped: no Playwright)")
        return
    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch(args=["--allow-file-access-from-files"])
            pg = b.new_page(viewport={"width": width, "height": 900})
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.on("console", lambda m: errs.append(m.text) if m.type == "error" and "Failed to load resource" not in m.text else None)
            pg.route("**/fonts.googleapis.com/**", lambda r: r.abort())
            pg.route("**/fonts.gstatic.com/**", lambda r: r.abort())
            fn(pg)
            b.close()
            assert not errs, errs
    except Exception as e:  # noqa: BLE001
        if "Executable doesn't exist" in str(e):
            print("  (skipped: no Chromium)")
            return
        raise


def _framed(d):
    """The review inside a frame, as the window shows it; the outer page keeps every message the review sends."""
    p = d / "framed.html"
    p.write_text('<!DOCTYPE html><html><body><script>window.got=[];addEventListener("message",e=>window.got.push(e.data))</script>'
                 '<iframe id="f" src="index.html" style="width:1200px;height:2000px"></iframe></body></html>', encoding="utf-8")
    return p


def test_the_page_draws_the_section_and_open_in_setup_lab_asks_the_window():
    tel, race = _tel("open2")
    d = _review("l2", tel, race, strategy={"burn": {"median_l": 2.55, "n": 9}})

    def go(pg):
        pg.goto(_framed(d).as_uri())
        f = pg.frame_locator("#f")
        f.locator("#setupSec:not([hidden])").wait_for(timeout=15000)
        body = f.locator("#setupBody").text_content()
        assert "What you raced" in body and "You raced Setup" in body and "camber" in body.lower(), body[:500]
        assert "How the car handled" in body and "One change to try" in body
        assert "Fuel:" not in body and "L a lap" not in body, "the open-setup section prints no fuel figure (the Fuel panel owns it)"
        assert f.locator(".secfail").count() == 0
        assert f.locator("#agreeNote").is_hidden(), f.locator("#agreeNote").text_content()
        assert f.locator('#jumpPanel a[href="#setupSec"]').get_attribute('hidden') is None, 'the jump link shows'
        f.locator("#btnSetupLab").click()
        pg.wait_for_function("window.got.some(m => m && m.type === 'debrief-open-setuplab')", timeout=5000)
        got = pg.evaluate("window.got.filter(m => m && m.type === 'debrief-open-setuplab')")
        assert got == [{"type": "debrief-open-setuplab", "car": CAR, "track": TRACK}], got
    _browser(go)


def test_fixed_setup_shows_only_the_dials_and_the_fuel_panels_figure():
    tel, race = _tel("fixed", fixed=True)
    d = _review("l3", tel, race, strategy={"burn": {"median_l": 2.55, "n": 9}})
    assert json.loads((d / "review.json").read_text(encoding="utf-8"))["setup"]["fixed"]

    def go(pg):
        pg.goto((d / "index.html").as_uri())
        pg.wait_for_selector("#setupSec:not([hidden])", timeout=15000)
        body = pg.text_content("#setupBody")
        assert "Brake bias: 52.0%." in body and "Fuel: 2.55 L (0.67 gal) a lap, as in the Fuel panel." in body, body
        low = body.lower()
        for w in ("camber", "kpa", "°c", "what you raced", "how the car handled", "faster?"):
            assert w not in low, (w, body)
        assert pg.locator("#btnSetupLab").count() == 0 and "in the Debrief window" in body, "outside the window: no button"
        assert pg.locator("#agreeNote").is_hidden()
    _browser(go)


def test_no_telemetry_hides_the_section_and_a_reason_is_one_line():
    tel, race = _tel("none")
    d = _review("l4", tel, "not-this-race.ibt")
    data = json.loads((d / "review.json").read_text(encoding="utf-8"))
    assert data.get("setup") is None and "setup" in data
    bad = dict(data, setup={"ok": False, "reason": "The telemetry file for this race couldn't be read."})
    e = TMP / "l5" / "reviews" / "r"
    e.mkdir(parents=True)
    report.write(e, bad, setup=False)

    def go(pg):
        pg.goto((d / "index.html").as_uri())
        pg.wait_for_selector("#summary", timeout=15000)
        assert pg.locator("#setupSec").is_hidden()
        pg.goto((e / "index.html").as_uri())
        pg.wait_for_selector("#setupSec:not([hidden])", timeout=15000)
        assert pg.text_content("#setupLede") == "The telemetry file for this race couldn't be read." and pg.locator("#setupBody > *").count() == 0
    _browser(go)


if __name__ == "__main__":
    import shutil
    import traceback
    failed = 0
    tests = [(k, v) for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for name, fn in tests:
        try:
            fn()
            print("ok  ", name)
        except Exception:  # noqa: BLE001
            failed += 1
            print("FAIL", name)
            traceback.print_exc()
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
