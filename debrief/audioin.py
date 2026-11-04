"""The microphone for corner notes (2.20): the default Windows input, recorded only while a key or wheel button is
held. No extra package: Windows' own waveIn interface (winmm.dll) through ctypes, loaded only when used. It mirrors
audioout.py (waveOut) and shares its ideas: one thread owns the device, nothing raises out of it, and off Windows
everything is a quiet "can't".

    r = Recorder(WaveInBackend(), skip_s=0.15)      # skip_s: the start tone's length, dropped from the take
    r.start()                                        # opens the default input and records (False + r.error if not)
    r.level                                          # 0..1, what the meter shows
    take = r.stop()                                  # a Take: take.pcm (16-bit mono, RATE Hz), take.seconds
    r.cancel()                                       # stop and forget it

Rules:
  * 16 kHz, 16-bit, mono (what the voice packs use), from the default input (WAVE_MAPPER): nothing is chosen, nothing
    is saved but the notes the person made, and the microphone is open only between start() and stop() (the
    Recording light on the Coach page follows Recorder.active).
  * A take is at most MAX_S seconds: at the limit the recorder stops by itself (hit_limit) and calls on_limit().
  * The device is closed on every path out (stop, cancel, an error, the limit).

The backend interface (WaveInBackend on Windows, FakeInput in tests):
    open(rate) -> handle (raises InputError)      start(handle)      read(handle) -> bytes recorded since the last read
    stop(handle) -> bytes (what was still held)   close(handle)
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from dataclasses import dataclass

import numpy as np

_LOG = logging.getLogger("debrief.audioin")

RATE = 16000
MAX_S = 8.0                 # a corner note is at most this long
BUF_S = 0.1                 # one waveIn buffer
BUFFERS = 6                 # queued at a time (0.6 s of room, so a slow poll loses nothing)
POLL_S = 0.03
LEVEL_FLOOR_DB = -60.0      # the meter: silence at this level or lower, full scale at 0 dB

WAVE_MAPPER = -1
CALLBACK_NULL = 0
WHDR_DONE = 0x1
MMSYSERR_NOERROR = 0


class InputError(Exception):
    def __init__(self, msg: str, code: int | None = None):
        super().__init__(msg)
        self.code = code


def level_of(pcm: bytes) -> float:
    """0..1 for the meter: the RMS of 16-bit samples, from LEVEL_FLOOR_DB (0) up to full scale (1)."""
    if not pcm or len(pcm) < 2:
        return 0.0
    x = np.frombuffer(pcm[:len(pcm) - len(pcm) % 2], "<i2").astype(np.float64) / 32768.0
    rms = float(np.sqrt(np.mean(x * x))) if x.size else 0.0
    db = 20.0 * np.log10(max(rms, 1e-9))
    return float(min(1.0, max(0.0, (db - LEVEL_FLOOR_DB) / -LEVEL_FLOOR_DB)))


# ---------------------------------------------------------------- Windows: waveIn through ctypes
class WaveInBackend:
    """winmm's waveIn* functions, polled (no callback). 16-bit mono PCM from the default input."""
    name = "wavein"

    def __init__(self):
        if sys.platform != "win32":
            raise InputError("the microphone can only be read on Windows")
        import ctypes
        from ctypes import wintypes
        self._ct = ctypes
        self._winmm = ctypes.WinDLL("winmm")

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
        self.WAVEFORMATEX, self.WAVEHDR = WAVEFORMATEX, WAVEHDR
        HWAVEIN = wintypes.HANDLE
        w = self._winmm
        w.waveInGetNumDevs.restype = wintypes.UINT
        w.waveInGetNumDevs.argtypes = []
        w.waveInOpen.restype = wintypes.UINT
        w.waveInOpen.argtypes = [ctypes.POINTER(HWAVEIN), wintypes.UINT, ctypes.POINTER(WAVEFORMATEX),
                                 ctypes.c_size_t, ctypes.c_size_t, wintypes.DWORD]
        for fn in ("waveInPrepareHeader", "waveInUnprepareHeader", "waveInAddBuffer"):
            f = getattr(w, fn)
            f.restype = wintypes.UINT
            f.argtypes = [HWAVEIN, ctypes.POINTER(WAVEHDR), wintypes.UINT]
        for fn in ("waveInStart", "waveInStop", "waveInReset", "waveInClose"):
            f = getattr(w, fn)
            f.restype = wintypes.UINT
            f.argtypes = [HWAVEIN]
        self._h = {}                    # handle value -> {"hwi", "bufs": [(WAVEHDR, buffer)], "next": int}

    def available(self) -> bool:
        try:
            return int(self._winmm.waveInGetNumDevs()) > 0
        except Exception:  # noqa: BLE001
            return False

    def open(self, rate: int = RATE):
        ct, wt = self._ct, None
        from ctypes import wintypes as wt
        fmt = self.WAVEFORMATEX(1, 1, rate, rate * 2, 2, 16, 0)
        hwi = wt.HANDLE()
        r = self._winmm.waveInOpen(ct.byref(hwi), WAVE_MAPPER & 0xFFFFFFFF, ct.byref(fmt), 0, 0, CALLBACK_NULL)
        if r != MMSYSERR_NOERROR:
            raise InputError("no microphone could be opened" + (" (it is in use by another program)" if r == 4 else ""),
                             r)
        size = int(rate * 2 * BUF_S)
        bufs = []
        try:
            for _ in range(BUFFERS):
                buf = ct.create_string_buffer(size)
                hdr = self.WAVEHDR()
                hdr.lpData = ct.addressof(buf)
                hdr.dwBufferLength = size
                self._check(self._winmm.waveInPrepareHeader(hwi, ct.byref(hdr), ct.sizeof(hdr)), "prepare")
                self._check(self._winmm.waveInAddBuffer(hwi, ct.byref(hdr), ct.sizeof(hdr)), "queue")
                bufs.append((hdr, buf))
        except InputError:
            self._release(hwi, bufs)
            raise
        key = int(hwi.value or 0)
        self._h[key] = {"hwi": hwi, "bufs": bufs, "next": 0}
        return key

    @staticmethod
    def _check(r, what):
        if r != MMSYSERR_NOERROR:
            raise InputError(f"the microphone failed ({what}, code {r})", r)

    def start(self, handle):
        self._check(self._winmm.waveInStart(self._h[handle]["hwi"]), "start")

    def read(self, handle) -> bytes:
        h = self._h[handle]
        ct = self._ct
        out = []
        n = len(h["bufs"])
        for _ in range(n):                           # buffers finish in the order they were queued
            hdr, buf = h["bufs"][h["next"]]
            if not (hdr.dwFlags & WHDR_DONE):
                break
            out.append(ct.string_at(hdr.lpData, hdr.dwBytesRecorded))
            hdr.dwFlags &= ~WHDR_DONE & 0xFFFFFFFF
            hdr.dwBytesRecorded = 0
            self._check(self._winmm.waveInAddBuffer(h["hwi"], ct.byref(hdr), ct.sizeof(hdr)), "requeue")
            h["next"] = (h["next"] + 1) % n
        return b"".join(out)

    def stop(self, handle) -> bytes:
        h = self._h[handle]
        self._winmm.waveInStop(h["hwi"])
        self._winmm.waveInReset(h["hwi"])           # hands back the buffer still filling, partly recorded
        return self.read_partial(h)

    def read_partial(self, h) -> bytes:
        ct = self._ct
        out = []
        n = len(h["bufs"])
        for _ in range(n):
            hdr, buf = h["bufs"][h["next"]]
            if not (hdr.dwFlags & WHDR_DONE):
                break
            out.append(ct.string_at(hdr.lpData, hdr.dwBytesRecorded))
            hdr.dwFlags &= ~WHDR_DONE & 0xFFFFFFFF
            hdr.dwBytesRecorded = 0
            h["next"] = (h["next"] + 1) % n
        return b"".join(out)

    def _release(self, hwi, bufs):
        ct = self._ct
        for hdr, _buf in bufs:
            try:
                self._winmm.waveInUnprepareHeader(hwi, ct.byref(hdr), ct.sizeof(hdr))
            except Exception:  # noqa: BLE001
                pass
        try:
            self._winmm.waveInClose(hwi)
        except Exception:  # noqa: BLE001
            pass

    def close(self, handle):
        h = self._h.pop(handle, None)
        if h is not None:
            try:
                self._winmm.waveInReset(h["hwi"])
            except Exception:  # noqa: BLE001
                pass
            self._release(h["hwi"], h["bufs"])


def default_backend():
    """WaveInBackend on Windows; None elsewhere (the window says the microphone can only be used on Windows)."""
    try:
        return WaveInBackend()
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- a take
@dataclass
class Take:
    pcm: bytes                  # 16-bit mono at RATE
    seconds: float
    hit_limit: bool = False
    rate: int = RATE


# ---------------------------------------------------------------- the recorder
class Recorder:
    """One recording at a time on one thread (threaded=False: the caller calls pump(), for tests). Never raises out
    of its thread; start() says False and error says why."""

    def __init__(self, backend, rate: int = RATE, max_s: float = MAX_S, skip_s: float = 0.0, threaded: bool = True,
                 on_limit=None, log=None):
        self.backend = backend
        self.rate = int(rate)
        self.max_s = float(max_s)
        self.skip_s = max(0.0, float(skip_s))
        self.threaded = threaded
        self.on_limit = on_limit
        self._log = log or _LOG.warning
        self._lock = threading.RLock()
        self._h = None
        self._chunks: list = []
        self._bytes = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.active = False             # the microphone is open: the Recording light
        self.level = 0.0
        self.error = ""
        self.hit_limit = False
        self.started_at: float | None = None

    @property
    def seconds(self) -> float:
        return self._bytes / (2.0 * self.rate)

    def start(self) -> bool:
        with self._lock:
            if self.active:
                return True
            self.error, self.hit_limit, self.level = "", False, 0.0
            self._chunks, self._bytes = [], 0
            be = self.backend
            if be is None:
                self.error = "The microphone can only be used on Windows."
                return False
            try:
                self._h = be.open(self.rate)
                be.start(self._h)
            except Exception as e:  # noqa: BLE001
                self.error = str(e) if isinstance(e, InputError) else f"The microphone could not be opened ({e.__class__.__name__})."
                self._close()
                return False
            self.active = True
            self.started_at = time.monotonic()
            self._stop = threading.Event()
            if self.threaded:
                self._thread = threading.Thread(target=self._run, args=(self._stop,), name="debrief-mic", daemon=True)
                self._thread.start()
            return True

    def pump(self):
        """Read what the device has: level, the 8 s limit. The thread calls this every POLL_S."""
        with self._lock:
            if not self.active or self._h is None:
                return
            try:
                data = self.backend.read(self._h)
            except Exception as e:  # noqa: BLE001
                self.error = "The microphone stopped working."
                self._note("read", e)
                self._close(keep=True)
                return
            limit = int((self.max_s + self.skip_s) * self.rate) * 2
            if data:
                data = data[:max(0, limit - self._bytes)]
                self._chunks.append(data)
                self._bytes += len(data)
                self.level = level_of(data)
            if self._bytes >= limit:
                self.hit_limit = True
                self._close(keep=True)
                cb = self.on_limit
            else:
                cb = None
        if cb is not None:
            try:
                cb()
            except Exception as e:  # noqa: BLE001
                self._note("limit", e)

    def _run(self, stop):
        while not stop.is_set() and self.active:
            try:
                self.pump()
            except Exception as e:  # noqa: BLE001
                self._note("pump", e)
            stop.wait(POLL_S)

    def _note(self, what, e):
        try:
            self._log(f"audioin: {what} failed ({e.__class__.__name__}: {str(e)[:100]})")
        except Exception:  # noqa: BLE001
            pass

    def _close(self, keep: bool = False):
        """Stop the device and close it. keep: leave what was recorded for stop() to hand over."""
        with self._lock:
            h, self._h = self._h, None
            self.active = False
            self.level = 0.0
            if h is None:
                return
            try:
                tail = self.backend.stop(h) or b""
            except Exception as e:  # noqa: BLE001
                tail = b""
                self._note("stop", e)
            if keep and tail and not self.hit_limit:
                self._chunks.append(tail)
                self._bytes += len(tail)
            try:
                self.backend.close(h)
            except Exception as e:  # noqa: BLE001
                self._note("close", e)

    def stop(self) -> Take | None:
        """Stop recording and give back the take (None when nothing was recorded or the microphone never opened)."""
        with self._lock:
            was = self.active
            self._stop.set()
            if was:
                self.pump()
                self._close(keep=True)
            data = b"".join(self._chunks)
            self._chunks, self._bytes = [], 0
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(1.0)
        self._thread = None
        if not data and not was and not self.hit_limit:
            return None
        skip = int(self.skip_s * self.rate) * 2
        data = data[skip:]
        cap = int(self.max_s * self.rate) * 2
        data = data[:cap]
        data = data[:len(data) - len(data) % 2]
        if not data:
            return None
        return Take(pcm=data, seconds=len(data) / (2.0 * self.rate), hit_limit=self.hit_limit, rate=self.rate)

    def cancel(self):
        with self._lock:
            self._stop.set()
            self._close()
            self._chunks, self._bytes = [], 0
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(1.0)
        self._thread = None


# ---------------------------------------------------------------- a stand-in microphone for tests
class FakeInput:
    """Plays a script into the recorder: each read() hands back one BUF_S chunk of `audio` (float samples or 16-bit
    PCM bytes), then silence, like a microphone nobody speaks into. fail_open / fail_read make it break."""
    name = "fake"

    def __init__(self, audio=None, rate: int = RATE, fail_open: bool = False, fail_read: bool = False,
                 realtime: bool = False):
        if audio is None:
            pcm = b""
        elif isinstance(audio, (bytes, bytearray)):
            pcm = bytes(audio)
        else:
            pcm = (np.clip(np.asarray(audio, dtype=np.float64), -1, 1) * 32767).astype("<i2").tobytes()
        self.pcm = pcm
        self.rate = rate
        self.fail_open, self.fail_read = fail_open, fail_read
        self.realtime = realtime        # read() gives what a real microphone would have by now, not a chunk a call
        self._t0 = 0.0
        self.pos = 0
        self.opened = 0
        self.closed = 0
        self.running = False
        self.reads = 0

    def open(self, rate=RATE):
        if self.fail_open:
            raise InputError("no microphone could be opened")
        self.opened += 1
        self.pos = 0
        self._t0 = time.monotonic()
        return self.opened

    def start(self, handle):
        self.running = True

    def read(self, handle) -> bytes:
        if self.fail_read:
            raise OSError("unplugged")
        if self.realtime:
            due = int((time.monotonic() - self._t0) * self.rate) * 2
            n = max(0, due - self.pos)
            chunk = self.pcm[self.pos:self.pos + n]
            self.pos += n
            return chunk + b"\x00" * (n - len(chunk))
        n = int(self.rate * 2 * BUF_S)
        chunk = self.pcm[self.pos:self.pos + n]
        self.pos += n
        self.reads += 1
        return chunk + b"\x00" * (n - len(chunk))

    def stop(self, handle) -> bytes:
        self.running = False
        return b""

    def close(self, handle):
        self.closed += 1
