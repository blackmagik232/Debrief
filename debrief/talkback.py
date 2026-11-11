"""Talk to your engineer (2.31): hold the button, ask, let go. The microphone is only open and the recogniser only runs while
the button is held; on release the words go through intents.parse to the engineer, which answers on the radio and in the
transcript. Everything else (button, microphone, recogniser) is injected, so the tests use fakes.

    tb = TalkBack(ask=runner.ask_engineer, recognizer=Recognizer(...), mic=Mic(...), note=engineer.add_note, beep=voice.play_tone)
    tb.press()      # button down: the microphone opens
    tb.release()    # button up: the recogniser answers, the engineer replies (on its own thread)
"""
from __future__ import annotations

import threading
import time

from . import stt

MIN_HOLD_S = 0.25            # a tap shorter than this is not a question


class TalkBack:
    def __init__(self, ask, recognizer, mic, note=None, beep=None, clock=time.monotonic, log=None, enabled=True):
        self.ask, self.rec, self.mic = ask, recognizer, mic
        self.note = note or (lambda text: None)
        self.beep = beep or (lambda name: None)
        self._clock = clock
        self._log = log or (lambda m: None)
        self.enabled = enabled
        self._lock = threading.Lock()
        self._held = False
        self._t0 = 0.0
        self._chunks = 0
        self._err = ""
        self.state = "idle"             # idle | listening | thinking | error
        self.last_text = ""
        self.last_error = ""
        self.heard = 0
        self._worker = None
        self._q: list = []
        self._ev = threading.Event()

    def status(self) -> dict:
        txt = {"idle": "Hold the button and ask.", "listening": "Listening...", "thinking": "Working it out...",
               "error": self.last_error}.get(self.state, "")
        return {"state": self.state, "text": txt, "last": self.last_text, "enabled": self.enabled,
                "error": self.last_error}

    def press(self):
        with self._lock:
            if self._held or not self.enabled:
                return
            self._held, self._t0, self._chunks = True, self._clock(), 0
            self._q = []
        try:
            self.rec.begin()
            self.mic.start(self._on_chunk)
        except stt.SttError as e:
            self._fail(str(e))
            return
        except Exception as e:  # noqa: BLE001
            self._fail("Talk-back could not start (" + e.__class__.__name__ + ").")
            return
        self.state = "listening"
        try:
            self.beep("start")
        except Exception:  # noqa: BLE001
            pass

    def _on_chunk(self, data: bytes):
        # the microphone's own thread: the recogniser eats the chunk here, only while the button is held
        if not self._held:
            return
        if self._clock() - self._t0 > stt.MAX_HOLD_S:
            return
        self._chunks += 1
        try:
            self.rec.feed(data)
        except Exception:  # noqa: BLE001
            pass

    def release(self):
        with self._lock:
            if not self._held:
                return
            self._held = False
            held = self._clock() - self._t0
        self.mic.stop()
        if self.state == "error":
            return
        if held < MIN_HOLD_S or self._chunks == 0:
            try:
                self.rec.finish()
            except Exception:  # noqa: BLE001
                pass
            self.state = "idle"
            return
        self.state = "thinking"
        threading.Thread(target=self._finish, name="debrief-talkback", daemon=True).start()

    def _finish(self):
        try:
            text = self.rec.finish()
        except Exception as e:  # noqa: BLE001
            self._fail("Talk-back could not understand (" + e.__class__.__name__ + ").")
            return
        self.last_text = text
        self.heard += 1
        try:
            self.ask(text if text else "", "voice")
        except Exception as e:  # noqa: BLE001
            self._log("talkback: the engineer step failed (" + e.__class__.__name__ + ")")
        if self.state != "error":
            self.state = "idle"

    def _fail(self, msg):
        self.last_error = msg
        self.state = "error"
        self._held = False
        try:
            self.mic.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.note(msg)
        except Exception:  # noqa: BLE001
            pass
        self._log("talkback: " + msg)
