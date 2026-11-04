"""The coach overlay (Debrief 2.19): a small see-through window on top of iRacing while the live coach runs.

It shows the state in large type (COACHING, MUTED, REHEARSING or OFF), the one thing and its count, the last cue
and the lap's delta to the reference. Clicks pass through it, so it never takes the mouse or the focus from
iRacing. iRacing has to run in borderless windowed mode: exclusive fullscreen hides every overlay.

Layout of this file, so most of it can be tested on Linux with no display:

* view_model(status, unit): what to show, from the coach's own status (the one LiveRunner.status() dict the Coach
  page and the driving view read). Pure.
* ex_style / alpha_byte: the Win32 window-style flags (click-through, no taskbar button, no focus). Pure.
* Store: the saved choices, per monitor (position, size, opacity) and the on/off setting (off by default).
* Win32: the few Win32 calls, with ctypes imported only when one is made.
* OverlayWindow: the Tk window. The Tk module and the Win32 shim are passed in, so tests give it fakes.
* Overlay: the controller the server holds. It runs the window on its own thread, only when asked, and does
  nothing at all (no thread, no window) while the overlay is off. If the window can't be made (no Tk, not
  Windows) it says why and the coach carries on normally.

tkinter and ctypes.windll are never imported at module import time.
"""
from __future__ import annotations

import math
import re
import sys
import threading
import time

from . import units as _units

# ---------------------------------------------------------------- what to show
STATE_WORDS = {"coaching": ("COACHING", "ok"), "muted": ("MUTED", "bad"), "rehearsing": ("REHEARSING", "info"),
               "off": ("OFF", "off"), "waiting": ("WAITING", "off"), "starting": ("STARTING", "off")}
COLORS = {"ok": "#3ddc84", "bad": "#ff4d4d", "info": "#5aa9ff", "off": "#9aa0a6", "text": "#f2f4f7",
          "dim": "#aeb4bd", "bg": "#101418", "edit": "#1d2d4d"}
NO_THING = "No one thing yet"
NO_CUE = "Nothing said yet"
EDIT_HINT = "Drag to move. Drag the corner to resize."


def turn_words(t) -> str:
    """"T4" / "C12" written as "Turn 4" (the same rule as the Coach page's turnWords)."""
    return re.sub(r"\b[TC](\d{1,2})\b(?!\.\w)", r"Turn \1", str(t or ""))


def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def signed(d) -> str:
    """+0.12 for slower, a real minus sign for quicker, 0.00 when level (the driving view's cdSigned)."""
    x = _num(d)
    if x is None:
        return ""
    s = f"{abs(x):.2f}"
    return ("+" if x > 0 and float(s) else "−" if x < 0 and float(s) else "") + s


def view_model(status, unit="mph", edit=False) -> dict:
    """What the overlay shows for the coach's status (None when no coach has been started).

    {"state": coaching|muted|rehearsing|off|waiting|starting, "word": "COACHING", "tone": ok|bad|info|off,
     "thing": str, "count": str, "cue": str, "delta": "+0.12 s" or "", "delta_tone": ok|bad|dim, "sub": str}
    Speeds in the coach's words are written in the unit from Settings > Units. Edit mode shows the hint in `sub`."""
    st = status if isinstance(status, dict) else {}
    running = bool(st.get("running"))
    state = st.get("show") if running else "off"
    if running and st.get("state") == "error":
        state = "off"
    if state not in STATE_WORDS:
        state = "coaching" if running else "off"
    word, tone = STATE_WORDS[state]
    thing = count = cue = delta = sub = ""
    delta_tone = "dim"
    if running and state != "off":
        F = st.get("focus") if isinstance(st.get("focus"), dict) else None
        thing = turn_words((F or {}).get("text") or "") or NO_THING
        count = str((F or {}).get("count_text") or (F or {}).get("progress") or "")
        sp = [c for c in (st.get("spoken") or []) if isinstance(c, dict) and c.get("text")]
        cue = str(sp[-1]["text"]) if sp else NO_CUE
        d = _num(st.get("live_delta"))
        if d is None and isinstance(st.get("last_lap"), dict):
            d = _num(st["last_lap"].get("delta"))
        if d is not None:
            delta = signed(d) + " s"
            delta_tone = "bad" if d > 0.005 else "ok" if d < -0.005 else "dim"
        if state == "muted":
            m = st.get("mute") if isinstance(st.get("mute"), dict) else {}
            ql = _num(m.get("quiet_left"))
            sub = f"Quiet for {int(math.ceil(ql))} s more" if ql is not None and ql > 0 else "Unmute to hear cues"
    vm = {"state": state, "word": word, "tone": tone, "thing": thing, "count": count, "cue": cue, "delta": delta,
          "delta_tone": delta_tone, "sub": sub}
    if edit:
        vm["sub"] = EDIT_HINT
    return {k: _units.convert_text(v, unit) if isinstance(v, str) else v for k, v in vm.items()}


# ---------------------------------------------------------------- Win32 window styles
GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020      # clicks go to the window underneath
WS_EX_TOOLWINDOW = 0x00000080       # no taskbar button, not in Alt+Tab
WS_EX_APPWINDOW = 0x00040000
WS_EX_LAYERED = 0x00080000          # needed for opacity
WS_EX_NOACTIVATE = 0x08000000       # never takes the focus
LWA_ALPHA = 0x2
CLICK_THROUGH_FLAGS = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
EDIT_FLAGS = WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE


def ex_style(current: int, click_through: bool) -> int:
    """The window's extended style with the overlay's flags set. click_through=False (Edit position) keeps the
    window layered and focus-free but lets the mouse reach it so it can be dragged."""
    s = (int(current) | EDIT_FLAGS) & ~WS_EX_APPWINDOW
    return (s | WS_EX_TRANSPARENT) if click_through else (s & ~WS_EX_TRANSPARENT)


def alpha_byte(opacity) -> int:
    x = _num(opacity)
    x = DEFAULT_OPACITY if x is None else min(MAX_OPACITY, max(MIN_OPACITY, x))
    return int(round(x * 255))


# ---------------------------------------------------------------- saved choices, per monitor
DEFAULT_OPACITY, MIN_OPACITY, MAX_OPACITY = 0.85, 0.3, 1.0
SIZES = {"small": (280, 132), "medium": (360, 170), "large": (480, 226)}
DEFAULT_SIZE = "medium"
MIN_W, MIN_H, MAX_W, MAX_H = 220, 104, 900, 450
MARGIN = 24


def monitor_key(m: dict) -> str:
    """One name for a monitor: its size and where it sits, e.g. "2560x1440@0,0"."""
    return f"{int(m['w'])}x{int(m['h'])}@{int(m['x'])},{int(m['y'])}"


def pick_monitor(monitors: list, x, y) -> dict | None:
    """The monitor that holds the point, else the primary one, else the first."""
    for m in monitors:
        if m["x"] <= x < m["x"] + m["w"] and m["y"] <= y < m["y"] + m["h"]:
            return m
    return next((m for m in monitors if m.get("primary")), monitors[0] if monitors else None)


def fit(g: dict, mon: dict) -> dict:
    """g (x, y, w, h, opacity) cleaned: size in range, kept inside the monitor so it can't be lost off-screen."""
    w = int(min(MAX_W, max(MIN_W, _num(g.get("w")) or SIZES[DEFAULT_SIZE][0])))
    h = int(min(MAX_H, max(MIN_H, _num(g.get("h")) or SIZES[DEFAULT_SIZE][1])))
    w, h = min(w, mon["w"]), min(h, mon["h"])
    x = _num(g.get("x"))
    y = _num(g.get("y"))
    if x is None or y is None:
        x, y = mon["x"] + mon["w"] - w - MARGIN, mon["y"] + MARGIN        # top right
    x = int(min(max(x, mon["x"]), mon["x"] + mon["w"] - w))
    y = int(min(max(y, mon["y"]), mon["y"] + mon["h"] - h))
    op = _num(g.get("opacity"))
    op = DEFAULT_OPACITY if op is None else round(min(MAX_OPACITY, max(MIN_OPACITY, op)), 2)
    return {"x": x, "y": y, "w": w, "h": h, "opacity": op}


class Store:
    """The overlay's saved choices. load() returns the saved dict ({} for none); save(d) writes it (may raise
    OSError). Shape: {"enabled": False, "last_monitor": key, "monitors": {key: {x, y, w, h, opacity}}}."""

    def __init__(self, load, save):
        self._load, self._save = load, save
        self._lock = threading.Lock()

    def data(self) -> dict:
        try:
            d = self._load()
        except Exception:  # noqa: BLE001
            d = {}
        d = d if isinstance(d, dict) else {}
        mons = d.get("monitors") if isinstance(d.get("monitors"), dict) else {}
        return {"enabled": d.get("enabled") is True,        # off unless it was turned on
                "last_monitor": d.get("last_monitor") if isinstance(d.get("last_monitor"), str) else None,
                "monitors": {str(k): dict(v) for k, v in mons.items() if isinstance(v, dict)}}

    def enabled(self) -> bool:
        return self.data()["enabled"]

    def _update(self, fn):
        with self._lock:
            d = self.data()
            fn(d)
            self._save(d)

    def set_enabled(self, on: bool):
        self._update(lambda d: d.__setitem__("enabled", bool(on)))

    def geometry(self, mon: dict) -> dict:
        return fit(self.data()["monitors"].get(monitor_key(mon), {}), mon)

    def save_geometry(self, mon: dict, **kw):
        def go(d):
            key = monitor_key(mon)
            cur = d["monitors"].get(key, {})
            cur.update({k: v for k, v in kw.items() if k in ("x", "y", "w", "h", "opacity")})
            d["monitors"][key] = fit(cur, mon)
            d["last_monitor"] = key
        self._update(go)

    def start_monitor(self, monitors: list) -> dict | None:
        """Where it opens: the monitor it was last on, if that one is still connected, else the primary."""
        last = self.data()["last_monitor"]
        for m in monitors:
            if monitor_key(m) == last:
                return m
        return pick_monitor(monitors, -10 ** 9, -10 ** 9)


# ---------------------------------------------------------------- the Win32 calls (ctypes only when called)
class Win32:
    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ct, self.wt = ctypes, wintypes
        u = self.user32 = ctypes.windll.user32
        u.GetParent.argtypes = [wintypes.HWND]
        u.GetParent.restype = wintypes.HWND
        u.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        u.GetWindowLongW.restype = ctypes.c_long
        u.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
        u.SetWindowLongW.restype = ctypes.c_long
        u.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.COLORREF, ctypes.c_ubyte, wintypes.DWORD]
        u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, wintypes.UINT]

    def get_hwnd(self, widget) -> int:
        child = widget.winfo_id()
        return self.user32.GetParent(child) or child       # a Tk top-level's frame is the child's parent

    def get_ex_style(self, hwnd) -> int:
        return self.user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF

    def set_ex_style(self, hwnd, style: int):
        ct = self.ct
        self.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ct.c_long(style if style < 2 ** 31 else style - 2 ** 32).value)
        # SWP_NOSIZE | NOMOVE | NOZORDER | NOACTIVATE | FRAMECHANGED: makes Windows read the new style now
        self.user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020)

    def set_alpha(self, hwnd, opacity):
        self.user32.SetLayeredWindowAttributes(hwnd, 0, alpha_byte(opacity), LWA_ALPHA)

    def monitors(self) -> list:
        """[{x, y, w, h, primary}] for every connected monitor (work in screen pixels)."""
        ct, wt = self.ct, self.wt

        class MONITORINFO(ct.Structure):
            _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]

        out = []
        proc_t = ct.WINFUNCTYPE(ct.c_int, wt.HMONITOR, wt.HDC, ct.POINTER(wt.RECT), wt.LPARAM)

        def cb(hmon, _hdc, _rect, _lp):
            mi = MONITORINFO()
            mi.cbSize = ct.sizeof(MONITORINFO)
            if self.user32.GetMonitorInfoW(hmon, ct.byref(mi)):
                r = mi.rcMonitor
                out.append({"x": r.left, "y": r.top, "w": r.right - r.left, "h": r.bottom - r.top,
                            "primary": bool(mi.dwFlags & 1)})
            return 1
        self.user32.EnumDisplayMonitors(None, None, proc_t(cb), 0)
        return out


def check_available(platform: str | None = None, import_tk=None) -> tuple[bool, str]:
    """(True, "") when the overlay window can be made on this PC, else (False, why in plain words)."""
    if (platform or sys.platform) != "win32":
        return False, "The overlay needs Windows."
    try:
        (import_tk or (lambda: __import__("tkinter")))()
    except Exception:  # noqa: BLE001  (ImportError, or Tk's own libraries missing)
        return False, "This PC's Debrief has no Tk window library, so the overlay can't be drawn. The coach is not affected."
    return True, ""


# ---------------------------------------------------------------- the window
FAST_S, IDLE_S = 0.25, 1.0       # while the coach runs (4 times a second); while it is off (nothing is redrawn)
TOPMOST_EVERY = 8                # ticks between re-asserting "on top"


class OverlayWindow:
    """The Tk window. tk is the tkinter module (or a stand-in), win32 the Win32 shim, store the Store,
    get_status() the coach's status dict (or None), get_unit() the speed unit from Settings > Units."""

    def __init__(self, tk, win32, store: Store, get_status, get_unit, log=None):
        self.tk, self.win32, self.store = tk, win32, store
        self.get_status, self.get_unit = get_status, get_unit
        self.log = log or (lambda m: None)
        self.root = None
        self.mon = None
        self.geom = None
        self.edit = False
        self.last_vm = None
        self.ticks = 0
        self.stop_flag = False
        self._cmds = {}
        self._cmd_lock = threading.Lock()
        self._drag = None
        self._err_logged = False

    # -- built once, on the Tk thread
    def build(self):
        tk = self.tk
        self.root = r = tk.Tk()
        r.withdraw()
        r.overrideredirect(True)
        r.attributes("-topmost", True)
        r.title("Debrief coach overlay")
        mons = self.win32.monitors()
        self.mon = self.store.start_monitor(mons) or {"x": 0, "y": 0, "w": r.winfo_screenwidth(),
                                                      "h": r.winfo_screenheight(), "primary": True}
        self.geom = self.store.geometry(self.mon)
        L = lambda **kw: tk.Label(r, anchor="w", justify="left", bg=COLORS["bg"], fg=COLORS["text"], **kw)  # noqa: E731
        self.w_word, self.w_delta = L(text=""), L(text="")
        self.w_thing, self.w_count, self.w_cue, self.w_sub = L(text=""), L(text=""), L(text=""), L(text="")
        self.w_grip = tk.Label(r, text="◢", bg=COLORS["bg"], fg=COLORS["dim"])
        self.widgets = [self.w_word, self.w_delta, self.w_thing, self.w_count, self.w_cue, self.w_sub]
        self._layout()
        for w in [r] + self.widgets:
            w.bind("<ButtonPress-1>", self._press)
            w.bind("<B1-Motion>", self._motion)
            w.bind("<ButtonRelease-1>", self._release)
        self.w_grip.bind("<ButtonPress-1>", self._press_grip)
        self.w_grip.bind("<B1-Motion>", self._motion_grip)
        self.w_grip.bind("<ButtonRelease-1>", self._release)
        self._place()
        r.update()
        self._styles()
        r.deiconify()
        self._styles()      # once more once it is mapped: Windows can reset the style on show
        r.after(int(FAST_S * 1000), self.tick)

    def _layout(self):
        w = self.geom["w"]
        k = w / SIZES[DEFAULT_SIZE][0]
        f = lambda pt, bold=False: ("Segoe UI", max(7, int(round(pt * k))), "bold" if bold else "normal")  # noqa: E731
        self.w_word.configure(font=f(24, True))
        self.w_delta.configure(font=f(20, True), anchor="e")
        self.w_thing.configure(font=f(11), wraplength=w - 20)
        self.w_count.configure(font=f(11, True))
        self.w_cue.configure(font=f(10), wraplength=w - 20)
        self.w_sub.configure(font=f(9))
        pad = 10
        self.w_word.place(x=pad, y=int(4 * k), relwidth=0.62)
        self.w_delta.place(relx=0.55, y=int(8 * k), relwidth=0.45, width=-pad)
        self.w_thing.place(x=pad, y=int(54 * k), width=w - 2 * pad)
        self.w_count.place(x=pad, y=int(98 * k), width=w - 2 * pad)
        self.w_cue.place(x=pad, y=int(122 * k), width=w - 2 * pad)
        self.w_sub.place(x=pad, y=int(146 * k), width=w - 2 * pad)

    def _place(self):
        g = self.geom
        self.root.geometry(f"{g['w']}x{g['h']}{g['x']:+d}{g['y']:+d}")

    def _styles(self):
        hwnd = self.win32.get_hwnd(self.root)
        self.win32.set_ex_style(hwnd, ex_style(self.win32.get_ex_style(hwnd), click_through=not self.edit))
        self.win32.set_alpha(hwnd, self.geom["opacity"])

    # -- orders from other threads (the page's buttons): applied on the next tick
    def command(self, **kw):
        with self._cmd_lock:
            self._cmds.update(kw)

    def _apply_commands(self):
        with self._cmd_lock:
            cmds, self._cmds = self._cmds, {}
        if "edit" in cmds and bool(cmds["edit"]) != self.edit:
            self.edit = bool(cmds["edit"])
            self._edit_changed()
        if "opacity" in cmds or "size" in cmds:
            g = dict(self.geom)
            if "opacity" in cmds:
                g["opacity"] = cmds["opacity"]
            if cmds.get("size") in SIZES:
                g["w"], g["h"] = SIZES[cmds["size"]]
            self.geom = fit(g, self.mon)
            self.store.save_geometry(self.mon, **self.geom)
            self._layout()
            self._place()
            self._styles()
        if cmds.get("stop"):
            self.stop_flag = True

    def _edit_changed(self):
        r = self.root
        bg = COLORS["edit"] if self.edit else COLORS["bg"]
        r.configure(bg=bg)
        for w in self.widgets + [self.w_grip]:
            w.configure(bg=bg)
        if self.edit:
            self.w_grip.place(relx=1.0, rely=1.0, anchor="se")
        else:
            self.w_grip.place_forget()
            self._save_position()
        self.last_vm = None           # redraw
        self._styles()

    # -- dragging (Edit position only: otherwise the window is click-through and never gets these)
    def _press(self, e):
        if self.edit:
            self._drag = ("move", e.x_root - self.geom["x"], e.y_root - self.geom["y"])

    def _motion(self, e):
        if self.edit and self._drag and self._drag[0] == "move":
            self.geom["x"], self.geom["y"] = e.x_root - self._drag[1], e.y_root - self._drag[2]
            self._place()

    def _press_grip(self, e):
        if self.edit:
            self._drag = ("size", e.x_root - self.geom["w"], e.y_root - self.geom["h"])

    def _motion_grip(self, e):
        if self.edit and self._drag and self._drag[0] == "size":
            self.geom["w"], self.geom["h"] = e.x_root - self._drag[1], e.y_root - self._drag[2]
            self.geom = fit(self.geom, self.mon)
            self._layout()
            self._place()

    def _release(self, _e=None):
        if self.edit and self._drag:
            self._drag = None
            self._save_position()

    def _save_position(self):
        """Keep it on the monitor its middle is on, and remember this monitor's place, size and opacity."""
        try:
            g = self.geom
            mons = self.win32.monitors()
            mon = pick_monitor(mons, g["x"] + g["w"] // 2, g["y"] + g["h"] // 2) or self.mon
            if monitor_key(mon) != monitor_key(self.mon):
                self.mon = mon
                g = dict(self.store.geometry(mon), x=g["x"], y=g["y"], w=g["w"], h=g["h"])
            self.geom = fit(g, self.mon)
            self.store.save_geometry(self.mon, **self.geom)
            self._place()
        except Exception as e:  # noqa: BLE001  (an unsaved position is not worth stopping the overlay)
            self.log(f"overlay: position not saved ({type(e).__name__}: {e})")

    # -- the 4-times-a-second tick
    def render(self, vm: dict):
        self.w_word.configure(text=vm["word"], fg=COLORS[vm["tone"]])
        self.w_delta.configure(text=vm["delta"], fg=COLORS[vm["delta_tone"]] if vm["delta_tone"] != "dim" else COLORS["dim"])
        self.w_thing.configure(text=vm["thing"])
        self.w_count.configure(text=vm["count"])
        self.w_cue.configure(text=("Last cue: " + vm["cue"]) if vm["cue"] else "")
        self.w_sub.configure(text=vm["sub"])

    def interval(self) -> float:
        st = self.last_vm
        return IDLE_S if (st is None or st["state"] == "off") and not self.edit else FAST_S

    def tick(self):
        try:
            self._apply_commands()
            if self.stop_flag:
                self.root.quit()
                return
            vm = view_model(self.get_status(), self.get_unit(), edit=self.edit)
            if vm != self.last_vm:                      # nothing is redrawn when nothing changed
                self.last_vm = vm
                self.render(vm)
            self.ticks += 1
            if self.ticks % TOPMOST_EVERY == 0:
                self.root.attributes("-topmost", True)  # iRacing can take the top back
        except Exception as e:  # noqa: BLE001  (a bad tick never closes the overlay)
            if not self._err_logged:
                self._err_logged = True
                self.log(f"overlay: tick failed ({type(e).__name__}: {e})")
        try:
            self.root.after(int(self.interval() * 1000), self.tick)
        except Exception:  # noqa: BLE001  (the window is gone)
            pass

    def run(self):
        self.root.mainloop()

    def close(self):
        r, self.root = self.root, None
        if r is not None:
            try:
                r.destroy()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------- the controller the server holds
class Overlay:
    """start() / stop() / command() / status(). Nothing exists (no thread, no window, no Tk import) until start()."""

    def __init__(self, store: Store, get_status, get_unit, log=None, platform=None, tk_factory=None, win32_factory=None):
        self.store, self.get_status, self.get_unit = store, get_status, get_unit
        self.log = log or (lambda m: None)
        self._platform = platform
        self._tk_factory = tk_factory or (lambda: __import__("tkinter"))
        self._win32_factory = win32_factory or Win32
        self.window = None
        self.thread = None
        self.error = ""
        self.edit = False
        self._avail = None
        self._lock = threading.Lock()

    def available(self) -> tuple[bool, str]:
        if self._avail is None:                 # asked on every page poll: look once
            self._avail = check_available(self._platform, lambda: self._tk_factory())
        return self._avail

    def running(self) -> bool:
        t = self.thread
        return bool(t is not None and t.is_alive() and self.window is not None and self.window.root is not None)

    def start(self, wait_s: float = 3.0) -> dict:
        """Starts the window if it can be made. Returns status(); a failure is in status()["why"], never raised."""
        with self._lock:
            if self.running() or (self.thread is not None and self.thread.is_alive()):
                return self.status()
            ok, why = self.available()
            if not ok:
                self.error = why
                return self.status()
            self.error = ""
            self.edit = False
            ready = threading.Event()
            self.thread = threading.Thread(target=self._run, args=(ready,), name="coach-overlay", daemon=True)
            self.thread.start()
        ready.wait(wait_s)
        return self.status()

    def _run(self, ready: threading.Event):
        win = None
        try:
            win = OverlayWindow(self._tk_factory(), self._win32_factory(), self.store, self.get_status,
                                self.get_unit, self.log)
            win.build()
            win.edit = False
            self.window = win
            ready.set()
            win.run()
        except Exception as e:  # noqa: BLE001  (no Tk, no display, no Win32: the coach carries on)
            self.error = f"The overlay window couldn't be made on this PC ({type(e).__name__}). The coach is not affected."
            self.log(f"overlay: {type(e).__name__}: {str(e)[:200]}")
        finally:
            ready.set()
            if win is not None:
                win.close()
            self.window = None
            self.edit = False

    def stop(self, wait_s: float = 3.0):
        win, t = self.window, self.thread
        if win is not None:
            win.command(stop=True)
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(wait_s)
        self.thread = None if t is None or not t.is_alive() else t

    def command(self, **kw):
        if "edit" in kw:
            self.edit = bool(kw["edit"])
        win = self.window
        if win is not None:
            win.command(**kw)

    def status(self) -> dict:
        ok, why = self.available()
        win = self.window
        g = win.geom if win is not None and win.geom else None
        return {"enabled": self.store.enabled(), "running": self.running(), "edit": self.edit and self.running(),
                "available": ok, "why": self.error or why,
                "opacity": (g or {}).get("opacity", DEFAULT_OPACITY),
                "size": next((n for n, wh in SIZES.items() if g and (g["w"], g["h"]) == wh), "custom" if g else DEFAULT_SIZE)}
