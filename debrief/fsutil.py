"""File helpers shared by everything that saves into the library or settings."""
from __future__ import annotations

import os
import time

# Windows: antivirus, the search indexer, OneDrive or an open Explorer preview can hold a file for a
# moment, and os.replace then fails with "access denied" (winerror 5) or "in use" (winerror 32).
RETRIES = 6
_LOCKED = (5, 32)


def _transient(e: OSError) -> bool:
    return isinstance(e, PermissionError) or getattr(e, "winerror", None) in _LOCKED


def replace(src, dst) -> None:
    """os.replace that retries a briefly locked file up to RETRIES times (50-250 ms apart), then
    re-raises the last error. Any other error is raised straight away."""
    for i in range(RETRIES):
        try:
            os.replace(src, dst)
            return
        except OSError as e:
            if not _transient(e) or i == RETRIES - 1:
                raise
            time.sleep(min(0.25, 0.05 * (i + 1)))
