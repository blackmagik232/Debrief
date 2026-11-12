"""Car balance map (2.15): where the car pushes (understeer) or rotates (oversteer), per corner and phase, from
your own iRacing telemetry (.ibt files), and one conventional setup change to try for the clearest finding.

How balance is measured (the understeer index, in steering-wheel degrees):
  - The steering the car "should" need if the tyres were not sliding at all is wheelbase x curvature x steering
    ratio. Neither the wheelbase in iRacing nor the steering ratio is in the telemetry, so the product of the two
    (G) is learned from your own laps, together with the car's normal gentle-bend understeer (K, steering per g):
        steering = G x curvature + K x lateral g        (fitted on 0.10 to 0.40 g, above 40 km/h, no kerbs)
    Only steady driving goes into the fit (brake off, wheel moving slower than 30 degrees a second), so the
    rotation you get from trail braking can't bend it. K is only learned when those bends were driven at different
    speeds (FIT_SPEED_MIN); below FIT_SPEED_SPREAD the fit with K = 0 is a second opinion in the fitting margin.
    With the wheelbase in WHEELBASE_M, G becomes a ratio.
  - A real car's rotation follows the wheel a few hundredths of a second later (0.07 to 0.08 s on his McLaren
    files). That lag is measured per file (the shift, 0 to LAG_MAX_S, where steering and yaw curvature line up best)
    and the yaw and sideways g are shifted by it, so each moment's steering is compared with the rotation it made.
    Without this, a 0.07 s lag made noise sessions call "pushes" in about half of them.
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
  - Entry is compared with his own typical corner entry (the median of that lap's entries), not with the brake-off
    fit: under braking every car turns in with less steering than in steady corners, so an absolute reading said
    "rotates on the way in" almost everywhere. A corner's way in is called only when it differs from his typical
    entry by the practical size; the typical entry itself is reported as a plain note.
  - Countersteer: the steering points the other way to the car's (lag-shifted) rotation for at least 0.25 s (above
    40 km/h, 8 degrees of steering reaching 12, 0.15 rad/s of yaw), and the rotation isn't falling away through zero
    for most of it (that is a smooth change of direction, the car following the wheel). Each one is an oversteer
    moment you had to catch.

Honest statistics (2.13's review lesson): one number per lap per corner and phase (the median excess), so laps are
the units. A place is called "pushes" or "rotates" only when
  (1) a sign-flip permutation test across laps (the "shuffle" null: each lap's numbers flipped together, so the
      family of all corners and phases is tested at once, max-|t|, step-down) gives a family-wise p below 0.05,
      The fit of G and K is shared by every lap, so an error in it moves every lap the same way, which flipping laps
      can't see. So each shuffled pattern also gets the effect of one bootstrap refit (whole laps resampled), and
      the null carries both kinds of noise. (Without this, noisy test sessions made false calls in 15 % of them.)
  (2) the median is at least the practical size (PRACT_ROAD_DEG at the front wheels, times the steering ratio
      learned from G, and PRACT_FRAC of the steering used there), and
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
    setup_ideas(report, setup, series_setup=None, last_change=None, dials=None) -> [idea]
    lowpass(x, rate, fc)                -> zero-phase low-pass
    WHEELBASE_M                         -> road-car wheelbases we could source (only used to turn G into a ratio)
"""
from __future__ import annotations

import math
import re
from itertools import product
from pathlib import Path

import numpy as np

from . import engine, ibt

VERSION = 2                 # 2 (2.18): info["dials"] (the car's in-car dials, for a fixed-setup series)
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

PRACT_ROAD_DEG = 0.5        # practical size: at least half a degree at the front wheels (in steering-wheel degrees:
                            # x the car's steering ratio, learned from G; about 10 degrees of wheel at 21 to 1) ...
PRACT_FRAC = 0.20           # ... and at least 20 % of the steering used there (a car whose steering quickens by 10 %
                            # in hairpins must not read as "pushes")
WB_DEFAULT_M = 2.6          # wheelbase assumed for that ratio when the car's isn't in WHEELBASE_M
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
FIT_SPEED_MIN = 1.1         # gentle bends must span this speed ratio (90th / 10th percentile) to learn K at all (else
                            # K = 0) ...
FIT_SPEED_SPREAD = 1.5      # ... and this much before K is trusted: below it, the fit with K = 0 is a second opinion and
                            # its difference goes into the fitting margin (G and K trade off when the bends are at
                            # one speed, which moves slow corners the most)
IV_MAX_REL = 0.15           # the ordinary and the instrumented fit of G may differ this much (share); more = the yaw channel is
                            # too noisy to call a place, and nothing is labelled push or rotate
YAW_MEDIAN_S = 0.25         # the yaw rate goes through a running median this long before the low-pass (bump blips)
LAG_MAX_S = 0.20            # the car's yaw follows the steering this much later at most (measured per file)
ENTRY_MIN_CORNERS = 3       # corner entries a lap needs before "his typical entry" is worked out for it

CS_DEG = 8.0                # countersteer: steering at least this many degrees the "wrong" way ...
CS_PEAK_DEG = 12.0          # ... reaching at least this much opposite lock ...
CS_YAW = 0.15               # ... while rotating at least this fast (rad/s, compared with the yaw the steering
                            #     produces: shifted by the measured lag) ...
CS_MIN_S = 0.25             # ... for at least this long ...
CS_FALL = 0.5               # ... and not a smooth change of direction: the rotation falling (|yaw| dropping faster than
CS_FALL_FRAC = 0.75         #     this, rad/s^2) for this share of the time or more is the car already following the wheel

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
            "VertAccel", "LFshockVel", "RFshockVel", "LRshockVel", "RRshockVel", "LFspeed", "RFspeed", "LRspeed", "RRspeed")


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


def _wheel_speeds(col: dict, a: int, b: int) -> dict:
    """2.23: the mean rear and front wheel speeds of a lap (m/s), when the file has all four wheel-speed channels
    (the handling read uses them to spot wheelspin). {} otherwise."""
    if all(c in col for c in ("LFspeed", "RFspeed", "LRspeed", "RRspeed")):
        return {"wsr": (col["LRspeed"][a:b] + col["RRspeed"][a:b]) / 2.0,
                "wsf": (col["LFspeed"][a:b] + col["RFspeed"][a:b]) / 2.0}
    return {}


def load_laps(src) -> tuple:
    """Clean laps from one .ibt path or a list (one car and track), as time series. Never raises.

    lap: {"file", "lap", "time", "rate", "pct", "v" m/s, "sw" deg, "r" rad/s, "ay" m/s^2, "b" %, "th" %, "kerb"}
    info: {"files", "skipped", "car", "car_path", "track", "track_code", "length_m", "setups": [fingerprint...],
           "setup": newest file's CarSetup, "missing": [channel...]}"""
    from . import setuplab
    paths = src if isinstance(src, (list, tuple)) else [src]
    laps = []
    info = {"files": [], "skipped": [], "car": None, "car_path": None, "track": None, "track_code": None,
            "length_m": None, "setups": [], "setup": None, "missing": [], "dials": []}
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
                for dial, chans in DIAL_CHANNELS.items():
                    if dial not in info["dials"] and any(ib.has(c) for c in chans):
                        info["dials"].append(dial)
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
                                 "kerb": kerb[a:b], "setup": fp, "session": x.get("session")}
                                | _wheel_speeds(col, a, b))
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
        # yaw rate: a short running median first (bump blips of 0.1 to 0.15 s are removed, smooth turns are not
        # changed). Noise in the yaw channel biases the fit of the car's normal steering toward "understeer".
        r = lowpass(_running_median(np.asarray(lap["r"], float), max(3, int(round(rate * YAW_MEDIAN_S)))), rate)
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
                "dsw": np.gradient(np.abs(sw)) * rate, "file": lap.get("file"), "lag": 0}
    except Exception:  # noqa: BLE001
        return None


def _shift(x, n: int) -> np.ndarray:
    """x moved n samples earlier (x[t + n] at t), the last value held at the end."""
    x = np.asarray(x, float)
    if n <= 0 or len(x) <= n:
        return x
    return np.concatenate([x[n:], np.repeat(x[-1:], n)])


def _apply_lag(p: dict, n: int) -> dict:
    """Compare the steering at t with the yaw (and sideways g) it produces n samples later."""
    vs = np.maximum(p["v"], 1.0)
    r, ay = _shift(p["r"], n), _shift(p["ay"], n)
    q = dict(p)
    q.update(r=r, ay=ay, k_yaw=r / vs, k_ay=ay / vs ** 2, lag=int(n))
    return q


def _lag_corr(P: list, steady: bool) -> tuple:
    """(best shift in samples, correlation there) of steering against the later yaw curvature. steady=True: only
    brake-off samples."""
    rate = float(P[0]["rate"])
    best, best_c = 0, None
    for n in range(0, int(round(LAG_MAX_S * rate)) + 1):
        a, b = [], []
        for p in P:
            m = len(p["sw"]) - n
            if m < 10:
                continue
            ok = p["ok"][:m] & p["ok"][n:]
            if steady:
                ok &= (p["b"] <= BRAKE_ON)[:m]
            a.append(p["sw"][:m][ok])
            b.append(p["k_yaw"][n:][ok])
        if not a:
            continue
        a, b = np.concatenate(a), np.concatenate(b)
        if len(a) < 100 or np.std(a) == 0 or np.std(b) == 0:
            continue
        c = abs(float(np.corrcoef(a, b)[0, 1]))
        if best_c is None or c > best_c + 1e-12:
            best, best_c = n, c
    return best, best_c


def estimate_lag(P: list) -> tuple:
    """(samples, seconds, correlation): how much later the yaw follows the steering on these laps (one file), the
    shift between 0 and LAG_MAX_S where steering and yaw curvature line up best (cross-correlation over every
    sample above MIN_KMH and off the kerbs). On his real McLaren files: 4 or 5 samples, about 0.07 s.
    2.23.2: it is worked out twice, on every sample and on brake-off samples only. A real problem on the way in
    (braking) can pull the first one; when the two disagree by more than 2 samples the smaller shift is used."""
    if not P:
        return 0, 0.0, None
    rate = float(P[0]["rate"])
    best, best_c = _lag_corr(P, False)
    if sum(int(((p["b"] <= BRAKE_ON) & p["ok"]).sum()) for p in P) >= 400:
        b2, _c2 = _lag_corr(P, True)
        if abs(b2 - best) > 2:
            best = min(best, b2)
    return best, best / rate, best_c


def _lag_compensate(P: list) -> tuple:
    """Each file's laps shifted by that file's own steering-to-yaw lag. -> (laps, {file: seconds})."""
    by = {}
    for i, p in enumerate(P):
        by.setdefault(p.get("file"), []).append(i)
    out, lags = list(P), {}
    for f, idx in by.items():
        n, sec, _c = estimate_lag([P[i] for i in idx])
        lags[str(f)] = round(sec, 3)
        for i in idx:
            out[i] = _apply_lag(P[i], n)
    return out, lags


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
    """(steer, curvature, lat g, lap index, speed) arrays from the gentle-bend band of every lap (steady driving
    only, unless steady=False: then every gentle-bend sample, the sensitivity check)."""
    S, K, A, I, V = [], [], [], [], []
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
        V.append(p["v"][m])
    if not S:
        return np.zeros(0), np.zeros(0), np.zeros(0), np.zeros(0, int), np.zeros(0)
    return np.concatenate(S), np.concatenate(K), np.concatenate(A), np.concatenate(I), np.concatenate(V)


def speed_spread(v) -> float:
    """90th / 10th percentile of the speeds the gentle bends were driven at (1.0 = all at one speed)."""
    v = np.asarray(v, float)
    if len(v) < 10:
        return 1.0
    lo, hi = np.percentile(v, [10, 90])
    return float(hi / lo) if lo > 0 else 1.0


def _robust_fit(sw, k, a, use_k: bool = True) -> tuple | None:
    """steer = G * curvature + K * lat g, through zero, with Huber weights (a few slides can't drag it).
    use_k=False: K = 0 (gentle bends all at about one speed, where curvature and lateral g say the same thing and
    the two can't be told apart)."""
    if len(sw) < 50:
        return None
    X = np.column_stack([k, a]) if use_k else np.column_stack([k])
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
    return float(beta[0]), (float(beta[1]) if use_k else 0.0)


def fit_car(P: list, kind: str = "yaw", steady: bool = True, boot: bool = True, k_zero: bool = False) -> dict | None:
    """G (steering degrees per 1/m of curvature) and K (steering degrees per g), with a lap-bootstrap interval.
    K is only learned when the gentle bends span FIT_SPEED_MIN in speed (or k_zero=False); otherwise K = 0 (said in
    "k_learned"). "k_trusted": they span FIT_SPEED_SPREAD."""
    sw, k, a, idx, v = _fit_samples(P, kind, steady)
    spread = speed_spread(v)
    use_k = spread >= FIT_SPEED_MIN and not k_zero
    est = _robust_fit(sw, k, a, use_k)
    if est is None or abs(est[0]) < 1.0:
        return None
    rng = np.random.default_rng(PERM_SEED)
    n = len(P)
    by = [np.nonzero(idx == i)[0] for i in range(n)]
    boots = []
    for _ in range(N_FIT_BOOT if boot else 0):
        pick = rng.integers(0, n, n)
        sel = np.concatenate([by[i] for i in pick])
        e = _robust_fit(sw[sel], k[sel], a[sel], use_k)
        if e is not None:
            boots.append(e)
    boots = np.array(boots) if boots else np.array([est])
    lo, hi = np.percentile(boots[:, 0], [5, 95])
    return {"G": est[0], "K": est[1], "G_ci": [float(lo), float(hi)],
            "K_ci": [float(x) for x in np.percentile(boots[:, 1], [5, 95])],
            "samples": int(len(sw)), "boots": boots, "speed_spread": round(spread, 2), "k_learned": bool(use_k),
            "k_trusted": bool(use_k and spread >= FIT_SPEED_SPREAD)}


def fit_iv(P: list, use_k: bool = True) -> tuple | None:
    """(G, K) fitted with the sideways-g curvature (LatAccel / speed squared) as the instrument for the yaw
    curvature. Noise in the yaw rate makes the ordinary fit read G too small (the car then looks like it understeers
    everywhere); this fit does not have that bias, so its difference from the ordinary fit is part of the margin."""
    S, Zk, Xk, A = [], [], [], []
    for p in P:
        g = np.abs(p["ay"]) / G0
        m = (p["ok"] & (g >= FIT_G[0]) & (g <= FIT_G[1]) & (np.abs(p["k_yaw"]) >= MIN_CURV)
             & (p["b"] <= BRAKE_ON) & (np.abs(p["dsw"]) <= STEADY_DEG_S))
        S.append(p["sw"][m]); Xk.append(p["k_yaw"][m]); Zk.append(p["k_ay"][m]); A.append(p["ay"][m] / G0)
    if not S:
        return None
    sw, xk, zk, a = (np.concatenate(x) for x in (S, Xk, Zk, A))
    if len(sw) < 50:
        return None
    try:
        if use_k:
            X, Z = np.column_stack([xk, a]), np.column_stack([zk, a])
            beta = np.linalg.solve(Z.T @ X, Z.T @ sw)
            return float(beta[0]), float(beta[1])
        return float(np.sum(zk * sw) / np.sum(zk * xk)), 0.0
    except (np.linalg.LinAlgError, FloatingPointError):
        return None


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


def _relative_entry(vals: dict) -> tuple:
    """Entry places compared with his own typical entry: each lap's entry numbers minus the median of that lap's
    corner entries (laps with fewer than ENTRY_MIN_CORNERS entries give no entry numbers). Under braking every car
    turns in with less steering than in steady corners (the weight is on the front), and the gentle-bend fit is
    brake-off, so an absolute entry reading would say "rotates" almost everywhere. -> (vals, typical per lap)."""
    ek = [k for k in vals if k[1] == "entry"]
    if not ek:
        return vals, None
    M = np.column_stack([vals[k] for k in ek])
    ok = np.isfinite(M).sum(axis=1) >= ENTRY_MIN_CORNERS
    typ = np.full(M.shape[0], np.nan)
    if ok.any():
        typ[ok] = np.nanmedian(M[ok], axis=1)
    out = dict(vals)
    for k in ek:
        out[k] = vals[k] - typ
    return out, typ


def _cell_values(P, cidx, fits, kind="yaw"):
    """{(corner index, phase): [per-lap median excess or nan]} (entry: relative to his typical entry on that lap),
    the steering used there (median), and the typical entry per lap (absolute, or None).
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
    vals, typ = _relative_entry(vals)
    return vals, {k: (float(np.median(v)) if v else None) for k, v in us.items()}, typ


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
    # step-down (Westfall and Young): the clearest place is tested against the maximum over every place, the next
    # against the maximum over the places left, and so on, never smaller than the one before. Same family-wise
    # error, but one very clear place no longer hides a second one.
    order = np.argsort(-np.abs(t_obs), kind="stable")
    p = np.empty(n_c)
    prev = 0.0
    for rank, j in enumerate(order):
        mx = T[:, order[rank:]].max(axis=1)
        prev = max(prev, float(np.sum(mx >= abs(t_obs[j]) - 1e-12)) / len(S))
        p[j] = prev
    return t_obs, p


def _speed_class(kmh: float) -> str:
    return "slow" if kmh < SLOW_KMH else ("fast" if kmh >= FAST_KMH else "medium")


# ---------------------------------------------------------------- countersteer
def _countersteer(P, corners, masks, G, L, prefix):
    """Moments the steering pointed against the car's rotation: a catch. P must be lag-compensated (each lap's yaw
    shifted by its file's measured lag), so in a smooth change of direction, where the yaw simply follows the wheel
    a few hundredths of a second later, the two still point the same way. What is left is also checked: the opposite
    lock must reach CS_PEAK_DEG, last CS_MIN_S, and the rotation must not be falling away through zero for most of it
    (that is the car already following the wheel, not a slide being held)."""
    out = []
    sgn = 1.0 if G >= 0 else -1.0
    for i, p in enumerate(P):
        swa = p["sw"] * sgn
        r = p["r"]
        bad = p["ok"] & (np.abs(r) >= CS_YAW) & (np.abs(swa) >= CS_DEG) & (swa * r < 0)
        if not bad.any():
            continue
        edges = np.diff(np.concatenate([[0], bad.astype(int), [0]]))
        starts, ends = np.nonzero(edges == 1)[0], np.nonzero(edges == -1)[0]
        for s, e in zip(starts, ends):
            if (e - s) / p["rate"] < CS_MIN_S:
                continue
            if float(np.max(np.abs(swa[s:e]))) < CS_PEAK_DEG:
                continue
            fall = np.gradient(np.abs(r[s:e])) * p["rate"] < -CS_FALL
            if float(np.mean(fall)) >= CS_FALL_FRAC:
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
                        "phase": phase, "duration_s": round(float((e - s) / p["rate"]), 2),
                        "peak_deg": round(float(np.max(np.abs(swa[s:e])))),
                        "kmh": round(float(p["v"][s]) * KMH), "d_m": round(float(p["d"][s]))})
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
            "entry_typical": None, "entry_note": None,
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
            base["message"] = (f"{'Only ' + _plural(len(laps), 'clean lap') if laps else 'No clean laps'}"
                               f" with steering and yaw rate (how fast the car is turning). The balance map needs at "
                               f"least {MIN_LAPS}.")
            base["summary"] = base["message"]
            return base
        P, keep = [], []
        for x in laps:
            p = _prep(x, L)
            if p is not None:
                P.append(p)
                keep.append(x)
        laps = keep
        P, lags = _lag_compensate(P)
        base["laps"] = len(P)
        if len(P) < MIN_LAPS:
            base["message"] = (f"Only {_plural(len(P), 'lap')} could be read. The balance map needs at least "
                               f"{MIN_LAPS}.")
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
        wb = WHEELBASE_M.get(str(info.get("car_path") or "").lower())
        floor = PRACT_ROAD_DEG * abs(G) / 57.2958 / (wb[0] if wb else WB_DEFAULT_M)
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
        vals, used, typ = _cell_values(P, cell_masks, [(G, K)] * nP)
        if typ is not None and np.isfinite(typ).sum() >= MIN_LAPS:
            te = float(np.nanmedian(typ))
            base["entry_typical"] = {"median_deg": _r(te, 1), "laps": int(np.isfinite(typ).sum())}
            base["entry_note"] = (
                "Under braking a car usually turns in with less steering than in steady corners; here your typical "
                "corner entry needs about the same, and each corner's way in is compared with that typical entry."
                if abs(te) < 1.0 else
                "Under braking a car always turns in with "
                + ("less" if te < 0 else "more") + " steering than in steady corners (on your typical corner entry "
                f"here, about {_plural(round(abs(te)), 'degree')} {'less' if te < 0 else 'more'}), so each corner's "
                "way in is compared with your own typical entry, not with steady cornering.")
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
            fit_i = fit_iv(P, fit["k_learned"])
            unreliable = bool(fit_i and abs(fit_i[0]) > 1.0 and abs(fit_i[0] - G) / abs(G) > IV_MAX_REL)
            if unreliable:
                base["notes"].append("The yaw-rate signal is noisy enough that the car's normal steering cannot be "
                                     "learned reliably, so no place is called a push or a rotation from these laps.")
            if not fit["k_trusted"]:
                # gentle bends at about one speed: G and K can't be told apart well, so the fit with K = 0 is a
                # second opinion, and its difference goes into the margin
                fit_c = fit_car(P, "yaw", boot=False, k_zero=True)
                if fit_c:
                    vals_c = _cell_values(P, cell_masks, [(fit_c["G"], 0.0)] * nP)[0]
                    model_c = np.array([abs(float(np.nanmean(vals_c[key] - vals[key])))
                                        if np.isfinite(vals_c[key] - vals[key]).any() else 0.0 for key in keys])
                    margin = np.maximum(margin, model_c)
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
                prac = max(floor, PRACT_FRAC * u)
                ci, ph = key
                label = "unclear"
                mg = float(margin[j])
                xb = vals_b[key][np.isfinite(vals_b[key])]
                med_b = float(np.median(xb)) if len(xb) else 0.0
                if (not unreliable and padj[j] < ALPHA and abs(med) >= prac and abs(med) > mg and np.sign(med_b) == np.sign(med)
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
        base["car_fit"] = {"G": _r(G, 1), "G_ci": [_r(x, 1) for x in fit["G_ci"]], "K": _r(K, 1),
                           "K_ci": [_r(x, 1) for x in fit["K_ci"]], "samples": fit["samples"], "kind": "yaw rate",
                           "ratio_estimate": _r(G / 57.2958 / wb[0], 1) if wb else None,
                           "wheelbase_source": wb[1] if wb else None, "yaw_lag_s": lags,
                           "speed_spread": fit["speed_spread"], "k_learned": fit["k_learned"],
                           "practical_floor_deg": _r(floor, 1),
                           "text": _fit_text(G, K, wb, lags, fit["k_learned"])}
        base["summary"] = summary_text(base)
        base["ok"] = True
        n = len(base["findings"])
        base["message"] = (f"{_plural(n, 'place')} where the balance is clear, from {_plural(len(P), 'clean lap')}."
                           if n else f"No place shows a clear push or rotation yet, from {_plural(len(P), 'clean lap')}.")
        if len(set(x.get("setup") for x in laps if x.get("setup"))) > 1:
            base["notes"].append("These laps were driven on more than one setup, so the map mixes them. Pick one "
                                 "setup's files for a cleaner read.")
        if setup is not None:
            base["setup_ideas"] = setup_ideas(base, setup, series_setup=series_setup, dials=info.get("dials"))
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
                     f"null, whole map at once (family-wise {ALPHA:g}); at least {PRACT_ROAD_DEG:g} degrees at the "
                     f"front wheels (times the car's steering ratio) and {PRACT_FRAC:.0%} of the steering used, and "
                     f"more than the fitting error alone could do"),
            "entry": "the way in is compared with his own typical corner entry on the same lap (braking always "
                     "turns the car in more than steady cornering)",
            "lag": f"the yaw is compared with the steering that produced it: shifted by the lag measured per file "
                   f"(0 to {LAG_MAX_S:g} s)",
            "neutral": f"{CI_LEVEL:.0%} interval of the median inside the practical size",
            "min_laps": MIN_LAPS}


def _plural(n, word: str) -> str:
    n = int(n)
    return f"{n} {word}{'' if n == 1 else 's'}"


def _fit_text(G, K, wb, lags=None, k_learned=True) -> str:
    if k_learned:
        s = (f"Learned from your laps: in gentle bends this car needs about {_plural(round(abs(K)), 'degree')} of "
             f"steering wheel per g {'more' if K >= 0 else 'less'} than the pure geometry")
    else:
        s = ("Learned from your laps: your gentle bends were all at about the same speed, so only the steering for "
             "each bend is learned, not how it changes with sideways g")
    if wb:
        s += (f"; with a {wb[0]:.2f} m wheelbase ({wb[1]}) that is a steering ratio of about "
              f"{abs(G) / 57.2958 / wb[0]:.0f} to 1")
    else:
        s += "; the steering ratio and wheelbase are not in the telemetry, so their product was learned instead"
    s += "."
    v = sorted(x for x in (lags or {}).values() if x is not None)
    if v and v[len(v) // 2] >= 0.01:
        mid = v[len(v) // 2]
        s += (f" The car's rotation follows your steering about {mid:.2f} s later, so each moment's steering is "
              f"compared with the rotation it produced.")
    return s


def _cell_text(c: dict) -> str:
    if c["label"] in ("push", "rotate"):
        more = "more" if c["label"] == "push" else "less"
        deg = _plural(round(abs(c["median_deg"])), "degree")
        if c["phase"] == "entry":
            return (f"{c['corner']} {c['phase_words']}: the car {c['label_words']} more than on your other corner "
                    f"entries. You used about {deg} {more} steering than on your typical entry "
                    f"({c['laps']} laps, {c['confidence']}).")
        return (f"{c['corner']} {c['phase_words']}: the car {c['label_words']}. You used about "
                f"{deg} {more} steering than this car normally needs there "
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
        usual = " more than usual" if ph == "entry" else ""
        parts.append(f"{verbs[lab]}{usual} {PHASE_OF[ph]} {cls} corners ({_names(cs)})")
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
        s += (f" You had to catch the rear (countersteer) {_plural(v['events'], 'time')} at {k}"
              f" ({v['laps']} of {_plural(v['of_laps'], 'lap')}).")
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


TARGETS = {
    "front_arb": (_ARB, "front", "Front anti-roll bar", {-1: "one step softer", 1: "one step stiffer"}),
    "rear_arb": (_ARB, "rear", "Rear anti-roll bar", {-1: "one step softer", 1: "one step stiffer"}),
    "bias": (_BIAS, None, "Brake bias", {-1: "a little more to the rear", 1: "a little more to the front"}),
    "tc": (_TC, None, "Traction control", {1: "one step more", -1: "one step less"}),
    "wing": (_WING, None, "Rear wing", {-1: "one step less", 1: "one step more"}),
}
# (phase, label, speed class or None) -> options in order; the first one that can be made on this car is suggested.
# Each list ends with the other axle, so a bar already at its lowest falls through to a change that can be made.
IDEAS = {
    ("mid", "push", "slow"): [("front_arb", -1), ("rear_arb", 1)],
    ("mid", "push", "medium"): [("front_arb", -1), ("rear_arb", 1)],
    ("mid", "push", "fast"): [("wing", -1), ("front_arb", -1), ("rear_arb", 1)],
    ("mid", "rotate", "slow"): [("rear_arb", -1), ("front_arb", 1)],
    ("mid", "rotate", "medium"): [("rear_arb", -1), ("front_arb", 1)],
    ("mid", "rotate", "fast"): [("wing", 1), ("rear_arb", -1), ("front_arb", 1)],
    ("entry", "push", None): [("bias", -1), ("front_arb", -1), ("rear_arb", 1)],
    ("entry", "rotate", None): [("bias", 1), ("rear_arb", -1), ("front_arb", 1)],
    ("exit", "push", None): [("front_arb", -1), ("rear_arb", 1)],
    ("exit", "rotate", None): [("rear_arb", -1), ("tc", 1), ("front_arb", 1)],
}
WHY = {
    "front_arb": "a softer front bar (or stiffer rear) lets the front tyres grip more in the corner",
    "rear_arb": "a softer rear bar lets the rear tyres grip more (a stiffer one frees the rear to turn)",
    "bias": "brake bias moves braking work between the axles, which changes how the car turns in",
    "tc": "more traction control holds the rear when you get on the throttle",
    "wing": "the rear wing sets how much the rear grips at speed compared with the front",
}
WHY_WAY = {
    ("front_arb", -1): "a softer front bar lets the front tyres grip more in the corner",
    ("front_arb", 1): "a stiffer front bar takes a little grip from the front, which calms a rear that slides first",
    ("rear_arb", -1): "a softer rear bar lets the rear tyres grip more",
    ("rear_arb", 1): "a stiffer rear bar frees the rear to turn, which helps a front that slides first",
    ("bias", 1): "more brake to the front keeps the rear steadier as you brake and turn in",
    ("bias", -1): "more brake to the rear takes braking work off the front tyres, so they have more left to turn",
    ("tc", 1): "more traction control holds the rear when you get on the throttle",
    ("wing", 1): "more rear wing gives the rear more grip at speed",
    ("wing", -1): "less rear wing lets the front do more of the turning at speed",
}
NUMBER_CAVEAT = ("On most cars in iRacing a higher number is stiffer (or more), but check the garage's own help "
                 "text for this car.")
TC_CAVEAT = ("On most cars in iRacing a higher traction control number means more help, but on some cars it is the "
             "other way round: check the garage's own help text for this car.")
THEN = "Change only this, then drive at least 6 clean laps and compare again in Setup Lab."


def _parts(text: str):
    """(prefix letters, number text) of a setup value: 'P2' -> ('P', '2'), '3 (TC1)' -> ('', '3')."""
    from . import setuplab
    s = setuplab._num_text(text)
    if s is None:
        return "", None
    k = str(text).find(s)
    pre = str(text)[:k].strip()
    return (pre if re.fullmatch(r"[A-Za-z]{1,3}", pre) else ""), s


def _step(key: str, text: str, way: int):
    """(new text value, step) one step `way` (+1 up, -1 down) from the current value; None if not a number."""
    from . import setuplab
    _pre, s = _parts(text)
    if s is None:
        return None
    dec = setuplab._decimals(s)
    leaf = key.rsplit(".", 1)[-1]
    step = 0.5 if (leaf in _BIAS or (leaf in _WING and dec)) else (1.0 if dec == 0 else 10 ** -dec)
    new = float(s) + way * step
    if leaf in _ARB + _TC and new < 1:
        return None                                  # already at the lowest number (the range isn't in the file)
    return (f"{new:.{max(dec, 1 if step == 0.5 else 0)}f}" if dec or step == 0.5 else f"{int(round(new))}"), s


def _option(flat, target, way):
    """One change on this car: {"target", "way", "key", "label", "from", "to", "unit", "text"}; "to" is None when it
    can't be made (already at its lowest, or not a number). None when the car has no such setting."""
    leaves, side, lab, words = TARGETS[target]
    key, val = _find_key(flat, leaves, side)
    if key is None:
        return None
    pre, _num_txt = _parts(str(val))
    st = _step(key, str(val), way)
    unit = ibt.unit(val)
    name = lab
    if unit.startswith("(") and unit.endswith(")") and len(unit) <= 12:
        name, unit = f"{lab} {unit[1:-1]}", ""      # '3 (TC1)': "Traction control TC1 3 → 4", no doubled brackets
    if st is None:
        why = ("already at its lowest" if ibt.number(val) is not None else "not a number Debrief can step")
        return {"target": target, "way": way, "key": key, "label": lab, "from": str(val), "to": None,
                "text": f"{name} {words[way]}", "blocked": f"it is set to {val}, {why}"}
    new, old = st
    old, new = pre + old, pre + new
    if unit == "%":
        shown = f"{old}% → {new}%"
    else:
        shown = f"{old} → {new}{(' ' + unit) if unit else ''}"
    return {"target": target, "way": way, "key": key, "label": lab, "from": old, "to": new, "unit": unit,
            "text": f"{name} {shown} ({words[way]})"}


def _lower_first(t: str) -> str:
    return t[0].lower() + t[1:] if t else t


def _cap(t: str) -> str:
    return t[:1].upper() + t[1:] if t else t


def _short(o: dict) -> str:
    """'Front anti-roll bar 2 → 3 (one step stiffer)' -> 'Front anti-roll bar 2 → 3' (for inside brackets)."""
    t = str(o.get("text") or "")
    return t.rsplit(" (", 1)[0] if t.endswith(")") and " → " in t else t


# ---- in-car dials and driving advice for a fixed-setup series (2.18)
# iRacing names the live in-car adjustments dc...; a car without the dial has no such channel.
DIAL_CHANNELS = {"bias": ("dcBrakeBias",), "tc": ("dcTractionControl", "dcTractionControl1"), "abs": ("dcABS",)}
# where it is sound to answer a balance finding with an in-car dial: brake bias for the way in, traction control for
# a rear that slides as the throttle goes on. Mid-corner balance and a push on the way out have no sound dial answer.
FIXED_DIAL = {("entry", "push"): ("bias", -1), ("entry", "rotate"): ("bias", 1), ("exit", "rotate"): ("tc", 1)}
DIAL_WORDS = {("bias", -1): "move brake bias a little to the rear on the in-car dial (one click, about half a percent)",
              ("bias", 1): "move brake bias a little to the front on the in-car dial (one click, about half a percent)",
              ("tc", 1): "turn traction control up one step on the in-car dial"}
DIAL_LABEL = {"bias": "Brake bias (in-car dial)", "tc": "Traction control (in-car dial)"}
BIAS_CAVEAT = ("On most cars the brake bias number is the share of braking on the front wheels, so a lower number is "
               "more to the rear: check the car's in-car display.")
DRIVING = {("entry", "push"): "brake a touch earlier and ease off the brake more gradually as you turn in, so the "
                              "front tyres keep their load and grip",
           ("entry", "rotate"): "come off the brake a little sooner or more gently as you turn in, so the rear stays "
                                "settled",
           ("mid", "push"): "carry a little less speed into the middle and wait for the front to bite before adding "
                            "throttle",
           ("mid", "rotate"): "keep the steering and throttle steady through the middle: a small, steady throttle "
                              "settles the rear",
           ("exit", "push"): "let the car point at the exit before full throttle, and open the steering as you add it",
           ("exit", "rotate"): "squeeze the throttle on more gently and straighten the steering before the big push"}
THEN_DIAL = "Change only this, then drive at least 6 clean laps and look at this map again."
THEN_DRIVE = "Try it for at least 6 clean laps, then look at this map again."


def _strength(cells) -> tuple:
    """How strong a group of findings is: clear before likely, then more places, then the smaller p."""
    clear = any(c.get("confidence") == "clear" for c in cells)
    p = min((c.get("p_family") if isinstance(c.get("p_family"), (int, float)) else 1.0) for c in cells) if cells else 1.0
    return (clear, len({c.get("corner") for c in cells}), -float(p))


def _groups(report: dict) -> list:
    """The findings grouped as ideas are: (phase, label, and the speed class for the middle of corners)."""
    out, seen = [], set()
    for f in (report or {}).get("findings") or []:
        key = (f["phase"], f["label"], f["class"] if f["phase"] == "mid" else None)
        if key in seen:
            continue
        seen.add(key)
        same = [c for c in report["findings"] if (c["phase"], c["label"]) == (f["phase"], f["label"])
                and (f["phase"] != "mid" or c["class"] == f["class"])]
        verb = ("pushes" if f["label"] == "push" else "rotates") + (" more than usual" if f["phase"] == "entry" else "")
        out.append({"f": f, "same": same, "where": f"{PHASE_OF[f['phase']]} {_names(same)}", "verb": verb,
                    "place": f"{_names(same)} ({PHASE_WORDS[f['phase']]})",
                    "conf": "worth a try" if f["confidence"] == "clear" else "a guess", "strength": _strength(same)})
    return out


def _fixed_ideas(report: dict, setup: dict | None, dials) -> list:
    """A fixed-setup series: the garage is locked, so each finding gets an in-car dial where the car has one and it
    is a sound answer, else plain driving advice."""
    flat = ibt.flatten(setup or {})
    have = set(dials or ())
    for target, leaves in (("bias", _BIAS), ("tc", _TC)):
        if _find_key(flat, leaves)[0] is not None:
            have.add(target)
    out = []
    taken = {}                       # dial target -> (way, place): the stronger finding keeps the dial (review 2.18)
    for g in sorted(_groups(report), key=lambda g: g["strength"], reverse=True):
        f = g["f"]
        dial = FIXED_DIAL.get((f["phase"], f["label"]))
        text = f"The car {g['verb']} {g['where']}."
        if dial and dial[0] in have and dial[0] in taken and taken[dial[0]][0] != dial[1]:
            text += (f" Moving the {DIAL_LABEL[dial[0]].split(' (')[0].lower()} for this would undo the idea for "
                     f"{taken[dial[0]][1]}, which has evidence at least as strong, so leave the dial there.")
            dial = None
        if dial and dial[0] in have:
            taken.setdefault(dial[0], (dial[1], g["place"]))
            target, way = dial
            text += (f" The setup is locked, but this car has a {DIAL_LABEL[target].split(' (')[0].lower()} dial: "
                     f"{DIAL_WORDS[dial]}. Why: {WHY_WAY[(target, way)]}. "
                     f"{BIAS_CAVEAT if target == 'bias' else TC_CAVEAT} {THEN_DIAL}")
            out.append({"finding": f["text"], "corner": _names(g["same"]), "phase": f["phase"], "label": f["label"],
                        "change": None, "alternative": None, "dial": {"target": target, "way": way,
                                                                       "label": DIAL_LABEL[target],
                                                                       "text": DIAL_WORDS[dial]},
                        "driving": None, "why": WHY_WAY[(target, way)], "confidence": g["conf"], "text": text,
                        "then": THEN_DIAL, "_s": g["strength"]})
        else:
            drive = DRIVING[(f["phase"], f["label"])]
            text += f" The setup is locked, so adapt the driving: {drive}. {THEN_DRIVE}"
            out.append({"finding": f["text"], "corner": _names(g["same"]), "phase": f["phase"], "label": f["label"],
                        "change": None, "alternative": None, "dial": None, "driving": drive, "why": None,
                        "confidence": g["conf"], "text": text, "then": THEN_DRIVE, "_s": g["strength"]})
    out.sort(key=lambda i: (i["dial"] is None, i["confidence"] != "worth a try", -i["_s"][1]))
    for k, i in enumerate(out):
        i.pop("_s")
        i["first"] = k == 0
    return out


def _undo_of(o, last) -> dict | None:
    """The change in `last` (his change into this setup) that option o would put back, else None."""
    if not o or not last or o.get("to") is None:
        return None
    from . import setuplab
    to = setuplab._num_text(str(o["to"]))
    for ch in last.get("changes") or []:
        keys = ch.get("keys") or [ch.get("key")]
        old = setuplab._num_text(str(ch.get("old"))) if ch.get("old") is not None else None
        if o["key"] in keys and to is not None and old is not None and float(to) == float(old):
            return ch
    return None


def setup_ideas(report: dict, setup: dict | None, series_setup=None, last_change: dict | None = None,
                dials=None) -> list:
    """Conventional, one-change-at-a-time setup ideas for the clearest balance findings. Ideas with a change that
    can actually be made come first, then the most certain, then the most places.

    idea: {"finding": cell text, "corner", "phase", "label", "change": {"key","label","from","to","text"} | None,
           "alternative": same | None, "why", "confidence" ("worth a try" | "a guess"), "text", "then",
           "conflict": {"corner", "change"} | None, "undo": the change of his it would put back | None}
    "Try one change" is only said for a change that can be made on this car; when the usual change can't be (a bar
    already at its lowest, or the setting isn't in this car's setup), the next one on the list is offered and the
    text says why, or it says plainly that there is nothing to change.

    Two ideas never move the same setting opposite ways: the one with the stronger evidence keeps it (clear before
    likely, then more corners, then the smaller p); the other takes its next option, or says the two pull against
    each other. last_change: {"from", "to" (setup labels), "changes": setuplab.diff() items, "slower": bool} - his
    change into the setup these laps are on; an idea that would simply put it back is left out unless the laps showed
    that change made the car slower.

    A fixed-setup series gets in-car dial ideas where the car has the dial (dials: "bias" / "tc" / "abs", from the
    telemetry's dc channels, or the setup's own keys) and it is a sound answer, else driving advice. Never raises."""
    try:
        if str(series_setup or "").lower() == "fixed":
            return _fixed_ideas(report, setup, dials)
        flat = ibt.flatten(setup or {})
        groups = _groups(report)
        for g in groups:
            f = g["f"]
            opts = IDEAS.get((f["phase"], f["label"], f["class"])) or IDEAS.get((f["phase"], f["label"], None)) or []
            g["opts"] = opts
            g["cand"] = [(t, w, _option(flat, t, w)) for t, w in opts]
            found = [o for _t, _w, o in g["cand"] if o is not None and o["to"] is not None]
            g["undone"] = []
            if last_change and not last_change.get("slower"):
                keep = []
                for o in found:
                    ch = _undo_of(o, last_change)
                    (g["undone"].append((o, ch)) if ch else keep.append(o))
                found = keep
            g["found"] = found
        # the stronger evidence picks first; a weaker idea never moves the same setting the other way
        taken = []                                      # (group, option)
        for g in sorted(groups, key=lambda g: g["strength"], reverse=True):
            g["first"], g["clash"], g["clash_with"] = None, None, None
            for o in g["found"]:
                hit = next(((og, oo) for og, oo in taken if oo["key"] == o["key"] and oo["way"] != o["way"]), None)
                if hit is None:
                    g["first"] = o
                    break
                if g["clash"] is None:
                    g["clash"], g["clash_with"] = o, hit
            if g["first"] is not None:
                taken.append((g, g["first"]))
        out = []
        for g in groups:
            f, first = g["f"], g["first"]
            rest = [o for o in g["found"] if o is not first and (g["clash"] is None or o is not g["clash"])]
            alt = None
            for o in rest:
                if not any(oo["key"] == o["key"] and oo["way"] != o["way"] for og, oo in taken if og is not g):
                    alt = o
                    break
            skipped = []
            for t, w, o in g["cand"]:
                if first is not None and o is first:
                    break
                if o is not None and o["to"] is None:
                    skipped.append(o)
            undo = next((ch for o, ch in g["undone"]), None)
            text = f"The car {g['verb']} {g['where']}."
            if skipped:
                s0 = skipped[0]
                text += (f" The usual change, {_lower_first(s0['text'])}, can't be made here ({s0['blocked']})."
                         if first else
                         f" The usual change is {_lower_first(s0['text'])}, but {s0['blocked']}.")
            if g["undone"]:
                o, ch = g["undone"][0]
                text += (f" {'The usual change' if not skipped else 'The next one'}, {_lower_first(o['text'])}, would "
                         f"undo the change you made from {last_change.get('from') or 'the last setup'} to "
                         f"{last_change.get('to') or 'this one'} ({_lower_first(ch.get('text') or '')}), and the laps "
                         f"don't show that change made the car slower, so it isn't suggested.")
            if g["clash"] is not None:
                og, oo = g["clash_with"]
                same = og["strength"] == g["strength"]
                text += (f" {_cap(_lower_first(_short(g['clash'])))} would pull against the idea for {og['place']} "
                         f"({_lower_first(_short(oo))}), which has "
                         f"{'evidence about as strong' if same else 'stronger evidence'}: one would undo the other, so "
                         + ("try that one first and look here again after." if first is None else "try this instead."))
            if first:
                text += (f" Try one change: {first['text']}. Why: "
                         f"{WHY_WAY.get((first['target'], first['way']), WHY[first['target']])}.")
                if alt:
                    text += f" If that doesn't help, put it back and try {_lower_first(alt['text'])}."
                if last_change and last_change.get("slower") and _undo_of(first, last_change):
                    text += (f" That goes back on your change from {last_change.get('from')} to "
                             f"{last_change.get('to')}, and the laps showed {last_change.get('to')} slower, so going "
                             f"back is reasonable.")
                if f["phase"] == "entry" and f["label"] == "rotate":
                    text += (" Rotation on the way in also comes from how much brake you carry into the turn: "
                             "if you like how it turns in, leave it.")
                text += " " + (TC_CAVEAT if first["target"] == "tc" else NUMBER_CAVEAT) + " " + THEN
            elif skipped and not g["undone"] and g["clash"] is None:
                text += " This car's setup shows nothing else Debrief knows how to change for this."
            elif not skipped and not g["undone"] and g["clash"] is None:
                lab = TARGETS[g["opts"][0][0]][2].lower() if g["opts"] else "balance"
                text += f" The usual fix is the {lab}, but this car's setup doesn't show one to change."
            elif g["undone"] and g["clash"] is None:
                text += " Nothing else Debrief knows how to change for this is left on this car."
            out.append({"finding": f["text"], "corner": _names(g["same"]), "phase": f["phase"], "label": f["label"],
                        "change": first, "alternative": alt,
                        "why": WHY_WAY.get((first["target"], first["way"])) if first else None,
                        "confidence": g["conf"], "text": text, "then": THEN, "_n": len(g["same"]),
                        "conflict": ({"corner": g["clash_with"][0]["place"], "change": g["clash_with"][1]["text"]}
                                     if g["clash"] is not None else None),
                        "undo": undo})
        out.sort(key=lambda i: (i["change"] is None, i["confidence"] != "worth a try", -i["_n"]))
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
