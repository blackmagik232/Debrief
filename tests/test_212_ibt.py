"""2.12.0: reading iRacing telemetry files (.ibt). Run: python tests/test_212_ibt.py"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
TMP = Path(tempfile.mkdtemp(prefix="debrief-212-ibt-"))

import fake_ibt as fi  # noqa: E402
from debrief import ibt  # noqa: E402

fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


p = fi.make(TMP / "a.ibt", [99.2, 98.9, 99.4, 101.0, 99.0], offs=(3,), incidents=(2,), untimed=(4,))
with ibt.open_ibt(p) as ib:
    check(ib.tick_rate == 60, "tick rate read")
    check(ib.car()["path"] == "mclaren570sgt4" and ib.car()["name"] == "Mclaren 570s GT4", "player's car, not the first car listed")
    check(ib.player().get("UserName") == "Swazi Williams", "player found by DriverCarIdx")
    check(ib.session["DriverInfo"]["Drivers"][0]["UserName"] == "Other Driver: Jr", "a name with ': ' does not break the YAML")
    check(ib.track()["name"] == "Zolder Circuit" and abs(ib.track()["surface_temp"] - 22.2) < 0.01, "track and surface temp")
    s = ib.setup
    check(s["Chassis"]["LeftFront"]["Camber"] == "-4.0 deg", "setup read by indentation")
    flat = ibt.flatten(s)
    check(flat["Tires.LeftFront.StartingPressure"] == "176 kPa", "setup flattened")
    lp = [x for x in ibt.laps(ib) if x["lap"] >= 1]
    times = [round(x["time"], 2) for x in lp]
    check(times[:5] == [99.2, 98.9, 99.4, 101.0, 99.0], f"lap times {times}")
    check([x["clean"] for x in lp] == [True, True, False, False, True], "incident lap and off-track lap not clean; untimed lap timed from samples")
    check(lp[4]["timed_by"] == "samples" and lp[0]["timed_by"] == "iracing", "who timed each lap")
    sp = ib.col("Speed")
    check(len(sp) == ib.record_count and sp.max() > 50, "channel read")

check(ibt.number("176 kPa") == 176 and ibt.number("+37 clicks") == 37 and ibt.number("Track") is None, "number()")
check(ibt.numbers("78C, 82C, 88C") == [78, 82, 88], "numbers()")
check(ibt.unit("-4.0 deg") == "deg", "unit()")

r = fi.make(TMP / "r.ibt", [104.0, 99.5, 99.1], session="Race")
with ibt.open_ibt(r) as ib:
    lp = [x for x in ibt.laps(ib) if x["lap"] >= 1]
    check(lp[0]["start_lap"] and not lp[0]["clean"] and lp[1]["clean"], "race lap 1 is a start lap, not clean")

bad = TMP / "bad.ibt"
bad.write_bytes(b"MZ" + b"\0" * 400)
try:
    ibt.open_ibt(bad)
    check(False, "a non-telemetry file is refused")
except ibt.IbtError:
    check(True, "a non-telemetry file is refused")
check(ibt.summary(bad)["ok"] is False, "summary never raises")
sm = ibt.summary(p)
check(sm["ok"] and sm["clean_laps"] == 3 and abs(sm["best"] - 98.9) < 0.01, "summary")

print("FAILED" if fails else "ALL OK", fails)
sys.exit(1 if fails else 0)
