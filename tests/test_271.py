"""Debrief 2.7.1: fixes from the independent verification of 2.7.0.

    python3 tests/ci_run.py tests/test_271.py

H1 session classification by exclusion (library.session_kind, strategy._is_race, the library page's isRace,
cmd_review's meta.session), M1/M2 secrets that can't be opened or protected, M5 the diagnostics scrubber,
M6 goals from race rows, L1/L2 library backups before a re-render, L4 the exit-speed note.
(M3 is in test_27_b.py, M4 in test_27_c.py, L3 in test_27_a.py.) Never contacts the network.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
TMP = Path(tempfile.mkdtemp(prefix="debrief-271-"))
for _k in ("DEBRIEF_SETTINGS", "DEBRIEF_CACHE_DIR", "DEBRIEF_SCHEDULE_DIR", "DEBRIEF_UPDATE_STATE"):
    os.environ.setdefault(_k, str(TMP / _k.lower()))

from debrief import app, goals, library, profile, report, strategy  # noqa: E402
from debrief.desktop import settings_io  # noqa: E402

import make_fixture_library as mfl  # noqa: E402

QUIET = lambda m: None  # noqa: E731


def _newdir() -> Path:
    return Path(tempfile.mkdtemp(dir=TMP))


def _fixture_lib() -> tuple[Path, list]:
    f = _newdir() / "lib"
    made = mfl.main(str(f))
    return f, made


def _set_session(f: Path, dname: str, label) -> None:
    p = f / "reviews" / dname / "review.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    if label is None:
        d["meta"].pop("session", None)
    else:
        d["meta"]["session"] = label
    p.write_text(json.dumps(d), encoding="utf-8")


# ================================================================ H1: race by exclusion
LABELS = {"Race": "race", "race": "race", " Race ": "race", "race 2": "race", "Heat": "race", "Feature": "race",
          "Session": "race", "": "race", None: "race", "Qualify": "qualifying", "Qualifying": "qualifying",
          "Lone Qualify": "qualifying", "lone qualifying": "qualifying", "Practice": "practice",
          "Open Practice": "practice", "Warmup": "practice", "Warm up": "practice", "  open   practice": "practice"}


def test_session_kind_classifies_by_exclusion():
    for label, want in LABELS.items():
        assert library.session_kind(label) == want, (label, library.session_kind(label))
        assert library.is_race({"session": label}) is (want == "race"), label
        assert strategy._is_race({"session": label}) is (want == "race"), label
    assert library.is_race({}) and library.is_race(None)


def test_library_page_israce_mirrors_session_kind():
    """The JS rule in templates/library.html gives the same answer as library.session_kind (run in node, or
    skipped when node isn't there)."""
    node = shutil.which("node")
    if not node:
        print("  (node not installed: JS check skipped)")
        return
    html = (ROOT / "debrief" / "templates" / "library.html").read_text(encoding="utf-8")
    a = html.index("const sessKind=")
    b = html.index("const isRace=", a)
    js = html[a:html.index("\n", b)] + "\n"
    labels = [k for k in LABELS]
    js += "const L=" + json.dumps(labels) + ";console.log(JSON.stringify(L.map(s=>[sessKind(s),isRace({session:s})])));"
    out = subprocess.run([node, "-e", js], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    for label, (kind, race) in zip(labels, got):
        assert kind == LABELS[label] and race is (LABELS[label] == "race"), (label, kind, race)


def test_session_and_heat_reviews_count_for_pbs_trends_and_season_after_a_re_render():
    """A 2.6-style library (layout 2.6.0) with reviews labelled Session / Heat / Feature / None / Open Practice /
    Lone Qualify, re-rendered by refresh_all: the race-like ones count for PBs, season, goals and the skills
    profile; only the practice and qualifying ones are left out. A "Session" review whose folder says
    _practice (saved 2.1-2.7.0 with practice included) counts as a race too: it may hold the race."""
    f, made = _fixture_lib()
    (f / ".layout").write_text("2.6.0", encoding="utf-8")
    labels = ["Session", "Heat", "Feature", None, "Open Practice", "Lone Qualify", "Session", "Race", "Race"]
    for d, lab in zip(made, labels):
        _set_session(f, d, lab)
    # one more "Session" review, saved by a practice-included review of a practice session
    src = f / "reviews" / made[-1]
    prac = f / "reviews" / (made[-1] + "_practice")
    shutil.copytree(src, prac)
    _set_session(f, prac.name, "Session")
    report.refresh_all(f, log=QUIET)
    assert (f / ".layout").read_text(encoding="utf-8") == report.LAYOUT
    rows = library.list_reviews(f)
    by = {r["dir"]: r for r in rows}
    assert library.is_race(by[prac.name])   # 2.7.0 second verifier: a mixed review can hold the race
    excluded = {made[4], made[5]}
    for d in made:
        assert library.is_race(by[d]) is (d not in excluded), (d, by[d]["session"])
    races = library.race_rows(rows)
    assert len(races) == len(made) - 2 + 1   # + the mixed _practice "Session" review
    # season summary and the skills profile see the Session / Heat / Feature reviews
    ss = library.season_summary(rows)
    assert sum(s["races"] for s in ss["seasons"]) == len(races), ss
    pr = profile.build(rows)
    assert pr["n_practice_skipped"] == 2, pr
    # personal bests: a "Session" review that is the quickest at its combo is a PB
    pbs = [r for r in rows if (r.get("pb") or {}).get("is_pb")]
    assert any(r["session"] in ("Session", "Heat", "Feature", None) for r in pbs), [(r["dir"], r["session"]) for r in pbs]
    # the library page carries those rows as races (isRace in the page: tested above)
    html = (f / "index.html").read_text(encoding="utf-8")
    assert '"session":"Heat"' in html and '"session":"Open Practice"' in html


class _MixedApi:
    """cmd_review over an event with practice and race laps (include_practice ticked)."""

    def __init__(self, kinds):
        self.requests_made = 0
        self.calls = []
        now = datetime(2026, 10, 20, 20, 0, tzinfo=timezone.utc)
        self.laps = [{"id": f"L{i}", "track": {"id": 468, "name": "Charlotte Motor Speedway", "variant": "Roval 2019"},
                      "car": {"id": 190, "name": "Ford Mustang GT4"}, "event": "EV1", "sessionType": k,
                      "startTime": (now + timedelta(minutes=2 * i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                      "lapTime": 99.5 + (i % 3) * 0.2, "clean": True, "driver": {"slug": "me"}}
                     for i, k in enumerate(kinds)]

    def me(self):
        return {"slug": "me", "firstName": "Test", "lastName": "Driver"}

    def teams(self, refresh=False):
        return []

    def cars(self, refresh=False):
        return [{"id": 190, "name": "Ford Mustang GT4"}]

    def tracks(self, refresh=False):
        return [{"id": 468, "name": "Charlotte Motor Speedway", "variant": "Roval 2019", "length": 3600}]

    def find_laps(self, fresh=False, **params):
        self.requests_made += 1
        if params.get("drivers") == ["following"]:
            return []
        return list(self.laps)

    def lap_csv(self, lap_id):
        import test_27_b
        self.calls.append(lap_id)
        return test_27_b._csv(99.5)


def _review_mixed(kinds, session_kind=None):
    api = _MixedApi(kinds)
    cfg = {"token": "test-token-123456", "library_folder": str(_newdir() / "lib"), "cache_dir": str(_newdir()),
           "include_practice": True, "max_reference_laps": 0, "salutation": ""}
    real_make, real_say = app.make_api, app.say
    app.make_api = lambda c: api
    app.say = QUIET
    try:
        page = app.cmd_review(cfg, "EV1", open_after=False, session_kind=session_kind, track_id=468, car_id=190)
    finally:
        app.make_api, app.say = real_make, real_say
    return json.loads((page.parent / "review.json").read_text(encoding="utf-8")), page, api


def test_review_with_practice_included_is_saved_as_the_session_it_reviewed():
    """H1: "Review my last race" with practice included never saves meta.session = "Session": an event with
    practice and race laps is reviewed as the race (its laps only), one with only practice laps as Practice."""
    d, page, api = _review_mixed([1] * 6 + [3] * 8)
    assert d["meta"]["session"] == "Race", d["meta"]["session"]
    assert len(d["laps"]) == 8 and all(c in [f"L{i}" for i in range(6, 14)] for c in api.calls), api.calls
    assert not page.parent.name.endswith(("_practice", "_qualifying"))
    assert library.is_race({"session": d["meta"]["session"]})
    d, page, _ = _review_mixed([1] * 8)
    assert d["meta"]["session"] == "Practice" and page.parent.name.endswith("_practice")
    d, page, _ = _review_mixed([1] * 4 + [2] * 6)   # no race: the newest session (qualifying)
    assert d["meta"]["session"] == "Qualifying" and page.parent.name.endswith("_qualifying")
    d, _, _ = _review_mixed([3] * 8, session_kind=3)
    assert d["meta"]["session"] == "Race"
    assert app.session_label(None) == "Race" and app.session_label(3) == "Race" and app.session_label(1) == "Practice"


# ================================================================ M1 / M2: secrets that can't be opened or protected
class _Dpapi:
    """settings_io's DPAPI on any OS: XOR "encryption" tied to an account byte. account() switches Windows
    account (blobs made under another one can't be opened, as with real DPAPI); fail_protect makes
    CryptProtectData fail."""

    def __init__(self):
        self.acct = 1
        self.fail_protect = False

    @property
    def fail_unprotect(self):
        return self.acct != 1

    @fail_unprotect.setter
    def fail_unprotect(self, v):   # True: now on "another account"; False: back on the first one
        self.acct = 2 if v else 1

    def call(self, protect, data):
        if protect:
            if self.fail_protect:
                raise OSError(5, "DPAPI call failed")
            return bytes([self.acct]) + bytes(b ^ 0x5A for b in data)
        if not data or data[0] != self.acct:
            raise OSError(13, "DPAPI call failed")
        return bytes(b ^ 0x5A for b in data[1:])


def _with_dpapi(fn):
    d = _newdir()
    sp = d / "settings.toml"
    fake = _Dpapi()
    saved = (os.environ.get("DEBRIEF_SETTINGS"), settings_io.dpapi_available, settings_io.secrets_protected,
             settings_io._dpapi_call, app.say)
    logs = []
    os.environ["DEBRIEF_SETTINGS"] = str(sp)
    settings_io.dpapi_available = lambda: True
    settings_io.secrets_protected = lambda: True
    settings_io._dpapi_call = fake.call
    app.say = logs.append
    settings_io._LOGGED_ONCE.clear()
    try:
        sp.write_text("token = 'g61_plaintexttoken1234'\nlibrary_folder = '%s'\n"
                      "discord_webhook = 'https://discord.com/api/webhooks/123456789012345678/%s'\n\n[extra]\nfoo = 1\n"
                      % ((d / "lib").as_posix(), "a" * 68), encoding="utf-8")
        fn(sp, fake, logs)
    finally:
        env, settings_io.dpapi_available, settings_io.secrets_protected, settings_io._dpapi_call, app.say = saved
        if env is None:
            os.environ.pop("DEBRIEF_SETTINGS", None)
        else:
            os.environ["DEBRIEF_SETTINGS"] = env
        settings_io._LOGGED_ONCE.clear()


def test_unreadable_token_is_not_no_token():
    def run(sp, fake, logs):
        settings_io.save({"sync_on_open": False})          # the plain secrets are rewritten protected
        text = sp.read_text(encoding="utf-8")
        assert "token_dpapi = " in text and "g61_plaintexttoken1234" not in text
        v = settings_io.public_view()
        assert v["token_saved"] and not v["token_unreadable"] and not v["discord_unreadable"]
        fake.fail_unprotect = True                          # another Windows account
        v = settings_io.public_view()
        assert v["token_saved"] is False and v["token_unreadable"] is True and v["discord_unreadable"] is True
        assert v["discord_saved"] is False
        raw = app._parse_toml(sp.read_text(encoding="utf-8"))
        assert settings_io.secret_state(raw, "token") == ("", "unreadable")
        assert settings_io.secret_state({}, "token") == ("", "absent")
        app.load_settings(); app.load_settings()
        said = [m for m in logs if "can't be opened on this Windows account" in m]
        assert len(said) == 2 and any("Garage 61 token" in m for m in said) and any("Discord webhook" in m for m in said), logs
        # an unrelated save while unreadable keeps the blobs
        settings_io.save({"check_updates": False})
        fake.fail_unprotect = False
        assert app.load_settings()["token"] == "g61_plaintexttoken1234"
        assert settings_io.public_view()["token_unreadable"] is False
        # pasting a new token on the other account replaces it (and that account can open it)
        fake.fail_unprotect = True
        v = settings_io.save({"token": "g61_newtoken_55556666"})
        assert v["token_saved"] and not v["token_unreadable"] and v["discord_unreadable"]
        assert app.load_settings()["token"] == "g61_newtoken_55556666"
    _with_dpapi(run)


def test_protect_failing_with_an_old_blob_saves_the_plain_token():
    def run(sp, fake, logs):
        settings_io.save({"sync_on_open": False})          # an old token_dpapi now exists
        fake.fail_protect = True
        v = settings_io.save({"token": "g61_brandnewtoken9999"})   # used to fail "edited by hand"
        assert v["token_saved"] and v["token_hint"] == "…9999"
        raw = app._parse_toml(sp.read_text(encoding="utf-8"))
        assert raw["token"] == "g61_brandnewtoken9999" and raw["token_dpapi"] == ""
        assert app.load_settings()["token"] == "g61_brandnewtoken9999"
        assert sum("couldn't encrypt the Garage 61 token" in m for m in logs) == 1, logs
        settings_io.save({"token": "g61_brandnewtoken0000"})
        assert sum("couldn't encrypt the Garage 61 token" in m for m in logs) == 1, logs   # once per process
    _with_dpapi(run)


def test_window_shows_the_unreadable_messages():
    html = (ROOT / "debrief" / "desktop" / "ui" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "debrief" / "desktop" / "ui" / "app.js").read_text(encoding="utf-8")
    msg = "Your saved Garage 61 token can't be opened on this Windows account. Paste it again."
    assert html.count(msg) == 2 and 'id="tokUnread"' in html and 'id="gTokUnread"' in html   # Settings + first-run guide
    assert "Your saved Discord webhook can't be opened on this Windows account. Paste it again." in html
    assert 'id="discordUnread"' in html
    for k in ("v.token_unreadable", "v.discord_unreadable", "$('tokUnread')", "$('gTokUnread')", "$('discordUnread')"):
        assert k in js, k


# ================================================================ M5: the diagnostics scrubber
def test_scrubber_catches_every_form_of_the_home_folder_and_secrets():
    from debrief.desktop.server import Handler
    keep = {k: os.environ.get(k) for k in ("USERPROFILE", "USERNAME", "HOME")}
    os.environ["USERPROFILE"] = r"C:\Users\Swazi Williams"
    os.environ["USERNAME"] = "Swazi Williams"
    known = set(app._KNOWN_SECRETS)
    try:
        tok = "g61_SECRETtoken_abcdef123456"
        hook = "https://discord.com/api/webhooks/123456789012345678/abcDEF_ghi-jkl"
        app.remember_secret("g61_OLDtoken_yyyyyyy88888")    # a token replaced earlier in this run
        cases = [
            (r"ERROR at C:\Users\Swazi Williams\AppData\Local\Debrief\x.py", r"ERROR at ~\AppData\Local\Debrief\x.py"),
            ("opened file:///C:/Users/Swazi%20Williams/Documents/Debrief%20Reviews/index.html",
             "opened file:///~/Documents/Debrief%20Reviews/index.html"),
            (r"short path C:\Users\SWAZIW~1\AppData\Local\Temp\x", r"short path ~\AppData\Local\Temp\x"),
            (r"lowercase c:\users\swazi williams\documents", r"lowercase ~\documents"),
            ("GET https://garage61.net/api/x?access_token=g61_OTHERtoken_zzzzzz999", "GET https://garage61.net/api/x?access_token=…"),
            ("Authorization: Basic dXNlcjpwYXNzd29yZDEyMzQ=", "Authorization: Basic …"),
            ("old token g61_OLDtoken_yyyyyyy88888 rejected (401)", "old token … rejected (401)"),
            ("POST " + hook + "?wait=true failed", "POST …?wait=true failed"),
            ("webhook https://discordapp.com/api/webhooks/123456789012345678/abcDEF_ghi-jkl",
             "webhook https://discordapp.com/api/webhooks/123456789012345678/…"),
            ("user swazi.williams+g61@gmail.com", "user [email]"),
            ("Bearer " + tok, "Bearer …"),
            ("token: " + tok.upper(), "token: …"),
            (r"\\?\C:\Users\Swazi Williams\Documents", r"\\?\~\Documents"),
            ("C:\\\\Users\\\\Swazi Williams\\\\Documents (json-escaped)", "~\\\\Documents (json-escaped)"),
            ("C:/Users/Swazi Williams/Documents", "~/Documents"),
            ('{"api_key": "abcdef123456", "password":"hunter2hunter2"}', '{"api_key": "…", "password":"…"}'),
            ("username Swazi Williams logged in as SWAZI WILLIAMS", "username [user] logged in as [user]"),
            ("Driver Alex Driver finished P4 at Charlotte", "Driver Alex Driver finished P4 at Charlotte"),
            ("nobody here", "nobody here"),
        ]
        for line, want in cases:
            got = Handler.scrub_diagnostics(line, [tok, hook])
            assert got == want, (line, got, want)
        # the log writer uses the same scrubber (but leaves bare names alone)
        assert app.scrub_text(r"at C:\Users\Swazi Williams\x token=abcdefgh1234", names=False) == r"at ~\x token=…"
        assert app.scrub_text("Swazi Williams", names=False) == "Swazi Williams"
    finally:
        for k, v in keep.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        app._KNOWN_SECRETS.clear()
        app._KNOWN_SECRETS.update(known)


# ================================================================ M6: goals agree with the library page
def test_goals_progress_counts_race_rows_only():
    f, made = _fixture_lib()
    goals.save_goal(f, {"track": mfl.INDY["track"], "car": mfl.INDY["car"], "target_s": 82.5})
    rows = library.list_reviews(f)
    indy = sorted([r for r in rows if r["track"] == mfl.INDY["track"]], key=lambda r: r["best"])
    _set_session(f, indy[0]["dir"], "Practice")   # the quickest Indy review was a practice session
    gp_default = goals.progress(f)[0]
    rows = library.list_reviews(f)
    gp_page = goals.progress(f, rows=library.race_rows(rows))[0]
    assert gp_default["best"] == gp_page["best"] and gp_default["races"] == gp_page["races"]
    assert gp_default["best"] != indy[0]["best"] and gp_default["races"] == len(indy) - 1
    # the server's /api/goals answers with the same numbers
    src = (ROOT / "debrief" / "desktop" / "server.py").read_text(encoding="utf-8")
    body = src[src.index("    def _get_goals(self, q):"):src.index("    def _get_profile(self, q):")]
    assert "library.race_rows(library.list_reviews(folder))" in body and "goals.progress(folder, rows=rows)" in body


# ================================================================ L1 / L2: backups before a re-render
def test_a_truncated_backup_is_made_again_atomically():
    f, made = _fixture_lib()
    report.backup_reviews(f, "2.6.0", log=QUIET)
    tgt = f / ".backup" / "2.6.0" / made[0] / "review.json"
    src = f / "reviews" / made[0] / "review.json"
    full = src.read_text(encoding="utf-8")
    tgt.write_text(full[: len(full) // 2], encoding="utf-8")   # a copy cut short
    n = report.backup_reviews(f, "2.6.0", log=QUIET)
    assert n == 1 and tgt.read_text(encoding="utf-8") == full
    assert not [p for p in tgt.parent.iterdir() if p.name != "review.json"]   # no temp file left behind
    # a backup of a review that has since been re-rendered is the one worth keeping: never overwritten
    old = tgt.read_text(encoding="utf-8")
    time.sleep(0.02)
    d = json.loads(full)
    d["extra_new_layout_key"] = "x" * 50
    src.write_text(json.dumps(d), encoding="utf-8")
    assert report.backup_reviews(f, "2.6.0", log=QUIET) == 0 and tgt.read_text(encoding="utf-8") == old


def test_a_review_that_cant_be_backed_up_is_not_rewritten_and_the_layout_stays_open():
    f, made = _fixture_lib()
    (f / ".layout").write_text("2.6.0", encoding="utf-8")
    # one review's backup folder is blocked by a file of that name
    (f / ".backup" / "2.6.0").mkdir(parents=True)
    (f / ".backup" / "2.6.0" / made[0]).write_text("not a folder", encoding="utf-8")
    before = (f / "reviews" / made[0] / "index.html").read_text(encoding="utf-8")
    logs = []
    n = report.refresh_all(f, log=logs.append)
    assert n == len(made) - 1, n
    assert (f / "reviews" / made[0] / "index.html").read_text(encoding="utf-8") == before
    assert (f / "reviews" / made[1] / "index.html").read_text(encoding="utf-8") != before
    assert any(made[0] in m and "kept as it was because it couldn't be backed up first" in m for m in logs), logs
    assert (f / ".layout").read_text(encoding="utf-8") == "2.6.0"   # tried again next launch
    # the whole .backup is a file: nothing is rewritten
    f2, made2 = _fixture_lib()
    (f2 / ".layout").write_text("2.6.0", encoding="utf-8")
    (f2 / ".backup").write_text("x", encoding="utf-8")
    assert report.refresh_all(f2, log=QUIET) == 0 and (f2 / ".layout").read_text(encoding="utf-8") == "2.6.0"
    # once the blocker is gone the next launch finishes the job
    (f / ".backup" / "2.6.0" / made[0]).unlink()
    assert report.refresh_all(f, log=QUIET) == len(made)
    assert (f / ".layout").read_text(encoding="utf-8") == report.LAYOUT
    assert (f / ".backup" / "2.6.0" / made[0] / "review.json").is_file()


# ================================================================ L4: exit speed measured at 150 m before 2.7.0
def test_old_reviews_exit_speeds_are_kept_and_flagged():
    f, made = _fixture_lib()
    (f / ".layout").write_text("2.6.0", encoding="utf-8")
    p = f / "reviews" / made[0] / "review.json"
    before = [c["summary"]["exit_mph"] for c in json.loads(p.read_text(encoding="utf-8"))["corners"]]
    report.refresh_all(f, log=QUIET)
    after = json.loads(p.read_text(encoding="utf-8"))
    assert [c["summary"]["exit_mph"] for c in after["corners"]] == before          # never recomputed
    assert not any("exit_at_m" in c for c in after["corners"])                      # never mixed in one review
    rows = library.list_reviews(f)
    assert all(r["exit_150"] for r in rows)
    pr = profile.build(rows)
    assert pr["exit_note"] == "Exit speeds from reviews before 2.7.0 were measured 150 m after the apex."
    assert pr["n_exit_150"] == len(library.race_rows(rows))
    # a 2.7.0 review (corners carry exit_at_m) is not flagged, and with none left there is no note
    d = json.loads(p.read_text(encoding="utf-8"))
    for c in d["corners"]:
        c["exit_at_m"] = 120.0
    assert library._exit_150(d) is False
    assert "exit_note" not in profile.build([dict(r, exit_150=False) for r in rows])
    html = (ROOT / "debrief" / "templates" / "library.html").read_text(encoding="utf-8")
    assert "SKP.exit_note" in html


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
