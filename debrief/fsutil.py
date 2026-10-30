"""File helpers shared by everything that saves into the library or settings."""
from __future__ import annotations

import os
import time

# Windows: antivirus, the search indexer, OneDrive or an open Explorer preview can hold a file for a
# moment, and os.replace then fails with "access denied" (winerror 5) or "in use" (winerror 32).
# OneDrive and Defender often hold a file for 1-3 s, so keep trying for about 5 s in all.
RETRIES = 10
_LOCKED = (5, 32)


def _delay(i: int) -> float:
    """Wait before try i+2: 0.05, 0.1, 0.2, 0.4, 0.8, then 1.0 s (about 5.5 s over 10 tries)."""
    return min(1.0, 0.05 * (2 ** i))


def _transient(e: OSError) -> bool:
    return isinstance(e, PermissionError) or getattr(e, "winerror", None) in _LOCKED


def replace(src, dst, tries: int = RETRIES) -> None:
    """os.replace that retries a briefly locked file up to `tries` times (backing off from 50 ms to 1 s),
    then re-raises the last error. Any other error is raised straight away."""
    tries = max(1, int(tries))
    for i in range(tries):
        try:
            os.replace(src, dst)
            return
        except OSError as e:
            if not _transient(e) or i == tries - 1:
                raise
            time.sleep(_delay(i))


def atomic_write_text(path, text: str, fsync: bool = False, tries: int = RETRIES) -> None:
    """Write text to path via a temp file in the same folder + replace(), so readers only ever see the old
    file or the whole new one. fsync=True also flushes it to disk first (for files that are the only copy)."""
    import tempfile
    from pathlib import Path
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            if fsync:
                f.flush()
                os.fsync(f.fileno())
        replace(tmp, path) if tries == RETRIES else replace(tmp, path, tries)   # 2 args: tests stand in for replace
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
