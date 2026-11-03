"""Review library: one folder per race plus a home page with progress over time.

Practice / qualifying vs race (2.7, "Trust")
--------------------------------------------
Every row carries ``session`` from the review's meta ("Race", "Practice", "Qualifying", None on reviews
older than 2.3). The rule, used everywhere a number is worked out ACROSS reviews:

  * Personal bests, progress trends, recurring weak corners, the season summary, goals and the skills
    profile use RACE sessions only by default (``is_race`` / ``session_kind``: classified by exclusion,
    so only a session that starts with "practice" / "open practice" / "warmup" or "qualif" / "lone qualif"
    is left out; None, "Session", "Heat", "Feature" and anything else unknown count as a race).
  * Practice and qualifying rows are kept in the library and on the library page (the Races table
    shows them with a session chip) but don't set or compare against a personal best unless the caller
    asks: ``include_practice=True`` on list_reviews / pb_for / prep.build / profile.build, which the
    library page's "Include practice and qualifying" checkbox reads as the rows' ``pb_all``.
  * A qualifying lap may count as a "best lap" ONLY when explicitly included that way.

Reasons: a practice session has no fuel load, no traffic and no pressure, so its best lap would make
every race look like a step back; and a trend fitted through a mix of practice and race laps measures
the mix, not the driver.
"""
from __future__ import annotations

import json
import shutil
import math
import re
from pathlib import Path

from .engine import g
from . import fsutil, report


def local_day(iso):
    """Race date in the PC's local time zone (Garage 61 times are UTC)."""
    if not iso:
        return ""
    from datetime import datetime, timezone
    s = str(iso).replace("Z", "+00:00")
    m = re.match(r"(.*T\d\d:\d\d:\d\d)(\.\d+)?(.*)$", s)
    if m:  # Python 3.10 only accepts 3- or 6-digit fractions; drop them
        s = m.group(1) + m.group(3)
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%Y-%m-%d")
    except ValueError:
        return str(iso)[:10]


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")[:40]


def review_dir(folder, laps, track_name, car_name, suffix: str = "") -> Path:
    start = min((g(l, "start_time", "startTime", default="") or "") for l in laps)
    day = local_day(start) or "undated"
    ev = str(g(laps[0], "event", default="event"))[-6:].lower()
    d = Path(folder) / "reviews" / (f"{day}_{slugify(track_name)}_{slugify(car_name)}_{ev}" + (f"_{suffix}" if suffix else ""))
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------- short names

# Base track name (before any "(config)" or " - config") -> short name.
_TRACK_BASES = {
    "charlotte motor speedway": "Charlotte",
    "watkins glen international": "Watkins Glen",
    "mount panorama circuit": "Bathurst",
    "circuit de spa-francorchamps": "Spa",
    "autodromo nazionale monza": "Monza",
    "autodromo enzo e dino ferrari": "Imola",
    "autodromo internazionale enzo e dino ferrari": "Imola",
    "weathertech raceway at laguna seca": "Laguna Seca",
    "weathertech raceway laguna seca": "Laguna Seca",
    "mazda raceway laguna seca": "Laguna Seca",
    "sebring international raceway": "Sebring",
    "daytona international speedway": "Daytona",
    "circuit of the americas": "COTA",
    "virginia international raceway": "VIR",
    "lime rock park": "Lime Rock",
    "road america": "Road America",
    "road atlanta": "Road Atlanta",
    "michelin raceway road atlanta": "Road Atlanta",
    "mid-ohio sports car course": "Mid-Ohio",
    "canadian tire motorsport park": "Mosport",
    "circuit gilles villeneuve": "Montreal",
    "suzuka international racing course": "Suzuka",
    "okayama international circuit": "Okayama",
    "fuji international speedway": "Fuji",
    "autodromo jose carlos pace": "Interlagos",
    "brands hatch circuit": "Brands Hatch",
    "silverstone circuit": "Silverstone",
    "donington park racing circuit": "Donington",
    "oulton park circuit": "Oulton Park",
    "snetterton circuit": "Snetterton",
    "circuit zandvoort": "Zandvoort",
    "circuit park zandvoort": "Zandvoort",
    "red bull ring": "Red Bull Ring",
    "hockenheimring baden-wurttemberg": "Hockenheim",
    "hockenheimring baden-württemberg": "Hockenheim",
    "nurburgring combined": "Nürburgring",
    "nürburgring combined": "Nürburgring",
    "nurburgring grand-prix-strecke": "Nürburgring GP",
    "nürburgring grand-prix-strecke": "Nürburgring GP",
    "nurburgring nordschleife": "Nordschleife",
    "nürburgring nordschleife": "Nordschleife",
    "circuit de barcelona catalunya": "Barcelona",
    "circuit de barcelona-catalunya": "Barcelona",
    "circuito de navarra": "Navarra",
    "algarve international circuit": "Portimão",
    "autódromo internacional do algarve": "Portimão",
    "sonoma raceway": "Sonoma",
    "long beach street circuit": "Long Beach",
    "indianapolis motor speedway": "Indianapolis",
    "summit point raceway": "Summit Point",
    "oran park raceway": "Oran Park",
    "phillip island circuit": "Phillip Island",
    "detroit grand prix at belle isle": "Belle Isle",
    "motorsport arena oschersleben": "Oschersleben",
    "misano world circuit marco simoncelli": "Misano",
    "circuit de nevers magny-cours": "Magny-Cours",
    "circuit paul ricard": "Paul Ricard",
    "le mans": "Le Mans",
    "circuit des 24 heures du mans": "Le Mans",
    "tsukuba circuit": "Tsukuba",
    "willow springs international raceway": "Willow Springs",
    "barber motorsports park": "Barber",
    "homestead miami speedway": "Homestead",
    "texas motor speedway": "Texas",
    "atlanta motor speedway": "Atlanta",
    "las vegas motor speedway": "Las Vegas",
    "talladega superspeedway": "Talladega",
    "bristol motor speedway": "Bristol",
    "martinsville speedway": "Martinsville",
    "kansas speedway": "Kansas",
    "michigan international speedway": "Michigan",
    "pocono raceway": "Pocono",
    "phoenix raceway": "Phoenix",
    "darlington raceway": "Darlington",
    "richmond raceway": "Richmond",
    "dover motor speedway": "Dover",
    "chicagoland speedway": "Chicagoland",
    "auto club speedway": "Fontana",
    "new hampshire motor speedway": "Loudon",
    "world wide technology raceway": "Gateway",
}

# Configs that mean "the usual layout": dropped from the short name.
_DEFAULT_CONFIGS = {"", "grand prix", "gp", "full course", "full", "road course", "international",
                    "full circuit", "grand prix circuit", "combined", "national"}
_CONFIG_WORDS = {"grand prix": "GP", "international": "Intl", "national": "National"}
_FILLER = re.compile(r"\b(international|raceway|speedway|motor|motorsports?|circuit|park|racing|course|"
                     r"autodromo|autódromo|circuito|street|at|de|the)\b", re.I)


def short_track_name(name) -> str:
    """'Charlotte Motor Speedway (Roval 2019)' -> 'Roval', 'Watkins Glen International (Boot)' -> 'Watkins Glen Boot'."""
    full = re.sub(r"\s+", " ", str(name or "")).strip()
    if not full:
        return "Unknown track"
    m = re.match(r"^(.*?)\s*(?:\((.*)\)|\s[-–]\s(.*))\s*$", full)
    base, config = (m.group(1), (m.group(2) or m.group(3) or "")) if m else (full, "")
    config = re.sub(r"\b(19|20)\d\d\b", "", config).strip(" -–,")
    cfg_l = config.lower()
    if "roval" in cfg_l:
        return "Roval"
    known = _TRACK_BASES.get(base.lower())
    if not known:
        words = _FILLER.sub("", base)
        known = re.sub(r"\s+", " ", words).strip(" -–,") or base
        if len(known) > 18:
            known = " ".join(known.split()[:2])
    if known.lower() == "daytona" and "road" in cfg_l:
        return "Daytona Road"
    config = re.sub(r"\b(pits|gesamtstrecke|layout|circuit|configuration)\b", "", config, flags=re.I)
    config = re.sub(r"\s+", " ", config).strip(" -–,")
    cfg_l = config.lower()
    if cfg_l in _DEFAULT_CONFIGS:
        return known
    cfg = _CONFIG_WORDS.get(cfg_l, config)
    return f"{known} {cfg}".strip()


# ---------------------------------------------------------------- rows

def _num(x):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _r(x, nd=3):
    x = _num(x)
    return None if x is None else round(x, nd)


def _int(x):
    x = _num(x)
    return int(round(x)) if x is not None else None


def season_from_date(day: str) -> dict | None:
    """Best guess at the iRacing season for a race date, for reviews that predate the `season` key.

    iRacing runs four 12-13 week seasons a year starting roughly mid-December, mid-March, mid-June and
    mid-September (e.g. "2026 Season 4" ran 2026-09-15 .. 2026-12-15). Marked estimated=True."""
    m = re.match(r"^(\d{4})-(\d\d)-(\d\d)", str(day or ""))
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    md = mo * 100 + d
    if md >= 1215:
        y, n = y + 1, 1
    elif md >= 915:
        n = 4
    elif md >= 615:
        n = 3
    elif md >= 315:
        n = 2
    else:
        n = 1
    return {"id": None, "name": f"{y} Season {n}", "shortName": f"{y}S{n}", "estimated": True}


def _season(d: dict, day: str) -> dict | None:
    s = d.get("season")
    if isinstance(s, dict) and (s.get("name") or s.get("shortName")):
        return {"id": s.get("id"), "name": str(s.get("name") or s.get("shortName")),
                "shortName": str(s.get("shortName") or s.get("name")), "estimated": False}
    return season_from_date(day)


_RESULT_KEYS = ("start_pos", "finish_pos", "field_size", "incidents", "irating_before", "irating_after",
                "sr_before", "sr_after", "sof", "laps_led", "irating_change", "sr_change", "positions_gained",
                # 2.4 (numbers only here; finish_status and incident_laps stay in result.json / the review page)
                "pos_after_lap1", "pit_stops", "penalties", "positions_lap1")


def _result(d: dict, review_dir: Path) -> dict | None:
    res = d.get("result")
    if not isinstance(res, dict):
        try:
            from . import results
            res = results.load(review_dir)
        except Exception:
            res = None
    if not isinstance(res, dict) or _num(res.get("finish_pos")) is None:
        return None
    out = {k: res.get(k) for k in _RESULT_KEYS if _num(res.get(k)) is not None}
    if res.get("notes"):
        out["notes"] = str(res["notes"])[:500]
    return out


def _consistency(d: dict) -> dict | None:
    c = d.get("consistency")
    if not isinstance(c, dict) or _num(c.get("score")) is None:
        return None
    return {"score": _int(c.get("score")), "label": str(c.get("label") or ""), "std": _r(c.get("std"))}


def _exit_150(d: dict) -> bool:
    """True for a review made before 2.7.0: it has corners but none says where its exit speed was read."""
    cs = [c for c in (d.get("corners") or []) if isinstance(c, dict)]
    return bool(cs) and not any("exit_at_m" in c for c in cs)


def _skills(d: dict) -> dict | None:
    return _skills_and_why(d)[0]


def _skills_and_why(d: dict) -> tuple:
    """(compact skill numbers or None, why there are none or None). 2.18: a review saved without a skills block
    (made before 2.3 and never re-rendered) gets one worked out here from its saved traces (pure, no requests),
    so the Skills tab never says "no skill scores yet" while the reviews hold what it needs. why is a
    skills.for_review reason ("no_traces", "not_enough_clean_laps", ...) or "few_clean" (scored on fewer clean
    laps than the Skills tab needs)."""
    try:
        from . import skills
        blk = d.get("skills")
        if not isinstance(blk, dict):
            blk = skills.for_review(d)
        c = skills.compact(blk)
        if c is not None:
            n = c.get("n_clean")
            return c, ("few_clean" if isinstance(n, int) and n < skills.MIN_CLEAN else None)
        why = blk.get("reason") if isinstance(blk, dict) else None
        return None, str(why or "bad_data")
    except Exception:
        return None, "bad_data"


def clean_best(d: dict):
    """The review's best lap time if it can count for PBs, goals and the team board, else None.

    Reviews from this version say so in best["clean"] (False = every lap was flagged by Garage 61, so
    the best is for display only). Older reviews have no flag: their best counts only when it is at
    least 75% of the median lap (a shorter "best" is a partial lap, e.g. 7 s at the finish line)."""
    b = d.get("best") if isinstance(d.get("best"), dict) else {}
    t = _num(b.get("t"))
    if "clean" in b:
        return t if b.get("clean") else None
    laps = [_num(l.get("t")) for l in d.get("laps") or [] if isinstance(l, dict)]
    laps = [x for x in laps if x is not None and x > 0]
    med = _num(d.get("median"))
    if med is None and laps:
        med = float(sorted(laps)[len(laps) // 2])
    if t is None and laps:  # no best stored: quickest plausible lap
        t = min((x for x in laps if med is None or x >= 0.75 * med), default=None)
    if t is None or (med is not None and t < 0.75 * med):
        return None
    return t


def _row(p: Path) -> dict | None:
    """The library row for one review.json (parsed here; list_reviews uses the row index instead)."""
    return _row_from(json.loads(p.read_text(encoding="utf-8")), p)


def _racecap(d: dict) -> dict | None:
    try:
        from . import profile
        return profile.racecap_row(d)
    except Exception:  # noqa: BLE001  (an odd scan must never lose the review from the library)
        return None


def _row_from(d, p: Path) -> dict | None:
    if not isinstance(d, dict):
        return None
    m = d.get("meta") or {}
    if not isinstance(m, dict):
        m = {}
    laps = [l for l in (d.get("laps") or []) if isinstance(l, dict) and _num(l.get("t"))]
    best = _num((d.get("best") or {}).get("t"))
    if best is None and laps:
        best = min(_num(l["t"]) for l in laps)
    if best is None:
        return None
    corners = []
    for c in d.get("corners") or []:
        if not isinstance(c, dict):
            continue
        av = _num(c.get("avail"))
        if av is None:
            continue
        corners.append({"id": str(c.get("id", "?")), "apex_m": _r(c.get("apex_m"), 1), "avail": round(av, 4),
                        "rank": c.get("rank") if isinstance(c.get("rank"), int) else None})
    # fill missing ranks from avail order
    if any(c["rank"] is None for c in corners):
        for i, c in enumerate(sorted(corners, key=lambda c: -c["avail"])):
            c["rank"] = i + 1
    corners.sort(key=lambda c: c["rank"])
    top = corners[0] if corners else None
    refs = [r for r in (d.get("refs") or []) if isinstance(r, dict) and _num(r.get("t")) and not r.get("rival")]
    main = next((r for r in refs if r.get("main")), refs[0] if refs else None)
    counts = d.get("counts") or {}
    from . import names   # 2.18: one spelling per track and car (names.track / names.car)
    track = names.track(m.get("track")) or "Unknown track"
    car = names.car(m.get("car")) or "Unknown car"
    clean = sum(1 for l in laps if l.get("clean"))
    ids = d.get("combo_ids") if isinstance(d.get("combo_ids"), dict) else {}
    day = local_day(m.get("started_at")) or p.parent.name[:10]
    sk, sk_why = _skills_and_why(d)
    return {
        "dir": p.parent.name,
        "path": f"reviews/{p.parent.name}/index.html",
        "date": day,
        "started_at": str(m.get("started_at") or ""),
        "track": track, "track_short": short_track_name(track), "car": car,
        "combo": f"{track}|{car}",
        "session": row_session(m.get("session"), p.parent.name),
        "best": round(best, 3), "best_lap": (d.get("best") or {}).get("n"),
        # False when this race's best can't count for PBs/goals (all laps flagged, or a partial lap)
        "best_clean": clean_best(d) is not None,
        "median": _r(d.get("median")), "std": _r(d.get("std_clean")), "optimal": _r(d.get("optimal")),
        "laps": len(laps), "analysed": counts.get("analysed", len(laps)) if isinstance(counts, dict) else len(laps),
        "clean": clean, "fade10": _r(d.get("trend_per10")), "L": _r(d.get("L"), 1),
        "top_corner": (f"{top['id']} ~{top['avail']:.2f}s" if top else ""),
        "top_corner_id": top["id"] if top else None, "top_corner_avail": top["avail"] if top else None,
        "corners": corners,
        "ref_gap": round(best - _num(main["t"]), 3) if (main and clean_best(d) is not None) else None,
        "test_data": bool(m.get("test_data")),
        "event_id": str(m.get("event_id") or ""),
        # v2 fields (all optional: None/absent on reviews made before Debrief v2)
        "track_id": _int(ids.get("track_id")), "car_id": _int(ids.get("car_id")),
        "irating": _int(d.get("irating")),
        "season": _season(d, day),
        "consistency": _consistency(d),
        "result": _result(d, p.parent),
        # 2.3: compact skill numbers (loss per phase, per-mille of lap time) for the Skills tab; None on
        # reviews without a usable skills block (old reviews get one when the library refreshes)
        "skills": sk,
        "skills_why": sk_why,
        # 2.7.1 (L4): reviews before 2.7.0 read exit speed 150 m after the apex; 2.7.0 scales it per corner
        # (corners[].exit_at_m). Old values can't be recomputed (no traces are kept), so they are flagged.
        "exit_150": _exit_150(d),
        # compact replay-scan numbers for the racecraft profile, the track prep brief and the weekly digest
        "racecap": _racecap(d),
        "driver": str(m.get("driver") or ""),
        "pb": None,  # filled by list_reviews (PB at the time of that race, chronologically)
    }


# ---------------------------------------------------------------- combos + personal bests

_PRACTICE_PREFIXES = ("practice", "open practice", "warmup", "warm up", "warm-up")
_QUALIFYING_PREFIXES = ("qualif", "lone qualif")


def session_kind(s) -> str:
    """"practice" | "qualifying" | "race" for a review's session label. Classified by EXCLUSION (2.7.1):
    only a label that starts with practice / open practice / warmup is practice, only one that starts with
    qualif / lone qualif is qualifying; everything else (None, "", "Session", "Heat", "Feature", "Race 2")
    is a race. strategy._is_race and the library page's isRace (templates/library.html) use this same rule."""
    t = re.sub(r"\s+", " ", str(s or "")).strip().lower()
    if t.startswith(_PRACTICE_PREFIXES):
        return "practice"
    if t.startswith(_QUALIFYING_PREFIXES):
        return "qualifying"
    return "race"


def row_session(label, dir_name: str = ""):
    """The session a library row shows. Reviews saved 2.1.0-2.7.0 with practice included carry the generic
    "Session" and may hold practice, qualifying and race laps together (their folder suffix came from the
    first lap, so a _practice folder can hold the race). They count as a race: over-counting a practice-only
    one is the lesser harm than hiding a race. dir_name is kept for callers; it no longer changes the label."""
    return label


def review_path(folder, laps, track_name, car_name, suffix: str = "") -> Path:
    """review_dir's folder without creating it."""
    start = min((g(l, "start_time", "startTime", default="") or "") for l in laps)
    day = local_day(start) or "undated"
    ev = str(g(laps[0], "event", default="event"))[-6:].lower()
    return Path(folder) / "reviews" / (f"{day}_{slugify(track_name)}_{slugify(car_name)}_{ev}" + (f"_{suffix}" if suffix else ""))


def retire_legacy_mixed(race_dir, race_start=None, log=None) -> list:
    """Before a race review is written to race_dir: an older review of the same event saved as "Session" in a
    _practice / _qualifying folder (2.1.0-2.7.0 with practice included) may be the same race reviewed together
    with its practice laps. It is only treated so when one of its laps started at or after race_start (the
    epoch seconds of the new race's first lap); otherwise it was a practice-only review and is relabelled
    Practice / Qualifying and left in place. A mixed one is first copied to .backup/replaced/, then renamed to
    race_dir when that doesn't exist yet (keeping a typed-in result.json) or removed when it does, so the race
    is never listed twice and a wrong guess never loses a review. Returns the folders retired."""
    race_dir = Path(race_dir)
    moved = []
    for suf in ("_practice", "_qualifying"):
        old = race_dir.with_name(race_dir.name + suf)
        try:
            d = json.loads((old / "review.json").read_text(encoding="utf-8"))
            meta = d.get("meta") or {}
        except Exception:  # noqa: BLE001  (missing or unreadable: not ours to touch)
            continue
        if str(meta.get("session") or "").strip().lower() not in ("", "session"):
            continue
        starts = [float(l["start"]) for l in (d.get("laps") or [])
                  if isinstance(l, dict) and isinstance(l.get("start"), (int, float))]
        holds_race = race_start is None or not starts or max(starts) >= float(race_start) - 1
        if not holds_race:
            # a practice-only review from before 2.7.0: say what it is, and leave it where it is
            try:
                meta["session"] = "Practice" if suf == "_practice" else "Qualifying"
                d["meta"] = meta
                fsutil.write_text(old / "review.json", json.dumps(d, ensure_ascii=False, allow_nan=False),
                                  fsync=True)
            except (OSError, ValueError) as e:
                if log:
                    log(f"  Couldn't relabel the older review {old.name}: {e}")
            continue
        dest = race_dir.parent.parent / ".backup" / "replaced" / old.name
        try:
            if dest.exists():
                shutil.rmtree(dest, ignore_errors=True)
            shutil.copytree(old, dest)          # a copy first, so nothing is ever lost
        except OSError as e:
            if log:
                log(f"  Couldn't back up the older mixed review {old.name}, so it was left as it is: {e}")
            continue
        try:
            if not race_dir.exists():
                old.rename(race_dir)
            else:
                shutil.rmtree(old)
            moved.append(old.name)
            if log:
                log(f"  Replaced an older review of this event that mixed practice and race laps ({old.name}; "
                    "a copy is in .backup/replaced).")
        except OSError as e:
            if log:
                log(f"  Couldn't tidy the older mixed review {old.name} (a copy is in .backup/replaced): {e}")
    return moved


def is_race(row) -> bool:
    """True for a race review; False for practice / qualifying rows (session_kind). The rule is in the
    module docstring."""
    s = (row or {}).get("session") if isinstance(row, dict) else None
    return session_kind(s) == "race"


def race_rows(rows: list, include_practice: bool = False) -> list:
    """The rows cross-review numbers (PBs, trends, weak corners, profile, season) are worked out on."""
    return list(rows) if include_practice else [r for r in rows if is_race(r)]


def _norm(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def same_combo(a: dict, b: dict) -> bool:
    """True when two rows/reviews are the same track+car. Uses Garage 61 ids when BOTH sides have
    both ids (names can change between versions); otherwise compares track and car names."""
    ta, ca, tb, cb = a.get("track_id"), a.get("car_id"), b.get("track_id"), b.get("car_id")
    if None not in (ta, ca, tb, cb):
        return int(ta) == int(tb) and int(ca) == int(cb)
    return _norm(a.get("track")) == _norm(b.get("track")) and _norm(a.get("car")) == _norm(b.get("car"))


def _ts(r: dict) -> float:
    """Sort key: race start as a UTC timestamp (falls back to the date, then 0)."""
    from datetime import datetime, timezone
    for v, fmt in ((r.get("started_at"), None), (r.get("date"), "%Y-%m-%d")):
        if not v:
            continue
        s = str(v).replace("Z", "+00:00")
        try:
            if fmt:
                dt = datetime.strptime(s[:10], fmt).replace(hour=12)
            else:
                mm = re.match(r"(.*T\d\d:\d\d:\d\d)(\.\d+)?(.*)$", s)
                dt = datetime.fromisoformat(mm.group(1) + mm.group(3) if mm else s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            continue
    return 0.0


PB_EPS = 0.0005  # laps must be at least half a millisecond quicker to count as a new PB


def _pb_info(best: float, previous: list) -> dict:
    """previous: earlier rows at the same combo. Sign convention: delta = this best - previous best,
    so a NEGATIVE delta is a new personal best."""
    prev = [r for r in previous if _num(r.get("best")) is not None and r.get("best_clean", True)]
    if best is None:  # this race's best isn't clean: never a PB (no delta either), and not a benchmark
        return {"first_time": False, "is_pb": False, "best_clean": False,
                "previous_best": round(min(r["best"] for r in prev), 3) if prev else None,
                "previous_date": min(prev, key=lambda r: (r["best"], _ts(r))).get("date") if prev else None,
                "delta": None, "races_at_combo": len(prev) + 1}
    if not prev:
        return {"first_time": True, "is_pb": False, "previous_best": None, "previous_date": None,
                "delta": None, "races_at_combo": 1}
    pb = min(prev, key=lambda r: (r["best"], _ts(r)))
    delta = round(best - pb["best"], 3)
    return {"first_time": False, "is_pb": best < pb["best"] - PB_EPS, "previous_best": round(pb["best"], 3),
            "previous_date": pb.get("date") or None, "delta": delta, "races_at_combo": len(prev) + 1}


def _apply_pbs(rows: list, include_practice: bool = False, key: str = "pb") -> None:
    """Fill row[key] = PB status at the time of that race (only earlier races count).

    Same result as calling _pb_info(best, grp[:i]) for each race of a combo, but worked out in one pass
    (running count + running quickest), with each row's timestamp parsed once: O(n) per combo, not O(n²).

    2.7: by default only race rows set or compare against a PB (module docstring). A practice / qualifying
    row then gets {"is_pb": False, "first_time": False, "session_excluded": True, ...} with the race PB it
    would have been measured against; include_practice=True counts every row (written to `key`, which the
    library page uses as "pb_all")."""
    ts = {id(x): _ts(x) for x in rows}
    done = set()
    for r in rows:
        if id(r) in done:
            continue
        grp = sorted([x for x in rows if same_combo(x, r)], key=lambda x: (ts[id(x)], x["dir"]))
        n_prev, pb = 0, None          # earlier clean rows with a best, and the quickest of them (first on ties)
        for x in grp:
            done.add(id(x))
            counts = include_practice or is_race(x)
            best = x["best"] if x.get("best_clean", True) else None
            if not counts:
                info = _pb_from(None, n_prev, pb)
                info["best_clean"] = bool(x.get("best_clean", True))
                info["session_excluded"] = True
                info["delta"] = round(best - pb[0]["best"], 3) if (best is not None and pb) else None
                x[key] = info
                continue
            x[key] = _pb_from(best, n_prev, pb)
            if _num(x.get("best")) is not None and x.get("best_clean", True):
                if pb is None or (x["best"], ts[id(x)]) < (pb[0]["best"], pb[1]):
                    pb = (x, ts[id(x)])
                n_prev += 1


def _pb_from(best, n_prev: int, pb) -> dict:
    """_pb_info from a running count of earlier clean rows and the quickest one so far ((row, ts) or None)."""
    if best is None:
        return {"first_time": False, "is_pb": False, "best_clean": False,
                "previous_best": round(pb[0]["best"], 3) if pb else None,
                "previous_date": pb[0].get("date") if pb else None,
                "delta": None, "races_at_combo": n_prev + 1}
    if pb is None:
        return {"first_time": True, "is_pb": False, "previous_best": None, "previous_date": None,
                "delta": None, "races_at_combo": 1}
    p = pb[0]
    delta = round(best - p["best"], 3)
    return {"first_time": False, "is_pb": best < p["best"] - PB_EPS, "previous_best": round(p["best"], 3),
            "previous_date": p.get("date") or None, "delta": delta, "races_at_combo": n_prev + 1}


def _unify_combo_keys(rows: list) -> None:
    """Rows with the same Garage 61 ids share one combo key even if a name changed."""
    by_ids = {}
    for r in sorted(rows, key=_ts):
        if r.get("track_id") is not None and r.get("car_id") is not None:
            k = (r["track_id"], r["car_id"])
            if k in by_ids:
                r["combo"] = by_ids[k]
            else:
                by_ids[k] = r["combo"]
    # an old row (no ids) with the same names as an id row: borrow the ids so pickers can use them
    named = {(_norm(r["track"]), _norm(r["car"])): (r["track_id"], r["car_id"]) for r in rows
             if r.get("track_id") is not None and r.get("car_id") is not None}
    for r in rows:
        if r.get("track_id") is None or r.get("car_id") is None:
            ids = named.get((_norm(r["track"]), _norm(r["car"])))
            if ids:
                r["ids_borrowed"] = True
                r["track_id"], r["car_id"] = ids


def _data_as_row(data: dict) -> dict:
    m = data.get("meta") or {}
    ids = data.get("combo_ids") if isinstance(data.get("combo_ids"), dict) else {}
    best = clean_best(data)
    return {"track": str(m.get("track") or ""), "car": str(m.get("car") or ""),
            "track_id": _int(ids.get("track_id")), "car_id": _int(ids.get("car_id")),
            "best": best, "started_at": str(m.get("started_at") or ""), "session": m.get("session"),
            "date": local_day(m.get("started_at")), "event_id": str(m.get("event_id") or "")}


def pb_for(folder, data: dict, exclude_dir: str | None = None, rows: list | None = None,
           include_practice: bool = False) -> dict:
    """Personal-best status of a freshly analysed review against the library.

    Returns {"first_time", "is_pb", "previous_best", "previous_date", "delta", "races_at_combo"}.
      * combo match: combo_ids when both sides have them, else track + car names
      * only reviews of races that started BEFORE this one count as "previous" (re-reviewing an old
        race doesn't compare it against later races); the same event and `exclude_dir` are skipped
      * delta = this best - previous best (seconds). Negative = new PB. None on a first visit.
      * races_at_combo includes this race (1 = first time here)
      * is_pb is False on a first visit (first_time says that instead)
      * a race whose best isn't clean (see clean_best) is never a PB or a first-time benchmark:
        {"is_pb": False, "first_time": False, "best_clean": False, "delta": None, ...}; earlier
        races with unclean bests don't count as the previous best either
      * 2.7: only RACE reviews set or compare against a PB (module docstring). A practice / qualifying
        review gets {"is_pb": False, "first_time": False, "session_excluded": True, "previous_best": the
        race PB, "delta": its gap to that}, and earlier practice rows never count as the previous best,
        unless include_practice=True.
    rows: library rows already in hand (list_reviews output, with or without PBs); None reads them (from the
    row index, so this is cheap even on a big library).
    """
    me = _data_as_row(data)
    t_me = _ts(me) if me["started_at"] else None
    prev = []
    for r in (rows if rows is not None else list_reviews(folder, _pbs=False)):
        if exclude_dir and r["dir"] == exclude_dir:
            continue
        if me["event_id"] and r.get("event_id") == me["event_id"]:
            continue
        if not same_combo(me, r):
            continue
        if t_me is not None and r.get("started_at") and _ts(r) >= t_me:
            continue
        if not include_practice and not is_race(r):
            continue
        prev.append(r)
    if not include_practice and not is_race(me):
        info = _pb_info(None, prev)
        info["best_clean"] = me["best"] is not None
        info["session_excluded"] = True
        info["delta"] = round(me["best"] - info["previous_best"], 3) if (me["best"] is not None and info["previous_best"] is not None) else None
        return info
    return _pb_info(me["best"], prev)


# ---------------------------------------------------------------- season summary

def season_summary(rows: list, include_practice: bool = False) -> dict:
    """Per-season totals for the Season tab. Seasons come from Garage 61 (`season`) or, for old
    reviews, are estimated from the race date (estimated=True). 2.7: race rows only unless include_practice."""
    seasons = {}
    for r in sorted(race_rows(rows, include_practice), key=_ts):
        s = r.get("season") or {"name": "Unknown season", "shortName": "?", "estimated": True}
        k = s["name"]
        e = seasons.setdefault(k, {"key": k, "name": s["name"], "shortName": s.get("shortName"),
                                   "estimated": True, "races": 0, "first": r["date"], "last": r["date"],
                                   "combos": {}, "irating": [], "results": []})
        e["estimated"] = e["estimated"] and bool(s.get("estimated"))
        e["races"] += 1
        e["last"] = r["date"]
        c = e["combos"].setdefault(r["combo"], {"combo": r["combo"], "track_short": r["track_short"],
                                                "car": r["car"], "races": 0, "best": None})
        c["races"] += 1
        if r.get("best_clean", True) and _num(r.get("best")) is not None:   # a flagged/partial best never counts
            c["best"] = r["best"] if c["best"] is None else min(c["best"], r["best"])
        if r.get("irating") is not None:
            e["irating"].append(r["irating"])
        if r.get("result"):
            e["results"].append(r["result"])
    out = []
    for e in seasons.values():
        res = e.pop("results")
        ir = e.pop("irating")
        fin = [x["finish_pos"] for x in res if _num(x.get("finish_pos")) is not None]
        inc = [x["incidents"] for x in res if _num(x.get("incidents")) is not None]
        src = [x["sr_change"] for x in res if _num(x.get("sr_change")) is not None]
        irc = [x["irating_change"] for x in res if _num(x.get("irating_change")) is not None]
        e.update({"combos": sorted(e["combos"].values(), key=lambda c: -c["races"]),
                  "irating_first": ir[0] if ir else None, "irating_last": ir[-1] if ir else None,
                  # change across the season's reviewed races (last minus first; positive = gained)
                  "irating_change": (ir[-1] - ir[0]) if len(ir) > 1 else None,
                  "results": len(res),
                  "avg_finish": round(sum(fin) / len(fin), 1) if fin else None,
                  "best_finish": min(fin) if fin else None,
                  "avg_incidents": round(sum(inc) / len(inc), 1) if inc else None,
                  "sr_change_total": round(sum(src), 2) if src else None,
                  "irating_change_entered": sum(irc) if irc else None})
        out.append(e)
    out.sort(key=lambda e: e["last"], reverse=True)
    return {"seasons": out, "sessions": "all" if include_practice else "race"}


# ---------------------------------------------------------------- row index (2.6)
# <library>/.rows.json keeps each review's library row (and the small summary the window's review list
# shows) next to the review.json size and mtime it was worked out from, so a scan reads one small file and
# stats the reviews instead of parsing every ~300 KB review.json. An entry is used only while review.json
# and result.json still have the same mtime_ns and size; anything else (a new, changed or deleted review, a
# missing or damaged index, another Debrief version or time zone) is worked out again and the index is
# rewritten atomically. Losing the index only costs one slow scan.
INDEX_FILE = ".rows.json"
INDEX_VERSION = 3   # 2: rows gained "racecap" (the Skills tab's racecraft profile) and "driver"; 3 (2.18): skills
                    # worked out for reviews saved without them, and "skills_why"
RACY_NS = 3_000_000_000   # entries for files changed less than 3 s before they were read are re-read
_INDEX_LOCK = __import__("threading").Lock()


def _index_header() -> dict:
    import time
    from . import __version__
    # rows hold the race date in the PC's time zone (local_day), so a time-zone change redoes them
    return {"v": INDEX_VERSION, "app": __version__,
            "tz": f"{time.timezone}|{time.altzone}|{time.daylight}|{'/'.join(time.tzname)}"}


def _stat_key(p: Path) -> list | None:
    try:
        st = p.stat()
    except OSError:
        return None
    return [st.st_mtime_ns, st.st_size]


def _summary_from(d, p: Path) -> dict | None:
    """What the window's review list and team.library_combos read from a review, raw (formatted by them)."""
    if not isinstance(d, dict):
        return None
    m = d.get("meta", {})
    if not isinstance(m, dict):
        return None
    ids = d.get("combo_ids") if isinstance(d.get("combo_ids"), dict) else {}
    laps = d.get("laps", [])
    return {"track": m.get("track"), "car": m.get("car"), "started_at": m.get("started_at"),
            "session": row_session(m.get("session"), p.parent.name), "best_t": (d.get("best") or {}).get("t"),
            "n_laps": len(laps) if hasattr(laps, "__len__") else None,
            "track_id": ids.get("track_id"), "car_id": ids.get("car_id"), "clean_best": clean_best(d)}


def _load_index(folder: Path) -> dict:
    try:
        d = json.loads((folder / INDEX_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(d, dict) or d.get("header") != _index_header() or not isinstance(d.get("reviews"), dict):
        return {}
    return d["reviews"]


def _scan(folder) -> list:
    """[(review dir name, entry)] for every reviews/*/review.json, in folder order. entry = {"m", "s", "r",
    "row", "sum"}: review.json's mtime_ns and size, result.json's [mtime_ns, size] (or None), the library row
    and the summary (None when the review can't be read). Rows come back as fresh objects every call."""
    folder = Path(folder)
    base = folder / "reviews"
    if not base.is_dir():
        return []
    with _INDEX_LOCK:   # one thread works a changed library out; the others then find it ready
        old = _load_index(folder)
        out, new, dirty = [], {}, False
        for p in sorted(base.glob("*/review.json")):
            name = p.parent.name
            key = _stat_key(p)         # stat before reading: a review saved meanwhile is redone next scan
            if key is None:
                continue
            rkey = _stat_key(p.parent / "result.json")
            e = old.get(name)
            if not (isinstance(e, dict) and [e.get("m"), e.get("s")] == key and e.get("r") == rkey
                    and "row" in e and "sum" in e and not e.get("racy")):
                try:
                    text = p.read_text(encoding="utf-8")
                except OSError:        # locked for a moment (antivirus, OneDrive): skip, don't remember
                    continue
                try:
                    d = json.loads(text)
                except ValueError:
                    d = None
                try:
                    row = _row_from(d, p) if d is not None else None
                except Exception:  # noqa: BLE001  (an odd review is skipped, as before)
                    row = None
                try:
                    summ = _summary_from(d, p)
                except Exception:  # noqa: BLE001
                    summ = None
                e = {"m": key[0], "s": key[1], "r": rkey, "row": row, "sum": summ}
                # "racy" (as in git): a file changed within RACY_NS of this read could change again with the
                # same size and a time stamp the drive can't tell apart (FAT/exFAT keep 2 s, some network
                # shares worse), so it is read again next scan until it has settled
                newest = max(key[0], (rkey or [0, 0])[0])
                if 0 <= __import__("time").time_ns() - newest < RACY_NS:   # a future time stamp (clock skew)
                    # is not racy: it would otherwise be re-read on every scan for as long as it stays ahead
                    e["racy"] = True
                dirty = True
            new[name] = e
            out.append((name, e))
        if dirty or set(new) != set(old):
            try:
                fsutil.atomic_write_text(folder / INDEX_FILE, json.dumps(
                    {"header": _index_header(), "reviews": new}, separators=(",", ":")), tries=3)
            except (OSError, ValueError, TypeError):
                pass   # a read-only or locked library: the rows are still right, just not remembered
            # entries just worked out are the same objects as in `new`: hand out copies, so callers that
            # change rows (PBs, combo keys) never touch what a later scan in this call might reuse
            out = [(n, json.loads(json.dumps(e))) for n, e in out]
    return out


def scan_key(folder) -> tuple:
    """A cheap fingerprint of the library's reviews (names, sizes, mtimes of review.json and result.json):
    equal fingerprints mean list_reviews would return the same rows. Reads no review."""
    base = Path(folder) / "reviews"
    out = []
    if base.is_dir():
        for p in sorted(base.glob("*/review.json")):
            out.append((p.parent.name, tuple(_stat_key(p) or ()), tuple(_stat_key(p.parent / "result.json") or ())))
    return (str(folder), tuple(out))


def review_summaries(folder) -> list[dict]:
    """[{"dir", "track", "car", "started_at", "session", "best_t", "n_laps", "track_id", "car_id",
    "clean_best"}] for every readable review, raw values from review.json (meta, combo_ids, best.t, len(laps),
    clean_best()), folder order. From the row index: no review.json is parsed unless it changed."""
    return [dict(e["sum"], dir=n) for n, e in _scan(folder) if isinstance(e.get("sum"), dict)]


def list_reviews(folder, _pbs: bool = True, include_practice: bool = False) -> list[dict]:
    """All reviews in the library folder as row dicts, newest first (practice and qualifying rows included:
    filter with race_rows / is_race). Unreadable files are skipped.
    Rows come from the row index (.rows.json), worked out again only for reviews that changed.
    row["pb"]: PB status counting race sessions only (2.7 rule, module docstring), or every session when
    include_practice=True. row["pb_all"]: always the every-session version, for the library page's toggle."""
    rows = [e["row"] for _, e in _scan(folder) if e.get("row")]
    _unify_combo_keys(rows)
    if _pbs:
        _apply_pbs(rows, include_practice=include_practice)
        _apply_pbs(rows, include_practice=True, key="pb_all")
    rows.sort(key=lambda r: (r["date"], r["started_at"], r["dir"]), reverse=True)
    return rows


def scan_count(folder) -> int:
    """Races with a saved replay scan in the library (the Racing page's "replay scans")."""
    try:
        from . import racecap_link, replay_scan
        root = replay_scan.scans_root(folder)
        n = 0
        for _sub, _slug, d in racecap_link.race_dirs(Path(root)) if Path(root).is_dir() else []:
            try:
                if (d / "scan").is_dir() and any((d / "scan").iterdir()):
                    n += 1
            except OSError:
                continue
        return n
    except Exception:  # noqa: BLE001
        return 0


def counts(folder, rows: list | None = None) -> dict:
    """2.18: one count of the saved reviews for every page, so they all start from the same numbers.
    saved: reviews in the window's Saved reviews list; with_laps: those with a timed lap (the library rows the
    Progress, Season, Skills and Goals tabs use); races / practice: with_laps split by session; no_laps: saved
    but without a timed lap (counted nowhere else); scanned: race rows with a replay scan; scans: races with a
    saved replay scan file; skills: race rows with skill scores. Pages that use a part of these say which part."""
    folder = Path(folder)
    sums = [s for s in review_summaries(folder) if s.get("n_laps") is not None]
    rows = list_reviews(folder, _pbs=False) if rows is None else rows
    races = race_rows(rows)
    try:
        from . import profile
        scanned = len(profile.scanned_rows(races)[0])
        skilled = sum(1 for r in races if profile._usable(r) is not None)
    except Exception:  # noqa: BLE001
        scanned = skilled = 0
    return {"saved": len(sums), "with_laps": len(rows), "races": len(races), "practice": len(rows) - len(races),
            "no_laps": max(0, len(sums) - len(rows)), "scanned": scanned, "scans": scan_count(folder),
            "skills": skilled}


def _atomic_write(path: Path, text: str) -> None:
    import os
    import tempfile
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        fsutil.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def rebuild(folder, rows: list | None = None) -> Path:
    """Write <folder>/index.html: race rows + goals progress + season summary, all embedded.
    rows: list_reviews(folder) output already in hand (with PBs); None reads them (cheap: row index)."""
    from datetime import datetime, timezone
    from . import goals
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    rows = list_reviews(folder) if rows is None else rows
    races = race_rows(rows)   # 2.7: goals, the season summary and the skills profile count race sessions only
    try:
        gp = goals.progress(folder, rows=races)
    except Exception:  # a damaged goals.json must never stop the library page
        gp = []
    try:
        from . import profile
        sk = profile.build(races)
    except Exception as e:  # a skills problem must never stop the library page
        import logging
        logging.getLogger("debrief.library").exception("Skill profile failed: %s", e)
        sk = None
    try:
        from . import discord
        digest = discord.digest(rows)
    except Exception as e:  # a digest problem must never stop the library page
        import logging
        logging.getLogger("debrief.library").exception("Weekly digest failed: %s", e)
        digest = None
    extra = {"goals": gp, "season": season_summary(rows), "skills": sk, "digest": digest,
             "sessions_rule": "Personal bests, progress, weak corners, the season summary, goals and the skills "
                              "profile count race sessions only; practice and qualifying reviews are listed but "
                              "don't set or compare against a personal best unless you tick \"Include practice "
                              "and qualifying\".",
             "n_practice": len(rows) - len(races),
             "counts": counts(folder, rows),
             "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    html = report.render_library(rows, extra)
    out = folder / "index.html"
    _atomic_write(out, html)
    return out
