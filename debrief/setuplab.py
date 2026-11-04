"""Setup Lab: what changed between his setups, which one was faster, and what the telemetry suggests trying next.

Everything here is read only and works on saved telemetry (.ibt) files:
  index()            reads each file once (ibt.summary plus a few channels) and caches the result by name, size, time
  group()            sorts files into setups ("Setup A, B, C" per car + track) and runs (files in a row on one setup)
  diff()             the settings that changed between two setups, in plain words
  compare()          clean-lap pace per setup with an evidence gate, like the rest of Debrief
  suggestions()      rule-based ideas, each saying which readings it used and how sure it is
  check_setup_file() a safety check for setup files from outside (.sto / .zip): never extracts or runs anything
  report()           all of the above for one car + track, as one JSON-ready dict for the window

What real files showed (owner's files, Nov 2026):
  - iRacing writes the setup into the telemetry (CarSetup). Setup .sto files are encrypted, so this is the only source.
  - Tyre carcass temps (LFtempCL/CM/CR) only change at a pit-stall stop; the value at a file's first sample is the
    reading from the last stop (often in an earlier file), so readings are de-duplicated by value across files.
    CL is the car's left side: outer for left tyres, inner for right tyres. Surface temps (LFtempL/M/R) are live.
  - A never-pitted car shows the default cold temps (44C in GT4 cars, 42C in the MX-5) - these are ignored.
  - CarSetup's LastTemps / LastHotPressure / TreadRemaining are pit readings, not settings; the garage's corner
    weights, ride heights and nose weight are results that move with fuel. None of these make a "new setup".
  - DriverSetupName lives at DriverInfo level and may be missing ("fixed.sto" when present).
  - The fixed-setup MX-5 keeps its whole setup (tyres too) under 'Suspension'.

Debrief never writes into the iRacing setups folder and never changes a telemetry file.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import zipfile
from pathlib import Path

import numpy as np

from debrief import ibt
from . import names as _names   # 2.18: one display name per track and car
from .units import lap as _names_lap   # 2.18: one lap-time format (m:ss.000)

CACHE_VERSION = 3

# Leaf keys that are readings (they change without the driver touching the setup) - never part of a setup's identity.
READING_KEYS = {"LastHotPressure", "LastTempsOMI", "LastTempsIMO", "TreadRemaining", "UpdateCount"}
FUEL_KEYS = {"FuelLevel"}
# Garage results that follow from the settings and the fuel load: shown in a diff, never part of the identity.
RESULT_KEYS = {"CornerWeight", "RideHeight", "NoseWeight"}

CORNER_KEYS = {"LeftFront": "LF", "RightFront": "RF", "LeftRear": "LR", "RightRear": "RR"}
CORNER_WORDS = {"LF": "left front", "RF": "right front", "LR": "left rear", "RR": "right rear"}
PAIR = {"LeftFront": ("RightFront", "Front"), "RightFront": ("LeftFront", "Front"),
        "LeftRear": ("RightRear", "Rear"), "RightRear": ("LeftRear", "Rear")}

# Evidence gate (same spirit as the rest of Debrief: say "not enough to tell" rather than guess).
# 5 clean laps per setup is the minimum Coach Dave's Setup IQ also uses.
MIN_LAPS = 5
MAD_K = 3.0                    # a lap more than 3 x MAD slower than its stint's median is not pace (traffic, mistake)
TRAFFIC = 1.07                 # fallback when the MAD is 0: laps slower than 107% of the median
TRAFFIC_SIDE = 0.10            # a lap with a car alongside (CarLeftRight not clear) for >10% of it is a traffic lap
MAX_TEMP_GAP = 6.0             # deg C of track surface temperature: more and the comparison is confounded
MAX_FUEL_GAP = 15.0            # litres of average fuel load: more and the comparison is confounded
MIN_DIFF = 0.10                # s a lap: a smaller gap is never called a difference
BOOT_N = 2000                  # bootstrap resamples for the interval of the median difference
BOOT_SEED = 212
BLOCK_STINTS = 3               # with this many stints on each setup, whole stints are resampled (block bootstrap)
SMALL_N = 10                   # fewer counted laps than this on a setup: a 95% interval instead of 90%
MAX_MORE_LAPS = 60             # beyond this "more laps would tell" is not a useful answer

# Tyre temperature targets for GT cars (carcass temps at a pit stop, stint of >= 5 laps): the inner edge hotter than
# the outer by 6-10 deg C at the front and a little less at the rear. Low-medium confidence: lap time beats them.
CAMBER_TARGET = {"front": (6.0, 10.0), "rear": (4.0, 8.0)}
CAMBER_MARGIN = 2.0            # outside the target by more than this before an idea is given
CROWN = 4.0                    # middle vs average of the edges, deg C: pressure high / low
TEMP_STINT_LAPS = 5            # a pit reading only counts after a stint of at least 5 laps

# Hot pressures: live, sampled at the fastest point of each lap, laps 3+ of a stint.
HOT_POS = 3
AXLE_KPA = 7.0                 # one tyre more than this off its axle partner -> change that tyre's cold pressure
IMBALANCE_KPA = 5.0            # front/rear difference worth reporting
# No target window is shipped for any car: there is no credible current source for GT4 hot pressures. A target is
# learned from his own fastest stints instead, or he can pass his own (`targets`).
HOT_TARGETS: dict = {}
ATM_KPA = 101.3                # gauge + this = absolute pressure: hot / cold scale as absolute pressures (gas law)

BOTTOM_MM = 1.0                # within 1 mm of the learned floor counts as touching it
BOTTOM_FRAC = 0.02             # more than 2% of clean-lap samples piled at the floor: a flat bottom on the trace
BOTTOM_MOVE_MM = 5.0           # the channel must move at least this much, or it is not a real ride-height trace
BOTTOM_LOSS = 0.05             # s: laps that touch must lose this much near that spot before a change is suggested
HIST_BINS, HIST_MM = 80, 0.5   # stored low end of each ride-height trace (40 mm from 10 mm under the 0.5th pct)

_NAME_TIME = re.compile(r"(\d{4})-(\d{2})-(\d{2}) (\d{2})-(\d{2})-(\d{2})\.ibt$", re.I)
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
HOT_RUN_LAPS = 3               # a stint's hot pressures count only when it has this many counted laps (a "run", as in
#                                the tyre pressure table), so "Now" never shows a reading the table calls "not read"

# Setups only fixed series race in iRacing's official series: Setup Lab doesn't ask "open or fixed?" for these (he can
# still change it). Matched on the car's iRacing path (start) or screen name (anywhere), lower case.
KNOWN_FIXED = (("mx5 mx52016", "global mazda mx-5 cup", "the Global Mazda MX-5 Cup"),
               ("mx5 cup", "mazda mx-5 cup", "the Mazda MX-5 Cup"),
               ("toyotagr86", "toyota gr86", "the Toyota GR86"))


def lap_text(t) -> str:
    """A lap time the way every Setup Lab table shows it: m:ss.sss ('1:00.343', '0:59.748')."""
    try:
        t = float(t)
    except (TypeError, ValueError):
        return "-"
    if not np.isfinite(t):
        return "-"
    m = int(t // 60)
    return f"{m}:{t - 60 * m:06.3f}"


def day_text(when: str) -> str:
    """'2026-10-20 18:00' -> 'Oct 20' (no locale: the window shows English month names)."""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(when or ""))
    if not m or not 1 <= int(m.group(2)) <= 12:
        return str(when or "")
    return f"{_MONTHS[int(m.group(2)) - 1]} {int(m.group(3))}"


# ================================================================ reading files

def _when(name: str, start: int | None) -> tuple[str, float]:
    """Local time from the file name (what his PC wrote), else from the header. Returns (text, sort key)."""
    m = _NAME_TIME.search(name)
    if m:
        y, mo, d, h, mi, s = (int(x) for x in m.groups())
        try:
            key = time.mktime((y, mo, d, h, mi, s, 0, 0, -1))
        except (OverflowError, ValueError):
            key = float(start or 0)
        return f"{y:04d}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}", key
    if start:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(start)), float(start)
    return "", 0.0


def _cold(t3) -> bool:
    """A reading from a car that never got hot: the defaults (44C / 42C), or all three within 3 deg C under 50 C."""
    if not t3 or len(t3) != 3 or any(v is None for v in t3):
        return True
    return (max(t3) - min(t3) <= 3.0 and max(t3) < 50.0) or all(round(v) in (42, 44) for v in t3)


def _reading_from_channels(vals: dict, i: int) -> dict:
    """{corner: (outer, middle, inner)} from carcass channels at sample i."""
    out = {}
    for c in ("LF", "RF", "LR", "RR"):
        cl, cm, cr = (float(vals[c][z][i]) for z in range(3))
        out[c] = (round(cl, 1), round(cm, 1), round(cr, 1)) if c in ("LF", "LR") else (round(cr, 1), round(cm, 1), round(cl, 1))
    return out


def _reading_from_setup(setup: dict) -> dict:
    """The setup text's last pit reading: LF/LR are OMI (outer, middle, inner), RF/RR are IMO (inner, middle, outer)."""
    out = {}
    for sec in setup.values():
        if not isinstance(sec, dict):
            continue
        for k, v in sec.items():
            c = CORNER_KEYS.get(k)
            if not c or not isinstance(v, dict):
                continue
            if "LastTempsOMI" in v:
                n = ibt.numbers(v["LastTempsOMI"])
                if len(n) == 3:
                    out[c] = (n[0], n[1], n[2])
            elif "LastTempsIMO" in v:
                n = ibt.numbers(v["LastTempsIMO"])
                if len(n) == 3:
                    out[c] = (n[2], n[1], n[0])
    return out


def _pct_bin(p: float) -> int:
    return int(min(19, max(0, p * 20)))


def _detail(path) -> dict:
    """The extra per-file facts Setup Lab needs (one pass over a few channels). Never raises."""
    try:
        with ibt.open_ibt(path) as ib:
            return _detail_of(ib)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200], "os_error": isinstance(e, OSError)}


def _detail_of(ib) -> dict:
    di = ib.session.get("DriverInfo") or {}
    rate = ib.tick_rate or 60
    out = {"ok": True, "duration_s": round(ib.record_count / rate, 1),
           "setup_name": str(di.get("DriverSetupName") or ib.player().get("DriverSetupName") or ""),
           "setup_modified": di.get("DriverSetupIsModified"), "notes": []}
    if out["setup_name"] in ("None", "null"):
        out["setup_name"] = ""
    out["setup_load"] = str(di.get("DriverSetupLoadTypeName") or "")       # "fixed" (iRacing's) or "user"
    # the session itself says whether the setup was fixed (real files: 0 in the GT4 Falken race, 1 in the MX-5)
    wi = ib.session.get("WeekendInfo") or {}
    fx = (wi.get("WeekendOptions") or {}).get("IsFixedSetup")
    out["fixed_setup"] = bool(int(fx)) if str(fx).strip() in ("0", "1") else None
    out["official"] = str(wi.get("Official")).strip() == "1"
    out["event"] = str(wi.get("EventType") or "")
    # race length from the session text (a timed race says "1800.0000 sec")
    race = None
    for s in (ib.session.get("SessionInfo") or {}).get("Sessions") or []:
        if str(s.get("SessionType") or "").lower() == "race":
            secs = ibt.number(s.get("SessionTime")) if "unlimited" not in str(s.get("SessionTime")) else None
            lp = s.get("SessionLaps")
            race = {"minutes": round(secs / 60, 1) if secs else None,
                    "laps": int(lp) if isinstance(lp, (int, float)) or str(lp).isdigit() else None}
    out["race"] = race
    # conditions: live track temperature if recorded, else the session header
    tr = ib.track()
    temp = None
    for ch in ("TrackTempCrew", "TrackTemp"):
        if ib.has(ch):
            a = ib.col(ch)
            a = a[np.isfinite(a)]
            if len(a):
                temp = float(np.median(a))
                break
    out["surface_temp"] = round(temp, 1) if temp is not None else tr.get("surface_temp")
    state = "wet" if tr.get("wet") else "dry"
    if ib.has("WeatherDeclaredWet") and ib.col("WeatherDeclaredWet").astype(bool).any():
        state = "wet"
    elif ib.has("TrackWetness"):
        w = float(np.median(ib.col("TrackWetness")))
        if w >= 3:
            state = "wet"
        elif w >= 2 and state == "dry":
            state = "damp"
    out["track_state"] = state

    laps = ibt.laps(ib)
    press = {c: ib.col(f"{c}pressure") for c in ("LF", "RF", "LR", "RR") if ib.has(f"{c}pressure")}
    fuel = ib.col("FuelLevel") if ib.has("FuelLevel") else None
    pct = ib.col("LapDistPct") if ib.has("LapDistPct") else None
    speed = ib.col("Speed") if ib.has("Speed") else None
    side = ib.col("CarLeftRight").astype(int) if ib.has("CarLeftRight") else None
    rows, stint, pos, prev = [], 0, 0, None
    clean_idx, clean_no = [], []
    for x in laps:
        if prev is not None and (x["lap"] != prev["lap"] + 1 or x["session_num"] != prev["session_num"] or prev["pit"]):
            stint += 1
            pos = 0
        a, b = x["start"], x["end"]
        # pos counts the stint's flying laps (timed, off pit road): 1 is the first flying lap, tyres still warming
        # up; 0 is an out lap / pit lap. (At Zolder the stop sits inside one lap, so the next lap is already flying.)
        flying = x["time"] is not None and not x["pit"]
        if flying:
            pos += 1
        row = {"lap": x["lap"], "time": round(x["time"], 3) if x["time"] else None, "session": x["session"],
               "clean": x["clean"], "start_lap": x["start_lap"], "pit": x["pit"], "stint": stint,
               "pos": pos if flying else 0,
               "warm": not flying or pos <= 1, "traffic": False}
        if fuel is not None:
            row["fuel_level"] = round(float(fuel[a]), 2)
        if x["clean"]:
            clean_idx.append((a, b))
            clean_no.append(x["lap"])
            if side is not None and b > a:
                busy = float(np.mean((side[a:b] != 0) & (side[a:b] != 1)))
                row["traffic"] = busy > TRAFFIC_SIDE
            if press:
                # one consistent spot: the fastest sample of the lap (else the lap median)
                k = a + int(np.argmax(speed[a:b])) if speed is not None and b > a else None
                row["press"] = {c: round(float(v[k] if k is not None else np.median(v[a:b])), 1) for c, v in press.items()}
            if fuel is not None and b - a > 1:
                used = float(fuel[a] - fuel[b - 1])
                if 0 < used < 20:
                    row["fuel"] = round(used, 3)
            if pct is not None and b - a > 40:
                # time in each 5% of the lap, so time lost at one spot can be seen
                pp = pct[a:b].astype(float).copy()
                j = 0
                while j < min(len(pp), 10) and pp[j] > 0.5:          # the last samples before the line
                    pp[j] = 0.0
                    j += 1
                pp = np.maximum.accumulate(pp)
                edges = np.searchsorted(pp, np.linspace(0, 1, 21)[1:-1])
                tt = np.concatenate(([0], edges, [b - a])) / rate
                row["bins"] = [round(float(v), 3) for v in np.diff(tt)]
        rows.append(row)
        prev = x
    out["laps"] = rows

    # pit readings: the carcass temps at the first sample and wherever they change (only at a pit-stall stop).
    # A reading made at a stop belongs to the stint that ended there; its length says how much to trust it.
    readings = []
    if all(ib.has(f"{c}temp{z}") for c in ("LF", "RF", "LR", "RR") for z in ("CL", "CM", "CR")):
        vals = {c: [ib.col(f"{c}temp{z}") for z in ("CL", "CM", "CR")] for c in ("LF", "RF", "LR", "RR")}
        stacked = np.stack([v for c in vals for v in vals[c]], axis=1)
        idx = [0] + [int(i) + 1 for i in np.nonzero(np.any(np.abs(np.diff(stacked, axis=0)) > 0.05, axis=1))[0]]
        st = ib.col("SessionTime") if ib.has("SessionTime") else None
        for i in idx:
            r = _reading_from_channels(vals, i)
            stint_laps = None
            if i > 0:
                before = [(row, x) for row, x in zip(rows, laps) if x["start"] < i]
                if before:
                    sid = before[-1][0]["stint"]
                    stint_laps = sum(1 for row in rows if row["stint"] == sid and row["pos"] >= 1)
            readings.append({"t": round(float(st[i]), 1) if st is not None else round(i / rate, 1),
                             "at_start": i == 0, "source": "channels", "tyres": r, "stint_laps": stint_laps,
                             "cold": all(_cold(v) for v in r.values())})
    out["readings"] = readings

    # ride heights on clean laps: the low end of the trace (to learn the car's floor across files) and, per lap,
    # the lowest point and where on the lap it was.
    ride = {}
    if clean_idx:
        for ch, label in (("LFrideHeight", "LF"), ("RFrideHeight", "RF"), ("LRrideHeight", "LR"),
                          ("RRrideHeight", "RR"), ("CFSRrideHeight", "splitter")):
            if not ib.has(ch):
                continue
            col = ib.col(ch).astype(float)
            sel = np.concatenate([np.arange(a, b) for a, b in clean_idx])
            v = col[sel]
            v = v[np.isfinite(v)]
            if len(v) < 10:
                continue
            p05 = float(np.percentile(v, 0.5))
            lo_edge = p05 - 0.010
            hist, _e = np.histogram(v, bins=HIST_BINS, range=(lo_edge, lo_edge + HIST_BINS * HIST_MM / 1000.0))
            per_lap = []
            for (a, b), no in zip(clean_idx, clean_no):
                seg = col[a:b]
                low = np.nonzero(seg <= np.nanmin(seg) + BOTTOM_MM / 1000.0)[0]
                k = int(low[len(low) // 2]) if len(low) else int(np.argmin(seg))      # middle of the low spot
                per_lap.append([round(float(seg[k]) * 1000, 1), round(float(pct[a + k]), 3) if pct is not None else None,
                                int(no)])
            ride[label] = {"p05_mm": round(p05 * 1000, 2), "n": int(len(v)), "lo_mm": round(lo_edge * 1000, 2),
                           "hist": hist.astype(int).tolist(),
                           "move_mm": round(float(np.percentile(v, 99) - np.percentile(v, 1)) * 1000, 1),
                           "laps": per_lap}
    out["ride"] = ride

    # live surface temps (info only): median outer/middle/inner on clean laps
    surf = {}
    if clean_idx and all(ib.has(f"{c}temp{z}") for c in ("LF", "RF", "LR", "RR") for z in ("L", "M", "R")):
        sel = np.concatenate([np.arange(a, b) for a, b in clean_idx])
        for c in ("LF", "RF", "LR", "RR"):
            l, m, r = (float(np.median(ib.col(f"{c}temp{z}")[sel])) for z in ("L", "M", "R"))
            o, i = (l, r) if c in ("LF", "LR") else (r, l)
            surf[c] = (round(o, 1), round(m, 1), round(i, 1))
    out["surface_temps"] = surf
    return out


def _read_file(path: Path) -> dict:
    rec = ibt.summary(path)
    if rec.get("ok"):
        det = _detail(path)
        if det.get("ok"):
            det.pop("ok", None)
            rec["detail"] = det
        else:
            rec["detail"] = None
            rec["detail_error"] = det.get("error")
            if det.get("os_error"):
                rec["os_error"] = True
    return rec


def index(folder, cache_path=None, progress=None, stop=None) -> dict:
    """Read every .ibt in `folder` once (read only). Results are cached in `cache_path` (JSON) by name, size and
    modified time, so a second call reads no file that has not changed. Never raises on a bad file.

    progress(done, total, name) is called after each file; stop() returning True ends the scan early.
    Returns {"folder", "files": [record], "read": n, "cached": n, "bad": [{"file", "error"}], "stopped": bool,
    "missing_folder": bool}. A record is ibt.summary()'s dict plus "detail", "size", "mtime", "when", "sort"."""
    folder = Path(folder)
    out = {"folder": str(folder), "files": [], "read": 0, "cached": 0, "bad": [], "stopped": False,
           "missing_folder": not folder.is_dir()}
    if out["missing_folder"]:
        return out
    cache = {}
    if cache_path:
        try:
            raw = json.loads(Path(cache_path).read_text(encoding="utf-8"))
            if raw.get("version") == CACHE_VERSION:
                cache = raw.get("files") or {}
        except Exception:  # noqa: BLE001
            cache = {}
    try:
        paths = sorted(p for p in folder.iterdir() if p.suffix.lower() == ".ibt" and p.is_file())
    except OSError as e:
        out["bad"].append({"file": str(folder), "error": str(e)[:200]})
        return out
    new_cache = {}
    total = len(paths)
    for k, p in enumerate(paths):
        if stop is not None and stop():
            out["stopped"] = True
            break
        try:
            st = p.stat()
        except OSError as e:
            out["bad"].append({"file": p.name, "error": str(e)[:200]})
            continue
        key = f"{p.name}|{st.st_size}|{st.st_mtime_ns}"
        rec = cache.get(key)
        if rec is not None:
            out["cached"] += 1
        else:
            try:
                rec = _read_file(p)
            except Exception as e:  # noqa: BLE001
                rec = {"file": p.name, "ok": False, "error": str(e)[:200], "os_error": isinstance(e, OSError)}
            rec["size"] = st.st_size
            rec["mtime"] = st.st_mtime_ns
            rec["when"], rec["sort"] = _when(p.name, rec.get("start"))
            out["read"] += 1
        if not rec.get("os_error"):       # a lock or a file still being written is not remembered: read it next time
            new_cache[key] = rec
        if rec.get("ok"):
            out["files"].append(rec)
        else:
            out["bad"].append({"file": p.name, "error": rec.get("error") or "unreadable"})
        if progress is not None:
            try:
                progress(k + 1, total, p.name)
            except Exception:  # noqa: BLE001
                pass
    if cache_path and out["read"]:
        if out["stopped"]:                       # keep what we had for the files we did not reach
            for key, rec in cache.items():
                new_cache.setdefault(key, rec)
        _save_json(Path(cache_path), {"version": CACHE_VERSION, "files": new_cache})
    out["files"].sort(key=lambda r: (r.get("sort") or 0, r["file"]))
    return out


def _save_json(path: Path, data) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass                                      # a cache that can't be saved only costs time next scan


# ================================================================ setups and runs

def _leaf(key: str) -> str:
    return key.rsplit(".", 1)[-1]


def identity(setup: dict) -> dict:
    """The flattened setup without readings, fuel and garage results (what the driver actually set)."""
    return {k: v for k, v in ibt.flatten(setup).items()
            if _leaf(k) not in READING_KEYS | FUEL_KEYS | RESULT_KEYS}


def fingerprint(setup: dict) -> str:
    """Same settings -> same fingerprint, whatever the pit readings, tyre wear or fuel load were."""
    ident = identity(setup)
    if not ident:
        return ""
    return hashlib.sha1(json.dumps(sorted(ident.items())).encode("utf-8")).hexdigest()[:12]


def _letters(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _match(rec, car_path, track_code) -> bool:
    c = (rec.get("car") or {}).get("path") or ""
    t = (rec.get("track") or {}).get("code") or ""
    return c.lower() == str(car_path).lower() and t.lower() == str(track_code).lower()


def combos(idx: dict) -> list:
    """Every car + track in the index: [{"car_path", "car", "track_code", "track", "files", "last"}], newest first."""
    seen = {}
    for r in idx.get("files") or []:
        c, t = r.get("car") or {}, r.get("track") or {}
        key = ((c.get("path") or "").lower(), (t.get("code") or "").lower())
        e = seen.setdefault(key, {"car_path": c.get("path"), "car": _names.car(c.get("name")) or c.get("name"), "track_code": t.get("code"),
                                  "track": _names.track(t.get("name"), t.get("config")),
                                  "files": 0, "last": "", "_sort": 0})
        e["files"] += 1
        if (r.get("sort") or 0) >= e["_sort"]:
            e["_sort"], e["last"] = r.get("sort") or 0, r.get("when") or ""
    out = sorted(seen.values(), key=lambda e: -e["_sort"])
    for e in out:
        e.pop("_sort")
    return out


def group(records: list, run_gap_hours: float = 3.0) -> dict:
    """Setups and runs for one car + track. records: index records (any order).

    A setup is every file with the same fingerprint, named Setup A, B, C in the order first used. A run is files in a
    row on the same setup with no more than `run_gap_hours` between one file's end and the next file's start.
    Returns {"setups": [{"id", "label", "name", "names", "fingerprint", "files", "first", "last", "setup"}],
             "runs": [{"id", "setup", "files", "first", "last", "clean_laps", "sessions"}]}"""
    recs = sorted(records, key=lambda r: (r.get("sort") or 0, r["file"]))
    setups, by_fp = [], {}
    for r in recs:
        fp = fingerprint(r.get("setup") or {})
        det = r.get("detail") or {}
        nm = (det.get("setup_name") or r.get("setup_name") or "").strip()
        if nm and det.get("setup_modified") in (1, "1", True) and det.get("setup_load") != "fixed":
            nm += ", changed"
        if fp not in by_fp:
            sid = _letters(len([s for s in setups if s["fingerprint"]])) if fp else "?"
            s = {"id": sid, "label": f"Setup {sid}" if fp else "No setup recorded", "name": nm, "names": [],
                 "fingerprint": fp, "files": [], "first": r.get("when") or "", "last": "", "setup": r.get("setup") or {}}
            by_fp[fp] = s
            setups.append(s)
        s = by_fp[fp]
        s["files"].append(r["file"])
        s["last"] = r.get("when") or ""
        if nm and nm not in s["names"]:
            s["names"].append(nm)
        r["setup_id"] = s["id"]
    for s in setups:
        if s["names"] and not s["name"]:
            s["name"] = s["names"][0]
    # setups that share a file name ("lime.sto" saved over three times) are told apart by when they were first driven
    named = [s for s in setups if s["name"] and s["fingerprint"]]
    for s in named:
        same = [o for o in named if o["name"] == s["name"]]
        if len(same) > 1:
            day = day_text(s["first"])
            s["first_day"] = day if sum(day_text(o["first"]) == day for o in same) == 1 else (
                f"{day}, {str(s['first'])[11:16]}" if len(str(s["first"])) >= 16 else day)
            s["label"] = f"Setup {s['id']} ({s['name']}, first driven {s['first_day']})"
        else:
            s["label"] = f"Setup {s['id']} ({s['name']})"
    runs = []
    for r in recs:
        end = (r.get("sort") or 0) + ((r.get("detail") or {}).get("duration_s") or 0)
        last = runs[-1] if runs else None
        if last and last["setup"] == r["setup_id"] and (r.get("sort") or 0) - last["_end"] <= run_gap_hours * 3600:
            last["files"].append(r["file"])
            last["last"] = r.get("when") or ""
            last["_end"] = max(last["_end"], end)
        else:
            last = {"id": len(runs) + 1, "setup": r["setup_id"], "files": [r["file"]], "first": r.get("when") or "",
                    "last": r.get("when") or "", "clean_laps": 0, "sessions": [], "_end": end}
            runs.append(last)
        last["clean_laps"] += r.get("clean_laps") or 0
        for ss in r.get("sessions") or []:
            if ss not in last["sessions"]:
                last["sessions"].append(ss)
    for run in runs:
        run.pop("_end")
    return {"setups": setups, "runs": runs}


# ================================================================ diff

_SETTING = {
    "Camber": "camber", "ToeIn": "toe", "StartingPressure": "starting (cold) pressure",
    "ColdPressure": "starting (cold) pressure", "SpringRate": "spring", "SpringPerchOffset": "spring perch offset",
    "BumpStiffness": "bump (compression) damper", "ReboundStiffness": "rebound damper",
    "RideHeight": "ride height (garage result)", "CornerWeight": "corner weight (garage result)",
    "NoseWeight": "nose weight (garage result)", "ArbSetting": "anti-roll bar", "ArbBlades": "anti-roll bar blades",
    "AntiRollBar": "anti-roll bar", "FarbPreload": "anti-roll bar preload", "ArbPreloadAdj": "anti-roll bar preload",
    "WingAngle": "wing", "BrakePads": "brake pads", "BrakePressureBias": "brake bias", "AbsSetting": "ABS setting",
    "AbsTcEscSetting": "ABS / traction control map", "Tc1_Long": "traction control 1", "Tc2_Lat": "traction control 2",
    "TcSetting": "traction control", "ThrottleSetting": "throttle map", "CrossWeight": "cross weight",
    "FuelLevel": "fuel", "FuelLowWarning": "low-fuel warning", "TireType": "tyre type", "TireCompound": "tyre type",
    "DashDisplayPage": "dash page", "ShockCompSetting": "shock setting",
}
_NO_PREFIX = {"BrakePads", "CrossWeight", "NoseWeight", "FuelLevel", "FuelLowWarning", "BrakePressureBias",
              "AbsSetting", "AbsTcEscSetting", "Tc1_Long", "Tc2_Lat", "TcSetting", "ThrottleSetting", "DashDisplayPage",
              "TireType", "TireCompound"}
_SECTION_PREFIX = {"Front": "front", "FrontBrakes": "front", "Rear": "rear"}

# One-line explanations for the words a non-engineer may not know.
EXPLAIN = {
    "camber": "how far the top of the tyre leans in; more negative = leans in more",
    "toe": "whether the tyres point in or out when you look from above",
    "anti-roll bar": "the bar linking left and right; stiffer = less body roll",
    "spring perch offset": "moves the spring seat, which sets the ride height",
    "bump (compression) damper": "how hard the damper resists the wheel moving up",
    "rebound damper": "how hard the damper resists the wheel dropping back down",
    "cross weight": "how the weight is shared between the diagonals",
    "brake bias": "how much of the braking goes to the front wheels",
    "starting (cold) pressure": "the tyre pressure set in the garage, before it heats up",
}


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def label(key: str) -> str:
    """Plain words for a flattened setup key: 'Chassis.LeftFront.Camber' -> 'Left front camber'."""
    parts = key.split(".")
    leaf = parts[-1]
    sec = parts[-2] if len(parts) >= 2 else ""
    words = _SETTING.get(leaf) or re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", leaf).replace("_", " ").lower()
    if leaf == "WingAngle":
        return "Rear wing"
    if leaf in _NO_PREFIX:
        return _cap(words)
    if sec in CORNER_KEYS:
        return _cap(f"{CORNER_WORDS[CORNER_KEYS[sec]]} {words}")
    if sec in _SECTION_PREFIX:
        return _cap(f"{_SECTION_PREFIX[sec]} {words}")
    return _cap(words)


def _num_text(v: str):
    m = re.search(r"[-+]?\d+(?:\.\d+)?", str(v))
    return m.group() if m else None


def _decimals(s: str | None) -> int:
    return len(s.split(".")[1]) if s and "." in s else 0


def _kind(key: str) -> str:
    leaf = _leaf(key)
    if leaf in FUEL_KEYS:
        return "fuel"
    if leaf in RESULT_KEYS:
        return "result"
    return "setting"


def _change(key: str, old, new, lab: str | None = None, both: bool = False, keys=None) -> dict:
    lab = lab or label(key)
    o_n, n_n = _num_text(old) if old is not None else None, _num_text(new) if new is not None else None
    uo, un = ibt.unit(old), ibt.unit(new)
    delta, unit = None, ""
    if o_n is not None and n_n is not None and uo == un:
        d = float(n_n) - float(o_n)
        dec = max(_decimals(o_n), _decimals(n_n))
        delta, unit = round(d, dec), uo
        text = f"{lab} {o_n} → {n_n}" + (f" {uo}" if uo else "")
    else:
        text = f"{lab} {old if old is not None else '(not set)'} → {new if new is not None else '(not set)'}"
    if both:
        text += " (both sides)"
    item = {"key": key, "keys": keys or [key], "label": lab, "old": old, "new": new, "delta": delta, "unit": unit,
            "both_sides": both, "kind": _kind(key), "text": text}
    leaf_words = _SETTING.get(_leaf(key))
    if leaf_words in EXPLAIN:
        item["explain"] = EXPLAIN[leaf_words]
    return item


def diff(setup_a: dict, setup_b: dict) -> list:
    """Settings that changed from setup_a to setup_b, in plain words. Pit readings are never listed.

    Each item: {"key", "keys", "label", "old", "new", "delta" (number or None), "unit", "both_sides", "kind"
    ("setting" | "result" | "fuel"), "text", optional "explain"}. A left/right pair that changed the same way on
    both sides is one item ("Front camber -4.0 → -3.6 deg (both sides)"). Settings come before garage results."""
    fa = {k: v for k, v in ibt.flatten(setup_a).items() if _leaf(k) not in READING_KEYS}
    fb = {k: v for k, v in ibt.flatten(setup_b).items() if _leaf(k) not in READING_KEYS}
    changed = [k for k in list(fa) + [k for k in fb if k not in fa] if fa.get(k) != fb.get(k)]
    done, out = set(), []
    for k in changed:
        if k in done:
            continue
        parts = k.split(".")
        if len(parts) >= 2 and parts[-2] in PAIR:
            other_corner, axle = PAIR[parts[-2]]
            other = ".".join(parts[:-2] + [other_corner, parts[-1]])
            if other in changed and other not in done and fa.get(k) == fa.get(other) and fb.get(k) == fb.get(other):
                words = _SETTING.get(parts[-1]) or label(parts[-1]).lower()
                group_key = ".".join(parts[:-2] + ["*" + axle, parts[-1]])
                out.append(_change(group_key, fa.get(k), fb.get(k), lab=f"{axle} {words}", both=True, keys=[k, other]))
                out[-1]["kind"] = _kind(k)
                done.update((k, other))
                continue
        out.append(_change(k, fa.get(k), fb.get(k)))
        done.add(k)
    order = {"setting": 0, "fuel": 1, "result": 2}
    out.sort(key=lambda x: order[x["kind"]])
    return out


# ================================================================ compare

def _mad(xs) -> float:
    if not xs:
        return 0.0
    a = np.asarray(xs, float)
    return float(np.median(np.abs(a - np.median(a))))


def _too_slow(t: float, med: float, mad: float) -> bool:
    return t > med + MAD_K * mad if mad > 0 else t > TRAFFIC * med


def pace_laps(recs: list) -> tuple[list, dict]:
    """The laps that show a setup's pace: clean, not a race start, not the out lap or the first flying lap of a stint
    (tyres warming up), not a lap spent alongside another car (CarLeftRight, >10% of the lap), and not more than
    3 x MAD slower than its stint's median (107% when the MAD is 0; stints under 3 laps use the setup's own spread).
    Returns ([(file, row)], {"warmup", "traffic", "slow"} counts of laps left out)."""
    cand, dropped = [], {"warmup": 0, "traffic": 0, "slow": 0}
    for r in recs:
        for x in ((r.get("detail") or {}).get("laps") or []):
            if not (x.get("clean") and not x.get("start_lap") and x.get("time")):
                continue
            if x.get("warm"):
                dropped["warmup"] += 1
            elif x.get("traffic"):
                dropped["traffic"] += 1
            else:
                cand.append((r["file"], x))
    if not cand:
        return [], dropped
    all_t = [x["time"] for _f, x in cand]
    g_med, g_mad = float(np.median(all_t)), _mad(all_t)
    stints = {}
    for f, x in cand:
        stints.setdefault((f, x["stint"]), []).append(x["time"])
    keep = []
    for f, x in cand:
        ts = stints[(f, x["stint"])]
        med, mad = (float(np.median(ts)), _mad(ts)) if len(ts) >= 3 else (g_med, g_mad)
        if _too_slow(x["time"], med, mad):
            dropped["slow"] += 1
        else:
            keep.append((f, x))
    return keep, dropped


def _setup_pace(recs: list) -> dict:
    keep, dropped = pace_laps(recs)
    out = {"files": [r["file"] for r in recs], "clean_laps": len(keep), "median": None, "best": None, "mad": None,
           "stints": [], "left_out": dropped, "surface_temp": None, "track_state": "dry", "fuel": None,
           "sessions": [], "times": []}
    temps = [(r.get("detail") or {}).get("surface_temp") for r in recs if (r.get("detail") or {}).get("laps")]
    temps = [t for t in temps if t is not None]
    out["surface_temp"] = round(float(np.median(temps)), 1) if temps else None
    states = {(r.get("detail") or {}).get("track_state") or "dry" for r in recs}
    out["track_state"] = "wet" if "wet" in states else ("damp" if "damp" in states else "dry")
    if not keep:
        return out
    times = [x["time"] for _f, x in keep]
    out["times"] = [round(t, 3) for t in times]
    out["median"] = round(float(np.median(times)), 3)
    out["best"] = round(min(times), 3)
    out["mad"] = round(_mad(times), 3)
    fl = [x["fuel_level"] for _f, x in keep if x.get("fuel_level") is not None]
    out["fuel"] = round(float(np.mean(fl)), 1) if fl else None
    stints = {}
    for f, x in keep:
        stints[(f, x["stint"])] = stints.get((f, x["stint"]), 0) + 1
    out["stints"] = [{"file": f, "stint": s, "clean_laps": n} for (f, s), n in stints.items()]
    out["sessions"] = sorted({x.get("session") for _f, x in keep if x.get("session")})
    return out


def _gate(p: dict, label_: str) -> list:
    """What is missing before this setup's pace can be trusted (empty list = enough)."""
    if p["clean_laps"] >= MIN_LAPS:
        return []
    n = MIN_LAPS - p["clean_laps"]
    return [f"{label_} needs {n} more clean lap{'s' if n > 1 else ''} (has {p['clean_laps']}; "
            f"{MIN_LAPS} is the minimum)"]


def _fmt_s(x: float) -> str:
    return f"{x:.1f}" if x >= 0.095 else f"{x:.2f}"


def _boot_ci(ta: list, tb: list, sa: list | None = None, sb: list | None = None) -> tuple[float, float, int, str]:
    """Bootstrap interval of median(a) - median(b) (fixed seed, so the same laps always give the same answer).

    sa / sb: the same laps grouped by stint. When each setup has at least BLOCK_STINTS stints, whole stints are
    resampled (a block bootstrap: laps in one stint share its fuel, tyres and rubber, so they are not independent)
    and the interval is 90%. Otherwise laps are resampled, and with fewer than SMALL_N laps on either setup the
    interval is widened to 95%. Returns (lo, hi, level %, "stints" | "laps")."""
    rng = np.random.default_rng(BOOT_SEED)
    if sa and sb and len(sa) >= BLOCK_STINTS and len(sb) >= BLOCK_STINTS:
        def meds(stints):
            arrs = [np.asarray(x, float) for x in stints]
            pick = rng.integers(0, len(arrs), (BOOT_N, len(arrs)))
            return np.array([np.median(np.concatenate([arrs[j] for j in row])) for row in pick])
        ma, mb, level, how = meds(sa), meds(sb), 90, "stints"
    else:
        a, b = np.asarray(ta, float), np.asarray(tb, float)
        ma = np.median(a[rng.integers(0, len(a), (BOOT_N, len(a)))], axis=1)
        mb = np.median(b[rng.integers(0, len(b), (BOOT_N, len(b)))], axis=1)
        level, how = (95 if min(len(a), len(b)) < SMALL_N else 90), "laps"
    tail = (100 - level) / 2
    lo, hi = np.percentile(ma - mb, [tail, 100 - tail])
    return float(lo), float(hi), level, how


def _more_laps(ta: list, tb: list, d: float) -> int:
    """Rough clean laps still needed on each setup for a 90% interval to clear a gap of max(|d|, 0.1 s)."""
    resid = [t - float(np.median(ta)) for t in ta] + [t - float(np.median(tb)) for t in tb]
    sigma = 1.4826 * float(np.median(np.abs(resid))) if resid else 0.0
    effect = max(abs(d), MIN_DIFF)
    if sigma <= 0:
        return 1
    need = 2 * (1.645 * 1.2533 * sigma / effect) ** 2          # SE of a median ~ 1.2533 sigma / sqrt(n)
    return max(1, int(np.ceil(need - min(len(ta), len(tb)))))


def lap_trend(stints: list) -> dict | None:
    """How lap times moved within a run: a Theil-Sen slope (median of every pairwise slope inside each stint,
    pooled over stints, so the gaps between stints and days never count) with Sen's 90% interval from Kendall's
    S. stints: [(xs, ys)], xs = lap order, ys = lap times; stints under 3 laps are skipped.
    Returns {"slope", "lo", "hi" (s per lap; negative = getting quicker), "stints", "pairs"} or None."""
    slopes, var, used = [], 0.0, 0
    for xs, ys in stints:
        n = len(xs)
        if n < 3:
            continue
        used += 1
        var += n * (n - 1) * (2 * n + 5) / 18.0
        for i in range(n):
            for j in range(i + 1, n):
                if xs[j] != xs[i]:
                    slopes.append((ys[j] - ys[i]) / (xs[j] - xs[i]))
    if len(slopes) < 3:
        return None
    slopes.sort()
    N = len(slopes)
    c = 1.645 * float(np.sqrt(var))
    lo_i, hi_i = max(0, int(np.floor((N - c) / 2))), min(N - 1, int(np.ceil((N + c) / 2)))
    return {"slope": round(float(np.median(slopes)), 4), "lo": round(slopes[lo_i], 4), "hi": round(slopes[hi_i], 4),
            "stints": used, "pairs": N}


def _lap_order(recs: list) -> dict:
    """Every timed lap of this car + track numbered in the order he drove them: {(file, lap): n}."""
    xmap, k = {}, 0
    for r in sorted(recs, key=lambda r: (r.get("sort") or 0, r["file"])):
        for x in ((r.get("detail") or {}).get("laps") or []):
            if x.get("time"):
                xmap[(r["file"], x["lap"])] = k
                k += 1
    return xmap


def interleaved(a_id: str, b_id: str, recs: list, files_with_pace: set) -> bool:
    """Driven A, B, A (or B, A, B) in time: going back to the first setup cancels his own improvement and the track
    rubbering in. Only files that gave counted laps take part."""
    run = []
    for r in sorted(recs, key=lambda r: (r.get("sort") or 0, r["file"])):
        sid = r.get("setup_id")
        if sid in (a_id, b_id) and r["file"] in files_with_pace and (not run or run[-1] != sid):
            run.append(sid)
    return len(run) >= 3


def _pair(a: dict, b: dict, ctx: dict | None = None) -> dict:
    pa, pb = a["pace"], b["pace"]
    ctx = ctx or {}
    out = {"a": a["id"], "b": b["id"], "verdict": "not enough to tell", "faster": None, "diff_s": None,
           "ci90": None, "ci": None, "ci_level": None, "more_laps": None, "comparable": True, "confounders": [],
           "missing": [], "interleaved": None, "trend": None, "drift_s": None, "caution": None, "text": ""}
    out["missing"] = a["missing"] + b["missing"]
    if pa["median"] is not None and pb["median"] is not None:
        out["diff_s"] = round(pa["median"] - pb["median"], 3)        # > 0: b is faster
    ta, tb = pa["surface_temp"], pb["surface_temp"]
    if ta is not None and tb is not None and abs(ta - tb) > MAX_TEMP_GAP:
        warm = a if ta > tb else b
        out["confounders"].append(f"the track was {abs(ta - tb):.0f} °C warmer for {warm['label']} "
                                  f"({max(ta, tb):.0f} °C vs {min(ta, tb):.0f} °C)")
    fa, fb = pa["fuel"], pb["fuel"]
    if fa is not None and fb is not None and abs(fa - fb) > MAX_FUEL_GAP:
        heavy = a if fa > fb else b
        out["confounders"].append(f"{heavy['label']} carried about {abs(fa - fb):.0f} L more fuel on average "
                                  f"({max(fa, fb):.0f} L vs {min(fa, fb):.0f} L)")
    for s in (a, b):
        if s["pace"]["track_state"] != "dry":
            out["confounders"].append(f"{s['label']} was driven on a {s['pace']['track_state']} track")
    out["comparable"] = not out["confounders"]
    if out["missing"]:
        out["text"] = (f"{a['label']} vs {b['label']}: not enough to tell yet. " + _cap("; ".join(out["missing"]))
                       + (". Also, " + "; ".join(out["confounders"]) if out["confounders"] else "") + ".")
        return out
    if out["confounders"]:
        out["verdict"] = "confounded"
        out["text"] = (f"{a['label']} vs {b['label']} can't be compared fairly: " + "; ".join(out["confounders"])
                       + ". The lap times are shown but not adjusted. Drive both in similar conditions.")
        return out
    d = out["diff_s"]
    lo, hi, level, how = _boot_ci(pa["times"], pb["times"], a.get("_stints"), b.get("_stints"))
    out["ci"], out["ci_level"], out["ci_method"] = [round(lo, 3), round(hi, 3)], level, how
    out["ci90"] = out["ci"]                    # kept for older readers: the interval used, at ci_level %
    # driving order: was one setup simply driven later, while he was getting quicker?
    xa, xb = a.get("_x") or [], b.get("_x") or []
    later = None
    if xa and xb:
        later = b if float(np.median(xb)) > float(np.median(xa)) else a
    out["interleaved"] = bool(ctx.get("interleaved"))
    trend = lap_trend((a.get("_trend") or []) + (b.get("_trend") or []))
    out["trend"] = trend
    real = lo > 0 or hi < 0
    fast, slow = ((b, a) if d > 0 else (a, b)) if d else (None, None)
    if real and fast is not None and later is fast and not out["interleaved"]:
        gap_laps = abs(float(np.median(xb)) - float(np.median(xa)))
        quicker = -trend["hi"] if trend and trend["hi"] < 0 else 0.0     # the cautious end of the interval
        drift = quicker * gap_laps
        out["drift_s"] = round(drift, 3)
        if drift >= 0.5 * abs(d):
            out["verdict"] = "confounded"
            out["comparable"] = False
            why = (f"you were getting quicker as you drove (about {quicker:.2f} s a lap within each run, so about "
                   f"{drift:.1f} s over the {gap_laps:.0f} laps between the two setups)")
            out["confounders"].append(why)
            out["text"] = (f"{a['label']} vs {b['label']} can't be compared fairly yet: {fast['label']} looked "
                           f"{_fmt_s(abs(d))} s a lap quicker, but {why}, which could explain at least half of it. "
                           f"Go back to {slow['label']} for a few laps to be sure.")
            return out
        out["caution"] = (f"{fast['label']} was also driven later, so part of this may be you getting quicker. "
                          f"Go back to {slow['label']} for a few laps to be sure.")
    if real and abs(d) >= MIN_DIFF:
        out["verdict"] = "faster"
        out["faster"] = fast["id"]
        out["text"] = (f"{fast['label']} was faster than {slow['label']} by about {_fmt_s(abs(d))} s a lap "
                       f"(typical clean lap {lap_text(fast['pace']['median'])} vs {lap_text(slow['pace']['median'])}, "
                       f"{fast['pace']['clean_laps']} and {slow['pace']['clean_laps']} clean laps)."
                       + (" " + out["caution"] if out["caution"] else ""))
        return out
    out["verdict"] = "no clear difference"
    out["caution"] = None
    gap = f"typical laps {lap_text(pa['median'])} vs {lap_text(pb['median'])}"
    if real:
        out["text"] = (f"{fast['label']} was about {_fmt_s(abs(d))} s a lap quicker than {slow['label']}: smaller than "
                       f"the {MIN_DIFF:.1f} s Debrief counts as a real difference.")
        return out
    n = _more_laps(pa["times"], pb["times"], d)
    out["more_laps"] = n
    if n > MAX_MORE_LAPS:
        out["text"] = (f"No clear difference between {a['label']} and {b['label']} ({gap}): they are so close it would "
                       f"take more than {MAX_MORE_LAPS} more clean laps on each to tell them apart.")
    else:
        out["text"] = (f"No clear difference yet between {a['label']} and {b['label']} ({gap}): if the gap is real, "
                       f"about {n} more clean lap{'s' if n != 1 else ''} on each would show it.")
    return out


def compare(runs, setups: list | None = None) -> dict:
    """Pace per setup for one car + track, with the evidence gate.

    runs: the index records for one car + track (group() is run on them unless `setups` from group() is given).
    A setup needs 5 counted laps (pace_laps()). A winner is named only when the bootstrap interval of the
    difference of medians excludes zero (whole stints resampled when each setup has 3+ stints; 95% instead of 90%
    with under 10 laps) and the gap is at least 0.1 s. Very different track temperature or fuel, or a damp/wet
    track, makes the pair "confounded" (said, never adjusted). So does driving order: when the quicker setup was
    simply driven later (not A, B, A) and his own within-run improvement (lap_trend) over the laps between them
    could explain at least half the gap; a smaller drift still adds a caution to a "faster" verdict.
    Returns {"setups": [{"id", "label", "pace": {...}, "enough": bool, "missing": [...]}],
             "pairs": [{"a", "b", "verdict" ("faster" | "no clear difference" | "not enough to tell" | "confounded"),
                        "faster", "diff_s" (a median - b median), "ci" [lo, hi], "ci_level" (90 | 95), "ci90"
                        (same as ci), "more_laps", "comparable", "confounders", "missing", "interleaved", "trend",
                        "drift_s", "caution", "text"}],
             "verdict": plain sentences, "headline": one short sentence for the top of the page (the pairs' own
             texts follow it, so it never repeats them), "not_compared": [why a setup isn't compared with the others]}.
    Pairs are consecutive setups first (A vs B, B vs C), then every other pair where both setups have enough laps
    (A vs C), each judged by the same rules."""
    recs = list(runs)
    g = {"setups": setups} if setups is not None else group(recs)
    xmap = _lap_order(recs)
    rows, pace_files = [], {}
    for s in g["setups"]:
        if not s["fingerprint"]:
            continue
        mine = [r for r in recs if r.get("setup_id") == s["id"]]
        p = _setup_pace(mine)
        miss = _gate(p, s["label"])
        keep, _d = pace_laps(mine)
        by_stint = {}
        for f, x in keep:
            by_stint.setdefault((f, x["stint"]), []).append(x)
        trend = []
        for (f, _st), xs in by_stint.items():
            pts = sorted((xmap[(f, x["lap"])], x["time"]) for x in xs if (f, x["lap"]) in xmap)
            trend.append(([q for q, _t in pts], [t for _q, t in pts]))
        pace_files[s["id"]] = {f for f, _x in keep}
        rows.append({"id": s["id"], "label": s["label"], "pace": p, "enough": not miss, "missing": miss,
                     "_x": [xmap[(f, x["lap"])] for f, x in keep if (f, x["lap"]) in xmap],
                     "_stints": [[x["time"] for x in xs] for xs in by_stint.values()], "_trend": trend})
    pairs = []
    for i in range(len(rows) - 1):
        a, b = rows[i], rows[i + 1]
        il = interleaved(a["id"], b["id"], recs, pace_files[a["id"]] | pace_files[b["id"]])
        pairs.append(_pair(a, b, {"interleaved": il}))
    not_compared = []
    for i in range(len(rows)):
        for j in range(i + 2, len(rows)):
            a, b = rows[i], rows[j]
            if a["enough"] and b["enough"]:
                il = interleaved(a["id"], b["id"], recs, pace_files[a["id"]] | pace_files[b["id"]])
                pairs.append(_pair(a, b, {"interleaved": il}))
    if len(rows) > 2:
        for r in rows:
            if not r["enough"]:
                need = MIN_LAPS - r["pace"]["clean_laps"]
                not_compared.append(f"{r['label']} isn't compared with the other setups yet: it needs {need} more "
                                    f"clean lap{'s' if need != 1 else ''} ({MIN_LAPS} is the minimum).")
    for r in rows:
        for k in ("_x", "_stints", "_trend"):
            r.pop(k, None)
    headline = None
    if not rows:
        verdict = "No laps with a setup recorded for this car and track yet."
    elif len(rows) == 1:
        r = rows[0]
        verdict = (f"Only one setup so far ({r['label']}). Try one change and drive it to compare."
                   + ("" if r["enough"] else " " + "; ".join(r["missing"]) + "."))
    else:
        decided = [p for p in pairs if p["verdict"] in ("faster", "no clear difference")]
        verdict = " ".join(p["text"] for p in pairs)
        if not decided:
            verdict = "Not enough to tell which setup is faster yet. " + verdict
        lab = {r["id"]: r["label"] for r in rows}
        wins = [p for p in pairs if p["verdict"] == "faster"]
        if len(pairs) == 1:
            headline = pairs[0]["text"] if decided else verdict
        elif wins:
            headline = ("; ".join(f"{lab[p['faster']]} was faster than {lab[p['b'] if p['faster'] == p['a'] else p['a']]}"
                                  for p in wins) + ". Each comparison is below.")
        elif decided:
            headline = "No setup was clearly faster than another yet. Each comparison is below."
        else:
            headline = "Not enough to tell which setup is faster yet. Each comparison is below."
    return {"setups": rows, "pairs": pairs, "verdict": verdict.strip(), "headline": (headline or verdict).strip(),
            "not_compared": not_compared}


def change_into(records: list, setup_id: str) -> dict | None:
    """His change into setup `setup_id`: the settings that changed from the setup he drove just before he first used
    it, and whether the laps showed that change made the car slower (Setup Lab's own verdict, nothing new).
    Returns {"from", "to" ("Setup B"), "from_id", "to_id", "changes": diff() settings, "verdict", "slower"} or None."""
    try:
        recs = list(records)
        g = group(recs)
        by = {s["id"]: s for s in g["setups"] if s["fingerprint"]}
        if setup_id not in by:
            return None
        order = [run["setup"] for run in g["runs"] if run["setup"] in by]
        prev = next((a for a, b in zip(order, order[1:]) if b == setup_id and a != setup_id), None)
        if prev is None:
            return None
        changes = [c for c in diff(by[prev]["setup"], by[setup_id]["setup"]) if c["kind"] == "setting"]
        cmp_ = compare(recs, setups=g["setups"])
        pair = next((p for p in cmp_["pairs"] if {p["a"], p["b"]} == {prev, setup_id}), None)
        return {"from": f"Setup {prev}", "to": f"Setup {setup_id}", "from_id": prev, "to_id": setup_id,
                "changes": changes, "verdict": pair["verdict"] if pair else None,
                "slower": bool(pair and pair["verdict"] == "faster" and pair["faster"] == prev)}
    except Exception:  # noqa: BLE001
        return None


# ================================================================ suggestions

AFTER = " Then compare again."
FIXED_TEXT = ("Fixed setup series: the setup is locked, tyre pressures included, so there are no garage changes to "
              "try. What is still yours is the fuel you carry and the in-car dials, such as brake bias, traction "
              "control and ABS, on cars that have them.")
TEMP_CAVEAT = ("Lap time beats temperature rules: pressures that look wrong on the temperatures often work in iRacing, "
               "so keep a change only if the laps are as fast or faster.")


def series_setup_kind(value, override=None) -> str:
    """'open' | 'fixed' | 'unknown' from the schedule's value (which may be None) and his own override."""
    for v in (override, value):
        if isinstance(v, str) and v.lower() in ("open", "fixed"):
            return v.lower()
    return "unknown"


def series_setup_from_files(recs: list) -> tuple[str, str]:
    """What the telemetry itself says (WeekendOptions.IsFixedSetup), from official sessions only (newest first): an
    offline test session says "fixed" whatever the series is (real Mustang file, Nov 2026), so it never decides.
    Returns ("open" | "fixed" | "unknown", source text)."""
    rs = sorted(recs, key=lambda r: (r.get("sort") or 0), reverse=True)
    for r in rs:
        det = r.get("detail") or {}
        if det.get("fixed_setup") is not None and det.get("official"):
            kind = "fixed" if det["fixed_setup"] else "open"
            return kind, f"the telemetry (the official session on {r.get('when') or r['file']} was {kind} setup)"
    if any((r.get("detail") or {}).get("fixed_setup") is not None for r in rs):
        return "unknown", "only test or unofficial sessions so far, which don't say what the series uses"
    return "unknown", ""


def known_fixed(car_path, car_name=None) -> str:
    """Where Debrief knows a car only races fixed setups in iRacing's official series: the source words, else ''."""
    path, name = str(car_path or "").lower().strip(), str(car_name or "").lower()
    for p, n, words in KNOWN_FIXED:
        if path.startswith(p) or (n and n in name):
            return f"Debrief's list of fixed-setup cars ({words} races fixed setups in iRacing's official series)"
    return ""


def series_setup_guess(recs: list, car_path, car_name=None) -> tuple[str, str]:
    """What Setup Lab can tell without his choice or the schedule: the telemetry first, then the cars known to race
    fixed setups only. Returns ("open" | "fixed" | "unknown", source text)."""
    kind, source = series_setup_from_files(recs)
    if kind != "unknown":
        return kind, source
    car_name = car_name or next(((r.get("car") or {}).get("name") for r in reversed(recs) if (r.get("car") or {}).get("name")), "")
    known = known_fixed(car_path, car_name)
    if known:
        return "fixed", known
    return kind, source


def _setting_value(setup: dict, corner_key: str, leaf: str):
    """Text of a corner setting (e.g. ('LeftFront', 'Camber')) wherever the car keeps it."""
    for k, v in ibt.flatten(setup).items():
        parts = k.split(".")
        if len(parts) >= 2 and parts[-2] == corner_key and parts[-1] == leaf:
            return v
    return None


def _pressure_key(setup: dict, corner_key: str):
    for leaf in ("StartingPressure", "ColdPressure"):
        v = _setting_value(setup, corner_key, leaf)
        if v is not None:
            return leaf, v
    return None, None


_CK = {"LF": "LeftFront", "RF": "RightFront", "LR": "LeftRear", "RR": "RightRear"}


def _collect_readings(recs: list) -> tuple[list, dict]:
    """Hot pit readings after a stint of >= 5 laps, de-duplicated by value across files (a file's first sample
    repeats the last stop). Returns (readings, {"cold", "short", "no_stint"} counts of readings left out)."""
    seen, out, skip = set(), [], {"cold": 0, "short": 0, "no_stint": 0}
    cand = []
    for r in recs:
        det = r.get("detail") or {}
        for x in det.get("readings") or []:
            cand.append((r["file"], x))
        st = _reading_from_setup(r.get("setup") or {})
        if len(st) == 4:
            cand.append((r["file"], {"source": "setup", "tyres": st, "stint_laps": None,
                                     "cold": all(_cold(v) for v in st.values())}))
    # readings made at a stop in these files first, so a repeat at a later file's start is matched to its stint
    cand.sort(key=lambda fx: fx[1].get("stint_laps") is None)
    for f, x in cand:
        tyres = {c: tuple(round(float(v), 1) for v in t3) for c, t3 in x["tyres"].items()}
        key = tuple(sorted((c, tuple(round(v) for v in t3)) for c, t3 in tyres.items()))
        if key in seen:
            continue
        seen.add(key)
        if x.get("cold") or all(_cold(v) for v in tyres.values()):
            skip["cold"] += 1
        elif x.get("stint_laps") is None:
            skip["no_stint"] += 1
        elif x["stint_laps"] < TEMP_STINT_LAPS:
            skip["short"] += 1
        else:
            out.append({"file": f, "source": x.get("source"), "tyres": tyres, "stint_laps": x["stint_laps"]})
    return out, skip


def _files_text(n_read: int, files: set) -> str:
    return (f"{n_read} pit reading{'s' if n_read != 1 else ''} after stints of {TEMP_STINT_LAPS}+ laps, "
            f"{len(files)} file{'s' if len(files) != 1 else ''}")


def _change_item(rule, setting, direction, why, action, check, data, confidence, **extra) -> dict:
    it = {"rule": rule, "setting": setting, "direction": direction, "why": why, "action": action + AFTER,
          "check": check, "data": data, "confidence": confidence, "kind": "change"}
    it.update(extra)
    it["text"] = f"{why} {it['action']}" + (f" Check: {check}" if check else "")
    return it


def _info_item(rule, setting, why, check, data, confidence, **extra) -> dict:
    it = {"rule": rule, "setting": setting, "direction": None, "why": why, "check": check, "data": data,
          "confidence": confidence, "kind": "info"}
    it.update(extra)
    it["text"] = (f"{why} Check: {check}" if check else why).strip()
    return it


def _temp_groups(per: dict, pair: tuple) -> list:
    a, b = per.get(pair[0]), per.get(pair[1])
    if a and b and a["way"] == b["way"]:
        return [pair]
    return [(c,) for c in pair if per.get(c)]


def _camber_items(readings: list, setup: dict) -> list:
    out = []
    for axle, pair in (("front", ("LF", "RF")), ("rear", ("LR", "RR"))):
        lo_t, hi_t = CAMBER_TARGET[axle]
        per = {}
        for c in pair:
            vals = [x["tyres"][c] for x in readings if c in x["tyres"] and not _cold(x["tyres"][c])]
            if not vals:
                continue
            med = float(np.median([i - o for o, _m, i in vals]))
            way = "less" if med > hi_t + CAMBER_MARGIN else ("more" if med < lo_t - CAMBER_MARGIN else None)
            if way:
                per[c] = {"median": med, "way": way, "n": len(vals),
                          "files": {x["file"] for x in readings if c in x["tyres"]}}
        for cs in _temp_groups(per, pair):
            info = [per[c] for c in cs]
            way = info[0]["way"]
            med = float(np.mean([i["median"] for i in info]))
            n = min(i["n"] for i in info)
            files = set().union(*[i["files"] for i in info])
            cur_n = ibt.number(_setting_value(setup, _CK[cs[0]], "Camber"))
            new = None
            if cur_n is not None:
                new = round(min(0.0, cur_n + 0.1) if way == "less" else cur_n - 0.1, 1)
            move = f" ({cur_n:.1f} → {new:.1f})" if new is not None else ""
            where = f"both {axle} tyres" if len(cs) == 2 else f"the {CORNER_WORDS[cs[0]]}"
            sides = f" (left {per[cs[0]]['median']:.0f}, right {per[cs[1]]['median']:.0f})" if len(cs) == 2 else ""
            target = f"For a GT car the inside is usually {lo_t:.0f} to {hi_t:.0f} °C hotter at the {axle}."
            if way == "less":
                why = f"Inside edge of {where} ran about {med:.0f} °C hotter than the outside{sides}. {target}"
                check = (f"after a 5-lap run the inside should be about {(lo_t + hi_t) / 2:.0f} °C hotter, "
                         f"not {med:.0f}. {TEMP_CAVEAT}")
            else:
                usual = f"For a GT car it is usually {lo_t:.0f} to {hi_t:.0f} °C hotter."
                if med >= 0.5:
                    why = f"The inside of {where} was only {med:.0f} °C hotter than the outside{sides}. {usual}"
                elif med > -0.5:
                    why = f"The inside and outside of {where} ran at about the same temperature{sides}. {usual}"
                else:
                    why = (f"Outside edge of {where} ran {abs(med):.0f} °C hotter than the inside{sides}. "
                           f"For a GT car the inside is usually {lo_t:.0f} to {hi_t:.0f} °C hotter.")
                check = f"after a 5-lap run the inside should read {lo_t:.0f} to {hi_t:.0f} °C hotter. {TEMP_CAVEAT}"
            setting = f"{_cap(axle)} camber" if len(cs) == 2 else _cap(f"{CORNER_WORDS[cs[0]]} camber")
            out.append(_change_item("camber", setting, f"{way} negative", why,
                                    f"Try 0.1° {way} negative {axle} camber{move}, the smallest step.", check,
                                    f"pit-stall tyre carcass temperatures, {_files_text(n, files)}", "low-medium",
                                    explain=EXPLAIN["camber"], corners=list(cs)))
    return out


def _pressure_temp_items(readings: list, setup: dict) -> list:
    out = []
    for axle, pair in (("front", ("LF", "RF")), ("rear", ("LR", "RR"))):
        per = {}
        for c in pair:
            vals = [x["tyres"][c] for x in readings if c in x["tyres"] and not _cold(x["tyres"][c])]
            if not vals:
                continue
            crown = float(np.median([m - (o + i) / 2 for o, m, i in vals]))
            if abs(crown) > CROWN:
                per[c] = {"way": "lower" if crown > 0 else "higher", "val": abs(crown), "n": len(vals),
                          "files": {x["file"] for x in readings if c in x["tyres"]}}
        for cs in _temp_groups(per, pair):
            info = [per[c] for c in cs]
            way = info[0]["way"]
            val = float(np.mean([i["val"] for i in info]))
            n = min(i["n"] for i in info)
            files = set().union(*[i["files"] for i in info])
            _leaf_, cur = _pressure_key(setup, _CK[cs[0]])
            cur_n = ibt.number(cur)
            move = f" ({cur_n:.0f} → {cur_n - 2 if way == 'lower' else cur_n + 2:.0f} kPa)" if cur_n is not None else ""
            where = f"both {axle} tyres" if len(cs) == 2 else f"the {CORNER_WORDS[cs[0]]}"
            if way == "lower":
                why = (f"The middle of {where} ran {val:.0f} °C hotter than the average of its edges: the tyre bulges "
                       f"in the middle, a sign the pressure is high.")
            else:
                why = (f"The middle of {where} ran {val:.0f} °C cooler than the average of its edges: the tyre sags "
                       f"in the middle, a sign the pressure is low.")
            setting = f"{_cap(axle)} starting pressure" if len(cs) == 2 else _cap(f"{CORNER_WORDS[cs[0]]} starting pressure")
            out.append(_change_item("pressure_from_temps", setting, way, why,
                                    f"Try {way} starting pressure by 2 kPa{move}, the smallest useful step.",
                                    f"after a 5-lap run the middle should be within about 2 to 3 °C of the edges' "
                                    f"average. {TEMP_CAVEAT}",
                                    f"pit-stall tyre carcass temperatures, {_files_text(n, files)}", "low-medium",
                                    explain=EXPLAIN["starting (cold) pressure"], corners=list(cs)))
    return out


def _hot_laps(recs: list, runs_only: bool = False) -> list:
    """(file, row) for clean laps 3+ of a stint that have a hot-pressure sample. runs_only: only stints with at least
    HOT_RUN_LAPS counted laps (the runs the tyre pressure table shows), so the two never disagree."""
    ok_stints = None
    if runs_only:
        keep, _d = pace_laps(recs)
        n = {}
        for f, x in keep:
            n[(f, x.get("stint"))] = n.get((f, x.get("stint")), 0) + 1
        ok_stints = {k for k, v in n.items() if v >= HOT_RUN_LAPS}
    out = []
    for r in recs:
        for x in ((r.get("detail") or {}).get("laps") or []):
            if x.get("clean") and x.get("press") and (x.get("pos") or 0) >= HOT_POS:
                if ok_stints is None or (r["file"], x.get("stint")) in ok_stints:
                    out.append((r["file"], x))
    return out


def learned_target(history: list) -> dict | None:
    """Hot pressures from his own fastest stints at this car + track (top quarter of stints with >= 3 pace laps,
    at least 3 such stints). Learned from his laps - not an official target.
    Returns {"values": {corner: kPa}, "stints": n used, "of": n stints} or None."""
    keep, _d = pace_laps(history)
    stints = {}
    for f, x in keep:
        stints.setdefault((f, x["stint"]), []).append(x["time"])
    ok = [(float(np.median(ts)), k) for k, ts in stints.items() if len(ts) >= 3]
    if len(ok) < 3:
        return None
    ok.sort()
    best = {k for _t, k in ok[:max(1, len(ok) // 4)]}
    vals = {c: [] for c in ("LF", "RF", "LR", "RR")}
    for f, x in _hot_laps(history):
        if (f, x["stint"]) in best:
            for c, v in x["press"].items():
                vals[c].append(v)
    if not any(vals.values()):
        return None
    return {"values": {c: round(float(np.median(v)), 1) for c, v in vals.items() if v}, "stints": len(best), "of": len(ok),
            "files": sorted({f for f, _s in best})}


LEARNED_MIN_STINTS = 2         # a "fastest stints" line needs at least this many stints behind it
LEARNED_SAME_KPA = 2.0         # ...and is left out when every tyre is within this of what it runs now


def _learned_usable(lt: dict | None, now: dict, current_files: set) -> bool:
    """Whether the learned hot pressures say anything: from 2+ stints, none of them from the laps that make the
    "now" numbers (that would compare the run with itself), and more than 2 kPa from now on at least one tyre."""
    if not lt or lt["stints"] < LEARNED_MIN_STINTS or set(lt.get("files") or []) & current_files:
        return False
    return any(abs(now[c] - v) > LEARNED_SAME_KPA for c, v in lt["values"].items() if c in now)


def _cold_step(hot_gap: float, cold: float, hot: float) -> int:
    """Cold-pressure change (kPa, at least 1) that moves the hot pressure by about hot_gap. Gauge pressures are
    turned into absolute ones first: hot / cold is a ratio of absolute pressures, not of the gauge readings."""
    return max(1, int(round(hot_gap * (cold + ATM_KPA) / (hot + ATM_KPA))))


def _hot_pressure_items(recs: list, setup: dict, car_path: str, targets: dict | None, history: list) -> list:
    laps = _hot_laps(recs, runs_only=True)
    if not laps:
        return []
    med = {}
    for c in ("LF", "RF", "LR", "RR"):
        v = [x["press"][c] for _f, x in laps if c in x["press"]]
        if v:
            med[c] = round(float(np.median(v)), 1)
    stints = {(f, x["stint"]) for f, x in laps}
    conf = "reading" if len(stints) >= 2 else "guess"
    data = (f"live hot tyre pressures at the fastest point of each lap, median of {len(laps)} clean lap"
            f"{'s' if len(laps) != 1 else ''} (lap {HOT_POS} on) from {len(stints)} stint{'s' if len(stints) != 1 else ''}")
    parts = ", ".join(f"{CORNER_WORDS[c]} {v:.0f}" for c, v in med.items())
    cold = {c: ibt.number(_pressure_key(setup, _CK[c])[1]) for c in med}
    lt = learned_target(history) if history else None
    if not _learned_usable(lt, med, {r["file"] for r in recs}):
        lt = None
    lines = [f"Now: {parts} kPa."]
    if lt:
        n = lt["stints"]
        vals = ", ".join(f"{CORNER_WORDS[c]} {v:.0f}" for c, v in lt["values"].items())
        lines.append(f"In your fastest stint{'s' if n != 1 else ''} here ({n} of {lt['of']}): {vals} kPa. "
                     "Learned from your own laps, not an official target.")
    items = [_info_item("hot_pressure", "Hot tyre pressures", " ".join(lines), "",
                        data + ("; the fastest-stint line is from your own fastest stints at this car and track"
                                if lt else ""), conf, values=med, lines=lines,
                        learned=({"values": lt["values"], "stints": lt["stints"], "of": lt["of"]} if lt else None))]
    # main rule: a tyre far off its axle partner
    for axle, (l, r) in (("front", ("LF", "RF")), ("rear", ("LR", "RR"))):
        if l not in med or r not in med or abs(med[l] - med[r]) <= AXLE_KPA:
            continue
        hi_c, lo_c = (l, r) if med[l] > med[r] else (r, l)
        pick = hi_c
        if lt and hi_c in lt["values"] and lo_c in lt["values"]:
            pick = max((hi_c, lo_c), key=lambda c: abs(med[c] - lt["values"][c]))
        partner = lo_c if pick == hi_c else hi_c
        gap = med[pick] - med[partner]
        c0 = cold.get(pick)
        if c0:
            step = _cold_step(abs(gap), c0, med[pick])
            rise = f"It rises from {c0:.0f} kPa cold to {med[pick]:.0f} hot, so"
            move = f" ({c0:.0f} → {c0 - step if gap > 0 else c0 + step:.0f} kPa)"
        else:
            step, rise, move = max(1, int(round(abs(gap)))), "", ""
        way = "lower" if gap > 0 else "raise"
        why = (f"The {CORNER_WORDS[pick]} ran {abs(gap):.0f} kPa {'higher' if gap > 0 else 'lower'} than the "
               f"{CORNER_WORDS[partner]} when hot ({med[pick]:.0f} vs {med[partner]:.0f} kPa).")
        act = f"{rise} try {step} kPa {'less' if gap > 0 else 'more'} cold pressure on the {CORNER_WORDS[pick]}{move}."
        act = act.strip()
        items.append(_change_item("axle_pressure", _cap(f"{CORNER_WORDS[pick]} starting pressure"), way, why,
                                  _cap(act),
                                  f"after a 5-lap run the two {axle} tyres should be within a few kPa when hot.",
                                  data, conf, corners=[pick]))
    if all(c in med for c in ("LF", "RF", "LR", "RR")):
        fr = (med["LF"] + med["RF"]) / 2 - (med["LR"] + med["RR"]) / 2
        if abs(fr) >= IMBALANCE_KPA:
            items.append(_info_item("pressure_balance", "Front vs rear (hot)",
                                    f"The fronts run {abs(fr):.0f} kPa {'higher' if fr > 0 else 'lower'} than the rears when hot.",
                                    "", data, conf))
    if lt:
        for c, v in med.items():
            t = lt["values"].get(c)
            if t is not None and abs(v - t) > AXLE_KPA and cold.get(c):
                step = _cold_step(abs(v - t), cold[c], v)
                way = "lower" if v > t else "raise"
                items.append(_change_item(
                    "learned_target_gap", _cap(f"{CORNER_WORDS[c]} starting pressure"), way,
                    f"The {CORNER_WORDS[c]} runs {v:.0f} kPa hot; in your fastest stints it ran about {t:.0f}.",
                    f"Try {step} kPa {'less' if v > t else 'more'} cold pressure ({cold[c]:.0f} → "
                    f"{cold[c] - step if v > t else cold[c] + step:.0f} kPa).",
                    "the hot pressure should land near your fastest stints' and the laps should not get slower.",
                    data + "; target learned from your own fastest stints", "guess", corners=[c]))
    tg = (targets or {}).get(car_path) if targets else HOT_TARGETS.get(car_path)
    if tg:
        lo, hi = tg
        for c, v in med.items():
            if (v < lo or v > hi) and cold.get(c):
                gap = lo - v if v < lo else v - hi
                step = _cold_step(gap, cold[c], v)
                way = "raise" if v < lo else "lower"
                items.append(_change_item(
                    "hot_pressure_target", _cap(f"{CORNER_WORDS[c]} starting pressure"), way,
                    f"The {CORNER_WORDS[c]} ran {v:.0f} kPa hot, outside the {lo:.0f} to {hi:.0f} kPa target you gave.",
                    f"Try {step} kPa {'more' if way == 'raise' else 'less'} cold pressure.",
                    "after a 5-lap run its hot pressure should be inside the target.", data, conf, corners=[c]))
    return items


def learned_floor(history: list) -> dict:
    """Each ride-height channel's floor for this car: the 0.5th percentile across his files (weighted median of each
    file's 0.5th percentile by samples). {channel: mm}."""
    per = {}
    for r in history:
        for c, d in ((r.get("detail") or {}).get("ride") or {}).items():
            if d.get("move_mm", 0) >= BOTTOM_MOVE_MM:
                per.setdefault(c, []).append((d["p05_mm"], d["n"]))
    out = {}
    for c, lst in per.items():
        lst.sort()
        tot, acc = sum(n for _v, n in lst), 0
        for v, n in lst:
            acc += n
            if acc >= tot / 2:
                out[c] = v
                break
    return out


def _bottoming_items(recs: list, history: list) -> list:
    words = {"LF": "left front", "RF": "right front", "LR": "left rear", "RR": "right rear",
             "splitter": "splitter (front centre)"}
    floors = learned_floor(history or recs)
    out = []
    for c, floor in floors.items():
        at, n, touch, total, pcts = 0, 0, [], 0, []
        files = set()
        for r in recs:
            d = ((r.get("detail") or {}).get("ride") or {}).get(c)
            if not d or d["move_mm"] < BOTTOM_MOVE_MM:
                continue
            edges = d["lo_mm"] + HIST_MM * (np.arange(len(d["hist"])) + 1)
            at += int(np.sum(np.asarray(d["hist"])[edges <= floor + BOTTOM_MM + 1e-9]))
            n += d["n"]
            for mn, p, lap_no in d["laps"]:
                total += 1
                if mn <= floor + BOTTOM_MM:
                    touch.append((r["file"], lap_no))
                    if p is not None:
                        pcts.append(_pct_bin(p))
            files.add(r["file"])
        if not n or at / n <= BOTTOM_FRAC or len(touch) < max(2, total // 2):
            continue
        b = max(set(pcts), key=pcts.count) if pcts else None
        where = f"around {b * 5} to {b * 5 + 5}% of the lap, " if b is not None else ""
        # does it line up with time lost there? compare touching laps with the others near that spot
        loss = None
        if b is not None:
            bins_of = {}
            for r in recs:
                for x in ((r.get("detail") or {}).get("laps") or []):
                    if x.get("bins"):
                        bins_of[(r["file"], x["lap"])] = sum(x["bins"][max(0, b - 1): b + 2])
            tset = set(touch)
            t_in = [v for k, v in bins_of.items() if k in tset]
            t_out = [v for k, v in bins_of.items() if k not in tset]
            if len(t_in) >= 2 and len(t_out) >= 2:
                loss = float(np.median(t_in) - np.median(t_out))
        end = "front" if c in ("LF", "RF", "splitter") else "rear"
        why = (f"The {words[c]} ride height hit its floor ({floor:.0f} mm, learned from your laps in this car) "
               f"{where}on {len(touch)} of {total} clean laps, and sat flat there for {at / n * 100:.0f}% of the time. "
               f"A flat bottom on the trace usually means the car is touching the ground or its bump stops.")
        data = f"live ride height on clean laps, {len(files)} file{'s' if len(files) != 1 else ''}"
        if loss is not None and loss > BOTTOM_LOSS:
            why += f" Laps that touched were {loss:.2f} s slower through that part of the lap."
            out.append(_change_item("bottoming", f"{_cap(end)} ride height", "raise", why,
                                    f"It may be bottoming: try raising the {end} by the smallest step "
                                    f"(spring perch offset).",
                                    "the flat spot should go away and that part of the lap should not get slower.",
                                    data, "guess", corners=[c]))
        else:
            note = (" It doesn't cost time there that we can see, so there is nothing to change for now."
                    if loss is not None else " We can't tell yet whether it costs time there.")
            out.append(_info_item("bottoming", f"{_cap(end)} ride height", why + note, "", data, "guess", corners=[c]))
    return out


def _fuel_items(recs: list, setup: dict, race_minutes=None, race_laps=None) -> list:
    laps, _d = pace_laps(recs)
    used = [x["fuel"] for _f, x in laps if x.get("fuel")]
    if not used:
        return []
    per = float(np.median(used))
    lap_s = float(np.median([x["time"] for _f, x in laps]))
    files = {f for f, x in laps if x.get("fuel")}
    data = f"fuel level, {len(used)} clean lap{'s' if len(used) != 1 else ''} in {len(files)} file{'s' if len(files) != 1 else ''}"
    why = f"You used about {per:.2f} L a lap on clean laps."
    setup_fuel = None
    for k, v in ibt.flatten(setup).items():
        if _leaf(k) == "FuelLevel":
            setup_fuel = ibt.number(v)
    if race_minutes is None and race_laps is None:
        for r in recs:
            rc = (r.get("detail") or {}).get("race") or {}
            if rc.get("laps"):
                race_laps = rc["laps"]
            elif rc.get("minutes"):
                race_minutes = rc["minutes"]
    conf = "reading" if len(used) >= 3 else "guess"
    act = None
    direction = None
    if race_laps or race_minutes:
        n = int(race_laps) if race_laps else int(np.ceil(race_minutes * 60 / lap_s)) + 1
        need = (n + 1) * per
        length = (f"{int(race_laps)} laps" if race_laps else
                  f"a {race_minutes:.0f}-minute race at about {lap_text(lap_s)} a lap is about {n} laps")
        why += f" {_cap(length)}; that needs about {need:.0f} L with one lap spare."
        if setup_fuel is not None:
            why += f" The setup carries {setup_fuel:.1f} L."
            if setup_fuel < need - 0.5:
                direction = "more"
                act = f"Add about {np.ceil(need - setup_fuel):.0f} L, or plan a stop."
            elif setup_fuel > need + 3 * per:
                direction = "less"
                act = (f"You carry about {setup_fuel - need:.0f} L more than you need: less fuel is less weight, "
                       f"if the series lets you choose.")
    check = "after the race, the fuel left should be about one lap's worth."
    extra = {"per_lap_l": round(per, 3), "setup_l": setup_fuel}
    if act:
        it = _change_item("fuel", "Fuel", direction, why, act, check, data, conf, **extra)
        it["action"] = act                     # fuel is not a setup change to compare
        it["text"] = f"{why} {act} Check: {check}"
        return [it]
    return [_info_item("fuel", "Fuel", why, check, data, conf, **extra)]


# one change at a time: the most reliable signal first
PRIORITY = ("axle_pressure", "hot_pressure_target", "learned_target_gap", "bottoming", "camber",
            "pressure_from_temps", "fuel")


def suggestions(run_or_files, car: str = "", series_setup=None, race_minutes=None, race_laps=None,
                targets: dict | None = None, history: list | None = None) -> dict:
    """Rule-based setup ideas for one setup's files, one change at a time.

    run_or_files: a list of index records (all on one setup), or {"files": [records]}.
    car: car path. series_setup: "open" | "fixed" | None/"unknown". race_minutes / race_laps: the series' race
    length (else taken from a Race session in the files). targets: optional {car_path: (low kPa, high kPa)} hot
    window he trusts (none is shipped). history: every record for this car + track (for the target learned from his
    fastest stints and the car's ride-height floor); defaults to run_or_files.
    Returns {"items": [{"rule", "setting", "direction", "why", "action" (changes), "check", "data", "confidence"
             ("reading" | "guess" | "low-medium" for tyre temperatures), "kind" ("change" | "info" | "note"), "text"}],
             "next": the one change to try first (or None), "notes": [...], "pit_readings": n,
             "readings_left_out": {"cold", "short", "no_stint"}, "series_setup"}"""
    recs = run_or_files.get("files") if isinstance(run_or_files, dict) else run_or_files
    recs = [r for r in (recs or []) if isinstance(r, dict)]
    history = [r for r in (history or recs) if isinstance(r, dict)]
    kind = series_setup_kind(series_setup)
    out = {"items": [], "next": None, "notes": [], "pit_readings": 0, "series_setup": kind,
           "readings_left_out": {"cold": 0, "short": 0, "no_stint": 0}, "cold_readings_ignored": 0}
    if kind == "fixed":
        out["items"].append({"rule": "fixed_series", "kind": "note", "setting": None, "direction": None,
                             "confidence": "reading", "data": "series setting", "check": "",
                             "why": "Fixed setup series.", "text": FIXED_TEXT})
        # the setup (tyre pressures too) is locked, but the fuel he carries is still his
        out["items"] += _fuel_items(recs, recs[-1].get("setup") or {}, race_minutes, race_laps) if recs else []
        return out
    if not recs:
        out["notes"].append("No telemetry files for this setup.")
        return out
    if kind == "unknown":
        out["items"].append({"rule": "unknown_series", "kind": "note", "setting": None, "direction": None,
                             "confidence": "guess", "data": "series setting", "check": "",
                             "why": "We don't know if this series is open or fixed setup.",
                             "text": "We don't know whether this series is open or fixed setup, so these ideas assume "
                                     "you can change the setup. Set it above, under This series is."})
    setup = recs[-1].get("setup") or {}
    readings, skip = _collect_readings(recs)
    out["readings_left_out"] = skip
    out["cold_readings_ignored"] = skip["cold"]
    out["pit_readings"] = len(readings)
    if skip["cold"]:
        out["notes"].append(f"{skip['cold']} tyre reading{'s were' if skip['cold'] != 1 else ' was'} from cold tyres "
                            f"(never pitted) and {'were' if skip['cold'] != 1 else 'was'} ignored.")
    if skip["short"]:
        out["notes"].append(f"{skip['short']} tyre reading(s) came after a stint shorter than {TEMP_STINT_LAPS} laps "
                            "and were ignored.")
    if skip["no_stint"]:
        out["notes"].append(f"{skip['no_stint']} tyre reading(s) were carried over from a stop we can't see "
                            "(another file or the garage), so their stint length is unknown and they were ignored.")
    if not readings:
        out["notes"].append(f"No usable tyre temperature readings yet: drive at least {TEMP_STINT_LAPS} laps, then stop "
                            "in your pit stall (not the garage) so iRacing measures the tyres.")
    out["items"] += _hot_pressure_items(recs, setup, car, targets, history)
    out["items"] += _bottoming_items(recs, history)
    out["items"] += _camber_items(readings, setup)
    out["items"] += _pressure_temp_items(readings, setup)
    out["items"] += _fuel_items(recs, setup, race_minutes, race_laps)
    if any(i["rule"] in ("camber", "pressure_from_temps") for i in out["items"]):
        out["notes"].append(TEMP_CAVEAT)
    changes = [i for i in out["items"] if i["kind"] == "change" and i["rule"] != "fuel"]
    if changes:
        best = min(changes, key=lambda i: (PRIORITY.index(i["rule"]), i["confidence"] != "reading"))
        out["next"] = best
        best["next"] = True
    return out


# ================================================================ file safety

SAFE_MEMBERS = {".sto", ".htm", ".html", ".txt", ".json", ".ibt"}
MAX_STO = 2 * 1024 * 1024                 # a setup is a few KB; anything this big is not a setup
MAX_ZIP = 500 * 1024 * 1024
MAX_MEMBERS = 500
_SCRIPT = re.compile(rb"powershell|cmd\.exe|cmd /c|wscript|cscript|vbscript|createobject|invoke-expression|"
                     rb"frombase64string|rundll32|regsvr32|mshta|<script|javascript:|@echo off|sub autoopen|"
                     rb"document_open|shell\(|start-process", re.I)
# "#!" starts a script only at the very first byte; anywhere else in an encrypted .sto it is just two random bytes
_BAD_EXT = {".exe", ".dll", ".scr", ".com", ".bat", ".cmd", ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse", ".wsf",
            ".wsh", ".hta", ".msi", ".msp", ".lnk", ".jar", ".reg", ".sys", ".cpl", ".pif", ".docm", ".xlsm", ".pptm",
            ".dotm", ".xlam", ".iso", ".img", ".vhd", ".appx", ".msix", ".py", ".sh"}


def _scan_bytes(head: bytes, what: str) -> list:
    probs = []
    if head[:2] == b"MZ":
        probs.append(f"{what} is a Windows program (it starts like an .exe / .dll)")
    if head[:4] == b"\x7fELF":
        probs.append(f"{what} is a program (Linux executable)")
    if head[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        probs.append(f"{what} is an Office document (these can carry macros)")
    if b"This program cannot be run in DOS mode" in head or b"This program must be run under Win32" in head:
        if not probs:
            probs.append(f"{what} has a Windows program hidden inside it")
    if head[:2] == b"#!":
        probs.append(f"{what} is a script (it starts with '#!')")
    m = _SCRIPT.search(head)
    if m:
        probs.append(f"{what} contains script or command text ('{m.group().decode('latin-1', 'replace')}')")
    return probs


def _bad_path(name: str) -> bool:
    n = name.replace("\\", "/")
    return (n.startswith("/") or re.match(r"^[A-Za-z]:", n) is not None or any(p == ".." for p in n.split("/"))
            or "\0" in n)


def check_setup_file(path, name: str | None = None) -> dict:
    """Check a setup file he got from someone else before it goes near iRacing. Reads only; never extracts or runs.

    .sto: must not be a program, a zip, an Office file or script text, and must be a sensible size.
    .zip: lists the members (from the zip's table only) and flags any member that is not .sto/.htm/.txt/.json/.ibt,
    any path that would escape the folder, nested zips, encrypted members, oversize contents, and checks the first
    bytes of each listed member the same way as a .sto.
    name: the file's own name when it was saved under another one (its types are judged from this name).
    Returns {"ok": bool, "problems": [str], "kind": "sto" | "zip" | "other", "size": n, "members": [names]}"""
    p = Path(path)
    shown = Path(str(name).replace("\\", "/").rsplit("/", 1)[-1]) if name else p
    out = {"ok": False, "problems": [], "kind": "other", "size": 0, "members": [], "file": shown.name}
    try:
        st = p.stat()
    except OSError as e:
        out["problems"].append(f"could not read the file ({e.strerror or e})")
        return out
    out["size"] = st.st_size
    suf = [s.lower() for s in shown.suffixes]
    ext = suf[-1] if suf else ""
    if any(s in _BAD_EXT for s in suf[:-1]):
        out["problems"].append("the name hides another file type (e.g. setup.exe.sto)")
    if ext in _BAD_EXT:
        out["problems"].append(f"'{ext}' files are programs or scripts, not setups")
        return out
    try:
        with open(p, "rb") as f:
            head = f.read(64 * 1024)
    except OSError as e:
        out["problems"].append(f"could not read the file ({e.strerror or e})")
        return out
    is_zip = head[:2] == b"PK"
    if ext == ".zip" or is_zip:
        out["kind"] = "zip"
        if ext == ".sto":
            out["problems"].append("this .sto is really a zip archive, not a setup")
        if st.st_size > MAX_ZIP:
            out["problems"].append("the zip is far too big for setups")
        try:
            with zipfile.ZipFile(p) as z:
                infos = z.infolist()
                if len(infos) > MAX_MEMBERS:
                    out["problems"].append(f"the zip holds {len(infos)} files - far more than a setup pack")
                total = 0
                for zi in infos[:MAX_MEMBERS]:
                    name = zi.filename
                    out["members"].append(name)
                    total += zi.file_size
                    if _bad_path(name):
                        out["problems"].append(f"'{name}' would be written outside the folder (path trick)")
                        continue
                    if name.endswith("/"):
                        continue
                    msuf = [s.lower() for s in Path(name).suffixes]
                    mext = msuf[-1] if msuf else ""
                    if mext not in SAFE_MEMBERS:
                        out["problems"].append(f"'{name}' is not a setup file ({mext or 'no extension'})")
                        continue
                    if any(s in _BAD_EXT for s in msuf[:-1]):
                        out["problems"].append(f"'{name}' hides another file type in its name")
                    if zi.flag_bits & 0x1:
                        out["problems"].append(f"'{name}' is password-protected, so it can't be checked")
                        continue
                    if mext == ".sto" and zi.file_size > MAX_STO:
                        out["problems"].append(f"'{name}' is far too big for a setup")
                    try:
                        with z.open(zi) as fh:
                            mhead = fh.read(64 * 1024)
                    except Exception:  # noqa: BLE001
                        out["problems"].append(f"'{name}' could not be read inside the zip")
                        continue
                    if mhead[:2] == b"PK":
                        out["problems"].append(f"'{name}' is another zip inside the zip")
                    probs = _scan_bytes(mhead, f"'{name}'")
                    if mext == ".ibt":                       # telemetry is binary channel data; only program headers matter
                        probs = [x for x in probs if "script" not in x]
                    out["problems"] += probs
                if total > MAX_ZIP:
                    out["problems"].append("the zip unpacks to far more than any setup pack (possible zip bomb)")
        except zipfile.BadZipFile:
            out["problems"].append("the zip is damaged or not really a zip")
        except Exception as e:  # noqa: BLE001
            out["problems"].append(f"the zip could not be read ({str(e)[:80]})")
    elif ext == ".sto":
        out["kind"] = "sto"
        if st.st_size == 0:
            out["problems"].append("the setup file is empty")
        elif st.st_size > MAX_STO:
            out["problems"].append(f"the setup file is {st.st_size // 1024} KB - far too big for a setup")
        out["problems"] += _scan_bytes(head, "the file")
        if st.st_size > len(head):
            try:
                with open(p, "rb") as f:
                    f.seek(len(head))
                    rest = f.read(MAX_STO)
                if b"This program cannot be run in DOS mode" in rest:
                    out["problems"].append("the file has a Windows program hidden inside it")
            except OSError:
                pass
    elif ext in SAFE_MEMBERS:
        out["kind"] = ext.lstrip(".")
        out["problems"] += [x for x in _scan_bytes(head, "the file") if ext != ".ibt" or "script" not in x]
    else:
        out["problems"].append(f"'{ext or 'no extension'}' is not a setup file type (.sto or .zip)")
    out["ok"] = not out["problems"]
    return out


# ================================================================ report

def _bad_for(bad: list, car_path, track_code) -> list:
    """The unreadable files that belong to this car + track. An unreadable file can't say what it holds, but iRacing
    names its files '<carpath>_<track code> <date> <time>.ibt', so the name tells. The rest are counted once, in the
    telemetry panel, not on every car's report."""
    want = f"{car_path}_{track_code}".lower()
    return [b for b in bad if str(b.get("file") or "").lower().startswith(want)]


def report(folder, car_path, track_code, series_setup=None, cache_path=None, race_minutes=None, race_laps=None,
           targets: dict | None = None, idx: dict | None = None, progress=None, stop=None) -> dict:
    """Everything Setup Lab shows for one car + track, JSON-ready.

    Pass `idx` (from index()) to skip the scan. Returns {"car", "car_path", "track", "track_code", "series_setup",
    "files": n, "setups": [{"id", "label", "name", "first", "last", "files"}], "runs": [...],
    "diffs": [{"from", "to", "changes": diff()}], "compare": compare(), "suggestions": suggestions() for the most
    recent setup (with "for_setup"), "notes": [str], "bad_files": [...] (only this car + track's)}"""
    idx = idx if idx is not None else index(folder, cache_path=cache_path, progress=progress, stop=stop)
    recs = [r for r in idx.get("files") or [] if _match(r, car_path, track_code)]
    kind = series_setup_kind(series_setup)
    source = "series setting" if kind != "unknown" else ""
    if kind == "unknown":
        kind, source = series_setup_guess(recs, car_path)
    out = {"car_path": car_path, "track_code": track_code, "car": "", "track": "", "series_setup": kind,
           "series_setup_source": source,
           "files": len(recs), "setups": [], "runs": [], "diffs": [], "compare": None, "suggestions": None,
           "notes": [], "bad_files": _bad_for(idx.get("bad") or [], car_path, track_code)}
    if idx.get("missing_folder"):
        out["notes"].append("The telemetry folder was not found.")
    if idx.get("stopped"):
        out["notes"].append("The scan was stopped early, so some files are missing.")
    if out["bad_files"]:
        out["notes"].append(f"{len(out['bad_files'])} file{'s' if len(out['bad_files']) != 1 else ''} for this car and "
                            "track could not be read (damaged or still being written) and "
                            f"{'were' if len(out['bad_files']) != 1 else 'was'} skipped.")
    if not recs:
        out["notes"].append("No telemetry for this car and track yet.")
        return out
    out["car"] = _names.car((recs[-1].get("car") or {}).get("name")) or car_path
    t = recs[-1].get("track") or {}
    out["track"] = _names.track(t.get("name"), t.get("config")) or track_code
    no_detail = [r["file"] for r in recs if not r.get("detail")]
    if no_detail:
        out["notes"].append(f"{len(no_detail)} file(s) had a setup but their channels could not be read.")
    g = group(recs)
    out["setups"] = [{k: s[k] for k in ("id", "label", "name", "names", "first", "last", "files", "fingerprint")}
                     for s in g["setups"]]
    out["runs"] = g["runs"]
    real = [s for s in g["setups"] if s["fingerprint"]]
    if len(real) < len(g["setups"]):
        out["notes"].append("Some files had no setup recorded and are left out of the comparison.")
    by_first = {s["id"]: s for s in real}
    order = [run["setup"] for run in g["runs"] if run["setup"] in by_first]
    seen_pairs = set()
    for a, b in zip(order, order[1:]):
        if a != b and (a, b) not in seen_pairs:
            seen_pairs.add((a, b))
            out["diffs"].append({"from": a, "to": b, "changes": diff(by_first[a]["setup"], by_first[b]["setup"])})
    out["compare"] = compare(recs, setups=g["setups"])
    if real:
        latest = order[-1] if order else real[-1]["id"]
        mine = [r for r in recs if r.get("setup_id") == latest]
        sug = suggestions(mine, car=car_path, series_setup=kind, race_minutes=race_minutes, race_laps=race_laps,
                          targets=targets, history=recs)
        sug["for_setup"] = latest
        out["suggestions"] = sug
        out["notes"] += sug.get("notes") or []
    out["notes"].append("Setups are read from the telemetry: iRacing's .sto setup files are encrypted and can't be read.")
    return out
