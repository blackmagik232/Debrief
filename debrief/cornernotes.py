"""Corner notes (2.20): short spoken notes the driver records with a held button while driving, kept per track and
turn, and played back by the coach on the straight before that corner. Pure logic and files; the microphone is
audioin.py and the playing is the coach (livecoach.py, cue kind "note").

Where things live (only ever inside Debrief's own data folder, never in iRacing's folders):

    <data folder>/notes/notes.json                         the index (every note, trash too)
    <data folder>/notes/<track>/<turn>-<YYYYMMDD-HHMMSS>.wav   16 kHz mono, cleaned and levelled to -20 dB

* clean(): a take under MIN_S, or one with no speech (an energy check), is discarded; the rest has a gentle high-pass
  (rumble, wheel noise), silence trimmed and is levelled to the voice packs' loudness (voicepack.TARGET_DB).
* tag(): which turn a note belongs to. Said on the straight before a corner: the next corner. Said inside a corner
  (braking, turn-in, apex, exit): that corner.
* NoteStore: add, list, move to another turn, switch on or off, delete (the trash keeps it TRASH_DAYS days), restore,
  re-record, and how many times each note was played.
* plan_says(): the learning plan. A note plays on each of the next PLAN_EVERY_LAP laps after it was recorded, then
  every third lap until lap PLAN_THIRD_UNTIL, then only on laps where that corner lost time against the reference.
* summary(): one line per note for Copy summary. No words, only numbers.
"""
from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

from . import fsutil, voicepack

_LOG = logging.getLogger("debrief.cornernotes")

RATE = 16000
MIN_S = 0.4                 # a held button shorter than this is a slip, not a note
MAX_S = 8.0
TRASH_DAYS = 30
FOLDER = "notes"
INDEX = "notes.json"
MAX_TURN = 60               # a corner number a note can be moved to
HIGHPASS_HZ = 100.0         # gentle: wheel and engine rumble, not the voice
PAD_MS = 120                # silence kept around the speech when trimming
SPEECH_ABS_DB = -45.0       # a frame quieter than this is never speech
SPEECH_OVER_FLOOR_DB = 10.0  # ... and speech is this far over the take's own noise floor
SPEECH_MIN_FRAMES = 8       # 20 ms frames: 0.16 s of speech at least

# the learning plan (laps through the corner after the note was recorded)
PLAN_EVERY_LAP = 5          # laps 1..5 after recording: every lap
PLAN_THIRD_UNTIL = 20       # laps 6..20: every third lap
PLAN_THIRD = 3
PLAN_LOSS_MIN_S = 0.05      # after that: only when the corner lost at least this against the reference last time


# ---------------------------------------------------------------- cleaning
@dataclass
class Cleaned:
    ok: bool
    why: str = ""               # "too short" | "no speech" | "" (kept)
    pcm: bytes = b""
    seconds: float = 0.0
    rate: int = RATE


def to_float(pcm: bytes):
    pcm = bytes(pcm)[:len(pcm) - len(pcm) % 2]
    return np.frombuffer(pcm, "<i2").astype(np.float64) / 32768.0


def has_speech(x, sr: int) -> bool:
    """Simple energy check: enough 20 ms frames well above the take's own noise floor (and above SPEECH_ABS_DB).
    Silence, a steady hum and a click are not speech."""
    lv = voicepack.frame_levels(np.asarray(x, dtype=np.float64), sr, 20)
    if lv.size < SPEECH_MIN_FRAMES:
        return False
    floor = float(np.percentile(lv, 10))
    thr = max(SPEECH_ABS_DB, floor + SPEECH_OVER_FLOOR_DB)
    return int(np.count_nonzero(lv > thr)) >= SPEECH_MIN_FRAMES


def clean(pcm: bytes, rate: int = RATE) -> Cleaned:
    """The take as a note: Cleaned(ok=False, why=...) when it is to be thrown away, else the cleaned 16-bit PCM."""
    x = to_float(pcm)
    secs = len(x) / float(rate) if rate else 0.0
    if secs < MIN_S:
        return Cleaned(False, "too short", b"", secs, rate)
    y = voicepack.highpass(voicepack.remove_dc(x), rate, HIGHPASS_HZ)     # rumble first: it isn't speech
    if not has_speech(y, rate):
        return Cleaned(False, "no speech", b"", secs, rate)
    y = voicepack.trim(y, rate, pad_ms=PAD_MS, rel_db=-35.0)
    if y.size < int(0.15 * rate):
        return Cleaned(False, "no speech", b"", secs, rate)
    y = voicepack.fades(voicepack.normalize(y, rate, voicepack.TARGET_DB), rate)
    return Cleaned(True, "", voicepack.to_pcm16(y), len(y) / float(rate), rate)


# ---------------------------------------------------------------- which turn a note belongs to
def _get(c, name, default=None):
    return c.get(name, default) if isinstance(c, dict) else getattr(c, name, default)


def rel_m(pct: float, apex_pct: float, length_m: float) -> float:
    """Metres from the apex along the lap, in (-L/2, L/2]: negative before it."""
    return ((float(pct) - float(apex_pct) + 0.5) % 1.0 - 0.5) * float(length_m)


def zone_m(c) -> tuple:
    """(start, end) of a corner in metres from its apex: from where braking or the corner begins to where it ends."""
    start = float(_get(c, "start_rel_m", -60.0) or -60.0)
    brake = _get(c, "brake_rel_m")
    lo = min(start, float(brake)) if brake is not None else start
    hi = float(_get(c, "end_rel_m", 30.0) or 30.0)
    return lo, max(hi, 1.0)


def tag(corners, length_m, pct) -> dict | None:
    """Which corner a note said at lap fraction pct belongs to: {"turn", "where": "straight" | "corner", "apex_pct"}.
    Inside a corner's zone (braking to exit): that corner. Anywhere else, on the straight: the next corner ahead.
    None when there is no corner map yet."""
    cs = [c for c in (corners or []) if _get(c, "num") is not None]
    if not cs or not length_m or pct is None:
        return None
    L = float(length_m)
    for c in cs:
        lo, hi = zone_m(c)
        if lo <= rel_m(pct, _get(c, "apex_pct"), L) <= hi:
            return {"turn": int(_get(c, "num")), "where": "corner", "apex_pct": float(_get(c, "apex_pct"))}

    def ahead(c):       # metres from here to the start of the corner's zone
        lo, _hi = zone_m(c)
        return ((float(_get(c, "apex_pct")) + lo / L - float(pct)) % 1.0) * L
    c = min(cs, key=ahead)
    return {"turn": int(_get(c, "num")), "where": "straight", "apex_pct": float(_get(c, "apex_pct"))}


# ---------------------------------------------------------------- the learning plan
def plan_says(passes: int, lost_last) -> tuple:
    """(play it this lap?, which part of the plan) for a note that has been passed `passes` times since it was
    recorded (this lap not counted). lost_last: did the corner lose time against the reference on its last clean pass
    (True / False, None when not known)."""
    n = max(0, int(passes))
    if n < PLAN_EVERY_LAP:
        return True, "every lap"
    if n < PLAN_THIRD_UNTIL:
        return (n - PLAN_EVERY_LAP) % PLAN_THIRD == 0, "every third lap"
    return bool(lost_last), "when you lose time there"


def plan_text(passes: int) -> str:
    """Where a note is in its plan, in plain words (the Corner notes list)."""
    n = max(0, int(passes))
    if n < PLAN_EVERY_LAP:
        left = PLAN_EVERY_LAP - n
        return f"plays every lap, {left} more lap{'s' if left != 1 else ''}"
    if n < PLAN_THIRD_UNTIL:
        return "plays every third lap"
    return "plays when you lose time there"


# ---------------------------------------------------------------- the store
def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(text or "").lower()).strip("_")[:60] or "track"


def short_track(name: str) -> str:
    try:
        from . import library
        return library.short_track_name(name)
    except Exception:  # noqa: BLE001
        return str(name or "")


def times_text(n: int) -> str:
    return f"{n} time{'s' if n != 1 else ''}"


class NoteStore:
    """The notes folder: <folder>/notes.json and one folder of WAV files per track. Thread-safe (the coach's thread
    counts plays, the window's thread edits). `version` goes up with every change so the coach knows to look again."""

    def __init__(self, folder, clock=time.time, log=None):
        self.root = Path(folder)
        self._clock = clock
        self._log = log or _LOG.warning
        self._lock = threading.RLock()
        self._notes: list = []
        self.version = 0
        self.error = ""
        self._load()

    # ---- files ----
    @property
    def index_path(self) -> Path:
        return self.root / INDEX

    def _load(self):
        try:
            d = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = {}
        ns = d.get("notes") if isinstance(d, dict) else None
        self._notes = [n for n in (ns or []) if isinstance(n, dict) and isinstance(n.get("id"), str)
                       and isinstance(n.get("file"), str)]

    def _save(self):
        """Raises nothing: a failure is remembered in .error and the notes stay in memory."""
        try:
            fsutil.write_json(self.index_path, {"version": 1, "notes": self._notes}, mkdir=True, compact=False,
                              indent=1)
            self.error = ""
        except OSError as e:
            self.error = "The notes couldn't be saved."
            self._note(f"saving the index failed ({e.__class__.__name__})")

    def _note(self, msg: str):
        try:
            self._log(f"cornernotes: {msg}")
        except Exception:  # noqa: BLE001
            pass

    def _path(self, n: dict) -> Path | None:
        """The note's WAV, only if it lies inside the notes folder (a hand-edited index can't point elsewhere)."""
        try:
            p = (self.root / n["file"]).resolve()
            p.relative_to(self.root.resolve())
        except (ValueError, OSError, KeyError):
            return None
        return p

    def _bump(self):
        self.version += 1

    # ---- adding ----
    def add(self, ctx: dict, pcm: bytes, rate: int = RATE, turn=None, where: str = "", apex_pct=None) -> dict:
        """Save a cleaned note. ctx: track_key, track, car, session, lap, pct, dist_m, kmh, time, run."""
        with self._lock:
            now = datetime.fromtimestamp(self._clock())
            tdir = slug(ctx.get("track_key") or ctx.get("track"))
            stamp = now.strftime("%Y%m%d-%H%M%S")
            rel = self._free_name(tdir, turn, stamp)
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            voicepack.write_wav(p, pcm, rate)
            n = {"id": f"{int(self._clock() * 1000)}-{len(self._notes)}", "track_key": ctx.get("track_key") or "",
                 "track": ctx.get("track") or "", "car": ctx.get("car") or "", "session": ctx.get("session") or "",
                 "lap": ctx.get("lap"), "pct": _r(ctx.get("pct"), 5), "dist_m": _r(ctx.get("dist_m"), 1),
                 "kmh": _r(ctx.get("kmh"), 1), "time": _r(ctx.get("time"), 2), "run": ctx.get("run") or "",
                 "turn": turn, "where": where or "", "apex_pct": _r(apex_pct, 5), "file": rel, "stamp": stamp,
                 "seconds": round(len(pcm) / 2.0 / rate, 2), "rate": rate, "enabled": True, "played": 0, "passes": 0,
                 "created": now.strftime("%Y-%m-%d %H:%M:%S"), "deleted": None}
            self._notes.append(n)
            self._save()
            self._bump()
            return dict(n)

    def _free_name(self, tdir: str, turn, stamp: str, own: str | None = None) -> str:
        base = f"{turn if turn is not None else 'x'}-{stamp}"
        rel, i = f"{tdir}/{base}.wav", 1
        while rel != own and ((self.root / rel).exists() or any(n.get("file") == rel for n in self._notes)):
            i += 1
            rel = f"{tdir}/{base}-{i}.wav"
        return rel

    # ---- reading ----
    def get(self, note_id: str) -> dict | None:
        with self._lock:
            n = next((x for x in self._notes if x["id"] == note_id), None)
            return dict(n) if n else None

    def all(self, include_trash: bool = False) -> list:
        with self._lock:
            return [dict(n) for n in self._notes if include_trash or not n.get("deleted")]

    def active(self, track_key: str) -> list:
        """Notes for this track that aren't in the trash (on or off), newest first."""
        with self._lock:
            return sorted((dict(n) for n in self._notes if not n.get("deleted") and n.get("track_key") == track_key),
                          key=lambda n: n.get("created") or "", reverse=True)

    def pcm(self, note_id: str):
        """(16-bit PCM bytes, rate) of a note, or None when its file is missing or isn't a WAV."""
        with self._lock:
            n = next((x for x in self._notes if x["id"] == note_id), None)
            p = self._path(n) if n else None
        if p is None:
            return None
        try:
            x, r, _info = voicepack.read_wav(p)
        except (OSError, ValueError):
            return None
        return voicepack.to_pcm16(x), int(r)

    def grouped(self) -> list:
        """For the Practice page: [{"track_key", "track", "turns": [{"turn", "notes": [...]}]}], tracks A to Z, turns
        in order (notes with no turn yet last), each turn's notes newest first. Trash not included."""
        with self._lock:
            ns = [dict(n) for n in self._notes if not n.get("deleted")]
        out: dict = {}
        for n in ns:
            g = out.setdefault(n.get("track_key") or "", {"track_key": n.get("track_key") or "",
                                                            "track": n.get("track") or "Unknown track", "turns": {}})
            g["turns"].setdefault(n.get("turn"), []).append(n)
        res = []
        for g in sorted(out.values(), key=lambda g: g["track"].lower()):
            turns = sorted(g["turns"].items(), key=lambda kv: (kv[0] is None, kv[0] or 0))
            res.append({"track_key": g["track_key"], "track": g["track"],
                        "turns": [{"turn": t, "notes": sorted(v, key=lambda n: n.get("created") or "", reverse=True)}
                                  for t, v in turns]})
        return res

    def trash(self) -> list:
        with self._lock:
            self._purge_locked()
            return sorted((dict(n) for n in self._notes if n.get("deleted")), key=lambda n: n["deleted"], reverse=True)

    # ---- changing ----
    def _find(self, note_id):
        n = next((x for x in self._notes if x["id"] == note_id), None)
        if n is None:
            raise KeyError("That note wasn't found.")
        return n

    def set_enabled(self, note_id: str, on: bool) -> dict:
        with self._lock:
            n = self._find(note_id)
            n["enabled"] = bool(on)
            self._save()
            self._bump()
            return dict(n)

    def move(self, note_id: str, turn: int) -> dict:
        """Belongs to another turn now. The file is renamed to match (<turn>-<time>.wav)."""
        if isinstance(turn, bool) or not isinstance(turn, int) or not 1 <= turn <= MAX_TURN:
            raise ValueError(f"Pick a turn from 1 to {MAX_TURN}.")
        with self._lock:
            n = self._find(note_id)
            n["turn"] = turn
            n["apex_pct"] = None                  # its place on the lap was the old corner's: matched by number now
            n["where"] = "moved"
            self._rename(n)
            self._save()
            self._bump()
            return dict(n)

    def tag_untagged(self, track_key: str, corners, length_m) -> int:
        """Notes recorded before the coach had a corner map get their turn now. Returns how many."""
        k = 0
        with self._lock:
            for n in self._notes:
                if n.get("deleted") or n.get("track_key") != track_key or n.get("turn") is not None:
                    continue
                t = tag(corners, length_m, n.get("pct"))
                if t:
                    n["turn"], n["where"], n["apex_pct"] = t["turn"], t["where"], _r(t["apex_pct"], 5)
                    self._rename(n)
                    k += 1
            if k:
                self._save()
                self._bump()
        return k

    def _rename(self, n: dict):
        p = self._path(n)
        if p is None or not p.exists():
            return
        rel = self._free_name(Path(n["file"]).parent.as_posix(), n.get("turn"), n.get("stamp") or "note", own=n["file"])
        if rel == n["file"]:
            return
        try:
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            p.replace(self.root / rel)
            n["file"] = rel
        except OSError as e:
            self._note(f"renaming a note failed ({e.__class__.__name__})")

    def replace_audio(self, note_id: str, pcm: bytes, rate: int = RATE) -> dict:
        """Re-record: new sound for the same note (same turn). Its plan starts again."""
        with self._lock:
            n = self._find(note_id)
            p = self._path(n)
            if p is None:
                raise ValueError("That note's file can't be used.")
            voicepack.write_wav(p, pcm, rate)
            n.update(seconds=round(len(pcm) / 2.0 / rate, 2), rate=rate, played=0, passes=0, run="", lap=None,
                     created=datetime.fromtimestamp(self._clock()).strftime("%Y-%m-%d %H:%M:%S"))
            self._save()
            self._bump()
            return dict(n)

    def delete(self, note_id: str) -> dict:
        """Into the trash: gone from the list and never played, kept TRASH_DAYS days, then removed for good."""
        with self._lock:
            n = self._find(note_id)
            n["deleted"] = datetime.fromtimestamp(self._clock()).strftime("%Y-%m-%d %H:%M:%S")
            self._save()
            self._bump()
            return dict(n)

    def restore(self, note_id: str) -> dict:
        with self._lock:
            n = self._find(note_id)
            n["deleted"] = None
            self._save()
            self._bump()
            return dict(n)

    def purge(self) -> int:
        with self._lock:
            return self._purge_locked()

    def _purge_locked(self) -> int:
        cut = datetime.fromtimestamp(self._clock() - TRASH_DAYS * 86400).strftime("%Y-%m-%d %H:%M:%S")
        gone = [n for n in self._notes if n.get("deleted") and n["deleted"] < cut]
        for n in gone:
            p = self._path(n)
            try:
                if p is not None:
                    p.unlink(missing_ok=True)
            except OSError:
                pass
            self._notes.remove(n)
        if gone:
            self._save()
        return len(gone)

    def mark_pass(self, note_id: str):
        """The car went by the corner on a lap (counted for the learning plan)."""
        with self._lock:
            n = next((x for x in self._notes if x["id"] == note_id), None)
            if n is not None:
                n["passes"] = int(n.get("passes") or 0) + 1
                self._save()

    def mark_played(self, note_id: str):
        with self._lock:
            n = next((x for x in self._notes if x["id"] == note_id), None)
            if n is not None:
                n["played"] = int(n.get("played") or 0) + 1
                self._save()

    # ---- for the coach ----
    def by_corner(self, track_key: str, corners, length_m, tol_m: float | None = None) -> dict:
        """{corner num: the newest enabled note} for this track's corner map. A note is matched to a corner by where
        that corner is on the lap (its apex, within max(60 m, 2% of the lap)), or by its number when it has no place
        (moved by hand). One note per corner: the newest."""
        out: dict = {}
        L = float(length_m or 0.0)
        tol = tol_m if tol_m is not None else max(60.0, 0.02 * L)
        cs = [c for c in (corners or []) if _get(c, "num") is not None]
        for n in self.active(track_key):                   # newest first
            if not n.get("enabled", True) or n.get("turn") is None:
                continue
            hit = None
            if n.get("apex_pct") is not None and L:
                best = min(cs, key=lambda c: abs(rel_m(_get(c, "apex_pct"), n["apex_pct"], L)), default=None)
                if best is not None and abs(rel_m(_get(best, "apex_pct"), n["apex_pct"], L)) <= tol:
                    hit = int(_get(best, "num"))
            elif any(int(_get(c, "num")) == n["turn"] for c in cs):
                hit = int(n["turn"])
            if hit is not None:
                out.setdefault(hit, n)
        return out


def _r(v, k):
    try:
        return round(float(v), k)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- Copy summary
def summary_line(n: dict) -> str:
    """'Spa, Turn 5, lap 4: note 3.1 s (played 5 times)'. Numbers only, never what was said."""
    turn = f"Turn {n['turn']}" if n.get("turn") is not None else "no turn yet"
    lap = f", lap {n['lap']}" if n.get("lap") is not None else ""
    off = ", off" if not n.get("enabled", True) else ""
    return (f"{short_track(n.get('track'))}, {turn}{lap}: note {float(n.get('seconds') or 0):.1f} s "
            f"(played {times_text(int(n.get('played') or 0))}{off})")


def summary(notes) -> str:
    """One line per note (trash left out), by track and turn."""
    rows = sorted((n for n in notes if not n.get("deleted")),
                  key=lambda n: (short_track(n.get("track")).lower(), n.get("turn") is None, n.get("turn") or 0,
                                 n.get("created") or ""))
    return "\n".join(summary_line(n) for n in rows) if rows else "No corner notes yet."


# ---------------------------------------------------------------- the tones (generated, no voice clips)
def tones(rate: int = RATE) -> dict:
    """{"start": rising, "saved": falling, "discard": low} as float samples: short beeps made here, so the notes
    need no new voice clips. Rising at the start of a recording, falling when it was saved, one low tone when the
    note was thrown away."""
    def beep(freq, secs, level=0.30):
        n = int(secs * rate)
        i = np.arange(n)
        edge = max(1, int(0.008 * rate))
        env = np.minimum(1.0, np.minimum(i / edge, (n - 1 - i) / edge))
        return level * env * np.sin(2 * math.pi * freq * i / rate)

    gap = np.zeros(int(0.02 * rate))
    return {"start": np.concatenate([beep(660, 0.06), gap, beep(990, 0.07)]),
            "saved": np.concatenate([beep(990, 0.06), gap, beep(660, 0.09)]),
            "discard": beep(220, 0.22, 0.35)}


TONE_S = 0.3                # the start tone (0.15 s) and the sound card's delay: the first part of a take is dropped, so
                            # the beep itself is never in a note
