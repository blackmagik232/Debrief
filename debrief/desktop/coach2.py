"""Debrief Coach 2.0 in the window's server: voice packs, the output device, the audio delay, the cue log,
rehearsal, "Repeat that" and "Mute for 1 minute" (debrief/coachvoice.py, audioout.py, cuelog.py,
voicepack.py). Nothing here changes iRacing's volume.

Kept in its own module so the server only needs a few hook lines in its coach code (make_voice, hotkeys, the wheel,
extra() for coach/status, and get()/post()/upload() for the routes below).

    GET  api/coach/voices                     voice packs, sound devices, the saved voice settings
    GET  api/coach/logs                       recent cue logs (newest first)
    GET  api/coach/log?name=[&summary=1]      one cue log's rows, or its "Copy for Claude" text
    POST api/coach/voice-settings {voice_pack, device}
    POST api/coach/preview                    play the chosen pack's sample through the chosen device
    POST api/coach/measure                    measure the audio delay (saved as audio_latency_s)
    POST api/coach/import-pack {folder}       add his own voice pack from a folder on this PC
    POST api/coach/import-pack-zip?name=      ... or from a .zip (the raw body)
    POST api/coach/repeat                     say the last cue again
    POST api/coach/quiet                      mute for 1 minute
    POST api/coach/rehearse {id}              play one of his Practice sessions through the coach, as if live
    POST api/coach/wheel-repeat-learn / wheel-repeat-clear   a wheel button for "Repeat that"
    POST api/coach/pace                       2.17.2 "How's my pace?": the coach answers when it is safe to talk
    POST api/coach/wheel-pace-learn / wheel-pace-clear       a wheel button for "How's my pace?"
    POST api/coach/mute-set {muted}            2.18: mute or unmute outright (the window's Mute button)
    GET  api/cornernotes                          2.20 corner notes by track and turn (+ trash, Copy summary text)
    GET  api/coach/mic                         2.20 the Mic test's state and live level
    POST api/coach/mic-test                    2.20 record 3 s and play it back
    POST api/coach/uselap                      2.20 "use this lap": the lap just finished becomes the reference
    POST api/coach/wheel-note-learn / -clear   a wheel button held to record a corner note
    POST api/coach/wheel-uselap-learn / -clear a wheel button for "use this lap"
    POST api/cornernotes/play|enable|move|delete|restore|rerecord|rerecord-stop   one note
    POST api/coach/rehearse-speed {speed}      2.18: a rehearsal at 1x, 2x or 4x
    POST api/coach/rehearse-skip {what}        2.18: a rehearsal skips to the next "lap" or "cue"
    GET  api/coach/result?car=&track=[&plan=]  2.18: for Practice: the coach's reference lap there, and what the
                                               coach counted for a plan's one thing
"""
from __future__ import annotations

import re
import shutil
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .. import app
from . import settings_io

GET_ROUTES = ("api/coach/voices", "api/coach/logs", "api/coach/log", "api/coach/result", "api/cornernotes", "api/coach/mic")
POST_ROUTES = ("api/coach/voice-settings", "api/coach/preview", "api/coach/measure", "api/coach/import-pack",
               "api/coach/repeat", "api/coach/quiet", "api/coach/rehearse", "api/coach/wheel-repeat-learn",
               "api/coach/wheel-repeat-clear", "api/coach/pace", "api/coach/wheel-pace-learn",
               "api/coach/wheel-pace-clear", "api/coach/mute-set", "api/coach/rehearse-speed",
               "api/coach/rehearse-skip", "api/overlay/set", "api/coach/mic-test", "api/coach/uselap",
               "api/coach/wheel-note-learn", "api/coach/wheel-note-clear", "api/coach/wheel-uselap-learn",
               "api/coach/wheel-uselap-clear", "api/cornernotes/play", "api/cornernotes/enable", "api/cornernotes/move",
               "api/cornernotes/delete", "api/cornernotes/restore", "api/cornernotes/rerecord", "api/cornernotes/rerecord-stop")
UPLOAD_ROUTE = "api/coach/import-pack-zip"
QUIET_S = 60.0
REPEAT_NOTE_S = 12.0             # 2.18: "Repeat that: not said, the coach is muted" stays on the page this long
SIDE_IDLE_S = 30.0               # a voice made for Preview / Measure (coach off) is closed after this long unused
PACK_ID_OK = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,39}$")
ZIP_NAME_OK = re.compile(r"^[^\x00-\x1f/\\<>:\"|?*]{1,160}\.zip$", re.I)
HOTKEY_LABELS = {"repeat": "Ctrl+Shift+R", "quiet": "Ctrl+Shift+N", "pace": "Ctrl+Shift+P", "note": "Ctrl+Shift+V",
                 "uselap": "Ctrl+Shift+L"}
# wheel buttons (role -> saved key in coach.json, what the button does in plain words, what it calls on the runner)
WHEEL_ROLES = {"repeat": ("wheel_repeat", "It says the last cue again.", "repeat"),
               "pace": ("wheel_pace", "It asks the coach for your pace.", "ask_pace"),
               "note": ("wheel_note", "Hold it to record a corner note; let go to save it.", "note_press"),
               "uselap": ("wheel_uselap", "It makes the lap you just drove your reference lap, if it was clean.",
                          "use_last_lap")}
HOLD_ROLES = {"note": "note_release"}      # 2.20 push-to-talk: the runner call for the button going up


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


EXTRA_FIELDS = {"voice_pack": clean_pack_id, "device": clean_device,
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
        self.speed = 1.0              # 2.18: the rehearsal speed picked (1x, 2x or 4x)
        self.wheels = {}              # role -> wheelmute.WheelMute ("repeat": Repeat that, "pace": How's my pace?)
        self.wheel_notes = {}         # role -> a note for the window ("Saved: ...")
        self.mictest = None           # 2.20 noterec.MicTest (made on the first Mic test)
        self.rerec = None             # 2.20 {"id", "rec", "wall"} while a note is being re-recorded
        self.rerec_note = ""


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
    """The coach's system-wide keys (mute, repeat, quiet for a minute, pace), each on its own thread (livecoach.Hotkey).
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
    return HotkeyGroup({"mute": livecoach.Hotkey(settings.hotkey, lambda: runner.toggle(by="Ctrl+Shift+M"),
                                                 log=app.say),
                        "repeat": livecoach.Hotkey(settings.repeat_hotkey, runner.repeat, log=app.say),
                        "quiet": livecoach.Hotkey(settings.quiet_hotkey, lambda: runner.mute_for(QUIET_S),
                                                  log=app.say),
                        "pace": livecoach.Hotkey(settings.pace_hotkey, runner.ask_pace, log=app.say),
                        "note": livecoach.Hotkey(settings.note_hotkey, lambda: runner.note_press(), log=app.say,
                                                 on_release=lambda: runner.note_release()),
                        "uselap": livecoach.Hotkey(settings.use_lap_hotkey, lambda: runner.use_last_lap(),
                                                   log=app.say)})


def runner_of(server):
    with server.coach_lock:
        r = server.coach
    return r if r is not None and r.running() else None


# ---------------------------------------------------------------- wheel buttons: "Repeat that" and "How's my pace?"
def _wheel_saved(role="repeat"):
    from .. import wheelmute
    return wheelmute.clean_binding(settings_io.load_extra("coach").get(WHEEL_ROLES[role][0]))


def _wheel_press(server, role="repeat"):
    r = runner_of(server)
    if r is not None:
        getattr(r, WHEEL_ROLES[role][2])()


def _wheel_release(server, role="note"):
    """2.20: the held button went up (or the wheel was unplugged): the note is saved. Works while the runner is
    stopping too, so the microphone is never left open."""
    with server.coach_lock:
        r = server.coach
    fn = getattr(r, HOLD_ROLES[role], None) if r is not None else None
    if fn is not None:
        fn()


def _wheel(server, role="repeat"):
    from .. import wheelmute
    st = state(server)
    with st.lock:
        if st.wheels.get(role) is None:
            f = getattr(server, "wheel_backend_factory", None)
            rel = (lambda: _wheel_release(server, role)) if role in HOLD_ROLES else None
            st.wheels[role] = wheelmute.WheelMute(on_press=lambda: _wheel_press(server, role),
                                                  binding=_wheel_saved(role), backend=f() if f else None,
                                                  log=app.say, on_release=rel)
        return st.wheels[role]


def start_wheel(server):
    for role in WHEEL_ROLES:
        try:
            b = _wheel_saved(role)
            if b is None:
                continue
            w = _wheel(server, role)
            w.set_binding(b)
            w.start()
        except Exception:  # noqa: BLE001
            app.say(f"ERROR starting the wheel {role} button:\n" + traceback.format_exc())


def stop_wheel(server):
    st = state(server)
    with st.lock:
        ws = [w for w in st.wheels.values() if w is not None]
    for w in ws:
        try:
            w.stop()
        except Exception:  # noqa: BLE001
            pass


def wheel_status(server, role="repeat") -> dict:
    b = _wheel_saved(role)
    st = state(server)
    with st.lock:
        w = st.wheels.get(role)
    s = w.status() if w is not None else {"state": "off", "text": ""}
    state_ = s.get("state")
    if state_ in ("off", "not_set") or (state_ == "ready" and not b):
        text = (f"{b['label']}. {WHEEL_ROLES[role][1]}" if b else "No wheel button chosen yet.")
        state_ = "saved" if b else "not_set"
    else:
        text = s.get("text") or ""
    return {"state": state_, "text": text, "label": b["label"] if b else None, "note": st.wheel_notes.get(role, "")}


def _wheel_learned(server, binding, role="repeat"):
    st = state(server)
    if binding:
        try:
            with server.coach_json_lock:
                data = settings_io.load_extra("coach")
                data[WHEEL_ROLES[role][0]] = binding
                settings_io.save_extra("coach", data)
            st.wheel_notes[role] = f"Saved: {binding.get('label')}."
        except OSError as e:
            app.say(f"ERROR saving the wheel {role} button: {type(e).__name__}: {e}")
            st.wheel_notes[role] = "The button couldn't be saved. Details were saved to debrief.log."
    else:
        st.wheel_notes[role] = "No button was pressed. Try again and press the button within 15 seconds."
    with server.coach_lock:
        run = server.coach_starting or (server.coach is not None and server.coach.running())
    with st.lock:
        w = st.wheels.get(role)
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


def close_mic(server):
    """Debrief closing or the coach stopping: nothing keeps the microphone open."""
    st = state(server)
    with st.lock:
        mt, rr, st.rerec = st.mictest, st.rerec, None
    if mt is not None:
        try:
            mt.cancel()
        except Exception:  # noqa: BLE001
            pass
    if rr is not None:
        try:
            rr["rec"].cancel()
        except Exception:  # noqa: BLE001
            pass


def on_shutdown(server):
    stop_wheel(server)
    close_mic(server)
    ov = getattr(server, "_overlay", None)
    if ov is not None:
        try:
            ov.stop(wait_s=1.0)          # closing Debrief closes it (the thread is a daemon either way)
        except Exception:  # noqa: BLE001
            pass
    close_side(server)


# ---------------------------------------------------------------- coach/status extras
def extra(server, st: dict):
    s2 = state(server)
    with server.coach_lock:
        hk = server.coach_hotkey
    keys = hk.status() if isinstance(hk, HotkeyGroup) else {}
    st["keys"] = {name: {"key": HOTKEY_LABELS.get(name, "Ctrl+Shift+M"), "on": bool(keys.get(name))}
                  for name in ("repeat", "quiet", "pace", "note", "uselap")}
    st["mic"] = mic_status(server, st)
    with s2.lock:
        st["measure"] = dict(s2.measure) if s2.measure else None
        pv = dict(s2.preview) if s2.preview else None
        st["rehearsal"] = dict(s2.rehearsal) if s2.rehearsal else None
    if pv and pv.get("state") == "playing" and (pv.get("until") or 0) <= time.monotonic():
        pv["state"] = "done"
    if pv:
        pv.pop("until", None)
    st["preview"] = pv
    for role in WHEEL_ROLES:
        try:
            st[WHEEL_ROLES[role][0]] = wheel_status(server, role)
        except Exception:  # noqa: BLE001
            st[WHEEL_ROLES[role][0]] = {"state": "error", "text": "The wheel button couldn't be read."}
    if st.get("mode") != "rehearsal" and st.get("running"):
        with s2.lock:
            if s2.rehearsal and s2.rehearsal.get("state") == "playing":
                s2.rehearsal["state"] = "stopped"
    with s2.lock:
        st["rehearse_speed"] = s2.speed
    # 2.18: the voice he chose, and if it isn't the one playing, why and what plays instead
    try:
        st["voice_note"] = voice_note_for(server, st.get("voice") if st.get("running") else None)
    except Exception:  # noqa: BLE001
        st["voice_note"] = {"ok": True, "text": "", "using": ""}
    st["overlay"] = overlay_status(server)       # 2.19: the coach overlay (off unless he turned it on)
    rn = st.get("repeat_note")
    if isinstance(rn, dict) and time.time() - float(rn.get("wall") or 0) > REPEAT_NOTE_S:
        st["repeat_note"] = None            # shown for a few seconds after the press, then gone


# ---------------------------------------------------------------- 2.20 corner notes: store, microphone, Mic test
_NOTES_LOCK = threading.Lock()


def notes_store(server):
    """The one notes folder (<data folder>/notes). Made on first use; never touches iRacing's folders."""
    from .. import cornernotes
    with _NOTES_LOCK:
        ns = getattr(server, "_notes_store", None)
        if ns is None:
            ns = cornernotes.NoteStore(settings_dir() / cornernotes.FOLDER, log=app.say)
            server._notes_store = ns
        return ns


def mic_backend(server):
    """The microphone: the test stand-in when set (tests), else Windows' waveIn (None elsewhere)."""
    f = getattr(server, "mic_backend_factory", None)
    if f:
        return f()
    from .. import audioin
    return audioin.default_backend()


def mic_backend_fn(server):
    return lambda: mic_backend(server)


def mic_test(server):
    from .. import noterec
    st = state(server)
    with st.lock:
        if st.mictest is None:
            st.mictest = noterec.MicTest(lambda: mic_backend(server), lambda: _side_voice(server), log=app.say)
        return st.mictest


def mic_status(server, coach_st: dict | None = None) -> dict:
    """The Recording light and the meter: the microphone is in use for a held note, the Mic test or a re-record."""
    st = state(server)
    with st.lock:
        mt, rr = st.mictest, st.rerec
        rr_note = st.rerec_note
    t = mt.status() if mt is not None else {"state": "idle", "text": "", "level": 0.0, "recording": False}
    note = (coach_st or {}).get("note") or None
    held = bool(note and note.get("recording"))
    rec_on = bool(held or t.get("recording") or rr is not None)
    level = max(float(t.get("level") or 0.0), float((note or {}).get("level") or 0.0),
                float(rr["rec"].level) if rr is not None else 0.0)
    return {"recording": rec_on, "level": round(level, 3), "test": t, "note": (note or {}).get("last"),
            "rerecording": rr["id"] if rr is not None else None, "rerecord_note": rr_note,
            "available": getattr(server, "mic_backend_factory", None) is not None or sys.platform == "win32"}


def _note_view(n: dict) -> dict:
    from .. import cornernotes
    return {"id": n["id"], "turn": n.get("turn"), "lap": n.get("lap"), "created": n.get("created"),
            "seconds": n.get("seconds"), "played": int(n.get("played") or 0), "passes": int(n.get("passes") or 0),
            "enabled": bool(n.get("enabled", True)), "where": n.get("where") or "", "car": n.get("car") or "",
            "session": n.get("session") or "", "deleted": n.get("deleted"),
            "plan": cornernotes.plan_text(int(n.get("passes") or 0)), "line": cornernotes.summary_line(n)}


def get_notes(h, q):
    """Corner notes by track, then turn; the trash (30 days); the Copy summary text."""
    from .. import cornernotes
    store = notes_store(h.server)
    store.purge()
    tracks = [{"track_key": g["track_key"], "track": g["track"], "short": cornernotes.short_track(g["track"]),
               "turns": [{"turn": t["turn"], "notes": [_note_view(n) for n in t["notes"]]} for t in g["turns"]]}
              for g in store.grouped()]
    trash = [dict(_note_view(n), track=cornernotes.short_track(n.get("track"))) for n in store.trash()]
    return h._json({"ok": True, "tracks": tracks, "trash": trash, "summary": cornernotes.summary(store.all()),
                    "trash_days": cornernotes.TRASH_DAYS, "folder": str(store.root),
                    "mic": mic_status(h.server, _runner_status(h.server))})


def _runner_status(server):
    r = runner_of(server)
    try:
        return r.status() if r is not None else None
    except Exception:  # noqa: BLE001
        return None


def get_mic(h, q):
    return h._json({"ok": True, "mic": mic_status(h.server, _runner_status(h.server))})


def _mic_free(server) -> str | None:
    """Why the microphone can't be used now (one use at a time), or None."""
    r = runner_of(server)
    nr = getattr(r, "noterec", None) if r is not None else None
    if nr is not None and nr.recording:
        return "A corner note is being recorded."
    st = state(server)
    with st.lock:
        if st.rerec is not None:
            return "A note is being re-recorded. Stop that first."
        mt = st.mictest
    if mt is not None and mt.recording:
        return "The Mic test is recording."
    return None


def post_mic_test(h, body):
    why = _mic_free(h.server)
    if why:
        raise ValueError(why)
    if mic_backend(h.server) is None:
        raise ValueError("The microphone can only be used on Windows.")
    mt = mic_test(h.server)
    if not mt.start():
        raise ValueError(mt.status().get("text") or "The Mic test couldn't start.")
    return h._json({"ok": True, "mic": mic_status(h.server, _runner_status(h.server))})


def post_uselap(h, body):
    r = runner_of(h.server)
    if r is None:
        raise ValueError("Start the coach first.")
    return h._json({"ok": True, "asked": bool(r.use_last_lap())})


def _note_id(body) -> str:
    nid = body.get("id")
    if not isinstance(nid, str) or not 1 <= len(nid) <= 80:
        raise ValueError("Pick a note.")
    return nid


def _note_call(fn, *a):
    try:
        return fn(*a)
    except KeyError as e:
        raise ValueError(str(e.args[0]) if e.args else "That note wasn't found.")


def post_note_play(h, body):
    nid = _note_id(body)
    store = notes_store(h.server)
    got = store.pcm(nid)
    if got is None:
        raise ValueError("That note's sound couldn't be read.")
    v = _side_voice(h.server)
    fn = getattr(v, "play_clip", None)
    if fn is None or not fn("(note)", got[0], got[1], priority=9):
        raise ValueError("The note couldn't be played through the voice's sound device.")
    return h._json({"ok": True})


def post_note_enable(h, body):
    on = body.get("on")
    if not isinstance(on, bool):
        raise ValueError("Bad request.")
    n = _note_call(notes_store(h.server).set_enabled, _note_id(body), on)
    return h._json({"ok": True, "note": _note_view(n)})


def post_note_move(h, body):
    t = body.get("turn")
    if isinstance(t, bool) or not isinstance(t, int):
        raise ValueError("Pick a turn number.")
    n = _note_call(notes_store(h.server).move, _note_id(body), t)
    return h._json({"ok": True, "note": _note_view(n)})


def post_note_delete(h, body):
    n = _note_call(notes_store(h.server).delete, _note_id(body))
    return h._json({"ok": True, "note": _note_view(n)})


def post_note_restore(h, body):
    n = _note_call(notes_store(h.server).restore, _note_id(body))
    return h._json({"ok": True, "note": _note_view(n)})


def post_note_rerecord(h, body):
    """Re-record: the microphone opens now (a rising tone) and records until Stop (or 8 s); the note keeps its turn."""
    from .. import audioin, cornernotes
    nid = _note_id(body)
    if notes_store(h.server).get(nid) is None:
        raise ValueError("That note wasn't found.")
    why = _mic_free(h.server)
    if why:
        raise ValueError(why)
    be = mic_backend(h.server)
    if be is None:
        raise ValueError("The microphone can only be used on Windows.")
    st = state(h.server)
    rec = audioin.Recorder(be, skip_s=cornernotes.TONE_S, on_limit=lambda: _rerecord_finish(h.server), log=app.say)
    if not rec.start():
        raise ValueError(rec.error or "The microphone could not be opened.")
    with st.lock:
        st.rerec = {"id": nid, "rec": rec, "wall": time.time()}
        st.rerec_note = "Recording. Say your note, then press Stop."
    try:
        v = _side_voice(h.server)
        if hasattr(v, "play_tone"):
            v.play_tone("start")
    except Exception:  # noqa: BLE001
        pass
    return h._json({"ok": True, "mic": mic_status(h.server, _runner_status(h.server))})


def _rerecord_finish(server) -> dict:
    from .. import cornernotes
    st = state(server)
    with st.lock:
        rr, st.rerec = st.rerec, None
    if rr is None:
        return {"ok": False, "text": "Nothing was being recorded."}
    take = rr["rec"].stop()
    tone, ok, text = "discard", False, "Note not kept: nothing was recorded."
    if take is not None:
        c = cornernotes.clean(take.pcm, take.rate)
        if c.ok:
            try:
                notes_store(server).replace_audio(rr["id"], c.pcm, c.rate)
                tone, ok, text = "saved", True, "Re-recorded. It starts its learning plan again."
            except (KeyError, ValueError, OSError):
                text = "The note couldn't be saved. Details were saved to debrief.log."
        else:
            text = "Note not kept: " + ("it was under 0.4 seconds." if c.why == "too short" else "no speech was heard.")
    with st.lock:
        st.rerec_note = text
    try:
        v = _side_voice(server)
        if hasattr(v, "play_tone"):
            v.play_tone(tone)
    except Exception:  # noqa: BLE001
        pass
    return {"ok": ok, "text": text}


def post_note_rerecord_stop(h, body):
    r = _rerecord_finish(h.server)
    return h._json({"ok": True, "result": r, "mic": mic_status(h.server, _runner_status(h.server))})


# ---------------------------------------------------------------- 2.19 the coach overlay
_OVERLAY_LOCK = threading.Lock()


def overlay_of(server):
    """The server's one overlay controller. Creating it makes no window and imports no Tk."""
    from .. import overlay
    with _OVERLAY_LOCK:
        ov = getattr(server, "_overlay", None)
        if ov is None:
            store = overlay.Store(lambda: settings_io.load_extra("overlay"), lambda d: settings_io.save_extra("overlay", d))
            ov = overlay.Overlay(store, lambda: _overlay_status_source(server), settings_io.units, log=app.say)
            server._overlay = ov
            server._overlay_autostarted = False
        return ov


def _overlay_status_source(server):
    """The overlay reads the same status the Coach page polls (LiveRunner.status), or None when no coach runs."""
    r = runner_of(server)
    return r.status() if r is not None else None


def overlay_autostart(server):
    """Once per run of Debrief, shows the overlay if he left it turned on. Never raises."""
    try:
        ov = overlay_of(server)
        if not server._overlay_autostarted:
            server._overlay_autostarted = True
            if ov.store.enabled() and not server.stopping.is_set():
                # on its own thread: looking for Tk and opening the window never holds up Debrief starting
                threading.Thread(target=_overlay_autostart_run, args=(ov,), name="overlay-autostart", daemon=True).start()
    except Exception:  # noqa: BLE001  (the overlay never gets in the coach's way)
        app.say("ERROR starting the coach overlay:\n" + traceback.format_exc())


def _overlay_autostart_run(ov):
    try:
        ov.start(wait_s=0.0)
    except Exception:  # noqa: BLE001
        app.say("ERROR starting the coach overlay:\n" + traceback.format_exc())


def overlay_status(server) -> dict:
    overlay_autostart(server)
    return overlay_of(server).status()


def post_overlay(h, body):
    """2.19: Show overlay on/off, Edit position on/off, opacity and size. Off by default; one choice per call."""
    from .. import overlay
    ov = overlay_of(h.server)
    allowed = {"enabled", "edit", "opacity", "size", "preset", "nudge", "big", "reset", "test"}
    if not body or not set(body) <= allowed:
        raise ValueError("Bad request.")
    for k in ("enabled", "edit", "big", "reset", "test"):
        if k in body and not isinstance(body[k], bool):
            raise ValueError("Bad request.")
    if "big" in body and "nudge" not in body:
        raise ValueError("Bad request.")
    try:
        if "enabled" in body:
            if body["enabled"]:
                st = ov.start()
                if st["available"] and st["why"] == "":
                    ov.store.set_enabled(True)
            else:
                ov.store.set_enabled(False)
                ov.command(edit=False)
                ov.stop()
        if "edit" in body:
            if body["edit"] and not ov.running():
                raise ValueError("Show the overlay first.")
            ov.command(edit=body["edit"])
        if "opacity" in body:
            op = body["opacity"]
            if isinstance(op, bool) or not isinstance(op, (int, float)) or not overlay.MIN_OPACITY <= op <= overlay.MAX_OPACITY:
                raise ValueError("Opacity is 30% to 100%.")
            if not ov.running():
                raise ValueError("Show the overlay first.")
            ov.command(opacity=float(op))
        if "size" in body:
            if body["size"] not in overlay.SIZES:
                raise ValueError("Pick Small, Medium or Large.")
            if not ov.running():
                raise ValueError("Show the overlay first.")
            ov.command(size=body["size"])
        if "preset" in body:
            if body["preset"] not in overlay.PRESETS:
                raise ValueError("Pick one of the nine spots.")
            if not ov.running():
                raise ValueError("Show the overlay first.")
            ov.command(preset=body["preset"])
        if "nudge" in body:
            if body["nudge"] not in overlay.DIRECTIONS:
                raise ValueError("Pick left, right, up or down.")
            if not ov.running():
                raise ValueError("Show the overlay first.")
            ov.command(nudge=body["nudge"], big=bool(body.get("big")))
        if body.get("reset"):
            if not ov.running():
                raise ValueError("Show the overlay first.")
            ov.command(reset=True)
        if body.get("test"):
            st = ov.test(close_after=not ov.store.enabled())
            if not ov.running():
                raise ValueError(st.get("problem") or "The overlay didn't open.")
    except OSError as e:
        app.say(f"ERROR saving the overlay settings: {type(e).__name__}: {e}")
        raise ValueError("The overlay setting couldn't be saved. Details were saved to debrief.log.")
    return h._json({"ok": True, "overlay": ov.status()})


def voice_note_for(server, voice_status=None) -> dict:
    """coachvoice.voice_note for the saved voice: the running voice's status, or (coach off) the installed packs."""
    from .. import coachvoice, voicepack
    pack = server.coach_settings().voice_pack
    return coachvoice.voice_note(pack, voice_status, voicepack.list_packs(packs_dir()))


# ---------------------------------------------------------------- GET
def get(h, r, q):
    return {"api/coach/voices": get_voices, "api/coach/logs": get_logs, "api/coach/log": get_log,
            "api/coach/result": get_result, "api/cornernotes": get_notes, "api/coach/mic": get_mic}[r](h, q)


def get_result(h, q):
    """2.18 for the Practice session page: the lap the coach starts from for this car + track (named as the Practice
    list names it: livecoach.reference_choice over Setup Lab's index, no file opened), and what the live coach counted
    for the plan's one thing (cuelog.focus_results), with what counts as done."""
    from .. import cuelog, livecoach
    from .server import setuplab_cache_path
    car, track, plan = (q.get("car") or "")[:200], (q.get("track") or "")[:200], (q.get("plan") or "")[:120]
    if not car or not track:
        raise ValueError("Bad request.")
    ref = None
    try:
        idx, _k = h.server.setuplab.load(setuplab_cache_path(h._cfg()))
        ref = livecoach.reference_choice(idx, car, track) if idx else None
    except (OSError, SystemExit, KeyError, ValueError):
        ref = None
    res = cuelog.focus_results(logs_dir()).get(plan) if plan else None
    need, of = livecoach.done_rule()
    out = {"ok": True, "reference": ref, "done_text": f"Done when you hit it on {need} of {of} laps", "result": None}
    if isinstance(res, dict) and str(res.get("car") or "").lower() == car.lower() and \
            str(res.get("track") or "").lower() == track.lower():
        hits, n = int(res.get("hits") or 0), int(res.get("of") or 0)
        out["result"] = {"hits": hits, "of": n, "when": res.get("when"), "corner": res.get("corner"),
                         "text": f"While coaching, you hit it on {hits} of {n} lap{'s' if n != 1 else ''}."}
    return h._json(out)


def get_voices(h, q):
    from .. import audioout, coachvoice, voicepack
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
        "settings": {"voice_pack": s.voice_pack, "device": s.device,
                     "audio_latency_s": s.audio_latency_s, "rate": s.rate, "volume": s.volume},
        "user_dir": str(packs_dir()), "can_choose_device": bool(devs) or _test_backend(h.server) is not None,
        "chosen_note": coachvoice.voice_note(s.voice_pack, None, voicepack.list_packs(packs_dir())),
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
          "api/coach/wheel-repeat-learn": post_wheel_learn, "api/coach/wheel-repeat-clear": post_wheel_clear,
          "api/coach/pace": post_pace, "api/coach/wheel-pace-learn": lambda h, b: post_wheel_learn(h, b, "pace"),
          "api/coach/wheel-pace-clear": lambda h, b: post_wheel_clear(h, b, "pace"),
          "api/coach/mute-set": post_mute_set, "api/coach/rehearse-speed": post_rehearse_speed,
          "api/coach/rehearse-skip": post_rehearse_skip, "api/overlay/set": post_overlay,
          "api/coach/mic-test": post_mic_test, "api/coach/uselap": post_uselap,
          "api/coach/wheel-note-learn": lambda h, b: post_wheel_learn(h, b, "note"),
          "api/coach/wheel-note-clear": lambda h, b: post_wheel_clear(h, b, "note"),
          "api/coach/wheel-uselap-learn": lambda h, b: post_wheel_learn(h, b, "uselap"),
          "api/coach/wheel-uselap-clear": lambda h, b: post_wheel_clear(h, b, "uselap"),
          "api/cornernotes/play": post_note_play, "api/cornernotes/enable": post_note_enable, "api/cornernotes/move": post_note_move,
          "api/cornernotes/delete": post_note_delete, "api/cornernotes/restore": post_note_restore,
          "api/cornernotes/rerecord": post_note_rerecord, "api/cornernotes/rerecord-stop": post_note_rerecord_stop}[r]
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
    if not isinstance(raw, dict) or not set(raw) <= {"voice_pack", "device"}:
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
        if "voice_pack" in new:
            note = "The coach uses the new voice the next time it starts."
    close_side(h.server)
    return h._json({"ok": True, "settings": {k: keep[k] for k in ("voice_pack", "device", "audio_latency_s")}, "note": note})


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
                    note = ""
                    try:
                        note = voice_note_for(h.server, v.status() or {}).get("text") or ""
                    except Exception:  # noqa: BLE001
                        pass
                    st.preview = {"state": "error", "text": note or ("No sample to play" +
                                                                     (f": {reason}" if reason else "."))}
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
    note = getattr(r, "repeat_note", None) if not text else None
    return h._json({"ok": True, "text": text, "said": bool(text), "muted": bool(note),
                    "note": (note or {}).get("text") or ""})


def post_mute_set(h, body):
    """2.18: Mute / Unmute as the button says it (never a toggle, so its first press always mutes)."""
    on = body.get("muted")
    if not isinstance(on, bool):
        raise ValueError("Bad request.")
    r = runner_of(h.server)
    if r is None:
        raise ValueError("Start the coach first.")
    muted = bool(r.set_muted(on, by="the Mute button" if on else "the Unmute button"))
    return h._json({"ok": True, "muted": muted, "mute": r.mute_state()})


def post_rehearse_speed(h, body):
    """2.18: 1x, 2x or 4x, for the rehearsal playing now and the next one."""
    sp = body.get("speed")
    if isinstance(sp, bool) or not isinstance(sp, (int, float)) or float(sp) not in (1.0, 2.0, 4.0):
        raise ValueError("Pick 1x, 2x or 4x.")
    st = state(h.server)
    with st.lock:
        st.speed = float(sp)
    r = runner_of(h.server)
    if r is not None and getattr(r, "mode", "") == "rehearsal":
        r.set_speed(float(sp))
    return h._json({"ok": True, "speed": float(sp)})


def post_rehearse_skip(h, body):
    """2.18: the rehearsal skips to the next lap or the next cue (nothing is said on the way)."""
    what = body.get("what")
    if what not in ("lap", "cue"):
        raise ValueError("Bad request.")
    r = runner_of(h.server)
    if r is None or getattr(r, "mode", "") != "rehearsal":
        raise ValueError("Start a rehearsal first.")
    if not r.skip(what):
        raise ValueError("The rehearsal can't skip right now.")
    return h._json({"ok": True, "what": what})


def post_pace(h, body):
    """2.17.2 "How's my pace?": the coach answers on the car's next safe moment (not at once, never under braking)."""
    r = runner_of(h.server)
    if r is None:
        raise ValueError("Start the coach first.")
    return h._json({"ok": True, "asked": bool(r.ask_pace())})


def post_quiet(h, body):
    r = runner_of(h.server)
    if r is None:
        raise ValueError("Start the coach first.")
    left = r.mute_for(QUIET_S)
    return h._json({"ok": True, "quiet_left": left})


def post_rehearse(h, body):
    """Play one of his Practice sessions through the whole coach (voice, device), at the speed picked (2.18: 1x,
    2x or 4x)."""
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
    with st.lock:
        speed = st.speed
    res = srv.start_coach(source_factory=lambda: ChainPlayback(paths, speed=speed), mode="rehearsal",
                          log_header={"source": "rehearsal"})
    if not res.get("ok"):
        with st.lock:
            st.rehearsal = None
        return h._json(res, 400)
    r = runner_of(srv)
    if r is not None and speed != 1.0:
        r.set_speed(speed)                   # the coach times its cues for this speed
    app.say(f"Coach rehearsal started ({len(paths)} file(s)).")
    return h._json({**res, "status": srv.coach_status()})


def post_wheel_learn(h, body, role="repeat"):
    w = _wheel(h.server, role)
    if not w.available():
        return h._json({"ok": False, "error": "Wheel buttons can't be read on this computer.",
                        "wheel": wheel_status(h.server, role)}, 400)
    state(h.server).wheel_notes[role] = ""
    if not w.learn(on_learned=lambda b: _wheel_learned(h.server, b, role)):
        return h._json({"ok": False, "error": "Wheel buttons can't be read on this computer.",
                        "wheel": wheel_status(h.server, role)}, 400)
    return h._json({"ok": True, "wheel": wheel_status(h.server, role)})


def post_wheel_clear(h, body, role="repeat"):
    from .server import SETTINGS_WRITE_ERROR
    try:
        with h.server.coach_json_lock:
            data = settings_io.load_extra("coach")
            data.pop(WHEEL_ROLES[role][0], None)
            settings_io.save_extra("coach", data)
    except OSError as e:
        app.say(f"ERROR clearing the wheel {role} button: {type(e).__name__}: {e}")
        raise ValueError(SETTINGS_WRITE_ERROR)
    st = state(h.server)
    with st.lock:
        w = st.wheels.get(role)
    if w is not None:
        w.cancel_learn()
        w.set_binding(None)
    st.wheel_notes[role] = "Wheel button cleared."
    return h._json({"ok": True, "wheel": wheel_status(h.server, role)})


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
        self.interval = (1.0 / rate_hz) / speed if speed > 0 else 0.0
        self._sizes = []
        for p in self.paths:
            try:
                self._sizes.append(max(1, p.stat().st_size))
            except OSError:
                self._sizes.append(1)

    def set_speed(self, speed: float):
        """2.18: 1x, 2x or 4x from now on (this file and the ones after it)."""
        self.speed = float(speed)
        if self.cur is not None:
            self.cur.set_speed(self.speed)
            self.interval = self.cur.interval
        else:
            self.interval = (1.0 / self.rate_hz) / self.speed if self.speed > 0 else 0.0

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
