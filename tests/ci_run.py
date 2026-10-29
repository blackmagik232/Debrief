"""Run one test file for CI and, when it fails, report why as GitHub annotations.

CI job logs aren't readable from every place the project is worked on, but annotations are, so a failing
test file writes its FAIL lines, tracebacks' last lines and the output tail as ::error annotations.
Usage: python tests/ci_run.py tests/test_x.py
"""
import os
import subprocess
import sys


def _esc(s: str) -> str:
    return s.replace("%", "%25").replace("\r", "").replace("\n", "%0A")


def main() -> int:
    for stream in (sys.stdout, sys.stderr):            # a cp1252 console mustn't crash the reporter itself
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    path = sys.argv[1]
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    p = subprocess.run([sys.executable, path], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       encoding="utf-8", errors="replace")
    out = p.stdout or ""
    sys.stdout.write(out)
    sys.stdout.flush()
    if p.returncode == 0:
        return 0
    lines = out.splitlines()
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    fails = [ln for ln in lines if ln.startswith(("FAIL", "ERROR")) or "Error:" in ln or "assert" in ln][:20]
    for ln in fails:
        print(f"::error title={name} failure::{_esc(ln[:900])}")
    tail = "\n".join(lines[-60:])
    for i in range(0, len(tail), 3500):            # annotations are capped in size, so split the tail
        print(f"::error title={name} output tail {i // 3500 + 1}::{_esc(tail[i:i + 3500])}")
    print(f"::error title={name}::exit code {p.returncode}")
    return p.returncode or 1


if __name__ == "__main__":
    sys.exit(main())
