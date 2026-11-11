"""What the driver asked the engineer (2.31): recognised or typed words -> one intent. A pure function, no audio, no clock.

    parse("how's the fuel") -> {"intent": "fuel"}
    parse("gap behind?")    -> {"intent": "gap_behind"}
    parse("blah blah")      -> {"intent": "unknown"}

The same list of phrases is the recogniser's restricted grammar (GRAMMAR): Vosk is only allowed to hear these (plus
"[unk]"), which is far more reliable with a headset and engine noise than free dictation.
"""
from __future__ import annotations

import re

INTENTS = ("fuel", "gap_behind", "gap_ahead", "gaps", "position", "pace", "losing_time", "box", "tyres", "weather",
           "time_left", "quiet", "talk", "repeat", "radio_check", "unknown")

# Words that carry no meaning for the match (a driver says "hey engineer, can you tell me ...").
NOISE = frozenset("""uh um er hmm hey okay ok engineer please can could you tell me give what whats what's is are the a an my
mine i it to of for do does how's hows how about and just now there right then so yeah again say""".split())

# intent -> phrases (the first group of words that must ALL appear, in order not required) matched on the cleaned text.
# Longest/most specific first: a phrase that matches wins in the order of this list.
RULES = [
    ("radio_check", ["radio check", "check check", "can you hear me", "are you there", "comms check"]),
    ("quiet", ["quiet", "be quiet", "shut up", "urgent only", "only urgent", "urgent calls only", "less talk", "stop talking",
               "no chatter", "keep it down"]),
    ("talk", ["talk to me", "talk me", "all calls", "all calls back", "keep me posted", "keep talking", "speak to me",
              "start talking", "chatter back"]),
    ("repeat", ["repeat", "say that again", "come again", "say again", "what did you say", "once more", "repeat that"]),
    ("box", ["when do i box", "when box", "box this lap", "box now", "should i box", "pit this lap", "when do i pit",
             "when pit", "when is my stop", "when's my stop", "whens my stop", "pit window", "pit stop", "do i need to stop", "do i pit", "box box", "box",
             "pit"]),
    ("losing_time", ["where am i losing", "losing time", "where am i slow", "where am i slower", "where lose", "where losing",
                     "where can i improve", "what's slow", "where slow", "worst sector", "where am i behind", "time loss"]),
    ("gap_behind", ["gap behind", "who's behind", "whos behind", "who is behind", "car behind", "behind me",
                    "behind gap", "gap to the car behind", "who behind", "how far behind", "what's behind", "anyone behind"]),
    ("gap_ahead", ["gap ahead", "who's ahead", "whos ahead", "who is ahead", "car ahead", "ahead of me", "gap in front",
                   "who's in front", "who in front", "how far ahead", "what's ahead", "anyone ahead", "gap to the car ahead"]),
    ("gaps", ["gaps", "gap", "what are the gaps", "traffic", "who's around", "whos around"]),
    ("position", ["position", "what position", "where am i", "where am i running", "what place", "what am i running",
                  "what's my position", "my position", "p what", "running order", "who's leading", "am i leading"]),
    ("fuel", ["fuel", "fuel to the end", "laps of fuel", "how many laps of fuel", "how much fuel", "fuel left", "gas",
              "petrol", "tank", "fuel status", "fuel check", "fuel to the flag", "fuel to finish"]),
    ("pace", ["pace", "my pace", "last lap", "best lap", "lap time", "how fast", "how am i doing", "what was that lap",
              "how was that lap", "last lap time", "my last lap", "my best lap", "delta", "how quick", "what's my best"]),
    ("tyres", ["tyres", "tires", "tyre", "tire", "tyre temps", "tire temps", "tyre wear", "tire wear", "rubber"]),
    ("weather", ["weather", "track temp", "track temperature", "air temp", "temperature", "temps", "how hot", "is it wet",
                 "rain", "grip", "conditions", "wet track", "track condition"]),
    ("time_left", ["how long left", "time left", "how long", "time remaining", "how many laps left", "laps left",
                   "laps to go", "how long to go", "how much longer", "time to go", "how long until the end", "remaining"]),
]


FUEL_WORDS = frozenset(("fuel", "gas", "petrol", "tank"))


def clean(text) -> str:
    t = str(text or "").lower().replace("’", "'")
    t = re.sub(r"[^a-z0-9' ]+", " ", t)
    words = [w for w in t.split() if w not in NOISE or w in ("what's",)]
    return " ".join(words)


def _has(words: list, phrase: str) -> bool:
    p = phrase.split()
    # all the phrase's words are in the text and, for a phrase of 2+ words, in the same order
    if len(p) == 1:
        return p[0] in words
    it = iter(words)
    return all(w in it for w in p)


def parse(text) -> dict:
    """Recognised or typed words -> {"intent": name, "text": cleaned}. Unknown words give intent "unknown"."""
    c = clean(text)
    raw = " ".join(re.sub(r"[^a-z0-9' ]+", " ", str(text or "").lower().replace("’", "'")).split())
    if not c and not raw:
        return {"intent": "unknown", "text": ""}
    words = c.split()
    rwords = raw.split()
    best = None
    for intent, phrases in RULES:
        for ph in phrases:
            # a one-word phrase must match a whole word; longer phrases in order, on the text with and without noise words
            if _has(words, ph) or _has(rwords, ph) or ph == c or ph == raw:
                score = len(ph.split())
                if best is None or score > best[0]:
                    best = (score, intent)
                break
    if best is None:
        return {"intent": "unknown", "text": c}
    intent = best[1]
    # "laps left of fuel" is about fuel, not the clock; "where am I on fuel" is about fuel, not the position
    if intent in ("position", "time_left", "pace", "losing_time") and any(w in FUEL_WORDS for w in rwords):
        intent = "fuel"
    # "gap behind" together with "gap ahead" -> both
    if intent in ("gap_behind", "gap_ahead") and _has(words, "behind") and _has(words, "ahead"):
        intent = "gaps"
    return {"intent": intent, "text": c}


def _grammar() -> list:
    out = set()
    for _intent, phrases in RULES:
        out.update(phrases)
    out.update(["what's my pace", "how's the fuel", "gap behind", "gap ahead", "what's the gap behind",
                "what's the gap ahead", "who's behind me", "who's ahead of me", "where am i losing time",
                "when do i box", "box this lap", "how long left", "talk to me", "radio check", "quiet please",
                "how many laps of fuel", "what's my position", "repeat that", "how are the tyres", "track temp"])
    return sorted(out)


GRAMMAR = _grammar()
