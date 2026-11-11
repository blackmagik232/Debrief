"""2.31 Talk to your engineer, the server's side: the push-to-talk button (a keyboard key or a wheel / button-box button), the
speech model download, the microphone picker, and the TalkBack that ties them to the live runner.

The wheel button is coach2's generic wheel role "talk" (hold-style, like the corner-note button); the key is ptt.KeyPoller.
Both go to runner.talk_press / runner.talk_release. Nothing here runs unless Talk to your engineer is on.
"""
from __future__ import annotations

import threading
import time
import traceback

from .. import app, ptt, stt, talkback
from . import settings_io

KEY_SAVED = "talk_key"


def _st(server):
    from . import coach2
    st = coach2.state(server)
    if not hasattr(st, "talk"):
        st.talk = {"keys": None, "tb": None, "model": {"state": "idle", "text": "", "done": 0, "total": 0}, "note": "",
                   "learning": False, "mic": None, "thread": None}
    return st, st.talk


def key_saved():
    return ptt.clean_binding(settings_io.load_extra("coach").get(KEY_SAVED))


def _data_dir():
    from . import coach2
    return coach2.settings_dir()


def _keys(server, runner_fn):
    st, t = _st(server)
    with st.lock:
        if t["keys"] is None:
            f = getattr(server, "key_backend_factory", None)
            t["keys"] = ptt.KeyPoller(on_press=lambda: runner_fn("talk_press"), on_release=lambda: runner_fn("talk_release"),
                                      binding=key_saved(), backend=f() if f else None, log=app.say)
        return t["keys"]


def _runner_call(server):
    def call(name):
        with server.coach_lock:
            r = server.coach
        fn = getattr(r, name, None) if r is not None else None
        if fn is not None:
            fn()
    return call


# ---------------------------------------------------------------- the model
def model_state(server) -> dict:
    _st_, t = _st(server)
    m = dict(t["model"])
    if stt.model_ready(_data_dir()) and m["state"] != "downloading":
        m = {"state": "ready", "text": "The speech model is installed.", "done": 0, "total": 0}
    pct = int(100 * m.get("done", 0) / m["total"]) if m.get("total") else None
    m["pct"] = pct
    m["vosk"] = stt.have_vosk()
    fp = stt._saved_hash(_data_dir()) if m.get("state") == "ready" else None
    m["fingerprint"] = fp                      # the SHA-256 of the file as downloaded (shown in Settings; not pinned yet)
    m["pinned"] = bool(stt.MODEL_SHA256)
    return m


def start_model_download(server) -> dict:
    st, t = _st(server)
    if stt.model_ready(_data_dir()):
        return {"ok": True, "already": True}
    if not stt.have_vosk():
        t["model"] = {"state": "error", "text": "Speech recognition is not installed in this copy of Debrief.", "done": 0, "total": 0}
        return {"ok": False, "error": t["model"]["text"]}
    with st.lock:
        th = t.get("thread")
        if th is not None and th.is_alive():
            return {"ok": True, "already": True}
        t["model"] = {"state": "downloading", "text": "Downloading the speech model (about 40 MB)...", "done": 0, "total": 0}

        def work():
            def prog(done, total):
                t["model"] = {"state": "downloading", "done": done, "total": total,
                              "text": f"Downloading the speech model: {done // 1048576} of {max(1, total // 1048576)} MB"}
            try:
                res = stt.download_model(_data_dir(), progress=prog)
                note = "" if res.get("pinned") else " (checksum recorded, not yet pinned)"
                app.say(f"talk: speech model installed, sha256 {res.get('sha256')}")
                t["model"] = {"state": "ready", "text": "The speech model is installed." + note, "done": 0, "total": 0}
            except stt.SttError as e:
                t["model"] = {"state": "error", "text": str(e), "done": 0, "total": 0}
            except Exception:  # noqa: BLE001
                app.say("ERROR downloading the speech model:\n" + traceback.format_exc())
                t["model"] = {"state": "error", "text": "The speech model could not be downloaded.", "done": 0, "total": 0}
        th = threading.Thread(target=work, name="debrief-model", daemon=True)
        t["thread"] = th
        th.start()
    return {"ok": True}


# ---------------------------------------------------------------- start / stop with the coach
def setup(server, runner, voice, settings):
    """Called when the coach starts (and when the settings change while it runs)."""
    from . import coach2
    st, t = _st(server)
    teardown(server, keep_model=True)
    if runner is None or not getattr(settings, "talk_back", False):
        runner and setattr(runner, "talk", None)
        return
    if not stt.model_ready(_data_dir()):
        start_model_download(server)
    f = getattr(server, "talk_factory", None)
    if f is not None:                                   # tests: fake recogniser and microphone
        rec, mic = f(settings)
    else:
        rec = stt.Recognizer(base=_data_dir())
        mic = stt.Mic(device=getattr(settings, "mic_name", ""))
    eng = runner.engineer

    def beep(name):
        try:
            if hasattr(voice, "play_tone"):
                voice.play_tone(name)
        except Exception:  # noqa: BLE001
            pass
    tb = talkback.TalkBack(ask=lambda text, src="voice": runner.ask_engineer(text, src), recognizer=rec, mic=mic,
                           note=(eng.add_note if eng is not None else None), beep=beep, log=app.say)
    t["tb"] = tb
    runner.talk = tb
    if f is None:
        threading.Thread(target=_preload, args=(rec,), daemon=True, name="debrief-sttload").start()
    k = _keys(server, _runner_call(server))
    k.set_binding(key_saved())
    if key_saved():
        k.start()


def _preload(rec):
    try:
        if stt.model_ready(rec.base):
            rec.load()
    except Exception:  # noqa: BLE001
        pass


def teardown(server, keep_model=False):
    st, t = _st(server)
    k = t.get("keys")
    if k is not None:
        try:
            k.stop()
        except Exception:  # noqa: BLE001
            pass
    tb = t.get("tb")
    if tb is not None:
        try:
            tb.enabled = False
            tb.release()
        except Exception:  # noqa: BLE001
            pass
    t["tb"] = None


# ---------------------------------------------------------------- the settings page
def mic_list(server):
    f = getattr(server, "talk_mic_list", None)
    if f:
        return f()
    try:
        return stt.Mic().devices()
    except Exception:  # noqa: BLE001
        return []


def get_status(server) -> dict:
    from . import coach2
    st, t = _st(server)
    kb = key_saved()
    w = coach2.wheel_status(server, "talk")
    s = server.coach_settings()
    binding = None
    if kb:
        binding = {"kind": "key", "label": kb["label"]}
    elif w.get("label"):
        binding = {"kind": "wheel", "label": w["label"]}
    ks = t["keys"].status() if t.get("keys") is not None else {"state": "off"}
    learning = (w.get("state") == "learning") or ks.get("state") == "learning"
    tb = t.get("tb")
    return {"ok": True, "enabled": bool(s.talk_back), "binding": binding, "learning": learning, "note": t.get("note", ""),
            "model": model_state(server), "mic": s.mic_name, "mics": mic_list(server),
            "libs": {"vosk": stt.have_vosk(), "sounddevice": stt.have_mic_lib()}, "talk": tb.status() if tb else None,
            "text": ("Press the button or key you want to hold to talk..." if learning else
                     (f"Hold {binding['label']} and ask." if binding else "No button chosen yet."))}


def post_learn(h, body):
    """Press the button you want: the next key on the keyboard OR button on any wheel / button box becomes the talk button."""
    from . import coach2
    server = h.server
    st, t = _st(server)
    w = coach2._wheel(server, "talk")
    keys = _keys(server, _runner_call(server))
    t["note"] = ""
    done = threading.Event()
    lock = threading.Lock()

    def finish(kind, binding):
        with lock:
            if done.is_set():
                return
            if binding is None:
                if kind == "timeout":
                    t["note"] = "No button was pressed. Try again and press it within 15 seconds."
                    done.set()
                    w.cancel_learn()
                    keys.cancel_learn()
                return
            done.set()
        if kind == "key":
            w.cancel_learn()
            try:
                with server.coach_json_lock:
                    data = settings_io.load_extra("coach")
                    data[KEY_SAVED] = binding
                    data.pop("wheel_talk", None)
                    settings_io.save_extra("coach", data)
                t["note"] = f"Saved: hold {binding['label']} to talk."
            except OSError:
                t["note"] = "The key couldn't be saved. Details were saved to debrief.log."
            keys.set_binding(binding)
            keys.start()
            w.set_binding(None)
        else:
            keys.cancel_learn()
            try:
                with server.coach_json_lock:
                    data = settings_io.load_extra("coach")
                    data.pop(KEY_SAVED, None)
                    settings_io.save_extra("coach", data)
            except OSError:
                pass
            keys.set_binding(None)
            coach2._wheel_learned(server, binding, "talk")
            t["note"] = f"Saved: hold {binding.get('label')} to talk."
    ok_w = w.available() and w.learn(on_learned=lambda b: finish("wheel", b) if b else finish("timeout", None))
    ok_k = keys.available() and keys.learn(on_learned=lambda b: finish("key", b) if b else finish("timeout", None))
    if not ok_w and not ok_k:
        return h._json({"ok": False, "error": "Buttons and keys can't be read on this computer.", "talk": get_status(server)}, 400)
    return h._json({"ok": True, "talk": get_status(server)})


def post_clear(h, body):
    from . import coach2
    server = h.server
    st, t = _st(server)
    try:
        with server.coach_json_lock:
            data = settings_io.load_extra("coach")
            data.pop(KEY_SAVED, None)
            data.pop("wheel_talk", None)
            settings_io.save_extra("coach", data)
    except OSError:
        raise ValueError("The settings couldn't be saved. Details were saved to debrief.log.")
    if t.get("keys") is not None:
        t["keys"].cancel_learn()
        t["keys"].set_binding(None)
    w = coach2._wheel(server, "talk")
    w.cancel_learn()
    w.set_binding(None)
    t["note"] = "Talk button cleared."
    return h._json({"ok": True, "talk": get_status(server)})


def post_model(h, body):
    res = start_model_download(h.server)
    return h._json({**res, "talk": get_status(h.server)}, 200 if res.get("ok") else 400)


def get_talk(h, q):
    return h._json(get_status(h.server))
