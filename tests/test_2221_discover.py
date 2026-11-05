"""Debrief 2.22.1: teammates who drive other tracks or cars than you are found.

Garage 61's /laps refuses a request with no `tracks` (exact error text from the owner's log), may then ask for `cars`,
and may refuse a long list. Discovery sends every track id, adds cars if asked, and splits into chunks if refused.

    python3 tests/test_2221_discover.py
"""
from __future__ import annotations

import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.modules.setdefault("irsdk", None)

from debrief import team  # noqa: E402
from debrief.g61 import Garage61Error  # noqa: E402

FAILS = []
NO_TRACKS = ('{"error_message":"operation FindLaps: decode params: query: \\"tracks\\": query parameter \\"tracks\\" not set"}')
NO_CARS = ('{"error_message":"operation FindLaps: decode params: query: \\"cars\\": query parameter \\"cars\\" not set"}')


def check(name, fn):
    try:
        fn()
        print("ok  ", name)
    except Exception:  # noqa: BLE001
        FAILS.append(name)
        print("FAIL", name)
        traceback.print_exc()


def row(i, slug, name, track, car):
    f, _, l = name.partition(" ")
    return {"id": f"{slug}-{track}-{car}-{i}", "driver": {"slug": slug, "firstName": f, "lastName": l}, "lapTime": 100.0 + i,
            "startTime": "2026-11-01T18:00:00Z", "track": {"id": track, "name": f"Track {track}"},
            "car": {"id": car, "name": f"Car {car}"}}


ROWS = [row(1, "josh", "Josh Spence", 11, 5), row(2, "josh", "Josh Spence", 12, 6),   # combos the owner never drove
        row(3, "swazi", "Swazi Williams", 1, 2), row(4, "kenta", "Kenta Young", 1, 2)]


class FakeApi:
    requests_made = 0

    def __init__(self, need_cars=False, max_tracks=None, no_age_rows=False):
        self.need_cars, self.max_tracks, self.no_age_rows = need_cars, max_tracks, no_age_rows
        self.sent = []

    def tracks(self, refresh=False):
        return [{"id": i, "name": f"Track {i}"} for i in range(1, 101)]

    def cars(self, refresh=False):
        return [{"id": i, "name": f"Car {i}"} for i in range(1, 11)]

    def laps_page(self, **kw):
        self.sent.append(kw)
        if not kw.get("tracks"):
            raise Garage61Error("Garage 61 said no (400)", status=400, detail=NO_TRACKS)
        if self.need_cars and not kw.get("cars"):
            raise Garage61Error("Garage 61 said no (400)", status=400, detail=NO_CARS)
        if self.max_tracks and len(kw["tracks"]) > self.max_tracks:
            raise Garage61Error("Garage 61 said no (400)", status=400, detail='{"error_message":"query too long"}')
        if self.no_age_rows and "age" in kw:
            return [], 0
        out = [r for r in ROWS if r["track"]["id"] in kw["tracks"]]
        return out, len(out)


def run(api, members=("Josh Spence", "Kenta Young")):
    logs = []
    found, note = team.discover_combos(api, "midfield-menace", list(members), "swazi", "Swazi Williams", logs.append)
    return found, note, logs


def keys(found):
    return {(c["track_id"], c["car_id"]): c for c in found}


def test_all_tracks_sent_once():
    api = FakeApi()
    found, note, logs = run(api)
    assert len(api.sent) == 1 and len(api.sent[0]["tracks"]) == 100 and "cars" not in api.sent[0], api.sent
    k = keys(found)
    assert set(k) == {(11, 5), (12, 6), (1, 2)}, k                       # Josh's combos the owner never drove
    assert k[(11, 5)]["who"] == ["Josh Spence"] and k[(1, 2)]["who"] == ["Kenta Young"], k
    assert all(c["source"] == "garage61" for c in found) and "Found 3" in note
    assert any("asking for 100 tracks, 0 cars" in l for l in logs) and not any("[1, 2, 3" in l for l in logs), logs


def test_cars_retry():
    api = FakeApi(need_cars=True)
    found, note, logs = run(api)
    assert len(api.sent) == 2 and "cars" not in api.sent[0] and len(api.sent[1]["cars"]) == 10, api.sent
    assert (11, 5) in keys(found) and any("adding all car ids" in l for l in logs), logs


def test_chunking():
    api = FakeApi(max_tracks=40)
    found, note, logs = run(api)
    # 1 refused + 3 chunks (100 tracks / 40)
    assert len(api.sent) == 4 and [len(s["tracks"]) for s in api.sent] == [100, 40, 40, 20], [len(s["tracks"]) for s in api.sent]
    assert set(keys(found)) == {(11, 5), (12, 6), (1, 2)}
    assert any("splitting it into chunks of 40" in l for l in logs), logs


def test_retry_without_age():
    api = FakeApi(no_age_rows=True)
    found, note, logs = run(api)
    assert len(api.sent) == 2 and api.sent[0]["age"] == -2 and "age" not in api.sent[1], api.sent
    assert (11, 5) in keys(found)


def test_failure_never_raises():
    class Bad(FakeApi):
        def laps_page(self, **kw):
            raise Garage61Error("no", status=500, detail="boom")
    found, note, logs = run(Bad())
    assert found == [] and note.startswith("Garage 61 couldn't list") and any("discovery failed: 500" in l for l in logs), logs


def test_zero_rows_message():
    logs = []
    mates = {}
    first = [row(3, "swazi", "Swazi Williams", 1, 2)]
    team._DISCOVERED.clear()
    team._DISCOVERED.update({"josh spence", "joshspence", "josh"})
    team.gather_mates(first, None, "swazi", 100.0, members=["Josh Spence", "Nobody Here"], my_name="Swazi Williams",
                      log=logs.append)
    joined = "\n".join(logs)
    assert "Josh Spence: 0 laps at this track and car" in joined, joined
    assert "Nobody Here: 0 returned (name not matched to any Garage 61 driver)" in joined, joined
    assert "no driver in this group matched" not in joined
    team._DISCOVERED.clear()


def test_members_sees_discovered_tracks():
    from debrief import members
    found, _, _ = run(FakeApi())
    allc = team.merge_combos([], found)
    unl = [dict(c, loaded=False) for c in allc]
    data = {"me": {"name": "Swazi Williams", "key": "me"},
            "members": [{"name": "Josh Spence", "key": "josh", "is_me": False}, {"name": "Swazi Williams", "key": "me", "is_me": True}],
            "combos": unl}
    v = members.view(data, "josh spence")
    labels = {u["track"] for u in v["unloaded"]}
    assert labels == {"Track 11", "Track 12"}, v["unloaded"]


for n, f in list(globals().items()):
    if n.startswith("test_") and callable(f):
        check(n, f)

if FAILS:
    print("FAILED:", ", ".join(FAILS))
    sys.exit(1)
print("all passed")
