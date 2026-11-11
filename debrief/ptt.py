"""Push-to-talk keys (2.31 "Talk to your engineer"): a keyboard key read with Windows' GetAsyncKeyState (through ctypes, no extra
dependency), so it works while iRacing has the focus and does NOT take the key away from iRacing (RegisterHotKey would).

The wheel or button-box button is read by wheelmute.WheelMute (winmm joyGetPosEx, role "talk"); this module is the keyboard
half, and `capture` below is the shared "press the button you want" flow for both.

    k = KeyPoller(on_press=..., on_release=..., binding={"vk": 120, "label": "F9"})
    k.start(); k.learn(cb); k.status(); k.stop()

Polled at 30 Hz on its own thread, one GetAsyncKeyState call per poll (a few microseconds). Off Windows, or in tests, a
fake backend stands in. Never raises out of its thread.
"""
from __future__ import annotations

import logging
import sys
import threading
import time

_LOG = logging.getLogger("debrief.ptt")
POLL_HZ = 30.0
LEARN_TIMEOUT_S = 15.0
HOLD_MAX_S = 20.0           # a key that never reads as released stops the recording after this
# keys that can't be a talk button (the mouse, the keys that type or close things the sim needs)
NOT_ALLOWED = {0x01, 0x02, 0x04, 0x05, 0x06, 0x1B, 0x5B, 0x5C, 0x14}     # mouse buttons, Esc, Win keys, Caps Lock
NAMES = {0x08: "Backspace", 0x09: "Tab", 0x0D: "Enter", 0x20: "Space", 0x21: "Page Up", 0x22: "Page Down", 0x23: "End",
         0x24: "Home", 0x25: "Left", 0x26: "Up", 0x27: "Right", 0x28: "Down", 0x2D: "Insert", 0x2E: "Delete",
         0xC0: "` (backtick)", 0xBA: ";", 0xBB: "=", 0xBC: ",", 0xBD: "-", 0xBE: ".", 0xBF: "/", 0xDB: "[", 0xDC: "\\",
         0xDD: "]", 0xDE: "'", 0xA0: "Left Shift", 0xA1: "Right Shift", 0xA2: "Left Ctrl", 0xA3: "Right Ctrl",
         0xA4: "Left Alt", 0xA5: "Right Alt", 0x13: "Pause", 0x91: "Scroll Lock", 0x90: "Num Lock"}


def key_name(vk: int) -> str:
    if vk in NAMES:
        return NAMES[vk]
    if 0x70 <= vk <= 0x87:
        return f"F{vk - 0x6F}"
    if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
        return chr(vk)
    if 0x60 <= vk <= 0x69:
        return f"Numpad {vk - 0x60}"
    return f"Key {vk}"


def clean_binding(b):
    """{"vk": 1..254, "label": text} or None."""
    if not isinstance(b, dict):
        return None
    try:
        vk = int(b.get("vk"))
    except (TypeError, ValueError):
        return None
    if not 8 <= vk <= 254 or vk in NOT_ALLOWED:
        return None
    return {"vk": vk, "label": key_name(vk)}


class WinKeys:
    """GetAsyncKeyState through ctypes."""

    def __init__(self):
        import ctypes
        self._f = ctypes.windll.user32.GetAsyncKeyState

    def down(self, vk: int) -> bool:
        return bool(self._f(int(vk)) & 0x8000)

    def all_down(self) -> list:
        return [vk for vk in range(8, 255) if vk not in NOT_ALLOWED and self.down(vk)]


class FakeKeys:
    """A keyboard for tests: set .held to the set of virtual-key codes currently down."""

    def __init__(self):
        self.held: set = set()
        self.calls = 0

    def down(self, vk):
        self.calls += 1
        return vk in self.held

    def all_down(self):
        return sorted(v for v in self.held if v not in NOT_ALLOWED)


def default_backend():
    if sys.platform != "win32":
        return None
    try:
        return WinKeys()
    except Exception:  # noqa: BLE001
        return None


class KeyPoller:
    def __init__(self, on_press=None, on_release=None, binding=None, backend=None, hz: float = POLL_HZ, log=None,
                 clock=time.monotonic):
        self.on_press, self.on_release = on_press, on_release
        self.binding = clean_binding(binding)
        self._backend = backend
        self._tried = backend is not None
        self.hz = max(5.0, min(100.0, float(hz)))
        self._log = log or _LOG.warning
        self._clock = clock
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._learn = None
        self._held = False
        self._since = 0.0
        self.presses = 0
        self.polls = 0

    def _be(self):
        if not self._tried:
            self._tried = True
            self._backend = default_backend()
        return self._backend

    def available(self) -> bool:
        return self._be() is not None

    def start(self) -> bool:
        if self._be() is None:
            return False
        with self._lock:
            if self._thread is not None and self._thread.is_alive() and not self._stop.is_set():
                return True
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._run, args=(self._stop,), daemon=True, name="debrief-ptt-key")
            self._thread.start()
        return True

    def stop(self, timeout: float = 2.0) -> bool:
        with self._lock:
            self._stop.set()
            t, self._thread = self._thread, None
            self._learn = None
        if t is not None and t is not threading.current_thread():
            t.join(timeout)
        self._release()
        return t is None or not t.is_alive()

    def running(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive()

    def set_binding(self, binding):
        with self._lock:
            self.binding = clean_binding(binding)
            self._release()

    def learn(self, on_learned=None, timeout_s: float = LEARN_TIMEOUT_S) -> bool:
        """The next key that goes down becomes the binding: on_learned(binding), or on_learned(None) after timeout_s."""
        if self._be() is None:
            return False
        be = self._backend
        with self._lock:
            self._learn = {"cb": on_learned, "until": self._clock() + max(1.0, float(timeout_s)),
                           "base": set(be.all_down())}
        return self.start()

    def cancel_learn(self):
        with self._lock:
            self._learn = None

    def status(self) -> dict:
        b = self.binding
        if not self.available():
            return {"state": "unavailable", "text": "Keyboard keys can't be read on this computer.", "binding": None}
        if self._learn is not None:
            return {"state": "learning", "text": "Press the key you want to hold to talk.", "binding": b}
        if b is None:
            return {"state": "not_set", "text": "No key chosen.", "binding": None}
        return {"state": "ready" if self.running() else "off", "text": f"Hold {b['label']} to talk.", "binding": dict(b)}

    def _release(self):
        if self._held:
            self._held = False
            if self.on_release is not None:
                try:
                    self.on_release()
                except Exception as e:  # noqa: BLE001
                    self._log(f"ptt: release action failed ({e.__class__.__name__})")

    def _run(self, stop):
        period = 1.0 / self.hz
        while not stop.wait(period):
            try:
                self.poll_once()
            except Exception as e:  # noqa: BLE001
                self._log(f"ptt: key read failed ({e.__class__.__name__})")
        self._release()

    def poll_once(self):
        be = self._backend
        if be is None:
            return
        self.polls += 1
        with self._lock:
            lr, b = self._learn, self.binding
        if lr is not None:
            now = self._clock()
            new = [vk for vk in be.all_down() if vk not in lr["base"]]
            lr["base"] &= set(be.all_down()) | lr["base"]
            nb = clean_binding({"vk": new[0]}) if new else None
            if nb is not None or now >= lr["until"]:
                with self._lock:
                    self._learn = None
                    if nb is not None:
                        self.binding = nb
                if lr["cb"] is not None:
                    lr["cb"](nb)
            return
        if b is None:
            return
        down = be.down(b["vk"])
        now = self._clock()
        if down and not self._held:
            self._held, self._since = True, now
            self.presses += 1
            if self.on_press is not None:
                self.on_press()
        elif self._held and (not down or now - self._since > HOLD_MAX_S):
            self._release()
