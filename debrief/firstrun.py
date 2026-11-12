"""First-run walkthrough (2.14), logic only: what is set up already, and the next thing to do, in plain words.

    steps(facts)               -> [{"id", "done", "title", "text", "action"}] from plain facts (pure, for tests)
    gather(...)                -> facts read from Debrief's own files (settings, Setup Lab's index, the state file)
    walkthrough(...)           -> {"steps", "done", "total", "next", "all_done", "dismissed", "show"} for the window
    mark(name, path=None)      -> remember "voice_tested" (or "dismissed") in firstrun.json next to settings.toml
    dismiss(path=None) / undismiss(path=None)

Steps (in order; 2.18: the telemetry steps come first, because Practice, Setup Lab, Coach and Racing need only
the telemetry iRacing saves on this PC; Garage 61 is last and only race reviews, history and team wait for it):
    folder      iRacing's telemetry folder is there                 (settings_io.telemetry_folder)
    record      iRacing has saved telemetry (.ibt) files in it      (counted in the folder; iRacing records nothing
                                                                    until it is turned on: Alt+L in the car)
    telemetry   Debrief has read at least one telemetry file        (Setup Lab's index: <cache>/setuplab-index.json)
    session     one session with at least MIN_CLEAN clean laps      (sessionibt.sessions over that index)
    voice       the coach's voice has been tested                   (firstrun.json "voice_tested", marked by the
                                                                    server when Test voice is heard)
    token       a Garage 61 personal access token is saved and      (settings.toml, settings_io.token_present;
                Garage 61 didn't reject it at the last check        firstrun.json "token_rejected")
"action" is what the window's button for the step does: "settings_token", "settings_folder", "read_telemetry",
"practice", "test_voice" (None when the step is done, for "record", which is done in iRacing, and for "telemetry"
while the folder has no .ibt files to read). Nothing here raises; a damaged state file counts as empty.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from . import fsutil

_LOCK = threading.Lock()                   # mark(): one read-change-write of the state file at a time

STATE_FILE = "firstrun.json"
SETUPLAB_CACHE = "setuplab-index.json"     # the server's SETUPLAB_CACHE, in the cache folder
MIN_CLEAN = 10                             # practiceplan.WANT_LAPS: the clean laps Practice needs to pick one thing
MAX_COUNT = 100000                         # .ibt files counted in the folder at most (a quick look, not a read)
FLAGS = ("voice_tested", "dismissed", "token_rejected")

# 2.33: the coach's voice test is no longer a setup step (the coach is optional; Coach > Settings > Test voice stays)
STEP_IDS = ("folder", "record", "telemetry", "session", "token")
SKIPPED_KEY = "skipped"                    # 2.19.2: firstrun.json "skipped": [step ids he chose to skip]
TELEMETRY_HOW = ("iRacing doesn't save telemetry until you turn it on: click Turn it on for me and Debrief does it each "
                 "time you get in the car (or press Alt+L in the car yourself), then drive a few laps.")
TELEMETRY_AUTO_ON = ("Debrief turns on iRacing's telemetry each time you get in the car. Drive a few laps and the files "
                     "appear here.")


def steps(facts: dict) -> list:
    """The walkthrough's steps from facts: {"token": bool, "token_rejected": bool, "folder": bool, "folder_path": str,
    "ibt_files": int (.ibt files in the folder), "files": int (telemetry files read), "best_clean": int (most clean
    laps in one session), "voice_tested": bool}."""
    f = facts if isinstance(facts, dict) else {}
    files = int(f.get("files") or 0)
    best = int(f.get("best_clean") or 0)
    folder = str(f.get("folder_path") or "")
    out = []
    skipped = {x for x in (f.get("skipped") or []) if isinstance(x, str)}

    def add(sid, done, title, todo, ok, action, detail=""):
        out.append({"id": sid, "done": bool(done), "title": title, "text": ok if done else todo,
                    "action": None if done else action, "skipped": bool(not done and sid in skipped),
                    "detail": "" if done else detail})   # 2.33: e.g. the folder looked in, shown behind "Why?"

    ibt = int(f.get("ibt_files") or 0)
    add("folder", f.get("folder"), "Find your iRacing telemetry",
        "Debrief can't find your iRacing telemetry folder. If iRacing saves it somewhere else, pick the folder in Settings.",
        "Telemetry folder found.", "settings_folder", f"Debrief looked in {folder}." if folder else "")
    add("record", ibt > 0 or files > 0, "Turn on telemetry in iRacing",
        ((f"Debrief found your telemetry folder but no telemetry files (.ibt) in it yet. "
          f"{TELEMETRY_AUTO_ON if f.get('ensure_telemetry') else TELEMETRY_HOW}")
         if f.get("folder") else f"Debrief hasn't found your telemetry folder yet. {TELEMETRY_HOW}"),
        (f"Debrief found {ibt} telemetry file{'s' if ibt != 1 else ''} (.ibt) in your telemetry folder."
         if ibt else "iRacing is saving your telemetry."),
        # 2.32: Debrief can turn it on for you; once that is on there is nothing to click, just drive
        None if f.get("ensure_telemetry") else "telemetry_auto")
    add("telemetry", files > 0, "Read your telemetry",
        ("Debrief has found telemetry files but hasn't read them yet. Click Read my telemetry." if ibt else
         "Once iRacing has saved some telemetry, Debrief reads it from the folder."),
        f"{files} telemetry file{'s' if files != 1 else ''} read.",
        "read_telemetry" if ibt or not f.get("folder") else None)   # nothing in the folder yet: nothing to read
    add("session", best >= MIN_CLEAN, f"Drive {MIN_CLEAN} clean laps in one session",
        (f"Your best session so far has {best} clean lap{'s' if best != 1 else ''}. Drive at least {MIN_CLEAN} clean "
         f"laps in one go (no offs, no pit stops) so Debrief can pick one thing for you to work on." if files else
         f"Drive at least {MIN_CLEAN} clean laps in one go (no offs, no pit stops) so Debrief can pick one thing for "
         f"you to work on."),
        f"You have a session with {best} clean laps. Open it on the Practice page.", "practice")
    rejected = bool(f.get("token")) and bool(f.get("token_rejected"))
    add("token", f.get("token") and not rejected, "Connect Garage 61 for race reviews",
        ("Garage 61 didn't accept your saved personal access token. Paste it again in Settings." if rejected else
         "Race reviews, your history and team laps come from Garage 61: paste your Garage 61 personal access token "
         "in Settings. Practice, Setup Lab, the Coach and Racing work without it."),
        "Your Garage 61 personal access token is saved.", "settings_token")
    return out


def walkthrough(facts: dict | None = None, state: dict | None = None, **gather_kw) -> dict:
    """Everything the window needs: the steps, how many are done, the next one, and whether to show it at all
    (not when dismissed or when every step is done)."""
    if facts is None:
        facts = gather(**gather_kw)
    if state is None:
        state = load_state(gather_kw.get("state_path"))
    facts = {**facts, "skipped": (state or {}).get(SKIPPED_KEY) or facts.get("skipped") or []}
    st = steps(facts)
    done = sum(1 for s in st if s["done"])
    nskip = sum(1 for s in st if s["skipped"])
    nxt = next((s for s in st if not s["done"] and not s["skipped"]), None)
    dismissed = bool((state or {}).get("dismissed"))
    return {"steps": st, "done": done, "total": len(st), "skipped": nskip, "left": len(st) - done - nskip,
            "next": nxt["id"] if nxt else None,
            "all_done": nxt is None, "dismissed": dismissed, "show": not dismissed and nxt is not None,
            "text": ("You're all set." if nxt is None else f"{done} of {len(st)} done. Next: {nxt['title']}.")}


# ---------------------------------------------------------------- reading what's there
def _settings_dir() -> Path:
    from . import app
    return app.settings_path().parent


def state_path(path=None) -> Path:
    return Path(path) if path else _settings_dir() / STATE_FILE


def load_state(path=None) -> dict:
    try:
        d = json.loads(state_path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001  (missing or damaged: nothing remembered)
        return {}
    return d if isinstance(d, dict) else {}


def _save_state(data: dict, path=None) -> bool:
    try:
        fsutil.write_json(state_path(path), data, mkdir=True)
        return True
    except Exception:  # noqa: BLE001
        return False


def mark(name: str, path=None, value: bool = True) -> bool:
    """Remember a flag ("voice_tested", "dismissed", "token_rejected"). False when it can't be saved."""
    if name not in FLAGS:
        return False
    with _LOCK:                            # two flags set at once (Test voice and Hide) both stay set
        d = load_state(path)
        d[name] = bool(value)
        return _save_state(d, path)


def skip_step(step_id: str, path=None, skip: bool = True) -> bool:
    """Skip a setup step (2.19.2), or take the skip back (skip=False). False for an unknown step or no save."""
    if step_id not in STEP_IDS:
        return False
    with _LOCK:
        d = load_state(path)
        cur = [x for x in (d.get(SKIPPED_KEY) or []) if x in STEP_IDS and x != step_id]
        d[SKIPPED_KEY] = cur + [step_id] if skip else cur
        return _save_state(d, path)


def dismiss(path=None) -> bool:
    return mark("dismissed", path)


def undismiss(path=None) -> bool:
    return mark("dismissed", path, False)


def _cache_dir() -> Path:
    from . import app
    return Path(os.environ.get("DEBRIEF_CACHE_DIR") or Path(app.HERE) / ".cache")


def _index_records(cache_file: Path) -> list:
    """Records of the files Setup Lab's index has read OK ([] when there's no index or it's damaged / old)."""
    try:
        from . import setuplab
        raw = json.loads(Path(cache_file).read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("version") != setuplab.CACHE_VERSION:
            return []
        return [r for r in (raw.get("files") or {}).values() if isinstance(r, dict) and r.get("ok")
                and isinstance(r.get("file"), str)]
    except Exception:  # noqa: BLE001
        return []


def gather(raw_settings: dict | None = None, cache_file=None, state_path=None, folder=None) -> dict:
    """Facts for steps() from Debrief's own files (read only). raw_settings: settings.toml parsed (read when None);
    cache_file: Setup Lab's index file (the cache folder's setuplab-index.json when None); folder: the telemetry folder
    (from the settings when None). Never raises."""
    facts = {"token": False, "folder": False, "folder_path": "", "ibt_files": 0, "files": 0, "best_clean": 0,
             "voice_tested": False, "token_rejected": False, "ensure_telemetry": False}
    try:
        from .desktop import settings_io
        raw = raw_settings
        if raw is None:
            raw, _err = settings_io._raw()
        facts["token"] = bool(settings_io.token_present(raw or {}))
        facts["ensure_telemetry"] = (raw or {}).get("ensure_telemetry") is True
        tf = Path(folder) if folder else settings_io.telemetry_folder(raw or {})
        facts["folder_path"] = str(tf)
        facts["folder"] = tf.is_dir()
        if facts["folder"]:
            facts["ibt_files"] = count_ibt(tf)
    except Exception:  # noqa: BLE001
        pass
    try:
        recs = _index_records(Path(cache_file) if cache_file else _cache_dir() / SETUPLAB_CACHE)
        facts["files"] = len(recs)
        if recs:
            from . import sessionibt
            sess = sessionibt.sessions(recs)
            facts["best_clean"] = max((int(s.get("clean_laps") or 0) for s in sess), default=0)
    except Exception:  # noqa: BLE001
        pass
    st = load_state(state_path)
    facts["voice_tested"] = bool(st.get("voice_tested"))
    facts["token_rejected"] = bool(st.get("token_rejected"))
    return facts


def count_ibt(folder) -> int:
    """How many telemetry files (.ibt) iRacing has saved in the folder (top level, as Setup Lab reads it). Only names
    are looked at. 0 when it can't be listed."""
    n = 0
    try:
        with os.scandir(folder) as it:
            for e in it:
                if e.name.lower().endswith(".ibt"):
                    try:
                        if e.is_file():
                            n += 1
                    except OSError:
                        continue
                    if n >= MAX_COUNT:
                        break
    except OSError:
        return 0
    return n


def token_rejected(path=None) -> bool:
    """Garage 61 rejected the saved token at the last connection check (cleared by a good check or a new token)."""
    return bool(load_state(path).get("token_rejected"))
