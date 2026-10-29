"""Track cards: short, hand-written notes for each track layout.

One small JSON file per layout in <library>/track-notes/. Notes belong to the track, not to a series
or a car, so a card follows the track into every series that visits it. Telemetry numbers (brake
points, gears, weak corners) are never stored here: the Prepare page works them out from your laps.

Five optional fields, one or two lines each, plus an optional line per car:
    lap1      Lap 1 / the start
    hotspots  where people crash
    passing   where to pass and defend
    limits    track limits and kerbs
    focus     what to work on this week (up to 3 lines)

Team track cards (2.4): with `team_cards_folder` set to a folder the team shares (OneDrive, Google Drive,
Dropbox...) and `share_my_cards` on, every save also writes YOUR card to
    <team_cards_folder>/Debrief track cards/<track_key>/<your Garage 61 slug>.json
and nothing else. Teammates' files are read and merged in memory only (shared_for); a merge never writes
a file, so sync clients and conflict copies can't make cards overwrite each other.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from . import fsutil

SCHEMA = 1
FIELDS = ("lap1", "hotspots", "passing", "limits", "focus")
LABELS = {"lap1": "Lap 1 / start", "hotspots": "Crash hotspots", "passing": "Passing spots",
          "limits": "Track limits / kerbs", "focus": "Focus this week"}
MAX_FIELD = 300
MAX_FOCUS_LINES = 3
MAX_CAR_NOTE = 200
MAX_CARS = 20


def _legacy_key(name: str) -> str:
    """The key Debrief 2.2.0-2.2.1 used (accented letters dropped). Read so older cards still open."""
    s = str(name or "").lower()
    s = re.sub(r"\b(19|20)\d\d\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    s = re.sub(r"-+", "-", s)
    return s[:120] or "unknown-track"


def track_key(name: str) -> str:
    """A file-safe key for a track layout. Years are dropped ('Roval 2019' and 'Roval 2025' share a card),
    'Track - Config' and 'Track (Config)' give the same key, and accents are folded ('Autódromo' and
    'Autodromo' share a card). A name with no Latin letters at all gets a short hash, never a shared key."""
    from .schedule import fold
    raw = " ".join(str(name or "").split())
    s = re.sub(r"\b(19|20)\d\d\b", " ", fold(raw).lower())
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    s = re.sub(r"-+", "-", s)[:120].strip("-")
    if s:
        return s
    if not raw:
        return "unknown-track"
    return "track-" + hashlib.sha1(raw.casefold().encode("utf-8")).hexdigest()[:12]


def folder(library_folder) -> Path:
    return Path(library_folder) / "track-notes"


def _path(library_folder, key: str) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,119}", key or ""):
        raise ValueError("That track wasn't found.")
    return folder(library_folder) / f"{key}.json"


def empty(track: str) -> dict:
    return {"schema": SCHEMA, "key": track_key(track), "track": str(track or "").strip(), "fields": {k: "" for k in FIELDS},
            "car_notes": {}, "updated": None, "season": None}


def load(library_folder, track: str) -> dict:
    """The card for a track (an empty one if none is saved). Never raises for a bad file."""
    key = track_key(track)
    out = empty(track)
    d = None
    for k in dict.fromkeys((key, _legacy_key(track))):
        if k == "unknown-track" and key != k:
            continue   # 2.2.1 put every non-Latin name on this one card: don't show it for them
        try:
            d = json.loads(_path(library_folder, k).read_text(encoding="utf-8"))
            break
        except (OSError, ValueError):
            d = None
    if d is None:
        return out
    if not isinstance(d, dict):
        return out
    f = d.get("fields") if isinstance(d.get("fields"), dict) else {}
    out["fields"] = {k: str(f.get(k) or "") for k in FIELDS}
    cn = d.get("car_notes") if isinstance(d.get("car_notes"), dict) else {}
    out["car_notes"] = {str(k): str(v) for k, v in cn.items() if isinstance(v, str) and v.strip()}
    out["updated"] = d.get("updated") if isinstance(d.get("updated"), str) else None
    out["season"] = d.get("season") if isinstance(d.get("season"), str) else None
    if isinstance(d.get("track"), str) and d["track"].strip():
        out["track"] = d["track"]
    out["read_only"] = isinstance(d.get("schema"), int) and d["schema"] > SCHEMA
    return out


def has_notes(card: dict) -> bool:
    return any((card.get("fields") or {}).get(k) for k in FIELDS) or bool(card.get("car_notes"))


def clean(raw) -> dict:
    """Validate a card from the window. Raises ValueError with a friendly message."""
    if not isinstance(raw, dict):
        raise ValueError("Bad request.")
    track = raw.get("track")
    if not isinstance(track, str) or not track.strip() or len(track) > 200:
        raise ValueError("Pick a track for the card.")
    fields = raw.get("fields") if isinstance(raw.get("fields"), dict) else {}
    out = {}
    for k in FIELDS:
        v = fields.get(k)
        if v in (None, ""):
            out[k] = ""
            continue
        if not isinstance(v, str):
            raise ValueError(f"{LABELS[k]} must be text.")
        v = "\n".join(line.strip() for line in v.replace("\r", "").split("\n") if line.strip())
        if len(v) > MAX_FIELD:
            raise ValueError(f"Keep {LABELS[k]} short: up to {MAX_FIELD} characters.")
        if k == "focus" and len(v.split("\n")) > MAX_FOCUS_LINES:
            raise ValueError(f"Pick at most {MAX_FOCUS_LINES} things to focus on.")
        out[k] = v
    cars = {}
    cn = raw.get("car_notes")
    if cn not in (None, ""):
        if not isinstance(cn, dict) or len(cn) > MAX_CARS:
            raise ValueError("Bad request.")
        for car, note in cn.items():
            if not isinstance(car, str) or not car.strip() or len(car) > 200:
                raise ValueError("Bad request.")
            if note in (None, ""):
                continue
            if not isinstance(note, str):
                raise ValueError("Car notes must be text.")
            note = " ".join(note.split())
            if len(note) > MAX_CAR_NOTE:
                raise ValueError(f"Keep car notes short: up to {MAX_CAR_NOTE} characters.")
            if note:
                cars[car.strip()] = note
    season = raw.get("season")
    season = season.strip()[:40] if isinstance(season, str) and season.strip() else None
    out_card = {"track": track.strip(), "fields": out, "car_notes": cars, "season": season}
    # the card's "updated" time when the window opened it: a save is refused if it changed since
    base = raw.get("base_updated", _NO_BASE)
    if base is not _NO_BASE:
        if base is not None and (not isinstance(base, str) or len(base) > 40):
            raise ValueError("Bad request.")
        out_card["base_updated"] = base
    return out_card


_NO_BASE = object()


class Conflict(ValueError):
    """The card changed since the window opened it (another window, or a newer save)."""


def save(library_folder, card: dict, cfg: dict | None = None) -> dict:
    """Save a cleaned card (see clean). An all-empty card deletes the file. With cfg (the settings) and
    `share_my_cards` on, your shared team file is written (or deleted) too; a shared-folder problem never
    fails the local save, it is returned as card["share_error"] instead."""
    key = track_key(card["track"])
    p = _path(library_folder, key)
    existing = load(library_folder, card["track"])
    if existing.get("read_only"):
        raise ValueError("This card was saved by a newer Debrief. Update Debrief to edit it.")
    if "base_updated" in card and card["base_updated"] != existing.get("updated"):
        raise Conflict("This track card was changed somewhere else after you opened it. "
                       "Close it and open it again to see the newer notes, then make your change.")
    legacy = _legacy_key(card["track"])
    old = _path(library_folder, legacy) if legacy not in (key, "unknown-track") else None
    d = {"schema": SCHEMA, "key": key, "track": card["track"], "fields": {k: card["fields"].get(k, "") for k in FIELDS},
         "car_notes": dict(card.get("car_notes") or {}), "season": card.get("season") or existing.get("season"),
         "updated": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")}
    if not has_notes(d):
        for f in (p, old):
            try:
                if f is not None:
                    f.unlink()
            except FileNotFoundError:
                pass
        return _share(cfg, d, empty(card["track"]))
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=p.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        fsutil.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    if old is not None:   # moved to the new (accent-folded) key
        try:
            old.unlink()
        except OSError:
            pass
    return _share(cfg, d, load(library_folder, card["track"]))


def all_cards(library_folder) -> list:
    """Every saved card, newest first (for 'which tracks have notes')."""
    out = []
    for p in folder(library_folder).glob("*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and isinstance(d.get("track"), str):
            out.append({"key": p.stem, "track": d["track"], "updated": d.get("updated")})
    out.sort(key=lambda c: c.get("updated") or "", reverse=True)
    return out


# ---------------------------------------------------------------- team track cards (2.4)
SHARED_DIR = "Debrief track cards"
KIND = "debrief-track-card"
MAX_SHARED_BYTES = 16 * 1024
MAX_AUTHORS = 50
MAX_SCAN = 500   # directory entries looked at per track (a folder full of junk can't stall the page)
_SLUG_FILE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}\.json$")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _team_folder(cfg) -> Path | None:
    raw = (cfg or {}).get("team_cards_folder") if isinstance(cfg, dict) else None
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 400:
        return None
    try:
        p = Path(os.path.expandvars(os.path.expanduser(raw.strip())))
        return p if p.is_dir() else None
    except (OSError, ValueError):
        return None


def shared_root(cfg) -> Path | None:
    """<team_cards_folder>/Debrief track cards, or None when no (existing) team folder is set.
    The 'Debrief track cards' folder itself may not exist yet (publish creates it)."""
    base = _team_folder(cfg)
    return None if base is None else base / SHARED_DIR


def slugify(s) -> str:
    from .schedule import fold
    s = re.sub(r"[^a-z0-9]+", "-", fold(str(s or "")).lower()).strip("-")
    return re.sub(r"-+", "-", s)[:80].strip("-")


def author_from_cache(cfg) -> dict | None:
    """{"slug","name"} from the Garage 61 'me' answer Debrief already saved (Check connection or a review).
    Reads the cache only: never a request. None when it's unknown."""
    try:
        from .g61 import BASE, Garage61
        me = Garage61.peek_saved((cfg or {}).get("token", ""), Path(cfg["cache_dir"]), "me",
                                 cfg.get("api_base") or BASE)
    except Exception:  # noqa: BLE001  (no settings, no cache: just unknown)
        return None
    if not isinstance(me, dict):
        return None
    slug = slugify(me.get("slug"))
    if not slug or not _SLUG.match(slug):
        return None
    first = str(me.get("firstName") or me.get("firstname") or "").strip()
    last = str(me.get("lastName") or me.get("lastname") or "").strip()
    name = " ".join(" ".join(filter(None, (first, last))).split())[:80] or slug
    return {"slug": slug, "name": name}


def _write_atomic(p: Path, obj) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=p.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=1, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        fsutil.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def publish(cfg, card: dict, author: dict) -> bool:
    """Write YOUR shared card: only <root>/<track_key>/<author_slug>.json, atomically. A card with no notes
    deletes that one file. False (nothing written) when sharing is off, there's no team folder or no
    author. Other files in the folder are never touched. OSError is raised for the caller to report."""
    if not isinstance(cfg, dict) or not cfg.get("share_my_cards"):
        return False
    root = shared_root(cfg)
    slug = (author or {}).get("slug") if isinstance(author, dict) else None
    if root is None or not isinstance(slug, str) or not _SLUG.match(slug):
        return False
    track = str(card.get("track") or "").strip()
    if not track:
        return False
    d = root / track_key(track)
    p = d / f"{slug}.json"
    if not has_notes(card):
        if not (p.exists() or p.is_symlink()):
            return True
        _check_publish_target(root, d, p)
        try:
            if p.is_file():
                p.unlink()
        except FileNotFoundError:
            pass
        return True
    from . import __version__
    name = " ".join(str(author.get("name") or slug).split())[:80] or slug
    body = {"schema": SCHEMA, "kind": KIND, "track": track, "author": {"slug": slug, "name": name},
            "fields": {k: str((card.get("fields") or {}).get(k) or "") for k in FIELDS},
            "car_notes": dict(card.get("car_notes") or {}),
            "updated": card.get("updated") if isinstance(card.get("updated"), str) else _now(),
            "app": f"Debrief {__version__}"}
    _check_publish_target(root, d, p)
    d.mkdir(parents=True, exist_ok=True)
    _check_publish_target(root, d, p)          # again, now the folders exist
    _write_atomic(p, body)
    return True


_LINK_MSG = "Your shared track card folder is a link to somewhere else, so Debrief left it alone."


def _is_link(p: Path) -> bool:
    """A symlink, or (Windows) a directory junction."""
    try:
        if p.is_symlink():
            return True
        isj = getattr(os.path, "isjunction", None)          # Python 3.12+
        return bool(isj and isj(str(p)))
    except OSError:
        return True


def _check_publish_target(root: Path, d: Path, p: Path) -> None:
    """Before writing or deleting YOUR shared card (2.4.0 review M2): the 'Debrief track cards' folder, the
    track folder and the file must not be links (or junctions), and each must really be where it looks
    (os.path.realpath, which also follows junctions) inside the shared root. Raises OSError (plain)."""
    try:
        if _is_link(root) or _is_link(d) or _is_link(p):
            raise OSError(_LINK_MSG)
        for x in (root, d):
            if x.exists() and not x.is_dir():
                raise OSError(_LINK_MSG)
        if p.exists() and not p.is_file():
            raise OSError(_LINK_MSG)
        rbase = os.path.realpath(str(root.parent))
        rroot = os.path.realpath(str(root))
        rd = os.path.realpath(str(d))
        rp = os.path.realpath(str(p))
        if os.path.dirname(rroot) != rbase or os.path.dirname(rd) != rroot or os.path.dirname(rp) != rd:
            raise OSError(_LINK_MSG)
    except ValueError:
        raise OSError(_LINK_MSG)


def _share(cfg, saved: dict, out: dict) -> dict:
    """After a local save: publish when sharing is on. Never fails the save."""
    if not isinstance(cfg, dict) or not cfg.get("share_my_cards"):
        return out
    author = author_from_cache(cfg)
    if author is None:
        out["share_error"] = "Check your Garage 61 connection once so Debrief knows your name."
        return out
    if shared_root(cfg) is None:
        out["share_error"] = "The team folder for track cards wasn't found. Pick it again in Settings."
        return out
    try:
        publish(cfg, saved, author)
    except OSError:
        out["share_error"] = ("Saved on this PC, but Debrief couldn't write your card to the team folder. "
                              "Check the folder is still there and try again.")
    return out


def _inside(p: Path, root: Path) -> bool:
    try:
        return os.path.commonpath([str(p.resolve()), str(root.resolve())]) == str(root.resolve())
    except (OSError, ValueError):
        return False


def _read_shared(p: Path, root: Path, me_slug):
    """One teammate's card file, validated, or None. Read only."""
    stem = p.name[:-5]
    if stem == me_slug:
        return None
    try:
        if p.is_symlink() or not p.is_file() or not _inside(p, root):
            return None
        if p.stat().st_size > MAX_SHARED_BYTES:
            return None
        with open(p, "rb") as f:
            raw = f.read(MAX_SHARED_BYTES + 1)
        if len(raw) > MAX_SHARED_BYTES:
            return None
        d = json.loads(raw.decode("utf-8-sig"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(d, dict) or d.get("kind") != KIND:
        return None
    sc = d.get("schema")
    if not isinstance(sc, int) or isinstance(sc, bool) or sc < 1 or sc > SCHEMA:
        return None
    a = d.get("author")
    if not isinstance(a, dict) or a.get("slug") != stem:
        return None   # a renamed copy or a conflict copy: whose card is it? ignore it
    try:
        c = clean({"track": d.get("track") if isinstance(d.get("track"), str) and d["track"].strip() else "?",
                   "fields": d.get("fields") if isinstance(d.get("fields"), dict) else {},
                   "car_notes": d.get("car_notes") if isinstance(d.get("car_notes"), dict) else {}})
    except ValueError:
        return None
    if not has_notes(c):
        return None
    name = a.get("name") if isinstance(a.get("name"), str) else ""
    name = " ".join(name.split())[:80] or stem
    upd = d.get("updated") if isinstance(d.get("updated"), str) and len(d["updated"]) <= 40 else None
    return {"author": {"slug": stem, "name": name}, "updated": upd, "fields": c["fields"], "car_notes": c["car_notes"]}


def shared_for(cfg, track: str, me_slug: str | None) -> list[dict]:
    """Teammates' cards for a track, newest first: [{"author","updated","fields","car_notes"}]. Read only:
    never creates, changes or deletes a file. Your own file (me_slug) is skipped, since yours is local."""
    root = shared_root(cfg)
    if root is None:
        return []
    d = root / track_key(track)
    try:
        if d.is_symlink() or not d.is_dir() or not _inside(d, root):
            return []
        names = []
        with os.scandir(d) as it:
            for i, e in enumerate(it):
                if i >= MAX_SCAN:
                    break
                if _SLUG_FILE.match(e.name):
                    names.append(e.name)
    except OSError:
        return []
    out = []
    for n in sorted(names):
        c = _read_shared(d / n, root, me_slug)
        if c is not None:
            out.append(c)
            if len(out) >= MAX_AUTHORS:
                break
    out.sort(key=lambda c: c.get("updated") or "", reverse=True)
    return out
