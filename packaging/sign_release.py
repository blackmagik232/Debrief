"""Sign a Debrief installer for the in-app updater (Ed25519, pure Python, no extra packages).

    python packaging/sign_release.py --keygen
        Makes a new key pair. Put the PUBLIC key in debrief/updater.py (RELEASE_PUBLIC_KEY) and the
        SECRET key in the GitHub secret DEBRIEF_SIGNING_KEY (environment "release"). Never commit the secret.

    DEBRIEF_SIGNING_KEY=<secret hex> python packaging/sign_release.py dist-installer/Debrief-Setup-2.2.2.exe
        Writes Debrief-Setup-2.2.2.exe.sig next to the installer. The signature covers the file name
        (which carries the version) and its SHA-256: see debrief.updater.signed_message.
        Refuses to sign if the key doesn't match RELEASE_PUBLIC_KEY in updater.py (when that is set),
        so a release can never ship with a signature the app would reject.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from debrief import updater as U  # noqa: E402


def _encode(pt) -> bytes:
    x, y, z, _ = pt
    zi = pow(z, U._P - 2, U._P)
    x, y = x * zi % U._P, y * zi % U._P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _expand(seed: bytes):
    if len(seed) != 32:
        raise ValueError("the secret key must be 32 bytes (64 hex characters)")
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def public_key(seed: bytes) -> bytes:
    a, _ = _expand(seed)
    return _encode(U._pt_mul(a, U._G))


def sign(seed: bytes, message: bytes) -> bytes:
    a, prefix = _expand(seed)
    A = _encode(U._pt_mul(a, U._G))
    r = int.from_bytes(hashlib.sha512(prefix + message).digest(), "little") % U._L
    R = _encode(U._pt_mul(r, U._G))
    k = int.from_bytes(hashlib.sha512(R + A + message).digest(), "little") % U._L
    S = (r + k * a) % U._L
    return R + S.to_bytes(32, "little")


def sign_installer(path: Path, seed: bytes) -> Path:
    name = path.name
    if not re.fullmatch(r"Debrief-Setup-[\d.]+\.exe", name):
        raise SystemExit(f"{name} isn't named like a Debrief installer (Debrief-Setup-x.y.z.exe)")
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    msg = U.signed_message(name, h.hexdigest())
    pub = public_key(seed)
    want = U.RELEASE_PUBLIC_KEY
    if want is not None and pub.hex() != str(want).strip().lower():
        raise SystemExit("DEBRIEF_SIGNING_KEY doesn't match RELEASE_PUBLIC_KEY in debrief/updater.py; not signing.")
    sig = sign(seed, msg)
    if not U.ed25519_verify(pub, msg, sig):
        raise SystemExit("signature self-check failed")
    out = path.with_name(name + ".sig")
    out.write_text(sig.hex() + "\n", encoding="ascii")
    print(f"Signed {name} (public key {pub.hex()})")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("installer", nargs="?")
    ap.add_argument("--keygen", action="store_true")
    a = ap.parse_args(argv)
    if a.keygen:
        seed = os.urandom(32)
        print("SECRET key (GitHub secret DEBRIEF_SIGNING_KEY; keep it private):", seed.hex())
        print("PUBLIC key (RELEASE_PUBLIC_KEY in debrief/updater.py):        ", public_key(seed).hex())
        return 0
    if not a.installer:
        ap.error("give the installer to sign, or --keygen")
    raw = (os.environ.get("DEBRIEF_SIGNING_KEY") or "").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{64}", raw):
        raise SystemExit("DEBRIEF_SIGNING_KEY must be the 64-hex-character secret key from --keygen")
    sign_installer(Path(a.installer), bytes.fromhex(raw))
    return 0


if __name__ == "__main__":
    sys.exit(main())
