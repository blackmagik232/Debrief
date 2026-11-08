"""Resource audit (2.26): how much CPU, memory and disk Debrief's live pieces cost, so nothing slows iRacing down.

Run:  python tools/perf_audit.py [--json]

What it measures (on whatever PC runs it; the numbers on Swazi's Ryzen 5 5600X3D will be lower than a cloud box):
  coach      the live coach's work per telemetry sample (Coach.feed), played from a stand-in .ibt as fast as possible
  read       the cost of reading N iRacing variables the way pyirsdk does (struct.unpack_from on shared memory)
  yaml       parsing iRacing's session info for a 60-car field (pyirsdk re-parses it whenever iRacing changes it)
  planner    endurance.plan for the worst cases it accepts (24 h, 6 drivers)
  racelog    bytes per hour of the planned endurance race log (1 sample a second, 64 cars, gzip)
  memory     Debrief's resident memory after importing its modules (Linux/Windows)

It prints a table with the budget each piece must stay under. Nothing here talks to iRacing or the network.
"""
from __future__ import annotations

import gzip
import io
import json
import os
import random
import statistics
import struct
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

# Budgets: share of ONE CPU core (the 5600X3D has 12 threads) and memory/disk caps. iRacing itself is mostly
# single-thread bound, so Debrief's live work must stay small and off its thread.
BUDGET = {
    "coach_pct_core": 2.0,        # live coach at 20 Hz
    "read_pct_core": 1.0,         # telemetry reads for coach + crew chief
    "yaml_ms": 250.0,             # one session-info parse, on its own thread, never the reading thread
    "planner_ms": 200.0,          # one plan (on a button press)
    "racelog_mb_per_h": 15.0,     # gzipped race log
    "memory_mb": 400.0,           # whole app resident memory
}


def _timeit(fn, n):
    t0 = time.perf_counter()
    for _ in range(n):
        fn()
    return (time.perf_counter() - t0) / n


def coach_cost():
    import fake_ibt as fi
    from debrief import livecoach as lc
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "cc.ibt")
        fi.make(p, [92.0 + 0.1 * (i % 3) for i in range(8)])
        src = lc.IbtPlaybackSource(p, speed=0)
        samples = []
        while True:
            s = src.read()
            if s is None:
                break
            samples.append(s)
        ref = lc.build_reference(p)          # with a reference lap the coach does its full work (cues, zones)
    coach = lc.Coach(ref, None, lc.Settings())
    # the file is 60 Hz; the live runner reads 20 times a second, so feed every 3rd sample
    feed = samples[::3]
    t0 = time.perf_counter()
    for s in feed:
        coach.feed(s)
    per = (time.perf_counter() - t0) / max(1, len(feed))
    return {"per_sample_us": per * 1e6, "rate_hz": 20, "pct_core": per * 20 * 100}


def read_cost(n_scalars=60, n_arrays=14, rate_scalar=20, rate_array=4):
    """pyirsdk.__getitem__ is one struct.unpack_from per variable on the shared memory buffer."""
    buf = bytearray(64 * 1024)
    offs = [random.randrange(0, 60000) & ~7 for _ in range(n_scalars)]
    aoffs = [random.randrange(0, 60000 - 512) & ~7 for _ in range(n_arrays)]
    mv = memoryview(buf)

    def scal():
        for o in offs:
            struct.unpack_from("f", mv, o)[0]

    def arr():
        for o in aoffs:
            list(struct.unpack_from("f" * 64, mv, o))
    ts = _timeit(scal, 2000)
    ta = _timeit(arr, 2000)
    pct = (ts * rate_scalar + ta * rate_array) * 100
    return {"scalars": n_scalars, "arrays_64": n_arrays, "scalar_set_us": ts * 1e6, "array_set_us": ta * 1e6,
            "pct_core": pct, "note": f"{n_scalars} values at {rate_scalar} Hz + {n_arrays} car arrays at {rate_array} Hz"}


def _session_yaml(cars=60):
    lines = ["DriverInfo:", " DriverCarIdx: 7", " DriverCarFuelMaxLtr: 120.000", " DriverCarMaxFuelPct: 1.000",
             " DriverCarEstLapTime: 98.5000", " Drivers:"]
    for i in range(cars):
        lines += [f" - CarIdx: {i}", f"   UserName: Driver {i}", f"   AbbrevName: D{i}", f"   Initials: D{i}",
                  f"   UserID: {100000 + i}", f"   TeamID: {-(5000 + i // 3)}", f"   TeamName: Team {i // 3}",
                  f"   CarNumber: \"{i + 1}\"", "   CarPath: bmwm4gt3", "   CarClassID: 4029",
                  "   CarScreenName: BMW M4 GT3", "   CarClassShortName: GT3", f"   IRating: {1500 + i * 20}",
                  "   LicString: A 3.50", "   IsSpectator: 0", "   CarIsPaceCar: 0"]
    lines += ["SessionInfo:", " Sessions:"]
    for sn, name in enumerate(("PRACTICE", "QUALIFY", "RACE")):
        lines += [f" - SessionNum: {sn}", f"   SessionType: {name.title()}", f"   SessionName: {name}",
                  "   ResultsPositions:"]
        for i in range(cars):
            lines += [f"   - Position: {i + 1}", f"     ClassPosition: {i}", f"     CarIdx: {i}", f"     Lap: {300 - i}",
                      f"     Time: {i * 1.5:.4f}", f"     FastestLap: {12 + i % 9}", f"     FastestTime: {98 + i * 0.05:.4f}",
                      f"     LastTime: {99 + i * 0.05:.4f}", f"     LapsLed: {0 if i else 40}", f"     LapsComplete: {300 - i}",
                      f"     LapsDriven: {300.0 - i:.3f}", f"     Incidents: {i % 7}", "     ReasonOutId: 0",
                      "     ReasonOutStr: Running"]
    return "\n".join(lines) + "\n"


def yaml_cost():
    import yaml
    try:
        import irsdk
        loader = irsdk.CustomYamlSafeLoader
        lname = "pyirsdk loader (" + ("C libyaml" if issubclass(loader, getattr(yaml, "CSafeLoader", ())) else "pure Python") + ")"
    except Exception:  # noqa: BLE001
        loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
        lname = "PyYAML " + loader.__name__
    src = _session_yaml(60)
    t = _timeit(lambda: yaml.load(src, Loader=loader), 5)
    tp = _timeit(lambda: yaml.load(src, Loader=yaml.SafeLoader), 3)
    return {"bytes": len(src), "parse_ms": t * 1000, "loader": lname, "pure_python_ms": tp * 1000}


def planner_cost():
    try:
        from debrief import endurance
    except Exception:  # noqa: BLE001
        return {"skipped": "endurance.py not present"}
    drivers = [{"name": f"Driver {k}", "lap_t": 98 + k * 0.4, "burn_l": 3.1 + k * 0.05} for k in range(6)]
    inp = {"minutes": 1440, "tank_l": 120, "pit_lane_s": 32, "fuel_rate_lps": 2.6, "tyre_s": 22,
           "drivers": drivers, "reserve_laps": 1}
    worst = 0.0
    for _ in range(5):
        t0 = time.perf_counter()
        endurance.plan(inp)
        worst = max(worst, time.perf_counter() - t0)
    return {"worst_ms": worst * 1000, "case": "24 h, 6 drivers, all options"}


def racelog_cost(seconds=600, cars=64):
    """The planned race log: one line a second with every car's lap, % round the lap (0.1%), position, class
    position, on pit road and last lap, plus our car's fuel and flags. Measured on 10 minutes, scaled to an hour."""
    rnd = random.Random(1)
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", compresslevel=6) as gz:
        pct = [rnd.random() for _ in range(cars)]
        laps = [0] * cars
        last = [0.0] * cars
        for t in range(seconds):
            for i in range(cars):
                pct[i] += 1.0 / (95 + i * 0.3)
                if pct[i] >= 1:
                    pct[i] -= 1
                    laps[i] += 1
                    last[i] = round(95 + i * 0.3 + rnd.uniform(-0.6, 0.6), 3)
            row = {"t": t, "lap": laps, "pct": [round(p * 1000) for p in pct], "pos": list(range(1, cars + 1)),
                   "pit": [0] * cars, "last": last, "fuel": round(80 - t * 0.03, 2), "flags": 4}
            gz.write((json.dumps(row, separators=(",", ":")) + "\n").encode())
    per_h = out.tell() * 3600 / seconds / 1e6
    return {"mb_per_hour": per_h, "mb_24h": per_h * 24}


def memory_now_mb():
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class PMC(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
            c = PMC()
            c.cb = ctypes.sizeof(PMC)
            ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(c), c.cb)
            return c.WorkingSetSize / 1e6
        with open("/proc/self/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1e3
    except Exception:  # noqa: BLE001
        return None
    return None


def memory_cost():
    import importlib
    for m in ("debrief.livecoach", "debrief.strategy", "debrief.engine", "debrief.desktop.server", "debrief.setuplab",
              "debrief.team", "debrief.overlay", "debrief.coachvoice"):
        try:
            importlib.import_module(m)
        except Exception:  # noqa: BLE001
            pass
    return {"rss_mb": memory_now_mb()}


def run():
    res = {"coach": coach_cost(), "read": read_cost(), "yaml": yaml_cost(), "planner": planner_cost(),
           "racelog": racelog_cost(), "memory": memory_cost()}
    checks = [
        ("Live coach (20 Hz)", f"{res['coach']['pct_core']:.2f}% of a core", res["coach"]["pct_core"] <= BUDGET["coach_pct_core"],
         f"<= {BUDGET['coach_pct_core']}%"),
        ("Telemetry reads", f"{res['read']['pct_core']:.2f}% of a core", res["read"]["pct_core"] <= BUDGET["read_pct_core"],
         f"<= {BUDGET['read_pct_core']}%"),
        ("Session info parse, 60 cars", f"{res['yaml']['parse_ms']:.0f} ms", res["yaml"]["parse_ms"] <= BUDGET["yaml_ms"],
         f"<= {BUDGET['yaml_ms']:.0f} ms, off the reading thread"),
        ("Endurance plan, worst case", ("skipped" if "skipped" in res["planner"] else f"{res['planner']['worst_ms']:.0f} ms"),
         "skipped" in res["planner"] or res["planner"]["worst_ms"] <= BUDGET["planner_ms"], f"<= {BUDGET['planner_ms']:.0f} ms"),
        ("Race log", f"{res['racelog']['mb_per_hour']:.1f} MB/h ({res['racelog']['mb_24h']:.0f} MB for 24 h)",
         res["racelog"]["mb_per_hour"] <= BUDGET["racelog_mb_per_h"], f"<= {BUDGET['racelog_mb_per_h']:.0f} MB/h"),
        ("App memory after imports", f"{res['memory']['rss_mb']:.0f} MB" if res["memory"]["rss_mb"] else "unknown",
         (res["memory"]["rss_mb"] or 0) <= BUDGET["memory_mb"], f"<= {BUDGET['memory_mb']:.0f} MB"),
    ]
    return res, checks


if __name__ == "__main__":
    res, checks = run()
    if "--json" in sys.argv:
        print(json.dumps({"results": res, "checks": [{"what": a, "value": b, "ok": c, "budget": d} for a, b, c, d in checks]}, indent=2))
    else:
        for a, b, c, d in checks:
            print(f"{'OK  ' if c else 'OVER'} {a:<30} {b:<32} budget {d}")
        print("details:", json.dumps({k: v for k, v in res.items()}, default=str)[:1500])
