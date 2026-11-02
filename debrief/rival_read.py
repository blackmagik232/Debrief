"""Read a rival's lap (2.9.1): play one clean lap of another car from the loaded replay, at normal speed, recording nothing.

Pattern as the replay scan: pick_lap() and friends are pure arithmetic on the saved scan; read_for_review() talks to iRacing
through replay_scan.RealSim and ties the result to one saved review. The trace is saved in the race's scan folder as
laps/<car>_clean.json, the format racecap_link.rival_laps reads, and added to the saved review's Compare with no Garage 61
request. It is only ever a rival: refs[].rival, no_pedals, never the main comparison, never coaching or time to find.

iRacing gives no pedals for other cars in a replay, so throttle and brake are never kept, and speed is only trusted while
the camera is on that car. Every driver is treated the same way.
"""
from __future__ import annotations

import json
import math
import re
import statistics
from pathlib import Path

from . import engine, racecap_link, replay_scan
from .replay_scan import OPEN_REPLAY

MODE = "clean"
LAPS_DIR = "laps"
LEAD_IN_S = 3.0          # start this long before the lap
TIMING_SLACK_S = 1.5     # the lap read must take about what the scan says it took
OFF_CAMERA_SHARE = 0.05  # more of the lap than this off the car and the read is thrown away
MAX_GAP = 0.05           # a hole in the readings wider than this share of the lap means the lap wasn't seen
MAX_POINTS = 6000        # thin very long laps so a trace stays well inside racecap_link.MAX_TRACE_BYTES
MAX_CHOICES = 10
NEAR_PLACES = 3          # cars that finished this close count as nearby, even without a fight
TOO_QUICK = 0.9          # a "lap" this far under the car's usual lap is a misread line crossing
INPUTS_FROM = "iRacing gives steering for every car; throttle and brake only for your own car"


# ---------------------------------------------------------------- which lap (pure)
def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def lap_windows(car) -> dict:
    """{lap: (start, end, seconds)} of a car's race laps from its line crossings (lap n runs crossing n-1 to n)."""
    cross = {}
    for k, v in (car.get("line_crossings") or {}).items():
        try:
            cross[int(k)] = float(v)
        except (TypeError, ValueError):
            continue
    counted = {str(k) for k in car.get("lap_times") or {}}     # laps after the flag aren't race laps
    return {n: (cross[n - 1], cross[n], cross[n] - cross[n - 1]) for n in sorted(cross)
            if n - 1 in cross and cross[n] > cross[n - 1] and str(n) in counted}


def dirty_laps(scan, car, wins) -> set:
    """Not clean: the opening lap, or a lap with an incident this car was in, or a pit stop."""
    me = str(car.get("number"))
    spans = []
    for i in scan.get("incidents") or []:
        if isinstance(i, dict) and any(isinstance(c, dict) and str(c.get("number")) == me for c in i.get("cars") or []):
            a, b = _num(i.get("from")), _num(i.get("to"))
            if a is not None:
                spans.append((a, b if b is not None else a))
    for p in scan.get("pit_stops") or []:
        if isinstance(p, dict) and str((p.get("car") or {}).get("number")) == me:
            a, b = _num(p.get("in")), _num(p.get("out"))
            if a is not None:
                spans.append((a - 5, (b if b is not None else math.inf) + 5))
    out = {1}
    for lap, (a, b, _s) in wins.items():
        if any(x < b and y > a for x, y in spans):
            out.add(lap)
    return out


def pick_lap(scan, car, median=None) -> dict | None:
    """{"lap", "t0", "t1", "time"} of the car's fastest clean lap, or None when it has none. Never falls back on a dirty lap."""
    wins = lap_windows(car)
    if not wins:
        return None
    usual = statistics.median(w[2] for w in wins.values())
    bad = dirty_laps(scan, car, wins)
    pool = {n: w for n, w in wins.items() if n not in bad and w[2] >= TOO_QUICK * usual
            and not (median and not 0.75 * median <= w[2] <= 1.25 * median)}     # as engine.is_full_ref: else a new review drops it
    if not pool:
        return None
    lap = min(pool, key=lambda n: pool[n][2])
    return {"lap": lap, "t0": pool[lap][0], "t1": pool[lap][1], "time": pool[lap][2]}


def _together(scan, me) -> dict:
    """{car number: seconds spent fighting this driver}."""
    out = {}
    for b in scan.get("battles") or []:
        if not isinstance(b, dict):
            continue
        a, c = str((b.get("ahead") or {}).get("number")), str((b.get("behind") or {}).get("number"))
        if me in (a, c):
            other = c if a == me else a
            out[other] = out.get(other, 0.0) + (_num(b.get("seconds")) or 0.0)
    return out


def candidates(scan, me_car, median=None, done=()) -> list:
    """Nearby drivers with a clean lap, the cars you fought most first, then the cars that finished closest."""
    me = str(me_car.get("number"))
    fought = _together(scan, me)
    mine = me_car.get("finish")
    cls = me_car.get("car_class")
    rows = []
    for c in scan.get("cars") or []:
        if not isinstance(c, dict) or str(c.get("number")) == me or c.get("never_on_track"):
            continue
        n = str(c.get("number"))
        gap = abs(c["finish"] - mine) if isinstance(c.get("finish"), int) and isinstance(mine, int) else None
        same_class = cls is None or c.get("car_class") in (None, cls)
        if n not in fought and not (gap is not None and gap <= NEAR_PLACES and same_class):
            continue
        lap = pick_lap(scan, c, median)
        # with no class data, a much faster car not raced is probably another class
        if lap is not None and cls is None and n not in fought and median and lap["time"] < 0.96 * median:
            continue
        if lap is None:
            continue
        rows.append({"car": n, "name": racecap_link._who(c), "lap": lap["lap"], "lap_time": round(lap["time"], 3),
                     "seconds": round(fought.get(n, 0.0)), "gap": gap, "read": n in done})
    rows.sort(key=lambda r: (-r["seconds"], r["gap"] if r["gap"] is not None else 999, r["car"]))
    return rows[:MAX_CHOICES]


# ---------------------------------------------------------------- the trace (pure)
def build_trace(rows, track_length_m, meta, lap_s) -> dict:
    """A trace in RaceCap's format from readings (session time, lap share, steer rad, rpm, gear, speed m/s or None).
    Raises ValueError with plain words when the readings aren't that whole lap."""
    rows = [r for r in rows if r[1] is not None and r[1] >= 0]
    k = 0
    while k < len(rows) and rows[k][1] > 0.5:                    # still before the line
        k += 1
    rows = rows[k:]
    if len(rows) < 100:
        raise ValueError("too few readings of that lap")
    t0 = rows[0][0]
    dist, t, speed, gear, rpm, steer = [], [], [], [], [], []
    prev = None
    for when, d, st, rp, gr, sp in rows:
        if prev is not None and d < prev[1] - 0.5:               # the line again: the lap is done
            break
        if prev is not None and d - prev[1] > MAX_GAP:
            raise ValueError("the readings have a hole in them")
        if sp is None:                                           # off the camera for a moment: from the distance covered
            sp = (d - prev[1]) * (track_length_m or 0) / (when - prev[0]) if prev is not None and when > prev[0] else None
        dist.append(d)
        t.append(when - t0)
        speed.append(sp * 3.6 if sp is not None else None)
        gear.append(gr)
        rpm.append(rp)
        steer.append(math.degrees(st) if st is not None else None)
        prev = (when, d)
    if dist[0] > 0.02 or dist[-1] < 0.98:
        raise ValueError("the readings don't cover the whole lap")
    if abs(t[-1] - lap_s) > max(TIMING_SLACK_S, 0.015 * lap_s):
        raise ValueError(f"that lap took {t[-1]:.1f} s in the replay but {lap_s:.1f} s in the scan")
    speed = _smooth(speed, 5)
    if any(v is None for v in speed):
        raise ValueError("no speed readings for part of that lap")
    every = max(1, math.ceil(len(dist) / MAX_POINTS))
    pick = lambda vals, nd: [None if v is None else round(float(v), nd) for v in vals[::every]]  # noqa: E731
    return {**meta, "mode": MODE, "source": "iracing", "inputs_from": INPUTS_FROM, "dist": pick(dist, 6), "t": pick(t, 3), "speed": pick(speed, 2),
            "gear": pick(gear, 0), "rpm": pick(rpm, 0), "steer": pick(steer, 2), "throttle": None, "brake": None}


def _smooth(values, n):
    half = n // 2
    out = []
    for k in range(len(values)):
        win = [v for v in values[max(0, k - half):k + half + 1] if v is not None]
        out.append(sum(win) / len(win) if win else None)
    return out


# ---------------------------------------------------------------- into the saved review (pure)
def compare_entry(data, tr) -> dict:
    """What the review needs for this lap: the same lap dict and telemetry a new review would use, on the review's own grid.
    Raises ValueError when the lap can't sit beside this review's laps."""
    made = racecap_link.rival_lap(tr)
    if made is None:
        raise ValueError("that lap's readings can't be used")
    lap, text = made
    raw = engine.parse_csv(text)
    why = engine.is_full_ref(lap, raw, data.get("median"))
    if why:
        raise ValueError(why)
    L = float(data["L"])
    R = engine.resample(raw, lap["lapTime"], L)
    if not engine.pedals_missing(R):        # only ever true of a lap with real pedals, which this read never keeps
        raise ValueError("that lap claims pedal data iRacing doesn't give")
    # the same shape as the traces engine.analyze saves, every second grid cell
    trace = {"tt": [round(float(v), 2) for v in R["tt"][::2]], "v": [int(round(float(v))) for v in R["v"][::2]],
             "b": [int(round(float(v))) for v in R["b"][::2]], "th": [int(round(float(v))) for v in R["th"][::2]],
             "g": [int(v) for v in R["gear"][::2]], "t": lap["lapTime"], "label": lap["driver_name"], "role": lap["_role"], "no_pedals": True}
    if not any(len(trace["tt"]) == len(v.get("tt") or []) for k, v in (data.get("traces") or {}).items() if str(k).startswith("me") and isinstance(v, dict)):
        raise ValueError("that lap doesn't line up with the laps in this review")
    ref = {"name": lap["driver_name"], "t": lap["lapTime"], "rating": None, "role": lap["_role"], "main": False,
           "style": engine.lap_style(R), "rival": True, "car": str(tr.get("car"))}
    return {"ref": ref, "trace": trace, "split": _split(data.get("segments"), R, L, lap["lapTime"])}


def _split(seg, R, L, lap_time):
    cuts = seg.get("cuts") if isinstance(seg, dict) else None
    if not isinstance(cuts, list) or len(cuts) < 2:
        return None
    tt = [engine.tt_at(R, float(d)) for d in cuts]      # the last cut runs to the line, as engine.build_segments does
    tt[-1] = float(R["tt"][-1]) + (float(R["tt"][-1]) - float(R["tt"][-2])) * (L - float(R["d"][-1])) / engine.STEP
    s = [tt[i + 1] - tt[i] for i in range(len(tt) - 1)]
    tot = sum(s)
    return [round(v * lap_time / tot, 3) if tot > 0 else round(v, 3) for v in s]


def add_to_compare(data, entry) -> str:
    """Put a rival's lap on a review dict and return its key. Reading the same driver again replaces their lap."""
    ref = entry["ref"]
    refs = data.setdefault("refs", [])
    old = next((r for r in refs if isinstance(r, dict) and r.get("rival") and
                (r.get("car") == ref["car"] or racecap_link.same_name(r.get("name"), ref["name"]))), None)
    if old is not None:
        key = old.get("key")
        refs[refs.index(old)] = {"key": key, **ref}
    else:
        used = [int(m.group(1)) for r in refs if isinstance(r, dict) for m in [re.fullmatch(r"ref(\d+)", str(r.get("key")))] if m]
        used += [int(m.group(1)) for k in (data.get("traces") or {}) for m in [re.fullmatch(r"ref(\d+)", str(k))] if m]
        key = f"ref{max(used, default=-1) + 1}"
        refs.append({"key": key, **ref})
    data.setdefault("traces", {})[key] = entry["trace"]
    seg = data.get("segments")
    if entry["split"] is not None and isinstance(seg, dict):
        if not isinstance(seg.get("refs"), dict):
            seg["refs"] = {}
        seg["refs"][key] = entry["split"]
    return key


# ---------------------------------------------------------------- the saved review and its scan
def _review_and_scan(library_folder, review_dir):
    """(data, scan folder, scan, your car in it), or SystemExit in plain words. No iRacing, no requests."""
    from . import library
    try:
        data = json.loads((Path(review_dir) / "review.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        raise SystemExit("That review's data file is damaged.")
    meta = data.get("meta") or {}
    if library.session_kind(meta.get("session")) != "race":
        raise SystemExit("Reading a rival's lap is for race reviews. Open the race's review and read it from there.")
    if not isinstance(data.get("L"), (int, float)) or not any(str(k).startswith("me") for k in data.get("traces") or {}):
        raise SystemExit("This review is too old to compare laps in. Review the race again, then read the lap.")
    folder = replay_scan.find_for_event(library_folder, meta.get("event_id"))
    scan = racecap_link.read_scan(folder) if folder else None
    if scan is None:
        raise SystemExit("This race has no saved scan yet. Press Scan this race first, then read a rival's lap.")
    linked = (data.get("racecap") or {}).get("subsession") if isinstance(data.get("racecap"), dict) else None
    if linked and str(linked) != str(scan.get("subsession")):
        raise SystemExit(f"This review's race log is from race {linked}, but the saved scan is race {scan.get('subsession')}. Scan this race again first.")
    me = racecap_link.find_me(scan.get("cars") or [], meta.get("driver"))
    if me is None:
        raise SystemExit(f"Couldn't find {meta.get('driver')} in this race's saved scan.")
    return data, folder, scan, me


def _saved_cars(folder) -> set:
    try:
        return {f.name[:-len(f"_{MODE}.json")] for f in (Path(folder) / LAPS_DIR).glob(f"*_{MODE}.json")}
    except OSError:
        return set()


def choices(library_folder, review_dir) -> dict:
    """The drivers the window offers, the cars you fought most first."""
    data, folder, scan, me = _review_and_scan(library_folder, review_dir)
    return {"cars": candidates(scan, me, data.get("median"), _saved_cars(folder))}


# ---------------------------------------------------------------- talking to iRacing
def _say_stopped():
    return SystemExit("Stopped. Nothing was saved and your review is unchanged. Your replay is back where it was.")


def _read_lap(sim, clock, idx, number, group, session, lap, stop, say):
    """Readings of one lap at normal speed with the camera on the car. Assumes the caller puts the replay back."""
    t0, t1, secs = lap["t0"], lap["t1"], lap["time"]
    target = max(0.0, t0 - LEAD_IN_S)
    before = sim.read().get("session_time")
    sim.seek(session, target)
    limit = clock.now() + 30
    while True:         # landed: still, in that session, just before the lap, and moved (a seek takes a moment to happen)
        if stop():
            raise _say_stopped()
        a = sim.read()
        when = a.get("session_time")
        if a.get("session_num") == session and when is not None and target - LEAD_IN_S <= when <= t0 - 0.5:
            clock.sleep(0.5)
            b = sim.read()
            if b.get("session_num") == session and b.get("session_time") == when and (when != before or abs(when - target) < 0.1):
                break
        if clock.now() > limit:
            raise SystemExit("The replay didn't move to that lap. Nothing was saved and your review is unchanged.")
        clock.sleep(0.1)
    sim.cam_car(number, group)
    limit = clock.now() + 10
    while sim.read().get("cam_car_idx") != idx:
        if stop():
            raise _say_stopped()
        if clock.now() > limit:
            raise SystemExit(f"The camera wouldn't go to car #{number}. Nothing was saved and your review is unchanged.")
        clock.sleep(0.25)
    say(0.1, f"Watching lap {lap['lap']} of #{number} at normal speed. Leave iRacing alone; nothing is recorded.")
    sim.set_speed(1)
    rows, off, last, asked = [], 0, None, clock.now()
    limit = clock.now() + secs + LEAD_IN_S + 25.0
    shown = -1
    while True:
        if stop():
            raise _say_stopped()
        if clock.now() > limit:
            raise SystemExit("The replay didn't play through that lap. Nothing was saved and your review is unchanged. Keep iRacing open, leave it alone, and try again.")
        x = sim.read_lap(idx)
        when = x.get("session_time")
        if when is not None and x.get("session_num") is not None:
            if x["session_num"] != session or (last is not None and (when < last - 0.5 or when > last + 2.0)):
                raise SystemExit("The replay jumped while the lap was being read (was it touched?). Nothing was saved and your review is unchanged.")
            last = when
            on_car = x.get("cam_car_idx") == idx
            if not on_car and clock.now() - asked >= 1.0:       # iRacing's director took the camera away: ask for it back
                sim.cam_car(number, group)
                asked = clock.now()
            if t0 - 0.1 <= when:
                off += 0 if on_car else 1
                rows.append((float(when), x.get("pct"), x.get("steer"), x.get("rpm"), x.get("gear"), x.get("speed") if on_car else None))
            if when > t1 + 1.0:
                break
            pct = int(100 * max(0.0, min(1.0, (when - t0) / secs)))
            if pct >= shown + 5:
                shown = pct
                say(0.1 + 0.75 * pct / 100, f"Reading the lap: {pct}% of lap {lap['lap']} of #{number}")
        clock.sleep(1 / 60)
    if rows and off / len(rows) > OFF_CAMERA_SHARE:
        raise SystemExit(f"The camera didn't stay on #{number}, so its speed can't be trusted. Nothing was saved and your review is unchanged.")
    return rows


def _put_back(sim, here, paused_at, info):
    """The replay and camera go back to where they were. Each part is tried even if another fails."""
    try:
        sim.set_speed(0)
    except Exception:  # noqa: BLE001
        pass
    if here.get("cam_car_idx") in info["drivers"] and here.get("cam_group") is not None:
        try:
            sim.cam_car(info["drivers"][here["cam_car_idx"]]["number"], here["cam_group"], here.get("cam_num") or 0)
        except Exception:  # noqa: BLE001
            pass
    if paused_at.get("session_num") is not None and paused_at.get("session_time") is not None:
        try:
            sim.seek(paused_at["session_num"], paused_at["session_time"])
        except Exception:  # noqa: BLE001
            pass


def read_for_review(library_folder, review_dir, car, api=None, progress=None, should_stop=None, sim=None, clock=None) -> dict:
    """Read one driver's fastest clean lap and add it to this saved review's Compare. SystemExit with plain words on any failure,
    and then the review is exactly as it was."""
    from . import library, results
    say = progress or (lambda frac, msg: None)
    stop = should_stop or (lambda: False)
    d = Path(review_dir)
    data, folder, scan, me = _review_and_scan(library_folder, d)
    meta = data.get("meta") or {}
    number = str(car)
    rival = next((c for c in scan.get("cars") or [] if isinstance(c, dict) and str(c.get("number")) == number), None)
    if rival is None or not re.fullmatch(r"[A-Za-z0-9_-]{1,12}", number):
        raise SystemExit(f"Car #{number} isn't in this race's saved scan.")
    if number == str(me.get("number")):
        raise SystemExit("That's your own car. Choose another driver.")
    lap = pick_lap(scan, rival, data.get("median"))
    if lap is None:
        raise SystemExit(f"{racecap_link._who(rival)} has no clean lap to read in this race. Choose another driver.")
    say(0.02, "Looking for iRacing...")
    if sim is None:
        try:
            sim = replay_scan.RealSim()
        except ImportError:
            raise SystemExit("This copy of Debrief is missing its iRacing connection. Reinstall the newest version.") from None
        except Exception:  # noqa: BLE001  (not on Windows)
            raise SystemExit(OPEN_REPLAY.replace("Scan this race", "Read this lap")) from None
    clock = clock or replay_scan.Clock()
    if not sim.connect():
        raise SystemExit(OPEN_REPLAY.replace("Scan this race", "Read this lap"))
    try:
        info = sim.info()
        sub = info.get("subsession")
        if not sub or not info.get("drivers") or not info.get("sessions"):
            raise SystemExit(OPEN_REPLAY.replace("Scan this race", "Read this lap"))
        wrong = f"iRacing has race {sub} at {info.get('track')} loaded, which isn't this review's race. Load the right replay and try again."
        if not any("race" in (str(s.get("type")) + " " + str(s.get("name"))).lower() for s in info["sessions"]):
            raise SystemExit("The replay loaded in iRacing has no race session. Load this race's replay.")
        if str(sub) != str(scan.get("subsession")) or not racecap_link.same_track(info.get("track"), meta.get("track")):
            raise SystemExit(wrong)
        cars = [c for c in info["drivers"].values() if not c["pace_car"] and not c["spectator"]]
        if racecap_link.find_me(cars, meta.get("driver")) is None:
            raise SystemExit(f"Couldn't find {meta.get('driver')} in the replay loaded in iRacing (race {sub}). Load this race's replay.")
        if api is not None:
            try:
                ev = api.event_for_subsession(sub)
            except Exception:  # noqa: BLE001  (Garage 61 is never needed: the race ID already matched)
                ev = None
            if ev is not None and str(ev) != str(meta.get("event_id")):
                raise SystemExit(wrong)
        idx = next((i for i, c in info["drivers"].items() if str(c.get("number")) == number and racecap_link.same_name(c.get("name"), rival.get("name"))), None)
        if idx is None:
            raise SystemExit(f"Car #{number} {racecap_link._who(rival)} isn't in the replay loaded in iRacing.")
        session = scan.get("session_num")
        here = sim.read()
        if not isinstance(session, int):
            session = replay_scan.race_session(info, {}, here.get("session_num"))
        group = here.get("cam_group") if here.get("cam_group") is not None else 1
        paused_at = here
        try:
            sim.set_speed(0)
            clock.sleep(0.3)
            paused_at = sim.read()
            rows = _read_lap(sim, clock, idx, number, group, session, lap, stop, say)
        except Exception as e:  # noqa: BLE001  (a lost connection reads the same as any other failed read)
            raise SystemExit(f"Reading the lap stopped ({type(e).__name__}). Nothing was saved and your review is unchanged.") from None
        finally:
            _put_back(sim, here, paused_at, info)
    finally:
        if hasattr(sim, "close"):
            sim.close()
    say(0.88, "Adding the lap to Compare...")
    keep = {"car": number, "name": rival.get("name"), "lap": lap["lap"], "lap_time": round(lap["time"], 3), "session_num": session, "t0": lap["t0"], "t1": lap["t1"]}
    try:
        trace = build_trace(rows, info.get("track_length_m") or scan.get("track_length_m"), keep, lap["time"])
        entry = compare_entry(data, trace)
    except (ValueError, KeyError, TypeError, IndexError) as e:
        raise SystemExit(f"That lap couldn't be used: {e}. Nothing was saved and your review is unchanged.") from None
    path = Path(folder) / LAPS_DIR / f"{number}_{MODE}.json"
    before = path.read_bytes() if path.exists() else None
    written = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        replay_scan._write_atomic(path, json.dumps(trace, separators=(",", ":")).encode("utf-8"))
        written = True
        results._rerender(d, lambda x: add_to_compare(x, entry))
    except Exception as e:  # noqa: BLE001
        if written:         # a review that didn't take the lap keeps the laps folder as it was too
            if before is None:
                path.unlink(missing_ok=True)
            else:
                replay_scan._write_atomic(path, before)
        raise SystemExit(f"The lap couldn't be saved ({type(e).__name__}). Your review is unchanged.") from None
    try:
        library.rebuild(library_folder)
    except Exception:  # noqa: BLE001  (the lap is saved; the library page catches up next time)
        pass
    return {"review": d.name, "car": number, "name": entry["ref"]["name"], "lap": lap["lap"], "lap_time": lap["time"]}
