"""Car balance map (2.15): where the car pushes (understeer) or rotates (oversteer), per corner and phase, from
your own iRacing telemetry (.ibt files), and one conventional setup change to try for the clearest finding.

How balance is measured (the understeer index, in steering-wheel degrees):
  - The steering the car "should" need if the tyres were not sliding at all is wheelbase x curvature x steering
    ratio. Neither the wheelbase in iRacing nor the steering ratio is in the telemetry, so the product of the two
    (G) is learned from your own laps, together with the car's normal gentle-bend understeer (K, steering per g):
        steering = G x curvature + K x lateral g        (fitted on 0.10 to 0.40 g, above 40 km/h, no kerbs)
    Only steady driving goes into the fit (brake off, wheel moving slower than 30 degrees a second), so the
    rotation you get from trail braking can't bend it. With the wheelbase in WHEELBASE_M, G becomes a ratio.
    Curvature comes from the yaw rate (YawRate / speed): steering minus G x yaw-curvature is exactly the front
    minus rear slip angle, so it is the textbook balance number even while the car is turning in. The same thing
    from lateral acceleration (LatAccel / speed^2) is worked out as a cross-check.
  - Near the limit (0.30 g and more) the "excess" is the steering you used minus what that fit predicts, signed
    by the direction of the turn: more steering than normal = the front is giving up first = pushes (understeer);
    less = the rear is = rotates (oversteer). So "balance" here always means: compared with how this car steers
    in gentle bends.
  - Signals are smoothed first (the response of a 4th-order Butterworth low-pass at 4 Hz run forwards and
    backwards, done with numpy's FFT, so no scipy is needed). Samples under 40 km/h or 0.30 g, and anything within
    0.15 s of a kerb strike (a vertical-acceleration or shock-velocity spike) are ignored.
  - Phases, per lap and corner (the corner coach's own corners, T1..Tn):
        entry  braking (brake > 5 %) while turned in (steering at least 20 % of that corner's peak) and not
               unwinding the wheel
        mid    within 15 m of that lap's slowest point, brake under 10 %. (Real laps showed 20 to 60 % throttle
               already at the slowest point, so the "pedals under 10 %" idea would leave the middle empty.)
        exit   after that, throttle 10 % or more, brake off
  - Countersteer: the steering points the other way to the car's rotation for at least 0.2 s (above 40 km/h,
    5 degrees of steering, 0.15 rad/s of yaw). Each one is an oversteer moment you had to catch.

Honest statistics (2.13's review lesson): one number per lap per corner and phase (the median excess), so laps are
the units. A place is called "pushes" or "rotates" only when
  (1) a sign-flip permutation test across laps (the "shuffle" null: each lap's numbers flipped together, so the
      family of all corners and phases is tested at once, max-|t|) gives a family-wise p below 0.05,
      The fit of G and K is shared by every lap, so an error in it moves every lap the same way, which flipping laps
      can't see. So each shuffled pattern also gets the effect of one bootstrap refit (whole laps resampled), and
      the null carries both kinds of noise. (Without this, noisy test sessions made false calls in 15 % of them.)
  (2) the median is at least the practical size (PRACT_DEG and PRACT_FRAC of the steering used there), and
      bigger than what the fitting error alone could do there (97.5th percentile of the refits' effect), and
  (3) the same fit from ALL gentle-bend samples (not only steady ones) gives the same answer at that size. On his
      real McLaren laps the two fits differ by about 20 %, which moves slow corners (big curvature) the most, so
      slow-corner calls that depend on which fit is used are left "not clear yet".
"neutral" needs the 90 % interval of the median (order statistics, no assumptions), widened by that fitting
margin (and the difference between the two fits), inside +/- the practical size.
Everything else is "not clear yet". Tests check that pure noise gives a false "pushes"/"rotates" anywhere in the
map in at most 5 % of sessions.

Public API (JSON-friendly; nothing raises for bad input):
    load_laps(paths)                    -> (laps, info): clean laps as time series
    analyze_laps(laps, info=None, setup=None, series_setup=None) -> report (see analyze_laps)
    analyze_files(paths, series_setup=None) -> report, with setup ideas from the newest file's setup
    setup_ideas(report, setup, series_setup=None) -> [idea]
    lowpass(x, rate, fc)                -> zero-phase low-pass
    WHEELBASE_M                         -> road-car wheelbases we could source (only used to turn G into a ratio)
"""
from __future__ import annotations

import math
from itertools import product
from pathlib import Path

import numpy as np

from . import engine, ibt

VERSION = 1
G0 = 9.80665
KMH = 3.6

# ---------------------------------------------------------------- thresholds (all in one place)
FC_HZ = 4.0                 # low-pass corner frequency
ORDER = 4                   # Butterworth order of the response (applied forwards and backwards)
MIN_KMH = 40.0              # slower samples are ignored
EVAL_G = 0.30               # balance is read at this lateral g and more
FIT_G = (0.10, 0.40)        # the car's normal steering (G, K) is learned in this band
MIN_CURV = 1.0 / 2000.0     # 1/m: straighter than a 2 km radius is not a bend
KERB_VERT_MS2 = 5.0         # vertical accel this far from its 0.5 s running median = a kerb strike
KERB_SHOCK_MS = 0.35        # or any shock moving faster than this (m/s)
KERB_PAD_S = 0.15           # samples this close to a strike are ignored too
TURNED_FRAC = 0.20          # entry: steering at least this share of the corner's peak
UNWIND_DEG_S = 20.0         # entry: unwinding faster than this is not entry any more
BRAKE_ON = 5.0              # %
MID_HALF_M = 15.0           # mid: within this of the slowest point
MID_BRAKE_MAX = 10.0        # %
EXIT_THR = 10.0             # %
MIN_SAMPLES = 6             # samples (0.1 s) a phase needs on a lap to give a number
MIN_LAPS = 6                # laps with a number before a place can be labelled
OFFPACE_FRAC = 1.07         # laps slower than 107 % of the median are left out (as the corner coach does)
MIN_COVER = 0.95

PRACT_DEG = 3.0             # practical size: at least 3 degrees of steering wheel ...
PRACT_FRAC = 0.08           # ... and at least 8 % of the steering used there
NOISE_DEG = 0.5             # smallest lap-to-lap spread assumed (a perfectly repeatable number can't be "significant")
ALPHA = 0.05                # family-wise error rate for "pushes" / "rotates"
CLEAR_P = 0.01              # ... "clear" below this with CLEAR_LAPS laps, else "likely"
CLEAR_LAPS = 12
CI_LEVEL = 0.90             # interval of the median (order statistics)
EXACT_MAX_LAPS = 11         # up to 2^11 sign patterns are enumerated exactly
N_PERM = 4000               # otherwise this many random patterns (fixed seed)
PERM_SEED = 215
N_FIT_BOOT = 200            # bootstrap refits of G, K (whole laps resampled)
N_FIT_NULL = 40             # refits whose effect on every place is put into the permutation null
STEADY_DEG_S = 30.0         # the fit uses steady driving only: wheel moving slower than this, brake off

CS_DEG = 5.0                # countersteer: steering at least this many degrees the "wrong" way ...
CS_YAW = 0.15               # ... while rotating at least this fast (rad/s) ...
CS_MIN_S = 0.20             # ... for at least this long

SLOW_KMH = 100.0            # corner class by the slowest point on your fastest lap
FAST_KMH = 150.0

PARAMS = {k: v for k, v in globals().items() if k.isupper() and isinstance(v, (int, float)) and not isinstance(v, bool)}

# Road-car wheelbases (metres) we could source with confidence. iRacing does not write the wheelbase or the
# steering ratio into the telemetry, and the balance map never needs them (it learns G = wheelbase x ratio from
# your laps). They only turn G into an approximate steering ratio for the report. Race versions are built on the
# same chassis; small differences are possible.
WHEELBASE_M = {
    "mclaren570sgt4": (2.670, "McLaren 570S road car, 2,670 mm"),
    "mclaren720sgt3": (2.670, "McLaren 720S road car, 2,670 mm (carexpert.com.au)"),
    "porsche992rgt3": (2.507, "Porsche 911 GT3 R (992), 2,507 mm (stuttcars.com)"),
    "fordmustanggt4": (2.720, "Ford Mustang (S550) road car, 2,720 mm"),
    "mx5 mx52016": (2.310, "Mazda MX-5 (ND) road car, 2,310 mm"),
}

LABELS = {"push": "pushes (understeer)", "neutral": "neutral", "rotate": "rotates (oversteer)",
          "unclear": "not clear yet"}
PHASES = ("entry", "mid", "exit")
PHASE_WORDS = {"entry": "on the way in", "mid": "in the middle", "exit": "on the way out"}
PHASE_OF = {"entry": "on the way into", "mid": "in the middle of", "exit": "on the way out of"}
CHANNELS = ("Lap", "LapLastLapTime", "SessionNum", "OnPitRoad", "PlayerTrackSurface", "PlayerCarMyIncidentCount",
            "IsOnTrack", "LapDistPct", "Speed", "Brake", "Throttle", "SteeringWheelAngle", "YawRate", "LatAccel",
            "VertAccel", "LFshockVel", "RFshockVel", "LRshockVel", "RRshockVel")


# ---------------------------------------------------------------- small helpers
def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _r(v, d=2):
    v = _num(v)
    return None if v is None else round(v, d)


def lowpass(x, rate: float, fc: float = FC_HZ, order: int = ORDER) -> np.ndarray:
    """Zero-phase low-pass: gain 1 / (1 + (f/fc)^(2*order)), the response of an order-`order` Butterworth filter
    run forwards and backwards. Done in the frequency domain with reflected padding (numpy only)."""
    x = np.asarray(x, float)
    n = len(x)
    if n < 8 or rate <= 0:
        return x.copy()
    bad = ~np.isfinite(x)
    if bad.any():
        if bad.all():
            return x.copy()
        idx = np.arange(n)
        x = x.copy()
        x[bad] = np.interp(idx[bad], idx[~bad], x[~bad])
    pad = int(min(n - 1, max(8, 2 * rate)))
    y = np.concatenate([x[pad:0:-1], x, x[-2:-pad - 2:-1]])
    f = np.fft.rfftfreq(len(y), 1.0 / rate)
    gain = 1.0 / (1.0 + (f / fc) ** (2 * order))
    out = np.fft.irfft(np.fft.rfft(y) * gain, len(y))
    return out[pad:pad + n]


def _running_median(x, k):
    from numpy.lib.stride_tricks import sliding_window_view
    k = max(3, int(k) | 1)
    if len(x) < k:
        return np.full(len(x), float(np.median(x)) if len(x) else 0.0)
    return np.median(sliding_window_view(np.pad(x, (k // 2, k // 2), mode="edge"), k), axis=1)


def kerb_mask(vert, shock_max, rate: float) -> np.ndarray:
    """True where a kerb strike (vertical acceleration spike or a fast shock) is within KERB_PAD_S."""
    n = len(vert) if vert is not None else (len(shock_max) if shock_max is not None else 0)
    hit = np.zeros(n, bool)
    if vert is not None and len(vert):
        va = np.asarray(vert, float)
        hit |= np.abs(va - _running_median(va, rate * 0.5)) > KERB_VERT_MS2
    if shock_max is not None and len(shock_max):
        hit |= np.asarray(shock_max, float) > KERB_SHOCK_MS
    if not hit.any():
        return hit
    k = int(round(KERB_PAD_S * rate))
    if k > 0:
        hit = np.convolve(hit.astype(float), np.ones(2 * k + 1), mode="same") > 0
    return hit


def _unwrap_lap(p):
    p = np.asarray(p, float).copy()
    if len(p) and p[0] > 0.5:
        k = int(np.argmax(p < 0.5))
        if k > 0:
            p[:k] -= 1.0
    return np.maximum.accumulate(p)


def _median_ci(vals, level=CI_LEVEL):
    """Distribution-free interval of the median from order statistics (coverage >= level)."""
    x = np.sort(np.asarray(vals, float))
    n = len(x)
    if n == 0:
        return None, None
    a = (1 - level) / 2
    cdf = 0.0
    k = 0
    for j in range(n + 1):                       # largest k with P(Binom(n, .5) < k) <= a
        pj = math.comb(n, j) / 2.0 ** n
        if cdf + pj > a:
            break
        cdf += pj
        k = j + 1
    if k == 0:
        return float(x[0]), float(x[-1])
    return float(x[k - 1]), float(x[n - k])


# ---------------------------------------------------------------- reading laps
def _length_m(ib) -> float | None:
    km = ib.track().get("length_km")
    if km and km > 0.1:
        return km * 1000.0
    return None


def load_laps(src) -> tuple:
    """Clean laps from one .ibt path or a list (one car and track), as time series. Never raises.

    lap: {"file", "lap", "time", "rate", "pct", "v" m/s, "sw" deg, "r" rad/s, "ay" m/s^2, "b" %, "th" %, "kerb"}
    info: {"files", "skipped", "car", "car_path", "track", "track_code", "length_m", "setups": [fingerprint...],
           "setup": newest file's CarSetup, "missing": [channel...]}"""
    from . import setuplab
    paths = src if isinstance(src, (list, tuple)) else [src]
    laps = []
    info = {"files": [], "skipped": [], "car": None, "car_path": None, "track": None, "track_code": None,
            "length_m": None, "setups": [], "setup": None, "missing": []}
    combo, newest = None, -1
    for p in paths:
        name = Path(str(p)).name
        try:
            with ibt.open_ibt(p) as ib:
                car, tr = ib.car(), ib.track()
                key = (str(car.get("path") or "").lower(), str(tr.get("code") or "").lower())
                if combo is None:
                    combo = key
                    info.update(car=car.get("name") or car.get("path"), car_path=car.get("path"),
                                track=tr.get("name"), track_code=tr.get("code"), length_m=_length_m(ib))
                elif key != combo:
                    info["skipped"].append({"file": name, "why": "a different car or track"})
                    continue
                need = ("Speed", "LapDistPct", "SteeringWheelAngle", "YawRate", "LatAccel", "Brake", "Throttle")
                miss = [c for c in need if not ib.has(c)]
                if miss or not info["length_m"]:
                    info["skipped"].append({"file": name, "why": ("no " + ", ".join(miss) + " channel") if miss
                                            else "track length unknown"})
                    info["missing"] = sorted(set(info["missing"]) | set(miss))
                    continue
                rate = float(ib.tick_rate or 60)
                col = {c: ib.col(c).astype(float) for c in CHANNELS if ib.has(c) and c not in (
                    "Lap", "LapLastLapTime", "SessionNum", "OnPitRoad", "PlayerTrackSurface",
                    "PlayerCarMyIncidentCount", "IsOnTrack")}
                shocks = [col[c] for c in ("LFshockVel", "RFshockVel", "LRshockVel", "RRshockVel") if c in col]
                smax = np.max(np.abs(np.stack(shocks)), axis=0) if shocks else None
                kerb = kerb_mask(col.get("VertAccel"), smax, rate)
                fp = setuplab.fingerprint(ib.setup)
                if fp and fp not in info["setups"]:
                    info["setups"].append(fp)
                if ib.start_date >= newest:
                    newest, info["setup"] = ib.start_date, ib.setup
                n = 0
                for x in ibt.laps(ib):
                    if not x["clean"] or not x["time"]:
                        continue
                    a, b = x["start"], x["end"]
                    if b - a < 100:
                        continue
                    pct = col["LapDistPct"][a:b]
                    u = _unwrap_lap(pct)
                    if float(u[-1] - u[0]) < MIN_COVER:
                        continue
                    laps.append({"file": name, "lap": x["lap"], "time": float(x["time"]), "rate": rate,
                                 "pct": pct, "v": col["Speed"][a:b], "sw": col["SteeringWheelAngle"][a:b] * 57.2958,
                                 "r": col["YawRate"][a:b], "ay": col["LatAccel"][a:b],
                                 "b": col["Brake"][a:b] * 100.0, "th": col["Throttle"][a:b] * 100.0,
                                 "kerb": kerb[a:b], "setup": fp})
                    n += 1
                info["files"].append({"file": name, "clean_laps": n})
        except Exception as e:  # noqa: BLE001
            info["skipped"].append({"file": name, "why": f"could not be read ({str(e)[:120]})"})
    return laps, info


# ---------------------------------------------------------------- per-lap preparation
def _prep(lap: dict, L: float) -> dict | None:
    """Filtered signals, distance and curvature for one lap. None if unusable."""
    try:
        rate = float(lap.get("rate") or 60.0)
        v = lowpass(lap["v"], rate)
        sw = lowpass(lap["sw"], rate)
        r = lowpass(lap["r"], rate)
        ay = lowpass(lap["ay"], rate)
        n = len(v)
        if n < 100 or not (len(sw) == len(r) == len(ay) == n):
            return None
        b = np.asarray(lap.get("b"), float) if lap.get("b") is not None else np.zeros(n)
        th = np.asarray(lap.get("th"), float) if lap.get("th") is not None else np.zeros(n)
        kerb = lap.get("kerb")
        if kerb is None:
            kerb = kerb_mask(lap.get("vert"), lap.get("shock"), rate)
        kerb = np.asarray(kerb, bool)
        vs = np.maximum(v, 1.0)
        d = _unwrap_lap(lap["pct"]) * L
        fast = v * KMH >= MIN_KMH
        return {"rate": rate, "v": v, "sw": sw, "r": r, "ay": ay, "b": b, "th": th, "kerb": kerb, "d": d % L,
                "k_yaw": r / vs, "k_ay": ay / vs ** 2, "ok": fast & ~kerb,
                "dsw": np.gradient(np.abs(sw)) * rate}
    except Exception:  # noqa: BLE001
        return None


def _corners_from(lap: dict, L: float) -> tuple:
    """Corners (engine.detect_corners on this lap, the corner coach's numbering) and each one's slowest speed."""
    n = len(lap["v"])
    z = np.zeros(n)
    raw = {"v": np.asarray(lap["v"], float), "p": np.asarray(lap["pct"], float),
           "b": np.asarray(lap.get("b") if lap.get("b") is not None else z, float) / 100.0,
           "th": np.asarray(lap.get("th") if lap.get("th") is not None else z, float) / 100.0,
           "gear": z, "abs": z, "la": z, "lo": z, "st": np.asarray(lap["sw"], float) / 57.2958, "rpm": z,
           "lat": np.full(n, np.nan), "lon": np.full(n, np.nan), "_missing": []}
    R = engine.resample(raw, float(lap["time"]), float(L))
    corners = engine.detect_corners(R, L=L)
    mins = [float(np.interp(c["apex_m"] % L, R["d"], R["v"])) / engine.MPH * KMH for c in corners]
    return corners, mins


# ---------------------------------------------------------------- the car's normal steering
def _fit_samples(P: list, kind: str = "yaw", steady: bool = True) -> tuple:
    """(steer, curvature, lat g, lap index) arrays from the gentle-bend band of every lap (steady driving only,
    unless steady=False: then every gentle-bend sample, the sensitivity check)."""
    S, K, A, I = [], [], [], []
    for i, p in enumerate(P):
        g = np.abs(p["ay"]) / G0
        k = p["k_yaw"] if kind == "yaw" else p["k_ay"]
        m = p["ok"] & (g >= FIT_G[0]) & (g <= FIT_G[1]) & (np.abs(k) >= MIN_CURV)
        if steady:
            m &= (p["b"] <= BRAKE_ON) & (np.abs(p["dsw"]) <= STEADY_DEG_S)
        S.append(p["sw"][m])
        K.append(k[m])
        A.append(p["ay"][m] / G0)
        I.append(np.full(int(m.sum()), i))
    if not S:
        return np.zeros(0), np.zeros(0), np.zeros(0), np.zeros(0, int)
    return np.concatenate(S), np.concatenate(K), np.concatenate(A), np.concatenate(I)


def _robust_fit(sw, k, a) -> tuple | None:
    """steer = G * curvature + K * lat g, through zero, with Huber weights (a few slides can't drag it)."""
    if len(sw) < 50:
        return None
    X = np.column_stack([k, a])
    w = np.ones(len(sw))
    beta = None
    for _ in range(8):
        Xw = X * w[:, None]
        try:
            beta, *_ = np.linalg.lstsq(Xw.T @ X, Xw.T @ sw, rcond=None)
        except np.linalg.LinAlgError:
            return None
        res = sw - X @ beta
        s = 1.4826 * float(np.median(np.abs(res))) or 1.0
        u = np.abs(res) / (1.345 * s)
        w = np.where(u <= 1, 1.0, 1.0 / np.maximum(u, 1e-9))
    return float(beta[0]), float(beta[1])


def fit_car(P: list, kind: str = "yaw", steady: bool = True, boot: bool = True) -> dict | None:
    """G (steering degrees per 1/m of curvature) and K (steering degrees per g), with a lap-bootstrap interval."""
    sw, k, a, idx = _fit_samples(P, kind, steady)
    est = _robust_fit(sw, k, a)
    if est is None or abs(est[0]) < 1.0:
        return None
    rng = np.random.default_rng(PERM_SEED)
    n = len(P)
    by = [np.nonzero(idx == i)[0] for i in range(n)]
    boots = []
    for _ in range(N_FIT_BOOT if boot else 0):
        pick = rng.integers(0, n, n)
        sel = np.concatenate([by[i] for i in pick])
        e = _robust_fit(sw[sel], k[sel], a[sel])
        if e is not None:
            boots.append(e)
    boots = np.array(boots) if boots else np.array([est])
    lo, hi = np.percentile(boots[:, 0], [5, 95])
    return {"G": est[0], "K": est[1], "G_ci": [float(lo), float(hi)],
            "K_ci": [float(x) for x in np.percentile(boots[:, 1], [5, 95])],
            "samples": int(len(sw)), "boots": boots}


# ---------------------------------------------------------------- per lap, corner and phase
def _phase_masks(p: dict, c: dict, L: float) -> dict | None:
    rel = ((p["d"] - c["apex_m"] + L / 2) % L) - L / 2
    zone = (rel >= c["start_m"] - c["apex_m"]) & (rel <= c["end_m"] - c["apex_m"]) & p["ok"]
    if zone.sum() < 3 * MIN_SAMPLES:
        return None
    zi = np.nonzero(zone)[0]
    k_min = zi[int(np.argmin(p["v"][zi]))]
    xm = rel[k_min]
    peak = float(np.max(np.abs(p["sw"][zi])))
    turned = np.abs(p["sw"]) >= TURNED_FRAC * peak
    entry = zone & (rel < xm - MID_HALF_M) & (p["b"] > BRAKE_ON) & turned & (p["dsw"] > -UNWIND_DEG_S)
    mid = zone & (np.abs(rel - xm) <= MID_HALF_M) & (p["b"] < MID_BRAKE_MAX)
    exit_ = zone & (rel > xm + MID_HALF_M) & (p["th"] >= EXIT_THR) & (p["b"] <= BRAKE_ON)
    return {"entry": entry, "mid": mid, "exit": exit_, "zone": zone, "rel": rel}


def _cell_index(P, masks) -> list:
    """Per lap, {(corner index, phase): sample indexes read for the balance} (near the limit, in a bend, enough)."""
    out = []
    for p, mk in zip(P, masks):
        ev = (np.abs(p["ay"]) / G0 >= EVAL_G) & (np.abs(p["k_yaw"]) >= MIN_CURV)
        d = {}
        for key, m in mk.items():
            idx = np.nonzero(m & ev)[0]
            if len(idx) >= MIN_SAMPLES:
                d[key] = idx
        out.append(d)
    return out


def _cell_values(P, cidx, fits, kind="yaw"):
    """{(corner index, phase): [per-lap median excess or nan]}, and the steering used there (median).
    cidx: _cell_index(); fits: one (G, K) or None per lap."""
    nl = len(P)
    keys = set()
    for d in cidx:
        keys |= set(d)
    vals = {k: np.full(nl, np.nan) for k in keys}
    us = {k: [] for k in keys}
    for i, (p, d, f) in enumerate(zip(P, cidx, fits)):
        if f is None:
            continue
        G, K = f
        kk = p["k_yaw"] if kind == "yaw" else p["k_ay"]
        for key, idx in d.items():
            expect = G * kk[idx] + K * p["ay"][idx] / G0
            vals[key][i] = float(np.median(np.sign(G * kk[idx]) * (p["sw"][idx] - expect)))
            us[key].append(float(np.median(np.abs(expect))))
    return vals, {k: (float(np.median(v)) if v else None) for k, v in us.items()}


def _sign_patterns(n: int) -> np.ndarray:
    if n <= EXACT_MAX_LAPS:
        return np.array(list(product((1.0, -1.0), repeat=n)))
    rng = np.random.default_rng(PERM_SEED)
    S = rng.choice((1.0, -1.0), size=(N_PERM, n))
    S[0] = 1.0
    return S


def maxt_pvalues(V: np.ndarray, shifts: np.ndarray | None = None) -> tuple:
    """Family-wise sign-flip permutation test. V: laps x cells (nan = no number). Each lap's numbers are flipped
    together, so the correlation between places on the same lap is kept. shifts (refits x cells): how much each
    place's mean moves when the car's normal steering is refitted on resampled laps; one refit is added to each
    shuffled pattern, so the null also carries the fitting error (which moves every lap the same way and so can't
    be seen by flipping laps). Returns (t, adjusted p) per cell."""
    n_l, n_c = V.shape
    M = np.isfinite(V)
    Z = np.where(M, V, 0.0)
    n = M.sum(0).astype(float)
    ss = (Z ** 2).sum(0)

    sh = None
    extra = np.zeros(n_c)
    if shifts is not None and len(shifts):
        sh = np.nan_to_num(np.asarray(shifts, float))
        extra = sh.var(axis=0)                           # the fit's own spread goes into each place's yardstick

    def tstat(sums):
        mean = sums / np.maximum(n, 1)
        var = (ss - n * mean ** 2) / np.maximum(n - 1, 1)
        se2 = np.maximum(var, NOISE_DEG ** 2) / np.maximum(n, 1)
        return mean / np.sqrt(se2 + extra)

    t_obs = tstat(Z.sum(0))
    S = _sign_patterns(n_l)
    sums = S @ Z
    if sh is not None:
        sums = sums + n * sh[np.arange(len(S)) % len(sh)]
    T = np.abs(tstat(sums))                            # patterns x cells
    mx = T.max(axis=1)
    p = np.array([(np.sum(mx >= abs(t) - 1e-12)) / len(S) for t in t_obs])
    return t_obs, p


def _speed_class(kmh: float) -> str:
    return "slow" if kmh < SLOW_KMH else ("fast" if kmh >= FAST_KMH else "medium")


# ---------------------------------------------------------------- countersteer
def _countersteer(P, corners, masks, G, L, prefix):
    out = []
    sgn = 1.0 if G >= 0 else -1.0
    for i, p in enumerate(P):
        swa = p["sw"] * sgn
        bad = p["ok"] & (np.abs(p["r"]) >= CS_YAW) & (np.abs(swa) >= CS_DEG) & (swa * p["r"] < 0)
        if not bad.any():
            continue
        edges = np.diff(np.concatenate([[0], bad.astype(int), [0]]))
        starts, ends = np.nonzero(edges == 1)[0], np.nonzero(edges == -1)[0]
        for s, e in zip(starts, ends):
            if (e - s) / p["rate"] < CS_MIN_S:
                continue
            where, phase = None, None
            for ci, c in enumerate(corners):
                m = masks[i].get((ci, "_zone"))
                if m is not None and m[s]:
                    where = ci
                    for ph in PHASES:
                        mm = masks[i].get((ci, ph))
                        if mm is not None and mm[s:e].any():
                            phase = ph
                            break
                    break
            out.append({"lap": i, "corner": where, "name": f"{prefix}{where + 1}" if where is not None else None,
                        "phase": phase, "duration_s": round((e - s) / p["rate"], 2),
                        "kmh": round(float(p["v"][s]) * KMH)})
    return out


# ---------------------------------------------------------------- whole analysis
def analyze_laps(laps, info=None, setup=None, series_setup=None, prefix: str = "T") -> dict:
    """Balance map for clean laps of one car and track. Never raises.

    {"ok", "message", "version", "laps", "corners": [{"id","name","apex_m","min_kmh","class"}],
     "cells": [{"corner","id","phase","phase_words","class","label","label_words","median_deg","ci_deg",
                "pct_of_steering","laps","p_family","confidence","practical_deg","text"}],
     "findings": [cell with label push/rotate], "countersteer": {"events": [...], "by_corner": {name: {...}}},
     "car_fit": {"G","G_ci","K","K_ci","ratio_estimate","wheelbase_source","samples","kind"},
     "cross_check": {"kind": "lateral acceleration", "agree": n, "of": n}, "summary": str,
     "setup_ideas": [...], "method": {...}, "params": PARAMS, "info": info}"""
    info = dict(info or {})
    base = {"ok": False, "version": VERSION, "laps": 0, "corners": [], "cells": [], "findings": [],
            "countersteer": {"events": [], "by_corner": {}}, "car_fit": None, "cross_check": None,
            "summary": "", "setup_ideas": [], "notes": [], "method": _method(), "params": PARAMS, "info": _clean_info(info)}
    try:
        laps = [x for x in (laps or []) if isinstance(x, dict) and _num(x.get("time"))]
        if laps:
            med = float(np.median([x["time"] for x in laps]))
            laps = [x for x in laps if x["time"] <= med * OFFPACE_FRAC]
        L = _num(info.get("length_m")) or _guess_length(laps)
        if len(laps) < MIN_LAPS or not L:
            base["laps"] = len(laps)
            base["message"] = (f"{'Only ' + str(len(laps)) if laps else 'No'} clean lap{'s' if len(laps) != 1 else ''}"
                               f" with steering and yaw rate. The balance map needs at least {MIN_LAPS}.")
            base["summary"] = base["message"]
            return base
        P, keep = [], []
        for x in laps:
            p = _prep(x, L)
            if p is not None:
                P.append(p)
                keep.append(x)
        laps = keep
        base["laps"] = len(P)
        if len(P) < MIN_LAPS:
            base["message"] = f"Only {len(P)} laps could be read. The balance map needs at least {MIN_LAPS}."
            base["summary"] = base["message"]
            return base
        fastest = min(range(len(laps)), key=lambda i: laps[i]["time"])
        corners, mins = _corners_from(laps[fastest], L)
        if not corners:
            base["message"] = "No corners found on your fastest lap."
            base["summary"] = base["message"]
            return base
        base["corners"] = [{"id": c["id"], "name": f"{prefix}{k + 1}", "apex_m": _r(c["apex_m"], 1),
                            "min_kmh": _r(mins[k], 0), "class": _speed_class(mins[k])} for k, c in enumerate(corners)]
        fit = fit_car(P, "yaw")
        if fit is None:
            base["message"] = "Not enough gentle-bend driving to learn how this car normally steers."
            base["summary"] = base["message"]
            return base
        G, K = fit["G"], fit["K"]
        masks = []
        for p in P:
            mk = {}
            for ci, c in enumerate(corners):
                pm = _phase_masks(p, c, L)
                if pm is None:
                    continue
                mk[(ci, "_zone")] = pm["zone"]
                for ph in PHASES:
                    mk[(ci, ph)] = pm[ph]
            masks.append(mk)
        cell_masks = _cell_index(P, [{k: v for k, v in m.items() if k[1] != "_zone"} for m in masks])
        nP = len(P)
        vals, used = _cell_values(P, cell_masks, [(G, K)] * nP)
        keys = sorted(k for k in vals if np.isfinite(vals[k]).sum() >= MIN_LAPS)
        cells = []
        if keys:
            V = np.column_stack([vals[k] for k in keys])
            # what a refit of the car's normal steering does to each place (the fit is shared by every lap)
            rng = np.random.default_rng(PERM_SEED + 1)
            boots = fit["boots"]
            pick = boots[rng.integers(0, len(boots), min(N_FIT_NULL, len(boots)))]
            shifts = []
            for g, k in pick:
                alt = _cell_values(P, cell_masks, [(float(g), float(k))] * nP)[0]
                shifts.append([float(np.nanmean(alt[key] - vals[key])) if np.isfinite(alt[key] - vals[key]).any()
                               else 0.0 for key in keys])
            shifts = np.array(shifts)
            infl = math.sqrt(nP / max(1, nP - 1))
            margin = np.percentile(np.abs(shifts), 97.5, axis=0) * infl if len(shifts) else np.zeros(len(keys))
            # sensitivity: the same fit from every gentle-bend sample (not only steady driving). On real laps the
            # two can differ by 20 %, so a place counts only when both say the same thing.
            fit_b = fit_car(P, "yaw", steady=False, boot=False)
            vals_b = _cell_values(P, cell_masks, [(fit_b["G"], fit_b["K"])] * nP)[0] if fit_b else vals
            model = np.array([abs(float(np.nanmean(vals_b[key] - vals[key])))
                              if np.isfinite(vals_b[key] - vals[key]).any() else 0.0 for key in keys])
            margin = np.maximum(margin, model)
            t, padj = maxt_pvalues(V, shifts * infl)
            # cross-check from lateral acceleration
            fit_ay = fit_car(P, "ay", boot=False)
            vals_ay = _cell_values(P, cell_masks, [(fit_ay["G"], fit_ay["K"])] * nP, "ay")[0] if fit_ay else {}
            agree = of = 0
            for j, key in enumerate(keys):
                x = vals[key][np.isfinite(vals[key])]
                med = float(np.median(x))
                lo, hi = _median_ci(x)
                u = used.get(key) or 0.0
                prac = max(PRACT_DEG, PRACT_FRAC * u)
                ci, ph = key
                label = "unclear"
                mg = float(margin[j])
                xb = vals_b[key][np.isfinite(vals_b[key])]
                med_b = float(np.median(xb)) if len(xb) else 0.0
                if (padj[j] < ALPHA and abs(med) >= prac and abs(med) > mg and np.sign(med_b) == np.sign(med)
                        and abs(med_b) >= prac):
                    label = "push" if med > 0 else "rotate"
                elif lo is not None and lo - mg > -prac and hi + mg < prac:
                    label = "neutral"
                conf = None
                if label in ("push", "rotate"):
                    conf = "clear" if padj[j] < CLEAR_P and len(x) >= CLEAR_LAPS else "likely"
                    y = vals_ay[key][np.isfinite(vals_ay[key])] if key in vals_ay else []
                    of += 1
                    agree += int(len(y) > 0 and np.sign(np.median(y)) == np.sign(med))
                cname = f"{prefix}{ci + 1}"
                cells.append({"corner": cname, "id": corners[ci]["id"], "corner_index": ci, "phase": ph,
                              "phase_words": PHASE_WORDS[ph], "class": base["corners"][ci]["class"],
                              "label": label, "label_words": LABELS[label], "median_deg": _r(med, 1),
                              "ci_deg": [_r(lo, 1), _r(hi, 1)], "steering_deg": _r(u, 1),
                              "pct_of_steering": _r(100 * med / u, 0) if u else None, "laps": int(len(x)),
                              "t": _r(t[j], 2), "p_family": _r(padj[j], 4), "confidence": conf,
                              "practical_deg": _r(prac, 1), "fit_margin_deg": _r(mg, 1)})
            base["cross_check"] = {"kind": "lateral acceleration (LatAccel / speed squared)", "agree": agree, "of": of}
        for c in cells:
            c["text"] = _cell_text(c)
        base["cells"] = cells
        base["findings"] = sorted([c for c in cells if c["label"] in ("push", "rotate")],
                                  key=lambda c: (c["p_family"], -abs(c["median_deg"] or 0)))
        ev = _countersteer(P, corners, masks, G, L, prefix)
        byc = {}
        for e in ev:
            nm = e["name"] or "between corners"
            b = byc.setdefault(nm, {"events": 0, "laps": set(), "phases": {}})
            b["events"] += 1
            b["laps"].add(e["lap"])
            if e["phase"]:
                b["phases"][e["phase"]] = b["phases"].get(e["phase"], 0) + 1
        base["countersteer"] = {"events": ev, "by_corner": {k: {"events": v["events"], "laps": len(v["laps"]),
                                                                "of_laps": len(P), "phases": v["phases"]}
                                                            for k, v in byc.items()}}
        wb = WHEELBASE_M.get(str(info.get("car_path") or "").lower())
        base["car_fit"] = {"G": _r(G, 1), "G_ci": [_r(x, 1) for x in fit["G_ci"]], "K": _r(K, 1),
                           "K_ci": [_r(x, 1) for x in fit["K_ci"]], "samples": fit["samples"], "kind": "yaw rate",
                           "ratio_estimate": _r(G / 57.2958 / wb[0], 1) if wb else None,
                           "wheelbase_source": wb[1] if wb else None,
                           "text": _fit_text(G, K, wb)}
        base["summary"] = summary_text(base)
        base["ok"] = True
        n = len(base["findings"])
        base["message"] = (f"{n} place{'s' if n != 1 else ''} where the balance is clear, from {len(P)} clean laps."
                           if n else f"No place shows a clear push or rotation yet, from {len(P)} clean laps.")
        if len(set(x.get("setup") for x in laps if x.get("setup"))) > 1:
            base["notes"].append("These laps were driven on more than one setup, so the map mixes them. Pick one "
                                 "setup's files for a cleaner read.")
        if setup is not None:
            base["setup_ideas"] = setup_ideas(base, setup, series_setup=series_setup)
        return base
    except Exception as e:  # noqa: BLE001
        base["ok"] = False
        base["message"] = f"The balance map could not read these laps ({str(e)[:160]})."
        base["summary"] = base["message"]
        return base


def _guess_length(laps) -> float | None:
    if not laps:
        return None
    x = laps[0]
    rate = float(x.get("rate") or 60)
    return float(np.sum(np.asarray(x["v"], float)) / rate) or None


def _clean_info(info: dict) -> dict:
    return {k: v for k, v in info.items() if k != "setup"}


def _method() -> dict:
    return {"index": "steering used minus the steering this car needs in gentle bends (learned from your laps), "
                     "in steering-wheel degrees; positive = pushes, negative = rotates",
            "curvature": "yaw rate / speed (cross-check: lateral acceleration / speed squared)",
            "filter": f"{ORDER}th-order Butterworth response at {FC_HZ:g} Hz, forwards and backwards",
            "ignored": f"under {MIN_KMH:g} km/h, under {EVAL_G:g} g, within {KERB_PAD_S:g} s of a kerb strike",
            "test": (f"sign-flip permutation across laps with the fitting error of the car's normal steering in the "
                     f"null, whole map at once (family-wise {ALPHA:g}); at least {PRACT_DEG:g} degrees and "
                     f"{PRACT_FRAC:.0%} of the steering used, and more than the fitting error alone could do"),
            "neutral": f"{CI_LEVEL:.0%} interval of the median inside the practical size",
            "min_laps": MIN_LAPS}


def _fit_text(G, K, wb) -> str:
    s = (f"Learned from your laps: in gentle bends this car needs about {abs(K):.0f} degrees of steering wheel "
         f"per g {'more' if K >= 0 else 'less'} than the pure geometry")
    if wb:
        s += (f"; with a {wb[0]:.2f} m wheelbase ({wb[1]}) that is a steering ratio of about "
              f"{abs(G) / 57.2958 / wb[0]:.0f} to 1")
    else:
        s += "; the steering ratio and wheelbase are not in the telemetry, so their product was learned instead"
    return s + "."


def _cell_text(c: dict) -> str:
    if c["label"] in ("push", "rotate"):
        more = "more" if c["label"] == "push" else "less"
        return (f"{c['corner']} {c['phase_words']}: the car {c['label_words']}. You used about "
                f"{abs(c['median_deg']):.0f} degrees {more} steering than this car normally needs there "
                f"({c['laps']} laps, {c['confidence']}).")
    if c["label"] == "neutral":
        return f"{c['corner']} {c['phase_words']}: neutral ({c['laps']} laps)."
    return f"{c['corner']} {c['phase_words']}: not clear yet ({c['laps']} laps)."


def _names(cs) -> str:
    seen = {}
    for c in cs:
        seen.setdefault(c["corner"], c.get("corner_index", 0))
    return ", ".join(sorted(seen, key=lambda k: seen[k]))


def summary_text(rep: dict) -> str:
    """One or two plain sentences: 'The car pushes in the middle of slow corners (T4, T6) and is neutral
    elsewhere.'"""
    cells = rep.get("cells") or []
    if not cells:
        return rep.get("message") or "Not enough laps to read the car's balance yet."
    groups = {}
    for c in rep.get("findings") or []:
        groups.setdefault((c["label"], c["phase"], c["class"]), []).append(c)
    verbs = {"push": "pushes", "rotate": "rotates"}
    parts = []
    for (lab, ph, cls), cs in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        parts.append(f"{verbs[lab]} {PHASE_OF[ph]} {cls} corners ({_names(cs)})")
    neutral = [c for c in cells if c["label"] == "neutral"]
    unclear = [c for c in cells if c["label"] == "unclear"]
    if parts:
        s = "The car " + (", ".join(parts[:-1]) + " and " + parts[-1] if len(parts) > 1 else parts[0])
        if neutral and not unclear:
            s += " and is neutral elsewhere."
        elif neutral:
            s += (f". Elsewhere it is neutral in {len(neutral)} of {len(neutral) + len(unclear)} places and not "
                  f"clear yet in the rest.")
        else:
            s += ". Elsewhere it is not clear yet."
    elif neutral and not unclear:
        s = "The car is neutral everywhere it could be measured."
    elif neutral:
        s = (f"No clear push or rotation. The car is neutral in {len(neutral)} of {len(cells)} places; the rest "
             f"are not clear yet.")
    else:
        s = "No clear push or rotation yet: more clean laps would show it."
    cs = (rep.get("countersteer") or {}).get("by_corner") or {}
    many = sorted(((k, v) for k, v in cs.items() if k != "between corners"), key=lambda kv: -kv[1]["events"])
    if many:
        k, v = many[0]
        s += (f" You had to catch the rear (countersteer) {v['events']} time{'s' if v['events'] != 1 else ''} at {k}"
              f" ({v['laps']} of {v['of_laps']} laps).")
    return s


# ---------------------------------------------------------------- setup ideas
_ARB = ("ArbSetting", "ArbBlades", "AntiRollBar", "ArbStiffness", "ArbSize", "Arb")
_BIAS = ("BrakePressureBias", "BrakeBias", "FrontBrakeBias")
_TC = ("TcSetting", "Tc1_Long", "TractionControl", "TcGain", "Tc1Setting")
_WING = ("WingAngle", "RearWingAngle", "WingSetting", "RearWing")


def _find_key(flat: dict, leaves, side: str | None = None):
    for k, v in flat.items():
        parts = k.split(".")
        if parts[-1] not in leaves:
            continue
        sec = parts[-2] if len(parts) >= 2 else ""
        if side == "front" and not sec.lower().startswith("front"):
            continue
        if side == "rear" and not sec.lower().startswith("rear"):
            continue
        return k, v
    return None, None


def _step(key: str, text: str, way: int):
    """(new text value, step) one step `way` (+1 up, -1 down) from the current value; None if not a number."""
    from . import setuplab
    s = setuplab._num_text(text)
    if s is None:
        return None
    dec = setuplab._decimals(s)
    leaf = key.rsplit(".", 1)[-1]
    step = 0.5 if (leaf in _BIAS or (leaf in _WING and dec)) else (1.0 if dec == 0 else 10 ** -dec)
    new = float(s) + way * step
    if leaf in _ARB + _TC and new < 1:
        return None                                  # already at the lowest number (the range isn't in the file)
    return (f"{new:.{max(dec, 1 if step == 0.5 else 0)}f}" if dec or step == 0.5 else f"{int(round(new))}"), s


TARGETS = {
    "front_arb": (_ARB, "front", "Front anti-roll bar", {-1: "one step softer", 1: "one step stiffer"}),
    "rear_arb": (_ARB, "rear", "Rear anti-roll bar", {-1: "one step softer", 1: "one step stiffer"}),
    "bias": (_BIAS, None, "Brake bias", {-1: "a little more to the rear", 1: "a little more to the front"}),
    "tc": (_TC, None, "Traction control", {1: "one step more", -1: "one step less"}),
    "wing": (_WING, None, "Rear wing", {-1: "one step less", 1: "one step more"}),
}
# (phase, label, speed class or None) -> options in order; the first one the car has is suggested
IDEAS = {
    ("mid", "push", "slow"): [("front_arb", -1), ("rear_arb", 1)],
    ("mid", "push", "medium"): [("front_arb", -1), ("rear_arb", 1)],
    ("mid", "push", "fast"): [("wing", -1), ("front_arb", -1)],
    ("mid", "rotate", "slow"): [("rear_arb", -1), ("front_arb", 1)],
    ("mid", "rotate", "medium"): [("rear_arb", -1), ("front_arb", 1)],
    ("mid", "rotate", "fast"): [("wing", 1), ("rear_arb", -1)],
    ("entry", "push", None): [("bias", -1), ("front_arb", -1)],
    ("entry", "rotate", None): [("bias", 1), ("rear_arb", -1)],
    ("exit", "push", None): [("front_arb", -1), ("rear_arb", 1)],
    ("exit", "rotate", None): [("rear_arb", -1), ("tc", 1)],
}
WHY = {
    "front_arb": "a softer front bar (or stiffer rear) lets the front tyres grip more in the corner",
    "rear_arb": "a softer rear bar lets the rear tyres grip more (a stiffer one frees the rear to turn)",
    "bias": "brake bias moves braking work between the axles, which changes how the car turns in",
    "tc": "more traction control holds the rear when you get on the throttle",
    "wing": "the rear wing sets how much the rear grips at speed compared with the front",
}
NUMBER_CAVEAT = ("On most cars in iRacing a higher number is stiffer (or more), but check the garage's own help "
                 "text for this car.")
THEN = "Change only this, then drive at least 6 clean laps and compare again in Setup Lab."


def _option(flat, target, way):
    leaves, side, lab, words = TARGETS[target]
    key, val = _find_key(flat, leaves, side)
    if key is None:
        return None
    st = _step(key, str(val), way)
    unit = ibt.unit(val)
    if st is None:
        why = ("which may already be the lowest" if ibt.number(val) is not None else "which isn't a number to step")
        return {"target": target, "key": key, "label": lab, "from": str(val), "to": None,
                "text": f"{lab}: {words[way]} (it is set to {val}, {why})"}
    new, old = st
    return {"target": target, "key": key, "label": lab, "from": old, "to": new, "unit": unit,
            "text": f"{lab} {old} → {new}{(' ' + unit) if unit else ''} ({words[way]})"}


def setup_ideas(report: dict, setup: dict | None, series_setup=None) -> list:
    """Conventional, one-change-at-a-time setup ideas for the clearest balance findings (most certain first).

    idea: {"finding": cell text, "corner", "phase", "label", "change": {"key","label","from","to","text"} | None,
           "alternative": same | None, "why", "confidence" ("worth a try" | "a guess"), "text", "then"}
    A fixed-setup series gives no ideas (a note instead). Never raises."""
    try:
        if str(series_setup or "").lower() == "fixed":
            return [{"finding": None, "change": None, "alternative": None, "confidence": None,
                     "text": "This series uses a fixed setup, so the balance can't be changed in the garage: adapt "
                             "the driving (where you brake and how fast you add throttle) instead."}]
        flat = ibt.flatten(setup or {})
        out, seen = [], set()
        for f in (report or {}).get("findings") or []:
            opts = IDEAS.get((f["phase"], f["label"], f["class"])) or IDEAS.get((f["phase"], f["label"], None)) or []
            found = [o for o in (_option(flat, t, w) for t, w in opts) if o is not None]
            found.sort(key=lambda o: o["to"] is None)          # a change that can actually be made first
            key = (f["phase"], f["label"], f["class"] if f["phase"] == "mid" else None)
            if key in seen:
                continue
            seen.add(key)
            same = [c for c in report["findings"] if (c["phase"], c["label"]) == (f["phase"], f["label"])
                    and (f["phase"] != "mid" or c["class"] == f["class"])]
            where = f"{PHASE_OF[f['phase']]} {_names(same)}"
            verb = "pushes" if f["label"] == "push" else "rotates"
            conf = "worth a try" if f["confidence"] == "clear" else "a guess"
            first = found[0] if found else None
            alt = found[1] if len(found) > 1 else None
            if first:
                text = (f"The car {verb} {where}. Try one change: {first['text']}. Why: "
                        f"{WHY[first['target']]}.")
                if alt:
                    text += f" If that doesn't help, put it back and try {alt['text'][0].lower() + alt['text'][1:]}."
                if (f["phase"], f["label"]) == ("entry", "rotate"):
                    text += (" Rotation on the way in also comes from how much brake you carry into the turn: "
                             "if you like how it turns in, leave it.")
                text += " " + NUMBER_CAVEAT + " " + THEN
            else:
                lab = TARGETS[opts[0][0]][2].lower() if opts else "balance"
                text = (f"The car {verb} {where}. The usual fix is the {lab}, but this car's setup doesn't show one "
                        f"to change.")
            out.append({"finding": f["text"], "corner": _names(same), "phase": f["phase"], "label": f["label"],
                        "change": first, "alternative": alt, "why": WHY[first["target"]] if first else None,
                        "confidence": conf, "text": text, "then": THEN, "_n": len(same)})
        out.sort(key=lambda i: (i["confidence"] != "worth a try", i["change"] is None or i["change"]["to"] is None,
                                -i["_n"]))
        for k, i in enumerate(out):
            i.pop("_n")
            i["first"] = k == 0
        return out
    except Exception:  # noqa: BLE001
        return []


def analyze_files(paths, series_setup=None, prefix: str = "T") -> dict:
    """analyze_laps over the clean laps of one or more .ibt files, with setup ideas from the newest file's setup."""
    try:
        laps, info = load_laps(paths)
    except Exception as e:  # noqa: BLE001
        laps, info = [], {"error": str(e)[:160]}
    return analyze_laps(laps, info=info, setup=info.get("setup"), series_setup=series_setup, prefix=prefix)
