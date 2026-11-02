"""What a replay scan means for this driver's race (2.8.0): laps lined up with the review, gaps, clean air against
fighting, pace against similar cars, where passes happen, what stops and incidents cost, and whether a corner's lost
time is driving or racecraft. Pure arithmetic on a saved scan and review: no requests, and the review's own numbers
never change. Positions are on the road; correlations are described as such, never as causes.
"""
from __future__ import annotations

import bisect
import statistics as st

MATCH_S = 0.25        # a review lap and a scan lap this close are the same lap (both are timed to a few hundredths)
FIGHT_GAP = 1.0       # within this of a car at the line counts as fighting
CLEAR_GAP = 2.0       # this far from both neighbours is clear air
SIMILAR = 0.04        # without class data, "similar cars" are within 4% on best lap
MIN_LAPS = 2
RACECRAFT_MIN_S = 0.10
LONG_STOP_S = 60.0
SETTLE_S = 8.0        # iRacing's position reading settles this long after the line
MAX_GAP_S = 60.0
MIN_PEER_LAPS = 5
PASS_WINDOW_S = 8.0
PASS_NEAR = 0.1       # a tenth of a lap: closer than this on both sides for a swap to be a pass
TOW_JUMP = 0.03
NEAR_FIGHT_S = 5.0    # this long within a second of a car counts as racing it
NEAR_CLEAR_S = 2.0


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _times(car):
    out = {}
    for k, v in (car.get("lap_times") or {}).items():
        n, t = _int(k), _num(v)
        if n is not None and t and t > 0:
            out[n] = t
    return out


def _cross(car):
    out = {}
    for k, v in (car.get("line_crossings") or {}).items():
        n, t = _int(k), _num(v)
        if n is not None and t is not None:
            out[n] = t
    return out


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _r(v, d=2):
    return None if v is None else round(float(v), d)


def align_laps(review_laps, car) -> dict:
    """{review lap number: race lap number}, by lap time and in order. Garage 61 and the replay number laps differently,
    and the difference isn't constant (a race with long pit stops changes it)."""
    scan = sorted(_times(car).items())
    out, at = {}, 0
    for l in sorted((x for x in review_laps or [] if isinstance(x, dict) and _num(x.get("t")) and x.get("n") is not None), key=lambda x: x["n"]):
        for i in range(at, len(scan)):
            if abs(scan[i][1] - l["t"]) <= MATCH_S:
                out[int(l["n"])] = scan[i][0]
                at = i + 1
                break
    return out


def _cls(car):
    return car.get("car_class")


def positions(samples, car) -> list:
    """iRacing's own running position at the end of each lap, for this car. It settles a few seconds after the line, and
    it's the one position every car always has: the scan's lap order leaves out cars away from the replay camera."""
    idx = car.get("car_idx")
    if not samples or idx not in (samples.get("idxs") or []) or not samples.get("samples"):
        return []
    col = samples["idxs"].index(idx)
    rows = samples["samples"]
    times = [r[0] for r in rows]
    out = []
    for n, t in sorted(_cross(car).items()):
        if n < 1:
            continue
        k = bisect.bisect_left(times, t + SETTLE_S)
        if k >= len(rows):
            k = len(rows) - 1
        p = rows[k][5][col] if rows[k][5] and col < len(rows[k][5]) else None
        if isinstance(p, int) and p > 0:
            out.append({"lap": n, "pos": p})
    return out


def _valid(v):
    return isinstance(v, (int, float)) and 0 <= v <= 1


def _wrapped(a, b):
    d = (a - b) % 1.0
    return d - 1.0 if d > 0.5 else d


def real_pass(samples, my_idx, other_idx, t, made) -> bool | None:
    """Did the two cars really swap on track around t? None when the readings can't say. A swap seen only in the
    estimated order, with the cars a third of a lap apart throughout, isn't a pass."""
    idxs = (samples or {}).get("idxs") or []
    if my_idx not in idxs or other_idx not in idxs:
        return None
    a, b = idxs.index(my_idx), idxs.index(other_idx)
    rows = samples.get("samples") or []
    times = [r[0] for r in rows]
    leads = {"before": [], "after": []}
    for k in range(bisect.bisect_left(times, t - PASS_WINDOW_S), bisect.bisect_right(times, t + PASS_WINDOW_S)):
        p = rows[k][2] or []
        if a < len(p) and b < len(p) and _valid(p[a]) and _valid(p[b]) and abs(rows[k][0] - t) >= 0.5:
            leads["before" if rows[k][0] < t else "after"].append(_wrapped(p[a], p[b]))
    if not leads["before"] or not leads["after"]:
        return None
    x, y = leads["before"][-1], leads["after"][0]
    close = abs(x) < PASS_NEAR and abs(y) < PASS_NEAR
    return close and (x < 0 < y if made else y < 0 < x)


def lap_at_time(samples, idx, t):
    """The lap the car is on by iRacing's lap counter, which keeps counting when a line crossing was missed."""
    idxs = (samples or {}).get("idxs") or []
    if idx not in idxs or not samples.get("samples") or not isinstance(t, (int, float)):
        return None
    rows = samples["samples"]
    k = min(bisect.bisect_left([r[0] for r in rows], t), len(rows) - 1)
    v = (rows[k][6] or [None] * len(idxs))[idxs.index(idx)]
    return v + 1 if isinstance(v, int) and v >= 0 else None


def position_at(samples, idx, t):
    idxs = (samples or {}).get("idxs") or []
    if idx not in idxs or not samples.get("samples"):
        return None
    rows = samples["samples"]
    k = min(bisect.bisect_left([r[0] for r in rows], t), len(rows) - 1)
    p = (rows[k][5] or [None] * len(idxs))[idxs.index(idx)]
    return p if isinstance(p, int) and p > 0 else None


def stop_kind(samples, idx, p) -> str:
    """"tow" when the car appears in its stall from elsewhere on the track, "drive" for no stationary time, else "stop"."""
    idxs = (samples or {}).get("idxs") or []
    t = _num(p.get("in"))
    if idx in idxs and t is not None and samples.get("samples"):
        col = idxs.index(idx)
        rows = samples["samples"]
        k = bisect.bisect_left([r[0] for r in rows], t)
        here = next((rows[j][2][col] for j in range(k, min(k + 6, len(rows))) if rows[j][2] and _valid(rows[j][2][col])), None)
        prev = next((rows[j][2][col] for j in range(k - 1, max(k - 40, -1), -1) if rows[j][2] and _valid(rows[j][2][col]) and not (rows[j][4] or [0] * (col + 1))[col]), None)
        if here is not None and prev is not None and abs(_wrapped(here, prev)) > TOW_JUMP:
            return "tow"
    return "drive" if not (_num(p.get("stationary_s")) or 0) and (_num(p.get("seconds")) or 0) < 60 else "stop"


def _near_cars(samples, me_car, by_idx, t0, t1, lap_s):
    """(seconds within a second of a car on your lap, its name, seconds near a car on another lap, its name), from all readings."""
    idxs = (samples or {}).get("idxs") or []
    idx = me_car.get("car_idx")
    if idx not in idxs or t0 is None or t1 is None or not lap_s:
        return None, None, None, None
    me = idxs.index(idx)
    rows = samples["samples"]
    times = [r[0] for r in rows]
    span = FIGHT_GAP / lap_s
    seen, other = {}, {}
    total = traffic = 0.0
    lo, hi = bisect.bisect_left(times, t0), bisect.bisect_right(times, t1)
    for k in range(lo, hi):
        p, pit = rows[k][2] or [], rows[k][4] or []
        if me >= len(p) or not _valid(p[me]) or (me < len(pit) and pit[me]):
            continue
        step = times[k] - times[k - 1] if k else 0.0
        laps = rows[k][6] or []
        near = [i for i, v in enumerate(p) if i != me and _valid(v) and abs(_wrapped(v, p[me])) < span and not (i < len(pit) and pit[i])]
        mine = (laps[me] + p[me]) if me < len(laps) and isinstance(laps[me], int) else None
        same = [i for i in near if mine is not None and i < len(laps) and isinstance(laps[i], int) and abs(laps[i] + p[i] - mine) < span]
        if same:
            total += step
            for i in same:
                seen[i] = seen.get(i, 0.0) + step
        elif near:
            traffic += step
            for i in near:
                other[i] = other.get(i, 0.0) + step
    name = (by_idx.get(idxs[max(seen, key=seen.get)]) or {}).get("name") if seen else None
    oname = (by_idx.get(idxs[max(other, key=other.get)]) or {}).get("name") if other else None
    return total, name, traffic, oname


def _neighbours(samples, me_car, n, t, by_idx, cross):
    """(car ahead, its gap, car behind, its gap) at the line, from iRacing's order. A car too far from the camera to have
    a crossing near this moment has no gap: if it were close, it would be in view."""
    idxs = (samples or {}).get("idxs") or []
    idx = me_car.get("car_idx")
    if t is None or idx not in idxs:
        return None, None, None, None
    rows = samples["samples"]
    k = min(bisect.bisect_left([r[0] for r in rows], t + SETTLE_S), len(rows) - 1)
    pos = rows[k][5] or []
    cls = _cls(me_car)
    order = sorted((p, i) for p, i in zip(pos, idxs) if isinstance(p, int) and p > 0 and i in by_idx and (cls is None or _cls(by_idx[i]) == cls))
    ids = [i for _, i in order]
    if idx not in ids:
        return None, None, None, None
    at = ids.index(idx)

    def gap(other, sign):
        times = [c for c in cross[str(by_idx[other].get("number"))].values() if 0 <= (t - c) * sign <= MAX_GAP_S]
        return min((abs(t - c) for c in times), default=None)
    a = ids[at - 1] if at else None
    b = ids[at + 1] if at + 1 < len(ids) else None
    return (by_idx[a].get("name") if a is not None else None, gap(a, 1) if a is not None else None,
            by_idx[b].get("name") if b is not None else None, gap(b, -1) if b is not None else None)


def _state(n, a_gap, b_gap, near_s, traffic_s, pits, incidents, fights):
    """From every car in view during the lap. Without readings, it's unknown, never clear air."""
    if n == 1:
        return "start"
    if any(p <= n <= q for p, q in pits):
        return "pit"
    if n in incidents:
        return "incident"
    near = [g for g in (a_gap, b_gap) if g is not None and g >= 0]
    if any(g < FIGHT_GAP for g in near) or n in fights or (near_s or 0) >= NEAR_FIGHT_S:
        return "fight"
    if near_s is None:
        return "unknown"
    if (traffic_s or 0) >= NEAR_FIGHT_S:
        return "traffic"
    if near_s < NEAR_CLEAR_S and all(g >= CLEAR_GAP for g in near):
        return "clear"
    return "close"


def _median(v):
    return st.median(v) if v else None


def _corner_at(metres, corners):
    for c in corners:
        s, e = _num(c.get("start_m")), _num(c.get("end_m"))
        if s is not None and e is not None and s <= metres <= e:
            return c.get("id")
    return None


def _pct_lookup(samples, idx):
    if not samples or idx is None or idx not in (samples.get("idxs") or []):
        return None
    col, rows = samples["idxs"].index(idx), samples.get("samples") or []
    times = [r[0] for r in rows]

    def at(t):
        import bisect
        k = min(bisect.bisect_left(times, t), len(rows) - 1)
        p = rows[k][2]
        return p[col] if p and col < len(p) else None
    return at


def build(scan: dict, me_car: dict, rc: dict, data: dict, samples: dict | None = None) -> dict:
    me = str(me_car.get("number"))
    cars = {str(c.get("number")): c for c in scan.get("cars") or [] if isinstance(c, dict)}
    cross = {k: _cross(c) for k, c in cars.items()}
    mine = _times(me_car)
    lap_map = align_laps(data.get("laps"), me_car)
    back = {v: k for k, v in lap_map.items()}
    pit_spans = []
    idx = me_car.get("car_idx")
    my_stops = [p for p in scan.get("pit_stops") or [] if isinstance(p, dict) and str((p.get("car") or {}).get("number")) == me]
    for p in my_stops:
        pit_spans.append((_lap_between(cross[me], p.get("in")), _lap_between(cross[me], p.get("out"))))
    incident_laps = {i.get("lap") for i in rc.get("incidents") or []}
    fight_laps = set()
    for f in rc.get("fights") or []:
        if (f.get("seconds") or 0) >= 15:
            fight_laps.update(range(f.get("lap_from") or 0, (f.get("lap_to") or 0) + 1))
    best = min(mine.values()) if mine else None
    rows = []
    by_idx = {c.get("car_idx"): c for c in cars.values()}
    pos_at = {x["lap"]: x["pos"] for x in rc.get("positions") or []}
    for n in sorted(set(mine) | {x for x in cross[me] if x >= 1}):
        a_n, a_g, b_n, b_g = _neighbours(samples, me_car, n, cross[me].get(n), by_idx, cross)
        near_s, near_name, traffic_s, traffic_name = _near_cars(samples, me_car, by_idx, cross[me].get(n - 1), cross[me].get(n), mine.get(n)) if samples else (None, None, None, None)
        rows.append({"lap": n, "review_lap": back.get(n), "t": _r(mine.get(n), 3), "pos": pos_at.get(n), "near_s": _r(near_s, 1), "near": near_name, "traffic_s": _r(traffic_s, 1), "traffic": traffic_name,
                     "ahead": a_n, "gap_ahead": _r(a_g), "behind": b_n, "gap_behind": _r(b_g),
                     "state": _state(n, a_g, b_g, near_s, traffic_s, pit_spans, incident_laps, fight_laps)})
    # After a long stop (repairs) the car may not be the same car, so clear air against fighting is only compared before it
    long_stop = min((a for (a, _b), p in zip(pit_spans, [x for x in scan.get("pit_stops") or [] if isinstance(x, dict) and str((x.get("car") or {}).get("number")) == me])
                     if (_num(p.get("seconds")) or 0) > LONG_STOP_S and a), default=None)
    usable = [r for r in rows if r["t"] and best and r["t"] <= best * 1.07]
    comparable = [r for r in usable if long_stop is None or r["lap"] < long_stop]
    by = {s: [r["t"] for r in comparable if r["state"] == s] for s in ("clear", "close", "fight", "traffic")}
    clear, fight = _median(by["clear"]), _median(by["fight"] + by["close"] + by["traffic"])
    pace = {"clear": _r(clear, 3), "fight": _r(fight, 3), "n_clear": len(by["clear"]), "n_fight": len(by["fight"]) + len(by["close"]) + len(by["traffic"]),
            "fight_cost": _r(fight - clear, 2) if clear and fight and len(by["clear"]) >= MIN_LAPS and len(by["fight"]) + len(by["close"]) + len(by["traffic"]) >= MIN_LAPS else None,
            "best": _r(best, 3), "typical": _r(_median([r["t"] for r in usable]), 3)}
    pace.update(_peers(cars, me_car, me, best, pace["typical"]))
    corners = [c for c in data.get("corners") or [] if isinstance(c, dict)]
    return {"lap_map": {str(k): v for k, v in lap_map.items()}, "matched": len(lap_map), "review_laps": len(data.get("laps") or []),
            "laps": rows, "pace": pace, "pit_cost": _pits(rc, samples, idx, my_stops), "incident_cost": _incidents(rc, rows, clear),
            "pass_corners": _pass_corners(rc, corners, data, _pct_lookup(samples, me_car.get("car_idx"))),
            "corner_context": _corner_context(corners, lap_map, {r["lap"]: r["state"] for r in comparable}),
            "craft": _craft(rc), "verdict": _verdict(rc, rows, pace, scan)}


def _lap_between(cross, t):
    t = _num(t)
    if t is None:
        return 0
    for n in sorted(cross):
        if n >= 1 and t <= cross[n]:
            return n
    return max(cross) if cross else 0


def _peers(cars, me_car, me, best, typical):
    cls = _cls(me_car)
    pool, label = [], "similar cars"
    for k, c in cars.items():
        t = _times(c)
        if k == me or not t:
            continue
        if len(t) < MIN_PEER_LAPS:      # too few laps in view of the replay camera to judge
            continue
        b = min(t.values())
        if cls is not None:
            label = "your class"
            if _cls(c) != cls:
                continue
        elif not best or abs(b - best) > SIMILAR * best:
            continue
        pool.append((b, _median([x for x in t.values() if x <= b * 1.07])))
    if not pool or not best:
        return {"peers": 0, "peer_label": label}
    typ = sorted(x[1] for x in pool if x[1])
    out = {"peers": len(pool), "peer_label": label, "peer_best": _r(min(x[0] for x in pool), 3), "peer_typical": _r(_median(typ), 3)}
    if typical and typ:
        out["vs_peers"] = _r(typical - out["peer_typical"], 2)
        out["peer_rank"] = 1 + sum(1 for x in typ if x < typical)
    return out


def _pits(rc, samples, idx, stops):
    out = []
    for p, row in zip(stops, rc.get("pits") or []):
        a, b = _num(p.get("in")), _num(p.get("out"))
        before = position_at(samples, idx, a - 3) if a is not None else None
        after = position_at(samples, idx, b + SETTLE_S) if b is not None else None
        out.append({"lap": row.get("lap"), "seconds": row.get("seconds"), "kind": stop_kind(samples, idx, p), "before": before, "after": after,
                    "places": after - before if before and after else None})
    return {"stops": out, "total_s": _r(sum(x.get("seconds") or 0 for x in rc.get("pits") or []), 1)}


def _incidents(rc, rows, clear):
    t = {r["lap"]: r["t"] for r in rows}
    pos = {r["lap"]: r["pos"] for r in rows if r["pos"]}
    out = []
    for i in rc.get("incidents") or []:
        lap, lost = i.get("lap"), i.get("places_lost")
        before = next((pos[n] for n in range((lap or 1) - 1, 0, -1) if n in pos), None)
        back = next((n for n in sorted(pos) if lap and n > lap and before and pos[n] <= before), None)
        out.append({"lap": lap, "places": lost, "seconds": _r(t[lap] - clear, 1) if clear and t.get(lap) else None,
                    "back_by_lap": back})
    return out


def _pass_corners(rc, corners, data, pct_at):
    L = _num(data.get("L"))
    if not pct_at or not L or not corners:
        return []
    tally = {}
    for p in rc.get("passes") or []:
        if p.get("how") != "on track" or (p.get("lap") or 0) <= 1:
            continue
        pct = pct_at(p.get("t") or 0)
        cid = _corner_at(pct * L, corners) if isinstance(pct, (int, float)) else None
        if cid:
            row = tally.setdefault(cid, {"corner": cid, "made": 0, "lost": 0})
            row["made" if p.get("made") else "lost"] += 1
    return sorted(tally.values(), key=lambda r: -(r["made"] + r["lost"]))


def _corner_context(corners, lap_map, states) -> dict:
    """Per corner: is the time lost in clear air too (driving), or mainly when fighting (racecraft)? Needs both kinds of lap."""
    out = {}
    for c in corners:
        clear, busy = [], []
        for k, m in (c.get("per_lap") or {}).items():
            race = lap_map.get(_int(k))
            t = _num((m or {}).get("t")) if isinstance(m, dict) else None
            if race is None or t is None:
                continue
            s = states.get(race)
            if s == "clear":
                clear.append(t)
            elif s in ("fight", "close", "traffic"):
                busy.append(t)
        if len(clear) < MIN_LAPS or len(busy) < MIN_LAPS:
            continue
        d = _median(busy) - _median(clear)
        avail = _num(c.get("avail")) or 0.0
        kind = "racecraft" if d >= RACECRAFT_MIN_S and d >= 0.5 * avail else "driving"
        out[str(c.get("id"))] = {"kind": kind, "busy_vs_clear": _r(d, 2), "n_clear": len(clear), "n_busy": len(busy)}
    return out


def _craft(rc) -> dict:
    on = [p for p in rc.get("passes") or [] if p.get("how") == "on track" and (p.get("lap") or 0) > 1]
    made = [p for p in on if p.get("made")]
    grid, after = rc.get("grid"), rc.get("pos_after_lap1")
    return {"made": len(made), "lost": len(on) - len(made), "stuck": sum(1 for p in made if p.get("stuck")),
            "lap1_gain": grid - after if isinstance(grid, int) and isinstance(after, int) else None,
            "fights": len(rc.get("fights") or []), "incidents": len(rc.get("incidents") or [])}


def _clock(s):
    m = int(s // 60)
    return f"{m}:{int(s % 60):02d}" if m else f"{s:.0f} s"


def _verdict(rc, rows, pace, scan) -> str:
    bits = []
    grid, fin = rc.get("grid"), rc.get("finish")
    where = " overall." if rc.get("official") else " overall on the road."
    if grid and fin:
        bits.append(f"P{grid} to P{fin}" + where)
    elif fin:
        bits.append(f"Finished P{fin}" + where)
    total = sum(p.get("seconds") or 0 for p in rc.get("pits") or [])
    laps = scan.get("laps")
    done = max((r["lap"] for r in rows), default=None)
    if total >= 60:
        bits.append(f"{_clock(total)} in the pits, {done} laps completed." if done else f"{_clock(total)} in the pits.")
    elif laps and done and done < laps:
        bits.append(f"{done} laps completed.")
    v = pace.get("vs_peers")
    if v is not None:
        who = pace.get("peer_label")
        bits.append(f"Your typical lap was {abs(v):.1f} s {'off' if v > 0 else 'quicker than'} {who}." if abs(v) >= 0.05 else f"Your typical lap matched {who}.")
    return " ".join(bits)
