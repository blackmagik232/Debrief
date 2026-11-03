"""Debrief 2.18 "Car vs car" in the window's server (debrief/carcompare.py does the work).

    GET api/carcompare[?track=&class=&a=&b=&refresh=1]

Reads (all read only, nothing fetched): the results tables kept beside race reviews (field.json), replay scans
(Debrief's own and RaceCap's, when Debrief knows RaceCap's folder) and the telemetry files, through Setup Lab's
index and its cache. track and class pick the comparison (default: the pair with most drivers); a and b pick the two
cars for "Where each car is faster" (default: the two with most clean laps of his). The answer is cached (memory, then
a file) under a key made of every input file's name, size and change time, the query and the versions. It holds
file and folder names only, never a full path.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .. import app
from . import carsetup, racing

VERSION = 1
CACHE = "carcompare"
GET_ROUTES = ("api/carcompare",)
MAX_FILES_PER_CAR = 8          # newest telemetry files per car read for laps
MAX_TEXT = 120


def _versions() -> list:
    from .. import __version__
    mod = app.optional_module("carcompare")
    return [__version__, VERSION, getattr(mod, "VERSION", None)]


def inputs(cfg) -> dict:
    inp = racing.inputs(cfg)         # scans, reviews and telemetry files, stamped
    stamp = list(inp["stamp"])
    if inp["library"]:
        rd = Path(inp["library"]) / "reviews"
        try:
            for p in sorted(rd.glob("*/field.json")) if rd.is_dir() else []:
                racing._stat(p, f"reviews/{p.parent.name}/field.json", stamp)
        except OSError:
            pass
    inp["stamp"] = sorted(stamp)
    return inp


def _telemetry(cfg, inp) -> list:
    """Setup Lab's index records of the telemetry folder (its cache: files read once), each with its full path."""
    if not inp["telemetry_folder"]:
        return []
    from .. import setuplab
    from .server import setuplab_cache_path
    try:
        idx = setuplab.index(inp["telemetry_folder"], cache_path=setuplab_cache_path(cfg))
    except Exception:  # noqa: BLE001
        app.say("Car vs car: the telemetry folder couldn't be read.")
        return []
    out = []
    for r in idx.get("files") or []:
        car, tr = r.get("car") or {}, r.get("track") or {}
        clean = [x for x in ((r.get("detail") or {}).get("laps") or []) if x.get("clean") and x.get("time")]
        name = " ".join(x for x in (tr.get("name"), f"({tr['config']})" if tr.get("config") else "") if x)
        out.append({"file": r.get("file"), "path": str(Path(inp["telemetry_folder"]) / str(r.get("file"))),
                    "car": car.get("name") or car.get("path") or "", "car_class": car.get("class") or "",
                    "track": name, "clean": len(clean), "sort": r.get("sort") or 0})
    return out


def work_out(cfg, inp, q) -> dict:
    cc = app.optional_module("carcompare")
    if cc is None:
        raise ValueError("Car vs car isn't available right now. Details were saved to debrief.log in "
                         + app.data_folder_label() + ".")
    got = cc.collect(inp["library"], inp["racecap"])
    races = got["races"]
    me = sorted({str(r.get("me")) for r in _field_docs(inp["library"]) if r.get("me")})
    tel = _telemetry(cfg, inp)
    opts = cc.options(races)
    # telemetry-only pairs (two of his cars in one class at one track) are options too
    tnames = cc._Names(cc._same_track)
    for o in opts:
        tnames.key(o["track"])
    for t in tel:
        t["_track"] = tnames.key(t["track"]) if t["track"] else ""
        t["_class"] = cc.class_key(t["car"], t["car_class"])
    for t in tel:
        if not t["_track"] or not t["clean"]:
            continue
        o = next((o for o in opts if o["track"] == t["_track"] and o["class"] == t["_class"]), None)
        if o is None:
            o = {"track": t["_track"], "class": t["_class"], "cars": [], "drivers": 0}
            opts.append(o)
        if not any(cc._same_car(c, t["car"]) for c in o["cars"]):
            o["cars"] = sorted(o["cars"] + [t["car"]])
    opts.sort(key=lambda o: (-(len(o["cars"]) > 1), -o["drivers"], o["track"], o["class"]))
    track, cls = q.get("track") or "", q.get("class") or ""
    pick = next((o for o in opts if o["track"] == track and o["class"] == cls), None) or (opts[0] if opts else None)
    out = {"options": opts[:200], "notes": got["notes"], "races": len(races), "telemetry_files": len(tel),
           "selected": None, "compare": None, "where": None, "tel_cars": []}
    if pick is None:
        return out
    out["selected"] = {"track": pick["track"], "class": pick["class"]}
    out["compare"] = cc.compare(races, pick["track"], pick["class"], me=me)
    # where each car is faster: his own telemetry in two cars of this class at this track
    mine = [t for t in tel if t["_track"] == pick["track"] and t["_class"] == pick["class"] and t["clean"]]
    cars = {}
    for t in mine:
        k = next((c for c in cars if cc._same_car(c, t["car"])), t["car"])
        cars.setdefault(k, []).append(t)
    out["tel_cars"] = [{"car": c, "clean_laps": sum(t["clean"] for t in v), "files": len(v)}
                       for c, v in sorted(cars.items(), key=lambda kv: -sum(t["clean"] for t in kv[1]))]
    names = [c["car"] for c in out["tel_cars"]]
    a = next((c for c in names if c == q.get("a")), names[0] if names else None)
    b = next((c for c in names if c == q.get("b") and c != a), next((c for c in names if c != a), None))
    if a and b:
        from .. import cornercoach
        laps = {}
        for car in (a, b):
            files = sorted(cars[car], key=lambda t: -t["sort"])[:MAX_FILES_PER_CAR]
            laps[car], _info = cornercoach.load_laps([t["path"] for t in files])
        out["where"] = cc.where_faster(laps[a], laps[b], a, b)
    return out


def _field_docs(lib) -> list:
    out = []
    if not lib:
        return out
    try:
        for p in sorted((Path(lib) / "reviews").glob("*/field.json")):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(d, dict):
                    out.append(d)
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    return out


def get_carcompare(h, q):
    cfg = h._cfg()
    extra = set(q) - {"refresh", "track", "class", "a", "b"}
    if extra or q.get("refresh") not in (None, "", "0", "1") or any(len(str(v)) > MAX_TEXT for v in q.values()):
        raise ValueError("Bad request.")
    inp = inputs(cfg)
    raw = json.dumps([_versions(), inp["stamp"], bool(inp["library"]), bool(inp["telemetry_folder"]),
                      bool(inp["racecap"]), [q.get(k) or "" for k in ("track", "class", "a", "b")]], sort_keys=True, default=str)
    key = "carcompare-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]
    if q.get("refresh") == "1":
        st = carsetup.state(h.server)
        with st.lock:
            st.mem.pop(key, None)
        try:
            (Path(cfg["cache_dir"]) / CACHE / (key + ".json")).unlink()
        except OSError:
            pass
    val, hit = carsetup.cached(h.server, cfg["cache_dir"], key, lambda: work_out(cfg, inp, q), folder_name=CACHE)
    paths = [p for p in (inp["library"], inp["telemetry_folder"], inp["racecap"]) if p]
    where = {"library": bool(inp["library"]), "telemetry": bool(inp["telemetry_folder"]), "racecap": bool(inp["racecap"])}
    return h._json(racing._scrub({"ok": True, "cached": hit, "found": where, **val}, paths))


def get(h, r, q):
    return {"api/carcompare": get_carcompare}[r](h, q)
