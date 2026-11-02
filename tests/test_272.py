"""2.7.0 second-verifier fixes: mixed "Session" reviews stay races and are replaced (not duplicated) when the race
is reviewed again; a race with fewer than 2 laps falls back to a session that has laps; Windows "unreachable"
errors count as offline; the window shell reads "#tab&incprac"; the scrubber doesn't eat ordinary words.
Run: python tests/test_272.py"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
TMP = Path(tempfile.mkdtemp(prefix="debrief-272-"))
os.environ.setdefault("DEBRIEF_SETTINGS", str(TMP / "settings.toml"))
os.environ.setdefault("DEBRIEF_CACHE_DIR", str(TMP / "cache"))

from debrief import app, g61, library  # noqa: E402
import test_271  # noqa: E402

QUIET = lambda m: None  # noqa: E731
_n = [0]


def _newdir() -> Path:
    _n[0] += 1
    d = TMP / f"d{_n[0]}"
    d.mkdir(parents=True)
    return d


def _review(kinds, lib):
    api = test_271._MixedApi(kinds)
    cfg = {"token": "test-token-123456", "library_folder": str(lib), "cache_dir": str(_newdir()),
           "include_practice": True, "max_reference_laps": 0, "salutation": ""}
    real_make, real_say = app.make_api, app.say
    logs = []
    app.make_api, app.say = (lambda c: api), logs.append
    try:
        page = app.cmd_review(cfg, "EV1", open_after=False, track_id=468, car_id=190)
    finally:
        app.make_api, app.say = real_make, real_say
    return json.loads((page.parent / "review.json").read_text(encoding="utf-8")), page, logs


def _make_legacy(lib, race_name, suffix="_practice"):
    """A 2.6/2.7.0-style mixed review: meta.session "Session" in a _practice folder, with a typed-in result."""
    old = Path(lib) / "reviews" / (race_name + suffix)
    old.mkdir(parents=True)
    (old / "review.json").write_text(json.dumps({"meta": {"session": "Session", "track": "x", "car": "y"},
                                                 "laps": []}), encoding="utf-8")
    (old / "result.json").write_text(json.dumps({"finish": 7}), encoding="utf-8")
    return old


def test_session_in_a_practice_folder_counts_as_a_race():
    assert library.row_session("Session", "2026-10-20_x_y_ev1_practice") == "Session"
    assert library.is_race({"session": library.row_session("Session", "a_practice")})
    assert library.row_session("Practice", "a_practice") == "Practice"


def test_reviewing_the_race_renames_the_old_mixed_review_instead_of_duplicating():
    lib = _newdir() / "lib"
    _, page, _ = _review([1] * 6 + [3] * 8, lib)             # find the race folder's name
    race_name = page.parent.name
    shutil.rmtree(page.parent)
    old = _make_legacy(lib, race_name)
    d, page, logs = _review([1] * 6 + [3] * 8, lib)
    assert page.parent.name == race_name and d["meta"]["session"] == "Race"
    assert not old.exists(), "the old mixed folder should have become the race folder"
    folders = sorted(p.name for p in (Path(lib) / "reviews").iterdir() if p.is_dir())
    assert folders == [race_name], folders
    assert json.loads((page.parent / "result.json").read_text(encoding="utf-8")) == {"finish": 7}
    assert any("mixed practice and race" in m for m in logs), logs


def test_when_the_race_folder_exists_the_old_mixed_one_moves_to_backup():
    lib = _newdir() / "lib"
    _, page, _ = _review([1] * 6 + [3] * 8, lib)
    race_name = page.parent.name
    old = _make_legacy(lib, race_name, "_qualifying")
    _review([1] * 6 + [3] * 8, lib)
    folders = sorted(p.name for p in (Path(lib) / "reviews").iterdir() if p.is_dir())
    assert folders == [race_name], folders
    assert (Path(lib) / ".backup" / "replaced" / old.name / "review.json").is_file()


def test_a_real_practice_review_is_left_alone():
    lib = _newdir() / "lib"
    _, page, _ = _review([1] * 6 + [3] * 8, lib)
    race_name = page.parent.name
    prac = Path(lib) / "reviews" / (race_name + "_practice")
    prac.mkdir()
    (prac / "review.json").write_text(json.dumps({"meta": {"session": "Practice"}}), encoding="utf-8")
    _review([1] * 6 + [3] * 8, lib)
    assert prac.is_dir()


def test_a_race_with_one_lap_falls_back_to_a_session_with_laps():
    lib = _newdir() / "lib"
    d, page, _ = _review([1] * 6 + [2] * 3 + [3] * 1, lib)  # lap-1 crash: one race lap
    assert d["meta"]["session"] in ("Practice", "Qualifying") and len(d["laps"]) >= 2, d["meta"]
    d, _, _ = _review([1] * 6 + [3] * 2, _newdir() / "lib")   # two race laps is a race
    assert d["meta"]["session"] == "Race" and len(d["laps"]) == 2


def test_windows_unreachable_errors_are_offline():
    import requests
    for msg in ("[WinError 10065] A socket operation was attempted to an unreachable host",
                "[WinError 10051] A socket operation was attempted to an unreachable network",
                "[Errno 113] No route to host",
                "[WinError 10061] No connection could be made because the target machine actively refused it",
                "[Errno 11001] getaddrinfo failed"):
        e = requests.ConnectionError(OSError(msg))
        assert g61._is_offline(e), msg
    for msg in ("('Connection aborted.', RemoteDisconnected('Remote end closed connection without response'))",
                "[WinError 10054] An existing connection was forcibly closed by the remote host"):
        assert not g61._is_offline(requests.ConnectionError(msg)), msg


def test_window_shell_reads_the_tab_before_the_incprac_flag():
    js = (ROOT / "debrief" / "desktop" / "ui" / "app.js").read_text(encoding="utf-8")
    assert "const hash=(rest.split('#')[1]||'').split('&')[0];" in js


def test_scrubber_keeps_ordinary_words():
    saved = {k: os.environ.get(k) for k in ("USERNAME", "USER")}
    try:
        for name in ("Race", "Max", "Spa"):
            os.environ["USERNAME"] = os.environ["USER"] = name
            out = app.scrub_text(f"Reviewing your {name} at Circuit de Spa-Francorchamps, Max speed 150")
            assert "[user]" not in out, (name, out)
        os.environ["USERNAME"] = os.environ["USER"] = "Swazi"
        assert "[user]" in app.scrub_text("logged in as swazi")
        for line in ("monkey: banana", "hotkey=F5", "Track key: roval", "icon@2x.png"):
            assert app.scrub_text(line, names=False) == line, line
        for line, gone in (("access_token=abcdef123456", "abcdef123456"), ("token: abcdef123456", "abcdef123456"),
                           ("api_key=zzzzzz999999", "zzzzzz999999"), ("mail me at a.b@example.com", "a.b@example.com"),
                           ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz")):
            assert gone not in app.scrub_text(line, names=False), line
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


if __name__ == "__main__":
    failed = 0
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        t0 = time.time()
        try:
            fn()
            print(f"ok    {name} ({time.time() - t0:.1f}s)")
        except Exception:  # noqa: BLE001
            import traceback
            failed += 1
            print(f"FAIL  {name}")
            traceback.print_exc()
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
