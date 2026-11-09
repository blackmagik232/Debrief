"""2.18 lane A: credits, branding and the first-run gate. Run: python tests/test_218_credits.py

- debrief/credits.py, CREDITS.md, THIRD_PARTY_LICENSES.txt and Help > Credits list the same parts;
- every third-party package Debrief imports or requires is credited, and the build bundles both files;
- the app says "Debrief · by Black Magik Studios"; Midfield Menace stays in team features only;
- the window's gate: only Garage 61's pages wait for the token (static checks; the browser checks are in
  test_28_shell.py and test_26_c.py);
- a 401 from the connection check is remembered (token_rejected) and a new token clears it.
"""
from __future__ import annotations

import ast
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="debrief-218-credits-"))
os.environ["DEBRIEF_SETTINGS"] = str(TMP / "settings.toml")
os.environ["DEBRIEF_CACHE_DIR"] = str(TMP / "cache")
os.environ["DEBRIEF_UPDATE_STATE"] = str(TMP / "update.json")

from debrief import brand, credits, firstrun  # noqa: E402

UI = ROOT / "debrief" / "desktop" / "ui"
fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


def read(p):
    return Path(p).read_text(encoding="utf-8")


print("== the credits list")
C = credits.CREDITS
names = [c["name"] for c in C]
check(len(names) == len(set(names)), "no part listed twice")
check(all(c["name"] and c["author"] and c["licence"] and c["link"].startswith("https://") and c["use"] for c in C),
      "each has a name, author, licence, link and what it is for")
check(all(c["kind"] in credits.KINDS for c in C), "each has a known kind")
need = {"Python", "requests", "urllib3", "certifi", "charset-normalizer", "idna", "NumPy", "pypdf", "pyirsdk",
        "PyYAML", "pywin32", "IBM Plex Sans", "Barlow Condensed", "JetBrains Mono", "Kokoro-82M", "Crew Chief"}
check(need <= set(names), f"the parts Debrief ships are all there (missing: {sorted(need - set(names))})")
by = {c["name"]: c for c in C}
check(by["Kokoro-82M"]["licence"] == "Apache-2.0" and by["Kokoro-82M"]["kind"] == "voices", "Kokoro: Apache-2.0 voices")
check(by["Crew Chief"]["kind"] == "ideas" and "no code or sounds" in by["Crew Chief"]["use"], "Crew Chief: ideas only")
check(by["pyirsdk"]["licence"] == "MIT", "pyirsdk: MIT")

# every package in requirements.txt is credited (by its import or project name)
req = [re.split(r"[<>=;\s\[]", ln.strip(), maxsplit=1)[0].lower() for ln in read(ROOT / "requirements.txt").splitlines()
       if ln.strip() and not ln.lstrip().startswith("#")]
alias = {"tomli": None}                                   # only on Python < 3.11; the app is built with a newer one
lower = {n.lower() for n in names}
missing = [r for r in req if alias.get(r, r) is not None and r not in lower]
check(not missing, f"every package in requirements.txt is credited (missing: {missing})")
# every third-party module the app imports is credited
STD = set(sys.stdlib_module_names) | {"debrief", "__future__", "irsdk", "win32com", "pythoncom", "pywintypes",
                                       "winreg", "winsound", "_winapi", "msvcrt",
                                       "win32gui", "win32con", "win32api"}      # 2.27: pywin32's, credited as pywin32
mods = set()
for p in (ROOT / "debrief").rglob("*.py"):
    if p.name == "make_icon.py":                         # a build-time helper (Pillow), never in the app
        continue
    for node in ast.walk(ast.parse(read(p))):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            mods.add(node.module.split(".")[0])
third = sorted(m for m in mods - STD if not m.startswith("_"))
mod_to_credit = {"numpy": "NumPy", "requests": "requests", "pypdf": "pypdf", "yaml": "PyYAML", "comtypes": "comtypes",
                 "tomli": None}
unknown = [m for m in third if m not in mod_to_credit]
check(not unknown, f"no third-party import without a credit (new: {unknown})")
check(all(mod_to_credit[m] is None or mod_to_credit[m] in names for m in third if m in mod_to_credit),
      f"imports map to credits: {third}")

print("== CREDITS.md, THIRD_PARTY_LICENSES.txt and the build")
md = read(ROOT / "CREDITS.md")
check("Debrief · by Black Magik Studios" in md and "© 2026 Black Magik Studios" in md, "CREDITS.md: the byline")
check(all(f"| {c['name']} | {c['author']} | {c['licence']} | <{c['link']}> |" in md for c in C),
      "CREDITS.md has every part with its author, licence and link")
lic = read(ROOT / "THIRD_PARTY_LICENSES.txt")
check(lic.count("Apache License") >= 2 and "TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION" in lic,
      "the full Apache-2.0 text is there (requests, Kokoro-82M)")
check("Requests\nCopyright 2019 Kenneth Reitz" in lic, "requests' NOTICE is passed on")
check(lic.count("Permission is hereby granted, free of charge") >= 5, "the MIT texts are there")
for n in ("pyirsdk", "PyYAML", "urllib3", "charset-normalizer", "idna", "pypdf", "certifi", "Python", "Kokoro-82M",
          "Crew Chief", "RFC 8032", "IBM Plex Sans", "NumPy", "pywin32"):
    check(re.search(r"^=+\n" + re.escape(n), lic, re.M) is not None, f"licence section: {n}")
check("Mihail Latyshov" in lic and "SIL OPEN FONT LICENSE Version 1.1" in lic and "IETF Trust" in lic,
      "pyirsdk, the fonts and RFC 8032 keep their copyright lines")
spec = read(ROOT / "packaging" / "debrief.spec")
check('for _f in ("CREDITS.md", "THIRD_PARTY_LICENSES.txt", "LICENSE"):' in spec and 'datas.append((str(ROOT / _f), "."))' in spec,
      "the build bundles CREDITS.md and THIRD_PARTY_LICENSES.txt (and stops if one is missing)")
check("copy_metadata(_dist)" in spec and '"pyirsdk"' in spec, "and each package's own licence metadata")
check("StringStruct('LegalCopyright', 'Copyright (C) 2026 Black Magik Studios')" in spec, "Debrief.exe's copyright")

print("== branding")
html, js = read(UI / "index.html"), read(UI / "app.js")
check(brand.MAKER == "Black Magik Studios" and brand.PUBLISHER == brand.MAKER, "the maker is Black Magik Studios")
check(brand.BYLINE == "Debrief · by Black Magik Studios" and brand.COPYRIGHT == "© 2026 Black Magik Studios", brand.BYLINE)
check(not hasattr(brand, "TEAM_NAME"), "no team is built in")
check("<title>Debrief · by Black Magik Studios</title>" in html and 'id="brandMaker">by Black Magik Studios<' in html,
      "window title and the line under the logo")
check("© 2026 Black Magik Studios" in html and 'id="btnCredits"' in html, "About: the copyright and a Credits button")
readme = read(ROOT / "README.md")
check("Debrief · by Black Magik Studios" in readme and "© 2026 Black Magik Studios" in readme, "README: byline and ©")
check("Magic Studios" not in readme + html + js + md, "Magik with a K")
check("Sir, you" not in html and "Unproven in the car" not in html and "Copy for Claude" not in html + js,
      "no owner's or developer's wording")
m = re.search(r"words:\[(.*?)\]\],\n keys:", js, re.S)
check(m is not None and "RaceCap" not in m.group(1), "the glossary doesn't mention RaceCap")
check("Midfield Menace</b> team" not in html and "If you're on a team, ask a teammate to invite you" in html,
      "the Garage 61 step talks about any team")
check("API token" not in html + js + readme, "one name: personal access token")

print("== the gate (static)")
check("const G61_NAV=new Set(['library','season','skills','goals','sessions','team']);" in js,
      "only Garage 61's pages are gated")
check("const g61Ready=()=>tokenSaved&&!tokenRejected&&!checkFail;" in js, "a failed or rejected check unlocks nothing")
check("checkOk=s.state==='done';checkFail=s.state==='error';" in js, "the check's outcome is kept")

print("== a rejected token is remembered")
from debrief.desktop import server as S, settings_io  # noqa: E402

(TMP / "settings.toml").write_text('token = "g61-test-token-ABCDEFGHIJKLMNOP"\n', encoding="utf-8")
check(settings_io.public_view()["token_rejected"] is False, "a fresh token: not rejected")
S._note_token_check(ok=False, rejected=True)
check(firstrun.token_rejected() and settings_io.public_view()["token_rejected"] is True, "a 401: remembered")
S._note_token_check(ok=False, rejected=False)
check(firstrun.token_rejected(), "a network failure changes nothing")
S._note_token_check(ok=True, rejected=False)
check(not firstrun.token_rejected(), "a good check clears it")
S._note_token_check(ok=False, rejected=True)
settings_io.save({"token": "g61-test-token-QRSTUVWXYZABCDEFGH"})
check(not firstrun.token_rejected() and settings_io.public_view()["token_rejected"] is False,
      "a new token clears it")
check(dict((s["id"], s["done"]) for s in firstrun.walkthrough()["steps"])["token"] is True, "and the step is done")

print("== /api/brand carries the credits")
srv = S.DebriefServer()
import threading  # noqa: E402
import json  # noqa: E402
import urllib.request  # noqa: E402

threading.Thread(target=srv.serve_forever, daemon=True).start()
try:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(urllib.request.Request(srv.url + "api/brand", headers={"X-Debrief": "1"}), timeout=30) as r:
            j = json.loads(r.read())
    except Exception as e:  # noqa: BLE001
        j = {"error": repr(e)}
    check(isinstance(j.get("credits"), list) and [c["name"] for c in j["credits"]] == names, f"credits sent ({str(j)[:120]})")
    check(j.get("maker") == "Black Magik Studios" and j.get("copyright") == brand.COPYRIGHT and j.get("trademarks"),
          "with the maker, copyright and trademarks")
finally:
    srv.shutdown()
    srv.server_close()

print(f"\n{'all passed' if not fails else str(fails) + ' FAILED'}")
sys.exit(1 if fails else 0)
