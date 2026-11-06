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
CORVETTE, BMW, MX5 = 100, 101, 102
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

    def __init__(self, rows=ROWS, limit=10, fail429=0, req_tracks=True):
        self.rows, self.limit, self.fail429 = rows, limit, fail429
        self.cache = Path(tempfile.mkdtemp())
        self.calls, self.pages = [], []
        store = sync.Store.for_api(self)
        sync._write_json(store.root / "sessions.json", {"schema": sync.SCHEMA, "sessions": [
            {"key": "a", "track_id": SPA, "car_id": CORVETTE, "track": "Spa", "car": "Chevrolet Corvette Z06 GT3.R",
             "start": NOW.strftime("%Y-%m-%dT%H:%M:%SZ")},
            {"key": "b", "track_id": ZOL, "car_id": 7, "track": "Zolder", "car": "McLaren 570S GT4",
             "start": (NOW - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")},
            {"key": "c", "track_id": 99, "car_id": 7, "track": "Old Track", "car": "McLaren 570S GT4",
             "start": (NOW - timedelta(days=200)).strftime("%Y-%m-%dT%H:%M:%SZ")}]})

    def me(self):
        return {"slug": "swazi", "firstName": "Swazi", "lastName": "Williams"}

    def teams(self, refresh=False):
        return [{"slug": "overdriveracademy", "name": "Overdriver Academy"}]

    def tracks(self):
        return [{"id": SPA, "name": "Spa"}, {"id": ZOL, "name": "Zolder"}]

    def cars(self):
        return [{"id": CORVETTE, "name": "Corvette", "category": "GT3"}, {"id": BMW, "name": "BMW", "category": "GT3"},
                {"id": MX5, "name": "MX5", "category": "Cup"}]

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
    assert not any(r.get("car") == "Global Mazda MX-5 Cup" for r in first["board"])   # other class left out
    assert len({c["track"] for c in d["combos"]}) == len(d["combos"])                 # one row per track
    assert any("Garage 61 total for Josh Spence: 3 rows" in l for l in logs), logs
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
