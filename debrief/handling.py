"""Automatic handling read (2.23): what the car does wrong, in plain words, and whether it is the setup or the
driving. Pure logic on telemetry laps already read (balance.load_laps); no network, never raises.

What it looks at, per corner and per phase (the corner coach's corners; phases as balance.py defines them):
  entry  braking, turned in, up to the apex      mid  around the slowest point      exit  throttle pickup to the straight
  - understeer / oversteer: the steering used compared with the steering this car's OWN gentle bends say the
    measured yaw rate needs (balance.fit_car, learned from these laps: no per-car numbers). More steering than that
    = the front gives up first = understeer; less = the rear = oversteer. Entry is compared with the lap's own
    typical entry (the reason is in balance.py's header).
  - instability: on the way in, the wheel reversing direction two or more times (REV_DEG or more each way) while the
    yaw rate wobbles (YAW_WOBBLE rad/s or more around its own smooth curve).
  - poor traction: on the way out with the throttle 40 % or more, any of: the car rotating more than the steering
    says (YAW_SPIKE rad/s), speed rising under half as fast as this car's own good exits at that speed (STALL_FRAC),
    or the rear wheels turning more than STALL_SPIN faster than the fronts (when the wheel-speed channels are in
    the file; the baseline ratio is this car's own on a straight, off the throttle).
Only clean laps count (the file reader already drops off-track laps, pit laps, in/out laps and the first lap of a
race; laps flagged "traffic", "pit" or "off_track" are dropped here too, and laps over OFFPACE_FRAC of the median).

Setup or driving: a problem is a SETUP issue only when it shows on SETUP_FRAC of the laps in several corners of the
same kind and phase (at least 3 corners, or every corner of that kind when it has only 2) and on most laps overall.
Inconsistent lap to lap, or in one or a few corners only, it is called driving, and the report points to the corner
coach. Sureness uses the app's own scale (Sure, Likely, A hint, A guess; today.SURE_WORDS):
  Sure    80 % of laps or more, 12 or more laps, 3 or more corners
  Likely  70 % of laps or more, 8 or more laps
  A hint  every other setup issue, and every driving issue
This is a read of how the laps look, not a significance test; the Balance map above it (balance.py) does the
shuffle test. Both use the same fit.

analyze(laps, info=None, series_setup=None, prefix="T") -> report (JSON-friendly)
"""
from __future__ import annotations

import math

import numpy as np

from . import balance as bl

VERSION = 1
MIN_LAPS = 6                # clean laps needed before anything is said
MIN_CELL_LAPS = 5           # laps with a number in a corner and phase before that place counts
SETUP_FRAC = 0.6            # a place "shows" when this share of its laps do
WEAK_FRAC = 0.4             # below SETUP_FRAC but at least this: seen, but inconsistent
LAP_SHARE = 0.6             # and the group shows on this share of laps overall
SURE_FRAC, SURE_LAPS, SURE_CORNERS = 0.8, 12, 3
LIKELY_FRAC, LIKELY_LAPS = 0.7, 8
REV_DEG = 4.0               # instability: the wheel turning back at least this much (hysteresis)
REV_MIN = 2                 # ... at least this many times on the way in
YAW_WOBBLE = 0.08           # ... with the yaw rate wobbling this much (rad/s, half the peak-to-peak)
YAW_SPIKE = 0.10            # traction: yaw rate over what the steering says, rad/s
THR_TRACTION = 40.0         # traction is read with the throttle this far down (%)
STALL_THR = 70.0            # speed-rise check at this throttle or more
STALL_FRAC = 0.5            # ... under this share of the car's own good rise (its ACC_PCT-th percentile)
ACC_PCT = 90                # what the car manages when it hooks up, per speed bin
STALL_MIN_REF = 0.5         # m/s^2: slower typical rise than this is not read (nothing to compare with)
STALL_SPIN = 1.05           # rear wheels this much faster than the fronts, against the car's own baseline
SPEED_BIN = 5.0             # m/s
KINDS = ("understeer", "oversteer", "instability", "traction")
KIND_WORDS = {"understeer": "understeer", "oversteer": "oversteer", "instability": "instability",
              "traction": "poor traction"}
PHASE_TITLE = {"entry": "on the way in", "mid": "mid-corner", "exit": "on the way out"}
CLASS_WORDS = {"slow": "slow corners", "medium": "medium-speed corners", "fast": "fast corners"}
SURE_WORDS = {"sure": "Sure", "likely": "Likely", "hint": "A hint", "guess": "A guess"}
SURE_ORDER = ("guess", "hint", "likely", "sure")
CHANNEL_NOTES = {
    "yaw": ("YawRate", "how fast the car is turning"),
    "steer": ("SteeringWheelAngle", "the steering"),
    "speed": ("Speed", "speed"),
    "throttle": ("Throttle", "throttle"),
    "brake": ("Brake", "brake"),
    "latg": ("LatAccel", "sideways g (used to learn how the car normally steers)"),
}


def _plural(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")


def _names(cs, prefix="T"):
    return ", ".join(cs)


def _lap_ok(x: dict, race_only_first: bool = True) -> str | None:
    """Why a lap is left out, or None."""
    if x.get("off_track") or x.get("off"):
        return "off track"
    if x.get("pit"):
        return "pit"
    if x.get("traffic"):
        return "traffic"
    if str(x.get("session") or "").lower() == "race" and int(x.get("lap") or 0) <= 1:
        return "first lap of the race"
    return None


def _reversals2(s: np.ndarray, hyst: float) -> int:
    """How many times the signal turns back by at least `hyst` (the first move is not a reversal)."""
    if len(s) < 3:
        return 0
    hi = lo = float(s[0])
    mode = 0                                   # +1 rising, -1 falling, 0 not yet moved
    n = 0
    for v in s[1:]:
        v = float(v)
        if mode == 0:
            hi, lo = max(hi, v), min(lo, v)
            if hi - lo >= hyst:
                mode = 1 if v >= hi else -1
                hi = lo = v
        elif mode == 1:
            if v > hi:
                hi = v
            elif hi - v >= hyst:
                n, mode, lo = n + 1, -1, v
        else:
            if v < lo:
                lo = v
            elif v - lo >= hyst:
                n, mode, hi = n + 1, 1, v
    return n


def _wobble(r: np.ndarray) -> float:
    """Half the peak-to-peak of the yaw rate around its own smooth (quadratic) curve."""
    if len(r) < 8:
        return 0.0
    x = np.arange(len(r), dtype=float)
    res = r - np.polyval(np.polyfit(x, r, 2), x)
    return float((res.max() - res.min()) / 2.0)


def _judge(cells: dict, n_laps: int) -> dict | None:
    """cells: {corner index: (has bool[laps], hit bool[laps])} for one kind, phase and speed class.
    -> {"verdict": "setup" | "driving", "why", "strong": [ci], "weak": [ci], "laps_showing", "laps", "frac"} or None."""
    use = {ci: (h, k) for ci, (h, k) in cells.items() if int(h.sum()) >= MIN_CELL_LAPS}
    if not use:
        return None
    frac = {ci: float(k[h].sum()) / float(h.sum()) for ci, (h, k) in use.items()}
    strong = sorted(ci for ci, f in frac.items() if f >= SETUP_FRAC)
    weak = sorted(ci for ci, f in frac.items() if WEAK_FRAC <= f < SETUP_FRAC)
    if not strong and not weak:
        return None
    need = max(2, min(3, len(use)))
    out = {"strong": strong, "weak": weak, "frac": frac, "laps_showing": 0, "laps": 0}
    if len(use) >= 2 and len(strong) >= need:
        have = np.zeros(n_laps, int)
        hit = np.zeros(n_laps, int)
        for ci in strong:
            h, k = use[ci]
            have += h
            hit += (k & h)
        seen = have > 0
        show = seen & (hit >= np.ceil(have / 2.0))
        out["laps"], out["laps_showing"] = int(seen.sum()), int(show.sum())
        if out["laps"] and out["laps_showing"] / out["laps"] >= LAP_SHARE:
            out["verdict"], out["why"] = "setup", None
            return out
    # not a setup issue: driving. Say which kind.
    if not strong:
        out["why"] = "inconsistent"
    elif len(strong) == 1:
        out["why"] = "one_corner"
    else:
        out["why"] = "few_corners"
    pick = strong or weak
    h = np.zeros(n_laps, int)
    k = np.zeros(n_laps, int)
    for ci in pick:
        h += use[ci][0]
        k += use[ci][1] & use[ci][0]
    out["laps"] = int((h > 0).sum())
    out["laps_showing"] = int(((h > 0) & (k >= 1)).sum())
    out["verdict"] = "driving"
    return out


def _sureness(j: dict) -> str:
    if j["verdict"] != "setup":
        return "hint"
    fr = np.mean([j["frac"][ci] for ci in j["strong"]])
    if fr >= SURE_FRAC and j["laps"] >= SURE_LAPS and len(j["strong"]) >= SURE_CORNERS:
        return "sure"
    if fr >= LIKELY_FRAC and j["laps"] >= LIKELY_LAPS:
        return "likely"
    return "hint"


def _title(kind: str, phase: str, cls: str | None) -> str:
    base = {"mid": f"Mid-corner {KIND_WORDS[kind]}", "entry": f"{KIND_WORDS[kind].capitalize()} on the way in",
            "exit": f"{KIND_WORDS[kind].capitalize()} on the way out"}[phase]
    if kind == "traction":
        base = "Poor traction on the way out"
    if cls:
        base += f" in {CLASS_WORDS[cls]}"
    return base[:1].upper() + base[1:]


def _issue(kind, phase, cls, j, names, total_corners_in_group):
    sure = _sureness(j)
    where = _names([names[ci] for ci in (j["strong"] or j["weak"])])
    title = _title(kind, phase, cls)
    k, n = j["laps_showing"], j["laps"]
    if j["verdict"] == "setup":
        text = (f"{title} on {k} of {_plural(n, 'clean lap')}, in {_plural(len(j['strong']), 'corner')} "
                f"({where}). That is the same in several corners and on most laps, so it looks like the setup.")
    else:
        why = {"inconsistent": "it comes and goes from lap to lap",
               "one_corner": f"it shows in {where} only",
               "few_corners": f"it shows in only {len(j['strong'])} of {total_corners_in_group} corners"}[j["why"]]
        text = (f"{title} at {where}, on {k} of {_plural(n, 'clean lap')}: {why}, so it looks like driving, not "
                f"setup. The corner coach has the detail for {where}.")
    return {"id": f"{kind}:{phase}:{cls or 'any'}", "kind": kind, "phase": phase, "class": cls, "title": title,
            "verdict": j["verdict"], "why_driving": j.get("why"), "corners": [names[ci] for ci in (j["strong"] or j["weak"])],
            "laps_showing": k, "laps": n, "sureness": sure, "sure_words": SURE_WORDS[sure],
            "share": round(float(np.mean([j["frac"][ci] for ci in (j["strong"] or j["weak"])])), 2),
            "text": text}


def analyze(laps, info=None, series_setup=None, prefix: str = "T") -> dict:
    """The handling read for clean laps of one car and track (balance.load_laps' laps). Never raises.

    {"ok", "message", "version", "laps", "needed", "left_out": {reason: n}, "corners": [{"name","class"}],
     "issues": [issue], "setup_issues": [...], "driving_issues": [...], "channels": {...}, "summary",
     "series_setup"}
    issue: {"id", "kind", "phase", "class", "title", "verdict" ("setup" | "driving"), "why_driving", "corners",
            "laps_showing", "laps", "sureness", "sure_words", "share", "text"}"""
    info = dict(info or {})
    rep = {"ok": False, "version": VERSION, "laps": 0, "needed": MIN_LAPS, "left_out": {}, "corners": [],
           "issues": [], "setup_issues": [], "driving_issues": [], "channels": {}, "summary": "", "message": "",
           "series_setup": series_setup}
    try:
        given = [x for x in (laps or []) if isinstance(x, dict) and bl._num(x.get("time"))]
        left = {}
        keep = []
        for x in given:
            why = _lap_ok(x)
            if why:
                left[why] = left.get(why, 0) + 1
            else:
                keep.append(x)
        if keep:
            med = float(np.median([x["time"] for x in keep]))
            slow = [x for x in keep if x["time"] > med * bl.OFFPACE_FRAC]
            if slow:
                left["slow lap"] = len(slow)
            keep = [x for x in keep if x["time"] <= med * bl.OFFPACE_FRAC]
        rep["left_out"] = left
        rep["laps"] = len(keep)
        extra = ""
        if left:
            extra = " Left out: " + ", ".join(f"{n} {w}" for w, n in sorted(left.items())) + "."
        L = bl._num(info.get("length_m")) or bl._guess_length(keep)
        if len(keep) < MIN_LAPS or not L:
            rep["message"] = (f"Not enough clean laps yet: {len(keep)} of {MIN_LAPS}." + extra +
                              " Clean means no off-track, no pit and not the first lap of a race.")
            rep["summary"] = rep["message"]
            return rep
        P, laps2 = [], []
        for x in keep:
            p = bl._prep(x, L)
            if p is not None:
                P.append(p)
                laps2.append(x)
        keep = laps2
        if len(P) < MIN_LAPS:
            rep["laps"] = len(P)
            rep["message"] = f"Not enough clean laps yet: {len(P)} of {MIN_LAPS} could be read." + extra
            rep["summary"] = rep["message"]
            return rep
        P, _lags = bl._lag_compensate(P)
        nP = len(P)
        rep["laps"] = nP
        fit = bl.fit_car(P, "yaw", boot=False)
        if fit is None:
            rep["message"] = ("Not enough steady cornering to learn how this car normally steers (it needs gentle "
                              "bends at 0.1 to 0.4 g)." + extra)
            rep["summary"] = rep["message"]
            return rep
        G, K = fit["G"], fit["K"]
        fastest = min(range(nP), key=lambda i: keep[i]["time"])
        corners, mins = bl._corners_from(keep[fastest], L)
        if not corners:
            rep["message"] = "No corners found on the fastest lap." + extra
            rep["summary"] = rep["message"]
            return rep
        names = [f"{prefix}{k + 1}" for k in range(len(corners))]
        cls = [bl._speed_class(m) for m in mins]
        rep["corners"] = [{"name": names[k], "class": cls[k]} for k in range(len(corners))]
        wb = bl.WHEELBASE_M.get(str(info.get("car_path") or "").lower())
        floor = bl.PRACT_ROAD_DEG * abs(G) / 57.2958 / (wb[0] if wb else bl.WB_DEFAULT_M)
        pms = [[bl._phase_masks(p, c, L) for c in corners] for p in P]
        cidx = bl._cell_index(P, [{(ci, ph): pm[ph] for ci, pm in enumerate(row) if pm is not None
                                   for ph in bl.PHASES} for row in pms])
        vals, used, _typ = bl._cell_values(P, cidx, [(G, K)] * nP)

        has_ws = all(x.get("wsr") is not None and x.get("wsf") is not None for x in keep)
        used_ch = ["yaw", "steer", "speed", "throttle", "brake", "latg"]
        rep["channels"] = {"used": [CHANNEL_NOTES[c][0] for c in used_ch] + (["wheel speeds"] if has_ws else []),
                           "wheel_speeds": has_ws,
                           "note": ("" if has_ws else
                                    "The wheel-speed channels are not in these files, so wheelspin is read from "
                                    "how the car turns and speeds up instead.")}
        # --- understeer / oversteer: per lap, per place
        flags = {}                                   # (kind, phase, ci) -> (has, hit)

        def put(kind, phase, ci, has, hit):
            flags[(kind, phase, ci)] = (np.asarray(has, bool), np.asarray(hit, bool))
        for (ci, ph), arr in vals.items():
            has = np.isfinite(arr)
            if has.sum() < MIN_CELL_LAPS:
                continue
            prac = max(floor, bl.PRACT_FRAC * (used.get((ci, ph)) or 0.0))
            a = np.where(has, arr, 0.0)
            put("understeer", ph, ci, has, has & (a >= prac))
            put("oversteer", ph, ci, has, has & (a <= -prac))
        # --- instability (entry) and traction (exit)
        # the car's own typical speed rise at full throttle, per speed bin, and the rear/front wheel baseline
        acc_bins = {}
        base_ratio = None
        ratios, ratios_any = [], []
        for p, x, row in zip(P, keep, pms):
            a_lon = np.gradient(p["v"]) * p["rate"]
            for pm in row:
                if pm is None:
                    continue
                m = pm["exit"] & (p["th"] >= STALL_THR)
                for b, a in zip(np.floor(p["v"][m] / SPEED_BIN).astype(int), a_lon[m]):
                    acc_bins.setdefault(int(b), []).append(float(a))
            if has_ws:
                n = len(p["v"])
                if len(x["wsr"]) == n:
                    st = (np.abs(p["sw"]) < 3.0) & (p["v"] > 20.0) & (p["b"] < 5)      # straight, not braking
                    for q, store in ((st & (p["th"] < 5), ratios), (st, ratios_any)):
                        if q.any():
                            store.append(np.asarray(x["wsr"], float)[q] / np.maximum(np.asarray(x["wsf"], float)[q], 1.0))
        acc_ref = {b: float(np.percentile(v, ACC_PCT)) for b, v in acc_bins.items() if len(v) >= 10}
        if ratios or ratios_any:           # coasting on a straight first (no wheelspin there), else any straight
            base_ratio = float(np.median(np.concatenate(ratios or ratios_any)))
        for ci in range(len(corners)):
            has_i = np.zeros(nP, bool)
            hit_i = np.zeros(nP, bool)
            has_t = np.zeros(nP, bool)
            hit_t = np.zeros(nP, bool)
            for i, (p, x) in enumerate(zip(P, keep)):
                pm = pms[i][ci]
                if pm is None:
                    continue
                rel = pm["rel"]
                # instability: the way in (braking, before the apex)
                e = np.nonzero(pm["zone"] & (rel < 0) & (p["b"] > bl.BRAKE_ON))[0]
                if len(e) >= bl.MIN_SAMPLES * 2:
                    has_i[i] = True
                    sg = np.sign(np.median(p["sw"][e])) or 1.0
                    rev = _reversals2(sg * p["sw"][e], REV_DEG)
                    hit_i[i] = rev >= REV_MIN and _wobble(sg * p["r"][e]) >= YAW_WOBBLE
                # traction: the way out
                xi = np.nonzero(pm["exit"] & (p["th"] >= THR_TRACTION))[0]
                if len(xi) >= bl.MIN_SAMPLES:
                    has_t[i] = True
                    pred = ((p["sw"][xi] - K * p["ay"][xi] / bl.G0) / G) * p["v"][xi]
                    over = (p["r"][xi] - pred) * np.sign(pred)
                    spike = float(np.max(over)) >= YAW_SPIKE
                    a_lon = np.gradient(p["v"]) * p["rate"]
                    hot = np.nonzero(pm["exit"] & (p["th"] >= STALL_THR))[0]
                    stall = False
                    if len(hot) >= bl.MIN_SAMPLES:
                        r_ = []
                        for k_, a_ in zip(np.floor(p["v"][hot] / SPEED_BIN).astype(int), a_lon[hot]):
                            ref = acc_ref.get(int(k_))
                            if ref is not None and ref >= STALL_MIN_REF:
                                r_.append(a_ / ref)
                        stall = len(r_) >= bl.MIN_SAMPLES and float(np.median(r_)) < STALL_FRAC
                    spin = False
                    if has_ws and base_ratio and len(x["wsr"]) == len(p["v"]):
                        rr = np.asarray(x["wsr"], float)[xi] / np.maximum(np.asarray(x["wsf"], float)[xi], 1.0)
                        spin = float(np.median(rr)) > base_ratio * STALL_SPIN
                    hit_t[i] = bool(spike or stall or spin)
            if has_i.sum() >= MIN_CELL_LAPS:
                put("instability", "entry", ci, has_i, hit_i)
            if has_t.sum() >= MIN_CELL_LAPS:
                put("traction", "exit", ci, has_t, hit_t)
        # --- group by kind, phase and (for mid-corner and traction) speed class; judge each group
        groups = {}
        for (kind, ph, ci), fh in flags.items():
            c = cls[ci] if (ph == "mid" or kind == "traction") else None
            groups.setdefault((kind, ph, c), {})[ci] = fh
        issues = []
        for (kind, ph, c), cells in groups.items():
            j = _judge(cells, nP)
            if j is not None:
                issues.append(_issue(kind, ph, c, j, names, len(cells)))
        order = {"setup": 0, "driving": 1}
        issues.sort(key=lambda i: (order[i["verdict"]], -SURE_ORDER.index(i["sureness"]), -i["share"],
                                   -len(i["corners"])))
        # a corner that is both understeer and oversteer swings: it is driving, not a car that does both
        rep["issues"] = issues
        rep["setup_issues"] = [i for i in issues if i["verdict"] == "setup"]
        rep["driving_issues"] = [i for i in issues if i["verdict"] == "driving"][:4]
        rep["issues"] = rep["setup_issues"] + rep["driving_issues"]
        rep["ok"] = True
        if rep["setup_issues"]:
            top = rep["setup_issues"][0]
            rep["message"] = (f"{top['title']} ({top['sure_words']}), from {_plural(nP, 'clean lap')}."
                              + (f" {_plural(len(rep['setup_issues']) - 1, 'more setup issue')}."
                                 if len(rep["setup_issues"]) > 1 else ""))
        elif rep["driving_issues"]:
            rep["message"] = (f"Nothing that looks like the setup. {_plural(len(rep['driving_issues']), 'thing')} "
                              f"look like driving: see the corner coach.")
        else:
            rep["message"] = f"The car looks balanced and stable on these {_plural(nP, 'clean lap')}."
        rep["summary"] = rep["message"]
        return rep
    except Exception as e:  # noqa: BLE001
        rep["ok"] = False
        rep["message"] = f"The handling read could not be worked out ({str(e)[:140]})."
        rep["summary"] = rep["message"]
        return rep
