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
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import fsutil
from .engine import g

SCHEMA = 1
EPOCH = "2008-01-01T00:00:00Z"   # iRacing launched in 2008: "after" this means everything
PAGE = 1000                      # Garage 61's maximum page size for /laps
MAX_PAGES = 100                  # 100,000 laps for one track and car is plenty
OVERLAP = timedelta(days=2)      # re-ask a little before the last sync, for laps that uploaded late
SESSION_NAMES = {1: "Practice", 2: "Qualifying", 3: "Race"}


class Stopped(Exception):
    """The sync was asked to stop. Everything up to the last checkpoint is saved."""


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
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":")), encoding="utf-8")
    fsutil.replace(tmp, p)


def _read_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


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
        st = _read_json(self.root / "state.json", {})
        if not isinstance(st, dict) or st.get("schema") != SCHEMA:
            st = {"schema": SCHEMA}
        st.setdefault("combos", {})
        st.setdefault("tel_none", [])
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

    def merge(self, track, car, laps) -> tuple[int, int]:
        """Add laps for one track/car. Returns (new laps, total laps)."""
        have = self.laps(track, car)
        before = len(have)
        for l in laps:
            lid = g(l, "id")
            if lid is None:
                continue
            have[str(lid)] = l
        _write_json(self._combo_file(track, car), have)
        return len(have) - before, len(have)

    def all_laps(self):
        for p in sorted(self.laps_dir.glob("*_*.json")):
            d = _read_json(p, {})
            if isinstance(d, dict):
                yield from d.values()

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
                "last_requests": st.get("last_requests"), "stopped": bool(st.get("stopped"))}


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


def run(api, log=print, progress=None, should_stop=None, telemetry: str = "best", now: datetime | None = None) -> dict:
    """Bring the local copy up to date. Returns a summary dict (also saved in state.json).

    telemetry: "best" downloads your best lap's telemetry for every session (resumable), "none" skips it.
    should_stop: a callable checked before every request; when it returns True the sync saves and stops.
    """
    stop = should_stop or (lambda: False)
    now = now or datetime.now(timezone.utc)
    run_start = _iso(now)
    store = Store.for_api(api)
    st = store.state()
    first = not st.get("first_complete")
    req0 = api.requests_made

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

    st["last_started"] = run_start
    st["stopped"] = False
    stopped = False
    new_laps = 0
    try:
        # 1. which tracks and cars to look at (Garage 61's lap search needs a track)
        check()
        last = _parse(st.get("last_success"))
        since = EPOCH if first or last is None else _iso(last - OVERLAP)
        step("Downloading your full Garage 61 history (first sync)..." if first else
             "Checking Garage 61 for new laps...", 0.03)
        rows = api.my_statistics(since)
        driven = _combos_from_stats(rows)
        combos = st["combos"]
        todo = []
        for (t, c), day in sorted(driven.items(), key=lambda kv: kv[1], reverse=True):
            todo.append((t, c))
        # an interrupted first sync: finish the combos it hadn't reached
        for key, info in combos.items():
            if not info.get("done"):
                t, c = (int(x) for x in key.split(":"))
                if (t, c) not in todo:
                    todo.append((t, c))
        names = _names(api) if todo else {}
        if first and not todo:
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
            if first and info.get("done"):
                continue   # resumed first sync: this one's already saved
            prev = _parse(info.get("synced_until"))
            after = EPOCH if prev is None else _iso(prev - OVERLAP)
            # an explicit age (days) as well, so no default age limit on Garage 61's side trims the history
            age = max(1, (now - _parse(after)).days + 2)
            got = []
            for page in range(MAX_PAGES):
                check()
                items = api.search_laps_page(tracks=[t], cars=[c], drivers=["me"], group="none", unclean=True,
                                             after=after, age=age, limit=PAGE, offset=page * PAGE)
                got.extend(items)
                if len(items) < PAGE:
                    break
            added, total = store.merge(t, c, got)
            new_laps += added
            combos[key] = {"done": True, "synced_until": run_start, "laps": total}
            store.save_state(st)
            label = f"{names.get(('track', t)) or f'Track {t}'} · {names.get(('car', c)) or f'Car {c}'}"
            step(f"  {label}: {total} laps" + (f" ({added} new)" if added and not first else ""),
                 0.05 + 0.55 * (i + 1) / max(1, len(todo)))

        # 3. sessions (no requests apart from the track and car names, saved for a week)
        check()
        sessions = build_sessions(store, names or _names(api))
        st["first_complete"] = True
        st["last_success"] = run_start
        store.save_state(st)
        step(f"{len(sessions)} sessions in your history.", 0.62)

        # 4. telemetry for each session's best lap (newest first), skipping laps already saved
        if telemetry != "none":
            tried = set(st.get("tel_none") or [])
            want = [s["best_lap_id"] for s in sessions
                    if s.get("best_lap_id") and s["best_lap_id"] not in tried and not api.has_csv(s["best_lap_id"])]
            st["telemetry_left"] = len(want)
            store.save_state(st)
            if want:
                step(f"Downloading telemetry for {len(want)} session best lap{'s' if len(want) != 1 else ''}...", 0.63)
            for i, lid in enumerate(want):
                check()
                if api.lap_csv(lid) is None:
                    tried.add(lid)
                st["telemetry_left"] = len(want) - i - 1
                if i % 10 == 9 or i == len(want) - 1:
                    st["tel_none"] = sorted(tried)
                    store.save_state(st)
                if progress:
                    progress(0.63 + 0.35 * (i + 1) / len(want), f"Telemetry for session {i + 1} of {len(want)}...")
            st["tel_none"] = sorted(tried)
    except Stopped:
        stopped = True
        st["stopped"] = True
        log("  Sync paused. It carries on from here next time.")
    st["last_requests"] = api.requests_made - req0
    store.save_state(st)
    summ = store.summary()
    summ.update({"stopped": stopped, "first": first, "new_laps": new_laps, "requests": st["last_requests"]})
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
