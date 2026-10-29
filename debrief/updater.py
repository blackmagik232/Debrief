"""Built-in updater for the installed app.

    check(force=False)  -> {"current", "latest", "newer", "notes", "url", "installer", "size", "sha256_url", ...}
    download(info, progress) -> path of the verified installer (temp folder)
    launch(path)        start the installer silently; it closes Debrief, installs and reopens it

Debrief asks GitHub for the newest release at most once a day (one request, nothing about you is
sent), and only when "check_updates" is on in settings.toml (it is by default). The installer is only
run after its SHA-256 matches the checksum file published with the release and, once RELEASE_PUBLIC_KEY
is set, after its Ed25519 signature (Debrief-Setup-x.y.z.exe.sig) checks out against that key.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

REPO = "blackmagik232/Debrief"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
CHECK_EVERY_S = 24 * 3600
MAX_INSTALLER = 200 * 1024 * 1024
MAX_SIG = 4096

# Ed25519 public key (64 hex characters) that signs every release.
# None = signing isn't set up yet: releases are accepted on their SHA-256 alone (the 2.2.1 behaviour).
# As soon as a key is set here, an installer with a missing or bad .sig file is NEVER installed.
# Make a key pair with:  python packaging/sign_release.py --keygen
# then paste the PUBLIC key here and store the SECRET key as the GitHub secret DEBRIEF_SIGNING_KEY
# (in the "release" environment). Never commit the secret key.
RELEASE_PUBLIC_KEY: str | None = None
SIG_CONTEXT = "Debrief release signature v1"
TEMP_PREFIX = "debrief-update-"


class UpdateError(Exception):
    """A problem to show as-is."""


def _ver(s) -> tuple:
    nums = re.findall(r"\d+", str(s or ""))[:3]
    return tuple(int(n) for n in nums) + (0,) * (3 - len(nums)) if nums else (0, 0, 0)


def newer(latest, current) -> bool:
    return _ver(latest) > _ver(current)


def _state_path() -> Path:
    from . import app
    return Path(os.environ.get("DEBRIEF_UPDATE_STATE") or (app.HERE / "update.json"))


def _read_state() -> dict:
    try:
        d = json.loads(_state_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(d: dict) -> None:
    try:
        p = _state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(d), encoding="utf-8")
    except OSError:
        pass


def _headers():
    from . import __version__
    return {"User-Agent": f"Debrief/{__version__} (updater)", "Accept": "application/vnd.github+json"}


def _from_release(rel: dict) -> dict:
    tag = str(rel.get("tag_name") or "")
    raw_assets = rel.get("assets")
    assets = [a for a in raw_assets if isinstance(a, dict)] if isinstance(raw_assets, list) else []
    exe = next((a for a in assets if re.fullmatch(r"Debrief-Setup-[\d.]+\.exe", str(a.get("name") or ""))), None)
    sha = None
    if exe:
        sha = next((a for a in assets if a.get("name") == exe["name"] + ".sha256"), None)
        sig = next((a for a in assets if a.get("name") == exe["name"] + ".sig"), None)
    else:
        sig = None
    digest = str((exe or {}).get("digest") or "")
    return {"latest": tag.lstrip("v"), "notes": str(rel.get("body") or "")[:4000], "url": rel.get("html_url"),
            "published": rel.get("published_at"),
            "installer": (exe or {}).get("browser_download_url"), "installer_name": (exe or {}).get("name"),
            "size": (exe or {}).get("size"), "sha256_url": (sha or {}).get("browser_download_url"),
            "sig_url": (sig or {}).get("browser_download_url"),
            "sha256": digest.split(":", 1)[1].lower() if digest.startswith("sha256:") else None}


def _checked_at(st: dict) -> float:
    """When the saved answer was fetched; 0 (= ask again) if it's missing or isn't a number."""
    v = st.get("checked")
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return 0.0
    v = float(v)
    return v if v == v and abs(v) != float("inf") else 0.0


def _stale(checked: float, now: float) -> bool:
    """Older than a day, or in the future (the PC's clock was wrong when it was saved): ask again."""
    age = now - checked
    return age > CHECK_EVERY_S or age < -3600


def check(force: bool = False, now: float | None = None) -> dict:
    """What's the newest release? Uses the saved answer if it's under a day old (unless force)."""
    from . import __version__
    now = time.time() if now is None else now
    st = _read_state()
    rel = st.get("release") if isinstance(st.get("release"), dict) else None
    if force or rel is None or _stale(_checked_at(st), now):
        import requests
        try:
            r = requests.get(API, headers=_headers(), timeout=15)
        except requests.RequestException as e:
            raise UpdateError("Couldn't reach GitHub to check for updates. Check your connection.") from e
        if r.status_code == 404:
            raise UpdateError("Couldn't see Debrief's releases on GitHub (the repository may be private).")
        if r.status_code != 200:
            raise UpdateError(f"GitHub returned {r.status_code} when checking for updates. Try again later.")
        try:
            js = r.json()
            if not isinstance(js, dict):
                raise ValueError("not an object")
            rel = _from_release(js)
        except (ValueError, TypeError, AttributeError) as e:
            raise UpdateError("GitHub sent something unexpected. Try again later.") from e
        st = {"checked": now, "release": rel}
        _write_state(st)
    out = dict(rel)
    out.update({"current": __version__, "newer": newer(rel.get("latest"), __version__) and bool(rel.get("installer")),
                "checked": _checked_at(st) or None})
    return out


# ---------------------------------------------------------------- Ed25519 signature check
# A small pure-Python verifier (RFC 8032, section 5.1.7), so the installed app needs no extra
# packages (the build leaves out `cryptography`). It only checks signatures; it never holds a secret.
_P = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def _recover_x(y: int, sign: int):
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


def _pt_add(a, b):
    x1, y1, z1, t1 = a
    x2, y2, z2, t2 = b
    A = (y1 - x1) * (y2 - x2) % _P
    B = (y1 + x1) * (y2 + x2) % _P
    C = 2 * t1 * t2 * _D % _P
    D = 2 * z1 * z2 % _P
    E, F, G, H = B - A, D - C, D + C, B + A
    return (E * F % _P, G * H % _P, F * G % _P, E * H % _P)


def _pt_mul(k: int, pt):
    q = (0, 1, 1, 0)
    while k:
        if k & 1:
            q = _pt_add(q, pt)
        pt = _pt_add(pt, pt)
        k >>= 1
    return q


def _pt_equal(a, b) -> bool:
    return (a[0] * b[2] - b[0] * a[2]) % _P == 0 and (a[1] * b[2] - b[1] * a[2]) % _P == 0


def _pt_decode(s: bytes):
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % _P)


_GY = 4 * pow(5, _P - 2, _P) % _P
_G = (_recover_x(_GY, 0), _GY, 1, _recover_x(_GY, 0) * _GY % _P)


def ed25519_verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """True only for a valid Ed25519 signature of message by public_key (RFC 8032)."""
    try:
        public_key, signature, message = bytes(public_key), bytes(signature), bytes(message)
    except TypeError:
        return False
    if len(public_key) != 32 or len(signature) != 64:
        return False
    A = _pt_decode(public_key)
    R = _pt_decode(signature[:32])
    if A is None or R is None:
        return False
    S = int.from_bytes(signature[32:], "little")
    if S >= _L:  # non-canonical S: reject (prevents malleability)
        return False
    h = int.from_bytes(hashlib.sha512(signature[:32] + public_key + message).digest(), "little") % _L
    return _pt_equal(_pt_mul(S, _G), _pt_add(R, _pt_mul(h, A)))


def signed_message(installer_name: str, sha256_hex: str) -> bytes:
    """What a release signature covers: the installer's file name (which carries the version, so an
    old signed installer can't be passed off as a newer one) and its SHA-256."""
    return f"{SIG_CONTEXT}\n{installer_name}\n{sha256_hex.lower()}\n".encode("ascii")


def _parse_sig(text: str):
    m = re.search(r"\b([0-9a-fA-F]{128})\b", text or "")
    return bytes.fromhex(m.group(1)) if m else None


def _public_key():
    k = RELEASE_PUBLIC_KEY
    if k is None:
        return None
    k = str(k).strip()
    if not re.fullmatch(r"[0-9a-fA-F]{64}", k):
        # a mistyped key must never mean "don't check": refuse every update instead
        raise UpdateError("This copy of Debrief has a broken update key, so it can't check updates are "
                          "genuine. Download the newest version from the release page instead.")
    return bytes.fromhex(k)


def _fetch_small(url, what: str, limit: int) -> str | None:
    import requests
    if not url or not str(url).startswith("https://github.com/" + REPO + "/releases/download/"):
        return None
    try:
        r = requests.get(url, headers=_headers(), timeout=30)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    text = r.text if isinstance(r.text, str) else ""
    return text[:limit]


# ---------------------------------------------------------------- download, install, clean up
def download(info: dict, progress=None, dest_dir=None) -> Path:
    """Download the installer, check its SHA-256 and (once a release key is set) its signature.
    Returns its path."""
    import requests
    url = info.get("installer")
    if not url or not str(url).startswith("https://github.com/" + REPO + "/releases/download/"):
        raise UpdateError("This release has no installer to download.")
    name = Path(str(info.get("installer_name") or "Debrief-Setup.exe")).name
    if not re.fullmatch(r"Debrief-Setup-[\d.]+\.exe", name):
        raise UpdateError("This release has no installer to download.")
    key = _public_key()
    sig = None
    if key is not None:
        latest = str(info.get("latest") or "")
        if name != f"Debrief-Setup-{latest}.exe":
            raise UpdateError("This release's installer doesn't match its version, so it wasn't installed.")
        sig = _parse_sig(_fetch_small(info.get("sig_url"), "signature", MAX_SIG) or "")
        if sig is None:
            raise UpdateError("This release isn't signed, so Debrief won't install it automatically. "
                              "Download it from the release page instead.")
    want = str(info.get("sha256") or "").lower() or None
    if want and not re.fullmatch(r"[0-9a-f]{64}", want):
        want = None
    if not want and info.get("sha256_url"):
        m = re.search(r"\b([0-9a-fA-F]{64})\b", _fetch_small(info.get("sha256_url"), "checksum", MAX_SIG) or "")
        want = m.group(1).lower() if m else None
    if not want:
        raise UpdateError("This release has no checksum, so Debrief won't install it automatically. "
                          "Download it from the release page instead.")
    try:
        total = int(info.get("size") or 0)
    except (TypeError, ValueError, OverflowError):
        total = 0
    if not 0 <= total <= MAX_INSTALLER:
        total = 0
    cleanup_old_downloads()
    d = Path(dest_dir or tempfile.mkdtemp(prefix=TEMP_PREFIX))
    d.mkdir(parents=True, exist_ok=True)
    out = d / name
    h = hashlib.sha256()
    got = 0

    def drop():
        try:
            out.unlink()
        except OSError:
            pass
        if dest_dir is None:
            try:
                d.rmdir()
            except OSError:
                pass
    try:
        with requests.get(url, headers=_headers(), stream=True, timeout=60) as r:
            if r.status_code != 200:
                raise UpdateError(f"GitHub returned {r.status_code} for the installer. Try again later.")
            with open(out, "wb") as f:
                for chunk in r.iter_content(256 * 1024):
                    got += len(chunk)
                    if got > MAX_INSTALLER:
                        raise UpdateError("The download was much bigger than expected, so it was stopped.")
                    f.write(chunk)
                    h.update(chunk)
                    if progress and total:
                        progress(min(0.95, got / total), f"Downloading Debrief {info.get('latest')}... "
                                                         f"{got // (1024 * 1024)} of {total // (1024 * 1024)} MB")
    except requests.RequestException as e:
        drop()
        raise UpdateError("The download stopped. Check your connection and try again.") from e
    except OSError as e:
        drop()
        raise UpdateError("Debrief couldn't save the download (is the disk full?). Try again.") from e
    except UpdateError:
        drop()
        raise
    if total and got != total:
        drop()
        raise UpdateError("The download didn't finish. Try again.")
    digest = h.hexdigest().lower()
    if digest != want:
        drop()
        raise UpdateError("The download didn't match its checksum, so it wasn't installed. Try again.")
    if key is not None and not ed25519_verify(key, signed_message(name, digest), sig):
        drop()
        raise UpdateError("The download's signature didn't check out, so it wasn't installed. "
                          "Download Debrief from the release page instead.")
    return out


def cleanup_old_downloads(older_than_s: float = 600.0, now: float | None = None) -> int:
    """Delete installers left in %TEMP% by earlier updates (50-100 MB each). Folders younger than
    older_than_s are kept, so an installer that is still running isn't touched. Returns how many went."""
    import shutil
    now = time.time() if now is None else now
    n = 0
    base = Path(tempfile.gettempdir())
    try:
        dirs = list(base.glob(TEMP_PREFIX + "*"))
    except OSError:
        return 0
    for d in dirs:
        try:
            if not d.is_dir() or d.is_symlink() or now - d.stat().st_mtime < older_than_s:
                continue
            shutil.rmtree(d)
            n += 1
        except OSError:
            continue
    return n


def _pending_path() -> Path:
    return _state_path().with_name("update-pending.json")


def install_log_path() -> Path:
    return _state_path().with_name("update-install.log")


def launch(path, version: str | None = None) -> None:
    """Start the installer silently and detached. It waits for Debrief to close (the DebriefRunning
    mutex), installs, then reopens Debrief (/RELAUNCH=1). The outcome is checked on the next start
    (see pending_result)."""
    if os.name != "nt":
        raise UpdateError("Updating from inside Debrief only works on Windows.")
    from . import __version__
    log = install_log_path()
    try:
        p = _pending_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"from": __version__, "to": str(version or ""), "time": time.time(),
                                 "log": str(log)}), encoding="utf-8")
    except OSError:
        pass
    flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen([str(path), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/CURRENTUSER", "/NORESTART",
                          "/NOCLOSEAPPLICATIONS", "/RELAUNCH=1", f"/LOG={log}"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True, creationflags=flags)
    except OSError as e:
        try:
            _pending_path().unlink()
        except OSError:
            pass
        raise UpdateError("Windows wouldn't start the installer (antivirus may have blocked it). "
                          "Download it from the release page instead.") from e


def pending_result(now: float | None = None, grace_s: float = 180.0) -> dict | None:
    """After an in-app update: did it work? Call once at start. Returns None (no update was started,
    or it may still be running), {"ok": True, "version"} or {"ok": False, "message", "log"}.
    Never touches your data; only the small update-pending.json note is removed."""
    from . import __version__
    now = time.time() if now is None else now
    p = _pending_path()
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict):
        d = {}
    to = str(d.get("to") or "")
    t = d.get("time")
    t = float(t) if isinstance(t, (int, float)) and not isinstance(t, bool) else 0.0
    done = bool(to) and not newer(to, __version__)
    if not done and 0 <= now - t < grace_s:
        return None   # the installer may still be working
    try:
        p.unlink()
    except OSError:
        pass
    if done:
        return {"ok": True, "version": __version__}
    log = str(d.get("log") or install_log_path())
    return {"ok": False, "version": to, "current": __version__, "log": log,
            "message": (f"The update to Debrief {to or 'the newest version'} didn't finish, so you still have "
                        f"{__version__}. Your reviews and settings are fine. Try Update again, or download the "
                        "installer from the release page. Details: update-install.log in "
                        "Debrief's data folder.")}
