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
nothing else does. Two requests at once work it out once (carsetup.cached). It is worked out on the request: about a
second for a few races; the telemetry reader keeps each file's reading in memory, so a new race only reads the new
file. The answer holds file and folder names only, never a full path.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .. import app
from . import carsetup, settings_io

VERSION = 1                 # bump when the answer's shape changes
CACHE = "racing"            # folder in the cache folder
MAX_FILES = 400             # newest telemetry files read (the same as incidentmap.collect's default)
GET_ROUTES = ("api/racing",)


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


def work_out(inp: dict) -> dict:
    """Both reports from one look at the files. Paths never reach the answer."""
    im = app.optional_module("incidentmap")
    sr = app.optional_module("startreport")
    if im is None or sr is None:
        raise ValueError("The racing pages aren't available right now. Details were saved to debrief.log in "
                         + app.data_folder_label() + ".")
    src = im.collect(library_folder=inp["library"], racecap_folder=inp["racecap"], telemetry_files=inp["files"],
                     max_files=MAX_FILES)
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
    val, hit = carsetup.cached(h.server, cfg["cache_dir"], key, lambda: work_out(inp), folder_name=CACHE)
    paths = [p for p in (inp["library"], inp["telemetry_folder"], inp["racecap"]) if p]
    where = {"library": bool(inp["library"]), "telemetry": bool(inp["telemetry_folder"]), "racecap": bool(inp["racecap"]),
             "racecap_set": bool(str(cfg.get("racecap_library") or "").strip()),
             "telemetry_name": Path(inp["telemetry_folder"]).name if inp["telemetry_folder"] else None}
    return h._json(_scrub({"ok": True, "cached": hit, "where": where, "counts": inp["counts"], **val}, paths))


def get(h, r, q):
    return {"api/racing": get_racing}[r](h, q)
