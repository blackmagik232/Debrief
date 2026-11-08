"""Endurance planner (2.26 "Stint plan"): stints, drivers, fuel, tyres and pit stops for a long race. Pure arithmetic,
no requests, no files; the server and the Prepare page call plan(inputs).

How a plan is worked out (simulate):
  * The race is driven stint by stint. Each stint belongs to one driver, who has their own lap time and fuel use.
  * A stint runs on the fuel in the tank, keeping `reserve_laps` of that driver's fuel spare (the same rule as
    strategy.plan: laps a tank = floor((tank - reserve x burn) / burn)).
  * Timed race: the car's race ends at the first line crossing after the clock runs out, counting the time spent in
    the pits. (strategy.plan, for sprints, works out the laps from the lap time alone; over hours the pit time
    matters, so here the laps come out of the simulation.)
  * At each stop: fuel for the next stint (or only what is needed to finish, so the last stop is short), tyres every
    `tyre_stints` stints, and a driver change when the next stint is someone else's. iRacing's driver swap needs the
    car to sit in the stall for at least 30 s (the swap timer runs while service is done), so a swap stop is at least
    30 s of service.
  * Stop time = pit lane loss + service. Service = fuel added / fill rate, plus the tyre change (or the longer of the
    two when fuel and tyres happen together). With no lane loss or fill rate known, the measured whole-stop loss
    (pit_loss_s, from strategy.prefill) is used per stop instead.
Checks: iRacing's fair share (1/4 of an equal share of the laps, rounded up, per driver), the session's minimum and
maximum drivers, the tyre set limit, and driver availability (hours each driver can drive, in minutes from the start).
Options compared over the same distance: saving fuel to drop a stop, and double-stinting tyres.

plan(inputs) raises ValueError("plain message") for bad inputs. All times in seconds, fuel in litres.
"""
from __future__ import annotations

import math

SWAP_S = 30.0                 # iRacing driver-swap timer (support.iracing.com, Team Driving)
FAIR_SHARE = 0.25             # 1/4 of an equal share of the laps
MAX_DRIVERS = 8
MAX_STINTS = 2000
SAVE_STEP = 0.5               # % steps when looking for the smallest fuel save that drops a stop
DEFAULT_SAVE_COST = 0.08      # assumed: seconds a lap slower for each 1% of fuel saved (lift and coast)


def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(v) else None
    if isinstance(v, str) and v.strip():
        try:
            f = float(v.strip())
        except ValueError:
            return None
        return f if math.isfinite(f) else None
    return None


def _in(d, key, lo, hi, label, required=False, whole=False, default=None):
    v = d.get(key)
    if v is None or v == "":
        if required:
            raise ValueError(f"Type the {label}.")
        return default
    f = _num(v)
    if f is None:
        raise ValueError(f"The {label} must be a number.")
    if whole and abs(f - round(f)) > 1e-9:
        raise ValueError(f"The {label} must be a whole number.")
    if not lo <= f <= hi:
        raise ValueError(f"The {label} must be between {lo:g} and {hi:g}.")
    return int(round(f)) if whole else f


def _bool(d, key, default):
    v = d.get(key, default)
    if v is None:
        return default
    if not isinstance(v, bool):
        raise ValueError(f"Choose yes or no for {key.replace('_', ' ')}.")
    return v


def clock(s: float) -> str:
    """Seconds from the start as h:mm (rounded down to the minute)."""
    s = max(0, int(s))
    return f"{s // 3600}:{(s % 3600) // 60:02d}"


def _drivers(raw) -> list:
    if not isinstance(raw, list) or not raw:
        raise ValueError("Add at least one driver.")
    if len(raw) > MAX_DRIVERS:
        raise ValueError(f"A plan can have at most {MAX_DRIVERS} drivers.")
    out, seen = [], set()
    for i, d in enumerate(raw, 1):
        if not isinstance(d, dict):
            raise ValueError("Each driver needs a name, a lap time and fuel use.")
        name = d.get("name")
        name = name.strip() if isinstance(name, str) else ""
        if not name:
            name = f"Driver {i}"
        if len(name) > 40:
            raise ValueError("Keep each driver's name under 40 characters.")
        if name.lower() in seen:
            raise ValueError(f"Two drivers are called {name}: give each one their own name.")
        seen.add(name.lower())
        lap_t = _in(d, "lap_t", 10, 1200, f"lap time in seconds for {name}", required=True)
        burn = _in(d, "burn_l", 0.05, 20, f"fuel use a lap (litres) for {name}", required=True)
        avail = d.get("avail")
        wins = []
        if avail not in (None, "", []):
            if not isinstance(avail, list) or len(avail) > 12:
                raise ValueError(f"{name}'s driving hours aren't readable. Leave them empty for any time.")
            for w in avail:
                if not isinstance(w, (list, tuple)) or len(w) != 2:
                    raise ValueError(f"{name}'s driving hours aren't readable. Leave them empty for any time.")
                a, b = _num(w[0]), _num(w[1])
                if a is None or b is None or a < 0 or b > 1440 * 2 or b <= a:
                    raise ValueError(f"Each of {name}'s driving windows needs a start before its end.")
                wins.append((a * 60.0, b * 60.0))
        out.append({"name": name, "lap_t": lap_t, "burn": burn, "avail": wins})
    return out


def _read(inputs: dict) -> dict:
    if not isinstance(inputs, dict):
        raise ValueError("Fill in the race details first.")
    laps = _in(inputs, "laps", 1, 5000, "number of laps", whole=True)
    minutes = None if laps is not None else _in(inputs, "minutes", 10, 1440, "race length in minutes")
    if laps is None and minutes is None:
        raise ValueError("Type the race length in laps or minutes.")
    cfg = {
        "laps": laps, "minutes": minutes,
        "tank": _in(inputs, "tank_l", 1, 200, "tank size (litres)", required=True),
        "reserve": _in(inputs, "reserve_laps", 0, 5, "number of spare laps of fuel", default=1.0),
        "drivers": _drivers(inputs.get("drivers")),
        "in_row": _in(inputs, "stints_in_row", 1, 4, "stints each driver does in a row", whole=True, default=1),
        "lane": _in(inputs, "pit_lane_s", 0, 180, "pit lane time in seconds"),
        "rate": _in(inputs, "fuel_rate_lps", 0.2, 20, "refuelling speed (litres a second)"),
        "tyre_s": _in(inputs, "tyre_s", 0, 120, "tyre change time in seconds"),
        "stop_loss": _in(inputs, "pit_loss_s", 0, 300, "pit stop time in seconds"),
        "parallel": _bool(inputs, "parallel_service", True),
        "tyre_stints": _in(inputs, "tyre_stints", 0, 6, "stints on one set of tyres", whole=True, default=1),
        "tyre_sets": _in(inputs, "tyre_sets", 1, 99, "tyre set limit", whole=True),
        "fair_share": _bool(inputs, "fair_share", True),
        "min_drivers": _in(inputs, "min_drivers", 1, MAX_DRIVERS, "minimum drivers", whole=True),
        "max_drivers": _in(inputs, "max_drivers", 1, MAX_DRIVERS, "maximum drivers", whole=True),
        "save_max": _in(inputs, "save_max_pct", 0, 20, "most fuel saving to consider (%)", default=10.0),
        "save_cost": _in(inputs, "save_cost_s", 0, 2, "lap time lost per 1% of fuel saved", default=DEFAULT_SAVE_COST),
    }
    if cfg["min_drivers"] and cfg["max_drivers"] and cfg["min_drivers"] > cfg["max_drivers"]:
        raise ValueError("The minimum drivers can't be more than the maximum.")
    for d in cfg["drivers"]:
        if math.floor((cfg["tank"] - cfg["reserve"] * d["burn"]) / d["burn"] + 1e-9) < 1:
            raise ValueError(f"The tank is too small for one lap at {d['name']}'s fuel use.")
    return cfg


def _lpt(tank, reserve, burn) -> int:
    return int(math.floor((tank - reserve * burn) / burn + 1e-9))


def _laps_on(fuel, reserve, burn) -> int:
    return max(0, int(math.floor((fuel - reserve * burn) / burn + 1e-9)))


def _free(d, a, b) -> bool:
    """Can driver d drive from a to b (seconds from the start)? No windows: any time."""
    if not d["avail"]:
        return True
    return any(w0 <= a + 1e-6 and b <= w1 + 1e-6 for w0, w1 in d["avail"])


def _free_at(d, a) -> bool:
    return not d["avail"] or any(w0 <= a + 1e-6 < w1 for w0, w1 in d["avail"])


def _stop_time(cfg, add, tyres, swap) -> tuple:
    """(seconds, how) for one stop."""
    if cfg["lane"] is not None and cfg["rate"] is not None:
        fuel_s = add / cfg["rate"]
        tyre_s = (cfg["tyre_s"] or 0.0) if tyres else 0.0
        service = max(fuel_s, tyre_s) if cfg["parallel"] else fuel_s + tyre_s
        if swap:
            service = max(service, SWAP_S)
        return cfg["lane"] + service, "parts"
    if cfg["stop_loss"] is not None:   # the measured whole-stop loss (lane + refuelling), the same every stop
        return cfg["stop_loss"], "loss"
    return 0.0, "unknown"


def _remaining(cfg, t, lap, lap_t, target) -> int:
    """Laps still to drive at this driver's pace: to the target laps, or to the first crossing after the clock."""
    if target is not None:
        return max(0, target - lap)
    # the first crossing at or after the clock (a crossing exactly on time ends it); after a stop that ran past the
    # clock the car still has to cross the line once more
    T = cfg["minutes"] * 60.0
    return max(1, int(math.ceil((T - t) / lap_t - 1e-9)))


def _est(cfg, d, t, lap, target) -> float:
    """How long driver d's stint starting at t would be: a full tank at their fuel use, or less if the race ends first."""
    n = min(_lpt(cfg["tank"], cfg["reserve"], d["burn"]), _remaining(cfg, t, lap, d["lap_t"], target))
    return max(1, n) * d["lap_t"]


def _pick(cfg, order_i, t, est) -> tuple:
    """Which driver takes the stint starting at t: the next one in the rotation who is free for the whole stint (est(i):
    that driver's own stint length in seconds), else the first free at its start, else the planned one (with a
    warning). Returns (index, warning or None)."""
    ds = cfg["drivers"]
    n = len(ds)
    for k in range(n):
        i = (order_i + k) % n
        if _free(ds[i], t, t + est(i)):
            return i, None
    for k in range(n):
        i = (order_i + k) % n
        if _free_at(ds[i], t):
            return i, f"No driver is free for all of the stint at {clock(t)}: {ds[i]['name']} starts it."
    return order_i % n, f"No driver is free at {clock(t)}: {ds[order_i % n]['name']} is planned anyway."


def simulate(cfg: dict, target_laps: int | None = None) -> dict:
    ds = cfg["drivers"]
    target = target_laps if target_laps is not None else cfg["laps"]
    tank, res = cfg["tank"], cfg["reserve"]
    t, lap, fuel = 0.0, 0, None
    stints, warns = [], []
    rot = 0                 # rotation position (counts stints in row)
    tyre_age, sets, stops = 0, 1, 0
    cur = None
    pit_total = 0.0
    how_seen = set()
    while len(stints) < MAX_STINTS:
        # who drives this stint
        if cur is None:
            want = (rot // cfg["in_row"]) % len(ds)
            cur, w = _pick(cfg, want, t, lambda i, t0=t: _est(cfg, ds[i], t0, lap, target))
            if w:
                warns.append(w)
            fuel = tank if target is None or (target * ds[cur]["burn"] + res * ds[cur]["burn"]) > tank else \
                math.ceil((target + res) * ds[cur]["burn"] * 10) / 10.0
        d = ds[cur]
        rem = _remaining(cfg, t, lap, d["lap_t"], target)
        if rem <= 0:
            break
        can = _laps_on(fuel, res, d["burn"])
        n = min(rem, can)
        if n < 1:                     # can't happen with a valid tank; never loop forever
            raise ValueError("The tank is too small for one lap at this fuel use.")
        st = {"n": len(stints) + 1, "driver": d["name"], "from_lap": lap + 1, "to_lap": lap + n, "laps": n,
              "start_s": round(t, 1), "fuel_start_l": round(fuel, 1)}
        t += n * d["lap_t"]
        lap += n
        fuel -= n * d["burn"]
        st["end_s"] = round(t, 1)
        if d["avail"] and not _free(d, st["start_s"], t):
            warns.append(f"{d['name']}'s stint from {clock(st['start_s'])} to {clock(t)} runs outside the hours "
                         f"{d['name']} can drive.")
        stints.append(st)
        if n == rem:                  # flag
            st["stop"] = None
            break
        # stop after this stint: who's next?
        rot += 1
        want = (rot // cfg["in_row"]) % len(ds)
        nxt, w = _pick(cfg, want, t, lambda i, t0=t: _est(cfg, ds[i], t0, lap, target))
        if w:
            warns.append(w)
        nd = ds[nxt]
        swap = nxt != cur
        tyres = cfg["tyre_stints"] > 0 and tyre_age + 1 >= cfg["tyre_stints"]
        # fuel to add: the next driver's full stint, or only what gets them to the flag (two passes: the stop's own
        # length changes how many laps are left in a timed race)
        add = max(0.0, tank - fuel)
        for _ in range(3):
            stop_s, how = _stop_time(cfg, add, tyres, swap)
            left = _remaining(cfg, t + stop_s, lap, nd["lap_t"], target)
            need = (min(left, _lpt(tank, res, nd["burn"])) + res) * nd["burn"]
            add = min(max(0.0, tank - fuel), max(0.0, math.ceil((need - fuel) * 10) / 10.0))
        stop_s, how = _stop_time(cfg, add, tyres, swap)
        how_seen.add(how)
        st["stop"] = {"fuel_add_l": round(add, 1), "tyres": tyres, "swap": swap, "to": nd["name"],
                      "stop_s": round(stop_s, 1)}
        stops += 1
        pit_total += stop_s
        t += stop_s
        fuel += add
        if tyres:
            sets += 1
            tyre_age = 0
        else:
            tyre_age += 1
        cur = nxt
    else:
        raise ValueError("That race is too long to plan stint by stint.")
    return {"laps": lap, "finish_s": round(t, 1), "stops": stops, "stints": stints, "pit_time_s": round(pit_total, 1),
            "tyre_sets_used": sets if cfg["tyre_stints"] > 0 else 1, "warnings": warns, "stop_how": sorted(how_seen)}


def _driver_rows(cfg, sim) -> list:
    rows = []
    total = sim["laps"]
    n = len(cfg["drivers"])
    fair = int(math.ceil(FAIR_SHARE * total / n - 1e-9)) if total else 0
    for d in cfg["drivers"]:
        mine = [s for s in sim["stints"] if s["driver"] == d["name"]]
        laps = sum(s["laps"] for s in mine)
        secs = sum(s["end_s"] - s["start_s"] for s in mine)
        rows.append({"name": d["name"], "stints": len(mine), "laps": laps, "drive_s": round(secs, 1),
                     "share_pct": round(100.0 * laps / total, 1) if total else 0.0, "fair_share_laps": fair,
                     "short": cfg["fair_share"] and laps < fair})
    return rows


def _with(cfg, **kw) -> dict:
    c = dict(cfg)
    c.update(kw)
    return c


def _options(cfg, base) -> list:
    """Alternatives over the same distance as the base plan, so their times compare directly."""
    out = []
    same = simulate(cfg, target_laps=base["laps"])
    # 1) save fuel to drop one stop
    if same["stops"] > 0 and cfg["save_max"] > 0:
        s = SAVE_STEP
        while s <= cfg["save_max"] + 1e-9:
            ds = [dict(d, burn=d["burn"] * (1 - s / 100.0), lap_t=d["lap_t"] + cfg["save_cost"] * s) for d in cfg["drivers"]]
            v = simulate(_with(cfg, drivers=ds), target_laps=base["laps"])
            if v["stops"] < same["stops"]:
                delta = v["finish_s"] - same["finish_s"]
                out.append({"key": "save", "label": f"Save {s:g}% fuel: {v['stops']} stop{'s' if v['stops'] != 1 else ''} "
                            f"instead of {same['stops']}", "stops": v["stops"], "delta_s": round(delta, 1),
                            "detail": f"every driver uses {s:g}% less fuel a lap (about "
                                      f"{sum(d['burn'] for d in cfg['drivers']) / len(cfg['drivers']) * s / 100.0:.2f} L); "
                                      f"laps assumed {cfg['save_cost'] * s:.2f} s slower",
                            "better": delta < 0})
                break
            s += SAVE_STEP
        if not out:
            out.append({"key": "save", "label": f"Saving up to {cfg['save_max']:g}% fuel doesn't drop a stop",
                        "stops": same["stops"], "delta_s": None, "detail": "", "better": False})
    # 2) tyres every stop vs every other stint
    if same["stops"] > 0 and cfg["tyre_s"] and cfg["tyre_stints"] > 0:
        alt = 2 if cfg["tyre_stints"] == 1 else 1
        v = simulate(_with(cfg, tyre_stints=alt), target_laps=base["laps"])
        delta = v["finish_s"] - same["finish_s"]
        out.append({"key": "tyres", "label": "Double-stint the tyres" if alt == 2 else "New tyres at every stop",
                    "stops": v["stops"], "delta_s": round(delta, 1), "tyre_sets_used": v["tyre_sets_used"],
                    "detail": ("no pit time saved: the tyres are changed while the car refuels" if abs(delta) < 0.05
                               else "pit time only: Debrief can't see how much the tyres fade yet"),
                    "better": delta < -0.05})
    return out


def plan(inputs: dict) -> dict:
    cfg = _read(inputs)
    base = simulate(cfg)
    warns = list(dict.fromkeys(base["warnings"]))
    rows = _driver_rows(cfg, base)
    used = [r for r in rows if r["laps"] > 0]
    if cfg["fair_share"]:
        for r in rows:
            if r["short"]:
                warns.append(f"{r['name']} drives {r['laps']} lap{'s' if r['laps'] != 1 else ''}; the fair share "
                             f"needs at least {r['fair_share_laps']}.")
    if cfg["min_drivers"] and len(used) < cfg["min_drivers"]:
        warns.append(f"Only {len(used)} driver{'s' if len(used) != 1 else ''} drive; the session needs at least "
                     f"{cfg['min_drivers']}.")
    if cfg["max_drivers"] and len(used) > cfg["max_drivers"]:
        warns.append(f"{len(used)} drivers drive; the session allows at most {cfg['max_drivers']}.")
    if cfg["tyre_sets"] and base["tyre_sets_used"] > cfg["tyre_sets"]:
        warns.append(f"The plan uses {base['tyre_sets_used']} sets of tyres; the limit is {cfg['tyre_sets']}. "
                     "Double-stint some tyres.")
    if base["stops"] and "unknown" in base["stop_how"]:
        warns.append("Pit stop time unknown: type the pit lane time and refuelling speed (or the whole stop time) "
                     "so the plan counts the time in the pits.")
    if base["stops"] and "loss" in base["stop_how"] and any(s.get("stop") and s["stop"]["swap"] for s in base["stints"]):
        warns.append("Driver swaps keep the car in the stall for at least 30 s. Type the pit lane time and refuelling "
                     "speed so swap stops are timed exactly; for now every stop uses your measured stop time.")
    if cfg["minutes"] is not None:
        warns.append("Timed race: the laps come from your own pace. If the leader is quicker, you may run a lap less.")
    if cfg["parallel"] and cfg["tyre_s"] and cfg["tyre_stints"] > 0 and base["stops"]:
        warns.append("Fuel and tyres are counted as happening together. Untick that if this car does them one after "
                     "the other.")
    return {"laps": base["laps"], "finish_s": base["finish_s"], "finish_text": clock(base["finish_s"]),
            "stops": base["stops"], "pit_time_s": base["pit_time_s"], "tyre_sets_used": base["tyre_sets_used"],
            "stints": base["stints"], "drivers": rows, "options": _options(cfg, base),
            "swap_s": SWAP_S, "warnings": warns}


def share_text(p: dict, title: str = "") -> str:
    """The plan as plain text to paste in Discord."""
    lines = [f"Stint plan{': ' + title if title else ''} · {p['laps']} laps · {p['stops']} stops"]
    for s in p.get("stints") or []:
        stop = s.get("stop")
        tail = ""
        if stop:
            bits = [f"add {stop['fuel_add_l']:.1f} L"]
            if stop["tyres"]:
                bits.append("tyres")
            if stop["swap"]:
                bits.append(f"swap to {stop['to']}")
            tail = " → box: " + ", ".join(bits)
        lines.append(f"{s['n']}. {s['driver']} · laps {s['from_lap']}-{s['to_lap']} · from {clock(s['start_s'])}{tail}")
    lines.append("Planned in Debrief · by Black Magik Studios")
    return "\n".join(lines)


# ---------------------------------------------------------------- saved plans (<library>/endurance/<track>.json)
FOLDER = "endurance"
MAX_SAVED_BYTES = 64 * 1024


def _plan_path(library_folder, track: str):
    from pathlib import Path
    from . import library
    slug = library.slugify(track or "") or "race"
    return Path(library_folder) / FOLDER / f"{slug[:80]}.json"


def save_inputs(library_folder, track: str, inputs: dict, title: str = "") -> dict:
    """Keeps the planner's inputs for a track so the window opens with them next time. Checked with _read first, so
    only a plan that works is saved. Returns {"saved": path name}."""
    import json
    import time
    from . import fsutil
    _read(inputs)
    doc = {"schema": 1, "track": track, "title": (title or "")[:80], "inputs": inputs,
           "saved": time.strftime("%Y-%m-%d %H:%M")}
    text = json.dumps(doc, indent=1, ensure_ascii=False)
    if len(text.encode("utf-8")) > MAX_SAVED_BYTES:
        raise ValueError("That plan is too big to save.")
    p = _plan_path(library_folder, track)
    fsutil.atomic_write_text(p, text, mkdir=True)
    return {"saved": p.name, "when": doc["saved"]}


def load_inputs(library_folder, track: str) -> dict | None:
    """The saved inputs for a track, or None (missing, unreadable or no longer valid: never raises)."""
    import json
    try:
        p = _plan_path(library_folder, track)
        if not p.is_file() or p.stat().st_size > MAX_SAVED_BYTES:
            return None
        d = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(d, dict) or not isinstance(d.get("inputs"), dict):
            return None
        _read(d["inputs"])
        return {"inputs": d["inputs"], "title": d.get("title") or "", "saved": d.get("saved") or ""}
    except (OSError, ValueError, TypeError):
        return None
