"""2.14: mute the coach from a wheel button (wheelmute.py), with a fake joystick backend. Run: python tests/test_214_wheel.py

No real joystick is touched: the backend is a stand-in that returns device lists and button bitmasks the test sets."""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from debrief import wheelmute as wm  # noqa: E402

fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


class FakeBackend:
    """devices: {id: {"name", "mid", "pid"}}; masks: {id: bitmask}. Counts reads."""

    def __init__(self):
        self.devs = {0: {"name": "Pedals", "mid": 100, "pid": 1}, 2: {"name": "Wheel", "mid": 200, "pid": 7}}
        self.masks = {0: 0, 2: 0}
        self.reads = 0
        self.fail = False
        self.lock = threading.Lock()

    def devices(self):
        if self.fail:
            raise OSError("driver hiccup")
        with self.lock:
            return [dict(v, id=k, buttons=32) for k, v in sorted(self.devs.items())]

    def buttons(self, dev):
        self.reads += 1
        if self.fail:
            raise OSError("driver hiccup")
        with self.lock:
            return self.masks.get(dev) if dev in self.devs else None

    def press(self, dev, button, down=True):
        with self.lock:
            bit = 1 << (button - 1)
            self.masks[dev] = (self.masks.get(dev, 0) | bit) if down else (self.masks.get(dev, 0) & ~bit)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def tick(self, s=0.05):
        self.t += s


def poll(w, clock, n=1, s=0.05):
    for _ in range(n):
        clock.tick(s)
        w.poll_once()


print("== learn a button")
be, clk = FakeBackend(), Clock()
presses, learned = [], []
w = wm.WheelMute(on_press=lambda: presses.append(1), backend=be, clock=clk)
check(w.status()["state"] == "off" and w.binding is None, "nothing set yet")
w.running = lambda: True                              # polled by hand here: status as if the thread were running
be.press(0, 3)                                       # a pedal button held down before learning starts
w._learn = {"cb": learned.append, "until": clk() + 15, "base": None}   # learn without the thread (poll by hand)
poll(w, clk, 3)
check(learned == [] and w.status()["state"] == "learning", "a button already held doesn't count")
be.press(2, 5)
poll(w, clk)
check(len(learned) == 1 and learned[0]["device"] == 2 and learned[0]["button"] == 5, "learned button 5 on device 2")
check(learned[0]["name"] == "Wheel" and learned[0]["mid"] == 200 and learned[0]["label"] == "Button 5 on Wheel",
      "the device's name and numbers are kept")
check(presses == [], "the learning press doesn't toggle mute")
poll(w, clk, 3)
check(presses == [], "holding it after learning: still no toggle")

print("== press edge")
be.press(2, 5, False)
poll(w, clk)
be.press(2, 5)
poll(w, clk)
check(presses == [1], "press: one toggle")
poll(w, clk, 10)
check(presses == [1], "held down: no repeat")
be.press(2, 5, False)
poll(w, clk, 10)
be.press(2, 5)
poll(w, clk)
check(len(presses) == 2, "pressed again later: second toggle")
be.press(2, 5, False)
poll(w, clk)
be.press(2, 5)
poll(w, clk)
check(len(presses) == 2, "pressed again within 0.3 s of the last press: ignored (switch bounce)")
be.press(2, 4)
be.press(2, 5, False)
poll(w, clk, 5)
check(len(presses) == 2, "another button: nothing")
be.press(0, 5)
poll(w, clk, 5)
check(len(presses) == 2, "the same button number on another device: nothing")

print("== devices change number")
be.devs = {0: {"name": "Wheel", "mid": 200, "pid": 7}, 1: {"name": "Pedals", "mid": 100, "pid": 1}}
be.masks = {0: 0, 1: 1 << 4}
poll(w, clk, int(wm.RESCAN_S / 0.05) + 2)
check(len(presses) == 2, "after reordering the pedals' button 5 is not taken for the wheel's")
be.press(0, 5)
poll(w, clk)
check(len(presses) == 3 and w._dev == 0, "the wheel is found on its new number by name and numbers")
check(w.status()["state"] == "ready" and "Button 5 on Wheel" in w.status()["text"], w.status()["text"])
be.devs = {1: {"name": "Pedals", "mid": 100, "pid": 1}}
poll(w, clk, 3)
check(w.status()["state"] == "not_found" and "isn't plugged in" in w.status()["text"], "wheel unplugged: said")
be.devs = {3: {"name": "Wheel", "mid": 200, "pid": 7}, 1: {"name": "Pedals", "mid": 100, "pid": 1}}
be.masks = {3: 0, 1: 0}
poll(w, clk, 25)
be.press(3, 5)
poll(w, clk)
check(len(presses) == 4 and w._dev == 3, "plugged back in on another number: found again")

print("== matching rules")
twins = [{"id": 0, "name": "Box", "mid": 5, "pid": 5}, {"id": 1, "name": "Box", "mid": 5, "pid": 5}]
check(wm.match(twins, wm.clean_binding({"device": 1, "button": 2, "name": "Box", "mid": 5, "pid": 5})) == 1,
      "two identical devices: the saved number decides")
generic = [{"id": 0, "name": "Microsoft PC-joystick driver", "mid": 1, "pid": 2},
           {"id": 1, "name": "Microsoft PC-joystick driver", "mid": 3, "pid": 4}]
check(wm.match(generic, wm.clean_binding({"device": 0, "button": 1, "name": "Microsoft PC-joystick driver",
                                          "mid": 3, "pid": 4})) == 1, "generic name: maker/product numbers decide")
check(wm.match(generic, wm.clean_binding({"device": 0, "button": 1})) == 0, "no identity saved: the number")
check(wm.match(generic, wm.clean_binding({"device": 0, "button": 1, "name": "Other"})) is None,
      "a different device on the saved number is not used")
check(wm.match([], None) is None, "no binding: None")
for bad in (None, {}, {"device": 20, "button": 1}, {"device": 0, "button": 0}, {"device": 0, "button": 33},
            {"device": "x", "button": 1}, {"device": 0, "button": True}, "junk"):
    check(wm.clean_binding(bad) is None, f"bad binding refused: {bad!r}")
b = wm.clean_binding({"device": "2", "button": "5", "name": "W" * 100, "mid": "x"})
check(b["device"] == 2 and b["button"] == 5 and len(b["name"]) == 64 and b["mid"] is None, "binding cleaned")
check(wm.label({"device": 2, "button": 1, "name": ""}) == "Button 1 on controller 3", "label without a name")

print("== the thread")
be2 = FakeBackend()
got = threading.Event()
learned2 = []
w2 = wm.WheelMute(on_press=got.set, backend=be2, hz=100)
before = {t.name for t in threading.enumerate()}
check(w2.learn(lambda b: learned2.append(b)), "learn starts the thread")
check(w2.running() and any(t.name == "debrief-wheelmute" for t in threading.enumerate()), "thread running")
time.sleep(0.1)
be2.press(2, 9)
deadline = time.time() + 3
while not learned2 and time.time() < deadline:
    time.sleep(0.02)
check(learned2 and learned2[0]["button"] == 9, "learned through the thread")
be2.press(2, 9, False)
time.sleep(0.45)
be2.press(2, 9)
check(got.wait(3), "press seen through the thread")
be2.fail = True
time.sleep(0.2)
check(w2.running(), "a failing driver doesn't end the thread")
be2.fail = False
r0 = be2.reads
deadline = time.time() + 3
while be2.reads == r0 and time.time() < deadline:
    time.sleep(0.05)
check(be2.reads > r0, "keeps reading after the driver recovers")
check(w2.stop(2.0) and not w2.running(), "stop: thread ended")
time.sleep(0.05)
check(not [t for t in threading.enumerate() if t.name == "debrief-wheelmute"], "no thread left")
r1 = be2.reads
time.sleep(0.15)
check(be2.reads == r1, "no reads after stop")
check(w2.start() and w2.stop(), "start again and stop")

# learn timeout through the thread
be3 = FakeBackend()
out = []
w3 = wm.WheelMute(backend=be3, hz=100)
w3.learn(out.append, timeout_s=1.0)
deadline = time.time() + 4
while not out and time.time() < deadline:
    time.sleep(0.02)
check(out == [None] and w3.binding is None, "learning times out with no press: callback(None)")
check(w3.status()["state"] == "not_set" and "No button was pressed" in w3.status()["text"], w3.status()["text"])
w3.learn(out.append, timeout_s=30)
w3.cancel_learn()
check(w3.status()["state"] != "learning", "learning cancelled")
w3.stop()

# a callback that raises doesn't kill the thread
be4 = FakeBackend()
w4 = wm.WheelMute(on_press=lambda: 1 / 0, backend=be4, binding={"device": 2, "button": 1, "name": "Wheel",
                                                                 "mid": 200, "pid": 7}, hz=100)
w4.start()
time.sleep(0.1)
be4.press(2, 1)
time.sleep(0.2)
check(w4.running() and w4.presses == 1, "a failing toggle is caught")
w4.stop()

# CPU: ~20 reads a second at the default rate
be5 = FakeBackend()
w5 = wm.WheelMute(backend=be5, binding={"device": 2, "button": 1, "name": "Wheel", "mid": 200, "pid": 7})
w5.start()
time.sleep(1.0)
w5.stop()
check(10 <= be5.reads <= 30, f"default rate: {be5.reads} reads in a second (about 20)")

print("== off Windows / no joystick interface")
saved = wm.sys.platform
try:
    wm.sys.platform = "linux"
    check(wm.default_backend() is None, "not Windows: no backend")
    w6 = wm.WheelMute(on_press=lambda: None)
    check(not w6.available() and not w6.start() and not w6.running(), "start is a no-op")
    check(not w6.learn(lambda b: None), "learn is a no-op")
    check(w6.status()["state"] == "unavailable" and "can't be read" in w6.status()["text"], w6.status()["text"])
    check(w6.stop(), "stop is fine")
    w6.poll_once()
    check(True, "poll without a backend: nothing happens")
finally:
    wm.sys.platform = saved
check(not [t for t in threading.enumerate() if t.name == "debrief-wheelmute"], "no wheel threads left at the end")
if sys.platform == "win32":
    be = wm.default_backend()
    check(be is None or isinstance(be.devices(), list), "Windows: the real backend lists devices without error")
    if be is not None:
        import ctypes
        check(ctypes.sizeof(be._JI) == 52 and ctypes.sizeof(be._JC) == 728,
              "Windows: JOYINFOEX and JOYCAPSW have the sizes winmm expects (52 and 728 bytes)")

print(f"\n{'all passed' if not fails else str(fails) + ' FAILED'}")
sys.exit(1 if fails else 0)
