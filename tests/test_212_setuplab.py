"""2.12.0: Setup Lab (debrief/setuplab.py). Run: python tests/test_212_setuplab.py"""
from __future__ import annotations

import copy
import json
import os
import random
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
TMP = Path(tempfile.mkdtemp(prefix="debrief-212-setuplab-"))

import fake_ibt as fi  # noqa: E402
from debrief import ibt, setuplab as sl  # noqa: E402

fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


def folder(name):
    d = TMP / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def mk(d, stamp, laps, car="mclaren570sgt4", track="zolder gp", **kw):
    """A fake file named like iRacing's: '<carpath>_<trackcode> YYYY-MM-DD HH-MM-SS.ibt'."""
    return fi.make(d / f"{car}_{track} {stamp}.ibt", laps, car_path=car, track_code=track, **kw)


def setup_b():
    s = copy.deepcopy(fi.SETUP_GT4)
    s["Chassis"]["LeftFront"]["Camber"] = "-3.6 deg"
    s["Chassis"]["RightFront"]["Camber"] = "-3.6 deg"
    s["Chassis"]["Rear"]["WingAngle"] = "3.5 deg"
    s["Chassis"]["LeftFront"]["SpringRate"] = "150 N/mm"
    return s


def cold_setup(s=None):
    """A setup whose own pit readings are the never-pitted defaults (what real files show), so a test controls them."""
    s = copy.deepcopy(s or fi.SETUP_GT4)
    for c, v in s["Tires"].items():
        for k in ("LastTempsOMI", "LastTempsIMO"):
            if k in v:
                v[k] = "44C, 44C, 44C"
    return s


PACE_A = [99.5, 99.6, 99.4, 99.5]
PACE_B = [99.0, 99.1, 98.9, 99.0]

# ---------------------------------------------------------------- index + cache
d = folder("cache")
mk(d, "2026-11-01 18-00-00", PACE_A)
mk(d, "2026-11-01 19-00-00", PACE_A)
mk(d, "2026-11-02 18-00-00", PACE_B, setup=setup_b())
(d / "broken 2026-11-02 19-00-00.ibt").write_bytes(b"MZ" + b"\0" * 300)
(d / "notes.txt").write_text("not telemetry")
calls = {"n": 0}
real_summary = ibt.summary


def counting(path):
    calls["n"] += 1
    return real_summary(path)


ibt.summary = counting
cache = TMP / "cache.json"
seen = []
idx = sl.index(d, cache_path=cache, progress=lambda i, n, name: seen.append((i, n)))
check(idx["read"] == 4 and calls["n"] == 4 and len(idx["files"]) == 3, f"first scan reads every .ibt ({idx['read']}, {calls['n']})")
check(len(idx["bad"]) == 1 and idx["bad"][0]["file"].startswith("broken"), "a bad file is listed, never raises")
check(seen[-1] == (4, 4), "progress reported")
check(cache.exists() and json.loads(cache.read_text())["version"] == sl.CACHE_VERSION, "cache written")
idx2 = sl.index(d, cache_path=cache)
check(calls["n"] == 4 and idx2["read"] == 0 and idx2["cached"] == 4, "second scan reads no file (all from cache)")
check([r["file"] for r in idx2["files"]] == [r["file"] for r in idx["files"]], "cached records are the same files")
check(idx2["files"][0]["when"] == "2026-11-01 18:00", "local time taken from the file name")
mk(d, "2026-11-01 19-00-00", PACE_A + [99.7])            # rewritten: new size -> read again
idx3 = sl.index(d, cache_path=cache)
check(calls["n"] == 5 and idx3["read"] == 1, "a changed file is read again, the others are not")
stopped = sl.index(folder("cache"), cache_path=TMP / "c2.json", stop=lambda: True)
check(stopped["stopped"] and stopped["files"] == [], "stop() ends the scan")
check(sl.index(TMP / "nowhere")["missing_folder"], "a missing folder is reported, not raised")
ibt.summary = real_summary
check(idx["files"][0]["detail"]["setup_name"] == "baseline.sto", "setup name read from DriverInfo")

# a file that can't be opened just now (iRacing writing it, a lock) is not remembered as bad: read again next time
d = folder("locked")
mk(d, "2026-11-01 18-00-00", PACE_A)
lp = mk(d, "2026-11-01 19-00-00", PACE_A)
real_open = ibt.open_ibt


def locked(path):
    if Path(path).name == lp.name:
        raise PermissionError(13, "The process cannot access the file because it is being used by another process")
    return real_open(path)


ibt.open_ibt = locked
lc_cache = TMP / "locked.json"
i1 = sl.index(d, cache_path=lc_cache)
ibt.open_ibt = real_open
check(len(i1["files"]) == 1 and len(i1["bad"]) == 1 and not any(lp.name in k for k in json.loads(lc_cache.read_text())["files"]),
      "a locked file is listed as unreadable but not cached")
i2 = sl.index(d, cache_path=lc_cache)
check(len(i2["files"]) == 2 and i2["read"] == 1 and i2["cached"] == 1, "...and is read on the next scan")
bad_cache = TMP / "bad.json"
d = folder("damaged")
(d / "x 2026-11-01 18-00-00.ibt").write_bytes(b"\0" * 400)
sl.index(d, cache_path=bad_cache)
check(len(json.loads(bad_cache.read_text())["files"]) == 1, "a damaged file is cached (it won't get better)")

# ---------------------------------------------------------------- fingerprint
base = copy.deepcopy(fi.SETUP_GT4)
moved = copy.deepcopy(base)
moved["Tires"]["LeftFront"]["LastTempsOMI"] = "90C, 95C, 99C"
moved["Tires"]["LeftFront"]["LastHotPressure"] = "199 kPa"
moved["Tires"]["RightRear"]["TreadRemaining"] = "80%, 81%, 82%"
moved["Chassis"]["Rear"]["FuelLevel"] = "30.0 L"
moved["Chassis"]["LeftFront"]["CornerWeight"] = "3100 N"
moved["Chassis"]["LeftFront"]["RideHeight"] = "75.0 mm"
check(sl.fingerprint(base) == sl.fingerprint(moved), "fingerprint ignores pit readings, wear, fuel and garage results")
press = copy.deepcopy(base)
press["Tires"]["LeftFront"]["StartingPressure"] = "172 kPa"
check(sl.fingerprint(base) != sl.fingerprint(press), "starting pressure is part of the setup")
check(sl.fingerprint(base) != sl.fingerprint(setup_b()), "a camber change is a new setup")
check(sl.fingerprint({}) == "", "no setup -> empty fingerprint")

# ---------------------------------------------------------------- diff + labels
ch = sl.diff(base, setup_b())
texts = [c["text"] for c in ch]
check("Front camber -4.0 → -3.6 deg (both sides)" in texts, f"both sides grouped: {texts}")
fc = next(c for c in ch if c["label"] == "Front camber")
check(abs(fc["delta"] - 0.4) < 1e-9 and fc["unit"] == "deg" and fc["both_sides"] and len(fc["keys"]) == 2, "delta and unit")
check("Rear wing 2.5 → 3.5 deg" in texts, "rear wing label")
check("Left front spring 140 → 150 N/mm" in texts, "one-side change stays one item")
check(not any("LastTemps" in c["key"] or "TreadRemaining" in c["key"] for c in sl.diff(base, moved)), "readings never listed")
dm = sl.diff(base, moved)
check({c["kind"] for c in dm} == {"fuel", "result"} and dm[0]["kind"] == "fuel", "fuel and garage results marked, after settings")
uneven = copy.deepcopy(base)
uneven["Chassis"]["LeftFront"]["Camber"] = "-3.8 deg"
uneven["Chassis"]["RightFront"]["Camber"] = "-3.6 deg"
du = sl.diff(base, uneven)
check(len(du) == 2 and not any(c["both_sides"] for c in du), "sides changed differently stay separate")
pads = copy.deepcopy(base)
pads["Chassis"]["FrontBrakes"]["BrakePads"] = "Race"
dp = sl.diff(base, pads)
check(dp[0]["text"] == "Brake pads Endurance → Race" and dp[0]["delta"] is None, "text setting without a number")
check(sl.label("Chassis.FrontBrakes.ArbSetting") == "Front anti-roll bar", "front ARB label")
check(sl.label("Chassis.InCarDials.BrakePressureBias") == "Brake bias", "brake bias label")
check(sl.label("Tires.LeftFront.StartingPressure") == "Left front starting (cold) pressure", "pressure label")
check(sl.label("Chassis.LeftRear.SpringPerchOffset") == "Left rear spring perch offset", "camel case fallback")
check("explain" in fc, "camber gets a one-line explanation")

# ---------------------------------------------------------------- compare + evidence gate


def recs_of(d):
    return sl.index(d)["files"]


d = folder("gate_few")
mk(d, "2026-11-01 18-00-00", PACE_A)
mk(d, "2026-11-02 18-00-00", PACE_B, setup=setup_b())
cmp_ = sl.compare(recs_of(d))
check(cmp_["pairs"][0]["verdict"] == "not enough to tell", "3 counted laps each -> not enough to tell")
check(any("more clean lap" in m and "5 is the minimum" in m for m in cmp_["pairs"][0]["missing"]),
      f"says what is missing: {cmp_['pairs'][0]['missing']}")
check(cmp_["setups"][0]["pace"]["left_out"]["warmup"] == 1, "first flying lap of a stint left out (tyres warming up)")

d = folder("gate_ok")
mk(d, "2026-11-01 18-00-00", PACE_A)
mk(d, "2026-11-01 19-00-00", PACE_A + [120.0])          # a slow lap (traffic or a mistake)
mk(d, "2026-11-02 18-00-00", PACE_B, setup=setup_b())
mk(d, "2026-11-02 19-00-00", PACE_B, setup=setup_b())
cmp_ = sl.compare(recs_of(d))
p = cmp_["pairs"][0]
check(p["verdict"] == "faster" and p["faster"] == "B", f"enough laps + clear gap -> verdict ({p['text']})")
check("faster than Setup A (baseline.sto, first driven Nov 1) by about 0.5 s a lap" in p["text"], "plain wording (2.18: setups sharing a file name carry the day)")
check(p["ci90"] and p["ci90"][0] > 0, f"90% bootstrap interval excludes zero {p['ci90']}")
check(sl.compare(recs_of(d))["pairs"][0]["ci90"] == p["ci90"], "fixed seed: same laps, same interval")
sa = cmp_["setups"][0]["pace"]
check(sa["left_out"]["slow"] == 1 and sa["clean_laps"] == 6 and abs(sa["median"] - 99.5) < 0.01,
      f"lap > 3 x MAD slower than its stint left out {sa['left_out']}")
check(cmp_["setups"][0]["enough"] and cmp_["setups"][1]["enough"], "both setups pass the gate")

d = folder("gate_same")
mk(d, "2026-11-01 18-00-00", PACE_A)
mk(d, "2026-11-01 19-00-00", PACE_A)
mk(d, "2026-11-02 18-00-00", [x - 0.05 for x in PACE_A], setup=setup_b())
mk(d, "2026-11-02 19-00-00", [x - 0.05 for x in PACE_A], setup=setup_b())
p = sl.compare(recs_of(d))["pairs"][0]
check(p["verdict"] == "no clear difference" and "No clear difference yet" in p["text"]
      and isinstance(p["more_laps"], int) and f"about {p['more_laps']} more clean lap" in p["text"],
      f"0.05 s gap -> no clear difference, with laps still needed ({p['text']})")

p = sl.compare(recs_of(folder("gate_ok")))["pairs"][0]
check(p["interleaved"] is False and p["caution"] and "also driven later" in p["text"]
      and "Go back to Setup A (baseline.sto, first driven Nov 1) for a few laps to be sure." in p["text"],
      "B driven after A: a 'faster' verdict says part of it may be him getting quicker")
check(p["ci_level"] == 95 and p["ci_method"] == "laps" and p["ci90"] == p["ci"], "under 10 laps a setup: a 95% interval")

# finding 3: an interval clear of zero but a gap under 0.1 s is not a difference, and needs no more laps
d = folder("gate_tiny")
mk(d, "2026-11-01 18-00-00", [99.50, 99.50, 99.51, 99.49, 99.50, 99.50, 99.51])
mk(d, "2026-11-01 19-00-00", [99.50, 99.50, 99.51, 99.49, 99.50, 99.50, 99.51])
mk(d, "2026-11-02 18-00-00", [99.45, 99.45, 99.46, 99.44, 99.45, 99.45, 99.46], setup=setup_b())
mk(d, "2026-11-02 19-00-00", [99.45, 99.45, 99.46, 99.44, 99.45, 99.45, 99.46], setup=setup_b())
p = sl.compare(recs_of(d))["pairs"][0]
check(p["verdict"] == "no clear difference" and p["more_laps"] is None and p["ci"][0] > 0
      and "about 0.05 s a lap quicker than Setup A" in p["text"]
      and "smaller than the 0.1 s Debrief counts as a real difference" in p["text"], f"tiny but real gap ({p['text']})")


# finding 1: driving order. Identical setups and a driver who gets 0.03 s a lap quicker must not make B "faster".
def drifting(name, order, per_lap=0.03, laps=8, noise=0.03, seed=1):
    import numpy as np
    rng = np.random.default_rng(seed)
    d, k = folder(name), 0
    for i, which in enumerate(order):
        times = []
        for _j in range(laps):
            times.append(100.0 - per_lap * k + float(rng.normal(0, noise)))
            k += 1
        mk(d, f"2026-11-0{1 + i // 2} {18 + i % 2}-00-00", times, setup=setup_b() if which == "B" else None)
    return d


for seed in (1, 2, 3):
    p = sl.compare(recs_of(drifting(f"drift{seed}", "AABB", seed=seed)))["pairs"][0]
    check(p["verdict"] != "faster" and p["faster"] is None, f"0.03 s/lap improvement, same setup: no 'faster' ({p['verdict']})")
check(p["verdict"] == "confounded" and not p["comparable"] and p["interleaved"] is False
      and p["trend"]["hi"] < 0 and p["drift_s"] >= 0.5 * abs(p["diff_s"])
      and "you were getting quicker as you drove" in p["text"] and "Go back to Setup A" in p["text"]
      and any("getting quicker" in c for c in p["confounders"]), f"drift explains the gap -> confounded ({p['text']})")
lt = sl.lap_trend([([0, 1, 2, 3, 4], [100.0, 99.97, 99.94, 99.91, 99.88]), ([10, 11, 12], [99.7, 99.67, 99.64])])
check(lt and abs(lt["slope"] + 0.03) < 1e-6 and lt["stints"] == 2, f"Theil-Sen slope within stints ({lt})")
check(sl.lap_trend([([0, 1], [1.0, 2.0])]) is None, "stints under 3 laps give no trend")
p = sl.compare(recs_of(drifting("drift_aba", "ABBA")))["pairs"][0]
check(p["interleaved"] is True and p["verdict"] != "faster" and not p["caution"]
      and not any("getting quicker" in c for c in p["confounders"]), f"A, B, A: interleaved, so no drift caution ({p['verdict']})")
# a real 0.5 s gain, driven A, B, A: named faster with no caution
d = folder("aba_real")
mk(d, "2026-11-01 18-00-00", PACE_A + [99.5, 99.6])
mk(d, "2026-11-01 19-00-00", PACE_B + [99.0, 99.1], setup=setup_b())
mk(d, "2026-11-02 18-00-00", PACE_A + [99.5, 99.4])
p = sl.compare(recs_of(d))["pairs"][0]
check(p["verdict"] == "faster" and p["faster"] == "B" and p["interleaved"] and not p["caution"]
      and "driven later" not in p["text"], f"interleaved real gain: faster, no caution ({p['text']})")
# block bootstrap: 3+ stints on each setup resample whole stints
d = folder("blocks")
for k in range(3):
    mk(d, f"2026-11-01 1{k}-00-00", PACE_A + [99.5, 99.6])
for k in range(3):
    mk(d, f"2026-11-02 1{k}-00-00", PACE_B + [99.0, 99.1], setup=setup_b())
p = sl.compare(recs_of(d))["pairs"][0]
check(p["ci_method"] == "stints" and p["ci_level"] == 90 and p["verdict"] == "faster", f"3 stints each: block bootstrap ({p['ci_method']})")

d = folder("gate_temp")
mk(d, "2026-11-01 18-00-00", PACE_A, surface_temp=22.0)
mk(d, "2026-11-01 19-00-00", PACE_A, surface_temp=22.0)
mk(d, "2026-11-02 18-00-00", PACE_B, setup=setup_b(), track_temp=35.0)
mk(d, "2026-11-02 19-00-00", PACE_B, setup=setup_b(), track_temp=35.0)
p = sl.compare(recs_of(d))["pairs"][0]
check(p["verdict"] == "confounded" and not p["comparable"] and any("warmer" in m for m in p["confounders"]),
      f"13 °C hotter track -> confounded ({p['confounders']})")
check("not adjusted" in p["text"] and p["faster"] is None, "confounded: said, not adjusted, no winner")

d = folder("gate_fuel")
mk(d, "2026-11-01 18-00-00", PACE_A, fuel=(70.0, 2.9))
mk(d, "2026-11-01 19-00-00", PACE_A, fuel=(70.0, 2.9))
mk(d, "2026-11-02 18-00-00", PACE_B, setup=setup_b(), fuel=(30.0, 2.9))
mk(d, "2026-11-02 19-00-00", PACE_B, setup=setup_b(), fuel=(30.0, 2.9))
p = sl.compare(recs_of(d))["pairs"][0]
check(p["verdict"] == "confounded" and any("more fuel" in m for m in p["confounders"]), "very different fuel -> confounded")

d = folder("gate_wet")
mk(d, "2026-11-01 18-00-00", PACE_A)
mk(d, "2026-11-01 19-00-00", PACE_A)
mk(d, "2026-11-02 18-00-00", PACE_B, setup=setup_b(), wet=True)
mk(d, "2026-11-02 19-00-00", PACE_B, setup=setup_b(), wet=True)
p = sl.compare(recs_of(d))["pairs"][0]
check(p["verdict"] == "confounded" and any("wet track" in m for m in p["confounders"]), "wet against dry -> confounded")

d = folder("stints")
mk(d, "2026-11-01 18-00-00", [99.5, 99.4, 99.6, 101.0, 99.5, 99.4, 99.6, 99.5], pits=(3,))
pace = sl.compare(recs_of(d))["setups"][0]
check(len(pace["pace"]["stints"]) == 2 and pace["pace"]["clean_laps"] == 5 and pace["enough"]
      and pace["pace"]["left_out"]["warmup"] == 2, "a pit stop splits the file into two stints, each losing its warm-up lap")

d = folder("traffic")
mk(d, "2026-11-01 18-00-00", [99.5, 99.4, 99.6, 99.9, 99.5, 99.4], traffic=(3,))
pace = sl.compare(recs_of(d))["setups"][0]["pace"]
check(pace["left_out"]["traffic"] == 1 and 99.9 not in pace["times"], "a lap with a car alongside is left out")

d = folder("race")
mk(d, "2026-11-01 18-00-00", [104.0, 99.5, 99.1, 99.3], session="Race")
pace = sl.compare(recs_of(d))["setups"][0]["pace"]
check(pace["clean_laps"] == 3 and 104.0 not in pace["times"], "race start lap left out (it is the warm-up lap)")

# ---------------------------------------------------------------- grouping
d = folder("groups")
mk(d, "2026-11-01 18-00-00", PACE_A)
mk(d, "2026-11-01 18-30-00", PACE_A)
mk(d, "2026-11-02 18-00-00", PACE_B, setup=setup_b(), setup_name="")
mk(d, "2026-11-03 18-00-00", PACE_A)
g = sl.group(recs_of(d))
check([s["id"] for s in g["setups"]] == ["A", "B"] and g["setups"][1]["label"] == "Setup B", "setups named in order first used")
check([run["setup"] for run in g["runs"]] == ["A", "B", "A"] and len(g["runs"][0]["files"]) == 2, "runs are files in a row")
check(g["setups"][0]["first"] == "2026-11-01 18:00" and g["setups"][0]["last"] == "2026-11-03 18:00", "setup dates")

# ---------------------------------------------------------------- suggestions
L = [99.5] * 6
L13 = [99.5] * 13
d = folder("camber")
hot_in = {"LF": (70, 78, 86), "RF": (71, 78, 85), "LR": (70, 73, 76), "RR": (70, 73, 76)}
hot_in2 = {"LF": (72, 80, 88), "RF": (72, 80, 87), "LR": (71, 74, 77), "RR": (71, 74, 77)}
mk(d, "2026-11-01 18-00-00", L13, pits=(5, 11), pit_temps=[(5, hot_in), (11, hot_in2)], setup=cold_setup())
s = sl.suggestions(recs_of(d), car="mclaren570sgt4", series_setup="open")
cam = [i for i in s["items"] if i["rule"] == "camber"]
check(len(cam) == 1 and cam[0]["setting"] == "Front camber" and cam[0]["direction"] == "less negative",
      f"inner hot -> less negative front camber {[i['text'] for i in cam]}")
check(cam and cam[0]["confidence"] == "low-medium" and "-4.0 → -3.9" in cam[0]["text"] and "0.1°" in cam[0]["action"],
      "temperature ideas are low-medium, one smallest step with the current and new value")
check(cam and cam[0]["action"].endswith("Then compare again.") and "Lap time beats temperature rules" in cam[0]["check"],
      "says compare again, and that lap time beats temperature rules")
check(cam and "after stints of 5+ laps" in cam[0]["data"] and "6 to 10 °C" in cam[0]["why"], "says which data and target it used")
check(cam and not cam[0]["check"].startswith("Check") and " Check: after a 5-lap run" in cam[0]["text"],
      "the check has no 'Check:' of its own (the window adds the label); the plain text keeps one")
check(s["pit_readings"] == 2, f"two pit readings after 5+ lap stints ({s['pit_readings']})")

d = folder("camber_short")
mk(d, "2026-11-01 18-00-00", L, pits=(2,), pit_temps=[(2, hot_in)], setup=cold_setup())
s = sl.suggestions(recs_of(d), series_setup="open")
check(not [i for i in s["items"] if i["rule"] == "camber"] and s["readings_left_out"]["short"] == 1,
      "a reading after a 3-lap stint is not used")
d = folder("camber_carried")
mk(d, "2026-11-01 18-00-00", L, pit_temps=[(-1, hot_in)], setup=cold_setup())
s = sl.suggestions(recs_of(d), series_setup="open")
check(s["readings_left_out"]["no_stint"] == 1 and s["pit_readings"] == 0, "a reading carried in from another stop is not used")

d = folder("camber_low")
mk(d, "2026-11-01 18-00-00", L, pits=(5,), setup=cold_setup(),
   pit_temps=[(5, {"LF": (70, 74, 78), "RF": (70, 74, 78), "LR": (80, 76, 72), "RR": (72, 76, 78)})])
s = sl.suggestions(recs_of(d), series_setup="open")
cam = [i for i in s["items"] if i["rule"] == "camber"]
check(len(cam) == 1 and cam[0]["setting"] == "Left rear camber" and cam[0]["direction"] == "more negative",
      f"outer hotter -> more negative {[i['setting'] for i in cam]}")
check(cam and cam[0]["why"].startswith("Outside edge of the left rear ran 8 °C hotter than the inside."), cam and cam[0]["why"])
d = folder("camber_flat")
mk(d, "2026-11-01 18-00-00", L, pits=(5,), setup=cold_setup(),
   pit_temps=[(5, {"LF": (76, 77, 78), "RF": (76, 77, 78), "LR": (72, 75, 78), "RR": (72, 75, 78)})])
cam = [i for i in sl.suggestions(recs_of(d), series_setup="open")["items"] if i["rule"] == "camber"]
check(cam and cam[0]["why"] == "The inside of both front tyres was only 2 °C hotter than the outside (left 2, right 2). "
      "For a GT car it is usually 6 to 10 °C hotter.", f"inside only slightly hotter: plain wording ({cam and cam[0]['why']})")

d = folder("crown")
mk(d, "2026-11-01 18-00-00", L, pits=(5,), setup=cold_setup(),
   pit_temps=[(5, {"LF": (76, 86, 84), "RF": (76, 86, 84), "LR": (84, 76, 86), "RR": (75, 78, 80)})])
s = sl.suggestions(recs_of(d), series_setup="open")
pt = {i["setting"]: i for i in s["items"] if i["rule"] == "pressure_from_temps"}
check("Front starting pressure" in pt and pt["Front starting pressure"]["direction"] == "lower", f"hot middle -> lower pressure {list(pt)}")
check("Left rear starting pressure" in pt and pt["Left rear starting pressure"]["direction"] == "higher", "cool middle -> higher pressure")
check(pt and "176 → 174 kPa" in pt["Front starting pressure"]["text"], "smallest step from the setup's starting pressure")
check(not [i for i in s["items"] if i["rule"] == "camber"], "in-target spreads give no camber idea")
check(all(i["confidence"] == "low-medium" for i in pt.values()) and not [i for i in s["items"] if i["rule"] == "axle_pressure"],
      "temperature pressure idea stays separate from the hot-pressure rule")

d = folder("cold")
mk(d, "2026-11-01 18-00-00", L, pits=(5,), setup=cold_setup(),
   pit_temps=[(5, {"LF": (44, 44, 44), "RF": (45, 46, 47), "LR": (44, 44, 44), "RR": (44, 44, 44)})])
s = sl.suggestions(recs_of(d), series_setup="open")
check(not [i for i in s["items"] if i["rule"] in ("camber", "pressure_from_temps")], "cold readings give no tyre ideas")
check(s["cold_readings_ignored"] >= 1 and any("cold tyres" in n for n in s["notes"]), "cold readings counted and noted")
check(s["pit_readings"] == 0 and any("pit stall" in n for n in s["notes"]), "says how to get a real reading")

d = folder("hotp")
mk(d, "2026-11-01 18-00-00", L, pressure={"LF": 195, "RF": 186, "LR": 185, "RR": 185})
mk(d, "2026-11-01 19-00-00", L, pressure={"LF": 195, "RF": 186, "LR": 185, "RR": 185})
s = sl.suggestions(recs_of(d), car="mclaren570sgt4", series_setup="open")
hp = [i for i in s["items"] if i["rule"] == "hot_pressure"]
ax = [i for i in s["items"] if i["rule"] == "axle_pressure"]
check(hp and hp[0]["values"]["LF"] == 195 and "fastest point of each lap" in hp[0]["data"] and "lap 3 on" in hp[0]["data"],
      "hot pressures at the fastest point, laps 3+")
check(len(ax) == 1 and ax[0]["corners"] == ["LF"] and ax[0]["direction"] == "lower" and "176 → 168 kPa" in ax[0]["action"]
      and "rises from 176 kPa cold to 195 hot" in ax[0]["action"], f"9 kPa off its partner -> change that tyre, sized from its rise ({ax[0]['action'] if ax else ''})")
check(ax and ax[0]["confidence"] == "reading" and s["next"] is ax[0] and ax[0]["action"].endswith("Then compare again."),
      "the hot-pressure change is the one change to try next")
check(not [i for i in s["items"] if i["rule"] == "hot_pressure_target"], "no invented target window")
s = sl.suggestions(recs_of(d), car="mclaren570sgt4", series_setup="open", targets={"mclaren570sgt4": (180, 190)})
tg = [i for i in s["items"] if i["rule"] == "hot_pressure_target"]
check(len(tg) == 1 and tg[0]["direction"] == "lower", "his own target window is used when given")
d = folder("hotp_even")
mk(d, "2026-11-01 18-00-00", L, pressure={"LF": 190, "RF": 185, "LR": 185, "RR": 185})
s = sl.suggestions(recs_of(d), series_setup="open")
check(not [i for i in s["items"] if i["rule"] == "axle_pressure"], "5 kPa apart is within the 7 kPa rule")

d = folder("learned")
for k, (pace, lf) in enumerate([(99.0, 186), (99.1, 186), (99.6, 194), (99.6, 194), (99.7, 195), (99.7, 195),
                                (99.8, 196), (99.8, 196)]):
    mk(d, f"2026-11-0{k + 1} 18-00-00", [pace] * 6, pressure={"LF": lf, "RF": lf, "LR": 185, "RR": 185})
recs = recs_of(d)
lt = sl.learned_target(recs)
check(lt and lt["values"]["LF"] == 186 and lt["stints"] == 2 and lt["of"] == 8, f"target learned from the fastest stints {lt}")
s = sl.suggestions(recs[-1:], series_setup="open", history=recs)
hp = [i for i in s["items"] if i["rule"] == "hot_pressure"]
lg = [i for i in s["items"] if i["rule"] == "learned_target_gap" and i["corners"] == ["LF"]]
check(not [i for i in s["items"] if i["rule"] == "learned_target"] and len(hp) == 1 and hp[0]["setting"] == "Hot tyre pressures",
      "one 'Hot tyre pressures' card, not a second learned-target item")
check(hp and hp[0]["lines"][0].startswith("Now: left front 196") and len(hp[0]["lines"]) == 2
      and hp[0]["lines"][1].startswith("In your fastest stints here (2 of 8): left front 186")
      and "not an official target" in hp[0]["why"] and not hp[0]["check"], f"now + fastest stints, labelled as his own ({hp and hp[0]['lines']})")
check(lg and lg[0]["direction"] == "lower" and lg[0]["confidence"] == "guess", "a tyre far from his fastest stints gets a change")
# the fastest-stint line is left out: from 1 stint, from the run itself, or within 2 kPa of now
s = sl.suggestions(recs[-1:], series_setup="open", history=recs[:1] + recs[2:])
hp = [i for i in s["items"] if i["rule"] == "hot_pressure"][0]
check(len(hp["lines"]) == 1 and hp["learned"] is None and not [i for i in s["items"] if i["rule"] == "learned_target_gap"],
      "a learned line from one stint is not shown")
s = sl.suggestions(recs[:1], series_setup="open", history=recs)
hp = [i for i in s["items"] if i["rule"] == "hot_pressure"][0]
check(len(hp["lines"]) == 1, "a learned line that includes the current run's stints is not shown")
d = folder("learned_same")
for k, (pace, lf) in enumerate([(99.0, 187), (99.1, 187), (99.6, 188), (99.6, 188), (99.7, 188), (99.7, 188),
                                (99.8, 188), (99.8, 188)]):
    mk(d, f"2026-11-0{k + 1} 18-00-00", [pace] * 6, pressure={"LF": lf, "RF": lf, "LR": 185, "RR": 185})
recs2 = recs_of(d)
hp = [i for i in sl.suggestions(recs2[-1:], series_setup="open", history=recs2)["items"] if i["rule"] == "hot_pressure"][0]
check(len(hp["lines"]) == 1, "within 2 kPa of now: the learned line says nothing new and is left out")
check(sl._cold_step(9, 176, 195) == 8 and sl._cold_step(20, 100, 200) == 13,
      "cold step from absolute pressures (gauge + 101.3 kPa), not gauge ratios")
d = folder("balance")
mk(d, "2026-11-01 18-00-00", L, pressure={"LF": 195, "RF": 195, "LR": 185, "RR": 185})
bal = [i for i in sl.suggestions(recs_of(d), series_setup="open")["items"] if i["rule"] == "pressure_balance"]
check(bal and bal[0]["setting"] == "Front vs rear (hot)" and bal[0]["kind"] == "info", "front/rear balance is its own info item")


def bump(li, p):
    deep = 2.0 if li % 2 == 0 else 0.2                       # even laps hit a 50 mm floor around 30-35% of the lap
    return max(0.050, 0.077 - deep * max(0.0, 0.05 - abs(p - 0.325)))


d = folder("bottom")
mk(d, "2026-11-01 18-00-00", [99.5] * 12, ride=bump)
s = sl.suggestions(recs_of(d), series_setup="open")
bt = [i for i in s["items"] if i["rule"] == "bottoming"]
check(bt and bt[0]["kind"] == "info" and "30 to 35% of the lap" in bt[0]["why"] and "on 6 of 12 clean laps" in bt[0]["why"]
      and "doesn't cost time" in bt[0]["why"], f"bottoming with no time lost -> inform only {[i['why'] for i in bt]}")
d = folder("bottom_slow")
mk(d, "2026-11-01 18-00-00", [100.5 if k % 2 == 0 else 99.5 for k in range(12)], ride=bump)
s = sl.suggestions(recs_of(d), series_setup="open")
bt = [i for i in s["items"] if i["rule"] == "bottoming"]
check(bt and bt[0]["kind"] == "change" and "may be bottoming" in bt[0]["action"] and "slower through that part" in bt[0]["why"],
      f"bottoming that lines up with time loss -> a change {[i['text'] for i in bt]}")
fl = sl.learned_floor(recs_of(d))
check(abs(fl["LF"] - 50.0) < 0.5, f"floor learned as the 0.5th percentile ({fl})")
d = folder("nobottom")
mk(d, "2026-11-01 18-00-00", L)
s = sl.suggestions(recs_of(d), series_setup="open")
check(not [i for i in s["items"] if i["rule"] == "bottoming"], "a flat (unmoving) trace is not bottoming")

d = folder("fuel")
mk(d, "2026-11-01 18-00-00", [99.0] * 5, fuel=(58.0, 2.9))
s = sl.suggestions(recs_of(d), series_setup="open", race_minutes=30)
fu = [i for i in s["items"] if i["rule"] == "fuel"]
check(fu and abs(fu[0]["per_lap_l"] - 2.9) < 0.02 and fu[0]["confidence"] == "reading", "fuel per lap from the fuel level")
check(fu and "about 20 laps" in fu[0]["why"] and fu[0]["direction"] == "more" and "Add about" in fu[0]["action"],
      f"race need vs setup fuel ({fu[0]['text'] if fu else ''})")
check(s["next"] is None, "fuel is not offered as the setup change to try")
s = sl.suggestions(recs_of(d), series_setup="open")
fu = [i for i in s["items"] if i["rule"] == "fuel"]
check(fu and fu[0]["kind"] == "info" and "about 2.90 L a lap" in fu[0]["why"], "without a race length: just the per-lap use")
d = folder("fuel_race")
mk(d, "2026-11-01 18-00-00", [99.0] * 5, fuel=(58.0, 2.9), session=["Practice", "Race"], session_secs=1800)
s = sl.suggestions(recs_of(d), series_setup="open")
fu = [i for i in s["items"] if i["rule"] == "fuel"]
check(fu and "30-minute race" in fu[0]["why"], "race length read from the file's Race session")

s = sl.suggestions(recs_of(folder("camber")), series_setup="fixed")
check(len(s["items"]) == 1 and s["items"][0]["kind"] == "note" and "Fixed setup series" in s["items"][0]["text"], "fixed series -> one note")
s = sl.suggestions(recs_of(folder("camber")), series_setup=None)
check(s["items"][0]["rule"] == "unknown_series" and len(s["items"]) > 1, "unknown series -> note, then ideas")
check(sl.series_setup_kind(None) == "unknown" and sl.series_setup_kind(None, "open") == "open"
      and sl.series_setup_kind("fixed") == "fixed", "series setup kind with his override")
json.dumps(s)
check(True, "suggestions are JSON-ready")

# ---------------------------------------------------------------- file safety
sd = folder("safety")
rnd = random.Random(7)
clean = sd / "good.sto"
clean.write_bytes(bytes(rnd.getrandbits(8) for _ in range(4096)))
check(sl.check_setup_file(clean)["ok"], f"a normal (encrypted-looking) .sto is fine {sl.check_setup_file(clean)['problems']}")
(sd / "mz.sto").write_bytes(b"MZ\x90\x00" + b"\0" * 60 + b"This program cannot be run in DOS mode" + b"\0" * 500)
r = sl.check_setup_file(sd / "mz.sto")
check(not r["ok"] and any("Windows program" in x for x in r["problems"]), "a .sto that is a program is refused")
(sd / "ps.sto").write_bytes(bytes(rnd.getrandbits(8) for _ in range(200)) + b"powershell -enc AAAA")
check(not sl.check_setup_file(sd / "ps.sto")["ok"], "script text in a .sto is refused")
(sd / "hidden.sto").write_bytes(bytes(100) + b"This program cannot be run in DOS mode" + bytes(100))
check(not sl.check_setup_file(sd / "hidden.sto")["ok"], "an embedded program is refused")
(sd / "empty.sto").write_bytes(b"")
check(not sl.check_setup_file(sd / "empty.sto")["ok"], "an empty .sto is refused")
(sd / "big.sto").write_bytes(b"\x01" * (sl.MAX_STO + 10))
check(not sl.check_setup_file(sd / "big.sto")["ok"], "an oversized .sto is refused")
noise = bytearray(rnd.getrandbits(8) for _ in range(8192))
noise[3000:3003] = b"#!/"
(sd / "shebang_inside.sto").write_bytes(bytes(noise))
check(sl.check_setup_file(sd / "shebang_inside.sto")["ok"], "'#!/' in the middle of encrypted bytes is not a script")
(sd / "shebang.sto").write_bytes(b"#!/bin/sh\nrm -rf ~\n")
r = sl.check_setup_file(sd / "shebang.sto")
check(not r["ok"] and any("starts with '#!'" in x for x in r["problems"]), "a file starting with #! is a script")
(sd / "setup.exe.sto").write_bytes(clean.read_bytes())
check(not sl.check_setup_file(sd / "setup.exe.sto")["ok"], "a double extension is refused")
(sd / "x.exe").write_bytes(b"MZ")
check(not sl.check_setup_file(sd / "x.exe")["ok"], "an .exe is refused")

with zipfile.ZipFile(sd / "pack.zip", "w") as z:
    z.writestr("GT4/zolder.sto", clean.read_bytes())
    z.writestr("GT4/readme.txt", "baseline for Zolder")
    z.writestr("GT4/sheet.htm", "<html><body><table><tr><td>Camber</td></tr></table></body></html>")
r = sl.check_setup_file(sd / "pack.zip")
check(r["ok"] and r["kind"] == "zip" and len(r["members"]) == 3, f"a clean setup pack is fine {r['problems']}")
with zipfile.ZipFile(sd / "evil.zip", "w") as z:
    z.writestr("zolder.sto", clean.read_bytes())
    z.writestr("install.exe", b"MZ" + bytes(100))
r = sl.check_setup_file(sd / "evil.zip")
check(not r["ok"] and any("install.exe" in x for x in r["problems"]), "a zip with an .exe is refused")
with zipfile.ZipFile(sd / "trav.zip", "w") as z:
    z.writestr("../../Documents/iRacing/setups/x.sto", clean.read_bytes())
r = sl.check_setup_file(sd / "trav.zip")
check(not r["ok"] and any("outside the folder" in x for x in r["problems"]), "path traversal is refused")
with zipfile.ZipFile(sd / "disguised.zip", "w") as z:
    z.writestr("fast.sto", b"MZ" + bytes(200))
    z.writestr("macro.htm", "<script>new ActiveXObject('WScript.Shell')</script>")
r = sl.check_setup_file(sd / "disguised.zip")
check(not r["ok"] and len(r["problems"]) >= 2, "a program named .sto and a script in .htm inside a zip are refused")
(sd / "zip.sto").write_bytes((sd / "pack.zip").read_bytes())
r = sl.check_setup_file(sd / "zip.sto")
check(not r["ok"] and any("really a zip" in x for x in r["problems"]), "a .sto that is a zip is flagged")
before = sorted(p.name for p in sd.rglob("*"))
for f in sd.iterdir():
    sl.check_setup_file(f)
check(sorted(p.name for p in sd.rglob("*")) == before, "checking never extracts or writes anything")

# ---------------------------------------------------------------- report
d = folder("report")
mk(d, "2026-11-01 18-00-00", PACE_A, pit_temps=[(-1, hot_in)])
mk(d, "2026-11-01 19-00-00", PACE_A, pit_temps=[(-1, hot_in2)])
mk(d, "2026-11-02 18-00-00", PACE_B, setup=setup_b(), pit_temps=[(-1, hot_in)], fuel=(58.0, 2.9))
mk(d, "2026-11-02 19-00-00", PACE_B, setup=setup_b(), pit_temps=[(-1, hot_in2)], fuel=(58.0, 2.9))
mk(d, "2026-11-02 20-00-00", [80.0] * 3, car="mx5 mx52016", track="oulton fosters")
(d / "broken 2026-11-03 10-00-00.ibt").write_bytes(b"\0" * 50)
(d / "mclaren570sgt4_zolder gp 2026-11-03 11-00-00.ibt").write_bytes(b"\0" * 50)
rep = sl.report(d, "mclaren570sgt4", "zolder gp", series_setup=None, cache_path=TMP / "rep.json")
check([b["file"] for b in rep["bad_files"]] == ["mclaren570sgt4_zolder gp 2026-11-03 11-00-00.ibt"]
      and any(n.startswith("1 file for this car and track could not be read") for n in rep["notes"]),
      f"unreadable files: only this car + track's are on its report ({rep['bad_files']})")
other = sl.report(d, "mx5 mx52016", "oulton fosters", cache_path=TMP / "rep.json")
check(other["bad_files"] == [] and not any("could not be read" in n for n in other["notes"]), "...and not on another car's")
unk = [i for i in rep["suggestions"]["items"] if i["rule"] == "unknown_series"]
check(unk and unk[0]["text"].endswith("Set it above, under This series is."), "says where to set the series")
blob = json.dumps(rep, ensure_ascii=False)
check("\u2013" not in blob and "\u2014" not in blob, "no long dashes in anything Setup Lab shows")
check(rep["files"] == 4 and [s["id"] for s in rep["setups"]] == ["A", "B"], "report: this car + track only, two setups")
check(rep["diffs"] and rep["diffs"][0]["from"] == "A" and rep["diffs"][0]["to"] == "B"
      and any(c["label"] == "Front camber" for c in rep["diffs"][0]["changes"]), "report: diff between consecutive setups")
check(rep["compare"]["pairs"][0]["faster"] == "B", "report: compare verdict")
check(rep["suggestions"]["for_setup"] == "B" and rep["series_setup"] == "unknown", "report: suggestions for the latest setup")
check(any("could not be read" in n for n in rep["notes"]) and any(".sto" in n for n in rep["notes"]), "report: data notes")
check(rep["car"] == "McLaren 570S GT4" and rep["track"] == "Circuit Zolder (Grand Prix)", "report: names")   # 2.18: names.car / names.track
json.dumps(rep)
check(True, "report is JSON-ready")
cb = sl.combos(sl.index(d))
check(len(cb) == 2 and cb[0]["car_path"] == "mx5 mx52016", "combos: every car + track, newest first")
empty = sl.report(d, "nocar", "notrack", idx=sl.index(d))
check(empty["files"] == 0 and any("No telemetry" in n for n in empty["notes"]), "report: nothing for this car + track")

d = folder("fixedfile")
mk(d, "2026-11-01 18-00-00", PACE_A, fixed_setup=True, official=True, setup_name="fixed.sto", setup_load="fixed",
   setup_modified=0)
mk(d, "2026-11-01 19-00-00", PACE_A, fixed_setup=False, official=False)      # an unofficial session never decides
rep = sl.report(d, "mclaren570sgt4", "zolder gp", series_setup=None)
check(rep["series_setup"] == "fixed" and "official session" in rep["series_setup_source"]
      and rep["suggestions"]["items"][0]["rule"] == "fixed_series", "schedule says nothing: the official file's IsFixedSetup decides")
rep = sl.report(d, "mclaren570sgt4", "zolder gp", series_setup="open")
check(rep["series_setup"] == "open" and rep["series_setup_source"] == "series setting", "the series setting / his override wins")
d = folder("testonly")
mk(d, "2026-11-01 18-00-00", PACE_A, fixed_setup=True, official=False)
rep = sl.report(d, "mclaren570sgt4", "zolder gp")
check(rep["series_setup"] == "unknown" and "test or unofficial" in rep["series_setup_source"],
      "a test session saying 'fixed' does not decide the series")
d = folder("changed")
mk(d, "2026-11-01 18-00-00", PACE_A, setup_name="fixed.sto", setup_load="user", setup_modified=1, fixed_setup=False, official=True)
rep = sl.report(d, "mclaren570sgt4", "zolder gp")
check(rep["setups"][0]["label"] == "Setup A (fixed.sto, changed)" and rep["series_setup"] == "open",
      f"iRacing's setup loaded and changed is labelled so ({rep['setups'][0]['label']})")

print("FAILED" if fails else "ALL OK", fails)
sys.exit(1 if fails else 0)
