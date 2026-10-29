"""Team C fixes (2.2.2): server, security, updater, settings file, report writes, build.

    python3 tests/test_fix_c.py

Never contacts GitHub, Garage 61 or iRacing: requests.get is replaced with fakes, and the in-process
server is only reached on 127.0.0.1. The browser checks use Playwright's Chromium and are skipped when
it isn't installed.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packaging"))
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"

TMP = Path(tempfile.mkdtemp(prefix="debrief-fixc-"))
LIB = TMP / "lib"
(LIB / "reviews").mkdir(parents=True)
ST = TMP / "settings.toml"
ST.write_text(f'token = "PASTE-YOUR-GARAGE61-TOKEN-HERE"\nlibrary_folder = \'{LIB}\'\n', encoding="utf-8")
os.environ.update(DEBRIEF_SETTINGS=str(ST), DEBRIEF_CACHE_DIR=str(TMP / "cache"),
                  DEBRIEF_SCHEDULE_DIR=str(TMP / "sched"), DEBRIEF_UPDATE_STATE=str(TMP / "update.json"))

import requests  # noqa: E402

_real_get = requests.get


def _blocked(*a, **k):
    raise requests.ConnectionError("network blocked in tests")


requests.get = _blocked
requests.post = _blocked

from debrief import __version__, app, fsutil, report, updater  # noqa: E402
from debrief.desktop import server as S  # noqa: E402
from debrief.desktop import settings_io  # noqa: E402
import debrief.desktop as desktop  # noqa: E402
import sign_release  # noqa: E402

OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ================================================================ helpers
class Resp:
    def __init__(self, status=200, body=b"", js=None):
        self.status_code, self._b, self._js = status, body, js
        self.text = body.decode("latin-1") if isinstance(body, bytes) else body

    def json(self):
        if isinstance(self._js, Exception):
            raise self._js
        return self._js

    def iter_content(self, n):
        for i in range(0, len(self._b), n):
            yield self._b[i:i + n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


DL = "https://github.com/blackmagik232/Debrief/releases/download/v9.9.9/"
PAYLOAD = b"MZ fake installer bytes " * 3000
SHA = hashlib.sha256(PAYLOAD).hexdigest()
SEED = bytes.fromhex("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb")   # RFC 8032 test 2
PUB = bytes.fromhex("3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c")


def release(sig_text=None, with_sig=True, tag="v9.9.9", payload=PAYLOAD, name=None):
    nm = name or f"Debrief-Setup-{tag[1:]}.exe"
    assets = [{"name": nm, "browser_download_url": DL + nm, "size": len(payload),
               "digest": "sha256:" + hashlib.sha256(payload).hexdigest()}]
    if with_sig:
        assets.append({"name": nm + ".sig", "browser_download_url": DL + nm + ".sig"})
    return {"tag_name": tag, "html_url": "https://github.com/x", "body": "", "assets": assets}


def fake(rel, payload=PAYLOAD, sig_text=None):
    def get(url, **kw):
        if url == updater.API:
            return Resp(js=rel)
        if url.endswith(".sig"):
            return Resp(body=(sig_text or "").encode())
        return Resp(body=payload)
    return get


class KeySet:
    """Temporarily set RELEASE_PUBLIC_KEY."""
    def __init__(self, key):
        self.key = key

    def __enter__(self):
        self.old = updater.RELEASE_PUBLIC_KEY
        updater.RELEASE_PUBLIC_KEY = self.key

    def __exit__(self, *a):
        updater.RELEASE_PUBLIC_KEY = self.old


def expect_update_error(fn, contains=""):
    try:
        fn()
    except updater.UpdateError as e:
        assert contains in str(e), str(e)
        return str(e)
    raise AssertionError("no UpdateError")


_SRV = {}


def srv():
    if "s" not in _SRV:
        s = S.DebriefServer(heartbeat_timeout=3600)
        threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
        _SRV["s"] = s
    return _SRV["s"]


def get(rel, headers=None):
    rq = urllib.request.Request(srv().url + rel, headers=headers or {})
    try:
        with OP.open(rq, timeout=20) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def post(rel, body):
    rq = urllib.request.Request(srv().url + rel, data=json.dumps(body).encode(), method="POST",
                                headers={"X-Debrief": "1", "Content-Type": "application/json"})
    try:
        with OP.open(rq, timeout=20) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def raw_http(payload: bytes, timeout=2.0) -> bytes:
    c = socket.create_connection(("127.0.0.1", srv().port), 5)
    c.settimeout(timeout)
    c.sendall(payload)
    buf = b""
    try:
        while True:
            d = c.recv(65536)
            if not d:
                break
            buf += d
    except socket.timeout:
        pass
    c.close()
    return buf


def browser():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    return sync_playwright


# ================================================================ H1: release signatures
RFC8032 = [  # (secret, public, message, signature) from RFC 8032 section 7.1
    ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
     "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
    ("c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
     "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025", "af82",
     "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a"),
    ("833fe62409237b9d62ec77587520911e9a759cec1d19755b7da901b96dca3d42",
     "ec172b93ad5e563bf4932c70e1245034c35467ef2efd4d64ebf819683467e2bf",
     "ddaf35a193617abacc417349ae20413112e6fa4e89a97ea20a9eeee64b55d39a2192992a274fc1a836ba3c23a3feebbd454d4423643ce80e2a9ac94fa54ca49f",
     "dc2a4459e7369633a52b1bf277839a00201009a3efbf3ecb69bea2186c26b58909351fc9ac90b3ecfdfbc7c66431e0303dca179c138ac17ad9bef1177331a704"),
]


def test_h1_ed25519_rfc8032_vectors():
    for sk, pk, m, sig in RFC8032:
        pk, m, sig = bytes.fromhex(pk), bytes.fromhex(m), bytes.fromhex(sig)
        assert updater.ed25519_verify(pk, m, sig)
        # the signer used by CI produces exactly the RFC signature (deterministic Ed25519)
        assert sign_release.public_key(bytes.fromhex(sk)) == pk
        assert sign_release.sign(bytes.fromhex(sk), m) == sig


def test_h1_ed25519_rejects_tampering():
    sk, pk, m, sig = (bytes.fromhex(x) for x in RFC8032[1])
    assert not updater.ed25519_verify(pk, m + b"x", sig)                 # other message
    for i in (0, 31, 32, 63):                                            # any flipped bit
        bad = bytearray(sig)
        bad[i] ^= 1
        assert not updater.ed25519_verify(pk, m, bytes(bad))
    assert not updater.ed25519_verify(bytes.fromhex(RFC8032[0][1]), m, sig)  # other key
    # S + L is the same point maths but non-canonical: must be refused (malleability)
    S_ = int.from_bytes(sig[32:], "little") + updater._L
    if S_ < 2 ** 256:
        assert not updater.ed25519_verify(pk, m, sig[:32] + S_.to_bytes(32, "little"))
    assert not updater.ed25519_verify(pk[:31], m, sig) and not updater.ed25519_verify(pk, m, sig[:63])
    assert not updater.ed25519_verify(b"\xff" * 32, m, sig)              # not a point on the curve
    assert not updater.ed25519_verify(None, m, sig)


def _sig_for(payload=PAYLOAD, name="Debrief-Setup-9.9.9.exe", seed=SEED):
    return sign_release.sign(seed, updater.signed_message(name, hashlib.sha256(payload).hexdigest())).hex()


def test_h1_unsigned_allowed_only_while_no_key():
    assert updater.RELEASE_PUBLIC_KEY is None   # shipped state: the owner adds the key later
    requests.get = fake(release(with_sig=False))
    try:
        info = updater.check(force=True)
        assert updater.download(info, dest_dir=TMP / "h1a").read_bytes() == PAYLOAD
        with KeySet(PUB.hex()):
            expect_update_error(lambda: updater.download(info, dest_dir=TMP / "h1b"), "isn't signed")
            assert not (TMP / "h1b" / "Debrief-Setup-9.9.9.exe").exists()
    finally:
        requests.get = _blocked


def test_h1_good_signature_installs_bad_one_blocks():
    with KeySet(PUB.hex()):
        try:
            requests.get = fake(release(), sig_text=_sig_for() + "  Debrief-Setup-9.9.9.exe\n")
            info = updater.check(force=True)
            assert info["sig_url"].endswith(".sig")
            assert updater.download(info, dest_dir=TMP / "h1c").read_bytes() == PAYLOAD
            # signed by someone else
            other = bytes.fromhex(RFC8032[0][0])
            requests.get = fake(release(), sig_text=_sig_for(seed=other))
            expect_update_error(lambda: updater.download(updater.check(force=True), dest_dir=TMP / "h1d"),
                                "signature")
            assert not (TMP / "h1d" / "Debrief-Setup-9.9.9.exe").exists()
            # an attacker-built installer whose digest is its own hash (the r5 repro) with the real
            # release's signature: blocked
            evil = b"MZ attacker-built installer " * 1000
            requests.get = fake(release(payload=evil), payload=evil, sig_text=_sig_for())
            expect_update_error(lambda: updater.download(updater.check(force=True), dest_dir=TMP / "h1e"),
                                "signature")
            # garbage .sig file
            requests.get = fake(release(), sig_text="not a signature")
            expect_update_error(lambda: updater.download(updater.check(force=True), dest_dir=TMP / "h1f"),
                                "isn't signed")
            # an old signed installer passed off as a newer version: the name carries the version
            requests.get = fake(release(tag="v9.9.10", name="Debrief-Setup-9.9.9.exe"), sig_text=_sig_for())
            expect_update_error(lambda: updater.download(updater.check(force=True), dest_dir=TMP / "h1g"),
                                "doesn't match its version")
        finally:
            requests.get = _blocked
    # a mistyped key never means "don't check"
    with KeySet("not-hex"):
        expect_update_error(lambda: updater.download({"installer": DL + "Debrief-Setup-9.9.9.exe",
                                                      "installer_name": "Debrief-Setup-9.9.9.exe", "sha256": SHA}),
                            "broken update key")


def test_h1_sign_release_tool(tmp=TMP / "signtool"):
    tmp.mkdir()
    exe = tmp / "Debrief-Setup-9.9.9.exe"
    exe.write_bytes(PAYLOAD)
    os.environ["DEBRIEF_SIGNING_KEY"] = SEED.hex()
    try:
        assert sign_release.main([str(exe)]) == 0
        sig = bytes.fromhex((tmp / "Debrief-Setup-9.9.9.exe.sig").read_text().strip())
        assert updater.ed25519_verify(PUB, updater.signed_message(exe.name, SHA), sig)
        with KeySet(RFC8032[0][1]):   # key in updater.py doesn't match the secret: refuses to sign
            try:
                sign_release.main([str(exe)])
                raise AssertionError("signed with a key the app would reject")
            except SystemExit as e:
                assert "doesn't match" in str(e)
    finally:
        del os.environ["DEBRIEF_SIGNING_KEY"]


def test_h1_ci_workflow_split_and_signing():
    wf = (ROOT / ".github" / "workflows" / "build.yml").read_text(encoding="utf-8")
    try:
        import yaml
    except ImportError:
        yaml = None
    if yaml:
        d = yaml.safe_load(wf)
        assert d["permissions"] == {"contents": "read"}
        jobs = d["jobs"]
        assert jobs["windows"]["permissions"] == {"contents": "read"}
        rel = jobs["release"]
        assert rel["needs"] == "windows" and rel["permissions"] == {"contents": "write"}
        assert rel["environment"] == "release"
        for job in jobs.values():
            for st in job["steps"]:
                if str(st.get("uses", "")).startswith("actions/checkout"):
                    assert st["with"]["persist-credentials"] is False
        assert not any("gh release" in str(st.get("run", "")) for st in jobs["windows"]["steps"])
    assert "DEBRIEF_SIGNING_KEY" in wf and "sign_release.py" in wf and ".exe.sig" in wf
    assert "pyinstaller==" in wf and "pip install --upgrade pip" not in wf
    assert "tests/test_fix_c.py" in wf


# ================================================================ M1: library pages sandboxed
EVIL = """<!doctype html><html><head><title>Evil</title></head><body><script>
window.__res = 'pending';
fetch('../api/settings', {method: 'POST', headers: {'X-Debrief': '1'}, body: JSON.stringify({team_slug: 'pwned'})})
  .then(r => r.json()).then(j => { window.__res = 'ok:' + JSON.stringify(j) }).catch(e => { window.__res = 'blocked' });
try { window.__parent = String(parent.document.title) } catch (e) { window.__parent = 'blocked' }
</script></body></html>"""


def test_m1_lib_responses_carry_sandbox_csp():
    (LIB / "evil.html").write_text(EVIL, encoding="utf-8")
    (LIB / "data.json").write_text("{}", encoding="utf-8")
    code, h, body = get("lib/evil.html")
    assert code == 200
    csp = h["Content-Security-Policy"]
    assert csp.startswith("sandbox ") and "allow-scripts" in csp and "allow-same-origin" not in csp
    assert b"debrief-frame-bridge" in body and body.index(b"debrief-frame-bridge") < body.lower().rindex(b"</body>")
    assert get("lib/data.json")[1]["Content-Security-Policy"].startswith("sandbox ")
    # the window itself: strict policy that still allows its own inline theme script (by hash)
    code, h, body = get("")
    ui = h["Content-Security-Policy"]
    assert "default-src 'self'" in ui and "'unsafe-inline'" not in ui.split("script-src", 1)[1].split(";")[0]
    assert "'sha256-" in ui and "frame-ancestors 'none'" in ui
    assert S.FRAME_SANDBOX in csp   # the iframe attribute D adds matches the header


def test_m1_planted_page_cannot_reach_api_in_browser():
    sp = browser()
    if sp is None:
        print("  SKIPPED: playwright not installed")
        return
    (LIB / "evil.html").write_text(EVIL, encoding="utf-8")
    before = ST.read_text(encoding="utf-8")
    with sp() as pw:
        try:
            b = pw.chromium.launch()
        except Exception:  # noqa: BLE001
            print("  SKIPPED: no Chromium for Playwright")
            return
        try:
            pg = b.new_page()
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            # 1) the window loads and runs under its CSP (app.js ran: it fills in the version pill)
            pg.goto(srv().url)
            # (arrow-function predicates: the window's CSP blocks eval, which string predicates need)
            pg.wait_for_function("() => document.getElementById('updPill').textContent.includes('Debrief')"
                                 " || !document.getElementById('vWelcome').hidden", timeout=20000)
            assert not errs, errs
            # 2) the planted page inside the window's frame: no API, no parent access
            msgs = pg.evaluate("""() => new Promise(res => {
                const got = [];
                addEventListener('message', e => { if (e.data && e.data.type === 'debrief-frame') got.push(e.data.href) });
                const f = document.getElementById('frame');
                f.src = 'lib/evil.html';
                setTimeout(() => res(got), 2500);
            })""")
            assert any(m.endswith("lib/evil.html") for m in msgs), msgs   # the bridge tells the window
            res = pg.frames[1].evaluate("[window.__res, window.__parent]")
            assert res == ["blocked", "blocked"], res
            # 3) opened on its own, too
            pg.goto(srv().url + "lib/evil.html")
            pg.wait_for_timeout(1200)
            assert pg.evaluate("window.__res") == "blocked"
        finally:
            b.close()
    assert ST.read_text(encoding="utf-8") == before   # settings untouched


# ================================================================ M2: fixed MIME types
def test_m2_registry_cannot_change_types():
    mimetypes.init()
    mimetypes.add_type("text/plain", ".js")
    mimetypes.add_type("application/x-css", ".css")
    mimetypes.add_type("text/plain", ".json")
    try:
        (LIB / "x.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
        (LIB / "d.json").write_text("{}", encoding="utf-8")
        for rel, want in (("static/app.js", "text/javascript; charset=utf-8"), ("static/app.css", "text/css; charset=utf-8"),
                          ("static/icon.png", "image/png"), ("", "text/html; charset=utf-8"),
                          ("lib/x.svg", "image/svg+xml"), ("lib/d.json", "application/json; charset=utf-8")):
            code, h, _ = get(rel)
            assert code == 200 and h["Content-Type"] == want, (rel, h["Content-Type"])
            assert h["X-Content-Type-Options"] == "nosniff"
    finally:
        mimetypes.init()


# ================================================================ M3: atomic review writes, tracked refresh
def _review_data():
    return {"meta": {"track": "T", "car": "C", "started_at": "2026-10-01T20:00:00+00:00"},
            "laps": [{"n": 1, "t": 90.0, "clean": True}], "best": {"n": 1, "t": 90.0}}


def test_m3_report_write_is_atomic():
    d = LIB / "reviews" / "2026-10-01_t_c_abcdef"
    d.mkdir(parents=True, exist_ok=True)
    report.write(d, _review_data())
    good = (d / "review.json").read_text(encoding="utf-8")
    real = fsutil.replace

    def die(src, dst):
        raise OSError(28, "No space left on device")
    fsutil.replace = die
    try:
        data = _review_data()
        data["meta"]["track"] = "Changed"
        try:
            report.write(d, data)
            raise AssertionError("expected OSError")
        except OSError:
            pass
    finally:
        fsutil.replace = real
    assert (d / "review.json").read_text(encoding="utf-8") == good      # old copy whole
    assert json.loads(good)["meta"]["track"] == "T"
    assert not [p for p in d.iterdir() if p.name.endswith(".tmp")]      # no temp files left


def test_m3_refresh_killed_mid_write_keeps_review():
    """The r7 repro: kill the process while refresh_all writes review.json. With temp file + replace,
    the original stays valid."""
    import subprocess
    lib = TMP / "killlib"
    d = lib / "reviews" / "2026-10-02_t_c_aaaaaa"
    d.mkdir(parents=True)
    (d / "review.json").write_text(json.dumps(_review_data()), encoding="utf-8")
    before = (d / "review.json").read_text(encoding="utf-8")
    child = f"""
import os, sys
sys.path.insert(0, {str(ROOT)!r})
real = os.fdopen
def dying(fd, *a, **k):
    f = real(fd, *a, **k)
    w = f.write
    def half(data):
        w(data[: len(data) // 2]); f.flush(); os._exit(0)
    f.write = half
    return f
os.fdopen = dying
from debrief import report
report.refresh_all({str(lib)!r})
"""
    subprocess.run([sys.executable, "-c", child], check=False, timeout=60)
    assert (d / "review.json").read_text(encoding="utf-8") == before
    names = [r["dir"] for g in S.ReviewIndex().list(lib) for r in g["reviews"]]
    assert d.name in names


def test_m3_refresh_is_tracked_job():
    jobs = S.Jobs()
    gate = threading.Event()
    assert jobs.run_background("refresh", lambda: gate.wait(5))
    assert jobs.busy and not jobs.running and jobs.snapshot().get("background") == ["refresh"]
    assert not jobs.run_background("refresh", lambda: None)    # never two at once
    # a job waits for the refresh to finish before it starts (no two writers in one folder)
    t0 = time.monotonic()
    threading.Timer(0.4, gate.set).start()
    started = {}
    real_run = S.Jobs._run
    S.Jobs._run = lambda self, kind, jid, params=None: started.setdefault("at", time.monotonic())
    try:
        assert jobs.start("check")["ok"]
        jobs.thread.join(2)
    finally:
        S.Jobs._run = real_run
    assert started["at"] - t0 >= 0.35
    assert not jobs.busy or jobs.running


def test_m3_watchdog_waits_for_refresh():
    s = S.DebriefServer(heartbeat_timeout=0.5, startup_grace=0)
    gate = threading.Event()
    s.jobs.run_background("refresh", lambda: gate.wait(10))
    w = threading.Thread(target=s.watchdog, daemon=True)
    w.start()
    time.sleep(2.5)
    assert not s.stopping.is_set()          # idle window, but the refresh is still writing
    gate.set()
    w.join(5)
    assert s.stopping.is_set()
    s.server_close()


def test_m3_close_for_update_blocks_jobs_and_waits():
    s = S.DebriefServer(heartbeat_timeout=3600)
    exited = threading.Event()
    s.exit = lambda code: exited.set()
    s.shutdown = lambda: None
    gate = threading.Event()
    s.jobs.run_background("refresh", lambda: gate.wait(20))
    s._close_for_update()
    assert s.jobs.closing
    r = s.jobs.start("check")
    assert not r["ok"] and "closing" in r["error"]
    time.sleep(5)
    assert not exited.is_set()               # still waiting for the refresh
    gate.set()
    assert exited.wait(3)
    s.server_close()


# ================================================================ M4: update outcome, temp cleanup
def test_m4_cleanup_old_installers():
    base = Path(tempfile.gettempdir())
    old = Path(tempfile.mkdtemp(prefix=updater.TEMP_PREFIX))
    (old / "Debrief-Setup-1.0.0.exe").write_bytes(b"x" * 1000)
    os.utime(old, (time.time() - 7200, time.time() - 7200))
    fresh = Path(tempfile.mkdtemp(prefix=updater.TEMP_PREFIX))   # an installer that may still be running
    other = Path(tempfile.mkdtemp(prefix="someone-else-"))
    try:
        assert updater.cleanup_old_downloads() >= 1
        assert not old.exists() and fresh.exists() and other.exists()
        assert all(p.parent == base for p in (fresh, other))
    finally:
        import shutil
        for p in (fresh, other):
            shutil.rmtree(p, ignore_errors=True)


def test_m4_failed_update_is_reported_next_start():
    pend = updater._pending_path()
    # no update started: nothing to say
    assert updater.pending_result() is None
    # started a moment ago (installer may still run): wait
    pend.write_text(json.dumps({"from": __version__, "to": "99.0.0", "time": time.time()}), encoding="utf-8")
    assert updater.pending_result() is None and pend.exists()
    # long ago and still the old version: it didn't finish
    pend.write_text(json.dumps({"from": __version__, "to": "99.0.0", "time": time.time() - 3600}), encoding="utf-8")
    r = updater.pending_result()
    assert r["ok"] is False and "99.0.0" in r["message"] and "fine" in r["message"] and not pend.exists()
    # now on the new version: it worked
    pend.write_text(json.dumps({"from": "1.0.0", "to": __version__, "time": time.time()}), encoding="utf-8")
    assert updater.pending_result() == {"ok": True, "version": __version__}
    # garbage file: no crash, removed
    pend.write_text("[1,2", encoding="utf-8")
    assert updater.pending_result() is None
    # the window gets the message with the update check
    s = srv()
    s.update_outcome = {"ok": False, "message": "The update to Debrief 99.0.0 didn't finish"}
    try:
        code, h, body = get("api/update", {"X-Debrief": "1"})
        j = json.loads(body)
        assert j["last_update"]["ok"] is False and "didn't finish" in j["last_update"]["message"]
    finally:
        s.update_outcome = None


def test_m4_launch_and_installer_script():
    if os.name != "nt":
        expect_update_error(lambda: updater.launch(TMP / "x.exe", "9.9.9"), "Windows")
    iss = (ROOT / "packaging" / "debrief.iss").read_text(encoding="utf-8")
    assert 'Name: "{app}\\_internal"' not in iss.split("[InstallDelete]", 1)[1].split("[Files]", 1)[0]
    assert "CheckForMutexes('DebriefRunning')" in iss and "ssPostInstall" in iss and "_internal.new" in iss
    src = (ROOT / "debrief" / "updater.py").read_text(encoding="utf-8")
    assert "/NOCLOSEAPPLICATIONS" in src and "/LOG=" in src
    assert '"DebriefRunning"' in (ROOT / "debrief" / "desktop" / "__init__.py").read_text(encoding="utf-8")


# ================================================================ L1: refused requests close the connection
def test_l1_refused_post_body_is_not_a_second_request():
    s = srv()
    inner = (f"POST /{s.secret}/api/ping HTTP/1.1\r\nHost: 127.0.0.1:{s.port}\r\nX-Debrief: 1\r\n"
             f"Content-Length: 0\r\n\r\n").encode()
    for first in ("POST /no-secret", f"POST /{s.secret}/api/ping", f"GET /{s.secret}/api/status"):
        hdr = "" if "api/ping" not in first else ""   # second case: missing X-Debrief
        outer = (f"{first} HTTP/1.1\r\nHost: 127.0.0.1:{s.port}\r\n{hdr}Content-Type: text/plain\r\n"
                 f"Content-Length: {len(inner)}\r\n\r\n").encode() + inner
        buf = raw_http(outer)
        assert buf.count(b"HTTP/1.1 ") == 1, (first, buf[:300])
        assert b"Connection: close" in buf
    # a normal POST keeps the connection usable (body read)
    ok = (f"POST /{s.secret}/api/ping HTTP/1.1\r\nHost: 127.0.0.1:{s.port}\r\nX-Debrief: 1\r\n"
          f"Content-Length: 2\r\n\r\n{{}}").encode()
    buf = raw_http(ok + ok)
    assert buf.count(b"HTTP/1.1 200") == 2, buf[:400]
    assert S.Handler.timeout == 30


# ================================================================ L2 / B1: settings file edits
def _save_with(text, updates):
    ST.write_text(text, encoding="utf-8")
    return settings_io.save(updates)


def test_l2_new_key_goes_before_tables():
    _save_with(f"library_folder = '{LIB}'\n[extra]\nfoo = 1\n", {"token": "newtoken123", "sync_on_open": False})
    cfg = app.load_settings()
    assert cfg["token"] == "newtoken123" and cfg["sync_on_open"] is False
    assert app._parse_toml(ST.read_text())["extra"] == {"foo": 1}       # the table is untouched
    _save_with(f"token = 'PASTE'\nlibrary_folder = '{LIB}'\n\n# my notes\n[extra]\nnote = 1\n", {"check_updates": False})
    assert app.load_settings()["check_updates"] is False


def test_l2_keys_inside_strings_and_tables_are_left_alone():
    _save_with(f"token = 'PASTE'\nlibrary_folder = '{LIB}'\nmotd = \"\"\"\nteam_slug = \"in-a-string\"\n\"\"\"\n"
               f"team_slug = \"real\"\n", {"team_slug": "new-team"})
    d = app._parse_toml(ST.read_text())
    assert d["team_slug"] == "new-team" and 'team_slug = "in-a-string"' in d["motd"]
    _save_with(f"library_folder = '{LIB}'\n[t]\nteam_slug = 'x'\n", {"team_slug": "top"})
    d = app._parse_toml(ST.read_text())
    assert d["team_slug"] == "top" and d["t"]["team_slug"] == "x"
    # a key whose value spans lines is replaced whole
    _save_with(f"library_folder = '{LIB}'\ntoken = '''\nold\n'''\nmax_reference_laps = 1\n", {"token": "abcdefgh123"})
    d = app._parse_toml(ST.read_text())
    assert d["token"] == "abcdefgh123" and d["max_reference_laps"] == 1


def test_l2_unsafe_edit_is_refused_not_faked():
    # a dotted key we can't edit line by line: refuse, leave the file as it was
    text = f"library_folder = '{LIB}'\ntoken = 'PASTE'\nsync_on_open.x = 1\n"
    ST.write_text(text, encoding="utf-8")
    try:
        settings_io.save({"sync_on_open": False})
        raise AssertionError("save claimed success")
    except (ValueError, SystemExit):
        pass
    assert ST.read_text(encoding="utf-8") == text


def test_l2_concurrent_saves_and_unique_temp():
    ST.write_text(f"token = 'PASTE'\nlibrary_folder = '{LIB}'\n", encoding="utf-8")
    errs = []

    def go(i):
        try:
            settings_io.save({"max_reference_laps": i % 4, "team_slug": f"team-{i}"})
        except Exception as e:  # noqa: BLE001
            errs.append(e)
    ts = [threading.Thread(target=go, args=(i,)) for i in range(12)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs, errs
    app.load_settings()
    assert not [p for p in ST.parent.iterdir() if p.name.endswith(".tmp")]
    ST.write_text(f'token = "PASTE-YOUR-GARAGE61-TOKEN-HERE"\nlibrary_folder = \'{LIB}\'\n', encoding="utf-8")


# ================================================================ L3: lock-file URL
def test_l3_lock_file_url_validated():
    good = "http://127.0.0.1:51234/" + "a" * 32 + "/"
    assert desktop.valid_lock_url(good)
    assert desktop.valid_lock_url(srv().url)
    for bad in ("https://127.0.0.1:51234/" + "a" * 32 + "/", "http://evil.example:51234/" + "a" * 32 + "/",
                "http://127.0.0.1.evil.example:80/" + "a" * 32 + "/", "http://localhost:51234/" + "a" * 32 + "/",
                "http://user@127.0.0.1:51234/" + "a" * 32 + "/", "http://127.0.0.1/" + "a" * 32 + "/",
                "http://127.0.0.1:51234/" + "a" * 31 + "/", "http://127.0.0.1:51234/" + "a" * 32 + "/x",
                "http://127.0.0.1:99999/" + "a" * 32 + "/", "http://127.0.0.1:51234/" + "a" * 32 + "/?q",
                "javascript:alert(1)", "", None, "http://127.0.0.1:51234/" + "a" * 32 + "/\nx"):
        assert not desktop.valid_lock_url(bad), bad


# ================================================================ B2-B5: updater robustness
def test_b2_future_checked_date_rechecks():
    calls = []
    requests.get = lambda url, **k: (calls.append(url), Resp(js={"tag_name": "v9.9.9", "assets": []}))[1]
    try:
        Path(os.environ["DEBRIEF_UPDATE_STATE"]).write_text(
            json.dumps({"checked": time.time() + 365 * 86400, "release": {"latest": "1.0.0"}}), encoding="utf-8")
        updater.check(now=time.time() + 3 * 86400)
        assert len(calls) == 1
        updater.check(now=time.time() + 3 * 86400 + 60)   # the fresh answer is used again
        assert len(calls) == 1
    finally:
        requests.get = _blocked


def test_b3_corrupt_checked_value():
    for bad in ("yesterday", None, [1], True, float("nan")):
        Path(os.environ["DEBRIEF_UPDATE_STATE"]).write_text(
            json.dumps({"checked": bad, "release": {"latest": "1.0"}}), encoding="utf-8")
        try:
            updater.check()
        except updater.UpdateError:
            pass    # offline in tests: it asked GitHub again, which is right
    Path(os.environ["DEBRIEF_UPDATE_STATE"]).write_text(
        json.dumps({"checked": "yesterday", "release": {"latest": "1"}}), encoding="utf-8")
    code, h, body = get("api/update?auto=1", {"X-Debrief": "1"})
    assert b"could not convert" not in body and b"float" not in body
    j = json.loads(body)
    assert j["ok"] is False and "connection" in j["error"].lower()


def test_b4_non_object_json():
    for js in (None, [], [1, 2], "x", 3, ValueError("bad json"), {"assets": "nope"}, {"assets": [1, None]}):
        requests.get = lambda url, js=js, **k: Resp(js=js)
        try:
            info = updater.check(force=True)
            assert isinstance(js, dict) and info["newer"] is False
        except updater.UpdateError:
            pass
        finally:
            requests.get = _blocked


def test_b5_odd_size_and_names():
    requests.get = lambda *a, **k: Resp(body=b"x")
    try:
        for size in ("abc", None, -5, 10 ** 30, [1]):
            info = {"installer": DL + "Debrief-Setup-9.9.9.exe", "sha256": "0" * 64, "size": size,
                    "installer_name": "Debrief-Setup-9.9.9.exe", "latest": "9.9.9"}
            expect_update_error(lambda: updater.download(info, dest_dir=TMP / "b5"), "checksum")
        # a name with a path in it, or not an installer name: refused
        expect_update_error(lambda: updater.download({"installer": DL + "x", "sha256": "0" * 64,
                                                      "installer_name": "..\\..\\evil.exe"}), "no installer")
    finally:
        requests.get = _blocked


# ================================================================ B6: schedule temp folder
def test_b6_failed_schedule_download_leaves_no_temp_dir():
    import glob
    before = set(glob.glob(tempfile.gettempdir() + "/debrief-sched-*"))
    try:
        S._run_schedule({}, lambda f, m: None)
        raise AssertionError("expected SystemExit")
    except SystemExit as e:
        assert "connection" in str(e).lower() or "reach" in str(e).lower()
    assert set(glob.glob(tempfile.gettempdir() + "/debrief-sched-*")) - before == set()


# ================================================================ L6: upload refused before reading
def test_l6_upload_refused_while_busy_without_reading():
    s = srv()
    gate = threading.Event()
    with s.jobs.lock:
        s.jobs.st = {"state": "running", "kind": "check", "id": 999, "title": "Checking the connection"}
    try:
        n = 5_000_000
        head = (f"POST /{s.secret}/api/schedule-upload HTTP/1.1\r\nHost: 127.0.0.1:{s.port}\r\nX-Debrief: 1\r\n"
                f"Content-Type: application/pdf\r\nContent-Length: {n}\r\n\r\n%PDF-").encode()
        t0 = time.monotonic()
        buf = raw_http(head, timeout=3)
        assert b"to finish first" in buf and b"Connection: close" in buf
        assert time.monotonic() - t0 < 2.5
    finally:
        with s.jobs.lock:
            s.jobs.st = {"state": "idle"}
        gate.set()


# ================================================================ L8: longer lock retries
def test_l8_lock_retry_budget():
    total = sum(fsutil._delay(i) for i in range(fsutil.RETRIES - 1))
    assert 4.0 <= total <= 7.0, total
    tries = []
    real = os.replace

    def flaky(a, b):
        tries.append(1)
        if len(tries) < 7:     # locked for ~1.5 s, like OneDrive
            raise PermissionError(13, "locked")
        return real(a, b)
    a, b = TMP / "l8a", TMP / "l8b"
    a.write_text("x")
    os.replace = flaky
    try:
        fsutil.replace(a, b)
    finally:
        os.replace = real
    assert b.read_text() == "x" and len(tries) == 7


# ================================================================ lead additions
def test_layout_bumped_for_new_review_page():
    # at least the 2.2.2 layout (2.3 bumps it again; test_23_d pins the current value)
    assert tuple(int(x) for x in report.LAYOUT.split(".")) >= (2, 2, 2)


def test_lib_csp_blocks_network_like_templates():
    code, h, _ = get("lib/data.json") if (LIB / "data.json").exists() else (None, None, None)
    (LIB / "n.html").write_text("<html><body>x</body></html>", encoding="utf-8")
    code, h, _ = get("lib/n.html")
    csp = h["Content-Security-Policy"]
    assert csp.startswith("sandbox ") and "connect-src 'none'" in csp and "object-src 'none'" in csp


def test_salutation_setting():
    ST.write_text(f"token = 'PASTE'\nlibrary_folder = '{LIB}'\n", encoding="utf-8")
    assert settings_io.public_view()["salutation"] == ""
    v = settings_io.save({"salutation": "  sir  "})
    assert v["salutation"] == "sir" and app.load_settings()["salutation"] == "sir"
    assert settings_io.save({"salutation": ""})["salutation"] == ""
    for bad in ("x" * 21, 5, "<b>", "a\x00"):
        try:
            settings_io.save({"salutation": bad})
            raise AssertionError(f"accepted {bad!r}")
        except ValueError:
            pass
    # a hand-edited odd value reads as none
    ST.write_text(f"token = 'PASTE'\nlibrary_folder = '{LIB}'\nsalutation = 7\n", encoding="utf-8")
    assert settings_io.public_view()["salutation"] == ""
    assert 'salutation = ""' in (ROOT / "settings.example.toml").read_text(encoding="utf-8")
    ST.write_text(f'token = "PASTE-YOUR-GARAGE61-TOKEN-HERE"\nlibrary_folder = \'{LIB}\'\n', encoding="utf-8")


def test_notes_conflict_flag():
    import types
    real = app.optional_module

    class Conflict(ValueError):
        pass

    def fake_module(kind):
        def save(folder, card):
            if kind == "cls":
                raise Conflict("This track card was changed somewhere else after you opened it.")
            if kind == "msg":
                raise ValueError("The card changed since you opened it.")
            raise ValueError("Pick a track.")
        return types.SimpleNamespace(Conflict=Conflict, clean=lambda c: c or {}, save=save)
    try:
        for kind, want in (("cls", True), ("msg", True), ("plain", None)):
            app.optional_module = lambda name, kind=kind: fake_module(kind) if name == "notes" else real(name)
            code, j = post("api/notes", {"card": {"track": "T"}})
            assert code == 400 and j["ok"] is False and j.get("conflict") is want, (kind, j)
    finally:
        app.optional_module = real


# ================================================================ selftest covers the new checks
def test_selftest_checks_types_and_csp():
    src = (ROOT / "debrief" / "desktop" / "__init__.py").read_text(encoding="utf-8")
    for k in ('"type:app.js"', '"csp:page"', '"csp:lib"'):
        assert k in src


if __name__ == "__main__":
    failed = 0
    tests = [(n, f) for n, f in list(globals().items()) if n.startswith("test_") and callable(f)]
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
    if "s" in _SRV:
        _SRV["s"].shutdown()
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
