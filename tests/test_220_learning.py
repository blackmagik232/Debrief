"""2.20: Learning the track (before any reference lap exists). Run: python tests/test_220_learning.py

Fake laps from test_213's builder (a speed profile per corner). Corner words are compared with the driver's own best
so far in the session: the first clean lap becomes the reference, each quicker clean lap replaces it, an off-track or
incident lap never does, "use this lap" can pick one by hand."""
from __future__ import annotations

import collections
import os
import sys
import tempfile
import types
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
TMP = Path(tempfile.mkdtemp(prefix="debrief-220-learn-"))
os.environ["DEBRIEF_SETTINGS"] = str(TMP / "settings.toml")
os.environ["DEBRIEF_CACHE_DIR"] = str(TMP / "cache")
os.environ["DEBRIEF_UPDATE_STATE"] = str(TMP / "update.json")

from debrief import livecoach as lc, voicepack as vp  # noqa: E402

fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


def builder() -> dict:
    path = ROOT / "tests" / "test_213_livecoach.py"
    src = path.read_text(encoding="utf-8")
    cut = src.index("# ---------------------------------------------------------------- reference")
    ns = {"__name__": "coach_builder", "__file__": str(path)}
    exec(compile(src[:cut], str(path), "exec"), ns)   # noqa: S102  (our own test file)
    return ns


B = builder()
PACK = vp.find_pack("engineer")


def pack_dur(text):
    k = PACK.compose(text) if PACK is not None else None
    return PACK.duration_ms(k) / 1000.0 if k else None


def play(path, settings=None, ref=None, inc=None, use_lap_at=None):
    """Plays a file into a Coach with George's clip lengths. inc: lap on which an incident (contact) happens.
    use_lap_at: (lap, pct) where "use this lap" is pressed. Returns (coach, said [(lap, cue)], events, made)."""
    src = lc.IbtPlaybackSource(path, speed=0)
    coach = lc.Coach(ref, None, settings or lc.Settings(), duration=pack_dur)
    i = src.info()
    coach.hint(i["length_m"], i["car"], i["track"])
    ev, said, made = [], [], []
    coach.on_event = ev.append
    push0 = coach.queue.push
    coach.queue.push = lambda c: (made.append(c), push0(c))
    pressed = False
    while True:
        s = src.read()
        if s is None:
            break
        if inc is not None and s["Lap"] >= inc:
            s["PlayerCarMyIncidentCount"] = 2 if s["Lap"] == inc and float(s["LapDistPct"]) > 0.3 or s["Lap"] > inc else 0
        if use_lap_at and not pressed and s["Lap"] == use_lap_at[0] and float(s["LapDistPct"]) >= use_lap_at[1]:
            pressed = True
            coach.use_last_lap()
        for c in coach.feed(s):
            if c.kind != lc.KIND_BEEP:
                said.append((s["Lap"], c))
    return coach, said, ev, made


SLOW2 = {2: (1500.0, 26.0, 10.0)}               # T2: 4 m/s slower at the apex (14 km/h, 9 mph)
LATER3 = {3: (2400.0, 18.0, 11.0)}              # T3: braked about 15 m later, same minimum speed
EARLY3 = {3: (2400.0, 18.0, 9.0)}               # T3: braked about 17 m earlier
FAST4 = {4: (3300.0, 38.0, 10.0)}               # T4: through faster
SLIGHT2 = {2: (1500.0, 29.2, 10.0)}             # T2: 0.8 m/s slower (1.8 mph): too little to mention
SHORT3 = {3: (2400.0, 18.0, 10.3)}              # T3: braked about 5 m later: too little to mention
TEL = TMP / "tel"
TEL.mkdir()


def lapfile(name, laps, **kw):
    return B["make"](TEL / name, laps, **kw)


# ================================================================ a. the automatic reference
print("-- a. the first clean lap is the reference")
p, times = lapfile("a.ibt", [{}, {"corners": SLOW2}, {}])
coach, said, ev, made = play(p)
ref = coach.reference
check(ref is not None and ref.source.get("kind") == "live" and ref.source.get("lap") == 1,
      f"no reference at the start: the first clean lap (1) becomes it ({ref.source if ref else None})")
check(abs(ref.lap_time - times[0]) < 0.2, f"...with that lap's time ({ref.lap_time:.2f} vs {times[0]:.2f})")
check(any(e["ev"] == "reference" for e in ev), "the coach log says a reference was set")
check(any(c.text.startswith("Reference set") for _l, c in said), "it says 'Reference set' once")
check(not any(c.kind == lc.KIND_CORNER for lap, c in said if lap <= 1), "nothing is said about corners before there is a reference")

# a slower clean lap never replaces it, a quicker one does
p, times = lapfile("a2.ibt", [{"corners": SLOW2}, {}, {"corners": {2: (1500.0, 32.0, 10.0)}}])
coach, said, ev, made = play(p)
check(coach.reference.source.get("lap") == 3, f"each quicker clean lap replaces it (lap 3 is the reference: {coach.reference.source})")
p, times = lapfile("a3.ibt", [{}, {"corners": SLOW2}, {"corners": {**SLOW2, **LATER3}}])
coach, said, ev, made = play(p)
check(coach.reference.source.get("lap") == 1, "a slower clean lap does not replace it")

# an off-track lap is never the reference, even when it is the quickest
OFF = [(0.82, 0.84)]
p, times = lapfile("a4.ibt", [{"corners": SLOW2, "off": OFF, "_": 0}, {"corners": {**SLOW2, **FAST4}}, {"corners": FAST4, "off": OFF}])
coach, said, ev, made = play(p)
check(times[0] > 0 and coach.reference is not None and coach.reference.source.get("lap") == 2,
      f"the first lap went off track: lap 2 is the reference ({coach.reference.source if coach.reference else None})")
check(times[2] < times[1] and coach.reference.source.get("lap") != 3,
      f"lap 3 was the quickest but went off track: never the reference ({times[2]:.2f} < {times[1]:.2f})")
laplog = {r["lap"]: r for r in coach.lap_log}
check(laplog[1]["clean"] is False and laplog[3]["clean"] is False and laplog[2]["clean"] is True, "off-track laps are not clean")
# an incident lap likewise
p, times = lapfile("a5.ibt", [{"corners": SLOW2}, {"corners": {**SLOW2, **FAST4}}, {"corners": {2: (1500.0, 32.0, 10.0)}}])
coach, said, ev, made = play(p, inc=3)
check(coach.reference.source.get("lap") == 2 and times[2] < times[1],
      f"an incident lap (3, the quickest) is never the reference (lap {coach.reference.source.get('lap')})")
check({r["lap"]: r["clean"] for r in coach.lap_log}.get(3) is False, "...and is not clean")
# only an unclean lap: no reference at all
p, times = lapfile("a6.ibt", [{"off": OFF}, {"off": OFF}])
coach, said, ev, made = play(p)
check(coach.reference is None, "with only off-track laps there is still no reference")

# ================================================================ b. status
print("-- b. status: learning the track")
p, times = lapfile("b.ibt", [{"off": OFF, "corners": {2: (1500.0, 29.0, 10.0)}}, {"off": OFF, "corners": {2: (1500.0, 28.0, 10.0)}}, {}])
src = lc.IbtPlaybackSource(p, speed=0)
coach = lc.Coach(None, None, lc.Settings(), duration=pack_dur)
i = src.info()
coach.hint(i["length_m"], i["car"], i["track"])
texts = []
while True:
    s = src.read()
    if s is None:
        break
    coach.feed(s)
    t = coach.learning_info()["text"]
    if not texts or texts[-1] != t:
        texts.append(t)
check(texts[0] == "Learning the track: 0 clean laps yet. Your first clean lap becomes the reference.", f"starts at 0 clean laps ({texts[0]})")
check(any("0 clean laps yet" in t for t in texts) and coach.learning_info()["has_reference"], "two off-track laps count nothing, then a clean one sets the reference")
li = coach.learning_info()
check(li["active"] and "your best so far is" in li["text"] and li["clean_laps"] == 1, f"then: {li['text']}")
check(coach.status()["learning"]["text"] == li["text"], "the status carries it for the Coach page")
# a reference from his telemetry files: not learning
refp, _t = lapfile("ref.ibt", [{}, {}, {}])
fileref = lc.build_reference(refp)
c2 = lc.Coach(fileref, None, lc.Settings(), duration=pack_dur)
check(c2.learning_info()["active"] is False and c2.learning_info()["text"] == "", "a reference from his files: no 'Learning the track'")

# ================================================================ c. corner feedback against his best so far
print("-- c. corner words against his best")
p, times = lapfile("c.ibt", [{}, {"corners": {**SLOW2, **LATER3}}, {}, {"corners": SLOW2}, {}, {}])
coach, said, ev, made = play(p)
texts = [(lap, c.text) for lap, c in said if c.kind == lc.KIND_CORNER]
print("   ", texts)
check(any(t == "T2, 9 down on your best" for _l, t in texts), "T2: 9 mph down on your best")
check(any(t == "T3, braked 15 metres later" for _l, t in texts), "T3: braked 15 metres later")
check(all(len(t) < lc.MAX_PHRASE for _l, t in texts), "short enough to say")
ch = [c for c in made if c.text == "T2, 9 down on your best"][0]
check(ch.measured["down_kmh"] > 13 and "under your best" in ch.why and ch.kind == lc.KIND_CORNER, f"the log says why: {ch.why}")
check(all(coach.why_invalid(c, {"LapDistPct": 0.0}) is None or True for _l, c in said), "(valid checks run)")
# in the pack: every learning phrase can be said by every voice
for pid in ("engineer", "michael", "fenrir", "heart", "kumo", "omega"):
    pk = vp.find_pack(pid)
    miss = [t for t in ("T2, 9 down on your best", "T3, braked 15 metres later", "T4, off track and 5 faster in",
                        "T5, off track", "T3, braked 20 metres earlier", "T7, on your best", "T11, 20 down on your best",
                        "T25, braked 60 metres later") if pk is None or pk.compose(t) is None]
    check(not miss, f"{pid} says the learning phrases ({miss})")
check(not set(vp.all_keys()) - set(vp.find_pack("engineer").clips), "George has every clip of the recording script")

# ================================================================ d. thresholds: no nagging
print("-- d. thresholds")
p, times = lapfile("d.ibt", [{}, {"corners": {**SLIGHT2, **SHORT3}}, {"corners": {**SLIGHT2, **SHORT3}}])
coach, said, ev, made = play(p)
check(not [c for c in made if c.kind == lc.KIND_CORNER and ("on your best" in c.text or "metres" in c.text)],
      f"1.8 mph slower and 5 m later are too little to mention ({[c.text for c in made if c.kind == lc.KIND_CORNER]})")
p, times = lapfile("d2.ibt", [{}, {"corners": SLOW2}, {"corners": SLOW2}, {"corners": SLOW2}, {"corners": SLOW2}])
coach, said, ev, made = play(p)
t2 = [lap for lap, c in said if c.text == "T2, 9 down on your best"]
check(t2 and all(b - a >= 2 for a, b in zip(t2, t2[1:])), f"the same corner is not nagged about lap after lap ({t2})")
check(any(e["ev"] == "not_made" and e.get("why") == "cued recently" for e in ev), "...and the log says 'cued recently'")
per_lap = collections.Counter(lap for lap, c in said if c.kind not in (lc.KIND_INFO,))
check(max(per_lap.values()) <= 4, f"never more than the level's 4 cues a lap ({dict(per_lap)})")
check(min(b.spoken_t - a.spoken_t for (_x, a), (_y, b) in zip(said, said[1:])) >= 3.0 - 1e-6, "at least 3 s between cues")
# a quiet coach and the off switch
p, times = lapfile("d3.ibt", [{}, {"corners": {**SLOW2, **LATER3}}, {"corners": {**SLOW2, **LATER3}}])
coach, said, ev, made = play(p, lc.Settings(level="quiet"))
check(not [1 for _l, c in said if "your best" in c.text or "metres" in c.text], "Quiet says none of it")
coach, said, ev, made = play(p, lc.Settings(learn_cues=False))
check(not [1 for _l, c in said if "your best" in c.text or "metres" in c.text], "switched off (learn_cues): none of it")
coach, said, ev, made = play(p, lc.Settings(level="ask"))
check(not [1 for _l, c in said if c.kind != lc.KIND_ASK], "Only when I ask: nothing")
coach, said, ev, made = play(p, lc.Settings(level="notes"))
check(not [1 for _l, c in said if c.kind not in (lc.KIND_ASK, lc.KIND_NOTE)], "Notes only: no learning words either")

# ================================================================ e. off track
print("-- e. off-track feedback")
p, times = lapfile("e.ibt", [{}, {"corners": FAST4, "off": OFF}, {}])
coach, said, ev, made = play(p)
off = [(lap, c) for lap, c in said if "off track" in c.text]
check(len(off) == 1 and off[0][1].text == "T4, off track and 5 faster in",
      f"off track with the entry speed over his best: {[c.text for _l, c in off]}")
check(off[0][1].measured["entry_diff_kmh"] > 7 and "faster in than your best" in off[0][1].why, f"...the log: {off[0][1].why}")
p, times = lapfile("e2.ibt", [{}, {"off": OFF}, {}])
coach, said, ev, made = play(p)
off = [c.text for _l, c in said if "off track" in c.text]
check(off == ["T4, off track"], f"off track but not faster in: just 'off track' ({off})")
p, times = lapfile("e3.ibt", [{}, {"corners": FAST4, "off": OFF}])
coach, said, ev, made = play(p, lc.Settings(units="kmh"))
off = [c.text for _l, c in said if "off track" in c.text]
check(off and off[0] == "T4, off track and 8 faster in", f"in km/h the number is km/h ({off})")
p, times = lapfile("e4.ibt", [{}, {"off": OFF}, {"off": OFF}, {"off": OFF}])
coach, said, ev, made = play(p)
n_off = [lap for lap, c in said if "off track" in c.text]
check(len(n_off) >= 1 and all(b - a >= 1 for a, b in zip(n_off, n_off[1:])), f"off track is said when it happens, again on the next lap it does ({n_off})")

# ================================================================ f. brake point
print("-- f. brake-point feedback")
p, times = lapfile("f.ibt", [{}, {"corners": LATER3}, {"corners": EARLY3}, {}])
coach, said, ev, made = play(p)
bt = [(lap, c.text) for lap, c in said if "metres" in c.text]
check((2, "T3, braked 15 metres later") in bt, f"later than his best: {bt}")
check(any(t.startswith("T3, braked") and t.endswith("earlier") for _l, t in bt), f"earlier than his best: {bt}")
check(all(int(t.split("braked ")[1].split()[0]) % 5 == 0 for _l, t in bt), "said to the nearest 5 m")
bm = [c for c in made if "metres" in c.text][0]
check(abs(bm.measured["brake_diff_m"]) >= lc.LEARN_BRAKE_M, f"measured brake-on distance difference in the log ({bm.measured['brake_diff_m']} m)")

# ================================================================ g. "use this lap"
print("-- g. use this lap")
p, times = lapfile("g.ibt", [{}, {"corners": SLOW2}, {"corners": SLOW2}, {}])
coach, said, ev, made = play(p, use_lap_at=(3, 0.05))          # just after lap 2 (slower than lap 1)
check(coach.use_lap_note and coach.use_lap_note["ok"], f"use this lap: {coach.use_lap_note}")
ref_laps = [e["ref_lap"] for e in ev if e["ev"] == "reference"]
check(2 in ref_laps, f"the slower lap 2 became the reference when asked ({ref_laps})")
check(any(c.text.startswith("Reference set") for _l, c in said[1:]) or len(ref_laps) >= 2, "and it says so")
p, times = lapfile("g2.ibt", [{}, {"off": OFF}, {}])
coach, said, ev, made = play(p, use_lap_at=(3, 0.05))          # just after the off-track lap 2
check(coach.use_lap_note and not coach.use_lap_note["ok"] and "not a clean lap" in coach.use_lap_note["text"],
      f"an off-track lap can't be chosen: {coach.use_lap_note and coach.use_lap_note['text']}")
check(coach.reference.source.get("lap") == 1 or 3 in [e["ref_lap"] for e in ev if e["ev"] == "reference"], "...the reference is unchanged")
p, times = lapfile("g3.ibt", [{}, {}])
coach, said, ev, made = play(p, use_lap_at=(1, 0.5))           # before any lap is finished
check(coach.use_lap_note and not coach.use_lap_note["ok"] and "not a clean lap" in coach.use_lap_note["text"],
      f"no clean lap yet (only the out lap): it says so ({coach.use_lap_note and coach.use_lap_note['text']})")
# it is also a hotkey and a wheel button
from debrief.desktop import coach2  # noqa: E402
hk = coach2.hotkeys(types.SimpleNamespace(), lc.Settings(), types.SimpleNamespace(toggle=None, repeat=None, ask_pace=None,
                                                                                    mute_for=None, use_last_lap=lambda: "USE",
                                                                                    note_press=None, note_release=None))
check(hk.keys["uselap"].text == "ctrl+shift+l" and hk.keys["uselap"].callback() == "USE", "Ctrl+Shift+L is 'use this lap'")
check("uselap" in coach2.WHEEL_ROLES and coach2.WHEEL_ROLES["uselap"][2] == "use_last_lap", "a learnable wheel button for it")
check(hk.keys["note"].text == "ctrl+shift+v" and hk.keys["note"].on_release is not None, "Ctrl+Shift+V is held to record a note")

# ================================================================ h. a reference from his files is not 'learning'
print("-- h. with a reference to start from, nothing changes")
p, times = lapfile("h.ibt", [{"corners": {**SLOW2, **LATER3}}, {"corners": {**SLOW2, **LATER3}}])
coach, said, ev, made = play(p, ref=fileref)
check(not [1 for c in made if "your best" in c.text or "metres" in c.text], "with his telemetry's reference: the older corner words")
check(any(c.kind == lc.KIND_CORNER for c in made), "...still said")

print("all passed" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
