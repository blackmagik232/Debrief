"""'Prep for this track': everything you know about a track + car before you go racing there.

Built from your own library (offline) plus ONE Garage 61 lap search for your teammates' best laps.

Garage 61 requests (only when the user clicks "Prep"):
  * teammates' best laps: exactly 1 request  (GET /laps?teams=..&tracks=..&cars=..&group=driver)
  * your account (to leave you out of the teammate list): 0, or 1 when the 24 h cache has expired
  * track/car names: 0 when you've reviewed a race at the combo; else 0-2 (tracks/cars lists, cached 7 days)
  Worst case: 4 requests. Typical: 1. No telemetry is downloaded.

Coaching language is correlational on purpose: "on your quicker laps you tended to...", never "do X
and you will gain Y".
"""
from __future__ import annotations

import json
import statistics as st
from datetime import datetime, timezone
from pathlib import Path

from . import brand, library, report, goals, schedule, series
from .engine import g, fmt_t

MAX_TEAMMATES = 12


# ---------------------------------------------------------------- picker
def combos(folder, include_practice: bool = False) -> list:
    """Track+car combos in your library, newest first:
    [{"track", "car", "track_short", "track_id", "car_id", "races", "last_date", "best"}]
    track_id/car_id are None for reviews made before Debrief v2 unless another review of the same
    combo has them (see resolve_ids). 2.7: "best" and "races" count race sessions only unless
    include_practice (library docstring); a combo you've only practised at is still listed, with best None."""
    groups = {}
    for r in library.list_reviews(folder):  # newest first
        race = include_practice or library.is_race(r)
        if not race and r["combo"] in groups:
            continue
        if not race:
            groups[r["combo"]] = {"track": r["track"], "car": r["car"], "track_short": r["track_short"],
                                  "track_id": r.get("track_id"), "car_id": r.get("car_id"), "races": 0,
                                  "last_date": r["date"], "best": None, "practice_only": True}
            continue
        c = groups.get(r["combo"])
        if c is None:
            groups[r["combo"]] = {"track": r["track"], "car": r["car"], "track_short": r["track_short"],
                                  "track_id": r.get("track_id"), "car_id": r.get("car_id"), "races": 1,
                                  "last_date": r["date"], "best": r["best"] if r.get("best_clean", True) else None}
        elif c.get("practice_only"):
            c.update({"races": 1, "last_date": r["date"], "best": r["best"] if r.get("best_clean", True) else None,
                      "practice_only": False})
        else:
            c["races"] += 1
            if r.get("best_clean", True):
                c["best"] = r["best"] if c["best"] is None else min(c["best"], r["best"])
            if c["track_id"] is None:
                c["track_id"], c["car_id"] = r.get("track_id"), r.get("car_id")
    return list(groups.values())


def _label(it: dict) -> str:
    return str(g(it, "name", default="?")) + ((" (" + g(it, "variant") + ")") if g(it, "variant") else "")


def resolve_ids(api, track: str, car: str):
    """(track_id, car_id) for full names like 'Charlotte Motor Speedway (Roval 2019)', using the cached
    Garage 61 track/car lists (0-2 requests, cached 7 days). (None, None) when not found."""
    tid = next((g(t, "id") for t in api.tracks() if library._norm(_label(t)) == library._norm(track)), None)
    cid = next((g(c, "id") for c in api.cars() if library._norm(_label(c)) == library._norm(car)), None)
    return tid, cid


# ---------------------------------------------------------------- helpers
def _read_review(folder, dirname):
    try:
        d = json.loads((Path(folder) / "reviews" / dirname / "review.json").read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def _num(x):
    return library._num(x)


def match_corners(reviews: list, L: float | None) -> list:
    """Match corners between reviews by apex position (names can shift between races).
    reviews: [(index, [corner dicts with id/apex_m/avail/rank])], processed newest first so names
    follow the newest review. Returns canon corners: {"id", "apex", "per": {index: corner}}."""
    tol = max(60.0, 0.02 * (L or 4000.0))
    canon = []
    for ri, corners in reviews:
        for cc in corners:
            ap = _num(cc.get("apex_m"))
            best, bd = None, 1e9
            for k in canon:
                if ri in k["per"]:
                    continue
                if ap is not None and k["apex"] is not None:
                    dd = abs(k["apex"] - ap)
                    if dd <= tol and dd < bd:
                        best, bd = k, dd
                elif ap is None and k["id"] == cc.get("id"):
                    best = k
            if best is None:
                best = {"id": cc.get("id"), "apex": ap, "per": {}}
                canon.append(best)
            best["per"][ri] = cc
    return canon


def weak_corners(rows: list) -> list:
    """Recurring weak corners across your reviews at one combo (rows = library rows, any order).
    avg_avail = average 'time on the table' (your median through the corner minus your quicker laps,
    25th percentile) in seconds per lap. top3 = races where it ranked in your top 3."""
    rows = sorted(rows, key=library._ts, reverse=True)  # newest first
    L = next((r.get("L") for r in rows if r.get("L")), None)
    canon = match_corners([(i, r.get("corners") or []) for i, r in enumerate(rows)], L)
    out = []
    for k in canon:
        per = k["per"]
        av = [_num(c.get("avail")) for c in per.values() if _num(c.get("avail")) is not None]
        top3 = sum(1 for c in per.values() if isinstance(c.get("rank"), int) and c["rank"] <= 3)
        # a corner your latest race doesn't have keeps an older race's name, which may now belong to a
        # different corner (names follow apex order): label it so it can't be mistaken for that one
        label = k["id"] if 0 in per else (f"older {k['id']}" + (f" (~{k['apex']:,.0f} m)" if k["apex"] is not None else ""))
        out.append({"id": k["id"], "label": label, "apex_m": round(k["apex"]) if k["apex"] is not None else None,
                    "avg_avail": round(sum(av) / len(av), 3) if av else 0.0, "top3": top3, "seen": len(per),
                    "races": len(rows), "in_latest": 0 in per,
                    "latest_rank": per[0].get("rank") if 0 in per else None,
                    "history": [{"date": rows[i]["date"], "dir": rows[i].get("dir"), "rank": per[i].get("rank") if i in per else None,
                                 "avail": _num(per[i].get("avail")) if i in per else None}
                                for i in reversed(range(len(rows)))]})
    out.sort(key=lambda w: (-w["top3"], -w["avg_avail"]))
    return out


def _trend(xs: list) -> float | None:
    """Least-squares slope of race best vs race number, s per race (negative = getting quicker)."""
    n = len(xs)
    if n < 2:
        return None
    mx, my = (n - 1) / 2, sum(xs) / n
    den = sum((i - mx) ** 2 for i in range(n))
    return round(sum((i - mx) * (x - my) for i, x in enumerate(xs)) / den, 3) if den else None


def _history(rows: list) -> dict | None:
    if not rows:
        return None
    rows = sorted(rows, key=library._ts)
    # only races whose best counts (not every lap flagged, not a partial lap) feed the typical best and
    # the trend; the others stay in races_list, marked best_clean False
    bests = [r["best"] for r in rows if r.get("best_clean", True)] or [r["best"] for r in rows]
    meds = [r["median"] for r in rows if _num(r.get("median")) is not None]
    pb = min([r for r in rows if r.get("best_clean", True)] or rows, key=lambda r: r["best"])  # clean bests first
    last = rows[-1]
    try:
        days = (datetime.now().date() - datetime.strptime(last["date"][:10], "%Y-%m-%d").date()).days
    except ValueError:
        days = None
    tr = _trend(bests)
    if tr is None:
        trend_text = "One race reviewed here so far."
    elif tr < -0.02:
        trend_text = f"Your race-best laps here have been getting quicker, about {abs(tr):.2f} s per race."
    elif tr > 0.02:
        trend_text = f"Your race-best laps here have drifted slower, about {tr:.2f} s per race."
    else:
        trend_text = "Your race-best laps here have been steady."
    return {"races": len(rows), "best": pb["best"], "best_date": pb["date"], "best_path": pb["path"],
            "median_of_bests": round(st.median(bests), 3), "latest_median": _num(last.get("median")),
            "median_lap": round(st.median(meds), 3) if meds else None,
            "trend_s_per_race": tr, "trend_text": trend_text, "last_date": last["date"], "days_since": days,
            "races_list": [{"date": r["date"], "best": r["best"], "best_clean": r.get("best_clean", True),
                            "median": r.get("median"), "std": r.get("std"),
                            "path": r["path"], "test_data": r.get("test_data"), "irating": r.get("irating"),
                            "result": r.get("result")} for r in rows]}


def _technique(latest: dict | None) -> list | None:
    """Corner technique from the newest review that has data.technique (Agent 1's v2 key).
    vs_ref = your typical minus the comparison lap (explicit sign; e.g. min_mph +2 = you carry 2 mph more)."""
    if not latest or not isinstance(latest.get("technique"), list):
        return None
    out = []
    for t in latest["technique"]:
        if not isinstance(t, dict) or not t.get("id"):
            continue
        out.append({"id": str(t["id"]), "apex_m": _num(t.get("apex_m")),
                    "typical": t.get("typical") if isinstance(t.get("typical"), dict) else None,
                    "best": t.get("best") if isinstance(t.get("best"), dict) else None,
                    "ref": t.get("ref") if isinstance(t.get("ref"), dict) else None,
                    "vs_ref": t.get("vs_ref") if isinstance(t.get("vs_ref"), dict) else None,
                    "notes": [str(n) for n in (t.get("notes") or []) if n][:3]})
    return out or None


def _membership(api, slug):
    """(membership, team {"slug", "name"}) from api.teams() (cached a day). UNKNOWN when it can't be read."""
    from . import team
    if api is None or not callable(getattr(api, "teams", None)):
        return brand.UNKNOWN, team.team_info(slug, None)
    mem, teams = team.membership_for(api, slug, log=lambda m: None)
    return mem, team.team_info(slug, teams)


def _teammates(api, track_id, car_id, team_slug, my_best, log, members=None):
    """ONE /laps request: best lap per driver on the team at this combo (group=driver). None when you're
    not on the team (then no request at all)."""
    team_slug = team_slug or brand.GROUP_SLUG
    if api is None:
        return {"laps": [], "team": team_slug, "error": "Offline: teammates' laps weren't loaded."}
    if track_id is None or car_id is None:
        return {"laps": [], "team": team_slug, "error": "Garage 61 doesn't know this track or car id yet."}
    from .g61 import Garage61Error
    try:
        teams = [team_slug]
        me = (api.me() or {})
        my_slug = g(me, "slug", default="")
        my_name = " ".join(filter(None, [g(me, "firstName", "firstname"), g(me, "lastName", "lastname")])) or my_slug
        laps = api.find_laps(teams=teams, tracks=[track_id], cars=[car_id], group="driver", age=-2, limit=100)   # age: as the Team page
    except Garage61Error as e:
        log(f"  Couldn't load teammates' laps: {e}")
        return {"laps": [], "team": team_slug, "error": str(e)}
    from . import team
    try:
        tl = api.teams()
        roster = team.team_roster(tl, team_slug)
        group = brand.team_name_for(tl, team_slug)
    except Exception:  # noqa: BLE001
        roster, group = {}, ""
    # 2.20.1: one extra request at most (all their laps) when a best lap was rejected or a member has no row
    found, reasons = team.gather_mates(
        laps, lambda: api.find_laps(teams=teams, tracks=[track_id], cars=[car_id], group="none", age=-2, limit=1000),
        my_slug, my_best, roster, members=members, my_name=my_name, group=group, log=log)
    out, others = [], []
    for m in found.values():
        l, t = m["lap"], m["t"]
        season = g(l, "season", default=None)
        row = {"name": m["name"], "slug": m["slug"], "t": round(t, 3),
               "date": library.local_day(g(l, "startTime", "start_time")),
               "irating": g(l, "driverRating", "driver_rating"), "clean": True,
               "season": g(season, "name") if isinstance(season, dict) else None,
               # gap = their lap - your best (negative = they were quicker)
               "gap": round(t - my_best, 3) if my_best else None}
        (out if m.get("member", True) else others).append(row)
    out.sort(key=lambda x: x["t"])
    notes = [team.reason_text(r) for r in reasons]
    if len(out) > MAX_TEAMMATES:   # said, with names, never silently
        notes.append(f"{len(out) - MAX_TEAMMATES} slower teammate{'s' if len(out) - MAX_TEAMMATES != 1 else ''} left out to "
                     "keep the list short: " + ", ".join(r["name"] for r in out[MAX_TEAMMATES:]) + ".")
    if others:   # not on the team's list: apart, never mixed in
        others.sort(key=lambda x: x["t"])
        notes.append(f"Also in {group or 'the group'} (not on your team): " + ", ".join(
            f"{r['name']} {team._lt(r['t'])}" for r in others[:MAX_TEAMMATES]) + ".")
    return {"laps": out[:MAX_TEAMMATES], "team": ",".join(teams), "error": None, "others": others[:MAX_TEAMMATES],
            "skipped": len([r for r in reasons if r["name"] not in {m["name"] for m in found.values()}]),
            "notes": notes}


# ---------------------------------------------------------------- practice plan
_METRIC = {  # metric -> (noun, unit)
    "min_mph": ("minimum speed", "mph"), "exit_mph": ("exit speed past the apex", "mph"),
    "brake_on": ("brake point", "m"), "full_thr": ("full-throttle point", "m"),
    "peak_brake": ("peak brake pressure", "%"), "abs_s": ("time on ABS", "s"), "coast_s": ("coasting", "s"),
}


def _strong(drv: dict) -> bool:
    """Reviews made before 2.2.2 kept any |r| >= 0.5 from 10 laps, which noise alone produces on most
    corners. Only patterns that clear today's bar (engine.pattern_r_needed) become practice targets."""
    from .engine import pattern_r_needed
    r, n = _num((drv or {}).get("r")), (drv or {}).get("n")
    if r is None or not isinstance(n, int):
        return False
    return abs(r) >= pattern_r_needed(n)


def _target(drv: dict, apex: float | None, exit_at: float | None = None) -> tuple[str, str] | None:
    """Turn one correlation from the engine into something to try in practice, worded as what your quicker
    laps did (2.7: never "aim for": the link is correlational, not proof of cause).
    The engine's r is corr(metric, corner time): r < 0 means a HIGHER metric went with QUICKER laps,
    so the number quoted is your 75th percentile; r > 0 means LOWER went with quicker, the 25th."""
    k, r = drv.get("metric"), _num(drv.get("r"))
    p25, p75 = _num(drv.get("p25")), _num(drv.get("p75"))
    if k not in _METRIC or r is None or p25 is None or p75 is None:
        return None
    up = r < 0
    v = p75 if up else p25
    q = f"(r {r:+.2f}, {drv.get('n', '?')} laps)"
    if k == "min_mph":
        return (f"your quicker laps were around ~{v:.0f} mph at the slowest point",
                f"on your quicker laps you tended to carry {'more' if up else 'less'} minimum speed {q}")
    if k == "exit_mph":
        where = f"{exit_at:.0f} m" if exit_at else "a short way"
        return (f"your quicker laps were around ~{v:.0f} mph {where} past the apex",
                f"on your quicker laps you tended to come out {'faster' if up else 'slower'} {q}")
    if k == "brake_on":
        before = (apex - v) if apex is not None else None
        txt = (f"your quicker laps braked ~{before:.0f} m before the apex" if before is not None and before > 0
               else f"your quicker laps braked at about the {v:.0f} m mark")
        return (txt, f"on your quicker laps you tended to brake {'later' if up else 'earlier'} {q}")
    if k == "full_thr":
        after = (v - apex) if apex is not None else None
        txt = (f"your quicker laps were flat out ~{after:.0f} m after the apex" if after is not None and after > 0
               else f"your quicker laps were flat out by about the {v:.0f} m mark")
        return (txt, f"on your quicker laps you tended to reach full throttle {'later' if up else 'sooner'} {q}")
    if k == "peak_brake":
        return (f"your quicker laps peaked around {v:.0f}% brake",
                f"on your quicker laps you tended to brake {'harder' if up else 'lighter'} {q}")
    if k == "abs_s":
        return (f"your quicker laps spent about {v:.2f} s on ABS",
                f"on your quicker laps you tended to spend {'more' if up else 'less'} time on ABS {q}")
    if k == "coast_s":
        return (f"your quicker laps coasted about {v:.2f} s",
                f"on your quicker laps you tended to coast {'more' if up else 'less'} {q}")
    return None


def _quick_laps(corner: dict | None, clean_laps: set) -> dict | None:
    """Median metrics over your clean laps that were at or under the 25th-percentile corner time.
    (The corner's 'best_lap' can be a partial lap, so it isn't used.)"""
    if not corner:
        return None
    p25 = _num(corner.get("p25_t"))
    per = corner.get("per_lap") or {}
    rows = [m for n, m in per.items() if isinstance(m, dict) and (not clean_laps or str(n) in clean_laps)
            and _num(m.get("t")) is not None and (p25 is None or m["t"] <= p25 + 1e-9)
            and _num(m.get("min_mph")) is not None and m["min_mph"] > 5]
    if len(rows) < 2:
        return None
    med = lambda k: st.median([m[k] for m in rows if _num(m.get(k)) is not None]) if any(
        _num(m.get(k)) is not None for m in rows) else None
    apex = _num(corner.get("apex_m"))
    bo = med("brake_on")
    gears = [m.get("gear") for m in rows if isinstance(m.get("gear"), int) and m.get("gear") > 0]
    return {"n": len(rows), "min_mph": med("min_mph"), "exit_mph": med("exit_mph"),
            "before": (apex - bo) if (apex is not None and bo is not None) else None,
            "gear": st.mode(gears) if gears else None}


def _corner_drill(w: dict, corner: dict | None, tech: dict | None, laps: int, clean_laps: set = frozenset()) -> dict:
    cid = w.get("label") or w["id"] or "this corner"
    apex = _num((corner or {}).get("apex_m")) or _num(w.get("apex_m"))
    where = f"apex ~{apex:,.0f} m" if apex is not None else ""
    targets, why = [], []
    for drv in (corner or {}).get("drivers") or []:
        if not _strong(drv):
            continue
        t = _target(drv, apex, _num((corner or {}).get("exit_at_m")))
        if t and len(targets) < 2:
            targets.append(t[0])
            why.append(t[1])
    summ = (corner or {}).get("summary") or {}
    vs = (tech or {}).get("vs_ref") or {}
    ref = (tech or {}).get("ref") or {}
    if _num(vs.get("min_mph")) is not None and _num(ref.get("min_mph")) is not None and vs["min_mph"] <= -1.5:
        typ = _num(((tech or {}).get("typical") or {}).get("min_mph"))
        why.append(f"the comparison lap carried ~{abs(vs['min_mph']):.0f} mph more minimum speed here "
                   f"({ref['min_mph']:.0f} mph" + (f" vs your typical {typ:.0f})" if typ is not None else ")"))
        if not any("slowest point" in x for x in targets):
            targets.append(f"work toward ~{ref['min_mph']:.0f} mph at the slowest point")
    if not targets:  # no strong pattern: copy what your own quicker laps through it looked like
        q = _quick_laps(corner, clean_laps)
        if q:
            bits = [f"~{q['min_mph']:.0f} mph minimum"]
            if q.get("before") is not None and q["before"] > 0:
                bits.insert(0, f"brake ~{q['before']:.0f} m before the apex")
            if q.get("gear"):
                bits.append(f"gear {q['gear']}")
            targets.append("copy your quicker laps here: " + ", ".join(bits))
            why.append(f"your quickest quarter of clean laps through it ({q['n']} laps) looked like that")
        elif _num(summ.get("min_mph")) is not None and summ["min_mph"] > 5:
            targets.append(f"hit the same ~{summ['min_mph']:.0f} mph minimum every lap")
        else:
            targets.append("use the same brake marker and line every lap")
    ev = (f"Ranked in your top 3 for time on the table in {w['top3']} of {w['races']} "
          f"race{'s' if w['races'] != 1 else ''} here, ~{w['avg_avail']:.2f} s a lap on average")
    if why:
        ev += "; in your latest race " + "; ".join(why[:3])
    return {"title": f"{cid}: one corner at a time", "laps": laps, "corner": w["id"], "where": where,
            "focus": f"{laps} laps focused only on {cid}" + (f" ({where})" if where else "") + ": "
                     + ", ".join(targets) + ". Ignore the rest of the lap.",
            "targets": targets, "evidence": ev + "."}


def practice_plan(hist, weak, latest, tech, goal, mates) -> list:
    """3-5 drills, each tied to evidence from your reviews (or teammates if you've never raced here)."""
    drills = []
    if hist:
        lc = {c.get("id"): c for c in (latest or {}).get("corners") or []}
        clean_laps = {str(l.get("n")) for l in (latest or {}).get("laps") or [] if isinstance(l, dict) and l.get("clean")}
        tmap = {t["id"]: t for t in tech or []}
        for i, w in enumerate([w for w in weak if w["avg_avail"] > 0.02][:3]):
            # the canonical id follows the newest review, so lc[w.id] / tmap[w.id] are the same corner
            # there; a corner the latest race doesn't have may share a name with a different one, so it
            # gets no latest-race evidence at all
            drills.append(_corner_drill(w, lc.get(w["id"]) if w["in_latest"] else None,
                                        tmap.get(w["id"]) if w["in_latest"] else None, 10 if i == 0 else 8, clean_laps))
        cons = (latest or {}).get("consistency") if isinstance((latest or {}).get("consistency"), dict) else None
        std = _num((latest or {}).get("std_clean"))
        best = hist["best"]
        if (cons and cons.get("label") in ("Up and down", "Scattered")) or (std is not None and std > 0.35):
            window = best + 0.5
            ev = (f"Your clean laps in the latest race spread σ {std:.2f} s" if std is not None else
                  "Your clean laps in the latest race were spread out")
            if cons and _num(cons.get("within_05")) is not None:
                ev += f"; {cons['within_05']:.0f}% were within 0.5 s of your best"
            drills.append({"title": "Consistency run", "laps": 10, "corner": None, "where": "",
                           "focus": f"10 laps in a row, every one under {fmt_t(window)} (your best + 0.5 s). "
                                    "Same markers every lap; no hero laps.",
                           "targets": [f"every lap under {fmt_t(window)}"], "evidence": ev + "."})
        fade = _num((latest or {}).get("trend_per10"))
        deg = _num(((latest or {}).get("stint") or {}).get("deg_s_per_lap")) if isinstance((latest or {}).get("stint"), dict) else None
        if (deg is not None and deg > 0.015) or (deg is None and fade is not None and fade > 0.15):
            per10 = deg * 10 if deg is not None else fade
            drills.append({"title": "Long run", "laps": 15, "corner": None, "where": "",
                           "focus": "15 laps on a race fuel load. Compare laps 1-5 with laps 11-15 and note which "
                                    "corners get slower first.",
                           "targets": ["last 5 laps within 0.3 s of the first 5"],
                           "evidence": f"In your latest race your clean laps tended to get ~{per10:.2f} s slower "
                                       "every 10 laps."})
        if goal and best is not None and best > goal["target_s"]:
            on_table = sum(w["avg_avail"] for w in weak[:3])
            drills.append({"title": "Goal attempt", "laps": 5, "corner": None, "where": "",
                           "focus": f"5-lap qualifying-style run on low fuel, aiming for {fmt_t(goal['target_s'])}.",
                           "targets": [f"{fmt_t(goal['target_s'])} or quicker"],
                           "evidence": f"Your goal is {fmt_t(goal['target_s'])}; your best here is {fmt_t(best)} "
                                       f"({best - goal['target_s']:.3f} s to find). Your top three weak corners "
                                       f"averaged ~{on_table:.2f} s a lap on the table."})
        if len(drills) < 3:
            latest_laps = [l for l in (latest or {}).get("laps") or [] if isinstance(l, dict)]
            cl = sum(1 for l in latest_laps if l.get("clean"))
            drills.append({"title": "Clean laps", "laps": 10, "corner": None, "where": "",
                           "focus": "10 laps with no off-tracks or incidents at 99% pace. Brake a touch early "
                                    "rather than late.",
                           "targets": ["10 of 10 clean"],
                           "evidence": (f"{cl} of {len(latest_laps)} analysed laps were clean in your latest race."
                                        if latest_laps else "Clean laps make every other drill count.")})
    else:
        quick = (mates or {}).get("laps") or []
        q = quick[0] if quick else None
        drills.append({"title": "Learn the track", "laps": 10, "corner": None, "where": "",
                       "focus": "10 steady laps at 90% pace. Pick one brake marker for every corner and say it "
                                "out loud as you pass it.",
                       "targets": ["one marker per corner"],
                       "evidence": "You haven't reviewed a race here yet, so there's no data on your own laps."})
        drills.append({"title": "Build to race pace", "laps": 10, "corner": None, "where": "",
                       "focus": "10 laps, a little quicker every 3 laps. Stop pushing when you go off; that's "
                                "the limit for now.",
                       "targets": ["3 clean laps in a row at your quickest pace"],
                       "evidence": "A clean baseline makes your first race review here much more useful."})
        drills.append({"title": "Study a teammate", "laps": 5, "corner": None, "where": "",
                       "focus": (f"Load {q['name']}'s {fmt_t(q['t'])} in Garage 61, watch where they brake and "
                                 "how fast they go through the slowest corners, then try it for 5 laps.")
                                if q else "Ask a teammate for a lap here, or follow a quick driver in this car "
                                          "on Garage 61, then copy their markers for 5 laps.",
                       "targets": [f"within 2 s of {fmt_t(q['t'])}"] if q else ["copy one marker per corner"],
                       "evidence": (f"{q['name']} is the quickest teammate here ({fmt_t(q['t'])})."
                                    if q else "No teammate laps here yet.")})
        if goal:
            drills.append({"title": "Goal attempt", "laps": 5, "corner": None, "where": "",
                           "focus": f"Once you're comfortable: 5 laps on low fuel aiming for {fmt_t(goal['target_s'])}.",
                           "targets": [f"{fmt_t(goal['target_s'])} or quicker"],
                           "evidence": f"Your goal for this combo is {fmt_t(goal['target_s'])}."})
    return drills[:5]


# ---------------------------------------------------------------- pre-race brief
BRIEF_CORNERS = 3
BRIEF_RIVALS = 5


def wet_dirs(folder, rows: list) -> set:
    """The reviews (by folder name) of races on a wet track, from each review's own conditions (above 1, which the
    review page also calls dry). An older review with no reading counts as not known to be wet. Library rows don't carry this, so it reads those reviews' files."""
    out = set()
    for r in rows:
        d = _read_review(folder, r["dir"]) if r.get("dir") else None
        wetness = _num(((d or {}).get("conditions") or {}).get("wetness")) if isinstance((d or {}).get("conditions"), dict) else None
        if wetness is not None and wetness > 1:
            out.add(r.get("dir"))
    return out


def _last_cost(w: dict, wet) -> dict:
    """The newest race where this corner was in your top three for time to find, and what it cost."""
    for x in reversed(w.get("history") or []):     # history runs oldest first
        if isinstance(x.get("rank"), int) and x["rank"] <= 3 and (x.get("avail") or 0) > 0.02:
            return {"last_date": x["date"], "last_avail": round(x["avail"], 3), "last_wet": x.get("dir") in wet}
    return {"last_date": None, "last_avail": None, "last_wet": False}


def brief(rows: list, track: str, car: str, hist: dict | None, weak: list, wet=frozenset()) -> dict | None:
    """What your saved reviews say about this track, for the card at the top of the page. Saved data only: this
    never touches Garage 61. rows = race rows. Anything with no data is left out; None when nothing is known.
    wet = wet_dirs: wet races are kept out of the typical lap and shown on their own."""
    from . import profile
    here = [r for r in rows if library._norm(r.get("track")) == library._norm(track)]
    pace = None
    if hist:
        pace = {"races": hist["races"], "best": hist["best"], "best_date": hist["best_date"],
                "typical": hist.get("median_lap"), "last_date": hist["last_date"], "other_car": None}
    elif here:      # never here in this car: your laps in another car are still worth knowing, and labelled
        clean = [r for r in here if r.get("best_clean", True)]
        if clean:
            b = min(clean, key=lambda r: r["best"])
            meds = [r["median"] for r in here if r.get("car") == b.get("car") and _num(r.get("median")) is not None]
            pace = {"races": sum(1 for r in here if r.get("car") == b.get("car")), "best": b["best"], "best_date": b["date"],
                    "typical": round(st.median(meds), 3) if meds else None, "other_car": b.get("car"),
                    "last_date": max(r["date"] for r in here if r.get("car") == b.get("car"))}
    if pace and wet:
        same = [r for r in here if library._norm(r.get("car")) == library._norm(pace["other_car"] or car)]
        wets = [r for r in same if r.get("dir") in wet]
        meds = [r["median"] for r in same if r.get("dir") not in wet and _num(r.get("median")) is not None]
        clean_dry = [r for r in same if r.get("dir") not in wet and r.get("best_clean", True)]
        pace.update({"typical": round(st.median(meds), 3) if meds else None, "typical_dry": True, "typical_races": len(meds),
                     "wet_races": len(wets), "wet_best": min((r["best"] for r in wets if r.get("best_clean", True)), default=None),
                     "best_wet": not clean_dry})
    repeat = [{"id": w.get("label") or w["id"], "top3": w["top3"], "races": w["races"], "avg_avail": w["avg_avail"], **_last_cost(w, wet)}
              for w in weak if w.get("in_latest") and w["top3"] >= 2 and w["avg_avail"] > 0.02][:BRIEF_CORNERS]
    scanned, _ = profile.scanned_rows(here)
    who = {}
    for r in scanned:
        for x in r["racecap"].get("rivals") or []:
            a = who.setdefault(x["name"].casefold(), {"name": x["name"], "races": 0, "seconds": 0.0, "fights": 0, "made": 0, "lost": 0})
            a["races"] += 1
            for k in ("seconds", "fights", "made", "lost"):
                a[k] += x.get(k) or 0
    # the same few cars in one race aren't rivals: like the racecraft profile, this waits for 3 scanned races
    rivals = [a for a in who.values() if a["seconds"] >= 15 or a["made"] + a["lost"] >= 2] if len(scanned) >= 3 else []
    rivals.sort(key=lambda a: (-a["races"], -a["seconds"], -(a["made"] + a["lost"]), a["name"]))
    for a in rivals:
        a["seconds"] = round(a["seconds"], 1)
    if not (pace or repeat or rivals):
        return None
    return {"pace": pace, "corners": repeat, "rivals": rivals[:BRIEF_RIVALS], "scans": len(scanned),
            "note": profile.RACECRAFT_NOTE if rivals else None}


def race_brief(folder, rows: list, track: str, car: str, hist: dict | None, weak: list, today=None) -> dict:
    """brief() plus the race itself: conditions from your series' schedule, and racecraft facts behind the racecraft
    profile's own bar (RACECRAFT_MIN_RACES scanned races here). Always a dict, and it says plainly what isn't known."""
    from . import profile
    today = today or datetime.now(timezone.utc).date()
    here = [r for r in rows if library._norm(r.get("track")) == library._norm(track)]
    wet = wet_dirs(folder, here)
    b = brief(rows, track, car, hist, weak, wet)
    out = dict(b) if b else {"pace": None, "corners": [], "rivals": [], "scans": 0, "note": None}
    out["no_history"] = None if here else "You have no saved race reviews at this track yet, so there's nothing of yours to show here."
    rc = profile.racecraft(here)
    out["racecraft"] = None
    out["racecraft_wait"] = None
    if rc.get("enough"):
        out["racecraft"] = {"races": rc["n"], "lap1": rc.get("lap1"), "passes": rc.get("passes")}
        out["note"] = profile.RACECRAFT_NOTE
    elif here:
        wait = f"Racecraft facts here need {rc['min']} scanned races. You have {rc['n']}."
        if rc.get("waiting"):
            wait += f" {rc['waiting']} older scan{' needs' if rc['waiting'] == 1 else 's need'} a refresh before it counts."
        out["racecraft_wait"] = wait
    sched = schedule.load_current(today)
    mine = series.load(folder)
    out["upcoming"] = series.upcoming_at(sched, mine, track, car, today)
    out["upcoming_note"] = None
    if not out["upcoming"]:
        out["upcoming_note"] = ("No season schedule is loaded, so the race conditions aren't known." if not sched else
                                "None of your series race this track in this car for the rest of the season.")
    return out


# ---------------------------------------------------------------- build / write
def build(api, folder, track_id: int, car_id: int, team_slug: str | None, log=print,
          track: str | None = None, car: str | None = None, include_practice: bool = False, members=None) -> dict:
    """Prep data for one track + car. api may be None (offline: no teammates).
    track/car (full names) are optional fallbacks for old reviews without Garage 61 ids.
    Request worst case: 4 (see module docstring); data["requests"] says how many were used.
    2.7: history, the trend and the recurring weak corners come from RACE reviews only (library docstring)
    unless include_practice; practice / qualifying reviews at the combo are counted in "practice_sessions"."""
    folder = Path(folder)
    start_req = getattr(api, "requests_made", 0) if api is not None else 0
    rows_all = library.list_reviews(folder)
    rows = library.race_rows(rows_all, include_practice)
    n_prac = 0
    probe = {"track": track or "", "car": car or "", "track_id": track_id, "car_id": car_id}
    mine = [r for r in rows if library.same_combo(probe, r)]
    if not mine and track and car:  # ids given but the reviews have none: fall back to names
        mine = [r for r in rows if library._norm(r["track"]) == library._norm(track)
                and library._norm(r["car"]) == library._norm(car)]
    if mine:
        track, car = mine[0]["track"], mine[0]["car"]
    elif (not track or not car) and api is not None:
        from .g61 import Garage61Error
        try:
            track = track or next((_label(t) for t in api.tracks() if g(t, "id") == track_id), None)
            car = car or next((_label(c) for c in api.cars() if g(c, "id") == car_id), None)
        except Garage61Error as e:
            log(f"  Couldn't load track/car names: {e}")
        if track and car:  # old reviews have names but no ids
            mine = [r for r in rows if library._norm(r["track"]) == library._norm(track)
                    and library._norm(r["car"]) == library._norm(car)]
            if mine:
                track, car = mine[0]["track"], mine[0]["car"]
    if mine:
        n_prac = sum(1 for r in rows_all if not library.is_race(r) and library.same_combo(mine[0], r))
    elif track and car:
        n_prac = sum(1 for r in rows_all if not library.is_race(r) and library._norm(r["track"]) == library._norm(track)
                     and library._norm(r["car"]) == library._norm(car))
    track = track or f"Track {track_id}"
    car = car or f"Car {car_id}"
    log(f"Preparing {library.short_track_name(track)} in the {car}...")
    hist = _history(mine)
    weak = weak_corners(mine) if mine else []
    newest = sorted(mine, key=library._ts, reverse=True)
    latest = _read_review(folder, newest[0]["dir"]) if newest else None
    tech_src = next((d for d in (_read_review(folder, r["dir"]) for r in newest[:3])
                     if d and isinstance(d.get("technique"), list)), None)
    tech = _technique(tech_src)
    goal = goals.for_combo(folder, track, car, track_id, car_id)
    if track_id is None or car_id is None:
        ids = next(((r["track_id"], r["car_id"]) for r in mine if r.get("track_id") is not None), (None, None))
        track_id, car_id = track_id if track_id is not None else ids[0], car_id if car_id is not None else ids[1]
    team_slug = team_slug or brand.GROUP_SLUG
    mem, team_d = _membership(api, team_slug)
    if mem == brand.NOT_MEMBER:
        log(f"You're not on the {team_d['name']} team on Garage 61, so teammates' laps weren't loaded.")
        mates = {"laps": [], "team": team_slug, "error": None, "skipped": 0}
    else:
        log("Loading your teammates' best laps here (1 request)...")
        mates = _teammates(api, track_id, car_id, team_slug, hist["best"] if hist else None, log, members)
    # drills quote technique as "in your latest race", so only use it when it IS from the latest race
    tech_latest = tech if (tech_src is not None and latest is not None and tech_src.get("meta") == latest.get("meta")) else None
    plan = practice_plan(hist, weak, latest, tech_latest, goal, mates)
    used = (getattr(api, "requests_made", 0) - start_req) if api is not None else 0
    log(f"Garage 61 requests for this prep: {used}")
    return {"version": 1, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "track": track, "car": car, "track_short": library.short_track_name(track),
            "track_id": track_id, "car_id": car_id, "raced": bool(mine), "history": hist,
            "brief": race_brief(folder, rows, track, car, hist, weak), "weak": weak[:6], "technique": tech,
            "technique_date": library.local_day((tech_src or {}).get("meta", {}).get("started_at")) if tech_src else None,
            "latest": ({"date": newest[0]["date"], "path": newest[0]["path"], "L": newest[0].get("L")}
                       if newest else None),
            "teammates": mates, "goal": goal, "plan": plan, "requests": used,
            "membership": mem, "team": team_d,
            "sessions": "all" if include_practice else "race", "practice_sessions": n_prac,
            "test_data": any(r.get("test_data") for r in mine)}


def path_for(folder, data: dict) -> Path:
    return Path(folder) / "prep" / f"{library.slugify(data.get('track'))}_{library.slugify(data.get('car'))}.html"


def write(folder, data: dict) -> Path:
    """<folder>/prep/<track>_<car>.html, self-contained (data embedded), links back to the reviews."""
    out = path_for(folder, data)
    out.parent.mkdir(parents=True, exist_ok=True)
    html = report.fill((report.TPL / "prep.html").read_text(encoding="utf-8"), {"/*PREP*/null": report._embed(data)})
    library._atomic_write(out, html)
    return out
