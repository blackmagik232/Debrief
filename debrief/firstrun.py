"""First-run walkthrough (2.14), logic only: what is set up already, and the next thing to do, in plain words.

    steps(facts)               -> [{"id", "done", "title", "text", "action"}] from plain facts (pure, for tests)
    gather(...)                -> facts read from Debrief's own files (settings, Setup Lab's index, the state file)
    walkthrough(...)           -> {"steps", "done", "total", "next", "all_done", "dismissed", "show"} for the window
    mark(name, path=None)      -> remember "voice_tested" (or "dismissed") in firstrun.json next to settings.toml
    dismiss(path=None) / undismiss(path=None)

Steps (in order):
    token       a Garage 61 token is saved                         (settings.toml, settings_io.token_present)
    folder      iRacing's telemetry folder is there                 (settings_io.telemetry_folder)
    telemetry   Debrief has read at least one telemetry file        (Setup Lab's index: <cache>/setuplab-index.json)
    session     one session with at least 5 clean laps              (sessionibt.sessions over that index)
    voice       the coach's voice has been tested                   (firstrun.json "voice_tested", marked by the
                                                                    server when Test voice is heard)
"action" is what the window's button for the step does: "settings_token", "settings_folder", "read_telemetry",
"practice", "test_voice" (None when the step is done). Nothing here raises; a damaged state file counts as empty.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import fsutil

STATE_FILE = "firstrun.json"
SETUPLAB_CACHE = "setuplab-index.json"     # the server's SETUPLAB_CACHE, in the cache folder
MIN_CLEAN = 5                              # sessionibt.MIN_LAPS

STEP_IDS = ("token", "folder", "telemetry", "session", "voice")


def steps(facts: dict) -> list:
    """The walkthrough's steps from facts: {"token": bool, "folder": bool, "folder_path": str, "files": int (telemetry
    files read), "best_clean": int (most clean laps in one session), "voice_tested": bool}."""
    f = facts if isinstance(facts, dict) else {}
    files = int(f.get("files") or 0)
    best = int(f.get("best_clean") or 0)
    folder = str(f.get("folder_path") or "")
    out = []

    def add(sid, done, title, todo, ok, action):
        out.append({"id": sid, "done": bool(done), "title": title, "text": ok if done else todo,
                    "action": None if done else action})

    add("token", f.get("token"), "Connect Garage 61",
        "Paste your Garage 61 personal access token in Settings so Debrief can fetch your laps.",
        "Your Garage 61 token is saved.", "settings_token")
    add("folder", f.get("folder"), "Find your iRacing telemetry",
        (f"Debrief can't find your telemetry folder{f' ({folder})' if folder else ''}. If iRacing saves it somewhere "
         f"else, pick the folder in Settings."),
        f"Telemetry folder found{f': {folder}' if folder else ''}.", "settings_folder")
    add("telemetry", files > 0, "Read your telemetry",
        ("No telemetry files read yet. In iRacing, press Alt+L in the car to start saving telemetry, drive a few "
         "laps, then click Read telemetry." if f.get("folder") else
         "Once the folder is found, Debrief reads your telemetry files from it."),
        f"{files} telemetry file{'s' if files != 1 else ''} read.", "read_telemetry")
    add("session", best >= MIN_CLEAN, f"Drive {MIN_CLEAN} clean laps in one session",
        (f"Your best session so far has {best} clean lap{'s' if best != 1 else ''}. Drive at least {MIN_CLEAN} clean "
         f"laps in one go (no offs, no pit stops) so Debrief can review it." if files else
         f"Drive at least {MIN_CLEAN} clean laps in one go (no offs, no pit stops) so Debrief can review it."),
        f"You have a session with {best} clean laps. Open it on the Practice page.", "practice")
    add("voice", f.get("voice_tested"), "Test the coach's voice",
        "Click Test voice on the Coach page to check you can hear it over the game.",
        "The coach's voice has been tested.", "test_voice")
    return out


def walkthrough(facts: dict | None = None, state: dict | None = None, **gather_kw) -> dict:
    """Everything the window needs: the steps, how many are done, the next one, and whether to show it at all
    (not when dismissed or when every step is done)."""
    if facts is None:
        facts = gather(**gather_kw)
    if state is None:
        state = load_state(gather_kw.get("state_path"))
    st = steps(facts)
    done = sum(1 for s in st if s["done"])
    nxt = next((s for s in st if not s["done"]), None)
    dismissed = bool((state or {}).get("dismissed"))
    return {"steps": st, "done": done, "total": len(st), "next": nxt["id"] if nxt else None,
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
    """Remember a flag ("voice_tested", "dismissed"). False when it can't be saved."""
    if name not in ("voice_tested", "dismissed"):
        return False
    d = load_state(path)
    d[name] = bool(value)
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
    facts = {"token": False, "folder": False, "folder_path": "", "files": 0, "best_clean": 0, "voice_tested": False}
    try:
        from .desktop import settings_io
        raw = raw_settings
        if raw is None:
            raw, _err = settings_io._raw()
        facts["token"] = bool(settings_io.token_present(raw or {}))
        tf = Path(folder) if folder else settings_io.telemetry_folder(raw or {})
        facts["folder_path"] = str(tf)
        facts["folder"] = tf.is_dir()
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
    facts["voice_tested"] = bool(load_state(state_path).get("voice_tested"))
    return facts
