"""Render review pages and the library home page from templates."""
from __future__ import annotations

import json
import os
from pathlib import Path

TPL = Path(__file__).resolve().parent / "templates"


def _embed(obj) -> str:
    """JSON for a <script> block. <, > and & are written as \\u003c, \\u003e, \\u0026 (same value once
    parsed) so text such as "</script>" or "<!--<script>" in notes or names can't end or upset the
    script element."""
    return (json.dumps(obj, separators=(",", ":"))
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


LAYOUT = "2.28.1"   # bump when review.html changes so saved reviews are re-rendered (no requests needed)

# Review-dict extras worked out on every write (so saved reviews get them when refresh_all re-renders them),
# in this order, before insights.build (which reads them): (key, module, function). Each is guarded: a module
# that is missing or fails drops only its own key, and a None result leaves the key out (e.g. session_review
# for a race). technique_plus is not here: the engine writes it at analysis time (analysis_plus.extend).
EXTRAS = (("brake_spread", "technique_plus", "spread_for_review"),
          ("skills", "skills", "for_review"),
          ("session_review", "session_review", "for_review"))
# 2.4: worked out only when the key is absent (the engine writes both for new reviews, from raw laps that an
# old review.json doesn't keep). Same guard as EXTRAS; they also run before insights.build.
IF_ABSENT = (("strategy", "strategy", "block_from_review"),
             ("racecraft", "racecraft", "build"),
             ("depth", "depth", "for_review"))


def _extras(data: dict, log=None) -> None:
    """2.7 (M7): a derived key is replaced only when the renderer gives a value, and dropped only when it
    was absent before. When a renderer fails (or answers None) on a review that already has that key, the
    old value is kept and the reason logged: review.json is the only copy, so saved analysis is never
    thrown away because new code couldn't redo it."""
    import importlib
    todo = list(EXTRAS) + [x for x in IF_ABSENT if data.get(x[0]) is None]
    for key, mod, fn in todo:
        name = f"{__package__}.{mod}"
        had = key in data and data.get(key) is not None
        failed = False
        try:
            val = getattr(importlib.import_module(name), fn)(data)
        except ModuleNotFoundError as e:   # a copy without that module (nothing to log on every write)
            val = None
            failed = True
            if log and e.name != name:
                log(f"  Couldn't work out {key}: {e}")
        except Exception as e:  # noqa: BLE001  (never worth losing the review over)
            val = None
            failed = True
            if log:
                log(f"  Couldn't work out {key}: {type(e).__name__}: {e}" +
                    ("; keeping the saved one" if had else ""))
        if val is not None:
            data[key] = val
        elif had:
            if not failed and log:
                log(f"  {key} came back empty this time; keeping the saved one")
        else:
            data.pop(key, None)


def _from_iracing(out_dir: Path, data: dict, log=None) -> None:
    """2.27: the "From iRacing" section, when Debrief recorded this session (recordings.py). Only reviews inside a
    library's reviews folder look; one that finds nothing keeps what it already had (M7)."""
    try:
        if out_dir.parent.name != "reviews":
            return
        from . import recordings
        fi = recordings.for_review(data, out_dir.parent.parent)
        if fi:
            data["from_iracing"] = fi
    except Exception as e:  # noqa: BLE001
        if log:
            log(f"  Couldn't add the recording to the review: {type(e).__name__}: {e}")


def sanitize(obj):
    """obj with every NaN / Infinity float replaced by None (null in JSON), in place for dicts and lists,
    and returned. Strict JSON readers reject NaN/Infinity, and the page treats null as "no value"."""
    import math
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (float, dict, list)):
                obj[k] = sanitize(v)
        return obj
    if isinstance(obj, list):
        for i, v in enumerate(obj):
            if isinstance(v, (float, dict, list)):
                obj[i] = sanitize(v)
        return obj
    return obj


def write(out_dir: Path, data: dict, log=None) -> Path:
    out_dir = Path(out_dir)
    _extras(data, log)
    _from_iracing(out_dir, data, log)
    try:   # the coaching summary at the top of the page; never worth losing the review over
        from . import insights
        data["insights"] = insights.build(data)
    except Exception:  # noqa: BLE001
        data.pop("insights", None)
    sanitize(data)   # never write NaN / Infinity into review.json or the page (2.3.0 verification L1)
    # the page gets D.brand (team names for the header); review.json itself doesn't keep it
    html = fill((TPL / "review.html").read_text(encoding="utf-8"), {"/*DATA*/null": _embed(dict(data, brand=brand()))})
    # each file is written to a temp file and swapped in, so a crash or a forced close mid-write leaves
    # the old file whole (review.json is the only copy of an analysed review)
    atomic_write(out_dir / "review.json", json.dumps(data, allow_nan=False))
    page = out_dir / "index.html"
    atomic_write(page, html)
    return page


def atomic_write(path: Path, text: str) -> None:
    """Write text to path via fsutil's one atomic write (temp file, flushed to disk, Windows replace retry)."""
    from . import fsutil
    fsutil.write_text(path, text, fsync=True)


# ---------------------------------------------------------------- 2.7 (M7): backups before a re-render
BACKUP_DIR = ".backup"
BACKUP_KEEP = 2   # newest backup sets kept (one per old layout version); older ones are pruned


def _backup_sets(lib: Path) -> list:
    """Backup sets in <library>/.backup, newest first (by modification time, then name)."""
    root = lib / BACKUP_DIR
    try:
        sets = [d for d in root.iterdir() if d.is_dir()]
    except OSError:
        return []

    def key(d):
        try:
            return d.stat().st_mtime
        except OSError:
            return 0.0
    return sorted(sets, key=lambda d: (key(d), d.name), reverse=True)


def _backup_ok(target: Path, src: Path) -> bool:
    """Is an existing backup copy usable? It must parse as JSON, and its size must match the source unless the
    source has been rewritten since the copy was made (a refresh that was stopped halfway: the backup then
    holds the review as it was before, which is the point of it, and must not be overwritten)."""
    try:
        json.loads(target.read_text(encoding="utf-8"))
        ts, ss = target.stat(), src.stat()
    except (OSError, ValueError):
        return False
    return ts.st_size == ss.st_size or ss.st_mtime_ns > ts.st_mtime_ns


def _copy_atomic(src: Path, target: Path) -> None:
    """Copy src to target through a temporary file in the target's folder, then os.replace: a copy cut
    short (power cut, full disk) never leaves a half-written backup under the real name. Raises OSError."""
    import shutil
    import tempfile
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + target.name + ".", suffix=".tmp", dir=str(target.parent))
    os.close(fd)
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def backup_reviews(library_folder, version: str, log=None, failed: set | None = None) -> int:
    """Copy every reviews/*/review.json into <library>/.backup/<version>/<review-folder>/review.json before
    the first rewrite of a new layout. version is the layout the reviews are in now (the old one), or
    "unknown". Returns how many were copied. An existing set for that version is completed, not redone; a
    copy in it that is damaged (doesn't parse, or the wrong size: _backup_ok) is made again. Each copy is
    atomic (_copy_atomic). failed (a set) collects the review folders that couldn't be backed up.
    Keeps at most BACKUP_KEEP sets (the newest), pruning the rest."""
    lib = Path(library_folder)
    version = _safe_version(version)
    dest = lib / BACKUP_DIR / version
    n = 0
    for p in sorted((lib / "reviews").glob("*/review.json")):
        target = dest / p.parent.name / "review.json"
        if target.exists() and _backup_ok(target, p):
            continue
        try:
            _copy_atomic(p, target)
            n += 1
        except OSError as e:
            if failed is not None:
                failed.add(p.parent.name)
            if log:
                log(f"  Couldn't back up {p.parent.name}: {e}")
    try:   # the set's time is when it was (last) written, so pruning keeps the newest
        if dest.exists():
            os.utime(dest, None)
    except OSError:
        pass
    prune_backups(lib, keep=BACKUP_KEEP, log=log)
    return n


def _safe_version(version) -> str:
    v = "".join(ch for ch in str(version or "").strip() if ch.isalnum() or ch in "._-") or "unknown"
    return v.strip(".") or "unknown"


def prune_backups(library_folder, keep: int = BACKUP_KEEP, log=None) -> int:
    """Delete backup sets beyond the `keep` newest. Returns how many sets were removed."""
    import shutil
    lib = Path(library_folder)
    gone = 0
    for d in _backup_sets(lib)[max(0, int(keep)):]:
        try:
            shutil.rmtree(d)
            gone += 1
        except OSError as e:
            if log:
                log(f"  Couldn't remove the old backup {d.name}: {e}")
    return gone


def list_backups(library_folder) -> list:
    """Backup set versions in <library>/.backup, newest first."""
    return [d.name for d in _backup_sets(Path(library_folder))]


def restore_backup(library_folder, version: str, log=None, rerender: bool = True) -> int:
    """Put every review.json saved in <library>/.backup/<version> back in place (atomically), then re-render
    each page from it. Returns how many reviews were restored. No requests. Raises FileNotFoundError when
    there's no such backup set."""
    lib = Path(library_folder)
    src = lib / BACKUP_DIR / _safe_version(version)
    if not src.is_dir():
        raise FileNotFoundError(f"No backup set '{version}' in {lib / BACKUP_DIR}")
    n = 0
    for p in sorted(src.glob("*/review.json")):
        out_dir = lib / "reviews" / p.parent.name
        try:
            text = p.read_text(encoding="utf-8")
            data = json.loads(text)
            out_dir.mkdir(parents=True, exist_ok=True)
            atomic_write(out_dir / "review.json", text)
            if rerender and isinstance(data, dict):
                html = fill((TPL / "review.html").read_text(encoding="utf-8"),
                            {"/*DATA*/null": _embed(dict(data, brand=brand()))})
                atomic_write(out_dir / "index.html", html)
            n += 1
        except (OSError, ValueError) as e:
            if log:
                log(f"  Couldn't restore {p.parent.name}: {e}")
    if n:
        try:
            from . import library
            library.rebuild(lib)
        except Exception as e:  # noqa: BLE001
            if log:
                log(f"  Couldn't update the library page: {e}")
    return n


def refresh_all(library_folder, log=None, should_stop=None, done=None, progress=None) -> int:
    """Re-render every saved review with the current page layout, from its review.json (no requests).
    Runs once per layout version (marker file .layout in the library). Returns how many were redone.
    should_stop() -> True pauses it between reviews (the marker isn't written, so it carries on later);
    done is a set of review folders already redone in this run, so a resumed run skips them.
    progress(done_count, total) is called after each review (the window shows it on the Skills page)."""
    lib = Path(library_folder)
    marker = lib / ".layout"
    old_layout = "unknown"
    try:
        old_layout = marker.read_text(encoding="utf-8").strip() or "unknown"
        if old_layout == LAYOUT:
            return 0
    except OSError:
        pass
    n = 0
    done = done if done is not None else set()
    todo = sorted((lib / "reviews").glob("*/review.json"))
    total = len(todo)
    unsafe: set = set()   # 2.7.1 (L2): review folders with no backup copy: left as they are this time
    if todo and not all(p.parent.name in done for p in todo):
        # 2.7 (M7): before the first rewrite, a copy of every review.json (the only copy of an analysed
        # review) goes to .backup/<old layout>/; restore_backup() puts it back. At most 2 sets are kept.
        try:
            backup_reviews(lib, old_layout, log=log, failed=unsafe)
        except Exception as e:  # noqa: BLE001  (no backup at all: nothing is rewritten this time)
            unsafe.update(p.parent.name for p in todo)
            if log:
                log(f"  Couldn't back up the saved reviews before re-rendering: {e}")
    skipped = 0
    if progress:
        progress(sum(1 for p in todo if p.parent.name in done), total)
    for p in todo:
        if p.parent.name in done:
            continue
        if should_stop and should_stop():
            return n
        if p.parent.name in unsafe:
            skipped += 1
            if log:
                log(f"  {p.parent.name} kept as it was because it couldn't be backed up first; "
                    "Debrief tries again next time it starts.")
            continue
        try:
            before = p.stat().st_mtime_ns
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("laps"):
                if p.stat().st_mtime_ns == before:   # a review saved meanwhile is already in the new layout
                    try:      # 2.8: new race analysis reaches reviews that already have a saved scan
                        from . import racecap_link
                        racecap_link.refresh_saved(lib, data)
                    except Exception as e:  # noqa: BLE001
                        if log:
                            log(f"  Couldn't update {p.parent.name}'s race log: {e}")
                    write(p.parent, data, log=log)
                    n += 1
            done.add(p.parent.name)
        except Exception as e:  # noqa: BLE001  (one odd review mustn't stop the rest)
            done.add(p.parent.name)
            if log:
                log(f"  Couldn't refresh {p.parent.name}: {e}")
        k = sum(1 for q in todo if q.parent.name in done)
        if progress:
            progress(k, total)
        if log and n and n % 25 == 0:
            log(f"  Refreshed {k} of {total} saved reviews...")
    if not skipped:   # 2.7.1 (L2): a review left un-refreshed keeps the layout open, so the next launch retries
        try:
            lib.mkdir(parents=True, exist_ok=True)
            atomic_write(marker, LAYOUT)
        except OSError:
            pass
    if n or done:   # the library page once, now that every review is in the new layout (also after a resumed run)
        try:
            from . import library
            library.rebuild(lib)
        except Exception as e:  # noqa: BLE001  (rebuilt again the next time it's opened)
            if log:
                log(f"  Couldn't update the library page: {e}")
    return n


def render_library(rows: list, extra=None) -> str:
    """Library page with rows and extra (goals/season) embedded. Both markers are filled in one pass
    over the template, so marker-like text inside the data is never substituted again.
    EXTRA.brand is the team identity (brand.as_dict()), added when extra is a dict or None."""
    tpl = (TPL / "library.html").read_text(encoding="utf-8")
    if extra is None or isinstance(extra, dict):
        extra = dict(extra or {}, brand=brand())
    return fill(tpl, {"/*ROWS*/[]": _embed(rows), "/*EXTRA*/null": _embed(extra)})


BRAND_MARKER = "/*BRAND*/null"


def brand() -> dict:
    """debrief.brand.as_dict() (team names and links, no secrets): every page template gets it, as D.brand on
    review pages, EXTRA.brand on the library page and, in any template, wherever /*BRAND*/null appears."""
    try:
        from . import brand as _brand
        return _brand.as_dict()
    except Exception:  # noqa: BLE001  (a page never fails over its header)
        return {}


def fill(tpl: str, values: dict) -> str:
    """Replace each marker in the template exactly once, in a single pass (data never re-scanned).
    A /*BRAND*/null marker in the template is filled with brand() unless the caller gives its own value."""
    import re
    values = dict(values)
    if BRAND_MARKER not in values and BRAND_MARKER in tpl:
        values[BRAND_MARKER] = _embed(brand())
    return re.sub("|".join(re.escape(k) for k in values), lambda m: values.pop(m.group(0), m.group(0)), tpl)
