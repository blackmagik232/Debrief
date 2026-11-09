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

2.17.2 (and the Coaching level, LEVELS):
  e) checked again just before it plays: every cue carries still_valid(state, starting) (a fresh telemetry sample in,
     None while it still holds, else why not). A brake marker whose clip could no longer end 1 s before its brake
     point, a word about a corner once the next corner's braking zone has started, a sector or lap time once a newer
     sector is done: dropped ("no longer valid" in the cue log), never said late. LiveRunner hands the check to the
     voice, which runs it right before the sound starts. At most QUEUE_DEPTH cues of one kind wait.
  f) hard zones: from the reference lap, heavy braking (Brake > 0.5) until full throttle (> 0.9) again. Cues that
     are not about a corner (sector and lap times, "good" cues, "Reference set", the answer to "How's my pace?")
     wait until the car is out of one and only start when they end before the next one at this speed.
  g) "How's my pace?" (Coach.ask, a key or a wheel button): last clean lap against the reference and the corner that
     cost the most ("Down three tenths, most in T4"). The level "Only when I ask" says nothing else.

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

from . import cornernotes, crewchief, engine, ibt, livedata, perfmeter, voicepack
from . import units as _units

GRID = 1000                 # reference points around the lap
KMH = 3.6
KMH_PER_MPH = 1.609344      # 2.17.2: speeds are said in mph (owner's choice); measured in km/h as before
BRAKE_ON = 0.1              # live brake pedal above this (after being below it) is a brake onset
_LOG = logging.getLogger("debrief.livecoach")

ABS_IBT = "BrakeABSactive"   # the ABS channel's name in .ibt files (Garage 61's CSV calls it ABSActive)
MAX_PHRASE = 60             # characters: a corner-coach phrase longer than this is not spoken

KIND_MARKER, KIND_FEEDBACK, KIND_LAP, KIND_BEEP = "marker", "feedback", "lap", "beep"
KIND_CORNER, KIND_SECTOR, KIND_INFO = "corner", "sector", "info"     # 2.17.2
KIND_ASK = "ask"            # 2.17.2: the answer to "How's my pace?"
KIND_NOTE = "note"          # 2.20: one of his own recorded corner notes, played before its corner
FOCUS_MIN_HZ = 15.0         # 2.14 review: live reads at least this often while a practice focus is watched
PRIORITY = {KIND_MARKER: 3, KIND_FEEDBACK: 2, KIND_CORNER: 2, KIND_LAP: 1, KIND_SECTOR: 1, KIND_INFO: 1,
            KIND_ASK: 2, KIND_NOTE: 2}
QUEUE_DEPTH = {KIND_MARKER: 2, KIND_FEEDBACK: 2, KIND_CORNER: 2, KIND_SECTOR: 1, KIND_LAP: 1, KIND_INFO: 1,
               KIND_ASK: 1, KIND_NOTE: 1}  # 2.17.2: at most this many cues of one kind wait (the oldest goes)
HARD_BRAKE = 0.5            # 2.17.2 hard zone: from the reference's brake above this ...
HARD_THROTTLE = 0.9         # ... until its throttle is above this again
HARD_GATING = True          # (off only to measure what the hard zones cost)
ASK_WAIT_S = 15.0           # the answer to "How's my pace?" waits this long for a place to say it
PACE_MOST_MIN_S = 0.05      # "most in T4" only when that corner cost at least this
NO_CLEAN_TEXT = "No clean lap yet"
FAR_DOWN_TEXT = "Down more than two seconds"
LAP_TIMES = ("full", "short")   # "Reference set, one thirty-eight nine" or "..., thirty-eight nine"

# 2.17.2 Coaching level. quiet: 2.17.0's few cues (focus corners and the lap only, Settings.max_per_lap and gap_s).
# normal (the default) and busy also give: focus corners learned from this session when no review or Corner coach
# card names any, a word after any corner where he lost (or gained) time against the reference, sector deltas, and
# "Building your reference" / "Reference set, ..." before there is a reference.
#   per_lap     cues a lap, every kind together (quiet: Settings.max_per_lap, markers + feedback only)
#   gap_s       at most one cue every this many seconds (quiet: Settings.gap_s)
#   live_top    focus corners learned from this session
#   corner_min_s  a corner where he lost or gained at least this against the reference gets a word
#   positives   "good" cues a lap
LEVELS = {
    "quiet": {"per_lap": None, "gap_s": None, "live_top": 0, "corner_min_s": None, "positives": 0,
              "sectors": False, "first_laps": False},
    "normal": {"per_lap": 4, "gap_s": 3.0, "live_top": 2, "corner_min_s": 0.1, "positives": 1,
               "sectors": True, "first_laps": True},
    "busy": {"per_lap": 6, "gap_s": 2.5, "live_top": 3, "corner_min_s": 0.1, "positives": 2,
             "sectors": True, "first_laps": True},
    # 2.17.2 "Only when I ask": nothing but the answer to "How's my pace?"
    "ask": {"per_lap": 0, "gap_s": 3.0, "live_top": 0, "corner_min_s": None, "positives": 0,
            "sectors": False, "first_laps": False},
    # 2.20 "Notes only": nothing but his own corner notes (and the answer to "How's my pace?" when he asks)
    "notes": {"per_lap": 0, "gap_s": 3.0, "live_top": 0, "corner_min_s": None, "positives": 0,
              "sectors": False, "first_laps": False},
}
DEFAULT_LEVEL = "normal"
LIVE_MIN_LOSS_S = 0.05      # a corner is a session focus corner only when it costs at least this on average
CORNER_MAX_S = 1.0          # a corner this far off the reference was a moment (traffic, a slide): no word about it
LEARN_LAPS = 3              # session focus corners come from the last this many clean laps
# 2.20 Learning the track: a corner is compared with his own best so far in this session (the live reference)
LEARN_BRAKE_M = 10.0        # braked this much later or earlier than his best: a word about it (said to the nearest 5 m)
LEARN_BIG = 2.0             # an error this many times the threshold is said again even a lap after the last word
NOTE_TEXT = "Your note, {}"
NOTE_WINDOW_S = 0.4         # a note is made within this much time of its latest start: it ends 1 to 1.4 s before the brake point


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
    slower_kmh: float = 4.0             # min speed this much under the reference -> "more speed" (2.17.2: said in
                                        # mph, from slower_mph(): 4 km/h -> 3 mph down or more)
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
    # Coach 2.0
    voice_pack: str = "engineer"        # voicepack id ("" or unknown: the default pack)
    device: str = ""                    # output device name ("" = Windows default)
    repeat_hotkey: str = "ctrl+shift+r"   # say the last cue again
    quiet_hotkey: str = "ctrl+shift+n"    # mute for a minute
    fixed_laps: int = 2                 # a corner driven well this many passes in a row is left alone
    log_cues: bool = True               # write the cue log (coach-logs/*.jsonl)
    level: str = DEFAULT_LEVEL          # 2.17.2 Coaching level: quiet / normal / busy / ask (LEVELS)
    pace_hotkey: str = "ctrl+shift+p"   # 2.17.2 "How's my pace?"
    lap_times: str = "short"            # 2.17.2 how a lap time is read: "full" or "short" (no minute)
    units: str = "mph"                  # 2.18: spoken speeds in "mph" or "kmh" (the window passes Settings > Units)
    # 2.20
    notes: bool = True                  # play his own recorded corner notes before their corners
    learn_cues: bool = True             # Learning the track: words against his own best so far in this session
    note_hotkey: str = "ctrl+shift+v"   # hold to record a corner note
    use_lap_hotkey: str = "ctrl+shift+l"  # "use this lap" as the reference
    # 2.29 Crew chief (fuel, pit window, rivals): the calls in the window, and the voice for them
    crew_calls: bool = True
    crew_voice: bool = True

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
        s.audio_latency_s = max(0.0, min(0.5, s.audio_latency_s))
        s.fixed_laps = max(1, min(10, s.fixed_laps))
        s.device = "".join(ch for ch in s.device if ch.isprintable()).strip()[:31]
        s.voice_pack = s.voice_pack.strip()[:40]
        s.level = s.level.strip().lower()
        if s.level not in LEVELS:
            s.level = DEFAULT_LEVEL
        s.lap_times = s.lap_times.strip().lower()
        if s.lap_times not in LAP_TIMES:
            s.lap_times = "short"
        return s

    def lv(self, key: str):
        """This coaching level's value for key (LEVELS)."""
        return LEVELS.get(self.level, LEVELS[DEFAULT_LEVEL])[key]

    @property
    def quiet(self) -> bool:
        return self.level == "quiet"

    @property
    def ask_only(self) -> bool:
        """2.17.2 "Only when I ask": nothing is said but the answer to "How's my pace?"."""
        return self.level == "ask"

    @property
    def notes_only(self) -> bool:
        """2.20 "Notes only": nothing is said but his own corner notes (and the answer to "How's my pace?")."""
        return self.level == "notes"

    def per_lap(self) -> int:
        return self.max_per_lap if self.quiet else int(self.lv("per_lap"))

    def gap(self) -> float:
        return self.gap_s if self.quiet else float(self.lv("gap_s"))

    def slower_mph(self) -> float:
        """2.17.2: the "more speed" trigger in mph: slower_kmh in mph rounded up to a whole mph (4 km/h, 2.5 mph ->
        3 mph down or more); a threshold under 1 mph (tests) is kept as it is."""
        m = self.slower_kmh / KMH_PER_MPH
        return float(math.ceil(m - 1e-9)) if m >= 1.0 else m

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
        """'1:38.874 from Nov 2'. 2.19.1: a lap the coach adopted while you drive is 'from this session': a calendar
        day beside it could disagree with the Practice list (which dates laps by their telemetry file)."""
        if (self.source or {}).get("kind") == "live" and not (self.source or {}).get("played"):
            return fmt_lap(self.lap_time) + " from this session"
        return fmt_lap(self.lap_time) + day_words(self.source.get("date"))

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


def file_when(name, start=None) -> str:
    """2.18: when a telemetry file was driven, 'YYYY-MM-DD HH:MM', exactly as Practice lists it (setuplab._when: the
    time in the file name his PC wrote, else the header's). '' when neither is known."""
    try:
        from . import setuplab
        return setuplab._when(Path(str(name or "")).name, start if start and start > 0 else None)[0]
    except Exception:  # noqa: BLE001
        return ""


def _date_of(ib) -> str:
    """The day of the file's lap. 2.18: from the file name first, as Practice shows it. The header's start date can
    be another day (Maya's lap of Nov 3 was called "from Sep 21"), so it is only used when the name has no date."""
    return file_when(getattr(ib, "path", ""), getattr(ib, "start_date", 0))[:10]


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
                                      "when": file_when(ib.path, ib.start_date), "session": x["session"]},
                              prefix=prefix)


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


def reference_candidates(index, car_path: str, track_code: str) -> list:
    """[(best clean lap s, file name, index record)] of Setup Lab's index for this car + track, fastest first: the
    order find_reference opens files in (2.18: shared with reference_choice, so the Practice page names the same lap)."""
    cp, tc = str(car_path or "").lower(), str(track_code or "").lower()
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
    return cand


def reference_choice(index, car_path: str, track_code: str) -> dict | None:
    """2.18: the lap the coach starts from for this car + track, worked out from Setup Lab's index alone (no file is
    opened): {"file", "lap_time", "lap_text", "date", "when", "text"} with the date as Practice lists it, or None.
    find_reference opens the same file first (it moves to the next only when that file is still being written or
    gives no lap)."""
    cand = reference_candidates(index, car_path, track_code)
    if not cand:
        return None
    best, name, r = cand[0]
    when = file_when(name, r.get("start"))
    return {"file": name, "lap_time": round(best, 3), "lap_text": fmt_lap(best), "date": when[:10], "when": when,
            "text": fmt_lap(best) + day_words(when[:10])}


def day_words(date: str) -> str:
    """' from Nov 3' for '2026-11-03' ('' when there is no date): the coach's way of naming a lap's day."""
    try:
        d = datetime.strptime(str(date or "")[:10], "%Y-%m-%d")
    except ValueError:
        return ""
    return f" from {d.strftime('%b')} {d.day}"


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

    cand = reference_candidates(index, cp, tc)
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
    why: str = ""               # Coach 2.0 cue log: why this cue was made, in a few words
    measured: dict = field(default_factory=dict)   # ... and the numbers behind it
    blocked: str | None = None  # ... the last rule that held it back (said when it is dropped)
    # 2.17.2: still_valid(state, starting=True) -> None while the cue still holds for this telemetry sample, else why
    # not (a few words). starting: it is about to start playing now (also checks room before a brake point)
    still_valid: object = field(default=None, repr=False, compare=False)

    def to_dict(self) -> dict:
        return {"t": round(self.spoken_t if self.spoken_t is not None else self.t, 2), "made": round(self.t, 2),
                "text": self.text, "kind": self.kind, "priority": self.priority, "corner": self.corner}


class CueQueue:
    """Waiting cues, most important first. A cue past its expiry is dropped, never spoken late. on_drop(cue, why) is
    told about every cue that leaves without being said (Coach 2.0 cue log)."""

    def __init__(self, on_drop=None):
        self.items: list = []
        self.dropped: list = []
        self.on_drop = on_drop

    def _dropped(self, c: Cue, why: str):
        self.dropped.append(c)
        del self.dropped[:-20]
        if self.on_drop is not None:
            try:
                self.on_drop(c, why)
            except Exception:  # noqa: BLE001
                pass

    def push(self, cue: Cue):
        for c in self.items:
            if c.key == cue.key:
                self._dropped(c, "replaced by a newer cue")
        self.items = [c for c in self.items if c.key != cue.key] + [cue]
        same = sorted((c for c in self.items if c.kind == cue.kind), key=lambda c: c.t)
        for c in same[:max(0, len(same) - QUEUE_DEPTH.get(cue.kind, 2))]:     # 2.17.2: the oldest of a kind go
            self.items.remove(c)
            self._dropped(c, f"too many waiting ({cue.kind})")

    def prune(self, why_not):
        """2.17.2: drop every waiting cue that why_not(cue) gives a reason for ("no longer valid: ...")."""
        keep = []
        for c in self.items:
            why = why_not(c)
            if why:
                self._dropped(c, "no longer valid: " + why)
            else:
                keep.append(c)
        self.items = keep

    def pop(self, now: float, allowed=lambda c: True) -> Cue | None:
        keep = []
        for c in self.items:
            if now > c.expires:
                self._dropped(c, "stale: " + c.blocked if c.blocked else "stale")
            else:
                keep.append(c)
        self.items = keep
        for c in sorted(keep, key=lambda c: (-c.priority, c.t)):
            if allowed(c):
                self.items.remove(c)
                return c
        return None

    def clear(self, why: str = "", keep_kind: str | None = None):
        items = [c for c in self.items if c.kind != keep_kind]
        self.items = [c for c in self.items if c.kind == keep_kind]
        if why:
            for c in items:
                self._dropped(c, why)


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
        self.said = 0                   # 2.17.2: every cue said this lap (the level's per-lap limit)
        self.positives = 0              # ... "good" cues said this lap
        self.closs: dict = {}           # ... corner num -> seconds lost against the reference (clean passes)
        self.sector_t: list = []        # ... session time at each sector boundary passed


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
        self.on_event = None             # Coach 2.0: fn(dict) for the cue log (made, spoken, dropped, ...)
        self._ctx: dict = {}             # where the car is now, for those events
        self._passes: dict = {}          # corner num -> recent verdicts (True = an error pass), newest last
        self._fixed: set = set()         # corners he has fixed: no cue about them until they slip again
        self._erred: set = set()         # corners with an error pass since they were last fixed
        self.queue = CueQueue(on_drop=self._on_drop)
        self.spoken: list = []
        self.events: list = []          # notable non-spoken events ("reference updated", ...)
        self.quiet_reason: str | None = None
        self.last_lap: dict | None = None
        self.lap_log: list = []          # the last laps seen: lap, time, clean, delta, timed_by
        self._cancel = False
        self._corner_err: dict = {}      # corner num -> [error on the last pass, error on the pass before]
        self._corner_last: dict = {}     # corner num -> (lap number, kind) of its last cue
        self.focus = None                # 2.14: the practice plan's focus corner (practiceplan.LiveFocus), optional
        self.focus_corner = None         # ... that corner on the reference (Corner), matched by its apex
        self.keep_reference = None       # 2.15: a reference he chose (a teammate's lap): never replaced while in use
        self.proven: list = []           # 2.17.2: focus corners from the race reviews or the Corner coach
        self.learned: list = []          # ... focus corners learned from this session (when there are no proven ones)
        self._loss_hist: list = []       # ... {corner num: seconds lost} for the last clean laps against this reference
        self._segs: dict = {}            # ... corner num -> (entry pct, exit pct) timed against the reference
        self._sectors: list = []         # ... the two sector boundaries (pct), thirds of the reference lap by time
        self._fb_made: dict = {}         # ... corner num -> session time its weak-corner feedback was made
        self._building = None            # ... "Building your reference": None, "made" or "said"
        self._full_since = None          # ... session time full throttle began (no reference: a straight)
        self._hard: list = []            # ... hard zones of the reference [(start pct, end pct)] (hard_zones)
        self._sector_seq = 0             # ... sector boundaries and lines crossed (a newer one outdates older times)
        self._pct = None                 # ... where the car is now (cues made now remember it)
        self._ask = False                # ... "How's my pace?" asked: answered on the next sample
        self._speaking_until = -1e9      # ... session time the last cue said ends
        self.asks = 0
        self.invalid_drops = 0           # ... cues dropped because they were no longer valid when they would play
        self.time_scale = 1.0            # 2.18 rehearsal at 2x / 4x: session seconds per second of real sound
        self.date_hint = None            # 2.18 rehearsal: the day the played file was driven (a lap adopted from it)
        self.live_delta = None           # 2.18 driving view: seconds against the reference so far this lap
        self.skipping = False            # 2.18 rehearsal: skipping to the next lap (cues are made, not said)
        self._last_along_t = -1e9
        self._last_off_t = -1e9
        self._last_inc_t = -1e9
        # 2.20 corner notes and learning the track
        self.notes = None                # cornernotes.NoteStore (the runner sets it), optional
        self.track_key = ""              # names.track_key of the track he is on (notes are filed by it)
        self.run_id = ""                 # this run of the coach: a note recorded on lap 4 of it first plays on lap 5
        self.recording = False           # he is recording a note: no cue starts
        self.count_notes = True          # live: passes and plays are counted in the store (a rehearsal counts its own)
        self._pass_local: dict = {}      # ... note id -> passes counted in this rehearsal only
        self._note_map: dict = {}        # corner num -> the note to play there (newest enabled one)
        self._note_key = None            # ... what the map was made from (store version, reference)
        self._note_done: set = set()     # ... corners whose note was decided this lap
        self._note_planned: set = set()  # ... corners whose note is coming this lap (no brake marker there)
        self._note_dur: dict = {}        # ... cue text -> seconds of the note it plays
        self._closs_last: dict = {}      # corner num -> seconds lost on its last clean pass (learning plan)
        self._learn_last: dict = {}      # corner num -> lap of the last learning word about it
        self._clean_laps = 0             # clean laps seen since the car and track were set (Learning the track)
        self._use_lap = False            # "use this lap" asked, answered on the next sample
        self._last_clean = None          # (lap, time) of the last clean lap with a time
        self.use_lap_note = None         # {"ok", "text", "wall"}: what "use this lap" did, for the window
        self._learn_session = True       # no reference when the car and track were set: Learning the track
        self._reset_all()
        if reference is not None:
            self.set_reference(reference)

    # ---- public ----
    def set_reference(self, ref: Reference | None, weak_corners=None, adopted: bool = False):
        """adopted: a lap he drove now became the reference (_adopt). Anything else is a reference found or chosen
        before driving, or none at all: with none, this session is Learning the track (learning_mode)."""
        if weak_corners is not None:
            self.weak_input = list(weak_corners)
        if not adopted:
            self._learn_session = ref is None
        same = ref is not None and ref is self.reference
        self.reference = ref
        self.proven = match_weak(ref, self.weak_input, self.settings.top_n, self.settings.corner_prefix) if ref else []
        if not same:                     # times against an older reference say nothing about this one
            self._loss_hist = []
            self.learned = []
        self._timing(ref)
        self._learn(emit=False)
        self._note_key = None            # 2.20: the corner map changed: his notes are matched to it again
        self._closs_last = {} if not same else self._closs_last
        self.weak = self.proven or self.learned
        self._obs = {}
        self._marker_done = set()
        self._beep_done = set()
        self._map_focus()
        if ref is not None:
            src = ref.source or {}
            self._emit("reference", lap_time=round(ref.lap_time, 3), source=src.get("kind"), ref_lap=src.get("lap"),
                       ref_date=src.get("date"), ref_text=ref.describe(), corners=len(ref.corners),
                       focus=[{"name": w.name, "id": w.corner.id, "lost_s": w.lost_s, "phrase": w.phrase,
                               "brake_m": round(w.corner.brake_m) if w.corner.brake_m is not None else None}
                              for w in self.weak])

    # ---- 2.17.2: corner times, sectors and focus corners learned from this session ----
    def _timing(self, ref):
        self._segs, self._sectors = {}, []
        self._hard = hard_zones(ref)
        if ref is None:
            return
        L = ref.length_m
        for c in ref.corners:
            lo = min(c.start_rel_m, (c.brake_rel_m - 60.0) if c.brake_rel_m is not None else c.start_rel_m)
            a, b = c.apex_pct + lo / L, c.apex_pct + c.end_rel_m / L
            if 0.0 <= a < b < 1.0:       # a corner across the line isn't timed
                self._segs[c.num] = (a, b)
        lt = ref.lap_time
        tt = np.maximum.accumulate(np.asarray(ref.time, float))
        if len(tt) and tt[-1] > 0:
            # each boundary at a third of the lap by time, moved back to the exit of the corner before it, so the
            # sector's word comes at the start of a straight
            for k in (1, 2):
                third = float(np.interp(lt * k / 3.0, tt, ref.pct))
                exits = [b for _a, b in self._segs.values() if third - 0.15 < b <= third]
                self._sectors.append(max(exits) if exits else third)

    def _learn(self, emit: bool = True):
        """Focus corners from this session: when no race review or Corner coach card names any, the corners where he
        lost the most against the reference over his last clean laps (LEARN_LAPS), top Settings level live_top."""
        st = self.settings
        n = int(st.lv("live_top") or 0)
        ref = self.reference
        old = [w.corner.num for w in self.learned]
        if ref is None or self.proven or n <= 0 or not self._loss_hist:
            self.learned = [] if (ref is None or self.proven or n <= 0) else self.learned
        else:
            tot: dict = {}
            for laploss in self._loss_hist[-LEARN_LAPS:]:
                for num, x in laploss.items():
                    tot.setdefault(num, []).append(x)
            avg = sorted(((sum(v) / len(v), num) for num, v in tot.items()), key=lambda x: (-x[0], x[1]))
            self.learned = [Weak(corner=ref.corner(num), lost_s=round(a, 3), name=f"{st.corner_prefix}{num}",
                                 source="session")
                            for a, num in avg[:n] if a >= LIVE_MIN_LOSS_S and ref.corner(num) is not None]
        if not self.proven:
            self.weak = self.learned
        new = [w.corner.num for w in self.learned]
        if emit and new != old:
            self._emit("focus_learned", why=f"lost the most against the reference over the last "
                                            f"{min(LEARN_LAPS, len(self._loss_hist))} clean laps",
                       focus=[{"name": w.name, "id": w.corner.id, "lost_s": w.lost_s} for w in self.learned])

    def focus_info(self) -> dict:
        """What the coach focuses on and why, in plain words (the Coach page's status)."""
        st = self.settings
        names = [w.name for w in self.weak]
        listed = ", ".join(names[:-1]) + (" and " if len(names) > 1 else "") + names[-1] if names else ""
        if st.ask_only:
            return {"source": None, "names": [], "level": st.level,
                    "text": "Only when you ask: the coach stays quiet until you ask for your pace (Ctrl+Shift+P or "
                            "your wheel button)."}
        if st.notes_only:
            return {"source": None, "names": [], "level": st.level,
                    "text": "Notes only: the coach says nothing but your own corner notes before their corners "
                            "(and your pace when you ask)."}
        if self.proven:
            src = "corner coach" if any(w.source == "corner coach" for w in self.proven) else "reviews"
            text = (f"Focus: {listed}, from the Corner coach." if src == "corner coach"
                    else f"Focus: {listed}, from your race reviews.")
        elif self.learned:
            src = "session"
            n = min(LEARN_LAPS, len(self._loss_hist))
            text = (f"Learning from this session: {listed} cost you the most against your reference over your last "
                    + (f"{n} clean laps." if n > 1 else "clean lap."))
        elif self.reference is None:
            src = None
            text = ("No reference lap yet. Drive one clean lap and it becomes the reference." if st.quiet else
                    "Building your reference: drive one clean lap and the coach compares every lap after it.")
        elif st.quiet:
            src = None
            text = "No focus corners from your reviews or the Corner coach, so only lap times are said."
        else:
            src = "session"
            text = ("Learning from this session: after your next clean lap the coach picks the corners where you "
                    "lose the most.")
        return {"source": src, "names": names, "text": text, "level": st.level}

    def set_focus(self, focus):
        """2.14: the practice plan's "one thing this session" (practiceplan.LiveFocus or None). After each clean pass
        through its corner it may give a short progress cue ("Good, 84"), queued as that corner's feedback so every
        limit above (gap, per lap, per corner, silence rules) applies to it.

        The plan names its corner by the corner coach's numbering, which can differ from the reference lap's (an
        extra kink found on one lap shifts every number after it), so the corner is matched to the reference's own by
        its apex (focus_corner, within max(60 m, 2% of the lap), as match_weak does). Its cue is keyed and limited on
        that reference corner, and when it is also a weak corner the plan's cue takes the place of that corner's
        weak-corner cues (its brake marker and its feedback: never two cues about one corner, never two that
        disagree). No match: the pass is still measured, nothing is said."""
        self.focus = focus
        self._map_focus()

    def _focus_owns(self, num) -> bool:
        """The practice plan watches this reference corner: its cue replaces the weak-corner marker and feedback."""
        fc = self.focus_corner
        return fc is not None and fc.num == num and bool(getattr(self.focus, "active", True))

    def _map_focus(self):
        self.focus_corner = None
        f, ref = self.focus, self.reference
        if f is None or ref is None or not ref.corners:
            return
        try:
            zone = getattr(f, "zone", None) or {}
            apex = float(zone["apex_m"])
            L = ref.length_m
            pos = apex % L / L
            best = min(ref.corners, key=lambda c: abs(_rel_m(pos, c.apex_pct, L)))
            if abs(_rel_m(pos, best.apex_pct, L)) <= max(60.0, 0.02 * L):
                self.focus_corner = best
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            self.focus_corner = None

    def mute(self):
        self.settings.muted = True
        self.queue.clear("muted")
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

    # ---- 2.20: recording a corner note, "use this lap", Learning the track ----
    def set_recording(self, on: bool):
        """He is holding the note button: no cue starts, what is waiting is forgotten and what is being said stops
        (the microphone must not hear it). Any thread."""
        self.recording = bool(on)
        if on:
            self.queue.clear("recording a note")
            self._cancel = True

    def use_last_lap(self) -> bool:
        """"Use this lap" (Ctrl+Shift+L or a wheel button): the lap he just finished becomes the reference, when it
        was a clean one. Answered on the next sample (use_lap_note says what happened). Any thread."""
        self._use_lap = True
        return True

    def learning_info(self) -> dict:
        """What Learning the track looks like in the window: {"active", "clean_laps", "has_reference", "text"}. Active
        until a lap from his telemetry files is the reference: first with no reference ("N clean laps yet"), then
        while the reference is his own best of this session."""
        ref = self.reference
        n = self._clean_laps
        plural = "s" if n != 1 else ""
        if ref is None:
            return {"active": True, "clean_laps": n, "has_reference": False,
                    "text": f"Learning the track: {n} clean lap{plural} yet. Your first clean lap becomes the reference."}
        if self._learn_session and (ref.source or {}).get("kind") == "live":
            return {"active": True, "clean_laps": n, "has_reference": True,
                    "text": f"Learning the track: your best so far is {fmt_lap(ref.lap_time)} "
                            f"({n} clean lap{plural}). A quicker clean lap replaces it."}
        return {"active": False, "clean_laps": n, "has_reference": True, "text": ""}

    def learning_mode(self) -> bool:
        """Corners are compared with his own best of this session: there was no reference when the car and track were
        set, and the reference is now a lap he drove in this run."""
        ref = self.reference
        return bool(ref is not None and self._learn_session and (ref.source or {}).get("kind") == "live"
                    and self.settings.learn_cues)

    def _do_use_lap(self, t: float):
        ll = self.last_lap
        cand = None
        if self._pending is not None:
            cand = (self._pending.lap, self._pending.measured)
        elif self._last_clean is not None and ll and ll.get("clean") and self._last_clean[0].num == ll.get("lap"):
            cand = self._last_clean
        if cand is None:
            why = ("No lap is finished yet, so there is no lap to use." if not ll else
                   "Your last lap was not a clean lap (off track, an incident or the pits), so it can't be the "
                   "reference.")
            self.use_lap_note = {"ok": False, "text": why, "wall": round(time.time(), 1)}
            self._emit("asked", why="use this lap: " + why)
            return
        lap, lt = cand
        old = self.reference
        if old is not None and (old.source or {}).get("lap") == lap.num and abs(old.lap_time - lt) < 0.01:
            self.use_lap_note = {"ok": True, "text": f"Lap {lap.num} ({fmt_lap(lt)}) is already the reference.",
                                 "wall": round(time.time(), 1)}
            return
        self.keep_reference = None
        self._adopt(lap, lt)
        if self.reference is not old:
            self.use_lap_note = {"ok": True, "text": f"Lap {lap.num} ({fmt_lap(lt)}) is the reference now.",
                                 "wall": round(time.time(), 1)}
            self._emit("asked", why=f"use this lap: lap {lap.num} is the reference")
            if self.settings.lv("first_laps"):
                self._push(Cue(text=reference_set_text(lt, short=self.settings.lap_times == "short"),
                               priority=PRIORITY[KIND_INFO], key="info:refset", t=t, kind=KIND_INFO, expires=t + 45.0,
                               why="you chose this lap as the reference", measured={"lap": lap.num,
                                                                                 "lap_time": round(lt, 3)}))
        else:
            why = (self.events[-1]["text"] if self.events else "its corners did not match the old reference")
            self.use_lap_note = {"ok": False, "text": f"That lap couldn't be used: {why}.", "wall": round(time.time(), 1)}

    # ---- 2.17.2: "How's my pace?" and cues checked again just before they play ----
    def ask(self):
        """He asked "How's my pace?" (any thread): answered on the next sample, where it fits (pace_text)."""
        self._ask = True

    def pace_text(self) -> str:
        """The answer: the last clean lap against the reference and the corner that cost the most there
        ("Down three tenths, most in T4"), "New best, up a tenth", "Good, on pace" (within a tenth), or, before
        there is one, "Building your reference" / "Reference set, ..." / "No clean lap yet"."""
        st, ref = self.settings, self.reference
        row = next((r for r in reversed(self.lap_log) if r.get("delta") is not None), None)
        if row is None:
            if ref is None:
                return BUILDING_TEXT
            return reference_set_text(ref.lap_time, short=st.lap_times == "short") if ref.source.get("kind") == "live" \
                else NO_CLEAN_TEXT
        d = float(row["delta"])
        if d < 0:
            return "New best" + (f", up {delta_words(d)}" if st.delta_min_s - 1e-9 <= -d < 2.05 else "")
        if d < st.delta_min_s - 1e-9:
            return "Good, on pace"
        if d >= 2.05:
            return FAR_DOWN_TEXT
        most = row.get("most")
        return f"Down {delta_words(d)}" + (f", most in {st.corner_prefix}{most}" if most else "")

    def why_invalid(self, cue: Cue, state, starting: bool = True) -> str | None:
        """None while cue still holds for this telemetry sample (a dict of channels), else why not, in a few words.
        Called from the voice's thread right before a cue plays (LiveRunner), so it only reads."""
        fn = cue.still_valid
        if fn is None or not state:
            return None
        try:
            return fn(state, starting)
        except Exception:  # noqa: BLE001  (a failed check never silences the coach: the rules at pop time held)
            return None

    def note_invalid(self, cue: Cue, why: str):
        """A cue the voice did not play because it was no longer valid (the cue log says so)."""
        self.invalid_drops += 1
        self._emit("dropped", cue, dropped="no longer valid: " + why)

    def _nonurgent(self, c: Cue) -> bool:
        """Cues not about a corner just driven or coming up: they wait for a place outside the hard zones."""
        return c.kind in (KIND_SECTOR, KIND_LAP, KIND_ASK) or c.key.startswith("good:") or c.key == "info:refset"

    def _hard_at(self, pct: float):
        """(inside a hard zone, metres to the next hard zone's start) at pct; (False, inf) with none."""
        if not self._hard or self.reference is None:
            return False, math.inf
        L = self.reference.length_m
        ahead = math.inf
        for a, b in self._hard:
            if (a <= pct <= b) if a <= b else (pct >= a or pct <= b):
                return True, 0.0
            ahead = min(ahead, ((a - pct) % 1.0) * L)
        return False, ahead

    def _next_zone(self, num: int):
        """(start pct, end pct) of the braking and apex zone of the corner after corner num, or None."""
        ref = self.reference
        if ref is None or len(ref.corners) < 2:
            return None
        cs = sorted(ref.corners, key=lambda c: c.apex_pct)
        i = next((k for k, c in enumerate(cs) if c.num == num), None)
        if i is None:
            return None
        c = cs[(i + 1) % len(cs)]
        L = ref.length_m
        lo = c.brake_rel_m if c.brake_rel_m is not None else min(-60.0, 0.5 * c.start_rel_m)
        return (c.apex_pct + lo / L) % 1.0, (c.apex_pct + max(30.0, 0.3 * c.end_rel_m) / L) % 1.0

    def _validity(self, cue: Cue):
        """still_valid for a cue made now (see Cue): its kind's own rule, and when it is about to start, room before
        the next brake point (and, for a cue that is not about a corner, before the next hard zone)."""
        kind, key = cue.kind, cue.key
        seq, p0 = self._sector_seq, self._pct
        lapn = self._lap.num if self._lap is not None else None
        num = int(cue.corner[1:]) if cue.corner and cue.corner[1:].isdigit() else None
        zone = self._next_zone(num) if num is not None and kind in (KIND_FEEDBACK, KIND_CORNER) else None
        if zone is not None and p0 is not None:
            a, b = zone
            if (a <= p0 <= b) if a <= b else (p0 >= a or p0 <= b):
                zone = (p0, b)                            # made inside the next zone already: no longer valid
        ref = self.reference

        def own(state):
            pct = float(state.get("LapDistPct") or 0.0)
            if kind == KIND_NOTE and num is not None and ref is not None:     # 2.20: ends 1 s before the corner's braking
                c = ref.corner(num)
                if c is None:
                    return None
                d = ((self._anchor_pct(c) - pct) % 1.0) * ref.length_m
                if d >= ref.length_m / 2:
                    return "passed its brake point"
                if d / max(float(state.get("Speed") or 0.0), 5.0) < self._need_s(cue.text):
                    return "would not end 1 s before its brake point"
                return None
            if kind == KIND_MARKER and num is not None and ref is not None:
                c = ref.corner(num)
                if c is None or c.brake_pct is None:
                    return None
                d = ((c.brake_pct - pct) % 1.0) * ref.length_m
                if d >= ref.length_m / 2:
                    return "passed its brake point"
                if d / max(float(state.get("Speed") or 0.0), 5.0) < self._need_s(cue.text):
                    return "would not end 1 s before its brake point"
                return None
            if zone is not None and p0 is not None:
                dp = (pct - p0) % 1.0
                if dp < 0.5 and dp >= (zone[0] - p0) % 1.0:
                    return "the next corner's braking zone has started"
                return None
            if kind in (KIND_SECTOR, KIND_LAP):
                return None if self._sector_seq == seq else "a newer sector is done"
            if key == "info:building":
                return None if self.reference is None else "the reference is set"
            if key == "info:refset":
                return None if state.get("Lap") == lapn else "a new lap has started"
            return None

        def still_valid(state, starting=True):
            why = own(state)
            if why or not starting:
                return why
            return self._start_invalid(cue, state)
        return still_valid

    def _start_invalid(self, cue: Cue, state) -> str | None:
        """Can this cue start now, at this sample? The same rules a cue is held back by (see feed), as reasons."""
        st, ref = self.settings, self.reference
        if self.recording:
            return "recording a note"
        if float(state.get("Brake") or 0.0) > st.braking_pedal:
            return "braking"
        clr = state.get("CarLeftRight")
        if clr is not None and int(clr) not in (0, 1):
            return "car alongside"
        if ref is None:
            return None
        pct, L = float(state.get("LapDistPct") or 0.0), ref.length_m
        vv = max(float(state.get("Speed") or 0.0), 5.0)
        need = self._need_s(cue.text)
        for c in ref.corners:
            rel = _rel_m(pct, c.apex_pct, L)
            lo = c.brake_rel_m if c.brake_rel_m is not None else min(-60.0, 0.5 * c.start_rel_m)
            if lo <= rel <= max(30.0, 0.3 * c.end_rel_m):
                return "in a corner"
            if c.brake_pct is not None:
                d = ((c.brake_pct - pct) % 1.0) * L
                if d < L / 2 and d / vv < need:
                    return "would not end 1 s before a brake point"
        if HARD_GATING and self._nonurgent(cue):
            inside, ahead = self._hard_at(pct)
            if inside:
                return "in a hard zone"
            if (self.duration(cue.text) + self._lat()) * vv > ahead:
                return "would run into a hard zone"
        return None

    # ---- Coach 2.0: the cue log's events ----
    def _emit(self, ev: str, cue: Cue | None = None, **kw):
        fn = self.on_event
        if fn is None:
            return
        d = {"ev": ev, **self._ctx}
        if cue is not None:
            d.update(kind=cue.kind, text=cue.text, corner=cue.corner, why=cue.why or None,
                     measured=cue.measured or None, made_t=round(cue.t, 2))
        d.update(kw)
        try:
            fn({k: v for k, v in d.items() if v is not None})
        except Exception:  # noqa: BLE001  (the log never stops the coach)
            pass

    def _on_drop(self, cue: Cue, why: str):
        self._emit("dropped", cue, dropped=why)

    def _push(self, cue: Cue):
        if self.settings.ask_only and cue.kind != KIND_ASK:
            self._emit("not_made", cue, why="Only when I ask")   # 2.17.2: nothing but the answer he asks for
            return
        if self.settings.notes_only and cue.kind not in (KIND_ASK, KIND_NOTE):
            self._emit("not_made", cue, why="Notes only")        # 2.20: nothing but his own notes
            return
        try:   # 2.18: every speed it says in the one unit from Settings > Units
            from . import units
            cue.text = units.convert_text(cue.text, self.settings.units)
        except Exception:  # noqa: BLE001
            pass
        if cue.still_valid is None:
            cue.still_valid = self._validity(cue)
        self.queue.push(cue)
        self._emit("made", cue)

    def corner_state(self) -> dict:
        """{corner num: "fixed"} for the corners left alone because he has fixed them (for status and tests)."""
        return {n: "fixed" for n in sorted(self._fixed)}

    def duration(self, text: str) -> float:
        """Seconds of session time the cue takes to say. 2.18: a rehearsal played at 2x or 4x (time_scale) moves the
        car 2 or 4 times as far while the words play at their own speed, so the length is scaled to match."""
        return self._real_duration(text) * max(1.0, float(self.time_scale or 1.0))

    def _real_duration(self, text: str) -> float:
        nd = self._note_dur.get(text)
        if nd:                                   # 2.20: one of his own notes: its recorded length
            return float(nd)
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
            if self.settings.units == "kmh":   # 2.18: the same speeds, said in km/h
                out += [speed_text(w.name, n) for n in range(max(1, int(math.ceil(self.settings.slower_kmh))), 21)]
            else:
                out += [speed_text(w.name, n) for n in range(max(1, int(self.settings.slower_mph())), 13)]
            if w.phrase:
                from . import units
                out.append(units.convert_text(w.phrase, self.settings.units))
        for k in range(1, 10):
            out += [f"Down {delta_words(k / 10)}", f"New best, up {delta_words(k / 10)}"]
        out.append("New best")
        out += ["Good, on pace", NO_CLEAN_TEXT, FAR_DOWN_TEXT]      # 2.17.2 "How's my pace?"
        for w in self.weak:
            out += [f"Down {delta_words(k / 10)}, most in {w.name}" for k in range(1, 10)]
        st = self.settings
        if st.lv("first_laps"):
            out.append(BUILDING_TEXT)
        if self.reference is not None and not st.quiet:     # 2.17.2: every corner, and the sectors
            pre = st.corner_prefix
            for c in self.reference.corners:
                name = f"{pre}{c.num}"
                out += [early_text(name)] + [corner_time_text(name, k / 10) for k in range(1, 6)]
                out += [good_text(name, k / 10) for k in range(1, 4)]
            for k in range(2):
                out += [sector_text(k, d / 10) for d in (-3, -2, -1, 1, 2, 3, 4, 5)]
            lo = int(math.ceil(st.slower_kmh)) if st.units == "kmh" else int(st.slower_mph())
            for c in self.reference.corners:
                out += [speed_text(f"{pre}{c.num}", n) for n in range(max(1, lo), 21 if st.units == "kmh" else 9)]
        if self.reference is not None and st.learn_cues and not st.quiet:     # 2.20 Learning the track
            for c in self.reference.corners:
                name = f"{st.corner_prefix}{c.num}"
                out += [learn_off_text(name), learn_on_text(name)]
                out += [learn_speed_text(name, k) for k in range(3, 9)]
                out += [learn_brake_text(name, m, way) for m in (10, 15, 20, 25, 30) for way in (True, False)]
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
                          "phrase": w.phrase, "source": w.source,
                          "plan_owns": self._focus_owns(w.corner.num)} for w in self.weak],
                "muted": self.settings.muted, "quiet_reason": self.quiet_reason,
                "last_lap": self.last_lap, "spoken": [c.to_dict() for c in self.spoken[-10:]],
                "events": self.events[-10:],
                "focus": self._focus_status(), "level": self.settings.level, "focus_info": self.focus_info(),
                "learning": self.learning_info(), "use_lap_note": self.use_lap_note, "recording": self.recording}

    def _focus_status(self):
        try:
            fs = self.focus.status() if self.focus is not None else None
        except Exception:  # noqa: BLE001
            return None
        if isinstance(fs, dict):
            fs = dict(fs, **focus_count(getattr(self.focus, "passes", None) or [],
                                        self._lap.num if self._lap is not None else None, fs.get("progress")))
            fs["progress"] = fs["count_text"]
        return fs

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
        self._full_since = None
        self._inc_seen = None
        self._note_done = set()
        self._note_planned = set()
        self.queue.clear("session reset")

    def _need_s(self, text: str) -> float:
        st = self.settings
        return self.duration(text) + st.end_before_brake_s + self._lat()

    def _lat(self) -> float:
        """The audio delay in session seconds (2.18: scaled like duration in a faster rehearsal)."""
        return self.settings.audio_latency_s * max(1.0, float(self.time_scale or 1.0))

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
        self._pct = pct
        Lc = self.reference.length_m if self.reference is not None else (self._length_hint or None)
        self._ctx = {"t": round(t, 2), "lap": lapn, "dist_m": round(pct * Lc) if Lc else None,
                     "kmh": round(float(s.get("Speed") or 0.0) * KMH, 1)}
        alongside = clr not in (0, 1)
        if alongside:
            if not self._alongside:                      # a car just came alongside: stop talking, forget queued
                self._cancel = True
                self._emit("cancelled", why="car alongside")
            self.queue.clear("car alongside")
            self._traffic_until = t + st.traffic_hold_s  # quiet until 2 s after it clears
            self._last_along_t = t
        self._alongside = alongside
        on_track = on and surf == ibt.ON_TRACK and not pit

        # ---- the line ----
        lap = self._lap
        if lap is None or lapn != lap.num:
            self._sector_seq += 1                         # 2.17.2: a lap's times are outdated at the line
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
            self._last_off_t = t
        if inc != lap.inc0:
            lap.incident = True
        if self._inc_seen is not None and inc != self._inc_seen:
            self._last_inc_t = t
        self._inc_seen = inc
        thr = float(s.get("Throttle") or 0.0)
        if thr >= 0.9 and brk <= st.braking_pedal:
            if self._full_since is None:
                self._full_since = t
        else:
            self._full_since = None
        if lap.valid and not lap.pit and len(lap.trace) < 20000:
            lap.trace.append((t, pct, v, brk, float(s.get("Throttle") or 0.0), float(s.get("Gear") or 0.0)))
        if self._pending is not None:
            self._lap_result(t, last_lap_time)
        if self._use_lap:                                 # 2.20 "use this lap"
            self._use_lap = False
            self._do_use_lap(t)
        start_lap = is_race(stype) and lapn <= 1
        coachable = lap.valid and not lap.pit and not start_lap

        # ---- corners ----
        ref = self.reference
        L = ref.length_m if ref else 0.0
        self.live_delta = None                            # 2.18 driving view: this lap so far against the reference
        if ref is not None and lap.valid and not lap.pit and lap.t0 is not None and len(ref.time):
            self.live_delta = round((t - lap.t0) - float(np.interp(pct, ref.pct, ref.time)), 2)
        dists = {}                                        # corner num -> metres to its brake point (ahead only)
        busy = False                                      # inside a corner's braking or apex zone
        beep = None
        hard_in, hard_m = self._hard_at(pct)              # 2.17.2 hard zones
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
            self._notes_step(pct, v, t, coachable, lap, stype)
            if self._prev_pct is not None and lap.valid and not lap.pit and lap.t0 is not None:
                self._timed(self._prev_pct, pct, t, lap, coachable)     # 2.17.2 corner times and sectors
        elif (st.lv("first_laps") and self._building is None and not st.muted and on and not alongside
              and surf in (ibt.ON_TRACK, ibt.IN_PIT_STALL, ibt.APPROACHING_PITS) and v > 3.0
              and not (is_race(stype) and (not st.in_races or lapn <= 1))):
            self._building = "made"                       # 2.17.2: no reference yet, on the move
            self._push(Cue(text=BUILDING_TEXT, priority=PRIORITY[KIND_INFO], key="info:building", t=t,
                           kind=KIND_INFO, expires=t + 120.0, why="no reference lap yet: the first clean lap "
                                                                  "becomes the reference"))

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
            fc = self.focus_corner
            if said and st.feedback and fc is not None:      # keyed on the reference's own corner (see set_focus)
                last = (getattr(self.focus, "passes", None) or [{}])[-1]
                self._push(Cue(text=said, priority=PRIORITY[KIND_FEEDBACK], key=f"feedback:{fc.id}", t=t,
                               kind=KIND_FEEDBACK, corner=fc.id, expires=t + st.stale_s,
                               why=f"practice plan: {getattr(self.focus, 'plan', {}).get('metric') or 'focus'} "
                                   f"{'hit' if last.get('hit') else 'missed'}",
                               measured={k: last.get(k) for k in ("value", "hit", "near") if k in last}))

        # ---- 2.17.2 "How's my pace?" ----
        if self._ask:
            self._ask = False
            self.asks += 1
            text = self.pace_text()
            row = next((r for r in reversed(self.lap_log) if r.get("delta") is not None), None) or {}
            self._push(Cue(text=text, priority=PRIORITY[KIND_ASK], key="ask", t=t, kind=KIND_ASK,
                           expires=t + ASK_WAIT_S, why="you asked: How's my pace?",
                           measured={"lap": row.get("lap"), "lap_time": row.get("time"), "delta_s": row.get("delta")}))

        # ---- speak? ----
        self._prev_pct, self._prev_t = pct, t
        reason = self._quiet(st, stype, on_track, alongside, t, coachable, start_lap, lap)
        self.quiet_reason = reason
        if st.muted:
            self.queue.clear("muted", keep_kind=KIND_ASK)     # he asked: the answer comes, muted or not
            if not self.queue.items:
                return []
        if beep is not None and reason is None:
            return [Cue(text="", priority=0, key=f"beep:{beep}", t=t, kind=KIND_BEEP, corner=beep, spoken_t=t)]
        vv = max(v, 5.0)

        def blocked(c: Cue) -> str | None:
            """The rule that keeps this cue back now, in a few words (the cue log says it when a cue is dropped)."""
            if self.recording:                            # 2.20: nothing starts while he records a note
                return "recording a note"
            if c.kind == KIND_ASK:                        # 2.17.2: he asked; only the safety rules hold it back
                if alongside or t < self._traffic_until:
                    return "car alongside"
                if brk > st.braking_pedal:
                    return "braking"
                if t < self._speaking_until:
                    return "still talking"
                if ref is None:
                    return None
                if busy:
                    return "in a corner"
                need = self._need_s(c.text)
                if any(dist / vv < need for dist in dists.values()):
                    return "no room before a brake point"
                if HARD_GATING and (hard_in or (self.duration(c.text) + self._lat()) * vv > hard_m):
                    return "in a hard zone" if hard_in else "no room before a hard zone"
                return None
            if c.kind == KIND_INFO and ref is None:
                return self._info_blocked(st, t, brk, v, pit, on_track, alongside, stype, lapn)
            if reason and not (c.kind in (KIND_LAP, KIND_INFO)
                               and reason in ("Out lap or in lap", "Lap not seen from the start")):
                return reason.lower()
            if t - self._last_spoken_t < st.gap():
                return "rate limit"
            if brk > st.braking_pedal:                    # never under braking ...
                return "braking"
            if busy:                                      # ... or mid-corner
                return "in a corner"
            need = self._need_s(c.text)
            for num, dist in dists.items():              # must END end_before_brake_s before every brake point
                if dist / vv < need:
                    return "no room before a brake point"
            if HARD_GATING and self._hard and self._nonurgent(c):    # 2.17.2: not in or into a hard zone
                if hard_in:
                    return "in a hard zone"
                if (self.duration(c.text) + self._lat()) * vv > hard_m:
                    return "no room before a hard zone"
            if st.quiet:
                if c.kind in (KIND_MARKER, KIND_FEEDBACK):
                    num = int(c.corner[1:]) if c.corner else None
                    if lap.cues >= st.max_per_lap:
                        return "lap limit"
                    if num in lap.corners:
                        return "corner limit"
            else:
                if c.kind not in (KIND_INFO, KIND_NOTE) and lap.said >= st.per_lap():     # his own notes: no limit
                    return "lap limit"
                if c.kind in (KIND_MARKER, KIND_FEEDBACK, KIND_CORNER) and c.corner \
                        and int(c.corner[1:]) in lap.corners:
                    return "corner limit"
                if c.key.startswith("good:") and lap.positives >= int(st.lv("positives")):
                    return "good-cue limit"
            if c.kind != KIND_MARKER and st.markers and coachable:   # keep room for a weak corner's focus cue
                for w in self.weak:
                    d = dists.get(w.corner.num)
                    if d is None or w.corner.num in self._marker_done or w.corner.num in self._note_planned \
                            or not self._marker_wanted(w, lap):
                        continue                          # (2.20: a corner with its note coming gets no marker)
                    trig, _late = self._marker_span(w, vv)
                    if d - trig < vv * (st.gap_s + need):
                        return "room kept for a brake marker"
            return None

        def allowed(c: Cue) -> bool:
            b = blocked(c)
            if b is not None:
                c.blocked = b
            return b is None

        self.queue.prune(lambda c: self.why_invalid(c, s, starting=False))   # 2.17.2: outdated cues go now
        cue = self.queue.pop(t, allowed)
        if cue is None:
            return []
        why = self.why_invalid(cue, s, starting=True)
        if why:
            self.queue._dropped(cue, "no longer valid: " + why)
            return []
        self._last_spoken_t = t
        self._speaking_until = t + self.duration(cue.text) + self._lat()
        cue.spoken_t = t
        nxt = min((d / vv for d in dists.values()), default=None)
        if self.skipping:                                 # 2.18 rehearsal skipping ahead: made, never said
            self._emit("dropped", cue, dropped="skipped ahead (rehearsal)")
        else:
            self._emit("spoken", cue, dur_s=round(self._real_duration(cue.text), 2),
                       next_brake_s=round(nxt, 2) if nxt is not None else None,
                       waited_s=round(t - cue.t, 2))
        if cue.kind in (KIND_MARKER, KIND_FEEDBACK):
            num = int(cue.corner[1:])
            lap.cues += 1
            lap.corners.add(num)
            self._corner_last[num] = (lap.num, cue.kind)
        elif cue.kind == KIND_CORNER and cue.corner:
            lap.corners.add(int(cue.corner[1:]))
        if cue.key.startswith("good:"):
            lap.positives += 1
        if cue.kind == KIND_INFO:
            if cue.key == "info:building":
                self._building = "said"
        elif cue.kind not in (KIND_ASK, KIND_NOTE):       # the answer he asked for, his own note: not the lap's cues
            lap.said += 1
        self.spoken.append(cue)
        del self.spoken[:-50]
        return [cue]

    def _info_blocked(self, st, t, brk, v, pit, on_track, alongside, stype, lapn) -> str | None:
        """2.17.2: with no reference there are no brake points to keep clear of, so "Building your reference" is said
        only where nothing can be asked of him: rolling out of the pits, or after 1.5 s of full throttle on a
        straight; never alongside a car, under braking, in a race he isn't coached in, or on a race's first lap."""
        if is_race(stype) and (not st.in_races or lapn <= 1):
            return "race session"
        if alongside or t < self._traffic_until:
            return "car alongside"
        if t - self._last_spoken_t < st.gap():
            return "rate limit"
        if brk > st.braking_pedal:
            return "braking"
        if pit:
            return None if 3.0 < v < 30.0 else "on pit road"
        if not on_track:
            return "off track"
        if self._full_since is None or t - self._full_since < 1.5 or v < 15.0:
            return "waiting for a straight"
        return None

    def _quiet(self, st, stype, on_track, alongside, t, coachable, start_lap, lap) -> str | None:
        if self.recording:
            return "Recording a note"
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

    # -- 2.20 his own corner notes --
    def _anchor_pct(self, c: Corner) -> float:
        """Where a note must have ended 1 s before: the corner's brake point, or where its zone begins when it has none."""
        if c.brake_pct is not None:
            return c.brake_pct
        L = self.reference.length_m
        return (c.apex_pct + min(-60.0, 0.5 * c.start_rel_m) / L) % 1.0

    def _notes_refresh(self):
        """His notes for this track matched to the reference's corners (one note a corner, the newest on one), again
        whenever a note changes or the reference does."""
        store, ref = self.notes, self.reference
        if store is None or ref is None or not self.track_key:
            self._note_map, self._note_key = {}, None
            return
        key = (getattr(store, "version", 0), id(ref))
        if key == self._note_key:
            return
        try:
            if store.tag_untagged(self.track_key, ref.corners, ref.length_m):
                key = (getattr(store, "version", 0), id(ref))
            self._note_map = store.by_corner(self.track_key, ref.corners, ref.length_m)
        except Exception as e:  # noqa: BLE001
            self._note_map = {}
            self._log(f"livecoach: corner notes skipped ({e.__class__.__name__})")
        self._note_key = key

    def _notes_step(self, pct: float, v: float, t: float, coachable: bool, lap: _Lap, stype: str):
        """Before each corner that has a note: at the moment the note would just end 1 s before the braking point,
        decide whether it is played this lap (the learning plan), and make its cue. Said the way a brake marker is: it
        waits for a free spot, is checked again just before it starts, and is dropped when it can't finish in time."""
        st, ref = self.settings, self.reference
        self._notes_refresh()
        if not self._note_map or ref is None or not st.notes or st.ask_only:
            return
        if is_race(stype) and not st.in_races:           # races are for racing: the note neither plays nor counts
            return
        L = ref.length_m
        vv = max(v, 5.0)
        for num, note in list(self._note_map.items()):
            c = ref.corner(num)
            if c is None:
                continue
            anchor = self._anchor_pct(c)
            d = ((anchor - pct) % 1.0) * L
            if d >= L / 2:                               # behind us: ready for next lap
                self._note_done.discard(num)
                self._note_planned.discard(num)
                continue
            if num in self._note_done:
                continue
            name = f"{st.corner_prefix}{num}"
            text = NOTE_TEXT.format(name)
            self._note_dur[text] = float(note.get("seconds") or 0.0)
            vref = max(vv, float(np.interp(anchor, ref.pct, ref.speed, period=1.0)))
            late = vref * self._need_s(text)
            trig = late + vref * NOTE_WINDOW_S
            if not late <= d <= trig:
                continue
            self._note_done.add(num)
            if not coachable:
                continue
            if note.get("run") == self.run_id and note.get("lap") is not None and lap.num <= int(note["lap"]):
                continue                                 # recorded on this lap (or later): it plays from the next lap
            fresh = self.notes.get(note["id"]) or note
            passes = int(fresh.get("passes") or 0) + self._pass_local.get(note["id"], 0)
            lost = self._closs_last.get(num)
            play, phase = cornernotes.plan_says(passes, None if lost is None else lost >= cornernotes.PLAN_LOSS_MIN_S)
            if self.count_notes:
                try:
                    self.notes.mark_pass(note["id"])
                except Exception as e:  # noqa: BLE001
                    self._log(f"livecoach: corner note count skipped ({e.__class__.__name__})")
            else:                                        # a rehearsal: hear the plan move on without touching the counts
                self._pass_local[note["id"]] = self._pass_local.get(note["id"], 0) + 1
            if not play:
                self._emit("not_made", kind=KIND_NOTE, text=text, corner=c.id, why=f"note plan: not this lap ({phase})",
                           measured={"note_id": note["id"], "passes": passes})
                continue
            self._note_planned.add(num)
            self._push(Cue(text=text, priority=PRIORITY[KIND_NOTE], key=f"note:{c.id}", t=t, kind=KIND_NOTE,
                           corner=c.id, expires=t + max(0.3, (d - late) / vv),
                           why=f"your note for {name}, lap {passes + 1} since you recorded it ({phase})",
                           measured={"note_id": note["id"], "seconds": float(note.get("seconds") or 0.0),
                                     "passes": passes, "plan": phase, "to_brake_m": round(d)}))

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
        if n in self._fixed:                     # Coach 2.0: he has fixed it; nothing until it slips again
            return False
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
        if not st.markers or not coachable or c.num in self._marker_done or self._focus_owns(c.num):
            return
        if c.num in self._note_planned:                  # 2.20: his own note for this corner is coming: no marker too
            return
        vv = max(v, 5.0)
        trig, late = self._marker_span(w, vv)
        if late <= d <= trig:
            self._marker_done.add(c.num)
            if self._marker_wanted(w, lap):
                err = self._corner_err.get(c.num)
                why = ("focus corner, first look this session" if err is None else
                       "focus corner, off the reference last time there")
                self._push(Cue(text=marker_text(w.name), priority=PRIORITY[KIND_MARKER],
                               key=f"marker:{c.id}", t=t, kind=KIND_MARKER, corner=c.id,
                               expires=t + (d - late) / vv, why=why,
                               measured={"brake_point_m": round(-c.brake_rel_m) if c.brake_rel_m is not None else None,
                                         "to_brake_m": round(d), "lost_s": w.lost_s}))
            elif c.num in self._fixed:
                self._emit("not_made", kind=KIND_MARKER, text=marker_text(w.name), corner=c.id,
                           why="corner fixed")

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
        mph = mph_down(down)
        slow = down / KMH_PER_MPH >= st.slower_mph() - 1e-9
        err = slow or early >= st.early_m
        prev = self._corner_err.get(c.num)
        self._corner_err[c.num] = [err, bool(prev and prev[0])]
        measured = {"min_kmh": round(o.min_v * KMH, 1), "ref_min_kmh": round(c.min_kmh, 1), "down_kmh": round(down, 1),
                    "brake_point_m": round(-o.brake_rel) if o.brake_rel is not None else None,
                    "ref_brake_point_m": round(-c.brake_rel_m) if c.brake_rel_m is not None else None,
                    "early_m": round(early, 1), "down_mph": mph}
        if self._judge(c, err, measured):
            return                                        # Coach 2.0: a corner he has fixed is left alone
        if not err or not st.feedback:
            return
        if self._focus_owns(c.num):
            return                                        # the practice plan's cue speaks for this corner (2.14)
        last = self._corner_last.get(c.num)
        persists = bool(prev and prev[0])
        if last is not None and lap.num - last[0] < st.repeat_laps and not persists:
            self._emit("not_made", kind=KIND_FEEDBACK, corner=c.id, why="cued recently", measured=measured)
            return
        if w.phrase and phrase_fits(w, early, st.early_m):   # the corner coach's advice for this corner (2.13)
            text, why = w.phrase, "corner coach advice; this pass " + (
                f"{mph} mph slow" if slow else f"braked {early:.0f} m early")
        elif slow:
            said = mph if st.units != "kmh" else max(1, int(round(down)))   # 2.18: the number in the chosen unit
            text, why = speed_text(w.name, int(said)), f"minimum speed {mph} mph under the reference"
        else:
            text, why = early_text(w.name), f"braked {early:.0f} m before the reference"
        self._push(Cue(text=text, priority=PRIORITY[KIND_FEEDBACK], key=f"feedback:{c.id}", t=t,
                       kind=KIND_FEEDBACK, corner=c.id, expires=t + st.stale_s, why=why, measured=measured))
        self._fb_made[c.num] = t

    # -- 2.17.2: every corner and sector timed against the reference --
    def _timed(self, p0: float, p1: float, t: float, lap: _Lap, coachable: bool):
        """Called each sample (p0 -> p1 this lap). When the car leaves a timed corner: the time lost or gained there
        against the reference (and, at a sector boundary, the sector's), and maybe a word about it."""
        if p1 <= p0:
            return
        hits = [(b, num) for num, (_a, b) in self._segs.items() if p0 < b <= p1]
        secs = [(b, k) for k, b in enumerate(self._sectors) if p0 < b <= p1]
        if not hits and not secs:
            return
        tr = np.asarray(lap.trace, float)
        if len(tr) < 5:
            return
        pp = np.maximum.accumulate(tr[:, 1])
        for _b, num in hits:
            self._corner_pass(num, tr, pp, t, lap, coachable)
        for b, k in secs:
            self._sector_seq += 1                         # 2.17.2: older sector and lap times are outdated
            self._sector_pass(k, b, tr, pp, t, lap, coachable)

    def _clean_since(self, t0: float, lap: _Lap) -> bool:
        return (self._last_along_t < t0 - 0.5 and self._last_off_t < t0 and self._last_inc_t < t0
                and not lap.pit)

    def _corner_pass(self, num, tr, pp, t, lap: _Lap, coachable: bool):
        st, ref = self.settings, self.reference
        c = ref.corner(num)
        a, b = self._segs[num]
        if c is None or pp[0] > a + 0.002:
            return                                        # the lap's trace doesn't cover the corner
        tin, tout = np.interp([a, b], pp, tr[:, 0])
        rin, rout = np.interp([a, b], ref.pct, ref.time)
        lost = float((tout - tin) - (rout - rin))
        learn = self.learning_mode()
        if learn and self._last_off_t >= tin and math.isfinite(lost):      # 2.20: he went off track in this corner
            if self._learn_gate(num, coachable):
                self._learn_pass(num, c, tr, pp, a, b, t, lap, off=True)
            return
        if not self._clean_since(tin, lap) or not math.isfinite(lost):
            return
        lap.closs[num] = lost
        self._closs_last[num] = lost
        cmin = st.lv("corner_min_s")
        if st.quiet or cmin is None or not st.feedback or not coachable or self._focus_owns(num):
            return
        if learn:                                         # 2.20 Learning the track: against his best so far
            if self._learn_gate(num, coachable) and self._fb_made.get(num) != t:     # (not twice about one corner)
                self._learn_pass(num, c, tr, pp, a, b, t, lap, off=False)
            return
        if self._fb_made.get(num) == t:
            return                                        # its focus-corner word was just made
        m = (pp >= a) & (pp <= b)
        seg = tr[m]
        down = c.min_kmh - float(seg[:, 2].min()) * KMH if len(seg) else 0.0
        early = 0.0
        if c.brake_rel_m is not None and len(seg) > 1:
            on = np.nonzero((seg[1:, 3] > BRAKE_ON) & (seg[:-1, 3] <= BRAKE_ON))[0]
            if len(on):
                rel = _rel_m(float(pp[m][on[0] + 1]), c.apex_pct, ref.length_m)
                if rel <= 40.0:
                    early = c.brake_rel_m - rel
        name = f"{st.corner_prefix}{num}"
        measured = {"lost_s": round(lost, 3), "min_kmh": round(float(seg[:, 2].min()) * KMH, 1) if len(seg) else None,
                    "ref_min_kmh": round(c.min_kmh, 1), "down_kmh": round(down, 1), "early_m": round(early, 1),
                    "down_mph": mph_down(down)}
        if cmin - 1e-9 <= lost <= CORNER_MAX_S:
            if num in self._fixed:
                return
            re_, rd = early / st.early_m, down / st.slower_kmh
            if max(re_, rd) >= 1.0 and re_ >= rd:
                text, why = early_text(name), f"lost {lost:.2f} s here, braked {early:.0f} m before the reference"
            elif down / KMH_PER_MPH >= st.slower_mph() - 1e-9 and (
                    mph_down(down) if st.units != "kmh" else int(round(down))) <= voicepack.MAX_KMH:
                said = mph_down(down) if st.units != "kmh" else max(1, int(round(down)))   # the number in the chosen unit
                text, why = speed_text(name, said), \
                    f"lost {lost:.2f} s here, minimum speed {mph_down(down)} mph under the reference"
            else:
                text, why = corner_time_text(name, lost), f"lost {lost:.2f} s here against the reference"
            self._push(Cue(text=text, priority=PRIORITY[KIND_CORNER], key=f"corner:{c.id}", t=t, kind=KIND_CORNER,
                           corner=c.id, expires=t + st.stale_s + 1.0, why=why, measured=measured))
        elif cmin - 1e-9 <= -lost <= CORNER_MAX_S and int(st.lv("positives")) > lap.positives:
            self._push(Cue(text=good_text(name, -lost), priority=PRIORITY[KIND_SECTOR], key=f"good:{c.id}", t=t,
                           kind=KIND_CORNER, corner=c.id, expires=t + st.stale_s + 1.0,
                           why=f"gained {-lost:.2f} s here against the reference", measured=measured))

    def _learn_gate(self, num, coachable: bool) -> bool:
        st = self.settings
        return not (st.quiet or st.lv("corner_min_s") is None or not st.feedback or not coachable
                    or self._focus_owns(num))

    def _learn_pass(self, num, c: Corner, tr, pp, a: float, b: float, t: float, lap: _Lap, off: bool):
        """2.20 Learning the track: one pass through a corner against his own best so far in this session (the live
        reference). One word at most: off track (and how much faster he went in than on his best), else braked later or
        earlier than his best (10 m or more, to the nearest 5 m), else the minimum speed (3 mph or more) down on his best.
        A corner he cleaned up since the last word gets "on your best". Never about a corner just spoken about (a lap or
        two apart, unless it is twice as bad)."""
        st, ref = self.settings, self.reference
        L = ref.length_m
        name = f"{st.corner_prefix}{num}"
        m = (pp >= a) & (pp <= b)
        seg = tr[m]
        kmh_unit = st.units == "kmh"

        def said(kmh: float) -> int:                     # a speed difference in km/h as a whole number in his unit
            return max(1, int(round(kmh))) if kmh_unit else mph_down(kmh)
        # speed going in: half way from the brake point (or where the corner starts) to the apex, against his best
        # there (on the straight before the brakes every lap is at the same top speed, so that says nothing)
        pe = c.apex_pct + 0.5 * (c.brake_rel_m if c.brake_rel_m is not None else c.start_rel_m) / L
        d_in = None
        if 0.0 <= pe < 1.0 and pp[0] <= pe < pp[-1]:
            d_in = (float(np.interp(pe, pp, tr[:, 2])) - float(np.interp(pe, ref.pct, ref.speed))) * KMH
        down = c.min_kmh - float(seg[:, 2].min()) * KMH if len(seg) else 0.0
        later = None                                      # metres later than his best (negative: earlier)
        if c.brake_rel_m is not None and len(seg) > 1:
            on = np.nonzero((seg[1:, 3] > BRAKE_ON) & (seg[:-1, 3] <= BRAKE_ON))[0]
            if len(on):
                rel = _rel_m(float(pp[m][on[0] + 1]), c.apex_pct, L)
                if rel <= 40.0:
                    later = rel - c.brake_rel_m
        measured = {"min_kmh": round(float(seg[:, 2].min()) * KMH, 1) if len(seg) else None,
                    "ref_min_kmh": round(c.min_kmh, 1), "down_kmh": round(down, 1),
                    "entry_diff_kmh": round(d_in, 1) if d_in is not None else None,
                    "brake_diff_m": round(later, 1) if later is not None else None}
        thr_kmh = st.slower_mph() * KMH_PER_MPH
        big, text, why = 0.0, None, ""
        if off:
            n = said(d_in) if d_in is not None and d_in >= thr_kmh - 1e-9 else None
            ok_n = n is not None and voicepack.LEARN_SPEEDS[0] <= n <= voicepack.LEARN_SPEEDS[1]
            text = learn_off_text(name, n if ok_n else None)
            why = "off track here" + (f", {n} {'km/h' if kmh_unit else 'mph'} faster in than your best there" if ok_n else "")
            big = 2.0                                     # off track is always worth a word
        else:
            cands = []
            if later is not None and abs(later) >= LEARN_BRAKE_M:
                m5 = int(5 * round(abs(later) / 5.0))
                if m5 in voicepack.LEARN_METRES:
                    cands.append((abs(later) / LEARN_BRAKE_M, learn_brake_text(name, m5, later > 0),
                                  f"braked {m5} m {'later' if later > 0 else 'earlier'} than your best"))
            n = said(down)
            if down >= thr_kmh - 1e-9 and voicepack.LEARN_SPEEDS[0] <= n <= voicepack.LEARN_SPEEDS[1]:
                cands.append((down / thr_kmh, learn_speed_text(name, n),
                              f"minimum speed {mph_down(down)} mph under your best"))
            if cands:
                big, text, why = max(cands, key=lambda x: x[0])
        if text is None:
            prev = self._learn_last.get(num)
            fine = (down < thr_kmh * 0.34 and (later is None or abs(later) < LEARN_BRAKE_M * 0.5)) and not off
            if prev is not None and fine and int(st.lv("positives")) > lap.positives:
                self._learn_last.pop(num, None)           # he put it right: one word, once
                self._push(Cue(text=learn_on_text(name), priority=PRIORITY[KIND_SECTOR], key=f"good:{c.id}", t=t,
                               kind=KIND_CORNER, corner=c.id, expires=t + st.stale_s + 1.0,
                               why="back on your best here after a word about it", measured=measured))
            return
        last = self._learn_last.get(num)
        if last is not None and lap.num - last < st.repeat_laps and big < LEARN_BIG:
            self._emit("not_made", kind=KIND_CORNER, text=text, corner=c.id, why="cued recently", measured=measured)
            return
        self._learn_last[num] = lap.num
        self._push(Cue(text=text, priority=PRIORITY[KIND_CORNER], key=f"corner:{c.id}", t=t, kind=KIND_CORNER,
                       corner=c.id, expires=t + st.stale_s + 1.0, why=why, measured=measured))

    def _sector_pass(self, k, b, tr, pp, t, lap: _Lap, coachable: bool):
        st, ref = self.settings, self.reference
        tb = float(np.interp(b, pp, tr[:, 0]))
        lap.sector_t.append(tb)
        if k == 0:
            t0, r0 = lap.t0, 0.0
        elif len(lap.sector_t) >= 2:
            t0, r0 = lap.sector_t[-2], float(np.interp(self._sectors[k - 1], ref.pct, ref.time))
        else:
            return
        if t0 is None or pp[0] > 0.01 + (self._sectors[k - 1] if k else 0.0) or not self._clean_since(t0, lap):
            return
        d = (tb - t0) - (float(np.interp(b, ref.pct, ref.time)) - r0)
        if not st.lv("sectors") or not st.lap_delta or not coachable or lap.off or lap.incident:
            return
        if st.delta_min_s - 1e-9 <= abs(d) <= st.lap_delta_max_s:
            self._push(Cue(text=sector_text(k, d), priority=PRIORITY[KIND_SECTOR], key=f"sector:{k + 1}", t=t,
                           kind=KIND_SECTOR, expires=t + 10.0, why=f"sector {k + 1} against the reference",
                           measured={"sector": k + 1, "delta_s": round(d, 3)}))

    def _judge(self, c: Corner, err: bool, measured: dict) -> bool:
        """Coach 2.0 "already fixed": a corner where he made an error earlier in the session and has since driven
        Settings.fixed_laps clean passes in a row without one is fixed, and nothing more is said about it (no brake
        marker, no word after it). One slip is not enough to bring it back; it comes back (regresses) when 2 of its
        last 3 passes are errors. A corner he drove well from the start is not "fixed": its first error is still
        said. True: this pass's error is not to be cued (the corner counts as fixed)."""
        n = c.num
        v = self._passes.setdefault(n, [])
        v.append(bool(err))
        del v[:-6]
        if err and n not in self._fixed:
            self._erred.add(n)
        k = self.settings.fixed_laps
        if n in self._fixed:
            if sum(v[-3:]) >= 2:
                self._fixed.discard(n)
                self._erred.add(n)
                self._emit("corner_regressed", corner=c.id, why="2 of the last 3 passes off the reference",
                           measured=measured)
                return False
            if err:
                self._emit("not_made", kind=KIND_FEEDBACK, corner=c.id, why="corner fixed (one slip)",
                           measured=measured)
            return True
        if n in self._erred and len(v) >= k and not any(v[-k:]):
            self._fixed.add(n)
            self._erred.discard(n)
            self._emit("corner_fixed", corner=c.id, why=f"{k} good passes in a row", measured=measured)
        return False

    # -- at the line (c) --
    def _end_lap(self, lap: _Lap, tc: float, stype: str, last_lap_time):
        start_lap = is_race(stype) and lap.num <= 1
        clean = lap.valid and not (lap.pit or lap.off or lap.incident or start_lap)
        if clean:
            self._clean_laps += 1                          # 2.20: Learning the track counts them
        lt = (tc - lap.t0) if lap.t0 is not None else None
        most = max(lap.closs.items(), key=lambda kv: (kv[1], -kv[0]), default=None) if clean else None
        self.last_lap = {"lap": lap.num, "time": round(lt, 3) if lt else None, "clean": clean, "delta": None,
                         "most": most[0] if most and most[1] >= PACE_MOST_MIN_S else None}
        self._emit("lap_end", lap=lap.num, lap_time=round(lt, 3) if lt else None, clean=clean,
                   cues=lap.cues if self.settings.quiet else lap.said)
        self.lap_log.append(self.last_lap)
        del self.lap_log[:-30]
        if clean and lap.closs and self.reference is not None:     # 2.17.2: learn the session's focus corners
            self._loss_hist.append(dict(lap.closs))
            del self._loss_hist[:-LEARN_LAPS]
            self._learn()
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
        self._last_clean = (p.lap, lt)                    # 2.20: a clean lap with its time ("use this lap")
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
                self._push(Cue(text=text, priority=PRIORITY[KIND_LAP], key="lap", t=t, kind=KIND_LAP,
                               expires=t + st.lap_cue_wait_s, why="lap against the reference",
                               measured={"lap": p.lap.num, "lap_time": round(lt, 3), "ref_time": round(ref.lap_time, 3),
                                         "delta_s": round(d, 3), "timed_by": self.last_lap.get("timed_by")}))
        # a lap he was given (a teammate's, 2.15) stays the reference for its own car and track: his new bests
        # don't replace it there, but still do in any other car or at any other track
        given_in_use = self.keep_reference is not None and ref is self.keep_reference
        if (ref is None or lt < ref.lap_time) and st.update_reference and not given_in_use:
            self._adopt(p.lap, lt)
            if ref is None and self.reference is not None and st.lv("first_laps"):
                self._push(Cue(text=reference_set_text(lt, short=st.lap_times == "short"), priority=PRIORITY[KIND_INFO], key="info:refset", t=t,
                               kind=KIND_INFO, expires=t + 45.0,            # not urgent: waits for a long straight
                               why="first clean lap: it is now the reference",
                               measured={"lap": p.lap.num, "lap_time": round(lt, 3)}))

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
                                     source=({"kind": "live", "lap": lap.num, "date": self.date_hint[:10],
                                              "when": self.date_hint, "played": True} if self.date_hint else
                                             {"kind": "live", "lap": lap.num,
                                              "date": datetime.now().strftime("%Y-%m-%d")}),
                                     prefix=self.settings.corner_prefix)
        except ValueError as e:
            self.events.append({"t": lap.t0, "text": f"new best lap not used as reference ({e})"})
            return
        if ref is not None and len(new.braked) < max(1, len(ref.braked) - 2):
            self.events.append({"t": lap.t0, "text": "new best lap not used as reference (corners did not match)"})
            return
        hist, base = list(self._loss_hist), dict(lap.closs)
        self.set_reference(new, adopted=True)
        self.events.append({"t": lap.t0, "text": f"reference updated to {fmt_lap(lt)} (lap {lap.num})"})
        if ref is not None and hist and base:
            # 2.17.2: what he learned about his corners carries over to the new reference: each lap's loss against
            # the old one, minus the new reference lap's own (corners matched by apex, as match_weak does)
            tol = max(60.0, 0.02 * new.length_m)
            to_new = {}
            for c in ref.corners:
                best = min(new.corners, key=lambda x: abs(_rel_m(c.apex_pct, x.apex_pct, new.length_m)), default=None)
                if best is not None and abs(_rel_m(c.apex_pct, best.apex_pct, new.length_m)) <= tol:
                    to_new[c.num] = best.num
            moved = [{to_new[n]: x - base[n] for n, x in h.items() if n in to_new and n in base} for h in hist]
            self._loss_hist = [h for h in moved if h]
            self._learn()

    _length_hint = None
    _car_hint = None
    _track_hint = None

    def hint(self, length_m=None, car=None, track=None):
        """Track length / car / track for a reference made from a live lap when there was none before."""
        self._length_hint, self._car_hint, self._track_hint = length_m, car, track
        self._clean_laps, self._last_clean, self._closs_last, self._learn_last = 0, None, {}, {}
        try:      # 2.20: his notes are filed by this
            from . import names
            tr = track or {}
            self.track_key = names.track_key(tr.get("name") or tr.get("code"), tr.get("config")) if tr else ""
        except Exception:  # noqa: BLE001
            self.track_key = ""
        self._note_key = None


DONE_OF = 10                # 2.18: "done when you hit it on 7 of 10 laps" (practiceplan.KEEP_BELOW, 70%)


def done_rule() -> tuple:
    """(hits, out of) that make the practice plan's one thing done: the session score's own rule (KEEP_BELOW)."""
    try:
        from . import practiceplan
        share = float(practiceplan.KEEP_BELOW)
    except Exception:  # noqa: BLE001
        share = 0.7
    return int(math.ceil(share * DONE_OF - 1e-9)), DONE_OF


def focus_count(passes, current_lap, before: str | None = None) -> dict:
    """2.18: the one thing's live count, from laps that are finished only (a pass on the lap he is still driving
    waits for the line, so "Hit 3 of 7" never shows before a lap is done). before: the focus's own words for when
    nothing is measured yet ("No clean pass through the corner yet", or that telemetry is read too slowly).
    {"count_hits", "count_of", "count_text", "done_text", "done"}."""
    done = [p for p in passes or [] if isinstance(p, dict) and isinstance(p.get("lap"), int)
            and current_lap is not None and p["lap"] < current_lap]
    n, h = len(done), sum(1 for p in done if p.get("hit"))
    need, of = done_rule()
    waiting = len(passes or []) > n
    if n:
        text = f"Hit {h} of {n} so far"
    elif waiting:
        text = "Counted when this lap is done"
    else:
        text = before or "No clean pass through the corner yet"
    last = done[-of:]
    reached = len(last) >= of and sum(1 for p in last if p.get("hit")) >= need
    return {"count_hits": h, "count_of": n, "count_text": text, "done": reached,
            "done_text": f"Done when you hit it on {need} of {of} laps"}


def marker_text(name: str) -> str:
    return f"Brake marker, {name}"


def early_text(name: str) -> str:
    return f"{name}, braked early"


def mph_down(kmh: float) -> int:
    """2.17.2: a speed difference measured in km/h, in whole mph as the coach says it."""
    return int(round(float(kmh) / KMH_PER_MPH))


def speed_text(name: str, mph_down_: int) -> str:
    """'T3: more speed, 4 down' (2.17.2: the number is mph)."""
    return f"{name}: more speed, {mph_down_} down"


# 2.17.2: every phrase below is said by the bundled voice packs (voicepack.compose), else by Windows speech
def corner_time_text(name: str, lost_s: float) -> str:
    """'T3, two tenths down'"""
    return f"{name}, {delta_words(lost_s)} down"


def good_text(name: str, gained_s: float) -> str:
    """'T3, good, up a tenth'"""
    return f"{name}, good, up {delta_words(gained_s)}"


SECTOR_WORDS = ("one", "two", "three")


def sector_text(k: int, d: float) -> str:
    """k 0..2, d = this sector's time minus the reference's: 'Sector one, up two tenths' / 'Sector two, a tenth down'."""
    head = f"Sector {SECTOR_WORDS[k]}, "
    return head + (f"up {delta_words(d)}" if d < 0 else f"{delta_words(d)} down")


def learn_speed_text(name: str, n: int) -> str:
    """2.20 'T5, 3 down on your best' (the number is mph, or km/h with Settings > Units)."""
    return f"{name}, {n} down on your best"


def learn_off_text(name: str, n: int | None = None) -> str:
    """2.20 'T5, off track' / 'T5, off track and 6 faster in'."""
    return f"{name}, off track" + (f" and {n} faster in" if n else "")


def learn_brake_text(name: str, metres: int, later: bool) -> str:
    """2.20 'T3, braked 15 metres later' (distances are always in metres)."""
    return f"{name}, braked {metres} metres {'later' if later else 'earlier'}"


def learn_on_text(name: str) -> str:
    return f"{name}, on your best"


BUILDING_TEXT = "Building your reference"


def reference_set_text(lap_time: float, short: bool = False) -> str:
    """'Reference set, one thirty-eight nine'; short (2.17.2, Settings.lap_times): 'Reference set, thirty-eight nine'"""
    return "Reference set, " + (voicepack.lap_time_short_words(lap_time) if short
                                else voicepack.lap_time_words(lap_time))


def hard_zones(ref) -> list:
    """2.17.2 the reference lap's hard zones: [(start pct, end pct)] from where its brake goes over HARD_BRAKE until
    its throttle is over HARD_THROTTLE again (end < start: across the line). A second brake before full throttle is
    part of the same zone."""
    if ref is None or len(ref.pct) < 3:
        return []
    pct = np.asarray(ref.pct, float)
    hb = np.asarray(ref.brake, float) > HARD_BRAKE
    full = (np.asarray(ref.throttle, float) > HARD_THROTTLE) & ~hb
    n = len(pct)
    ends = {}
    for i0 in np.nonzero(hb & ~np.roll(hb, 1))[0]:
        j = next(((i0 + k) % n for k in range(1, n) if full[(i0 + k) % n]), None)
        if j is None:
            continue
        a = float(pct[i0])
        if j not in ends or (a - float(pct[j])) % 1.0 < (ends[j] - float(pct[j])) % 1.0:
            ends[j] = a                                   # the earliest start of zones ending at the same place
    return sorted((a, float(pct[j])) for j, a in ends.items())

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

# only what the coach reads (each live read copies these out of iRacing's buffer); livedata.py owns the list
CHANNELS = livedata.COACH_CHANNELS       # 2.14 added steering (the practice focus's trail-braking number)


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
                          "length_m": _ibt_length_m(ib), "driver": (ib.player() or {}).get("UserName"),
                          "when": file_when(self.path, ib.start_date)}       # 2.18: the day it was driven
        self.step = max(1, int(round(self.tick / max(1.0, rate_hz))))
        self.interval = (self.step / self.tick) / self.speed if self.speed > 0 else 0.0
        self.realtime = self.speed > 0
        self._i = max(0, int(start_s * self.tick))

    def set_speed(self, speed: float):
        """2.18 rehearsal: play at this many times real speed from now on (the samples are the same ones)."""
        self.speed = float(speed)
        self.interval = (self.step / self.tick) / self.speed if self.speed > 0 else 0.0
        self.realtime = self.speed > 0

    def info(self) -> dict:
        return self._info

    def progress(self) -> dict:
        """Coach 2.0 rehearsal: how far the playback is (fraction 0..1, seconds played, seconds in the file)."""
        n = max(1, self._n)
        return {"fraction": round(min(1.0, self._i / n), 4), "at_s": round(min(self._i, n) / self.tick, 1),
                "total_s": round(n / self.tick, 1)}

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


class IrsdkSource(livedata.LiveReader):
    """Live iRacing for the coach. Since 2.27 the connection, the channel reads and the session-info thread are
    livedata.LiveReader's (the background recorder uses the same one). read() returns None while iRacing isn't
    running (and tries to reconnect every couple of seconds); state is 'not_installed', 'not_running' or 'connected'."""

    def __init__(self, rate_hz: float = 20.0, retry_s: float = 2.0, crew: bool = False):
        """crew (2.29): also read what the crew chief needs: fuel, the race clock, flags, and the CarIdx arrays of
        the whole field (about 4 times a second; the latest copy is s["_field"])."""
        super().__init__(rate_hz=rate_hz, retry_s=retry_s,
                         own=CHANNELS + (crewchief.CREW_OWN if crew else ()),
                         field=livedata.FIELD_CHANNELS if crew else ())
        self._field_last = None
        self._field_next = 0.0
        self._types: dict = {}
        self._info: dict | None = None
        self._info_v = -1
        self._sync_t = 0.0

    def _derive(self, sections: dict):
        di = sections.get("DriverInfo") or {}
        me = next((d for d in di.get("Drivers") or [] if d.get("CarIdx") == di.get("DriverCarIdx")), {})
        wi = sections.get("WeekendInfo") or {}
        self._types = {s.get("SessionNum"): s.get("SessionType") or s.get("SessionName") or ""
                       for s in (sections.get("SessionInfo") or {}).get("Sessions") or []}
        km = ibt.number(wi.get("TrackLength"))
        self._info = {"car": {"name": me.get("CarScreenName") or "", "path": me.get("CarPath") or "",
                              "id": me.get("CarID"), "class": me.get("CarClassShortName") or ""},
                      "track": {"name": wi.get("TrackDisplayName") or wi.get("TrackName") or "",
                                "code": wi.get("TrackName") or "", "config": wi.get("TrackConfigName") or "",
                                "id": wi.get("TrackID")},
                      "length_m": km * 1000.0 if km else None, "driver": me.get("UserName")}

    def info(self) -> dict | None:
        return self._info

    def read(self) -> dict | None:
        s = self.read_own()
        if s is None:
            return None
        v, sections = self.info_sections()
        if v != self._info_v and sections:
            self._info_v = v
            self._derive(sections)
        if (self._info is None or s.get("SessionNum") not in self._types) and time.monotonic() - self._sync_t > 1.0:
            # the very first read (or a session the thread hasn't seen yet): read it here once, as before 2.27
            self._sync_t = time.monotonic()
            if self.load_info_now():
                v, sections = self.info_sections()
                self._info_v = v
                self._derive(sections)
        s["SessionType"] = self._types.get(s.get("SessionNum"), "")
        if self.field:
            now = time.monotonic()
            if now >= self._field_next:
                self._field_next = now + 0.25
                f = self.read_field()
                if f:
                    self._field_last = f
            s["_field"] = self._field_last
        return s


# ---------------------------------------------------------------- the runner

def reference_why(ref, given=None) -> str:
    """2.17.2: where the reference lap came from, in a few plain words (the Coach page's status)."""
    if ref is None:
        return ""
    if given is not None and ref is given:
        return "the lap you chose"
    kind = (ref.source or {}).get("kind")
    if kind == "ibt":
        return "your best lap in your telemetry files"
    if kind == "live" and (ref.source or {}).get("played"):
        return "a clean lap from the session being rehearsed"     # 2.18: not "just now"
    if kind == "live":
        return "your best clean lap this session"
    if kind == "g61":
        return "a Garage 61 lap"
    return ""


def quicker_known(index, car_path: str, track_code: str, lap_time: float) -> dict | None:
    """2.19.1: Practice's reference lap (reference_choice) when it is quicker than lap_time, else None. The Coach page
    says so when the lap it is using is slower than the one Practice names, so the two pages never disagree silently."""
    try:
        ch = reference_choice(index, car_path, track_code)
    except Exception:  # noqa: BLE001
        return None
    if ch and ch["lap_time"] < float(lap_time) - 0.0005:
        return ch
    return None


def show_state(state: str, running: bool, mode: str, muted: bool) -> str:
    """2.18: the one word the driving view shows in large type: "coaching", "muted", "rehearsing" or "off" (and,
    while it gets going, "starting" or "waiting" for iRacing). Muted wins over everything while it runs."""
    if not running:
        return "off"
    if muted:
        return "muted"
    if mode == "rehearsal":
        return "rehearsing"
    if state in ("coaching", "no_reference"):
        return "coaching"
    return state if state in ("starting", "waiting") else "coaching"


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
    keep_given: that reference is a lap he chose (a teammate's, 2.15): while it is in use his new bests don't
    replace it (elsewhere they still update his own).
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
                 reference_index=None, focus_for=None, keep_given=False, log_dir=None, mode: str = "live",
                 log_header: dict | None = None, notes=None, mic=None, crew=None, crew_plan_for=None, crew_log=None):
        self.source = source
        self.mode = mode                # Coach 2.0: "live" (iRacing) or "rehearsal" (one of his files played back)
        self._log_dir = log_dir         # Coach 2.0 cue log folder (None: no log)
        self._log_header = dict(log_header or {})
        self.cuelog = None
        self._quiet_until = None        # "Mute for 1 minute": clock time it ends
        self.repeats = 0
        self._focus_for = focus_for
        self.voice = voice
        self.settings = settings or Settings()
        self.coach = Coach(None, weak_corners, self.settings, log=log, duration=self._voice_duration)
        self.coach.keep_reference = reference if keep_given else None
        # 2.20 corner notes: his notes (cornernotes.NoteStore) and a microphone (audioin backend, or a function giving one)
        self.notes = notes
        self.run_id = f"{int(time.time() * 1000)}-{id(self) % 10000}"
        self.coach.notes, self.coach.run_id, self.coach.count_notes = notes, self.run_id, mode == "live"
        self.noterec = None
        if notes is not None and mode == "live":
            from . import noterec
            self.noterec = noterec.NoteRecorder(notes, (mic if callable(mic) else (lambda: mic)), self.note_context,
                                                lambda: self.voice, self.coach.set_recording, log=log or _LOG.warning)
        self._given = reference
        self._telemetry_dir = telemetry_dir
        self._reference_index = reference_index
        self._refs: dict = {}           # (car path, track code) -> Reference, for the runner's life
        self.reference_searches = 0     # how many times the telemetry folder was searched (tests)
        self._weak_for = weak_for
        self._weak_static = weak_corners
        self._log = log or _LOG.warning
        self.perf = perfmeter.PerfMeter("coach", on_slow=lambda m: self._log("livecoach: " + m))
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
        self._focus_dirty = threading.Event()    # 2.14 review: look at the practice plan again (refresh_focus)
        self._focus_lap = None
        self._latest = None             # 2.17.2: the newest telemetry sample (cues are checked against it before playing)
        self._takes_check = None        # ... does the voice's say() take check= (PackVoice does)
        self._mute_by = self._mute_at = None    # 2.18: who muted and when (the window says so on reopening)
        self.repeat_note = None         # 2.18: {"said": False, "text", "wall"} when "Repeat that" was not said
        self.speed = 1.0                # 2.18 rehearsal speed (1x, 2x or 4x)
        self._skip = None               # 2.18 rehearsal: {"what": "lap" | "cue", "lap", "laps"} while skipping
        self.skip_note = ""
        self.skipped = 0                # cues made while skipping (never said)
        self._focus_saved = None        # 2.18: the last focus count written to focus-results.json
        self.coach.on_event = self._on_event
        # 2.29 Crew chief: fuel, pit window, rivals. Only when driving live; it speaks through the same voice.
        self.crew = crew if crew is not None else (crewchief.CrewChief(units=self.settings.units, clock=clock)
                                                   if mode == "live" else None)
        self.crew_speaker = crewchief.CallSpeaker(voice_on=self.settings.crew_voice)
        if self.crew is not None:
            self.crew_speaker.on_drop = self.crew.forgive      # a dropped call may be made again (box this lap)
        self.crew_perf = perfmeter.PerfMeter("crewchief", on_slow=lambda m: self._log("crewchief: " + m))
        self.crew_log = crew_log
        self._crew_plan_for = crew_plan_for
        self._crew_info_v = None
        self._crew_plan_key = None
        self._crew_lap_logged = None
        self._crew_noclip: set = set()
        self.crew_note = ""

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
        if self.noterec is not None:               # the microphone never stays open after the coach stops
            try:
                self.noterec.cancel()
            except Exception:  # noqa: BLE001
                pass
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

    def refresh_focus(self):
        """The practice plan for this car + track may have changed (a new plan was made or scored): the loop asks
        focus_for again before its next sample and switches when the plan_id differs. Also done once a lap."""
        self._focus_dirty.set()

    def mute(self, by: str = ""):
        self.set_muted(True, by)

    def unmute(self):
        self.set_muted(False)

    def set_muted(self, on: bool, by: str = "") -> bool:
        """2.18: mute or unmute, said outright (the window's Mute button sends the state it shows, so its first press
        always mutes, whatever a key or the wheel did a moment before). Muting during "Mute for 1 minute" makes it
        stay muted until Unmute. Mute lasts while the coach runs; every start begins unmuted. Returns muted."""
        if on:
            was = self.coach.settings.muted and self._quiet_until is None
            self._quiet_until = None
            self.coach.mute()
            _call(self.voice, "cancel")
            if not was:
                self._mute_by, self._mute_at = by or "", time.time()
                self._event({"ev": "muted", "why": "mute" + (f" ({by})" if by else "")})
        else:
            was = self.coach.settings.muted
            self._quiet_until = None
            self.coach.unmute()
            self._mute_by = self._mute_at = None
            self.repeat_note = None             # "not said, the coach is muted" is old news once it talks again
            if was:
                self._event({"ev": "muted", "why": "unmuted" + (f" ({by})" if by else ""), "seconds": 0.0})
        return self.coach.settings.muted

    def toggle(self, by: str = "") -> bool:
        """The mute key and the wheel button: one press mutes, the next unmutes."""
        return self.set_muted(not self.coach.settings.muted, by)

    def mute_for(self, seconds: float = 60.0) -> float:
        """Coach 2.0 "Mute for 1 minute": quiet now, talking again by itself after `seconds` (pressing it again
        starts the minute again; Unmute ends it early). Returns the seconds left."""
        self.coach.mute()
        _call(self.voice, "cancel")
        self._quiet_until = self._clock() + max(1.0, float(seconds))
        self._mute_by, self._mute_at = "for 1 minute", time.time()
        self._event({"ev": "muted", "why": f"for {int(seconds)} s", "seconds": float(seconds)})
        return float(seconds)

    def mute_state(self) -> dict:
        """2.18: the mute as the window shows it: {"on", "by", "since" (wall-clock seconds), "quiet_left"}."""
        on = bool(self.coach.settings.muted)
        ql = self.quiet_left()
        return {"on": on, "by": self._mute_by if on else None, "since": round(self._mute_at, 1) if on and
                self._mute_at else None, "quiet_left": round(ql, 1) if on and ql is not None else None}

    # ---- 2.18 rehearsal: faster, and skipping ahead ----
    SPEEDS = (1.0, 2.0, 4.0)
    SKIP_LAPS = 3               # "next cue" looks this many laps ahead at most

    def set_speed(self, speed: float) -> bool:
        """A rehearsal played at 1x, 2x or 4x. The words still take their own time, so the coach counts each cue as
        2 or 4 times as long in the file's time (Coach.time_scale): every cue still ends before its brake point, and
        fewer cues fit than at 1x. False in live coaching or for another speed."""
        try:
            x = float(speed)
        except (TypeError, ValueError):
            return False
        fn = getattr(self.source, "set_speed", None)
        if self.mode != "rehearsal" or x not in self.SPEEDS or fn is None:
            return False
        fn(x)
        self.speed = x
        self.coach.time_scale = x
        self._event({"ev": "asked", "why": f"rehearsal speed {x:g}x"})
        return True

    def skip(self, what: str = "lap") -> bool:
        """Rehearsal only: jump ahead to the next lap ("lap") or to the next cue ("cue"). The samples in between still
        go through the coach as fast as they can (so its laps, reference and focus stay right), with nothing said;
        cues made on the way are logged as skipped. "cue" gives up after SKIP_LAPS laps."""
        if self.mode != "rehearsal" or what not in ("lap", "cue") or not self.running():
            return False
        self.skip_note = ""
        self._skip = {"what": what, "lap": None, "laps": 0}
        _call(self.voice, "cancel")
        self._event({"ev": "asked", "why": f"skip to the next {what}"})
        return True

    def _skip_step(self, s: dict) -> bool:
        """True while this sample is skipped (on the coach's thread)."""
        sk = self._skip
        if sk is None:
            return False
        lap = s.get("Lap")
        if sk["lap"] is None:
            sk["lap"] = lap
        elif lap != sk["lap"]:
            sk["laps"] += 1
            sk["lap"] = lap
            if sk["what"] == "lap":
                self._skip = None
                self.skip_note = f"Skipped to lap {lap}."
                return False
            if sk["laps"] >= self.SKIP_LAPS:
                self._skip = None
                self.skip_note = f"No cue in the next {self.SKIP_LAPS} laps: playing on from lap {lap}."
                return False
        return True

    def quiet_left(self) -> float | None:
        q = self._quiet_until
        return max(0.0, q - self._clock()) if q is not None else None

    def repeat(self) -> str | None:
        """Coach 2.0 "Repeat that": the last spoken cue again, now. 2.18: never while muted (muted means silent; only
        the answer to "How's my pace?" comes through, because he asked for that one): nothing is said, and
        repeat_note says why for the window."""
        if self.coach.settings.muted:
            self.repeat_note = {"said": False, "wall": round(time.time(), 1),
                                "text": "Repeat that: not said, because the coach is muted. Unmute to hear it."}
            self._event({"ev": "repeated", "dropped": "muted"})
            return None
        self.repeat_note = None
        fn = getattr(self.voice, "repeat", None)
        text = None
        if fn is not None:
            try:
                text = fn()
            except Exception:  # noqa: BLE001
                text = None
        elif self.spoken:
            last = self.spoken[-1]
            try:
                self.voice.say(last.text, priority=9, t=None)
                text = last.text
            except Exception:  # noqa: BLE001
                text = None
        if text:
            self.repeats += 1
            self._event({"ev": "repeated", "text": text})
        return text

    def ask_pace(self) -> bool:
        """2.17.2 "How's my pace?" (the window, Ctrl+Shift+P or a wheel button): the coach answers on its next
        sample, as soon as it is safe to talk. False when the coach isn't running."""
        if not self.running():
            return False
        self.coach.ask()
        self._event({"ev": "asked", "why": "How's my pace?"})
        return True

    # ---- 2.20 corner notes and "use this lap" ----
    def note_context(self) -> dict | None:
        """Where the car is, for tagging a note: None unless live coaching has a telemetry sample and a named track."""
        s, info = self._latest, self._info
        if self.mode != "live" or not self.running() or not s or not info:
            return None
        pct = s.get("LapDistPct")
        if pct is None or not 0.0 <= float(pct) <= 1.0001:
            return None
        from . import names
        tr, car = info.get("track") or {}, info.get("car") or {}
        key = names.track_key(tr.get("name") or tr.get("code"), tr.get("config"))
        if not key:
            return None
        ref = self.coach.reference
        L = ref.length_m if ref is not None else info.get("length_m")
        return {"track_key": key, "track": names.track(tr.get("name") or tr.get("code"), tr.get("config")),
                "car": names.car(car.get("name") or car.get("path") or ""), "session": str(s.get("SessionType") or ""),
                "lap": s.get("Lap"), "pct": float(pct), "dist_m": float(pct) * float(L) if L else None,
                "kmh": float(s.get("Speed") or 0.0) * KMH, "time": s.get("SessionTime"), "run": self.run_id,
                "corners": list(ref.corners) if ref is not None else [], "length_m": L}

    def note_press(self) -> bool:
        """The record-a-note button or Ctrl+Shift+V went down (any thread)."""
        nr = self.noterec
        if nr is None:
            return False
        ok = nr.press()
        self._event({"ev": "asked", "why": "corner note: recording" if ok else "corner note: not started"})
        return ok

    def note_release(self):
        """... and went up: the note is saved (or thrown away) and the coach may talk again."""
        nr = self.noterec
        if nr is None:
            return None
        r = nr.release()
        if r:
            self._event({"ev": "asked", "why": "corner note: " + ("saved" if r.get("ok") else "not kept")})
        return r

    def use_last_lap(self) -> bool:
        """"Use this lap" (Ctrl+Shift+L or a wheel button): the lap just finished becomes the reference if it was clean."""
        if not self.running():
            return False
        self.coach.use_last_lap()
        self._event({"ev": "asked", "why": "use this lap"})
        return True

    def _say_note(self, cue: Cue):
        nid = (cue.measured or {}).get("note_id")
        got = self.notes.pcm(nid) if (self.notes is not None and nid) else None
        fn = getattr(self.voice, "play_clip", None)
        if got is None or fn is None:
            self.coach.note_invalid(cue, "the note's sound could not be played")
            return
        pcm, rate = got
        live = self.mode == "live"
        fn(cue.text, pcm, rate, priority=cue.priority, check=lambda c=cue: self._play_check(c),
           on_start=(lambda: self.notes.mark_played(nid)) if live else None)

    def _say(self, cue: Cue):
        """Hand a cue to the voice with its check: right before the sound starts the voice asks whether the cue is
        still valid against the newest telemetry (2.17.2); a cue that isn't is dropped and logged, not played."""
        if cue.kind == KIND_NOTE:
            self._say_note(cue)
            return
        if self._takes_check is None:
            self._takes_check = _accepts(getattr(self.voice, "say", None), "check")
        if self._takes_check:
            self.voice.say(cue.text, priority=cue.priority, t=cue.spoken_t, check=lambda c=cue: self._play_check(c))
        else:
            self.voice.say(cue.text, priority=cue.priority, t=cue.spoken_t)

    def _play_check(self, cue: Cue) -> str | None:
        why = self.coach.why_invalid(cue, self._latest, starting=True)
        if why:
            self.coach.note_invalid(cue, why)
        return why

    def _event(self, d: dict):
        """Coach 2.0: one line in the cue log (the coach's own events come through here too)."""
        lg = self.cuelog
        if lg is not None:
            ctx = dict(self.coach._ctx or {})
            lg.write({**ctx, **d})

    def _on_event(self, d: dict):
        lg = self.cuelog
        if lg is not None:
            lg.write(d)

    def _open_log(self, info: dict):
        self.cuelog = None
        if not self._log_dir or not self.settings.log_cues:
            return
        try:
            from . import cuelog
            car = (info.get("car") or {}).get("name") or (info.get("car") or {}).get("path") or "car"
            track = short_track((info.get("track") or {}).get("name") or "") or "track"
            vs = {}
            try:
                vs = self.voice.status() or {}
            except Exception:  # noqa: BLE001
                pass
            pk = vs.get("pack") or {}
            voice = (pk.get("name") or vs.get("backend") or "") + (f", {(vs.get('device') or {}).get('device')}"
                                                                    if vs.get("device") else "")
            self.cuelog = cuelog.CueLog(self._log_dir, car, track, mode=self.mode,
                                        header={"settings": self.settings.to_dict(), "voice": voice,
                                                **self._log_header})
        except Exception as e:  # noqa: BLE001
            self._problem(f"the cue log could not be opened ({e.__class__.__name__})")

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
            self.coach.set_reference(self.coach.reference, adopted=True)    # top_n / prefix may have changed (same lap)

    def _quicker_known(self, ref):
        """quicker_known() for the lap in use, asked of Setup Lab's index once per lap and car + track (never raises)."""
        if ref is None or self.coach.keep_reference is ref or not self._key or not self._key[0]:
            return None
        ck = (self._key, round(ref.lap_time, 3))
        if getattr(self, "_qk_key", None) != ck:
            val = None
            try:
                idx = self._reference_index
                if callable(idx):
                    idx = idx()
                if idx:
                    val = quicker_known(idx, self._key[0], self._key[1], ref.lap_time)
            except Exception:  # noqa: BLE001
                val = None
            self._qk_key, self._qk_val = ck, val
        return self._qk_val

    def _why_with_quicker(self):
        ref = self.coach.reference
        why = reference_why(ref, self.coach.keep_reference)
        qk = self._quicker_known(ref)
        return f"{why}; Practice lists a quicker clean lap, {qk['text']}, that the coach is not using yet" if qk and why else why

    def status(self) -> dict:
        """What the window can show. state: stopped | starting | waiting | no_reference | coaching | finished | error.
        text: one plain line, e.g. 'Waiting for iRacing to start.' or
        'Coaching: Mclaren 570s GT4 at Zolder, reference 1:38.874 from Nov 2'."""
        cs = self.coach.status()
        info = self._info or {}
        from . import names
        car = names.car((info.get("car") or {}).get("name") or "")
        track = short_track((info.get("track") or {}).get("name") or "")
        st = self._state
        if st == "waiting":
            src = getattr(self.source, "state", "")
            text = ("Waiting: Debrief's iRacing connection is missing. Reinstall Debrief." if src == "not_installed"
                    else "Waiting for iRacing to start.")
        elif st == "no_reference":
            text = (f"Listening: {car} at {track}. No clean lap to coach from yet. Drive a clean lap to set one."
                    if self.settings.quiet else
                    f"Building your reference: {car} at {track}. No lap of yours in your telemetry files, so your "
                    f"first clean lap becomes the reference and every lap after it is compared with it.")
        elif st == "coaching":
            ref = self.coach.reference
            word = "Rehearsing" if self.mode == "rehearsal" else "Coaching"     # 2.18: never "Coaching" in a rehearsal
            text = f"{word}: {car} at {track}, reference {ref.describe()}" if ref else f"{word}: {car} at {track}"
            why = self._why_with_quicker()
            if why and not self.settings.quiet:
                text += f" ({why})"
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
        if self.mode == "rehearsal":
            text = ("Rehearsal finished" if st == "finished" else
                    "Rehearsal: " + text if st in ("no_reference", "starting") else
                    "Rehearsal: reading the file" if st == "waiting" else text)
        ql = self.quiet_left()
        if ql is not None and cs["muted"] and st == "coaching":
            text = text.replace(" (muted)", "") + f" (quiet for {int(math.ceil(ql))} s more)"
        prog = None
        pf = getattr(self.source, "progress", None)
        if callable(pf):
            try:
                prog = pf()
            except Exception:  # noqa: BLE001
                prog = None
        return {"state": st, "text": text, "car": car, "track": track, "running": self.running(),
                "mode": self.mode, "progress": prog, "quiet_left": round(ql, 1) if ql is not None else None,
                "fixed": [c for c in (self._fixed_names())], "repeats": self.repeats,
                "log": self.cuelog.path.name if self.cuelog is not None else None,
                "muted": cs["muted"], "quiet_reason": cs["quiet_reason"], "reference": cs["reference"] and
                {k: cs["reference"][k] for k in ("lap_time", "lap_text", "text", "source")},
                "corners": len(cs["reference"]["corners"]) if cs["reference"] else 0,
                "weak": cs["weak"], "last_lap": cs["last_lap"], "spoken": cs["spoken"], "events": cs["events"],
                "voice": vs, "error": self._error, "errors": self._errors, "settings": self.settings.to_dict(),
                "focus": cs.get("focus"), "level": cs.get("level"), "focus_info": cs.get("focus_info"),
                "asks": self.coach.asks, "invalid_drops": self.coach.invalid_drops,
                "reference_why": self._why_with_quicker(),
                "learning": cs.get("learning"), "use_lap_note": cs.get("use_lap_note"),
                "recording": bool(self.noterec is not None and self.noterec.recording),
                "note": self.noterec.status() if self.noterec is not None else None,
                "notes_here": self._notes_here(),
                # 2.18 the driving view
                "show": show_state(st, self.running(), self.mode, cs["muted"]), "mute": self.mute_state(),
                "repeat_note": self.repeat_note, "live_delta": self.coach.live_delta, "speed": self.speed,
                "skipping": (self._skip or {}).get("what"), "skip_note": self.skip_note, "skipped": self.skipped,
                "perf": self.perf.summary(), "crew": self.crew_status()}

    def _notes_here(self):
        """How many of his notes are for the track he is on (the Coach page says so), or None without a store."""
        try:
            key = self.coach.track_key
            if self.notes is None or not key:
                return None
            ns = self.notes.active(key)
            return {"count": len(ns), "on": sum(1 for n in ns if n.get("enabled", True))}
        except Exception:  # noqa: BLE001
            return None

    def _fixed_names(self) -> list:
        ref = self.coach.reference
        if ref is None:
            return []
        out = []
        for n in self.coach.corner_state():
            w = next((w for w in self.coach.weak if w.corner.num == n), None)
            out.append(w.name if w else f"{self.settings.corner_prefix}{n}")
        return out

    # ---- the loop (its own thread) ----
    def _run(self):
        try:
            while not self._stop.is_set():
                t0 = self._clock()
                q = self._quiet_until
                if q is not None and t0 >= q:            # the minute is up: talking again
                    self._quiet_until = None
                    self.coach.unmute()
                    self._mute_by = self._mute_at = self.repeat_note = None
                    self._event({"ev": "muted", "why": "minute over, talking again", "seconds": 0.0})
                _pw, _pc = time.perf_counter(), time.thread_time()     # 2.26 perf meter: this step's cost
                try:
                    s = self.source.read()
                except Exception as e:  # noqa: BLE001
                    self._problem(f"reading telemetry failed ({e.__class__.__name__}: {str(e)[:120]})")
                    s = None
                if s is not None:
                    self._latest = s
                if s is None:
                    if getattr(self.source, "done", False):
                        self._state = "finished"
                        break
                    if self._state not in ("error",):
                        self._state = "waiting"
                    self._key = None
                    self._crew_idle()
                    self._stop.wait(1.0)
                    continue
                try:
                    skipping = self._skip_step(s)    # 2.18 rehearsal: skipping ahead (nothing said)
                    self._ensure_reference()
                    lap = s.get("Lap")
                    newlap = lap != self._focus_lap
                    if newlap:                       # once a lap: has the practice plan changed?
                        self._focus_lap = lap
                        self._focus_dirty.set()
                    if self._focus_dirty.is_set():
                        self._focus_dirty.clear()
                        self._check_focus()
                    self._prepare()                  # a new best may have changed the reference
                    self.coach.skipping = skipping and (self._skip or {}).get("what") == "lap"
                    cues = self.coach.feed(s)
                    self.coach.skipping = False
                    if newlap:
                        self._save_focus()
                    if skipping and (self._skip or {}).get("what") == "cue" and \
                            any(c.kind != KIND_BEEP for c in cues):
                        self._skip, skipping = None, False
                        self.skip_note = f"Skipped to the next cue, on lap {lap}."
                    if skipping:
                        for cue in cues:
                            self.skipped += 1
                            self.coach.spoken = [c for c in self.coach.spoken if c is not cue]
                        cues = []
                    if self.coach.take_cancel():
                        _call(self.voice, "cancel")
                    self._crew_step(s)                   # 2.29: fuel, pit window, rivals (its own cost is metered)
                    for cue in cues:
                        try:
                            if cue.kind == KIND_BEEP:
                                _call(self.voice, "beep")
                                continue
                            self.spoken.append(cue)
                            del self.spoken[:-50]
                            self._say(cue)
                        except Exception as e:  # noqa: BLE001
                            self._problem(f"speaking failed ({e.__class__.__name__})")
                except Exception as e:  # noqa: BLE001
                    self._problem(f"coach step failed ({e.__class__.__name__}: {str(e)[:120]})")
                if self.mode == "live":
                    self.perf._done(_pw, _pc)
                iv = getattr(self.source, "interval", 0.0) or 0.0
                if self._skip is not None:
                    iv = 0.0                             # skipping ahead: as fast as it goes
                if iv > 0 and float(getattr(self.source, "speed", 1.0) or 1.0) > 1.0 and self._skip is None:
                    # 2.18 rehearsal at 2x / 4x: keep to a schedule, so a slow step or a coarse timer (Windows waits
                    # in ~15 ms steps) is caught up on the next samples instead of lowering the speed for good
                    now = self._clock()
                    due = (self._due if self._due is not None else t0) + iv
                    if now - due > 1.0:
                        due = now                    # a long stall (a file opening): carry on from here
                    self._due = due
                    self._stop.wait(max(0.0, due - now))
                else:
                    self._due = None
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
            if self.crew_log is not None:
                try:
                    self.crew_log.close()
                except Exception:  # noqa: BLE001
                    pass

    # ---- 2.29 the crew chief (its own thread is this one; its cost is metered separately) ----
    def _crew_on(self) -> bool:
        return self.crew is not None and bool(self.settings.crew_calls)

    def _crew_context(self):
        """Hand the crew chief the session text when it changed, and its saved stint plan for the track."""
        crew = self.crew
        fn = getattr(self.source, "info_sections", None)
        if not callable(fn):
            return
        ver, secs = fn()
        if secs and ver != self._crew_info_v:
            self._crew_info_v = ver
            crew.set_info(secs, ver)
        key = crew.track_name
        if key and key != self._crew_plan_key and self._crew_plan_for is not None:
            self._crew_plan_key = key
            try:
                crew.set_plan(self._crew_plan_for(key))
            except Exception:  # noqa: BLE001
                crew.set_plan(None)

    def _crew_step(self, s: dict):
        if not self._crew_on():
            return
        w0, c0 = time.perf_counter(), time.thread_time()
        try:
            now = self._clock()
            crew = self.crew
            crew.units = _units.norm(self.settings.units)
            self._crew_context()
            calls = crew.feed(s, s.get("_field"), now=now)
            self._crew_deliver(calls, s, now)
        except Exception as e:  # noqa: BLE001  (the crew chief never stops the coach)
            self._problem(f"crew chief step failed ({e.__class__.__name__}: {str(e)[:120]})")
        finally:
            if self.mode == "live":
                self.crew_perf._done(w0, c0)

    def _crew_idle(self):
        """iRacing answered nothing: after a few seconds the crew chief says 'Data lost'."""
        if not self._crew_on():
            return
        try:
            now = self._clock()
            calls = self.crew.no_data(now)
            if calls:
                self._crew_deliver(calls, self._latest or {}, now)
        except Exception as e:  # noqa: BLE001
            self._problem(f"crew chief step failed ({e.__class__.__name__}: {str(e)[:120]})")

    def _crew_deliver(self, calls, s: dict, now: float):
        crew, sp, lg = self.crew, self.crew_speaker, self.crew_log
        sp.voice_on = bool(self.settings.crew_voice)
        sp.muted = bool(self.settings.muted)
        if lg is not None:
            if crew.lap is not None and crew.lap != self._crew_lap_logged and crew.snap:
                self._crew_lap_logged = crew.lap
                lg.add({"k": "lap", "t": s.get("SessionTime"), "lap": crew.lap, "sit": crewchief.situation(crew)})
            for c in calls:
                lg.add(crewchief.call_row(crew, c))
        sp.offer(calls)
        c = sp.pump(s, now)
        for ev in sp.take_events():
            if lg is not None:
                lg.add({"k": ev["what"], "call": ev["call"], "t": s.get("SessionTime"), "why": ev.get("why", "")})
        if c is None:
            return
        why = crew.still_valid(c, s)
        if why:
            c.spoken, c.why = False, why
            if lg is not None:
                lg.add({"k": "dropped", "call": c.id, "t": s.get("SessionTime"), "why": why})
            return
        self._say_crew(c)

    def _say_crew(self, c):
        """One crew chief call to the voice: sound priority 6 for fuel critical, 4 for the rest (coaching is 1 to 3), so
        they share one queue. A voice pack without the clips falls back to Windows speech; with neither, the call
        stays text in the window and a line goes to the log (once for each phrase)."""
        v = self.voice
        covers = getattr(v, "covers", None)
        try:
            has = bool(covers(c.say)) if callable(covers) else True
        except Exception:  # noqa: BLE001
            has = True
        if not has and c.say not in self._crew_noclip:
            self._crew_noclip.add(c.say)
            self._log(f"crewchief: the voice pack has no clip for '{c.say}'; Windows speech says it, or it stays text")
        c.spoken = True
        if self._takes_check is None:
            self._takes_check = _accepts(getattr(v, "say", None), "check")
        crew = self.crew
        if self._takes_check:
            v.say(c.say, priority=c.priority, t=c.session_t, check=lambda c=c: crew.still_valid(c, self._latest))
        else:
            v.say(c.say, priority=c.priority, t=c.session_t)

    def crew_status(self) -> dict:
        if self.crew is None:
            return {"enabled": False, "state": "off", "text": "The crew chief runs when you drive live."}
        if not self.settings.crew_calls:
            return {"enabled": False, "state": "off", "text": "Crew chief calls are off in Settings."}
        d = self.crew.snapshot()
        d.update(enabled=True, voice_on=bool(self.settings.crew_voice), perf=self.crew_perf.summary(),
                 log=(self.crew_log.path.name if self.crew_log is not None and self.crew_log.path else None),
                 plan_note=self.crew.plan_note, no_clip=sorted(self._crew_noclip)[:5])
        return d

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
        # 2.18: a lap adopted in a rehearsal is from the day the played file was driven, not today
        self.coach.date_hint = (info.get("when") or None) if self.mode == "rehearsal" else None
        car, track = info.get("car") or {}, info.get("track") or {}
        key = ((car.get("path") or "").lower(), (track.get("code") or "").lower())
        if key == self._key:
            if self.coach.reference is not None and self._state != "coaching":
                self._state = "coaching"
            return
        if self._key and self._key[0] and self.coach.reference is not None:
            self._refs[self._key] = self.coach.reference      # keep the best so far (a lap adopted on track too)
        self._key, self._info = key, info
        if key[0] or key[1]:
            self._open_log(info)
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
        self._focus_rate(focus)
        self.coach.hint(info.get("length_m"), car, track)
        self.coach.set_reference(ref, weak or [])
        self._state = "coaching" if ref is not None else "no_reference"
        self._prepared = None
        self._prepare()

    def _save_focus(self):
        """2.18: once a lap, the one thing's count over finished laps goes to focus-results.json beside the cue logs,
        so the Practice page can show what the coach counted (live coaching only; a rehearsal isn't a drive)."""
        if self.mode != "live" or not self._log_dir:
            return
        fs = self.coach._focus_status()
        if not fs or not fs.get("plan_id") or not fs.get("count_of"):
            return
        key = (fs["plan_id"], fs["count_hits"], fs["count_of"])
        if key == self._focus_saved:
            return
        self._focus_saved = key
        info = self._info or {}
        try:
            from . import cuelog
            cuelog.save_focus_result(self._log_dir, fs["plan_id"], {
                "hits": fs["count_hits"], "of": fs["count_of"], "corner": fs.get("corner"), "done": fs.get("done"),
                "done_text": fs.get("done_text"), "car": (info.get("car") or {}).get("path"),
                "track": (info.get("track") or {}).get("code"), "when": datetime.now().strftime("%Y-%m-%d %H:%M")})
        except Exception as e:  # noqa: BLE001
            self._problem(f"the coach's count could not be saved ({e.__class__.__name__})")

    def _check_focus(self):
        """Ask focus_for again for the current car + track; switch the coach's focus only when the plan changed (same
        plan: keep the passes counted so far)."""
        if self._focus_for is None or self._key is None:
            return
        info = self._info or {}
        try:
            new = self._focus_for(info.get("car") or {}, info.get("track") or {}, info.get("length_m"))
        except Exception as e:  # noqa: BLE001
            self._problem(f"practice focus not available ({e.__class__.__name__})")
            return
        old = self.coach.focus
        if getattr(new, "plan_id", None) == getattr(old, "plan_id", None) and (new is None) == (old is None):
            return
        self.coach.set_focus(new)
        self._focus_rate(new)
        self._prepared = None                         # the voice gets the new plan's phrases

    def _focus_rate(self, focus):
        """2.14 review: while a practice focus is watched, live iRacing is read at least FOCUS_MIN_HZ times a second
        (a slower sample_hz in coach.json can't place a brake point on the 5 m grid); back to sample_hz without one."""
        src = self.source
        if not isinstance(src, IrsdkSource):
            return
        if self._base_interval is None:
            self._base_interval = float(src.interval)
        want = self._base_interval
        if focus is not None and getattr(focus, "active", False):
            want = min(want, 1.0 / FOCUS_MIN_HZ)
        src.interval = want

    _base_interval = None
    _due = None

    def _prepare(self):
        """Hand the voice every fixed phrase for this reference, to render ahead of time (once per reference; with
        no reference yet, once, the phrases of the first laps)."""
        r = self.coach.reference
        key = r if r is not None else ("none", self.settings.level)
        if self._prepared is key or (r is None and self._prepared == key):
            return
        self._prepared = key
        _call(self.voice, "prepare", self.coach.vocabulary())


def _accepts(fn, name: str) -> bool:
    """fn takes a keyword argument called name."""
    import inspect
    try:
        ps = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in ps or any(p.kind == p.VAR_KEYWORD for p in ps.values())


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

    HOLD_MAX_S = 12.0           # a held key that never reads as released ends the recording after this

    def __init__(self, text: str, callback, log=None, on_release=None):
        self.text = text
        self.callback = callback
        self.on_release = on_release     # 2.20 push-to-talk: called when the key is let go again
        self._quit = False
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
                        if self.on_release is not None:          # 2.20: held until the main key comes up
                            self.wait_release(lambda vk=vk: bool(user32.GetAsyncKeyState(vk) & 0x8000))
            finally:
                user32.UnregisterHotKey(None, 1)
        except Exception as e:  # noqa: BLE001
            self._log(f"livecoach: mute key not available ({e.__class__.__name__})")
        finally:
            self.registered = False
            self._done.set()

    def wait_release(self, is_down, sleep=time.sleep, clock=time.monotonic):
        """Push-to-talk: wait until is_down() says the key is up (checked every 20 ms; at most HOLD_MAX_S, and not
        after stop()), then call on_release(). RegisterHotKey only reports the press, so the key is polled."""
        t0 = clock()
        try:
            while not self._quit and clock() - t0 < self.HOLD_MAX_S and is_down():
                sleep(0.02)
        finally:
            try:
                self.on_release()
            except Exception as e:  # noqa: BLE001
                self._log(f"livecoach: key release action failed ({e.__class__.__name__})")

    def stop(self, timeout: float = 2.0):
        self._quit = True
        if self._thread is None or not self._thread.is_alive() or self._tid is None:
            return
        try:
            import ctypes
            ctypes.windll.user32.PostThreadMessageW(self._tid, self.WM_QUIT, 0, 0)
        except Exception:  # noqa: BLE001
            pass
        self._thread.join(timeout)
