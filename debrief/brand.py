"""Single source of truth for Debrief's team identity (Midfield Menace).

Everything that names the team, links to it on Garage 61 or brands a page imports from here:
app.DEFAULT_TEAM, team.py, leaderboards.py, prep.py, the desktop server's /api/brand, the report templates,
the Discord share card and the installer metadata (packaging/ is checked against this file in tests).

Colours are not set here: the pages keep their existing theme tokens (--a / --me), so light and dark
mode stay consistent.
"""

APP_NAME = "Debrief"
PUBLISHER = "Midfield Menace"

TEAM_SLUG = "midfield-menace"
TEAM_NAME = "Midfield Menace"
TEAM_SHORT = "MM"
TEAM_URL = "https://garage61.net"   # the team page path is not confirmed; link the site, the copy names the team

# Membership states reported by team.py / leaderboards.py / prep.py and shown by the UI and templates.
MEMBER, NOT_MEMBER, UNKNOWN = "member", "not_member", "unknown"

JOIN_TITLE = f"Join {TEAM_NAME} on Garage 61"
JOIN_TEXT = (f"Team comparisons need you on the {TEAM_NAME} team on Garage 61. Ask a teammate to invite you, "
             "then press Refresh. Your own reviews, practice and prep work without it.")


def as_dict() -> dict:
    """What the desktop server returns from /api/brand and what the templates get as D.brand."""
    return {"app_name": APP_NAME, "publisher": PUBLISHER, "team_slug": TEAM_SLUG, "team_name": TEAM_NAME,
            "team_short": TEAM_SHORT, "team_url": TEAM_URL, "join_title": JOIN_TITLE, "join_text": JOIN_TEXT}


def membership(teams, slug: str = TEAM_SLUG) -> str:
    """member / not_member from a Garage 61 teams list (dicts with "slug" or "id"); unknown when the list
    couldn't be loaded (None)."""
    if teams is None:
        return UNKNOWN
    for t in teams:
        if isinstance(t, dict) and str(t.get("slug") or t.get("id") or "").lower() == (slug or "").lower():
            return MEMBER
    return NOT_MEMBER


def team_name_for(teams, slug: str = TEAM_SLUG) -> str:
    """The live Garage 61 display name when the teams list has it, else TEAM_NAME for our slug, else a
    tidied slug. Never the bare slug for Midfield Menace."""
    for t in teams or ():
        if isinstance(t, dict) and str(t.get("slug") or t.get("id") or "").lower() == (slug or "").lower():
            if t.get("name"):
                return str(t["name"])
    if (slug or "").lower() == TEAM_SLUG:
        return TEAM_NAME
    return (slug or "").replace("-", " ").title() or TEAM_NAME
