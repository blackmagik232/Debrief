"""Mute the live coach from a button on the wheel (2.14).

Windows' own joystick interface (winmm.dll: joyGetNumDevs, joyGetDevCapsW, joyGetPosEx with JOY_RETURNBUTTONS),
through ctypes, loaded only when used: up to 16 devices and 32 buttons each, read about 20 times a second on a
thread of its own (one call per read for the chosen device). The device list is only looked at again when the chosen
device stops answering (after 2 s, then less and less often, up to every 30 s), or when the device on its number
turns out to be another one (its name and numbers are checked every 5 s); while learning, at most once a second.
Off Windows, or when winmm can't be loaded, everything is a quiet no-op.

    w = WheelMute(on_press=coach_runner.toggle, binding=saved_binding)
    w.start()                       # False when there's no joystick interface (not Windows)
    w.learn(on_learned)             # "press the button you want": the next button pressed on any device
    w.status()                      # {"state", "text", "binding"} in plain words for the window
    w.stop()

A binding (JSON-ready, saved by the caller, e.g. with the coach settings):
    {"device": 2, "button": 5, "name": "FANATEC Wheel", "mid": 3615, "pid": 6, "label": "Button 5 on FANATEC Wheel"}
button counts from 1, as Windows' game controller panel shows it.

Devices can change number when USB devices are plugged in a different order, so the binding is matched by the
device's identity first (product name, and the maker / product numbers Windows reports), its number second: the
saved number is used only when the device on it has the same identity, or when the saved binding has none.
Note: for many USB wheels Windows reports a generic name here (e.g. "Microsoft PC-joystick driver"); then the maker
and product numbers are what tell devices apart, and two identical devices fall back to the saved number.

Never raises out of its thread: a failing read is logged once and the loop carries on.
"""
from __future__ import annotations

import logging
import sys
import threading
import time

_LOG = logging.getLogger("debrief.wheelmute")

MAX_DEVICES = 16
MAX_BUTTONS = 32
POLL_HZ = 20.0
RESCAN_MIN_S = 2.0          # the chosen device stopped answering: look for it after this ...
RESCAN_MAX_S = 30.0         # ... doubling each time it still isn't there, up to this
RESCAN_S = RESCAN_MIN_S     # (kept for older callers)
IDENTITY_S = 5.0            # while it answers, check this often that it is still the same device (one caps call)
LEARN_SCAN_S = 1.0          # while learning, the device list at most this often
DEBOUNCE_S = 0.3            # a second press within this is ignored
LEARN_TIMEOUT_S = 15.0


# ---------------------------------------------------------------- the Windows backend
class WinmmBackend:
    """joyGetPosEx / joyGetDevCapsW through ctypes. devices() -> [{"id","name","mid","pid","buttons"}];
    buttons(id) -> the pressed-buttons bitmask (bit 0 = button 1) or None when the device isn't there."""

    JOY_RETURNBUTTONS = 0x80
    JOYERR_NOERROR = 0

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self._ct = ctypes
        self._winmm = ctypes.WinDLL("winmm")

        class JOYINFOEX(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("dwFlags", wintypes.DWORD), ("dwXpos", wintypes.DWORD),
                        ("dwYpos", wintypes.DWORD), ("dwZpos", wintypes.DWORD), ("dwRpos", wintypes.DWORD),
                        ("dwUpos", wintypes.DWORD), ("dwVpos", wintypes.DWORD), ("dwButtons", wintypes.DWORD),
                        ("dwButtonNumber", wintypes.DWORD), ("dwPOV", wintypes.DWORD),
                        ("dwReserved1", wintypes.DWORD), ("dwReserved2", wintypes.DWORD)]

        class JOYCAPSW(ctypes.Structure):
            _fields_ = [("wMid", wintypes.WORD), ("wPid", wintypes.WORD), ("szPname", wintypes.WCHAR * 32),
                        ("wXmin", wintypes.UINT), ("wXmax", wintypes.UINT), ("wYmin", wintypes.UINT),
                        ("wYmax", wintypes.UINT), ("wZmin", wintypes.UINT), ("wZmax", wintypes.UINT),
                        ("wNumButtons", wintypes.UINT), ("wPeriodMin", wintypes.UINT),
                        ("wPeriodMax", wintypes.UINT), ("wRmin", wintypes.UINT), ("wRmax", wintypes.UINT),
                        ("wUmin", wintypes.UINT), ("wUmax", wintypes.UINT), ("wVmin", wintypes.UINT),
                        ("wVmax", wintypes.UINT), ("wCaps", wintypes.UINT), ("wMaxAxes", wintypes.UINT),
                        ("wNumAxes", wintypes.UINT), ("wMaxButtons", wintypes.UINT),
                        ("szRegKey", wintypes.WCHAR * 32), ("szOEMVxD", wintypes.WCHAR * 260)]
        self._JI, self._JC = JOYINFOEX, JOYCAPSW
        self._winmm.joyGetNumDevs.restype = wintypes.UINT
        self._winmm.joyGetPosEx.argtypes = [wintypes.UINT, ctypes.POINTER(JOYINFOEX)]
        self._winmm.joyGetPosEx.restype = wintypes.UINT
        self._winmm.joyGetDevCapsW.argtypes = [ctypes.c_size_t, ctypes.POINTER(JOYCAPSW), wintypes.UINT]
        self._winmm.joyGetDevCapsW.restype = wintypes.UINT
        self._info = JOYINFOEX()
        self._info.dwSize = ctypes.sizeof(JOYINFOEX)
        self._info.dwFlags = self.JOY_RETURNBUTTONS

    def buttons(self, dev: int):
        if self._winmm.joyGetPosEx(int(dev), self._ct.byref(self._info)) != self.JOYERR_NOERROR:
            return None
        return int(self._info.dwButtons)

    def identity(self, dev: int):
        """{"name", "mid", "pid"} of the device on this number (one joyGetDevCapsW call), None when unreadable."""
        caps = self._JC()
        if self._winmm.joyGetDevCapsW(int(dev), self._ct.byref(caps), self._ct.sizeof(caps)) != self.JOYERR_NOERROR:
            return None
        return {"name": caps.szPname.strip(), "mid": int(caps.wMid), "pid": int(caps.wPid)}

    def devices(self) -> list:
        out = []
        n = min(MAX_DEVICES, int(self._winmm.joyGetNumDevs() or 0))
        for i in range(n):
            if self.buttons(i) is None:                  # nothing plugged in on this number
                continue
            caps = self._JC()
            name, mid, pid, nb = "", None, None, None
            if self._winmm.joyGetDevCapsW(i, self._ct.byref(caps), self._ct.sizeof(caps)) == self.JOYERR_NOERROR:
                name, mid, pid, nb = caps.szPname.strip(), int(caps.wMid), int(caps.wPid), int(caps.wNumButtons)
            out.append({"id": i, "name": name, "mid": mid, "pid": pid, "buttons": nb})
        return out


def default_backend():
    """The Windows backend, or None (not Windows, or winmm can't be loaded)."""
    if sys.platform != "win32":
        return None
    try:
        return WinmmBackend()
    except Exception as e:  # noqa: BLE001
        _LOG.warning(f"wheelmute: Windows joystick interface not available ({e.__class__.__name__}: {e})")
        return None


# ---------------------------------------------------------------- bindings
def clean_binding(b) -> dict | None:
    """A saved binding checked: {"device": 0..15, "button": 1..32, "name", "mid", "pid", "label"} or None."""
    if not isinstance(b, dict):
        return None
    try:
        dev, btn = int(b.get("device")), int(b.get("button"))
    except (TypeError, ValueError):
        return None
    if not (0 <= dev < MAX_DEVICES and 1 <= btn <= MAX_BUTTONS) or isinstance(b.get("button"), bool):
        return None

    def opt_int(v):
        try:
            return None if v is None or isinstance(v, bool) else int(v)
        except (TypeError, ValueError):
            return None
    name = str(b.get("name") or "")[:64]
    out = {"device": dev, "button": btn, "name": name, "mid": opt_int(b.get("mid")), "pid": opt_int(b.get("pid"))}
    out["label"] = label(out)
    return out


def label(b: dict) -> str:
    """'Button 5 on FANATEC Wheel' (or 'on controller 3' when Windows gives no name)."""
    who = (b.get("name") or "").strip() or f"controller {int(b.get('device', 0)) + 1}"
    return f"Button {b.get('button')} on {who}"


def _same(dev: dict, b: dict) -> bool:
    """Same identity: maker and product numbers when both sides have them, else the name."""
    if b.get("mid") is not None and b.get("pid") is not None and dev.get("mid") is not None:
        return (dev.get("mid"), dev.get("pid")) == (b["mid"], b["pid"]) and \
            (not b.get("name") or not dev.get("name") or dev.get("name") == b.get("name"))
    return bool(b.get("name")) and dev.get("name") == b.get("name")


def match(devices: list, b: dict | None):
    """The device number the binding means now, or None when its device isn't plugged in."""
    if not b:
        return None
    devs = [d for d in devices or [] if isinstance(d, dict)]
    has_identity = bool(b.get("name")) or b.get("mid") is not None
    if not has_identity:
        return b["device"] if any(d.get("id") == b["device"] for d in devs) else None
    same = [d for d in devs if _same(d, b)]
    if not same:
        return None
    for d in same:                                      # two identical devices: the saved number decides
        if d.get("id") == b["device"]:
            return d["id"]
    return same[0]["id"]


# ---------------------------------------------------------------- the poller
class WheelMute:
    """Polls one wheel button and calls on_press() on each press (the moment it goes down). See the module text."""

    def __init__(self, on_press=None, binding=None, backend=None, hz: float = POLL_HZ, log=None,
                 clock=time.monotonic):
        self.on_press = on_press
        self.binding = clean_binding(binding)
        self._backend = backend
        self._backend_tried = backend is not None
        self.hz = max(1.0, min(100.0, float(hz)))
        self._log = log or _LOG.warning
        self._clock = clock
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._learn = None              # {"cb", "until", "base": {dev: mask}}
        self._learn_result = None       # "learned" | "timeout" | "cancelled"
        self._dev = None                # the device number the binding is on now
        self._prev = False
        self._last_press = -1e9
        self._last_scan = -1e9
        self._next_scan = -1e9          # the chosen device is missing: look again at this time
        self._backoff = 0.0             # ... after this long (RESCAN_MIN_S doubling up to RESCAN_MAX_S)
        self._last_ident = -1e9
        self.scans = 0                  # device lists read (tests)
        self.answered: set = set()      # device numbers that have answered at some point
        self._devices: list = []
        self._logged = set()
        self.presses = 0

    # ---- public ----
    def available(self) -> bool:
        return self._get_backend() is not None

    def start(self) -> bool:
        """Start polling. False (and no thread) when there's no joystick interface on this computer."""
        if self._get_backend() is None:
            return False
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return True
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, daemon=True, name="debrief-wheelmute")
            self._thread.start()
        return True

    def stop(self, timeout: float = 2.0) -> bool:
        """Stop polling; True when the thread has ended."""
        self._stop.set()
        with self._lock:
            t, self._thread = self._thread, None
            self._learn = None
        if t is not None and t is not threading.current_thread():
            t.join(timeout)
            return not t.is_alive()
        return True

    def running(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive()

    def set_binding(self, binding):
        with self._lock:
            self.binding = clean_binding(binding)
            self._dev = None
            self._prev = True               # a button already held when it is set doesn't count as a press
            self._next_scan = -1e9
            self._backoff = 0.0

    def learn(self, on_learned=None, timeout_s: float = LEARN_TIMEOUT_S) -> bool:
        """'Press the button you want': the next button that goes down on any device becomes the binding, and
        on_learned(binding) is called (on_learned(None) after timeout_s with no press). Starts polling if needed.
        False when there's no joystick interface."""
        if self._get_backend() is None:
            return False
        with self._lock:
            self._learn = {"cb": on_learned, "until": self._clock() + max(1.0, float(timeout_s)), "base": None}
            self._learn_result = None
        return self.start()

    def cancel_learn(self):
        with self._lock:
            if self._learn is not None:
                self._learn = None
                self._learn_result = "cancelled"

    def status(self) -> dict:
        """{"state": "unavailable" | "off" | "learning" | "not_set" | "not_found" | "ready", "text", "binding"}."""
        b = self.binding
        if self._backend is None and self._backend_tried:
            st, text = "unavailable", "Wheel buttons can't be read on this computer."
        elif self._learn is not None:
            st, text = "learning", ("Press the button on your wheel you want to use for mute. Windows only "
                                    "passes on the first 32 buttons of each device, so pick one of those.")
        elif not self.running():
            st, text = "off", "Wheel button mute is off."
        elif b is None:
            st, text = "not_set", ("No wheel button chosen yet." if self._learn_result != "timeout" else
                                   "No button was pressed. Try again and press the button within 15 seconds.")
        elif self._dev is None:
            st, text = "not_found", f"{label(b)}: that controller isn't plugged in."
        else:
            st, text = "ready", f"{label(b)} mutes and unmutes the coach."
        return {"state": st, "text": text, "binding": dict(b) if b else None, "presses": self.presses}

    # ---- internals ----
    def _get_backend(self):
        if not self._backend_tried:
            self._backend_tried = True
            self._backend = default_backend()
        return self._backend

    def _note(self, key, msg):
        if key not in self._logged:
            self._logged.add(key)
            try:
                self._log(msg)
            except Exception:  # noqa: BLE001
                pass

    def _run(self):
        period = 1.0 / self.hz
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as e:  # noqa: BLE001  (never out of the thread)
                self._note(("loop", e.__class__.__name__), f"wheelmute: read failed ({e.__class__.__name__}: "
                                                           f"{str(e)[:120]})")
            self._stop.wait(period)

    def _scan(self, now):
        be = self._backend
        self.scans += 1
        try:
            self._devices = list(be.devices() or [])
        except Exception as e:  # noqa: BLE001
            self._devices = []
            self._note(("scan", e.__class__.__name__), f"wheelmute: device list failed ({e})")
        self.answered |= {d.get("id") for d in self._devices if isinstance(d, dict)}
        self._last_scan = now
        self._dev = match(self._devices, self.binding)
        self._last_ident = now

    def _missing(self, now):
        """The chosen device isn't there: look again after RESCAN_MIN_S, doubling up to RESCAN_MAX_S."""
        self._dev = None
        self._prev = False
        self._backoff = min(RESCAN_MAX_S, self._backoff * 2.0) if self._backoff else RESCAN_MIN_S
        self._next_scan = now + self._backoff

    def _still_same(self, now) -> bool:
        """Every IDENTITY_S: the device on the chosen number is still the bound one (backends with identity())."""
        fn = getattr(self._backend, "identity", None)
        b = self.binding
        if fn is None or now - self._last_ident < IDENTITY_S or not (b.get("name") or b.get("mid") is not None):
            return True
        self._last_ident = now
        try:
            ident = fn(self._dev)
        except Exception as e:  # noqa: BLE001
            self._note(("ident", e.__class__.__name__), f"wheelmute: device check failed ({e})")
            return True
        return ident is not None and _same(ident, b)

    def poll_once(self):
        """One read (the thread calls this ~20 times a second; tests call it directly)."""
        be = self._backend
        if be is None:
            return
        now = self._clock()
        with self._lock:
            lr = self._learn
        if lr is not None:
            self._poll_learn(lr, now)
            return
        if self.binding is None:
            return
        if self._dev is None:                            # not found yet / unplugged: look, with a growing wait
            if now < self._next_scan:
                return
            self._scan(now)
            if self._dev is None:
                self._missing(now)
                return
            self._backoff = 0.0
        elif not self._still_same(now):                  # another device on that number now: look for ours
            self._scan(now)
            if self._dev is None:
                self._missing(now)
                return
        try:
            mask = be.buttons(self._dev)
        except Exception as e:  # noqa: BLE001
            mask = None
            self._note(("read", e.__class__.__name__), f"wheelmute: button read failed ({e})")
        if mask is None:                                 # unplugged: look for it again (now, then backing off)
            self._dev = None
            self._prev = False
            self._next_scan = now
            return
        down = bool(int(mask) >> (self.binding["button"] - 1) & 1)
        if down and not self._prev and now - self._last_press >= DEBOUNCE_S:
            self._last_press = now
            self.presses += 1
            if self.on_press is not None:
                try:
                    self.on_press()
                except Exception as e:  # noqa: BLE001
                    self._note(("cb", e.__class__.__name__), f"wheelmute: mute toggle failed ({e})")
        self._prev = down

    def _poll_learn(self, lr, now):
        be = self._backend
        if now - self._last_scan >= LEARN_SCAN_S:
            self._scan(now)
        masks = {}
        for d in self._devices:
            try:
                m = be.buttons(d["id"])
            except Exception:  # noqa: BLE001
                m = None
            if m is not None:
                masks[d["id"]] = int(m)
        if lr["base"] is None:                           # buttons already held when learning starts don't count
            lr["base"] = masks
            return
        found = None
        for d in self._devices:
            dev = d["id"]
            if dev not in masks:
                continue
            new = masks[dev] & ~lr["base"].get(dev, 0)
            if new:
                bit = (new & -new).bit_length()          # lowest newly pressed button, 1-based
                found = clean_binding({"device": dev, "button": bit, "name": d.get("name"), "mid": d.get("mid"),
                                       "pid": d.get("pid")})
                break
            lr["base"][dev] = masks[dev]                 # a button let go and pressed again still counts
        timed_out = found is None and now > lr["until"]
        if found is None and not timed_out:
            return
        with self._lock:
            if self._learn is not lr:
                return
            self._learn = None
            if found is not None:
                self.binding = found
                self._dev = found["device"]
                self._prev = True                        # the learning press doesn't toggle mute
                self._learn_result = "learned"
            else:
                self._learn_result = "timeout"
        cb = lr.get("cb")
        if cb is not None:
            try:
                cb(found)
            except Exception as e:  # noqa: BLE001
                self._note(("learncb", e.__class__.__name__), f"wheelmute: saving the button failed ({e})")
