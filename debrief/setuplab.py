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

CACHE_VERSION = 1

# Leaf keys that are readings (they change without the driver touching the setup) - never part of a setup's identity.
READING_KEYS = {"LastHotPressure", "LastTempsOMI", "LastTempsIMO", "TreadRemaining", "UpdateCount"}
FUEL_KEYS = {"FuelLevel"}
# Garage results that follow from the settings and the fuel load: shown in a diff, never part of the identity.
RESULT_KEYS = {"CornerWeight", "RideHeight", "NoseWeight"}

CORNER_KEYS = {"LeftFront": "LF", "RightFront": "RF", "LeftRear": "LR", "RightRear": "RR"}
CORNER_WORDS = {"LF": "left front", "RF": "right front", "LR": "left rear", "RR": "right rear"}
PAIR = {"LeftFront": ("RightFront", "Front"), "RightFront": ("LeftFront", "Front"),
        "LeftRear": ("RightRear", "Rear"), "RightRear": ("LeftRear", "Rear")}

# Evidence gate (same spirit as the rest of Debrief: say "not enough to tell" rather than guess)
MIN_STINTS = 2
MIN_STINT_LAPS = 3
MIN_LAPS = 6
TRAFFIC = 1.07                 # laps slower than 107% of the setup's median are traffic, not pace
MAX_TEMP_GAP = 6.0             # deg C of track surface temperature
MIN_DIFF = 0.15                # s a lap: smaller than this is never called a difference

# Tyre temperature targets for GT cars (carcass temps at a pit stop): inner edge 5-8 deg C hotter than the outer.
CAMBER_TARGET = (5.0, 8.0)
CAMBER_HIGH = CAMBER_TARGET[1] + 2.0      # more than this: too much negative camber
CAMBER_LOW = 0.0                          # outer hotter than inner: not enough negative camber
CROWN = 4.0                               # middle vs edges, deg C: pressure too high / too low
IMBALANCE_KPA = 5.0

# Hot-pressure target windows per car path, kPa. Deliberately empty: we have no target we can stand behind, so the
# hot pressures are reported with their front/rear and left/right balance instead. report()/suggestions() take a
# `targets` argument so a window he trusts (from his series or team) can be used.
HOT_TARGETS: dict = {}

BOTTOM_MM = 1.0                # samples within 1 mm of the lowest value count as "at the floor"
BOTTOM_FRAC = 0.02             # more than 2% of clean-lap samples at the floor
BOTTOM_MOVE_MM = 5.0           # the channel must move at least this much, or it is not a real ride-height trace

_NAME_TIME = re.compile(r"(\d{4})-(\d{2})-(\d{2}) (\d{2})-(\d{2})-(\d{2})\.ibt$", re.I)


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
        return {"ok": False, "error": str(e)[:200]}


def _detail_of(ib) -> dict:
    di = ib.session.get("DriverInfo") or {}
    rate = ib.tick_rate or 60
    out = {"ok": True, "duration_s": round(ib.record_count / rate, 1),
           "setup_name": str(di.get("DriverSetupName") or ib.player().get("DriverSetupName") or ""),
           "setup_modified": di.get("DriverSetupIsModified"), "notes": []}
    if out["setup_name"] in ("None", "null"):
        out["setup_name"] = ""
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
    have = {c: ib.has(f"{c}pressure") for c in ("LF", "RF", "LR", "RR")}
    press = {c: ib.col(f"{c}pressure") for c in have if have[c]}
    fuel = ib.col("FuelLevel") if ib.has("FuelLevel") else None
    pct = ib.col("LapDistPct") if ib.has("LapDistPct") else None
    rows, stint, prev = [], 0, None
    clean_idx = []
    for x in laps:
        if prev is not None and (x["lap"] != prev["lap"] + 1 or x["session_num"] != prev["session_num"] or prev["pit"]):
            stint += 1
        a, b = x["start"], x["end"]
        row = {"lap": x["lap"], "time": round(x["time"], 3) if x["time"] else None, "session": x["session"],
               "clean": x["clean"], "start_lap": x["start_lap"], "pit": x["pit"], "stint": stint}
        if x["clean"]:
            clean_idx.append((a, b))
            if press:
                row["press"] = {c: round(float(np.median(v[a:b])), 1) for c, v in press.items()}
            if fuel is not None and b - a > 1:
                used = float(fuel[a] - fuel[b - 1])
                if 0 < used < 20:
                    row["fuel"] = round(used, 3)
        rows.append(row)
        prev = x
    out["laps"] = rows

    # pit readings: the carcass temps at the first sample and wherever they change (only at a pit-stall stop)
    readings = []
    if all(ib.has(f"{c}temp{z}") for c in ("LF", "RF", "LR", "RR") for z in ("CL", "CM", "CR")):
        vals = {c: [ib.col(f"{c}temp{z}") for z in ("CL", "CM", "CR")] for c in ("LF", "RF", "LR", "RR")}
        stacked = np.stack([v for c in vals for v in vals[c]], axis=1)
        idx = [0] + [int(i) + 1 for i in np.nonzero(np.any(np.abs(np.diff(stacked, axis=0)) > 0.05, axis=1))[0]]
        st = ib.col("SessionTime") if ib.has("SessionTime") else None
        for i in idx:
            r = _reading_from_channels(vals, i)
            readings.append({"t": round(float(st[i]), 1) if st is not None else round(i / rate, 1),
                             "at_start": i == 0, "source": "channels", "tyres": r,
                             "cold": all(_cold(v) for v in r.values())})
    out["readings"] = readings

    # ride heights on clean laps: is there a hard floor the car keeps hitting?
    ride = {}
    if clean_idx:
        sel = np.concatenate([np.arange(a, b) for a, b in clean_idx])
        lap_of = np.concatenate([np.full(b - a, k) for k, (a, b) in enumerate(clean_idx)])
        for ch, label in (("LFrideHeight", "LF"), ("RFrideHeight", "RF"), ("LRrideHeight", "LR"),
                          ("RRrideHeight", "RR"), ("CFSRrideHeight", "splitter")):
            if not ib.has(ch):
                continue
            v = ib.col(ch)[sel].astype(float)
            ok = np.isfinite(v)
            if ok.sum() < 10:
                continue
            v2, lo2 = v[ok], lap_of[ok]
            floor = float(v2.min())
            at = v2 <= floor + BOTTOM_MM / 1000.0
            where = None
            if pct is not None and at.any():
                bins = np.bincount([_pct_bin(float(p)) for p in pct[sel][ok][at]], minlength=20)
                where = int(np.argmax(bins))
            ride[label] = {"floor_mm": round(floor * 1000, 1), "median_mm": round(float(np.median(v2)) * 1000, 1),
                           "move_mm": round(float(np.percentile(v2, 99) - np.percentile(v2, 1)) * 1000, 1),
                           "at_floor": round(float(at.mean()), 4), "laps_at_floor": int(len(set(lo2[at].tolist()))),
                           "laps": len(clean_idx), "where_bin": where}
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
                rec = {"file": p.name, "ok": False, "error": str(e)[:200]}
            rec["size"] = st.st_size
            rec["mtime"] = st.st_mtime_ns
            rec["when"], rec["sort"] = _when(p.name, rec.get("start"))
            out["read"] += 1
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
        e = seen.setdefault(key, {"car_path": c.get("path"), "car": c.get("name"), "track_code": t.get("code"),
                                  "track": " - ".join(x for x in (t.get("name"), t.get("config")) if x),
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
        nm = ((r.get("detail") or {}).get("setup_name") or r.get("setup_name") or "").strip()
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
        if s["name"] and s["fingerprint"]:
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


def _setup_pace(recs: list) -> dict:
    """Clean laps of one setup after the traffic filter, with stints and conditions."""
    laps = []
    for r in recs:
        for x in ((r.get("detail") or {}).get("laps") or []):
            if x.get("clean") and not x.get("start_lap") and x.get("time"):
                laps.append((r["file"], x["stint"], float(x["time"]), x.get("session") or ""))
    out = {"files": [r["file"] for r in recs], "clean_laps": 0, "median": None, "best": None, "mad": None,
           "stints": [], "traffic_laps": 0, "surface_temp": None, "track_state": "dry", "sessions": [], "times": []}
    temps = [(r.get("detail") or {}).get("surface_temp") for r in recs if (r.get("detail") or {}).get("laps")]
    temps = [t for t in temps if t is not None]
    out["surface_temp"] = round(float(np.median(temps)), 1) if temps else None
    states = {(r.get("detail") or {}).get("track_state") or "dry" for r in recs}
    out["track_state"] = "wet" if "wet" in states else ("damp" if "damp" in states else "dry")
    if not laps:
        return out
    med0 = float(np.median([t for *_x, t, _s in laps]))
    keep = [x for x in laps if x[2] <= TRAFFIC * med0]
    out["traffic_laps"] = len(laps) - len(keep)
    times = [x[2] for x in keep]
    out["times"] = [round(t, 3) for t in times]
    out["clean_laps"] = len(times)
    out["median"] = round(float(np.median(times)), 3)
    out["best"] = round(min(times), 3)
    out["mad"] = round(_mad(times), 3)
    stints = {}
    for f, s, _t, _ss in keep:
        stints[(f, s)] = stints.get((f, s), 0) + 1
    out["stints"] = [{"file": f, "stint": s, "clean_laps": n} for (f, s), n in stints.items()]
    out["sessions"] = sorted({x[3] for x in keep if x[3]})
    return out


def _gate(p: dict, label_: str) -> list:
    """What is missing before this setup's pace can be trusted (empty list = enough)."""
    good = [s for s in p["stints"] if s["clean_laps"] >= MIN_STINT_LAPS]
    miss = []
    if len(good) < MIN_STINTS:
        need = MIN_STINTS - len(good)
        miss.append(f"{label_} needs {need} more run{'s' if need > 1 else ''} of at least {MIN_STINT_LAPS} clean laps "
                    f"(has {len(good)})")
    if p["clean_laps"] < MIN_LAPS:
        miss.append(f"{label_} needs {MIN_LAPS - p['clean_laps']} more clean lap{'s' if MIN_LAPS - p['clean_laps'] > 1 else ''} "
                    f"(has {p['clean_laps']})")
    return miss


def _fmt_s(x: float) -> str:
    return f"{x:.1f}" if x >= 0.095 else f"{x:.2f}"


def _pair(a: dict, b: dict) -> dict:
    pa, pb = a["pace"], b["pace"]
    out = {"a": a["id"], "b": b["id"], "verdict": "not enough to tell", "faster": None, "diff_s": None,
           "threshold_s": None, "missing": [], "comparable": True, "text": ""}
    out["missing"] = a["missing"] + b["missing"]
    ta, tb = pa["surface_temp"], pb["surface_temp"]
    if ta is not None and tb is not None and abs(ta - tb) > MAX_TEMP_GAP:
        out["comparable"] = False
        warm = a if ta > tb else b
        out["missing"].append(f"the track was {abs(ta - tb):.0f} °C warmer for {warm['label']} ({max(ta, tb):.0f} °C "
                              f"vs {min(ta, tb):.0f} °C) - drive both on a similar day")
    for s in (a, b):
        if s["pace"]["track_state"] != "dry":
            out["comparable"] = False
            out["missing"].append(f"{s['label']} was driven on a {s['pace']['track_state']} track - compare dry laps only")
    if pa["median"] is not None and pb["median"] is not None:
        out["diff_s"] = round(pa["median"] - pb["median"], 3)        # > 0: b is faster
    if out["missing"]:
        out["text"] = f"{a['label']} vs {b['label']}: not enough to tell yet - " + "; ".join(out["missing"]) + "."
        return out
    resid = [t - pa["median"] for t in pa["times"]] + [t - pb["median"] for t in pb["times"]]
    pooled = float(np.median(np.abs(resid))) if resid else 0.0
    thr = max(MIN_DIFF, pooled)
    out["threshold_s"] = round(thr, 3)
    d = out["diff_s"]
    if abs(d) > thr:
        fast, slow = (b, a) if d > 0 else (a, b)
        out["verdict"] = "faster"
        out["faster"] = fast["id"]
        out["text"] = (f"{fast['label']} was faster than {slow['label']} by about {_fmt_s(abs(d))} s a lap "
                       f"(typical clean lap {fast['pace']['median']:.2f} vs {slow['pace']['median']:.2f} s, "
                       f"{fast['pace']['clean_laps']} and {slow['pace']['clean_laps']} clean laps).")
    else:
        out["verdict"] = "no clear difference"
        out["text"] = (f"No clear difference between {a['label']} and {b['label']}: typical laps "
                       f"{pa['median']:.2f} vs {pb['median']:.2f} s, a gap of {abs(d):.2f} s, which is inside "
                       f"the normal lap-to-lap spread ({thr:.2f} s).")
    return out


def compare(runs, setups: list | None = None) -> dict:
    """Pace per setup for one car + track, with the evidence gate.

    runs: the index records for one car + track (each with "setup_id" from group(), or group() is run here), or
    group()'s dict together with `setups` - simplest is compare(records).
    Returns {"setups": [{"id", "label", "pace": {...}, "enough": bool, "missing": [...]}],
             "pairs": [{"a", "b", "verdict" ("faster" | "no clear difference" | "not enough to tell"), "faster",
                        "diff_s" (a median - b median), "threshold_s", "comparable", "missing", "text"}],
             "verdict": one plain sentence}. Pairs are consecutive setups (A vs B, B vs C)."""
    recs = list(runs)
    g = {"setups": setups} if setups is not None else group(recs)
    rows = []
    for s in g["setups"]:
        if not s["fingerprint"]:
            continue
        mine = [r for r in recs if r.get("setup_id") == s["id"]]
        p = _setup_pace(mine)
        miss = _gate(p, s["label"])
        rows.append({"id": s["id"], "label": s["label"], "pace": p, "enough": not miss, "missing": miss})
    pairs = [_pair(rows[i], rows[i + 1]) for i in range(len(rows) - 1)]
    if not rows:
        verdict = "No laps with a setup recorded for this car and track yet."
    elif len(rows) == 1:
        r = rows[0]
        verdict = (f"Only one setup so far ({r['label']}). Try a change and drive it to compare."
                   + ("" if r["enough"] else " " + "; ".join(r["missing"]) + "."))
    else:
        decided = [p for p in pairs if p["verdict"] != "not enough to tell"]
        verdict = " ".join(p["text"] for p in pairs) if pairs else ""
        if not decided:
            verdict = "Not enough to tell which setup is faster yet. " + " ".join(p["text"] for p in pairs)
    return {"setups": rows, "pairs": pairs, "verdict": verdict.strip()}


# ================================================================ suggestions

def series_setup_kind(value, override=None) -> str:
    """'open' | 'fixed' | 'unknown' from the schedule's value (which may be None) and his own override."""
    for v in (override, value):
        if isinstance(v, str) and v.lower() in ("open", "fixed"):
            return v.lower()
    return "unknown"


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


def _collect_readings(recs: list) -> tuple[list, int]:
    """Hot pit readings across files, de-duplicated by value (a file's first sample repeats the last stop)."""
    seen, out, cold = set(), [], 0
    for r in recs:
        det = r.get("detail") or {}
        rs = list(det.get("readings") or [])
        st = _reading_from_setup(r.get("setup") or {})
        if len(st) == 4:
            rs.append({"t": None, "at_start": False, "source": "setup", "tyres": st,
                       "cold": all(_cold(v) for v in st.values())})
        for x in rs:
            tyres = {c: tuple(round(float(v), 1) for v in t3) for c, t3 in x["tyres"].items()}
            key = tuple(sorted((c, tuple(round(v) for v in t3)) for c, t3 in tyres.items()))
            if key in seen:
                continue
            seen.add(key)
            if x.get("cold") or all(_cold(v) for v in tyres.values()):
                cold += 1
                continue
            out.append({"file": r["file"], "source": x.get("source"), "tyres": tyres})
    return out, cold


def _files_text(n_read: int, files: set) -> str:
    return f"{n_read} pit reading{'s' if n_read != 1 else ''} from {len(files)} file{'s' if len(files) != 1 else ''}"


def _camber_items(readings: list, setup: dict) -> list:
    if not readings:
        return []
    per = {}
    for c in ("LF", "RF", "LR", "RR"):
        vals = [x["tyres"][c] for x in readings if c in x["tyres"] and not _cold(x["tyres"][c])]
        if not vals:
            continue
        spreads = [round(i - o, 1) for o, _m, i in vals]
        files = {x["file"] for x in readings if c in x["tyres"]}
        med = float(np.median(spreads))
        if med > CAMBER_HIGH:
            way = "less"
        elif med < CAMBER_LOW:
            way = "more"
        else:
            way = None
        agree = sum(1 for s in spreads if (s > CAMBER_HIGH if way == "less" else s < CAMBER_LOW)) if way else 0
        per[c] = {"spreads": spreads, "median": med, "way": way, "agree": agree, "n": len(spreads), "files": files}
    out = []
    used = set()
    for axle, pair, ckeys in (("front", ("LF", "RF"), ("LeftFront", "RightFront")),
                              ("rear", ("LR", "RR"), ("LeftRear", "RightRear"))):
        a, b = per.get(pair[0]), per.get(pair[1])
        groups = []
        if a and b and a["way"] and a["way"] == b["way"]:
            groups.append(((pair[0], pair[1]), (ckeys[0], ckeys[1])))
        else:
            for c, ck in zip(pair, ckeys):
                if per.get(c) and per[c]["way"]:
                    groups.append(((c,), (ck,)))
        for cs, cks in groups:
            info = [per[c] for c in cs]
            way = info[0]["way"]
            med = float(np.mean([i["median"] for i in info]))
            n = min(i["n"] for i in info)
            agree = min(i["agree"] for i in info)
            files = set().union(*[i["files"] for i in info])
            conf = "reading" if n >= 2 and agree >= 2 else "guess"
            cur = _setting_value(setup, cks[0], "Camber")
            cur_n = ibt.number(cur)
            target_mid = sum(CAMBER_TARGET) / 2
            excess = abs(med - target_mid)
            step = min(0.5, max(0.1, round(excess / 3.0 * 10) / 10))
            lo, hi = max(0.1, step - 0.1), step
            if cur_n is not None:
                new = cur_n + hi if way == "less" else cur_n - hi
                if way == "less" and new > 0:
                    new = 0.0
                move = f" ({cur_n:.1f} → about {new:.1f})"
            else:
                move = ""
            where = (f"both {axle} tyres" if len(cs) == 2 else f"the {CORNER_WORDS[cs[0]]}")
            per_side = (f" (left {per[cs[0]]['median']:.0f}, right {per[cs[1]]['median']:.0f})" if len(cs) == 2 else "")
            if way == "less":
                why = (f"Inside edge of {where} ran about {med:.0f} °C hotter than the outside{per_side} over "
                       f"{_files_text(n, files)}. For a GT car the inside is usually 5–8 °C hotter.")
                act = f"Try {lo:.1f}–{hi:.1f}° less negative {axle} camber{move}."
                check = f"Check: after a 5-lap run, the inside should be about {target_mid:.0f} °C hotter than the outside, not {med:.0f}."
            else:
                why = (f"Outside edge of {where} ran {abs(med):.0f} °C hotter than the inside{per_side} over "
                       f"{_files_text(n, files)}. For a GT car the inside is usually 5–8 °C hotter, so the tyre "
                       f"is not leaning into the corners enough.")
                act = f"Try {lo:.1f}–{hi:.1f}° more negative {axle} camber{move}."
                check = "Check: after a 5-lap run, the inside should read a few degrees hotter than the outside."
            item = {"rule": "camber", "setting": f"{_cap(axle)} camber" if len(cs) == 2 else _cap(f"{CORNER_WORDS[cs[0]]} camber"),
                    "direction": f"{way} negative", "why": why, "confidence": conf, "check": check,
                    "data": f"pit-stall tyre carcass temperatures, {_files_text(n, files)}",
                    "kind": "change", "explain": EXPLAIN["camber"], "corners": list(cs)}
            item["text"] = f"{why} {act} {check}"
            item["action"] = act
            out.append(item)
            used.update(cs)
    return out


def _pressure_temp_items(readings: list, setup: dict) -> list:
    per = {}
    for c in ("LF", "RF", "LR", "RR"):
        vals = [x["tyres"][c] for x in readings if c in x["tyres"] and not _cold(x["tyres"][c])]
        if not vals:
            continue
        crowns = [round(m - (o + i) / 2, 1) for o, m, i in vals]
        highs = [round(m - max(o, i), 1) for o, m, i in vals]
        med_hi = float(np.median(highs))
        med_crown = float(np.median(crowns))
        if med_hi > CROWN:
            way, val, agree = "lower", med_hi, sum(1 for h in highs if h > CROWN)
        elif med_crown < -CROWN:
            way, val, agree = "higher", -med_crown, sum(1 for h in crowns if h < -CROWN)
        else:
            continue
        files = {x["file"] for x in readings if c in x["tyres"]}
        per[c] = {"way": way, "val": val, "agree": agree, "n": len(vals), "files": files}
    out = []
    ck = {"LF": "LeftFront", "RF": "RightFront", "LR": "LeftRear", "RR": "RightRear"}
    for axle, pair in (("front", ("LF", "RF")), ("rear", ("LR", "RR"))):
        a, b = per.get(pair[0]), per.get(pair[1])
        groups = [pair] if a and b and a["way"] == b["way"] else [(c,) for c in pair if per.get(c)]
        for cs in groups:
            info = [per[c] for c in cs]
            way = info[0]["way"]
            val = float(np.mean([i["val"] for i in info]))
            n = min(i["n"] for i in info)
            agree = min(i["agree"] for i in info)
            files = set().union(*[i["files"] for i in info])
            conf = "reading" if n >= 2 and agree >= 2 else "guess"
            leaf, cur = _pressure_key(setup, ck[cs[0]])
            cur_n = ibt.number(cur)
            move = ""
            if cur_n is not None:
                move = f" ({cur_n:.0f} → about {cur_n - 4 if way == 'lower' else cur_n + 4:.0f} kPa)"
            where = f"both {axle} tyres" if len(cs) == 2 else f"the {CORNER_WORDS[cs[0]]}"
            if way == "lower":
                why = (f"The middle of {where} ran {val:.0f} °C hotter than both edges over {_files_text(n, files)}: "
                       f"the tyre is bulging in the middle, a sign the pressure is too high.")
                check = "Check: after a 5-lap run the middle should be within about 2–3 °C of the edges."
            else:
                why = (f"The middle of {where} ran {val:.0f} °C cooler than the edges over {_files_text(n, files)}: "
                       f"the tyre is sagging in the middle, a sign the pressure is too low.")
                check = "Check: after a 5-lap run the middle should be within about 2–3 °C of the edges."
            act = f"Try {way} starting pressure by 3–5 kPa{move}."
            item = {"rule": "pressure_from_temps", "setting": (f"{_cap(axle)} starting pressure" if len(cs) == 2
                                                               else _cap(f"{CORNER_WORDS[cs[0]]} starting pressure")),
                    "direction": way, "why": why, "confidence": conf, "check": check, "action": act,
                    "data": f"pit-stall tyre carcass temperatures, {_files_text(n, files)}", "kind": "change",
                    "explain": EXPLAIN["starting (cold) pressure"], "corners": list(cs)}
            item["text"] = f"{why} {act} {check}"
            out.append(item)
    return out


def _clean_laps_of(recs: list) -> list:
    out = []
    for r in recs:
        for x in ((r.get("detail") or {}).get("laps") or []):
            if x.get("clean") and not x.get("start_lap") and x.get("time"):
                out.append((r["file"], x))
    if not out:
        return out
    med = float(np.median([x["time"] for _f, x in out]))
    return [(f, x) for f, x in out if x["time"] <= TRAFFIC * med]


def _hot_pressure_items(recs: list, car_path: str, targets: dict | None) -> list:
    laps = _clean_laps_of(recs)
    vals = {c: [x["press"][c] for _f, x in laps if x.get("press") and c in x["press"]] for c in ("LF", "RF", "LR", "RR")}
    if not any(vals.values()):
        return []
    files = {f for f, x in laps if x.get("press")}
    med = {c: round(float(np.median(v)), 1) for c, v in vals.items() if v}
    n = max(len(v) for v in vals.values())
    data = f"live hot tyre pressures, median over {n} clean lap{'s' if n != 1 else ''} in {len(files)} file{'s' if len(files) != 1 else ''}"
    parts = ", ".join(f"{CORNER_WORDS[c]} {v:.0f}" for c, v in med.items())
    items = [{"rule": "hot_pressure", "setting": "Tyre pressures (hot)", "direction": None, "kind": "info",
              "why": f"Hot pressures on clean laps: {parts} kPa.", "confidence": "reading", "data": data,
              "check": "Check: compare these with the hot pressure your series or team aims for.",
              "values": med}]
    items[0]["text"] = items[0]["why"] + " " + items[0]["check"]
    if all(c in med for c in ("LF", "RF", "LR", "RR")):
        fr = (med["LF"] + med["RF"]) / 2 - (med["LR"] + med["RR"]) / 2
        lr_f = med["LF"] - med["RF"]
        lr_r = med["LR"] - med["RR"]
        msgs = []
        if abs(fr) >= IMBALANCE_KPA:
            msgs.append(f"the fronts run {abs(fr):.0f} kPa {'higher' if fr > 0 else 'lower'} than the rears")
        for side_d, axle in ((lr_f, "front"), (lr_r, "rear")):
            if abs(side_d) >= IMBALANCE_KPA:
                msgs.append(f"the {'left' if side_d > 0 else 'right'} {axle} runs {abs(side_d):.0f} kPa higher than the "
                            f"{'right' if side_d > 0 else 'left'} {axle}")
        if msgs:
            why = "On clean laps " + " and ".join(msgs) + "."
            it = {"rule": "pressure_balance", "setting": "Tyre pressures (hot)", "direction": None, "kind": "info",
                  "why": why, "confidence": "reading" if len(files) >= 2 else "guess", "data": data,
                  "check": "Check: if you want them even when hot, start the hotter tyres a few kPa lower and look "
                           "again after a 5-lap run."}
            it["text"] = f"{why} {it['check']}"
            items.append(it)
    tg = (targets or {}).get(car_path) if targets else HOT_TARGETS.get(car_path)
    if tg:
        lo, hi = tg
        for c, v in med.items():
            if v < lo or v > hi:
                way = "raise" if v < lo else "lower"
                gap = lo - v if v < lo else v - hi
                why = (f"The {CORNER_WORDS[c]} ran {v:.0f} kPa hot, outside the {lo:.0f}–{hi:.0f} kPa target you gave.")
                it = {"rule": "hot_pressure_target", "setting": _cap(f"{CORNER_WORDS[c]} starting pressure"),
                      "direction": way, "kind": "change", "why": why,
                      "confidence": "reading" if len(files) >= 2 else "guess", "data": data,
                      "action": f"Try to {way} its starting pressure by about {gap:.0f} kPa.",
                      "check": "Check: after a 5-lap run its hot pressure should be inside the target."}
                it["text"] = f"{why} {it['action']} {it['check']}"
                items.append(it)
    return items


def _bottoming_items(recs: list) -> list:
    words = {"LF": "left front", "RF": "right front", "LR": "left rear", "RR": "right rear", "splitter": "splitter (front centre)"}
    hits = {}
    for r in recs:
        for c, d in ((r.get("detail") or {}).get("ride") or {}).items():
            if d["move_mm"] < BOTTOM_MOVE_MM:
                continue                      # a flat trace is not a ride-height reading
            if d["at_floor"] > BOTTOM_FRAC and d["laps_at_floor"] >= max(2, d["laps"] // 2):
                hits.setdefault(c, []).append((r["file"], d))
    out = []
    for c, lst in hits.items():
        frac = float(np.median([d["at_floor"] for _f, d in lst]))
        bins = [d["where_bin"] for _f, d in lst if d["where_bin"] is not None]
        where = ""
        if bins:
            b = max(set(bins), key=bins.count)
            where = f", mostly around {b * 5}–{b * 5 + 5}% of the lap"
        floor = lst[0][1]["floor_mm"]
        end = "front" if c in ("LF", "RF", "splitter") else "rear"
        why = (f"The {words[c]} ride height sat flat at its lowest point ({floor:.0f} mm) for {frac * 100:.0f}% of "
               f"clean-lap time{where}, in {len(lst)} file{'s' if len(lst) != 1 else ''}. A flat bottom on the trace "
               f"usually means the car is hitting the ground or its bump stops.")
        it = {"rule": "bottoming", "setting": f"{_cap(end)} ride height", "direction": "raise", "kind": "change",
              "why": why, "confidence": "guess", "data": f"live ride height on clean laps, {len(lst)} file(s)",
              "action": f"It may be bottoming: try raising the {end} a little (spring perch offset) or a firmer {end} spring.",
              "check": "Check: the flat spot at the bottom of the ride-height trace should go away, without losing time there."}
        it["text"] = f"{why} {it['action']} {it['check']}"
        out.append(it)
    return out


def _fuel_items(recs: list, setup: dict, race_minutes=None, race_laps=None) -> list:
    laps = _clean_laps_of(recs)
    used = [x["fuel"] for _f, x in laps if x.get("fuel")]
    if not used:
        return []
    per = float(np.median(used))
    times = [x["time"] for _f, x in laps]
    lap_s = float(np.median(times)) if times else None
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
    need_txt, act = "", None
    direction = None
    if race_laps or (race_minutes and lap_s):
        n = int(race_laps) if race_laps else int(np.ceil(race_minutes * 60 / lap_s)) + 1
        need = (n + 1) * per
        length = f"{int(race_laps)} laps" if race_laps else f"a {race_minutes:.0f}-minute race at about {lap_s:.1f} s a lap is about {n} laps"
        need_txt = f" {_cap(length)}; that needs about {need:.0f} L with one lap spare."
        if setup_fuel is not None:
            need_txt += f" The setup carries {setup_fuel:.1f} L."
            if setup_fuel < need - 0.5:
                direction = "more"
                act = f"Add about {np.ceil(need - setup_fuel):.0f} L, or plan a stop."
            elif setup_fuel > need + 3 * per:
                direction = "less"
                act = (f"You carry about {setup_fuel - need:.0f} L more than you need: less fuel is less weight, "
                       f"if the series lets you choose.")
    it = {"rule": "fuel", "setting": "Fuel", "direction": direction, "kind": "change" if act else "info",
          "why": why + need_txt, "confidence": "reading" if len(used) >= 3 else "guess", "data": data,
          "check": "Check: after the race, the fuel left should be about one lap's worth.",
          "per_lap_l": round(per, 3), "setup_l": setup_fuel}
    if act:
        it["action"] = act
    it["text"] = " ".join(x for x in (it["why"], act, it["check"]) if x)
    return [it]


def suggestions(run_or_files, car: str = "", series_setup=None, race_minutes=None, race_laps=None,
                targets: dict | None = None) -> dict:
    """Rule-based setup ideas for one setup's files.

    run_or_files: a list of index records (all on one setup), or {"files": [records]}.
    car: car path (for a hot-pressure target, if one is known). series_setup: "open" | "fixed" | None/"unknown".
    race_minutes / race_laps: the series' race length (else taken from a Race session in the files).
    targets: optional {car_path: (low kPa, high kPa)} hot pressure window he trusts.
    Returns {"items": [{"rule", "setting", "direction", "why", "confidence" ("reading" | "guess"), "check", "data",
             "kind" ("change" | "info" | "note"), "text", optional "action", "explain"}], "notes": [...],
             "cold_readings_ignored": n, "pit_readings": n}"""
    recs = run_or_files.get("files") if isinstance(run_or_files, dict) else run_or_files
    recs = [r for r in (recs or []) if isinstance(r, dict)]
    kind = series_setup_kind(series_setup)
    out = {"items": [], "notes": [], "cold_readings_ignored": 0, "pit_readings": 0, "series_setup": kind}
    if kind == "fixed":
        out["items"].append({"rule": "fixed_series", "kind": "note", "setting": None, "direction": None,
                             "confidence": "reading", "data": "series setting", "check": "",
                             "why": "Fixed setup series.",
                             "text": "Fixed setup series: the setup is locked, so there are no setup changes to try. "
                                     "Only what the series lets you change is yours (usually tyre pressures where "
                                     "allowed, the in-car dials such as brake bias, ABS and traction control, and fuel)."})
        return out
    if not recs:
        out["notes"].append("No telemetry files for this setup.")
        return out
    if kind == "unknown":
        out["items"].append({"rule": "unknown_series", "kind": "note", "setting": None, "direction": None,
                             "confidence": "guess", "data": "series setting", "check": "",
                             "why": "We don't know if this series is open or fixed setup.",
                             "text": "We don't know whether this series is open or fixed setup, so these ideas assume "
                                     "you can change the setup. You can set it in Setup Lab."})
    setup = recs[-1].get("setup") or {}
    readings, cold = _collect_readings(recs)
    out["cold_readings_ignored"] = cold
    out["pit_readings"] = len(readings)
    if cold:
        out["notes"].append(f"{cold} tyre reading{'s were' if cold != 1 else ' was'} from cold tyres (never pitted) "
                            f"and {'were' if cold != 1 else 'was'} ignored.")
    if not readings:
        out["notes"].append("No hot pit-stall tyre readings yet: drive a few laps and stop in your pit stall "
                            "(not the garage) so iRacing measures the tyres.")
    out["items"] += _camber_items(readings, setup)
    out["items"] += _pressure_temp_items(readings, setup)
    out["items"] += _hot_pressure_items(recs, car, targets)
    out["items"] += _bottoming_items(recs)
    out["items"] += _fuel_items(recs, setup, race_minutes, race_laps)
    return out


# ================================================================ file safety

SAFE_MEMBERS = {".sto", ".htm", ".html", ".txt", ".json", ".ibt"}
MAX_STO = 2 * 1024 * 1024                 # a setup is a few KB; anything this big is not a setup
MAX_ZIP = 500 * 1024 * 1024
MAX_MEMBERS = 500
_SCRIPT = re.compile(rb"powershell|cmd\.exe|cmd /c|wscript|cscript|vbscript|createobject|invoke-expression|"
                     rb"frombase64string|rundll32|regsvr32|mshta|<script|javascript:|@echo off|sub autoopen|"
                     rb"document_open|shell\(|start-process|#!/", re.I)
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
    m = _SCRIPT.search(head)
    if m:
        probs.append(f"{what} contains script or command text ('{m.group().decode('latin-1', 'replace')}')")
    return probs


def _bad_path(name: str) -> bool:
    n = name.replace("\\", "/")
    return (n.startswith("/") or re.match(r"^[A-Za-z]:", n) is not None or any(p == ".." for p in n.split("/"))
            or "\0" in n)


def check_setup_file(path) -> dict:
    """Check a setup file he got from someone else before it goes near iRacing. Reads only; never extracts or runs.

    .sto: must not be a program, a zip, an Office file or script text, and must be a sensible size.
    .zip: lists the members (from the zip's table only) and flags any member that is not .sto/.htm/.txt/.json/.ibt,
    any path that would escape the folder, nested zips, encrypted members, oversize contents, and checks the first
    bytes of each listed member the same way as a .sto.
    Returns {"ok": bool, "problems": [str], "kind": "sto" | "zip" | "other", "size": n, "members": [names]}"""
    p = Path(path)
    out = {"ok": False, "problems": [], "kind": "other", "size": 0, "members": [], "file": p.name}
    try:
        st = p.stat()
    except OSError as e:
        out["problems"].append(f"could not read the file ({e.strerror or e})")
        return out
    out["size"] = st.st_size
    suf = [s.lower() for s in p.suffixes]
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

def report(folder, car_path, track_code, series_setup=None, cache_path=None, race_minutes=None, race_laps=None,
           targets: dict | None = None, idx: dict | None = None, progress=None, stop=None) -> dict:
    """Everything Setup Lab shows for one car + track, JSON-ready.

    Pass `idx` (from index()) to skip the scan. Returns {"car", "car_path", "track", "track_code", "series_setup",
    "files": n, "setups": [{"id", "label", "name", "first", "last", "files"}], "runs": [...],
    "diffs": [{"from", "to", "changes": diff()}], "compare": compare(), "suggestions": suggestions() for the most
    recent setup (with "for_setup"), "notes": [str], "bad_files": [...]}"""
    idx = idx if idx is not None else index(folder, cache_path=cache_path, progress=progress, stop=stop)
    recs = [r for r in idx.get("files") or [] if _match(r, car_path, track_code)]
    kind = series_setup_kind(series_setup)
    out = {"car_path": car_path, "track_code": track_code, "car": "", "track": "", "series_setup": kind,
           "files": len(recs), "setups": [], "runs": [], "diffs": [], "compare": None, "suggestions": None,
           "notes": [], "bad_files": idx.get("bad") or []}
    if idx.get("missing_folder"):
        out["notes"].append("The telemetry folder was not found.")
    if idx.get("stopped"):
        out["notes"].append("The scan was stopped early, so some files are missing.")
    if out["bad_files"]:
        out["notes"].append(f"{len(out['bad_files'])} file{'s' if len(out['bad_files']) != 1 else ''} could not be read "
                            "(damaged or still being written) and were skipped.")
    if not recs:
        out["notes"].append("No telemetry for this car and track yet.")
        return out
    out["car"] = (recs[-1].get("car") or {}).get("name") or car_path
    t = recs[-1].get("track") or {}
    out["track"] = " - ".join(x for x in (t.get("name"), t.get("config")) if x) or track_code
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
                          targets=targets)
        sug["for_setup"] = latest
        out["suggestions"] = sug
        out["notes"] += sug.get("notes") or []
    out["notes"].append("Setups are read from the telemetry: iRacing's .sto setup files are encrypted and can't be read.")
    return out
