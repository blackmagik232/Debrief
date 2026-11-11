"""Offline speech recognition for "Talk to your engineer" (2.31): Vosk (Apache-2.0) with its small English model, the microphone
through sounddevice (PortAudio). Nothing is sent anywhere: the audio is read while the button is held and thrown away.

  * the model (vosk-model-small-en-us-0.15, about 40 MB) is downloaded ONCE, the first time Talk to your engineer is turned
    on, into %LOCALAPPDATA%\\Debrief\\models, with a progress line and a SHA-256 check;
  * the recogniser is only allowed to hear the engineer's phrases (intents.GRAMMAR) plus "[unk]", which is far more reliable
    with a headset and engine noise than free dictation;
  * both libraries are optional at run time: without them Talk-back says why in plain words and the rest of Debrief works.

Everything the tests need is injectable: `Recognizer(model_factory=...)`, `Mic(backend=...)`.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

from . import intents

MODEL_NAME = "vosk-model-small-en-us-0.15"
MODEL_URL = f"https://alphacephei.com/vosk/models/{MODEL_NAME}.zip"
# The expected SHA-256 of the zip. NOT filled in yet: the build sandbox could not reach the model host to hash it (see the
# 2.31 CHANGELOG, "Not proven yet"). While it is None the download still checks the zip is a real Vosk model, writes the
# hash it got next to the model and into the log, and the status line says the hash is not pinned.
MODEL_SHA256 = None
MODEL_SIZE_HINT = 41_000_000
MAX_DOWNLOAD_BYTES = 150_000_000       # the real file is about 41 MB: anything much bigger is not the model
MAX_UNZIPPED_BYTES = 400_000_000       # unpacked (about 70 MB for the real one): a zip bomb is refused before it is written
MAX_ZIP_FILES = 2000
RATE = 16000
BLOCK = 1600                     # 0.1 s of audio per chunk
MAX_HOLD_S = 15.0
MODEL_FILES = ("am/final.mdl", "conf/model.conf")


class SttError(Exception):
    pass


def models_dir(base=None) -> Path:
    if base:
        return Path(base) / "models"
    root = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(root) / "Debrief" / "models"


def model_path(base=None) -> Path:
    return models_dir(base) / MODEL_NAME


def model_ready(base=None) -> bool:
    p = model_path(base)
    return all((p / f).exists() for f in MODEL_FILES)


def have_vosk() -> bool:
    try:
        import vosk  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def have_mic_lib() -> bool:
    try:
        import sounddevice  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _safe_extract(zf: zipfile.ZipFile, dest: Path):
    root = dest.resolve()
    infos = zf.infolist()
    if len(infos) > MAX_ZIP_FILES or sum(max(0, m.file_size) for m in infos) > MAX_UNZIPPED_BYTES:
        raise SttError("The speech model download was far bigger than a speech model, so it was not used.")
    for m in infos:
        target = (dest / m.filename).resolve()
        if root != target and root not in target.parents:
            raise SttError("The model download had an unsafe file name and was not used.")
    zf.extractall(dest)


def download_model(base=None, progress=None, opener=None, expect_sha=None, url=MODEL_URL) -> dict:
    """Download, check and unpack the model. progress(done_bytes, total_bytes_or_None). Returns {"path", "sha256", "pinned"}.
    Raises SttError with a plain sentence. Idempotent: a model already in place is left alone."""
    expect = expect_sha if expect_sha is not None else MODEL_SHA256
    if model_ready(base):
        return {"path": str(model_path(base)), "sha256": _saved_hash(base), "pinned": bool(expect), "already": True}
    d = models_dir(base)
    d.mkdir(parents=True, exist_ok=True)
    opener = opener or urllib.request.urlopen
    tmp = Path(tempfile.mkdtemp(prefix="model-", dir=str(d)))
    try:
        zpath = tmp / "model.zip"
        h = hashlib.sha256()
        try:
            with opener(url, timeout=30) as r, open(zpath, "wb") as f:
                total = None
                try:
                    total = int(r.headers.get("Content-Length") or 0) or None
                except Exception:  # noqa: BLE001
                    total = None
                if total and total > MAX_DOWNLOAD_BYTES:
                    raise SttError("The speech model download was far bigger than a speech model, so it was not started.")
                done = 0
                while True:
                    chunk = r.read(262144)
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    done += len(chunk)
                    if done > MAX_DOWNLOAD_BYTES:
                        raise SttError("The speech model download was far bigger than a speech model, so it was stopped.")
                    if progress:
                        progress(done, total or MODEL_SIZE_HINT)
        except SttError:
            raise
        except Exception as e:  # noqa: BLE001
            raise SttError("The speech model could not be downloaded (" + e.__class__.__name__
                           + "). Check the internet connection and turn Talk to your engineer on again.")
        digest = h.hexdigest()
        if expect and digest.lower() != expect.lower():
            raise SttError("The speech model download did not match the expected checksum, so it was not used.")
        try:
            with zipfile.ZipFile(zpath) as zf:
                names = zf.namelist()
                top = names[0].split("/")[0] if names else ""
                if not all(f"{top}/{f}" in names for f in MODEL_FILES):
                    raise SttError("The speech model download was not a Vosk model, so it was not used.")
                out = tmp / "x"
                out.mkdir()
                _safe_extract(zf, out)
        except zipfile.BadZipFile:
            raise SttError("The speech model download was damaged, so it was not used.")
        final = model_path(base)
        if final.exists():
            shutil.rmtree(final, ignore_errors=True)
        shutil.move(str(out / top), str(final))
        try:
            (final / "debrief.sha256").write_text(digest + "\n", encoding="utf-8")
        except OSError:
            pass
        return {"path": str(final), "sha256": digest, "pinned": bool(expect)}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _saved_hash(base=None):
    try:
        return (model_path(base) / "debrief.sha256").read_text(encoding="utf-8").strip()
    except OSError:
        return None


class Recognizer:
    """A Vosk recogniser with a restricted grammar. feed(pcm16 bytes) while the button is held; finish() -> the words."""

    def __init__(self, base=None, model_factory=None, grammar=None):
        self.base = base
        self._model_factory = model_factory
        self._model = None
        self._grammar = list(grammar or intents.GRAMMAR)
        self._rec = None
        self._lock = threading.Lock()

    def load(self):
        """Load the model (about a second, 50 to 100 MB of memory). Safe to call twice."""
        with self._lock:
            if self._model is not None:
                return
            if self._model_factory is not None:
                self._model = self._model_factory()
                return
            try:
                import vosk
            except Exception:  # noqa: BLE001
                raise SttError("Speech recognition is not installed in this copy of Debrief.")
            try:
                vosk.SetLogLevel(-1)
            except Exception:  # noqa: BLE001
                pass
            if not model_ready(self.base):
                raise SttError("The speech model has not been downloaded yet.")
            self._model = vosk.Model(str(model_path(self.base)))

    def begin(self):
        self.load()
        phrases = self._grammar + ["[unk]"]
        if self._model_factory is not None:
            self._rec = self._model.recognizer(RATE, json.dumps(phrases))
            return
        import vosk
        self._rec = vosk.KaldiRecognizer(self._model, RATE, json.dumps(phrases))

    def feed(self, pcm: bytes):
        if self._rec is not None and pcm:
            self._rec.AcceptWaveform(pcm)

    def finish(self) -> str:
        rec, self._rec = self._rec, None
        if rec is None:
            return ""
        try:
            res = json.loads(rec.FinalResult() or "{}")
        except (TypeError, ValueError):
            return ""
        text = str(res.get("text") or "").replace("[unk]", " ")
        return " ".join(text.split())


class Mic:
    """16 kHz mono microphone through sounddevice. open(device_name) -> stream; chunks are pushed to a callback."""

    def __init__(self, backend=None, device=""):
        self._backend = backend
        self.device = device or ""
        self._stream = None

    def _be(self):
        if self._backend is not None:
            return self._backend
        try:
            import sounddevice as sd
            return sd
        except Exception:  # noqa: BLE001
            raise SttError("Talk-back needs a microphone (the audio library is not available on this PC).")

    def devices(self) -> list:
        try:
            be = self._be()
            out = []
            for d in be.query_devices():
                if (d.get("max_input_channels") or 0) > 0:
                    out.append(str(d.get("name") or ""))
            seen, res = set(), []
            for n in out:
                if n and n not in seen:
                    seen.add(n)
                    res.append(n)
            return res
        except Exception:  # noqa: BLE001
            return []

    def start(self, on_chunk):
        be = self._be()
        kw = {}
        if self.device:
            kw["device"] = self.device
        try:
            self._stream = be.RawInputStream(samplerate=RATE, blocksize=BLOCK, channels=1, dtype="int16",
                                             callback=lambda indata, frames, t, status: on_chunk(bytes(indata)), **kw)
            self._stream.start()
        except SttError:
            raise
        except Exception as e:  # noqa: BLE001
            self._stream = None
            raise SttError("Talk-back needs a microphone: " + (str(e)[:100] or e.__class__.__name__))

    def stop(self):
        s, self._stream = self._stream, None
        if s is not None:
            try:
                s.stop()
                s.close()
            except Exception:  # noqa: BLE001
                pass
