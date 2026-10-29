"""Sign a Debrief installer for the in-app updater (Ed25519). Runs on the owner's PC (keygen) and in the
CI release job (sign). Never part of Debrief.exe.

    python packaging/sign_release.py keygen --out debrief-release-key.txt
        Makes a new key pair. Writes the SECRET key (64 hex characters) to the file (it refuses to
        overwrite one) and prints only the PUBLIC key (64 hex characters) for RELEASE_PUBKEYS in
        debrief/updater.py. Put the secret in the GitHub secret DEBRIEF_SIGNING_KEY (environment
        "release"). Never commit it.

    DEBRIEF_SIGNING_KEY=<secret> python packaging/sign_release.py sign dist-installer/Debrief-Setup-2.5.0.exe --version 2.5.0
        Writes Debrief-Setup-2.5.0.exe.sig (one line: 128 hex characters, the 64-byte signature) and
        Debrief-Setup-2.5.0.exe.sha256 next to the installer, then checks the signature with Debrief's own
        verifier (debrief/ed25519.py) before exiting 0. The signature covers the version, the file name
        and its SHA-256: see debrief.updater.signed_message (format v1).
        Refuses to sign if the key isn't one of the keys in RELEASE_PUBKEYS / RELEASE_PUBLIC_KEY (when
        any is set), so a release can never ship with a signature the app would reject.

    DEBRIEF_SIGNING_KEY=<secret> python packaging/sign_release.py pubkey
        Prints the public key hex of the secret in the env var (to check the constant in updater.py).

The secret is read ONLY from the env var DEBRIEF_SIGNING_KEY: either the 64-hex secret from keygen (the
2.2.2 format) or an Ed25519 PEM private key (BEGIN PRIVATE KEY; needs `cryptography`). It is never
printed, logged or written anywhere by `sign` or `pubkey`.

Signing uses `cryptography` when it is installed (the CI release job pins it), otherwise the pure-Python
RFC 8032 code below; Ed25519 is deterministic, so both give the same signature. The 2.2.2 command
forms (`--keygen`, and a bare installer path) still work.
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

from debrief import ed25519 as E  # noqa: E402
from debrief import updater as U  # noqa: E402

ENV = "DEBRIEF_SIGNING_KEY"


# ---------------------------------------------------------------- pure-Python RFC 8032 signing
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
    return E.point_encode(E.point_mul(a, E.G))


def _sign_pure(seed: bytes, message: bytes) -> bytes:
    a, prefix = _expand(seed)
    A = E.point_encode(E.point_mul(a, E.G))
    r = int.from_bytes(hashlib.sha512(prefix + message).digest(), "little") % E.L
    R = E.point_encode(E.point_mul(r, E.G))
    k = int.from_bytes(hashlib.sha512(R + A + message).digest(), "little") % E.L
    S = (r + k * a) % E.L
    return R + S.to_bytes(32, "little")


def _crypto():
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        return Ed25519PrivateKey
    except ImportError:
        return None


def sign(seed: bytes, message: bytes) -> bytes:
    """Ed25519 signature of message with the 32-byte secret seed."""
    if len(seed) != 32:
        raise ValueError("the secret key must be 32 bytes (64 hex characters)")
    K = _crypto()
    if K is not None:
        return K.from_private_bytes(seed).sign(message)
    return _sign_pure(seed, message)


# ---------------------------------------------------------------- the key from the environment
def _seed_from_env() -> bytes:
    """The 32-byte secret from DEBRIEF_SIGNING_KEY. Error messages never include the value."""
    raw = (os.environ.get(ENV) or "").strip()
    if not raw:
        raise SystemExit(f"{ENV} isn't set.")
    if re.fullmatch(r"[0-9a-fA-F]{64}", raw):
        return bytes.fromhex(raw)
    if "PRIVATE KEY" in raw:
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        except ImportError:
            raise SystemExit(f"{ENV} is a PEM key; reading it needs `cryptography` (pip install cryptography).") from None
        try:
            k = serialization.load_pem_private_key(raw.replace("\\n", "\n").encode("ascii"), password=None)
        except Exception:
            raise SystemExit(f"{ENV} isn't a readable unencrypted PEM private key.") from None
        if not isinstance(k, Ed25519PrivateKey):
            raise SystemExit(f"{ENV} isn't an Ed25519 key.")
        return k.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                               serialization.NoEncryption())
    raise SystemExit(f"{ENV} must be the 64-hex-character secret key from keygen, or an Ed25519 PEM key.")


def _app_keys() -> list:
    try:
        return U.release_keys()
    except U.UpdateError:
        raise SystemExit("RELEASE_PUBKEYS in debrief/updater.py has a key that isn't 64 hex characters; not signing.") from None


# ---------------------------------------------------------------- commands
def sign_installer(path: Path, seed: bytes, version: str | None = None) -> Path:
    path = Path(path)
    name = path.name
    try:
        from_name = U.version_from_name(name)
    except ValueError:
        raise SystemExit(f"{name} isn't named like a Debrief installer (Debrief-Setup-x.y.z.exe)") from None
    version = from_name if version is None else str(version).strip().lstrip("v")
    if version != from_name:
        raise SystemExit(f"--version {version} doesn't match the installer name {name}; not signing.")
    if not path.is_file():
        raise SystemExit(f"{path} doesn't exist")
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    sha = h.hexdigest()
    msg = U.signed_message(version, name, sha)
    pub = public_key(seed)
    keys = _app_keys()
    if keys and pub not in keys:
        raise SystemExit(f"{ENV} doesn't match RELEASE_PUBKEYS in debrief/updater.py; not signing.")
    sig = sign(seed, msg)
    # prove that the app's own verifier (the code installed copies run) accepts it
    if not E.verify(pub, msg, sig) or not U.verify_release(version, name, sha, sig, [pub]):
        raise SystemExit("signature self-check with debrief/ed25519.py failed; not publishing a .sig")
    if sig != _sign_pure(seed, msg):
        raise SystemExit("signature self-check failed (cryptography and the RFC 8032 code disagree)")
    out = path.with_name(name + ".sig")
    out.write_text(sig.hex() + "\n", encoding="ascii")
    path.with_name(name + ".sha256").write_text(f"{sha}  {name}", encoding="ascii")
    print(f"Signed {name} for version {version} (public key {pub.hex()}); wrote {out.name} and {name}.sha256")
    return out


def keygen(out: Path) -> bytes:
    out = Path(out)
    if out.exists():
        raise SystemExit(f"{out} already exists; not overwriting a key.")
    seed = os.urandom(32)
    fd = os.open(str(out), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(seed.hex() + "\n")
    pub = public_key(seed)
    print(f"Wrote the SECRET key to {out}. Back it up offline and add it as the GitHub secret {ENV}"
          " (environment \"release\"). Never commit it.")
    print("PUBLIC key (put it in RELEASE_PUBKEYS in debrief/updater.py):")
    print(pub.hex())
    return pub


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("keygen", "sign", "pubkey"):
        ap = argparse.ArgumentParser(prog="sign_release.py")
        sub = ap.add_subparsers(dest="cmd", required=True)
        k = sub.add_parser("keygen", help="make a key pair (on your own PC)")
        k.add_argument("--out", required=True, help="file for the SECRET key (64 hex characters)")
        s = sub.add_parser("sign", help=f"sign an installer with the key in {ENV}")
        s.add_argument("installer")
        s.add_argument("--version", default=None)
        sub.add_parser("pubkey", help=f"print the public key of {ENV}")
        a = ap.parse_args(argv)
        if a.cmd == "keygen":
            keygen(Path(a.out))
        elif a.cmd == "sign":
            sign_installer(Path(a.installer), _seed_from_env(), a.version)
        else:
            print(public_key(_seed_from_env()).hex())
        return 0
    # 2.2.2 forms
    ap = argparse.ArgumentParser(prog="sign_release.py")
    ap.add_argument("installer", nargs="?")
    ap.add_argument("--keygen", action="store_true")
    ap.add_argument("--out", default=None)
    ap.add_argument("--version", default=None)
    a = ap.parse_args(argv)
    if a.keygen:
        if a.out:
            keygen(Path(a.out))
            return 0
        seed = os.urandom(32)   # 2.2.2 behaviour, for the owner's own terminal only
        print("SECRET key (GitHub secret DEBRIEF_SIGNING_KEY; keep it private):", seed.hex())
        print("PUBLIC key (RELEASE_PUBKEYS in debrief/updater.py):           ", public_key(seed).hex())
        return 0
    if not a.installer:
        ap.error("give the installer to sign, or use: keygen --out FILE | sign EXE --version V | pubkey")
    sign_installer(Path(a.installer), _seed_from_env(), a.version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
