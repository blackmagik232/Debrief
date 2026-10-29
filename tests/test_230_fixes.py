"""Debrief 2.3.0 release-blocker fixes (verification report VERIFY-2.3.0.md: H1, M1, M2, M3, L1, L3).

    python3 tests/test_230_fixes.py

Offline: nothing here talks to Garage 61, iRacing or GitHub. Browser checks use Playwright + Chromium and
print SKIPPED when it isn't installed.
"""
from __future__ import annotations

import copy
import json
import math
import random
import shutil
import signal
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load(name, path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# both fixture folders have a synth.py: load each by path
synth_b = _load("synth_23b", ROOT / "tests" / "fixtures_23_b" / "synth.py")   # hand-built review dicts
synth_a = _load("synth_a", ROOT / "tests" / "fixtures_a" / "synth.py")        # engine input
from debrief import engine, insights, library, profile, report, skills, technique_plus  # noqa: E402

TPL = ROOT / "debrief" / "templates"


def _engine_race(n=16):
    """A review dict made by the real engine from the fixtures_a synthetic race (+ the skills block)."""
    meta, laps, tel = synth_a.race(n=n)
    return engine.analyze(meta, laps, tel, [], {}, track_length=3.0)


class _Alarm:
    """Fail (instead of hanging the suite) if the block runs longer than s seconds."""

    def __init__(self, s):
        self.s = s

    def __enter__(self):
        def boom(*a):
            raise TimeoutError(f"took more than {self.s} s")
        self.old = signal.signal(signal.SIGALRM, boom)
        signal.alarm(self.s)

    def __exit__(self, *a):
        signal.alarm(0)
        signal.signal(signal.SIGALRM, self.old)


# ================================================================ H1: profile trend needs significance
def _rows(vals, days=None, phase="overall"):
    rows = []
    for k, v in enumerate(vals):
        day = (days[k] if days else 7 * k)
        iso = f"2026-{1 + day // 28:02d}-{1 + day % 28:02d}"
        lp = {"braking": 1.0, "entry": 1.0, "mid": 1.0, "exit": 1.0, "overall": 6.0}
        lp[phase] = round(float(v), 2)
        rows.append({"dir": f"r{k:02d}", "date": iso, "started_at": iso + "T19:00:00+00:00", "car": "Ford Mustang GT4",
                     "skills": {"v": 1, "n_clean": 12, "lap_t": 90.0, "loss_pm": lp}})
    return rows


def _trend(vals, **kw):
    return profile.build(_rows(vals, **kw))["groups"][0]["phases"]["overall"]["trend"]


def test_mann_kendall_values_and_ties():
    S, p = profile.mann_kendall(list(range(10)))
    assert S == 45 and abs(p - math.erfc(44 / math.sqrt(125) / math.sqrt(2))) < 1e-12, (S, p)
    assert profile.mann_kendall([5.0] * 8) == (0, 1.0)          # all ties: no evidence at all
    assert profile.mann_kendall([1, 2]) == (0, 1.0)
    # ties shrink the variance: [1,1,2,2,3,3] -> S=12, var=(6*5*17 - 3*2*1*9)/18 = 25.33
    S, p = profile.mann_kendall([1, 1, 2, 2, 3, 3])
    assert S == 12 and abs(p - math.erfc((11 / math.sqrt((510 - 54) / 18)) / math.sqrt(2))) < 1e-12
    # the smallest possible p with 4 races is above 0.05: 4 races never give a direction
    assert profile.mann_kendall([4, 3, 2, 1])[1] > 0.05


def test_trend_stores_test_and_p():
    tr = _trend([8.0, 7.6, 7.1, 6.9, 6.4, 6.0, 5.7, 5.1, 4.8, 4.2])
    assert tr["test"] == "mann_kendall" and tr["direction"] == "improving" and tr["p"] < 0.05 and tr["mk_s"] < 0
    p = profile.build(_rows([6.0] * 5))
    assert p["params"]["trend_test"] == "mann_kendall" and p["params"]["trend_max_p"] == 0.05
    assert p["groups"][0]["phases"]["overall"]["trend"]["direction"] == "steady"


def test_noise_false_trend_rate_at_most_5_percent():
    """Verifier repro 2 (stationary Gaussian, sd 1.5 per-mille, weekly races): Improving/Slipping must be rare."""
    rng = random.Random(230)
    for n, trials in ((6, 300), (10, 300), (20, 150)):
        hits = 0
        for _ in range(trials):
            tr = _trend([max(0.0, rng.gauss(6.0, 1.5)) for _ in range(n)])
            hits += tr["direction"] != "steady"
        rate = hits / trials
        assert rate <= 0.06, (n, rate)


def test_shuffled_real_values_rarely_trend():
    """Verifier repro 1 shape: a fixed set of race values in random order (no trend by construction)."""
    base = [4.1, 7.9, 5.6, 6.3, 3.8, 8.4, 5.1, 6.9, 4.7, 7.2, 5.9, 6.6]
    rng = random.Random(7)
    hits = 0
    for _ in range(300):
        v = base[:10]
        rng.shuffle(v)
        hits += _trend(v)["direction"] != "steady"
    assert hits / 300 <= 0.06, hits


def test_real_improvement_over_10_races_is_found():
    # a clear fall (0.4 per-mille a race with a little noise) over 10 weekly races
    rng = random.Random(3)
    vals = [8.0 - 0.4 * k + rng.gauss(0, 0.3) for k in range(10)]
    tr = _trend(vals)
    assert tr["direction"] == "improving" and tr["p"] < 0.05 and tr["n"] == 10 and tr["span_ok"], tr
    tr = _trend(vals[::-1])
    assert tr["direction"] == "slipping", tr
    # power with realistic noise (sd 1.5, 0.5 per-mille a race, 10 races): most runs still find it
    hits = sum(_trend([6 - 0.5 * k + rng.gauss(0, 1.5) for k in range(10)])["direction"] == "improving"
               for _ in range(200))
    assert hits / 200 >= 0.5, hits


def test_span_gate_still_says_not_enough_races():
    # 10 races in 9 days: strong fall but span_ok False -> the page says "Not enough races yet"
    tr = _trend([8 - 0.5 * k for k in range(10)], days=list(range(10)))
    assert tr["direction"] == "steady" and tr["span_ok"] is False
    # 3 races: no trend block at all
    assert _trend([8, 7, 6]) is None


# ================================================================ M1: bad grids never hang; refresh finishes
BAD_CASES = {
    "L=-1": lambda d: d.__setitem__("L", -1), "L=0": lambda d: d.__setitem__("L", 0),
    "L=1e-300": lambda d: d.__setitem__("L", 1e-300), "L=inf": lambda d: d.__setitem__("L", float("inf")),
    "L=nan": lambda d: d.__setitem__("L", float("nan")), "L=x": lambda d: d.__setitem__("L", "x"),
    "step=0": lambda d: d.__setitem__("step", 0), "step=-1": lambda d: d.__setitem__("step", -1),
    "step=nan": lambda d: d.__setitem__("step", float("nan")), "step=True": lambda d: d.__setitem__("step", True),
    "step=1e-9": lambda d: d.__setitem__("step", 1e-9),
    "L=-1,step=-1": lambda d: (d.__setitem__("L", -1), d.__setitem__("step", -1)),
    "corner end 1e300": lambda d: d["corners"][0].__setitem__("end_m", 1e300),
    "corner start -1e12": lambda d: d["corners"][0].__setitem__("start_m", -1e12),
}


def test_bad_grid_gives_bad_data_quickly():
    base = synth_b.exit_loss_review(n_laps=12)
    for name, mut in BAD_CASES.items():
        d = copy.deepcopy(base)
        mut(d)
        t = time.time()
        with _Alarm(5):
            s = skills.for_review(d)
        assert time.time() - t < 2, name
        assert isinstance(s, dict) and s["reason"] in ("bad_data", None), (name, s.get("reason"))
        if s["reason"] is None:      # a far-off corner is dropped, the rest still scores
            assert name.startswith("corner"), name
            assert all(math.isfinite(c["loss_raw"]["exit"]) for c in s["corners"])
        json.dumps(s, allow_nan=False)


def test_empty_or_junk_traces():
    base = synth_b.exit_loss_review(n_laps=12)
    d = copy.deepcopy(base)
    for tr in d["traces"].values():
        tr["tt"] = []
    assert skills.for_review(d)["reason"] == "no_traces"
    d = copy.deepcopy(base)
    for tr in d["traces"].values():
        tr["tt"][5] = float("nan")
    assert skills.for_review(d)["reason"] == "no_traces"
    d = copy.deepcopy(base)
    d["traces"]["me4"]["tt"][7] = None          # one lap with a hole: that lap is left out, the rest scores
    s = skills.for_review(d)
    assert s["reason"] is None and 4 not in s["typical_laps"]
    d = copy.deepcopy(base)
    d["traces"]["me2"]["b"] = "x"               # junk channel: bad_data, not an exception
    assert skills.for_review(d)["reason"] in ("bad_data", None)


def test_refresh_all_finishes_on_library_with_bad_review():
    lib = Path(tempfile.mkdtemp(prefix="debrief-230-"))
    try:
        good = synth_b.exit_loss_review(n_laps=12)
        bad = copy.deepcopy(good)
        bad["L"] = -1                       # the verifier's hang case
        worse = copy.deepcopy(good)
        worse["step"] = 0
        for name, d in (("2026-10-01_a_good", good), ("2026-10-02_b_badL", bad), ("2026-10-03_c_badstep", worse),
                        ("2026-10-04_d_good", copy.deepcopy(good))):
            (lib / "reviews" / name).mkdir(parents=True)
            (lib / "reviews" / name / "review.json").write_text(json.dumps(d), encoding="utf-8")
        seen = []
        with _Alarm(60):
            n = report.refresh_all(lib, log=seen.append)
        assert n == 4, (n, seen)
        assert (lib / ".layout").read_text(encoding="utf-8").strip() == report.LAYOUT
        for name, want in (("2026-10-01_a_good", None), ("2026-10-02_b_badL", "bad_data"),
                           ("2026-10-03_c_badstep", "bad_data"), ("2026-10-04_d_good", None)):
            d = json.loads((lib / "reviews" / name / "review.json").read_text(encoding="utf-8"))
            assert d["skills"]["reason"] == want, (name, d["skills"].get("reason"))
            assert (lib / "reviews" / name / "index.html").exists()
        rows = library.list_reviews(lib)
        assert sum(1 for r in rows if r.get("skills")) == 2
    finally:
        shutil.rmtree(lib, ignore_errors=True)


def test_technique_plus_and_session_review_survive_junk():
    from debrief import session_review
    d = synth_b.make_review(n_laps=12, session="Practice")
    d["laps"][2]["n"] = "x"
    d["laps"][3]["n"] = float("nan")
    d["corners"][1]["per_lap"]["5"]["brake_on"] = 1e308
    d["corners"][1]["per_lap"]["6"]["brake_on"] = -1e308
    with _Alarm(5):
        sr = session_review.for_review(copy.deepcopy(d))
        sp = technique_plus.spread_for_review(copy.deepcopy(d))
    assert sr["kind"] == "practice"
    json.dumps(sr, allow_nan=False)
    json.dumps(sp, allow_nan=False)


# ================================================================ M2: overall is the real gap
def _gap_by_hand(d, s):
    """Independent: per lap, time from each corner's B to E summed over corners (lap-wrapped, straight from
    the traces), better laps' median of that, then the median over typical laps of (lap - median)."""
    L, step = float(d["L"]), float(d["step"])

    def t_at(tr, x):
        tt = np.asarray(tr["tt"], float)
        dd = np.arange(len(tt)) * step
        T = float(tr["t"])
        dd, tt = np.append(dd, L), np.append(tt, T)
        q = math.floor(x / L)
        if x - q * L == 0 and q > 0:
            q -= 1
        return float(np.interp(x - q * L, dd, tt)) + q * T

    def tot(n):
        tr = d["traces"][f"me{n}"]
        return sum(t_at(tr, c["markers"]["E"]) - t_at(tr, c["markers"]["B"]) for c in s["corners"])
    ref = float(np.median([tot(n) for n in s["ref_laps"]]))
    return float(np.median([tot(n) - ref for n in s["typical_laps"]]))


def test_overall_equals_real_gap_synth():
    d = synth_b.exit_loss_review(loss=0.05, n_laps=12)
    s = skills.for_review(d)
    assert s["overall"]["basis"] == "zone_gap"
    assert abs(s["overall"]["loss_s"] - _gap_by_hand(d, s)) <= 0.01
    assert abs(s["overall"]["loss_s"] - 0.05) <= 0.01


def test_overall_equals_real_gap_engine_review():
    """On a real-engine review where the summed phase medians differ from the gap, overall is the gap."""
    for n in (14, 16, 20):
        d = _engine_race(n=n)
        s = skills.for_review(d)
        assert s["reason"] is None, s.get("reason")
        gap = _gap_by_hand(d, s)
        assert abs(s["overall"]["loss_s"] - max(0.0, gap)) <= 0.01, (n, s["overall"], gap)
        assert abs(s["overall"]["gap_raw_s"] - gap) <= 0.01
        shares = [v["share"] for v in s["phases"].values() if v["share"] is not None]
        if s["overall"]["sum_s"] > 0:
            assert abs(sum(shares) - 1.0) <= 0.01, shares
        assert abs(s["overall"]["sum_s"] - sum(v["loss_s"] for v in s["phases"].values() if v["loss_s"] is not None)) < 0.002


def test_mixed_review_sum_differs_but_overall_is_gap():
    # typical laps lose in C2 exit; better laps 1-3 each have one different random slow stretch -> the
    # per-phase medians against a composite reference add up to more than the real gap
    extra = {n: [(2060.0, 2250.0, 0.08)] for n in range(4, 13)}
    extra[1] = [(1850.0, 1970.0, 0.06)]
    extra[2] = [(2000.0, 2060.0, 0.06)]
    extra[3] = [(450.0, 800.0, 0.06)]
    d = synth_b.make_review(n_laps=12, extra=extra, lap_t_jitter={1: -1, 2: -1, 3: -1})
    s = skills.for_review(d)
    gap = _gap_by_hand(d, s)
    assert abs(s["overall"]["loss_s"] - max(0.0, gap)) <= 0.01, (s["overall"], gap)


def test_phase_insight_wording():
    d = _engine_race(16)
    d["skills"] = skills.for_review(d)
    sk = d["skills"]
    # force a dominant exit loss so the item fires
    sk["phases"]["exit"].update(loss_s=0.4, loss_pm=4.5)
    for p in ("braking", "mid"):
        if sk["phases"][p]["loss_s"] is not None:
            sk["phases"][p].update(loss_s=0.05, loss_pm=0.6)
    sk["overall"]["loss_s"] = 0.37
    it = next(b for b in insights.build(d)["bad"] if b["key"] == "phase")
    assert "of the" not in it["detail"] and "where the time goes" in it["detail"], it["detail"]
    assert "0.37 s slower than your better laps" in it["detail"], it["detail"]
    for w in ("will", "costing", "because", "causes"):
        assert f" {w} " not in f" {it['text']} {it['detail']} ", w


# ================================================================ M3: unmeasured phases
def test_zero_length_phase_is_not_scored():
    d = _engine_race(16)
    s = skills.for_review(d)
    unmeasured = [p for p, v in s["phases"].items() if v["corners"] == 0]
    assert unmeasured, "this synth trail-brakes to the apex, so at least one phase has no length"
    for p in unmeasured:
        v = s["phases"][p]
        assert v["loss_s"] is None and v["score"] is None and v["loss_pm"] is None and v["share"] is None, v
    for c in s["corners"]:
        for p in unmeasured:
            assert c["loss"][p] is None
    comp = skills.compact(s)
    assert not (set(unmeasured) & set(comp["loss_pm"])), comp
    # the profile then has no tile for it (instead of "100/100, best 100, Steady")
    rows = [{"dir": f"r{k}", "date": f"2026-10-{1 + 7 * k:02d}", "started_at": f"2026-10-{1 + 7 * k:02d}T19:00:00+00:00",
             "car": "Ford Mustang GT4", "skills": comp} for k in range(4)]
    ph = profile.build(rows)["groups"][0]["phases"]
    assert not (set(unmeasured) & set(ph)), list(ph)


def test_page_shows_na_and_honest_total():
    b = _browser()
    if b is None:
        return
    d = _engine_race(16)
    d["skills"] = skills.for_review(d)
    tmp = Path(tempfile.mkdtemp(prefix="debrief-230p-"))
    try:
        p = tmp / "r.html"
        p.write_text(report.fill((TPL / "review.html").read_text(encoding="utf-8"), {"/*DATA*/null": report._embed(d)}),
                     encoding="utf-8")
        ctx = b.new_context(viewport={"width": 1280, "height": 900})
        pg = ctx.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.route("**/fonts.googleapis.com/**", lambda r: r.abort())
        pg.goto(p.resolve().as_uri())
        pg.wait_for_timeout(450)
        rows = pg.evaluate("[...document.querySelectorAll('#wicRows .wicrow')].map(r=>[r.dataset.phase,r.innerText.replace(/\\n/g,' ')])")
        foot = pg.inner_text("#wicFoot")
        ctx.close()
        assert not errs, errs
        un = [k for k, v in d["skills"]["phases"].items() if v["corners"] == 0]
        for k, txt in rows:
            if k in un:
                assert "n/a" in txt and "100" not in txt, (k, txt)
            else:
                assert "%" in txt, (k, txt)
        assert "All four together" not in foot and "not measured" in foot, foot
        assert f"{d['skills']['overall']['loss_s']:.2f} s" in foot and "slower than your better laps" in foot, foot
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ================================================================ L1 / L3
def test_report_write_never_writes_nan_or_infinity():
    d = synth_b.exit_loss_review(n_laps=12)
    d["corners"][1]["per_lap"]["5"]["brake_on"] = 1e308       # the verifier's case (std overflows)
    d["junk"] = {"a": float("inf"), "b": [1.0, float("nan"), {"c": float("-inf")}]}
    d["median"] = float("nan")
    tmp = Path(tempfile.mkdtemp(prefix="debrief-230w-"))
    try:
        report.write(tmp, d)
        txt = (tmp / "review.json").read_text(encoding="utf-8")

        def strict(x):
            raise ValueError("non-strict JSON constant " + x)
        back = json.loads(txt, parse_constant=strict)
        assert back["junk"] == {"a": None, "b": [1.0, None, {"c": None}]} and back["median"] is None
        assert ":NaN" not in (tmp / "index.html").read_text(encoding="utf-8")   # the embedded copy too
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    assert report.sanitize({"x": [float("nan"), 2.0]}) == {"x": [None, 2.0]}


def test_targets_gain_wording():
    src = (TPL / "review.html").read_text(encoding="utf-8")
    assert "'−'+t.gain_s.toFixed(2)" not in src
    assert "s to find'" in src and "'To find'" in src


# ---------------------------------------------------------------- browser helper
_PW = {}


def _browser():
    if "b" in _PW:
        return _PW["b"]
    try:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        _PW["pw"], _PW["b"] = pw, pw.chromium.launch()
    except Exception:  # noqa: BLE001
        print("  SKIPPED: Playwright/Chromium not available")
        _PW["b"] = None
    return _PW["b"]


if __name__ == "__main__":
    fails = 0
    tests = [(k, v) for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for name, fn in tests:
        try:
            fn()
            print("ok   ", name)
        except Exception as e:  # noqa: BLE001
            fails += 1
            import traceback
            traceback.print_exc()
            print("FAIL ", name, e)
    if _PW.get("b"):
        _PW["b"].close()
        _PW["pw"].stop()
    print(f"{len(tests) - fails}/{len(tests)} passed")
    sys.exit(1 if fails else 0)
