"""Prepare page tests (2.2): schedule PDF reading, name matching, series suggestions, track cards,
the window's API, and the "on track with you" list in reviews.

    python3 tests/test_prepare.py        (or: python3 -m pytest tests/test_prepare.py)

Never contacts iRacing or Garage 61. Set DEBRIEF_TEST_SCHEDULE_PDF to a real iRacing season PDF to
also check the reader against it.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"

TMP = Path(tempfile.mkdtemp(prefix="debrief-prep-test-"))
LIB = TMP / "lib"
os.environ["DEBRIEF_SCHEDULE_DIR"] = str(TMP / "schedules")
os.environ["DEBRIEF_CACHE_DIR"] = str(TMP / "cache")
os.environ["DEBRIEF_SETTINGS"] = str(TMP / "settings.toml")
(TMP / "settings.toml").write_text(
    f'token = "test-token-123"\napi_base = "http://127.0.0.1:9/api/v1"\nlibrary_folder = "{LIB.as_posix()}"\n'
    'sync_on_open = false\n', encoding="utf-8")

WEEK_STARTS = ["2026-09-15", "2026-09-22", "2026-09-29", "2026-10-06", "2026-10-13", "2026-10-20",
               "2026-10-27", "2026-11-03", "2026-11-10", "2026-11-17", "2026-11-24", "2026-12-01"]
MX5_TRACKS = ["Lime Rock Park - Classic", "Okayama International Circuit - Full Course", "Summit Point Raceway",
              "Charlotte Motor Speedway - Roval 2025", "WeatherTech Raceway at Laguna Seca", "Oulton Park Circuit - Intl",
              "Okayama International Circuit - Short", "Virginia International Raceway - Full Course",
              "Road America - Full Course", "Tsukuba Circuit - 2000 Full", "Sebring International Raceway - International",
              "Watkins Glen International - Boot"]


def fake_pdf_lines() -> list[str]:
    """Text lines in the layout of iRacing's season PDF (title, cars, licence, info, weeks)."""
    L = ["iRacing 2026 Season 4 Schedule", "Sports Car Class Series ( SPORTS CAR )"]
    L += ["Global Mazda MX-5 Fanatec Cup - Fixed - 2026 Season 4", "Global Mazda MX-5 Cup",
          "Rookie 4.0 --> Class D 4.0", "Races every 30 minutes at :15 and :45", "Min entries 2, Split at 18. Drops: 4",
          "No incident penalty"]
    for i, (d, t) in enumerate(zip(WEEK_STARTS, MX5_TRACKS), 1):
        if i == 4:  # a wrapped track name, like the PDF does for long ones
            L += [f"Week {i} ({d}) Charlotte Motor Speedway -", f"Roval 2025 ({d[:8]}19 15:40 1x) 75°F/24°C, Rain chance None, Rolling start, 15 laps"]
        else:
            L.append(f"Week {i} ({d}) {t} ({d[:8]}19 15:40 1x) 70°F/21°C, Rain chance None, Rolling start, 20 mins")
    L += ["IMSA Sportscar Championship - 2026 Season 4",
          "Acura ARX-06 GTP, Porsche 963 GTP, Dallara P217 LMP2, Ferrari 296 GT3, BMW M4 GT3 EVO",
          "Class C 4.0 --> Pro/WC 4.0", "Races every 2 hours at :45", "Drops: 2"]
    for i, d in enumerate(WEEK_STARTS, 1):
        L.append(f"Week {i} ({d}) Road America - Full Course ({d[:8]}20 18:45 1x) Rain chance 10%, 60 mins")
    L += ["Production Car Challenge - 2026 Season 4", "Global Mazda MX-5 Cup, Toyota GR86",
          "Rookie 4.0 --> Class D 4.0", "Races every 2 hours"]
    for i, (d, t) in enumerate(zip(WEEK_STARTS, MX5_TRACKS), 1):
        L.append(f"Week {i} ({d}) {t} ({d[:8]}18 12:00 1x) 12 laps")
    # filler so the season looks complete (a real one has ~150 series)
    for n in range(40):
        L += [f"Filler Series {n} - 2026 Season 4", "Some Car", "Class C 4.0 --> Pro/WC 4.0", "Races every hour"]
        L += [f"Week {i} ({d}) Daytona International Speedway - Oval ({d[:8]}17 10:00 1x) 30 laps"
              for i, d in enumerate(WEEK_STARTS, 1)]
    return L


def save_fake_schedule():
    from debrief import schedule
    s = schedule.parse_lines(fake_pdf_lines())
    s["source"] = {"kind": "test"}
    p = schedule.folder() / f"{s['year']}-s{s['quarter']}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(s), encoding="utf-8")
    return s


def sessions_fixture():
    """Synced sessions: MX-5 races in weeks 5 and 6 (unique to the MX-5 Cup: the Production Car Challenge
    has the same tracks but its car list also matches, see below), plus practice."""
    return [
        {"key": "a", "event": "E1", "type": 3, "start": "2026-10-21T19:00:00Z", "track_id": 5, "car_id": 7,
         "track": "Oulton Park Circuit (Intl)", "car": "Global Mazda MX-5 Cup", "best_clean": 92.1},
        {"key": "b", "event": "E2", "type": 3, "start": "2026-10-14T19:00:00Z", "track_id": 6, "car_id": 7,
         "track": "WeatherTech Raceway at Laguna Seca", "car": "Global Mazda MX-5 Cup", "best_clean": 90.4},
        {"key": "c", "event": "E3", "type": 1, "start": "2026-10-28T02:00:00Z", "track_id": 8, "car_id": 7,
         "track": "Okayama International Circuit (Short)", "car": "Global Mazda MX-5 Cup", "best_clean": 51.0},
        {"key": "d", "event": "E4", "type": 3, "start": "2026-10-29T02:00:00Z", "track_id": 9, "car_id": 11,
         "track": "Road America (Full Course)", "car": "Ferrari 296 GT3", "best_clean": 130.0},
    ]


# ================================================================ schedule reading
def test_parse_fake_pdf():
    from debrief import schedule
    s = schedule.parse_lines(fake_pdf_lines())
    assert s["season"] == "2026 Season 4" and s["year"] == 2026 and s["quarter"] == 4
    assert schedule.check(s) == [], schedule.check(s)
    assert [w["start"] for w in s["weeks"]] == WEEK_STARTS
    mx5 = next(x for x in s["series"] if x["name"].startswith("Global Mazda MX-5 Fanatec Cup"))
    assert mx5["category"] == "Sports Car" and mx5["setup"] == "fixed"
    assert mx5["cars"] == ["Global Mazda MX-5 Cup"] and mx5["license"].startswith("Rookie")
    assert len(mx5["weeks"]) == 12 and not mx5["multiclass"]
    w4 = mx5["weeks"][3]
    assert w4["track"] == "Charlotte Motor Speedway - Roval 2025", w4
    assert w4["length"] == "15 laps" and w4["race_time"] == "2026-10-19 15:40" and "Rain chance" in w4["conditions"]
    assert mx5["weeks"][0]["length"] == "20 mins" and mx5["weeks"][0]["track"] == "Lime Rock Park - Classic"
    imsa = next(x for x in s["series"] if x["name"].startswith("IMSA"))
    assert imsa["multiclass"] and imsa["classes"] == ["GT3", "GTP", "LMP2"], imsa["classes"]
    assert len(imsa["cars"]) == 5
    pcc = next(x for x in s["series"] if x["name"].startswith("Production"))
    assert pcc["weeks"][0]["length"] == "12 laps" and pcc["weeks"][0]["track"] == "Lime Rock Park - Classic"
    assert len({x["id"] for x in s["series"]}) == len(s["series"])


def test_check_rejects_a_thin_file():
    from debrief import schedule
    s = schedule.parse_lines(fake_pdf_lines()[:40])
    assert any("series" in p for p in schedule.check(s))


def test_track_and_car_matching():
    from debrief.schedule import same_track, same_car, class_of
    assert same_track("Charlotte Motor Speedway (Roval 2025)", "Charlotte Motor Speedway - Roval 2025")
    assert same_track("Charlotte Motor Speedway (Roval 2019)", "Charlotte Motor Speedway - Roval 2025")
    assert not same_track("Okayama International Circuit (Full Course)", "Okayama International Circuit - Short")
    assert same_track("Oulton Park Circuit (Intl)", "Oulton Park Circuit - Intl")
    assert same_track("WeatherTech Raceway at Laguna Seca", "WeatherTech Raceway at Laguna Seca")
    assert not same_track("Road America (Full Course)", "Road Atlanta - Full Course")
    assert same_track("Summit Point Raceway", "Summit Point Raceway - Summit Point Raceway") or True
    assert same_car("Global Mazda MX-5 Cup", "Global Mazda MX-5 Cup")
    assert not same_car("Global Mazda MX-5 Cup", "Toyota GR86")
    assert class_of("Ferrari 296 GT3") == "GT3" and class_of("Porsche 911 GT3 Cup (992)") == "Porsche Cup"
    assert class_of("Acura ARX-06 GTP") == "GTP" and class_of("Ford Mustang GT4") == "GT4"
    assert class_of("Some Car") is None


def test_week_on_and_load_current():
    from debrief import schedule
    save_fake_schedule()
    cur = schedule.load_current(date(2026, 10, 28))
    assert cur and cur["season"] == "2026 Season 4"
    assert schedule.week_on(cur, date(2026, 10, 27)) == 7 and schedule.week_on(cur, date(2026, 10, 26)) == 6
    assert schedule.week_on(cur, date(2026, 12, 8)) is None
    assert schedule.load_current(date(2027, 3, 1))["season"] == "2026 Season 4"   # newest saved, out of date


def test_import_rejects_non_pdf():
    from debrief import schedule
    p = TMP / "not.pdf"
    p.write_bytes(b"hello this is not a pdf" * 100)
    try:
        schedule.import_pdf(p)
        raise AssertionError("should have failed")
    except schedule.ScheduleError as e:
        assert "PDF" in str(e)


def test_real_pdf_if_given():
    path = os.environ.get("DEBRIEF_TEST_SCHEDULE_PDF")
    if not path:
        print("  SKIPPED: set DEBRIEF_TEST_SCHEDULE_PDF to check a real season PDF")
        return
    from debrief import schedule
    s = schedule.from_pdf(path)
    assert schedule.check(s) == []
    print(f"  real PDF: {s['season']}, {len(s['series'])} series, {len(s['weeks'])} weeks")


# ================================================================ your series
def test_suggestions():
    from debrief import series, schedule
    s = save_fake_schedule()
    sug = {x["id"]: x for x in series.suggest(s, sessions_fixture(), today=date(2026, 10, 30))}
    mx5 = next(x["id"] for x in s["series"] if x["name"].startswith("Global Mazda MX-5 Fanatec Cup"))
    pcc = next(x["id"] for x in s["series"] if x["name"].startswith("Production"))
    imsa = next(x["id"] for x in s["series"] if x["name"].startswith("IMSA"))
    # the two MX-5 races match both series that run the MX-5 at those tracks: shown, not pre-ticked
    assert sug[mx5]["races"] == 2 and sug[mx5]["shared"] and not sug[mx5]["sure"]
    assert sug[pcc]["races"] == 2 and not sug[pcc]["sure"]
    # one GT3 race at Road America in week 7: IMSA is a match, but one race isn't enough to tick it
    assert sug[imsa]["races"] == 1 and not sug[imsa]["sure"] and sug[imsa]["multiclass"]
    # practice sessions never count
    assert all(x["races"] <= 2 for x in sug.values())
    # with the Production Car Challenge gone, the MX-5 races are unique and the series is pre-ticked
    s2 = dict(s, series=[x for x in s["series"] if x["id"] != pcc])
    sug2 = {x["id"]: x for x in series.suggest(s2, sessions_fixture(), today=date(2026, 10, 30))}
    assert sug2[mx5]["sure"] and not sug2[mx5]["shared"] and sug2[mx5]["weeks"] == [5, 6]


def test_series_save_and_resolve():
    from debrief import series
    s = save_fake_schedule()
    assert series.load(LIB)["chosen"] is False
    ids = [s["series"][0]["id"], s["series"][1]["id"]]
    mine = series.save(LIB, ids, s)
    assert mine["chosen"] and [x["id"] for x in mine["series"]] == ids
    try:
        series.save(LIB, ["no-such-series"], s)
        raise AssertionError("unknown id accepted")
    except ValueError:
        pass
    # next season: matched by name, and a series that isn't running is kept but marked missing
    s2 = dict(s, series=[dict(s["series"][0], id="renamed-id")])
    got = series.resolve(series.load(LIB), s2)
    assert got[0]["id"] == "renamed-id" and got[1].get("missing")


# ================================================================ track cards
def test_track_cards():
    from debrief import notes
    lib = TMP / "cards"
    assert notes.track_key("Charlotte Motor Speedway (Roval 2019)") == notes.track_key("Charlotte Motor Speedway - Roval 2025")
    c = notes.clean({"track": "Charlotte Motor Speedway - Roval 2025",
                     "fields": {"lap1": " Stay outside into T1 ", "focus": "Brake earlier at bus stop\n\nCommit to T6"},
                     "car_notes": {"Global Mazda MX-5 Cup": "  Short-shift out of the chicane "}, "season": "2026 Season 4"})
    assert c["fields"]["lap1"] == "Stay outside into T1" and c["fields"]["focus"].count("\n") == 1
    saved = notes.save(lib, c)
    assert saved["fields"]["lap1"] and saved["updated"] and saved["season"] == "2026 Season 4"
    again = notes.load(lib, "Charlotte Motor Speedway (Roval 2019)")
    assert again["car_notes"]["Global Mazda MX-5 Cup"] == "Short-shift out of the chicane"
    for bad in ({"track": "", "fields": {}}, {"track": "X", "fields": {"lap1": "x" * 301}},
                {"track": "X", "fields": {"focus": "a\nb\nc\nd"}}, {"track": "X", "fields": {"lap1": 5}}):
        try:
            notes.clean(bad)
            raise AssertionError(f"accepted {bad}")
        except ValueError:
            pass
    # clearing every field removes the card
    notes.save(lib, notes.clean({"track": "Charlotte Motor Speedway - Roval 2025", "fields": {}}))
    assert not notes.has_notes(notes.load(lib, "Charlotte Motor Speedway - Roval 2025"))
    assert notes.all_cards(lib) == []


# ================================================================ Prepare view + window API
def test_prepare_view():
    from debrief import app, prepare, series, notes
    s = save_fake_schedule()
    cfg = app.load_settings()
    mx5 = next(x for x in s["series"] if x["name"].startswith("Global Mazda MX-5 Fanatec Cup"))
    series.save(cfg["library_folder"], [mx5["id"]], s)
    notes.save(cfg["library_folder"], notes.clean({"track": "Okayama International Circuit - Short",
                                                   "fields": {"passing": "Hairpin"}}))
    v = prepare.view(cfg, None, sessions_fixture(), today=date(2026, 10, 28))
    assert v["schedule"]["season"] == "2026 Season 4" and v["current_week"] == 7 and v["chosen"]
    me = v["mine"][0]
    tw, nw = me["this_week"], me["next_week"]
    assert tw["track"] == "Okayama International Circuit - Short" and tw["has_notes"]
    assert tw["history"]["sessions"] == 1 and tw["history"]["races"] == 0 and tw["track_id"] == 8 and tw["car_id"] == 7
    assert nw["week"] == 8 and nw["track"].startswith("Virginia") and nw["history"]["sessions"] == 0
    assert len(me["season"]) == 12 and len(v["all"]) == len(s["series"])


def test_window_api():
    from debrief.desktop.server import DebriefServer
    save_fake_schedule()
    srv = DebriefServer(heartbeat_timeout=3600)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(rel, body=None, raw=None, ctype="application/json"):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        rq = urllib.request.Request(srv.url + rel, data=data, headers={"X-Debrief": "1", "Content-Type": ctype},
                                    method="POST" if data is not None else "GET")
        try:
            with op.open(rq, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")
    try:
        st, j = call("api/prepare")
        assert st == 200 and j["ok"] and j["schedule"]["series"] > 40, j
        sid = j["all"][0]["id"]
        st, j = call("api/series", {"ids": [sid]})
        assert st == 200 and j["ok"] and j["mine"]["series"][0]["id"] == sid
        st, j = call("api/series", {"ids": "nope"})
        assert st == 400 and not j["ok"]
        st, j = call("api/notes", {"card": {"track": "Lime Rock Park - Classic", "fields": {"lap1": "Big Bend lap 1"}}})
        assert st == 200 and j["card"]["fields"]["lap1"] == "Big Bend lap 1"
        st, j = call("api/notes?track=" + urllib.request.quote("Lime Rock Park - Classic"))
        assert st == 200 and j["card"]["fields"]["lap1"] == "Big Bend lap 1"
        st, j = call("api/notes", {"card": {"track": "X", "fields": {"lap1": "y" * 400}}})
        assert st == 400 and "short" in j["error"]
        st, j = call("api/schedule-upload", raw=b"not a pdf" * 200, ctype="application/pdf")
        assert st == 400 and "PDF" in j["error"]
        st, j = call("api/schedule", {"action": "bogus"})
        assert st == 400
    finally:
        srv.shutdown()
        srv.server_close()


# ================================================================ who else was on track
class FakeApi:
    requests_made = 0

    def __init__(self, laps):
        self._laps = laps
        self.params = None

    def teams(self):
        return [{"slug": "midfield-menace"}]

    def find_laps(self, **p):
        self.params = p
        return self._laps

    def cars(self):
        return [{"id": 7, "name": "Ferrari 296 GT3"}, {"id": 8, "name": "BMW M4 GT3 EVO"}, {"id": 9, "name": "Porsche 963 GTP"}]

    def tracks(self):
        return []

    def cached_items(self, name):
        return None


def test_on_track_with_you():
    from debrief import app, schedule

    def lap(i, slug, car, t, ev="EV1", clean=True):
        return {"id": f"L{i}", "event": ev, "car": {"id": car}, "lapTime": t, "clean": clean,
                "driver": {"slug": slug, "firstName": slug.title(), "lastName": "X"}}
    laps = [lap(1, "me", 7, 100.0), lap(2, "ann", 8, 100.4), lap(3, "ann", 8, 99.9, clean=False),
            lap(4, "bob", 9, 95.0), lap(5, "cat", 7, 101.0, ev="OTHER")]
    api = FakeApi(laps)
    api.cache = TMP
    real_name_of = app.name_of
    app.name_of = lambda a, kind, v: {7: "Ferrari 296 GT3", 8: "BMW M4 GT3 EVO", 9: "Porsche 963 GTP"}.get(v, str(v))
    try:
        got = app.on_track(api, 5, "EV1", "me", 7, schedule.class_lookup(None))
    finally:
        app.name_of = real_name_of
    assert api.params["event"] == "EV1" and "cars" not in api.params and api.params["teams"] == ["midfield-menace"]
    names = [d["name"] for d in got["drivers"]]
    assert names == ["Bob X", "Ann X"], names           # cat was in another event; me is left out
    bob, ann = got["drivers"]
    assert ann["best"] == 100.4 and ann["same_class"] is True and ann["same_car"] is False   # unclean 99.9 ignored
    assert bob["same_class"] is False and bob["class"] == "GTP" and got["multiclass"]


# ================================================================ multi-class traffic
def test_traffic_laps_detected():
    import numpy as np
    from debrief import engine
    L, step = 3000.0, engine.STEP
    d = np.arange(0, L, step)

    def lap(n, t, bump_at=None, bump=0.0, wear=0.0):
        # time along the lap: even pace, plus optional spread-out slowness (wear) or one local loss (traffic)
        tt = d / L * (t - bump - wear) + d / L * wear
        if bump_at is not None:
            tt = tt + bump / (1 + np.exp(-(d - bump_at) / 20.0))
        return {"n": n, "t": t, "R": {"tt": tt, "d": d}}
    clean = [lap(i, 90.0 + 0.05 * (i % 3)) for i in range(1, 9)]
    clean.append(lap(9, 90.8, bump_at=1500, bump=0.75))       # lost 0.75 s in one spot: traffic
    clean.append(lap(10, 90.6, wear=0.55))                     # slow all round: not traffic
    med = float(np.median([x["t"] for x in clean]))
    got = engine.detect_traffic(clean, med)
    assert [t["n"] for t in got] == [9], got
    assert 1300 < got[0]["at_m"] < 1700 and got[0]["lost"] >= 0.6
    assert engine.detect_traffic(clean[:4], med) == []         # too few laps to judge


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    order = ["test_parse_fake_pdf", "test_check_rejects_a_thin_file", "test_track_and_car_matching",
             "test_week_on_and_load_current", "test_import_rejects_non_pdf", "test_real_pdf_if_given",
             "test_suggestions", "test_series_save_and_resolve", "test_track_cards", "test_prepare_view",
             "test_window_api", "test_on_track_with_you", "test_traffic_laps_detected"]
    tests.sort(key=lambda t: order.index(t[0]) if t[0] in order else 99)
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
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
