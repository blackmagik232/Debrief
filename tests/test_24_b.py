"""Debrief 2.4 workstream 24-B: team track cards, the .xlsx reader, the Track Intel importer, results fields.

    python3 tests/test_24_b.py

File system only. Never contacts Garage 61. Spreadsheets are built in the test with zipfile.
"""
from __future__ import annotations

import faulthandler
import io
import json
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
TMP = Path(tempfile.mkdtemp(prefix="debrief-24b-"))
os.environ.setdefault("DEBRIEF_SCHEDULE_DIR", str(TMP / "schedules"))
os.environ.setdefault("DEBRIEF_CACHE_DIR", str(TMP / "cache"))
os.environ.setdefault("DEBRIEF_SETTINGS", str(TMP / "settings.toml"))

from debrief import g61, intel, notes, prepare, results, xlsx  # noqa: E402

FIX = ROOT / "tests" / "fixtures_24_b"
ROVAL = "Charlotte Motor Speedway (Roval 2019)"
TOKEN = "test-token-24b"


# ================================================================ helpers
def _fresh(name) -> Path:
    d = Path(tempfile.mkdtemp(prefix=f"{name}-", dir=TMP))
    return d


def _cfg(share=True, me=True, folder=True):
    base = _fresh("cfg")
    cfg = {"library_folder": str(base / "lib"), "cache_dir": str(base / "cache"), "token": TOKEN,
           "share_my_cards": share}
    if folder:
        (base / "team").mkdir()
        cfg["team_cards_folder"] = str(base / "team")
    if me:
        ns = __import__("hashlib").sha256((g61.BASE.rstrip("/") + "/|" + TOKEN).encode()).hexdigest()[:10]
        p = Path(cfg["cache_dir"]) / ns / (__import__("hashlib").sha1(b"me").hexdigest()[:20] + ".json")
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps({"slug": "swazi-williams", "firstName": "Swazi", "lastName": "Williams"}),
                     encoding="utf-8")
    return cfg


def _card(track=ROVAL, **fields):
    return notes.clean({"track": track, "fields": fields})


def _mate(root: Path, slug, name=None, track=ROVAL, updated="2026-11-20T10:00:00Z", **over):
    d = root / notes.track_key(track)
    d.mkdir(parents=True, exist_ok=True)
    body = {"schema": 1, "kind": "debrief-track-card", "track": track,
            "author": {"slug": slug, "name": name or slug.title()},
            "fields": {"lap1": "", "hotspots": f"{slug} says T1", "passing": "", "limits": "", "focus": ""},
            "car_notes": {}, "updated": updated, "app": "Debrief 2.4.0"}
    body.update(over)
    p = d / f"{slug}.json"
    p.write_text(json.dumps(body), encoding="utf-8")
    return p


def _snapshot(root: Path) -> dict:
    out = {}
    for dp, dns, fns in os.walk(root):
        for n in dns + fns:
            p = Path(dp) / n
            st = p.lstat()
            out[str(p)] = (st.st_mtime_ns, st.st_size)
    return out


def _xlsx(sheets, shared=None, styles=None, extra=None, workbook_pr="") -> bytes:
    """sheets: [(name, sheet_xml_body)]. Returns the bytes of an .xlsx built with zipfile."""
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    rns = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        sh = "".join(f'<sheet name="{n}" sheetId="{i + 1}" r:id="rId{i + 1}"/>' for i, (n, _) in enumerate(sheets))
        z.writestr("xl/workbook.xml", f'<?xml version="1.0"?><workbook {ns} {rns}>{workbook_pr}<sheets>{sh}</sheets></workbook>')
        rels = "".join(f'<Relationship Id="rId{i + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i + 1}.xml"/>'
                       for i in range(len(sheets)))
        z.writestr("xl/_rels/workbook.xml.rels", f'<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rels}</Relationships>')
        for i, (_, body) in enumerate(sheets):
            z.writestr(f"xl/worksheets/sheet{i + 1}.xml", f'<?xml version="1.0"?><worksheet {ns}><sheetData>{body}</sheetData></worksheet>')
        if shared is not None:
            z.writestr("xl/sharedStrings.xml", f'<?xml version="1.0"?><sst {ns}>{shared}</sst>')
        if styles is not None:
            z.writestr("xl/styles.xml", f'<?xml version="1.0"?><styleSheet {ns}>{styles}</styleSheet>')
        for k, v in (extra or {}).items():
            z.writestr(k, v)
    return buf.getvalue()


def _row(r, cells):
    return f'<row r="{r}">' + "".join(cells) + "</row>"


def _s(ref, idx):
    return f'<c r="{ref}" t="s"><v>{idx}</v></c>'


def _inline(ref, text):
    return f'<c r="{ref}" t="inlineStr"><is><t>{text}</t></is></c>'


def _intel_book(rows, header=("Track", "Crash spots", "Overtaking", "Kerbs", "Focus")):
    """A one-sheet Track Intel workbook with inline strings."""
    cols = "ABCDEFGHIJ"
    body = _row(1, [_inline(f"{cols[i]}1", h) for i, h in enumerate(header)])
    for r, vals in enumerate(rows, 2):
        body += _row(r, [_inline(f"{cols[i]}{r}", v) for i, v in enumerate(vals) if v])
    return _xlsx([("Track Intel", body)])


def _raises(fn, *a, contains=None, exc=ValueError):
    try:
        fn(*a)
    except exc as e:
        if contains:
            assert contains.lower() in str(e).lower(), str(e)
        return str(e)
    raise AssertionError(f"{fn.__name__} did not raise")


# ================================================================ day-1 fixtures match the shapes
def test_fixtures_have_the_interface_shapes():
    card = json.loads((FIX / "sample_card_file.json").read_text(encoding="utf-8"))
    assert card["kind"] == "debrief-track-card" and card["schema"] == 1
    assert set(card["fields"]) == set(notes.FIELDS) and card["author"]["slug"] == "swazi-williams"
    shared = json.loads((FIX / "sample_shared_for.json").read_text(encoding="utf-8"))
    assert all(set(c) == {"author", "updated", "fields", "car_notes"} for c in shared)
    assert [c["updated"] for c in shared] == sorted([c["updated"] for c in shared], reverse=True)
    up = json.loads((FIX / "sample_intel_upload.json").read_text(encoding="utf-8"))
    assert set(up["sheets"][0]) == {"name", "header", "rows", "suggest"}
    pv = json.loads((FIX / "sample_intel_preview.json").read_text(encoding="utf-8"))
    for t in pv["tracks"]:
        assert {"track", "matched", "existing", "incoming", "result", "changes", "warnings"} <= set(t)
    ap = json.loads((FIX / "sample_intel_apply.json").read_text(encoding="utf-8"))
    assert {"tracks", "changed", "skipped"} <= set(ap)
    res = json.loads((FIX / "sample_result.json").read_text(encoding="utf-8"))
    v = results.validate(res)
    assert v["positions_lap1"] == res["positions_lap1"] and v["incident_laps"] == res["incident_laps"]
    xr = json.loads((FIX / "sample_xlsx_read.json").read_text(encoding="utf-8"))
    assert set(xr["sheets"][0]) == {"name", "rows"}


# ================================================================ team track cards
def test_author_from_cache_reads_only_the_cache():
    cfg = _cfg()
    assert notes.author_from_cache(cfg) == {"slug": "swazi-williams", "name": "Swazi Williams"}
    assert notes.author_from_cache(_cfg(me=False)) is None
    assert notes.author_from_cache({"cache_dir": str(TMP), "token": ""}) is None
    assert notes.author_from_cache({}) is None


def test_shared_root():
    cfg = _cfg()
    assert notes.shared_root(cfg) == Path(cfg["team_cards_folder"]) / "Debrief track cards"
    assert notes.shared_root(_cfg(folder=False)) is None
    assert notes.shared_root({"team_cards_folder": str(TMP / "nope")}) is None
    assert notes.shared_root(None) is None


def test_publish_writes_exactly_one_file_at_the_right_path():
    cfg = _cfg()
    team = Path(cfg["team_cards_folder"])
    saved = notes.save(cfg["library_folder"], _card(hotspots="Bus stop on lap 1"), cfg)
    assert "share_error" not in saved
    files = [p for p in team.rglob("*") if p.is_file()]
    want = team / "Debrief track cards" / notes.track_key(ROVAL) / "swazi-williams.json"
    assert files == [want], files
    d = json.loads(want.read_text(encoding="utf-8"))
    assert d["kind"] == "debrief-track-card" and d["schema"] == 1 and d["track"] == ROVAL
    assert d["author"] == {"slug": "swazi-williams", "name": "Swazi Williams"}
    assert d["fields"]["hotspots"] == "Bus stop on lap 1" and d["app"].startswith("Debrief ")
    assert d["updated"] == saved["updated"]
    assert not [p for p in want.parent.iterdir() if p.name.startswith(".tmp")]   # temp file was moved into place


def test_publish_leaves_other_authors_alone():
    cfg = _cfg()
    root = Path(cfg["team_cards_folder"]) / "Debrief track cards"
    other = _mate(root, "josh-spence")
    before = other.read_bytes(), other.stat().st_mtime_ns
    notes.save(cfg["library_folder"], _card(lap1="Stay outside"), cfg)
    assert (other.read_bytes(), other.stat().st_mtime_ns) == before


def test_sharing_off_writes_nothing():
    cfg = _cfg(share=False)
    notes.save(cfg["library_folder"], _card(lap1="x"), cfg)
    assert list(Path(cfg["team_cards_folder"]).rglob("*")) == []
    assert notes.publish(cfg, _card(lap1="x"), {"slug": "swazi-williams", "name": "S"}) is False
    assert list(Path(cfg["team_cards_folder"]).rglob("*")) == []
    # no cfg at all (2.3 callers): local save only
    notes.save(cfg["library_folder"], _card(lap1="y"))
    assert list(Path(cfg["team_cards_folder"]).rglob("*")) == []


def test_no_author_means_no_publish_and_a_plain_message():
    cfg = _cfg(me=False)
    out = notes.save(cfg["library_folder"], _card(lap1="x"), cfg)
    assert out["fields"]["lap1"] == "x"   # the local save still happened
    assert "Garage 61 connection" in out["share_error"]
    assert list(Path(cfg["team_cards_folder"]).rglob("*.json")) == []


def test_missing_team_folder_never_fails_the_local_save():
    cfg = _cfg()
    shutil.rmtree(cfg["team_cards_folder"])
    out = notes.save(cfg["library_folder"], _card(lap1="x"), cfg)
    assert out["fields"]["lap1"] == "x" and "team folder" in out["share_error"]


def test_empty_card_deletes_only_your_shared_file():
    cfg = _cfg()
    root = Path(cfg["team_cards_folder"]) / "Debrief track cards"
    notes.save(cfg["library_folder"], _card(lap1="x"), cfg)
    mine = root / notes.track_key(ROVAL) / "swazi-williams.json"
    other = _mate(root, "hayden")
    assert mine.exists()
    notes.save(cfg["library_folder"], _card(), cfg)
    assert not mine.exists() and other.exists()
    assert notes.load(cfg["library_folder"], ROVAL)["updated"] is None


def _symlink(link: Path, target: Path) -> bool:
    try:
        os.symlink(target, link)
        return True
    except (OSError, NotImplementedError, AttributeError):   # Windows without developer mode
        return False


def test_merge_read_returns_only_valid_cards_and_never_writes():
    cfg = _cfg()
    root = Path(cfg["team_cards_folder"]) / "Debrief track cards"
    d = root / notes.track_key(ROVAL)
    _mate(root, "josh-spence", "Josh Spence", updated="2026-11-21T09:00:00Z")
    _mate(root, "hayden", "Hayden", updated="2026-11-19T09:00:00Z", car_notes={"BMW M4 GT4": "Gear 3 in the hairpin"})
    # huge file
    (d / "michael-blake.json").write_text(json.dumps({"schema": 1, "kind": "debrief-track-card", "track": ROVAL,
                                                      "author": {"slug": "michael-blake", "name": "M"},
                                                      "fields": {"lap1": "x" * 20000}}), encoding="utf-8")
    # mismatched slug (a copied file)
    (d / "someone-else.json").write_text(json.dumps({"schema": 1, "kind": "debrief-track-card", "track": ROVAL,
                                                     "author": {"slug": "hayden", "name": "Hayden"},
                                                     "fields": {"lap1": "copied"}, "updated": "2026-12-01T00:00:00Z"}),
                                         encoding="utf-8")
    # OneDrive conflict copy: the name looks like a slug, the author inside doesn't match
    (d / "swazi-williams-DESKTOP-1.json").write_text(json.dumps({"schema": 1, "kind": "debrief-track-card",
                                                                "author": {"slug": "swazi-williams"}, "fields": {"lap1": "c"}}),
                                                     encoding="utf-8")
    (d / "josh-spence-desktop-1.json").write_text(json.dumps({"schema": 1, "kind": "debrief-track-card",
                                                             "author": {"slug": "josh-spence"}, "fields": {"lap1": "c"}}),
                                                  encoding="utf-8")
    # newer schema, wrong kind, broken JSON, upper-case name, a temp file
    (d / "future.json").write_text(json.dumps({"schema": 99, "kind": "debrief-track-card", "author": {"slug": "future"},
                                               "fields": {"lap1": "f"}}), encoding="utf-8")
    (d / "notacard.json").write_text(json.dumps({"schema": 1, "kind": "other", "author": {"slug": "notacard"},
                                                 "fields": {"lap1": "n"}}), encoding="utf-8")
    (d / "broken.json").write_text("{nope", encoding="utf-8")
    (d / "Upper.json").write_text(json.dumps({"schema": 1, "kind": "debrief-track-card", "author": {"slug": "Upper"},
                                              "fields": {"lap1": "u"}}), encoding="utf-8")
    (d / ".tmp-abc.json").write_text("{}", encoding="utf-8")
    # symlinks: to a valid-looking card outside the root, and a whole track folder link
    outside = _fresh("outside")
    (outside / "evil.json").write_text(json.dumps({"schema": 1, "kind": "debrief-track-card", "author": {"slug": "evil"},
                                                   "fields": {"lap1": "evil"}}), encoding="utf-8")
    linked = _symlink(d / "evil.json", outside / "evil.json")
    # your own file is skipped (yours is local)
    _mate(root, "swazi-williams")
    before = _snapshot(Path(cfg["team_cards_folder"]))
    out = notes.shared_for(cfg, "Charlotte Motor Speedway - Roval 2025", "swazi-williams")
    after = _snapshot(Path(cfg["team_cards_folder"]))
    assert before == after   # no file created, changed or removed
    assert [c["author"]["slug"] for c in out] == ["josh-spence", "hayden"], out
    assert out[0]["author"]["name"] == "Josh Spence" and out[1]["car_notes"] == {"BMW M4 GT4": "Gear 3 in the hairpin"}
    assert set(out[0]) == {"author", "updated", "fields", "car_notes"}
    if not linked:
        print("  (symlink part SKIPPED: this OS doesn't allow symlinks here)")
    # me_slug None: your own file shows like anyone else's
    assert "swazi-williams" in [c["author"]["slug"] for c in notes.shared_for(cfg, ROVAL, None)]


def test_merge_read_caps_and_length_rules():
    cfg = _cfg()
    root = Path(cfg["team_cards_folder"]) / "Debrief track cards"
    for i in range(60):
        _mate(root, f"driver-{i:02d}", updated=f"2026-11-{(i % 28) + 1:02d}T00:00:00Z")
    out = notes.shared_for(cfg, ROVAL, None)
    assert len(out) == notes.MAX_AUTHORS
    # over-long field (under 16 KB) fails notes.clean, so it's ignored rather than shown cut
    _mate(root, "long-one", fields={"lap1": "y" * 400})
    assert "long-one" not in [c["author"]["slug"] for c in notes.shared_for(cfg, ROVAL, None)]
    # html-ish text comes through as text (the pages render it with textContent)
    cfg2 = _cfg()
    root2 = Path(cfg2["team_cards_folder"]) / "Debrief track cards"
    _mate(root2, "nasty", fields={"lap1": "<img src=x onerror=alert(1)>"})
    assert notes.shared_for(cfg2, ROVAL, None)[0]["fields"]["lap1"] == "<img src=x onerror=alert(1)>"


def test_shared_for_without_folder_is_empty():
    assert notes.shared_for(_cfg(folder=False), ROVAL, None) == []
    assert notes.shared_for(_cfg(), "Nowhere Raceway", None) == []


def test_a_symlinked_track_folder_is_ignored():
    cfg = _cfg()
    root = Path(cfg["team_cards_folder"]) / "Debrief track cards"
    elsewhere = _fresh("elsewhere")
    _mate(elsewhere, "hayden")
    root.mkdir(parents=True)
    if not _symlink(root / notes.track_key(ROVAL), elsewhere / notes.track_key(ROVAL)):
        print("  SKIPPED (no symlinks on this OS)")
        return
    assert notes.shared_for(cfg, ROVAL, None) == []


# ================================================================ xlsx
def test_xlsx_shared_inline_rich_gap_bool_numbers():
    shared = ("<si><t>Track</t></si><si><t>Notes</t></si>"
              "<si><r><t>Bus </t></r><r><rPr><b/></rPr><t>stop</t></r><rPh><t>x</t></rPh></si>"
              "<si><t xml:space=\"preserve\"> Roval </t></si>")
    body = (_row(1, [_s("A1", 0), _s("C1", 1), '<c r="D1" t="str"><v>Laps</v></c>'])
            + _row(2, [_s("A2", 3), _s("C2", 2), '<c r="D2"><v>15</v></c>', '<c r="E2" t="b"><v>1</v></c>'])
            + _row(4, [_inline("A4", "Okayama"), '<c r="C4"><v>2.5</v></c>', '<c r="D4" t="e"><v>#DIV/0!</v></c>',
                       '<c r="E4" t="b"><v>0</v></c>'])
            + '<row r="7"/><row r="8"><c r="B8" t="s"/></row>')
    book = xlsx.read(_xlsx([("Intel", body)], shared=shared))
    rows = book["sheets"][0]["rows"]
    assert book["sheets"][0]["name"] == "Intel"
    assert rows == [["Track", "", "Notes", "Laps", ""],
                    [" Roval ", "", "Bus stop", "15", "TRUE"],
                    ["", "", "", "", ""],
                    ["Okayama", "", "2.5", "", "FALSE"]], rows


def test_xlsx_dates_multiple_sheets_and_path_input():
    styles = ('<numFmts count="1"><numFmt numFmtId="164" formatCode="dd/mm/yyyy"/></numFmts>'
              '<cellXfs count="3"><xf numFmtId="0"/><xf numFmtId="14"/><xf numFmtId="164"/></cellXfs>')
    s1 = _row(1, ['<c r="A1" s="1"><v>46336</v></c>', '<c r="B1" s="2"><v>46336.5</v></c>', '<c r="C1"><v>46336</v></c>'])
    s2 = _row(1, [_inline("A1", "second")])
    data = _xlsx([("Dates", s1), ("Two", s2)], styles=styles)
    p = TMP / "dates.xlsx"
    p.write_bytes(data)
    for src in (data, str(p), p):
        book = xlsx.read(src)
        assert [s["name"] for s in book["sheets"]] == ["Dates", "Two"]
        assert book["sheets"][0]["rows"] == [["2026-11-10", "2026-11-10 12:00", "46336"]], book["sheets"][0]["rows"]
        assert book["sheets"][1]["rows"] == [["second"]]
    # 1904 date system
    b = xlsx.read(_xlsx([("D", s1)], styles=styles, workbook_pr='<workbookPr date1904="1"/>'))
    assert b["sheets"][0]["rows"][0][0] == "2030-11-11"


def test_xlsx_row_and_column_caps():
    body = "".join(_row(r, [f'<c r="{c}{r}"><v>{r}</v></c>' for c in ("A", "AZ")]) for r in range(1, 30))
    book = xlsx.read(_xlsx([("S", body)]), max_rows=10, max_cols=3)
    rows = book["sheets"][0]["rows"]
    assert len(rows) == 10 and all(len(r) == 1 for r in rows)
    many = [(f"S{i}", _row(1, [_inline("A1", str(i))])) for i in range(25)]
    assert len(xlsx.read(_xlsx(many))["sheets"]) == xlsx.MAX_SHEETS


def test_xlsx_refuses_old_xls_and_garbage():
    _raises(xlsx.read, b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 600, contains="old .xls", exc=xlsx.XlsxError)
    _raises(xlsx.read, b"Track,Notes\nRoval,x\n", contains="isn't an .xlsx", exc=xlsx.XlsxError)
    _raises(xlsx.read, b"PK\x03\x04garbage", contains="damaged", exc=xlsx.XlsxError)
    _raises(xlsx.read, b"PK\x03\x04" + b"\0" * (xlsx.MAX_FILE + 1), contains="too big", exc=xlsx.XlsxError)
    _raises(xlsx.read, str(TMP / "missing.xlsx"), contains="couldn't open", exc=xlsx.XlsxError)
    assert issubclass(xlsx.XlsxError, ValueError)


def _patch_size(data: bytes, name: str, size: int) -> bytes:
    """Rewrite the uncompressed size a zip claims for one entry (local header and central directory)."""
    import struct
    b = bytearray(data)
    nb = name.encode()
    i = 0
    while True:
        i = b.find(b"PK\x03\x04", i)
        if i < 0:
            break
        n, m = struct.unpack("<HH", b[i + 26:i + 30])
        if bytes(b[i + 30:i + 30 + n]) == nb:
            struct.pack_into("<I", b, i + 22, size)
        i += 4
    i = 0
    while True:
        i = b.find(b"PK\x01\x02", i)
        if i < 0:
            break
        n = struct.unpack("<H", b[i + 28:i + 30])[0]
        if bytes(b[i + 46:i + 46 + n]) == nb:
            struct.pack_into("<I", b, i + 24, size)
        i += 4
    return bytes(b)


def test_xlsx_zip_bomb_limits():
    faulthandler.dump_traceback_later(60, exit=True)
    try:
        ok = _xlsx([("S", _row(1, [_inline("A1", "x")]))])
        # an entry claiming 50 MB is refused before anything is decompressed
        bomb = _patch_size(ok, "xl/worksheets/sheet1.xml", 50 * 1024 * 1024)
        _raises(xlsx.read, bomb, contains="too big", exc=xlsx.XlsxError)
        # a real highly-compressible part over 10 MB
        big = _xlsx([("S", "<row r=\"1\"/>" + " " * (11 * 1024 * 1024))])
        assert len(big) < xlsx.MAX_FILE
        _raises(xlsx.read, big, contains="too big", exc=xlsx.XlsxError)
        # many parts adding up past 25 MB
        parts = {f"xl/media/pad{i}.bin": "\0" * (9 * 1024 * 1024) for i in range(3)}
        _raises(xlsx.read, _xlsx([("S", "")], extra=parts), contains="too big", exc=xlsx.XlsxError)
        # too many entries
        many = {f"xl/pad/{i}.txt": "" for i in range(xlsx.MAX_ENTRIES + 1)}
        _raises(xlsx.read, _xlsx([("S", "")], extra=many), contains="too many", exc=xlsx.XlsxError)
        # a part whose header understates its size is still capped while reading
        lie = _xlsx([("S", "<row r=\"1\"/>" + " " * (11 * 1024 * 1024))])
        lie = _patch_size(lie, "xl/worksheets/sheet1.xml", 100)
        try:
            xlsx.read(lie)
            raise AssertionError("understated size was accepted")
        except xlsx.XlsxError:
            pass
    finally:
        faulthandler.cancel_dump_traceback_later()


def test_xlsx_refuses_doctype():
    shared = '<si><t>&lol;</t></si>'
    data = _xlsx([("S", _row(1, [_s("A1", 0)]))], shared=shared)
    # put a DOCTYPE with an entity into the shared strings part
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as zin, zipfile.ZipFile(buf, "w") as zout:
        for i in zin.infolist():
            raw = zin.read(i)
            if i.filename == "xl/sharedStrings.xml":
                raw = (b'<?xml version="1.0"?><!DOCTYPE sst [<!ENTITY lol "lol">]>'
                       b'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>&lol;</t></si></sst>')
            zout.writestr(i, raw)
    _raises(xlsx.read, buf.getvalue(), contains="won't read", exc=xlsx.XlsxError)
    # lower-case doctype too
    data2 = _xlsx([("S", '<!doctype x>' + _row(1, [_inline("A1", "x")]))])
    _raises(xlsx.read, data2, exc=xlsx.XlsxError)


def test_xlsx_missing_shared_strings_and_bad_index():
    book = xlsx.read(_xlsx([("S", _row(1, [_s("A1", 5), _inline("B1", "ok")]))]))
    assert book["sheets"][0]["rows"] == [["", "ok"]]
    assert xlsx.read(_xlsx([("Empty", "")]))["sheets"][0]["rows"] == []


# ================================================================ intel: parsing + suggestions
def test_header_suggestions():
    s = intel.suggest(["Track", "Crash spots", "Overtaking", "Kerbs", "Focus"])
    assert s == {0: "track", 1: "hotspots", 2: "passing", 3: "limits", 4: "focus"}, s
    s = intel.suggest(["Circuit", "Track limits", "Car", "Setup notes", "Turn 1", "Notes", "Goal", "Whatever"])
    assert s[0] == "track" and s[1] == "limits" and s[2] == "car" and s[3] == "car_note" and s[4] == "lap1"
    assert s[5] == "focus" and s[6] == "focus" and 7 not in s, s
    # each field once (except focus): a second track column isn't suggested as track
    s = intel.suggest(["Track", "Layout", "Vehicle", "Defending", "Tyre wear", "Car note"])
    assert s[0] == "track" and 1 not in s and s[2] == "car" and s[3] == "passing" and s[4] == "car_note"
    assert 5 not in s, s
    assert intel.suggest(["", "  "]) == {}


def test_parse_csv_variants():
    csv_text = "Week schedule,,\nTrack;Crash spots;Overtaking\nCharlotte Roval;T1 lap 1;Bus stop\n;;\nOkayama;Hairpin;\n"
    p = intel.parse_upload(csv_text.encode("utf-8-sig"), "intel.csv")
    sh = p["sheets"][0]
    assert sh["header"] == ["Track", "Crash spots", "Overtaking"] and sh["name"] == "intel"
    assert sh["rows"] == [["Charlotte Roval", "T1 lap 1", "Bus stop"], ["Okayama", "Hairpin", ""]]
    assert sh["suggest"] == {0: "track", 1: "hotspots", 2: "passing"}
    # cp1252 and tabs
    p = intel.parse_upload("Track\tNotes\nN\xfcrburgring\tK\xfcrbs\n".encode("cp1252"), "x.txt")
    assert p["sheets"][0]["rows"] == [["Nürburgring", "Kürbs"]]
    # quoted multi-line cells
    p = intel.parse_upload(b'Track,Focus\n"Roval","Line one\nLine two"\n', "x.csv")
    assert p["sheets"][0]["rows"][0][1] == "Line one\nLine two"
    _raises(intel.parse_upload, b"", "x.csv", contains="empty")
    _raises(intel.parse_upload, b"just one cell\n", "x.csv", contains="header row")
    _raises(intel.parse_upload, b"a" * (intel.MAX_BYTES + 1), "x.csv", contains="too big")


def test_parse_xlsx_by_magic_and_extension():
    data = _intel_book([("Charlotte Roval", "T1", "Bus stop", "Oval exit", "Brake marker")])
    for name in ("intel.xlsx", "intel.bin", ""):
        p = intel.parse_upload(data, name)
        assert p["sheets"][0]["header"][0] == "Track" and p["sheets"][0]["suggest"][3] == "limits"
    _raises(intel.parse_upload, b"\xd0\xcf\x11\xe0" + b"\0" * 100, "old.xls", contains="old .xls")
    # a sheet with no header row is dropped, others kept
    two = _xlsx([("Cover", _row(1, [_inline("A1", "Title only")])),
                 ("Intel", _row(1, [_inline("A1", "Track"), _inline("B1", "Notes")]) + _row(2, [_inline("A2", "Roval"), _inline("B2", "x")]))])
    assert [s["name"] for s in intel.parse_upload(two, "a.xlsx")["sheets"]] == ["Intel"]


# ================================================================ intel: preview / apply
def _setup(existing=None, share=False):
    cfg = _cfg(share=share)
    if existing:
        notes.save(cfg["library_folder"], notes.clean({"track": ROVAL, "fields": existing}))
    return cfg


KNOWN = [ROVAL, "Okayama International Circuit (Full Course)"]
MAP = {0: "track", 1: "hotspots", 2: "passing", 3: "limits", 4: "focus"}


def test_preview_fill_empty_keeps_existing_text():
    cfg = _setup({"passing": "Mine: back chicane"})
    parsed = intel.parse_upload(_intel_book([("Charlotte Motor Speedway - Roval 2025", "T1 lap 1", "Bus stop", "Oval exit", "")]), "a.xlsx")
    pv = intel.preview(parsed, 0, MAP, "fill_empty", cfg, KNOWN)
    t = pv["tracks"][0]
    assert t["track"] == ROVAL and t["matched"] == ROVAL and not t["new"]
    assert t["result"]["fields"]["passing"] == "Mine: back chicane"
    assert t["result"]["fields"]["hotspots"] == "T1 lap 1" and t["result"]["fields"]["limits"] == "Oval exit"
    assert t["changes"] == ["hotspots", "limits"]
    assert t["incoming"]["fields"]["passing"] == "Bus stop"
    # nothing was written by preview
    assert notes.load(cfg["library_folder"], ROVAL)["fields"]["hotspots"] == ""


def test_preview_replace_never_wipes_fields_the_sheet_leaves_empty():
    cfg = _setup({"passing": "old pass", "lap1": "old start"})
    parsed = intel.parse_upload(_intel_book([(ROVAL, "", "new pass", "", "")]), "a.xlsx")
    t = intel.preview(parsed, 0, MAP, "replace", cfg, KNOWN)["tracks"][0]
    assert t["result"]["fields"]["passing"] == "new pass"
    assert t["result"]["fields"]["lap1"] == "old start" and t["changes"] == ["passing"]


def test_preview_append_and_two_rows_merge():
    cfg = _setup({"hotspots": "T1 on lap 1"})
    parsed = intel.parse_upload(_intel_book([(ROVAL, "T1 on lap 1", "Bus stop", "", ""),
                                             ("Charlotte Motor Speedway - Roval 2025", "Oval exit wall", "Bus stop", "", ""),
                                             ("okayama international circuit (full course)", "Hairpin", "", "", "")]), "a.xlsx")
    pv = intel.preview(parsed, 0, MAP, "append", cfg, KNOWN)
    assert [t["track"] for t in pv["tracks"]] == [ROVAL, "Okayama International Circuit (Full Course)"]
    t = pv["tracks"][0]
    assert t["result"]["fields"]["hotspots"] == "T1 on lap 1\nOval exit wall"   # deduplicated, line by line
    assert t["result"]["fields"]["passing"] == "Bus stop"
    assert pv["rows"] == 3


def test_preview_new_track_and_several_columns_for_one_field():
    cfg = _setup()
    header = ("Track", "Notes A", "Notes B", "Car", "Setup", "Gearing")
    parsed = intel.parse_upload(_intel_book([("Nowhere Raceway", "Kerb at T3", "Wall at T5", "Ford Mustang GT4", "Soft rear", "Short 3rd"),
                                             ("Nowhere Raceway", "", "", "Ford Mustang GT4", "Soft rear", "")], header=header), "a.xlsx")
    mp = {0: "track", 1: "limits", 2: "limits", 3: "car", 4: "car_note", 5: "car_note"}
    t = intel.preview(parsed, 0, mp, "fill_empty", cfg, KNOWN)["tracks"][0]
    assert t["new"] and t["matched"] is None and t["warnings"][0] == "New track: kept as typed."
    assert t["result"]["fields"]["limits"] == "Kerb at T3 · Wall at T5"
    assert t["result"]["car_notes"] == {"Ford Mustang GT4": "Soft rear · Short 3rd · Soft rear"} or \
        t["result"]["car_notes"] == {"Ford Mustang GT4": "Soft rear · Short 3rd"}, t["result"]["car_notes"]
    # car column without car_note is ignored; car_note without car is flagged
    pv = intel.preview(parsed, 0, {0: "track", 3: "car"}, "fill_empty", cfg, KNOWN)
    assert pv["tracks"][0]["result"]["car_notes"] == {}
    pv = intel.preview(parsed, 0, {0: "track", 4: "car_note"}, "fill_empty", cfg, KNOWN)
    assert pv["warnings"] and pv["tracks"][0]["result"]["car_notes"] == {}


def test_preview_flags_long_text_and_focus_lines():
    cfg = _setup()
    long_text = "word " * 100
    parsed = intel.parse_upload(_intel_book([(ROVAL, long_text, "", "", "a\nb\nc\nd\ne")]), "a.csv.xlsx")
    t = intel.preview(parsed, 0, MAP, "fill_empty", cfg, KNOWN)["tracks"][0]
    assert len(t["result"]["fields"]["hotspots"]) <= notes.MAX_FIELD
    assert t["result"]["fields"]["focus"] == "a\nb\nc"
    assert any("cut to 300" in w for w in t["warnings"]) and any("3 lines" in w for w in t["warnings"]), t["warnings"]
    notes.clean({"track": t["track"], "fields": t["result"]["fields"]})   # the result is always saveable


def test_preview_bad_input():
    cfg = _setup()
    parsed = intel.parse_upload(_intel_book([(ROVAL, "x", "", "", "")]), "a.xlsx")
    _raises(intel.preview, parsed, 0, {1: "hotspots"}, "fill_empty", cfg, KNOWN, contains="names the track")
    _raises(intel.preview, parsed, 0, {0: "track", 1: "track"}, "fill_empty", cfg, KNOWN, contains="one column")
    _raises(intel.preview, parsed, 0, {0: "track", 9: "hotspots"}, "fill_empty", cfg, KNOWN)
    _raises(intel.preview, parsed, 0, {0: "track", 1: "nonsense"}, "fill_empty", cfg, KNOWN)
    _raises(intel.preview, parsed, 3, MAP, "fill_empty", cfg, KNOWN, contains="sheet")
    _raises(intel.preview, parsed, 0, MAP, "overwrite_all", cfg, KNOWN, contains="how to import")
    _raises(intel.preview, {"nope": 1}, 0, MAP, "fill_empty", cfg, KNOWN)
    # string column keys (as JSON sends them) are fine
    assert intel.preview(parsed, 0, {"0": "track", "1": "hotspots"}, None, cfg, KNOWN)["mode"] == "fill_empty"


def test_apply_goes_through_notes_save_and_is_idempotent():
    for mode in intel.MODES:
        cfg = _setup({"passing": "Mine"})
        parsed = intel.parse_upload(_intel_book([(ROVAL, "T1 lap 1", "Bus stop", "", "Brake marker"),
                                                 ("Okayama International Circuit - Full Course", "Hairpin", "", "", "")]), "a.xlsx")
        calls = []
        real = notes.save

        def spy(lib, card, cfg_=None):
            calls.append((lib, card["track"], cfg_ is cfg))
            return real(lib, card, cfg_)
        notes.save = spy
        try:
            out = intel.apply(parsed, 0, MAP, mode, cfg, KNOWN)
            assert out == {"tracks": 2, "changed": 2, "skipped": 0, "errors": []}, (mode, out)
            assert [c[1] for c in calls] == [ROVAL, "Okayama International Circuit (Full Course)"]
            assert all(c[2] for c in calls)   # settings passed on, so publishing still applies
            calls.clear()
            again = intel.apply(parsed, 0, MAP, mode, cfg, KNOWN)
            assert again == {"tracks": 2, "changed": 0, "skipped": 2, "errors": []}, (mode, again)
            assert calls == []
        finally:
            notes.save = real
        card = notes.load(cfg["library_folder"], ROVAL)
        want = "Bus stop" if mode == "replace" else ("Mine\nBus stop" if mode == "append" else "Mine")
        assert card["fields"]["passing"] == want, (mode, card["fields"])


def test_apply_publishes_when_sharing_is_on():
    cfg = _setup(share=True)
    parsed = intel.parse_upload(_intel_book([(ROVAL, "T1", "", "", "")]), "a.xlsx")
    intel.apply(parsed, 0, MAP, "fill_empty", cfg, KNOWN)
    f = Path(cfg["team_cards_folder"]) / "Debrief track cards" / notes.track_key(ROVAL) / "swazi-williams.json"
    assert json.loads(f.read_text(encoding="utf-8"))["fields"]["hotspots"] == "T1"


def test_apply_skips_read_only_cards():
    cfg = _setup()
    p = notes._path(cfg["library_folder"], notes.track_key(ROVAL))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"schema": 99, "track": ROVAL, "fields": {}}), encoding="utf-8")
    parsed = intel.parse_upload(_intel_book([(ROVAL, "T1", "", "", "")]), "a.xlsx")
    pv = intel.preview(parsed, 0, MAP, "fill_empty", cfg, KNOWN)
    assert pv["tracks"][0]["read_only"] and pv["tracks"][0]["changes"] == []
    assert intel.apply(parsed, 0, MAP, "fill_empty", cfg, KNOWN)["changed"] == 0


# ================================================================ results
def test_results_new_fields_ranges():
    ok = results.validate({"finish_pos": 9, "start_pos": 14, "field_size": 22, "pos_after_lap1": 11, "pit_stops": "1",
                           "penalties": 0, "finish_status": "DNF",
                           "incident_laps": [{"lap": 12, "kind": "spin", "points": 2}, {"lap": "3", "kind": "Off", "points": "1"},
                                             {"lap": 5, "kind": "contact"}]})
    assert ok["pos_after_lap1"] == 11 and ok["pit_stops"] == 1 and ok["penalties"] == 0
    assert ok["finish_status"] == "dnf" and ok["positions_lap1"] == 3 and ok["positions_gained"] == 5
    assert ok["incident_laps"] == [{"lap": 3, "kind": "off", "points": 1}, {"lap": 5, "kind": "contact", "points": 0},
                                   {"lap": 12, "kind": "spin", "points": 2}]
    bad = [{"pos_after_lap1": 0}, {"pos_after_lap1": 201}, {"pos_after_lap1": 23, "field_size": 22},
           {"pos_after_lap1": 1.5}, {"pit_stops": -1}, {"pit_stops": 51}, {"penalties": 51}, {"penalties": True},
           {"finish_status": "crashed"}, {"finish_status": 3},
           {"incident_laps": [{"lap": 1, "kind": "off", "points": 0}] * 31},
           {"incident_laps": [{"lap": 1, "kind": "wreck", "points": 0}]},
           {"incident_laps": [{"lap": 0, "kind": "off", "points": 0}]},
           {"incident_laps": [{"lap": 2001, "kind": "off", "points": 0}]},
           {"incident_laps": [{"lap": 1, "kind": "off", "points": 3}]},
           {"incident_laps": [{"lap": 1, "kind": "off", "points": 8}]},
           {"incident_laps": [{"lap": True, "kind": "off"}]},
           {"incident_laps": ["lap 1"]}, {"incident_laps": "1,2"}, {"incident_laps": {"lap": 1}}]
    for b in bad:
        _raises(results.validate, dict({"finish_pos": 5}, **b))
    assert len(results.validate({"finish_pos": 1, "incident_laps": [{"lap": i, "kind": "other", "points": 4} for i in range(1, 31)]})["incident_laps"]) == 30


def test_results_old_shape_unchanged():
    v = results.validate({"finish_pos": "7", "start_pos": 9, "field_size": 18, "incidents": 2})
    assert v == {"finish_pos": 7, "start_pos": 9, "field_size": 18, "incidents": 2, "positions_gained": 2}
    v = results.validate({"finish_pos": 3, "finish_status": "", "incident_laps": [], "pos_after_lap1": ""})
    assert v == {"finish_pos": 3}
    assert "positions_lap1" not in results.validate({"finish_pos": 3, "pos_after_lap1": 2})
    assert json.dumps(results.validate({"finish_pos": 1, "notes": "x" * 500,
                                        "incident_laps": [{"lap": 2000, "kind": "contact", "points": 4}] * 30})).__len__() < 64 * 1024


# ================================================================ prepare
def test_week_view_adds_shared_notes_and_strategy():
    from debrief import schedule
    cfg = _cfg()
    root = Path(cfg["team_cards_folder"]) / "Debrief track cards"
    _mate(root, "josh-spence")
    _mate(root, "hayden")
    _mate(root, "swazi-williams")
    ser = {"id": "s", "name": "Series", "cars": ["Ford Mustang GT4"],
           "weeks": [{"week": 1, "start": "2026-11-17", "track": "Charlotte Motor Speedway - Roval 2025", "length": "15 laps"}]}
    w = prepare._week_view(ser, 1, [], cfg["library_folder"], None, schedule.Matcher(), cfg=cfg, sched=None,
                           me_slug="swazi-williams")
    assert w["shared_notes"] == 2
    assert "strategy" in w
    try:
        from debrief import strategy  # noqa: F401  (24-A)
        assert w["strategy"] is None or isinstance(w["strategy"], dict)
    except ImportError:
        assert w["strategy"] is None
    # old callers (no cfg) still work and count nothing
    w = prepare._week_view(ser, 1, [], cfg["library_folder"], None, schedule.Matcher())
    assert w["shared_notes"] == 0 and w["strategy"] is None


def test_week_view_strategy_uses_prefill_race_when_available():
    import types
    from debrief import schedule
    calls = []
    fake = types.ModuleType("debrief.strategy")

    def prefill(cfg, store, sched, track, cars, week):
        calls.append((store, track, tuple(cars), week["length"]))
        return {"race": {"laps": 15, "minutes": None, "source": "schedule", "text": "15 laps"}, "lap_t": None}
    fake.prefill = prefill
    had = sys.modules.get("debrief.strategy")
    sys.modules["debrief.strategy"] = fake
    import debrief
    old_attr = getattr(debrief, "strategy", None)
    debrief.strategy = fake
    try:
        cfg = _cfg(folder=False)
        ser = {"id": "s", "name": "S", "cars": ["Ford Mustang GT4"],
               "weeks": [{"week": 1, "start": "2026-11-17", "track": ROVAL, "length": "15 laps"}]}
        w = prepare._week_view(ser, 1, [], cfg["library_folder"], None, schedule.Matcher(), cfg=cfg, sched={})
        assert w["strategy"] == {"laps": 15, "minutes": None, "source": "schedule", "text": "15 laps"}
        assert calls == [(None, ROVAL, ("Ford Mustang GT4",), "15 laps")]   # no store: never a request
        fake.prefill = lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))
        assert prepare._week_view(ser, 1, [], cfg["library_folder"], None, schedule.Matcher(), cfg=cfg)["strategy"] is None
    finally:
        if had is None:
            sys.modules.pop("debrief.strategy", None)
        else:
            sys.modules["debrief.strategy"] = had
        if old_attr is None:
            try:
                del debrief.strategy
            except AttributeError:
                pass
        else:
            debrief.strategy = old_attr


def test_module_never_imports_network_code():
    import ast
    for mod in ("notes", "xlsx", "intel"):
        tree = ast.parse((ROOT / "debrief" / f"{mod}.py").read_text(encoding="utf-8"))
        names = {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
        names |= {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        assert not names & {"requests", "urllib", "http", "socket"}, (mod, names)


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except Exception as e:  # noqa: BLE001
                failed += 1
                import traceback
                traceback.print_exc()
                print(f"FAIL {name}: {e}")
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"{'FAILED' if failed else 'passed'}: {failed} failure(s)")
    sys.exit(1 if failed else 0)
