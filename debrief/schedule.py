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


def norm(s) -> str:
    s = str(s or "").lower().replace("&", " and ")
    s = re.sub(r"[®™]", "", s)
    s = re.sub(r"[^a-z0-9.]+", " ", s)
    return " ".join(s.split())


_STOP = {"the", "of", "and", "international", "raceway", "speedway", "circuit", "motor", "park", "race",
         "course", "motorsports", "sports", "car", "track", "grand", "prix"}


def _tokens(s) -> set:
    return {t for t in norm(s).split() if t not in _STOP and not re.fullmatch(r"(19|20)\d\d", t)}


def split_track(name) -> tuple[str, str]:
    """'Charlotte Motor Speedway - Roval 2025' or 'Charlotte Motor Speedway (Roval 2025)' -> (venue, config)."""
    s = " ".join(str(name or "").split())
    m = re.match(r"^(.*?)\s*\((.*)\)\s*$", s) or re.match(r"^(.*?)\s+[-–]\s+(.*)$", s)
    return (m.group(1), m.group(2)) if m else (s, "")


def same_track(a, b) -> bool:
    """Do two track names (Garage 61 style or schedule style) mean the same layout?"""
    va, ca = split_track(a)
    vb, cb = split_track(b)
    tva, tvb = _tokens(va), _tokens(vb)
    if not tva or not tvb or not (tva <= tvb or tvb <= tva or len(tva & tvb) >= 2):
        return False
    tca, tcb = _tokens(ca), _tokens(cb)
    if not tca or not tcb:
        # one side has no layout name: accept only when the other is a plain/full/default layout
        other = tca or tcb
        return not other or bool(other & {"full", "grand", "gp", "oval", "road", "default"})
    return tca <= tcb or tcb <= tca


def same_car(a, b) -> bool:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    return ta <= tb or tb <= ta or len(ta & tb) / max(len(ta), len(tb)) >= 0.75


def slug(s) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", str(s or "").lower())).strip("-")[:80] or "series"


# ---------------------------------------------------------------- PDF parsing
_SEASON = re.compile(r"\b(20\d\d)\s+Season\s*(\d)\b", re.I)
_CATEGORY = re.compile(r"\(\s*(OVAL|SPORTS\s+CAR|FORMULA\s+CAR|DIRT\s+OVAL|DIRT\s+ROAD|UNRANKED)\s*\)", re.I)
_WEEK = re.compile(r"^Week\s+(\d{1,2})\s*\((\d{4}-\d{2}-\d{2})\)\s*(.*)$", re.I)
_RACE_AT = re.compile(r"\((\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})(?:\s+(\d+x))?\)\s*(.*)$")
_LENGTH = re.compile(r"(\d+)\s*(laps?|mins?|minutes?)\.?\s*$", re.I)
_LICENSE = re.compile(r"-->|\b[RDCBAP]\s*\d\.\d\b.*\b[RDCBAP]\s*\d\.\d\b")
_FREQ = re.compile(r"^(Races?\s+every|Race\s+times|Timeslots|Races\s+at|Every\s+\d)", re.I)
_RULES = re.compile(r"^(Min\s+entries|Drops?:|Penalty|Incident|No\s+incident|Fixed\s+setup|Open\s+setup|Split\s+at)", re.I)


def _pdf_lines(path) -> list[str]:
    try:
        from pypdf import PdfReader
    except ImportError as e:  # pragma: no cover  (bundled with the app)
        raise ScheduleError("Debrief's PDF reader is missing. Reinstall Debrief.") from e
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            raise ScheduleError("That PDF is locked, so Debrief can't read it.")
        lines = []
        for page in reader.pages:
            txt = page.extract_text() or ""
            lines.extend(" ".join(l.split()) for l in txt.splitlines())
    except ScheduleError:
        raise
    except Exception as e:  # noqa: BLE001  (pypdf raises many kinds of errors for a bad file)
        raise ScheduleError("That file couldn't be read as a PDF. Pick iRacing's season schedule PDF.") from e
    return [l for l in lines if l]


def parse_lines(lines: list[str]) -> dict:
    """Turn the PDF's text lines into a schedule dict (not yet checked; see check())."""
    season = None
    category = None
    series: list[dict] = []
    cur = None
    pending_week = None   # a week line whose track name wraps onto the next line(s)

    def finish_week():
        nonlocal pending_week
        if pending_week is not None and cur is not None:
            cur["weeks"].append(_week_from(pending_week))
        pending_week = None

    for line in lines:
        m = _CATEGORY.search(line)
        if m and len(line) < 80 and not _WEEK.match(line):
            finish_week()
            category = " ".join(w.capitalize() for w in m.group(1).split())
            continue
        wm = _WEEK.match(line)
        if wm and cur is not None:
            finish_week()
            pending_week = {"week": int(wm.group(1)), "start": wm.group(2), "text": wm.group(3)}
            if _RACE_AT.search(pending_week["text"]) and _LENGTH.search(pending_week["text"]):
                finish_week()
            continue
        if pending_week is not None:
            if _RACE_AT.search(pending_week["text"]) and _LENGTH.search(pending_week["text"]):
                finish_week()
            else:
                pending_week["text"] += " " + line
                if _LENGTH.search(line):
                    finish_week()
                continue
        sm = _SEASON.search(line)
        if sm:
            name = line[:sm.start()].strip(" -–:|")
            season = season or f"{sm.group(1)} Season {sm.group(2)}"
            if name and len(name) <= 120 and not name.lower().startswith(("iracing", "schedule", "page")):
                finish_week()
                cur = {"name": name, "category": category, "cars": [], "license": None, "frequency": None,
                       "rules": [], "weeks": [], "_stage": "cars"}
                series.append(cur)
            continue
        if cur is None:
            continue
        if cur["_stage"] == "cars":
            if _LICENSE.search(line):
                cur["license"] = line
                cur["_stage"] = "info"
            else:
                cur["cars"].extend(c.strip() for c in re.split(r",\s*(?![^()]*\))", line) if c.strip())
            continue
        if _FREQ.match(line) and not cur["frequency"]:
            cur["frequency"] = line
        elif _RULES.match(line):
            cur["rules"].append(line)
    finish_week()

    out = []
    seen = {}
    for s in series:
        if not s["weeks"]:
            continue
        s.pop("_stage", None)
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
    return {"schema": SCHEMA, "season": season, "year": year, "quarter": quarter,
            "weeks": _season_weeks(out), "series": out}


def _week_from(w: dict) -> dict:
    text = " ".join(w["text"].split())
    out = {"week": w["week"], "start": w["start"], "track": text, "race_time": None, "length": None, "conditions": None}
    m = _RACE_AT.search(text)
    rest = ""
    if m:
        out["track"] = text[:m.start()].strip(" ,")
        out["race_time"] = f"{m.group(1)} {m.group(2)}"
        rest = m.group(4)
    lm = _LENGTH.search(rest or text)
    if lm:
        unit = lm.group(2).lower()
        out["length"] = f"{lm.group(1)} {'laps' if unit.startswith('lap') else 'mins'}"
        if lm.group(1) == "1" and unit.startswith("lap"):
            out["length"] = "1 lap"
        if not m:  # no race time in the line: the length sits at the end of the track text
            out["track"] = text[:lm.start()].strip(" ,.")
        rest = (rest[:lm.start()] if m else "").strip(" ,.")
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


def check(sched: dict) -> list[str]:
    """Problems that mean the PDF wasn't read properly (empty list = fine)."""
    probs = []
    if not sched.get("season"):
        probs.append("couldn't find the season name")
    n = len(sched.get("series") or [])
    if n < MIN_SERIES:
        probs.append(f"only found {n} series")
    weeks = sched.get("weeks") or []
    if not 8 <= len(weeks) <= 14:
        probs.append(f"found {len(weeks)} race weeks")
    starts = [w["start"] for w in weeks]
    if starts != sorted(starts):
        probs.append("the week dates are out of order")
    blank = sum(1 for s in sched.get("series") or [] for w in s["weeks"] if not w.get("track"))
    if blank:
        probs.append(f"{blank} weeks have no track")
    return probs


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
            "source": (sched.get("source") or {}).get("kind")}


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
