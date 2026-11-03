"""2.15: car balance map (debrief/balance.py). Run: python tests/test_215_balance.py

Synthetic laps are built from a known car: steering = G x curvature + K x lateral g (+ noise), and a known push
or rotation is added on purpose where a test wants one. Checks: a known push / rotation is found in the right
corner and phase, pure noise stays neutral or "not clear yet", the false-positive rate of the whole map on pure
noise is at most 5 %, countersteer is counted, kerb strikes are ignored, setup ideas use the car's own keys, and
one fake .ibt goes through the file reader. No network, temp folders only."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
TMP = Path(tempfile.mkdtemp(prefix="debrief-215-balance-"))

import fake_ibt as fi  # noqa: E402
from debrief import balance as bl, ibt  # noqa: E402

fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


def no_long_dash(obj, where):
    txt = json.dumps(obj, ensure_ascii=False, default=str)
    check("—" not in txt and "–" not in txt, f"{where}: no long dashes")


# ---------------------------------------------------------------- synthetic laps
L = 2400.0
RATE = 60
G_TRUE = 2800.0          # steering degrees per 1/m (a ratio of ~18 on a 2.7 m car)
K_TRUE = 2.0             # degrees per g
VMAX = 52.0
# apex distance, slowest speed (m/s), turn direction
CORNERS = [(300.0, 20.0, 1), (700.0, 32.0, -1), (1100.0, 24.0, 1), (1500.0, 28.0, -1), (1950.0, 22.0, 1)]
WIDTH = 70.0             # curvature ramps over this distance either side of the apex
LAT_G = 1.25             # lateral g at the slowest point
# flat-out sweepers (no braking): long stretches of gentle cornering, where the car's normal steering is learned
SWEEPERS = [(500.0, 900.0, -1), (1300.0, 1300.0, 1), (1720.0, 800.0, -1), (2200.0, 1100.0, 1)]   # (middle, radius, dir)


def _profiles(d):
    v = np.full_like(d, VMAX)
    kap = np.zeros_like(d)
    brake = np.zeros_like(d)
    thr = np.ones_like(d) * 100.0
    for A, vmin, s in CORNERS:
        R = vmin ** 2 / (LAT_G * 9.81)
        rel = d - A
        shape = np.clip(1 - np.abs(rel) / WIDTH, 0, 1)
        kap += s * shape / R
        bz = (rel > -160) & (rel <= 0)
        vin = np.sqrt(vmin ** 2 + (VMAX ** 2 - vmin ** 2) * np.clip(-rel / 160.0, 0, 1))
        vout = np.sqrt(vmin ** 2 + 2 * 4.0 * np.clip(rel, 0, None))
        v = np.minimum(v, np.where(rel <= 0, np.where(rel > -160, vin, VMAX), vout))
        brake[(rel > -160) & (rel < -25)] = 80.0
        brake[(rel >= -25) & (rel < -12)] = 30.0
        thr[bz | ((rel > -12) & (rel < 8))] = 0.0
        thr[(rel >= 8) & (rel < 120)] = np.minimum(thr[(rel >= 8) & (rel < 120)], 30 + (rel[(rel >= 8) & (rel < 120)] - 8))
    for M, R, s in SWEEPERS:
        kap += s * np.clip(1 - np.abs(d - M) / 110.0, 0, 1) ** 0.5 / R
    v = np.maximum(v, 5.0)
    return v, kap, brake, thr


D = np.arange(0.0, L, 0.25)
V0, KAP0, BRK0, THR0 = _profiles(D)


def make_lap(rng, extra=None, scale=1.0, lap_noise=0.5, corner_noise=1.5, white=2.0, countersteer=False, kerb=False):
    """One lap as balance.load_laps gives it. extra(d, kap, ay) -> steering degrees added (turn direction sign)."""
    v_d = V0 * scale * (1 + 0.01 * rng.standard_normal())
    t_d = np.concatenate([[0.0], np.cumsum(np.diff(D) / v_d[:-1])])
    T = float(t_d[-1] + 0.25 / v_d[-1])
    t = np.arange(0.0, T, 1.0 / RATE)
    d = np.interp(t, t_d, D)
    v = np.interp(d, D, v_d)
    kap = np.interp(d, D, KAP0)
    ay = v ** 2 * kap
    r = v * kap
    sw = G_TRUE * kap + K_TRUE * ay / 9.81
    n = len(t)
    # noise: white, a slow wander over the lap, and an offset per corner (all symmetric)
    wander = np.cumsum(rng.standard_normal(n))
    wander = bl.lowpass(wander - np.linspace(0, wander[-1], n), RATE, 0.3)
    wander *= lap_noise / max(1e-9, np.std(wander))
    off = np.zeros(n)
    for A, _vm, s in CORNERS:
        off[np.abs(d - A) < 150] += s * corner_noise * rng.standard_normal()
    sw = sw + wander + off + white * rng.standard_normal(n)
    if extra is not None:
        sw = sw + extra(d, kap, ay)
    vert = 9.81 + 0.3 * rng.standard_normal(n)
    if countersteer:
        A = CORNERS[1][0]
        m = (d > A + 20) & (d < A + 45)                    # exit of T2: steering flicks the other way
        sw[m] = -np.sign(kap[m]) * 25.0
    if kerb:
        A = CORNERS[3][0]
        k = int(np.argmin(np.abs(d - (A + 2))))
        sw[k:k + 5] += np.sign(kap[k]) * 400.0             # a big spike in the steering trace ...
        vert[k - 2:k + 6] += 25.0                          # ... at a kerb strike
    return {"file": "synthetic", "lap": 0, "time": T, "rate": RATE, "pct": (d / L) % 1.0, "v": v, "sw": sw, "r": r,
            "ay": ay + 0.2 * rng.standard_normal(n), "b": np.interp(d, D, BRK0), "th": np.interp(d, D, THR0),
            "vert": vert, "shock": np.zeros(n)}


def session(seed, n_laps=8, **kw):
    rng = np.random.default_rng(seed)
    laps = [make_lap(rng, **kw) for _ in range(n_laps)]
    for i, x in enumerate(laps):
        x["lap"] = i + 1
    return laps


INFO = {"length_m": L, "car_path": "mclaren570sgt4", "car": "McLaren 570S GT4", "track": "Test"}


def cell(rep, corner, phase):
    return next((c for c in rep["cells"] if c["corner"] == corner and c["phase"] == phase), None)


# ---------------------------------------------------------------- 0. the filter
x = np.sin(2 * np.pi * 1.0 * np.arange(600) / 60) + np.sin(2 * np.pi * 15 * np.arange(600) / 60)
y = bl.lowpass(x, 60, 4.0)
check(np.max(np.abs(y[60:-60] - np.sin(2 * np.pi * np.arange(600) / 60)[60:-60])) < 0.05,
      "low-pass keeps 1 Hz and removes 15 Hz, with no delay")
check(abs(bl.lowpass(np.ones(50), 60)[25] - 1.0) < 1e-9, "low-pass keeps a constant")
lo, hi = bl._median_ci([1, 2, 3, 4, 5, 6])
check((lo, hi) == (1.0, 6.0), "order-statistic interval of 6 values is min..max (coverage 97%)")

# ---------------------------------------------------------------- 1. a known push and a known rotation
T3 = CORNERS[2][0]
T5 = CORNERS[4][0]


def push_t3_rot_t5(d, kap, ay):
    out = np.zeros_like(d)
    m = np.abs(d - T3) < 18
    out[m] += np.sign(kap[m]) * 12.0                       # 12 degrees more steering mid-corner at T3
    m = (d > T5 - 70) & (d < T5 - 18)
    out[m] -= np.sign(kap[m]) * 9.0                        # 9 degrees less on the way into T5
    return out


rep = bl.analyze_laps(session(1, n_laps=10, extra=push_t3_rot_t5), info=INFO)
check(rep["ok"], f"analysis runs ({rep['message']})")
check(len(rep["corners"]) == 5, f"5 corners found ({len(rep['corners'])})")
c = cell(rep, "T3", "mid")
check(c is not None and c["label"] == "push", f"T3 middle: pushes ({c and c['label']}, {c and c['median_deg']})")
c = cell(rep, "T5", "entry")
check(c is not None and c["label"] == "rotate", f"T5 entry: rotates ({c and c['label']}, {c and c['median_deg']})")
others = [x for x in rep["findings"] if (x["corner"], x["phase"]) not in (("T3", "mid"), ("T5", "entry"))]
check(not others, f"nothing else is called a push or rotation ({[(x['corner'], x['phase']) for x in others]})")
G = rep["car_fit"]["G"]
check(abs(G - G_TRUE) / G_TRUE < 0.08, f"car's steering learned from the laps ({G} vs {G_TRUE})")
check(rep["car_fit"]["ratio_estimate"] is not None, "with a sourced wheelbase, a steering ratio is estimated")
check("pushes in the middle of slow corners (T3)" in rep["summary"], f"summary names it: {rep['summary']}")
check("rotates on the way into slow corners (T5)" in rep["summary"], "summary names the rotation")
rep20 = bl.analyze_laps(session(11, n_laps=20, extra=push_t3_rot_t5), info=INFO)
check(any(x["label"] == "neutral" for x in rep20["cells"]),
      f"with 20 laps some places are positively neutral ({sum(x['label'] == 'neutral' for x in rep20['cells'])})")
check(any(x["label"] == "unclear" for x in rep["cells"]), "with 10 laps the fitting margin leaves places not clear yet")
no_long_dash(rep, "report")

# ---------------------------------------------------------------- 2. pure noise
rep0 = bl.analyze_laps(session(2, n_laps=10), info=INFO)
check(rep0["ok"] and not rep0["findings"], f"noise only: no push or rotation ({rep0['summary']})")
check(all(x["label"] in ("neutral", "unclear") for x in rep0["cells"]), "noise only: every place neutral or unclear")

# too few laps: no claims
rep_few = bl.analyze_laps(session(3, n_laps=4, extra=push_t3_rot_t5), info=INFO)
check(not rep_few["ok"] and not rep_few["findings"] and "at least 6" in rep_few["message"],
      f"4 laps: says it needs more ({rep_few['message']})")

# ---------------------------------------------------------------- 3. false-positive rate
# (a) the family-wise test alone, on pure noise matrices of the real shape (laps x places)
rng = np.random.default_rng(9)
fp = 0
N = 400
for k in range(N):
    n_l = int(rng.integers(6, 16))
    V = rng.standard_normal((n_l, 15)) * rng.uniform(0.5, 6) + rng.standard_normal((n_l, 1)) * 2.0
    V[rng.random(V.shape) < 0.1] = np.nan
    _t, p = bl.maxt_pvalues(V)
    fp += int((p < bl.ALPHA).any())
rate = fp / N
print(f"     family-wise test on noise: {fp}/{N} sessions with any false call = {rate:.1%}")
check(rate <= 0.05 + 2 * np.sqrt(0.05 * 0.95 / N), f"sign-flip max-t test: false-positive rate {rate:.1%} (target 5%)")
# (b) whole analysis on synthetic noise sessions (no push anywhere): every other one with heavy lap-to-lap noise
fp = 0
N2 = 40
for k in range(N2):
    noise = {"lap_noise": 3.0, "corner_noise": 4.0} if k % 2 == 0 else {}
    r = bl.analyze_laps(session(100 + k, n_laps=int(6 + k % 6), **noise), info=INFO)
    fp += int(bool(r["findings"]))
rate2 = fp / N2
print(f"     whole map on noise sessions: {fp}/{N2} with any false push/rotate = {rate2:.1%}")
check(rate2 <= 0.05, f"whole balance map: false-positive rate {rate2:.1%} (target 5%)")
# (c) shuffled real-shaped signal: the T3 push, but with each lap's sign flipped at random -> no claim allowed
rngs = np.random.default_rng(77)
fp = 0
N3 = 20
for k in range(N3):
    flips = rngs.choice((1.0, -1.0), size=8)
    laps = []
    rr = np.random.default_rng(500 + k)
    for i in range(8):
        f = flips[i]
        laps.append(make_lap(rr, extra=lambda d, kap, ay, f=f: f * np.sign(kap) * 6.0 * (np.abs(d - T3) < 18)))
    r = bl.analyze_laps(laps, info=INFO)
    fp += int(any(x["corner"] == "T3" and x["phase"] == "mid" for x in r["findings"]) and abs(flips.sum()) < 8)
rate3 = fp / N3
print(f"     shuffled-sign sessions: {fp}/{N3} with a false call at T3 = {rate3:.1%}")
check(rate3 <= 0.10, f"shuffled signs: no steady push claimed ({rate3:.1%})")

# ---------------------------------------------------------------- 4. countersteer
repc = bl.analyze_laps(session(4, n_laps=8, countersteer=True), info=INFO)
cs = repc["countersteer"]["by_corner"]
check("T2" in cs and cs["T2"]["laps"] == 8, f"countersteer counted at T2 on every lap ({cs})")
check(cs.get("T2", {}).get("phases", {}).get("exit", 0) >= 1, "countersteer placed on the way out")
check(not [k for k in cs if k != "T2"], "no countersteer anywhere else")
check("countersteer" in repc["summary"] and "T2" in repc["summary"], "summary mentions it")
check(not bl.analyze_laps(session(2, n_laps=8), info=INFO)["countersteer"]["events"], "noise: no countersteer")

# ---------------------------------------------------------------- 5. kerb strikes are ignored
repk = bl.analyze_laps(session(5, n_laps=8, kerb=True), info=INFO)
c = cell(repk, "T4", "mid")
check(c is not None and c["label"] != "push", f"a steering spike at a kerb strike is ignored ({c and c['label']})")
m = bl.kerb_mask(np.r_[np.full(100, 9.81), [40.0], np.full(100, 9.81)], None, 60)
check(m[100] and m[95] and not m[80], "kerb mask covers the strike and 0.15 s either side")
lap_nokerb = session(5, n_laps=8, kerb=True)
for x in lap_nokerb:
    x["vert"] = np.full(len(x["v"]), 9.81)                 # same spike, but no kerb in the vertical channel
repn = bl.analyze_laps(lap_nokerb, info=INFO)
cn = cell(repn, "T4", "mid")
check(cn is not None and (cn["median_deg"] or 0) > (c["median_deg"] or 0), "without the kerb channel the spike shows")

# ---------------------------------------------------------------- 6. setup ideas
setup = fi.SETUP_GT4
ideas = bl.setup_ideas(rep, setup)
check(ideas and ideas[0]["first"], "setup ideas made, the first marked")
mid = next((i for i in ideas if i["phase"] == "mid" and i["label"] == "push"), None)
check(mid is not None and mid["change"]["text"].startswith("Front anti-roll bar 2 → 1"),
      f"mid-corner push in a slow corner: front bar one step softer ({mid and mid['change']['text']})")
check(mid is not None and "Setup Lab" in mid["text"] and "Change only this" in mid["text"], "one change, then compare")
ent = next((i for i in ideas if i["phase"] == "entry"), None)
check(ent is not None and ent["change"] is not None and "Rear anti-roll bar" in ent["change"]["text"],
      f"entry rotation without a brake-bias setting: rear bar ({ent and ent['change']['text']})")
s2 = json.loads(json.dumps(setup))
s2["Chassis"]["FrontBrakes"]["BrakePressureBias"] = "58.7%"
ent2 = next(i for i in bl.setup_ideas(rep, s2) if i["phase"] == "entry")
check(ent2["change"]["text"].startswith("Brake bias 58.7 → 59.2"), f"with brake bias: forward a little ({ent2['change']['text']})")
fixed = bl.setup_ideas(rep, setup, series_setup="fixed")
check(len(fixed) == 1 and "fixed setup" in fixed[0]["text"] and fixed[0]["change"] is None, "fixed series: no changes")
s3 = json.loads(json.dumps(setup))
s3["Chassis"]["FrontBrakes"]["ArbSetting"] = "1"
mid3 = next(i for i in bl.setup_ideas(rep, s3) if i["phase"] == "mid")
check(mid3["change"]["text"].startswith("Rear anti-roll bar 1 → 2"), f"front bar already at 1: rear bar stiffer instead ({mid3['change']['text']})")
check(bl.setup_ideas(rep, {}) and bl.setup_ideas(rep, {})[0]["change"] is None, "no setup keys: plain words, no numbers")
check(bl.setup_ideas(None, None) == [], "bad input: no ideas, no crash")
no_long_dash(ideas, "ideas")

# ---------------------------------------------------------------- 7. through an .ibt file
def write_ibt(path, laps):
    cols = {k: [] for k in ("Lap", "LapDistPct", "LapLastLapTime", "Speed", "SteeringWheelAngle", "YawRate",
                            "LatAccel", "Brake", "Throttle", "VertAccel")}
    n0 = 120
    cols["Lap"] += [0] * n0
    cols["LapDistPct"] += list(np.linspace(0.95, 0.999, n0))
    cols["LapLastLapTime"] += [-1.0] * n0
    for k in ("Speed",):
        cols[k] += [VMAX] * n0
    for k in ("SteeringWheelAngle", "YawRate", "LatAccel", "Brake"):
        cols[k] += [0.0] * n0
    cols["Throttle"] += [1.0] * n0
    cols["VertAccel"] += [9.81] * n0
    shown = -1.0
    for i, x in enumerate(laps):
        n = len(x["v"])
        cols["Lap"] += [i + 1] * n
        cols["LapDistPct"] += list(x["pct"])
        cols["LapLastLapTime"] += [shown] * n
        shown = x["time"]
        cols["Speed"] += list(x["v"])
        cols["SteeringWheelAngle"] += list(x["sw"] / 57.2958)
        cols["YawRate"] += list(x["r"])
        cols["LatAccel"] += list(x["ay"])
        cols["Brake"] += list(x["b"] / 100)
        cols["Throttle"] += list(x["th"] / 100)
        cols["VertAccel"] += list(x["vert"])
    tail = 300
    cols["Lap"] += [len(laps) + 1] * tail
    cols["LapDistPct"] += list(np.linspace(0, 0.05, tail))
    cols["LapLastLapTime"] += [shown] * tail
    for k in ("Speed",):
        cols[k] += [VMAX] * tail
    for k in ("SteeringWheelAngle", "YawRate", "LatAccel", "Brake"):
        cols[k] += [0.0] * tail
    cols["Throttle"] += [1.0] * tail
    cols["VertAccel"] += [9.81] * tail
    n = len(cols["Lap"])
    cols2 = {k: ("i" if k == "Lap" else "f", np.asarray(v)) for k, v in cols.items()}
    cols2["SessionNum"] = ("i", np.zeros(n, np.int32))
    text = fi.session_text().replace("TrackLength: 3.98 km", f"TrackLength: {L / 1000:.2f} km")
    ibt.write_fake(path, text, cols2, tick_rate=RATE)
    return path


f1 = write_ibt(TMP / "mclaren570sgt4_zolder gp 2026-11-03 10-00-00.ibt", session(1, n_laps=10, extra=push_t3_rot_t5))
laps, info = bl.load_laps([f1])
check(len(laps) >= 8, f"file reader: clean laps read ({len(laps)})")
check(info["car_path"] == "mclaren570sgt4" and info["setup"], "file reader: car and setup from the file")
repf = bl.analyze_files([f1])
cf = cell(repf, "T3", "mid")
check(repf["ok"] and cf is not None and cf["label"] == "push", f"from the file: T3 middle pushes ({repf['summary']})")
check(repf["setup_ideas"] and repf["setup_ideas"][0]["change"] is not None, "from the file: setup ideas from its setup")
json.dumps(repf)
check(True, "report is JSON-ready")
bad = TMP / "bad.ibt"
bad.write_bytes(b"MZ" + b"\0" * 400)
rb = bl.analyze_files([bad])
check(not rb["ok"] and rb["info"]["skipped"], "an unreadable file is skipped, no crash")
check("Lap" in bl.CHANNELS and "YawRate" in bl.CHANNELS, "channels listed")

print(f"\n{'all passed' if not fails else str(fails) + ' FAILED'}")
sys.exit(1 if fails else 0)
