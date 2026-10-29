"""Track Intel importer: turn a team spreadsheet (CSV or .xlsx) of track tips into track cards.

    parse_upload(bytes, filename)  -> sheets with a header row, the data rows and column suggestions
    preview(parsed, sheet, mapping, mode, cfg, known_tracks)   -> what each card would become (no writes)
    apply(...)                     -> saves the changed cards through notes.save (so size rules and
                                      team sharing still apply); returns counts

`mapping` is {column index: field}; fields are FIELDS below ("ignore" or a missing column = not used).
Modes: fill_empty (default: only empty fields of the card), append (add new lines to what's there),
replace (the sheet's text replaces a field; a field the sheet leaves empty is never wiped).
Importing the same sheet twice changes nothing the second time, in every mode.
Never sends a request.
"""
from __future__ import annotations

import csv
import io
import re

from . import notes, xlsx

FIELDS = ("track", "car", "lap1", "hotspots", "passing", "limits", "focus", "car_note", "ignore")
LABELS = {"track": "Track", "car": "Car", "lap1": "Lap 1 / start", "hotspots": "Crash hotspots",
          "passing": "Passing spots", "limits": "Track limits / kerbs", "focus": "Focus this week",
          "car_note": "Car note", "ignore": "Don't import"}
MODES = ("fill_empty", "append", "replace")
MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 2000
MAX_COLS = 40
MAX_CELL = 2000
JOIN = " · "

# header keywords per field. The longest matching phrase wins, so "Track limits" is limits, not track,
# and "Car note" is car_note, not car; then the earliest in the header; then this order.
SUGGEST = (
    ("track", ("track", "circuit", "venue", "layout", "config")),
    ("car", ("car", "vehicle", "class")),
    ("lap1", ("lap 1", "start", "opening", "t1", "turn 1")),
    ("hotspots", ("crash", "hotspot", "danger", "incident", "caution", "wreck")),
    ("passing", ("pass", "overtake", "defend", "attack", "passing")),
    ("limits", ("limit", "kerb", "curb", "cut", "track limits")),
    ("focus", ("focus", "work on", "practice", "goal", "notes")),
    ("car_note", ("setup", "gearing", "car note", "tyre", "tire")),
)


class IntelError(ValueError):
    """A plain message for the driver."""


def _norm_tokens(s) -> list:
    from .schedule import fold
    return re.sub(r"[^a-z0-9]+", " ", fold(str(s or "")).lower()).split()


def _phrase_at(phrase: list, words: list) -> int | None:
    """Every word of the phrase appears in order; a header word may extend a keyword
    ('overtaking' matches 'overtak(e)', 'kerbs' matches 'kerb')."""
    n = len(phrase)
    for i in range(len(words) - n + 1):
        ok = True
        for p, w in zip(phrase, words[i:i + n]):
            stem = p[:-1] if len(p) > 4 and p.endswith("e") else p
            if not (w == p or (len(p) >= 3 and w.startswith(stem) and not p.isdigit())):
                ok = False
                break
        if ok:
            return i
    return None


def suggest_field(header: str) -> list:
    """Candidate fields for one header, best first."""
    words = _norm_tokens(header)
    if not words:
        return []
    cands = []
    for order, (field, keys) in enumerate(SUGGEST):
        best = None
        for k in keys:
            ph = _norm_tokens(k)
            at = _phrase_at(ph, words)
            if at is not None:
                score = (-len(" ".join(ph)), at)
                best = score if best is None or score < best else best
        if best:
            cands.append((best, order, field))
    # the longest keyword wins; on a tie, the one that comes first in the header ('Setup notes' is a car note)
    return [f for _, _, f in sorted(cands)]


def suggest(header: list) -> dict:
    """{column index: field}. Each field is suggested once (first column wins), except focus."""
    used, out = set(), {}
    for i, h in enumerate(header):
        for f in suggest_field(h):
            if f in used and f != "focus":
                continue
            out[i] = f
            used.add(f)
            break
    return out


# ---------------------------------------------------------------- reading the upload
def _cell(v) -> str:
    s = str(v or "").replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    return s.strip()[:MAX_CELL]


def _table(name: str, rows: list) -> dict | None:
    rows = [[_cell(c) for c in r[:MAX_COLS]] for r in rows[:MAX_ROWS + 50]]
    h = next((i for i, r in enumerate(rows) if sum(1 for c in r if c) >= 2), None)
    if h is None:
        return None
    body = [r for r in rows[h + 1:] if any(r)][:MAX_ROWS]
    width = max([len(rows[h])] + [len(r) for r in body])
    header = rows[h] + [""] * (width - len(rows[h]))
    header = [c.replace("\n", " ") for c in header]
    body = [r + [""] * (width - len(r)) for r in body]
    return {"name": str(name)[:100], "header": header, "rows": body, "suggest": suggest(header)}


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def _csv_rows(text: str) -> list:
    sample = text[:20000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
        if sample.count(";") > sample.count(",") and sample.count(";") > sample.count("\t"):
            dialect = type("semi", (csv.excel,), {"delimiter": ";"})
        elif sample.count("\t") > sample.count(","):
            dialect = csv.excel_tab
    csv.field_size_limit(max(MAX_CELL * 4, 131072))
    try:
        out = []
        for i, r in enumerate(csv.reader(io.StringIO(text, newline=""), dialect)):
            if i >= MAX_ROWS + 50:
                break
            out.append(r)
        return out
    except csv.Error:
        raise IntelError("That CSV file couldn't be read. Save it again as CSV (comma separated) and try again.")


def parse_upload(data: bytes, filename: str) -> dict:
    """{"sheets": [{"name", "header": [...], "rows": [[...]], "suggest": {col_index: field}}]}.
    Sheets with no header row (no row with 2 or more filled cells) are left out. Raises ValueError (plain)."""
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise IntelError("That file is empty.")
    if len(data) > MAX_BYTES:
        raise IntelError("That file is too big. Keep it under 5 MB.")
    data = bytes(data)
    ext = str(filename or "").lower().rsplit(".", 1)[-1] if "." in str(filename or "") else ""
    if data[:4] in (b"PK\x03\x04", b"\xd0\xcf\x11\xe0") or ext in ("xlsx", "xlsm", "xls"):
        book = xlsx.read(data, max_rows=MAX_ROWS, max_cols=MAX_COLS)
        sheets = [t for t in (_table(s["name"], s["rows"]) for s in book["sheets"]) if t]
    else:
        if b"\x00" in data[:4096]:
            raise IntelError("That file isn't a spreadsheet Debrief can read. Use .xlsx or CSV.")
        name = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", str(filename or "").replace("\\", "/").rsplit("/", 1)[-1]) or "CSV"
        t = _table(name, _csv_rows(_decode(data)))
        sheets = [t] if t else []
    if not sheets:
        raise IntelError("Debrief couldn't find a header row (a row naming the columns, like Track and Notes).")
    return {"sheets": sheets}


# ---------------------------------------------------------------- mapping
def _mapping(parsed, sheet, mapping) -> tuple[dict, dict]:
    if not isinstance(parsed, dict) or not isinstance(parsed.get("sheets"), list):
        raise IntelError("Upload the sheet again.")
    if isinstance(sheet, bool) or not isinstance(sheet, int) or not 0 <= sheet < len(parsed["sheets"]):
        raise IntelError("Pick a sheet.")
    sh = parsed["sheets"][sheet]
    width = len(sh["header"])
    if not isinstance(mapping, dict) or len(mapping) > MAX_COLS:
        raise IntelError("Pick what each column holds.")
    by_field = {}
    for k, f in mapping.items():
        try:
            ci = int(k)
        except (TypeError, ValueError):
            raise IntelError("Pick what each column holds.")
        if not 0 <= ci < width:
            raise IntelError("Pick what each column holds.")
        if f in (None, "", "ignore"):
            continue
        if f not in FIELDS:
            raise IntelError("Pick what each column holds.")
        by_field.setdefault(f, []).append(ci)
    if "track" not in by_field:
        raise IntelError("Pick the column that names the track.")
    if len(by_field["track"]) > 1:
        raise IntelError("Pick just one column for the track.")
    if len(by_field.get("car", [])) > 1:
        raise IntelError("Pick just one column for the car.")
    for f in by_field:
        by_field[f].sort()
    return sh, by_field


# ---------------------------------------------------------------- text helpers
def _lines(text: str) -> list:
    return [ln.strip() for ln in str(text or "").replace("\r", "").split("\n") if ln.strip()]


def _dedupe(seq) -> list:
    seen, out = set(), []
    for x in seq:
        k = " ".join(x.split()).casefold()
        if k not in seen:
            seen.add(k)
            out.append(x)
    return out


def _fit(lines: list, field: str, warn: list, track: str) -> str:
    """Join lines within the card's limits (whole lines; a single over-long line is cut). Flags what's dropped."""
    label = notes.LABELS[field]
    if field == "focus" and len(lines) > notes.MAX_FOCUS_LINES:
        warn.append(f"{track}: {label} keeps {notes.MAX_FOCUS_LINES} lines; "
                    f"{len(lines) - notes.MAX_FOCUS_LINES} more were left out.")
        lines = lines[:notes.MAX_FOCUS_LINES]
    out = []
    for ln in lines:
        cand = "\n".join(out + [ln])
        if len(cand) <= notes.MAX_FIELD:
            out.append(ln)
            continue
        if not out:
            out.append(ln[:notes.MAX_FIELD].rstrip())
        warn.append(f"{track}: {label} was cut to {notes.MAX_FIELD} characters.")
        break
    return "\n".join(out)


def _has_line(existing: list, ln: str) -> bool:
    k = " ".join(ln.split()).casefold()
    cut = " ".join(ln[:notes.MAX_FIELD].rstrip().split()).casefold()
    return any(" ".join(e.split()).casefold() in (k, cut) for e in existing)


def _merge_field(field, existing: str, incoming: list, mode: str, warn: list, track: str) -> str:
    if not incoming:
        return existing
    if mode == "fill_empty":
        return existing if existing else _fit(incoming, field, warn, track)
    if mode == "replace":
        return _fit(incoming, field, warn, track)
    old = _lines(existing)
    new = [ln for ln in incoming if not _has_line(old, ln)]
    if not new:
        return existing
    merged = _fit(old + new, field, warn, track)
    return merged if len(_lines(merged)) >= len(old) else existing


def _merge_car(existing: str, incoming: str, mode: str) -> str:
    if not incoming:
        return existing
    if mode == "fill_empty":
        return existing or incoming
    if mode == "replace":
        return incoming
    if not existing:
        return incoming
    if incoming.casefold() in existing.casefold():
        return existing
    joined = existing + JOIN + incoming
    return joined if len(joined) <= notes.MAX_CAR_NOTE else existing


# ---------------------------------------------------------------- preview / apply
def _venue(name):
    from .schedule import _layout
    try:
        return _layout(name)[0]
    except Exception:  # noqa: BLE001
        return None


class _Resolver:
    """_resolve with the known tracks indexed by venue once (2.4.0 review L3): same_track is only true when
    the venues are equal, so only names at the same venue are compared. Answers are cached per typed name.
    Gives exactly what _resolve gives (the first match in list order)."""

    def __init__(self, known: list):
        self.known = [k for k in known if isinstance(k, str) and k.strip()]
        self.k_exact, self.k_venue = {}, {}
        for i, k in enumerate(self.known):
            self.k_exact.setdefault(k, i)
            v = _venue(k)
            if v:
                self.k_venue.setdefault(v, []).append(i)
        self.seen, self.s_exact, self.s_key, self.s_venue = [], {}, {}, {}
        self.cache = {}

    def add_seen(self, name: str) -> None:
        if name in self.s_exact:
            return
        i = len(self.seen)
        self.seen.append(name)
        self.s_exact[name] = i
        self.s_key.setdefault(notes.track_key(name), i)
        v = _venue(name)
        if v:
            self.s_venue.setdefault(v, []).append(i)

    def __call__(self, typed: str) -> tuple[str, str | None]:
        hit = self.cache.get(typed)
        if hit is not None:
            return hit
        from .schedule import same_track
        v = _venue(typed)
        cands = [self.k_exact[typed]] if typed in self.k_exact else []
        for i in self.k_venue.get(v, []) if v else []:
            try:
                if same_track(typed, self.known[i]):
                    cands.append(i)
                    break
            except Exception:  # noqa: BLE001
                continue
        if cands:
            k = self.known[min(cands)]
            out = (k, k)
        else:
            sc = [self.s_exact[typed]] if typed in self.s_exact else []
            tk = notes.track_key(typed)
            if tk in self.s_key:
                sc.append(self.s_key[tk])
            for i in self.s_venue.get(v, []) if v else []:
                if same_track(typed, self.seen[i]):
                    sc.append(i)
                    break
            out = (self.seen[min(sc)], None) if sc else (typed, None)
        self.cache[typed] = out
        return out


def _resolve(typed: str, known: list, seen: list) -> tuple[str, str | None]:
    """(name the card is saved under, matched known track or None). The simple form; preview uses
    _Resolver, which gives the same answers without comparing every row with every known track."""
    from .schedule import same_track
    for k in known:
        try:
            if isinstance(k, str) and k.strip() and (k == typed or same_track(typed, k)):
                return k, k
        except Exception:  # noqa: BLE001
            continue
    for s in seen:
        if s == typed or same_track(typed, s) or notes.track_key(s) == notes.track_key(typed):
            return s, None
    return typed, None


def preview(parsed, sheet: int, mapping: dict, mode: str, cfg, known_tracks: list) -> dict:
    """What each card would become. Writes nothing.
    {"mode", "rows", "skipped_rows", "warnings", "tracks": [{"track", "typed", "matched", "new", "existing",
     "incoming", "result", "changes", "warnings", "read_only"}]}"""
    mode = mode or "fill_empty"
    if mode not in MODES:
        raise IntelError("Pick how to import: fill empty fields, add to what's there, or replace.")
    sh, by = _mapping(parsed, sheet, mapping)
    lib = cfg["library_folder"]
    known = [k for k in dict.fromkeys(known_tracks or []) if isinstance(k, str) and k.strip()]
    tcol = by["track"][0]
    ccol = by.get("car", [None])[0]
    sheet_warn = []
    if "car_note" in by and ccol is None:
        sheet_warn.append("Car notes need a column that names the car, so they were left out.")
    groups, order = {}, []
    resolve = _Resolver(known)
    skipped = 0
    for r in sh["rows"]:
        typed = " ".join(r[tcol].split()) if tcol < len(r) else ""
        if not typed or len(typed) > 200:
            skipped += 1
            continue
        name, matched = resolve(typed)
        resolve.add_seen(name)
        key = notes.track_key(name)
        g = groups.get(key)
        if g is None:
            g = groups[key] = {"track": name, "typed": typed, "matched": matched,
                               "lines": {f: [] for f in notes.FIELDS}, "cars": {}}
            order.append(key)
        for f in notes.FIELDS:
            vals = [r[c] for c in by.get(f, []) if c < len(r) and r[c]]
            if not vals:
                continue
            if len(vals) == 1:
                g["lines"][f].extend(_lines(vals[0]))
            else:   # several columns for one field: one line per row, joined
                g["lines"][f].append(JOIN.join(" ".join(v.split()) for v in vals))
        if ccol is not None and "car_note" in by:
            car = " ".join(r[ccol].split())[:200] if ccol < len(r) else ""
            cn = [" ".join(r[c].split()) for c in by["car_note"] if c < len(r) and r[c].strip()]
            if car and cn:
                g["cars"].setdefault(car, []).extend(cn)
    tracks = []
    for key in order:
        g = groups[key]
        warn = []
        ex = notes.load(lib, g["track"])
        track = ex["track"] if notes.has_notes(ex) or ex.get("updated") else g["track"]
        ro = bool(ex.get("read_only"))
        inc_fields, res_fields = {}, {}
        for f in notes.FIELDS:
            lines = _dedupe(g["lines"][f])
            w2 = []
            inc_fields[f] = _fit(lines, f, w2, track) if lines else ""
            res_fields[f] = _merge_field(f, ex["fields"].get(f, ""), lines, mode, warn, track)
            if mode == "fill_empty" and ex["fields"].get(f):
                w2 = []   # the sheet's text isn't used for this field, so its length doesn't matter
            warn.extend(w for w in w2 if w not in warn)
        inc_cars, res_cars = {}, dict(ex.get("car_notes") or {})
        for car, vals in g["cars"].items():
            txt = JOIN.join(_dedupe(vals))
            if len(txt) > notes.MAX_CAR_NOTE:
                warn.append(f"{track}: the car note for {car} was cut to {notes.MAX_CAR_NOTE} characters.")
                txt = txt[:notes.MAX_CAR_NOTE].rstrip(" ·")
            inc_cars[car] = txt
            ek = next((k for k in res_cars if k.casefold() == car.casefold()), car)
            merged = _merge_car(res_cars.get(ek, ""), txt, mode)
            if merged:
                res_cars[ek] = merged
        if len(res_cars) > notes.MAX_CARS:
            warn.append(f"{track}: a card keeps notes for {notes.MAX_CARS} cars; the rest were left out.")
            res_cars = dict(list(res_cars.items())[:notes.MAX_CARS])
        changes = [f for f in notes.FIELDS if res_fields[f] != ex["fields"].get(f, "")]
        if res_cars != (ex.get("car_notes") or {}):
            changes.append("car_notes")
        if g["matched"] is None:
            warn.insert(0, "New track: kept as typed.")
        if ro:
            warn.append("This card was saved by a newer Debrief, so it won't be changed.")
            changes, res_fields, res_cars = [], dict(ex["fields"]), dict(ex.get("car_notes") or {})
        tracks.append({"track": track, "typed": g["typed"], "matched": g["matched"], "new": g["matched"] is None,
                       "existing": {"fields": dict(ex["fields"]), "car_notes": dict(ex.get("car_notes") or {})},
                       "incoming": {"fields": inc_fields, "car_notes": inc_cars},
                       "result": {"fields": res_fields, "car_notes": res_cars},
                       "changes": changes, "warnings": warn, "read_only": ro})
    return {"mode": mode, "rows": len(sh["rows"]) - skipped, "skipped_rows": skipped,
            "warnings": sheet_warn, "tracks": tracks}


def apply(parsed, sheet, mapping, mode, cfg, known_tracks) -> dict:
    """Save every card the preview changes, through notes.save(notes.clean(...)). Re-running it changes nothing.
    {"tracks": n, "changed": k, "skipped": s, "errors": [plain messages]}"""
    pv = preview(parsed, sheet, mapping, mode, cfg, known_tracks)
    changed, skipped, errors = 0, 0, []
    for t in pv["tracks"]:
        if not t["changes"]:
            skipped += 1
            continue
        try:
            card = notes.clean({"track": t["track"], "fields": t["result"]["fields"],
                                "car_notes": t["result"]["car_notes"]})
            notes.save(cfg["library_folder"], card, cfg)
            changed += 1
        except (ValueError, OSError) as e:
            skipped += 1
            errors.append(f"{t['track']}: {e}")
    return {"tracks": len(pv["tracks"]), "changed": changed, "skipped": skipped, "errors": errors}
