"""Skill trends across races (Debrief 2.3): the library's Skills tab.

Input is library rows (``library.list_reviews``) that carry the compact ``skills`` shape
({"v", "n_clean", "lap_t", "loss_pm": {...}}). Scores are **recomputed from loss_pm** with the current
``skills.CAPS_PM``, so retuning the caps re-scores old races without touching any review.

Each race compares your typical laps with your own better laps in that race; lower time lost means your
typical laps were closer to your best driving. Trends are a robust (Theil-Sen) slope and are only given
a direction with enough races over enough time AND when a Mann-Kendall test (two-sided, ties handled,
normal approximation with continuity correction) gives p < 0.05. Race-to-race spread of loss_pm is about
1.5 per-mille, so without the test most "Improving"/"Slipping" words on flat data were noise (2.3.0
verification H1). With 4 races the test can't reach p < 0.05, so a direction needs 5 or more races.

Garage 61 request budget (spec 1.6): technique_plus, skills, session_review, profile, heat map: **0**.
Library refresh for layout 2.3.0: **0**. This module never makes a request.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timezone

import numpy as np

from . import skills

VERSION = 1
PHASES = ("braking", "entry", "mid", "exit", "overall")
MIN_CLEAN = 6
TREND_MIN_RACES = 4
TREND_MIN_DAYS = 14
TREND_MIN_CHANGE_PM = 0.5
TREND_MAX_P = 0.05
TREND_TEST = "mann_kendall"
WEAKEST_LAST = 5


def _num(x):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _r(x, nd=2):
    x = _num(x)
    return None if x is None else round(x, nd)


def _ts(row) -> float:
    from . import library
    return library._ts(row)


def _day(ts: float) -> date:
    return datetime.fromtimestamp(ts, tz=timezone.utc).date()


def theil_sen(xs, ys):
    """Median of pairwise slopes (robust to one odd race). None with fewer than 2 distinct xs."""
    sl = [(ys[j] - ys[i]) / (xs[j] - xs[i]) for i in range(len(xs)) for j in range(i + 1, len(xs))
          if xs[j] != xs[i]]
    return float(np.median(sl)) if sl else None


def mann_kendall(ys):
    """Two-sided Mann-Kendall trend test on ys in time order. Returns (S, p). Ties (equal values) are
    handled with the usual variance correction; p uses the normal approximation with continuity
    correction. p is 1.0 when every value is equal or there are fewer than 3 values."""
    ys = [float(y) for y in ys]
    n = len(ys)
    if n < 3:
        return 0, 1.0
    S = 0
    for i in range(n - 1):
        for j in range(i + 1, n):
            d = ys[j] - ys[i]
            S += (d > 0) - (d < 0)
    counts = {}
    for y in ys:
        counts[y] = counts.get(y, 0) + 1
    var = (n * (n - 1) * (2 * n + 5) - sum(t * (t - 1) * (2 * t + 5) for t in counts.values() if t > 1)) / 18.0
    if var <= 0 or S == 0:
        return S, 1.0
    z = (S - (1 if S > 0 else -1)) / math.sqrt(var)
    return S, float(math.erfc(abs(z) / math.sqrt(2.0)))


def _usable(row):
    s = row.get("skills") if isinstance(row, dict) else None
    if not isinstance(s, dict) or not isinstance(s.get("loss_pm"), dict):
        return None
    n = _num(s.get("n_clean"))
    if n is None or n < MIN_CLEAN:
        return None
    return s


def _class_key(car):
    from . import schedule
    try:
        c = schedule.class_of(car)
    except Exception:
        c = None
    return str(c or car or "Unknown car")


def _trend(races, min_change=TREND_MIN_CHANGE_PM):
    n = len(races)
    if n < TREND_MIN_RACES:
        return None
    t0 = races[0]["_t"]
    xs = [(r["_t"] - t0) / 86400.0 for r in races]
    ys = [r["loss_pm"] for r in races]
    days = xs[-1] - xs[0]
    s = theil_sen(xs, ys)
    if s is None:
        return None
    per_week = s * 7.0
    change = s * days
    mk_s, p = mann_kendall(ys)
    # spec 1.3 + 2.3.0 H1: a direction only with >= 4 races over >= 14 days, a change of >= 0.5 per-mille
    # AND Mann-Kendall p < 0.05 with the same sign; otherwise "steady" (span_ok tells the page whether
    # the window was long enough to judge: "Not enough races yet")
    if (days < TREND_MIN_DAYS or abs(change) < min_change or p >= TREND_MAX_P
            or (mk_s < 0) != (change < 0)):
        direction = "steady"
    else:
        direction = "improving" if change < 0 else "slipping"
    return {"slope_pm_per_week": _r(per_week, 3), "change_pm": _r(change, 2), "direction": direction,
            "n": n, "days": int(round(days)), "span_ok": days >= TREND_MIN_DAYS,
            "test": TREND_TEST, "p": round(p, 4), "mk_s": int(mk_s)}


def _phase_block(items, phase):
    races = []
    for row, s, t in items:
        v = _num(s["loss_pm"].get(phase))
        if v is None:
            continue
        races.append({"dir": row.get("dir"), "date": row.get("date"), "track_short": row.get("track_short"),
                      "car": row.get("car"), "n_clean": int(_num(s.get("n_clean"))), "loss_pm": round(v, 2),
                      "score": skills.score(v, phase), "_t": t})
    if not races:
        return None
    weekly, seasons = {}, {}
    for r in races:
        y, w, _ = _day(r["_t"]).isocalendar()
        weekly.setdefault(f"{y}-W{w:02d}", []).append(r["loss_pm"])
    for (row, s, t) in items:
        v = _num(s["loss_pm"].get(phase))
        if v is None:
            continue
        nm = (row.get("season") or {}).get("name") if isinstance(row.get("season"), dict) else None
        seasons.setdefault(str(nm or "Unknown season"), []).append(v)
    scored = [r["score"] for r in races if r["score"] is not None]
    out = {"latest": races[-1]["score"], "latest_loss_pm": races[-1]["loss_pm"],
           "best_score": max(scored) if scored else None,
           "trend": _trend(races),
           "weekly": [{"week": k, "loss_pm": _r(float(np.median(v))), "n": len(v)} for k, v in sorted(weekly.items())],
           "seasons": [{"name": k, "loss_pm": _r(float(np.median(v))), "n": len(v)} for k, v in seasons.items()],
           "races": [{k: v for k, v in r.items() if k != "_t"} for r in races]}
    return out


def _group(key, label, items):
    items = sorted(items, key=lambda x: (x[2], str(x[0].get("dir"))))
    phases = {}
    for p in PHASES:
        b = _phase_block(items, p)
        if b:
            phases[p] = b
    return {"key": key, "label": label, "races": len(items), "phases": phases}


# ---------------------------------------------------------------- racecraft across scanned races (2.8)
# A scan's numbers only exist on reviews with data["racecap"] version 2. Passes are only those of cars near the
# replay camera and positions are on the road unless official, so the page says so. No requests, no scan files read.
RACECRAFT_MIN_RACES = 3
BEST_CORNER_MIN_MADE = 2
RACECRAFT_NOTE = ("Passes only count cars near the replay camera, so the real number can be higher. "
                  "Positions are on the road, not official results.")


def _pos_int(v):
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else None


def _recovery(rc):
    """One entry per incident. status: no_loss, back (got the places back), not_back, ended (race over), unknown.
    race_analysis has no "before" position for a lap 1 incident, so the grid slot is used for that here."""
    grid = _pos_int(rc.get("grid"))
    pos = {}
    for x in rc.get("positions") or []:
        if isinstance(x, dict) and _pos_int(x.get("pos")) and isinstance(x.get("lap"), int):
            pos[x["lap"]] = x["pos"]
    costs = rc.get("incident_cost") if isinstance(rc.get("incident_cost"), list) else []
    out = []
    for k, i in enumerate(x for x in rc.get("incidents") or [] if isinstance(x, dict)):
        lap = i.get("lap") if isinstance(i.get("lap"), int) else None
        places = i.get("places_lost") if isinstance(i.get("places_lost"), int) else None
        cost = costs[k] if k < len(costs) and isinstance(costs[k], dict) else {}
        before = grid if lap == 1 else next((pos[n] for n in range((lap or 1) - 1, 0, -1) if n in pos), None)
        later = sorted(n for n in pos if lap is not None and n > lap)
        back = next((n for n in later if before is not None and pos[n] <= before), None)
        if i.get("out"):
            status = "ended"
        elif places is None or lap is None:
            status = "unknown"
        elif places <= 0:
            status = "no_loss"
        elif back is not None:
            status = "back"
        elif before is None or not later:
            status = "unknown"       # nothing to compare with, or no laps left to win them back
        else:
            status = "not_back"
        out.append({"lap": lap, "places": places, "seconds": _num(cost.get("seconds")), "status": status,
                    "laps_back": back - lap if status == "back" else None})
    return out


def _rivals_of(rc):
    who = {}

    def row(name):
        return who.setdefault(str(name), {"name": str(name), "seconds": 0.0, "fights": 0, "made": 0, "lost": 0})
    for f in rc.get("fights") or []:
        if isinstance(f, dict) and f.get("other"):
            r = row(f["other"])
            r["seconds"] += _num(f.get("seconds")) or 0.0
            r["fights"] += 1
    for p in rc.get("passes") or []:
        if isinstance(p, dict) and p.get("other") and p.get("how") == "on track" and (p.get("lap") or 0) > 1:
            row(p["other"])["made" if p.get("made") else "lost"] += 1
    rows = sorted(who.values(), key=lambda r: (-r["seconds"], -(r["made"] + r["lost"]), r["name"]))[:12]
    for r in rows:
        r["seconds"] = round(r["seconds"], 1)
    return rows


def racecap_row(d: dict) -> dict | None:
    """The compact scan numbers kept on a library row. {"v": 1} marks a scan the library hasn't reworked yet."""
    rc = d.get("racecap") if isinstance(d, dict) else None
    if not isinstance(rc, dict):
        return None
    v = rc.get("version") if isinstance(rc.get("version"), int) else 1
    craft = rc.get("craft")
    if v < 2 or not isinstance(craft, dict):
        return {"v": 1}
    grid = _pos_int(rc.get("grid"))
    first = next((x.get("pos") for x in rc.get("positions") or [] if isinstance(x, dict) and x.get("lap") == 1), None)
    first = _pos_int(first) or _pos_int(rc.get("pos_after_lap1"))
    apex = {str(c.get("id")): _r(c.get("apex_m"), 1) for c in d.get("corners") or [] if isinstance(c, dict)}
    corners = [{"id": str(c.get("corner")), "apex_m": apex.get(str(c.get("corner"))),
                "made": int(_num(c.get("made")) or 0), "lost": int(_num(c.get("lost")) or 0)}
               for c in rc.get("pass_corners") or [] if isinstance(c, dict) and c.get("corner")]
    pit = rc.get("pit_cost") if isinstance(rc.get("pit_cost"), dict) else {}
    stops = [{"seconds": _num(p.get("seconds")), "places": p.get("places") if isinstance(p.get("places"), int) else None}
             for p in pit.get("stops") or [] if isinstance(p, dict) and p.get("kind", "stop") == "stop"]
    meta = d.get("meta") if isinstance(d.get("meta"), dict) else {}
    return {"v": v, "sub": str(rc.get("subsession") or ""), "grid": grid, "lap1_pos": first,
            "lap1": grid - first if grid and first else None,
            "finish": _pos_int(rc.get("finish")), "official": bool(rc.get("official")),
            "class_finish": _pos_int(rc.get("class_finish")), "class_size": _pos_int(rc.get("class_size")),
            "made": int(_num(craft.get("made")) or 0), "lost": int(_num(craft.get("lost")) or 0),
            "stuck": int(_num(craft.get("stuck")) or 0), "fights": int(_num(craft.get("fights")) or 0),
            "incidents": int(_num(craft.get("incidents")) or 0), "corners": corners,
            "recovery": _recovery(rc), "stops": stops, "rivals": _rivals_of(rc), "multiclass": bool(meta.get("multiclass"))}


def scanned_rows(rows) -> tuple:
    """(rows with a usable scan, oldest first and one per race, how many scans still wait for a refresh).
    Reviewing the same race twice must not count its passes twice, so the newest review of a subsession wins."""
    good, waiting = {}, 0
    for row in rows or []:
        rc = row.get("racecap") if isinstance(row, dict) else None
        if not isinstance(rc, dict):
            continue
        if (rc.get("v") or 1) < 2:
            waiting += 1
            continue
        key = rc.get("sub") or "dir:" + str(row.get("dir"))
        old = good.get(key)
        if old is None or _safe_ts(row) >= _safe_ts(old):
            good[key] = row
    return sorted(good.values(), key=lambda r: (_safe_ts(r), str(r.get("dir")))), waiting


def _safe_ts(row):
    try:
        return _ts(row)
    except Exception:  # noqa: BLE001
        return 0.0


def _median(v):
    return float(np.median(v)) if v else None


def _best_corners(scanned):
    """Corner names only mean something at one track and can shift between races there, so corners are matched
    per track by apex position, as the track prep page does."""
    from . import library, prep
    tracks = {}
    for row in scanned:
        tracks.setdefault(library._norm(row.get("track")), []).append(row)
    out = []
    for group in tracks.values():
        group = sorted(group, key=_safe_ts, reverse=True)      # names follow the newest review
        L = next((r.get("L") for r in group if r.get("L")), None)
        canon = prep.match_corners([(i, r["racecap"]["corners"]) for i, r in enumerate(group)], L)
        for k in canon:
            made = sum(c["made"] for c in k["per"].values())
            lost = sum(c["lost"] for c in k["per"].values())
            if made >= BEST_CORNER_MIN_MADE:
                out.append({"track": group[0].get("track_short"), "corner": k["id"], "made": made, "lost": lost,
                            "races": len(k["per"])})
    out.sort(key=lambda c: (-(c["made"] - c["lost"]), -c["made"], str(c["track"]), str(c["corner"])))
    return out[:6]


def _craft_note(n_official: int, n_races: int) -> str:
    if not n_official:
        return RACECRAFT_NOTE
    where = "official" if n_official == n_races else f"official in {n_official} of {n_races} races and on the road in the rest"
    return RACECRAFT_NOTE.split(" Positions ")[0] + f" Positions are {where}."


def _craft_summary(out: dict) -> list:
    """What your scanned races say about you, in sentences. Only your own numbers, and only facts the page also shows."""
    said = []
    avg, n = out["lap1"]["avg"], out["n"]
    if avg is not None:
        a = abs(avg)
        said.append("On lap 1 you hold your place on average." if a < 0.5
                    else f"On lap 1 you {'gain' if avg > 0 else 'lose'} about {a:g} place{'' if a == 1 else 's'} on average.")
    pr = out["passes"]
    said.append(f"Each race you make about {pr['made'] / n:.1f} on-track passes and suffer about {pr['lost'] / n:.1f}, among the cars the replay showed.")
    best = next((c for c in out["corners"] if c["made"] > c["lost"]), None)
    if best:
        said.append(f"Your best passing corner is {best['corner']} at {best['track']}: {best['made']} made, {best['lost']} suffered.")
    return said


def racecraft(rows: list) -> dict:
    """Racecraft over your scanned races for the Skills tab. Below RACECRAFT_MIN_RACES only the counts come back,
    and the page asks for more scans: one or two races aren't a habit."""
    scanned, waiting = scanned_rows(rows)
    out = {"n": len(scanned), "min": RACECRAFT_MIN_RACES, "enough": len(scanned) >= RACECRAFT_MIN_RACES,
           "waiting": waiting, "note": RACECRAFT_NOTE}
    if not out["enough"]:
        return out
    rcs = [r["racecap"] for r in scanned]
    lap1 = [rc["lap1"] for rc in rcs if rc.get("lap1") is not None]
    made, lost, stuck = (sum(rc[k] for rc in rcs) for k in ("made", "lost", "stuck"))
    rec = [x for rc in rcs for x in rc.get("recovery") or []]
    hurt = [x for x in rec if x["status"] in ("back", "not_back")]
    back = [x for x in rec if x["status"] == "back"]
    stops = [x for rc in rcs for x in rc.get("stops") or []]
    out.update({
        "lap1": {"races": len(lap1), "avg": _r(sum(lap1) / len(lap1), 1) if lap1 else None,
                 "gained": sum(1 for x in lap1 if x > 0), "lost": sum(1 for x in lap1 if x < 0),
                 "same": sum(1 for x in lap1 if x == 0), "best": max(lap1) if lap1 else None,
                 "worst": min(lap1) if lap1 else None},
        "passes": {"made": made, "lost": lost, "stuck": stuck,
                   "stick_pct": round(100.0 * stuck / made) if made else None},
        "corners": _best_corners(scanned),
        "recovery": {"incidents": len(rec), "no_loss": sum(1 for x in rec if x["status"] == "no_loss"),
                     "cost_places": len(hurt), "back": len(back), "ended": sum(1 for x in rec if x["status"] == "ended"),
                     "laps_back": _r(_median([x["laps_back"] for x in back]), 1),
                     "places": _r(_median([x["places"] for x in hurt]), 1)},
        "pits": {"stops": len(stops), "seconds": _r(_median([x["seconds"] for x in stops if x["seconds"] is not None]), 1),
                 "places": _r(_median([x["places"] for x in stops if x["places"] is not None]), 1)},
        "multiclass": sum(1 for rc in rcs if rc.get("multiclass")),
        "official": sum(1 for rc in rcs if rc.get("official")),
        "races": [{"date": r.get("date"), "track": r.get("track_short"), "car": r.get("car"), "path": r.get("path"),
                   **{k: r["racecap"].get(k) for k in ("grid", "finish", "class_finish", "class_size", "official", "lap1", "made", "lost", "stuck", "incidents")}}
                  for r in reversed(scanned)]})
    out["note"] = _craft_note(out["official"], out["n"])
    out["summary"] = _craft_summary(out)
    return out


# ---------------------------------------------------------------- other trends over time (2.8)
# Same rule as the corner phases: a direction only with enough races over enough time and a real statistical signal.
def _ot_consistency(r):
    c = r.get("consistency")
    return _num(c.get("score")) if isinstance(c, dict) else None


def _ot_clean(r):
    laps = _num(r.get("laps"))
    return round(100.0 * (_num(r.get("clean")) or 0.0) / laps, 1) if laps else None


def _ot_places(r):
    res = r.get("result") if isinstance(r.get("result"), dict) else {}
    a, b = _num(res.get("start_pos")), _num(res.get("finish_pos"))
    return a - b if a and b else None


def _ot_incidents(r):
    res = r.get("result") if isinstance(r.get("result"), dict) else {}
    return _num(res.get("incidents"))


def _scan_value(fn):
    def get(r):
        rc = r.get("racecap") if isinstance(r.get("racecap"), dict) else {}
        return fn(rc) if (rc.get("v") or 1) >= 2 else None
    return get


# key, name, unit, lower is better, smallest change worth a word, value
OVER_TIME = (("clean", "Clean laps", "%", False, 5.0, _ot_clean),
             ("consistency", "Consistency score", "", False, 3.0, _ot_consistency),
             ("places", "Places gained in the race", "places", False, 1.0, _ot_places),
             ("incidents", "Incident points", "points", True, 1.0, _ot_incidents),
             ("lap1", "Places gained on lap 1", "places", False, 1.0, _scan_value(lambda rc: rc.get("lap1"))),
             ("pass_balance", "Passes made minus suffered", "passes", False, 1.0,
              _scan_value(lambda rc: rc.get("made", 0) - rc.get("lost", 0))))


def over_time(rows: list) -> list:
    """Direction of the things the corner-phase trends don't cover. Metrics with fewer than 2 races are left out."""
    rows = sorted((r for r in rows or [] if isinstance(r, dict)), key=lambda r: (_safe_ts(r), str(r.get("dir"))))
    keep = {id(r) for r in scanned_rows(rows)[0]}
    out = []
    for key, name, unit, lower, min_change, get in OVER_TIME:
        pts = []
        for r in rows:
            if key in ("lap1", "pass_balance") and id(r) not in keep:   # the same race reviewed twice counts once
                continue
            v = _num(get(r))
            if v is not None:
                pts.append({"date": r.get("date"), "value": round(v, 1), "track": r.get("track_short"), "_t": _safe_ts(r),
                            "loss_pm": v if lower else -v})    # _trend calls a falling value "improving"
        if len(pts) < 2:
            continue
        t = _trend(pts, min_change)
        if t:
            t["change"] = _r(t["change_pm"] if lower else -t["change_pm"], 1)
        out.append({"key": key, "name": name, "unit": unit, "lower_better": lower, "latest": pts[-1]["value"], "trend": t,
                    "races": [{k: p[k] for k in ("date", "value", "track")} for p in pts]})
    return out


EXIT_NOTE = "Exit speeds from reviews before 2.7.0 were measured 150 m after the apex."


def build(rows: list, include_practice: bool = False) -> dict:
    """Profile for the library Skills tab (see spec 1.3). Never raises on odd rows; empty input gives
    {"groups": [], "weakest": None, "n_races": 0}.
    2.7: race sessions only (library.is_race) unless include_practice; practice laps have no fuel load or
    traffic, so they would make every race look like a step back. "sessions" says which rule was used."""
    from . import library
    items = []
    n_skipped = 0
    n_old = 0   # 2.7.1 (L4): reviews whose exit speeds were read at a fixed 150 m (before 2.7.0)
    for row in rows or []:
        if not include_practice and isinstance(row, dict) and not library.is_race(row):
            n_skipped += 1
            continue
        if isinstance(row, dict) and row.get("exit_150"):
            n_old += 1
        s = _usable(row)
        if s is None:
            continue
        try:
            t = _ts(row)
        except Exception:
            t = 0.0
        items.append((row, s, t))
    out = {"version": VERSION, "caps_pm": {k: v for k, v in skills.CAPS_PM.items()},
           "params": {"min_clean": MIN_CLEAN, "trend_min_races": TREND_MIN_RACES,
                      "trend_min_days": TREND_MIN_DAYS, "trend_min_change_pm": TREND_MIN_CHANGE_PM,
                      "trend_method": "theil_sen", "trend_test": TREND_TEST, "trend_max_p": TREND_MAX_P,
                      "weakest_last": WEAKEST_LAST},
           "groups": [], "weakest": None, "n_races": len(items),
           "sessions": "all" if include_practice else "race", "n_practice_skipped": n_skipped}
    # 2.7.1 (L4): those can't be redone (no traces are kept), so the Skills tab says so
    out["n_exit_150"] = n_old
    if n_old:
        out["exit_note"] = EXIT_NOTE
    race_list = [r for r in rows or [] if isinstance(r, dict) and (include_practice or library.is_race(r))]
    out["racecraft"] = racecraft(race_list)
    out["over_time"] = over_time(race_list)
    if not items:
        return out
    out["groups"].append(_group("all", "All cars", items))
    by_cls = {}
    for it in items:
        by_cls.setdefault(_class_key(it[0].get("car")), []).append(it)
    for k in sorted(by_cls, key=lambda k: (-len(by_cls[k]), k)):
        out["groups"].append(_group("class:" + k, k, by_cls[k]))
    allg = out["groups"][0]["phases"]
    worst, wv = None, None
    for p in ("braking", "entry", "mid", "exit"):
        rs = (allg.get(p) or {}).get("races") or []
        last = [r["loss_pm"] for r in rs[-WEAKEST_LAST:]]
        if not last:
            continue
        m = float(np.median(last))
        if wv is None or m > wv:
            worst, wv = p, m
    if worst is not None:
        out["weakest"] = {"group": "all", "phase": worst, "median_loss_pm": _r(wv)}
    return out
