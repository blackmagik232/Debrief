"""2.15: pressure learner (debrief/pressurelearn.py). Run: python tests/test_215_pressure.py

Runs are built as Setup Lab index records (the shape setuplab.index() saves), so each case can set the cold
pressure, track temperature, hot pressures and lap times on purpose. Checks: a real pressure effect is found in
its band, pure noise is not (shuffle false-positive rate at most 5 %), too few runs only lists what was run, the
hot-pressure prediction needs 4 runs and its typical miss is measured on runs left out. Also run on a fake .ibt
folder through setuplab.index. No network, temp folders only."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
TMP = Path(tempfile.mkdtemp(prefix="debrief-215-pressure-"))

import fake_ibt as fi  # noqa: E402
from debrief import pressurelearn as pl, setuplab as sl  # noqa: E402

fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


def no_long_dash(obj, where):
    txt = json.dumps(obj, ensure_ascii=False, default=str)
    check("—" not in txt and "–" not in txt, f"{where}: no long dashes")


def setup_with(cold_f, cold_r=None):
    s = json.loads(json.dumps(fi.SETUP_GT4))
    cold_r = cold_f if cold_r is None else cold_r
    for c, v in (("LeftFront", cold_f), ("RightFront", cold_f), ("LeftRear", cold_r), ("RightRear", cold_r)):
        s["Tires"][c]["StartingPressure"] = f"{v:.0f} kPa"
    return s


K = [0]


def record(cold, temp, pace, rng, n_laps=6, hot_rise=(14.0, 12.0), car="mclaren570sgt4", track="zolder gp",
           noise=0.15, cold_r=None):
    """One file with one stint: out lap, warm-up lap, then n_laps flying laps around `pace`."""
    K[0] += 1
    cold_r = cold if cold_r is None else cold_r
    laps = [{"lap": 1, "time": None, "clean": False, "start_lap": False, "pit": True, "stint": 0, "pos": 0,
             "warm": True, "traffic": False},
            {"lap": 2, "time": pace + 1.5, "clean": True, "start_lap": False, "pit": False, "stint": 1, "pos": 1,
             "warm": True, "traffic": False}]
    for i in range(n_laps):
        hf = cold + hot_rise[0] + 0.3 * rng.standard_normal()
        hr = cold_r + hot_rise[1] + 0.3 * rng.standard_normal()
        laps.append({"lap": 3 + i, "time": round(pace + noise * rng.standard_normal(), 3), "clean": True,
                     "start_lap": False, "pit": False, "stint": 1, "pos": 2 + i, "warm": False, "traffic": False,
                     "press": {"LF": round(hf + 1, 1), "RF": round(hf - 1, 1), "LR": round(hr + 1, 1),
                               "RR": round(hr - 1, 1)}})
    return {"file": f"{car}_{track} 2026-11-{1 + K[0] // 24:02d} {K[0] % 24:02d}-00-00.ibt", "ok": True,
            "car": {"path": car, "name": "McLaren 570S GT4" if car.startswith("mclaren") else car},
            "track": {"code": track, "name": "Zolder Circuit" if track == "zolder gp" else track, "surface_temp": temp},
            "setup": setup_with(cold, cold_r), "sort": 1_790_000_000 + K[0] * 3600, "when": f"run {K[0]}",
            "detail": {"surface_temp": temp, "laps": laps}}


# ---------------------------------------------------------------- 1. a real effect is found
rng = np.random.default_rng(1)
recs = []
for k in range(8):                                    # interleaved: 168, 176, 168, 176 ...
    cold = 168 if k % 2 == 0 else 176
    recs.append(record(cold, 22.0 + 0.3 * rng.standard_normal(), 98.0 if cold == 168 else 98.4, rng))
rep = pl.learn({"files": recs}, "mclaren570sgt4", "zolder gp")
check(rep["runs"] == 8, f"8 runs found ({rep['runs']})")
b = rep["bands"][0] if rep["bands"] else {}
check(b.get("band") == "20 to 25 °C", f"band named in plain words ({b.get('band')})")
f = b.get("finding")
check(f is not None and f["cold"] == "168 kPa cold", f"fastest pressure found: {b.get('text')}")
check(f is not None and 0.3 < f["gap_s"] < 0.5 and f["p"] < 0.05, f"gap and shuffle p ({f})")
check("your fastest runs were on 168 kPa cold (hot about 182 front, 180 rear)" in b.get("text", ""), "hot pressure given with it")
check(f is not None and not f["driven_later"], "interleaved runs: no driving-order caution")
no_long_dash(rep, "report")
json.dumps(rep)

# the quick pressure driven last, in one go after the other: not compared at all (getting quicker as he drives
# can't be told apart from the pressure)
rng = np.random.default_rng(2)
recs2 = [record(176, 22.0, 98.4, rng) for _ in range(4)] + [record(168, 22.0, 98.0, rng) for _ in range(4)]
b2 = pl.learn({"files": recs2}, "mclaren570sgt4", "zolder gp")["bands"][0]
check(b2["finding"] is None and "one after the other" in b2["text"] and "back and forth" in b2["text"]
      and b2["test"]["tested"] is False, f"one go each, one after the other: not compared ({b2['text']})")
# one pressure per day: the day can't be told apart from the pressure
rng = np.random.default_rng(4)
recs3 = [record(176, 22.0, 98.4, rng) for _ in range(4)] + [record(168, 22.0, 98.0, rng) for _ in range(4)]
for i, x in enumerate(recs3):
    x["sort"] = 1_790_000_000 + (0 if i < 4 else 2 * 86400) + i * 3600
b3 = pl.learn({"files": recs3}, "mclaren570sgt4", "zolder gp")["bands"][0]
check(b3["finding"] is None and "different days" in b3["text"] and "the day can't be separated from the pressure"
      in b3["text"], f"one pressure per day: says the day can't be separated ({b3['text']})")
# blocks: runs straight after each other on one pressure on one day count once
bl_ = pl.blocks([dict(cold_key="A", median=1.0, sort=10, file="a"), dict(cold_key="A", median=3.0, sort=20, file="b"),
                 dict(cold_key="B", median=2.0, sort=30, file="c"), dict(cold_key="A", median=5.0, sort=40, file="d")])
check([(b["label"], len(b["runs"]), b["median"]) for b in bl_] == [("A", 2, 2.0), ("B", 1, 2.0), ("A", 1, 5.0)],
      f"blocks: consecutive runs on one pressure count once ({[(b['label'], len(b['runs'])) for b in bl_]})")
check(not pl.back_and_forth(bl_)[0], "ABA: B only once, not compared")
check(pl.back_and_forth(pl.blocks([dict(cold_key=k, median=1.0, sort=i, file=str(i)) for i, k in enumerate("ABAB")]))[0],
      "ABAB: compared")

# ---------------------------------------------------------------- 2. not enough runs
rng = np.random.default_rng(3)
few = [record(168, 22.0, 98.0, rng), record(176, 22.5, 98.6, rng)]
r = pl.learn(few, "mclaren570sgt4", "zolder gp")
b = r["bands"][0]
check(b["finding"] is None and "Can't compare them yet" in b["text"] and "168 kPa cold: 1 run," in b["text"],
      f"1 + 1 runs: lists, no claim ({b['text']})")
check("Not enough runs" in " ".join(r["text"]), "says not enough runs")
three = [record(168 if i % 2 == 0 else 176, 22.0, 98.0 if i % 2 == 0 else 99.0, rng) for i in range(6)]
b = pl.learn(three, "mclaren570sgt4", "zolder gp")["bands"][0]
check(b["finding"] is None and b["test"]["p_min"] >= 0.05 and "about 4 goes on each pressure" in b["text"],
      f"3 + 3 runs back and forth can never pass a 5% shuffle test ({b['test']}, {b['text']})")
same = [record(176, 22.0, 98.0, rng) for _ in range(3)]
b = pl.learn(same, "mclaren570sgt4", "zolder gp")["bands"][0]
check(b["finding"] is None and "all on 176 kPa cold" in b["text"], "all on one pressure: says so")
one = [record(176, 22.0, 98.0, rng)]
b = pl.learn(one, "mclaren570sgt4", "zolder gp")["bands"][0]
check("only 1 run" in b["text"], f"one run: says only one ({b['text']})")
split = [record(168, 18.0, 98.0, rng) for _ in range(4)] + [record(176, 27.0, 98.5, rng) for _ in range(4)]
r = pl.learn(split, "mclaren570sgt4", "zolder gp")
check(len(r["bands"]) == 2 and not any(x["finding"] for x in r["bands"]),
      "pressures in different temperature bands are never compared")
short = [record(168, 22.0, 98.0, rng, n_laps=1) for _ in range(5)]
check(pl.learn(short, "mclaren570sgt4", "zolder gp")["runs"] == 0, "stints with too few counted laps are not runs")
check(pl.learn({"files": []}, "x", "y")["text"][0].startswith("No runs yet"), "nothing: says no runs")
check(pl.learn(None, None)["runs"] == 0, "bad input: no crash")

# ---------------------------------------------------------------- 3. shuffle false-positive rate (no effect)
fp = 0
N = 1000
rng = np.random.default_rng(7)
for k in range(N):
    n1, n2 = int(rng.integers(4, 7)), int(rng.integers(4, 7))
    rr = [record(168, 22.0, 98.0, rng, n_laps=int(rng.integers(3, 8)), noise=0.3) for _ in range(n1)]
    rr += [record(176, 22.0, 98.0, rng, n_laps=int(rng.integers(3, 8)), noise=0.3) for _ in range(n2)]
    order = rng.permutation(len(rr))
    rr = [rr[i] for i in order]
    for i, x in enumerate(rr):
        x["sort"] = 1_790_000_000 + i * 3600
    # every run also gets its own day-to-day offset (track, rubber, mood): runs are the units, not laps
    for x in rr:
        off = 0.25 * rng.standard_normal()
        for lp in x["detail"]["laps"]:
            if lp["time"]:
                lp["time"] += off
    res = pl.learn(rr, "mclaren570sgt4", "zolder gp")
    fp += int(any(bb["finding"] for bb in res["bands"]))
rate = fp / N
print(f"     no-effect sessions: {fp}/{N} with a false 'fastest pressure' = {rate:.1%}")
check(rate <= 0.05, f"shuffle test false-positive rate {rate:.1%} (target 5%)")
# three pressures: picking the best of three is part of the null too
fp = 0
for k in range(100):
    rr = []
    for cold in (164, 170, 176):
        rr += [record(cold, 22.0, 98.0, rng, noise=0.3) for _ in range(4)]
    for x in rr:
        off = 0.25 * rng.standard_normal()
        for lp in x["detail"]["laps"]:
            if lp["time"]:
                lp["time"] += off
    fp += int(any(bb["finding"] for bb in pl.learn(rr, "mclaren570sgt4", "zolder gp")["bands"]))
print(f"     three-pressure no-effect sessions: {fp}/100 = {fp / 100:.1%}")
check(fp / 100 <= 0.05, f"best of three pressures: false-positive rate {fp / 100:.1%} (target 5%)")

# a day effect with one pressure per day (2 or 3 days, 4 runs a day, the day worth 0.25 s): the old run-level
# shuffle called a "fastest pressure" in about 6 sessions in 10; now never more than 1 in 20
fp = 0
for k in range(200):
    rr = []
    days = int(rng.integers(2, 4))
    for d in range(days):
        de = 0.25 * rng.standard_normal()
        for j in range(4):
            x = record((164, 170, 176)[d], 22.0, 98.0 + de, rng, noise=0.1)
            x["sort"] = 1_790_000_000 + d * 86400 + j * 3600
            rr.append(x)
    fp += int(any(bb["finding"] for bb in pl.learn(rr, "mclaren570sgt4", "zolder gp")["bands"]))
print(f"     one pressure per day, day effect 0.25 s: {fp}/200 = {fp / 200:.1%}")
check(fp / 200 <= 0.05, f"day effect: false-positive rate {fp / 200:.1%} (target 5%)")
# getting quicker (0.04 s a run), A then B
fp = 0
for k in range(200):
    rr = [record(168 if i < 4 else 176, 22.0, 98.0 - 0.04 * i, rng, noise=0.1) for i in range(8)]
    for i, x in enumerate(rr):
        x["sort"] = 1_790_000_000 + i * 3600
    fp += int(any(bb["finding"] for bb in pl.learn(rr, "mclaren570sgt4", "zolder gp")["bands"]))
print(f"     A then B, getting quicker: {fp}/200 = {fp / 200:.1%}")
check(fp / 200 <= 0.05, f"getting quicker: false-positive rate {fp / 200:.1%} (target 5%)")

# ---------------------------------------------------------------- 4. hot pressure prediction
rng = np.random.default_rng(11)


def runs_for_pred(n, temps=None):
    out = []
    for i in range(n):
        t = temps[i] if temps else float(rng.uniform(15, 32))
        cold = float(rng.choice([164, 168, 172, 176]))
        out.append(record(cold, t, 98.0, rng, hot_rise=(6.0 + 0.4 * t + rng.normal(0, 0.8), 5.0 + 0.35 * t)))
    return out


r3 = pl.learn(runs_for_pred(3), "mclaren570sgt4", "zolder gp")
check(r3["prediction"] is None and "needs 4 runs" in r3["prediction_text"], "3 runs: no prediction yet")
r8 = pl.learn(runs_for_pred(8), "mclaren570sgt4", "zolder gp")
m = r8["prediction"]
check(m is not None and m["uses_temp"] and m["runs"] == 8, f"8 runs: prediction with temperature ({m and m['runs']})")
check(m is not None and abs(m["axles"]["front"]["c"] - 0.4) < 0.15, f"front slope learned ({m and m['axles']['front']})")
check(m is not None and 1 <= m["miss_kpa"] <= 3, f"typical miss from left-out runs is honest ({m and m['miss_kpa']} kPa)")
p = pl.predict_hot(m, 168, 168, 25.0)
check(p is not None and abs(p["front"] - (168 + 6 + 10)) < 3 and abs(p["rear"] - (168 + 5 + 8.75)) < 3,
      f"prediction at 25 °C ({p})")
check("typical miss" in r8["prediction_text"] and "left out" in r8["prediction_text"], r8["prediction_text"])
narrow = pl.learn(runs_for_pred(5, temps=[22.0, 22.5, 23.0, 22.2, 22.8]), "mclaren570sgt4", "zolder gp")
check(narrow["prediction"] and not narrow["prediction"]["uses_temp"] and "isn't learned yet" in narrow["prediction_text"],
      "runs at one temperature: no temperature effect claimed")
check(pl.predict_hot(None, 1, 2, 3) is None, "no model: no prediction")
check(p["cold_outside"] == [], "cold pressures inside the runs' own: no warning")
lo_f, hi_f = m["cold_range"]["front"]
p2 = pl.predict_hot(m, hi_f + 12, 168, 25.0)
check(p2["cold_outside"] == ["front"], f"a cold front pressure outside the runs' own range is flagged ({m['cold_range']})")
# per car when the track has too few
other = runs_for_pred(6)
for x in other:
    x["track"] = {"code": "spa", "name": "Spa", "surface_temp": x["track"]["surface_temp"]}
mix = pl.learn(other + runs_for_pred(1), "mclaren570sgt4", "zolder gp")
check(mix["prediction"] and mix["prediction"]["basis"] == "this car, all tracks", "falls back to the car's other tracks")
check(any(b["band"] for b in mix["by_car"]), "per-car overview by band")

# ---------------------------------------------------------------- 5. through setuplab.index on fake files
folder = TMP / "telemetry"
folder.mkdir()
for i, (cold, temp) in enumerate([(176, 22.0), (176, 22.4)]):
    fi.make(folder / f"mclaren570sgt4_zolder gp 2026-11-0{i + 1} 10-00-00.ibt", [99.0, 98.8, 98.9, 99.1, 98.7, 98.9],
            setup=setup_with(cold), track_temp=temp, start_date=1_790_000_000 + i * 86400)
idx = sl.index(folder)
r = pl.learn(idx, "mclaren570sgt4", "zolder gp")
check(r["runs"] == 2 and r["bands"] and r["bands"][0]["finding"] is None, f"fake files: {r['text']}")
check(r["runs"] and r["bands"][0]["runs"][0]["hot"], "hot pressures read from the live channel")

print(f"\n{'all passed' if not fails else str(fails) + ' FAILED'}")
sys.exit(1 if fails else 0)
