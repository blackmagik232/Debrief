"""Live voice coach (2.13): while you drive, a few short spoken cues based on YOUR OWN best lap and your weak corners.

Pure logic first, then where the samples come from:

  Reference      your best clean lap for this car and track, on a LapDistPct grid (GRID points), with its corners.
                 Built from your .ibt telemetry files (build_reference / find_reference), from a Garage 61 lap
                 (reference_from_g61_csv: the same parsed-CSV shape engine.analyze uses), or from a lap the coach
                 just watched you drive (a new best, when Settings.update_reference allows).
                 Corners and brake points come from Debrief's own corner model (engine.detect_corners,
                 engine.corner_metrics: the same detector, brake-point rule and C1..Cn numbering the reviews use),
                 so "T3" here is the corner the review page calls C3.
  weak corners   what the reviews say costs you time (prep.weak_corners rows: id / apex_m / avg_avail), matched to
                 the reference's corners by apex position, as prep.match_corners does. Optional.
  Coach          feed(sample) -> [Cue]. A sample is a dict of iRacing channel names (SessionTime, Lap, LapDistPct,
                 Speed, Brake, Throttle, Gear, OnPitRoad, PlayerTrackSurface, CarLeftRight, IsOnTrack,
                 PlayerCarMyIncidentCount, SessionNum) plus "SessionType" (Practice / Lone Qualify / Race / ...).
  LiveRunner     a background thread: reads a source ~20 times a second, feeds the Coach, hands cues to a voice.
  IrsdkSource    live iRacing through pyirsdk (imported only when used). read() is None while iRacing isn't running.
  IbtPlaybackSource  plays an .ibt file as if it were live (real time, faster, or as fast as possible for tests).

Cues are deliberately few, short (2-5 words) and quiet ("one focus at a time"):
  a) "Brake marker, T3"           focus cue on the straight before one of the top 2 weak corners: at least 150 m
                                  before the reference brake point and timed so it ENDS >= 1.0 s before it
                                  (distance = reference speed x (cue length + 1.0 s + 0.1 s audio)); if it can't
                                  fit, it is dropped
  b) "T3: more speed, 6 down" / "T3, braked early"   on the straight after a weak corner; when the weak corners
                                  come from the corner coach (a "phrase" per corner), its advice instead
                                  ("T3: lighter brake")
  c) "Down three tenths" / "New best, up two tenths"  once per lap after LapLastLapTime updates, on the first
                                  straight where it fits (within 10 s); clean laps, 0.1 s <= delta <= 2 s
  Limits: at most 2 corrective cues (a + b) per lap and 1 per corner per lap; a corner is cued again only after
  2 laps unless the error persists; nothing about a corner he is driving well; at most one cue every 4 s.
  d) silence: a car alongside (stop talking at once, forget queued cues, quiet until 2 s after it clears), under
     braking (pedal > 0.05) or inside any corner's braking/apex zone, pit road, off track, out/in laps, a lap not
     seen from its start, race lap 1 (and races at all unless in_races), mute. A cue that can't start in time is
     dropped, never spoken late.
  Optional: a short tone at a weak corner's brake point (Settings.brake_beep, off by default).

Nothing here imports pyirsdk, Windows modules or the window at import time. CPU: a handful of comparisons per sample.
"""
from __future__ import annotations

import logging
import math
import re
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path

import numpy as np

from . import engine, ibt

GRID = 1000                 # reference points around the lap
KMH = 3.6
BRAKE_ON = 0.1              # live brake pedal above this (after being below it) is a brake onset
_LOG = logging.getLogger("debrief.livecoach")

ABS_IBT = "BrakeABSactive"   # the ABS channel's name in .ibt files (Garage 61's CSV calls it ABSActive)
MAX_PHRASE = 60             # characters: a corner-coach phrase longer than this is not spoken

KIND_MARKER, KIND_FEEDBACK, KIND_LAP, KIND_BEEP = "marker", "feedback", "lap", "beep"
PRIORITY = {KIND_MARKER: 3, KIND_FEEDBACK: 2, KIND_LAP: 1}


# ---------------------------------------------------------------- settings

@dataclass
class Settings:
    markers: bool = True                # cue a): focus cue on the straight before a weak corner
    feedback: bool = True               # cue b): after a weak corner, on the next straight
    lap_delta: bool = True              # cue c)
    top_n: int = 2                      # weak corners coached per session ("one focus at a time")
    max_per_lap: int = 2                # corrective cues (markers + feedback) per lap, all corners together
    repeat_laps: int = 2                # a corner is cued at most every this many laps, unless the error persists
    end_before_brake_s: float = 1.0     # a cue must END at least this long before a brake point
    audio_latency_s: float = 0.1        # sound card / player start-up allowed for in that sum
    words_per_s: float = 2.5            # speaking speed for cues with no pre-rendered length
    marker_min_m: float = 150.0         # the focus cue starts at least this far before the brake point
    marker_window_s: float = 0.6        # ...within this much time of its latest start; later it is dropped
    slower_kmh: float = 4.0             # min speed this much under the reference -> "more speed"
    early_m: float = 15.0               # braked this much earlier than the reference -> "braked early"
    delta_min_s: float = 0.1            # lap delta said only from this size
    lap_cue_wait_s: float = 10.0        # the lap delta may wait this long after LapLastLapTime updates for a free
                                        # straight (at Zolder the line is only ~165 m before T1's brake point, so a
                                        # 2 s limit dropped every lap delta on his real files)
    lap_delta_max_s: float = 2.0        # a lap this far off the reference (traffic, a moment) gets no delta
    in_races: bool = False              # coach in Race sessions too
    update_reference: bool = True       # a new best clean lap becomes the reference
    gap_s: float = 4.0                  # at most one cue every gap_s
    traffic_hold_s: float = 2.0         # quiet this long after a car alongside has cleared
    braking_pedal: float = 0.05         # no cue starts with the brake pedal above this
    stale_s: float = 2.0                # a cue not said within this is dropped
    brake_beep: bool = False            # a short tone at a weak corner's brake point
    muted: bool = False
    volume: int = 100                   # 0..100
    rate: int = 0                       # SAPI speaking rate -10..10
    sample_hz: float = 20.0             # how often live telemetry is read
    corner_prefix: str = "T"            # spoken corner name: T3
    hotkey: str = "ctrl+shift+m"        # mute toggle (optional, Windows)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> "Settings":
        """Unknown keys are ignored and bad values fall back to the default, so an old or hand-edited settings
        file never stops the coach."""
        s = cls()
        for f in fields(cls):
            if not isinstance(d, dict) or f.name not in d:
                continue
            default = getattr(s, f.name)
            v = d[f.name]
            try:
                if isinstance(default, bool):
                    v = v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")
                elif isinstance(default, int):
                    v = int(v)
                elif isinstance(default, float):
                    v = float(v)
                    if not math.isfinite(v):
                        continue
                else:
                    v = str(v)
            except (TypeError, ValueError):
                continue
            setattr(s, f.name, v)
        s.top_n = max(0, min(10, s.top_n))
        s.max_per_lap = max(0, min(10, s.max_per_lap))
        s.repeat_laps = max(1, min(20, s.repeat_laps))
        s.words_per_s = max(1.0, min(6.0, s.words_per_s))
        s.volume = max(0, min(100, s.volume))
        s.rate = max(-10, min(10, s.rate))
        s.sample_hz = max(5.0, min(60.0, s.sample_hz))
        return s

# ---------------------------------------------------------------- reference

@dataclass
class Corner:
    num: int                    # 1..n along the lap (engine's C-number)
    id: str                     # "C3", as the reviews name it
    name: str                   # spoken name, "T3"
    apex_pct: float
    apex_m: float
    min_kmh: float              # reference minimum speed
    start_rel_m: float          # corner zone, metres from the apex (negative = before it)
    end_rel_m: float
    brake_pct: float | None     # reference brake onset (None: a lift or flat corner)
    brake_m: float | None
    brake_rel_m: float | None   # brake onset, metres from the apex (negative)

    def to_dict(self) -> dict:
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in asdict(self).items()}


@dataclass
class Reference:
    lap_time: float
    length_m: float
    car: dict
    track: dict
    source: dict                # {"kind": "ibt"|"g61"|"live", "file", "lap", "date" (YYYY-MM-DD), ...}
    pct: np.ndarray
    speed: np.ndarray           # m/s on the pct grid
    brake: np.ndarray           # 0..1
    throttle: np.ndarray        # 0..1
    gear: np.ndarray
    time: np.ndarray            # s from the line
    corners: list = field(default_factory=list)

    @property
    def braked(self) -> list:
        return [c for c in self.corners if c.brake_pct is not None]

    def corner(self, num: int):
        return next((c for c in self.corners if c.num == num), None)

    def describe(self) -> str:
        """'1:38.874 from Nov 2'"""
        when = ""
        try:
            d = datetime.strptime(str(self.source.get("date") or "")[:10], "%Y-%m-%d")
            when = f" from {d.strftime('%b')} {d.day}"
        except ValueError:
            pass
        return fmt_lap(self.lap_time) + when

    def summary(self) -> dict:
        """JSON-friendly description for the window (no arrays)."""
        return {"lap_time": round(self.lap_time, 3), "lap_text": fmt_lap(self.lap_time), "text": self.describe(),
                "length_m": round(self.length_m, 1), "car": self.car, "track": self.track, "source": self.source,
                "corners": [c.to_dict() for c in self.corners]}


def fmt_lap(t) -> str:
    if t is None or not math.isfinite(t):
        return "-"
    m, s = divmod(float(t), 60.0)
    return f"{int(m)}:{s:06.3f}" if m >= 1 else f"{s:.3f}"


def _raw_from(cols: dict, n: int) -> dict:
    """engine.parse_csv's raw-lap shape from channel arrays (iRacing names). Missing channels are zero-filled and
    listed in _missing, as parse_csv does."""
    names = {"v": "Speed", "p": "LapDistPct", "b": "Brake", "th": "Throttle", "gear": "Gear", "abs": "ABSActive",
             "la": "LatAccel", "lo": "LongAccel", "st": "SteeringWheelAngle", "rpm": "RPM"}
    raw, missing = {}, []
    for k, nm in names.items():
        a = cols.get(nm)
        if a is None and k == "abs":
            a = cols.get(ABS_IBT)                        # .ibt files call it BrakeABSactive
        if a is None:
            missing.append(nm)
            raw[k] = np.zeros(n)
        else:
            raw[k] = np.asarray(a, float)
    raw["lat"] = np.full(n, np.nan)
    raw["lon"] = np.full(n, np.nan)
    raw["_missing"] = sorted(set(missing))
    return raw


def reference_from_raw(raw: dict, lap_time: float, length_m: float, car: dict | None = None,
                       track: dict | None = None, source: dict | None = None, prefix: str = "T") -> Reference:
    """A Reference from one lap in engine.parse_csv's raw shape (v m/s, p LapDistPct, b/th 0..1, gear, ...).
    Raises ValueError when the lap can't be used."""
    if not lap_time or lap_time <= 0 or not length_m or length_m <= 100:
        raise ValueError("a reference needs a lap time and the track length")
    if len(raw.get("v") if raw.get("v") is not None else []) < 50:
        raise ValueError("too few samples in the lap")
    if engine.coverage(raw) < 0.95:
        raise ValueError("the lap does not cover the whole track")
    if engine.pedals_missing(raw):
        raise ValueError("the lap has no brake or throttle channel")
    R = engine.resample(raw, float(lap_time), float(length_m))
    L = float(length_m)
    pct = np.arange(GRID) / GRID
    d = pct * L
    speed = np.interp(d, R["d"], R["v"]) / engine.MPH
    brake = np.interp(d, R["d"], R["b"]) / 100.0
    thr = np.interp(d, R["d"], R["th"]) / 100.0
    gear = R["gear"][np.clip(np.searchsorted(R["d"], d), 0, len(R["d"]) - 1)]
    tt = np.interp(d, R["d"], R["tt"])
    corners = []
    for k, c in enumerate(engine.detect_corners(R, L=L)):
        m = engine.corner_metrics(R, c) or {}
        apex = float(m.get("min_at", c["apex_m"])) % L
        num = k + 1
        bo = m.get("brake_on")
        brake_m = (float(bo) % L) if bo is not None else None
        brake_rel = _rel_m(brake_m / L, apex / L, L) if brake_m is not None else None
        if brake_rel is not None and brake_rel > 0:      # a brake onset after the slowest point isn't a brake point
            brake_m = brake_rel = None
        corners.append(Corner(num=num, id=f"C{num}", name=f"{prefix}{num}", apex_pct=apex / L, apex_m=apex,
                              min_kmh=float(m.get("min_mph", np.interp(apex, R["d"], R["v"]))) / engine.MPH * KMH,
                              start_rel_m=float(c["start_m"]) - float(c["apex_m"]),
                              end_rel_m=float(c["end_m"]) - float(c["apex_m"]),
                              brake_pct=(brake_m / L) if brake_m is not None else None, brake_m=brake_m,
                              brake_rel_m=brake_rel))
    return Reference(lap_time=float(lap_time), length_m=L, car=dict(car or {}), track=dict(track or {}),
                     source=dict(source or {}), pct=pct, speed=speed, brake=brake, throttle=thr, gear=gear,
                     time=tt, corners=corners)


def reference_from_g61_csv(csv_text: str, lap_time: float, length_m: float, car=None, track=None, lap_id=None,
                           date=None, prefix: str = "T") -> Reference:
    """A Garage 61 lap (Garage61.lap_csv text) as the reference."""
    raw = engine.parse_csv(csv_text)
    return reference_from_raw(raw, lap_time, length_m, car, track,
                              {"kind": "g61", "lap": lap_id, "date": date}, prefix=prefix)


def _ibt_length_m(ib) -> float | None:
    km = ib.track().get("length_km")
    if km and km > 0.1:
        return km * 1000.0
    if ib.has("LapDist"):
        v = ib.col("LapDist")
        return float(np.nanmax(v)) if len(v) else None
    return None


def _ibt_lap_raw(ib, lap: dict) -> dict:
    a, b = lap["start"], lap["end"]
    cols = {}
    for nm in ("Speed", "LapDistPct", "Brake", "Throttle", "Gear", "SteeringWheelAngle", "RPM", "LatAccel",
               "LongAccel", "ABSActive", ABS_IBT):
        if ib.has(nm):
            cols[nm] = ib.col(nm)[a:b]
    return _raw_from(cols, b - a)


def _date_of(ib) -> str:
    try:
        return datetime.fromtimestamp(ib.start_date).strftime("%Y-%m-%d") if ib.start_date > 0 else ""
    except (OverflowError, OSError, ValueError):
        return ""


def best_lap_in(ib) -> dict | None:
    """The fastest clean, fully timed lap in an open .ibt (ibt.laps), or None."""
    clean = [x for x in ibt.laps(ib) if x["clean"] and x["time"]]
    return min(clean, key=lambda x: x["time"]) if clean else None


def build_reference(src, lap: int | None = None, prefix: str = "T", stop=None) -> Reference | None:
    """Your best clean lap as a Reference.

    src: an .ibt path, an open ibt.Ibt, a list of .ibt paths (the fastest clean lap across them), or a list of
    sample dicts covering one lap (as the coach records them). lap: a lap number to use instead of the fastest
    (one file only). stop() returning True ends a list early (the coach is stopping). Returns None when there is no
    usable clean lap. Never raises for a bad file."""
    if isinstance(src, list) and src and isinstance(src[0], dict):
        return reference_from_samples(src, prefix=prefix)
    paths = src if isinstance(src, (list, tuple)) else [src]
    best = None
    for p in paths:
        if stop is not None and stop():
            break
        try:
            if isinstance(p, ibt.Ibt):
                ref = _ref_from_ibt(p, lap, prefix)
            else:
                with ibt.open_ibt(p) as ib:
                    ref = _ref_from_ibt(ib, lap, prefix)
        except Exception as e:  # noqa: BLE001
            _LOG.warning(f"livecoach: no reference from {Path(str(getattr(p, 'path', p))).name}: {e}")
            continue
        if ref is not None and (best is None or ref.lap_time < best.lap_time):
            best = ref
    return best


def _ref_from_ibt(ib, lap: int | None, prefix: str) -> Reference | None:
    if lap is None:
        x = best_lap_in(ib)
    else:
        x = next((y for y in ibt.laps(ib) if y["lap"] == lap and y["time"]), None)
    if x is None:
        return None
    L = _ibt_length_m(ib)
    tr = ib.track()
    return reference_from_raw(_ibt_lap_raw(ib, x), x["time"], L, car=ib.car(),
                              track={"name": tr.get("name"), "code": tr.get("code"), "config": tr.get("config"),
                                     "id": tr.get("id")},
                              source={"kind": "ibt", "file": ib.path.name, "lap": x["lap"], "date": _date_of(ib),
                                      "session": x["session"]}, prefix=prefix)


def reference_from_samples(samples: list, length_m: float | None = None, lap_time: float | None = None,
                           car=None, track=None, source=None, prefix: str = "T") -> Reference | None:
    """A Reference from one lap of live samples (dicts). lap_time defaults to the SessionTime span."""
    if len(samples) < 50:
        return None
    cols = {nm: np.array([float(s.get(nm) or 0.0) for s in samples]) for nm in
            ("Speed", "LapDistPct", "Brake", "Throttle", "Gear", "SteeringWheelAngle")}
    if lap_time is None:
        lap_time = float(samples[-1]["SessionTime"]) - float(samples[0]["SessionTime"])
    if length_m is None:
        length_m = float(max(float(s.get("LapDist") or 0) for s in samples)) or None
    try:
        return reference_from_raw(_raw_from(cols, len(samples)), lap_time, length_m, car, track, source, prefix=prefix)
    except ValueError:
        return None


FRESH_S = 90.0              # a telemetry file changed this recently may still be being written by iRacing: skip it
SCAN_LIMIT = 5              # without Setup Lab's index, only the newest few matching files are opened


def _index_files(index) -> list:
    if isinstance(index, dict):
        return list(index.get("files") or [])
    return list(index or [])


def find_reference(folder, car_path: str, track_code: str, prefix: str = "T", limit: int = SCAN_LIMIT, index=None,
                   stop=None, fresh_s: float | None = None, now: float | None = None) -> Reference | None:
    """Your best clean lap for this car and track from the .ibt files in folder (Documents\\iRacing\\telemetry).

    index: Setup Lab's saved index (setuplab.index()'s dict or its list of records), which already knows each file's
    best clean lap: the file with the fastest one is the only file opened (the next is tried only if that one gives
    no lap). Only files unchanged since they were indexed count. Without an index, or with nothing in it for this
    car + track, the newest `limit` files named '<carpath>_<track code> <date> <time>.ibt' are read instead.
    Files changed in the last FRESH_S seconds are skipped (iRacing may still be writing them), and stop() returning
    True ends the search between files."""
    folder = Path(folder) if folder else None
    if not folder or not folder.is_dir() or not car_path:
        return None
    fresh = FRESH_S if fresh_s is None else fresh_s
    now = time.time() if now is None else now
    cp, tc = str(car_path).lower(), str(track_code or "").lower()

    def settled(p: Path, size=None, mtime_ns=None) -> bool:
        try:
            st = p.stat()
        except OSError:
            return False
        if size is not None and (st.st_size != size or st.st_mtime_ns != mtime_ns):
            return False                                   # changed since Setup Lab read it
        return now - st.st_mtime >= fresh

    cand = []
    for r in _index_files(index):
        if not isinstance(r, dict) or not r.get("ok", True) or not isinstance(r.get("best"), (int, float)):
            continue
        if str((r.get("car") or {}).get("path") or "").lower() != cp:
            continue
        if tc and str((r.get("track") or {}).get("code") or "").lower() != tc:
            continue
        cand.append((float(r["best"]), str(r.get("file") or ""), r))
    cand.sort(key=lambda x: (x[0], x[1]))
    for _best, name, r in cand[:max(1, limit)]:
        if stop is not None and stop():
            return None
        p = folder / name
        if Path(name).name != name or not settled(p, r.get("size"), r.get("mtime")):
            continue
        ref = build_reference(p, prefix=prefix)
        if ref is not None:
            return ref
    want = f"{car_path}_{track_code}".lower() if track_code else f"{car_path}_".lower()
    try:
        files = [p for p in folder.glob("*.ibt") if p.name.lower().startswith(want) and settled(p)]
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    return build_reference(files[:limit], prefix=prefix, stop=stop) if files else None


# ---------------------------------------------------------------- weak corners

@dataclass
class Weak:
    corner: Corner
    lost_s: float | None
    name: str                   # spoken name
    review_id: str | None = None   # what the race review calls it ("C2"), when the weak corner came from one
    phrase: str | None = None   # the corner coach's short advice ("T4: lighter brake"), said after the corner
    source: str | None = None   # "corner coach" when it came from the corner coach (then there's no review_id)
    rule: str | None = None     # the corner coach's rule behind the phrase ("brake_later" ...), when given


POINT_RULES = {"brake later": "brake_later", "brake earlier": "brake_earlier"}


def phrase_rule(w: "Weak") -> str | None:
    """The rule behind a weak corner's phrase: its own rule, else read from the words ("brake later")."""
    if w.rule:
        return w.rule
    low = str(w.phrase or "").lower()
    return next((r for words, r in POINT_RULES.items() if words in low), None)


def phrase_fits(w: "Weak", early_m: float, early_min: float) -> bool:
    """2.13 review: a brake-point phrase is only said when this lap's error agrees with it. "Brake later" only
    after braking early by early_min or more on this lap (never after a late-braking overshoot); "brake earlier"
    only after braking late by that much. Other phrases always fit."""
    r = phrase_rule(w)
    if r == "brake_later":
        return early_m >= early_min
    if r == "brake_earlier":
        return early_m <= -early_min
    return True


def _rel_m(pct: float, apex_pct: float, L: float) -> float:
    """Signed distance (m) from the apex to pct around the loop, in (-L/2, L/2]."""
    return (((pct - apex_pct) + 0.5) % 1.0 - 0.5) * L


def _num_of(x) -> int | None:
    m = re.fullmatch(r"\s*[CTct](?:urn\s*)?(\d+)\s*", str(x or ""))
    return int(m.group(1)) if m else None


def match_weak(ref: Reference, weak, top_n: int = 3, prefix: str = "T") -> list:
    """Weak corners (prep.weak_corners rows or similar) -> the reference's corners, biggest time loss first.

    An item may be a dict with apex_m (or pct) and avg_avail / avail / lost_s / time_lost, a corner number (int),
    or a name ("C3"/"T3"). Positions are matched by apex within max(60 m, 2% of the lap), as prep.match_corners does.
    The spoken name follows the review's C-number when the item has one, so the voice says what the review shows."""
    if not ref or not ref.corners or not weak:
        return []
    L = ref.length_m
    tol = max(60.0, 0.02 * L)
    out, used = [], set()
    for order, w in enumerate(weak):
        c, lost, num, phrase, source, rule = None, None, None, None, None, None
        if isinstance(w, dict):
            num = _num_of(w.get("id"))
            rule = w.get("rule") if isinstance(w.get("rule"), str) and len(w["rule"]) <= 30 else None
            source = w.get("source") if w.get("source") == "corner coach" else None
            ph = w.get("phrase")
            if isinstance(ph, str) and ph.strip() and len(ph.strip()) <= MAX_PHRASE:
                phrase = " ".join(ph.split())
            for k in ("avg_avail", "avail", "lost_s", "time_lost"):
                v = w.get(k)
                if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
                    lost = float(v)
                    break
            pos = None
            if isinstance(w.get("apex_m"), (int, float)):
                pos = float(w["apex_m"]) % L / L
            elif isinstance(w.get("pct"), (int, float)):
                pos = float(w["pct"]) % 1.0
            if pos is not None:
                best = min(ref.corners, key=lambda cc: abs(_rel_m(pos, cc.apex_pct, L)))
                if abs(_rel_m(pos, best.apex_pct, L)) <= tol:
                    c = best
            elif num is not None:
                c = ref.corner(num)
        elif isinstance(w, int) and not isinstance(w, bool):
            c, num = ref.corner(w), w
        else:
            num = _num_of(w)
            c = ref.corner(num) if num is not None else None
        if c is None or c.num in used:
            continue
        used.add(c.num)
        out.append((order, Weak(corner=c, lost_s=lost, name=f"{prefix}{num if num is not None else c.num}",
                                review_id=f"C{num}" if num is not None and source is None else None,
                                phrase=phrase, source=source, rule=rule if phrase else None)))
    # biggest time loss first; items without a number keep their given order, after those with one
    out.sort(key=lambda x: (x[1].lost_s is None, -(x[1].lost_s or 0.0), x[0]))
    return [w for _o, w in out][:max(0, top_n)]


# ---------------------------------------------------------------- cues

@dataclass
class Cue:
    text: str
    priority: int
    key: str                    # e.g. "marker:C3", "feedback:C3", "lap"
    t: float = 0.0              # session time it was made
    kind: str = ""
    corner: str | None = None   # corner id ("C3") it is about
    expires: float = 0.0        # session time after which it is dropped, not spoken
    spoken_t: float | None = None   # session time it was handed to the voice

    def to_dict(self) -> dict:
        return {"t": round(self.spoken_t if self.spoken_t is not None else self.t, 2), "made": round(self.t, 2),
                "text": self.text, "kind": self.kind, "priority": self.priority, "corner": self.corner}


class CueQueue:
    """Waiting cues, most important first. A cue past its expiry is dropped, never spoken late."""

    def __init__(self):
        self.items: list = []
        self.dropped: list = []

    def push(self, cue: Cue):
        self.items = [c for c in self.items if c.key != cue.key] + [cue]

    def pop(self, now: float, allowed=lambda c: True) -> Cue | None:
        keep = []
        for c in self.items:
            if now > c.expires:
                self.dropped.append(c)
            else:
                keep.append(c)
        del self.dropped[:-20]
        self.items = keep
        for c in sorted(keep, key=lambda c: (-c.priority, c.t)):
            if allowed(c):
                self.items.remove(c)
                return c
        return None

    def clear(self):
        self.items = []


_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"]


def delta_words(d: float) -> str:
    """0.24 -> 'two tenths', 0.1 -> 'a tenth', 1.36 -> '1.4 seconds'."""
    a = abs(d)
    tenths = int(round(a * 10 + 1e-9))
    if tenths >= 10:
        return f"{a:.1f} seconds"
    if tenths == 1:
        return "a tenth"
    return f"{_WORDS[tenths]} tenths"


def is_race(stype: str) -> bool:
    return "race" in str(stype or "").lower()


# ---------------------------------------------------------------- the coach

class _Lap:
    def __init__(self, num: int, t0: float | None, valid: bool, inc: int):
        self.num = num
        self.t0 = t0                    # crossing time (None: not seen from its start)
        self.valid = valid and t0 is not None
        self.pit = False
        self.off = False
        self.inc0 = inc
        self.incident = False
        self.trace: list = []
        self.cues = 0                   # corrective cues said this lap
        self.corners: set = set()       # corner numbers cued this lap


class _Obs:
    """One pass through a weak corner."""
    def __init__(self, lap_ok: bool):
        self.ok = lap_ok
        self.min_v = 1e9
        self.brake_rel = None
        self.prev_brake = None
        self.off = False
        self.traffic = False
        self.n = 0


class _Pending:
    """A finished lap waiting for iRacing's own time (LapLastLapTime updates about 2 s after the line)."""
    def __init__(self, lap: _Lap, tc: float, measured: float, clean: bool, last0):
        self.lap, self.tc, self.measured, self.clean, self.last0 = lap, tc, measured, clean, last0


class Coach:
    """feed(sample) -> [Cue] (none or one; a 'beep' cue when Settings.brake_beep is on). Independent of where the
    samples come from. After feed, take_cancel() is True when what is being said must stop now (car alongside).

    duration(text) -> seconds: how long a cue takes to say (the voice's pre-rendered length when it has one,
    else words / Settings.words_per_s). Every cue must end Settings.end_before_brake_s before the next brake point."""

    def __init__(self, reference: Reference | None = None, weak_corners=None, settings: Settings | None = None,
                 log=None, duration=None):
        self.settings = settings or Settings()
        self._log = log or _LOG.warning
        self.duration_fn = duration
        self.weak_input = list(weak_corners or [])
        self.reference = None
        self.weak: list = []
        self.queue = CueQueue()
        self.spoken: list = []
        self.events: list = []          # notable non-spoken events ("reference updated", ...)
        self.quiet_reason: str | None = None
        self.last_lap: dict | None = None
        self.lap_log: list = []          # the last laps seen: lap, time, clean, delta, timed_by
        self._cancel = False
        self._corner_err: dict = {}      # corner num -> [error on the last pass, error on the pass before]
        self._corner_last: dict = {}     # corner num -> (lap number, kind) of its last cue
        self.focus = None                # 2.14: the practice plan's focus corner (practiceplan.LiveFocus), optional
        self._reset_all()
        if reference is not None:
            self.set_reference(reference)

    # ---- public ----
    def set_reference(self, ref: Reference | None, weak_corners=None):
        if weak_corners is not None:
            self.weak_input = list(weak_corners)
        self.reference = ref
        self.weak = match_weak(ref, self.weak_input, self.settings.top_n, self.settings.corner_prefix) if ref else []
        self._obs = {}
        self._marker_done = set()
        self._beep_done = set()

    def set_focus(self, focus):
        """2.14: the practice plan's "one thing this session" (practiceplan.LiveFocus or None). After each clean pass
        through its corner it may give a short progress cue ("Good, 84"), queued as that corner's feedback so every
        limit above (gap, per lap, per corner, silence rules) applies to it."""
        self.focus = focus

    def mute(self):
        self.settings.muted = True
        self.queue.clear()
        self._cancel = True

    def unmute(self):
        self.settings.muted = False

    def toggle(self) -> bool:
        if self.settings.muted:
            self.unmute()
        else:
            self.mute()
        return self.settings.muted

    def take_cancel(self) -> bool:
        c, self._cancel = self._cancel, False
        return c

    def duration(self, text: str) -> float:
        if self.duration_fn is not None:
            try:
                d = self.duration_fn(text)
                if d:
                    return float(d)
            except Exception:  # noqa: BLE001
                pass
        return max(0.6, len(str(text).split()) / self.settings.words_per_s)

    def vocabulary(self) -> list:
        """Every fixed phrase this coach can say with the current reference and weak corners, for the voice to
        render ahead of time (no speech synthesis while iRacing is running)."""
        out = []
        for w in self.weak:
            if w.corner.brake_pct is not None:
                out += [marker_text(w.name), early_text(w.name)]
            out += [speed_text(w.name, n) for n in range(max(1, int(math.ceil(self.settings.slower_kmh))), 16)]
            if w.phrase:
                out.append(w.phrase)
        for k in range(1, 10):
            out += [f"Down {delta_words(k / 10)}", f"New best, up {delta_words(k / 10)}"]
        out.append("New best")
        if self.focus is not None:                       # 2.14 practice focus cues
            try:
                out += list(self.focus.vocabulary())
            except Exception:  # noqa: BLE001
                pass
        return list(dict.fromkeys(out))

    def status(self) -> dict:
        r = self.reference
        return {"reference": r.summary() if r else None,
                "weak": [{"name": w.name, "id": w.corner.id, "review_id": w.review_id, "lost_s": w.lost_s,
                          "apex_m": round(w.corner.apex_m),
                          "brake_m": round(w.corner.brake_m) if w.corner.brake_m is not None else None,
                          "phrase": w.phrase, "source": w.source} for w in self.weak],
                "muted": self.settings.muted, "quiet_reason": self.quiet_reason,
                "last_lap": self.last_lap, "spoken": [c.to_dict() for c in self.spoken[-10:]],
                "events": self.events[-10:],
                "focus": self._focus_status()}

    def _focus_status(self):
        try:
            return self.focus.status() if self.focus is not None else None
        except Exception:  # noqa: BLE001
            return None

    def feed(self, s: dict) -> list:
        try:
            return self._feed(s)
        except Exception as e:  # noqa: BLE001  (a bad sample never stops the coach)
            self._log(f"livecoach: sample skipped ({e.__class__.__name__}: {str(e)[:120]})")
            return []

    # ---- internals ----
    def _reset_all(self):
        self._t = None
        self._sn = None
        self._lap: _Lap | None = None
        self._prev_pct = None
        self._prev_t = None
        self._traffic_until = -1e9
        self._alongside = False
        self._last_spoken_t = -1e9
        self._obs = {}
        self._marker_done = set()
        self._beep_done = set()
        self._pending: _Pending | None = None
        self.queue.clear()

    def _need_s(self, text: str) -> float:
        st = self.settings
        return self.duration(text) + st.end_before_brake_s + st.audio_latency_s

    def _feed(self, s: dict) -> list:
        st = self.settings
        t = s.get("SessionTime")
        pct = s.get("LapDistPct")
        if t is None or pct is None:
            return []
        t, pct = float(t), float(pct)
        sn = s.get("SessionNum")
        if (self._t is not None and (t < self._t - 1.0 or t - self._t > 30.0)) or sn != self._sn:
            self._reset_all()            # new session, a replay jump or a long gap: start over
            self._sn = sn
        self._t = t
        stype = str(s.get("SessionType") or "")
        lapn = int(s.get("Lap") if s.get("Lap") is not None else -1)
        v = float(s.get("Speed") or 0.0)
        brk = float(s.get("Brake") or 0.0)
        pit = bool(s.get("OnPitRoad") or False)
        surf = s.get("PlayerTrackSurface")
        surf = ibt.ON_TRACK if surf is None else int(surf)
        on = s.get("IsOnTrack")
        on = True if on is None else bool(on)
        clr = s.get("CarLeftRight")
        clr = 0 if clr is None else int(clr)
        inc = int(s.get("PlayerCarMyIncidentCount") or 0)
        last_lap_time = s.get("LapLastLapTime")
        if pct < 0 or pct > 1.0001:                      # not in the world
            self._prev_pct = None
            return []
        alongside = clr not in (0, 1)
        if alongside:
            if not self._alongside:                      # a car just came alongside: stop talking, forget queued
                self._cancel = True
            self.queue.clear()
            self._traffic_until = t + st.traffic_hold_s  # quiet until 2 s after it clears
        self._alongside = alongside
        on_track = on and surf == ibt.ON_TRACK and not pit

        # ---- the line ----
        lap = self._lap
        if lap is None or lapn != lap.num:
            crossed = (lap is not None and lapn == lap.num + 1 and self._prev_pct is not None
                       and self._prev_pct > 0.9 and pct < 0.1)
            tc = None
            if crossed:
                gap = (1.0 - self._prev_pct) + pct
                frac = (1.0 - self._prev_pct) / gap if gap > 0 else 1.0
                tc = self._prev_t + frac * (t - self._prev_t)
                self._end_lap(lap, tc, stype, last_lap_time)
            self._lap = lap = _Lap(lapn, tc, valid=crossed and on_track, inc=inc)
        if pit or surf in (ibt.IN_PIT_STALL, ibt.APPROACHING_PITS):
            lap.pit = True
        if surf == ibt.OFF_TRACK or not on:
            lap.off = True
        if inc != lap.inc0:
            lap.incident = True
        if lap.valid and not lap.pit and len(lap.trace) < 20000:
            lap.trace.append((t, pct, v, brk, float(s.get("Throttle") or 0.0), float(s.get("Gear") or 0.0)))
        if self._pending is not None:
            self._lap_result(t, last_lap_time)
        start_lap = is_race(stype) and lapn <= 1
        coachable = lap.valid and not lap.pit and not start_lap

        # ---- corners ----
        ref = self.reference
        L = ref.length_m if ref else 0.0
        dists = {}                                        # corner num -> metres to its brake point (ahead only)
        busy = False                                      # inside a corner's braking or apex zone
        beep = None
        if ref:
            for c in ref.corners:
                rel = _rel_m(pct, c.apex_pct, L)
                lo = c.brake_rel_m if c.brake_rel_m is not None else min(-60.0, 0.5 * c.start_rel_m)
                if lo <= rel <= max(30.0, 0.3 * c.end_rel_m):
                    busy = True
                if c.brake_pct is not None:
                    dist = ((c.brake_pct - pct) % 1.0) * L
                    if dist < L / 2:
                        dists[c.num] = dist
            for w in self.weak:
                self._observe(w, pct, v, brk, t, alongside, surf, on, coachable, lap)
                self._marker(w, dists, v, t, coachable, lap)
                b = self._beep(w, dists, v, coachable)
                beep = beep or b

        # ---- 2.14 practice focus: one corner measured each clean pass ----
        if self.focus is not None:
            ok = coachable and on_track and not alongside and t >= self._traffic_until
            try:
                said = self.focus.feed(s, ok=ok, lap=lapn)
            except Exception as e:  # noqa: BLE001
                said = None
                if not getattr(self, "_focus_logged", False):     # once, not 20 times a second
                    self._focus_logged = True
                    self._log(f"livecoach: practice focus skipped ({e.__class__.__name__}: {str(e)[:120]})")
            if said and st.feedback:
                cid = str(getattr(self.focus, "corner_id", "") or "")
                if re.fullmatch(r"C\d+", cid):
                    self.queue.push(Cue(text=said, priority=PRIORITY[KIND_FEEDBACK], key=f"feedback:{cid}", t=t,
                                        kind=KIND_FEEDBACK, corner=cid, expires=t + st.stale_s))

        # ---- speak? ----
        self._prev_pct, self._prev_t = pct, t
        reason = self._quiet(st, stype, on_track, alongside, t, coachable, start_lap, lap)
        self.quiet_reason = reason
        if st.muted:
            self.queue.clear()
            return []
        if beep is not None and reason is None:
            return [Cue(text="", priority=0, key=f"beep:{beep}", t=t, kind=KIND_BEEP, corner=beep, spoken_t=t)]
        vv = max(v, 5.0)

        def allowed(c: Cue) -> bool:
            if reason and not (c.kind == KIND_LAP and reason in ("Out lap or in lap", "Lap not seen from the start")):
                return False
            if t - self._last_spoken_t < st.gap_s:
                return False
            if brk > st.braking_pedal or busy:            # never under braking or mid-corner
                return False
            need = self._need_s(c.text)
            for num, dist in dists.items():              # must END end_before_brake_s before every brake point
                if dist / vv < need:
                    return False
            if c.kind in (KIND_MARKER, KIND_FEEDBACK):
                num = int(c.corner[1:]) if c.corner else None
                if lap.cues >= st.max_per_lap or num in lap.corners:
                    return False
            if c.kind != KIND_MARKER and st.markers and coachable:   # keep room for a weak corner's focus cue
                for w in self.weak:
                    d = dists.get(w.corner.num)
                    if d is None or w.corner.num in self._marker_done or not self._marker_wanted(w, lap):
                        continue
                    trig, _late = self._marker_span(w, vv)
                    if d - trig < vv * (st.gap_s + need):
                        return False
            return True

        cue = self.queue.pop(t, allowed)
        if cue is None:
            return []
        self._last_spoken_t = t
        cue.spoken_t = t
        if cue.kind in (KIND_MARKER, KIND_FEEDBACK):
            num = int(cue.corner[1:])
            lap.cues += 1
            lap.corners.add(num)
            self._corner_last[num] = (lap.num, cue.kind)
        self.spoken.append(cue)
        del self.spoken[:-50]
        return [cue]

    def _quiet(self, st, stype, on_track, alongside, t, coachable, start_lap, lap) -> str | None:
        if st.muted:
            return "Muted"
        if is_race(stype) and not st.in_races:
            return "Race session (coaching in races is off)"
        if self.reference is None:
            return "No reference lap yet"
        if not on_track:
            return "On pit road" if lap.pit else "Off track"
        if alongside or t < self._traffic_until:
            return "Car alongside"
        if start_lap:
            return "Race start lap"
        if lap.t0 is None:
            return "Lap not seen from the start"
        if not coachable:
            return "Out lap or in lap"
        return None

    # -- the focus cue (a) --
    def _marker_span(self, w: Weak, v: float):
        """(distance at which the focus cue is made, distance under which it is too late) before the brake point."""
        st = self.settings
        c = w.corner
        vref = max(v, float(np.interp(c.brake_pct, self.reference.pct, self.reference.speed, period=1.0)))
        late = max(st.marker_min_m, vref * self._need_s(marker_text(w.name)))
        return late + vref * st.marker_window_s, late

    def _marker_wanted(self, w: Weak, lap: _Lap) -> bool:
        """Nothing when he is doing well: a focus cue only before the first look at the corner or while the last
        pass there was off the reference; and not twice running for the same corner (then the feedback gets the
        turn), not within repeat_laps of its last cue unless the error persists."""
        n = w.corner.num
        err = self._corner_err.get(n)
        if err is not None and not err[0]:
            return False
        last = self._corner_last.get(n)
        if last is not None:
            gap = lap.num - last[0]
            if last[1] == KIND_MARKER and gap <= 1 and err is not None:
                return False
            if gap < self.settings.repeat_laps and not (err and err[0]):
                return False
        return True

    def _marker(self, w: Weak, dists: dict, v: float, t: float, coachable: bool, lap: _Lap):
        st = self.settings
        c = w.corner
        if c.brake_pct is None:
            return
        d = dists.get(c.num)
        if d is None:                            # behind us: ready for next lap
            self._marker_done.discard(c.num)
            return
        if not st.markers or not coachable or c.num in self._marker_done:
            return
        vv = max(v, 5.0)
        trig, late = self._marker_span(w, vv)
        if late <= d <= trig:
            self._marker_done.add(c.num)
            if self._marker_wanted(w, lap):
                self.queue.push(Cue(text=marker_text(w.name), priority=PRIORITY[KIND_MARKER],
                                    key=f"marker:{c.id}", t=t, kind=KIND_MARKER, corner=c.id,
                                    expires=t + (d - late) / vv))

    def _beep(self, w: Weak, dists: dict, v: float, coachable: bool):
        c = w.corner
        if not self.settings.brake_beep or c.brake_pct is None:
            return None
        d = dists.get(c.num)
        if d is None or d > 50.0:
            self._beep_done.discard(c.num)
            return None
        if d > max(v, 5.0) * 0.06 + 1.0:                 # not yet within one sample of the brake point
            return None
        if c.num in self._beep_done or not coachable:
            return None
        self._beep_done.add(c.num)
        return c.id

    # -- after the corner (b) --
    def _observe(self, w: Weak, pct, v, brk, t, alongside, surf, on, coachable, lap: _Lap):
        st = self.settings
        c = w.corner
        L = self.reference.length_m
        rel = _rel_m(pct, c.apex_pct, L)
        start = min(c.start_rel_m, (c.brake_rel_m - 60.0) if c.brake_rel_m is not None else c.start_rel_m)
        inside = start <= rel <= c.end_rel_m
        o = self._obs.get(c.num)
        if inside:
            if o is None:
                o = self._obs[c.num] = _Obs(coachable)
            o.n += 1
            o.ok = o.ok and coachable
            o.min_v = min(o.min_v, v)
            if brk > BRAKE_ON and o.prev_brake is not None and o.prev_brake <= BRAKE_ON and o.brake_rel is None \
                    and rel <= 40.0:
                o.brake_rel = rel
            o.prev_brake = brk
            if surf == ibt.OFF_TRACK or not on:
                o.off = True
            if alongside or t < self._traffic_until:
                o.traffic = True
            return
        if o is None:
            return
        del self._obs[c.num]
        if rel < c.end_rel_m or rel > c.end_rel_m + 200.0:   # left backwards or jumped (reset/tow): no verdict
            return
        if not o.ok or o.off or o.traffic or o.n < 5:
            return                                        # a pass that says nothing about his driving
        down = c.min_kmh - o.min_v * KMH
        early = (c.brake_rel_m - o.brake_rel) if (c.brake_rel_m is not None and o.brake_rel is not None) else 0.0
        # early > 0: braked before the reference's brake point; < 0: braked later (an overshoot)
        err = down >= st.slower_kmh or early >= st.early_m
        prev = self._corner_err.get(c.num)
        self._corner_err[c.num] = [err, bool(prev and prev[0])]
        if not err or not st.feedback:
            return
        last = self._corner_last.get(c.num)
        persists = bool(prev and prev[0])
        if last is not None and lap.num - last[0] < st.repeat_laps and not persists:
            return
        if w.phrase and phrase_fits(w, early, st.early_m):   # the corner coach's advice for this corner (2.13)
            text = w.phrase
        else:
            text = speed_text(w.name, int(round(down))) if down >= st.slower_kmh else early_text(w.name)
        self.queue.push(Cue(text=text, priority=PRIORITY[KIND_FEEDBACK], key=f"feedback:{c.id}", t=t,
                            kind=KIND_FEEDBACK, corner=c.id, expires=t + st.stale_s))

    # -- at the line (c) --
    def _end_lap(self, lap: _Lap, tc: float, stype: str, last_lap_time):
        start_lap = is_race(stype) and lap.num <= 1
        clean = lap.valid and not (lap.pit or lap.off or lap.incident or start_lap)
        lt = (tc - lap.t0) if lap.t0 is not None else None
        self.last_lap = {"lap": lap.num, "time": round(lt, 3) if lt else None, "clean": clean, "delta": None}
        self.lap_log.append(self.last_lap)
        del self.lap_log[:-30]
        self._pending = _Pending(lap, tc, lt, clean, last_lap_time) if (clean and lt and lt > 0) else None

    def _lap_result(self, t: float, last_lap_time):
        """iRacing's own lap time once LapLastLapTime has updated (about 2 s after the line); the time measured from
        the samples if it hasn't within 4 s (or the source has no such channel)."""
        p = self._pending
        official = None
        if last_lap_time is not None and p.last0 is not None and abs(float(last_lap_time) - float(p.last0)) > 1e-6:
            official = float(last_lap_time) if float(last_lap_time) > 0 else None
            if official is not None and abs(official - p.measured) > max(2.0, 0.05 * p.measured):
                official = None                          # not this lap's time
        elif last_lap_time is not None and t - p.tc < 4.0:
            return                                       # still waiting for iRacing
        self._pending = None
        lt = official if official is not None else p.measured
        self.last_lap["time"] = round(lt, 3)
        self.last_lap["timed_by"] = "iracing" if official is not None else "samples"
        st = self.settings
        ref = self.reference
        if ref is not None and abs(lt - ref.lap_time) > max(5.0, 0.15 * ref.lap_time):
            return                                        # not a real lap of this track (cut, reset, wrong reference)
        if ref is not None:
            d = lt - ref.lap_time
            self.last_lap["delta"] = round(d, 3)
            text = None
            if d < 0:
                text = "New best" + (f", up {delta_words(d)}" if -d >= st.delta_min_s - 1e-9 else "")
            elif st.delta_min_s - 1e-9 <= d <= st.lap_delta_max_s:
                text = f"Down {delta_words(d)}"
            if text and st.lap_delta:
                self.queue.push(Cue(text=text, priority=PRIORITY[KIND_LAP], key="lap", t=t, kind=KIND_LAP,
                                    expires=t + st.lap_cue_wait_s))
        if (ref is None or lt < ref.lap_time) and st.update_reference:
            self._adopt(p.lap, lt)

    def _adopt(self, lap: _Lap, lt: float):
        ref = self.reference
        tr = lap.trace
        if len(tr) < 50:
            return
        a = np.array(tr)
        cols = {"Speed": a[:, 2], "LapDistPct": a[:, 1], "Brake": a[:, 3], "Throttle": a[:, 4], "Gear": a[:, 5]}
        L = ref.length_m if ref else (self._length_hint or None)
        if not L:
            return
        try:
            new = reference_from_raw(_raw_from(cols, len(a)), lt, L, car=ref.car if ref else self._car_hint,
                                     track=ref.track if ref else self._track_hint,
                                     source={"kind": "live", "lap": lap.num, "date": datetime.now().strftime("%Y-%m-%d")},
                                     prefix=self.settings.corner_prefix)
        except ValueError as e:
            self.events.append({"t": lap.t0, "text": f"new best lap not used as reference ({e})"})
            return
        if ref is not None and len(new.braked) < max(1, len(ref.braked) - 2):
            self.events.append({"t": lap.t0, "text": "new best lap not used as reference (corners did not match)"})
            return
        self.set_reference(new)
        self.events.append({"t": lap.t0, "text": f"reference updated to {fmt_lap(lt)} (lap {lap.num})"})

    _length_hint = None
    _car_hint = None
    _track_hint = None

    def hint(self, length_m=None, car=None, track=None):
        """Track length / car / track for a reference made from a live lap when there was none before."""
        self._length_hint, self._car_hint, self._track_hint = length_m, car, track


def marker_text(name: str) -> str:
    return f"Brake marker, {name}"


def early_text(name: str) -> str:
    return f"{name}, braked early"


def speed_text(name: str, kmh_down: int) -> str:
    return f"{name}: more speed, {kmh_down} down"

# ---------------------------------------------------------------- 2.14 one corner from live samples

def corner_phases(samples, corner, length_m) -> dict | None:
    """The corner coach's phase numbers (cornercoach.phases: brake point, peak %, trail %, coasting, throttle
    pick-up, minimum speed ...) for ONE pass through one corner, from the live samples of that pass (dicts with
    SessionTime, LapDistPct, Speed, Brake, Throttle and, when there, SteeringWheelAngle and Gear). corner: a dict
    with apex_m, start_m, end_m in metres from the line, as the corner coach's report gives them. None when the
    samples don't cover the whole corner or can't be read."""
    try:
        rows = [x for x in samples or [] if isinstance(x, dict) and x.get("LapDistPct") is not None
                and x.get("SessionTime") is not None]
        if len(rows) < 5:
            return None
        cols = {nm: [float(x.get(nm) or 0.0) for x in rows]
                for nm in ("SessionTime", "LapDistPct", "Speed", "Brake", "Throttle")}
        if all(x.get("SteeringWheelAngle") is not None for x in rows):
            cols["SteeringWheelAngle"] = [float(x["SteeringWheelAngle"]) for x in rows]
        if all(x.get("Gear") is not None for x in rows):
            cols["Gear"] = [float(x["Gear"]) for x in rows]
        from . import cornercoach
        return cornercoach.phases_window(cols, corner, length_m)
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- sample sources

# only what the coach reads (each live read copies these out of iRacing's buffer)
CHANNELS = ("SessionTime", "SessionNum", "Lap", "LapDistPct", "LapLastLapTime", "Speed", "Brake", "Throttle",
            "Gear", "OnPitRoad", "PlayerTrackSurface", "CarLeftRight", "IsOnTrack", "PlayerCarMyIncidentCount",
            "SteeringWheelAngle")       # 2.14: steering, for the practice focus's trail-braking number


class IbtPlaybackSource:
    """Plays an .ibt file as if it were live. speed 1.0 = real time, 0 = as fast as possible (tests).
    iRacing does not write CarLeftRight into .ibt files (checked on the owner's files), so playback never shows
    a car alongside unless the file has the channel."""

    def __init__(self, path, speed: float = 1.0, rate_hz: float = 20.0, start_s: float = 0.0):
        self.path = Path(path)
        self.speed = float(speed)
        self.done = False
        self.state = "playing"
        with ibt.open_ibt(self.path) as ib:
            self.tick = ib.tick_rate or 60
            self._n = ib.record_count
            self._cols = {nm: ib.col(nm) for nm in CHANNELS if ib.has(nm)}
            self._types = ib.session_types()
            tr = ib.track()
            self._info = {"car": ib.car(), "track": {"name": tr.get("name"), "code": tr.get("code"),
                                                     "config": tr.get("config"), "id": tr.get("id")},
                          "length_m": _ibt_length_m(ib), "driver": (ib.player() or {}).get("UserName")}
        self.step = max(1, int(round(self.tick / max(1.0, rate_hz))))
        self.interval = (self.step / self.tick) / self.speed if self.speed > 0 else 0.0
        self.realtime = self.speed > 0
        self._i = max(0, int(start_s * self.tick))

    def info(self) -> dict:
        return self._info

    def read(self) -> dict | None:
        if self._i >= self._n:
            self.done = True
            self.state = "finished"
            return None
        i = self._i
        self._i += self.step
        s = {}
        for nm, a in self._cols.items():
            x = a[i]
            s[nm] = x.item() if hasattr(x, "item") else x
        s["SessionType"] = self._types.get(int(s.get("SessionNum") or 0), "")
        return s

    def close(self):
        self._cols = {}


class IrsdkSource:
    """Live iRacing through pyirsdk (imported only here). read() returns None while iRacing isn't running (and tries
    to reconnect every couple of seconds); state is 'not_installed', 'not_running' or 'connected'."""

    def __init__(self, rate_hz: float = 20.0, retry_s: float = 2.0):
        self.interval = 1.0 / max(1.0, rate_hz)
        self.realtime = True
        self.done = False
        self.state = "not_running"
        self.error = ""
        self._ir = None
        self._retry_s = retry_s
        self._next_try = 0.0
        self._types: dict = {}
        self._info: dict | None = None
        self._info_t = 0.0

    def _connect(self) -> bool:
        if self._ir is None:
            try:
                import irsdk  # pyirsdk: only on the PC
            except Exception as e:  # noqa: BLE001
                self.state, self.error = "not_installed", f"pyirsdk is not available ({e.__class__.__name__})"
                return False
            try:
                self._ir = irsdk.IRSDK()
            except Exception as e:  # noqa: BLE001
                self.state, self.error = "not_running", str(e)[:120]
                return False
        try:
            if self._ir.is_initialized and self._ir.is_connected:
                self.state = "connected"
                return True
            now = time.monotonic()
            if now < self._next_try:
                return False
            self._next_try = now + self._retry_s
            try:
                self._ir.shutdown()
            except Exception:  # noqa: BLE001
                pass
            ok = bool(self._ir.startup()) and bool(self._ir.is_connected)
        except Exception as e:  # noqa: BLE001
            self.state, self.error = "not_running", str(e)[:120]
            return False
        self.state = "connected" if ok else "not_running"
        if ok:
            self._info, self._types = None, {}
        return ok

    def _get(self, key):
        try:
            return self._ir[key]
        except Exception:  # noqa: BLE001
            return None

    def _load_info(self):
        di = self._get("DriverInfo") or {}
        me = next((d for d in di.get("Drivers") or [] if d.get("CarIdx") == di.get("DriverCarIdx")), {})
        wi = self._get("WeekendInfo") or {}
        self._types = {s.get("SessionNum"): s.get("SessionType") or s.get("SessionName") or ""
                       for s in (self._get("SessionInfo") or {}).get("Sessions") or []}
        km = ibt.number(wi.get("TrackLength"))
        self._info = {"car": {"name": me.get("CarScreenName") or "", "path": me.get("CarPath") or "",
                              "id": me.get("CarID"), "class": me.get("CarClassShortName") or ""},
                      "track": {"name": wi.get("TrackDisplayName") or wi.get("TrackName") or "",
                                "code": wi.get("TrackName") or "", "config": wi.get("TrackConfigName") or "",
                                "id": wi.get("TrackID")},
                      "length_m": km * 1000.0 if km else None, "driver": me.get("UserName")}
        self._info_t = time.monotonic()

    def info(self) -> dict | None:
        return self._info

    def read(self) -> dict | None:
        if not self._connect():
            return None
        ir = self._ir
        try:
            ir.freeze_var_buffer_latest()
            try:
                s = {nm: self._get(nm) for nm in CHANNELS}
            finally:
                ir.unfreeze_var_buffer_latest()
            if self._info is None or time.monotonic() - self._info_t > 5.0 or s.get("SessionNum") not in self._types:
                self._load_info()
        except Exception as e:  # noqa: BLE001
            self.state, self.error = "not_running", str(e)[:120]
            return None
        if s.get("SessionTime") is None:
            return None
        s["SessionType"] = self._types.get(s.get("SessionNum"), "")
        return s

    def close(self):
        try:
            if self._ir is not None:
                self._ir.shutdown()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- the runner

def short_track(name: str) -> str:
    try:
        from . import library
        return library.short_track_name(name)
    except Exception:  # noqa: BLE001
        return name or ""


class LiveRunner:
    """Reads a source on a background thread, feeds the Coach, speaks the cues.

        r = LiveRunner(IrsdkSource(), make_voice(), settings=Settings(), telemetry_dir=..., weak_for=fn)
        r.start(); ... r.status(); r.toggle(); ... r.stop()

    reference: a Reference to use (only while its car/track match the live car/track, when both are known).
    telemetry_dir: where to look for your best lap for whatever car/track iRacing reports (find_reference).
    reference_index: Setup Lab's saved index of that folder (a dict, or a callable returning one or None), so only
    the file holding the best lap is opened. A reference found (or adopted on track) is kept per car + track for the
    runner's life, so going back to a car + track opens nothing.
    weak_for(car_dict, track_dict) -> weak corner list (e.g. prep.weak_corners rows for that combo), optional.
    focus_for(car_dict, track_dict, length_m) -> the practice plan's live focus for that combo (2.14:
    practiceplan.LiveFocus) or None, optional; asked whenever the car + track changes, like weak_for.
    Never raises out of its thread; problems go to log and to status()['error']."""

    def __init__(self, source, voice, settings: Settings | None = None, reference: Reference | None = None,
                 weak_corners=None, telemetry_dir=None, weak_for=None, log=None, clock=time.monotonic,
                 reference_index=None, focus_for=None):
        self.source = source
        self._focus_for = focus_for
        self.voice = voice
        self.settings = settings or Settings()
        self.coach = Coach(None, weak_corners, self.settings, log=log, duration=self._voice_duration)
        self._given = reference
        self._telemetry_dir = telemetry_dir
        self._reference_index = reference_index
        self._refs: dict = {}           # (car path, track code) -> Reference, for the runner's life
        self.reference_searches = 0     # how many times the telemetry folder was searched (tests)
        self._weak_for = weak_for
        self._weak_static = weak_corners
        self._log = log or _LOG.warning
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._key = None
        self._info = None
        self._state = "stopped"
        self._error = ""
        self._errors = 0
        self._prepared = None
        self.spoken: list = []

    def _voice_duration(self, text):
        fn = getattr(self.voice, "duration", None)
        return fn(text) if fn else None

    # ---- control (window thread) ----
    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return False
        self._stop.clear()
        self._state = "starting"
        self._thread = threading.Thread(target=self._run, name="debrief-livecoach", daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout: float = 3.0) -> bool:
        """Stops the loop and waits for it. True when the thread has ended."""
        self._stop.set()
        t = self._thread
        if t and t.is_alive() and threading.current_thread() is not t:
            t.join(timeout)
        alive = bool(t and t.is_alive())
        if not alive:
            self._state = "stopped"
        return not alive

    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def mute(self):
        self.coach.mute()
        _call(self.voice, "cancel")

    def unmute(self):
        self.coach.unmute()

    def toggle(self) -> bool:
        m = self.coach.toggle()
        if m:
            _call(self.voice, "cancel")
        return m

    def update_settings(self, d: dict):
        """Apply new settings while running (from the window). Volume/rate go to the voice too."""
        new = Settings.from_dict({**self.settings.to_dict(), **(d or {})})
        for f in fields(Settings):
            setattr(self.settings, f.name, getattr(new, f.name))
        try:
            self.voice.set(volume=self.settings.volume, rate=self.settings.rate)
        except Exception:  # noqa: BLE001
            pass
        if self.coach.reference is not None:
            self.coach.set_reference(self.coach.reference)    # top_n / prefix may have changed

    def status(self) -> dict:
        """What the window can show. state: stopped | starting | waiting | no_reference | coaching | finished | error.
        text: one plain line, e.g. 'Waiting for iRacing to start.' or
        'Coaching: Mclaren 570s GT4 at Zolder, reference 1:38.874 from Nov 2'."""
        cs = self.coach.status()
        info = self._info or {}
        car = (info.get("car") or {}).get("name") or ""
        track = short_track((info.get("track") or {}).get("name") or "")
        st = self._state
        if st == "waiting":
            src = getattr(self.source, "state", "")
            text = ("Waiting: Debrief's iRacing connection is missing. Reinstall Debrief." if src == "not_installed"
                    else "Waiting for iRacing to start.")
        elif st == "no_reference":
            text = f"Listening: {car} at {track}. No clean lap to coach from yet. Drive a clean lap to set one."
        elif st == "coaching":
            ref = self.coach.reference
            text = f"Coaching: {car} at {track}, reference {ref.describe()}" if ref else f"Coaching: {car} at {track}"
            if cs["muted"]:
                text += " (muted)"
        elif st == "finished":
            text = "Playback finished"
        elif st == "error":
            text = f"Coach stopped: {self._error}"
        elif st == "starting":
            text = "Starting the coach"
        else:
            text = "Coach off"
        try:
            vs = self.voice.status()
        except Exception:  # noqa: BLE001
            vs = {"backend": "?", "available": False, "reason": ""}
        return {"state": st, "text": text, "car": car, "track": track, "running": self.running(),
                "muted": cs["muted"], "quiet_reason": cs["quiet_reason"], "reference": cs["reference"] and
                {k: cs["reference"][k] for k in ("lap_time", "lap_text", "text", "source")},
                "corners": len(cs["reference"]["corners"]) if cs["reference"] else 0,
                "weak": cs["weak"], "last_lap": cs["last_lap"], "spoken": cs["spoken"], "events": cs["events"],
                "voice": vs, "error": self._error, "errors": self._errors, "settings": self.settings.to_dict(),
                "focus": cs.get("focus")}

    # ---- the loop (its own thread) ----
    def _run(self):
        try:
            while not self._stop.is_set():
                t0 = self._clock()
                try:
                    s = self.source.read()
                except Exception as e:  # noqa: BLE001
                    self._problem(f"reading telemetry failed ({e.__class__.__name__}: {str(e)[:120]})")
                    s = None
                if s is None:
                    if getattr(self.source, "done", False):
                        self._state = "finished"
                        break
                    if self._state not in ("error",):
                        self._state = "waiting"
                    self._key = None
                    self._stop.wait(1.0)
                    continue
                try:
                    self._ensure_reference()
                    self._prepare()                  # a new best may have changed the reference
                    cues = self.coach.feed(s)
                    if self.coach.take_cancel():
                        _call(self.voice, "cancel")
                    for cue in cues:
                        try:
                            if cue.kind == KIND_BEEP:
                                _call(self.voice, "beep")
                                continue
                            self.spoken.append(cue)
                            del self.spoken[:-50]
                            self.voice.say(cue.text, priority=cue.priority, t=cue.spoken_t)
                        except Exception as e:  # noqa: BLE001
                            self._problem(f"speaking failed ({e.__class__.__name__})")
                except Exception as e:  # noqa: BLE001
                    self._problem(f"coach step failed ({e.__class__.__name__}: {str(e)[:120]})")
                iv = getattr(self.source, "interval", 0.0) or 0.0
                if iv > 0:
                    self._stop.wait(max(0.0, iv - (self._clock() - t0)))
        except BaseException as e:  # noqa: BLE001  (never out of the thread)
            self._error = f"{e.__class__.__name__}: {str(e)[:160]}"
            self._state = "error"
            try:
                self._log(f"livecoach: stopped by an error: {self._error}")
            except Exception:  # noqa: BLE001
                pass
        finally:
            try:
                self.source.close()
            except Exception:  # noqa: BLE001
                pass

    def _problem(self, msg: str):
        self._errors += 1
        self._error = msg
        if self._errors <= 5 or self._errors % 100 == 0:
            try:
                self._log(f"livecoach: {msg}")
            except Exception:  # noqa: BLE001
                pass

    def _ensure_reference(self):
        info = None
        try:
            info = self.source.info()
        except Exception:  # noqa: BLE001
            pass
        info = info or {}
        car, track = info.get("car") or {}, info.get("track") or {}
        key = ((car.get("path") or "").lower(), (track.get("code") or "").lower())
        if key == self._key:
            if self.coach.reference is not None and self._state != "coaching":
                self._state = "coaching"
            return
        if self._key and self._key[0] and self.coach.reference is not None:
            self._refs[self._key] = self.coach.reference      # keep the best so far (a lap adopted on track too)
        self._key, self._info = key, info
        weak = self._weak_static
        if self._weak_for is not None:
            try:
                weak = self._weak_for(car, track)
            except Exception as e:  # noqa: BLE001
                self._problem(f"weak corners not available ({e.__class__.__name__})")
        ref = None
        g = self._given
        if g is not None and (not key[0] or not g.car.get("path") or
                              ((g.car.get("path") or "").lower() == key[0] and
                               (not key[1] or (g.track.get("code") or "").lower() == key[1]))):
            ref = g
        if ref is None and key[0] and key in self._refs:
            ref = self._refs[key]
        if ref is None and self._telemetry_dir and key[0]:
            idx = self._reference_index
            if callable(idx):
                try:
                    idx = idx()
                except Exception as e:  # noqa: BLE001
                    self._problem(f"Setup Lab's index not available ({e.__class__.__name__})")
                    idx = None
            self.reference_searches += 1
            ref = find_reference(self._telemetry_dir, key[0], key[1], prefix=self.settings.corner_prefix, index=idx,
                                 stop=self._stop.is_set)
            if ref is not None:
                self._refs[key] = ref
        focus = None
        if self._focus_for is not None:
            try:
                focus = self._focus_for(car, track, info.get("length_m"))
            except Exception as e:  # noqa: BLE001
                self._problem(f"practice focus not available ({e.__class__.__name__})")
        self.coach.set_focus(focus)
        self.coach.hint(info.get("length_m"), car, track)
        self.coach.set_reference(ref, weak or [])
        self._state = "coaching" if ref is not None else "no_reference"
        self._prepared = None
        self._prepare()

    def _prepare(self):
        """Hand the voice every fixed phrase for this reference, to render ahead of time (once per reference)."""
        r = self.coach.reference
        if r is None or self._prepared is r:
            return
        self._prepared = r
        _call(self.voice, "prepare", self.coach.vocabulary())


def _call(obj, name, *a):
    fn = getattr(obj, name, None)
    if fn is not None:
        try:
            fn(*a)
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- optional global hotkey (Windows)

_MODS = {"alt": 0x1, "ctrl": 0x2, "control": 0x2, "shift": 0x4, "win": 0x8}


def parse_hotkey(text: str):
    """'ctrl+shift+m' -> (mods, virtual key) or None."""
    parts = [p.strip().lower() for p in str(text or "").split("+") if p.strip()]
    if not parts:
        return None
    mods = 0
    for p in parts[:-1]:
        if p not in _MODS:
            return None
        mods |= _MODS[p]
    k = parts[-1]
    if len(k) == 1 and k.isalnum():
        vk = ord(k.upper())
    elif re.fullmatch(r"f([1-9]|1[0-9]|2[0-4])", k):
        vk = 0x6F + int(k[1:])
    elif k == "pause":
        vk = 0x13
    elif k == "scrolllock":
        vk = 0x91
    else:
        return None
    return mods, vk


class Hotkey:
    """A system-wide key (RegisterHotKey) that calls callback() on its own thread. Windows only; elsewhere, or if
    the key is already taken by another program, start() returns False and nothing happens. Map a wheel button to
    this key (e.g. in the wheel's software) to mute from the car."""
    WM_HOTKEY, WM_QUIT, MOD_NOREPEAT = 0x0312, 0x0012, 0x4000

    def __init__(self, text: str, callback, log=None):
        self.text = text
        self.callback = callback
        self._log = log or _LOG.warning
        self._thread = None
        self._tid = None
        self._ok = threading.Event()
        self._done = threading.Event()
        self.registered = False

    def start(self, timeout: float = 2.0) -> bool:
        import sys
        hk = parse_hotkey(self.text)
        if sys.platform != "win32" or hk is None:
            return False
        self._thread = threading.Thread(target=self._run, args=hk, name="debrief-hotkey", daemon=True)
        self._thread.start()
        self._done.wait(timeout)
        return self.registered

    def _run(self, mods, vk):
        try:
            import ctypes
            from ctypes import wintypes
            user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
            self._tid = kernel32.GetCurrentThreadId()
            if not user32.RegisterHotKey(None, 1, mods | self.MOD_NOREPEAT, vk):
                self._log(f"livecoach: the mute key {self.text} is in use by another program")
                self._done.set()
                return
            self.registered = True
            self._done.set()
            msg = wintypes.MSG()
            try:
                while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                    if msg.message == self.WM_HOTKEY:
                        try:
                            self.callback()
                        except Exception as e:  # noqa: BLE001
                            self._log(f"livecoach: mute key action failed ({e.__class__.__name__})")
            finally:
                user32.UnregisterHotKey(None, 1)
        except Exception as e:  # noqa: BLE001
            self._log(f"livecoach: mute key not available ({e.__class__.__name__})")
        finally:
            self.registered = False
            self._done.set()

    def stop(self, timeout: float = 2.0):
        if self._thread is None or not self._thread.is_alive() or self._tid is None:
            return
        try:
            import ctypes
            ctypes.windll.user32.PostThreadMessageW(self._tid, self.WM_QUIT, 0, 0)
        except Exception:  # noqa: BLE001
            pass
        self._thread.join(timeout)
