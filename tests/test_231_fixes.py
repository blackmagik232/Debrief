"""Debrief 2.31 review round 2: one regression test for each fix.

    1 priority          engineer sound priority below the crew chief's, ranks after every crew call, a dropped call is forgiven
    2 swallow           a question only offers its answer; a waiting box call is not swallowed
    3 repeat            "say again" never replays a stale crew call
    4 car state         a new session starts with a fresh car state; an out lap never makes the next lap an "in lap"
    5 last lap          "last lap of the session" only when the clock runs out in this lap; "how long left" under a minute
    6 crew calls off    the engineer's waiting messages are still pumped
    7 cold              a temperature below zero is shown and left out of the speech (no crash)
    8 briefing          "Start position" only at the start; "You are position N" after a stop; fuel laps from the board
    9 target            the crew chief's median of recent laps is never shown as "your target"
    10 intents          fuel questions, quiet keeps traffic warnings, infinite readings
    11 model download   size caps, and the fingerprint shown in Settings

    python3 tests/ci_run.py tests/test_231_fixes.py
Windows-safe: no signals, no /tmp literals.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.modules.setdefault("irsdk", None)
TMP = Path(tempfile.mkdtemp(prefix="debrief-231f-"))
os.environ["DEBRIEF_SETTINGS"] = str(TMP / "settings.toml")
os.environ["DEBRIEF_CACHE_DIR"] = str(TMP / "cache")

import eng_sim as ES  # noqa: E402
from debrief import audioout, crewchief as cc, engineer as EN, intents, livecoach as lc, sessionkind as SK, stt, voice as vc  # noqa: E402

fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


class V(vc.FakeVoice):
    """A voice like the pack: takes radio= and on_drop=; remembers them."""

    def say(self, text, priority=0, t=None, interrupt=False, check=None, radio=False, on_drop=None):
        self.drops = getattr(self, "drops", {})
        self.drops[text] = on_drop
        return super().say(text, priority, t)


class Src:
    name = "stand-in"


def runner(role="driver", kind="race", clock0=1000.0, **settings):
    clock = [clock0]
    r = lc.LiveRunner(Src(), V(), mode="live", clock=lambda: clock[0]) if False else lc.LiveRunner(Src(), V(), mode="live")
    r._clock = lambda: clock[0]
    for k, v in settings.items():
        setattr(r.settings, k, v)
    sim = ES.Sim(kind)
    sim.on_track, sim.garage, sim.surf = True, False, 3
    own = sim.own()
    own["LapDistPct"], own["Brake"], own["CarLeftRight"] = 0.3, 0.0, 1
    r._latest = own
    r.engineer.feed(own, None, sim.sections, 1, clock[0], role=role)
    return r, own, clock


def box_call(own, t, lap=None):
    c = cc.Call(kind="box_now", rank=3, say="Box this lap.", text="Box this lap.", lap=own["Lap"] if lap is None else lap, t=t,
                session_t=own["SessionTime"], priority=4, ttl=14.0, data={})
    c.id = 7
    return c


# ---------------------------------------------------------------- 1
def test_1_priorities():
    for k, (rank, prio, _ttl) in EN.KINDS.items():
        check(prio < cc.PRIORITY_CREW, f"{k}: sound priority {prio} is below the crew chief's {cc.PRIORITY_CREW}")
        check(rank > max(cc.RANKS.values()), f"{k}: rank {rank} comes after every crew chief call")
    # the speaker picks the box call before an answer that was offered first
    sp = cc.CallSpeaker()
    ans = cc.Call(kind="eng_answer", rank=EN.KINDS["eng_answer"][0], say="x", text="x", t=0.0, priority=3, ttl=12.0, id=1)
    bx = cc.Call(kind="box_now", rank=3, say="Box this lap.", text="Box this lap.", t=0.1, priority=4, ttl=14.0, id=2)
    sp.offer([ans, bx])
    check(sp.pump({}, 1.0) is bx, "the speaker picks the box call ahead of the answer")
    # the real player: a crew call interrupts an engineer answer mid-sentence (the reviewer's a_player.py)
    be = audioout.FakeBackend(speed=1.0)
    p = audioout.Player(be)
    res = {}

    def mk(tag, secs, prio):
        pcm = b"\x00\x00" * int(16000 * secs)
        p.play(pcm, 16000, priority=prio, tag=tag, on_start=lambda it, t=tag: res.setdefault(t, "started"),
               on_end=lambda it, ok, t=tag: res.__setitem__(t + "_end", ok))
    mk("answer", 3.0, EN.KINDS["eng_answer"][1])
    time.sleep(0.4)
    mk("box", 0.4, cc.PRIORITY_CREW)
    time.sleep(1.2)
    p.close()
    check(res.get("box") == "started" and res.get("answer_end") is False, f"the box call interrupts the answer: {res}")
    # a crew call the player dropped is forgiven (made again) and the transcript says so
    r, own, clock = runner()
    c = box_call(own, clock[0])
    r.crew.limits["box_now"] = [clock[0], 0, 0, c.id]
    r._say_crew(c)
    cb = r.voice.drops.get("Box this lap.")
    check(callable(cb), "the voice is given a way to say the call was dropped")
    cb()
    check("box_now" not in r.crew.limits, "a dropped crew call does not use up its rate limit")


# ---------------------------------------------------------------- 2
def test_2_question_does_not_swallow():
    r, own, clock = runner()
    sp = r.crew_speaker
    box = box_call(own, clock[0] - 0.5)
    sp.pending = [box]
    sp._last_said = clock[0] - 3.0
    r.ask_engineer("what's the gap behind", "ptt")
    check(r.voice.texts() == [], "the question thread says nothing itself")
    check(box in sp.pending and box.spoken is None, "the waiting box call is still waiting")
    r._crew_deliver([], own, clock[0])
    check(r.voice.texts() == ["Box this lap."], f"the live loop says the box call first: {r.voice.texts()}")
    clock[0] += 3.0
    r._crew_deliver([], own, clock[0])
    check(r.voice.texts()[-1] == "Nobody behind you.", f"... then the answer: {r.voice.texts()}")


# ---------------------------------------------------------------- 3
def test_3_repeat():
    r, own, clock = runner()
    r.crew_speaker._last_said = -1e9
    r._say_crew(box_call(own, clock[0]))
    res = r.ask_engineer("say again", "ptt")
    check(res["text"] == "Nothing urgent pending." and res["say"] == "Nothing urgent pending.", f"a crew call is not replayed: {res['text']}")
    r2, own2, clock2 = runner()
    r2.ask_engineer("radio check", "ptt")
    r2._crew_deliver([], own2, clock2[0])
    res = r2.ask_engineer("repeat that", "ptt")
    check(res["text"] == "Loud and clear.", f"an answer is repeated: {res['text']}")


def test_3b_box_answer_obeys_the_box_rules():
    # 2.31 review round 2: "when do I box" answered "Box this lap." past pit entry, and "say again" replayed it a lap later
    from debrief import voice as vmod

    class Src:
        name = "x"

    def fresh(pct):
        r = lc.LiveRunner(Src(), vmod.FakeVoice(), mode="live")
        sim = ES.Sim("race")
        sim.on_track, sim.garage, sim.surf = True, False, 3
        own = sim.own()
        own.update(Lap=12, LapDistPct=pct, Brake=0.0)
        r._latest = own
        r.engineer.feed(own, None, sim.sections, 1, 1000.0, role="driver",
                        board={"box": {"need": True, "in_laps": 0, "text": "Box this lap"}})
        return r, own

    def pump(r, own, t):
        for i in range(60):
            r._crew_deliver([], own, t + i * 0.05)

    r, own = fresh(0.95)                      # past the default pit entry (85% of the lap)
    r.ask_engineer("when do i box", "ptt")
    pump(r, own, 1000.0)
    check("Box this lap." not in r.voice.texts(), f"no spoken 'Box this lap' past pit entry ({r.voice.texts()})")
    r, own = fresh(0.30)                      # before pit entry it is said
    r.ask_engineer("when do i box", "ptt")
    pump(r, own, 1000.0)
    check("Box this lap." in r.voice.texts(), f"'Box this lap' is said before pit entry ({r.voice.texts()})")
    own = dict(own, Lap=13, LapDistPct=0.2)
    own["SessionTime"] += 95
    r._latest = own
    res = r.ask_engineer("say again", "ptt")
    check(res["text"] == "Nothing urgent pending.", f"a box answer is never replayed a lap later: {res['text']}")


# ---------------------------------------------------------------- 4
def _feeder(e, sim, secs, sn):
    out = []

    def feed(s):
        s["SessionNum"], s["SessionUniqueID"] = sn[0], 9 + sn[0]
        e.feed(s, None, secs, 1, sim.t, role="driver")
        out.extend((e.sess["kind"], e.car.state, c.say) for c in e.take_calls())
    return feed, out


def test_4_practice_to_qualifying():
    sim = ES.Sim("practice", lap_s=90.0)
    secs = sim.sections
    secs["SessionInfo"]["Sessions"] = [{"SessionNum": 0, "SessionType": "Practice", "SessionName": "PRACTICE"},
                                       {"SessionNum": 1, "SessionType": "Open Qualify", "SessionName": "QUALIFY"}]
    e, sn = EN.Engineer(), [0]
    feed, out = _feeder(e, sim, secs, sn)
    feed(sim.own())
    for s in sim.leave_pits():
        feed(s)
    while sim.lap < 4:
        feed(sim.tick())
    sim.to_pits()
    for _ in range(40):
        feed(sim.tick())
    sim.in_garage()
    for _ in range(40):
        feed(sim.tick())
    sn[0] = 1
    sim.pct = 0.0
    for s in sim.leave_pits():
        feed(s)
    sim.lap_s = 120.0
    n = sim.lap + 1
    while sim.lap < n:
        feed(sim.tick())
    sim.lap_s = 90.0
    for _ in range(100):
        feed(sim.tick())
    check(e.car.state == "flying", f"5 s into the first qualifying push lap the car is flying, not in-lap ({e.car.state})")
    check(not any("Cool-down" in say for _k, _c, say in out), "no 'Cool-down lap' call at the start of the push lap")
    # a fresh session also starts with a fresh best lap
    check(e.best is None, "practice's best lap is not carried into qualifying")
    # the out lap on its own (second run, from the reviewer's d3_quali.py)
    car = SK.CarState()
    car.best_s = 90.0
    own = {"PlayerTrackSurface": 3, "Lap": 2}
    car.lap, car.out_lap = 2, True
    car.update(dict(own, Lap=3, LapLastLapTime=147.8), 0.0, "qualify", False)
    check(car.state == "flying" and not car.slow_prev, "a 148 s out lap does not make the next lap an in-lap")
    car.update(dict(own, Lap=4, LapLastLapTime=100.0), 0.0, "qualify", False)
    check(car.state == "in", "a slow push lap (111%) does: the lap after it is the way in")


# ---------------------------------------------------------------- 5
def test_5_last_lap():
    sim = ES.Sim("practice", lap_s=90.0)
    e = EN.Engineer()
    said = []
    e.feed(sim.own(), None, sim.sections, 1, sim.t)
    for s in sim.leave_pits():
        e.feed(s, None, sim.sections, 1, sim.t)
    sim.remain = 90 * 3 + 45.0           # the clock runs out 45 s into the 4th lap
    while sim.remain > 0:
        s = sim.tick()
        e.feed(s, None, sim.sections, 1, sim.t)
        said += [(round(sim.remain), sim.lap, round(sim.pct, 2)) for c in e.take_calls() if c.say == "Last lap of the session."]
    check(len(said) == 1 and said[0][1] == 3, f"'last lap' once, on the lap the clock runs out in (lap index 3): {said}")
    check(said and said[0][0] <= 90, f"... and not a lap early (said with {said[0][0] if said else None} s left; 90 s a lap)")
    # "how long left" under a minute: only 'last lap' when the clock really ends in this lap
    e2 = EN.Engineer(target_fn=lambda: 30.0)             # a 30 s lap
    s = ES.Sim("practice").own()
    s.update(SessionTimeRemain=45.0, LapDistPct=0.0)
    e2.feed(s, None, ES.sections("practice"), 1, 100.0)
    text, say = e2._a_time_left()
    check("45 seconds left" in text and say is None, f"45 s left on a 30 s lap: text only, no 'last lap' ({text!r}, {say!r})")
    e2.s = dict(s, SessionTimeRemain=20.0, LapDistPct=0.2)
    text, say = e2._a_time_left()
    check(say == "Last lap of the session.", f"20 s left, 24 s of the lap to go: last lap ({say!r})")


# ---------------------------------------------------------------- 6
def test_6_crew_calls_off():
    r, own, clock = runner(crew_calls=False)
    r.engineer.take_calls()
    c = cc.Call(kind="eng_note", rank=15, say="Copy.", text="Copy.", t=clock[0], priority=2, ttl=10.0, id=9001)
    c.data["row"] = r.engineer.add("engineer", "Copy.", "eng_note")["id"]
    r._eng_calls[c.id] = c
    r.crew_speaker.offer([c])
    braking = dict(own, Brake=0.9)
    r._latest = braking
    r._crew_step(braking)
    check(r.voice.texts() == [] and c in r.crew_speaker.pending, "while braking the message waits")
    clock[0] += 1.0
    free = dict(own, Brake=0.0, Throttle=1.0, SessionTime=own["SessionTime"] + 1)
    r._latest = free
    r._crew_step(free)
    check("Copy." in r.voice.texts(), f"with the crew chief's calls off it is still pumped and said ({r.voice.texts()})")


# ---------------------------------------------------------------- 7
def test_7_cold():
    e = EN.Engineer()
    s = ES.Sim("practice").own()
    s.update(AirTemp=-30.0, TrackTempCrew=-25.0)
    e.feed(s, None, ES.sections("practice"), 1, 100.0)
    for units in ("mph", "kph"):
        e.units = units
        text, say = e._a_weather()
        check("-" in text, f"{units}: a below-zero temperature is shown ({text})")
    e.units = "kph"
    check(say is None, f"... and left out of the speech ({say!r})")
    e.ask("weather")
    s.update(AirTemp=20.0, TrackTempCrew=-1.0)
    e.s = s
    text, say = e._a_weather()
    check(say == "Air temp sixty-eight." or say == "Air temp twenty.", f"only the usable one is spoken ({say!r})")
    # the briefing too
    e2 = EN.Engineer()
    s2 = ES.Sim("practice").own()
    s2["TrackTempCrew"] = -5.0
    e2.feed(s2, None, ES.sections("practice"), 1, 100.0)
    e2._brief(s2, 100.0, 100.0)
    check(True, "the briefing with a freezing track does not raise")


# ---------------------------------------------------------------- 8
def test_8_briefing():
    sim = ES.Sim("race")
    e = EN.Engineer(units="mph")
    s = sim.own()
    s.update(PlayerCarPosition=12, PlayerCarClassPosition=12, SessionState=2, Lap=0)
    e.feed(s, None, sim.sections, 1, 100.0, role="driver", board={"fuel": {"laps": 17.6}})
    e.take_calls()
    e._brief(s, 100.0, 100.0)
    b = [c for c in e.take_calls() if c.kind == "eng_brief"][0]
    check("Start position twelve" in b.say, f"on the grid: start position ({b.say})")
    check("Fuel for seventeen laps" in b.say, f"fuel laps from the board ({b.say})")
    s2 = dict(s, Lap=22, PlayerCarPosition=5, PlayerCarClassPosition=5, SessionState=4)
    e.s = s2
    e._brief(s2, 4000.0, 4000.0)
    b = [c for c in e.take_calls() if c.kind == "eng_brief"][0]
    check("Start position" not in b.say and "You are position five" in b.say, f"after a stop: where he is running ({b.say})")


# ---------------------------------------------------------------- 9
def test_9_target_label():
    r, own, clock = runner(kind="practice")
    c = r.crew
    c.target_given = None
    c.plan_lap_s = None
    c.lap_times.extend([92.0, 91.0, 93.0, 90.5])
    check(r._engineer_target() is None, "the median of recent laps is not offered as a target")
    c.target_given = 88.5
    check(r._engineer_target() == 88.5, "a target he saved is")
    e = EN.Engineer(target_fn=lambda: None)
    check(e._target() is None, "no target and no best lap: none")


# ---------------------------------------------------------------- 10
def test_10_intents_quiet_and_overflow():
    for t, want in (("laps left of fuel", "fuel"), ("how many laps left of fuel", "fuel"), ("where am I on fuel", "fuel"),
                    ("how many laps of fuel left", "fuel"), ("laps left", "time_left"), ("where am I", "position"),
                    ("how long left", "time_left")):
        got = intents.parse(t)["intent"]
        check(got == want, f"'{t}' -> {got}")
    e = EN.Engineer()
    s = ES.Sim("lone_qualify").own()
    e.feed(s, None, ES.sections("lone_qualify"), 1, 100.0)
    e.ask("quiet")
    e.s = dict(s)
    check(e.urgent_only, "quiet: urgent calls only")
    c = e._make("eng_traffic", "Traffic ahead in 3 s.", "Traffic ahead in three seconds.", 100.0)
    check(c is not None, "... qualifying traffic warnings still come through")
    check(e._make("eng_note", "Nice.", "Nice.", 100.0) is None, "... and notes do not")
    # crew chief safety calls are not touched by it: the crew speaker has no urgent_only
    check(not hasattr(cc.CallSpeaker(), "urgent_only"), "the crew chief's calls are not affected")
    check(SK._int(float("inf")) is None and SK._int(float("-inf")) is None and SK._int(float("nan")) is None,
          "sessionkind._int: infinity and NaN are not numbers")
    check(SK._int(1e308) == int(1e308), "... a huge number is a number")
    car = SK.CarState()
    car.update({"PlayerTrackSurface": 3, "Lap": float("inf"), "IsOnTrack": 1}, 0.0)
    check(True, "a car state reading with an infinite lap does not raise")


# ---------------------------------------------------------------- 11
class Resp:
    def __init__(self, data, length=None):
        self._d, self._p = data, 0
        self.headers = {"Content-Length": str(length if length is not None else len(data))}

    def read(self, n):
        c = self._d[self._p:self._p + n]
        self._p += n
        return c

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_11_model_caps():
    old = (stt.MAX_DOWNLOAD_BYTES, stt.MAX_UNZIPPED_BYTES)
    try:
        stt.MAX_DOWNLOAD_BYTES = 1000
        try:
            stt.download_model(TMP / "a", opener=lambda u, timeout=0: Resp(b"x" * 5000, 5000))
            check(False, "a download bigger than the cap must be refused")
        except stt.SttError as e:
            check("bigger than a speech model" in str(e), f"an oversize download is refused up front ({e})")
        try:
            stt.download_model(TMP / "b", opener=lambda u, timeout=0: Resp(b"x" * 5000, 0))
            check(False, "an unannounced oversize download must be stopped")
        except stt.SttError as e:
            check("stopped" in str(e), f"... and one that does not say its size is stopped as it grows ({e})")
        stt.MAX_DOWNLOAD_BYTES = old[0]
        stt.MAX_UNZIPPED_BYTES = 5000
        z = TMP / "bomb.zip"
        with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(f"{stt.MODEL_NAME}/am/final.mdl", b"\0" * 100000)
            zf.writestr(f"{stt.MODEL_NAME}/conf/model.conf", b"x")
        try:
            stt.download_model(TMP / "c", opener=lambda u, timeout=0: Resp(z.read_bytes()))
            check(False, "a zip that unpacks to far more must be refused")
        except stt.SttError as e:
            check("bigger than a speech model" in str(e), f"a zip bomb is refused before it is written ({e})")
        check(not (TMP / "c" / "models").exists() or not list((TMP / "c").rglob("final.mdl")), "... and nothing was unpacked")
    finally:
        stt.MAX_DOWNLOAD_BYTES, stt.MAX_UNZIPPED_BYTES = old
    # a good model: the fingerprint is kept and shown
    z2 = TMP / "ok.zip"
    with zipfile.ZipFile(z2, "w") as zf:
        zf.writestr(f"{stt.MODEL_NAME}/am/final.mdl", b"model")
        zf.writestr(f"{stt.MODEL_NAME}/conf/model.conf", b"x")
    res = stt.download_model(TMP / "d", opener=lambda u, timeout=0: Resp(z2.read_bytes()))
    check(len(res["sha256"]) == 64 and stt._saved_hash(TMP / "d") == res["sha256"], "the model fingerprint is saved")
    check(stt.MODEL_SHA256 is None, "MODEL_SHA256 is not pinned (the host could not be reached from here)")
    from debrief.desktop import talk
    talk._data_dir = lambda: TMP / "d"
    st = talk.model_state(type("S", (), {})()) if False else None
    js = (ROOT / "debrief" / "desktop" / "ui" / "app.js").read_text(encoding="utf-8")
    check("Model fingerprint" in js and "m.fingerprint" in js, "Settings shows the model fingerprint")


def main():
    tests = [(n, f) for n, f in list(globals().items()) if n.startswith("test_") and callable(f)]
    bad = 0
    for name, fn in tests:
        try:
            fn()
        except Exception:  # noqa: BLE001
            import traceback
            bad += 1
            print(f"FAIL {name} raised")
            traceback.print_exc()
    shutil.rmtree(TMP, ignore_errors=True)
    print("ALL OK" if not fails and not bad else f"{fails} checks failed, {bad} tests raised")
    sys.exit(1 if fails or bad else 0)


if __name__ == "__main__":
    main()
