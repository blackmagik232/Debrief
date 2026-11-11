"""Night shift folder (2.30.2): after a session, Debrief copies its telemetry to the folder Siyanqaba's night shift
reads on the Zerver (a shared folder on the home network). Off until Settings > Siyanqaba has a folder.

Only ever READS iRacing's telemetry folder: files are copied, never moved, changed or deleted there.

What lands in the night shift folder for one session (CONTRACT.md):
    <file>.ibt ...               the session's telemetry files, each written as <file>.ibt.partial, then renamed,
                                 so the night shift never picks up half a file
    <session id>.session.json    {"session_id", "files": [...], "car_path", "track_key", "sent_at"}: written last
    feel/<session id>.json       the "How did it feel" taps, when there are any (copied again after each tap)

    push(dest, sid, paths, feel_file=None, sent_file=None, info=None) -> {"sent", "skipped", "error"}
    is_sent(sent_file, paths) / pending(sessions, sent_file, since)
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

from . import fsutil

SETTLE_S = 120            # a file changed in the last 2 minutes may still be being written: wait
AUTO_DAYS = 7             # sessions sent on their own: the last week (older ones with Send to Zerver)
MANIFEST = "{sid}.session.json"
_lock = threading.Lock()


def _key(p: Path) -> str | None:
    try:
        st = p.stat()
    except OSError:
        return None
    return f"{p.name}|{st.st_size}|{int(st.st_mtime)}"


def load_sent(sent_file) -> dict:
    try:
        d = json.loads(Path(sent_file).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def is_sent(sent_file, paths) -> bool:
    sent = load_sent(sent_file) if sent_file else {}
    keys = [_key(Path(p)) for p in paths or []]
    return bool(keys) and all(k is not None and k in sent for k in keys)


def settled(paths, now=None) -> bool:
    now = time.time() if now is None else now
    for p in paths or []:
        try:
            if now - Path(p).stat().st_mtime < SETTLE_S:
                return False
        except OSError:
            return False
    return True


def _copy(src: Path, dest_dir: Path) -> None:
    final = dest_dir / src.name
    try:
        if final.stat().st_size == src.stat().st_size:
            return                                   # already there (a retry after the manifest failed)
    except OSError:
        pass
    part = dest_dir / (src.name + ".partial")
    shutil.copyfile(src, part)
    os.replace(part, final)


def copy_feel(dest, sid, feel_file) -> bool:
    """The feel file for one session into <dest>/feel/. False (and nothing else happens) when it can't be done."""
    if not dest or not feel_file or not Path(feel_file).is_file():
        return False
    try:
        d = Path(dest) / "feel"
        d.mkdir(parents=True, exist_ok=True)
        part = d / f"{sid}.json.partial"
        shutil.copyfile(feel_file, part)
        os.replace(part, d / f"{sid}.json")
        return True
    except OSError:
        return False


def push(dest, sid, paths, feel_file=None, sent_file=None, info=None) -> dict:
    """Copy one session to the night shift folder. Never raises: problems come back as plain words in "error"."""
    out = {"sent": [], "skipped": [], "error": None}
    if not dest:
        out["error"] = "No night shift folder in Settings."
        return out
    dest = Path(dest)
    paths = [Path(p) for p in paths or []]
    if not paths:
        out["error"] = "This session has no telemetry files."
        return out
    if not dest.is_dir():
        out["error"] = f"Debrief can't reach {dest} right now. Is the Zerver on and the folder shared?"
        return out
    missing = [p.name for p in paths if not p.is_file()]
    if missing:
        out["error"] = "Some of this session's files have gone: " + ", ".join(missing) + "."
        return out
    if not settled(paths):
        out["error"] = "iRacing may still be writing this session. It is sent once the files have been quiet for two minutes."
        return out
    with _lock:
        sent = load_sent(sent_file) if sent_file else {}
        try:
            for p in paths:
                k = _key(p)
                if k in sent:
                    out["skipped"].append(p.name)
                    continue
                _copy(p, dest)
                out["sent"].append(p.name)
            info = info if isinstance(info, dict) else {}
            man = {"session_id": sid, "files": [p.name for p in paths], "car_path": str(info.get("car_path") or ""),
                   "track_key": str(info.get("track_key") or ""),
                   "sent_at": datetime.now().astimezone().isoformat(timespec="seconds")}
            fsutil.write_json(dest / MANIFEST.format(sid=sid), man, compact=False, indent=1)
            copy_feel(dest, sid, feel_file)
            if sent_file:
                stamp = man["sent_at"]
                for p in paths:
                    k = _key(p)
                    if k:
                        sent[k] = stamp
                fsutil.write_json(sent_file, sent, mkdir=True)
        except OSError as e:
            out["error"] = f"Copying to {dest} stopped: {e.strerror or e}. It is tried again next time Debrief opens."
    return out


def pending(sessions, sent_file, since_days=AUTO_DAYS, now=None) -> list:
    """Sessions (dicts with "paths" and "sort") from the last since_days that are not all sent yet, oldest first."""
    now = time.time() if now is None else now
    cut = now - since_days * 86400
    out = [s for s in sessions or [] if (s.get("sort") or 0) >= cut and s.get("paths")
           and not is_sent(sent_file, s["paths"])]
    return sorted(out, key=lambda s: s.get("sort") or 0)
