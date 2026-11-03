"""Debrief 2.11: the Prepare page's picker, the one prep view and the season strip.

    python3 tests/ci_run.py tests/test_211_prepare.py

On the real bundled 2026 Season 4 schedule with synthetic reviews. The clock is fixed at season week 7. No network,
temp folders from tempfile, utf-8 everywhere. Browser tests use Playwright + Chromium and are skipped without them.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import threading
import traceback
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"

TMP = Path(tempfile.mkdtemp(prefix="debrief-211-"))
ST = TMP / "settings.toml"
LIB = TMP / "lib"
LIB.mkdir()


def write_settings(lib=LIB, token=""):
    ST.write_text(f"library_folder = '{lib}'\ntoken = \"{token}\"\n", encoding="utf-8")


write_settings()
os.environ.update(DEBRIEF_SETTINGS=str(ST), DEBRIEF_CACHE_DIR=str(TMP / "cache"), DEBRIEF_SCHEDULE_DIR=str(TMP / "schedules"))

from debrief import __version__, notes, prep, prepare, schedule, series  # noqa: E402
from debrief.desktop import server as S  # noqa: E402

TODAY = date(2026, 11, 2)      # season week 7 of 12
GT4 = "gt4-falken-tyre-challenge"
ZOLDER_REVIEW = "Circuit Zolder (Grand Prix)"
ZOLDER = "Circuit Zolder - Grand Prix"
MCL = "McLaren 570S GT4"
UI = ROOT / "debrief" / "desktop" / "ui"


def sched():
    return schedule.load_current(TODAY)


def new_lib() -> Path:
    return Path(tempfile.mkdtemp(prefix="lib-", dir=TMP))


def add_review(lib, name, day, track=ZOLDER_REVIEW, car=MCL, best=98.0, median=99.0, wetness=0.0, session="Race", cs=None):
    d = Path(lib) / "reviews" / name
    d.mkdir(parents=True)
    laps = [{"n": i, "t": median + (i - 5) * 0.1, "clean": True} for i in range(1, 10)]
    cs = cs or [{"id": c, "apex_m": 500.0 * int(c[1:]), "avail": a, "rank": r} for c, a, r in (("C1", 0.15, 1), ("C2", 0.10, 2), ("C3", 0.05, 3))]
    (d / "review.json").write_text(json.dumps({
        "meta": {"driver": "D", "event_id": name, "car": car, "track": track, "started_at": day + "T19:00:00+00:00", "session": session},
        "L": 3950.0, "laps": laps, "best": {"n": 1, "t": best, "clean": True}, "median": median, "std_clean": 0.5, "corners": cs,
        "conditions": {"wetness": wetness, "track_temp_c": 20.0}, "counts": {"analysed": 9, "clean": 9}}), encoding="utf-8")


def zolder_lib() -> Path:
    lib = new_lib()
    add_review(lib, "a", "2026-10-30", best=98.069, median=99.5)
    add_review(lib, "b", "2026-11-01", best=109.753, median=110.731, wetness=50.0)
    add_review(lib, "c", "2026-11-02", best=98.874, median=99.7)
    return lib


def cat(lib, sessions=()):
    mine = series.load(lib)
    from debrief import library
    rows = library.list_reviews(lib)
    return prepare.catalog(sched(), mine, rows, list(sessions), schedule.Matcher(), TODAY)


def focus(lib, **kw):
    return prepare.focus({"library_folder": str(lib)}, today=TODAY, **kw)


# ---------------------------------------------------------------- the catalog
def test_version():
    assert __version__ == "2.11.0"


def test_catalog_has_every_car_by_class_and_no_placeholder():
    c = cat(new_lib())
    names = [x["name"] for x in c["cars"]]
    assert not any("see race week" in n.lower() for n in names)
    assert len(names) == len(set(names)) and "Gen 4 Grand National" in names
    assert next(x for x in c["cars"] if x["name"] == MCL)["class"] == "GT4"
    assert sum(1 for n in names if schedule.norm(n) == "mercedes amg gt4") == 1      # the schedule spells it two ways
    # classes stay together, and a car the class rules don't know sits in its series' category
    seen = []
    for x in c["cars"]:
        if not seen or seen[-1] != x["class"]:
            seen.append(x["class"])
    assert len(seen) == len(set(seen)) and seen[0] == "GTP"
    assert next(x for x in c["cars"] if x["name"] == "Mini Stock")["class"] == "Oval"


def test_see_race_week_series_use_the_cars_each_week_names():
    c = cat(new_lib())
    s = next(x for x in c["series"] if x["id"].startswith("draft-master"))
    assert "Gen 4 Grand National" in s["cars"] and not any("see race" in x.lower() for x in s["cars"])
    wk = s["weeks"][0]
    assert wk["cars"] == ["Gen 4 Grand National"]
    plain = next(x for x in c["series"] if x["id"] == GT4)
    assert MCL in plain["cars"] and "cars" not in plain["weeks"][0]


def test_driven_cars_most_used_first_and_a_review_and_session_are_one_race():
    lib = zolder_lib()
    add_review(lib, "t1", "2026-10-20", track="Lime Rock Park (Grand Prix)", car="Toyota GR86")
    c = cat(lib)
    assert [(d["name"], d["uses"]) for d in c["driven"]] == [(MCL, 3), ("Toyota GR86", 1)]
    sessions = [{"car": "Toyota GR86", "track": "Lime Rock Park (Grand Prix)", "start": f"2026-10-{d}T19:00:00Z", "type": 3} for d in (20, 21, 22, 23, 24)]
    assert [(d["name"], d["uses"]) for d in cat(lib, sessions)["driven"]] == [("Toyota GR86", 5), (MCL, 3)]
    assert cat(new_lib())["driven"] == []


def test_catalog_tracks_add_where_you_drove_without_duplicating_the_schedule():
    lib = zolder_lib()
    add_review(lib, "x", "2026-10-20", track="Made Up Raceway (Full)")
    t = cat(lib)["tracks"]
    assert ZOLDER in t and "Made Up Raceway (Full)" in t and ZOLDER_REVIEW not in t


def test_weeks_left_and_starred_flags():
    lib = zolder_lib()
    series.save(lib, [GT4], sched())
    s = next(x for x in cat(lib)["series"] if x["id"] == GT4)
    assert s["starred"]
    assert [w["no"] for w in s["weeks"] if w["left"]] == [7, 8, 9, 10, 11, 12]


# ---------------------------------------------------------------- the view
def test_focus_race_facts_and_never_an_in_game_date():
    lib = new_lib()
    d = focus(lib, series_id=GT4, week_no=7, car=MCL)
    assert d["week"]["now"] and d["week"]["car_runs"] and d["track"] == ZOLDER
    assert d["race"]["length"] and d["race"]["start_type"] == "Rolling start" and d["race"]["rain"]
    assert d["series"]["frequency"].startswith("Races every")
    w = next(w for w in schedule.find_series(sched(), GT4)["weeks"] if w["season_week"] == 7)
    assert w["race_time"] not in json.dumps(d)
    assert w["race_time"][:10] not in json.dumps(d)


def test_focus_splits_dry_and_wet():
    lib = zolder_lib()
    d = focus(lib, series_id=GT4, week_no=7, car=MCL)
    p = d["pace"]
    assert p["other_car"] is None
    assert p["dry"]["best"] == 98.069 and p["dry"]["typical"] == 99.6 and p["dry"]["races"] == 2
    assert p["wet"]["best"] == 109.753 and p["wet"]["typical"] == 110.731 and p["wet"]["races"] == 1
    assert d["status"]["code"] == "raced" and d["status"]["races"] == 3


def test_a_wet_race_late_evening_keeps_its_synced_session_wet():
    lib = new_lib()
    add_review(lib, "w", "2026-11-01", best=110.0, median=111.0, wetness=50.0)
    from debrief import library
    day = library.list_reviews(lib)[0]["date"]
    s = {"car": MCL, "track": ZOLDER_REVIEW, "start": day + "T12:00:00", "best_clean": 109.0, "type": 3}
    p = prepare.focus({"library_folder": str(lib)}, sessions=[s], series_id=GT4, week_no=7, car=MCL, today=TODAY)["pace"]
    assert p["dry"] is None or p["dry"]["best"] is None
    assert p["wet"]["best"] == 109.0


def test_focus_in_another_car_is_labelled_and_never_mixed():
    lib = zolder_lib()
    d = focus(lib, series_id=GT4, week_no=7, car="BMW M4 G82 GT4 Evo")
    assert d["pace"]["other_car"] == MCL and d["status"]["code"] == "other_car"
    assert d["corners"] == []          # corners are per car


def test_focus_any_track_keeps_history_and_has_no_race():
    lib = zolder_lib()
    d = focus(lib, series_id=GT4, week_no=None, car=MCL, track=ZOLDER)
    assert d["week"] is None and d["race"] is None and d["pace"]["dry"]["best"] == 98.069
    none = focus(lib, car=MCL, track="Made Up Raceway (Full)")
    assert none["series"] is None and none["pace"] is None and none["status"]["code"] in ("new", "no_history")


def test_a_track_other_than_the_weeks_drops_the_week():
    d = focus(new_lib(), series_id=GT4, week_no=7, car=MCL, track="Road America - Full Course")
    assert d["week"] is None and d["track"] == "Road America - Full Course"


def test_repeat_corners_from_the_prep_rules():
    lib = zolder_lib()
    d = focus(lib, series_id=GT4, week_no=7, car=MCL)
    assert [c["id"] for c in d["corners"]][:2] == ["C1", "C2"] and d["corners"][0]["top3"] == 3


def test_racecraft_and_rivals_stay_behind_the_gate():
    d = focus(zolder_lib(), series_id=GT4, week_no=7, car=MCL)
    assert d["racecraft"] is None and d["rivals"] == [] and "3 scanned races" in d["racecraft_wait"]
    empty = focus(new_lib(), series_id=GT4, week_no=7, car=MCL)
    assert empty["racecraft"] is None and empty["racecraft_wait"] is None


def test_warmups_only_from_other_starred_series_and_ids_come_from_your_history():
    lib = new_lib()
    add_review(lib, "z", "2026-10-30")
    r = json.loads((lib / "reviews" / "z" / "review.json").read_text(encoding="utf-8"))
    r["combo_ids"] = {"track_id": 11, "car_id": 22}
    (lib / "reviews" / "z" / "review.json").write_text(json.dumps(r), encoding="utf-8")
    all_s = sched()["series"]
    # a series racing week 8's track a week before GT4 Falken does, so it's a warm-up only once starred
    w9 = next(w for w in schedule.find_series(sched(), GT4)["weeks"] if w["season_week"] == 9)
    early = next((s for s in all_s if s["id"] != GT4 and any(schedule.same_track(x["track"], w9["track"]) and x["start"] < w9["start"] and x["start"] >= "2026-11-03" for x in s["weeks"])), None)
    assert focus(lib, series_id=GT4, week_no=9, car=MCL)["warmups"] == []
    if early:
        series.save(lib, [GT4, early["id"]], sched())
        assert focus(lib, series_id=GT4, week_no=9, car=MCL)["warmups"][0]["series"] == early["name"]
    z = focus(lib, car=MCL, track=ZOLDER)
    assert z["track_id"] == 11 and z["car_id"] == 22
    other = focus(lib, car=MCL, track="Road America - Full Course")   # a new track: the car is still known
    assert other["track_id"] is None and other["car_id"] == 22


def test_the_card_and_prep_page_carry_over():
    lib = zolder_lib()
    notes.save(lib, {"track": ZOLDER, "fields": {"lap1": "Stay right"}, "car_notes": {}})
    built = lib / "prep" / f"{prep.library.slugify(ZOLDER_REVIEW)}_{prep.library.slugify(MCL)}.html"
    built.parent.mkdir()
    built.write_text("<title>x</title>", encoding="utf-8")
    d = focus(lib, series_id=GT4, week_no=7, car=MCL)
    assert d["has_notes"] and d["card"]["fields"]["lap1"] == "Stay right" and d["page"] == "prep/" + built.name


def test_plan_strip_matches_the_season_plan_and_can_be_skipped():
    lib = zolder_lib()
    series.save(lib, [GT4], sched())
    d = focus(lib, series_id=GT4, week_no=7, car=MCL)
    P = prepare.plan(lib, sched(), series.load(lib), [], schedule.Matcher(), TODAY)
    assert [w["no"] for w in d["plan"]["weeks"]] == [w["season_week"] for w in P["weeks"]]
    assert [w["no"] for w in d["plan"]["weeks"] if w["flag"]] == [w["season_week"] for w in P["weeks"] if w["flag"]]
    assert d["plan"]["weeks"][0]["now"] and d["plan"]["weeks"][0]["short"]
    assert focus(lib, series_id=GT4, week_no=7, car=MCL, with_plan=False)["plan"] is None
    # an unstarred series still gets a strip, and looking at it doesn't star it
    before = (lib / "my-series.json").read_text(encoding="utf-8")
    o = focus(lib, series_id="sports-car-challenge-by-falken-tyre", week_no=7, car=MCL)
    assert o["plan"]["weeks"] and (lib / "my-series.json").read_text(encoding="utf-8") == before


# ---------------------------------------------------------------- remembering
def test_choice_saves_beside_stars_and_drops_anything_odd():
    lib = new_lib()
    assert series.load_choice(lib)["car"] is None
    series.save_choice(lib, {"car": MCL, "series": GT4, "week": 9, "follow_now": True, "season": "S", "x": 1})
    got = series.load_choice(lib)
    assert got["car"] == MCL and got["week"] == 9 and got["follow_now"] and "x" not in got
    series.save_choice(lib, {"car": "<img>", "series": 5, "week": "9", "follow_now": "yes"})
    got = series.load_choice(lib)
    assert got["car"] is None and got["series"] is None and got["week"] is None and not got["follow_now"]
    (lib / "prepare-choice.json").write_text("not json", encoding="utf-8")
    assert series.load_choice(lib)["car"] is None
    assert not (lib / "my-series.json").exists()


# ---------------------------------------------------------------- server
_SRV = {}
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_REAL = {"view": prepare.view, "focus": prepare.focus}


def srv():
    if "s" not in _SRV:
        prepare.view = lambda cfg, api=None, sessions=None, today=None: _REAL["view"](cfg, api, sessions, TODAY)
        prepare.focus = lambda *a, **k: _REAL["focus"](*a, **{**k, "today": TODAY})
        s = S.DebriefServer(heartbeat_timeout=3600)
        threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
        _SRV["s"] = s
    return _SRV["s"]


def call(rel, body=None, headers=None):
    hdr = {"X-Debrief": "1"} if headers is None else headers
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdr = {**hdr, "Content-Type": "application/json"}
    rq = urllib.request.Request(srv().url + rel, data=data, headers=hdr)
    try:
        with OP.open(rq, timeout=20) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_endpoints_have_the_same_safety_as_their_neighbours():
    lib = zolder_lib()
    write_settings(lib)
    code, _ = call("api/prepfocus?series=" + GT4, headers={})
    assert code == 404
    code, _ = call("api/prepare-choice", {"car": MCL}, headers={})
    assert code in (403, 404)
    code, body = call("api/prepfocus?car=%3Cimg%3E")
    assert code == 400 and b"Pick a car" in body
    code, _ = call("api/prepfocus?week=abc")
    assert code == 400
    code, body = call(f"api/prepfocus?series={GT4}&week=7&car=McLaren%20570S%20GT4")
    d = json.loads(body)
    assert code == 200 and d["ok"] and d["track"] == ZOLDER and d["pace"]["dry"]["best"] == 98.069
    code, body = call("api/prepare-choice", {"car": MCL, "series": GT4, "week": 7})
    assert code == 200 and json.loads(body)["choice"]["week"] == 7
    assert json.loads(call("api/prepare")[1])["choice"]["series"] == GT4


def test_no_garage61_request_for_the_view_or_catalog():
    class Spy:
        requests_made = 0
        def cached_items(self, kind):
            return [{"id": 900, "name": "Brand New Car"}] if kind == "cars" else [{"id": 901, "name": "Brand New Track"}]
        def __getattr__(self, name):
            raise AssertionError("asked Garage 61 for " + name)
    lib = zolder_lib()
    d = _REAL["focus"]({"library_folder": str(lib)}, Spy(), [], GT4, 7, MCL, None, True, TODAY)
    assert d["ok"]
    v = _REAL["view"]({"library_folder": str(lib)}, Spy(), [], TODAY)
    assert "Brand New Car" in [c["name"] for c in v["catalog"]["cars"]] and "Brand New Track" in v["catalog"]["tracks"]


def test_ui_rules():
    js = (UI / "app.js").read_text(encoding="utf-8")
    html = (UI / "index.html").read_text(encoding="utf-8")
    part = js[js.index("function renderPrepare()"):js.index("$('btnSchDl').addEventListener")]
    assert "innerHTML" not in part and "—" not in part
    sec = html[html.index('<section id="vPrep"'):html.index('<section id="vGoals"')]
    assert "—" not in sec and not re.search(r"\son[a-z]+=", sec)
    for i in ("fPrepAny", "prepJoin", "schPill", "btnSchLoad", "intelPanel", "planWeeks", "mySeries"):
        assert f'id="{i}"' in sec, i
    more = sec[sec.index('id="prepMore"'):]
    for i in ("btnSchDl", "btnSchLoad", "btnIntel", "fPrepAny", "prepJoin"):
        assert f'id="{i}"' in more, i
    assert 'id="seriesPicker"' not in sec and "pickAll" not in sec


# ---------------------------------------------------------------- the window
_PW = {}


def browser():
    if "b" in _PW:
        return _PW["b"]
    try:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        _PW["pw"], _PW["b"] = pw, pw.chromium.launch()
    except Exception:  # noqa: BLE001
        print("  SKIPPED: Playwright/Chromium not available")
        _PW["b"] = None
    return _PW["b"]


def window(lib, width=1300, routes=None, token=""):
    b = browser()
    if not b:
        return None
    write_settings(lib, token)
    ctx = b.new_context(viewport={"width": width, "height": 860})
    pg = ctx.new_page()
    pg.set_default_timeout(15000)
    pg.errs = []
    pg.on("pageerror", lambda e: pg.errs.append(str(e)))
    pg.route("**/api/sync", lambda r: r.fulfill(status=200, content_type="application/json", body='{"ok":false,"error":"off"}'))
    for pat, fn in (routes or {}).items():
        pg.route(pat, fn)
    pg.goto(srv().url)
    pg.wait_for_function("() => document.querySelector('.navbtn[data-nav=prep]') && document.getElementById('vWelcome') && !window.__none")
    open_prepare(pg)
    return pg, ctx


def open_prepare(pg):
    pg.wait_for_function("() => { const b = document.querySelector('.navbtn[data-nav=prep]'); if (!b) return false; b.click(); return !document.getElementById('vPrep').hidden }")
    pg.wait_for_function("() => document.getElementById('schPill').textContent !== 'Not loaded'")


def wait_view(pg, text):
    pg.wait_for_function("t => document.getElementById('mySeries').textContent.toLowerCase().includes(t.toLowerCase())", arg=text)


def test_window_empty_state():
    lib = new_lib()
    r = window(lib, token="fake")       # with no token and no reviews the app shows Welcome instead
    if not r:
        return
    pg, ctx = r
    try:
        wait_view(pg, "Pick a car to get started")
        st = pg.evaluate("() => ({cars: document.getElementById('pkCarsRow').hidden, stars: document.getElementById('pkStarsRow').hidden,"
                         " plan: document.getElementById('planPanel').hidden, more: document.getElementById('prepMore').open,"
                         " star: document.getElementById('pkStar').disabled, weeks: document.getElementById('pkWeeksRow').hidden})")
        assert st == {"cars": True, "stars": True, "plan": True, "more": False, "star": True, "weeks": True}, st
        # keyboard only: search a car, Enter, and the view fills from the schedule
        pg.focus("#pkCar")
        pg.keyboard.type("mclaren 570")
        pg.wait_for_function("() => !document.getElementById('pkCarList').hidden && document.querySelector('#pkCarList .comboopt.on')")
        assert pg.get_attribute("#pkCar", "aria-expanded") == "true"
        pg.keyboard.press("Enter")
        pg.wait_for_function("v => document.getElementById('pkCar').value === v", arg=MCL)
        wait_view(pg, MCL)
        assert not pg.is_hidden("#planPanel") or pg.wait_for_function("() => !document.getElementById('planPanel').hidden")
        assert not pg.errs, pg.errs
    finally:
        ctx.close()


def test_window_real_flow_keyboard_week_strip_star_and_memory():
    lib = zolder_lib()
    r = window(lib)
    if not r:
        return
    pg, ctx = r
    try:
        wait_view(pg, MCL)
        assert pg.evaluate("() => [...document.querySelectorAll('#pkCars button')].map(b => b.textContent)") == [MCL]
        pg.wait_for_function("() => document.querySelector('#pkWeeks button.on')")
        assert pg.inner_text("#pkWeeks button.on") == "This week"
        names = pg.evaluate("() => [...document.querySelectorAll('#pkWeeks button')].map(b => b.textContent)")
        assert names[:3] == ["This week", "Next week", "Week 9"], names

        # the series dropdown is only series using this car, with Browse all series inside it
        pg.focus("#pkSeries")
        pg.keyboard.press("ArrowDown")
        pg.wait_for_function("() => !document.getElementById('pkSeriesList').hidden")
        opts = pg.evaluate("() => [...document.querySelectorAll('#pkSeriesList [role=option] .combot')].map(e => e.textContent)")
        assert "GT4 Falken Tyre Challenge" in opts and opts[-1] == "Browse all series" and "Mini Stock Rookie Series by Thrustmaster" not in opts
        pg.keyboard.press("Escape")
        assert pg.is_hidden("#pkSeriesList")
        pg.keyboard.type("gt4 falken")
        pg.keyboard.press("Enter")
        pg.wait_for_function("() => document.getElementById('pkSeries').value === 'GT4 Falken Tyre Challenge'")

        # the picked week fills the view; no in-game date anywhere, and the old view stays until the new one is in
        wait_view(pg, "Zolder")
        t = pg.inner_text("#mySeries")
        assert "1:38.069" in t and "1:49.753" in t and "Rolling" in t and "In-game" not in t and "—" not in t, t
        assert pg.evaluate("() => document.querySelector('.pacetbl tbody tr th').textContent") == "Dry"

        # no flash: while a slow answer is on its way, the view is never empty
        pg.evaluate("""() => { window.__emptied = 0; const m = document.getElementById('mySeries');
            new MutationObserver(() => { if (!m.textContent.trim()) window.__emptied++ }).observe(m, {childList: true}) }""")
        calls = []
        pg.route("**/api/prepfocus*", lambda rt: (calls.append(rt.request.url), rt.continue_()))
        pg.focus("#pkWeeks button:nth-child(2)")
        pg.keyboard.press("Enter")
        pg.wait_for_function("() => document.querySelector('#pkWeeks button.on').textContent === 'Next week'")
        pg.wait_for_function("() => document.getElementById('mySeries').textContent.includes('Next week')")
        assert pg.evaluate("() => window.__emptied") == 0
        assert pg.evaluate("() => document.activeElement.textContent") == "Next week"
        assert "plan=0" in calls[-1]        # a week change doesn't rework the season plan

        # the strip: one chip per remaining week, Needs prep where flagged, Enter selects above
        chips = pg.evaluate("() => [...document.querySelectorAll('#planWeeks button')].map(b => b.textContent)")
        assert len(chips) == 6 and any("Needs prep" in c for c in chips), chips
        pg.focus("#planWeeks button:nth-child(4)")
        pg.keyboard.press("Enter")
        pg.wait_for_function("() => document.querySelector('#pkWeeks button.on').textContent === 'Week 10'")
        pg.wait_for_function("() => document.querySelector('#planWeeks button.on').textContent.includes('Week 10')")

        # the star saves to the existing store and gives a quick button
        pg.focus("#pkStar")
        pg.keyboard.press("Enter")
        pg.wait_for_function("() => document.getElementById('pkStar').getAttribute('aria-pressed') === 'true'")
        pg.wait_for_function("() => document.querySelectorAll('#pkStars button').length === 1")
        assert [x["id"] for x in series.load(lib)["series"]] == [GT4]

        # remembered across visits, on the server, because the window's address changes each launch
        pg.wait_for_function("() => true")
        pg.wait_for_timeout(350)
        assert series.load_choice(lib)["week"] == 10
        pg.reload()
        open_prepare(pg)
        pg.wait_for_function("() => document.querySelector('#pkWeeks button.on') && document.querySelector('#pkWeeks button.on').textContent === 'Week 10'")
        assert pg.input_value("#pkSeries") == "GT4 Falken Tyre Challenge" and pg.input_value("#pkCar") == MCL

        # taking the star off keeps nothing behind
        pg.click("#pkStar")
        pg.wait_for_function("() => document.getElementById('pkStar').getAttribute('aria-pressed') === 'false'")
        assert series.load(lib)["series"] == []
        assert not pg.errs, pg.errs
    finally:
        ctx.close()


def test_window_any_track_other_track_and_card():
    lib = zolder_lib()
    r = window(lib)
    if not r:
        return
    pg, ctx = r
    try:
        wait_view(pg, MCL)
        pg.focus("#pkTrack")
        pg.keyboard.type("made up raceway")
        pg.wait_for_function("() => document.querySelector('#pkTrackList .combogrp').textContent.includes('Nothing')")
        pg.keyboard.press("Escape")
        pg.focus("#pkTrack")
        pg.keyboard.type("road america - full")
        pg.keyboard.press("Enter")
        wait_view(pg, "doesn't race here")
        assert not pg.evaluate("() => document.querySelector('#pkWeeks button.on')")
        pg.click("#pvCard")
        pg.wait_for_function("() => document.getElementById('dlgCard').open")
        pg.wait_for_function("() => !document.getElementById('cardSave').disabled")
        pg.fill("#c_lap1", "<b>Stay right</b>")
        pg.click("#cardSave")
        pg.wait_for_function("() => !document.getElementById('dlgCard').open")
        wait_view(pg, "<b>Stay right</b>")      # shown as text
        assert pg.evaluate("() => document.querySelectorAll('#mySeries .cardview b').length") == 1
        # the tools are under More
        assert not pg.is_visible("#btnSchLoad")
        pg.click("#prepMore summary")
        pg.wait_for_function("() => document.getElementById('prepMore').open")
        assert pg.is_visible("#btnSchLoad") and pg.is_visible("#btnIntel")
        assert not pg.errs, pg.errs
    finally:
        ctx.close()


def test_window_see_race_week_series_disables_other_cars_weeks():
    lib = new_lib()
    r = window(lib, token="fake")       # with no token and no reviews the app shows Welcome instead
    if not r:
        return
    pg, ctx = r
    try:
        wait_view(pg, "Pick a car")
        pg.focus("#pkSeries")
        pg.keyboard.type("draft master")
        pg.keyboard.press("Enter")
        pg.wait_for_function("() => document.getElementById('pkSeries').value.startsWith('Draft Master')")
        car = pg.input_value("#pkCar")
        assert car and "see race" not in car.lower()
        pg.wait_for_function("() => document.querySelectorAll('#pkWeeks button').length > 1")
        st = pg.evaluate("() => [...document.querySelectorAll('#pkWeeks button')].map(b => [b.disabled, b.title])")
        for off, title in st:
            assert off == title.startswith("Other cars"), st
        assert not pg.errs, pg.errs
    finally:
        ctx.close()


def test_window_build_uses_known_ids_or_fetches_the_lists():
    lib = zolder_lib()
    posted, focus_calls, fetched = [], [], []

    def fake_focus(rt):
        focus_calls.append(1)
        ids = {"track_id": 5, "car_id": 6} if fetched else {"track_id": None, "car_id": None}
        rt.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True, "track": ZOLDER, "car": MCL, "series": None, "week": None, "race": None,
                   "pace": None, "status": None, "corners": [], "warmups": [], "card": {"fields": {}, "car_notes": {}}, "has_notes": False, "shared_notes": 0,
                   "racecraft": None, "rivals": [], "racecraft_note": None, "racecraft_wait": None, "page": None, "plan": None, **ids}))

    def fake_combos(rt):
        if "fetch=1" in rt.request.url:
            fetched.append(1)
        rt.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True, "combos": [], "tracks": [{"id": 5, "name": "T"}] if fetched else None,
                   "cars": [{"id": 6, "name": "C"}] if fetched else None, "requests": 0, "prep_available": True, "token": True}))

    def fake_prep(rt):
        posted.append(json.loads(rt.request.post_data))
        rt.fulfill(status=200, content_type="application/json", body='{"ok":false,"error":"stopped by the test"}')

    r = window(lib, routes={"**/api/prepfocus*": fake_focus, "**/api/combos*": fake_combos, "**/api/prep": fake_prep}, token="fake")
    if not r:
        return
    pg, ctx = r
    try:
        pg.wait_for_function("() => document.getElementById('pvBuild') && !document.getElementById('pvBuild').disabled")
        assert not fetched
        pg.click("#pvBuild")
        pg.wait_for_function("() => document.getElementById('banner').textContent.includes('stopped by the test')")
        assert fetched and posted == [{"track_id": 5, "car_id": 6}], (fetched, posted)
        # now the ids are known: straight to the job, nothing fetched again
        n = len(fetched)
        pg.click("#pvBuild")
        pg.wait_for_function("() => true")
        pg.wait_for_timeout(100)
        assert len(fetched) == n and len(posted) == 2
        assert not pg.errs, pg.errs
    finally:
        ctx.close()
        write_settings()


def test_window_no_token_says_how_to_build():
    r = window(zolder_lib(), token="")
    if not r:
        return
    pg, ctx = r
    try:
        pg.wait_for_function("() => document.getElementById('pvBuild')")
        assert pg.is_disabled("#pvBuild") and "token in Settings" in pg.inner_text("#pvMsg")
    finally:
        ctx.close()


def test_window_phone_has_no_sideways_scroll():
    lib = zolder_lib()
    r = window(lib, width=390)
    if not r:
        return
    pg, ctx = r
    try:
        wait_view(pg, MCL)
        pg.focus("#pkSeries")
        pg.keyboard.press("ArrowDown")
        pg.wait_for_function("() => !document.getElementById('pkSeriesList').hidden")
        d = pg.evaluate("() => { const v = document.getElementById('vPrep'); return v.scrollWidth - v.clientWidth }")
        assert d <= 0, d
    finally:
        ctx.close()


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
    if _PW.get("b"):
        _PW["b"].close()
        _PW["pw"].stop()
    if "s" in _SRV:
        _SRV["s"].shutdown()
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
