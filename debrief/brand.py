"""Single source of truth for Debrief's maker (Black Magik Studios) and its team identity (Midfield Menace).

2.18: Debrief is made by Black Magik Studios ("Magik" with a K): the window, About, the README and the installer say
"Debrief · by Black Magik Studios". Midfield Menace is a team brand inside it and only names team and league features
(the Team page, team leaderboards, teammates' laps, the team Discord).

Everything that names the team, links to it on Garage 61 or brands a page imports from here:
app.DEFAULT_TEAM, team.py, leaderboards.py, prep.py, the desktop server's /api/brand, the report templates,
the Discord share card and the installer metadata (packaging/ is checked against this file in tests).

Colours are not set here: the pages keep their existing theme tokens (--a / --me), so light and dark
mode stay consistent.
"""
import re


APP_NAME = "Debrief"
MAKER = "Black Magik Studios"
PUBLISHER = MAKER
COPYRIGHT = f"\u00a9 2026 {MAKER}"
BYLINE = f"{APP_NAME} \u00b7 by {MAKER}"

TEAM_SLUG = "midfield-menace"
TEAM_NAME = "Midfield Menace"
TEAM_SHORT = "MM"
TEAM_URL = "https://garage61.net"   # the team page path is not confirmed; link the site, the copy names the team

# Membership states reported by team.py / leaderboards.py / prep.py and shown by the UI and templates.
MEMBER, NOT_MEMBER, UNKNOWN = "member", "not_member", "unknown"

# 2.21: the team (Midfield Menace, a name and three people) and the Garage 61 group its laps come from (Overdriver
# Academy) are two things. Settings holds both; these are the defaults.
GROUP_SLUG = "overdriver-academy"
GROUP_NAME = "Overdriver Academy"
MEMBERS = ("Josh Spence", "Swazi Williams", "Kenta Young")


def join_title(group_name=None) -> str:
    return f"Join {group_name or GROUP_NAME} on Garage 61"


def join_text(group_name=None) -> str:
    g = group_name or GROUP_NAME
    return (f"Team comparisons need you in the {g} group on Garage 61. Ask someone in it to invite you, "
            "then press Refresh. Your own reviews, practice and prep work without it.")


def parse_members(raw):
    """Settings' member names ("Josh Spence; Swazi Williams; Kenta Young", or a list) as a clean list. Missing (None):
    the defaults. Empty text: no fixed team (every driver in the group is a teammate)."""
    if raw is None:
        return list(MEMBERS)
    parts = raw if isinstance(raw, (list, tuple)) else re.split(r"[;\n,]", str(raw))
    out = []
    for x in parts:
        x = " ".join(str(x).split())[:60]
        if x and x.lower() not in [o.lower() for o in out]:
            out.append(x)
    return out[:20]


JOIN_TITLE = join_title()
JOIN_TEXT = join_text()


def as_dict() -> dict:
    """What the desktop server returns from /api/brand and what the templates get as D.brand."""
    return {"app_name": APP_NAME, "maker": MAKER, "publisher": PUBLISHER, "copyright": COPYRIGHT, "team_slug": TEAM_SLUG, "team_name": TEAM_NAME,
            "team_short": TEAM_SHORT, "team_url": TEAM_URL, "group_slug": GROUP_SLUG, "group_name": GROUP_NAME, "members": list(MEMBERS), "join_title": JOIN_TITLE, "join_text": JOIN_TEXT}


def membership(teams, slug: str = GROUP_SLUG) -> str:
    """member / not_member from a Garage 61 teams list (dicts with "slug" or "id"); unknown when the list
    couldn't be loaded (None)."""
    if teams is None:
        return UNKNOWN
    for t in teams:
        if isinstance(t, dict) and str(t.get("slug") or t.get("id") or "").lower() == (slug or "").lower():
            return MEMBER
    return NOT_MEMBER


def team_name_for(teams, slug: str = GROUP_SLUG) -> str:
    """The live Garage 61 display name when the teams list has it, else TEAM_NAME for our slug, else a
    tidied slug. Never the bare slug for Midfield Menace."""
    for t in teams or ():
        if isinstance(t, dict) and str(t.get("slug") or t.get("id") or "").lower() == (slug or "").lower():
            if t.get("name"):
                return str(t["name"])
    if (slug or "").lower() == GROUP_SLUG:
        return GROUP_NAME
    if (slug or "").lower() == TEAM_SLUG:
        return TEAM_NAME
    return (slug or "").replace("-", " ").title() or TEAM_NAME
