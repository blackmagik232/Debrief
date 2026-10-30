"""Run every tests/test_*.py for CI, several files at a time, with ci_run.py's reporting.

    python tests/run_all.py            # all files, workers = min(4, CPUs)
    python tests/run_all.py -j 1       # one at a time
    python tests/run_all.py tests/test_sync.py tests/test_library.py

Each file still runs in its own Python process (exactly as `python tests/ci_run.py <file>`), with its own
temp folder (TEMP/TMP/TMPDIR point at a fresh folder per file, so files never share temp files; servers in
the tests bind port 0). A file's output is printed in one block when it finishes, inside a ::group:: so the
log stays readable, and a failing file adds the same ::error annotations ci_run.py writes.

A file that fails while others run beside it is run once more on its own: if it passes alone, that is
reported as a ::warning (a timing check squeezed by the parallel run), not hidden; if it fails again, the
run fails. New test files are picked up automatically, so nothing has to be added to the workflow.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ci_run  # noqa: E402

# the slowest files first, so the long ones don't start last (the rest follow in name order)
SLOW_FIRST = ("test_24_fixes.py", "test_fix_a.py", "test_fix_d.py", "test_25_c.py", "test_23_c.py",
              "test_fix_c.py", "test_24_c.py", "test_25_d.py", "test_230_fixes.py", "test_integration.py",
              "test_library.py")


def discover() -> list[str]:
    files = sorted(p.name for p in HERE.glob("test_*.py"))
    first = [f for f in SLOW_FIRST if f in files]
    return [str(Path("tests") / f) for f in first + [f for f in files if f not in first]]


def ci_runs(workflow: str, filename: str) -> bool:
    """True when the workflow text runs tests/<filename>: its own ci_run.py step, or this runner (which
    runs every tests/test_*.py) in a step that comes before the PyInstaller build."""
    if f"python tests/ci_run.py tests/{filename}" in workflow or f"python tests/{filename}" in workflow:
        return True
    i = workflow.find("python tests/run_all.py")
    b = workflow.find("Build with PyInstaller")
    return i >= 0 and (b < 0 or i < b) and (HERE / filename).is_file() and filename.startswith("test_")


def _run(path: str, tmp_root: Path) -> tuple[int, str, float]:
    tmp = Path(tempfile.mkdtemp(prefix=Path(path).stem + "-", dir=tmp_root))
    t = time.perf_counter()
    try:
        rc, out = ci_run.run_file(path, {"TEMP": str(tmp), "TMP": str(tmp), "TMPDIR": str(tmp)})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return rc, out, time.perf_counter() - t


def main(argv=None) -> int:
    ci_run._utf8_console()
    ap = argparse.ArgumentParser()
    ap.add_argument("-j", "--jobs", type=int, default=min(4, os.cpu_count() or 1))
    ap.add_argument("files", nargs="*")
    a = ap.parse_args(argv)
    os.chdir(HERE.parent)
    files = a.files or discover()
    tmp_root = Path(tempfile.mkdtemp(prefix="debrief-tests-"))
    lock = threading.Lock()
    results = {}

    def one(path):
        rc, out, secs = _run(path, tmp_root)
        name = Path(path).name
        with lock:
            results[path] = (rc, secs)
            ok = "ok" if rc == 0 else f"FAILED (exit {rc})"
            # a failure first run in parallel is re-run alone below, so its annotations wait for that run
            body = out if (rc == 0 or a.jobs == 1) else out.replace("::error ", "error: ")
            sys.stdout.write(f"::group::{name} {ok} ({secs:.1f}s)\n{body}\n::endgroup::\n")
            sys.stdout.flush()

    t0 = time.perf_counter()
    try:
        with ThreadPoolExecutor(max_workers=max(1, a.jobs)) as ex:
            list(ex.map(one, files))
        failed = [p for p in files if results[p][0] != 0]
        hard = []
        for path in (failed if a.jobs > 1 else []):
            name = Path(path).name
            rc, out, secs = _run(path, tmp_root)
            sys.stdout.write(f"::group::{name} re-run alone: {'ok' if rc == 0 else 'FAILED'} ({secs:.1f}s)\n{out}\n::endgroup::\n")
            if rc == 0:
                print(f"::warning title={name}::failed beside other test files but passed on its own; "
                      f"a timing check may be too tight for a busy machine")
            else:
                hard.append(path)
        if a.jobs == 1:
            hard = failed
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
    print(f"\n{'file':<28}{'secs':>7}  result")
    for p in sorted(files, key=lambda p: -results[p][1]):
        rc, secs = results[p]
        print(f"{Path(p).name:<28}{secs:7.1f}  {'ok' if rc == 0 else 'FAILED' + (' (passed alone)' if p not in hard else '')}")
    print(f"{len(files) - len(hard)}/{len(files)} files passed in {time.perf_counter() - t0:.0f}s with {a.jobs} at a time")
    if hard:
        print(f"::error title=tests::{len(hard)} test file(s) failed: " + ", ".join(Path(p).name for p in hard))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
