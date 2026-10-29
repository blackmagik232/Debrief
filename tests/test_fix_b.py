"""Debrief 2.2.2 fixes, team B (sync, Garage 61 client, schedule PDF, series, Prepare, track cards, iRacing).

    python3 tests/test_fix_b.py        (or: python3 -m pytest tests/test_fix_b.py)

Never contacts Garage 61 or iRacing: the sync tests use the fake Garage 61 from test_sync.py.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
TMP = Path(tempfile.mkdtemp(prefix="debrief-fixb-"))
os.environ.setdefault("DEBRIEF_SCHEDULE_DIR", str(TMP / "schedules"))
os.environ.setdefault("DEBRIEF_CACHE_DIR", str(TMP / "cache"))
os.environ.setdefault("DEBRIEF_SETTINGS", str(TMP / "settings.toml"))

from test_prepare import fake_pdf_lines  # noqa: E402
import test_sync as TS  # noqa: E402

QUIET = dict(log=lambda m: None)


def _sched(lines):
    from debrief import schedule
    return schedule.parse_lines(lines)


def _series(s, prefix):
    return next(x for x in s["series"] if x["name"].startswith(prefix))


# ================================================================ matching (H1, H2, M7, C3)
def test_same_track_is_strict():
    from debrief.schedule import same_track as T
    no = [("Indianapolis Motor Speedway (Oval)", "Indianapolis Motor Speedway - Grand Prix"),
          ("Lucas Oil Indianapolis Raceway Park (Oval)", "Indianapolis Motor Speedway - Oval"),
          ("Atlanta Motor Speedway", "Road Atlanta - Full Course"),
          ("The Dirt Track at Charlotte", "Charlotte Motor Speedway - Oval"),
          ("Watkins Glen International (Boot)", "Watkins Glen International - Classic Boot"),
          ("Nürburgring Grand-Prix-Strecke (Grand Prix)", "Nürburgring Combined (Gesamtstrecke VLN)"),
          ("Okayama International Circuit (Full Course)", "Okayama International Circuit - Short"),
          ("Road America (Full Course)", "Road Atlanta - Full Course"),
          ("Lime Rock Park (Classic)", "Lime Rock Park - Chicane")]
    yes = [("Nürburgring Nordschleife", "Nurburgring Nordschleife"),
           ("Nurburgring Combined (Gesamtstrecke VLN)", "Nürburgring Combined - Gesamtstrecke VLN"),
           ("Autódromo José Carlos Pace - Grand Prix", "Autodromo Jose Carlos Pace (Grand Prix)"),
           ("Charlotte Motor Speedway (Roval 2019)", "Charlotte Motor Speedway - Roval 2025"),
           ("Oulton Park Circuit (Intl)", "Oulton Park Circuit - International"),
           ("Autodromo Nazionale Monza (Grand Prix)", "Autodromo Nazionale Monza - GP"),
           ("Road America", "Road America - Full Course"),
           ("Daytona International Speedway (Oval)", "Daytona International Speedway - Oval"),
           ("Watkins Glen International (Boot)", "Watkins Glen International - Boot")]
    for a, b in no:
        assert not T(a, b) and not T(b, a), (a, b)
    for a, b in yes:
        assert T(a, b) and T(b, a), (a, b)


def test_same_car_is_strict():
    from debrief.schedule import same_car as C
    for a, b in [("Porsche 911 GT3 Cup (992)", "Porsche 911 GT3 R (992)"), ("BMW M4 GT3", "BMW M4 GT3 EVO"),
                 ("Cadillac V-Series.R GTP", "GTP"), ("Global Mazda MX-5 Cup", "Toyota GR86"),
                 ("Porsche 911 GT3 Cup (992)", "GT3 Class")]:
        assert not C(a, b) and not C(b, a), (a, b)
    for a, b in [("Mercedes-AMG GT3 2020", "Mercedes-AMG GT3"), ("Global Mazda MX-5 Cup", "Global Mazda MX-5 Cup"),
                 ("Ferrari 296 GT3", "GT3 Class"), ("Škoda Octavia", "Skoda Octavia")]:
        assert C(a, b) and C(b, a), (a, b)


def test_matcher_prefers_garage61_ids_and_refuses_ambiguous_names():
    from debrief import schedule, prepare
    tracks = [{"id": 1, "name": "Indianapolis Motor Speedway", "variant": "Oval"},
              {"id": 2, "name": "Indianapolis Motor Speedway", "variant": "Grand Prix"},
              {"id": 3, "name": "Road America", "variant": "Full Course"},
              {"id": 4, "name": "Okayama International Circuit", "variant": "Full Course"},
              {"id": 5, "name": "Okayama International Circuit", "variant": "Full Course"}]   # two ids, one name
    cars = [{"id": 10, "name": "Porsche 911 GT3 Cup (992)"}, {"id": 11, "name": "Porsche 911 GT3 R (992)"},
            {"id": 12, "name": "BMW M4 GT3"}, {"id": 13, "name": "BMW M4 GT3 EVO"}]
    m = schedule.Matcher(tracks, cars)
    assert m.track_id("Indianapolis Motor Speedway - Grand Prix") == 2
    assert m.track_id("Indianapolis Motor Speedway - Oval") == 1
    assert m.track_id("Okayama International Circuit - Full Course") is None      # ambiguous: no id (L6)
    assert m.car_id("Porsche 911 GT3 R (992)") == 11 and m.car_id("BMW M4 GT3") == 12
    # a session whose ids say otherwise doesn't match, even if its name looks close
    s = {"track_id": 1, "track": "Indianapolis Motor Speedway (Grand Prix)", "car_id": 10, "car": "x"}
    assert not m.track(s, "Indianapolis Motor Speedway - Grand Prix")
    assert m.track({"track_id": 2, "track": "?"}, "Indianapolis Motor Speedway - Grand Prix")
    assert not m.car(s, "Porsche 911 GT3 R (992)") and m.car(s, "Porsche 911 GT3 Cup (992)")

    class Api:
        def cached_items(self, n):
            return {"tracks": tracks, "cars": cars}[n]
    assert prepare._ids_from_lists(Api(), "Okayama International Circuit - Full Course", ["BMW M4 GT3 EVO"]) == (None, 13)


def test_prepare_history_ignores_other_layouts_and_cars():
    from debrief import prepare
    sessions = [
        {"type": 3, "start": "2026-10-01T00:00:00Z", "track_id": 1, "car_id": 5, "best_clean": 40.0,
         "track": "Indianapolis Motor Speedway (Oval)", "car": "Dallara IR18"},
        {"type": 3, "start": "2026-10-02T00:00:00Z", "track_id": 2, "car_id": 6, "best_clean": 80.0,
         "track": "Indianapolis Motor Speedway (Grand Prix)", "car": "Porsche 911 GT3 Cup (992)"},
        {"type": 3, "start": "2026-10-03T00:00:00Z", "track_id": 2, "car_id": 7, "best_clean": 79.0,
         "track": "Indianapolis Motor Speedway (Grand Prix)", "car": "Porsche 911 GT3 R (992)"}]
    h = prepare.history_at(sessions, "Indianapolis Motor Speedway - Grand Prix", ["Porsche 911 GT3 R (992)"])
    assert h["sessions"] == 1 and h["best"] == 79.0 and h["track_id"] == 2 and h["car_id"] == 7, h
    assert h["other_car_sessions"] == 1


# ================================================================ PDF parsing (H3, H4, M4, M5, M6, C1, C2, C4)
def test_lengths_in_hours_and_text_after_the_length():
    from debrief import schedule
    L = fake_pdf_lines()
    i = max(k for k, l in enumerate(L) if l.startswith("Week 12 (") and "Road America" in l and k < 60)
    L[i] = "Week 12 (2026-12-01) Road America - Full Course (2026-12-05 18:45 1x) Rain chance 10%, 6 hours"
    j = L.index("IMSA Sportscar Championship - 2026 Season 4") + 5
    L[j] = L[j].replace("60 mins", "2 hr 40 min")
    L = [l.replace(", 20 mins", ", 20 mins, Rolling start") if l.startswith("Week") else l for l in L]
    s = _sched(L)
    assert schedule.check(s) == [], schedule.check(s)
    imsa = _series(s, "IMSA")
    assert len(imsa["weeks"]) == 12 and imsa["weeks"][11]["length"] == "6 hours"
    assert imsa["weeks"][0]["length"] == "2 hours 40 mins" and imsa["weeks"][0]["conditions"] == "Rain chance 10%"
    assert _series(s, "Production")["weeks"][0]["track"] == "Lime Rock Park - Classic"
    mx = _series(s, "Global Mazda")
    assert len(mx["weeks"]) == 12 and mx["weeks"][0]["length"] == "20 mins"
    assert mx["weeks"][0]["track"] == "Lime Rock Park - Classic" and "Rolling start" in mx["weeks"][0]["conditions"]
    w = schedule._week_from({"week": 1, "start": "2026-09-15",
                             "text": "Daytona International Speedway - Road Course (2026-09-19 15:40 1x) 2.4 hrs"})
    assert w["length"] == "2.4 hours" and w["track"] == "Daytona International Speedway - Road Course"


def test_wrapped_series_title_and_race_time():
    from debrief import schedule
    L = fake_pdf_lines()
    j = L.index("Production Car Challenge - 2026 Season 4")
    L[j:j + 1] = ["Production Car Challenge -", "2026 Season 4"]
    i = next(k for k, l in enumerate(L) if l.startswith("Week 5 (2026-10-13) WeatherTech"))
    L[i:i + 1] = ["Week 5 (2026-10-13) WeatherTech Raceway at Laguna Seca (2026-10-",
                  "17 15:40 1x) 70°F/21°C, Rain chance None, Rolling start, 20 mins"]
    s = _sched(L)
    assert schedule.check(s) == [], schedule.check(s)
    pcc = _series(s, "Production")
    assert pcc["name"] == "Production Car Challenge" and pcc["cars"] == ["Global Mazda MX-5 Cup", "Toyota GR86"]
    assert len(_series(s, "IMSA")["weeks"]) == 12
    w5 = _series(s, "Global Mazda")["weeks"][4]
    assert w5["track"] == "WeatherTech Raceway at Laguna Seca" and w5["race_time"] == "2026-10-17 15:40", w5


def test_page_headers_and_footers_are_dropped():
    from debrief import schedule
    L = fake_pdf_lines()
    k = next(n for n, l in enumerate(L) if l.endswith("Charlotte Motor Speedway -"))
    L.insert(k + 1, "2026 Season 4 Schedule - Page 2")
    i = next(n for n, l in enumerate(L) if l.startswith("Week 6 ("))
    L.insert(i, "Season Schedule 2026 Season 4")
    L.insert(i, "Page 3")
    s = _sched(L)
    assert schedule.check(s) == [], schedule.check(s)
    assert not [x for x in s["series"] if "Schedule" in x["name"]]
    mx = _series(s, "Global Mazda")
    assert len(mx["weeks"]) == 12 and mx["weeks"][3]["track"] == "Charlotte Motor Speedway - Roval 2025"
    pages = [["iRacing 2026 Season 4 Schedule", "x%d" % n, "Week 1 (2026-09-15) A", "Footer text"] for n in range(5)]
    assert schedule._page_furniture(pages) == {"iRacing 2026 Season 4 Schedule", "Footer text"}


def test_series_named_iracing_is_kept():
    from debrief import schedule
    L = [l.replace("Filler Series 3 - ", "iRacing Porsche Cup - ") for l in fake_pdf_lines()]
    s = _sched(L)
    assert schedule.check(s) == [] and any(x["name"] == "iRacing Porsche Cup" for x in s["series"])


def test_series_without_licence_line_keeps_rules_out_of_cars():
    L = fake_pdf_lines()
    L.remove("Rookie 4.0 --> Class D 4.0")   # the MX-5 Cup's licence line is missing
    mx = _series(_sched(L), "Global Mazda")
    assert mx["cars"] == ["Global Mazda MX-5 Cup"], mx["cars"]
    assert mx["frequency"].startswith("Races every")


def test_check_rejects_merged_swallowed_and_odd_schedules():
    from debrief import schedule
    s = _sched(fake_pdf_lines())
    assert schedule.check(s) == []
    # two series glued together: duplicate week numbers
    bad = json.loads(json.dumps(s))
    a, b = bad["series"][0], bad["series"].pop(1)
    a["weeks"] += b["weeks"]
    assert any("merged" in p for p in schedule.check(bad)), schedule.check(bad)
    # more weeks than any season has
    bad = json.loads(json.dumps(s))
    bad["series"][0]["weeks"] = [dict(bad["series"][0]["weeks"][0], week=n) for n in range(1, 17)]
    assert any("merged" in p for p in schedule.check(bad))
    # a track that swallowed the next lines
    bad = json.loads(json.dumps(s))
    bad["series"][0]["weeks"][2]["track"] = "Summit Point Raceway Week 4 (2026-10-06) Charlotte"
    assert any("track names" in p for p in schedule.check(bad)), schedule.check(bad)
    bad["series"][0]["weeks"][2]["track"] = "Charlotte Motor Speedway - 2026 Season 4 Schedule - Page 2 Roval"
    assert any("track names" in p for p in schedule.check(bad))
    # the old parser's failure (no week ever closes) is now caught even if it happens
    L = [l.replace(", 20 mins", ", 20 mins, Rolling start") for l in fake_pdf_lines()]
    assert schedule.check(_sched(L)) == []


def test_check_allows_short_seasons_and_a_tba_week():
    from debrief import schedule
    s = _sched(fake_pdf_lines())
    short = json.loads(json.dumps(s))
    for x in short["series"]:
        x["weeks"] = x["weeks"][:6]
    short["weeks"] = short["weeks"][:6]
    assert schedule.check(short) == [], schedule.check(short)
    tba = json.loads(json.dumps(s))
    tba["series"][0]["weeks"][5]["track"] = ""
    assert schedule.check(tba) == [] and schedule.warnings(tba)
    assert "diagnostics" in s and s["diagnostics"]["series_without_license"] == 0


# ================================================================ series (L4, M9)
def test_resolve_prefers_name_over_a_reused_id():
    from debrief import series
    sched = {"series": [{"id": "gt-sprint", "name": "GT Sprint Series"},
                        {"id": "gt-sprint-2", "name": "GT Sprint Series Fixed"}]}
    mine = {"series": [{"id": "gt-sprint-2", "name": "GT Sprint Series"}]}     # last season's order
    got = series.resolve(mine, sched)
    assert got[0]["name"] == "GT Sprint Series" and got[0]["id"] == "gt-sprint"
    gone = series.resolve({"series": [{"id": "gt-sprint", "name": "Formula Vee"}]}, sched)
    assert gone[0].get("missing")


def test_suggest_ignores_races_before_week_one():
    from debrief import series
    s = _sched(fake_pdf_lines())
    mx_tracks = {"Lime Rock Park (Classic)", "Okayama International Circuit (Full Course)"}
    sess = [{"type": 3, "start": "2026-09-12T19:00:00Z", "track": "Lime Rock Park (Classic)",
             "car": "Global Mazda MX-5 Cup"},
            {"type": 3, "start": "2026-09-10T19:00:00Z", "track": "Lime Rock Park (Classic)",
             "car": "Global Mazda MX-5 Cup"}]
    assert mx_tracks and series.suggest(s, sess, today=date(2026, 10, 1)) == []
    sess.append({"type": 3, "start": "2026-09-16T19:00:00Z", "track": "Lime Rock Park (Classic)",
                 "car": "Global Mazda MX-5 Cup"})
    got = series.suggest(s, sess, today=date(2026, 10, 1))
    assert got and all(h["races"] == 1 and h["weeks"] == [1] for h in got), got


# ================================================================ track cards (C5, M7, L2)
def test_track_card_keys_fold_accents_and_never_share_unknown():
    from debrief import notes
    assert notes.track_key("Autódromo José Carlos Pace - Grand Prix") == notes.track_key("Autodromo Jose Carlos Pace (Grand Prix)")
    a, b = notes.track_key("鈴鹿サーキット"), notes.track_key("富士スピードウェイ")
    assert a != b and a.startswith("track-") and notes._path(TMP, a)
    assert notes.track_key("Charlotte Motor Speedway (Roval 2019)") == notes.track_key("Charlotte Motor Speedway - Roval 2025")
    # a card saved by 2.2.1 under the old key still opens, and moves to the new key on the next save
    lib = TMP / "cards-legacy"
    old = notes._legacy_key("Autódromo José Carlos Pace - Grand Prix")
    (lib / "track-notes").mkdir(parents=True)
    (lib / "track-notes" / f"{old}.json").write_text(json.dumps(
        {"schema": 1, "track": "Autódromo José Carlos Pace - Grand Prix", "fields": {"lap1": "Old note"},
         "updated": "2026-01-01T00:00:00Z"}), encoding="utf-8")
    c = notes.load(lib, "Autódromo José Carlos Pace - Grand Prix")   # the schedule name it was saved under
    assert c["fields"]["lap1"] == "Old note"
    notes.save(lib, notes.clean({"track": "Autódromo José Carlos Pace - Grand Prix", "fields": {"lap1": "New"}}))
    assert not (lib / "track-notes" / f"{old}.json").exists()
    assert notes.load(lib, "Autodromo Jose Carlos Pace (Grand Prix)")["fields"]["lap1"] == "New"


def test_track_card_save_refuses_a_stale_window():
    from debrief import notes
    lib = TMP / "cards-conflict"
    first = notes.save(lib, notes.clean({"track": "Lime Rock Park - Classic", "fields": {"lap1": "A"}}))
    time.sleep(1.1)
    # window 1 saves on top of what it opened
    notes.save(lib, notes.clean({"track": "Lime Rock Park - Classic", "fields": {"lap1": "B"},
                                 "base_updated": first["updated"]}))
    # window 2 still holds the first version: refused, nothing overwritten
    try:
        notes.save(lib, notes.clean({"track": "Lime Rock Park - Classic", "fields": {"lap1": "C"},
                                     "base_updated": first["updated"]}))
        raise AssertionError("stale save accepted")
    except notes.Conflict as e:
        assert "changed" in str(e)
    assert notes.load(lib, "Lime Rock Park - Classic")["fields"]["lap1"] == "B"
    # no base sent (an older window): saves as before
    notes.save(lib, notes.clean({"track": "Lime Rock Park - Classic", "fields": {"lap1": "D"}}))
    assert notes.load(lib, "Lime Rock Park - Classic")["fields"]["lap1"] == "D"


# ================================================================ sync (C1, C2, H5, M1, M2, M3, M8, L1)
class _Srv:
    def __init__(self, mut=None):
        self.fake = TS.FakeG61()
        if mut:
            mut(self.fake)
        self.srv, base = TS.serve(self.fake)
        self.tmp = tempfile.mkdtemp(prefix="debrief-fixb-sync-")
        self.api = TS.client(base, self.tmp)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.srv.shutdown()


def test_corrupt_laps_file_is_set_aside_and_downloaded_again():
    from debrief import sync
    with _Srv() as s:
        sync.run(s.api, telemetry="none", now=TS.NOW, **QUIET)
        store = sync.Store.for_api(s.api)
        f = store._combo_file(10, 20)
        f.write_text(f.read_text()[:1000])   # truncated by a power cut
        s.fake.add("NEW1", 10, 20, "EVNEW", 1, TS.NOW + timedelta(hours=1), 91.0)
        r = sync.run(s.api, telemetry="none", now=TS.NOW + timedelta(hours=2), **QUIET)
        assert len(store.laps(10, 20)) == 2301, len(store.laps(10, 20))
        assert list(store.laps_dir.glob("10_20.corrupt-*.json")), "damaged file not kept"
        assert r["warnings"] and r["laps"] == s.fake.n + 1, r
        # an empty file (another power-cut shape) and a damaged state file
        f.write_text("")
        (store.root / "state.json").write_text("{not json")
        r = sync.run(s.api, telemetry="none", now=TS.NOW + timedelta(hours=3), **QUIET)
        assert len(store.laps(10, 20)) == 2301 and list(store.root.glob("state.corrupt-*.json"))


def test_writes_are_flushed_to_disk_before_the_swap():
    from debrief import sync
    synced = []
    real = os.fsync
    os.fsync = lambda fd: synced.append(fd) or real(fd)
    try:
        p = TMP / "durable" / "x.json"
        sync._write_json(p, {"a": 1})
        assert synced and json.loads(p.read_text()) == {"a": 1}
        assert not [x for x in p.parent.iterdir() if x.name != "x.json"], "temp file left behind"
    finally:
        os.fsync = real


def test_laps_driven_between_a_paused_first_sync_and_its_resume_are_kept():
    from debrief import sync
    with _Srv() as s:
        sync.run(s.api, telemetry="none", now=TS.NOW, should_stop=lambda: s.api.requests_made >= 4, **QUIET)
        st = sync.Store.for_api(s.api).state()
        assert not st.get("first_complete") and st["combos"]["11:21"]["done"]
        s.fake.add("GAP1", 11, 21, "EVGAP", 3, TS.NOW + timedelta(days=1), 95.0)
        sync.run(s.api, telemetry="none", now=TS.NOW + timedelta(days=5), **QUIET)
        sync.run(s.api, telemetry="none", now=TS.NOW + timedelta(days=6), **QUIET)
        assert "GAP1" in sync.Store.for_api(s.api).laps(11, 21)


def test_pages_are_checked_against_total_and_repeats():
    from debrief import sync

    def cap(fake):   # a server whose page limit is smaller than ours
        orig = fake.search
        fake.search = lambda q: (lambda r: {"items": r["items"][:500], "total": r["total"]})(orig(q))
    with _Srv(cap) as s:
        sync.run(s.api, telemetry="none", now=TS.NOW, **QUIET)
        assert len(sync.Store.for_api(s.api).laps(10, 20)) == 2300

    def nooff(fake):   # a server that ignores offset
        orig = fake.search
        fake.search = lambda q: orig({k: v for k, v in q.items() if k != "offset"})
    with _Srv(nooff) as s:
        r = sync.run(s.api, telemetry="none", now=TS.NOW, **QUIET)
        assert sum(1 for p, _ in s.fake.log if p == "laps") <= 4
        st = sync.Store.for_api(s.api).state()["combos"]["10:20"]
        assert st.get("incomplete") and not st["done"] and "synced_until" not in st, st   # retried next sync
        assert any("listed 2300" in w for w in r["warnings"])


def test_one_failing_telemetry_lap_doesnt_stop_the_sync():
    from debrief import sync
    from debrief.g61 import Garage61Error
    with _Srv() as s:
        sync.run(s.api, telemetry="none", now=TS.NOW, **QUIET)
        bad = sync.Store.for_api(s.api).sessions()[0]["best_lap_id"]
        tries = {"n": 0}
        real = s.api._get

        def get(path, *a, **k):
            if path == f"laps/{bad}/csv":
                tries["n"] += 1
                raise Garage61Error("Garage 61 kept refusing requests; try again later.", status=500)
            return real(path, *a, **k)
        s.api._get = get
        for i in range(4):
            r = sync.run(s.api, telemetry="best", now=TS.NOW + timedelta(hours=i + 1), **QUIET)
            assert not r["stopped"] and r["synced"]
        st = sync.Store.for_api(s.api).state()
        assert tries["n"] == sync.TEL_TRIES and bad in st["tel_none"], (tries, st["tel_none"])
        assert st["telemetry_left"] == 0
        # the other 24 sessions got their telemetry on the first run
        assert sum(1 for p, _ in s.fake.log if p.endswith("/csv")) == 24

        def reject(path, *a, **k):   # a rejected token is still fatal
            raise Garage61Error("Garage 61 rejected the token (401).", status=401)
        st["tel_none"] = {}
        st["tel_done"] = []
        sync.Store.for_api(s.api).save_state(st)
        for f in s.api.cache.glob("*.csv"):
            f.unlink()
        s.api._get = lambda path, *a, **k: reject(path) if path.endswith("/csv") else real(path, *a, **k)
        try:
            sync.run(s.api, telemetry="best", now=TS.NOW + timedelta(hours=9), **QUIET)
            raise AssertionError("401 swallowed")
        except Garage61Error as e:
            assert e.status == 401


def test_refused_telemetry_is_asked_again_after_30_days():
    from debrief import sync
    with _Srv() as s:
        sync.run(s.api, telemetry="none", now=TS.NOW, **QUIET)
        lid = sync.Store.for_api(s.api).sessions()[0]["best_lap_id"]
        s.fake.forbid_csv.add(lid)
        sync.run(s.api, now=TS.NOW + timedelta(hours=1), **QUIET)
        st = sync.Store.for_api(s.api).state()
        assert lid in st["tel_none"]
        s.fake.forbid_csv.clear()
        s.fake.log.clear()
        sync.run(s.api, now=TS.NOW + timedelta(days=2), **QUIET)
        assert f"laps/{lid}/csv" not in [p for p, _ in s.fake.log]
        for f in s.api.cache.glob("*.missing"):
            f.unlink()
        s.fake.log.clear()
        sync.run(s.api, now=TS.NOW + timedelta(days=31), **QUIET)
        assert f"laps/{lid}/csv" in [p for p, _ in s.fake.log]
        # a 2.2.1 state file (a plain list) still loads
        store = sync.Store.for_api(s.api)
        st = store.state()
        st["tel_none"] = ["X1"]
        store.save_state(st)
        assert "X1" in store.state()["tel_none"]


def test_weekly_deep_check_finds_laps_that_uploaded_late():
    from debrief import sync
    with _Srv() as s:
        sync.run(s.api, telemetry="none", now=TS.NOW, **QUIET)
        sync.run(s.api, telemetry="none", now=TS.NOW + timedelta(days=5), **QUIET)
        # a lap driven on day 1 that only reached Garage 61 on day 6: outside the 2-day window
        s.fake.add("LATE1", 11, 21, "EVLATE", 1, TS.NOW + timedelta(days=1), 99.0)
        sync.run(s.api, telemetry="none", now=TS.NOW + timedelta(days=6), **QUIET)
        store = sync.Store.for_api(s.api)
        assert "LATE1" not in store.laps(11, 21)
        sync.run(s.api, telemetry="none", now=TS.NOW + timedelta(days=8), **QUIET)   # a week after the last deep check
        assert "LATE1" in store.laps(11, 21)


def test_disk_use_is_bounded():
    from debrief import sync
    with _Srv() as s:
        for h in range(3):
            sync.run(s.api, telemetry="none", now=TS.NOW + timedelta(hours=h), **QUIET)
        assert not [p for p in s.api.cache.glob("*.json")
                    if "drivingStatistics" in p.read_text(encoding="utf-8", errors="ignore")]
        lap = next(iter(sync.Store.for_api(s.api).laps(10, 20).values()))
        assert set(lap) <= set(sync._LAP_KEYS) and "driver" not in lap
        # the telemetry cache keeps under its cap, least recently used first, never a kept lap
        for i in range(6):
            (s.api.cache / f"{i:020d}.csv").write_text("x" * 1000)
            os.utime(s.api.cache / f"{i:020d}.csv", (1000 + i, 1000 + i))
        gone = s.api.prune_csv(max_bytes=3000)
        left = sorted(p.name for p in s.api.cache.glob("*.csv"))
        assert gone == 3 and left == [f"{i:020d}.csv" for i in (3, 4, 5)], left


def test_pause_ends_a_long_backoff_quickly():
    from debrief.g61 import Garage61, Interrupted
    api = Garage61("tok-123456", TMP / "c-pause", min_gap_s=0, base_url="http://127.0.0.1:9/api/v1/", log=lambda m: None)
    flag = threading.Event()
    api.should_stop = flag.is_set
    threading.Timer(0.3, flag.set).start()
    t0 = time.monotonic()
    try:
        api._sleep(60)
        raise AssertionError("not interrupted")
    except Interrupted:
        pass
    assert time.monotonic() - t0 < 2.0


def test_paused_during_backoff_counts_as_a_pause():
    from debrief import sync
    from debrief.g61 import Interrupted
    with _Srv() as s:
        real = s.api.laps_page

        def slow(**p):
            s.api._sleep(30)   # a Retry-After wait, cut short by the pause
            return real(**p)
        s.api.laps_page = slow
        flag = threading.Event()
        threading.Timer(0.3, flag.set).start()
        t0 = time.monotonic()
        r = sync.run(s.api, telemetry="none", now=TS.NOW, should_stop=flag.is_set, **QUIET)
        assert r["stopped"] and time.monotonic() - t0 < 5
        assert s.api.should_stop is None and Interrupted


# ================================================================ iRacing scaffold (L7)
def test_iracing_result_fields():
    from debrief import iracing
    res = {"event_strength_of_field": 2100, "session_results": [{"simsession_type_name": "Race", "results": [
        {"cust_id": 1, "car_class_id": 7, "finish_position": 0, "starting_position": 2,
         "finish_position_in_class": 0, "starting_position_in_class": 1, "laps_led": 5, "incidents": 2,
         "oldi_rating": 2000, "newi_rating": 2050, "old_sub_level": 399, "new_sub_level": 410},
        {"cust_id": 2, "car_class_id": 8, "finish_position": 1, "starting_position": 0},
        {"cust_id": 3, "car_class_id": 7, "finish_position": 2, "starting_position": 1},
        {"team_id": -9, "car_class_id": 8, "finish_position": 3, "starting_position": 3,
         "finish_position_in_class": 1, "starting_position_in_class": 1, "laps_led": 0,
         "driver_results": [{"cust_id": 4, "incidents": 6, "oldi_rating": 1500, "newi_rating": 1490,
                             "old_sub_level": 250, "new_sub_level": 240, "laps_led": 1}]}]}]}
    me = iracing.to_result_fields(res, 1)
    assert me["laps_led"] == 5 and me["field_size"] == 2 and me["finish_pos"] == 1 and me["start_pos"] == 2
    assert me["sr_before"] == 3.99
    team = iracing.to_result_fields(res, 4)
    assert team and team["incidents"] == 6 and team["irating_after"] == 1490 and team["laps_led"] == 1
    assert team["field_size"] == 2 and team["finish_pos"] == 2
    assert iracing.to_result_fields(res, 99) is None


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for n, f in tests:
        t0 = time.time()
        try:
            f()
            print(f"ok    {n} ({time.time() - t0:.1f}s)")
        except Exception as e:  # noqa: BLE001
            failed += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL  {n}: {e}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
