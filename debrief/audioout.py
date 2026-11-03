"""Sound out of a chosen device for the live coach (Coach 2.0): the headset he uses in iRacing, not just Windows'
default. No extra package: Windows' own waveOut interface (winmm.dll) through ctypes, loaded only when used.

    p = Player(WaveOutBackend(), device="Headset Earphone (Arctis 7)")   # "" = Windows default
    p.play(pcm16_bytes, 16000, priority=3)       # returns at once; one playback thread plays it
    p.cancel()                                   # stop now (a car alongside) and forget what waits
    p.measure_latency()                          # seconds from "play" to the device starting it (0..0.5)
    p.status(); p.close()

Rules:
  * One playback thread owns the device (every waveOut call happens there). The device stays open while the
    player lives, so a cue never waits for a device to open (that is also when Bluetooth headsets clip the start).
  * One sound at a time. A new sound with a higher priority than the one playing (or interrupt=True) stops it at
    once (waveOutReset) and plays; an equal or lower one waits its turn, but never longer than max_wait_s after it
    was asked for (then it is dropped: a late cue is worse than none). At most MAX_PENDING wait.
  * The chosen device is found by its name (Windows renumbers devices when USB things are plugged in). When it is
    missing or stops working, the sound goes to Windows' default device instead and status()["note"] says so; the
    chosen device is looked for again at most every RECHECK_S and used again as soon as it is back.
  * Never raises out of its thread; a failure is logged once and the next sound is tried again.

The backend interface (WaveOutBackend on Windows, FakeBackend in tests):
    devices() -> [{"id": int, "name": str}]           open(device_id, rate) -> handle (raises AudioError)
    write(handle, pcm) -> buf                         done(buf) -> bool
    wait(handle, timeout_s)                           position(handle) -> samples played, or None
    reset(handle)                                     release(handle, buf)          close(handle)
"""
from __future__ import annotations

import logging
import statistics
import sys
import threading
import time

_LOG = logging.getLogger("debrief.audioout")

WAVE_MAPPER = -1            # Windows' default output device
DEFAULT_NAME = "Windows default"
MAX_PENDING = 3
MAX_WAIT_S = 1.0            # a sound waiting longer than this for its turn is dropped
RECHECK_S = 5.0             # a missing chosen device is looked for again at most this often
LATENCY_MAX_S = 0.5
NAME_LEN = 31               # WAVEOUTCAPSW.szPname is 32 WCHARs including the terminating zero

# winmm results
MMSYSERR_NOERROR = 0
MMSYSERR_ERROR = 1
MMSYSERR_BADDEVICEID = 2
MMSYSERR_ALLOCATED = 4
MMSYSERR_INVALHANDLE = 5
MMSYSERR_NODRIVER = 6
MMSYSERR_NOMEM = 7
WAVERR_BADFORMAT = 32
LOST = {MMSYSERR_ERROR, MMSYSERR_BADDEVICEID, MMSYSERR_INVALHANDLE, MMSYSERR_NODRIVER}


class AudioError(Exception):
    def __init__(self, msg: str, code: int | None = None):
        super().__init__(msg)
        self.code = code

    @property
    def lost(self) -> bool:
        """The device itself is gone or broken (unplugged, driver gone), as opposed to a passing problem."""
        return self.code in LOST


def clean_name(name) -> str:
    """A device name as saved in the settings: plain text, at most NAME_LEN characters ("" = Windows default)."""
    s = "".join(ch for ch in str(name or "") if ch.isprintable()).strip()
    return "" if s.lower() == DEFAULT_NAME.lower() else s[:NAME_LEN]


# ---------------------------------------------------------------- Windows: waveOut through ctypes
class WaveOutBackend:
    """winmm's waveOut* functions. 16-bit mono PCM only (what the voice packs and Windows speech give)."""
    name = "waveout"
    CALLBACK_EVENT = 0x00050000
    WHDR_DONE = 0x00000001
    TIME_SAMPLES = 0x0002

    def __init__(self):
        if sys.platform != "win32":
            raise AudioError("sound devices can only be chosen on Windows")
        import ctypes
        from ctypes import wintypes
        self._ct = ctypes
        self._wt = wintypes
        self._winmm = ctypes.WinDLL("winmm")
        self._k32 = ctypes.WinDLL("kernel32")

        class WAVEFORMATEX(ctypes.Structure):
            _fields_ = [("wFormatTag", wintypes.WORD), ("nChannels", wintypes.WORD),
                        ("nSamplesPerSec", wintypes.DWORD), ("nAvgBytesPerSec", wintypes.DWORD),
                        ("nBlockAlign", wintypes.WORD), ("wBitsPerSample", wintypes.WORD), ("cbSize", wintypes.WORD)]

        class WAVEHDR(ctypes.Structure):
            pass
        WAVEHDR._fields_ = [("lpData", ctypes.c_void_p), ("dwBufferLength", wintypes.DWORD),
                            ("dwBytesRecorded", wintypes.DWORD), ("dwUser", ctypes.c_size_t),
                            ("dwFlags", wintypes.DWORD), ("dwLoops", wintypes.DWORD),
                            ("lpNext", ctypes.POINTER(WAVEHDR)), ("reserved", ctypes.c_size_t)]

        class WAVEOUTCAPSW(ctypes.Structure):
            _fields_ = [("wMid", wintypes.WORD), ("wPid", wintypes.WORD), ("vDriverVersion", wintypes.UINT),
                        ("szPname", wintypes.WCHAR * 32), ("dwFormats", wintypes.DWORD),
                        ("wChannels", wintypes.WORD), ("wReserved1", wintypes.WORD), ("dwSupport", wintypes.DWORD)]

        class MMTIME(ctypes.Structure):
            _fields_ = [("wType", wintypes.UINT), ("u", wintypes.DWORD), ("pad", wintypes.DWORD)]

        self.WAVEFORMATEX, self.WAVEHDR, self.WAVEOUTCAPSW, self.MMTIME = WAVEFORMATEX, WAVEHDR, WAVEOUTCAPSW, MMTIME
        HWAVEOUT = wintypes.HANDLE
        w = self._winmm
        w.waveOutGetNumDevs.restype = wintypes.UINT
        w.waveOutGetNumDevs.argtypes = []
        w.waveOutGetDevCapsW.restype = wintypes.UINT
        w.waveOutGetDevCapsW.argtypes = [ctypes.c_size_t, ctypes.POINTER(WAVEOUTCAPSW), wintypes.UINT]
        w.waveOutOpen.restype = wintypes.UINT
        w.waveOutOpen.argtypes = [ctypes.POINTER(HWAVEOUT), wintypes.UINT, ctypes.POINTER(WAVEFORMATEX),
                                  ctypes.c_size_t, ctypes.c_size_t, wintypes.DWORD]
        for fn in ("waveOutPrepareHeader", "waveOutUnprepareHeader", "waveOutWrite"):
            f = getattr(w, fn)
            f.restype = wintypes.UINT
            f.argtypes = [HWAVEOUT, ctypes.POINTER(WAVEHDR), wintypes.UINT]
        for fn in ("waveOutReset", "waveOutClose"):
            f = getattr(w, fn)
            f.restype = wintypes.UINT
            f.argtypes = [HWAVEOUT]
        w.waveOutGetPosition.restype = wintypes.UINT
        w.waveOutGetPosition.argtypes = [HWAVEOUT, ctypes.POINTER(MMTIME), wintypes.UINT]
        k = self._k32
        k.CreateEventW.restype = wintypes.HANDLE
        k.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        k.WaitForSingleObject.restype = wintypes.DWORD
        k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k.CloseHandle.restype = wintypes.BOOL
        k.CloseHandle.argtypes = [wintypes.HANDLE]

    def devices(self) -> list:
        out = []
        n = int(self._winmm.waveOutGetNumDevs())
        for i in range(min(n, 64)):
            caps = self.WAVEOUTCAPSW()
            if self._winmm.waveOutGetDevCapsW(i, self._ct.byref(caps), self._ct.sizeof(caps)) == 0:
                out.append({"id": i, "name": clean_name(caps.szPname) or f"Device {i + 1}"})
        return out

    def open(self, device_id: int, rate: int):
        ct = self._ct
        fmt = self.WAVEFORMATEX(1, 1, int(rate), int(rate) * 2, 2, 16, 0)
        ev = self._k32.CreateEventW(None, False, False, None)
        if not ev:
            raise AudioError("no event for the sound device")
        h = self._wt.HANDLE()
        dev = 0xFFFFFFFF if device_id == WAVE_MAPPER else int(device_id)
        r = self._winmm.waveOutOpen(ct.byref(h), dev, ct.byref(fmt), ev, 0, self.CALLBACK_EVENT)
        if r != 0:
            self._k32.CloseHandle(ev)
            raise AudioError(f"the sound device could not be opened (winmm {r})", r)
        return {"h": h, "ev": ev, "rate": int(rate)}

    def write(self, handle, pcm: bytes):
        ct = self._ct
        data = ct.create_string_buffer(bytes(pcm), len(pcm))
        hdr = self.WAVEHDR()
        hdr.lpData = ct.cast(data, ct.c_void_p)
        hdr.dwBufferLength = len(pcm)
        hdr.dwFlags = 0
        r = self._winmm.waveOutPrepareHeader(handle["h"], ct.byref(hdr), ct.sizeof(hdr))
        if r != 0:
            raise AudioError(f"the sound could not be prepared (winmm {r})", r)
        r = self._winmm.waveOutWrite(handle["h"], ct.byref(hdr), ct.sizeof(hdr))
        if r != 0:
            self._winmm.waveOutUnprepareHeader(handle["h"], ct.byref(hdr), ct.sizeof(hdr))
            raise AudioError(f"the sound could not be played (winmm {r})", r)
        return {"hdr": hdr, "data": data}            # both kept alive until release()

    def done(self, buf) -> bool:
        return bool(buf["hdr"].dwFlags & self.WHDR_DONE)

    def wait(self, handle, timeout_s: float):
        self._k32.WaitForSingleObject(handle["ev"], max(1, int(timeout_s * 1000)))

    def position(self, handle):
        t = self.MMTIME()
        t.wType = self.TIME_SAMPLES
        if self._winmm.waveOutGetPosition(handle["h"], self._ct.byref(t), self._ct.sizeof(t)) != 0:
            return None
        return int(t.u) if t.wType == self.TIME_SAMPLES else None

    def reset(self, handle):
        self._winmm.waveOutReset(handle["h"])

    def release(self, handle, buf):
        hdr = buf["hdr"]
        for _ in range(200):                      # unprepare refuses while the buffer is still queued
            r = self._winmm.waveOutUnprepareHeader(handle["h"], self._ct.byref(hdr), self._ct.sizeof(hdr))
            if r != 33:                           # WAVERR_STILLPLAYING
                return
            self._winmm.waveOutReset(handle["h"])
            time.sleep(0.005)

    def close(self, handle):
        try:
            self._winmm.waveOutReset(handle["h"])
            self._winmm.waveOutClose(handle["h"])
        finally:
            self._k32.CloseHandle(handle["ev"])


def default_backend():
    """WaveOutBackend on Windows, else None (no sound device can be chosen)."""
    try:
        return WaveOutBackend()
    except Exception:  # noqa: BLE001
        return None


def list_devices(backend=None) -> list:
    """[{"id", "name"}] of the output devices, never raises ([] off Windows)."""
    be = backend if backend is not None else default_backend()
    if be is None:
        return []
    try:
        return be.devices()
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------- the player
class _Item:
    __slots__ = ("pcm", "rate", "priority", "asked", "on_start", "on_end", "tag", "kind", "result")

    def __init__(self, pcm, rate, priority, asked, on_start, on_end, tag, kind="play"):
        self.pcm, self.rate, self.priority, self.asked = pcm, int(rate), int(priority), asked
        self.on_start, self.on_end, self.tag, self.kind = on_start, on_end, tag, kind
        self.result = None


class Player:
    def __init__(self, backend, device: str = "", log=None, clock=time.monotonic, max_wait_s: float = MAX_WAIT_S):
        self.backend = backend
        self.device = clean_name(device)
        self._log = log or _LOG.warning
        self._clock = clock
        self.max_wait_s = float(max_wait_s)
        self._cv = threading.Condition()
        self._pending: list = []
        self._current: _Item | None = None
        self._interrupt = False
        self._stop = False
        self._handle = None
        self._open_id = None            # device id the handle is open on
        self._open_rate = None
        self._using_default = False     # the chosen device is missing: playing through the default
        self._next_check = 0.0
        self.note = ""                  # plain words for the window ("Your headset ... isn't connected ...")
        self.played = 0
        self.dropped = 0
        self.errors = 0
        self._logged = set()
        self._thread = threading.Thread(target=self._run, name="debrief-audio", daemon=True)
        self._thread.start()

    # ---- any thread ----
    def play(self, pcm: bytes, rate: int, priority: int = 0, interrupt: bool = False, on_start=None, on_end=None,
             tag=None) -> bool:
        if not pcm:
            return False
        item = _Item(bytes(pcm)[:len(pcm) - len(pcm) % 2], rate, priority, self._clock(), on_start, on_end, tag)
        with self._cv:
            if self._stop:
                return False
            cur = self._current
            if cur is not None and (interrupt or priority > cur.priority):
                self._interrupt = True
                self._pending.insert(0, item)
            else:
                self._pending.append(item)
                self._pending.sort(key=lambda x: -x.priority)   # stable: equal priorities keep their order
            while len(self._pending) > MAX_PENDING:
                self._drop(self._pending.pop(), "too many waiting")
            self._cv.notify_all()
        return True

    def cancel(self):
        """Stop what is playing now and forget everything waiting."""
        with self._cv:
            for it in self._pending:
                self._drop(it, "cancelled")
            self._pending = []
            if self._current is not None:
                self._interrupt = True
            self._cv.notify_all()

    def set_device(self, name: str):
        name = clean_name(name)
        with self._cv:
            if name == self.device:
                return
            self.device = name
            self._next_check = 0.0
            self._pending.append(_Item(b"", 0, -1, self._clock(), None, None, None, kind="reopen"))
            self._cv.notify_all()

    def busy(self) -> bool:
        with self._cv:
            return self._current is not None or bool(self._pending)

    def measure_latency(self, tries: int = 3, rate: int = 16000, timeout: float = 6.0):
        """Seconds from handing a sound to the device to the device starting to play it (median of tries), from
        the device's own position report; where a device doesn't report a position, from when it says the sound has
        finished, less the sound's length. None when it can't be measured. Bounded 0..LATENCY_MAX_S. Blocks."""
        res = []
        for _ in range(max(1, tries)):
            done = threading.Event()
            box = {}
            item = _Item(b"\0\0" * int(rate * 0.25), rate, 99, self._clock(), None,
                         lambda it, ok, box=box, done=done: (box.update(r=it.result), done.set()), None,
                         kind="measure")
            with self._cv:
                if self._stop:
                    return None
                self._pending.insert(0, item)
                if self._current is not None:
                    self._interrupt = True
                self._cv.notify_all()
            if not done.wait(timeout) or box.get("r") is None:
                continue
            res.append(box["r"])
        if not res:
            return None
        return round(min(LATENCY_MAX_S, max(0.0, statistics.median(res))), 3)

    def status(self) -> dict:
        with self._cv:
            return {"device": self.device or DEFAULT_NAME, "using_default": self._using_default or not self.device,
                    "note": self.note, "playing": self._current is not None, "played": self.played,
                    "dropped": self.dropped, "errors": self.errors, "backend": getattr(self.backend, "name", "?")}

    def close(self, timeout: float = 2.0) -> bool:
        with self._cv:
            self._stop = True
            for it in self._pending:
                self._drop(it, "closing")
            self._pending = []
            self._interrupt = True
            self._cv.notify_all()
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout)
        return not self._thread.is_alive()

    # ---- the playback thread ----
    def _drop(self, item, why):
        if item.kind == "play":
            self.dropped += 1
        self._end(item, False, why)

    def _end(self, item, ok, why=None):
        if item.on_end is not None:
            try:
                item.on_end(item, ok if why is None else False)
            except Exception:  # noqa: BLE001
                pass

    def _note(self, key, msg):
        if key not in self._logged:
            self._logged.add(key)
            try:
                self._log(f"audio: {msg}")
            except Exception:  # noqa: BLE001
                pass

    def _resolve(self):
        """(device id, using default) for the chosen device name."""
        if not self.device:
            return WAVE_MAPPER, False
        try:
            devs = self.backend.devices()
        except Exception as e:  # noqa: BLE001
            self._note("devices", f"the device list could not be read ({e.__class__.__name__})")
            devs = []
        for d in devs:
            if d.get("name") == self.device:
                return int(d["id"]), False
        low = self.device.lower()
        for d in devs:                                    # Windows sometimes shortens or re-cases a name
            n = str(d.get("name") or "").lower()
            if n and (n.startswith(low) or low.startswith(n)):
                return int(d["id"]), False
        return WAVE_MAPPER, True

    def _close_dev(self):
        if self._handle is not None:
            try:
                self.backend.close(self._handle)
            except Exception:  # noqa: BLE001
                pass
        self._handle, self._open_id, self._open_rate = None, None, None

    def _ensure(self, rate: int, force_default: bool = False):
        now = self._clock()
        want_id, missing = (WAVE_MAPPER, bool(self.device)) if force_default else (None, None)
        if want_id is None:
            if self._handle is not None and self._open_rate == rate and \
                    (not self._using_default or now < self._next_check):
                return self._handle
            want_id, missing = self._resolve()
            if missing:
                self._next_check = now + RECHECK_S
        if self._handle is not None and self._open_id == want_id and self._open_rate == rate:
            self._set_default(missing)
            return self._handle
        self._close_dev()
        try:
            self._handle = self.backend.open(want_id, rate)
        except AudioError as e:
            if want_id == WAVE_MAPPER:
                raise
            self._note(f"open:{self.device}", f"{self.device} could not be opened ({e}); using Windows default")
            self._next_check = now + RECHECK_S
            self._handle = self.backend.open(WAVE_MAPPER, rate)
            want_id, missing = WAVE_MAPPER, True
        self._open_id, self._open_rate = want_id, rate
        self._set_default(missing)
        return self._handle

    def _set_default(self, missing: bool):
        self._using_default = bool(missing)
        self.note = (f"{self.device} isn't connected, so the coach plays through Windows default."
                     if missing and self.device else "")

    def _next(self):
        with self._cv:
            while not self._stop and not self._pending:
                self._cv.wait(0.5)
            if self._stop:
                return None
            item = self._pending.pop(0)
            self._interrupt = False
            self._current = item
            return item

    def _run(self):
        try:
            while True:
                item = self._next()
                if item is None:
                    break
                try:
                    if item.kind == "reopen":
                        if self._handle is not None:
                            self._close_dev()
                        continue
                    if item.kind == "play" and self._clock() - item.asked > self.max_wait_s:
                        self._drop(item, "waited too long")
                        continue
                    self._play(item)
                except Exception as e:  # noqa: BLE001
                    self.errors += 1
                    self._note(f"err:{e.__class__.__name__}", f"playing failed ({e.__class__.__name__}: {str(e)[:120]})")
                    self._end(item, False, "error")
                finally:
                    with self._cv:
                        self._current = None
        finally:
            self._close_dev()

    def _play(self, item: _Item):
        for attempt in (0, 1):
            try:
                h = self._ensure(item.rate, force_default=attempt == 1)
                t0 = self._clock()
                buf = self.backend.write(h, item.pcm)
                break
            except AudioError as e:
                was_default = self._open_id == WAVE_MAPPER or not self.device
                self._close_dev()
                if attempt == 1 or (was_default and not e.lost):
                    raise
                self._note(f"lost:{self.device}", f"the sound device stopped working ({e}); trying Windows default")
                self._next_check = self._clock() + RECHECK_S
        if item.on_start is not None and item.kind == "play":
            try:
                item.on_start(item)
            except Exception:  # noqa: BLE001
                pass
        dur = len(item.pcm) / 2.0 / item.rate
        started = None
        complete = False
        limit = t0 + dur + 3.0                           # a device that never says "done" doesn't hang the thread
        try:
            while True:
                with self._cv:
                    if self._interrupt or self._stop:
                        break
                if self.backend.done(buf):
                    complete = True
                    break
                if item.kind == "measure" and started is None:
                    pos = self.backend.position(h)
                    if pos:
                        started = self._clock()
                if self._clock() > limit:
                    self._note("hang", "the sound device didn't finish a sound in time")
                    break
                self.backend.wait(h, 0.002 if item.kind == "measure" and started is None else 0.01)
            if not complete:
                self.backend.reset(h)
        finally:
            try:
                self.backend.release(h, buf)
            except Exception:  # noqa: BLE001
                pass
        if item.kind == "measure":
            if started is not None:
                item.result = started - t0
            elif complete:
                item.result = (self._clock() - t0) - dur
        else:
            self.played += complete
        self._end(item, complete)


# ---------------------------------------------------------------- a stand-in device for tests
class FakeBackend:
    """A pretend sound card. Sounds "play" in real time scaled by speed (speed 10: a 1 s sound takes 0.1 s).
    latency_s: delay before the position starts moving. fail: {"open": n, "write": n} makes the n-th call (1-based)
    raise a lost-device error; devices can be changed by assigning .device_list. log: every call, in order."""
    name = "fake"

    def __init__(self, devices=None, speed: float = 1.0, latency_s: float = 0.0, report_position: bool = True):
        self.device_list = list(devices if devices is not None else [{"id": 0, "name": "Speakers"},
                                                                       {"id": 1, "name": "Headset"}])
        self.speed = float(speed)
        self.latency_s = float(latency_s)
        self.report_position = report_position
        self.log: list = []
        self.fail: dict = {}
        self.counts = {"open": 0, "write": 0}
        self.open_handles = 0
        self.lock = threading.Lock()

    def devices(self):
        return [dict(d) for d in self.device_list]

    def _maybe_fail(self, what):
        self.counts[what] += 1
        n = self.fail.get(what)
        if n is not None and (n == self.counts[what] or n == "always"):
            raise AudioError(f"fake {what} failed", MMSYSERR_NODRIVER)

    def open(self, device_id, rate):
        with self.lock:
            if device_id != WAVE_MAPPER and not any(d["id"] == device_id for d in self.device_list):
                raise AudioError("no such device", MMSYSERR_BADDEVICEID)
            self._maybe_fail("open")
            self.open_handles += 1
            self.log.append(("open", device_id, rate))
            return {"dev": device_id, "rate": rate, "closed": False}

    def write(self, handle, pcm):
        with self.lock:
            if handle["closed"]:
                raise AudioError("closed", MMSYSERR_INVALHANDLE)
            self._maybe_fail("write")
            self.log.append(("write", handle["dev"], len(pcm)))
            t = time.monotonic()
            return {"t0": t + self.latency_s / self.speed, "dur": len(pcm) / 2.0 / handle["rate"] / self.speed,
                    "reset": False, "handle": handle}

    def done(self, buf):
        return buf["reset"] or time.monotonic() >= buf["t0"] + buf["dur"]

    def wait(self, handle, timeout_s):
        time.sleep(min(timeout_s, 0.005))

    def position(self, handle):
        return None if not self.report_position else 0   # (measure uses positions only through _pos)

    def reset(self, handle):
        with self.lock:
            self.log.append(("reset", handle["dev"]))

    def release(self, handle, buf):
        buf["reset"] = True

    def close(self, handle):
        with self.lock:
            if not handle["closed"]:
                handle["closed"] = True
                self.open_handles -= 1
                self.log.append(("close", handle["dev"]))


class FakeLatencyBackend(FakeBackend):
    """FakeBackend whose position starts moving latency_s after a write (for the calibration test)."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._last = None

    def write(self, handle, pcm):
        buf = super().write(handle, pcm)
        self._last = buf
        return buf

    def position(self, handle):
        if not self.report_position or self._last is None:
            return None
        return int(max(0.0, time.monotonic() - self._last["t0"]) * handle["rate"] * self.speed) or None
