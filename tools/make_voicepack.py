"""Build a Debrief voice pack with the Kokoro-82M neural voice (build time only: Kokoro is never shipped in the app).

Needs a Python with kokoro-onnx (and numpy), and Kokoro's two model files (kokoro.onnx, voices.bin):

    python tools/make_voicepack.py --preset engineer                      # George, the default voice
    python tools/make_voicepack.py --preset all                           # the six bundled voices
    python tools/make_voicepack.py --voice em_alex --lang en-us --id alex --name "Alex (Spanish accent)" \
        --out debrief/data/voicepacks/alex                                # any other Kokoro voice

Model files: --model-dir, or the KOKORO_DIR environment variable (a folder holding kokoro.onnx and voices.bin).

Every sentence in voicepack.lines() is spoken once, whole, with its punctuation (so the intonation is natural),
then cut at the comma where a pack joins parts, trimmed, levelled to the same loudness and written as 16-bit mono
WAV. The same inputs always give the same pack. Also written: preview.wav (ten typical cues, for listening) and
coverage.txt (which of the coach's phrases the pack can say).
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from debrief import voicepack as vp  # noqa: E402

KOKORO_RATE = 24000
LICENSE = "Generated with Kokoro-82M, Apache-2.0"
OVERHEAD_S = 0.2                  # a spoken word takes this long on top of its syllables (one-word lines aren't "slow")
TARGET_WPS = 2.9                  # words a second: brisk and clear, like a race engineer on the radio
SPEEDS = (0.85, 0.90, 0.95, 1.00, 1.05, 1.10, 1.15, 1.20, 1.25, 1.30)   # voices differ a lot in pace
CALIBRATE = ["Brake marker, turn four.", "Turn three, carry more speed.", "Down three tenths.",
             "New best, up two tenths.", "Hold the brake in."]

PRESETS = {   # the voices that come with Debrief (owner's choice); engineer is the default
    "engineer": ("bm_george", "George (British male)"),
    "michael": ("am_michael", "Michael (American male)"),
    "fenrir": ("am_fenrir", "Fenrir (American male)"),
    "heart": ("af_heart", "Heart (American female)"),
    "kumo": ("jm_kumo", "Kumo (Japanese accent, male)"),
    "omega": ("hm_omega", "Omega (Indian accent, male)"),
}


def lang_for(voice: str) -> str:
    """en-gb for Kokoro's British voices; every other voice (American, and the accented ones) speaks en-us."""
    return "en-gb" if voice[:2] in ("bm", "bf") else "en-us"


def syllables(text: str) -> int:
    """Rough syllable count (vowel groups per word), for the speaking-rate check."""
    import re
    n = 0
    for w in re.findall(r"[a-z]+", text.lower()):
        k = len(re.findall(r"[aeiouy]+", w))
        if w.endswith("e") and not w.endswith(("le", "ee")) and k > 1:
            k -= 1
        n += max(1, k)
    return max(1, n)


PART_OVERHEAD_S = 0.1
PART_RANGE = (0.45, 2.2)          # a cut part's syllable rate against the voice's usual one


def part_rate(words: str, x, sr) -> float:
    b = vp.speech_bounds(x, sr)
    secs = (b[1] - b[0]) / sr if b else 0.0
    return syllables(words) / max(secs - PART_OVERHEAD_S, 0.05)


def cut_ok(parts, text, sr, ref_rate) -> bool:
    """Both halves of a comma cut sound about as long as their words (a dip inside a word is not the comma)."""
    for role in ("lead", "tail"):
        r = part_rate(parts_text(text, role), parts[role], sr) / ref_rate
        if not PART_RANGE[0] <= r <= PART_RANGE[1]:
            return False
    return True


def flat_tops(x) -> int:
    """Samples in a clipped (flattened) peak: runs of 3+ equal samples above 0.9. Kokoro's own output may go a
    little over 1.0 without being clipped (it is levelled afterwards), so the peak level alone says nothing."""
    a = np.abs(np.asarray(x, dtype=np.float64))
    flat = (a[1:-1] >= 0.9) & (np.abs(a[1:-1] - a[:-2]) < 1e-6) & (np.abs(a[2:] - a[1:-1]) < 1e-6)
    return int(flat.sum())


def check_takes(takes) -> list:
    """Takes that look wrong: [(line, text, reason)]. takes: [{"n", "text", "said", "peak", "hot", "rate",
    "parts": [(key, words, secs)]}]. Flags no speech, clipping, and a speaking rate (syllables a second) far from
    the pack's usual one, for the whole line or for a cut part (garbled words, a cut in the wrong place)."""
    rates = [t["rate"] for t in takes if t.get("rate")]
    med = float(np.median(rates)) if rates else 0.0
    bad = []
    for t in takes:
        why = []
        if not t.get("rate"):
            why.append("no speech")
        else:
            if t["hot"] >= 3:
                why.append(f"clipped ({t['hot']} flattened samples at the peaks)")
            r = t["rate"] / med if med else 1.0
            if not 0.6 <= r <= 1.6:
                why.append(f"odd length for its words ({t['rate']:.1f} syllables/s, usual {med:.1f})")
            for key, words, secs in t["parts"]:
                pr = syllables(words) / max(secs - PART_OVERHEAD_S, 0.05) / med if med else 1.0
                if not PART_RANGE[0] <= pr <= PART_RANGE[1]:
                    why.append(f"{key} ('{words}') is {secs:.2f} s, odd for its words")
        if t["said"] != t["text"]:
            why.append(f"spoken as '{t['said']}' to get a pause at the comma (listen to the join)"
                       if "|" not in t["said"] else "the two halves were said separately (listen to the join)")
        if why:
            bad.append((t["n"], t["text"], "; ".join(why)))
    return bad


def words_per_s(x, sr, text) -> float:
    b = vp.speech_bounds(x, sr)
    secs = (b[1] - b[0]) / sr if b else 1.0
    return len(text.replace(",", " ").split()) / max(secs, 0.1)


def pick_speed(tts, voice, lang, log) -> float:
    best = None
    for sp in SPEEDS:
        rates = [words_per_s(*tts(t, voice, sp, lang), t) for t in CALIBRATE]
        wps = float(np.median(rates))
        log(f"  speed {sp:.2f}: {wps:.2f} words a second")
        if best is None or abs(wps - TARGET_WPS) < abs(best[1] - TARGET_WPS):
            best = (sp, wps)
    return best[0]


def make_tts(model: Path, voices: Path):
    from kokoro_onnx import Kokoro
    k = Kokoro(str(model), str(voices))

    def tts(text, voice, speed, lang):
        a, sr = k.create(text, voice=voice, speed=speed, lang=lang)
        return np.asarray(a, dtype=np.float64), int(sr)
    return tts


def spoken_variants(text: str, roles) -> list:
    """What Kokoro is asked to say for a line: the line itself; where it has to be cut at the comma, also with a
    full stop or a "..." there (Kokoro sometimes runs "Good, zero" together without a pause to cut at)."""
    if roles == {"whole"} or ", " not in text:
        return [text]
    head, _, tail = text.partition(", ")
    return [text, f"{head}. {tail[:1].upper()}{tail[1:]}", f"{head}... {tail}"]


def parts_text(text: str, role: str) -> str:
    if role == "whole":
        return text
    head, _, tail = text.partition(",")
    return head + "," if role == "lead" else tail.strip()


def build(tts, voice, lang, pack_id, name, out, rate=22050, speed=None, credit=None, log=print) -> dict:
    t0 = time.time()
    log(f"{name}: voice {voice}, {lang}, into {out}")
    if speed is None:
        speed = pick_speed(tts, voice, lang, log)
    log(f"  using speed {speed:.2f}")
    ref_rate = float(np.median([part_rate(parts_text(t, "whole"), *tts(t, voice, speed, lang)) for t in CALIBRATE]))
    clips, gaps, problems, takes = {}, [], [], []
    lines = vp.lines()
    for i, ln in enumerate(lines):
        roles = {r for _k, r in ln["clips"]}
        parts = None
        tries = [(said, db) for db in (-40.0, -32.0) for said in spoken_variants(ln["text"], roles)]
        for said, db in tries:
            x, sr = tts(said, voice, speed, lang)
            try:
                parts = vp.cut_line(x, sr, roles, rel_db=db)
            except ValueError:
                continue
            if "_gap_ms" not in parts or cut_ok(parts, ln["text"], sr, ref_rate):
                break
            parts = None
        if parts is None and "whole" not in roles:
            # no pause anywhere: the two halves said on their own
            head, _, tail = ln["text"].partition(", ")
            said = f"{head}, | {tail[:1].upper()}{tail[1:]}"
            xl, sr = tts(head + ",", voice, speed, lang)
            xt, sr = tts(tail[:1].upper() + tail[1:], voice, speed, lang)
            parts = {"lead": xl, "tail": xt}
            x = np.concatenate([xl, xt])
        if parts is None:
            problems.append(f"line {ln['n']} '{ln['text']}': no clear pause at the comma")
            continue
        if "_gap_ms" in parts:
            gaps.append(parts["_gap_ms"])
        b = vp.speech_bounds(x, sr)
        take = {"n": ln["n"], "text": ln["text"], "said": said, "peak": float(np.abs(x).max()) if x.size else 0.0,
                "hot": flat_tops(x), "parts": [],
                "rate": syllables(ln["text"]) / max(0.05, (b[1] - b[0]) / sr - OVERHEAD_S) if b and b[1] > b[0] else 0.0}
        for key, role in ln["clips"]:
            y = vp.finish(vp.resample(parts[role], sr, rate), rate)
            if y.size == 0:
                problems.append(f"line {ln['n']}: {key} came out empty")
                continue
            clips[key] = (y, parts_text(ln["text"], role), role)
            if role != "whole":
                pb = vp.speech_bounds(y, rate)
                take["parts"].append((key, parts_text(ln["text"], role), (pb[1] - pb[0]) / rate if pb else 0.0))
        takes.append(take)
        if (i + 1) % 25 == 0:
            log(f"  {i + 1}/{len(lines)} lines ({time.time() - t0:.0f} s)")
    pad2 = 2 * vp.PAD_MS
    join = float(np.clip(np.median(gaps) - pad2, 40, 250)) if gaps else vp.JOIN_MS
    meta = {"id": pack_id, "name": name, "voice": voice, "lang": lang, "speed": speed,
            "voice_credit": credit or f"Kokoro voice {voice}", "license": LICENSE,
            "loudness_db": vp.TARGET_DB, "generator": "tools/make_voicepack.py"}
    vp.write_pack(out, meta, clips, rate, join)
    pack = vp.load_pack(out, strict=True)
    prev = vp.preview(pack)
    vp.write_pack(out, meta, clips, rate, join, preview=prev)
    pack = vp.load_pack(out, strict=True)
    missing = sorted(set(vp.all_keys()) - set(pack.clips))
    cov = vp.coverage(coach_vocabulary(), pack)
    size = vp.pack_bytes(out)
    report = [f"{name} ({pack_id}): {len(pack.clips)} clips, {size / 1e6:.2f} MB, {rate} Hz, speed {speed:.2f}, "
              f"comma pause {join:.0f} ms",
              f"Coach phrases covered: {len(cov['covered'])} of {len(cov['covered']) + len(cov['missing'])} "
              f"({100 * cov['share']:.1f}%)"]
    report += [f"  not covered: {p}" for p in cov["missing"]]
    report += [f"  clip missing: {k}" for k in missing] + [f"  problem: {p}" for p in problems]
    qa = check_takes(takes)
    report.append(f"Takes to listen to: {len(qa)}")
    report += [f"  line {n} '{t}': {why}" for n, t, why in qa]
    (Path(out) / "coverage.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
    for r in report:
        log(r)
    log(f"  done in {time.time() - t0:.0f} s")
    return {"clips": len(pack.clips), "bytes": size, "coverage": cov["share"], "missing": missing,
            "problems": problems, "qa": qa}


def coach_vocabulary() -> list:
    """Every phrase the live coach and the practice plan can say, for corners T1..T25 (as Coach.vocabulary and
    practiceplan.cue_vocabulary build them)."""
    from debrief import cornercoach, livecoach, practiceplan
    out = []
    for n in range(1, vp.MAX_TURN + 1):
        name = f"T{n}"
        out += [livecoach.marker_text(name), livecoach.early_text(name)]
        out += [livecoach.speed_text(name, k) for k in range(4, 16)]
        out += [f"{name}: {v}" for v in cornercoach.VOICE.values()]
        out += [f"{name}: hold it in", f"{name}: same every lap"]
    for k in range(1, 10):
        out += [f"Down {livecoach.delta_words(k / 10)}", f"New best, up {livecoach.delta_words(k / 10)}"]
    out += ["New best"]
    for m in ("peak_brake", "brake_point", "trail", "coast", "throttle_point", "min_speed", "corner_time"):
        out += practiceplan.cue_vocabulary({"metric": m})
    return list(dict.fromkeys(out))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--preset", help="one of " + ", ".join(PRESETS) + ", or 'all'")
    ap.add_argument("--voice", help="Kokoro voice id, e.g. bm_george")
    ap.add_argument("--lang", help="en-gb or en-us (default: en-gb for bm_/bf_ voices, else en-us)")
    ap.add_argument("--id", help="pack id (letters, digits, - and _)")
    ap.add_argument("--name", help="name shown in Debrief")
    ap.add_argument("--credit", help="voice credit shown in Debrief")
    ap.add_argument("--out", help="pack folder (default debrief/data/voicepacks/<id>)")
    ap.add_argument("--rate", type=int, default=22050, choices=vp.RATES)
    ap.add_argument("--speed", type=float, help="Kokoro speed (default: picked from words a second)")
    ap.add_argument("--model-dir", default=os.environ.get("KOKORO_DIR"))
    a = ap.parse_args(argv)
    if not a.model_dir:
        ap.error("give --model-dir (or set KOKORO_DIR) to the folder with kokoro.onnx and voices.bin")
    md = Path(a.model_dir)
    jobs = []
    if a.preset:
        ids = list(PRESETS) if a.preset == "all" else [a.preset]
        for i in ids:
            if i not in PRESETS:
                ap.error(f"unknown preset {i}")
            voice, name = PRESETS[i]
            jobs.append((voice, lang_for(voice), i, name, ROOT / "debrief" / "data" / "voicepacks" / i))
    else:
        if not (a.voice and a.id and a.name):
            ap.error("give --preset, or --voice, --id and --name")
        if not vp.ID_RE.match(a.id):
            ap.error("--id: letters, digits, - and _ only")
        jobs.append((a.voice, a.lang or lang_for(a.voice), a.id, a.name,
                     Path(a.out) if a.out else ROOT / "debrief" / "data" / "voicepacks" / a.id))
    tts = make_tts(md / "kokoro.onnx", md / "voices.bin")
    bad = 0
    for voice, lang, pid, name, out in jobs:
        r = build(tts, voice, lang, pid, name, out, rate=a.rate, speed=a.speed, credit=None)
        bad += bool(r["missing"] or r["problems"] or r["coverage"] < 1.0)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
