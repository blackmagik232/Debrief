"""Read iRacing telemetry files (.ibt) from Documents\\iRacing\\telemetry. Read only: never writes or moves them.

An .ibt file holds (format as written by iRacing, version 2):
  - a header (tick rate, where the variable list, the session text and the samples are),
  - the session text (YAML): track, car, sessions, weather and the full CarSetup the driver drove with,
  - one fixed-size record per sample (60 a second) with every channel (Speed, Throttle, tyre data, ...).

What real files showed (checked on the owner's files, Nov 2026):
  - CarSetup is complete (tyres, chassis, dials, rear wing...). Values are text with units ("176 kPa", "-4.0 deg").
  - Tyre temperatures and wear (LFtempCL.., LFwearM..) only change when the car stops in the pit stall, so they
    are pit readings, not live readings. Hot pressures (LFpressure), ride heights and shock travel are live.
  - In a multi-class session DriverInfo lists every car: the driver's own car is Drivers[DriverCarIdx].

Nothing here needs iRacing running or pyirsdk. Channels are read lazily with numpy views over a memory map,
so a 250 MB race file costs almost nothing until a channel is asked for.
"""
from __future__ import annotations

import mmap
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

HEADER = struct.Struct("<10i")          # ver, status, tickRate, sessionInfoUpdate, sessionInfoLen, sessionInfoOffset,
                                        # numVars, varHeaderOffset, numBuf, bufLen
DISK = struct.Struct("<qddii")           # startDate, startTime, endTime, lapCount, recordCount (at offset 112)
VAR_SIZE = 144
TYPES = {0: np.dtype("S1"), 1: np.dtype("?"), 2: np.dtype("<i4"), 3: np.dtype("<u4"), 4: np.dtype("<f4"), 5: np.dtype("<f8")}

# iRacing's track surface values (PlayerTrackSurface / CarIdxTrackSurface)
NOT_IN_WORLD, OFF_TRACK, IN_PIT_STALL, APPROACHING_PITS, ON_TRACK = -1, 0, 1, 2, 3

# Free-text keys iRacing writes unquoted; a name with ': ' or a leading special character breaks YAML.
_TEXT_KEYS = ("UserName", "TeamName", "AbbrevName", "Initials", "DriverSetupName", "CarDesignStr", "CarNumber",
              "CarScreenName", "CarScreenNameShort", "TrackDisplayName", "TrackDisplayShortName", "TrackConfigName",
              "TrackCity", "TrackCountry", "SeriesName", "SessionName", "ClubName", "DivisionName", "CarClassShortName")
_TEXT_RE = re.compile(r"^(\s*(?:" + "|".join(_TEXT_KEYS) + r")): (.*)$", re.M)


class IbtError(Exception):
    pass


@dataclass
class Var:
    name: str
    type: int
    offset: int
    count: int
    unit: str
    desc: str


@dataclass
class Ibt:
    path: Path
    version: int
    tick_rate: int
    record_count: int
    start_date: int                      # unix seconds
    vars: dict = field(default_factory=dict)
    session_text: str = ""
    _mm: object = None
    _buf_offset: int = 0
    _buf_len: int = 0
    _cache: dict = field(default_factory=dict)
    _session: dict | None = None

    # ---- channels ----
    def has(self, name: str) -> bool:
        return name in self.vars

    def col(self, name: str) -> np.ndarray:
        """One channel as a numpy array, one value per sample (shape (n, count) for array channels)."""
        if name in self._cache:
            return self._cache[name]
        v = self.vars.get(name)
        if v is None:
            raise KeyError(name)
        dt = TYPES[v.type]
        n = self.record_count
        shape = (n,) if v.count == 1 else (n, v.count)
        strides = (self._buf_len,) if v.count == 1 else (self._buf_len, dt.itemsize)
        a = np.ndarray(shape=shape, dtype=dt, buffer=self._mm, offset=self._buf_offset + v.offset, strides=strides)
        a = np.array(a)                  # copy out of the map: small, and the file can be closed
        self._cache[name] = a
        return a

    def close(self):
        try:
            if self._mm is not None:
                self._mm.close()
        except Exception:  # noqa: BLE001
            pass
        self._mm = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    # ---- session text ----
    @property
    def session(self) -> dict:
        if self._session is None:
            self._session = parse_session(self.session_text)
        return self._session

    @property
    def setup(self) -> dict:
        """CarSetup as nested dicts of text values. Read by indentation, so odd driver names elsewhere never break it."""
        return parse_block(self.session_text, "CarSetup")

    def player(self) -> dict:
        di = self.session.get("DriverInfo") or {}
        idx = di.get("DriverCarIdx")
        for d in di.get("Drivers") or []:
            if d.get("CarIdx") == idx:
                return d
        return {}

    def car(self) -> dict:
        p = self.player()
        return {"name": p.get("CarScreenName") or "", "path": p.get("CarPath") or self.path.name.split("_")[0],
                "id": p.get("CarID"), "class": p.get("CarClassShortName") or ""}

    def track(self) -> dict:
        wi = self.session.get("WeekendInfo") or {}
        return {"name": wi.get("TrackDisplayName") or wi.get("TrackName") or "", "config": wi.get("TrackConfigName") or "",
                "id": wi.get("TrackID"), "code": wi.get("TrackName") or "",
                "length_km": number(wi.get("TrackLength")), "surface_temp": number(wi.get("TrackSurfaceTemp")),
                "air_temp": number(wi.get("TrackAirTemp")), "wet": _is_wet(wi)}

    def session_types(self) -> dict:
        out = {}
        for s in (self.session.get("SessionInfo") or {}).get("Sessions") or []:
            out[s.get("SessionNum")] = s.get("SessionType") or s.get("SessionName") or ""
        return out


def _is_wet(wi: dict) -> bool:
    opts = wi.get("WeekendOptions") or {}
    tw = str(opts.get("TrackWater") or wi.get("TrackWetness") or "")
    return bool(re.search(r"wet|damp|moist", tw, re.I))


def open_ibt(path) -> Ibt:
    path = Path(path)
    f = open(path, "rb")
    try:
        size = path.stat().st_size
        if size < 144:
            raise IbtError("too small to be a telemetry file")
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
    finally:
        f.close()
    try:
        (ver, _st, tick, _siu, silen, sioff, nvars, vho, nbuf, blen) = HEADER.unpack_from(mm, 0)
        if ver not in (1, 2) or not (1 <= tick <= 1000) or nvars <= 0 or blen <= 0 or nbuf < 1:
            raise IbtError("not an iRacing telemetry file")
        buf_off = struct.unpack_from("<4i", mm, 48)[1]
        start_date, _t0, _t1, _laps, recs = DISK.unpack_from(mm, 112)
        if not (0 < sioff < size and 0 < silen <= size - sioff and 0 < vho < size and 0 < buf_off <= size):
            raise IbtError("telemetry file header points outside the file")
        fit = (size - buf_off) // blen
        recs = min(recs, fit) if recs > 0 else fit
        vars_ = {}
        for i in range(nvars):
            o = vho + i * VAR_SIZE
            t, off, cnt = struct.unpack_from("<iii", mm, o)
            name = bytes(mm[o + 16:o + 48]).split(b"\0")[0].decode("latin-1")
            desc = bytes(mm[o + 48:o + 112]).split(b"\0")[0].decode("latin-1")
            unit = bytes(mm[o + 112:o + 144]).split(b"\0")[0].decode("latin-1")
            if t in TYPES and 0 <= off and off + TYPES[t].itemsize * max(cnt, 1) <= blen:
                vars_[name] = Var(name, t, off, max(cnt, 1), unit, desc)
        text = bytes(mm[sioff:sioff + silen]).split(b"\0")[0].decode("latin-1")
    except (struct.error, ValueError) as e:
        mm.close()
        raise IbtError(f"could not read the telemetry file ({e})") from e
    except IbtError:
        mm.close()
        raise
    return Ibt(path=path, version=ver, tick_rate=tick, record_count=int(recs), start_date=int(start_date), vars=vars_,
               session_text=text, _mm=mm, _buf_offset=buf_off, _buf_len=blen)


def parse_session(text: str) -> dict:
    """The session YAML as dicts. Names are quoted first so a driver called "O'Neil: fast" can't break it."""
    import yaml
    fixed = _TEXT_RE.sub(lambda m: f"{m.group(1)}: {_quote(m.group(2))}", text)
    try:
        d = yaml.safe_load(fixed)
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        out = {}
        for key in ("WeekendInfo", "DriverInfo", "SessionInfo"):
            try:
                blk = _block_text(fixed, key)
                if blk:
                    out[key] = (yaml.safe_load(blk) or {}).get(key) or {}
            except Exception:  # noqa: BLE001
                pass
        return out


def _quote(v: str) -> str:
    v = v.rstrip()
    return "'" + v.replace("'", "''") + "'"


def _block_text(text: str, key: str) -> str:
    m = re.search(rf"^{key}:\s*$", text, re.M)
    if not m:
        return ""
    end = re.search(r"^\S", text[m.end():], re.M)
    return text[m.start(): m.end() + (end.start() if end else len(text) - m.end())]


def parse_block(text: str, key: str) -> dict:
    """An indentation-nested 'Key: value' block as dicts of strings (no YAML library needed)."""
    blk = _block_text(text, key)
    if not blk:
        return {}
    root: dict = {}
    stack = [(-1, root)]
    for line in blk.splitlines()[1:]:
        if not line.strip() or line.strip().startswith("#"):
            continue
        ind = len(line) - len(line.lstrip(" "))
        k, sep, v = line.strip().partition(":")
        if not sep:
            continue
        while stack and stack[-1][0] >= ind:
            stack.pop()
        parent = stack[-1][1] if stack else root
        v = v.strip()
        if v == "":
            child: dict = {}
            parent[k] = child
            stack.append((ind, child))
        else:
            parent[k] = v
    return root


_NUM = re.compile(r"[-+]?\d+(?:\.\d+)?")


def number(v):
    """First number in a setup value: '176 kPa' -> 176.0, '+37 clicks' -> 37.0, '50.0%' -> 50.0, 'Track' -> None."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = _NUM.search(str(v))
    return float(m.group()) if m else None


def numbers(v) -> list:
    """Every number in a value: '44C, 45C, 47C' -> [44, 45, 47]."""
    return [float(x) for x in _NUM.findall(str(v or ""))]


def unit(v) -> str:
    m = re.match(r"\s*[-+]?\d+(?:\.\d+)?\s*(.*)$", str(v or ""))
    return m.group(1).strip() if m else ""


def flatten(setup: dict, prefix: str = "") -> dict:
    """{'Chassis': {'LeftFront': {'Camber': '-4.0 deg'}}} -> {'Chassis.LeftFront.Camber': '-4.0 deg'}."""
    out = {}
    for k, v in (setup or {}).items():
        p = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(flatten(v, p))
        else:
            out[p] = v
    return out


# ---------------------------------------------------------------- laps

def laps(ib: Ibt) -> list:
    """The driver's laps in this file: number, time, session type, and whether it was clean.

    A lap's time is LapLastLapTime as iRacing reports it just after the line. A lap is not clean when any of it was
    on pit road, off track, an incident was added, or iRacing gave no time. Session type comes from SessionNum."""
    if not ib.has("Lap") or not ib.has("LapLastLapTime"):
        return []
    lap = ib.col("Lap").astype(int)
    last = ib.col("LapLastLapTime").astype(float)
    sess = ib.col("SessionNum").astype(int) if ib.has("SessionNum") else np.zeros_like(lap)
    pit = ib.col("OnPitRoad").astype(bool) if ib.has("OnPitRoad") else np.zeros(len(lap), bool)
    surf = ib.col("PlayerTrackSurface").astype(int) if ib.has("PlayerTrackSurface") else np.full(len(lap), ON_TRACK)
    inc = ib.col("PlayerCarMyIncidentCount").astype(int) if ib.has("PlayerCarMyIncidentCount") else np.zeros(len(lap), int)
    ontrack = ib.col("IsOnTrack").astype(bool) if ib.has("IsOnTrack") else np.ones(len(lap), bool)
    types = ib.session_types()
    n = len(lap)
    if n < 2:
        return []
    out = []
    edges = np.nonzero((lap[1:] != lap[:-1]))[0] + 1         # first sample of each new lap
    starts = np.concatenate(([0], edges))
    ends = np.concatenate((edges, [n]))
    rate = ib.tick_rate or 60
    for i in range(len(starts) - 1):                        # the last lap in the file never finished
        a, b = int(starts[i]), int(ends[i])
        if lap[b] != lap[a] + 1:                            # a reset or tow, not a crossing
            continue
        if not ontrack[a:b].any():
            continue
        # LapLastLapTime updates about 2 s after the line (real files); read it within 4 s of the crossing.
        # iRacing gives -1 for a lap it did not time; then the lap is timed from the samples instead.
        look = last[b: min(n, b + 4 * rate)]
        prev = last[b - 1] if b > 0 else -1
        official = None
        for x in look:
            if abs(x - prev) > 1e-6:
                official = float(x) if x > 0 else None
                break
        measured = _crossing(ib, b, rate) - _crossing(ib, a, rate)
        if official is not None and abs(official - measured) > max(2.0, 0.05 * measured):
            official = None                                 # not this lap's time
        t = official if official is not None else (measured if a > 0 else None)
        stype = types.get(int(sess[a]), "")
        start_lap = stype.lower() == "race" and int(lap[a]) <= 1
        clean = (t is not None and not pit[a:b].any() and not (surf[a:b] == OFF_TRACK).any()
                 and inc[b - 1] == inc[a] and a > 0 and not start_lap)
        out.append({"lap": int(lap[a]), "time": t, "timed_by": "iracing" if official is not None else "samples",
                    "session_num": int(sess[a]), "session": stype, "start": a, "end": b, "clean": bool(clean),
                    "start_lap": bool(start_lap), "pit": bool(pit[a:b].any()),
                    "off": bool((surf[a:b] == OFF_TRACK).any()), "incidents": int(inc[b - 1] - inc[a])})
    return out


def _crossing(ib: Ibt, i: int, rate: int) -> float:
    """Time of the line crossing just before sample i, between samples, from LapDistPct (1.0 -> 0.0)."""
    if i <= 0 or not ib.has("LapDistPct"):
        return i / rate
    d = ib.col("LapDistPct")
    before, after = float(d[i - 1]), float(d[i])
    gap = (1.0 - before) + after
    frac = (1.0 - before) / gap if 0 < gap < 0.05 else 1.0
    return (i - 1 + frac) / rate


def summary(path) -> dict:
    """Small record of one file for the index: car, track, when, sessions, laps and setup. Never raises."""
    try:
        with open_ibt(path) as ib:
            lp = laps(ib)
            clean = [x["time"] for x in lp if x["clean"]]
            return {"file": Path(path).name, "ok": True, "start": ib.start_date, "car": ib.car(), "track": ib.track(),
                    "sessions": sorted(set(x["session"] for x in lp if x["session"])),
                    "laps": len(lp), "clean_laps": len(clean), "best": min(clean) if clean else None,
                    "setup": ib.setup, "setup_name": (ib.player().get("DriverSetupName") or "") if ib.player() else ""}
    except Exception as e:  # noqa: BLE001
        return {"file": Path(path).name, "ok": False, "error": str(e)[:200]}


# ---------------------------------------------------------------- writing fake files (tests only)

def write_fake(path, session_text: str, columns: dict, tick_rate: int = 60, start_date: int = 1_790_000_000):
    """Write a small valid .ibt for tests. columns: name -> (type, list of values) with type 'i','f','d','b'."""
    tmap = {"i": (2, "<i4"), "f": (4, "<f4"), "d": (5, "<f8"), "b": (1, "?")}
    names = list(columns)
    offs, off = {}, 0
    for nm in names:
        t, _vals = columns[nm]
        offs[nm] = off
        off += np.dtype(tmap[t][1]).itemsize
    blen = max(off, 4)
    n = max(len(v) for _t, v in columns.values())
    vho = 144
    sioff = vho + VAR_SIZE * len(names)
    sbytes = session_text.encode("latin-1") + b"\0"
    buf_off = sioff + len(sbytes)
    rec = np.zeros((n, blen), np.uint8)
    for nm in names:
        t, vals = columns[nm]
        dt = np.dtype(tmap[t][1])
        arr = np.asarray(vals, dtype=dt)
        rec[:, offs[nm]:offs[nm] + dt.itemsize] = arr.view(np.uint8).reshape(n, dt.itemsize)
    head = bytearray(buf_off)
    HEADER.pack_into(head, 0, 2, 1, tick_rate, 1, len(sbytes), sioff, len(names), vho, 1, blen)
    struct.pack_into("<4i", head, 48, 0, buf_off, 0, 0)
    DISK.pack_into(head, 112, start_date, 0.0, n / tick_rate, 0, n)
    for i, nm in enumerate(names):
        o = vho + i * VAR_SIZE
        struct.pack_into("<iii?", head, o, tmap[columns[nm][0]][0], offs[nm], 1, False)
        head[o + 16:o + 16 + len(nm)] = nm.encode()
    head[sioff:sioff + len(sbytes)] = sbytes
    Path(path).write_bytes(bytes(head) + rec.tobytes())
