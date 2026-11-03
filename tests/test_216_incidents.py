"""2.16: incident map (debrief/incidentmap.py). Run: python tests/test_216_incidents.py

Fake telemetry with incident steps (1 off track, 2 spin, 4 contact, 5 = two at once) and fake replay scans
(test_216_start's builders) check where each incident is placed, what kind it probably was, the map per track,
the statistics, and that random incidents almost never make a "pattern" (false positive rate at most 5%).
"""
from __future__ import annotations

import json
import random
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.modules.setdefault("irsdk", None)       # Windows CI has pyirsdk: nothing here may talk to iRacing
TMP = Path(tempfile.mkdtemp(prefix="debrief-216-incidents-"))

import fake_ibt  # noqa: E402
import test_216_start as fx  # noqa: E402  (the fake race builders; its tests only run as __main__)
from debrief import incidentmap as im  # noqa: E402

GREEN, L_M = fx.GREEN, fx.L_M


def test_points_are_split_into_iracing_values():
    assert im.split_points(1) == [1] and im.split_points(2) == [2] and im.split_points(4) == [4]
    assert im.split_points(5) == [4, 1] and im.split_points(3) == [2, 1] and im.split_points(8) == [4, 4]
    assert im.split_points(6) == [4, 2] and im.split_points(0) == []


def test_zones_and_locating():
    zs = im.zones(fx.CORNERS, L_M)
    assert [z["key"] for z in zs] == ["C1", "C2", "C3", "C4", "C5"]
    assert abs(sum(z["share"] for z in zs) - 1.0) < 1e-9
    assert im.locate(0.05, zs)["key"] == "C1" and im.locate(0.95, zs)["key"] == "C1"        # Turn 1's zone crosses the line
    assert im.locate(0.2, zs)["key"] == "C2" and im.locate(0.5, zs)["key"] == "C3" and im.locate(1.4, zs)["key"] == "C3"
    assert im.locate(None, zs) is None and zs[0]["label"] == "Turn 1"
    q = im.zones(None, None)
    assert [z["key"] for z in q] == ["Q1", "Q2", "Q3", "Q4"] and im.locate(0.6, q)["label"] == "the third quarter of the lap"
    assert im.zones([{"id": "C1", "end_m": "x"}, "junk"], 4000)[0]["key"] == "Q1"
    assert im.at_zone("Turn 2") == "at Turn 2" and im.at_zone("the first quarter of the lap").startswith("in ")


def _race_file(path, **kw):
    prog, times = fx.progress(pace=fx.PACE)
    return fx.make_ibt(path, fx.prog_fn(prog, times), times[-1], **kw), prog, times


def test_telemetry_steps_are_placed_and_described():
    t_off, t_spin, t_hit, t_two = GREEN + 30.0, GREEN + 150.0, GREEN + 200.0, GREEN + 260.0
    path, prog, times = _race_file(TMP / "race.ibt", incidents=[(GREEN - 20.0, 4), (t_off + 1.5, 1), (t_spin, 2), (t_hit, 4), (t_two, 5)],
                                   offs=[(t_off, t_off + 2.0)], spins=[(t_spin - 0.5, t_spin + 0.5)], alongside=[(t_hit - 1, t_hit)],
                                   positions=[(GREEN + 0.5, 4, 4)])
    f = im.read_ibt(path)
    assert f["ok"] and f["subsession"] == fx.SUB and f["me_idx"] == 3 and not f["multiclass"], f
    S = f["sessions"][0]
    assert S["kind"] == "race" and S["green"] and S["start_pos"] == 4, S
    ev = S["events"]
    assert [e["points"] for e in ev] == [4, 1, 2, 4, 5], ev
    pre, off, spin, hit, two = ev
    assert pre["before_green"] and pre["lap"] == 0
    assert abs(off["t"] - t_off) < 0.11 and off["off"] and off["lap"] == 1, off            # placed where the wheels left
    assert abs(off["pct"] - fx.prog_fn(prog, times)(t_off) % 1.0) < 0.002, off
    assert spin["spun"] and spin["parts"] == [2] and spin["lap"] == 2, spin
    assert hit["alongside"] is True and spin["alongside"] is False
    assert two["parts"] == [4, 1] and two["at_once"], two
    assert im.telemetry_steps_seen([f]) == [1, 2, 4, 5]
    assert im.read_ibt(path) is f       # memoised


def test_practice_file_from_fake_ibt():
    p = fake_ibt.make(TMP / "practice.ibt", [99.0, 98.5, 98.7, 99.1], incidents=(1, 2), subsession=fx.SUB + 7)
    f = im.read_ibt(p)
    S = f["sessions"][0]
    assert S["kind"] == "practice" and [e["points"] for e in S["events"]] == [2, 2], S["events"]
    assert all(not e["before_green"] for e in S["events"]) and "green" not in S


def test_points_missed_between_two_files_are_counted_not_placed():
    prog, times = fx.progress(pace=fx.PACE)
    pf = fx.prog_fn(prog, times)
    a = fx.make_ibt(TMP / "a.ibt", pf, 200.0, incidents=[(GREEN + 30, 1)], start_date=1_790_000_000)
    b = fx.make_ibt(TMP / "b.ibt", pf, 360.0, t_start=260.0, inc_start=5, incidents=[(300.0, 2)], start_date=1_790_000_300)
    src = {"telemetry": [im.read_ibt(a), im.read_ibt(b)], "scans": [], "reviews": [], "corners": {}}
    outs = im.outings(src)
    assert len(outs) == 1 and outs[0]["files"] == ["a.ibt", "b.ibt"], outs
    assert outs[0]["not_recorded"] == 4 and len(outs[0]["events"]) == 2, outs[0]


def test_near_cars_and_classification_from_the_scan():
    prog, times = fx.progress(pace=fx.PACE)
    raw = fx.raw_samples(prog, times, offs={2: [(GREEN + 100.0, GREEN + 108.0)], fx.ME: [(GREEN + 102.0, GREEN + 105.0)]})
    scan = fx.scan_of(raw)
    me = fx.me_car(scan)
    # car 4 passes you on the run to Turn 1: the moment the two are level
    t_pass = next(t for k, t in enumerate(times) if t > GREEN and prog[4][k] >= prog[fx.ME][k])
    near = im.near_cars(raw, fx.ME, t_pass, L_M, im._names(scan))
    assert near["readable"] and near["cars"][0]["name"] == "Driver E" and near["cars"][0]["metres"] <= im.NEAR_M, near
    t_alone = GREEN + 300.0
    far = im.near_cars(raw, fx.ME, t_alone, L_M, im._names(scan), within_m=5.0)
    assert far["readable"] and far["cars"] == [], far
    c = im.classify({"parts": [4], "near": near})
    assert c["type"] == "contact" and c["sure"] == "sure" and c["with"] == ["Driver E"] and "probably with them" in c["why"], c
    c = im.classify({"parts": [1], "off": True, "near": far})
    assert c["type"] == "own" and c["sure"] == "probably", c
    c = im.classify({"parts": [1], "off": True, "near": {"readable": False, "cars": []}})
    assert c["type"] == "own" and c["sure"] == "possibly" and "no replay scan" in c["why"], c
    c = im.classify({"parts": [2], "spun": True, "near": near})
    assert c["type"] == "contact" and c["sure"] == "possibly" and "tap" in c["why"], c
    c = im.classify({"parts": [1], "alongside": True})
    assert c["type"] == "contact" and "spotter" in c["why"].lower(), c
    # car 2, just ahead, went off 2 s before you in the same spot: caught up
    grp = im.scan_group(scan, me["number"], GREEN + 104.0)
    assert grp and grp["first"] == "Driver C" and grp["lead_s"] >= 1.0, grp
    c = im.classify({"parts": [1], "off": True, "group": grp, "near": far})
    assert c["type"] == "caught_up" and c["sure"] == "probably" and "Driver C" in c["why"], c
    c = im.classify({"parts": [4], "slowed": True, "near": {"readable": True, "cars": [{"name": "A", "metres": 3}, {"name": "B", "metres": 6}]}})
    assert c["type"] == "caught_up" and c["sure"] == "possibly", c
    for e in (c,):
        assert "\u2014" not in e["why"] and " - " not in e["why"]


def test_scan_only_race_uses_the_scans_moments():
    prog, times = fx.progress(pace=fx.PACE)
    raw = fx.raw_samples(prog, times, offs={2: [(GREEN + 100.0, GREEN + 108.0)], fx.ME: [(GREEN + 102.0, GREEN + 106.0)]})
    scan = fx.scan_of(raw)
    o = fx.outing_for(scan, raw)
    evs = o["_events"]
    assert len(evs) == 1 and evs[0]["source"] == "scan" and evs[0]["points"] is None, evs
    assert evs[0]["class"]["type"] == "caught_up" and evs[0]["zone"] == "C1" and evs[0]["lap"] == 2, evs[0]
    m = im.track_map(fx.TRACK, [o])
    assert m["events"] == 1 and not m["points_known"] and m["by_zone"][0]["caught_up"] == 1, m
    json.dumps(m)


def _outing(i, events, laps=10, kind="race"):
    return {"id": f"s{i}", "kind": kind, "subsession": i, "when": 1000.0 + i, "laps": laps,
            "_events": [{"pct": p, "zone": None, "points": pts, "parts": im.split_points(pts), "lap": lap, "lap1": kind == "race" and lap == 1,
                         "before_green": False, "class": {"type": typ, "sure": "probably", "why": "x", "with": []}, "source": "telemetry"}
                        for p, pts, lap, typ in events]}


def _map(outs, zs):
    for o in outs:
        for e in o["_events"]:
            z = im.locate(e["pct"], zs)
            e["zone"], e["where"] = z["key"], z["label"]
        o["_zones"] = zs
    return im.track_map(fx.TRACK, outs)


def test_map_counts_and_plain_words():
    zs = im.zones(fx.CORNERS, L_M)
    outs = [_outing(0, [(0.05, 4, 1, "contact"), (0.06, 1, 3, "own"), (0.5, 2, 5, "own")]),
            _outing(1, [(0.04, 4, 1, "contact")], kind="race"), _outing(2, [(0.3, 1, 2, "own")], kind="practice")]
    m = _map(outs, zs)
    t1 = next(b for b in m["by_zone"] if b["zone"] == "C1")
    assert t1["events"] == 3 and t1["points"] == 9 and t1["lap1"] == 2 and t1["contact"] == 2 and t1["sessions"] == 2, t1
    assert m["by_session"] == {"race": 4, "qualifying": 0, "practice": 1} and m["points"] == 12
    text = " ".join(m["sentences"])
    assert "9 of your 12 incident points at Test Raceway came at Turn 1, mostly contact on lap 1." in text, text
    assert "pattern" not in text.replace("not a pattern", ""), text
    assert any(c["zone"] == "C1" and c["type"] == "contact" and c["events"] == 2 for c in m["cells"]), m["cells"]


def test_a_real_hot_corner_is_found_and_two_sessions_are_not_enough():
    zs = im.zones(fx.CORNERS, L_M)
    outs = [_outing(i, [(0.12, 4, 1 + i, "contact"), (0.6 + 0.03 * i, 1, 4, "own")]) for i in range(2)]
    m = _map(outs, zs)
    assert not m["patterns"] and "only says what happened" in " ".join(m["sentences"]), m["sentences"]
    outs = [_outing(i, [(0.12 + 0.01 * (i % 3), 4, 2 + i, "contact")]) for i in range(6)] + [_outing(9, [(0.7, 1, 3, "own")])]
    m = _map(outs, zs)
    hot = [p for p in m["patterns"] if p["kind"] == "corner"]
    assert hot and hot[0]["zone"] == "C1" and hot[0]["p"] < 0.05 and hot[0]["sessions"] == 6, m["patterns"]
    assert any("Turn 1 is a pattern" in s for s in m["sentences"])


def test_lap1_pattern():
    zs = im.zones(fx.CORNERS, L_M)
    outs = [_outing(i, [(0.1 + 0.15 * (i % 5), 4, 1, "contact")], laps=15) for i in range(5)]
    m = _map(outs, zs)
    assert any(p["kind"] == "lap1" for p in m["patterns"]), m["patterns"]


def test_false_positive_rate_on_random_incidents():
    """Incidents anywhere on the lap, on any lap: any pattern claim is a false positive. Two kinds of noise: even over
    the lap's distance (the test's own assumption) and even over the corners (corner zones of different lengths)."""
    zs = im.zones(fx.CORNERS, L_M)
    rng = random.Random(2161)
    for how in ("distance", "corner"):
        hits, datasets = 0, 300
        for _ in range(datasets):
            outs = []
            for i in range(rng.randint(3, 15)):
                laps = rng.randint(5, 25)
                evs = []
                for _k in range(rng.randint(1, 6)):
                    if how == "distance":
                        p = rng.random()
                    else:
                        z = rng.choice(zs)
                        p = (z["lo"] + rng.random() * z["share"]) % 1.0
                    evs.append((p, rng.choice([1, 2, 4]), rng.randint(1, laps), rng.choice(["own", "contact", "caught_up"])))
                outs.append(_outing(i, evs, laps=laps))
            if _map(outs, zs)["patterns"]:
                hits += 1
        rate = hits / datasets
        print(f"  incident map false positives on random data (even by {how}): {hits}/{datasets} = {rate:.1%}")
        assert rate <= 0.05, (how, rate)


def test_statistics_helpers():
    import itertools
    import math
    probs = [0.1, 0.5, 0.3, 0.7]
    exact = sum(math.prod(q if c else 1 - q for q, c in zip(probs, combo))
                for combo in itertools.product([0, 1], repeat=4) if sum(combo) >= 2)
    assert abs(im.poisson_binomial_tail(probs, 2) - exact) < 1e-12
    assert abs(im.poisson_binomial_tail(probs, 0) - 1.0) < 1e-12 and im.poisson_binomial_tail(probs, 5) == 0.0
    t = im.sign_flip([[1] * 6])[0]
    assert abs(t["p"] - 2 / 64) < 1e-4 and t["plus"] == 6, t
    t = im.sign_flip([[1] * 5])[0]
    assert t["p"] >= 0.0625 - 1e-9, t                # five races can never reach 5% two-sided
    t = im.sign_flip([[3, None, -1, 2] * 5])[0]       # 20 races: shuffled, fixed seed
    assert t == im.sign_flip([[3, None, -1, 2] * 5])[0] and 0 < t["p"] <= 1
    assert im.sign_flip([[0, 0, None]])[0]["p"] == 1.0 and im.sign_flip([]) == []
    k = im.kendall_trend(list(range(10)))
    assert k["tau"] == 1.0 and k["p"] < 0.01
    assert im.kendall_trend([1, 2, 3])["p"] is None and im.kendall_trend(["x", None])["n"] == 0


def test_bad_or_missing_data_never_raises():
    bad = TMP / "broken.ibt"
    bad.write_bytes(b"\0" * 400)
    r = im.read_ibt(bad)
    assert r["ok"] is False and str(TMP) not in r["why"], r
    assert im.read_ibt(TMP / "missing.ibt")["ok"] is False
    assert im.near_cars(None, 3, 10, 4000)["cars"] == [] and im.near_cars({"idxs": [3], "samples": "x"}, 3, 10, None)["readable"] is False
    assert im.scan_group(None, "1", 5) is None and im.scan_group({"incidents": ["x", {"from": "a"}]}, "1", 5) is None
    assert im.classify({})["type"] == "own"
    out = im.build(library_folder=TMP / "none", telemetry_folder=TMP / "none2")
    assert out["events"] == 0 and isinstance(out["tracks"], list)
    out = im.build(src={"telemetry": [{"sessions": [{"num": 1, "events": "junk"}]}], "scans": None})
    assert isinstance(out, dict) and out["notes"] is not None
    json.dumps(out)


def test_end_to_end_folders():
    lib, tel = TMP / "lib", TMP / "tel"
    tel.mkdir(parents=True, exist_ok=True)
    prog, times = fx.progress(pace=fx.PACE)
    raw = fx.raw_samples(prog, times)
    scan = fx.scan_of(raw)
    fx.save_scan(lib, raw, scan, event_id="EV-I")
    fx.save_review(lib, "2026-11-02_inc", "2026-11-02T19:00:00+00:00", event_id="EV-I")
    t_pass = next(t for k, t in enumerate(times) if t > GREEN and prog[4][k] >= prog[fx.ME][k])
    fx.make_ibt(tel / "race.ibt", fx.prog_fn(prog, times), times[-1], incidents=[(t_pass, 4), (GREEN + 300.0, 1)],
                offs=[(GREEN + 299.0, GREEN + 300.0)])
    fake_ibt.make(tel / "practice.ibt", [99.0, 98.5, 98.7], incidents=(1,), track="Test Raceway", subsession=fx.SUB - 1)
    src = im.collect(library_folder=lib, telemetry_folder=tel)
    assert len(src["scans"]) == 1 and len(src["telemetry"]) == 2 and src["corners"], src["notes"]
    m = im.build(src)
    assert len(m["tracks"]) == 1 and m["sessions"] == 2 and m["events"] == 3, m
    t = m["tracks"][0]
    lst = {(r["kind_of_session"], r["points"]): r for r in t["list"]}
    hit = lst[("race", 4)]
    assert hit["type"] == "contact" and hit["with"] == ["Driver E"] and hit["where"] == "Turn 1" and hit["lap1"], hit
    assert lst[("race", 1)]["type"] == "own" and lst[("practice", 2)]["kind_of_session"] == "practice"
    assert m["steps_seen"] == [1, 2, 4] and "1x, 2x, 4x" in m["steps_note"]
    json.dumps(m)
    for s in t["sentences"]:
        assert "\u2014" not in s and "\u2013" not in s and " - " not in s, s


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
    shutil.rmtree(fx.TMP, ignore_errors=True)
    print("FAILED" if fails else "ALL OK", fails)
    sys.exit(1 if fails else 0)
