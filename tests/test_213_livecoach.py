"""2.13: the live voice coach (livecoach.py) and the voice (voice.py). Run: python tests/test_213_livecoach.py

Fake .ibt files here are driven by a speed profile (position is the integral of speed), so "2.5 s before the brake
point" can be checked against when the car really gets there. Real files are only used when present (never in CI)."""
from __future__ import annotations

import json
import math
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
TMP = Path(tempfile.mkdtemp(prefix="debrief-213-coach-"))

import fake_ibt as fi  # noqa: E402
from debrief import ibt, livecoach as lc, voice as vc  # noqa: E402

fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


# ---------------------------------------------------------------- a fake track driven by speed

L = 3980.0          # fake_ibt's session text says 3.98 km
VMAX, ACC = 60.0, 4.0
# corner: apex metres, minimum speed m/s, braking deceleration m/s2
BASE = {1: (600.0, 20.0, 10.0), 2: (1500.0, 30.0, 10.0), 3: (2400.0, 18.0, 10.0), 4: (3300.0, 35.0, 10.0)}


def profile(corners):
    """Speed and brake at every metre of the lap."""
    x = np.arange(0.0, L, 1.0)
    v = np.full(len(x), VMAX)
    brake = np.zeros(len(x))
    for apex, vmin, dec in corners.values():
        rel = ((x - apex + L / 2) % L) - L / 2
        before = np.sqrt(np.maximum(0.0, vmin ** 2 + 2 * dec * np.maximum(0.0, -rel)))
        after = np.sqrt(vmin ** 2 + 2 * ACC * np.maximum(0.0, rel))
        pv = np.where(rel <= 0, before, after)
        binding = pv < v
        brake[binding & (rel <= 0) & (pv < VMAX)] = 0.9
        brake[binding & (rel > 0)] = 0.0
        v = np.minimum(v, pv)
    return x, v, brake


def lap_samples(corners, rate=60):
    x, v, b = profile(corners)
    t = np.concatenate(([0.0], np.cumsum(1.0 / v)))[:-1]
    T = float(np.sum(1.0 / v))
    ts = np.arange(0.0, T, 1.0 / rate)
    xs = np.interp(ts, t, x)
    return T, xs / L, np.interp(xs, x, v), np.interp(xs, x, b) > 0.5


def make(path, laps, session="Practice", lead_pit=True, rate=60, start_date=1_793_620_000):
    """laps: list of dicts {corners: {...} overrides, clr: [(pct_from, pct_to, value)], pit: [(from, to)],
    off: [(from, to)]}. A lead-in (lap 0: from 85% of the lap, on pit road when lead_pit) comes first, and 3 s of the
    next lap after the last one so its line crossing is in the file."""
    cols = {k: [] for k in ("Lap", "LapDistPct", "LapDist", "LapLastLapTime", "OnPitRoad", "PlayerTrackSurface",
                            "PlayerCarMyIncidentCount", "IsOnTrack", "SessionTime", "Speed", "Brake", "Throttle",
                            "SteeringWheelAngle", "Gear", "CarLeftRight")}
    shown = -1.0
    sched = []                                   # (sample index from which LapLastLapTime shows value)

    def add(lapn, pct, spd, brk, pit=(), clr=(), off=()):
        n = len(pct)
        p = np.asarray(pct)
        c_pit = np.zeros(n, bool)
        for a, b in pit:
            c_pit |= (p >= a) & (p < b)
        c_clr = np.ones(n, np.int32)
        for a, b, val in clr:
            c_clr[(p >= a) & (p < b)] = val
        c_surf = np.full(n, ibt.ON_TRACK, np.int32)
        for a, b in off:
            c_surf[(p >= a) & (p < b)] = ibt.OFF_TRACK
        cols["Lap"] += [lapn] * n
        cols["LapDistPct"] += list(p)
        cols["LapDist"] += list(p * L)
        cols["OnPitRoad"] += list(c_pit)
        cols["PlayerTrackSurface"] += list(c_surf)
        cols["CarLeftRight"] += list(c_clr)
        cols["Speed"] += list(spd)
        cols["Brake"] += list(np.where(brk, 0.9, 0.0))
        cols["Throttle"] += list(np.where(brk, 0.0, 1.0))
        cols["Gear"] += list(np.where(np.asarray(spd) > 40, 5, 3))

    # lead-in: 85% -> 100% at 20 m/s
    m = int(0.15 * L / 20.0 * rate)
    add(0, 0.85 + 0.15 * np.arange(m) / m, np.full(m, 20.0), np.zeros(m, bool),
        pit=[(0.0, 1.01)] if lead_pit else ())
    times = []
    for i, spec in enumerate(laps):
        cs = {**BASE, **(spec.get("corners") or {})}
        T, p, v, b = lap_samples(cs, rate)
        times.append(T)
        sched.append((len(cols["Lap"]), None))
        add(i + 1, p, v, b, pit=spec.get("pit", ()), clr=spec.get("clr", ()), off=spec.get("off", ()))
    T, p, v, b = lap_samples(BASE, rate)
    k = 3 * rate
    sched.append((len(cols["Lap"]), None))
    add(len(laps) + 1, p[:k], v[:k], b[:k])
    n = len(cols["Lap"])
    last = np.full(n, -1.0)
    for j, (start, _x) in enumerate(sched[1:]):   # lap j finished at sched[j+1]; shown 2 s later
        last[start + 2 * rate:] = times[j]
    cols["LapLastLapTime"] = last
    cols["PlayerCarMyIncidentCount"] = np.zeros(n, np.int32)
    cols["IsOnTrack"] = np.ones(n, bool)
    cols["SessionTime"] = 100.0 + np.arange(n) / rate
    typ = {"Lap": "i", "LapDistPct": "f", "LapDist": "f", "LapLastLapTime": "f", "OnPitRoad": "b",
           "PlayerTrackSurface": "i", "PlayerCarMyIncidentCount": "i", "IsOnTrack": "b", "SessionTime": "d",
           "Speed": "f", "Brake": "f", "Throttle": "f", "SteeringWheelAngle": "f", "Gear": "i", "CarLeftRight": "i"}
    out = {nm: (typ[nm], np.asarray(cols[nm])) for nm in typ}
    out["SessionNum"] = ("i", np.zeros(n, np.int32))
    ibt.write_fake(path, fi.session_text(session=session), out, tick_rate=rate, start_date=start_date)
    return path, times


def play(path, settings=None, weak=None, ref=None, voice=None):
    """Plays a file as fast as possible straight into a Coach (no thread). Returns (coach, voice, source)."""
    src = lc.IbtPlaybackSource(path, speed=0)
    coach = lc.Coach(ref, weak, settings or lc.Settings())
    v = voice or vc.FakeVoice()
    while True:
        s = src.read()
        if s is None:
            break
        for c in coach.feed(s):
            v.say(c.text, priority=c.priority, t=c.spoken_t)
    return coach, v, src


def crossing_time(path, lapn, pct_at):
    """When the car in the file reaches pct_at on lap lapn (interpolated)."""
    with ibt.open_ibt(path) as ib:
        lap, p, t = ib.col("Lap"), ib.col("LapDistPct"), ib.col("SessionTime")
        idx = np.nonzero((lap == lapn) & (p >= pct_at))[0]
        i = int(idx[0])
        return float(t[i - 1] + (pct_at - p[i - 1]) / (p[i] - p[i - 1]) * (t[i] - t[i - 1]))


def gaps_ok(said, gap=4.0):
    ts = [x["t"] for x in said]
    return all(b - a >= gap - 1e-6 for a, b in zip(ts, ts[1:]))


# ---------------------------------------------------------------- reference
refp, ref_times = make(TMP / "mclaren570sgt4_zolder gp ref.ibt", [{}, {}, {}])
ref = lc.build_reference(refp)
check(ref is not None and len(ref.corners) == 4, f"reference: 4 corners found ({ref and len(ref.corners)})")
check(abs(ref.lap_time - ref_times[1]) < 0.02, f"reference lap time {ref.lap_time:.3f} ~ {ref_times[1]:.3f}")
check(ref.source["kind"] == "ibt" and ref.source["lap"] in (1, 2, 3), "reference source names the file lap")
check(ref.car["path"] == "mclaren570sgt4" and ref.track["code"] == "zolder gp", "reference car and track")
c2 = ref.corner(2)
_x, _v, _b = profile(BASE)
true_brake2 = float(_x[np.nonzero(_b[:1500] > 0)[0][-1] - 0])   # last braking metre before apex 2 is the apex
true_onset2 = float(_x[np.nonzero(np.diff(_b[:1500]) > 0)[0][0] + 1])
check(abs(c2.apex_m - 1500) < 15, f"T2 apex {c2.apex_m:.0f} m")
check(abs(c2.brake_m - true_onset2) < 15, f"T2 brake point {c2.brake_m:.0f} m (true {true_onset2:.0f})")
check(abs(c2.min_kmh - 30 * 3.6) < 2.0, f"T2 min speed {c2.min_kmh:.1f} km/h")
check(len(ref.speed) == lc.GRID and len(ref.gear) == lc.GRID and ref.brake.max() > 0.5, "reference grid arrays")
json.dumps(ref.summary())
check(ref.describe().startswith(lc.fmt_lap(ref.lap_time)) and " from " in ref.describe(), f"describe: {ref.describe()}")
check(lc.build_reference(TMP / "nope.ibt") is None, "a missing file gives no reference (no exception)")
fdir = TMP / "telemetry"
fdir.mkdir()
make(fdir / "mclaren570sgt4_zolder gp 2026-11-01 10-00-00.ibt", [{}, {}])
make(fdir / "fordmustanggt4_zolder gp 2026-11-01 11-00-00.ibt", [{"corners": {4: (3300.0, 45.0, 10.0)}}, {}])
fr = lc.find_reference(fdir, "mclaren570sgt4", "zolder gp")
check(fr is not None and fr.source["file"].startswith("mclaren570sgt4"), "find_reference picks this car's files only")

# Garage 61 CSV shape and recorded samples
T, p, v, b = lap_samples(BASE, 60)
csv = "Speed,LapDistPct,Brake,Throttle,Gear\n" + "\n".join(
    f"{v[i]:.3f},{p[i]:.5f},{0.9 if b[i] else 0},{0 if b[i] else 1},4" for i in range(len(p)))
g = lc.reference_from_g61_csv(csv, T, L, car={"path": "mclaren570sgt4"}, track={"code": "zolder gp"}, lap_id="x1")
check(g.source["kind"] == "g61" and len(g.corners) == 4 and abs(g.corner(2).brake_m - c2.brake_m) < 10,
      "a Garage 61 lap gives the same corners and brake points")

# weak corners: prep.weak_corners rows, matched by apex; top 3 by time lost
weak = [{"id": "C4", "apex_m": 3290, "avg_avail": 0.05}, {"id": "C2", "apex_m": 1510, "avg_avail": 0.40},
        {"id": "C3", "apex_m": 2395, "avg_avail": 0.30}, {"id": "C1", "apex_m": 605, "avg_avail": 0.10}]
mw = lc.match_weak(ref, weak, 3)
check([w.name for w in mw] == ["T2", "T3", "T1"], f"top 3 weak corners by time lost: {[w.name for w in mw]}")
check([w.name for w in lc.match_weak(ref, [{"apex_m": 1000}, 3, "C1"], 3)] == ["T3", "T1"],
      "far-off position ignored; numbers and names accepted")

# ---------------------------------------------------------------- settings
s0 = lc.Settings()
check(s0.top_n == 3 and not s0.in_races and s0.marker_lead_s == 2.5 and s0.gap_s == 4.0, "settings defaults")
s1 = lc.Settings.from_dict(json.loads(json.dumps({**s0.to_dict(), "in_races": "true", "top_n": "2", "rate": 99,
                                                   "bogus": 1, "gap_s": "x"})))
check(s1.in_races is True and s1.top_n == 2 and s1.rate == 10 and s1.gap_s == 4.0, "settings from JSON: coerced and clamped")

# ---------------------------------------------------------------- practice session
SLOW2 = {2: (1500.0, 30.0 - 6 / 3.6, 7.0)}          # T2 braked early AND 6 km/h slower
EARLY3 = {3: (2400.0, 18.0, 8.0)}                   # T3 braked early, same speed
FAST4 = {4: (3300.0, 38.0, 10.0)}                   # T4 quicker: a new best
cur, cur_times = make(TMP / "cur.ibt", [{}, {"corners": SLOW2}, {"corners": EARLY3},
                                        {"clr": [(0.20, 0.40, 2)]}, {"corners": FAST4}, {}])
coach, fv, _src = play(cur, weak=weak, ref=ref)
said = fv.said
texts = fv.texts()
print("   practice cues:", [(round(x["t"] - 100, 1), x["text"]) for x in said])
check(len(said) > 5, f"cues spoken ({len(said)})")
m2 = [x for x in said if x["text"] == "Brake marker, T2"]
check(len(m2) >= 3, f"T2 marker on the coachable laps ({len(m2)})")
lap_of = lambda t: next(i for i, x in enumerate([100.0 + 0.15 * L / 20.0] + list(np.cumsum(cur_times) + 100.0 + 0.15 * L / 20.0)) if x > t)  # noqa: E731
leads = []
for x in m2:
    lp = lap_of(x["t"])
    leads.append(crossing_time(cur, lp, c2.brake_pct) - x["t"])
check(all(2.2 <= d <= 2.6 for d in leads), f"T2 marker about 2.5 s before the reference brake point: {[round(d, 2) for d in leads]}")
check(not any("T4" in t for t in texts), "T4 (not in the top 3) is never mentioned")
fb = [x for x in said if x["text"].startswith("T2: carry more speed")]
check(len(fb) == 1 and "6 down" in fb[0]["text"], f"feedback after the slow T2: {[x['text'] for x in fb]}")
apex2_lap2 = crossing_time(cur, 2, c2.apex_pct)
check(fb and fb[0]["t"] > apex2_lap2 and fb[0]["t"] - apex2_lap2 < 12, "feedback comes after the corner, not long after")
check(sum(t == "T3: braked early" for t in texts) == 1, "T3 braked early (same speed) said once")
lapcues = [x for x in said if x["text"].startswith(("Down", "Up", "New best"))]
check(any(x["text"].startswith("Down") and "tenth" in x["text"] or "seconds" in x["text"] for x in lapcues),
      f"lap delta after the slow lap: {[x['text'] for x in lapcues]}")
check(not any(abs(t - cur_times[0]) < 0.1 and False for t in cur_times), "")
d1 = cur_times[0] - ref.lap_time
check(abs(d1) < 0.1 and len([x for x in lapcues if lap_of(x["t"]) == 2]) == 0, f"no lap cue for a lap within 0.1 s ({d1:+.3f})")
nb = [x for x in said if x["text"].startswith("New best")]
check(len(nb) == 1, f"new best said on the quick lap: {[x['text'] for x in nb]}")
check(any("reference updated" in e["text"] for e in coach.events) and abs(coach.reference.lap_time - cur_times[4]) < 0.05,
      f"reference updated to the new best ({coach.reference.lap_time:.3f})")
check(coach.reference.source["kind"] == "live" and len(coach.reference.corners) == 4, "the live reference has its corners")
# car alongside on lap 4 from 20% to 40% (T2's zone): nothing then and for 3 s after
t_a = crossing_time(cur, 4, 0.20)
t_b = crossing_time(cur, 4, 0.40) + 3.0
check(not any(t_a <= x["t"] <= t_b for x in said), "nothing while a car is alongside and 3 s after")
check(not any(lap_of(x["t"]) == 4 and "T2" in x["text"] for x in said), "no T2 marker or feedback in traffic")
check(gaps_ok(said), "never two cues within 4 s")
check(not any(lap_of(x["t"]) <= 1 and not x["text"].startswith(("Down", "Up", "New")) for x in said),
      "nothing on the lead-in from the pits (out lap)")
check(coach.weak and [w.name for w in coach.weak] == ["T2", "T3", "T1"], "weak corners matched again on the new reference")

# the same through the LiveRunner thread (as fast as possible)
before = {t.name for t in threading.enumerate()}
fv2 = vc.FakeVoice()
r = lc.LiveRunner(lc.IbtPlaybackSource(cur, speed=0), fv2, reference=ref, weak_corners=weak)
r.start()
t0 = time.monotonic()
while r.running() and time.monotonic() - t0 < 60:
    time.sleep(0.05)
check(not r.running() and r.status()["state"] == "finished", f"runner finished the playback ({r.status()['state']})")
check(fv2.texts() == texts, "runner speaks exactly what the coach says")
check(r.stop() and "debrief-livecoach" not in {t.name for t in threading.enumerate()} - before, "no thread left")

# ---------------------------------------------------------------- races, pits, out laps
race, _rt = make(TMP / "race.ibt", [{}, {"corners": SLOW2}, {}], session="Race", lead_pit=False)
_c, fr0, _s = play(race, weak=weak, ref=ref)
check(fr0.said == [] and _c.quiet_reason == "Race session (coaching in races is off)", "no cues in a race by default")
_c, fr1, _s = play(race, settings=lc.Settings(in_races=True), weak=weak, ref=ref)
print("   race cues:", [(round(x["t"] - 100, 1), x["text"]) for x in fr1.said])
check(len(fr1.said) > 0, f"cues in a race with in_races ({len(fr1.said)})")
end_lap1 = crossing_time(race, 2, 0.0001)
check(not any(x["t"] < end_lap1 for x in fr1.said), "nothing on race lap 1 (the start lap), not even its lap time")

pits, _pt = make(TMP / "pits.ibt", [{"pit": [(0.0, 0.12)]}, {}, {"pit": [(0.9, 1.01)]}, {}])
_c, fp, _s = play(pits, weak=weak, ref=ref)
print("   pit cues:", [(round(x["t"] - 100, 1), x["text"]) for x in fp.said])
lap2_start = crossing_time(pits, 2, 0.0001)
check(not any(x["t"] < lap2_start for x in fp.said), "nothing on a lap that started on pit road (out lap)")
check(any(lap2_start <= x["t"] < crossing_time(pits, 3, 0.0001) for x in fp.said), "coaching on the next lap")
pit_in = crossing_time(pits, 3, 0.9)
lap4_start = crossing_time(pits, 4, 0.0001)
check(not any(pit_in <= x["t"] < lap4_start + 1 for x in fp.said), "nothing on pit road, no lap time for the in lap")
off, _ot = make(TMP / "off.ibt", [{}, {"off": [(0.36, 0.38)], "corners": SLOW2}, {}])
_c, fo, _s = play(off, weak=weak, ref=ref)
check(not any("T2: carry" in t for t in fo.texts()), "no corner feedback when off track there")
check(not any(t.startswith(("Down", "New")) and crossing_time(off, 3, 0.0001) - 1 < x["t"] < crossing_time(off, 3, 0.0001) + 3
              for x in fo.said for t in [x["text"]]), "no lap time for a lap with an off")

# ---------------------------------------------------------------- mute, rate cap, stale cues
_c, fm, _s = play(cur, settings=lc.Settings(muted=True), weak=weak, ref=ref)
check(fm.said == [] and _c.quiet_reason == "Muted", "muted: nothing said")
co = lc.Coach(ref, weak)
check(co.toggle() is True and co.settings.muted and co.toggle() is False, "toggle mutes and unmutes")
_c, fg, _s = play(cur, settings=lc.Settings(gap_s=30.0), weak=weak, ref=ref)
check(gaps_ok(fg.said, 30.0) and len(fg.said) >= 3, f"a longer gap is kept ({len(fg.said)} cues)")
check(len(_c.queue.dropped) > 0, f"cues that couldn't be said in time were dropped ({len(_c.queue.dropped)})")
check(all(x.spoken_t - x.t <= 2.0 + 1e-6 for x in _c.spoken), "no cue spoken more than 2 s after it was made")
q = lc.CueQueue()
q.push(lc.Cue("old", 2, "a", t=10.0, kind="feedback", expires=12.0))
q.push(lc.Cue("new", 1, "b", t=11.5, kind="lap", expires=13.5))
check(q.pop(12.5) .text == "new" and [c.text for c in q.dropped] == ["old"], "stale cue dropped, fresh one given")
q.push(lc.Cue("m", 3, "m", t=0, expires=5))
q.push(lc.Cue("l", 1, "l", t=0, expires=5))
check(q.pop(1.0).text == "m", "most important cue first")
check(q.pop(1.0, allowed=lambda c: False) is None and len(q.items) == 1, "a blocked cue waits")
check(lc.delta_words(0.24) == "two tenths" and lc.delta_words(-0.1) == "a tenth" and lc.delta_words(1.36) == "1.4 seconds",
      "delta words")

# no reference: nothing said; the first clean lap becomes the reference
nr = lc.Coach(None, weak)
nr.hint(L, {"path": "mclaren570sgt4"}, {"code": "zolder gp"})
src = lc.IbtPlaybackSource(cur, speed=0)
out = []
while (s := src.read()) is not None:
    out += nr.feed(s)
check(nr.reference is not None and len(nr.reference.corners) == 4, "no reference at first: a clean lap becomes one")
check(nr.feed({"SessionTime": "bad", "LapDistPct": 0.5}) == [], "a bad sample is skipped, never raises")

# ---------------------------------------------------------------- runner start/stop, live source
before = {t.name for t in threading.enumerate()}
r = lc.LiveRunner(lc.IbtPlaybackSource(cur, speed=1.0), vc.FakeVoice(), reference=ref, weak_corners=weak)
r.start()
time.sleep(0.4)
st = r.status()
check(st["state"] == "coaching" and st["text"].startswith("Coaching: Mclaren 570s GT4 at Zolder, reference "),
      f"status while coaching: {st['text']}")
json.dumps(st)
r.mute()
check(r.status()["muted"] and r.status()["text"].endswith("(muted)"), "mute shows in the status")
t0 = time.monotonic()
check(r.stop(timeout=2.0) and time.monotonic() - t0 < 1.0, "stop is quick")
check("debrief-livecoach" not in {t.name for t in threading.enumerate()} - before, "no leftover thread after stop")

sys.modules.pop("irsdk", None)
src = lc.IrsdkSource()
check(src.read() is None and src.state == "not_installed", f"IrsdkSource without pyirsdk: None ({src.state})")


class _FakeIR:
    is_initialized = False
    is_connected = False

    def startup(self):
        return False

    def shutdown(self):
        pass


sys.modules["irsdk"] = types.SimpleNamespace(IRSDK=_FakeIR)
try:
    r = lc.LiveRunner(lc.IrsdkSource(retry_s=0.1), vc.FakeVoice(), telemetry_dir=fdir)
    r.start()
    time.sleep(0.3)
    check(r.status()["state"] == "waiting" and r.status()["text"] == "Listening — iRacing not running",
          f"iRacing not running: {r.status()['text']}")
    check(r.stop(timeout=2.5), "runner waiting for iRacing stops cleanly")
finally:
    sys.modules.pop("irsdk", None)


class _LiveIR:
    """iRacing as pyirsdk shows it, replaying the fake file."""
    is_initialized = True
    is_connected = True

    def __init__(self):
        self.src = lc.IbtPlaybackSource(cur, speed=0)
        self.s = self.src.read()

    def startup(self):
        return True

    def shutdown(self):
        pass

    def freeze_var_buffer_latest(self):
        self.s = self.src.read() or self.s

    def unfreeze_var_buffer_latest(self):
        pass

    def __getitem__(self, k):
        if k == "DriverInfo":
            return {"DriverCarIdx": 3, "Drivers": [{"CarIdx": 3, "CarPath": "mclaren570sgt4", "CarScreenName": "Mclaren 570s GT4"}]}
        if k == "WeekendInfo":
            return {"TrackName": "zolder gp", "TrackDisplayName": "Zolder Circuit", "TrackLength": "3.98 km"}
        if k == "SessionInfo":
            return {"Sessions": [{"SessionNum": 0, "SessionType": "Practice"}]}
        return self.s.get(k)


sys.modules["irsdk"] = types.SimpleNamespace(IRSDK=_LiveIR)
try:
    src = lc.IrsdkSource()
    s = src.read()
    check(s is not None and s["SessionType"] == "Practice" and "CarLeftRight" in s and src.state == "connected",
          "IrsdkSource reads the channels and the session type")
    check(src.info()["car"]["path"] == "mclaren570sgt4" and abs(src.info()["length_m"] - 3980) < 1, "IrsdkSource info")
    src.interval = 0
    r = lc.LiveRunner(src, vc.FakeVoice(), telemetry_dir=fdir, weak_for=lambda car, track: weak)
    r.start()
    time.sleep(1.0)
    st = r.status()
    check(st["state"] == "coaching" and st["reference"]["source"]["file"].startswith("mclaren570sgt4"),
          f"live: reference found in the telemetry folder ({st['text']})")
    check(r.stop(timeout=2.0), "live runner stops")
finally:
    sys.modules.pop("irsdk", None)

# ---------------------------------------------------------------- voice
nv = vc.make_voice() if sys.platform != "win32" else vc.NullVoice("test")
check(isinstance(nv, vc.NullVoice) and nv.say("hello") is False and nv.status()["available"] is False,
      "off Windows the voice is a silent stand-in")
fk = vc.FakeVoice(clock=lambda: 5.0)
fk.say("Down two tenths", priority=1, t=10.0)
fk.say("Brake marker, T2", priority=3, t=10.5)
fk.say("T2: braked early", priority=2, t=20.0)
check([x["purge"] for x in fk.said] == [False, True, False], "a more important cue cuts off one still being spoken")
check(lc.parse_hotkey("ctrl+shift+m") == (0x6, ord("M")) and lc.parse_hotkey("f9") == (0, 0x78) and lc.parse_hotkey("hyper+q") is None,
      "hotkey text parsed")
check(lc.Hotkey("ctrl+shift+m", lambda: None).start() is (sys.platform == "win32" and None is not None),
      "the mute key is a no-op off Windows")
src_text = (ROOT / "debrief" / "voice.py").read_text() + (ROOT / "debrief" / "livecoach.py").read_text()
top = [ln for ln in src_text.splitlines() if ln.startswith(("import ", "from "))]
check(not any(m in ln for ln in top for m in ("irsdk", "win32", "comtypes", "pythoncom", "ctypes")),
      "nothing Windows-only or pyirsdk imported at module level")

# ---------------------------------------------------------------- the owner's real files (only when present)
REAL = Path("/mnt/user-data/uploads/Documents/iRacing/telemetry")
zr = REAL / "mclaren570sgt4_zolder gp 2026-11-02 12-43-35.ibt"
zl = REAL / "mclaren570sgt4_zolder gp 2026-11-02 13-12-16.ibt"
if zr.exists() and zl.exists():
    rr = lc.build_reference(zr)
    check(rr is not None and abs(rr.lap_time - 98.874) < 0.01, f"real: reference {rr.describe()} (lap {rr.source['lap']})")
    print(f"   real: {len(rr.corners)} corners, {len(rr.braked)} with a brake point, lap {rr.length_m:.0f} m")
    for c in rr.corners:
        print(f"     {c.name}: brake {c.brake_pct * 100:5.1f}% {c.brake_m:5.0f} m  apex {c.apex_pct * 100:5.1f}% "
              f"{c.apex_m:5.0f} m  min {c.min_kmh:5.1f} km/h" if c.brake_pct is not None else f"     {c.name}: lift")
    check(6 <= len(rr.braked) <= 11, "real: Zolder braking zones in a sensible range")
    _c, fz, _s = play(zl, settings=lc.Settings(in_races=True), ref=rr,
                      weak=[{"id": "C4", "apex_m": rr.corner(4).apex_m, "avg_avail": 0.3},
                            {"id": "C6", "apex_m": rr.corner(6).apex_m, "avg_avail": 0.2},
                            {"id": "C2", "apex_m": rr.corner(2).apex_m, "avg_avail": 0.1}])
    print("   real 13-12-16 cues (session time):", [(round(x["t"], 1), x["text"]) for x in fz.said])
    print("   real last lap:", _c.last_lap, "events:", _c.events)
    check(gaps_ok(fz.said), "real: never two cues within 4 s")
else:
    print("skip real files (not on this machine)")

print("FAILED" if fails else "ALL OK", fails)
sys.exit(1 if fails else 0)
