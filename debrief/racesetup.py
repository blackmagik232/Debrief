"""The "Setup" section of a race review (2.35): the setup he raced, what changed since his last session at this car
and track, whether that was faster (only when Setup Lab's own check says so), how the car handled, the tyre readings
iRacing took in the pit stall, and one change to try. Read only; never raises; nothing new is measured here.

    for_review(data, telemetry_folder, library_folder=None, setuplab_index=None) -> dict | None

Finding the race's telemetry (.ibt) files:
  - a review built from this PC (meta.source "pc", 2.34 localrace) names its files in meta.files;
  - a Garage 61 review is matched the way recordings.py joins a recording to a Garage 61 session: a file with a Race
    session, the same track and car (recordings.same_track / same_car on the file's own names) whose time covers the
    review's first lap start (meta.started_at) give or take recordings.WINDOW_S; then sessionibt.group_records (same
    car + track, same iRacing SubSessionID, files in a row) gives the rest of that race's files.
Setup Lab's saved index (setuplab.index's cache: pass the index dict or the cache file's path) is used for every file it
already has; a file it doesn't have is read once here (setuplab's own reader), and only files named for this car
(iRacing names them '<carpath>_<track> <date>.ibt') or, for a Garage 61 review, written around the race's time. The
cache file is only read, never written.

Every part reuses an existing module:
  setup raced, changes    setuplab.group (Setup A, B, ... labels), setuplab.diff against the newest file of the last
                          earlier session (sessionibt.group_records) at this car + track
  faster or not           setuplab.compare: only its "faster" verdict (3+ stints a setup, Welch interval clear of zero,
                          0.1 s or more) is said; anything else is "not enough to tell yet", with Setup Lab's reason
  handling                balance.load_laps on the race files, race laps only, then handling.analyze
  tyres                   setuplab._collect_readings (pit-stall carcass temperatures after a 5+ lap stint), the
                          setup's LastHotPressure (iRacing's pit reading) and setuplab._hot_laps (live hot pressures)
  one change              setupadvice.advise / card_text as they stand (no new tuning), always marked a suggestion;
                          held back, as the window's Car balance card does, while the series' setup rules are unknown
  open or fixed           the race file's own WeekendOptions.IsFixedSetup when the session is official, else
                          setuplab.series_setup_guess (newest official file, then the cars known to race fixed only)
A fixed-setup race shows only brake bias, traction control, ABS and fuel.
"""
from __future__ import annotations

import json
import logging
import math
import re
import time
from pathlib import Path

import numpy as np

from . import ibt
from . import names as _names
from . import setuplab as sl

_LOG = logging.getLogger("debrief.racesetup")

VERSION = 1
MAX_EXTRA_READS = 40           # files not in Setup Lab's index read here at most (newest first)
NEAR_RACE_S = 8 * 3600         # a Garage 61 review: unindexed files written within this of the race are read
IDS_WITHIN_S = 48 * 3600       # SubSessionIDs are read only for files this close before the race (to split sessions)

DIAL_LEAVES = {"bias": ("BrakePressureBias", "BrakeBias", "FrontBrakeBias"),
               "tc": ("TcSetting", "Tc1_Long", "TractionControl", "TcGain", "Tc1Setting"),
               "abs": ("AbsSetting", "ABSSetting", "Abs", "AbsTcEscSetting")}
DIAL_CHANNELS = {"bias": ("dcBrakeBias",), "tc": ("dcTractionControl", "dcTractionControl1"), "abs": ("dcABS",)}
CHANNEL_WORDS = {"Speed": "speed", "LapDistPct": "lap distance", "SteeringWheelAngle": "steering", "YawRate": "yaw rate",
                 "LatAccel": "sideways g", "Brake": "brake", "Throttle": "throttle"}
DIAL_NAMES = {"bias": "Brake bias", "tc": "Traction control", "abs": "ABS"}

SUGGESTION = "A suggestion, not a sure fix"
HELD = ("There is something to try, but Debrief doesn't know yet whether this series is open or fixed setup. Say which "
        "in Setup Lab (This series is) and the idea appears there.")
NO_PIT = ("No pit-stall tyre reading in this race: iRacing only measures tyre temperatures when you stop in your pit "
          "stall after at least 5 laps.")


# ---------------------------------------------------------------- small helpers
def _fin(x):
    """A finite float or None (never NaN or infinity in the output)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _num_text(v, nd=1) -> str:
    v = _fin(v)
    if v is None:
        return ""
    s = f"{v:.{nd}f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def _plural(n, word) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _clean(obj):
    """JSON-safe copy: NaN / infinity become None, tuples become lists, numpy numbers plain numbers."""
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        return _fin(obj)
    if isinstance(obj, np.bool_):
        return bool(obj)
    return obj


# ---------------------------------------------------------------- Setup Lab's records (index first, then the folder)
def _cached_records(setuplab_index) -> dict:
    """{file name: record} from Setup Lab's index dict or its cache file (read only)."""
    out = {}
    if not setuplab_index:
        return out
    if isinstance(setuplab_index, dict):
        files = setuplab_index.get("files") or []
        for r in files if isinstance(files, list) else []:
            if isinstance(r, dict) and r.get("ok", True) and isinstance(r.get("file"), str):
                out[r["file"]] = r
        return out
    try:
        raw = json.loads(Path(setuplab_index).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return out
    if not isinstance(raw, dict) or raw.get("version") != sl.CACHE_VERSION or not isinstance(raw.get("files"), dict):
        return out
    for key, r in raw["files"].items():
        if isinstance(r, dict) and r.get("ok") and isinstance(r.get("file"), str):
            r = dict(r)
            r["_key"] = key
            out[r["file"]] = r
    return out


def _current(rec: dict, st) -> bool:
    """True when an index record is for the file as it is now (same size and change time, when the record says)."""
    if rec.get("_key"):
        return rec["_key"] == f"{rec['file']}|{st.st_size}|{st.st_mtime_ns}"
    if rec.get("size") is not None and rec.get("mtime") is not None:
        return rec["size"] == st.st_size and rec["mtime"] == st.st_mtime_ns
    return True


def _read(path: Path, st=None) -> dict:
    """One file read the way setuplab.index reads it (same record shape). Never raises."""
    try:
        rec = sl._read_file(path)
    except Exception as e:  # noqa: BLE001
        rec = {"file": path.name, "ok": False, "error": str(e)[:200]}
    try:
        st = st or path.stat()
        rec["size"], rec["mtime"] = st.st_size, st.st_mtime_ns
    except OSError:
        pass
    rec["when"], rec["sort"] = sl._when(path.name, rec.get("start"))
    return rec


class _Files:
    """The telemetry folder's .ibt files, with Setup Lab's records where it has them and a record read here when not."""

    def __init__(self, folder, setuplab_index):
        self.folder = Path(folder) if folder else None
        self.cached = _cached_records(setuplab_index)
        self.stats = {}
        if self.folder and self.folder.is_dir():
            try:
                for p in self.folder.iterdir():
                    if p.suffix.lower() == ".ibt" and p.is_file():
                        try:
                            self.stats[p.name] = p.stat()
                        except OSError:
                            continue
            except OSError:
                pass
        self.recs = {}
        self.bad = []
        self.reads = 0

    def record(self, name: str) -> dict | None:
        """The record for one file (None when the file is gone or unreadable; unreadable ones are listed in bad)."""
        if name in self.recs:
            return self.recs[name]
        st = self.stats.get(name)
        rec = None
        if st is not None:
            c = self.cached.get(name)
            if c is not None and _current(c, st):
                rec = c
            else:
                self.reads += 1
                rec = _read(self.folder / name, st)
                if not rec.get("ok"):
                    self.bad.append({"file": name, "error": rec.get("error") or "unreadable"})
                    rec = None
        self.recs[name] = rec
        return rec

    def known(self) -> list:
        """Every record Setup Lab's index has for a file as it is now, plus every file read here (never a new read)."""
        out = []
        for name, st in self.stats.items():
            if name in self.recs:
                rec = self.recs[name]
            else:
                c = self.cached.get(name)
                rec = c if c is not None and _current(c, st) else None
            if rec:
                out.append(rec)
        return out

    def path(self, name: str) -> Path:
        return self.folder / name

    def unindexed(self, keep) -> list:
        """Names of files Setup Lab's index doesn't have as they are now, for which keep(name, stat) is True, newest
        first, at most MAX_EXTRA_READS."""
        out = []
        for name, st in self.stats.items():
            c = self.cached.get(name)
            if c is not None and _current(c, st):
                continue
            if keep(name, st):
                out.append((st.st_mtime, name))
        out.sort(reverse=True)
        return [n for _t, n in out[:MAX_EXTRA_READS]]


# ---------------------------------------------------------------- which files were this race
def _is_race(rec: dict) -> bool:
    return any(str(s).strip().lower() == "race" for s in rec.get("sessions") or [])


def _start(rec: dict) -> float:
    return float(rec.get("sort") or rec.get("start") or 0)


def _end(rec: dict) -> float:
    return _start(rec) + float((rec.get("detail") or {}).get("duration_s") or 0)


def _track_names(rec: dict) -> list:
    t = rec.get("track") or {}
    nm, cfg = t.get("name") or "", t.get("config") or ""
    out = [nm, _names.track(nm, cfg)]
    if cfg:
        out += [f"{nm} ({cfg})", f"{nm} - {cfg}"]
    return [x for x in out if x]


def _same_combo(rec: dict, track, car) -> bool:
    from . import recordings as rc
    c = rec.get("car") or {}
    car_ok = any(rc.same_car(x, car) for x in (c.get("name"), _names.car(c.get("name")), c.get("path")) if x)
    return car_ok and any(rc.same_track(x, track) for x in _track_names(rec))


def _pc_files(meta: dict, files: _Files) -> list:
    names = [str(n) for n in (meta.get("files") or []) if isinstance(n, str) and n]
    return [Path(n).name for n in names if Path(n).name in files.stats]


def _g61_files(meta: dict, files: _Files) -> list:
    from . import recordings as rc
    t0 = rc.epoch(meta.get("started_at"))
    if t0 is None or not meta.get("track") or not meta.get("car"):
        return []
    def near_race(name, st):
        # iRacing's file name carries the local time it started; the change time is when it was last written
        named = sl._when(name, None)[1]
        return abs(st.st_mtime - t0) <= NEAR_RACE_S or (named > 0 and abs(named - t0) <= NEAR_RACE_S)
    near = files.unindexed(near_race)
    for n in near:
        files.record(n)
    hits = []
    for name in set(files.cached) | set(near):
        if name not in files.stats:
            continue
        r = files.record(name)
        if not r or not _is_race(r):
            continue
        if _start(r) - rc.WINDOW_S <= t0 <= _end(r) + rc.WINDOW_S and _same_combo(r, meta.get("track"), meta.get("car")):
            hits.append(name)
    return sorted(hits, key=lambda n: _start(files.record(n)))


def _ids(recs: list, folder: Path | None, before: float) -> dict:
    from . import sessionibt
    out = {}
    for r in recs:
        if folder and 0 <= before - _start(r) <= IDS_WITHIN_S + 6 * 3600:
            out[r["file"]] = sessionibt._subsession(folder / r["file"])
    return out


# ---------------------------------------------------------------- the parts of the section
def _saved_answers() -> dict:
    """His open / fixed answers saved by Setup Lab (settings_io extra "setuplab", "series"). Never raises."""
    try:
        from .desktop import settings_io
        v = settings_io.load_extra("setuplab").get("series")
        return v if isinstance(v, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _scheduled(car_path: str, car_name: str, track_name: str):
    """(kind, source) from the season schedule, or None. Never raises."""
    try:
        from .desktop import server
        return server.schedule_series_setup({"car": car_name, "car_path": car_path, "track": track_name}, None)
    except Exception:  # noqa: BLE001
        return None


def _series(race_recs: list, history: list, car_path: str, car_name: str, track_code: str = "",
            track_name: str = "") -> dict:
    """setuplab.series_truth for this race: the SAME decision Setup Lab's page and Car balance make (this race's own
    telemetry first, then his answer, the schedule, the lists). A pure guess from the car's name is not acted on here:
    the series stays "unknown" (and the suggestion held) until he answers, as Car balance does."""
    t = sl.series_truth(history, car_path, car_name, _saved_answers(), track_code, None,
                        scheduled=_scheduled(car_path, car_name, track_name), prefer=race_recs)
    if not t["confirmed"] and t["kind"] != "unknown":
        t = dict(t, kind="unknown")
    return t


FIXED_TRY_TEXT = ("Fixed setup series: the garage is locked, so there is no setup change to try. The in-car dials "
                 "and the driving are what is yours.")
FIXED_RACE_TEXT = "Fixed setup series: everyone runs the same locked setup, so only the in-car dials and fuel were yours."


def _setup_part(g: dict, race_id: str, race_rec: dict, fixed: bool) -> dict:
    s = next((x for x in g["setups"] if x["id"] == race_id), None)
    det = race_rec.get("detail") or {}
    name = (det.get("setup_name") or race_rec.get("setup_name") or "").strip()
    if fixed:
        text = FIXED_RACE_TEXT
        return {"id": race_id, "label": "Fixed setup", "name": name, "text": text}
    if not s or not s.get("fingerprint"):
        return {"id": None, "label": "No setup recorded", "name": name,
                "text": "iRacing didn't write the setup into this race's telemetry, so Debrief can't say which one you raced."}
    return {"id": s["id"], "label": s["label"], "name": s.get("name") or name, "text": f"You raced {s['label']}."}


def _changes_part(recs: list, race_names: set, race_id: str, g: dict, folder) -> tuple:
    """(changes dict, id of the setup of the last earlier session or None)."""
    from . import sessionibt
    race_first = min(_start(r) for r in recs if r["file"] in race_names)
    earlier = [r for r in recs if r["file"] not in race_names and _start(r) < race_first]
    if not earlier:
        return ({"since": None, "lines": [], "text": "This is the first session Debrief can see for this car at this "
                 "track, so there is nothing earlier to compare the setup with."}, None)
    pool = earlier + [r for r in recs if r["file"] in race_names]
    groups = sessionibt.group_records(pool, ids=_ids(pool, folder, race_first))
    before = [grp for grp in groups if not any(r["file"] in race_names for r in grp)]
    prev = before[-1] if before else [earlier[-1]]
    with_setup = [r for r in prev if sl.fingerprint(r.get("setup") or {})]
    if not with_setup:
        return ({"since": prev[-1].get("when") or "", "lines": [], "text": "Your last session here had no setup "
                 "recorded in its telemetry, so Debrief can't say what changed."}, None)
    last = with_setup[-1]
    day = sl.day_text(last.get("when") or "") or "your last session"
    by = {s["id"]: s for s in g["setups"]}
    prev_id = last.get("setup_id")
    if prev_id == race_id:
        return ({"since": last.get("when") or "", "from": by.get(prev_id, {}).get("label"), "lines": [],
                 "same": True, "text": f"Same setup as your last session here ({day})."}, prev_id)
    race_setup = by.get(race_id, {}).get("setup") or {}
    items = [c for c in sl.diff(last.get("setup") or {}, race_setup) if c["kind"] == "setting"]
    lines = [c["text"] for c in items]
    frm = by.get(prev_id, {}).get("label") or "the setup before"
    short = f"Setup {prev_id}" if prev_id and prev_id != "?" else "another setup"
    if lines:
        text = (f"Since your last session here ({day}, on {short}), "
                f"{_plural(len(lines), 'setting')} changed:")
    else:
        text = f"Your last session here ({day}) was on {short}; no setting Debrief can read changed."
    return ({"since": last.get("when") or "", "from": frm, "lines": lines, "same": False, "text": text,
             "changes": [{k: c.get(k) for k in ("label", "old", "new", "delta", "unit", "both_sides", "text")}
                         for c in items]}, prev_id)


def _faster_part(recs: list, g: dict, prev_id, race_id) -> dict:
    out = {"verdict": "not enough to tell", "check": None, "text": "", "laps": None}
    if prev_id is None or race_id is None:
        out["text"] = "Nothing earlier to compare with, so there isn't enough to tell whether this setup is faster."
        return out
    if prev_id == race_id:
        out["verdict"] = "same setup"
        out["text"] = "Same setup as last time, so there is nothing to compare."
        return out
    cmp_ = sl.compare(recs, setups=g["setups"])
    pair = next((p for p in cmp_["pairs"] if {p["a"], p["b"]} == {prev_id, race_id}), None)
    if pair is None:
        rows = {r["id"]: r for r in cmp_["setups"]}
        miss = [m for i in (prev_id, race_id) for m in (rows.get(i) or {}).get("missing") or []]
        out["text"] = ("There isn't enough to tell whether the change made you faster yet"
                       + (": " + "; ".join(miss) if miss else "") + ".")
        return out
    out["check"] = pair["verdict"]
    out["diff_s"] = _fin(pair.get("diff_s"))
    if pair["verdict"] == "faster" and pair.get("faster") in (prev_id, race_id):
        out["verdict"] = "faster" if pair["faster"] == race_id else "slower"
        out["text"] = pair["text"]
        return out
    if pair["verdict"] == "no clear difference":
        out["text"] = "Not enough to tell yet. " + pair["text"]
    else:
        out["text"] = ("There isn't enough to tell whether the change made you faster yet. "
                       + pair["text"].split(": not enough to tell yet. ", 1)[-1])
    return out


def _handling_part(paths: list, kind: str) -> tuple:
    """(part, report, load info). Race laps only when the files have any."""
    from . import balance, handling
    laps, info = balance.load_laps([str(p) for p in paths])
    race = [x for x in laps if str(x.get("session") or "").lower() == "race"]
    rep = handling.analyze(race or laps, info=info, series_setup=kind if kind in ("open", "fixed") else None)
    part = {"ok": bool(rep.get("ok")), "laps": rep.get("laps") or 0, "issues": [], "text": ""}
    if not rep.get("ok"):
        miss = [CHANNEL_WORDS.get(c, c) for c in (info or {}).get("missing") or []]
        if not laps and miss:
            part["text"] = ("How the car handled: it can't be read from this race's telemetry, which has no "
                            + (", ".join(miss[:-1]) + " or " + miss[-1] if len(miss) > 1 else miss[0]) + " data.")
        else:
            msg = rep.get("message") or "not enough clean laps to read it."
            part["text"] = "How the car handled: " + msg[:1].lower() + msg[1:]
        return part, rep, info
    for i in rep.get("issues") or []:
        part["issues"].append({"title": i["title"], "kind": i["kind"], "phase": i["phase"],
                               "looks_like": "setup" if i["verdict"] == "setup" else "driving",
                               "sure_words": i["sure_words"], "corners": list(i["corners"]),
                               "laps_showing": i["laps_showing"], "laps": i["laps"], "text": i["text"]})
    if not part["issues"]:
        part["text"] = (f"How the car handled: no clear pattern of the car pushing (understeer) or rotating "
                        f"(oversteer) on these {_plural(part['laps'], 'clean race lap')}.")
    else:
        said = [f"{i['title'][:1].lower() + i['title'][1:]} (looks like "
                f"{'the setup' if i['looks_like'] == 'setup' else 'driving, not the setup'}; {i['sure_words']})"
                for i in part["issues"]]
        part["text"] = (f"How the car handled on {_plural(part['laps'], 'clean race lap')}: " + "; ".join(said) + ".")
    return part, rep, info


def _corner_text(c, t3) -> str:
    o, m, i = (_num_text(v, 0) for v in t3)
    return f"{sl.CORNER_WORDS[c]} {o} / {m} / {i} °C"


def _tyres_part(race_recs: list) -> dict:
    out = {"temps": None, "pit_pressures": None, "live_pressures": None, "lines": [], "text": ""}
    readings, _skip = sl._collect_readings(race_recs)
    if readings:
        r = readings[-1]
        temps = {c: [_fin(v) for v in r["tyres"][c]] for c in ("LF", "RF", "LR", "RR") if c in r["tyres"]}
        out["temps"] = {"corners": temps, "stint_laps": r.get("stint_laps")}
        out["lines"].append("Tyre temperatures at your pit stop (outer / middle / inner), after a "
                            f"{_plural(int(r.get('stint_laps') or 0), 'lap')} stint: "
                            + ", ".join(_corner_text(c, t) for c, t in temps.items()) + ".")
        # iRacing's own pit reading of the hot pressures, in the setup text, is only real once the tyres were measured
        setup = race_recs[-1].get("setup") or {}
        pit = {}
        for c in ("LF", "RF", "LR", "RR"):
            v = sl._setting_value(setup, sl._CK[c], "LastHotPressure")
            if _fin(ibt.number(v)) is not None:
                pit[c] = _fin(ibt.number(v))
        if pit and not all(sl._cold(t) for t in sl._reading_from_setup(setup).values() or [None]):
            out["pit_pressures"] = pit
            out["lines"].append("Hot pressures iRacing measured in the pit stall: "
                                + ", ".join(f"{sl.CORNER_WORDS[c]} {_num_text(v, 0)}" for c, v in pit.items()) + " kPa.")
    else:
        out["lines"].append(NO_PIT)
    laps = sl._hot_laps(race_recs, runs_only=True)
    if laps:
        med = {}
        for c in ("LF", "RF", "LR", "RR"):
            v = [x["press"][c] for _f, x in laps if c in (x.get("press") or {}) and _fin(x["press"][c]) is not None]
            if v:
                med[c] = round(float(np.median(v)), 1)
        if med:
            out["live_pressures"] = {"corners": med, "laps": len(laps)}
            out["lines"].append(f"Hot pressures on track (middle of {_plural(len(laps), 'clean lap')}, lap "
                                f"{sl.HOT_POS} of a stint on): "
                                + ", ".join(f"{sl.CORNER_WORDS[c]} {_num_text(v, 0)}" for c, v in med.items()) + " kPa.")
    out["text"] = " ".join(out["lines"])
    return out


def _dial_values(paths: list) -> dict:
    """{dial: (median, low, high)} from the in-car dial channels on track. Never raises."""
    vals = {}
    for p in paths:
        try:
            with ibt.open_ibt(p) as ib:
                on = ib.col("IsOnTrack").astype(bool) if ib.has("IsOnTrack") else None
                for dial, chans in DIAL_CHANNELS.items():
                    ch = next((c for c in chans if ib.has(c)), None)
                    if ch is None:
                        continue
                    a = ib.col(ch).astype(float)
                    if on is not None and len(on) == len(a) and on.any():
                        a = a[on]
                    a = a[np.isfinite(a)]
                    if len(a):
                        vals.setdefault(dial, []).append(a)
        except Exception:  # noqa: BLE001
            continue
    out = {}
    for d, arrs in vals.items():
        a = np.concatenate(arrs)
        out[d] = (float(np.median(a)), float(a.min()), float(a.max()))
    return out


def _dials_part(setup: dict, paths: list) -> list:
    from . import balance as bl
    flat = ibt.flatten(setup or {})
    live = _dial_values(paths)
    out = []
    for d in ("bias", "tc", "abs"):
        if d in live:
            med, lo, hi = live[d]
            v = _num_text(med, 2)
            text = f"{DIAL_NAMES[d]}: {v}"
            if hi - lo > 1e-6:
                text += f" (you moved it between {_num_text(lo, 2)} and {_num_text(hi, 2)} during the race)"
            out.append({"dial": d, "label": DIAL_NAMES[d], "value": v, "from": "in-car dial", "text": text + "."})
            continue
        _k, val = bl._find_key(flat, DIAL_LEAVES[d])
        if val not in (None, ""):
            v = str(val).strip()
            label = "ABS and traction control mode" if _k and _k.endswith("AbsTcEscSetting") else DIAL_NAMES[d]
            out.append({"dial": d, "label": label, "value": v, "from": "setup", "text": f"{label}: {v}."})
    return out


def _fuel_part(race_recs: list, setup: dict) -> dict | None:
    rows = [x for r in race_recs for x in ((r.get("detail") or {}).get("laps") or [])
            if str(x.get("session") or "").lower() == "race"]
    used = [x["fuel"] for x in rows if x.get("clean") and _fin(x.get("fuel")) is not None]
    first = next((x.get("fuel_level") for x in rows if _fin(x.get("fuel_level")) is not None), None)
    garage = _fin(ibt.number(next((v for k, v in ibt.flatten(setup or {}).items() if k.endswith("FuelLevel")), None)))
    out = {"start_l": _fin(first) if first is not None else garage, "per_lap_l": None, "laps": len(used), "text": ""}
    parts = []
    if out["start_l"] is not None:
        parts.append(f"{'about ' if first is not None else ''}{_num_text(out['start_l'], 1)} L in the tank at the start"
                     + ("" if first is not None else " (as set in the garage)"))
    if used:
        out["per_lap_l"] = round(float(np.median(used)), 2)
        parts.append(f"about {_num_text(out['per_lap_l'], 2)} L a lap (middle of {_plural(len(used), 'clean lap')})")
    if not parts:
        return None
    out["text"] = "Fuel: " + ", ".join(parts) + "."
    return out


def _suggest_text(what: str, title: str, sure_words, then) -> str:
    return (f"One change to try ({SUGGESTION.lower()}): {what[:1].lower() + what[1:]}"
            + (f", for {title[:1].lower() + title[1:]} ({sure_words})" if title else "")
            + f". {then or ''}").strip()


def _try_part(rep: dict, info: dict, setup: dict, kind: str, last_change, car_path, track_code,
              confirmed: bool = True) -> dict:
    from . import setupadvice as sa
    advice = sa.advise(rep, setup, series_setup=kind if kind in ("open", "fixed") else None, last_change=last_change,
                       dials=(info or {}).get("dials"))
    top = advice.get("top")
    out = {"suggestion": True, "text": "", "detail": None, "setting": None, "sure_words": None, "held": False,
           "card": advice.get("card") or "",
           "open_setup_lab": {"car_path": car_path, "track_code": track_code, "route": "api/setuplab/report",
                              "query": {"car": car_path, "track": track_code}}}
    if top and (top.get("change") or top.get("dial")) and top.get("verdict") == "setup":
        what = top["change"]["text"] if top.get("change") else top["dial"]["text"]
        # kept so a re-render can show it, without reading the telemetry again, once he has said open or fixed
        pending = {"what": what, "garage": bool(top.get("change")), "title": top.get("title") or "",
                   "sure_words": top.get("sure_words"), "then": top.get("then") or "", "detail": top.get("text"),
                   "setting": (top.get("change") or top.get("dial") or {}).get("label")}
        if kind not in ("open", "fixed") or not confirmed:
            out["held"] = True
            out["text"] = HELD
            out["pending"] = pending
            return out
        out["setting"], out["sure_words"] = pending["setting"], pending["sure_words"]
        out["text"] = _suggest_text(what, pending["title"], pending["sure_words"], pending["then"])
        out["detail"] = pending["detail"]
        return out
    if top and top.get("verdict") == "driving":
        out["text"] = ("Nothing here points at the setup: what the car did looks like driving. "
                       + (top.get("coach") or "The corner coach has the detail."))
        return out
    if not (rep or {}).get("ok"):
        out["text"] = "No setup change to suggest: the car's handling couldn't be read from this race."
        return out
    why = advice.get("empty") or "nothing clear enough to change."
    out["text"] = "No setup change to suggest. " + why
    return out


def _early_line(out: dict):
    """The 'Early advice: not yet checked on real cars.' line, when this section shows handling items or a change to try."""
    from . import handling
    line = handling.early_advice_line()
    if not line:
        return None
    issues = ((out.get("handling") or {}).get("issues")) or []
    t = out.get("try") or {}
    return line if (issues or t.get("setting") or t.get("pending")) else None


def _assemble_lines(out: dict) -> list:
    """Every sentence of the section in order (one place, so a re-render that changes the series rebuilds it the same)."""
    fixed = bool(out.get("fixed"))
    su = out.get("setup") or {}
    lines = [su.get("text")]
    if not fixed and su.get("id"):
        ch, fa = out.get("changes") or {}, out.get("faster") or {}
        lines += [ch.get("text")] + list(ch.get("lines") or []) + [fa.get("text")]
    lines.append((out.get("handling") or {}).get("text"))
    if out.get("early_advice"):
        lines.append(out["early_advice"])
    if not fixed:
        lines += list((out.get("tyres") or {}).get("lines") or [])
    if fixed:
        lines += [d["text"] for d in out.get("dials") or []] or ["No brake bias, traction control or ABS setting in this "
                                                                 "car's telemetry."]
        if out.get("fuel"):
            lines.append(out["fuel"]["text"])
    lines.append((out.get("try") or {}).get("text"))
    return [x for x in lines if x]


_SURE_PAREN = re.compile(r"(\(|; )(Sure|Likely)(\))")
_RECHECK = ("answer", "schedule", "car_answer", "list")   # origins that a later answer or schedule can change
REBUILD_FOR_OPEN = "Rebuild to see the full setup comparison."


def _cap_text(v):
    return _SURE_PAREN.sub(lambda m: m.group(1) + "A hint" + m.group(3), v)


def _cap_walk(v):
    """(copy of v with "Sure" / "Likely" in a sureness place capped to "A hint", changed?)."""
    if isinstance(v, str):
        n = _cap_text(v)
        return n, n != v
    if isinstance(v, list):
        out = [_cap_walk(x) for x in v]
        return [x for x, _c in out], any(c for _x, c in out)
    if isinstance(v, dict):
        res, ch = {}, False
        for k, x in v.items():
            if k == "sure_words" and x in ("Sure", "Likely"):
                res[k], ch = "A hint", True
            elif k == "sureness" and x in ("sure", "likely"):
                res[k], ch = "hint", True
                if "sureness_raw" not in v:
                    res["sureness_raw"] = x
            else:
                res[k], c = _cap_walk(x)
                ch = ch or c
        return res, ch
    return v, False


def cap_early(sec):
    """2.36: a Setup section saved by 2.35 carries "Sure" / "Likely" in its handling items and its change to try, and no
    early-advice line. While handling.TUNED_ON_REAL_LAPS is False this caps those words to "A hint" and adds the line,
    from the saved text alone (no telemetry read). Returns the same object when nothing changes. Never raises."""
    try:
        from . import handling
        if handling.TUNED_ON_REAL_LAPS or not isinstance(sec, dict) or sec.get("ok") is not True:
            return sec
        out, changed = _cap_walk(sec)
        if not out.get("early_advice"):
            line = _early_line(out)
            if line:
                out["early_advice"] = line
                lines = list(out.get("lines") or [])
                htxt = (out.get("handling") or {}).get("text")
                at = lines.index(htxt) + 1 if htxt in lines else max(0, len(lines) - 1)
                lines.insert(at, line)
                out["lines"] = lines
                changed = True
        return out if changed else sec
    except Exception:  # noqa: BLE001
        return sec


def refresh_series(sec):
    """A saved Setup section brought up to date from saved data alone, WITHOUT reading any telemetry: (1) 2.35's "Sure" /
    "Likely" are capped and the early-advice line added (cap_early); (2) the series part is worked out again from his saved
    answer (and the schedule): a section that was held, whose series was unknown / a guess, or that was built from his answer,
    the schedule or the list follows a later answer. Returns the same object when nothing changes; never raises."""
    sec = cap_early(sec)
    try:
        if not isinstance(sec, dict) or sec.get("ok") is not True or sec.get("series_origin") == "telemetry":
            return sec
        t0 = sec.get("try") if isinstance(sec.get("try"), dict) else {}
        if t0.get("held") and not isinstance(t0.get("pending"), dict):
            return sec                                   # saved before 2.36: nothing to show instead, so it stays held
        if not (t0.get("held") or sec.get("series_setup") not in ("open", "fixed") or sec.get("series_confirmed") is False
                or sec.get("series_origin") in _RECHECK):
            return sec
        car_path, track_code = sec.get("car_path") or "", sec.get("track_code") or ""
        t = sl.series_truth([], car_path, sec.get("car"), _saved_answers(), track_code, None,
                            scheduled=_scheduled(car_path, sec.get("car") or "", sec.get("track") or ""))
        if t["kind"] not in ("open", "fixed") or not t["confirmed"]:
            return sec
        same = t["kind"] == sec.get("series_setup") and bool(sec.get("fixed")) == (t["kind"] == "fixed")
        if same and not t0.get("held") and sec.get("series_confirmed") is not False:
            if t["origin"] == sec.get("series_origin") and t["source"] == sec.get("series_setup_source"):
                return sec
            out = json.loads(json.dumps(sec))
            out.update(series_setup_source=t["source"], series_origin=t["origin"])
            return out
        out = json.loads(json.dumps(sec))
        out.update(series_setup=t["kind"], series_setup_source=t["source"], series_origin=t["origin"],
                   series_confirmed=True, fixed=t["kind"] == "fixed")
        if out["fixed"] and not sec.get("fixed"):
            su = dict(out.get("setup") or {})
            su.update(label="Fixed setup", text=FIXED_RACE_TEXT)
            out["setup"] = su
            out["changes"] = out["faster"] = out["tyres"] = None
        elif sec.get("fixed") and not out["fixed"]:
            su = dict(out.get("setup") or {})            # the open parts (changes, tyres) were never worked out
            su.update(id=None, label="Open setup", text=REBUILD_FOR_OPEN)
            out["setup"] = su
            out["changes"] = out["faster"] = out["tyres"] = None
        tr = out.get("try") if isinstance(out.get("try"), dict) else {}
        pend = tr.pop("pending", None) if tr.get("held") else None
        if pend:
            if out["fixed"] and pend.get("garage"):
                tr.update(held=False, text=FIXED_TRY_TEXT, detail=None, setting=None, sure_words=None)
            else:
                tr.update(held=False, setting=pend.get("setting"), sure_words=pend.get("sure_words"),
                          text=_suggest_text(pend["what"], pend.get("title") or "", pend.get("sure_words"),
                                             pend.get("then")), detail=pend.get("detail"))
        elif out["fixed"] and not sec.get("fixed") and tr.get("setting"):
            dial_labels = {d.get("label") for d in out.get("dials") or []}
            if tr.get("setting") not in dial_labels:    # a garage change to try has no place in a fixed setup series
                tr.update(text=FIXED_TRY_TEXT, detail=None, setting=None, sure_words=None)
        out["try"] = tr
        out["early_advice"] = _early_line(out)
        out["lines"] = _assemble_lines(out)
        return cap_early(out)
    except Exception:  # noqa: BLE001
        _LOG.exception("racesetup: the saved Setup section could not be refreshed")
        return sec


# ---------------------------------------------------------------- the section
def for_review(data: dict, telemetry_folder, library_folder=None, setuplab_index=None) -> dict | None:
    """The Setup section for one race review (see the module docstring). None when no telemetry file of this race can
    be found; a dict with ok False and a plain "reason" when one was found but couldn't be used. Never raises.

    library_folder is accepted for the review pages' common signature; nothing is read from it yet.

    {"ok", "version", "source" ("pc" | "garage61"), "files": [names], "car", "car_path", "track", "track_code",
     "series_setup" ("open" | "fixed" | "unknown"), "series_setup_source", "fixed",
     "setup": {"id", "label", "name", "text"},
     "changes": {"since", "from", "lines": [plain lines], "same", "text"} | None (fixed),
     "faster": {"verdict" ("faster" | "slower" | "same setup" | "not enough to tell"), "check" (Setup Lab's own
               verdict or None), "text"} | None (fixed),
     "handling": {"ok", "laps", "issues": [{"title", "looks_like" ("setup" | "driving"), "sure_words", "corners",
                  "text", ...}], "text"},
     "tyres": {"temps", "pit_pressures", "live_pressures", "lines", "text"} | None (fixed),
     "dials": [{"dial", "label", "value", "from", "text"}], "fuel": {"start_l", "per_lap_l", "laps", "text"} | None,
     "try": {"suggestion": True, "text", "detail", "setting", "sure_words", "held", "card",
             "open_setup_lab": {"car_path", "track_code", "route", "query": {"car", "track"}}},
     "lines": every sentence above in order, "notes": [...]}"""
    try:
        return _for_review(data, telemetry_folder, setuplab_index)
    except Exception as e:  # noqa: BLE001
        _LOG.exception("racesetup: the Setup section could not be worked out")
        return {"ok": False, "version": VERSION,
                "reason": f"The Setup section couldn't be worked out this time ({type(e).__name__})."}


def _for_review(data, telemetry_folder, setuplab_index):
    meta = (data or {}).get("meta") if isinstance((data or {}).get("meta"), dict) else {}
    if not telemetry_folder or not Path(telemetry_folder).is_dir():
        return None
    files = _Files(telemetry_folder, setuplab_index)
    pc = str(meta.get("source") or "") == "pc" or bool(meta.get("files"))
    names = _pc_files(meta, files) if pc else _g61_files(meta, files)
    if not names:
        return None
    out = {"ok": False, "version": VERSION, "source": "pc" if pc else "garage61", "files": names, "notes": []}
    race_recs = [r for r in (files.record(n) for n in names) if r]
    if not race_recs:
        out["reason"] = ("The telemetry file for this race couldn't be read (it may be damaged or still being written), "
                         "so there is no Setup section.")
        return out
    newest = max(race_recs, key=_start)
    car_path = (newest.get("car") or {}).get("path") or ""
    track_code = (newest.get("track") or {}).get("code") or ""
    prefix = f"{car_path}_".lower()
    for n in files.unindexed(lambda nm, _st: nm.lower().startswith(prefix)):
        files.record(n)
    allrecs = files.known()
    allrecs = sl.merge_track_codes(allrecs)
    race_names = set(names) & {r["file"] for r in race_recs}
    combo = next((r for r in allrecs if r["file"] == newest["file"]), newest)
    track_code = (combo.get("track") or {}).get("code") or track_code
    history = [dict(r) for r in allrecs if sl._match(r, car_path, track_code) or r["file"] in race_names]
    history.sort(key=lambda r: (_start(r), r["file"]))
    race_h = [r for r in history if r["file"] in race_names]
    t = newest.get("track") or {}
    car_name = (newest.get("car") or {}).get("name") or ""
    out.update(car=_names.car(car_name) or car_path, car_path=car_path, track_code=track_code,
               track=_names.track(t.get("name"), t.get("config")) or track_code)
    if files.bad:
        out["notes"].append(f"{_plural(len(files.bad), 'telemetry file')} couldn't be read and "
                            f"{'was' if len(files.bad) == 1 else 'were'} left out.")
    if len(race_recs) < len(names):
        out["notes"].append("Part of this race's telemetry couldn't be read, so this uses the rest.")

    truth = _series(race_h, history, car_path, car_name, track_code, out["track"])
    kind, source = truth["kind"], truth["source"]
    fixed = kind == "fixed"
    out.update(series_setup=kind, series_setup_source=source, fixed=fixed, series_origin=truth["origin"],
               series_confirmed=bool(truth["confirmed"]))
    g = sl.group(history)
    newest_h = max(race_h, key=_start)
    race_id = newest_h.get("setup_id")
    setup = newest_h.get("setup") or {}
    out["setup"] = _setup_part(g, race_id, newest_h, fixed)
    lines = [out["setup"]["text"]]
    prev_id = None
    out["changes"] = out["faster"] = out["tyres"] = None
    if not fixed and out["setup"]["id"]:
        out["changes"], prev_id = _changes_part(history, race_names, race_id, g, files.folder)
        lines.append(out["changes"]["text"])
        lines += out["changes"]["lines"]
        out["faster"] = _faster_part(history, g, prev_id, race_id)
        lines.append(out["faster"]["text"])
    paths = [files.path(r["file"]) for r in race_h]
    hpart, rep, info = _handling_part(paths, kind)
    out["handling"] = hpart
    lines.append(hpart["text"])
    if not fixed:
        out["tyres"] = _tyres_part(race_h)
        lines += out["tyres"]["lines"]
    out["dials"] = _dials_part(setup, paths)
    if fixed:
        lines += [d["text"] for d in out["dials"]] or ["No brake bias, traction control or ABS setting in this car's "
                                                       "telemetry."]
    out["fuel"] = _fuel_part(race_h, setup)
    if fixed and out["fuel"]:
        lines.append(out["fuel"]["text"])
    last = None
    if not fixed and race_id:
        try:
            last = sl.change_into(history, race_id)
        except Exception:  # noqa: BLE001
            last = None
    out["try"] = _try_part(rep, info, setup, kind, last, car_path, track_code, bool(truth["confirmed"]))
    lines.append(out["try"]["text"])
    out["early_advice"] = _early_line(out)
    out["lines"] = _assemble_lines(out)
    out["ok"] = True
    return _clean(out)
