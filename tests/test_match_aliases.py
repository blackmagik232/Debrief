"""Track/car name matching: real spelling differences still match, different layouts and cars never do.

    python3 tests/test_match_aliases.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from debrief.schedule import same_track, same_car  # noqa: E402

SAME_TRACK = [("Mount Panorama Circuit", "Mount Panorama Motor Racing Circuit"),
              ("Sebring International Raceway", "Sebring International Raceway - International"),
              ("Charlotte Motor Speedway (Roval 2019)", "Charlotte Motor Speedway - Roval 2025"),
              ("Summit Point Raceway", "Summit Point Raceway - Summit Point Raceway"),
              ("Nürburgring Grand-Prix-Strecke (GP)", "Nurburgring Grand-Prix-Strecke - Grand Prix")]
DIFF_TRACK = [("Indianapolis Motor Speedway (Oval)", "Indianapolis Motor Speedway - Grand Prix"),
              ("Atlanta Motor Speedway", "Road Atlanta - Full Course"),
              ("Okayama International Circuit (Full Course)", "Okayama International Circuit - Short"),
              ("Watkins Glen International (Boot)", "Watkins Glen International - Classic Boot")]
SAME_CAR = [("Mazda MX-5 Cup", "Global Mazda MX-5 Cup"), ("Porsche 963 GTP", "Porsche 963"),
            ("Ford Mustang GT4", "Ford Mustang GT4")]
DIFF_CAR = [("Porsche 911 GT3 Cup (992)", "Porsche 911 GT3 R (992)"), ("BMW M4 GT3", "BMW M4 GT3 EVO"),
            ("Global Mazda MX-5 Cup", "Toyota GR86"), ("Porsche 963 GTP", "Porsche 911 GT3 R (992)")]


def test_tracks():
    bad = [p for p in SAME_TRACK if not same_track(*p)] + [p for p in DIFF_TRACK if same_track(*p)]
    assert not bad, bad


def test_cars():
    bad = [p for p in SAME_CAR if not same_car(*p)] + [p for p in DIFF_CAR if same_car(*p)]
    assert not bad, bad


if __name__ == "__main__":
    failed = 0
    for name, fn in [(n, f) for n, f in globals().items() if n.startswith("test_")]:
        try:
            fn()
            print(f"ok    {name}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {name}: {e}")
    print(f"{2 - failed}/2 passed")
    sys.exit(1 if failed else 0)
