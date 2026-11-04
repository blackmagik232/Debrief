"""Debrief 2.18 Car vs car (debrief/carcompare.py and GET api/carcompare).

Synthetic fields: lap time = track base + car effect + iRating effect (0.8 s a lap per 1000 iRating) + race-day
offset + driver noise. Checks:
  - no car effect: a "quicker" sentence in at most 5 % of fields (family-wise, three cars);
  - the car mix swinging 4 to 1 between races (the 2.18 review's scenario): no false claim beyond 5 %, and a real
    0.5 s gap recovered within 10 %; one car driven by a much more mixed bunch: no false claim beyond 5 %;
  - finishers (Running and 90 % of the winner's laps), the results file's header, Cust ID, one entry per
    subsession, the same layout only, one season by default;
  - no car effect but the faster drivers prefer car A: no false claim either (the iRating adjustment and bands);
  - a real 0.4 s car effect with enough drivers is found, and the sentence names the right car;
  - too few drivers: "not enough to tell", whatever the numbers;
  - the results file's car name and lap times are read; the window keeps the table beside the review;
  - where_faster on fake telemetry: a car that's faster at one corner shows there, two runs of the same car show
    nothing bigger than his own spread, one lap each is a rough look;
  - the server endpoint (cached, no paths, no long dashes) and the page in a browser (carcompare-light.png).

    python3 tests/test_218_carcompare.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.modules.setdefault("irsdk", None)
TMP = Path(tempfile.mkdtemp(prefix="debrief-218-"))
os.environ["DEBRIEF_SETTINGS"] = str(TMP / "settings.toml")
os.environ["DEBRIEF_CACHE_DIR"] = str(TMP / "cache")
os.environ["DEBRIEF_SCHEDULE_DIR"] = str(TMP / "schedule")
os.environ["DEBRIEF_UPDATE_STATE"] = str(TMP / "update.json")

import corner_laps as cl  # noqa: E402
from debrief import app, carcompare as CC, cornercoach, results_csv  # noqa: E402
from debrief.desktop import server as S  # noqa: E402

TRACK = "Circuit Zolder (Grand Prix)"
CARS = ("McLaren 570S GT4", "Ford Mustang GT4", "BMW M4 G82 GT4 Evo")
LIB = TMP / "lib"
IRACING = TMP / "iRacing"
TEL = IRACING / "telemetry"
ENV, PW = {}, {}


# ================================================================ synthetic fields
def field(seed, n_drivers=60, effects=None, confound=False, races=4, cars=CARS[:3], me=None):
    """Races at one track. effects: {car: seconds}. confound: the higher a driver's iRating, the likelier the first car
    (also inside each iRating band, so the bands alone can't fix it)."""
    rng = np.random.default_rng(seed)
    effects = effects or {}
    people = []
    for k in range(n_drivers):
        ir = float(np.clip(rng.lognormal(np.log(2000), 0.45), 600, 7000))
        if confound:
            p_a = 1 / (1 + np.exp(-(ir - 2200) / 400))       # the higher the iRating, the likelier car A
            car = cars[0] if rng.random() < p_a else cars[1 + int(rng.integers(len(cars) - 1))]
        else:
            car = cars[k % len(cars)]
        people.append({"name": f"Driver {k}", "car": car, "ir": ir, "skill": rng.normal(0, 0.5)})
    if me:
        people.append({"name": me, "car": cars[0], "ir": 1800.0, "skill": -0.6})
    out = []
    for r in range(races):
        day = rng.normal(0, 0.6)
        ds = []
        for p in people:
            if rng.random() < 0.4 and p["name"] != me:
                continue
            pace = 100.0 + effects.get(p["car"], 0.0) - 0.8 * (p["ir"] - 2000) / 1000 + p["skill"] + day + rng.normal(0, 0.25)
            ds.append({"name": p["name"], "car": p["car"], "class": CC.class_key(p["car"]), "irating": p["ir"],
                       "pace": pace, "best": pace - 1.2, "laps": 14, "finished": True})
        out.append({"source": "results", "id": f"{seed}-{r}", "track": TRACK, "when": "", "drivers": ds})
    return out


def LIMIT(n):
    """The most false claims a test held at 5 % can show in n fields by chance (5 % plus two standard errors). Over
    1500 fields each (scratch runs for 2.18), the false-claim rate was 4.6 % with no car effect and 5.1 % with the
    hard confounding below: the test is held at 5 %, no better and no worse."""
    return 0.05 + 2 * (0.05 * 0.95 / n) ** 0.5


def claims(races, n_perm=200, seed=0):
    return CC.compare(races, TRACK, "GT4", n_perm=n_perm, seed=seed)["verdicts"]


def test_no_effect_false_claims_at_most_5pct():
    n, bad = 200, 0                 # every field and every shuffle seeded: the same answer every run
    for s in range(n):
        bad += bool(claims(field(40000 + s), seed=s))
    rate = bad / n
    ENV["null_rate"] = rate
    print(f"  null (no car effect): {bad}/{n} fields with a false claim ({rate:.1%})")
    assert rate <= LIMIT(n), rate


def test_confounded_field_no_false_claim():
    n, bad, raw_a_quicker = 120, 0, 0
    for s in range(n):
        races = field(5000 + s, confound=True)
        res = CC.compare(races, TRACK, "GT4", n_perm=200, seed=s)
        bad += bool(res["verdicts"])
        med = {c["car"]: c["median"] for c in res["cars"]}
        raw_a_quicker += med[CARS[0]] < min(v for k, v in med.items() if k != CARS[0])
    rate = bad / n
    ENV["confound_rate"] = rate
    print(f"  confounded (faster drivers pick the {CARS[0]}): raw numbers favour it in {raw_a_quicker}/{n}, "
          f"false claims {bad}/{n} ({rate:.1%})")
    assert raw_a_quicker >= n * 0.8, "the confounding should fool the raw numbers"
    assert rate <= LIMIT(n), rate


def test_real_effect_found():
    found, n = 0, 20
    for s in range(n):
        res = CC.compare(field(9000 + s, n_drivers=150, effects={CARS[1]: -0.4}), TRACK, "GT4", n_perm=200, seed=s)
        v = [x for x in res["verdicts"] if x["fast"] == CARS[1]]
        wrong = [x for x in res["verdicts"] if x["slow"] == CARS[1]]
        assert not wrong, wrong
        found += bool(v)
        if s == 0:
            assert v and abs(v[0]["gap"] - 0.4) < 0.25, res["verdicts"]
            assert v[0]["text"].startswith("Drivers of similar iRating were about ") and \
                f"a lap quicker in the {CARS[1]} than in the " in v[0]["text"], v[0]["text"]
            assert "drivers in the same iRating bands" in v[0]["text"]
            assert res["caveat"] == "Car choice and driver skill can't be fully separated."
            assert res["slope_per_1000"] < 0 and abs(res["slope_per_1000"] + 0.8) < 0.3, res["slope_per_1000"]
            row = next(c for c in res["cars"] if c["car"] == CARS[1])
            assert row["drivers"] >= 15 and row["with_irating"] >= 15
    ENV["power"] = found / n
    print(f"  real 0.4 s effect, 50 drivers a car: found in {found}/{n}")
    assert found >= n * 0.8, found


def mix_field(seed, lo, hi=0.6, races=8, n=240, effect=-0.5):
    """The 2.18 review's scenario: races alternate between a McLaren-light, Mustang-heavy mix (with the BMW) and the
    other way round; the Mustang is really `effect` s a lap quicker; the McLaren and the BMW are the same."""
    A, B, C = CARS
    rng = np.random.default_rng(seed)
    people = [{"name": f"Driver {k}", "car": CARS[k % 3], "ir": float(np.clip(rng.lognormal(np.log(2000), 0.45), 600, 7000)),
               "skill": rng.normal(0, 0.5)} for k in range(n)]
    out = []
    for r in range(races):
        share = {A: lo, B: hi, C: hi * 0.5} if r % 2 == 0 else {A: hi, B: lo, C: lo * 0.5}
        day = rng.normal(0, 0.6)
        ds = []
        for p in people:
            if rng.random() >= share[p["car"]]:
                continue
            pace = 100 + (effect if p["car"] == B else 0) - 0.8 * (p["ir"] - 2000) / 1000 + p["skill"] + day + rng.normal(0, 0.25)
            ds.append({"name": p["name"], "car": p["car"], "class": "GT4", "irating": p["ir"], "pace": pace,
                       "best": pace - 1, "laps": 14, "finished": True})
        out.append({"source": "results", "id": str(r), "track": TRACK, "when": "", "drivers": ds})
    return out


def test_car_mix_swing():
    A, B, C = CARS
    n, false_ac, gaps = 100, 0, []
    for s in range(n):
        res = CC.compare(mix_field(200000 + s, lo=0.15), TRACK, "GT4", n_perm=200, seed=s)
        false_ac += any({v["fast"], v["slow"]} == {A, C} for v in res["verdicts"])
        gaps += [v["gap"] for v in res["verdicts"] if {v["fast"], v["slow"]} == {A, B}]
    ENV["mix"] = {"false_A_vs_C": f"{false_ac}/{n}", "gap_mean": round(float(np.mean(gaps)), 3), "found": len(gaps)}
    print(f"  car mix 4:1 swing: false McLaren-vs-BMW claims {false_ac}/{n}; Mustang gap {np.mean(gaps):.3f} s "
          f"(true 0.5) in {len(gaps)} fields")
    assert false_ac / n <= LIMIT(n), false_ac
    assert len(gaps) >= 0.8 * n and abs(np.mean(gaps) - 0.5) <= 0.05, (len(gaps), np.mean(gaps))


def test_mixed_bunch_in_one_car():
    """No car effect; the two small cars are driven by a much more mixed bunch (spread 1.2 s against 0.3 s)."""
    n, bad = 100, 0
    for s in range(n):
        rng = np.random.default_rng(300000 + s)
        people = []
        for k in range(120):
            car = rng.choice(CARS, p=[0.8, 0.1, 0.1])
            people.append({"name": f"D{k}", "car": car, "ir": float(np.clip(rng.lognormal(np.log(2000), 0.45), 600, 7000)),
                           "skill": rng.normal(0, 0.3 if car == CARS[0] else 1.2)})
        races = []
        for r in range(4):
            day = rng.normal(0, 0.6)
            ds = [{"name": p["name"], "car": p["car"], "class": "GT4", "irating": p["ir"], "best": None, "laps": 14,
                   "finished": True, "pace": 100 - 0.8 * (p["ir"] - 2000) / 1000 + p["skill"] + day + rng.normal(0, 0.25)}
                  for p in people if rng.random() < 0.6]
            races.append({"source": "results", "id": str(r), "track": TRACK, "when": "", "drivers": ds})
        bad += bool(CC.compare(races, TRACK, "GT4", n_perm=200, seed=s)["verdicts"])
    print(f"  mixed bunch in the small cars: false claims {bad}/{n}")
    assert bad / n <= LIMIT(n), bad


def test_confidence_words_and_counts():
    assert CC._confidence(0.01, 20, 20) == "unlikely to be chance"
    assert CC._confidence(0.10, 20, 20) == "likely"
    assert CC._confidence(0.50, 20, 20) == "not enough to tell"
    assert CC._confidence(0.001, 14, 40).startswith("not enough to tell")
    # driver counts are drivers in iRating bands both cars have: a Mustang band nobody in the McLaren is in doesn't count
    races = field(31, n_drivers=90, cars=CARS[:2])
    for r in races:
        for d in r["drivers"]:
            if d["car"] == CARS[1] and d["irating"] >= 3500:
                d["irating"] = 3600.0
            if d["car"] == CARS[0] and d["irating"] >= 3500:
                d["irating"] = 3400.0
    res = CC.compare(races, TRACK, "GT4", n_perm=50)
    p = res["pairs"][0]
    rated = {c: len({(d["name"]) for r in races for d in r["drivers"] if d["car"] == c}) for c in CARS[:2]}
    assert p["drivers_a"] + p["drivers_b"] < sum(rated.values()), (p, rated)


def test_too_few_drivers():
    res = CC.compare(field(7, n_drivers=18, effects={CARS[1]: -1.5}), TRACK, "GT4", n_perm=400)
    assert not res["verdicts"], res["verdicts"]
    assert "Not enough to tell" in res["summary"] and "at least 15 drivers" in res["summary"], res["summary"]
    assert all("not enough" in c["confidence"] or c["reference"] for c in res["cars"])


def test_scans_without_irating_and_you():
    races = field(11, me="Swazi Williams")
    res = CC.compare(races, TRACK, "GT4", me=["Swazi Williams"], n_perm=200)
    you = res["you"]["items"][0]
    assert you["car"] == CARS[0] and you["percentile"] is not None and "quicker than" in you["text"], you
    for r in races:
        for d in r["drivers"]:
            d["irating"] = None
        r["source"] = "scan"
    res = CC.compare(races, TRACK, "GT4", n_perm=200)
    assert not res["verdicts"] and "iRatings aren't known" in res["summary"], res["summary"]


def test_results_file_reads_car_and_laps():
    top = ('"Start Time","Track","Series","Hosted Session Name","Session ID","Subsession ID","Points Type","SOF"\n'
           '"2026-10-20 19:00:00 GMT","Circuit Zolder - Grand Prix","GT4 Falken Tyre Challenge","","111","222","race","1810"\n\n')
    head = ('"Fin Pos","Car ID","Car","Car Class ID","Car Class","Cust ID","Name","Start Pos","Car #","Out","Interval",'
            '"Laps Led","Average Lap Time","Fastest Lap Time","Fast Lap#","Laps Comp","Inc","Old iRating","New iRating","Session Name"\n')
    rows = ['"1","135","McLaren 570S GT4","84","GT4 Class","1","A Fast","2","7","Running","-","3","1:39.512","1:38.004","5","15","0","2400","2450","RACE"',
            '"2","204","Ford Mustang GT4","84","GT4 Class","2","B Next","1","9","Disconnected","-1L","0","1:41.002","1:39.660","3","9","4","1700","1650","RACE"',
            '"3","204","Ford Mustang GT4","84","GT4 Class","3","DQ Guy","3","11","Disqualified","-1L","0","1:40.002","1:39.660","3","15","4","1700","1650","RACE"',
            '"4","204","Ford Mustang GT4","84","GT4 Class","4","Slow Runner","4","12","running","-2L","0","1:40.402","1:39.960","3","13","0","1700","1650","RACE"',
            '"5","204","Ford Mustang GT4","84","GT4 Class","5","Late Runner","5","13","Running","-1L","0","1:40.502","1:39.990","3","14","0","1700","1650","RACE"']
    p = results_csv.parse(top + head + "\n".join(rows) + "\n")
    assert p["race"] == {"track": "Circuit Zolder - Grand Prix", "subsession": "222", "when": "2026-10-20 19:00:00 GMT",
                         "series": "GT4 Falken Tyre Challenge"}, p["race"]
    a, b, dq, slow, late = p["drivers"]
    assert a["model"] == "McLaren 570S GT4" and a["car"] == "7" and a["lap"] == {"avg": 99.512, "best": 98.004, "laps": 15}, a
    assert b["fields"]["finish_status"] == "disconnected" and dq["out"] == "Disqualified" and a["cust_id"] == "1"
    race = CC._race_from_field({"track": TRACK, "drivers": p["drivers"], "race": p["race"]}, "x")
    assert race["id"] == "222" and race["track"] == TRACK
    fin = {d["name"]: d["finished"] for d in race["drivers"]}
    # Running (any case) and at least 90 % of the class winner's 15 laps (13.5): 13 laps isn't, 14 is
    assert fin == {"A Fast": True, "B Next": False, "DQ Guy": False, "Slow Runner": False, "Late Runner": True}, fin
    assert race["drivers"][0]["class"] == "GT4" and race["drivers"][0]["irating"] == 2400
    ENV["csv"] = head + "\n".join(rows[:2]) + "\n"


def test_same_name_two_members():
    """'John Smith' and 'John Smith2' are two members: their Cust IDs keep them apart."""
    def drv(name, cid, pace):
        return {"name": name, "id": cid, "car": CARS[0], "class": "GT4", "irating": 2000.0, "pace": pace, "best": None,
                "laps": 14, "finished": True}
    races = [{"source": "results", "id": "1", "track": TRACK, "when": "",
              "drivers": [drv("John Smith", "100", 100.0), drv("John Smith", "200", 101.0), drv("Other", "300", 100.5)]}]
    rows = CC._drivers(races, TRACK, "GT4")[0]
    assert len(rows) == 3 and sorted(r["name"] for r in rows) == ["John Smith", "John Smith", "Other"], rows
    assert all("id:" in r["person"] for r in rows)


def test_one_entry_per_race_and_layouts():
    """A results table and a later replay scan of the same race (subsession from the CSV header) count once; a scan's
    track takes its layout from the review of the same race; two layouts of one venue are never pooled."""
    lib = TMP / "dup"
    rv = lib / "reviews" / "2026-10-20_zolder"
    rv.mkdir(parents=True, exist_ok=True)
    (rv / "review.json").write_text(json.dumps({"meta": {"track": TRACK, "started_at": "2026-10-20T19:00:00+00:00"}}))
    p = results_csv.parse(('"Start Time","Track","Subsession ID"\n"2026-10-20 19:00:00 GMT","Circuit Zolder - Grand Prix","555"\n\n'
                           + ENV["csv"]))
    (rv / "field.json").write_text(json.dumps({"track": TRACK, "when": "2026-10-20T19:00:00+00:00", "subsession": "",
                                               "race": p["race"], "drivers": p["drivers"]}))
    from debrief import replay_scan
    for sub in ("555", "777"):
        sd = replay_scan.scans_root(lib) / f"{sub}_circuit-zolder"
        (sd / "scan").mkdir(parents=True, exist_ok=True)
        (sd / "scan" / "a.scan.json").write_text(json.dumps({"racecap_scan": 1, "subsession": int(sub), "track": "Circuit Zolder",
            "cars": [{"name": "X", "car": CARS[0], "lap_times": {"1": 100.0, "2": 100.2, "3": 100.1}, "laps_completed": 3}]}))
    rv2 = lib / "reviews" / "2026-10-27_zolder"
    rv2.mkdir(parents=True, exist_ok=True)
    (rv2 / "review.json").write_text(json.dumps({"meta": {"track": TRACK, "started_at": "2026-10-27T19:00:00+00:00"},
                                                 "racecap": {"subsession": "777"}}))
    got = CC.collect(lib)
    ids = sorted(r["id"] for r in got["races"])
    assert ids == ["555", "777"], ids
    scan = next(r for r in got["races"] if r["id"] == "777")
    assert scan["track"] == TRACK and scan["when"].startswith("2026-10-27"), scan
    opts = CC.options(got["races"])
    assert [o["track"] for o in opts] == [CC._tracks(got["races"]).names[0]] and opts[0]["drivers"] == 3, opts
    a = {"source": "results", "id": "1", "track": "Circuit de Spa-Francorchamps - Grand Prix Pits", "when": "", "drivers": []}
    b = {"source": "results", "id": "2", "track": "Circuit de Spa-Francorchamps - Endurance", "when": "", "drivers": []}
    c = {"source": "results", "id": "3", "track": "Circuit de Spa-Francorchamps (Grand Prix Pits)", "when": "", "drivers": []}
    t = CC._tracks([a, b, c])
    assert a["_track"] != b["_track"] and a["_track"] == c["_track"], t.names


def test_one_season_by_default():
    races = field(41, n_drivers=40, races=4)
    for r, when in zip(races, ("2026-06-20", "2026-09-20", "2026-10-01", "")):
        r["when"] = when + ("T19:00:00+00:00" if when else "")
    seasons = [("2026 Season 3", CC.date(2026, 6, 9), CC.date(2026, 9, 15)), ("2026 Season 4", CC.date(2026, 9, 15), CC.date(2026, 12, 15))]
    kept, info = CC.pick_period(races, TRACK, "GT4", "season", seasons)
    assert info["mode"] == "season" and info["races"] == 2 and info["races_all"] == 4 and info["undated"] == 1, info
    assert info["label"] == "2026 Season 4 (15 Sep 2026 to 14 Dec 2026)", info["label"]
    assert len([r for r in kept if r["track"] == TRACK]) == 2
    kept, info = CC.pick_period(races, TRACK, "GT4", "season", [])          # no schedule: the 13 weeks to the newest race
    assert info["races"] == 2 and info["label"] == "the 13 weeks to 1 Oct 2026", info
    kept, info = CC.pick_period(races, TRACK, "GT4", "all", seasons)
    assert info["mode"] == "all" and len(kept) == 4
    res = CC.compare(races, TRACK, "GT4", n_perm=20, period="season", season_list=seasons)
    assert res["counts"]["races"] == 2 and res["period"]["mode"] == "season"


# ================================================================ where each car is faster (fake telemetry)
def _laps(path):
    laps, _info = cornercoach.load_laps([str(path)])
    return laps


def tel_files():
    if "tel" in ENV:
        return ENV["tel"]
    TEL.mkdir(parents=True, exist_ok=True)
    a = cl.make(TEL / "mclaren570sgt4_zolder gp 2026-11-01 10-00-00.ibt", 9, seed=7, session="Race")
    keep = cl.VMIN
    try:
        cl.VMIN = tuple(v + 2.5 if k == 3 else v for k, v in enumerate(keep))      # car B: faster through T4
        b = cl.make(TEL / "fordmustanggt4_zolder gp 2026-11-02 10-00-00.ibt", 9, seed=8, session="Race",
                    car="Ford Mustang GT4", car_path="fordmustanggt4", car_id=204)
    finally:
        cl.VMIN = keep
    a2 = TMP / "same" / "mclaren570sgt4_zolder gp 2026-11-03 10-00-00.ibt"
    a2.parent.mkdir(parents=True, exist_ok=True)
    cl.make(a2, 9, seed=9, session="Race")
    ENV["tel"] = (TEL / a.name if hasattr(a, "name") else TEL / "mclaren570sgt4_zolder gp 2026-11-01 10-00-00.ibt",
                  TEL / "fordmustanggt4_zolder gp 2026-11-02 10-00-00.ibt", a2)
    return ENV["tel"]


def test_where_faster_on_fake_telemetry():
    pa, pb, pa2 = tel_files()
    la, lb, la2 = _laps(pa), _laps(pb), _laps(pa2)
    assert len(la) >= 3 and len(lb) >= 3, (len(la), len(lb))
    w = CC.where_faster(la, lb, "McLaren 570S GT4", "Ford Mustang GT4")
    assert not w["rough"]
    t4 = [r for r in w["rows"] if r["segment"] == "Turn 4"]
    assert t4 and "min_kmh" in t4[0]["found"] and t4[0]["found"]["min_kmh"]["diff"] < 0, w["rows"]
    assert "km/h faster at the slowest point in the Ford Mustang GT4" in t4[0]["text"], t4[0]["text"]
    assert "possibly" not in t4[0]["text"] and t4[0]["found"]["min_kmh"]["p"] <= 0.05
    others = [r for r in w["rows"] if r["kind"] == "corner" and r["segment"] not in ("Turn 4", "Turn 5")]
    assert len(others) <= 1, [r["text"] for r in w["rows"]]
    same = CC.where_faster(la, la2, "Run 1", "Run 2")
    assert len([r for r in same["rows"] if r["kind"] == "corner"]) <= 1, [r["text"] for r in same["rows"]]
    assert len(same["rows"]) == 0, [r["text"] for r in same["rows"]]
    rough = CC.where_faster(la[:4], lb[:4], "McLaren 570S GT4", "Ford Mustang GT4")
    assert rough["rough"] and 1 <= len(rough["rows"]) <= 3, rough["rows"]
    assert all("possibly" in r["text"] for r in rough["rows"]), [r["text"] for r in rough["rows"]]
    for r in w["rows"] + rough["rows"]:
        assert "you brake" not in r["text"]
    for r in w["rows"] + rough["rows"]:
        assert "—" not in r["text"] and "–" not in r["text"]
    ENV["where"] = [r["text"] for r in w["rows"]]


def test_where_faster_false_rows_at_most_5pct():
    """Same car on both sides, 5 and 9 laps, laps of two kinds (his quick third) and a lap-wide factor: a comparison
    shows a false row in at most 5 % of runs, counting every measure in every corner (37 checks)."""
    keys = ["t", "brake_m", "min_kmh", "exit_kmh"] * 7 + ["t", "top_kmh"] * 5
    sd = {"t": 0.05, "brake_m": 4.0, "min_kmh": 0.8, "exit_kmh": 0.8, "top_kmh": 0.6}
    good = {"t": -0.06, "brake_m": 5.0, "min_kmh": 0.9, "exit_kmh": 0.6, "top_kmh": 0.3}

    def laps(rng, n):
        g, f = rng.random(n) < 1 / 3, rng.normal(0, 1, n)
        M = np.column_stack([sd[k] * (0.6 * rng.standard_t(3, n) + 0.5 * f) + good[k] * g for k in keys])
        M[:, [i for i, k in enumerate(keys) if k == "brake_m"]] = np.round(M[:, [i for i, k in enumerate(keys) if k == "brake_m"]] / 5) * 5
        return M
    N, out = 200, {}
    for n in (5, 9):
        bad = 0
        for s in range(N):
            rng = np.random.default_rng(700000 + s)
            bad += any(CC.wf_test(laps(rng, n), laps(rng, n), [CC.NOISE_FLOOR[k] for k in keys], seed=s)["found"])
        out[n] = bad
        assert bad / N <= LIMIT(N), (n, bad)
    # and a real 2 km/h difference at one corner's slowest point with 9 laps a car is found
    rng = np.random.default_rng(5)
    Ma, Mb = laps(rng, 9), laps(rng, 9)
    Mb[:, 2] += 2.0
    assert CC.wf_test(Ma, Mb, [CC.NOISE_FLOOR[k] for k in keys])["found"][2]
    ENV["wf_false"] = {f"{n} laps": f"{b}/{N}" for n, b in out.items()}
    print(f"  where faster, same car: false rows in {out[5]}/{N} (5 laps) and {out[9]}/{N} (9 laps) comparisons")


# ================================================================ server and window
def settings():
    LIB.mkdir(parents=True, exist_ok=True)
    Path(os.environ["DEBRIEF_SETTINGS"]).write_text(
        'token = "g61-test-token-ABCDEFGHIJKLMNOP"\n'
        f"library_folder = '{LIB}'\napi_base = \"http://127.0.0.1:9/api/v1/\"\niracing_folder = '{IRACING}'\n", encoding="utf-8")


def srv():
    if "srv" not in ENV:
        settings()
        app.LOG_FILE = str(TMP / "debrief.log")
        s = S.DebriefServer(heartbeat_timeout=3600)
        threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
        ENV["srv"] = s
        ENV["opener"] = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return ENV["srv"]


def call(rel, header=True, data=None, js=None):
    s = srv()
    h = {"X-Debrief": "1"} if header else {}
    if js is not None:
        data = json.dumps(js).encode()
        h["Content-Type"] = "application/json"
    elif data is not None:
        h["Content-Type"] = "application/octet-stream"
    rq = urllib.request.Request(s.url + rel, headers=h, data=data, method="POST" if data is not None else "GET")
    try:
        with ENV["opener"].open(rq, timeout=300) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw


def library():
    """Reviews with a results table each (made like the window does: the CSV through api/result-csv, then Save)."""
    if "lib" in ENV:
        return
    tel_files()
    races = field(21, n_drivers=70, effects={CARS[1]: -0.5}, me="Swazi Williams", cars=CARS[:2])
    head = ('"Fin Pos","Car ID","Car","Car Class ID","Car Class","Cust ID","Name","Start Pos","Car #","Out","Laps Led",'
            '"Average Lap Time","Fastest Lap Time","Laps Comp","Inc","Old iRating","New iRating","Session Name"\n')
    for k, r in enumerate(races):
        d = LIB / "reviews" / f"2026-10-{20 + k:02d}_circuit-zolder-grand-prix_mclaren-570s-gt4_{k}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "review.json").write_text(json.dumps({"meta": {"driver": "Swazi Williams", "track": TRACK, "car": CARS[0],
                                                            "started_at": f"2026-10-{20 + k:02d}T19:00:00+00:00"}}), encoding="utf-8")
        lines = []
        for i, x in enumerate(sorted(r["drivers"], key=lambda x: x["pace"]), 1):
            avg = x["pace"]
            cid = 1000 + int(x["name"].split()[-1]) if x["name"].split()[-1].isdigit() else 999     # one id per member
            lines.append(f'"{i}","1","{x["car"]}","84","GT4 Class","{cid}","{x["name"]}","{i}","{i}","Running","0",'
                         f'"1:{avg - 60:06.3f}","1:{avg - 61.2:06.3f}","14","0","{int(x["irating"])}","{int(x["irating"])}","RACE"')
        top = (f'"Start Time","Track","Subsession ID"\n"2026-10-{20 + k:02d} 19:00:00 GMT","Circuit Zolder - Grand Prix",'
               f'"{900 + k}"\n\n')
        csv = (top + head + "\n".join(lines) + "\n").encode()
        ENV.setdefault("csvs", []).append((d, csv))
        st, j = call("api/result-csv?review=" + urllib.request.quote(d.name), data=csv)
        assert st == 200 and j["ok"] and j["matched"]["name"] == "Swazi Williams", (st, j)
        assert "cust_id" not in json.dumps(j)
        assert not (d / "field.json").exists(), "kept before Save"
        st, j = call("api/result", js={"review": d.name, "action": "save", "fields": {"finish_pos": 5},
                                       "field_me": "Swazi Williams"})
        assert st == 200 and j["ok"] and j["field"]["kept"], j
        doc = json.loads((d / "field.json").read_text(encoding="utf-8"))
        assert doc["subsession"] == str(900 + k) and doc["me"] == "Swazi Williams" and doc["drivers"][0]["out"] == "Running"
    ENV["lib"] = True


def test_results_table_kept_only_when_it_fits():
    library()
    d, csv = ENV["csvs"][0]
    other = LIB / "reviews" / "2026-11-01_spa_x"
    other.mkdir(parents=True, exist_ok=True)
    (other / "review.json").write_text(json.dumps({"meta": {"driver": "Swazi Williams", "track": "Circuit de Spa-Francorchamps (Grand Prix Pits)"}}))
    st, j = call("api/result-csv?review=" + other.name, data=csv)          # Zolder's file on a Spa review: refused
    assert st == 400 and "is for Circuit Zolder - Grand Prix" in j["error"], j
    (other / "review.json").write_text(json.dumps({"meta": {"driver": "Swazi Williams", "track": TRACK}, "racecap": {"subsession": "123"}}))
    st, j = call("api/result-csv?review=" + other.name, data=csv)          # another race at the same track: refused
    assert st == 400 and "race 900" in j["error"] and "race 123" in j["error"], j
    (other / "review.json").write_text(json.dumps({"meta": {"driver": "Someone Else", "track": TRACK}}))
    st, j = call("api/result-csv?review=" + other.name, data=csv)
    assert st == 200 and j["matched"] is None
    st, j = call("api/result", js={"review": other.name, "action": "save", "fields": {"finish_pos": 3}, "field_me": ""})
    assert j["ok"] and not j["field"]["kept"] and "your name wasn't found" in j["field"]["note"], j
    assert not (other / "field.json").exists()
    # a good table is never replaced silently: the same race again keeps a .bak; another race is refused
    good = (d / "field.json").read_text(encoding="utf-8")
    st, j = call("api/result-csv?review=" + d.name, data=csv.replace(b'"Running","0"', b'"Running","1"', 1))
    st, j = call("api/result", js={"review": d.name, "action": "save", "fields": {"finish_pos": 5}, "field_me": "Swazi Williams"})
    assert j["field"]["kept"] and "field.json.bak" in j["field"]["note"], j
    assert (d / "field.json.bak").read_text(encoding="utf-8") == good
    doc = json.loads(good)
    doc["subsession"] = "4242"
    (d / "field.json").write_text(json.dumps(doc), encoding="utf-8")
    st, j = call("api/result-csv?review=" + d.name, data=csv)
    st, j = call("api/result", js={"review": d.name, "action": "save", "fields": {"finish_pos": 5}, "field_me": "Swazi Williams"})
    assert not j["field"]["kept"] and "another race (4242)" in j["field"]["note"], j
    assert json.loads((d / "field.json").read_text(encoding="utf-8"))["subsession"] == "4242"
    (d / "field.json").write_text(good, encoding="utf-8")
    shutil.rmtree(other)


def no_dash_or_path(obj, where):
    t = json.dumps(obj, ensure_ascii=False)
    assert "—" not in t and "–" not in t, f"long dash in {where}"
    for p in (str(TMP), str(LIB), str(TEL)):
        assert p not in t, f"a path in {where}"


def test_endpoint():
    library()
    st, j = call("api/carcompare", header=False)
    assert st == 404
    st, j = call("api/carcompare?nope=1")
    assert st == 400
    t0 = time.time()
    st, j = call("api/carcompare")
    assert st == 200 and j["ok"], j
    first = time.time() - t0
    assert j["selected"] == {"track": TRACK, "class": "GT4"}, j["selected"]
    c = j["compare"]
    assert len(c["cars"]) == 2 and c["verdicts"] and c["verdicts"][0]["fast"] == CARS[1], c["summary"]
    assert c["you"]["items"][0]["car"] == CARS[0]
    w = j["where"]
    assert w and {w["a"], w["b"]} == {"McLaren 570S GT4", "Ford Mustang GT4"}, (w, j["tel_cars"])
    assert c["period"]["mode"] == "season" and c["period"]["races"] == 4 and "Season" in c["period"]["label"], c["period"]
    assert "Swazi" not in json.dumps(j["options"]) and '"person"' not in json.dumps(j) and "cust" not in json.dumps(j)
    st, ja = call("api/carcompare?period=all")
    assert ja["period"] == "all" and ja["compare"]["period"]["mode"] == "all"
    st, jb = call("api/carcompare?period=nope")
    assert st == 400
    no_dash_or_path(j, "api/carcompare")
    st, j2 = call("api/carcompare")
    assert j2["cached"] and j2["compare"] == j["compare"]
    st, j3 = call("api/carcompare?refresh=1")
    assert st == 200 and not j3["cached"]
    ENV["api"] = {"summary": c["summary"], "first_s": round(first, 1)}


def test_teammate_lap_fills_in_a_car():
    """His telemetry only in the McLaren; a teammate's Ford lap loaded on the Coach page fills in the Ford, labelled
    with the teammate's name, as a rough look (one lap)."""
    library()
    from debrief.desktop import carsetup
    pa, pb, _pa2 = tel_files()
    lap = _laps(pb)[0]
    st = carsetup.state(srv())
    with st.lock:
        st.team[("fordmustanggt4", "zolder gp")] = {"summary": {"driver": "Alex Teammate", "car": "Ford Mustang GT4",
                                                               "track": TRACK, "label": "Alex Teammate's best lap", "lap_time": lap["time"]},
                                                   "cornercoach": lap, "kept": False}
    away = TMP / "away"
    away.mkdir(exist_ok=True)
    shutil.move(str(pb), str(away / pb.name))
    try:
        s, j = call("api/carcompare?refresh=1")
        assert s == 200 and j["ok"], j
        w = j["where"]
        assert w and w["b"] == "Ford Mustang GT4 (Alex Teammate's lap)" and w["teammate"] == "Alex Teammate", (w, j["tel_cars"])
        assert w["rough"] and w["laps_b"] == 1 and len(w["rows"]) <= 3
        assert any(c.get("driver") == "Alex Teammate" for c in j["tel_cars"])
    finally:
        shutil.move(str(away / pb.name), str(pb))
        with st.lock:
            st.team.clear()
        call("api/carcompare?refresh=1")


def browser():
    if "b" not in PW:
        try:
            from playwright.sync_api import sync_playwright
            PW["pw"] = sync_playwright().start()
            PW["b"] = PW["pw"].chromium.launch()
        except Exception:  # noqa: BLE001
            print("  SKIPPED: Playwright/Chromium not available")
            PW["b"] = None
    return PW["b"]


def test_page_in_the_window():
    library()
    b = browser()
    if not b:
        return
    shots = os.environ.get("DEBRIEF_SHOTS")
    ctx = b.new_context(viewport={"width": 1320, "height": 900}, color_scheme="light", device_scale_factor=2 if shots else 1)
    pg = ctx.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    for pat in ("**/api/sync", "**/api/update*"):
        pg.route(pat, lambda r: r.fulfill(body='{"ok":true,"skipped":true}', content_type="application/json"))
    try:
        pg.goto(srv().url)
        pg.wait_for_function("() => document.querySelector('.navbtn[data-nav=cars]')")
        pg.click(".navbtn[data-nav=cars]")
        pg.wait_for_selector("#vCars", state="visible")
        pg.wait_for_selector("#ccTable table", timeout=120000)
        t = pg.text_content("#vCars")
        assert "Car vs car" in t and "Drivers of similar iRating were about" in t and "You vs the field" in t, t[:800]
        assert "Car choice and driver skill can't be fully separated." in t and "Likely: unlikely to be chance" in t, t[:1500]
        assert "Where you were faster in each car" in t and "different sessions, days and setups" in t, t[:2500]
        assert "Clear" not in t and "per 1000 iRating" in pg.inner_html("#vCars")
        assert "One season: " in t
        assert "—" not in t and "–" not in t and str(TMP) not in t
        assert pg.locator("#ccTable tbody tr").count() == 2
        if shots:
            Path(shots).mkdir(parents=True, exist_ok=True)
            pg.evaluate("() => { const b = document.getElementById('banner'); if (b) b.hidden = true }")
            h = pg.evaluate("() => document.getElementById('vCars').scrollHeight")
            pg.set_viewport_size({"width": 1320, "height": max(900, min(int(h) + 40, 9000))})
            time.sleep(0.3)
            pg.screenshot(path=str(Path(shots) / "carcompare-light.png"))
        assert not errs, errs
    finally:
        ctx.close()


def test_empty_state_words():
    js = (ROOT / "debrief" / "desktop" / "ui" / "app.js").read_text(encoding="utf-8")
    assert "cars:()=>openCars()" in js and "'vCars'" in js
    for words in ("Add the iRacing results file", "Scan this race", "Pick a teammate\\'s file"):
        assert words in js, words
    assert "into your telemetry folder" not in js and "Clear answer" not in js
    for f in ("README.md", "CHANGELOG.md"):
        assert "into your telemetry folder" not in (ROOT / f).read_text(encoding="utf-8"), f


if __name__ == "__main__":
    fails = 0
    only = sys.argv[1:]
    try:
        for k, fn in list(globals().items()):
            if k.startswith("test_") and callable(fn) and (not only or k in only):
                try:
                    fn()
                    print("ok", k)
                except (Exception, SystemExit):  # noqa: BLE001
                    fails += 1
                    print("FAIL", k)
                    traceback.print_exc()
    finally:
        if PW.get("b"):
            PW["b"].close()
            PW["pw"].stop()
        if "srv" in ENV:
            ENV["srv"].shutdown()
        shutil.rmtree(TMP, ignore_errors=True)
    print({k: v for k, v in ENV.items() if k in ("null_rate", "confound_rate", "power", "mix", "wf_false", "where", "api")})
    print("FAILED" if fails else "ALL OK", fails)
    sys.exit(1 if fails else 0)
