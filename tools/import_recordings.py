"""Make a Debrief voice pack from your own recordings of the recording script (docs/voice-recording-script.md).

Two ways to record (mix them if you like):

  One long recording: read the lines in order, with a clear pause (about one second) between lines.
      python tools/import_recordings.py --long take.wav --id myvoice --name "My voice" --by "Your name"
  Several long recordings, each saying where it starts (line number, then a colon):
      python tools/import_recordings.py --long 1:part1.wav --long 99:part2.wav --id myvoice --name "My voice" --by "Your name"
  One file per line, named by its line number ("012.wav", "12 turn one.wav"):
      python tools/import_recordings.py --folder takes --id myvoice --name "My voice" --by "Your name"
  A file per line overrides the same line from a long recording, so a bad line can be fixed on its own.

WAV works everywhere (most phone recorder apps can save WAV). FLAC, MP3 and M4A need ffmpeg (on the PATH, in
tools/, or --ffmpeg path). Each line is cleaned (rumble filter at 80 Hz, clicks, a light noise gate), trimmed,
cut at the comma where the pack needs it, and levelled to the same loudness as the voices that come with Debrief.

The pack goes to --out (default voicepacks/<id>); import-report.txt beside it lists every line with where it was
found in the recording, so you can check the lines lined up. If the number of pieces in a long recording doesn't
match the lines, nothing is written (unless --force) and the report shows where to look.

    python tools/import_recordings.py --write-script docs/voice-recording-script.md    # rewrite the script
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from debrief import voicepack as vp  # noqa: E402

RATE = 22050                  # the bundled packs' rate
MIN_PAUSE_S = 0.6             # a quiet stretch at least this long separates two lines
MIN_PIECE_S = 0.12            # anything shorter is a click or a breath, not a line
EXTRA = (".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wma")


class ImportError_(Exception):
    pass


# ---------------------------------------------------------------- reading audio

def find_ffmpeg(given=None):
    for c in ([given] if given else []) + [shutil.which("ffmpeg"), str(ROOT / "tools" / "ffmpeg.exe"),
                                           str(ROOT / "tools" / "ffmpeg")]:
        if c and Path(c).is_file():
            return c
    return None


def read_audio(path, ffmpeg=None):
    """(float samples, rate) of any recording: WAV directly, other formats through ffmpeg."""
    p = Path(path)
    if p.suffix.lower() == ".wav":
        x, sr, _info = vp.read_wav(p)
        return np.asarray(x, dtype=np.float64), sr
    if p.suffix.lower() not in EXTRA:
        raise ImportError_(f"{p.name}: not a sound file Debrief knows (use WAV)")
    ff = find_ffmpeg(ffmpeg)
    if not ff:
        raise ImportError_(f"{p.name}: {p.suffix} needs ffmpeg, which isn't on this computer. Save the recording "
                           f"as WAV instead (most recorder apps can), or put ffmpeg.exe in the tools folder.")
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "a.wav"
        r = subprocess.run([ff, "-nostdin", "-v", "error", "-i", str(p), "-vn", "-ac", "1", "-ar", "48000",
                            "-acodec", "pcm_s16le", str(out)], capture_output=True, timeout=600)
        if r.returncode != 0 or not out.is_file():
            raise ImportError_(f"{p.name}: ffmpeg could not read it ({r.stderr.decode(errors='replace')[:200]})")
        x, sr, _info = vp.read_wav(out)
    return np.asarray(x, dtype=np.float64), sr


# ---------------------------------------------------------------- splitting and cleaning

def split_long(x, sr, min_pause_s=MIN_PAUSE_S, min_piece_s=MIN_PIECE_S):
    """Pieces of speech in a long recording: [(start, end)] in samples. 20 ms frames above the noise floor
    (+10 dB, and within 45 dB of the loudest frame) are speech; quiet stretches shorter than min_pause_s stay
    inside a piece (the pause at a comma); pieces shorter than min_piece_s are dropped."""
    f = max(1, int(0.02 * sr))
    lv = vp.frame_levels(x, sr, 20)
    if lv.size == 0:
        return []
    floor = float(np.percentile(lv, 10))
    thr = max(floor + 10.0, float(lv.max()) - 45.0)
    on = lv > thr
    pieces, i, n = [], 0, len(on)
    while i < n:
        if not on[i]:
            i += 1
            continue
        j = i
        while j < n and on[j]:
            j += 1
        if pieces and (i - pieces[-1][1]) * 0.02 < min_pause_s:
            pieces[-1][1] = j
        else:
            pieces.append([i, j])
        i = j
    return [(a * f, min(len(x), b * f)) for a, b in pieces if (b - a) * 0.02 >= min_piece_s]


def clean(x, sr):
    """Rumble filter, de-click, light noise gate (before cutting and levelling)."""
    return vp.noise_gate(vp.declick(vp.highpass(vp.remove_dc(x), sr, 80.0), sr), sr)


def expected_s(text: str) -> float:
    from make_voicepack import syllables  # same folder
    return syllables(text) / 3.5 + 0.2


# ---------------------------------------------------------------- the whole import

def parse_long(arg: str):
    m = re.match(r"^(\d+):(.+)$", arg)
    return (int(m.group(1)), m.group(2)) if m and not re.match(r"^[A-Za-z]:[\\/]", arg) else (1, arg)


def line_number(name: str):
    m = re.match(r"^\s*(\d{1,4})\b", Path(name).stem)
    return int(m.group(1)) if m else None


def collect(longs=(), folder=None, ffmpeg=None, min_pause_s=MIN_PAUSE_S, force=False):
    """{line number: (samples, rate, where)} plus report lines and problems."""
    lines = {ln["n"]: ln for ln in vp.lines()}
    takes, report, problems = {}, [], []
    for arg in longs:
        start, path = parse_long(arg)
        x, sr = read_audio(path, ffmpeg)
        pieces = split_long(x, sr, min_pause_s)
        left = len(lines) - start + 1
        nxt = sorted(n for n in (parse_long(a)[0] for a in longs) if n > start)
        want = (nxt[0] - start) if nxt else left
        report.append(f"{Path(path).name}: {len(pieces)} pieces found, lines {start} to {start + want - 1} expected "
                      f"({want} lines)")
        if len(pieces) != want:
            problems.append(f"{Path(path).name} has {len(pieces)} pieces but {want} lines were expected from line "
                            f"{start}. Usually a line was skipped or said twice, or a pause inside a line was too "
                            f"long. The times below show where it stops lining up.")
        for k, (a, b) in enumerate(pieces):
            n = start + k
            ln = lines.get(n)
            secs = (b - a) / sr
            exp = expected_s(ln["text"]) if ln else 0
            flag = "" if (ln and 0.4 <= secs / exp <= 2.5) else "   <- check this one"
            report.append(f"  line {n:3d} at {int(a / sr // 60)}:{a / sr % 60:04.1f}  {secs:4.1f} s  "
                          f"{(ln or {}).get('text', '(no such line)')}{flag}")
            if ln is not None and k < want:
                pad = int(0.1 * sr)
                takes[n] = (x[max(0, a - pad):b + pad], sr, f"{Path(path).name} at {a / sr:.1f} s")
    if folder:
        for f in sorted(Path(folder).iterdir()):
            if not f.is_file() or f.suffix.lower() not in (".wav",) + EXTRA:
                continue
            n = line_number(f.name)
            if n is None or n not in lines:
                report.append(f"  {f.name}: no line number at the start of the name, skipped")
                continue
            x, sr = read_audio(f, ffmpeg)
            takes[n] = (x, sr, f.name)
            report.append(f"  line {n:3d} from {f.name}")
    return takes, report, problems


def build_pack(takes, out, pack_id, name, by, rate=RATE) -> dict:
    """Cut, clean and level every take into a pack at out. Returns {"clips", "missing", "problems"}."""
    clips, gaps, problems = {}, [], []
    for ln in vp.lines():
        t = takes.get(ln["n"])
        if t is None:
            continue
        x, sr, where = t
        roles = {r for _k, r in ln["clips"]}
        cx = clean(x, sr)
        parts = None
        for db in (-40.0, -32.0):
            try:
                parts = vp.cut_line(cx, sr, roles, rel_db=db)
                break
            except ValueError:
                continue
        if parts is None:
            problems.append(f"line {ln['n']} ({where}): no pause at the comma in '{ln['text']}'. Say it again with "
                            f"a short pause at the comma.")
            continue
        if "_gap_ms" in parts:
            gaps.append(parts["_gap_ms"])
        for key, role in ln["clips"]:
            y = vp.finish(vp.resample(parts[role], sr, rate), rate)
            if y.size == 0:
                problems.append(f"line {ln['n']} ({where}): no speech found")
                continue
            head, _, tail = ln["text"].partition(",")
            clips[key] = (y, ln["text"] if role == "whole" else (head + "," if role == "lead" else tail.strip()), role)
    if not clips:
        raise ImportError_("no usable lines were found")
    join = float(np.clip(np.median(gaps) - 2 * vp.PAD_MS, 40, 250)) if gaps else vp.JOIN_MS
    meta = {"id": pack_id, "name": name, "voice": "recorded", "voice_credit": f"Recorded by {by}",
            "license": f"Recorded by {by}", "loudness_db": vp.TARGET_DB, "generator": "tools/import_recordings.py"}
    vp.write_pack(out, meta, clips, rate, join)
    pack = vp.load_pack(out, strict=True)
    vp.write_pack(out, meta, clips, rate, join, preview=vp.preview(pack))
    return {"clips": len(clips), "missing": sorted(set(vp.all_keys()) - set(clips)), "problems": problems}


def write_script(path):
    """The recording script, from voicepack.lines() (so the line numbers always match the importer)."""
    out = ["# Debrief voice recording script", "",
           "Read these lines to make your own voice for the live coach. How to say them: calm, clear, like a race "
           "engineer on the radio. Not too fast, the same pace and the same distance from the microphone all the "
           "way through.", "",
           "**Recording tips**", "",
           "- A quiet room, phone or headset microphone about a hand's width from your mouth.",
           "- Save as WAV if your recorder app can (FLAC, MP3 or M4A also work when ffmpeg is installed).",
           "- One long recording: read every line in order and leave a clear pause (count \"one, two\" in your "
           "head) between lines. A comma inside a line gets a short, natural pause, much shorter than the pause "
           "between lines.",
           "- Made a mistake? Pause, then read the line again, and fix it later by recording just that line as "
           "its own file named by its number (for example `57.wav`).",
           "- You can split the reading into several recordings, one per section; tell the importer where each "
           "one starts (`--long 1:part1.wav --long 59:part2.wav`).", "",
           "Then make the pack:", "",
           "```", "python tools/import_recordings.py --long myvoice.wav --id myvoice --name \"My voice\" "
           "--by \"Your name\"", "```", ""]
    sec = None
    for ln in vp.lines():
        if ln["section"] != sec:
            sec = ln["section"]
            out += ["", f"## Section {sec} (from line {ln['n']})", "", vp.SECTIONS[sec], ""]
        out.append(f"{ln['n']}. {ln['text']}")
    Path(path).write_text("\n".join(out) + "\n", encoding="utf-8")
    return path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--long", action="append", default=[], help="a long recording, optionally START:file")
    ap.add_argument("--folder", help="a folder of one file per line, named by line number")
    ap.add_argument("--id", help="pack id (letters, digits, - and _)")
    ap.add_argument("--name", help="name shown in Debrief")
    ap.add_argument("--by", help="who recorded it (shown as the credit)")
    ap.add_argument("--out", help="pack folder (default voicepacks/<id>)")
    ap.add_argument("--ffmpeg", help="path to ffmpeg, for FLAC/MP3/M4A")
    ap.add_argument("--pause", type=float, default=MIN_PAUSE_S, help="shortest pause between lines, in seconds")
    ap.add_argument("--force", action="store_true", help="write the pack even if the lines didn't line up")
    ap.add_argument("--write-script", help="write the recording script to this file and stop")
    a = ap.parse_args(argv)
    if a.write_script:
        print(write_script(a.write_script))
        return 0
    if not (a.long or a.folder):
        ap.error("give --long and/or --folder")
    if not (a.id and vp.ID_RE.match(a.id) and a.name and a.by):
        ap.error("give --id (letters, digits, - and _), --name and --by")
    out = Path(a.out) if a.out else Path("voicepacks") / a.id
    try:
        takes, report, problems = collect(a.long, a.folder, a.ffmpeg, a.pause)
    except ImportError_ as e:
        print(f"Stopped: {e}")
        return 2
    out.mkdir(parents=True, exist_ok=True)
    rep = out.parent / f"{a.id}-import-report.txt"
    if problems and not a.force:
        rep.write_text("\n".join(problems + [""] + report) + "\n", encoding="utf-8")
        print("\n".join(problems))
        print(f"Nothing was written. Check {rep}")
        return 1
    try:
        r = build_pack(takes, out, a.id, a.name, a.by)
    except (ImportError_, vp.VoicePackError) as e:
        print(f"Stopped: {e}")
        return 2
    summary = [f"{a.name}: {r['clips']} clips written to {out}"]
    summary += [f"Missing (Windows speech will say these): {', '.join(r['missing'])}"] if r["missing"] else []
    summary += r["problems"] + problems
    rep.write_text("\n".join(summary + [""] + report) + "\n", encoding="utf-8")
    print("\n".join(summary))
    print(f"Report: {rep}")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    sys.exit(main())
