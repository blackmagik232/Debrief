"""2.7.5: results file import, the RaceCap link, rival laps, Resolve markers. Run: python tests/test_275.py"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="debrief-275-"))

from debrief import engine, markers, racecap_link as rl, results, results_csv as rc  # noqa: E402

HEAD = '"Start Time","Track"\n"2026-10-07 19:00","Test Track"\n\n"Fin Pos","Car Class ID","Car Class","Cust ID","Name","Start Pos","Car #","Out","Laps Led","Inc","Old iRating","New iRating","Old License Sub-Level","New License Sub-Level","Session Name"\n'
ROWS = ['"1","1","GT3","1","A Fast","2","1","Running","6","0","2000","2050","399","401","RACE"',
        '"2","2","GT4","2","S Michael Williams2","5","7","Running","1","4","1500","1490","250","240","RACE"',
        '"3","1","GT3","3","C Slow","1","3","Disconnected","0","8","-1","-1","100","90","RACE"',
        '"4","2","GT4","4","D Other","4","9","Running","0","2","1400","1410","300","310","RACE"',
        '"0","2","GT4","2","S Michael Williams2","9","7","Running","0","0","1","1","1","1","QUALIFY"']


def csv_text():
    return HEAD + "\n".join(ROWS) + "\n"


def test_csv_positions_are_in_class_and_fields_validate():
    p = rc.parse(csv_text().encode("utf-8"))
    assert p["multiclass"] and len(p["drivers"]) == 4
    me = rc.pick(p, "S Michael Williams")
    f = me["fields"]
    assert f["finish_pos"] == 1 and f["start_pos"] == 2 and f["field_size"] == 2 and f["incidents"] == 4
    assert f["sr_before"] == 2.5 and f["irating_after"] == 1490 and f["finish_status"] == "running"
    assert results.validate(f)["positions_gained"] == 1
    slow = next(d for d in p["drivers"] if d["name"] == "C Slow")["fields"]
    assert "irating_before" not in slow and slow["finish_status"] == "disconnected"


def test_csv_refuses_other_files():
    assert rc._int("1e999") is None
    for bad in ("a,b\n1,2\n", HEAD):
        try:
            rc.parse(bad)
        except rc.ResultsFileError:
            continue
        raise AssertionError(bad)
    assert rc.pick(rc.parse(csv_text()), "Nobody Here") is None


def car(n, name, grid, fin):
    return {"number": n, "name": name, "grid": grid, "finish": fin, "car_idx": int(n),
            "line_crossings": {str(k): 100.0 + 90 * k for k in range(7)}, "lap_times": {str(k): 90.0 for k in range(1, 7)}}


def scan():
    a, b, me = {"number": "1", "name": "A Fast"}, {"number": "3", "name": "C Slow2"}, {"number": "7", "name": "S Michael Williams"}
    return {"racecap_scan": 6, "subsession": 555, "track": "Test Track", "green": 100.0, "start_type": "rolling", "laps": 6,
            "cars": [car("1", "A Fast", 1, 1), car("7", "S Michael Williams", 8, 2), car("3", "C Slow2", 2, 3)],
            "order_by_lap": {"1": ["1", "3", "7"], "2": ["1", "7", "3"], "6": ["1", "7", "3"]},
            "passes": [{"time": 200.0, "passer": me, "passed": b, "for_position": 2, "how": "on track"},
                       {"time": 205.0, "passer": b, "passed": me, "for_position": 2, "how": "on track"},
                       {"time": 300.0, "passer": me, "passed": b, "for_position": 2, "how": "on track"},
                       {"time": 400.0, "passer": a, "passed": b, "for_position": 1, "how": "on track"}],
            "battles": [{"from": 195.0, "to": 310.0, "seconds": 115.0, "for_position": 2, "ahead": b, "behind": me, "closest_gap_s": 0.11, "passes": [200.0, 300.0]}],
            "incidents": [{"from": 410.0, "to": 415.0, "lap": 4, "level": "major", "places_lost": 4, "cars": [
                dict(me, what={"contact": 3}, places_lost=4, out_of_race=False), dict(b, what={"spin": 3}, places_lost=0, out_of_race=False)]},
                {"from": 450.0, "to": 451.0, "lap": 4, "level": "trivial", "cars": [dict(me, what={"off track": 1})]}],
            "pit_stops": [{"car": me, "in": 500.0, "out": 530.0, "lap": 5, "seconds": 30.0}]}


def test_build_is_factual_names_both_drivers_and_labels_the_road():
    s = scan()
    r = rl.build(s, s["cars"][1], 555, None, {"traffic": [3], "traffic_possible": [6]}, {"laps": [{"n": n + 2, "t": 90.0} for n in range(1, 7)]})
    assert r["lap_map"]["5"] == 3 and r["laps"][1]["review_lap"] == 4
    assert r["start_type"] == "rolling" and not r["official"] and r["pos_after_lap1"] is None   # no readings given here
    first, second, third = r["passes"]
    assert first["made"] and not first["stuck"] and not second["made"] and third["stuck"]
    assert len(r["incidents"]) == 1 and r["incidents"][0]["others"] == ["C Slow"]
    titles = [x["title"] for x in r["stories"]]
    assert any(t.startswith("You and C Slow") for t in titles) and "P8 to P2" in titles
    assert "on the road" in json.dumps(r["stories"])
    tc = {t["review_lap"]: t for t in r["traffic_check"]}
    assert tc[3]["lap"] == 1 and tc[6]["lap"] == 4


def test_positions_come_from_iracing_not_the_partial_lap_order():
    from debrief import race_analysis as ra
    s = scan()
    me = s["cars"][1]
    samples = {"idxs": [1, 7, 3], "samples": [[t, 4, None, None, None, [1, 2 if t > 300 else 9, 3], None, None] for t in range(100, 800, 5)]}
    got = {x["lap"]: x["pos"] for x in ra.positions(samples, me)}
    assert got[1] == 9 and got[3] == 2      # settles after the line; not the order_by_lap list
    r = rl.build(s, me, 555, None, None, {"laps": []}, samples)
    assert r["pos_after_lap1"] == 9 and r["laps"][2]["pos"] == 2 and r["laps"][2]["ahead"] == "A Fast" and r["laps"][2]["gap_ahead"] == 0.0
    assert all(p["for_position"] is None for p in r["passes"])
    assert r["pit_cost"]["total_s"] == 30.0 and isinstance(r["pits"], list)


def test_clear_air_compare_stops_at_a_long_stop():
    from debrief import race_analysis as ra
    assert ra.align_laps([{"n": 3, "t": 90.0}, {"n": 4, "t": 91.0}, {"n": 5, "t": 80.0}], {"lap_times": {"1": 90.01, "2": 90.99, "7": 80.02}}) == {3: 1, 4: 2, 5: 7}
    assert ra._corner_context([{"id": "C1", "avail": 0.2, "per_lap": {"1": {"t": 10.0}, "2": {"t": 10.0}, "3": {"t": 10.5}, "4": {"t": 10.5}}}], {1: 1, 2: 2, 3: 3, 4: 4},
                              {1: "clear", 2: "clear", 3: "fight", 4: "fight"})["C1"]["kind"] == "racecraft"


def test_scan_finish_within_class_when_class_is_known():
    s = scan()
    for c, k in zip(s["cars"], (1, 2, 2)):
        c["car_class"] = k
    r = rl.build(s, s["cars"][1], 555, None, None, {"laps": []})
    assert r["class_finish"] == 1 and r["class_size"] == 2 and r["finish"] == 2
    no = rl.build(scan(), scan()["cars"][1], 555, None, None, {"laps": []})
    assert no["class_finish"] is None and "in class" not in no["verdict"]
    for c in s["cars"]:
        c["grid"] = None
    r = rl.build(s, s["cars"][1], 555, None, None, {"laps": []})
    assert "Finished P1 in class, P2 overall" in r["verdict"]


def test_official_results_win_over_the_road():
    s = scan()
    r = rl.build(s, s["cars"][1], 555, {"finish": ["7", "1"], "start": {"7": 4}})
    assert r["finish"] == 1 and r["grid"] == 4 and r["official"]


def library():
    lib = TMP / "lib"
    d = lib / "Races" / "555_test-track"
    (d / "scan").mkdir(parents=True, exist_ok=True)
    (d / "scan" / "555_test-track.scan.json").write_text(json.dumps(scan()), encoding="utf-8")
    (lib / "config.yaml").write_text("obs: {password: SECRET}\n", encoding="utf-8")
    return lib, d


def test_find_needs_garage61_to_agree_or_a_typed_id():
    lib, d = library()
    asked = []

    class Api:
        def event_for_subsession(self, sub):
            asked.append(sub)
            return "EV1"
    found, why = rl.prepare(lib, "EV1", "Test Track", "", "S Michael Williams", Api())
    assert found and found["dir"] == d and asked == ["555"] and why is None
    found, why = rl.prepare(lib, "OTHER", "Test Track", "", "S Michael Williams", Api())
    assert found is None and "Race ID" in why or "race ID" in why
    found, why = rl.prepare(lib, "OTHER", "Test Track", "", "S Michael Williams", None, subsession="555")
    assert found
    found, why = rl.prepare(lib, "OTHER", "Elsewhere Ring", "", "S Michael Williams", None, subsession="555")
    assert found is None and "not this track" in why
    found, why = rl.prepare(lib, "EV1", "Test Track", "", "Nobody Here", Api())
    assert found is None and "Couldn't find" in why
    assert rl.prepare(lib, "EV1", "Test Track", "", "Swazi Williams", Api())[0]   # same family name and first letter
    assert rl.prepare(lib, "EV1", "Test Track", "", "Bob Williams", Api())[0] is None


def test_no_folder_does_nothing():
    assert rl.races_root("") is None and rl.races_root(TMP / "nothing") is None


def trace(name="R Ival", car_no="9", t=88.0):
    n = 2000
    return {"car": car_no, "name": name, "mode": "clean", "source": "iracing", "lap_time": t,
            "dist": [i / n for i in range(n)], "t": [t * i / n for i in range(n)], "speed": [150.0 + 30 * ((i // 200) % 2) for i in range(n)],
            "gear": [4] * n, "rpm": [6000] * n, "steer": [5.0] * n, "throttle": None, "brake": None}


def test_rival_trace_reads_as_telemetry_without_pedals():
    _lib, d = library()
    (d / "laps").mkdir(exist_ok=True)
    (d / "laps" / "9_clean.json").write_text(json.dumps(trace()), encoding="utf-8")
    (d / "laps" / "7_clean.json").write_text(json.dumps(trace("Me", "7")), encoding="utf-8")
    laps = rl.rival_laps(d, "7")
    assert len(laps) == 1 and "no pedal data" in laps[0][0]["_role"] and not laps[0][0]["_main"]
    raw = engine.parse_csv(laps[0][1])
    assert engine.pedals_missing(raw) and abs(raw["v"][0] - 150 / 3.6) < 0.01 and "Gear" not in raw["_missing"]


def test_rival_lap_goes_through_the_real_analysis_and_page_data():
    import csv
    import io
    sys.path.insert(0, str(ROOT / "tests"))
    import fixtures_tracks as ft
    from debrief import report
    meta, laps, tel = ft.race(sorted(ft.SHAPES)[0] if hasattr(ft, "SHAPES") else "street")
    lid = laps[2]["id"] if "id" in laps[2] else laps[2]["lapId"]
    rows = list(csv.DictReader(io.StringIO(tel[lid])))
    tr = {"car": "9", "name": "R Ival4", "mode": "clean", "source": "iracing", "lap_time": float(laps[2].get("lapTime") or laps[2].get("lap_time")) - 0.2,
          "dist": [float(r["LapDistPct"]) for r in rows], "speed": [float(r["Speed"]) * 3.6 for r in rows], "gear": [float(r.get("Gear") or 3) for r in rows],
          "rpm": [float(r.get("RPM") or 5000) for r in rows], "steer": [0.0 for _ in rows], "throttle": None, "brake": None}
    lap = {"id": "racecap:9:clean", "lapTime": tr["lap_time"], "clean": True, "driver_name": "R Ival", "_main": False, "_rival": True, "_role": "Rival"}
    d = engine.analyze(meta, laps, tel, [lap], {lap["id"]: rl.trace_csv(tr)}, log=lambda m: None)
    assert [r["name"] for r in d["refs"]] == ["R Ival"] and not d["refs"][0]["main"] and d["refs"][0]["rival"]
    assert d["traces"]["ref0"]["no_pedals"]
    base = engine.analyze(meta, laps, tel, [], {}, log=lambda m: None)
    assert all(c.get("ref_gap") is None for c in d["corners"]) and d["median"] == base["median"]
    from debrief import insights
    a, b = insights.build(d), insights.build(base)
    assert a["time_to_find"] == b["time_to_find"] and "R Ival" not in json.dumps(a)
    out = TMP / "rival"
    out.mkdir()
    report.write(out, d)
    json.loads((out / "review.json").read_text(encoding="utf-8"))


def test_digest_post_button_can_show_on_season():
    js = (ROOT / "debrief/desktop/ui/app.js").read_text(encoding="utf-8")
    assert "(v==='season'&&discordSaved)" in js
    # 2.18: on a review it shows even without a webhook (it then explains how to set one up)
    assert "$('fbDiscord').hidden=v!=='review'&&!(v==='season'&&discordSaved);" in js


def test_markers_exact_and_approximate():
    s = scan()
    d = {"meta": {"track": "Test Track"}, "racecap": rl.build(s, s["cars"][1], 555)}
    text = markers.edl(d)
    assert markers.items(d)[1] and "Passed C Slow" in text and "01:01:40:00" in text and "ResolveColor" in text
    d2 = {"meta": {"track": "T|x"}, "laps": [{"n": 1, "t": 90.0}, {"n": 2, "t": 90.0}],
          "racecraft": {"timeline": [{"n": 2, "tags": ["best"]}]}}
    assert not markers.items(d2)[1] and "approximate" in markers.edl(d2) and "01:01:30:00" in markers.edl(d2)


if __name__ == "__main__":
    fails = 0
    for k, fn in list(globals().items()):
        if k.startswith("test_"):
            try:
                fn()
                print("ok", k)
            except Exception:  # noqa: BLE001
                import traceback
                fails += 1
                traceback.print_exc()
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(1 if fails else 0)
