"""Live re-plan (2.30 "Endurance Race Room"): the rest of a team race, worked out again from where the race is now.

Pure arithmetic on top of endurance.simulate: no requests, no files, no clock. Race Room calls replan() when the race
drifts (a caution, a long stop, a late driver, a different burn) and shows the options side by side.

What goes in (all optional except the saved plan's inputs):
    time_left_s / crossings   how far to the flag: seconds on the clock, or the number of line crossings left (the
                              crew chief's laps-to-go, which already follows the leader). crossings wins when both are given.
    elapsed_s                 race clock since the green flag: the drivers' driving hours (minutes from the start) are
                              measured from the green, so they are shifted by this much
    driver                    who is in the car now (a name from the plan); fuel_l: litres now (or the crew's estimate)
    lap_f                     how far round the lap the car is (0..1)
    pace_ratio, burn_ratio    what the race really shows against the plan for the driver in the car (1.04 = 4% slower /
                              thirstier), applied to every driver: a track that is slower today is slower for all of them
    stints_on_tyres, row_done tyre age in stints; stints already done in a row by the current driver
    delays                    {driver name: minutes}: that driver can't get in for that long from now

How the live state is turned into the planner's start (endurance.simulate(start=...)), with f = lap_f and the current
driver's lap time p and burn b:
    t0 = -f x p                     the clock at the last line crossing, so the next crossing is (1 - f) x p from now
    fuel at the line = fuel + f x b   as if this lap had just begun
The first stint then runs floor((fuel at the line - spare x b) / b) laps (0: pit now).

Hand checks (tank 80 L, spare 1 lap, drivers A B C all 100 s a lap at 3.0 L a lap, pit lane 25 s, fuel 5 L/s; a swap keeps
the car in the stall at least 30 s, so a swap stop is 25 + 30 = 55 s):
  1. 30 crossings left, 41 L, A in the car, f = 0: A runs floor((41 - 3) / 3) = 12 laps, then B takes the car for the 18 left.
     The stop adds (18 + 1) x 3 - (41 - 36) = 52 L (10.4 s of fuel, shorter than the 30 s swap). One stop:
     finish = 30 x 100 + 55 = 3055 s.
  2. 20 crossings left, 80 L: floor(77 / 3) = 25 >= 20, so no stop at all: 2000 s.
  3. 27 crossings left, 80 L: A runs 25 laps, stops (55 s) and B drives the last 2: 2700 + 55 = 2755 s. Saving 5% (2.85 L a
     lap, laps 0.08 x 5 = 0.4 s slower) gives floor((80 - 2.85) / 2.85) = 27: no stop, 27 x 100.4 = 2710.8 s, 44.2 s
     quicker. Saving 4% (2.88 L) gives only 26 laps, so 5% is the smallest saving that drops the stop.
  4. The same 30 crossings and 41 L but half way round the lap (f = 0.5): the clock starts at -50 s and the fuel at the line is
     41 + 0.5 x 3 = 42.5 L, so A runs floor((42.5 - 3) / 3) = 13 laps and B 17: 3055 - 50 = 3005 s.
  5. 3.5 L left (30 crossings): not enough for a lap with the spare, so the first stint is 0 laps (pit now): B then fills
     (25 + 1) x 3 - 3.5 = 74.5 L, drives 25 laps and C the last 5 after a second stop: 3110 s.
The numbers above are asserted in tests/test_230_replan.py.
"""
from __future__ import annotations

import math

from . import endurance as E

NEVER = [(-1e9, -1e9 + 1.0)]          # a driver window that is never open (an empty list would mean "any time")
MAX_OPTION_SAVE = 10.0


def _num(v, default=None):
    f = E._num(v)
    return default if f is None else f


def _shift_windows(d, elapsed_s, delay_min):
    """The driving windows of driver d on the clock of 'now' (windows are seconds from the green flag)."""
    wins = list(d["avail"])
    d_s = max(0.0, float(delay_min or 0.0)) * 60.0
    if not wins and d_s <= 0:
        return []
    if not wins:
        return [(d_s, 1e9)]
    out = []
    for a, b in wins:
        a2, b2 = a - elapsed_s, b - elapsed_s
        a2 = max(a2, d_s)
        if b2 > a2:
            out.append((a2, b2))
    return out or list(NEVER)


def _driver_index(cfg, name):
    if not name:
        return None
    low = str(name).strip().lower()
    for i, d in enumerate(cfg["drivers"]):
        if d["name"].lower() == low:
            return i
    return None


def build(inputs: dict, st: dict) -> tuple:
    """(cfg, target, start, notes): the planner settings for the rest of the race. Raises ValueError (plain words)."""
    cfg = dict(E._read(inputs))
    notes: list = []
    pr = min(1.5, max(0.7, _num(st.get("pace_ratio"), 1.0)))
    br = min(2.0, max(0.5, _num(st.get("burn_ratio"), 1.0)))
    elapsed = max(0.0, _num(st.get("elapsed_s"), 0.0))
    delays = st.get("delays") if isinstance(st.get("delays"), dict) else {}
    dl = {str(k).strip().lower(): _num(v, 0.0) for k, v in delays.items()}
    ds = []
    for d in cfg["drivers"]:
        nd = dict(d, lap_t=d["lap_t"] * pr, burn=d["burn"] * br)
        nd["avail"] = _shift_windows(nd, elapsed, dl.get(d["name"].lower()))
        ds.append(nd)
    cfg["drivers"] = ds
    if abs(pr - 1.0) > 0.005:
        notes.append(f"Lap times scaled by {pr:.3f} (the race against the plan).")
    if abs(br - 1.0) > 0.005:
        notes.append(f"Fuel use scaled by {br:.3f} (the race against the plan).")
    crossings = st.get("crossings")
    crossings = int(crossings) if _num(crossings) is not None and crossings >= 1 else None
    tl = _num(st.get("time_left_s"))
    if crossings is None:
        if tl is None or tl <= 0:
            raise ValueError("Race Room doesn't know how much race is left yet.")
        cfg["laps"], cfg["minutes"] = None, tl / 60.0
    else:
        cfg["laps"], cfg["minutes"] = crossings, None
    i = _driver_index(cfg, st.get("driver"))
    fuel = _num(st.get("fuel_l"))
    start = None
    if i is not None and fuel is not None:
        d = ds[i]
        f = min(1.0, max(0.0, _num(st.get("lap_f"), 0.0)))
        start = {"driver": i, "fuel": min(cfg["tank"] + d["burn"], fuel + f * d["burn"]), "t": -f * d["lap_t"],
                 "rot": i * cfg["in_row"] + int(_num(st.get("row_done"), 0)),
                 "tyre_age": int(_num(st.get("stints_on_tyres"), 0))}
    else:
        notes.append("Who is in the car, or how much fuel there is, isn't known: planned from a fresh start of the "
                     "next stint.")
    return cfg, crossings, start, notes


def _stint_rows(sim):
    rows = []
    for s in sim["stints"]:
        r = {"n": s["n"], "driver": s["driver"], "laps": s["laps"], "from_lap": s["from_lap"], "to_lap": s["to_lap"],
             "start_in_s": max(0.0, s["start_s"]), "end_in_s": s["end_s"], "stop": s.get("stop")}
        rows.append(r)
    return rows


def _opt(key, label, v, base, detail="", extra=None):
    delta = v["finish_s"] - base["finish_s"]
    o = {"key": key, "label": label, "stops": v["stops"], "finish_s": v["finish_s"], "delta_s": round(delta, 1),
         "better": delta < -0.05, "detail": detail}
    if extra:
        o.update(extra)
    return o


def replan(inputs: dict, st: dict) -> dict:
    """The rest of the race from the live state st (see the module text). Returns
    {"ok", "laps", "finish_s", "stops", "stints": [...], "options": [...], "notes": [...], "warnings": [...]}.
    The options are over the same distance as the base plan, so their finish times compare directly: delta_s < 0 is
    quicker than the base (full tanks, the stops the plan has)."""
    cfg, target, start, notes = build(inputs, st)
    base = E.simulate(cfg, target_laps=target, start=start)
    dist = base["laps"]
    options = []
    # 1) save fuel to drop a stop: the smallest saving (0.5% steps) that takes a stop away over the same distance
    if base["stops"] > 0 and cfg["save_max"] > 0:
        found = None
        s = E.SAVE_STEP
        while s <= min(cfg["save_max"], MAX_OPTION_SAVE) + 1e-9:
            ds = [dict(d, burn=d["burn"] * (1 - s / 100.0), lap_t=d["lap_t"] + cfg["save_cost"] * s) for d in cfg["drivers"]]
            c2 = dict(cfg, drivers=ds)
            st2 = dict(start, fuel=start["fuel"]) if start else None
            v = E.simulate(c2, target_laps=dist, start=st2)
            if v["stops"] < base["stops"]:
                found = (s, v)
                break
            s += E.SAVE_STEP
        if found:
            s, v = found
            options.append(_opt("save", f"Save {s:g}% fuel: {v['stops']} stop{'s' if v['stops'] != 1 else ''} instead of "
                                f"{base['stops']}", v, base,
                                f"every driver uses {s:g}% less fuel a lap; laps assumed {cfg['save_cost'] * s:.2f} s slower",
                                {"save_pct": s}))
        else:
            options.append({"key": "save", "label": f"Saving up to {min(cfg['save_max'], MAX_OPTION_SAVE):g}% fuel doesn't "
                            "drop a stop", "stops": base["stops"], "finish_s": base["finish_s"], "delta_s": None,
                            "better": False, "detail": ""})
    # 2) tyres: double-stint (or a new set at every stop)
    if base["stops"] > 0 and cfg["tyre_s"] and cfg["tyre_stints"] > 0:
        alt = 2 if cfg["tyre_stints"] == 1 else 1
        v = E.simulate(dict(cfg, tyre_stints=alt), target_laps=dist, start=start)
        options.append(_opt("tyres", "Double-stint the tyres" if alt == 2 else "New tyres at every stop", v, base,
                            "pit time only: Debrief can't see how much the tyres fade" if abs(v["finish_s"] - base["finish_s"]) >= 0.05
                            else "no pit time saved: the tyres are changed while the car refuels",
                            {"tyre_sets_used": v["tyre_sets_used"]}))
    # 3) one fewer driver change (the current driver stays out for a second stint) when the plan swaps every stop
    warns = list(dict.fromkeys(base["warnings"]))
    if start and base["stints"] and base["stints"][0]["laps"] == 0:
        notes.append("The fuel doesn't reach the line with the spare lap: pit now.")
    return {"ok": True, "laps": dist, "finish_s": base["finish_s"], "stops": base["stops"],
            "pit_time_s": base["pit_time_s"], "stints": _stint_rows(base), "options": options, "notes": notes,
            "warnings": warns, "from_state": bool(start)}


def drift(plan_state: dict, last: dict | None, now_lap, caution: bool, stop_n: int, lap_s_ratio: float,
          burn_ratio: float) -> str | None:
    """Why a re-plan is due now (a short reason), or None. last = what the previous re-plan was made from:
    {"lap", "caution", "stops", "pace", "burn"}. Cheap: it is asked every reading, the re-plan itself once a lap or on
    an event (a caution begins or ends, a stop is done, pace or burn moved by 3% or more)."""
    if last is None:
        return "first look"
    if caution != last.get("caution"):
        return "caution " + ("began" if caution else "ended")
    if stop_n != last.get("stops"):
        return "a stop was made"
    if abs(lap_s_ratio - last.get("pace", 1.0)) >= 0.03:
        return "pace changed"
    if abs(burn_ratio - last.get("burn", 1.0)) >= 0.03:
        return "fuel use changed"
    if now_lap is not None and last.get("lap") is not None and now_lap != last["lap"]:
        return "new lap"
    return None
