"""Speaking short coaching cues out loud on Windows, offline, without PowerShell (2.13).

Windows has a speech engine built in (SAPI, the "Microsoft David / Zira" voices). It is reached over COM, either
through pywin32 (win32com) when it is installed, or through comtypes (pure Python, in requirements.txt for Windows).
If neither works (or this is not Windows) the voice is a quiet stand-in that only logs what it would have said, so
nothing else in Debrief ever depends on speech working.

All COM work happens on the voice's own background thread (COM objects belong to the thread that made them), and
speaking is asynchronous: say() only puts the words on a small queue and returns at once. A newer, more important cue
cuts off a less important one that is still being spoken (SAPI "purge before speak").

Nothing Windows-only is imported when this module is imported.

    v = make_voice(volume=90, rate=1)      # SapiVoice on Windows, NullVoice elsewhere
    v.say("Brake marker, T3", priority=3)
    v.close()
"""
from __future__ import annotations

import logging
import queue
import sys
import threading
import time

SVSF_ASYNC = 1               # SpeechVoiceSpeakFlags.SVSFlagsAsync
SVSF_PURGE = 2               # SpeechVoiceSpeakFlags.SVSFPurgeBeforeSpeak

_LOG = logging.getLogger("debrief.voice")


def _clamp(v, lo, hi, default):
    try:
        return max(lo, min(hi, int(round(float(v)))))
    except (TypeError, ValueError):
        return default


def speak_seconds(text: str, rate: int = 0) -> float:
    """Rough time SAPI takes to say this (about 14 characters a second at rate 0), used to decide whether a newer cue
    would talk over an older one."""
    return (0.4 + len(text or "") / 14.0) * (0.9 ** _clamp(rate, -10, 10, 0))


class NullVoice:
    """Says nothing; notes what it would have said in the log. Used when speech is not available."""
    backend = "none"
    available = False

    def __init__(self, reason: str = "speech is not available on this computer", log=None):
        self.reason = reason
        self._log = log or _LOG.info
        self.said = []

    def say(self, text: str, priority: int = 0, t: float | None = None) -> bool:
        self.said.append((t, text, priority))
        del self.said[:-50]
        try:
            self._log(f"voice (silent: {self.reason}): {text}")
        except Exception:  # noqa: BLE001
            pass
        return False

    def set(self, volume=None, rate=None):
        pass

    def status(self) -> dict:
        return {"backend": self.backend, "available": False, "reason": self.reason}

    def close(self):
        pass


class FakeVoice:
    """For tests: records (t, text, priority, purge) for every cue. t is the time the caller passes (session time
    in the coach's tests) or, without one, the clock's time."""
    backend = "fake"
    available = True

    def __init__(self, clock=time.monotonic, rate: int = 0):
        self.clock = clock
        self.rate = rate
        self.volume = 100
        self.said: list[dict] = []
        self.closed = False
        self._busy_until = -1e9
        self._last_pri = -1

    def say(self, text: str, priority: int = 0, t: float | None = None) -> bool:
        now = self.clock() if t is None else float(t)
        purge = priority > self._last_pri and now < self._busy_until
        self.said.append({"t": now, "text": text, "priority": priority, "purge": purge})
        self._busy_until = now + speak_seconds(text, self.rate)
        self._last_pri = priority
        return True

    def texts(self) -> list[str]:
        return [x["text"] for x in self.said]

    def set(self, volume=None, rate=None):
        if volume is not None:
            self.volume = _clamp(volume, 0, 100, 100)
        if rate is not None:
            self.rate = _clamp(rate, -10, 10, 0)

    def status(self) -> dict:
        return {"backend": "fake", "available": True, "reason": ""}

    def close(self):
        self.closed = True


def _create_sapi():
    """(SpVoice object, backend name, uninitialise function) on THIS thread. Raises when no COM route works."""
    errors = []
    try:
        import pythoncom  # pywin32
        import win32com.client
        pythoncom.CoInitialize()
        try:
            return win32com.client.Dispatch("SAPI.SpVoice"), "pywin32", pythoncom.CoUninitialize
        except Exception:
            pythoncom.CoUninitialize()
            raise
    except Exception as e:  # noqa: BLE001
        errors.append(f"pywin32: {e.__class__.__name__}")
    try:
        import comtypes
        import comtypes.client
        comtypes.CoInitialize()
        try:
            # dynamic=True talks to SpVoice through IDispatch, so comtypes never has to generate wrapper modules
            # into comtypes.gen (which a frozen app cannot write to)
            return comtypes.client.CreateObject("SAPI.SpVoice", dynamic=True), "comtypes", comtypes.CoUninitialize
        except Exception:
            comtypes.CoUninitialize()
            raise
    except Exception as e:  # noqa: BLE001
        errors.append(f"comtypes: {e.__class__.__name__}: {str(e)[:80]}")
    raise RuntimeError("; ".join(errors))


class SapiVoice:
    """Windows speech (SAPI) on its own thread. Never raises; if SAPI can't be started it turns into a silent voice
    and status() says why."""

    def __init__(self, volume: int = 100, rate: int = 0, log=None):
        self.volume = _clamp(volume, 0, 100, 100)
        self.rate = _clamp(rate, -10, 10, 0)
        self._log = log or _LOG.warning
        self._q: queue.Queue = queue.Queue(maxsize=8)
        self._stop = threading.Event()
        self._ready = threading.Event()
        self.backend = "starting"
        self.available = False
        self.reason = ""
        self._busy_until = -1e9
        self._last_pri = -1
        self._thread = threading.Thread(target=self._run, name="debrief-voice", daemon=True)
        self._thread.start()

    # ---- caller side (any thread) ----
    def say(self, text: str, priority: int = 0, t: float | None = None) -> bool:
        if self._stop.is_set() or not text:
            return False
        now = time.monotonic()
        flags = SVSF_ASYNC
        if priority > self._last_pri and now < self._busy_until:
            flags |= SVSF_PURGE                    # a more important cue cuts off the one still being spoken
        self._busy_until = now + speak_seconds(text, self.rate)
        self._last_pri = priority
        self._put(("say", text, flags))
        return True

    def set(self, volume=None, rate=None):
        if volume is not None:
            self.volume = _clamp(volume, 0, 100, self.volume)
        if rate is not None:
            self.rate = _clamp(rate, -10, 10, self.rate)
        self._put(("set", None, 0))

    def _put(self, item):
        try:
            self._q.put_nowait(item)
        except queue.Full:                         # never queue up old words: drop the oldest
            try:
                self._q.get_nowait()
                self._q.put_nowait(item)
            except (queue.Empty, queue.Full):
                pass

    def status(self) -> dict:
        return {"backend": self.backend, "available": self.available, "reason": self.reason}

    def wait_ready(self, timeout: float = 5.0) -> bool:
        self._ready.wait(timeout)
        return self.available

    def close(self, timeout: float = 2.0):
        self._stop.set()
        self._put(("stop", None, 0))
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout)

    # ---- the voice's own thread ----
    def _apply(self, sp):
        try:
            sp.Volume = self.volume
            sp.Rate = self.rate
        except Exception as e:  # noqa: BLE001
            self._log(f"voice: could not set volume/rate ({e.__class__.__name__})")

    def _run(self):
        sp, uninit = None, None
        try:
            sp, self.backend, uninit = _create_sapi()
            self.available = True
            self._apply(sp)
        except Exception as e:  # noqa: BLE001
            self.backend, self.available = "none", False
            self.reason = f"Windows speech could not be started ({str(e)[:160]})"
            self._log(f"voice: {self.reason}")
        finally:
            self._ready.set()
        try:
            while not self._stop.is_set():
                try:
                    kind, text, flags = self._q.get(timeout=0.5)
                except queue.Empty:
                    continue
                if kind == "stop":
                    break
                if sp is None:
                    if kind == "say":
                        _LOG.info(f"voice (silent): {text}")
                    continue
                try:
                    if kind == "set":
                        self._apply(sp)
                    elif kind == "say":
                        sp.Speak(text, flags)
                except Exception as e:  # noqa: BLE001
                    self._log(f"voice: speaking failed ({e.__class__.__name__}: {str(e)[:120]})")
        finally:
            if sp is not None:
                try:
                    sp.Speak("", SVSF_ASYNC | SVSF_PURGE)    # stop anything still being said
                except Exception:  # noqa: BLE001
                    pass
            sp = None
            if uninit is not None:
                try:
                    uninit()
                except Exception:  # noqa: BLE001
                    pass


def make_voice(volume: int = 100, rate: int = 0, log=None):
    """The best voice this computer has: SapiVoice on Windows (which goes quiet on its own if SAPI fails),
    NullVoice anywhere else."""
    if sys.platform != "win32":
        return NullVoice("spoken cues need Windows", log=log)
    try:
        return SapiVoice(volume=volume, rate=rate, log=log)
    except Exception as e:  # noqa: BLE001  (e.g. no threads left)
        return NullVoice(f"speech could not start ({e.__class__.__name__})", log=log)
