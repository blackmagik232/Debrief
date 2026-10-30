"""Debrief 2.5 workstream 25-A: weekly team leaderboards, season head-to-heads, who to learn from.

    python3 tests/test_25_a.py

Never contacts Garage 61: every test uses a recording fake client (FakeApi) that counts calls and has
no network code at all. File system only, temp folders only.
"""
from __future__ import annotations

import ast
import faulthandler
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "fixtures_a"))
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
TMP = Path(tempfile.mkdtemp(prefix="debrief-25a-"))
os.environ.setdefault("DEBRIEF_SCHEDULE_DIR", str(TMP / "schedules"))
os.environ.setdefault("DEBRIEF_CACHE_DIR", str(TMP / "cache"))
os.environ.setdefault("DEBRIEF_SETTINGS", str(TMP / "settings.toml"))
faulthandler.dump_traceback_later(300, exit=True)

from debrief import leaderboards, schedule, team  # noqa: E402
import synth  # noqa: E402

FIX = ROOT / "tests" / "fixtures_25_a"
FORBIDDEN = re.compile(r"\b(will gain|costing|because|you should|the reason)\b", re.I)
NOW = datetime(2026, 10, 30, 19, 0, 0, tzinfo=timezone.utc)      # Friday of week 7
WEEK1 = datetime(2026, 9, 16, tzinfo=timezone.utc).date()        # week 7 starts 2026-10-28

TRACKS = [{"id": 51, "name": "Okayama International Circuit", "variant": "Full Course"},
          {"id": 52, "name": "Okayama International Circuit", "variant": "Short"},
          {"id": 18, "name": "Road America", "variant": "Full Course"},
          {"id": 30, "name": "Lime Rock Park", "variant": "Classic"}]
CARS = [{"id": 67, "name": "Global Mazda MX-5 Cup"}, {"id": 170, "name": "Porsche 963 GTP"},
        {"id": 180, "name": "BMW M4 GT3 EVO"}, {"id": 181, "name": "BMW M4 GT3"},
        {"id": 190, "name": "Ferrari 296 GT3"}, {"id": 200, "name": "Ford Mustang GT4"}]
TRACK_NAMES = {51: "Okayama International Circuit - Full Course", 18: "Road America - Full Course",
               30: "Lime Rock Park - Classic"}


# ---------------------------------------------------------------- fakes
def lap(slug, t, day="2026-10-29T20:00:00Z", car=67, first=None, **flags):
    return {"id": f"{slug}-{t}-{car}", "lapTime": t, "clean": True, "startTime": day, "car": {"id": car},
            "driverRating": 2000, "driver": {"slug": slug, "firstName": first or slug.split("-")[0].title(),
                                             "lastName": "X"}, **flags}


def in_week(kw, hours=30):
    """A lap time stamp inside the week the search asked for."""
    a = datetime.fromisoformat(kw["after"].replace("Z", "+00:00")) + timedelta(hours=hours)
    return a.isoformat().replace("+00:00", "Z")


def default_laps(kw):
    d = in_week(kw)
    return [lap("jordan-x", 98.412, day=d), lap("alex", 98.871, day=d), lap("sam-y", 99.5, day=d)]


class FakeApi:
    """Records every call. requests_made counts what would have been a Garage 61 request.
    No network code: nothing here can reach the internet."""

    def __init__(self, laps_fn=None, me_saved=True):
        self.calls = []
        self.requests_made = 0
        self.laps_fn = laps_fn or default_laps
        self.me_saved = me_saved

    def peek(self, key):
        return {"slug": "alex", "firstName": "Alex", "lastName": "Driver"} if key == "me" and self.me_saved else None

    def me(self):
        self.calls.append(("me", {}))
        self.requests_made += 1
        return {"slug": "alex"}

    def cached_items(self, name):
        return {"tracks": TRACKS, "cars": CARS}.get(name, [])

    def find_laps(self, **kw):
        self.calls.append(("find_laps", kw))
        self.requests_made += 1
        return self.laps_fn(kw)

    def tracks(self, *a, **k):
        raise AssertionError("weekly refresh must never load the track list")

    def cars(self, *a, **k):
        raise AssertionError("weekly refresh must never load the car list")

    def laps(self):
        return [c for c in self.calls if c[0] == "find_laps"]


def make_series(name, cars, track_id=51, first_week=1, weeks=12, multiclass=None):
    sid = schedule.slug(name)
    classes = sorted({c for c in (schedule.class_of(x) for x in cars) if c})
    return {"id": sid, "name": name, "cars": cars, "classes": classes,
            "multiclass": len(classes) >= 2 if multiclass is None else multiclass,
            "weeks": [{"week": n, "start": (WEEK1 + timedelta(days=7 * (n - 1))).isoformat(),
                       "track": TRACK_NAMES[track_id], "race_time": None, "length": "15 laps", "conditions": None}
                      for n in range(first_week, weeks + 1)]}


def make_sched(series_list):
    return {"schema": 1, "season": "2026 Season 4", "year": 2026, "quarter": 4,
            "weeks": [{"week": n, "start": (WEEK1 + timedelta(days=7 * (n - 1))).isoformat()} for n in range(1, 13)],
            "series": series_list}


def mine(sched):
    return {"chosen": True, "series": [{"id": s["id"], "name": s["name"]} for s in sched["series"]]}


def lib():
    p = Path(tempfile.mkdtemp(dir=TMP))
    return {"library_folder": str(p), "team_slug": "midfield-menace"}


def at(dt):
    leaderboards._now = lambda: dt


MX5 = lambda **k: make_series("Global Mazda MX-5 Fanatec Cup", ["Global Mazda MX-5 Cup"], **k)


# ---------------------------------------------------------------- request budget
def test_one_series_one_request_with_the_spec_params():
    at(NOW)
    cfg, api = lib(), FakeApi()
    sched = make_sched([MX5(first_week=7)])
    res = leaderboards.refresh(api, cfg, sched, mine(sched), log=lambda m: None)
    assert len(api.calls) == 1 and api.requests_made == 1 and res["requests"] == 1, api.calls
    kw = api.calls[0][1]
    assert kw == {"tracks": [51], "cars": [67], "teams": ["midfield-menace"], "drivers": ["me"], "group": "driver",
                  "after": "2026-10-28T00:00:00Z", "age": 4, "limit": 200}, kw
    snap = leaderboards.load_snapshot(cfg["library_folder"], "2026-season-4", sched["series"][0]["id"], 7)
    assert snap and snap["requests"] == 1 and not snap["final"] and snap["end"] == "2026-11-04"
    assert [r["key"] for r in snap["board"]] == ["jordan-x", "me", "sam-y"]
    me = snap["board"][1]
    assert me["name"] == "Alex Driver" and me["is_me"] and me["gap"] == round(98.871 - 98.412, 3) and me["gap_pct"] > 0 and me["class"] == "MX-5"


def test_partial_best_costs_exactly_two_and_never_enters_the_board():
    at(NOW)

    def laps(kw):
        if kw["group"] == "none":
            return [lap("jordan-x", 11.02), lap("jordan-x", 98.6), lap("tyler-q", 99.9, pitIn=True),
                    lap("tyler-q", 99.1), lap("sam-y", 97.0, clean=False)]
        return [lap("jordan-x", 11.02), lap("alex", 98.871), lap("tyler-q", 6.0, incomplete=True),
                lap("sam-y", 97.0, clean=False), lap("sam-y", 99.4)]
    cfg, api = lib(), FakeApi(laps)
    sched = make_sched([MX5(first_week=7)])
    res = leaderboards.refresh(api, cfg, sched, mine(sched), log=lambda m: None)
    assert len(api.laps()) == 2 and res["requests"] == 2, api.calls
    assert api.calls[1][1]["group"] == "none" and api.calls[1][1]["limit"] == 1000
    snap = leaderboards.load_snapshot(cfg["library_folder"], "2026-season-4", sched["series"][0]["id"], 7)
    got = {r["key"]: r["t"] for r in snap["board"]}
    assert got == {"jordan-x": 98.6, "me": 98.871, "tyler-q": 99.1, "sam-y": 99.4}, got
    assert snap["requests"] == 2


def test_laps_outside_the_race_week_never_count():
    at(NOW)

    def laps(kw):
        old = lap("jordan-x", 97.0, day="2026-10-27T23:59:00Z")      # the day before week 7
        if kw["group"] == "none":
            return [old, lap("jordan-x", 98.9)]
        return [old, lap("alex", 98.871)]
    cfg, api = lib(), FakeApi(laps)
    sched = make_sched([MX5(first_week=7)])
    leaderboards.refresh(api, cfg, sched, mine(sched), log=lambda m: None)
    snap = leaderboards.load_snapshot(cfg["library_folder"], "2026-season-4", sched["series"][0]["id"], 7)
    assert {r["key"]: r["t"] for r in snap["board"]} == {"me": 98.871, "jordan-x": 98.9}
    assert len(api.laps()) == 2


def test_second_refresh_within_ten_minutes_is_free():
    at(NOW)
    cfg, api = lib(), FakeApi()
    sched = make_sched([MX5(first_week=7)])
    leaderboards.refresh(api, cfg, sched, mine(sched), log=lambda m: None)
    at(NOW + timedelta(minutes=9))
    res = leaderboards.refresh(api, cfg, sched, mine(sched), log=lambda m: None)
    assert len(api.calls) == 1 and res["requests"] == 0, api.calls
    p = leaderboards.plan(cfg, api, sched, mine(sched))
    assert p["est_total"] == 0 and p["series"][0]["cached"] and p["series"][0]["est_requests"] == 0
    at(NOW + timedelta(minutes=11))
    assert leaderboards.plan(cfg, api, sched, mine(sched))["est_total"] == 1
    leaderboards.refresh(api, cfg, sched, mine(sched), log=lambda m: None)
    assert len(api.calls) == 2


def test_final_week_not_refetched_unless_force_old():
    cfg, api = lib(), FakeApi()
    sched = make_sched([MX5(first_week=6)])
    sid = sched["series"][0]["id"]
    at(datetime(2026, 10, 25, 12, tzinfo=timezone.utc))            # during week 6
    leaderboards.refresh(api, cfg, sched, mine(sched), log=lambda m: None)
    assert len(api.calls) == 1
    at(NOW)                                                          # week 6 ended 2026-10-28: 2.8 days ago
    leaderboards.refresh(api, cfg, sched, mine(sched), log=lambda m: None)
    assert len(api.calls) == 3, api.calls                            # week 7 + week 6 (it wasn't final yet)
    w6 = leaderboards.load_snapshot(cfg["library_folder"], "2026-season-4", sid, 6)
    assert w6["final"] is True and w6["end"] == "2026-10-28"
    at(NOW + timedelta(hours=1))
    leaderboards.refresh(api, cfg, sched, mine(sched), log=lambda m: None)
    assert [c[1]["after"] for c in api.calls[3:]] == ["2026-10-28T00:00:00Z"], api.calls[3:]   # only week 7
    p = leaderboards.plan(cfg, api, sched, mine(sched))
    assert all(r["week"] != 6 for r in p["series"])
    at(NOW + timedelta(hours=2))
    n = len(api.calls)
    leaderboards.refresh(api, cfg, sched, mine(sched), force_old=True, log=lambda m: None)
    afters = sorted(c[1]["after"] for c in api.calls[n:])
    assert afters == ["2026-10-21T00:00:00Z", "2026-10-28T00:00:00Z"], afters


def test_past_weeks_are_capped_at_three_per_refresh():
    at(NOW)
    cfg, api = lib(), FakeApi()
    sched = make_sched([MX5(), make_series("Production Car Challenge", ["Global Mazda MX-5 Cup"], track_id=30)])
    p = leaderboards.plan(cfg, api, sched, mine(sched))
    assert len(api.calls) == 0                                       # the estimate never asks
    assert sum(1 for r in p["series"] if r["past"]) == 3 and p["est_total"] == 5, p
    leaderboards.refresh(api, cfg, sched, mine(sched), log=lambda m: None)
    assert len(api.calls) == 5, api.calls                            # 2 this week + 3 past weeks in all
    weeks = sorted(int(c[1]["after"][5:7] + c[1]["after"][8:10]) for c in api.calls)
    assert weeks[0] >= 1014, weeks                                   # never older than 3 weeks back


def test_nine_series_gives_eight_and_a_note():
    at(NOW)
    cfg, api = lib(), FakeApi()
    sched = make_sched([make_series(f"Series {i}", ["Global Mazda MX-5 Cup"], first_week=7) for i in range(9)])
    res = leaderboards.refresh(api, cfg, sched, mine(sched), log=lambda m: None)
    assert len(api.calls) == 8 and res["requests"] == 8, len(api.calls)
    assert res["left_out"] == 1 and any("left out" in n and "8" in n for n in res["notes"]), res["notes"]
    assert [s["id"] for s in res["skipped"]] == ["series-8"], res["skipped"]
    p = leaderboards.plan(cfg, api, sched, mine(sched))
    assert p["left_out"] == 1 and len({r["id"] for r in p["series"]}) == 8 and len(api.calls) == 8
    v = leaderboards.view(cfg["library_folder"], sched)
    assert len(v["series"]) == 8 and v["notes"] and v["skipped"]


def test_hard_budget_never_more_than_two_per_series_week():
    at(NOW)

    def laps(kw):   # every answer has a partial "best": the worst case
        d = in_week(kw)
        return [lap("jordan-x", 9.0, day=d), lap("alex", 98.871, day=d), lap("jordan-x", 98.5, day=d)]
    cfg, api = lib(), FakeApi(laps)
    sched = make_sched([make_series(f"S{i}", ["Global Mazda MX-5 Cup"]) for i in range(10)])
    res = leaderboards.refresh(api, cfg, sched, mine(sched), force_old=True, log=lambda m: None)
    assert len(api.calls) == (leaderboards.MAX_SERIES + leaderboards.MAX_PAST_WEEKS) * 2, len(api.calls)
    snaps = list((Path(cfg["library_folder"]) / "team" / "weekly").rglob("w*.json"))
    reqs = [json.loads(p.read_text(encoding="utf-8"))["requests"] for p in snaps]
    assert len(snaps) == 11 and all(r <= 2 for r in reqs) and sum(reqs) == len(api.calls) == res["requests"]
    assert len({p.parent.name for p in snaps}) == leaderboards.MAX_SERIES


def test_unknown_ids_skip_without_requests():
    at(NOW)
    cfg = lib()
    api = FakeApi()
    api.cached_items = lambda name: []                                # lists never loaded
    sched = make_sched([MX5(first_week=7)])
    res = leaderboards.refresh(api, cfg, sched, mine(sched), log=lambda m: None)
    assert api.calls == [] and res["skipped"][0]["note"] == leaderboards.NO_IDS_NOTE
    p = leaderboards.plan(cfg, None, sched, mine(sched))
    assert p["est_total"] == 0 and p["skipped"][0]["note"] == leaderboards.NO_IDS_NOTE
    assert p["series"][0]["ids_known"] is False


def test_me_is_asked_only_when_never_saved():
    at(NOW)
    cfg, api = lib(), FakeApi(me_saved=False)
    sched = make_sched([MX5(first_week=7)])
    leaderboards.refresh(api, cfg, sched, mine(sched), log=lambda m: None)
    assert [c[0] for c in api.calls] == ["me", "find_laps"]
    api2 = FakeApi(me_saved=False)
    at(NOW + timedelta(minutes=1))
    leaderboards.refresh(api2, cfg, sched, mine(sched), log=lambda m: None)
    assert api2.calls == []                                           # fresh snapshot: not even me()


def test_multiclass_positions_within_class_and_all_car_ids():
    at(NOW)

    def laps(kw):
        return [lap("alex-p", 126.512, car=170), lap("jordan-x", 127.204, car=170),
                lap("alex", 136.903, car=180), lap("sam-y", 137.48, car=190), lap("kim-z", 137.1, car=180)]
    cfg, api = lib(), FakeApi(laps)
    imsa = make_series("IMSA SportsCar Championship", ["Porsche 963 GTP", "BMW M4 GT3 EVO", "Ferrari 296 GT3"],
                       track_id=18, first_week=7)
    sched = make_sched([imsa])
    leaderboards.refresh(api, cfg, sched, mine(sched), log=lambda m: None)
    kw = api.calls[0][1]
    assert kw["group"] == "driver-car" and kw["cars"] == [170, 180, 190] and kw["tracks"] == [18], kw
    snap = leaderboards.load_snapshot(cfg["library_folder"], "2026-season-4", imsa["id"], 7)
    rows = {r["key"]: r for r in snap["board"]}
    assert snap["multiclass"] is True
    assert [rows[k]["pos"] for k in ("alex-p", "jordan-x", "alex" if "alex" in rows else "me")] == [1, 2, 3]
    me = rows["me"]
    assert me["class"] == "GT3" and me["class_pos"] == 1 and me["gap"] == 0.0 and me["gap_pct"] == 0.0
    assert rows["kim-z"]["class_pos"] == 2 and abs(rows["kim-z"]["gap"] - round(137.1 - 136.903, 3)) < 1e-9
    assert rows["jordan-x"]["class"] == "GTP" and rows["jordan-x"]["class_pos"] == 2
    assert rows["sam-y"]["class_pos"] == 3 and rows["sam-y"]["car"] == "Ferrari 296 GT3"
    # a GT3 lap is never "implausible" against the GTP times
    assert len(rows) == 5


# ---------------------------------------------------------------- head-to-heads
def _snap(week, board, multiclass=False, sid="mx5"):
    return {"schema": 1, "season": "2026 Season 4", "series_id": sid, "series": sid.upper(), "week": week,
            "multiclass": multiclass, "board": board}


def _r(key, t, cls="MX-5"):
    return {"key": key, "name": key.title(), "t": t, "class": cls, "is_me": key == "me"}


def test_h2h_sign_and_verdicts():
    one = leaderboards.head_to_heads([_snap(3, [_r("me", 99.0), _r("jordan", 98.5)])])
    j = one[0]
    assert j["series"][0]["weeks"][0]["gap_pct"] > 0                  # positive = they were quicker
    assert abs(j["series"][0]["weeks"][0]["gap_pct"] - round((99.0 / 98.5 - 1) * 100, 3)) < 1e-9
    assert j["shared_weeks"] == 1 and j["verdict"] is None and j["behind"] == 1 and j["ahead"] == 0
    two = leaderboards.head_to_heads([_snap(3, [_r("me", 99.0), _r("jordan", 98.5)]),
                                      _snap(4, [_r("me", 90.0), _r("jordan", 89.7)])])
    assert two[0]["verdict"] == "slightly quicker than you", two[0]
    lvl = leaderboards.head_to_heads([_snap(3, [_r("me", 99.0), _r("jordan", 99.05)]),
                                      _snap(4, [_r("me", 90.0), _r("jordan", 89.95)])])
    assert lvl[0]["verdict"] == "about level"
    slow = leaderboards.head_to_heads([_snap(3, [_r("me", 99.0), _r("jordan", 100.0)]),
                                       _snap(4, [_r("me", 90.0), _r("jordan", 91.0)])])
    assert slow[0]["verdict"] == "slower than you" and slow[0]["ahead"] == 2


def test_h2h_compares_within_class_only():
    snaps = [_snap(7, [_r("alex", 126.5, "GTP"), _r("me", 136.9, "GT3"), _r("sam", 137.4, "GT3")], True, "imsa")]
    h = {p["key"]: p for p in leaderboards.head_to_heads(snaps)}
    assert h["alex"]["shared_weeks"] == 0 and h["alex"]["verdict"] is None
    assert h["sam"]["shared_weeks"] == 1 and h["sam"]["series"][0]["class"] == "GT3"


def test_view_reads_snapshots_and_degrades():
    at(NOW)
    cfg = lib()
    assert leaderboards.view(cfg["library_folder"])["series"] == []          # nothing saved yet
    api = FakeApi()
    sched = make_sched([MX5(first_week=7)])
    leaderboards.refresh(api, cfg, sched, mine(sched), log=lambda m: None)
    bad = Path(cfg["library_folder"]) / "team" / "weekly" / "2026-season-4" / "x" / "w01.json"
    bad.parent.mkdir(parents=True)
    bad.write_text("{not json", encoding="utf-8")
    v = leaderboards.view(cfg["library_folder"])
    assert v["season"] == "2026 Season 4" and len(v["series"]) == 1
    w = v["series"][0]["weeks"][0]
    assert w["week"] == 7 and w["final"] is False and len(w["board"]) == 3 and v["series"][0]["current_week"] == 7
    assert {p["key"] for p in v["h2h"]} == {"jordan-x", "sam-y"}
    assert leaderboards.view(cfg["library_folder"], sched)["series"][0]["id"] == sched["series"][0]["id"]


def test_fixture_shapes_match_the_code():
    at(NOW)

    def laps(kw):
        if kw["cars"] == [67]:
            return [lap("jordan-x", 98.412), lap("alex", 98.871), lap("sam-y", 99.5)]
        return [lap("alex-p", 126.512, car=170), lap("alex", 136.903, car=180), lap("sam-y", 137.48, car=190)]
    cfg, api = lib(), FakeApi(laps)
    sched = make_sched([MX5(first_week=7), make_series("IMSA SportsCar Championship",
                                                        ["Porsche 963 GTP", "BMW M4 GT3 EVO", "Ferrari 296 GT3"],
                                                        track_id=18, first_week=7)])
    leaderboards.refresh(api, cfg, sched, mine(sched), log=lambda m: None)
    got = leaderboards.view(cfg["library_folder"], sched)
    fx = json.loads((FIX / "sample_weekly_view.json").read_text(encoding="utf-8"))
    assert set(fx) | {"membership", "team"} == set(got), (set(fx) ^ set(got))   # 2.6: membership keys
    assert set(fx["series"][0]) == set(got["series"][0])
    assert set(fx["series"][0]["weeks"][0]) == set(got["series"][0]["weeks"][0])
    assert set(fx["series"][0]["weeks"][0]["board"][0]) == set(got["series"][0]["weeks"][0]["board"][0])
    assert set(fx["h2h"][0]) == set(got["h2h"][0])
    assert set(fx["h2h"][0]["series"][0]) == set(got["h2h"][0]["series"][0])
    tj = json.loads((FIX / "sample_team_with_weekly.json").read_text(encoding="utf-8"))
    assert tj["weekly"] == fx and tj["combos"][0]["learn"]
    lf = json.loads((FIX / "sample_learn_from.json").read_text(encoding="utf-8"))
    rows = team.learn_for_combo(tj["combos"][0])
    assert set(lf[0]) == set(rows[0]), set(lf[0]) ^ set(rows[0])


# ---------------------------------------------------------------- who to learn from
def _m(b=80.0, mn=60.0, ex=75.0, ft=120.0, gear=3):
    return {"brake_before_apex_m": b, "min_mph": mn, "exit_mph": ex, "full_thr_after_apex_m": ft, "gear": gear}


def _team(corners, board=None, drivers=("me", "jordan-x", "sam-y", "fast-f")):
    board = board or [{"key": "fast-f", "t": 95.0}, {"key": "jordan-x", "t": 98.4}, {"key": "me", "t": 98.9},
                      {"key": "sam-y", "t": 99.2}]
    return {"version": 1, "combos": [{"track": "Okayama", "car": "MX-5", "track_id": 51, "car_id": 67, "board": board,
                                      "kings": {"L": 3700.0, "drivers": list(drivers),
                                                "names": {"me": "Alex Driver", "jordan-x": "Jordan X",
                                                          "sam-y": "Sam Y", "fast-f": "Fast F"},
                                                "corners": corners}}]}


def _kc(cid, apex, times, metrics=None):
    return {"id": cid, "apex_m": apex, "times": times, "metrics": metrics or {}}


def _review(apexes, ids=(51, 67)):
    return {"meta": {"track": "Okayama", "car": "MX-5"}, "L": 3700.0,
            "combo_ids": {"track_id": ids[0], "car_id": ids[1]},
            "corners": [{"id": f"C{i + 1}", "apex_m": a} for i, a in enumerate(apexes)]}


def test_learn_from_matches_by_apex_not_id():
    # the team's laps found an extra corner first: review C3 (1480 m) is the team's C4 (1495 m)
    corners = [_kc("C1", 200.0, {"me": 5.0, "jordan-x": 5.0, "sam-y": 5.0, "fast-f": 5.0}),
               _kc("C2", 400.0, {"me": 7.9, "jordan-x": 7.9, "sam-y": 7.9, "fast-f": 7.9}),
               _kc("C3", 900.0, {"me": 6.0, "jordan-x": 6.0, "sam-y": 6.0, "fast-f": 6.0}),
               _kc("C4", 1495.0, {"me": 9.42, "jordan-x": 9.24, "sam-y": 9.40, "fast-f": 9.40},
                   {"me": _m(92, 61, 77, 140, 3), "jordan-x": _m(81, 64, 79, 118, 3)})]
    rows = team.learn_from(_team(corners), _review([400.0, 905.0, 1480.0]))
    assert [r["corner"] for r in rows] == ["C3"], rows
    r = rows[0]
    assert r["team_corner"] == "C4" and r["apex_m"] == 1480.0 and r["teammate"]["key"] == "jordan-x"
    assert r["gap_s"] == 0.18 and r["your_t"] == 9.42 and r["their_t"] == 9.24 and r["basis"] == "best laps"
    assert r["how"] == {"brake_before_apex_m": [92, 81], "min_mph": [61, 64], "full_thr_after_apex_m": [140, 118]}
    assert r["text"].startswith("Jordan's best lap was 0.18 s quicker than yours through C3.")
    assert "braked about 10 m later" in r["text"] and "3 mph more" in r["text"]
    assert "best lap" in r["text"] and not FORBIDDEN.search(r["text"]) and " so " not in r["text"]
    assert r["alt"] is None                                          # sam-y 0.02 s: under the gate
    assert team.learn_from(_team(corners), _review([400.0], ids=(52, 67))) == []   # another layout


def test_learn_from_gate_similar_pace_and_alt():
    corners = [_kc("C1", 500.0, {"me": 8.00, "jordan-x": 7.96, "sam-y": 8.1, "fast-f": 8.2}),   # 0.04: no
               _kc("C2", 1500.0, {"me": 9.50, "jordan-x": 9.30, "sam-y": 9.45, "fast-f": 9.10})]
    rows = team.learn_from(_team(corners), _review([500.0, 1500.0]))
    assert [r["corner"] for r in rows] == ["C2"], rows
    r = rows[0]
    # fast-f is 0.40 s quicker here but 4 % quicker over the lap; jordan (0.5 %) is the similar-pace pick
    assert r["teammate"]["key"] == "jordan-x" and r["similar_pace"] is True and r["lap_gap_pct"] == 0.51
    assert r["alt"] == {"key": "fast-f", "name": "Fast F", "gap_s": 0.4}
    # with nobody at similar pace, the biggest gap wins
    corners2 = [_kc("C2", 1500.0, {"me": 9.50, "jordan-x": 9.40, "fast-f": 9.10})]
    board = [{"key": "fast-f", "t": 95.0}, {"key": "jordan-x", "t": 96.0}, {"key": "me", "t": 98.9}]
    r2 = team.learn_from(_team(corners2, board, drivers=("me", "jordan-x", "fast-f")), _review([1500.0]))[0]
    assert r2["teammate"]["key"] == "fast-f" and r2["similar_pace"] is False and r2["alt"]["key"] == "jordan-x"


def test_learn_from_how_thresholds():
    me = _m(80.0, 60.0, 75.0, 120.0, 3)
    under = {"jordan-x": _m(76.0, 61.4, 75.0, 111.0, 3)}             # 4 m, 1.4 mph, 9 m, same gear
    over = {"jordan-x": _m(75.0, 61.5, 75.0, 110.0, 2)}              # 5 m, 1.5 mph, 10 m, other gear
    for mets, want in ((under, {}), (over, {"brake_before_apex_m": [80.0, 75.0], "min_mph": [60.0, 61.5],
                                            "full_thr_after_apex_m": [120.0, 110.0], "gear": [3, 2]})):
        corners = [_kc("C1", 700.0, {"me": 8.0, "jordan-x": 7.8}, {"me": me, **mets})]
        r = team.learn_from(_team(corners, drivers=("me", "jordan-x")), _review([700.0]))[0]
        assert r["how"] == want, (r["how"], want)
        assert not FORBIDDEN.search(r["text"]) and "best lap" in r["text"]
    assert "2nd gear at the slowest point (you were in 3rd)" in r["text"], r["text"]
    # a lift-only corner (no brake point) just leaves braking out
    corners = [_kc("C1", 700.0, {"me": 8.0, "jordan-x": 7.8},
                   {"me": _m(b=None), "jordan-x": _m(b=40.0)})]
    r = team.learn_from(_team(corners, drivers=("me", "jordan-x")), _review([700.0]))[0]
    assert "brake_before_apex_m" not in r["how"]


def test_learn_from_without_me_or_data_is_empty():
    corners = [_kc("C1", 700.0, {"jordan-x": 7.8, "sam-y": 8.0})]
    assert team.learn_from(_team(corners, drivers=("jordan-x", "sam-y")), _review([700.0])) == []
    assert team.learn_from({}, _review([700.0])) == []
    assert team.learn_from({"combos": [{"track": "Okayama", "car": "MX-5", "kings": None}]}, _review([700.0])) == []
    assert team.learn_from(_team([]), {"meta": {}}) == []
    # old team.json (2.4): no metrics -> still the gap line, with no "how" facts
    corners = [{"id": "C1", "apex_m": 700.0, "times": {"me": 8.0, "jordan-x": 7.8}}]
    r = team.learn_from(_team(corners, drivers=("me", "jordan-x")), _review([700.0]))
    assert r and r[0]["how"] == {} and "On that lap" not in r[0]["text"]


def test_learn_from_caps_at_five_sorted_by_gap():
    corners = [_kc(f"C{i}", 300.0 * i, {"me": 8.0, "jordan-x": 8.0 - 0.06 * i}) for i in range(1, 9)]
    rows = team.learn_from(_team(corners, drivers=("me", "jordan-x")), _review([300.0 * i for i in range(1, 9)]))
    assert len(rows) == 5 and [r["gap_s"] for r in rows] == sorted((r["gap_s"] for r in rows), reverse=True)


def test_learn_from_saved_and_render_saved():
    at(NOW)
    cfg = lib()
    folder = Path(cfg["library_folder"])
    assert team.learn_from_saved(folder, _review([700.0])) == []                # no team page yet
    page = team.render_saved(folder)                                            # no team.json: still a page
    assert page.exists() and "weekly" in json.loads((folder / "team" / "team.json").read_text(encoding="utf-8"))
    # an old (2.4) team.json without weekly/learn/metrics
    corners = [{"id": "C1", "apex_m": 700.0, "times": {"me": 8.0, "jordan-x": 7.8}}]
    old = _team(corners, drivers=("me", "jordan-x"))
    old.update({"team": {"slug": "midfield-menace", "name": "Midfield Menace"}, "me": {"name": "S", "key": "me"},
                "h2h": [], "notes": [], "requests": 3, "generated_at": "2026-10-01T00:00:00+00:00"})
    (folder / "team" / "team.json").write_text(json.dumps(old), encoding="utf-8")
    (folder / "team" / "team.json.bad").write_text("x", encoding="utf-8")
    assert team.learn_from_saved(folder, _review([700.0]))[0]["teammate"]["key"] == "jordan-x"
    sched = make_sched([MX5(first_week=7)])
    leaderboards.refresh(FakeApi(), cfg, sched, mine(sched), log=lambda m: None)
    team.render_saved(folder)
    d = json.loads((folder / "team" / "team.json").read_text(encoding="utf-8"))
    assert d["weekly"]["series"][0]["weeks"][0]["week"] == 7 and d["combos"][0]["learn"]
    assert d["requests"] == 3 and d["team"]["name"] == "Midfield Menace"
    html = (folder / "team" / "index.html").read_text(encoding="utf-8")
    assert "Okayama" in html
    (folder / "team" / "team.json").write_text("[]", encoding="utf-8")
    assert team.learn_from_saved(folder, _review([700.0])) == []


def test_corner_kings_metrics_from_telemetry():
    a = synth.lap_csv({0: 0.0, 2: 0.0})
    b = synth.lap_csv({0: -2.0, 2: 1.5})
    entries = [{"key": "me", "name": "Me", "t": a[1], "csv": a[0], "is_me": True},
               {"key": "jordan-x", "name": "Jordan X", "t": b[1], "csv": b[0], "is_me": False}]
    k = team.corner_kings(entries, track_length=3000.0)
    assert k and len(k["corners"]) == 4, k and [c["id"] for c in k["corners"]]
    keys = {"brake_before_apex_m", "min_mph", "exit_mph", "full_thr_after_apex_m", "gear"}
    for c in k["corners"]:
        assert set(c["metrics"]) == {"me", "jordan-x"}
        for m in c["metrics"].values():
            assert set(m) == keys and m["min_mph"] > 0 and m["gear"] in (3, 5)
            assert m["brake_before_apex_m"] is None or m["brake_before_apex_m"] > 0
            assert m["full_thr_after_apex_m"] is None or m["full_thr_after_apex_m"] > -50
    c3 = k["corners"][2]
    assert c3["metrics"]["jordan-x"]["min_mph"] > c3["metrics"]["me"]["min_mph"]   # +1.5 m/s at the 3rd apex
    combo = {"board": [{"key": "me", "t": a[1]}, {"key": "jordan-x", "t": b[1]}], "kings": k}
    rows = team.learn_for_combo(combo)
    assert all(not FORBIDDEN.search(r["text"]) and "best lap" in r["text"] for r in rows)


# ---------------------------------------------------------------- hygiene
def test_no_network_code_in_new_paths():
    for mod in ("leaderboards.py",):
        tree = ast.parse((ROOT / "debrief" / mod).read_text(encoding="utf-8"))
        names = {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
        names |= {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        assert not names & {"requests", "urllib", "http", "socket"}, (mod, names)
    src = (ROOT / "debrief" / "leaderboards.py").read_text(encoding="utf-8")
    assert "| Weekly refresh |" in src                                  # the budget row is in the docstring


def test_snapshot_writes_are_atomic_and_leave_no_temp_files():
    at(NOW)
    cfg = lib()
    sched = make_sched([MX5(first_week=7)])
    leaderboards.refresh(FakeApi(), cfg, sched, mine(sched), log=lambda m: None)
    left = [p.name for p in Path(cfg["library_folder"]).rglob("*") if p.name.startswith(".tmp-") or p.suffix == ".tmp"]
    assert left == [], left


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except Exception as e:  # noqa: BLE001
                failed += 1
                import traceback
                traceback.print_exc()
                print(f"FAIL {name}: {e}")
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"{'FAILED' if failed else 'passed'}: {failed} failure(s)")
    sys.exit(1 if failed else 0)
