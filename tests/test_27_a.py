"""Debrief 2.7.0 "Trust", team A: honest update wording (H1), logging into debrief.log (H2), secrets at
rest (H3), server error visibility (M6), CI release lookup (M8), bundled fonts (M9), diagnostics (L3).

    python3 tests/test_27_a.py

Windows test rules: no SIGALRM, no /tmp, every open() says encoding="utf-8". No network: a fake Garage 61
is never needed here; the server is started on 127.0.0.1 with a throwaway settings file.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="debrief-27a-"))
for _k in ("DEBRIEF_SETTINGS", "DEBRIEF_CACHE_DIR", "DEBRIEF_SCHEDULE_DIR", "DEBRIEF_UPDATE_STATE"):
    os.environ[_k] = str(TMP / _k.lower())
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"

import debrief  # noqa: E402
from debrief import app, updater  # noqa: E402
from debrief import desktop  # noqa: E402
from debrief.desktop import server as S  # noqa: E402
from debrief.desktop import settings_io  # noqa: E402

ST = Path(os.environ["DEBRIEF_SETTINGS"])
LIB = TMP / "lib"
TOKEN = "g61-test-token-ABCDEFGHIJKLMNOP"
HOOK = "https://discord.com/api/webhooks/123456789012345678/" + "wXyZ" * 17   # 68-character token
HOME = str(Path.home())
ENV = {}


def write_settings(extra: str = "", token: str = TOKEN) -> None:
    LIB.mkdir(parents=True, exist_ok=True)
    ST.parent.mkdir(parents=True, exist_ok=True)
    ST.write_text(f'token = "{token}"\nlibrary_folder = \'{LIB}\'\napi_base = "http://127.0.0.1:9/api/v1/"\n' + extra,
                  encoding="utf-8")


def srv():
    if "srv" not in ENV:
        write_settings()
        app.LOG_FILE = str(TMP / "debrief.log")
        s = S.DebriefServer(heartbeat_timeout=3600)
        threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
        ENV["srv"] = s
        ENV["opener"] = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return ENV["srv"]


def get(rel, header=True, base=None):
    s = srv()
    h = {"X-Debrief": "1"} if header else {}
    rq = urllib.request.Request((base or s.url) + rel, headers=h)
    try:
        with ENV["opener"].open(rq, timeout=30) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def js(b: bytes):
    return json.loads(b.decode("utf-8"))


class patched:
    def __init__(self, obj, **kw):
        self.obj, self.kw, self.old = obj, kw, {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = getattr(self.obj, k)
            setattr(self.obj, k, v)
        return self

    def __exit__(self, *a):
        for k, v in self.old.items():
            setattr(self.obj, k, v)


# ================================================================ H1: honest update wording
def test_verification_level_follows_the_keys():
    assert updater.RELEASE_PUBKEYS == () and updater.RELEASE_PUBLIC_KEY is None   # 2.7.0 ships unsigned still
    assert updater.verification_level() == "checksum"
    assert updater.installing_message().startswith("Checksum matched. Installing...")
    assert "Signature" not in updater.installing_message() and "Verified." not in updater.installing_message()
    assert updater.verification_note() == ("Updates are checked by SHA-256 checksum. Signed updates switch on "
                                           "once a signing key is published.")
    with patched(updater, RELEASE_PUBKEYS=("ab" * 32,)):
        assert updater.verification_level() == "signature"
        assert updater.installing_message().startswith("Signature verified. Installing...")
        assert updater.verification_note() == "Updates are signature-verified."
    with patched(updater, RELEASE_PUBKEYS=("not-a-key",)):      # a broken key refuses updates: never "checksum"
        assert updater.verification_level() == "signature"
    assert updater.verification_level() == "checksum"


def test_update_job_says_only_what_it_checked():
    src = (ROOT / "debrief" / "desktop" / "server.py").read_text(encoding="utf-8")
    assert "Verified. Installing" not in src and "updater.installing_message()" in src
    seen = []

    class Done(Exception):
        pass

    def fake_download(info, progress=None):
        return TMP / "x.exe"

    def fake_launch(path, version=None):
        raise Done()
    with patched(app, FROZEN=True), patched(updater, check=lambda force=False: {"newer": True, "latest": "9.9.9", "current": "2.7.0"},
                                           download=fake_download, launch=fake_launch):
        try:
            S._run_update(lambda f, m: seen.append(m))
        except Done:
            pass
    assert seen[-1].startswith("Checksum matched. Installing..."), seen
    assert not any("Signature" in m or m.startswith("Verified") for m in seen)


def test_settings_view_carries_the_verification_wording():
    write_settings()
    v = settings_io.public_view()
    assert v["verification_level"] == "checksum" and "SHA-256 checksum" in v["verification_note"]
    with patched(updater, RELEASE_PUBKEYS=("cd" * 32,)):
        v = settings_io.public_view()
        assert v["verification_level"] == "signature" and v["verification_note"] == "Updates are signature-verified."
    code, _, body = get("api/settings")
    j = js(body)
    assert code == 200 and j["verification_level"] == "checksum" and "signing key" in j["verification_note"]
    html = (ROOT / "debrief" / "desktop" / "ui" / "index.html").read_text(encoding="utf-8")
    assert 'id="updVerify"' in html and "SHA-256 checksum" in html
    appjs = (ROOT / "debrief" / "desktop" / "ui" / "app.js").read_text(encoding="utf-8")
    assert "verification_note" in appjs
    doc = (ROOT / "packaging" / "SIGNING.md").read_text(encoding="utf-8")
    assert "Checksum matched" in doc and "Signature verified" in doc and "verification_level()" in doc


# ================================================================ H2: logging reaches debrief.log
def test_logging_handler_forwards_warnings_to_say():
    seen = []
    h = desktop.attach_logging(seen.append)
    try:
        assert desktop.attach_logging(seen.append) is h                      # idempotent
        logging.getLogger("debrief.sync").warning("laps file %s unreadable; set aside as %s", "a.json", "a.json.bad")
        try:
            raise ValueError("boom")
        except ValueError:
            logging.getLogger("debrief.library").exception("Skill profile failed: %s", "boom")
        logging.getLogger("debrief.discord").info("Posted to Discord (webhook %s)", "…1234")   # INFO: not forwarded
        logging.getLogger("somebody.else").warning("not ours")
    finally:
        logging.getLogger("debrief").removeHandler(h)
    assert seen[0] == "[sync] WARNING: laps file a.json unreadable; set aside as a.json.bad", seen
    assert seen[1].startswith("[library] ERROR: Skill profile failed: boom\nTraceback") and "ValueError: boom" in seen[1]
    assert len(seen) == 2, seen
    # every logging call in the modules that use it is at WARNING or above, except discord's Posted line
    for mod, levels in (("sync.py", {"warning"}), ("library.py", {"exception"}), ("discord.py", {"info"})):
        src = (ROOT / "debrief" / mod).read_text(encoding="utf-8")
        used = set(re.findall(r"_log\.(\w+)\(|getLogger\([^)]*\)\.(\w+)\(", src))
        used = {a or b for a, b in used}
        assert used == levels, (mod, used)
    # the real handler writes through app.say into debrief.log
    log = TMP / "log-h2.log"
    with patched(app, LOG_FILE=str(log)):
        h = desktop.attach_logging()
        try:
            logging.getLogger("debrief.sync").warning("sync state unreadable; set aside as %s", "state.bad")
        finally:
            logging.getLogger("debrief").removeHandler(h)
    assert "[sync] WARNING: sync state unreadable; set aside as state.bad" in log.read_text(encoding="utf-8")


def test_startup_line_names_the_version():
    src = (ROOT / "debrief" / "desktop" / "__init__.py").read_text(encoding="utf-8")
    assert "debrief desktop start " in src and "Debrief {__version__}" in src
    assert "attach_logging()" in src.split("def main(", 1)[1]


# ================================================================ H3: secrets at rest
def test_secret_forms_both_load():
    write_settings(f'discord_webhook = "{HOOK}"\n')
    raw, err = settings_io._raw()
    assert err is None and settings_io.read_secret(raw, "token") == TOKEN
    assert settings_io.discord_webhook() == HOOK and settings_io.token_present(raw)
    cfg = app.load_settings()
    assert cfg["token"] == TOKEN and cfg["discord_webhook"] == HOOK and "token_dpapi" not in cfg
    # the protected form: unreadable here (not Windows or another account) means "no secret", never a crash
    ST.write_text(f'token_dpapi = "AAECAw=="\nlibrary_folder = \'{LIB}\'\ndiscord_webhook_dpapi = "AAECAw=="\n',
                  encoding="utf-8")
    raw, _ = settings_io._raw()
    if os.name != "nt":
        assert settings_io.read_secret(raw, "token") == "" and not settings_io.token_present(raw)
        assert settings_io.discord_webhook() is None
        cfg = app.load_settings()
        assert "token_dpapi" not in cfg and "discord_webhook_dpapi" not in cfg and not cfg.get("token")
    assert settings_io.unprotect_secret("not base64!") is None and settings_io.unprotect_secret("") is None
    # protected first, plain as a fallback (an install that was downgraded and re-upgraded)
    ST.write_text(f'token = "{TOKEN}"\ntoken_dpapi = "AAECAw=="\nlibrary_folder = \'{LIB}\'\n', encoding="utf-8")
    raw, _ = settings_io._raw()
    if os.name != "nt":
        assert settings_io.read_secret(raw, "token") == TOKEN
    v = settings_io.public_view()
    assert v["token_saved"] is True or os.name == "nt"
    assert TOKEN not in json.dumps(v) and HOOK not in json.dumps(v)
    write_settings()


def test_plaintext_round_trip_off_windows():
    """Linux (CI, the tests): secrets_protected() is False, so save() writes the plain keys as before."""
    write_settings()
    v = settings_io.save({"token": TOKEN + "-2", "discord_webhook": HOOK})
    txt = ST.read_text(encoding="utf-8")
    if os.name == "nt" and settings_io.secrets_protected():
        print("  (Windows installed-app mode: covered by test_dpapi_round_trip_on_windows)")
        return
    assert v["secrets_at_rest"] == "plain"
    assert f'token = "{TOKEN}-2"' in txt and f'discord_webhook = "{HOOK}"' in txt and "_dpapi" not in txt
    assert settings_io.discord_webhook() == HOOK and app.load_settings()["token"] == TOKEN + "-2"
    assert v["token_hint"] == "…" + (TOKEN + "-2")[-4:] and v["discord_saved"] is True
    assert TOKEN not in json.dumps(v) and HOOK not in json.dumps(v)
    v = settings_io.save({"clear_discord": True})
    assert v["discord_saved"] is False and settings_io.discord_webhook() is None
    write_settings()


def test_dpapi_round_trip_on_windows():
    if os.name != "nt":
        print("  SKIPPED: DPAPI is Windows-only (plaintext round trip tested instead)")
        return
    assert settings_io.dpapi_available()
    blob = settings_io.protect_secret(TOKEN)
    assert blob != TOKEN and TOKEN not in blob and re.fullmatch(r"[A-Za-z0-9+/=]+", blob)
    assert settings_io.unprotect_secret(blob) == TOKEN
    assert settings_io.unprotect_secret(blob[:-8] + "AAAAAAA=") in (None, TOKEN)   # a damaged blob: never a crash
    # the installed-app path: a plain token is rewritten protected on the first save, the webhook saved protected
    write_settings()
    os.environ["DEBRIEF_PROTECT_SECRETS"] = "1"
    try:
        assert settings_io.secrets_protected()
        v = settings_io.save({"discord_webhook": HOOK})
        txt = ST.read_text(encoding="utf-8")
        assert TOKEN not in txt and HOOK not in txt, txt
        assert 'token = ""' in txt and 'token_dpapi = "' in txt and 'discord_webhook_dpapi = "' in txt
        assert v["secrets_at_rest"] == "dpapi" and v["token_saved"] and v["token_hint"] == "…" + TOKEN[-4:]
        assert v["discord_saved"] and v["discord_hint"] == "…/5678"
        assert settings_io.discord_webhook() == HOOK
        cfg = app.load_settings()
        assert cfg["token"] == TOKEN and cfg["discord_webhook"] == HOOK and "token_dpapi" not in cfg
        assert TOKEN not in json.dumps(v) and HOOK not in json.dumps(v)
        v = settings_io.save({"token": TOKEN + "-3", "clear_discord": True})
        txt = ST.read_text(encoding="utf-8")
        assert TOKEN not in txt and 'discord_webhook_dpapi = ""' in txt
        assert app.load_settings()["token"] == TOKEN + "-3" and settings_io.discord_webhook() is None
        assert v["discord_saved"] is False
    finally:
        os.environ.pop("DEBRIEF_PROTECT_SECRETS", None)
    write_settings()


def test_example_settings_mention_the_protected_form():
    ex = (ROOT / "settings.example.toml").read_text(encoding="utf-8")
    assert "token_dpapi" in ex and "discord_webhook_dpapi" in ex
    app._parse_toml(ex)   # still valid TOML


# ================================================================ M6: server errors are visible
def test_handle_error_logs_tracebacks_but_not_disconnects():
    """socketserver calls server.handle_error(request, client_address) when a request thread raises
    (http.server has no handler-level hook). The default printed to stderr, which pythonw drops."""
    s = srv()
    seen = []
    assert "handle_error" in S.DebriefServer.__dict__
    with patched(app, say=seen.append):
        try:
            raise RuntimeError("request thread blew up")
        except RuntimeError:
            s.handle_error(None, ("127.0.0.1", 1))
        assert seen and seen[-1].startswith("ERROR handling a request from the window:\nTraceback") \
            and "RuntimeError: request thread blew up" in seen[-1]
        n = len(seen)
        for exc in (ConnectionResetError("gone"), BrokenPipeError("gone"), ConnectionAbortedError("gone")):
            try:
                raise exc
            except OSError:
                s.handle_error(None, ("127.0.0.1", 1))
        assert len(seen) == n
    # end to end: a handler method that raises is logged, and the server keeps serving
    real = S.Handler.do_HEAD

    def broken(self):
        raise RuntimeError("do_HEAD blew up")
    S.Handler.do_HEAD = broken
    try:
        with patched(app, say=seen.append):
            rq = urllib.request.Request(s.url + "api/status", method="HEAD")
            try:
                ENV["opener"].open(rq, timeout=10).close()
            except (urllib.error.URLError, ConnectionError, OSError):
                pass
            for _ in range(50):
                if any("do_HEAD blew up" in m for m in seen):
                    break
                threading.Event().wait(0.1)
    finally:
        S.Handler.do_HEAD = real
    assert any("do_HEAD blew up" in m and m.startswith("ERROR handling a request") for m in seen), seen[-3:]
    assert get("api/status")[0] == 200


def test_watchdog_survives_an_error_and_still_stops():
    s = S.DebriefServer(heartbeat_timeout=0.5, startup_grace=0)
    exits, seen = [], []
    s.exit = lambda code=0: exits.append(code)
    s.last_ping -= 10   # the window is long gone

    class J:
        kind = "sync"
        busy = False
        lock = threading.Lock()

        @staticmethod
        def pause_sync(*a):
            raise RuntimeError("watchdog bug")
    with patched(app, say=seen.append), patched(s, jobs=J()):
        t = threading.Thread(target=s.watchdog, daemon=True)
        t.start()
        t.join(10)
    assert not t.is_alive() and s.stopping.is_set()
    assert any(m.startswith("ERROR in the window watchdog:\nTraceback") and "watchdog bug" in m for m in seen), seen
    assert any("Stopping" in m for m in seen)
    s.server_close()


# ================================================================ L3: diagnostics
def test_diagnostics_fields_and_scrubbing():
    s = srv()
    write_settings(f'discord_webhook = "{HOOK}"\n')
    log = Path(app.LOG_FILE)
    with open(log, "w", encoding="utf-8") as f:
        for i in range(300):
            f.write(f"line {i}\n")
        f.write(f"token in the clear: {TOKEN} and bearer {TOKEN}\n")
        f.write(f"webhook {HOOK}\n")
        f.write("mail from swazi.test@example.com\n")
        f.write(f"path {HOME}{os.sep}secret-folder{os.sep}x\n")
    assert get("api/diagnostics", header=False)[0] == 404
    assert get("api/diagnostics", base=f"http://127.0.0.1:{s.port}/not-the-secret/")[0] == 404
    code, _, body = get("api/diagnostics")
    assert code == 200, body
    d = js(body)
    assert d["ok"] and d["version"] == debrief.__version__ and d["python"] == sys.version.split()[0]
    assert isinstance(d["platform"], str) and d["platform"] and d["frozen"] is False
    assert d["verification_level"] == "checksum" and d["reviews"] == 0 and d["sessions"] == 0
    assert d["settings_folder"] and isinstance(d["cache_bytes"], int)
    assert len(d["log"]) == 200 and d["log"][0] == "line 104"
    txt = body.decode("utf-8")
    lines = "\n".join(d["log"])
    assert TOKEN not in txt and HOOK not in txt and "wXyZ" not in txt
    assert "swazi.test@example.com" not in txt and "[email]" in lines
    assert "secret-folder" in lines and HOME not in lines and "~" + os.sep + "secret-folder" in lines
    assert "token in the clear: … and bearer …" in lines and "webhook …" in lines
    # the scrubber on its own
    sc = S.Handler.scrub_diagnostics
    assert sc("Bearer abcdefghijklmnop", ()) == "Bearer …" and sc("token=abcdefghijklmnop x", ()) == "token=… x"
    assert sc("nobody here", ()) == "nobody here"
    write_settings()


def test_about_has_copy_diagnostics():
    html = (ROOT / "debrief" / "desktop" / "ui" / "index.html").read_text(encoding="utf-8")
    appjs = (ROOT / "debrief" / "desktop" / "ui" / "app.js").read_text(encoding="utf-8")
    assert 'id="btnDiag"' in html and "Copy diagnostics" in html and 'class="btn" id="btnDiag"' in html
    assert "api('diagnostics')" in appjs and "navigator.clipboard" in appjs and "execCommand('copy')" in appjs
    assert 'id="btnFolder"' in html and "open-folder" in appjs      # the existing "Open folder" route still serves


# ================================================================ M9: fonts
def test_no_page_references_google_fonts():
    files = [ROOT / "debrief" / "desktop" / "ui" / "index.html", ROOT / "debrief" / "desktop" / "ui" / "app.css",
             ROOT / "debrief" / "desktop" / "ui" / "app.js"] + sorted((ROOT / "debrief" / "templates").glob("*.html"))
    for p in files:
        txt = p.read_text(encoding="utf-8")
        assert "fonts.googleapis.com" not in txt and "fonts.gstatic.com" not in txt, p
    for p in (ROOT / "debrief" / "templates").glob("*.html"):   # no font link at all: from disk, system fonts
        head = p.read_text(encoding="utf-8").split("<style>", 1)[0]
        assert "<link" not in head and "Fonts (2.7.0)" in head, p
    idx = (ROOT / "debrief" / "desktop" / "ui" / "index.html").read_text(encoding="utf-8")
    assert '<link rel="stylesheet" href="static/fonts/fonts.css">' in idx


def test_csp_has_no_google_entries():
    csp = S.ui_csp("<html></html>")
    assert "google" not in csp and "gstatic" not in csp
    assert "font-src 'self';" in csp and "style-src 'self' 'unsafe-inline';" in csp
    assert "google" not in S.LIB_CSP


def test_fonts_are_bundled_and_served():
    fonts = ROOT / "debrief" / "desktop" / "ui" / "fonts"
    css = (fonts / "fonts.css").read_text(encoding="utf-8")
    assert (fonts / "LICENSE.txt").exists() and "SIL Open Font License" in (fonts / "LICENSE.txt").read_text(encoding="utf-8")
    want = {("IBM Plex Sans", w) for w in (400, 500, 600)} | {("Barlow Condensed", w) for w in (600, 700)} \
        | {("JetBrains Mono", w) for w in (400, 600)}
    got = {(m.group(1), int(m.group(2))) for m in re.finditer(r"font-family:'([^']+)';font-style:normal;font-weight:(\d+)", css)}
    assert got == want, got
    assert css.count("font-display:swap") == len(re.findall(r"@font-face", css))
    urls = re.findall(r"url\(\./([a-z0-9-]+\.woff2)\)", css)
    assert urls and all((fonts / u).is_file() for u in urls)
    total = sum(p.stat().st_size for p in fonts.iterdir())
    assert total < 400 * 1024, total
    spec = (ROOT / "packaging" / "debrief.spec").read_text(encoding="utf-8")
    assert '"debrief/desktop/ui/fonts"' in spec
    # served by the window's server, with types Edge accepts and no secret involved
    code, h, body = get("static/fonts/fonts.css", header=False)
    assert code == 200 and h["Content-Type"].startswith("text/css") and body.decode("utf-8") == css
    assert h.get("Cross-Origin-Resource-Policy") == "cross-origin"   # library pages are an opaque origin
    code, h, body = get("static/fonts/" + urls[0], header=False)
    assert code == 200 and h["Content-Type"] == "font/woff2" and body[:4] == b"wOF2"
    assert h.get("Access-Control-Allow-Origin") == "*"
    for bad in ("static/fonts/LICENSE.txt", "static/fonts/../app.js", "static/fonts/", "static/fonts/x.ttf"):
        assert get(bad, header=False)[0] == 404, bad


def test_library_pages_get_the_local_font_link():
    s = srv()
    secret = s.secret
    for name in ("review.html", "library.html", "prep.html", "team.html"):
        html = (ROOT / "debrief" / "templates" / name).read_bytes()
        out = S.with_fonts(html, secret)
        head = out[:out.lower().find(b"</head>")].decode("utf-8")
        link = f'<link rel="stylesheet" href="/{secret}/static/fonts/fonts.css">'
        assert head.count(link) == 1 and head.index(link) < head.index("<style>"), name
        assert "googleapis" not in head
        assert S.with_fonts(out, secret) == out                       # serving an already-served page: no second link
        assert out[out.lower().find(b"</head>"):] == html[html.lower().find(b"</head>"):]   # the body is untouched
    # a page saved by 2.6 still carries the Google links: they are replaced as it is served
    old = (b'<!doctype html><html><head><link rel="preconnect" href="https://fonts.googleapis.com">\n'
           b'<link rel="stylesheet" referrerpolicy="no-referrer" href="https://fonts.googleapis.com/css2?family=X&display=swap">\n'
           b'<style>a{}</style></head><body>fonts.googleapis.com in the data stays</body></html>')
    out = S.with_fonts(old, "SEC")
    assert out.count(b"googleapis") == 1 and b'href="/SEC/static/fonts/fonts.css"' in out and b"preconnect" not in out
    # end to end: a review page served from the library has the local link and the bridge, under the sandbox CSP
    d = LIB / "reviews" / "2026-01-01_test"
    d.mkdir(parents=True, exist_ok=True)
    (d / "review.json").write_text("{}", encoding="utf-8")
    (d / "index.html").write_bytes(old)
    code, h, body = get("lib/reviews/2026-01-01_test/index.html", header=False)
    assert code == 200 and b'href="/' + secret.encode() + b'/static/fonts/fonts.css"' in body
    assert b"debrief-frame-bridge" in body and "sandbox" in h["Content-Security-Policy"]


# ================================================================ M8: CI
def test_build_workflow_authenticates_the_release_lookup():
    yml = (ROOT / ".github" / "workflows" / "build.yml").read_text(encoding="utf-8")
    step = yml[yml.index("Upgrade test (previous release"):yml.index("Silent install smoke test")]
    assert 'Authorization' in step and 'Bearer $env:GITHUB_TOKEN' in step
    assert "GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}" in step
    assert "::warning title=Upgrade test skipped::" in step and "try {" in step and "catch {" in step
    assert "-TimeoutSec" in step


if __name__ == "__main__":
    failed = 0
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        try:
            fn()
            print(f"ok    {name}")
        except Exception:  # noqa: BLE001
            import traceback
            failed += 1
            print(f"FAIL  {name}")
            traceback.print_exc()
    print(f"{len(tests) - failed}/{len(tests)} passed")
    if "srv" in ENV:
        try:
            ENV["srv"].shutdown()
            ENV["srv"].server_close()
        except Exception:  # noqa: BLE001
            pass
    sys.exit(1 if failed else 0)
