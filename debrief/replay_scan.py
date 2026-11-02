"""The replay scan (2.7.5), built into Debrief from RaceCap's RaceScan. Same file format, so each app can read the other's scans.

It runs the loaded replay through the race at high speed, records nothing, and writes down WHEN things happen for every
car: green flag, line crossings, running order, passes, battles, incidents, pit stops, cars dropping out. Official numbers
still come from the iRacing results.

collect() talks to iRacing and only gathers samples; analyse() is pure arithmetic on them; for_review() ties it to a review.
"""
from __future__ import annotations

import gzip
import json
import os
import re
import statistics
import time
from pathlib import Path


def mmss(t):
    """Seconds as m:ss."""
    t = max(0.0, float(t))
    return f"{int(t // 60)}:{int(t % 60):02d}"


def noun(n, one, many=None):
    return one if n == 1 else (many or one + "s")


def plural(n, one, many=None):
    return f"{n} {noun(n, one, many)}"


def parse_time(v):
    if isinstance(v, (int, float)):
        return float(v)
    t = 0.0
    for p in str(v).strip().split(":"):
        t = t * 60 + float(p)
    return t


def race_session(info, race, current):
    want = race.get("session", "race")
    if isinstance(want, int):
        return want
    hits = [s["num"] for s in info["sessions"] if str(want).lower() in (s["type"] + " " + s["name"]).lower()]
    return hits[-1] if hits else current

SCAN_VERSION = 6

# thresholds (race seconds unless noted)
GAP_S = 1.0          # two cars this close in time are "together"
GAP_KEEP = 2.0       # ...and the fight is still on while they stay within this
BATTLE_MIN = 5.0     # together for at least this long to count as a battle
BATTLE_BREAK = 5.0   # a gap in closeness shorter than this does not end the battle
PASS_HOLD = 2.0      # a position swap must stick this long (side-by-side jitter is dropped)
OFF_MIN = 0.5        # shortest off-track excursion kept
STOP_MIN = 2.0       # shortest standstill on track kept
STOP_MPS = 3.0       # slower than this is "stopped"
SAME_CAR_MERGE = 5.0     # off + stop of one car this close together are one event
GROUP_T, GROUP_PCT = 3.0, 0.03   # events of different cars this close in time and track position are one incident
PIT_MIN = 3.0
GONE_MIN = 2.0
OFF_TRACK, ON_TRACK = 0, 3
PARADE, RACING, CHECKERED = 3, 4, 5
ROLLING_MPS = 10.0   # most of the field faster than this at the green means a rolling start

T, STATE, PCT, SURF, PIT, POS, LAPC, FLAGS = range(8)
# iRacing's per-car flag bits
FLAG_BLACK, FLAG_DQ, FLAG_REPAIR = 0x010000, 0x020000, 0x100000
FLAG_NAMES = ((FLAG_REPAIR, "mechanical black flag"), (FLAG_DQ, "disqualified"), (FLAG_BLACK, "black flag"))


class ScanError(Exception):
    pass


# ---------------------------------------------------------------- collect

def collect(sim, clock, info, race=None, speed=16, end=None, say=lambda text: None, progress=None, stop=None):
    """Play the race session from its start at `speed` and return the raw samples."""
    race = race or {}
    progress = progress or (lambda t: None)
    stop = stop or (lambda: False)
    here = sim.read()
    num = race_session(info, race, here["session_num"])
    idxs = sorted(i for i, d in info["drivers"].items() if not d["pace_car"] and not d["spectator"])
    if not idxs:
        raise ScanError("iRacing lists no cars for this replay")
    notes, samples = [], []

    def wait(cond, timeout, step=0.1):
        limit = clock.now() + timeout
        while clock.now() < limit:
            if cond():
                return True
            clock.sleep(step)
        return cond()

    was = (here["session_num"], here["session_time"])

    def settled():
        """The rewind has finished: the replay sits still in the race session, somewhere other than where it was.
        An unchanged replay is accepted only when already at session start."""
        a = sim.read()
        if a["session_num"] != num or a["session_time"] is None:
            return False
        clock.sleep(0.5)
        b = sim.read()
        if b["session_num"] != num or b["session_time"] != a["session_time"]:
            return False
        moved = (b["session_num"], b["session_time"]) != was
        return moved or (was[0] == num and was[1] is not None and was[1] <= 2.0)

    sim.set_speed(0)
    clock.sleep(0.3)
    was = (lambda r: (r["session_num"], r["session_time"]))(sim.read())      # where it sits once paused
    for attempt in (1, 2):
        sim.seek(num, 0.0)
        if wait(settled, 30):
            break
    else:
        raise ScanError("the replay did not move to the start of the race session")
    live = (None, None)
    try:
        sim.set_speed(speed)
        last_t, last_move = None, clock.now()
        began = clock.now()
        while True:
            s = sim.read_cars()
            now, t = clock.now(), s["session_time"]
            if s["session_num"] != num:
                notes.append("the replay left the race session")
                break
            if t is not None and (last_t is None or t > last_t + 1e-6):
                samples.append(_pack(t, s, idxs))
                live = (live[0] if live[0] is not None else s.get("live_time"), s.get("live_time"))
                last_t, last_move = t, now
                progress(t)
            elif t is not None and last_t is not None and t < last_t - 1.0 and now - began < 30:
                samples.clear()            # a late rewind landing: start the readings again from here
                samples.append(_pack(t, s, idxs))
                last_t, last_move = t, now
            elif t is not None and last_t is not None and t < last_t - 1.0:
                notes.append(f"the replay jumped backwards at {mmss(last_t)} (was it touched?): scan stopped there")
                break
            elif now - last_move > 6.0:
                break                      # the replay reached its end (or never started moving)
            if end is not None and t is not None and t >= end:
                break
            if stop():
                notes.append("stopped by you: the scan covers only part of the race")
                break
            clock.sleep(1 / 60)
    finally:
        try:
            sim.set_speed(0)
            if here["session_num"] is not None and here["session_time"] is not None:
                sim.seek(here["session_num"], here["session_time"])     # put the replay back where it was
        except Exception:
            pass
    if len(samples) < 10:
        raise ScanError("the replay did not play (no samples were read). Is the replay loaded and iRacing in front?")
    return {"scan_version": SCAN_VERSION, "info": _slim(info), "idxs": idxs, "session_num": num, "speed": speed,
            "live_time": list(live), "notes": notes, "samples": samples}


def _pack(t, s, idxs):
    def col(key, conv):
        a = s.get(key)
        if a is None:
            return None
        return [conv(a[i]) if i < len(a) and a[i] is not None else None for i in idxs]
    return [round(t, 4), s.get("session_state"), col("pct", lambda v: round(float(v), 5)), col("surface", int),
            col("pit", lambda v: 1 if v else 0), col("pos", int), col("lapc", int), col("car_flags", int)]


def _slim(info):
    return {"subsession": info.get("subsession"), "track": info.get("track"), "track_length_m": info.get("track_length_m"),
            "sessions": info.get("sessions"),
            "drivers": {str(i): {"number": d["number"], "name": d["name"], "team": d.get("team", ""),
                                 "pace_car": d["pace_car"], "spectator": d["spectator"]} for i, d in info["drivers"].items()}}


# ---------------------------------------------------------------- analyse

def analyse(raw, race=None):
    race = race or {}
    info, idxs, S = raw["info"], raw["idxs"], raw["samples"]
    drivers = {int(k): v for k, v in info["drivers"].items()}
    n, K = len(idxs), len(S)
    times = [s[T] for s in S]
    notes = list(raw.get("notes") or [])
    cars = range(n)

    def label(c):
        d = drivers[idxs[c]]
        return {"number": d["number"], "name": d["name"]}

    def tag(c):
        d = drivers[idxs[c]]
        return f"#{d['number']} {d['name']}"

    # ---- what the replay let us read
    def readable(col):
        vals = [tuple(s[col]) for s in S if s[col] is not None]
        if not vals:
            return "no"
        return "yes" if len(set(vals)) > 1 else "constant (never changes)"
    can = {"track position": readable(PCT), "on/off track": readable(SURF), "pit road": readable(PIT),
           "running order": readable(POS), "lap counter": readable(LAPC),
           "session state": "yes" if len({s[STATE] for s in S if s[STATE] is not None}) > 1 else
                            ("constant (never changes)" if any(s[STATE] is not None for s in S) else "no")}
    has_flags = any(len(s) > FLAGS and s[FLAGS] is not None for s in S)
    can["car flags"] = ("yes" if any(len(s) > FLAGS and s[FLAGS] and any(s[FLAGS]) for s in S) else "read, but none were shown") \
        if has_flags else "not in this scan"
    if can["track position"] != "yes":
        raise ScanError("iRacing did not give track positions for the cars during playback, so nothing can be worked out")
    has_surf, has_pit = can["on/off track"] != "no", can["pit road"] != "no"

    # Every reading is unpacked ONCE into per-car columns: PC[c][k] track position (None when unknown or the car is
    # not in the world), SU[c][k] surface, PI[c][k] on pit road. The analysis below reads these instead of re-walking
    # the nested samples millions of times. pct/surf/pit stay as the readable way to ask.
    PC = [[None] * K for _ in cars]
    SU = [[None] * K for _ in cars]
    PI = [[False] * K for _ in cars]
    for k, s in enumerate(S):
        a = s[PCT]
        sf = s[SURF] if has_surf else None
        pp = s[PIT] if has_pit else None
        for c in cars:
            if sf is not None:
                SU[c][k] = sf[c]
            if pp is not None:
                PI[c][k] = bool(pp[c])
            v = a[c] if a else None
            if v is None or v < -0.5:
                continue
            if sf is not None and sf[c] == -1:
                continue
            PC[c][k] = max(v, 0.0)

    def pct(c, k):
        return PC[c][k]

    def surf(c, k):
        return SU[c][k]

    def pit(c, k):
        return PI[c][k]

    # ---- green flag and chequered flag
    green = green_how = None
    states = [s[STATE] for s in S]
    for k in range(1, K):
        if states[k] == RACING and states[k - 1] is not None and states[k - 1] < RACING:
            green, green_how = times[k], "session state"
            break
    if green is None and states[0] == RACING:
        notes.append("the replay starts after the green flag, so the start itself is not in it")
    if green is None and states[0] != RACING:
        first = [pct(c, 0) for c in cars]
        for k in range(1, K):
            moved = sum(1 for c in cars if first[c] is not None and pct(c, k) is not None and abs(pct(c, k) - first[c]) > 0.001)
            if moved >= max(2, n // 2):
                green, green_how = times[k], "first movement (session state not readable)"
                break
    if green is None and race.get("green") is not None:
        green, green_how = parse_time(race["green"]), "typed in the window"
    chequered = next((times[k] for k in range(K) if states[k] is not None and states[k] >= CHECKERED), None)
    k_green = next((k for k in range(K) if green is not None and times[k] >= green), 0)
    # Rolling or standing start: is the field already moving when the green comes? The pace lap begins when the
    # session state turns to parade laps (3).
    start_type = pace_from = None
    if green is not None and k_green >= 2:
        k0, moving, seen = k_green - 2, 0, 0
        for c in cars:
            a, b = pct(c, k0), pct(c, k_green)
            if a is None or b is None:
                continue
            d = (b - a) % 1.0
            seen += 1
            if d < 0.5 and d * (info.get("track_length_m") or 4000.0) / max(times[k_green] - times[k0], 1e-6) > ROLLING_MPS:
                moving += 1
        if seen:
            start_type = "rolling" if moving * 2 > seen else "standing"
        if start_type == "rolling":
            pace_from = next((times[k] for k in range(1, k_green) if states[k] == PARADE and states[k - 1] != PARADE), None)
    if chequered is None and not any("stopped by you" in x for x in notes):
        notes.append("the chequered flag was not found: laps are counted to the end of the replay")

    # ---- speed (laps per second) from each car's own movement over the last second
    length = info.get("track_length_m")
    v_stop = STOP_MPS / length if length else 0.0008
    speed = [[None] * K for _ in cars]
    # The look-back sample j for each k (the latest reading at least a second earlier) is the same for every car,
    # so it is found once.
    back, j = [0] * K, 0
    for k in range(K):
        while j < k and times[k] - times[j + 1] >= 1.0:
            j += 1
        back[k] = j
    for c in cars:
        P, sp = PC[c], speed[c]
        for k in range(K):
            j = back[k]
            a, b = P[j], P[k]
            dt = times[k] - times[j]
            if a is None or b is None or dt < 0.5:
                continue
            d = b - a
            if d < -0.5:
                d += 1.0
            if -0.02 <= d <= 0.2:            # anything else is a teleport (tow / reset), not driving
                sp[k] = max(d, 0.0) / dt
    field_speed = []
    for k in range(K):
        vs = [speed[c][k] for c in cars if speed[c][k] is not None]
        field_speed.append(statistics.median(vs) if vs else None)

    # ---- line crossings and lap numbers
    cross = [[] for _ in cars]            # (time, sample index)
    # Only crossings after the green count. On a rolling start the cars gridded behind the line drive across it
    # at the start of the pace lap; counting that used to hand them an extra lap and put them a lap ahead of the
    # real leaders all race.
    k_first = k_green + 1 if green is not None else 1
    for c in cars:
        P = PC[c]
        for k in range(max(1, k_first), K):
            a, b = P[k - 1], P[k]
            if a is not None and b is not None and a > 0.75 and b < 0.25:
                tc = times[k - 1] + (1 - a) / ((1 - a) + b) * (times[k] - times[k - 1])
                cross[c].append((tc, k))
    # Lap numbers: iRacing's own lap counter when the replay gives it, otherwise worked out from where the car
    # sat at the green (behind the line: the first crossing only starts lap 1).
    use_sdk = can["lap counter"] == "yes"
    p_green = [next((pct(c, k) for k in range(k_green, K) if pct(c, k) is not None), None) for c in cars]
    numbers = []
    for c in cars:
        nums, prev = [], None
        for i, (tc, k) in enumerate(cross[c]):
            sdk = [S[j][LAPC][c] for j in range(k, min(k + 4, K)) if S[j][LAPC] is not None and S[j][LAPC][c] is not None and S[j][LAPC][c] >= 0] if use_sdk else []
            if sdk:
                nno = max(sdk)
            elif prev is None:
                nno = 0 if (p_green[c] is not None and p_green[c] > 0.5) else 1
            else:
                nno = prev + 1
            if prev is not None and nno <= prev:
                nno = prev + 1
            nums.append(nno)
            prev = nno
        numbers.append(nums)
    lap_at = [dict(zip(numbers[c], (tc for tc, _ in cross[c]))) for c in cars]   # lap completed -> time
    can["lap numbers"] = "from iRacing's lap counter" if use_sdk else "worked out from the grid (no lap counter)"

    # ---- live race progress (laps done + track position) for every car at every sample
    prog = [[None] * K for _ in cars]
    inworld = [[False] * K for _ in cars]
    for c in cars:
        done = numbers[c][0] - 1 if numbers[c] else (-1 if (p_green[c] is not None and p_green[c] > 0.5) else 0)
        ci, last = 0, None
        for k in range(K):
            while ci < len(cross[c]) and cross[c][ci][1] <= k:
                done = numbers[c][ci]
                ci += 1
            p = PC[c][k]
            if p is not None:
                last = done + p
                inworld[c][k] = True
            prog[c][k] = last
    # iRacing raises the chequered state a little before or just as the leader reaches the line. The race ends
    # at the LEADER'S next crossing; everyone else finishes at their own first crossing from that moment on.
    if chequered is not None:
        kc = next(k for k in range(K) if times[k] >= chequered)
        known = [c for c in cars if prog[c][kc] is not None and inworld[c][kc]]
        if known:
            lead = max(known, key=lambda c: prog[c][kc])
            step = times[kc] - times[max(kc - 2, 0)]
            took = [tc for tc, _ in cross[lead] if tc >= chequered - step - 1e-6]
            if took:
                chequered = round(took[0], 3)
    finish = [next((tc for tc, _ in cross[c] if chequered is not None and tc >= chequered - 1e-6), None) for c in cars]

    g0 = green or times[0]
    RAC = [[inworld[c][k] and times[k] >= g0 and (finish[c] is None or times[k] <= finish[c]) for k in range(K)] for c in cars]

    def racing(c, k):
        return RAC[c][k]

    _orders = {}

    def order(k, only_racing=False):
        """Cars by race progress at sample k, leader first. The full order is memoised (rank() asks for the same k
        many times); callers must not change the list."""
        if only_racing:
            return sorted((c for c in cars if prog[c][k] is not None and RAC[c][k]), key=lambda c: -prog[c][k])
        o = _orders.get(k)
        if o is None:
            o = _orders[k] = sorted((c for c in cars if prog[c][k] is not None), key=lambda c: -prog[c][k])
        return o

    def k_at(t):
        lo, hi = 0, K - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if times[mid] < t:
                lo = mid + 1
            else:
                hi = mid
        return lo

    _ranks = {}

    def rank(c, k):
        r_ = _ranks.get(k)
        if r_ is None:
            r_ = _ranks[k] = {c_: i + 1 for i, c_ in enumerate(order(k))}
        return r_.get(c)

    # ---- passes (a swap in live order that sticks)
    flips = {}
    for k in range(max(k_green, 1), K):
        live = [c for c in cars if RAC[c][k] and inworld[c][k - 1]]
        if len(live) < 2:
            continue
        # Only cars that were close enough on the previous reading can have swapped by this one. Two cars swap only
        # if the gap between them (before) is smaller than the difference in how far each moved, which is at most
        # D = (biggest move) - (smallest move). So with the cars sorted by progress before, each car is compared
        # only with the ones behind it within D, not with the whole field. A small margin keeps rounding on the
        # safe side; every candidate still goes through the exact check below, in the original pair order.
        pv = [prog[c][k - 1] for c in live]
        mv = [prog[c][k] - pv[i] for i, c in enumerate(live)]
        D = max(mv) - min(mv) + 1e-9
        by = sorted(range(len(live)), key=pv.__getitem__, reverse=True)
        cand = []
        for x, i in enumerate(by):
            pi = pv[i]
            for j in by[x + 1:]:
                if pi - pv[j] > D:
                    break
                cand.append((i, j) if i < j else (j, i))
        cand.sort()
        for i, j in cand:
            a, b = live[i], live[j]
            d0, d1 = prog[a][k - 1] - prog[b][k - 1], prog[a][k] - prog[b][k]
            if d0 > 0 > d1:
                flips.setdefault((a, b) if a < b else (b, a), []).append((k, b, a))   # b passed a
            elif d0 < 0 < d1:
                flips.setdefault((a, b) if a < b else (b, a), []).append((k, a, b))
    passes = []
    for pair, fl in flips.items():
        kept = []
        for f in fl:
            if kept and times[f[0]] - times[kept[-1][0]] < PASS_HOLD:
                kept.pop()                # swapped back straight away: not a pass
            else:
                kept.append(f)
        for k, winner, loser in kept:
            if pit(loser, k) or pit(winner, k):
                how = "in the pits"
            elif surf(loser, k) == OFF_TRACK or (speed[loser][k] is not None and field_speed[k] and speed[loser][k] < 0.5 * field_speed[k]):
                how = "other car off or slow"
            else:
                how = "on track"
            passes.append({"time": round(times[k], 2), "passer": label(winner), "passed": label(loser),
                           "for_position": rank(winner, k), "how": how, "_k": k, "_pair": pair})
    passes.sort(key=lambda p: p["time"])

    # ---- battles (neighbours in the race order running within GAP_S)
    spans = {}
    for k in range(k_green, K):
        o = order(k, only_racing=True)
        for a, b in zip(o, o[1:]):
            va, vb, fs = speed[a][k], speed[b][k], field_speed[k]
            if va is None or vb is None or not fs or min(va, vb) < 0.5 * fs or PI[a][k] or PI[b][k]:
                continue                  # a car crawling back after a spin is not in a battle
            if SU[a][k] not in (None, ON_TRACK) or SU[b][k] not in (None, ON_TRACK):
                continue
            gap = (prog[a][k] - prog[b][k]) / vb
            key = (a, b) if a < b else (b, a)
            lst = spans.setdefault(key, [])
            live = bool(lst) and times[k] - lst[-1]["to"] <= BATTLE_BREAK
            if gap >= (GAP_KEEP if live else GAP_S):
                continue
            if live:
                lst[-1]["to"] = times[k]
                if gap < lst[-1]["gap"]:
                    lst[-1]["gap"], lst[-1]["at"] = gap, times[k]
            else:
                lst.append({"from": times[k], "to": times[k], "gap": gap, "at": times[k], "k": k, "ahead": a, "behind": b})
    battles = []
    track_passes = {}                     # pair -> its on-track passes, in time order
    for p in passes:
        if p["how"] == "on track":
            track_passes.setdefault(p["_pair"], []).append(p)
    for key, lst in spans.items():
        for sp in lst:
            if sp["to"] - sp["from"] < BATTLE_MIN:
                continue
            inside = [p for p in track_passes.get(key, ()) if sp["from"] - 1 <= p["time"] <= sp["to"] + 1]
            battles.append({"from": round(sp["from"], 2), "to": round(sp["to"], 2), "seconds": round(sp["to"] - sp["from"], 1),
                            "for_position": rank(sp["ahead"], sp["k"]), "ahead": label(sp["ahead"]), "behind": label(sp["behind"]),
                            "closest_gap_s": round(sp["gap"], 2), "closest_at": round(sp["at"], 2), "passes": [p["time"] for p in inside]})
    battles.sort(key=lambda b: b["from"])

    # ---- incidents: off track, or stopped on track
    def runs(c, mask):
        """(first, last) sample of each stretch from the green on where the car is racing and mask[k] is true."""
        out, start, last = [], None, None
        R = RAC[c]
        for k in range(k_green, K):
            if R[k] and mask[k]:
                if start is None:
                    start = k
                last = k
            elif start is not None:
                out.append((start, last))
                start = None
        if start is not None:
            out.append((start, last))
        return out

    events = []
    for c in cars:
        raw_ev = []
        sp, su, pi = speed[c], SU[c], PI[c]
        if has_surf:
            off = [x == OFF_TRACK for x in su]
            raw_ev += [(a, b, "off track") for a, b in runs(c, off) if times[b] - times[a] >= OFF_MIN]
        t_ok = g0 + 3
        still = [sp[k] is not None and sp[k] < v_stop and not pi[k] and su[k] in (None, ON_TRACK) and times[k] > t_ok
                 for k in range(K)]
        raw_ev += [(a, b, "stopped on track") for a, b in runs(c, still) if times[b] - times[a] >= STOP_MIN]
        raw_ev.sort()
        for a, b, kind in raw_ev:
            if events and events[-1]["c"] == c and times[a] - events[-1]["to"] <= SAME_CAR_MERGE:
                e = events[-1]
                e["to"], e["kb"] = max(e["to"], times[b]), max(e["kb"], b)
            else:
                e = {"c": c, "from": times[a], "to": times[b], "ka": a, "kb": b, "kinds": {}, "pct": pct(c, a)}
                events.append(e)
            e["kinds"][kind] = round(e["kinds"].get(kind, 0) + times[b] - times[a], 1)

    # cars that leave the world (tow, disconnect, retire)
    out_at, resets = {}, []
    for c in cars:
        seen, k = False, k_green
        while k < K:
            if inworld[c][k]:
                seen = True
                k += 1
                continue
            j = k
            while j < K and not inworld[c][j]:
                j += 1
            if seen and (finish[c] is None or times[k] < finish[c]):
                if j == K:
                    out_at[c] = times[k]
                elif times[j] - times[k] >= GONE_MIN:
                    resets.append({"car": label(c), "from": round(times[k], 2), "back_at": round(times[j], 2),
                                   "back_in_pits": pit(c, j) or surf(c, j) in (1, 2)})
            k = j

    # When each car STOPPED RACING: the last moment it was moving on track, for cars that never took the flag.
    # (A car can sit in its pit stall, still connected, long after its race is over.)
    race_end = chequered if chequered is not None else times[-1]
    stopped_at = {}
    for c in cars:
        if finish[c] is not None or not any(inworld[c][k_green:]):
            continue
        last = next((k for k in range(K - 1, k_green - 1, -1) if inworld[c][k] and not pit(c, k) and surf(c, k) in (None, ON_TRACK)
                     and speed[c][k] is not None and speed[c][k] > v_stop), None)
        if last is not None and times[last] < race_end - 30:
            nxt = min(last + 1, K - 1)
            stopped_at[c] = {"time": round(times[nxt], 2),
                             "where": "in the pits" if (pit(c, nxt) or surf(c, nxt) in (1, 2)) else
                                      ("left the session" if not inworld[c][nxt] else "on track")}

    # Flags shown to each car (mechanical black flag = required repairs, black flag = penalty to serve)
    flags = []
    if has_flags:
        fcol = [s[FLAGS] if len(s) > FLAGS else None for s in S]
        for c in cars:
            FL = [a[c] if a is not None else None for a in fcol]
            if not any(FL[k_green:]):
                continue                  # never shown a flag: nothing to find
            for bit, name in FLAG_NAMES:
                start = last = None
                for k in range(k_green, K):
                    on = FL[k] is not None and bool(FL[k] & bit)
                    if on:
                        start = k if start is None else start
                        last = k
                    if start is not None and (not on or k == K - 1) and times[k] - times[last] > 2.0 or (start is not None and k == K - 1):
                        flags.append({"car": label(c), "flag": name, "from": round(times[start], 2), "to": round(times[last], 2), "_c": c})
                        start = None
        flags.sort(key=lambda f: f["from"])

    # How each non-finisher's race ended.
    #   mechanical black flag : the car was shown it and never raced again
    #   retired to pits       : stopped racing and went to (or was towed to) the pits
    #   stopped on track      : stopped racing out on the circuit and never reached the pits
    # A car that leaves the session while the race is still on is logged as disconnected as well.
    ended = {}
    for c, st in stopped_at.items():
        t = st["time"]
        k0 = k_at(t)
        to_pits = any(inworld[c][k] and (pit(c, k) or surf(c, k) in (1, 2)) for k in range(k0, K))
        meatball = next((f for f in flags if f["_c"] == c and f["flag"] == "mechanical black flag" and f["to"] >= t - 300), None)
        how = "mechanical black flag" if meatball else ("retired to pits" if to_pits else "stopped on track")
        gone = out_at.get(c)
        ended[c] = {"how": how, "time": st["time"], "reached_pits": to_pits,
                    "disconnected_at": round(gone, 2) if gone is not None and gone < race_end else None,
                    "left_car_after_race_at": round(gone, 2) if gone is not None and gone >= race_end else None}
    for f in flags:
        f.pop("_c")

    def circ(a, b):
        d = abs((a or 0) - (b or 0)) % 1.0
        return min(d, 1 - d)
    events.sort(key=lambda e: e["from"])
    groups = []
    for e in events:
        g = next((g for g in groups if any(e["from"] <= x["to"] + GROUP_T and x["from"] <= e["to"] + GROUP_T
                                           and circ(e["pct"], x["pct"]) <= GROUP_PCT for x in g)), None)
        (g.append(e) if g else groups.append([e]))
    last_event = {}
    for e in events:
        last_event[e["c"]] = max(last_event.get(e["c"], -1e9), e["from"])
    incidents = []
    for g in groups:
        t0, t1 = min(e["from"] for e in g), max(e["to"] for e in g)
        kb, ka = k_at(t0 - 1.0), k_at(min(t1 + 10.0, times[-1]))
        members = []
        for e in g:
            c = e["c"]
            before, after = rank(c, kb), rank(c, ka)
            gone = c in stopped_at and stopped_at[c]["time"] <= t1 + 90 and e["from"] == last_event[c]
            members.append({**label(c), "what": e["kinds"], "from": round(e["from"], 2), "to": round(e["to"], 2),
                            "position_before": before, "position_after": after,
                            "places_lost": (after - before) if before and after else None, "out_of_race": gone})
        stopped = any("stopped on track" in m["what"] for m in members)
        lost = sum(max(m["places_lost"] or 0, 0) for m in members)
        off_s = max((m["what"].get("off track", 0) for m in members), default=0)
        if len(members) >= 2 or stopped or lost >= 2 or any(m["out_of_race"] for m in members):
            level = "major"
        elif off_s >= 2.0 or lost == 1:
            level = "minor"
        else:
            level = "trivial"                # wheels off for a moment, nothing lost
        incidents.append({"from": round(t0, 2), "to": round(t1, 2), "lap": int(prog[g[0]["c"]][g[0]["ka"]] or 0) + 1,
                          "track_pct": round(g[0]["pct"] or 0, 3), "level": level, "cars": members,
                          "stopped": stopped, "places_lost": lost})

    # ---- pit stops
    pits = []
    if has_pit:
        for c in cars:
            merged = []
            for a, b in runs(c, PI[c]):
                if merged and times[a] - times[merged[-1][1]] <= 2.0:
                    merged[-1][1] = b
                else:
                    merged.append([a, b])
            for a, b in merged:
                if times[b] - times[a] >= PIT_MIN and a > k_green + 1:
                    still = sum(times[k] - times[k - 1] for k in range(a + 1, b + 1) if speed[c][k] is not None and speed[c][k] < v_stop)
                    pits.append({"car": label(c), "in": round(times[a], 2), "out": round(times[b], 2),
                                 "lap": int(prog[c][a] or 0) + 1, "seconds": round(times[b] - times[a], 1), "stationary_s": round(still, 1)})
        pits.sort(key=lambda p: p["in"])

    # ---- laps, running order per lap, lead changes at the line
    max_lap = max((max(d) for d in lap_at if d), default=0)
    order_by_lap, leaders = {}, {}
    for N in range(1, max_lap + 1):
        done = sorted((lap_at[c][N], c) for c in cars if N in lap_at[c] and (finish[c] is None or lap_at[c][N] <= finish[c] + 1e-6))
        if done:
            order_by_lap[N] = [drivers[idxs[c]]["number"] for _, c in done]
            leaders[N] = done[0]
    race_laps = max((N for N, (tc, c) in leaders.items() if chequered is None or tc <= (finish[c] or tc) + 1e-6), default=0)
    if chequered is not None:
        fin = [(N, tc) for N, (tc, c) in leaders.items() if tc >= chequered]
        if fin:
            race_laps = fin[0][0]
    lead_changes, led, prev = [], {}, None
    for N in sorted(leaders):
        if N > race_laps:
            break
        tc, c = leaders[N]
        led[c] = led.get(c, 0) + 1
        if prev is not None and c != prev:
            lead_changes.append({"lap": N, "time_at_line": round(tc, 2), "from": label(prev), "to": label(c)})
        prev = c
    lead_passes = [{k: v for k, v in p.items() if not k.startswith("_")} for p in passes if p["for_position"] == 1 and p["how"] == "on track"]

    def on_grid(c):
        return inworld[c][k_green] and not pit(c, k_green) and surf(c, k_green) in (None, ON_TRACK)
    grid = [c for c in order(k_green) if on_grid(c)] if green is not None else []
    # Grids are staggered two wide, so track position alone can swap neighbours: use iRacing's own order at the green.
    at_green = S[k_green][POS] if can["running order"] == "yes" and green is not None else None
    if at_green and all(at_green[c] and at_green[c] > 0 for c in grid):
        grid.sort(key=lambda c: at_green[c])
    pit_start = [c for c in cars if green is not None and inworld[c][k_green] and not on_grid(c)]

    def laps_done(c):
        return max((N for N in lap_at[c] if finish[c] is None or lap_at[c][N] <= finish[c] + 1e-6), default=0)
    final = sorted(cars, key=lambda c: (-laps_done(c), finish[c] if finish[c] is not None else float("inf"), -(prog[c][K - 1] or -9)))
    can["finishing order"] = "worked out from line crossings"
    if can["running order"] == "yes":
        last = next((S[k][POS] for k in range(K - 1, -1, -1) if S[k][POS] is not None and any(p and p > 0 for p in S[k][POS])), None)
        if last:
            placed = [c for _, c in sorted((last[c], c) for c in cars if last[c] and last[c] > 0)]
            final = placed + [c for c in final if c not in placed]
            can["finishing order"] = "from iRacing's running order at the end of the replay"

    car_rows = []
    for c in cars:
        laps = {N: round(lap_at[c][N] - lap_at[c][N - 1], 3) for N in sorted(lap_at[c]) if N - 1 in lap_at[c] and N >= 1
                and (finish[c] is None or lap_at[c][N] <= finish[c] + 1e-6)}
        best = min(laps.items(), key=lambda kv: kv[1]) if laps else None
        car_rows.append({**label(c), "car_idx": idxs[c], "grid": grid.index(c) + 1 if c in grid else None,
                         "pit_lane_start": c in pit_start, "never_on_track": not any(inworld[c]),
                         "finish": final.index(c) + 1, "laps_completed": laps_done(c),
                         "laps_led": led.get(c, 0), "best_lap": {"lap": best[0], "time": best[1]} if best else None,
                         "finish_time": round(finish[c], 2) if finish[c] is not None else None,
                         "stopped_racing": stopped_at.get(c), "ended": ended.get(c),
                         "out_at": round(out_at[c], 2) if c in out_at else None,
                         "line_crossings": {str(N): round(t, 3) for N, t in sorted(lap_at[c].items())},
                         "lap_times": {str(N): v for N, v in laps.items()}})
    car_rows.sort(key=lambda r: r["finish"])
    steps = [b - a for a, b in zip(times, times[1:])]
    for p in passes:
        p.pop("_k"); p.pop("_pair")
    return {"racecap_scan": SCAN_VERSION, "subsession": info.get("subsession"), "track": info.get("track"),
            "track_length_m": length, "session_num": raw.get("session_num"),
            "scanned": {"from": times[0], "to": times[-1], "speed": raw.get("speed"), "samples": K,
                        "step_s": round(statistics.median(steps), 3) if steps else None},
            "readable": can, "green": green, "green_from": green_how, "start_type": start_type, "pace_lap_from": pace_from, "chequered": chequered, "laps": race_laps,
            "cars": car_rows, "order_by_lap": {str(N): v for N, v in order_by_lap.items() if N <= race_laps},
            "lead_changes_at_line": lead_changes, "passes_for_lead": lead_passes, "passes": passes, "battles": battles,
            "incidents": incidents, "pit_stops": pits, "flags": flags, "resets": resets, "notes": notes}


# ---------------------------------------------------------------- output

def describe(r):
    """Plain-text summary a person can read top to bottom."""
    def who(d):
        return f"#{d['number']} {d['name']}"
    sc = r["scanned"]
    L = [f"RaceScan: {r['track']}, subsession {r['subsession']}",
         f"Scanned race time {mmss(sc['from'])} to {mmss(sc['to'])} at {sc['speed']}x: {sc['samples']} readings, one every {sc['step_s']} s of race time.",
         "", "WHAT THE REPLAY LET ME READ"]
    L += [f"  {k}: {v}" for k, v in r["readable"].items()]
    L += ["", "FLAGS",
          f"  Green flag: {mmss(r['green']) if r['green'] is not None else 'not found'}" + (f"  (from {r['green_from']})" if r["green_from"] else ""),
          "  Start: " + (r.get("start_type") or "not known") + (f" (pace lap from {mmss(r['pace_lap_from'])})" if r.get("pace_lap_from") is not None else ""),
          f"  Chequered flag: {mmss(r['chequered']) if r['chequered'] is not None else 'not found'}",
          f"  Race laps: {r['laps']}",
          "", "CARS (finish order as scanned; the results page is the official one)"]
    for c in r["cars"]:
        best = f"best {c['best_lap']['time']:.3f} on L{c['best_lap']['lap']}" if c["best_lap"] else "no timed lap"
        st = c.get("stopped_racing")
        extra, en = "", c.get("ended")
        if en:
            extra = f", {en['how'].upper()} at {mmss(en['time'])}"
            if en["disconnected_at"] is not None:
                extra += f", DISCONNECTED at {mmss(en['disconnected_at'])}"
        grid = "pit" if c["pit_lane_start"] else (c["grid"] or "-")
        if c["never_on_track"]:
            best, extra = "never on track", ""
        L.append(f"  P{c['finish']:<2} {who(c):<28} grid {grid:<3} laps {c['laps_completed']:<3} led {c['laps_led']:<3} {best}{extra}")
    L += ["", f"LEAD CHANGES AT THE LINE ({len(r['lead_changes_at_line'])})"]
    L += [f"  Lap {x['lap']:<3} {mmss(x['time_at_line']):>8}  {who(x['from'])} -> {who(x['to'])}" for x in r["lead_changes_at_line"]] or ["  none"]
    L += ["", f"PASSES FOR THE LEAD, the moment it happened ({len(r['passes_for_lead'])})"]
    L += [f"  {mmss(p['time']):>8}  {who(p['passer'])} passes {who(p['passed'])} ({p['how']})" for p in r["passes_for_lead"]] or ["  none"]
    shown = [x for x in r["incidents"] if x["level"] != "trivial"]
    L += ["", f"INCIDENTS ({len(shown)} worth a look, plus {len(r['incidents']) - len(shown)} moments of wheels off that cost nothing)"]
    for x in shown:
        names = " & ".join(who(m) for m in x["cars"])
        what = sorted({k for m in x["cars"] for k in m["what"]})
        lost = f", {plural(x['places_lost'], 'place')} lost" if x["places_lost"] else ""
        gone = ", OUT OF RACE: " + " & ".join(who(m) for m in x["cars"] if m["out_of_race"]) if any(m["out_of_race"] for m in x["cars"]) else ""
        L.append(f"  {mmss(x['from']):>8}-{mmss(x['to']):<8} lap {x['lap']:<3} {x['level'].upper():<6} {names}: {', '.join(what)}{lost}{gone}")
    if not shown:
        L.append("  none")
    L += ["", f"BATTLES, within {GAP_S:g} s and staying within {GAP_KEEP:g} s, {BATTLE_MIN:g} s or longer ({len(r['battles'])})"]
    for b in r["battles"]:
        ps = f", pass at {', '.join(mmss(t) for t in b['passes'])}" if b["passes"] else ""
        L.append(f"  {mmss(b['from']):>8}-{mmss(b['to']):<8} for P{b['for_position']}: {who(b['ahead'])} ahead of {who(b['behind'])}, closest {b['closest_gap_s']:.2f} s{ps}")
    if not r["battles"]:
        L.append("  none")
    L += ["", f"PIT STOPS ({len(r['pit_stops'])})"]
    L += [f"  {mmss(p['in']):>8}-{mmss(p['out']):<8} lap {p['lap']:<3} {who(p['car'])}, {p['seconds']:.0f} s on pit road" for p in r["pit_stops"]] or ["  none"]
    fl = r.get("flags") or []
    if fl:
        L += ["", f"FLAGS SHOWN TO CARS ({len(fl)})"] + [f"  {mmss(x['from']):>8}-{mmss(x['to']):<8} {who(x['car'])}: {x['flag']}" for x in fl]
    if r["resets"]:
        L += ["", "TOWS / RESETS"] + [f"  {mmss(x['from']):>8} {who(x['car'])} gone, back at {mmss(x['back_at'])}" + (" in the pits" if x["back_in_pits"] else "") for x in r["resets"]]
    other = len(r["passes"]) - len(r["passes_for_lead"])
    L += ["", f"Other passes found: {other} (all are in the .scan.json file)."]
    if r["readable"].get("car flags") == "not in this scan":
        L += ["", "Car flags were not read in this scan, so a mechanical black flag cannot be told from a plain retirement here.",
              "Scan the race again with RaceCap 0.3.2 or later to get them."]
    if r["notes"]:
        L += ["", "NOTES"] + ["  ! " + x for x in r["notes"]]
    return "\n".join(L) + "\n"


def brief(r):
    """A few lines for the window."""
    g = mmss(r["green"]) if r["green"] is not None else "not found"
    bad = [k for k in ("track position", "on/off track", "pit road", "running order", "lap counter", "session state")
           if r["readable"].get(k) != "yes"]
    lines = [f"Scan done: green {g}, {r['laps']} laps, {len(r['passes_for_lead'])} pass(es) for the lead, "
             f"{plural(sum(1 for x in r['incidents'] if x['level'] != 'trivial'), 'incident')}, {plural(len(r['battles']), 'battle')}, {len(r['pit_stops'])} pit {noun(len(r['pit_stops']), 'stop')}."]
    if bad:
        lines.append("  CHECK: could not read " + ", ".join(bad) + ".")
    lines += ["  CHECK: " + x for x in r["notes"]]
    return "\n".join(lines)


def slug(text):
    return re.sub(r"[^A-Za-z0-9]+", "-", str(text or "race")).strip("-").lower() or "race"


# ---------------------------------------------------------------- iRacing (only on the PC that runs it; imported lazily)
class Clock:
    def now(self):
        return time.monotonic()

    def sleep(self, s):
        time.sleep(s)


def _track_length_m(text):
    try:
        num, unit = str(text).split()[:2]
        return float(num) * (1609.344 if unit.lower().startswith("mi") else 1000.0)
    except (ValueError, TypeError):
        return None


class RealSim:
    def __init__(self):
        import irsdk
        self.ir = irsdk.IRSDK()

    def connect(self):
        try:
            if self.ir.is_initialized and self.ir.is_connected:
                return True
            self.ir.shutdown()
            return bool(self.ir.startup()) and bool(self.ir.is_connected)
        except Exception:  # noqa: BLE001
            return False

    def close(self):
        try:
            self.ir.shutdown()
        except Exception:  # noqa: BLE001
            pass

    def _get(self, key):
        try:
            return self.ir[key]
        except Exception:  # noqa: BLE001
            return None

    def info(self):
        wi = self._get("WeekendInfo") or {}
        drivers = {}
        for d in (self._get("DriverInfo") or {}).get("Drivers", []) or []:
            drivers[int(d["CarIdx"])] = {"number": str(d.get("CarNumber", "")).strip('"'), "name": d.get("UserName", ""),
                                         "team": d.get("TeamName", ""), "pace_car": bool(d.get("CarIsPaceCar", 0)),
                                         "spectator": bool(d.get("IsSpectator", 0))}
        sessions = [{"num": int(s["SessionNum"]), "type": s.get("SessionType", ""), "name": s.get("SessionName", "")}
                    for s in (self._get("SessionInfo") or {}).get("Sessions", []) or []]
        return {"subsession": wi.get("SubSessionID"), "track": wi.get("TrackDisplayName", ""),
                "track_length_m": _track_length_m(wi.get("TrackLength")), "drivers": drivers, "sessions": sessions}

    def read(self):
        self.ir.freeze_var_buffer_latest()
        try:
            return {"session_num": self._get("ReplaySessionNum"), "session_time": self._get("ReplaySessionTime")}
        finally:
            self.ir.unfreeze_var_buffer_latest()

    def read_cars(self):
        self.ir.freeze_var_buffer_latest()
        try:
            g = self._get

            def arr(key):
                v = g(key)
                return list(v) if v is not None else None
            return {"session_num": g("ReplaySessionNum"), "session_time": g("ReplaySessionTime"), "session_state": g("SessionState"),
                    "live_time": g("SessionTime"), "pct": arr("CarIdxLapDistPct"), "surface": arr("CarIdxTrackSurface"),
                    "pit": arr("CarIdxOnPitRoad"), "pos": arr("CarIdxPosition"), "lapc": arr("CarIdxLapCompleted"),
                    "car_flags": arr("CarIdxSessionFlags")}
        finally:
            self.ir.unfreeze_var_buffer_latest()

    def seek(self, session_num, t):
        self.ir.replay_search_session_time(int(session_num), int(round(t * 1000)))

    def set_speed(self, speed):
        self.ir.replay_set_play_speed(int(speed), False)


# ---------------------------------------------------------------- saved in Debrief's library, one folder per race
SCANS_DIR = "replay-scans"
OPEN_REPLAY = ("Open iRacing and load this race's replay (Results > Watch replay), then press Scan this race again. "
               "Debrief has to run on the same PC as iRacing.")


def scans_root(library_folder) -> Path:
    return Path(library_folder) / SCANS_DIR / "Races"


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def save(library_folder, raw, result, event_id=None) -> Path:
    """The race's folder, with the same files RaceCap writes, plus which Garage 61 event it belongs to."""
    d = scans_root(library_folder) / f"{result.get('subsession') or 'unknown'}_{slug(result.get('track'))}"
    (d / "scan").mkdir(parents=True, exist_ok=True)
    base = d / "scan" / f"{result.get('subsession') or 'unknown'}_{slug(result.get('track'))}"
    _write_atomic(base.with_name(base.name + ".samples.json.gz"), gzip.compress(json.dumps(raw, separators=(",", ":")).encode("utf-8"), 6))
    _write_atomic(base.with_name(base.name + ".scan.txt"), describe(result).encode("utf-8"))
    _write_atomic(base.with_name(base.name + ".scan.json"), json.dumps(result, indent=1).encode("utf-8"))
    if event_id:
        _write_atomic(d / "link.json", json.dumps({"event_id": str(event_id)}).encode("utf-8"))
    return d


def find_for_event(library_folder, event_id) -> Path | None:
    """The saved scan folder of this Garage 61 event, with no requests."""
    if not event_id:
        return None
    root = scans_root(library_folder)
    try:
        folders = list(root.iterdir())
    except OSError:
        return None
    for d in folders:
        try:
            if json.loads((d / "link.json").read_text(encoding="utf-8")).get("event_id") == str(event_id):
                return d
        except (OSError, ValueError, AttributeError):
            continue
    return None


def for_review(library_folder, review_dir, api=None, progress=None, should_stop=None, sim=None, clock=None, speed=16) -> dict:
    """Scan the loaded replay for one saved review, after checking it is that race. Raises SystemExit with plain words."""
    from . import library, racecap_link, results
    say = progress or (lambda frac, msg: None)
    d = Path(review_dir)
    data = json.loads((d / "review.json").read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit("That review's data file is damaged.")
    meta = data.get("meta") or {}
    if library.session_kind(meta.get("session")) != "race":
        raise SystemExit("A replay scan is for race reviews. Open the race's review and scan from there.")
    say(0.02, "Looking for iRacing...")
    if sim is None:
        try:
            sim = RealSim()
        except Exception:  # noqa: BLE001  (no iRacing library, or not on Windows)
            raise SystemExit(OPEN_REPLAY) from None
    clock = clock or Clock()
    if not sim.connect():
        raise SystemExit(OPEN_REPLAY)
    try:
        info = sim.info()
        sub = info.get("subsession")
        if not sub or not info.get("drivers") or not info.get("sessions"):
            raise SystemExit(OPEN_REPLAY)
        if not any("race" in (str(s.get("type")) + " " + str(s.get("name"))).lower() for s in info["sessions"]):
            raise SystemExit("The replay loaded in iRacing has no race session. Load this race's replay.")
        wrong = f"iRacing has race {sub} at {info.get('track')} loaded, which isn't this review's race. Load the right replay and try again."
        if not racecap_link.same_track(info.get("track"), meta.get("track")):
            raise SystemExit(wrong)
        cars = [c for c in info["drivers"].values() if not c["pace_car"] and not c["spectator"]]
        if racecap_link.find_me(cars, meta.get("driver")) is None:
            raise SystemExit(f"Couldn't find {meta.get('driver')} in the replay loaded in iRacing (race {sub}). Load this race's replay.")
        confirmed = None
        if api is not None:
            try:
                ev = api.event_for_subsession(sub)
                if ev is not None:
                    confirmed = str(ev) == str(meta.get("event_id"))
            except Exception:  # noqa: BLE001  (Garage 61 is never needed to scan)
                confirmed = None
        if confirmed is False:
            raise SystemExit(wrong)
        say(0.05, "Scanning the replay. Leave iRacing alone until it finishes; nothing is recorded.")
        laps = [l.get("t") for l in data.get("laps") or [] if isinstance(l, dict) and isinstance(l.get("t"), (int, float))]
        total = (sum(laps) + 180.0) if laps else None
        shown = [-99.0]

        def tick(t):
            if t - shown[0] >= 16:
                shown[0] = t
                say(0.05 + 0.85 * min(1.0, t / total) if total else None, f"Scanning replay: {mmss(t)} of race time")
        try:
            stop = should_stop or (lambda: False)
            raw = collect(sim, clock, info, {}, speed, None, progress=tick, stop=stop)
            if stop() or any("stopped by you" in str(n) for n in raw.get("notes") or []):
                raise SystemExit("Scan stopped. Nothing was saved.")
            result = analyse(raw, {})
        except ScanError as e:
            if should_stop and should_stop():
                raise SystemExit("Scan stopped. Nothing was saved.") from None
            raise SystemExit(f"The scan stopped: {e}.") from None
    finally:
        if hasattr(sim, "close"):
            sim.close()
    say(0.93, "Working out the race log...")
    folder = save(library_folder, raw, result, meta.get("event_id"))
    car = racecap_link.find_me(result["cars"], meta.get("driver"))
    if car is None:
        raise SystemExit("The scan finished but couldn't place you in the race.")
    found = {"dir": folder, "subsession": str(sub), "scan": result, "car": car, "official": None}
    results._rerender(d, lambda x: racecap_link.attach(x, found))
    library.rebuild(library_folder)
    return {"review": d.name, "subsession": str(sub), "confirmed": confirmed}
