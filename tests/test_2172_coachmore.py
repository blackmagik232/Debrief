"""2.17.2: the coach talks more. Run: python tests/test_2172_coachmore.py

Coaching level (Quiet / Normal / Busy), a word after any corner where he lost or gained time against the reference,
sector deltas, focus corners learned from this session, "Building your reference" and "Reference set, ..." before
there is a reference, and the bundled voice (George) saying all of it. Fake telemetry from test_213's builder; his
real files (Documents\\iRacing\\telemetry) only when present (never in CI)."""
from __future__ import annotations

import collections
import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))
TMP = Path(tempfile.mkdtemp(prefix="debrief-2172-"))

from debrief import cuelog, livecoach as lc, voice as vc, voicepack as vp  # noqa: E402

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


def pack_dur(text):
    k = PACK.compose(text) if PACK is not None else None
    return PACK.duration_ms(k) / 1000.0 if k else None


def play(path, settings, ref=None, weak=None, hint=True):
    """Plays a file into a Coach with George's exact clip lengths. Returns (coach, said [(lap, cue, sample)], events)."""
    src = lc.IbtPlaybackSource(path, speed=0)
    coach = lc.Coach(ref, weak, settings, duration=pack_dur)
    if hint:
        i = src.info()
        coach.hint(i["length_m"], i["car"], i["track"])
    ev = []
    coach.on_event = ev.append
    said = []
    while True:
        s = src.read()
        if s is None:
            break
        for c in coach.feed(s):
            if c.kind != lc.KIND_BEEP:
                said.append((s["Lap"], c, s))
    return coach, said, ev


def safety(said, ev, settings, label):
    """Never under braking, never within 1 s of a brake point (cue end + audio delay), never overlapping."""
    braking = [c.text for _l, c, s in said if float(s.get("Brake") or 0) > settings.braking_pedal]
    check(not braking, f"{label}: no cue starts under braking ({braking[:3]})")
    short = [(e["text"], e.get("next_brake_s"), e["dur_s"]) for e in ev if e["ev"] == "spoken"
             and e.get("next_brake_s") is not None
             and e["next_brake_s"] < e["dur_s"] + settings.end_before_brake_s + settings.audio_latency_s - 1e-6]
    check(not short, f"{label}: every cue ends at least 1 s before the next brake point ({short[:3]})")
    end, over = -1e9, []
    for _l, c, _s in said:
        if c.spoken_t < end - 1e-6:
            over.append(c.text)
        end = c.spoken_t + (pack_dur(c.text) or len(c.text.split()) / 2.5) + settings.audio_latency_s
    check(not over, f"{label}: no cue overlaps the one before ({over[:3]})")
    ts = [c.spoken_t for _l, c, _s in said]
    gaps = [b - a for a, b in zip(ts, ts[1:])]
    check(all(g >= settings.gap() - 1e-6 for g in gaps),
          f"{label}: at most one cue every {settings.gap():g} s (smallest {min(gaps, default=0):.2f} s)")


def per_lap(said):
    return collections.Counter(lap for lap, _c, _s in said)


# ---------------------------------------------------------------- settings
print("-- Coaching level")
s0 = lc.Settings()
check(s0.level == "normal" and s0.per_lap() == 4 and s0.gap() == 3.0, "Normal is the default: 4 cues a lap, 3 s apart")
sb = lc.Settings(level="busy")
check(sb.per_lap() == 6 and sb.gap() == 2.5 and sb.lv("live_top") == 3, "Busy: 6 a lap, 2.5 s apart, 3 focus corners")
sq = lc.Settings(level="quiet")
check(sq.per_lap() == sq.max_per_lap == 2 and sq.gap() == sq.gap_s == 4.0 and sq.lv("live_top") == 0
      and not sq.lv("sectors") and not sq.lv("first_laps"), "Quiet: 2.17.0's limits and nothing new")
check(lc.Settings.from_dict({"level": "BUSY"}).level == "busy" and lc.Settings.from_dict({"level": "loud"}).level ==
      "normal" and lc.Settings.from_dict({"level": 3}).level == "normal", "level from coach.json: checked, else Normal")
check(lc.Settings.from_dict({**sq.to_dict()}).level == "quiet", "Quiet is kept in coach.json")
check("level" in cuelog.SETTINGS_KEYS and "sector" in cuelog.MEASURED_KEYS, "the cue log keeps the level and sector")

from debrief.desktop import server as S  # noqa: E402
check(S.clean_coach_settings({"level": "busy"}) == {"level": "busy"}, "the window may set the level")
for bad in ("loud", 2, None, ""):
    try:
        S.clean_coach_settings({"level": bad})
        check(False, f"level {bad!r} refused")
    except ValueError as e:
        check(str(e) == "Pick Quiet, Normal, Busy, Notes only or Only when I ask.", f"level {bad!r} refused in plain words")

# ---------------------------------------------------------------- words and voice
print("-- words and George")
check(lc.corner_time_text("T3", 0.21) == "T3, two tenths down" and lc.good_text("T3", 0.1) == "T3, good, up a tenth",
      "corner time words")
check(lc.sector_text(0, -0.24) == "Sector one, up two tenths" and lc.sector_text(1, 0.1) == "Sector two, a tenth down",
      "sector words")
check(lc.reference_set_text(98.874) == "Reference set, one thirty-eight nine", lc.reference_set_text(98.874))
check([vp.lap_time_words(x) for x in (65.04, 52.3, 120.0, 99.96)] ==
      ["one oh five oh", "fifty-two three", "two oh oh oh", "one forty oh"], "lap time words, to the tenth")
want = {"T3, a tenth down": ["turn_3", "lost_0_1"], "T3, good, up two tenths": ["turn_3", "good_lead", "up_0_2"],
        "Sector one, up two tenths": ["sector_1", "up_0_2"], "Sector three, 1.4 seconds down": ["sector_3", "lost_1_4"],
        "Building your reference": ["building_your_reference"],
        "Reference set, one thirty-eight nine": ["reference_set", "lap_98", "tenth_9"],
        "Reference set, fifty-two three": ["reference_set", "lap_52", "tenth_3"]}
for t, k in want.items():
    check(vp.compose(t) == k, f"compose({t!r}) = {vp.compose(t)}")
for t in ("T3, ten tenths down", "Sector four, up a tenth", "Sector one, good, up a tenth", "Reference set, three oh one one",
          "Reference set, sixty nine"):
    check(vp.compose(t) is None, f"compose({t!r}) is None (Windows speech says it)")
old_lines = [ln for ln in vp.lines() if ln["section"] in "ABCDEF"]
check(old_lines[-1]["text"] == "Building your reference." and all(ln["section"] in "GHIJKLMNO" for ln in vp.lines()[len(old_lines):]),
      "new lines come after the old ones (earlier line numbers never change)")
check(PACK is not None and not (set(vp.all_keys()) - set(PACK.clips)), "George has every clip, new ones too")
rates = [vp.loudness_db(PACK.clips[k], PACK.rate) for k in ("lost_0_2", "sector_1", "reference_set", "lap_98",
                                                              "tenth_9", "building_your_reference", "down_0_3")]
check(max(rates) - min(rates) < 1.5, f"the new clips are as loud as the old ones ({[round(r, 1) for r in rates]})")
import make_voicepack as mv  # noqa: E402
voc = [p for p in mv.coach_vocabulary() if not mv.by_design(p)]
cov = PACK.coverage(voc)
check(cov["share"] == 1.0 and len(voc) > 2500, f"George covers 100% of the coach's phrases ({len(cov['covered'])}/{len(voc)})")

# ---------------------------------------------------------------- fake laps: Normal, Busy, Quiet
print("-- fake laps")
TEL = TMP / "tel"
TEL.mkdir()
refp, _t = B["make"](TEL / "mclaren570sgt4_zolder gp ref.ibt", [{}, {}, {}])
ref = lc.build_reference(refp)
SLOW2 = {2: (1500.0, 26.0, 10.0)}               # T2: 4 m/s slower at the apex
EARLY3 = {3: (2400.0, 18.0, 7.0)}               # T3: braked early
FAST = {1: (600.0, 22.0, 10.0), 4: (3300.0, 38.0, 10.0)}    # T1, T4 quicker than the reference
laps = [{"corners": {**SLOW2, **EARLY3}}, {"corners": {**SLOW2, **FAST}}, {"corners": {**SLOW2, **EARLY3, **FAST}},
        {"corners": {**SLOW2}}, {"corners": {**EARLY3, **FAST}}]
cur, _times = B["make"](TEL / "mclaren570sgt4_zolder gp cur.ibt", laps)

coach, said, ev = play(cur, lc.Settings(), ref=ref)
pl = per_lap(said)
print("   Normal:", [(lap, c.text) for lap, c, _s in said])
check(all(1 <= pl.get(n, 0) <= 4 for n in range(1, 6)), f"Normal: 1 to 4 cues every lap ({dict(pl)})")
check(sum(pl.values()) > 2 * 5, f"Normal says far more than 2.17.0 ({sum(pl.values())} cues in 5 laps)")
kinds = collections.Counter(c.kind for _l, c, _s in said)
check(kinds[lc.KIND_CORNER] >= 3, f"Normal: words after corners ({dict(kinds)})")
safety(said, ev, coach.settings, "Normal")
goods = collections.Counter(lap for lap, c, _s in said if c.key.startswith("good:"))
check(goods and max(goods.values()) <= 1, f"Normal: at most 1 'good' a lap ({dict(goods)})")
check(any(c.text.startswith("T2") and lap > 1 for lap, c, _s in said), "slow T2 is talked about")
check(any(c.text in ("T3, braked early",) for _l, c, _s in said), "early braking at T3 is called early braking")
lt = [c for _l, c, _s in said if c.kind == lc.KIND_CORNER and not c.key.startswith("good:")]
check(all(c.measured.get("lost_s", 0) >= 0.1 - 1e-6 and c.why.startswith("lost ") for c in lt),
      "a corner word only where he lost a tenth or more, and the log says how much")
check([w.name for w in coach.learned] and coach.weak == coach.learned and {"T2", "T3"} & {w.name for w in coach.learned},
      f"no proven weak corners: focus corners learned from this session ({[(w.name, w.lost_s) for w in coach.learned]})")
fi = coach.focus_info()
check(fi["source"] == "session" and fi["text"].startswith("Learning from this session: ") and "—" not in fi["text"],
      f"status says it is learning: {fi['text']}")
check(any(e["ev"] == "focus_learned" and e.get("focus") for e in ev), "the cue log says when the focus was learned")
check(any(c.kind == lc.KIND_MARKER and c.corner in {w.corner.id for w in coach.learned} for _l, c, _s in said),
      "a learned focus corner gets its brake marker")
check(all(c.why for _l, c, _s in said), "every cue says why in the log")

cb, saidb, evb = play(cur, lc.Settings(level="busy"), ref=ref)
plb = per_lap(saidb)
print("   Busy:", dict(plb))
check(all(plb.get(n, 0) <= 6 for n in plb) and sum(plb.values()) >= sum(pl.values()),
      f"Busy: up to 6 a lap, at least as many as Normal ({sum(plb.values())} vs {sum(pl.values())})")
safety(saidb, evb, cb.settings, "Busy")
gb = collections.Counter(lap for lap, c, _s in saidb if c.key.startswith("good:"))
check(not gb or max(gb.values()) <= 2, f"Busy: at most 2 'good' a lap ({dict(gb)})")
check(len(cb.learned) <= 3, "Busy: up to 3 learned focus corners")

cq, saidq, _evq = play(cur, lc.Settings(level="quiet"), ref=ref)
check(all(c.kind == lc.KIND_LAP for _l, c, _s in saidq),
      f"Quiet with no weak corners: only the lap time, as in 2.17.0 ({[c.text for _l, c, _s in saidq]})")
check(not cq.learned and cq.focus_info()["source"] is None, "Quiet learns no focus corners")

# proven weak corners win over learned ones
cw, saidw, _evw = play(cur, lc.Settings(), ref=ref, weak=[{"id": "C2", "apex_m": 1500.0, "avg_avail": 0.3}])
check([w.name for w in cw.weak] == ["T2"] and not cw.learned and cw.focus_info()["source"] == "reviews",
      "with a race review's weak corner, that is the focus (nothing learned)")
fb = [lap for lap, c, _s in saidw if c.corner == "C2" and c.kind in (lc.KIND_FEEDBACK, lc.KIND_CORNER)]
check(len(fb) == len(set(fb)), "never two words about one corner in a lap")

# a car alongside: nothing while it's there
trafp, _ = B["make"](TEL / "mclaren570sgt4_zolder gp traffic.ibt",
                     [{"corners": SLOW2}, {"corners": SLOW2, "clr": [(0.20, 0.60, 2)]}, {"corners": SLOW2}])
ct, saidt, _e = play(trafp, lc.Settings(level="busy"), ref=ref)
inside = [c.text for lap, c, s in saidt if lap == 2 and 0.18 <= float(s["LapDistPct"]) <= 0.65]
check(not inside, f"car alongside: quiet until 2 s after it clears ({inside})")

# race: lap 1 quiet even in Busy
racep, _ = B["make"](TEL / "mclaren570sgt4_zolder gp race.ibt", [{"corners": SLOW2}, {"corners": SLOW2}], session="Race",
                     lead_pit=False)
cr, saidr, _e = play(racep, lc.Settings(level="busy", in_races=True), ref=ref)
check(not [c for lap, c, _s in saidr if lap <= 1] and saidr, f"race: nothing on lap 1, cues after ({len(saidr)})")
cr2, saidr2, _e = play(racep, lc.Settings(level="busy"), ref=ref)
check(not saidr2, "race: nothing at all while coaching in races is off")

# ---------------------------------------------------------------- the first laps: no reference yet
print("-- first laps")
firstp, ftimes = B["make"](TEL / "mclaren570sgt4_zolder gp first.ibt", [{}, {"corners": SLOW2}, {"corners": SLOW2}])
c1, said1, ev1 = play(firstp, lc.Settings())
texts = [(lap, c.text) for lap, c, _s in said1]
print("   ", texts)
check(texts and texts[0] == (0, "Building your reference"), "out lap: 'Building your reference', said once")
check(sum(t == "Building your reference" for _l, t in texts) == 1, "...only once")
b0 = said1[0][2]
check(float(b0["Brake"]) <= 0.05 and (bool(b0["OnPitRoad"]) or float(b0["Throttle"]) >= 0.9),
      "...rolling out of the pits or on a straight, never braking")
rs = [(lap, t) for lap, t in texts if t.startswith("Reference set, ")]
check(len(rs) == 1 and rs[0][0] == 2 and rs[0][1] == lc.reference_set_text(ftimes[0], short=True),
      f"after the first clean lap: '{rs and rs[0][1]}' ({vp.lap_time_short_words(ftimes[0])}, short by default)")
check(c1.reference is not None and c1.reference.source.get("kind") == "live", "that lap is the reference")
check(any(lap >= 2 and t.startswith("T2") for lap, t in texts), "from lap 2 the corners are compared with it")
check(PACK.compose(rs[0][1]) is not None if rs else False, "George says 'Reference set' and the time")
c1q, said1q, _e = play(firstp, lc.Settings(level="quiet"))
check(not [c for _l, c, _s in said1q if c.kind == lc.KIND_INFO], "Quiet: no first-lap words (as in 2.17.0)")
cn, saidn, _e = play(firstp, lc.Settings(), ref=ref)
check(not [c for _l, c, _s in saidn if c.kind == lc.KIND_INFO], "with a reference from his files: no first-lap words")

# the runner's status explains the reference and the focus
r = lc.LiveRunner(lc.IbtPlaybackSource(cur, speed=0), vc.FakeVoice(), reference=ref)
r._ensure_reference()
st = r.status()
check(st["reference_why"] == "your best lap in your telemetry files" and "(your best lap in your telemetry files)"
      in st["text"], f"status names where the reference comes from: {st['text']}")
check(st["level"] == "normal" and st["focus_info"]["text"].startswith("Learning from this session"),
      f"status: {st['focus_info']['text']}")
r.update_settings({"level": "quiet"})
check(r.status()["level"] == "quiet" and "(" not in r.status()["text"], "Quiet: the status line as in 2.17.0")
check(lc.reference_why(c1.reference) == "your best clean lap this session", "a lap from this session")

# ---------------------------------------------------------------- the page
print("-- the page")
html = (ROOT / "debrief" / "desktop" / "ui" / "index.html").read_text(encoding="utf-8")
js = (ROOT / "debrief" / "desktop" / "ui" / "app.js").read_text(encoding="utf-8")
m = re.search(r'<fieldset class="radios colevel" id="coLevel">(.*?)</fieldset>', html, re.S)
check(m and [v for v in re.findall(r'value="(\w+)"', m.group(1))] == ["quiet", "normal", "busy", "notes", "ask"],
      "the Coach page has the level picker: Quiet, Normal, Busy, Notes only (2.20), Only when I ask")
check(m and all(w in m.group(1) for w in ("<b>Quiet</b>", "<b>Normal</b>", "<b>Busy</b>")) and "—" not in m.group(1)
      and "–" not in m.group(1), "each level with a one-line explanation, no long dashes")
check("coLevelShow" in js and "coach/settings',{settings:{level:" in js and "focus_learned:'Focus learned'" in js,
      "the page saves the level at once and shows the focus")
check("(from this session)" in js, "learned focus corners are labelled as from this session")

# ---------------------------------------------------------------- his real files
print("-- real laps")
REAL = Path("/mnt/user-data/uploads/Documents/iRacing/telemetry")
files = sorted(REAL.glob("mclaren570sgt4_zolder*.ibt")) if REAL.is_dir() else []
if len(files) >= 3:
    for level in ("quiet", "normal", "busy"):
        dist = collections.Counter()
        tot = 0
        for f in files:
            rref = lc.build_reference([p for p in files if p != f])
            st_ = lc.Settings(level=level, in_races=True)
            co, sd, evr = play(f, st_, ref=rref)
            safety(sd, evr, st_, f"real {level} {f.name[-23:-4]}") if sd else None
            per = per_lap(sd)
            for lap in {x["lap"] for x in co.lap_log if x["clean"]}:
                dist[per.get(lap, 0)] += 1
            tot += len(sd)
        print(f"   {level}: cues per clean lap {dict(sorted(dist.items()))} ({sum(dist.values())} clean laps)")
        if level == "normal":
            check(set(dist) <= {2, 3, 4} and sum(dist.values()) >= 10,
                  f"real Zolder laps, Normal: 2 to 4 cues every clean lap ({dict(sorted(dist.items()))})")
        if level == "busy":
            check(max(dist) <= 6, f"real Zolder laps, Busy: never more than 6 ({dict(sorted(dist.items()))})")
        if level == "quiet":
            check(max(dist) <= 3, f"real Zolder laps, Quiet: as few as before ({dict(sorted(dist.items()))})")
else:
    print("   (his telemetry files are not here: skipped)")

print(f"\n{'FAILED ' + str(fails) if fails else 'ALL OK'}")
sys.exit(1 if fails else 0)
