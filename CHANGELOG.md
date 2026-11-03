# Changelog

## 2.13.0

- **Practice** (new page, Ctrl+0): every time you drove, straight from the telemetry files iRacing saves on your PC, newest first, without waiting for Garage 61. Open one to see your best and typical lap, how consistent you were, and how it compares with last time at that track.
- **Best possible lap**: your best time through each part of the lap added up, with a bar for where the time is. It now counts only clean laps once there are 5 of them (before, warm-up and slow laps could make it 0.6 to 0.9 s too quick), and says so. It's a guide, not a lap you can count on.
- **Corner coach**: for each corner, what you do differently on your best laps (brake later, press less hard, come off the brake earlier...), how to try it, what it gets you, why it works and the laps it comes from, with how sure it is (Clear, Likely, Few laps). Taken from your recent laps of that car at that track. Corners with no clear pattern and your braking habits are listed too.
- The session page also shows every lap (counted, or why not), fuel per lap and how far a tank goes, the track conditions and the setups you used, with a link to Setup Lab for that car.
- **Coach these corners**: the live coach now focuses on the Corner coach's corners for that car and track, and after each one says its short tip ("T4: lighter brake"). The Coach page shows which corners those are. Without Corner coach tips it works as before, from your race reviews.
- The coach keeps the ABS reading when its reference lap comes from a telemetry file.
- Corners read the same way on the session page: **Turn 3**, with the race review's name **(C3)** in small text, in the best possible lap bars, the Corner coach cards and the text. On the Coach page, its corners show as "Turn 3 (from the Corner coach)". The Corner coach's tips use colons and full stops instead of dashes.
- Not proven yet: the Corner coach tips and the spoken tips haven't been tried in the car; on your real files the best possible lap was still 0.76 s under your best at Zolder on Nov 1 (5 clean laps, each part's best from a different lap) and 0.65 s on Nov 2 (only 3 clean laps, so it counts all valid laps and says it may be too quick).

## 2.12.0

- **Setup Lab** (new page, Ctrl+8): Debrief reads the telemetry files iRacing saves on your PC (only reads them, never changes them) and shows your setups (A, B, C), what changed between them in plain words, which one was faster (only when the clean laps clearly say so; otherwise how many more laps would tell; and if the faster one was simply driven later, it says part of that may be you getting quicker), one change to try next with how sure it is, and notes on readings it left out. Fixed setup series just say so. You can tell it whether a series is open or fixed. The first read goes through every file, then only new ones.
- **Check a setup file** (Setup Lab > More): a setup from someone else is checked for programs and scripts before you use it. Debrief never opens, runs or installs it.
- **Coach** (new page, Ctrl+9): a few short spoken cues while you drive, from your own best lap and the corners your race reviews say cost you time. Quiet in races unless you switch that on, and never on lap 1. Mute with the button or Ctrl+Shift+M. Settings for what it says, volume, speed and an optional brake beep, and Test voice.
- Prepare shows small links to **Setup Lab for this car** (when there are telemetry files for it) and the **Coach**.
- Settings > Setup Lab and Coach: pick iRacing's folder if your telemetry isn't in Documents\iRacing\telemetry.
- Not proven yet: the coach hasn't been used in the car yet; the Windows voice hasn't been heard on your PC yet (Test voice is the first check); and "quiet when a car is alongside" only works live, because iRacing doesn't record the spotter in its telemetry files, so it can't be tried from a saved file.
