"""2.5 workstream B: Discord via webhook, and signed releases.

    python3 tests/test_25_b.py

Never contacts Discord or GitHub: the updater gets a fake requests.get, and discord.py gets a fake
session. requests.get/post are replaced with functions that raise, so a stray real call fails loudly.
"""
from __future__ import annotations

import base64
import faulthandler
import hashlib
import io
import json
import logging
import os
import re
import struct
import subprocess
import sys
import tempfile
import traceback
import zlib
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

faulthandler.dump_traceback_later(300, exit=True)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packaging"))
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
TMP = Path(tempfile.mkdtemp(prefix="debrief-25b-"))
os.environ["DEBRIEF_UPDATE_STATE"] = str(TMP / "update.json")
FIX = ROOT / "tests" / "fixtures_25_b"

import requests  # noqa: E402


def _blocked(*a, **k):
    raise requests.ConnectionError("network blocked in tests")


requests.get = _blocked
requests.post = _blocked

from debrief import discord, ed25519, updater  # noqa: E402
import sign_release  # noqa: E402

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
    from cryptography.hazmat.primitives import serialization
    HAVE_CRYPTO = True
except ImportError:
    HAVE_CRYPTO = False

# ================================================================ RFC 8032 section 7.1 test vectors
# Copied from RFC 8032, "7.1. Test Vectors for Ed25519" (TEST 1, TEST 2, TEST 3, TEST SHA(abc)),
# in the RFC's own layout. The parser below joins the hex lines.
RFC8032_7_1 = """
-----TEST 1

ALGORITHM:
Ed25519

SECRET KEY:
9d61b19deffd5a60ba844af492ec2cc4
4449c5697b326919703bac031cae7f60

PUBLIC KEY:
d75a980182b10ab7d54bfed3c964073a
0ee172f3daa62325af021a68f707511a

MESSAGE (length 0 bytes):

SIGNATURE:
e5564300c360ac729086e2cc806e828a
84877f1eb8e5d974d873e06522490155
5fb8821590a33bacc61e39701cf9b46b
d25bf5f0595bbe24655141438e7a100b

-----TEST 2

ALGORITHM:
Ed25519

SECRET KEY:
4ccd089b28ff96da9db6c346ec114e0f
5b8a319f35aba624da8cf6ed4fb8a6fb

PUBLIC KEY:
3d4017c3e843895a92b70aa74d1b7ebc
9c982ccf2ec4968cc0cd55f12af4660c

MESSAGE (length 1 byte):
72

SIGNATURE:
92a009a9f0d4cab8720e820b5f642540
a2b27b5416503f8fb3762223ebdb69da
085ac1e43e15996e458f3613d0f11d8c
387b2eaeb4302aeeb00d291612bb0c00

-----TEST 3

ALGORITHM:
Ed25519

SECRET KEY:
c5aa8df43f9f837bedb7442f31dcb7b1
66d38535076f094b85ce3a2e0b4458f7

PUBLIC KEY:
fc51cd8e6218a1a38da47ed00230f058
0816ed13ba3303ac5deb911548908025

MESSAGE (length 2 bytes):
af82

SIGNATURE:
6291d657deec24024827e69c3abe01a3
0ce548a284743a445e3680d7db5ac3ac
18ff9b538d16f290ae67f760984dc659
4a7c15e9716ed28dc027beceea1ec40a

-----TEST SHA(abc)

ALGORITHM:
Ed25519

SECRET KEY:
833fe62409237b9d62ec77587520911e
9a759cec1d19755b7da901b96dca3d42

PUBLIC KEY:
ec172b93ad5e563bf4932c70e1245034
c35467ef2efd4d64ebf819683467e2bf

MESSAGE (length 64 bytes):
ddaf35a193617abacc417349ae204131
12e6fa4e89a97ea20a9eeee64b55d39a
2192992a274fc1a836ba3c23a3feebbd
454d4423643ce80e2a9ac94fa54ca49f

SIGNATURE:
dc2a4459e7369633a52b1bf277839a00
201009a3efbf3ecb69bea2186c26b589
09351fc9ac90b3ecfdfbc7c66431e030
3dca179c138ac17ad9bef1177331a704
"""


def _parse_rfc(text):
    out = []
    for block in text.split("-----TEST ")[1:]:
        name, _, body = block.partition("\n")
        fields, cur = {}, None
        for line in body.splitlines():
            line = line.strip()
            if line.endswith(":") or line.startswith("MESSAGE ("):
                cur = line.split(" (")[0].rstrip(":")
                fields[cur] = ""
            elif cur and line:
                fields[cur] += line
        out.append((name.strip(), bytes.fromhex(fields["SECRET KEY"]), bytes.fromhex(fields["PUBLIC KEY"]),
                    bytes.fromhex(fields["MESSAGE"]), bytes.fromhex(fields["SIGNATURE"])))
    return out


VECTORS = _parse_rfc(RFC8032_7_1)

# ================================================================ test keys (NEVER the release key)
SEED1 = bytes.fromhex("35be258c12aef5cd9cb0798e0c6f51094d7ea86fb74124f38f921865467f7655")  # sha256("Debrief 2.5 test key 1")
SEED2 = bytes.fromhex("4cec410e9468c1b9778b4970fa53eaae206fc5173f9001ba85e4fcea61549ae4")  # sha256("Debrief 2.5 test key 2")
if HAVE_CRYPTO:   # made at test time from the fixed seeds
    def _pub(seed):
        return Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    PUB1, PUB2 = _pub(SEED1), _pub(SEED2)
else:             # pasted once (generated with cryptography 50.0.1)
    PUB1 = bytes.fromhex("1fcd064ec0818dd48b0d7f7988e5535003a0eb023d8be153609a0626a4de4971")
    PUB2 = bytes.fromhex("61a960c34f4599d6f268c00a9d859e8f92184894c1b653ca70b2fcfd6934712b")

DL = "https://github.com/blackmagik232/Debrief/releases/download/v9.9.9/"
PAYLOAD = b"MZ fake installer bytes " * 3000
SHA = hashlib.sha256(PAYLOAD).hexdigest()
# pasted once: SEED1's signature over signed_message("9.9.9", "Debrief-Setup-9.9.9.exe", SHA)
SIG_PASTED = bytes.fromhex("9aaa4e3b142949ad4245150c3149a16a12f545ff29f5cf3dfd4cd6133229a333"
                           "d2c51f36c0dd86ab5382acc276e11c28974dfdf90d624107abc2bf7db5757501")


def sig_for(version="9.9.9", name="Debrief-Setup-9.9.9.exe", payload=PAYLOAD, seed=SEED1):
    if HAVE_CRYPTO:
        return Ed25519PrivateKey.from_private_bytes(seed).sign(
            updater.signed_message(version, name, hashlib.sha256(payload).hexdigest()))
    return sign_release.sign(seed, updater.signed_message(version, name, hashlib.sha256(payload).hexdigest()))


# ================================================================ fake GitHub for the updater
class Resp:
    def __init__(self, status=200, body=b"", js=None, headers=None):
        self.status_code, self._b, self._js = status, body, js
        self.headers = headers or {}
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


def release(tag="v9.9.9", name=None, payload=PAYLOAD, with_sig=True, with_sha=True, digest=True):
    nm = name or f"Debrief-Setup-{tag[1:]}.exe"
    exe = {"name": nm, "browser_download_url": DL + nm, "size": len(payload)}
    if digest:
        exe["digest"] = "sha256:" + hashlib.sha256(payload).hexdigest()
    assets = [exe]
    if with_sha:
        assets.append({"name": nm + ".sha256", "browser_download_url": DL + nm + ".sha256"})
    if with_sig:
        assets.append({"name": nm + ".sig", "browser_download_url": DL + nm + ".sig"})
    return {"tag_name": tag, "html_url": "https://github.com/x", "body": "", "assets": assets}


CALLS = []


def fake(rel, payload=PAYLOAD, sig_text="", sig_status=200, sha_text=None):
    def get(url, **kw):
        CALLS.append(url)
        if url == updater.API:
            return Resp(js=rel)
        if url.endswith(".sig"):
            return Resp(status=sig_status, body=sig_text.encode() if isinstance(sig_text, str) else sig_text)
        if url.endswith(".sha256"):
            return Resp(body=(sha_text if sha_text is not None else hashlib.sha256(payload).hexdigest()).encode())
        return Resp(body=payload)
    return get


class Keys:
    """Temporarily set RELEASE_PUBKEYS (and RELEASE_PUBLIC_KEY)."""

    def __init__(self, keys=(), single=None):
        self.keys, self.single = keys, single

    def __enter__(self):
        self.old = (updater.RELEASE_PUBKEYS, updater.RELEASE_PUBLIC_KEY)
        updater.RELEASE_PUBKEYS, updater.RELEASE_PUBLIC_KEY = self.keys, self.single

    def __exit__(self, *a):
        updater.RELEASE_PUBKEYS, updater.RELEASE_PUBLIC_KEY = self.old


_N = [0]


def dl(rel, **kw):
    """check + download against the fake; returns the path or raises UpdateError."""
    _N[0] += 1
    dest = TMP / f"dl{_N[0]}"
    requests.get = fake(rel, **kw)
    try:
        info = updater.check(force=True)
        return updater.download(info, dest_dir=dest), dest
    finally:
        requests.get = _blocked


def refused(rel, contains, **kw):
    _N[0] += 1
    dest = TMP / f"dl{_N[0]}"
    requests.get = fake(rel, **kw)
    try:
        info = updater.check(force=True)
        try:
            updater.download(info, dest_dir=dest)
        except updater.UpdateError as e:
            assert contains in str(e), str(e)
            assert not any(dest.glob("*.exe")), "a refused download was left on disk"
            return str(e)
        raise AssertionError("not refused: " + contains)
    finally:
        requests.get = _blocked


# ================================================================ ed25519
def test_rfc8032_vectors_verify():
    assert [v[0] for v in VECTORS] == ["1", "2", "3", "SHA(abc)"]
    assert VECTORS[3][3] == hashlib.sha512(b"abc").digest()   # the SHA(abc) message is what it says
    for name, sk, pk, msg, sig in VECTORS:
        assert len(sk) == 32 and len(pk) == 32 and len(sig) == 64, name
        assert ed25519.verify(pk, msg, sig), name
        assert updater.ed25519_verify(pk, msg, sig), name          # 2.2.2 alias
        assert sign_release.public_key(sk) == pk, name
        assert sign_release.sign(sk, msg) == sig, name
        assert sign_release._sign_pure(sk, msg) == sig, name
        if HAVE_CRYPTO:   # cross-check the copied vectors against an independent implementation
            k = Ed25519PrivateKey.from_private_bytes(sk)
            assert k.sign(msg) == sig, name
            Ed25519PublicKey.from_public_bytes(pk).verify(sig, msg)


def test_ed25519_rejects_tampering():
    for name, sk, pk, msg, sig in VECTORS:
        for i in (0, 17, 31, 32, 50, 63):
            for bit in (0, 7):
                bad = bytearray(sig)
                bad[i] ^= 1 << bit
                assert not ed25519.verify(pk, msg, bytes(bad)), (name, i, bit)
        badpk = bytearray(pk)
        badpk[3] ^= 1
        assert not ed25519.verify(bytes(badpk), msg, sig), name
        assert not ed25519.verify(pk, msg + b"\x00", sig), name
        if msg:
            badm = bytearray(msg)
            badm[0] ^= 0x80
            assert not ed25519.verify(pk, bytes(badm), sig), name


def test_ed25519_s_at_or_over_L_fails():
    name, sk, pk, msg, sig = VECTORS[1]
    S = int.from_bytes(sig[32:], "little")
    assert not ed25519.verify(pk, msg, sig[:32] + (S + ed25519.L).to_bytes(32, "little"))   # same maths, non-canonical
    assert not ed25519.verify(pk, msg, sig[:32] + ed25519.L.to_bytes(32, "little"))
    assert not ed25519.verify(pk, msg, sig[:32] + b"\xff" * 32)


def test_ed25519_wrong_lengths_and_types_fail():
    name, sk, pk, msg, sig = VECTORS[0]
    for p, s in ((pk[:31], sig), (pk + b"\x00", sig), (pk, sig[:63]), (pk, sig + b"\x00"), (b"", b"")):
        assert not ed25519.verify(p, msg, s)
    assert not ed25519.verify(None, msg, sig) and not ed25519.verify(pk, msg, None)
    assert not ed25519.verify(pk, None, sig)


def test_ed25519_non_canonical_points_fail():
    # y = 1 (the identity, x = 0) encoded as y + p: same point, non-canonical encoding
    noncanon = (1 + ed25519.P).to_bytes(32, "little")
    assert ed25519.point_decode((1).to_bytes(32, "little")) is not None
    assert ed25519.point_decode(noncanon) is None
    # x = 0 with the sign bit set
    assert ed25519.point_decode(((1 << 255) | 1).to_bytes(32, "little")) is None
    # y >= p in the signature's R
    name, sk, pk, msg, sig = VECTORS[0]
    assert not ed25519.verify(pk, msg, b"\xff" * 31 + b"\x7f" + sig[32:])
    assert not ed25519.verify(noncanon, msg, sig)
    # a y with no x on the curve
    assert not ed25519.verify((2).to_bytes(32, "little"), msg, sig)


def test_ed25519_module_has_no_dependencies():
    src = (ROOT / "debrief" / "ed25519.py").read_text(encoding="utf-8")
    imports = set(re.findall(r"^\s*(?:from|import)\s+([\w.]+)", src, re.M))
    assert imports <= {"__future__", "hashlib"}, imports


def test_no_cryptography_at_runtime():
    for p in (ROOT / "debrief").rglob("*.py"):
        src = p.read_text(encoding="utf-8")
        assert not re.search(r"^\s*(from|import)\s+cryptography", src, re.M), p
    spec = (ROOT / "packaging" / "debrief.spec").read_text(encoding="utf-8")
    assert "cryptography" in spec   # still in the PyInstaller excludes


# ================================================================ the signed message
def test_signed_message_v1_format():
    m = updater.signed_message("2.5.0", "Debrief-Setup-2.5.0.exe", "AB" * 32)
    assert m == b"Debrief release v1\n2.5.0\nDebrief-Setup-2.5.0.exe\n" + b"ab" * 32 + b"\n"
    # the 2.2.2 call form gives the same v1 message (the version comes from the name)
    assert updater.signed_message("Debrief-Setup-2.5.0.exe", "ab" * 32) == m
    for bad in (("2.5.0\n", "Debrief-Setup-2.5.0.exe", "ab" * 32), ("2.5.0", "a\nb.exe", "ab" * 32),
                ("2.5.0", "Debrief-Setup-2.5.0.exe", "zz" * 32), ("", "Debrief-Setup-2.5.0.exe", "ab" * 32)):
        try:
            updater.signed_message(*bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    fx = json.loads((FIX / "sample_release_signing.json").read_text(encoding="utf-8"))
    assert updater.signed_message("9.9.9", "Debrief-Setup-9.9.9.exe", SHA) == fx["message_v1"].encode()
    assert ed25519.verify(bytes.fromhex(fx["test_public_key"]), fx["message_v1"].encode(),
                          bytes.fromhex(fx["test_signature"]))


def test_pasted_test_signature():
    assert PUB1.hex() == "1fcd064ec0818dd48b0d7f7988e5535003a0eb023d8be153609a0626a4de4971"
    assert sig_for() == SIG_PASTED
    assert ed25519.verify(PUB1, updater.signed_message("9.9.9", "Debrief-Setup-9.9.9.exe", SHA), SIG_PASTED)


def test_shipped_key_constants():
    assert isinstance(updater.RELEASE_PUBKEYS, tuple)
    for k in updater.RELEASE_PUBKEYS:
        assert re.fullmatch(r"[0-9a-f]{64}", k), "RELEASE_PUBKEYS must hold 64-hex public keys"
    assert updater.RELEASE_PUBLIC_KEY is None or re.fullmatch(r"[0-9a-fA-F]{64}", updater.RELEASE_PUBLIC_KEY)
    assert PUB1.hex() not in updater.RELEASE_PUBKEYS and PUB2.hex() not in updater.RELEASE_PUBKEYS


def test_release_keys_parsing():
    with Keys((PUB1.hex(), PUB2.hex().upper())):
        assert updater.release_keys() == [PUB1, PUB2]
    with Keys(PUB1.hex()):                      # ("abc") without the comma is a plain str
        assert updater.release_keys() == [PUB1]
    with Keys((), PUB1.hex()):                  # 2.2.2 alias alone
        assert updater.release_keys() == [PUB1]
    with Keys((PUB1.hex(),), PUB1.hex()):       # both, same key: once
        assert updater.release_keys() == [PUB1]
    with Keys((), None):
        assert updater.release_keys() == []
    with Keys((PUB1.hex(), "nothex")):
        try:
            updater.release_keys()
            raise AssertionError("a broken key must refuse")
        except updater.UpdateError as e:
            assert "broken update key" in str(e)


# ================================================================ updater: no key set (today's behaviour)
def test_no_key_installs_on_sha_alone():
    with Keys((), None):
        p, _ = dl(release(with_sig=False))
        assert p.read_bytes() == PAYLOAD
        p, _ = dl(release(with_sig=False, digest=False))          # from the .sha256 file
        assert p.read_bytes() == PAYLOAD
        # NOT "Updates are switched off": the lead's decision #1
        refused(release(with_sig=False, with_sha=False, digest=False), "no checksum")
        refused(release(with_sig=False, digest=False), "didn't match its checksum", sha_text="0" * 64)
        CALLS.clear()
        dl(release())                                              # a .sig is ignored without a key
        assert not any(u.endswith(".sig") for u in CALLS)


# ================================================================ updater: key set
def test_valid_signature_installs():
    with Keys((PUB1.hex(),)):
        CALLS.clear()
        p, _ = dl(release(), sig_text=sig_for().hex() + "\n")
        assert p.read_bytes() == PAYLOAD
        sig_i = next(i for i, u in enumerate(CALLS) if u.endswith(".sig"))
        exe_i = next(i for i, u in enumerate(CALLS) if u.endswith(".exe"))
        assert sig_i < exe_i, "the .sig is fetched before the installer"
        assert sum(u.endswith(".sig") for u in CALLS) == 1          # +1 GitHub request per update
        # the 2.2.2 writer's line ("<hex>  name") and a base64 line both work
        p, _ = dl(release(), sig_text=sig_for().hex() + "  Debrief-Setup-9.9.9.exe\n")
        assert p.exists()
        b64 = base64.b64encode(sig_for()).decode()
        assert len(b64) == 88
        p, _ = dl(release(), sig_text=b64 + "\n")
        assert p.exists()
        # with a key, the signature is the check: a release without a published checksum still installs
        p, _ = dl(release(with_sha=False, digest=False), sig_text=sig_for().hex())
        assert p.exists()


def test_missing_or_unreadable_sig_refuses():
    with Keys((PUB1.hex(),)):
        msg = refused(release(with_sig=False), "isn't signed")
        assert msg == updater.NOT_SIGNED
        assert "Debrief's release key" in msg and "release page" in msg
        refused(release(), "isn't signed", sig_text="", sig_status=404)
        refused(release(), "isn't signed", sig_text="not a signature")
        refused(release(), "isn't signed", sig_text="A" * 87)


def test_bad_signature_refuses():
    with Keys((PUB1.hex(),)):
        refused(release(), "signature doesn't match", sig_text=sig_for(seed=SEED2).hex())   # other key
        flipped = bytearray(sig_for())
        flipped[10] ^= 4
        refused(release(), "signature doesn't match", sig_text=bytes(flipped).hex())
        # attacker installer whose digest is its own hash, with the real release's signature
        evil = b"MZ attacker-built installer " * 1000
        refused(release(payload=evil), "signature doesn't match", payload=evil, sig_text=sig_for().hex())


def test_downgrade_refused_once_key_set():
    """An older signed release can't be served as a newer one."""
    old = b"MZ old installer 9.9.8 " * 2000
    with Keys((PUB1.hex(),)):
        old_sig = sig_for("9.9.8", "Debrief-Setup-9.9.8.exe", payload=old).hex()
        # the old installer renamed to the new version's name: the version in the message differs
        refused(release(tag="v9.9.9", payload=old), "signature doesn't match", payload=old, sig_text=old_sig)
        # the old installer under its own name but a newer tag
        refused(release(tag="v9.9.9", name="Debrief-Setup-9.9.8.exe", payload=old), "doesn't match its version",
                payload=old, sig_text=old_sig)
        # a signature for the same name but another version (v1 binds the version, not only the name)
        refused(release(), "signature doesn't match", sig_text=sig_for("9.9.8", "Debrief-Setup-9.9.9.exe").hex())


def test_wrong_name_refused_once_key_set():
    with Keys((PUB1.hex(),)):
        refused(release(), "signature doesn't match", sig_text=sig_for("9.9.9", "Debrief-Setup-9.9.8.exe").hex())
        refused(release(), "signature doesn't match", sig_text=sig_for("9.9.9", "Other-Setup-9.9.9.exe").hex())


def test_second_of_two_keys_installs():
    with Keys((PUB2.hex(), PUB1.hex())):
        p, _ = dl(release(), sig_text=sig_for(seed=SEED1).hex())
        assert p.read_bytes() == PAYLOAD
    with Keys((PUB1.hex(), PUB2.hex())):
        p, _ = dl(release(), sig_text=sig_for(seed=SEED2).hex())
        assert p.read_bytes() == PAYLOAD


def test_release_public_key_alias_still_works():
    with Keys((), PUB1.hex()):
        p, _ = dl(release(), sig_text=sig_for().hex())
        assert p.exists()
        refused(release(with_sig=False), "isn't signed")


def test_signed_but_checksum_mismatch_refuses():
    with Keys((PUB1.hex(),)):
        rel = release()
        rel["assets"][0]["digest"] = "sha256:" + "0" * 64
        refused(rel, "didn't match its checksum", sig_text=sig_for().hex())


def test_broken_key_refuses_every_update():
    with Keys(("nothex",)):
        refused(release(), "broken update key", sig_text=sig_for().hex())


# ================================================================ sign_release.py
def _run(argv, env_key=None):
    old = os.environ.get("DEBRIEF_SIGNING_KEY")
    if env_key is None:
        os.environ.pop("DEBRIEF_SIGNING_KEY", None)
    else:
        os.environ["DEBRIEF_SIGNING_KEY"] = env_key
    out, err = io.StringIO(), io.StringIO()
    code = None
    try:
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = sign_release.main(argv)
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else str(e.code)
    finally:
        if old is None:
            os.environ.pop("DEBRIEF_SIGNING_KEY", None)
        else:
            os.environ["DEBRIEF_SIGNING_KEY"] = old
    return code, out.getvalue() + err.getvalue()


def test_sign_release_sign_writes_sig_and_sha_and_selfchecks():
    d = TMP / "sign1"
    d.mkdir()
    exe = d / "Debrief-Setup-9.9.9.exe"
    exe.write_bytes(PAYLOAD)
    with Keys((PUB1.hex(),)):
        code, out = _run(["sign", str(exe), "--version", "9.9.9"], SEED1.hex())
    assert code == 0, out
    assert SEED1.hex() not in out and PUB1.hex() in out
    line = (d / "Debrief-Setup-9.9.9.exe.sig").read_text(encoding="ascii")
    assert re.fullmatch(r"[0-9a-f]{128}\n", line)
    sig = bytes.fromhex(line.strip())
    assert sig == SIG_PASTED
    assert (d / "Debrief-Setup-9.9.9.exe.sha256").read_text(encoding="ascii") == f"{SHA}  Debrief-Setup-9.9.9.exe"
    # and the updater accepts exactly these files
    with Keys((PUB1.hex(),)):
        p, _ = dl(release(), sig_text=line)
        assert p.exists()


def test_sign_release_refusals_never_show_the_key():
    d = TMP / "sign2"
    d.mkdir()
    exe = d / "Debrief-Setup-9.9.9.exe"
    exe.write_bytes(PAYLOAD)
    with Keys((PUB2.hex(),)):   # the app's key isn't the signing key
        code, out = _run(["sign", str(exe), "--version", "9.9.9"], SEED1.hex())
    assert code != 0 and "doesn't match" in str(code) + out and SEED1.hex() not in str(code) + out
    assert not (d / "Debrief-Setup-9.9.9.exe.sig").exists()
    with Keys(()):
        code, out = _run(["sign", str(exe), "--version", "9.9.8"], SEED1.hex())   # wrong version
        assert code != 0 and "doesn't match" in str(code)
        code, out = _run(["sign", str(exe)], None)                                # no key
        assert code != 0 and "isn't set" in str(code)
        secretish = "x" + SEED1.hex()[1:]
        code, out = _run(["sign", str(exe)], secretish)                           # garbage key
        assert code != 0 and secretish not in str(code) + out
        bad = d / "Setup.exe"
        bad.write_bytes(b"x")
        code, out = _run(["sign", str(bad)], SEED1.hex())
        assert code != 0 and "isn't named like" in str(code)
        # 2.2.2 form still works (version from the name)
        code, out = _run([str(exe)], SEED1.hex())
        assert code == 0 and SEED1.hex() not in out
        assert bytes.fromhex((d / "Debrief-Setup-9.9.9.exe.sig").read_text(encoding="ascii").strip()) == SIG_PASTED


def test_sign_release_pubkey_and_keygen():
    code, out = _run(["pubkey"], SEED1.hex())
    assert code == 0 and out.strip() == PUB1.hex()
    kf = TMP / "keygen" / "debrief-release-key.txt"
    kf.parent.mkdir()
    code, out = _run(["keygen", "--out", str(kf)])
    assert code == 0, out
    secret = kf.read_text(encoding="ascii").strip()
    assert re.fullmatch(r"[0-9a-f]{64}", secret)
    pub = sign_release.public_key(bytes.fromhex(secret)).hex()
    assert pub in out and secret not in out
    code, out = _run(["keygen", "--out", str(kf)])
    assert code != 0 and "already exists" in str(code)
    assert kf.read_text(encoding="ascii").strip() == secret


def test_sign_release_reads_pem_when_cryptography_is_there():
    if not HAVE_CRYPTO:
        print("SKIPPED (cryptography not installed)")
        return
    pem = Ed25519PrivateKey.from_private_bytes(SEED1).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    code, out = _run(["pubkey"], pem)
    assert code == 0 and out.strip() == PUB1.hex()
    code, out = _run(["pubkey"], "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n")
    assert code != 0 and "AAAA" not in str(code) + out


def test_sign_release_runs_as_a_script():
    d = TMP / "sign3"
    d.mkdir()
    exe = d / "Debrief-Setup-9.9.9.exe"
    exe.write_bytes(PAYLOAD)
    env = dict(os.environ, DEBRIEF_SIGNING_KEY=SEED1.hex())
    r = subprocess.run([sys.executable, str(ROOT / "packaging" / "sign_release.py"), "sign", str(exe),
                        "--version", "9.9.9"], env=env, capture_output=True, text=True, timeout=120)
    if updater.release_keys() and PUB1 not in updater.release_keys():
        assert r.returncode != 0   # the shipped key isn't the test key: refuses, as it should
        return
    assert r.returncode == 0, r.stdout + r.stderr
    assert SEED1.hex() not in r.stdout + r.stderr


# ================================================================ build.yml
def _wf():
    return (ROOT / ".github" / "workflows" / "build.yml").read_text(encoding="utf-8")


def test_workflow_kept_and_signing_job():
    wf = _wf()
    assert "pyinstaller==6.22.3" in wf
    for t in ("test_25_a", "test_25_b", "test_25_c", "test_25_d"):
        assert f"python tests/{t}.py" in wf, t
    try:
        import yaml
    except ImportError:
        yaml = None
        print("  (PyYAML not installed: text checks only)")
    assert "echo $DEBRIEF_SIGNING_KEY" not in wf and 'echo "$DEBRIEF_SIGNING_KEY"' not in wf
    assert not re.search(r"DEBRIEF_SIGNING_KEY[^\n]*>", wf.split("jobs:", 1)[1]), "the key is written to a file"
    if not yaml:
        return
    d = yaml.safe_load(wf)
    jobs = d["jobs"]
    b, rel = jobs["windows"], jobs["release"]
    names = [s.get("name", "") for s in b["steps"]]
    for must in ("Smoke test the built app", "Upgrade test (previous release -> this build, the way the in-app updater does it)",
                 "Silent install smoke test", "Build with PyInstaller", "Upload installer"):
        assert must in names, must
    i_last_old = names.index("Test 2.4.0 review fixes")
    assert names[i_last_old + 1:i_last_old + 5] == [n for n in names if "(2.5)" in n] and len(
        [n for n in names if "(2.5)" in n]) == 4
    assert b["permissions"] == {"contents": "read"} and d["permissions"] == {"contents": "read"}
    assert not any("gh release" in str(s.get("run", "")) or "DEBRIEF_SIGNING_KEY" in json.dumps(s) for s in b["steps"])
    assert rel["needs"] == "windows" and rel["environment"] == "release"
    assert rel["permissions"] == {"contents": "write"}
    assert rel["if"] == ("github.event_name != 'pull_request' && (github.ref == 'refs/heads/main' || "
                         "startsWith(github.ref, 'refs/tags/v'))")
    steps = rel["steps"]
    for s in steps:
        if str(s.get("uses", "")).startswith("actions/checkout"):
            assert s["with"]["persist-credentials"] is False
    inst = next(s for s in steps if "cryptography" in str(s.get("run", "")))
    assert re.search(r'cryptography==\d+\.\d+\.\d+"', inst["run"]), "cryptography must be pinned exactly"
    signs = [s for s in steps if "DEBRIEF_SIGNING_KEY" in json.dumps(s.get("env", {}))]
    assert len(signs) == 1
    sg = signs[0]
    assert sg["env"]["DEBRIEF_SIGNING_KEY"] == "${{ secrets.DEBRIEF_SIGNING_KEY }}"
    assert "sign_release.py sign" in sg["run"] and "--version" in sg["run"]
    assert "release_keys()" in sg["run"]
    assert steps.index(inst) < steps.index(sg)
    clean = [s for s in steps if s.get("if") == "always()"]
    assert clean and "RUNNER_TEMP" in clean[0]["run"] and steps.index(clean[0]) > steps.index(sg)
    pub = next(s for s in steps if "gh release create" in str(s.get("run", "")))
    assert ".exe.sig" in pub["run"] and ".exe.sha256" in pub["run"]
    assert "DEBRIEF_SIGNING_KEY" not in json.dumps(pub)


# ================================================================ discord
TOKEN = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789_-AbCdEfGhIjKlMnOpQrStUvWxYz01"
HOOK = "https://discord.com/api/webhooks/123456789012341234/" + TOKEN


def png(w=1200, h=630, extra=0):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    raw = zlib.compress(b"\x00\x00\x00\x00" * 4)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", raw + b"\x00" * extra) + chunk(b"IEND", b""))


class FakeSession:
    def __init__(self, *responses, raise_exc=None):
        self.responses = list(responses)
        self.calls = []
        self.raise_exc = raise_exc

    def _do(self, method, url, **kw):
        self.calls.append((method, url, kw))
        if self.raise_exc:
            raise self.raise_exc
        return self.responses.pop(0) if self.responses else Resp(status=200, js={"id": "1"})

    def post(self, url, **kw):
        return self._do("POST", url, **kw)

    def get(self, url, **kw):
        return self._do("GET", url, **kw)


class LogCatch(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))


def derr(fn):
    try:
        fn()
    except discord.DiscordError as e:
        return e
    raise AssertionError("no DiscordError")


def test_discord_url_validation():
    good = [HOOK, HOOK.replace("discord.com", "ptb.discord.com"), HOOK.replace("discord.com", "canary.discord.com"),
            HOOK.replace("discord.com", "discordapp.com")]
    for u in good:
        assert discord.valid(u), u
    bad = [HOOK.replace("https://", "http://"),                      # http
           HOOK.replace("discord.com", "discord.com.evil.example"),  # bad host
           HOOK.replace("discord.com", "evil-discord.com"),
           HOOK.replace("discord.com", "discord.gg"),
           HOOK + "?wait=true", HOOK + "?", HOOK + "#x",               # query string / fragment
           HOOK + "/", HOOK + " ", " " + HOOK, HOOK[:60] + "\n" + HOOK[60:], HOOK.replace("1234/", "12 34/"),
           "https://discord.com/api/webhooks/1234/" + TOKEN,          # id too short
           "https://discord.com/api/webhooks/123456789012341234/short",
           "https://discord.com/api/webhooks/123456789012341234/" + TOKEN + "!",
           "https://user@discord.com/api/webhooks/123456789012341234/" + TOKEN,
           "", None, 123, b"https://discord.com"]
    for u in bad:
        assert not discord.valid(u), u
    assert discord.WEBHOOK_RE == (r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/webhooks/(\d{17,20})/"
                                  r"([A-Za-z0-9_-]{60,100})$")
    assert discord.hint(HOOK) == "…/1234" and discord.hint("nope") == ""
    assert TOKEN not in discord.hint(HOOK)
    fx = json.loads((FIX / "sample_discord.json").read_text(encoding="utf-8"))
    assert discord.valid(fx["webhook_fake"]) and discord.hint(fx["webhook_fake"]) == fx["hint"]
    assert fx["errors"]["gone_401_403_404"] == discord.MSG_GONE and fx["errors"]["bad_url"] == discord.MSG_BAD_URL


def test_discord_post_multipart():
    s = FakeSession(Resp(status=200, js={"id": "1300000000000000001", "channel_id": "9"}))
    img = png()
    r = discord.post(HOOK, img, "  Jordan · Okayama · MX-5: best 1:38.412 @everyone and @here <@123>  ", session=s)
    assert r == {"ok": True, "id": "1300000000000000001"}
    assert len(s.calls) == 1
    method, url, kw = s.calls[0]
    assert method == "POST" and url == HOOK + "?wait=true" and kw["timeout"] == 20
    assert kw["files"] == {"files[0]": ("debrief.png", img, "image/png")}
    payload = json.loads(kw["data"]["payload_json"])
    assert payload["allowed_mentions"] == {"parse": []}
    assert "@everyone" not in payload["content"] and "@here" not in payload["content"]
    assert payload["content"].startswith("Jordan · Okayama")
    assert TOKEN not in json.dumps(kw.get("headers", {}))
    # 204 (no wait body)
    assert discord.post(HOOK, img, "", session=FakeSession(Resp(status=204))) == {"ok": True, "id": None}


def test_discord_status_messages():
    img = png()
    for code in (401, 403, 404):
        e = derr(lambda: discord.post(HOOK, img, "x", session=FakeSession(Resp(status=code))))
        assert str(e) == "That Discord webhook no longer works. Paste a new one in Settings."
    e = derr(lambda: discord.post(HOOK, img, "x", session=FakeSession(Resp(status=413))))
    assert str(e) == "The picture is too big for Discord."
    e = derr(lambda: discord.post(HOOK, img, "x", session=FakeSession(Resp(status=500))))
    assert "500" in str(e) and TOKEN not in str(e)
    # 429 with a long wait: no retry
    waits = []
    s = FakeSession(Resp(status=429, headers={"Retry-After": "30"}))
    e = derr(lambda: discord.post(HOOK, img, "x", session=s, sleep=waits.append))
    assert str(e) == "Discord asked Debrief to wait. Try again in a minute." and len(s.calls) == 1 and waits == []
    # 429 with a short wait: one wait, one retry
    s = FakeSession(Resp(status=429, headers={"Retry-After": "2.5"}), Resp(status=200, js={"id": "7"}))
    assert discord.post(HOOK, img, "x", session=s, sleep=waits.append) == {"ok": True, "id": "7"}
    assert waits == [2.5] and len(s.calls) == 2
    # 429 twice: gives up after the one retry
    waits.clear()
    s = FakeSession(Resp(status=429, headers={"Retry-After": "1"}), Resp(status=429, headers={"Retry-After": "1"}))
    e = derr(lambda: discord.post(HOOK, img, "x", session=s, sleep=waits.append))
    assert "wait" in str(e) and len(s.calls) == 2 and waits == [1.0]
    # 429 with retry_after only in the JSON body
    s = FakeSession(Resp(status=429, js={"retry_after": 0.5}), Resp(status=204))
    waits.clear()
    assert discord.post(HOOK, img, "x", session=s, sleep=waits.append)["ok"] and waits == [0.5]
    # 429 without any wait hint
    e = derr(lambda: discord.post(HOOK, img, "x", session=FakeSession(Resp(status=429)), sleep=waits.append))
    assert "wait" in str(e)


def test_discord_network_error_hides_url():
    img = png()
    exc = requests.ConnectionError(f"HTTPSConnectionPool(host='discord.com', port=443): Max retries exceeded "
                                   f"with url: /api/webhooks/123456789012341234/{TOKEN}?wait=true")
    e = derr(lambda: discord.post(HOOK, img, "x", session=FakeSession(raise_exc=exc)))
    assert str(e) == "Couldn't reach Discord."
    assert e.__cause__ is None and e.__context__ is None
    tb = "".join(traceback.format_exception(type(e), e, e.__traceback__))
    assert TOKEN not in tb and "123456789012341234" not in tb
    e = derr(lambda: discord.check(HOOK, session=FakeSession(raise_exc=exc)))
    assert str(e) == "Couldn't reach Discord." and e.__context__ is None
    e = derr(lambda: discord.post(HOOK, img, "x", session=FakeSession(raise_exc=requests.Timeout(HOOK))))
    assert str(e) == "Couldn't reach Discord."


def test_discord_refuses_before_any_network_call():
    s = FakeSession()
    cases = [
        (HOOK, b"GIF89a" + b"\x00" * 100, "x", "isn't a PNG"),
        (HOOK, b"\x89PNG\r\n\x1a\n" + b"\x00" * 40, "x", "isn't a PNG"),             # no IHDR
        (HOOK, png(extra=4 * 1024 * 1024), "x", "too big"),                           # > 4 MB
        (HOOK, png(w=3000, h=600), "x", "too big"),                                   # IHDR width > 2400
        (HOOK, png(w=800, h=2401), "x", "too big"),
        (HOOK, png(w=0, h=600), "x", "too big"),
        (HOOK, png(), "y" * 2001, "too long"),
        (HOOK, "not bytes", "x", "isn't a PNG"),
        (HOOK + "?x=1", png(), "x", "isn't a Discord webhook"),
        ("http://discord.com/api/webhooks/123456789012341234/" + TOKEN, png(), "x", "isn't a Discord webhook"),
    ]
    for url, img, content, want in cases:
        e = derr(lambda: discord.post(url, img, content, session=s))
        assert want in str(e), (want, str(e))
        assert TOKEN not in str(e)
    assert s.calls == [], "a refused post reached the network"
    assert png(w=2400, h=2400)[:8] == discord.PNG_MAGIC
    assert discord.post(HOOK, png(w=2400, h=2400), "z" * 2000, session=s)["ok"]   # the limits themselves are OK
    derr(lambda: discord.check(HOOK + "#", session=s))
    assert len(s.calls) == 1


def test_discord_check():
    s = FakeSession(Resp(status=200, js={"type": 1, "id": "123456789012341234", "name": "Debrief",
                                         "channel_id": "987654321098765432", "guild_id": "5", "token": TOKEN}))
    r = discord.check(HOOK, session=s)
    assert r == {"ok": True, "name": "Debrief", "channel_id": "987654321098765432"}
    assert TOKEN not in json.dumps(r)
    assert s.calls[0][0] == "GET" and s.calls[0][1] == HOOK and s.calls[0][2]["timeout"] == 20
    fx = json.loads((FIX / "sample_discord.json").read_text(encoding="utf-8"))
    assert set(r) == set(fx["check"])
    e = derr(lambda: discord.check(HOOK, session=FakeSession(Resp(status=404))))
    assert str(e) == discord.MSG_GONE
    e = derr(lambda: discord.check(HOOK, session=FakeSession(Resp(status=200, js=ValueError("x")))))
    assert "unexpected" in str(e)


def test_discord_logs_never_contain_the_webhook():
    h = LogCatch()
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(h)
    root.setLevel(logging.DEBUG)
    try:
        discord.post(HOOK, png(), "hello", session=FakeSession(Resp(status=200, js={"id": "1"})))
        for code in (401, 413, 429, 500):
            try:
                discord.post(HOOK, png(), "x", session=FakeSession(Resp(status=code)), sleep=lambda s: None)
            except discord.DiscordError:
                pass
        try:
            discord.post(HOOK, png(), "x", session=FakeSession(raise_exc=requests.ConnectionError(HOOK)))
        except discord.DiscordError:
            pass
        discord.check(HOOK, session=FakeSession(Resp(status=200, js={"name": "D", "channel_id": "1", "token": TOKEN})))
        # urllib3 logs request paths at DEBUG; after a post its records are redacted
        logging.getLogger("urllib3.connectionpool").debug('%s://%s:%s "%s %s %s" %s', "https", "discord.com", 443,
                                                          "POST", f"/api/webhooks/123456789012341234/{TOKEN}?wait=true",
                                                          "HTTP/1.1", 200)
    finally:
        root.removeHandler(h)
        root.setLevel(old_level)
    text = "\n".join(h.lines)
    assert "Posted to Discord (webhook …1234)" in h.lines
    assert TOKEN not in text and HOOK not in text, text
    assert "/api/webhooks/123456789012341234/…" in text
    assert discord.redact(f"see {HOOK}?wait=true", HOOK) == "see https://discord.com/api/webhooks/123456789012341234/…?wait=true"


def test_discord_nothing_runs_on_import():
    src = (ROOT / "debrief" / "discord.py").read_text(encoding="utf-8")
    assert not re.search(r"^\s*import requests", src.split("def ", 1)[0], re.M), "requests imported at module level"
    for word in ("threading", "Timer", "sched", "asyncio", "atexit"):
        assert word not in src, word
    # importing it in a fresh interpreter with the network blocked makes no call and adds no log filter
    code = ("import sys, logging, requests\n"
            "def boom(*a, **k): raise SystemExit('network on import')\n"
            "requests.get = requests.post = requests.request = boom\n"
            "import debrief.discord as d\n"
            "assert not logging.getLogger('urllib3.connectionpool').filters\n"
            "print('clean')\n")
    r = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "clean" in r.stdout, r.stdout + r.stderr


if __name__ == "__main__":
    import time
    fails = 0
    names = [n for n in sorted(globals()) if n.startswith("test_")]
    order = sorted(names, key=lambda n: list(globals()).index(n))
    for n in order:
        t0 = time.time()
        try:
            globals()[n]()
            print(f"ok    {n} ({time.time() - t0:.1f}s)")
        except Exception:
            fails += 1
            print(f"FAIL  {n}")
            traceback.print_exc()
    print(f"{len(order) - fails}/{len(order)} passed")
    faulthandler.cancel_dump_traceback_later()
    sys.exit(1 if fails else 0)
