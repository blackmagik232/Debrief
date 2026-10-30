**Just want to use Debrief? Get the installer (step 1 below). Everything else here is for developers.**

# Debrief

Reviews your iRacing races from Garage 61 and keeps a library of every review, in its own window.

## For Midfield Menace drivers: install

You need Windows 10 or 11. Microsoft Edge is used for the Debrief window (it comes with Windows).

1. Download **Debrief-Setup-x.y.z.exe** from the newest release at **github.com/blackmagik232/debrief/releases**. No GitHub account needed. Don't use the green Code button (that's the source code, not the app).
2. Double-click it. Windows will probably show a blue **"Windows protected your PC"** box, because the installer isn't code-signed. Click **More info**, then **Run anyway**.
3. Click through the installer. It installs just for you (no admin password needed), adds Debrief to the Start menu and, if you leave the box ticked, to your desktop. Leave **Launch Debrief** ticked at the end.
4. Debrief opens on a short getting-started guide:
   - Make a free account at **garage61.net** and connect your iRacing account. To compare laps with the team, join **Midfield Menace** on Garage 61 (see [Midfield Menace on Garage 61](#midfield-menace-on-garage-61) below).
   - Request a personal API token at **garage61.net/developer**. It's free for personal, non-commercial use. Garage 61 checks each request by hand, so it can take a little while.
   - Paste the token into Debrief and click **Save and check**. Debrief checks the connection straight away, then starts downloading your history in the background. Waiting for Garage 61's email? Close Debrief; the steps are still there when you come back.
   - Click **Review last race** (or press **Ctrl+R**) once your race has uploaded to Garage 61.

**Updating:** Debrief tells you when a new version is out (**Update now** in the sidebar). You can also download the newer installer and run it. It installs over the old version and keeps your token, settings and reviews.

**Uninstalling:** Windows Settings > Apps > Debrief > Uninstall. Your reviews (`Documents\Debrief Reviews`) and your settings (`%LOCALAPPDATA%\Debrief`) are left alone. Delete those folders yourself if you want them gone.

In the installed app, your token, settings, saved Garage 61 data and `debrief.log` live in `%LOCALAPPDATA%\Debrief` (paste that into the File Explorer address bar to open it). Settings > About in Debrief shows the exact folder and the version. Everything below about the Debrief folder applies to that folder instead.

---

The rest of this page is for running Debrief from the source code (the developer setup).

## First-time setup (from source)

The source-only launchers live in the `dev` folder (see `dev/README.md`).

1. Run `dev\Setup.bat` (creates `.venv` next to the code and installs the requirements).
2. Run `dev\Install shortcut.bat` for a desktop icon, or start the window with `.venv\Scripts\python -m debrief.desktop`.
3. Debrief opens on the same getting-started guide as the installed app: paste your Garage 61 token and click **Save and check**.

## Using it

- **Review last race** (**Ctrl+R**): finds your newest race on Garage 61, builds the review and opens it. Progress shows in the window.
- **All sessions**: every race, qualifying and practice session you've driven, newest first, with laps and best lap. Click **Review** on any of them (practice and qualifying reviews get their own folder next to the race's).
  - **History sync**: the first time, Debrief downloads your whole Garage 61 history. It's paced at about one request a second, so a big history takes a while; you can keep using Debrief meanwhile, and if you close it the sync carries on from where it stopped next time. After that, Debrief catches up each time it opens (only new laps, usually a few requests) and **Sync now** does it on demand. Starting a review pauses a running sync.
  - It also saves telemetry for your best lap of every session (once each). Both can be switched off in Settings > History sync.
  - From a command prompt: `python -m debrief sync`.
- **Library**: one page with three tabs:
  - **Library**: your personal bests, progress over time per track and car, and the corners that keep costing you time. When you have reviews, Debrief opens on your last race.
    - **Skills**: your driver profile. Each race with at least six clean laps gets scores for braking, turn-in to apex, apex to throttle and corner exits, so you can see which phase has the most time to find lately and how it changes over the season. No Garage 61 requests.
- **Saved reviews** in the sidebar open any earlier review.
- **Add race result** (bar at the top of an open review): type in where you started and finished, incidents, iRating and safety rating from the iRacing results page. Only the finishing position is needed. You can edit or delete it later. No Garage 61 requests.
- **Goals**: set a target lap time for a track and car (for example 1:23.500). Debrief shows how close your best is and marks it achieved when you beat it. Edit or delete goals any time. No Garage 61 requests.
- **Prepare**: load iRacing's season schedule once a season (**Download official schedule**, or **Load PDF...** for a copy you downloaded; Debrief reads it on your PC and never shares it). Debrief suggests your series from this season's races; tick the ones you race. For each series you see this week's and next week's track, race length and start time, your history there, and a **track card**: five short optional notes (lap 1, crash hotspots, passing spots, track limits, focus this week) for what telemetry can't see. Cards are saved per track in `track-notes` in your reviews folder, so a card follows the track into every series. **Build prep page** still gives your history, teammates' pace, the corners that keep costing you and a practice plan; prep pages for any track and car are under the same page.
- **Reviews** open with **What to work on**: a two-line verdict, up to three corners to **fix first** (what to change there and roughly what it's worth), your **bad habits** and **good habits**, then the **race story** (lap times). Every chart and table is still there under **All the numbers**.
- **Updates**: Debrief checks GitHub for a newer version once a day (on by default; switch it off in Settings > Updates). **Update now** downloads the installer, checks its SHA-256 against the one published with the release, installs it and reopens Debrief.
- **Multi-class races**: a review lists every teammate and followed driver who was in the same session, in any car or class (1 extra request; Garage 61 doesn't share the rest of the field). In a multi-class race, clean laps that lost their time in one short stretch (usually traffic) are left out of your pace and consistency numbers and listed in the review.
- **Team**: the Midfield Menace hub (you need to be on the team on Garage 61; until then the page shows how to join and your own sections still work). Click **Refresh team data** to get:
  - **Leaderboards**: everyone's best clean lap at your six most recent tracks and cars, with you highlighted.
  - **Corner kings**: who is quickest through each corner, from each driver's best lap with telemetry (up to five drivers per track and car).
  - **Head-to-heads**: you against each teammate. The gap is your best minus theirs, so a minus number means you were quicker.
  The page says exactly how many Garage 61 requests the refresh used.

Only one job runs at a time (a review, a prep page or a team refresh). If you close the window while one is running, Debrief finishes it before it shuts down.

Reviews are saved in `Documents\Debrief Reviews`. You can open them in a browser too: double-click `index.html` there. The team hub is in the `team` folder and prep pages are in the `prep` folder; they open the same way.

From a command prompt you can also run `python -m debrief team` or `python -m debrief prep --track ID --car ID`.

Keyboard: **Ctrl+R** reviews your last race, **Ctrl+,** opens Settings.

## Comparison laps

The Garage 61 API only shares laps from **you, your teammates, and drivers you follow**. There's no global search. To get comparisons, follow a few quick drivers in the cars you race on Garage 61, or have buddies join your team. Debrief picks up to three: a main comparison about 0.8s quicker than you, the fastest one, and one close to your pace.

## Staying within Garage 61's rules

- Personal, non-commercial use only.
- Debrief never polls Garage 61 in a loop. By default it does two things on its own: when it opens it catches up your history (only new laps, usually a few requests), and once a day it asks GitHub (not Garage 61) whether there's a newer version. Turn them off in Settings > History sync (**Sync each time Debrief opens**) and Settings > Updates (**Check for updates when Debrief opens**). Everything else happens only when you click something.
- At least one second between requests, and it waits if Garage 61 says to slow down.
- Everything is cached in the `.cache` folder inside Debrief (`%LOCALAPPDATA%\Debrief\.cache` for the installed app), so a lap is never downloaded twice. A new race is about 30 requests; re-opening it costs none.
- Team refresh: at most about 60 requests the first time (six tracks and cars); clicking it again within ten minutes uses none, and later refreshes only download what's new.
- Prep page: a handful of requests (teammates' best laps at that track and car).
- Review: one more request to list teammates and followed drivers who were in the same session.
- Prepare page and track cards: none (the schedule comes from iRacing's PDF; downloading it is one request to iRacing, only when you click).
- Requests are spaced about a second apart; if Garage 61 asks Debrief to slow down, later requests in that run are spaced further apart.
- Goals, race results and Season use no requests at all.

## Midfield Menace on Garage 61

Debrief is built for the Midfield Menace team. The Team hub, the weekly leaderboards and the teammates' laps on Prep pages come from the **Midfield Menace team on Garage 61**, so you need to be a member there:

1. Make your Garage 61 account and connect iRacing (step 4 of the install).
2. Ask a Midfield Menace teammate (or Overdriver) to invite you to the team on Garage 61, and accept the invite.
3. In Debrief, open **Team** and press **Refresh**.

Until you're on the team, the Team page, Prep pages and weekly leaderboards show a "Join Midfield Menace on Garage 61" panel instead of empty boards. Your own reviews, Library, Season, Skills, Goals and Prepare work without it. **Settings > Team and extras > Team** shows whether Debrief sees you as a member.

Developers who want to point Debrief at a different Garage 61 team can use **Advanced: use a different Garage 61 team** under that same card, or `team_slug` in settings.toml.

The optional iRacing API credentials (`iracing_client_id` / `iracing_client_secret` in settings.toml) are still read if present, but no longer shown in Settings: nothing in Debrief needs them today, and typing a race result works without them.

## Keeping your token safe

- The token lives only in `settings.toml` in this folder (`%LOCALAPPDATA%\Debrief` for the installed app). Debrief never shows it again after you save it.
- Don't share settings.toml. If you give a buddy a copy of the Debrief folder, delete settings.toml from the copy first.
- If it ever leaks, create a new token at garage61.net/developer and paste the new one in Settings.

## Troubleshooting

- Problems are written to `debrief.log` in this folder (`%LOCALAPPDATA%\Debrief` for the installed app).
- **"Garage 61 rejected the token (401)"**: paste the token again in Settings > Garage 61 token and click **Save and check**.
- **"Telemetry download: NOT available"**: some telemetry may need a Garage 61 Pro plan. Your own laps normally work.

## Building the installer

GitHub Actions builds it on every push (`.github/workflows/build.yml`): it runs the tests, builds `Debrief.exe` with PyInstaller (`packaging/debrief.spec`), checks it with `Debrief.exe --selftest`, builds `Debrief-Setup-<version>.exe` with Inno Setup (`packaging/debrief.iss`), test-installs it silently and uploads it as a build artifact. Pushing a tag like `v2.0.0` also publishes a GitHub Release with the installer attached. The tag must match `__version__` in `debrief/__init__.py`.

To build on your own Windows PC:

```
pip install -r requirements.txt pyinstaller
pyinstaller packaging/debrief.spec --noconfirm --clean
dist\Debrief\Debrief.exe --selftest
iscc /DAppVersion=2.0.0 packaging\debrief.iss
```

The installer appears in `dist-installer`. The source setup above (`dev\Setup.bat`, settings.toml next to the code) keeps working; only the installed app uses `%LOCALAPPDATA%\Debrief`.
