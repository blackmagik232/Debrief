"""Build the Debrief Command Center page (a developer tool; not part of the app people download).

The Command Center is the project's one hub, published as an artifact on claude.ai. Its live half (status, the game
plan, decisions, releases, chats) lives in the artifact's own database and is updated directly by any chat. Its code
half (components, the code map, which tests to run) is built here from tools/codemap.py's output and embedded in the
page, so after every push:

    python tools/codemap.py              # writes codemap.json and CODEMAP.md (generated, not committed)
    python tools/commandcenter.py        # writes commandcenter/index.html (generated, not committed)

then republish commandcenter/index.html to the Command Center artifact. Nothing here talks to the network.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = Path(__file__).resolve().parent / "commandcenter_template.html"
HISTORY = Path(__file__).resolve().parent / "commandcenter_history.json"   # releases, chats and decisions up to 2.35 (frozen)
OUT = ROOT / "commandcenter" / "index.html"

# The window's sidebar pages and the code behind each (kept by hand: a page reaches its modules through the server's
# routes, which the code map can't follow on its own). Update when a page gains or loses a part.
PAGES = {
    "today": ("Today", "Your one thing right now, your last race and practice.", ["debrief/desktop/home.py", "debrief/today.py"]),
    "prep": ("Prepare", "This week's race: schedule, history, track cards, fuel and stint plans.",
             ["debrief/prepare.py", "debrief/prep.py", "debrief/schedule.py", "debrief/series.py", "debrief/strategy.py",
              "debrief/endurance.py", "debrief/notes.py"]),
    "room": ("Race Room", "The live pit board from iRacing in practice, qualifying and races.",
             ["debrief/roomlive.py", "debrief/roomboard.py", "debrief/raceroom.py", "debrief/crewchief.py", "debrief/replan.py",
              "debrief/endurancelog.py", "debrief/endureview.py", "debrief/livedata.py"]),
    "coach": ("Coach", "Spoken cues while you drive, the overlay and corner notes.",
              ["debrief/livecoach.py", "debrief/desktop/coach2.py", "debrief/coachvoice.py", "debrief/voicepack.py",
               "debrief/overlay.py", "debrief/cornernotes.py", "debrief/practiceplan.py", "debrief/livedata.py"]),
    "sessions": ("All sessions", "Every session, newest first; Review a session lands here.",
                 ["debrief/sync.py", "debrief/recordings.py", "debrief/localrace.py", "debrief/app.py", "debrief/liverec.py"]),
    "practice": ("Practice", "Every drive from your telemetry: best possible lap, Corner coach, One thing.",
                 ["debrief/sessionibt.py", "debrief/cornercoach.py", "debrief/practiceplan.py", "debrief/ibt.py"]),
    "racing": ("Racing", "Your starts and incidents across your races.",
               ["debrief/desktop/racing.py", "debrief/startreport.py", "debrief/incidentmap.py", "debrief/replay_scan.py"]),
    "setuplab": ("Setup Lab", "Setups from your telemetry, handling, pressures, one change at a time.",
                 ["debrief/setuplab.py", "debrief/desktop/carsetup.py", "debrief/balance.py", "debrief/handling.py",
                  "debrief/setupadvice.py", "debrief/pressurelearn.py", "debrief/setupshare.py"]),
    "cars": ("Car vs car", "Which car of a class is quicker at a track.", ["debrief/carcompare.py", "debrief/desktop/carcompare.py"]),
    "review": ("Race review", "The review page: what to work on, corners, charts, Setup section.",
               ["debrief/engine.py", "debrief/insights.py", "debrief/report.py", "debrief/racesetup.py", "debrief/depth.py",
                "debrief/analysis_plus.py", "debrief/recordings.py"]),
    "library": ("Progress", "Am I getting faster? Trends at every track and car.",
                ["debrief/library.py", "debrief/report.py", "debrief/goals.py", "debrief/profile.py"]),
    "skills": ("Skills", "Your driver profile.", ["debrief/skills.py", "debrief/profile.py"]),
    "team": ("Team", "Leaderboards, members and team sync from your Garage 61 teams.",
             ["debrief/team.py", "debrief/members.py", "debrief/teamsync.py", "debrief/leaderboards.py", "debrief/cards.py"]),
    "settings": ("Settings", "Garage 61, telemetry, units, live data, updates.",
                 ["debrief/desktop/settings_io.py", "debrief/liverec.py", "debrief/updater.py", "debrief/firstrun.py"]),
}


def build(codemap: dict) -> str:
    nodes = {}
    for k, n in codemap["nodes"].items():
        nodes[k] = {key: n.get(key) for key in ("id", "area", "lines", "doc", "imports", "imported_by", "tests", "routes")}
        nodes[k]["public"] = (n.get("public") or [])[:40]
    for k, t in (codemap.get("tests") or {}).items():
        nodes.setdefault(k, {"id": k, "area": "tests", "lines": t.get("lines"), "doc": t.get("doc"), "imports": t.get("covers") or [],
                             "imported_by": [], "tests": [k], "routes": [], "public": []})
    win = codemap.get("window") or {}
    known = set(nodes)
    pages = {p: {"label": a, "what": b, "modules": [m for m in mods if m in known]} for p, (a, b, mods) in PAGES.items()}
    data = {"version": codemap.get("version"), "commit": codemap.get("commit"), "built": date.today().isoformat(),
            "areas": codemap.get("areas"), "totals": codemap.get("totals"), "nodes": nodes, "routes": codemap.get("routes"),
            "pages": pages, "navs": win.get("navs") or {},
            "history": json.loads(HISTORY.read_text(encoding="utf-8")) if HISTORY.is_file() else {},
            "window": {"elements": win.get("elements") or {}, "classes": win.get("classes") or {}, "navs": win.get("navs") or {},
                       "functions": {f: {"file": v.get("file"), "tests": v.get("tests") or [], "via": v.get("via") or []}
                                     for f, v in (win.get("functions") or {}).items()}}}
    js = json.dumps(data, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")
    return TEMPLATE.read_text(encoding="utf-8").replace("__CODE_JSON__", js)


def main() -> int:
    cm = ROOT / "codemap.json"
    if not cm.is_file() or "--fresh" in sys.argv:
        subprocess.run([sys.executable, str(ROOT / "tools" / "codemap.py")], check=True, cwd=ROOT, stdout=subprocess.DEVNULL)
    html = build(json.loads(cm.read_text(encoding="utf-8")))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html, encoding="utf-8")
    print(f"Wrote {OUT.relative_to(ROOT)} ({len(html) // 1024} KB). Republish it to the Command Center artifact.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
