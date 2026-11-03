"""2.16: start / lap-1 report (debrief/startreport.py). Run: python tests/test_216_start.py

Fake races are built here: every car's progress round the lap is scripted second by second, turned into the replay
scan's raw readings (the same columns replay_scan.collect saves) and run through replay_scan.analyse, so the start
report reads a real scan. Fake telemetry for the same race is written with ibt.write_fake. Also holds the builders
test_216_incidents.py uses.
"""
from __future__ import annotations

import gzip
import json
import math
import random
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.modules.setdefault("irsdk", None)       # Windows CI has pyirsdk: nothing here may talk to iRacing
TMP = Path(tempfile.mkdtemp(prefix="debrief-216-start-"))

import fake_ibt  # noqa: E402
from debrief import ibt, replay_scan  # noqa: E402
from debrief import incidentmap as im  # noqa: E402
from debrief import startreport as sr  # noqa: E402

L_M = 4000.0
GREEN = 60.0
LAP_S = 100.0
SUB = 91000001
TRACK = "Test Raceway"
CORNERS = [{"id": "C1", "start_m": 200.0, "end_m": 600.0}, {"id": "C2", "start_m": 900.0, "end_m": 1300.0},
           {"id": "C3", "start_m": 1700.0, "end_m": 2200.0}, {"id": "C4", "start_m": 2600.0, "end_m": 3100.0},
           {"id": "C5", "start_m": 3300.0, "end_m": 3700.0}]
ME = 3              # fake_ibt.session_text says DriverCarIdx: 3


# ---------------------------------------------------------------- fake race builders (also used by test_216_incidents)
def progress(n=10, pace=None, step=0.5, t_end=GREEN + 3 * LAP_S + 30, gap=0.003):
    """{car_idx: list of progress (laps, 0 = the line)} and the time list. Car idx k starts k-th on the grid,
    gap laps apart, behind the line. pace: {car_idx: [(t0, t1, share of normal speed)]}."""
    times = list(np.arange(0.0, t_end, step))
    prog = {}
    for c in range(n):
        p = -0.01 - gap * c
        out = []
        for t in times:
            if t > GREEN:
                f = 1.0
                for t0, t1, share in (pace or {}).get(c, ()):
                    if t0 <= t < t1:
                        f = share
                p += step / LAP_S * f
            out.append(p)
        prog[c] = out
    return prog, times


def raw_samples(prog, times, view_m=None, offs=None, classes=None, names=None, pits=None):
    """The scan's raw readings: [t, state, pct, surface, pit, position, laps completed, flags] per reading."""
    n = len(prog)
    leader_done = next((t for k, t in enumerate(times) if max(prog[c][k] for c in prog) >= 3.0), None)
    rows = []
    for k, t in enumerate(times):
        state = 4 if t > GREEN else 3
        if leader_done is not None and t >= leader_done:
            state = 5
        order = sorted(range(n), key=lambda c: -prog[c][k])
        pos = [order.index(c) + 1 if t > GREEN else 0 for c in range(n)]
        pct, surf, pit = [], [], []
        for c in range(n):
            p = prog[c][k]
            far = view_m is not None and c != ME and abs(im._wrapped(p % 1.0, prog[ME][k] % 1.0)) * L_M > view_m
            off = any(t0 <= t < t1 for t0, t1 in (offs or {}).get(c, ()))
            inpit = any(t0 <= t < t1 for t0, t1 in (pits or {}).get(c, ()))
            pct.append(-1.0 if far else round(p % 1.0, 5))
            surf.append(-1 if far else (0 if off else (1 if inpit else 3)))
            pit.append(1 if inpit else 0)
        lapc = [int(math.floor(prog[c][k])) if prog[c][k] >= 0 else -1 for c in range(n)]
        rows.append([round(t, 4), state, pct, surf, pit, pos, lapc, [0] * n])
    drivers = {str(c): {"number": str(10 + c), "name": (names or {}).get(c) or ("Swazi Williams" if c == ME else f"Driver {chr(65 + c)}"),
                        "team": "", "car_class": (classes or {}).get(c), "car": "car", "pace_car": False, "spectator": False}
               for c in range(n)}
    info = {"subsession": SUB, "track": TRACK, "track_length_m": L_M, "sessions": [{"num": 2, "type": "Race", "name": "RACE"}],
            "drivers": drivers}
    return {"scan_version": 6, "info": info, "idxs": list(range(n)), "session_num": 2, "speed": 16,
            "live_time": [None, None], "notes": [], "samples": rows}


def scan_of(raw, classes=None):
    scan = replay_scan.analyse(raw, {})
    for c in scan["cars"]:
        c["car_class"] = (classes or {}).get(c["car_idx"])
    return scan


def me_car(scan):
    return next(c for c in scan["cars"] if c["car_idx"] == ME)


def save_scan(lib: Path, raw, scan, event_id=None) -> Path:
    return replay_scan.save(lib, raw, scan, event_id)


def save_review(lib: Path, name, started_at, result=None, event_id="EV1", corners=CORNERS, session="Race", track="Test Raceway (Grand Prix)"):
    d = lib / "reviews" / name
    d.mkdir(parents=True, exist_ok=True)
    data = {"meta": {"driver": "Swazi Williams", "track": track, "event_id": event_id, "started_at": started_at,
                     "session": session}, "corners": corners, "L": L_M, "laps": []}
    if result:
        data["result"] = result
    (d / "review.json").write_text(json.dumps(data), encoding="utf-8")
    return d


def make_ibt(path, prog_me, t_end, sessions=("Practice", "Qualify", "Race"), sess_num=2, green=GREEN, incidents=(), offs=(),
             spins=(), positions=(), subsession=SUB, multiclass=False, rate=20, inc_start=0, t_start=0.0, alongside=(),
             start_date=1_790_000_000, track=TRACK):
    """A telemetry file of your car. prog_me(t) -> laps (0 = the line). incidents: [(t, points)]. positions:
    [(t, overall, class)] from t on. offs/spins/alongside: [(t0, t1)]."""
    ts = np.arange(t_start, t_end, 1.0 / rate)
    n = len(ts)
    prog = np.array([prog_me(t) for t in ts])
    lap = np.where(prog >= 0, np.floor(prog) + 1, 0).astype(np.int32)
    pct = (prog % 1.0).astype(np.float32)
    inc = np.full(n, inc_start, np.int32)
    for t, d in incidents:
        inc[ts >= t] += d
    surf = np.full(n, 3, np.int32)
    for t0, t1 in offs:
        surf[(ts >= t0) & (ts < t1)] = 0
    yaw = np.zeros(n, np.float32)
    for t0, t1 in spins:
        yaw[(ts >= t0) & (ts < t1)] = 4.0
    side = np.ones(n, np.int32)
    for t0, t1 in alongside:
        side[(ts >= t0) & (ts < t1)] = 2
    pos = np.zeros(n, np.int32)
    cpos = np.zeros(n, np.int32)
    for t, a, b in positions:
        pos[ts >= t] = a
        cpos[ts >= t] = b
    state = np.where(ts > green, 4, 3).astype(np.int32) if green is not None else np.full(n, 4, np.int32)
    cols = {"SessionNum": ("i", np.full(n, sess_num, np.int32)), "SessionState": ("i", state), "SessionTime": ("d", ts),
            "Lap": ("i", lap), "LapDistPct": ("f", pct), "LapDist": ("f", pct * L_M), "PlayerCarMyIncidentCount": ("i", inc),
            "PlayerTrackSurface": ("i", surf), "Speed": ("f", np.full(n, 40.0, np.float32)), "YawRate": ("f", yaw),
            "PlayerCarPosition": ("i", pos), "PlayerCarClassPosition": ("i", cpos), "OnPitRoad": ("b", np.zeros(n, bool)),
            "IsOnTrack": ("b", np.ones(n, bool)), "CarLeftRight": ("i", side),
            "LapLastLapTime": ("f", np.full(n, -1.0, np.float32))}
    text = fake_ibt.session_text(session=list(sessions), subsession=subsession, track=track, track_code="testraceway")
    if not multiclass:
        text = text.replace("CarClassShortName: LMP3", "CarClassShortName: GT4 Class")
    ibt.write_fake(path, text, cols, tick_rate=rate, start_date=start_date)
    return path


def prog_fn(prog, times, car=ME):
    arr = np.array(prog[car])
    tt = np.array(times)
    return lambda t: float(np.interp(t, tt, arr))


def outing_for(scan, raw, zs=None, **extra):
    """An outing dict as incidentmap.outings makes one, from a scan only."""
    o = {"id": f"{SUB}:scan", "kind": "race", "track": TRACK, "subsession": SUB, "when": 1.0, "files": [], "events": [],
         "scan": {"scan": scan, "samples": raw, "official": None}, "me_car": me_car(scan), "classes": {}, "multiclass": False,
         "L": L_M, "laps": scan.get("laps")}
    o.update(extra)
    o["_zones"] = zs or im.zones(CORNERS, L_M)
    o["_events"] = im.outing_events(o)
    return o


# ---------------------------------------------------------------- the scripted race used below
# Cars 4 and 5 start right behind you (P5, P6) and go faster from the green to Turn 1: both pass you there.
# Car 1 (P2) slows right down in Turn 3: you (and everyone) pass it there.
PACE = {4: [(GREEN, GREEN + 12, 1.15)], 5: [(GREEN, GREEN + 12, 1.15)], 1: [(GREEN + 38, GREEN + 48, 0.3)]}


def test_lap1_corner_by_corner():
    prog, times = progress(pace=PACE)
    raw = raw_samples(prog, times)
    scan = scan_of(raw)
    zs = im.zones(CORNERS, L_M)
    got = sr.lap1_scan(scan, raw, me_car(scan), zs)
    assert got is not None
    seg = {s["key"]: s for s in got["segments"]}
    assert seg["C1"]["label"] == "the start to Turn 1", seg["C1"]
    assert (seg["C1"]["lost"], seg["C1"]["gained"]) == (2, 0), seg["C1"]
    assert (seg["C3"]["gained"], seg["C3"]["lost"]) == (1, 0), seg["C3"]
    assert all(s["net"] == 0 for k, s in seg.items() if k not in ("C1", "C3")), got
    assert list(seg) == ["C1", "C2", "C3", "C4", "C5", "line"], list(seg)
    o = outing_for(scan, raw)
    row = sr.race_row(o)
    assert row["start"] == 4 and row["start_from"] == "replay", row
    assert row["lap1_pos"] == 5 and row["change"] == -1 and row["seen_net"] == -1, row
    assert [s["est_pos"] for s in row["corners"]][:3] == [6, 6, 5], row["corners"]
    text = " ".join(row["sentences"])
    assert "lost 1 place" in text and "lost 2 between the start and Turn 1" in text and "gained 1 at Turn 3" in text, text
    json.dumps(row)


def test_cars_out_of_view_are_not_counted_and_the_gap_is_said():
    prog, times = progress(pace=PACE)
    raw = raw_samples(prog, times, view_m=100.0)      # only cars within 100 m readable: car 1 drops out of view in Turn 3
    scan = scan_of(raw)
    row = sr.race_row(outing_for(scan, raw))
    seg = {s["key"]: s for s in row["corners"]}
    assert seg["C3"]["gained"] == 0, seg["C3"]
    assert row["change"] == -1 and row["seen_net"] == -2, row
    assert any("only shows cars near you" in s for s in row["sentences"]), row["sentences"]


def test_other_class_cars_do_not_count_in_a_multiclass_race():
    classes = {c: ("GT4" if c >= 2 else "LMP3") for c in range(10)}
    pace = dict(PACE)
    pace[1] = [(GREEN + 38, GREEN + 48, 0.3)]       # an LMP3 car slowing in Turn 3: not a place in your class
    prog, times = progress(pace=pace)
    raw = raw_samples(prog, times, classes=classes)
    scan = scan_of(raw, classes)
    o = outing_for(scan, raw, classes={str(k): v for k, v in classes.items()}, multiclass=True)
    row = sr.race_row(o)
    seg = {s["key"]: s for s in row["corners"]}
    assert seg["C3"]["gained"] == 0 and seg["C1"]["lost"] == 2, row["corners"]
    assert row["in_class"] and row["start"] == 2 and row["lap1_pos"] == 4, row       # P4 overall, 2nd GT4
    assert "in class" in row["sentences"][0], row["sentences"]


def test_start_position_sources_in_order():
    prog, times = progress(pace=PACE)
    raw = raw_samples(prog, times)
    scan = scan_of(raw)
    o = outing_for(scan, raw, review={"result": {"start_pos": 7, "finish_pos": 3}})
    assert sr.race_row(o)["start"] == 7 and sr.race_row(o)["start_from"] == "official"
    o = outing_for(scan, raw, start_pos=9, start_class_pos=9)
    r = sr.race_row(o)
    assert r["start"] == 9 and r["start_from"] == "telemetry" and "telemetry" in r["start_words"], r
    o = outing_for(scan, raw)
    assert sr.race_row(o)["start_from"] == "replay"
    bare = dict(raw, samples=[[r_[0], r_[1], r_[2], r_[3], r_[4], [0] * 10, r_[6], r_[7]] for r_ in raw["samples"]])
    o = outing_for(scan, bare)
    r = sr.race_row(o)
    assert r["start_from"] in ("estimate", None) and r["lap1_pos"] is None, r
    if r["start_from"] == "estimate":
        assert "guess" in r["start_words"]


def test_bands():
    assert sr.band(1, 30)["key"] == "front" and sr.band(10, 30)["key"] == "front" and sr.band(11, 30)["key"] == "middle"
    assert sr.band(21, 30)["key"] == "back" and sr.band(30, 30)["rule"] == "thirds"
    assert sr.band(5, None) == {"key": "front", "rule": "fixed", "label": "P1 to P5"}
    assert sr.band(6, 4)["key"] == "middle" and sr.band(16, None)["key"] == "back"
    assert sr.band(None, 20) is None and sr.band(0, 20) is None


def _row(i, change, start=10, field=30, segs=None, track=TRACK):
    corners = [{"key": k, "label": ("the start to Turn 1" if k == "C1" else im.corner_label(k)), "gained": max(v, 0),
                "lost": max(-v, 0), "net": v} for k, v in (segs or {}).items()]
    return {"id": f"r{i}", "ok": True, "track": track, "when": 1000.0 + i, "change": change, "start": start,
            "lap1_pos": start - change if change is not None else None, "band": sr.band(start, field), "corners": corners}


def test_one_or_two_races_only_describe():
    rep = sr.report([_row(0, -3, segs={"C1": -3}), _row(1, -2, segs={"C1": -2})])
    g = rep["tracks"][0]
    assert not any(c["pattern"] for c in g["corners"]) and not any(b["pattern"] for b in g["bands"])
    text = " ".join(g["sentences"])
    assert "only says what happened" in text and "habit, not chance" not in text, text
    assert "In 2 of 2 races at Test Raceway you lost places between the start and Turn 1 on lap 1." in text, text


def test_a_real_habit_is_found():
    rows = [_row(i, -2 - (i % 2), segs={"C1": -2 - (i % 2), "C2": (1 if i % 3 == 0 else (-1 if i % 3 == 1 else 0))}) for i in range(7)]
    rep = sr.report(rows)
    g = rep["tracks"][0]
    c1 = next(c for c in g["corners"] if c["key"] == "C1")
    assert c1["pattern"] == "lose" and c1["p"] < 0.05 and c1["lost"] == 7, c1
    assert not next(c for c in g["corners"] if c["key"] == "C2")["pattern"]
    text = " ".join(g["sentences"])
    assert "you usually lose places between the start and Turn 1 on lap 1" in text, text
    assert "In 7 of 7 races at Test Raceway you lost places between the start and Turn 1" in text, text
    assert any(b["band"] == "all" and b["pattern"] == "lose" for b in g["bands"]), g["bands"]


def test_two_lucky_races_never_make_a_habit_even_when_strong():
    rows = [_row(i, -8, segs={"C1": -8}) for i in range(2)] + [_row(5, 0, segs={"C1": 0})]
    rep = sr.report(rows)
    assert not any(c["pattern"] for c in rep["tracks"][0]["corners"])


def test_trend_needs_five_races_and_is_found_when_clear():
    rows = [_row(i, i - 4) for i in range(4)]
    assert sr.report(rows)["overall"]["trend"]["p"] is None
    rows = [_row(i, i - 4) for i in range(9)]
    tr = sr.report(rows)["overall"]["trend"]
    assert tr["direction"] == "better" and tr["p"] < 0.05, tr
    assert any("getting better" in s for s in sr.report(rows)["overall"]["sentences"])


def test_false_positive_rate_on_random_data():
    """Noise only: each race's changes are as likely + as -. Any habit claim at all counts as a false positive."""
    rng = random.Random(2160)
    datasets, hits, trend_hits, trend_sets = 400, 0, 0, 0
    for _ in range(datasets):
        n = rng.randint(3, 20)
        rows = []
        for i in range(n):
            segs = {k: rng.choice([-2, -1, -1, 0, 0, 0, 1, 1, 2]) for k in ("C1", "C2", "C3", "C4", "line")}
            change = rng.choice([-5, -3, -2, -1, 0, 1, 2, 3, 5])
            rows.append(_row(i, change, start=rng.randint(1, 30), field=30, segs=segs))
        g = sr.report(rows)["tracks"][0]
        if any(c["pattern"] for c in g["corners"]) or any(b["pattern"] for b in g["bands"]):
            hits += 1
        if g["trend"]["p"] is not None:
            trend_sets += 1
            trend_hits += 1 if g["trend"]["direction"] else 0
    rate = hits / datasets
    trate = trend_hits / max(trend_sets, 1)
    print(f"  start report false positives on random data: {hits}/{datasets} = {rate:.1%}; trend {trend_hits}/{trend_sets} = {trate:.1%}")
    assert rate <= 0.05, rate
    assert trate <= 0.05, trate


def test_bad_or_missing_data_never_raises():
    assert sr.lap1_scan(None, None, None, []) is None
    assert sr.lap1_scan({"green": 5}, {"idxs": [3], "samples": [[0, 1, None, None, None, None, None]]}, {"car_idx": 3}, []) is None
    r = sr.race_row({"id": "x", "scan": {"scan": {"green": "soon"}, "samples": "junk"}, "me_car": {"car_idx": "?"}})
    assert isinstance(r, dict)
    r = sr.race_row({})
    assert isinstance(r, dict)
    rep = sr.report([None, {"ok": True}, {"ok": False}, "junk"])
    assert rep["races"] == 1
    out = sr.build(library_folder=TMP / "nothing here", telemetry_folder=TMP / "nor here")
    assert out["races"] == 0 and isinstance(out["tracks"], list)
    out = sr.build(src={"scans": "junk", "telemetry": [{"sessions": "junk"}]})
    assert isinstance(out, dict)
    json.dumps(out)


def test_end_to_end_with_scan_telemetry_and_review():
    lib = TMP / "lib"
    tel = TMP / "tel"
    tel.mkdir(parents=True, exist_ok=True)
    prog, times = progress(pace=PACE)
    raw = raw_samples(prog, times)
    scan = scan_of(raw)
    save_scan(lib, raw, scan, event_id="EV-216")
    save_review(lib, "2026-11-02_test", "2026-11-02T19:00:00+00:00", event_id="EV-216")
    me_prog = prog_fn(prog, times)
    end1 = float(me_car(scan)["line_crossings"]["1"])
    make_ibt(tel / "race.ibt", me_prog, times[-1], incidents=[(GREEN + 8.0, 4)], positions=[(GREEN + 0.5, 4, 4), (end1 + 2, 5, 5)])
    t0 = time_now()
    out = sr.build(library_folder=lib, telemetry_folder=tel)
    took = time_now() - t0
    assert out["races"] == 1, out
    r = out["list"][0]
    assert r["start"] == 4 and r["start_from"] == "telemetry" and r["lap1_pos"] == 5 and r["lap1_from"] == "telemetry", r
    assert r["contact"] == "yes" and r["incident_points"] == 4, r
    assert r["incidents"][0]["type"] == "contact" and r["incidents"][0]["where"] == "Turn 1", r["incidents"]
    assert {s["key"] for s in r["corners"]} >= {"C1", "C3"}, r["corners"]
    assert r["sources"] == ["telemetry", "replay scan"], r["sources"]
    json.dumps(out)
    assert took < 10, took
    for s in [x for g in out["tracks"] + [out["overall"]] for x in g["sentences"]] + r["sentences"]:
        assert "\u2014" not in s and "\u2013" not in s and " - " not in s, s


def time_now():
    import time
    return time.perf_counter()


def test_registered_for_the_window_and_the_build():
    from debrief import desktop
    assert "startreport" in desktop.FEATURE_MODULES and "incidentmap" in desktop.FEATURE_MODULES
    spec = (ROOT / "packaging" / "debrief.spec").read_text(encoding="utf-8")
    must = spec[spec.index("for _must in"):spec.index("if _must not in")]
    assert '"debrief.startreport"' in must and '"debrief.incidentmap"' in must


if __name__ == "__main__":
    fails = 0
    for k, fn in list(globals().items()):
        if k.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok", k)
            except (Exception, SystemExit):  # noqa: BLE001
                import traceback
                fails += 1
                print("FAIL", k)
                traceback.print_exc()
    shutil.rmtree(TMP, ignore_errors=True)
    print("FAILED" if fails else "ALL OK", fails)
    sys.exit(1 if fails else 0)
