"""2.15: a teammate's .ibt as the reference lap (debrief/teamref.py). Run: python tests/test_215_teamref.py

The teammate's file is a fake McLaren GT4 at Zolder (tests/corner_laps.py) with another driver's name. Checks:
the best clean lap is picked and labelled with the name from THEIR file, it works as the live coach's Reference
and as the corner coach's reference, a different car or track is refused in plain words, programs and damaged
files are refused, nothing is copied or changed. No network, temp folders only."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
TMP = Path(tempfile.mkdtemp(prefix="debrief-215-teamref-"))

import corner_laps as cl  # noqa: E402
import fake_ibt as fi  # noqa: E402
from debrief import cornercoach as cc, livecoach as lc, teamref as tr  # noqa: E402

fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


mate_dir = TMP / "from discord"
mate_dir.mkdir()
mine_dir = TMP / "telemetry"
mine_dir.mkdir()
mate = cl.make(mate_dir / "mclaren570sgt4_zolder gp 2026-11-02 20-00-00.ibt", 6, seed=5, name="Alex O'Neil: Jr")
mine = cl.make(mine_dir / "mclaren570sgt4_zolder gp 2026-11-03 10-00-00.ibt", 12, seed=7)
before = {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in mate_dir.iterdir()}
listing = sorted(os.listdir(TMP))

r = tr.load_reference(mate, "mclaren570sgt4", "zolder gp")
check(r["ok"], f"teammate's file accepted ({r['reason']})")
check(r["driver"] == "Alex O'Neil: Jr", f"name from the player car in THEIR file ({r['driver']})")
check(r["label"].startswith("Alex O'Neil: Jr's best lap (1:"), f"label: {r['label']}")
laps_in = [x for x in __import__("debrief.ibt", fromlist=["ibt"]).laps(__import__("debrief.ibt", fromlist=["ibt"]).open_ibt(mate))
           if x["clean"] and x["time"]]
check(abs(r["lap_time"] - min(x["time"] for x in laps_in)) < 1e-3, "their fastest clean lap is the one used")
ref = r["livecoach"]
check(isinstance(ref, lc.Reference) and ref.corners and ref.source["kind"] == "teammate"
      and ref.source["driver"] == "Alex O'Neil: Jr", "a live coach Reference, marked as a teammate's")
coach = lc.Coach(ref, [], lc.Settings())
src = lc.IbtPlaybackSource(mine, speed=0)
for _ in range(3000):
    s = src.read()
    if s is None:
        break
    coach.feed(s)
check(coach.status() is not None, "the live coach runs against it")
ccl = r["cornercoach"]
check(isinstance(ccl, dict) and isinstance(ccl.get("R"), dict) and ccl["label"] == "Alex O'Neil: Jr's best lap",
      "a corner coach reference lap with a short label")
rep = cc.analyze_files([mine], reference=ccl)
check(rep["ok"] and rep["reference"] == "Alex O'Neil: Jr's best lap", f"corner coach uses it ({rep['message']})")
check(all("Alex O'Neil: Jr's best lap" in c["text"] for c in rep["reference_cards"]), "reference cards name the teammate")
json.dumps(r["summary"])
check("livecoach" not in r["summary"] and r["summary"]["corners"] == len(ref.corners), "summary is JSON-ready")

# nothing copied or changed
after = {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in mate_dir.iterdir()}
check(before == after and sorted(os.listdir(TMP)) == listing, "the teammate's file is not copied, moved or changed")

# ---------------------------------------------------------------- refusals
other_car = TMP / "fordmustanggt4_zolder gp 2026-11-02 20-00-00.ibt"
fi.make(other_car, [99.0, 98.9, 99.1], car="Ford Mustang GT4", car_path="fordmustanggt4", name="Alex")
r2 = tr.load_reference(other_car, "mclaren570sgt4", "zolder gp")
check(not r2["ok"] and "different car (Ford Mustang GT4)" in r2["reason"], r2["reason"])
other_track = TMP / "mclaren570sgt4_spa 2026-11-02 20-00-00.ibt"
fi.make(other_track, [139.0, 138.9, 139.1], track="Spa", track_code="spa up", name="Alex")
r3 = tr.load_reference(other_track, "mclaren570sgt4", "zolder gp")
check(not r3["ok"] and "different track or layout" in r3["reason"], r3["reason"])
check(tr.load_reference(other_track)["ok"], "without his car and track given, any car is fine")
exe = TMP / "fast_lap.ibt"
exe.write_bytes(b"MZ" + b"\x90" * 5000 + b"This program cannot be run in DOS mode" + b"\0" * 5000)
r4 = tr.load_reference(exe, "mclaren570sgt4", "zolder gp")
check(not r4["ok"] and "Windows program" in r4["reason"], r4["reason"])
hidden = TMP / "hidden.ibt"
hidden.write_bytes(mate.read_bytes() + b"MZ\x90\x00" + b"This program cannot be run in DOS mode" + b"\0" * 64)
r5 = tr.load_reference(hidden, "mclaren570sgt4", "zolder gp")
check(not r5["ok"] and "program hidden inside" in r5["reason"], f"a program hidden after the telemetry: {r5['reason']}")
junk = TMP / "junk.ibt"
junk.write_bytes(os.urandom(4096))
r6 = tr.load_reference(junk)
check(not r6["ok"] and "not a readable iRacing telemetry file" in r6["reason"], r6["reason"])
named = TMP / "lap.ibt.exe"
named.write_bytes(mate.read_bytes()[:4096])
check(not tr.load_reference(named)["ok"], "a .exe is refused whatever it holds")
fake_ext = TMP / "lap.txt"
fake_ext.write_bytes(b"hello" * 400)
check("not a telemetry file" in tr.load_reference(fake_ext)["reason"], "not an .ibt: refused")
check("not found" in tr.load_reference(TMP / "nope.ibt")["reason"], "missing file: plain reason")
tiny = TMP / "tiny.ibt"
tiny.write_bytes(b"\0" * 10)
check("too small" in tr.load_reference(tiny)["reason"], "tiny file: refused")
old = tr.MAX_IBT
tr.MAX_IBT = 1000
check("far bigger than any telemetry file" in tr.load_reference(mate)["reason"], "size cap")
tr.MAX_IBT = old
dirty = TMP / "mclaren570sgt4_zolder gp 2026-11-02 21-00-00.ibt"
fi.make(dirty, [99.0, 98.9, 99.1], offs=(0, 1, 2), name="Alex")
r7 = tr.load_reference(dirty, "mclaren570sgt4", "zolder gp")
check(not r7["ok"] and "no clean, timed lap" in r7["reason"], r7["reason"])
for x in (r2, r3, r4, r5, r6, r7):
    check("—" not in x["reason"] and "–" not in x["reason"], "no long dashes in reasons")
    json.dumps(x["summary"])

print(f"\n{'all passed' if not fails else str(fails) + ' FAILED'}")
sys.exit(1 if fails else 0)
