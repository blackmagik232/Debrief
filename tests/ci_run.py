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


def run_file(path: str, env_extra: dict | None = None) -> tuple[int, str]:
    """Run one test file; returns (exit code, its output followed by ::error annotations when it failed)."""
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", **(env_extra or {}))
    p = subprocess.run([sys.executable, path], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       encoding="utf-8", errors="replace")
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
