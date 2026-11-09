"""2.24: team sync. "Refresh team data" also keeps a local copy of what your Garage 61 teams have shared with you:
every teammate's laps (practice, qualifying and race), found from the team's driving statistics, so the member pages
and the reference picker can read them without asking Garage 61 again.

It follows the owner's own sync (sync.py): the same lap slimming, durable writes, 429 back-off and telemetry rules.
  * the team statistics (1 request per team) list every member, track, car and day driven since the last sync
    (first sync: the last 180 days); your own driving is left to your own sync
  * only members and tracks whose driving changed are asked for, at most 10 tracks per request, per member,
    race + practice + qualifying, unclean laps included (marked), no car filter
  * paced about 3 s apart; after two 429s it stops and the next Refresh carries on ("Synced 6 of 14 tracks")
  * telemetry: the best lap of each member at each track and car, a few per Refresh, never twice
Layout (next to your own sync, same per-account folder):
    <cache>/<account>/team/<team-slug>/state.json
    <cache>/<account>/team/<team-slug>/<member-key>/member.json
    <cache>/<account>/team/<team-slug>/<member-key>/laps/<track-id>.json     every lap, keyed by lap id
"""
from __future__ import annotations

import json
import re
import statistics
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import sync
from .engine import g

SCHEMA = 1
FIRST_DAYS = 180            # a first sync goes back this far
PACE_S = 3.0                # seconds between requests
STOP_AFTER_429 = 2          # this many 429s in one run: stop, resume on the next Refresh
TRACKS_PER_REQUEST = 10
TEL_BUDGET = 25             # telemetry downloads per Refresh (the rest wait for the next one)
POINTER = "teamlib.json"    # <library>/team/teamlib.json says where the team library is (the cache is per account)



def _is_member_lap(lap, m) -> bool:
    """True when a team lap belongs to member m (matched by Garage 61 slug, else by name)."""
    d = lap.get("driver") if isinstance(lap.get("driver"), dict) else None
    if not d:
        return False
    if d.get("slug") and m.get("slug"):
        return d["slug"] == m["slug"]
    nm = " ".join(str(x) for x in (d.get("firstName"), d.get("lastName")) if x) or d.get("name") or ""
    return bool(nm) and nm.strip().lower() == str(m.get("name") or "").strip().lower()

def _safe(s) -> str:
    s = re.sub(r"[^a-z0-9._-]+", "-", str(s or "").strip().lower()).strip("-.")
    return s[:60] or "unknown"


def root_for(api) -> Path:
    return Path(api.cache) / "team"


def write_pointer(library_folder, root) -> None:
    p = Path(library_folder) / "team" / POINTER
    sync._write_json(p, {"schema": SCHEMA, "root": str(root)})


def root_from_library(library_folder) -> Path | None:
    d = sync._read_json(Path(library_folder) / "team" / POINTER, {})
    r = d.get("root") if isinstance(d, dict) else None
    return Path(r) if r and Path(r).is_dir() else None


def _state(tdir: Path) -> dict:
    st = sync._read_json(tdir / "state.json", {})
    if not isinstance(st, dict) or st.get("schema") != SCHEMA:
        st = {"schema": SCHEMA}
    for k in ("items", "fp", "tel", "names", "members", "track_len"):
        if not isinstance(st.get(k), dict):
            st[k] = {}
    return st


def _save(tdir: Path, st: dict) -> None:
    st["schema"] = SCHEMA
    sync._write_json(tdir / "state.json", st)


def _stat_rows(api, team_slug, start, me_slug, log):
    """[{key, slug, name, track, car, day, laps}] for the team's other members. Raises on a refusal."""
    from . import team as T
    out = []
    for r in api.team_statistics(team_slug, start) or []:
        if not isinstance(r, dict):
            continue
        who = g(r, "driver", "user", "member")
        d = T.driver_of({"driver": who}) if isinstance(who, dict) else {"slug": str(who or ""), "name": str(who or "")}
        tr, car = g(r, "track", "trackId", "track_id"), g(r, "car", "carId", "car_id")
        tid = sync._int(sync._id(tr))
        cid = sync._int(sync._id(car))
        if tid is None or not (d["slug"] or d["name"]) or (me_slug and d["slug"] == me_slug):
            continue
        out.append({"key": _safe(d["slug"] or d["name"]), "slug": d["slug"], "name": d["name"], "track": tid, "car": cid,
                    "day": str(g(r, "day", "date", "startDate", "start", default="") or "")[:10],
                    "laps": int(g(r, "lapCount", "laps", "lapsDriven", default=0) or 0),
                    "fp": sync._stats_rows([r]).get(f"{tid}:{cid}", {})})
    return out


def sync_team(api, root: Path, team: dict, me_slug: str, log, stop, telemetry: str, now: datetime, tel_budget: list) -> dict:
    slug = str(g(team, "slug", default="") or "")
    name = str(g(team, "name", default="") or slug)
    tdir = root / _safe(slug)
    st = _state(tdir)
    res = {"slug": slug, "name": name, "total": 0, "done": 0, "limited": False, "new_laps": 0, "error": "",
           "tel_done": 0, "tel_left": 0}
    st["name"] = name
    first = not st.get("first_complete")
    last = sync._parse(st.get("last_success"))
    since_dt = (now - timedelta(days=FIRST_DAYS)) if first or last is None else last - sync.OVERLAP
    since = sync._iso(since_dt)
    edge = since[:10]
    try:
        rows = _stat_rows(api, slug, since, me_slug, log)
    except Exception as e:  # noqa: BLE001
        stt = getattr(e, "status", None)
        res["error"] = (f"Garage 61 wouldn't share {name}'s driving statistics ({stt or type(e).__name__}), "
                        "so nothing could be synced for this team.")
        log("  " + res["error"])
        return res
    run_start = sync._iso(now)
    names = st["names"]
    for t in api.tracks() or []:
        if isinstance(t, dict) and g(t, "id") is not None:
            v = g(t, "variant")
            names.setdefault("t" + str(g(t, "id")), str(g(t, "name") or "") + (f" ({v})" if v else ""))
            ln = g(t, "lap_length", "lapLength", "length")
            if isinstance(ln, (int, float)) and not isinstance(ln, bool):
                st["track_len"][str(g(t, "id"))] = ln
    for c in api.cars() or []:
        if isinstance(c, dict) and g(c, "id") is not None:
            names.setdefault("c" + str(g(c, "id")), str(g(c, "name") or ""))
    # what changed: per member and track, the days driven and their fingerprints
    work: dict = {}
    for r in rows:
        m = work.setdefault(r["key"], {"slug": r["slug"], "name": r["name"], "tracks": {}})
        st["members"][r["key"]] = {"slug": r["slug"], "name": r["name"]}
        t = m["tracks"].setdefault(r["track"], {"newest": "", "fp": {}})
        t["newest"] = max(t["newest"], r["day"])
        for d, fp in (r["fp"] or {}).items():
            t["fp"][d] = fp
        if not r["fp"] and r["day"]:
            t["fp"][r["day"]] = ""
    todo: dict = {}
    for mk, m in work.items():
        for tid, t in m["tracks"].items():
            ik = f"{mk}:{tid}"
            info = st["items"].get(ik) or {}
            saved = st["fp"].get(ik) or {}
            changed = (not info.get("done")) or any(
                (d not in saved) if d <= edge else (not fp or saved.get(d) != fp) for d, fp in t["fp"].items())
            if changed:
                todo.setdefault(mk, []).append((tid, t))
    res["total"] = sum(len(v) for v in todo.values())
    log(f"  Team {name}: {len(rows)} statistics rows, {len(work)} members, {res['total']} member tracks to sync")
    r0 = api.rate_limited
    limit = TRACKS_PER_REQUEST
    limited = False

    def hit_limit():
        return api.rate_limited - r0 >= STOP_AFTER_429

    for mk, items in sorted(todo.items()):
        if limited or stop():
            break
        items.sort(key=lambda x: x[1]["newest"], reverse=True)
        m = work[mk]
        mdir = tdir / mk
        i = 0
        while i < len(items) and not limited:
            if stop():
                break
            chunk = items[i:i + limit]
            afters = []
            for tid, _t in chunk:
                info = st["items"].get(f"{mk}:{tid}") or {}
                p = sync._parse(info.get("synced_until"))
                afters.append(since_dt if p is None or first else min(since_dt, p - sync.OVERLAP))
            after_dt = min(afters)
            age = max(1, (now - after_dt).days + 2)
            got, offset, total, ok = [], 0, None, True
            for _page in range(sync.MAX_PAGES):
                if hit_limit():
                    limited, ok = True, False
                    break
                try:
                    page, tot = api.laps_page(teams=[slug], tracks=[t for t, _ in chunk], group="none",
                                              unclean=True, after=sync._iso(after_dt), age=age, limit=sync.PAGE, offset=offset)
                except Exception as e:  # noqa: BLE001
                    stt = getattr(e, "status", None)
                    msg = ((getattr(e, "detail", "") or "") + " " + str(e)).replace("\n", " ")[:200]
                    if stt == 400 and "more than" in msg:
                        from . import team as T
                        lim = T._track_limit(msg, limit)
                        if lim < limit:
                            log(f"  Garage 61 allows only {lim} tracks per request; using chunks of {lim}")
                            limit = lim
                            chunk = None
                            break
                    if stt == 401 or getattr(e, "fatal", False):
                        raise
                    log(f"  Garage 61 refused {m['name']}'s laps ({stt or type(e).__name__}): {msg}")
                    if stt in (429, None) or (isinstance(stt, int) and stt >= 500):
                        limited = True
                    ok = False
                    break
                if tot is not None:
                    total = tot
                page = [l for l in page if isinstance(l, dict)]
                got.extend(page)
                offset += len(page)
                if not page or (total is not None and offset >= total) or (total is None and len(page) < sync.PAGE):
                    break
            if chunk is None:        # smaller chunks, same position
                continue
            # keep what arrived, whatever happened (laps are keyed by id: nothing is saved twice)
            by_track: dict = {}
            for l in got:
                # Garage 61 rejects a member slug as a "drivers" filter, so the team's laps for these
                # tracks come back for everyone and are sorted to the member here.
                if not _is_member_lap(l, m):
                    continue
                by_track.setdefault(sync.lap_track(l), []).append(l)
            for tid, laps in by_track.items():
                if tid is None:
                    continue
                res["new_laps"] += _merge(mdir, tid, laps)
            sync._write_json(mdir / "member.json", {"name": m["name"], "slug": m["slug"], "key": mk,
                                                    "last_synced": run_start if ok else (_member(mdir).get("last_synced"))})
            if ok:
                for tid, t in chunk:
                    ik = f"{mk}:{tid}"
                    st["items"][ik] = {"done": True, "synced_until": run_start}
                    st["fp"][ik] = {**(st["fp"].get(ik) or {}), **{d: fp for d, fp in t["fp"].items() if fp}}
                    res["done"] += 1
                i += len(chunk)
            elif not limited:
                # this request failed for a reason other than "slow down": skip these tracks this time
                # (they are tried again next Refresh) rather than asking again forever
                res.setdefault("skipped", []).append(f"{m['name']}: {len(chunk)} track(s)")
                i += len(chunk)
            _save(tdir, st)
            if not ok:
                break
    res["limited"] = limited
    res["stopped"] = bool(stop())
    complete = res["done"] >= res["total"] and not limited and not res["stopped"]
    if complete:
        st["first_complete"] = True
        st["last_success"] = run_start
    # telemetry: each member's best lap per track and car
    if telemetry != "none" and not limited and not stop():
        _telemetry(api, tdir, st, run_start, now, log, stop, tel_budget, res, hit_limit)
    elif telemetry != "none":
        res["tel_left"] = len(_wanted(api, tdir, st, now))
    st["last_summary"] = {k: res[k] for k in ("total", "done", "limited", "new_laps", "tel_done", "tel_left")}
    st["last_run"] = run_start
    _save(tdir, st)
    return res


def _merge(mdir: Path, tid, laps) -> int:
    p = mdir / "laps" / f"{int(tid)}.json"
    have = sync._read_json(p, {})
    if not isinstance(have, dict):
        have = {}
    before = len(have)
    for l in laps:
        lid = g(l, "id")
        if lid is None:
            continue
        s = sync.slim(l)
        if isinstance(l.get("driver"), dict):
            s["driver"] = {k: l["driver"][k] for k in ("slug", "firstName", "lastName") if k in l["driver"]}
        have[str(lid)] = s
    sync._write_json(p, have)
    return len(have) - before


def _member(mdir: Path) -> dict:
    d = sync._read_json(mdir / "member.json", {})
    return d if isinstance(d, dict) else {}


def _all_laps(mdir: Path) -> list:
    out = []
    for p in sorted((mdir / "laps").glob("*.json")) if (mdir / "laps").is_dir() else []:
        d = sync._read_json(p, {})
        if isinstance(d, dict):
            out.extend(v for v in d.values() if isinstance(v, dict))
    return out


def best_laps(mdir: Path) -> dict:
    """{(track_id, car_id): best lap dict} per member, a clean lap if there is one (else the quickest, marked)."""
    best: dict = {}
    for l in _all_laps(mdir):
        t = sync.lap_secs(l)
        if t is None:
            continue
        k = (sync.lap_track(l), sync.lap_car(l))
        score = (0 if sync.lap_clean(l) else 1, t)
        if k not in best or score < best[k][0]:
            best[k] = (score, l)
    return {k: v[1] for k, v in best.items()}


def _wanted(api, tdir: Path, st: dict, now) -> list:
    tel = st["tel"]
    out = []
    for mdir in sorted(p for p in tdir.iterdir() if p.is_dir()) if tdir.is_dir() else []:
        for (t, c), l in best_laps(mdir).items():
            lid = str(g(l, "id"))
            rec = tel.get(lid)
            if rec and rec.get("s") == "done":
                continue
            if rec and rec.get("s") == "none":
                at = sync._parse(rec.get("at"))
                if at is not None and now - at < sync.TEL_RETRY:
                    continue
            out.append((sync.lap_start(l), lid))
    out.sort(reverse=True)
    return [lid for _s, lid in out]


def _telemetry(api, tdir, st, run_start, now, log, stop, budget, res, hit_limit):
    want = _wanted(api, tdir, st, now)
    tel = st["tel"]
    fails = run_fail = 0
    for n, lid in enumerate(want):
        if stop() or hit_limit():
            break
        if budget[0] <= 0:
            break
        if api.has_csv(lid):
            tel[lid] = {"s": "done", "at": run_start}
            continue
        budget[0] -= 1
        try:
            csv = api.lap_csv(lid)
        except Exception as e:  # noqa: BLE001
            if getattr(e, "fatal", False) or getattr(e, "status", None) == 401:
                raise
            run_fail += 1
            fails = int((tel.get(lid) or {}).get("n") or 0) + 1
            tel[lid] = {"s": "fail", "n": fails, "at": run_start}
            if fails >= sync.TEL_TRIES:
                tel[lid] = {"s": "none", "why": "Garage 61 kept failing to send this lap's telemetry.", "at": run_start}
            if run_fail >= sync.TEL_FAIL_RUN:
                log("  Garage 61 isn't sending telemetry right now; the rest is fetched next Refresh.")
                break
            continue
        run_fail = 0
        if csv is None:
            tel[lid] = {"s": "none", "why": "Garage 61 hasn't shared telemetry for this lap.", "at": run_start}
        else:
            tel[lid] = {"s": "done", "at": run_start}
            res["tel_done"] += 1
    res["tel_left"] = len(_wanted(api, tdir, st, now))
    _save(tdir, st)


def run(api, log=print, progress=None, should_stop=None, telemetry: str = "best", now: datetime | None = None,
        library_folder=None) -> dict:
    """Sync every one of your Garage 61 teams. Never raises for a refusal: a team that can't be read says why."""
    stop = should_stop or (lambda: False)
    now = now or datetime.now(timezone.utc)
    req0 = api.requests_made
    root = root_for(api)
    out = {"teams": [], "total": 0, "done": 0, "limited": False, "new_laps": 0, "message": "", "requests": 0, "notes": []}
    old_gap = (getattr(api, "min_gap", None), getattr(api, "base_gap", None))
    for attr in ("min_gap", "base_gap"):
        if isinstance(getattr(api, attr, None), (int, float)):
            setattr(api, attr, max(getattr(api, attr), PACE_S))
    try:
        try:
            teams = [t for t in (api.teams() or []) if isinstance(t, dict) and g(t, "slug")]
            me = api.me() or {}
        except Exception as e:  # noqa: BLE001
            out["message"] = "Garage 61 wouldn't list your teams, so there was nothing to sync. " + str(e)[:150]
            return out
        me_slug = str(g(me, "slug", default="") or "")
        if not teams:
            out["message"] = "You're not in a Garage 61 team, so there is no team data to sync."
            return out
        budget = [TEL_BUDGET]
        hidden = hidden_teams(library_folder)
        for i, t in enumerate(teams):
            if str(g(t, "slug")) in hidden:
                out["notes"].append(f"{g(t, 'name', default=g(t, 'slug'))}: hidden in Settings > Team, so it was not synced.")
                continue
            if progress:
                try:
                    progress(0.1 + 0.8 * i / len(teams), f"Syncing team {g(t, 'name', default=g(t, 'slug'))}...")
                except Exception:  # noqa: BLE001
                    pass
            r = sync_team(api, root, t, me_slug, log, stop, telemetry, now, budget)
            out["teams"].append(r)
            for k in ("total", "done", "new_laps"):
                out[k] += r[k]
            out["limited"] = out["limited"] or r["limited"]
            if r["error"]:
                out["notes"].append(r["error"])
            if r["limited"] or stop():
                break
    finally:
        for attr, v in zip(("min_gap", "base_gap"), old_gap):
            if v is not None:
                setattr(api, attr, v)
        out["requests"] = api.requests_made - req0
    left = out["total"] - out["done"]
    tel_left = sum(r.get("tel_left", 0) for r in out["teams"])
    if left > 0 or out["limited"]:
        out["message"] = (f"Synced {out['done']} of {out['total']} tracks; Refresh again to continue."
                          + (" Garage 61 asked Debrief to slow down." if out["limited"] else ""))
    else:
        out["message"] = (f"Team sync done: {out['new_laps']} new laps"
                          + (f"; telemetry for {tel_left} more laps waits for the next Refresh" if tel_left else "") + ".")
    if library_folder is not None:
        try:
            write_pointer(library_folder, root)
        except OSError:
            pass
    return out


def hidden_teams(library_folder) -> set:
    if library_folder is None:
        return set()
    d = sync._read_json(Path(library_folder) / "team" / "teamprefs.json", {})
    return {str(x) for x in (d.get("hidden") if isinstance(d, dict) else None) or []}


def set_hidden(library_folder, slug, hidden: bool) -> None:
    h = hidden_teams(library_folder)
    (h.add if hidden else h.discard)(str(slug))
    sync._write_json(Path(library_folder) / "team" / "teamprefs.json", {"hidden": sorted(h)})


def summary(root, library_folder, track_name=None, same_track=None) -> dict:
    """For Settings > Team, Review options and Prepare (no requests): your teams (name, members, last synced, hidden),
    the teammates and cars a comparison could use, and what teammates have at one track."""
    hidden = hidden_teams(library_folder)
    teams, people, here = [], {}, []
    for tdir in _teams(root) if root else []:
        st = _state(tdir)
        last = ""
        for mk, m in st["members"].items():
            md = tdir / mk
            ls = str(_member(md).get("last_synced") or "")
            last = max(last, ls)
            cars = set()
            for (t, c), l in best_laps(md).items():
                cars.add(_name_of(st, "c", c))
                if track_name and same_track and same_track(_name_of(st, "t", t), track_name):
                    here.append((sync.lap_secs(l) or 1e9, f"{m.get('name') or mk} ({_name_of(st, 'c', c)}, best {_lap(sync.lap_secs(l))})"))
            if m.get("name") or mk:
                people.setdefault(m.get("name") or mk, set()).update(cars)
        teams.append({"slug": tdir.name, "name": st.get("name") or tdir.name, "hidden": tdir.name in hidden or st.get("name") in hidden,
                      "members": len(st["members"]), "last_synced": last or None,
                      "last_synced_text": _when(last) if last else "not synced yet"})
    for sl in hidden:   # a hidden team that was never synced still shows, so it can be shown again
        if not any(t["slug"] == sl for t in teams):
            teams.append({"slug": sl, "name": sl, "hidden": True, "members": 0, "last_synced": None,
                          "last_synced_text": "not synced yet"})
    here.sort()
    if not people:
        why = "Team data hasn't been synced yet: press Refresh team data on the Team page."
    elif track_name and here:
        why = "Teammates have laps here: " + "; ".join(x for _t, x in here[:6]) + "."
    elif track_name:
        why = "None of your teammates has synced laps at this track yet (or Garage 61 hasn't shared any). Press Refresh team data to look again."
    else:
        why = ""
    return {"ok": True, "teams": teams, "here": why,
            "people": [{"name": n, "cars": sorted(c)} for n, c in sorted(people.items())]}


def _lap(t):
    if not t or t >= 1e9:
        return "no time"
    m, s_ = divmod(float(t), 60)
    return f"{int(m)}:{s_:06.3f}"


# ---------------------------------------------------------------- reading the library (no requests)
def _when(iso) -> str:
    d = sync._parse(iso)
    return d.astimezone().strftime("%d %b %Y").lstrip("0") if d else "not synced yet"


def _teams(root: Path) -> list:
    return sorted(p for p in root.iterdir() if p.is_dir()) if root and root.is_dir() else []


def _name_of(st, kind, i, fallback=None):
    return (st.get("names") or {}).get(kind + str(i)) or fallback or f"{'Track' if kind == 't' else 'Car'} {i}"


def member_sessions(root, name) -> dict:
    """What the library holds for one member (found by name or slug, across the teams): sessions, consistency,
    tracks and cars, last synced. {"found": False, "why"} when nothing is stored."""
    from .team import norm_name
    want = norm_name(name)
    laps, last, mname, st0 = [], "", "", {}
    for tdir in _teams(root):
        st = _state(tdir)
        for mk, m in st["members"].items():
            if want in (norm_name(m.get("name")), norm_name(m.get("slug")), norm_name(mk)):
                md = tdir / mk
                laps += [(st, l) for l in _all_laps(md)]
                last = max(last, str(_member(md).get("last_synced") or ""))
                mname = m.get("name") or mname
                st0 = st
    if not laps:
        return {"found": False, "why": ("Not synced yet: press Refresh team data on the Team page to sync "
                                        f"{str(name).split()[0] if str(name).split() else 'their'}'s practice sessions and laps.")}
    groups: dict = {}
    for st, l in laps:
        t, c, ty = sync.lap_track(l), sync.lap_car(l), sync.lap_session_type(l)
        ev = g(l, "event")
        start = sync.lap_start(l)
        k = f"{ev}|{ty}" if ev else f"{t}|{c}|{start[:10]}|{ty}"
        gp = groups.setdefault(k, {"date": start[:10], "track": _name_of(st, "t", t), "car": _name_of(st, "c", c),
                                   "type": sync.session_name(ty), "laps": 0, "times": [], "unclean": 0, "all": []})
        gp["laps"] += 1
        gp["date"] = max(gp["date"], start[:10]) if gp["date"] else start[:10]
        s = sync.lap_secs(l)
        if s is not None:
            gp["all"].append(s)
            if sync.lap_clean(l):
                gp["times"].append(s)
            else:
                gp["unclean"] += 1
    sessions = []
    for gp in groups.values():
        ts = gp["times"] or gp["all"]
        sessions.append({"date": gp["date"], "track": gp["track"], "car": gp["car"], "type": gp["type"],
                         "laps": gp["laps"], "unclean": gp["unclean"],
                         "best": min(ts) if ts else None,
                         "typical": statistics.median(ts) if ts else None,
                         "only_unclean": bool(gp["all"]) and not gp["times"]})
    sessions.sort(key=lambda s: s["date"], reverse=True)
    clean = [s for _st, l in laps for s in [sync.lap_secs(l)] if s is not None and sync.lap_clean(l)]
    cons = consistency_words(clean)
    return {"found": True, "name": mname or name, "sessions": sessions, "consistency": cons,
            "tracks": sorted({s["track"] for s in sessions}), "cars": sorted({s["car"] for s in sessions}),
            "laps": len(laps), "last_synced": last or None, "last_synced_text": _when(last)}


def consistency_words(clean: list) -> str:
    if len(clean) < 8:
        return "Not enough clean laps synced yet to say how steady they are (needs at least 8)."
    q = statistics.quantiles(clean, n=4)
    mid = statistics.median(clean)
    spread = q[2] - q[0]
    pct = 100 * spread / mid if mid else 0
    feel = "very steady" if pct < 0.8 else "steady" if pct < 1.6 else "uneven" if pct < 3 else "all over the place"
    return (f"{feel.capitalize()}: the middle half of their {len(clean)} clean laps are within {spread:.2f} s of each other "
            f"(about {pct:.1f}% of a typical lap).")


def member_lines(root) -> list:
    """['<member>: N tracks, M laps, last synced <date>'] for every member in the library, newest first."""
    rows = []
    for tdir in _teams(root):
        st = _state(tdir)
        for mk, m in st["members"].items():
            md = tdir / mk
            files = list((md / "laps").glob("*.json")) if (md / "laps").is_dir() else []
            n = len(_all_laps(md))
            ls = str(_member(md).get("last_synced") or "")
            rows.append((ls, f"{m.get('name') or mk}: {len(files)} track{'s' if len(files) != 1 else ''}, {n} laps, "
                             f"last synced {_when(ls) if ls else 'not yet'}"))
    rows.sort(reverse=True)
    return [r for _l, r in rows]


def laps_for(root, car_name, track_name, same) -> list:
    """The synced teammates' best laps at this car and track for the reference picker. `same(car, track)` says
    whether a stored car and track name is the one asked for. [{driver, lap_id, lap, lap_text, usable, note, team, member}]"""
    from .team import norm_name
    out = []
    for tdir in _teams(root):
        st = _state(tdir)
        for mk, m in st["members"].items():
            for (t, c), l in best_laps(tdir / mk).items():
                tn, cn = _name_of(st, "t", t), _name_of(st, "c", c)
                if not same(cn, tn):
                    continue
                lid = str(g(l, "id"))
                rec = st["tel"].get(lid) or {}
                ok = rec.get("s") == "done"
                if ok:
                    note = "Telemetry synced: use it as your reference."
                elif rec.get("s") == "none":
                    note = rec.get("why") or "Garage 61 hasn't shared telemetry for this lap."
                else:
                    note = "Telemetry not synced yet: press Refresh team data to fetch it."
                if not sync.lap_clean(l):
                    note += " (Not a clean lap.)"
                out.append({"driver": m.get("name") or mk, "lap_id": lid, "lap": sync.lap_secs(l), "usable": ok,
                            "note": note, "team": tdir.name, "member": mk, "track_id": t,
                            "date": sync.lap_start(l)[:10], "norm": norm_name(m.get("name"))})
    out.sort(key=lambda x: x["lap"] or 1e9)
    return out


def track_length(root, team, track_id):
    st = _state(Path(root) / _safe(team))
    v = st["track_len"].get(str(track_id))
    return float(v) if isinstance(v, (int, float)) and v > 0 else None


# ---------------------------------------------------------------- 2.25: automatic background sync
AUTO_FILE = "autosync.json"   # <library>/team/autosync.json: when the last automatic sync ran and what it found
AUTO_GAP_S = 30 * 60          # at most one automatic team sync per 30 minutes


def _auto_path(library_folder) -> Path:
    return Path(library_folder) / "team" / AUTO_FILE


def auto_due(library_folder, now: datetime | None = None, gap_s: float = AUTO_GAP_S) -> bool:
    """True when no automatic team sync has run in the last 30 minutes (attempts count, so a failure isn't retried at once)."""
    now = now or datetime.now(timezone.utc)
    d = sync._read_json(_auto_path(library_folder), {})
    last = sync._parse(d.get("at")) if isinstance(d, dict) else None
    return last is None or (now - last).total_seconds() >= gap_s


def auto_line(library_folder) -> str | None:
    """"Synced 11:42 pm · 3 new laps" for the Team page, or None when no automatic sync has finished."""
    d = sync._read_json(_auto_path(library_folder), {})
    last = sync._parse(d.get("done_at")) if isinstance(d, dict) else None
    if last is None:
        return None
    n = int(d.get("new_laps") or 0)
    when = last.astimezone().strftime("%I:%M %p").lstrip("0").lower()
    return f"Synced {when} \u00b7 {n} new lap{'s' if n != 1 else ''}"


def auto_run(api, library_folder, log=print, progress=None, should_stop=None, telemetry: str = "best",
             now: datetime | None = None) -> dict | None:
    """The incremental team sync, run by Debrief itself (no button): when it opens after your own sync and after a new
    race of yours. None when one already ran in the last 30 minutes. Same pacing and 429 rules as Refresh."""
    now = now or datetime.now(timezone.utc)
    if not auto_due(library_folder, now):
        return None
    p = _auto_path(library_folder)
    rec = {"at": sync._iso(now)}
    sync._write_json(p, rec)    # the attempt counts even if it fails
    out = run(api, log=log, progress=progress, should_stop=should_stop, telemetry=telemetry, now=now,
              library_folder=library_folder)
    if out.get("teams"):
        rec.update({"done_at": sync._iso(datetime.now(timezone.utc)), "new_laps": out.get("new_laps", 0),
                    "message": out.get("message", "")})
        sync._write_json(p, rec)
    return out
