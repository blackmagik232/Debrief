"""Built-in updater for the installed app.

    check(force=False)  -> {"current", "latest", "newer", "notes", "url", "installer", "size", "sha256_url", ...}
    download(info, progress) -> path of the verified installer (temp folder)
    launch(path)        start the installer silently; it closes Debrief, installs and reopens it

Debrief asks GitHub for the newest release at most once a day (one request, nothing about you is
sent), and only when "check_updates" is on in settings.toml (it is by default). The installer is only
run after its SHA-256 matches the checksum file published with the release and, once a key is set in
RELEASE_PUBKEYS, after its Ed25519 signature (Debrief-Setup-x.y.z.exe.sig) checks out against one of
those keys. The signature covers the version, the installer's name and its SHA-256 (signed_message).

Requests: 1 GitHub request per daily check; an update download is the installer plus the .sha256 and
(once a key is set) +1 GitHub request for the .sig.
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

from . import ed25519

REPO = "blackmagik232/Debrief"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
CHECK_EVERY_S = 24 * 3600
MAX_INSTALLER = 200 * 1024 * 1024
MAX_SIG = 4096

# Ed25519 public keys (64 hex characters each) that may sign a release. A tuple, so a key can be
# rotated: add the new key as the 2nd item, release once signed with the old key, then drop the old one.
# Empty = signing isn't set up yet: releases are accepted on their SHA-256 alone (the 2.2.1 behaviour).
# As soon as a key is set here, an installer with a missing or bad .sig file is NEVER installed.
# Make a key pair with:  python packaging/sign_release.py keygen --out debrief-release-key.txt
# then paste the PUBLIC key here, e.g. RELEASE_PUBKEYS = ("<64 hex>",)  (mind the comma), and store the
# SECRET key as the GitHub secret DEBRIEF_SIGNING_KEY (in the "release" environment). See
# packaging/SIGNING.md. Never commit the secret key.
RELEASE_PUBKEYS: tuple = ()
RELEASE_PUBLIC_KEY: str | None = None    # 2.2.2 name for a single key; still honoured (added to the tuple)
SIG_CONTEXT = "Debrief release v1"
TEMP_PREFIX = "debrief-update-"
NOT_SIGNED = ("This update isn't signed by Debrief's release key, so it wasn't installed. "
              "Download it from the release page (github.com/blackmagik232/debrief/releases) if you trust it.")
BAD_SIGNATURE = ("This update's signature doesn't match Debrief's release key, so it wasn't installed. "
                 "Download it from the release page (github.com/blackmagik232/debrief/releases) if you trust it.")


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
            raise UpdateError("Couldn't find Debrief's releases on GitHub. Try again later, or download the newest installer from "
                              "github.com/blackmagik232/debrief/releases.")
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


# ---------------------------------------------------------------- release signatures
# The verifier is pure Python (debrief/ed25519.py, RFC 8032), so the installed app needs no extra
# packages (the build leaves out `cryptography`). It only checks signatures; it never holds a secret.
ed25519_verify = ed25519.verify          # 2.2.2 name, kept as an alias
_P, _L, _G = ed25519.P, ed25519.L, ed25519.G
_pt_add, _pt_mul, _pt_equal, _pt_decode = ed25519.point_add, ed25519.point_mul, ed25519.point_equal, ed25519.point_decode

_NAME_RE = r"Debrief-Setup-([\d.]+)\.exe"


def release_keys() -> list:
    """The public keys (32 bytes each) a release may be signed with: RELEASE_PUBKEYS plus the 2.2.2-style
    RELEASE_PUBLIC_KEY. [] = signing isn't set up (SHA-256 only). A mistyped key never means "don't
    check": it refuses every update instead."""
    raw = RELEASE_PUBKEYS
    raw = [raw] if isinstance(raw, (str, bytes)) else list(raw or ())   # ("abc") without the comma is a str
    if RELEASE_PUBLIC_KEY is not None:
        raw.append(RELEASE_PUBLIC_KEY)
    out = []
    for k in raw:
        if k is None:
            continue
        k = k.decode("ascii", "replace") if isinstance(k, bytes) else str(k)
        k = k.strip()
        if not re.fullmatch(r"[0-9a-fA-F]{64}", k):
            raise UpdateError("This copy of Debrief has a broken update key, so it can't check updates are "
                              "genuine. Download the newest version from the release page (github.com/blackmagik232/debrief/releases) instead.")
        b = bytes.fromhex(k)
        if b not in out:
            out.append(b)
    return out


def version_from_name(installer_name: str) -> str:
    m = re.fullmatch(_NAME_RE, str(installer_name or ""))
    if not m:
        raise ValueError(f"{installer_name!r} isn't named like a Debrief installer (Debrief-Setup-x.y.z.exe)")
    return m.group(1)


def signed_message(version: str, installer_name: str, sha256_hex: str | None = None) -> bytes:
    """What a release signature covers (v1): the version (so an older release can't be served as a
    newer one), the installer's file name and its SHA-256:

        b"Debrief release v1\n" + version + b"\n" + installer_name + b"\n" + sha256_hex_lowercase + b"\n"

    The 2.2.2 two-argument form signed_message(installer_name, sha256_hex) is still accepted; it takes
    the version from the file name, which the updater requires to match the release anyway."""
    if sha256_hex is None:
        installer_name, sha256_hex = version, installer_name
        version = version_from_name(installer_name)
    parts = [str(version), str(installer_name), str(sha256_hex).lower()]
    if not re.fullmatch(r"[0-9a-f]{64}", parts[2]):
        raise ValueError("sha256_hex must be 64 hex characters")
    for x in parts[:2]:
        if not x or not re.fullmatch(r"[0-9A-Za-z.+_-]+", x):
            raise ValueError(f"{x!r} can't go in a signed message")
    return (SIG_CONTEXT + "\n" + "\n".join(parts) + "\n").encode("ascii")


def _parse_sig(text: str):
    """The 64-byte signature from a .sig file: 128 hex characters (what sign_release.py writes) or one
    line of base64 (88 characters). None if it's neither."""
    text = text or ""
    m = re.search(r"\b([0-9a-fA-F]{128})\b", text)
    if m:
        return bytes.fromhex(m.group(1))
    m = re.match(r"\s*([A-Za-z0-9+/]{86}==)(?:\s|$)", text)
    if m:
        import base64
        try:
            b = base64.b64decode(m.group(1), validate=True)
        except ValueError:
            return None
        return b if len(b) == 64 else None
    return None


def verify_release(version: str, installer_name: str, sha256_hex: str, sig: bytes, keys=None) -> bool:
    """True if sig is a valid v1 signature of this release by ANY of the keys (default: release_keys())."""
    keys = release_keys() if keys is None else keys
    try:
        msg = signed_message(version, installer_name, sha256_hex)
    except ValueError:
        return False
    return any(ed25519.verify(k, msg, sig) for k in keys)


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
    Returns its path.

    With a key in RELEASE_PUBKEYS: 1. fetch the .sig (missing or unreadable -> refused), 2. download the
    installer and hash it, 3. build signed_message(info["latest"], name, sha), 4. verify it against any
    key, 5. only then compare with the published SHA-256. Any failure deletes the file.
    Without a key (today's shipped state): the published SHA-256 must be there and match."""
    import requests
    url = info.get("installer")
    if not url or not str(url).startswith("https://github.com/" + REPO + "/releases/download/"):
        raise UpdateError("This release has no installer to download.")
    name = Path(str(info.get("installer_name") or "Debrief-Setup.exe")).name
    if not re.fullmatch(r"Debrief-Setup-[\d.]+\.exe", name):
        raise UpdateError("This release has no installer to download.")
    keys = release_keys()
    latest = str(info.get("latest") or "")
    sig = None
    if keys:
        # 1. the .sig first (<= 1 KB, same release-download prefix check)
        if name != f"Debrief-Setup-{latest}.exe":
            raise UpdateError("This release's installer doesn't match its version, so it wasn't installed.")
        sig = _parse_sig(_fetch_small(info.get("sig_url"), "signature", MAX_SIG) or "")
        if sig is None:
            raise UpdateError(NOT_SIGNED)
    want = str(info.get("sha256") or "").lower() or None
    if want and not re.fullmatch(r"[0-9a-f]{64}", want):
        want = None
    if not want and info.get("sha256_url"):
        m = re.search(r"\b([0-9a-fA-F]{64})\b", _fetch_small(info.get("sha256_url"), "checksum", MAX_SIG) or "")
        want = m.group(1).lower() if m else None
    if not want and not keys:
        # no key yet: the published SHA-256 is the only check, so it must be there
        raise UpdateError("This release has no checksum, so Debrief won't install it automatically. "
                          "Download it from the release page (github.com/blackmagik232/debrief/releases) instead.")
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
    # 3-4. the signature over (version, name, the sha we computed), against any release key
    if keys and not verify_release(latest, name, digest, sig, keys):
        drop()
        raise UpdateError(BAD_SIGNATURE)
    # 5. then the published checksum (with a key set it's an integrity extra; without one it's the check)
    if want and digest != want:
        drop()
        raise UpdateError("The download didn't match its checksum, so it wasn't installed. Try again.")
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
                          "Download it from the release page (github.com/blackmagik232/debrief/releases) instead.") from e


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
                        "installer from the release page (github.com/blackmagik232/debrief/releases). Details: update-install.log in "
                        "Debrief's data folder.")}
