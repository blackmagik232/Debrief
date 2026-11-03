"""Teammate reference (2.15): a teammate's .ibt file as the lap to learn from, for the live coach and the corner
coach. Read only: the file is opened where it is, never copied, moved or changed.

    load_reference(path, car_path=None, track_code=None, prefix="T") -> result dict:
        {"ok", "reason" (plain words when refused), "driver", "label" ("Alex Smith's best lap (1:38.874)"),
         "car", "track", "lap", "lap_time",
         "lap_text", "file", "check": {...the safety check...},
         "livecoach": livecoach.Reference | None      (pass to livecoach.Coach as its reference)
         "cornercoach": lap dict | None               (pass to cornercoach.analyze_laps/analyze_files(reference=);
                                                       its label, "Alex Smith's best lap", is used inside sentences)
         "summary": JSON-ready description (no arrays)}

Checks, in order (the first one that fails refuses the file, with the reason in plain words):
  1. it is a file named .ibt, between MIN_IBT and MAX_IBT bytes;
  2. Setup Lab's file check (setuplab.check_setup_file: not a program, Office file or zip in disguise);
  3. no Windows program hidden anywhere inside (the whole file is searched, not only its start);
  4. iRacing's telemetry header reads correctly (ibt.open_ibt);
  5. same car and track as his (when car_path / track_code are given);
  6. it has a clean, timed lap that covers the whole track.
The driver's name comes from the player car in THEIR file (DriverInfo.Drivers[DriverCarIdx].UserName).
"""
from __future__ import annotations

import mmap
from pathlib import Path

import numpy as np

from . import cornercoach, ibt, livecoach, setuplab

MAX_IBT = 1536 * 1024 * 1024      # 1.5 GB: a two-hour race at 60 samples a second is well under this
MIN_IBT = 1024
_STUB = (b"This program cannot be run in DOS mode", b"This program must be run under Win32")
CC_NAMES = {"v": "Speed", "p": "LapDistPct", "b": "Brake", "th": "Throttle", "gear": "Gear", "abs": "BrakeABSactive",
            "la": "LatAccel", "lo": "LongAccel", "st": "SteeringWheelAngle", "rpm": "RPM"}


def _hidden_program(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            if Path(path).stat().st_size == 0:
                return False
            with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                return any(mm.find(s) >= 0 for s in _STUB)
    except (OSError, ValueError):
        return True                     # can't be checked -> not used


def check_file(path) -> dict:
    """The safety checks 1 to 4. {"ok", "problems": [str], "size"}. Never raises; opens nothing for writing."""
    p = Path(str(path))
    out = {"ok": False, "problems": [], "size": 0, "file": p.name}
    try:
        if not p.is_file():
            out["problems"].append("the file was not found")
            return out
        out["size"] = p.stat().st_size
    except OSError as e:
        out["problems"].append(f"the file could not be read ({e.strerror or e})")
        return out
    if p.suffix.lower() != ".ibt":
        out["problems"].append("it is not a telemetry file (iRacing's end in .ibt)")
        return out
    if out["size"] > MAX_IBT:
        out["problems"].append(f"it is {out['size'] // (1024 * 1024)} MB, far bigger than any telemetry file")
        return out
    if out["size"] < MIN_IBT:
        out["problems"].append("it is too small to be a telemetry file")
        return out
    sc = setuplab.check_setup_file(p)
    if sc.get("problems"):
        out["problems"] += sc["problems"]
        return out
    if _hidden_program(p):
        out["problems"].append("it has a Windows program hidden inside it")
        return out
    try:
        with ibt.open_ibt(p) as ib:
            if not ib.vars or ib.record_count < 1:
                out["problems"].append("it holds no telemetry")
    except ibt.IbtError as e:
        out["problems"].append(f"it is not a readable iRacing telemetry file ({e})")
    except Exception as e:  # noqa: BLE001
        out["problems"].append(f"it could not be read ({str(e)[:80]})")
    out["ok"] = not out["problems"]
    return out


def _cc_raw(ib, lap: dict) -> dict:
    """cornercoach's raw lap shape (engine.parse_csv's names) for one lap of an open file."""
    a, b = lap["start"], lap["end"]
    n = b - a
    raw, missing = {}, []
    for k, nm in CC_NAMES.items():
        if ib.has(nm):
            raw[k] = np.asarray(ib.col(nm)[a:b], float)
        else:
            missing.append("ABSActive" if nm == "BrakeABSactive" else nm)
            raw[k] = np.zeros(n)
    raw["lat"] = np.full(n, np.nan)
    raw["lon"] = np.full(n, np.nan)
    raw["_missing"] = sorted(set(missing))
    for w in cornercoach.WHEELS:
        if ib.has(w):
            raw[w] = np.asarray(ib.col(w)[a:b], float)
    return raw


def _length_m(ib) -> float | None:
    km = ib.track().get("length_km")
    if km and km > 0.1:
        return km * 1000.0
    if ib.has("LapDist"):
        v = ib.col("LapDist")
        return float(np.nanmax(v)) if len(v) else None
    return None


def load_reference(path, car_path: str | None = None, track_code: str | None = None, prefix: str = "T") -> dict:
    """A teammate's best clean lap as a reference for both coaches. Never raises."""
    p = Path(str(path))
    out = {"ok": False, "reason": "", "driver": "", "label": "", "car": "", "car_path": "", "track": "",
           "track_code": "", "lap": None, "lap_time": None, "lap_text": "", "file": p.name, "check": None,
           "livecoach": None, "cornercoach": None, "summary": None}
    chk = check_file(p)
    out["check"] = chk
    if not chk["ok"]:
        out["reason"] = "This file can't be used: " + "; ".join(chk["problems"]) + "."
        return _done(out)
    try:
        with ibt.open_ibt(p) as ib:
            car, tr = ib.car(), ib.track()
            out.update(car=car.get("name") or car.get("path") or "", car_path=car.get("path") or "",
                       track=" ".join(x for x in (tr.get("name"), f"({tr['config']})" if tr.get("config") else "") if x),
                       track_code=tr.get("code") or "")
            name = str(ib.player().get("UserName") or "").strip()
            out["driver"] = name or "Your teammate"
            if car_path and str(car_path).lower() != out["car_path"].lower():
                out["reason"] = (f"This lap is in a different car ({out['car'] or out['car_path']}). A reference only "
                                 f"helps in the same car, so pick a file from the same car.")
                return _done(out)
            if track_code and str(track_code).lower() != out["track_code"].lower():
                out["reason"] = (f"This lap is at a different track or layout ({out['track'] or out['track_code']}). "
                                 f"Pick a file from the same track and layout.")
                return _done(out)
            best = livecoach.best_lap_in(ib)
            if best is None:
                out["reason"] = "This file has no clean, timed lap (every lap had an off, a pit stop or an incident)."
                return _done(out)
            L = _length_m(ib)
            if not L:
                out["reason"] = "The track length isn't in this file, so the lap can't be lined up with yours."
                return _done(out)
            ref = livecoach.build_reference(ib, prefix=prefix)
            lap_text = livecoach.fmt_lap(best["time"])
            short = f"{possessive(name)} best lap" if name else "your teammate's best lap"
            label = f"{short[:1].upper() + short[1:]} ({lap_text})"
            if ref is not None:
                ref.source = dict(ref.source, kind="teammate", driver=out["driver"], label=short)
            cc = cornercoach.lap_from_raw(_cc_raw(ib, best), best["time"], L, label=short)
            if ref is None and cc is None:
                out["reason"] = "The best lap doesn't cover the whole track, so it can't be used as a reference."
                return _done(out)
            out.update(ok=True, lap=best["lap"], lap_time=round(float(best["time"]), 3), lap_text=lap_text,
                       label=label, livecoach=ref, cornercoach=cc)
            return _done(out)
    except Exception as e:  # noqa: BLE001
        out["reason"] = f"This file could not be read ({str(e)[:120]})."
        return _done(out)


def possessive(name: str) -> str:
    """"Alex Smith's", "Swazi Williams'" (a name ending in s takes only the apostrophe)."""
    name = str(name or "").strip()
    return name + ("'" if name.lower().endswith("s") else "'s")


def _done(out: dict) -> dict:
    out["summary"] = {k: out[k] for k in ("ok", "reason", "driver", "label", "car", "track", "lap", "lap_time",
                                          "lap_text", "file")}
    if out.get("livecoach") is not None:
        try:
            out["summary"]["corners"] = len(out["livecoach"].corners)
        except Exception:  # noqa: BLE001
            pass
    return out
