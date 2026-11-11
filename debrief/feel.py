"""How did it feel (2.30.2): quick taps after a session. Pick a corner, tap what the car did (Loose, Push, Brakes long,
Lockup, Bottoming, Snap) and, if you like, where in the corner (Entry, Mid, Exit). No AI, no network: the most
accurate input there is, kept as plain data next to the session.

One file per session: <library>/feel/<session id>.json (session id = c1.session_id of the session's .ibt files):

    {"feel_version": 1, "session_id": "...", "track_key": "...", "car_path": "...", "length_m": 3980,
     "saved_at": "...", "taps": [{"corner": "C3", "apex_pct": 0.378, "tag": "loose", "phase": "exit", "at": "..."}]}

Corners are kept with where they are on the lap (apex_pct), because Debrief finds corners from your laps and Turn 3
can shift between sessions; Siyanqaba and the fact sheet match taps to corners by position (c1.corner_key).

    FeelStore(folder).load(sid) / add(sid, info, corner, apex_pct, tag, phase) / undo(sid, index) / path(sid)
"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from pathlib import Path

from . import fsutil

FEEL_VERSION = 1
FOLDER = "feel"
TAGS = ("loose", "push", "brakes_long", "lockup", "bottoming", "snap")
PHASES = ("entry", "mid", "exit")
LABELS = {"loose": "Loose", "push": "Push", "brakes_long": "Brakes long", "lockup": "Lockup", "bottoming": "Bottoming",
          "snap": "Snap", "entry": "Entry", "mid": "Mid", "exit": "Exit"}
MAX_TAPS = 200                 # per session: far more than anyone taps; keeps a damaged page from growing a file
SID_OK = re.compile(r"^[0-9a-f]{16}$")
CORNER_OK = re.compile(r"^[A-Za-z]{0,3}\d{1,3}$")
KEY_OK = re.compile(r"^[\w .\-]{0,80}$")


def folder_for(library_folder) -> Path:
    return Path(library_folder) / FOLDER


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class FeelStore:
    def __init__(self, folder):
        self.folder = Path(folder)
        self._lock = threading.Lock()

    def path(self, sid: str) -> Path:
        if not isinstance(sid, str) or not SID_OK.match(sid):
            raise ValueError("That session isn't known. Open it again from the session list.")
        return self.folder / f"{sid}.json"

    def load(self, sid: str) -> dict:
        p = self.path(sid)
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"feel_version": FEEL_VERSION, "session_id": sid, "taps": []}
        if not isinstance(d, dict) or d.get("session_id") != sid or not isinstance(d.get("taps"), list):
            return {"feel_version": FEEL_VERSION, "session_id": sid, "taps": []}
        d["taps"] = [t for t in d["taps"] if isinstance(t, dict) and t.get("tag") in TAGS]
        return d

    def add(self, sid: str, info: dict, corner, apex_pct, tag, phase=None) -> dict:
        """One tap. info: {"track_key", "car_path", "length_m"} of the session. Returns the whole file."""
        if tag not in TAGS:
            raise ValueError("Pick what the car did: " + ", ".join(LABELS[t] for t in TAGS) + ".")
        if phase not in (None, "") and phase not in PHASES:
            raise ValueError("Where in the corner: Entry, Mid or Exit (or leave it).")
        corner = str(corner or "")
        if not CORNER_OK.match(corner):
            raise ValueError("Pick a corner first.")
        try:
            a = float(apex_pct) if apex_pct is not None else None
        except (TypeError, ValueError):
            a = None
        if a is not None and not (0.0 <= a <= 1.0):
            a = None
        info = info if isinstance(info, dict) else {}
        with self._lock:
            d = self.load(sid)
            if len(d["taps"]) >= MAX_TAPS:
                raise ValueError(f"This session already has {MAX_TAPS} taps. Undo some first.")
            for k in ("track_key", "car_path"):
                v = str(info.get(k) or "")
                if v and KEY_OK.match(v):
                    d[k] = v
            try:
                L = float(info.get("length_m") or 0)
                if L > 0:
                    d["length_m"] = round(L, 1)
            except (TypeError, ValueError):
                pass
            d["feel_version"] = FEEL_VERSION
            d["session_id"] = sid
            d["taps"].append({"corner": corner, "apex_pct": None if a is None else round(a, 4), "tag": tag,
                              "phase": phase or None, "at": _now()})
            d["saved_at"] = _now()
            fsutil.write_json(self.path(sid), d, mkdir=True, compact=False, indent=1)
            return d

    def undo(self, sid: str, index=None) -> dict:
        """Remove one tap (the last one when index is None). Returns the whole file."""
        with self._lock:
            d = self.load(sid)
            if not d["taps"]:
                return d
            i = len(d["taps"]) - 1 if index is None else int(index)
            if not (0 <= i < len(d["taps"])):
                raise ValueError("That tap is already gone.")
            d["taps"].pop(i)
            d["saved_at"] = _now()
            fsutil.write_json(self.path(sid), d, mkdir=True, compact=False, indent=1)
            return d


def summary(d: dict) -> list:
    """One short line per tap, newest first: "Turn 3: Loose (exit)"."""
    out = []
    for t in reversed((d or {}).get("taps") or []):
        m = re.fullmatch(r"[CT](\d+)", str(t.get("corner") or ""))
        turn = f"Turn {m.group(1)}" if m else str(t.get("corner") or "")
        out.append(f"{turn}: {LABELS.get(t.get('tag'), t.get('tag'))}" + (f" ({t['phase']})" if t.get("phase") else ""))
    return out
