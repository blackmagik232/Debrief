"""Run one test file for CI and, when it fails, report why as GitHub annotations.

CI job logs aren't readable from every place the project is worked on, but annotations are, so a failing
test file writes its FAIL lines, tracebacks' last lines and the output tail as ::error annotations.
Usage: python tests/ci_run.py tests/test_x.py
"""
from __future__ import annotations

import os
import subprocess
import sys


def _esc(s: str) -> str:
    return s.replace("%", "%25").replace("\r", "").replace("\n", "%0A")


# A test must never reach the internet or write into the repo (2.7, task 6a): the updater's state file
# defaults to <repo>/update.json and the update check is a real GitHub request unless a test fakes it.
# DEBRIEF_NO_NETWORK makes the Garage 61 client refuse to ask; the proxy variables point every other HTTP
# library (requests, urllib) at a port nothing listens on, so a real request fails at once instead of
# going out. NO_PROXY keeps the tests' own 127.0.0.1 servers reachable.
DEAD_PROXY = "http://127.0.0.1:9"
LOCAL_ONLY = "127.0.0.1,localhost"


def offline_env(tmp: str) -> dict:
    """Environment for one test file: the updater's state inside tmp, no network."""
    return {"DEBRIEF_UPDATE_STATE": os.path.join(tmp, "update.json"), "DEBRIEF_NO_NETWORK": "1",
            "HTTPS_PROXY": DEAD_PROXY, "HTTP_PROXY": DEAD_PROXY, "https_proxy": DEAD_PROXY, "http_proxy": DEAD_PROXY,
            "NO_PROXY": LOCAL_ONLY, "no_proxy": LOCAL_ONLY}


def run_file(path: str, env_extra: dict | None = None) -> tuple[int, str]:
    """Run one test file; returns (exit code, its output followed by ::error annotations when it failed).
    The file runs with its own temp folder (TEMP/TMP/TMPDIR, unless env_extra sets them) and offline_env()."""
    import shutil
    import tempfile
    extra = dict(env_extra or {})
    own_tmp = None
    if not any(k in extra for k in ("TEMP", "TMP", "TMPDIR")):
        own_tmp = tempfile.mkdtemp(prefix=os.path.splitext(os.path.basename(path))[0] + "-")
        extra.update(TEMP=own_tmp, TMP=own_tmp, TMPDIR=own_tmp)
    tmp = extra.get("TEMP") or extra.get("TMP") or extra.get("TMPDIR")
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", **offline_env(tmp), **extra)
    try:
        p = subprocess.run([sys.executable, path], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           encoding="utf-8", errors="replace")
    finally:
        if own_tmp:
            shutil.rmtree(own_tmp, ignore_errors=True)
    out = p.stdout or ""
    if p.returncode == 0:
        return 0, out
    lines = out.splitlines()
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    notes = []
    fails = [ln for ln in lines if ln.startswith(("FAIL", "ERROR")) or "Error:" in ln or "assert" in ln][:20]
    for ln in fails:
        notes.append(f"::error title={name} failure::{_esc(ln[:900])}")
    tail = "\n".join(lines[-60:])
    for i in range(0, len(tail), 3500):            # annotations are capped in size, so split the tail
        notes.append(f"::error title={name} output tail {i // 3500 + 1}::{_esc(tail[i:i + 3500])}")
    notes.append(f"::error title={name}::exit code {p.returncode}")
    if out and not out.endswith("\n"):
        out += "\n"
    return p.returncode or 1, out + "\n".join(notes) + "\n"


def _utf8_console():
    for stream in (sys.stdout, sys.stderr):            # a cp1252 console mustn't crash the reporter itself
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main() -> int:
    _utf8_console()
    rc, out = run_file(sys.argv[1])
    sys.stdout.write(out)
    sys.stdout.flush()
    return rc


if __name__ == "__main__":
    sys.exit(main())
