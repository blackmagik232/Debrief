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
import queue
import re
import sys
import threading
import time
import traceback

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


FUEL_KINDS = ("box_now", "box_soon", "caution_box", "fuel_critical", "fuel_tight", "save_or_box")


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
    thing = count = cue = delta = sub = crew = room = ""
    delta_tone = crew_tone = "dim"
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
        rm = st.get("room") if isinstance(st.get("room"), dict) else {}
        cr = st.get("crew") if isinstance(st.get("crew"), dict) else {}
        if cr.get("enabled") and cr.get("overlay") and cr.get("state") in ("racing", "lost"):
            # 2.29: one line, "Fuel 13.4 laps · Box L17-18 · Plan +1" (the call itself when it is urgent)
            call = cr.get("call") if isinstance(cr.get("call"), dict) else None
            crew = ("DATA LOST" if cr.get("state") == "lost" else str(cr["overlay"]))
            if call and call.get("rank", 99) <= 4 and cr.get("state") != "lost":
                crew = str(call.get("text") or crew)[:60]
            if cr.get("fuel_estimated") and cr.get("state") != "lost":
                # 2.30: a crew PC's fuel is always an estimate. The flag comes from the crew chief (no guessing from the words);
                # only a call that is about fuel or the stop carries it, a car alongside does not
                if not (call and call.get("rank", 99) <= 4) or call.get("kind") in FUEL_KINDS:
                    crew += " (est.)"
            crew_tone = "bad" if cr.get("state") == "lost" or (call and call.get("rank", 99) <= 3) else "dim"
        if rm.get("enabled") and rm.get("overlay") and rm.get("state") == "racing":
            room = str(rm["overlay"])[:80]       # 2.30 Race Room: "Stint 41:02 · next Ben 02:15 · BOX L29 (est.)"
        if state == "muted":
            m = st.get("mute") if isinstance(st.get("mute"), dict) else {}
            ql = _num(m.get("quiet_left"))
            sub = f"Quiet for {int(math.ceil(ql))} s more" if ql is not None and ql > 0 else "Unmute to hear cues"
    vm = {"state": state, "word": word, "tone": tone, "thing": thing, "count": count, "cue": cue, "delta": delta,
          "delta_tone": delta_tone, "sub": sub, "crew": crew, "crew_tone": crew_tone, "room": room if not sub else ""}
    if edit:
        vm["sub"] = EDIT_HINT
        vm["room"] = ""
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
EDIT_FLAGS = WS_EX_LAYERED | WS_EX_TOOLWINDOW       # Edit position: no TRANSPARENT and no NOACTIVATE


def ex_style(current: int, click_through: bool) -> int:
    """The window's extended style with the overlay's flags set. click_through=False (Edit position) keeps the
    window layered and out of the taskbar, but removes WS_EX_TRANSPARENT (so the mouse reaches it) and
    WS_EX_NOACTIVATE (so a click on it is a real click and a drag works)."""
    s = (int(current) | EDIT_FLAGS) & ~WS_EX_APPWINDOW
    if click_through:
        return s | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE
    return s & ~WS_EX_TRANSPARENT & ~WS_EX_NOACTIVATE


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


def valid_monitors(monitors) -> list:
    """Only monitors with a real size (a broken answer from Windows is ignored)."""
    out = []
    for m in monitors or []:
        try:
            if int(m["w"]) > 0 and int(m["h"]) > 0:
                out.append({"x": int(m["x"]), "y": int(m["y"]), "w": int(m["w"]), "h": int(m["h"]),
                            "primary": bool(m.get("primary"))})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def best_monitor(monitors: list, g: dict) -> dict | None:
    """The monitor the box overlaps most; if it is on none of them (lost off-screen), the primary one."""
    best, area = None, 0
    gx, gy = _num(g.get("x")), _num(g.get("y"))
    gw = _num(g.get("w")) or SIZES[DEFAULT_SIZE][0]
    gh = _num(g.get("h")) or SIZES[DEFAULT_SIZE][1]
    if gx is not None and gy is not None:
        for m in monitors:
            w = min(gx + gw, m["x"] + m["w"]) - max(gx, m["x"])
            h = min(gy + gh, m["y"] + m["h"]) - max(gy, m["y"])
            if w > 0 and h > 0 and w * h > area:
                best, area = m, w * h
    return best or pick_monitor(monitors, -10 ** 9, -10 ** 9)


def clamp_to_monitors(g: dict, monitors: list) -> tuple[dict, dict | None]:
    """(box fully on a connected monitor, that monitor). A saved spot on a monitor that is gone, or beyond the
    edge of one, comes back on the nearest monitor. With no monitor known the box is returned as it was."""
    mons = valid_monitors(monitors)
    mon = best_monitor(mons, g)
    return (fit(g, mon), mon) if mon else (dict(g), None)


PRESETS = ("top-left", "top-center", "top-right", "middle-left", "center", "middle-right",
           "bottom-left", "bottom-center", "bottom-right")
NUDGE_SMALL, NUDGE_BIG = 10, 50
DIRECTIONS = {"left": (-1, 0), "right": (1, 0), "up": (0, -1), "down": (0, 1)}


def preset_xy(name: str, g: dict, mon: dict) -> tuple[int, int]:
    """Top-left corner of a box of g's size at one of the nine spots on the monitor, MARGIN from its edges."""
    if name not in PRESETS:
        raise ValueError("Unknown spot.")
    v, h = name.split("-") if "-" in name else ("middle", "center")
    xs = {"left": mon["x"] + MARGIN, "center": mon["x"] + (mon["w"] - g["w"]) // 2,
          "right": mon["x"] + mon["w"] - g["w"] - MARGIN}
    ys = {"top": mon["y"] + MARGIN, "middle": mon["y"] + (mon["h"] - g["h"]) // 2,
          "bottom": mon["y"] + mon["h"] - g["h"] - MARGIN}
    if name == "center":
        v, h = "middle", "center"
    return int(xs[h]), int(ys[v])


def nudge_xy(g: dict, direction: str, big: bool, mon: dict) -> tuple[int, int]:
    """Moves 10 px (50 with Shift) and stays on the monitor."""
    if direction not in DIRECTIONS:
        raise ValueError("Unknown direction.")
    step = NUDGE_BIG if big else NUDGE_SMALL
    dx, dy = DIRECTIONS[direction]
    moved = fit(dict(g, x=g["x"] + dx * step, y=g["y"] + dy * step), mon)
    return moved["x"], moved["y"]


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


OFF_WINDOWS = "The overlay works on Windows only; the coach still speaks."


def next_step(platform: str | None, reason: str) -> str:
    """2.19.2: what to do when the overlay can't open, in plain words ("" when nothing is wrong)."""
    if not reason:
        return ""
    if (platform or sys.platform) != "win32":
        return OFF_WINDOWS
    if "Tk" in reason:
        return ("Debrief's Tk window library is missing on this PC, so the overlay can't be drawn. Install Debrief "
                "again with its installer (it brings Tk with it). The coach carries on.")
    return ("Check that iRacing runs in borderless windowed mode (exclusive fullscreen hides every overlay), then turn "
            "Show overlay on again. If it still won't open, close and reopen Debrief; debrief.log has the details. "
            "The coach carries on.")


# ---------------------------------------------------------------- the window
FAST_S, IDLE_S = 0.25, 1.0       # while the coach runs (4 times a second); while it is off (nothing is redrawn)
PUMP_MS = 80                     # how often the Tk thread looks for orders from the page (well under a quarter second)
TOPMOST_EVERY = 8                # ticks between re-asserting "on top"
TEST_SECONDS = 15
EDIT_BORDER = "#5aa9ff"
SAMPLE_STATUS = {"running": True, "state": "coaching", "show": "coaching", "mode": "live",
                 "focus": {"text": "Brake a little later into T4", "count_text": "Hit 3 of 7 so far"},
                 "spoken": [{"t": 1.0, "text": "Good, T4 was a tenth quicker"}], "live_delta": -0.12,
                 "crew": {"enabled": True, "state": "racing", "overlay": "Fuel 13.4 laps · Box L17-18 · Plan +1", "call": None}}


class OverlayWindow:
    """The Tk window. tk is the tkinter module (or a stand-in), win32 the Win32 shim, store the Store,
    get_status() the coach's status dict (or None), get_unit() the speed unit from Settings > Units.

    One rule: every Tk call is made on the thread that built the window (the overlay's own thread). Other
    threads only call command(), which puts an order on a queue; the Tk thread empties the queue every PUMP_MS
    from an after() callback."""

    def __init__(self, tk, win32, store: Store, get_status, get_unit, log=None):
        self.tk, self.win32, self.store = tk, win32, store
        self.get_status, self.get_unit = get_status, get_unit
        self.log = log or (lambda m: None)
        self.root = None
        self.mon = None
        self.geom = None
        self.edit = False
        self.hwnd = 0
        self.last_vm = None
        self.ticks = 0
        self.stop_flag = False
        self.tk_thread = None
        self.test_until = None
        self.test_close = False
        self._q = queue.Queue()
        self._drag = None
        self._err_logged = False
        self.on_edit_changed = None          # the controller hears when Done is pressed on the overlay itself

    # -- built once, on the Tk thread
    def build(self):
        tk = self.tk
        self.tk_thread = threading.get_ident()
        self.root = r = tk.Tk()
        r.withdraw()
        try:       # a Tk callback error is written to Debrief's log (a windowed build has no stderr to print to)
            r.report_callback_exception = self._callback_error
        except Exception:  # noqa: BLE001
            pass
        r.overrideredirect(True)
        r.attributes("-topmost", True)
        r.title("Debrief coach overlay")
        mons = valid_monitors(self.win32.monitors())
        self.mon = self.store.start_monitor(mons) or {"x": 0, "y": 0, "w": r.winfo_screenwidth(),
                                                      "h": r.winfo_screenheight(), "primary": True}
        self.geom, self.mon = self._clamped(self.store.geometry(self.mon), mons)
        L = lambda **kw: tk.Label(r, anchor="w", justify="left", bg=COLORS["bg"], fg=COLORS["text"], **kw)  # noqa: E731
        self.w_word, self.w_delta = L(text=""), L(text="")
        self.w_thing, self.w_count, self.w_cue, self.w_sub = L(text=""), L(text=""), L(text=""), L(text="")
        self.w_crew = L(text="")
        self.w_room = L(text="")
        self.w_grip = tk.Label(r, text="◢", bg=COLORS["bg"], fg=COLORS["dim"])
        # the bar shown only while editing: "Drag to move · Done"
        self.w_bar = tk.Label(r, text="Drag to move  ·  ", bg=EDIT_BORDER, fg="#0b1220", anchor="w")
        self.w_done = tk.Label(r, text="Done", bg="#ffffff", fg="#0b1220", cursor="hand2")
        self.widgets = [self.w_word, self.w_delta, self.w_thing, self.w_count, self.w_cue, self.w_sub, self.w_crew, self.w_room]
        self._layout()
        for w in [r] + self.widgets + [self.w_bar]:
            w.bind("<ButtonPress-1>", self._press)
            w.bind("<B1-Motion>", self._motion)
            w.bind("<ButtonRelease-1>", self._release)
        self.w_grip.bind("<ButtonPress-1>", self._press_grip)
        self.w_grip.bind("<B1-Motion>", self._motion_grip)
        self.w_grip.bind("<ButtonRelease-1>", self._release)
        self.w_done.bind("<ButtonRelease-1>", self._done_clicked)
        self._place()
        r.update()
        self._styles()
        r.deiconify()
        self._styles()      # once more once it is mapped: Windows can reset the style on show
        r.after(PUMP_MS, self._pump)
        r.after(int(FAST_S * 1000), self.tick)

    def _clamped(self, g, mons):
        """g on a connected monitor (see clamp_to_monitors); (g, monitor)."""
        mons = valid_monitors(mons)
        if not mons:
            return fit(g, self.mon), self.mon
        g2, mon = clamp_to_monitors(g, mons)
        return g2, mon

    def _callback_error(self, exc, val, tb):
        self.log("overlay: a window callback failed:\n" + "".join(traceback.format_exception(exc, val, tb)))

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
        self.w_crew.configure(font=f(10, True))
        self.w_room.configure(font=f(9, True))
        self.w_bar.configure(font=("Segoe UI", 9, "bold"))
        self.w_done.configure(font=("Segoe UI", 9, "bold"))
        pad = 10
        oy = 22 if self.edit else 0          # the Edit position bar sits above everything
        self.w_word.place(x=pad, y=oy + int(4 * k), relwidth=0.62)
        self.w_delta.place(relx=0.55, y=oy + int(8 * k), relwidth=0.45, width=-pad)
        self.w_thing.place(x=pad, y=oy + int(54 * k), width=w - 2 * pad)
        self.w_count.place(x=pad, y=oy + int(98 * k), width=w - 2 * pad)
        self.w_cue.place(x=pad, y=oy + int(120 * k), width=w - 2 * pad)
        self.w_crew.place(x=pad, y=oy + int(134 * k), width=w - 2 * pad)      # 2.29 crew chief: fuel laps · window · plan
        self.w_sub.place(x=pad, y=oy + int(150 * k), width=w - 2 * pad)
        self.w_room.place(x=pad, y=oy + int(150 * k), width=w - 2 * pad)      # 2.30 Race Room: stint · next driver · box (shares the sub line)
        if self.edit:
            self._place_bar()

    def _place_bar(self):
        self.w_bar.place(x=0, y=0, relwidth=1.0, height=22)
        self.w_done.place(relx=1.0, x=-6, y=2, anchor="ne", height=18)
        self.w_grip.place(relx=1.0, rely=1.0, anchor="se")

    def _place(self):
        g = self.geom
        self.root.geometry(f"{g['w']}x{g['h']}{g['x']:+d}{g['y']:+d}")

    def _styles(self):
        hwnd = self.win32.get_hwnd(self.root)
        self.hwnd = int(hwnd or 0)
        self.win32.set_ex_style(hwnd, ex_style(self.win32.get_ex_style(hwnd), click_through=not self.edit))
        self.win32.set_alpha(hwnd, self.geom["opacity"])

    # -- orders from other threads (the page's buttons): a queue, emptied on the Tk thread
    def command(self, **kw):
        """Safe from any thread: only puts the order on the queue."""
        self._q.put(kw)

    def _pump(self):
        try:
            self._drain()
        except Exception:  # noqa: BLE001
            self._fail_once("an order failed")
        try:
            if not self.stop_flag:
                self.root.after(PUMP_MS, self._pump)
        except Exception:  # noqa: BLE001  (the window is gone)
            pass

    def _drain(self):
        if threading.get_ident() != self.tk_thread:      # never touch Tk from another thread
            return
        cmds = {}
        while True:
            try:
                cmds.update(self._q.get_nowait())
            except queue.Empty:
                break
        if cmds:
            self._apply(cmds)
        if self.test_until is not None and time.monotonic() >= self.test_until:
            self._end_test()

    def _apply(self, cmds):
        if cmds.get("stop"):
            self.stop_flag = True
            self.root.quit()
            return
        if cmds.get("test"):
            self.test_until = time.monotonic() + float(cmds["test"])
            self.test_close = bool(cmds.get("test_close"))
            cmds["edit"] = True
            self.last_vm = None
        if cmds.get("edit") is False and self.test_until is not None:
            cmds.pop("edit")
            self._end_test()                 # Done on the page during the test ends it
        if "edit" in cmds:
            self._set_edit(bool(cmds["edit"]))
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
        if cmds.get("preset"):
            x, y = preset_xy(cmds["preset"], self.geom, self.mon)
            self._move_to(x, y)
        if cmds.get("nudge"):
            x, y = nudge_xy(self.geom, cmds["nudge"], bool(cmds.get("big")), self.mon)
            self._move_to(x, y)
        if cmds.get("reset"):
            self._move_to(None, None)

    def _move_to(self, x, y):
        """Put the box at x, y (None, None = the default spot, top right), keep it on a monitor, remember it."""
        g = dict(self.geom, x=x, y=y)
        if x is None:
            g = fit(g, self.mon)
        self.geom, self.mon = self._clamped(g, self.win32.monitors())
        self.store.save_geometry(self.mon, **self.geom)
        self._layout()
        self._place()

    def _set_edit(self, on: bool):
        if on == self.edit:
            return
        self.edit = on
        r = self.root
        bg = COLORS["edit"] if on else COLORS["bg"]
        r.configure(bg=bg, highlightbackground=EDIT_BORDER, highlightcolor=EDIT_BORDER,
                    highlightthickness=3 if on else 0)
        for w in self.widgets + [self.w_grip]:
            w.configure(bg=bg)
        if on:
            self._layout()
        else:
            self._layout()
            self.w_grip.place_forget()
            self.w_bar.place_forget()
            self.w_done.place_forget()
            self._drag = None
            self.test_until = None
            self._save_position()
        self.last_vm = None           # redraw
        self._styles()
        if self.on_edit_changed:
            try:
                self.on_edit_changed(on)
            except Exception:  # noqa: BLE001
                pass

    def _end_test(self):
        self.test_until = None
        if self.test_close:
            self.stop_flag = True
            self.root.quit()
        else:
            self._set_edit(False)

    def _done_clicked(self, _e=None):
        if self.test_until is not None:
            self._end_test()
        elif self.edit:
            self._set_edit(False)

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
            mons = valid_monitors(self.win32.monitors())
            mon = pick_monitor(mons, g["x"] + g["w"] // 2, g["y"] + g["h"] // 2) or self.mon
            if monitor_key(mon) != monitor_key(self.mon):
                self.mon = mon
                g = dict(self.store.geometry(mon), x=g["x"], y=g["y"], w=g["w"], h=g["h"])
            self.geom, self.mon = self._clamped(g, mons or [self.mon])
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
        self.w_room.configure(text=vm.get("room", ""))
        self.w_crew.configure(text=vm.get("crew", ""), fg=COLORS["bad"] if vm.get("crew_tone") == "bad" else COLORS["dim"])

    def interval(self) -> float:
        st = self.last_vm
        return IDLE_S if (st is None or st["state"] == "off") and not self.edit else FAST_S

    def _fail_once(self, what):
        if not self._err_logged:
            self._err_logged = True
            self.log(f"overlay: {what}:\n{traceback.format_exc()}")

    def tick(self):
        try:
            if self.stop_flag:
                self.root.quit()
                return
            status = SAMPLE_STATUS if self.test_until is not None else self.get_status()
            vm = view_model(status, self.get_unit(), edit=self.edit)
            if vm != self.last_vm:                      # nothing is redrawn when nothing changed
                self.last_vm = vm
                self.render(vm)
            self.ticks += 1
            if self.ticks % TOPMOST_EVERY == 0:
                self.root.attributes("-topmost", True)  # iRacing can take the top back
        except Exception:  # noqa: BLE001  (a bad tick never closes the overlay)
            self._fail_once("tick failed")
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
    """start() / stop() / command() / status(). Nothing exists (no thread, no window, no Tk import) until start().
    Nothing here waits on the window except a short, optional wait in start() for the page's own button."""

    def __init__(self, store: Store, get_status, get_unit, log=None, platform=None, tk_factory=None, win32_factory=None):
        self.store, self.get_status, self.get_unit = store, get_status, get_unit
        self.log = log or (lambda m: None)
        self._platform = platform
        self._tk_factory = tk_factory or (lambda: __import__("tkinter"))
        self._win32_factory = win32_factory or Win32
        self.window = None
        self.thread = None
        self.error = ""                  # the reason it could not open or stopped, in plain words ("" = none)
        self.hwnd = 0
        self.edit = False                # Edit position as asked for (the window follows within PUMP_MS)
        self._avail = None
        self._lock = threading.Lock()

    def available(self) -> tuple[bool, str]:
        if self._avail is None:                 # asked on every page poll: look once
            self._avail = check_available(self._platform, lambda: self._tk_factory())
        return self._avail

    def running(self) -> bool:
        t, w = self.thread, self.window
        return bool(t is not None and t.is_alive() and w is not None and w.root is not None)

    def start(self, wait_s: float = 2.0) -> dict:
        """Starts the window if it can be made. Returns status(); a failure is in status()["why"], never raised."""
        try:
            with self._lock:
                if self.running() or self.thread is not None and self.thread.is_alive() and self.window is not None:
                    return self.status()
                ok, why = self.available()
                if not ok:
                    self.error = why
                    return self.status()
                self.error = ""
                self.hwnd = 0
                ready = threading.Event()
                t = threading.Thread(target=self._run, args=(ready,), name="coach-overlay", daemon=True)
                self.thread = t
                t.start()
            if wait_s and wait_s > 0:
                ready.wait(wait_s)
        except Exception:  # noqa: BLE001  (never raised at the coach)
            self.error = "The overlay couldn't be started."
            self.log("overlay: start failed:\n" + traceback.format_exc())
        return self.status()

    def _run(self, ready: threading.Event):
        win = None
        try:
            win = OverlayWindow(self._tk_factory(), self._win32_factory(), self.store, self.get_status,
                                self.get_unit, self.log)
            win.on_edit_changed = lambda on: setattr(self, "edit", bool(on))     # Done pressed on the overlay itself
            self.window = win               # set first so stop() can reach a window that is still opening
            win.build()
            self.hwnd = win.hwnd
            ready.set()
            win.run()
        except Exception as e:  # noqa: BLE001  (no Tk, no display, no Win32: the coach carries on)
            self.error = f"{type(e).__name__}: {str(e)[:160]}"
            self.log("overlay: the window failed:\n" + traceback.format_exc())
        finally:
            ready.set()
            if win is not None:
                try:
                    win.close()
                except Exception:  # noqa: BLE001
                    pass
            if self.window is win:
                self.window = None
                self.edit = False

    def stop(self, wait_s: float = 0.0):
        """Asks the window to close. Returns at once (wait_s=0); the Tk thread closes it itself."""
        win, t = self.window, self.thread
        self.window = None                 # off right away: a new start() makes a fresh window
        self.edit = False
        if win is not None:
            win.command(stop=True)
        if t is not None and wait_s and t.is_alive() and t is not threading.current_thread():
            t.join(wait_s)
        if self.thread is t:
            self.thread = None

    def command(self, **kw):
        win = self.window
        if "edit" in kw:
            self.edit = bool(kw["edit"]) and win is not None
        if kw.get("test"):
            self.edit = True
        if win is not None:
            win.command(**kw)

    def test(self, seconds: float = TEST_SECONDS, close_after: bool = False) -> dict:
        """Edit position with sample text for `seconds` seconds, then it locks (or closes, if it was only for the test)."""
        st = self.start()
        if self.running():
            self.command(test=seconds, test_close=close_after)
        return st

    def status(self) -> dict:
        ok, why = self.available()
        win = self.window
        g = win.geom if win is not None and win.geom else None
        running = self.running()
        reason = self.error or why
        state = "running" if running else ("failed" if reason else "off")
        hwnd = int(getattr(win, "hwnd", 0) or 0) if running else 0
        if running:
            line = "Running. Window found (handle %d)." % hwnd if hwnd else "Running, but its window handle was not found."
        elif reason:
            line = f"Couldn't start: {reason}"
        else:
            line = "Off."
        plat = getattr(self, "_platform", None)
        step = next_step(plat, reason) if not running else ""
        problem = (OFF_WINDOWS if step == OFF_WINDOWS else f"The overlay couldn't open: {reason}. {step}") if step else ""
        return {"enabled": self.store.enabled(), "running": running, "usable": bool(ok or running), "next_step": step,
                 "edit": bool(running and self.edit),
                "available": ok, "why": self.error or why, "state": state, "hwnd": hwnd, "line": line,
                "problem": problem,
                "opacity": (g or {}).get("opacity", DEFAULT_OPACITY),
                "size": next((n for n, wh in SIZES.items() if g and (g["w"], g["h"]) == wh), "custom" if g else DEFAULT_SIZE)}


def run_test(seconds: float = TEST_SECONDS, out=print, store=None, ctl=None) -> int:
    """python -m debrief overlay-test: the overlay for `seconds` seconds in Edit position with sample text, no
    iRacing needed. Prints what it found. Returns 0 when it opened."""
    if ctl is None:
        from .desktop import settings_io
        store = store or Store(lambda: settings_io.load_extra("overlay"), lambda d: settings_io.save_extra("overlay", d))
        ctl = Overlay(store, lambda: None, settings_io.units, log=out)
    st = ctl.test(seconds, close_after=True)
    out(f"Overlay: {st['line']}")
    if not ctl.running():
        out(st["problem"] or "The overlay did not open.")
        return 1
    out(f"Open for {int(seconds)} seconds. Drag it, then press Done on it. It saves where you leave it.")
    t = ctl.thread
    if t is not None:
        t.join(seconds + 5)
    return 0
