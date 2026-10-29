"""Write the 24-A sample fixtures (shapes from spec 2.1) for other workstreams to build against.

    python3 tests/fixtures_24_a/make_samples.py

Outputs (next to this file):
  sample_strategy.json         review.json "strategy" block (fuel_used source, one paired pit stop)
  sample_strategy_level.json   "strategy" from consecutive fuel-level drops, no pit flags
  sample_racecraft.json        review.json "racecraft" block (lap 1 in the analysis, traffic, unclean, pit)
  sample_racecraft_lap1_out.json  racecraft where lap 1 was left out of the analysis (raw time used)
  sample_prefill.json          GET api/strategy (strategy.prefill) with every source filled
  sample_prefill_empty.json    strategy.prefill with no data at all (every value None + the note)
  sample_plan_15.json          strategy.plan: 15 laps, 0 stops
  sample_plan_40.json          strategy.plan: 40 laps, 2 stops with windows
  sample_plan_timed.json       strategy.plan: 40 minute race
  sample_review.json           a full engine review (synthetic telemetry) carrying strategy + racecraft
  sample_strategy_cars.json    <library>/strategy.json
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import samples  # noqa: E402
from debrief import racecraft, strategy  # noqa: E402


def dump(name, obj):
    (HERE / name).write_text(json.dumps(obj, indent=1, allow_nan=False, ensure_ascii=False) + "\n",
                             encoding="utf-8")
    print("wrote", name)


def main():
    data = samples.engine_review()
    dump("sample_review.json", samples.trim_review(data))
    dump("sample_strategy.json", data["strategy"])
    dump("sample_racecraft.json", data["racecraft"])
    d2 = samples.engine_review(fuel="level", pits=False, multiclass=False, lap1_out=True)
    dump("sample_strategy_level.json", d2["strategy"])
    dump("sample_racecraft_lap1_out.json", d2["racecraft"])
    with tempfile.TemporaryDirectory() as tmp:
        cfg, store, week = samples.prefill_world(Path(tmp))
        dump("sample_prefill.json", strategy.prefill(cfg, store, None, samples.TRACK, [samples.CAR], week))
        dump("sample_strategy_cars.json", strategy.load_cars(cfg["library_folder"]))
        empty = Path(tmp) / "empty"
        empty.mkdir()
        dump("sample_prefill_empty.json", strategy.prefill({"library_folder": str(empty)}, None, None,
                                                           samples.TRACK, [samples.CAR], None))
    dump("sample_plan_15.json", strategy.plan({"laps": 15, "burn_l": 2.5, "tank_l": 47, "reserve_laps": 1}))
    dump("sample_plan_40.json", strategy.plan({"laps": 40, "burn_l": 2.5, "tank_l": 47, "reserve_laps": 1,
                                               "pit_loss_s": 50.1, "burn_n": 17}))
    dump("sample_plan_timed.json", strategy.plan({"minutes": 40, "lap_t": 85.9, "burn_l": 2.41, "p90": 2.52,
                                                  "tank_l": 47, "tank_estimated": True}))
    assert racecraft.VERSION == 1


if __name__ == "__main__":
    main()
