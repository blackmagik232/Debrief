/* 2.31.0 Help: the guide. The words here are what Help shows in its Learn sections (Start here, Get faster, Know your
   car, Race smarter, Reading the numbers) and the page cards in Every page. app.js draws them (showHelp) together with
   HELP.tour, HELP.words, HELP.keys and HELP.credits, which the rest of app.js keeps adding to.
   Block kinds: lede, h (heading), p, steps (numbered cards, each can open a page), cards (a grid), list, tip, warn,
   table {head, rows}, sure (the sureness chips). **two stars** make bold text. go: a sidebar page name (data-nav);
   hgo: firstrun or whatsnew. Text only: nothing here is ever treated as HTML. */
window.DEBRIEF_HELP = Object.freeze({
sections: [
/* ---------------------------------------------------------------- Start here */
{id: 'start', group: 'Learn', title: 'Start here', blocks: [
  {lede: `Debrief turns the laps you already drive into a short list of what to work on next, then helps you practise it and shows whether it worked. It reads the telemetry files iRacing saves on this PC, and your laps from Garage 61 if you connect it (optional). It never changes anything in iRacing.`},
  {h: 'Get set up (about 10 minutes)'},
  {steps: [
    {t: 'Turn on telemetry in iRacing', d: `Easiest: tick **Settings > Live data > Make iRacing save my telemetry** (or **Turn it on for me** on the setup checklist) and Debrief turns it on each time you get in the car. Or press **Alt+L** in the car yourself. iRacing then saves a telemetry file (.ibt) for every session. Debrief finds the folder by itself; if it can't, Settings lets you pick it.`, go: 'settings', label: 'Open Settings'},
    {t: 'Optional: connect Garage 61', d: `Race reviews, Practice, Setup Lab, the Coach and Racing all work without it, from your telemetry files. Garage 61 adds **comparison laps** from your teammates and the drivers you follow, your history from before Debrief, and the Team page. Make a free Garage 61 account, request a personal token, paste it in Settings and press **Save and check**. Debrief then downloads your history in the background, newest first.`, go: 'settings', label: 'Open Settings'},
    {t: 'Drive 10 clean laps in one session', d: `Any car, any track. Ten laps without an incident or a pit stop is what Practice needs to pick your first thing to work on.`},
    {t: 'Optional: the coach', d: `If you want spoken cues while you drive, open the Coach page, pick your headset and press **Test voice**. Everything else in Debrief works without it.`, go: 'coach', label: 'Open Coach'}]},
  {tip: `The **setup checklist** on Today shows which of these are done and the next step, with a button for each.`, hgo: 'firstrun', label: 'Show the setup checklist'},
  {h: 'Your first week'},
  {cards: [
    {t: 'Session 1: get a baseline', d: `Drive 10 to 15 laps at a track you know. Don't try anything new. Open **Practice** afterwards: your typical lap, best lap and best possible lap are your starting point.`, go: 'practice', label: 'Open Practice'},
    {t: 'Session 2: one thing', d: `At the top of the Practice session is **One thing this session**. Press **Coach this**, then **Start coaching** on the Coach page, and drive. Work on that one corner only.`, go: 'coach', label: 'Open Coach'},
    {t: 'After your next race', d: `Press **Review last race** (Ctrl+Enter). Without Garage 61 the review is built from the telemetry file iRacing saved on this PC, result included. Read **What to work on** at the top. Everything below it is the evidence.`},
    {t: 'End of the week', d: `Open **Progress** and **Skills** to see whether your typical laps are getting closer to your best ones. That gap is the easiest time you have.`, go: 'library', label: 'Open Progress'}]},
  {h: 'Where things are'},
  {p: `The sidebar follows the order you use it in: **Before you drive** (Prepare, Coach, Race Room), **After you drive** (All sessions, Practice, Racing), **Car** (Setup Lab, Car vs car), **Over time** (Progress, Season, Skills, Goals) and **Team**. **Today** at the top shows your one thing right now, your last race and your last practice. Every page is explained under **Every page** in this Help.`},
  {p: `Ctrl+1 to Ctrl+9 and Ctrl+0 open the pages in the sidebar's order, Ctrl+Enter reviews your last race, and **F1** opens the keyboard keys.`}]},

/* ---------------------------------------------------------------- Get faster */
{id: 'improve', group: 'Learn', title: 'Get faster', blocks: [
  {lede: `Lap time doesn't come from everywhere at once. It hides in two or three corners, and in the gap between your typical lap and your best one. Debrief finds those corners, gives you one thing to try, coaches you through it on track, then checks whether it stuck. Do that loop every session and you will get faster.`},
  {h: 'The Debrief loop'},
  {steps: [
    {t: 'Drive with telemetry on', d: `Practise in runs of **10 or more clean laps** on one car and track. More laps make every finding surer; with only a few laps Debrief says "a hint" or "no clear pattern yet" instead of guessing.`},
    {t: 'Read the top, not everything', d: `After practice: **Practice**, open the session. After a race: **Review last race**, then **What to work on**. The top is ranked by time per lap: the first card is worth the most. Read further down only when you want the why.`, go: 'practice', label: 'Open Practice'},
    {t: 'Pick one thing', d: `**One thing this session** gives you a corner, what to do and a number you can check, like "peak brake at or below 88%". Work on that alone. Change three things at once and you can't tell which one helped.`},
    {t: 'Practise it with the coach', d: `Press **Coach this**, then **Start coaching** and drive. The coach says a brake marker on the straight before that corner, tells you after it whether you gained or lost, and keeps count. Pick **Quiet** if you want fewer words.`, go: 'coach', label: 'Open Coach'},
    {t: 'Check that it stuck', d: `Open the session afterwards. "You hit it on 7 of 10 laps" means it is becoming a habit and the next session moves on. **Quicker** or **No clear difference** says whether doing it actually paid off in that corner.`},
    {t: 'Watch the trend', d: `Once a week look at **Progress** (are you getting faster at each track?), **Skills** (braking, entry, middle and exit of corners) and **Goals** (your target lap times).`, go: 'skills', label: 'Open Skills'}]},
  {h: 'Where the time hides'},
  {p: `Debrief splits every corner into the same parts for every lap: **braking** (from the brake point until you let go), **entry** (turning in, down to the slowest point), **middle** and **exit** (back to full throttle). It then compares your typical laps with your own quickest ones in each part. That is why it can say "most of it is on the exit of T4" instead of "be faster in T4".`},
  {table: {head: ['Where you lose it', 'What it usually looks like', 'What to try first'], rows: [
    ['Braking', 'Braking earlier than on your good laps, or not hard enough at the start of the stop', 'Brake at the same marker every lap; press hard at once, then ease off'],
    ['Entry', 'Letting go of the brake too early, then coasting into the turn', 'Keep a little brake on as you turn in (trail braking)'],
    ['Middle', 'A lower slowest speed than your good laps', 'Carry speed in: a slightly later, smoother release of the brake'],
    ['Exit', 'Late or hesitant throttle, or a lift after you got on it', 'Get the car turned first, then one smooth push to full throttle']]}},
  {tip: `Your own best laps are the target, not someone else's. Closing the gap between your typical lap and your best lap is the cheapest time there is: you have already shown you can do it.`},
  {h: 'Compare two laps the right way'},
  {list: [
    `On a review, open **Compare laps** and pick your **best** lap against a **typical** one, or against a teammate's lap.`,
    `Follow the time-gap line. Where it drops sharply is where time went: one sharp drop beats ten small ones.`,
    `At that spot look at the traces: **brake** (where it starts, how hard, how long it stays on), **speed** (the lowest point in the corner) and **throttle** (when it comes back and whether it lifts again).`,
    `The track map shows the same place, so you know which corner and which part of it.`]},
  {h: 'A good 20-minute practice'},
  {steps: [
    {t: 'Laps 1 to 3: warm up', d: `Get the tyres in and settle in. Don't judge anything yet; Debrief leaves out-laps and the first flying lap out of the numbers.`},
    {t: 'Laps 4 to 15: your one thing', d: `Coach on. Work only on today's corner and let the rest of the lap happen. Same marker, same input, every lap.`},
    {t: 'Straight after: look', d: `Open the session in Practice while it is fresh. Did you hit it? Was it quicker? Then pick the next thing or keep this one.`, go: 'practice', label: 'Open Practice'}]},
  {h: 'Before race day'},
  {p: `**Prepare** gets you ready for the next race: pick your car, series, track and week at the top. You get the schedule, your history at that track, your **track card** (your own notes for each corner), **Plan fuel** and, for long races, **Plan stints**. Then practise that track with the Corner coach so your one thing is for the track you are about to race.`, go: 'prep', label: 'Open Prepare'},
  {h: 'Learning a new track'},
  {list: [
    `Start the coach before your first lap. With no reference lap yet it learns as you go: your first clean lap becomes the reference and every quicker clean lap replaces it.`,
    `Hold **Ctrl+Shift+V** while driving and say a short note, like "brake at the 100 board". The coach plays it before that corner on your next laps.`,
    `A teammate's telemetry file makes a good reference too: pick it on the Coach page or a Practice session.`]},
  {h: 'Traps to avoid'},
  {warn: `**Chasing the best possible lap.** It adds up your best moments from different laps. It is a guide to where time is, not a lap time you owe yourself.`},
  {warn: `**Trusting one lap.** One great lap can be luck or a tow. Debrief works from your typical laps for that reason, and so should you.`},
  {warn: `**Changing the setup and your driving on the same day.** If the lap time changes, you won't know why. One at a time.`},
  {warn: `**Ignoring "no clear pattern yet".** It means the laps don't show a reason. Drive more laps the same way, or try the suggested experiment, rather than guessing.`}]},

/* ---------------------------------------------------------------- Know your car */
{id: 'car', group: 'Learn', title: 'Know your car', blocks: [
  {lede: `A car that does what you expect is a car you can push. This section explains what the car is doing under you, how Debrief tells a setup problem from a driving one, and how to change the setup one step at a time.`},
  {h: 'Weight moves, grip follows'},
  {p: `When you brake, weight moves to the front tyres, so the front grips more and the car turns better. When you accelerate, weight moves to the rear: the rear grips more and the front less. Almost every handling problem is about **where the weight is when you ask the car to turn**.`},
  {cards: [
    {t: 'Trail braking', d: `Keeping a little brake on as you turn in, then easing off towards the slowest point. The weight stays on the front tyres, so the car turns. Letting go too early is the most common cause of a car that won't turn in.`},
    {t: 'Coasting', d: `Both pedals off. The car is neither slowing well nor speeding up, and it is unsettled. Debrief counts it in every corner: a lot of coasting usually means braking too early.`},
    {t: 'Smooth inputs', d: `Sudden steering, brake or throttle moves throw the weight around and the tyres let go. Smoother hands and feet usually mean more grip, not less speed.`}]},
  {h: 'Pushes or rotates?'},
  {p: `**Pushes** (understeer): the front slides first, the car runs wide and you add more steering. **Rotates** (oversteer): the rear slides first and you need less steering, or have to catch it. Debrief measures this from your steering against how fast the car is actually turning, compared with how this car normally steers in gentle bends, learned from your own laps.`},
  {p: `What to do depends on **where** in the corner it happens. Try the driving fix first; if it happens on most laps and in most corners of the same kind, it is probably the setup.`},
  {table: {head: ['Where', 'If it pushes', 'If it rotates'], rows: [
    ['Entry (turning in)', 'Driving: keep some brake on as you turn in, a little slower in. Setup ideas: brake bias a little rearward, softer front anti-roll bar.', 'Driving: less brake while turning, no early downshifts. Setup ideas: brake bias a little forward, softer rear anti-roll bar.'],
    ['Middle', 'Driving: less steering, be patient for the front to bite. Setup ideas: softer front or stiffer rear anti-roll bar.', 'Driving: hold a steady throttle to settle the rear. Setup ideas: stiffer front or softer rear anti-roll bar.'],
    ['Exit (on the throttle)', 'Driving: unwind the steering before full throttle. Setup ideas: softer front anti-roll bar, or a little stiffer rear.', 'Driving: squeeze the throttle more gently. Setup ideas: softer rear anti-roll bar, more traction control if the car has it.']]}},
  {tip: `These are common directions, not rules for every car. **Setup Lab's suggestion for your car comes first**: it is worked out from your own laps.`, go: 'setuplab', label: 'Open Setup Lab'},
  {h: 'Changing the setup without fooling yourself'},
  {steps: [
    {t: 'Let Setup Lab read your laps', d: `Open Setup Lab (or any Practice session). It has already looked for pushing, rotating, a car that is unsettled on the way in and poor traction on the way out, and says whether each looks like the **setup** or your **driving**.`, go: 'setuplab', label: 'Open Setup Lab'},
    {t: 'Make one change', d: `Take the top-ranked idea only and make it in the iRacing garage. Each idea says why, what to expect and how sure it is.`},
    {t: 'Drive at least 6 clean laps', d: `Then compare again. Setup Lab waits for **3 separate runs** on a setup before it names one setup faster than another, so a single good run can't fool it.`},
    {t: 'Say how it felt', d: `Press **Agree** or **Not what I felt**. Debrief uses that to adjust how sure it is next time.`}]},
  {h: 'Tyre pressures'},
  {p: `Tyres work best in a narrow pressure window once they are warm. Setup Lab shows your **hot pressures** over time, read from your laps, and what a cold pressure comes up to at a given track temperature. Pressures are in kPa: 100 kPa is about 14.5 psi.`},
  {h: 'Fixed setup series'},
  {p: `In a fixed series the setup is locked, but **brake bias**, **traction control** and **ABS** (on cars that have them) and fuel are still yours. Setup Lab points you to those dials or to your driving instead.`},
  {h: 'Which car, and learning from teammates'},
  {list: [
    `**Car vs car** shows which car of a class is quicker at a track for drivers of similar iRating, and where you were faster in each car. It only names a quicker car when the numbers hold up.`,
    `Pick a **teammate's telemetry file** on a Practice session or the Coach page to compare corners with their best lap, or to be coached against it. Comparing your setups helps too: Setup Lab's **Share what changed** gives you text to paste into Discord.`]}]},

/* ---------------------------------------------------------------- Race smarter */
{id: 'race', group: 'Learn', title: 'Race smarter', blocks: [
  {lede: `Pace wins qualifying; races are won on lap 1, in clean laps and in the pits. Debrief looks at your races as a whole as well as one at a time.`},
  {h: 'After every race'},
  {steps: [
    {t: 'Review last race', d: `Press it (or Ctrl+Enter) after the race. It reads your telemetry file on this PC; with Garage 61 connected it uses Garage 61 once the race is there, and **Add comparison laps** upgrades a review made from your PC. **What to work on** comes first: **Fix first**, ranked by time per lap, then **Keep doing**.`},
    {t: 'Add the result', d: `**Add race result**, then **Load iRacing results file**: your finish, lap 1 position, pit stops and incidents, with little or nothing to type. It also feeds Car vs car.`},
    {t: 'Scan this race', d: `With the race's replay loaded in iRacing, Debrief fast-forwards through it and adds a race log: every pass, fight, incident and pit stop. It records nothing.`},
    {t: 'Read Race craft', d: `A strip with one box per lap: where you lost time and probably why (traffic, an incident, a pit stop), and your lap 1 against your typical lap.`}]},
  {h: 'Starts and incidents'},
  {p: `The **Racing** page looks across all your races: how many places you gain or lose on lap 1, and which corners your incidents happen in and what kind they probably were. It only calls something a pattern when at least 3 races show it and chance can't explain it. A corner where you keep picking up incidents is worth a careful plan for next time.`, go: 'racing', label: 'Open Racing'},
  {tip: `Consistency wins races. A high **consistency score** and a small **pace drift** (laps not getting slower over the race) are often worth more places than a quicker best lap.`},
  {h: 'Fuel and stints'},
  {list: [
    `**Plan fuel** on Prepare works from your own laps at that track: how many stops, how much to start with, the window to stop in and what to add. Check the garage before the race.`,
    `**Plan stints** for long and team races: up to 8 drivers with their own lap times, fuel use and the hours they can drive. It checks iRacing's fair share rule and compares saving fuel to drop a stop.`]},
  {h: 'During the race'},
  {list: [
    `The **crew chief** calls fuel laps left, laps to go, when to box and how much to add, the cheap stop under a caution, and a car closing or alongside. It stays quiet under braking and with a car alongside.`,
    `The **Race Room** (under Coach) is the pit board for team and long races, for the driver and for teammates on their own PCs.`,
    `**Live data** quietly records each iRacing session while you drive (fuel, pit stops, tyres, incidents) and adds it to your reviews.`], go: 'room', label: 'Open the Race Room'}]},

/* ---------------------------------------------------------------- Reading the numbers */
{id: 'numbers', group: 'Reference', title: 'Reading the numbers', blocks: [
  {lede: `Debrief would rather say "not sure yet" than tell you something wrong. Every finding carries the same four words for how sure it is, on every page.`},
  {sure: [
    ['sure', 'Read straight from your laps, or a pattern so clear that chance alone shows it less than 1 time in 100, with 12 or more laps.'],
    ['likely', 'Chance alone would show it less than 1 time in 20. Worth acting on.'],
    ['hint', 'Only a few laps show it, or it could still be chance. Worth a try, then check.'],
    ['guess', 'No clear pattern. An experiment: try it the same way every lap and see.']]},
  {h: 'How Debrief checks itself'},
  {p: `Before it gives a tip, Debrief shuffles which laps count as your best ones hundreds of times. If shuffled laps show a pattern as clear as the real one too often, the pattern is probably chance and you get **no clear pattern yet** instead of a tip.`},
  {h: 'Typical, best and best possible'},
  {cards: [
    {t: 'Typical lap', d: `The middle of your clean laps: half were quicker, half slower. It is your real pace and what most of Debrief measures.`},
    {t: 'Best lap', d: `Your quickest clean lap. Proof of what you can do, but it can include luck or a tow.`},
    {t: 'Best possible lap', d: `Your best time through each part of the lap, added up. A guide to where time is, not a lap you can count on.`}]},
  {h: 'What Debrief will never do'},
  {list: [
    `Promise lap time. "Your best laps were 0.12 s quicker here" describes your laps; it is not a guarantee.`,
    `Count a lap with an incident or a pit stop as clean, or drop a lap or a teammate without saying who and why.`,
    `Change anything in iRacing or post anywhere without you pressing the button.`]},
  {p: `Speeds and fuel follow **Settings > Units** (mph or km/h, litres or gallons).`}]}
],

/* Every page: one card per sidebar page, in the sidebar's order. go = the page's data-nav name. */
pages: [
  {go: 'today', t: 'Today', what: 'Your one thing to work on right now, your last race and your last practice.', when: 'Every time you open Debrief.'},
  {go: 'prep', t: 'Prepare', what: "This week's race: schedule, your history at the track, track card notes, Plan fuel and Plan stints.", when: 'The days before a race.'},
  {go: 'coach', t: 'Coach', what: 'Short spoken cues while you drive, from your own best lap. Also the crew chief, overlay, driving view and corner notes.', when: 'Every time you drive.'},
  {go: 'room', t: 'Race Room', what: 'The pit board for team and long races: driver, stint timer, fuel, box calls and every stop in your class.', when: 'During team and endurance races.'},
  {go: 'sessions', t: 'All sessions', what: 'Every race, qualifying and practice session Garage 61 has for you. Pick one to review it.', when: 'To review an older session.'},
  {go: 'practice', t: 'Practice', what: 'Every time you drove, from your telemetry files: best and typical lap, best possible lap, the Corner coach and One thing this session.', when: 'Straight after you drive.'},
  {go: 'racing', t: 'Racing', what: 'Your starts and incidents across all your races, and the patterns in them.', when: 'Every few races.'},
  {go: 'setuplab', t: 'Setup Lab', what: 'What changed between your setups, which was faster, how the car handles in each corner and one change to try next.', when: 'When you work on the car.'},
  {go: 'cars', t: 'Car vs car', what: 'Which car of a class is quicker at a track for drivers of similar iRating, and where you were faster in each.', when: 'Choosing a car.'},
  {go: 'library', t: 'Progress', what: 'Am I getting faster? Your trend at every track and car.', when: 'Once a week.'},
  {go: 'season', t: 'Season', what: "This season's races in one place.", when: 'Looking back over a season.'},
  {go: 'skills', t: 'Skills', what: 'Your driver profile: braking, entry, middle and exit, against your own better laps.', when: 'To pick what to work on long-term.'},
  {go: 'goals', t: 'Goals', what: 'Target lap times for a track and car, and how close your races get.', when: 'Setting a target.'},
  {go: 'team', t: 'Team', what: "Leaderboards for your Garage 61 teams' tracks and cars, who owns each corner and how you stack up.", when: 'Learning from teammates.'},
  {go: 'settings', t: 'Settings', what: 'Garage 61, telemetry folder, units, live data, updates and Copy diagnostics.', when: 'Setting up, or when something is wrong.'}],

/* the glossary's groups: a term not listed here goes under More words */
groups: [
  ['Laps and pace', ['Clean lap', 'Typical lap', 'Median', 'Best sectors added up', 'Best possible lap', 'Comparison lap', 'Reference lap', 'Pace drift', 'Consistency score', 'Possibly traffic', 'Session (Practice)', 'Sector time', 'Lone Qualify', "Teammate's lap"]],
  ['Corners and driving', ['Brake point', 'Apex', 'Coasting', 'Trail braking', 'Corner coach', 'Corner time', 'One thing this session', 'A guess (an experiment)', 'Hit it', 'Quicker, No clear difference', 'Hard zones', 'Focus corner', 'Fixed corner']],
  ['How sure', ['Sure, Likely, A hint, A guess', 'Not clear yet', 'Not enough to tell', 'Likely, A hint (Car vs car)', 'Pattern (Racing)', 'Setup ideas: A hint, A guess']],
  ['The coach', ['Coaching level', "How's my pace?", 'Checked before it plays', 'Say lap times', 'Brake marker', 'Coach overlay', 'Driving view', 'Wheel button mute', 'Mute', 'Voice pack', 'Audio delay', 'Coach log', 'Corner note', 'Notes only']],
  ['Car and setup', ['Setup', 'Open or fixed setup', 'Fixed setup', 'Laps counted', 'Pushes, Rotates', 'Countersteer', 'Yaw rate', 'Hot pressure', 'kPa', 'Share what changed', 'Similar drivers (Car vs car)']],
  ['Racing and strategy', ['Stint', 'Fair share', 'Driver swap', 'Race log', 'On the road', 'Rolling or standing start', 'Places gained or lost on lap 1', 'Places seen changing', 'Your own, Contact, Caught up, Not known', 'Incident points']],
  ['Files and sharing', ['Recorded by Debrief', 'Resolve markers', 'SHA-256 checksum', 'Discord webhook']]]
});
