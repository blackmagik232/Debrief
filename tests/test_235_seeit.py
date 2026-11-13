"""Debrief 2.35 "See it": the From iRacing section draws charts (fuel, position, gaps, pit stops, tyres, driver aids,
incidents on the track map), keeps each block's words, and puts the tables under "Show the numbers".

    python3 tests/ci_run.py tests/test_235_seeit.py

No network; temp folders from tempfile; the browser tests are skipped without Playwright + Chromium.
"""
from __future__ import annotations

import json
import os
import random
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

TMP = Path(tempfile.mkdtemp(prefix="debrief-235-"))
ST = TMP / "settings.toml"
ST.write_text(f"library_folder = '{TMP / 'lib'}'\n", encoding="utf-8")
os.environ.update(DEBRIEF_SETTINGS=str(ST), DEBRIEF_CACHE_DIR=str(TMP / "cache"),
                  DEBRIEF_SCHEDULE_DIR=str(TMP / "sched"), DEBRIEF_UPDATE_STATE=str(TMP / "update.json"))

import make_fixture_library as MFL  # noqa: E402
from debrief import recordings, report  # noqa: E402

START = "2026-10-01T23:30:00+00:00"
SHOTS = os.environ.get("DEBRIEF_SHOTS")        # a folder: the browser tests save screenshots there


def _sec(laps=30, multi=True):
    """A recorded race section with everything the charts draw."""
    rng = random.Random(5)
    per = [{"lap": n, "fuel_used_l": round(2.9 + rng.random() * 0.25, 3), "pit": n == 14} for n in range(1, laps + 1)]
    pos = [{"lap": n, "pos": max(1, 9 - n // 6 + (2 if 14 <= n <= 16 else 0)), "cpos": max(1, 4 - n // 10) if multi else None} for n in range(1, laps + 1)]
    gaps = [{"lap": n, "ahead": round(4 - n * 0.1 + (25 if n == 15 else 0), 2), "ahead_name": "A. Driver",
             "behind": round(1.5 + n * 0.05, 2), "behind_name": "B. Driver"} for n in range(1, laps + 1)]
    riv = [{"number": str(i), "name": f"Driver {i}", "is_me": i == 7, "class": "GT4" if i % 2 else "GT3",
            "stops": [{"lap": 12 + i % 5, "lane_s": 38.0 + i}] + ([{"lap": 24, "lane_s": 30.0}] if i == 3 else [])} for i in range(1, 31)]
    riv.append({"number": "99", "name": "No Stop", "class": "GT4", "stops": []})
    return {"num": 2, "type": "Race", "laps": laps,
            "fuel": {"per_lap_median_l": 3.0, "laps_counted": laps - 1, "start_l": 60.0, "per_lap": per,
                     "stints": [{"stint": 1, "first_lap": 1, "last_lap": 14, "fuel_l": 42.0, "per_lap_l": 3.0},
                                {"stint": 2, "first_lap": 15, "last_lap": laps, "fuel_l": 48.0, "per_lap_l": 3.0}]},
            "aids": {"seen": {"bb": [54.0, 53.5, 54.5], "abs": [3], "tc": [2, 3]},
                     "changes": [{"lap": 5, "aid": "bb", "from": 54.0, "to": 53.5, "d": 0.3},
                                 {"lap": 18, "aid": "bb", "from": 53.5, "to": 54.5, "d": 0.62},
                                 {"lap": 9, "aid": "tc", "from": 2, "to": 3, "d": 0.1}],
                     "per_lap": [{"lap": n, "bb": 54.0 if n < 5 else 53.5 if n < 18 else 54.5, "abs": 3, "tc": 2 if n < 9 else 3} for n in range(1, laps + 1)]},
            "incidents": {"total": 6, "events": [{"lap": 3, "gained": 2, "d": 0.18}, {"lap": 3, "gained": 2, "d": 0.185}, {"lap": 21, "gained": 1, "d": 0.9}, {"lap": 27, "gained": 1, "d": 0.55}]},
            "pit_stops": [{"lap": 14, "lane_s": 52, "stall_s": 31, "fuel_added_l": 50.0, "tyres_changed": True}],
            "tyres": {"wear_end": [0.82, 0.55, 0.91, 0.33]},
            "weather": {"track_c": 31.0, "air_c": 22.0},
            "race": {"positions": pos, "finish": {"pos": 4, "cpos": 1 if multi else None}, "gaps": gaps, "rivals": riv}}


SUMMARY = {"folder": "rec1", "final": True, "fuel_tank_l": 100.0, "fuel_max_pct": 1.0}


def test_charts_payload_has_every_chart_and_blocks_point_at_them():
    v = recordings.view(_sec(), SUMMARY)
    C = v["charts"]
    assert set(C) >= {"fuel", "position", "gaps", "stops", "incidents", "tyres", "aids"}, set(C)
    assert C["fuel"]["laps_per_tank"] == round(100 / 3.0, 1) and C["position"]["class"] is True
    assert [b["chart"] for b in v["blocks"] if b.get("chart")] == ["fuel", "aids", "incidents", "tyres", "position", "gaps", "stops"]
    assert any(b.get("numbers_of") == "fuel" for b in v["blocks"])
    assert len(C["incidents"]) == len(next(b for b in v["blocks"] if b.get("chart") == "incidents")["table"]["rows"])
    json.dumps(v, allow_nan=False)


def test_template_draws_charts_and_keeps_tables_under_show_the_numbers():
    t = (ROOT / "debrief" / "templates" / "review.html").read_text(encoding="utf-8")
    assert "const FI_DRAW={" in t
    for k in ("fuel(", "position(", "gaps(", "stops(", "tyres(", "aids(", "incidents("):
        assert k in t[t.index("const FI_DRAW={"):], k
    assert "'Show the numbers'" in t and "details.finums" in t
    assert "data-units" in t, "fuel follows Settings > Units"
    assert report.LAYOUT >= "2.35.0", "saved reviews are drawn again once with the charts"


def _loop(n=360):
    """A track outline (lat, lon) for the incidents map: a rounded loop near Charlotte."""
    import math
    return [[35.35 + 0.004 * math.sin(2 * math.pi * i / n), -80.68 + 0.007 * math.cos(2 * math.pi * i / n) * (1 + 0.2 * math.sin(4 * math.pi * i / n))]
            for i in range(n)]


def _write(lib, sec=None, summary=None, corners=True, track_map=True):
    d = lib / "reviews" / "2026-10-01_charlotte_ford_ev1"
    d.mkdir(parents=True, exist_ok=True)
    data = MFL.fake_review(MFL.ROVAL, START, 0.4, True, MFL.S4, 1100, None, random.Random(7))
    data["meta"]["session"] = "Race"
    data["from_iracing"] = recordings.view(sec or _sec(), summary or SUMMARY)
    if not corners:
        data["corners"] = []
    if track_map:
        data["map"] = _loop()
    report.write(d, data)
    return d


def _browser(fn, width=1280):
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # noqa: BLE001
        print("  (skipped: no Playwright)")
        return
    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch()
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


def _shot(pg, name):
    if SHOTS:
        el = pg.locator("#fromIracing")
        el.screenshot(path=str(Path(SHOTS) / f"{name}.png"))


def test_window_draws_every_chart_with_no_script_errors():
    d = _write(TMP / "lib1")

    def go(pg):
        pg.goto((d / "index.html").as_uri())
        pg.wait_for_selector("#fromIracing:not([hidden])", timeout=15000)
        n = pg.eval_on_selector_all("#fiBody .fichart svg", "els => els.length")
        assert n == 7, n
        assert pg.locator("#fiBody .secfail").count() == 0
        # every chart's table is folded under Show the numbers, the fuel laps with it
        sums = pg.eval_on_selector_all("#fiBody details.finums:not([hidden]) > summary", "els => els.map(e => e.textContent)")
        assert sums and set(sums) == {"Show the numbers"}, sums
        heads = pg.eval_on_selector_all("#fiBody > .panel > h3", "els => els.map(e => e.textContent)")
        assert "Fuel lap by lap" not in heads, heads
        assert pg.eval_on_selector_all("#fiBody details.finums h4", "els => els.map(e => e.textContent)") == ["Fuel lap by lap"]
        body = pg.text_content("#fiBody")
        assert "A full tank lasts about 33.3 laps" in body and "% of the way round" not in body.split("Where:")[1].split("Show the numbers")[0], body[:300]
        assert "Where: " in body
        assert "The 6 other cars that stopped are in the numbers below." in body
        _shot(pg, "desktop")
        # Save as PDF opens the numbers
        pg.evaluate("() => window.dispatchEvent(new Event('beforeprint'))")
        assert pg.eval_on_selector_all("#fiBody details.finums:not([hidden])", "els => els.every(e => e.open)")
    _browser(go)


def test_phone_width_and_gallons():
    from debrief.desktop import server as S
    d = _write(TMP / "lib2")
    page = d / "served.html"           # as the window serves it: Settings > Units = mph
    page.write_bytes(S.with_units((d / "index.html").read_bytes(), "mph"))

    def go(pg):
        pg.goto(page.as_uri())
        pg.wait_for_selector("#fromIracing:not([hidden])", timeout=15000)
        wide = pg.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth + 1")
        assert wide, "nothing sticks out sideways on a phone"
        fuel = pg.locator("#fiBody .fichart svg").first
        assert "gallons" in fuel.get_attribute("aria-label")
        _shot(pg, "phone")
    _browser(go, width=390)


def test_a_short_practice_with_little_data_still_draws_and_no_corners_says_the_share_of_the_lap():
    sec = _sec(laps=3, multi=False)
    sec["race"] = None
    sec["tyres"] = {"wear_end": [None, None, None, None]}
    d = _write(TMP / "lib3", sec=sec, corners=False, track_map=False)

    def go(pg):
        pg.goto((d / "index.html").as_uri())
        pg.wait_for_selector("#fromIracing:not([hidden])", timeout=15000)
        assert pg.locator("#fiBody .secfail").count() == 0
        assert "% of the way round" in pg.text_content("#fiBody")
        assert pg.locator("#fiBody .fichart svg").count() == 2, "fuel and aids; no race, no tyre readings and no map, so no other chart"
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
