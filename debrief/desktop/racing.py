"""Debrief 2.16 "Racing" in the window's server: your starts (debrief/startreport.py) and your incidents
(debrief/incidentmap.py), worked out together from what is already on this PC.

    GET api/racing[?refresh=1]

Where the data comes from (all read only):
  - the library folder (the library_folder setting): replay scans Debrief saved (Scan this race on a review) and the
    saved reviews (official results and each track's corners);
  - the telemetry folder, found as Setup Lab finds it (the iracing_folder setting, else Documents\\iRacing\\telemetry):
    the newest MAX_FILES .ibt files;
  - RaceCap's folder, only when Debrief already knows one (the racecap_library setting). Nothing is ever written there.

The answer is cached (memory, then a file in the cache folder) under a key made of every input file's name, size and
change time and Debrief's and both modules' versions, so a new scan, review or telemetry file works it out again and
nothing else does. Two requests at once work it out once (carsetup.cached).

Telemetry is the slow part: each .ibt is read through once (about 0.05 to 0.6 s a file on the owner's PC, ~0.2 s on
average, so 400 files would take over a minute). So each file's reading (incidentmap.read_ibt, without its path, plus
the track's corners when they were worked out from it) is kept in its own cache file (FILES_CACHE, by name, size and
change time): after the first time a cold open reads none of them. A request reads new files for at most BUDGET_S;
the rest are read on a background thread (daemon, never waited for: closing or updating Debrief stops it between two
files) and the answer says "pending" so the page asks again. A partial answer is never cached. The answer and the
caches hold file and folder names only, never a full path; other drivers' names stay on this PC.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

from .. import app
from . import carsetup, settings_io

VERSION = 1                 # bump when the answer's shape changes
CACHE = "racing"            # folder in the cache folder
MAX_FILES = 400             # newest telemetry files read (the same as incidentmap.collect's default)
GET_ROUTES = ("api/racing",)
FILES_CACHE = "racing-files"    # folder in the cache folder: one reading per telemetry file
BUDGET_S = 3.0              # a request reads new telemetry files for at most this long; the rest in the background
SAVE_EVERY_S = 5.0          # the background reader saves what it has read this often


class Readings:
    """Each telemetry file's reading, kept in memory and in the cache folder, and the background reader."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.recs = None            # key -> reading (loaded from the cache folder on first use)
        self.dirty = False
        self.thread = None
        self.left = 0               # files the background reader still has to read
        self.total = 0

    def load(self):
        with self.lock:
            if self.recs is not None:
                return
            recs = {}
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and raw.get("version") == _file_version():
                    recs = {k: v for k, v in (raw.get("files") or {}).items() if isinstance(v, dict)}
            except (OSError, ValueError):
                recs = {}
            self.recs = recs

    def save(self, keep=None):
        with self.lock:
            if not self.dirty or self.recs is None:
                return
            if keep is not None:            # files no longer in the folder are dropped
                self.recs = {k: v for k, v in self.recs.items() if k in keep or self.thread is not None}
            data = {"version": _file_version(), "files": dict(self.recs)}
            self.dirty = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, allow_nan=False), encoding="utf-8")
            os.replace(tmp, self.path)
        except (OSError, ValueError) as e:
            app.say(f"Racing: couldn't save the telemetry readings ({type(e).__name__}).")

    def get(self, key):
        with self.lock:
            return (self.recs or {}).get(key)

    def put(self, key, rec):
        with self.lock:
            if self.recs is None:
                self.recs = {}
            self.recs[key] = rec
            self.dirty = True


_R_LOCK = threading.Lock()


def _file_version():
    im = app.optional_module("incidentmap")
    return [1, getattr(im, "VERSION", None)]


def readings(server, cache_dir) -> Readings:
    with _R_LOCK:
        r = getattr(server, "_racing_readings", None)
        path = Path(cache_dir) / FILES_CACHE / "files.json"
        if r is None or r.path != path:
            r = Readings(path)
            server._racing_readings = r
        return r


def file_key(f: Path):
    try:
        st = f.stat()
    except OSError:
        return None
    return f"{f.name}|{st.st_size}|{st.st_mtime_ns}"


def _stopping(server) -> bool:
    ev = getattr(server, "stopping", None)
    return bool(getattr(server, "closing", False)) or bool(ev is not None and ev.is_set())


def read_one(f: Path) -> dict:
    """One file's reading for the cache: no path in it."""
    im = app.optional_module("incidentmap")
    rec = dict(im.read_ibt(f))
    rec.pop("path", None)
    return rec


def telemetry(server, cache_dir, files, budget=None) -> tuple:
    """(readings of the files in order, files not read yet). Reads new files for at most `budget` seconds and starts
    the background reader for the rest. Never raises for one bad file."""
    R = readings(server, cache_dir)
    R.load()
    budget = BUDGET_S if budget is None else budget
    t0 = time.monotonic()
    out, missing, keys = [], [], set()
    for f in files:
        k = file_key(f)
        if k is None:
            continue
        keys.add(k)
        rec = R.get(k)
        if rec is None:
            if time.monotonic() - t0 >= budget or _stopping(server):
                missing.append(f)
                continue
            rec = read_one(f)
            R.put(k, rec)
        out.append(dict(rec, path=str(f)))         # the path only in memory, for the corners
    R.save(keep=keys)
    if missing:
        start_reader(server, R, missing)
    return out, missing


def start_reader(server, R: Readings, files):
    with R.lock:
        if R.thread is not None and R.thread.is_alive():
            return
        R.left, R.total = len(files), len(files)
        t = threading.Thread(target=_reader, args=(server, R, list(files)), daemon=True, name="racing-telemetry")
        R.thread = t
    t.start()


def _reader(server, R: Readings, files):
    last = time.monotonic()
    try:
        for f in files:
            if _stopping(server):
                break
            k = file_key(f)
            if k is not None and R.get(k) is None:
                R.put(k, read_one(f))
            with R.lock:
                R.left = max(0, R.left - 1)
            if time.monotonic() - last >= SAVE_EVERY_S:
                R.save()
                last = time.monotonic()
    except Exception:  # noqa: BLE001  (the next request reads what's left)
        import traceback
        app.say("ERROR reading telemetry for Racing:\n" + traceback.format_exc())
    finally:
        with R.lock:
            R.thread = None
            R.left = 0
        R.save()


def _corners_of(server, cache_dir):
    """incidentmap's corners for a track from one telemetry file, remembered with that file's reading."""
    im = app.optional_module("incidentmap")
    R = readings(server, cache_dir)

    def get(rec):
        p = rec.get("path")
        k = file_key(Path(p)) if p else None
        old = R.get(k) if k else None
        if old is not None and "corners_found" in old:
            return old["corners_found"] or None
        got = im._corners_from_ibt(p) if p else None
        if old is not None and k:
            R.put(k, dict(old, corners_found=[got[0], got[1]] if got else False))
        return got
    return get


def _versions() -> list:
    from .. import __version__
    out = [__version__, VERSION]
    for name in ("startreport", "incidentmap"):
        mod = app.optional_module(name)
        out.append([name, getattr(mod, "VERSION", None)])
    return out


def _stat(p: Path, label: str, out: list):
    try:
        st = p.stat()
    except OSError:
        return
    out.append([label, st.st_size, st.st_mtime_ns])


def _is_dir(p) -> bool:
    try:
        return bool(p) and Path(p).is_dir()
    except (OSError, ValueError):
        return False


def inputs(cfg) -> dict:
    """What the racing pages would read, and a stamp of it: {"library", "telemetry_folder", "racecap", "files" (the
    telemetry files), "stamp" (names, sizes, change times), "counts"}. Never raises."""
    from .. import racecap_link, replay_scan
    lib = str(cfg.get("library_folder") or "").strip()
    lib = lib if _is_dir(lib) else None
    tel = None
    try:
        tel = settings_io.telemetry_folder(cfg)
    except Exception:  # noqa: BLE001
        tel = None
    tel = tel if _is_dir(tel) else None
    rc = None
    v = str(cfg.get("racecap_library") or "").strip()
    if v:
        try:
            rc = racecap_link.races_root(v)
        except Exception:  # noqa: BLE001
            rc = None
    stamp, counts = [], {"scans": 0, "reviews": 0, "telemetry": 0}
    roots = []
    if lib:
        roots.append(("own", replay_scan.scans_root(lib)))
    if rc is not None:
        roots.append(("racecap", rc))
    for tag, root in roots:
        if not _is_dir(root):
            continue
        for _sub, _slug, d in racecap_link.race_dirs(Path(root)):
            n0 = len(stamp)
            try:
                for f in sorted((d / "scan").iterdir()) if (d / "scan").is_dir() else []:
                    _stat(f, f"{tag}/{d.name}/scan/{f.name}", stamp)
            except OSError:
                pass
            if len(stamp) > n0:
                counts["scans"] += 1
            for name in ("link.json", "results.json"):
                _stat(d / name, f"{tag}/{d.name}/{name}", stamp)
    if lib:
        rd = Path(lib) / "reviews"
        try:
            revs = sorted(rd.glob("*/review.json")) if rd.is_dir() else []
        except OSError:
            revs = []
        for p in revs:
            _stat(p, f"reviews/{p.parent.name}/review.json", stamp)
            _stat(p.parent / "result.json", f"reviews/{p.parent.name}/result.json", stamp)
        counts["reviews"] = len(revs)
    files = []
    if tel:
        try:
            got = []
            for f in Path(tel).glob("*.ibt"):
                try:
                    got.append((f.stat().st_mtime, f))
                except OSError:
                    continue
            files = [f for _m, f in sorted(got, key=lambda x: (-x[0], x[1].name))[:MAX_FILES]]
        except OSError:
            files = []
        for f in files:
            _stat(f, f"telemetry/{f.name}", stamp)
        counts["telemetry"] = len(files)
    return {"library": lib, "telemetry_folder": str(tel) if tel else None, "racecap": str(rc) if rc is not None else None,
            "files": files, "stamp": sorted(stamp), "counts": counts}


def cache_key(inp: dict) -> str:
    raw = json.dumps([_versions(), inp["stamp"], bool(inp["library"]), bool(inp["telemetry_folder"]), bool(inp["racecap"])],
                     sort_keys=True, default=str)
    return "racing-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def work_out(inp: dict, tel=None, corners_of=None) -> dict:
    """Both reports from one look at the files. Paths never reach the answer. tel: the telemetry files' readings
    (telemetry()); without it every file is read here."""
    im = app.optional_module("incidentmap")
    sr = app.optional_module("startreport")
    if im is None or sr is None:
        raise ValueError("The racing pages aren't available right now. Details were saved to debrief.log in "
                         + app.data_folder_label() + ".")
    if tel is None:
        src = im.collect(library_folder=inp["library"], racecap_folder=inp["racecap"], telemetry_files=inp["files"],
                         max_files=MAX_FILES)
    else:
        src = im.collect(library_folder=inp["library"], racecap_folder=inp["racecap"], telemetry=tel, corners_of=corners_of)
    found = {"scans": len(src.get("scans") or []), "telemetry": len(src.get("telemetry") or []),
             "reviews": len(src.get("reviews") or [])}
    starts = sr.build(src)
    incidents = im.build(src)
    return {"starts": starts, "incidents": incidents, "found": found}


def _scrub(obj, paths):
    """Belt and braces: any full folder path that slipped into a string becomes its last part."""
    if not paths:
        return obj
    if isinstance(obj, str):
        for p in paths:
            if p in obj:
                obj = obj.replace(p, Path(p).name)
        return obj
    if isinstance(obj, list):
        return [_scrub(x, paths) for x in obj]
    if isinstance(obj, dict):
        return {k: _scrub(v, paths) for k, v in obj.items()}
    return obj


def get_racing(h, q):
    cfg = h._cfg()
    ref = q.get("refresh")
    if ref not in (None, "", "0", "1"):
        raise ValueError("Bad request.")
    extra = set(q) - {"refresh"}
    if extra:
        raise ValueError("Bad request.")
    inp = inputs(cfg)
    key = cache_key(inp)
    if ref == "1":
        st = carsetup.state(h.server)
        with st.lock:
            st.mem.pop(key, None)
        try:
            (Path(cfg["cache_dir"]) / CACHE / (key + ".json")).unlink()
        except OSError:
            pass
    paths = [p for p in (inp["library"], inp["telemetry_folder"], inp["racecap"]) if p]
    st = carsetup.state(h.server)
    with st.lock:
        known = key in st.mem
    pending = None
    if not known and not (Path(cfg["cache_dir"]) / CACHE / (key + ".json")).is_file():
        tel, missing = telemetry(h.server, cfg["cache_dir"], inp["files"])
        if missing:         # some files are still being read: a partial answer, never cached
            R = readings(h.server, cfg["cache_dir"])
            with R.lock:
                left, total = (R.left or len(missing)), (R.total or len(missing))
            pending = {"left": left, "total": max(total, left), "files": len(inp["files"])}
            val, hit = _scrub(work_out(inp, tel, _corners_of(h.server, cfg["cache_dir"])), paths), False
        else:
            val, hit = carsetup.cached(h.server, cfg["cache_dir"], key,
                                       lambda: _scrub(work_out(inp, tel, _corners_of(h.server, cfg["cache_dir"])), paths),
                                       folder_name=CACHE)
            readings(h.server, cfg["cache_dir"]).save()
    else:
        val, hit = carsetup.cached(h.server, cfg["cache_dir"], key,
                                   lambda: _scrub(work_out(inp, *_tel_now(h.server, cfg, inp)), paths), folder_name=CACHE)
    where = {"library": bool(inp["library"]), "telemetry": bool(inp["telemetry_folder"]), "racecap": bool(inp["racecap"]),
             "racecap_set": bool(str(cfg.get("racecap_library") or "").strip()),
             "telemetry_name": Path(inp["telemetry_folder"]).name if inp["telemetry_folder"] else None}
    return h._json(_scrub({"ok": True, "cached": hit, "pending": pending, "where": where, "counts": inp["counts"], **val}, paths))


def _tel_now(server, cfg, inp):
    """Readings for a cache miss that raced a cache hit: read whatever is left now (rare)."""
    tel, _missing = telemetry(server, cfg["cache_dir"], inp["files"], budget=1e9)
    return tel, _corners_of(server, cfg["cache_dir"])


def get(h, r, q):
    return {"api/racing": get_racing}[r](h, q)
