"""The background recorder (2.27 "Live data"): writes down every iRacing session while you drive it.

Garage 61 has your laps. It does not have your fuel, your driver aids, your pit service, your tyres, your incidents with
the lap they happened on, or what the other 60 cars were doing. iRacing shows all of that only while it runs, so the
recorder reads it live (livedata.py) and keeps it, forever, in:

    <library folder>\\Recorded\\<2026-11-08_1930>_<track>_<subsession or local>\\
        session.yaml.json   iRacing's session text (track, car, drivers, results), only what changed since the last copy
        car.jsonl.gz        your car once a second, plus one row per lap (time, fuel used, aids, incidents, pit)
        field.jsonl.gz      every car's lap, position, pit road and gaps once a second (a short row; unchanged lists left out)
        summary.json        the finished picture, written when the session ends or iRacing closes

One folder per iRacing subsession. Practice, qualifying and the race of one subsession share the folder and are
separate sections inside it (each row carries its SessionNum).

Safe against a crash or a power cut: rows are written in small complete gzip pieces every FLUSH_S seconds (at most that
much is lost), and a folder whose summary.json is missing or not final is finished the next time the recorder starts.
Only one recorder runs at a time (RecorderLock): the Debrief window records for itself when no tray recorder is
running; if one is, the window just shows what it says. Recordings are never deleted by Debrief.

Reading only. This module never changes iRacing, never asks it to write telemetry files, and never touches Garage 61.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import statistics
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import app, fsutil, livedata, perfmeter

VERSION = 1
SUMMARY_V = 2       # 2.35: green-flag stop and aid filtering, gaps, AI count (older summaries are worked out again)
RACING = 4          # irsdk SessionState: racing (green flag)
FOLDER = "Recorded"
CAR, FIELD, INFO, SUMMARY = "car.jsonl.gz", "field.jsonl.gz", "session.yaml.json", "summary.json"
FLUSH_S = 30.0            # complete gzip pieces are appended this often
ROW_S = 1.0               # one row a second in both logs
FIELD_HZ = 4.0
GONE_S = 6.0              # iRacing not answering for this long = it has closed
TEL_ASK_S = 10.0          # 2.32: ask iRacing to save telemetry at most this often...
TEL_ASKS = 3              # ...and this many times per iRacing connection, then say it didn't work
BEAT_S = 3.0              # lock/status file refresh; a lock older than STALE_S is a dead recorder's
STALE_S = 20.0
INFO_EVERY_S = 300.0      # the bulky SessionInfo part is copied at most this often (and at each end)
KEYFRAME = 60             # a field row writes every list in full this often
RUN_VALUE = "Debrief Recorder"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

SESSION_STATE = {0: "invalid", 1: "get in car", 2: "warmup", 3: "parade laps", 4: "racing", 5: "checkered", 6: "cool down"}

# (iRacing channel, short key in car.jsonl.gz, decimals or None)
CAR_ROW = (("Lap", "lap", None), ("LapDistPct", "d", 3), ("Speed", "v", 1), ("FuelLevel", "fuel", 2),
           ("FuelLevelPct", "fp", 3), ("FuelUsePerHour", "fh", 2), ("dcBrakeBias", "bb", 2), ("dcABS", "abs", 2),
           ("dcTractionControl", "tc", 2), ("OnPitRoad", "pit", "i"), ("PitSvFlags", "psf", None),
           ("PitSvFuel", "psv", 2), ("PlayerCarPitSvStatus", "pss", None), ("PitstopActive", "pa", "i"),
           ("PlayerCarInPitStall", "ps", "i"), ("PitRepairLeft", "rep", 1), ("PitOptRepairLeft", "orep", 1),
           ("FastRepairAvailable", "fra", None), ("FastRepairUsed", "fru", None), ("TireSetsUsed", "tsu", None),
           ("TireSetsAvailable", "tsa", None), ("PlayerCarMyIncidentCount", "ti", None),
           ("PlayerCarTeamIncidentCount", "tti", None), ("PlayerCarDriverIncidentCount", "dii", None),
           ("DCDriversSoFar", "drv", None), ("DCLapStatus", "dls", None), ("SessionTimeRemain", "rem", 0),
           ("SessionLapsRemainEx", "lrem", None), ("SessionFlags", "fl", None), ("SessionState", "st", None),
           ("TrackTempCrew", "tt", 1), ("AirTemp", "at", 1), ("TrackWetness", "wet", None),
           ("PlayerTrackSurface", "trk", None), ("IsOnTrack", "ont", "i"), ("IsDiskLoggingEnabled", "dle", "i"),
           ("PlayerCarPosition", "pos", None), ("PlayerCarClassPosition", "cpos", None))
WEAR = (("LFwearM", "RFwearM", "LRwearM", "RRwearM"))
AIDS = (("dcBrakeBias", "bb", "brake bias"), ("dcABS", "abs", "ABS"), ("dcTractionControl", "tc", "traction control"))

# field.jsonl.gz: (iRacing array, short key, how to store each value)
FIELD_ROW = (("CarIdxLapCompleted", "lap", "i"), ("CarIdxLapDistPct", "pct", "m"), ("CarIdxLastLapTime", "last", 3),
             ("CarIdxBestLapTime", "best", 3), ("CarIdxPosition", "pos", "i"), ("CarIdxClassPosition", "cpos", "i"),
             ("CarIdxF2Time", "f2", 1), ("CarIdxEstTime", "est", 1), ("CarIdxOnPitRoad", "pit", "i"),
             ("CarIdxTrackSurface", "surf", "i"), ("CarIdxTireCompound", "tcmp", "i"),
             ("CarIdxFastRepairsUsed", "frp", "i"))


def say(msg: str):
    try:
        app.say(msg)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- small helpers
def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def epoch_of(iso) -> float:
    try:
        return datetime.fromisoformat(str(iso)).timestamp()
    except ValueError:
        return time.time()


def recordings_root(library_folder) -> Path:
    return Path(library_folder) / FOLDER


def _num(x):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) != float("inf") else None


def _store(v, how):
    """One value as it is written to a log."""
    if v is None:
        return None
    if isinstance(v, list):
        return [_store(x, how) for x in v]
    if how == "i":
        try:
            return int(v)
        except (TypeError, ValueError):
            return None
    if how == "m":                      # lap fraction -> thousandths
        f = _num(v)
        return -1 if f is None or f < 0 else int(round(f * 1000))
    if how is None:
        return int(v) if isinstance(v, bool) else v
    f = _num(v)
    if f is None:
        return None
    return int(round(f)) if how == 0 else round(f, how)


def valid_time(x):
    x = _num(x)
    return x if x is not None and x > 1.0 else None


class GzJsonl:
    """Rows kept in memory and appended to a .gz file as one complete gzip piece every `flush_s`. Any number of pieces
    read back as one stream, and a crash loses only the rows not written yet."""

    def __init__(self, path, flush_s: float = FLUSH_S, clock=time.monotonic):
        self.path, self.flush_s, self.clock = Path(path), flush_s, clock
        self.rows: list = []
        self.last = clock()

    def add(self, row: dict):
        self.rows.append(json.dumps(row, separators=(",", ":")))
        if self.clock() - self.last >= self.flush_s:
            self.flush()

    def flush(self):
        self.last = self.clock()
        if not self.rows:
            return
        data = ("\n".join(self.rows) + "\n").encode("utf-8")
        self.rows = []
        with open(self.path, "ab") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=6) as gz:
                gz.write(data)


def read_jsonl(path):
    """Rows of a .jsonl.gz, whole pieces and the good part of a damaged last one. Never raises."""
    try:
        f = gzip.open(path, "rt", encoding="utf-8")
    except OSError:
        return
    try:
        for line in f:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                yield row
    except (EOFError, OSError, ValueError):
        return
    finally:
        try:
            f.close()
        except Exception:  # noqa: BLE001
            pass


def expand_field(rows):
    """field.jsonl.gz rows with the lists left out (unchanged) filled in from the row before."""
    cur: dict = {}
    for r in rows:
        cur.update({k: v for k, v in r.items() if k not in ("t", "sn", "w", "k")})
        out = dict(cur)
        for k in ("t", "sn", "w", "k"):
            if k in r:
                out[k] = r[k]
        yield out


def merged_info(folder) -> dict:
    """The newest copy of WeekendInfo / DriverInfo / SessionInfo from session.yaml.json."""
    try:
        doc = json.loads((Path(folder) / INFO).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict = {}
    for snap in doc.get("snapshots") or []:
        for k in livedata.INFO_KEYS:
            if k in snap:
                out[k] = snap[k]
    return out


# ---------------------------------------------------------------- settings of the recorder
def _state_path() -> Path:
    return app.settings_path().parent / "liverec.json"


def load_state() -> dict:
    try:
        d = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def save_state(**kw) -> None:
    d = load_state()
    d.update(kw)
    try:
        fsutil.write_json(_state_path(), d, mkdir=True)
    except OSError:
        pass


def prefs() -> dict:
    """{'record_sessions', 'start_with_windows', 'library_folder'} from settings.toml; the defaults (on, on) when it
    can't be read."""
    try:
        cfg = app.load_settings()
        return {"record_sessions": cfg["record_sessions"], "start_with_windows": cfg["start_with_windows"],
                "ensure_telemetry": cfg.get("ensure_telemetry") is True, "library_folder": cfg["library_folder"]}
    except (SystemExit, OSError, KeyError, ValueError):
        return {"record_sessions": True, "start_with_windows": True, "ensure_telemetry": False,
                "library_folder": str(app.documents_dir() / "Debrief Reviews")}


# ---------------------------------------------------------------- one recorder at a time
def _pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)      # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return False
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False
    except Exception:  # noqa: BLE001
        return True


def lock_path() -> Path:
    return app.settings_path().parent / "recorder.json"


def stop_path() -> Path:
    return app.settings_path().parent / "recorder.stop"


def read_lock(path=None, stale_s: float = STALE_S, clock=time.time) -> dict | None:
    """What the running recorder last wrote, with 'alive': True when it beat within stale_s seconds and its process
    exists. None when there is no file."""
    p = Path(path or lock_path())
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        age = clock() - p.stat().st_mtime
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict):
        return None
    d["age_s"] = round(age, 1)
    d["alive"] = age <= stale_s and _pid_alive(d.get("pid"))
    return d


class RecorderLock:
    """recorder.json in the settings folder is both the lock and the status the window reads. The holder rewrites it
    every few seconds; one nobody has rewritten for STALE_S seconds (or whose process is gone) is up for grabs."""

    def __init__(self, role: str, path=None, stale_s: float = STALE_S, clock=time.time):
        self.role, self.path, self.stale_s, self.clock = role, Path(path or lock_path()), stale_s, clock
        self.token = uuid.uuid4().hex
        self.held = False

    def acquire(self) -> bool:
        cur = read_lock(self.path, self.stale_s, self.clock)
        if cur and cur.get("alive") and cur.get("token") != self.token:
            return False
        self.held = True
        self.beat({"state": "starting"})
        back = read_lock(self.path, self.stale_s, self.clock)       # two starting together: the last writer wins
        self.held = bool(back and back.get("token") == self.token)
        return self.held

    def beat(self, status: dict | None = None):
        if not self.held:
            return
        doc = dict(status or {})
        doc.update(token=self.token, pid=os.getpid(), role=self.role, version=_version())
        try:
            fsutil.write_json(self.path, doc, mkdir=True)
        except OSError:
            pass

    def release(self):
        if not self.held:
            return
        self.held = False
        d = read_lock(self.path, self.stale_s, self.clock)
        if d and d.get("token") == self.token:
            try:
                self.path.unlink()
            except OSError:
                pass


def _version() -> str:
    try:
        from . import __version__
        return __version__
    except Exception:  # noqa: BLE001
        return ""


def request_stop(wait_s: float = 0.0) -> bool:
    """Ask a running recorder to quit (the installer does this before it replaces Debrief.exe). True when none is
    running (any more)."""
    cur = read_lock()
    if not cur or not cur.get("alive"):
        return True
    try:
        stop_path().write_text("stop", encoding="utf-8")
    except OSError:
        return False
    end = time.monotonic() + wait_s
    while time.monotonic() < end:
        d = read_lock()
        if not d or not d.get("alive"):
            return True
        time.sleep(0.25)
    d = read_lock()
    return not d or not d.get("alive")


def status_view() -> dict:
    """What Settings shows: {'state', 'text', 'running', 'by'}. Read from the running recorder's file, so it is the same
    whether the recorder is in this window or in the tray."""
    d = read_lock()
    if not d or not d.get("alive"):
        return {"state": "off", "text": "Not running", "running": False, "by": None}
    return {"state": d.get("state") or "waiting", "text": d.get("text") or "Waiting for iRacing", "running": True,
            "by": d.get("role"), "session": d.get("session"), "disk_logging": d.get("disk_logging"),
            "telemetry": d.get("telemetry"),
            "perf": d.get("perf")}


# ---------------------------------------------------------------- start with Windows (the Run key)
def _winreg():
    if os.name != "nt":
        return None
    try:
        import winreg
        return winreg
    except ImportError:
        return None


def autostart_command(exe: str | None = None, frozen: bool | None = None) -> str:
    """The command the Run key holds: the installed Debrief.exe with --recorder (from source: pythonw -m debrief.desktop)."""
    frozen = app.FROZEN if frozen is None else frozen
    if frozen:
        return f'"{exe or sys.executable}" --recorder'
    py = exe or sys.executable
    pyw = Path(py).with_name("pythonw.exe")
    if os.name == "nt" and pyw.exists():
        py = str(pyw)
    return f'"{py}" -m debrief.desktop --recorder'


def autostart_value(winreg=None) -> str | None:
    wr = winreg or _winreg()
    if wr is None:
        return None
    try:
        with wr.OpenKey(wr.HKEY_CURRENT_USER, RUN_KEY, 0, wr.KEY_READ) as k:
            v, _t = wr.QueryValueEx(k, RUN_VALUE)
            return str(v)
    except OSError:
        return None


def set_autostart(enabled: bool, winreg=None, command: str | None = None) -> bool:
    """Write (or remove) the Run value "Debrief Recorder" for this Windows account. Returns True when the registry now
    says what was asked. Off Windows (or with winreg missing) nothing is done and the answer is False."""
    wr = winreg or _winreg()
    if wr is None:
        return False
    try:
        if enabled:
            with wr.CreateKeyEx(wr.HKEY_CURRENT_USER, RUN_KEY, 0, wr.KEY_SET_VALUE) as k:
                wr.SetValueEx(k, RUN_VALUE, 0, wr.REG_SZ, command or autostart_command())
        else:
            try:
                with wr.OpenKey(wr.HKEY_CURRENT_USER, RUN_KEY, 0, wr.KEY_SET_VALUE) as k:
                    wr.DeleteValue(k, RUN_VALUE)
            except FileNotFoundError:
                pass
        return True
    except OSError as e:
        say(f"liverec: couldn't {'set' if enabled else 'remove'} the start-with-Windows entry ({type(e).__name__}).")
        return False


def reconcile_autostart(winreg=None, command: str | None = None) -> str:
    """Make the Run key match Settings (on by default; on at the first start after the 2.27 update). Returns what it did:
    'on', 'off', 'unchanged' or 'unavailable'."""
    wr = winreg or _winreg()
    if wr is None:
        return "unavailable"
    want = prefs()["start_with_windows"]
    cmd = command or autostart_command()
    have = autostart_value(wr)
    if want and have == cmd:
        return "unchanged"
    if not want and have is None:
        return "unchanged"
    ok = set_autostart(want, wr, cmd)
    if ok and not load_state().get("autostart_applied"):
        save_state(autostart_applied=True)
    return ("on" if want else "off") if ok else "unavailable"


def paused() -> bool:
    return bool(load_state().get("paused"))


def set_paused(v: bool):
    save_state(paused=bool(v))


# ---------------------------------------------------------------- the session being recorded
class _Section:
    """One SessionNum inside a subsession: lap, fuel, aid and incident bookkeeping between rows."""

    def __init__(self, num, type_, opened_t):
        self.num, self.type = num, type_
        self.opened_t = opened_t
        self.lap = None
        self.lap_burn = 0.0
        self.fuel_prev = None
        self.lap_pit = False
        self.lap_inc = 0
        self.changes: list = []
        self.aids: dict = {}
        self.inc = None
        self.first_lap = True
        self.laps_done = 0
        self.pit_prev = None


class _Session:
    def __init__(self, folder: Path, sub, track, started_iso, flush_s, clock):
        self.folder, self.sub, self.track, self.started = folder, sub, track, started_iso
        self.car = GzJsonl(folder / CAR, flush_s, clock)
        self.field = GzJsonl(folder / FIELD, flush_s, clock)
        self.sec: _Section | None = None
        self.snaps: list = []
        self.hashes: dict = {}
        self.info_at = -1e9
        self.last_t = None
        self.next_row = 0.0
        self.field_prev: dict = {}
        self.field_rows = 0
        self.me_pos = (None, None)
        self.me_idx = None
        self.disk_logging = None
        self.last_secs: dict = {}
        self.field_next = 0.0


class Recorder:
    """Reads iRacing and records it. Run it with start() (its own thread) or drive tick() yourself (the tests do).

        rec = Recorder(library_folder=lambda: ..., role="window")
        rec.start(); rec.status(); rec.pause(); rec.resume(); rec.stop()
    """

    def __init__(self, reader=None, library_folder=None, role: str = "window", lock: RecorderLock | None = None,
                 clock=time.monotonic, wall=None, flush_s: float = FLUSH_S, log=None, prefs_fn=None,
                 use_lock: bool = True, on_finished=None, utc=None):
        self.reader = reader or livedata.LiveReader(rate_hz=20.0, retry_s=3.0, own=livedata.COACH_CHANNELS + livedata.OWN_EXTRA,
                                                    field=livedata.FIELD_CHANNELS)
        self._lib = library_folder
        self.role, self.clock, self.flush_s = role, clock, flush_s
        self.wall = wall or (lambda: datetime.now())
        self.say = log or say
        self.prefs_fn = prefs_fn or prefs
        self.perf = perfmeter.PerfMeter("recorder", on_slow=lambda m: self.say("liverec: " + m))
        self.lock = lock if lock is not None else (RecorderLock(role) if use_lock else None)
        self.cur: _Session | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._state = "starting"
        self._text = "Starting"
        self._last_ok = None
        self._seen_ver = -1
        self._next_beat = 0.0
        self._next_field = 0.0
        self._prefs = {}
        self._prefs_at = -1e9
        self._pause_local = False
        self.recovered: list = []
        self.errors = 0
        self.has_lock = False
        self.on_finished = on_finished
        self.utc = utc or now_utc           # the wall clock as an ISO UTC string (the tests give a scripted one)
        self.finished: list = []              # folders finished by this recorder (the tests and the window read it)
        # 2.32 "Make iRacing save my telemetry": None (switched off), "saving", "asked", "failed" or "off_track"
        self.tel_state = None
        self._tel_asks = 0
        self._tel_next = 0.0
        self.tel_sent = 0                     # requests sent to iRacing in this recorder's life (the tests read it)

    def _w(self) -> int:
        return int(epoch_of(self.utc()))

    # ------------------------------------------------------------ control
    def pause(self):
        set_paused(True)
        self._pause_local = True

    def resume(self):
        set_paused(False)
        self._pause_local = False

    def is_paused(self) -> bool:
        return paused() or self._pause_local

    def enabled(self) -> bool:
        return bool(self._prefs.get("record_sessions", True)) and not self.is_paused()

    def library(self) -> Path | None:
        f = self._prefs.get("library_folder") if self._prefs else None
        if self._lib is not None:
            f = self._lib() if callable(self._lib) else self._lib
        return Path(f) if f else None

    def start(self) -> bool:
        """Take the lock and start the thread. False when another recorder is running (nothing is started)."""
        if self._thread is not None and self._thread.is_alive():
            return True
        if self.lock is not None and not self.lock.acquire():
            self.say(f"liverec: another recorder is already running; this {self.role} does not record.")
            return False
        self.has_lock = True
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="liverec")
        self._thread.start()
        return True

    def stop(self, timeout: float = 5.0):
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout)
        self._shutdown()

    def _shutdown(self):
        try:
            self._end_session("Debrief closed")
        except Exception:  # noqa: BLE001
            self.say("liverec: ERROR finishing the session at shutdown:\n" + _tb())
        try:
            self.reader.close()
        except Exception:  # noqa: BLE001
            pass
        if self.lock is not None:
            self.lock.release()
        self.has_lock = False

    def _run(self):
        self._housekeeping(force=True)
        self._recover()
        while not self._stop.is_set():
            t0 = time.perf_counter()
            try:
                with self.perf.step():
                    busy = self.tick()
            except Exception:  # noqa: BLE001
                self.errors += 1
                if self.errors <= 5 or self.errors % 100 == 0:
                    self.say("liverec: ERROR while recording (it carries on):\n" + _tb())
                busy = False
            pause = (getattr(self.reader, "interval", 0.05) - (time.perf_counter() - t0)) if busy else 1.0
            self._stop.wait(max(0.005, pause))
        self._shutdown()          # a quit request ended the loop, or stop() did: finish the session and let go

    # ------------------------------------------------------------ one pass
    def _housekeeping(self, force=False):
        now = self.clock()
        if not force and now < self._next_beat:
            return
        self._next_beat = now + BEAT_S
        self._prefs = self.prefs_fn()
        self._prefs_at = now
        if self.lock is not None and self.has_lock:
            if stop_path().exists():
                try:
                    stop_path().unlink()
                except OSError:
                    pass
                self.say("liverec: asked to quit (by the installer or a second start).")
                self._stop.set()
            self.lock.beat(self.status_doc())

    def tick(self) -> bool:
        """One pass. True while iRacing is answering (call again soon), False when there is nothing to do."""
        self._housekeeping()
        if not self.enabled():
            if self._prefs.get("ensure_telemetry") is True:
                o = self.reader.read_own(keep_none=False)
                self._telemetry_guard(o, self.clock())
            if self.cur is not None:
                self._end_session("paused")
            self._state, self._text = "paused", "Paused"
            if not self._prefs.get("record_sessions", True):
                self._text = "Off in Settings"
            return False
        now = self.clock()
        own = self.reader.read_own(keep_none=False)
        if own is None:
            self._telemetry_guard(None, now)
            if self.cur is not None and self._last_ok is not None and now - self._last_ok > GONE_S:
                self._end_session("iRacing closed")
            if self.cur is None:
                self._state, self._text = "waiting", "Waiting for iRacing"
            return False
        self._last_ok = now
        self._telemetry_guard(own, now)
        ver, secs = self.reader.info_sections()
        if not secs:
            self._state, self._text = "waiting", "iRacing is up; reading the session"
            return True
        self._follow(own, secs, ver)
        sess = self.cur
        if sess is None:
            return True
        self._own(sess, own, secs, now)
        if now >= self._next_field:
            self._next_field = now + 1.0 / FIELD_HZ
            f = self.reader.read_field()
            if f:
                self._field(sess, f, own, now)
        self._state = "recording"
        self._text = self._recording_text(sess)
        return True

    # ------------------------------------------------------------ 2.32: iRacing's own telemetry
    def _telemetry_guard(self, own, now):
        """With "Make iRacing save my telemetry" on: when you are in the car and iRacing isn't saving its telemetry file,
        ask it to start (the same as pressing Alt+L), at most every TEL_ASK_S seconds and TEL_ASKS times per iRacing
        connection. Never asks to stop, and does nothing at all with the switch off."""
        if self._prefs.get("ensure_telemetry") is not True:
            self.tel_state = None
            return
        if own is None:                       # iRacing closed: the next connection gets fresh tries
            self._tel_asks, self._tel_next = 0, 0.0
            if self.tel_state in ("failed", "asked", "off_track"):
                self.tel_state = None
            return
        saving = own.get("IsDiskLoggingEnabled")
        if saving is None:                    # this iRacing doesn't say: nothing safe to do
            return
        if saving:
            if self.tel_state in ("asked", "failed"):
                self.say("liverec: iRacing is saving telemetry now.")
            self.tel_state, self._tel_asks = "saving", 0
            return
        if not own.get("IsOnTrack"):
            if self.tel_state != "failed":
                self.tel_state = "off_track"
            return
        if self._tel_asks >= TEL_ASKS:
            if self.tel_state != "failed":
                self.say("liverec: asked iRacing %d times to save telemetry and it still isn't; press Alt+L in the car." % TEL_ASKS)
            self.tel_state = "failed"
            return
        if now < self._tel_next:
            return
        self._tel_next = now + TEL_ASK_S
        self._tel_asks += 1
        ask = getattr(self.reader, "telemetry_start", None)
        if ask is not None and ask():
            self.tel_sent += 1
            self.tel_state = "asked"
            self.say("liverec: iRacing wasn't saving telemetry; asked it to start (Settings > Live data).")

    # ------------------------------------------------------------ which folder
    @staticmethod
    def _ids(secs):
        wi = secs.get("WeekendInfo") or {}
        sub = wi.get("SubSessionID")
        try:
            sub = int(sub)
        except (TypeError, ValueError):
            sub = 0
        return sub, str(wi.get("TrackName") or ""), wi

    def _follow(self, own, secs, ver):
        sub, trk, wi = self._ids(secs)
        t = _num(own.get("SessionTime"))
        cur = self.cur
        if cur is not None:
            went_back = (sub == 0 and t is not None and cur.last_t is not None and t < cur.last_t - 5.0
                         and (cur.sec is None or own.get("SessionNum") == cur.sec.num))
            if sub != (cur.sub or 0) or (sub == 0 and trk and cur.track and trk != cur.track) or went_back:
                self._end_session("next session started")
                cur = None
        if cur is None:
            lib = self.library()
            if lib is None:
                return
            cur = self._open_session(lib, sub, wi, secs)
            self.cur = cur
            self._seen_ver = -1
        cur.last_t = t if t is not None else cur.last_t
        cur.last_secs = secs
        if ver != self._seen_ver:
            self._seen_ver = ver
            self._snapshot(cur, secs, own)
        sn = own.get("SessionNum")
        if cur.sec is None or (sn is not None and sn != cur.sec.num):
            self._open_section(cur, sn, secs, t)

    def _open_session(self, lib, sub, wi, secs) -> _Session:
        from . import library as _lib
        track = wi.get("TrackDisplayName") or wi.get("TrackName") or "track"
        stamp = self.wall().strftime("%Y-%m-%d_%H%M")
        base = f"{stamp}_{_lib.slugify(track) or 'track'}_{sub or 'local'}"
        root = recordings_root(lib)
        root.mkdir(parents=True, exist_ok=True)
        d, n = root / base, 1
        while d.exists():
            n += 1
            d = root / f"{base}-{n}"
        d.mkdir()
        s = _Session(d, sub or None, str(wi.get("TrackName") or ""), self.utc(), self.flush_s, self.clock)
        di = secs.get("DriverInfo") or {}
        s.me_idx = di.get("DriverCarIdx")
        self.say(f"liverec: recording {track} to {d.name}.")
        return s

    def _open_section(self, s: _Session, num, secs, t):
        if s.sec is not None:
            self._close_section(s, secs)
        types = {x.get("SessionNum"): x.get("SessionType") or x.get("SessionName") or ""
                 for x in (secs.get("SessionInfo") or {}).get("Sessions") or []}
        s.sec = _Section(num, types.get(num, ""), t)
        s.car.add({"k": "open", "sn": num, "t": _store(t, 1), "w": self._w(), "at": self.utc(), "type": s.sec.type})
        s.next_row = 0.0
        s.field_next = 0.0

    def _close_section(self, s: _Session, secs):
        sec = s.sec
        if sec is None:
            return
        s.car.add({"k": "close", "sn": sec.num, "t": _store(s.last_t, 1), "w": self._w(), "at": self.utc()})
        s.car.flush()
        s.field.flush()
        self._snapshot(s, secs or s.last_secs, None, force=True)
        self._write_summary(s, final=False, reason="")
        s.sec = None

    # ------------------------------------------------------------ session info copies
    def _snapshot(self, s: _Session, secs, own, force=False):
        now = self.clock()
        snap = {}
        for k in livedata.INFO_KEYS:
            v = secs.get(k)
            if v is None:
                continue
            if k == "SessionInfo" and not force and now - s.info_at < INFO_EVERY_S and k in s.hashes:
                continue
            h = hashlib.sha1(json.dumps(v, sort_keys=True, default=str).encode("utf-8")).hexdigest()
            if s.hashes.get(k) == h:
                continue
            s.hashes[k] = h
            snap[k] = v
            if k == "SessionInfo":
                s.info_at = now
        if not snap:
            return
        snap = {"at": self.utc(), "t": _store((own or {}).get("SessionTime") if own else s.last_t, 1),
                "counter": self._seen_ver, **snap}
        s.snaps.append(snap)
        di = secs.get("DriverInfo") or {}
        if di.get("DriverCarIdx") is not None:
            s.me_idx = di.get("DriverCarIdx")
        self._write_info(s)

    def _write_info(self, s: _Session):
        doc = {"version": VERSION, "started": s.started, "subsession": s.sub, "snapshots": s.snaps}
        try:
            fsutil.write_json(s.folder / INFO, doc, default=str)
        except OSError as e:
            self.say(f"liverec: couldn't write {INFO} ({type(e).__name__}).")

    # ------------------------------------------------------------ your car
    def _own(self, s: _Session, o: dict, secs, now):
        sec = s.sec
        if sec is None:
            return
        t = _num(o.get("SessionTime"))
        lap = o.get("Lap")
        w = self._w()
        # aids: any change is kept with where on the lap it happened
        for ch, key, label in AIDS:
            v = o.get(ch)
            if v is None:
                continue
            v = round(float(v), 2)
            old = sec.aids.get(key)
            if old is not None and v != old and len(sec.changes) < 60:
                sec.changes.append({"aid": key, "from": old, "to": v, "t": _store(t, 1), "d": _store(o.get("LapDistPct"), 3)})
            sec.aids[key] = v
        # incidents
        inc = o.get("PlayerCarMyIncidentCount")
        if inc is not None:
            if sec.inc is not None and inc > sec.inc:
                n = int(inc - sec.inc)
                sec.lap_inc += n
                s.car.add({"k": "inc", "sn": sec.num, "t": _store(t, 1), "w": w, "lap": lap, "d": _store(o.get("LapDistPct"), 3),
                           "n": n, "total": int(inc)})
            sec.inc = int(inc)
        # fuel burnt: only drops count, so a refuel is not "negative fuel"
        f = _num(o.get("FuelLevel"))
        if f is not None:
            if sec.fuel_prev is not None and f < sec.fuel_prev:
                sec.lap_burn += sec.fuel_prev - f
            sec.fuel_prev = f
        if o.get("OnPitRoad"):
            sec.lap_pit = True
        # the lap line
        if lap is not None:
            if sec.lap is None:
                sec.lap = lap
                sec.lap_pit = bool(o.get("OnPitRoad"))
            elif lap != sec.lap:
                if lap > sec.lap:
                    self._finish_lap(s, o, t, w)
                sec.lap = lap
                sec.lap_burn, sec.lap_inc, sec.changes = 0.0, 0, []
                sec.lap_pit = bool(o.get("OnPitRoad"))
        if o.get("IsDiskLoggingEnabled") is not None:
            s.disk_logging = bool(o.get("IsDiskLoggingEnabled"))
        # the 1 Hz row
        if t is not None and t >= s.next_row:
            s.next_row = t + ROW_S
            row = {"k": "s", "sn": sec.num, "t": _store(t, 1), "w": w}
            for ch, key, how in CAR_ROW:
                v = o.get(ch)
                if v is not None:
                    row[key] = _store(v, how)
            wear = [o.get(c) for c in WEAR]
            if any(x is not None for x in wear):
                row["w4"] = [None if x is None else round(float(x), 3) for x in wear]
            s.car.add(row)

    def _finish_lap(self, s: _Session, o, t, w):
        sec = s.sec
        pos, cpos = s.me_pos
        if pos is None:
            pos = o.get("PlayerCarPosition")
        if cpos is None:
            cpos = o.get("PlayerCarClassPosition")
        have_fuel = sec.fuel_prev is not None
        row = {"k": "lap", "sn": sec.num, "t": _store(t, 1), "w": w, "lap": sec.lap, "time": valid_time(o.get("LapLastLapTime")),
               "fuel_used": round(sec.lap_burn, 3) if have_fuel else None,
               "fuel": round(sec.fuel_prev, 2) if have_fuel else None,
               "aids": dict(sec.aids), "chg": list(sec.changes), "inc": sec.lap_inc, "pit": bool(sec.lap_pit),
               "pos": pos if pos else None, "cpos": cpos if cpos else None}
        if sec.first_lap:
            row["partial"] = True            # joined part-way round: its fuel and time are not a whole lap
            sec.first_lap = False
        if o.get("DCDriversSoFar") is not None:
            row["drv"] = o.get("DCDriversSoFar")
        s.car.add(row)
        sec.laps_done += 1

    # ------------------------------------------------------------ the field
    def _field(self, s: _Session, f: dict, o, now):
        sec = s.sec
        if sec is None:
            return
        t = _num(o.get("SessionTime"))
        cur = {}
        for ch, key, how in FIELD_ROW:
            v = f.get(ch)
            if v is not None:
                cur[key] = _store(list(v), how)
        idx = s.me_idx
        for key, i in (("pos", 0), ("cpos", 1)):
            arr = cur.get(key)
            if arr and isinstance(idx, int) and 0 <= idx < len(arr):
                val = arr[idx]
                s.me_pos = (val if i == 0 else s.me_pos[0], val if i == 1 else s.me_pos[1])
        due = t is not None and t >= s.field_next
        pit_changed = any(cur.get(k) != s.field_prev.get(k) for k in ("pit", "surf")) and bool(s.field_prev)
        if not (due or pit_changed):
            return
        if due:
            s.field_next = t + ROW_S
        keyframe = s.field_rows % KEYFRAME == 0
        row = {"k": "f", "sn": sec.num, "t": _store(t, 1), "w": self._w()}
        for key, v in cur.items():
            if keyframe or s.field_prev.get(key) != v:
                row[key] = v
        s.field_prev = cur
        s.field_rows += 1
        s.field.add(row)

    # ------------------------------------------------------------ finishing
    def _end_session(self, reason: str):
        s = self.cur
        if s is None:
            return
        self.cur = None
        self._last_ok = None
        try:
            if s.sec is not None:
                s.car.add({"k": "close", "sn": s.sec.num, "t": _store(s.last_t, 1), "w": self._w(), "at": self.utc()})
            s.car.flush()
            s.field.flush()
            if s.last_secs:
                self._snapshot(s, s.last_secs, None, force=True)
            self._write_summary(s, final=True, reason=reason)
        except Exception:  # noqa: BLE001
            self.say("liverec: ERROR finishing a recording (the rows are kept; it is finished at the next start):\n" + _tb())
        self.finished.append(s.folder)
        if self.on_finished is not None:
            threading.Thread(target=self._notify, args=(s.folder,), daemon=True, name="liverec-done").start()
        self.say(f"liverec: finished {s.folder.name} ({reason}).")

    def _notify(self, folder):
        try:
            self.on_finished(folder)
        except Exception:  # noqa: BLE001
            pass

    def _write_summary(self, s: _Session, final: bool, reason: str):
        doc = summarise_dir(s.folder, ended_by=reason, final=final)
        try:
            fsutil.write_json(s.folder / SUMMARY, doc, default=str)
        except OSError as e:
            self.say(f"liverec: couldn't write {SUMMARY} ({type(e).__name__}).")

    def _recover(self):
        """Folders a crash or a power cut left without a final summary.json are finished now."""
        lib = self.library()
        if lib is None:
            return
        try:
            for d in unfinished(lib):
                doc = summarise_dir(d, ended_by="recovered after Debrief or the PC stopped suddenly", final=True)
                fsutil.write_json(d / SUMMARY, doc, default=str)
                self.recovered.append(d)
                self.say(f"liverec: finished the recording {d.name}, which was cut short.")
        except Exception:  # noqa: BLE001
            self.say("liverec: ERROR finishing earlier recordings:\n" + _tb())

    # ------------------------------------------------------------ status
    def _recording_text(self, s: _Session) -> str:
        from . import library as _lib
        sec = s.sec
        wi = (s.last_secs.get("WeekendInfo") or {})
        track = _lib.short_track_name(wi.get("TrackDisplayName") or wi.get("TrackName"))
        kind = (sec.type if sec else "") or "session"
        n = sec.laps_done if sec else 0
        return f"Recording {track} {kind.lower()}, {n} lap{'' if n == 1 else 's'}"

    def status_doc(self) -> dict:
        doc = {"state": self._state, "text": self._text, "paused": self.is_paused(),
               "perf": self.perf.summary(), "telemetry": self.tel_state}
        if self.cur is not None and self.cur.sec is not None:
            doc["session"] = {"folder": self.cur.folder.name, "num": self.cur.sec.num, "type": self.cur.sec.type,
                              "laps": self.cur.sec.laps_done}
            doc["disk_logging"] = self.cur.disk_logging
        return doc

    def status(self) -> dict:
        d = self.status_doc()
        d.update(running=self._thread is not None and self._thread.is_alive(), by=self.role)
        return d


def _tb() -> str:
    import traceback
    return traceback.format_exc()


def unfinished(library_folder) -> list:
    """Recording folders that have rows but no final summary."""
    root = recordings_root(library_folder)
    out = []
    try:
        dirs = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return out
    for d in dirs:
        if not ((d / CAR).exists() or (d / INFO).exists()):
            continue
        try:
            sm = json.loads((d / SUMMARY).read_text(encoding="utf-8"))
            if isinstance(sm, dict) and sm.get("final") is True:
                continue
        except (OSError, ValueError):
            pass
        out.append(d)
    return out


# ---------------------------------------------------------------- the summary, worked out from the files
def _median(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 3) if xs else None


def _stops_own(samples, laps_by_t):
    """Your own pit stops: from OnPitRoad going on and off in the once-a-second rows."""
    stops, cur = [], None
    prev = None
    for r in samples:
        on = r.get("pit")
        if on is None:
            continue
        if on and not prev:
            cur = {"lap": r.get("lap"), "in_t": r.get("t"), "fuel_in": r.get("fuel"), "tsu_in": r.get("tsu"),
                   "rep_in": r.get("rep"), "stall_s": 0.0, "_last": r.get("t")}
        if cur is not None:
            if r.get("ps") or r.get("pa"):
                dt = (r.get("t") or 0) - (cur["_last"] or 0)
                cur["stall_s"] += max(0.0, min(dt, 5.0))
            cur["_last"] = r.get("t")
            cur["out_t"] = r.get("t")
            cur["fuel_out"] = r.get("fuel")
            cur["tsu_out"] = r.get("tsu")
        if not on and prev and cur is not None:
            stops.append(cur)
            cur = None
        prev = on
    if cur is not None:
        stops.append(cur)
    out = []
    for c in stops:
        lane = round((c.get("out_t") or 0) - (c.get("in_t") or 0), 1)
        added = None
        if c.get("fuel_in") is not None and c.get("fuel_out") is not None:
            added = round(max(0.0, c["fuel_out"] - c["fuel_in"]), 1)
        out.append({"lap": c.get("lap"), "in_t": c.get("in_t"), "lane_s": lane, "stall_s": round(c["stall_s"], 1),
                    "fuel_added_l": added,
                    "tyres_changed": (c.get("tsu_out") is not None and c.get("tsu_in") is not None
                                      and c["tsu_out"] > c["tsu_in"]) or None,
                    "repair_s": c.get("rep_in") if c.get("rep_in") else None})
    return out


def _field_stops(rows, drivers, me_idx, green_t=None):
    """Every other car's pit stops, from CarIdxOnPitRoad going on and off (and CarIdxTrackSurface 1 = in its stall).
    2.35: a visit that began before the green flag (a pit-lane start, the garage) isn't a stop, and two visits of one car
    less than 30 s apart are one stop (a drive-through that touched the line twice)."""
    return _merge_stops(_field_stops_raw(rows, drivers, me_idx), green_t)


def _merge_stops(stops: dict, green_t=None) -> dict:
    out = {}
    for i, sp in stops.items():
        keep = []
        for x in sorted(sp, key=lambda x: x.get("in_t") or 0):
            if green_t is not None and (x.get("in_t") or 0) < green_t:
                continue
            if keep and (x["in_t"] - (keep[-1]["in_t"] + keep[-1]["lane_s"])) < 30.0:
                k = keep[-1]
                k["lane_s"] = round(x["in_t"] + x["lane_s"] - k["in_t"], 1)
                k["stall_s"] = round(k["stall_s"] + x["stall_s"], 1)
                k["stopped"] = k["stopped"] or x["stopped"]
                continue
            keep.append(dict(x))
        if keep:
            out[i] = keep
    return out


def _gaps(field_rows, lap_rows, me_idx, drivers):
    """2.35: at the end of each of your laps, the gap to the car ahead of you and behind you in your class
    (CarIdxF2Time: time behind the leader). [{lap, ahead, behind, ahead_name, behind_name}]; [] when nothing fits."""
    if me_idx is None or not field_rows:
        return []
    out, j = [], 0
    rows = [r for r in field_rows if r.get("t") is not None]
    for l in lap_rows:
        t = l.get("t")
        if t is None:
            continue
        while j < len(rows) and rows[j]["t"] < t:
            j += 1
        if j >= len(rows):
            break
        r = rows[j]
        f2, cp = r.get("f2") or [], r.get("cpos") or r.get("pos") or []
        if me_idx >= len(f2) or me_idx >= len(cp) or not cp[me_idx] or f2[me_idx] is None:
            continue
        mine, me_t = cp[me_idx], f2[me_idx]
        row = {"lap": l.get("lap")}
        for want, key, sign in ((mine - 1, "ahead", 1), (mine + 1, "behind", -1)):
            for i, c in enumerate(cp):
                if c == want and i != me_idx and i < len(f2) and f2[i] is not None and f2[i] >= 0:
                    d = drivers.get(i) or {}
                    if d.get("CarIsPaceCar"):
                        continue
                    g = (me_t - f2[i]) * sign
                    if 0 <= g < 600:
                        row[key] = round(g, 2)
                        row[key + "_name"] = d.get("UserName") or ""
                    break
        if "ahead" in row or "behind" in row:
            out.append(row)
    return out


def _field_stops_raw(rows, drivers, me_idx):
    state: dict = {}
    stops: dict = {}
    for r in rows:
        pit, surf, lap, t = r.get("pit"), r.get("surf"), r.get("lap"), r.get("t")
        if not pit or t is None:
            continue
        for i, on in enumerate(pit):
            d = drivers.get(i)
            if d is None or d.get("CarIsPaceCar") or d.get("IsSpectator"):
                continue
            st = state.get(i)
            if st is None:
                state[i] = {"on": bool(on), "first": True, "in_t": t if on else None, "lap": None, "stall": 0.0, "last": t}
                continue
            stall = bool(surf and i < len(surf) and surf[i] == 1)
            if on and not st["on"]:
                st.update(on=True, in_t=t, stall=0.0, last=t, first=False,
                          lap=((lap[i] if lap and i < len(lap) and lap[i] is not None and lap[i] >= 0 else 0) + 1))
            elif on and st["on"]:
                if stall:
                    st["stall"] += max(0.0, min(t - st["last"], 5.0))
                st["last"] = t
            elif not on and st["on"]:
                st["on"] = False
                if st["lap"] is not None and st["in_t"] is not None:
                    lane = t - st["in_t"]
                    if lane >= 5.0:
                        stops.setdefault(i, []).append({"lap": st["lap"], "in_t": round(st["in_t"], 1), "lane_s": round(lane, 1),
                                                        "stall_s": round(st["stall"], 1), "stopped": st["stall"] >= 1.0})
    return stops


def _section(num, car, field, si_sessions, drivers, me_idx, tl_end):
    rows = [r for r in car if r.get("sn") == num]
    opens = [r for r in rows if r.get("k") == "open"]
    closes = [r for r in rows if r.get("k") == "close"]
    samples = [r for r in rows if r.get("k") == "s"]
    laps = [r for r in rows if r.get("k") == "lap"]
    incs = [r for r in rows if r.get("k") == "inc"]
    sinfo = si_sessions.get(num) or {}
    typ = (opens[0].get("type") if opens else "") or sinfo.get("SessionType") or sinfo.get("SessionName") or ""
    wall = [r.get("w") for r in rows if r.get("w")]

    def iso(ts):
        return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds") if ts else None
    started = (opens[0].get("at") if opens else None) or iso(min(wall) if wall else None)
    ended = (closes[-1].get("at") if closes else None) or iso(max(wall) if wall else None)
    whole = [l for l in laps if not l.get("partial")]
    timed = [l for l in whole if l.get("time") and not l.get("pit")] or [l for l in whole if l.get("time")]
    best = min((l["time"] for l in timed), default=None)
    clean = [l for l in timed if best and l["time"] <= best * 1.07]
    typical = _median([l["time"] for l in clean])
    burns = [l for l in whole if l.get("fuel_used") is not None and l["fuel_used"] > 0.01 and not l.get("pit")]
    is_race = str(typ).strip().lower() == "race"
    green_t = next((r.get("t") for r in samples if r.get("st") == RACING and r.get("t") is not None), None) if is_race else None
    # stints: a new one begins after each of your own pit stops
    stops = [x for x in _stops_own(samples, None) if green_t is None or (x.get("in_t") or 0) >= green_t]
    cut = sorted({s["lap"] for s in stops if s.get("lap") is not None})
    stints, cur = [], []
    for l in whole:
        cur.append(l)
        if l.get("lap") in cut:
            stints.append(cur)
            cur = []
    if cur:
        stints.append(cur)
    stint_rows = []
    for k, st in enumerate(stints, 1):
        fl = [l["fuel_used"] for l in st if l.get("fuel_used") is not None and l["fuel_used"] > 0.01 and not l.get("pit")]
        if not st:
            continue
        stint_rows.append({"stint": k, "first_lap": st[0].get("lap"), "last_lap": st[-1].get("lap"), "laps": len(st),
                           "counted_laps": len(fl), "fuel_l": round(sum(fl), 2) if fl else None,
                           "per_lap_l": round(sum(fl) / len(fl), 3) if fl else None})
    # aids
    # 2.35: only what you drove with counts. Before the first whole lap (and before the green flag in a race) iRacing
    # reports 0 while the car loads, which read as "you changed brake bias from 0 to 50".
    aid_seen: dict = {}
    for l in whole:
        if (l.get("lap") or 0) >= 1:
            for k, v in (l.get("aids") or {}).items():
                aid_seen.setdefault(k, set()).add(v)
    first_t = next((l.get("t") for l in whole if (l.get("lap") or 0) >= 1 and l.get("t") is not None), None)
    for r in samples:
        if (r.get("lap") or 0) < 1 or (green_t is not None and (r.get("t") or 0) < green_t) or (
                first_t is not None and (r.get("t") or 0) < first_t - 600):
            continue
        for k in ("bb", "abs", "tc"):
            if r.get(k) is not None:
                aid_seen.setdefault(k, set()).add(r[k])
    changes = []
    for l in whole:
        if (l.get("lap") or 0) < 1:
            continue
        for c in l.get("chg") or []:
            if c.get("from") in (None, 0) and l is next((x for x in whole if (x.get("lap") or 0) >= 1), None):
                continue
            changes.append(dict(c, lap=l.get("lap")))
    by_lap: dict = {}
    for e in incs:
        by_lap[e.get("lap")] = by_lap.get(e.get("lap"), 0) + int(e.get("n") or 0)
    last = samples[-1] if samples else {}
    out = {"num": num, "type": typ, "name": sinfo.get("SessionName") or "", "started": started, "ended": ended,
           "laps": len(whole), "lap_rows": len(laps), "lap_times": [{"lap": l.get("lap"), "time": l.get("time"), "pit": bool(l.get("pit"))} for l in whole],
           "best": best, "typical": typical,
           "fuel": {"per_lap_median_l": _median([l["fuel_used"] for l in burns]), "laps_counted": len(burns),
                    "stints": stint_rows, "start_l": next((r.get("fuel") for r in samples if r.get("fuel") is not None), None),
                    "per_lap": [{"lap": l.get("lap"), "fuel_used_l": l.get("fuel_used"), "pit": bool(l.get("pit"))} for l in whole
                                if l.get("fuel_used") is not None]},
           "aids": {"seen": {k: sorted(v) for k, v in aid_seen.items()},
                    "per_lap": [{"lap": l.get("lap"), **(l.get("aids") or {})} for l in whole if l.get("aids") and (l.get("lap") or 0) >= 1],
                    "changes": changes},
           "incidents": {"total": last.get("ti") if last.get("ti") is not None else sum(by_lap.values()),
                         "team_total": last.get("tti"),
                         "by_lap": [{"lap": k, "gained": v} for k, v in sorted(by_lap.items(), key=lambda kv: (kv[0] is None, kv[0]))],
                         "events": [{"lap": e.get("lap"), "t": e.get("t"), "d": e.get("d"), "gained": e.get("n"), "total": e.get("total")} for e in incs]},
           "pit_stops": stops, "tyres": {"sets_used": last.get("tsu"), "sets_available": last.get("tsa"),
                                         "wear_end": last.get("w4")},
           "weather": {"track_c": last.get("tt"), "air_c": last.get("at"), "wetness": last.get("wet")}}
    # race extras
    if is_race:
        fr = [r for r in field if r.get("sn") == num]
        pos_laps = [{"lap": l.get("lap"), "pos": l.get("pos"), "cpos": l.get("cpos")} for l in whole if l.get("pos")]
        fin = None
        for rp in sinfo.get("ResultsPositions") or []:
            if rp.get("CarIdx") == me_idx:
                fin = {"pos": rp.get("Position"), "cpos": (rp.get("ClassPosition") or 0) + 1 if rp.get("ClassPosition") is not None else None,
                       "laps": rp.get("LapsComplete"), "incidents": rp.get("Incidents")}
        if fin is None and pos_laps:
            fin = {"pos": pos_laps[-1]["pos"], "cpos": pos_laps[-1]["cpos"], "from": "last lap row"}
        stp = _field_stops(fr, drivers, me_idx, green_t)
        rivals = []
        for i, d in sorted(drivers.items()):
            if d.get("CarIsPaceCar") or d.get("IsSpectator"):
                continue
            rivals.append({"car_idx": i, "number": str(d.get("CarNumber") or ""), "name": d.get("UserName") or "",
                           "team": d.get("TeamName") or "", "class": d.get("CarClassShortName") or "",
                           "is_me": i == me_idx, "stops": stp.get(i, [])})
        out["race"] = {"positions": pos_laps, "finish": fin, "rivals": rivals, "field_rows": len(fr),
                       "gaps": _gaps(fr, [l for l in whole if (l.get("lap") or 0) >= 1], me_idx, drivers),
                       "green_t": green_t}
    return out


def summarise_dir(folder, ended_by: str = "", final: bool = True) -> dict:
    """The whole picture of one recording folder, from its files only (so it is the same live as after a crash)."""
    folder = Path(folder)
    info = merged_info(folder)
    wi, di, si = info.get("WeekendInfo") or {}, info.get("DriverInfo") or {}, info.get("SessionInfo") or {}
    me_idx = di.get("DriverCarIdx")
    drivers = {d.get("CarIdx"): d for d in di.get("Drivers") or [] if isinstance(d, dict)}
    me = drivers.get(me_idx) or {}
    car = list(read_jsonl(folder / CAR))
    field = list(expand_field(read_jsonl(folder / FIELD)))
    sessions = {s.get("SessionNum"): s for s in si.get("Sessions") or [] if isinstance(s, dict)}
    nums = sorted({r["sn"] for r in car if r.get("sn") is not None})
    try:
        hdr = json.loads((folder / INFO).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        hdr = {}
    secs = [_section(n, car, field, sessions, drivers, me_idx, None) for n in nums]
    dle = [r.get("dle") for r in car if r.get("dle") is not None]
    disk = bool(dle[-1]) if dle else None
    track_name = wi.get("TrackDisplayName") or wi.get("TrackName") or ""
    sub = wi.get("SubSessionID") or hdr.get("subsession")
    started = hdr.get("started") or next((s["started"] for s in secs if s.get("started")), None)
    ended = max((s["ended"] for s in secs if s.get("ended")), default=None)
    ai = [d for i, d in drivers.items() if d.get("CarIsAI") and not d.get("CarIsPaceCar") and i != me_idx]
    return {"version": VERSION, "sv": SUMMARY_V, "app": _version(), "final": bool(final), "complete": bool(final), "ended_by": ended_by,
            "folder": folder.name, "subsession": int(sub) if sub else None,
            "track": {"name": track_name, "config": wi.get("TrackConfigName") or "", "code": wi.get("TrackName") or "",
                      "id": wi.get("TrackID"), "length": wi.get("TrackLength")},
            "car": {"name": me.get("CarScreenName") or "", "path": me.get("CarPath") or "", "id": me.get("CarID"),
                    "class": me.get("CarClassShortName") or ""},
            "driver": me.get("UserName") or "", "team": me.get("TeamName") or "",
            "fuel_tank_l": di.get("DriverCarFuelMaxLtr"), "fuel_max_pct": di.get("DriverCarMaxFuelPct"),
            "started": started, "ended": ended,
            "disk_logging": disk,
            "disk_logging_note": ("" if disk is not False else
                                  "iRacing's own telemetry files (.ibt) were switched off during this session, so no "
                                  "telemetry file was saved. Turn on Settings > Live data > Make iRacing save my telemetry "
                                  "and Debrief switches it on for you."),
            "sessions": secs, "ai_cars": len(ai), "official": bool(sub)}
