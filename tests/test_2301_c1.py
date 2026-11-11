"""2.30.1: the Siyanqaba link. C1 (debrief/c1.py) and C2 (debrief/factsheet.py) keep CONTRACT.md.
Run: python tests/test_2301_c1.py

Fake .ibt laps from tests/corner_laps.py (a McLaren 570S GT4 at Zolder, seven corners, falling fuel)."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
TMP = Path(tempfile.mkdtemp(prefix="debrief-2301-c1-"))
os.environ["DEBRIEF_SETTINGS"] = str(TMP / "settings.toml")
os.environ["DEBRIEF_CACHE_DIR"] = str(TMP / "cache")
os.environ["DEBRIEF_UPDATE_STATE"] = str(TMP / "update.json")

import corner_laps as cl  # noqa: E402
import debrief  # noqa: E402
from debrief import c1, factsheet  # noqa: E402

fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


print("== C1: the door imports nothing heavy")
code = ("import sys; import debrief.c1, debrief.factsheet; "
        "bad = sorted(m for m in sys.modules if m.split('.')[0] in "
        "('requests', 'yaml', 'irsdk', 'pywintypes', 'win32com', 'pypdf', 'tkinter', 'webview', 'sounddevice')"
        " or m.startswith('debrief.desktop')); print(','.join(bad))")
res = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
                     timeout=120)
check(res.returncode == 0 and res.stdout.strip() == "", f"c1 and factsheet load with numpy only ({res.stdout.strip() or 'none'})")
for name in ("review_session", "read_laps", "corner_cards", "habit_p", "detect_corners", "build_segments",
             "session_id", "same_corner", "corner_key", "corner_tolerance_m", "THRESHOLDS", "C1_VERSION"):
    check(hasattr(c1, name), f"c1.{name} exists")
check(c1.C1_VERSION == "1" and factsheet.FACTSHEET_VERSION == "1", "C1 and C2 are version 1")
check(all(isinstance(v, (int, float)) for v in c1.THRESHOLDS.values()), "every threshold is a number")

print("== C1: a session through the door")
p = TMP / "zolder.ibt"
cl.make(p, 15)
laps = c1.read_laps(p)
check(len(laps) >= 15 and {"lap", "time", "timed_by", "clean", "pit", "off", "incidents", "start_lap"} <= set(laps[1]),
      f"read_laps gives the laps with their flags ({len(laps)})")
r = c1.review_session([p])
for k in ("summary", "laps", "consistency", "theoretical", "segment_spread", "fuel", "stints", "notes", "thresholds"):
    check(k in r, f"review_session has {k}")
check(r["summary"]["best"] and r["summary"]["clean_laps"] >= 5, "best lap and clean laps")
check(all("valid" in x and "clean" in x and "reasons" in x for x in r["laps"]), "every lap says valid / clean and why")
th = r["theoretical"]
check(th["enough"] and th["possible"] <= th["best"], "best possible lap is at most the best lap")
cc = c1.corner_cards([p])
check(cc.get("ok") and cc["corners"] and all("apex_m" in c and "id" in c for c in cc["corners"]), "corner cards with corners")
check(all(c.get("p_value") is not None and c["p_value"] < 0.05 for c in cc["cards"]), "every card passed the shuffle test")
check(c1.habit_p([90 + i * 0.1 for i in range(5)], [1.0] * 5) is None, "habit_p needs enough laps")

print("== session id and corner matching")
sid = c1.session_id([p])
q = TMP / "copy"
q.mkdir()
shutil.copy2(p, q / p.name)
check(re.fullmatch(r"[0-9a-f]{16}", sid) is not None, "session id is 16 hex characters")
check(c1.session_id([q / p.name]) == sid, "a copied file keeps the session id (the Zerver gets the same id)")
check(c1.session_id([p, q / p.name]) == c1.session_id([q / p.name, p]), "order of files doesn't matter")
L = 4000.0
check(c1.same_corner(0.30, 0.31, L) and not c1.same_corner(0.30, 0.33, L), "60 m / 2% tolerance (80 m on 4 km)")
check(c1.same_corner(0.995, 0.005, L), "a corner across the line still matches")
cs = [{"id": "C1", "apex_pct": 0.1}, {"id": "C2", "apex_pct": 0.3}]
check(c1.corner_key(0.305, cs, L) == "C2" and c1.corner_key(0.6, cs, L) is None, "corner_key by position")

print("== C2: the fact sheet")
corners = factsheet._corners(cc)
feel = {"taps": [{"corner": "C3", "apex_pct": corners[2]["apex_pct"] + 0.002, "tag": "loose", "phase": "exit",
                  "at": "2026-11-10T21:39:52-07:00"}]}
fs = factsheet.build(r, cc, session_id=sid, feel=feel)
check(factsheet.check(fs) == [], f"keeps the contract ({factsheet.check(fs)})")
check(fs["producer"] == {"app": "debrief", "version": debrief.__version__, "c1": "1"}, "producer names this release")
check(fs["session"]["id"] == sid and fs["session"]["kind"] == "practice" and fs["session"]["units"]["speed"] == "mph",
      "session block, mph by default")
check(set(fs["clauses"]) <= set(factsheet.CLAUSE_KEYS), "only known clause keys")
check(all(re.search(r"\d", v) for k, v in fs["clauses"].items() if k in ("best", "consistency", "fuel")),
      "numbers sit inside their clauses")
check("km/h" not in json.dumps(fs["clauses"]), "no km/h in an mph sheet")
check(not re.search(r"\bT\d", fs["clauses"].get("focus", "")), "corners read 'Turn 6', not 'T6'")
check(fs["clauses"]["feel"] == "you noted Turn 3 felt loose on exit", "feel tap matched to its corner by position")
if cc["cards"]:
    check(fs["required"] == ["focus"] and fs["intent"] == "focus" and "focus" in fs["clauses"],
          "a proven corner card is the focus")
check(all(c["apex_pct"] is not None and 0 <= c["apex_pct"] < 1 for c in fs["corners"]), "corners carry apex_pct")
json.dumps(fs)  # JSON-ready

fk = factsheet.build(r, cc, session_id=sid, speed_unit="kmh", fuel_unit="gal")
check(fk["session"]["units"] == {"speed": "km/h", "fuel": "gal"} and "gallons" in fk["clauses"]["fuel"],
      "km/h and gallons follow the setting")

print("== C2: thin sessions say what is missing")
p3 = TMP / "short.ibt"
cl.make(p3, 3, seed=4)
f3 = factsheet.build(c1.review_session([p3]), c1.corner_cards([p3]))
check(factsheet.check(f3) == [] and "focus" not in f3["clauses"] and f3["intent"] is None, "no focus without proof")
check(any("best possible lap" in x and "more lap" in x for x in f3["not_proven_yet"]), "best possible lap: laps needed")
check(any(x.startswith("consistency (needs") for x in f3["not_proven_yet"]), "consistency: laps needed")
empty = factsheet.build({"summary": {}}, None)
check(factsheet.check(empty) == [] and list(empty["clauses"]) == ["summary"], "no valid laps: one plain clause")

print("== C2: check() catches broken sheets")
bad = json.loads(json.dumps(fs))
bad["intent"] = "pit"
check(any("intent" in x for x in factsheet.check(bad)), "a live intent is refused after a session")
bad = json.loads(json.dumps(fs))
bad["clauses"]["weather"] = "sunny"
check(any("unknown clause" in x for x in factsheet.check(bad)), "an unknown clause is refused")
bad = json.loads(json.dumps(fs))
bad["required"] = ["trend_x"]
check(any("required" in x for x in factsheet.check(bad)), "a required clause must exist")
bad = json.loads(json.dumps(fs))
bad["factsheet_version"] = "0"
check(any("version" in x for x in factsheet.check(bad)), "an old version is refused")

print("== CONTRACT.md and the install file agree with the code")
ct = (ROOT / "CONTRACT.md").read_text(encoding="utf-8")
check(f"| 1 (Debrief {debrief.__version__})" in ct or "C1 v1" in ct, "CONTRACT.md lists C1 v1")
check(all(k in ct for k in factsheet.CLAUSE_KEYS), "CONTRACT.md names every clause key")
pp = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
check('dependencies = ["numpy>=1.24"]' in pp and 'attr = "debrief.__version__"' in pp,
      "pyproject: numpy only, version from debrief/__init__.py")

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'all passed' if not fails else str(fails) + ' FAILED'}")
sys.exit(1 if fails else 0)
