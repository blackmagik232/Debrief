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

## Code map
- Before changing code, read the project doc `claude/debrief-codemap.md` (or run `python tools/codemap.py`, which writes
  `codemap.json` and `CODEMAP.md`; both are generated, so don't commit them). It lists every module, what it imports, what
  imports it, the window routes it answers and the exact test files to run. The visual version is the Command Center's
  Components and Code map tabs. Regenerate both after every push (see below).
- Before a change, `python tools/codemap.py --which <thing>` (a module, function or method, #element, .class, nav:page or
  api/route; repeat --which for several) prints the test files to run for it. Run those first, then the full suite before a release.

## The hub: Debrief Command Center (owner's rule, 12 Nov 2026)
https://claude.ai/artifact/HwmaZGkTr4xoVa7K7wu4K5 is the one hub. It replaced the game plan Whiteboard and the System
Board (both retired, history only). Two halves:
- **Live half** (its database; any chat updates it with db_write, no republish): `status/now` (published, waiting,
  next, not proven, after-push list, links, docs), `plan/<id>` (lane next/later/parked, order, version, title, text,
  done_when, state planned/building/built/published), `decisions/<id>` (text, state open/decided, answer, date),
  `releases/v2-36-0` style (v, date, commit, name, state waiting/published, lede, points, mods), `chats/<id>`,
  `dlog/<id>` (standing decisions). Releases, chats and decisions up to 2.35 are built in from
  `tools/commandcenter_history.json`.
- **Code half** (components, code map, which tests): `python tools/codemap.py`, then `python tools/commandcenter.py`
  writes `commandcenter/index.html` (generated, not committed); republish it to the artifact (publish_files,
  as index.html). `tools/commandcenter.py` keeps the page-to-code map by hand: update PAGES when a page changes.

## After every push (owner's rule, 12 Nov 2026)
Every push to this repo (a release branch, main, or a work-in-progress branch) is followed, in the same session, by
bringing the project's tracking up to date. Do all of these before telling him the work is done:
1. **Handoff doc** (project `claude/debrief-handoff-<date>.md`): where things stand, the branch and commit pushed, the next
   steps, and a "Start here" block a new chat can paste. A new chat starts from the newest one.
2. **Status doc** (project `claude/debrief-status-<date>.md`, new one per session): what's built, pushed, published, and
   Not proven yet.
3. **Code map**: `python tools/codemap.py`, then upload `CODEMAP.md` as project `claude/debrief-codemap.md`.
4. **History index** (project `claude/debrief-history-index.md`): the chat that did the work, any release, any new
   decision (date, chat, files it governs).
5. **Command Center**: rebuild and republish the code half, then update the live half (status, plan cards, the
   release, the chat, any decisions).
6. **Roadmap doc** (https://claude.ai/artifact/LSqCxCqT1spRdHa5SmzJoS): move items between built, published and next;
   add the Discord post for a new release.
7. **The Paddock** (https://claude.ai/artifact/664XggfeYjNdBHBU6M9x9H): its `boards/roadmap` document.
8. **Always-consult doc** (project `claude/debrief-always-consult.md`): its "Where things stand" section.
If one can't be updated (no access, a tool refused), say which in the handoff doc and in the reply.
Between pushes too: when a plan, decision or version changes, update the Command Center's live half right away.

## Commands
- `python3 tests/run_all.py`: all tests (about 6 minutes). Must be 100% green before every commit.
- Windows-safe tests: no SIGALRM, no /tmp literals, utf-8 file opens, normalize CRLF, no sub-3 s network timing.

## Release flow
1. Branch `release-x.y.z` from main. Bump `debrief/__init__.py`, add a plain-words CHANGELOG entry at the top, and
   update README/Help if anything he sees changed, all in the same commit.
   Every update ships with a What's new (owner's rule, 2.30.4): the CHANGELOG entry starts with plain words for the
   people using Debrief (what they will see, how to use it), then `### For the record` for files, tests and
   "Not proven yet". The app shows the first part once after updating; GitHub shows both. `python -m debrief.whatsnew
   check` must pass (the Windows build runs it).
   Friends use Debrief now: ship only finished features, fully explained. Anything unproven stays off main.
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
