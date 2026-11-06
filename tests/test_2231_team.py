"""Debrief 2.23.1: teammates show up even when they drove a different car in the same race.

  - teammate searches are by track, with no car filter; Josh (other GT3 car) appears with his car
  - tonight's race (a synced session with no review) is on the page, newest first, one row per track
  - discovery: tracks in chunks of at most 10, at most 4 requests, adapts to a lower limit
  - after two 429s the rest is left to open on demand and the page says so
  - honest wording: "No laps found for X at the N tracks searched"

    python3 tests/test_2231_team.py

No network: a stand-in Garage 61 only.
"""
from __future__ import annotations

import sys
import tempfile
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.modules.setdefault("irsdk", None)

from debrief import brand, sync, team  # noqa: E402
from debrief.g61 import Garage61Error  # noqa: E402

FAILS = []
SPA, ZOL = 11, 12
CORVETTE, BMW, MX5, LAMBO, GT4 = 100, 101, 102, 103, 104
NOW = datetime.now(timezone.utc)
CFG = {"team_name": "Midfield Menace", "team_slug": "overdriveracademy", "team_members": list(brand.MEMBERS)}


def lap(slug, first, last, t, track, car, tname, cname, day):
    return {"id": f"{slug}-{track}-{car}-{t}", "driver": {"slug": slug, "firstName": first, "lastName": last},
            "lapTime": t, "startTime": day, "driverRating": 2500,
            "track": {"id": track, "name": tname}, "car": {"id": car, "name": cname}}


DAY = NOW.strftime("%Y-%m-%dT18:00:00Z")
ROWS = [lap("swazi", "Swazi", "Williams", 140.5, SPA, CORVETTE, "Spa", "Chevrolet Corvette Z06 GT3.R", DAY),
        lap("josh", "Josh", "Spence", 139.8, SPA, BMW, "Spa", "BMW M4 GT3", DAY),
        lap("josh", "Josh", "Spence", 120.0, SPA, MX5, "Spa", "Global Mazda MX-5 Cup", DAY)]


class Api:
    requests_made = 0
    rate_limited = 0

    def __init__(self, rows=ROWS, limit=10, fail429=0, req_tracks=True, my_car=(CORVETTE, "Chevrolet Corvette Z06 GT3.R"),
                 roster=False):
        self.roster = roster
        self.rows, self.limit, self.fail429 = rows, limit, fail429
        self.cache = Path(tempfile.mkdtemp())
        self.calls, self.pages = [], []
        store = sync.Store.for_api(self)
        sync._write_json(store.root / "sessions.json", {"schema": sync.SCHEMA, "sessions": [
            {"key": "a", "event": "EV1", "track_id": SPA, "car_id": my_car[0], "track": "Spa", "car": my_car[1],
             "start": NOW.strftime("%Y-%m-%dT%H:%M:%SZ")},
            {"key": "b", "track_id": ZOL, "car_id": 7, "track": "Zolder", "car": "McLaren 570S GT4",
             "start": (NOW - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")},
            {"key": "c", "track_id": 99, "car_id": 7, "track": "Old Track", "car": "McLaren 570S GT4",
             "start": (NOW - timedelta(days=200)).strftime("%Y-%m-%dT%H:%M:%SZ")}]})

    def me(self):
        return {"slug": "swazi", "firstName": "Swazi", "lastName": "Williams"}

    def teams(self, refresh=False):
        t = {"slug": "overdriveracademy", "name": "Overdriver Academy"}
        if self.roster:
            t["members"] = [{"slug": "josh", "firstName": "Josh", "lastName": "Spence"},
                            {"slug": "swazi", "firstName": "Swazi", "lastName": "Williams"}]
        return [t]

    def tracks(self):
        return [{"id": SPA, "name": "Spa"}, {"id": ZOL, "name": "Zolder"}]

    def cars(self):
        return [{"id": CORVETTE, "name": "Corvette", "category": "GT3"}, {"id": BMW, "name": "BMW", "category": "GT3"},
                {"id": MX5, "name": "MX5", "category": "Cup"},
                {"id": LAMBO, "name": "Lamborghini Huracan GT3 EVO", "category": "GT3"}, {"id": GT4, "name": "Mustang GT4", "category": "GT4"}]

    def cached_items(self, k):
        return []

    def _check(self, kw):
        assert kw.get("tracks"), "tracks is required"
        if len(kw["tracks"]) > self.limit:
            raise Garage61Error("bad", status=400, detail='{"message":"Cannot specify more than %d tracks"}' % self.limit)

    def find_laps(self, **kw):
        self.calls.append(kw)
        self._check(kw)
        assert kw.get("drivers") == ["me"] or "cars" not in kw, "teammates must not be filtered by the owner's car"
        rows = [l for l in self.rows if l["track"]["id"] in kw["tracks"]]
        if kw.get("drivers") == ["me"]:
            return [l for l in rows if l["driver"]["slug"] == "swazi"]
        rows = [l for l in rows if l["driver"]["slug"] != "swazi"]
        if kw.get("group") == "driver":   # best lap per driver, any car
            best = {}
            for l in rows:
                if l["driver"]["slug"] not in best or l["lapTime"] < best[l["driver"]["slug"]]["lapTime"]:
                    best[l["driver"]["slug"]] = l
            return list(best.values())
        return rows

    def laps_page(self, **kw):
        self.pages.append(kw)
        self._check(kw)
        if self.fail429 and len(self.pages) <= self.fail429:
            self.rate_limited += 2
        rows = [l for l in self.rows if l["track"]["id"] in kw["tracks"]]
        return rows, len(rows)

    stats = None
    stats_error = None

    def team_statistics(self, slug, start):
        if self.stats_error:
            raise self.stats_error
        return list(self.stats or [])

    def has_csv(self, i):
        return False

    def lap_csv(self, i):
        return None

    def _cached(self, key, fn, max_age_s=None):
        return fn()


def build(api, logs=None):
    team.library_combos = lambda f: []
    logs = logs if logs is not None else []
    return team.build(api, Path(tempfile.mkdtemp()), "overdriveracademy", log=logs.append, cfg=CFG), logs


def check(name, fn):
    try:
        fn()
        print("ok  ", name)
    except Exception:  # noqa: BLE001
        FAILS.append(name)
        print("FAIL", name)
        traceback.print_exc()


def test_josh_with_his_car_and_newest_first():
    d, logs = build(Api())
    first = d["combos"][0]
    assert first["track_short"].startswith("Spa") and first["car"].startswith("Chevrolet"), first["track"]
    josh = next(r for r in first["board"] if r["name"] == "Josh Spence")
    assert josh["car"] == "BMW M4 GT3" and josh["diff_car"] is True and abs(josh["t"] - 139.8) < 1e-6, josh
    cup = [r for r in first["board"] if r.get("car") == "Global Mazda MX-5 Cup"]
    assert cup and cup[0]["other_class"] and cup[0]["gap"] == 0                        # other class: own group, no gap to you
    assert len({c["track"] for c in d["combos"]}) == len(d["combos"])                 # one row per track
    assert any("Garage 61 total for Josh Spence: 2 rows" in l for l in logs), logs
    assert not any("matching Garage 61 driver" in l for l in logs)


def test_discovery_chunks_and_cap():
    api = Api(limit=3)
    ids = list(range(1, 46))
    logs = []
    combos, note = team.discover_combos(api, "overdriveracademy", [], "swazi", "Swazi", logs.append, track_ids=ids)
    assert len(api.pages) <= team.DISCOVER_MAX_REQUESTS and all(len(p["tracks"]) <= 10 for p in api.pages), api.pages
    assert all(len(p["tracks"]) <= 3 for p in api.pages[1:]), [len(p["tracks"]) for p in api.pages]
    api2 = Api()
    team.discover_combos(api2, "overdriveracademy", [], "swazi", "Swazi", logs.append, track_ids=ids)
    assert len(api2.pages) == 4 and all(len(p["tracks"]) == 10 for p in api2.pages)


def _race(slug, first, last, t, track, car, cname, n):
    l = lap(slug, first, last, t, track, car, "Spa", cname, DAY)
    l.update(id=f"{slug}-race{n}-{t}", sessionType=3, session="Race")
    return l


def test_lambo_race_laps_in_other_class_appear():
    # two races today; the owner drove a GT4, Josh a Lamborghini GT3, race laps only
    rows = [_race("swazi", "Swazi", "Williams", 150.0, SPA, GT4, "Mustang GT4", 1),
            _race("josh", "Josh", "Spence", 139.4, SPA, LAMBO, "Lamborghini Huracan GT3 EVO", 1),
            _race("josh", "Josh", "Spence", 140.1, SPA, LAMBO, "Lamborghini Huracan GT3 EVO", 2),
            _race("kenta", "Kenta", "Young", 141.0, SPA, BMW, "BMW M4 GT3", 2)]
    d, logs = build(Api(rows=rows, my_car=(GT4, "Mustang GT4"), roster=True))
    c = d["combos"][0]
    josh = next(r for r in c["board"] if r["name"] == "Josh Spence")
    assert josh["car"].startswith("Lamborghini") and josh["other_class"] and abs(josh["t"] - 139.4) < 1e-6, josh
    assert "haven't driven this class" in josh["note"], josh
    assert any(r["name"] == "Kenta Young" for r in c["board"])
    from debrief import members
    v = members.view(d, "Josh Spence")
    assert v["ok"] and "Lamborghini" in v["tracks"][0]["label"] and "haven't driven this class" in v["tracks"][0]["gap_text"], v


def test_teammate_seen_tracks_are_candidates():
    import json
    api = Api()
    sync._write_json(sync.Store.for_api(api).root / "sessions.json", {"schema": sync.SCHEMA, "sessions": []})
    folder = Path(tempfile.mkdtemp())
    (folder / "team").mkdir()
    (folder / "team" / "team.json").write_text(json.dumps({"combos": [{"track": "Roval", "car": "X", "track_id": 55,
                                                                      "last_date": "2026-11-01"}]}), encoding="utf-8")
    assert team.candidate_track_ids(api, folder) == [55]


def test_member_pass_respects_cap():
    api = Api(rows=[ROWS[0]], roster=True)
    team.discover_combos(api, "overdriveracademy", ["Josh Spence"], "swazi", "Swazi", lambda *_: None,
                         track_ids=list(range(1, 36)), member_slugs={"Josh Spence": "josh"})
    assert len(api.pages) <= team.DISCOVER_MAX_REQUESTS and len(api.pages) == 4, len(api.pages)
    assert api.pages[-1]["drivers"] == ["josh"] and len(api.pages[-1]["tracks"]) <= 10
    api2 = Api(rows=[ROWS[0]])
    team.discover_combos(api2, "overdriveracademy", ["Josh Spence"], "swazi", "Swazi", lambda *_: None,
                         track_ids=list(range(1, 80)), member_slugs={"Josh Spence": "josh", "Kenta Young": "kenta"})
    assert len(api2.pages) == 4 and api2.pages[-1]["drivers"] == ["josh"], [p.get("drivers") for p in api2.pages]


def test_same_race_by_event_and_weekly_group():
    rows = [_race("josh", "Josh", "Spence", 139.4, SPA, LAMBO, "Lamborghini Huracan GT3 EVO", 1),
            _race("swazi", "Swazi", "Williams", 150.0, SPA, GT4, "Mustang GT4", 1)]
    api = Api(rows=rows)
    d, logs = build(api)
    ev = [c for c in api.calls if c.get("event")]
    assert ev and ev[0]["event"] == "EV1" and ev[0]["unclean"] is True and "cars" not in ev[0], api.calls
    r = d["races"][0]
    assert r["title"].startswith("Tonight at") and any(v["name"] == "Josh Spence" and v["car"].startswith("Lamborghini")
                                                      for v in r["drivers"]), r
    assert d["weekly"]["team"]["slug"] == "overdriveracademy", d["weekly"]["team"]     # not the built-in default
    assert d["weekly"]["membership"] != "unknown", d["weekly"]["membership"]


def test_g61_sends_numeric_track_id():
    from debrief import g61
    from debrief.g61 import Garage61
    g61.no_network = lambda url="": False   # the suite runs offline; the request itself is faked below
    seen = []

    class R:
        status_code, headers, text = 200, {}, "{}"

        def json(self):
            return {"items": []}
    api = Garage61("tok", Path(tempfile.mkdtemp()), min_gap_s=0, backoff_base=0)
    api.s.get = lambda url, timeout=30: (seen.append(url), R())[1]
    api.find_laps(fresh=True, tracks=[{"id": 61, "name": "Circuit Zolder"}], event="E1", drivers=["following"])
    assert "tracks=61&" in seen[0] + "&" and "%7B" not in seen[0] and "Zolder" not in seen[0], seen


def _stat(slug, first, last, track, tname, car, cname, laps):
    return {"driver": {"slug": slug, "firstName": first, "lastName": last}, "track": {"id": track, "name": tname},
            "car": {"id": car, "name": cname}, "day": NOW.strftime("%Y-%m-%d"), "lapCount": laps, "time": laps * 140.0}


def test_team_statistics_find_joshs_lambo():
    rows = [_race("swazi", "Swazi", "Williams", 150.0, SPA, GT4, "Mustang GT4", 1),
            _race("josh", "Josh", "Spence", 139.4, SPA, LAMBO, "Lamborghini Huracan GT3 EVO", 1)]
    api = Api(rows=rows, my_car=(GT4, "Mustang GT4"))
    api.stats = [_stat("josh", "Josh", "Spence", SPA, "Spa", LAMBO, "Lamborghini Huracan GT3 EVO", 20),
                 _stat("josh", "Josh", "Spence", 21, "Road Atlanta", LAMBO, "Lamborghini Huracan GT3 EVO", 12),
                 _stat("josh", "Josh", "Spence", 22, "Zolder", BMW, "BMW M4 GT3", 8)]
    d, logs = build(api)
    tracks = {c["track"] for c in d["combos"]}
    assert {"Road Atlanta", "Zolder"} <= tracks, tracks                         # his other tracks are listed, to load on demand
    spa = next(c for c in d["combos"] if c["track"] == "Spa")
    assert any(r["name"] == "Josh Spence" and r["car"].startswith("Lamborghini") for r in spa["board"]), spa["board"]
    rec = next(r for r in d["member_stats"] if r["name"] == "Josh Spence")
    assert rec["tracks"] == 3 and rec["cars"] == 2 and rec["laps"] == 40, rec
    from debrief import members
    v = members.view(d, "Josh Spence")
    assert v["records"]["laps"] == 40 and "Road Atlanta" in members.html(v), v["records"]
    assert any("team statistics: 3 rows" in l for l in logs), logs


def test_team_statistics_refused_is_honest():
    api = Api()
    api.stats_error = Garage61Error("no", status=403, detail="forbidden")
    d, logs = build(api)
    assert any("team statistics refused: 403" in l for l in logs), logs
    assert any("wouldn't share the team's driving statistics" in n for n in d["notes"]), d["notes"]
    assert d["combos"] and d["member_stats"] == []


def test_candidates_newest_first_within_60_days():
    api = Api()
    assert team.candidate_track_ids(api, tempfile.mkdtemp()) == [SPA, ZOL]


def test_two_429_stops_loading():
    d, logs = build(Api(fail429=1))
    assert d["combos"][0]["loaded"] and d["combos"][0]["board"]          # tonight's race still loads
    assert all(not c["loaded"] for c in d["combos"][1:]) and len(d["combos"]) > 1
    assert team.SLOW_NOTE in d["notes"] and "slow down" in d["combos"][1]["note"]


def test_honest_missing_member():
    d, logs = build(Api(rows=[ROWS[0]]))
    joined = "\n".join(logs)
    assert "No laps found for" in joined and "tracks searched" in joined, joined
    assert "matching Garage 61 driver" not in joined


for n, f in list(globals().items()):
    if n.startswith("test_") and callable(f):
        check(n, f)

if FAILS:
    print("FAILED:", ", ".join(FAILS))
    sys.exit(1)
print("all passed")
