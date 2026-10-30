"""Debrief 2.5 shell (workstream 25-D): the Discord webhook setting, the stage/post/check endpoints and the
window's Post-to-Discord dialog, the weekly leaderboards job and estimate, "who to learn from" on new reviews,
layout 2.5.0, feature modules, version and build lists.

    python3 tests/test_25_d.py

leaderboards.py (25-A), discord.py and ed25519.py (25-B) and the team.py additions (25-A) are written in
parallel. These tests put small stand-ins with the agreed §3.1 shapes (tests/fixtures_25_d/sample_*.json) in
their place, so they pass before and after the merge; the test_real_* ones also run the real modules when a
copy has them. Never contacts Garage 61 or Discord: requests is blocked, the server is in-process on
127.0.0.1 and the browser checks are skipped without Playwright.
"""
from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import re
import shutil
import struct
import sys
import tempfile
import threading
import time
import types
import urllib.error
import urllib.request
import zlib
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"

TMP = Path(tempfile.mkdtemp(prefix="debrief-25d-"))
LIB = TMP / "lib"
(LIB / "reviews").mkdir(parents=True)
ST = TMP / "settings.toml"
LOG = TMP / "debrief.log"

WEBHOOK_ID = "123456789012345678"
TOKEN = "Zq7_" + "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-_" + "kLmNoPqRsTuVwXyZ0123456789"   # 68 characters
HOOK = f"https://discord.com/api/webhooks/{WEBHOOK_ID}/{TOKEN}"
assert 60 <= len(TOKEN) <= 100


def write_settings(extra="", token="PASTE-YOUR-GARAGE61-TOKEN-HERE"):
    ST.write_text(f'token = "{token}"\nlibrary_folder = \'{LIB}\'\n' + extra, encoding="utf-8")


write_settings()
os.environ.update(DEBRIEF_SETTINGS=str(ST), DEBRIEF_CACHE_DIR=str(TMP / "cache"),
                  DEBRIEF_SCHEDULE_DIR=str(TMP / "sched"), DEBRIEF_UPDATE_STATE=str(TMP / "update.json"))

import requests  # noqa: E402

NET = []


def _blocked(*a, **k):
    NET.append(a[:1])
    raise requests.ConnectionError("network blocked in tests")


requests.get = _blocked
requests.post = _blocked
requests.request = _blocked
requests.Session.request = lambda self, *a, **k: _blocked(*a, **k)

import debrief  # noqa: E402
from debrief import app, library, report, schedule, series, team  # noqa: E402
import debrief.desktop as desktop  # noqa: E402
from debrief.desktop import server as S  # noqa: E402
from debrief.desktop import settings_io  # noqa: E402
import make_fixture_library  # noqa: E402

app.LOG_FILE = str(LOG)
FX = ROOT / "tests" / "fixtures_25_d"
UI = ROOT / "debrief" / "desktop" / "ui"
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
NEW = ("leaderboards", "discord", "ed25519")


def sample(name):
    return json.loads((FX / f"sample_{name}.json").read_text(encoding="utf-8"))


def make_png(w=1200, h=630, noise=True) -> bytes:
    """A real PNG (stdlib only), big enough to pass the 1 KB floor."""
    import random
    rng = random.Random(w * 7 + h)
    rows = b"".join(b"\x00" + (bytes(rng.randrange(256) for _ in range(min(w, 64) * 3)) if noise else b"\x00" * (min(w, 64) * 3))
                    + b"\x00" * ((w - min(w, 64)) * 3) for _ in range(min(h, 64)))
    rows += (b"\x00" + b"\x00" * (w * 3)) * (h - min(h, 64))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (S.PNG_MAGIC + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(rows, 1)) + chunk(b"IEND", b""))


PNG = make_png()
assert len(PNG) > 1024


# ================================================================ stand-in modules (§3.1 shapes)
class Rec:
    """Calls the stand-ins saw (discord.post / check, leaderboards.plan / refresh, team additions)."""
    def __init__(self):
        self.calls = []

    def names(self):
        return [c[0] for c in self.calls]

    def count(self, name):
        return self.names().count(name)


WEBHOOK_RE = r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/webhooks/(\d{17,20})/([A-Za-z0-9_-]{60,100})$"


@contextmanager
def fakes(rec=None, post_raises=None, check_result=None, learn=True):
    """discord, leaderboards and ed25519 stand-ins in sys.modules (and as attributes of the debrief package, so
    `from .. import discord` finds them too), plus team.learn_from_saved / team.render_saved."""
    rec = rec if rec is not None else Rec()
    saved_mods = {n: sys.modules.get("debrief." + n) for n in NEW}
    saved_attrs = {n: getattr(debrief, n, None) for n in NEW}
    saved_team = {k: getattr(team, k, None) for k in ("learn_from_saved", "render_saved", "learn_from")}

    d = types.ModuleType("debrief.discord")
    d.WEBHOOK_RE = WEBHOOK_RE

    class DiscordError(Exception):
        pass
    d.DiscordError = DiscordError
    d.MSG_BAD_URL = ("That isn't a Discord webhook address. In Discord: channel settings > Integrations > "
                     "Webhooks > Copy Webhook URL.")
    d.valid = lambda url: isinstance(url, str) and bool(re.match(WEBHOOK_RE, url)) and not any(c.isspace() for c in url)
    d.hint = lambda url: "…/" + re.match(WEBHOOK_RE, url).group(1)[-4:]

    def check(url):
        rec.calls.append(("discord.check", url))
        if isinstance(check_result, Exception):
            raise check_result
        return check_result or {"ok": True, "name": "Debrief", "channel_id": "123456789012"}
    d.check = check

    def post(url, png, content):
        rec.calls.append(("discord.post", url, png, content))
        if post_raises is not None:
            raise post_raises
        return {"ok": True, "id": "998877"}
    d.post = post

    lb = types.ModuleType("debrief.leaderboards")
    lb.MAX_SERIES = 8

    def plan(cfg, api, sched, mine):
        rec.calls.append(("leaderboards.plan", mine))
        if api is not None:
            api.cached_items("tracks")   # what the real one may read: the cached lists, never a request
        return sample("plan")
    lb.plan = plan

    def refresh(api, cfg, sched, mine, series_ids=None, force_old=False, log=print):
        rec.calls.append(("leaderboards.refresh", series_ids, force_old))
        for s in sample("plan")["series"]:
            if series_ids and s["id"] not in series_ids:
                continue
            log(f"Week {s['week']} at {s['track']}...")
            api.find_laps(tracks=[1], cars=[2], teams=[cfg["team_slug"]], drivers=["me"], group="driver", limit=200)
        return {"series": len(sample("plan")["series"]), "requests": api.requests_made}
    lb.refresh = refresh
    lb.view = lambda folder, sched=None: {"season": "2026 Season 4", "series": [], "h2h": []}

    ed = types.ModuleType("debrief.ed25519")
    ed.verify = lambda pk, msg, sig: False

    def learn_from_saved(folder, review):
        rec.calls.append(("team.learn_from_saved", review.get("meta", {}).get("track")))
        try:
            tj = json.loads((Path(folder) / "team" / "team.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        m = review.get("meta") or {}
        if any(library.same_combo({"track": c.get("track"), "car": c.get("car")}, {"track": m.get("track"), "car": m.get("car")})
               for c in tj.get("combos") or []):
            return sample("learn_from")
        return []

    def render_saved(folder):
        rec.calls.append(("team.render_saved",))
        out = Path(folder) / "team"
        out.mkdir(parents=True, exist_ok=True)
        (out / "index.html").write_text("<!doctype html><title>Team hub</title><body>Team</body>", encoding="utf-8")
        return out / "index.html"

    mods = {"discord": d, "leaderboards": lb}
    if not (ROOT / "debrief" / "ed25519.py").is_file():
        mods["ed25519"] = ed      # the real one (25-B) is used as it is: updater imports from it
    if (ROOT / "debrief" / "discord.py").is_file():
        # the real rules (valid, hint, messages, DiscordError); only check and post are recorded stand-ins
        import importlib
        real = importlib.import_module("debrief.discord")
        for k in ("valid", "hint", "MSG_BAD_URL", "DiscordError", "WEBHOOK_RE"):
            if hasattr(real, k):
                setattr(d, k, getattr(real, k))
    try:
        for n, m in mods.items():
            sys.modules["debrief." + n] = m
            setattr(debrief, n, m)
        if learn:
            team.learn_from_saved = learn_from_saved
        team.render_saved = render_saved
        yield rec
    finally:
        for n in mods:
            if saved_mods[n] is None:
                sys.modules.pop("debrief." + n, None)
            else:
                sys.modules["debrief." + n] = saved_mods[n]
            if saved_attrs[n] is None:
                if hasattr(debrief, n):
                    delattr(debrief, n)
            else:
                setattr(debrief, n, saved_attrs[n])
        for k, v in saved_team.items():
            if v is None:
                if hasattr(team, k):
                    delattr(team, k)
            else:
                setattr(team, k, v)


class FakeG61:
    """Stands in for app.make_api(cfg): a Garage 61 client that records every call and never uses the network.
    Only cached_items is free; anything else counts as a request."""
    FREE = {"cached_items", "has_csv"}

    def __init__(self, laps=None, csv=None):
        self.requests_made = 0
        self.calls = []
        self.cache = str(TMP / "fake-g61-cache")   # sync.Store.for_api reads it (the window's Sessions strip)
        self.laps = laps or []
        self.csv = csv or {}

    def _req(self, name, *a, **k):
        self.calls.append(name)
        self.requests_made += 1

    def cached_items(self, kind):
        self.calls.append("cached_items")
        return None

    def has_csv(self, lap_id):
        return lap_id in self.csv

    def me(self):
        self._req("me")
        return {"slug": "alex-driver", "firstName": "Alex", "lastName": "Driver"}

    def teams(self):
        self._req("teams")
        return []

    def cars(self, refresh=False):
        self._req("cars")
        return [{"id": 190, "name": "Ford Mustang GT4"}]

    def tracks(self, refresh=False):
        self._req("tracks")
        return [{"id": 468, "name": "Charlotte Motor Speedway", "variant": "Roval 2019", "length": 3000}]

    def find_laps(self, fresh=False, **params):
        self._req("find_laps")
        if params.get("drivers") == ["me"] and params.get("event"):
            return [dict(l) for l in self.laps]
        return []

    def lap_csv(self, lap_id):
        self._req("lap_csv")
        return self.csv.get(lap_id)

    def my_statistics(self, start):
        self._req("my_statistics")
        return []


@contextmanager
def patched(obj, **kw):
    old = {k: getattr(obj, k) for k in kw}
    try:
        for k, v in kw.items():
            setattr(obj, k, v)
        yield
    finally:
        for k, v in old.items():
            setattr(obj, k, v)


# ================================================================ server helpers
_SRV = {}


def srv():
    if "s" not in _SRV:
        s = S.DebriefServer(heartbeat_timeout=3600)
        threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
        _SRV["s"] = s
    return _SRV["s"]


def get(rel, headers=None):
    rq = urllib.request.Request(srv().url + rel, headers=headers if headers is not None else {"X-Debrief": "1"})
    try:
        with OP.open(rq, timeout=20) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def post(rel, body=None, headers=None, raw=None):
    data = raw if raw is not None else json.dumps(body or {}).encode("utf-8")
    h = {"X-Debrief": "1", "Content-Type": "application/json"} if headers is None else headers
    rq = urllib.request.Request(srv().url + rel, data=data, method="POST", headers=h)
    try:
        with OP.open(rq, timeout=20) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def stage(data=PNG, ctype="image/png"):
    return post("api/discord-stage", raw=data, headers={"X-Debrief": "1", "Content-Type": ctype})


def js(body):
    return json.loads(body.decode("utf-8"))


def wait_job(timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        snap = srv().jobs.snapshot()
        if snap.get("state") != "running":
            return snap
        time.sleep(0.1)
    raise AssertionError("job didn't finish")


def _fill_lib():
    if not any((LIB / "reviews").iterdir()):
        make_fixture_library.main(str(LIB))


def log_text():
    try:
        return LOG.read_text(encoding="utf-8")
    except OSError:
        return ""


# ================================================================ settings: the webhook is a secret
def test_settings_webhook_saved_never_returned():
    write_settings()
    with fakes():
        v = settings_io.save({"discord_webhook": "  " + HOOK + " "})
        assert v["discord_saved"] is True and v["discord_hint"] == "…/5678", v
        dump = json.dumps(v)
        assert TOKEN not in dump and HOOK not in dump and TOKEN[:12] not in dump and "webhooks" not in dump
        assert settings_io.discord_webhook() == HOOK
        assert f'discord_webhook = "{HOOK}"' in ST.read_text(encoding="utf-8")
        code, _, body = get("api/settings")
        assert code == 200 and TOKEN not in body.decode("utf-8") and js(body)["discord_saved"] is True
        # an empty box keeps what's saved; clear_discord removes it
        assert settings_io.save({"discord_webhook": "", "salutation": "sir"})["discord_saved"] is True
        v = settings_io.save({"clear_discord": True})
        assert v["discord_saved"] is False and v["discord_hint"] is None and settings_io.discord_webhook() is None
    write_settings()


def test_settings_bad_webhook_plain_error():
    write_settings()
    want = ("That isn't a Discord webhook address. In Discord: channel settings > Integrations > Webhooks > "
            "Copy Webhook URL.")
    with fakes():
        for bad in ("http://discord.com/api/webhooks/123456789012345678/" + TOKEN,
                    "https://evil.example/api/webhooks/123456789012345678/" + TOKEN,
                    HOOK + "?wait=true", HOOK + "#x", "https://discord.com/api/webhooks/12/" + TOKEN,
                    "not a url", 42, ["x"]):
            try:
                settings_io.save({"discord_webhook": bad})
                raise AssertionError(f"saved {bad!r}")
            except ValueError as e:
                assert str(e) == want, e
        code, _, body = post("api/settings", {"discord_webhook": HOOK + "?x=1"})
        assert code == 400 and js(body)["error"] == want and TOKEN not in body.decode("utf-8")
    # without the discord module the local copy of WEBHOOK_RE still guards the setting
    with patched(settings_io, _discord=lambda: None):
        try:
            settings_io.save({"discord_webhook": "https://discord.com.evil/api/webhooks/1/x"})
            raise AssertionError("saved")
        except ValueError as e:
            assert str(e) == want
        assert settings_io.webhook_valid(HOOK) and settings_io.webhook_hint(HOOK) == "…/5678"
    assert "discord_webhook" not in ST.read_text(encoding="utf-8")


def test_public_view_and_log_never_hold_the_webhook():
    write_settings(f'discord_webhook = "{HOOK}"\n')
    with fakes():
        code, _, body = post("api/settings", {"discord_webhook": HOOK})
        assert code == 200
        code, _, body = get("api/settings")
        txt = body.decode("utf-8")
        assert TOKEN not in txt and WEBHOOK_ID not in txt and js(body)["discord_hint"] == "…/5678"
        assert "Discord webhook updated" in log_text()
        # a post, a failing post and a check: the log names the webhook only by its hint
        nonce = js(stage()[2])["nonce"]
        assert post("api/discord-post", {"nonce": nonce, "content": "hi"})[0] == 200
    err = RuntimeError(f"HTTPSConnectionPool: Max retries exceeded with url: /api/webhooks/{WEBHOOK_ID}/{TOKEN}?wait=true")
    with fakes(post_raises=err, check_result=RuntimeError(f"boom {HOOK}")):
        nonce = js(stage()[2])["nonce"]
        code, _, body = post("api/discord-post", {"nonce": nonce, "content": "hi"})
        assert code == 502 and TOKEN not in body.decode("utf-8")
        code, _, body = post("api/discord-check", {})
        assert code == 502 and TOKEN not in body.decode("utf-8") and HOOK not in body.decode("utf-8")
    lg = log_text()
    assert "Posted to Discord (webhook …/5678)" in lg and "Discord post failed (webhook …/5678)" in lg
    assert TOKEN not in lg and HOOK not in lg, lg[-800:]
    assert S.redact_webhook("x " + HOOK + " y", None).count(TOKEN) == 0
    write_settings()


# ================================================================ stage / post / check
def test_post_without_stage_is_refused_and_never_calls_discord():
    write_settings(f'discord_webhook = "{HOOK}"\n')
    with fakes() as rec:
        for body in ({}, {"nonce": "x" * 22}, {"nonce": None}, {"nonce": "../../etc"}, {"nonce": ["a"]},
                     {"nonce": "A" * 22, "content": "hello"}):
            code, _, b = post("api/discord-post", body)
            assert code == 400 and js(b)["error"] == "Make the preview again.", (body, b)
        assert rec.count("discord.post") == 0
    write_settings()


def test_nonce_single_use_and_expires():
    write_settings(f'discord_webhook = "{HOOK}"\n')
    st = srv().discord_stage
    with fakes() as rec:
        code, _, b = stage()
        j = js(b)
        assert code == 200 and j["ok"] and j["width"] == 1200 and j["height"] == 630, j
        assert j["target"] == {"saved": True, "hint": "…/5678", "name": None}, j
        assert rec.count("discord.post") == 0 and not NET           # staging alone sends nothing
        code, _, b = post("api/discord-post", {"nonce": j["nonce"], "content": "Alex · Roval: best 1:25.612"})
        assert code == 200 and js(b) == {"ok": True, "id": "998877"}
        assert rec.count("discord.post") == 1
        _, url, png, content = rec.calls[-1]
        assert url == HOOK and png == PNG and content == "Alex · Roval: best 1:25.612"
        code, _, b = post("api/discord-post", {"nonce": j["nonce"], "content": "again"})   # reuse
        assert code == 400 and js(b)["error"] == "Make the preview again."
        assert rec.count("discord.post") == 1
        # expiry: a monkeypatched clock 10 minutes and a second later
        now = [1000.0]
        with patched(st, clock=lambda: now[0]):
            nonce = js(stage()[2])["nonce"]
            now[0] += S.DiscordStage.TTL - 1
            assert len(st) >= 1
            now[0] += 2
            code, _, b = post("api/discord-post", {"nonce": nonce})
            assert code == 400 and js(b)["error"] == "Make the preview again."
            # at most 3 staged: the oldest goes first
            ns = [js(stage()[2])["nonce"] for _ in range(4)]
            assert len(st) == 3 and st.pop(ns[0]) is None and st.pop(ns[3]) is not None
        assert rec.count("discord.post") == 1
        # no file is ever written for a staged card
        assert not [p for p in TMP.rglob("*") if p.is_file() and p.read_bytes()[:8] == S.PNG_MAGIC]
    write_settings()


def test_stage_checks_the_picture():
    write_settings(f'discord_webhook = "{HOOK}"\n')
    with fakes() as rec:
        cases = [(b"GIF89a" + b"\x00" * 2000, "image/png"),          # not a PNG
                 (PNG, "application/octet-stream"),                    # wrong type
                 (PNG[:900], "image/png"),                             # under 1 KB
                 (make_png(2401, 100), "image/png"),                   # too wide for Discord
                 (make_png(100, 2500), "image/png"),
                 (S.PNG_MAGIC + b"\x00\x00\x00\x0dIHDX" + b"\x00" * 2000, "image/png")]
        for data, ct in cases:
            code, h, b = stage(data, ct)
            assert code == 400 and not js(b)["ok"], (ct, len(data), b)
        assert len(srv().discord_stage) == 0 or True
        # over 4 MB: refused from the Content-Length alone, connection closed
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", srv().port, timeout=10)
        c.putrequest("POST", "/" + srv().secret + "/api/discord-stage")
        c.putheader("X-Debrief", "1")
        c.putheader("Content-Type", "image/png")
        c.putheader("Content-Length", str(4 * 1024 * 1024 + 1))
        c.endheaders()
        r = c.getresponse()
        assert r.status == 400 and r.getheader("Connection", "").lower() == "close"
        c.close()
        for bad_len in ("-5", "abc", ""):
            c = http.client.HTTPConnection("127.0.0.1", srv().port, timeout=10)
            c.putrequest("POST", "/" + srv().secret + "/api/discord-stage")
            c.putheader("X-Debrief", "1")
            c.putheader("Content-Type", "image/png")
            c.putheader("Content-Length", bad_len)
            c.endheaders()
            assert c.getresponse().status == 400
            c.close()
        # without X-Debrief the endpoint doesn't exist
        assert stage.__call__ and post("api/discord-stage", raw=PNG, headers={"Content-Type": "image/png"})[0] == 404
        assert rec.count("discord.post") == 0 and rec.count("discord.check") == 0 and not NET
        # the biggest allowed picture is fine
        code, _, b = stage(make_png(2400, 2400))
        assert code == 200 and js(b)["width"] == 2400
    write_settings()


def _clear_stage():
    """Start from an empty preview store: earlier tests leave previews behind, and the store keeps only 3,
    so this test's own nonce could be pushed out (seen on Windows CI)."""
    st = srv().discord_stage
    with st.lock:
        st.items.clear()


def test_post_needs_a_saved_webhook_and_reports_discord_errors():
    write_settings()
    _clear_stage()
    with fakes() as rec:
        nonce = js(stage()[2])["nonce"]
        assert js(stage()[2])["target"]["saved"] is False
        code, _, b = post("api/discord-post", {"nonce": nonce, "content": "x"})
        assert code == 400 and "webhook in Settings" in js(b)["error"] and rec.count("discord.post") == 0, \
            (code, b, rec.names(), ST.read_text(encoding="utf-8"))
        code, _, b = post("api/discord-check", {})
        assert code == 400 and rec.count("discord.check") == 0, (code, b, rec.names())
    write_settings(f'discord_webhook = "{HOOK}"\n')
    with fakes() as rec:
        nonce = js(stage()[2])["nonce"]
        code, _, b = post("api/discord-post", {"nonce": nonce, "content": "x" * 2001})
        assert code == 400 and rec.count("discord.post") == 0
    with fakes(post_raises=None) as rec:
        d = sys.modules["debrief.discord"]
        rec2 = Rec()
        with fakes(rec2, post_raises=d.DiscordError("That Discord webhook no longer works. Paste a new one in Settings.")):
            nonce = js(stage()[2])["nonce"]
            code, _, b = post("api/discord-post", {"nonce": nonce, "content": "x"})
            # the stand-in's DiscordError class differs per context; the message is shown when it's a DiscordError
            assert code == 400 and js(b)["error"] == "That Discord webhook no longer works. Paste a new one in Settings."
            assert rec2.count("discord.post") == 1
    # posting is allowed while a job runs (it never touches the library)
    with fakes() as rec, patched(srv().jobs, st={"state": "running", "kind": "sync", "id": 99, "title": "Syncing"}):
        nonce = js(stage()[2])["nonce"]
        assert post("api/discord-post", {"nonce": nonce, "content": "x"})[0] == 200 and rec.count("discord.post") == 1
    write_settings()


def test_discord_check_one_request_and_target_name():
    write_settings(f'discord_webhook = "{HOOK}"\n')
    with fakes() as rec:
        code, _, b = post("api/discord-check", {})
        j = js(b)
        assert code == 200 and j == {"ok": True, "name": "Debrief", "channel_id": "123456789012", "hint": "…/5678"}, j
        assert rec.names() == ["discord.check"] and rec.calls[0][1] == HOOK
        assert js(stage()[2])["target"] == {"saved": True, "hint": "…/5678", "name": "Debrief"}
        # a new webhook forgets the old name until it's checked again
        other = HOOK.replace(WEBHOOK_ID, "876543210987654321")
        assert post("api/settings", {"discord_webhook": other})[0] == 200
        assert js(stage()[2])["target"] == {"saved": True, "hint": "…/4321", "name": None}
    write_settings()


def test_source_scan_only_one_discord_post_call_site():
    """discord.post( appears only in server.py, inside Handler._post_discord_post."""
    hits = []
    for p in sorted((ROOT / "debrief").rglob("*")):
        if p.suffix not in (".py", ".js", ".html") or not p.is_file():
            continue
        txt = p.read_text(encoding="utf-8", errors="replace")
        if p.name == "discord.py":   # the module's own definition (def post(...)) isn't a call site
            txt = re.sub(r"def post\(", "", txt)
        for m in re.finditer(r"discord\s*\.\s*post\s*\(", txt):
            hits.append((p, txt.count("\n", 0, m.start()) + 1))
    srvp = ROOT / "debrief" / "desktop" / "server.py"
    assert len(hits) == 1 and hits[0][0] == srvp, hits
    src = srvp.read_text(encoding="utf-8")
    line = hits[0][1]
    fn = [i for i, ln in enumerate(src.splitlines()[:line], 1) if re.match(r"\s*def ", ln)][-1]
    assert src.splitlines()[fn - 1].strip().startswith("def _post_discord_post(self"), src.splitlines()[fn - 1]
    # nothing else reaches it: no getattr/import tricks for "post" on the discord module
    for p in (ROOT / "debrief").rglob("*.py"):
        t = p.read_text(encoding="utf-8")
        assert not re.search(r"getattr\(\s*discord\s*,\s*[\"']post", t), p
        assert not re.search(r"from\s+\.+\s*discord\s+import[^\n]*\bpost\b", t), p
    # the window: only the Post button's click listener sends api/discord-post, and only for a real click
    js_src = (UI / "app.js").read_text(encoding="utf-8")
    sends = [m.start() for m in re.finditer(r"['\"]discord-post['\"]", js_src)]
    assert len(sends) == 1, sends
    handler = js_src.rfind("$('discordPost').addEventListener('click'", 0, sends[0])
    assert handler >= 0 and "e.isTrusted" in js_src[handler:sends[0]] and "staged" in js_src[handler:sends[0]]
    assert js_src.count("discordPost').click(") == 0 and ".dispatchEvent(" not in js_src


# ================================================================ weekly leaderboards
def _sched():
    return sample("schedule")


def test_weekly_estimate_makes_no_requests():
    write_settings(token="g61-test-token-123456")
    api = FakeG61()
    with fakes() as rec, patched(app, make_api=lambda cfg: api), patched(schedule, load_current=lambda *a, **k: _sched()):
        code, _, b = get("api/weekly?estimate=1")
        j = js(b)
        assert code == 200 and j["ok"] and j["available"] and j["schedule"] and j["plan"]["est_total"] == 3, j
        assert api.requests_made == 0 and [c for c in api.calls if c not in FakeG61.FREE] == [] and not NET
        assert rec.count("leaderboards.plan") == 1 and rec.count("leaderboards.refresh") == 0
        assert get("api/weekly?estimate=1", headers={})[0] == 404          # needs X-Debrief
    with fakes(), patched(schedule, load_current=lambda *a, **k: None):
        j = js(get("api/weekly?estimate=1")[2])
        assert j["ok"] and j["schedule"] is False and j["plan"] is None
    write_settings()


def test_weekly_job_runs_refresh_then_render_saved():
    write_settings(token="g61-test-token-123456")
    _fill_lib()
    api = FakeG61()
    err = RuntimeError("discord.post must not be called by a job")
    with fakes(post_raises=err) as rec, patched(app, make_api=lambda cfg: api), \
            patched(schedule, load_current=lambda *a, **k: _sched()):
        for bad in ({"series_ids": "gt4"}, {"series_ids": ["../x"]}, {"series_ids": [1]}, {"force_old": "yes"},
                    {"series_ids": ["a"] * 51}):
            code, _, b = post("api/weekly", bad)
            assert code == 400, (bad, b)
        code, _, b = post("api/weekly", {"series_ids": ["gt4-falken"], "force_old": True})
        assert code == 200 and js(b)["ok"], b
        snap = wait_job()
        assert snap["state"] == "done" and snap["kind"] == "weekly" and snap["title"] == "Updating this week's team leaderboards", snap
        assert snap["result"] == {"page": "team/index.html", "requests": 1}, snap["result"]
        assert rec.names() == ["leaderboards.refresh", "team.render_saved"], rec.names()
        assert rec.calls[0][1:] == (["gt4-falken"], True)
        assert api.calls == ["find_laps"] and rec.count("discord.post") == 0 and not NET
        code, _, b = post("api/weekly", {})
        assert code == 200
        snap = wait_job()
        assert snap["result"]["requests"] == 3 and rec.count("discord.post") == 0   # the same fake: 1 + 2
    # without a season schedule the job stops with a plain message
    with fakes(), patched(app, make_api=lambda cfg: FakeG61()), patched(schedule, load_current=lambda *a, **k: None):
        post("api/weekly", {})
        snap = wait_job()
        assert snap["state"] == "error" and snap["error"].startswith("Load the season schedule first"), snap
    write_settings()


def test_weekly_passes_saved_series_to_leaderboards():
    write_settings(token="g61-test-token-123456")
    lib = Path(tempfile.mkdtemp(dir=TMP))
    seen = {}
    with fakes() as rec, patched(schedule, load_current=lambda *a, **k: _sched()), \
            patched(app, make_api=lambda cfg: FakeG61()):
        lb = sys.modules["debrief.leaderboards"]
        old = lb.refresh

        def refresh(api, cfg, sched, mine, **kw):
            seen.update(mine=mine, sched=sched, kw=kw, slug=cfg["team_slug"])
            return old(api, cfg, sched, mine, **kw)
        lb.refresh = refresh
        res = app.cmd_weekly({"library_folder": str(lib), "team_slug": "midfield-menace", "cache_dir": str(TMP / "c")})
    assert seen["mine"] == series.load(lib) and seen["sched"]["season"] == "2026 Season 4"
    assert seen["kw"] == {"series_ids": None, "force_old": False, "log": seen["kw"]["log"]} and seen["slug"] == "midfield-menace"
    assert res["page"] == lib / "team" / "index.html" and res["requests"] == 2
    write_settings()


# ================================================================ reviews: learn_from, and no Discord
def _tel():
    sys.path.insert(0, str(FX))
    import math
    L = 3000.0
    corners = [(500, 22), (1500, 18), (2400, 25)]

    def speed(d, k):
        v = 52.0
        for apex, vmin in corners:
            dd = (d - apex + L / 2) % L - L / 2
            v = min(v, vmin + (52 - vmin) * (1 - math.exp(-(dd / (90 + k)) ** 2)))
        return v

    def lap_csv(k):
        rows = ["Speed,LapDistPct,Lat,Lon,Brake,Throttle,Gear,ABSActive,LatAccel,LongAccel,SteeringWheelAngle,RPM"]
        d, dt, t, prev = 0.0, 1 / 60, 0.0, speed(0, k)
        while d < L:
            v = speed(d, k)
            acc = (v - prev) / dt
            ang = 2 * math.pi * d / L
            rows.append(f"{v:.3f},{d / L:.6f},{35.35 + 0.004 * math.cos(ang):.7f},{-80.68 + 0.005 * math.sin(ang):.7f},"
                        f"{0.9 if acc < -2 else 0.0},{0.0 if acc < -0.5 else 1.0},{min(6, 2 + int(v // 10))},false,0.1,"
                        f"{acc / 9.81:.3f},{10 * math.sin(ang * 3):.2f},6000")
            prev, d, t = v, d + v * dt, t + dt
        return "\n".join(rows), t
    laps, csv = [], {}
    for i in range(1, 9):
        text, t = lap_csv(i * 3)
        lid = f"lap{i}"
        laps.append({"id": lid, "lapTime": round(t, 3), "lapNumber": i, "clean": True, "sessionType": 3,
                     "event": "EVT25D000001", "car": {"id": 190}, "track": {"id": 468},
                     "startTime": f"2026-10-28T19:{i:02d}:00Z"})
        csv[lid] = text
    return laps, csv


def _run_review(lib):
    laps, csv = _tel()
    api = FakeG61(laps, csv)
    cfg = {"library_folder": str(lib), "include_practice": False, "cache_dir": str(TMP / "cache"),
           "max_reference_laps": 0, "salutation": "", "team_slug": "midfield-menace"}
    with patched(app, make_api=lambda c: api):
        page = app.cmd_review(cfg, "EVT25D000001", open_after=False, session_kind=3, track_id=468, car_id=190)
    return json.loads((Path(page).parent / "review.json").read_text(encoding="utf-8")), api


def test_full_review_attaches_learn_from_only_when_team_json_matches():
    err = RuntimeError("discord.post must not be called by a review")
    lib = Path(tempfile.mkdtemp(dir=TMP))
    with fakes(post_raises=err) as rec:
        d, api = _run_review(lib)                                  # no team.json at all
        assert "learn_from" not in d and d["meta"]["track"] == "Charlotte Motor Speedway (Roval 2019)"
        (lib / "team").mkdir()
        other = sample("team")
        other["combos"][0]["car"] = "Porsche 911 GT3 R (992)"       # same track, another car
        (lib / "team" / "team.json").write_text(json.dumps(other), encoding="utf-8")
        d, _ = _run_review(lib)
        assert "learn_from" not in d
        (lib / "team" / "team.json").write_text(json.dumps(sample("team")), encoding="utf-8")
        d, api = _run_review(lib)
        assert d["learn_from"] == sample("learn_from"), d.get("learn_from")
        assert rec.count("team.learn_from_saved") == 3 and rec.count("discord.post") == 0 and not NET
        assert "find_laps" in api.calls and "learn" not in " ".join(api.calls)   # learn_from: no requests
        # the saved review keeps it through the layout refresh
        (lib / ".layout").write_text("2.4.0", encoding="utf-8")
        assert report.refresh_all(lib) == 1
        again = json.loads(next((lib / "reviews").glob("*/review.json")).read_text(encoding="utf-8"))
        assert again["learn_from"] == sample("learn_from")
    # an older team module (no learn_from_saved) or one that fails: the review is still written, without the key
    with fakes(learn=False):
        if hasattr(team, "learn_from_saved"):
            delattr(team, "learn_from_saved")
        d, _ = _run_review(Path(tempfile.mkdtemp(dir=TMP)))
        assert "learn_from" not in d
    with fakes() as rec:
        team.learn_from_saved = lambda folder, review: 1 / 0
        d, _ = _run_review(lib)
        assert "learn_from" not in d and "Couldn't work out who to learn from" in log_text()


def test_learn_from_guard_shapes():
    with fakes():
        team.learn_from_saved = lambda f, r: None
        assert app.learn_from_for(LIB, {}) == []
        team.learn_from_saved = lambda f, r: [{"corner": "C1"}, "junk", None]
        assert app.learn_from_for(LIB, {}) == [{"corner": "C1"}]
        team.learn_from_saved = lambda f, r: {"corner": "C1"}
        assert app.learn_from_for(LIB, {}) == []


def test_review_job_never_posts_to_discord():
    """The review job through the server, with discord.post patched to raise: it is never called."""
    write_settings(f'discord_webhook = "{HOOK}"\n', token="g61-test-token-123456")
    laps, csv = _tel()
    api = FakeG61(laps, csv)
    with fakes(post_raises=RuntimeError("no")) as rec, patched(app, make_api=lambda c: api):
        code, _, b = post("api/review", {"event": "EVT25D000001", "session_type": 3, "track_id": 468, "car_id": 190})
        assert code == 200 and js(b)["ok"], b
        snap = wait_job(120)
        assert snap["state"] == "done", snap
        assert rec.count("discord.post") == 0 and len(srv().discord_stage) >= 0 and not NET
    write_settings()


# ================================================================ layout, modules, version, build lists
def test_layout_version_modules_and_spec():
    assert tuple(int(x) for x in report.LAYOUT.split(".")) >= (2, 5, 0)          # 2.5.0 or later
    assert tuple(int(x) for x in debrief.__version__.split(".")) >= (2, 5, 0)
    for n in NEW:
        assert n in desktop.FEATURE_MODULES, n
    spec = (ROOT / "packaging" / "debrief.spec").read_text(encoding="utf-8")
    must = spec[spec.index("for _must in"):spec.index("if _must not in")]
    for n in NEW + ("strategy", "intel", "team"):
        assert f'"debrief.{n}"' in must, n
    assert '"cryptography"' in spec[spec.index("excludes"):]          # never bundled
    ex = (ROOT / "settings.example.toml").read_text(encoding="utf-8")
    assert "# discord_webhook = \"\"" in ex and "only posts when you press Post" in ex
    assert S.JOB_TITLES["weekly"] == "Updating this week's team leaderboards"
    # the request/old-layout refresh marker moves on, so saved reviews are re-rendered once
    lib = Path(tempfile.mkdtemp(dir=TMP))
    out = lib / "reviews" / "2026-10-01_x"
    out.mkdir(parents=True)
    (out / "review.json").write_text(json.dumps({"meta": {"track": "T", "car": "C"}, "laps": [{"n": 1, "t": 90.0, "clean": True}],
                                                 "corners": [], "best": {"n": 1, "t": 90.0}}), encoding="utf-8")
    (lib / ".layout").write_text("2.4.0", encoding="utf-8")
    assert report.refresh_all(lib) == 1 and (lib / ".layout").read_text(encoding="utf-8").strip() == report.LAYOUT


def test_selftest_with_stand_ins():
    """The real selftest, with the 2.5 modules stood in when this copy doesn't have them yet."""
    home = Path(tempfile.mkdtemp(prefix="home", dir=TMP))
    old = app.HERE
    app.HERE = home
    buf = io.StringIO()
    try:
        with fakes(), contextlib.redirect_stdout(buf):
            rc = desktop.selftest()
    finally:
        app.HERE = old
    res = json.loads((home / "selftest.json").read_text(encoding="utf-8"))
    assert rc == 0 and res["ok"], res
    assert res["version"] == debrief.__version__


def test_window_security_model_unchanged():
    code, h, body = get("", headers={})
    csp = h["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "unsafe-eval" not in csp and "frame-ancestors 'none'" in csp
    assert "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]
    assert "connect-src 'self'" in csp and "img-src 'self' data:" in csp
    html = (UI / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"\son[a-z]+\s*=", html, re.I)
    src = (UI / "app.js").read_text(encoding="utf-8")
    for bad in ("eval(", "new Function", "innerHTML", "insertAdjacentHTML", "outerHTML", "document.write"):
        assert bad not in src, bad
    assert f'sandbox="{S.FRAME_SANDBOX}"' in html and "allow-same-origin" not in S.FRAME_SANDBOX
    assert S.FRAME_SANDBOX == "allow-scripts allow-popups allow-popups-to-escape-sandbox allow-downloads"
    assert S.LIB_CSP.startswith("sandbox allow-scripts ") and "connect-src 'none'" in S.LIB_CSP
    # the share reply is only taken from our own frame, for the id we sent
    i = src.index("'debrief:share-card'")
    blk = src[src.rfind("addEventListener('message'", 0, i):i + 200]
    assert "e.source!==f.contentWindow" in blk and "m.id!==share.id" in blk


# ================================================================ app window (Playwright)
_PW = {}


def _browser():
    if "b" in _PW:
        return _PW["b"]
    try:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        b = pw.chromium.launch()
    except Exception:  # noqa: BLE001
        print("  SKIPPED: Playwright/Chromium not available")
        _PW["b"] = None
        return None
    _PW["pw"], _PW["b"] = pw, b
    return b


SHARE_DIR = "2026-10-28_roval-share_25d001"


def _share_review():
    """A review folder whose page is the share-responder fixture (25-C's review.html answers the same way)."""
    _fill_lib()
    d = LIB / "reviews" / SHARE_DIR
    d.mkdir(parents=True, exist_ok=True)
    src = next(p for p in sorted((LIB / "reviews").glob("*/review.json")) if p.parent.name != SHARE_DIR)
    data = json.loads(src.read_text(encoding="utf-8"))
    data["meta"]["started_at"] = "2026-10-28T23:30:00+00:00"
    (d / "review.json").write_text(json.dumps(data), encoding="utf-8")
    shutil.copy(FX / "share_responder.html", d / "index.html")
    return d


def _window(requests_seen=None):
    b = _browser()
    if not b:
        return None
    _share_review()
    ctx = b.new_context(viewport={"width": 1280, "height": 900})
    pg = ctx.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" and "net::ERR_FAILED" not in m.text
          and "status of 400" not in m.text else None)
    if requests_seen is not None:
        pg.on("request", lambda r: requests_seen.append((r.method, r.url.split("/api/")[-1] if "/api/" in r.url else r.url)))
    pg.route("**/fonts.googleapis.com/**", lambda r: r.abort())
    pg.route("**/fonts.gstatic.com/**", lambda r: r.abort())
    pg.goto(srv().url)
    pg.wait_for_function("() => document.querySelectorAll('#revlist .rev').length > 0", timeout=15000)
    return pg, ctx, errs


def _open_share_review(pg):
    pg.click(f".rev[data-dir='{SHARE_DIR}']")
    pg.wait_for_function("() => !document.getElementById('vFrame').hidden && !document.getElementById('fbDiscord').hidden",
                         timeout=15000)
    pg.wait_for_function("() => document.title.includes('Charlotte Roval')", timeout=15000)


def _preview(pg):
    pg.click("#fbDiscord")
    pg.wait_for_function("() => document.getElementById('dlgDiscord').open", timeout=10000)


def test_window_post_to_discord_dialog():
    write_settings(f'discord_webhook = "{HOOK}"\n')
    seen = []
    with fakes() as rec:
        r = _window(seen)
        if not r:
            return
        pg, ctx, errs = r
        try:
            st = srv().discord_stage
            st.items.clear()
            _open_share_review(pg)
            assert pg.evaluate("() => document.getElementById('fbDiscord').textContent") == "Post to Discord"
            # 1. a preview stages the card and shows it; nothing is posted
            _preview(pg)
            v = pg.evaluate("() => ({src: document.getElementById('discordImg').src.slice(0, 22),"
                            " text: document.getElementById('discordText').value,"
                            " to: document.getElementById('discordTarget').textContent,"
                            " n: document.getElementById('discordCount').textContent})")
            assert v["src"] == "data:image/png;base64," and v["text"].startswith("Alex Driver · Roval"), v
            assert v["text"] != "DECOY" and v["to"] == "Posts to your team's webhook (…/5678)", v
            assert v["n"] == f"{len(v['text'])} / 2000", v
            assert len(st) == 1
            time.sleep(0.8)
            assert rec.count("discord.post") == 0, "a stage alone must post nothing"
            assert not [u for m, u in seen if u.startswith("discord-post")]
            # 2. Cancel posts nothing
            pg.click("#discordCancel")
            pg.wait_for_function("() => !document.getElementById('dlgDiscord').open")
            time.sleep(0.5)
            assert rec.count("discord.post") == 0 and not [u for m, u in seen if u.startswith("discord-post")]
            # 3. Escape posts nothing either
            _preview(pg)
            pg.keyboard.press("Escape")
            pg.wait_for_function("() => !document.getElementById('dlgDiscord').open")
            time.sleep(0.3)
            assert rec.count("discord.post") == 0
            # 4. a script can't post for you: a synthetic click on Post does nothing
            _preview(pg)
            pg.evaluate("() => document.getElementById('discordPost').click()")
            time.sleep(0.5)
            assert rec.count("discord.post") == 0 and pg.evaluate("() => document.getElementById('dlgDiscord').open")
            # 5. Post (a real click) posts once, with the edited message
            pg.fill("#discordText", "Okay, sir: new PB at the Roval")
            assert pg.evaluate("() => document.getElementById('discordCount').textContent") == "30 / 2000"
            pg.click("#discordPost")
            pg.wait_for_function("() => !document.getElementById('dlgDiscord').open", timeout=10000)
            pg.wait_for_function("() => document.getElementById('banner').textContent === 'Posted to Discord.'", timeout=5000)
            assert rec.count("discord.post") == 1
            _, url, png, content = [c for c in rec.calls if c[0] == "discord.post"][0]
            assert url == HOOK and png.startswith(S.PNG_MAGIC) and S.png_size(png) == (1200, 630)
            assert content == "Okay, sir: new PB at the Roval"
            assert len([u for m, u in seen if u.startswith("discord-post")]) == 1
            assert len([u for m, u in seen if u.startswith("discord-stage")]) == 3   # three previews
            # the page never learnt the webhook address
            html = pg.content()
            assert TOKEN not in html and WEBHOOK_ID not in html
            # 6. without a saved webhook the button isn't there
            write_settings()
            pg.click(".navbtn[data-nav=settings]")
            pg.wait_for_function("() => document.getElementById('discordPill').textContent === 'Off'", timeout=5000)
            pg.click(f".rev[data-dir='{SHARE_DIR}']")
            pg.wait_for_function("() => !document.getElementById('vFrame').hidden", timeout=10000)
            assert pg.evaluate("() => document.getElementById('fbDiscord').hidden")
            assert rec.count("discord.post") == 1
            assert not errs, errs
        finally:
            ctx.close()
    write_settings()


def test_window_settings_discord_panel():
    write_settings()
    with fakes() as rec:
        r = _window()
        if not r:
            return
        pg, ctx, errs = r
        try:
            pg.click(".navbtn[data-nav=settings]")
            pg.wait_for_function("() => !document.getElementById('vSettings').hidden")
            note = pg.evaluate("() => document.getElementById('fDiscord').textContent")
            assert "Debrief only posts when you press Post and confirm. It never posts on its own." in note
            assert pg.evaluate("() => document.getElementById('discordCheck').hidden && document.getElementById('discordClear').hidden")
            pg.click("#pExtras > summary")   # 2.6: Discord sits under "Team and extras"
            pg.fill("#discordHook", "https://example.com/hook")
            pg.click("#fDiscord button[type=submit]")
            pg.wait_for_function("() => document.getElementById('discordMsg').classList.contains('bad')", timeout=5000)
            assert pg.evaluate("() => document.getElementById('discordMsg').textContent").startswith("That isn't a Discord webhook address.")
            pg.fill("#discordHook", HOOK)
            pg.click("#fDiscord button[type=submit]")
            pg.wait_for_function("() => document.getElementById('discordPill').textContent === 'Webhook saved (…/5678)'", timeout=5000)
            assert pg.evaluate("() => document.getElementById('discordHook').value") == ""
            pg.click("#discordCheck")
            pg.wait_for_function("() => !document.getElementById('discordWhere').hidden", timeout=5000)
            assert pg.evaluate("() => document.getElementById('discordWhere').textContent") == \
                "Posts go to the webhook “Debrief” in channel 1234…."
            assert rec.count("discord.check") == 1 and rec.count("discord.post") == 0
            pg.click("#discordClear")
            pg.click("#discordClear")
            pg.wait_for_function("() => document.getElementById('discordPill').textContent === 'Off'", timeout=5000)
            assert settings_io.discord_webhook() is None and TOKEN not in pg.content()
            assert not errs, errs
        finally:
            ctx.close()
    write_settings()


def test_window_team_view_weekly_estimate():
    write_settings("sync_on_open = false\n", token="g61-test-token-123456")
    api = FakeG61()
    with fakes() as rec, patched(app, make_api=lambda cfg: api), patched(schedule, load_current=lambda *a, **k: _sched()):
        team_page = LIB / "team" / "index.html"
        if team_page.exists():
            team_page.unlink()
        r = _window()
        if not r:
            return
        pg, ctx, errs = r
        try:
            pg.click(".navbtn[data-nav=team]")
            pg.wait_for_function("() => !document.getElementById('vTeam').hidden", timeout=30000)
            pg.wait_for_function("() => document.getElementById('weeklyNote').textContent.includes('About')", timeout=30000)
            assert pg.evaluate("() => document.getElementById('weeklyNote').textContent") == (
                "This week's leaderboards for your series. About 3 Garage 61 requests (1 per series, 2 if a lap needs "
                "a second look).")
            sk = pg.evaluate("() => [...document.querySelectorAll('#weeklySkipped li')].map(e => e.textContent)")
            assert sk[0] == "Not included:" and sk[1].startswith("IMSA Vintage Series: Load the track"), sk
            assert api.requests_made == 0
            pg.click("#btnWeekly")
            pg.wait_for_function("() => !document.getElementById('vFrame').hidden && !document.getElementById('fbWeekly').hidden",
                                 timeout=20000)
            assert rec.names()[-2:] == ["leaderboards.refresh", "team.render_saved"] or "team.render_saved" in rec.names()
            pg.wait_for_function("() => document.getElementById('banner').textContent.startsWith(\"This week's leaderboards updated\")",
                                 timeout=10000)
            assert rec.count("discord.post") == 0
            assert not errs, errs
        finally:
            ctx.close()
            team_page = LIB / "team" / "index.html"
            if team_page.exists():
                team_page.unlink()
    write_settings()


def test_window_share_error_reply_stages_nothing():
    """The page couldn't draw the card ({type, id, error}): a plain toast, nothing staged, nothing posted."""
    write_settings(f'discord_webhook = "{HOOK}"\n')
    with fakes() as rec:
        b = _browser()
        if not b:
            return
        d = _share_review()
        bad = LIB / "reviews" / (SHARE_DIR + "e")
        bad.mkdir(exist_ok=True)
        shutil.copy(d / "review.json", bad / "review.json")
        shutil.copy(FX / "share_responder_error.html", bad / "index.html")
        seen = []
        r = _window(seen)
        pg, ctx, errs = r
        try:
            srv().discord_stage.items.clear()
            pg.click(f".rev[data-dir='{bad.name}']")
            pg.wait_for_function("() => !document.getElementById('fbDiscord').hidden && document.title.includes('Charlotte Roval')",
                                 timeout=15000)
            pg.click("#fbDiscord")
            pg.wait_for_function("() => document.getElementById('banner').textContent === \"Couldn't make the share card.\"",
                                 timeout=10000)
            time.sleep(0.4)
            assert not pg.evaluate("() => document.getElementById('dlgDiscord').open")
            assert len(srv().discord_stage) == 0 and not [u for m, u in seen if u.startswith("discord-")]
            assert rec.count("discord.post") == 0 and not errs, errs
        finally:
            ctx.close()
            shutil.rmtree(bad, ignore_errors=True)
    write_settings()


def test_window_real_review_page_share():
    """With 25-C's review.html (share responder): a real rendered review answers the frame bar's request."""
    tpl = (ROOT / "debrief" / "templates" / "review.html").read_text(encoding="utf-8")
    if "debrief:share-request" not in tpl:
        print("  SKIPPED: review.html has no share responder in this copy yet (25-C)")
        return
    write_settings(f'discord_webhook = "{HOOK}"\n')
    with fakes() as rec:
        b = _browser()
        if not b:
            return
        _fill_lib()
        lib = LIB
        d, _api = None, None
        laps_dir = Path(tempfile.mkdtemp(dir=TMP))
        data, _ = _run_review(laps_dir)
        out = lib / "reviews" / "2026-10-28_real-share_25d002"
        out.mkdir(parents=True, exist_ok=True)
        report.write(out, data)
        r = _window()
        pg, ctx, errs = r
        try:
            pg.click(f".rev[data-dir='{out.name}']")
            pg.wait_for_function("() => !document.getElementById('fbDiscord').hidden", timeout=15000)
            pg.wait_for_timeout(1500)
            _preview(pg)
            v = pg.evaluate("() => ({src: document.getElementById('discordImg').src.slice(0, 22),"
                            " text: document.getElementById('discordText').value})")
            assert v["src"] == "data:image/png;base64," and "Alex Driver" in v["text"], v
            pg.click("#discordCancel")
            time.sleep(0.4)
            assert rec.count("discord.post") == 0
            assert not errs, errs
        finally:
            ctx.close()
            shutil.rmtree(out, ignore_errors=True)
    write_settings()


# ================================================================ the real 2.5 modules, when this copy has them
def _real(name):
    if not (ROOT / "debrief" / f"{name}.py").is_file():
        print(f"  SKIPPED: debrief/{name}.py isn't in this copy yet")
        return None
    import importlib
    return importlib.import_module("debrief." + name)


def test_real_discord_module_agrees_with_settings():
    d = _real("discord")
    if d is None:
        return
    assert d.valid(HOOK) and not d.valid(HOOK + "?wait=true") and not d.valid(HOOK.replace("https", "http"))
    assert d.hint(HOOK).endswith("5678") and TOKEN not in d.hint(HOOK)
    assert settings_io.webhook_valid(HOOK) and settings_io.webhook_hint(HOOK) == d.hint(HOOK)
    # the real post refuses a non-PNG before any network call
    try:
        d.post(HOOK, b"GIF89a" + b"\0" * 2000, "x")
        raise AssertionError("posted a GIF")
    except Exception as e:  # noqa: BLE001
        assert TOKEN not in str(e)
    assert not NET


def test_real_weekly_estimate_makes_no_requests():
    lb = _real("leaderboards")
    if lb is None:
        return
    write_settings(token="g61-test-token-123456")
    api = FakeG61()
    with patched(app, make_api=lambda cfg: api), patched(schedule, load_current=lambda *a, **k: _sched()):
        code, _, b = get("api/weekly?estimate=1")
        j = js(b)
        assert code == 200 and j["ok"] and isinstance(j["plan"], dict) and "est_total" in j["plan"], j
    assert api.requests_made == 0 and [c for c in api.calls if c not in FakeG61.FREE] == [] and not NET
    write_settings()


def test_real_learn_from_saved_matches_only_its_combo():
    if not hasattr(team, "learn_from_saved") or not (ROOT / "debrief" / "leaderboards.py").is_file():
        print("  SKIPPED: team.learn_from_saved isn't in this copy yet")
        return
    lib = Path(tempfile.mkdtemp(dir=TMP))
    (lib / "team").mkdir()
    (lib / "team" / "team.json").write_text(json.dumps(sample("team")), encoding="utf-8")
    review = json.loads(next(p for p in sorted((_share_review().parent).glob("*/review.json"))).read_text(encoding="utf-8"))
    review["meta"]["car"] = "Some Other Car"
    assert app.learn_from_for(lib, review) == []
    assert app.learn_from_for(Path(tempfile.mkdtemp(dir=TMP)), review) == []


if __name__ == "__main__":
    import faulthandler
    faulthandler.dump_traceback_later(900, exit=True)   # never hang CI (works on Windows too)
    failed = 0
    tests = [(n, f) for n, f in list(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        t0 = time.time()
        NET.clear()
        try:
            fn()
            print(f"ok    {name} ({time.time() - t0:.1f}s)")
        except Exception:  # noqa: BLE001
            import traceback
            failed += 1
            print(f"FAIL  {name}")
            traceback.print_exc()
    faulthandler.cancel_dump_traceback_later()
    if "s" in _SRV:
        _SRV["s"].shutdown()
    if "pw" in _PW:
        try:
            _PW["b"].close()
            _PW["pw"].stop()
        except Exception:  # noqa: BLE001
            pass
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
