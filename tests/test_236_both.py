"""Debrief 2.36 "Both sources": your own laps from this PC, other drivers' laps from Garage 61.

    python3 tests/ci_run.py tests/test_236_both.py

Stand-in .ibt files (tests/fake_ibt.py) in a temp telemetry folder and a stand-in Garage 61 that records every request.
Checks: Add comparison laps on a PC review keeps the PC laps, result, From iRacing and Setup and only adds the
comparison laps (meta.source "pc", meta.refs_from "garage61"); no lap of the driver's own is requested from Garage 61;
the main comparison lap is the one in conditions like the race's, with the reason on the page; unknown conditions keep
the old choice and say nothing new; the gap line appears only where a defensible link exists, never splits fuel, and
uses correlational words; a review built from Garage 61 from the start still works; the page draws without script errors.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
TMP = Path(tempfile.mkdtemp(prefix="debrief-236-"))
LIB, TEL = TMP / "lib", TMP / "telemetry"
LIB.mkdir()
TEL.mkdir()
os.environ.update(DEBRIEF_SETTINGS=str(TMP / "settings.toml"), DEBRIEF_CACHE_DIR=str(TMP / "cache"),
                  DEBRIEF_SCHEDULE_DIR=str(TMP / "schedule"), DEBRIEF_UPDATE_STATE=str(TMP / "update.json"),
                  DEBRIEF_NO_NETWORK="1")
(TMP / "settings.toml").write_text(f"library_folder = '{LIB.as_posix()}'\niracing_folder = '{TMP.as_posix()}'\n"
                                   "sync_on_open = false\n", encoding="utf-8")

import fake_ibt as F  # noqa: E402
import test_235_seeit as SEEIT  # noqa: E402
from debrief import app, depth, library, localrace, recordings, results  # noqa: E402

# test_235_seeit sets its own environment when imported: put this test's back
os.environ.update(DEBRIEF_SETTINGS=str(TMP / "settings.toml"), DEBRIEF_CACHE_DIR=str(TMP / "cache"),
                  DEBRIEF_SCHEDULE_DIR=str(TMP / "schedule"), DEBRIEF_UPDATE_STATE=str(TMP / "update.json"))
app.LOG_FILE = str(TMP / "debrief.log")
FAILS = []
SUB = 81236000
RACE = TEL / "mclaren570sgt4_zolder gp 2026-11-10 20-00-00.ibt"
LAPS = [99.4, 98.8, 98.6, 98.9, 98.7, 99.0, 98.5, 98.8]


def check(ok, what):
    print(("ok   " if ok else "FAIL ") + what)
    if not ok:
        FAILS.append(what)


def csv_for(lap_time, n=1200):
    """A fake lap of a 3 km track with three corners, as Garage 61 returns it."""
    p = np.linspace(0, 0.999, n)
    v = np.full(n, 60.0)
    for c in (0.2, 0.5, 0.8):
        v -= 25.0 * np.exp(-((p - c) / 0.02) ** 2)
    rows = ["Speed,LapDistPct,Brake,Throttle,Gear,LatAccel,LongAccel,SteeringWheelAngle,RPM"]
    rows += [f"{a:.3f},{b:.5f},0,1,4,0,0,0,7000" for a, b in zip(v, p)]
    return "\n".join(rows)


def ref_lap(lid, slug, name, t, track_temp=None, wet=0.0, air=19.0):
    lap = {"id": lid, "lapTime": t, "clean": True, "event": "EV1", "sessionType": 3,
           "driver": {"slug": slug, "firstName": name, "lastName": "Test"},
           "car": {"id": 135}, "track": {"id": 200}, "startTime": "2026-11-10T20:30:00Z"}
    if track_temp is not None:
        lap.update(trackTemp=track_temp, airTemp=air, trackWetness=wet)
    return lap


class StandIn:
    """Garage 61 as the app sees it: it records every request. Own laps (ids L...) can be listed and downloaded only in
    the tests that build a review from Garage 61 itself."""

    def __init__(self, refs, own=None):
        self.refs = refs
        self.own = own or []
        self.requests_made = 0
        self.csv_asked = []
        self.lap_queries = []

    def me(self):
        return {"slug": "me", "firstName": "Swazi", "lastName": "Williams"}

    def teams(self, refresh=False):
        return [{"slug": "crew", "id": 1}]

    def cars(self, refresh=False):
        return [{"id": 135, "name": "McLaren 570S GT4"}]

    def tracks(self, refresh=False):
        return [{"id": 200, "name": "Circuit Zolder", "variant": "Grand Prix", "lapLength": 3.98}]

    def event_for_subsession(self, sub):
        self.requests_made += 1
        return "EV1"

    def find_laps(self, fresh=False, **params):
        self.requests_made += 1
        self.lap_queries.append(dict(params))
        if params.get("drivers") == ["following"]:
            return [dict(x) for x in self.refs]
        return [dict(x) for x in self.own]

    def my_statistics(self, start):
        self.requests_made += 1
        return []

    def lap_csv(self, lid):
        self.requests_made += 1
        self.csv_asked.append(lid)
        t = next((x.get("lapTime") for x in self.refs + self.own if x["id"] == lid), 98.0)
        return csv_for(t)


def upgrade(api):
    real = app.make_api
    app.make_api = lambda cfg: api
    try:
        info = {}
        page = app.cmd_review_upgrade(app.load_settings(), SUB, progress=None, info=info)
        return page, info
    finally:
        app.make_api = real


def load(page):
    return json.loads((Path(page).parent / "review.json").read_text(encoding="utf-8"))


def fresh_pc_review(track_temp=33, wetness=1):
    for d in (LIB / "reviews").glob("*"):
        shutil.rmtree(d, ignore_errors=True)
    for f in TEL.glob("*.ibt"):
        f.unlink()
    F.make(RACE, LAPS, session="Race", subsession=SUB, incidents=(2,), start_date=1_794_340_000,
           track_temp=track_temp, air_temp=20, wetness=wetness)
    return app.cmd_review_local(app.load_settings(), open_after=False)


# ================================================================ 1. own laps from the PC, others from Garage 61
def test_both_sources():
    # a recording's "From iRacing" section and a typed-in result are PC-only parts that must survive
    fi = recordings.view(SEEIT._sec(), SEEIT.SUMMARY)
    real_for = recordings.for_review
    recordings.for_review = lambda data, lib: json.loads(json.dumps(fi))
    try:
        page0 = fresh_pc_review()
        results.save(LIB, Path(page0).parent.name, {"finish_pos": 4, "field_size": 20})
        app.cmd_review_local(app.load_settings(), open_after=False, rebuild=True)
        before = load(page0)
        refs = [ref_lap("T1", "alex", "Alex", 97.9, 31.0), ref_lap("T2", "bo", "Bo", 97.6, 45.0),
                ref_lap("T3", "cy", "Cy", 98.2, 32.0)]
        api = StandIn(refs, own=[ref_lap("L1", "me", "Me", 98.5, 33.0)])
        page, info = upgrade(api)
    finally:
        recordings.for_review = real_for
    after = load(page)
    check(Path(page).parent.name == Path(page0).parent.name and len(list((LIB / "reviews").glob("2*"))) == 1,
          "the same review folder is updated: the race is never listed twice")
    m0, m1 = before["meta"], after["meta"]
    check(m1["source"] == "pc" and m1.get("refs_from") == "garage61" and m1["files"] == m0["files"]
          and m1["subsession"] == SUB and m1["event_id"] == m0["event_id"],
          f"meta says both sources: source pc, refs_from garage61, same files and race ({m1.get('source')}, {m1.get('refs_from')})")
    check([(l["n"], l["t"]) for l in before["laps"]] == [(l["n"], l["t"]) for l in after["laps"]]
          and before["best"]["t"] == after["best"]["t"] and before["median"] == after["median"],
          "your laps (numbers, times, best, median) are exactly the ones read from your PC")
    c0 = {c["id"]: c["per_lap"] for c in before["corners"]}
    c1 = {c["id"]: c["per_lap"] for c in after["corners"]}
    check(c0 == c1 and c0, "every corner reading of your laps is the same: the telemetry is the PC's")
    check(after.get("result", {}).get("finish_pos") == 4 and after["result"] == before["result"],
          "the result stays")
    check(after.get("from_iracing") == before.get("from_iracing") and after.get("from_iracing"),
          "the From iRacing section stays")
    check(after.get("setup") and after["setup"] == before["setup"], "the Setup section stays")
    check(after.get("agree") == before.get("agree"), "the one-answer-per-fact words are the same (recordings.agree)")
    check(m1["ai_cars"] == m0["ai_cars"] and m1["vs_ai"] == m0["vs_ai"], "the vs AI facts stay")
    names_ = [r["name"] for r in after["refs"]]
    check(len(after["refs"]) >= 2 and "Alex" in " ".join(names_) and info.get("refs") == len(after["refs"]),
          f"the teammates' laps are added as comparison laps ({names_})")
    check(after["on_track"].get("local") is not True and not before["refs"], "who was on track now comes from Garage 61")
    # fewer requests: nothing of your own is listed or downloaded
    check(api.csv_asked and all(x.startswith("T") for x in api.csv_asked),
          f"only comparison laps were downloaded, none of your own ({api.csv_asked})")
    check(not any(q.get("drivers") == ["me"] for q in api.lap_queries),
          "your own lap list is not asked for either (the track and car matched by name)")
    check(m1["requests"] == api.requests_made and api.requests_made <= 12,
          f"the review records its Garage 61 requests ({m1['requests']} = {api.requests_made})")
    row = next(r for r in library.list_reviews(LIB) if r["dir"] == Path(page).parent.name)
    check(row["source"] == "pc" and row["refs_from"] == "garage61",
          "the library row says where the laps and the comparison laps are from")
    return page, after, api


def test_picks_by_conditions(after):
    main = next(r for r in after["refs"] if r["main"])
    check(main["name"].startswith("Alex"), f"main comparison = the lap in conditions like the race ({main['name']})")
    rm = after["depth"]["ref_match"]
    check(rm["status"] == "similar" and "track 31 C vs your 33 C" in (rm.get("why") or ""),
          f"the reason names both temperatures ({rm.get('why')})")
    check(rm["why"].startswith("Similar conditions") and "both dry" in rm["why"], "and says both were dry")
    # quicker lap in very different conditions (45 C) is not the main one
    check(all(not r["main"] for r in after["refs"] if r["name"].startswith("Bo")), "the quicker 45 C lap is not the main one")


def test_unknown_keeps_old(api_cls):
    refs = [ref_lap("T1", "alex", "Alex", 97.9), ref_lap("T2", "bo", "Bo", 97.2), ref_lap("T3", "cy", "Cy", 98.2)]
    fresh_pc_review()
    page, _info = upgrade(api_cls(refs))
    d = load(page)
    main = next(r["name"] for r in d["refs"] if r["main"])
    quicker = [r for r in refs if r["lapTime"] < 98.5]
    old = depth.pick_main(quicker, 98.5, None, app.lap_time)
    check(main.startswith(old["driver"]["firstName"]), f"unknown conditions: the same main lap as before ({main})")
    rm = (d.get("depth") or {}).get("ref_match") or {}
    check("why" not in rm and not (d.get("depth") or {}).get("gap"), "and nothing new is said about conditions or a gap")
    cands = [dict(x) for x in sorted(refs, key=lambda x: x["lapTime"])]
    me = {"track_temp_c": 33.0, "wetness": 0.0, "air_temp_c": 20.0, "session": 3}
    cands[0].update(trackTemp=50.0, trackWetness=0.0)
    cands[1].update(trackTemp=32.0, trackWetness=0.0)
    check(depth.pick_quickest(cands, me, app.lap_time) is cands[1], "nobody quicker: the lap in your conditions beats a quicker hot lap")
    check(depth.pick_quickest(cands, None, app.lap_time) is cands[0], "no conditions of yours: the quickest, as before")
    check(depth.pick_quickest([dict(x) for x in refs], me, app.lap_time)["lapTime"] == min(r["lapTime"] for r in refs),
          "conditions unknown on every lap: the quickest")


def test_lap_conditions_read():
    d = load(app.cmd_review_local(app.load_settings(), open_after=False, rebuild=True))
    c = d.get("conditions") or {}
    check(c.get("track_temp_c") == 33.0 and c.get("air_temp_c") == 20.0 and c.get("wetness") == 0.0,
          f"the PC review knows its own conditions from the telemetry channels ({c})")
    for f in TEL.glob("*.ibt"):
        f.unlink()
    F.make(RACE, LAPS, session="Race", subsession=SUB, start_date=1_794_340_000)
    d = load(app.cmd_review_local(app.load_settings(), open_after=False, rebuild=True))
    c = d.get("conditions") or {}
    check(c.get("track_temp_c") == 22.2 and c.get("wetness") is None,
          f"with no live channels the session text's temperature is used and wetness stays unknown ({c})")


# ================================================================ 3. the gap line
def _race(temp, typical, wet=0.0):
    return {"conditions": {"track_temp_c": temp, "wetness": wet, "air_temp_c": 18.0},
            "laps": [{"t": typical + 0.01 * i, "clean": True} for i in range(6)]}


def _data(my_temp=36.0, ref_temp=28.0, gap=0.8):
    d = _race(my_temp, 98.5)
    d.update(best={"t": 98.5}, median=98.8,
             refs=[{"name": "Alex Test", "t": 98.5 - gap, "main": True, "rival": False}],
             ref_match={"status": "different", "ref_t": 98.5 - gap, "mine": {"track_temp_c": my_temp, "wetness": 0.0},
                        "theirs": {"track_temp_c": ref_temp, "wetness": 0.0}})
    return d


def test_gap_line():
    # eight earlier dry races here: hotter = slower by about 0.05 s per degree
    earlier = [_race(t, 98.0 + 0.05 * (t - 20)) for t in (20, 22, 24, 27, 29, 30, 33, 38)]
    g = depth.gap_note(_data(), earlier)
    check(g is not None and g["fuel"] == "unknown", "a link in your own races: the gap is split")
    s = g["sentence"] if g else ""
    check("0.80 s" in s and "may be the conditions" in s and "a hint, not a measurement" in s,
          f"correlational words ({s[:120]})")
    check("Fuel can't be split out" in s and "doesn't have the other lap's fuel load" in s, "fuel is said to be unsplittable")
    check(g and abs(g["conditions_s"] - 0.40) < 0.08 and abs(g["conditions_s"] + g["rest_s"] - g["gap_s"]) < 0.002,
          f"about 8 C x 0.05 = 0.4 s, and the parts add up to the gap ({g and g['conditions_s']})")
    check(depth.gap_note(_data(), earlier[:3]) is None, "too few earlier races: nothing shown")
    check(depth.gap_note(_data(), [_race(t, 98.0) for t in (20, 22, 24, 27, 29, 30, 33, 38)]) is None,
          "no link between temperature and your laps: nothing shown")
    check(depth.gap_note(_data(), [_race(t, 98.0 + 0.05 * (t - 20)) for t in (30, 31, 32, 33, 34, 35, 36, 37)]) is None,
          "temperatures too close together to show a link: nothing shown")
    # 2.36 review fix: seven races at rho 0.86 is not significant, so it shows nothing; the same seven at rho 0.90 do
    soft = [_race(t, y) for t, y in zip((20, 24, 27, 30, 33, 38), (98.0, 98.3, 98.1, 98.6, 98.4, 98.9))]
    check(depth.temp_sensitivity(_race(36.0, 98.5), soft) is None and depth.gap_note(_data(), soft) is None,
          "borderline: 7 races at rho 0.86 (not significant) show nothing")
    firm = [_race(t, y) for t, y in zip((20, 24, 27, 30, 33, 38), (98.0, 98.2, 98.1, 98.5, 98.4, 98.8))]
    ts = depth.temp_sensitivity(_race(36.0, 98.5), firm)
    check(ts and ts["n"] == 7 and ts["rho"] >= 0.9, "7 races at rho 0.90 or more are enough")
    pts = [(20, 98.0), (22, 98.1), (24, 98.15), (27, 98.3), (29, 98.4), (30, 98.5), (33, 98.6), (38, 105.0)]
    odd = depth.temp_sensitivity(_race(36.0, 98.7), [_race(t, y) for t, y in pts])
    check(odd and 0.02 < odd["s_per_c"] < 0.08, f"the slope is Theil-Sen: one odd race can't tilt it ({odd and odd['s_per_c']})")
    d = _data()
    d["ref_match"]["theirs"]["track_temp_c"] = None
    check(depth.gap_note(d, earlier) is None, "the other lap's temperature unknown: nothing shown")
    d = _data()
    d["ref_match"]["theirs"]["wetness"] = 40.0
    check(depth.gap_note(d, earlier) is None, "a wet-dry difference has no basis here: nothing shown")
    check(depth.gap_note(_data(my_temp=28.0, ref_temp=36.0), earlier) is None,
          "when conditions would, if anything, favour you: nothing shown")
    check(depth.gap_note(_data(gap=0.01), earlier) is None, "a tiny gap isn't split")
    check(depth.gap_note(_data(my_temp=36.0, ref_temp=10.0), earlier) is None, "outside the temperatures ever seen: nothing shown")
    d = _data()
    d["refs"][0]["t"] = 97.0
    check(depth.gap_note(d, earlier) is None, "a main lap that changed since conditions were read isn't paired")


# ================================================================ 4. a Garage 61 review is unchanged
def test_garage61_review_unchanged():
    now = datetime(2026, 10, 20, 20, 0, tzinfo=timezone.utc)
    own = [{"id": f"L{i}", "track": {"id": 200, "name": "Circuit Zolder", "variant": "Grand Prix"},
            "car": {"id": 135, "name": "McLaren 570S GT4"}, "event": "EV1", "sessionType": 3,
            "startTime": (now + timedelta(minutes=2 * i)).strftime("%Y-%m-%dT%H:%M:%SZ"), "lapTime": 99.5 + (i % 3) * 0.2,
            "clean": True, "driver": {"slug": "me"}, "trackTemp": 30.0, "airTemp": 20.0, "trackWetness": 0.0}
           for i in range(8)]
    refs = [ref_lap("T1", "alex", "Alex", 98.4, 29.0), ref_lap("T2", "bo", "Bo", 97.8, 44.0)]
    api = StandIn(refs, own)
    cfg = {"token": "test-token-123456", "library_folder": str(TMP / "lib2"), "cache_dir": str(TMP / "cache2"),
           "include_practice": False, "max_reference_laps": 3, "salutation": ""}
    real_make = app.make_api
    app.make_api = lambda c: api
    try:
        page = app.cmd_review(cfg, "EV1", open_after=False, session_kind=3, track_id=200, car_id=135)
    finally:
        app.make_api = real_make
    d = load(page)
    check("source" not in d["meta"] and "refs_from" not in d["meta"], "a Garage 61 review says nothing of a PC")
    check(sum(1 for x in api.csv_asked if x.startswith("L")) == 8, "it still downloads your own laps from Garage 61")
    main = next(r["name"] for r in d["refs"] if r["main"])
    check(main.startswith("Alex"), f"the conditions rule picks its main comparison lap too ({main})")
    check("track 29 C vs your 30 C" in ((d.get("depth") or {}).get("ref_match") or {}).get("why", ""), "with the reason")
    return page


# ================================================================ 6. review fixes (second pass)
from debrief.g61 import Garage61Error  # noqa: E402
from debrief.desktop import server as SRV  # noqa: E402


class Offline(StandIn):
    """Garage 61 answers the track and car lists (cached) but the comparison-lap search fails."""

    def find_laps(self, fresh=False, **params):
        self.requests_made += 1
        if params.get("drivers") == ["following"]:
            raise Garage61Error("Debrief can't reach Garage 61.")
        return super().find_laps(fresh=fresh, **params)


class NoCsv(StandIn):
    def lap_csv(self, lid):
        self.requests_made += 1
        self.csv_asked.append(lid)
        return None


class Unmatched(StandIn):
    """Names that don't match the PC's, so the race's own lap list is asked for (the fallback)."""

    def __init__(self, refs, match_on=99):
        super().__init__(refs, [])
        self.match_on = match_on
        self.me_queries = 0

    def tracks(self, refresh=False):
        return [{"id": 200, "name": "Somewhere Else"}]

    def cars(self, refresh=False):
        return [{"id": 135, "name": "Other Car"}]

    def my_statistics(self, start):
        self.requests_made += 1
        return [{"day": f"2026-11-0{i}", "track": {"id": 300 + i}, "car": {"id": 400 + i}} for i in range(1, 9)]

    def find_laps(self, fresh=False, **params):
        if params.get("drivers") == ["me"]:
            self.requests_made += 1
            self.me_queries += 1
            if self.me_queries >= self.match_on:
                return [{"id": "L9", "track": {"id": 200}, "car": {"id": 135}}]
            return []
        return super().find_laps(fresh=fresh, **params)


def cfg_with_token():
    return dict(app.load_settings(), token="test-token-123456")


def rebuild_pc(api, cfg=None, refs_from="garage61"):
    real = app.make_api
    app.make_api = lambda c: api
    try:
        return app.cmd_review_local(cfg or cfg_with_token(), open_after=False, rebuild=True, refs_from=refs_from)
    finally:
        app.make_api = real


def test_rebuild_keeps_the_comparison_laps():
    refs = [ref_lap("T1", "alex", "Alex", 97.9, 31.0), ref_lap("T2", "bo", "Bo", 97.6, 45.0)]
    fresh_pc_review()
    page, _ = upgrade(StandIn(refs))
    before = load(page)
    params = SRV.review_rebuild_params(Path(page).parent)
    check(params.get("local") and params.get("refs_from") == "garage61" and params["rebuild"],
          f"Rebuild of a both-sources review remembers it has comparison laps ({params})")
    # the click: comparison laps are asked for again
    api = StandIn(refs)
    page2 = rebuild_pc(api, refs_from=params.get("refs_from"))
    after = load(page2)
    check(len(after["refs"]) == len(before["refs"]) >= 2 and after["meta"].get("refs_from") == "garage61"
          and (after.get("depth") or {}).get("ref_match", {}).get("why") == (before.get("depth") or {}).get("ref_match", {}).get("why"),
          "Rebuild keeps the comparison laps and the why line")
    check(api.csv_asked and all(x.startswith("T") for x in api.csv_asked), "and still downloads none of his own laps")
    # a plain PC review (no comparison laps) rebuilds from the PC alone, as before
    fresh_pc_review()
    plain = SRV.review_rebuild_params(Path(app.cmd_review_local(app.load_settings(), open_after=False)).parent)
    check("refs_from" not in plain, "a review with no comparison laps rebuilds from the PC alone")
    # Garage 61 can't be reached: the old review and its comparison laps stay, and it is said
    fresh_pc_review()
    page, _ = upgrade(StandIn(refs))
    raw_before = (Path(page).parent / "review.json").read_text(encoding="utf-8")
    for label, api in (("offline", Offline(refs)), ("no download", NoCsv(refs))):
        try:
            rebuild_pc(api)
            check(False, f"{label}: Rebuild must not drop the comparison laps")
        except SystemExit as e:
            check("left as it is" in str(e) and "comparison laps" in str(e), f"{label}: the review is left as it is, and it says so ({e})")
        check((Path(page).parent / "review.json").read_text(encoding="utf-8") == raw_before, f"{label}: review.json is untouched")
    try:
        rebuild_pc(StandIn(refs), cfg=dict(app.load_settings(), token=""))
        check(False, "no token: Rebuild must not drop the comparison laps")
    except SystemExit as e:
        check("left as it is" in str(e) and "token" in str(e), f"no token: the saved comparison laps are kept ({e})")
    check((Path(page).parent / "review.json").read_text(encoding="utf-8") == raw_before, "no token: review.json is untouched")
    # Rebuild all runs the same path
    done = SRV._rebuild_all.__globals__["app"]
    real = app.make_api
    app.make_api = lambda c: StandIn(refs)
    try:
        res = SRV._rebuild_all(cfg_with_token(), [(Path(page).parent.name, params)], lambda *a, **k: None)
    finally:
        app.make_api = real
    check(res["rebuilt"] == [Path(page).parent.name] and not res["failed"]
          and len(load(page)["refs"]) >= 2, f"Rebuild all keeps the comparison laps too ({res})")
    app.make_api = lambda c: Offline(refs)
    try:
        res = SRV._rebuild_all(cfg_with_token(), [(Path(page).parent.name, params)], lambda *a, **k: None)
    except SystemExit:
        res = None
    finally:
        app.make_api = real
    check((Path(page).parent / "review.json").exists() and len(load(page)["refs"]) >= 2,
          "Rebuild all with Garage 61 unreachable leaves the saved comparison laps in place")


def test_a_failed_fetch_is_not_no_comparison_laps():
    refs = [ref_lap("T1", "alex", "Alex", 97.9, 31.0)]
    for label, api in (("search failed", Offline(refs)), ("every download failed", NoCsv(refs))):
        fresh_pc_review()
        page, info = upgrade(api)
        m = load(page)["meta"]
        check(m.get("refs_failed") == "garage61" and not m.get("refs_checked") and info.get("failed") is True
              and info.get("refs") == 0, f"{label}: marked failed, not 'checked and none' ({m.get('refs_failed')}, {m.get('refs_checked')})")
    fresh_pc_review()
    page, info = upgrade(StandIn([]))
    m = load(page)["meta"]
    check(m.get("refs_checked") == "garage61" and not m.get("refs_failed") and not info.get("failed"),
          "a real 'Garage 61 had none' still says so")
    html = (ROOT / "debrief" / "templates" / "review.html").read_text(encoding="utf-8")
    js = (ROOT / "debrief" / "desktop" / "ui" / "app.js").read_text(encoding="utf-8")
    check("couldn\\'t get comparison laps from Garage 61 this time" in html and "refs_failed" in html
          and "couldn\\'t be reached, or its comparison laps couldn\\'t be downloaded" in js and "R.failed" in js,
          "the page and the banner say Garage 61 couldn't be reached, not that nobody is followed")


def test_the_lap_list_fallback_is_capped_and_honest():
    refs = [ref_lap("T1", "alex", "Alex", 97.9, 31.0)]
    fresh_pc_review()
    api = Unmatched(refs)
    page, info = upgrade(api)
    check(api.me_queries == 3 and info.get("unmatched"), f"at most 3 recent combos are tried ({api.me_queries})")
    fresh_pc_review()
    api = Unmatched(refs, match_on=2)
    page, info = upgrade(api)
    d = load(page)
    check(api.me_queries == 2 and d["meta"].get("own_list") is True and len(d["refs"]) >= 1 and info.get("own_list"),
          "when the fallback ran, the review and the result say so")
    check(not any(x.startswith("L") for x in api.csv_asked), "and none of his laps' telemetry was downloaded")
    fresh_pc_review()
    page, info = upgrade(StandIn(refs))
    check(not load(page)["meta"].get("own_list") and not info.get("own_list"), "matched by name: no lap list was asked for")
    html = (ROOT / "debrief" / "templates" / "review.html").read_text(encoding="utf-8")
    js = (ROOT / "debrief" / "desktop" / "ui" / "app.js").read_text(encoding="utf-8")
    ch = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    top = ch.split("## 2.35.0")[0]
    check("looked up your recent lap list to match the track" in html and "looked up your recent lap list" in js
          and "recent lap list" in top and "None of your own laps are downloaded" not in top,
          "page, banner and CHANGELOG word it the same honest way")


def test_mostly_dry_is_dry_and_forecast_is_not_measured():
    check(localrace._wet_percent(1) == 0 and localrace._wet_percent(2) == 0 and localrace._wet_percent(0) is None
          and localrace._wet_percent(3) > depth.SIMILAR_WETNESS and localrace._wet_percent(7) == 100.0,
          "iRacing's 'mostly dry' (2) is dry, 'very lightly wet' (3) is not")
    fresh_pc_review(wetness=2)
    d = load(app.cmd_review_local(app.load_settings(), open_after=False, rebuild=True))
    check((d.get("conditions") or {}).get("wetness") == 0.0, "a mostly dry race is read as dry")
    # only the session text's forecast: marked, never a gap line, the why line says forecast
    for f in TEL.glob("*.ibt"):
        f.unlink()
    for dd in (LIB / "reviews").glob("*"):
        shutil.rmtree(dd, ignore_errors=True)
    F.make(RACE, LAPS, session="Race", subsession=SUB, start_date=1_794_340_000, wetness=1)
    app.cmd_review_local(app.load_settings(), open_after=False)
    refs = [ref_lap("T1", "alex", "Alex", 97.9, 22.0, wet=0.0), ref_lap("T2", "bo", "Bo", 97.6, 45.0)]
    page, _ = upgrade(StandIn(refs))
    d = load(page)
    c = d.get("conditions") or {}
    check(c.get("track_temp_forecast") is True and c.get("track_temp_c") == 22.2, f"the forecast is marked as such ({c})")
    why = ((d.get("depth") or {}).get("ref_match") or {}).get("why") or ""
    check("forecast track temperature" in why and not (d.get("depth") or {}).get("gap"),
          f"the why line says forecast and no gap line is shown ({why})")
    earlier = [_race(t, 98.0 + 0.05 * (t - 20)) for t in (20, 22, 24, 27, 29, 30, 33, 38)]
    dd = _data()
    dd["ref_match"]["mine"]["track_temp_forecast"] = True
    check(depth.gap_note(dd, earlier) is None and depth.gap_note(_data(), earlier) is not None,
          "gap_note refuses a forecast temperature of his")
    fc = [dict(_race(t, 98.0 + 0.05 * (t - 20)), conditions={"track_temp_c": t, "wetness": 0.0, "track_temp_forecast": True})
          for t in (20, 22, 24, 27, 29, 30, 33, 38)]
    check(depth.temp_sensitivity(_race(36.0, 98.5), fc) is None, "forecast temperatures don't make a temperature link")
    fresh_pc_review()
    d = load(app.cmd_review_local(app.load_settings(), open_after=False, rebuild=True))
    check(not (d.get("conditions") or {}).get("track_temp_forecast"), "measured channels are not marked as forecast")


# ================================================================ 5. the page
def test_page(pc_page, g61_page, after):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  SKIPPED: Playwright not installed")
        return
    gap = {"gap_s": 0.8, "conditions_s": 0.4, "rest_s": 0.4, "fuel": "unknown", "track_c": [36, 28], "n": 6, "rho": 0.9,
           "sentence": "Of the 0.80 s between your best lap and Alex's, about 0.40 s may be the conditions (a hint)."}
    d = json.loads(json.dumps(after))
    d["depth"]["gap"] = gap
    d["meta"]["refs_checked"] = "garage61"
    from debrief import report
    out = TMP / "gap-page" / "reviews" / "2026-11-10_zolder_x"
    out.mkdir(parents=True)
    report.write(out, d)
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception:  # noqa: BLE001
            print("  SKIPPED: Chromium not available")
            return
        try:
            for label, page_path in (("upgraded PC review", Path(pc_page)), ("review with a gap line", out / "index.html"),
                                     ("Garage 61 review", Path(g61_page))):
                pg = b.new_page(viewport={"width": 1400, "height": 900})
                errs = []
                pg.on("pageerror", lambda e: errs.append(str(e)))
                pg.on("console", lambda msg: errs.append(msg.text) if msg.type == "error" else None)
                pg.goto(page_path.as_uri())
                pg.wait_for_timeout(1500)
                body = pg.text_content("body")
                depth_txt = pg.text_content("#depthBody") or ""
                check(not errs, f"{label}: draws with no script errors {errs[:2]}")
                if label == "upgraded PC review":
                    check("Similar conditions to your race: track 31 C vs your 33 C" in depth_txt,
                          "the page says why the comparison lap was chosen")
                    check("none of your own laps were downloaded" in body and "come from Garage 61" in body,
                          "the source note says which laps came from where")
                    check("Garage 61 flagged" not in body and "iRacing flagged" in body or "flagged" not in body,
                          "your laps are still described as iRacing's, not Garage 61's")
                    check("may be the conditions" not in body, "no gap line without a defensible link")
                elif label == "review with a gap line":
                    check("may be the conditions" in depth_txt and "not a measurement" in depth_txt,
                          "the gap line and its caveat are drawn")
                else:
                    check("Similar conditions to your race: track 29 C vs your 30 C" in depth_txt,
                          "a Garage 61 review shows the reason too")
                pg.close()
        finally:
            b.close()


if __name__ == "__main__":
    pc_page, after, api = test_both_sources()
    test_picks_by_conditions(after)
    test_unknown_keeps_old(StandIn)
    test_lap_conditions_read()
    test_gap_line()
    test_rebuild_keeps_the_comparison_laps()
    test_a_failed_fetch_is_not_no_comparison_laps()
    test_the_lap_list_fallback_is_capped_and_honest()
    test_mostly_dry_is_dry_and_forecast_is_not_measured()
    g61 = test_garage61_review_unchanged()
    # the PC review again, with comparison laps, for the page checks
    fresh_pc_review()
    pc_page, _ = upgrade(StandIn([ref_lap("T1", "alex", "Alex", 97.9, 31.0), ref_lap("T2", "bo", "Bo", 97.6, 45.0)]))
    test_page(pc_page, g61, load(pc_page))
    print("all passed" if not FAILS else f"{len(FAILS)} failed")
    sys.exit(1 if FAILS else 0)
