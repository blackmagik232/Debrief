"""Incident map (2.16 "Racing"): every incident of yours, where on the track it happened and what kind it probably was.

Where the incidents come from
  - Telemetry (.ibt, read only): PlayerCarMyIncidentCount goes up in steps. iRacing's usual values are +1 off track,
    +2 loss of control and +4 contact with another car (light contact is 0 and can't be seen). The owner's files
    (Nov 2026, Zolder races, practice and qualifying) showed steps of 1, 2 and 4 only; a bigger step (two at once,
    say 4 + 1) is split into iRacing's usual values and marked as "at once". Each step is placed by LapDistPct on
    the lap, with the lap number and the session type (race, qualifying, practice). Steps before the green flag of
    a race (the pace lap) are kept and marked. Points added while no telemetry was recording (between two files of
    one session) are counted, never placed.
  - Replay scans (debrief/replay_scan.py, read only): for a race with a scan, every car's lap position at the moment
    shows whether another car was close (within NEAR_M metres along the track), and the scan's own incident groups
    show whether another car was in trouble first in the same spot. A race with a scan but no telemetry uses the
    scan's off-track and stopped-on-track moments of your car instead (no points known).

What kind it probably was (classify): "contact" when iRacing counted contact (4x) or another car was close,
"caught_up" when another car's trouble started first in the same spot, "own" when nothing shows another car near.
Each comes with how sure it is ("sure", "probably", "possibly") and why, in plain words.

Patterns ("Turn 1 is where your incidents keep happening") are only said with at least MIN_RACES sessions showing it
and an exact test (Poisson-binomial tail, Bonferroni over every corner tested) below ALPHA; with fewer sessions the
map only describes what happened. The same module holds the statistics the start report (startreport.py) uses.

Nothing here raises on bad or missing data, writes anything, or makes a request. Outputs are JSON-ready dicts.
"""
from __future__ import annotations

import bisect
import gzip
import json
import math
import re
from pathlib import Path

import numpy as np

VERSION = 1

# ---------------------------------------------------------------- iRacing's incident points
POINTS_OFF, POINTS_LOSS, POINTS_CONTACT = 1, 2, 4
POINT_WORDS = {1: "off track", 2: "loss of control", 4: "contact"}
OFF_LOOKBACK_S = 5.0        # an off track step is placed where the wheels left the track, looked for this far back
SPIN_YAW = 2.0              # rad/s: turning faster than this (above SPIN_MIN_SPEED) is a spin, not a corner
SPIN_MIN_SPEED = 5.0        # m/s
SPIN_WINDOW_S = 3.0
SLOW_FRAC = 0.35            # "slowed suddenly": speed fell below this share of what it was in the 3 s before
SLOW_WINDOW_S = 3.0
ALONGSIDE_WINDOW_S = (2.0, 0.5)   # spotter (CarLeftRight) looked at from 2 s before to 0.5 s after the points

# ---------------------------------------------------------------- other cars, from a replay scan
NEAR_M = 10.0               # another car this close along the track is "close" (side by side or nose to tail)
NEAR_WINDOW_S = (2.0, 0.5)  # looked for from 2 s before the points were added to 0.5 s after
CAUGHT_LEAD_S = 1.0         # another car's trouble began at least this much earlier in the same scan incident
CAUGHT_CARS = 2             # or this many other cars close while you slowed suddenly
GROUP_PAD_S = 3.0           # a step this close to a scan incident's time span belongs to it
T, STATE, PCT, SURF, PIT, POS, LAPC = range(7)     # replay_scan's sample columns
RACING = 4                  # iRacing's session state while racing (green flag)

# ---------------------------------------------------------------- statistics (shared with startreport.py)
MIN_RACES = 3               # a pattern needs at least this many races (or sessions) showing it
ALPHA = 0.05                # family-wise chance that noise alone makes a pattern claim
EFFECT = 2.0                # ...and the hot spot must hold at least twice its fair share
PERM_N = 2000               # shuffles when there are too many races to try every sign flip
PERM_SEED = 216
EXACT_MAX = 12              # up to this many races every sign flip is tried (2^12 = 4096)
TREND_MIN = 5               # a trend over time needs at least this many races
MAX_FILE_BYTES = 2 * 1024 ** 3
MAX_SCAN_BYTES = 80 * 1024 * 1024

HOW_SURE = ("sure", "probably", "possibly")


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def _r(v, d=1):
    v = _num(v)
    return None if v is None else round(v, d)


def _plural(n, one, many=None):
    return f"{n} {one if n == 1 else (many or one + 's')}"


def session_kind(label) -> str:
    """race / qualifying / practice for an iRacing session type. Offline testing and warm-up count as practice."""
    t = str(label or "").lower()
    if "race" in t:
        return "race"
    if "qualif" in t:
        return "qualifying"
    return "practice"


# ---------------------------------------------------------------- corners and where a point on the lap is
def zones(corners=None, L=None) -> list:
    """The lap cut into one zone per corner: from the previous corner's exit to this corner's exit, so the
    braking zone belongs to the corner it leads into. [{"key": "C1", "label": "Turn 1", "lo": pct, "hi": pct,
    "share": share of the lap}] in lap order. Without corners, four quarters of the lap. Never raises."""
    L = _num(L)
    ends = []
    for c in corners or []:
        if not isinstance(c, dict):
            continue
        e = _num(c.get("end_m"))
        cid = str(c.get("id") or "")
        if e is None or not cid or not L or L <= 0:
            continue
        ends.append(((e % L) / L, cid))
    ends.sort()
    if not ends:
        q = [(0.25, "Q1", "the first quarter of the lap"), (0.5, "Q2", "the second quarter of the lap"),
             (0.75, "Q3", "the third quarter of the lap"), (1.0, "Q4", "the last quarter of the lap")]
        out, lo = [], 0.0
        for hi, k, label in q:
            out.append({"key": k, "label": label, "lo": lo, "hi": hi, "share": hi - lo, "corner": False})
            lo = hi
        return out
    out = []
    for i, (hi, cid) in enumerate(ends):
        lo = ends[i - 1][0]
        share = (hi - lo) % 1.0 or 1.0
        out.append({"key": cid, "label": corner_label(cid), "lo": lo, "hi": hi, "share": share, "corner": True})
    return out


def corner_label(cid) -> str:
    m = re.match(r"^C(\d+)$", str(cid or ""))
    return f"Turn {m.group(1)}" if m else str(cid or "somewhere on the lap")


def at_zone(label) -> str:
    """"at Turn 3", "in the first quarter of the lap"."""
    return ("in " if str(label).startswith("the ") else "at ") + str(label)


def locate(pct, zs) -> dict | None:
    """The zone a lap position (0..1) falls in."""
    p = _num(pct)
    if p is None or not zs:
        return None
    p %= 1.0
    for z in zs:
        lo, hi = z["lo"], z["hi"]
        if (lo < hi and lo <= p < hi) or (lo >= hi and (p >= lo or p < hi)):
            return z
    return zs[-1]


# ---------------------------------------------------------------- statistics
def sign_flip(columns, seed=PERM_SEED) -> list:
    """Family-wise sign-flip test across races. columns: one list per claim, one value per race (None = no data).
    Under "no real pattern" each race's value is as likely to be + as -. The statistic per claim is
    |sum| / sqrt(sum of squares); a claim's p is the share of sign flips (all 2^n of them up to EXACT_MAX races,
    else PERM_N with a fixed seed) where ANY claim looks at least as strong, so testing many claims doesn't add
    false ones. Returns one dict per column: {"p", "stat", "n", "sum", "plus", "minus"}."""
    cols = [list(c) for c in columns or []]
    n = max((len(c) for c in cols), default=0)
    out = []
    if not cols or n == 0:
        return [{"p": 1.0, "stat": 0.0, "n": 0, "sum": 0.0, "plus": 0, "minus": 0} for _ in cols]
    X = np.zeros((n, len(cols)))
    for j, c in enumerate(cols):
        for i, v in enumerate(c):
            x = _num(v)
            X[i, j] = x if x is not None else 0.0
    Q = np.sqrt((X ** 2).sum(axis=0))
    live = Q > 0
    obs = np.where(live, np.abs(X.sum(axis=0)) / np.where(live, Q, 1.0), 0.0)
    if n <= EXACT_MAX:
        grid = ((np.arange(2 ** n)[:, None] >> np.arange(n)[None, :]) & 1) * 2 - 1
        signs, smooth = grid.astype(float), 0
    else:
        rng = np.random.default_rng(seed)
        signs, smooth = rng.choice([-1.0, 1.0], size=(PERM_N, n)), 1
    S = np.abs(signs @ X) / np.where(live, Q, 1.0)
    S[:, ~live] = 0.0
    null_max = S.max(axis=1)
    for j, c in enumerate(cols):
        vals = [x for x in (_num(v) for v in c) if x is not None]
        if not live[j]:
            p = 1.0
        else:
            p = (smooth + int((null_max >= obs[j] - 1e-12).sum())) / (smooth + len(null_max))
        out.append({"p": round(float(p), 4), "stat": round(float(obs[j]), 3), "n": len(vals), "sum": round(float(sum(vals)), 3),
                    "plus": sum(1 for x in vals if x > 0), "minus": sum(1 for x in vals if x < 0)})
    return out


def poisson_binomial_tail(probs, k) -> float:
    """P(at least k successes) when trial i succeeds with probability probs[i] (exact)."""
    dist = np.zeros(len(probs) + 1)
    dist[0] = 1.0
    for q in probs:
        q = min(1.0, max(0.0, float(q)))
        dist[1:] = dist[1:] * (1 - q) + dist[:-1] * q
        dist[0] *= (1 - q)
    return float(min(1.0, dist[int(max(0, k)):].sum()))


def kendall_trend(values, seed=PERM_SEED) -> dict:
    """Kendall's tau between race order (oldest first) and the values, with a shuffle p (PERM_N shuffles, fixed seed,
    two-sided). {"n", "tau", "p"}; p is None under TREND_MIN races."""
    y = np.array([v for v in (_num(x) for x in values or []) if v is not None], dtype=float)
    n = len(y)
    if n < 2:
        return {"n": n, "tau": None, "p": None}
    iu = np.triu_indices(n, 1)

    def tau(v):
        s = np.sign(v[None, :] - v[:, None])[iu]       # j later than i: +1 when the value went up
        return float(s.sum() / len(s))
    obs = tau(y)
    if n < TREND_MIN:
        return {"n": n, "tau": round(obs, 3), "p": None}
    rng = np.random.default_rng(seed)
    P = np.argsort(rng.random((PERM_N, n)), axis=1)          # PERM_N shuffles of the race order
    Y = y[P]
    taus = np.sign(Y[:, None, :] - Y[:, :, None])[:, iu[0], iu[1]].mean(axis=1)
    count = int((np.abs(taus) >= abs(obs) - 1e-12).sum())
    return {"n": n, "tau": round(obs, 3), "p": round((1 + count) / (1 + PERM_N), 4)}


# ---------------------------------------------------------------- telemetry
_MEMO: dict = {}
MEMO_MAX = 400
CHANNELS = ("SessionNum", "SessionState", "SessionTime", "Lap", "LapDistPct", "LapDist", "PlayerCarMyIncidentCount",
            "PlayerTrackSurface", "OnPitRoad", "Speed", "YawRate", "CarLeftRight", "PlayerCarPosition",
            "PlayerCarClassPosition", "IsOnTrack")
SETTLE_S = 8.0              # iRacing's position reading settles this long after the line (race_analysis.SETTLE_S)


def split_points(d) -> list:
    """A step in the incident count as iRacing's usual values: 4 -> [4]; 5 -> [4, 1]; 3 -> [2, 1]."""
    d, out = int(d), []
    for v in (4, 2, 1):
        while d >= v:
            out.append(v)
            d -= v
    return out


def _classes(ib) -> tuple:
    """({car_idx: class name}, the driver's car_idx, driver's name, number of cars per class) from the session text."""
    di = ib.session.get("DriverInfo") or {}
    me = di.get("DriverCarIdx")
    cls, size = {}, {}
    name = ""
    for d in di.get("Drivers") or []:
        if not isinstance(d, dict):
            continue
        idx = d.get("CarIdx")
        if d.get("CarIsPaceCar") in (1, "1") or d.get("IsSpectator") in (1, "1") or str(d.get("UserName") or "").lower() == "pace car":
            continue
        c = str(d.get("CarClassShortName") or d.get("CarClassID") or "")
        if isinstance(idx, int):
            cls[idx] = c
            size[c] = size.get(c, 0) + 1
        if idx == me:
            name = str(d.get("UserName") or "")
    return cls, me, name, size


def read_ibt(path) -> dict:
    """One telemetry file: what it is, and per session the green flag, start and lap-1 positions and every incident
    step. Never raises ({"ok": False, "why"} instead). Memoised on (path, size, modified time)."""
    p = Path(str(path))
    try:
        st = p.stat()
        key = (str(p), st.st_size, st.st_mtime_ns)
    except OSError:
        return {"ok": False, "file": p.name, "why": "The telemetry file couldn't be opened."}
    if key in _MEMO:
        return _MEMO[key]
    try:
        if st.st_size > MAX_FILE_BYTES:
            raise ValueError("too big")
        from . import ibt
        with ibt.open_ibt(p) as ib:
            out = _read_open(ib, p)
    except Exception as e:  # noqa: BLE001  (one odd file never stops the map)
        out = {"ok": False, "file": p.name, "why": f"The telemetry file couldn't be read ({type(e).__name__})."}
    _MEMO[key] = out
    while len(_MEMO) > MEMO_MAX:
        _MEMO.pop(next(iter(_MEMO)))
    return out


def _read_open(ib, p: Path) -> dict:
    n = ib.record_count
    rate = ib.tick_rate or 60
    col = {nm: ib.col(nm) for nm in CHANNELS if ib.has(nm)}
    tr = ib.track()
    wi = ib.session.get("WeekendInfo") or {}
    try:
        sub = int(wi.get("SubSessionID") or 0) or None
    except (TypeError, ValueError):
        sub = None
    classes, me_idx, name, size = _classes(ib)
    L = (tr.get("length_km") or 0) * 1000.0 or None
    if not L and "LapDist" in col and n:
        L = float(np.nanmax(col["LapDist"])) or None
    base = {"ok": True, "file": p.name, "path": str(p), "when": int(ib.start_date or 0), "subsession": sub,
            "track": " ".join(x for x in (tr.get("name"), tr.get("config")) if x).strip() or str(tr.get("code") or ""),
            "track_code": str(tr.get("code") or ""), "car": ib.car().get("name") or "", "driver": name, "me_idx": me_idx,
            "L": L, "classes": {str(k): v for k, v in classes.items()},
            "multiclass": len({v for v in classes.values() if v}) > 1, "class_size": size.get(classes.get(me_idx), None) if me_idx is not None else None,
            "has_spotter": "CarLeftRight" in col, "sessions": []}
    if n < 2 or "PlayerCarMyIncidentCount" not in col or "SessionNum" not in col:
        base["why"] = "This file has no incident count."
        return base
    types = ib.session_types()
    sess = col["SessionNum"].astype(int)
    time = col["SessionTime"].astype(float) if "SessionTime" in col else np.arange(n) / rate
    inc = col["PlayerCarMyIncidentCount"].astype(int)
    lap = col["Lap"].astype(int) if "Lap" in col else np.zeros(n, int)
    pct = col["LapDistPct"].astype(float) if "LapDistPct" in col else np.full(n, np.nan)
    state = col["SessionState"].astype(int) if "SessionState" in col else np.zeros(n, int)
    surf = col["PlayerTrackSurface"].astype(int) if "PlayerTrackSurface" in col else None
    speed = col["Speed"].astype(float) if "Speed" in col else None
    yaw = col["YawRate"].astype(float) if "YawRate" in col else None
    side = col["CarLeftRight"].astype(int) if "CarLeftRight" in col else None
    pos = col["PlayerCarPosition"].astype(int) if "PlayerCarPosition" in col else None
    cpos = col["PlayerCarClassPosition"].astype(int) if "PlayerCarClassPosition" in col else None
    edges = np.nonzero(np.diff(sess) != 0)[0] + 1
    for a, b in zip(np.concatenate(([0], edges)), np.concatenate((edges, [n]))):
        a, b = int(a), int(b)
        if b - a < 2:
            continue
        num = int(sess[a])
        stype = str(types.get(num, "") or "")
        kind = session_kind(stype)
        S = {"num": num, "type": stype, "kind": kind, "t0": _r(time[a], 2), "t1": _r(time[b - 1], 2),
             "inc_first": int(inc[a]), "inc_last": int(inc[b - 1]), "laps": int(lap[a:b].max()), "events": []}
        g = None
        if kind == "race":
            on = np.nonzero(state[a:b] >= RACING)[0]
            if len(on) and on[0] > 0:
                g = a + int(on[0])
                S["green"] = _r(time[g], 2)
                S["green_lap"] = int(lap[g])
            # iRacing's position at the green (taken from the first reading after it) and after lap 1
            if g is not None and pos is not None:
                k = next((j for j in range(g, min(b, g + 5 * rate)) if pos[j] > 0), None)
                if k is not None:
                    S["start_pos"] = int(pos[k])
                    S["start_class_pos"] = int(cpos[k]) if cpos is not None and cpos[k] > 0 else None
                x1 = next((j for j in range(max(g, a + 1), b) if lap[j] >= 2 and lap[j - 1] < 2), None)
                if x1 is not None:
                    S["lap1_end"] = _r(time[x1], 2)
                    k = min(b - 1, x1 + int(SETTLE_S * rate))
                    if k > x1 + rate and pos[k] > 0:
                        S["lap1_pos"] = int(pos[k])
                        S["lap1_class_pos"] = int(cpos[k]) if cpos is not None and cpos[k] > 0 else None
        steps = np.nonzero(np.diff(inc[a:b]) > 0)[0] + a + 1
        for i in steps:
            i = int(i)
            d = int(inc[i] - inc[i - 1])
            parts = split_points(d)
            at = i
            if 1 in parts and surf is not None:          # where the wheels left the track
                j = i
                lo = max(a, i - int(OFF_LOOKBACK_S * rate))
                while j > lo and surf[j] != 0:
                    j -= 1
                if surf[j] == 0:
                    while j > lo and surf[j - 1] == 0:
                        j -= 1
                    at = j
            w0, w1 = max(a, i - int(SPIN_WINDOW_S * rate)), min(b, i + int(SPIN_WINDOW_S * rate))
            spun = bool(yaw is not None and speed is not None and
                        (np.abs(yaw[w0:w1]) * (speed[w0:w1] >= SPIN_MIN_SPEED)).max(initial=0.0) >= SPIN_YAW)
            slowed = None
            if speed is not None:
                before = speed[max(a, i - int(SLOW_WINDOW_S * rate)):i + 1]
                after = speed[i:min(b, i + int(SLOW_WINDOW_S * rate))]
                if len(before) and len(after) and before.max() > 5.0:
                    slowed = bool(after.min() < SLOW_FRAC * before.max())
            alongside = None
            if side is not None:
                s0, s1 = max(a, i - int(ALONGSIDE_WINDOW_S[0] * rate)), min(b, i + int(ALONGSIDE_WINDOW_S[1] * rate))
                alongside = bool((side[s0:s1] >= 2).any())
            race_lap = None
            before_green = False
            if kind == "race":
                if g is None and state[i] < RACING:
                    before_green = True
                elif g is not None and i < g:
                    before_green = True
                race_lap = 0 if before_green else max(1, int(lap[at]))
            p = _num(pct[at])
            S["events"].append({"t": _r(time[at], 2), "t_points": _r(time[i], 2), "session_num": num, "session": stype,
                                "kind_of_session": kind, "lap": race_lap if kind == "race" else int(lap[at]),
                                "before_green": before_green, "pct": round(p % 1.0, 4) if p is not None else None,
                                "points": d, "parts": parts, "at_once": len(parts) > 1,
                                "off": bool(1 in parts or (surf is not None and (surf[max(a, i - rate):i + 1] == 0).any())),
                                "spun": spun, "slowed": slowed, "alongside": alongside,
                                "speed": _r(speed[at], 1) if speed is not None else None})
        base["sessions"].append(S)
    return base


def telemetry_steps_seen(files) -> list:
    """Every step size seen in these files (for saying which iRacing values really appear)."""
    seen = set()
    for f in files or []:
        for s in (f or {}).get("sessions") or []:
            for e in s.get("events") or []:
                seen.add(int(e.get("points") or 0))
    return sorted(x for x in seen if x > 0)


# ---------------------------------------------------------------- replay scans
def sample_rows(samples):
    rows = (samples or {}).get("samples") if isinstance(samples, dict) else None
    return rows if isinstance(rows, list) else []


def _valid(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 1


def _wrapped(a, b):
    d = (a - b) % 1.0
    return d - 1.0 if d > 0.5 else d


def near_cars(samples, me_idx, t, L, names=None, window=NEAR_WINDOW_S, within_m=NEAR_M) -> dict:
    """Other cars within within_m metres of yours along the track around time t, from the scan's readings.
    {"readable": bool (your car had readings then), "cars": [{"idx", "name", "metres"}] closest first}."""
    rows = sample_rows(samples)
    idxs = (samples or {}).get("idxs") or []
    L = _num(L)
    t = _num(t)
    if me_idx not in idxs or not rows or not L or t is None:
        return {"readable": False, "cars": []}
    me = idxs.index(me_idx)
    times = [r[0] for r in rows]
    lo, hi = bisect.bisect_left(times, t - window[0]), bisect.bisect_right(times, t + window[1])
    best, readable = {}, False
    for k in range(lo, min(hi, len(rows))):
        p = rows[k][PCT] if len(rows[k]) > PCT else None
        pit = rows[k][PIT] if len(rows[k]) > PIT else None
        if not p or me >= len(p) or not _valid(p[me]):
            continue
        readable = True
        for j, v in enumerate(p):
            if j == me or not _valid(v) or (pit and j < len(pit) and pit[j]):
                continue
            m = abs(_wrapped(v, p[me])) * L
            if m <= within_m and m < best.get(j, 1e9):
                best[j] = m
    names = names or {}
    cars = [{"idx": idxs[j], "name": names.get(idxs[j]) or names.get(str(idxs[j])) or f"car {idxs[j]}", "metres": round(m, 1)}
            for j, m in sorted(best.items(), key=lambda kv: kv[1])]
    return {"readable": readable, "cars": cars}


def scan_group(scan, me_number, t, pad=GROUP_PAD_S) -> dict | None:
    """The scan's incident (a group of cars in trouble in one spot) that includes your car around time t."""
    t = _num(t)
    if t is None:
        return None
    for i in (scan or {}).get("incidents") or []:
        if not isinstance(i, dict):
            continue
        a, b = _num(i.get("from")), _num(i.get("to"))
        if a is None or b is None or not (a - pad <= t <= b + pad):
            continue
        cars = [c for c in i.get("cars") or [] if isinstance(c, dict)]
        mine = next((c for c in cars if str(c.get("number")) == str(me_number)), None)
        if mine is None:
            continue
        others = [c for c in cars if c is not mine]
        first = min(others, key=lambda c: _num(c.get("from")) or 1e18) if others else None
        lead = (_num(mine.get("from")) or t) - (_num(first.get("from")) or t) if first else None
        return {"from": a, "to": b, "level": i.get("level"), "lap": i.get("lap"), "others": [_name(c) for c in others],
                "first": _name(first) if first else None, "first_what": sorted((first or {}).get("what") or {}),
                "lead_s": round(lead, 1) if lead is not None else None, "mine": sorted((mine.get("what") or {}).keys())}
    return None


def _name(c) -> str:
    from .results_csv import clean_name
    c = c if isinstance(c, dict) else {}
    return clean_name(c.get("name")) or ("#" + str(c.get("number") or "?"))


# ---------------------------------------------------------------- what kind of incident
def classify(ev: dict) -> dict:
    """{"type": own / contact / caught_up, "sure": sure / probably / possibly, "why": plain words, "with": [names]}.
    ev: an event (points/parts/off/spun/slowed/alongside) with "near" (near_cars) and "group" (scan_group) when a scan
    covers it."""
    parts = ev.get("parts") or []
    near = (ev.get("near") or {}).get("cars") or []
    readable = bool((ev.get("near") or {}).get("readable"))
    grp = ev.get("group") or None
    names = [c["name"] for c in near]
    lead = (grp or {}).get("lead_s")
    contact = POINTS_CONTACT in parts
    if grp and grp.get("first") and lead is not None and lead >= CAUGHT_LEAD_S and (contact or near or ev.get("slowed") or ev.get("off") or ev.get("source") == "scan"):
        what = " and ".join(grp.get("first_what") or []) or "in trouble"
        return {"type": "caught_up", "sure": "probably", "with": grp.get("others") or names,
                "why": f"{grp['first']} was {what} about {lead:.0f} s before you in the same spot, so you were probably caught up in it."}
    if contact:
        if len(near) >= CAUGHT_CARS and ev.get("slowed"):
            return {"type": "caught_up", "sure": "possibly", "with": names,
                    "why": f"iRacing counted contact, {_plural(len(near), 'other car')} were within {NEAR_M:.0f} m and you slowed suddenly, so it may have been a pile up."}
        if near:
            return {"type": "contact", "sure": "sure", "with": names[:1],
                    "why": f"iRacing counted contact (4x). The closest car was {near[0]['name']}, {near[0]['metres']:.0f} m away, so probably with them."}
        return {"type": "contact", "sure": "sure", "with": [],
                "why": "iRacing counted contact (4x). " + ("The replay showed no car within "
                       f"{NEAR_M:.0f} m, so it may have been light contact a moment earlier." if readable else "Who with isn't known without a replay scan.")}
    if near or ev.get("alongside"):
        who = f"{near[0]['name']} was {near[0]['metres']:.0f} m away" if near else "the spotter showed a car alongside"
        if ev.get("source") == "scan" and grp and grp.get("others"):
            return {"type": "contact", "sure": "possibly", "with": grp["others"],
                    "why": f"The replay shows {', '.join(grp['others'][:3])} in trouble in the same spot at the same time."}
        return {"type": "contact", "sure": "possibly", "with": names[:1],
                "why": f"{who.capitalize() if not near else who}, so it may have been a tap or a squeeze rather than your own mistake."}
    what = "spin" if ev.get("spun") else (POINT_WORDS.get(max(parts)) if parts else ("off track moment" if ev.get("off") else "incident"))
    what = "off track moment" if what == "off track" else what
    if readable or ev.get("alongside") is False:
        return {"type": "own", "sure": "probably", "with": [],
                "why": f"No other car was close, so this {what} was probably your own mistake."}
    return {"type": "own", "sure": "possibly", "with": [],
            "why": f"Nothing shows another car near this {what}, but there's no replay scan or spotter data to be sure."}


# ---------------------------------------------------------------- finding everything on this PC
_SCAN_MEMO: dict = {}


def _read_scan_dir(d: Path) -> dict | None:
    """{"dir", "scan", "samples", "event_id"} for one race folder (own scan or RaceCap's), read only. Memoised."""
    from . import racecap_link
    try:
        files = sorted((d / "scan").glob("*.scan.json"))
        stamp = tuple((f.name, f.stat().st_mtime_ns, f.stat().st_size) for f in files) + tuple(
            (f.name, f.stat().st_mtime_ns) for f in (d / "scan").glob("*.samples.json.gz"))
    except OSError:
        return None
    key = (str(d), stamp)
    if key in _SCAN_MEMO:
        return _SCAN_MEMO[key]
    scan = racecap_link.read_scan(d)
    if scan is None:
        return None
    samples = racecap_link.read_samples(d)
    ev = None
    try:
        ev = json.loads((d / "link.json").read_text(encoding="utf-8")).get("event_id")
    except (OSError, ValueError, AttributeError):
        pass
    out = {"dir": str(d), "scan": scan, "samples": samples if isinstance(samples, dict) else None, "event_id": str(ev) if ev else None,
           "official": racecap_link.read_official(d, scan.get("subsession"))}
    _SCAN_MEMO[key] = out
    while len(_SCAN_MEMO) > 60:
        _SCAN_MEMO.pop(next(iter(_SCAN_MEMO)))
    return out


_REVIEW_MEMO: dict = {}


def _read_review(p: Path) -> dict | None:
    """The few things the racing pages need from one saved review: meta, the official result, corners, lap length."""
    try:
        st = p.stat()
        key = (str(p), st.st_mtime_ns, st.st_size)
        if key in _REVIEW_MEMO:
            return _REVIEW_MEMO[key]
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict):
        return None
    m = d.get("meta") if isinstance(d.get("meta"), dict) else {}
    res = d.get("result") if isinstance(d.get("result"), dict) else None
    if res is None:
        try:
            r2 = json.loads((p.parent / "result.json").read_text(encoding="utf-8"))
            res = r2 if isinstance(r2, dict) else None
        except (OSError, ValueError):
            res = None
    corners = [{"id": c.get("id"), "start_m": c.get("start_m"), "end_m": c.get("end_m"), "apex_m": c.get("apex_m")}
               for c in d.get("corners") or [] if isinstance(c, dict) and c.get("id")]
    out = {"dir": p.parent.name, "track": str(m.get("track") or ""), "driver": str(m.get("driver") or ""),
           "event_id": str(m.get("event_id") or ""), "started_at": str(m.get("started_at") or ""),
           "session": str(m.get("session") or ""), "result": res, "corners": corners, "L": _num(d.get("L")),
           "multiclass": bool(m.get("multiclass"))}
    _REVIEW_MEMO[key] = out
    while len(_REVIEW_MEMO) > 2000:
        _REVIEW_MEMO.pop(next(iter(_REVIEW_MEMO)))
    return out


def _when_iso(s) -> float | None:
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class _Tracks:
    """One name per track: names that are the same track (racecap_link.same_track) share the first one seen."""

    def __init__(self):
        self.names = []

    def key(self, name) -> str:
        from .racecap_link import same_track
        name = str(name or "").strip() or "Unknown track"
        for n in self.names:
            if same_track(n, name):
                return n
        self.names.append(name)
        return name


def collect(library_folder=None, telemetry_folder=None, racecap_folder=None, driver=None, telemetry_files=None,
            max_files=400) -> dict:
    """Everything both racing pages read, found once: replay scans (own and RaceCap's), telemetry files and saved
    reviews (for official results and each track's corners). Read only; never raises.
    {"scans": [...], "telemetry": [read_ibt...], "reviews": [...], "corners": {track: {"corners", "L", "from"}},
     "driver", "notes": [plain words]}."""
    notes = []
    out = {"scans": [], "telemetry": [], "reviews": [], "corners": {}, "driver": str(driver or ""), "notes": notes}
    try:
        from . import racecap_link, replay_scan
        roots = []
        if library_folder:
            roots.append(replay_scan.scans_root(library_folder))
        if racecap_folder:
            r = racecap_link.races_root(racecap_folder)
            if r is not None:
                roots.append(r)
        seen = set()
        for root in roots:
            for sub, _slug, d in racecap_link.race_dirs(root) if root and Path(root).is_dir() else []:
                if sub in seen:
                    continue
                got = _read_scan_dir(d)
                if got is not None:
                    seen.add(sub)
                    out["scans"].append(got)
    except Exception as e:  # noqa: BLE001
        notes.append(f"Replay scans couldn't all be read ({type(e).__name__}).")
    try:
        if library_folder:
            rd = Path(library_folder) / "reviews"
            for p in sorted(rd.glob("*/review.json")) if rd.is_dir() else []:
                r = _read_review(p)
                if r is not None:
                    out["reviews"].append(r)
    except Exception as e:  # noqa: BLE001
        notes.append(f"Saved reviews couldn't all be read ({type(e).__name__}).")
    try:
        files = [Path(f) for f in telemetry_files or []]
        if telemetry_folder and Path(telemetry_folder).is_dir():
            files += sorted(Path(telemetry_folder).glob("*.ibt"), key=lambda f: f.stat().st_mtime, reverse=True)[:max_files]
        done = set()
        for f in files:
            if str(f) in done:
                continue
            done.add(str(f))
            x = read_ibt(f)
            if x.get("ok"):
                out["telemetry"].append(x)
            else:
                notes.append(f"{f.name}: {x.get('why')}")
    except Exception as e:  # noqa: BLE001
        notes.append(f"The telemetry folder couldn't all be read ({type(e).__name__}).")
    if not out["driver"]:
        out["driver"] = next((r["driver"] for r in out["reviews"] if r.get("driver")), "")
    out["corners"] = _corner_maps(out)
    return out


def _corner_maps(src) -> dict:
    """{track: {"corners", "L", "from"}}: each track's corners from its newest race review (so every race at a track
    is placed on the same corners), else from the best clean lap in a telemetry file there (engine.detect_corners)."""
    tracks = _Tracks()
    maps = {}
    revs = sorted((r for r in src.get("reviews") or [] if r.get("corners") and r.get("L")),
                  key=lambda r: (_when_iso(r.get("started_at")) or 0), reverse=True)
    for r in revs:
        k = tracks.key(r.get("track"))
        if k not in maps:
            maps[k] = {"corners": r["corners"], "L": r["L"], "from": f"review {r['dir']}"}
    for f in src.get("telemetry") or []:
        k = tracks.key(f.get("track"))
        if k in maps:
            continue
        got = _corners_from_ibt(f.get("path"))
        if got:
            maps[k] = {"corners": got[0], "L": got[1], "from": f"telemetry {f.get('file')}"}
    src["_tracks"] = tracks
    return maps


def _corners_from_ibt(path):
    try:
        from . import engine, ibt, livecoach
        with ibt.open_ibt(path) as ib:
            lap = livecoach.best_lap_in(ib)
            L = livecoach._ibt_length_m(ib)
            if not lap or not L:
                return None
            raw = livecoach._ibt_lap_raw(ib, lap)
            R = engine.resample(raw, float(lap["time"]), float(L))
            cs = engine.detect_corners(R, L=float(L))
            return (cs, float(L)) if cs else None
    except Exception:  # noqa: BLE001
        return None


def track_key(src, name) -> str:
    t = src.get("_tracks") if isinstance(src, dict) else None
    if t is None:
        t = _Tracks()
        for k in (src or {}).get("corners") or {}:
            t.key(k)
        if isinstance(src, dict):
            src["_tracks"] = t
    return t.key(name)


def zones_for(src, track) -> list:
    m = ((src or {}).get("corners") or {}).get(track) or {}
    return zones(m.get("corners"), m.get("L"))


def find_me_in_scan(scan, me_idx=None, name=None):
    from . import racecap_link
    cars = [c for c in (scan or {}).get("cars") or [] if isinstance(c, dict)]
    if me_idx is not None:          # the telemetry's own car index is the same car in the replay
        hit = next((c for c in cars if c.get("car_idx") == me_idx), None)
        if hit is not None:
            return hit
    return racecap_link.find_me(cars, name) if name else None


# ---------------------------------------------------------------- sessions (one race or practice outing each)
def outings(src) -> list:
    """Telemetry sessions joined across files (one race split over two files is one outing), each with its scan when
    there is one, plus scanned races with no telemetry. Oldest first.
    [{"id", "kind", "type", "track", "subsession", "when", "files", "events", "not_recorded", "scan", "me_car", "L",
      "green", "start", "lap1", "classes", "multiclass", "class_size", "driver", "laps"}]."""
    scans = {str((s.get("scan") or {}).get("subsession")): s for s in (src or {}).get("scans") or []}
    groups = {}
    for f in sorted((src or {}).get("telemetry") or [], key=lambda x: (x.get("when") or 0, x.get("file") or "")):
        for S in f.get("sessions") or []:
            key = (f.get("subsession") or f"file:{f.get('file')}", S["num"])
            g = groups.get(key)
            if g is None:
                g = groups[key] = {"id": f"{key[0]}:{S['num']}", "kind": S["kind"], "type": S["type"],
                                   "track": track_key(src, f.get("track")), "subsession": f.get("subsession"),
                                   "when": f.get("when"), "files": [], "events": [], "not_recorded": 0, "L": f.get("L"),
                                   "classes": f.get("classes") or {}, "multiclass": f.get("multiclass"),
                                   "class_size": f.get("class_size"), "driver": f.get("driver"), "me_idx": f.get("me_idx"),
                                   "laps": 0, "_last_inc": 0, "has_spotter": f.get("has_spotter")}
            if S.get("inc_first", 0) > g["_last_inc"]:
                g["not_recorded"] += S["inc_first"] - g["_last_inc"]
            g["_last_inc"] = max(g["_last_inc"], S.get("inc_last", 0))
            g["files"].append(f.get("file"))
            g["laps"] = max(g["laps"], S.get("laps") or 0)
            g["events"] += [dict(e, file=f.get("file")) for e in S.get("events") or []]
            for k in ("green", "start_pos", "start_class_pos", "lap1_pos", "lap1_class_pos", "lap1_end"):
                if S.get(k) is not None and g.get(k) is None:
                    g[k] = S[k]
    out = []
    for g in groups.values():
        g.pop("_last_inc", None)
        if g["kind"] == "race" and g.get("subsession") is not None and str(g["subsession"]) in scans:
            g["scan"] = scans.pop(str(g["subsession"]))
        out.append(g)
    names = (src or {}).get("driver")
    for sub, s in scans.items():
        sc = s.get("scan") or {}
        out.append({"id": f"{sub}:scan", "kind": "race", "type": "Race", "track": track_key(src, sc.get("track")),
                    "subsession": int(sub) if str(sub).isdigit() else sub, "when": None, "files": [], "events": [],
                    "not_recorded": 0, "L": sc.get("track_length_m"), "classes": {}, "multiclass": None,
                    "class_size": None, "driver": names, "me_idx": None, "laps": sc.get("laps") or 0, "scan": s,
                    "has_spotter": False, "green": sc.get("green")})
    revs = (src or {}).get("reviews") or []
    for g in out:
        s = g.get("scan")
        if s:
            g["me_car"] = find_me_in_scan(s["scan"], g.get("me_idx"), g.get("driver") or names)
            if not g.get("classes"):
                g["classes"] = {str(c.get("car_idx")): c.get("car_class") for c in s["scan"].get("cars") or []
                                if isinstance(c, dict) and c.get("car_class") is not None}
            rev = next((r for r in revs if s.get("event_id") and r.get("event_id") == s.get("event_id")), None)
            if g.get("when") is None:
                g["when"] = _when_iso((rev or {}).get("started_at")) or _dir_time(s.get("dir"))
            g["review"] = rev
        if g["kind"] == "race" and g.get("review") is None and g.get("when"):
            g["review"] = next((r for r in revs if session_kind(r.get("session")) == "race" and r.get("started_at")
                                and abs((_when_iso(r["started_at"]) or 0) - g["when"]) <= 1800
                                and track_key(src, r.get("track")) == g["track"]), None)
    out.sort(key=lambda g: (g.get("when") or 0, str(g["id"])))
    return out


def _dir_time(d):
    try:
        return Path(d).stat().st_mtime if d else None
    except OSError:
        return None


def _names(scan) -> dict:
    from .results_csv import clean_name
    return {c.get("car_idx"): clean_name(c.get("name")) or f"#{c.get('number')}" for c in (scan or {}).get("cars") or []
            if isinstance(c, dict)}


def outing_events(o: dict) -> list:
    """Every incident of one outing, located and classified. Telemetry first; a scanned race with no telemetry uses
    the scan's moments of your car (no points known)."""
    zs = o.get("_zones") or zones(None, None)
    s = o.get("scan") or {}
    scan, samples, me = s.get("scan"), s.get("samples"), o.get("me_car")
    L = o.get("L") or (scan or {}).get("track_length_m")
    names = _names(scan)
    evs = []
    if o.get("files"):
        for e in o.get("events") or []:
            ev = dict(e, source="telemetry")
            if scan and me and o["kind"] == "race" and not e.get("before_green"):
                ev["near"] = near_cars(samples, me.get("car_idx"), e.get("t_points"), L, names)
                ev["group"] = scan_group(scan, me.get("number"), e.get("t_points"))
            evs.append(ev)
    elif scan and me:
        for i in scan.get("incidents") or []:
            if not isinstance(i, dict) or i.get("level") == "trivial":
                continue
            mine = next((c for c in i.get("cars") or [] if isinstance(c, dict) and str(c.get("number")) == str(me.get("number"))), None)
            if mine is None:
                continue
            t = _num(mine.get("from")) or _num(i.get("from"))
            what = mine.get("what") or {}
            ev = {"t": _r(t, 2), "t_points": _r(t, 2), "session": "Race", "kind_of_session": "race",
                  "lap": _my_lap(me, t) or i.get("lap"), "before_green": False, "pct": _pct_at(samples, me.get("car_idx"), t, i.get("track_pct")),
                  "points": None, "parts": [], "at_once": False, "off": "off track" in what,
                  "spun": None, "slowed": "stopped on track" in what, "alongside": None, "source": "scan",
                  "scan_what": sorted(what)}
            ev["near"] = near_cars(samples, me.get("car_idx"), t, L, names)
            ev["group"] = scan_group(scan, me.get("number"), t)
            evs.append(ev)
    for ev in evs:
        z = locate(ev.get("pct"), zs)
        ev["zone"] = z["key"] if z else None
        ev["where"] = z["label"] if z else "somewhere on the lap"
        ev["lap1"] = o["kind"] == "race" and ev.get("lap") == 1
        ev.update({"class": classify(ev)})
        ev["near"] = (ev.get("near") or {}).get("cars", []) if isinstance(ev.get("near"), dict) else []
        ev["group"] = ev.get("group") or None
    return evs


def _my_lap(me_car, t):
    """The lap your car was on at time t, from its own line crossings (the scan's incident lap is the first car's)."""
    from .racecap_link import _lap_at
    try:
        return _lap_at(me_car, t) if _num(t) is not None else None
    except Exception:  # noqa: BLE001
        return None


def _pct_at(samples, idx, t, fallback=None):
    idxs = (samples or {}).get("idxs") or []
    rows = sample_rows(samples)
    if idx in idxs and rows and _num(t) is not None:
        k = min(bisect.bisect_left([r[0] for r in rows], t), len(rows) - 1)
        p = rows[k][PCT]
        v = p[idxs.index(idx)] if p and idxs.index(idx) < len(p) else None
        if _valid(v):
            return round(v, 4)
    return _num(fallback)


# ---------------------------------------------------------------- the map
TYPE_WORDS = {"own": "your own mistake", "contact": "contact", "caught_up": "caught up in someone else's incident"}


def build(src=None, **find) -> dict:
    """The incident map across every session found. src: collect()'s result (or pass collect's arguments).
    {"version", "tracks": [{"track", "zones", "cells", "by_zone", "sessions", "events", "points", "lap1", "summary",
     "patterns", "sentences"}], "steps_seen", "sessions", "events": n, "notes", "thresholds", "how"}. Never raises."""
    try:
        return _build(src if src is not None else collect(**find))
    except Exception as e:  # noqa: BLE001  (the window shows the reason instead of breaking)
        return {"version": VERSION, "tracks": [], "events": 0, "sessions": 0, "steps_seen": [],
                "notes": [f"The incident map couldn't be worked out ({type(e).__name__})."], "thresholds": thresholds(), "how": HOW}


HOW = (f"Incidents come from your telemetry's incident count (iRacing's usual points: 1 off track, 2 loss of control, "
       f"4 contact) and, for scanned races, the replay shows which cars were within {NEAR_M:.0f} m. A corner is only called a "
       f"pattern when at least {MIN_RACES} sessions had an incident there, it holds at least {EFFECT:.0f} times its fair "
       f"share of the lap, and an exact test says noise alone would do that less than {ALPHA:.0%} of the time over every "
       f"corner tested.")


def thresholds() -> dict:
    return {"min_races": MIN_RACES, "alpha": ALPHA, "effect": EFFECT, "near_m": NEAR_M, "spin_yaw_rad_s": SPIN_YAW,
            "caught_lead_s": CAUGHT_LEAD_S, "perm_n": PERM_N, "perm_seed": PERM_SEED, "exact_max": EXACT_MAX,
            "trend_min": TREND_MIN}


def _build(src) -> dict:
    outs = outings(src)
    by_track = {}
    for o in outs:
        o["_zones"] = zones_for(src, o["track"])
        o["_events"] = outing_events(o)
        by_track.setdefault(o["track"], []).append(o)
    tracks = [track_map(t, os_) for t, os_ in by_track.items()]
    tracks.sort(key=lambda x: -x["events"])
    steps = telemetry_steps_seen(src.get("telemetry"))
    notes = list(src.get("notes") or [])
    odd = [s for s in steps if s not in (1, 2, 4)]
    steps_note = ("Your telemetry shows incident steps of " + ", ".join(f"{s}x" for s in steps) + "."
                  + (f" Steps of {', '.join(str(s) for s in odd)} are two incidents at once and are split into iRacing's usual values." if odd else "")
                  ) if steps else "No incident steps were found in your telemetry."
    return {"version": VERSION, "tracks": tracks, "sessions": len(outs), "events": sum(t["events"] for t in tracks),
            "steps_seen": steps, "steps_note": steps_note, "notes": notes, "thresholds": thresholds(), "how": HOW}


def _event_row(o, e) -> dict:
    c = e.get("class") or {}
    return {"session": o["id"], "kind_of_session": o["kind"], "session_type": o.get("type"), "subsession": o.get("subsession"), "when": o.get("when"),
            "review": str((o.get("review") or {}).get("dir") or "") or None,
            "t": e.get("t"), "lap": e.get("lap"), "lap1": bool(e.get("lap1")), "before_green": bool(e.get("before_green")),
            "pct": e.get("pct"), "zone": e.get("zone"), "where": e.get("where"), "points": e.get("points"),
            "parts": e.get("parts") or [], "at_once": bool(e.get("at_once")), "what": _what(e), "source": e.get("source"),
            "type": c.get("type"), "sure": c.get("sure"), "why": c.get("why"), "with": c.get("with") or [],
            "near": [dict(x) for x in e.get("near") or []][:4], "spun": e.get("spun"), "off": e.get("off"),
            "slowed": e.get("slowed")}


def _what(e) -> str:
    if e.get("parts"):
        return " + ".join(f"{POINT_WORDS.get(p, str(p))} ({p}x)" for p in e["parts"])
    return ", ".join(e.get("scan_what") or []) or "incident"


def track_map(track, outs) -> dict:
    zs = outs[0].get("_zones") if outs else zones(None, None)
    rows = [_event_row(o, e) for o in outs for e in o.get("_events") or []]
    cells = {}
    by_zone = {z["key"]: {"zone": z["key"], "label": z["label"], "share": round(z["share"], 3), "events": 0, "points": 0,
                          "lap1": 0, "race": 0, "qualifying": 0, "practice": 0, "own": 0, "contact": 0, "caught_up": 0,
                          "sessions": 0} for z in zs}
    sess_in_zone = {z["key"]: set() for z in zs}
    for r in rows:
        z = r["zone"]
        if z not in by_zone:
            continue
        b = by_zone[z]
        b["events"] += 1
        b["points"] += r["points"] or 0
        b["lap1"] += 1 if r["lap1"] else 0
        b[r["kind_of_session"]] = b.get(r["kind_of_session"], 0) + 1
        if r["type"] in ("own", "contact", "caught_up"):
            b[r["type"]] += 1
        cell = cells.setdefault(f"{z}|{r['type']}", {"zone": z, "type": r["type"], "events": 0, "points": 0})
        cell["events"] += 1
        cell["points"] += r["points"] or 0
        sess_in_zone[z].add(r["session"])
    for k, s in sess_in_zone.items():
        by_zone[k]["sessions"] = len(s)
    races = [o for o in outs if o["kind"] == "race"]
    out = {"track": track, "zones": [{"key": z["key"], "label": z["label"], "lo": round(z["lo"], 4), "hi": round(z["hi"], 4),
                                      "share": round(z["share"], 4)} for z in zs],
           "corner_names": "corners" if zs and zs[0].get("corner") else "quarters",
           "sessions": len(outs), "races": len(races), "events": len(rows), "points": sum(r["points"] or 0 for r in rows),
           "not_recorded": sum(o.get("not_recorded") or 0 for o in outs),
           "points_known": all(r["points"] is not None for r in rows),
           "cells": sorted(cells.values(), key=lambda c: (-c["events"], c["zone"], str(c["type"]))),
           "by_zone": [by_zone[z["key"]] for z in zs], "list": sorted(rows, key=lambda r: ((r["when"] or 0), r["t"] or 0)),
           "by_session": {k: sum(1 for r in rows if r["kind_of_session"] == k) for k in ("race", "qualifying", "practice")},
           "lap1": {"events": sum(1 for r in rows if r["lap1"]), "points": sum(r["points"] or 0 for r in rows if r["lap1"]),
                    "later": sum(1 for r in rows if r["kind_of_session"] == "race" and not r["lap1"] and not r["before_green"]),
                    "before_green": sum(1 for r in rows if r["before_green"])},
           "types": {k: sum(1 for r in rows if r["type"] == k) for k in ("own", "contact", "caught_up")}}
    out["patterns"] = patterns(outs, zs, rows)
    out["sentences"] = sentences(out)
    return out


def patterns(outs, zs, rows) -> list:
    """Corners where incidents keep happening, and lap 1, each only with the gates in the module docstring."""
    found = []
    if not zs:
        return found
    sess = {}
    for r in rows:
        if r["zone"] is not None and not r["before_green"]:
            sess.setdefault(r["session"], []).append(r)
    per = [(sid, evs) for sid, evs in sess.items()]
    n_sess = len([o for o in outs])
    tests = len(zs) + 1
    for z in zs:
        hit = sum(1 for _sid, evs in per if any(e["zone"] == z["key"] for e in evs))
        probs = [1 - (1 - z["share"]) ** len(evs) for _sid, evs in per]
        expected = sum(probs)
        if hit < MIN_RACES or hit < EFFECT * expected:
            continue
        p = min(1.0, poisson_binomial_tail(probs, hit) * tests)
        if p < ALPHA:
            found.append({"kind": "corner", "zone": z["key"], "label": z["label"], "sessions": hit, "of": n_sess,
                          "expected": round(expected, 2), "p": round(p, 4),
                          "text": f"{z['label']} is a pattern: you had an incident there in {hit} of {_plural(n_sess, 'session')}, "
                                  f"about {hit / max(expected, 1e-9):.1f} times what an even spread round the lap would give."})
    race = [(o, [r for r in rows if r["session"] == o["id"] and not r["before_green"]]) for o in outs if o["kind"] == "race"]
    race = [(o, evs) for o, evs in race if evs and (o.get("laps") or 0) >= 2]
    hit = sum(1 for _o, evs in race if any(e["lap1"] for e in evs))
    probs = [1 - (1 - 1.0 / o["laps"]) ** len(evs) for o, evs in race]
    expected = sum(probs)
    if hit >= MIN_RACES and hit >= EFFECT * expected:
        p = min(1.0, poisson_binomial_tail(probs, hit) * tests)
        if p < ALPHA:
            found.append({"kind": "lap1", "sessions": hit, "of": len(race), "expected": round(expected, 2), "p": round(p, 4),
                          "text": f"Lap 1 is a pattern: {hit} of your {len(race)} races with incidents had one on lap 1, "
                                  f"about {hit / max(expected, 1e-9):.1f} times what an even spread over the laps would give."})
    return found


def sentences(m) -> list:
    """Plain words for one track. Facts first; a pattern only when patterns() found one."""
    out = []
    n, ev = m["sessions"], m["events"]
    track = m["track"]
    if not ev:
        out.append(f"No incidents found at {track} in {_plural(n, 'session')}." if n else f"No sessions found at {track}.")
        return out
    pts = m["points"]
    if m["points_known"]:
        out.append(f"{_plural(ev, 'incident')} ({pts} {'point' if pts == 1 else 'points'}) at {track} over {_plural(n, 'session')}.")
    else:
        out.append(f"{_plural(ev, 'incident')} at {track} over {_plural(n, 'session')}" + (f", {pts} points where telemetry shows them." if pts else "."))
    top = sorted(m["by_zone"], key=lambda b: (-(b["points"] if m["points_known"] else b["events"]), -b["events"]))[0]
    if top["events"]:
        kinds = sorted(((top[k], k) for k in ("contact", "own", "caught_up")), reverse=True)
        main = kinds[0][1] if kinds[0][0] else None
        lap1 = top["lap1"]
        tail = ""
        if main and kinds[0][0] * 2 > top["events"]:
            tail = ", mostly " + TYPE_WORDS[main]
            if lap1 * 2 > top["events"]:
                tail += " on lap 1"
        elif lap1 * 2 > top["events"]:
            tail = ", mostly on lap 1"
        if m["points_known"] and pts:
            out.append(f"{top['points']} of your {pts} incident points at {track} came {at_zone(top['label'])}{tail}.")
        else:
            out.append(f"{top['events']} of your {ev} incidents at {track} came {at_zone(top['label'])}{tail}.")
    t = m["types"]
    bits = [f"{t[k]} {TYPE_WORDS[k]}" for k in ("own", "contact", "caught_up") if t[k]]
    if bits:
        out.append("Most likely kinds: " + ", ".join(bits) + ".")
    if m["lap1"]["events"]:
        out.append(f"{_plural(m['lap1']['events'], 'incident')} on lap 1 and {m['lap1']['later']} later in races.")
    if m["lap1"]["before_green"]:
        out.append(f"{_plural(m['lap1']['before_green'], 'incident')} before the green flag (the pace lap).")
    if m["not_recorded"]:
        out.append(f"{m['not_recorded']} more points were added while no telemetry was recording, so they can't be placed.")
    if m["patterns"]:
        out += [p["text"] for p in m["patterns"]]
    elif n < MIN_RACES:
        out.append(f"With {_plural(n, 'session')} this only says what happened, not a pattern.")
    else:
        out.append("No corner stands out more than chance would explain yet.")
    return out
