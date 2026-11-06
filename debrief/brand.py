"""Single source of truth for Debrief's maker (Black Magik Studios).

2.18: Debrief is made by Black Magik Studios ("Magik" with a K): the window, About, the README and the installer say
"Debrief \u00b7 by Black Magik Studios".

2.23.1: no team, group or person is built into Debrief. Team features use whatever Garage 61 teams the signed-in
user belongs to; names and members come from Garage 61 only.

Colours are not set here: the pages keep their existing theme tokens (--a / --me), so light and dark
mode stay consistent.
"""
import re


APP_NAME = "Debrief"
MAKER = "Black Magik Studios"
PUBLISHER = MAKER
COPYRIGHT = f"\u00a9 2026 {MAKER}"
BYLINE = f"{APP_NAME} \u00b7 by {MAKER}"

# Membership states reported by team.py / leaderboards.py / prep.py and shown by the UI and templates.
MEMBER, NOT_MEMBER, UNKNOWN = "member", "not_member", "unknown"

def join_title(group_name=None) -> str:
    return f"Join {group_name} on Garage 61" if group_name else "Join a team on Garage 61"


def join_text(group_name=None) -> str:
    return ("Team comparisons need you in a team on Garage 61. Ask someone in it to invite you, "
            "then press Refresh. Your own reviews, practice and prep work without it.")


JOIN_TITLE = join_title()
JOIN_TEXT = join_text()


def as_dict() -> dict:
    """What the desktop server returns from /api/brand and what the templates get as D.brand."""
    return {"app_name": APP_NAME, "maker": MAKER, "publisher": PUBLISHER, "copyright": COPYRIGHT, "join_title": JOIN_TITLE, "join_text": JOIN_TEXT}


def membership(teams, slug: str | None = None) -> str:
    """member / not_member from a Garage 61 teams list (dicts with "slug" or "id"); unknown when the list
    couldn't be loaded (None). With no slug: member when you are in any team."""
    if teams is None:
        return UNKNOWN
    if not slug:
        return MEMBER if any(isinstance(t, dict) and (t.get("slug") or t.get("id")) for t in teams) else NOT_MEMBER
    for t in teams:
        if isinstance(t, dict) and str(t.get("slug") or t.get("id") or "").lower() == slug.lower():
            return MEMBER
    return NOT_MEMBER


def team_name_for(teams, slug: str | None = None) -> str:
    """The live Garage 61 display name when the teams list has it, else a tidied slug. With no slug: the names of all
    the teams, joined."""
    items = [t for t in teams or () if isinstance(t, dict)]
    if not slug:
        return ", ".join(str(t.get("name") or t.get("slug") or t.get("id")) for t in items if t.get("name") or t.get("slug") or t.get("id"))
    for t in items:
        if str(t.get("slug") or t.get("id") or "").lower() == slug.lower() and t.get("name"):
            return str(t["name"])
    return slug.replace("-", " ").title()
