"""2.14 pre-release review fixes: the live practice focus agrees with the session score (dead band, the corner's last
grid cell read, gaps and sparse samples), the coach matches the plan's corner to its own reference corner, the wheel
mute stops rescanning, the session score's time verdict survives session drift, and the smaller fixes.
Run: python tests/test_214_review.py

Fake .ibt laps come from tests/corner_laps.py. Set DEBRIEF_REAL_TELEMETRY to a telemetry folder to also play real
.ibt files through the live focus (read only; never copied). No network, temp folders only."""
from __future__ import annotations

import glob
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
TMP = Path(tempfile.mkdtemp(prefix="debrief-214-review-"))

import corner_laps as cl  # noqa: E402
from debrief import cornercoach as cc, livecoach as lc, practiceplan as pp  # noqa: E402

fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


# ---------------------------------------------------------------- helpers: every metric, target at the session median
KEYS = {"brake_point": "brake_m", "peak_brake": "peak_pct", "trail": "trail_pct", "coast": "coast_s",
        "throttle_point": "pickup_m", "min_speed": "min_kmh", "corner_time": "t"}
OPS = {"brake_point": "between", "peak_brake": "<=", "trail": ">=", "coast": "<=", "throttle_point": "<=",
       "min_speed": ">=", "corner_time": "<="}
TOLS = {"brake_point": 5.0, "peak_brake": 3.0, "trail": 5.0, "coast": 0.1, "throttle_point": 5.0, "min_speed": 2.0,
        "corner_time": 0.0}


def edge_plans(zone, L, offline, q=50):
    """One plan per metric with its line at the q-th percentile of this file's own laps: the hardest case for the
    live cue (about half the passes sit near the line). Distances on multiples of 5 m, as real plans are."""
    out = []
    for m, key in KEYS.items():
        vals = [p[key] for p in offline.values() if p and p.get(key) is not None]
        if len(vals) < 4:
            continue
        x = float(np.percentile(vals, q))
        if key in ("brake_m", "pickup_m"):
            x = 5.0 * round(x / 5.0)
        rule = ({"key": key, "op": "between", "lo": x - 5.0, "hi": x + 5.0} if OPS[m] == "between"
                else {"key": key, "op": OPS[m], "value": x})
        out.append({"kind": "consistency" if m == "corner_time" else "focus", "metric": m, "id": zone["id"],
                    "zone": zone, "length_m": L, "success_rule": rule, "target": -x if m == "brake_point" else x,
                    "tolerance": TOLS[m], "plan_id": f"edge-{m}"})
    return out


def samples_of(path, hz=20):
    src = lc.IbtPlaybackSource(path, speed=0, rate_hz=hz)
    out = []
    while True:
        s = src.read()
        if s is None:
            return out
        out.append(s)


def contradictions(paths, corners, L, qs=(30, 50, 70)):
    """(cues spoken, cues whose hit/miss differs from the session score of that lap, live minus offline corner
    times) for every corner and metric over these files played at 20 Hz."""
    spoken = bad = 0
    tdiff = []
    for f in paths:
        laps, _ = cc.load_laps([f])
        if len(laps) < 4:
            continue
        S = samples_of(f)
        for c in corners:
            if c["start_m"] < 0 or c["end_m"] > L:
                continue                                # corners across the line: lap numbers don't line up
            zone = {k: c[k] for k in ("id", "apex_m", "start_m", "end_m")}
            offline = {x["lap"]: cc.phases(x["R"], zone) for x in laps}
            for q in qs:
                for plan in edge_plans(zone, L, offline, q):
                    fo = pp.LiveFocus(plan, repeat_laps=1)
                    for s in S:
                        lap = s.get("Lap")
                        said = fo.feed(s, ok=True, lap=lap)
                        if not said or lap not in offline or offline[lap] is None:
                            continue
                        hit_off = pp.measure(plan, offline[lap])[1]
                        if hit_off is None:
                            continue
                        spoken += 1
                        bad += bool(hit_off) != fo.passes[-1]["hit"]
                    if plan["metric"] == "corner_time" and q == 50:
                        tdiff += [p["value"] - offline[p["lap"]]["t"] for p in fo.passes
                                  if offline.get(p["lap"]) is not None]
    return spoken, bad, tdiff


# ---------------------------------------------------------------- S1: live cue never contradicts the session score
print("== S1: the live cue agrees with the session score (fake laps at 20 Hz)")
fA = cl.make(TMP / "a.ibt", 12, seed=5)
fB = cl.make(TMP / "b.ibt", 12, seed=11)
repF = cc.analyze_files([fA, fB])
LF = repF["info"]["length_m"]
CF = repF["corners"]
check(len(CF) >= 5, f"{len(CF)} corners found on the fake laps")
n, bad, td = contradictions([fA, fB], CF[1:4], LF)
check(n >= 100, f"{n} live cues compared with the session score")
check(bad == 0, f"no live cue contradicts the session score ({bad} of {n})")
check(td and abs(float(np.median(td))) < 0.01,
      f"corner time live vs session score: median difference {float(np.median(td)) if td else 0:+.4f} s (was -0.05)")

REAL = os.environ.get("DEBRIEF_REAL_TELEMETRY")
if REAL and Path(REAL).is_dir():
    print("== S1 on real files (DEBRIEF_REAL_TELEMETRY)")
    files = sorted(glob.glob(str(Path(REAL) / "mclaren570sgt4_zolder*.ibt")))
    rep = cc.analyze_files(files)
    if rep.get("corners"):
        n, bad, td = contradictions(files, rep["corners"], rep["info"]["length_m"], qs=(30, 50, 70))
        check(n >= 100, f"real files: {n} live cues compared")
        check(bad == 0, f"real files: no live cue contradicts the session score ({bad} of {n})")
        check(td and abs(float(np.median(td))) < 0.01, f"real files: corner time median difference "
                                                       f"{float(np.median(td)) if td else 0:+.4f} s")
else:
    print("(real files skipped: set DEBRIEF_REAL_TELEMETRY to a telemetry folder)")

print("== S1: the dead band")
bp = {"kind": "focus", "metric": "brake_point", "target": 140, "tolerance": 5.0,
      "success_rule": {"key": "brake_m", "op": "between", "lo": -145.0, "hi": -135.0}}
for v, want in ((-140.0, "Good brake point"), (-145.0, None), (-135.0, None), (-150.0, None), (-130.0, None),
                (-155.0, "Braked too early"), (-125.0, "Braked too late")):
    check(pp.progress_cue(bp, {"brake_m": v}) == want, f"brake point {v:g}: {want!r}")
pk = {"kind": "focus", "metric": "peak_brake", "tolerance": 3.0, "success_rule": {"key": "peak_pct", "op": "<=",
                                                                                  "value": 88.0}}
check(pp.progress_cue(pk, {"peak_pct": 87.0}) is None and pp.progress_cue(pk, {"peak_pct": 89.0}) is None,
      "peak brake within 1.5 of the line: quiet")
check(pp.progress_cue(pk, {"peak_pct": 86.0}) == "Good, 86" and pp.progress_cue(pk, {"peak_pct": 90.0}) ==
      "Too much brake", "peak brake clear of the line: spoken")
ct = {"kind": "consistency", "metric": "corner_time", "tolerance": 0.0,
      "success_rule": {"key": "t", "op": "<=", "value": 8.90}}
check(pp.progress_cue(ct, {"t": 8.87}) is None and pp.progress_cue(ct, {"t": 8.80}) == "Good, on pace",
      "corner time: quiet within 0.05 s of the line")
ms = {"kind": "focus", "metric": "min_speed", "tolerance": 2.0, "success_rule": {"key": "min_kmh", "op": ">=",
                                                                                 "value": 120.0}}
check(pp.progress_cue(ms, {"min_kmh": 120.5}) is None and pp.progress_cue(ms, {"min_kmh": 119.2}) is None and
      pp.progress_cue(ms, {"min_kmh": 121.5}) == "Good speed" and pp.progress_cue(ms, {"min_kmh": 118.0}) ==
      "More speed", "min speed: quiet within 1 km/h")
check(pp.dead_band({"success_rule": {"key": "coast_s"}, "tolerance": 0.3}) == 0.15, "coasting: half the tolerance")
check(pp.near_line({}, None) is False and pp.near_line(bp, {"brake_m": None}) is False, "no number: not near")


# ---------------------------------------------------------------- S2: gaps, the exit sample, sparse samples
print("== S2: gaps and sparse samples")
S20 = samples_of(fA)
z2 = {k: CF[2][k] for k in ("id", "apex_m", "start_m", "end_m")}
plan2 = {"kind": "focus", "metric": "min_speed", "id": z2["id"], "zone": z2, "length_m": LF, "plan_id": "g",
         "tolerance": 2.0, "success_rule": {"key": "min_kmh", "op": ">=", "value": 0.0}}
check(pp.LiveFocus.MAX_GAP_M == 10.0, "largest gap between two samples: 10 m (2 grid steps)")


def run(samples, **kw):
    fo = pp.LiveFocus(plan2, repeat_laps=1)
    for s in samples:
        fo.feed(s, ok=True, lap=s.get("Lap"))
    return fo


base = run(S20)
check(base.status()["measured"] >= 10 and base.unreliable == 0, f"clean feed: {base.status()['measured']} passes")


def rel_of(s):
    return ((float(s["LapDistPct"]) * LF - z2["apex_m"] + LF / 2) % LF) - LF / 2


def drop(samples, lo_rel, hi_rel, every_lap=True):
    """Samples with those between lo_rel and hi_rel metres from the apex left out (a stall in the live reads)."""
    return [s for s in samples if not (lo_rel <= rel_of(s) <= hi_rel)]


s_rel, e_rel = z2["start_m"] - z2["apex_m"], z2["end_m"] - z2["apex_m"]
mid = drop(S20, -6.0, 6.0)                                 # a 12 m stall at the apex
fo = run(mid)
check(fo.status()["measured"] == 0 and fo.unreliable >= 10, f"a 12 m stall in the corner: nothing measured "
                                                          f"({fo.unreliable} passes set aside)")
small = drop(S20, -2.0, 2.0)                               # one sample lost: still within 10 m at these speeds
check(run(small).status()["measured"] == base.status()["measured"], "one lost sample: still measured")
tail = drop(S20, e_rel + pp.LiveFocus.TAIL_M - 1.0, e_rel + pp.LiveFocus.TAIL_M + 14.0)
fo = run(tail)
check(fo.status()["measured"] == 0, "a gap at the sample after the corner: nothing measured")
sparse = S20[::3]                                          # about 6.7 samples a second
fo = run(sparse)
st = fo.status()
check(st["measured"] == 0 and fo.unreliable >= 5, f"samples too far apart: nothing measured ({fo.unreliable} set aside)")
check(fo.too_slow or "too slowly" not in st["progress"], "too slow flagged only when the time between samples is long")
S12 = samples_of(fA, hz=12)
fo = run(S12)
check(fo.status()["measured"] == 0 and fo.too_slow and "too slowly" in fo.status()["progress"],
      f"read 12 times a second: not measured, said so ({fo.status()['progress']!r})")
S15 = samples_of(fA, hz=15)
check(run(S15).status()["measured"] >= 10, "read 15 times a second: measured")


class SlowIrsdk(lc.IrsdkSource):
    def info(self):
        return {"car": {"path": "mclaren570sgt4"}, "track": {"code": "zolder gp"}, "length_m": LF}

    def read(self):
        return None


src = SlowIrsdk(rate_hz=8.0)
r = lc.LiveRunner(src, None, settings=lc.Settings(sample_hz=8.0),
                  focus_for=lambda car, track, L: pp.LiveFocus(plan2, length_m=L))
r._ensure_reference()
check(abs(src.interval - 1.0 / lc.FOCUS_MIN_HZ) < 1e-9, f"a focus is watched: live reads at least "
                                                         f"{lc.FOCUS_MIN_HZ:g} a second (was 8)")
r._focus_for = lambda car, track, L: None
r._key = None
r._ensure_reference()
check(abs(src.interval - 1.0 / 8.0) < 1e-9, "no focus: back to the coach's own sample rate")
src2 = SlowIrsdk(rate_hz=30.0)
r2 = lc.LiveRunner(src2, None, focus_for=lambda car, track, L: pp.LiveFocus(plan2, length_m=L))
r2._ensure_reference()
check(abs(src2.interval - 1.0 / 30.0) < 1e-9, "a faster sample rate is left as it is")


# ---------------------------------------------------------------- S3: the plan's corner on the coach's own numbering
print("== S3: the plan's corner matched to the reference's own corner")
ref = lc.build_reference([fA])
check(ref is not None and len(ref.corners) >= 5, "reference from the fake file")
target = ref.corners[2]
# the plan numbers this corner one higher (the corner coach found an extra kink before it)
zone_sh = {"id": f"C{target.num + 1}", "apex_m": target.apex_m + 12.0, "start_m": target.apex_m - 250.0,
           "end_m": target.apex_m + 150.0}
offl = {x["lap"]: cc.phases(x["R"], zone_sh) for x in cc.load_laps([fB])[0]}
mins = [p["min_kmh"] for p in offl.values() if p]
plan3 = {"kind": "focus", "metric": "min_speed", "id": zone_sh["id"], "corner": f"T{target.num + 1}",
         "zone": zone_sh, "length_m": LF, "plan_id": "s3", "tolerance": 2.0, "target": 0,
         "success_rule": {"key": "min_kmh", "op": ">=", "value": float(np.median(mins)) + 0.3}}
weak = [{"apex_m": target.apex_m, "lost_s": 0.4}]
coach = lc.Coach(ref, weak, lc.Settings(slower_kmh=0.1, early_m=0.1, repeat_laps=1, top_n=3))
coach.set_focus(pp.LiveFocus(plan3, repeat_laps=1))
check(coach.focus_corner is not None and coach.focus_corner.num == target.num,
      f"plan's {zone_sh['id']} is the reference's {target.id}")
check(any(w.corner.num == target.num for w in coach.weak), "the same corner is also a weak corner")
pushed = []
orig_push = coach.queue.push
coach.queue.push = lambda c: (pushed.append(c), orig_push(c))
said = []
for s in samples_of(fB):
    said += coach.feed(s)
fb = [c for c in pushed if c.kind == lc.KIND_FEEDBACK]
check(fb and all(c.key == f"feedback:{target.id}" and c.corner == target.id for c in fb),
      f"focus cues keyed on the reference's corner ({sorted(set(c.key for c in fb))})")
vocab = set(pp.cue_vocabulary(plan3))
check(all(c.text in vocab for c in fb), "no weak-corner feedback for the focus corner: only the plan's cues "
                                         f"({sorted(set(c.text for c in fb))})")
check(len([c for c in said if c.kind == lc.KIND_FEEDBACK]) >= 1, "a plan cue was spoken")
check(not [c for c in pushed if c.kind == lc.KIND_MARKER and c.corner == target.id],
      "no weak-corner brake marker for the focus corner either")
laps_said = {}
for c in said:
    if c.kind == lc.KIND_FEEDBACK:
        laps_said.setdefault(round(c.spoken_t // 90), []).append(c)
check(all(len(v) == 1 for v in laps_said.values()), "at most one cue about that corner per lap")
# the weak corner's own feedback comes back without the focus
coach2 = lc.Coach(ref, weak, lc.Settings(slower_kmh=0.1, early_m=0.1, repeat_laps=1, top_n=3))
pushed2 = []
o2 = coach2.queue.push
coach2.queue.push = lambda c: (pushed2.append(c), o2(c))
for s in samples_of(fB):
    coach2.feed(s)
check(any(c.kind == lc.KIND_FEEDBACK and c.text not in vocab for c in pushed2),
      "without a focus the weak corner's own feedback is given")
# a plan corner the reference doesn't have: measured, never spoken
far = {"id": "C2", "apex_m": (target.apex_m + ref.corners[3].apex_m) / 2.0, "start_m": 0.0, "end_m": 0.0}
far["start_m"], far["end_m"] = far["apex_m"] - 60.0, far["apex_m"] + 60.0
coach3 = lc.Coach(ref, [], lc.Settings())
coach3.set_focus(pp.LiveFocus(dict(plan3, zone=far, id="C2"), repeat_laps=1))
check(coach3.focus_corner is None, "no reference corner near the plan's apex: no match")
check(coach3.vocabulary() is not None, "vocabulary still works")
coach4 = lc.Coach(None, [], lc.Settings())
coach4.set_focus(pp.LiveFocus(plan3))
check(coach4.focus_corner is None, "no reference yet: no match")
coach4.set_reference(ref)
check(coach4.focus_corner is not None and coach4.focus_corner.num == target.num, "matched once a reference is set")

print(f"\n{'all passed' if not fails else str(fails) + ' FAILED'}")
sys.exit(1 if fails else 0)
