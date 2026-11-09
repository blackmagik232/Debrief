"""Voice packs for the live coach (Coach 2.0): recorded or pre-generated speech clips instead of Windows' robotic
voice. Pure logic on files: no Windows, no sound card, no speech engine. numpy only.

A pack is a folder:

    manifest.json   {"format": 1, "id", "name", "voice", "voice_credit", "license", "sample_rate", "join_ms",
                     "version", "clips": {key: {"file", "text", "ms", "role"}}, "preview": "preview.wav"?}
    <key>.wav       16-bit mono PCM, one per clip, all at sample_rate

The coach only ever says a small fixed vocabulary ("Brake marker, T4", "T4: lighter brake", "Down three tenths",
"Good, 84" ...). compose(text) reads one of those exact strings and returns the clip keys that say it, or None
when the text is not something a pack can say (the caller then falls back to Windows speech). Most cues are one
whole recorded phrase ("Brake marker, turn four"); where the combinations would explode, a phrase is joined from a
LEAD clip cut at the comma of a real spoken sentence ("Turn four,") and a TAIL clip cut from the other side of the
same kind of sentence ("lighter brake."), so the join keeps the speaker's own pause and intonation.

    pack = load_pack(folder)                 # VoicePack; raises VoicePackError when the manifest is unusable
    keys = pack.compose("T4: lighter brake")  # ["turn_4", "lighter_brake"] or None
    pcm = pack.render(keys, volume=90)        # 16-bit mono PCM bytes at pack.rate
    pack.duration_ms(keys)                    # exact length of that audio
    wav = wav_bytes(pcm, pack.rate)           # a complete .wav file in memory (or write_wav(path, pcm, rate))

list_packs(user_dir) lists the bundled packs (debrief/data/voicepacks) and his own (user_dir); import_pack()
checks and installs a pack folder or zip he was given. lines() is the full list of sentences a pack is made from:
the generator (tools/make_voicepack.py), the recording script (docs/voice-recording-script.md) and the importer
for his own recordings (tools/import_recordings.py) all use it, so they always agree.
"""
from __future__ import annotations

import json
import re
import shutil
import struct
import tempfile
import time
import zipfile
from pathlib import Path

import numpy as np

FORMAT = 1
MANIFEST = "manifest.json"
DEFAULT_PACK = "engineer"
BUNDLED_DIR = Path(__file__).resolve().parent / "data" / "voicepacks"

RATES = (16000, 22050, 24000, 32000, 44100, 48000)
MAX_CLIP_BYTES = 2 * 1024 * 1024          # one clip: about 40 s at 24 kHz, far more than any cue
MAX_PACK_BYTES = 60 * 1024 * 1024         # a whole pack, all files
MAX_CLIPS = 2000
MAX_MANIFEST_BYTES = 512 * 1024
MAX_TEXT = 200
KEY_RE = re.compile(r"^[a-z0-9_]{1,64}$")
FILE_RE = re.compile(r"^[a-z0-9_\-]{1,64}\.wav$")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,39}$")
ROLES = ("whole", "lead", "tail")

# sound processing targets (shared by the generator and the importer, so every pack is equally loud)
TARGET_DB = -20.0          # gated RMS in dBFS; about -18 LUFS for speech
PEAK_DB = -1.0             # no sample above this
PAD_MS = 30                # silence kept before and after the speech when trimming
FADE_MS = 6                # fade in/out on every clip
XFADE_MS = 8               # crossfade at a join
JOIN_MS = 90               # default extra pause between a lead clip and the tail after it
EDGE_QUIET = 1e-3          # a clip edge this quiet has already been faded (no second fade at a join)

MAX_TURN = 25
MIN_NUM = 20               # "Good, 84": brake % 20..100 (a pass under 20% is a lift, not braking: Windows
MAX_NUM = 100              # speech says those few)
MIN_KMH = 4                # "more speed, 7 down": 4..20 (lines B; 2.17.2: the number is mph)
MAX_KMH = 20
MIN_MPH = 3                # 2.17.2: "more speed, 3 down" (3 mph is the coach's trigger), line K
MAX_TENTHS = 20            # lap deltas: a tenth .. 2.0 seconds
LEARN_SPEEDS = (3, 20)     # 2.20 "Turn five, three down on your best": the number is mph (or km/h), 3..20
LEARN_METRES = tuple(range(10, 65, 5))   # 2.20 "braked fifteen metres later": 10 .. 60 m in steps of 5
LAP_TENTHS = (400, 1800)   # 2.17.2 "Reference set, one thirty-eight nine": lap times 0:40.0 .. 3:00.9 (in tenths)


class VoicePackError(Exception):
    def __init__(self, problems):
        self.problems = list(problems) if isinstance(problems, (list, tuple)) else [str(problems)]
        super().__init__("; ".join(self.problems))


# ---------------------------------------------------------------- words

_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
         "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


def number_words(n: int) -> str:
    """0..999 in words: 84 -> 'eighty-four', 100 -> 'one hundred'."""
    n = int(n)
    if not 0 <= n <= 999:
        raise ValueError(n)
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10] + ("-" + _ONES[n % 10] if n % 10 else "")
    rest = n % 100
    return _ONES[n // 100] + " hundred" + (" and " + number_words(rest) if rest else "")


def amount_key(tenths: int) -> str:
    return f"{tenths // 10}_{tenths % 10}"


def amount_words(tenths: int) -> str:
    """3 -> 'three tenths', 1 -> 'a tenth', 10 -> 'one second', 14 -> 'one point four seconds'."""
    if tenths == 1:
        return "a tenth"
    if tenths < 10:
        return f"{_ONES[tenths]} tenths"
    s, t = divmod(tenths, 10)
    if t == 0:
        return f"{_ONES[s]} second" + ("s" if s > 1 else "")
    return f"{_ONES[s]} point {_ONES[t]} seconds"


def lap_time_words(seconds: float) -> str:
    """A lap time the way an engineer says it, to the tenth: 98.874 -> 'one thirty-eight nine', 65.04 ->
    'one oh five oh', 52.3 -> 'fifty-two three'."""
    n = int(round(float(seconds) * 10))
    m, rest = divmod(n, 600)
    sec, tenth = divmod(rest, 10)
    tw = "oh" if tenth == 0 else _ONES[tenth]
    if m == 0:
        return f"{number_words(sec)} {tw}"
    return f"{number_words(m)} {_sec_words(sec)} {tw}"


def _sec_words(sec: int) -> str:
    return "oh oh" if sec == 0 else f"oh {_ONES[sec]}" if sec < 10 else number_words(sec)


def lap_time_short_words(seconds: float) -> str:
    """2.17.2 a lap time the short way race engineers read it, without the minute: 98.874 -> 'thirty-eight nine',
    65.04 -> 'oh five oh'; under a minute it is the same as lap_time_words (52.3 -> 'fifty-two three')."""
    n = int(round(float(seconds) * 10))
    m, rest = divmod(n, 600)
    sec, tenth = divmod(rest, 10)
    if m == 0:
        return lap_time_words(seconds)
    return f"{_sec_words(sec)} {'oh' if tenth == 0 else _ONES[tenth]}"


def _lap_parts(tenths: int):
    """(minutes-and-seconds words, tenth word) for a lap time in tenths of a second."""
    w = lap_time_words(tenths / 10)
    head, _, tail = w.rpartition(" ")
    return head, tail


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


# corner advice said after "Turn N," (cornercoach.VOICE, practiceplan "voice", livecoach early_text)
TAILS = ["carry more speed", "lighter brake", "brake later", "brake earlier", "trail into it", "no coasting",
         "ease off slower", "throttle earlier", "smoother throttle", "smoother brake", "hold it in",
         "same every lap", "braked early"]
# whole phrases said on their own (practiceplan progress cues, the lap cue "New best")
WHOLES = ["Good brake point", "Good, held it in", "Good, no coasting", "Good throttle", "Good speed",
          "Good, on pace", "Too much brake", "Braked too early", "Braked too late", "Hold the brake in",
          "Less coasting", "More speed", "New best", "Building your reference"]

SECTIONS = {
    "A": "Brake markers. One clear phrase, a small pause at the comma.",
    "B": "Corner tips. Say the turn, a short natural pause at the comma, then the tip.",
    "C": "Lap times, slower than your best. Flat and matter-of-fact.",
    "D": "Lap times, a new best. A little lift, but stay calm. Short pause at the comma.",
    "E": "Brake pressure. First 'Good, eighty-four' with a short pause at the comma, then each number on its own.",
    "F": "Short practice cues. Each one on its own.",
    "G": "Time in a corner or a sector. Say the turn or sector, a short pause at the comma, then the time.",
    "H": "Lap times. Like reading a stopwatch: a short pause at the comma before the last number.",
    "I": "Your pace, when you ask for it. Matter-of-fact, a short natural pause at the comma.",
    "J": "Short lap times, without the minute. Like reading a stopwatch: a short pause at the comma.",
    "K": "One more corner tip. Say the turn, a short natural pause at the comma, then the tip.",
    "L": "Learning the track, against your best so far. Say the turn, a short natural pause at the comma, then the rest.",
    "M": "Crew chief on the radio. Calm and clear, like an engineer telling you what to do next. Numbers are litres, "
         "gallons, laps or tenths of a second. A short natural pause at the comma.",
}
PACE_WHOLES = ["No clean lap yet", "Down more than two seconds"]      # 2.17.2 answers to "How's my pace?"


# ---------------------------------------------------------------- 2.29 crew chief (fixed phrases and numbers)
CREW_WHOLES = ["Data lost", "Data back", "Fuel critical", "Box this lap", "Box next lap", "Box now, cheap stop",
               "Car alongside", "Car ahead pitting", "On plan", "Ahead of plan", "Behind plan", "Up a place",
               "Lost a place", "Fuel is tight", "Short of the flag", "Save fuel or box next lap"]
CREW_BOX_IN = (2, 5)         # "Box in three laps"
CREW_LITRES = (1, 120)       # "Forty-two litres"
CREW_GALLONS = (1, 32)       # "Eleven gallons"
CREW_FUEL_LAPS = (1, 2)      # "Two laps of fuel"
CREW_RATE = (1, 20)          # "three tenths a lap" (a tenth .. two seconds)
CREW_OFF = (3, 20)           # "three tenths off target"
CREW_LEADS = ("Car behind closing", "Gaining on the car ahead")


def _cap(w: str) -> str:
    return w[:1].upper() + w[1:]


def _unit_words(n: int, one: str, many: str) -> str:
    return f"{_cap(number_words(n))} {one if n == 1 else many}."


def crew_litres_text(n: int) -> str:
    return _unit_words(n, "litre", "litres")


def crew_gallons_text(n: int) -> str:
    return _unit_words(n, "gallon", "gallons")


def crew_laps_fuel_text(n: int) -> str:
    return f"{_cap(number_words(n))} {'lap' if n == 1 else 'laps'} of fuel."


def crew_rate_text(k: int) -> str:
    return f"{amount_words(k)} a lap"


def crew_off_text(k: int) -> str:
    return f"{_cap(amount_words(k))} off target."


def lines() -> list:
    """Every sentence a pack is made from, in reading order: [{"n", "section", "text", "clips": [(key, role)]}].
    role "whole": the whole sentence is the clip; "lead": the part before the first comma pause; "tail": the part
    after it. Pure and fixed: line numbers never change for a given FORMAT."""
    out = []

    def add(section, text, clips):
        out.append({"n": len(out) + 1, "section": section, "text": text, "clips": clips})

    for n in range(1, MAX_TURN + 1):
        add("A", f"Brake marker, turn {number_words(n)}.", [(f"brake_marker_turn_{n}", "whole")])
    tails = [(_slug(t), t) for t in TAILS] + [(f"more_speed_{k}_down", f"more speed, {number_words(k)} down")
                                             for k in range(MIN_KMH, MAX_KMH + 1)]
    for i, (key, words) in enumerate(tails):
        turn = i % MAX_TURN + 1
        clips = ([(f"turn_{turn}", "lead")] if i < MAX_TURN else []) + [(key, "tail")]
        add("B", f"Turn {number_words(turn)}, {words}.", clips)
    for k in range(1, MAX_TENTHS + 1):
        add("C", f"Down {amount_words(k)}.", [(f"down_{amount_key(k)}", "whole")])
    for k in range(1, MAX_TENTHS + 1):
        add("D", f"New best, up {amount_words(k)}.", ([("new_best_lead", "lead")] if k == 1 else [])
            + [(f"up_{amount_key(k)}", "tail")])
    add("E", "Good, eighty-four.", [("good_lead", "lead")])
    for n in range(MIN_NUM, MAX_NUM + 1):    # numbers on their own: "Good, seven" runs together with no pause to cut
        w = number_words(n)
        add("E", f"{w[:1].upper()}{w[1:]}.", [(f"num_{n}", "whole")])
    for w in WHOLES:
        add("F", f"{w}.", [(_slug(w), "whole")])
    # 2.17.2 (added after everything above, so earlier line numbers never change)
    for k in range(1, MAX_TENTHS + 1):         # "Turn three, two tenths down." -> tail "two tenths down"
        turn = (k + 2) % MAX_TURN + 1
        add("G", f"Turn {number_words(turn)}, {amount_words(k)} down.", [(f"lost_{amount_key(k)}", "tail")])
    for k, w in enumerate(("one", "two", "three"), 1):
        add("G", f"Sector {w}, up {amount_words(k + 1)}.", [(f"sector_{k}", "lead")])
    add("H", "Reference set, one thirty-eight nine.", [("reference_set", "lead")])
    for i, n in enumerate(range(LAP_TENTHS[0] // 10, LAP_TENTHS[1] // 10 + 1)):   # whole seconds 40 .. 180
        head, _ = _lap_parts(n * 10)
        digit = (n * 7) % 10                    # each tenth word comes from one of the first lines
        _h, tw = _lap_parts(n * 10 + digit)
        clips = [(f"lap_{n}", "lead")] + ([(f"tenth_{digit}", "tail")] if i < 10 else [])
        add("H", f"{head[:1].upper()}{head[1:]}, {tw}.", clips)
    # 2.17.2 "How's my pace?" (added after everything above, so earlier line numbers never change)
    for n in range(1, MAX_TURN + 1):           # "Down three tenths, most in turn four." -> lead + tail
        k = (n - 1) % MAX_TENTHS + 1
        add("I", f"Down {amount_words(k)}, most in turn {number_words(n)}.",
            ([(f"down_lead_{amount_key(k)}", "lead")] if n <= MAX_TENTHS else []) + [(f"most_turn_{n}", "tail")])
    for w in PACE_WHOLES:
        add("I", f"{w}.", [(_slug(w), "whole")])
    for sec in range(60):                      # short lap times: "Thirty-eight, nine." -> lead "thirty-eight"
        w = _sec_words(sec)
        add("J", f"{w[:1].upper()}{w[1:]}, {'oh' if (sec * 3) % 10 == 0 else _ONES[(sec * 3) % 10]}.",
            [(f"sec_{sec}", "lead")])
    for k in range(MIN_MPH, MIN_KMH):          # 2.17.2 mph: the smallest "more speed" number
        add("K", f"Turn {number_words(k + 5)}, more speed, {number_words(k)} down.", [(f"more_speed_{k}_down", "tail")])
    # 2.20 "Learning the track" (added after everything above, so earlier line numbers never change). The turn word
    # is a lead clip every pack already has; each line gives the tail clip(s) after it.
    for k in range(LEARN_SPEEDS[0], LEARN_SPEEDS[1] + 1):
        add("L", f"Turn {number_words(k % MAX_TURN + 1)}, {number_words(k)} down on your best.",
            [(f"best_down_{k}", "tail")])
    add("L", "Turn three, off track.", [("off_track", "tail")])
    for k in range(LEARN_SPEEDS[0], LEARN_SPEEDS[1] + 1):
        add("L", f"Turn {number_words(k % MAX_TURN + 1)}, off track and {number_words(k)} faster in.",
            [(f"off_track_fast_{k}", "tail")])
    for way in ("later", "earlier"):
        for i, m in enumerate(LEARN_METRES):
            add("L", f"Turn {number_words(i % MAX_TURN + 1)}, braked {number_words(m)} metres {way}.",
                [(f"braked_{way}_{m}", "tail")])
    add("L", "Turn five, on your best.", [("on_your_best", "tail")])
    # 2.29 "Crew chief" (added after everything above, so earlier line numbers never change)
    for w in CREW_WHOLES:
        add("M", f"{w}.", [("cc_" + _slug(w), "whole")])
    for n in range(CREW_BOX_IN[0], CREW_BOX_IN[1] + 1):
        add("M", f"Box in {number_words(n)} laps.", [(f"cc_box_in_{n}", "whole")])
    for n in range(CREW_LITRES[0], CREW_LITRES[1] + 1):
        add("M", crew_litres_text(n), [(f"cc_litres_{n}", "whole")])
    for n in range(CREW_GALLONS[0], CREW_GALLONS[1] + 1):
        add("M", crew_gallons_text(n), [(f"cc_gallons_{n}", "whole")])
    for n in range(CREW_FUEL_LAPS[0], CREW_FUEL_LAPS[1] + 1):
        add("M", crew_laps_fuel_text(n), [(f"cc_laps_fuel_{n}", "whole")])
    for k in range(CREW_RATE[0], CREW_RATE[1] + 1):      # the first two lines also give the two lead-ins
        lead = CREW_LEADS[(k - 1) % 2]
        clips = ([("cc_" + _slug(lead), "lead")] if k <= 2 else []) + [(f"cc_rate_{k}", "tail")]
        add("M", f"{lead}, {crew_rate_text(k)}.", clips)
    for k in range(CREW_OFF[0], CREW_OFF[1] + 1):
        add("M", crew_off_text(k), [(f"cc_off_{k}", "whole")])
    return out


def all_keys() -> dict:
    """{key: (role, line number)} for every clip a complete pack has."""
    out = {}
    for ln in lines():
        for key, role in ln["clips"]:
            out.setdefault(key, (role, ln["n"]))
    return out


# ---------------------------------------------------------------- the coach's words -> clip keys

_TURN = r"(?:T|Turn\s*)(\d{1,3})"
_AMOUNT = re.compile(r"^(?:(a|one|two|three|four|five|six|seven|eight|nine) tenths?|(\d{1,2})\.(\d) seconds?)$", re.I)
_WHOLE_KEYS = {w.lower(): _slug(w) for w in WHOLES + PACE_WHOLES}
_TAIL_KEYS = {t: _slug(t) for t in TAILS}


def _amount(text: str):
    m = _AMOUNT.match(text.strip())
    if not m:
        return None
    if m.group(1):
        w = m.group(1).lower()
        k = 1 if w == "a" else _ONES.index(w)
        if (k == 1) != text.lower().strip().endswith("tenth"):
            return None
    else:
        k = int(m.group(2)) * 10 + int(m.group(3))
    return amount_key(k) if 1 <= k <= MAX_TENTHS else None


def _tail(text: str):
    t = text.strip().lower()
    if t in _TAIL_KEYS:
        return _TAIL_KEYS[t]
    m = re.fullmatch(r"more speed,\s*(\d{1,3}) down", t)
    if m and MIN_MPH <= int(m.group(1)) <= MAX_KMH:
        return f"more_speed_{int(m.group(1))}_down"
    # 2.20 learning the track
    m = re.fullmatch(r"(\d{1,3}) down on your best", t)
    if m and LEARN_SPEEDS[0] <= int(m.group(1)) <= LEARN_SPEEDS[1]:
        return f"best_down_{int(m.group(1))}"
    if t == "off track":
        return "off_track"
    m = re.fullmatch(r"off track and (\d{1,3}) faster in", t)
    if m and LEARN_SPEEDS[0] <= int(m.group(1)) <= LEARN_SPEEDS[1]:
        return f"off_track_fast_{int(m.group(1))}"
    m = re.fullmatch(r"braked (\d{1,3}) metres (later|earlier)", t)
    if m and int(m.group(1)) in LEARN_METRES:
        return f"braked_{m.group(2)}_{int(m.group(1))}"
    if t == "on your best":
        return "on_your_best"
    return None


def _time_tail(text: str):
    """2.17.2: 'two tenths down' -> ['lost_0_2'], 'up a tenth' -> ['up_0_1'], 'good, up a tenth' -> ['good_lead',
    'up_0_1'], else None."""
    t = text.strip().lower()
    m = re.fullmatch(r"(good,\s*)?up\s+(.+)", t)
    if m:
        a = _amount(m.group(2))
        return (["good_lead"] if m.group(1) else []) + [f"up_{a}"] if a else None
    m = re.fullmatch(r"(.+?)\s+down", t)
    if m:
        a = _amount(m.group(1))
        return [f"lost_{a}"] if a else None
    return None


_LAP_KEYS = None


def _lap_keys(words: str):
    """'one thirty-eight nine' -> ['lap_98', 'tenth_9'] for the lap times a pack says (LAP_TENTHS), else None."""
    global _LAP_KEYS
    if _LAP_KEYS is None:
        _LAP_KEYS = {}
        for n in range(LAP_TENTHS[0] // 10, LAP_TENTHS[1] // 10 + 1):
            for d in range(10):
                _LAP_KEYS[lap_time_words(n + d / 10)] = [f"lap_{n}", f"tenth_{d}"]
        for sec in range(60):                  # 2.17.2 short lap times ("thirty-eight nine")
            for d in range(10):
                _LAP_KEYS.setdefault(f"{_sec_words(sec)} {'oh' if d == 0 else _ONES[d]}", [f"sec_{sec}", f"tenth_{d}"])
    return _LAP_KEYS.get(" ".join(words.lower().split()))


_CREW_WORDS = None
_CREW_PHRASES = None


def _crew_tables():
    global _CREW_WORDS, _CREW_PHRASES
    if _CREW_WORDS is None:
        _CREW_WORDS = {number_words(n): n for n in range(0, 200)}
        ph = {w.lower(): "cc_" + _slug(w) for w in CREW_WHOLES}
        for k in range(CREW_RATE[0], CREW_RATE[1] + 1):
            ph["rate:" + crew_rate_text(k)] = f"cc_rate_{k}"
        for k in range(CREW_OFF[0], CREW_OFF[1] + 1):
            ph[crew_off_text(k).lower().rstrip(".")] = f"cc_off_{k}"
        _CREW_PHRASES = ph
    return _CREW_WORDS, _CREW_PHRASES


def _crew_sentence(low: str):
    """One crew chief sentence (lower case, no full stop) -> clip keys, or None."""
    words, ph = _crew_tables()
    if low in ph:
        return [ph[low]]
    m = re.fullmatch(r"box in ([a-z\- ]+) laps", low)
    if m and m.group(1) in words and CREW_BOX_IN[0] <= words[m.group(1)] <= CREW_BOX_IN[1]:
        return [f"cc_box_in_{words[m.group(1)]}"]
    for noun, one, many, lim, tag in (("litre", "litre", "litres", CREW_LITRES, "litres"),
                                      ("gallon", "gallon", "gallons", CREW_GALLONS, "gallons")):
        m = re.fullmatch(r"([a-z\- ]+) (" + one + "|" + many + ")", low)
        if m and m.group(1) in words:
            n = words[m.group(1)]
            if lim[0] <= n <= lim[1] and (m.group(2) == one) == (n == 1):
                return [f"cc_{tag}_{n}"]
    m = re.fullmatch(r"([a-z\- ]+) (lap|laps) of fuel", low)
    if m and m.group(1) in words:
        n = words[m.group(1)]
        if CREW_FUEL_LAPS[0] <= n <= CREW_FUEL_LAPS[1] and (m.group(2) == "lap") == (n == 1):
            return [f"cc_laps_fuel_{n}"]
    m = re.fullmatch(r"(car behind closing|gaining on the car ahead),\s*(.+)", low)
    if m and "rate:" + m.group(2) in ph:
        return ["cc_" + _slug(m.group(1)), ph["rate:" + m.group(2)]]
    return None


def crew_compose(text) -> list | None:
    """2.29: the clip keys for a crew chief call ("Box this lap. Forty-two litres."), one or more sentences, or None."""
    if not isinstance(text, str) or len(text) > 140:
        return None
    parts = [p.strip().rstrip(".!") for p in re.split(r"(?<=[.!])\s+", " ".join(text.split())) if p.strip()]
    if not parts:
        return None
    out = []
    for p in parts:
        k = _crew_sentence(p.lower())
        if k is None:
            return None
        out += k
    return out


def compose(text) -> list | None:
    """The clip keys that say one of the coach's cues, or None when no pack can say it (unknown words, a corner
    past turn 25, a delta over 2 seconds ...). Only reads the text: whether a given pack has every clip is
    VoicePack.compose's job."""
    if not isinstance(text, str):
        return None
    ck = crew_compose(text)
    if ck:
        return ck
    t = " ".join(text.split()).strip().rstrip(".!")
    if not t or len(t) > 80:
        return None
    low = t.lower()
    m = re.fullmatch(r"Brake marker,\s*" + _TURN, t, re.I)
    if m:
        n = int(m.group(1))
        return [f"brake_marker_turn_{n}"] if 1 <= n <= MAX_TURN else None
    m = re.fullmatch(_TURN + r"\s*[:,]\s*(.+)", t, re.I)
    if m:
        n, tail = int(m.group(1)), _tail(m.group(2))
        if not 1 <= n <= MAX_TURN:
            return None
        if tail:
            return [f"turn_{n}", tail]
        tt = _time_tail(m.group(2)) if "," in t[:8] else None     # "T3, a tenth down" / "T3, good, up a tenth"
        return [f"turn_{n}"] + tt if tt else None
    m = re.fullmatch(r"sector (one|two|three),\s*(.+)", low)
    if m:
        tt = _time_tail(m.group(2))
        return [f"sector_{('one', 'two', 'three').index(m.group(1)) + 1}"] + tt if tt and "good" not in tt[0] else None
    m = re.fullmatch(r"reference set,\s*(.+)", low)
    if m:
        k = _lap_keys(m.group(1))
        return ["reference_set"] + k if k else None
    if low in _WHOLE_KEYS:
        return [_WHOLE_KEYS[low]]
    m = re.fullmatch(r"down\s+(.+?),\s*most in\s+" + _TURN, t, re.I)     # 2.17.2 "Down three tenths, most in T4"
    if m:
        a, n = _amount(m.group(1)), int(m.group(2))
        return [f"down_lead_{a}", f"most_turn_{n}"] if a and 1 <= n <= MAX_TURN else None
    m = re.fullmatch(r"down\s+(.+)", low)
    if m:
        a = _amount(m.group(1))
        return [f"down_{a}"] if a else None
    m = re.fullmatch(r"new best,\s*up\s+(.+)", low)
    if m:
        a = _amount(m.group(1))
        return ["new_best_lead", f"up_{a}"] if a else None
    m = re.fullmatch(r"good,\s*(\d{1,3})", low)
    if m:
        n = int(m.group(1))
        return ["good_lead", f"num_{n}"] if MIN_NUM <= n <= MAX_NUM else None
    tail = _tail(t)                     # a tip on its own ("Throttle earlier")
    return [tail] if tail and "," not in t else None


# ---------------------------------------------------------------- WAV files

def read_wav_bytes(data: bytes):
    """(float32 mono samples in -1..1, sample rate, info) from WAV bytes: PCM 8/16/24/32-bit, float 32/64-bit,
    WAVE_FORMAT_EXTENSIBLE; several channels are mixed down. Raises ValueError for anything else."""
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("not a WAV file")
    pos, fmt, raw = 12, None, None
    while pos + 8 <= len(data):
        cid, size = data[pos:pos + 4], struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = data[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            if len(body) < 16:
                raise ValueError("damaged WAV header")
            tag, ch, rate, _br, _al, bits = struct.unpack("<HHIIHH", body[:16])
            if tag == 0xFFFE and len(body) >= 26:
                tag = struct.unpack("<H", body[24:26])[0]
            fmt = (tag, ch, rate, bits)
        elif cid == b"data":
            raw = body
            break
        pos += 8 + size + (size & 1)
    if fmt is None or raw is None:
        raise ValueError("WAV file without sound data")
    tag, ch, rate, bits = fmt
    if ch < 1 or ch > 8 or not 4000 <= rate <= 384000:
        raise ValueError("unusual WAV format")
    width = bits // 8
    n = len(raw) // (width * ch) if width else 0
    raw = raw[:n * width * ch]
    if tag == 1 and bits == 16:
        x = np.frombuffer(raw, "<i2").astype(np.float32) / 32768.0
    elif tag == 1 and bits == 8:
        x = (np.frombuffer(raw, np.uint8).astype(np.float32) - 128.0) / 128.0
    elif tag == 1 and bits == 24:
        b = np.frombuffer(raw, np.uint8).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        v = np.where(v >= 1 << 23, v - (1 << 24), v)
        x = v.astype(np.float32) / float(1 << 23)
    elif tag == 1 and bits == 32:
        x = (np.frombuffer(raw, "<i4").astype(np.float64) / 2147483648.0).astype(np.float32)
    elif tag == 3 and bits in (32, 64):
        x = np.frombuffer(raw, "<f4" if bits == 32 else "<f8").astype(np.float32)
        x = np.nan_to_num(np.clip(x, -1.0, 1.0))
    else:
        raise ValueError(f"WAV format not supported ({bits}-bit, type {tag})")
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return x.astype(np.float32), int(rate), {"channels": ch, "bits": bits, "tag": tag}


def read_wav(path):
    return read_wav_bytes(Path(path).read_bytes())


def to_pcm16(x) -> bytes:
    a = np.clip(np.round(np.asarray(x, dtype=np.float64) * 32767.0), -32768, 32767).astype("<i2")
    return a.tobytes()


def wav_bytes(pcm: bytes, rate: int) -> bytes:
    """A complete 16-bit mono WAV file around PCM bytes."""
    pcm = bytes(pcm)[:len(pcm) - len(pcm) % 2]
    return (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE" + b"fmt "
            + struct.pack("<IHHIIHH", 16, 1, 1, int(rate), int(rate) * 2, 2, 16)
            + b"data" + struct.pack("<I", len(pcm)) + pcm)


def write_wav(path, pcm, rate: int):
    """Write 16-bit mono PCM (bytes, or float samples in -1..1) as a WAV file. Returns the path."""
    if not isinstance(pcm, (bytes, bytearray)):
        pcm = to_pcm16(pcm)
    Path(path).write_bytes(wav_bytes(pcm, rate))
    return path


def _clip_header(data: bytes, rate: int):
    """Strict check of one pack clip: 16-bit mono PCM at the pack's rate. Returns the PCM bytes."""
    x, r, info = read_wav_bytes(data)
    if info["tag"] != 1 or info["bits"] != 16 or info["channels"] != 1:
        raise ValueError("not 16-bit mono PCM")
    if r != rate:
        raise ValueError(f"sample rate {r} instead of {rate}")
    return x


# ---------------------------------------------------------------- sound processing (numpy only)

def _db(v):
    return 20.0 * np.log10(np.maximum(v, 1e-9))


def frame_levels(x, sr, frame_ms=10):
    f = max(1, int(sr * frame_ms / 1000))
    n = len(x) // f
    if n == 0:
        return np.zeros(0)
    return _db(np.sqrt(np.mean(np.asarray(x[:n * f], dtype=np.float64).reshape(n, f) ** 2, axis=1)))


def loudness_db(x, sr) -> float:
    """Gated RMS in dBFS (a LUFS-like approximation for short speech): 20 ms frames above -60 dBFS, then only the
    frames within 15 dB of the ungated mean (so pauses and breaths don't count)."""
    lv = frame_levels(x, sr, 20)
    lv = lv[lv > -60]
    if lv.size == 0:
        return -120.0
    pw = 10 ** (lv / 10)
    mean = 10 * np.log10(pw.mean())
    keep = pw[lv > mean - 15]
    return float(10 * np.log10(keep.mean()))


def remove_dc(x):
    x = np.asarray(x, dtype=np.float64)
    return x - x.mean() if x.size else x


def _fft_filter(x, sr, gain_fn):
    n = len(x)
    if n < 8:
        return np.asarray(x, dtype=np.float64)
    pad = int(0.05 * sr)
    y = np.concatenate([np.zeros(pad), x, np.zeros(pad)])
    X = np.fft.rfft(y)
    f = np.fft.rfftfreq(len(y), 1.0 / sr)
    return np.fft.irfft(X * gain_fn(f), len(y))[pad:pad + n]


def highpass(x, sr, hz=80.0):
    """Remove rumble below hz (a smooth 4th-order-like slope), zero phase."""
    return _fft_filter(x, sr, lambda f: 1.0 / np.sqrt(1.0 + (hz / np.maximum(f, 1e-3)) ** 8))


def resample(x, sr_from, sr_to):
    """Band-limited resampling (FFT); fine for the short, silence-padded clips of a pack."""
    if sr_from == sr_to or len(x) == 0:
        return np.asarray(x, dtype=np.float64)
    n_out = int(round(len(x) * sr_to / sr_from))
    pad = int(0.02 * sr_from)
    y = np.concatenate([np.zeros(pad), np.asarray(x, dtype=np.float64), np.zeros(pad)])
    X = np.fft.rfft(y)
    m = int(round(len(y) * sr_to / sr_from))
    k = m // 2 + 1
    Y = np.zeros(k, dtype=complex)
    c = min(k, len(X))
    Y[:c] = X[:c]
    if sr_to < sr_from:                      # gentle roll-off just below the new Nyquist
        f = np.arange(c) / c
        Y[:c] *= np.clip((1.0 - f) / 0.04, 0.0, 1.0)
    out = np.fft.irfft(Y, m) * (m / len(y))
    p2 = int(round(pad * sr_to / sr_from))
    return out[p2:p2 + n_out]


def declick(x, sr):
    """Replace isolated one- or two-sample spikes (mouth clicks, digital ticks) with the local median."""
    x = np.asarray(x, dtype=np.float64).copy()
    if len(x) < 5:
        return x
    med = np.median(np.stack([np.roll(x, -2), np.roll(x, -1), x, np.roll(x, 1), np.roll(x, 2)]), axis=0)
    dev = np.abs(x - med)
    w = max(1, int(0.005 * sr))
    k = np.ones(w) / w
    local = np.convolve(np.abs(np.diff(x, prepend=x[0])), k, mode="same") + 1e-4
    spikes = dev > 8.0 * local
    spikes[:2] = spikes[-2:] = False
    x[spikes] = med[spikes]
    return x


def noise_gate(x, sr, floor_db=None, reduce_db=18.0):
    """Light gate: 10 ms frames under (noise floor + 8 dB) are turned down by reduce_db, with smooth edges."""
    x = np.asarray(x, dtype=np.float64)
    f = max(1, int(0.01 * sr))
    lv = frame_levels(x, sr, 10)
    if lv.size < 5:
        return x
    floor = np.percentile(lv, 10) if floor_db is None else floor_db
    g = np.where(lv < floor + 8.0, 10 ** (-reduce_db / 20), 1.0)
    g = np.convolve(g, np.ones(3) / 3, mode="same")
    env = np.repeat(g, f)
    env = np.concatenate([env, np.full(len(x) - len(env), env[-1] if len(env) else 1.0)])
    sm = max(1, int(0.005 * sr))
    env = np.convolve(env, np.ones(sm) / sm, mode="same")
    return x * env


def speech_bounds(x, sr, rel_db=-40.0):
    """(first, last) sample of speech: 10 ms frames within rel_db of the loudest frame, or None."""
    lv = frame_levels(x, sr, 10)
    if lv.size == 0:
        return None
    on = np.nonzero(lv > lv.max() + rel_db)[0]
    if on.size == 0:
        return None
    f = max(1, int(0.01 * sr))
    return int(on[0] * f), int(min(len(x), (on[-1] + 1) * f))


def trim(x, sr, pad_ms=PAD_MS, rel_db=-40.0):
    b = speech_bounds(x, sr, rel_db)
    if b is None:
        return np.zeros(0)
    pad = int(sr * pad_ms / 1000)
    return np.asarray(x[max(0, b[0] - pad):min(len(x), b[1] + pad)], dtype=np.float64)


def fades(x, sr, ms=FADE_MS):
    x = np.asarray(x, dtype=np.float64).copy()
    n = min(len(x) // 2, int(sr * ms / 1000))
    if n > 0:
        r = np.sin(np.linspace(0, np.pi / 2, n)) ** 2
        x[:n] *= r
        x[-n:] *= r[::-1]
    return x


def limit(x, sr, peak_db=PEAK_DB):
    """Bring every peak down to peak_db with a short smooth gain dip (5 ms), not hard clipping."""
    x = np.asarray(x, dtype=np.float64)
    thr = 10 ** (peak_db / 20)
    if x.size == 0 or np.abs(x).max() <= thr:
        return x
    g = np.minimum(1.0, thr / np.maximum(np.abs(x), 1e-9))
    w = max(3, int(0.005 * sr) | 1)
    h = w // 2
    gp = np.concatenate([np.ones(h), g, np.ones(h)])
    gmin = np.lib.stride_tricks.sliding_window_view(gp, w).min(axis=1)
    gp = np.concatenate([np.ones(h), gmin, np.ones(h)])
    gs = np.convolve(gp, np.ones(w) / w, mode="valid")
    return np.clip(x * gs, -thr, thr)


def normalize(x, sr, target_db=TARGET_DB, peak_db=PEAK_DB):
    """Gain to target_db gated loudness with peaks under peak_db; repeated so the limiter's dip is made up."""
    x = np.asarray(x, dtype=np.float64)
    for _ in range(4):
        cur = loudness_db(x, sr)
        if cur <= -100:
            return x
        if abs(cur - target_db) < 0.05:
            break
        x = limit(x * 10 ** ((target_db - cur) / 20), sr, peak_db)
    return x


def pause_split(x, sr, rel_db=-40.0, min_gap_ms=50, min_head_ms=100):
    """The comma pause in a spoken sentence: (lead end sample, tail start sample) or None. Takes the first quiet
    stretch of at least min_gap_ms (after min_head_ms of speech) that is at least 0.6 x the longest such stretch,
    so a short gap inside a word ("twenty-three") is not taken for the comma."""
    lv = frame_levels(x, sr, 10)
    b = speech_bounds(x, sr, rel_db)
    if b is None:
        return None
    f = max(1, int(0.01 * sr))
    s0, s1 = b[0] // f, b[1] // f
    quiet = lv < lv.max() + rel_db
    gaps, i = [], s0
    while i < s1:
        if quiet[i]:
            j = i
            while j < s1 and quiet[j]:
                j += 1
            if (i - s0) * 10 >= min_head_ms and j < s1 and (j - i) * 10 >= min_gap_ms:
                gaps.append((i, j))
            i = j
        else:
            i += 1
    if not gaps:
        return None
    longest = max(j - i for i, j in gaps)
    i, j = next(g for g in gaps if g[1] - g[0] >= 0.6 * longest)
    return i * f, j * f


def finish(x, sr, target_db=TARGET_DB):
    """The last steps for every clip: DC off, trim with PAD_MS, loudness, fades."""
    y = trim(remove_dc(x), sr)
    if y.size == 0:
        return y
    return fades(normalize(y, sr, target_db), sr)


def cut_line(x, sr, roles, rel_db=-40.0):
    """Clips from one spoken line: {role: samples} for the roles wanted ("whole", "lead", "tail"). Raises
    ValueError when a lead or tail is wanted but no comma pause is found. rel_db: how far under the loudest
    moment counts as the pause (-40: real silence; -32 also takes a deep dip between words)."""
    out = {}
    if "whole" in roles:
        out["whole"] = np.asarray(x, dtype=np.float64)
    if "lead" in roles or "tail" in roles:
        sp = pause_split(x, sr, rel_db=rel_db)
        if sp is None:
            raise ValueError("no pause at the comma")
        a, b = sp
        out["lead"] = np.asarray(x[:a], dtype=np.float64)
        out["tail"] = np.asarray(x[b:], dtype=np.float64)
        out["_gap_ms"] = (b - a) * 1000.0 / sr
    return out


# ---------------------------------------------------------------- packs

def _safe_name(name: str) -> bool:
    n = str(name).replace("\\", "/")
    return (bool(FILE_RE.match(n)) and "/" not in n and ".." not in n and "\0" not in n
            and re.match(r"^[A-Za-z]:", n) is None)


def check_manifest(m) -> list:
    """Problems with a manifest's structure (no file access), in plain words. [] when it is fine."""
    p = []
    if not isinstance(m, dict):
        return ["the manifest is not a list of settings"]
    if m.get("format", FORMAT) != FORMAT:
        p.append(f"made for a newer Debrief (format {m.get('format')})")
    if not isinstance(m.get("id"), str) or not ID_RE.match(m["id"]):
        p.append("the pack's id is missing or has odd characters")
    for k in ("name", "voice_credit", "license"):
        v = m.get(k, "")
        if not isinstance(v, str) or len(v) > MAX_TEXT:
            p.append(f"'{k}' is not a short text")
    if not isinstance(m.get("name"), str) or not m.get("name", "").strip():
        p.append("the pack has no name")
    if m.get("sample_rate") not in RATES:
        p.append("unsupported sample rate")
    j = m.get("join_ms", JOIN_MS)
    if not isinstance(j, (int, float)) or isinstance(j, bool) or not 0 <= j <= 500:
        p.append("'join_ms' must be 0 to 500")
    clips = m.get("clips")
    if not isinstance(clips, dict) or not clips:
        p.append("the pack lists no clips")
        return p
    if len(clips) > MAX_CLIPS:
        p.append(f"too many clips ({len(clips)})")
    files = set()
    for key, c in list(clips.items())[:MAX_CLIPS]:
        if not isinstance(key, str) or not KEY_RE.match(key):
            p.append(f"bad clip name {str(key)[:40]!r}")
            continue
        if not isinstance(c, dict):
            p.append(f"clip {key}: not a list of settings")
            continue
        f = c.get("file")
        if not isinstance(f, str) or not _safe_name(f):
            p.append(f"clip {key}: file name {str(f)[:60]!r} is not a plain .wav name")
        elif f in files:
            p.append(f"clip {key}: file {f} is used twice")
        else:
            files.add(f)
        if not isinstance(c.get("text", ""), str) or len(c.get("text", "")) > MAX_TEXT:
            p.append(f"clip {key}: text too long")
        if c.get("role", "whole") not in ROLES:
            p.append(f"clip {key}: unknown role")
    prev = m.get("preview")
    if prev is not None and (not isinstance(prev, str) or not _safe_name(prev)):
        p.append("the preview is not a plain .wav name")
    return p


class VoicePack:
    """A loaded pack: clips in memory as float arrays (a pack is a few MB)."""

    def __init__(self, folder, manifest: dict, clips: dict, problems=None):
        self.folder = Path(folder)
        self.manifest = manifest
        self.id = manifest["id"]
        self.name = manifest["name"]
        self.rate = int(manifest["sample_rate"])
        self.join_ms = float(manifest.get("join_ms", JOIN_MS))
        self.clips = clips                      # key -> np.float32 array
        self.problems = list(problems or [])

    def has(self, keys) -> bool:
        return bool(keys) and all(k in self.clips for k in keys)

    def compose(self, text):
        """compose(text) when this pack has every clip for it, else None (then Windows speech says it)."""
        keys = compose(text)
        return keys if keys and self.has(keys) else None

    def _gap(self, a: str, b: str) -> int:
        role = (self.manifest["clips"].get(a) or {}).get("role")
        return int(round(self.rate * (self.join_ms if role == "lead" else 120.0) / 1000))

    def render_samples(self, keys, volume: int = 100):
        """float64 samples saying keys in order, joined with the pack's comma pause and 8 ms crossfades."""
        if not self.has(keys):
            raise KeyError(f"missing clips: {[k for k in (keys or []) if k not in self.clips]}")
        xf = int(self.rate * XFADE_MS / 1000)
        out = np.zeros(0)
        prev = None
        for k in keys:
            c = np.asarray(self.clips[k], dtype=np.float64)
            if prev is None:
                out = c.copy()
            else:
                gap = self._gap(prev, k)
                if gap >= xf:
                    # a pause between them: each clip already fades to silence at its edges (finish), so a second
                    # fade is only put on an edge that isn't silent; a second one would soften the tail's first sound
                    # (its clip starts right where the speech does, cut at the comma)
                    r = np.linspace(1.0, 0.0, xf)
                    if abs(out[-1]) > EDGE_QUIET:
                        out[-xf:] *= r
                    if abs(c[0]) > EDGE_QUIET:
                        c = c.copy()
                        c[:xf] *= r[::-1]
                    out = np.concatenate([out, np.zeros(gap - xf), c])
                else:                           # no pause: overlap the edges (equal-power crossfade)
                    n = min(xf, len(out), len(c))
                    t = np.linspace(0.0, np.pi / 2, n)
                    mix = out[-n:] * np.cos(t) + c[:n] * np.sin(t)
                    out = np.concatenate([out[:-n], mix, c[n:]])
            prev = k
        vol = max(0, min(100, int(volume))) / 100.0
        return out * vol

    def render(self, keys, volume: int = 100) -> bytes:
        """16-bit mono PCM bytes at self.rate."""
        return to_pcm16(self.render_samples(keys, volume))

    def render_text(self, text, volume: int = 100):
        """PCM bytes for one cue, or None when the pack can't say it."""
        keys = self.compose(text)
        return self.render(keys, volume) if keys else None

    def duration_ms(self, keys) -> int | None:
        """Exact length of render(keys) in ms (None when a clip is missing). No audio is made."""
        if not self.has(keys):
            return None
        xf = int(self.rate * XFADE_MS / 1000)
        n = 0
        for i, k in enumerate(keys):
            n += len(self.clips[k])
            if i:
                gap = self._gap(keys[i - 1], k)
                n += gap - xf if gap >= xf else -min(xf, len(self.clips[keys[i - 1]]), len(self.clips[k]))
        return int(round(n * 1000 / self.rate))

    def coverage(self, vocabulary) -> dict:
        return coverage(vocabulary, self)

    def info(self) -> dict:
        m = self.manifest
        return {"id": self.id, "name": self.name, "voice": m.get("voice", ""), "voice_credit": m.get("voice_credit", ""),
                "license": m.get("license", ""), "sample_rate": self.rate, "clips": len(self.clips),
                "folder": str(self.folder), "preview": str(self.folder / m["preview"]) if m.get("preview") else None,
                "problems": self.problems}


def coverage(vocabulary, pack: VoicePack | None = None) -> dict:
    """Which of these phrases a pack can say in full (with pack=None: which the clip scheme covers at all).
    {"covered": [...], "missing": [...], "share": 0..1}."""
    phrases = [p for p in dict.fromkeys(str(x) for x in (vocabulary or [])) if p]
    cov, miss = [], []
    for p in phrases:
        ok = (pack.compose(p) if pack is not None else compose(p)) is not None
        (cov if ok else miss).append(p)
    return {"covered": cov, "missing": miss, "share": (len(cov) / len(phrases)) if phrases else 1.0}


def _inside(folder: Path, f: str) -> Path | None:
    p = (folder / f)
    try:
        if p.is_symlink():
            return None
        r = p.resolve()
        r.relative_to(folder.resolve())
    except (OSError, ValueError):
        return None
    return r


def load_pack(folder, strict: bool = False) -> VoicePack:
    """Read and check a pack folder. Raises VoicePackError when the manifest is unusable. A clip that is missing
    or damaged is left out and noted in pack.problems (strict=True: refused instead); compose() then returns
    None for the phrases that need it, so Windows speech says those."""
    folder = Path(folder)
    mp = folder / MANIFEST
    try:
        if mp.stat().st_size > MAX_MANIFEST_BYTES:
            raise VoicePackError(["the manifest is far too big"])
        m = json.loads(mp.read_text(encoding="utf-8"))
    except VoicePackError:
        raise
    except FileNotFoundError:
        raise VoicePackError(["no manifest.json in the folder"]) from None
    except (OSError, ValueError, UnicodeDecodeError) as e:
        raise VoicePackError([f"the manifest could not be read ({e.__class__.__name__})"]) from None
    probs = check_manifest(m)
    if probs:
        raise VoicePackError(probs)
    rate = int(m["sample_rate"])
    clips, bad, total = {}, [], 0
    for key, c in m["clips"].items():
        p = _inside(folder, c["file"])
        try:
            if p is None:
                raise ValueError("outside the pack folder or a link")
            size = p.stat().st_size
            if size > MAX_CLIP_BYTES:
                raise ValueError("far too big for a clip")
            total += size
            if total > MAX_PACK_BYTES:
                raise ValueError("the pack is far too big")
            x = _clip_header(p.read_bytes(), rate)
            if x.size == 0:
                raise ValueError("empty")
            clips[key] = x
        except (OSError, ValueError) as e:
            bad.append(f"clip {key} ({c['file']}): {e}")
    if strict and bad:
        raise VoicePackError(bad)
    if not clips:
        raise VoicePackError(bad or ["the pack has no usable clips"])
    return VoicePack(folder, m, clips, bad)


def pack_folders(user_dir=None, bundled_dir=None) -> list:
    """[(folder, bundled)] of every folder that has a manifest.json: bundled first, then his own."""
    out = []
    for base, bundled in ((Path(bundled_dir) if bundled_dir else BUNDLED_DIR, True),
                          (Path(user_dir) if user_dir else None, False)):
        if base is None or not base.is_dir():
            continue
        for d in sorted(base.iterdir()):
            if d.is_dir() and not d.name.startswith(".") and (d / MANIFEST).is_file():
                out.append((d, bundled))
    return out


def list_packs(user_dir=None, bundled_dir=None) -> list:
    """Every installed pack: [{"id", "name", "voice", "voice_credit", "license", "bundled", "default", "path",
    "preview", "clips", "ok", "problems"}], the default pack first. A user pack with the same id as a bundled one
    is listed but marked not ok (the bundled one wins). Never raises."""
    out, seen = [], set()
    for d, bundled in pack_folders(user_dir, bundled_dir):
        row = {"id": d.name, "name": d.name, "voice": "", "voice_credit": "", "license": "", "bundled": bundled,
               "default": False, "path": str(d), "preview": None, "clips": 0, "ok": False, "problems": []}
        try:
            m = json.loads((d / MANIFEST).read_text(encoding="utf-8"))
            probs = check_manifest(m)
            if not probs:
                row.update(id=m["id"], name=m["name"], voice=m.get("voice", ""),
                           voice_credit=m.get("voice_credit", ""), license=m.get("license", ""),
                           clips=len(m["clips"]))
                if m.get("preview") and (_inside(d, m["preview"]) or Path()).is_file():
                    row["preview"] = str(d / m["preview"])
            row["problems"] = probs
        except Exception as e:  # noqa: BLE001
            row["problems"] = [f"the manifest could not be read ({e.__class__.__name__})"]
        if not row["problems"] and row["id"] in seen:
            row["problems"] = [f"another pack already uses the id '{row['id']}'"]
        row["ok"] = not row["problems"]
        row["default"] = row["ok"] and bundled and row["id"] == DEFAULT_PACK
        if row["ok"]:
            seen.add(row["id"])
        out.append(row)
    out.sort(key=lambda r: (not r["default"], not r["bundled"], r["name"].lower()))
    return out


def find_pack(pack_id: str | None, user_dir=None, bundled_dir=None) -> VoicePack | None:
    """Load the pack with this id (None or unknown: the default pack). None when no pack loads. Never raises."""
    rows = [r for r in list_packs(user_dir, bundled_dir) if r["ok"]]
    want = [r for r in rows if r["id"] == pack_id] or [r for r in rows if r["default"]] or rows[:1]
    for r in want:
        try:
            return load_pack(r["path"])
        except VoicePackError:
            continue
    return None


# ---------------------------------------------------------------- installing a pack he was given

_BAD_EXT = {".exe", ".dll", ".scr", ".com", ".bat", ".cmd", ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse", ".wsf",
            ".wsh", ".hta", ".msi", ".msp", ".lnk", ".jar", ".reg", ".sys", ".cpl", ".pif", ".docm", ".xlsm",
            ".pptm", ".dotm", ".xlam", ".iso", ".img", ".vhd", ".appx", ".msix", ".py", ".sh"}


def _member_problem(name: str, head: bytes) -> str | None:
    n = name.replace("\\", "/")
    if n.startswith("/") or re.match(r"^[A-Za-z]:", n) or any(p == ".." for p in n.split("/")) or "\0" in n:
        return f"'{name}' would be written outside the folder (path trick)"
    base = n.rsplit("/", 1)[-1]
    suf = [s.lower() for s in Path(base).suffixes]
    if any(s in _BAD_EXT for s in suf):
        return f"'{name}' is a program or script, not a voice clip"
    if base != MANIFEST and (not suf or suf[-1] != ".wav"):
        return f"'{name}' is not a .wav file or manifest.json"
    if base != MANIFEST and not FILE_RE.match(base.lower()):
        return f"'{name}' has an unusual name (use letters, digits and _ only)"
    if head[:2] == b"MZ" or head[:4] == b"\x7fELF" or head[:2] == b"PK" or head[:2] == b"#!":
        return f"'{name}' is a program or an archive, not a voice clip"
    if base != MANIFEST and not (head[:4] == b"RIFF" and head[8:12] == b"WAVE"):
        return f"'{name}' is not really a WAV file"
    return None


def check_import(src) -> dict:
    """Check a pack folder or .zip before installing it. Reads only. {"ok", "problems", "files": [names],
    "size", "kind": "folder"|"zip"}."""
    p = Path(src)
    out = {"ok": False, "problems": [], "files": [], "size": 0, "kind": "folder" if p.is_dir() else "zip"}
    probs = out["problems"]
    entries = []                                   # (name, size, read_head)
    try:
        if p.is_dir():
            for f in sorted(p.rglob("*")):
                rel = f.relative_to(p).as_posix()
                if f.is_symlink():
                    probs.append(f"'{rel}' is a link")
                    continue
                if f.is_dir():
                    probs.append(f"'{rel}' is a folder inside the pack (a pack is one flat folder)")
                    continue
                entries.append((rel, f.stat().st_size, lambda f=f: f.open("rb").read(64)))
        elif p.is_file():
            if p.suffix.lower() != ".zip":
                probs.append("choose a voice pack folder or a .zip file")
                return out
            with zipfile.ZipFile(p) as z:
                infos = z.infolist()
                if len(infos) > MAX_CLIPS + 2:
                    probs.append("the zip holds far more files than a voice pack")
                    return out
                prefix = _zip_prefix([i.filename for i in infos])
                for zi in infos:
                    if zi.filename.endswith("/"):
                        continue
                    if zi.flag_bits & 0x1:
                        probs.append(f"'{zi.filename}' is password-protected")
                        continue
                    if zi.file_size > MAX_CLIP_BYTES * 2 or (zi.compress_size and zi.file_size / zi.compress_size > 200):
                        probs.append(f"'{zi.filename}' is far too big for a voice clip")
                        continue
                    rel = zi.filename[len(prefix):]
                    if "/" in rel.replace("\\", "/"):
                        probs.append(f"'{zi.filename}' is in a folder inside the pack")
                        continue
                    entries.append((rel, zi.file_size, lambda zi=zi, z=p: _zip_head(z, zi)))
        else:
            probs.append("the file or folder was not found")
            return out
    except (OSError, zipfile.BadZipFile) as e:
        probs.append(f"could not read it ({e.__class__.__name__})")
        return out
    total = 0
    for name, size, head in entries:
        out["files"].append(name)
        total += size
        if size > (MAX_MANIFEST_BYTES if name == MANIFEST else MAX_CLIP_BYTES):
            probs.append(f"'{name}' is far too big")
            continue
        try:
            msg = _member_problem(name, head())
        except Exception as e:  # noqa: BLE001
            msg = f"'{name}' could not be read ({e.__class__.__name__})"
        if msg:
            probs.append(msg)
    out["size"] = total
    if total > MAX_PACK_BYTES:
        probs.append("the pack is far too big")
    if MANIFEST not in out["files"]:
        probs.append("no manifest.json: this is not a Debrief voice pack")
    out["ok"] = not probs
    return out


def _zip_prefix(names) -> str:
    """'mypack/' when every member sits in one top folder (a zipped folder), else ''."""
    tops = {n.replace("\\", "/").split("/", 1)[0] for n in names}
    if len(tops) == 1 and all("/" in n.replace("\\", "/") for n in names):
        return tops.pop() + "/"
    return ""


def _zip_head(path, zi) -> bytes:
    with zipfile.ZipFile(path) as z, z.open(zi) as fh:
        return fh.read(64)


def import_pack(src, user_dir, replace: bool = False) -> dict:
    """Check, copy and load a pack folder or .zip into user_dir/<id>. Nothing is written outside user_dir, nothing
    is run, and a pack with any problem is refused. A pack whose id is bundled or already installed is refused
    unless replace=True (then the old folder is kept as <id>.old-<time>). {"ok", "problems", "id", "path"}."""
    res = {"ok": False, "problems": [], "id": None, "path": None}
    chk = check_import(src)
    if not chk["ok"]:
        res["problems"] = chk["problems"]
        return res
    user_dir = Path(user_dir)
    user_dir.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".incoming-", dir=user_dir))
    try:
        p = Path(src)
        if p.is_dir():
            for name in chk["files"]:
                shutil.copyfile(p / name, tmp / name)
        else:
            with zipfile.ZipFile(p) as z:
                prefix = _zip_prefix([i.filename for i in z.infolist()])
                for zi in z.infolist():
                    if zi.filename.endswith("/"):
                        continue
                    name = zi.filename[len(prefix):]
                    if name not in chk["files"]:
                        continue
                    dst = (tmp / name).resolve()
                    dst.relative_to(tmp.resolve())       # never outside the temporary folder
                    with z.open(zi) as fh:
                        data = fh.read(MAX_CLIP_BYTES * 2 + 1)
                    if len(data) > MAX_CLIP_BYTES * 2:
                        raise VoicePackError([f"'{name}' is far too big"])
                    dst.write_bytes(data)
        pack = load_pack(tmp, strict=True)
        pid = pack.id
        if any(r["id"] == pid and r["bundled"] for r in list_packs(None)):
            raise VoicePackError([f"'{pid}' is the name of a voice that comes with Debrief; give the pack another id"])
        dest = user_dir / pid
        if dest.exists():
            if not replace:
                raise VoicePackError([f"a voice pack called '{pid}' is already installed"])
            dest.rename(user_dir / f"{pid}.old-{time.strftime('%Y%m%d-%H%M%S')}")
        tmp.rename(dest)
        res.update(ok=True, id=pid, path=str(dest), problems=pack.problems)
        return res
    except VoicePackError as e:
        res["problems"] = e.problems
        return res
    except (OSError, ValueError, zipfile.BadZipFile) as e:
        res["problems"] = [f"could not install it ({e.__class__.__name__})"]
        return res
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- writing a pack (generator and importer)

def write_pack(folder, meta: dict, clips: dict, rate: int, join_ms: float, preview=None) -> dict:
    """Write clips {key: (float samples, text, role)} and the manifest into folder (created; existing clip
    files of the same names are replaced, other files are left alone). meta: id, name, voice, voice_credit,
    license, ... Returns the manifest."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    m = {"format": FORMAT, "version": 1}
    m.update(meta)
    m.update(sample_rate=int(rate), join_ms=int(round(join_ms)), clips={})
    for key in sorted(clips, key=lambda k: list(all_keys()).index(k) if k in all_keys() else 10 ** 6):
        x, text, role = clips[key]
        f = f"{key}.wav"
        write_wav(folder / f, x, rate)
        m["clips"][key] = {"file": f, "text": text, "ms": int(round(len(x) * 1000 / rate)), "role": role}
    if preview is not None:
        write_wav(folder / "preview.wav", preview, rate)
        m["preview"] = "preview.wav"
    probs = check_manifest(m)
    if probs:
        raise VoicePackError(probs)
    (folder / MANIFEST).write_text(json.dumps(m, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return m


PREVIEW_CUES = ["Brake marker, T4", "T4: lighter brake", "Down three tenths", "Brake marker, T1", "T1, braked early",
                "T7: more speed, 6 down", "New best, up two tenths", "T3: carry more speed", "Good, 84",
                "Hold the brake in"]


def preview(pack: VoicePack, cues=PREVIEW_CUES, gap_s: float = 0.7):
    """Samples of these cues one after another with gap_s between them (the pack's sample tape)."""
    parts = []
    for c in cues:
        keys = pack.compose(c)
        if keys:
            parts += [pack.render_samples(keys), np.zeros(int(gap_s * pack.rate))]
    return np.concatenate(parts) if parts else np.zeros(0)


def preview_samples(pack: VoicePack):
    """(float samples, rate) for the Preview button: the pack's own preview.wav when it has one (a recorded pack
    may), else the same sample tape made from its clips (the bundled packs: identical sound, no extra file)."""
    p = pack.manifest.get("preview")
    if p:
        f = _inside(pack.folder, p)
        try:
            if f is not None and f.is_file() and f.stat().st_size <= MAX_PACK_BYTES:
                x, r, _info = read_wav(f)
                if x.size:
                    return x, r
        except (OSError, ValueError):
            pass
    return preview(pack), pack.rate


def pack_bytes(folder) -> int:
    return sum(f.stat().st_size for f in Path(folder).iterdir() if f.is_file())

