"""Debrief 2.26 "Stint plan": the endurance planner (endurance.py), its endpoints and window, and the perf meter
(perfmeter.py, the Coach page's CPU line and Copy diagnostics).

    python3 tests/ci_run.py tests/test_226_endurance.py

Every expected number below is worked out by hand in the comments, from the rules in endurance.py's docstring.
No network; temp folders from tempfile; the browser test is skipped without Playwright + Chromium.
"""
from __future__ import annotations

import json
import math
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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"

TMP = Path(tempfile.mkdtemp(prefix="debrief-226-"))
LIB = TMP / "lib"
(LIB / "reviews").mkdir(parents=True)
ST = TMP / "settings.toml"
ST.write_text(f'token = "PASTE-YOUR-GARAGE61-TOKEN-HERE"\nlibrary_folder = \'{LIB}\'\n', encoding="utf-8")
os.environ.update(DEBRIEF_SETTINGS=str(ST), DEBRIEF_CACHE_DIR=str(TMP / "cache"),
                  DEBRIEF_SCHEDULE_DIR=str(TMP / "sched"), DEBRIEF_UPDATE_STATE=str(TMP / "update.json"))

import requests  # noqa: E402


def _blocked(*a, **k):
    raise requests.ConnectionError("network blocked in tests")


requests.get = _blocked
requests.post = _blocked

from debrief import endurance as E  # noqa: E402
from debrief import perfmeter  # noqa: E402

D3 = [{"name": "Swazi", "lap_t": 100, "burn_l": 3.0}, {"name": "Josh", "lap_t": 101, "burn_l": 3.1},
      {"name": "Kenta", "lap_t": 100.5, "burn_l": 2.9}]
SIX_H = {"minutes": 360, "tank_l": 100, "reserve_laps": 1, "pit_lane_s": 30, "fuel_rate_lps": 2.5, "tyre_s": 20,
         "drivers": D3}


def raises(fn, text):
    try:
        fn()
    except ValueError as e:
        assert text in str(e), (text, str(e))
        return str(e)
    raise AssertionError(f"no ValueError ({text})")


# ================================================================ the planner, by hand
def test_six_hours_three_drivers_by_hand():
    p = E.plan(SIX_H)
    s = p["stints"]
    # Swazi: laps a tank = floor((100 - 1 x 3.0) / 3.0) = 32
    assert (s[0]["driver"], s[0]["laps"], s[0]["fuel_start_l"]) == ("Swazi", 32, 100.0)
    # stop 1: 100 - 32 x 3.0 = 4.0 L left. Josh next: laps a tank floor((100 - 3.1) / 3.1) = 31, needs (31 + 1) x 3.1 =
    # 99.2 L, so add 95.2 L. Stop = lane 30 + max(95.2 / 2.5 = 38.08, tyres 20) = 68.08 s (more than the 30 s swap)
    st1 = s[0]["stop"]
    assert st1 == {"fuel_add_l": 95.2, "tyres": True, "swap": True, "to": "Josh", "stop_s": 68.1}, st1
    assert s[1]["start_s"] == 3268.1 and s[1]["laps"] == 31 and s[1]["fuel_start_l"] == 99.2
    # rotation: Swazi, Josh, Kenta, Swazi, ...
    assert [x["driver"] for x in s] == ["Swazi", "Josh", "Kenta", "Swazi", "Josh", "Kenta", "Swazi"]
    # last stint: starts at 19696.4 s; laps left = floor((21600 - 19696.4) / 100) + 1 = 20; the stop before it adds only
    # (20 + 1) x 3.0 = 63.0 L minus what's left, and is a swap, so service is max(60.2 / 2.5 = 24.1, 20, 30) = 30 s
    assert s[-1]["laps"] == 20 and s[-1]["stop"] is None
    assert s[-2]["stop"]["stop_s"] == 60.0 and s[-2]["stop"]["fuel_add_l"] == 60.2
    assert p["laps"] == 212 and p["stops"] == 6 and p["finish_s"] == 21696.4 and p["finish_s"] >= 21600
    assert p["laps"] == sum(x["laps"] for x in s)
    # every lap is driven once, in order
    assert all(s[i]["to_lap"] + 1 == s[i + 1]["from_lap"] for i in range(len(s) - 1))
    # time adds up: driving + stops = finish
    drive = sum(x["laps"] * {"Swazi": 100, "Josh": 101, "Kenta": 100.5}[x["driver"]] for x in s)
    assert abs(drive + p["pit_time_s"] - p["finish_s"]) < 0.5
    # fair share: equal share 212 / 3 = 70.67 laps, a quarter = 17.67, rounded up = 18
    assert all(r["fair_share_laps"] == 18 and not r["short"] for r in p["drivers"])
    assert sum(r["laps"] for r in p["drivers"]) == 212


def test_fuel_never_runs_below_the_reserve():
    for mins in (90, 180, 360, 720, 1440):
        p = E.plan(dict(SIX_H, minutes=mins))
        burns = {d["name"]: d["burn_l"] for d in D3}
        for st in p["stints"]:
            left = st["fuel_start_l"] - st["laps"] * burns[st["driver"]]
            assert left >= burns[st["driver"]] - 0.11, (mins, st, left)       # one spare lap (0.1 L rounding)
            assert st["fuel_start_l"] <= 100.0 + 1e-9


def test_timed_race_counts_pit_time():
    """The sprint planner's rule (laps = minutes x 60 / lap + 1) ignores the stops. Over six hours with 68 s stops the
    car loses about 4 laps to the pits; the plan's lap count must show that."""
    p = E.plan(dict(SIX_H, drivers=[{"name": "Solo", "lap_t": 100, "burn_l": 3.0}]))
    naive = math.floor(360 * 60 / 100) + 1                     # 217
    assert p["laps"] < naive and naive - p["laps"] == math.floor(p["pit_time_s"] / 100 + 1e-9) or \
        naive - p["laps"] in (math.floor(p["pit_time_s"] / 100), math.ceil(p["pit_time_s"] / 100)), (p["laps"], p["pit_time_s"])
    assert p["finish_s"] >= 360 * 60 and p["finish_s"] - 100 < 360 * 60 + 1e-6


def test_review_fixes_timed_edges_and_availability():
    """The independent check's three bugs (2.26 review)."""
    # 1) the clock ends exactly on a crossing: 60 min at 90 s = 40 laps exactly, finish 3600 s (not 41 laps)
    p = E.plan({"minutes": 60, "tank_l": 100, "drivers": [{"name": "A", "lap_t": 90, "burn_l": 1}]})
    assert p["laps"] == 40 and p["finish_s"] == 3600.0
    # 2) the clock runs out during a stop: the car still crosses the line once more; the plan never ends on a stop
    p = E.plan({"minutes": 30, "tank_l": 40, "reserve_laps": 5, "pit_lane_s": 0, "fuel_rate_lps": 1,
                "drivers": [{"name": "A", "lap_t": 187, "burn_l": 4.43}, {"name": "B", "lap_t": 120, "burn_l": 5.6}]})
    assert p["stints"][-1]["stop"] is None and p["stints"][-1]["laps"] >= 1 and p["stops"] == len(p["stints"]) - 1
    assert p["finish_s"] >= 1800
    # 3) each candidate's own stint length is checked against their hours, and a stint outside them is always flagged
    p = E.plan({"laps": 200, "tank_l": 50, "reserve_laps": 0, "pit_loss_s": 30,
                "drivers": [{"name": "A", "lap_t": 60, "burn_l": 5, "avail": [[0, 5]]},
                            {"name": "B", "lap_t": 120, "burn_l": 1, "avail": [[0, 60]]}]})
    for st in p["stints"]:
        d = {"A": (0, 300), "B": (0, 3600)}[st["driver"]]
        if not (d[0] <= st["start_s"] and st["end_s"] <= d[1] + 1e-6):
            assert any(f"{st['driver']}'s stint from {E.clock(st['start_s'])}" in w for w in p["warnings"]), (st, p["warnings"])
    # random timed races: the finish is always the first crossing at or after the clock, and never a stop
    import random
    rnd = random.Random(7)
    for _ in range(400):
        n = rnd.randint(1, 4)
        ds = [{"name": f"D{i}", "lap_t": rnd.choice([60, 90, 100, 120]) + rnd.choice([0, 0.5, 1.37]),
               "burn_l": round(rnd.uniform(1, 6), 2)} for i in range(n)]
        mins = rnd.randint(10, 600)
        inp = {"minutes": mins, "tank_l": rnd.choice([40, 60, 100, 120]), "reserve_laps": rnd.choice([0, 1, 2]),
               "pit_lane_s": rnd.choice([0, 25, 40]), "fuel_rate_lps": rnd.choice([1, 2.5]), "drivers": ds}
        try:
            p = E.plan(inp)
        except ValueError:
            continue
        last = p["stints"][-1]
        lt = next(d["lap_t"] for d in ds if d["name"] == last["driver"])
        assert last["stop"] is None
        assert p["finish_s"] >= mins * 60 - 0.06, (inp, p["finish_s"])
        # the line was crossed before the clock ran out one lap earlier, unless the clock ran out while the car was in
        # the pits: then it does exactly one lap after the stop
        assert p["finish_s"] - lt < mins * 60 + 0.06 or (last["start_s"] >= mins * 60 - 0.06 and last["laps"] == 1), \
            (inp, p["finish_s"])


def test_lap_race_and_no_stop():
    # 20 laps at 3.0 L with a 100 L tank: (20 + 1) x 3.0 = 63 L, no stop, start with 63 L
    p = E.plan({"laps": 20, "tank_l": 100, "drivers": [{"name": "A", "lap_t": 90, "burn_l": 3.0}]})
    assert p["stops"] == 0 and p["laps"] == 20 and p["stints"][0]["fuel_start_l"] == 63.0 and p["finish_s"] == 1800.0
    assert p["options"] == []
    # 100 laps: 32 a tank -> 32 + 32 + 32 + 4: three stops
    p = E.plan({"laps": 100, "tank_l": 100, "pit_loss_s": 50, "drivers": [{"name": "A", "lap_t": 90, "burn_l": 3.0}]})
    assert p["stops"] == 3 and [x["laps"] for x in p["stints"]] == [32, 32, 32, 4]
    assert p["pit_time_s"] == 150.0 and p["finish_s"] == 100 * 90 + 150
    # stop 1 fills to (32 + 1) x 3.0 = 99 L (one spare lap, not a brim-full tank), so 3 L are left after stints
    # 2 and 3; the last stop adds only (4 + 1) x 3.0 = 15 L minus those 3 L = 12 L
    assert p["stints"][0]["stop"]["fuel_add_l"] == 95.0 and p["stints"][1]["fuel_start_l"] == 99.0
    assert p["stints"][2]["stop"]["fuel_add_l"] == 12.0


def test_stints_in_row_and_swaps():
    p = E.plan(dict(SIX_H, stints_in_row=2))
    names = [x["driver"] for x in p["stints"]]
    assert names[:6] == ["Swazi", "Swazi", "Josh", "Josh", "Kenta", "Kenta"], names
    # no swap between a driver's own stints: stop = 30 + max(add / 2.5, 20) with no 30 s floor needed
    st = p["stints"][0]["stop"]
    assert st["swap"] is False and st["to"] == "Swazi"


def test_swap_floor_30_seconds():
    # a tiny add with a swap still keeps the car 30 s in the stall
    p = E.plan({"laps": 40, "tank_l": 100, "pit_lane_s": 25, "fuel_rate_lps": 10, "tyre_s": 0, "tyre_stints": 0,
                "drivers": [{"name": "A", "lap_t": 90, "burn_l": 3.0}, {"name": "B", "lap_t": 90, "burn_l": 3.0}]})
    st = p["stints"][0]["stop"]
    # 32 laps then 8 more: add (8 + 1) x 3 - 4 = 23 L -> 2.3 s of fuel, but a swap: 25 + 30 = 55 s
    assert st["fuel_add_l"] == 23.0 and st["swap"] and st["stop_s"] == 55.0 and st["tyres"] is False


def test_tyres_every_other_stint_and_limit():
    p = E.plan(dict(SIX_H, tyre_stints=2, tyre_sets=3))
    tyres = [x["stop"]["tyres"] for x in p["stints"] if x["stop"]]
    assert tyres == [False, True, False, True, False, True], tyres
    assert p["tyre_sets_used"] == 4
    assert any("uses 4 sets of tyres; the limit is 3" in w for w in p["warnings"])
    p = E.plan(dict(SIX_H, tyre_stints=0))
    assert p["tyre_sets_used"] == 1 and not any(x["stop"]["tyres"] for x in p["stints"] if x["stop"])


def test_tyres_one_after_the_other():
    p = E.plan(dict(SIX_H, parallel_service=False))
    # 30 + 95.2 / 2.5 + 20 = 88.08
    assert p["stints"][0]["stop"]["stop_s"] == 88.1
    q = E.plan(SIX_H)
    assert p["pit_time_s"] > q["pit_time_s"]


def test_availability_skips_a_driver():
    # Josh can only drive race hours 3-6: the first stint after Swazi goes to Kenta instead
    d = [dict(D3[0]), dict(D3[1], avail=[[180, 360]]), dict(D3[2])]
    p = E.plan(dict(SIX_H, drivers=d))
    first_two = [x["driver"] for x in p["stints"][:2]]
    assert first_two == ["Swazi", "Kenta"], first_two
    for x in p["stints"]:
        if x["driver"] == "Josh":
            assert x["start_s"] >= 180 * 60 and x["end_s"] <= 360 * 60 + 1e-6, x
    # nobody free: planned anyway, with a warning
    d = [dict(D3[0], avail=[[0, 30]]), dict(D3[1], avail=[[0, 30]])]
    p = E.plan(dict(SIX_H, minutes=120, drivers=d))
    assert any("No driver is free" in w for w in p["warnings"]), p["warnings"]


def test_fair_share_and_driver_counts():
    # Kenta can only do the first stint: 32ish laps of 212 -> fine; make Kenta drive almost nothing instead
    d = [dict(D3[0]), dict(D3[1]), dict(D3[2], avail=[[0, 1]])]
    p = E.plan(dict(SIX_H, drivers=d, min_drivers=3))
    k = next(r for r in p["drivers"] if r["name"] == "Kenta")
    assert k["laps"] == 0 and k["short"] and k["fair_share_laps"] == math.ceil(0.25 * p["laps"] / 3)
    w = " | ".join(p["warnings"])
    assert "Kenta drives 0 laps; the fair share needs at least" in w
    assert "Only 2 drivers drive; the session needs at least 3." in w
    p = E.plan(dict(SIX_H, max_drivers=2))
    assert any("3 drivers drive; the session allows at most 2." in x for x in p["warnings"])
    p = E.plan(dict(SIX_H, drivers=d, fair_share=False))
    assert not any("fair share" in x for x in p["warnings"])


def test_options_fuel_save_and_tyres():
    p = E.plan(SIX_H)
    o = {x["key"]: x for x in p["options"]}
    assert o["save"]["stops"] == p["stops"] - 1 and o["save"]["label"].startswith("Save ")
    # check the save option by re-running it by hand over the same distance
    pct = float(o["save"]["label"].split()[1].rstrip("%"))
    cfg = E._read(SIX_H)
    ds = [dict(d, burn=d["burn"] * (1 - pct / 100), lap_t=d["lap_t"] + E.DEFAULT_SAVE_COST * pct) for d in cfg["drivers"]]
    same = E.simulate(cfg, target_laps=p["laps"])
    v = E.simulate(dict(cfg, drivers=ds), target_laps=p["laps"])
    assert v["stops"] == o["save"]["stops"] and abs((v["finish_s"] - same["finish_s"]) - o["save"]["delta_s"]) < 0.11
    # one step less of saving keeps the stop
    if pct > E.SAVE_STEP:
        ds2 = [dict(d, burn=d["burn"] * (1 - (pct - E.SAVE_STEP) / 100)) for d in cfg["drivers"]]
        assert E.simulate(dict(cfg, drivers=ds2), target_laps=p["laps"])["stops"] == same["stops"]
    # tyres change while refuelling (38 s of fuel > 20 s of tyres): double-stinting saves nothing
    assert o["tyres"]["delta_s"] == 0.0 and "while the car refuels" in o["tyres"]["detail"]
    # one after the other, double-stinting saves 20 s at every other stop
    p = E.plan(dict(SIX_H, parallel_service=False))
    t = next(x for x in p["options"] if x["key"] == "tyres")
    assert t["delta_s"] < -40 and t["better"], t


def test_whole_stop_fallback_and_unknown():
    p = E.plan({"minutes": 120, "tank_l": 60, "pit_loss_s": 55, "drivers": D3[:2]})
    assert all(x["stop"]["stop_s"] == 55.0 for x in p["stints"] if x["stop"])
    assert any("Driver swaps keep the car in the stall" in w for w in p["warnings"])
    p = E.plan({"minutes": 120, "tank_l": 60, "drivers": D3[:1]})
    assert p["pit_time_s"] == 0.0 and any("Pit stop time unknown" in w for w in p["warnings"])


def test_bad_inputs_have_plain_messages():
    raises(lambda: E.plan({}), "Type the race length")
    raises(lambda: E.plan({"minutes": 60}), "Type the tank size")
    raises(lambda: E.plan({"minutes": 60, "tank_l": 50}), "Add at least one driver")
    raises(lambda: E.plan({"minutes": 60, "tank_l": 50, "drivers": [{"name": "A", "burn_l": 2}]}), "Type the lap time in seconds for A.")
    raises(lambda: E.plan({"minutes": 60, "tank_l": 5, "drivers": [{"name": "A", "lap_t": 90, "burn_l": 3}]}),
           "The tank is too small for one lap at A's fuel use.")
    raises(lambda: E.plan({"minutes": 60, "tank_l": 50, "drivers": [{"name": "A", "lap_t": 90, "burn_l": 3}] * 2}),
           "Two drivers are called A")
    raises(lambda: E.plan({"minutes": 60, "tank_l": 50, "drivers": [{"name": f"D{i}", "lap_t": 90, "burn_l": 3} for i in range(9)]}),
           "at most 8 drivers")
    raises(lambda: E.plan({"minutes": 60, "tank_l": 50, "drivers": [{"name": "A", "lap_t": 90, "burn_l": 3, "avail": [[5, 2]]}]}),
           "start before its end")
    raises(lambda: E.plan({"minutes": 60, "tank_l": 50, "parallel_service": "yes", "drivers": D3}), "Choose yes or no")
    raises(lambda: E.plan({"minutes": 60, "tank_l": 50, "min_drivers": 3, "max_drivers": 2, "drivers": D3}),
           "minimum drivers can't be more")
    raises(lambda: E.plan({"laps": 1.5, "tank_l": 50, "drivers": D3}), "whole number")
    raises(lambda: E.plan([1]), "Fill in the race details first")


def test_24_hours_is_quick_and_bounded():
    d6 = [{"name": f"D{k}", "lap_t": 98 + k * 0.4, "burn_l": 3.1 + k * 0.05} for k in range(6)]
    t0 = time.perf_counter()
    p = E.plan({"minutes": 1440, "tank_l": 120, "pit_lane_s": 32, "fuel_rate_lps": 2.6, "tyre_s": 22, "drivers": d6})
    took = time.perf_counter() - t0
    assert p["laps"] > 800 and len(p["stints"]) == p["stops"] + 1
    assert took < 2.0, took          # about 1 ms on a cloud box; generous for a slow Windows runner
    # the lap count never exceeds a 2000-stint loop: a 24 h race with a 1-lap tank is refused, not hung
    p = E.plan({"minutes": 1440, "tank_l": 6.2, "pit_loss_s": 10, "drivers": [{"name": "A", "lap_t": 60, "burn_l": 3}]})
    assert p["stops"] == p["laps"] - 1 or p["stops"] < 2000


def test_share_text():
    p = E.plan(SIX_H)
    t = E.share_text(p, "Spa 6h")
    lines = t.splitlines()
    assert lines[0] == "Stint plan: Spa 6h · 212 laps · 6 stops"
    assert lines[1] == "1. Swazi · laps 1-32 · from 0:00 → box: add 95.2 L, tyres, swap to Josh"
    assert lines[-1] == "Planned in Debrief · by Black Magik Studios"


def test_saved_inputs_round_trip():
    lib = TMP / "lib-save"
    r = E.save_inputs(lib, "Spa-Francorchamps (Grand Prix)", SIX_H, "Spa 6h")
    assert r["saved"].endswith(".json") and (lib / "endurance" / r["saved"]).is_file()
    got = E.load_inputs(lib, "Spa-Francorchamps (Grand Prix)")
    assert got["inputs"] == SIX_H and got["title"] == "Spa 6h"
    assert E.load_inputs(lib, "Nowhere") is None
    raises(lambda: E.save_inputs(lib, "Spa", {"minutes": 60}), "Type the tank size")
    (lib / "endurance" / r["saved"]).write_text("{broken", encoding="utf-8")
    assert E.load_inputs(lib, "Spa-Francorchamps (Grand Prix)") is None


# ================================================================ perf meter
def test_perfmeter_counts_cpu_and_slow_steps():
    wall = [0.0]
    cpu = [0.0]
    logs = []
    m = perfmeter.PerfMeter("coach", on_slow=logs.append, wall=lambda: wall[0], cpu=lambda: cpu[0])
    for i in range(100):                 # 20 Hz, each step 1 ms of CPU
        with m.step():
            wall[0] += 0.001
            cpu[0] += 0.001
        wall[0] += 0.049
    s = m.summary()
    assert abs(s["cpu_pct"] - 2.0) < 0.1 and s["step_ms"] == 1.0 and s["slow_steps"] == 0 and s["steps"] == 100, s
    with m.step():                       # one 120 ms stall
        wall[0] += 0.12
        cpu[0] += 0.01
    with m.step():                       # another, within a minute: counted, not logged again
        wall[0] += 0.08
    assert m.summary()["slow_steps"] == 2 and len(logs) == 1 and "one step took 120 ms" in logs[0]
    assert perfmeter.words({"cpu_pct": 0.05, "step_ms": 0.3, "slow_steps": 0}) == \
        "Using under 0.1% of one CPU core (0.3 ms a step)."
    assert perfmeter.words({"cpu_pct": 1.24, "step_ms": 2.0, "slow_steps": 2}).endswith(
        "2 slow steps (over 50 ms): see debrief.log.")
    assert perfmeter.words(None) == "" and perfmeter.words({"cpu_pct": None}) == ""


def test_perfmeter_memory_reads():
    mb = perfmeter.memory_mb()
    assert mb is None or 5 < mb < 4000, mb


def test_live_runner_reports_perf():
    """The live coach's status carries the meter; a played-back file (rehearsal) is not measured."""
    import fake_ibt as fi
    from debrief import livecoach as lc
    from debrief import voice as vc
    p = str(TMP / "perf.ibt")
    fi.make(p, [92.0, 92.1, 92.2])
    r = lc.LiveRunner(lc.IbtPlaybackSource(p, speed=0), vc.FakeVoice(), mode="live")
    r.start()
    t0 = time.time()
    while r.running() and time.time() - t0 < 60:
        time.sleep(0.05)
    r.stop()
    st = r.status()
    assert st["perf"]["steps"] > 100 and st["perf"]["step_ms"] is not None, st["perf"]
    r2 = lc.LiveRunner(lc.IbtPlaybackSource(p, speed=0), vc.FakeVoice(), mode="rehearsal")
    r2.start()
    t0 = time.time()
    while r2.running() and time.time() - t0 < 60:
        time.sleep(0.05)
    r2.stop()
    assert r2.status()["perf"]["steps"] == 0


def test_perf_audit_tool_runs():
    sys.path.insert(0, str(ROOT / "tools"))
    import perf_audit
    res, checks = perf_audit.run()
    assert res["planner"].get("worst_ms") is not None
    assert all(len(c) == 4 for c in checks)
    over = [c for c in checks if not c[2]]
    assert not over, over


# ================================================================ endpoints and window
_SRV = {}
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def srv():
    if "s" not in _SRV:
        from debrief.desktop import server as S
        s = S.DebriefServer(heartbeat_timeout=3600)
        threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
        _SRV["s"] = s
    return _SRV["s"]


def call(rel, body=None, headers=None):
    h = {"X-Debrief": "1", "Content-Type": "application/json"} if headers is None else headers
    rq = urllib.request.Request(srv().url + rel, data=None if body is None else json.dumps(body).encode("utf-8"),
                                method="GET" if body is None else "POST", headers=h)
    try:
        with OP.open(rq, timeout=20) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}")
        except ValueError:
            return e.code, {}


def test_api_endurance_plan_save_text():
    code, j = call("api/endurance", {"action": "plan", "inputs": SIX_H})
    assert code == 200 and j["ok"] and j["plan"]["laps"] == 212, j
    code, j = call("api/endurance", {"action": "plan", "inputs": {"minutes": 60}})
    assert code == 400 and j["error"] == "Type the tank size (litres)."
    code, j = call("api/endurance", {"action": "text", "title": "Spa", "inputs": SIX_H})
    assert code == 200 and j["text"].startswith("Stint plan: Spa · 212 laps")
    code, j = call("api/endurance", {"action": "save", "track": "Spa", "title": "Spa 6h", "inputs": SIX_H})
    assert code == 200 and j["ok"], j
    code, j = call("api/endurance?track=Spa")
    assert code == 200 and j["saved"]["inputs"] == SIX_H and j["saved"]["title"] == "Spa 6h"
    code, j = call("api/endurance?track=Elsewhere")
    assert code == 200 and j["saved"] is None
    for bad in ({"action": "nuke"}, {"action": "plan"}, {"action": "plan", "inputs": [1]}):
        code, j = call("api/endurance", bad)
        assert code == 400 and j["ok"] is False
    assert call("api/endurance?track=Spa", headers={})[0] == 404      # never without the app's header


def test_api_diagnostics_has_memory():
    code, j = call("api/diagnostics")
    assert code == 200 and j["ok"] and "memory_mb" in j and "coach_perf" in j, list(j)


def test_window_stint_planner():
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # noqa: BLE001
        print("  (skipped: no Playwright)")
        return
    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch()
            pg = b.new_page(viewport={"width": 1280, "height": 900})
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.route("**/fonts.googleapis.com/**", lambda r: r.abort())
            pg.route("**/fonts.gstatic.com/**", lambda r: r.abort())
            week = {"week": 7, "start": "2026-10-27", "track": "Spa", "length": "6 hours", "race_time": None,
                    "conditions": None, "history": None, "track_id": 1, "car_id": 2, "card": {"fields": {}, "car_notes": {}},
                    "has_notes": False, "shared_notes": 0}
            prepare = {"ok": True, "schedule": {"season": "2026 Season 4", "series": 1, "weeks": 12, "first_day": "2026-09-15",
                                                "last_day": "2026-12-07", "current_week": 7, "out_of_date": False, "warnings": []},
                       "chosen": True, "current_week": 7, "suggestions": [], "all": [], "token": False,
                       "mine": [{"id": "gt3-endu", "name": "GT Endurance", "missing": False, "category": "Sports Car",
                                 "cars": ["BMW M4 GT3"], "classes": ["GT3"], "multiclass": False, "setup": "open",
                                 "this_week": week, "next_week": None, "season": []}],
                       "catalog": {"current_week": 7, "tracks": ["Spa"], "driven": [], "cars": [{"name": "BMW M4 GT3", "class": "GT3"}],
                                   "series": [{"id": "gt3-endu", "name": "GT Endurance", "category": "Sports Car", "cars": ["BMW M4 GT3"],
                                               "starred": True, "suggested": False,
                                               "weeks": [{"no": 7, "week": 7, "start": "2026-10-27", "track": "Spa", "short": "Spa", "left": True}]}]}}
            focus = {"ok": True, "track": "Spa", "car": "BMW M4 GT3", "series": {"id": "gt3-endu", "name": "GT Endurance", "setup": "open"},
                     "week": {"no": 7, "week": 7, "start": "2026-10-27", "now": True, "next": False, "cars": ["BMW M4 GT3"], "car_runs": True},
                     "race": {"length": "6 hours", "rain": None, "start_type": None, "cautions": None}, "pace": None, "status": None,
                     "corners": [], "warmups": [], "card": {"fields": {}, "car_notes": {}}, "has_notes": False, "shared_notes": 0,
                     "track_id": 1, "car_id": 2, "racecraft": None, "rivals": [], "racecraft_note": None, "racecraft_wait": None,
                     "page": None, "plan": {"state": "ok", "notes": [], "weeks": []}}
            pg.route("**/api/prepare", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(prepare)))
            pg.route("**/api/prepfocus*", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(focus)))
            call("api/endurance", {"action": "save", "track": "Elsewhere-x", "inputs": SIX_H})
            pg.goto(srv().url)
            pg.wait_for_selector(".navbtn[data-nav=prep]", timeout=15000)
            pg.click(".navbtn[data-nav=prep]")
            pg.wait_for_selector("#pvEndu", timeout=10000)
            pg.click("#pvEndu")
            pg.wait_for_function("() => document.getElementById('dlgEndu').open", timeout=5000)
            assert pg.input_value("#e_hours") == "6"
            pg.click("#enduClose")
            # the saved plan for Spa (from the endpoint test, or saved here) loads over the prefill
            call("api/endurance", {"action": "save", "track": "Spa", "title": "Spa 6h", "inputs": SIX_H})
            pg.click("#pvEndu")
            pg.wait_for_function("() => document.querySelectorAll('#enduDrivers tr').length === 3", timeout=5000)
            assert pg.input_value("#e_hours") == "6" and pg.input_value("#e_title") == "Spa 6h"
            pg.fill("#e_start", "2026-11-14T13:00")
            # Kenta's clock is 16 hours ahead
            pg.locator("#enduDrivers tr").nth(2).locator(".e_dtz").fill("16")
            pg.click("#enduGo")
            pg.wait_for_function("() => !document.getElementById('enduRes').hidden", timeout=10000)
            res = pg.evaluate("() => ({k: [...document.querySelectorAll('#enduKpis .kpi')].map(e => e.textContent),"
                              " rows: [...document.querySelectorAll('#enduStints tr')].map(r => [...r.children].map(c => c.textContent)),"
                              " share: [...document.querySelectorAll('#enduShare tr')].length,"
                              " opts: [...document.querySelectorAll('#enduOpts li')].map(e => e.textContent)})")
            assert res["k"][0] == "Laps212" and res["k"][1] == "Stops6", res["k"]
            assert res["rows"][0][:3] == ["1", "Swazi", "1–32 (32)"] and "1:00" in res["rows"][0][3], res["rows"][0]
            assert "swap to Josh" in res["rows"][0][4] and res["rows"][-1][4] == "Flag"
            assert "(their " in res["rows"][2][3], res["rows"][2]          # Kenta's own clock
            assert res["share"] == 3 and any(o.startswith("Save ") for o in res["opts"])
            # a bad driving window is caught in the window, with the driver's name
            pg.locator("#enduDrivers tr").nth(1).locator(".e_davail").fill("evening")
            pg.click("#enduGo")
            pg.wait_for_function("() => document.getElementById('enduMsg').classList.contains('bad')", timeout=5000)
            assert pg.text_content("#enduMsg").startswith("Josh: type driving hours like")
            pg.locator("#enduDrivers tr").nth(1).locator(".e_davail").fill("")
            # adding drivers stops at 8
            for _ in range(6):
                pg.click("#enduAdd")
            assert pg.locator("#enduDrivers tr").count() == 8
            assert "at most 8" in pg.text_content("#enduMsg")
            pg.click("#enduClose")
            pg.wait_for_function("() => !document.getElementById('dlgEndu').open")
            b.close()
            assert not errs, errs
    except Exception as e:  # noqa: BLE001
        if "Executable doesn't exist" in str(e):
            print("  (skipped: no Chromium)")
            return
        raise


if __name__ == "__main__":
    failed = 0
    tests = [(k, v) for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for name, fn in tests:
        try:
            fn()
            print("ok  ", name)
        except Exception:  # noqa: BLE001
            failed += 1
            print("FAIL", name)
            traceback.print_exc()
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
