"""Shared builders for tests/test_24_a.py and make_samples.py (offline, deterministic).

engine_review()  runs engine.analyze on the synthetic telemetry of tests/fixtures_a/synth.py, with
                 Garage 61-style fuelUsed / fuelLevel fields and an in/out pit pair added to the raw laps.
prefill_world()  a library folder with two race reviews, strategy.json and a fake sync store.
"""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent

TRACK = "Charlotte Motor Speedway (Roval 2019)"
CAR = "Ford Mustang GT4"
TRACK_ID, CAR_ID = 468, 190


def _synth():
    import sys
    p = str(ROOT / "tests" / "fixtures_a")
    if p not in sys.path:
        sys.path.insert(0, p)
    import synth
    return synth


def engine_review(n=16, fuel="used", pits=True, multiclass=True, lap1_out=False, lap1_slow=True):
    """A review from engine.analyze. fuel: "used" (fuelUsed + fuelLevel), "level" (fuelLevel only) or None.
    pits: laps 10 (in) and 11 (out) get pit flags and 25 s / 20 s extra. lap1_out: lap 1 is 60% slower
    (left out of the analysis as a tow lap). multiclass: lap 7 loses time around 1100 m (traffic).
    lap1_slow: lap 1 (flagged unclean) loses time around 400 m and 1700 m (the C1 and C3 segments)."""
    from debrief import engine
    synth = _synth()
    slow = {7: (1100, 0.5)} if multiclass else {}
    unclean = (5,)
    if lap1_slow and not lap1_out:
        slow[1] = (400, 0.45)
        unclean = (1, 5)
    meta, laps, tel = synth.race(n=n, seed=3, noise=0.3, slow_laps=slow or None, unclean=unclean)
    meta = dict(meta, track=TRACK, car=CAR, multiclass=multiclass, session="Race")
    level = 46.0
    for l in laps:
        i = l["lapNumber"]
        burn = 2.40 + 0.01 * (i % 4)
        if pits and i == 10:
            l["pitIn"] = True
            l["lapTime"] = round(l["lapTime"] + 25.0, 3)
        if pits and i == 11:
            l["pitOut"] = True
            l["lapTime"] = round(l["lapTime"] + 20.0, 3)
        if lap1_out and i == 1:
            l["lapTime"] = round(l["lapTime"] * 1.6, 3)
        level -= burn
        if pits and i == 10:
            level = 44.0           # refuelled
        if fuel == "used":
            l["fuelUsed"] = round(burn if not (pits and i == 10) else -30.0, 3)   # a refuel spike
            l["fuelLevel"] = round(level, 2)
        elif fuel == "level":
            l["fuelLevel"] = round(level, 2)
        l["track"] = {"id": TRACK_ID}
        l["car"] = {"id": CAR_ID}
    return engine.analyze(meta, laps, tel, [], {}, track_length=None)


def trim_review(d):
    """Smaller sample_review.json: traces cut to two laps (still a valid review dict)."""
    d = json.loads(json.dumps(d, default=float))
    keep = {f"me{d['best']['n']}", "me1"}
    d["traces"] = {k: v for k, v in (d.get("traces") or {}).items() if k in keep}
    d["meta"]["test_data"] = True
    return d


def review_json(day, median, burn=None, pit_losses=None, session="Race", n=14, levels=None):
    laps = [{"n": i, "t": round(median + 0.05 * (i % 3), 3), "clean": True, "sectors": None} for i in range(1, n + 1)]
    d = {"meta": {"track": TRACK, "car": CAR, "started_at": f"{day}T19:00:00+00:00", "session": session,
                  "test_data": True},
         "L": 3600.0, "laps": laps, "best": {"n": 1, "t": laps[0]["t"], "clean": True}, "median": median,
         "combo_ids": {"track_id": TRACK_ID, "car_id": CAR_ID}, "corners": [], "counts": {"analysed": n}}
    if burn is not None:
        d["strategy"] = {"version": 1, "burn": {"median_l": burn, "p90_l": round(burn + 0.1, 3), "n": 12,
                                                "source": "fuel_used", "laps": {}},
                         "level": None, "pit": ({"laps": [], "loss_s": pit_losses[0], "n": len(pit_losses),
                                                 "losses": pit_losses} if pit_losses else None), "note": None}
    if levels is not None:
        d["stint"] = {"laps": [{"n": i + 1, "t": laps[i]["t"], "fuel_used": None, "fuel_level": v}
                               for i, v in enumerate(levels)]}
    return d


def write_review(lib: Path, name, d):
    p = lib / "reviews" / name / "review.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d), encoding="utf-8")


class FakeStore:
    """Looks like sync.Store for prefill: sessions() and laps(track, car). Records calls."""

    def __init__(self, sessions=None, laps=None):
        self._sessions = sessions or []
        self._laps = laps or {}
        self.calls = []

    def sessions(self):
        self.calls.append("sessions")
        return list(self._sessions)

    def laps(self, track, car):
        self.calls.append(("laps", track, car))
        return dict(self._laps.get((track, car), {}))


def synced_laps(n=20, fuel=True, pit=True, start_level=44.0):
    out, level = {}, start_level
    for i in range(1, n + 1):
        lt = 86.0 + 0.1 * (i % 3)
        l = {"id": f"s{i}", "event": "EV1", "sessionType": 3, "track": {"id": TRACK_ID}, "car": {"id": CAR_ID},
             "startTime": f"2026-10-15T19:{i:02d}:00Z", "lapTime": lt, "clean": True, "lapNumber": i}
        if fuel:
            level -= 2.30                       # fuelLevel: what's left at the end of the lap
            l["fuelUsed"] = 2.30
            l["fuelLevel"] = round(level, 2)
        if pit and i == 8:
            l["pitIn"], l["lapTime"] = True, lt + 24.0
        if pit and i == 9:
            l["pitOut"], l["lapTime"] = True, lt + 22.0
        out[l["id"]] = l
    return out


def fake_store(**kw):
    sess = [{"key": "EV1|3", "event": "EV1", "track_id": TRACK_ID, "car_id": CAR_ID, "type": 3, "session": "Race",
             "best_clean": 85.8, "track": TRACK, "car": CAR, "start": "2026-10-15T19:01:00Z"}]
    return FakeStore(sess, {(TRACK_ID, CAR_ID): synced_laps(**kw)})


def prefill_world(tmp: Path):
    lib = tmp / "lib"
    write_review(lib, "2026-09-10_roval", review_json("2026-09-10", 86.2, burn=2.35))
    write_review(lib, "2026-10-01_roval", review_json("2026-10-01", 85.9, burn=2.41, pit_losses=[50.1]))
    write_review(lib, "2026-10-05_roval_practice", review_json("2026-10-05", 85.0, burn=1.9, session="Practice"))
    from debrief import strategy
    strategy.save_car(lib, CAR, tank_l=47.0)
    week = {"week": 5, "start": "2026-10-20", "track": TRACK, "race_time": None, "length": "15 laps",
            "conditions": None}
    return {"library_folder": str(lib)}, fake_store(), week
