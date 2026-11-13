"""Debrief 2.35 step 3: the Setup section's logic for every race review (debrief/racesetup.py).

Stand-in .ibt files (tests/fake_ibt.py, and the 2.15 balance test's car for the handling read) cover: a race with an
earlier session at the same car + track and a setup change; faster with Setup Lab's check passing vs not enough laps;
a fixed-setup race; a Garage 61 review matched by time, track and car; no .ibt found (None); a broken file (no
exception); Setup Lab's index reused (no file read again).

    python3 tests/ci_run.py tests/test_235_setup.py

No network; temp folders from tempfile.
"""
from __future__ import annotations

import copy
import json
import random
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
TMP = Path(tempfile.mkdtemp(prefix="debrief-235-setup-"))

import fake_ibt as fi  # noqa: E402
from debrief import ibt, racesetup as rs, setuplab as sl  # noqa: E402

CAR, TRACK = "mclaren570sgt4", "zolder gp"


def _dir(name):
    d = TMP / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _name(stamp):
    return f"{CAR}_{TRACK} {stamp}.ibt"


def _mk(d, stamp, laps, **kw):
    return fi.make(d / _name(stamp), laps, car_path=CAR, track_code=TRACK, **kw)


def _race(d, stamp, laps, **kw):
    kw.setdefault("official", 1)
    kw.setdefault("fixed_setup", 0)
    kw.setdefault("subsession", 777001)
    return _mk(d, stamp, laps, session="Race", **kw)


def _setup_b():
    s = copy.deepcopy(fi.SETUP_GT4)
    s["Chassis"]["LeftFront"]["Camber"] = "-3.6 deg"
    s["Chassis"]["RightFront"]["Camber"] = "-3.6 deg"
    s["Chassis"]["Rear"]["WingAngle"] = "3.5 deg"
    return s


def _laps(base, n, seed):
    rng = random.Random(seed)
    return [round(base + rng.uniform(-0.05, 0.05), 3) for _ in range(n)]


def _pc(names):
    return {"meta": {"source": "pc", "session": "Race", "files": list(names), "car": "McLaren 570S GT4",
                     "track": "Zolder Circuit (Grand Prix)"}}


def _iso(stamp, plus=0.0):
    y, mo, dd, h, mi, s = (int(x) for x in re.findall(r"\d+", stamp))
    ts = time.mktime((y, mo, dd, h, mi, s, 0, 0, -1)) + plus
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


def _plain(out):
    """JSON-safe, and no sentence prints None / nan / a long dash."""
    json.dumps(out, allow_nan=False)
    for s in _strings(out):
        assert not re.search(r"\bNone\b|\bnan\b|\bNaN\b|\binf\b", s), f"printed a missing number: {s!r}"
        assert "—" not in s, f"long dash: {s!r}"


# ---------------------------------------------------------------- 1. a change since the last session, not enough laps
D1 = _dir("change")
_mk(D1, "2026-11-01 18-00-00", _laps(99.5, 7, 1))
RACE1 = _race(D1, "2026-11-02 19-00-00", _laps(99.0, 10, 2), setup=_setup_b(), fuel=(40.0, 2.6))


def test_change_since_last_session():
    out = rs.for_review(_pc([RACE1.name]), D1)
    _plain(out)
    assert out["ok"] and out["source"] == "pc" and out["files"] == [RACE1.name]
    assert out["car_path"] == CAR and out["track_code"] == TRACK and not out["fixed"]
    assert out["series_setup"] == "open" and "this race's telemetry" in out["series_setup_source"]
    assert out["setup"]["id"] == "B" and out["setup"]["label"].startswith("Setup B") and "You raced Setup B" in out["setup"]["text"]
    ch = out["changes"]
    assert not ch["same"] and ch["from"].startswith("Setup A") and "Nov 1" in ch["text"], ch
    assert any("Front camber -4.0 → -3.6 deg (both sides)" == x for x in ch["lines"]), ch["lines"]
    assert any(x.startswith("Rear wing") or "wing" in x.lower() for x in ch["lines"]), ch["lines"]
    assert not any("fuel" in x.lower() or "pressure" in x.lower() for x in ch["lines"]), "readings and fuel aren't setup changes"
    assert all(x in out["lines"] for x in ch["lines"]), "every changed setting is a line of the section"


def test_not_enough_laps_is_not_a_verdict():
    f = rs.for_review(_pc([RACE1.name]), D1)["faster"]
    assert f["verdict"] == "not enough to tell" and "faster" not in f["verdict"], f
    assert "enough to tell" in f["text"] and "was faster than" not in f["text"], f["text"]
    assert "stint" in f["text"], "Setup Lab's own reason is given (stints needed)"


def test_tyres_fuel_handling_and_try():
    out = rs.for_review(_pc([RACE1.name]), D1)
    ty = out["tyres"]
    assert ty["live_pressures"] and set(ty["live_pressures"]["corners"]) == {"LF", "RF", "LR", "RR"}, ty
    assert any("Hot pressures on track" in x and "kPa" in x for x in ty["lines"])
    assert ty["temps"] is None and rs.NO_PIT in ty["lines"], "no pit stop in the race: said, not invented"
    assert out["fuel"] and out["fuel"]["per_lap_l"] and abs(out["fuel"]["per_lap_l"] - 2.6) < 0.05, out["fuel"]
    h = out["handling"]
    assert not h["ok"] and h["text"].startswith("How the car handled:"), h
    t = out["try"]
    assert t["suggestion"] is True and t["text"].startswith("No setup change to suggest"), t
    assert t["open_setup_lab"] == {"car_path": CAR, "track_code": TRACK, "route": "api/setuplab/report",
                                   "query": {"car": CAR, "track": TRACK}}


def test_pit_stop_tyre_readings():
    d = _dir("pit")
    _mk(d, "2026-11-01 18-00-00", _laps(99.5, 7, 1))
    temps = {"LF": (80.0, 85.0, 91.0), "RF": (79.0, 84.0, 90.0), "LR": (78.0, 81.0, 84.0), "RR": (77.0, 80.0, 83.0)}
    r = _race(d, "2026-11-02 19-00-00", _laps(99.0, 12, 3), pits=(6,), pit_temps=[(6, temps)])
    out = rs.for_review(_pc([r.name]), d)
    _plain(out)
    ty = out["tyres"]
    assert ty["temps"] and ty["temps"]["corners"]["LF"] == [80.0, 85.0, 91.0], ty["temps"]
    assert any("left front 80 / 85 / 91 °C" in x for x in ty["lines"]), ty["lines"]
    assert ty["pit_pressures"] and ty["pit_pressures"]["LF"] == 190.0, "iRacing's pit-stall hot pressures from the setup text"
    assert out["changes"]["same"] and out["faster"]["verdict"] == "same setup", "same setup as last time: nothing compared"


# ---------------------------------------------------------------- 2. faster, with Setup Lab's check passing
D2 = _dir("faster")
for k, (day, fast) in enumerate([(1, True), (2, False), (3, True), (4, False), (5, True), (6, False)]):
    _mk(D2, f"2026-11-0{day} 18-00-00", _laps(99.0 if fast else 99.5, 8, 10 + k), setup=_setup_b() if fast else None)
RACE2 = _race(D2, "2026-11-07 19-00-00", _laps(99.0, 10, 30), setup=_setup_b())


def test_faster_only_when_the_check_passes():
    out = rs.for_review(_pc([RACE2.name]), D2)
    _plain(out)
    f = out["faster"]
    assert f["check"] == "faster" and f["verdict"] == "faster", f
    assert "was faster than" in f["text"] and out["setup"]["label"].split(" (")[0] in f["text"], f["text"]
    assert out["changes"]["from"].startswith("Setup B") and out["setup"]["id"] == "A", (out["changes"], out["setup"])
    # the same race against too few earlier laps: never a verdict on noise
    d = _dir("faster-few")
    for src in sorted(D2.iterdir())[-2:]:
        (d / src.name).write_bytes(src.read_bytes())
    few = rs.for_review(_pc([RACE2.name]), d)["faster"]
    assert few["verdict"] == "not enough to tell" and few["check"] != "faster", few


# ---------------------------------------------------------------- 3. fixed setup
def test_fixed_setup_shows_only_dials_and_fuel():
    d = _dir("fixed")
    s = copy.deepcopy(fi.SETUP_GT4)
    s["Chassis"]["InCarDials"]["BrakePressureBias"] = "52.0%"
    _mk(d, "2026-11-01 18-00-00", _laps(99.5, 7, 1))
    r = _race(d, "2026-11-02 19-00-00", _laps(99.0, 10, 2), setup=s, fixed_setup=1, fuel=(30.0, 2.4))
    out = rs.for_review(_pc([r.name]), d)
    _plain(out)
    assert out["fixed"] and out["series_setup"] == "fixed", out["series_setup"]
    assert out["changes"] is None and out["faster"] is None and out["tyres"] is None
    labels = [x["label"] for x in out["dials"]]
    assert "Brake bias" in labels and any("ABS" in x for x in labels), labels
    assert any(x == "Brake bias: 52.0%." for x in out["lines"]), out["lines"]
    assert any(x.startswith("Fuel:") and "L a lap" in x for x in out["lines"]), out["lines"]
    text = " ".join(out["lines"]).lower()
    assert "camber" not in text and "kpa" not in text and "°c" not in text, "nothing but dials and fuel from the setup"


# ---------------------------------------------------------------- 4. Garage 61 review matched by time, track and car
def test_garage61_review_matched():
    meta = {"meta": {"session": "Race", "car": "McLaren 570S GT4", "track": "Zolder Circuit - Grand Prix",
                     "started_at": _iso("2026-11-02 19-00-00", 30)}}
    out = rs.for_review(meta, D1)
    assert out and out["ok"] and out["source"] == "garage61" and out["files"] == [RACE1.name], out
    other = copy.deepcopy(meta)
    other["meta"]["car"] = "Porsche 911 GT3 R"
    assert rs.for_review(other, D1) is None, "another car: no file"
    late = copy.deepcopy(meta)
    late["meta"]["started_at"] = _iso("2026-11-03 19-00-00")
    assert rs.for_review(late, D1) is None, "another day: no file"


# ---------------------------------------------------------------- 5. nothing found, broken files
def test_no_ibt_is_none():
    empty = _dir("empty")
    assert rs.for_review(_pc(["nope.ibt"]), empty) is None
    assert rs.for_review(_pc([RACE1.name]), TMP / "no-such-folder") is None
    assert rs.for_review({"meta": {"car": "x", "track": "y", "started_at": _iso("2026-11-02 19-00-00")}}, empty) is None
    assert rs.for_review({}, D1) is None and rs.for_review(None, D1) is None


def test_broken_file_never_raises():
    d = _dir("broken")
    (d / "broken.ibt").write_bytes(b"MZ" + b"\0" * 400)
    out = rs.for_review(_pc(["broken.ibt"]), d)
    assert out is not None and out["ok"] is False and "couldn't be read" in out["reason"], out
    _plain(out)
    # a broken file next to a good race is left out, said once
    (D1.parent / "broken-too").mkdir(exist_ok=True)
    d2 = _dir("broken-too")
    for src in D1.iterdir():
        (d2 / src.name).write_bytes(src.read_bytes())
    (d2 / f"{CAR}_{TRACK} 2026-11-02 08-00-00.ibt").write_bytes(b"\0" * 500)
    out2 = rs.for_review(_pc([RACE1.name]), d2)
    assert out2["ok"] and any("couldn't be read" in n for n in out2["notes"]), out2["notes"]
    # anything unexpected inside: a dict with a reason, never an exception
    real = rs._for_review
    rs._for_review = lambda *a: 1 / 0
    try:
        bad = rs.for_review(_pc([RACE1.name]), D1)
    finally:
        rs._for_review = real
    assert bad["ok"] is False and bad["reason"], bad


# ---------------------------------------------------------------- 6. Setup Lab's index reused
def test_setuplab_index_reused():
    cache = TMP / "index.json"
    idx = sl.index(D1, cache_path=cache)
    calls = {"n": 0}
    real = sl._read_file

    def counting(p):
        calls["n"] += 1
        return real(p)
    sl._read_file = counting
    try:
        a = rs.for_review(_pc([RACE1.name]), D1, setuplab_index=idx)
        b = rs.for_review(_pc([RACE1.name]), D1, setuplab_index=cache)
        before = cache.read_bytes()
        c = rs.for_review(_pc([RACE1.name]), D1)
    finally:
        sl._read_file = real
    assert calls["n"] == 2, f"index dict and cache file: no file read again; without them, the two files ({calls['n']})"
    assert cache.read_bytes() == before, "the cache file is only read"
    assert a["setup"] == b["setup"] == c["setup"] and a["changes"]["lines"] == c["changes"]["lines"]


# ---------------------------------------------------------------- 7. the handling read and the one change
_src = (ROOT / "tests" / "test_215_balance.py").read_text(encoding="utf-8")
NS = {"__name__": "balance_builder_235", "__file__": str(ROOT / "tests" / "test_215_balance.py")}
exec(compile(_src[:_src.index("# ---------------------------------------------------------------- 0. the filter")],
             "test_215_balance.py", "exec"), NS)


def _mids(amount):
    def f(d, kap, ay):
        out = np.zeros_like(d)
        for A, _v, _s in NS["CORNERS"]:
            m = np.abs(d - A) < 18
            out[m] += np.sign(kap[m]) * amount
        return out
    return f


def _write_race(path, laps, **kw):
    """A race file with yaw rate, steering and sideways g (the 2.15 balance test's car), so the handling read runs."""
    RATE, L, VMAX = NS["RATE"], NS["L"], NS["VMAX"]
    keys = ("Lap", "LapDistPct", "LapLastLapTime", "Speed", "SteeringWheelAngle", "YawRate", "LatAccel", "Brake",
            "Throttle", "VertAccel")
    cols = {k: [] for k in keys}

    def flat(n, lap, pct, shown):
        cols["Lap"] += [lap] * n
        cols["LapDistPct"] += list(pct)
        cols["LapLastLapTime"] += [shown] * n
        cols["Speed"] += [VMAX] * n
        for k in ("SteeringWheelAngle", "YawRate", "LatAccel", "Brake"):
            cols[k] += [0.0] * n
        cols["Throttle"] += [1.0] * n
        cols["VertAccel"] += [9.81] * n
    flat(120, 0, np.linspace(0.95, 0.999, 120), -1.0)
    shown = -1.0
    for i, x in enumerate(laps):
        n = len(x["v"])
        cols["Lap"] += [i + 1] * n
        cols["LapDistPct"] += list(x["pct"])
        cols["LapLastLapTime"] += [shown] * n
        shown = x["time"]
        cols["Speed"] += list(x["v"])
        cols["SteeringWheelAngle"] += list(x["sw"] / 57.2958)
        cols["YawRate"] += list(x["r"])
        cols["LatAccel"] += list(x["ay"])
        cols["Brake"] += list(x["b"] / 100)
        cols["Throttle"] += list(x["th"] / 100)
        cols["VertAccel"] += list(x["vert"])
    flat(300, len(laps) + 1, np.linspace(0, 0.05, 300), shown)
    n = len(cols["Lap"])
    c2 = {k: ("i" if k == "Lap" else "f", np.asarray(v)) for k, v in cols.items()}
    c2["SessionNum"] = ("i", np.zeros(n, np.int32))
    text = fi.session_text(session="Race", car_path=CAR, track_code=TRACK, **kw).replace(
        "TrackLength: 3.98 km", f"TrackLength: {L / 1000:.2f} km")
    ibt.write_fake(path, text, c2, tick_rate=RATE)
    return path


def _handling_race(d, **kw):
    rng = np.random.default_rng(2)
    laps = [NS["make_lap"](rng, extra=_mids(40.0)) for _ in range(13)]
    return _write_race(d / _name("2026-11-02 19-00-00"), laps, subsession=777009, **kw)


def test_handling_read_and_one_change():
    d = _dir("handling")
    r = _handling_race(d, official=1, fixed_setup=0)
    out = rs.for_review(_pc([r.name]), d)
    _plain(out)
    h = out["handling"]
    assert h["ok"] and h["laps"] >= 6, h
    mu = [i for i in h["issues"] if i["kind"] == "understeer" and i["phase"] == "mid"]
    assert mu and mu[0]["looks_like"] == "setup" and "looks like the setup" in mu[0]["text"], h["issues"]
    assert "look" in h["text"] and "like the setup" in h["text"], h["text"]
    t = out["try"]
    assert t["suggestion"] and not t["held"] and t["text"].startswith("One change to try (a suggestion, not a sure fix"), t
    assert t["setting"] and t["sure_words"] and t["detail"], t
    assert out["changes"]["text"].startswith("This is the first session"), out["changes"]


def test_change_held_while_series_unknown():
    d = _dir("handling-unknown")
    r = _handling_race(d)                       # no Official / IsFixedSetup in the session text, a car not known as fixed
    out = rs.for_review(_pc([r.name]), d)
    assert out["series_setup"] == "unknown", out["series_setup"]
    assert out["try"]["held"] and out["try"]["text"] == rs.HELD and out["try"]["setting"] is None, out["try"]


if __name__ == "__main__":
    import shutil
    import traceback
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
