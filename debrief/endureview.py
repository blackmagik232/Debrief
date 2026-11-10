"""Endurance review (2.30): reads the team-race logs (endurancelog.py) after the race and works out what happened.

    logs = [load_log(p) for p in paths]            # one file per teammate PC (or just yours)
    merged = merge_logs(logs)                      # one picture of the race; merged["notes"] says what was left out and why
    r = review(merged)                             # pace by stint and driver, stops against the plan, fuel, traffic, positions, rivals
    r = review_folder(folder)                      # all of the above for every race*.jsonl.gz in a folder
    find_races(library_folder)                     # the folders that have race logs, newest first

Merging is generic: logs are joined by the race they are from (subsession) and the team (TeamID), never by a name. Nothing is
dropped without a line in `notes` that names the PC (the person whose log it is) and the reason: a log from another race, a log
without a header, gaps in the data, laps that two logs name different drivers for. Every lap that any log has is kept.

Traffic cost is an estimate: a lap counts as "in traffic" when a car of another class was within TRAFFIC_S seconds of the team
car for TRAFFIC_FRAMES of the one-a-second frames; its cost is the lap time less the driver's usual clean lap (the middle of his
quickest quarter of clean laps), only when positive.
"""
from __future__ import annotations

import math
import statistics
from pathlib import Path

from . import crewchief as cc
from . import endurance as E
from . import liverec

TRAFFIC_S = 3.0
TRAFFIC_FRAMES = 5
MIN_FADE_LAPS = 5
SLOW = cc.SLOW_FLAGS
LONG_STOP_S = 10.0
GLOB = "race*.jsonl.gz"
DUP_S = 1.5              # the same event in two logs: closer than this in time
STOP_DUP_S = 8.0         # the same stop in two logs: pit entries closer than this (a car does not stop twice in 8 s)


def _num(v):
    return cc._num(v)


def _med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def _pct(xs, p):
    xs = sorted(xs)
    if not xs:
        return None
    k = (len(xs) - 1) * p
    lo, hi = int(math.floor(k)), int(math.ceil(k))
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def _slope(ys):
    n = len(ys)
    if n < 3:
        return None
    mx = (n - 1) / 2.0
    my = sum(ys) / n
    den = sum((i - mx) ** 2 for i in range(n))
    return sum((i - mx) * (y - my) for i, y in enumerate(ys)) / den if den else None


# ---------------------------------------------------------------- reading
def load_log(path) -> dict:
    """One race log. Never raises: an unreadable file comes back with hdr None and an 'error'."""
    out = {"path": str(path), "name": Path(path).name, "hdr": None, "hdrs": [], "frames": [], "own": [], "events": [],
           "stops": [], "teamlaps": [], "replans": [], "rows": 0, "error": ""}
    try:
        for row in liverec.read_jsonl(path):
            out["rows"] += 1
            k = row.get("k")
            if k == "hdr":
                out["hdrs"].append(row)
                if out["hdr"] is None:
                    out["hdr"] = row
            elif k == "f":
                out["frames"].append(row)
            elif k == "o":
                out["own"].append(row)
            elif k == "stop":
                out["stops"].append(row)
            elif k == "teamlap":
                out["teamlaps"].append(row)
            elif k == "replan":
                out["replans"].append(row)
            elif k:
                out["events"].append(row)
    except Exception as e:  # noqa: BLE001
        out["error"] = e.__class__.__name__
    return out


def who_of(log) -> str:
    h = log.get("hdr") or {}
    return str(h.get("me") or "") or Path(log.get("name") or "a log").stem


# ---------------------------------------------------------------- merging
def merge_logs(logs: list) -> dict:
    notes: list = []
    used, skipped = [], []
    good = []
    for lg in logs:
        w = who_of(lg)
        if lg.get("error"):
            skipped.append({"who": w, "file": lg["name"], "why": "the file could not be read"})
            notes.append(f"{w}'s log ({lg['name']}) was left out: the file could not be read.")
        elif lg.get("hdr") is None:
            skipped.append({"who": w, "file": lg["name"], "why": "no header"})
            notes.append(f"{w}'s log ({lg['name']}) was left out: it has no header row, so Debrief can't tell which race it is from.")
        elif not lg["frames"]:
            skipped.append({"who": w, "file": lg["name"], "why": "no frames"})
            notes.append(f"{w}'s log ({lg['name']}) was left out: it has no readings of the field in it.")
        else:
            good.append(lg)
    if not good:
        return {"ok": False, "hdr": None, "notes": notes or ["There is no race log to read."], "used": [], "skipped": skipped,
                "frames": [], "own": [], "events": [], "stops": [], "teamlaps": [], "replans": [], "who": []}
    # the race most logs agree on (subsession + team); the biggest group wins
    groups: dict = {}
    for lg in good:
        h = lg["hdr"]
        groups.setdefault((h.get("sub"), h.get("team_id") or 0), []).append(lg)
    key = max(groups, key=lambda k: (len(groups[k]), sum(len(l["frames"]) for l in groups[k])))
    base = groups[key]
    for k, gl in groups.items():
        if k == key:
            continue
        for lg in gl:
            w = who_of(lg)
            skipped.append({"who": w, "file": lg["name"], "why": "another race or team"})
            notes.append(f"{w}'s log ({lg['name']}) was left out: it is from a different race or team "
                         f"(subsession {k[0]}, team {k[1]}), not the one most logs are from (subsession {key[0]}, team {key[1]}).")
    frames: dict = {}
    for lg in base:
        for fr in lg["frames"]:
            t = _num(fr.get("t"))
            if t is not None:
                frames.setdefault(int(round(t)), fr)
    fl = [frames[k] for k in sorted(frames)]
    for a, b in zip(fl, fl[1:]):
        if b["t"] - a["t"] > 5.0:
            notes.append(f"No readings between {cc.lap_text(a['t'])} and {cc.lap_text(b['t'])} into the session "
                         f"({b['t'] - a['t']:.0f} s): every log was off then (the PC that wrote it may have been closed or iRacing was away).")
    # the team car and its drivers: the header of the log with the most frames
    hdr = dict(max(base, key=lambda l: len(l["frames"]))["hdr"])
    drivers: dict = {}
    for lg in base:
        for d in lg["hdr"].get("drivers") or []:
            drivers.setdefault((d.get("idx"), d.get("name")), d)
    hdr["drivers"] = list(drivers.values())
    plan = next((l["hdr"].get("plan") for l in base if l["hdr"].get("plan")), None)
    hdr["plan"] = plan
    # own-car rows, by the PC that wrote them
    own = []
    for lg in base:
        w = who_of(lg)
        for r in lg["own"]:
            own.append(dict(r, who=w))
    own.sort(key=lambda r: r.get("t") or 0)
    # events and stops: the same one from two logs once. Two PCs read the same moment a fraction of a second apart, so the
    # time is compared with a tolerance (a rounded second would split 300.4 and 300.6 into two)
    ev, seen = [], {}
    for lg in base:
        for e in lg["events"]:
            key_ = (e.get("k"), e.get("n"), e.get("lap"), e.get("kind"), e.get("driver"))
            t_ = _num(e.get("t")) or 0.0
            if any(abs(t_ - u) < DUP_S for u in seen.get(key_, ())):
                continue
            seen.setdefault(key_, []).append(t_)
            ev.append(dict(e, **({"who": who_of(lg)} if e.get("k") in ("role",) else {})))
    ev.sort(key=lambda e: e.get("t") or 0)
    stops, seen_s = [], {}
    for lg in base:
        for r in lg["stops"]:
            t_ = _num(r.get("t_in")) or 0.0
            if any(abs(t_ - u) < STOP_DUP_S for u in seen_s.get(r.get("idx"), ())):
                continue
            seen_s.setdefault(r.get("idx"), []).append(t_)
            stops.append(r)
    stops.sort(key=lambda r: r.get("t_in") or 0)
    # team laps: every lap from any log; when two logs name different drivers for the same lap the one the driver's own PC wrote wins
    laps: dict = {}
    conflicts: list = []
    for lg in base:
        w = who_of(lg)
        for r in lg["teamlaps"]:
            n = r.get("lap")
            if n is None:
                continue
            r = dict(r, src=w)
            cur = laps.get(n)
            if cur is None:
                laps[n] = r
            elif (cur.get("driver") or None) != (r.get("driver") or None) and r.get("driver") and cur.get("driver"):
                from . import raceroom as RR
                mine = RR.match_name([r["driver"], cur["driver"]], w)
                if mine == r["driver"]:
                    laps[n] = r
                conflicts.append(n)
            elif cur.get("s") is None and r.get("s") is not None:
                laps[n] = r
    if conflicts:
        by = sorted({l["src"] for l in base})
        notes.append(f"{len(conflicts)} lap{'s' if len(conflicts) != 1 else ''} (first: lap {min(conflicts)}) were given different drivers by "
                     f"different logs ({', '.join(by)}); the driver's own log was believed.")
    for lg in base:
        used.append({"who": who_of(lg), "file": lg["name"], "frames": len(lg["frames"]), "own_rows": len(lg["own"])})
    return {"ok": True, "hdr": hdr, "frames": fl, "own": own, "events": ev, "stops": stops, "teamlaps": [laps[k] for k in sorted(laps)],
            "replans": [r for lg in base for r in lg["replans"]], "notes": notes, "used": used, "skipped": skipped,
            "who": sorted({who_of(l) for l in base})}


# ---------------------------------------------------------------- laps from the frames (fills any lap the logs did not name)
def laps_from_frames(frames, ti):
    """[{"lap","s","pit","slow","t"}] for the team car, from lap-completed changes and the last lap time."""
    out = []
    prev = None
    pit = slow = False
    pending = None
    for fr in frames:
        lc = fr.get("lc") or []
        if ti is None or ti >= len(lc):
            return out
        n = lc[ti]
        pits = fr.get("pit") or []
        if ti in pits:
            pit = True
        if (fr.get("fl") or 0) & SLOW:
            slow = True
        ll = (fr.get("ll") or [None] * (ti + 1))[ti]
        if pending is not None and ll is not None and ll > 0 and (ll != pending["ll0"] or fr["t"] - pending["t"] > 3):
            out.append({"lap": pending["lap"], "s": ll, "pit": pending["pit"], "slow": pending["slow"], "t": pending["t"]})
            pending = None
        if prev is not None and n is not None and n >= 0 and n > prev:
            if pending is not None:
                out.append({"lap": pending["lap"], "s": None, "pit": pending["pit"], "slow": pending["slow"], "t": pending["t"]})
            pending = {"lap": n, "t": fr["t"], "ll0": ll, "pit": pit, "slow": slow}
            pit = ti in pits
            slow = False
        if n is not None and n >= 0:
            prev = n
    if pending is not None:
        out.append({"lap": pending["lap"], "s": None, "pit": pending["pit"], "slow": pending["slow"], "t": pending["t"]})
    return out


# ---------------------------------------------------------------- the review
def _clean(rows, factor=1.35):
    """Clean laps: no pit, no caution/yellow, a time, and not far over the stint's typical time."""
    ok = [r for r in rows if r.get("s") and not r.get("pit") and not r.get("slow")]
    if not ok:
        return []
    m = _med([r["s"] for r in ok])
    return [r for r in ok if r["s"] <= m * factor]


def _pace(rows) -> dict:
    c = _clean(rows)
    ts = [r["s"] for r in c]
    d = {"laps": len(rows), "clean": len(c), "median": _med(ts), "best": min(ts) if ts else None, "spread": None, "fade": None,
         "fade_total": None}
    if len(ts) >= 5:
        d["spread"] = round(_pct(ts, 0.9) - _pct(ts, 0.1), 3)
    elif len(ts) >= 2:
        d["spread"] = round(max(ts) - min(ts), 3)
    if len(ts) >= MIN_FADE_LAPS:
        sl = _slope(ts)
        if sl is not None:
            d["fade"] = round(sl, 3)
            d["fade_total"] = round(sl * (len(ts) - 1), 2)
    for k in ("median", "best"):
        if d[k] is not None:
            d[k] = round(d[k], 3)
    return d


def review(m: dict) -> dict:
    """The Endurance review of a merged log (merge_logs' answer)."""
    if not m.get("ok"):
        return {"ok": False, "notes": m.get("notes") or [], "text": (m.get("notes") or ["No race log."])[0]}
    from . import raceroom as RR
    hdr, frames = m["hdr"], m["frames"]
    ti = hdr.get("team_idx")
    notes = list(m["notes"])
    drivers = {d.get("idx"): d for d in hdr.get("drivers") or [] if d.get("idx") is not None}
    class_of = {i: d.get("class") for i, d in drivers.items()}
    tclass = class_of.get(ti)
    plan = None
    if hdr.get("plan"):
        try:
            cfg = E._read(hdr["plan"])
            plan = {"cfg": cfg, "sim": E.simulate(cfg)}
        except (ValueError, KeyError, TypeError):
            notes.append("The plan saved in the log could not be read, so the stops are not compared with it.")
    pnames = [d["name"] for d in plan["cfg"]["drivers"]] if plan else []
    # --- laps: the names the logs gave, plus any lap only the frames show
    by_lap = {r["lap"]: dict(r) for r in m["teamlaps"] if r.get("lap") is not None}
    ff = laps_from_frames(frames, ti)
    added = 0
    for r in ff:
        if r["lap"] not in by_lap:
            by_lap[r["lap"]] = {"lap": r["lap"], "s": r["s"], "pit": r["pit"], "slow": r["slow"], "driver": None, "src": "frames"}
            added += 1
        elif by_lap[r["lap"]].get("s") is None and r["s"]:
            by_lap[r["lap"]]["s"] = r["s"]
    all_laps = [by_lap[k] for k in sorted(by_lap)]
    missing = [n for n in range(1, (all_laps[-1]["lap"] if all_laps else 0) + 1) if n not in by_lap]
    if missing:
        notes.append(f"No reading of lap{'s' if len(missing) != 1 else ''} {', '.join(map(str, missing[:8]))}"
                     f"{' and more' if len(missing) > 8 else ''}: they are missing from every log.")
    # --- team stops and stints
    tstops = [s for s in m["stops"] if s.get("idx") == ti]
    team_stops = [{"n": k + 1, "lap": s.get("lap"), "lane_s": s.get("lane_s"), "stall_s": s.get("stall_s"), "t_in": s.get("t_in"),
                   "t_out": s.get("t_out"), "under_caution": s.get("under_caution")} for k, s in enumerate(tstops)]
    bounds = [s["lap"] for s in team_stops if s.get("lap") is not None]
    # a lap belongs to the stint of the stop that follows it: a stop on lap L ends the stint with lap L in it
    stints, cur = [], []
    bi = 0
    for r in all_laps:
        cur.append(r)
        if bi < len(bounds) and r["lap"] >= bounds[bi]:
            stints.append(cur)
            cur = []
            bi += 1
    if cur:
        stints.append(cur)
    # a driver per stint: the one most of its laps name
    st_rows = []
    unnamed = 0
    for k, rows in enumerate(stints):
        names = [r.get("driver") for r in rows if r.get("driver")]
        drv = max(set(names), key=names.count) if names else None
        guessed = drv is None
        if drv is None:
            unnamed += 1
            ev = [e for e in m["events"] if e.get("k") == "stint" and e.get("n") == k + 1 and e.get("driver")]
            drv = ev[0]["driver"] if ev else (pnames[k % len(pnames)] if pnames else None)
        p = _pace(rows)
        st_rows.append({"n": k + 1, "driver": drv, "from_lap": rows[0]["lap"], "to_lap": rows[-1]["lap"], "guessed": guessed, **p})
    if unnamed:
        notes.append(f"{unnamed} stint{'s' if unnamed != 1 else ''} had no driver in any log: named from the plan's order instead (marked as guessed).")
    # --- drivers
    by_drv: dict = {}
    for r, rows in zip(st_rows, stints):
        by_drv.setdefault(r["driver"] or "unknown", []).append((r, rows))
    drv_rows = []
    for name, lst in by_drv.items():
        rows = [x for _r, rs in lst for x in rs]
        p = _pace(rows)
        stint_fades = [r["fade_total"] for r, _ in lst if r["fade_total"] is not None]
        drv_rows.append({"name": name, "stints": len(lst), "laps": p["laps"], "clean": p["clean"], "median": p["median"],
                         "best": p["best"], "spread": p["spread"], "fade": round(statistics.mean(stint_fades), 2) if stint_fades else None,
                         "drive_s": round(sum(x.get("s") or 0 for x in rows), 0)})
    drv_rows.sort(key=lambda d: -d["laps"])
    # --- stops against the plan
    pstops = [s for s in (plan["sim"]["stints"] if plan else []) if s.get("stop")]
    stop_rows = []
    for s in team_stops:
        ps = pstops[s["n"] - 1]["stop"] if s["n"] - 1 < len(pstops) else None
        d = {**s, "plan_stop_s": ps["stop_s"] if ps else None, "plan_swap": ps["swap"] if ps else None,
             "plan_fuel_add": ps["fuel_add_l"] if ps else None}
        if ps and s.get("lane_s") is not None:
            d["delta_s"] = round(s["lane_s"] - ps["stop_s"], 1)
            d["long"] = d["delta_s"] > LONG_STOP_S
        stop_rows.append(d)
    # --- fuel: only where a driver's own PC wrote it
    fuel_rows = []
    for k, r in enumerate(st_rows):
        t0 = team_stops[k - 1]["t_out"] if k > 0 and k - 1 < len(team_stops) else (frames[0]["t"] if frames else 0)
        t1 = team_stops[k]["t_in"] if k < len(team_stops) else (frames[-1]["t"] if frames else 0)
        rows = [o for o in m["own"] if o.get("fuel") is not None and t0 + 5 <= (o.get("t") or 0) <= t1 - 5]
        if len(rows) < 2:
            fuel_rows.append({"stint": r["n"], "driver": r["driver"], "measured": False,
                              "text": "No fuel reading: nobody's log from this stint has the car's fuel."})
            continue
        used = rows[0]["fuel"] - rows[-1]["fuel"]
        la = _lap_at(frames, rows[-1]["t"], ti) - _lap_at(frames, rows[0]["t"], ti)
        if la < 2 or used <= 0:
            fuel_rows.append({"stint": r["n"], "driver": r["driver"], "measured": False, "text": "Too short to measure."})
            continue
        burn = used / la
        pn = RR.match_name(pnames, r["driver"]) if pnames else None
        pb = plan["cfg"]["drivers"][pnames.index(pn)]["burn"] if pn else None
        fuel_rows.append({"stint": r["n"], "driver": r["driver"], "measured": True, "burn_l": round(burn, 3), "plan_burn_l": pb,
                          "delta_pct": None if pb is None else round((burn / pb - 1) * 100, 1), "by": rows[0].get("who")})
    # --- traffic
    traffic = _traffic(frames, ti, tclass, class_of, all_laps, st_rows, stints)
    # --- positions
    pos = []
    last_n = None
    for fr in frames:
        n = (fr.get("lc") or [None] * (ti + 1))[ti] if ti is not None and ti < len(fr.get("lc") or []) else None
        if n is not None and n != last_n and n >= 0:
            last_n = n
            cp = (fr.get("cp") or [0] * (ti + 1))[ti]
            po = (fr.get("po") or [0] * (ti + 1))[ti]
            if cp:
                pos.append({"lap": n, "cpos": cp, "pos": po, "t": fr["t"]})
    # --- rivals in the class
    rivals = _rivals(frames, m["stops"], drivers, class_of, tclass, ti)
    laps_sum = {"count": len(all_laps), "missing": missing, "from_frames": added}
    best = min(pos, key=lambda p: p["cpos"]) if pos else None
    out = {"ok": True, "track": hdr.get("track"), "team": (drivers.get(ti) or {}).get("team") or "", "who": m["who"], "used": m["used"],
           "skipped": m["skipped"], "notes": notes, "laps": laps_sum, "stints": st_rows, "drivers": drv_rows, "stops": stop_rows,
           "fuel": fuel_rows, "traffic": traffic, "positions": pos, "rivals": rivals,
           "finish": {"cpos": pos[-1]["cpos"], "pos": pos[-1]["pos"], "laps": pos[-1]["lap"], "best_cpos": best["cpos"]} if pos else None,
           "lap_list": [{"lap": r["lap"], "s": r.get("s"), "driver": r.get("driver"), "pit": bool(r.get("pit")), "slow": bool(r.get("slow"))}
                        for r in all_laps]}
    return out


def _lap_at(frames, t, ti):
    best = 0
    for fr in frames:
        if fr["t"] > t:
            break
        lc = fr.get("lc") or []
        if ti is not None and ti < len(lc) and lc[ti] is not None and lc[ti] >= 0:
            best = lc[ti]
    return best


def _traffic(frames, ti, tclass, class_of, all_laps, st_rows, stints) -> dict:
    """Laps that other classes' cars were within TRAFFIC_S of the team car for TRAFFIC_FRAMES readings, and what they cost."""
    out = {"laps": 0, "cost_s": 0.0, "by_class": [], "rows": [], "note": "An estimate: lap time less the driver's usual clean lap, on laps with other-class cars near."}
    if ti is None or tclass is None:
        return out
    # frames per lap
    per: dict = {}
    prev = None
    cur_lap = None
    for fr in frames:
        lc = fr.get("lc") or []
        if ti >= len(lc) or lc[ti] is None or lc[ti] < 0:
            continue
        n = lc[ti]
        if prev is not None and n > prev:
            cur_lap = n
        prev = n
        per.setdefault(n + 1, []).append(fr)
    lap_rows = {r["lap"]: r for r in all_laps}
    base_by_driver: dict = {}
    for srow, rows in zip(st_rows, stints):
        base_by_driver.setdefault(srow["driver"], []).extend(rows)
    base = {}
    for d, rows in base_by_driver.items():
        ts = sorted(r["s"] for r in _clean(rows))
        if len(ts) >= 4:
            q = ts[: max(2, len(ts) // 4)]
            base[d] = statistics.median(q)
    cost_by_class: dict = {}
    for lap, frs in per.items():
        row = lap_rows.get(lap)
        if not row or not row.get("s") or row.get("pit") or row.get("slow"):
            continue
        lap_s = row["s"]
        hits: dict = {}
        for fr in frs:
            lc, lp, pit = fr.get("lc") or [], fr.get("lp") or [], set(fr.get("pit") or [])
            if ti >= len(lp) or lp[ti] is None or lp[ti] < 0 or lc[ti] is None:
                continue
            mine = lc[ti] + lp[ti]
            near = set()
            for j, c in class_of.items():
                if j == ti or c == tclass or j in pit or j >= len(lp) or lp[j] is None or lp[j] < 0 or lc[j] is None or lc[j] < 0:
                    continue
                d = (lc[j] + lp[j]) - mine
                d -= round(d)
                if abs(d) * lap_s <= TRAFFIC_S:
                    near.add(c)
            for c in near:
                hits[c] = hits.get(c, 0) + 1
        if not hits or max(hits.values()) < TRAFFIC_FRAMES:
            continue
        b = base.get(row.get("driver"))
        if b is None:
            continue
        cost = max(0.0, lap_s - b)
        cls = max(hits, key=hits.get)
        out["laps"] += 1
        out["cost_s"] += cost
        cost_by_class[cls] = cost_by_class.get(cls, 0.0) + cost
        out["rows"].append({"lap": lap, "driver": row.get("driver"), "cost_s": round(cost, 2), "class": cls})
    out["cost_s"] = round(out["cost_s"], 1)
    out["by_class"] = [{"class": c, "cost_s": round(v, 1)} for c, v in sorted(cost_by_class.items(), key=lambda kv: -kv[1])]
    out["rows"] = out["rows"][:200]
    return out


def _rivals(frames, stops, drivers, class_of, tclass, ti) -> list:
    out = []
    if not frames:
        return out
    last = frames[-1]
    by_stops: dict = {}
    for s in stops:
        by_stops.setdefault(s.get("idx"), []).append(s)
    if not stops:                      # a log without stop rows: found again from the frames
        from . import raceroom as RR
        tr = RR.StopTracker()
        for fr in frames:
            n = len(fr.get("lc") or [])
            pit = [False] * n
            for i in fr.get("pit") or []:
                if i < n:
                    pit[i] = True
            surf = [3] * n
            for i in fr.get("sf") or []:
                if i < n:
                    surf[i] = 1
            tr.feed(fr["t"], {"CarIdxOnPitRoad": pit, "CarIdxTrackSurface": surf, "CarIdxLapCompleted": fr.get("lc")}, (),
                    bool((fr.get("fl") or 0) & cc.FULL_COURSE))
        by_stops = {i: v for i, v in tr.stops.items()}
    for j, c in class_of.items():
        if c != tclass or drivers.get(j, {}).get("pace"):
            continue
        d = drivers.get(j) or {}
        lc, cp = last.get("lc") or [], last.get("cp") or []
        if j >= len(lc) or lc[j] is None or lc[j] < 0:
            continue
        lt = []
        prev = None
        for fr in frames:
            lcj = (fr.get("lc") or [])[j] if j < len(fr.get("lc") or []) else None
            ll = (fr.get("ll") or [])[j] if j < len(fr.get("ll") or []) else None
            if prev is not None and lcj is not None and lcj > prev and ll and ll > 0:
                lt.append(ll)
            if lcj is not None:
                prev = lcj
        st = by_stops.get(j) or []
        out.append({"idx": j, "num": d.get("num"), "name": d.get("name"), "me": j == ti, "laps": lc[j],
                    "final_pos": cp[j] if j < len(cp) else None, "stops": len(st),
                    "stop_laps": [s.get("lap") for s in st][:12], "avg_lane_s": round(_med([s.get("lane_s") for s in st]) or 0, 1) if st else None,
                    "under_caution": sum(1 for s in st if s.get("under_caution")), "median_lap": round(_med(lt), 3) if lt else None,
                    "best_lap": round(min(lt), 3) if lt else None})
    out.sort(key=lambda r: (r["final_pos"] or 999))
    return out


# ---------------------------------------------------------------- finding races
def race_logs(folder) -> list:
    p = Path(folder)
    try:
        return sorted(p.glob(GLOB))
    except OSError:
        return []


def find_races(library_folder, limit=40) -> list:
    """Folders with race logs, newest first: [{"id", "title", "folder", "files", "mtime"}]. id is the folder's path under the library."""
    out = []
    lib = Path(library_folder)
    roots = [lib / "Endurance", liverec.recordings_root(lib)]
    for root in roots:
        try:
            for d in root.iterdir():
                if not d.is_dir():
                    continue
                fs = race_logs(d)
                if fs:
                    out.append({"id": str(d.relative_to(lib)).replace("\\", "/"), "title": d.name, "folder": str(d),
                                "files": [f.name for f in fs], "mtime": max(f.stat().st_mtime for f in fs)})
        except OSError:
            continue
    out.sort(key=lambda r: -r["mtime"])
    return out[:limit]


def review_folder(folder) -> dict:
    logs = [load_log(p) for p in race_logs(folder)]
    if not logs:
        return {"ok": False, "notes": ["There is no race log in that folder."], "text": "There is no race log in that folder."}
    return review(merge_logs(logs))
