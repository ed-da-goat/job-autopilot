from __future__ import annotations

import fcntl
import os

import pytest
from jobhunt import applylock


def test_flock_blocks_a_second_independent_fd(tmp_path):
    """Raw OS-level check, independent of applylock's own wrapping: a lock
    held on one fd must block a *different* fd opened on the same path.
    Re-flocking the SAME fd is a no-op in the kernel and would falsely pass
    even with no real mutex, so this opens two separate os.open() fds."""
    lock_path = tmp_path / "apply.lock"
    first_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    second_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(first_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            fcntl.flock(second_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(first_fd)
        os.close(second_fd)


def test_second_held_call_raises_domain_error(tmp_path):
    """`held()` opens a fresh, independent fd on every call, so a second
    call while the first is still active must hit the same real OS
    contention as the raw-fd test above, wrapped as the domain error."""
    lock_path = tmp_path / "apply.lock"

    with applylock.held(lock_path):
        with pytest.raises(applylock.ApplySessionActiveError, match="one at a time"):
            with applylock.held(lock_path):
                pass


def test_domain_error_message(tmp_path):
    lock_path = tmp_path / "apply.lock"

    with applylock.held(lock_path):
        with pytest.raises(applylock.ApplySessionActiveError) as excinfo:
            with applylock.held(lock_path):
                pass

    assert str(excinfo.value) == "another apply session is already running — one at a time"


def test_lock_releases_after_context_exit(tmp_path):
    """Once the first holder's `with` block exits, a fresh acquisition
    must succeed — and a third, after the second also exits, must too."""
    lock_path = tmp_path / "apply.lock"

    with applylock.held(lock_path):
        pass

    with applylock.held(lock_path):
        pass

    with applylock.held(lock_path):
        pass


def test_release_happens_even_when_body_raises(tmp_path):
    lock_path = tmp_path / "apply.lock"

    with pytest.raises(ValueError):
        with applylock.held(lock_path):
            raise ValueError("boom")

    # The lock must not still be held after an exception inside the block.
    with applylock.held(lock_path):
        pass


def test_apply_session_active_error_is_a_runtime_error():
    # jobhunt/web/api.py's `except RuntimeError` handler around
    # applysession.start() must keep catching this without modification.
    assert issubclass(applylock.ApplySessionActiveError, RuntimeError)
