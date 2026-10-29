"""iRacing season schedules for the Prepare page.

iRacing's data API is closed to new apps, so the schedule comes from iRacing's own season PDF:
either the copy you downloaded (Load PDF...) or the current one fetched on a click from iRacing's
public address. Debrief reads it on this PC and never shares it anywhere.

    from_pdf(path)        -> schedule dict (raises ScheduleError with a friendly message)
    import_pdf(path)      parse, check, save as <data>/schedules/<year>-s<n>.json (+ a copy of the PDF)
    download_official()   fetch the current season PDF to a temp file (one request to iRacing)
    load_current()        the schedule for today (or the newest one saved), or None

Schedule shape (schema 1):
    {"schema": 1, "season": "2026 Season 4", "year": 2026, "quarter": 4,
     "weeks": [{"week": 1, "start": "2026-09-15"}, ...],
     "source": {"kind": "pdf", "file": "...", "imported": "...Z", "sha256": "..."},
     "series": [{"id": "global-mazda-mx-5-fanatec-cup", "name": "...", "category": "Sports Car",
                 "license": "R 4.0 --> A 4.0", "cars": ["Global Mazda MX-5 Cup"], "classes": ["MX-5"],
                 "multiclass": false, "setup": "fixed" | "open" | null, "frequency": "...",
                 "weeks": [{"week": 1, "start": "2026-09-15", "track": "Okayama ... - Short",
                            "race_time": "2026-09-19 15:40", "length": "15 laps", "conditions": "..."}]}]}
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SCHEMA = 1
OFFICIAL_URL = "https://members-assets.iracing.com/public/schedulepdf/SeasonSchedule.pdf"
MAX_PDF_BYTES = 40 * 1024 * 1024
MIN_SERIES = 30          # a real season has ~150; fewer means the PDF wasn't read properly


class ScheduleError(Exception):
    """A problem worth showing to the user as-is."""


# ---------------------------------------------------------------- names and classes
_CLASS_RULES = [  # (class, regex on the car name), most specific first
    ("GTP", r"\bGTP\b|\bLMDh\b|\bHybrid V8\b|\bARX-06\b|\b963\b|\bV-Series\.R\b"),
    ("LMP2", r"\bLMP2\b|\bDallara P217\b|\bORECA 07\b"),
    ("LMP3", r"\bLMP3\b|\bLigier JS P320\b"),
    ("GTE", r"\bGTE\b|\bGTLM\b|\bRSR\b"),
    ("GT3", r"\bGT3\b(?! Cup)|\bGT3 R\b|\bEVO II\b|\bM4 GT3\b"),
    ("Porsche Cup", r"\bGT3 Cup\b|\bCup \(992\)|\bPorsche 911 GT3 Cup\b"),
    ("GT4", r"\bGT4\b"),
    ("TCR", r"\bTCR\b"),
    ("MX-5", r"\bMX-5\b"),
    ("GR86", r"\bGR86\b"),
    ("F4", r"\bFIA F4\b|\bF4\b"),
    ("Super Formula", r"\bSuper Formula\b|\bSF23\b"),
]


def class_of(car_name) -> str | None:
    """The racing class a car belongs to, from its name, or None if it isn't one Debrief knows."""
    s = str(car_name or "")
    for cls, rx in _CLASS_RULES:
        if re.search(rx, s, re.I):
            return cls
    return None


def class_lookup(sched):
    """A car-name -> class function for reviews. Works without a schedule too (names carry the class)."""
    return class_of


_FOLD = str.maketrans({"ß": "ss", "ø": "o", "Ø": "O", "ł": "l", "Ł": "L", "đ": "d", "Đ": "D", "æ": "ae",
                       "Æ": "AE", "œ": "oe", "Œ": "OE", "þ": "th", "ı": "i"})


def fold(s) -> str:
    """Accents folded to plain letters ('Nürburgring' -> 'Nurburgring'), so names match however they're spelled."""
    import unicodedata
    s = unicodedata.normalize("NFKD", str(s or "").translate(_FOLD))
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def norm(s) -> str:
    s = fold(s).lower().replace("&", " and ")
    s = re.sub(r"[®™]", "", s)
    s = re.sub(r"[^a-z0-9.]+", " ", s)
    s = re.sub(r"(?<![0-9])\.|\.(?![0-9])", " ", s)   # keep "2.4" but not "intl." or "V-Series.R"
    return " ".join(s.split())


# Matching is strict on purpose: a wrong match (history from another layout or car) is worse than none.
# Venue names must be the same after light clean-up; layouts must be the same after abbreviations are
# expanded. Only one side naming no layout at all is allowed, and only against a plain/full/oval layout.
_VENUE_STOP = {"the", "of", "and", "at"}
_CONFIG_STOP = {"the", "of", "and", "course", "layout", "config", "configuration"}
_ABBREV = {"intl": "international", "int": "international", "gp": "grand prix", "nat": "national",
           "natl": "national", "ctr": "center", "mt": "mount"}
_DEFAULT_LAYOUTS = ({"full"}, {"default"}, {"oval"})
_YEAR = re.compile(r"(19|20)\d\d")


def _words(s, stop) -> list:
    out = []
    for t in norm(s).split():
        for x in _ABBREV.get(t, t).split():
            if x not in stop and not _YEAR.fullmatch(x):
                out.append(x)
    return out


def _tokens(s) -> set:
    return set(_words(s, _VENUE_STOP))


def split_track(name) -> tuple[str, str]:
    """'Charlotte Motor Speedway - Roval 2025' or 'Charlotte Motor Speedway (Roval 2025)' -> (venue, config)."""
    s = " ".join(str(name or "").split())
    m = re.match(r"^(.*?)\s*\((.*)\)\s*$", s) or re.match(r"^(.*?)\s+[-–]\s+(.*)$", s)
    return (m.group(1), m.group(2)) if m else (s, "")


# generic words that some sources add to a venue's name and others leave out
# ('Mount Panorama Circuit' and 'Mount Panorama Motor Racing Circuit')
_VENUE_FILLER = {"motor", "racing", "circuit", "raceway", "racecourse", "motorsport", "motorsports"}


def _layout(name):
    venue, config = split_track(name)
    v = tuple(_words(venue, _VENUE_STOP))
    core = tuple(w for w in v if w not in _VENUE_FILLER)
    v = core or v
    c = set(_words(config, _CONFIG_STOP))
    if c and c <= set(_words(venue, _CONFIG_STOP)):
        # 'Summit Point Raceway - Summit Point Raceway', 'Sebring International Raceway - International':
        # the layout is just (part of) the venue's own name, i.e. its default layout
        c = set()
    return v, c


def same_track(a, b) -> bool:
    """Do two track names (Garage 61 style or schedule style) mean the same layout? Strict: the venue must
    be the same name and the layout the same words ('Boot' is not 'Classic Boot', 'Oval' is not 'Grand Prix')."""
    va, ca = _layout(a)
    vb, cb = _layout(b)
    if not va or va != vb:
        return False
    if ca and cb:
        return ca == cb
    other = ca or cb
    return not other or any(other == d for d in _DEFAULT_LAYOUTS)


_CAR_STOP = {"the", "and"}
_CLASS_ENTRY = {"class", "classes", "cars", "car", "all", "any"}


def _class_entry(s) -> str | None:
    """'GT3 Class' or 'GTP class cars' in a schedule's car list means any car of that class."""
    toks = set(_words(s, _CAR_STOP))
    if "class" not in toks and "classes" not in toks:
        return None
    rest = toks - _CLASS_ENTRY
    for cls, _ in _CLASS_RULES:
        if rest and rest == set(_words(cls, _CAR_STOP)):
            return cls
    return None


def same_car(a, b) -> bool:
    """Do two car names mean the same car? Strict: the same words (years aside), never two different classes
    ('Porsche 911 GT3 Cup' is not the 'GT3 R', 'BMW M4 GT3' is not the 'M4 GT3 EVO')."""
    for x, y in ((a, b), (b, a)):
        cls = _class_entry(y)
        if cls:
            return class_of(x) == cls
    ka, kb = class_of(a), class_of(b)
    if ka and kb and ka != kb:
        return False
    ta, tb = _words(a, _CAR_STOP), _words(b, _CAR_STOP)
    if not ta or not tb:
        return False
    if sorted(ta) == sorted(tb):
        return True
    # words some sources add or leave out: 'Global Mazda MX-5 Cup', 'Porsche 963 GTP' (its own class)
    cls = ka or kb
    benign = {"global", "fanatec"} | (set(_words(cls, _CAR_STOP)) if cls else set())
    sa, sb = set(ta), set(tb)
    extra = sa ^ sb
    return bool(sa & sb - benign) and extra <= benign


class Matcher:
    """Matches synced sessions to schedule tracks and cars, by Garage 61 id where the cached track and car
    lists name exactly one id for the schedule's name, by (strict) name otherwise. No requests."""

    def __init__(self, tracks=None, cars=None, name_of=None):
        self._tracks = [t for t in tracks or [] if isinstance(t, dict) and t.get("id") is not None]
        self._cars = [c for c in cars or [] if isinstance(c, dict) and c.get("id") is not None]
        self._name = name_of or _item_name
        self._memo = {}

    def _ids(self, kind, name):
        k = (kind, name)
        if k not in self._memo:
            items, same = (self._tracks, same_track) if kind == "track" else (self._cars, same_car)
            hits = [i for i in items if same(self._name(i), name)]
            exact = {_int(i.get("id")) for i in hits if norm(self._name(i)) == norm(name)} - {None}
            self._memo[k] = (exact, {_int(i.get("id")) for i in hits} - {None})
        return self._memo[k]

    def _one(self, kind, name):
        exact, ids = self._ids(kind, name)
        if len(exact) == 1:
            return next(iter(exact))
        return next(iter(ids)) if len(ids) == 1 else None

    def track_id(self, name):
        """The one Garage 61 track id for a schedule track name, or None (unknown or ambiguous)."""
        return self._one("track", name)

    def car_id(self, name):
        """The one Garage 61 car id for a schedule car name, or None (unknown or ambiguous)."""
        return None if _class_entry(name) else self._one("car", name)

    def _match(self, kind, session, sched_name, same):
        sid = _int(session.get(kind + "_id"))
        _, ids = self._ids(kind, sched_name)
        if sid is not None and ids:
            return sid in ids       # every id here already passed the strict name rules
        return same(session.get(kind), sched_name)

    def track(self, session, sched_track) -> bool:
        return self._match("track", session, sched_track, same_track)

    def car(self, session, sched_car) -> bool:
        if _class_entry(sched_car):
            return same_car(session.get("car"), sched_car)
        return self._match("car", session, sched_car, same_car)


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _item_name(it) -> str:
    nm = str(it.get("name") or "")
    v = it.get("variant")
    return nm + (f" ({v})" if v else "")


def slug(s) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", str(s or "").lower())).strip("-")[:80] or "series"


# ---------------------------------------------------------------- PDF parsing
_SEASON = re.compile(r"\b(20\d\d)\s+Season\s*(\d)\b", re.I)
_CATEGORY = re.compile(r"\(\s*(OVAL|SPORTS\s+CAR|FORMULA\s+CAR|DIRT\s+OVAL|DIRT\s+ROAD|UNRANKED)\s*\)", re.I)
_WEEK = re.compile(r"^Week\s+(\d{1,2})\s*\((\d{4}-\d{2}-\d{2})\)\s*(.*)$", re.I)
_RACE_AT = re.compile(r"\((\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})(?:\s+(\d+x))?\)\s*(.*)$")
# a race length: "15 laps", "20 mins", "6 hours", "2.4 hrs", "2 hr 40 min", followed by a comma or the line end
_LENGTH = re.compile(r"(?<![\d.])(?:(\d+(?:\.\d+)?)\s*(?:hours?|hrs?|h)\.?(?:\s*(\d+)\s*(?:minutes?|mins?|m)\.?)?"
                     r"|(\d+)\s*(laps?|minutes?|mins?)\.?)(?=\s*(?:[,;]|$))", re.I)
_LENGTH_END = re.compile(_LENGTH.pattern[:-len(r"(?=\s*(?:[,;]|$))")] + r"\s*$", re.I)
_LICENSE = re.compile(r"-->|\b[RDCBAP]\s*\d\.\d\b.*\b[RDCBAP]\s*\d\.\d\b")
_FREQ = re.compile(r"^(Races?\s+every|Race\s+times|Timeslots|Races\s+at|Every\s+\d)", re.I)
_RULES = re.compile(r"^(Min\s+entries|Drops?:|Penalty|Incident|No\s+incident|Fixed\s+setup|Open\s+setup|Split\s+at)", re.I)
_PAGE = re.compile(r"^(page\s*\d+(\s*(of|/)\s*\d+)?|\d+\s*(of|/)\s*\d+|\d{1,3})$", re.I)
_FURNITURE = re.compile(r"\b(schedule|page\s*\d+)\b", re.I)
_DATE_BITS = re.compile(r"\(\s*\d{4}-|\b\d{4}-\d{2}-\d{2}\b")
MAX_WEEK_LINES = 3        # a week line plus at most 2 wrapped lines
MAX_CAR_LINES = 3


def _pdf_lines(path) -> list[str]:
    try:
        from pypdf import PdfReader
    except ImportError as e:  # pragma: no cover  (bundled with the app)
        raise ScheduleError("Debrief's PDF reader is missing. Reinstall Debrief.") from e
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            raise ScheduleError("That PDF is locked, so Debrief can't read it.")
        pages = []
        for page in reader.pages:
            txt = page.extract_text() or ""
            pages.append([x for x in (" ".join(l.split()) for l in txt.splitlines()) if x])
        furniture = _page_furniture(pages)
        lines = [l for pl in pages for l in pl if l not in furniture]
    except ScheduleError:
        raise
    except Exception as e:  # noqa: BLE001  (pypdf raises many kinds of errors for a bad file)
        raise ScheduleError("That file couldn't be read as a PDF. Pick iRacing's season schedule PDF.") from e
    return [l for l in lines if l]


def _page_furniture(pages: list[list[str]]) -> set:
    """Lines repeated at the top or bottom of most pages (running headers and footers)."""
    if len(pages) < 3:
        return set()
    seen: dict = {}
    for lines in pages:
        for l in set(lines[:2] + lines[-2:]):
            if not _WEEK.match(l):
                seen[l] = seen.get(l, 0) + 1
    return {l for l, n in seen.items() if n >= max(3, 0.6 * len(pages))}


def _is_furniture(line: str, sm) -> bool:
    """A running header/footer: 'iRacing 2026 Season 4 Schedule', '2026 Season 4 Schedule - Page 2', 'Page 3'."""
    if _PAGE.match(line):
        return True
    if sm is None:
        return False
    name = line[:sm.start()].strip(" -–:|").lower()
    # 'iRacing Porsche Cup - 2026 Season 4' is a series; 'iRacing 2026 Season 4' alone is the header
    return bool(_FURNITURE.search(line)) or name in ("iracing", "iracing.com") or name.startswith(("schedule", "page"))


def _fix_wraps(text: str) -> str:
    """Join a race-time bracket split across lines: '(2026-10- 17 15:40 1x)' -> '(2026-10-17 15:40 1x)'."""
    return re.sub(r"\((\d{4}-(?:\d{1,2}-?)?)\s+(?=\d)", r"(\1", " ".join(text.split()))


def _week_done(text: str) -> bool:
    m = _RACE_AT.search(_fix_wraps(text))
    return bool(m and _LENGTH.search(m.group(4)))


def parse_lines(lines: list[str]) -> dict:
    """Turn the PDF's text lines into a schedule dict (not yet checked; see check())."""
    season = None
    category = None
    series: list[dict] = []
    cur = None
    pending_week = None   # a week line whose track name wraps onto the next line(s)
    loose = None          # (line, where) for the line just before: a possible wrapped series title
    diag = {"skipped_lines": 0, "headers_removed": 0}

    def finish_week():
        nonlocal pending_week
        if pending_week is not None and cur is not None:
            cur["weeks"].append(_week_from(pending_week))
        pending_week = None

    def start_series(name):
        nonlocal cur
        finish_week()
        cur = {"name": name, "category": category, "cars": [], "license": None, "frequency": None,
               "rules": [], "weeks": [], "_stage": "cars", "_car_lines": 0}
        series.append(cur)

    for line in lines:
        prev_loose, loose = loose, None
        sm = _SEASON.search(line)
        wm = _WEEK.match(line)
        if not wm and _is_furniture(line, sm):
            if sm:
                season = season or f"{sm.group(1)} Season {sm.group(2)}"
            diag["headers_removed"] += 1
            loose = prev_loose   # a header between a wrapped title's two lines doesn't break it
            continue
        m = _CATEGORY.search(line)
        if m and len(line) < 80 and not wm:
            finish_week()
            category = " ".join(w.capitalize() for w in m.group(1).split())
            continue
        if wm:
            finish_week()
            if cur is not None:
                pending_week = {"week": int(wm.group(1)), "start": wm.group(2), "text": wm.group(3), "lines": 1}
                if _week_done(pending_week["text"]):
                    finish_week()
            else:
                diag["skipped_lines"] += 1
            continue
        if sm:
            season = season or f"{sm.group(1)} Season {sm.group(2)}"
            name = line[:sm.start()].strip(" -–:|")
            if not name and prev_loose:
                # the title wrapped: 'Production Car Challenge -' / '2026 Season 4'
                text, where = prev_loose
                if where == "week" and pending_week is not None:
                    pending_week["text"] = pending_week["text"][:-len(text)].rstrip()
                elif where == "cars" and cur is not None and cur["cars"]:
                    del cur["cars"][-text.count(",") - 1:]
                    cur["_car_lines"] -= 1
                name = text.strip(" -–:|")
            if name and len(name) <= 120:
                start_series(name)
            else:
                finish_week()
                diag["skipped_lines"] += 1
            continue
        if _LICENSE.search(line) or _FREQ.match(line) or _RULES.match(line):
            finish_week()   # never part of a track name
        if pending_week is not None:
            if pending_week["lines"] < MAX_WEEK_LINES:
                pending_week["text"] += " " + line
                pending_week["lines"] += 1
                loose = (" " + line, "week")
                if _week_done(pending_week["text"]) or (not _RACE_AT.search(_fix_wraps(pending_week["text"]))
                                                         and _LENGTH_END.search(line)):
                    finish_week()
                continue
            finish_week()
        if cur is None:
            diag["skipped_lines"] += 1
            loose = (line, "none")
            continue
        if cur["_stage"] == "cars":
            if _LICENSE.search(line):
                cur["license"] = line
                cur["_stage"] = "info"
                continue
            if not (_FREQ.match(line) or _RULES.match(line)) and cur["_car_lines"] < MAX_CAR_LINES and not cur["weeks"]:
                cur["cars"].extend(c.strip() for c in re.split(r",\s*(?![^()]*\))", line) if c.strip())
                cur["_car_lines"] += 1
                loose = (line, "cars")
                continue
            cur["_stage"] = "info"
        if _FREQ.match(line) and not cur["frequency"]:
            cur["frequency"] = line
        elif _RULES.match(line):
            cur["rules"].append(line)
        else:
            diag["skipped_lines"] += 1
            loose = (line, "info")
    finish_week()

    out = []
    seen = {}
    for s in series:
        if not s["weeks"]:
            continue
        s.pop("_stage", None)
        s.pop("_car_lines", None)
        rules = " ".join(s.pop("rules")).lower()
        name_l = s["name"].lower()
        s["setup"] = "fixed" if ("fixed" in name_l or "fixed setup" in rules) else ("open" if "open setup" in rules or "open" in name_l.split() else None)
        s["classes"] = sorted({c for c in (class_of(x) for x in s["cars"]) if c})
        s["multiclass"] = len(s["classes"]) >= 2
        base = slug(s["name"])
        n = seen.get(base, 0)
        seen[base] = n + 1
        s["id"] = base if n == 0 else f"{base}-{n + 1}"
        out.append(s)
    year = quarter = None
    if season:
        m = _SEASON.search(season)
        year, quarter = int(m.group(1)), int(m.group(2))
    diag["weeks_without_race_time"] = sum(1 for s in out for w in s["weeks"] if not w.get("race_time"))
    diag["weeks_without_length"] = sum(1 for s in out for w in s["weeks"] if not w.get("length"))
    diag["series_without_license"] = sum(1 for s in out if not s.get("license"))
    return {"schema": SCHEMA, "season": season, "year": year, "quarter": quarter,
            "weeks": _season_weeks(out), "series": out, "diagnostics": diag}


def _length_text(lm) -> str:
    if lm.group(3):
        n, unit = lm.group(3), lm.group(4).lower()
        if unit.startswith("lap"):
            return "1 lap" if n == "1" else f"{n} laps"
        return f"{n} mins"
    h, mins = lm.group(1), lm.group(2)
    out = f"{h} hour" + ("" if h == "1" else "s")
    return out + (f" {mins} mins" if mins and mins != "0" else "")


def _week_from(w: dict) -> dict:
    text = _fix_wraps(w["text"])
    out = {"week": w["week"], "start": w["start"], "track": text, "race_time": None, "length": None, "conditions": None}
    m = _RACE_AT.search(text)
    rest = ""
    if m:
        out["track"] = text[:m.start()].strip(" ,")
        out["race_time"] = f"{m.group(1)} {m.group(2)}"
        rest = m.group(4)
        found = list(_LENGTH.finditer(rest))
        if found:
            lm = found[-1]
            out["length"] = _length_text(lm)
            rest = (rest[:lm.start()] + rest[lm.end():])
    else:
        lm = _LENGTH_END.search(text)
        if lm:
            out["length"] = _length_text(lm)
            out["track"] = text[:lm.start()].strip(" ,.")
    rest = re.sub(r"\s*,\s*,\s*", ", ", rest).strip(" ,.;")
    out["conditions"] = rest or None
    out["track"] = out["track"][:160]
    return out


def _season_weeks(series: list) -> list:
    """The season's week start dates: the most common date for each week number."""
    votes: dict = {}
    for s in series:
        for w in s["weeks"]:
            votes.setdefault(w["week"], {}).setdefault(w["start"], 0)
            votes[w["week"]][w["start"]] += 1
    return [{"week": n, "start": max(d.items(), key=lambda kv: kv[1])[0]} for n, d in sorted(votes.items())]


def _odd_track(t: str) -> str | None:
    """Why a parsed track name can't be a real track name (it swallowed other text), or None."""
    if len(t) > 100:
        return "is too long"
    if _SEASON.search(t) or re.search(r"\bweek\s+\d", t, re.I) or re.search(r"\bpage\s*\d", t, re.I):
        return "contains schedule text"
    if _DATE_BITS.search(t) or "-->" in t or "°" in t or _LICENSE.search(t):
        return "contains dates or race details"
    return None


def check(sched: dict) -> list[str]:
    """Problems that mean the PDF wasn't read properly (empty list = fine). Strict on purpose: a schedule
    that was read wrong (two series glued together, a series swallowed by the one before) is rejected
    rather than saved."""
    probs = []
    if not sched.get("season"):
        probs.append("couldn't find the season name")
    all_series = sched.get("series") or []
    n = len(all_series)
    if n < MIN_SERIES:
        probs.append(f"only found {n} series")
    weeks = sched.get("weeks") or []
    if not 4 <= len(weeks) <= 14:
        probs.append(f"found {len(weeks)} race weeks")
    starts = [w["start"] for w in weeks]
    if starts != sorted(starts):
        probs.append("the week dates are out of order")
    nums = [w["week"] for w in weeks]
    if nums != sorted(set(nums)):
        probs.append("the week numbers are out of order")
    merged, odd = [], []
    max_weeks = max(len(weeks), 1)
    for s in all_series:
        wn = [w["week"] for w in s["weeks"]]
        if len(wn) != len(set(wn)) or wn != sorted(wn) or len(wn) > max_weeks or len(wn) > 14:
            merged.append(s["name"])
        for w in s["weeks"]:
            why = _odd_track(str(w.get("track") or ""))
            if why:
                odd.append(f"week {w['week']} of {s['name']}")
    if merged:
        probs.append(f"{len(merged)} series look merged with another ({', '.join(merged[:3])})")
    if odd:
        probs.append(f"{len(odd)} track names don't look right ({', '.join(odd[:3])})")
    total = sum(len(s["weeks"]) for s in all_series)
    blank = sum(1 for s in all_series for w in s["weeks"] if not w.get("track"))
    if blank and blank > max(2, 0.05 * total):
        probs.append(f"{blank} weeks have no track")
    if n and sum(1 for s in all_series if s.get("license")) < n / 2:
        probs.append("most series have no licence line")
    return probs


def warnings(sched: dict) -> list[str]:
    """Things that look odd but don't stop the schedule loading (shown after an import)."""
    out = []
    blank = sum(1 for s in sched.get("series") or [] for w in s["weeks"] if not w.get("track"))
    if blank:
        out.append(f"{blank} week{'s' if blank != 1 else ''} have no track yet")
    d = sched.get("diagnostics") or {}
    if d.get("weeks_without_race_time"):
        out.append(f"{d['weeks_without_race_time']} weeks have no race time")
    return out


def from_pdf(path) -> dict:
    sched = parse_lines(_pdf_lines(path))
    probs = check(sched)
    if probs:
        raise ScheduleError("That doesn't look like iRacing's season schedule PDF (" + "; ".join(probs) + ").")
    return sched


# ---------------------------------------------------------------- storage
def folder() -> Path:
    from . import app
    return Path(os.environ.get("DEBRIEF_SCHEDULE_DIR") or (app.HERE / "schedules"))


def _write_json(p: Path, obj) -> None:
    from . import fsutil
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=p.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        fsutil.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def import_pdf(path, kind: str = "pdf") -> dict:
    """Read a season PDF, check it and save it. Returns a short summary for the window."""
    path = Path(path)
    try:
        if path.stat().st_size > MAX_PDF_BYTES:
            raise ScheduleError("That file is too big to be the season schedule.")
    except OSError as e:
        raise ScheduleError("Debrief couldn't open that file.") from e
    sched = from_pdf(path)
    raw = path.read_bytes()
    sched["source"] = {"kind": kind, "file": path.name, "sha256": hashlib.sha256(raw).hexdigest(),
                       "imported": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")}
    name = f"{sched['year']}-s{sched['quarter']}"
    d = folder()
    d.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copyfile(path, d / f"{name}.pdf")
    except (OSError, shutil.SameFileError):
        pass
    _write_json(d / f"{name}.json", sched)
    return summary(sched)


def download_official(dest_dir=None) -> Path:
    """Fetch iRacing's current season PDF (one request to iRacing, only when you click the button)."""
    import requests
    from . import __version__
    d = Path(dest_dir or tempfile.mkdtemp(prefix="debrief-sched-"))
    d.mkdir(parents=True, exist_ok=True)
    out = d / "SeasonSchedule.pdf"
    try:
        with requests.get(OFFICIAL_URL, stream=True, timeout=60,
                          headers={"User-Agent": f"Debrief/{__version__} (personal, non-commercial)"}) as r:
            if r.status_code != 200:
                raise ScheduleError(f"iRacing's site returned {r.status_code}. Download the PDF yourself and use Load PDF.")
            n = 0
            with open(out, "wb") as f:
                for chunk in r.iter_content(64 * 1024):
                    n += len(chunk)
                    if n > MAX_PDF_BYTES:
                        raise ScheduleError("iRacing's file was too big to be the schedule.")
                    f.write(chunk)
    except requests.RequestException as e:
        raise ScheduleError("Couldn't reach iRacing's site. Check your connection, or use Load PDF.") from e
    with open(out, "rb") as f:
        if f.read(5) != b"%PDF-":
            raise ScheduleError("iRacing's site didn't send a PDF. Download it yourself and use Load PDF.")
    return out


def saved() -> list[dict]:
    """Saved seasons, newest first."""
    out = []
    for p in folder().glob("*-s*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and d.get("schema") == SCHEMA and d.get("series"):
            d["_file"] = p.name
            out.append(d)
    out.sort(key=lambda d: (d.get("year") or 0, d.get("quarter") or 0), reverse=True)
    return out


def _d(s) -> date | None:
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def season_range(sched) -> tuple[date | None, date | None]:
    starts = [_d(w["start"]) for w in sched.get("weeks") or [] if _d(w["start"])]
    if not starts:
        return None, None
    return min(starts), max(starts) + timedelta(days=7)


def load_current(today: date | None = None) -> dict | None:
    """The saved season that covers today; else the next one to start; else the newest saved."""
    today = today or datetime.now(timezone.utc).date()
    seasons = saved()
    for s in seasons:
        a, b = season_range(s)
        if a and a <= today < b:
            return s
    upcoming = [s for s in seasons if (season_range(s)[0] or date.max) > today]
    if upcoming:
        return min(upcoming, key=lambda s: season_range(s)[0])
    return seasons[0] if seasons else None


def week_on(sched, day: date) -> int | None:
    """Which race week a day falls in (weeks start on the date the schedule gives, UTC)."""
    for w in sched.get("weeks") or []:
        a = _d(w["start"])
        if a and a <= day < a + timedelta(days=7):
            return w["week"]
    return None


def summary(sched) -> dict | None:
    if not sched:
        return None
    a, b = season_range(sched)
    today = datetime.now(timezone.utc).date()
    return {"season": sched.get("season"), "series": len(sched.get("series") or []),
            "weeks": len(sched.get("weeks") or []), "first_day": a.isoformat() if a else None,
            "last_day": (b - timedelta(days=1)).isoformat() if b else None,
            "current_week": week_on(sched, today), "out_of_date": bool(b and today >= b),
            "imported": (sched.get("source") or {}).get("imported"),
            "source": (sched.get("source") or {}).get("kind"), "warnings": warnings(sched)}


def find_series(sched, series_id):
    return next((s for s in (sched or {}).get("series") or [] if s.get("id") == series_id), None)


def series_at(sched, track_name, car_name, day: date) -> list:
    """Series that raced this car at this track in the week of `day`."""
    if not sched:
        return []
    wk = week_on(sched, day)
    if wk is None:
        return []
    out = []
    for ser in sched.get("series") or []:
        w = next((w for w in ser["weeks"] if w["week"] == wk), None)
        if w and same_track(track_name, w["track"]) and any(same_car(car_name, c) for c in ser["cars"]):
            out.append(ser)
    return out


def multiclass_at(sched, track_name, car_name, day: date) -> bool | None:
    """True/False when the schedule says whether that race was multi-class, None when it can't tell
    (no schedule, or the matching series disagree)."""
    found = {bool(s.get("multiclass")) for s in series_at(sched, track_name, car_name, day)}
    return found.pop() if len(found) == 1 else None
