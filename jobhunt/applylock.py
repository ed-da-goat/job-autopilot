"""Cross-process mutex for apply sessions.

`applysession.py`'s `_PROC` global only prevents two starts within the same
process — it cannot stop a UI-launched session from racing a manual CLI run
(both spawn Chrome against the same profile). `held()` takes an exclusive
`flock` on `data/apply.lock` so at most one apply session — CLI or
UI-launched — runs at a time, across processes.

flock is held on the process's open file descriptor and released by the
kernel the moment that descriptor closes, including on a hard kill or crash.
There is no lockfile-staleness case to handle here.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

DEFAULT_LOCK_PATH = Path("data/apply.lock")


class ApplySessionActiveError(RuntimeError):
    """Raised when another process already holds the apply-session lock."""


@contextmanager
def held(lock_path: Path = DEFAULT_LOCK_PATH) -> Iterator[None]:
    """Hold an exclusive, non-blocking lock on `lock_path` for the block.

    Raises ApplySessionActiveError immediately if another process holds it.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ApplySessionActiveError(
                "another apply session is already running — one at a time"
            ) from exc
        yield
    finally:
        os.close(fd)  # releases the flock, even on a hard kill of this process
