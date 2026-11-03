"""2.13: corner coach (per-corner advice in plain words from .ibt telemetry). Run: python tests/test_213_corners.py

Fake laps are built here from a simple driving model (speed from braking point, minimum speed and throttle
pick-up; brake, throttle, steering, ABS and wheel speeds to match), so each rule can be made to show on purpose.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
TMP = Path(tempfile.mkdtemp(prefix="debrief-213-corners-"))

import fake_ibt as fi  # noqa: E402  (session text only; this file builds its own samples)
from debrief import cornercoach as cc  # noqa: E402
from debrief import ibt  # noqa: E402

fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


# ---------------------------------------------------------------- fake laps
L = 3980.0          # fake_ibt.session_text says 3.98 km
VMAX = 60.0
ACC = 5.0
DS = 0.5
RATIO = {3: 180.0, 4: 140.0, 5: 115.0}
BASE = {"vmin": 25.0, "bdist": 140.0, "peak": 0.75, "rs": 60.0, "re": 40.0, "pu": 10.0, "maint": 0.0,
        "spin": False, "lock": False, "abs_at": 0.85}
APEXES = (1000.0, 2800.0)


def corner_env(d, A, p):
    """speed, brake, throttle envelopes of one corner over distance d."""
    v = np.full_like(d, VMAX)
    b = np.zeros_like(d)
    th = np.ones_like(d)
    s0 = A - p["bdist"]
    m = (d >= s0) & (d <= A)
    v[m] = np.sqrt(p["vmin"] ** 2 + (VMAX ** 2 - p["vmin"] ** 2) * (A - d[m]) / p["bdist"])
    go = A + max(0.0, p["pu"])
    m = (d > A) & (d <= go)
    v[m] = p["vmin"]
    m = d > go
    v[m] = np.minimum(VMAX, np.sqrt(p["vmin"] ** 2 + 2 * ACC * (d[m] - go)))
    # brake: firm from the brake point to rs before the apex, then linear to 0 at re before the apex
    rs, re_ = A - p["rs"], A - p["re"]
    m = (d >= s0) & (d < rs)
    b[m] = p["peak"] * np.clip((d[m] - s0) / 5.0, 0.2, 1.0)
    m = (d >= rs) & (d < re_)
    b[m] = p["peak"] * (re_ - d[m]) / max(1e-6, re_ - rs)
    # throttle: off from the brake point, maintenance between release and pick-up, ramp to full over 40 m
    pu = A + p["pu"]
    m = (d >= s0) & (d < pu)
    th[m] = 0.0
    m = (d >= re_) & (d < pu)
    th[m] = p["maint"]
    m = (d >= pu) & (d < pu + 40)
    th[m] = 0.3 + 0.7 * (d[m] - pu) / 40.0
    return v, b, th


def lap_arrays(params):
    """params: list (one per corner) -> dict of channel arrays on a DS grid and the lap time."""
    d = np.arange(0.0, L, DS)
    v = np.full_like(d, VMAX)
    b = np.zeros_like(d)
    th = np.ones_like(d)
    st = np.zeros_like(d)
    front = None
    spin_m = np.zeros(len(d), bool)
    lock_m = np.zeros(len(d), bool)
    abs_at = np.full_like(d, 2.0)
    for A, p in zip(APEXES, params):
        cv, cb, ct = corner_env(d, A, p)
        v = np.minimum(v, cv)
        b = np.maximum(b, cb)
        th = np.minimum(th, ct)
        tri = np.clip(np.where(d <= A, (d - (A - 40)) / 40.0, ((A + 60) - d) / 60.0), 0.0, 1.0)
        st = st + 1.0 * tri
        if p["spin"]:
            spin_m |= (d >= A + p["pu"]) & (d < A + p["pu"] + 40)
        if p["lock"]:
            lock_m |= (d >= A - p["bdist"] + 10) & (d < A - p["bdist"] + 30)
        zone = (d >= A - p["bdist"] - 10) & (d <= A + 100)
        abs_at[zone] = p["abs_at"]
    t = np.concatenate([[0.0], np.cumsum(DS / v[:-1])])
    T = float(t[-1] + DS / v[-1])
    gear = np.where(v < 35, 3, np.where(v < 48, 4, 5))
    rpm = v * np.vectorize(RATIO.get)(gear)
    front = v.copy()
    front[lock_m] *= 0.6
    rear = v * 1.01
    rear[spin_m] *= 1.2
    return {"d": d, "t": t, "T": T, "Speed": v, "Brake": b, "Throttle": th, "SteeringWheelAngle": st,
            "BrakeABSactive": (b >= abs_at - 1e-9) & (b > 0.05), "Gear": gear, "RPM": rpm,
            "LFspeed": front, "RFspeed": front, "LRspeed": rear, "RRspeed": rear}


def make_file(path, lap_params, rate=60, wheels=True, abs_ch=True, steer=True, session="Practice", **kw):
    """lap_params: one list of per-corner param dicts per lap. Writes an .ibt (out lap + laps + 5 s tail)."""
    laps = [lap_arrays(p) for p in lap_params]
    chans = ["Speed", "Brake", "Throttle", "Gear", "RPM"] + (["SteeringWheelAngle"] if steer else []) + \
        (["BrakeABSactive"] if abs_ch else []) + (["LFspeed", "RFspeed", "LRspeed", "RRspeed"] if wheels else [])
    cols = {k: [] for k in chans + ["Lap", "LapDistPct", "LapLastLapTime"]}
    # out lap: the last 5% of the lap at full speed
    n0 = int(0.05 * L / VMAX * rate)
    shown = -1.0
    seg = {"Lap": np.zeros(n0, int), "LapDistPct": np.linspace(0.95, 0.999, n0), "LapLastLapTime": np.full(n0, -1.0)}
    for k in chans:
        seg[k] = np.full(n0, laps[0][k][0], float) if k != "BrakeABSactive" else np.zeros(n0, bool)
    for k in cols:
        cols[k].append(seg[k])
    for i, lp in enumerate(laps + [laps[-1]]):
        tail = i == len(laps)
        ts = np.arange(0.0, 5.0 if tail else lp["T"], 1.0 / rate)
        dd = np.interp(ts, lp["t"], lp["d"])
        seg = {"Lap": np.full(len(ts), i + 1, int), "LapDistPct": dd / L}
        last = np.full(len(ts), shown)
        if i > 0:
            last[2 * rate:] = laps[i - 1]["T"]
            shown = laps[i - 1]["T"]
        seg["LapLastLapTime"] = last
        for k in chans:
            src = lp[k].astype(float)
            seg[k] = np.interp(dd, lp["d"], src)
            if k == "BrakeABSactive":
                seg[k] = seg[k] > 0.5
            if k == "Gear":
                seg[k] = np.round(seg[k]).astype(int)
        for k in cols:
            cols[k].append(seg[k])
    arr = {k: np.concatenate(v) for k, v in cols.items()}
    n = len(arr["Lap"])
    types = {"Lap": "i", "Gear": "i", "BrakeABSactive": "b"}
    out = {k: (types.get(k, "f"), arr[k]) for k in arr}
    out.update({"OnPitRoad": ("b", np.zeros(n, bool)), "PlayerTrackSurface": ("i", np.full(n, ibt.ON_TRACK)),
                "PlayerCarMyIncidentCount": ("i", np.zeros(n, int)), "IsOnTrack": ("b", np.ones(n, bool)),
                "SessionNum": ("i", np.zeros(n, int)), "LapDist": ("f", arr["LapDistPct"] * L)})
    ibt.write_fake(path, fi.session_text(session=session, **kw), out, tick_rate=rate)
    return path


RNG = np.random.default_rng(213)


def jit(p, scale=1.0, timing_only=False):
    """small lap-to-lap scatter so spreads are never zero. timing_only: scatter only the minimum speed (lap time)
    and the pedal pressure (no effect on time), so no driving difference goes with the quicker laps."""
    q = dict(p)
    if timing_only:
        q["vmin"] = p["vmin"] + RNG.normal(0, 0.3 * scale)
        q["peak"] = min(1.0, p["peak"] + RNG.normal(0, 0.02 * scale))
        return q
    q["bdist"] = p["bdist"] + RNG.normal(0, 1.5 * scale)
    q["vmin"] = p["vmin"] + RNG.normal(0, 0.1 * scale)
    q["pu"] = p["pu"] + RNG.normal(0, 1.5 * scale)
    q["peak"] = min(1.0, p["peak"] + RNG.normal(0, 0.01 * scale))
    q["re"] = p["re"] + RNG.normal(0, 1.0 * scale)
    return q


def laps_with(best_change: dict, n_best=4, n_typ=8, typ_change=None, other=None):
    """Corner 1 differs between best and typical laps; corner 2 is the same on every lap (a control)."""
    out = []
    for i in range(n_best + n_typ):
        c1 = dict(BASE, **(typ_change or {}))
        if i < n_best:
            c1.update(best_change)
        c2 = dict(BASE, **(other or {}))
        out.append([jit(c1), jit(c2)])
    order = RNG.permutation(len(out))       # best laps scattered through the session
    return [out[k] for k in order]


def run(name, lap_params, **kw):
    p = make_file(TMP / f"{name}.ibt", lap_params, **{k: v for k, v in kw.items() if k in ("wheels", "abs_ch", "steer")})
    rep = cc.analyze_files([p], **{k: v for k, v in kw.items() if k == "reference"})
    REPORTS.append(rep)
    return rep


REPORTS = []      # every report made here, checked at the end for sentence joins with " - "


def card(rep, cid="C1"):
    return next((c for c in rep["cards"] if c["id"] == cid), None)


def voice_ok(v):
    if not v or ":" not in v:
        return False
    n = cc._words(v.split(":", 1)[1])
    return 1 <= n <= 3 and cc._words(v) <= 4 and "," not in v      # 2.13 review: one idea, at most 4 words


# ---------------------------------------------------------------- 1. brake later
rep = run("later", laps_with({"bdist": 120.0, "vmin": 25.6}))
check(rep["ok"] and rep["laps"] == 12, f"12 clean laps read ({rep['message']})")
check([c["id"] for c in rep["corners"]] == ["C1", "C2"], f"two corners found by the engine's detector {rep['corners']}")
c = card(rep)
check(c is not None and c["rule"] == "brake_later", f"brake-later rule fires ({c and c['rule']})")
if c:
    check("brake about 20 m later" in c["how"].lower() and "marker 20 m past" in c["how"], f"how: {c['how']}")
    import re as _re
    mm = _re.search(r"\((\d+) m before the apex instead of (\d+) m\)", c["different"])
    check(mm is not None and int(mm.group(2)) - int(mm.group(1)) == 20, f"numbers both ways: {c['different']}")
    check(c["best_laps"] == 4 and c["typical_laps"] == 8 and "4 best laps vs 8 typical laps" in c["evidence"], "evidence counts")
    check(c["lost_s"] >= cc.MIN_LOST_S and c["confidence"] in ("clear", "likely"), f"lost {c['lost_s']} s, {c['confidence']}")
    check(c["voice"] == "T1: brake later" and voice_ok(c["voice"]), f"voice {c['voice']}")
    check(c["why"] and c["text"].startswith("T1: your best laps were about"), "why line and full text")
check(card(rep, "C2") is None, "the unchanged corner gets no card")

# ---------------------------------------------------------------- 2. lighter (ABS)
rep = run("lighter", laps_with({"peak": 0.80, "vmin": 26.0}, typ_change={"peak": 1.0, "rs": 90.0}))
c = card(rep)
check(c is not None and c["rule"] == "lighter", f"lighter-brake rule fires ({c and c['rule']})")
if c:
    check("brake at 80% instead of 100%" in c["different"], f"different: {c['different']}")
    check("about 80%" in c["how"] and "ABS starts working (about 85% on this car)" in c["how"], f"how: {c['how']}")
    check("ABS work" in c["why"], f"why: {c['why']}")
    check("ABS is working" in c["different"], "ABS share both ways when it differs")
    check(c["voice"].startswith("T1: lighter brake") and voice_ok(c["voice"]), f"voice {c['voice']}")
check(rep["method"]["abs"] == "BrakeABSactive" and rep["method"]["abs_onset_pct"] == 85.0,
      f"ABS from BrakeABSactive, onset {rep['method'].get('abs_onset_pct')}")
s = rep["summary"]
check(s and s["abs_share"] is not None and any("ABS" in x for x in s["lines"]), "summary talks about ABS")
check(s["full_share"] > 0.3, f"full-pedal share {s['full_share']}")

# same laps without an ABS channel: still lighter, no ABS words
rep = run("lighter_noabs", laps_with({"peak": 0.80, "vmin": 26.0}, typ_change={"peak": 1.0, "rs": 90.0}), abs_ch=False)
c = card(rep)
check(c is not None and c["rule"] == "lighter" and "ABS" not in c["how"] and "ABS" not in c["different"],
      "no ABS channel: lighter rule without ABS words")
check(any("no ABS channel" in x for x in rep["summary"]["lines"]), "summary says ABS can't be measured")

# ---------------------------------------------------------------- 3. trail braking instead of coasting
rep = run("trail", laps_with({"re": -5.0, "vmin": 25.8}, typ_change={"re": 45.0}))
c = card(rep)
check(c is not None and c["rule"] == "trail", f"trail-braking rule fires ({c and c['rule']})")
if c:
    check("brake on into the turn" in c["different"] and "instead of coasting" in c["different"], f"different: {c['different']}")
    check("as you turn in, and let it go as you reach the apex" in c["how"], f"how: {c['how']}")
    check(voice_ok(c["voice"]) and c["voice"] == "T1: trail into it", f"voice {c['voice']}")
    nb = c["numbers"]
    check(nb["coast_s"]["typical"] - nb["coast_s"]["best"] >= cc.R_COAST_S, f"coast numbers {nb['coast_s']}")
    check(nb["trail_pct"]["best"] >= 5 and nb["trail_pct"]["typical"] < 5, f"trail numbers {nb['trail_pct']}")
check(rep["summary"]["coast_s_per_lap"] > 0, "coasting per lap in the summary")

# coasting without trail braking on the best laps -> the coast rule (no trail words)
rep = run("coast", laps_with({"pu": -25.0, "vmin": 26.0}, typ_change={"re": 40.0, "pu": 20.0}))
c = card(rep)
check(c is not None and c["rule"] in ("coast", "throttle"), f"coast/throttle rule fires ({c and c['rule']})")
if c:
    words = (c["rule"], c["secondary"])
    check("coast" in words or "throttle" in words, f"rules {words}")
    check("hold" not in c["how"].lower() or c["rule"] == "trail", "no trail-brake advice when best laps don't trail")

# ---------------------------------------------------------------- 4. throttle earlier (maintenance throttle, no coasting)
rep = run("throttle", laps_with({"pu": 5.0}, typ_change={"pu": 35.0, "maint": 0.1}, other={"maint": 0.1}))
c = card(rep)
check(c is not None and c["rule"] == "throttle", f"throttle-earlier rule fires ({c and c['rule']})")
if c:
    check("about 30 m earlier" in c["how"] and "at the apex" in c["how"], f"how: {c['how']}")
    check("throttle earlier" in c["voice"] and voice_ok(c["voice"]), f"voice {c['voice']}")
    check(c["numbers"]["exit_kmh"]["best"] > c["numbers"]["exit_kmh"]["typical"] and "on the way out" in c["gets"],
          f"gets: {c['gets']}")

# ---------------------------------------------------------------- 5. carry speed (same entry, earlier release)
rep = run("carry", laps_with({"vmin": 27.0, "re": 50.0}, typ_change={"vmin": 25.0, "re": 40.0}))
c = card(rep)
check(c is not None and c["rule"] == "carry", f"carry-speed rule fires ({c and c['rule']})")
if c:
    check("arrive at the same speed" in c["different"] and "km/h more at the slowest point" in c["different"],
          f"different: {c['different']}")
    check("come off the brake a little earlier" in c["how"], f"how: {c['how']}")
    check("more through the middle" in c["gets"], f"gets: {c['gets']}")

# ---------------------------------------------------------------- 6. wheelspin and lock-up
rep = run("spin", laps_with({"vmin": 25.6, "spin": False}, typ_change={"spin": True}))
c = card(rep)
check(c is not None and "spin" in (c["rule"], c["secondary"]), f"wheelspin rule fires ({c and (c['rule'], c['secondary'])})")
if c:
    check("wheel speeds" in c["different"] and "squeeze the throttle" in c["how"].lower(), f"spin wording: {c['different']}")
check(rep["method"]["wheelspin"] == "wheel speeds", "wheelspin measured from wheel speeds")
rep = run("spin_rpm", laps_with({"vmin": 25.6, "spin": False}, typ_change={"spin": True}), wheels=False)
check(rep["method"]["wheelspin"] in ("rpm", None) and rep["method"]["lockup"] is None,
      f"no wheel speeds, ABS never on (pedal under 85%): RPM method for spin, no lock-up method "
      f"({rep['method'].get('wheelspin')}, {rep['method'].get('lockup')})")
rep = run("lock", laps_with({"vmin": 25.6, "lock": False}, typ_change={"lock": True}))
c = card(rep)
check(c is not None and "lock" in (c["rule"], c["secondary"]), f"lock-up rule fires ({c and (c['rule'], c['secondary'])})")
check(rep["summary"]["lockups"] and rep["summary"]["lockups"] >= 8, f"lock-ups counted ({rep['summary'].get('lockups')})")
check(rep["summary"]["lock_method"] == "wheel speeds", "lock-up method named")

# ---------------------------------------------------------------- 7. honesty gates
rep = run("few", laps_with({"bdist": 120.0, "vmin": 25.6}, n_best=2, n_typ=3))
check(not rep["ok"] and rep["cards"] == [] and "at least 6" in rep["message"], f"5 laps: no cards ({rep['message']})")
rows = [{"t": 1.0 + i * 0.01, "brake_m": -100.0} for i in range(7)]
best, typ = cc.split(rows)
check(len(best) == 3 and len(typ) == 4, "best = fastest third, at least 3; typical = the rest")
check(cc.diff_test([1, 2], [1, 2, 3], "brake_m") is None, "fewer than 3 in a group: no test")
# noise: every lap driven the same (only scatter) -> no cards
noise = [[jit(BASE, 1.0, True), jit(BASE, 1.0, True)] for _ in range(12)]
rep = run("noise", noise)
check(rep["ok"] and rep["cards"] == [], f"pure scatter: no cards ({[c['rule'] for c in rep['cards']]})")
check(all("no clear pattern yet" in u["text"] for u in rep["unclear"]), "time lost but no pattern: 'no clear pattern yet'")
# tiny real difference inside the noise: best brake 4 m later -> below the 10 m rule
rep = run("small", laps_with({"bdist": 136.0}))
check(card(rep) is None or card(rep)["rule"] != "brake_later", "a 4 m brake-point difference is not a card")
# time lost under 0.05 s -> fine, no card
quiet = [[dict(BASE), dict(BASE)] for _ in range(8)]
rep = run("quiet", quiet)
check(rep["ok"] and rep["cards"] == [] and rep["unclear"] == [] and len(rep["fine"]) == 2, "identical laps: both corners fine")

# ---------------------------------------------------------------- 8. reference lap (clearly labelled)
own = laps_with({}, n_best=4, n_typ=6)
p_own = make_file(TMP / "own.ibt", own)
p_ref = make_file(TMP / "ref.ibt", [[dict(BASE, bdist=110.0, vmin=26.0), dict(BASE)]] * 2)
with ibt.open_ibt(p_ref) as ib:
    x = next(y for y in ibt.laps(ib) if y["clean"])
    raw = cc._raw({nm: ib.col(nm) for nm in cc.CHANNELS if ib.has(nm)}, x["start"], x["end"])
ref = cc.lap_from_raw(raw, x["time"], L, label="Teammate Alex")
check(ref is not None and ref["label"] == "Teammate Alex", "reference lap from a raw lap")
rep = cc.analyze_files([p_own], reference=ref)
rc = next((c for c in rep["reference_cards"] if c["id"] == "C1"), None)
check(rep["reference"] == "Teammate Alex" and rc is not None, "reference cards made")
if rc:
    check(rc["reference"] == "Teammate Alex" and "Teammate Alex" in rc["text"] and rc["confidence"] == "thin",
          f"reference card labelled and thin: {rc['text'][:90]}")
    check(rc["rule"] == "brake_later", f"reference rule {rc['rule']}")
check(all(c["reference"] is None for c in rep["cards"]), "own cards never mention the reference")

# ---------------------------------------------------------------- 9. shapes, JSON, voice, bad input
rep = run("json", laps_with({"bdist": 120.0, "peak": 0.80, "re": -5.0, "vmin": 26.5}, typ_change={"peak": 1.0}))
try:
    json.dumps(rep, allow_nan=False)
    ok = True
except (TypeError, ValueError) as e:
    ok = False
    print(e)
check(ok, "report is JSON-serialisable (no numpy types, no NaN)")
keys = {"corner", "id", "apex_m", "lost_s", "rule", "secondary", "different", "how", "gets", "why", "evidence",
        "best_laps", "typical_laps", "confidence", "confidence_rule", "voice", "numbers", "text", "reference"}
check(rep["cards"] and all(keys <= set(c) for c in rep["cards"]), "card fields")
check(all(voice_ok(c["voice"]) for c in rep["cards"]), f"voice phrases one idea, 4 words: {[c['voice'] for c in rep['cards']]}")
vp = cc.voice_phrases(rep)
check(vp and all(k.startswith("C") and v.startswith("T") for k, v in vp.items()), f"voice_phrases {vp}")
for rule, w in cc.VOICE.items():
    check(1 <= cc._words(w) <= 3 and "," not in w, f"voice word count for {rule}: '{w}'")

junk = TMP / "junk.ibt"
junk.write_bytes(b"MZ" + b"\0" * 500)
for what, fn in (("missing file", lambda: cc.analyze_files([TMP / "nope.ibt"])),
                 ("junk file", lambda: cc.analyze_files([junk])),
                 ("None", lambda: cc.analyze_files(None)),
                 ("laps None", lambda: cc.analyze_laps(None)),
                 ("bad laps", lambda: cc.analyze_laps([{"R": {}, "time": 90}, {"x": 1}, None, 5])),
                 ("phases on junk", lambda: cc.phases({}, {"start_m": 0}) or {"ok": False}),
                 ("reference junk", lambda: cc.lap_from_raw({}, None, None) or {"ok": False}),
                 ("braking_summary empty", lambda: cc.braking_summary([], []))):
    try:
        r = fn()
        check(isinstance(r, dict) and r.get("ok") is not True or what == "braking_summary empty", f"bad input never raises: {what}")
        json.dumps(r)
    except Exception as e:  # noqa: BLE001
        check(False, f"bad input never raises: {what} ({type(e).__name__}: {e})")
rep = cc.analyze_files([junk, p_own])
check(rep["info"]["skipped"] and rep["laps"] == 10, "a junk file next to a good one is skipped, the good one used")

# mixed car files: the second car is skipped
p_other = make_file(TMP / "other.ibt", own, car="Ford Mustang GT4", car_path="fordmustanggt4")
rep = cc.analyze_files([p_own, p_other])
check(any(s["why"] == "a different car or track" for s in rep["info"]["skipped"]), "a different car is skipped")

# ---------------------------------------------------------------- 10. 2.13 review: honest statistics
# (a) noise passes no more than about 5% of the time: random rows, the full card decision (rules + permutation test)
def noise_rows(rng, n):
    rows = []
    for _ in range(n):
        rows.append({"t": 3.0 + rng.normal(0, 0.08), "brake_m": -140 + rng.normal(0, 6), "peak_pct": min(100, 92 + rng.normal(0, 6)),
                     "abs_share": max(0.0, 0.35 + rng.normal(0, 0.15)), "coast_s": max(0.0, 0.3 + rng.normal(0, 0.15)),
                     "trail_pct": max(0.0, 20 + rng.normal(0, 10)), "release_s": max(0.0, 0.5 + rng.normal(0, 0.2)),
                     "pickup_m": 10 + rng.normal(0, 12), "min_kmh": 110 + rng.normal(0, 3), "entry_kmh": 190 + rng.normal(0, 2),
                     "exit_kmh": 150 + rng.normal(0, 2), "spin_share": max(0.0, 0.1 + rng.normal(0, 0.08)),
                     "lock_share": max(0.0, 0.05 + rng.normal(0, 0.05))})
    return rows


def decide(rows, flags, seed=0):
    best, typ = cc.split(rows)
    D = {m: cc.diff_test([r.get(m) for r in best], [r.get(m) for r in typ], m) for m in cc.METRICS}
    fired = cc._rules(D, dict(flags, n_laps=len(rows)))
    if not fired:
        return [], fired
    pt = cc.perm_test(rows, flags, n_perm=200, seed=seed)
    return [r for r, _m in fired if pt["p"].get(r, 1.0) < cc.P_LIKELY], fired


nrng = np.random.default_rng(7)
fl = {"abs": True}
hits = fires = 0
TRIALS = 150
for i in range(TRIALS):
    ok_, fired = decide(noise_rows(nrng, 14), fl, seed=i)
    hits += bool(ok_)
    fires += bool(fired)
check(fires / TRIALS > 0.15, f"pure noise: the threshold rules alone fire often ({fires}/{TRIALS}), the old problem")
check(hits / TRIALS <= 0.08, f"pure noise: a card in at most about 5% of corners after the permutation test ({hits}/{TRIALS})")

# (b) the vectorised test agrees with diff_test on the real split, and a strong pattern passes
rows = noise_rows(np.random.default_rng(3), 12)
order = np.argsort([r["t"] for r in rows], kind="stable")
best, typ = cc.split(rows)
k = len(best)
for m in cc.RULE_METRICS:
    one = cc.diff_test([r.get(m) for r in best], [r.get(m) for r in typ], m)
    many = cc._diff_many(cc._col(rows, m), order[None, :k], order[None, k:], m)
    check(one is not None and abs(one["diff"] - many["diff"][0]) < 1e-9 and abs(one["z"] - many["z"][0]) < 1e-9,
          f"_diff_many matches diff_test for {m}")
strong = noise_rows(np.random.default_rng(4), 12)
for r in sorted(strong, key=lambda r: r["t"])[:4]:
    r["brake_m"] += 20.0                        # the four fastest brake 20 m later
pt = cc.perm_test(strong, fl, n_perm=400)
check(pt["p"].get("brake_later", 1) < cc.P_LIKELY, f"a real 20 m brake-point pattern passes (p {pt['p']})")
check(abs(cc.perm_test(strong, fl, n_perm=400)["p"].get("brake_later", 1) - pt["p"]["brake_later"]) < 1e-12,
      "fixed seed: the same laps always give the same answer")

# (c) brake-point advice needs MIN_BRAKE_LAPS laps
few9 = noise_rows(np.random.default_rng(5), 9)
for r in sorted(few9, key=lambda r: r["t"])[:3]:
    r["brake_m"] += 25.0
b9, t9 = cc.split(few9)
D9 = {m: cc.diff_test([r.get(m) for r in b9], [r.get(m) for r in t9], m) for m in cc.METRICS}
check(any(r == "brake_later" for r, _m in cc._rules(D9, {})) and
      not any(r == "brake_later" for r, _m in cc._rules(D9, {"n_laps": 9})),
      f"9 laps: no brake-point advice (needs {cc.MIN_BRAKE_LAPS})")

# (d) coasting less is only called trail braking when the trail number itself passes
def dd(diff, z, typical=0.0):
    return {"best": typical + diff, "typical": typical, "diff": diff, "z": z, "se": 1.0, "n_best": 4, "n_typical": 8}
Dt = {"coast_s": dd(-0.5, 4.0, 1.0), "trail_pct": dd(8.0, 1.0, 2.0)}
check([r for r, _m in cc._rules(Dt, {})] == ["coast"], f"trail number inside the noise: coast, not trail ({cc._rules(Dt, {})})")
Dt["trail_pct"] = dd(8.0, 3.0, 2.0)
check([r for r, _m in cc._rules(Dt, {})] == ["trail"], f"trail number passes: trail ({cc._rules(Dt, {})})")

# (e) time: corners never add up past the lap-level gap; no "s a lap" promises; speeds only when they passed
for r in REPORTS:
    if r.get("ok") and r.get("lap_gap_s") is not None:
        tot = sum(x["lost_s"] for x in r["cards"] + r["unclear"] + r["fine"])
        check(tot <= r["lap_gap_s"] + 0.01, f"corner times add up to {tot:.3f} s, lap gap {r['lap_gap_s']}")
        break
cards_all = [c for r in REPORTS for c in r.get("cards") or []]
check(cards_all and not any("s a lap" in (c["text"] + c["gets"]) for c in cards_all), "no 's a lap' promises in cards")
for c in cards_all:
    nb = c["numbers"]
    if "through the middle" in c["gets"]:
        check(nb["min_kmh"]["z"] >= cc.Z_LIKELY, f"{c['corner']}: middle speed named only when it passed ({nb['min_kmh']})")
quiet_rep = next(r for r in REPORTS if r["fine"] and not r["cards"] and not r["unclear"])
check(quiet_rep["fine"], "a corner can be 'close to your best'")
check(all(c.get("p_value") is not None and c["p_value"] < cc.P_LIKELY for c in cards_all), "every card passed the permutation test")

# (f) ABS: channel there but never on (a car without ABS) -> no-ABS words
no_abs = laps_with({"peak": 0.80, "vmin": 26.0}, typ_change={"peak": 1.0, "rs": 90.0, "abs_at": 2.0},
                   other={"abs_at": 2.0})
for lap in no_abs:
    for cp in lap:
        cp["abs_at"] = 2.0
rep = run("abs_never", no_abs)
check(rep["method"]["abs"] is None and rep["info"]["abs_channel"] and not rep["info"]["abs_active"],
      "ABS channel present but never on: no ABS")
check(any("ABS never came on" in x and "just before the wheels lock" in x for x in rep["summary"]["lines"]),
      f"summary says this car has no ABS here: {rep['summary']['lines'][:3]}")
check(not any("ABS" in (c["how"] + c["different"] + c["why"]) for c in rep["cards"]), "no ABS words in the cards")

# (g) like with like: race laps and practice laps are grouped; the evidence names the laps used
pr = make_file(TMP / "grp_practice.ibt", laps_with({"bdist": 120.0, "vmin": 25.6}), session="Practice")
rc_ = make_file(TMP / "grp_race.ibt", laps_with({}, n_best=1, n_typ=4), session="Race")
rep = cc.analyze_files([pr, rc_])
check([g["kind"] for g in rep["groups"]] == ["practice", "race"] and [g["used"] for g in rep["groups"]] == [True, False],
      f"groups {rep['groups']}")
c = card(rep)
check(c is not None and "practice and qualifying laps only" in c["evidence"] and rep["laps"] == 12,
      f"evidence names the laps used: {c and c['evidence']} ({rep['laps']} laps)")
check("practice and qualifying laps only" in rep["message"], rep["message"])
# opposite advice in two groups -> no card for that corner
res_a = {"cards": [dict(c, rule="brake_later", group="race laps only", p_value=0.01)], "unclear": [], "fine": [],
         "per_corner": {"C1": {}}}
res_b = {"cards": [dict(c, rule="brake_earlier", group="practice and qualifying laps only", p_value=0.02)],
         "unclear": [], "fine": [], "per_corner": {"C1": {}}}
mg = cc._merge([res_a, res_b])
check(not mg["cards"] and mg["unclear"] and "disagree" in mg["unclear"][0]["text"], f"opposite advice: no card ({mg['unclear']})")
mg = cc._merge([res_a, dict(res_b, cards=[])])
check(len(mg["cards"]) == 1, "a card from one group stands when the other has none")
# mixed kinds, but no group big enough
rep = cc.analyze_laps([dict(x, session="Race" if i % 2 else "Practice") for i, x in enumerate(cc.load_laps(pr)[0][:10])])
check(not rep["ok"] and "like with like" in rep["message"], f"no group big enough: {rep['message']}")

# (h) a skipped file names the file, never its folder
bad = TMP / "sub folder" / "broken.ibt"
bad.parent.mkdir(exist_ok=True)
bad.write_bytes(b"\0" * 300)
rep = cc.analyze_files([pr, bad])
why = " ".join(x["why"] for x in rep["info"]["skipped"])
check(rep["info"]["skipped"] and str(bad.parent) not in why, f"skip reason without the folder: {why}")


# 2.13 review: card text is joined with a colon or a full stop, never " - " (and no long dashes)
def _texts(r):
    for c in (r.get("cards") or []) + (r.get("reference_cards") or []):
        yield from (c.get(k) or "" for k in ("different", "how", "gets", "why", "evidence", "text"))
    yield from (u.get("text") or "" for u in r.get("unclear") or [])
    yield from ((r.get("summary") or {}).get("lines") or [])
    yield r.get("message") or ""


texts = [t for r in REPORTS for t in _texts(r) if t]
check(len(texts) > 40, f"card texts gathered ({len(texts)})")
bad = [t for t in texts if " - " in t or "\u2014" in t or "\u2013" in t]
check(not bad, f"no ' - ' or long dashes in card text: {bad[:3]}")
few = cc.analyze_laps([])
check(" - " not in (few.get("message") or ""), f"too-few-laps message: {few.get('message')}")
print("FAILED" if fails else "ALL OK", fails)
sys.exit(1 if fails else 0)
