"""Debrief 2.31 glue: the live runner's engineer (voice gets the radio click flag, the transcript fills, a crew PC is text only),
and the server routes api/raceroom action "ask" and api/coach/talk*. No iRacing, no network, no microphone.

    python3 tests/ci_run.py tests/test_231_server.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.modules.setdefault("irsdk", None)
TMP = Path(tempfile.mkdtemp(prefix="debrief-231-"))
os.environ["DEBRIEF_SETTINGS"] = str(TMP / "settings.toml")
os.environ["DEBRIEF_CACHE_DIR"] = str(TMP / "cache")
os.environ["DEBRIEF_SCHEDULE_DIR"] = str(TMP / "schedule")
os.environ["DEBRIEF_UPDATE_STATE"] = str(TMP / "update.json")

import eng_sim as ES  # noqa: E402
from debrief import livecoach as lc, voice as vc  # noqa: E402

fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


class RadioVoice(vc.FakeVoice):
    """A voice that, like the voice pack, accepts the radio flag."""

    def say(self, text, priority=0, t=None, interrupt=False, check=None, radio=False):
        self.radio = getattr(self, "radio", []) + [(text, radio)]
        return super().say(text, priority, t)


class Src:
    name = "stand-in"


def runner(voice, role="driver"):
    r = lc.LiveRunner(Src(), voice, mode="live")
    sim = ES.Sim("practice")
    r.engineer.feed(sim.own(), None, sim.sections, 1, 0.0, role=role)
    r._latest = sim.own()
    return r


def pump(r, now=1.0):
    """The live loop's next reading: the one place the queue is pumped (a question only offers its answer)."""
    r._crew_deliver([], r._latest, now)


def test_runner_speaks_with_radio_flag_and_logs_transcript():
    v = RadioVoice()
    r = runner(v)
    res = r.ask_engineer("radio check", "typed")
    check(not getattr(v, "radio", None), "runner: the question thread itself says nothing")
    pump(r)
    check(res["ok"] and res["intent"] == "radio_check", "runner: a typed radio check is understood")
    check(v.radio and v.radio[-1] == ("Loud and clear.", True), f"runner: the voice gets the answer with the radio click flag ({getattr(v, 'radio', None)})")
    tr = r.room_state()["engineer"]["transcript"]
    check(len(tr) == 2 and tr[0]["who"] == "engineer" and tr[1]["who"] == "you", "runner: the transcript has the question and the answer, newest first")
    res = r.ask_engineer("blah blah", "typed")
    check(res["text"] == "Say again?" and res["intent"] == "unknown", "runner: unknown words get 'Say again?'")


def test_runner_with_a_plain_voice_still_works():
    v = vc.FakeVoice()
    r = runner(v)
    r.ask_engineer("radio check", "typed")
    pump(r)
    check("Loud and clear." in v.texts(), "runner: a voice with no radio option still says the answer")


def test_crew_pc_answers_in_text_only():
    v = RadioVoice()
    r = runner(v, role="crew")
    res = r.ask_engineer("how's the fuel", "typed")
    pump(r)
    check(res["ok"] and not getattr(v, "radio", []), "runner: a crew PC speaks nothing")
    check(r.room_state()["engineer"]["transcript"][0]["who"] == "engineer", "runner: the crew PC's answer is in the transcript")


def test_ask_without_engineer():
    r = lc.LiveRunner(Src(), vc.FakeVoice(), mode="live")
    r.engineer = None
    check(r.ask_engineer("fuel")["ok"] is False, "runner: asking with no engineer says so")


def test_server_routes():
    from debrief import app
    from debrief.desktop import server as SV
    lib = TMP / "lib"
    lib.mkdir()
    (TMP / "settings.toml").write_text('token = "g61-test-token-ABCDEFGHIJKLMNOP"\n' f"library_folder = '{lib}'\n"
                                       'api_base = "http://127.0.0.1:9/api/v1/"\n', encoding="utf-8")
    app.LOG_FILE = str(TMP / "debrief.log")
    s = SV.DebriefServer(heartbeat_timeout=3600)
    threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(rel, body=None):
        h = {"X-Debrief": "1"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            h["Content-Type"] = "application/json"
        rq = urllib.request.Request(s.url + rel, headers=h, data=data, method="POST" if data is not None else "GET")
        try:
            with op.open(rq, timeout=30) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
    try:
        code, body = call("api/raceroom", {"action": "ask", "text": "fuel"})
        j = json.loads(body)
        check(code != 200 or not j.get("ok"), f"server: asking before the coach runs is refused in words ({code})")
        code, body = call("api/raceroom", {"action": "ask", "text": ""})
        check(code != 200 or not json.loads(body).get("ok"), "server: an empty question is refused")
        code, body = call("api/raceroom", {"action": "ask", "text": "x" * 500})
        check(code != 200 or not json.loads(body).get("ok"), "server: a very long question is refused")
        code, body = call("api/coach/talk")
        j = json.loads(body)
        check(code == 200 and j.get("ok") is not False, f"server: api/coach/talk answers ({code})")
        code, body = call("api/coach/talk-clear", {})
        check(code == 200, f"server: talk-clear answers ({code})")
        code, body = call("static/raceroom.js")
        check(code == 200 and b"enRender" in body, "server: raceroom.js carries the engineer page")
    finally:
        try:
            s.shutdown()
        except Exception:  # noqa: BLE001
            pass


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
