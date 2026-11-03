"""2.17.2: cues checked again just before they play, hard zones, "How's my pace?" (and "Only when I ask"), short lap
times, and the pause where two clips are joined. Run: python tests/test_2172_pace.py

Fake telemetry from test_213's builder; his real files (Documents\\iRacing\\telemetry) only when present (never in CI)."""
from __future__ import annotations

import collections
import json
import os
import re
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))
TMP = Path(tempfile.mkdtemp(prefix="debrief-2172p-"))
os.environ["DEBRIEF_SETTINGS"] = str(TMP / "settings.toml")
os.environ["DEBRIEF_CACHE_DIR"] = str(TMP / "cache")
os.environ["DEBRIEF_UPDATE_STATE"] = str(TMP / "update.json")

from debrief import audioout as ao, coachvoice as cv, cuelog, livecoach as lc, voicepack as vp  # noqa: E402

fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


def builder() -> dict:
    """test_213_livecoach's fake-track builder (make, profile, BASE ...), without running its checks."""
    path = ROOT / "tests" / "test_213_livecoach.py"
    src = path.read_text(encoding="utf-8")
    cut = src.index("# ---------------------------------------------------------------- reference")
    ns = {"__name__": "coach_builder", "__file__": str(path)}
    exec(compile(src[:cut], str(path), "exec"), ns)   # noqa: S102  (our own test file)
    return ns


B = builder()
PACK = vp.find_pack("engineer")
PACK_IDS = ("engineer", "michael", "fenrir", "heart", "kumo", "omega")


def pack_dur(text):
    k = PACK.compose(text) if PACK is not None else None
    return PACK.duration_ms(k) / 1000.0 if k else None


def play(path, settings, ref=None, weak=None, ask_at=(), mute_at=None):
    """Plays a file into a Coach with George's clip lengths. ask_at: (lap, pct) where he asks for his pace.
    Returns (coach, said [(lap, cue, sample)], events, made [(cue, sample)]); coach.checked: why_invalid of each cue
    said, at the moment it was said; coach.expected: pace_text() when he asked."""
    src = lc.IbtPlaybackSource(path, speed=0)
    coach = lc.Coach(ref, weak, settings, duration=pack_dur)
    i = src.info()
    coach.hint(i["length_m"], i["car"], i["track"])
    ev, said, made = [], [], []
    coach.on_event = ev.append
    cur = {}
    push0 = coach.queue.push
    coach.queue.push = lambda c: (made.append((c, dict(cur))), push0(c))
    todo = list(ask_at)
    coach.checked, coach.expected = [], []
    while True:
        s = src.read()
        if s is None:
            break
        cur.clear()
        cur.update(s)
        if todo and s["Lap"] == todo[0][0] and float(s["LapDistPct"]) >= todo[0][1]:
            todo.pop(0)
            coach.ask()
            coach.expected.append(coach.pace_text())
        if mute_at is not None and s["Lap"] == mute_at[0] and float(s["LapDistPct"]) >= mute_at[1] \
                and not coach.settings.muted:
            coach.mute()
        for c in coach.feed(s):
            if c.kind != lc.KIND_BEEP:
                said.append((s["Lap"], c, s))
                coach.checked.append(coach.why_invalid(c, s))
    return coach, said, ev, made


def at(pct, v=50.0, brake=0.0, lap=3, t=500.0):
    return {"LapDistPct": pct, "Speed": v, "Brake": brake, "Lap": lap, "SessionTime": t, "CarLeftRight": 1}


TEL = TMP / "tel"
TEL.mkdir()
refp, _t = B["make"](TEL / "mclaren570sgt4_zolder gp ref.ibt", [{}, {}, {}])
ref = lc.build_reference(refp)
L = ref.length_m
SLOW2 = {2: (1500.0, 26.0, 10.0)}
EARLY3 = {3: (2400.0, 18.0, 7.0)}
FAST = {1: (600.0, 22.0, 10.0), 4: (3300.0, 38.0, 10.0)}
laps = [{"corners": {**SLOW2, **EARLY3}}, {"corners": {**SLOW2, **FAST}}, {"corners": {**SLOW2, **EARLY3, **FAST}},
        {"corners": {**SLOW2}}, {"corners": {**EARLY3, **FAST}}]
cur, ctimes = B["make"](TEL / "mclaren570sgt4_zolder gp cur.ibt", laps)

# ================================================================ a. checked again just before it plays
print("-- a. still valid when it plays")
coach, said, ev, made = play(cur, lc.Settings(level="busy"), ref=ref, weak=[{"id": "C2", "apex_m": 1500.0,
                                                                            "avg_avail": 0.3}])
check(made and all(callable(c.still_valid) for c, _s in made), "every cue made carries still_valid")
check(said and not any(coach.checked), f"every cue said was valid when it started ({[w for w in coach.checked if w][:3]})")
check(any(c.kind == lc.KIND_MARKER for c, _s in made) and any(c.kind == lc.KIND_CORNER for c, _s in made)
      and any(c.kind == lc.KIND_SECTOR for c, _s in made), "markers, corner words and sector times were made")
cf = lc.Coach(ref, [], lc.Settings(), duration=pack_dur)
c2 = ref.corner(2)
cf._pct = (c2.brake_pct - 400 / L) % 1.0
mk = lc.Cue(text=lc.marker_text("T2"), priority=3, key="marker:C2", t=1.0, kind=lc.KIND_MARKER, corner="C2",
            expires=99.0)
cf._push(mk)
v = 50.0
need = cf._need_s(mk.text)
check(cf.why_invalid(mk, at(cf._pct, v=v)) is None, "a brake marker is valid where it was made")
close = (c2.brake_pct - 0.9 * need * v / L) % 1.0             # its clip would end 0.9 s short of the brake point
check(cf.why_invalid(mk, at(close, v=v)) == "would not end 1 s before its brake point",
      "...not once its clip could no longer end 1 s before the brake point")
check(cf.why_invalid(mk, at((c2.brake_pct + 0.01) % 1.0, v=v), starting=False) == "passed its brake point",
      "...nor once the car is past it")
cf._pct = c2.apex_pct + c2.end_rel_m / L
cw = lc.Cue(text="T2, two tenths down", priority=2, key="corner:C2", t=1.0, kind=lc.KIND_CORNER, corner="C2",
            expires=99.0)
cf._push(cw)
a, _b = cf._next_zone(2)
check(abs(a - ((ref.corner(3).brake_pct) % 1.0)) < 1e-9, "the next zone starts at the next corner's brake point")
check(cf.why_invalid(cw, at(cf._pct), starting=False) is None, "a word after a corner is valid where it was made")
check(cf.why_invalid(cw, at((a - 30 / L) % 1.0), starting=False) is None, "...still 30 m before the next corner's brakes")
check(cf.why_invalid(cw, at((a + 2 / L) % 1.0), starting=False) == "the next corner's braking zone has started",
      "...not once the next corner's braking zone has started")
cf._pct = 0.3
sc = lc.Cue(text="Sector one, up a tenth", priority=1, key="sector:1", t=1.0, kind=lc.KIND_SECTOR, expires=99.0)
cf._push(sc)
check(cf.why_invalid(sc, at(0.31), starting=False) is None, "a sector time is valid ...")
cf._sector_seq += 1
check(cf.why_invalid(sc, at(0.31), starting=False) == "a newer sector is done", "...until a newer sector is done")
drops = [e for e in ev if e["ev"] == "dropped" and str(e.get("dropped", "")).startswith("no longer valid: ")]
print("   dropped as no longer valid:", collections.Counter(e["dropped"] for e in drops))
check(not [e for e in ev if e["ev"] == "spoken" and any(d["text"] == e["text"] and d["t"] == e["t"] for d in drops)],
      "a cue dropped as no longer valid is never said")

# the queue: at most QUEUE_DEPTH of a kind waits
q = lc.CueQueue(on_drop=lambda c, why: dropped.append((c.key, why)))
dropped = []
for k in range(3):
    q.push(lc.Cue(text=f"s{k}", priority=1, key=f"sector:{k}", t=float(k), kind=lc.KIND_SECTOR, expires=99.0))
for k in range(3):
    q.push(lc.Cue(text=f"c{k}", priority=2, key=f"corner:C{k}", t=float(k), kind=lc.KIND_CORNER, expires=99.0))
check([c.key for c in q.items] == ["sector:2", "corner:C1", "corner:C2"],
      f"one sector time and two corner words wait at most ({[c.key for c in q.items]})")
check(dropped == [("sector:0", "too many waiting (sector)"), ("sector:1", "too many waiting (sector)"),
                  ("corner:C0", "too many waiting (corner)")], f"the oldest go, and the log says why ({dropped})")

# the voice runs the check right before the sound starts
be = ao.FakeBackend(speed=50.0)
pl = ao.Player(be)
ends = []
pl.play(b"\x00\x01" * 1600, 16000, priority=1, on_end=lambda it, ok: ends.append(("a", ok)), check=lambda: None)
pl.play(b"\x00\x01" * 1600, 16000, priority=1, on_end=lambda it, ok: ends.append(("b", ok)),
        check=lambda: "the car has moved on")
t0 = time.time()
while len(ends) < 2 and time.time() - t0 < 5:
    time.sleep(0.01)
check(sorted(ends) == [("a", True), ("b", False)] and pl.invalid == 1 and pl.played == 1,
      f"the player plays a still-valid sound and drops one whose check fails ({ends}, invalid {pl.invalid})")
pl.close()


class Renderer:
    available = True

    def render_async(self, text, rate, cb):
        cb(None)

    def close(self, timeout=None):
        pass


voice = cv.PackVoice("engineer", player=ao.Player(ao.FakeBackend(speed=50.0)), renderer=Renderer())
voice.wait_ready()
asked = []
check(voice.say("Down three tenths", priority=1, check=lambda: asked.append(1) or "moved on") is True,
      "PackVoice.say takes a check")
t0 = time.time()
while not asked and time.time() - t0 < 5:
    time.sleep(0.01)
time.sleep(0.05)
check(asked == [1] and voice.player.invalid == 1 and voice.player.played == 0, "...and the player runs it before playing")
voice.close()

# the runner: the check uses the newest sample and logs what it drops
r = lc.LiveRunner(lc.IbtPlaybackSource(cur, speed=0), voice=types.SimpleNamespace(say=lambda *a, **k: True),
                  reference=ref, log_dir=TMP / "logs")
r._ensure_reference()
r.cuelog = cuelog.CueLog(TMP / "logs", "Mclaren 570s GT4", "Zolder")
mc = lc.Cue(text=lc.marker_text("T2"), priority=3, key="marker:C2", t=1.0, kind=lc.KIND_MARKER, corner="C2",
            expires=99.0)
r.coach._pct = 0.2
r.coach._push(mc)
c2 = ref.corner(2)
r._latest = at((c2.brake_pct + 0.005) % 1.0)
check(r._play_check(mc) == "passed its brake point" and r.coach.invalid_drops == 1, "the runner's check drops a late marker")
rows = cuelog.read_log(TMP / "logs", r.cuelog.path.name)
check(any(x.get("ev") == "dropped" and x.get("dropped") == "no longer valid: passed its brake point" for x in rows),
      "the cue log says: dropped, no longer valid")
said_kw = []
r.voice = types.SimpleNamespace(say=lambda text, priority=0, t=None, check=None: said_kw.append(check) or True)
r._takes_check = None
r._say(mc)
check(said_kw and callable(said_kw[0]), "the runner hands the check to a voice that takes one")
said_old = []
r.voice = types.SimpleNamespace(say=lambda text, priority=0, t=None: said_old.append(text) or True)
r._takes_check = None
r._say(mc)
check(said_old == [mc.text], "...and still speaks through an older voice without one")

# ================================================================ b. hard zones
print("-- b. hard zones")
hz = lc.hard_zones(ref)
check(len(hz) == len(ref.braked), f"one hard zone per braked corner ({len(hz)})")
check(all(any(-5 <= ((a - c.brake_pct + 0.5) % 1.0 - 0.5) * L < 40 for c in ref.braked) for a, _b in hz),
      "each starts at heavy braking, just after a brake point")
check(all(any(0 <= ((b - c.apex_pct) % 1.0) * L < 60 for c in ref.braked) for _a, b in hz),
      "...and ends at full throttle, at the corner's exit")
syn = types.SimpleNamespace(pct=np.linspace(0, 1, 100, endpoint=False), brake=np.zeros(100), throttle=np.ones(100))
syn.brake[10:15] = 0.8
syn.throttle[10:20] = 0.3
syn.brake[17:19] = 0.7                          # a second stab before full throttle: the same zone
syn.brake[95:] = 0.9
syn.throttle[95:] = 0.0
syn.throttle[:3] = 0.5                          # across the line
z = lc.hard_zones(syn)
check(np.allclose(z, [(0.1, 0.2), (0.95, 0.03)]), f"zones from brake > 0.5 to throttle > 0.9, across the line too ({z})")
co = lc.Coach(ref, [], lc.Settings(), duration=pack_dur)
inside, ahead = co._hard_at(hz[0][0] + 0.001)
check(inside and ahead == 0.0, "inside a hard zone")
inside, ahead = co._hard_at((hz[0][0] - 100 / L) % 1.0)
check(not inside and abs(ahead - 100) < 1, f"100 m before one ({ahead:.0f} m)")
check(co._nonurgent(lc.Cue("Sector one, up a tenth", 1, "sector:1", kind=lc.KIND_SECTOR))
      and co._nonurgent(lc.Cue("Down a tenth", 1, "lap", kind=lc.KIND_LAP))
      and co._nonurgent(lc.Cue("T3, good, up a tenth", 1, "good:C3", kind=lc.KIND_CORNER))
      and co._nonurgent(lc.Cue("Reference set, thirty nine", 1, "info:refset", kind=lc.KIND_INFO))
      and not co._nonurgent(lc.Cue("T3, a tenth down", 2, "corner:C3", kind=lc.KIND_CORNER))
      and not co._nonurgent(lc.Cue("Brake marker, T3", 3, "marker:C3", kind=lc.KIND_MARKER)),
      "sector and lap times, good cues and Reference set wait for the hard zones; corner cues don't")
coach, said, ev, made = play(cur, lc.Settings(), ref=ref)
bad = []
for _l, c, s in said:
    if not coach._nonurgent(c):
        continue
    inside, ahead = coach._hard_at(float(s["LapDistPct"]))
    if inside or (pack_dur(c.text) + coach.settings.audio_latency_s) * max(float(s["Speed"]), 5.0) > ahead + 1e-6:
        bad.append(c.text)
check(not bad, f"no sector or lap time starts in a hard zone or runs into one ({bad[:3]})")
n_on = len(said)
lc.HARD_GATING = False
_c, said_off, _e, _m = play(cur, lc.Settings(), ref=ref)
lc.HARD_GATING = True
check(n_on >= 0.85 * len(said_off), f"hard zones cost Normal few cues on these laps ({n_on} vs {len(said_off)} without)")

# ================================================================ c. How's my pace?
print("-- c. How's my pace?")
co = lc.Coach(None, [], lc.Settings(), duration=pack_dur)
check(co.pace_text() == "Building your reference", "no reference: Building your reference")
co = lc.Coach(ref, [], lc.Settings(), duration=pack_dur)
check(co.pace_text() == "No clean lap yet", "a reference from his files, no clean lap yet")
live = lc.Reference(**{**ref.__dict__, "source": {"kind": "live", "lap": 1}})
co = lc.Coach(live, [], lc.Settings(), duration=pack_dur)
check(co.pace_text() == lc.reference_set_text(live.lap_time, short=True), f"his first lap is the reference: {co.pace_text()}")
co = lc.Coach(ref, [], lc.Settings(), duration=pack_dur)
for row, want in (({"delta": 0.31, "most": 4}, "Down three tenths, most in T4"),
                  ({"delta": 0.31, "most": None}, "Down three tenths"),
                  ({"delta": 1.44, "most": 12}, "Down 1.4 seconds, most in T12"),
                  ({"delta": 0.04, "most": 3}, "Good, on pace"),
                  ({"delta": -0.22, "most": 3}, "New best, up two tenths"),
                  ({"delta": -0.04}, "New best"),
                  ({"delta": 2.6, "most": 3}, "Down more than two seconds")):
    co.lap_log = [{"lap": 3, "time": 98.0, "clean": True, **row}, {"lap": 4, "time": 120.0, "clean": False, "delta": None}]
    check(co.pace_text() == want, f"{row} -> {co.pace_text()!r}")
check(vp.compose("Down three tenths, most in T4") == ["down_lead_0_3", "most_turn_4"]
      and vp.compose("No clean lap yet") == ["no_clean_lap_yet"]
      and vp.compose("Down more than two seconds") == ["down_more_than_two_seconds"], "the answers are clips")
phrases = (["Good, on pace", "New best", lc.NO_CLEAN_TEXT, lc.FAR_DOWN_TEXT, lc.BUILDING_TEXT]
           + [f"Down {lc.delta_words(k / 10)}" + (f", most in T{n}" if n else "") for k in range(1, 21)
              for n in range(0, vp.MAX_TURN + 1)]
           + [f"New best, up {lc.delta_words(k / 10)}" for k in range(1, 21)]
           + [lc.reference_set_text(k / 10, short=sh) for k in range(400, 1800, 7) for sh in (True, False)])
for pid in PACK_IDS:
    pk = vp.find_pack(pid)
    miss = [p for p in phrases if pk is None or pk.compose(p) is None]
    check(not miss, f"{pid} says every answer to How's my pace? and every lap time, full and short ({miss[:3]})")

# asked on the move: answered when it is safe, not counted in the lap's cues
coach, said, ev, made = play(cur, lc.Settings(), ref=ref, ask_at=[(3, 0.20), (4, 0.45)])
asks = [(lap, c, s) for lap, c, s in said if c.kind == lc.KIND_ASK]
check(len(asks) == 2 and coach.asks == 2, f"both questions answered ({[(lap, c.text) for lap, c, _s in asks]})")
check([c.text for _l, c, _s in asks] == coach.expected and asks[0][1].text.startswith(("Down", "New best")),
      f"about his last clean lap ({[c.text for _l, c, _s in asks]})")
check(any(", most in T" in c.text for _l, c, _s in asks) or not any(c.text.startswith("Down") for _l, c, _s in asks),
      "a slower lap names the corner that cost the most")
check(all(float(s["Brake"]) <= 0.05 for _l, c, s in asks) and not any(coach.checked),
      "never under braking, in a corner or into a hard zone")
check(all(c.spoken_t - c.t <= lc.ASK_WAIT_S for _l, c, _s in asks), "within a few seconds")
check(all(c.why == "you asked: How's my pace?" for _l, c, _s in asks), "the log says he asked")
pl_ask = collections.Counter(lap for lap, c, _s in said if c.kind != lc.KIND_ASK)
check(max(pl_ask.values()) <= 4, f"the answer isn't one of the lap's 4 cues ({dict(pl_ask)})")
coach, said, ev, made = play(cur, lc.Settings(), ref=ref, ask_at=[(3, 0.20)], mute_at=(2, 0.5))
check([c.kind for _l, c, _s in said if _l >= 3] == [lc.KIND_ASK], "muted: only the answer he asked for")

# Only when I ask
sa = lc.Settings(level="ask")
check(sa.ask_only and sa.per_lap() == 0 and lc.Settings.from_dict({"level": "ask"}).level == "ask", "the level 'ask'")
coach, said, ev, made = play(cur, sa, ref=ref)
check(not said, f"Only when I ask: nothing said when he doesn't ask ({[c.text for _l, c, _s in said][:3]})")
check(any(e["ev"] == "not_made" and e.get("why") == "Only when I ask" for e in ev), "...the log says why")
coach, said, ev, made = play(cur, sa, ref=ref, ask_at=[(2, 0.3), (4, 0.6)])
check([c.kind for _l, c, _s in said] == [lc.KIND_ASK, lc.KIND_ASK], "...and only the answers when he does")
check("Only when you ask" in coach.focus_info()["text"] and "—" not in coach.focus_info()["text"],
      coach.focus_info()["text"])

# the runner, the keys, the window
r = lc.LiveRunner(lc.IbtPlaybackSource(cur, speed=0), voice=types.SimpleNamespace(say=lambda *a, **k: True),
                  reference=ref)
check(r.ask_pace() is False, "asking with the coach off does nothing")
check(lc.Settings().pace_hotkey == "ctrl+shift+p" and lc.parse_hotkey("ctrl+shift+p") == (0x6, ord("P")),
      "Ctrl+Shift+P")
from debrief import app  # noqa: E402
from debrief.desktop import coach2, server as S  # noqa: E402
hk = coach2.hotkeys(types.SimpleNamespace(), lc.Settings(), types.SimpleNamespace(toggle=None, repeat=None,
                                                                                    ask_pace="ASK", mute_for=None))
check(hk.keys["pace"].text == "ctrl+shift+p" and hk.keys["pace"].callback == "ASK", "the pace key asks the runner")
check(S.clean_coach_settings({"level": "ask", "lap_times": "full"}) == {"level": "ask", "lap_times": "full"},
      "the window may pick Only when I ask and the lap time reading")
for k, bad, msg in (("level", "loud", "Pick Quiet, Normal, Busy or Only when I ask."), ("lap_times", "long", "Pick Full or Short.")):
    try:
        S.clean_coach_settings({k: bad})
        check(False, f"{k} {bad!r} refused")
    except ValueError as e:
        check(str(e) == msg, f"{k} {bad!r} refused in plain words")


class Wheel:
    def __init__(self):
        self.devs = {2: {"name": "Wheel", "mid": 200, "pid": 7}}
        self.masks = {2: 0}

    def devices(self):
        return [dict(v, id=k, buttons=32) for k, v in self.devs.items()]

    def buttons(self, dev):
        return self.masks.get(dev)

    def identity(self, dev):
        return self.devs.get(dev)


class Runner:
    def __init__(self):
        self.asked = 0

    def running(self):
        return True

    def ask_pace(self):
        self.asked += 1
        return True

    def repeat(self):
        return None


wb = Wheel()
srv = types.SimpleNamespace(coach_json_lock=threading.Lock(), coach_lock=threading.Lock(), coach=None,
                            coach_starting=False, wheel_backend_factory=lambda: wb)


class H:
    server = srv

    def _json(self, d, code=200):
        return code, d


code, rr = coach2.post_wheel_learn(H(), {}, "pace")
check(code == 200 and rr["ok"] and rr["wheel"]["state"] == "learning", f"Learn a wheel button for the pace ({rr})")
time.sleep(0.3)
wb.masks[2] = 1 << 4                             # button 5
t0 = time.time()
while coach2._wheel_saved("pace") is None and time.time() - t0 < 5:
    time.sleep(0.05)
wb.masks[2] = 0
b = coach2._wheel_saved("pace")
check(b and b["button"] == 5 and coach2._wheel_saved("repeat") is None, f"saved as the pace button only ({b})")
st = coach2.wheel_status(srv, "pace")
check(st["text"].endswith("It asks the coach for your pace.") and st["label"], st["text"])
srv.coach = Runner()
coach2.start_wheel(srv)
time.sleep(0.3)
wb.masks[2] = 1 << 4
t0 = time.time()
while srv.coach.asked == 0 and time.time() - t0 < 5:
    time.sleep(0.05)
wb.masks[2] = 0
check(srv.coach.asked == 1, "pressing it asks the coach for the pace")
code, rr = coach2.post_pace(H(), {})
check(code == 200 and rr["asked"] and srv.coach.asked == 2, "the window's How's my pace? button asks too")
code, rr = coach2.post_wheel_clear(H(), {}, "pace")
check(code == 200 and coach2._wheel_saved("pace") is None, "Clear forgets it")
coach2.stop_wheel(srv)
ex = {"mode": "live"}
srv.coach_hotkey = None
coach2.extra(srv, ex)
check(ex["keys"]["pace"]["key"] == "Ctrl+Shift+P" and "wheel_pace" in ex and "wheel_repeat" in ex,
      "coach status names the pace key and both wheel buttons")
check("api/coach/pace" in coach2.POST_ROUTES and "api/coach/wheel-pace-learn" in coach2.POST_ROUTES, "routes")

html = (ROOT / "debrief" / "desktop" / "ui" / "index.html").read_text(encoding="utf-8")
js = (ROOT / "debrief" / "desktop" / "ui" / "app.js").read_text(encoding="utf-8")
m = re.search(r'<fieldset class="radios colevel" id="coLevel">(.*?)</fieldset>', html, re.S)
check(m and re.findall(r'value="(\w+)"', m.group(1)) == ["quiet", "normal", "busy", "ask"]
      and "<b>Only when I ask</b>" in m.group(1), "the level picker has Only when I ask")
check('id="coPace"' in html and "Ctrl+Shift+P" in html and 'id="coWheelPLearn"' in html and 'id="coLapTimes"' in html,
      "the page: How's my pace? button, its wheel button and Say lap times")
check("coach/pace" in js and "k==='p'" in js and "coach/wheel-'+role+'-learn" in js and "lap_times:" in js,
      "the page asks, learns the button and saves the reading")
for part in (html, js):
    new = [ln for ln in part.splitlines() if re.search(r"pace|Pace|lap_times|Only when I ask|Hard zones|no longer valid", ln)]
    check(not [ln for ln in new if "—" in ln or "–" in ln], "no long dashes in the new window text")

# ================================================================ d. short lap times
print("-- d. lap times, full and short")
check(lc.Settings().lap_times == "short" and lc.Settings.from_dict({"lap_times": "FULL"}).lap_times == "full"
      and lc.Settings.from_dict({"lap_times": "x"}).lap_times == "short", "short by default; checked")
check([vp.lap_time_short_words(x) for x in (98.874, 65.04, 120.0, 52.3, 119.96)] ==
      ["thirty-eight nine", "oh five oh", "oh oh oh", "fifty-two three", "oh oh oh"], "short words")
check(lc.reference_set_text(98.874, short=True) == "Reference set, thirty-eight nine"
      and vp.compose("Reference set, thirty-eight nine") == ["reference_set", "sec_38", "tenth_9"]
      and vp.compose("Reference set, one thirty-eight nine") == ["reference_set", "lap_98", "tenth_9"], "both read")
firstp, ftimes = B["make"](TEL / "mclaren570sgt4_zolder gp first.ibt", [{}, {"corners": SLOW2}])
for mode in ("short", "full"):
    c1, said1, _e, _m = play(firstp, lc.Settings(lap_times=mode))
    rs = [c.text for _l, c, _s in said1 if c.text.startswith("Reference set")]
    check(rs == [lc.reference_set_text(ftimes[0], short=mode == "short")], f"{mode}: {rs}")

# ================================================================ e. the pause where clips join
print("-- e. joins")
for pid in PACK_IDS:
    pk = vp.find_pack(pid)
    check(pk is not None and 30 <= pk.join_ms <= 80, f"{pid}: {pk and pk.join_ms:.0f} ms between joined parts")
keys = PACK.compose("Down three tenths, most in T4")
x = PACK.render_samples(keys)
lead, tail = PACK.clips[keys[0]], PACK.clips[keys[1]]
gap = int(round(PACK.rate * PACK.join_ms / 1000))
xf = int(PACK.rate * vp.XFADE_MS / 1000)
check(len(x) == len(lead) + gap - xf + len(tail) and np.allclose(x[-len(tail):], tail),
      "the tail clip plays from its first sample (no second fade over its first sound)")
check(np.allclose(x[:len(lead)], lead) and not np.any(x[len(lead):len(lead) + gap - xf]),
      "...after the lead and a silent pause of the pack's length")
check(PACK.duration_ms(keys) == int(round(len(x) * 1000 / PACK.rate)), "duration_ms is still exact")
loud = np.ones(400) * 0.5
pkx = vp.VoicePack(TMP, {"id": "x", "name": "x", "sample_rate": 16000, "join_ms": 50,
                         "clips": {"a": {"role": "lead"}, "b": {"role": "tail"}}}, {"a": loud, "b": loud})
y = pkx.render_samples(["a", "b"])
check(y[399] < 0.01 and y[-400] < 0.01, "an edge that isn't already silent still gets a short fade (no click)")

# ================================================================ f. speeds in mph (owner's choice)
print("-- f. mph")
st0 = lc.Settings()
check(st0.slower_kmh == 4.0 and st0.slower_mph() == 3.0, "the 4 km/h trigger is 3 mph down or more")
check([lc.mph_down(k) for k in (4.0, 4.9, 6.0, 14.4, 32.2)] == [2, 3, 4, 9, 20], "km/h differences in whole mph")
check(lc.speed_text("T3", 4) == "T3: more speed, 4 down" and vp.compose("T3: more speed, 3 down") == ["turn_3", "more_speed_3_down"],
      "'more speed, 3 down' is a clip (3 mph)")
spd = [lc.speed_text(f"T{n}", k) for n in range(1, vp.MAX_TURN + 1) for k in range(int(st0.slower_mph()), vp.MAX_KMH + 1)]
for pid in PACK_IDS:
    pk = vp.find_pack(pid)
    miss = [p for p in spd if pk is None or pk.compose(p) is None]
    check(not miss, f"{pid} says every speed number, 3 to 20 mph, at every turn ({miss[:3]})")
coach, said, ev, made = play(cur, lc.Settings(), ref=ref)
sp = [(c.text, c.measured.get("down_kmh"), c.why) for _l, c, _s in said if "more speed" in c.text]
check(sp and all(int(t.split(", ")[-1].split()[0]) == lc.mph_down(k) >= 3 and k / lc.KMH_PER_MPH >= 3.0 - 1e-6
              and "mph under the reference" in w for t, k, w in sp),
      f"spoken speeds and the log's reason are mph ({sp[:2]})")
txt = cuelog.summary([{"ev": "session"}, {"ev": "spoken", "lap": 2, "dist_m": 900, "kmh": 160.9, "text": "T2: more speed, 4 down",
                       "why": "minimum speed 9 km/h under the reference", "measured": {"min_kmh": 80.5, "ref_min_kmh": 96.6}}])
check("min 50 vs ref 60 mph" in txt and " 100 \"T2" in txt and "6 mph under" in txt and "km/h" not in txt,
      "Copy for Claude shows mph, older logs too")
check("<th>mph</th>" in html and "mphText(" in js and "mphOf(r.kmh)" in js, "the Coach page shows mph")

# ================================================================ his real files
print("-- real laps")
REAL = Path("/mnt/user-data/uploads/Documents/iRacing/telemetry")
files = sorted(REAL.glob("mclaren570sgt4_zolder*.ibt")) if REAL.is_dir() else []
if len(files) >= 3:
    for level in ("quiet", "normal", "busy", "ask"):
        n_clean = n_said = n_bad = n_asked = n_answered = 0
        for f in files[:4]:
            rref = lc.build_reference([p for p in files if p != f])
            co, sd, evr, _m = play(f, lc.Settings(level=level, in_races=True), ref=rref, ask_at=[(3, 0.5)])
            n_bad += sum(1 for w in co.checked if w)
            n_asked += len(co.expected)
            n_answered += sum(1 for _l, c, _s in sd if c.kind == lc.KIND_ASK)
            clean = {x["lap"] for x in co.lap_log if x["clean"]}
            n_clean += len(clean)
            n_said += sum(1 for lap, c, _s in sd if lap in clean and c.kind != lc.KIND_ASK)
        print(f"   {level}: {n_said / max(1, n_clean):.2f} cues per clean lap ({n_clean} clean laps); "
              f"asked {n_asked}, answered {n_answered}")
        check(n_answered == n_asked, f"real laps, {level}: every How's my pace? answered")
        check(n_bad == 0, f"real laps, {level}: no cue said that was no longer valid")
else:
    print("   (his telemetry files are not here: skipped)")

print(f"\n{'FAILED ' + str(fails) if fails else 'ALL OK'}")
sys.exit(1 if fails else 0)
