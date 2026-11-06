"""Debrief 2.15 "Car and setup" in the window's server: the balance map, the pressure learner, the setup share
text and a teammate's lap as a reference (debrief/balance.py, pressurelearn.py, setupshare.py, teamref.py).

Kept in its own module so the server only needs a few hook lines (server.Handler.do_GET / do_POST call get(),
post() and upload(); the coach asks coach_reference() and coach_extra()).

    GET  api/balance?car=&track=[&setup=A|all]   how the car handles (cached in the cache folder)
    GET  api/pressure?car=&track=                 tyre pressures over time (cached)
    POST api/pressure/predict {car, track, cold_front, cold_rear, track_temp}
    GET  api/setuplab/share?car=&track=[&a=&b=]   "what changed" text for Discord (copy only, never posted)
    POST api/teamref/upload?name=&car=&track=[&keep=1]   a teammate's .ibt as the raw body
    GET  api/teamref?car=&track=                  the teammate lap kept for this car + track
    GET  api/teamref/cards?car=&track=            corner coach cards against that lap
    GET  api/teamref/laps?car=&track=             2.18: the teammate laps Debrief already has here (and how to get one)
    GET  api/teamref/for-review?dir=              2.18: the Practice session of this race's car and track, if any
    POST api/teamref/keep {car, track, keep}       tick or untick "Keep for this car and track" for the lap in use
    POST api/teamref/forget {car, track}
    POST api/teamref/coach {car, track, on}       coach against that lap (off by default)

A teammate's file is written to a private temp folder only for the checks (teamref.load_reference) and deleted
straight after; it is never copied anywhere else. The lap worked out from it stays in memory for the server's
life, and only when he ticks "Keep for this car and track" is that worked-out lap (not the file) saved under the
cache folder.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import threading
import time
import traceback
from dataclasses import asdict
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import numpy as np

from .. import app
from . import settings_io

VERSION = 1                       # bump when a cached result's shape changes
CACHE = "carsetup"                # folder in the cache folder: one JSON file per worked-out result
TEAM_DIR = "teamref"              # folder in the cache folder: kept teammate laps (only when he ticks Keep)
MEM = 24                          # results kept in memory
DISK = 120                        # result files kept on disk (oldest go first)
BALANCE_FILES = 20                # the balance map reads at most this many recent files of one setup
NAME_OK = re.compile(r"^[^\x00-\x1f/\\<>:\"|?*]{1,160}$")
SETUP_ID_OK = re.compile(r"^(all|[A-Z]{1,3})$")

GET_ROUTES = ("api/balance", "api/handling", "api/pressure", "api/setuplab/share", "api/teamref", "api/teamref/cards",
              "api/teamref/laps", "api/teamref/for-review")
POST_ROUTES = ("api/pressure/predict", "api/handling/feel", "api/teamref/forget", "api/teamref/coach", "api/teamref/keep")
UPLOAD_ROUTE = "api/teamref/upload"


# ---------------------------------------------------------------- state kept on the server
class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.mem = {}                 # cache key -> value
        self.busy = {}                # cache key -> lock while being worked out
        self.made = 0                 # results worked out (not read back): for tests
        self.team = {}                # (car, track) -> the teammate entry in use (see _team_entry)
        self.team_all = {}            # 2.21: (car, track, driver) -> every teammate lap loaded, so two teammates never overwrite each other
        self.team_cards = {}          # (car, track, paths key) -> corner coach report against the teammate
        self.coach_on = None          # (car, track) the coach uses the teammate's lap for, or None


_STATE_LOCK = threading.Lock()


def state(server) -> State:
    with _STATE_LOCK:
        st = getattr(server, "_carsetup", None)
        if st is None:
            st = State()
            server._carsetup = st
        return st


def _versions() -> list:
    from .. import __version__
    out = [__version__, VERSION]
    for name in ("balance", "pressurelearn", "setuplab", "ibt", "handling", "setupadvice"):
        mod = app.optional_module(name)
        out.append([name, getattr(mod, "VERSION", None), getattr(mod, "CACHE_VERSION", None)])
    return out


def cache_key(kind: str, paths=(), extra=None) -> str:
    """kind + every file's name, size and change time + Debrief's and the modules' versions. Raises OSError when a
    file is gone."""
    files = []
    for p in paths:
        st = Path(p).stat()
        files.append([Path(p).name, st.st_size, st.st_mtime_ns])
    raw = json.dumps([_versions(), kind, sorted(files), extra], sort_keys=True, default=str)
    return kind + "-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def cached(server, cache_dir, key: str, make, folder_name: str = CACHE):
    """The value for key: from memory, else Debrief's own cache file, else make() (once, even for two requests).
    folder_name: the folder in the cache folder (2.16's racing pages keep theirs apart)."""
    from .server import _jsonable
    st = state(server)
    with st.lock:
        if key in st.mem:
            v = st.mem.pop(key)
            st.mem[key] = v
            return v, True
        klock = st.busy.setdefault(key, threading.Lock())
    with klock:
        with st.lock:
            if key in st.mem:
                return st.mem[key], True
        folder = Path(cache_dir) / folder_name
        path = folder / (key + ".json")
        val, hit = None, True
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and raw.get("key") == key:
                val = raw.get("value")
        except (OSError, ValueError):
            val = None
        if val is None:
            hit = False
            val = _jsonable(make())
            with st.lock:
                st.made += 1
            try:
                folder.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps({"key": key, "value": val}, allow_nan=False), encoding="utf-8")
                os.replace(tmp, path)
                _prune(folder)
            except (OSError, ValueError) as e:
                app.say(f"Car and setup: couldn't save a result to the cache ({type(e).__name__}: {e}).")
        with st.lock:
            st.mem[key] = val
            while len(st.mem) > MEM:
                st.mem.pop(next(iter(st.mem)))
            st.busy.pop(key, None)
    return val, hit


def _prune(folder: Path):
    try:
        files = sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime)
        for p in files[:-DISK]:
            p.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------- inputs
def _ident(v, what):
    from .server import IDENT_OK
    if not isinstance(v, str) or not IDENT_OK.match(v):
        raise ValueError(f"Pick a {what}.")
    return v


def _combo(q):
    return _ident(q.get("car"), "car"), _ident(q.get("track"), "track")


def _index(server, cfg):
    from .server import setuplab_cache_path
    idx, _keys = server.setuplab.load(setuplab_cache_path(cfg))
    if idx is None:
        raise ValueError("Read your telemetry first.")
    return idx


def _records(idx, car, track) -> list:
    return [r for r in idx.get("files") or [] if str((r.get("car") or {}).get("path") or "").lower() == car.lower()
            and str((r.get("track") or {}).get("code") or "").lower() == track.lower()]


def _num(v, lo, hi, what, required=True):
    if v is None or v == "":
        if required:
            raise ValueError(f"Fill in the {what}.")
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"The {what} must be a number.")
    if not math.isfinite(x) or not lo <= x <= hi:
        raise ValueError(f"The {what} must be between {lo:g} and {hi:g}.")
    return x


# ---------------------------------------------------------------- the balance map
def balance_setups(recs: list) -> list:
    from .. import setuplab
    g = setuplab.group(recs)
    out = []
    for s in g["setups"]:
        if not s.get("fingerprint"):
            continue
        last = max((r.get("sort") or 0) for r in recs if r["file"] in s["files"])
        out.append({"id": s["id"], "label": s["label"], "files": list(s["files"]), "last": s.get("last") or "",
                    "_sort": last, "_setup": s.get("setup") or {}})
    out.sort(key=lambda s: -s["_sort"])
    return out


def balance_report(paths: list) -> dict:
    """The balance map over these files (no setup ideas for a fixed series: see _apply_series)."""
    from .. import balance
    rep = balance.analyze_files([str(p) for p in paths])
    rep.pop("params", None)
    rep.pop("method", None)
    cs = rep.get("countersteer") or {}
    cs["events"] = (cs.get("events") or [])[:40]          # the window only needs the per-corner counts
    rep["files"] = len(paths)
    return rep


def _apply_series(rep: dict, kind, setup: dict | None = None, last_change: dict | None = None) -> dict:
    """The setup ideas for this series and this setup, worked out again on the cached map (cheap): a fixed series
    gets in-car dials or driving advice; an open one never offers simply undoing his last change unless the laps
    showed it slower."""
    from .. import balance
    rep = dict(rep)
    if rep.get("ok") and (setup is not None or kind == "fixed"):
        rep["setup_ideas"] = balance.setup_ideas(rep, setup, series_setup=kind, last_change=last_change,
                                                 dials=(rep.get("info") or {}).get("dials"))
    rep["series_setup"] = kind
    return rep


def balance_laps_note(bal_laps, table_laps, label: str, files_used: int, files_all: int) -> str | None:
    """Why the balance map's lap count isn't the "Which was faster" table's, in plain words (None when they match)."""
    if bal_laps is None or table_laps is None or bal_laps == table_laps:
        return None
    if bal_laps > table_laps:
        return (f"Which was faster counts {table_laps} of these laps for {label}: it also leaves out warm-up laps, "
                f"laps alongside another car and laps far off that run's pace, to compare pace fairly. The balance map "
                f"keeps them, because they still show how the car steers.")
    why = (f"it reads only the newest {files_used} of {files_all} files" if files_used < files_all else
           "it leaves out laps where the steering or turning wasn't recorded all the way round, and laps more than "
           "7% off your typical lap")
    return f"The balance map uses {bal_laps} of the {table_laps} laps Which was faster counts for {label}: {why}."


def _selection(h, q):
    """Which setup's telemetry to read for this car and track (the newest setup, or the one picked, or all), shared by
    the balance map and the handling read (2.23). None when there is no telemetry with a setup yet."""
    server = h.server
    cfg = h._cfg()
    h._module("balance")
    car, track = _combo(q)
    want = q.get("setup") or None
    if want is not None and not SETUP_ID_OK.match(want):
        raise ValueError("Pick a setup from the list.")
    idx = _index(server, cfg)
    folder = settings_io.telemetry_folder(cfg)
    recs = _records(idx, car, track)
    setups = balance_setups(recs)
    pub = [{k: s[k] for k in ("id", "label", "last")} | {"files": len(s["files"])} for s in setups]
    from .. import setuplab
    if not setups:
        return {"none": True, "setups": []}
    pick = next((s for s in setups if s["id"] == want), None) if want and want != "all" else None
    if want == "all":
        names = [r["file"] for r in sorted(recs, key=lambda r: -(r.get("sort") or 0))]
        chosen = "all"
    else:
        pick = pick or setups[0]
        names = [n for n in sorted(pick["files"], key=lambda n: -next((r.get("sort") or 0) for r in recs
                                                                         if r["file"] == n))]
        chosen = pick["id"]
    names = names[:BALANCE_FILES]
    paths = [Path(folder) / n for n in reversed(names)]
    try:
        key = cache_key("balance", paths)
    except OSError:
        raise ValueError("Some telemetry files have gone. Press Read my telemetry again.")
    kind = _series_kind(car, track, idx)
    newest = max(recs, key=lambda r: r.get("sort") or 0)
    setup = newest.get("setup") if chosen == "all" else pick["_setup"]
    last = setuplab.change_into(recs, chosen) if chosen != "all" and kind != "fixed" else None
    mine = recs if chosen == "all" else [r for r in recs if r["file"] in set(pick["files"])]
    return {"none": False, "car": car, "track": track, "idx": idx, "recs": recs, "setups": setups, "pub": pub,
            "pick": pick, "chosen": chosen, "paths": paths, "key": key, "kind": kind, "setup": setup, "last": last,
            "mine": mine}


def get_balance(h, q):
    server = h.server
    cfg = h._cfg()
    S = _selection(h, q)
    if S["none"]:
        return h._json({"ok": True, "report": None, "setups": [], "setup": None,
                        "message": "No telemetry with a setup for this car and track yet."})
    from .. import setuplab
    paths, pick, chosen, recs, mine = S["paths"], S["pick"], S["chosen"], S["recs"], S["mine"]
    rep, hit = cached(server, cfg["cache_dir"], S["key"], lambda: balance_report(paths))
    last, kind, setup = S["last"], S["kind"], S["setup"]
    table, _d = setuplab.pace_laps(mine)
    note = balance_laps_note(rep.get("laps") if rep.get("ok") else None, len(table),
                             "all setups" if chosen == "all" else pick["label"], len(paths), len(mine))
    return h._json({"ok": True, "report": _apply_series(rep, kind, setup or {}, last), "setups": S["pub"],
                    "setup": chosen, "cached": hit, "files": len(paths), "laps_note": note,
                    "last_change": ({k: last[k] for k in ("from", "to", "verdict", "slower")} if last else None)})


# ---------------------------------------------------------------- 2.23 automatic handling read and setup advice
HANDLING_STORE = "handling"          # settings_io extra: {"feel": {car|track: {issue id: {answer, at}}}, "history": {...}}


def handling_report(paths: list, kind) -> dict:
    """The handling read over these files (cached by file; the advice is worked out again on it, which is cheap)."""
    from .. import balance, handling
    laps, info = balance.load_laps([str(p) for p in paths])
    rep = handling.analyze(laps, info=info, series_setup=kind)
    rep["files"] = len(paths)
    rep["dials"] = list(info.get("dials") or [])
    return rep


def _store() -> dict:
    d = settings_io.load_extra(HANDLING_STORE)
    d.setdefault("feel", {})
    d.setdefault("history", {})
    return d


def _save_store(d: dict) -> None:
    try:
        settings_io.save_extra(HANDLING_STORE, d)
    except OSError:
        pass                                  # the advice still shows; only the memory of it is lost


def _prev_fp(S) -> str | None:
    """Fingerprint of the setup he drove just before the chosen one (None when there is no earlier one)."""
    from .. import setuplab
    last = S.get("last")
    if not last:
        return None
    by = {s["id"]: s for s in setuplab.group(S["recs"])["setups"]}
    return (by.get(last.get("from_id")) or {}).get("fingerprint")


def get_handling(h, q):
    """GET api/handling?car=&track=[&setup=]: the automatic handling read and the advice, with his earlier answers
    and the setup history. Nothing to press: the Set-Up Lab page and the Practice session page ask for it."""
    import time
    from .. import handling, setupadvice, setuplab
    server = h.server
    cfg = h._cfg()
    S = _selection(h, q)
    if S["none"]:
        msg = "No telemetry with a setup for this car and track yet, so there is nothing to read."
        return h._json({"ok": True, "report": None, "advice": {"ok": False, "empty": msg, "items": [], "top": None,
                                                              "card": "Car balance: " + msg}, "message": msg})
    rep, hit = cached(server, cfg["cache_dir"], cache_key("handling", S["paths"], extra=[handling.VERSION]),
                      lambda: handling_report(S["paths"], S["kind"]))
    st = _store()
    hk = setupadvice.history_key(S["car"], S["track"])
    feel = (st["feel"].get(hk) or {})
    setup = S["setup"] or {}
    advice = setupadvice.advise(rep, setup, series_setup=S["kind"], last_change=S["last"], dials=rep.get("dials"),
                                feel=feel)
    fp = setuplab.fingerprint(setup) if setup else None
    changed = False
    if S["chosen"] != "all":
        from .. import ibt
        changed |= setupadvice.evaluate(st["history"], hk, fp, _prev_fp(S), ibt.flatten(setup),
                                        S["last"], [i["id"] for i in rep.get("setup_issues") or []])
        changed |= setupadvice.record_shown(st["history"], hk, advice.get("top"), fp,
                                            time.strftime("%Y-%m-%d %H:%M"))
    if changed:
        _save_store(st)
    return h._json({"ok": True, "report": {k: v for k, v in rep.items() if k != "dials"}, "advice": advice,
                    "setups": S["pub"], "setup": S["chosen"], "cached": hit, "series_setup": S["kind"],
                    "history": setupadvice.history_view(st["history"], hk), "feel": feel})


def post_handling_feel(h, body):
    """POST api/handling/feel {car, track, issue, answer: "agree" | "disagree" | "clear"}: "How did the car feel?"
    Kept locally; it moves that issue's sureness one step next time."""
    import time
    car, track = _combo(body)
    issue = str(body.get("issue") or "")
    answer = str(body.get("answer") or "")
    if not re.fullmatch(r"[a-z]{4,20}:[a-z]{3,6}:[a-z]{3,6}", issue):
        raise ValueError("That isn't one of the car-balance items.")
    if answer not in ("agree", "disagree", "clear"):
        raise ValueError("Answer agree, not what I felt, or clear.")
    from .. import setupadvice
    st = _store()
    hk = setupadvice.history_key(car, track)
    mine = st["feel"].setdefault(hk, {})
    if answer == "clear":
        mine.pop(issue, None)
    else:
        mine[issue] = {"answer": answer, "at": time.strftime("%Y-%m-%d %H:%M")}
    try:
        settings_io.save_extra(HANDLING_STORE, st)
    except OSError:
        raise ValueError("Debrief couldn't save your answer. Check that its settings folder can be written to.")
    return h._json({"ok": True, "feel": mine})


def _series_kind(car, track, idx):
    """open | fixed | None, as Setup Lab decides it (his own choice first, else the season schedule, else the
    telemetry and the cars known to race fixed setups only)."""
    from .. import setuplab
    from . import server as S
    try:
        saved = settings_io.load_extra("setuplab").get("series") or {}
        v = saved.get(S.series_key(car, track)) if isinstance(saved, dict) else None
        if v in ("open", "fixed"):
            return v
        combo = next((c for c in setuplab.combos(idx) if (c.get("car_path") or "").lower() == car.lower()
                      and (c.get("track_code") or "").lower() == track.lower()), None)
        kind = S.schedule_series_setup(combo, None)[0]
        if kind in ("open", "fixed"):
            return kind
        guess = setuplab.series_setup_guess(_records(idx, car, track), car, (combo or {}).get("car"))[0]
        return guess if guess in ("open", "fixed") else setuplab.remembered_for_car(saved, car)
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- tyre pressures over time
def pressure_report(server, cfg, car, track) -> dict:
    from .. import pressurelearn
    idx = _index(server, cfg)
    recs = [r for r in idx.get("files") or [] if str((r.get("car") or {}).get("path") or "").lower() == car.lower()]
    sig = sorted([r.get("file"), r.get("size"), r.get("mtime")] for r in recs)
    key = "pressure-" + hashlib.sha1(json.dumps([_versions(), car.lower(), track.lower(), sig],
                                                default=str).encode("utf-8")).hexdigest()[:24]

    def make():
        rep = pressurelearn.learn({"files": recs}, car, track)
        rep.pop("params", None)
        return rep
    rep, hit = cached(server, cfg["cache_dir"], key, make)
    return rep, hit


def get_pressure(h, q):
    cfg = h._cfg()
    h._module("pressurelearn")
    car, track = _combo(q)
    rep, hit = pressure_report(h.server, cfg, car, track)
    return h._json({"ok": True, "report": rep, "cached": hit, "can_predict": bool(rep.get("prediction"))})


def post_pressure_predict(h, body):
    cfg = h._cfg()
    pl = h._module("pressurelearn")
    car, track = _combo(body)
    cf = _num(body.get("cold_front"), 50, 500, "cold front pressure")
    cr = _num(body.get("cold_rear"), 50, 500, "cold rear pressure")
    tt = _num(body.get("track_temp"), -20, 80, "track temperature", required=False)
    rep, _hit = pressure_report(h.server, cfg, car, track)
    model = rep.get("prediction")
    if not model:
        raise ValueError("Not enough runs with hot pressures yet to predict them.")
    p = pl.predict_hot(model, cf, cr, tt)
    if not p:
        raise ValueError("The prediction couldn't be worked out.")
    t = tt if tt is not None else float(np.mean(model["temp_range"]))
    text = (f"At {t:.0f} °C track: front about {p['front']:.0f} kPa hot, rear about {p['rear']:.0f} kPa hot "
            f"(typical miss about {p['miss_kpa']} kPa).")
    if p.get("outside_range"):
        lo, hi = model["temp_range"]
        text += f" Your runs were at {lo:.0f} to {hi:.0f} °C, so this is a stretch."
    for axle in p.get("cold_outside") or []:
        lo, hi = (model.get("cold_range") or {}).get(axle) or (None, None)
        if lo is not None:
            rng = f"{lo:.0f} kPa" if round(lo) == round(hi) else f"{lo:.0f} to {hi:.0f} kPa"
            text += (f" Your runs started the {axle} at {rng} cold, so this {axle} pressure is outside what they "
                     f"show: treat it as a rough guess.")
    return h._json({"ok": True, "prediction": p, "text": text, "basis": model.get("basis")})


# ---------------------------------------------------------------- share what changed
def get_setuplab_share(h, q):
    cfg = h._cfg()
    setuplab = h._module("setuplab")
    share = h._module("setupshare")
    car, track = _combo(q)
    a, b = q.get("a") or None, q.get("b") or None
    for v in (a, b):
        if v is not None and not re.fullmatch(r"[A-Z]{1,3}", v):
            raise ValueError("Pick two setups from the list.")
    idx = _index(h.server, cfg)
    folder = settings_io.telemetry_folder(cfg)
    rep = setuplab.report(folder, car, track, idx=dict(idx, missing_folder=not folder.is_dir()))
    out = share.share(rep, a, b)
    return h._json({"ok": True, "text": out["text"], "markdown": out["markdown"], "ready": bool(out["ok"]),
                    "pair": list(out["pair"]) if out.get("pair") else None, "chars": out["chars"]})


# ---------------------------------------------------------------- a teammate's lap
def _team_key(car, track):
    return (str(car).lower(), str(track).lower())


def dkey(summary) -> str:
    """2.21: who a kept lap belongs to, as one lower-case key ("alex teammate"); 'unknown' when the file names no one."""
    d = " ".join(str((summary or {}).get("driver") or "").lower().split())
    return d if d and d != "your teammate" else "unknown"


def _team_file(cfg, car, track, driver=None) -> Path:
    """The kept file. 2.21: one per driver (a second teammate no longer replaces the first). driver=None is the
    pre-2.21 name, one file per car and track, which is still read and migrated."""
    parts = _team_key(car, track) + ((driver,) if driver is not None else ())
    k = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:20]
    return Path(cfg["cache_dir"]) / TEAM_DIR / (k + ".json")


def _read_team_file(f, k=None):
    """(raw, entry) from one kept file, or None. k = (car, track) key to require."""
    try:
        raw = json.loads(f.read_text(encoding="utf-8"))
        if raw.get("version") != VERSION:
            return None
        if k is not None and _team_key(raw.get("car_path"), raw.get("track_code")) != k:
            return None
        return raw, {"summary": raw["summary"], "livecoach": _ref_from_plain(raw.get("livecoach")),
                     "cornercoach": _from_plain(raw.get("cornercoach")), "kept": True}
    except (OSError, ValueError, KeyError, AttributeError, TypeError):
        return None
    except Exception:  # noqa: BLE001
        app.say("Car and setup: a kept teammate lap couldn't be read:\n" + traceback.format_exc())
        return None


def kept_files(cfg, car, track) -> list:
    """Every kept teammate lap for this car and track, newest first: [(path, raw, entry)]. Old one-per-track files
    count too (their driver comes from the file), so nothing kept before 2.21 is lost."""
    k = _team_key(car, track)
    folder = Path(cfg["cache_dir"]) / TEAM_DIR
    try:
        files = sorted(folder.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True) if folder.is_dir() else []
    except OSError:
        return []
    out = []
    for f in files:
        r = _read_team_file(f, k)
        if r:
            out.append((f, r[0], r[1]))
    return out


def _to_plain(o):
    if isinstance(o, np.ndarray):
        return {"__nd__": o.tolist(), "dtype": str(o.dtype)}
    if isinstance(o, dict):
        return {str(k): _to_plain(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_to_plain(v) for v in o]
    if isinstance(o, (np.floating, np.integer, np.bool_)):
        return o.item()
    return o


def _from_plain(o):
    if isinstance(o, dict):
        if "__nd__" in o:
            return np.asarray(o["__nd__"], dtype=o.get("dtype") or float)
        return {k: _from_plain(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_from_plain(v) for v in o]
    return o


def _ref_to_plain(ref) -> dict | None:
    if ref is None:
        return None
    d = {k: getattr(ref, k) for k in ("lap_time", "length_m", "car", "track", "source", "pct", "speed", "brake",
                                       "throttle", "gear", "time")}
    d["corners"] = [asdict(c) for c in ref.corners]
    return _to_plain(d)


def _ref_from_plain(d):
    from .. import livecoach
    if not d:
        return None
    d = _from_plain(d)
    corners = [livecoach.Corner(**c) for c in d.pop("corners", [])]
    return livecoach.Reference(corners=corners, **d)


def _team_entry(res: dict, kept: bool) -> dict:
    return {"summary": dict(res["summary"]), "livecoach": res.get("livecoach"), "cornercoach": res.get("cornercoach"),
            "kept": kept}


def _team_get(server, cfg, car, track):
    """The teammate entry in use for this car + track: from memory, else the newest kept file (his choice), else None."""
    st = state(server)
    k = _team_key(car, track)
    with st.lock:
        e = st.team.get(k)
    if e is not None:
        return e
    kept = kept_files(cfg, car, track)
    if not kept:
        return None
    e = kept[0][2]
    with st.lock:
        st.team.setdefault(k, e)
        st.team_all.setdefault(k + (dkey(e["summary"]),), e)
        return st.team[k]


def _team_save(cfg, car, track, e) -> bool:
    d = dkey(e["summary"])
    f = _team_file(cfg, car, track, d)
    data = {"version": VERSION, "car_path": car, "track_code": track, "summary": e["summary"],
            "livecoach": _ref_to_plain(e.get("livecoach")), "cornercoach": _to_plain(e.get("cornercoach"))}
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, f)
    except (OSError, ValueError) as x:
        app.say(f"Car and setup: couldn't keep the teammate lap ({type(x).__name__}: {x}).")
        return False
    old = _team_file(cfg, car, track)        # the pre-2.21 file for this car and track: moved to the new name
    try:
        r = _read_team_file(old, _team_key(car, track))
        if r and dkey(r[0].get("summary")) == d:
            old.unlink()
    except OSError:
        pass
    return True


def _team_public(e, server, car, track) -> dict | None:
    if not e:
        return None
    s = e["summary"]
    on = state(server).coach_on == _team_key(car, track)
    return {"driver": s.get("driver"), "label": s.get("label"), "lap_text": s.get("lap_text"),
            "lap_time": s.get("lap_time"), "car": s.get("car"), "track": s.get("track"), "corners": s.get("corners"),
            "kept": bool(e.get("kept")), "coach_on": on, "short": _short_label(s)}


def _short_label(s) -> str:
    d = str(s.get("driver") or "").strip()
    if not d or d == "Your teammate":
        return "your teammate's lap"
    from .. import teamref
    return f"{teamref.possessive(d)} lap"


MAX_TEAM_BODY = None    # None: teamref.MAX_IBT (tests set a small one)


def upload(h):
    """A teammate's .ibt as the raw body, its name in ?name=, his car and track in ?car=&track=, ?keep=1 to keep the
    worked-out lap. The file sits in a private temp folder for teamref's checks and is deleted straight after."""
    qs = {k: v[0] for k, v in parse_qs(urlsplit(h.path).query).items()}
    cl = (h.headers.get("Content-Length") or "").strip()
    name = qs.get("name") or ""

    def refuse(msg, code=400):
        h.close_connection = True
        return h._json({"ok": False, "error": msg}, code)
    if not cl.isdigit() or h.headers.get("Transfer-Encoding"):
        return refuse("Bad request.")
    teamref = app.optional_module("teamref")
    if teamref is None:
        return refuse("Teammate laps aren't available right now.", 500)
    n = int(cl)
    cap = MAX_TEAM_BODY if MAX_TEAM_BODY is not None else teamref.MAX_IBT
    if n > cap:
        return refuse(f"That file is far bigger than any telemetry file (over {cap // (1024 * 1024)} MB).")
    if not NAME_OK.match(name) or name.strip(". ") == "" or ".." in name:
        return refuse("Pick a telemetry file (.ibt).")
    try:
        car, track = _combo(qs)
        cfg = h._cfg()
    except ValueError as e:
        return refuse(str(e))
    keep = qs.get("keep") == "1"
    d = Path(tempfile.mkdtemp(prefix="debrief-teamref-"))

    def reply(obj, code=200):
        _remove_temp(d)                               # gone before the window hears back
        return h._json(obj, code)
    try:
        # saved under a fixed name: his file's name is only shown, never used as a path
        suf = Path(name).suffix.lower()
        p = d / ("teammate" + (suf if re.fullmatch(r"\.[a-z0-9]{1,10}", suf) else ""))
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
            return reply({"ok": False, "error": "The file didn't arrive in full. Try again."}, 400)
        try:
            res = teamref.load_reference(p, car, track)
        except Exception:  # noqa: BLE001
            app.say("ERROR reading a teammate's file:\n" + traceback.format_exc())
            return reply({"ok": False, "error": "Debrief couldn't read that file."}, 500)
        if not res.get("ok"):
            app.say("Teammate lap refused: " + (res.get("check") or {}).get("file", "") + " "
                    + str(res.get("reason") or "")[:160])
            return reply({"ok": True, "accepted": False, "reason": res.get("reason") or "This file can't be used.",
                            "file": name})
        res["summary"]["file"] = name
        e = _team_entry(res, False)
        st = state(h.server)
        k = _team_key(car, track)
        before = _kept_label(cfg, car, track)
        with st.lock:
            st.team[k] = e
            st.team_all[k + (dkey(e["summary"]),)] = e
            st.team_cards = {kk: v for kk, v in st.team_cards.items() if kk[:2] != k}
        if keep:
            e["kept"] = _team_save(cfg, car, track, e)
        app.say(f"Teammate lap loaded for {car} at {track}" + (" (kept)." if e["kept"] else "."))
        # a lap kept earlier is never deleted by picking another file without Keep: it stays saved for next time,
        # and the window says so (tick Keep to keep this one instead, or Forget it)
        still = before if (before and not e["kept"]) else None
        return reply({"ok": True, "accepted": True, "teammate": _team_public(e, h.server, car, track),
                        "still_kept": still})
    finally:
        _remove_temp(d)


def _remove_temp(d: Path, wait: float = 0.3) -> bool:
    """Delete the private temp folder; if Windows still holds the file (an antivirus scan, say), log it, wait a
    moment and try once more. Never raises."""
    for attempt in (1, 2):
        try:
            shutil.rmtree(d)
            return True
        except FileNotFoundError:
            return True
        except OSError as e:
            app.say(f"Car and setup: couldn't delete the teammate file's temp folder (try {attempt}): "
                    f"{type(e).__name__}: {e}")
            if attempt == 1:
                time.sleep(wait)
    return False


def _kept_label(cfg, car, track) -> str | None:
    """The label of the lap kept on disk for this car + track, or None."""
    kept = kept_files(cfg, car, track)
    return str((kept[0][1].get("summary") or {}).get("label") or "a teammate's lap") if kept else None


def _unlink_kept(cfg, car, track, driver) -> None:
    """Remove this driver's kept lap for the car and track (their own file, or the pre-2.21 one if it is theirs).
    Other teammates' kept laps stay."""
    for f, raw, _e in kept_files(cfg, car, track):
        if dkey(raw.get("summary")) == driver:
            try:
                f.unlink()
            except FileNotFoundError:
                pass


def post_teamref_keep(h, body):
    """Tick or untick "Keep for this car and track" for the lap in use: save its worked-out lap, or remove it."""
    cfg = h._cfg()
    car, track = _combo(body)
    keep = body.get("keep") is True
    e = _team_get(h.server, cfg, car, track)
    if e is None:
        raise ValueError("Pick a teammate's file first.")
    if keep:
        ok = _team_save(cfg, car, track, e)
        if not ok:
            raise ValueError("Debrief couldn't save it. Details were saved to debrief.log.")
        e["kept"] = True
    else:
        try:
            _unlink_kept(cfg, car, track, dkey(e["summary"]))
        except OSError as x:
            app.say(f"Car and setup: couldn't remove a kept teammate lap ({type(x).__name__}: {x}).")
            raise ValueError("Debrief couldn't remove the kept lap. Details were saved to debrief.log.")
        e["kept"] = False
    return h._json({"ok": True, "teammate": _team_public(e, h.server, car, track)})


def get_teamref(h, q):
    cfg = h._cfg()
    car, track = _combo(q)
    e = _team_get(h.server, cfg, car, track)
    return h._json({"ok": True, "teammate": _team_public(e, h.server, car, track)})


def get_teamref_cards(h, q):
    from . import server as S
    cfg = h._cfg()
    car, track = _combo(q)
    e = _team_get(h.server, cfg, car, track)
    if e is None or not e.get("cornercoach"):
        return h._json({"ok": True, "teammate": None, "cards": [], "message": "Pick a teammate's file first."})
    ss, idx = h.server.practice_sessions(cfg)
    paths = S.corner_paths(ss, car, track) if idx else []
    if not paths:
        return h._json({"ok": True, "teammate": _team_public(e, h.server, car, track), "cards": [],
                        "message": "No telemetry of yours for this car and track yet."})
    try:
        pk = cache_key("teamcards", paths, extra=[e["summary"].get("label"), e["summary"].get("lap_time")])
    except OSError:
        raise ValueError("Some telemetry files have gone. Press Read my telemetry again.")
    st = state(h.server)
    k = _team_key(car, track) + (pk,)
    with st.lock:
        rep = st.team_cards.get(k)
    if rep is None:
        from .. import cornercoach
        rep = cornercoach.analyze_files([str(p) for p in paths], reference=e["cornercoach"])
        times = [x.get("time") for x in rep.get("laps_used") or [] if isinstance(x.get("time"), (int, float))]
        rep = S._jsonable({key: rep.get(key) for key in ("ok", "message", "reference", "reference_cards", "laps",
                                                         "corners")})
        rep["my_best"] = min(times) if times else None
        with st.lock:
            st.team_cards[k] = rep           # memory only: the teammate's name is never written to disk unasked
    cards = rep.get("reference_cards") or []
    msg = (f"Your typical laps against {_short_label(e['summary'])}, from your last {rep.get('laps') or 0} clean laps "
           f"here." if rep.get("ok") else rep.get("message") or "Not enough clean laps here yet.")
    from .. import teamref
    head = teamref.headline(e["summary"].get("driver"), e["summary"].get("lap_time"), rep.get("my_best"), cards)
    return h._json({"ok": True, "teammate": _team_public(e, h.server, car, track), "cards": cards, "message": msg,
                    "laps": rep.get("laps") or 0, "files": len(paths), "headline": head,
                    "my_best": rep.get("my_best")})


def _same_names(car_a, track_a, car_b, track_b) -> bool:
    """The same car and the same track layout, by name (a race review's Garage 61 names against a telemetry
    file's iRacing names)."""
    from .. import carcompare, schedule
    if not (car_a and track_a and car_b and track_b):
        return False
    return carcompare.car_key(car_a) == carcompare.car_key(car_b) and schedule.same_track(track_a, track_b)


def team_board(cfg, car_name, track_name) -> list:
    """The team page's best laps (team.json, made by Refresh team data) for this car and track, or []."""
    try:
        data = json.loads((Path(cfg["library_folder"]) / "team" / "team.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError):
        return []
    for c in (data.get("combos") if isinstance(data, dict) else None) or []:
        if isinstance(c, dict) and _same_names(c.get("car"), c.get("track"), car_name, track_name):
            return [r for r in c.get("board") or [] if isinstance(r, dict)]
    return []


def team_notes(cfg, car_name, track_name) -> list:
    """2.20.1: the team page's per-teammate reasons ("Alex: no laps ...") for this car and track, or []."""
    try:
        data = json.loads((Path(cfg["library_folder"]) / "team" / "team.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError):
        return []
    for c in (data.get("combos") if isinstance(data, dict) else None) or []:
        if isinstance(c, dict) and _same_names(c.get("car"), c.get("track"), car_name, track_name):
            return [n for n in c.get("mate_notes") or [] if isinstance(n, str)]
    return []


def get_teamref_laps(h, q):
    """The teammate laps Debrief already has for this car + track: the telemetry lap in use (loaded or kept) and
    the team page's Garage 61 lap times, plus how to get a teammate's file. No requests."""
    from .. import teamref
    cfg = h._cfg()
    car, track = _combo(q)
    e = _team_get(h.server, cfg, car, track)
    names = {}
    try:
        recs = _records(_index(h.server, cfg), car, track)
        if recs:
            c, t = recs[-1].get("car") or {}, recs[-1].get("track") or {}
            names = {"car": c.get("name") or "",
                     "track": " ".join(x for x in (t.get("name"), f"({t['config']})" if t.get("config") else "") if x)}
    except ValueError:
        names = {}
    board = team_board(cfg, names.get("car"), names.get("track")) if names else []
    notes = team_notes(cfg, names.get("car"), names.get("track")) if names else []
    return h._json({"ok": True, "laps": teamref.laps_on_hand(e, board, notes), "how": teamref.HOW_TO_GET,
                    "team_page": bool(board)})


def get_teamref_for_review(h, q):
    """The newest Practice session (telemetry on this PC) in this race review's car and track, so the review's
    "Compare with a teammate's lap" opens it. {"session": id|None, "car", "track", "message"}."""
    cfg = h._cfg()
    d = h._review_dir(cfg, q.get("dir"))
    try:
        meta = (json.loads((d / "review.json").read_text(encoding="utf-8")) or {}).get("meta") or {}
    except (OSError, ValueError, AttributeError):
        meta = {}
    car, track = str(meta.get("car") or ""), str(meta.get("track") or "")
    ss, idx = h.server.practice_sessions(cfg)
    hits = [s for s in ss if _same_names(s.get("car"), s.get("track"), car, track)] if idx else []
    hits.sort(key=lambda s: -float(s.get("sort") or 0))
    if hits:
        return h._json({"ok": True, "session": hits[0].get("id"), "car": car, "track": track, "message": ""})
    if not idx:
        msg = ("Debrief hasn't read your telemetry yet. Open Practice and press Read my telemetry, then try again: "
               "a teammate's lap is lined up with your own telemetry from this car and track.")
    else:
        msg = (f"There's no telemetry of yours from the {car or 'this car'} at {track or 'this track'} on this PC, so "
               f"there's nothing to line a teammate's lap up with yet. Drive it once (iRacing saves the telemetry), "
               f"press Read my telemetry on Practice, then open the session.")
    return h._json({"ok": True, "session": None, "car": car, "track": track, "message": msg})


def post_teamref_forget(h, body):
    """Forget the teammate lap in use for this car and track (memory and kept file). 2.21: other teammates' laps
    for the same car and track stay."""
    cfg = h._cfg()
    car, track = _combo(body)
    st = state(h.server)
    k = _team_key(car, track)
    cur = _team_get(h.server, cfg, car, track)
    d = dkey(cur["summary"]) if cur else None
    with st.lock:
        st.team.pop(k, None)
        if d is not None:
            st.team_all.pop(k + (d,), None)
        st.team_cards = {kk: v for kk, v in st.team_cards.items() if kk[:2] != k}
        if st.coach_on == k:
            st.coach_on = None
    try:
        if d is not None:
            _unlink_kept(cfg, car, track, d)
    except OSError:
        pass
    return h._json({"ok": True})


def post_teamref_coach(h, body):
    cfg = h._cfg()
    car, track = _combo(body)
    on = body.get("on") is True
    st = state(h.server)
    if on and _team_get(h.server, cfg, car, track) is None:
        raise ValueError("Pick a teammate's file first.")
    with st.lock:
        st.coach_on = _team_key(car, track) if on else (None if st.coach_on == _team_key(car, track) else st.coach_on)
    with h.server.coach_lock:
        r = h.server.coach
    running = r is not None and r.running()
    return h._json({"ok": True, "on": on, "restart": running})


# ---------------------------------------------------------------- the coach's hooks
def coach_reference(server, settings):
    """The teammate's Reference when he chose to be coached against it, else None (the coach finds his own best lap
    as before). The coach itself keeps it from being replaced by his own new bests, and only while it is the
    reference in use (that car at that track); settings are left as they are."""
    st = state(server)
    with st.lock:
        k = st.coach_on
        e = st.team.get(k) if k else None
    if k and e is None:
        try:
            e = _team_get(server, app.load_settings(), *k)
        except (SystemExit, Exception):  # noqa: BLE001
            e = None
    if e is None or e.get("livecoach") is None:
        return None
    return e["livecoach"]


def coach_extra(server, status: dict) -> dict:
    """status["teammate"]: {"on", "label", "in_use"} for the Coach page."""
    st = state(server)
    with st.lock:
        k = st.coach_on
        e = st.team.get(k) if k else None
    ref = status.get("reference") or {}
    src = ref.get("source") or {}
    status["teammate"] = {"on": bool(k and e), "car_path": k[0] if k else None, "track_code": k[1] if k else None,
                          "label": (e or {}).get("summary", {}).get("label") if e else None,
                          "short": _short_label(e["summary"]) if e else None,
                          "in_use": src.get("kind") == "teammate"}
    return status


# ---------------------------------------------------------------- dispatch (called from server.Handler)
def get(h, r, q):
    fn = {"api/balance": get_balance, "api/handling": get_handling, "api/pressure": get_pressure, "api/setuplab/share": get_setuplab_share,
          "api/teamref": get_teamref, "api/teamref/cards": get_teamref_cards, "api/teamref/laps": get_teamref_laps,
          "api/teamref/for-review": get_teamref_for_review}[r]
    return fn(h, q)


def post(h, r, body):
    fn = {"api/pressure/predict": post_pressure_predict, "api/handling/feel": post_handling_feel, "api/teamref/forget": post_teamref_forget,
          "api/teamref/coach": post_teamref_coach, "api/teamref/keep": post_teamref_keep}[r]
    return fn(h, body)
