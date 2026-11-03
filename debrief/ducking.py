"""Lower iRacing's own volume while the coach speaks, and put it back after (Coach 2.0, optional, off by default).

Windows keeps a volume for every program that plays sound (the Volume mixer). Ducker sets iRacing's to a share of
what it was while a cue plays, then back. The Windows part (CoreAudioBackend) talks to Windows' audio sessions
(IMMDeviceEnumerator -> IAudioSessionManager2 -> each session's ISimpleAudioVolume) through ctypes alone, on the
ducker's own thread; nothing Windows-only is imported until it is used.

Never leave iRacing quiet:
  * the volume is put back after every cue (hold_s after it ends), when the coach stops (close), and on Debrief's
    way out (atexit);
  * a watchdog puts it back if a cue seems to last longer than max_s (a lost "ended" call), and it stays back
    until the next cue starts;
  * while ducked, the original volumes are written to a small file (state_file); if Debrief ever dies while
    iRacing is lowered, recover() puts them back the next time the coach starts;
  * if he changes iRacing's volume himself while it is lowered, his new volume is kept, not overwritten.

    d = Ducker(backend, level=0.4)       # 40% of iRacing's volume while the coach talks
    d.speaking(True) ... d.speaking(False)
    d.close()
The backend interface: sessions(process_prefix) -> [session]; session.key, session.get() -> 0..1,
session.set(v), session.close(); start()/stop() on the ducker's thread (COM set-up).
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import sys
import threading
import time
import weakref
from pathlib import Path

_LOG = logging.getLogger("debrief.ducking")

PROCESS = "iracingsim"          # iRacingSim64DX11.exe (matched on the start of the program's file name)
LEVEL = 0.4                     # default share of iRacing's volume while the coach talks
LEVEL_RANGE = (0.1, 0.8)
HOLD_S = 0.35                   # stay lowered this long after a cue ends (two cues close together: no pumping)
MAX_S = 8.0                     # longest a cue can keep iRacing lowered (the watchdog)
DUCK_RAMP_S = 0.06
RESTORE_RAMP_S = 0.2
TICK_S = 0.02
SAME = 0.02                     # a volume within this of the one we set is still ours

_LIVE = weakref.WeakSet()


def _restore_all_at_exit():
    for d in list(_LIVE):
        try:
            d.close(timeout=1.0)
        except Exception:  # noqa: BLE001
            pass


atexit.register(_restore_all_at_exit)


def clamp_level(v) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return LEVEL
    if x > 1.0:                     # given in percent
        x /= 100.0
    return round(max(LEVEL_RANGE[0], min(LEVEL_RANGE[1], x)), 2)


class Ducker:
    def __init__(self, backend_factory, level: float = LEVEL, process: str = PROCESS, hold_s: float = HOLD_S,
                 max_s: float = MAX_S, state_file=None, log=None, clock=time.monotonic):
        self._factory = backend_factory
        self.level = clamp_level(level)
        self.process = process.lower()
        self.hold_s, self.max_s = float(hold_s), float(max_s)
        self.state_file = Path(state_file) if state_file else None
        self._log = log or _LOG.warning
        self._clock = clock
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._speaking = False
        self._since = None              # when the current cue started
        self._release_at = None
        self._tripped = False           # the watchdog fired: stay back until the next cue starts
        self._ducked: dict = {}         # session key -> (session, original, value we set)
        self.ducks = 0
        self.restores = 0
        self.watchdog_trips = 0
        self.error = ""
        self.available = None           # None until the backend has started
        self._thread = threading.Thread(target=self._run, name="debrief-duck", daemon=True)
        self._thread.start()
        _LIVE.add(self)

    # ---- any thread ----
    def speaking(self, on: bool):
        with self._lock:
            now = self._clock()
            if on:
                if not self._speaking:
                    self._since = now
                    self._tripped = False
                self._speaking = True
                self._release_at = None
            else:
                if self._speaking:
                    self._release_at = now + self.hold_s
                self._speaking = False
        self._wake.set()

    def set_level(self, level):
        self.level = clamp_level(level)

    def status(self) -> dict:
        with self._lock:
            return {"on": True, "available": self.available, "ducked": bool(self._ducked), "level": self.level,
                    "ducks": self.ducks, "restores": self.restores, "watchdog": self.watchdog_trips,
                    "error": self.error}

    def is_ducked(self) -> bool:
        with self._lock:
            return bool(self._ducked)

    def close(self, timeout: float = 2.0) -> bool:
        """Put iRacing's volume back and stop."""
        with self._lock:
            self._speaking = False
            self._release_at = None
        self._stop.set()
        self._wake.set()
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout)
        return not self._thread.is_alive()

    # ---- the ducker's thread ----
    def _want(self, now) -> bool:
        with self._lock:
            if self._speaking and self._since is not None and now - self._since > self.max_s:
                if not self._tripped:
                    self._tripped = True
                    self.watchdog_trips += 1
                    self._say("a cue kept iRacing lowered too long: its volume is back")
                return False
            if self._tripped:
                return False
            if self._speaking:
                return True
            return self._release_at is not None and now < self._release_at

    def _say(self, msg):
        try:
            self._log(f"ducking: {msg}")
        except Exception:  # noqa: BLE001
            pass

    def _run(self):
        be = None
        try:
            try:
                be = self._factory()
                if hasattr(be, "start"):
                    be.start()
                self.available = True
            except Exception as e:  # noqa: BLE001
                self.available = False
                self.error = f"Windows' volume mixer couldn't be reached ({e.__class__.__name__})"
                self._say(self.error)
                return
            recover(self.state_file, be, log=self._log)
            while not self._stop.is_set():
                now = self._clock()
                want = self._want(now)
                try:
                    if want and not self._ducked:
                        self._duck(be)
                    elif not want and self._ducked:
                        self._restore()
                except Exception as e:  # noqa: BLE001
                    self.error = f"{e.__class__.__name__}: {str(e)[:100]}"
                    self._say(f"failed ({self.error})")
                    try:
                        self._restore()
                    except Exception:  # noqa: BLE001
                        pass
                self._wake.wait(TICK_S if self._ducked else 0.25)
                self._wake.clear()
        finally:
            try:
                self._restore()
            except Exception:  # noqa: BLE001
                pass
            if be is not None and hasattr(be, "stop"):
                try:
                    be.stop()
                except Exception:  # noqa: BLE001
                    pass

    def _ramp(self, pairs, secs):
        """pairs: [(session, from, to)] moved together in a few small steps (no click, no sudden jump)."""
        steps = max(1, int(secs / 0.02))
        for i in range(1, steps + 1):
            f = i / steps
            for s, a, b in pairs:
                s.set(a + (b - a) * f)
            if i < steps:
                time.sleep(secs / steps)

    def _duck(self, be):
        found = be.sessions(self.process)
        if not found:
            return
        got = {}
        for s in found:
            try:
                v = float(s.get())
            except Exception:  # noqa: BLE001
                continue
            got[s.key] = (s, v, round(v * self.level, 4))
        if not got:
            return
        self._save_state({k: v for k, (_s, v, _t) in got.items()})
        with self._lock:
            self._ducked = got
        self.ducks += 1
        self._ramp([(s, v, t) for s, v, t in got.values()], DUCK_RAMP_S)

    def _restore(self):
        with self._lock:
            got, self._ducked = self._ducked, {}
        if not got:
            return
        pairs = []
        for s, orig, mine in got.values():
            try:
                now = float(s.get())
            except Exception:  # noqa: BLE001
                continue
            if abs(now - mine) <= SAME:              # still the volume we set: put his back
                pairs.append((s, now, orig))
        try:
            self._ramp(pairs, RESTORE_RAMP_S)
        finally:
            mine = {id(p[0]) for p in pairs}
            failed = False
            for s, orig, _m in got.values():
                try:
                    if id(s) in mine:
                        s.set(orig)                  # exactly his volume at the end of the ramp
                except Exception:  # noqa: BLE001
                    failed = True
                try:
                    s.close()
                except Exception:  # noqa: BLE001
                    pass
            self.restores += 1
            if not failed:                           # else the file stays, so recover() tries again next start
                self._clear_state()

    def _save_state(self, originals: dict):
        if self.state_file is None:
            return
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps({"process": self.process, "level": self.level, "originals": originals,
                                       "t": time.time()}), encoding="utf-8")
            os.replace(tmp, self.state_file)
        except OSError:
            pass

    def _clear_state(self):
        if self.state_file is None:
            return
        try:
            self.state_file.unlink()
        except OSError:
            pass


def recover(state_file, backend, log=None) -> int:
    """After a crash while ducked: put back the volumes in state_file for sessions that still exist and are still at
    the lowered volume. Returns how many were put back. The file is removed either way."""
    if not state_file:
        return 0
    p = Path(state_file)
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    n = 0
    try:
        orig = d.get("originals") or {}
        level = clamp_level(d.get("level"))
        for s in backend.sessions(str(d.get("process") or PROCESS)):
            try:
                if s.key in orig:
                    o = float(orig[s.key])
                    if abs(float(s.get()) - o * level) <= SAME * 2 and 0.0 <= o <= 1.0:
                        s.set(o)
                        n += 1
            finally:
                s.close()
        if n and log:
            log(f"ducking: iRacing's volume was left lowered last time; put back ({n})")
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            p.unlink()
        except OSError:
            pass
    return n


# ---------------------------------------------------------------- a stand-in mixer for tests
class FakeSession:
    def __init__(self, mixer, key, process, volume):
        self.mixer, self.key, self.process, self.volume = mixer, key, process, float(volume)
        self.closed = False

    def get(self):
        return self.volume

    def set(self, v):
        if self.mixer.broken:
            raise OSError("mixer gone")
        self.volume = float(max(0.0, min(1.0, v)))
        self.mixer.history.append((self.key, round(self.volume, 4)))

    def close(self):
        self.closed = True


class FakeMixer:
    def __init__(self, sessions=None):
        self.list = [FakeSession(self, k, p, v) for k, p, v in (sessions or [("ir1", "iRacingSim64DX11.exe", 0.8)])]
        self.history: list = []
        self.broken = False

    def sessions(self, prefix):
        return [s for s in self.list if s.process.lower().startswith(prefix.lower())]

    def by_key(self, key):
        return next(s for s in self.list if s.key == key)


# ---------------------------------------------------------------- Windows: audio sessions through ctypes COM
class CoreAudioBackend:
    """Every audio session of every active output device whose program's file name starts with the prefix. Each
    session is (pid, session instance id) with its ISimpleAudioVolume. Plain ctypes calls through the COM vtables
    (no comtypes, no pywin32 needed). Create, use and stop it on one thread (the ducker's)."""

    CLSCTX_ALL = 0x17
    E_RENDER, DEVICE_STATE_ACTIVE = 0, 1

    def __init__(self):
        if sys.platform != "win32":
            raise OSError("needs Windows")
        import ctypes
        from ctypes import wintypes
        self.ct, self.wt = ctypes, wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                        ("Data4", ctypes.c_ubyte * 8)]
        self.GUID = GUID
        self.ole32 = ctypes.WinDLL("ole32")
        self.k32 = ctypes.WinDLL("kernel32")
        self.ole32.CoCreateInstance.restype = ctypes.c_long
        self.ole32.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p, wintypes.DWORD,
                                                ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
        self.ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        self.k32.OpenProcess.restype = wintypes.HANDLE
        self.k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        self.k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                        ctypes.POINTER(wintypes.DWORD)]
        self.k32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.CLSID_Enumerator = self._guid("BCDE0395-E52F-467C-8E3D-C4579291692E")
        self.IID_Enumerator = self._guid("A95664D2-9614-4F35-A746-DE8DB63617E6")
        self.IID_Manager2 = self._guid("77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F")
        self.IID_Control2 = self._guid("BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D")
        self.IID_Volume = self._guid("87CE5498-68D6-44E5-9215-6DA47EF883D8")
        self._inited = False

    def _guid(self, s):
        import uuid
        u = uuid.UUID(s)
        g = self.GUID()
        g.Data1, g.Data2, g.Data3 = u.fields[0], u.fields[1], u.fields[2]
        for i, b in enumerate(u.bytes[8:]):
            g.Data4[i] = b
        return g

    def start(self):
        hr = self.ole32.CoInitializeEx(None, 0)          # COINIT_MULTITHREADED; S_FALSE (1) = already
        self._inited = hr in (0, 1)

    def stop(self):
        if self._inited:
            self.ole32.CoUninitialize()
            self._inited = False

    def _call(self, ptr, index, *args, argtypes=(), restype=None):
        ct = self.ct
        vtbl = ct.cast(ptr, ct.POINTER(ct.POINTER(ct.c_void_p))).contents
        proto = ct.WINFUNCTYPE(restype or ct.c_long, ct.c_void_p, *argtypes)
        hr = proto(vtbl[index])(ptr, *args)
        return hr

    def _ok(self, hr, what):
        if hr is not None and int(hr) < 0:
            raise OSError(f"{what} failed (0x{int(hr) & 0xFFFFFFFF:08X})")

    def _release(self, ptr):
        if ptr:
            try:
                self._call(ptr, 2, restype=self.ct.c_ulong)
            except Exception:  # noqa: BLE001
                pass

    def _qi(self, ptr, iid):
        ct = self.ct
        out = ct.c_void_p()
        hr = self._call(ptr, 0, ct.byref(iid), ct.byref(out), argtypes=(ct.POINTER(self.GUID), ct.POINTER(ct.c_void_p)))
        self._ok(hr, "QueryInterface")
        return out

    def _process_name(self, pid: int) -> str:
        if not pid:
            return ""
        h = self.k32.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            buf = self.ct.create_unicode_buffer(520)
            n = self.wt.DWORD(520)
            if self.k32.QueryFullProcessImageNameW(h, 0, buf, self.ct.byref(n)):
                return Path(buf.value).name
            return ""
        finally:
            self.k32.CloseHandle(h)

    def sessions(self, prefix: str) -> list:
        ct = self.ct
        prefix = prefix.lower()
        out = []
        enum = ct.c_void_p()
        self._ok(self.ole32.CoCreateInstance(ct.byref(self.CLSID_Enumerator), None, self.CLSCTX_ALL,
                                             ct.byref(self.IID_Enumerator), ct.byref(enum)), "MMDeviceEnumerator")
        coll = ct.c_void_p()
        try:
            self._ok(self._call(enum, 3, self.E_RENDER, self.DEVICE_STATE_ACTIVE, ct.byref(coll),
                                argtypes=(ct.c_int, self.wt.DWORD, ct.POINTER(ct.c_void_p))), "EnumAudioEndpoints")
            n = self.wt.UINT()
            self._ok(self._call(coll, 3, ct.byref(n), argtypes=(ct.POINTER(self.wt.UINT),)), "GetCount")
            for i in range(min(int(n.value), 32)):
                dev = ct.c_void_p()
                if self._call(coll, 4, i, ct.byref(dev), argtypes=(self.wt.UINT, ct.POINTER(ct.c_void_p))) < 0:
                    continue
                try:
                    out += self._device_sessions(dev, prefix)
                finally:
                    self._release(dev)
        finally:
            self._release(coll)
            self._release(enum)
        return out

    def _device_sessions(self, dev, prefix) -> list:
        ct = self.ct
        mgr = ct.c_void_p()
        hr = self._call(dev, 3, ct.byref(self.IID_Manager2), self.CLSCTX_ALL, None, ct.byref(mgr),
                        argtypes=(ct.POINTER(self.GUID), self.wt.DWORD, ct.c_void_p, ct.POINTER(ct.c_void_p)))
        if hr < 0:
            return []
        senum = ct.c_void_p()
        out = []
        try:
            if self._call(mgr, 5, ct.byref(senum), argtypes=(ct.POINTER(ct.c_void_p),)) < 0:
                return []
            n = ct.c_int()
            if self._call(senum, 3, ct.byref(n), argtypes=(ct.POINTER(ct.c_int),)) < 0:
                return []
            for i in range(min(n.value, 256)):
                ctl = ct.c_void_p()
                if self._call(senum, 4, i, ct.byref(ctl), argtypes=(ct.c_int, ct.POINTER(ct.c_void_p))) < 0:
                    continue
                ctl2 = vol = None
                try:
                    ctl2 = self._qi(ctl, self.IID_Control2)
                    pid = self.wt.DWORD()
                    if self._call(ctl2, 14, ct.byref(pid), argtypes=(ct.POINTER(self.wt.DWORD),)) < 0:
                        continue
                    if not self._process_name(pid.value).lower().startswith(prefix):
                        continue
                    sid = ct.c_wchar_p()
                    key = f"{pid.value}"
                    if self._call(ctl2, 13, ct.byref(sid), argtypes=(ct.POINTER(ct.c_wchar_p),)) >= 0 and sid.value:
                        key = f"{pid.value}:{sid.value[-60:]}"
                        self.ole32.CoTaskMemFree(ct.cast(sid, ct.c_void_p))
                    vol = self._qi(ctl, self.IID_Volume)
                    out.append(_WinSession(self, key, vol))
                    vol = None                                 # the session owns it now
                except OSError:
                    continue
                finally:
                    self._release(vol)
                    self._release(ctl2)
                    self._release(ctl)
        finally:
            self._release(senum)
            self._release(mgr)
        return out


class _WinSession:
    def __init__(self, be: CoreAudioBackend, key: str, vol):
        self.be, self.key, self.vol = be, key, vol

    def get(self) -> float:
        ct = self.be.ct
        v = ct.c_float()
        self.be._ok(self.be._call(self.vol, 4, ct.byref(v), argtypes=(ct.POINTER(ct.c_float),)), "GetMasterVolume")
        return float(v.value)

    def set(self, v: float):
        ct = self.be.ct
        self.be._ok(self.be._call(self.vol, 3, ct.c_float(max(0.0, min(1.0, float(v)))), None,
                                  argtypes=(ct.c_float, ct.c_void_p)), "SetMasterVolume")

    def close(self):
        if self.vol:
            self.be._release(self.vol)
            self.vol = None
