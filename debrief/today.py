"""The Today start page (2.18), logic only: the one most useful next step, and the last race and practice.

    sureness(kind, value)   -> "sure" | "likely" | "hint" | "guess" | None: one scale for every page's badges
    pick(plan=, review=, racing=, practice=, setup=)
                            -> the "Your #1 thing right now" card (pure, for tests)
    last_race(row, review)  -> the Last race card from a saved review's row and its review.json data
    last_practice(session)  -> the Last practice card from the newest telemetry session
    racing_habit(starts)    -> the strongest lap 1 habit in startreport.build()'s result, or None

Nothing here works anything out again: the card only picks one of the answers other pages already gave
(the Practice page's plan, a review's Fix first, the Racing page's lap 1 habits, the setup checklist) so the
pages agree with it. The rule, in the order tried:

    1. The practice plan's one thing (a corner to work on), unless your newest driving is a race review of a
       different car or track with a corner to fix: then that review's Fix first #1.
    2. The newest review's Fix first #1.
    3. A lap 1 habit the Racing page calls "a habit, not chance" (places you usually lose).
    4. The plan's own next step when it isn't a corner yet ("Drive 5 clean laps first").
    5. The next setup step.
Nothing here raises: odd inputs count as missing.
"""
from __future__ import annotations

FOCUS_KINDS = ("focus", "consistency")     # practiceplan kinds that name a corner or the laps to work on

# One scale for "how sure" across the window. Each page's own levels map to it; the statistics behind them are
# unchanged. Sure: passed the strict test (fewer than 1 in 100 shuffles) or a direct reading. Likely: passed
# the usual test (fewer than 1 in 20). A hint: few laps, or could still be chance (1 in 5). A guess: no pattern,
# try it and see.
SURE_WORDS = {"sure": "Sure", "likely": "Likely", "hint": "A hint", "guess": "A guess"}
_SCALE = {
    "corner": {"clear": "sure", "likely": "likely", "thin": "hint", "unclear": "guess"},     # Corner coach, plan
    "setup": {"reading": "sure", "low-medium": "hint", "guess": "guess"},                      # Setup Lab
    "idea": {"worth a try": "likely", "a guess": "guess"},                                     # setup ideas
    "cars": {"unlikely to be chance": "likely", "likely": "hint"},                             # Car vs car
    "racing": {"lose": "likely", "gain": "likely"},                                            # lap 1 habits
}


def sureness(kind, value):
    """The shared level for one page's confidence word, or None when it has none."""
    return _SCALE.get(str(kind or ""), {}).get(str(value or "").strip().lower())


def _s(v) -> str:
    return v.strip() if isinstance(v, str) else ""


def _d(v) -> dict:
    return v if isinstance(v, dict) else {}


def _day(v) -> str:
    """"2026-11-02T19:00:00Z" -> "2026-11-02T19:00:00" (comparable as text); "" when not a date."""
    s = _s(v)
    return s[:19] if len(s) >= 10 and s[4:5] == "-" else ""


def _turn(t: str) -> str:
    """"T4" -> "Turn 4", as the window shows corners (the spoken name stays T4)."""
    import re
    return re.sub(r"\bT(\d+)\b", r"Turn \1", t)


def _same(a: str, b: str) -> bool:
    return bool(a) and bool(b) and a.strip().lower() == b.strip().lower()


# ---------------------------------------------------------------- the cards
def last_race(row, review=None) -> dict | None:
    """{"dir", "track", "car", "date", "started_at", "session", "best", "verdict", "fix"} for the newest saved
    review. row: the reviews list's row (dir, track, car, date, started_at, session, best); review: its
    review.json (only "insights" is read)."""
    r = _d(row)
    if not _s(r.get("dir")):
        return None
    ins = _d(_d(review).get("insights"))
    fix = next((f for f in ins.get("fix") or [] if isinstance(f, dict) and _s(f.get("corner"))), None)
    verdict = ins.get("verdict")
    if isinstance(verdict, list):
        verdict = " ".join(x for x in verdict if isinstance(x, str))
    out = {k: r.get(k) for k in ("dir", "track", "car", "date", "started_at", "session", "best")}
    out["verdict"] = _s(verdict) or None
    out["fix"] = None
    if fix:
        tips = [_s(t) for t in fix.get("tips") or [] if _s(t)]
        out["fix"] = {"corner": _s(fix.get("corner")), "gain": fix.get("gain") if isinstance(fix.get("gain"), (int, float)) else None,
                      "why": _s(fix.get("why")) or None, "tip": tips[0] if tips else None}
    return out


def last_practice(session) -> dict | None:
    """The newest telemetry session for the Last practice card."""
    s = _d(session)
    if not _s(s.get("id")):
        return None
    keep = ("id", "car", "car_path", "track", "track_code", "date", "session_types", "laps", "clean_laps", "best",
            "median")
    return {k: s.get(k) for k in keep}


def racing_habit(starts) -> dict | None:
    """The lap 1 habit the Racing page calls a pattern ("a habit, not chance"), places lost first:
    {"track", "where", "pattern", "races"} or None."""
    found = []
    for g in _d(starts).get("tracks") or []:
        if not isinstance(g, dict):
            continue
        for c in g.get("corners") or []:
            if isinstance(c, dict) and c.get("pattern") in ("lose", "gain"):
                found.append({"track": _s(g.get("track")), "where": _s(c.get("label")), "pattern": c["pattern"],
                              "races": c.get("n"), "corner": True})
        for b in g.get("bands") or []:
            if isinstance(b, dict) and b.get("pattern") in ("lose", "gain"):
                found.append({"track": _s(g.get("track")), "where": _s(b.get("label")), "pattern": b["pattern"],
                              "races": b.get("n"), "corner": False})
    found.sort(key=lambda x: (x["pattern"] != "lose", not x["corner"]))
    return found[0] if found else None


def _from_plan(j: dict, P: dict) -> dict:
    where = " · ".join(x for x in (_s(j.get("track")), _s(j.get("car"))) if x)
    focus = P.get("kind") in FOCUS_KINDS
    lines = []
    if focus:
        if _s(P.get("how")):
            lines.append(["How", _turn(_s(P["how"]))])
        if _s(P.get("check")):
            lines.append(["The check", _turn(_s(P["check"]))])
    elif _s(P.get("check")):
        lines.append(["", _turn(_s(P["check"]))])
    laps = P.get("from_laps")
    return {"source": "plan", "title": _turn(_s(P.get("text"))) or "Your next practice session",
            "lines": lines, "where": where,
            "why": ("From the Corner coach, over your last " + (f"{laps} " if isinstance(laps, int) and laps else "")
                    + "laps here. The Practice page shows the same one thing.") if focus else
                   "The Practice page's next step for this car and track.",
            "sureness": sureness("corner", P.get("confidence")) if focus else None,
            "action": {"label": "Coach this", "go": "coach", "car_path": j.get("car_path"),
                       "track_code": j.get("track_code")} if focus else {"label": "Open Practice", "go": "practice"},
            "more": {"label": "Open Practice", "go": "practice"} if focus else None}


def _from_review(R: dict) -> dict:
    F = _d(R.get("fix"))
    where = " · ".join(x for x in (_s(R.get("track")), _s(R.get("car"))) if x)
    corner = _s(F.get("corner"))
    gain = F.get("gain")
    title = (f"{corner}: {_s(F.get('tip'))}" if _s(F.get("tip")) else
             f"{corner}: about {gain:.2f} s a lap to find" if isinstance(gain, (int, float)) else f"Work on {corner}")
    lines = []
    if _s(F.get("why")):
        lines.append(["Why", _s(F["why"])])
    if _s(R.get("verdict")):
        lines.append(["Your last race", _s(R["verdict"])])
    return {"source": "review", "title": title, "lines": lines, "where": where,
            "why": "Fix first #1 in your newest race review.", "sureness": None,
            "action": {"label": "Open the review", "go": "review", "dir": R.get("dir")},
            "more": {"label": "Open Practice", "go": "practice"}}


def _from_racing(H: dict) -> dict:
    lose = H["pattern"] == "lose"
    at = H["where"]
    if at.startswith("the start to "):
        at = "between the start and " + at[len("the start to "):]
    elif at.startswith("started in the "):
        at = "when you start in the " + at[len("started in the "):]
    elif at == "all races":
        at = ""
    elif at and not at.startswith(("between ", "on ")):
        at = "at " + at
    title = f"Lap 1: you usually {'lose' if lose else 'gain'} places {at}".rstrip() + "."
    races = H.get("races")
    return {"source": "racing", "title": title,
            "lines": [["What to try", ("Plan that corner before the green: your line, where you'll brake, and "
                                       "room for the car beside you." if H.get("corner") else
                                       "Plan your first lap before the green: where you'll brake and where to "
                                       "leave room.") if lose else "Keep doing what you do there."]],
            "where": H.get("track") or "",
            "why": "The Racing page calls this a habit, not chance" + (f" ({races} races)." if isinstance(races, int) else "."),
            "sureness": "likely", "action": {"label": "Open Racing", "go": "racing"}, "more": None}


def _from_setup(S: dict) -> dict | None:
    st = next((x for x in S.get("steps") or [] if isinstance(x, dict) and not x.get("done")), None)
    if not st:
        return None
    return {"source": "setup", "title": _s(st.get("title")), "lines": [["", _s(st.get("text"))]] if _s(st.get("text")) else [],
            "where": "", "why": "The next setup step.", "sureness": None,
            "action": {"label": "Do this step", "go": "setup", "step": st.get("action")}, "more": None}


def pick(plan=None, review=None, racing=None, practice=None, setup=None) -> dict:
    """"Your #1 thing right now": one card {source, title, lines [[label, text]], where, why, sureness, action,
    more}. plan: the /api/plan reply; review: last_race(); racing: racing_habit(); practice: last_practice();
    setup: the first-run walkthrough. source "none" when there's nothing at all."""
    j, R, H, Pr, S = _d(plan), _d(review), _d(racing) or None, _d(practice), _d(setup)
    P = _d(j.get("plan"))
    focus = bool(P) and P.get("kind") in FOCUS_KINDS and bool(_s(P.get("text")))
    fix = bool(R) and bool(_d(R.get("fix")).get("corner"))
    if focus:
        newer_race = fix and _day(R.get("started_at") or R.get("date")) > _day(Pr.get("date")) and not (
            _same(_s(R.get("track")), _s(j.get("track"))) or _same(_s(R.get("car")), _s(j.get("car"))))
        if not newer_race:
            return _from_plan(j, P)
    if fix:
        return _from_review(R)
    if H and H.get("pattern") and H.get("where"):
        return _from_racing(H)
    if P and _s(P.get("text")):
        return _from_plan(j, P)
    card = _from_setup(S)
    if card:
        return card
    if R and _s(R.get("verdict")):
        return {"source": "review", "title": _s(R["verdict"]), "lines": [], "where": " · ".join(
            x for x in (_s(R.get("track")), _s(R.get("car"))) if x), "why": "From your newest race review.",
                "sureness": None, "action": {"label": "Open the review", "go": "review", "dir": R.get("dir")}, "more": None}
    return {"source": "none", "title": "Drive a few laps with telemetry on", "lines": [
        ["", "In iRacing, press Alt+L in the car to save telemetry. After 5 clean laps Debrief picks one thing for "
             "you to work on."]], "where": "", "why": "", "sureness": None,
            "action": {"label": "Open Practice", "go": "practice"}, "more": None}


def setup_done(setup) -> bool:
    """Setup counts as done (no "Getting set up" box) once Debrief has something of yours to show: a Garage 61
    token, or telemetry read with a session of 5 clean laps. Steps still open after that show as one quiet line."""
    S = _d(setup)
    if S.get("all_done"):
        return True
    done = {x.get("id") for x in S.get("steps") or [] if isinstance(x, dict) and x.get("done")}
    return "token" in done or "session" in done
