"""Coach 2.0 (2.17): the voice pack played through a chosen device, the audio delay, the cue log, rehearsal,
"Repeat that", "Mute for 1 minute" and the "already fixed" rule. Run: python tests/test_217_coach2.py

No Windows, no sound card: the device is audioout.FakeBackend (sounds "play" in real time on a timer), Windows speech is a stand-in renderer. The coach is driven by the fake .ibt files of
tests/test_213_livecoach.py (its lap builder, loaded without running its checks) and, for the window, the
McLaren-at-Zolder files of tests/corner_laps.py. Browser checks use Playwright + Chromium and are skipped when
either is missing; DEBRIEF_SHOTS=<folder> saves coach2-light.png and coach2-dark.png.
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
TMP = Path(tempfile.mkdtemp(prefix="debrief-217-"))
os.environ["DEBRIEF_SETTINGS"] = str(TMP / "settings.toml")
os.environ["DEBRIEF_CACHE_DIR"] = str(TMP / "cache")
os.environ["DEBRIEF_SCHEDULE_DIR"] = str(TMP / "schedule")
os.environ["DEBRIEF_UPDATE_STATE"] = str(TMP / "update.json")

from debrief import (app, audioout as ao, coachvoice as cv, cuelog, ibt, livecoach as lc,  # noqa: E402
                     voice as vc, voicepack as vp)
from debrief.desktop import coach2, server as S  # noqa: E402

fails = 0
RESULTS = []


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


def wait_for(fn, timeout=5.0, step=0.01):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return fn()


def builder() -> dict:
    """test_213_livecoach's fake-track builder (make, play, profile, BASE ...), without running its checks."""
    path = ROOT / "tests" / "test_213_livecoach.py"
    src = path.read_text(encoding="utf-8")
    cut = src.index("# ---------------------------------------------------------------- reference")
    ns = {"__name__": "coach_builder", "__file__": str(path)}
    exec(compile(src[:cut], str(path), "exec"), ns)   # noqa: S102  (our own test file)
    return ns


B = builder()
PACK = vp.find_pack("engineer")
check(PACK is not None and PACK.rate == 16000, "the default pack (George) loads, at 16 kHz")


def pcm(seconds, rate=16000):
    return b"\x01\x00" * int(seconds * rate)


def threads_named(prefix):
    return [t for t in threading.enumerate() if t.name.startswith(prefix) and t.is_alive()]


# ================================================================ 1. the playback layer (fake waveOut)
print("-- playback layer")
be = ao.FakeBackend(speed=1.0)
before = len(threads_named("debrief-audio"))
done = []
p = ao.Player(be, device="Headset")
p.play(pcm(0.15), 16000, priority=1, on_end=lambda it, ok: done.append(("a", ok)))
p.play(pcm(0.05), 16000, priority=1, on_end=lambda it, ok: done.append(("b", ok)))
wait_for(lambda: len(done) == 2, 3)
check(done == [("a", True), ("b", True)], f"equal priorities play in order, each to the end ({done})")
check([x for x in be.log if x[0] == "open"] == [("open", 1, 16000)], f"opened once, on the named device ({be.log[:2]})")
check(p.status()["device"] == "Headset" and not p.status()["using_default"] and p.status()["note"] == "", "status: the headset")
# a higher priority stops the one playing at once
done.clear()
starts = []
p.play(pcm(1.0), 16000, priority=1, on_start=lambda it: starts.append(("lap", time.monotonic())),
       on_end=lambda it, ok: done.append(("lap", ok)))
time.sleep(0.1)
t_marker = time.monotonic()
p.play(pcm(0.05), 16000, priority=3, on_start=lambda it: starts.append(("marker", time.monotonic())),
       on_end=lambda it, ok: done.append(("marker", ok)))
wait_for(lambda: len(done) == 2, 3)
check(done == [("lap", False), ("marker", True)], f"a higher priority cue cuts the lower one off ({done})")
check(any(x[0] == "reset" for x in be.log), "waveOutReset on the cut")
ms = dict(starts)
check(ms.get("marker", 9e9) - t_marker < 0.08, f"the priority cue starts within 80 ms ({(ms.get('marker', 0) - t_marker) * 1000:.0f} ms)")
# a lower one waits; one waiting too long is dropped, never said late
done.clear()
p.max_wait_s = 0.2
p.play(pcm(0.5), 16000, priority=3, on_end=lambda it, ok: done.append(("long", ok)))
time.sleep(0.03)
p.play(pcm(0.05), 16000, priority=1, on_end=lambda it, ok: done.append(("late", ok)))
wait_for(lambda: len(done) == 2, 3)
check(done == [("long", True), ("late", False)] and p.dropped >= 1, f"a cue that waited too long is dropped ({done})")
p.max_wait_s = ao.MAX_WAIT_S
# cancel: stop now, forget what waits
done.clear()
p.play(pcm(1.0), 16000, priority=1, on_end=lambda it, ok: done.append(("x", ok)))
p.play(pcm(1.0), 16000, priority=1, on_end=lambda it, ok: done.append(("y", ok)))
time.sleep(0.05)
t0 = time.monotonic()
p.cancel()
wait_for(lambda: len(done) == 2, 2)
check(sorted(done) == [("x", False), ("y", False)] and time.monotonic() - t0 < 0.2, f"cancel stops at once ({done})")
# the headset unplugged mid-session: Windows default, said in plain words, back on the headset when it returns
done.clear()
be.fail = {"write": be.counts["write"] + 1}
be.device_list = [{"id": 0, "name": "Speakers"}]
p.play(pcm(0.05), 16000, priority=1, on_end=lambda it, ok: done.append(("lost", ok)))
wait_for(lambda: done, 2)
check(done == [("lost", True)], f"a write failing on a lost device is played on Windows default ({done})")
check(("open", ao.WAVE_MAPPER, 16000) in be.log, "reopened on WAVE_MAPPER")
st = p.status()
check(st["using_default"] and "isn't connected" in st["note"] and "Headset" in st["note"], f"note: {st['note']}")
be.fail = {}
be.device_list = [{"id": 0, "name": "Speakers"}, {"id": 4, "name": "Headset"}]
p._next_check = 0.0
done.clear()
p.play(pcm(0.05), 16000, priority=1, on_end=lambda it, ok: done.append(("back", ok)))
wait_for(lambda: done, 2)
check(("open", 4, 16000) in be.log and not p.status()["using_default"] and p.status()["note"] == "",
      "the headset is used again (found by name on its new number)")
# a device that can't even be opened at first: default, no exception
be2 = ao.FakeBackend(devices=[{"id": 0, "name": "Speakers"}])
p2 = ao.Player(be2, device="Gone headset")
d2 = []
p2.play(pcm(0.05), 16000, on_end=lambda it, ok: d2.append(ok))
wait_for(lambda: d2, 2)
check(d2 == [True] and p2.status()["using_default"], "a chosen device that isn't there: Windows default")
p2.set_device("")
d2.clear()
p2.play(pcm(0.05), 16000, on_end=lambda it, ok: d2.append(ok))
wait_for(lambda: d2, 2)
check(d2 == [True] and p2.status()["device"] == "Windows default" and p2.status()["note"] == "", "Windows default chosen")
# no thread leaks
p.close()
p2.close()
check(len(threads_named("debrief-audio")) == before, "closing the players ends their threads")
check(be.open_handles == 0 and be2.open_handles == 0, "every device handle closed")
check(not p.play(pcm(0.1), 16000), "a closed player plays nothing")
check(ao.clean_name("Headset Earphone (Arctis 7 Chat) extra long name") == "Headset Earphone (Arctis 7 Chat" and
      ao.clean_name("Windows default") == "" and ao.clean_name("a\x00b") == "ab", "device names cleaned like szPname")
check(ao.list_devices(ao.FakeBackend()) == [{"id": 0, "name": "Speakers"}, {"id": 1, "name": "Headset"}],
      "device list from the backend")
if sys.platform != "win32":
    check(ao.default_backend() is None and ao.list_devices() == [], "off Windows: no device to choose, no error")

# ================================================================ 2. the audio delay
print("-- audio delay")
for lat in (0.0, 0.08, 0.2):
    bl = ao.FakeLatencyBackend(speed=4.0, latency_s=lat * 4.0)     # speed 4: real wait = latency_s / 4 = lat
    pl = ao.Player(bl)
    m = pl.measure_latency(tries=3)
    pl.close()
    check(m is not None and abs(m - lat) < 0.03, f"measured {m} s for a {lat} s start delay (from the position)")
bl = ao.FakeLatencyBackend(speed=1.0, latency_s=0.05, report_position=False)
pl = ao.Player(bl)
m = pl.measure_latency(tries=1)
pl.close()
check(m is not None and 0.03 <= m <= 0.12, f"no position from the device: from 'done' less the length ({m})")
bl = ao.FakeLatencyBackend(speed=1.0, latency_s=0.9)
pl = ao.Player(bl)
m = pl.measure_latency(tries=1)
pl.close()
check(m == ao.LATENCY_MAX_S, f"bounded to 0.5 s ({m})")
s = lc.Settings.from_dict({"audio_latency_s": 3})
check(s.audio_latency_s == 0.5 and lc.Settings.from_dict({"audio_latency_s": -1}).audio_latency_s == 0.0,
      "Settings keep audio_latency_s within 0..0.5")
c = lc.Coach(None, None, lc.Settings(audio_latency_s=0.0), duration=lambda t: 1.2)
c2 = lc.Coach(None, None, lc.Settings(audio_latency_s=0.3), duration=lambda t: 1.2)
check(abs(c2._need_s("x") - c._need_s("x") - 0.3) < 1e-9, "the coach starts a cue earlier by the measured delay")

# ================================================================ 3. the pack voice
print("-- pack voice")


class FakeRenderer:
    """Windows speech stand-in: 0.4 s of tone for any text, after delay_s, at 22050 Hz."""
    available = True

    def __init__(self, delay_s=0.0):
        self.delay_s = delay_s
        self.asked = []

    def render_async(self, text, rate, cb):
        self.asked.append((text, rate))

        def go():
            time.sleep(self.delay_s)
            cb((vp.to_pcm16(0.2 * np.sin(np.arange(int(0.4 * 22050)) / 5.0)), 22050))
        threading.Thread(target=go, daemon=True).start()

    def close(self, timeout=1.0):
        pass


be = ao.FakeBackend(speed=4.0)
ren = FakeRenderer()
v = cv.PackVoice("engineer", player=ao.Player(be, "Headset"), renderer=ren)
check(v.wait_ready(5) and v.pack is not None and v.pack.id == "engineer", "PackVoice loads the chosen pack")
for text in ("Brake marker, T4", "T4: lighter brake", "Down three tenths", "New best, up two tenths", "Good, 84",
             "T7: more speed, 6 down", "T3, braked early", "Down 1.4 seconds"):
    keys = PACK.compose(text)
    check(keys is not None and abs(v.duration(text) - PACK.duration_ms(keys) / 1000.0) < 1e-9,
          f"duration('{text}') is the pack's exact length ({v.duration(text):.3f} s)")
check(v.duration("Debrief coach is ready") is None, "an uncovered phrase has no length until Windows speech renders it")
v.prepare(["Brake marker, T4", "Down three tenths", "Debrief coach is ready"])
check(ren.asked == [("Debrief coach is ready", 0)], f"only the uncovered phrase goes to Windows speech ({ren.asked})")
wait_for(lambda: v.duration("Debrief coach is ready"), 2)
check(abs((v.duration("Debrief coach is ready") or 0) - 0.4) < 0.01, "then its rendered length is known")
writes0 = be.counts["write"]
check(v.say("Brake marker, T4", priority=3), "say a pack phrase")
wait_for(lambda: v.history and v.history[-1]["t_end"], 3)
h0 = v.history[-1]
check(h0["source"] == "pack" and h0["ok"] and be.counts["write"] == writes0 + 1, "played from the pack, once, to the end")
wl = [x for x in be.log if x[0] == "write"][-1]
check(wl[2] == len(PACK.render(PACK.compose("Brake marker, T4"))), "the bytes played are the pack's render")
check(v.say("Debrief coach is ready", priority=1), "say a phrase only Windows speech has")
wait_for(lambda: v.history[-1]["t_end"] and v.history[-1]["text"] == "Debrief coach is ready", 3)
check(v.history[-1]["source"] == "windows" and v.said_fallback == 1, "played through the same device")
check(v.repeat() == "Debrief coach is ready", "Repeat that says the last cue again")
wait_for(lambda: v.history[-1]["kind"] == "repeat" and v.history[-1]["t_end"], 3)
check(v.history[-1]["ok"], "the repeat played")
# volume: applied to the audio, not the pack
v.set(volume=50)
v.say("Down three tenths", priority=1)
wait_for(lambda: v.history[-1]["text"] == "Down three tenths" and v.history[-1]["t_end"], 3)
check(abs(np.abs(np.frombuffer(v._pcm(v._samples("Down three tenths")[0]), "<i2")).max()
          - 0.5 * np.abs(np.frombuffer(PACK.render(PACK.compose("Down three tenths")), "<i2")).max()) <= 1,
      "volume 50 halves the samples")
# speaking speed: only Windows speech is rendered again
ren.asked.clear()
v.set(rate=3)
wait_for(lambda: ren.asked, 2)
check(ren.asked == [("Debrief coach is ready", 3)], f"a speed change re-renders only Windows speech ({ren.asked})")
# a phrase that has to be rendered on the spot is dropped when it can't be ready in time
slow = FakeRenderer(delay_s=1.2)
v2 = cv.PackVoice("engineer", player=ao.Player(ao.FakeBackend(speed=4.0)), renderer=slow)
v2.wait_ready(5)
v2.say("Something unusual", priority=1)
time.sleep(1.5)
check(not v2.history, "an on-the-spot phrase that is ready too late is not said")
check(v2.preview() and wait_for(lambda: v2.history and v2.history[-1]["t_end"], 8) and v2.history[-1]["kind"] == "preview",
      "Preview plays the pack's sample tape")
v2.close()
st = v.status()
check(st["backend"] == "pack" and st["pack"]["id"] == "engineer" and "Kokoro" in st["pack"]["license"] and
      st["device"]["device"] == "Headset" and "ducking" not in st, "status: pack, licence, device")
v.close()
# no pack at all: Windows speech says everything, through the device
v3 = cv.PackVoice("nope", player=ao.Player(ao.FakeBackend(speed=4.0)), renderer=FakeRenderer(),
                  bundled_dir=TMP / "no-packs")
v3.wait_ready(3)
check(v3.pack is None and v3.status()["backend"] == "windows speech" and v3.say("Brake marker, T4"),
      "no pack: Windows speech, still through the chosen device")
v3.close()
# the factory
s = lc.Settings.from_dict({"voice_pack": "engineer", "device": "Headset", "duck": True, "duck_level": 30})
check(not hasattr(s, "duck"), "an old settings file's ducking keys are ignored")
fac = cv.make_coach_voice(s, backend=ao.FakeBackend(), renderer=FakeRenderer(), state_dir=TMP)
fac.wait_ready(5)
check(fac.pack is not None and fac.pack.id == "engineer" and fac.player.device == "Headset",
      "make_coach_voice: the chosen pack and device")
fac.close()

# ================================================================ 5. the "already fixed" rule and the cue log
print("-- already fixed + cue log")
TEL = TMP / "tel"
TEL.mkdir()
refp, _t = B["make"](TEL / "mclaren570sgt4_zolder gp ref.ibt", [{}, {}, {}])
ref = lc.build_reference(refp)
EARLY3 = {3: (2400.0, 18.0, 8.0)}               # T3 braked early
laps = [{"corners": EARLY3}, {}, {}, {"corners": EARLY3}, {}, {"corners": EARLY3}, {"corners": EARLY3}, {}]
cur, _times = B["make"](TEL / "mclaren570sgt4_zolder gp cur.ibt", laps)
weak = [{"id": "C3", "apex_m": 2400.0, "avg_avail": 0.3}]
events = []


def play_logged(path, settings=None):
    src = lc.IbtPlaybackSource(path, speed=0)
    fv = vc.FakeVoice(durations={t: PACK.duration_ms(PACK.compose(t)) / 1000.0 for t in
                                 ("Brake marker, T3", "T3, braked early") if PACK.compose(t)})
    coach = lc.Coach(ref, weak, settings or lc.Settings(top_n=1), duration=fv.duration)
    coach.on_event = events.append
    lap_of = {}
    while True:
        smp = src.read()
        if smp is None:
            break
        for cue in coach.feed(smp):
            fv.say(cue.text, priority=cue.priority, t=cue.spoken_t)
            lap_of[len(fv.said) - 1] = smp["Lap"]
    return coach, fv, lap_of


coach, fv, lap_of = play_logged(cur)
said = [(lap_of[i], x["text"]) for i, x in enumerate(fv.said)]
print("   cues:", said)
early = [lap for lap, t in said if t == "T3, braked early"]
check(1 not in early and any(e["ev"] == "not_made" and e.get("lap") == 1 and e.get("why") == "cued recently"
                             for e in events), f"lap 1: the brake marker was just said, so the feedback waits ({early})")
fixed = [e for e in events if e["ev"] == "corner_fixed"]
check(len(fixed) == 1 and fixed[0]["lap"] == 3 and fixed[0]["corner"] == "C3",
      f"T3 counts as fixed after 2 good laps (laps 2 and 3) ({[(e.get('lap'), e.get('corner')) for e in fixed]})")
check(4 not in early, "lap 4: one slip on a fixed corner is not cued")
slip = [e for e in events if e["ev"] == "not_made" and e.get("why") == "corner fixed (one slip)"]
check(len(slip) == 1 and slip[0]["lap"] == 4, "...and the log says why")
check(not any(lap == 5 and t == "Brake marker, T3" for lap, t in said), "no brake marker for a fixed corner")
reg = [e for e in events if e["ev"] == "corner_regressed"]
check(len(reg) == 1 and reg[0]["lap"] == 6 and 6 in early, f"2 slips in 3 laps: it is back, and cued (lap 6) ({early})")
spoken_ev = [e for e in events if e["ev"] == "spoken"]
check(len(spoken_ev) == len(fv.said), f"every spoken cue is in the log ({len(spoken_ev)} = {len(fv.said)})")
fb = [e for e in spoken_ev if e["kind"] == "feedback"]
check(fb and all(e.get("why", "").startswith("braked") and e["measured"]["early_m"] > 10 and
                 e["measured"]["ref_brake_point_m"] > 0 for e in fb), "feedback cues carry their reason and numbers")
check(all(isinstance(e.get("dist_m"), int) and isinstance(e.get("kmh"), float) and isinstance(e.get("lap"), int)
          and e.get("dur_s") for e in spoken_ev), "spoken lines carry lap, metres, km/h and the cue's length")
check(all(e["next_brake_s"] >= e["dur_s"] + lc.Settings().end_before_brake_s - 1e-6 for e in spoken_ev
          if e.get("next_brake_s") is not None), "every cue ended at least 1 s before the next brake point")
made = [e for e in events if e["ev"] == "made"]
check(all(e.get("why") for e in made), "every cue made says why")
drops = [e for e in events if e["ev"] == "dropped"]
check(all(e.get("dropped") for e in drops), f"every dropped cue says why ({sorted({e['dropped'] for e in drops})})")
# with the rule off (fixed_laps very high) lap 4's slip would have been said
events.clear()
coach_b, fv_b, lap_b = play_logged(cur, lc.Settings(top_n=1, fixed_laps=10))
said_b = [(lap_b[i], x["text"]) for i, x in enumerate(fv_b.said)]
check(any(lap == 4 and t == "T3, braked early" for lap, t in said_b), "without the rule, lap 4 would have been cued")

# a car alongside: what is being said is cut off, and the log says so
events.clear()
trafp, _ = B["make"](TEL / "mclaren570sgt4_zolder gp traffic.ibt", [{}, {"clr": [(0.20, 0.40, 2)]}, {}])
play_logged(trafp)
check(any(e["ev"] == "cancelled" and e["why"] == "car alongside" for e in events), "car alongside: logged as a cut-off")

# the stress tool (tools/coach_stress.py) on these files: counts cues, never too close to a brake point
import importlib.util  # noqa: E402
import shutil  # noqa: E402
_spec = importlib.util.spec_from_file_location("coach_stress", ROOT / "tools" / "coach_stress.py")
stress = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stress)
STR = TMP / "stress"
STR.mkdir()
for f in (refp, cur):
    shutil.copy(f, STR / f.name)
tot = stress.run(STR, "engineer", 1)
check(tot["files"] == 2 and tot["cues"] > 0 and tot["max_per_lap"] <= lc.Settings().max_per_lap and
      tot["short"] == 0 and tot["overlaps"] == 0 and not tot["uncovered"],
      f"stress tool: {tot['cues']} cues, at most {tot['max_per_lap']} a lap, none too close, none overlapping")

# the cue log file: content, privacy, rotation, size
print("-- cue log file")
LOGS = TMP / "coach-logs"
for i in range(33):                                # 33 old logs
    f = LOGS / f"2026-10-{(i % 28) + 1:02d} Old Car {i} Track.jsonl"
    LOGS.mkdir(exist_ok=True)
    f.write_text('{"ev":"session"}\n', encoding="utf-8")
    os.utime(f, (time.time() - 10000 + i, time.time() - 10000 + i))
(LOGS / "notes.txt").write_text("mine", encoding="utf-8")
lg = cuelog.CueLog(LOGS, "McLaren 570S GT4 / C:\\Users\\Swazi", "Zolder: GP", header={"settings": lc.Settings().to_dict()})
names = sorted(p.name for p in LOGS.iterdir() if cuelog.NAME_RE.match(p.name))
check(len(names) == cuelog.KEEP and lg.path.name in names, f"rotation keeps the newest {cuelog.KEEP} ({len(names)})")
check((LOGS / "notes.txt").exists() and not (LOGS / "2026-10-01 Old Car 0 Track.jsonl").exists(),
      "the oldest logs go, other files are left alone")
check(re.fullmatch(r"\d{4}-\d{2}-\d{2} McLaren 570S GT4 C Users Swazi Zolder GP\.jsonl", lg.path.name) is not None,
      f"file name: date car track, plain characters only ({lg.path.name})")
lg.write({"ev": "spoken", "t": 12.5, "lap": 3, "text": "Brake marker, T3", "driver": "Swazi Williams",
          "file": "C:\\Users\\Swazi\\x.ibt", "measured": {"min_kmh": 101.5, "name": "secret"}})
rows = cuelog.read_log(LOGS, lg.path.name)
raw = lg.path.read_text(encoding="utf-8")
check(rows[0]["ev"] == "session" and rows[1]["text"] == "Brake marker, T3", "rows read back")
check("Swazi" not in raw.replace("C Users Swazi", "") and "secret" not in raw and "driver" not in raw and "x.ibt" not in raw,
      "nothing personal is written (only known keys)")
check(cuelog.read_log(LOGS, "../settings.toml") == [] and cuelog.read_log(LOGS, "notes.txt") == [],
      "only log files can be read")
small = cuelog.CueLog(TMP / "small", "Car", "Track", max_bytes=600)
for i in range(30):
    small.write({"ev": "made", "text": "Down three tenths", "t": float(i)})
check(small.full and small.path.stat().st_size < 900 and cuelog.read_log(TMP / "small", small.path.name)[-1]["ev"] == "log_full",
      "a log stops growing at its size limit")

# ================================================================ 6. the runner: repeat, mute for a minute, the log
print("-- runner: repeat, mute for 1 minute, log")


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Endless:
    """The ref lap again and again, as a live source (no realtime pacing)."""
    realtime = False
    interval = 0.0
    done = False
    state = "connected"

    def __init__(self, path):
        self.src = lc.IbtPlaybackSource(path, speed=0)
        self.reads = 0
        self.gate = threading.Event()
        self.gate.set()

    def info(self):
        return {"car": {"name": "McLaren 570S GT4", "path": "mclaren570sgt4"},
                "track": {"name": "Circuit Zolder", "code": "zolder gp"}, "length_m": 3980.0, "driver": "Swazi Williams"}

    def read(self):
        self.gate.wait(2)
        s = self.src.read()
        if s is None:
            self.src = lc.IbtPlaybackSource(self.src.path, speed=0)
            s = self.src.read()
        self.reads += 1
        time.sleep(0.0005)
        return s

    def close(self):
        pass


clock = Clock()
fvr = vc.FakeVoice()
src = Endless(cur)
r = lc.LiveRunner(src, fvr, settings=lc.Settings(top_n=1), reference=ref, weak_corners=weak, clock=clock,
                  log_dir=LOGS)
r.start()
wait_for(lambda: fvr.said, 20)
check(bool(fvr.said), "the runner speaks")
check(r.cuelog is not None and r.cuelog.path.exists() and "McLaren 570S GT4 Zolder" in r.cuelog.path.name,
      f"a cue log is opened when the car and track are known ({r.cuelog and r.cuelog.path.name})")
n0 = len(fvr.said)
check(r.repeat() == fvr.said[-1]["text"] and len(fvr.said) == n0 + 1 and fvr.said[-1]["priority"] == 9,
      "Repeat that (FakeVoice): the last cue again, at once")
left = r.mute_for(60)
check(left == 60 and r.coach.settings.muted and r.status()["quiet_left"] == 60.0, "Mute for 1 minute: muted")
check("quiet for 60 s more" in r.status()["text"], f"status says so: {r.status()['text']}")
n1 = len(fvr.said)
reads = src.reads
wait_for(lambda: src.reads > reads + 3000, 20)
check(len(fvr.said) == n1, "nothing is said during the minute")
clock.t += 61
wait_for(lambda: not r.coach.settings.muted, 5)
check(not r.coach.settings.muted and r.quiet_left() is None, "after the minute it talks again by itself")
r.mute_for(60)
r.unmute()
check(not r.coach.settings.muted and r.quiet_left() is None, "Unmute ends the minute early")
r.stop()
rows = cuelog.read_log(LOGS, r.cuelog.path.name)
evs = {x["ev"] for x in rows}
check({"session", "reference", "made", "spoken", "repeated", "muted"} <= evs, f"the log has every kind of event ({sorted(evs)})")
check(not any("Swazi" in json.dumps(x) for x in rows), "his name (from iRacing) is not in the log")
summ = cuelog.summary(rows, r.cuelog.path.name, "2.17.0")
check("McLaren 570S GT4 at Zolder" in summ and "Spoken (lap" in summ and "Brake marker, T3" in summ and len(summ) <= 6000,
      "Copy for Claude: a compact summary")
check("—" not in summ and "–" not in summ, "no long dashes in the summary")
lst = cuelog.list_logs(LOGS)
check(lst[0]["name"] == r.cuelog.path.name and lst[0]["spoken"] >= 1, "the newest log is listed first, with counts")

# ================================================================ 7. rehearsal source
print("-- rehearsal")
ch = coach2.ChainPlayback([refp, cur], speed=0)
ch.interval = 0
n = 0
p0 = ch.progress()
while ch.read() is not None:
    n += 1
    if n == 2000:
        pm = ch.progress()
check(p0["fraction"] == 0.0 and 0 < pm["fraction"] < 1 and ch.progress()["fraction"] == 1.0 and ch.done,
      f"progress over both files ({p0['fraction']} -> {pm['fraction']} -> {ch.progress()['fraction']})")
check(ch.progress()["files"] == 2 and ch.state == "finished", "two files, then finished")
# real speed: a runner on a rehearsal source paces itself, stops when asked
ch = coach2.ChainPlayback([cur], speed=1.0)
fr = vc.FakeVoice()
rr = lc.LiveRunner(ch, fr, settings=lc.Settings(top_n=1), reference=ref, weak_corners=weak, mode="rehearsal",
                   log_dir=LOGS)
rr.start()
time.sleep(1.0)
pr = rr.status()["progress"]
check(rr.status()["mode"] == "rehearsal" and rr.status()["text"].startswith("Rehearsal"), rr.status()["text"])
check(pr and 0 < pr["at_s"] <= 1.6, f"plays at real speed ({pr})")
check(rr.stop(2) and not rr.running(), "Stop ends a rehearsal")
check(rr.cuelog is not None and rr.cuelog.path.name.endswith(" rehearsal.jsonl"), "a rehearsal has its own log")

# ================================================================ 8. the server and the window
print("-- server")
LIB, IRACING = TMP / "lib", TMP / "iRacing"
STEL = IRACING / "telemetry"
ENV, PW = {}, {}


def srv():
    if "srv" not in ENV:
        LIB.mkdir(parents=True, exist_ok=True)
        STEL.mkdir(parents=True, exist_ok=True)
        Path(os.environ["DEBRIEF_SETTINGS"]).write_text(
            'token = "g61-test-token-ABCDEFGHIJKLMNOP"\n'
            f"library_folder = '{LIB}'\napi_base = \"http://127.0.0.1:9/api/v1/\"\niracing_folder = '{IRACING}'\n",
            encoding="utf-8")
        app.LOG_FILE = str(TMP / "debrief.log")
        s = S.DebriefServer(heartbeat_timeout=3600)
        s.coach_audio_factory = lambda: ENV.setdefault("be", ao.FakeLatencyBackend(speed=4.0, latency_s=0.32))
        ENV["voices"] = []

        def voice_factory(settings):
            v = cv.make_coach_voice(settings, user_dir=coach2.packs_dir(), backend=s.coach_audio_factory(),
                                    renderer=FakeRenderer(), state_dir=TMP)
            ENV["voices"].append(v)
            return v
        s.coach_voice_factory = voice_factory
        threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
        ENV["srv"] = s
        ENV["opener"] = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return ENV["srv"]


def call(rel, body=None, header=True, raw=None, ctype=None):
    s = srv()
    h = {"X-Debrief": "1"} if header else {}
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode("utf-8"))
    if data is not None:
        h["Content-Type"] = ctype or "application/json"
    rq = urllib.request.Request(s.url + rel, headers=h, data=data, method="POST" if data is not None else "GET")
    try:
        with ENV["opener"].open(rq, timeout=120) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        b = e.read()
        try:
            return e.code, json.loads(b)
        except ValueError:
            return e.code, b


def no_long_dash(obj, where):
    t = json.dumps(obj, ensure_ascii=False)
    check("—" not in t and "–" not in t, f"no long dash in {where}")


def coach_status():
    return call("api/coach/status")[1]


s = srv()
code, j = call("api/coach/voices")
check(code == 200 and j["ok"] and [p["id"] for p in j["packs"]][0] == "engineer" and len(j["packs"]) == 1,
      f"voices: George is the one bundled voice ({[p['id'] for p in j.get('packs', [])]})")
check(j["devices"] == ["Speakers", "Headset"] and j["settings"]["voice_pack"] == "engineer" and j["settings"]["device"] == "",
      "voices: the device list and the defaults")
code, _ = call("api/coach/voices", header=False)
check(code == 404, "voices needs the window's header")
code, r = call("api/coach/voice-settings", {"settings": {"voice_pack": "engineer", "device": "Headset"}})
check(code == 200 and r["ok"] and r["settings"]["voice_pack"] == "engineer" and r["settings"]["device"] == "Headset",
      "voice settings saved")
saved = json.loads((TMP / "coach.json").read_text(encoding="utf-8"))["settings"]
check(saved["voice_pack"] == "engineer" and saved["device"] == "Headset" and "duck" not in saved, "...in coach.json")
for bad in ({"voice_pack": "../x"}, {"device": 5}, {"duck": True}, {"rate": 3}):
    code, r = call("api/coach/voice-settings", {"settings": bad})
    check(code == 400 and not r["ok"], f"refused: {bad} ({r.get('error')})")
code, r = call("api/coach/settings", {"settings": {"volume": 80}})
saved = json.loads((TMP / "coach.json").read_text(encoding="utf-8"))["settings"]
check(code == 200 and saved["voice_pack"] == "engineer" and saved["volume"] == 80, "the old Settings form keeps the voice settings")
code, r = call("api/coach/settings", {"settings": {"audio_latency_s": 0.9}})
check(code == 400, "the audio delay can't be set past 0.5 s by hand")
# preview and measure while the coach is off
code, r = call("api/coach/preview", {})
check(code == 200 and r["pending"], "Preview starts")
st = wait_for(lambda: (lambda x: x if (x.get("preview") or {}).get("state") in ("playing", "done", "error") else None)(coach_status()), 10)
check(st and st["preview"]["state"] in ("playing", "done") and "sample" in st["preview"]["text"], f"preview: {st and st['preview']}")
code, r = call("api/coach/measure", {})
st = wait_for(lambda: (lambda x: x if (x.get("measure") or {}).get("state") in ("done", "error") else None)(coach_status()), 15)
check(st and st["measure"]["state"] == "done" and abs(st["measure"]["value"] - 0.08) < 0.03,
      f"Measure audio delay: {st and st['measure']}")
saved = json.loads((TMP / "coach.json").read_text(encoding="utf-8"))["settings"]
check(abs(saved["audio_latency_s"] - st["measure"]["value"]) < 1e-9, "the measured delay is saved for the coach")
check(abs(s.coach_settings().audio_latency_s - saved["audio_latency_s"]) < 1e-9, "...and used by the next coach")
# repeat / quiet need a running coach
for route in ("api/coach/repeat", "api/coach/quiet"):
    code, r = call(route, {})
    check(code == 400 and r["error"] == "Start the coach first.", f"{route} without a coach")

# importing a pack: a folder and a zip, refused when it hides a program
mine = TMP / "mypack"
clips = {k: (np.concatenate([np.zeros(200), 0.2 * np.sin(np.arange(4000) / 7.0), np.zeros(200)]), k, "whole")
         for k in ("brake_marker_turn_3", "down_0_3")}
vp.write_pack(mine, {"id": "myvoice", "name": "My voice", "voice": "me", "voice_credit": "Recorded by me",
                     "license": "mine"}, clips, 16000, 90)
code, r = call("api/coach/import-pack", {"folder": str(mine)})
check(code == 200 and r["ok"] and r["id"] == "myvoice" and (coach2.packs_dir() / "myvoice" / "manifest.json").is_file(),
      f"a folder pack is added ({r})")
code, j = call("api/coach/voices")
check(any(p["id"] == "myvoice" and not p["bundled"] for p in j["packs"]), "and listed as his")
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as z:
    for f in mine.iterdir():
        z.write(f, "evil/" + f.name)
    z.writestr("evil/run.exe", b"MZ\x90\x00")
code, r = call("api/coach/import-pack-zip?name=evil.zip", raw=buf.getvalue(), ctype="application/octet-stream")
check(code == 400 and not r["ok"] and any("program" in p for p in r["problems"]), f"a zip hiding a program is refused ({r})")
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as z:
    m = json.loads((mine / "manifest.json").read_text(encoding="utf-8"))
    m["id"] = "zipvoice"
    z.writestr("manifest.json", json.dumps(m))
    for f in mine.glob("*.wav"):
        z.write(f, f.name)
code, r = call("api/coach/import-pack-zip?name=zipvoice.zip", raw=buf.getvalue(), ctype="application/octet-stream")
check(code == 200 and r["ok"] and r["id"] == "zipvoice", f"a .zip pack is added ({r})")
code, r = call("api/coach/import-pack", {"folder": str(TMP / "nowhere")})
check(code == 400 and "wasn't found" in r["error"], "a missing folder is refused")

# the coach on: the voice, repeat, quiet, logs
holder = {}


class Holding:
    """The fake session, read at real speed for the window tests (stops at the end)."""

    def __init__(self, path):
        self.src = lc.IbtPlaybackSource(path, speed=8.0)
        self.interval = self.src.interval
        self.realtime = True
        self.done = False
        self.state = "connected"

    def info(self):
        return {"car": {"name": "McLaren 570S GT4", "path": "mclaren570sgt4"},
                "track": {"name": "Circuit Zolder", "code": "zolder gp"}, "length_m": 3980.0}

    def read(self):
        smp = self.src.read()
        if smp is None:
            self.done = True
        return smp

    def close(self):
        pass


import shutil  # noqa: E402
for f in (refp, cur):
    shutil.copy(f, STEL / f.name)
    os.utime(STEL / f.name, (time.time() - 3600, time.time() - 3600))
s.coach_source_factory = lambda: Holding(STEL / cur.name)
code, r = call("api/coach/start", {})
check(code == 200 and r["ok"], f"the coach starts ({r.get('error')})")
st = wait_for(lambda: (lambda x: x if x.get("spoken") else None)(coach_status()), 40)
check(bool(st), "it speaks")
vs = st["voice"]
check(vs["backend"] == "pack" and vs["pack"]["id"] == "engineer" and vs["device"]["device"] == "Headset",
      f"through the George pack and the headset ({vs.get('pack')}, {vs.get('device')})")
check(st["keys"]["repeat"]["key"] == "Ctrl+Shift+R" and st["keys"]["quiet"]["key"] == "Ctrl+Shift+N", "the new keys are named")
check(st["log"] and st["log"].endswith(".jsonl"), f"the session's cue log ({st['log']})")
code, r = call("api/coach/repeat", {})
check(code == 200 and r["ok"] and r["said"] and r["text"] == st["spoken"][-1]["text"], f"repeat: {r}")
code, r = call("api/coach/quiet", {})
check(code == 200 and r["quiet_left"] == 60, "mute for 1 minute")
st = coach_status()
check(st["muted"] and st["quiet_left"] and "quiet for" in st["text"], st["text"])
code, r = call("api/coach/rehearse", {"id": "x"})
check(code == 400 and "Stop the coach first" in r["error"], "no rehearsal while coaching live")
call("api/coach/stop", {})
v_live = ENV["voices"][-1]
check(wait_for(lambda: v_live.player._thread is not None and not v_live.player._thread.is_alive(), 3),
      "stopping the coach closes its sound device thread")
code, j = call("api/coach/logs")
check(code == 200 and j["logs"] and j["logs"][0]["spoken"] >= 1, "logs: the session is listed")
name = j["logs"][0]["name"]
code, j = call("api/coach/log?" + urllib.parse.urlencode({"name": name}))
check(code == 200 and j["rows"] and any(x["ev"] == "spoken" for x in j["rows"]), "log: its rows")
code, j2 = call("api/coach/log?" + urllib.parse.urlencode({"name": name, "summary": "1"}))
check(code == 200 and "Spoken (lap" in j2["text"], "log: Copy for Claude text")
code, j = call("api/coach/log?" + urllib.parse.urlencode({"name": "../coach.json"}))
check(code == 400, "log: only log files")
no_long_dash(j2, "the summary")

# rehearsal through the server (a Practice session)
code, r = call("api/setuplab/read", {})
end = time.time() + 180
while time.time() < end and call("api/status")[1].get("state") == "running":
    time.sleep(0.2)
code, js = call("api/sessions-ibt")
ss = js.get("sessions") or []
check(code == 200 and ss, f"the practice sessions are listed ({len(ss)})")
if ss:
    code, r = call("api/coach/rehearse", {"id": ss[0]["id"]})
    check(code == 200 and r["ok"], f"rehearse a session ({r.get('error')})")
    st = wait_for(lambda: (lambda x: x if (x.get("progress") or {}).get("at_s") else None)(coach_status()), 15)
    check(st and st["mode"] == "rehearsal" and st["rehearsal"]["state"] == "playing" and st["text"].startswith("Rehearsal"),
          f"rehearsal status: {st and st['text']}")
    code, r2 = call("api/coach/start", {})
    check(code == 200 and r2.get("already"), "Start while rehearsing: the one runner is already on")
    call("api/coach/stop", {})
    st = coach_status()
    check(not st["running"], "Stop ends the rehearsal")
code, r = call("api/coach/rehearse", {"id": "no such session"})
check(code == 400, "an unknown session can't be rehearsed")
no_long_dash(coach_status(), "coach/status")


# ---------------------------------------------------------------- the window
def browser():
    if "b" not in PW:
        try:
            from playwright.sync_api import sync_playwright
            PW["pw"] = sync_playwright().start()
            PW["b"] = PW["pw"].chromium.launch()
        except Exception:  # noqa: BLE001
            print("  SKIPPED: Playwright/Chromium not available")
            PW["b"] = None
    return PW["b"]


def window():
    shots = os.environ.get("DEBRIEF_SHOTS")
    if shots:
        Path(shots).mkdir(parents=True, exist_ok=True)
    b = browser()
    if not b:
        return
    s.coach_source_factory = lambda: Holding(STEL / cur.name)
    for dark in (False, True):
        ctx = b.new_context(viewport={"width": 1320, "height": 900}, color_scheme="dark" if dark else "light",
                            device_scale_factor=2 if shots else 1)
        pg = ctx.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
        pg.route("**/api/sync", lambda rt: rt.fulfill(body='{"ok":true,"skipped":true}', content_type="application/json"))
        pg.route("**/api/update*", lambda rt: rt.fulfill(body='{"ok":true,"skipped":true}', content_type="application/json"))
        try:
            pg.goto(s.url)
            pg.wait_for_function("() => document.querySelector('.navbtn[data-nav=coach]')")
            pg.keyboard.press("Control+9")
            pg.wait_for_selector("#vCoach", state="visible")
            pg.wait_for_function("() => document.querySelector('#coPack option')")
            check(pg.locator("#coRepeat").is_disabled() and pg.locator("#coQuiet1").is_disabled(),
                  "Repeat that and Mute for 1 minute are there, off while the coach is off")
            check(pg.text_content("#coRepeat").strip().startswith("Repeat that") and "Ctrl+Shift+N" in pg.text_content("#coQuiet1"),
                  "their keys are shown")
            pg.wait_for_function("() => document.querySelector('#coRhPick').value")
            check(pg.locator("#coRhPick option").count() >= 1 and not pg.locator("#coRhGo").is_disabled(),
                  "the Practice sessions are offered for a rehearsal")
            pg.evaluate("() => { document.getElementById('coVoice').open = true; document.getElementById('coLogs').open = true }")
            check(pg.input_value("#coPack") == "engineer" and pg.input_value("#coDev") == "Headset" and pg.locator("#coDuck").count() == 0,
                  "Voice and sound shows the saved choices")
            opts = pg.eval_on_selector_all("#coDev option", "o => o.map(x => x.textContent)")
            check(opts[0] == "Windows default" and "Speakers" in opts, f"Play through: {opts}")
            check("Kokoro" in pg.text_content("#coPackNote") or "Apache" in pg.text_content("#coPackNote"),
                  f"the voice's credit is shown ({pg.text_content('#coPackNote')})")
            check(re.search(r"\d+ ms", pg.text_content("#coLat")) is not None, "the measured delay is shown")
            pg.wait_for_function("() => document.querySelectorAll('#coLogList tr button').length")
            pg.evaluate("() => [...document.querySelectorAll('#coLogList tr')].find(tr => tr.cells[3] && "
                        "tr.cells[3].textContent !== '0').querySelector('button').click()")
            pg.wait_for_function("() => document.querySelectorAll('#coLogRows tr').length")
            check("Said" in pg.text_content("#coLogRows"), "the log viewer lists what was said")
            if not dark:
                pg.click("#coStart")
                pg.wait_for_function("() => !document.getElementById('coRepeat').disabled", timeout=20000)
                pg.wait_for_function("() => document.querySelectorAll('#coSpoken li:not(.note)').length", timeout=40000)
                pg.click("#coRepeat")
                pg.wait_for_function("() => /Said again|Nothing said/.test(document.getElementById('coMsg').textContent)")
                check("Said again" in pg.text_content("#coMsg"), pg.text_content("#coMsg"))
                check("Voice: " in pg.text_content("#coVoiceLine") and "Headset" in pg.text_content("#coVoiceLine"),
                      pg.text_content("#coVoiceLine"))
                pg.keyboard.press("Control+Shift+N")
                pg.wait_for_function("() => /Quiet for/.test(document.getElementById('coQuiet1').textContent)")
                check(True, "Ctrl+Shift+N mutes for a minute from the window")
            else:
                pg.click("#coStart")
                pg.wait_for_function("() => document.querySelectorAll('#coSpoken li:not(.note)').length", timeout=40000)
            for sel in ("#vCoach",):
                t = pg.text_content(sel)
                check("—" not in t and "–" not in t, f"no long dash on the Coach page ({'dark' if dark else 'light'})")
            if shots:
                pg.evaluate("() => { const b = document.getElementById('banner'); if (b) b.hidden = true }")
                hgt = pg.evaluate("() => document.getElementById('vCoach').scrollHeight")
                pg.set_viewport_size({"width": 1320, "height": min(int(hgt) + 60, 8000)})
                time.sleep(0.4)
                pg.screenshot(path=str(Path(shots) / f"coach2-{'dark' if dark else 'light'}.png"), full_page=True)
            call("api/coach/stop", {})
            check(not [e for e in errs if "favicon" not in e], f"no page errors ({errs[:3]})")
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            check(False, "the Coach page in the browser")
        finally:
            call("api/coach/stop", {})
            ctx.close()


window()
s.stop_coach()
coach2.on_shutdown(s)
if PW.get("b"):
    PW["b"].close()
    PW["pw"].stop()
s.shutdown()
s.server_close()
print("all passed" if not fails else f"FAILED {fails}")
sys.exit(1 if fails else 0)
