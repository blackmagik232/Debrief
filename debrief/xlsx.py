"""A minimal .xlsx reader (stdlib zipfile + xml.etree only) for the Track Intel importer.

Reads cell values as text: shared strings, inline strings, rich text, numbers, booleans, and dates
(numbers in a date-formatted cell become YYYY-MM-DD, or YYYY-MM-DD HH:MM with a time). Formulas give their
cached value. Nothing else (styles, merged cells, charts) is looked at.

Untrusted files: the zip is checked before anything is decompressed (size, entry count, total and per-part
uncompressed size), and an XML part with a DOCTYPE is refused, so entity tricks never reach the parser.
"""
from __future__ import annotations

import io
import posixpath
import re
import zipfile
import zlib
from datetime import datetime, timedelta
from xml.etree import ElementTree as ET

MAX_FILE = 5 * 1024 * 1024
MAX_ENTRIES = 2000
MAX_TOTAL = 25 * 1024 * 1024
MAX_PART = 10 * 1024 * 1024
MAX_SHEETS = 20

_OLE = b"\xd0\xcf\x11\xe0"
_ZIP = b"PK\x03\x04"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_REF = re.compile(r"^([A-Za-z]{1,3})(\d+)$")
# built-in number formats that are dates/times (ECMA-376 18.8.30)
_DATE_IDS = set(range(14, 23)) | set(range(45, 48)) | {27, 28, 29, 30, 31, 32, 33, 34, 35, 36,
                                                       50, 51, 52, 53, 54, 55, 56, 57, 58}


class XlsxError(ValueError):
    """A plain message for the driver."""


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


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
    dt = base + timedelta(days=f)
    dt = dt.replace(microsecond=0) + (timedelta(seconds=1) if dt.microsecond >= 500000 else timedelta())
    if f < 1 and not date1904:
        return dt.strftime("%H:%M")
    return dt.strftime("%Y-%m-%d") if (dt.hour, dt.minute, dt.second) == (0, 0, 0) else dt.strftime("%Y-%m-%d %H:%M")


class _Book:
    def __init__(self, zf: zipfile.ZipFile):
        self.zf = zf
        self.names = {i.filename: i for i in zf.infolist()}
        self.lower = {n.lower(): n for n in self.names}

    def has(self, name: str) -> bool:
        return name.lower() in self.lower

    def xml(self, name: str):
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
        if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
            raise XlsxError("That spreadsheet has something Debrief won't read. Save it again as .xlsx or CSV.")
        try:
            return ET.fromstring(raw)
        except ET.ParseError:
            raise XlsxError("That spreadsheet looks damaged. Open it, save it again as .xlsx and try again.")


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
    return "".join(parts)


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
    out = []
    for i, (name, rid) in enumerate(sheets):
        path = targets.get(rid) or f"xl/worksheets/sheet{i + 1}.xml"
        if path.startswith("../") or not path.lower().endswith(".xml"):
            continue
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


def _rows(root, shared, dates, date1904, max_rows, max_cols) -> list:
    rows = {}
    next_r = 0
    for row in root.iter():
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
            m = _REF.match(c.get("r") or "")
            ci = _col(m.group(1)) if m else next_c
            next_c = ci + 1
            if ci >= max_cols:
                continue
            val = _cell_value(c, shared, dates, date1904)
            if val != "":
                cells[ci] = val
        if cells:
            rows[r] = cells
    if not rows:
        return []
    width = max(max(c) for c in rows.values()) + 1
    last = max(rows)
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
        out = []
        for name, path in sheets:
            root = book.xml(path)
            if root is None:
                continue
            out.append({"name": str(name)[:100], "rows": _rows(root, shared, dates, date1904, max_rows, max_cols)})
    if not out:
        raise XlsxError("That spreadsheet has no sheets Debrief can read.")
    return {"sheets": out}
