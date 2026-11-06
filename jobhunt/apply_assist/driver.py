"""Playwright browser session management for apply-assist.

Two modes:
  - CDP attach (JOBHUNT_CDP_URL set): connect to the candidate's already-running
    Chrome so he keeps his own profile/session cookies. We never own that
    browser's lifecycle — on close() we only disconnect.
  - Persistent local profile (default): launch a visible, persistent Chrome
    (or bundled Chromium fallback) so login state survives between runs.

Nothing here submits a form. This module only opens a page and returns a
handle; all field interaction lives in fill.py.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from jobhunt.apply_assist.detect import application_url

logger = logging.getLogger(__name__)

_PROFILE_DIR = Path("data/apply_browser_profile")

# Chrome flags for the persistent profile. The first two hide the restore
# bubble after an unclean exit. --disable-updater-scheduler stops branded Chrome
# from starting GoogleUpdater, whose process inherits the stderr pipe Playwright
# reads: without it, context.close() waited up to 5 minutes for the updater to
# exit. Builds that do not know a switch ignore it.
_LAUNCH_ARGS = [
    "--hide-crash-restore-bubble",
    "--disable-session-crashed-bubble",
    "--disable-updater-scheduler",
]


def open_browser() -> tuple[Any, Any, bool]:
    """Open a visible browser and return its context without navigating.

    Returns (playwright_handle, context, is_cdp). Same CDP-vs-persistent
    selection as open_page(); callers that need multiple tabs (the batch
    queue) drive `context.new_page()` themselves per posting.
    """
    cdp_url = os.environ.get("JOBHUNT_CDP_URL")
    if cdp_url:
        return _open_context_via_cdp(cdp_url)
    return _open_context_via_persistent()


def open_page(url: str) -> tuple[Any, Any, bool]:
    """Open `url` in a visible browser tab.

    Returns (playwright_handle, page, is_cdp). `playwright_handle` and `page`
    are opaque to callers other than close(); pass them straight through.
    """
    handle, context, is_cdp = open_browser()
    page = (
        context.new_page()
        if is_cdp
        else (context.pages[0] if context.pages else context.new_page())
    )
    page.goto(application_url(url), wait_until="domcontentloaded")
    return handle, page, is_cdp


def _open_context_via_cdp(cdp_url: str) -> tuple[Any, Any, bool]:
    pw = sync_playwright().start()
    browser = pw.chromium.connect_over_cdp(cdp_url)
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    return pw, context, True


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # exists but not ours (EPERM etc.) — treat as alive
    return True


def _cleanup_stale_singleton(profile_dir: Path) -> None:
    """Remove Chrome's Singleton{Lock,Cookie,Socket} symlinks when the lock's
    owner is dead. A crashed/killed Chrome leaves them behind and the next
    launch fails even though Chrome is installed. The lock target is
    "<hostname>-<pid>"; anything unparseable is left alone."""
    lock = profile_dir / "SingletonLock"
    if not lock.is_symlink():
        return
    try:
        target = os.readlink(lock)
    except OSError:
        return
    pid_part = target.rsplit("-", 1)[-1]
    if not pid_part.isdigit() or _pid_alive(int(pid_part)):
        return
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            (profile_dir / name).unlink(missing_ok=True)
        except OSError:
            pass


def _scrub_crash_restore(profile_dir: Path) -> None:
    """Stop Chrome from silently restoring a crashed session's tabs.

    Hard-killed sessions leave exit_type="Crashed" in Preferences plus
    Default/Sessions/* tab state; on the next launch Chrome lazily
    restores that pile as unattached tabs Playwright cannot see. Runs
    only when no Chrome holds the profile (caller guarantees the
    singleton scrub ran first). Cookies/Login Data are untouched.
    """
    sessions_dir = profile_dir / "Default" / "Sessions"
    if sessions_dir.exists():
        total_bytes = sum(
            f.stat().st_size for f in sessions_dir.rglob("*") if f.is_file()
        )
        total_mb = total_bytes / (1024 * 1024)
        if total_mb > 10:
            logger.warning(
                "apply profile Sessions dir %.1f MB — restore-pile rot",
                total_mb,
            )
        else:
            logger.debug("apply profile Sessions dir %.1f MB", total_mb)
        shutil.rmtree(sessions_dir, ignore_errors=True)

    prefs = profile_dir / "Default" / "Preferences"
    if not prefs.exists():
        return
    try:
        with prefs.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        logger.debug("apply profile Preferences unreadable — skipping scrub")
        return

    profile = data.get("profile")
    if not isinstance(profile, dict):
        return
    if profile.get("exit_type") != "Crashed" and profile.get("exited_cleanly") is not False:
        return

    profile["exit_type"] = "Normal"
    profile["exited_cleanly"] = True
    tmp_path = prefs.with_suffix(".tmp-scrub")
    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp_path, prefs)
    except OSError:
        logger.debug("apply profile Preferences scrub write failed")
        tmp_path.unlink(missing_ok=True)


def _open_context_via_persistent() -> tuple[Any, Any, bool]:
    _PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    _cleanup_stale_singleton(_PROFILE_DIR)
    _scrub_crash_restore(_PROFILE_DIR)
    channel = os.environ.get("JOBHUNT_BROWSER_CHANNEL", "chrome")
    pw = sync_playwright().start()
    try:
        # Keep Chrome's popup blocker: ATS page scripts window.open() blank
        # tabs that steal focus from the filled form.
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(_PROFILE_DIR),
            headless=False,
            channel=channel,
            args=list(_LAUNCH_ARGS),
            ignore_default_args=["--disable-popup-blocking"],
        )
    except Exception as channel_exc:
        try:
            context = pw.chromium.launch_persistent_context(
                user_data_dir=str(_PROFILE_DIR),
                headless=False,
                args=list(_LAUNCH_ARGS),
                ignore_default_args=["--disable-popup-blocking"],
            )
        except Exception as exc:
            pw.stop()
            raise RuntimeError(
                "Could not launch a visible browser. "
                f"{channel} channel failed: {channel_exc}; "
                f"bundled Chromium failed: {exc}. "
                "If neither browser is installed, run "
                "`uv run playwright install chromium` and try again."
            ) from exc
    return pw, context, False


def close(handle: Any, page: Any, is_cdp: bool) -> None:
    """Tear down the browser session.

    `handle` is the `playwright_handle` returned by open_page(); `page` is
    the same page returned alongside it. Never closes the candidate's own browser —
    a CDP session only disconnects (browser.close() on a CDP-connected
    Browser disconnects rather than terminating the remote process), leaving
    his window and tabs open.
    """
    if is_cdp:
        try:
            browser = page.context.browser
            if browser is not None:
                browser.close()
        finally:
            handle.stop()
        return

    try:
        page.context.close()
    finally:
        handle.stop()
