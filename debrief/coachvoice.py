"""The coach's voice in Coach 2.0: a voice pack (pre-made neural speech, debrief/voicepack.py) played through the
device he chose (debrief/audioout.py), with Windows speech only for the rare phrase the pack can't say. It never
changes iRacing's (or any other program's) volume.

    v = PackVoice("engineer", user_dir, volume=90, rate=0, player=audioout.Player(backend, "Headset"),
                  renderer=voice.SapiRenderer())
    v.wait_ready(); v.prepare(coach.vocabulary())
    v.duration("Brake marker, T4")      # exact seconds of the pack's audio (the coach times cues with it)
    v.say("Brake marker, T4", priority=3)
    v.repeat(); v.preview(); v.measure_latency(); v.status(); v.close()

It has the same methods as voice.SapiVoice (say, prepare, duration, cancel, beep, set, status, close, wait_ready),
so livecoach.LiveRunner drives either. Volume is applied to the audio itself (0..100); the speaking speed only
changes Windows speech (a pack's speed is part of its recording).

Priorities follow the coach's: a cue with a higher priority than the one playing stops it; an equal or lower one
waits for it (dropped if it can't start within a second). repeat(), preview() and the test phrase always play at
once. Never raises to the caller.
"""
from __future__ import annotations

import logging
import math
import sys
import threading
import time
from pathlib import Path

import numpy as np

from . import audioout, voicepack

_LOG = logging.getLogger("debrief.coachvoice")
MAX_PHRASES = 400
FALLBACK_FRESH_S = 0.8        # a phrase Windows speech had to render on the spot is played only if ready this soon
FALLBACK_RATE = 16000         # Windows speech is resampled to this when no pack is loaded
TEST_TEXT = "Brake marker, T3"


def tone(rate: int, freq: float = 880.0, seconds: float = 0.12, level: float = 0.35):
    n = int(seconds * rate)
    i = np.arange(n)
    edge = max(1, int(0.01 * rate))
    env = np.minimum(1.0, np.minimum(i / edge, (n - 1 - i) / edge))
    return level * env * np.sin(2 * math.pi * freq * i / rate)


class PackVoice:
    backend = "pack"

    def __init__(self, pack_id=None, user_dir=None, volume: int = 100, rate: int = 0, player=None, renderer=None,
                 log=None, clock=time.monotonic, bundled_dir=None, pack=None):
        self.pack_id = pack_id
        self.volume = max(0, min(100, int(volume)))
        self.rate = max(-10, min(10, int(rate)))
        self.player = player
        self.renderer = renderer
        self._log = log or _LOG.warning
        self._clock = clock
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._closed = False
        self.pack = None
        self.pack_error = ""
        self.sr = FALLBACK_RATE
        self._cache: dict = {}            # text -> float samples at self.sr, full volume (pack phrases)
        self._sapi: dict = {}             # text -> float samples at self.sr, full volume (Windows speech)
        self._sapi_gen = 0
        self._phrases: list = []
        self.last = None                  # (text, priority) of the last cue said
        self.said = 0
        self.said_fallback = 0
        self.silent: list = []            # phrases that could not be said at all
        self.playing = 0
        self.history: list = []           # [{"text", "source", "t_asked", "t_start", "t_end", "ok"}] (last 50)
        self._uncovered: set = set()
        self.available = player is not None
        self.reason = "" if player is not None else "no sound device"
        threading.Thread(target=self._load, args=(pack, bundled_dir, user_dir), name="debrief-voicepack",
                         daemon=True).start()

    # ---- loading ----
    def _load(self, pack, bundled_dir, user_dir):
        try:
            if pack is None:
                pack = voicepack.find_pack(self.pack_id, user_dir=user_dir, bundled_dir=bundled_dir)
            if pack is None:
                self.pack_error = "no voice pack could be loaded, so Windows speech says everything"
                self._log(f"voice: {self.pack_error}")
            else:
                with self._lock:
                    self.pack = pack
                    self.sr = pack.rate
        except Exception as e:  # noqa: BLE001
            self.pack_error = f"the voice pack could not be loaded ({e.__class__.__name__})"
            self._log(f"voice: {self.pack_error}")
        finally:
            self._ready.set()

    def wait_ready(self, timeout: float = 5.0) -> bool:
        self._ready.wait(timeout)
        return self.available

    # ---- what the coach calls ----
    def _keys(self, text):
        p = self.pack
        return p.compose(text) if p is not None else None

    def covers(self, text) -> bool:
        return self._keys(text) is not None

    def duration(self, text) -> float | None:
        """Exact seconds of what say(text) plays: the pack's clips (no audio needed to know it), or Windows speech
        already rendered; None when not known yet (the coach then estimates from the words)."""
        keys = self._keys(text)
        if keys:
            ms = self.pack.duration_ms(keys)
            return ms / 1000.0 if ms else None
        with self._lock:
            x = self._sapi.get(text)
        return len(x) / float(self.sr) if x is not None else None

    def prepare(self, phrases):
        """Get these phrases ready: pack phrases rendered now (a few ms each), the others by Windows speech on its
        own thread, so nothing is synthesised while he drives."""
        self._ready.wait(5.0)
        phrases = [p for p in dict.fromkeys(str(x) for x in phrases or []) if p][:MAX_PHRASES]
        self._phrases = phrases
        todo = []
        for p in phrases:
            keys = self._keys(p)
            if keys:
                with self._lock:
                    have = p in self._cache
                if not have:
                    try:
                        x = self.pack.render_samples(keys)
                        with self._lock:
                            self._cache[p] = x
                    except Exception:  # noqa: BLE001
                        todo.append(p)
            else:
                todo.append(p)
        with self._lock:
            self._uncovered = set(todo)
        if todo and self.renderer is not None:
            self._sapi_gen += 1
            gen = self._sapi_gen
            for p in todo:
                self.renderer.render_async(p, self.rate, lambda r, p=p, gen=gen: self._got_sapi(p, r, gen))

    def _got_sapi(self, text, res, gen):
        if res is None or gen != self._sapi_gen:
            return
        pcm, sr = res
        x = np.frombuffer(pcm[:len(pcm) - len(pcm) % 2], "<i2").astype(np.float64) / 32768.0
        x = voicepack.resample(x, sr, self.sr) if sr != self.sr else x
        with self._lock:
            self._sapi[text] = x

    def _samples(self, text):
        """(float samples at self.sr, source) or (None, None)."""
        with self._lock:
            x = self._cache.get(text)
            if x is not None:
                return x, "pack"
            y = self._sapi.get(text)
        if y is not None:
            return y, "windows"
        keys = self._keys(text)
        if keys:
            try:
                x = self.pack.render_samples(keys)
                with self._lock:
                    self._cache[text] = x
                return x, "pack"
            except Exception:  # noqa: BLE001
                return None, None
        return None, None

    def _pcm(self, x, volume=None) -> bytes:
        v = (self.volume if volume is None else volume) / 100.0
        return voicepack.to_pcm16(np.asarray(x) * v)

    def _play(self, text, x, source, priority, interrupt, kind="cue", check=None) -> bool:
        if self.player is None:
            return False
        rec = {"text": text, "source": source, "kind": kind, "t_asked": self._clock(), "t_start": None,
               "t_end": None, "ok": None}

        def started(_it):
            rec["t_start"] = self._clock()
            self.playing += 1

        def ended(_it, ok):
            rec["t_end"], rec["ok"] = self._clock(), bool(ok)
            if rec["t_start"] is not None:
                self.playing = max(0, self.playing - 1)
        with self._lock:
            self.history.append(rec)
            del self.history[:-50]
        kw = {"check": check} if check is not None else {}
        return self.player.play(self._pcm(x), self.sr, priority=priority, interrupt=interrupt, on_start=started,
                                on_end=ended, tag=text, **kw)

    def say(self, text: str, priority: int = 0, t=None, interrupt: bool = False, check=None) -> bool:
        """check (2.17.2): asked right before the sound starts; a reason back means the cue no longer fits and it is
        not played (the coach logs why)."""
        if self._closed or not text:
            return False
        self._ready.wait(5.0)
        x, source = self._samples(text)
        if x is not None:
            ok = self._play(text, x, source, priority, interrupt, check=check)
            if ok:
                self.said += 1
                self.said_fallback += source == "windows"
                self.last = (text, priority)
            return ok
        if self.renderer is None or self.player is None:
            self.silent.append(text)
            del self.silent[:-20]
            self._log(f"voice (silent: no pack phrase and no Windows speech): {text}")
            return False
        asked = self._clock()
        gen = self._sapi_gen

        def ready(res):
            self._got_sapi(text, res, gen)
            with self._lock:
                y = self._sapi.get(text)
            if y is None or self._closed:
                return
            if self._clock() - asked > FALLBACK_FRESH_S:
                self._log(f"voice: '{text}' was ready too late and was not said")
                return
            if self._play(text, y, "windows", priority, interrupt, check=check):
                self.said += 1
                self.said_fallback += 1
                self.last = (text, priority)
        self.renderer.render_async(text, self.rate, ready)
        return True

    def repeat(self) -> str | None:
        """Say the last cue again, now. Returns its text, or None when nothing has been said yet."""
        last = self.last
        if not last:
            return None
        x, source = self._samples(last[0])
        if x is None or not self._play(last[0], x, source, 9, True, kind="repeat"):
            return None
        return last[0]

    def say_now(self, text: str) -> bool:
        """The test phrase: played at once, whatever is playing."""
        x, source = self._samples(text)
        if x is None:
            return self.say(text, priority=9, interrupt=True)
        return self._play(text, x, source, 9, True, kind="test")

    def preview(self) -> float | None:
        """Play the pack's sample tape (about 15 s). Returns its length in seconds, or None."""
        self._ready.wait(5.0)
        p = self.pack
        if p is None or self.player is None:
            return None
        x, sr = voicepack.preview_samples(p)
        if sr != self.sr:
            x = voicepack.resample(x, sr, self.sr)
        if not len(x):
            return None
        self._play("(preview)", x, "pack", 9, True, kind="preview")
        return len(x) / float(self.sr)

    def cancel(self):
        if self.player is not None:
            self.player.cancel()

    def beep(self):
        if self.player is not None:
            self._play("(beep)", tone(self.sr), "beep", 8, True, kind="beep")

    def set(self, volume=None, rate=None):
        if volume is not None:
            try:
                self.volume = max(0, min(100, int(round(float(volume)))))
            except (TypeError, ValueError):
                pass
        if rate is not None:
            try:
                r = max(-10, min(10, int(round(float(rate)))))
            except (TypeError, ValueError):
                r = self.rate
            if r != self.rate:
                self.rate = r
                with self._lock:
                    self._sapi = {}
                if self._phrases:
                    threading.Thread(target=self.prepare, args=(list(self._phrases),), daemon=True).start()

    def set_device(self, name):
        if self.player is not None:
            self.player.set_device(name)

    def measure_latency(self):
        return self.player.measure_latency() if self.player is not None else None

    def status(self) -> dict:
        p = self.pack
        with self._lock:
            n = len(self._cache) + len(self._sapi)
            unc = sorted(self._uncovered)
        dev = self.player.status() if self.player is not None else None
        return {"backend": "pack" if p is not None else "windows speech", "available": self.available,
                "reason": self.reason or self.pack_error, "rendered": n,
                "pack": ({"id": p.id, "name": p.name, "credit": p.manifest.get("voice_credit", ""),
                          "license": p.manifest.get("license", ""), "rate": p.rate} if p is not None else None),
                "pack_error": self.pack_error,
                "fallback": ("Windows speech" if self.renderer is not None and getattr(self.renderer, "available", True)
                             else None),
                "uncovered": unc[:20], "said": self.said, "said_fallback": self.said_fallback,
                "silent": list(self.silent[-5:]), "device": dev}

    def close(self, timeout: float = 2.0):
        self._closed = True
        for part in (self.player, self.renderer):
            if part is not None:
                try:
                    part.close(timeout=timeout)
                except TypeError:
                    part.close()
                except Exception:  # noqa: BLE001
                    pass


def _pack_name(pack_id, packs) -> str:
    row = next((r for r in packs or [] if r.get("id") == pack_id), None)
    return (row or {}).get("name") or (str(pack_id) if pack_id else "The default voice")


def voice_note(chosen, status: dict | None = None, packs=None) -> dict:
    """2.18: whether the voice he chose is the one that plays, and if not why and what plays instead, in plain words
    for the Coach page: {"ok", "text", "using"}. chosen: the voice_pack id saved in the settings ("" = the default).
    status: the running voice's status() (None while the coach is off: then only the installed packs are looked at).
    packs: voicepack.list_packs rows. Never raises."""
    try:
        packs = list(packs or [])
        good = [r for r in packs if r.get("ok")]
        default = next((r for r in good if r.get("default")), good[0] if good else None)
        row = next((r for r in packs if r.get("id") == chosen), None) if chosen else default
        name = _pack_name(chosen, packs) if chosen else ((default or {}).get("name") or "The default voice")
        why = ""
        if chosen and row is None:
            why = "its voice pack isn't installed"
        elif row is not None and not row.get("ok"):
            why = "its voice pack is damaged (" + str((row.get("problems") or ["it can't be read"])[0]) + ")"
        st = status or {}
        if status is None:
            if not why:
                return {"ok": True, "text": "", "using": name}
            if default is not None and (row is None or default.get("id") != row.get("id")):
                return {"ok": False, "using": default.get("name"),
                        "text": f"{name} can't play: {why}. The coach uses {default.get('name')} instead."}
            return {"ok": False, "using": "Windows speech",
                    "text": f"{name} can't play: {why}, and no other voice pack is installed. Windows speech says "
                            f"the cues instead."}
        pk = st.get("pack")
        if pk:
            if not chosen or pk.get("id") == chosen:
                return {"ok": True, "text": "", "using": pk.get("name") or name}
            return {"ok": False, "using": pk.get("name"),
                    "text": f"{name} can't play: {why or 'its voice pack could not be loaded'}. The coach uses "
                            f"{pk.get('name')} instead."}
        backend = str(st.get("backend") or "")
        reason = str(st.get("pack_error") or st.get("reason") or "").strip()
        if backend == "fake":
            return {"ok": True, "text": "", "using": name}
        if backend == "windows speech":
            return {"ok": False, "using": "Windows speech",
                    "text": f"{name} can't play: {why or reason or 'no voice pack could be loaded'}. Windows speech "
                            f"says every cue instead."}
        if backend in ("none", "silent") or (not st.get("available", True) and backend != "starting"):
            return {"ok": False, "using": "nothing",
                    "text": f"{name} can't play and the coach is silent: {reason or 'no sound device could be opened'}."}
        return {"ok": False, "using": "Windows speech",
                "text": f"{name} can't play: Debrief couldn't open a sound device for voice packs"
                        f"{' (' + reason + ')' if reason else ''}. Windows speech says the cues instead."}
    except Exception:  # noqa: BLE001
        return {"ok": True, "text": "", "using": ""}


def make_coach_voice(settings, user_dir=None, state_dir=None, log=None, backend=None, renderer=None):
    """The coach's voice for these livecoach.Settings: a PackVoice through the chosen device on Windows. Off
    Windows (or when no sound device can be opened at all) the older voice (voice.make_voice: Windows speech or
    silent). backend/renderer: stand-ins for tests."""
    from . import voice as voice_mod
    be = backend if backend is not None else audioout.default_backend()
    if be is None:
        return voice_mod.make_voice(volume=settings.volume, rate=settings.rate, log=log)
    player = audioout.Player(be, device=getattr(settings, "device", ""), log=log)
    if renderer is None and backend is None and sys.platform == "win32":
        try:
            renderer = voice_mod.SapiRenderer(log=log)
        except Exception:  # noqa: BLE001
            renderer = None
    return PackVoice(getattr(settings, "voice_pack", None) or None, user_dir=user_dir, volume=settings.volume,
                     rate=settings.rate, player=player, renderer=renderer, log=log)
