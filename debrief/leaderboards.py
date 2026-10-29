"""Weekly team leaderboards for your series, and season head-to-heads (Debrief 2.5).

For each of your series (Prepare > Your series, at most MAX_SERIES), this week's track and cars come from
the loaded season schedule. One Garage 61 search per series gives each teammate's best lap there since
the week started; the board keeps only complete, clean, plausible laps set inside the race week.

Garage 61 request budget (spec 2.5, section 3.7):
    | Weekly refresh | 1 per series, 2 if a "best" lap is partial or unclean, max 8 series -> <= 16;
    |                | 0 when cached (< 10 min) or the week is final; past weeks max 3 per refresh
    |                | (same per-series cost) |
    Request 1   find_laps(tracks, cars, teams=[slug], drivers=["me"], group="driver"/"driver-car",
                after=<week start>, age=<days since start + 2>, limit=200)          cached 10 min
    Request 2   only when a returned "best" is unusable (partial, unclean, pit, implausible or set
                outside the race week): the same search with group="none", limit=1000  cached 10 min
    Never more than 2 per series per week. Past weeks: at most MAX_PAST_WEEKS week fetches per refresh
    in all (most recent first); only the 3 most recent past weeks are filled on their own, older or
    final ones need force_old. Worst case per refresh: (8 + 3) x 2 = 22.
    The track and car ids come from the cached lists only (prepare._ids_from_lists): never a request.
    Your Garage 61 slug comes from the saved "me" answer (peek); only if none was ever saved is me()
    asked once (cached a day).
    plan() and view() never send a request.

Sign conventions:
    board gap        = driver's best - class leader's best        (>= 0)
    board gap_pct    = gap / class leader's best * 100            (>= 0)
    h2h gap_pct      = (your best / their best - 1) * 100         (positive = they were quicker)
    h2h ahead/behind = weeks YOU were ahead of / behind them      (same as team.head_to_heads)

Files: <library>/team/weekly/<season_slug>/<series_id>/w<NN>.json, written atomically after each
series (a stopped refresh keeps what it got).
"""
from __future__ import annotations

import json
import os
import re
import statistics
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import fsutil, prepare, schedule, series, team
from .engine import g

SCHEMA = 1
MAX_SERIES = 8
MAX_PAST_WEEKS = 3          # past-week fetches per refresh, in all
AUTO_PAST_WEEKS = 3         # how far back a refresh fills on its own (older ones need force_old)
FRESH_S = 600               # matches Garage61.find_laps' cache
FINAL_AFTER = timedelta(days=2)
NO_IDS_NOTE = "Load the track and car lists once (Prepare > Prep pages for any track and car)."
_SAFE = re.compile(r"[^a-z0-9-]+")


def _now() -> datetime:
    """Current UTC time (tests replace this)."""
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_dt(s):
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _day(s) -> date | None:
    try:
        return date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def _midnight(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def _safe(s) -> str:
    return _SAFE.sub("-", str(s or "").lower()).strip("-")[:80] or "x"


# ---------------------------------------------------------------- files
def weekly_dir(library_folder) -> Path:
    return Path(library_folder) / "team" / "weekly"


def season_slug(sched) -> str:
    return _safe(schedule.slug((sched or {}).get("season") or "season"))


def snapshot_path(library_folder, season: str, series_id: str, week: int) -> Path:
    return weekly_dir(library_folder) / _safe(season) / _safe(series_id) / f"w{int(week):02d}.json"


def _read(p: Path):
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def load_snapshot(library_folder, season: str, series_id: str, week: int) -> dict | None:
    d = _read(snapshot_path(library_folder, season, series_id, week))
    return d if d and d.get("schema") == SCHEMA else None


def _write_json(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=p.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=1, ensure_ascii=False)
        fsutil.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _age_s(snap, now: datetime) -> float | None:
    dt = _parse_dt((snap or {}).get("fetched_at"))
    return None if dt is None else (now - dt).total_seconds()


def is_fresh(snap, now: datetime) -> bool:
    """A non-final snapshot fetched under 10 minutes ago (a refresh would only hit the cache)."""
    age = _age_s(snap, now)
    return bool(snap) and not snap.get("final") and age is not None and 0 <= age < FRESH_S


# ---------------------------------------------------------------- what to refresh (no requests)
def _chosen(sched, mine_series):
    """(series in use, series left out over MAX_SERIES)."""
    rows = [s for s in series.resolve(mine_series or {}, sched) if not s.get("missing")]
    return rows[:MAX_SERIES], rows[MAX_SERIES:]


def _week(ser, wk):
    return next((w for w in ser.get("weeks") or [] if w.get("week") == wk), None)


def _window(w):
    a = _day(w.get("start"))
    return (a, a + timedelta(days=7)) if a else (None, None)


def _car_items(api):
    try:
        return [c for c in (api.cached_items("cars") or []) if isinstance(c, dict) and c.get("id") is not None]
    except Exception:  # noqa: BLE001  (no client, no lists)
        return []


def _item_name(it) -> str:
    from .app import item_name
    return item_name(it)


def ids_for(api, ser, w, matcher=None):
    """(track_id, [car_ids], {car_id: car name}) from the cached lists only. Every car of the series is
    mapped with schedule.same_car, so a multi-class (or multi-car) board covers all of them."""
    if api is None:
        return None, [], {}
    m = matcher or prepare.matcher_for(api)
    cars = ser.get("cars") or []
    tid, cid = prepare._ids_from_lists(api, w["track"], cars, m)
    names, ids = {}, []
    items = _car_items(api)
    for it in items:
        nm = _item_name(it)
        if any(schedule.same_car(nm, c) for c in cars):
            try:
                i = int(it["id"])
            except (TypeError, ValueError):
                continue
            if i not in ids:
                ids.append(i)
                names[i] = nm
    if cid is not None and int(cid) not in ids:
        ids.append(int(cid))
    for it in items:
        try:
            names.setdefault(int(it["id"]), _item_name(it))
        except (TypeError, ValueError):
            pass
    return tid, sorted(ids), names


def _jobs(sched, mine_series, now, series_ids=None):
    """(season slug, series used, series left out, [(series, this week)], [(k, series, past week)]).
    k = 0 is the most recent past week. No requests."""
    used, left = _chosen(sched, mine_series)
    if series_ids:
        want = set(series_ids)
        used = [s for s in used if s["id"] in want]
    today = now.date()
    current, past = [], []
    for ser in used:
        # the series' own week dates (they can differ from the season's by a day)
        w = next((w for w in ser.get("weeks") or [] if _window(w)[0] and _window(w)[0] <= today < _window(w)[1]), None)
        if w is None and sched:
            w = _week(ser, schedule.week_on(sched, today))
        if w is not None:
            current.append((ser, w))
        weeks = sorted((w for w in ser.get("weeks") or [] if _window(w)[1] and _window(w)[1] <= today),
                       key=lambda w: w["week"], reverse=True)
        past += [(k, ser, w) for k, w in enumerate(weeks)]
    return season_slug(sched), used, left, current, past


def _past_candidates(lib, season, past, force_old, now):
    """Past weeks worth fetching, most recent first across series (k = 0 is last week)."""
    cands = []
    for k, ser, w in sorted(past, key=lambda x: x[0]):
        snap = load_snapshot(lib, season, ser["id"], w["week"])
        if force_old:
            if is_fresh(snap, now):
                continue
            cands.append((ser, w, snap))
            continue
        if k >= AUTO_PAST_WEEKS:
            continue
        if snap is None or (not snap.get("final") and not is_fresh(snap, now)):
            cands.append((ser, w, snap))
    return cands


def _job_rows(api, cfg, sched, mine_series, now, series_ids=None, force_old=False):
    lib = cfg["library_folder"]
    season, used, left, current, past = _jobs(sched, mine_series, now, series_ids)
    m = prepare.matcher_for(api) if api is not None else None

    def job(ser, w, is_past, snap):
        tid, cids, names = ids_for(api, ser, w, m)
        j = {"series": ser, "week": w, "past": is_past, "track_id": tid, "car_ids": cids, "car_names": names,
             "ids_known": tid is not None and bool(cids), "snapshot": snap, "est_requests": 0, "why": None}
        if not j["ids_known"]:
            j["why"] = "no_ids"
        elif snap and snap.get("final") and not force_old:
            j["why"] = "final"
        elif is_fresh(snap, now):
            j["why"] = "fresh"
        else:
            j["est_requests"] = 2 if int((snap or {}).get("requests") or 1) >= 2 else 1
        return j

    jobs = [job(ser, w, False, load_snapshot(lib, season, ser["id"], w["week"])) for ser, w in current]
    n_past = 0
    for ser, w, snap in _past_candidates(lib, season, past, force_old, now):
        if n_past >= MAX_PAST_WEEKS:
            break
        j = job(ser, w, True, snap)
        if j["why"] == "no_ids":
            continue
        jobs.append(j)
        n_past += 1
    return season, used, left, jobs


def _left_note(left):
    if not left:
        return None
    n = len(left)
    return (f"{n} of your series {'was' if n == 1 else 'were'} left out "
            f"(Debrief refreshes at most {MAX_SERIES}).")


def plan(cfg, api_or_none, sched, mine_series) -> dict:
    """What a refresh would do, and about how many Garage 61 requests it would take. Never a request."""
    now = _now()
    out = {"series": [], "est_total": 0, "skipped": [], "season": (sched or {}).get("season"),
           "left_out": 0, "notes": []}
    if not sched:
        out["notes"].append("Load this season's schedule first (Prepare).")
        return out
    season, used, left, jobs = _job_rows(api_or_none, cfg, sched, mine_series, now)
    out["left_out"] = len(left)
    if _left_note(left):
        out["notes"].append(_left_note(left))
    for s in left:
        out["skipped"].append({"id": s["id"], "name": s["name"], "note": _left_note(left)})
    seen = set()
    for j in jobs:
        ser, w = j["series"], j["week"]
        row = {"id": ser["id"], "name": ser["name"], "week": w["week"], "track": w.get("track"),
               "ids_known": j["ids_known"], "est_requests": j["est_requests"],
               "cached": j["why"] in ("fresh", "final"), "past": j["past"], "final": bool((j["snapshot"] or {}).get("final"))}
        out["series"].append(row)
        out["est_total"] += j["est_requests"]
        if not j["ids_known"] and ser["id"] not in seen:
            out["skipped"].append({"id": ser["id"], "name": ser["name"], "note": NO_IDS_NOTE})
        seen.add(ser["id"])
    return out


# ---------------------------------------------------------------- one board
def _me(api) -> tuple[str, str | None]:
    """(your Garage 61 slug, your name) from the saved "me" answer; me() only if none was ever saved."""
    me = None
    peek = getattr(api, "peek", None)
    if callable(peek):
        try:
            me = peek("me")
        except Exception:  # noqa: BLE001
            me = None
    if not isinstance(me, dict):
        try:
            me = api.me() or {}
        except Exception:  # noqa: BLE001
            me = {}
    name = " ".join(filter(None, [g(me, "firstName", "firstname"), g(me, "lastName", "lastname")])).strip()
    return g(me, "slug", default="") or "", name or None


def _lap_car(l):
    c = g(l, "car_id", "carId", "car")
    if isinstance(c, dict):
        return g(c, "id"), g(c, "name")
    return c, None


def _in_window(l, a: date, b: date) -> bool:
    dt = _parse_dt(g(l, "startTime", "start_time"))
    if dt is None:
        return True   # Garage 61 already searched from the week start; no date to judge by
    return _midnight(a) <= dt < _midnight(b)


def _ok_lap(l, a, b) -> bool:
    return team.usable(l) and _in_window(l, a, b)


def build_board(laps, my_slug, my_name, car_names, a: date, b: date, multiclass: bool) -> list:
    """Each driver's best usable lap inside [a, b) (per class in a multi-class series), with overall
    position and in-class position and gap."""
    cands = []
    for l in laps or []:
        if not isinstance(l, dict) or not _ok_lap(l, a, b):
            continue
        d = team.driver_of(l)
        if not d["slug"]:
            continue
        cid, cname = _lap_car(l)
        try:
            cid = int(cid) if cid is not None else None
        except (TypeError, ValueError):
            cid = None
        car = cname or car_names.get(cid) or ""
        cls = schedule.class_of(car)
        is_me = bool(my_slug) and d["slug"] == my_slug
        cands.append({"key": "me" if is_me else d["slug"], "name": my_name if is_me and my_name else d["name"],
                      "t": float(team.app.lap_time(l)), "car": car, "class": cls,
                      "date": team.lap_day(l), "irating": g(l, "driverRating"), "is_me": is_me})
    # plausibility per class: a GT3 lap isn't judged against the GTP times
    by_cls = {}
    for c in cands:
        by_cls.setdefault(c["class"] if multiclass else None, []).append(c)
    best = {}
    for k, rows in by_cls.items():
        anchor = team.anchor_for([r["t"] for r in rows])
        for r in rows:
            if not team.plausible(r["t"], anchor):
                continue
            kk = (r["key"], k)
            if kk not in best or r["t"] < best[kk]["t"]:
                best[kk] = r
    board = sorted(best.values(), key=lambda r: (r["t"], r["name"]))
    lead = {}
    cpos = {}
    for pos, r in enumerate(board, 1):
        k = r["class"] if multiclass else None
        lead.setdefault(k, r["t"])
        cpos[k] = cpos.get(k, 0) + 1
        r["pos"] = pos
        r["class_pos"] = cpos[k]
        r["gap"] = round(r["t"] - lead[k], 3)
        r["gap_pct"] = round((r["t"] / lead[k] - 1) * 100, 3) if lead[k] else 0.0
        r["t"] = round(r["t"], 3)
    return [{k: r[k] for k in ("key", "name", "t", "car", "class", "date", "irating", "pos", "class_pos",
                               "gap", "gap_pct", "is_me")} for r in board]


def _needs_second_look(laps, a, b) -> bool:
    """True when some driver's returned "best" can't be used: partial, unclean, pit, set outside the race
    week, or implausible against the others (an 11 s partial lap Garage 61 didn't flag)."""
    timed = [team.app.lap_time(l) for l in laps if isinstance(l, dict) and team.usable(l)]
    anchor = team.anchor_for(timed)
    for l in laps:
        if not isinstance(l, dict):
            continue
        if not _ok_lap(l, a, b) or not team.plausible(team.app.lap_time(l), anchor):
            return True
    return False


def fetch_week(api, cfg, ser, w, track_id, car_ids, car_names, my_slug, my_name, season_name, now, log=print):
    """One series-week: at most 2 Garage 61 requests. Returns the snapshot dict (not yet written)."""
    a, b = _window(w)
    multiclass = bool(ser.get("multiclass"))
    slug = cfg.get("team_slug") or "midfield-menace"
    base = dict(tracks=[int(track_id)], cars=list(car_ids), teams=[slug], drivers=["me"],
                after=_iso(_midnight(a)), age=max(1, (now.date() - a).days + 2))
    used = 1
    laps = api.find_laps(group="driver-car" if multiclass else "driver", limit=200, **base) or []
    if _needs_second_look(laps, a, b):
        used = 2
        laps = list(laps) + list(api.find_laps(group="none", limit=1000, **base) or [])
    board = build_board(laps, my_slug, my_name, car_names, a, b, multiclass)
    final = now > _midnight(b) + FINAL_AFTER
    return {"schema": SCHEMA, "season": season_name, "series_id": ser["id"], "series": ser["name"],
            "week": w["week"], "start": a.isoformat(), "end": b.isoformat(), "track": w.get("track"),
            "track_id": int(track_id), "car_ids": list(car_ids), "multiclass": multiclass,
            "fetched_at": _iso(now), "final": final,
            "requests": used,
            "board": board, "note": None if board else "No team laps in this week yet."}


def refresh(api, cfg, sched, mine_series, series_ids=None, force_old=False, log=print) -> dict:
    """Fetch this week's boards (and up to MAX_PAST_WEEKS missing past weeks). 1-2 requests per
    series-week, 0 for fresh or final snapshots. Writes each snapshot as soon as it's done."""
    now = _now()
    lib = cfg["library_folder"]
    out = {"season": (sched or {}).get("season"), "done": [], "skipped": [], "notes": [], "requests": 0,
           "left_out": 0}
    if not sched:
        out["notes"].append("Load this season's schedule first (Prepare).")
        return out
    season, used, left, jobs = _job_rows(api, cfg, sched, mine_series, now, series_ids, force_old)
    out["left_out"] = len(left)
    if _left_note(left):
        out["notes"].append(_left_note(left))
        log(_left_note(left))
    for s in left:
        out["skipped"].append({"id": s["id"], "name": s["name"], "note": _left_note(left)})
    work = [j for j in jobs if j["est_requests"] > 0]
    my_slug, my_name = _me(api) if work else ("", None)
    for j in jobs:
        ser, w = j["series"], j["week"]
        label = f"{ser['name']} week {w['week']}"
        if j["why"] == "no_ids":
            out["skipped"].append({"id": ser["id"], "name": ser["name"], "week": w["week"], "note": NO_IDS_NOTE})
            log(f"{label}: skipped. {NO_IDS_NOTE}")
            continue
        if j["why"] in ("fresh", "final"):
            out["done"].append({"id": ser["id"], "week": w["week"], "requests": 0, "cached": True})
            continue
        log(f"{label}: loading the team's best laps at {w.get('track')}...")
        snap = fetch_week(api, cfg, ser, w, j["track_id"], j["car_ids"], j["car_names"], my_slug, my_name,
                          sched.get("season"), now, log)
        _write_json(snapshot_path(lib, season, ser["id"], w["week"]), snap)   # checkpoint
        n = snap["requests"]
        out["requests"] += n
        out["done"].append({"id": ser["id"], "week": w["week"], "requests": n, "cached": False,
                            "drivers": len(snap["board"])})
    _write_json(weekly_dir(lib) / season / "status.json",
                {"schema": SCHEMA, "season": sched.get("season"), "updated": _iso(now),
                 "skipped": out["skipped"], "notes": out["notes"], "left_out": out["left_out"]})
    log(f"Weekly leaderboards ready. Garage 61 requests used: {out['requests']}.")
    return out


# ---------------------------------------------------------------- view (no requests)
def _verdict(shared, med):
    if shared < 2 or med is None:
        return None
    a = abs(med)
    if a < 0.15:
        return "about level"
    word = "quicker" if med > 0 else "slower"
    return f"slightly {word} than you" if a < 0.5 else f"{word} than you"


def _summ(weeks):
    gaps = [x["gap_pct"] for x in weeks]
    return {"shared_weeks": len(gaps), "ahead": sum(1 for x in gaps if x < 0), "behind": sum(1 for x in gaps if x > 0),
            "median_gap_pct": round(statistics.median(gaps), 3) if gaps else None}


def head_to_heads(snaps: list) -> list:
    """Your best against each teammate's in the weeks you both set a time, by series and class."""
    people = {}
    for s in snaps:
        board = s.get("board") or []
        mine = {}
        for r in board:
            if r.get("is_me") or r.get("key") == "me":
                k = r.get("class") if s.get("multiclass") else None
                if k not in mine or r["t"] < mine[k]["t"]:
                    mine[k] = r
        for r in board:
            if r.get("is_me") or r.get("key") == "me" or not r.get("key"):
                continue
            p = people.setdefault(r["key"], {"key": r["key"], "name": r.get("name") or r["key"], "_series": {}})
            k = r.get("class") if s.get("multiclass") else None
            you = mine.get(k)
            if not you or not r.get("t") or not you.get("t"):
                continue
            cls = r.get("class")
            ent = p["_series"].setdefault((s.get("series_id"), k), {"id": s.get("series_id"), "name": s.get("series"),
                                                                  "class": cls, "weeks": {}})
            wk = s.get("week")
            gp = round((you["t"] / r["t"] - 1) * 100, 3)
            prev = ent["weeks"].get(wk)
            if prev is None or r["t"] < prev["them"]:
                ent["weeks"][wk] = {"week": wk, "you": you["t"], "them": r["t"], "gap_pct": gp}
    out = []
    for p in people.values():
        ser_rows, allw = [], []
        for ent in p.pop("_series").values():
            weeks = sorted(ent["weeks"].values(), key=lambda x: x["week"] or 0)
            allw += weeks
            ser_rows.append({"id": ent["id"], "name": ent["name"], "class": ent["class"], **_summ(weeks), "weeks": weeks})
        tot = _summ(allw)
        p["series"] = ser_rows
        p.update(tot)
        p["verdict"] = _verdict(tot["shared_weeks"], tot["median_gap_pct"])
        out.append(p)
    out.sort(key=lambda p: (-p["shared_weeks"], str(p["name"]).lower()))
    return out


def _seasons(lib):
    root = weekly_dir(lib)
    try:
        return [p for p in root.iterdir() if p.is_dir()]
    except OSError:
        return []


def view(library_folder, sched=None) -> dict:
    """Saved snapshots of the season (the schedule's, else the newest one saved) plus season
    head-to-heads. Reads files only."""
    lib = Path(library_folder)
    out = {"season": (sched or {}).get("season"), "season_slug": None, "series": [], "h2h": [], "notes": [],
           "skipped": []}
    if sched:
        folder = weekly_dir(lib) / season_slug(sched)
    else:
        best, best_t = None, -1.0
        for p in _seasons(lib):
            ts = [f.stat().st_mtime for f in p.glob("*/w*.json")]
            if ts and max(ts) > best_t:
                best, best_t = p, max(ts)
        folder = best
    if folder is None or not folder.is_dir():
        return out
    out["season_slug"] = folder.name
    snaps = []
    for f in sorted(folder.glob("*/w*.json")):
        d = _read(f)
        if not d or d.get("schema") != SCHEMA or not isinstance(d.get("board"), list):
            continue
        snaps.append(d)
    if not snaps:
        return out
    out["season"] = out["season"] or snaps[0].get("season")
    try:
        order = [x["id"] for x in series.load(lib)["series"]]
    except Exception:  # noqa: BLE001
        order = []
    by = {}
    for s in snaps:
        by.setdefault(s.get("series_id"), []).append(s)
    today = _now().date()
    cur_wk = schedule.week_on(sched, today) if sched else None
    ids = sorted(by, key=lambda i: (order.index(i) if i in order else len(order), str(i)))
    for sid in ids:
        ws = sorted(by[sid], key=lambda s: s.get("week") or 0, reverse=True)
        cur = cur_wk
        if cur is None:
            cur = next((s["week"] for s in ws if _day(s.get("start")) and _day(s.get("start")) <= today
                        < _day(s.get("start")) + timedelta(days=7)), None)
        out["series"].append({
            "id": sid, "name": ws[0].get("series") or sid, "multiclass": bool(ws[0].get("multiclass")),
            "current_week": cur,
            "weeks": [{k: s.get(k) for k in ("week", "start", "end", "track", "track_id", "car_ids", "multiclass",
                                             "fetched_at", "final", "requests", "note", "board")} for s in ws]})
    out["h2h"] = head_to_heads(snaps)
    st = _read(folder / "status.json") or {}
    out["notes"] = [n for n in st.get("notes") or [] if isinstance(n, str)]
    out["skipped"] = [x for x in st.get("skipped") or [] if isinstance(x, dict)]
    out["generated_at"] = _iso(_now())
    return out
