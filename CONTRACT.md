# Contract between Debrief (public) and Siyanqaba (private)

Siyanqaba is Swazi's own offline engine on the Zerver. It uses Debrief's numbers and narrates Debrief's fact sheets.
Any change to a contract below needs a version bump here AND in Siyanqaba's copy, and passing tests on both sides.

| # | Contract | Owner | Version | Where |
|---|---|---|---|---|
| C1 | **Analysis interface:** the only Debrief module Siyanqaba imports for numbers. | Debrief | 1 (Debrief 2.30.1) | `debrief/c1.py`, test `tests/test_2301_c1.py` |
| C2 | **Fact sheet:** JSON Debrief writes and Siyanqaba narrates. | Debrief | 1 (Debrief 2.30.1) | `debrief/factsheet.py` (`build`, `check`) |
| C3 | **Zerver connection:** Debrief to Ollama `/api/chat` on the home network, off by default, timeout then the plain text. | Siyanqaba | 1 | Model `qwen3:4b-instruct-2507-q4_K_M` |

## C1 v1

`from debrief import c1` (numpy only; no window, sound, network or irsdk):

| Name | Wraps | Gives |
|---|---|---|
| `review_session(paths, history=None, race_minutes=None, race_laps=None)` | `sessionibt.review` | Laps (valid, clean and why), consistency, best possible lap, segment spread, fuel, stints, notes, thresholds |
| `read_laps(path)` | `ibt.open_ibt` + `ibt.laps` | One file's laps: time, timed by iRacing or samples, pit, off track, incidents, start lap |
| `corner_cards(paths)` | `cornercoach.analyze_files` | Per-corner advice that passed the permutation (shuffle) test; unclear and fine corners |
| `habit_p(times, values)` | `insights.luck_p` | Shuffle-test p for a pedal habit |
| `detect_corners`, `build_segments` | `engine` | Corner and segment maths for resampled laps |
| `session_id(paths)` | | 16 hex characters from the file names and sizes; the same on both machines |
| `same_corner`, `corner_key`, `corner_tolerance_m` | | Corners match by apex position within max(60 m, 2% of the lap), never by number |
| `THRESHOLDS` | | Every cut-off the claims depend on |

Install on the Zerver, pinned: `pip install "debrief @ git+https://github.com/blackmagik232/debrief@v2.30.1"`.

## C2 v1

```json
{
  "factsheet_version": "1",
  "producer": {"app": "debrief", "version": "2.30.1", "c1": "1"},
  "session": {"id": "9f3c1a7e5b2d4c88", "track": "Circuit Zolder (Grand Prix)", "track_key": "zolder gp",
              "car": "McLaren 570S GT4", "car_path": "mclaren570sgt4", "kind": "practice", "date": "2026-11-10",
              "units": {"speed": "mph", "fuel": "L"}},
  "corners": [{"id": "C6", "label": "Turn 6", "apex_pct": 0.7927, "name": null, "name_source": "detected"}],
  "required": ["focus"],
  "intent": "focus",
  "clauses": {"best": "best lap 1:35.038, best corners added up 1:34.876",
              "consistency": "4 of 13 clean laps within half a second of your best",
              "fuel": "fuel use 2.45 liters a lap",
              "focus": "in Turn 6 your best laps were about 0.25 seconds quicker; release the brake about 10 m earlier",
              "feel": "you noted Turn 3 felt loose on exit",
              "trend": "best lap 0.31 seconds quicker than your last session here"},
  "proven": {"best": true, "consistency": true, "fuel": true, "focus": true, "feel": true, "trend": true},
  "not_proven_yet": ["best possible lap (needs 2 more laps)"]
}
```

Rules:
- Clause keys are exactly `summary`, `best`, `consistency`, `fuel`, `focus`, `feel`, `trend`. A new key is a version bump.
- Every number lives inside a clause, with its unit in words. Units follow Debrief's Settings > Units (mph by default).
- `focus` and `required` come only from a corner card that passed the shuffle test with "likely" or "clear" confidence.
  Anything weaker, or short of laps, goes in `not_proven_yet` with what is missing, never in `clauses`.
- `intent` after a session is `"focus"` or `null`. `pit`, `stay`, `attack`, `defend` are live calls and are reserved:
  no AI during a live session.
- Corners are named by where they are on the lap (`apex_pct`). `C3` is a detected corner, not the track's official turn 3.
- `factsheet.check(sheet)` returns `[]` for a sheet that keeps these rules. Siyanqaba refuses unknown versions.

## Release flow

1. Debrief tags a release (`v2.30.1` ...).
2. A pull request in Siyanqaba moves the pin to that tag.
3. Siyanqaba's tests run the contract check, the regression suite and the answer key.
4. Merge only if the scores are at least as good as the last entry in `answer-key-history.jsonl`.
