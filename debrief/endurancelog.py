"""The team-race log (2.30): what Race Room writes during a race so the Endurance review can read it afterwards.

One gzip-JSON-lines file per PC, race.jsonl.gz:
  * in the folder of the 2.27 recording of the session, when the background recorder is running, else
  * <library>/Endurance/<date>_<track>/race.jsonl.gz (a teammate can put their own file in the same folder: the review reads
    every race*.jsonl.gz it finds there and merges them).

Rows (all small; frames are one a second, so a three-hour race is a few MB):
  hdr   once: track, subsession, team, who this PC is, every car (number, driver, class, team), the saved plan
  f     once a second: the CarIdx arrays of every car (laps, lap fraction, position, class position, last lap time, pit road,
        stalls) and the session clock and flags
  o     once a second while this PC drives: own fuel, tow, service flags, team incident count, tyre wear, driver-change channels
  stop  every pit stop of every car (lane time, time in the stall, whether it was under a caution)
  teamlap / stint / stint_end / team_stop / swap_alert / role / replan   what Race Room worked out
Rows are appended as they come and flushed every FLUSH_S seconds (each flush is one gzip member; liverec.read_jsonl reads
them all, and the good part of a damaged last one).
"""
from __future__ import annotations

import gzip
import json
import time
from pathlib import Path

NAME = "race.jsonl.gz"
FOLDER = "Endurance"
FLUSH_S = 15.0
MAX_WAIT = 3000


def _slug(text) -> str:
    from . import library
    return (library.slugify(text or "") or "race")[:60]


class RaceLog:
    """RaceLog(library_fn, recording_fn): add(row) every row; the file is chosen at the first flush and kept."""

    def __init__(self, library_fn=None, recording_fn=None, clock=time.monotonic, wall=time.time, log=None):
        self.library_fn = library_fn
        self.recording_fn = recording_fn
        self.clock = clock
        self.wall = wall
        self.log = log or (lambda m: None)
        self.rows: list = []
        self._last = clock()
        self.path: Path | None = None
        self.track = ""
        self.written = 0
        self.bytes = 0

    def add(self, row: dict):
        if row.get("k") == "hdr":
            self.track = str(row.get("track") or self.track)
        self.rows.append(row)
        if self.path is None and len(self.rows) > MAX_WAIT and not self._has_target():
            del self.rows[:-MAX_WAIT]                 # nowhere to write yet: keep the newest rows only
        if self.clock() - self._last >= FLUSH_S:
            self.flush()

    def _has_target(self) -> bool:
        try:
            return bool(self.library_fn and self.library_fn()) or bool(self.recording_fn and self.recording_fn())
        except Exception:  # noqa: BLE001
            return False

    def _choose(self) -> Path | None:
        if self.path is not None:
            return self.path
        try:
            rec = self.recording_fn() if self.recording_fn else None
        except Exception:  # noqa: BLE001
            rec = None
        if rec:
            return Path(rec) / NAME
        try:
            lib = self.library_fn() if self.library_fn else None
        except Exception:  # noqa: BLE001
            lib = None
        if lib:
            return Path(lib) / FOLDER / f"{time.strftime('%Y-%m-%d', time.localtime(self.wall()))}_{_slug(self.track)}" / NAME
        return None

    def flush(self, final=False):
        self._last = self.clock()
        if not self.rows:
            return None
        p = self._choose()
        if p is None:
            return None
        data = ("\n".join(json.dumps(r, separators=(",", ":")) for r in self.rows) + "\n").encode("utf-8")
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "ab") as raw:
                with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=6) as gz:
                    gz.write(data)
        except OSError as e:
            self.log(f"raceroom: could not write {p.name} ({e.__class__.__name__})")
            return None
        self.written += len(self.rows)
        try:
            self.bytes = p.stat().st_size
        except OSError:
            pass
        self.rows = []
        self.path = p
        return p

    def close(self):
        return self.flush(final=True)
