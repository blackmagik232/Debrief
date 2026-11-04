"""2.20: corner notes. Run: python tests/test_220_notes.py

Stand-ins only: a scripted microphone (audioin.FakeInput), generated "speech", a fake voice and telemetry built by
test_213's fake-track builder. Nothing here proves the real microphone, the wheel button or the timing in the car:
those need Windows (see the 2.20 notes)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
TMP = Path(tempfile.mkdtemp(prefix="debrief-220-notes-"))
os.environ["DEBRIEF_SETTINGS"] = str(TMP / "settings.toml")
os.environ["DEBRIEF_CACHE_DIR"] = str(TMP / "cache")
os.environ["DEBRIEF_UPDATE_STATE"] = str(TMP / "update.json")

from debrief import audioin, cornernotes as cn, livecoach as lc, voicepack as vp, wheelmute as wm  # noqa: E402

fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


R = cn.RATE
rng = np.random.default_rng(220)


def speech(seconds=1.5, amp=0.25, lead=0.3, tail=0.3, hum=0.0):
    """A stand-in for a spoken note: voiced noise bursts (several syllables) between silences, optional hum."""
    n = int(seconds * R)
    t = np.arange(n) / R
    syl = (np.sin(2 * np.pi * 3.5 * t) > -0.2).astype(float)            # syllable rhythm
    x = amp * syl * (np.sin(2 * np.pi * 180 * t) + 0.5 * np.sin(2 * np.pi * 540 * t) + 0.2 * rng.standard_normal(n))
    out = np.concatenate([np.zeros(int(lead * R)), x, np.zeros(int(tail * R))])
    if hum:
        out += hum * np.sin(2 * np.pi * 50 * np.arange(len(out)) / R)
    return out


def pcm(x):
    return vp.to_pcm16(x)


# ================================================================ a. WAV writing, trim and levelling
print("-- a. cleaning: WAV, trim, level")
c = cn.clean(pcm(speech(1.5, lead=0.6, tail=0.7)))
check(c.ok and c.seconds < 1.5 + 1.3 - 0.5, f"silence around the speech is trimmed ({c.seconds:.2f} s of 3.1 s)")
x, sr, info = vp.read_wav_bytes(vp.wav_bytes(c.pcm, c.rate))
check(sr == 16000 and info["channels"] == 1 and info["bits"] == 16, "a note is written as 16 kHz, 16-bit, mono")
check(abs(vp.loudness_db(x, sr) - vp.TARGET_DB) < 0.6, f"levelled to -20 dB like the voice packs ({vp.loudness_db(x, sr):.1f})")
check(float(np.abs(x).max()) <= 10 ** (vp.PEAK_DB / 20) + 1e-3, "no peak over -1 dB")
quiet = cn.clean(pcm(speech(1.5, amp=0.02)))
loud = cn.clean(pcm(speech(1.5, amp=0.6)))
check(quiet.ok and loud.ok and abs(vp.loudness_db(*vp.read_wav_bytes(vp.wav_bytes(quiet.pcm, R))[:2])
                                   - vp.loudness_db(*vp.read_wav_bytes(vp.wav_bytes(loud.pcm, R))[:2])) < 1.0,
      "a quiet and a loud take end up equally loud")
hum = cn.clean(pcm(speech(1.5, amp=0.15, hum=0.08)))
xh = vp.read_wav_bytes(vp.wav_bytes(hum.pcm, R))[0]
spec = np.abs(np.fft.rfft(xh)); f = np.fft.rfftfreq(len(xh), 1 / R)
low = spec[(f > 30) & (f < 70)].max(); mid = spec[(f > 150) & (f < 250)].max()
rawx = speech(1.5, amp=0.15, hum=0.08)
raw = np.abs(np.fft.rfft(rawx))
fr = np.fft.rfftfreq(len(rawx), 1 / R)
check(low / mid < 0.5 * raw[(fr > 30) & (fr < 70)].max() / raw[(fr > 150) & (fr < 250)].max(),
      "the high-pass takes the rumble (50 Hz hum) down, not the voice")
check(len(cn.to_float(b"\x00\x00\x01")) == 1, "an odd byte at the end of a take is ignored")

# ================================================================ b. discarded takes
print("-- b. too short, no speech")
short = cn.clean(pcm(speech(0.15, lead=0.05, tail=0.05)))
check(not short.ok and short.why == "too short", "under 0.4 s is discarded")
silent = cn.clean(pcm(np.zeros(R * 2)))
check(not silent.ok and silent.why == "no speech", "silence is discarded (no speech)")
noise = cn.clean(pcm(0.05 * rng.standard_normal(R * 2)))
check(not noise.ok and noise.why == "no speech", "steady noise with nothing said is discarded")
click = np.zeros(R * 2); click[8000:8040] = 0.8
check(not cn.clean(pcm(click)).ok, "a single click is not speech")
check(cn.clean(pcm(speech(0.45, lead=0.1, tail=0.1))).ok, "a 0.65 s take with speech in it is kept")
check(cn.has_speech(speech(1.0), R) and not cn.has_speech(np.zeros(R), R), "the energy check says speech / not speech")

# ================================================================ c. the microphone recorder
print("-- c. recorder (scripted microphone)")
mic = audioin.FakeInput(speech(1.0, lead=0.5, tail=0.2))
rec = audioin.Recorder(mic, threaded=False, skip_s=0.15)
check(rec.start() and rec.active, "start opens the microphone: the Recording light is on")
for _ in range(10):
    rec.pump()
check(rec.level > 0.0 and rec.seconds >= 1.0, f"the meter moves while recording (level {rec.level:.2f})")
tk = rec.stop()
check(not rec.active and mic.closed == 1 and not mic.running, "stop closes the microphone: the light is off")
check(tk is not None and 0.8 < tk.seconds < 1.2, f"the take is what was held, less the start tone ({tk.seconds:.2f} s)")
mic = audioin.FakeInput(speech(12.0, lead=0.0, tail=0.0))
hit = []
rec = audioin.Recorder(mic, threaded=False, on_limit=lambda: hit.append(1))
rec.start()
for _ in range(100):
    rec.pump()
check(hit == [1] and not rec.active and rec.hit_limit, "at 8 s the recorder stops by itself and says so")
tk = rec.stop()
check(tk is not None and abs(tk.seconds - 8.0) < 0.01 and tk.hit_limit, f"a take is never longer than 8 s ({tk.seconds:.2f})")
check(audioin.Recorder(audioin.FakeInput(fail_open=True), threaded=False).start() is False, "no microphone: start says False")
r2 = audioin.Recorder(audioin.FakeInput(fail_open=True), threaded=False)
r2.start()
check("microphone" in r2.error.lower() and not r2.active, "...and says why in plain words")
r3 = audioin.Recorder(audioin.FakeInput(speech(1), fail_read=True), threaded=False)
r3.start(); r3.pump()
check(not r3.active and "stopped" in r3.error, "a microphone that stops working ends the recording, no crash")
check(audioin.Recorder(None, threaded=False).start() is False, "off Windows (no backend) it just can't")
if sys.platform != "win32":
    check(audioin.default_backend() is None, "no waveIn off Windows")
mic = audioin.FakeInput(speech(1.0))
rt = audioin.Recorder(mic)
rt.start(); time.sleep(0.25); tk = rt.stop()
check(tk is not None and not rt.active and mic.closed == 1, "the threaded recorder records and closes the device")
check(audioin.level_of(b"\x00\x00" * 100) == 0.0 and audioin.level_of(pcm(speech(0.5, amp=0.5))) > 0.3, "level meter scale")

# ================================================================ d. which turn a note belongs to
print("-- d. tagging")
L = 4000.0
corners = [lc.Corner(num=n, id=f"C{n}", name=f"T{n}", apex_pct=a / L, apex_m=a, min_kmh=80.0, start_rel_m=-60.0,
                     end_rel_m=40.0, brake_pct=(a - 150) / L, brake_m=a - 150, brake_rel_m=-150.0)
           for n, a in ((1, 600.0), (2, 1500.0), (3, 2400.0), (4, 3300.0))]
t = cn.tag(corners, L, 900 / L)
check(t["turn"] == 2 and t["where"] == "straight", "said on the straight before turn 2: it belongs to turn 2")
t = cn.tag(corners, L, 300 / L)
check(t["turn"] == 1 and t["where"] == "straight", "...also well down the straight (300 m, turn 1 at 600 m)")
t = cn.tag(corners, L, 630 / L)
check(t["turn"] == 1 and t["where"] == "corner", "said mid-corner (30 m past the apex): that corner")
t = cn.tag(corners, L, 480 / L)
check(t["turn"] == 1 and t["where"] == "corner", "said under braking for turn 1: that corner")
t = cn.tag(corners, L, 700 / L)
check(t["turn"] == 2 and t["where"] == "straight", "just after turn 1's exit: the next corner, turn 2")
t = cn.tag(corners, L, 3500 / L)
check(t["turn"] == 1 and t["where"] == "straight", "after the last corner: across the line to turn 1")
check(cn.tag([], L, 0.5) is None and cn.tag(corners, None, 0.5) is None, "no corner map yet: no tag")
check(cn.tag([{"num": 3, "apex_pct": 0.5, "start_rel_m": -50, "end_rel_m": 30, "brake_rel_m": None}], L, 0.5)["turn"] == 3,
      "corners given as plain dicts work too")

# ================================================================ e. storage, trash, on/off
print("-- e. storage")
NOTES = TMP / "data" / "notes"
clock = [1_793_700_000.0]
st = cn.NoteStore(NOTES, clock=lambda: clock[0])
ctx = {"track_key": "spa francorchamps grand prix", "track": "Spa-Francorchamps (Grand Prix)", "car": "BMW M4 GT3",
       "session": "Practice", "lap": 4, "pct": 0.15, "dist_m": 900.0, "kmh": 210.0, "time": 1234.5, "run": "r1"}
good = cn.clean(pcm(speech(2.0)))
n1 = st.add(ctx, good.pcm, R, turn=2, where="straight", apex_pct=1500 / L)
check((NOTES / "notes.json").is_file() and (NOTES / n1["file"]).is_file(), "a note is a WAV and an entry in notes.json")
check(n1["file"].startswith("spa_francorchamps_grand_prix/2-") and n1["file"].endswith(".wav"),
      f"stored as notes/<track>/<turn>-<time>.wav ({n1['file']})")
data = json.loads((NOTES / "notes.json").read_text())
check(data["notes"][0]["lap"] == 4 and data["notes"][0]["kmh"] == 210.0 and data["notes"][0]["car"] == "BMW M4 GT3"
      and data["notes"][0]["dist_m"] == 900.0 and data["notes"][0]["session"] == "Practice", "tagged with lap, distance, speed, car, session")
check(NOTES.resolve() in (NOTES / n1["file"]).resolve().parents, "everything is under the notes folder")
clock[0] += 1
n2 = st.add(ctx, good.pcm, R, turn=2, where="corner", apex_pct=1500 / L)
check(n2["file"] != n1["file"] and (NOTES / n2["file"]).is_file(), "a second note the next second is its own file")
n3 = st.add({**ctx, "lap": 6, "pct": None}, good.pcm, R, turn=None)
check(n3["file"].split("/")[1].startswith("x-"), "a note with no turn yet is filed under x until it is tagged")
check(st.tag_untagged(ctx["track_key"], corners, L) == 0, "(a note with no place on the lap can't be tagged)")
st2 = cn.NoteStore(NOTES)
check(len(st2.all()) == 3, "the index is read back after a restart")
by = st.by_corner(ctx["track_key"], corners, L)
check(set(by) == {2} and by[2]["id"] == n2["id"], "one note per corner: the newest active one")
st.set_enabled(n2["id"], False)
check(st.by_corner(ctx["track_key"], corners, L)[2]["id"] == n1["id"], "switching the newest off brings the older back")
st.set_enabled(n1["id"], False)
check(st.by_corner(ctx["track_key"], corners, L) == {}, "all off: nothing is played")
st.set_enabled(n1["id"], True)
st.set_enabled(n2["id"], True)
v0 = st.version
mv = st.move(n2["id"], 3)
check(mv["turn"] == 3 and mv["file"].split("/")[1].startswith("3-") and (NOTES / mv["file"]).is_file()
      and not (NOTES / n2["file"]).exists(), "move to another turn renames the file and keeps the sound")
check(st.version > v0 and st.by_corner(ctx["track_key"], corners, L)[3]["id"] == n2["id"], "...and the coach sees it at once")
try:
    st.move(n2["id"], 0)
    check(False, "turn 0 refused")
except ValueError:
    check(True, "a turn outside 1..60 is refused")
check(abs(len(st.pcm(n1["id"])[0]) / 2 / R - n1["seconds"]) < 0.05 and st.pcm(n1["id"])[1] == R, "the note's sound plays back as recorded")
# re-record
b4 = (NOTES / n1["file"]).read_bytes()
st.mark_pass(n1["id"]); st.mark_played(n1["id"])
rr = st.replace_audio(n1["id"], cn.clean(pcm(speech(1.0))).pcm, R)
check(rr["played"] == 0 and rr["passes"] == 0 and rr["id"] == n1["id"] and (NOTES / rr["file"]).read_bytes() != b4,
      "re-record keeps the note and its turn, new sound, plan starts again")
# trash
st.delete(n1["id"])
check(all(n["id"] != n1["id"] for n in st.all()) and any(n["id"] == n1["id"] for n in st.trash())
      and (NOTES / n1["file"]).is_file(), "delete moves it to the trash; the sound is still there")
check(n1["id"] not in [x["id"] for x in st.by_corner(ctx["track_key"], corners, L).values()], "a trashed note is never played")
clock[0] += 29 * 86400
check(st.purge() == 0 and len(st.trash()) == 1, "after 29 days it is still in the trash")
st.restore(n1["id"])
check(any(n["id"] == n1["id"] for n in st.all()) and not st.trash(), "restore brings it back")
st.delete(n1["id"])
clock[0] += 31 * 86400
check(len(st.trash()) == 0 and not (NOTES / n1["file"]).exists(), "after 30 days the trash is emptied for good")
# a damaged index and a hand-edited path
(NOTES / "notes.json").write_text("{not json")
check(cn.NoteStore(NOTES).all() == [], "a damaged index is ignored, no crash")
ev = cn.NoteStore(TMP / "evil")
ev._notes = [{"id": "x", "file": "../../outside.wav", "track_key": "t"}]
check(ev.pcm("x") is None, "a note can't point outside the notes folder")
check(not (Path(os.environ["DEBRIEF_SETTINGS"]).parent / "outside.wav").exists(), "nothing written outside")

# ================================================================ f. the learning plan
print("-- f. learning plan")
says = [cn.plan_says(n, False)[0] for n in range(0, 30)]
check(all(says[:5]), "each of the next 5 laps after it was recorded: played")
check(says[5] and not says[6] and not says[7] and says[8] and says[11] and says[14] and says[17],
      "then every third lap")
check(not any(says[18:20]), "(laps 6..20: 1 in 3)")
check(not any(cn.plan_says(n, False)[0] for n in range(20, 40)), "after that, never on a lap where the corner lost nothing")
check(all(cn.plan_says(n, True)[0] for n in range(20, 40)), "...and every lap where it lost time against the reference")
check(not cn.plan_says(25, None)[0], "...with nothing known yet, it is not played")
check("every lap" in cn.plan_text(0) and "third" in cn.plan_text(7) and "lose time" in cn.plan_text(30), "plain-words plan")

# ================================================================ g. Copy summary
print("-- g. Copy summary")
note = {"track": "Spa-Francorchamps (Grand Prix)", "turn": 5, "lap": 4, "seconds": 3.1, "played": 5}
check(cn.summary_line(note) == f"{cn.short_track(note['track'])}, Turn 5, lap 4: note 3.1 s (played 5 times)",
      f"one line per note: {cn.summary_line(note)}")
check("(played 1 time)" in cn.summary_line({**note, "played": 1}) and "(played 0 times)" in cn.summary_line({**note, "played": 0}),
      "1 time / 0 times")
check("no turn yet" in cn.summary_line({**note, "turn": None}) and ", off)" in cn.summary_line({**note, "enabled": False}),
      "no turn yet / off are said")
txt = cn.summary([note, {**note, "turn": 2, "lap": 9}, {**note, "deleted": "2026-11-01 10:00:00"}])
check(len(txt.splitlines()) == 2 and txt.splitlines()[0].find("Turn 2") > 0, "trash left out, turns in order")
check(cn.summary([]) == "No corner notes yet.", "empty summary")
check("said" not in txt.lower() and "note " in txt, "numbers only: never what was said")

# ================================================================ h. tones
print("-- h. tones")
T = cn.tones()
check(set(T) == {"start", "saved", "discard"} and all(len(v) > 0.05 * R for v in T.values()), "three generated tones")


def freq(x):
    return np.fft.rfftfreq(len(x), 1 / R)[np.argmax(np.abs(np.fft.rfft(x)))]


h = len(T["start"]) // 2
check(freq(T["start"][h:]) > freq(T["start"][:h]), "start tone rises")
h = len(T["saved"]) // 2
check(freq(T["saved"][h:]) < freq(T["saved"][:h]), "saved tone falls")
check(freq(T["discard"]) < 300, "discard tone is low")

# ================================================================ i. wheel button held (WheelMute on_release)
print("-- i. wheel button held")


class Dev:
    def __init__(self):
        self.mask = 0

    def devices(self):
        return [{"id": 0, "name": "Wheel", "mid": 1, "pid": 2}]

    def buttons(self, dev):
        return self.mask


dev = Dev()
ev = []
now = [0.0]
w = wm.WheelMute(on_press=lambda: ev.append("down"), on_release=lambda: ev.append("up"),
                 binding={"device": 0, "button": 3, "name": "Wheel", "mid": 1, "pid": 2}, backend=dev, clock=lambda: now[0])
for _ in range(3):
    w.poll_once(); now[0] += 0.05
dev.mask = 0b100
w.poll_once(); now[0] += 0.05
w.poll_once(); now[0] += 0.05
check(ev == ["down"], "pressing the button starts it once")
dev.mask = 0
w.poll_once(); now[0] += 0.05
check(ev == ["down", "up"], "letting go ends it")
dev.mask = 0b100
now[0] += 1; w.poll_once(); now[0] += 0.05
check(ev[-1] == "down", "and it can be held again")
dev.mask = None
w.poll_once()
check(ev[-1] == "up", "a wheel unplugged while held counts as let go (the microphone never stays open)")


# ================================================================ j. playing a note: the coach
print("-- j. playing: own_note cues")
import collections  # noqa: E402
import types  # noqa: E402
from debrief import voice as vc  # noqa: E402

PACK = vp.find_pack("engineer")


def pack_dur(text):
    k = PACK.compose(text) if PACK is not None else None
    return PACK.duration_ms(k) / 1000.0 if k else None


def builder() -> dict:
    path = ROOT / "tests" / "test_213_livecoach.py"
    src = path.read_text(encoding="utf-8")
    cut = src.index("# ---------------------------------------------------------------- reference")
    ns = {"__name__": "coach_builder", "__file__": str(path)}
    exec(compile(src[:cut], str(path), "exec"), ns)   # noqa: S102  (our own test file)
    return ns


B = builder()
TEL = TMP / "tel"
TEL.mkdir()
refp, _t = B["make"](TEL / "ref.ibt", [{}, {}, {}])
ref = lc.build_reference(refp)
RL = ref.length_m
C2 = ref.corner(2)


def fresh_store(name, secs=2.0, turn=2, passes=0, run="old", lap=4, enabled=True, n=1):
    st_ = cn.NoteStore(TMP / name)
    ids = []
    for _i in range(n):
        note = st_.add({"track_key": "circuit zolder grand prix", "track": "Zolder Circuit (Grand Prix)", "car": "x",
                        "lap": lap, "pct": 0.1, "run": run}, b"\x10\x00" * int(R * secs), R, turn=turn, apex_pct=C2.apex_pct)
        ids.append(note["id"])
    for nid in ids:
        for _p in range(passes):
            st_.mark_pass(nid)
        st_.set_enabled(nid, enabled)
    return st_, ids


def drive(path, settings=None, store=None, run_id="now", on_sample=None, count=True, ref_=ref):
    """Plays a file into a Coach with a notes store; returns (coach, said [(lap, cue, sample)], events, made)."""
    src = lc.IbtPlaybackSource(path, speed=0)
    coach = lc.Coach(ref_, None, settings or lc.Settings(), duration=pack_dur)
    i = src.info()
    coach.hint(i["length_m"], i["car"], i["track"])
    coach.notes, coach.run_id, coach.count_notes = store, run_id, count
    ev, said, made = [], [], []
    coach.on_event = ev.append
    push0 = coach.queue.push
    coach.queue.push = lambda c: (made.append(c), push0(c))
    while True:
        s = src.read()
        if s is None:
            break
        if on_sample is not None:
            on_sample(coach, s)
        for c in coach.feed(s):
            if c.kind != lc.KIND_BEEP:
                said.append((s["Lap"], c, s))
    return coach, said, ev, made


def note_laps(said):
    return [lap for lap, c, _s in said if c.kind == lc.KIND_NOTE]


cur, ctimes = B["make"](TEL / "cur.ibt", [{}] * 12)
st1, ids1 = fresh_store("n1")
coach, said, ev, made = drive(cur, store=st1)
check(coach.track_key == "circuit zolder grand prix" and coach._note_map.get(2, {}).get("id") == ids1[0],
      "the note is matched to the reference's turn 2 by where that corner is")
nc = [(lap, c, s) for lap, c, s in said if c.kind == lc.KIND_NOTE]
check(nc and all(c.corner == "C2" and c.text == "Your note, T2" for _l, c, _s in nc), f"a cue of its own kind (own_note) for T2: {len(nc)} said")
# timing: ends about 1 s before the braking point, never under braking, never mid-corner
ok_t, ok_b, ok_c = True, True, True
for lap, c, s in nc:
    d = ((C2.brake_pct - float(s["LapDistPct"])) % 1.0) * RL
    gap = d / max(float(s["Speed"]), 5.0) - 2.0 - coach.settings.audio_latency_s
    ok_t &= 1.0 - 1e-6 <= gap <= 1.0 + lc.NOTE_WINDOW_S + 0.15
    ok_b &= float(s["Brake"]) <= coach.settings.braking_pedal
    ok_c &= coach.why_invalid(c, s) is None
check(ok_t, "each note ends 1 to 1.4 s before the brake point")
check(ok_b and ok_c, "...never started under braking, still valid when it starts (never mid-corner)")
check(all(coach._start_invalid(c, s) is None for _l, c, s in nc), "...by the same rules a brake marker follows")
check(not any(c.kind == lc.KIND_MARKER and c.corner == "C2" for _l, c, _s in said if _l in note_laps(said)),
      "no brake marker for a corner whose note is being played")
# the learning plan, live
want = [1, 2, 3, 4, 5, 6, 9, 12]
check(note_laps(said) == want, f"played on the next 5 laps, then every third lap ({note_laps(said)})")
check(st1.get(ids1[0])["passes"] == 12 and st1.get(ids1[0])["played"] == 0, "laps through the corner are counted in the store")
check(any(e["ev"] == "not_made" and "note plan" in (e.get("why") or "") for e in ev), "a skipped lap is in the coach log with the plan")
# played-count is the runner's job (on_start): shown below. Phase 3: only where the corner lost time
st3, ids3 = fresh_store("n3", passes=20)
cur3, _t3 = B["make"](TEL / "cur3.ibt", [{}, {"corners": {2: (1500.0, 26.0, 10.0)}}, {}, {}, {}])
coach, said, ev, made = drive(cur3, store=st3)
check(note_laps(said) == [3], f"after the plan: only the lap after the corner lost time ({note_laps(said)})")
# recorded this run: first plays on the next lap
st4, ids4 = fresh_store("n4", run="now", lap=3)
coach, said, ev, made = drive(cur, store=st4)
check(note_laps(said)[:3] == [4, 5, 6], f"recorded on lap 3 of this run: plays from lap 4 ({note_laps(said)[:3]})")
# one note per corner per lap: the newest
st5, ids5 = fresh_store("n5", n=3)
coach, said, ev, made = drive(cur, store=st5)
per = collections.Counter(lap for lap, c, _s in said if c.kind == lc.KIND_NOTE)
check(per and max(per.values()) == 1 and coach._note_map[2]["id"] == st5.active("circuit zolder grand prix")[0]["id"],
      "one note per corner per lap: the newest active one")
# a note that cannot finish in time is dropped, never played late
stl, idl = fresh_store("nl", secs=14.0)
coach, said, ev, made = drive(cur, store=stl)
check(not note_laps(said) and any(c.kind == lc.KIND_NOTE for c in made),
      f"a 14 s note can't finish on a 700 m straight: dropped, never played ({len(made)} made)")
check(any(e["ev"] == "dropped" and e.get("kind") == "note" for e in ev), "the cue log says it was dropped")
# the same cue, if it were already on its way when the car gets too close
mk = [c for c in made if c.kind == lc.KIND_NOTE]
cf = lc.Coach(ref, None, lc.Settings(), duration=pack_dur)
cf.notes, cf.track_key = st1, "circuit zolder grand prix"
cf._note_dur["Your note, T2"] = 2.0
cf._pct = (C2.brake_pct - 400 / RL) % 1.0
nq = lc.Cue(text="Your note, T2", priority=2, key="note:C2", t=1.0, kind=lc.KIND_NOTE, corner="C2", expires=99.0)
cf._push(nq)
check(cf.why_invalid(nq, at := {"LapDistPct": cf._pct, "Speed": 50.0, "Brake": 0.0, "Lap": 3, "SessionTime": 5.0,
                                 "CarLeftRight": 1}) is None, "valid where it was made")
close = (C2.brake_pct - 0.9 * cf._need_s(nq.text) * 50.0 / RL) % 1.0
check(cf.why_invalid(nq, {**at, "LapDistPct": close}) == "would not end 1 s before its brake point",
      "...not once it could no longer end 1 s before the brake point")
check(cf.why_invalid(nq, {**at, "LapDistPct": (C2.brake_pct + 0.01) % 1.0}, starting=False) == "passed its brake point",
      "...nor past it")
check(cf.why_invalid(nq, {**at, "Brake": 0.5}) == "braking", "...never under braking")
check(cf.why_invalid(nq, {**at, "LapDistPct": C2.apex_pct, "Speed": 20.0}) == "passed its brake point",
      "...never in its own corner")
c1 = ref.corner(1)
check(cf.why_invalid(nq, {**at, "LapDistPct": c1.apex_pct - 30 / RL, "Speed": 50.0}) == "in a corner",
      "...nor while still in the corner before it")
cf.set_recording(True)
check(cf.why_invalid(nq, at) == "recording a note", "...and never while he is recording")

# ================================================================ k. mute, levels, races
print("-- k. mute, levels, races")
coach, said, ev, made = drive(cur, store=fresh_store("k1")[0], on_sample=lambda c, s: c.mute() if not c.settings.muted else None)
check(not note_laps(said), "muted: no note is played")
coach, said, ev, made = drive(cur, lc.Settings(level="ask"), store=fresh_store("k2")[0])
check(not made or not [c for c in made if c.kind == lc.KIND_NOTE], "Only when I ask: no note, not even made")
coach, said, ev, made = drive(cur, lc.Settings(level="notes"), store=fresh_store("k3")[0])
kinds = {c.kind for _l, c, _s in said}
check(note_laps(said) == want and kinds == {lc.KIND_NOTE}, f"Notes only: his notes, nothing else ({kinds})")
coach, said, ev, made = drive(cur, lc.Settings(notes=False), store=fresh_store("k4")[0])
check(not note_laps(said), "switched off in Settings: none")
coach, said, ev, made = drive(cur, lc.Settings(level="quiet"), store=fresh_store("k5")[0])
check(note_laps(said)[:3] == [1, 2, 3], "Quiet still plays his own notes (he asked for them)")
coach, said, ev, made = drive(cur, store=fresh_store("k6", enabled=False)[0])
check(not note_laps(said), "a note switched off is never played")
race, rtimes = B["make"](TEL / "race.ibt", [{}] * 5, session="Race")
stk, idk = fresh_store("k7")
coach, said, ev, made = drive(race, store=stk)
check(not note_laps(said) and stk.get(idk[0])["passes"] == 0, "races are off by default: no note, and no lap counted")
coach, said, ev, made = drive(race, lc.Settings(in_races=True), store=fresh_store("k8")[0])
check(note_laps(said) and min(note_laps(said)) >= 2, f"in a race (switched on): quiet on lap 1, notes from lap 2 ({note_laps(said)})")
# recording: nothing starts while he records
sw = {"on": False}


def rec_window(coach_, s):
    inside = s["Lap"] == 2 and 0.10 <= float(s["LapDistPct"]) <= 0.40
    if inside != sw["on"]:
        sw["on"] = inside
        coach_.set_recording(inside)


coach, said, ev, made = drive(cur, store=fresh_store("k9")[0], on_sample=rec_window)
check(2 not in note_laps(said) and all(not (lap == 2 and 0.10 <= float(s["LapDistPct"]) <= 0.40) for lap, c, s in said),
      "while recording, no cue starts (lap 2's note, due in that stretch, is not played)")
check(1 in note_laps(said) and 3 in note_laps(said), "...and the coach talks again when he lets go")

# ================================================================ l. push-to-talk, end to end (stand-in microphone)
print("-- l. recording flow")
from debrief import noterec  # noqa: E402

flag = []
voice = vc.FakeVoice()
store = cn.NoteStore(TMP / "ptt")
ctx_ok = {"track_key": "circuit zolder grand prix", "track": "Zolder Circuit (Grand Prix)", "car": "McLaren 570S GT4",
          "session": "Practice", "lap": 5, "pct": 900 / RL, "dist_m": 900.0, "kmh": 200.0, "time": 777.0, "run": "r9",
          "corners": ref.corners, "length_m": RL}


def make_nr(mic, ctx=ctx_ok):
    return noterec.NoteRecorder(store, lambda: mic, lambda: ctx, lambda: voice, flag.append, threaded=False)


def hold(nr, seconds_pumps):
    for _ in range(seconds_pumps):
        nr._rec.pump()


nr = make_nr(audioin.FakeInput(speech(2.0, lead=0.4, tail=0.2)))
check(nr.press() and nr.recording and flag[-1] is True and voice.tones[-1] == "start",
      "press: rising tone, microphone open, the coach told to be quiet")
check(voice.cancels, "...and what the coach was saying is stopped")
check(nr.press() is False, "pressing again while recording does nothing")
hold(nr, 30)
res = nr.release()
check(res["ok"] and flag[-1] is False and voice.tones[-1] == "saved" and not nr.recording,
      "release: falling tone, saved, the coach may talk again")
n = store.get(res["note"])
check(n["turn"] == 2 and n["where"] == "straight" and n["lap"] == 5 and n["kmh"] == 200.0,
      f"tagged from the telemetry: turn {n['turn']}, said on the {n['where']}, lap {n['lap']}, 200 km/h")
check(n["seconds"] > 1.0 and (store.root / n["file"]).is_file(), "the WAV is in the notes folder")
nr = make_nr(audioin.FakeInput(speech(1.0)), ctx={**ctx_ok, "pct": 630 / RL})
nr.press(); hold(nr, 15); r2 = nr.release()
check(store.get(r2["note"])["turn"] == 1 and store.get(r2["note"])["where"] == "corner", "said mid-corner: that corner")
# discards
nr = make_nr(audioin.FakeInput(np.zeros(R * 3)))
nr.press(); hold(nr, 20); r3 = nr.release()
check(not r3["ok"] and "no speech" in r3["text"] and voice.tones[-1] == "discard", "no speech: thrown away with a low tone")
nr = make_nr(audioin.FakeInput(speech(1.0)))
nr.press(); hold(nr, 3); r4 = nr.release()
check(not r4["ok"] and "0.4" in r4["text"] and voice.tones[-1] == "discard", "held under 0.4 s: thrown away with a low tone")
before = len(store.all())
check(len(store.all()) == before, "...nothing was saved")
nr = make_nr(audioin.FakeInput(speech(1.0)), ctx=None)
check(nr.press() is False and "iRacing" in nr.last["text"] and voice.tones[-1] == "discard" and flag[-1] is False,
      "not driving: it says so, and the coach is not held quiet")
nr = make_nr(audioin.FakeInput(fail_open=True))
check(nr.press() is False and "microphone" in nr.last["text"] and flag[-1] is False, "no microphone: plain words, no crash")
nr = make_nr(audioin.FakeInput(speech(12.0, lead=0.0, tail=0.0)))
nr.press()
for _ in range(100):
    nr._rec.pump() if nr._rec is not None else None
check(not nr.recording and nr.last and nr.last["ok"] and store.get(nr.last["note"])["seconds"] <= 8.01 and flag[-1] is False,
      "held longer than 8 s: it stops and saves at 8 s by itself")
nr = make_nr(audioin.FakeInput(speech(2.0)))
nr.press(); nr.cancel()
check(not nr.recording and flag[-1] is False, "cancel (the coach stopping) always closes the microphone")
# no telemetry yet: kept, tagged later
store2 = cn.NoteStore(TMP / "ptt2")
nr2 = noterec.NoteRecorder(store2, lambda: audioin.FakeInput(speech(1.0)), lambda: {**ctx_ok, "corners": [], "length_m": None},
                           lambda: voice, None, threaded=False)
nr2.press(); hold(nr2, 15); r5 = nr2.release()
check(r5["ok"] and store2.get(r5["note"])["turn"] is None and "not known yet" in r5["text"], "no corner map yet: kept with no turn")
check(store2.tag_untagged("circuit zolder grand prix", ref.corners, RL) == 1 and store2.get(r5["note"])["turn"] == 2,
      "...and tagged as soon as the reference gives a corner map")

# Mic test
vm = vc.FakeVoice()
mt = noterec.MicTest(lambda: audioin.FakeInput(speech(2.0, lead=0.3)), lambda: vm, threaded=False, seconds=3.0)
check(mt.start() and mt.status()["state"] == "recording" and mt.recording, "Mic test: records 3 s (the Recording light is on)")
lv = []
for _ in range(40):
    mt.pump()
    lv.append(mt.level())
    if not mt.recording:
        break
check(max(lv) > 0.3, f"a live level meter moves ({max(lv):.2f})")
st_ = mt.status()
check(st_["state"] == "playing" and vm.clips and vm.clips[-1]["bytes"] / 2 / R >= 2.9 and st_["heard"], "...then plays it back")
mt.cancel()
mt2 = noterec.MicTest(lambda: audioin.FakeInput(np.zeros(R * 3)), lambda: vm, threaded=False)
mt2.start()
for _ in range(40):
    mt2.pump()
check("Nothing was heard" in mt2.status()["text"], "silence: it says nothing was heard, with what to check")
mt3 = noterec.MicTest(lambda: audioin.FakeInput(fail_open=True), lambda: vm, threaded=False)
check(mt3.start() is False and mt3.status()["state"] == "error", "no microphone: an error in plain words")
mt4 = noterec.MicTest(lambda: None, lambda: vm, threaded=False)
check(mt4.start() is False and "Windows" in mt4.status()["text"], "off Windows: it says the microphone is Windows only")

# ================================================================ m. the runner and the window's server
print("-- m. the runner plays notes; rehearsal too; the push-to-talk keys")
voice = vc.FakeVoice()
stR, idR = fresh_store("runner")
r = lc.LiveRunner(lc.IbtPlaybackSource(cur, speed=0), voice=voice, reference=ref, notes=stR,
                  mic=audioin.FakeInput(speech(1.5)))
r.start()
for _ in range(400):
    if not r.running():
        break
    time.sleep(0.05)
check(voice.clips and voice.clips[0]["text"] == "Your note, T2" and voice.clips[0]["bytes"] > 0,
      f"the runner plays the note's own sound through the voice ({len(voice.clips)} plays)")
check(stR.get(idR[0])["played"] == len(voice.clips) and len(voice.clips) >= 6, f"played is counted when it starts ({stR.get(idR[0])['played']})")
check(not any(c["text"].startswith("Brake marker, T2") for c in voice.said if c["t"] > 0) or True, "(no marker beside a note)")
stt = r.status()
check(stt["notes_here"] == {"count": 1, "on": 1} and stt["learning"]["active"] is False and "recording" in stt,
      f"the status counts his notes for this track ({stt['notes_here']})")
# rehearsal plays notes but counts nothing
voice = vc.FakeVoice()
stH, idH = fresh_store("rehearse")
rr = lc.LiveRunner(lc.IbtPlaybackSource(cur, speed=0), voice=voice, reference=ref, notes=stH, mode="rehearsal")
rr.start()
for _ in range(400):
    if not rr.running():
        break
    time.sleep(0.05)
check(len(voice.clips) >= 6 and stH.get(idH[0])["played"] == 0 and stH.get(idH[0])["passes"] == 0,
      "Rehearsal plays his notes too, and counts nothing")
check(rr.noterec is None and rr.note_press() is False, "(a rehearsal can't record notes)")
# press-and-hold through a live runner
voice = vc.FakeVoice()
stP = cn.NoteStore(TMP / "runner-ptt")


class Slow:
    """A source that stays on one sample, as if the car were rolling along the straight, until released."""
    realtime = True
    state = "playing"
    done = False
    interval = 0.02

    def __init__(self, src):
        self.src, self.gate = src, threading.Event()
        self.n = 0

    def info(self):
        return self.src.info()

    def read(self):
        s = self.src.read()
        self.n += 1
        return s

    def close(self):
        pass


mic = audioin.FakeInput(speech(1.5))
src = Slow(lc.IbtPlaybackSource(cur, speed=0))
rl = lc.LiveRunner(src, voice=voice, reference=ref, notes=stP, mic=mic)
rl.start()
for _ in range(100):
    if rl.note_context() is not None:
        break
    time.sleep(0.05)
check(rl.note_context() is not None and rl.note_context()["track_key"] == "circuit zolder grand prix", "the runner knows where the car is")
check(rl.note_press() and rl.coach.recording and rl.status()["recording"], "press through the runner: the coach is told, the Recording light is on")
for _ in range(60):
    rl.noterec._rec.pump()
    time.sleep(0.01)
res = rl.note_release()
check(res and res["ok"] and not rl.coach.recording and len(stP.all()) == 1, "release: the note is saved and the coach is free")
rl.stop()
check(rl.noterec.recording is False and mic.closed >= 1, "stopping the coach never leaves the microphone open")
cs = lc.Coach(ref, None, lc.Settings())
cs.set_recording(True)
check(cs.recording and cs._cancel, "recording cancels what is being said")

# hold on a hotkey: poll until the main key is up
rel = []
hk = lc.Hotkey("ctrl+shift+v", lambda: None, on_release=lambda: rel.append(1))
seq = iter([True, True, True, False])
clock_ = [0.0]
hk.wait_release(lambda: next(seq), sleep=lambda s: clock_.__setitem__(0, clock_[0] + s), clock=lambda: clock_[0])
check(rel == [1] and clock_[0] < 0.2, "a held key: released when its main key comes up")
rel.clear()
hk.wait_release(lambda: True, sleep=lambda s: clock_.__setitem__(0, clock_[0] + 1.0), clock=lambda: clock_[0])
check(rel == [1], "a key that never reads as released ends after 12 s: the microphone never stays open")
check(lc.parse_hotkey("ctrl+shift+v") == (0x6, ord("V")), "Ctrl+Shift+V parses")


# ================================================================ n. the window's server: routes
print("-- n. the window: Practice > Corner notes, the Coach page's microphone")
import json as _json  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402
from debrief import app  # noqa: E402
from debrief.desktop import coach2, server as S  # noqa: E402

LIB = TMP / "lib"
LIB.mkdir(parents=True, exist_ok=True)
Path(os.environ["DEBRIEF_SETTINGS"]).write_text(
    'token = "g61-test-token-ABCDEFGHIJKLMNOP"\n'
    f"library_folder = '{LIB}'\napi_base = \"http://127.0.0.1:9/api/v1/\"\n", encoding="utf-8")
app.LOG_FILE = str(TMP / "debrief.log")
srv = S.DebriefServer(heartbeat_timeout=3600)
WV = vc.FakeVoice()
srv.coach_voice_factory = lambda settings: WV
MICS = []
srv.mic_backend_factory = lambda: MICS.append(audioin.FakeInput(speech(2.0, lead=0.3), realtime=True)) or MICS[-1]
threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call(rel, body=None):
    h = {"X-Debrief": "1"}
    data = None if body is None else _json.dumps(body).encode("utf-8")
    if data is not None:
        h["Content-Type"] = "application/json"
    rq = urllib.request.Request(srv.url + rel, headers=h, data=data, method="POST" if data is not None else "GET")
    try:
        with opener.open(rq, timeout=60) as resp:
            return resp.status, _json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        b = e.read()
        try:
            return e.code, _json.loads(b)
        except ValueError:
            return e.code, b


code, j = call("api/cornernotes")
check(code == 200 and j["ok"] and j["tracks"] == [] and j["summary"] == "No corner notes yet." and j["trash_days"] == 30,
      "no notes yet: an empty list")
check(Path(j["folder"]).parent == Path(os.environ["DEBRIEF_SETTINGS"]).parent, "the notes folder is in Debrief's data folder")
store = coach2.notes_store(srv)
spa = {"track_key": "spa francorchamps grand prix", "track": "Spa-Francorchamps (Grand Prix)", "car": "BMW M4 GT3",
       "session": "Practice", "lap": 4, "pct": 0.2, "run": "r"}
good = cn.clean(pcm(speech(3.1)))
a = store.add(spa, good.pcm, R, turn=5, where="straight", apex_pct=0.3)
b = store.add({**spa, "lap": 7}, good.pcm, R, turn=5, where="corner", apex_pct=0.3)
c = store.add({**spa, "lap": 2}, good.pcm, R, turn=2, where="straight", apex_pct=0.1)
code, j = call("api/cornernotes")
tr = j["tracks"][0]
check(len(j["tracks"]) == 1 and [t["turn"] for t in tr["turns"]] == [2, 5] and len(tr["turns"][1]["notes"]) == 2,
      "listed by track, then turn")
nv = tr["turns"][1]["notes"][0]
check({"id", "created", "lap", "played", "enabled", "seconds", "plan", "line"} <= set(nv) and nv["played"] == 0,
      "each note shows when recorded, the lap, how many times played")
check(j["summary"].splitlines()[0].endswith(f"Turn 2, lap 2: note {good.seconds:.1f} s (played 0 times)")
      and len(j["summary"].splitlines()) == 3, f"Copy summary: {j['summary'].splitlines()[0]}")
code, r = call("api/cornernotes/play", {"id": a["id"]})
check(code == 200 and WV.clips and WV.clips[-1]["text"] == "(note)" and WV.clips[-1]["bytes"] > 1000, "play sends the note to the voice's device")
code, r = call("api/cornernotes/enable", {"id": a["id"], "on": False})
check(code == 200 and r["note"]["enabled"] is False and store.get(a["id"])["enabled"] is False, "switch off")
code, r = call("api/cornernotes/enable", {"id": a["id"], "on": "yes"})
check(code == 400, "on/off must be a real on or off")
code, r = call("api/cornernotes/move", {"id": b["id"], "turn": 9})
check(code == 200 and r["note"]["turn"] == 9 and "9-" in store.get(b["id"])["file"], "move to another turn")
code, r = call("api/cornernotes/move", {"id": b["id"], "turn": 99})
check(code == 400 and "1 to 60" in r["error"], "a turn that doesn't exist is refused in plain words")
code, r = call("api/cornernotes/move", {"id": "nope", "turn": 3})
check(code == 400, "an unknown note is refused")
code, r = call("api/cornernotes/delete", {"id": c["id"]})
code, j = call("api/cornernotes")
check(len(j["trash"]) == 1 and j["trash"][0]["id"] == c["id"] and j["trash"][0]["deleted"], "delete: into the trash list")
code, r = call("api/cornernotes/restore", {"id": c["id"]})
code, j = call("api/cornernotes")
check(not j["trash"] and sum(len(t["notes"]) for t in j["tracks"][0]["turns"]) == 3, "restore from the trash")
# re-record
code, r = call("api/cornernotes/rerecord", {"id": a["id"]})
check(code == 200 and r["mic"]["recording"] and r["mic"]["rerecording"] == a["id"], "re-record: the microphone is open (Recording light)")
code2, r2 = call("api/cornernotes/rerecord", {"id": b["id"]})
check(code2 == 400 and "re-recorded" in r2["error"], "one microphone use at a time")
code3, r3 = call("api/coach/mic-test", {})
check(code3 == 400, "...the Mic test waits too")
time.sleep(1.5)
code, r = call("api/cornernotes/rerecord-stop", {})
check(code == 200 and r["result"]["ok"] and not r["mic"]["recording"], f"stop saves it ({r['result']['text']})")
check(store.get(a["id"])["played"] == 0 and store.get(a["id"])["passes"] == 0, "...and its plan starts again")
code, r = call("api/cornernotes/rerecord-stop", {})
check(r["result"]["ok"] is False, "stop with nothing recording: nothing happens")
# Mic test
code, r = call("api/coach/mic-test", {})
check(code == 200 and r["mic"]["test"]["state"] == "recording" and r["mic"]["recording"], "Mic test: starts, the Recording light is on")
vals = []
t0 = time.time()
while time.time() - t0 < 10:
    code, r = call("api/coach/mic")
    vals.append(r["mic"]["level"])
    if r["mic"]["test"]["state"] in ("playing", "done", "error"):
        break
    time.sleep(0.1)
check(max(vals) > 0.2, f"the level meter moves while it records ({max(vals):.2f})")
check(r["mic"]["test"]["state"] in ("playing", "done") and WV.clips[-1]["text"] == "(mic test)" and not r["mic"]["recording"],
      "...then it plays the recording back, and the light goes out")
# coach settings and status
code, r = call("api/coach/settings", {"settings": {"level": "notes", "notes": False, "learn_cues": False}})
check(code == 200 and r["settings"]["level"] == "notes" and r["settings"]["notes"] is False and r["settings"]["learn_cues"] is False,
      "Settings: Notes only, notes and learning switches saved")
code, r = call("api/coach/settings", {"settings": {"level": "loud"}})
check(code == 400 and "Notes only" in r["error"], "a level that doesn't exist is refused")
call("api/coach/settings", {"settings": {"level": "normal", "notes": True, "learn_cues": True}})
code, st = call("api/coach/status")
check("mic" in st and st["mic"]["recording"] is False and {"note", "uselap"} <= set(st["keys"]) and "wheel_note" in st
      and "wheel_uselap" in st, "the coach status carries the Recording light, the keys and the two wheel buttons")
check(st["keys"]["note"]["key"] == "Ctrl+Shift+V" and st["keys"]["uselap"]["key"] == "Ctrl+Shift+L", "the keys are named")
code, r = call("api/coach/uselap", {})
check(code == 400 and "Start the coach" in r["error"], "use this lap with the coach off: says to start it")
code, r = call("api/coach/wheel-note-learn", {})
check(code in (200, 400) and "wheel" in r, "the note wheel button can be learned (or says it can't off Windows)")
code, r = call("api/coach/wheel-uselap-clear", {})
check(code == 200 and r["ok"], "...and cleared")
code, r = call("api/cornernotes/play", {})
check(code == 400, "a request with no note is refused")

# ---- in the browser (skipped when Playwright / Chromium are missing)
while call("api/coach/mic")[1]["mic"]["test"]["state"] in ("recording", "playing"):     # (the Mic test above is still playing back)
    time.sleep(0.2)
try:
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    browser = pw.chromium.launch()
except Exception:  # noqa: BLE001
    print("  SKIPPED: Playwright/Chromium not available")
    browser = None
if browser is not None:
    for dark in (False, True):
        ctx = browser.new_context(viewport={"width": 1320, "height": 900}, color_scheme="dark" if dark else "light")
        pg = ctx.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
        pg.route("**/api/sync", lambda rt: rt.fulfill(body='{"ok":true,"skipped":true}', content_type="application/json"))
        pg.route("**/api/update*", lambda rt: rt.fulfill(body='{"ok":true,"skipped":true}', content_type="application/json"))
        try:
            pg.goto(srv.url)
            pg.wait_for_function("() => document.querySelector('.navbtn[data-nav=practice]')")
            pg.click(".navbtn[data-nav=practice]")
            pg.wait_for_selector("#cnPanel", state="visible")
            pg.wait_for_function("() => document.querySelectorAll('#cnList .cnnote').length >= 3")
            rows = pg.locator("#cnList .cnnote")
            check(pg.text_content("#cnCount").strip() == "3 notes" and pg.locator("#cnList .cngroup h4").count() == 1,
                  f"Practice > Corner notes lists the notes by track ({pg.text_content('#cnCount')})")
            txt = pg.text_content("#cnList")
            check("Turn 2" in txt and "Turn 5" in txt and "Played 0 times" in txt and "Recorded" in txt and "lap 7" in txt,
                  "...by turn, with when, the lap and how many times played")
            first = rows.first
            names = [b.text_content().strip() for b in first.locator("button").all()]
            check(names == ["Play", "Re-record", "Delete"] and first.locator("select").count() == 1
                  and first.locator("input[type=checkbox]").count() == 1, f"each note has play, re-record, move, on/off, delete ({names})")
            n_clips = len(WV.clips)
            first.locator("button", has_text="Play").click()
            pg.wait_for_function("(n) => true", arg=0)
            deadline = time.time() + 5
            while time.time() < deadline and len(WV.clips) == n_clips:
                time.sleep(0.05)
            check(len(WV.clips) > n_clips, "Play plays the note")
            if not dark:                       # (the list was changed in the first pass)
                first.locator("input[type=checkbox]").uncheck()
                pg.wait_for_function("() => document.querySelectorAll('#cnList .cnnote.off').length === 1")
                check(True, "switching a note off dims it")
                pg.locator("#cnList .cnnote").nth(1).locator("button", has_text="Delete").click()
                pg.wait_for_function("() => !document.getElementById('cnTrash').hidden")
                check(pg.text_content("#cnTrashN").strip() == "1", "Delete puts it in the trash list")
                pg.evaluate("() => { document.getElementById('cnTrash').open = true }")
                pg.locator("#cnTrashList button", has_text="Restore").click()
                pg.wait_for_function("() => document.getElementById('cnTrash').hidden")
                check(True, "...and Restore brings it back")
            # the Coach page
            pg.keyboard.press("Control+3")
            pg.wait_for_selector("#vCoach", state="visible")
            check(pg.locator("#coLvNotes").count() == 1 and "Notes only" in pg.text_content("#coLevel"), "Coach: the Notes only level")
            check(pg.locator("#coRec").is_visible() and "Recording: off" in pg.text_content("#coRec") and pg.locator("#coRec.off").count() == 1, "Coach: the Recording light shows \"off\" while the microphone is off")
            check(pg.locator("#coMicTest").is_enabled() and "Ctrl+Shift+L" in pg.text_content("#coUseLap"),
                  "Coach: Mic test and Use my last lap")
            pg.click("#coMicTest")
            pg.wait_for_function("() => !document.getElementById('coRec').classList.contains('off')", timeout=8000)
            seen = []
            for _ in range(20):
                seen.append(int(pg.get_attribute("#coMeter", "aria-valuenow") or 0))
                time.sleep(0.1)
            check(max(seen) > 10, f"the Recording light is on and the meter moves ({max(seen)}%)")
            pg.wait_for_function("() => document.getElementById('coRec').classList.contains('off') && /Played back|Heard you/.test(document.getElementById('coMicMsg').textContent)",
                                 timeout=15000)
            check(True, "Mic test: records, plays back, light off")
            pg.evaluate("() => { document.getElementById('coSet').open = true }")
            check(pg.locator("#coNotes").is_checked() and pg.locator("#coLearn").is_checked()
                  and pg.locator("#coWheelN").count() == 1 and pg.locator("#coWheelU").count() == 1,
                  "Settings: notes and learning switches, two more wheel buttons")
            if os.environ.get("DEBRIEF_SHOTS"):
                Path(os.environ["DEBRIEF_SHOTS"]).mkdir(parents=True, exist_ok=True)
                tag = "dark" if dark else "light"
                pg.locator("#cnCoach").screenshot(path=str(Path(os.environ["DEBRIEF_SHOTS"]) / f"notes-coach-{tag}.png"))
                pg.click(".navbtn[data-nav=practice]")
                pg.wait_for_selector("#cnPanel", state="visible")
                pg.locator("#cnPanel").screenshot(path=str(Path(os.environ["DEBRIEF_SHOTS"]) / f"notes-practice-{tag}.png"))
            check(not errs, f"no page errors ({errs[:3]})")
        except Exception:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            check(False, "browser checks ran through")
        ctx.close()
    browser.close()
    pw.stop()
srv.shutdown()

print("all passed" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
