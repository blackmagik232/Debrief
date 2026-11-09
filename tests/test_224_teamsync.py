"""Debrief 2.24: Refresh team data syncs the teams' laps into a team library.

  - two teams with arbitrary names; members with practice and race laps in different cars
  - first sync: statistics + laps per member, telemetry for each best lap, a 403 telemetry lap says why
  - a repeat sync with no new driving asks only for the statistics; new driving fetches only that member and track
  - 429s stop the sync ("Synced 0 of N tracks; Refresh again") and the next one resumes
  - member pages, the header lines and the reference picker read the library

    python3 tests/test_224_teamsync.py

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

from debrief import members, sync, teamref, teamsync  # noqa: E402

FAILS = []
NOW = datetime.now(timezone.utc)


def day(n):
    return (NOW - timedelta(days=n)).strftime("%Y-%m-%dT18:00:00Z")


def lap(lid, slug, first, track, car, t, ago, typ=1, clean=True, ev=None):
    return {"id": lid, "driver": {"slug": slug, "firstName": first, "lastName": "Tester"}, "track": {"id": track, "name": f"Track{track}"},
            "car": {"id": car, "name": f"Car{car}"}, "lapTime": t, "startTime": day(ago), "sessionType": typ,
            "clean": clean, "event": ev}


NAMES = {"m1": "Mia", "m2": "Noah", "m3": "Zed", "me-slug": "Owner"}


class Api:
    def __init__(self):
        self.cache = Path(tempfile.mkdtemp()) / "acct"
        self.requests_made = 0
        self.rate_limited = 0
        self.min_gap = 1.0
        self.base_gap = 1.0
        self.laps, self.pages, self.stat_calls, self.csv_calls = [], [], [], []
        self.fail429 = 0
        self.no_tel = set()

    def _req(self):
        self.requests_made += 1

    def me(self):
        return {"slug": "me-slug", "firstName": "Owner"}

    def teams(self, refresh=False):
        return [{"slug": "alpha-x", "name": "Quick Foxes"}, {"slug": "beta-y", "name": "Slow Owls"}]

    def tracks(self):
        return [{"id": i, "name": f"Track{i}", "lap_length": 4000} for i in (1, 2, 3)]

    def cars(self):
        return [{"id": i, "name": f"Car{i}"} for i in (7, 8)]

    def team_statistics(self, slug, start):
        self._req()
        self.stat_calls.append((slug, start))
        out = {}
        for l in self.laps:
            if l["team"] != slug or l["startTime"][:10] < start[:10]:
                continue
            k = (l["driver"]["slug"], l["track"]["id"], l["car"]["id"], l["startTime"][:10])
            out[k] = out.get(k, 0) + 1
        return [{"driver": {"slug": s, "firstName": NAMES.get(s, s), "lastName": "Tester"}, "track": t, "car": c, "day": d,
                 "lapCount": n, "timeDriven": n * 90.0} for (s, t, c, d), n in out.items()]

    def laps_page(self, **kw):
        self._req()
        self.pages.append(kw)
        assert kw.get("tracks") and len(kw["tracks"]) <= 10 and kw["teams"] and "cars" not in kw
        # real Garage 61 answers 400 "Invalid drivers parameter" for a member slug: never send one
        assert "drivers" not in kw, kw
        if self.fail429 and len(self.pages) <= self.fail429:
            self.rate_limited += 2
        after = kw["after"][:10]
        rows = [l for l in self.laps if l["team"] == kw["teams"][0]
                and l["track"]["id"] in kw["tracks"] and l["startTime"][:10] >= after]
        return [{k: v for k, v in l.items() if k != "team"} for l in rows], len(rows)

    def has_csv(self, i):
        return i in getattr(self, "have", set())

    def lap_csv(self, i):
        self._req()
        self.csv_calls.append(i)
        if i in self.no_tel:
            return None
        self.have = getattr(self, "have", set()) | {i}
        return "t,v\n"

    def add(self, team, *a, **k):
        l = lap(*a, **k)
        l["team"] = team
        self.laps.append(l)


def world():
    a = Api()
    n = [0]

    def add(team, slug, first, track, car, t, ago, **k):
        n[0] += 1
        a.add(team, f"L{n[0]}", slug, first, track, car, t, ago, **k)
    for i in range(10):   # m1: practice in car 7 at track 1, a race in car 8 at track 2
        add("alpha-x", "m1", "Mia", 1, 7, 90.0 + i * 0.1, 3)
    add("alpha-x", "m1", "Mia", 1, 7, 85.0, 3, clean=False)
    add("alpha-x", "m1", "Mia", 2, 8, 100.0, 5, typ=3, ev="E1")
    add("alpha-x", "m2", "Noah", 1, 8, 91.0, 2)
    add("alpha-x", "me-slug", "Owner", 1, 7, 92.0, 2)      # the owner's own laps are not synced here
    add("beta-y", "m3", "Zed", 3, 7, 70.0, 1)
    return a, add


def check(name, fn):
    try:
        fn()
        print("ok  ", name)
    except Exception:  # noqa: BLE001
        FAILS.append(name)
        print("FAIL", name)
        traceback.print_exc()


def test_first_then_repeat_then_new():
    a, add = world()
    a.no_tel = {"L12"}   # m1's race lap: Garage 61 won't share it
    r = teamsync.run(a, log=lambda s: None, now=NOW)
    assert r["total"] == 4 and r["done"] == 4 and not r["limited"], r
    assert a.min_gap == 1.0 and a.base_gap == 1.0                       # pacing restored
    lap_pages = len(a.pages)
    assert lap_pages == 3, lap_pages                                    # m1 (2 tracks, one request), m2, m3
    assert all(p["unclean"] is True and p["teams"] and "drivers" not in p for p in a.pages)
    assert len(a.stat_calls) == 2
    root = teamsync.root_for(a)
    ms = teamsync.member_sessions(root, "Mia Tester")
    assert ms["found"] and ms["laps"] == 12 and len(ms["sessions"]) == 2, ms
    prac = next(s for s in ms["sessions"] if s["type"] == "Practice")
    assert prac["laps"] == 11 and prac["unclean"] == 1 and abs(prac["best"] - 90.0) < 1e-6, prac
    assert "steady" in ms["consistency"].lower(), ms["consistency"]
    assert not teamsync.member_sessions(root, "Owner Tester")["found"]
    lines = teamsync.member_lines(root)
    assert any(l.startswith("Mia Tester: 2 tracks, 12 laps, last synced") for l in lines), lines
    assert len(lines) == 3, lines
    # telemetry: best lap per member/track/car; the refused one says why
    assert "L12" in a.csv_calls and len(a.csv_calls) == 4, a.csv_calls
    got = teamsync.laps_for(root, "Car8", "Track2", lambda c, t: c == "Car8" and t == "Track2")
    assert len(got) == 1 and not got[0]["usable"] and "hasn't shared telemetry" in got[0]["note"], got
    ok = teamsync.laps_for(root, "Car7", "Track1", lambda c, t: c == "Car7" and t == "Track1")
    assert ok and ok[0]["usable"] and ok[0]["driver"] == "Mia Tester", ok
    on = teamref.laps_on_hand(None, [{"name": "Mia Tester", "t": 90.0}], None, ok)
    assert on[0]["kind"] == "synced" and on[0]["usable"] and not any(x["kind"] == "garage61" for x in on), on
    # repeat: nothing driven since, only the statistics are asked for
    n0, p0, c0 = a.requests_made, len(a.pages), len(a.csv_calls)
    r2 = teamsync.run(a, log=lambda s: None, now=NOW + timedelta(minutes=30))
    assert len(a.pages) == p0 and len(a.csv_calls) == c0 and r2["new_laps"] == 0, (len(a.pages), r2)
    assert a.requests_made - n0 == 2, a.requests_made - n0
    # new driving: only that member and track
    add("alpha-x", "m2", "Noah", 1, 8, 90.5, 0)
    r3 = teamsync.run(a, log=lambda s: None, now=NOW + timedelta(hours=1))
    assert r3["new_laps"] == 1 and len(a.pages) == p0 + 1, (r3, len(a.pages) - p0)
    last = a.pages[-1]
    assert last["tracks"] == [1] and last["after"][:10] >= day(3)[:10], last


def test_429_resume():
    a, _ = world()
    a.fail429 = 1
    r = teamsync.run(a, log=lambda s: None, now=NOW)
    assert r["limited"] and r["done"] < r["total"] and "Refresh again to continue" in r["message"], r
    assert r["message"].startswith(f"Synced {r['done']} of {r['total']} tracks"), r["message"]
    a.fail429 = 0
    n = len(a.pages)
    r2 = teamsync.run(a, log=lambda s: None, now=NOW + timedelta(minutes=5))
    assert not r2["limited"] and r2["done"] == 2 and r2["total"] == 2, (r, r2)
    assert len(a.pages) - n == 2
    assert len(teamsync.member_lines(teamsync.root_for(a))) == 3


def test_member_page_and_gaps():
    a, _ = world()
    teamsync.run(a, log=lambda s: None, now=NOW)
    root = teamsync.root_for(a)
    data = {"me": {"name": "Owner"}, "members": [{"name": "Mia Tester", "key": "m1"}], "combos": []}
    v = members.view(data, "Mia Tester", lib_root=root)
    assert v["library"]["found"] and "consistency" not in v["why"], v["why"]
    page = members.html(v)
    assert "Synced sessions" in page and "Practice" in page and "How steady" in page
    v2 = members.view(data, "Mia Tester", lib_root=teamsync.root_for(Api()))
    assert not v2["library"]["found"] and "Not synced yet" in v2["library"]["why"]
    assert "Not synced yet" in members.html(v2)


def test_no_teams_and_refusal():
    a = Api()
    a.teams = lambda refresh=False: []
    assert "not in a Garage 61 team" in teamsync.run(a, log=lambda s: None)["message"]
    b, _ = world()

    def boom(slug, start):
        raise RuntimeError("no")
    b.team_statistics = boom
    r = teamsync.run(b, log=lambda s: None, now=NOW)
    assert r["notes"] and "wouldn't share" in r["notes"][0], r


def test_settings_summary_hide_and_here():
    a, _ = world()
    lib = Path(tempfile.mkdtemp())
    teamsync.run(a, log=lambda s: None, now=NOW, library_folder=lib)
    root = teamsync.root_from_library(lib)
    sm = teamsync.summary(root, lib, "Track1", lambda x, y: x == y)
    assert {t["name"] for t in sm["teams"]} == {"Quick Foxes", "Slow Owls"} and sm["teams"][0]["last_synced_text"] != "not synced yet", sm
    assert "Mia Tester (Car7" in sm["here"] and any(p["name"] == "Noah Tester" for p in sm["people"]), sm
    assert "None of your teammates" in teamsync.summary(root, lib, "Nowhere", lambda x, y: x == y)["here"]
    assert "hasn't been synced" in teamsync.summary(None, lib, "Track1")["here"]
    teamsync.set_hidden(lib, "beta-y", True)
    b, _ = world()
    r = teamsync.run(b, log=lambda s: None, now=NOW, library_folder=lib)
    assert [t["slug"] for t in r["teams"]] == ["alpha-x"] and any("hidden" in n for n in r["notes"]), r


for n, f in list(globals().items()):
    if n.startswith("test_"):
        check(n, f)
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("all ok")
