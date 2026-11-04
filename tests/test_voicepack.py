"""Coach 2.0 voice packs (voicepack.py, tools/import_recordings.py). Run: python tests/test_voicepack.py

No Windows, no sound card, no speech engine: packs are made from synthetic sound here, and the bundled packs
(debrief/data/voicepacks) are checked as files."""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from debrief import cornercoach, livecoach as lc, practiceplan as pp, voicepack as vp  # noqa: E402
import import_recordings as ir  # noqa: E402

fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


TMP = Path(tempfile.mkdtemp(prefix="voicepack-test-"))
RATE = 22050


def syllable_burst(n_syl, sr, seed, amp=0.3):
    """Speech-like sound: n_syl voiced bursts of a 140 Hz buzz with harmonics, each 160 ms with 40 ms gaps."""
    rng = np.random.default_rng(seed)
    parts = []
    for i in range(max(1, n_syl)):
        n = int(0.16 * sr)
        t = np.arange(n) / sr
        f0 = 140 + 20 * rng.random()
        s = sum(np.sin(2 * np.pi * f0 * k * t) / k for k in range(1, 8))
        env = np.sin(np.pi * np.arange(n) / n) ** 0.5
        parts += [s * env * amp * (0.5 + rng.random()), np.zeros(int(0.04 * sr))]
    return np.concatenate(parts[:-1])


def synth_line(ln, sr, seed, comma_s=0.18):
    """A spoken-like line: a pause at the first comma (comma_s), syllables from the words."""
    from make_voicepack import syllables
    text = ln["text"]
    if "," in text:
        head, _, tail = text.partition(",")
        return np.concatenate([syllable_burst(syllables(head), sr, seed), np.zeros(int(comma_s * sr)),
                               syllable_burst(syllables(tail), sr, seed + 7)])
    return syllable_burst(syllables(text), sr, seed)


def synthetic_pack(folder, keys=None, rate=RATE, pack_id="synthetic"):
    clips = {}
    for ln in vp.lines():
        parts = vp.cut_line(synth_line(ln, rate, ln["n"]), rate, {r for _k, r in ln["clips"]})
        for key, role in ln["clips"]:
            if keys is None or key in keys:
                clips[key] = (vp.finish(parts[role], rate), ln["text"], role)
    vp.write_pack(folder, {"id": pack_id, "name": "Synthetic", "voice": "test", "voice_credit": "test",
                           "license": "test"}, clips, rate, 90)
    return vp.load_pack(folder, strict=True)


def coach_phrases():
    """Coach.vocabulary() for a few fake references, with weak corners from reviews and from the corner coach."""
    out = []
    for n_corners, weak in ((6, [3, 5]), (12, [{"id": "C11", "apex_m": 11 * 300.0, "lost_s": 0.3,
                                                 "phrase": "T11: lighter brake", "source": "corner coach",
                                                 "rule": "lighter"}, 2]),
                            (25, [{"id": f"C{k}", "apex_m": k * 300.0, "lost_s": 0.1 * k,
                                   "phrase": f"T{k}: {v}"} for k, v in zip(range(14, 26), cornercoach.VOICE.values())])):
        L = 300.0 * (n_corners + 1)
        g = np.linspace(0, 1, lc.GRID, endpoint=False)
        corners = [lc.Corner(num=k, id=f"C{k}", name=f"T{k}", apex_pct=k * 300.0 / L, apex_m=k * 300.0,
                             min_kmh=90.0, start_rel_m=-120.0, end_rel_m=60.0, brake_pct=(k * 300.0 - 100) / L,
                             brake_m=k * 300.0 - 100, brake_rel_m=-100.0) for k in range(1, n_corners + 1)]
        ref = lc.Reference(lap_time=90.0, length_m=L, car={}, track={}, source={"kind": "test"}, pct=g,
                           speed=np.full(lc.GRID, 40.0), brake=np.zeros(lc.GRID), throttle=np.ones(lc.GRID),
                           gear=np.full(lc.GRID, 4), time=g * 90.0, corners=corners)
        co = lc.Coach(ref, weak, lc.Settings(top_n=3))
        voc = co.vocabulary()
        check(len(voc) > 20, f"fake reference with {n_corners} corners gives a vocabulary ({len(voc)} phrases)")
        out += voc
    for m in ("peak_brake", "brake_point", "trail", "coast", "throttle_point", "min_speed", "corner_time"):
        out += pp.cue_vocabulary({"metric": m})
    for k in range(1, 26):                                      # practice plan "voice" lines
        out += [f"T{k}: brake later", f"T{k}: brake earlier", f"T{k}: hold it in", f"T{k}: same every lap",
                f"T{k}: no coasting", f"T{k}: throttle earlier", f"T{k}: carry more speed", f"T{k}: lighter brake"]
    return list(dict.fromkeys(out))


# ---------------------------------------------------------------- compose
print("-- compose")
cases = {"Brake marker, T4": ["brake_marker_turn_4"], "T4: lighter brake": ["turn_4", "lighter_brake"],
         "T4, braked early": ["turn_4", "braked_early"], "T4: more speed, 7 down": ["turn_4", "more_speed_7_down"],
         "Down three tenths": ["down_0_3"], "Down a tenth": ["down_0_1"], "Down 1.4 seconds": ["down_1_4"],
         "New best": ["new_best"], "New best, up two tenths": ["new_best_lead", "up_0_2"],
         "Good, 84": ["good_lead", "num_84"], "Too much brake": ["too_much_brake"],
         "T3: trail into it": ["turn_3", "trail_into_it"], "T2: carry more speed": ["turn_2", "carry_more_speed"],
         "T1: brake later": ["turn_1", "brake_later"], "Throttle earlier": ["throttle_earlier"],
         "Good brake point": ["good_brake_point"], "Good, held it in": ["good_held_it_in"]}
for text, want in cases.items():
    check(vp.compose(text) == want, f"compose({text!r}) = {vp.compose(text)}")
for text in ("T26: brake later", "Brake marker, T0", "Down 2.1 seconds", "New best, up 4.5 seconds", "Good, 101",
             "T4: drive faster", "C3: brake later", "Hello", "", "T4: more speed, 30 down", "Down two tenth",
             "Down a tenths", "Good, 0", "Good, 19", None, 42, "x" * 200):
    check(vp.compose(text) is None, f"compose({text!r}) is None (falls back to Windows speech)")

# "Good, 0" .. "Good, 19" are left to Windows speech by design (a peak brake that low is a lift, not braking)
BY_DESIGN = {f"Good, {k}" for k in range(vp.MIN_NUM)}
phrases = [p for p in coach_phrases() if p not in BY_DESIGN]
check(len(phrases) > 400, f"coach phrases to cover: {len(phrases)}")
cov = vp.coverage(phrases)
check(cov["share"] == 1.0, f"the clip scheme covers every coach phrase ({len(cov['covered'])}/{len(phrases)}), "
                           f"missing {cov['missing'][:5]}")
sch = set(vp.all_keys())
used = {k for p in phrases for k in vp.compose(p) or []}
check(used <= sch, "every key compose() uses is a clip in lines()")
check(len(vp.lines()) == len({ln["n"] for ln in vp.lines()}) and vp.lines()[0]["n"] == 1, "line numbers are 1..n")

# ---------------------------------------------------------------- a synthetic pack: render, timing, clicks
print("-- render")
pk = synthetic_pack(TMP / "synthetic")
check(len(pk.clips) == len(sch), f"synthetic pack has every clip ({len(pk.clips)})")
check(pk.coverage(phrases)["share"] == 1.0, "synthetic pack covers 100% of the coach phrases")
for text in ("T4: lighter brake", "New best, up two tenths", "Good, 84", "Brake marker, T4"):
    keys = pk.compose(text)
    x = pk.render_samples(keys)
    pcm = pk.render(keys)
    check(len(pcm) == 2 * len(x), f"{text}: render gives 16-bit PCM")
    check(abs(pk.duration_ms(keys) - len(x) * 1000 / pk.rate) <= 1, f"{text}: duration_ms {pk.duration_ms(keys)} "
                                                                    f"matches the audio")
    if len(keys) > 1:
        a = len(pk.clips[keys[0]])
        gap = pk._gap(keys[0], keys[1])
        check(gap == int(round(pk.rate * 0.09)), f"{text}: the pack's comma pause is used between parts")
        jump = float(np.abs(np.diff(x[a - 3:a + gap - int(pk.rate * 0.008) + 3])).max())
        check(jump < 0.005, f"{text}: no click at the join (largest step {jump:.4f})")
        quiet = np.abs(x[a:a + gap - int(pk.rate * 0.008)]).max()
        check(quiet < 1e-3, f"{text}: silence in the pause")
check(abs(np.abs(pk.render_samples(["num_84"], volume=50)).max()
          - 0.5 * np.abs(pk.render_samples(["num_84"])).max()) < 1e-6, "volume scales the audio")
# crossfade with no gap: a lead with join 0
m = json.loads((TMP / "synthetic" / "manifest.json").read_text())
m["join_ms"] = 0
(TMP / "synthetic" / "manifest.json").write_text(json.dumps(m))
pk0 = vp.load_pack(TMP / "synthetic")
keys = ["turn_4", "lighter_brake"]
x0 = pk0.render_samples(keys)
check(abs(pk0.duration_ms(keys) - len(x0) * 1000 / pk0.rate) <= 1, "duration_ms right with a crossfaded join")
check(len(x0) < len(pk0.clips["turn_4"]) + len(pk0.clips["lighter_brake"]), "join 0 overlaps the edges (crossfade)")
check(pk0.compose("Down 1.4 seconds") == ["down_1_4"] and pk0.duration_ms(["nope"]) is None, "missing key -> None")
part = synthetic_pack(TMP / "partial", keys={"turn_4", "brake_later"}, pack_id="partial")
check(part.compose("T4: brake later") == ["turn_4", "brake_later"] and part.compose("T4: lighter brake") is None,
      "a pack without a clip returns None for that phrase (Windows speech says it)")

# ---------------------------------------------------------------- loudness and the processing chain
print("-- processing")
x = syllable_burst(4, RATE, 1, amp=0.05)
y = vp.finish(np.concatenate([np.zeros(RATE), x, np.zeros(RATE)]) + 0.02, RATE)
check(abs(vp.loudness_db(y, RATE) - vp.TARGET_DB) < 0.3, f"finish levels to the target ({vp.loudness_db(y, RATE):.2f})")
check(np.abs(y).max() <= 10 ** (vp.PEAK_DB / 20) + 1e-6, "peaks stay under -1 dBFS")
check(len(y) < len(x) + int(0.08 * RATE), "silence trimmed (30 ms kept each side)")
check(abs(y[0]) < 1e-4 and abs(y[-1]) < 1e-4, "fades at both ends")
loud = vp.finish(syllable_burst(3, RATE, 2, amp=0.99) * 3, RATE)
check(np.abs(loud).max() <= 10 ** (vp.PEAK_DB / 20) + 1e-6 and abs(vp.loudness_db(loud, RATE) - vp.TARGET_DB) < 1,
      "a far too loud take is limited and levelled")
r = vp.resample(syllable_burst(2, 24000, 3), 24000, 22050)
check(abs(len(r) - round(len(syllable_burst(2, 24000, 3)) * 22050 / 24000)) <= 1, "resample length")
click = syllable_burst(2, RATE, 4) * 0.2
click[5000] = 0.95
check(np.abs(vp.declick(click, RATE)).max() < 0.5, "de-click removes a one-sample spike")
hp = vp.highpass(np.sin(2 * np.pi * 30 * np.arange(RATE) / RATE), RATE, 80)
check(np.abs(hp[RATE // 4:-RATE // 4]).max() < 0.1, "high-pass removes 30 Hz rumble")

# ---------------------------------------------------------------- the bundled packs
print("-- bundled packs")
rows = vp.list_packs(None)
check(bool(rows) and rows[0]["id"] == vp.DEFAULT_PACK and rows[0]["default"], "the default pack (engineer) is first")
want = {"engineer": "bm_george", "michael": "am_michael", "fenrir": "am_fenrir", "heart": "af_heart",
        "kumo": "jm_kumo", "omega": "hm_omega"}
have = {r["id"]: r for r in rows}
for pid, voice in want.items():
    r = have.get(pid)
    check(bool(r) and r["ok"] and r["bundled"] and r["voice"] == voice, f"bundled pack {pid} ({voice})")
check(not (set(have) - set(want)), f"no other bundled packs ({sorted(set(have) - set(want))})")
total = 0
for pid in want:
    if pid not in have:
        continue
    pack = vp.load_pack(have[pid]["path"], strict=True)
    total += vp.pack_bytes(have[pid]["path"])
    c = pack.coverage(phrases)
    check(c["share"] == 1.0, f"{pid}: covers 100% of the coach's phrases (missing {c['missing'][:5]})")
    check(set(pack.clips) == sch, f"{pid}: has every clip ({len(pack.clips)})")
    lv = np.array([vp.loudness_db(x, pack.rate) for x in pack.clips.values()])
    check(np.abs(lv - vp.TARGET_DB).max() <= 1.0, f"{pid}: every clip within 1 dB of {vp.TARGET_DB} "
                                                   f"(range {lv.min():.2f} .. {lv.max():.2f})")
    pk_ = max(float(np.abs(x).max()) for x in pack.clips.values())
    check(pk_ <= 10 ** (vp.PEAK_DB / 20) + 1e-3, f"{pid}: peaks under -1 dBFS")
    edges = max(max(abs(float(x[0])), abs(float(x[-1]))) for x in pack.clips.values())
    check(edges < 0.002, f"{pid}: every clip starts and ends at silence")
    jumps = []
    for text in ("T4: lighter brake", "New best, up two tenths", "Good, 84", "T7: more speed, 6 down"):
        keys = pack.compose(text)
        x = pack.render_samples(keys)
        a = len(pack.clips[keys[0]])
        g = pack._gap(keys[0], keys[1]) - int(pack.rate * vp.XFADE_MS / 1000)
        jumps.append(float(np.abs(np.diff(x[a - 3:a + g + 3])).max()))
        check(abs(pack.duration_ms(keys) - len(x) * 1000 / pack.rate) <= 1, f"{pid}: {text} duration")
    check(max(jumps) < 0.005, f"{pid}: no clicks at joins ({max(jumps):.4f})")
    secs = [len(x) / pack.rate for x in pack.clips.values()]
    check(0.15 < min(secs) and max(secs) < 4.0, f"{pid}: clip lengths sensible ({min(secs):.2f} .. {max(secs):.2f} s)")
check(total < 125e6, f"bundled packs total {total / 1e6:.1f} MB (2.20: 560 clips each, about 19 MB a voice)")

# ---------------------------------------------------------------- manifest validation
print("-- manifest checks")
base = json.loads((TMP / "partial" / "manifest.json").read_text())


def bad_manifest(mutate, label):
    d = TMP / f"bad-{label}"
    shutil.copytree(TMP / "partial", d)
    m = json.loads(json.dumps(base))
    mutate(m, d)
    (d / "manifest.json").write_text(json.dumps(m))
    try:
        p = vp.load_pack(d, strict=True)
        check(False, f"{label}: refused (loaded {len(p.clips)} clips)")
    except vp.VoicePackError as e:
        check(True, f"{label}: refused ({e.problems[0][:70]})")


bad_manifest(lambda m, d: m["clips"]["turn_4"].update(file="../turn_4.wav"), "path traversal")
bad_manifest(lambda m, d: m["clips"]["turn_4"].update(file="C:evil.wav"), "drive letter")
bad_manifest(lambda m, d: m["clips"]["turn_4"].update(file="sub/turn_4.wav"), "sub folder")
bad_manifest(lambda m, d: m["clips"]["turn_4"].update(file="turn_4.exe"), "not a wav name")
bad_manifest(lambda m, d: m["clips"].update({"Bad Key!": {"file": "x.wav"}}), "bad clip name")
bad_manifest(lambda m, d: m.update(id="../up"), "bad id")
bad_manifest(lambda m, d: m.update(sample_rate=12345), "odd sample rate")
bad_manifest(lambda m, d: m.update(clips={}), "no clips")
bad_manifest(lambda m, d: m.update(format=99), "newer format")
bad_manifest(lambda m, d: (d / "turn_4.wav").write_bytes(b"MZ" + b"\0" * 200), "a program renamed .wav")
bad_manifest(lambda m, d: (d / "turn_4.wav").write_bytes(vp.wav_bytes(b"\0" * (vp.MAX_CLIP_BYTES + 10), RATE)),
             "oversized clip")
bad_manifest(lambda m, d: (d / "turn_4.wav").write_bytes(vp.wav_bytes(b"\0\0" * 100, 44100)), "wrong sample rate")


def stereo(m, d):
    import wave
    with wave.open(str(d / "turn_4.wav"), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"\1\0" * 2000)


bad_manifest(stereo, "stereo clip")
d = TMP / "lenient"
shutil.copytree(TMP / "partial", d)
(d / "turn_4.wav").unlink()
p = vp.load_pack(d)
check("brake_later" in p.clips and "turn_4" not in p.clips and p.problems, "a missing clip is left out, not fatal")
check(p.compose("T4: brake later") is None, "...and its phrases fall back")
try:
    vp.load_pack(TMP / "nothing-here")
    check(False, "no manifest refused")
except vp.VoicePackError:
    check(True, "no manifest refused")
check(vp.check_manifest([]) != [] and vp.check_manifest({"id": "a"}) != [], "check_manifest refuses junk")

# ---------------------------------------------------------------- importing a pack he was given
print("-- import_pack")
users = TMP / "user-packs"


def make_zip(name, members):
    z = TMP / name
    with zipfile.ZipFile(z, "w") as zf:
        for n, data in members.items():
            zf.writestr(n, data)
    return z


good = {n.name: n.read_bytes() for n in (TMP / "partial").iterdir() if n.is_file()}
gm = json.loads(good["manifest.json"])
gm["id"] = "mine"
good["manifest.json"] = json.dumps(gm).encode()
res = vp.import_pack(make_zip("good.zip", good), users)
check(res["ok"] and (users / "mine" / "manifest.json").is_file(), f"a good zip installs ({res['problems']})")
check(any(r["id"] == "mine" and not r["bundled"] and r["ok"] for r in vp.list_packs(users)), "listed as his pack")
check(vp.find_pack("mine", users).id == "mine", "find_pack loads it")
check(vp.find_pack("no-such", users) is None or vp.find_pack("no-such", users).id == vp.DEFAULT_PACK,
      "an unknown id gives the default pack")
res = vp.import_pack(make_zip("good2.zip", good), users)
check(not res["ok"] and "already installed" in res["problems"][0], "the same id twice is refused")
res = vp.import_pack(make_zip("good3.zip", good), users, replace=True)
check(res["ok"] and any(p.name.startswith("mine.old-") for p in users.iterdir()), "replace keeps the old one aside")
folder_in_zip = {f"mypack/{k}": v for k, v in good.items()}
fm = json.loads(good["manifest.json"])
fm["id"] = "zipped-folder"
folder_in_zip["mypack/manifest.json"] = json.dumps(fm).encode()
check(vp.import_pack(make_zip("folder.zip", folder_in_zip), users)["ok"], "a zipped folder installs too")
eng = dict(good)
em = json.loads(good["manifest.json"])
em["id"] = "engineer"
eng["manifest.json"] = json.dumps(em).encode()
res = vp.import_pack(make_zip("eng.zip", eng), users)
check(not res["ok"], "a pack using a bundled pack's id is refused")
nasty = {"traversal": {"../evil.wav": good["turn_4.wav"]}, "absolute": {"/etc/evil.wav": good["turn_4.wav"]},
         "exe": {"setup.exe": b"MZ" + b"\0" * 100}, "hidden exe": {"clip.exe.wav": good["turn_4.wav"]},
         "script": {"x.ps1": b"Write-Host"}, "text": {"notes.txt": b"hi"}, "fake wav": {"fake.wav": b"MZ" + b"\0" * 99},
         "nested zip": {"inner.wav": b"PK\3\4" + b"\0" * 99}, "sub folder": {"a/b.wav": good["turn_4.wav"]}}
for label, extra in nasty.items():
    res = vp.import_pack(make_zip(f"{label}.zip", {**good, **extra}), users / "x")
    check(not res["ok"] and not (users / "x" / "mine").exists(), f"zip with {label}: refused "
                                                                  f"({(res['problems'] or ['?'])[0][:60]})")
check(not (TMP / "evil.wav").exists() and not Path("/etc/evil.wav").exists(), "nothing written outside")
big = make_zip("bomb.zip", {**good, "big.wav": b"RIFF" + b"\0" * (vp.MAX_CLIP_BYTES * 3)})
check(not vp.import_pack(big, users / "y")["ok"], "an oversized member is refused")
nom = {k: v for k, v in good.items() if k != "manifest.json"}
check(not vp.import_pack(make_zip("nomanifest.zip", nom), users / "z")["ok"], "no manifest refused")
check(not vp.import_pack(TMP / "missing.zip", users)["ok"], "a missing file is refused, not a crash")
src = TMP / "folder-src"
src.mkdir()
for k, v in good.items():
    (src / k).write_bytes(v)
fm2 = json.loads(good["manifest.json"])
fm2["id"] = "from-folder"
(src / "manifest.json").write_text(json.dumps(fm2))
check(vp.import_pack(src, users)["ok"], "a pack folder installs")
(src / "run.bat").write_text("@echo off")
fm2["id"] = "from-folder2"
(src / "manifest.json").write_text(json.dumps(fm2))
check(not vp.import_pack(src, users)["ok"], "a folder with a .bat is refused")
check(not any(p.name.startswith(".incoming") for p in users.iterdir()), "no temporary folders left behind")

# ---------------------------------------------------------------- his own recordings
print("-- import_recordings")
SR = 16000
lines = vp.lines()
rec, starts = [np.zeros(int(0.8 * SR))], []
rng = np.random.default_rng(5)
pos = len(rec[0])
for ln in lines:
    s = synth_line(ln, SR, 1000 + ln["n"], comma_s=0.2)
    starts.append(pos)
    rec += [s, np.zeros(int((1.0 + 0.3 * rng.random()) * SR))]
    pos += len(s) + len(rec[-1])
long = np.concatenate(rec)
long = long + rng.normal(0, 0.001, len(long)) + 0.01 * np.sin(2 * np.pi * 50 * np.arange(len(long)) / SR)
pieces = ir.split_long(long, SR)
check(len(pieces) == len(lines), f"a long recording splits into one piece per line ({len(pieces)}/{len(lines)})")
check(all(abs(a - s) < 0.12 * SR for (a, _b), s in zip(pieces, starts)), "pieces start where the lines start")
wav = TMP / "long.wav"
vp.write_wav(wav, long, SR)
takes, report, problems = ir.collect([str(wav)])
check(not problems and len(takes) == len(lines), f"collect: every line found ({len(takes)}, {problems[:1]})")
out = TMP / "recorded" / "me"
r = ir.build_pack(takes, out, "me", "Me", "Tester")
check(r["clips"] == len(sch) and not r["missing"] and not r["problems"], f"recorded pack has every clip "
                                                                         f"({r['clips']}, {r['problems'][:1]})")
me = vp.load_pack(out, strict=True)
check(me.manifest["license"] == "Recorded by Tester" and me.rate == ir.RATE, "credit and rate")
check(me.coverage(phrases)["share"] == 1.0, "recorded pack covers 100%")
lv = np.array([vp.loudness_db(x, me.rate) for x in me.clips.values()])
check(np.abs(lv - vp.TARGET_DB).max() <= 1.0, f"recorded clips levelled like the bundled ones ({lv.min():.2f}..{lv.max():.2f})")
hum = max(abs(np.fft.rfft(x)[int(50 * len(x) / me.rate)]) / len(x) for x in me.clips.values())
check(hum < 0.004, f"mains hum filtered out ({hum:.4f})")
# a skipped line: the count is off and nothing lines up
skip = np.concatenate([long[:starts[40] - int(0.3 * SR)], long[starts[41] - int(0.3 * SR):]])
vp.write_wav(TMP / "skip.wav", skip, SR)
_t, rep2, prob2 = ir.collect([str(TMP / "skip.wav")])
check(prob2 and "lines were expected" in prob2[0], "a skipped line is reported, not silently misaligned")
rc = ir.main(["--long", str(TMP / "skip.wav"), "--id", "skip", "--name", "Skip", "--by", "T",
              "--out", str(TMP / "skipped" / "skip")])
check(rc == 1 and not (TMP / "skipped" / "skip" / "manifest.json").exists(), "...and nothing is written")
check((TMP / "skipped" / "skip-import-report.txt").is_file(), "...but the report is")
# in two parts, plus one fixed line as its own file
cut = starts[60] - int(0.3 * SR)
vp.write_wav(TMP / "p1.wav", long[:cut], SR)
vp.write_wav(TMP / "p2.wav", long[cut:], SR)
fix = TMP / "fixes"
fix.mkdir()
vp.write_wav(fix / "007 brake marker seven.wav", synth_line(lines[6], 44100, 77), 44100)
takes, rep3, prob3 = ir.collect(["1:" + str(TMP / "p1.wav"), "61:" + str(TMP / "p2.wav")], folder=fix)
check(not prob3 and len(takes) == len(lines) and takes[7][2].startswith("007"), "two parts and a fix file line up")
check(ir.line_number("12 turn one.wav") == 12 and ir.line_number("take.wav") is None, "line numbers from names")
check(ir.parse_long("59:x.wav") == (59, "x.wav") and ir.parse_long(r"C:\rec\x.wav") == (1, r"C:\rec\x.wav"),
      "START:file parsing (and Windows paths)")
try:
    ir.read_audio(TMP / "x.mp3", ffmpeg=str(TMP / "no-ffmpeg"))
    check(ir.find_ffmpeg() is not None, "mp3 read with ffmpeg present")
except ir.ImportError_ as e:
    check("WAV" in str(e) or "could not read" in str(e), f"no ffmpeg: says to use WAV ({str(e)[:60]})")
import struct  # noqa: E402
for bits, tag in ((24, 1), (32, 3)):
    x = synth_line(lines[0], 48000, 3)
    if tag == 3:
        data = x.astype("<f4").tobytes()
    else:
        v = np.clip(np.round(x * 8388607), -8388608, 8388607).astype(np.int32)
        data = np.stack([v & 255, (v >> 8) & 255, (v >> 16) & 255], 1).astype(np.uint8).tobytes()
    w = (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE" + b"fmt "
         + struct.pack("<IHHIIHH", 16, tag, 1, 48000, 48000 * bits // 8, bits // 8, bits)
         + b"data" + struct.pack("<I", len(data)) + data)
    y, sr, _i = vp.read_wav_bytes(w)
    check(sr == 48000 and np.abs(y - x).max() < 1e-3, f"reads {bits}-bit {'float' if tag == 3 else 'PCM'} WAV")
script = (ROOT / "docs" / "voice-recording-script.md").read_text(encoding="utf-8")
check(all(f"{ln['n']}. {ln['text']}" in script for ln in lines), "the recording script lists every line, numbered")
check("\u2014" not in script and "\u2013" not in script, "no long dashes in the recording script")

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'all ok' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
