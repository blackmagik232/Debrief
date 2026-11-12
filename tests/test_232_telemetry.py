"""Debrief 2.32 "Telemetry on": Settings > Live data > Make iRacing save my telemetry.

    python3 tests/ci_run.py tests/test_232_telemetry.py

With the switch on, the recorder asks iRacing to start saving its telemetry file (.ibt) when you are in the car and it
isn't saving, at most every 10 s and 3 times per iRacing connection; it never asks with the switch off, never when
iRacing already saves, never off track, and it works with recording switched off. Settings round-trips the switch
(off by default), the setup checklist offers "Turn it on for me", and the real LiveReader sends pyirsdk's start command.
No network, temp folders from tempfile, utf-8 everywhere.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
TMP = Path(tempfile.mkdtemp(prefix="debrief-232-"))
ST = TMP / "settings.toml"
ST.write_text(f"library_folder = '{(TMP / 'lib').as_posix()}'\n", encoding="utf-8")
os.environ.update(DEBRIEF_SETTINGS=str(ST), DEBRIEF_CACHE_DIR=str(TMP / "cache"))
(TMP / "lib").mkdir()

import fake_live as F  # noqa: E402
from debrief import app, firstrun, livedata, liverec  # noqa: E402
from debrief.desktop import settings_io  # noqa: E402

FAILS = []


def check(ok, what):
    print(("ok   " if ok else "FAIL ") + what)
    if not ok:
        FAILS.append(what)


class AskingReader(F.FakeReader):
    """A FakeReader that also takes telemetry_start(): the sim starts saving after `works_after` requests (never if None)."""

    def __init__(self, sim, works_after=1):
        super().__init__(sim)
        self.asks, self.works_after = 0, works_after

    def telemetry_start(self):
        self.asks += 1
        if self.works_after is not None and self.asks >= self.works_after:
            self.sim.disk = True
        return True


def make(on=True, record=True, works_after=1, disk=False):
    sim = F.Sim()
    sim.disk = disk
    rd = AskingReader(sim, works_after)
    lib = Path(tempfile.mkdtemp(dir=TMP))
    prefs = {"record_sessions": record, "start_with_windows": False, "ensure_telemetry": on, "library_folder": str(lib)}
    said = []
    rec = liverec.Recorder(rd, library_folder=lambda: str(lib), use_lock=False, clock=lambda: sim.now, wall=lambda: sim.wall,
                           prefs_fn=lambda: dict(prefs), utc=sim.utc, log=said.append)
    return sim, rd, rec, said, prefs


def run(sim, rec, seconds, dt=0.1):
    end = sim.t + seconds
    while sim.t < end - 1e-9:
        sim.step(dt)
        rec.tick()


def test_recorder():
    sim, rd, rec, said, _ = make(on=False)
    run(sim, rec, 30)
    check(rd.asks == 0 and rec.tel_state is None, "switch off: iRacing is never asked")

    sim, rd, rec, said, _ = make(on=True, disk=True)
    run(sim, rec, 30)
    check(rd.asks == 0 and rec.tel_state == "saving", "already saving: never asked, state 'saving'")

    sim, rd, rec, said, _ = make(on=True, works_after=1)
    run(sim, rec, 30)
    check(rd.asks == 1, f"not saving: asked once, then iRacing saves ({rd.asks} asks)")
    check(rec.tel_state == "saving" and rec.status_doc().get("telemetry") == "saving", "the status says saving after")
    check(any("asked it to start" in m for m in said) and any("saving telemetry now" in m for m in said), "both in the log")

    sim, rd, rec, said, _ = make(on=True, works_after=None)
    run(sim, rec, 60)
    check(rd.asks == liverec.TEL_ASKS, f"iRacing never starts: asked {liverec.TEL_ASKS} times, no more ({rd.asks})")
    check(rec.tel_state == "failed" and any("Alt+L" in m for m in said), "then says it didn't work and to press Alt+L")
    sim, rd2, rec2, _, _ = make(on=True, works_after=None)
    run(sim, rec2, 15)
    check(rd2.asks == 2, f"at most once every {liverec.TEL_ASK_S:.0f} s ({rd2.asks} asks in 15 s)")

    sim, rd, rec, said, _ = make(on=True, works_after=None)
    sim.own_orig = sim.own
    sim.own = lambda: {**sim.own_orig(), "IsOnTrack": False}
    run(sim, rec, 30)
    check(rd.asks == 0 and rec.tel_state == "off_track", "not in the car: never asked, waits")

    sim, rd, rec, said, _ = make(on=True, works_after=None)
    sim.disk_known = False
    run(sim, rec, 30)
    check(rd.asks == 0, "an iRacing that doesn't say whether it saves: never asked")

    sim, rd, rec, said, _ = make(on=True, record=False, works_after=1)
    run(sim, rec, 20)
    check(rd.asks == 1 and rec.tel_state == "saving" and rec.cur is None,
          "recording switched off: still makes iRacing save telemetry, and records nothing itself")

    sim, rd, rec, said, _ = make(on=True, works_after=None)
    run(sim, rec, 60)
    sim.connected = False
    run(sim, rec, 10)
    sim.connected = True
    run(sim, rec, 5)
    check(rd.asks == liverec.TEL_ASKS + 1, f"a new iRacing connection gets fresh tries ({rd.asks})")


def test_reader():
    class IR:
        sent = []

        def telem_command(self, mode):
            IR.sent.append(mode)
    r = livedata.LiveReader()
    check(r.telemetry_start() is False, "the real reader: not connected, nothing sent")
    r._ir, r.state = IR(), "connected"
    try:
        import irsdk
        check(r.telemetry_start() is True and IR.sent == [irsdk.TelemCommandMode.start], "connected: sends pyirsdk's start")
    except ImportError:
        print("  SKIPPED: pyirsdk not installed")

    class Broken:
        def telem_command(self, mode):
            raise OSError("iRacing went away")
    r._ir = Broken()
    check(r.telemetry_start() is False, "a failed send is False, never an error")


def test_settings_and_checklist():
    check(app.load_settings().get("ensure_telemetry") is False, "off by default")
    check(settings_io.public_view().get("ensure_telemetry") is False, "Settings shows it off by default")
    check("ensure_telemetry" in settings_io.live_view(), "the Live data panel gets it")
    try:
        settings_io.validate({"ensure_telemetry": "yes"}) if hasattr(settings_io, "validate") else None
    except ValueError:
        pass
    check(liverec.prefs().get("ensure_telemetry") is False, "the recorder reads it, off by default")
    ST.write_text(ST.read_text(encoding="utf-8") + "ensure_telemetry = true\n", encoding="utf-8")
    check(app.load_settings().get("ensure_telemetry") is True and liverec.prefs()["ensure_telemetry"] is True,
          "switched on in settings.toml: the recorder sees it")
    st = {s["id"]: s for s in firstrun.steps({"folder": True})}
    check(st["record"]["action"] == "telemetry_auto", "checklist offers Turn it on for me")
    st = {s["id"]: s for s in firstrun.steps({"folder": True, "ensure_telemetry": True})}
    check(st["record"]["action"] is None and "each time you get in the car" in st["record"]["text"],
          "with it on: nothing to click, just drive")
    check(firstrun.gather(folder=str(TMP))["ensure_telemetry"] is True, "the checklist reads the setting")
    js = (ROOT / "debrief" / "desktop" / "ui" / "app.js").read_text(encoding="utf-8")
    html = (ROOT / "debrief" / "desktop" / "ui" / "index.html").read_text(encoding="utf-8")
    check('id="recTel"' in html and "ensure_telemetry:$('recTel').checked" in js and "telemetry_auto:[" in js,
          "the window has the switch, saves it, and the checklist button")


if __name__ == "__main__":
    test_recorder()
    test_reader()
    test_settings_and_checklist()
    print("all passed" if not FAILS else f"{len(FAILS)} failed")
    sys.exit(1 if FAILS else 0)
