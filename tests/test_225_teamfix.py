"""Debrief 2.25 team fixes.

  - every GT3 car ranks on one board (Garage 61 car groups; fallbacks: the car list's class, then the name)
  - the names line under a board lists each person once, in order
  - synced team laps survive an app update (version bump): nothing is downloaded again
  - the automatic team sync runs at most once per 30 minutes and puts "Synced 11:42 pm · N new laps" on the page

    python3 tests/test_225_teamfix.py

No network: stand-ins only.
"""
from __future__ import annotations

import json
import sys
import tempfile
import traceback
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.modules.setdefault("irsdk", None)


def _load(name):   # reuse an earlier test's stand-ins without running its checks
    src = (ROOT / "tests" / name).read_text(encoding="utf-8").split("for n, f in list(globals().items()):")[0]
    ns = {"__name__": "standin_" + name[:-3], "__file__": str(ROOT / "tests" / name)}
    exec(compile(src, name, "exec"), ns)
    return ns


T31 = _load("test_2231_team.py")
T24 = _load("test_224_teamsync.py")
from debrief import team, teamsync  # noqa: E402

FAILS = []


def check(name, fn):
    try:
        fn()
        print("ok  ", name)
    except Exception:  # noqa: BLE001
        FAILS.append(name)
        print("FAIL", name)
        traceback.print_exc()


def _gt3_rows():
    lap, SPA, DAY = T31["lap"], T31["SPA"], T31["DAY"]
    C, B, L, M = T31["CORVETTE"], T31["BMW"], T31["LAMBO"], T31["MX5"]
    return [lap("swazi", "Swazi", "Williams", 140.5, SPA, C, "Spa", "Chevrolet Corvette Z06 GT3.R", DAY),
            lap("josh", "Josh", "Spence", 139.8, SPA, B, "Spa", "BMW M4 GT3", DAY),
            lap("kenta", "Kenta", "Young", 141.2, SPA, L, "Spa", "Lamborghini Huracan GT3 EVO", DAY),
            lap("kenta", "Kenta", "Young", 150.0, SPA, M, "Spa", "Global Mazda MX-5 Cup", DAY)]


def _board(api):
    d, _ = T31["build"](api)
    return d["combos"][0]


def test_gt3_cars_rank_together_with_car_groups():
    api = T31["Api"](rows=_gt3_rows(), roster=True)
    C, B, L, M = T31["CORVETTE"], T31["BMW"], T31["LAMBO"], T31["MX5"]
    api.cars = lambda: [{"id": i, "name": n} for i, n in ((C, "Corvette"), (B, "BMW"), (L, "Lambo"), (M, "MX5"))]  # no class field
    api.car_groups = lambda: [{"id": 1, "name": "GT3", "cars": [C, B, L]}, {"id": 2, "name": "Everything", "cars": [C, B, L, M]},
                              {"id": 3, "name": "Cup", "cars": [M]}]
    c = _board(api)
    gt3 = [r for r in c["board"] if r["class"] == "GT3"]
    assert [r["name"] for r in gt3] == ["Josh Spence", "Swazi Williams", "Kenta Young"], c["board"]
    assert [r["pos"] for r in gt3] == [1, 2, 3] and gt3[0]["gap"] == 0 and abs(gt3[2]["gap"] - 1.4) < 1e-6, gt3
    assert not any(r.get("other_class") or r.get("note") for r in gt3), gt3
    cup = [r for r in c["board"] if r["class"] == "Cup"]
    assert len(cup) == 1 and cup[0]["other_class"] and "haven't driven this class" in cup[0]["note"], cup


def test_gt3_fallback_by_name_without_groups_or_class_field():
    api = T31["Api"](rows=_gt3_rows(), roster=True)
    C, B, L, M = T31["CORVETTE"], T31["BMW"], T31["LAMBO"], T31["MX5"]
    api.cars = lambda: [{"id": i, "name": n} for i, n in ((C, "Chevrolet Corvette Z06 GT3.R"), (B, "BMW M4 GT3"),
                                                          (L, "Lamborghini Huracan GT3 EVO"), (M, "Global Mazda MX-5 Cup"))]
    api.car_groups = lambda: (_ for _ in ()).throw(RuntimeError("no groups"))
    c = _board(api)
    assert [r["pos"] for r in c["board"] if r["class"] == "GT3"] == [1, 2, 3], c["board"]
    assert team.class_from_name("Porsche 911 GT3 Cup") == "" and team.class_from_name("Mustang GT4") == "GT4"
    assert team.class_from_name("Audi R8 LMS GT3 EVO II") == "GT3" and team.class_from_name("FIA F4") == "F4"


def test_names_line_is_deduplicated_in_order():
    entry = {"loaded": True, "races": 2, "board": [{"key": k, "name": n} for k, n in (
        ("kenta", "Kenta Young"), ("josh", "Josh Spence"), ("kenta", "Kenta Young"), ("josh", "Josh Spence"),
        ("me", "Me Myself"), ("josh", "Josh Spence"), ("kenta", "Kenta Young"))]}
    assert team.who_note(entry, []) == "Kenta, Josh", team.who_note(entry, [])
    entry["board"].append({"key": "k2", "name": "Kenta Other"})
    assert team.who_note(entry, []) == "Kenta Young, Josh, Kenta Other"


def _world_synced():
    a, add = T24["world"]()
    teamsync.run(a, log=lambda s: None, now=T24["NOW"])
    return a, add


def test_version_bump_keeps_synced_laps_and_downloads_nothing():
    import debrief
    a, _ = _world_synced()
    root = teamsync.root_for(a)
    before = {p: p.read_bytes() for p in root.rglob("*.json")}
    p0, c0 = len(a.pages), len(a.csv_calls)
    old = debrief.__version__
    try:
        debrief.__version__ = "99.0.0"          # the app is updated: same account, same cache folder
        assert teamsync.root_for(a) == root
        r = teamsync.run(a, log=lambda s: None, now=T24["NOW"] + timedelta(hours=1))
    finally:
        debrief.__version__ = old
    assert len(a.pages) == p0 and len(a.csv_calls) == c0 and r["new_laps"] == 0, (len(a.pages) - p0, r)
    for p, b in before.items():
        assert p.exists() and p.name == "state.json" or p.read_bytes() == b, p       # lap files untouched
    st = json.loads((next(root.glob("*/state.json"))).read_text(encoding="utf-8"))
    assert st["schema"] == teamsync.SCHEMA and st.get("first_complete"), st
    assert team.settings_stamp({}) == team.settings_stamp({"anything": 1})          # no version or setting in the stamp


def test_auto_sync_throttle_and_line():
    a, add = T24["world"]()
    lib = Path(tempfile.mkdtemp())
    now = T24["NOW"]
    assert teamsync.auto_due(lib, now) and teamsync.auto_line(lib) is None
    r1 = teamsync.auto_run(a, lib, log=lambda s: None, now=now)
    assert r1 is not None and r1["new_laps"] > 0
    line = teamsync.auto_line(lib)
    assert line and line.startswith("Synced ") and f"· {r1['new_laps']} new laps" in line, line
    n = a.requests_made
    assert teamsync.auto_run(a, lib, log=lambda s: None, now=now + timedelta(minutes=29)) is None
    assert a.requests_made == n                                                        # throttled: no request at all
    add("alpha-x", "m2", "Noah", 1, 8, 90.5, 0)
    r2 = teamsync.auto_run(a, lib, log=lambda s: None, now=now + timedelta(minutes=31))
    assert r2 is not None and r2["new_laps"] == 1 and "1 new lap" in teamsync.auto_line(lib) and "laps" not in teamsync.auto_line(lib)
    assert not teamsync.auto_due(lib, now + timedelta(minutes=40))


def test_page_shows_the_line():
    lib = Path(tempfile.mkdtemp())
    a, _ = T24["world"]()
    teamsync.auto_run(a, lib, log=lambda s: None, now=T24["NOW"])
    team.library_combos = lambda f: []
    p = team.render_saved(lib, {})
    html = Path(p).read_text(encoding="utf-8")
    assert "Synced " in html and '"auto_sync"' in html and 'id="autoSync"' in html
    assert json.loads((lib / "team" / "team.json").read_text(encoding="utf-8"))["auto_sync"].startswith("Synced ")


for n, f in list(globals().items()):
    if n.startswith("test_") and callable(f):
        check(n, f)

if FAILS:
    print("FAILED:", ", ".join(FAILS))
    sys.exit(1)
print("all passed")
