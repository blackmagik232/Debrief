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

Cues are deliberately few and quiet:
  a) "Brake marker, T3"            about 2.5 s before the reference brake point of a top-3 weak corner
  b) "T3: carry more speed, 6 down" / "T3: braked early"   just after a weak corner, once per corner per lap
  c) "Up two tenths" / "Down three tenths" / "New best..."  at the line, clean laps only, |delta| >= 0.1 s
  d) silence: car alongside (and 3 s after), pit road, off track, out/in laps, a lap not seen from its start,
     race lap 1 (and races at all unless in_races), at most one cue every 4 s, nothing in the 1.5 s before any
     brake point except that corner's own marker, nothing that would block a weak corner's marker, mute.
A cue that can't be said within 2 s (a marker: while there is still >= 1 s to the brake point) is dropped, never
spoken late.

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

KIND_MARKER, KIND_FEEDBACK, KIND_LAP = "marker", "feedback", "lap"
PRIORITY = {KIND_MARKER: 3, KIND_FEEDBACK: 2, KIND_LAP: 1}


# ---------------------------------------------------------------- settings

@dataclass
class Settings:
    markers: bool = True                # cue a)
    feedback: bool = True               # cue b)
    lap_delta: bool = True              # cue c)
    top_n: int = 3                      # weak corners coached (markers and feedback)
    marker_lead_s: float = 2.5          # say the marker this long before the brake point
    marker_min_lead_s: float = 1.0      # ...and never with less than this left (else dropped)
    slower_kmh: float = 4.0             # min speed this much under the reference -> "carry more speed"
    early_m: float = 15.0               # braked this much earlier than the reference -> "braked early"
    delta_min_s: float = 0.1            # lap delta said only from this size
    in_races: bool = False              # coach in Race sessions too
    update_reference: bool = True       # a new best clean lap becomes the reference
    gap_s: float = 4.0                  # at most one cue every gap_s
    traffic_hold_s: float = 3.0         # quiet this long after a car was alongside
    quiet_before_brake_s: float = 1.5   # quiet this long before any brake point (except its own marker)
    stale_s: float = 2.0                # a cue not said within this is dropped
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
               "LongAccel", "ABSActive"):
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


def build_reference(src, lap: int | None = None, prefix: str = "T") -> Reference | None:
    """Your best clean lap as a Reference.

    src: an .ibt path, an open ibt.Ibt, a list of .ibt paths (the fastest clean lap across them), or a list of
    sample dicts covering one lap (as the coach records them). lap: a lap number to use instead of the fastest
    (one file only). Returns None when there is no usable clean lap. Never raises for a bad file."""
    if isinstance(src, list) and src and isinstance(src[0], dict):
        return reference_from_samples(src, prefix=prefix)
    paths = src if isinstance(src, (list, tuple)) else [src]
    best = None
    for p in paths:
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


def find_reference(folder, car_path: str, track_code: str, prefix: str = "T", limit: int = 40) -> Reference | None:
    """Your best clean lap for this car and track across the .ibt files in folder (Documents\\iRacing\\telemetry).
    iRacing names its files '<carpath>_<track code> <date> <time>.ibt'; the newest `limit` matching files are read."""
    folder = Path(folder) if folder else None
    if not folder or not folder.is_dir() or not car_path:
        return None
    want = f"{car_path}_{track_code}".lower() if track_code else f"{car_path}_".lower()
    try:
        files = [p for p in folder.glob("*.ibt") if p.name.lower().startswith(want)]
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    return build_reference(files[:limit], prefix=prefix) if files else None


# ---------------------------------------------------------------- weak corners

@dataclass
class Weak:
    corner: Corner
    lost_s: float | None
    name: str                   # spoken name


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
        c, lost, num = None, None, None
        if isinstance(w, dict):
            num = _num_of(w.get("id"))
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
        out.append((order, Weak(corner=c, lost_s=lost, name=f"{prefix}{num if num is not None else c.num}")))
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


class Coach:
    """feed(sample) -> [Cue] (none or one). Independent of where the samples come from."""

    def __init__(self, reference: Reference | None = None, weak_corners=None, settings: Settings | None = None,
                 log=None):
        self.settings = settings or Settings()
        self._log = log or _LOG.warning
        self.weak_input = list(weak_corners or [])
        self.reference = None
        self.weak: list = []
        self.queue = CueQueue()
        self.spoken: list = []
        self.events: list = []          # notable non-spoken events ("reference updated", ...)
        self.quiet_reason: str | None = None
        self.last_lap: dict | None = None
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

    def mute(self):
        self.settings.muted = True
        self.queue.clear()

    def unmute(self):
        self.settings.muted = False

    def toggle(self) -> bool:
        if self.settings.muted:
            self.unmute()
        else:
            self.mute()
        return self.settings.muted

    def status(self) -> dict:
        r = self.reference
        return {"reference": r.summary() if r else None,
                "weak": [{"name": w.name, "id": w.corner.id, "lost_s": w.lost_s, "apex_m": round(w.corner.apex_m),
                          "brake_m": round(w.corner.brake_m) if w.corner.brake_m is not None else None} for w in self.weak],
                "muted": self.settings.muted, "quiet_reason": self.quiet_reason,
                "last_lap": self.last_lap, "spoken": [c.to_dict() for c in self.spoken[-10:]],
                "events": self.events[-10:]}

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
        self._traffic_until = -1e9
        self._last_spoken_t = -1e9
        self._obs = {}
        self._marker_done = set()
        self.queue.clear()

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
        if pct < 0 or pct > 1.0001:                      # not in the world
            self._prev_pct = None
            return []
        alongside = clr not in (0, 1)
        if alongside:
            self._traffic_until = t + st.traffic_hold_s
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
                self._end_lap(lap, tc, stype)
            self._lap = lap = _Lap(lapn, tc, valid=crossed and on_track, inc=inc)
        if pit or surf in (ibt.IN_PIT_STALL, ibt.APPROACHING_PITS):
            lap.pit = True
        if surf == ibt.OFF_TRACK or not on:
            lap.off = True
        if inc != lap.inc0:
            lap.incident = True
        if lap.valid and not lap.pit and len(lap.trace) < 20000:
            lap.trace.append((t, pct, v, brk, float(s.get("Throttle") or 0.0), float(s.get("Gear") or 0.0),
                              float(s.get("SteeringWheelAngle") or 0.0)))
        start_lap = is_race(stype) and lapn <= 1
        coachable = lap.valid and not lap.pit and not start_lap

        # ---- corners ----
        ref = self.reference
        L = ref.length_m if ref else 0.0
        tts = {}                                          # corner num -> seconds to its brake point (ahead only)
        if ref:
            vv = max(v, 5.0)
            for c in ref.braked:
                dist = ((c.brake_pct - pct) % 1.0) * L
                if dist < L / 2:
                    tts[c.num] = dist / vv
            for w in self.weak:
                self._observe(w, pct, v, brk, t, alongside, surf, on, coachable)
                self._marker(w, tts, t, coachable)

        # ---- speak? ----
        self._prev_pct, self._prev_t = pct, t
        reason = self._quiet(st, stype, on_track, alongside, t, coachable, start_lap, lap)
        self.quiet_reason = reason
        if st.muted:
            self.queue.clear()
            return []

        def allowed(c: Cue) -> bool:
            if reason and not (c.kind == KIND_LAP and reason in ("Out lap or in lap", "Lap not seen from the start")):
                return False
            if t - self._last_spoken_t < st.gap_s:
                return False
            for num, tt in tts.items():          # quiet before any brake point, except this corner's own marker
                if tt < st.quiet_before_brake_s and not (c.kind == KIND_MARKER and c.corner == f"C{num}"):
                    return False
            if c.kind != KIND_MARKER and st.markers and coachable:   # keep room for a weak corner's marker
                for w in self.weak:
                    tt = tts.get(w.corner.num)
                    if tt is not None and w.corner.num not in self._marker_done and tt <= st.marker_lead_s + st.gap_s \
                            and tt > st.marker_min_lead_s:
                        return False
            return True

        cue = self.queue.pop(t, allowed)
        if cue is None:
            return []
        self._last_spoken_t = t
        cue.spoken_t = t
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

    def _marker(self, w: Weak, tts: dict, t: float, coachable: bool):
        st = self.settings
        c = w.corner
        if c.brake_pct is None:
            return
        tt = tts.get(c.num)
        if tt is None:                           # behind us: ready for next lap
            self._marker_done.discard(c.num)
            return
        if not st.markers or not coachable or c.num in self._marker_done:
            return
        if st.marker_min_lead_s <= tt <= st.marker_lead_s:
            self._marker_done.add(c.num)
            self.queue.push(Cue(text=f"Brake marker, {w.name}", priority=PRIORITY[KIND_MARKER],
                                key=f"marker:{c.id}", t=t, kind=KIND_MARKER, corner=c.id,
                                expires=t + (tt - st.marker_min_lead_s)))

    def _observe(self, w: Weak, pct, v, brk, t, alongside, surf, on, coachable):
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
        if not st.feedback or not o.ok or o.off or o.traffic or o.n < 5:
            return
        down = c.min_kmh - o.min_v * KMH
        text = None
        if down >= st.slower_kmh:
            text = f"{w.name}: carry more speed, {int(round(down))} down"
        elif c.brake_rel_m is not None and o.brake_rel is not None and c.brake_rel_m - o.brake_rel >= st.early_m:
            text = f"{w.name}: braked early"
        if text:
            self.queue.push(Cue(text=text, priority=PRIORITY[KIND_FEEDBACK], key=f"feedback:{c.id}", t=t,
                                kind=KIND_FEEDBACK, corner=c.id, expires=t + st.stale_s))

    def _end_lap(self, lap: _Lap, tc: float, stype: str):
        st = self.settings
        start_lap = is_race(stype) and lap.num <= 1
        clean = lap.valid and not (lap.pit or lap.off or lap.incident or start_lap)
        lt = (tc - lap.t0) if lap.t0 is not None else None
        ref = self.reference
        info = {"lap": lap.num, "time": round(lt, 3) if lt else None, "clean": clean, "delta": None}
        self.last_lap = info
        if not clean or not lt or lt <= 0:
            return
        if ref is not None and abs(lt - ref.lap_time) > max(5.0, 0.15 * ref.lap_time):
            return                                        # not a real lap of this track (cut, reset, wrong reference)
        if ref is not None:
            d = lt - ref.lap_time
            info["delta"] = round(d, 3)
            text = None
            if d < 0:
                text = "New best" + (f", up {delta_words(d)}" if -d >= st.delta_min_s - 1e-9 else "")
            elif d >= st.delta_min_s - 1e-9:
                text = f"Down {delta_words(d)}"
            if text and st.lap_delta:
                self.queue.push(Cue(text=text, priority=PRIORITY[KIND_LAP], key="lap", t=tc, kind=KIND_LAP,
                                    expires=tc + st.stale_s))
        if (ref is None or lt < ref.lap_time) and st.update_reference:
            self._adopt(lap, lt)

    def _adopt(self, lap: _Lap, lt: float):
        ref = self.reference
        tr = lap.trace
        if len(tr) < 50:
            return
        a = np.array(tr)
        cols = {"Speed": a[:, 2], "LapDistPct": a[:, 1], "Brake": a[:, 3], "Throttle": a[:, 4], "Gear": a[:, 5],
                "SteeringWheelAngle": a[:, 6]}
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


# ---------------------------------------------------------------- sample sources

CHANNELS = ("SessionTime", "SessionNum", "Lap", "LapDistPct", "LapDist", "Speed", "Throttle", "Brake",
            "SteeringWheelAngle", "Gear", "LapCurrentLapTime", "LapLastLapTime", "LapDeltaToBestLap",
            "LapDeltaToBestLap_OK", "OnPitRoad", "PlayerTrackSurface", "CarLeftRight", "PlayerCarMyIncidentCount",
            "IsOnTrack")


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
    weak_for(car_dict, track_dict) -> weak corner list (e.g. prep.weak_corners rows for that combo), optional.
    Never raises out of its thread; problems go to log and to status()['error']."""

    def __init__(self, source, voice, settings: Settings | None = None, reference: Reference | None = None,
                 weak_corners=None, telemetry_dir=None, weak_for=None, log=None, clock=time.monotonic):
        self.source = source
        self.voice = voice
        self.settings = settings or Settings()
        self.coach = Coach(None, weak_corners, self.settings, log=log)
        self._given = reference
        self._telemetry_dir = telemetry_dir
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
        self.spoken: list = []

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

    def unmute(self):
        self.coach.unmute()

    def toggle(self) -> bool:
        return self.coach.toggle()

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
        text: one line, e.g. 'Listening — iRacing not running' or
        'Coaching: Mclaren 570s GT4 at Zolder, reference 1:38.874 from Nov 2'."""
        cs = self.coach.status()
        info = self._info or {}
        car = (info.get("car") or {}).get("name") or ""
        track = short_track((info.get("track") or {}).get("name") or "")
        st = self._state
        if st == "waiting":
            src = getattr(self.source, "state", "")
            text = ("Listening — pyirsdk is not installed" if src == "not_installed"
                    else "Listening — iRacing not running")
        elif st == "no_reference":
            text = f"Listening: {car} at {track} — no clean lap to coach from yet; drive a clean lap to set one"
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
                "voice": vs, "error": self._error, "errors": self._errors, "settings": self.settings.to_dict()}

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
                    for cue in self.coach.feed(s):
                        self.spoken.append(cue)
                        del self.spoken[:-50]
                        try:
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
        if ref is None and self._telemetry_dir and key[0]:
            ref = find_reference(self._telemetry_dir, key[0], key[1], prefix=self.settings.corner_prefix)
        self.coach.hint(info.get("length_m"), car, track)
        self.coach.set_reference(ref, weak or [])
        self._state = "coaching" if ref is not None else "no_reference"


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
