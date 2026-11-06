# Debrief

Free Windows iRacing coaching app by Black Magik Studios (Python local server + web UI, built with PyInstaller).
Owner and only user: Swazi Williams (address him as "sir"). He is not a programmer: explain changes in plain
words and say what he will see differently in the window.

## How to work here
- Plan first for anything beyond a small fix; wait for his "go".
- Publishing needs his explicit "publish" each time. Never force-push. Never send email or post anywhere for him.
- Keep credits low: Sonnet for building, Haiku for testing, one helper at a time where possible.
- Say plainly what was tested and what was not. Tests use stand-ins; real Garage 61 data and Windows prove the rest.
  Write a "Not proven yet" line in the CHANGELOG for anything only a real PC/real data can confirm.

## Commands
- `python3 tests/run_all.py`: all tests (about 6 minutes). Must be 100% green before every commit.
- Windows-safe tests: no SIGALRM, no /tmp literals, utf-8 file opens, normalize CRLF, no sub-3 s network timing.

## Release flow
1. Branch `release-x.y.z` from main. Bump `debrief/__init__.py`, add a plain-words CHANGELOG entry at the top, and
   update README/Help if anything he sees changed, all in the same commit.
2. Push the branch; wait for the "Build Windows installer" run to pass.
3. Only after his "publish": `git push origin <branch>:main` (fast-forward). The main run creates the release and tag.
   Check with `gh api repos/blackmagik232/debrief/releases/latest`.

## Product rules (decided by the owner)
- Brand: "Debrief · by Black Magik Studios" (Magik with a K). Licence: free to use, all rights reserved (LICENSE).
  Credit borrowed ideas/code in CREDITS.md and THIRD_PARTY_LICENSES.txt.
- Speeds in mph by default, following Settings > Units everywhere.
- Team features are generic: teams and members come only from the user's own Garage 61 teams. Never hard-code team names, slugs or people.
- Garage 61 `/laps` REQUIRES a `tracks` parameter (400 "query parameter \"tracks\" not set" otherwise). Team-track
  discovery passes every track id. It returns only your own, teammates' and followed drivers' laps.
- Never drop a teammate or a lap silently: say who and why, by name.
- Garage 61 token is stored encrypted in settings.toml under %LOCALAPPDATA%\Debrief. Never print, log or commit it.
  The log is %LOCALAPPDATA%\Debrief\debrief.log: read it first when something went wrong on his PC.
- Gemini and ChatGPT work is on hold.
- Coaching voice: Kokoro TTS clips, six voices; regenerate with `tools/make_voicepack.py --add-missing`.

## Lessons (Study Hall, Nov 2026)
1. CHANGELOG in the same commit as the version bump.
2. Test new features on real data before release when possible.
3. When a test fixture disagrees with real data, fix the fixture in the same change.
4. Parallel helpers each get their own branch/worktree and exclusive files; run the full suite after merging.
5. Write "Not proven yet" for anything untested on the real PC.
