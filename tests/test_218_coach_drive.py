"""2.18 Coach you can use while driving: the driving view's state, mute said outright (and "Repeat that" silent while
muted), one reference lap named the same everywhere, faster rehearsals and skipping ahead, the one thing counted on
finished laps only, the coach's count on the Practice session page, and why a chosen voice can't play.
Run: python tests/test_218_coach_drive.py

Fake telemetry from test_213's lap builder; the server with stand-in voices (no Windows, no sound card)."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
TMP = Path(tempfile.mkdtemp(prefix="debrief-218c-"))
os.environ["DEBRIEF_SETTINGS"] = str(TMP / "settings.toml")
os.environ["DEBRIEF_CACHE_DIR"] = str(TMP / "cache")
os.environ["DEBRIEF_SCHEDULE_DIR"] = str(TMP / "schedule")
os.environ["DEBRIEF_UPDATE_STATE"] = str(TMP / "update.json")

from debrief import app, coachvoice as cv, cuelog, livecoach as lc, sessionibt, setuplab  # noqa: E402
from debrief import voice as vc, voicepack as vp  # noqa: E402
from debrief.desktop import coach2, server as S  # noqa: E402

fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


def wait_for(fn, timeout=10.0, step=0.01):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return fn()


def builder() -> dict:
    """test_213_livecoach's fake-track builder (make, BASE ...), without running its checks."""
    path = ROOT / "tests" / "test_213_livecoach.py"
    src = path.read_text(encoding="utf-8")
    cut = src.index("# ---------------------------------------------------------------- reference")
    ns = {"__name__": "coach_builder", "__file__": str(path)}
    exec(compile(src[:cut], str(path), "exec"), ns)   # noqa: S102  (our own test file)
    return ns


B = builder()
PACK = vp.find_pack("engineer")
SEP21 = 1_790_000_000                     # 2026-09-21: a header date that isn't the day he drove
TEL = TMP / "iRacing" / "telemetry"
TEL.mkdir(parents=True)
EARLY3 = {3: (2400.0, 18.0, 8.0)}         # T3 braked early: a slower lap
# Nov 3 is the quicker session (as in Maya's Practice list), Nov 2 the slower one; both headers say Sep 21
NOV3 = TEL / "mclaren570sgt4_zolder gp 2026-11-03 18-04-12.ibt"
NOV2 = TEL / "mclaren570sgt4_zolder gp 2026-11-02 19-30-00.ibt"
B["make"](NOV3, [{}, {}, {}], start_date=SEP21)
B["make"](NOV2, [{"corners": EARLY3}] * 6,
          start_date=SEP21)
OLD = time.time() - 3600
for f in (NOV3, NOV2):
    os.utime(f, (OLD, OLD))

# ================================================================ 1. one reference lap, named the same everywhere
print("-- reference lap")
r3 = lc.build_reference(NOV3)
check(r3.source["date"] == "2026-11-03" and r3.describe().endswith("from Nov 3"),
      f"the lap's day comes from the file name, as Practice shows it, not the header's Sep 21 ({r3.describe()})")
check(r3.source.get("when") == "2026-11-03 18:04", f"and its time ({r3.source.get('when')})")
idx = setuplab.index(TEL, cache_path=TMP / "sl.json")
ss = sessionibt.sessions(dict(idx, folder=str(TEL)))
dates = {s["date"][:10]: s for s in ss}
check(set(dates) == {"2026-11-03", "2026-11-02"}, f"Practice lists Nov 3 and Nov 2 ({sorted(dates)})")
ch = lc.reference_choice(idx, "mclaren570sgt4", "zolder gp")
found = lc.find_reference(TEL, "mclaren570sgt4", "zolder gp", index=idx, fresh_s=0)
check(ch is not None and found is not None and ch["file"] == NOV3.name and ch["text"] == found.describe()
      and abs(ch["lap_time"] - found.lap_time) < 1e-3,
      f"Practice's 'coach reference' and the coach's own reference are the same lap ({ch and ch['text']} / "
      f"{found and found.describe()})")
check(ch["when"] == dates["2026-11-03"]["date"], f"with the Practice list's own date ({ch['when']})")
check(abs(ch["lap_time"] - min(s["best"] for s in ss)) < 0.01,
      f"and it is the quickest lap Practice lists ({ch['lap_time']} vs {[s['best'] for s in ss]})")
check(lc.reference_choice(idx, "othercar", "zolder gp") is None, "another car: no reference named")
# the cue log names it the same way
LOGS = TMP / "logs"
evs = []
co = lc.Coach(found, [], lc.Settings())
co.on_event = evs.append
co.set_reference(found)
row = next(e for e in evs if e["ev"] == "reference")
check(row.get("ref_text") == found.describe() and row.get("ref_date") == "2026-11-03",
      f"the cue log's reference line names the same lap ({row.get('ref_text')})")
lg = cuelog.CueLog(LOGS, "McLaren 570S GT4", "Zolder")
lg.write(row)
summ = cuelog.summary(cuelog.read_log(LOGS, lg.path.name))
check(found.describe() in summ, "Copy for Claude names it too")

# a rehearsal adopts a lap from the file it plays: that file's day, never "just now"
co = lc.Coach(None, [], lc.Settings())
co.date_hint = "2026-11-02 19:30"
src = lc.IbtPlaybackSource(NOV2, speed=0)
info = src.info()
check(info.get("when") == "2026-11-02 19:30", f"a played file knows its day ({info.get('when')})")
co.hint(info["length_m"], info["car"], info["track"])
while (smp := src.read()) is not None and co.reference is None:
    co.feed(smp)
check(co.reference is not None and co.reference.source.get("played") and co.reference.describe().endswith("from Nov 2"),
      f"a lap adopted while rehearsing is dated by the session played ({co.reference and co.reference.describe()})")
check(lc.reference_why(co.reference) == "a clean lap from the session being rehearsed",
      f"and said so ({lc.reference_why(co.reference)})")

# ================================================================ 2. the one thing counted on finished laps only
print("-- hit count")
P = [{"lap": 2, "hit": True}, {"lap": 3, "hit": False}, {"lap": 4, "hit": True}]
c = lc.focus_count(P, 4)
check(c["count_hits"] == 1 and c["count_of"] == 2 and c["count_text"] == "Hit 1 of 2 so far",
      f"the pass on the lap still being driven waits for the line ({c['count_text']})")
c = lc.focus_count([{"lap": 1, "hit": True}], 1)
check(c["count_of"] == 0 and c["count_text"] == "Counted when this lap is done", f"before a lap is done: {c['count_text']}")
c = lc.focus_count([], 1, "No clean pass through the corner yet")
check(c["count_text"] == "No clean pass through the corner yet", "nothing measured yet: the focus's own words")
check(c["done_text"] == "Done when you hit it on 7 of 10 laps" and lc.done_rule() == (7, 10),
      "what counts as done: 7 of 10, the session score's own rule")
ten = [{"lap": k, "hit": k % 10 not in (1, 2, 3)} for k in range(1, 13)]
check(lc.focus_count(ten, 13)["done"] and not lc.focus_count(ten[:9], 13)["done"],
      "done once 7 of the last 10 finished laps hit it")


class Focus:
    """A stand-in practiceplan.LiveFocus: one hit per lap, measured as soon as the corner is passed."""
    plan_id, active, zone = "p1", True, None

    def __init__(self):
        self.passes = []
        self.plan = {"metric": "brake_point"}

    def feed(self, s, ok=True, lap=None):
        if ok and lap is not None and float(s.get("LapDistPct") or 0) > 0.5 and not any(p["lap"] == lap for p in self.passes):
            self.passes.append({"lap": lap, "hit": True, "value": 1})
        return None

    def vocabulary(self):
        return []

    def status(self):
        n = len(self.passes)
        return {"plan_id": "p1", "measured": n, "hits": n, "text": "T2: brake later", "corner": "T2",
                "progress": f"Hit {n} of {n} so far" if n else "No clean pass through the corner yet"}


co = lc.Coach(r3, [], lc.Settings())
co.set_focus(Focus())
src = lc.IbtPlaybackSource(NOV2, speed=0)
early = None
deltas = []
while (smp := src.read()) is not None:
    co.feed(smp)
    fs = co.status()["focus"]
    if co.focus.passes and early is None:
        early = fs
    if co.live_delta is not None:
        deltas.append((smp["Lap"], smp["LapDistPct"], co.live_delta))
check(early is not None and early["count_of"] == 0 and early["progress"] == "Counted when this lap is done",
      f"the first measured pass shows no count until its lap is done ({early and early['progress']})")
check(co.status()["focus"]["count_of"] >= 3, f"finished laps are counted ({co.status()['focus']['progress']})")

# ================================================================ 3. the delta to the reference, live
print("-- live delta")
ends = {}
for lap, pct, d in deltas:
    if pct > 0.97:
        ends[lap] = d
log = {x["lap"]: x for x in co.lap_log if x.get("delta") is not None}
pairs = [(ends[k], log[k]["delta"]) for k in log if k in ends]
check(deltas and pairs and all(abs(a - b) < 0.35 for a, b in pairs),
      f"the live delta at the end of each lap is close to the lap's own delta ({pairs[:4]})")
slow = [d for lap, pct, d in deltas if 0.7 < pct < 0.95 and lap in log and log[lap]["delta"] > 0.2]
check(slow and min(slow) > 0, "a slow lap reads as down on the reference after the slow corner")

# ================================================================ 4. the state in large type
print("-- driving view state")
check(lc.show_state("coaching", True, "live", False) == "coaching", "coaching")
check(lc.show_state("coaching", True, "live", True) == "muted", "muted wins")
check(lc.show_state("coaching", True, "rehearsal", False) == "rehearsing", "a rehearsal is never 'coaching'")
check(lc.show_state("no_reference", True, "rehearsal", False) == "rehearsing", "nor before its reference")
check(lc.show_state("coaching", True, "rehearsal", True) == "muted", "a muted rehearsal says muted")
check(lc.show_state("finished", False, "rehearsal", False) == "off" and lc.show_state("stopped", False, "live", True)
      == "off", "off when nothing runs")
check(lc.show_state("waiting", True, "live", False) == "waiting", "waiting for iRacing")

# ================================================================ 5. mute said outright, Repeat that silent while muted
print("-- mute")


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class RepeatVoice(vc.FakeVoice):
    def __init__(self):
        super().__init__()
        self.repeats = 0
        self.last = None

    def say(self, text, priority=0, t=None):
        self.last = text
        return super().say(text, priority=priority, t=t)

    def repeat(self):
        self.repeats += 1
        return self.last


clk = Clock()
fv = RepeatVoice()
r = lc.LiveRunner(lc.IbtPlaybackSource(NOV2, speed=0), fv, reference=r3, clock=clk)
check(r.set_muted(True, "the Mute button") and r.mute_state()["by"] == "the Mute button",
      "the first press of Mute mutes")
check(r.set_muted(True, "the Mute button") and r.coach.settings.muted, "pressing Mute again stays muted (not a toggle)")
st = r.mute_state()
check(st["on"] and st["since"] and abs(st["since"] - time.time()) < 5 and st["quiet_left"] is None,
      f"the mute says since when ({st})")
check(not r.set_muted(False) and r.mute_state() == {"on": False, "by": None, "since": None, "quiet_left": None},
      "Unmute unmutes and forgets who muted")
check(r.toggle(by="Ctrl+Shift+M") and not r.toggle(), "the key and the wheel still toggle")
fv.last = "Down three tenths"
r.set_muted(True)
check(r.repeat() is None and fv.repeats == 0, "Repeat that while muted says nothing")
rn = r.status()["repeat_note"]
check(rn and not rn["said"] and "muted" in rn["text"], f"and the status says why ({rn and rn['text']})")
r.set_muted(False)
check(r.repeat() == "Down three tenths" and fv.repeats == 1 and r.status()["repeat_note"] is None,
      "unmuted, Repeat that says it again")
r.mute_for(60)
check(r.mute_state()["quiet_left"] == 60.0 and r.mute_state()["by"] == "for 1 minute", "Mute for 1 minute")
r.set_muted(True, "the Mute button")
check(r.coach.settings.muted and r.quiet_left() is None, "Mute during the minute: muted until Unmute")
r.ask_pace()           # not running: False, nothing asked
st = r.status()
check(st["show"] == "off" and st["mute"]["on"], "status carries the mute for the window")

# ================================================================ 6. rehearsal: faster, and skipping ahead
print("-- rehearsal speed")
ip = lc.IbtPlaybackSource(NOV2, speed=1.0)
iv1 = ip.interval
ip.set_speed(4.0)
check(abs(ip.interval * 4 - iv1) < 1e-9, f"4x reads samples 4 times as often ({iv1:.4f} -> {ip.interval:.4f})")
chp = coach2.ChainPlayback([NOV2, NOV3], speed=2.0)
check(abs(chp.interval - 0.025) < 1e-9, f"ChainPlayback at 2x before the first read ({chp.interval})")
chp.read()
chp.set_speed(4.0)
check(chp.cur.speed == 4.0 and abs(chp.interval - chp.cur.interval) < 1e-9, "and switched to 4x while playing")
chp.close()
live = lc.LiveRunner(lc.IbtPlaybackSource(NOV2, speed=0), vc.FakeVoice())
check(not live.set_speed(4) and not live.skip("lap"), "live coaching can't be sped up or skipped")
reh = lc.LiveRunner(coach2.ChainPlayback([NOV2]), vc.FakeVoice(), mode="rehearsal")
check(reh.set_speed(4) and reh.coach.time_scale == 4.0 and reh.speed == 4.0, "a rehearsal plays at 4x")
check(not reh.set_speed(3), "only 1x, 2x or 4x")
reh.source.close()
d1 = lc.Coach(r3, [], lc.Settings()).duration("Brake marker, T3")
c4 = lc.Coach(r3, [], lc.Settings())
c4.time_scale = 4.0
check(abs(c4.duration("Brake marker, T3") - 4 * d1) < 1e-9, "at 4x a cue takes 4 times as long in the file's time")


def play_scaled(scale):
    co = lc.Coach(r3, [{"id": "C3", "apex_m": 2400.0, "avg_avail": 0.3}], lc.Settings(top_n=1),
                  duration=lambda t: PACK.duration_ms(PACK.compose(t)) / 1000.0 if PACK and PACK.compose(t) else None)
    co.time_scale = scale
    ev = []
    co.on_event = ev.append
    src = lc.IbtPlaybackSource(NOV2, speed=0)
    while (smp := src.read()) is not None:
        co.feed(smp)
    return [e for e in ev if e["ev"] == "spoken"]


sp1, sp4 = play_scaled(1.0), play_scaled(4.0)
st_ = lc.Settings()
bad = [e for e in sp4 if e.get("next_brake_s") is not None and
       e["next_brake_s"] + 0.06 < 4 * e["dur_s"] + st_.end_before_brake_s + 4 * st_.audio_latency_s]
check(sp4 and not bad, f"at 4x every cue still ends before its brake point ({len(sp4)} said, {len(bad)} late)")
check(len(sp4) <= len(sp1), f"fewer cues fit at 4x than at 1x ({len(sp4)} vs {len(sp1)})")
check(all(e.get("dur_s") and e["dur_s"] < 3 for e in sp4), "the log keeps each cue's real length")

print("-- skip ahead")
sk = lc.LiveRunner(coach2.ChainPlayback([NOV2]), vc.FakeVoice(), mode="rehearsal")
sk._skip = {"what": "lap", "lap": None, "laps": 0}
check([sk._skip_step({"Lap": x}) for x in (2, 2, 2, 3)] == [True, True, True, False] and sk._skip is None
      and sk.skip_note == "Skipped to lap 3.", f"next lap: skips to the line ({sk.skip_note})")
sk._skip = {"what": "cue", "lap": None, "laps": 0}
steps = [sk._skip_step({"Lap": x}) for x in (2, 3, 4, 5, 5)]
check(steps == [True, True, True, False, False] and "No cue in the next 3 laps" in sk.skip_note,
      f"next cue gives up after 3 laps ({steps}, {sk.skip_note})")
sk.source.close()


class SlowVoice(vc.FakeVoice):
    pass


fr = SlowVoice()
evs = []
rr = lc.LiveRunner(coach2.ChainPlayback([NOV2], speed=40.0), fr, reference=r3, mode="rehearsal",
                   weak_corners=[{"id": "C3", "apex_m": 2400.0, "avg_avail": 0.3}], settings=lc.Settings(top_n=1))
rr._on_event = lambda d: evs.append(d)
rr.coach.on_event = rr._on_event
rr.start()
wait_for(lambda: rr.coach._lap is not None and rr.coach._lap.num >= 2, 30)
lap0 = rr.coach._lap.num
n0 = len(fr.said)
check(rr.skip("lap"), "Next lap starts")
wait_for(lambda: rr._skip is None, 10)
lap1 = rr.coach._lap.num
check(lap1 == lap0 + 1 and rr.skip_note == f"Skipped to lap {lap1}.", f"it lands on the next lap ({lap0} -> {lap1})")
t_land = float(rr._latest["SessionTime"])
said_mid = [x for x in fr.said[n0:] if x["t"] is not None and x["t"] < t_land - 0.2 and
            x["t"] > t_land - 60 and x["t"] > (fr.said[n0 - 1]["t"] if n0 else 0) + 0.2]
check(not said_mid, f"nothing was said on the way ({[x['text'] for x in said_mid]})")
n1 = len(fr.said)
check(rr.skip("cue"), "Next cue starts")
wait_for(lambda: rr._skip is None, 10)
note = rr.skip_note
if note.startswith("Skipped to the next cue"):
    check(wait_for(lambda: len(fr.said) > n1, 3), f"the next cue is said ({note})")
else:
    check("No cue in the next 3 laps" in note or not rr.running(), f"or it says there was none ({note})")
rr.stop()
check(rr.status()["show"] == "off", "stopped: off")

# ================================================================ 7. why the chosen voice can't play
print("-- voice note")
packs = vp.list_packs(TMP / "nopacks")
good = [p for p in packs if p["ok"]]
gname = next(p["name"] for p in good if p["default"])
n = cv.voice_note("engineer", None, packs)
check(n["ok"] and n["text"] == "", "the default pack installed: nothing to say")
n = cv.voice_note("bm_lewis", None, packs)
check(not n["ok"] and "isn't installed" in n["text"] and gname in n["text"], f"a missing pack: why and what plays ({n['text']})")
broken = packs + [{"id": "mine", "name": "My voice", "ok": False, "problems": ["the manifest could not be read"]}]
n = cv.voice_note("mine", None, broken)
check(not n["ok"] and "damaged" in n["text"] and "manifest" in n["text"], f"a damaged pack: {n['text']}")
n = cv.voice_note("mine", {"backend": "pack", "pack": {"id": "engineer", "name": gname}}, broken)
check(not n["ok"] and n["using"] == gname and "My voice can't play" in n["text"], f"running on another pack: {n['text']}")
n = cv.voice_note("engineer", {"backend": "windows speech", "pack": None, "available": True,
                               "pack_error": "no voice pack could be loaded, so Windows speech says everything"}, packs)
check(not n["ok"] and "Windows speech says every cue instead" in n["text"], f"no pack loaded: {n['text']}")
n = cv.voice_note("engineer", {"backend": "none", "available": False, "reason": "spoken cues need Windows"}, packs)
check(not n["ok"] and "silent" in n["text"] and "need Windows" in n["text"], f"no voice at all: {n['text']}")
n = cv.voice_note("engineer", {"backend": "sapi", "available": True}, packs)
check(not n["ok"] and "sound device" in n["text"] and "Windows speech" in n["text"], f"no sound device: {n['text']}")
n = cv.voice_note("engineer", {"backend": "pack", "pack": {"id": "engineer", "name": gname}}, packs)
check(n["ok"] and n["text"] == "", "the chosen pack playing: nothing to say")
for x in ("—", "–"):
    check(all(x not in cv.voice_note(i, None, broken)["text"] for i in ("bm_lewis", "mine")), "no long dashes")

# ================================================================ 8. the coach's count kept for Practice
print("-- focus results")
check(cuelog.save_focus_result(LOGS, "plan-a", {"hits": 7, "of": 10, "car": "c", "track": "t", "x": "dropped"}),
      "a count is saved")
check(cuelog.focus_results(LOGS)["plan-a"] == {"hits": 7, "of": 10, "car": "c", "track": "t"}, "only its own keys")
for k in range(cuelog.RESULTS_KEEP + 5):
    cuelog.save_focus_result(LOGS, f"p{k}", {"hits": 1, "of": 1})
fr_ = cuelog.focus_results(LOGS)
check(len(fr_) == cuelog.RESULTS_KEEP and "plan-a" not in fr_ and f"p{cuelog.RESULTS_KEEP + 4}" in fr_,
      "the newest plans are kept")
check(cuelog.rotate(LOGS, keep=0) >= 1 and (LOGS / cuelog.RESULTS).exists(), "rotating the logs keeps the counts")

# a live runner writes its count once a lap (never a rehearsal)
LD = TMP / "logs2"
lr = lc.LiveRunner(lc.IbtPlaybackSource(NOV2, speed=0), vc.FakeVoice(), reference=r3, log_dir=LD,
                   focus_for=lambda car, track, L: Focus())
lr.start()
wait_for(lambda: not lr.running(), 60)
res = cuelog.focus_results(LD).get("p1")
check(res and res["of"] >= 3 and res["hits"] == res["of"] and res["done_text"].startswith("Done when"),
      f"the live coach keeps its count of finished laps ({res})")
LD2 = TMP / "logs3"
lr = lc.LiveRunner(lc.IbtPlaybackSource(NOV2, speed=0), vc.FakeVoice(), reference=r3, log_dir=LD2, mode="rehearsal",
                   focus_for=lambda car, track, L: Focus())
lr.start()
wait_for(lambda: not lr.running(), 60)
check(not cuelog.focus_results(LD2), "a rehearsal keeps no count (it isn't a drive)")

# ================================================================ 9. the server
print("-- server")
LIB, IRACING = TMP / "lib", TMP / "iRacing"
ENV = {}


def srv():
    if "srv" not in ENV:
        LIB.mkdir(parents=True, exist_ok=True)
        Path(os.environ["DEBRIEF_SETTINGS"]).write_text(
            'token = "g61-test-token-ABCDEFGHIJKLMNOP"\n'
            f"library_folder = '{LIB}'\napi_base = \"http://127.0.0.1:9/api/v1/\"\niracing_folder = '{IRACING}'\n",
            encoding="utf-8")
        app.LOG_FILE = str(TMP / "debrief.log")
        s = S.DebriefServer(heartbeat_timeout=3600)
        s.coach_voice_factory = lambda settings: ENV.setdefault("voices", []).append(RepeatVoice()) or ENV["voices"][-1]
        threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
        ENV["srv"] = s
        ENV["opener"] = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return ENV["srv"]


def call(rel, body=None):
    s = srv()
    h = {"X-Debrief": "1"}
    data = None if body is None else json.dumps(body).encode("utf-8")
    if data is not None:
        h["Content-Type"] = "application/json"
    rq = urllib.request.Request(s.url + rel, headers=h, data=data, method="POST" if data is not None else "GET")
    try:
        with ENV["opener"].open(rq, timeout=120) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


class Holding:
    """The Nov 2 session at 8x as a live source; holds on its last sample (stays running)."""

    def __init__(self, path):
        self.src = lc.IbtPlaybackSource(path, speed=8.0)
        self.interval = self.src.interval
        self.realtime, self.done, self.state, self.last = True, False, "connected", None

    def info(self):
        return {"car": {"name": "McLaren 570S GT4", "path": "mclaren570sgt4"},
                "track": {"name": "Circuit Zolder", "code": "zolder gp"}, "length_m": 3980.0}

    def read(self):
        smp = self.src.read()
        self.last = smp or self.last
        return smp or self.last

    def close(self):
        pass


s = srv()
s.coach_source_factory = lambda: Holding(NOV2)
code, rs = call("api/coach/mute-set", {"muted": True})
check(code == 400 and "Start the coach" in rs["error"], "mute-set needs the coach on")
check(call("api/coach/start", {})[1]["ok"], "the coach starts")
st = wait_for(lambda: (lambda x: x if x.get("state") == "coaching" else None)(call("api/coach/status")[1]), 20)
check(st and st["show"] == "coaching" and not st["muted"], f"it is coaching, not muted ({st and st.get('show')})")
code, rs = call("api/coach/mute-set", {"muted": True})
check(code == 200 and rs["muted"] and rs["mute"]["by"] == "the Mute button", "the first Mute press mutes")
code, rs = call("api/coach/mute-set", {"muted": True})
check(rs["muted"], "a second Mute press (a stale page) keeps it muted")
st = call("api/coach/status")[1]
check(st["show"] == "muted" and st["mute"]["on"] and st["mute"]["since"], "reopening the page: the mute is plain to see")
code, rs = call("api/coach/repeat", {})
check(code == 200 and not rs["said"] and rs["muted"] and "muted" in rs["note"], f"Repeat that while muted: {rs['note']}")
check(call("api/coach/status")[1]["repeat_note"], "the status says so too")
code, rs = call("api/coach/mute-set", {"muted": "yes"})
check(code == 400, "mute-set takes true or false only")
code, rs = call("api/coach/rehearse-skip", {"what": "lap"})
check(code == 400 and "rehearsal" in rs["error"], "no skipping in live coaching")
code, rs = call("api/coach/rehearse-speed", {"speed": 3})
check(code == 400, "speed: 1x, 2x or 4x only")
code, rs = call("api/coach/rehearse-speed", {"speed": 4})
check(code == 200 and call("api/coach/status")[1]["rehearse_speed"] == 4.0 and ENV["srv"].coach.speed == 1.0,
      "the speed is kept for the next rehearsal; live coaching stays at real speed")
call("api/coach/rehearse-speed", {"speed": 1})
call("api/coach/stop", {})
check(call("api/coach/start", {})[1]["ok"], "start again")
st = wait_for(lambda: (lambda x: x if x.get("running") else None)(call("api/coach/status")[1]), 10)
check(st and not st["muted"] and st["show"] != "muted", "stopping the coach clears the mute: every start is unmuted")
call("api/coach/stop", {})
code, vj = call("api/coach/voices")
check(code == 200 and vj["chosen_note"]["ok"], "the default voice is installed: no note")
st = call("api/coach/status")[1]
check(st.get("voice_note", {}).get("ok", False), "coach off, voice fine: no note")

# Practice: the coach's reference and its count
code, r = call("api/setuplab/read", {})
end = time.time() + 180
while time.time() < end and call("api/status")[1].get("state") == "running":
    time.sleep(0.2)
code, js = call("api/sessions-ibt")
lst = js.get("sessions") or []
check(code == 200 and len(lst) == 2, f"Practice lists both sessions ({len(lst)})")
cuelog.save_focus_result(coach2.logs_dir(), "plan-x", {"hits": 7, "of": 10, "car": "mclaren570sgt4",
                                                       "track": "zolder gp", "when": "2026-11-03 18:30"})
code, j = call("api/coach/result?" + urllib.parse.urlencode({"car": "mclaren570sgt4", "track": "zolder gp",
                                                             "plan": "plan-x"}))
ref = (j or {}).get("reference") or {}
nov3 = next((x for x in lst if x["date"].startswith("2026-11-03")), {})
check(code == 200 and ref.get("text") == r3.describe() and ref.get("when") == nov3.get("date"),
      f"the session page names the coach's reference with the Practice list's date ({ref.get('text')}, {ref.get('when')})")
check(j["result"] and j["result"]["text"] == "While coaching, you hit it on 7 of 10 laps."
      and j["done_text"] == "Done when you hit it on 7 of 10 laps", f"and the coach's count with what counts as done ({j.get('result')})")
code, j = call("api/coach/result?" + urllib.parse.urlencode({"car": "othercar", "track": "zolder gp", "plan": "plan-x"}))
check(code == 200 and j["result"] is None and j["reference"] is None, "another car: neither")
code, j = call("api/coach/result?car=")
check(code == 400, "car and track are needed")

# the page has the driving view, its toggle and the Practice hook
html = (ROOT / "debrief" / "desktop" / "ui" / "index.html").read_text(encoding="utf-8")
js_ = (ROOT / "debrief" / "desktop" / "ui" / "app.js").read_text(encoding="utf-8")
css = (ROOT / "debrief" / "desktop" / "ui" / "app.css").read_text(encoding="utf-8")
check(all(x in html for x in ('id="coDrive"', 'id="coDriveBtn"', 'id="cdWord"', 'id="cdCues"', 'id="cdDelta"',
                                'id="cdRef"', 'id="coRhSpeed"', 'id="coRhSkipLap"', 'id="coRhSkipCue"', 'id="coMuted"')),
      "the driving view, its toggle, rehearsal speed and skip, the mute banner")
check("debrief-coach-drive" in js_ and "body.codriving .side" in css and "coach/mute-set" in js_ and
      "coach/result?" in js_, "remembered, folds the sidebar, mute said outright, Practice asks for the coach's count")

s.shutdown()
print(f"\n{'FAILED ' + str(fails) if fails else 'ALL OK'}")
sys.exit(1 if fails else 0)
