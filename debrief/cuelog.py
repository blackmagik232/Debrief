"""The coach's cue log (Coach 2.0): every cue the live coach makes, says, drops or cuts off, one JSON line each, so a
session can be looked at afterwards ("why did it say that there?", "why was it quiet in T4?").

    <settings dir>/coach-logs/<date> <car> <track>.jsonl            (rehearsals: "... rehearsal.jsonl")

A line: {"ev": made | spoken | dropped | cancelled | not_made | corner_fixed | corner_regressed | lap_end |
reference | focus_learned (2.17.2: focus corners learned from this session) | repeated | asked (2.17.2: "How's my
pace?") | muted | session, "t": session time, "lap", "dist_m", "kmh", "kind", "text", "corner",
"why" (why it was made), "dropped" (why it was not said), "measured" {...}, ...}. Several sessions on one day with
the same car and track go into the same file, each starting with a "session" line.

Small and private by design: only the car, the track and what the coach did (never his name, his iRacing id, a
teammate's name or a file path); a file stops growing at MAX_BYTES; only the newest KEEP files are kept.
Pure Python, no Windows.
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

FOLDER = "coach-logs"
KEEP = 30
MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 5000                     # rows read back for the viewer
NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2} [A-Za-z0-9 .\-]{1,90}\.jsonl$")
# the only keys a line may carry (anything else is dropped before writing: nothing personal slips in)
KEYS = ("ev", "t", "lap", "dist_m", "kmh", "kind", "text", "corner", "why", "dropped", "measured", "made_t", "dur_s",
        "next_brake_s", "waited_s", "lap_time", "clean", "cues", "source", "ref_lap", "ref_date", "corners", "focus",
        "car", "track", "mode", "version", "started", "settings", "voice", "seconds", "latency_s", "wall")
MEASURED_KEYS = ("min_kmh", "ref_min_kmh", "down_kmh", "brake_point_m", "ref_brake_point_m", "early_m", "to_brake_m",
                 "lost_s", "value", "hit", "near", "lap", "lap_time", "ref_time", "delta_s", "timed_by", "sector",
                 "down_mph")
FOCUS_KEYS = ("name", "id", "lost_s", "phrase", "brake_m")
SETTINGS_KEYS = ("markers", "feedback", "lap_delta", "top_n", "max_per_lap", "repeat_laps", "gap_s", "in_races",
                 "audio_latency_s", "end_before_brake_s", "voice_pack", "fixed_laps", "volume", "level", "lap_times")


def _plain(x, n=60) -> str:
    s = re.sub(r"[^A-Za-z0-9 .\-]+", " ", str(x or "")).strip()
    return re.sub(r"\s+", " ", s)[:n].strip() or "unknown"


def file_name(car: str, track: str, when: datetime | None = None, mode: str = "live") -> str:
    d = (when or datetime.now()).strftime("%Y-%m-%d")
    return f"{d} {_plain(car, 40)} {_plain(track, 40)}" + (" rehearsal" if mode == "rehearsal" else "") + ".jsonl"


def _clean(d: dict) -> dict:
    out = {}
    for k in KEYS:
        if k not in d or d[k] is None:
            continue
        v = d[k]
        if k == "measured" and isinstance(v, dict):
            v = {m: v[m] for m in MEASURED_KEYS if v.get(m) is not None}
            if not v:
                continue
        elif k == "focus" and isinstance(v, list):
            v = [{f: x.get(f) for f in FOCUS_KEYS if isinstance(x, dict) and x.get(f) is not None} for x in v[:5]]
        elif k == "settings" and isinstance(v, dict):
            v = {m: v[m] for m in SETTINGS_KEYS if m in v}
        elif isinstance(v, float):
            v = round(v, 3)
        elif isinstance(v, str):
            v = v[:200]
        out[k] = v
    return out


class CueLog:
    """Appends events to one log file. write() never raises; a full file gets one last line and then stays as is."""

    def __init__(self, folder, car: str, track: str, mode: str = "live", keep: int = KEEP, when=None,
                 max_bytes: int = MAX_BYTES, header: dict | None = None):
        self.folder = Path(folder)
        self.mode = mode
        self.path = self.folder / file_name(car, track, when, mode)
        self.max_bytes = int(max_bytes)
        self.lines = 0
        self.full = False
        self.error = ""
        self._lock = threading.Lock()
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            rotate(self.folder, keep, protect=self.path.name)
        except OSError as e:
            self.error = f"{e.__class__.__name__}"
        self.write({"ev": "session", "car": _plain(car), "track": _plain(track), "mode": mode,
                    "started": datetime.now().strftime("%Y-%m-%d %H:%M"), **(header or {})})

    def write(self, ev: dict) -> bool:
        if self.full or self.error:
            return False
        row = _clean(ev)
        if "wall" not in row:
            row["wall"] = round(time.time(), 1)
        line = json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock:
            try:
                size = self.path.stat().st_size if self.path.exists() else 0
                if size + len(line) > self.max_bytes:
                    line = json.dumps({"ev": "log_full", "wall": round(time.time(), 1)}) + "\n"
                    self.full = True
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line)
                self.lines += 1
                return not self.full
            except OSError as e:
                self.error = f"{e.__class__.__name__}"
                return False


def rotate(folder, keep: int = KEEP, protect: str | None = None) -> int:
    """Delete the oldest logs so at most `keep` are left (the one being written is never deleted). Returns how many
    went."""
    folder = Path(folder)
    try:
        files = [p for p in folder.iterdir() if p.is_file() and NAME_RE.match(p.name)]
    except OSError:
        return 0
    files.sort(key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
    n = 0
    room = keep - (1 if protect and not any(p.name == protect for p in files) else 0)
    for p in files[max(0, room):]:
        if p.name == protect:
            continue
        try:
            p.unlink()
            n += 1
        except OSError:
            pass
    return n


def _safe_path(folder, name) -> Path | None:
    if not isinstance(name, str) or not NAME_RE.match(name) or "/" in name or "\\" in name or ".." in name:
        return None
    p = Path(folder) / name
    try:
        if p.is_symlink() or not p.is_file():
            return None
        p.resolve().relative_to(Path(folder).resolve())
    except (OSError, ValueError):
        return None
    return p


def read_log(folder, name) -> list:
    """The rows of one log (at most MAX_ROWS, the last ones). [] when the name isn't a log in that folder."""
    p = _safe_path(folder, name)
    if p is None:
        return []
    rows = []
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict):
                    rows.append(r)
                    if len(rows) > MAX_ROWS:
                        del rows[:len(rows) - MAX_ROWS]
    except OSError:
        return []
    return rows


def counts(rows) -> dict:
    ev = Counter(r.get("ev") for r in rows)
    laps = {r.get("lap") for r in rows if r.get("ev") == "lap_end"}
    return {"spoken": ev.get("spoken", 0), "dropped": ev.get("dropped", 0), "made": ev.get("made", 0),
            "cancelled": ev.get("cancelled", 0), "laps": len(laps), "sessions": ev.get("session", 0)}


def list_logs(folder) -> list:
    """Newest first: [{"name", "date", "title", "mode", "size", "modified", "spoken", "dropped", "laps"}]."""
    folder = Path(folder)
    out = []
    try:
        files = [p for p in folder.iterdir() if p.is_file() and NAME_RE.match(p.name)]
    except OSError:
        return []
    files.sort(key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
    for p in files[:KEEP]:
        rows = read_log(folder, p.name)
        head = next((r for r in rows if r.get("ev") == "session"), {})
        c = counts(rows)
        title = f"{head.get('car') or '?'} at {head.get('track') or '?'}"
        out.append({"name": p.name, "date": p.name[:10], "title": title, "mode": head.get("mode") or "live",
                    "size": p.stat().st_size, "modified": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                    **c})
    return out


def _fmt_m(r) -> str:
    m = r.get("measured") or {}
    bits = []
    if "min_kmh" in m:                      # 2.17.2: shown in mph (measured in km/h, as before)
        bits.append(f"min {_mph(m['min_kmh'])} vs ref {_mph(m.get('ref_min_kmh'))} mph")
    if m.get("brake_point_m") is not None:
        bits.append(f"brake {m['brake_point_m']} m before apex vs ref {m.get('ref_brake_point_m')}")
    if "delta_s" in m:
        bits.append(f"lap {m.get('lap_time')} vs ref {m.get('ref_time')} ({m['delta_s']:+.2f} s)")
    if "value" in m:
        bits.append(f"plan value {m['value']} ({'hit' if m.get('hit') else 'miss'})")
    if "to_brake_m" in m:
        bits.append(f"made {m['to_brake_m']} m before brake point at {m.get('brake_point_m')} m")
    return "; ".join(bits)


def _mph(kmh):
    return round(kmh / 1.609344) if isinstance(kmh, (int, float)) else "?"


def _mph_text(s):
    """Older logs' 'N km/h' in mph."""
    return re.sub(r"(-?\d+(?:\.\d+)?) km/h", lambda m: f"{_mph(float(m.group(1)))} mph", str(s or ""))


def summary(rows, name: str = "", version: str = "", limit: int = 6000) -> str:
    """A compact, plain-text account of one log for him to paste to Claude: what the coach said and why, what it
    held back and why, per lap. No personal details (the log has none)."""
    if not rows:
        return "Empty coach log."
    head = next((r for r in rows if r.get("ev") == "session"), {})
    c = counts(rows)
    L = [f"Debrief coach log {name}".strip() + (f" (Debrief {version})" if version else ""),
         f"{head.get('car', '?')} at {head.get('track', '?')}, {head.get('mode', 'live')}, started {head.get('started', '?')}"]
    st = head.get("settings") or {}
    if st:
        L.append("Settings: " + ", ".join(f"{k}={v}" for k, v in st.items()))
    if head.get("voice"):
        L.append(f"Voice: {head['voice']}")
    for r in rows:
        if r.get("ev") == "reference":
            foc = ", ".join(f"{f.get('name')}" + (f" ({f['lost_s']:.2f} s)" if isinstance(f.get('lost_s'), (int, float)) else "")
                            + (f" '{f['phrase']}'" if f.get("phrase") else "") for f in r.get("focus") or [])
            L.append(f"Reference at t={r.get('t', '?')}: {r.get('lap_time')} s ({r.get('source')} lap {r.get('ref_lap')}, "
                     f"{r.get('ref_date')}), {r.get('corners')} corners; focus: {foc or 'none'}")
    spoken = [r for r in rows if r.get("ev") == "spoken"]
    per_lap = Counter(r.get("lap") for r in spoken)
    kinds = Counter(r.get("kind") for r in spoken)
    drops = Counter(r.get("dropped") for r in rows if r.get("ev") == "dropped")
    notm = Counter(r.get("why") for r in rows if r.get("ev") == "not_made")
    laps = [r for r in rows if r.get("ev") == "lap_end"]
    L.append(f"Laps {len(laps)} (clean {sum(1 for x in laps if x.get('clean'))}); spoken {c['spoken']} "
             f"({', '.join(f'{k} {v}' for k, v in kinds.most_common())}); max {max(per_lap.values()) if per_lap else 0} in a lap; "
             f"cut off {c['cancelled']}")
    if drops:
        L.append("Dropped: " + ", ".join(f"{k} {v}" for k, v in drops.most_common()))
    if notm:
        L.append("Not made: " + ", ".join(f"{k} {v}" for k, v in notm.most_common()))
    fx = [f"{r.get('corner')} fixed at lap {r.get('lap')}" for r in rows if r.get("ev") == "corner_fixed"]
    fx += [f"{r.get('corner')} slipped back at lap {r.get('lap')}" for r in rows if r.get("ev") == "corner_regressed"]
    fx += [f"focus learned at lap {r.get('lap')}: " + (", ".join(
        f"{f.get('name')}" + (f" ({f['lost_s']:.2f} s)" if isinstance(f.get("lost_s"), (int, float)) else "")
        for f in r.get("focus") or []) or "none") for r in rows if r.get("ev") == "focus_learned"]
    if fx:
        L.append("Corners: " + "; ".join(fx))
    other = Counter(r.get("ev") for r in rows if r.get("ev") in ("repeated", "asked", "muted"))
    if other:
        L.append("Driver: " + ", ".join(f"{k} {v}" for k, v in other.items()))
    L.append("Spoken (lap, metres, mph, cue | why | numbers | next brake point s, cue length s):")
    for r in spoken:
        L.append(f" L{r.get('lap')} {r.get('dist_m', '?')}m {_mph(r.get('kmh'))} \"{r.get('text')}\" | {_mph_text(r.get('why', ''))} | "
                 f"{_fmt_m(r)} | {r.get('next_brake_s', '-')} s, {r.get('dur_s', '-')} s")
    dropped = [r for r in rows if r.get("ev") == "dropped"][:40]
    if dropped:
        L.append("Dropped (lap, metres, cue | why):")
        L += [f" L{r.get('lap')} {r.get('dist_m', '?')}m \"{r.get('text')}\" | {r.get('dropped')}" for r in dropped]
    out = "\n".join(L)
    if len(out) > limit:
        out = out[:limit - 40].rsplit("\n", 1)[0] + "\n(cut short; the full log is in coach-logs)"
    return out


def folder_for(settings_dir) -> Path:
    return Path(settings_dir) / FOLDER
