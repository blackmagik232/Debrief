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
check(cmp_["pairs"][0]["verdict"] == "not enough to tell", "one run each -> not enough to tell")
check(any("more run" in m for m in cmp_["pairs"][0]["missing"]), f"says what is missing: {cmp_['pairs'][0]['missing']}")

d = folder("gate_ok")
mk(d, "2026-11-01 18-00-00", PACE_A)
mk(d, "2026-11-01 19-00-00", PACE_A + [120.0])          # a traffic lap
mk(d, "2026-11-02 18-00-00", PACE_B, setup=setup_b())
mk(d, "2026-11-02 19-00-00", PACE_B, setup=setup_b())
cmp_ = sl.compare(recs_of(d))
p = cmp_["pairs"][0]
check(p["verdict"] == "faster" and p["faster"] == "B", f"enough laps + clear gap -> verdict ({p['text']})")
check("faster than Setup A (baseline.sto) by about 0.5 s a lap" in p["text"], "plain wording")
sa = cmp_["setups"][0]["pace"]
check(sa["traffic_laps"] == 1 and sa["clean_laps"] == 8 and abs(sa["median"] - 99.5) < 0.01, "traffic lap (>107%) left out")
check(cmp_["setups"][0]["enough"] and cmp_["setups"][1]["enough"], "both setups pass the gate")

d = folder("gate_same")
mk(d, "2026-11-01 18-00-00", PACE_A)
mk(d, "2026-11-01 19-00-00", PACE_A)
mk(d, "2026-11-02 18-00-00", [x - 0.05 for x in PACE_A], setup=setup_b())
mk(d, "2026-11-02 19-00-00", [x - 0.05 for x in PACE_A], setup=setup_b())
p = sl.compare(recs_of(d))["pairs"][0]
check(p["verdict"] == "no clear difference" and "No clear difference" in p["text"], "0.05 s gap -> no clear difference")

d = folder("gate_temp")
mk(d, "2026-11-01 18-00-00", PACE_A, surface_temp=22.0)
mk(d, "2026-11-01 19-00-00", PACE_A, surface_temp=22.0)
mk(d, "2026-11-02 18-00-00", PACE_B, setup=setup_b(), track_temp=35.0)
mk(d, "2026-11-02 19-00-00", PACE_B, setup=setup_b(), track_temp=35.0)
p = sl.compare(recs_of(d))["pairs"][0]
check(p["verdict"] == "not enough to tell" and not p["comparable"] and any("warmer" in m for m in p["missing"]),
      f"13 °C hotter track -> not comparable ({p['missing']})")

d = folder("gate_wet")
mk(d, "2026-11-01 18-00-00", PACE_A)
mk(d, "2026-11-01 19-00-00", PACE_A)
mk(d, "2026-11-02 18-00-00", PACE_B, setup=setup_b(), wet=True)
mk(d, "2026-11-02 19-00-00", PACE_B, setup=setup_b(), wet=True)
p = sl.compare(recs_of(d))["pairs"][0]
check(not p["comparable"] and any("wet track" in m for m in p["missing"]), "wet against dry -> not comparable")

d = folder("stints")
mk(d, "2026-11-01 18-00-00", [99.5, 99.4, 99.6, 101.0, 99.5, 99.4, 99.6, 99.5], pits=(3,))
r = recs_of(d)
pace = sl.compare(r)["setups"][0]
check(len(pace["pace"]["stints"]) == 2 and pace["enough"], "a pit stop splits one file into two runs (gate passes)")

d = folder("race")
mk(d, "2026-11-01 18-00-00", [104.0, 99.5, 99.1, 99.3], session="Race")
pace = sl.compare(recs_of(d))["setups"][0]["pace"]
check(pace["clean_laps"] == 3 and 104.0 not in pace["times"], "race start lap left out")

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
d = folder("camber")
hot_in = {"LF": (70, 78, 86), "RF": (71, 78, 85), "LR": (70, 73, 76), "RR": (70, 73, 76)}
hot_in2 = {"LF": (72, 80, 88), "RF": (72, 80, 87), "LR": (71, 74, 77), "RR": (71, 74, 77)}
mk(d, "2026-11-01 18-00-00", L, pits=(2,), pit_temps=[(-1, hot_in), (2, hot_in2)], setup=cold_setup())
s = sl.suggestions(recs_of(d), car="mclaren570sgt4", series_setup="open")
cam = [i for i in s["items"] if i["rule"] == "camber"]
check(len(cam) == 1 and cam[0]["setting"] == "Front camber" and cam[0]["direction"] == "less negative", f"inner hot -> less negative front camber {[i['text'] for i in cam]}")
check(cam and cam[0]["confidence"] == "reading" and "-4.0 → about -3." in cam[0]["text"] and "Check:" in cam[0]["text"],
      "two pit readings -> 'reading', says the current and new value and how to check")
check(cam and "pit reading" in cam[0]["data"] and "°C hotter" in cam[0]["why"], "says which data it used, with numbers")
check(s["pit_readings"] == 2, "two pit readings found from the channels")

d = folder("camber_low")
mk(d, "2026-11-01 18-00-00", L, pit_temps=[(-1, {"LF": (75, 75, 76), "RF": (75, 75, 76), "LR": (80, 76, 72), "RR": (75, 76, 80)})], setup=cold_setup())
s = sl.suggestions(recs_of(d), series_setup="open")
cam = [i for i in s["items"] if i["rule"] == "camber"]
check(len(cam) == 1 and cam[0]["setting"] == "Left rear camber" and cam[0]["direction"] == "more negative"
      and cam[0]["confidence"] == "guess", f"outer hotter -> more negative, one reading -> guess {[i['setting'] for i in cam]}")

d = folder("crown")
mk(d, "2026-11-01 18-00-00", L, pit_temps=[(-1, {"LF": (80, 88, 82), "RF": (80, 88, 82), "LR": (84, 76, 86), "RR": (75, 78, 80)})], setup=cold_setup())
s = sl.suggestions(recs_of(d), series_setup="open")
pt = {i["setting"]: i for i in s["items"] if i["rule"] == "pressure_from_temps"}
check("Front starting pressure" in pt and pt["Front starting pressure"]["direction"] == "lower", f"hot middle -> lower pressure {list(pt)}")
check("Left rear starting pressure" in pt and pt["Left rear starting pressure"]["direction"] == "higher", "cool middle -> higher pressure")
check("176 → about 172 kPa" in pt["Front starting pressure"]["text"], "uses the setup's starting pressure")
check(not [i for i in s["items"] if i["rule"] == "camber"], "in-target spreads give no camber idea")

d = folder("cold")
mk(d, "2026-11-01 18-00-00", L, pit_temps=[(-1, {"LF": (44, 44, 44), "RF": (45, 46, 47), "LR": (44, 44, 44), "RR": (44, 44, 44)})], setup=cold_setup())
s = sl.suggestions(recs_of(d), series_setup="open")
check(not [i for i in s["items"] if i["rule"] in ("camber", "pressure_from_temps")], "cold readings give no tyre ideas")
check(s["cold_readings_ignored"] >= 1 and any("cold tyres" in n for n in s["notes"]), "cold readings counted and noted")
check(s["pit_readings"] == 0 and any("pit stall" in n for n in s["notes"]), "says how to get a real reading")

d = folder("hotp")
mk(d, "2026-11-01 18-00-00", L, pressure={"LF": 195, "RF": 195, "LR": 185, "RR": 185})
mk(d, "2026-11-01 19-00-00", L, pressure={"LF": 195, "RF": 195, "LR": 185, "RR": 185})
s = sl.suggestions(recs_of(d), car="mclaren570sgt4", series_setup="open")
hp = [i for i in s["items"] if i["rule"] == "hot_pressure"]
bal = [i for i in s["items"] if i["rule"] == "pressure_balance"]
check(hp and hp[0]["values"]["LF"] == 195 and "left front 195" in hp[0]["why"], "hot pressures reported")
check(bal and "fronts run 10 kPa higher than the rears" in bal[0]["why"] and bal[0]["confidence"] == "reading", "front/rear imbalance")
check(not [i for i in s["items"] if i["rule"] == "hot_pressure_target"], "no invented target window")
s = sl.suggestions(recs_of(d), car="mclaren570sgt4", series_setup="open", targets={"mclaren570sgt4": (180, 190)})
tg = [i for i in s["items"] if i["rule"] == "hot_pressure_target"]
check(len(tg) == 2 and all(i["direction"] == "lower" for i in tg), "his own target window is used when given")


def bump(li, p):
    v = 0.077 - 1.2 * max(0.0, 0.05 - abs(p - 0.325))     # dips to a 50 mm floor around 30-35% of the lap
    return max(0.050, v)


d = folder("bottom")
mk(d, "2026-11-01 18-00-00", L, ride=bump)
s = sl.suggestions(recs_of(d), series_setup="open")
bt = [i for i in s["items"] if i["rule"] == "bottoming"]
check(bt and bt[0]["confidence"] == "guess" and "30–35% of the lap" in bt[0]["why"] and "may be bottoming" in bt[0]["action"],
      f"flat floor on the ride height -> may be bottoming {[i['why'] for i in bt]}")
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
rep = sl.report(d, "mclaren570sgt4", "zolder gp", series_setup=None, cache_path=TMP / "rep.json")
check(rep["files"] == 4 and [s["id"] for s in rep["setups"]] == ["A", "B"], "report: this car + track only, two setups")
check(rep["diffs"] and rep["diffs"][0]["from"] == "A" and rep["diffs"][0]["to"] == "B"
      and any(c["label"] == "Front camber" for c in rep["diffs"][0]["changes"]), "report: diff between consecutive setups")
check(rep["compare"]["pairs"][0]["faster"] == "B", "report: compare verdict")
check(rep["suggestions"]["for_setup"] == "B" and rep["series_setup"] == "unknown", "report: suggestions for the latest setup")
check(any("could not be read" in n for n in rep["notes"]) and any(".sto" in n for n in rep["notes"]), "report: data notes")
check(rep["car"] == "Mclaren 570s GT4" and rep["track"] == "Zolder Circuit - Grand Prix", "report: names")
json.dumps(rep)
check(True, "report is JSON-ready")
cb = sl.combos(sl.index(d))
check(len(cb) == 2 and cb[0]["car_path"] == "mx5 mx52016", "combos: every car + track, newest first")
empty = sl.report(d, "nocar", "notrack", idx=sl.index(d))
check(empty["files"] == 0 and any("No telemetry" in n for n in empty["notes"]), "report: nothing for this car + track")

print("FAILED" if fails else "ALL OK", fails)
sys.exit(1 if fails else 0)
