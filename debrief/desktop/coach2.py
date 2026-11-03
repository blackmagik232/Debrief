"""Debrief Coach 2.0 in the window's server: voice packs, the output device, ducking, the audio delay, the cue log,
rehearsal, "Repeat that" and "Mute for 1 minute" (debrief/coachvoice.py, audioout.py, ducking.py, cuelog.py,
voicepack.py).

Kept in its own module so the server only needs a few hook lines in its coach code (make_voice, hotkeys, the wheel,
extra() for coach/status, and get()/post()/upload() for the routes below).

    GET  api/coach/voices                     voice packs, sound devices, the saved voice settings
    GET  api/coach/logs                       recent cue logs (newest first)
    GET  api/coach/log?name=[&summary=1]      one cue log's rows, or its "Copy for Claude" text
    POST api/coach/voice-settings {voice_pack, device, duck, duck_level}
    POST api/coach/preview                    play the chosen pack's sample through the chosen device
    POST api/coach/measure                    measure the audio delay (saved as audio_latency_s)
    POST api/coach/import-pack {folder}       add his own voice pack from a folder on this PC
    POST api/coach/import-pack-zip?name=      ... or from a .zip (the raw body)
    POST api/coach/repeat                     say the last cue again
    POST api/coach/quiet                      mute for 1 minute
    POST api/coach/rehearse {id}              play one of his Practice sessions through the coach, as if live
    POST api/coach/wheel-repeat-learn / wheel-repeat-clear   a wheel button for "Repeat that"
"""
from __future__ import annotations

import re
import shutil
import tempfile
import threading
import time
import traceback
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .. import app
from . import settings_io

GET_ROUTES = ("api/coach/voices", "api/coach/logs", "api/coach/log")
POST_ROUTES = ("api/coach/voice-settings", "api/coach/preview", "api/coach/measure", "api/coach/import-pack",
               "api/coach/repeat", "api/coach/quiet", "api/coach/rehearse", "api/coach/wheel-repeat-learn",
               "api/coach/wheel-repeat-clear")
UPLOAD_ROUTE = "api/coach/import-pack-zip"
QUIET_S = 60.0
SIDE_IDLE_S = 30.0               # a voice made for Preview / Measure (coach off) is closed after this long unused
PACK_ID_OK = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,39}$")
ZIP_NAME_OK = re.compile(r"^[^\x00-\x1f/\\<>:\"|?*]{1,160}\.zip$", re.I)
HOTKEY_LABELS = {"repeat": "Ctrl+Shift+R", "quiet": "Ctrl+Shift+N"}


# ---------------------------------------------------------------- where things live
def settings_dir() -> Path:
    return app.settings_path().parent


def packs_dir() -> Path:
    return settings_dir() / "voicepacks"


def logs_dir() -> Path:
    from .. import cuelog
    return cuelog.folder_for(settings_dir())


# ---------------------------------------------------------------- settings fields (server.COACH_FIELDS uses these)
def clean_pack_id(v):
    if not isinstance(v, str) or not (v == "" or PACK_ID_OK.match(v)):
        raise ValueError("Pick a voice from the list.")
    return v


def clean_device(v):
    from .. import audioout
    if not isinstance(v, str) or len(v) > 200:
        raise ValueError("Pick a device from the list.")
    return audioout.clean_name(v)


def clean_latency(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or not 0 <= float(v) <= 0.5:
        raise ValueError("The audio delay must be 0 to 0.5 seconds.")
    return round(float(v), 3)


EXTRA_FIELDS = {"voice_pack": clean_pack_id, "device": clean_device, "duck": bool,
                "duck_level": (10, 80, "iRacing's volume while the coach talks must be 10 to 80%."),
                "audio_latency_s": clean_latency}


# ---------------------------------------------------------------- state kept on the server
class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.side = None              # (key, voice) made for Preview / Measure while the coach is off
        self.side_used = 0.0
        self.side_timer = None
        self.measure = None           # {"state": measuring | done | error, "value", "text"}
        self.preview = None           # {"state": playing | done | error, "text", "until"}
        self.import_note = None
        self.rehearsal = None         # {"id", "title"} while a rehearsal runs or after it
        self.wheel = None             # wheelmute.WheelMute for "Repeat that"
        self.wheel_note = ""


_STATE_LOCK = threading.Lock()


def state(server) -> State:
    with _STATE_LOCK:
        st = getattr(server, "_coach2", None)
        if st is None:
            st = State()
            server._coach2 = st
        return st


# ---------------------------------------------------------------- the voice and the keys (called by the server)
def make_voice(server, settings):
    """The coach's voice: the server's test stand-in when set, else the voice pack through the chosen device."""
    if getattr(server, "coach_voice_factory", None):
        return server.coach_voice_factory(settings)
    from .. import coachvoice
    return coachvoice.make_coach_voice(settings, user_dir=packs_dir(), state_dir=settings_dir(), log=app.say,
                                       backend=_test_backend(server))


def _test_backend(server):
    f = getattr(server, "coach_audio_factory", None)       # tests only: a FakeBackend
    return f() if f else None


class HotkeyGroup:
    """The coach's system-wide keys (mute, repeat, quiet for a minute), each on its own thread (livecoach.Hotkey).
    .registered is the mute key's (what the server already reports); status() has every key."""

    def __init__(self, keys):
        self.keys = keys                # {"mute"|"repeat"|"quiet": Hotkey}

    @property
    def registered(self) -> bool:
        k = self.keys.get("mute")
        return bool(k is not None and k.registered)

    def start(self, timeout: float = 2.0):
        for k in self.keys.values():
            try:
                k.start(timeout=timeout)
            except Exception:  # noqa: BLE001
                pass
        return self.registered

    def status(self) -> dict:
        return {name: bool(k.registered) for name, k in self.keys.items()}

    def stop(self):
        for k in self.keys.values():
            try:
                k.stop()
            except Exception:  # noqa: BLE001
                pass


def hotkeys(server, settings, runner):
    from .. import livecoach
    return HotkeyGroup({"mute": livecoach.Hotkey(settings.hotkey, runner.toggle, log=app.say),
                        "repeat": livecoach.Hotkey(settings.repeat_hotkey, runner.repeat, log=app.say),
                        "quiet": livecoach.Hotkey(settings.quiet_hotkey, lambda: runner.mute_for(QUIET_S),
                                                  log=app.say)})


def runner_of(server):
    with server.coach_lock:
        r = server.coach
    return r if r is not None and r.running() else None


# ---------------------------------------------------------------- "Repeat that" from a wheel button
def _wheel_saved():
    from .. import wheelmute
    return wheelmute.clean_binding(settings_io.load_extra("coach").get("wheel_repeat"))


def _wheel_press(server):
    r = runner_of(server)
    if r is not None:
        r.repeat()


def _wheel(server):
    from .. import wheelmute
    st = state(server)
    with st.lock:
        if st.wheel is None:
            f = getattr(server, "wheel_backend_factory", None)
            st.wheel = wheelmute.WheelMute(on_press=lambda: _wheel_press(server), binding=_wheel_saved(),
                                           backend=f() if f else None, log=app.say)
        return st.wheel


def start_wheel(server):
    try:
        b = _wheel_saved()
        if b is None:
            return
        w = _wheel(server)
        w.set_binding(b)
        w.start()
    except Exception:  # noqa: BLE001
        app.say("ERROR starting the wheel repeat button:\n" + traceback.format_exc())


def stop_wheel(server):
    st = state(server)
    with st.lock:
        w = st.wheel
    if w is not None:
        try:
            w.stop()
        except Exception:  # noqa: BLE001
            pass


def wheel_status(server) -> dict:
    b = _wheel_saved()
    st = state(server)
    with st.lock:
        w = st.wheel
    s = w.status() if w is not None else {"state": "off", "text": ""}
    state_ = s.get("state")
    if state_ in ("off", "not_set") or (state_ == "ready" and not b):
        text = (f"{b['label']}. It says the last cue again." if b else "No wheel button chosen yet.")
        state_ = "saved" if b else "not_set"
    else:
        text = s.get("text") or ""
    return {"state": state_, "text": text, "label": b["label"] if b else None, "note": st.wheel_note}


def _wheel_learned(server, binding):
    st = state(server)
    if binding:
        try:
            with server.coach_json_lock:
                data = settings_io.load_extra("coach")
                data["wheel_repeat"] = binding
                settings_io.save_extra("coach", data)
            st.wheel_note = f"Saved: {binding.get('label')}."
        except OSError as e:
            app.say(f"ERROR saving the wheel repeat button: {type(e).__name__}: {e}")
            st.wheel_note = "The button couldn't be saved. Details were saved to debrief.log."
    else:
        st.wheel_note = "No button was pressed. Try again and press the button within 15 seconds."
    with server.coach_lock:
        run = server.coach_starting or (server.coach is not None and server.coach.running())
    with st.lock:
        w = st.wheel
    if w is not None and not run:
        w.stop(0)


# ---------------------------------------------------------------- a voice for Preview / Measure while the coach is off
def _side_key(s) -> tuple:
    return (s.voice_pack, s.device, s.volume, s.rate)


def _side_voice(server):
    """The running coach's voice, else a voice made for this (kept SIDE_IDLE_S, then closed)."""
    with server.coach_lock:
        r, vc = server.coach, server.coach_voice
    if r is not None and r.running() and vc is not None:
        return vc
    settings = server.coach_settings()
    st = state(server)
    key = _side_key(settings)
    with st.lock:
        old = None
        if st.side is not None and st.side[0] != key:
            old, st.side = st.side[1], None
        if st.side is None:
            st.side = (key, make_voice(server, settings))
        v = st.side[1]
        st.side_used = time.monotonic()
        if st.side_timer is None:
            st.side_timer = threading.Timer(SIDE_IDLE_S, _side_idle, args=(server,))
            st.side_timer.daemon = True
            st.side_timer.start()
    if old is not None:
        _close(old)
    wait = getattr(v, "wait_ready", None)
    if callable(wait):
        wait(5.0)
    return v


def _side_idle(server):
    st = state(server)
    with st.lock:
        st.side_timer = None
        busy = (st.measure or {}).get("state") == "measuring" or \
            ((st.preview or {}).get("until") or 0) > time.monotonic()
        if st.side is None:
            return
        if busy or time.monotonic() - st.side_used < SIDE_IDLE_S:
            st.side_timer = threading.Timer(SIDE_IDLE_S / 2, _side_idle, args=(server,))
            st.side_timer.daemon = True
            st.side_timer.start()
            return
        v, st.side = st.side[1], None
    _close(v)


def close_side(server):
    st = state(server)
    with st.lock:
        v, st.side = (st.side[1] if st.side else None), None
        t, st.side_timer = st.side_timer, None
    if t is not None:
        t.cancel()
    if v is not None:
        _close(v)


def _close(v):
    try:
        v.close()
    except Exception:  # noqa: BLE001
        pass


def on_coach_stop(server):
    """Called by the server's stop_coach: the wheel repeat button stops with the coach."""
    stop_wheel(server)


def on_shutdown(server):
    stop_wheel(server)
    close_side(server)


# ---------------------------------------------------------------- coach/status extras
def extra(server, st: dict):
    s2 = state(server)
    with server.coach_lock:
        hk = server.coach_hotkey
    keys = hk.status() if isinstance(hk, HotkeyGroup) else {}
    st["keys"] = {name: {"key": HOTKEY_LABELS.get(name, "Ctrl+Shift+M"), "on": bool(keys.get(name))}
                  for name in ("repeat", "quiet")}
    with s2.lock:
        st["measure"] = dict(s2.measure) if s2.measure else None
        pv = dict(s2.preview) if s2.preview else None
        st["rehearsal"] = dict(s2.rehearsal) if s2.rehearsal else None
    if pv and pv.get("state") == "playing" and (pv.get("until") or 0) <= time.monotonic():
        pv["state"] = "done"
    if pv:
        pv.pop("until", None)
    st["preview"] = pv
    try:
        st["wheel_repeat"] = wheel_status(server)
    except Exception:  # noqa: BLE001
        st["wheel_repeat"] = {"state": "error", "text": "The wheel button couldn't be read."}
    if st.get("mode") != "rehearsal" and st.get("running"):
        with s2.lock:
            if s2.rehearsal and s2.rehearsal.get("state") == "playing":
                s2.rehearsal["state"] = "stopped"


# ---------------------------------------------------------------- GET
def get(h, r, q):
    return {"api/coach/voices": get_voices, "api/coach/logs": get_logs, "api/coach/log": get_log}[r](h, q)


def get_voices(h, q):
    from .. import audioout, voicepack
    from .server import _jsonable, saved_coach_settings
    from .. import livecoach
    s = livecoach.Settings.from_dict(saved_coach_settings())
    packs = []
    for p in voicepack.list_packs(packs_dir()):
        packs.append({k: p.get(k) for k in ("id", "name", "voice_credit", "license", "bundled", "default", "clips",
                                            "ok", "problems")})
    devs = [d["name"] for d in audioout.list_devices(_test_backend(h.server))]
    have = s.device and s.device not in devs
    return h._json(_jsonable({
        "ok": True, "packs": packs, "devices": devs, "device_missing": bool(have),
        "settings": {"voice_pack": s.voice_pack, "device": s.device, "duck": s.duck, "duck_level": s.duck_level,
                     "audio_latency_s": s.audio_latency_s, "rate": s.rate, "volume": s.volume},
        "user_dir": str(packs_dir()), "can_choose_device": bool(devs) or _test_backend(h.server) is not None,
        "import_note": state(h.server).import_note}))


def get_logs(h, q):
    from .. import cuelog
    return h._json({"ok": True, "logs": cuelog.list_logs(logs_dir()), "folder": str(logs_dir())})


def get_log(h, q):
    from .. import __version__, cuelog
    name = q.get("name")
    rows = cuelog.read_log(logs_dir(), name)
    if not rows:
        raise ValueError("That coach log wasn't found.")
    if q.get("summary") == "1":
        return h._json({"ok": True, "name": name, "text": cuelog.summary(rows, name, __version__)})
    keep = ("ev", "t", "lap", "dist_m", "kmh", "kind", "text", "corner", "why", "dropped", "measured", "dur_s",
            "next_brake_s", "lap_time", "clean")
    view = [{k: r.get(k) for k in keep if r.get(k) is not None} for r in rows
            if r.get("ev") not in ("session", "reference", "log_full")]
    return h._json({"ok": True, "name": name, "rows": view[-1500:], "counts": cuelog.counts(rows),
                    "head": next((r for r in rows if r.get("ev") == "session"), {})})


# ---------------------------------------------------------------- POST
def post(h, r, body):
    fn = {"api/coach/voice-settings": post_voice_settings, "api/coach/preview": post_preview,
          "api/coach/measure": post_measure, "api/coach/import-pack": post_import_pack,
          "api/coach/repeat": post_repeat, "api/coach/quiet": post_quiet, "api/coach/rehearse": post_rehearse,
          "api/coach/wheel-repeat-learn": post_wheel_learn, "api/coach/wheel-repeat-clear": post_wheel_clear}[r]
    return fn(h, body if isinstance(body, dict) else {})


def _save_settings(server, new: dict) -> dict:
    from .server import COACH_FIELDS, SETTINGS_WRITE_ERROR, saved_coach_settings
    from .. import livecoach
    merged = livecoach.Settings.from_dict({**saved_coach_settings(), **new}).to_dict()
    keep = {k: merged[k] for k in COACH_FIELDS}
    try:
        with server.coach_json_lock:
            data = settings_io.load_extra("coach")
            data["settings"] = keep
            settings_io.save_extra("coach", data)
    except OSError as e:
        app.say(f"ERROR saving the coach settings: {type(e).__name__}: {e}")
        raise ValueError(SETTINGS_WRITE_ERROR)
    return keep


def post_voice_settings(h, body):
    from .server import clean_coach_settings
    raw = body.get("settings")
    if not isinstance(raw, dict) or not set(raw) <= {"voice_pack", "device", "duck", "duck_level"}:
        raise ValueError("Bad request.")
    new = clean_coach_settings(raw)
    keep = _save_settings(h.server, new)
    with h.server.coach_lock:
        r, vc = h.server.coach, h.server.coach_voice
    note = ""
    if r is not None and r.running():
        if "device" in new:
            fn = getattr(vc, "set_device", None)
            if fn:
                fn(keep["device"])
        if "voice_pack" in new or "duck" in new or "duck_level" in new:
            note = "The coach uses the new voice the next time it starts."
    close_side(h.server)
    return h._json({"ok": True, "settings": {k: keep[k] for k in ("voice_pack", "device", "duck", "duck_level",
                                                                  "audio_latency_s")}, "note": note})


def post_preview(h, body):
    st = state(h.server)
    with st.lock:
        st.preview = {"state": "starting", "text": "Getting the voice ready"}

    def run():
        try:
            v = _side_voice(h.server)
            fn = getattr(v, "preview", None)
            secs = fn() if fn else None
            with st.lock:
                if secs:
                    st.preview = {"state": "playing", "text": f"Playing a {secs:.0f} s sample",
                                  "until": time.monotonic() + secs + 0.5}
                    st.side_used = time.monotonic() + secs
                else:
                    reason = ""
                    try:
                        reason = (v.status() or {}).get("reason") or ""
                    except Exception:  # noqa: BLE001
                        pass
                    st.preview = {"state": "error", "text": "No sample to play" + (f": {reason}" if reason else ".")}
        except Exception:  # noqa: BLE001
            app.say("ERROR playing the voice sample:\n" + traceback.format_exc())
            with st.lock:
                st.preview = {"state": "error", "text": "The sample couldn't be played. Details were saved to debrief.log."}
    threading.Thread(target=run, name="debrief-voice-preview", daemon=True).start()
    return h._json({"ok": True, "pending": True})


def post_measure(h, body):
    st = state(h.server)
    with st.lock:
        if (st.measure or {}).get("state") == "measuring":
            return h._json({"ok": True, "pending": True})
        st.measure = {"state": "measuring", "text": "Measuring"}

    def run():
        try:
            v = _side_voice(h.server)
            fn = getattr(v, "measure_latency", None)
            val = fn() if fn else None
            if val is None:
                with st.lock:
                    st.measure = {"state": "error", "text": "The delay couldn't be measured on this device."}
                return
            keep = _save_settings(h.server, {"audio_latency_s": float(val)})
            r = runner_of(h.server)
            if r is not None:
                r.update_settings({"audio_latency_s": keep["audio_latency_s"]})
            with st.lock:
                st.measure = {"state": "done", "value": keep["audio_latency_s"],
                              "text": f"Audio delay {keep['audio_latency_s'] * 1000:.0f} ms. Saved."}
            app.say(f"Coach audio delay measured: {keep['audio_latency_s'] * 1000:.0f} ms.")
        except ValueError as e:
            with st.lock:
                st.measure = {"state": "error", "text": str(e)}
        except Exception:  # noqa: BLE001
            app.say("ERROR measuring the audio delay:\n" + traceback.format_exc())
            with st.lock:
                st.measure = {"state": "error", "text": "The delay couldn't be measured. Details were saved to debrief.log."}
    threading.Thread(target=run, name="debrief-voice-measure", daemon=True).start()
    return h._json({"ok": True, "pending": True})


def _import_reply(h, res, label):
    st = state(h.server)
    if res.get("ok"):
        st.import_note = f"Added {label}."
        app.say(f"Voice pack added: {res.get('id')}.")
        return h._json({"ok": True, "id": res.get("id"), "note": st.import_note,
                        "problems": (res.get("problems") or [])[:10]})
    probs = [str(p) for p in (res.get("problems") or ["It isn't a voice pack."])][:10]
    app.say("Voice pack refused: " + "; ".join(probs)[:300])
    return h._json({"ok": False, "error": "That voice pack can't be used.", "problems": probs}, 400)


def post_import_pack(h, body):
    from .. import voicepack
    folder = body.get("folder")
    if not isinstance(folder, str) or not folder.strip() or len(folder) > 400 or "\x00" in folder:
        raise ValueError("Type the folder that holds the voice pack.")
    p = Path(settings_io.expand_folder(folder.strip().strip('"')))
    if not p.is_dir():
        raise ValueError("That folder wasn't found.")
    res = voicepack.import_pack(p, packs_dir())
    return _import_reply(h, res, p.name)


def upload(h):
    """A voice pack .zip as the raw body (?name= is only shown). Checked by voicepack.import_pack in a private temp
    folder that is gone before the reply."""
    from .. import voicepack
    qs = {k: v[0] for k, v in parse_qs(urlsplit(h.path).query).items()}
    cl = (h.headers.get("Content-Length") or "").strip()
    name = qs.get("name") or ""

    def refuse(msg, code=400):
        h.close_connection = True
        return h._json({"ok": False, "error": msg}, code)
    if not cl.isdigit() or h.headers.get("Transfer-Encoding"):
        return refuse("Bad request.")
    n = int(cl)
    if n > voicepack.MAX_PACK_BYTES:
        return refuse("That file is far bigger than a voice pack.")
    if not ZIP_NAME_OK.match(name) or ".." in name:
        return refuse("Pick a voice pack (.zip).")
    d = Path(tempfile.mkdtemp(prefix="debrief-voicepack-"))
    try:
        p = d / "pack.zip"
        left = n
        with open(p, "wb") as f:
            while left:
                chunk = h.rfile.read(min(65536, left))
                if not chunk:
                    break
                f.write(chunk)
                left -= len(chunk)
        h._consumed = not left
        if left:
            h.close_connection = True
            return h._json({"ok": False, "error": "The file didn't arrive in full. Try again."}, 400)
        try:
            res = voicepack.import_pack(p, packs_dir())
        except Exception:  # noqa: BLE001
            app.say("ERROR adding a voice pack:\n" + traceback.format_exc())
            return h._json({"ok": False, "error": "Debrief couldn't read that file."}, 500)
        return _import_reply(h, res, Path(name).stem[:60])
    finally:
        shutil.rmtree(d, ignore_errors=True)


def post_repeat(h, body):
    r = runner_of(h.server)
    if r is None:
        raise ValueError("Start the coach first.")
    text = r.repeat()
    return h._json({"ok": True, "text": text, "said": bool(text)})


def post_quiet(h, body):
    r = runner_of(h.server)
    if r is None:
        raise ValueError("Start the coach first.")
    left = r.mute_for(QUIET_S)
    return h._json({"ok": True, "quiet_left": left})


def post_rehearse(h, body):
    """Play one of his Practice sessions through the whole coach (voice, device, ducking) at real speed."""
    from .server import SESSION_ID_OK
    sid = body.get("id")
    if not isinstance(sid, str) or not SESSION_ID_OK.match(sid):
        raise ValueError("Pick a session.")
    srv = h.server
    with srv.coach_lock:
        if srv.coach_starting or (srv.coach is not None and srv.coach.running()):
            raise ValueError("Stop the coach first. One at a time: live coaching or a rehearsal.")
    cfg = h._cfg()
    ss, idx = srv.practice_sessions(cfg)
    if idx is None:
        raise ValueError("Read your telemetry first.")
    me = next((x for x in ss if x.get("id") == sid), None)
    if me is None:
        raise ValueError("That session wasn't found. If it is new, press Read my telemetry.")
    paths = [p for p in (me.get("paths") or []) if Path(p).is_file()]
    if not paths:
        raise ValueError("This session's telemetry files have gone. Press Read my telemetry again.")
    title = f"{me.get('car') or '?'} at {me.get('track') or '?'}, {me.get('date') or ''}".strip(", ")
    st = state(srv)
    with st.lock:
        st.rehearsal = {"id": sid, "title": title, "state": "playing"}
    close_side(srv)
    res = srv.start_coach(source_factory=lambda: ChainPlayback(paths), mode="rehearsal",
                          log_header={"source": "rehearsal"})
    if not res.get("ok"):
        with st.lock:
            st.rehearsal = None
        return h._json(res, 400)
    app.say(f"Coach rehearsal started ({len(paths)} file(s)).")
    return h._json({**res, "status": srv.coach_status()})


def post_wheel_learn(h, body):
    w = _wheel(h.server)
    if not w.available():
        return h._json({"ok": False, "error": "Wheel buttons can't be read on this computer.",
                        "wheel": wheel_status(h.server)}, 400)
    state(h.server).wheel_note = ""
    if not w.learn(on_learned=lambda b: _wheel_learned(h.server, b)):
        return h._json({"ok": False, "error": "Wheel buttons can't be read on this computer.",
                        "wheel": wheel_status(h.server)}, 400)
    return h._json({"ok": True, "wheel": wheel_status(h.server)})


def post_wheel_clear(h, body):
    from .server import SETTINGS_WRITE_ERROR
    try:
        with h.server.coach_json_lock:
            data = settings_io.load_extra("coach")
            data.pop("wheel_repeat", None)
            settings_io.save_extra("coach", data)
    except OSError as e:
        app.say(f"ERROR clearing the wheel repeat button: {type(e).__name__}: {e}")
        raise ValueError(SETTINGS_WRITE_ERROR)
    st = state(h.server)
    with st.lock:
        w = st.wheel
    if w is not None:
        w.cancel_learn()
        w.set_binding(None)
    st.wheel_note = "Wheel button cleared."
    return h._json({"ok": True, "wheel": wheel_status(h.server)})


# ---------------------------------------------------------------- rehearsal source: his files, one after another
class ChainPlayback:
    """livecoach.IbtPlaybackSource over several files in order, at real speed. Files are opened on the coach's
    thread (on the first read), never on a request thread. progress() covers all of them."""

    def __init__(self, paths, speed: float = 1.0, rate_hz: float = 20.0):
        self.paths = [Path(p) for p in paths]
        self.speed, self.rate_hz = speed, rate_hz
        self.i = -1
        self.cur = None
        self.done = False
        self.state = "playing"
        self.realtime = True
        self.interval = 1.0 / rate_hz
        self._sizes = []
        for p in self.paths:
            try:
                self._sizes.append(max(1, p.stat().st_size))
            except OSError:
                self._sizes.append(1)

    def _next(self) -> bool:
        from .. import livecoach
        while True:
            if self.cur is not None:
                self.cur.close()
                self.cur = None
            self.i += 1
            if self.i >= len(self.paths):
                self.done, self.state = True, "finished"
                return False
            try:
                self.cur = livecoach.IbtPlaybackSource(self.paths[self.i], speed=self.speed, rate_hz=self.rate_hz)
                self.interval = self.cur.interval
                return True
            except Exception as e:  # noqa: BLE001
                app.say(f"Rehearsal: skipped a telemetry file ({e.__class__.__name__}).")

    def info(self):
        return self.cur.info() if self.cur is not None else None

    def read(self):
        if self.done:
            return None
        if self.cur is None and not self._next():
            return None
        s = self.cur.read()
        while s is None:
            if not self._next():
                return None
            s = self.cur.read()
        return s

    def progress(self) -> dict:
        tot = float(sum(self._sizes))
        before = float(sum(self._sizes[:max(0, self.i)]))
        frac = 0.0
        at = total = 0.0
        if self.cur is not None:
            p = self.cur.progress()
            frac, at, total = p["fraction"], p["at_s"], p["total_s"]
        f = 1.0 if self.done else (before + frac * self._sizes[self.i]) / tot if self.i >= 0 else 0.0
        return {"fraction": round(min(1.0, f), 4), "file": max(0, self.i) + 1, "files": len(self.paths),
                "at_s": at, "total_s": total}

    def close(self):
        if self.cur is not None:
            self.cur.close()
            self.cur = None
