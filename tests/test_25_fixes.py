"""Debrief 2.5.0 release review fixes (VERIFY-2.5.0.md M1, L1-L7): one regression test per finding.

    python3 tests/test_25_fixes.py

Never contacts Garage 61, iRacing, GitHub or Discord: Garage 61 is a recording fake with no network code,
Discord is a fake session, and the one browser test (L7) runs a local page with no network at all.
Windows-safe: tempfile only, utf-8 opens, faulthandler as the time guard.
"""
from __future__ import annotations

import faulthandler
import json
import os
import random
import re
import shutil
import struct
import sys
import tempfile
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
TMP = Path(tempfile.mkdtemp(prefix="debrief-25fix-"))
os.environ.setdefault("DEBRIEF_SCHEDULE_DIR", str(TMP / "schedules"))
os.environ.setdefault("DEBRIEF_CACHE_DIR", str(TMP / "cache"))
os.environ.setdefault("DEBRIEF_SETTINGS", str(TMP / "settings.toml"))
os.environ.setdefault("DEBRIEF_UPDATE_STATE", str(TMP / "update.json"))
faulthandler.dump_traceback_later(300, exit=True)

from debrief import discord, leaderboards, schedule, updater  # noqa: E402

NOW = datetime(2026, 10, 30, 19, 0, 0, tzinfo=timezone.utc)      # Friday of week 7
WEEK1 = datetime(2026, 9, 16, tzinfo=timezone.utc).date()        # week 7 starts 2026-10-28
TRACKS = [{"id": 51, "name": "Okayama International Circuit", "variant": "Full Course"},
          {"id": 18, "name": "Road America", "variant": "Full Course"}]
CARS = [{"id": 67, "name": "Global Mazda MX-5 Cup"}, {"id": 170, "name": "Porsche 963 GTP"},
        {"id": 180, "name": "BMW M4 GT3 EVO"}]
TRACK_NAMES = {51: "Okayama International Circuit - Full Course", 18: "Road America - Full Course"}
MASS = re.compile(r"@(everyone|here)", re.I)


# ---------------------------------------------------------------- Garage 61 fakes (no network code)
class FakeApi:
    def __init__(self, laps_fn, me_saved=True, fail_after=None):
        self.calls, self.requests_made = [], 0
        self.laps_fn, self.me_saved, self.fail_after = laps_fn, me_saved, fail_after

    def peek(self, key):
        return {"slug": "swazi", "firstName": "Swazi", "lastName": "Williams"} if key == "me" and self.me_saved else None

    def me(self):
        self.calls.append(("me", {}))
        self.requests_made += 1
        return {"slug": "swazi", "firstName": "Swazi", "lastName": "Williams"}

    def cached_items(self, name):
        return {"tracks": TRACKS, "cars": CARS}.get(name, [])

    def find_laps(self, **kw):
        self.calls.append(("find_laps", kw))
        self.requests_made += 1
        if self.fail_after is not None and kw["after"] < self.fail_after:
            raise RuntimeError("Garage 61 fell over")
        return self.laps_fn(kw)

    def laps(self):
        return [c for c in self.calls if c[0] == "find_laps"]


def lap(slug, t, day="2026-10-29T20:00:00Z", car=67, **flags):
    return {"id": f"{slug}-{t}-{car}", "lapTime": t, "clean": True, "startTime": day, "car": {"id": car},
            "driverRating": 2000, "driver": {"slug": slug, "firstName": slug.split("-")[0].title(), "lastName": "X"},
            **flags}


def in_week(kw, hours=30):
    a = datetime.fromisoformat(kw["after"].replace("Z", "+00:00")) + timedelta(hours=hours)
    return a.isoformat().replace("+00:00", "Z")


def make_series(name, cars, track_id=51, first_week=1, weeks=12):
    classes = sorted({c for c in (schedule.class_of(x) for x in cars) if c})
    return {"id": schedule.slug(name), "name": name, "cars": cars, "classes": classes, "multiclass": len(classes) >= 2,
            "weeks": [{"week": n, "start": (WEEK1 + timedelta(days=7 * (n - 1))).isoformat(),
                       "track": TRACK_NAMES[track_id], "race_time": None, "length": "15 laps", "conditions": None}
                      for n in range(first_week, weeks + 1)]}


def make_sched(series_list):
    return {"schema": 1, "season": "2026 Season 4", "year": 2026, "quarter": 4,
            "weeks": [{"week": n, "start": (WEEK1 + timedelta(days=7 * (n - 1))).isoformat()} for n in range(1, 13)],
            "series": series_list}


def mine(sched):
    return {"chosen": True, "series": [{"id": s["id"], "name": s["name"]} for s in sched["series"]]}


def lib():
    return {"library_folder": str(Path(tempfile.mkdtemp(dir=TMP))), "team_slug": "midfield-menace"}


def at(dt):
    leaderboards._now = lambda: dt


def quiet(_m):
    pass


def MX5(**k):
    return make_series("Global Mazda MX-5 Fanatec Cup", ["Global Mazda MX-5 Cup"], **k)


def board_of(cfg, sched, week=7):
    snap = leaderboards.load_snapshot(cfg["library_folder"], "2026-season-4", sched["series"][0]["id"], week)
    return [(r["key"], r["t"]) for r in snap["board"]]


def add_review(cfg, best, clean=True, track="Okayama International Circuit - Full Course",
               car="Global Mazda MX-5 Cup", track_id=51, car_id=67, name="r1"):
    d = Path(cfg["library_folder"]) / "reviews" / name
    d.mkdir(parents=True, exist_ok=True)
    rv = {"meta": {"track": track, "car": car, "started_at": "2026-10-20T20:00:00+00:00"},
          "combo_ids": {"track_id": track_id, "car_id": car_id}, "best": {"t": best, "clean": clean}}
    (d / "review.json").write_text(json.dumps(rv), encoding="utf-8")


# ================================================================ M1: partial laps never take over a board
def test_m1_majority_of_unflagged_partials_never_takes_the_board():
    """The VERIFY repro (r_leader.py case 6): 2 of 3 returned bests are unflagged 11-12 s partial laps."""
    at(NOW)
    cfg, sched = lib(), make_sched([MX5(first_week=7)])

    def laps(kw):
        d = in_week(kw)
        return [lap("jordan-x", 11.0, day=d), lap("sam-y", 12.0, day=d), lap("swazi", 98.9, day=d)]
    api = FakeApi(laps)
    res = leaderboards.refresh(api, cfg, sched, mine(sched), log=quiet)
    assert board_of(cfg, sched) == [("me", 98.9)], board_of(cfg, sched)
    assert res["requests"] == api.requests_made == 2       # the partials still earn the one second look


def test_m1_partials_never_push_out_a_real_lap_even_five_to_two():
    at(NOW)
    cfg, sched = lib(), make_sched([MX5(first_week=7)])

    def laps(kw):
        d = in_week(kw)
        part = [lap(f"p{i}-x", 6.0 + 3 * i, day=d) for i in range(5)]
        return part + [lap("swazi", 98.9, day=d), lap("jordan-x", 97.4, day=d)]
    leaderboards.refresh(FakeApi(laps), cfg, sched, mine(sched), log=quiet)
    assert board_of(cfg, sched) == [("jordan-x", 97.4), ("me", 98.9)], board_of(cfg, sched)


def test_m1_any_mix_of_partials_never_reaches_a_board():
    """Seeded fuzz of build_board: real laps 90-110 s, up to three times as many partials of 3-50 s."""
    rnd = random.Random(2505)
    a, b = datetime(2026, 10, 28).date(), datetime(2026, 11, 4).date()
    for _ in range(400):
        n_real, n_part = rnd.randint(1, 5), rnd.randint(0, 15)
        real = {f"r{i}": round(rnd.uniform(90, 110), 3) for i in range(n_real)}
        part = {f"p{i}": round(rnd.uniform(3, 50), 3) for i in range(n_part)}
        laps = [lap(k, t) for k, t in {**real, **part}.items()]
        rnd.shuffle(laps)
        board = leaderboards.build_board(laps, "swazi", "Swazi", {67: "Global Mazda MX-5 Cup"}, a, b, False)
        got = {r["key"]: r["t"] for r in board}
        assert got == real, (real, part, got)


def test_m1_multiclass_partials_stay_off_each_class_board():
    a, b = datetime(2026, 10, 28).date(), datetime(2026, 11, 4).date()
    names = {170: "Porsche 963 GTP", 180: "BMW M4 GT3 EVO"}
    laps = [lap("g1", 92.0, car=170), lap("g2", 10.0, car=170), lap("g3", 11.0, car=170),
            lap("t1", 104.0, car=180), lap("t2", 12.0, car=180), lap("t3", 13.5, car=180)]
    board = leaderboards.build_board(laps, "swazi", "Swazi", names, a, b, True)
    assert sorted((r["key"], r["class_pos"]) for r in board) == [("g1", 1), ("t1", 1)], board


def test_m1_library_best_is_the_reference_like_team_build():
    """Your best full lap at this track (from the review library) anchors the board: a clean but impossible
    160 s lap is dropped and the real laps stay, which laps alone couldn't tell apart."""
    at(NOW)
    cfg, sched = lib(), make_sched([MX5(first_week=7)])
    add_review(cfg, 98.2)
    add_review(cfg, 60.0, clean=False, name="r2")           # a flagged best never counts as the reference
    add_review(cfg, 20.0, track="Road America - Full Course", track_id=18, name="r3")   # another track

    def laps(kw):
        d = in_week(kw)
        return [lap("jordan-x", 97.0, day=d), lap("sam-y", 99.0, day=d), lap("tyler-q", 160.0, day=d),
                lap("swazi", 11.0, day=d)]
    leaderboards.refresh(FakeApi(laps), cfg, sched, mine(sched), log=quiet)
    assert board_of(cfg, sched) == [("jordan-x", 97.0), ("sam-y", 99.0)], board_of(cfg, sched)
    refs = leaderboards.library_refs(__import__("debrief.team", fromlist=["x"]).library_combos(cfg["library_folder"]),
                                     sched["series"][0], sched["series"][0]["weeks"][0], 51, [67], {}, False)
    assert refs == {None: 98.2}, refs


def test_m1_still_keeps_a_much_quicker_teammate():
    """The floor is 0.6 x, as before: a teammate 25% quicker than the rest is a real lap, not a partial."""
    a, b = datetime(2026, 10, 28).date(), datetime(2026, 11, 4).date()
    laps = [lap("swazi", 100.0), lap("jordan-x", 75.0), lap("sam-y", 101.0)]
    got = [r["key"] for r in leaderboards.build_board(laps, "swazi", "Swazi", {}, a, b, False)]
    assert got == ["jordan-x", "me", "sam-y"], got


# ================================================================ L1 / L2: the Discord message text
class Resp:
    status_code = 200
    headers = {}

    def json(self):
        return {"id": "1300000000000000001"}


class FakeSession:
    """Records the post; no network code."""

    def __init__(self):
        self.calls = []

    def post(self, url, **kw):
        self.calls.append((url, kw))
        return Resp()


HOOK = "https://discord.com/api/webhooks/123456789012341234/" + "T" * 68


def png(w=1200, h=630):
    def chunk(k, d):
        return struct.pack(">I", len(d)) + k + d + struct.pack(">I", zlib.crc32(k + d) & 0xffffffff)
    raw = b"".join(b"\x00" + b"\x00" * w * 3 for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def sent(content):
    s = FakeSession()
    discord.post(HOOK, png(), content, session=s)
    assert len(s.calls) == 1
    payload = json.loads(s.calls[0][1]["data"]["payload_json"])
    assert payload["allowed_mentions"] == {"parse": []}
    return payload["content"]


def test_l1_nested_mass_mentions_never_survive():
    tricks = ["@@everyone hey @here", "@every@everyoneone", "@@@@here", "@ever@@everyoneyone",
              "@her@@hereе", "@@EVERYONE", "@He@heRere", "@\x00everyone", "@\x1beveryone", "@\u0085here",
              "@\reveryone", "@e\x00veryone", "@@@every@@everyone@one@@here"]
    for t in tricks:
        out = discord.clean_content(t)
        assert not MASS.search(out), (t, out)
        assert not MASS.search(sent(t)), t
    assert discord.clean_content("@@everyone hey @here") == "everyone hey here"
    assert discord.clean_content("me@example.com <@123> @Jordan") == "me@example.com <@123> @Jordan"


def test_l1_fuzz_until_stable():
    rnd = random.Random(1)
    parts = ["@", "@@", "every", "one", "everyone", "here", "he", "re", "x", " ", "\x00", "\x07", "E", "\n"]
    for _ in range(3000):
        s = "".join(rnd.choice(parts) for _ in range(rnd.randint(1, 12)))
        out = discord.clean_content(s)
        assert not MASS.search(out), (s, out)
        assert discord.clean_content(out) == out, (s, out)        # already stable


def test_l2_control_characters_are_dropped_but_line_breaks_kept():
    s = "PB\x00 at\x07 the\x1b[31m Roval\x7f\x85\x9b!\nLap 3\r\nLap\t4\rdone​"
    out = sent(s)
    assert out == "PB at the[31m Roval!\nLap 3\nLap 4\ndone​", repr(out)
    assert not re.search(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]", out)
    assert sent("line one\n\nline two") == "line one\n\nline two"


# ================================================================ L3: back-fill budget and the estimate
def all_partial(kw):
    d = in_week(kw)
    return [lap("jordan-x", 11.0, day=d), lap("swazi", 98.9, day=d), lap("sam-y", 99.5, day=d)]


def test_l3_second_refresh_within_ten_minutes_spends_nothing_on_past_weeks():
    at(NOW)
    cfg = lib()
    sched = make_sched([MX5(), make_series("Production Car Challenge", ["Global Mazda MX-5 Cup"], track_id=18)])
    p = leaderboards.plan(cfg, FakeApi(all_partial), sched, mine(sched))
    api = FakeApi(all_partial)
    r1 = leaderboards.refresh(api, cfg, sched, mine(sched), log=quiet)
    assert r1["requests"] == 10 and p["est_max"] == 10, (r1["requests"], p)       # 2 current + 3 past, 2 each
    for minutes in (1, 9):
        at(NOW + timedelta(minutes=minutes))
        assert leaderboards.plan(cfg, FakeApi(all_partial), sched, mine(sched))["est_total"] == 0
        api2 = FakeApi(all_partial)
        leaderboards.refresh(api2, cfg, sched, mine(sched), log=quiet)
        assert api2.requests_made == 0, api2.calls
    at(NOW + timedelta(minutes=11))                          # the window is over: the next weeks back are filled
    api3 = FakeApi(all_partial)
    leaderboards.refresh(api3, cfg, sched, mine(sched), log=quiet)
    past = [c for c in api3.laps() if c[1]["after"] < "2026-10-28"]
    assert past and len({c[1]["after"] + str(c[1]["tracks"]) for c in past}) == 3, past


def test_l3_a_failed_past_week_is_not_retried_in_the_window_even_with_force_old():
    at(NOW)
    cfg, sched = lib(), make_sched([MX5(first_week=6)])
    api = FakeApi(all_partial, fail_after="2026-10-28")      # week 6 (past) fails, week 7 works
    try:
        leaderboards.refresh(api, cfg, sched, mine(sched), log=quiet)
    except RuntimeError:
        pass
    assert any(c[1]["after"].startswith("2026-10-21") for c in api.laps())
    at(NOW + timedelta(minutes=3))
    api2 = FakeApi(all_partial)
    leaderboards.refresh(api2, cfg, sched, mine(sched), force_old=True, log=quiet)
    assert not [c for c in api2.laps() if c[1]["after"].startswith("2026-10-21")], api2.calls
    at(NOW + timedelta(minutes=12))
    api3 = FakeApi(all_partial)
    leaderboards.refresh(api3, cfg, sched, mine(sched), log=quiet)
    assert [c for c in api3.laps() if c[1]["after"].startswith("2026-10-21")], api3.calls


def test_l3_estimate_counts_past_week_fills_and_the_worst_case():
    at(NOW)
    cfg = lib()
    sched = make_sched([MX5(first_week=4)])                  # weeks 4, 5, 6 are past
    p = leaderboards.plan(cfg, FakeApi(all_partial), sched, mine(sched))
    assert p["est_past"] == 3 and p["est_total"] == 4 and p["est_max"] == 8 and p["est_me"] == 0, p
    api = FakeApi(all_partial)
    leaderboards.refresh(api, cfg, sched, mine(sched), log=quiet)
    assert p["est_total"] <= api.requests_made <= p["est_max"], (api.requests_made, p)


def test_l3_me_request_is_counted_in_the_estimate():
    at(NOW)
    cfg, sched = lib(), make_sched([MX5(first_week=7)])

    def clean(kw):
        d = in_week(kw)
        return [lap("jordan-x", 98.4, day=d), lap("swazi", 98.9, day=d)]
    p = leaderboards.plan(cfg, FakeApi(clean, me_saved=False), sched, mine(sched))
    assert p["est_me"] == 1 and p["est_total"] == 2 and p["est_max"] == 3, p
    api = FakeApi(clean, me_saved=False)
    leaderboards.refresh(api, cfg, sched, mine(sched), log=quiet)
    assert api.requests_made == p["est_total"] == 2 and api.calls[0][0] == "me", api.calls
    p2 = leaderboards.plan(cfg, FakeApi(clean, me_saved=True), make_sched([MX5(first_week=7)]), mine(sched))
    assert p2["est_me"] == 0
    at(NOW + timedelta(minutes=2))                           # nothing to fetch: no me() either
    assert leaderboards.plan(cfg, FakeApi(clean, me_saved=False), sched, mine(sched))["est_total"] == 0


def test_l3_window_shows_the_past_weeks_and_the_ceiling():
    js = (ROOT / "debrief" / "desktop" / "ui" / "app.js").read_text(encoding="utf-8")
    fn = js[js.index("function weeklyText("):js.index("function renderWeekly(")]
    for k in ("est_past", "est_me", "est_max", "for earlier weeks", "At most "):
        assert k in fn, k


# ================================================================ L4: correlational fallback text
def test_l4_review_fallback_text_is_correlational():
    html = (ROOT / "debrief" / "templates" / "review.html").read_text(encoding="utf-8")
    i = html.index("’s best lap was ")
    line = html[html.rfind("\n", 0, i):html.index("\n", i)]
    assert "Both are best laps: this compares your best driving" in line, line
    text = "".join(re.findall(r"'([^']*)'", line))
    assert not re.search(r"\b(so|because|causes?|leads? to)\b", text, re.I), text
    team = (ROOT / "debrief" / "templates" / "team.html").read_text(encoding="utf-8")
    basis = re.search(r'id="learnBasis">([^<]*)<', team).group(1)
    assert not re.search(r"\b(so|because|causes?|leads? to)\b", basis, re.I), basis


# ================================================================ L5: SIGNING.md quotes the real messages
def test_l5_signing_guide_quotes_the_updater_messages_exactly():
    md = (ROOT / "packaging" / "SIGNING.md").read_text(encoding="utf-8")
    sec = md[md.index("## 6."):md.index("## 7.")]
    quotes = [ln[2:].strip() for ln in sec.splitlines() if ln.startswith("> ")]
    assert updater.NOT_SIGNED in quotes and updater.BAD_SIGNATURE in quotes, quotes
    assert sec.index(updater.NOT_SIGNED) < sec.index(updater.BAD_SIGNATURE)
    before_bad = sec[:sec.index(updater.BAD_SIGNATURE)]
    between = " ".join(before_bad[before_bad.index(updater.NOT_SIGNED):].split())
    assert "another version" in between and "another key" in between, between      # listed under BAD_SIGNATURE
    src = (ROOT / "debrief" / "updater.py").read_text(encoding="utf-8")
    m = re.search(r'"(This release\'s installer doesn\'t match its version[^"]*)"', src)
    assert m and m.group(1) in sec


# ================================================================ L6: release job supply chain
def _steps(yml):
    """(name, block) for every step, from the text (no YAML library needed)."""
    out = []
    for m in re.finditer(r"^      - (?:name: (.*)|uses: .*)$", yml, re.M):
        end = yml.find("\n      - ", m.end())
        out.append((m.group(1) or "", yml[m.start():end if end > 0 else len(yml)]))
    return out


def test_l6_signing_library_installed_with_hashes():
    yml = (ROOT / ".github" / "workflows" / "build.yml").read_text(encoding="utf-8")
    rel = yml[yml.index("\n  release:"):]
    inst = [b for n, b in _steps(rel) if "pip install" in b]
    assert len(inst) == 1 and "--require-hashes" in inst[0] and "-r packaging/sign-requirements.txt" in inst[0], inst
    assert "--only-binary" in inst[0]
    reqs = (ROOT / "packaging" / "sign-requirements.txt").read_text(encoding="utf-8")
    body = re.sub(r"(?m)^#.*\n", "", reqs).replace("\\\n", " ")
    entries = [ln.split() for ln in body.splitlines() if ln.strip()]
    names = {}
    for e in entries:
        m = re.fullmatch(r"([A-Za-z0-9_.-]+)==(\d+(?:\.\d+)+)", e[0])
        assert m, e[0]
        hashes = [x for x in e[1:] if x.startswith("--hash=")]
        assert hashes and len(hashes) == len(e) - 1, e
        assert all(re.fullmatch(r"--hash=sha256:[0-9a-f]{64}", h) for h in hashes), e
        names[m.group(1).lower()] = m.group(2)
    assert names.get("cryptography") and {"cffi", "pycparser"} <= set(names), names   # --require-hashes needs deps too
    spec = (ROOT / "packaging" / "debrief.spec").read_text(encoding="utf-8")
    assert '"cryptography"' in spec[spec.index("excludes"):]                     # still never bundled


def test_l6_actions_pinned_by_full_sha_with_the_tag_noted():
    yml = (ROOT / ".github" / "workflows" / "build.yml").read_text(encoding="utf-8")
    uses = re.findall(r"^\s*(?:- )?uses: (\S+)(.*)$", yml, re.M)
    assert uses
    for ref, rest in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", ref), ref
        assert re.fullmatch(r"\s+# v\d+(\.\d+)*", rest), (ref, rest)
    got = {r.split("@")[0] for r, _ in uses}
    assert got == {"actions/checkout", "actions/setup-python", "actions/upload-artifact",
                   "actions/download-artifact"}, got


def test_ci_runs_this_file_after_the_shell_tests():
    yml = (ROOT / ".github" / "workflows" / "build.yml").read_text(encoding="utf-8")
    steps = _steps(yml)
    names = [n for n, _ in steps]
    i = names.index("Test shell (2.5)")
    assert "python tests/ci_run.py tests/test_25_fixes.py" in steps[i + 1][1], steps[i + 1]


# ================================================================ L7: Post isn't live the moment the dialog opens
UI = ROOT / "debrief" / "desktop" / "ui"


def _open_discord_src():
    js = (UI / "app.js").read_text(encoding="utf-8")
    a = js.index("const POST_ARM_MS=")
    b = js.index("$('discordText').addEventListener('input'", a)
    return js, js[a:b]


def test_l7_static_post_starts_disabled_and_the_handler_checks_it():
    js, src = _open_discord_src()
    ms = int(re.search(r"const POST_ARM_MS=(\d+);", src).group(1))
    assert 500 <= ms <= 1000, ms
    assert "post.disabled=true" in src and "discordPost').disabled=false" not in src
    assert src.index("post.disabled=true") < src.index("showModal()")
    h = js.index("$('discordPost').addEventListener('click'")
    assert "$('discordPost').disabled)return" in js[h:h + 200]


HARNESS = """<!doctype html><meta charset="utf-8"><body>
<dialog id="dlgDiscord"><img id="discordImg"><textarea id="discordText"></textarea><span id="discordCount"></span>
<span id="discordTarget"></span><span id="discordDlgMsg"></span><button id="discordPost">Post</button></dialog>
<script>
const $=id=>document.getElementById(id);
function setMsg(id,t){$(id).textContent=t||''}
function discordCount(){}
let staged=null,discordHint=null;
%s
window.openDiscord=openDiscord;window.getStaged=()=>staged;
</script>"""


def _data_png():
    import base64
    return "data:image/png;base64," + base64.b64encode(png(40, 20)).decode()


def test_l7_browser_post_arms_after_600ms_and_after_the_preview_loads():
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # noqa: BLE001
        print("  SKIPPED: Playwright not available")
        return
    _, src = _open_discord_src()
    try:
        pw = sync_playwright().start()
    except Exception:  # noqa: BLE001
        print("  SKIPPED: Playwright not available")
        return
    try:
        try:
            b = pw.chromium.launch()
        except Exception:  # noqa: BLE001
            print("  SKIPPED: Chromium not available")
            return
        pg = b.new_page()
        held = []
        pg.route("http://debrief.invalid/**", lambda route: held.append(route))   # never reaches a network
        pg.set_content(HARNESS % src)
        dis = "() => document.getElementById('discordPost').disabled"
        # 1. a ready image: off at once and at ~300 ms, on after ~600 ms
        pg.evaluate("u => openDiscord({nonce:'n1',target:{}}, u, 'hi')", _data_png())
        assert pg.evaluate(dis) is True
        pg.wait_for_timeout(300)
        assert pg.evaluate(dis) is True, "Post was live 300 ms after the dialog opened"
        pg.wait_for_function("() => !document.getElementById('discordPost').disabled", timeout=3000)
        # a double-click right as the dialog appears lands on a disabled button
        pg.evaluate("() => document.getElementById('dlgDiscord').close()")
        pg.evaluate("() => { staged = {nonce:'n2'}; }")
        pg.evaluate("u => { window.clicks = 0; document.getElementById('discordPost').addEventListener('click', () => window.clicks++);"
                    " openDiscord({nonce:'n2',target:{}}, u, 'hi'); }", _data_png())
        pg.dblclick("#discordPost", force=True)
        assert pg.evaluate("() => window.clicks") == 0
        # 2. a preview that hasn't loaded keeps Post off past 600 ms, and arms once it loads
        pg.evaluate("() => document.getElementById('dlgDiscord').close()")
        pg.evaluate("() => { staged = {nonce:'n3'}; openDiscord({nonce:'n3',target:{}}, 'http://debrief.invalid/card.png', ''); }")
        pg.wait_for_timeout(900)
        assert pg.evaluate(dis) is True, "Post was live before the preview loaded"
        assert held, "the slow preview request wasn't held"
        held[0].fulfill(status=200, content_type="image/png", body=png(40, 20))
        pg.wait_for_function("() => !document.getElementById('discordPost').disabled", timeout=3000)
        # 3. a preview that can't be shown never arms Post
        pg.evaluate("() => document.getElementById('dlgDiscord').close()")
        pg.evaluate("() => { staged = {nonce:'n4'}; openDiscord({nonce:'n4',target:{}}, 'data:image/png;base64,AAAA', ''); }")
        pg.wait_for_timeout(900)
        assert pg.evaluate(dis) is True
        assert "can't be posted" in pg.evaluate("() => document.getElementById('discordDlgMsg').textContent")
        b.close()
    finally:
        pw.stop()


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except Exception as e:  # noqa: BLE001
                failed += 1
                import traceback
                traceback.print_exc()
                print(f"FAIL {name}: {e}")
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"{'FAILED' if failed else 'passed'}: {failed} failure(s)")
    sys.exit(1 if failed else 0)
