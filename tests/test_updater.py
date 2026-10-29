"""Built-in updater tests: version compare, release parsing, the daily check, and the checksum gate.

    python3 tests/test_updater.py

Never contacts GitHub: requests.get is replaced with a fake.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="debrief-upd-test-"))
os.environ["DEBRIEF_UPDATE_STATE"] = str(TMP / "update.json")

import requests  # noqa: E402
from debrief import updater, __version__  # noqa: E402

PAYLOAD = b"MZ fake installer bytes " * 4000
SHA = hashlib.sha256(PAYLOAD).hexdigest()
DL = "https://github.com/blackmagik232/Debrief/releases/download/v9.9.9/"


class Resp:
    def __init__(self, status=200, body=b"", js=None):
        self.status_code, self._b, self._js = status, body, js
        self.text = body.decode("latin-1") if isinstance(body, bytes) else body

    def json(self):
        return self._js

    def iter_content(self, n):
        for i in range(0, len(self._b), n):
            yield self._b[i:i + n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def release(tag="v9.9.9", with_sha=True, digest=None, size=None):
    assets = [{"name": f"Debrief-Setup-{tag[1:]}.exe", "browser_download_url": DL + f"Debrief-Setup-{tag[1:]}.exe",
               "size": len(PAYLOAD) if size is None else size, **({"digest": digest} if digest else {})}]
    if with_sha:
        assets.append({"name": f"Debrief-Setup-{tag[1:]}.exe.sha256",
                       "browser_download_url": DL + f"Debrief-Setup-{tag[1:]}.exe.sha256"})
    return {"tag_name": tag, "html_url": "https://github.com/x", "body": "notes", "assets": assets}


calls = []


def fake_get(rel, payload=PAYLOAD, sha_text=None):
    def get(url, **kw):
        calls.append(url)
        if url == updater.API:
            return Resp(js=rel)
        if url.endswith(".sha256"):
            return Resp(body=(sha_text if sha_text is not None else f"{SHA}  Debrief-Setup.exe").encode())
        return Resp(body=payload)
    return get


def test_version_compare():
    assert updater.newer("2.2.1", "2.2.0") and updater.newer("2.10.0", "2.9.9") and updater.newer("v3", "2.9")
    assert not updater.newer("2.2.0", "2.2.0") and not updater.newer("2.1.9", "2.2.0")


def test_check_and_daily_cache():
    os.environ["DEBRIEF_UPDATE_STATE"] = str(TMP / "u1.json")
    calls.clear()
    real = requests.get
    requests.get = fake_get(release())
    try:
        a = updater.check(now=1000.0)
        assert a["newer"] and a["latest"] == "9.9.9" and a["current"] == __version__ and a["installer"].startswith(DL)
        updater.check(now=1000.0 + 3600)            # within a day: no new request
        assert calls.count(updater.API) == 1
        updater.check(now=1000.0 + 25 * 3600)       # a day later: asks again
        assert calls.count(updater.API) == 2
        updater.check(force=True, now=1000.0 + 25 * 3600)
        assert calls.count(updater.API) == 3
    finally:
        requests.get = real


def test_download_checks_sha():
    real = requests.get
    try:
        requests.get = fake_get(release())
        info = updater.check(force=True)
        p = updater.download(info, dest_dir=TMP / "d1")
        assert p.read_bytes() == PAYLOAD
        # a bad checksum is refused and the file removed
        requests.get = fake_get(release(), sha_text="0" * 64)
        try:
            updater.download(info, dest_dir=TMP / "d2")
            raise AssertionError("bad checksum accepted")
        except updater.UpdateError as e:
            assert "checksum" in str(e)
        assert not (TMP / "d2" / info["installer_name"]).exists()
        # no checksum at all: refused
        requests.get = fake_get(release(with_sha=False))
        info2 = updater.check(force=True)
        try:
            updater.download(info2, dest_dir=TMP / "d3")
            raise AssertionError("missing checksum accepted")
        except updater.UpdateError:
            pass
        # GitHub's own asset digest works without the .sha256 file
        requests.get = fake_get(release(with_sha=False, digest="sha256:" + SHA))
        info3 = updater.check(force=True)
        assert updater.download(info3, dest_dir=TMP / "d4").read_bytes() == PAYLOAD
        # a short download is refused
        requests.get = fake_get(release(), payload=PAYLOAD[:-10])
        try:
            updater.download(updater.check(force=True), dest_dir=TMP / "d5")
            raise AssertionError("short download accepted")
        except updater.UpdateError:
            pass
        # only installers from this repository's releases
        try:
            updater.download(dict(info, installer="https://evil.example/x.exe"), dest_dir=TMP / "d6")
            raise AssertionError("foreign URL accepted")
        except updater.UpdateError:
            pass
    finally:
        requests.get = real


def test_not_newer_and_errors():
    real = requests.get
    try:
        requests.get = fake_get(release(tag="v" + __version__))
        assert not updater.check(force=True)["newer"]
        requests.get = lambda url, **kw: Resp(status=404)
        try:
            updater.check(force=True)
            raise AssertionError("404 not reported")
        except updater.UpdateError as e:
            assert "private" in str(e)

        def boom(url, **kw):
            raise requests.ConnectionError("offline")
        requests.get = boom
        try:
            updater.check(force=True)
            raise AssertionError("offline not reported")
        except updater.UpdateError:
            pass
    finally:
        requests.get = real


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
    sys.exit(1 if failed else 0)
