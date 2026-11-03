"""Speaking short coaching cues out loud on Windows, offline, without PowerShell (2.13).

Windows has a speech engine built in (SAPI, the "Microsoft David / Zira" voices), reached over COM: through pywin32
(win32com, late-bound so no generated wrapper files are needed, which suits the frozen app) or, when pywin32 is
missing, comtypes (also late-bound: dynamic=True). If neither works, or this is not Windows, the voice is a quiet
stand-in that only logs what it would have said, so nothing else in Debrief depends on speech working.

No speech synthesis while iRacing is running: at the start of a session the coach hands over its fixed phrases
(prepare), which are rendered once to small WAV files in a temporary folder on a background thread; a cue is then
played with winsound (SND_FILENAME | SND_ASYNC: Python's winsound refuses SND_MEMORY together with SND_ASYNC, so
the rendered audio sits in temp files rather than memory). A phrase that was not rendered (e.g. "Down 1.4 seconds")
falls back to SAPI's own asynchronous Speak. A newer, more important cue cuts off one still being spoken; cancel()
stops everything at once (a car alongside). The WAV lengths are known, so the coach can time cues exactly
(duration(text)). An optional short tone (beep) is a pre-made WAV too.

All COM work happens on the voice's own threads (COM objects belong to the thread that made them). Nothing
Windows-only is imported when this module is imported.

    v = make_voice(volume=90, rate=1)      # SapiVoice on Windows, NullVoice elsewhere
    v.prepare(["Brake marker, T3", ...])
    v.say("Brake marker, T3", priority=3)
    v.close()
"""
from __future__ import annotations

import logging
import math
import queue
import shutil
import struct
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

SVSF_ASYNC = 1               # SpeechVoiceSpeakFlags.SVSFlagsAsync
SVSF_PURGE = 2               # SpeechVoiceSpeakFlags.SVSFPurgeBeforeSpeak
SSFM_CREATE_FOR_WRITE = 3    # SpeechStreamFileMode.SSFMCreateForWrite
MAX_PHRASES = 200

_LOG = logging.getLogger("debrief.voice")


def _clamp(v, lo, hi, default):
    try:
        return max(lo, min(hi, int(round(float(v)))))
    except (TypeError, ValueError):
        return default


def speak_seconds(text: str, rate: int = 0) -> float:
    """Rough time SAPI takes to say this (about 2.5 words a second at rate 0)."""
    words = max(1, len(str(text or "").split()))
    return (words / 2.5) * (0.9 ** _clamp(rate, -10, 10, 0))


def wav_seconds(path) -> float | None:
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except Exception:  # noqa: BLE001
        return None


def write_tone(path, freq: float = 880.0, seconds: float = 0.12, rate: int = 22050, level: float = 0.35):
    """A short sine tone with soft edges as a 16-bit mono WAV (the brake-point beep)."""
    n = int(seconds * rate)
    edge = max(1, int(0.01 * rate))
    frames = bytearray()
    for i in range(n):
        env = min(1.0, i / edge, (n - 1 - i) / edge)
        frames += struct.pack("<h", int(32767 * level * env * math.sin(2 * math.pi * freq * i / rate)))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return path


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

    def prepare(self, phrases):
        pass

    def duration(self, text):
        return None

    def cancel(self):
        pass

    def beep(self):
        pass

    def set(self, volume=None, rate=None):
        pass

    def status(self) -> dict:
        return {"backend": self.backend, "available": False, "reason": self.reason, "rendered": 0}

    def close(self):
        pass


class FakeVoice:
    """For tests: records every cue as {t, text, priority, purge}. t is the time the caller passes (session time
    in the coach's tests) or, without one, the clock's time. durations: optional {text: seconds} as if rendered."""
    backend = "fake"
    available = True

    def __init__(self, clock=time.monotonic, rate: int = 0, durations: dict | None = None):
        self.clock = clock
        self.rate = rate
        self.volume = 100
        self.said: list[dict] = []
        self.cancels: list = []
        self.beeps: list = []
        self.prepared: list = []
        self.durations = dict(durations or {})
        self.closed = False
        self._busy_until = -1e9
        self._last_pri = -1

    def say(self, text: str, priority: int = 0, t: float | None = None) -> bool:
        now = self.clock() if t is None else float(t)
        purge = priority > self._last_pri and now < self._busy_until
        self.said.append({"t": now, "text": text, "priority": priority, "purge": purge})
        self._busy_until = now + (self.duration(text) or speak_seconds(text, self.rate))
        self._last_pri = priority
        return True

    def texts(self) -> list[str]:
        return [x["text"] for x in self.said]

    def prepare(self, phrases):
        self.prepared.append(list(phrases))

    def duration(self, text):
        return self.durations.get(text)

    def cancel(self):
        self.cancels.append(self.clock())
        self._busy_until = -1e9

    def beep(self):
        self.beeps.append(self.clock())

    def set(self, volume=None, rate=None):
        if volume is not None:
            self.volume = _clamp(volume, 0, 100, 100)
        if rate is not None:
            self.rate = _clamp(rate, -10, 10, 0)

    def status(self) -> dict:
        return {"backend": "fake", "available": True, "reason": "", "rendered": len(self.durations)}

    def close(self):
        self.closed = True


def _com_object(progid: str):
    """(COM object, backend, uninitialise) for progid on THIS thread, late-bound. Raises when no route works."""
    errors = []
    try:
        import pythoncom  # pywin32
        import win32com.client.dynamic
        pythoncom.CoInitialize()
        try:
            return win32com.client.dynamic.Dispatch(progid), "pywin32", pythoncom.CoUninitialize
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
            return comtypes.client.CreateObject(progid, dynamic=True), "comtypes", comtypes.CoUninitialize
        except Exception:
            comtypes.CoUninitialize()
            raise
    except Exception as e:  # noqa: BLE001
        errors.append(f"comtypes: {e.__class__.__name__}: {str(e)[:80]}")
    raise RuntimeError("; ".join(errors))


def _new_com(progid: str, backend: str):
    """Another COM object on a thread that is already initialised for this backend."""
    if backend == "pywin32":
        import win32com.client.dynamic
        return win32com.client.dynamic.Dispatch(progid)
    import comtypes.client
    return comtypes.client.CreateObject(progid, dynamic=True)


class SapiVoice:
    """Windows speech (SAPI) on its own thread, with pre-rendered phrases played by winsound. Never raises; if SAPI
    can't be started it turns into a silent voice and status() says why."""

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
        self._dir = None
        self._wavs: dict = {}            # text -> (path, seconds), filled by the render thread
        self._wlock = threading.Lock()
        self._render_gen = 0
        self._render_thread = None
        self._phrases: list = []
        self._beep_path = None
        self._thread = threading.Thread(target=self._run, name="debrief-voice", daemon=True)
        self._thread.start()

    # ---- caller side (any thread) ----
    def say(self, text: str, priority: int = 0, t: float | None = None) -> bool:
        if self._stop.is_set() or not text:
            return False
        now = time.monotonic()
        purge = priority > self._last_pri and now < self._busy_until
        self._busy_until = now + (self.duration(text) or speak_seconds(text, self.rate))
        self._last_pri = priority
        self._put(("say", text, purge))
        return True

    def duration(self, text: str) -> float | None:
        with self._wlock:
            x = self._wavs.get(text)
        return x[1] if x else None

    def prepare(self, phrases):
        """Render these phrases to WAV in the background (replacing any earlier set). Cues said before a phrase is
        ready are spoken live instead."""
        phrases = [p for p in dict.fromkeys(str(x) for x in phrases or []) if p][:MAX_PHRASES]
        self._phrases = phrases
        self._render_gen += 1
        gen = self._render_gen
        self._render_thread = threading.Thread(target=self._render, args=(gen, phrases), name="debrief-voice-render",
                                               daemon=True)
        self._render_thread.start()

    def cancel(self):
        """Stop whatever is being said, now."""
        self._busy_until = -1e9
        self._drain()
        self._put(("cancel", None, False))

    def beep(self):
        self._put(("beep", None, False))

    def set(self, volume=None, rate=None):
        changed = False
        if volume is not None:
            v = _clamp(volume, 0, 100, self.volume)
            changed |= v != self.volume
            self.volume = v
        if rate is not None:
            r = _clamp(rate, -10, 10, self.rate)
            changed |= r != self.rate
            self.rate = r
        self._put(("set", None, False))
        if changed and self._phrases:            # rendered audio has the old volume/rate baked in
            with self._wlock:
                self._wavs = {}
            self.prepare(self._phrases)

    def _drain(self):
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass

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
        with self._wlock:
            n = len(self._wavs)
        return {"backend": self.backend, "available": self.available, "reason": self.reason, "rendered": n}

    def wait_ready(self, timeout: float = 5.0) -> bool:
        self._ready.wait(timeout)
        return self.available

    def close(self, timeout: float = 2.0):
        self._stop.set()
        self._render_gen += 1
        self._put(("stop", None, False))
        for th in (self._thread, self._render_thread):
            if th is not None and th.is_alive() and threading.current_thread() is not th:
                th.join(timeout)
        if self._dir:
            shutil.rmtree(self._dir, ignore_errors=True)

    # ---- the render thread ----
    def _render(self, gen: int, phrases: list):
        if not self._ready.wait(10.0) or not self.available or not self._dir:
            return
        uninit = None
        try:
            sp, backend, uninit = _com_object("SAPI.SpVoice")
            sp.Volume, sp.Rate = self.volume, self.rate
            for i, text in enumerate(phrases):
                if gen != self._render_gen or self._stop.is_set():
                    return
                path = Path(self._dir) / f"g{gen}_{i}.wav"
                fs = _new_com("SAPI.SpFileStream", backend)
                fs.Open(str(path), SSFM_CREATE_FOR_WRITE, False)
                try:
                    sp.AudioOutputStream = fs
                    sp.Speak(text, 0)                        # synchronous, into the file
                finally:
                    fs.Close()
                secs = wav_seconds(path)
                if secs:
                    with self._wlock:
                        if gen == self._render_gen:
                            self._wavs[text] = (str(path), secs)
                time.sleep(0.005)                            # stay light next to iRacing
        except Exception as e:  # noqa: BLE001
            self._log(f"voice: phrases could not be prepared, speaking live instead ({e.__class__.__name__})")
        finally:
            if uninit is not None:
                try:
                    uninit()
                except Exception:  # noqa: BLE001
                    pass

    # ---- the voice's own thread ----
    def _apply(self, sp):
        try:
            sp.Volume = self.volume
            sp.Rate = self.rate
        except Exception as e:  # noqa: BLE001
            self._log(f"voice: could not set volume/rate ({e.__class__.__name__})")

    def _run(self):
        sp, uninit, ws = None, None, None
        try:
            sp, self.backend, uninit = _com_object("SAPI.SpVoice")
            self.available = True
            self._apply(sp)
            try:
                import winsound
                ws = winsound
                self._dir = tempfile.mkdtemp(prefix="debrief-voice-")
                self._beep_path = str(write_tone(Path(self._dir) / "beep.wav"))
            except Exception as e:  # noqa: BLE001
                ws = None
                self._log(f"voice: no pre-rendered sound, speaking live ({e.__class__.__name__})")
        except Exception as e:  # noqa: BLE001
            self.backend, self.available = "none", False
            self.reason = f"Windows speech could not be started ({str(e)[:160]})"
            self._log(f"voice: {self.reason}")
        finally:
            self._ready.set()

        def stop_all():
            if ws is not None:
                try:
                    ws.PlaySound(None, 0)
                except Exception:  # noqa: BLE001
                    pass
            if sp is not None:
                try:
                    sp.Speak("", SVSF_ASYNC | SVSF_PURGE)
                except Exception:  # noqa: BLE001
                    pass
        try:
            while not self._stop.is_set():
                try:
                    kind, text, purge = self._q.get(timeout=0.5)
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
                    elif kind == "cancel":
                        stop_all()
                    elif kind == "beep" and ws is not None and self._beep_path:
                        ws.PlaySound(self._beep_path, ws.SND_FILENAME | ws.SND_ASYNC | ws.SND_NODEFAULT)
                    elif kind == "say":
                        with self._wlock:
                            wav = self._wavs.get(text)
                        if purge:
                            stop_all()
                        if wav is not None and ws is not None:
                            # a new async PlaySound replaces one still playing
                            ws.PlaySound(wav[0], ws.SND_FILENAME | ws.SND_ASYNC | ws.SND_NODEFAULT)
                        else:
                            sp.Speak(text, SVSF_ASYNC | (SVSF_PURGE if purge else 0))
                except Exception as e:  # noqa: BLE001
                    self._log(f"voice: speaking failed ({e.__class__.__name__}: {str(e)[:120]})")
        finally:
            stop_all()
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
