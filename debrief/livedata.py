"""The one place Debrief reads iRacing live (2.27 "Live data"). The coach and the background recorder both use it.

pyirsdk (imported only here, and only on the PC) gives three kinds of data, read at three different speeds:

  own car       about 20 times a second: what the coach already read (speed, pedals, lap, incidents ...) plus fuel,
                driver aids, pit service, tyres, repairs, team incident counts, session clock, flags and weather.
  the field     about 4 times a second: the CarIdx arrays (one slot per car in the session: lap, position, pit road,
                gap to the leader ...). These exist only while iRacing is running; Garage 61 never has them.
  session info  iRacing's YAML text (track, car, every driver, session list). It is parsed on its own low-priority
                thread, and only when iRacing says it changed (the SDK's session-info update counter), so it can never
                make the 20 Hz reading wait. pyirsdk parses with PyYAML's C loader when it is installed.

A channel a car does not have (some cars have no traction control, for one) is found out once, remembered, and then
skipped without a sound: reading a name that doesn't exist makes pyirsdk search the whole session text every time,
which is exactly the kind of waste this module exists to avoid.

    r = LiveReader(own=COACH_CHANNELS + OWN_EXTRA, field=FIELD_CHANNELS)
    s = r.read_own()           # dict, or None while iRacing isn't running (reconnects by itself)
    f = r.read_field()         # {"CarIdxPosition": [...], ...}
    v, sections = r.info_sections()     # (version number, {"WeekendInfo": ..., "DriverInfo": ..., "SessionInfo": ...})

Nothing here talks to the network. The Garage 61 token is never in this module.
"""
from __future__ import annotations

import os
import threading
import time

# What the coach reads (each live read copies these out of iRacing's buffer).
COACH_CHANNELS = ("SessionTime", "SessionNum", "Lap", "LapDistPct", "LapLastLapTime", "Speed", "Brake", "Throttle",
                  "Gear", "OnPitRoad", "PlayerTrackSurface", "CarLeftRight", "IsOnTrack", "PlayerCarMyIncidentCount",
                  "SteeringWheelAngle")

# What the recorder adds for your own car. Whatever the car or session doesn't have is skipped.
OWN_EXTRA = ("FuelLevel", "FuelLevelPct", "FuelUsePerHour", "dcBrakeBias", "dcABS", "dcTractionControl",
             "PitSvFlags", "PitSvFuel", "PlayerCarPitSvStatus", "PitstopActive", "PlayerCarInPitStall",
             "PitRepairLeft", "PitOptRepairLeft", "FastRepairAvailable", "FastRepairUsed",
             "TireSetsUsed", "TireSetsAvailable", "LFwearM", "RFwearM", "LRwearM", "RRwearM",
             "PlayerCarTeamIncidentCount", "PlayerCarDriverIncidentCount", "DCDriversSoFar", "DCLapStatus",
             "SessionTimeRemain", "SessionLapsRemainEx", "SessionFlags", "SessionState",
             "TrackTempCrew", "AirTemp", "TrackWetness",
             # not in the brief, read for the same reasons: whether iRacing is writing its own .ibt files (read only),
             # and your position without waiting for the field arrays
             "IsDiskLoggingEnabled", "PlayerCarPosition", "PlayerCarClassPosition")

# One slot per car in the session (about 64). Read about 4 times a second.
FIELD_CHANNELS = ("CarIdxLapCompleted", "CarIdxLapDistPct", "CarIdxLastLapTime", "CarIdxBestLapTime",
                  "CarIdxPosition", "CarIdxClassPosition", "CarIdxF2Time", "CarIdxEstTime", "CarIdxOnPitRoad",
                  "CarIdxTrackSurface", "CarIdxTireCompound", "CarIdxFastRepairsUsed")

INFO_KEYS = ("WeekendInfo", "DriverInfo", "SessionInfo")
ENGINEER_INFO_KEYS = INFO_KEYS + ("SplitTimeInfo",)      # 2.31: the sector boundaries, for the Race Engineer
# 2.31 Race Engineer: what the practice / qualifying page reads besides the crew chief's channels
ENGINEER_OWN = ("IsInGarage", "IsReplayPlaying", "LapBestLapTime", "LapCurrentLapTime", "PlayerCarTowTime",
                "PlayerCarMyIncidentCount", "AirTemp", "TrackTempCrew", "TrackWetness", "PlayerCarPosition",
                "PlayerCarClassPosition", "SessionUniqueID", "FuelLevel", "LapDist",
                "LFtempCL", "LFtempCM", "LFtempCR", "RFtempCL", "RFtempCM", "RFtempCR",
                "LRtempCL", "LRtempCM", "LRtempCR", "RRtempCL", "RRtempCM", "RRtempCR",
                "LFwearL", "LFwearM", "LFwearR", "RFwearL", "RFwearM", "RFwearR",
                "LRwearL", "LRwearM", "LRwearR", "RRwearL", "RRwearM", "RRwearR")
REPROBE_S = 30.0          # a channel that read as None is tried again this often (only when the SDK can't list them)


def _lower_thread_priority() -> None:
    """Windows: the session-info thread runs below normal priority. Anywhere else (or on any error) nothing happens."""
    if os.name != "nt":
        return
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.SetThreadPriority(k32.GetCurrentThread(), -1)       # THREAD_PRIORITY_BELOW_NORMAL
    except Exception:  # noqa: BLE001
        pass


class LiveReader:
    """iRacing through pyirsdk. read_own() is None while iRacing isn't running (and a reconnect is tried every
    retry_s seconds); state is 'not_installed', 'not_running' or 'connected'."""

    def __init__(self, rate_hz: float = 20.0, retry_s: float = 2.0, own=COACH_CHANNELS, field=(),
                 info_poll_s: float = 0.5, info_fallback_s: float = 5.0, info_keys=INFO_KEYS):
        self.interval = 1.0 / max(1.0, rate_hz)
        self.info_keys = tuple(info_keys)
        self.realtime = True
        self.done = False
        self.state = "not_running"
        self.error = ""
        self.own = tuple(own)
        self.field = tuple(field)
        self._ir = None
        self._retry_s = retry_s
        self._next_try = 0.0
        self._listed: set | None = None       # every variable iRacing offers (when the SDK can tell us)
        self._missing: set = set()
        self._reprobe_at = 0.0
        self._lock = threading.Lock()
        self._sections: dict = {}
        self._version = 0
        self._info_poll_s, self._info_fallback_s = info_poll_s, info_fallback_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._gen = 0                         # one more with every new connection: an old thread sees it and ends

    # ------------------------------------------------------------ connection
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
            self._new_connection()
        return ok

    def _new_connection(self):
        """A fresh link to iRacing: forget what the last one knew, and start reading session info on its own thread."""
        with self._lock:
            self._sections, self._version = {}, self._version + 1
        self._missing = set()
        self._reprobe_at = time.monotonic() + REPROBE_S
        self._listed = None
        try:
            names = getattr(self._ir, "var_headers_names", None)
            if names:
                self._listed = set(names)
        except Exception:  # noqa: BLE001
            self._listed = None
        self._stop_thread()
        self._gen += 1
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._info_loop, args=(self._gen, self._stop), daemon=True,
                                        name="livedata-info")
        self._thread.start()

    def _stop_thread(self):
        self._stop.set()
        t = self._thread
        self._thread = None
        if t is not None and t is not threading.current_thread():
            t.join(timeout=1.0)

    # ------------------------------------------------------------ plain reads
    def _get(self, key):
        try:
            return self._ir[key]
        except Exception:  # noqa: BLE001
            return None

    def _names(self, wanted, keep_none: bool) -> dict:
        out = {}
        listed, missing = self._listed, self._missing
        now = time.monotonic()
        if listed is None and missing and now >= self._reprobe_at:
            missing.clear()                     # can't list them: look again for the ones that read as None
            self._reprobe_at = now + REPROBE_S
        for nm in wanted:
            if nm in missing or (listed is not None and nm not in listed):
                if keep_none:
                    out[nm] = None
                continue
            v = self._get(nm)
            if v is None:
                missing.add(nm)
                if keep_none:
                    out[nm] = None
                continue
            out[nm] = v
        return out

    def _frozen(self, wanted, keep_none: bool):
        if not self._connect():
            return None
        ir = self._ir
        try:
            ir.freeze_var_buffer_latest()
            try:
                return self._names(wanted, keep_none)
            finally:
                ir.unfreeze_var_buffer_latest()
        except Exception as e:  # noqa: BLE001
            self.state, self.error = "not_running", str(e)[:120]
            return None

    def read_own(self, keep_none: bool = True) -> dict | None:
        """Your car's channels. keep_none: a channel that doesn't exist is in the dict as None (the coach's old
        behaviour); False leaves it out (the recorder). None while iRacing isn't running."""
        s = self._frozen(self.own, keep_none)
        if s is None or s.get("SessionTime") is None:
            return None
        return s

    def read_field(self) -> dict | None:
        """The CarIdx arrays that exist, as lists. None while iRacing isn't running."""
        return self._frozen(self.field, False)

    def missing(self) -> list:
        """Channels found to be absent so far (for the log and the tests)."""
        return sorted(self._missing | ({n for n in self.own + self.field if n not in self._listed}
                                       if self._listed is not None and self.state == "connected" else set()))

    # ------------------------------------------------------------ session info
    def load_info_now(self) -> bool:
        """Read the session info right now on this thread (the coach's very first read, so its track and car are known
        from the first sample). Everything after that comes from the info thread."""
        return self._load_info(None)

    def _load_info(self, counter) -> bool:
        if self._ir is None:
            return False
        got = {k: self._get(k) for k in self.info_keys}
        if not any(got.values()):
            return False
        with self._lock:
            self._sections = {k: v for k, v in got.items() if v}
            self._version += 1
        self._last_counter = counter
        return True

    _last_counter = None

    def info_sections(self) -> tuple:
        """(version, sections). The version goes up each time the session info was read again."""
        with self._lock:
            return self._version, dict(self._sections)

    def _info_loop(self, gen: int, stop: threading.Event):
        _lower_thread_priority()
        last_t = 0.0
        last_c = None
        while not stop.wait(self._info_poll_s):
            if gen != self._gen:
                return
            ir = self._ir
            if ir is None:
                continue
            try:
                counter = ir.session_info_update      # cheap: one integer in iRacing's header
            except Exception:  # noqa: BLE001
                counter = None
            now = time.monotonic()
            if counter is not None:
                if counter == last_c:
                    continue
            elif now - last_t < self._info_fallback_s:     # an SDK without the counter: look every few seconds
                continue
            last_t = now
            try:
                if self._load_info(counter):
                    last_c = counter
            except Exception:  # noqa: BLE001  (a bad read here must never stop the thread)
                pass

    def close(self):
        self._stop_thread()
        try:
            if self._ir is not None:
                self._ir.shutdown()
        except Exception:  # noqa: BLE001
            pass
