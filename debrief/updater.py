"""Built-in updater for the installed app.

    check(force=False)  -> {"current", "latest", "newer", "notes", "url", "installer", "size", "sha256_url", ...}
    download(info, progress) -> path of the verified installer (temp folder)
    launch(path)        start the installer silently; it closes Debrief, installs and reopens it

Debrief asks GitHub for the newest release at most once a day (one request, nothing about you is
sent), and only when "check_updates" is on in settings.toml (it is by default). The installer is only
run after its SHA-256 matches the checksum file published with the release.
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
    assets = [a for a in rel.get("assets") or [] if isinstance(a, dict)]
    exe = next((a for a in assets if re.fullmatch(r"Debrief-Setup-[\d.]+\.exe", str(a.get("name") or ""))), None)
    sha = None
    if exe:
        sha = next((a for a in assets if a.get("name") == exe["name"] + ".sha256"), None)
    digest = str((exe or {}).get("digest") or "")
    return {"latest": tag.lstrip("v"), "notes": str(rel.get("body") or "")[:4000], "url": rel.get("html_url"),
            "published": rel.get("published_at"),
            "installer": (exe or {}).get("browser_download_url"), "installer_name": (exe or {}).get("name"),
            "size": (exe or {}).get("size"), "sha256_url": (sha or {}).get("browser_download_url"),
            "sha256": digest.split(":", 1)[1].lower() if digest.startswith("sha256:") else None}


def check(force: bool = False, now: float | None = None) -> dict:
    """What's the newest release? Uses the saved answer if it's under a day old (unless force)."""
    from . import __version__
    now = time.time() if now is None else now
    st = _read_state()
    rel = st.get("release") if isinstance(st.get("release"), dict) else None
    if force or rel is None or now - float(st.get("checked") or 0) > CHECK_EVERY_S:
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
            rel = _from_release(r.json())
        except ValueError as e:
            raise UpdateError("GitHub sent something unexpected. Try again later.") from e
        st = {"checked": now, "release": rel}
        _write_state(st)
    out = dict(rel)
    out.update({"current": __version__, "newer": newer(rel.get("latest"), __version__) and bool(rel.get("installer")),
                "checked": st.get("checked")})
    return out


def download(info: dict, progress=None, dest_dir=None) -> Path:
    """Download the installer and check its SHA-256. Returns its path."""
    import requests
    url = info.get("installer")
    if not url or not str(url).startswith("https://github.com/" + REPO + "/releases/download/"):
        raise UpdateError("This release has no installer to download.")
    want = (info.get("sha256") or "").lower() or None
    if not want and info.get("sha256_url"):
        try:
            r = requests.get(info["sha256_url"], headers=_headers(), timeout=30)
            if r.status_code == 200:
                m = re.search(r"\b([0-9a-fA-F]{64})\b", r.text)
                want = m.group(1).lower() if m else None
        except requests.RequestException:
            want = None
    if not want:
        raise UpdateError("This release has no checksum, so Debrief won't install it automatically. "
                          "Download it from the release page instead.")
    d = Path(dest_dir or tempfile.mkdtemp(prefix="debrief-update-"))
    d.mkdir(parents=True, exist_ok=True)
    out = d / (info.get("installer_name") or "Debrief-Setup.exe")
    h = hashlib.sha256()
    total = int(info.get("size") or 0)
    got = 0
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
        raise UpdateError("The download stopped. Check your connection and try again.") from e
    if total and got != total:
        raise UpdateError("The download didn't finish. Try again.")
    if h.hexdigest().lower() != want:
        try:
            out.unlink()
        except OSError:
            pass
        raise UpdateError("The download didn't match its checksum, so it wasn't installed. Try again.")
    return out


def launch(path) -> None:
    """Start the installer silently and detached. It waits for Debrief to close (Inno Setup's
    CloseApplications), installs, then reopens Debrief (/RELAUNCH=1)."""
    if os.name != "nt":
        raise UpdateError("Updating from inside Debrief only works on Windows.")
    flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([str(path), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/CURRENTUSER", "/NORESTART",
                      "/CLOSEAPPLICATIONS", "/RELAUNCH=1"],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True, creationflags=flags)
