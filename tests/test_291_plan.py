"""Debrief 2.9.1: the season plan on the Prepare page and the sharper pre-race brief.

    python3 tests/ci_run.py tests/test_291_plan.py

Built on the real bundled 2026 Season 4 schedule, with synthetic reviews and sessions. No network, temp folders
from tempfile, utf-8 everywhere. The browser test uses Playwright + Chromium and is skipped when either is missing.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import traceback
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="debrief-291-"))
os.environ["DEBRIEF_SCHEDULE_DIR"] = str(TMP / "schedules")
os.environ["DEBRIEF_CACHE_DIR"] = str(TMP / "cache")

from debrief import library, prep, prepare, schedule, series  # noqa: E402

TODAY = date(2026, 11, 2)          # season week 7 of 12
GT4 = "gt4-falken-tyre-challenge"
ZOLDER = "Circuit Zolder (Grand Prix)"
MCL = "McLaren 570S GT4"


def sched():
    return schedule.load_current(TODAY)


def new_lib() -> Path:
    return Path(tempfile.mkdtemp(prefix="lib-", dir=TMP))


def corners(*ranked):
    return [{"id": c, "apex_m": 500.0 * int(c[1:]), "avail": a, "rank": r} for c, a, r in ranked]


def add_review(lib, name, day, track=ZOLDER, car=MCL, best=98.0, median=99.0, wetness=0.0, session="Race", cs=None, **meta):
    d = lib / "reviews" / name
    d.mkdir(parents=True)
    laps = [{"n": i, "t": median + (i - 5) * 0.1, "clean": True} for i in range(1, 10)]
    (d / "review.json").write_text(json.dumps({
        "meta": {"driver": "D", "event_id": name, "car": car, "track": track, "started_at": day + "T19:00:00+00:00",
                 "session": session, **meta},
        "L": 3950.0, "laps": laps, "best": {"n": 1, "t": best, "clean": True}, "median": median, "std_clean": 0.5,
        "corners": cs or corners(("C1", 0.15, 1), ("C2", 0.10, 2), ("C3", 0.05, 3)),
        "conditions": {"wetness": wetness, "track_temp_c": 20.0}, "counts": {"analysed": 9, "clean": 9}}),
        encoding="utf-8")


def pick(lib, *ids):
    series.save(lib, list(ids), sched())
    return series.load(lib)


def rows_for(p, track_part):
    return [r for w in p["weeks"] for r in w["races"] if track_part in r["track"]]


def test_week_facts_read_the_schedules_own_words():
    f = series.week_facts({"length": "20 mins", "conditions": "Rain possible, Rolling start, Local enforced cautions"})
    assert f == {"length": "20 mins", "rain": "Rain possible", "start_type": "Rolling start", "cautions": "Local enforced cautions"}
    assert series.week_facts({"conditions": "Rain chance 10%, Standing start"})["rain"] == "Rain chance 10%"
    assert series.week_facts({"conditions": "Rain chance None"})["rain"] == "No rain"
    assert series.week_facts({"conditions": "All dry events, Cautions disabled"})["cautions"] == "No cautions"
    empty = series.week_facts({"conditions": None})     # not stated stays not stated, never "dry"
    assert empty["rain"] is None and empty["start_type"] is None and series.week_facts(None)["length"] is None


def test_plan_covers_the_rest_of_the_season_for_starred_series_only():
    lib = new_lib()
    add_review(lib, "a", "2026-10-31")
    p = prepare.plan(lib, sched(), pick(lib, GT4), [], today=TODAY)
    assert [w["season_week"] for w in p["weeks"]] == [7, 8, 9, 10, 11, 12]
    assert p["weeks"][0]["now"] and not p["weeks"][1]["now"]
    assert {r["series"] for w in p["weeks"] for r in w["races"]} == {"GT4 Falken Tyre Challenge"}
    nav = rows_for(p, "Navarra")[0]
    assert nav["length"] == "20 mins" and nav["rain"] == "Rain possible" and nav["start_type"] == "Rolling start"


def test_new_raced_and_other_car_status_from_reviews_and_sessions():
    lib = new_lib()
    add_review(lib, "z", "2026-10-31")                                     # raced, from a review
    sessions = [{"track": "Mobility Resort Motegi (Grand Prix)", "car": MCL, "type": 1, "best_clean": 110.0, "start": "2026-10-01T10:00:00Z"},
                {"track": "Autodromo Nazionale Monza (Grand Prix)", "car": "Ferrari 296 GT3", "type": 3, "best_clean": 105.0, "start": "2026-09-01T10:00:00Z"}]
    p = prepare.plan(lib, sched(), pick(lib, GT4), sessions, today=TODAY)
    assert rows_for(p, "Zolder")[0]["status"] == "raced"
    assert rows_for(p, "Navarra")[0]["status"] == "new"
    mot = rows_for(p, "Motegi")[0]
    assert mot["status"] == "practised" and mot["pace"]["best"] == 110.0
    mon = rows_for(p, "Monza")[0]
    assert mon["status"] == "other_car" and mon["pace"]["other_car"] is True and mon["pace"]["car"] == "Ferrari 296 GT3"


def test_a_review_and_its_synced_session_are_one_race():
    lib = new_lib()
    add_review(lib, "z", "2026-10-31")
    s = [{"track": ZOLDER, "car": MCL, "type": 3, "best_clean": 98.0, "start": "2026-10-31T19:00:00Z"}]
    assert rows_for(prepare.plan(lib, sched(), pick(lib, GT4), s, today=TODAY), "Zolder")[0]["races"] == 1


def test_wet_race_stays_out_of_the_typical_lap():
    lib = new_lib()
    add_review(lib, "a", "2026-10-31", best=98.0, median=99.0)
    add_review(lib, "b", "2026-11-01", best=109.7, median=110.7, wetness=50.0)
    add_review(lib, "c", "2026-11-02", best=98.8, median=100.0)
    pace = rows_for(prepare.plan(lib, sched(), pick(lib, GT4), [], today=TODAY), "Zolder")[0]["pace"]
    assert pace["best"] == 98.0 and pace["typical"] == 99.5 and pace["typical_races"] == 2
    assert pace["wet_races"] == 1 and pace["wet_best"] == 109.7


def test_test_data_and_partial_laps_never_count():
    lib = new_lib()
    add_review(lib, "t", "2026-10-31", test_data=True)
    add_review(lib, "m", "2026-10-01", track="Mobility Resort Motegi (Grand Prix)", best=110.0, median=111.0)
    s = [{"track": "Mobility Resort Motegi (Grand Prix)", "car": MCL, "type": 1, "best_clean": 6.0, "start": "2026-10-02T10:00:00Z"},
         {"track": "Mobility Resort Motegi (Grand Prix)", "car": MCL, "type": 1, "best_clean": 111.0, "start": "2026-10-03T10:00:00Z"}]
    p = prepare.plan(lib, sched(), pick(lib, GT4), s, today=TODAY)
    assert rows_for(p, "Zolder")[0]["status"] == "new"
    assert rows_for(p, "Motegi")[0]["pace"]["best"] == 110.0


def _two_series(lib):
    """Two made-up series: B races Motegi before A does; C is in other cars and races Monza before A."""
    weeks = [{"week": 7 + i, "season_week": 7 + i, "start": d, "track": t, "length": "20 mins", "conditions": "Rain possible"}
             for i, (d, t) in enumerate((("2026-10-27", "Circuit Zolder - Grand Prix"), ("2026-11-03", "Tsukuba Circuit - 2000 Full"),
                                         ("2026-11-10", "Mobility Resort Motegi - Grand Prix"), ("2026-11-17", "Autodromo Nazionale Monza - Grand Prix")))]
    def other(track_8, track_0=None):
        return [{"week": 1, "season_week": 6, "start": "2026-10-20", "track": track_0 or "Daytona International Speedway - Oval"},
                {"week": 2, "season_week": 8, "start": "2026-11-03", "track": track_8}]
    return {"season": "T", "weeks": [{"week": i, "start": w} for i, w in enumerate(
                ["2026-10-20", "2026-10-27", "2026-11-03", "2026-11-10", "2026-11-17"], start=6)],
            "series": [{"id": "a", "name": "A", "cars": [MCL], "weeks": weeks},
                       {"id": "b", "name": "B", "cars": [MCL], "weeks": other("Mobility Resort Motegi - Grand Prix", "Circuit Zolder - Grand Prix")},
                       {"id": "c", "name": "C", "cars": ["Ferrari 296 GT3"], "weeks": other("Autodromo Nazionale Monza - Grand Prix")}]}


def test_warmups_only_from_weeks_still_to_come_and_say_if_the_cars_differ():
    lib, s = new_lib(), _two_series(None)
    add_review(lib, "z", "2026-10-31")
    series.save(lib, ["a", "b", "c"], s)
    p = prepare.plan(lib, s, series.load(lib), [], today=TODAY)
    a = {r["track"]: r for w in p["weeks"] for r in w["races"] if r["series"] == "A"}
    assert [(x["series"], x["same_cars"], x["week"]) for x in a["Mobility Resort Motegi - Grand Prix"]["warmups"]] == [("B", True, 2)]
    assert [(x["series"], x["same_cars"]) for x in a["Autodromo Nazionale Monza - Grand Prix"]["warmups"]] == [("C", False)]
    # B raced Zolder in a week that's already over: too late to be extra laps for A's week, and A's is the current one
    assert a["Circuit Zolder - Grand Prix"]["warmups"] == []
    # the original call still looks back, for the whole-season list
    assert prepare.warmups(s["series"][0], s["series"][0]["weeks"][0], s["series"]) == [{"series": "B", "start": "2026-10-20"}]


def test_flags_go_to_new_tracks_without_a_same_car_warmup_first_and_are_capped():
    lib, s = new_lib(), _two_series(None)
    add_review(lib, "z", "2026-10-31")
    series.save(lib, ["a", "b", "c"], s)
    p = prepare.plan(lib, s, series.load(lib), [], today=TODAY)
    assert len(p["flagged"]) <= prepare.PLAN_FLAGS
    a = {r["track"]: r for w in p["weeks"] for r in w["races"] if r["series"] == "A"}
    assert not a["Circuit Zolder - Grand Prix"]["flag"]           # raced last week
    tsu = a["Tsukuba Circuit - 2000 Full"]
    assert tsu["flag"] and "none of your other series" in tsu["flag_reason"]
    assert p["flagged"][0]["track"] == "Tsukuba Circuit - 2000 Full"
    # B's own Motegi week is the first time there, so it takes the flag; A's week after it can lean on those laps
    b = next(r for w in p["weeks"] for r in w["races"] if r["series"] == "B" and "Motegi" in r["track"])
    assert b["flag"] and not a["Mobility Resort Motegi - Grand Prix"]["flag"]
    assert "different cars, in C" in a["Autodromo Nazionale Monza - Grand Prix"]["flag_reason"]
    assert not any(c in str(p) for c in ("target", "aim for", "should"))


def test_stale_races_can_be_flagged_recent_ones_not():
    lib = new_lib()
    add_review(lib, "z", "2025-11-01")
    add_review(lib, "m", "2026-10-30", track="Mobility Resort Motegi (Grand Prix)")
    p = prepare.plan(lib, sched(), pick(lib, GT4), [], today=TODAY)
    assert rows_for(p, "Motegi")[0]["flag"] is False
    # Zolder is raced but over a year ago; new tracks come first and fill the flags
    assert rows_for(p, "Zolder")[0]["status"] == "raced" and rows_for(p, "Zolder")[0]["flag"] is False
    assert prepare._flag_rank({"status": "raced", "last": "2025-11-01", "warmups": [], "start": "x"}, TODAY)[0] == 3
    assert prepare._flag_rank({"status": "raced", "last": "2026-10-30", "warmups": [], "start": "x"}, TODAY) is None
    assert "12 months ago" in prepare._flag_reason({"status": "raced", "last": "2025-11-01", "warmups": []}, TODAY)


def test_series_you_dont_own_shows_no_history_and_no_flags():
    lib = new_lib()
    add_review(lib, "z", "2026-10-31")
    s = sched()
    other = next(x for x in s["series"] if not any(schedule.same_car(MCL, c) for c in x.get("cars") or [])
                 and not x.get("multiclass") and any(c for c in x.get("cars") or []))
    series.save(lib, [other["id"]], s)
    p = prepare.plan(lib, s, series.load(lib), [], today=TODAY)
    assert p["series"][0]["no_history"] and "If you don't own" in p["series"][0]["note"]
    rs = [r for w in p["weeks"] for r in w["races"]]
    assert rs and all(not r["flag"] for r in rs) and p["flagged"] == []
    assert {r["status"] for r in rs} <= {"no_history", "other_car"}


def test_empty_states():
    lib = new_lib()
    assert prepare.plan(lib, sched(), series.load(lib), [], today=TODAY)["state"] == "no_series"
    p = prepare.plan(lib, sched(), pick(lib, GT4), [], today=TODAY)       # no reviews, no sync
    assert "no history" in p["note"] and p["flagged"] == []
    assert {r["status"] for w in p["weeks"] for r in w["races"]} == {"no_history"}
    assert prepare.plan(lib, sched(), series.load(lib), [], today=date(2026, 12, 20))["state"] == "season_over"
    (lib / "my-series.json").write_text(json.dumps({"series": [{"id": "gone", "name": "Long Gone Cup"}]}), encoding="utf-8")
    p = prepare.plan(lib, sched(), series.load(lib), [], today=TODAY)
    assert p["state"] == "no_series" and "Long Gone Cup" in p["series"][0]["note"]


def test_view_carries_the_plan_and_survives_a_broken_one():
    lib = new_lib()
    pick(lib, GT4)
    assert prepare.view({"library_folder": str(lib)}, None, [], TODAY)["plan"]["state"] == "ok"
    real = prepare.plan
    prepare.plan = lambda *a, **k: 1 / 0
    try:
        v = prepare.view({"library_folder": str(lib)}, None, [], TODAY)
    finally:
        prepare.plan = real
    assert v["ok"] and v["plan"] == {"state": "error"} and v["mine"]


def _zolder_lib():
    """Shaped like the real library: two dry races and a wet one in the middle."""
    lib = new_lib()
    add_review(lib, "a", "2026-10-31", best=98.069, median=99.291, cs=corners(("C4", 0.12, 1), ("C6", 0.11, 2), ("C3", 0.01, 7)))
    add_review(lib, "b", "2026-11-01", best=109.753, median=110.731, wetness=50.0, cs=corners(("C5", 0.25, 1), ("C3", 0.10, 3), ("C6", 0.02, 7)))
    add_review(lib, "c", "2026-11-02", best=98.874, median=99.942, cs=corners(("C3", 0.15, 1), ("C6", 0.11, 2), ("C7", 0.08, 3)))
    return lib


def _brief(lib, track=ZOLDER, car=MCL, today=TODAY):
    rows = library.race_rows(library.list_reviews(lib))
    mine = [r for r in rows if library._norm(r["track"]) == library._norm(track) and library._norm(r["car"]) == library._norm(car)]
    return prep.race_brief(lib, rows, track, car, prep._history(mine), prep.weak_corners(mine) if mine else [], today)


def test_brief_repeat_corners_say_when_they_last_cost_time():
    b = _brief(_zolder_lib())
    c6 = next(c for c in b["corners"] if c["id"] == "C6")
    assert c6["top3"] == 2 and c6["last_date"] == "2026-11-02" and c6["last_avail"] == 0.11 and c6["last_wet"] is False


def test_brief_last_cost_names_a_wet_race():
    w = {"history": [{"date": "2026-10-01", "dir": "d", "rank": 2, "avail": 0.2}, {"date": "2026-10-02", "dir": "w", "rank": 3, "avail": 0.3},
                     {"date": "2026-10-03", "dir": "x", "rank": 6, "avail": 0.4}]}
    assert prep._last_cost(w, {"w"}) == {"last_date": "2026-10-02", "last_avail": 0.3, "last_wet": True}
    assert prep._last_cost({"history": []}, set())["last_date"] is None


def test_brief_pace_keeps_the_wet_race_apart():
    p = _brief(_zolder_lib())["pace"]
    assert p["best"] == 98.069 and p["typical"] == 99.617 and p["typical_races"] == 2
    assert p["wet_races"] == 1 and p["wet_best"] == 109.753 and p["best_wet"] is False


def test_brief_wet_only_says_so():
    lib = new_lib()
    add_review(lib, "w", "2026-11-01", best=109.0, median=110.0, wetness=50.0)
    p = _brief(lib)["pace"]
    assert p["best_wet"] is True and p["typical"] is None


def test_brief_schedule_conditions_only_from_your_series():
    lib = _zolder_lib()
    assert _brief(lib)["upcoming"] == [] and "None of your series" in _brief(lib)["upcoming_note"]
    pick(lib, GT4)
    u = _brief(lib)["upcoming"]
    assert len(u) == 1 and u[0]["this_week"] and u[0]["length"] == "20 mins" and u[0]["rain"] == "Rain possible" and u[0]["start_type"] == "Rolling start"
    assert _brief(lib, today=date(2026, 11, 9))["upcoming"] == []                # that week is over
    assert _brief(lib, car="Ferrari 296 GT3")["upcoming"] == []                  # not this car


def test_brief_says_plainly_when_there_is_no_history():
    lib = new_lib()
    b = _brief(lib, track="Tsukuba Circuit (2000 Full)")
    assert "no saved race reviews" in b["no_history"] and b["pace"] is None and b["corners"] == []
    assert b["racecraft"] is None and b["racecraft_wait"] is None


def test_brief_racecraft_waits_behind_the_profiles_bar():
    from debrief import profile
    b = _brief(_zolder_lib())
    assert b["racecraft"] is None and f"need {profile.RACECRAFT_MIN_RACES} scanned races" in b["racecraft_wait"]


def test_brief_racecraft_shows_once_the_bar_is_met():
    from debrief import profile
    lib = _zolder_lib()
    rows = library.race_rows(library.list_reviews(lib))
    rc = {"v": 2, "lap1": 2, "made": 3, "lost": 1, "stuck": 2, "corners": [], "recovery": [], "stops": [], "rivals": []}
    enough = [dict(r, racecap=dict(rc, sub=str(i))) for i, r in enumerate(rows)]
    assert profile.racecraft(enough)["enough"]
    real, library.list_reviews = library.list_reviews, lambda *a, **k: enough
    try:
        d = prep.race_brief(lib, enough, ZOLDER, MCL, prep._history(enough), prep.weak_corners(enough), TODAY)
    finally:
        library.list_reviews = real
    assert d["racecraft"]["passes"]["made"] == 9 and d["racecraft"]["lap1"]["avg"] == 2 and d["note"] and d["racecraft_wait"] is None


def test_build_writes_the_new_brief_and_makes_no_request():
    lib = _zolder_lib()
    pick(lib, GT4)
    d = prep.build(None, lib, None, None, None, log=lambda m: None, track=ZOLDER, car=MCL)
    assert d["requests"] == 0 and d["brief"]["corners"] and "upcoming" in d["brief"]
    html = prep.write(lib, d).read_text(encoding="utf-8")
    assert "race_brief" not in html and "—" not in str(d["brief"])


def test_ui_copy_has_no_em_dashes_and_uses_text_content():
    js = (ROOT / "debrief" / "desktop" / "ui" / "app.js").read_text(encoding="utf-8")
    part = js[js.index("/* season plan"):js.index("$('btnSchDl').addEventListener")]
    assert "—" not in part and "innerHTML" not in part
    tpl = (ROOT / "debrief" / "templates" / "prep.html").read_text(encoding="utf-8")
    part = tpl[tpl.index("// pre-race brief"):tpl.index("if(host.children.length)$('briefsec')")]
    assert "—" not in part and "innerHTML" not in part


def test_prep_page_shows_the_brief_in_a_browser():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  (skipped: no Playwright)")
        return
    lib = _zolder_lib()
    pick(lib, GT4)
    good = prep.write(lib, prep.build(None, lib, None, None, None, log=lambda m: None, track=ZOLDER, car=MCL))
    empty = prep.write(lib, prep.build(None, lib, None, None, None, log=lambda m: None, track="Tsukuba Circuit (2000 Full)", car=MCL))
    try:
        with sync_playwright() as pw:
            br = pw.chromium.launch()
            pg = br.new_page()
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.goto(good.as_uri())
            t = pg.inner_text("#brief")
            assert "Rain possible" in t and "Rolling start" in t and "20 mins" in t and "Best 1:38.069" in t, t
            assert "Typical lap 1:39.617 over 2 dry races" in t and "1 wet race" in t, t
            assert "Last cost about 0.11 s a lap on Nov 2, 2026" in t and "Racecraft facts here need" in t, t
            pg.goto(empty.as_uri())
            assert "no saved race reviews" in pg.inner_text("#brief")
            br.close()
    except Exception as e:  # noqa: BLE001
        if "Executable doesn't exist" in str(e):
            print("  (skipped: no Chromium)")
            return
        raise
    assert not errs, errs


if __name__ == "__main__":
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
