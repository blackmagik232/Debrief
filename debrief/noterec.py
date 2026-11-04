"""Recording a corner note while driving (2.20), and the Mic test: the glue between the microphone (audioin.py),
the tones and the voice's player (coachvoice.PackVoice.play_tone / play_clip), the notes folder (cornernotes.py) and
the coach (Coach.set_recording).

    nr = NoteRecorder(store, backend_fn, context_fn, voice_fn, flag_fn)
    nr.press()          # the button or Ctrl+Shift+V goes down: rising tone, the microphone opens, the coach stops talking
    nr.release()        # it goes up: falling tone and the note is saved; or one low tone and it is thrown away
    nr.status()         # {"recording", "level", "text", "last": {...}}  (the Coach page's Recording light)

    mt = MicTest(backend_fn, voice_fn)
    mt.start()          # records 3 s with a live level, then plays it back; mt.status() says where it is

Rules (from the 2.20 spec): 16 kHz mono from the default Windows input, only while held, at most 8 s; a take under
0.4 s or without speech is discarded with a low tone; the coach starts no cue while recording; a wheel unplugged or a
key released always ends the recording (the microphone never stays open); nothing is written outside the notes folder.
Never raises: a failure is a plain-words note in status()["text"].
"""
from __future__ import annotations

import logging
import threading
import time

import numpy as np

from . import audioin, cornernotes

_LOG = logging.getLogger("debrief.noterec")

TEST_S = 3.0
PLAYBACK_PAD_S = 0.4


def _call(obj, name, *a, **k):
    fn = getattr(obj, name, None) if obj is not None else None
    if fn is None:
        return None
    try:
        return fn(*a, **k)
    except Exception:  # noqa: BLE001
        return None


class NoteRecorder:
    """Push-to-talk. press() and release() may come from any thread (a hotkey's, the wheel's, a request's)."""

    def __init__(self, store, backend_fn, context_fn, voice_fn, flag_fn=None, log=None, clock=time.time,
                 threaded: bool = True):
        self.store = store
        self._backend_fn = backend_fn          # () -> an audioin backend (WaveInBackend, FakeInput) or None
        self._context_fn = context_fn          # () -> where the car is (track, car, lap, pct, corners ...) or None
        self._voice_fn = voice_fn              # () -> the voice (play_tone, cancel) or None
        self._flag_fn = flag_fn                # fn(bool): the coach's "recording" flag
        self._log = log or _LOG.warning
        self._clock = clock
        self._threaded = threaded
        self._lock = threading.RLock()
        self._rec: audioin.Recorder | None = None
        self._ctx: dict | None = None
        self.last: dict | None = None          # {"ok", "text", "wall", "note"}
        self.saved = 0
        self.discarded = 0

    # ---- the button ----
    @property
    def recording(self) -> bool:
        r = self._rec
        return bool(r is not None and r.active)

    def level(self) -> float:
        r = self._rec
        return float(r.level) if r is not None and r.active else 0.0

    def press(self) -> bool:
        with self._lock:
            if self.recording:
                return False
            ctx = _safe(self._context_fn)
            if not ctx:
                self._done(False, "Corner notes need the coach running with iRacing on a track.", tone="discard")
                return False
            be = _safe(self._backend_fn)
            self._flag(True)
            _call(self._voice_fn_(), "cancel")           # what is being said stops: the microphone must not hear it
            rec = audioin.Recorder(be, skip_s=cornernotes.TONE_S, threaded=self._threaded, on_limit=self._limit,
                                   log=self._log)
            ok = rec.start()
            if not ok:
                self._flag(False)
                self._done(False, rec.error or "The microphone could not be opened.", tone="discard")
                return False
            _call(self._voice_fn_(), "play_tone", "start")
            self._rec, self._ctx = rec, ctx
            return True

    def release(self) -> dict | None:
        """The button went up (or the 8 s ran out, or the wheel went away): stop, clean, tag and save. Returns what
        last says."""
        with self._lock:
            rec, ctx = self._rec, self._ctx
            if rec is None:
                return self.last
            self._rec, self._ctx = None, None
            take = rec.stop()
            self._flag(False)
            if take is None:
                self._done(False, "Note not kept: nothing was recorded.", tone="discard")
                return self.last
            c = cornernotes.clean(take.pcm, take.rate)
            if not c.ok:
                self._done(False, "Note not kept: " + {"too short": "it was under 0.4 seconds",
                                                      "no speech": "no speech was heard"}.get(c.why, c.why) + ".",
                           tone="discard")
                return self.last
            t = cornernotes.tag(ctx.get("corners"), ctx.get("length_m"), ctx.get("pct"))
            try:
                n = self.store.add(ctx, c.pcm, c.rate, turn=t["turn"] if t else None,
                                   where=t["where"] if t else "", apex_pct=t["apex_pct"] if t else None)
            except Exception as e:  # noqa: BLE001
                self._log(f"noterec: saving a note failed ({e.__class__.__name__}: {str(e)[:100]})")
                self._done(False, "The note couldn't be saved. Details were saved to debrief.log.", tone="discard")
                return self.last
            self.saved += 1
            where = (f"Turn {n['turn']}" if n.get("turn") is not None else "a corner not known yet")
            self._done(True, f"Note saved for {where} ({n['seconds']:.1f} s).", tone="saved", note=n["id"])
            return self.last

    def _limit(self):
        """The recorder hit 8 s: the note ends there, whatever the button does."""
        self.release()

    def cancel(self):
        with self._lock:
            rec, self._rec, self._ctx = self._rec, None, None
            if rec is not None:
                rec.cancel()
            self._flag(False)

    # ---- helpers ----
    def _voice_fn_(self):
        return _safe(self._voice_fn)

    def _flag(self, on: bool):
        if self._flag_fn is not None:
            _safe(self._flag_fn, on)

    def _done(self, ok: bool, text: str, tone: str, note=None):
        if not ok:
            self.discarded += 1
        self.last = {"ok": ok, "text": text, "wall": round(self._clock(), 1), "note": note}
        _call(self._voice_fn_(), "play_tone", tone)

    def status(self) -> dict:
        return {"recording": self.recording, "level": round(self.level(), 3), "last": dict(self.last) if self.last else None,
                "saved": self.saved}


def _safe(fn, *a):
    try:
        return fn(*a) if fn is not None else None
    except Exception:  # noqa: BLE001
        return None


class MicTest:
    """The Coach page's Mic test: record TEST_S seconds with a live level meter, then play it back. One at a time.
    state: idle | recording | playing | done | error."""

    def __init__(self, backend_fn, voice_fn, log=None, clock=time.monotonic, threaded: bool = True, seconds: float = TEST_S):
        self._backend_fn = backend_fn
        self._voice_fn = voice_fn
        self._log = log or _LOG.warning
        self._clock = clock
        self._threaded = threaded
        self.seconds = float(seconds)
        self._lock = threading.RLock()
        self._rec: audioin.Recorder | None = None
        self.state = "idle"
        self.text = ""
        self.peak = 0.0
        self.heard = None              # was speech heard (an energy check)
        self._until = 0.0
        self._thread = None

    @property
    def recording(self) -> bool:
        r = self._rec
        return bool(r is not None and r.active)

    def start(self) -> bool:
        with self._lock:
            self.status()                       # (a playback that has ended counts as done)
            if self.state in ("recording", "playing"):
                return False
            rec = audioin.Recorder(_safe(self._backend_fn), max_s=self.seconds, threaded=self._threaded,
                                   on_limit=self._finish, log=self._log)
            self.peak, self.heard = 0.0, None
            if not rec.start():
                self.state, self.text = "error", rec.error or "The microphone could not be opened."
                return False
            self._rec = rec
            self.state, self.text = "recording", f"Say something. Recording {self.seconds:.0f} seconds."
            self._until = self._clock() + self.seconds
            return True

    def pump(self):
        """For tests without a thread: read the microphone and finish when the time is up."""
        r = self._rec
        if r is not None:
            r.pump()

    def level(self) -> float:
        r = self._rec
        return float(r.level) if r is not None and r.active else 0.0

    def _finish(self):
        with self._lock:
            rec, self._rec = self._rec, None
            if rec is None:
                return
            take = rec.stop()
            if take is None:
                self.state, self.text = "error", "Nothing was recorded. Check that Windows has a microphone set as the default input."
                return
            x = cornernotes.to_float(take.pcm)
            self.peak = float(np.abs(x).max()) if x.size else 0.0
            self.heard = cornernotes.has_speech(x, take.rate)
            if self.peak < 0.01:
                self.state, self.text = "done", "Nothing was heard. Check that the right microphone is the Windows default and not muted."
                return
            v = _safe(self._voice_fn)
            fn = getattr(v, "play_clip", None)
            if fn is None:
                self.state, self.text = "done", "Recorded. This voice can't play it back here."
                return
            try:
                fn("(mic test)", take.pcm, take.rate, priority=9)
            except Exception:  # noqa: BLE001
                self.state, self.text = "error", "The recording couldn't be played back."
                return
            self.state, self.text = "playing", "Playing it back."
            self._until = self._clock() + take.seconds + PLAYBACK_PAD_S

    def status(self) -> dict:
        with self._lock:
            if self.state == "playing" and self._clock() >= self._until:
                self.state = "done"
                self.text = ("Heard you. Played back." if self.heard else
                             "Played back, but no speech was heard. Speak closer to the microphone.")
            if self.state == "recording" and self._rec is None:
                self.state = "done"
            return {"state": self.state, "text": self.text, "level": round(self.level(), 3),
                    "peak": round(self.peak, 3), "recording": self.recording, "heard": self.heard}

    def cancel(self):
        with self._lock:
            r, self._rec = self._rec, None
            if r is not None:
                r.cancel()
            if self.state in ("recording", "playing"):
                self.state, self.text = "idle", ""
