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


# multi-speed laps: the sweepers driven at different speeds (lifting or in traffic), so curvature and lateral g
# say different things in the gentle bends and K can be learned apart from G
SWEEP_KMH = (125.0, 150.0, 170.0, 187.0)
# a chicane after T2 taken at 24 m/s: a left, a quick flick of the wheel, then a right. As on his real laps (Zolder
# T5), the wheel leads the car through the flick: it crosses zero about 0.2 s before the car's rotation does
CHICANE = (900.0, 50.0, 1 / 45.0, 24.0)                                  # middle, half length, curvature, speed
CHICANE_LEAD_S = 0.2


def _profiles(d, multi=False):
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
    for k, (M, R, s) in enumerate(SWEEPERS):
        kap += s * np.clip(1 - np.abs(d - M) / 110.0, 0, 1) ** 0.5 / R
        if multi:                                     # eased down to the sweeper's speed and back up
            vs = SWEEP_KMH[k] / 3.6
            w = np.clip(1 - (np.abs(d - M) - 110.0) / 80.0, 0, 1)
            v = np.minimum(v, VMAX - (VMAX - vs) * w)
    v = np.maximum(v, 5.0)
    return v, kap, brake, thr


D = np.arange(0.0, L, 0.25)
V0, KAP0, BRK0, THR0 = _profiles(D)
V0M, _K, _B, _T = _profiles(D, multi=True)
_cd = D - CHICANE[0]
KAPC = CHICANE[2] * np.clip((CHICANE[1] - np.abs(_cd)) / 15.0, 0, 1) * -np.tanh(_cd / 1.5)
VCH = np.sqrt(CHICANE[3] ** 2 + 2 * 6.0 * np.clip(np.abs(_cd) - CHICANE[1], 0, None))


def first_order(x, tau):
    """x as a real car's yaw follows the wheel: a first-order lag of tau seconds."""
    if not tau:
        return np.asarray(x, float)
    a = 1.0 / (1.0 + tau * RATE)
    y = np.empty(len(x))
    y[0] = x[0]
    for i in range(1, len(x)):
        y[i] = y[i - 1] + a * (x[i] - y[i - 1])
    return y


def make_lap(rng, extra=None, scale=1.0, lap_noise=0.5, corner_noise=1.5, white=2.0, countersteer=False, kerb=False,
             lag=0.0, multi=False, progressive=0.0, chicane=False):
    """One lap as balance.load_laps gives it. extra(d, kap, ay) -> steering degrees added (turn direction sign).
    lag: the yaw and sideways g follow the steering with a first-order lag of this many seconds (real cars: about
    0.07 s); multi: sweepers at different speeds; progressive: steering quickens by up to this share at the
    tightest corners (a geometry effect, not a slide); chicane: a quick left-right flick after T2."""
    v_d = (V0M if multi else V0) * scale * (1 + 0.01 * rng.standard_normal())
    if chicane:
        v_d = np.minimum(v_d, VCH * scale)
    t_d = np.concatenate([[0.0], np.cumsum(np.diff(D) / v_d[:-1])])
    T = float(t_d[-1] + 0.25 / v_d[-1])
    t = np.arange(0.0, T, 1.0 / RATE)
    d = np.interp(t, t_d, D)
    v = np.interp(d, D, v_d)
    kap = np.interp(d, D, KAP0 + (KAPC if chicane else 0.0))
    ay = v ** 2 * kap
    r = v * kap
    sw = G_TRUE * kap + K_TRUE * ay / 9.81
    if chicane:                                    # the wheel leads the car through the flick
        ahead = np.interp(d + v * CHICANE_LEAD_S, D, KAPC) - np.interp(d, D, KAPC)
        sw = sw + G_TRUE * ahead + K_TRUE * v ** 2 * ahead / 9.81
    if progressive:
        sw = sw + G_TRUE * kap * np.clip(np.abs(kap) / 0.03, 0, 1) * progressive
    r, ay = first_order(r, lag), first_order(ay, lag)
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
    out[m] += np.sign(kap[m]) * 16.0                       # 16 degrees more steering mid-corner at T3
    m = (d > T5 - 70) & (d < T5 - 18)
    out[m] -= np.sign(kap[m]) * 14.0                       # 14 degrees less on the way into T5
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
check("rotates more than usual on the way into slow corners (T5)" in rep["summary"], "summary names the rotation")
# the way in is compared with his typical entry: a rotation added to every corner entry is not a finding
check(rep["entry_note"] and "typical entry" in rep["entry_note"], f"entry note: {rep['entry_note']}")


def rot_all_entries(d, kap, ay):
    out = np.zeros_like(d)
    for A, _v, _s in CORNERS:
        m = (d > A - 70) & (d < A - 18)
        out[m] -= np.sign(kap[m]) * 14.0
    return out


repe = bl.analyze_laps(session(1, n_laps=10, extra=rot_all_entries), info=INFO)
check(not [x for x in repe["findings"] if x["phase"] == "entry"],
      f"the same rotation on every entry: no entry finding ({[(x['corner'], x['label']) for x in repe['findings']]})")
check(repe["entry_typical"] and repe["entry_typical"]["median_deg"] < -10,
      f"... and the typical entry carries it ({repe['entry_typical']})")
check(not [i for i in bl.setup_ideas(repe, fi.SETUP_GT4) if i["phase"] == "entry"], "... so no entry setup idea")
cell5 = cell(rep, "T5", "entry")
check("more than on your other corner entries" in cell5["text"] and "typical entry" in cell5["text"], cell5["text"])
rep20 = bl.analyze_laps(session(11, n_laps=20, extra=push_t3_rot_t5), info=INFO)
check(any(x["label"] == "neutral" for x in rep20["cells"]),
      f"with 20 laps some places are positively neutral ({sum(x['label'] == 'neutral' for x in rep20['cells'])})")
repn = bl.analyze_laps(session(1, n_laps=8, extra=push_t3_rot_t5, lap_noise=3.0, corner_noise=4.0), info=INFO)
check(any(x["label"] == "unclear" for x in repn["cells"]), "8 noisy laps: some places are not clear yet")
check(all(x["ci_deg"][0] - x["fit_margin_deg"] > -x["practical_deg"] - 0.11 and
          x["ci_deg"][1] + x["fit_margin_deg"] < x["practical_deg"] + 0.11 for x in rep["cells"] + repn["cells"]
          if x["label"] == "neutral"), "neutral only when the interval and fitting margin sit inside the practical size")
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
# (b2) the same with what real cars do and the simple model leaves out: the yaw follows the wheel a few hundredths
# of a second later (0.04 and 0.07 s; his McLaren files measure 0.07 to 0.08 s), the gentle bends at several
# speeds, and steering that quickens by 10 % in the tightest corners (geometry, not a slide)
for name, kw in (("yaw lag 0.04 s", {"lag": 0.04}), ("yaw lag 0.07 s", {"lag": 0.07}),
                 ("bends at several speeds, 10 % progressive steering", {"multi": True, "progressive": 0.10}),
                 ("all three, lag 0.07 s", {"lag": 0.07, "multi": True, "progressive": 0.10})):
    fp, Gs = 0, []
    for k in range(N2):
        noise = {"lap_noise": 3.0, "corner_noise": 4.0} if k % 2 == 0 else {}
        r = bl.analyze_laps(session(300 + k, n_laps=int(6 + k % 6), **noise, **kw), info=INFO)
        fp += int(bool(r["findings"]))
        if r["car_fit"]:
            Gs.append(r["car_fit"]["G"])
    rr = fp / N2
    print(f"     {name}: {fp}/{N2} sessions with any false push/rotate = {rr:.1%}; learned G median "
          f"{np.median(Gs):.0f} (true {G_TRUE:.0f})")
    check(rr <= 0.05, f"{name}: false-positive rate {rr:.1%} (target 5%)")
    check(abs(np.median(Gs) - G_TRUE) / G_TRUE < 0.10, f"{name}: the car's steering doesn't collapse "
          f"({np.median(Gs):.0f} vs {G_TRUE:.0f})")
# the lag is measured: 0 for in-phase laps, about 0.07 s for a 0.07 s lag
lg = bl._lag_compensate([bl._prep(x, L) for x in session(7, n_laps=3, lag=0.07)])[1]
check(0.04 <= list(lg.values())[0] <= 0.10, f"a 0.07 s yaw lag is measured ({lg})")
lg0 = bl._lag_compensate([bl._prep(x, L) for x in session(7, n_laps=3)])[1]
check(list(lg0.values())[0] <= 0.02, f"no lag measured on in-phase laps ({lg0})")
# K is only learned when the gentle bends span speeds; one speed: K = 0, and said so
rm = bl.analyze_laps(session(8, n_laps=8, multi=True), info=INFO)
check(rm["car_fit"]["k_learned"] and rm["car_fit"]["speed_spread"] >= bl.FIT_SPEED_MIN,
      f"bends at several speeds: K learned (spread {rm['car_fit']['speed_spread']})")
P1 = [bl._prep(x, L) for x in session(8, n_laps=8)]
for p in P1:
    p["v"] = np.full(len(p["v"]), 45.0)               # every gentle bend at one speed
f1 = bl.fit_car(P1, boot=False)
check(f1 is not None and not f1["k_learned"] and f1["K"] == 0.0, f"one speed: K left at 0 ({f1 and f1['speed_spread']})")
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
# a quick left-right flick with a realistic yaw lag (0.07 s, as on his real laps): the wheel crosses zero before the
# car's rotation does, which the old check took for a catch. Must not count.
for tau in (0.04, 0.07, 0.10):
    rch = bl.analyze_laps(session(4, n_laps=8, chicane=True, lag=tau), info=INFO)
    check(not rch["countersteer"]["events"], f"chicane with yaw lag {tau} s: no countersteer "
          f"({rch['countersteer']['events'][:2]})")
# the old rule (5 degrees against 0.15 rad/s of yaw for 0.2 s, no lag) counted the chicane on these laps
Pc = [bl._prep(x, L) for x in session(4, n_laps=8, chicane=True, lag=0.07)]
raw = 0
for p in Pc:
    bad = p["ok"] & (np.abs(p["r"]) >= 0.15) & (np.abs(p["sw"]) >= 5) & (p["sw"] * p["r"] < 0)
    e = np.diff(np.concatenate([[0], bad.astype(int), [0]]))
    raw += int(any((b - a) >= 0.2 * RATE for a, b in zip(np.nonzero(e == 1)[0], np.nonzero(e == -1)[0])))
check(raw >= 6, f"(the old rule saw a catch at the chicane on {raw} of 8 laps)")
# with the yaw shifted by only the car's own 0.07 s lag (on these featureless synthetic laps the chicane pulls the
# measured lag up; on his real files it measures 0.07 to 0.08 s with Zolder's chicane in), what is left of the
# mismatch is too short and the rotation is falling through zero: still not a catch
ev = bl._countersteer([bl._apply_lag(p, int(round(0.07 * RATE))) for p in Pc], [], [{}] * len(Pc), G_TRUE, L, "T")
check(not ev, f"chicane, yaw shifted by the true 0.07 s lag only: no countersteer ({ev[:2]})")
# a real catch on lagged laps (opposite lock held while the car keeps rotating): counted
rcc = bl.analyze_laps(session(4, n_laps=8, chicane=True, lag=0.07, countersteer=True), info=INFO)
cs = rcc["countersteer"]["by_corner"]
check("T2" in cs and cs["T2"]["laps"] == 8 and len(cs) == 1, f"a real catch with yaw lag: counted at T2 only ({cs})")
ev = rcc["countersteer"]["events"][0]
check(ev["duration_s"] >= bl.CS_MIN_S and ev["peak_deg"] >= bl.CS_PEAK_DEG, f"its length and lock are kept ({ev})")

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
# 2.18: the entry rotation (front bar stiffer) has the stronger evidence here, so the mid-corner push never gets the
# front bar the other way: it takes its next option (rear bar stiffer) and says why
check(mid is not None and mid["change"]["text"].startswith("Rear anti-roll bar 1 → 2")
      and "front anti-roll bar 2 → 1 would pull against the idea for" in mid["text"].lower(),
      f"mid-corner push in a slow corner: not the front bar the opposite way ({mid and mid['change']['text']})")
keys = {}
for i in ideas:
    if i["change"]:
        keys.setdefault(i["change"]["key"], set()).add(i["change"]["way"])
check(all(len(w) == 1 for w in keys.values()), f"no two ideas move one setting opposite ways ({keys})")
check(mid is not None and "Setup Lab" in mid["text"] and "Change only this" in mid["text"], "one change, then compare")
ent = next((i for i in ideas if i["phase"] == "entry"), None)
# his McLaren: no brake bias in the setup and the rear bar at 1 (its lowest), so the usual "rear bar softer" can't be
# made: it falls through to the other axle, and says why
check(ent is not None and ent["change"] is not None and ent["change"]["text"].startswith("Front anti-roll bar 2 → 3"),
      f"entry rotation, rear bar already at 1: front bar one step stiffer ({ent and ent['change']['text']})")
check(ent is not None and "The usual change, rear anti-roll bar one step softer, can't be made here (it is set to 1, "
      "already at its lowest)" in ent["text"] and "Try one change: Front anti-roll bar 2 → 3" in ent["text"]
      and "may already be" not in ent["text"], f"says the usual change can't be made ({ent and ent['text'][:160]})")
check(all(i["change"] is None or i["change"]["to"] is not None for i in ideas), "no idea offers a change that can't be made")
s2 = json.loads(json.dumps(setup))
s2["Chassis"]["FrontBrakes"]["BrakePressureBias"] = "58.7%"
ent2 = next(i for i in bl.setup_ideas(rep, s2) if i["phase"] == "entry")
check(ent2["change"]["text"].startswith("Brake bias 58.7% → 59.2%"), f"with brake bias: forward a little ({ent2['change']['text']})")
# nothing that can be changed: said plainly, no "Try one change", and ranked after an idea that can be made
s4 = json.loads(json.dumps(setup))
s4["Chassis"]["FrontBrakes"]["ArbSetting"] = "1"
s4["Chassis"]["Rear"]["ArbBlades"] = "1"
I4 = bl.setup_ideas(rep, s4)
e4 = next(i for i in I4 if i["phase"] == "entry")
# (entry rotate: rear bar can't go softer; front bar 1 -> 2 stiffer is still possible)
check(e4["change"]["text"].startswith("Front anti-roll bar 1 → 2"), f"rear bar at its lowest: front bar stiffer ({e4['change']['text']})")


def F(phase, label, conf="clear", corner="T9", cls="slow"):
    return {"corner": corner, "corner_index": 8, "phase": phase, "label": label, "class": cls, "confidence": conf,
            "text": f"{corner} {phase}", "p_family": 0.001, "median_deg": 12.0}


ex = bl.setup_ideas({"findings": [F("exit", "rotate")]}, {"Chassis": {"FrontBrakes": {"ArbSetting": "3"},
                                                                      "Rear": {"ArbBlades": "1"}}})[0]
check(ex["change"] is not None and ex["change"]["text"].startswith("Front anti-roll bar 3 → 4"),
      f"exit rotation, rear bar at its lowest, no traction control: front bar stiffer ({ex['change'] and ex['change']['text']})")
# ranking: a "worth a try" idea that can't be made goes after "a guess" that can
I8 = bl.setup_ideas({"findings": [F("exit", "rotate", "clear", "T2"), F("mid", "push", "likely", "T3")]},
                    {"Chassis": {"FrontBrakes": {"ArbSetting": "Locked"}, "Rear": {"ArbBlades": "1"}}})
check(len(I8) == 2 and I8[0]["phase"] == "mid" and I8[0]["change"] is not None and I8[1]["change"] is None,
      f"an idea that can be made comes first, whatever its confidence "
      f"({[(i['phase'], i['confidence'], i['change'] and i['change']['text']) for i in I8]})")
I9 = I8[1]
check("already at its lowest" in I9["text"] and "Try one change" not in I9["text"],
      f"nothing can be changed: says so plainly ({I9['text']})")
# Ford-style values: a letter in front is kept; traction control's own name in brackets isn't doubled up
o = bl._option({"Chassis.Front.ArbSetting": "P2"}, "front_arb", 1)
check(o["from"] == "P2" and o["to"] == "P3" and o["text"] == "Front anti-roll bar P2 → P3 (one step stiffer)", o["text"])
o = bl._option({"Chassis.InCarDials.Tc1_Long": "3 (TC1)"}, "tc", 1)
check(o["text"] == "Traction control TC1 3 → 4 (one step more)" and ") (" not in o["text"], o["text"])
I10 = bl.setup_ideas({"findings": [F("exit", "rotate")]},
                     {"Chassis": {"Rear": {"ArbBlades": "1"}, "InCarDials": {"Tc1_Long": "3 (TC1)"}}})
check(I10 and "Traction control TC1 3 → 4" in I10[0]["text"] and "other way round" in I10[0]["text"],
      f"a traction control idea says the number may run the other way on this car ({I10 and I10[0]['text']})")
fixed = bl.setup_ideas(rep, setup, series_setup="fixed")
check(fixed and all(i["change"] is None for i in fixed) and all("The setup is locked" in i["text"] for i in fixed),
      "fixed series: no garage changes")
# his McLaren has no brake bias dial: driving advice; with the dial (a dc channel), the way in uses it
check(all(i["dial"] is None and i["driving"] for i in fixed), f"no dials: driving advice ({[i['text'] for i in fixed]})")
fixd = bl.setup_ideas(rep, setup, series_setup="fixed", dials=["bias"])
fe = next(i for i in fixd if i["phase"] == "entry")
fm = next(i for i in fixd if i["phase"] == "mid")
check(fe["dial"] and fe["dial"]["target"] == "bias" and "to the front on the in-car dial" in fe["text"]
      and fm["dial"] is None and "adapt the driving" in fm["text"], f"brake bias dial for the way in, driving for the middle ({fe['text']})")
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
