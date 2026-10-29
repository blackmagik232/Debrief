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


def replace(src, dst) -> None:
    """os.replace that retries a briefly locked file up to RETRIES times (backing off from 50 ms to 1 s),
    then re-raises the last error. Any other error is raised straight away."""
    for i in range(RETRIES):
        try:
            os.replace(src, dst)
            return
        except OSError as e:
            if not _transient(e) or i == RETRIES - 1:
                raise
            time.sleep(_delay(i))
