"""A minimal .xlsx reader (stdlib zipfile + xml.etree only) for the Track Intel importer.

Reads cell values as text: shared strings, inline strings, rich text, numbers, booleans, and dates
(numbers in a date-formatted cell become YYYY-MM-DD, or YYYY-MM-DD HH:MM with a time). Formulas give their
cached value. Nothing else (styles, merged cells, charts) is looked at.

Untrusted files: the zip is checked before anything is decompressed (size, entry count, total and per-part
uncompressed size), and an XML part with a DOCTYPE is refused, so entity tricks never reach the parser.
Parts must be UTF-8 (a UTF-16/32 part, or one declaring another encoding, is refused, so the DOCTYPE check
sees the same text the parser does), and expat itself is run with handlers that refuse any DOCTYPE or
ENTITY declaration. The whole workbook has one budget of cells and of text, so a tiny file can't fan out
into gigabytes (one string repeated in every cell, or many sheets pointing at one part).
"""
from __future__ import annotations

import io
import posixpath
import re
import zipfile
import zlib
from datetime import datetime, timedelta
from xml.etree import ElementTree as ET
from xml.parsers import expat

MAX_FILE = 5 * 1024 * 1024
MAX_ENTRIES = 2000
MAX_TOTAL = 25 * 1024 * 1024
MAX_PART = 10 * 1024 * 1024
MAX_SHEETS = 20
MAX_CELLS = 200_000          # cells kept across the whole workbook (rows x width, gaps included)
MAX_TEXT = 10_000_000        # characters of cell text kept across the whole workbook
MAX_DEPTH = 64               # real spreadsheet XML nests about 6 deep
MAX_ELEMENTS = 1_000_000     # elements in one streamed sheet part (a full 10 MB sheet is about 660k)
MAX_TREE_ELEMENTS = 400_000  # elements in a part read whole (workbook, styles, shared strings)
MAX_BOOK_ELEMENTS = 1_500_000  # elements across the whole workbook
MAX_ROW_CELLS = 16_384       # Excel's own column limit
MAX_SEEN_CELLS = 300_000     # cell elements looked at across the workbook, empty ones included
MAX_STR = 4000               # characters kept from one string (the importer keeps 2,000)
_TOO_MUCH = "That spreadsheet has more in it than Debrief can read. Keep the Track Intel sheet to one table of tips."
_ENC_OK = {"utf-8", "utf8", "us-ascii", "ascii"}
_XML_DECL = re.compile(r'^\s*<\?xml[^>]*?\bencoding\s*=\s*["\']([^"\']*)["\']')

_OLE = b"\xd0\xcf\x11\xe0"
_ZIP = b"PK\x03\x04"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_REF = re.compile(r"^([A-Za-z]{1,3})(\d+)$")
# built-in number formats that are dates/times (ECMA-376 18.8.30)
_DATE_IDS = set(range(14, 23)) | set(range(45, 48)) | {27, 28, 29, 30, 31, 32, 33, 34, 35, 36,
                                                       50, 51, 52, 53, 54, 55, 56, 57, 58}


class XlsxError(ValueError):
    """A plain message for the driver."""


_LOCAL = {}                  # tag -> local name; a workbook uses a few dozen tags, each seen up to ~1M times


def _local(tag: str) -> str:
    try:
        return _LOCAL[tag]
    except KeyError:
        local = tag.rsplit("}", 1)[-1]
        if len(_LOCAL) < 4096:   # bounded, so a hostile file's made-up tags can't grow it without limit
            _LOCAL[tag] = local
        return local


def _col(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _num_text(v: str) -> str:
    try:
        f = float(v)
    except ValueError:
        return v.strip()
    if f != f or f in (float("inf"), float("-inf")):
        return ""
    if f.is_integer() and abs(f) < 1e15:
        return str(int(f))
    return repr(round(f, 10)).rstrip("0").rstrip(".") if "e" not in repr(f) else str(f)


def _date_text(v: str, date1904: bool) -> str:
    try:
        f = float(v)
    except ValueError:
        return v.strip()
    if not (0 <= f < 2958466):   # up to 9999-12-31
        return _num_text(v)
    base = datetime(1904, 1, 1) if date1904 else datetime(1899, 12, 30)   # 1900 system incl. the leap-year bug
    try:
        dt = base + timedelta(days=f)
    except (OverflowError, ValueError):
        return _num_text(v)
    try:
        dt = dt.replace(microsecond=0) + (timedelta(seconds=1) if dt.microsecond >= 500000 else timedelta())
    except OverflowError:
        dt = dt.replace(microsecond=0)
    if f < 1 and not date1904:
        return dt.strftime("%H:%M")
    return dt.strftime("%Y-%m-%d") if (dt.hour, dt.minute, dt.second) == (0, 0, 0) else dt.strftime("%Y-%m-%d %H:%M")


class _Budget:
    """Cells and characters kept so far, across every sheet of one workbook."""

    def __init__(self):
        self.cells = 0
        self.text = 0
        self.seen = 0

    def add(self, cells=0, text=0):
        self.cells += cells
        self.text += text
        if self.cells > MAX_CELLS or self.text > MAX_TEXT:
            raise XlsxError(_TOO_MUCH)


def _refuse_dtd(*_a):
    raise XlsxError("That spreadsheet has something Debrief won't read. Save it again as .xlsx or CSV.")


def _check_xml(raw: bytes, limit: int = MAX_ELEMENTS, counter=None) -> None:
    """Refuse anything but plain UTF-8 XML with no DOCTYPE or ENTITY (M1 of the 2.4.0 review)."""
    bad = "That spreadsheet has something Debrief won't read. Save it again as .xlsx or CSV."
    body = raw[3:] if raw[:3] == b"\xef\xbb\xbf" else raw
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise XlsxError(bad)                      # UTF-16/32 (with or without a BOM) and other encodings
    if "\x00" in text:
        raise XlsxError(bad)                      # BOM-less UTF-16/32 decodes as "UTF-8" full of NULs
    m = _XML_DECL.match(text[:400])
    if m and m.group(1).strip().lower() not in _ENC_OK:
        raise XlsxError(bad)
    up = text.upper()
    if "<!DOCTYPE" in up or "<!ENTITY" in up:
        raise XlsxError(bad)
    # and the parser's own view: expat refuses a DOCTYPE or an entity however it was spelled
    p = expat.ParserCreate()
    p.StartDoctypeDeclHandler = _refuse_dtd
    p.EntityDeclHandler = _refuse_dtd
    p.UnparsedEntityDeclHandler = _refuse_dtd
    p.ExternalEntityRefHandler = lambda *a: _refuse_dtd()
    # shape limits, checked while streaming so a tiny file can't make a huge tree (2.4.0 review r2 M-r2-1)
    st = {"depth": 0, "n": 0, "row": None}
    is_row = {}     # element name -> its local name is "row" (a sheet has a handful of names, seen millions of times)

    def _start(name, _attrs):
        st["depth"] += 1
        st["n"] += 1
        if st["depth"] > MAX_DEPTH or st["n"] > limit:
            raise XlsxError(_TOO_MUCH)
        if counter is not None:
            counter[0] += 1
            if counter[0] > MAX_BOOK_ELEMENTS:
                raise XlsxError(_TOO_MUCH)
        row = is_row.get(name)
        if row is None:
            if len(is_row) < 4096:      # bounded: a hostile part can't grow the memo without limit
                row = is_row[name] = name.rsplit(":", 1)[-1] == "row"
            else:
                row = name.rsplit(":", 1)[-1] == "row"
        if row:
            st["row"] = [st["depth"], 0]
        elif st["row"] is not None and st["depth"] == st["row"][0] + 1:
            st["row"][1] += 1
            if st["row"][1] > MAX_ROW_CELLS:
                raise XlsxError(_TOO_MUCH)

    def _end(_name):
        if st["row"] is not None and st["depth"] == st["row"][0]:
            st["row"] = None
        st["depth"] -= 1

    p.StartElementHandler = _start
    p.EndElementHandler = _end
    try:
        p.Parse(raw, True)
    except expat.ExpatError:
        raise XlsxError("That spreadsheet looks damaged. Open it, save it again as .xlsx and try again.")


class _Book:
    def __init__(self, zf: zipfile.ZipFile):
        self.zf = zf
        self.names = {i.filename: i for i in zf.infolist()}
        self.lower = {n.lower(): n for n in self.names}
        self.elements = [0]     # elements seen across every part read so far

    def has(self, name: str) -> bool:
        return name.lower() in self.lower

    def xml(self, name: str):
        raw = self.raw(name, MAX_TREE_ELEMENTS)
        if raw is None:
            return None
        try:
            return ET.fromstring(raw)
        except ET.ParseError:
            raise XlsxError("That spreadsheet looks damaged. Open it, save it again as .xlsx and try again.")

    def raw(self, name: str, limit: int = MAX_ELEMENTS):
        """The checked bytes of one XML part (or None when it isn't there)."""
        real = self.lower.get(name.lower())
        if real is None:
            return None
        info = self.names[real]
        if info.file_size > MAX_PART:
            raise XlsxError("That spreadsheet is too big to read. Keep it under 5 MB with fewer rows.")
        try:
            with self.zf.open(info) as f:
                raw = f.read(MAX_PART + 1)
        except (zipfile.BadZipFile, zlib.error, EOFError, OSError, NotImplementedError, RuntimeError):
            # bad CRC (a size or content that doesn't match its header), an unknown compression, encryption
            raise XlsxError("That spreadsheet looks damaged. Open it, save it again as .xlsx and try again.")
        if len(raw) > MAX_PART:   # the header lied about the size
            raise XlsxError("That spreadsheet is too big to read. Keep it under 5 MB with fewer rows.")
        _check_xml(raw, limit, self.elements)
        return raw


def _open(src) -> zipfile.ZipFile:
    if isinstance(src, (bytes, bytearray, memoryview)):
        data = bytes(src)
    else:
        try:
            with open(src, "rb") as f:
                data = f.read(MAX_FILE + 1)
        except OSError:
            raise XlsxError("Debrief couldn't open that file.")
    if data[:4] == _OLE:
        raise XlsxError("That's an old .xls or a password-protected file. Save it as .xlsx or CSV and try again.")
    if len(data) > MAX_FILE:
        raise XlsxError("That spreadsheet is too big. Keep it under 5 MB.")
    if data[:4] != _ZIP:
        raise XlsxError("That isn't an .xlsx spreadsheet. Save it as .xlsx or CSV and try again.")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        infos = zf.infolist()
    except (zipfile.BadZipFile, ValueError, OSError, EOFError):
        raise XlsxError("That spreadsheet looks damaged. Open it, save it again as .xlsx and try again.")
    if len(infos) > MAX_ENTRIES:
        raise XlsxError("That spreadsheet has too many parts to read safely.")
    if sum(i.file_size for i in infos) > MAX_TOTAL or any(i.file_size > MAX_PART for i in infos):
        raise XlsxError("That spreadsheet is too big to read. Keep it under 5 MB with fewer rows.")
    if any(i.flag_bits & 0x1 for i in infos):
        raise XlsxError("That's an old .xls or a password-protected file. Save it as .xlsx or CSV and try again.")
    return zf


def _text_of(el) -> str:
    """Text of an <si> or <is>: a plain <t>, or rich-text runs <r><t> joined. Phonetic runs (<rPh>) skipped."""
    parts = []
    for child in el:
        tag = _local(child.tag)
        if tag == "t":
            parts.append(child.text or "")
        elif tag == "r":
            for t in child:
                if _local(t.tag) == "t":
                    parts.append(t.text or "")
    return "".join(parts)[:MAX_STR]


def _shared(book: _Book) -> list:
    root = book.xml("xl/sharedStrings.xml")
    if root is None:
        return []
    return [_text_of(si) for si in root if _local(si.tag) == "si"]


def _date_styles(book: _Book) -> set:
    """Indexes into cellXfs whose number format is a date/time."""
    root = book.xml("xl/styles.xml")
    if root is None:
        return set()
    custom = set()
    xfs = None
    for el in root:
        tag = _local(el.tag)
        if tag == "numFmts":
            for nf in el:
                code = re.sub(r'"[^"]*"|\[[^\]]*\]|\\.', "", (nf.get("formatCode") or "").lower())
                if re.search(r"[dmyhs]", code) and not re.fullmatch(r"[#0.,% ]*", code):
                    try:
                        custom.add(int(nf.get("numFmtId")))
                    except (TypeError, ValueError):
                        pass
        elif tag == "cellXfs":
            xfs = el
    out = set()
    if xfs is not None:
        for i, xf in enumerate(x for x in xfs if _local(x.tag) == "xf"):
            try:
                fid = int(xf.get("numFmtId") or 0)
            except ValueError:
                continue
            if fid in _DATE_IDS or fid in custom:
                out.add(i)
    return out


def _sheet_list(book: _Book) -> tuple[list, bool]:
    wb = book.xml("xl/workbook.xml")
    if wb is None:
        raise XlsxError("That isn't an .xlsx spreadsheet. Save it as .xlsx or CSV and try again.")
    date1904 = False
    sheets = []
    for el in wb.iter():
        tag = _local(el.tag)
        if tag == "workbookPr":
            date1904 = str(el.get("date1904") or "").lower() in ("1", "true")
        elif tag == "sheet":
            sheets.append((el.get("name") or f"Sheet{len(sheets) + 1}", el.get(f"{{{_REL_NS}}}id")))
    targets = {}
    rels = book.xml("xl/_rels/workbook.xml.rels")
    if rels is not None:
        for r in rels:
            if _local(r.tag) == "Relationship" and r.get("Id"):
                t = (r.get("Target") or "").replace("\\", "/")
                t = t.lstrip("/") if t.startswith("/") else posixpath.normpath(posixpath.join("xl", t))
                targets[r.get("Id")] = t
    out, seen = [], set()
    for i, (name, rid) in enumerate(sheets):
        path = targets.get(rid) or f"xl/worksheets/sheet{i + 1}.xml"
        if path.startswith("../") or not path.lower().endswith(".xml"):
            continue
        if path.lower() in seen:          # two sheets can't point at the same part (H3)
            continue
        seen.add(path.lower())
        out.append((name, path))
    return out[:MAX_SHEETS], date1904


def _cell_value(c, shared, dates, date1904) -> str:
    t = c.get("t") or "n"
    v = None
    is_el = None
    for child in c:
        tag = _local(child.tag)
        if tag == "v":
            v = child.text or ""
        elif tag == "is":
            is_el = child
    if t == "inlineStr":
        return _text_of(is_el) if is_el is not None else ""
    if t == "e":
        return ""
    if v is None:
        return ""
    if t == "s":
        try:
            return shared[int(v)]
        except (ValueError, IndexError):
            return ""
    if t == "b":
        return "TRUE" if v.strip() in ("1", "true") else "FALSE"
    if t in ("str", "d"):
        return v
    try:
        style = int(c.get("s") or 0)
    except ValueError:
        style = 0
    return _date_text(v, date1904) if style in dates else _num_text(v)


def _row_elements(raw: bytes):
    """Each <row> as it is parsed, cleared afterwards, so a big sheet never sits in memory as a tree."""
    try:
        in_row = 0
        for ev, el in ET.iterparse(io.BytesIO(raw), events=("start", "end")):
            is_row = _local(el.tag) == "row"
            if ev == "start":
                in_row += is_row
                continue
            if is_row:
                in_row -= 1
                yield el
                el.clear()
            elif not in_row:
                el.clear()          # anything outside a row is never needed once it has ended
    except ET.ParseError:
        raise XlsxError("That spreadsheet looks damaged. Open it, save it again as .xlsx and try again.")


def _rows(rows_iter, shared, dates, date1904, max_rows, max_cols, budget=None) -> list:
    budget = budget or _Budget()
    rows = {}
    next_r = 0
    for row in rows_iter:
        if _local(row.tag) != "row":
            continue
        try:
            r = int(row.get("r")) - 1 if row.get("r") else next_r
        except ValueError:
            r = next_r
        next_r = r + 1
        if r < 0 or r >= max_rows:
            if r >= max_rows:
                break
            continue
        cells = {}
        next_c = 0
        for c in row:
            if _local(c.tag) != "c":
                continue
            budget.seen += 1
            if budget.seen > MAX_SEEN_CELLS:
                raise XlsxError(_TOO_MUCH)
            m = _REF.match(c.get("r") or "")
            ci = _col(m.group(1)) if m else next_c
            next_c = ci + 1
            if ci >= max_cols:
                continue
            val = _cell_value(c, shared, dates, date1904)
            if val != "":
                budget.add(cells=1, text=len(val))
                cells[ci] = val
        if cells:
            rows[r] = cells
    if not rows:
        return []
    width = max(max(c) for c in rows.values()) + 1
    last = max(rows)
    filled = sum(len(c) for c in rows.values())
    budget.add(cells=(last + 1) * width - filled)      # the gaps that become ""
    return [[rows.get(r, {}).get(ci, "") for ci in range(width)] for r in range(last + 1)]


def read(path_or_bytes, max_rows=2000, max_cols=40) -> dict:
    """{"sheets": [{"name", "rows": [[str, ...]]}]}: every row the same width, gaps filled with "",
    trailing empty rows dropped. Raises XlsxError with a plain message."""
    max_rows = max(1, min(int(max_rows), 2000))
    max_cols = max(1, min(int(max_cols), 40))
    zf = _open(path_or_bytes)
    with zf:
        book = _Book(zf)
        sheets, date1904 = _sheet_list(book)
        shared = _shared(book)
        dates = _date_styles(book)
        budget = _Budget()
        budget.add(text=sum(len(x) for x in shared))
        out = []
        for name, path in sheets:
            try:
                raw = book.raw(path)
                if raw is None:
                    continue
                rows = _rows(_row_elements(raw), shared, dates, date1904, max_rows, max_cols, budget)
            except XlsxError as e:
                # a big later sheet (lap data, say) shouldn't sink the tips sheet already read: keep what fits
                if out and (str(e) == _TOO_MUCH or "too big" in str(e)):
                    break
                raise
            out.append({"name": str(name)[:100], "rows": rows})
            del raw
    if not out:
        raise XlsxError("That spreadsheet has no sheets Debrief can read.")
    return {"sheets": out}
