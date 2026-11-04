"""2.19: the coach overlay.

    python3 tests/test_219_overlay.py

No display, no Tk and no Windows needed: the Tk module and the Win32 calls are stand-ins here.
  - the view model: every state (coaching, muted, rehearsing, off, waiting), the one thing and its count, the last
    cue, the delta, speeds in the unit from Settings > Units, the Edit position hint
  - the Win32 flags: click-through, no taskbar button, no focus; Edit position lets the mouse in
  - the saved choices: off by default, per monitor (position, size, opacity), kept on screen
  - the window: draws only when something changed, drags and resizes, saves where it was put
  - the controller: no thread or window until asked, why in words when Tk or Windows is missing, and the
    coach's route works with it
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from debrief import overlay as ov  # noqa: E402
from debrief.desktop import coach2  # noqa: E402

fails = 0


def check(ok, msg):
    global fails
    print(("ok   " if ok else "FAIL ") + msg)
    if not ok:
        fails += 1


# ---------------------------------------------------------------- (a) the view model
def status(**kw):
    base = {"running": True, "state": "coaching", "show": "coaching", "mode": "live", "muted": False,
            "focus": {"text": "Brake 5 km/h later into T4", "count_text": "Hit 3 of 7 so far"},
            "spoken": [{"t": 12.0, "text": "Down two tenths, T4"}, {"t": 80.0, "text": "Good, 84 km/h"}],
            "live_delta": 0.12, "last_lap": {"lap": 3, "time": 95.2, "delta": -0.4}, "mute": {}}
    base.update(kw)
    return base


v = ov.view_model(status(), "kmh")
check(v["state"] == "coaching" and v["word"] == "COACHING" and v["tone"] == "ok", f"coaching: {v['word']}")
check(v["thing"] == "Brake 5 km/h later into Turn 4", f"the one thing, T4 as Turn 4: {v['thing']}")
check(v["count"] == "Hit 3 of 7 so far", f"its count: {v['count']}")
check(v["cue"] == "Good, 84 km/h", f"the last cue is the newest: {v['cue']}")
check(v["delta"] == "+0.12 s" and v["delta_tone"] == "bad", f"slower than the reference is +: {v['delta']} {v['delta_tone']}")
vm = ov.view_model(status(live_delta=-0.31), "kmh")
check(vm["delta"] == "−0.31 s" and vm["delta_tone"] == "ok", f"quicker is a minus and ok: {vm['delta']}")
vm = ov.view_model(status(live_delta=0.0), "kmh")
check(vm["delta"] == "0.00 s" and vm["delta_tone"] == "dim", f"level: {vm['delta']}")
vm = ov.view_model(status(live_delta=None), "kmh")
check(vm["delta"] == "−0.40 s", f"no live delta: the last lap's: {vm['delta']}")
vm = ov.view_model(status(live_delta=None, last_lap=None), "kmh")
check(vm["delta"] == "", "no delta at all: blank")
vm = ov.view_model(status(live_delta="nan"), "kmh")
check(vm["delta"] == "−0.40 s", "a bad number is ignored")

# units: every speed in the coach's words follows Settings > Units
vm = ov.view_model(status(), "mph")
check(vm["cue"] == "Good, 52 mph" and "km/h" not in vm["thing"] and "3 mph" in vm["thing"], f"mph: {vm['cue']} / {vm['thing']}")
vm = ov.view_model(status(), "kmh")
check("84 km/h" in vm["cue"], f"km/h: {vm['cue']}")
check(ov.view_model(status())["cue"] == "Good, 52 mph", "mph is the default")

# muted wins, shows red, and says for how long
vm = ov.view_model(status(show="muted", muted=True, mute={"quiet_left": 12.2}), "mph")
check(vm["word"] == "MUTED" and vm["tone"] == "bad" and vm["sub"] == "Quiet for 13 s more", f"muted: {vm['word']} {vm['sub']}")
vm = ov.view_model(status(show="muted", muted=True, mute={"since": 1.0}), "mph")
check(vm["word"] == "MUTED" and vm["sub"] == "Unmute to hear cues", f"muted until unmuted: {vm['sub']}")
check(vm["count"] == "Hit 3 of 7 so far", "muted still shows the count")
# rehearsing
vm = ov.view_model(status(show="rehearsing", mode="rehearsal"), "mph")
check(vm["word"] == "REHEARSING" and vm["tone"] == "info", f"rehearsing: {vm['word']}")
# off: no coach, a stopped coach, an error
for label, s_ in (("no coach", None), ("stopped", {"running": False, "show": "coaching"}),
                  ("error", status(state="error", show="off"))):
    vm = ov.view_model(s_, "mph")
    check(vm["word"] == "OFF" and vm["tone"] == "off" and vm["thing"] == "" and vm["delta"] == "" and vm["cue"] == "",
          f"off ({label})")
check(ov.view_model(status(state="error", show="coaching"))["word"] == "OFF", "a coach in error shows OFF")
vm = ov.view_model(status(show="waiting", state="waiting"), "mph")
check(vm["word"] == "WAITING" and vm["state"] == "waiting", "waiting for iRacing")
check(ov.view_model(status(show="starting"))["word"] == "STARTING", "starting")
check(ov.view_model(status(show="odd"))["word"] == "COACHING", "an unknown state while running reads as coaching")
check(ov.view_model("junk")["word"] == "OFF", "junk status is OFF")
# no one thing / no cue yet
vm = ov.view_model(status(focus=None, spoken=[]), "mph")
check(vm["thing"] == ov.NO_THING and vm["cue"] == ov.NO_CUE and vm["count"] == "", "no focus, nothing said yet")
vm = ov.view_model(status(focus={"text": "X", "progress": "Counted when this lap is done"}), "mph")
check(vm["count"] == "Counted when this lap is done", "count falls back to progress")
# edit position
check(ov.view_model(None, "mph", edit=True)["sub"] == ov.EDIT_HINT, "Edit position shows its hint, coach off")
check(ov.view_model(status(), "mph", edit=True)["sub"] == ov.EDIT_HINT, "and while coaching")
check(ov.view_model(status(), "mph") == ov.view_model(status(), "mph"), "same status, same view (nothing to redraw)")

# ---------------------------------------------------------------- (b) the Win32 flags
base = 0x00040000 | 0x00000008      # WS_EX_APPWINDOW plus something else that must survive
ct = ov.ex_style(base, True)
for name, flag in (("layered", ov.WS_EX_LAYERED), ("transparent (click-through)", ov.WS_EX_TRANSPARENT),
                   ("toolwindow (no taskbar button)", ov.WS_EX_TOOLWINDOW), ("noactivate (no focus)", ov.WS_EX_NOACTIVATE)):
    check(ct & flag == flag, f"click-through style has {name}")
check(ct & ov.WS_EX_APPWINDOW == 0, "no taskbar button: APPWINDOW cleared")
check(ct & 0x8 == 0x8, "other styles are kept")
ed = ov.ex_style(ct, False)
check(ed & ov.WS_EX_TRANSPARENT == 0 and ed & ov.WS_EX_NOACTIVATE == 0 and ed & ov.WS_EX_LAYERED and ed & ov.WS_EX_TOOLWINDOW,
      "Edit position: mouse reaches it, can take a click, no taskbar button")
check(ov.ex_style(ed, True) == ct, "and back to click-through")
check(ov.WS_EX_LAYERED == 0x80000 and ov.WS_EX_TRANSPARENT == 0x20 and ov.WS_EX_TOOLWINDOW == 0x80
      and ov.WS_EX_NOACTIVATE == 0x08000000 and ov.GWL_EXSTYLE == -20, "the Win32 constants")
check(ov.alpha_byte(1.0) == 255 and ov.alpha_byte(0.5) == 128 and ov.alpha_byte(0.0) == int(round(0.3 * 255))
      and ov.alpha_byte(None) == int(round(0.85 * 255)) and ov.alpha_byte(9) == 255, "opacity as a byte, kept in range")

# ---------------------------------------------------------------- (c) saved choices, per monitor
class Mem:
    def __init__(self, d=None):
        self.d = d if d is not None else {}
        self.saves = 0

    def load(self):
        return self.d

    def save(self, d):
        self.d = d
        self.saves += 1


M1 = {"x": 0, "y": 0, "w": 1920, "h": 1080, "primary": True}
M2 = {"x": 1920, "y": 0, "w": 2560, "h": 1440, "primary": False}
M3 = {"x": -1280, "y": 100, "w": 1280, "h": 1024, "primary": False}
mem = Mem()
st = ov.Store(mem.load, mem.save)
check(st.enabled() is False, "off by default")
check(ov.Store(lambda: {"enabled": "yes"}, lambda d: None).enabled() is False, "only a real true turns it on")
check(ov.Store(lambda: 5, lambda d: None).enabled() is False and ov.Store(lambda: 5, lambda d: None).data()["monitors"] == {},
      "damaged settings read as the defaults")


def boom():
    raise OSError("gone")


check(ov.Store(boom, lambda d: None).enabled() is False, "unreadable settings read as off")
st.set_enabled(True)
check(st.enabled() is True and mem.d["enabled"] is True, "turned on")
st.set_enabled(False)
check(st.enabled() is False, "turned off")
g = st.geometry(M1)
check((g["w"], g["h"]) == ov.SIZES["medium"] and g["opacity"] == 0.85 and g["x"] == 1920 - g["w"] - ov.MARGIN and g["y"] == ov.MARGIN,
      f"default: medium, 85%, top right of the monitor: {g}")
st.save_geometry(M1, x=100, y=50, w=300, h=140, opacity=0.6)
st.save_geometry(M2, x=2000, y=400, w=480, h=226, opacity=1.0)
check(st.geometry(M1) == {"x": 100, "y": 50, "w": 300, "h": 140, "opacity": 0.6}, "monitor 1 remembered")
check(st.geometry(M2) == {"x": 2000, "y": 400, "w": 480, "h": 226, "opacity": 1.0}, "monitor 2 remembered separately")
check(st.geometry(M3)["opacity"] == 0.85 and st.geometry(M3)["x"] == -1280 + 1280 - 360 - ov.MARGIN,
      "a monitor never used gets the defaults")
check(ov.monitor_key(M2) == "2560x1440@1920,0", "monitor names")
check(st.start_monitor([M1, M2]) == M2, "opens on the monitor it was last on")
check(st.start_monitor([M1, M3]) == M1, "that monitor unplugged: the primary one")
check(st.start_monitor([]) is None, "no monitors known: none")
# survives a reload, and the saved file is plain data
st2 = ov.Store(mem.load, mem.save)
check(st2.geometry(M1)["x"] == 100 and st2.data()["last_monitor"] == ov.monitor_key(M2), "reloaded from the saved data")
# kept on screen, sizes in range
st.save_geometry(M1, x=99999, y=-500, w=5000, h=5)
gg = st.geometry(M1)
check(0 <= gg["x"] <= 1920 - gg["w"] and gg["y"] == 0 and gg["w"] == ov.MAX_W and gg["h"] == ov.MIN_H, f"kept on screen and in range: {gg}")
check(ov.fit({"opacity": 9}, M1)["opacity"] == 1.0 and ov.fit({"opacity": 0.01}, M1)["opacity"] == ov.MIN_OPACITY, "opacity range")
check(ov.pick_monitor([M1, M2], 2000, 10) == M2 and ov.pick_monitor([M1, M2], -5, -5) == M1, "which monitor holds a point")

# ---------------------------------------------------------------- stand-ins for Tk and Win32
class FWidget:
    def __init__(self, parent=None, **kw):
        self.kw = dict(kw)
        self.placed = False
        self.bound = {}

    def configure(self, **kw):
        self.kw.update(kw)

    config = configure

    def place(self, **kw):
        self.placed = True
        self.place_kw = kw

    def place_forget(self):
        self.placed = False

    def bind(self, ev, fn):
        self.bound[ev] = fn


class FRoot(FWidget):
    instances = []

    def __init__(self):
        super().__init__()
        self.queue = []
        self.quit_flag = False
        self.destroyed = False
        self.geom = None
        self.attrs = {}
        self.visible = False
        self.after_calls = 0
        self.redraws = 0
        FRoot.instances.append(self)

    def withdraw(self):
        self.visible = False

    def deiconify(self):
        self.visible = True

    def overrideredirect(self, v):
        self.borderless = v

    def attributes(self, k, v):
        self.attrs[k] = v

    def title(self, t):
        self.title_ = t

    def geometry(self, g):
        self.geom = g

    def update(self):
        pass

    def winfo_id(self):
        return 77

    def winfo_screenwidth(self):
        return 1920

    def winfo_screenheight(self):
        return 1080

    def after(self, ms, fn):
        self.after_calls += 1
        self.queue.append((ms, fn))

    def quit(self):
        self.quit_flag = True

    def destroy(self):
        self.destroyed = True

    def run_ticks(self, n):
        for _ in range(n):
            if not self.queue or self.quit_flag:
                break
            _ms, fn = self.queue.pop(0)
            fn()

    def mainloop(self):
        while not self.quit_flag:
            if self.queue:
                _ms, fn = self.queue.pop(0)
                fn()
            time.sleep(0.002)


class FLabel(FWidget):
    def __init__(self, parent=None, **kw):
        super().__init__(parent, **kw)
        FLabel.count += 1


FLabel.count = 0
FTk = SimpleNamespace(Tk=FRoot, Label=FLabel)


class FWin32:
    def __init__(self, monitors=None):
        self.mons = monitors if monitors is not None else [dict(M1), dict(M2)]
        self.ex = 0x00040000
        self.history = []
        self.alpha = None

    def monitors(self):
        return self.mons

    def get_hwnd(self, w):
        return 4242

    def get_ex_style(self, h):
        return self.ex

    def set_ex_style(self, h, s):
        self.ex = s
        self.history.append(s)

    def set_alpha(self, h, o):
        self.alpha = o


def event(x, y):
    return SimpleNamespace(x_root=x, y_root=y, x=0, y=0)


def make_window(status_box, unit_box=None, win=None, mem_=None):
    FRoot.instances.clear()
    mem_ = mem_ or Mem()
    store = ov.Store(mem_.load, mem_.save)
    w32 = win or FWin32()
    unit_box = unit_box or ["mph"]
    wnd = ov.OverlayWindow(FTk, w32, store, lambda: status_box[0], lambda: unit_box[0])
    wnd.build()
    return wnd, w32, store, mem_


# ---------------------------------------------------------------- (d) the window
box = [None]
wnd, w32, store, mem_ = make_window(box)
root = wnd.root
check(root.borderless is True and root.attrs.get("-topmost") is True and root.visible, "borderless, on top, shown")
check(w32.ex & ov.CLICK_THROUGH_FLAGS == ov.CLICK_THROUGH_FLAGS and w32.ex & ov.WS_EX_APPWINDOW == 0,
      "built click-through, no taskbar button, no focus")
check(abs(w32.alpha - 0.85) < 1e-9, "opacity applied")
check(root.geom == "360x170+1536+24", f"top right of the primary monitor: {root.geom}")
check(wnd.last_vm is None, "nothing drawn before the first tick")
wnd.tick()
check(wnd.last_vm["word"] == "OFF" and wnd.w_word.kw["text"] == "OFF", "first tick: OFF")
check(wnd.interval() == ov.IDLE_S, "coach off: checks once a second")
n = root.after_calls
configs = {id(x): dict(x.kw) for x in wnd.widgets}
wnd.tick()
wnd.tick()
check(all(x.kw == configs[id(x)] for x in wnd.widgets), "nothing changed: nothing redrawn")
box[0] = status()
wnd.tick()
check(wnd.w_word.kw["text"] == "COACHING" and wnd.w_word.kw["fg"] == ov.COLORS["ok"], "coaching drawn")
check(wnd.w_delta.kw["text"] == "+0.12 s" and wnd.w_delta.kw["fg"] == ov.COLORS["bad"], "delta drawn, red when slower")
check(wnd.w_cue.kw["text"].startswith("Last cue: Good, 52 mph"), f"cue in mph: {wnd.w_cue.kw['text']}")
check(wnd.w_count.kw["text"] == "Hit 3 of 7 so far", "count drawn")
check(wnd.interval() == ov.FAST_S, "coach running: 4 times a second")
check(root.after_calls > n, "re-scheduled itself")
box[0] = status(show="muted", muted=True)
wnd.tick()
check(wnd.w_word.kw["text"] == "MUTED" and wnd.w_word.kw["fg"] == ov.COLORS["bad"], "muted drawn in red")

def bad_status():
    raise RuntimeError("status broke")


logs = []
wnd.get_status = bad_status
wnd.log = logs.append
q = len(root.queue)
wnd.tick()
wnd.tick()
check(len(logs) == 1 and len(root.queue) == q + 2, "a failing tick is logged once and the overlay keeps going")
wnd.get_status = lambda: box[0]

# Edit position: mouse reaches it, drag moves it, saved per monitor
wnd.command(edit=True)
wnd._drain()
wnd.tick()
check(wnd.edit and w32.ex & ov.WS_EX_TRANSPARENT == 0 and w32.ex & ov.WS_EX_NOACTIVATE == 0, "Edit position: not click-through, can take a click")
check(wnd.w_grip.placed and wnd.w_sub.kw["text"] == ov.EDIT_HINT, "grip and hint shown")
check(wnd.root.kw.get("bg") == ov.COLORS["edit"], "edit colour")
wnd.w_word.bound["<ButtonPress-1>"](event(1600, 60))
wnd.w_word.bound["<B1-Motion>"](event(1000, 300))
check(root.geom == "360x170+936+264", f"dragged: {root.geom}")
wnd.w_word.bound["<ButtonRelease-1>"](event(1000, 300))
check(store.geometry(M1)["x"] == 936 and store.geometry(M1)["y"] == 264, "position saved for monitor 1")
wnd.w_grip.bound["<ButtonPress-1>"](event(1296, 434))
wnd.w_grip.bound["<B1-Motion>"](event(1396, 474))
check(root.geom == "460x210+936+264", f"resized from the corner: {root.geom}")
wnd.w_grip.bound["<ButtonRelease-1>"](event(1396, 474))
check((store.geometry(M1)["w"], store.geometry(M1)["h"]) == (460, 210), "size saved")
# dragged to the second monitor: remembered for that one
wnd.w_word.bound["<ButtonPress-1>"](event(1000, 300))
wnd.w_word.bound["<B1-Motion>"](event(2500, 300))
wnd.w_word.bound["<ButtonRelease-1>"](event(2500, 300))
check(ov.monitor_key(wnd.mon) == ov.monitor_key(M2) and store.data()["last_monitor"] == ov.monitor_key(M2),
      "the middle is on monitor 2: it is saved there")
check(store.geometry(M1)["x"] == 936, "monitor 1 keeps its own place")
# a drag when not editing does nothing
wnd.command(edit=False)
wnd._drain()
check(not wnd.edit and w32.ex & ov.WS_EX_TRANSPARENT and not wnd.w_grip.placed, "Edit position off: click-through again, grip gone")
before = root.geom
wnd.w_word.bound["<ButtonPress-1>"](event(10, 10))
wnd.w_word.bound["<B1-Motion>"](event(50, 50))
check(root.geom == before, "never moves while click-through")
# size and opacity
wnd.command(size="large", opacity=0.5)
wnd._drain()
check((wnd.geom["w"], wnd.geom["h"]) == ov.SIZES["large"] and wnd.geom["opacity"] == 0.5 and abs(w32.alpha - 0.5) < 1e-9,
      "size and opacity applied")
check(store.geometry(wnd.mon)["opacity"] == 0.5 and store.geometry(wnd.mon)["w"] == 480, "and remembered for this monitor")
wnd.command(stop=True)
wnd._drain()
check(root.quit_flag, "stop quits the loop")
wnd.close()
check(root.destroyed and wnd.root is None, "closed")

# reopening puts it back where this monitor had it
wnd2, w32b, store2, _m = make_window([None], mem_=mem_)
check(ov.monitor_key(wnd2.mon) == ov.monitor_key(M2) and wnd2.geom["opacity"] == 0.5 and wnd2.geom["w"] == 480,
      f"reopened on its last monitor with its size and opacity: {wnd2.root.geom}")
# monitor unplugged
wnd3, _w, _s, _m = make_window([None], win=FWin32([dict(M1)]), mem_=mem_)
check(ov.monitor_key(wnd3.mon) == ov.monitor_key(M1), "that monitor gone: opens on the primary one")

# ---------------------------------------------------------------- (e) the controller
# no Tk / not Windows: says why, never raises
def no_tk():
    raise ImportError("No module named 'tkinter'")


c = ov.Overlay(ov.Store(Mem().load, Mem().save), lambda: None, lambda: "mph", platform="win32", tk_factory=no_tk)
s_ = c.start()
check(not s_["available"] and "Tk" in s_["why"] and not s_["running"] and c.thread is None, f"no Tk: {s_['why']}")
c2 = ov.Overlay(ov.Store(Mem().load, Mem().save), lambda: None, lambda: "mph", platform="linux", tk_factory=lambda: FTk)
s_ = c2.start()
check(not s_["available"] and s_["why"] == "The overlay needs Windows." and c2.thread is None, f"not Windows: {s_['why']}")
c2.command(edit=True)
c2.stop()
check(not c2.status()["edit"], "stop and commands on an overlay that never started are harmless")
real = ov.check_available()
check(real[0] is False or sys.platform == "win32", f"this PC: {real}")

# a window that fails in its thread: the reason is kept, nothing raised
class BrokenTk:
    @staticmethod
    def Tk():
        raise RuntimeError("no display")

lg = []
c3 = ov.Overlay(ov.Store(Mem().load, Mem().save), lambda: None, lambda: "mph", log=lg.append, platform="win32",
                tk_factory=lambda: BrokenTk, win32_factory=FWin32)
s_ = c3.start()
check(not s_["running"] and "RuntimeError: no display" in s_["why"] and "The coach carries on." in s_["problem"]
      and s_["state"] == "failed", f"window failed: {s_['problem']}")
check(lg and "RuntimeError" in lg[0] and "Traceback" in lg[0], "and logged with its traceback")
# Win32 shim missing too
c3b = ov.Overlay(ov.Store(Mem().load, Mem().save), lambda: None, lambda: "mph", platform="win32",
                 tk_factory=lambda: FTk, win32_factory=lambda: (_ for _ in ()).throw(OSError("no windll")))
s_ = c3b.start()
check(not s_["running"] and "OSError" in s_["why"] and s_["problem"].startswith("The overlay couldn't open: "), "no Win32 calls: same")

# a real run on a thread with the stand-ins
FRoot.instances.clear()
live = [None]
m4 = Mem()
c4 = ov.Overlay(ov.Store(m4.load, m4.save), lambda: live[0], lambda: "mph", platform="win32", tk_factory=lambda: FTk,
                win32_factory=FWin32)
check(c4.thread is None and c4.window is None and FRoot.instances == [], "before start: no thread, no window, no Tk")
s_ = c4.start()
check(s_["running"] and s_["available"] and s_["why"] == "" and s_["enabled"] is False, f"started: {s_}")
check(c4.thread.name == "coach-overlay" and c4.thread.daemon, "its own daemon thread")
live[0] = status()
deadline = time.time() + 5
while time.time() < deadline and c4.window.w_word.kw.get("text") != "COACHING":
    time.sleep(0.02)
check(c4.window.w_word.kw.get("text") == "COACHING", "it follows the coach's status")
c4.command(edit=True)
deadline = time.time() + 5
while time.time() < deadline and not c4.window.edit:
    time.sleep(0.02)
check(c4.status()["edit"] and c4.status()["size"] == "medium", "Edit position on")
t4 = c4.thread
c4.stop()
check(not c4.running() and c4.window is None and not c4.status()["edit"], "stopped: off at once")
t4.join(5)
check(not t4.is_alive() and FRoot.instances[0].destroyed, "its window was destroyed by its own thread")
# stop works again, and it can restart
s_ = c4.start()
check(s_["running"], "restarts")
c4.stop()

# ---------------------------------------------------------------- (f) the coach's route
class H:
    def __init__(self, server):
        self.server = server
        self.out = None

    def _json(self, obj, code=200):
        self.out = obj
        return obj


def route(ctl, body):
    srv = SimpleNamespace(_overlay=ctl, _overlay_autostarted=True, coach_lock=threading.Lock(), coach=None,
                          stopping=threading.Event())
    h = H(srv)
    try:
        coach2.post_overlay(h, body)
        return h.out
    except ValueError as e:
        return {"ok": False, "error": str(e)}


m5 = Mem()
c5 = ov.Overlay(ov.Store(m5.load, m5.save), lambda: None, lambda: "mph", platform="win32", tk_factory=lambda: FTk,
                win32_factory=FWin32)
check("api/overlay/set" in coach2.POST_ROUTES, "the route is registered")
r = route(c5, {"edit": True})
check(r["ok"] is False and "Show the overlay first" in r["error"], f"edit before show: {r}")
r = route(c5, {"opacity": 0.5})
check(r["ok"] is False, "opacity before show")
for bad in ({}, {"enabled": "yes"}, {"bogus": 1}, {"enabled": True, "x": 1}, {"edit": 1}):
    check(route(c5, bad)["ok"] is False, f"bad body {bad}")
r = route(c5, {"enabled": True})
check(r["ok"] and r["overlay"]["running"] and r["overlay"]["enabled"] is True and m5.d["enabled"] is True,
      "Show overlay: window up, choice saved")
r = route(c5, {"edit": True})
check(r["ok"] and r["overlay"]["edit"] is True, "Edit position on")
for bad in ({"opacity": 2}, {"opacity": 0.1}, {"opacity": True}, {"size": "huge"}):
    check(route(c5, bad)["ok"] is False, f"bad value {bad}")
check(route(c5, {"size": "small"})["ok"] and route(c5, {"opacity": 0.7})["ok"], "size and opacity accepted")
deadline = time.time() + 5
while time.time() < deadline and c5.status()["size"] != "small":
    time.sleep(0.02)
check(c5.status()["size"] == "small" and c5.status()["opacity"] == 0.7, f"applied: {c5.status()}")
r = route(c5, {"enabled": False})
check(r["ok"] and not r["overlay"]["running"] and not r["overlay"]["edit"] and m5.d["enabled"] is False, "Show overlay off")
# unavailable: the page gets the reason, the setting stays off
m6 = Mem()
c6 = ov.Overlay(ov.Store(m6.load, m6.save), lambda: None, lambda: "mph", platform="linux", tk_factory=lambda: FTk)
r = route(c6, {"enabled": True})
check(r["ok"] and not r["overlay"]["running"] and r["overlay"]["why"] and not m6.d.get("enabled"),
      f"without a window the page says why and the setting stays off: {r['overlay']['why']}")
# the coach's status carries it
srv = SimpleNamespace(_overlay=c6, _overlay_autostarted=False, coach_lock=threading.Lock(), coach=None,
                      stopping=threading.Event())
o = coach2.overlay_status(srv)
check(o["running"] is False and o["enabled"] is False and "why" in o, "coach/status carries the overlay's state")
check(srv._overlay_autostarted is True, "autostart is tried once")
# autostart when left on
m7 = Mem({"enabled": True})
c7 = ov.Overlay(ov.Store(m7.load, m7.save), lambda: None, lambda: "mph", platform="win32", tk_factory=lambda: FTk,
                win32_factory=FWin32)
srv7 = SimpleNamespace(_overlay=c7, _overlay_autostarted=False, coach_lock=threading.Lock(), coach=None,
                       stopping=threading.Event())
coach2.overlay_autostart(srv7)
deadline = time.time() + 5
while time.time() < deadline and not c7.running():
    time.sleep(0.02)
check(c7.running(), "left on: it comes back with Debrief")
c7.stop()
check(not c7.running(), "and stops with it")
# the status source is the coach's own status
fake_runner = SimpleNamespace(status=lambda: {"running": True, "show": "coaching"}, running=lambda: True)
srv8 = SimpleNamespace(coach=fake_runner, coach_lock=threading.Lock())
check(coach2._overlay_status_source(srv8) == {"running": True, "show": "coaching"}, "reads the coach's one status")
check(coach2._overlay_status_source(SimpleNamespace(coach=None, coach_lock=threading.Lock())) is None, "no coach: None")

# ---------------------------------------------------------------- (h) 2.19.1: hardening
def wait_for(fn, secs=5.0):
    end = time.time() + secs
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.01)
    return fn()


def overlay_threads():
    return [t for t in threading.enumerate() if t.name == "coach-overlay" and t.is_alive()]


# -- one thread for Tk: every Tk call, from every command the server can send, is made on the overlay's own thread
CALLS = []


class Tracked:
    def __getattribute__(self, n):
        v = object.__getattribute__(self, n)
        if callable(v) and not n.startswith("__"):
            CALLS.append((n, threading.get_ident()))
        return v


class TRoot(Tracked, FRoot):
    pass


class TLabel(Tracked, FLabel):
    pass


TTk = SimpleNamespace(Tk=TRoot, Label=TLabel)
mt = Mem()
ct_ = ov.Overlay(ov.Store(mt.load, mt.save), lambda: status(), lambda: "mph", platform="win32", tk_factory=lambda: TTk,
                 win32_factory=FWin32)
ct_.start()
tk_ident = ct_.thread.ident
main_ident = threading.get_ident()
for kw in ({"edit": True}, {"size": "large"}, {"opacity": 0.6}, {"preset": "bottom-left"}, {"nudge": "up", "big": True},
           {"reset": True}, {"edit": False}, {"test": 30}):
    ct_.command(**kw)
    ct_.status()
    ct_.running()
ct_.command(edit=False)
check(wait_for(lambda: not ct_.window.edit), "commands from the server are applied")
ct_.stop()
check(wait_for(lambda: not overlay_threads()), "and it closes")
check(len(CALLS) > 50 and all(i == tk_ident for _n, i in CALLS) and main_ident != tk_ident,
      f"all {len(CALLS)} Tk calls (show/hide, edit, size, opacity, position, test) came from the overlay's own thread")

# the queue is emptied only on the Tk thread
wq, _w, _s, _m = make_window([None])
wq.command(edit=True)
other = threading.Thread(target=wq._drain)
other.start()
other.join()
check(not wq.edit and not wq._q.empty(), "drained from another thread: nothing happens, the order waits")
wq._drain()
check(wq.edit and wq._q.empty(), "drained on the Tk thread: applied")
check(ov.PUMP_MS <= 250, "the Tk thread looks for orders at least every quarter second")

# -- a failure stays in the overlay: logged with its traceback, one plain line, the coach carries on
class CrashRoot(FRoot):
    def mainloop(self):
        raise RuntimeError("tcl exploded")


lg2 = []
cc = ov.Overlay(ov.Store(Mem().load, Mem().save), lambda: None, lambda: "mph", log=lg2.append, platform="win32",
                tk_factory=lambda: SimpleNamespace(Tk=CrashRoot, Label=FLabel), win32_factory=FWin32)
s_ = cc.start()
wait_for(lambda: not overlay_threads())
s_ = cc.status()
check(s_["state"] == "failed" and not s_["running"] and s_["problem"] == "The overlay couldn't open: RuntimeError: tcl exploded. The coach carries on.",
      f"a crash in the overlay's thread: {s_['problem']}")
check(lg2 and "Traceback" in lg2[0] and "tcl exploded" in lg2[0], "its traceback is in the log")
check(s_["line"].startswith("Couldn't start: RuntimeError"), f"status line: {s_['line']}")
# a bad Tk callback is logged, not printed to a missing stderr
wcb, _w, _s, _m = make_window([None])
cb_logs = []
wcb.log = cb_logs.append
try:
    raise ValueError("in a button")
except ValueError:
    import sys as _sys
    wcb.root.report_callback_exception(*_sys.exc_info())
check(cb_logs and "in a button" in cb_logs[0], "a window callback error goes to the log")
# the coach never waits for the overlay: autostart with a slow or missing Tk, stop with a stuck window
def slow_no_tk():
    time.sleep(1.0)
    raise ImportError("no tkinter")


cs = ov.Overlay(ov.Store(Mem({"enabled": True}).load, Mem().save), lambda: None, lambda: "mph", platform="win32",
                tk_factory=slow_no_tk)
srv_s = SimpleNamespace(_overlay=cs, _overlay_autostarted=False, coach_lock=threading.Lock(), coach=None,
                        stopping=threading.Event())
t0 = time.time()
coach2.overlay_autostart(srv_s)
check(time.time() - t0 < 0.5, f"autostart with a slow Tk library returns at once ({time.time() - t0:.2f} s)")
check(wait_for(lambda: "Tk" in cs.status()["why"], 5), "and the reason arrives: " + cs.status()["why"][:60])


class StuckRoot(FRoot):
    def mainloop(self):
        time.sleep(1.5)          # ignores quit


cst = ov.Overlay(ov.Store(Mem().load, Mem().save), lambda: None, lambda: "mph", platform="win32",
                 tk_factory=lambda: SimpleNamespace(Tk=StuckRoot, Label=FLabel), win32_factory=FWin32)
cst.start()
t0 = time.time()
cst.stop()
on_shutdown_srv = SimpleNamespace(_overlay=cst, coach_lock=threading.Lock(), coach=None, stopping=threading.Event())
check(time.time() - t0 < 0.5 and not cst.running(), "stopping never waits for a window that is stuck")


class RaisingOverlay:
    store = SimpleNamespace(enabled=lambda: True)

    def start(self, wait_s=0):
        raise RuntimeError("start blew up")

    def status(self):
        return {}


srv_r = SimpleNamespace(_overlay=RaisingOverlay(), _overlay_autostarted=False, coach_lock=threading.Lock(), coach=None,
                        stopping=threading.Event())
coach2.overlay_autostart(srv_r)
check(srv_r._overlay_autostarted is True, "an overlay that raises on autostart does not raise at the server")

# -- Edit position, on the window itself
wE, wE32, stE, memE = make_window([None])
rE = wE.root
wE.command(edit=True)
wE._drain()
check(wE32.ex & ov.WS_EX_TRANSPARENT == 0 and wE32.ex & ov.WS_EX_NOACTIVATE == 0 and wE32.ex & ov.WS_EX_LAYERED
      and wE32.ex & ov.WS_EX_TOOLWINDOW, "editing: TRANSPARENT and NOACTIVATE removed, layered tool window kept")
check(rE.kw.get("highlightthickness") == 3 and rE.kw.get("highlightbackground") == ov.EDIT_BORDER, "a clear border")
check(wE.w_bar.placed and wE.w_done.placed and "Drag to move" in wE.w_bar.kw["text"] and wE.w_done.kw["text"] == "Done",
      "the bar reads Drag to move, then Done")
wE.w_thing.bound["<ButtonPress-1>"](event(1600, 100))      # drag by any part of the box
wE.w_thing.bound["<B1-Motion>"](event(1500, 400))
check(rE.geom == "360x170+1436+324", f"dragged by the text: {rE.geom}")
wE.w_bar.bound["<ButtonRelease-1>"](event(1500, 400))
wE.w_done.bound["<ButtonRelease-1>"](event(0, 0))
check(not wE.edit and wE32.ex & ov.WS_EX_TRANSPARENT and wE32.ex & ov.WS_EX_NOACTIVATE
      and rE.kw.get("highlightthickness") == 0 and not wE.w_bar.placed, "Done on the overlay locks it: click-through again")
check(stE.geometry(M1)["x"] == 1436 and stE.geometry(M1)["y"] == 324, "and the place is saved")
# the controller hears Done pressed on the overlay itself
mD = Mem()
cD = ov.Overlay(ov.Store(mD.load, mD.save), lambda: None, lambda: "mph", platform="win32", tk_factory=lambda: FTk,
                win32_factory=FWin32)
cD.start()
cD.command(edit=True)
check(cD.status()["edit"] is True, "Edit position shows as on at once")
check(wait_for(lambda: cD.window.edit), "and the window follows within a moment")
cD.window.w_done.bound["<ButtonRelease-1>"](event(0, 0))
check(cD.status()["edit"] is False, "Done on the overlay turns it off on the Coach page too")
cD.stop()

# -- placement: presets, nudges, reset
g0 = {"x": 0, "y": 0, "w": 360, "h": 170}
want = {"top-left": (24, 24), "top-center": (780, 24), "top-right": (1536, 24), "middle-left": (24, 455),
        "center": (780, 455), "middle-right": (1536, 455), "bottom-left": (24, 886), "bottom-center": (780, 886),
        "bottom-right": (1536, 886)}
check(set(ov.PRESETS) == set(want) and len(ov.PRESETS) == 9, "nine spots")
check(all(ov.preset_xy(n, g0, M1) == xy for n, xy in want.items()), "preset spots on a 1920x1080 monitor")
check(ov.preset_xy("top-left", g0, M2) == (1944, 24) and ov.preset_xy("bottom-right", g0, M2) == (1920 + 2560 - 360 - 24, 1440 - 170 - 24),
      "and on a monitor that is not at 0,0")
try:
    ov.preset_xy("nowhere", g0, M1)
    check(False, "an unknown spot is refused")
except ValueError:
    check(True, "an unknown spot is refused")
gg = {"x": 100, "y": 100, "w": 360, "h": 170}
check(ov.nudge_xy(gg, "right", False, M1) == (110, 100) and ov.nudge_xy(gg, "left", False, M1) == (90, 100)
      and ov.nudge_xy(gg, "up", False, M1) == (100, 90) and ov.nudge_xy(gg, "down", False, M1) == (100, 110), "nudge: 10 px")
check(ov.nudge_xy(gg, "right", True, M1) == (150, 100) and ov.nudge_xy(gg, "down", True, M1) == (100, 150), "Shift: 50 px")
check(ov.nudge_xy(dict(gg, x=3), "left", True, M1) == (0, 100) and ov.nudge_xy(dict(gg, y=3), "up", False, M1) == (100, 0)
      and ov.nudge_xy(dict(gg, x=1560), "right", True, M1) == (1560, 100), "a nudge stops at the monitor's edge")
wP, wP32, stP, _m = make_window([None])
wP.command(preset="bottom-left")
wP._drain()
check(wP.root.geom == "360x170+24+886" and stP.geometry(M1)["x"] == 24 and stP.geometry(M1)["y"] == 886,
      f"preset applied and saved (not in Edit position): {wP.root.geom}")
wP.command(nudge="right", big=True)
wP._drain()
check(wP.root.geom == "360x170+74+886", f"nudge: {wP.root.geom}")
wP.command(reset=True)
wP._drain()
check(wP.root.geom == "360x170+1536+24" and stP.geometry(M1)["x"] == 1536, f"reset: back to the top right: {wP.root.geom}")
check(not wP.edit, "placing never needs Edit position")

# -- never lost off-screen
M4 = {"x": 0, "y": 0, "w": 1280, "h": 720, "primary": True}
gc, mc = ov.clamp_to_monitors({"x": 5000, "y": 5000, "w": 360, "h": 170}, [M1, M2])
check(mc == M1 and gc["x"] == 1920 - 360 and gc["y"] == 1080 - 170, f"far off the screen: back on the primary monitor {gc}")
gc, mc = ov.clamp_to_monitors({"x": -1200, "y": 100, "w": 360, "h": 170}, [M1])
check(mc == M1 and gc["x"] == 0 and gc["y"] == 100, "left of the only monitor: pulled back")
gc, mc = ov.clamp_to_monitors({"x": 4300, "y": 200, "w": 360, "h": 170}, [M1, M2])
check(mc == M2 and gc["x"] == 1920 + 2560 - 360, "mostly on monitor 2: kept there, fully inside")
gc, mc = ov.clamp_to_monitors({"x": 1800, "y": 1000, "w": 360, "h": 170}, [M4])
check(mc == M4 and 0 <= gc["x"] <= 1280 - 360 and 0 <= gc["y"] <= 720 - 170, "monitor became smaller: inside it")
check(ov.clamp_to_monitors({"x": 5, "y": 5, "w": 360, "h": 170}, [])[1] is None, "no monitor known: left alone")
check(ov.valid_monitors([{"x": 0, "y": 0, "w": 0, "h": 0}, {"bad": 1}, M1]) == [M1], "broken monitor answers are ignored")
# saved for a monitor that has since been unplugged
mG = Mem({"last_monitor": ov.monitor_key(M3), "monitors": {ov.monitor_key(M3): {"x": -1000, "y": 900, "w": 400, "h": 200, "opacity": 0.9},
                                                          ov.monitor_key(M2): {"x": 4000, "y": 1300, "w": 480, "h": 226}}})
wG, _w, _s, _m = make_window([None], win=FWin32([dict(M1)]), mem_=mG)
gx = wG.geom
check(0 <= gx["x"] and gx["x"] + gx["w"] <= 1920 and 0 <= gx["y"] and gx["y"] + gx["h"] <= 1080,
      f"its monitor is gone: it opens fully on the one that is there {gx}")
wG2, _w, _s, _m = make_window([None], win=FWin32([]), mem_=Mem())
check(wG2.geom["x"] >= 0 and wG2.root.geom, "Windows listing no monitor: falls back to the screen size")
wG3, _w, _s, _m = make_window([None], win=FWin32([dict(M3)]), mem_=mG)
check(M3["x"] <= wG3.geom["x"] and wG3.geom["x"] + wG3.geom["w"] <= M3["x"] + M3["w"] and wG3.geom["y"] + wG3.geom["h"] <= M3["y"] + M3["h"],
      "a monitor to the left of the main one (negative x): inside it")

# -- on, off, on, off
FRoot.instances.clear()
mR = Mem()
cR = ov.Overlay(ov.Store(mR.load, mR.save), lambda: None, lambda: "mph", platform="win32", tk_factory=lambda: FTk,
                win32_factory=FWin32)
ok_all = True
for i in range(6):
    s_ = cR.start()
    ok_all &= bool(s_["running"])
    cR.command(edit=(i % 2 == 0))
    cR.stop()                         # straight away, without waiting for the window to close
    ok_all &= not cR.running()
check(ok_all, "six times on and straight off again")
check(wait_for(lambda: not overlay_threads()) and all(r.destroyed for r in FRoot.instances) and len(FRoot.instances) == 6,
      "every window was closed by its own thread, none left behind")
for i in range(3):
    r = route(cR, {"enabled": True})
    ok_all &= bool(r["ok"] and r["overlay"]["running"])
    r = route(cR, {"enabled": False})
    ok_all &= bool(r["ok"] and not r["overlay"]["running"])
check(ok_all, "and through the Coach page's own switch")
check(route(cR, {"enabled": True})["overlay"]["running"], "and on again")
on_shutdown_srv2 = SimpleNamespace(_overlay=cR, coach_lock=threading.Lock(), coach=None, stopping=threading.Event(),
                                   _wheels={})
try:
    coach2.on_shutdown(on_shutdown_srv2)
except Exception:  # noqa: BLE001  (the wheel parts need a real server; only the overlay matters here)
    pass
check(not cR.running() and wait_for(lambda: not overlay_threads()), "closing Debrief closes the overlay")

# -- diagnostics
cI = ov.Overlay(ov.Store(Mem().load, Mem().save), lambda: None, lambda: "mph", platform="win32", tk_factory=lambda: FTk,
                win32_factory=FWin32)
si = cI.status()
check(si["state"] == "off" and si["line"] == "Off." and si["hwnd"] == 0 and si["problem"] == "", f"off: {si['line']}")
si = cI.start()
check(si["state"] == "running" and si["hwnd"] == 4242 and si["line"] == "Running. Window found (handle 4242).", f"running: {si['line']}")
cI.stop()
cN = ov.Overlay(ov.Store(Mem().load, Mem().save), lambda: None, lambda: "mph", platform="linux", tk_factory=lambda: FTk)
si = cN.start()
check(si["state"] == "failed" and si["line"] == "Couldn't start: The overlay needs Windows." and si["problem"].endswith("The coach carries on."),
      f"couldn't start: {si['line']}")

# -- the 15 second test
mT = Mem()
cT = ov.Overlay(ov.Store(mT.load, mT.save), lambda: None, lambda: "mph", platform="win32", tk_factory=lambda: FTk,
                win32_factory=FWin32)
cT.test(0.4, close_after=True)
check(wait_for(lambda: cT.window is not None and cT.window.edit and "Turn 4" in cT.window.w_thing.kw.get("text", "")),
      "the test opens in Edit position with sample text (no iRacing)")
check(cT.status()["edit"] is True, "and the page shows Edit position")
check(wait_for(lambda: not cT.running(), 5) and not overlay_threads(), "and closes itself when the time is up")
cT.stop()
cK = ov.Overlay(ov.Store(Mem().load, Mem().save), lambda: None, lambda: "mph", platform="win32", tk_factory=lambda: FTk,
                win32_factory=FWin32)
cK.start()
cK.test(0.3, close_after=False)
check(wait_for(lambda: cK.window is not None and cK.window.edit), "test on an overlay that is already shown: Edit position")
check(wait_for(lambda: not cK.window.edit) and cK.running(), "then it locks and stays")
cK.stop()
cU = ov.Overlay(ov.Store(Mem().load, Mem().save), lambda: None, lambda: "mph", platform="win32", tk_factory=lambda: FTk,
                win32_factory=FWin32)
out_lines = []
check(ov.run_test(0.3, out=out_lines.append, ctl=cU) == 0 and any("Running" in x for x in out_lines),
      "python -m debrief overlay-test opens it, says what it found, and returns")
out_lines = []
check(ov.run_test(0.3, out=out_lines.append, ctl=cN) == 1 and any("Windows" in x for x in out_lines), "and says why when it can't")
app_src = (ROOT / "debrief" / "app.py").read_text(encoding="utf-8")
check('"overlay-test"' in app_src and "run_test" in app_src, "overlay-test is a command")

# -- the new buttons on the route
cB = ov.Overlay(ov.Store(Mem().load, Mem().save), lambda: None, lambda: "mph", platform="win32", tk_factory=lambda: FTk,
                win32_factory=FWin32)
for body in ({"preset": "top-left"}, {"nudge": "up"}, {"reset": True}):
    check(route(cB, body)["ok"] is False, f"{list(body)[0]} before Show overlay is refused politely")
route(cB, {"enabled": True})
check(route(cB, {"preset": "center"})["ok"] and route(cB, {"nudge": "left", "big": True})["ok"] and route(cB, {"reset": True})["ok"],
      "preset, nudge (Shift) and reset are accepted")
for bad in ({"preset": "mid"}, {"nudge": "diagonal"}, {"big": True}, {"nudge": "up", "big": "yes"}, {"reset": 1}, {"test": "yes"}):
    check(route(cB, bad)["ok"] is False, f"bad body {bad}")
check(wait_for(lambda: cB.window.geom["x"] == 1536 and cB.window.geom["y"] == 24), "reset put it back at the top right")
r = route(cB, {"test": True})
check(r["ok"] and r["overlay"]["edit"] is True, "Test the overlay from the page")
route(cB, {"enabled": False})
cB2 = ov.Overlay(ov.Store(Mem().load, Mem().save), lambda: None, lambda: "mph", platform="win32", tk_factory=lambda: FTk,
                 win32_factory=FWin32)
r = route(cB2, {"test": True})
check(r["ok"] and r["overlay"]["running"] and cB2.store.enabled() is False, "Test works with the overlay off, and does not turn it on")
cB2.stop()
r = route(cN, {"test": True})
check(r["ok"] is False and "couldn't open" in r["error"], f"Test on a PC that can't: the reason, not a crash: {r['error'][:70]}")

# ---------------------------------------------------------------- (g) the build and the page
spec = (ROOT / "packaging" / "debrief.spec").read_text(encoding="utf-8")
excl = spec.split("excludes = [", 1)[1].split("]", 1)[0]
check("tkinter" not in excl and "_tkinter" not in excl, "the build no longer excludes Tk")
check('"tkinter"' in spec.split("hiddenimports = sorted", 1)[1].split("for _must", 1)[0], "and names tkinter as an import")
html = (ROOT / "debrief" / "desktop" / "ui" / "index.html").read_text(encoding="utf-8")
js = (ROOT / "debrief" / "desktop" / "ui" / "app.js").read_text(encoding="utf-8")
check('id="ovShow"' in html and 'id="ovEdit"' in html and "borderless windowed" in html, "Coach page: Show overlay, Edit position, borderless note")
check('id="ovShow" type="checkbox" checked' not in html, "not ticked by default in the page")
check("overlay/set" in js and "ovRender(st.overlay)" in js and "exclusive fullscreen" in js, "page script and Help wording")
import re  # noqa: E402
src = (ROOT / "debrief" / "overlay.py").read_text(encoding="utf-8")
top = src.split("\nclass ", 1)[0]
check(not re.search(r"^(import|from) (tkinter|ctypes)", src, re.M), "tkinter and ctypes are never imported at module level")
code = src.split("from __future__ import annotations", 1)[1]
check("windll" not in code.split("class Win32")[0], "windll only inside Win32")

# the page
check('id="ovStatus"' in html and html.count("data-ovp=") == 9 and "data-ovn=" in html and 'id="ovReset"' in html
      and 'id="ovTest"' in html, "Coach page: status line, nine spots, nudge arrows, Reset position, Test")
check("Overlay status: " in js and "e.shiftKey" in js and "ovSet({reset:true})" in js, "page script: status, Shift nudge, reset")

print(f"\n{'FAILED ' + str(fails) if fails else 'ALL OK'}")
sys.exit(1 if fails else 0)
