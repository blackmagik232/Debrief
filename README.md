# Debrief

Reviews your iRacing races from Garage 61 and keeps a library of every review, in its own window.

## For teammates: install

You need Windows 10 or 11. Microsoft Edge is used for the Debrief window (it comes with Windows).

1. Open the repo's **Releases** page on GitHub (github.com/blackmagik232/debrief/releases) and download **Debrief-Setup-x.y.z.exe** from the newest release. The repo is private, so sign in to GitHub first; if you get a "page not found", ask Overdriver to add you.
2. Double-click it. Windows will probably show a blue **"Windows protected your PC"** box, because the installer isn't code-signed. Click **More info**, then **Run anyway**.
3. Click through the installer. It installs just for you (no admin password needed), adds Debrief to the Start menu and, if you leave the box ticked, to your desktop. Leave **Launch Debrief** ticked at the end.
4. Debrief opens on a short getting-started guide:
   - Make a free account at **garage61.net** and connect your iRacing account. To compare laps with the team, ask to join **Midfield Menace** on Garage 61.
   - Request a personal API token at **garage61.net/developer**. It's free for personal, non-commercial use. Garage 61 checks each request by hand, so it can take a little while.
   - Paste the token into Debrief and click **Save and check**. Debrief checks the connection straight away.
   - Click **Review last race** once your race has uploaded to Garage 61.

**Updating:** download the newer installer and run it. It installs over the old version and keeps your token, settings and reviews.

**Uninstalling:** Windows Settings > Apps > Debrief > Uninstall. Your reviews (`Documents\Debrief Reviews`) and your settings (`%LOCALAPPDATA%\Debrief`) are left alone. Delete those folders yourself if you want them gone.

In the installed app, your token, settings, saved Garage 61 data and `debrief.log` live in `%LOCALAPPDATA%\Debrief` (paste that into the File Explorer address bar to open it). Settings > About in Debrief shows the exact folder and the version. Everything below about the Debrief folder applies to that folder instead.

---

The rest of this page is for running Debrief from the source code (the developer setup).

## First-time setup (from source)

1. Double-click **Setup.bat** (already done if you've set up before).
2. Double-click **Install shortcut.bat**. It puts a **Debrief** icon on your desktop.
3. Open Debrief from the desktop icon. Go to **Settings**, paste your Garage 61 token (from garage61.net/developer), and click **Save token**. Then click **Check connection**.

If Windows shows a blue "Windows protected your PC" box, click **More info → Run anyway**. It appears because the files came from the internet.

## Using it

- **Review last race**: finds your newest race on Garage 61, builds the review and opens it. Progress shows in the window.
- **Library**: your personal bests, progress over time per track and car, and the corners that keep costing you time.
- **Past reviews** in the sidebar open any earlier race.
- **Add race result** (bar at the top of an open review): type in where you started and finished, incidents, iRating and safety rating from the iRacing results page. Only the finishing position is needed. You can edit or delete it later. No Garage 61 requests.
- **Season**: your iRating, races and results across the season, from the reviews in your library.
- **Goals**: set a target lap time for a track and car (for example 1:23.500). Debrief shows how close your best is and marks it achieved when you beat it. Edit or delete goals any time. No Garage 61 requests.
- **Prep**: before a race, pick a track and car you have raced (or search any track and car) and click **Build prep page**. You get your history there, where teammates are quicker, the corners that keep costing you, and a practice plan. Searching any track and car needs Garage 61's track and car lists; the first time, click **Load track and car lists** (2 requests, then saved for a week).
- **Team**: the Midfield Menace hub. Click **Refresh team data** to get:
  - **Leaderboards**: everyone's best clean lap at your six most recent tracks and cars, with you highlighted.
  - **Corner kings**: who is quickest through each corner, from each driver's best lap with telemetry (up to five drivers per track and car).
  - **Head-to-heads**: you against each teammate. The gap is your best minus theirs, so a minus number means you were quicker.
  The page says exactly how many Garage 61 requests the refresh used.

Only one job runs at a time (a review, a prep page or a team refresh). If you close the window while one is running, Debrief finishes it before it shuts down.

Reviews are saved in `Documents\Debrief Reviews`. You can open them in a browser too: double-click `index.html` there. The team hub is in the `team` folder and prep pages are in the `prep` folder; they open the same way.

From a command prompt you can also run `python -m debrief team` or `python -m debrief prep --track ID --car ID`.

The old .bat files (Review last race, Check connection, Open library, Edit settings) still work if you ever need them.

## Comparison laps

The Garage 61 API only shares laps from **you, your teammates, and drivers you follow**. There's no global search. To get comparisons, follow a few quick drivers in the cars you race on Garage 61, or have buddies join your team. Debrief picks up to three: a main comparison about 0.8s quicker than you, the fastest one, and one close to your pace.

## Staying within Garage 61's rules

- Personal, non-commercial use only.
- Debrief only contacts Garage 61 when you click something. There's no background polling.
- At least one second between requests, and it waits if Garage 61 says to slow down.
- Everything is cached in the `.cache` folder inside Debrief (`%LOCALAPPDATA%\Debrief\.cache` for the installed app), so a lap is never downloaded twice. A new race is about 30 requests; re-opening it costs none.
- Team refresh: at most about 60 requests the first time (six tracks and cars); clicking it again within ten minutes uses none, and later refreshes only download what's new.
- Prep page: a handful of requests (teammates' best laps at that track and car).
- Goals, race results and Season use no requests at all.

## Team and iRacing settings

- **Settings > Team** picks the Garage 61 team the Team hub and Prep pages compare you with (Midfield Menace by default). Your team list is loaded from Garage 61 once (1 request) and then reused.
- **Settings > iRacing (optional)**: iRacing's own data (official results, iRating and safety rating changes) needs API credentials that iRacing issues on request. You don't need them; typing a race result works without them. If iRacing gives you a client ID and secret, paste them there and Debrief will use them once that feature is switched on. Like the token, they are saved only in settings.toml and never shown again.

## Keeping your token safe

- The token lives only in `settings.toml` in this folder (`%LOCALAPPDATA%\Debrief` for the installed app). Debrief never shows it again after you save it.
- Don't share settings.toml. If you give a buddy a copy of the Debrief folder, delete settings.toml from the copy first.
- If it ever leaks, create a new token at garage61.net/developer and paste the new one in Settings.

## Troubleshooting

- Problems are written to `debrief.log` in this folder (`%LOCALAPPDATA%\Debrief` for the installed app).
- **"Garage 61 rejected the token (401)"**: paste the token again in Settings.
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

The installer appears in `dist-installer`. The source setup above (Setup.bat, settings.toml next to the code) keeps working exactly as before; only the installed app uses `%LOCALAPPDATA%\Debrief`.
