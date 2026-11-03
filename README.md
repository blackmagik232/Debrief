**Just want to use Debrief? Get the installer (step 1 below). Everything else here is for developers.**

# Debrief

Reviews your iRacing races from Garage 61 and keeps a library of every review, in its own window.

**New in 2.17:** **Coach 2.0**: the live coach speaks with a natural voice (four to pick from), through the headset you choose, with a **coach log** you can copy for Claude, **Rehearse** with a session, **Repeat that** and **Mute for 1 minute**. **2.16:** **Racing**: one page for your **starts** (places gained or lost on lap 1, corner by corner from a replay scan) and your **incidents** (which corners, what kind, how sure), across all your races. **2.15:** **Car and setup**: Setup Lab shows **how the car handles** in every corner (where it pushes or rotates, with one setup change to try at a time), **tyre pressures over time** (and what a cold pressure comes up to), and **Share what changed** to paste in Discord; compare with, or be coached against, a **teammate's lap** from their telemetry file. **2.14:** the **Practice loop**: Practice picks **one thing** to work on next session at a track, with a number you can check; the coach watches that corner and keeps count; afterwards the session page scores it ("You hit it on 7 of 10 laps") and says whether to keep it or move on. Also: mute the coach from a **wheel button**, and a **setup checklist** on the start page. **2.13:** **Practice** reviews every session straight from your telemetry files, with the best possible lap and the **Corner coach** (one thing to try in each corner, which the live coach then says while you drive). **2.12:** **Setup Lab** reads the telemetry files iRacing saves on your PC and shows what changed between your setups, which one was faster and one change to try next; the **Coach** says a few short cues while you drive, from your own best lap. **2.11:** Prepare is one clean page: pick a car, series, track and week at the top and everything below follows. **2.10:** a season plan and sharper pre-race brief on Prepare, and Read a rival's lap from a replay for Compare. Earlier: **New in 2.7.5:** Scan this race on any race review: with its replay loaded in iRacing, Debrief fast-forwards through it (recording nothing) and adds a race log of every pass, fight, incident and pit stop. Also: load iRacing's results file instead of typing a result; a Resolve marker file for any review; Help (tour, what the words mean, keys). If you also use RaceCap, point Settings > RaceCap link at its library folder: reviews gain a race log (passes, fights, incidents, pit stops, position by lap), race stories, a traffic check and rival laps. Debrief only reads RaceCap's race folders, never its settings or keys, and everything works the same without it.

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

In the installed app, your token, settings, saved Garage 61 data and `debrief.log` live in `%LOCALAPPDATA%\Debrief` (paste that into the File Explorer address bar to open it). Settings > About in Debrief shows the exact folder and the version, and **Copy diagnostics** copies a short report (version, counts and the end of the log, with your token, webhook, e-mail and Windows user name taken out) to paste when something goes wrong. In the installed app your Garage 61 token and Discord webhook are stored encrypted with your Windows account (DPAPI), so a copied settings file is useless on another PC or account; if Debrief ever says your saved token can't be opened, paste it again. Everything below about the Debrief folder applies to that folder instead.

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
    - Personal bests, trends, weak corners, Season and Skills count **race** sessions only, so a low-fuel practice lap never becomes a PB. Tick **Include practice and qualifying** to see those rows too.
    - **Skills**: your driver profile. Each race with at least six clean laps gets scores for braking, turn-in to apex, apex to throttle and corner exits, so you can see which phase has the most time to find lately and how it changes over the season. No Garage 61 requests.
- **Saved reviews** in the sidebar open any earlier review.
- **Add race result** (bar at the top of an open review): type in where you started and finished, incidents, iRating and safety rating from the iRacing results page. Only the finishing position is needed. You can edit or delete it later. No Garage 61 requests.
- **Goals**: set a target lap time for a track and car (for example 1:23.500). Debrief shows how close your best is and marks it achieved when you beat it. Edit or delete goals any time. No Garage 61 requests.
- **Prepare**: pick at the top: one click for the cars you've driven, or search every car by class, then a series that uses it, a track and a race week. Debrief remembers your last choice, and the star beside the series keeps it as a quick button. Below that is one view: race length, start type, rain, cautions and how often it runs; your best and typical lap there, dry and wet apart; the corners that keep costing you; extra laps from your other starred series; a **track card** (five short optional notes for what telemetry can't see, saved per track in `track-notes` in your reviews folder, so a card follows the track into every series); and racecraft and rivals once you have three scanned races there. **Build prep page** there gives your history, teammates' pace and a practice plan. The season plan shows one chip per week left, marking the weeks that need the most prep; click one to go to it. Under **More**: **Download official schedule** and **Load PDF...** (once a season; Debrief reads it on your PC and never shares it), Track Intel import and prep pages for any track and car.
- **Setup Lab** (**Ctrl+8**): your setups, read from the telemetry files iRacing saves (`Documents\iRacing\telemetry`; if yours are elsewhere, set the folder in Settings > Setup Lab and Coach). Press **Read my telemetry**: the first time reads every file (a few hundred take a few minutes), then only new ones. Pick a car and track (newest first) to see:
  - **Your setups**: Setup A, B, C in the order you first drove them, with dates and files. Debrief reads the setup from the telemetry, because iRacing's .sto setup files are encrypted.
  - **What changed** between them, in plain words (a change made the same on both sides is one line).
  - **Which was faster**: only when the clean laps clearly say so (at least 5 counted laps each). Otherwise it says it can't tell yet and roughly how many more laps would. Very different track temperature or fuel is called out, never adjusted for. Driving order counts too: if the faster setup was simply driven later, part of the gain may be you getting quicker, so Debrief says so and asks you to go back to the first setup for a few laps (A, then B, then A again settles it). If your own improvement could explain most of the gap, it says the two can't be compared fairly yet.
  - **Try next**: one change at a time, with why, how to check it and how sure Debrief is (a reading, a guess, or low to medium for tyre temperatures). A fixed setup series just says so. Set **This series is** open or fixed if Debrief can't tell. Under **What the telemetry shows**: your hot tyre pressures now (and, when it differs, what they were in your fastest stints there), front against rear, fuel use and ride height.
  - **Share what changed** (under What changed): a short text of your last change and what the laps said, plain or with bold for Discord, each with **Copy**. No names or file names; Debrief never posts it.
  - **How the car handles**: a grid of corners (Turn 1, Turn 2...) on the way in, in the middle and on the way out, each marked **Pushes** (understeer), **Rotates** (oversteer), **Neutral** or **Not clear yet**, with a one-line summary, how often you caught the rear (countersteer) and a small cross-check note. Pushes or Rotates only shows when a shuffle test across your laps says it isn't luck and it is big enough to feel. The way in is compared with your own typical corner entry (braking always turns a car in more than steady cornering), and the note under the grid says by how much. A catch only counts when you held opposite lock while the car kept rotating, not a quick change of direction. It reads your newest setup's files (pick another setup or all of them). **Setup ideas** below it: one change at a time with your setup's own numbers, why, and **Worth a try** or **A guess**; then drive 6 clean laps and compare again. Only changes your setup can actually make are offered (a bar already at its lowest falls through to the other end of the car, and says so). A fixed setup series gets a driving note instead.
  - **Tyre pressures over time**: your runs by track temperature (5 °C bands): cold pressure, hot pressure, typical lap, and which cold pressure was quickest only when you drove the pressures back and forth (A, B, A, B) and a shuffle test agrees. One pressure per day, or one block each after the other, is listed but not compared. With 4 or more runs, **Hot pressure from cold** predicts the hot pressures for a cold front and rear pressure and a track temperature, with its typical miss, and says when the pressures you type are outside what your runs show.
  - **Notes**: readings that were left out (cold tyres, short stints) and files that couldn't be read.
  - **More > Check a setup file**: a setup from someone else (.sto or .zip) is checked for programs and scripts before you use it. Debrief never opens, runs or installs it, and keeps no copy.
  - Debrief only reads iRacing's folders; it never writes into them. Prepare shows **Setup Lab for this car** when there are files for that car and track.
- **Practice** (**Ctrl+0**): every time you drove, from the same telemetry files as Setup Lab (press **Read my telemetry** there or here), newest first: date, car, track, session, laps and best lap. Files of one car and track with no more than 3 hours between them count as one session. Open one to see:
  - **How it went**: best lap, typical lap (the middle of your clean laps), how many laps were within half a second of your best, and how it compares with your last session there.
  - **Best possible lap**: your best time through each corner added up, from clean laps once there are 5 (else from all valid laps, and it says so), with a bar for each corner where time is. A guide, not a lap you can count on.
  - **Corner coach**: from your recent laps of that car at that track (up to the last 10 files, within 30 days), one card per corner where your best laps do something clearly different: what's different, how to try it, the speeds it changes, why, and the laps it comes from, marked Clear, Likely or Few laps. A tip is only given when it passes a shuffle test (Debrief shuffles which laps count as your best 400 times; a pattern that shuffled laps show as clearly in 1 shuffle in 20 or more is "no clear pattern yet"), race laps and practice or qualifying laps are compared separately, and brake-point tips need 10 clean laps. "Your best laps were about 0.12 s quicker here" is that corner's share of the gap between your quickest laps and the rest, not a promise of lap time. Then corners with no clear pattern yet, corners already close to your best, and your braking habits (ABS, full pedal, trail braking, coasting).
  - **Laps** (counted, or why not), **Fuel and strategy**, **Conditions** and **Setups used** (with a link to Setup Lab).
  - **Compare with a teammate's lap**: pick a teammate's .ibt file for the same car and track. Debrief checks it for anything harmful, takes their best clean lap and shows Corner coach cards against it. The file is read once and never copied; tick **Keep for this car and track** to keep only the worked-out lap (tick or untick it any time; picking another file without Keep never deletes a lap you kept, and it says so). **Forget it** removes it.
  - **Coach these corners** opens the Coach: it then focuses on those corners and says each one's short tip after the corner.
  - **This session's one thing** (top of Practice): one change for your next session at that car and track, picked from the Corner coach: the corner, what to do, how, the check that counts a lap as a hit (for example "peak brake at or below 88%"), how sure it is (Clear pattern, Likely, Few laps, Experiment) and how many laps it comes from. It is never surer than the Corner coach tip behind it. Where your best laps are quicker but there's no clear reason, it says **Try this and see** (an experiment, not a known fix). **Coach this** has the live coach watch it; **Pick another** picks a different corner. After you drive, open that session: **Your one thing** counts the laps where you hit it, says whether those laps were quicker through that corner (only when a shuffle test says the gap is real, with 3 or more laps each way, each compared with laps from the same part of the session; a carry-more-speed plan is timed to 150 m past the corner) and says **Next: keep this** (under 7 laps out of 10) or **move on**. A session is scored once it has a clean lap through that corner. The list below keeps every plan and its hit rate. Plans are kept in the `practice` folder next to your settings.
  - Corners are called **Turn 3** everywhere on this page, with the race review's name (**C3**) in small text beside it.
- **Racing** (after Practice in the sidebar, no shortcut): your starts and incidents across all your races, read from your replay scans, telemetry files and saved reviews (and RaceCap's scans when linked; Debrief only reads them). Pick a track at the top when you have raced at more than one.
  - **Your starts**: a table of your races (date, start and position after lap 1, places gained or lost, where they changed, lap 1 incidents, contact Yes/Probably/Possibly/No, and where the positions came from: official results, RaceCap's results, iRacing's position from your telemetry or the replay, or a guess from the replay), with **Open review** when the race has a saved review. A bar per corner shows places gained and lost on lap 1 over your scanned races; it needs a replay scan (**Scan this race** on a review) and only counts cars the replay saw near you.
  - **Your incidents**: per track, a grid of corners against **Your own**, **Contact** and **Caught up** with counts and points, lap 1 against later and race, qualifying and practice, then every incident with when, the lap, where, the points (iRacing counts 1x off track, 2x loss of control, 4x contact), what kind, how sure, who was close (from a replay scan) and why. Points added while telemetry wasn't recording are counted but not placed.
  - It only calls something a **pattern** when at least 3 races show it and a fair test says chance can't explain it; otherwise it says what happened. **Look again** picks up new files.
- **Coach** (**Ctrl+9**): press **Start coaching**, then drive. With iRacing running, Debrief says a few short cues: a brake marker on the straight before the one or two corners your race reviews say cost you most, a word after them, and your lap against your best ("Down two tenths"). It coaches from your own best clean lap for that car and track (from your telemetry files, or a better lap you drive). It stays quiet under braking, in corners, with a car alongside, on pit road, on out laps and on lap 1 of a race, and in races at all unless you switch that on. **Mute** or **Ctrl+Shift+M** (works from the car while Debrief is open; map it to a wheel button) silences it. Settings: what it says, focus corners (1 to 3), volume, speaking speed, an optional beep at the brake point, and **Test voice**. Corners from the Corner coach show as "Turn 3 (from the Corner coach)", with the tip it says after them. With a plan from Practice it also watches that one corner each clean lap, shows "Hit 3 of 5 so far" and now and then says a short word on how it went ("Good brake point"); it stays quiet when a pass is too close to the line to call, so it never contradicts the score. **Mute from a wheel button** (Settings on the Coach page): press **Learn**, then the button on your wheel within 15 seconds (one of its first 32 buttons); it mutes and unmutes the coach while it is on. **Clear** removes it. **Coach against a teammate's lap** (off by default): pick their file on the Coach page and tick **Coach against** (their name)**'s lap**; the next time you start the coach in that car at that track it measures you against their best lap, and your own new bests don't replace it. Keep Debrief open while you drive. **Voice and sound** (Coach page): the voice is George (British male); press **Preview** to hear him; pick the device it plays through (**Play through**; an unplugged device falls back to Windows default); **Measure audio delay** so each cue starts that much earlier; **Add your own voice pack** from a folder or .zip (checked first: only sound clips and a manifest). Windows speech says only the rare phrase a voice can't. **Repeat that** (**Ctrl+Shift+R**, or a wheel button) and **Mute for 1 minute** (**Ctrl+Shift+N**). A focus corner you got wrong and then drove well twice in a row is left alone until it slips again. **Rehearse with a session** plays one of your Practice sessions through the coach at real speed (not while coaching live). **Coach log**: every cue made, said or dropped and why, one file per session (last 30 kept, in `coach-logs` next to your settings, no personal data); **Copy for Claude** copies a short summary. Debrief never changes iRacing's volume, and nothing goes online.
- **Reviews** open with **What to work on**: a two-line verdict, up to three corners to **fix first** (what to change there and roughly what it's worth), your **bad habits** and **good habits**, then the **race story** (lap times). Every chart and table is still there under **All the numbers**.
- **Updates**: Debrief checks GitHub for a newer version once a day (on by default; switch it off in Settings > Updates). **Update now** downloads the installer, checks its SHA-256 against the one published with the release (Settings > Updates says "checksum"; once a signing key is published it also checks the signature and says so), installs it and reopens Debrief. Before an update re-draws your saved reviews, Debrief copies every review.json to `.backup` in your reviews folder (the two newest sets are kept).
- **Privacy**: Debrief talks only to Garage 61 (your sync and reviews) and GitHub (the daily update check). Fonts ship inside the app.
- **Multi-class races**: a review lists every teammate and followed driver who was in the same session, in any car or class (1 extra request; Garage 61 doesn't share the rest of the field). In a multi-class race, clean laps that lost their time in one short stretch (usually traffic) are left out of your pace and consistency numbers and listed in the review.
- **Team**: the Midfield Menace hub (you need to be on the team on Garage 61; until then the page shows how to join and your own sections still work). Click **Refresh team data** to get:
  - **Leaderboards**: everyone's best clean lap at your six most recent tracks and cars, with you highlighted.
  - **Corner kings**: who is quickest through each corner, from each driver's best lap with telemetry (up to five drivers per track and car).
  - **Head-to-heads**: you against each teammate. The gap is your best minus theirs, so a minus number means you were quicker.
  The page says exactly how many Garage 61 requests the refresh used.

Only one job runs at a time (a review, a prep page or a team refresh). If you close the window while one is running, Debrief finishes it before it shuts down.

Reviews are saved in `Documents\Debrief Reviews`. You can open them in a browser too: double-click `index.html` there. The team hub is in the `team` folder and prep pages are in the `prep` folder; they open the same way.

From a command prompt you can also run `python -m debrief team` or `python -m debrief prep --track ID --car ID`.

Setup checklist: until everything is set up, the start page shows what is done and the next step, each with a button (Settings, Read my telemetry, Practice, the Coach). **Hide** it whenever you like; **Help > Setup checklist** shows it again.

Keyboard: **Ctrl+R** reviews your last race, **Ctrl+1** to **Ctrl+9** open the pages in sidebar order (Setup Lab is **Ctrl+8**, Coach **Ctrl+9**), **Ctrl+0** opens Practice, **Ctrl+,** opens Settings, **Ctrl+Shift+M** mutes the coach, **Ctrl+Shift+R** repeats its last cue, **Ctrl+Shift+N** mutes it for 1 minute.

## Credits

The coach's voices were made with [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) by hexgrad (Apache-2.0 licence), voices bm_george, am_michael, am_fenrir and af_heart. They were generated once with `tools/make_voicepack.py` and ship as sound clips; Kokoro itself is not part of Debrief.

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
- Goals, race results, Season, Practice, Setup Lab and the Coach use no requests at all.

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
