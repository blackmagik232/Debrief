"""History sync: a local copy of all your Garage 61 laps (races, qualifying and practice).

The first sync downloads everything Garage 61 has for you. Every sync after that only asks for
laps since the last one, so it's usually a handful of requests. Debrief runs a catch-up sync each
time it opens (Settings can switch that off) and from the Sessions page's Sync now button.

How it stays inside Garage 61's limits:
  * requests go through the normal client, so they're paced (about one a second) and back off on 429
  * lap lists are fetched once per track and car (1000 laps a request); later syncs only ask for
    laps driven after the last sync
  * telemetry is downloaded for one lap per session (your best clean lap), never twice
  * the sync saves a checkpoint after every track and car and can be stopped at any time
    (closing the window, or starting a review). The next sync carries on from where it stopped.

Everything lives in the same per-account folder as Debrief's Garage 61 cache:
    <cache>/<account>/sync/state.json       progress and checkpoints
    <cache>/<account>/sync/laps/T_C.json     every lap for one track/car, keyed by lap id
    <cache>/<account>/sync/sessions.json     one row per session, newest first (the Sessions page)
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import fsutil
from .engine import g

SCHEMA = 1
EPOCH = "2008-01-01T00:00:00Z"   # iRacing launched in 2008: "after" this means everything
PAGE = 1000                      # Garage 61's maximum page size for /laps
MAX_PAGES = 100                  # 100,000 laps for one track and car is plenty
OVERLAP = timedelta(days=2)      # re-ask a little before the last sync, for laps that uploaded late
DEEP_EVERY = timedelta(days=7)   # once a week, look further back for laps that reached Garage 61 late
DEEP_BACK = timedelta(days=30)
TEL_TRIES = 3                    # a lap whose telemetry fails this many syncs running is skipped
TEL_RETRY = timedelta(days=30)   # telemetry Garage 61 refused (403/404) is asked for again after this
INCOMPLETE_TRIES = 3             # a track/car Garage 61 keeps sending short is retried on this many syncs
TEL_FAIL_RUN = 3                 # this many telemetry failures in a row: Garage 61 is struggling, stop for now
SESSION_NAMES = {1: "Practice", 2: "Qualifying", 3: "Race"}


class Stopped(Exception):
    """The sync was asked to stop. Everything up to the last checkpoint is saved."""


_log = logging.getLogger("debrief.sync")
_MISSING, _BAD = object(), object()


# ---------------------------------------------------------------- small helpers
def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(s) -> datetime | None:
    if not s:
        return None
    s = str(s).replace("Z", "+00:00")
    if "." in s:  # Python 3.10 only accepts 3- or 6-digit fractions
        head, _, rest = s.partition(".")
        tz = ""
        for sep in ("+", "-"):
            if sep in rest:
                tz = sep + rest.split(sep, 1)[1]
                break
        s = head + tz
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.fromisoformat(s[:10])
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _id(v):
    return g(v, "id") if isinstance(v, dict) else v


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def lap_track(l):
    return _int(_id(g(l, "track_id", "trackId", "track")))


def lap_car(l):
    return _int(_id(g(l, "car_id", "carId", "car")))


def lap_start(l) -> str:
    return str(g(l, "start_time", "startTime", default="") or "")


def lap_secs(l):
    t = g(l, "lap_time", "lapTime")
    try:
        t = float(t)
    except (TypeError, ValueError):
        return None
    return t if t > 0 else None


def lap_clean(l) -> bool:
    v = g(l, "clean", "isClean")
    if v is None and g(l, "unclean") is not None:
        return not bool(g(l, "unclean"))
    return True if v is None else bool(v)


def lap_session_type(l):
    v = g(l, "session_type", "sessionType")
    if isinstance(v, dict):
        v = g(v, "type", "id")
    if isinstance(v, str):
        v = {"practice": 1, "qualify": 2, "qualifying": 2, "race": 3}.get(v.lower(), _int(v) or v)
    return v


def session_name(t) -> str:
    return SESSION_NAMES.get(t, "Session")


def _write_json(p: Path, obj) -> None:
    """Durable write: a temp file in the same folder, flushed to disk, then swapped in. A power cut leaves
    either the old file or the new one, never an empty or half-written one."""
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=p.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(obj, separators=(",", ":")))
            f.flush()
            os.fsync(f.fileno())
        fsutil.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load(p: Path):
    """The parsed file, _MISSING if there's no file, or _BAD if it's there but can't be read as JSON."""
    try:
        txt = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _MISSING
    except OSError:
        return _BAD
    try:
        return json.loads(txt)
    except ValueError:
        return _BAD


def _read_json(p: Path, default):
    d = _load(p)
    return default if d is _MISSING or d is _BAD else d


def _set_aside(p: Path) -> Path | None:
    """Move an unreadable file out of the way (kept, never overwritten) as <name>.corrupt-<time>.json."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    dst = p.with_name(f"{p.stem}.corrupt-{ts}{p.suffix}")
    try:
        fsutil.replace(p, dst)
        return dst
    except OSError:
        return None


# fields the store keeps for each lap (everything build_sessions reads); the rest isn't needed on disk
_LAP_KEYS = ("id", "event", "track", "car", "track_id", "trackId", "car_id", "carId", "session_type", "sessionType",
             "start_time", "startTime", "lap_time", "lapTime", "clean", "isClean", "unclean")
_REF_KEYS = ("id", "name", "variant")


def slim(l: dict) -> dict:
    out = {}
    for k in _LAP_KEYS:
        if k in l:
            v = l[k]
            if k in ("track", "car") and isinstance(v, dict):
                v = {kk: v[kk] for kk in _REF_KEYS if kk in v}
            out[k] = v
    return out


# ---------------------------------------------------------------- the store
class Store:
    """The synced laps on disk. Reading it never contacts Garage 61."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.laps_dir = self.root / "laps"

    @classmethod
    def for_api(cls, api) -> "Store":
        return cls(Path(api.cache) / "sync")

    # state
    def state(self) -> dict:
        p = self.root / "state.json"
        st = _load(p)
        if st is _BAD:
            # unreadable: keep it aside and start the checkpoints again (the lap files are still used)
            _log.warning("sync state unreadable; set aside as %s", _set_aside(p))
        if not isinstance(st, dict) or st.get("schema") != SCHEMA:
            st = {"schema": SCHEMA}
        st.setdefault("combos", {})
        tn = st.get("tel_none")
        if isinstance(tn, list):   # 2.2.1 kept a plain list (never retried): retry those in 30 days
            st["tel_none"] = {str(k): st.get("last_success") or _iso(datetime.now(timezone.utc)) for k in tn}
        elif not isinstance(tn, dict):
            st["tel_none"] = {}
        if not isinstance(st.get("tel_fail"), dict):
            st["tel_fail"] = {}
        if not isinstance(st.get("tel_done"), list):
            st["tel_done"] = []
        return st

    def save_state(self, st: dict) -> None:
        st["schema"] = SCHEMA
        _write_json(self.root / "state.json", st)

    # laps
    def _combo_file(self, track, car) -> Path:
        return self.laps_dir / f"{int(track)}_{int(car)}.json"

    def laps(self, track, car) -> dict:
        d = _read_json(self._combo_file(track, car), {})
        return d if isinstance(d, dict) else {}

    def combo_ok(self, track, car) -> bool:
        """False (and the file is moved aside) when a track/car's lap file is there but unreadable."""
        p = self._combo_file(track, car)
        d = _load(p)
        if d is _MISSING or isinstance(d, dict):
            return True
        _log.warning("laps file %s unreadable; set aside as %s", p.name, _set_aside(p))
        return False

    def merge(self, track, car, laps) -> tuple[int, int]:
        """Add laps for one track/car. Returns (new laps, total laps). Never overwrites a lap file it couldn't
        read: that one is moved aside first (the caller re-downloads the track/car; see run())."""
        p = self._combo_file(track, car)
        have = _load(p)
        if have is _BAD or (have is not _MISSING and not isinstance(have, dict)):
            _log.warning("laps file %s unreadable; set aside as %s", p.name, _set_aside(p))
            have = {}
        elif have is _MISSING:
            have = {}
        before = len(have)
        for l in laps:
            lid = g(l, "id")
            if lid is None:
                continue
            have[str(lid)] = slim(l) if isinstance(l, dict) else l
        _write_json(p, have)
        return len(have) - before, len(have)

    def all_laps(self):
        for p in sorted(self.laps_dir.glob("*_*.json")):
            if ".corrupt-" in p.name:
                continue
            d = _read_json(p, {})
            if isinstance(d, dict):
                yield from d.values()

    def check_files(self, combos: dict) -> list:
        """Track/car keys ("T:C") whose lap file is unreadable (moved aside) or missing although the state
        says laps were saved. Their checkpoints are cleared so the next fetch starts from the beginning."""
        bad = []
        for key, info in list(combos.items()):
            try:
                t, c = (int(x) for x in key.split(":"))
            except ValueError:
                continue
            p = self._combo_file(t, c)
            lost = not p.exists() and int((info or {}).get("laps") or 0) > 0
            if lost or not self.combo_ok(t, c):
                bad.append(key)
                combos[key] = {"done": False}
        for p in self.laps_dir.glob("*_*.json"):   # a file the state doesn't know about
            if ".corrupt-" in p.name:
                continue
            try:
                t, c = (int(x) for x in p.stem.split("_"))
            except ValueError:
                continue
            if f"{t}:{c}" not in combos and not self.combo_ok(t, c):
                bad.append(f"{t}:{c}")
                combos[f"{t}:{c}"] = {"done": False}
        return bad

    # sessions
    def sessions(self) -> list:
        d = _read_json(self.root / "sessions.json", {})
        return list(d.get("sessions") or []) if isinstance(d, dict) else []

    def summary(self) -> dict:
        """What the window shows about the sync. No requests."""
        st = self.state()
        d = _read_json(self.root / "sessions.json", {})
        d = d if isinstance(d, dict) else {}
        return {"synced": bool(st.get("first_complete")), "last_sync": st.get("last_success"),
                "laps": int(d.get("laps") or 0), "sessions": len(d.get("sessions") or []),
                "combos": len(st.get("combos") or {}), "telemetry_left": int(st.get("telemetry_left") or 0),
                "last_requests": st.get("last_requests"), "stopped": bool(st.get("stopped")),
                "warnings": list(st.get("last_warnings") or [])}


# ---------------------------------------------------------------- sessions index
def build_sessions(store: Store, names: dict | None = None) -> list:
    """Group every synced lap into sessions (an event + session type), newest first, and save them."""
    names = names or {}
    groups: dict = {}
    total = 0
    for l in store.all_laps():
        total += 1
        t, c = lap_track(l), lap_car(l)
        st = lap_session_type(l)
        ev = g(l, "event")
        start = lap_start(l)
        key = f"{ev}|{st}" if ev else f"{t}|{c}|{start[:10]}|{st}"
        s = groups.get(key)
        if s is None:
            s = groups[key] = {"key": key, "event": str(ev) if ev else None, "track_id": t, "car_id": c,
                               "type": st if isinstance(st, int) else None, "session": session_name(st),
                               "start": start, "end": start, "laps": 0, "clean_laps": 0,
                               "best": None, "best_lap_id": None, "best_clean": None, "_any_best": None}
            for kind, obj in (("track", g(l, "track")), ("car", g(l, "car"))):
                if isinstance(obj, dict) and g(obj, "name"):
                    v = g(obj, "variant")
                    s[kind] = g(obj, "name") + (f" ({v})" if v else "")
        s["laps"] += 1
        if start and (not s["start"] or start < s["start"]):
            s["start"] = start
        if start and start > (s["end"] or ""):
            s["end"] = start
        lt = lap_secs(l)
        if lt is None:
            continue
        if s["_any_best"] is None or lt < s["_any_best"][0]:
            s["_any_best"] = (lt, str(g(l, "id")))
        if lap_clean(l):
            s["clean_laps"] += 1
            if s["best_clean"] is None or lt < s["best_clean"]:
                s["best_clean"] = lt
                s["best_lap_id"] = str(g(l, "id"))
    out = []
    for s in groups.values():
        anyb = s.pop("_any_best")
        s["best"] = s["best_clean"] if s["best_clean"] is not None else (anyb[0] if anyb else None)
        if s["best_lap_id"] is None and anyb:
            s["best_lap_id"] = anyb[1]   # no clean lap: telemetry for the quickest one still helps
        s.setdefault("track", names.get(("track", s["track_id"])) or f"Track {s['track_id']}")
        s.setdefault("car", names.get(("car", s["car_id"])) or f"Car {s['car_id']}")
        out.append(s)
    out.sort(key=lambda s: (s["start"] or "", s["key"]), reverse=True)
    _write_json(store.root / "sessions.json", {"schema": SCHEMA, "built": _iso(datetime.now(timezone.utc)),
                                              "laps": total, "sessions": out})
    return out


def _names(api) -> dict:
    """(kind, id) -> display name from Garage 61's track and car lists (saved for a week; 2 requests cold)."""
    out = {}
    for kind, fn in (("track", api.tracks), ("car", api.cars)):
        try:
            items = fn()
        except Exception:  # noqa: BLE001  (names are nice to have; ids still work)
            items = []
        for it in items or []:
            if isinstance(it, dict) and g(it, "id") is not None:
                v = g(it, "variant")
                out[(kind, _int(g(it, "id")))] = str(g(it, "name", default="")) + (f" ({v})" if v else "")
    return out


# ---------------------------------------------------------------- the sync
def _combos_from_stats(rows) -> dict:
    """(track, car) -> newest day driven, from Garage 61's driving statistics."""
    out = {}
    for r in rows or []:
        t = _int(_id(g(r, "track", "trackId", "track_id")))
        c = _int(_id(g(r, "car", "carId", "car_id")))
        if t is None or c is None:
            continue
        day = str(g(r, "day", "date", "startDate", "start", default="") or "")
        if (t, c) not in out or day > out[(t, c)]:
            out[(t, c)] = day
    return out


def _fetch_combo(api, t, c, after, age, check, warn):
    """Every lap for one track/car since `after`. Pages by offset, and checks the answers: stops when a page
    brings nothing new (a server that ignores offset), keeps going when Garage 61's total says there's more
    than a short page suggested (a server with a smaller page limit), and says so when laps are missing."""
    got, seen = [], set()
    offset = 0
    total = None
    for _ in range(MAX_PAGES):
        check()
        items, tot = api.laps_page(tracks=[t], cars=[c], drivers=["me"], group="none", unclean=True,
                                   after=after, age=age, limit=PAGE, offset=offset)
        if tot is not None:
            total = tot
        fresh = [l for l in items if isinstance(l, dict) and str(g(l, "id")) not in seen]
        for l in fresh:
            seen.add(str(g(l, "id")))
        got.extend(fresh)
        offset += len(items)
        if not items or not fresh:
            break
        if total is not None:
            if offset >= total:
                break
        elif len(items) < PAGE:
            break
    else:
        warn(f"  Track {t} · car {c}: stopped after {MAX_PAGES} pages of laps.")
    if total is not None and len(seen) < total:
        warn(f"  Track {t} · car {c}: Garage 61 listed {total} laps but sent {len(seen)}. "
             "The rest are fetched on a later sync.")
        return got, False
    return got, True


def run(api, log=print, progress=None, should_stop=None, telemetry: str = "best", now: datetime | None = None) -> dict:
    """Bring the local copy up to date. Returns a summary dict (also saved in state.json).

    telemetry: "best" downloads your best lap's telemetry for every session (resumable), "none" skips it.
    should_stop: a callable checked before every request (and during long waits); when it returns True the
    sync saves and stops.
    """
    from .g61 import Garage61Error, Interrupted
    stop = should_stop or (lambda: False)
    now = now or datetime.now(timezone.utc)
    run_start = _iso(now)
    store = Store.for_api(api)
    st = store.state()
    first = not st.get("first_complete")
    req0 = api.requests_made
    prev_stop = getattr(api, "should_stop", None)
    try:
        api.should_stop = stop
    except AttributeError:
        pass

    def step(msg, frac=None):
        log(msg)
        if progress:
            try:
                progress(frac, msg)
            except Exception:  # noqa: BLE001
                pass

    def check():
        if stop():
            raise Stopped()

    warnings = []

    def warn(msg):
        warnings.append(msg.strip())
        _log.warning(msg.strip())
        log(msg)

    st["last_started"] = run_start
    st["stopped"] = False
    stopped = False
    new_laps = 0
    changed = False
    try:
        # 0. lap files that can't be read are moved aside and downloaded again (never overwritten)
        combos = st["combos"]
        bad = store.check_files(combos)
        if bad:
            changed = True
            warn(f"  {len(bad)} saved lap file{'s were' if len(bad) != 1 else ' was'} damaged; "
                 "kept aside and downloading again.")
            store.save_state(st)

        # 1. which tracks and cars to look at (Garage 61's lap search needs a track)
        check()
        last = _parse(st.get("last_success"))
        deep = False
        if first or last is None:
            since = EPOCH
        else:
            since_dt = last - OVERLAP
            last_deep = _parse(st.get("last_deep")) or _parse(st.get("first_success")) or last
            if now - last_deep >= DEEP_EVERY:   # weekly: also catch laps that reached Garage 61 late
                deep = True
                since_dt = min(since_dt, now - DEEP_BACK)
            since = _iso(since_dt)
        step("Downloading your full Garage 61 history (first sync)..." if first else
             "Checking Garage 61 for new laps...", 0.03)
        rows = api.my_statistics(since)
        driven = _combos_from_stats(rows)
        todo = []
        for (t, c), day in sorted(driven.items(), key=lambda kv: kv[1], reverse=True):
            info = combos.get(f"{t}:{c}") or {}
            prev = _parse(info.get("synced_until"))
            if first and info.get("done") and prev is not None and day and day < _iso(prev - OVERLAP)[:10]:
                continue   # resumed first sync: saved already and not driven since
            todo.append((t, c))
        # an interrupted sync (or a damaged file): finish the combos it hadn't reached
        for key, info in combos.items():
            if not info.get("done"):
                t, c = (int(x) for x in key.split(":"))
                if (t, c) not in todo:
                    todo.append((t, c))
        names = _names(api) if todo else {}
        if first and not todo and not combos:
            step("Garage 61 shows no driving on your account yet.", 0.6)
        elif not todo:
            step("No new driving since the last sync.", 0.6)
        else:
            step(f"{len(todo)} track and car combination{'s' if len(todo) != 1 else ''} to "
                 f"{'download' if first else 'check'}.", 0.05)

        # 2. laps, one track/car at a time, with a checkpoint after each
        for i, (t, c) in enumerate(todo):
            key = f"{t}:{c}"
            info = combos.get(key) or {}
            if not store.combo_ok(t, c):   # damaged since the check above: start this one again
                info = {}
            prev = _parse(info.get("synced_until"))
            after_dt = None if prev is None else prev - OVERLAP
            if deep and after_dt is not None:
                after_dt = min(after_dt, now - DEEP_BACK)
            after = EPOCH if after_dt is None else _iso(after_dt)
            # an explicit age (days) as well, so no default age limit on Garage 61's side trims the history
            age = max(1, (now - _parse(after)).days + 2)
            got, complete = _fetch_combo(api, t, c, after, age, check, warn)
            added, total = store.merge(t, c, got)
            new_laps += added
            changed = changed or added > 0
            if complete:
                combos[key] = {"done": True, "synced_until": run_start, "laps": total}
            else:
                # laps are missing: don't move the checkpoint, so the next syncs ask for them again
                tries = int(info.get("incomplete") or 0) + 1
                combos[key] = {"done": tries >= INCOMPLETE_TRIES, "laps": total, "incomplete": tries}
                if info.get("synced_until"):
                    combos[key]["synced_until"] = info["synced_until"]
            store.save_state(st)
            label = f"{names.get(('track', t)) or f'Track {t}'} · {names.get(('car', c)) or f'Car {c}'}"
            step(f"  {label}: {total} laps" + (f" ({added} new)" if added and not first else ""),
                 0.05 + 0.55 * (i + 1) / max(1, len(todo)))

        # 3. sessions (no requests apart from the track and car names, saved for a week)
        check()
        sess_file = store.root / "sessions.json"
        old = _load(sess_file)
        if changed or first or not isinstance(old, dict) or not isinstance(old.get("sessions"), list):
            sessions = build_sessions(store, names or _names(api))
        else:
            sessions = list(old["sessions"])   # nothing new: the saved list is still right
        st["first_complete"] = True
        st["first_success"] = st.get("first_success") or run_start
        st["last_success"] = run_start
        if deep or first:
            st["last_deep"] = run_start
        store.save_state(st)
        step(f"{len(sessions)} sessions in your history.", 0.62)

        # 4. telemetry for each session's best lap (newest first), skipping laps already saved
        if telemetry != "none":
            none_ = st["tel_none"]
            for lid, when in list(none_.items()):   # refused before: ask again after a while
                w = _parse(when)
                if w is None or now - w >= TEL_RETRY:
                    none_.pop(lid, None)
            fails = st["tel_fail"]
            done = set(st["tel_done"])
            want = [s["best_lap_id"] for s in sessions
                    if s.get("best_lap_id") and s["best_lap_id"] not in none_ and s["best_lap_id"] not in done
                    and not api.has_csv(s["best_lap_id"])]
            want.sort(key=lambda lid: int(fails.get(lid) or 0))   # stable: newest first, failing laps last
            st["telemetry_left"] = len(want)
            store.save_state(st)
            if want:
                step(f"Downloading telemetry for {len(want)} session best lap{'s' if len(want) != 1 else ''}...", 0.63)
            run_fail = 0
            got_any = False
            for i, lid in enumerate(want):
                check()
                try:
                    csv = api.lap_csv(lid)
                except Garage61Error as e:
                    if getattr(e, "fatal", False) or getattr(e, "status", None) == 401:
                        raise
                    n = int(fails.get(lid) or 0) + 1
                    fails[lid] = n
                    run_fail += 1
                    if n >= TEL_TRIES:
                        fails.pop(lid, None)
                        none_[lid] = run_start
                        warn(f"  Telemetry for lap {lid} failed {n} times; skipping it for now.")
                    store.save_state(st)
                    if run_fail >= TEL_FAIL_RUN:
                        warn("  Garage 61 isn't sending telemetry right now; the rest is fetched next sync.")
                        break
                    continue
                run_fail = 0
                fails.pop(lid, None)
                if csv is None:
                    none_[lid] = run_start
                else:
                    done.add(lid)
                    got_any = True
                st["telemetry_left"] = len(want) - i - 1
                if i % 10 == 9 or i == len(want) - 1:
                    st["tel_done"] = sorted(done)
                    store.save_state(st)
                if progress:
                    progress(0.63 + 0.35 * (i + 1) / len(want), f"Telemetry for session {i + 1} of {len(want)}...")
            st["tel_done"] = sorted(done)
            st["telemetry_left"] = len([x for x in want if x not in done and x not in none_])
            if got_any and hasattr(api, "prune_csv"):
                try:
                    api.prune_csv(keep=[s["best_lap_id"] for s in sessions[:100] if s.get("best_lap_id")])
                except OSError:
                    pass
    except (Stopped, Interrupted):
        stopped = True
        st["stopped"] = True
        log("  Sync paused. It carries on from here next time.")
    finally:
        try:
            api.should_stop = prev_stop
        except AttributeError:
            pass
    st["last_requests"] = api.requests_made - req0
    st["last_warnings"] = warnings[-20:]
    store.save_state(st)
    summ = store.summary()
    summ.update({"stopped": stopped, "first": first, "new_laps": new_laps, "requests": st["last_requests"],
                 "warnings": warnings[-20:]})
    if not stopped:
        step(f"Sync done: {summ['sessions']} sessions, {summ['laps']} laps"
             + (f", {new_laps} new" if not first else "")
             + f" ({summ['requests']} Garage 61 request{'s' if summ['requests'] != 1 else ''}).", 1.0)
    return summ


def due(store: Store, min_gap_s: float = 600) -> bool:
    """True if an automatic sync on open should run (never synced, unfinished, or not in the last 10 minutes)."""
    st = store.state()
    if not st.get("first_complete") or st.get("stopped"):
        return True
    last = _parse(st.get("last_success"))
    return last is None or (datetime.now(timezone.utc) - last).total_seconds() >= min_gap_s
