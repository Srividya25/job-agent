"""Attach to the user's own Chrome over CDP.

Deliberately does NOT launch a fresh Playwright browser. A clean Chromium has
no cookies, so every run would mean logging into Workday, Greenhouse and Ashby
again — the single biggest annoyance of the previous implementation.

Instead a dedicated Chrome profile is launched once, logged into the job
portals once, and reused forever. It is a separate profile from everyday
browsing, so bookmarks, tabs and sessions are untouched.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path

import httpx
from playwright.async_api import Browser, Page, async_playwright

from ..config import ROOT

DEBUG_PORT = 9222
PROFILE_DIR = ROOT / "chrome-profile"

CHROME_PATHS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
]

# Chrome throttles timers in backgrounded windows, which makes automation crawl
# or time out when the window is parked on another desktop — which is exactly
# how this is meant to be used.
LAUNCH_FLAGS = [
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--no-first-run",
    "--no-default-browser-check",
]


class BrowserNotRunning(RuntimeError):
    pass


def chrome_binary() -> str | None:
    for path in CHROME_PATHS:
        if Path(path).exists():
            return path
    return shutil.which("google-chrome") or shutil.which("chromium")


def port_open(port: int = DEBUG_PORT, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        return sock.connect_ex((host, port)) == 0


def is_running(port: int = DEBUG_PORT) -> bool:
    """A CDP endpoint that actually answers, not merely an open socket."""
    if not port_open(port):
        return False
    try:
        response = httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=2.0)
        return response.status_code == 200
    except httpx.HTTPError:
        return False


def launch(port: int = DEBUG_PORT, headless: bool = False) -> subprocess.Popen | None:
    """Start the dedicated Chrome profile with remote debugging enabled.

    A separate --user-data-dir is mandatory, not a preference: Chrome 136+
    refuses --remote-debugging-port on the default profile directory. That
    restriction happens to give us the better design anyway.
    """
    if is_running(port):
        return None

    binary = chrome_binary()
    if not binary:
        raise BrowserNotRunning(
            "Chrome not found. Install Google Chrome, or set CHROME_PATH."
        )

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    command = [
        binary,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={PROFILE_DIR}",
        *LAUNCH_FLAGS,
    ]
    if headless:
        command.append("--headless=new")

    # Launch the binary directly. `open -g -a "Google Chrome" --args …` was
    # tried to avoid activating the app, but macOS ignores --args when Chrome
    # is already running (the user's own browser almost always is), so the
    # debug port never opened at all.
    #
    # This costs one focus event, at launch only. It does not recur: the
    # is_running() guard above means Chrome starts once and every later run
    # attaches to it silently, which is what actually matters while working.
    return subprocess.Popen(
        command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


async def wait_until_ready(port: int = DEBUG_PORT, timeout: float = 20.0) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if is_running(port):
            return True
        await asyncio.sleep(0.3)
    return False


@dataclass
class Session:
    """A live connection to the user's Chrome."""

    browser: Browser
    _playwright: object
    _work_page: Page | None = None

    async def close(self) -> None:
        # Tabs we opened are closed here. Previously each run created a page
        # and left it behind; eighteen had accumulated before this was caught.
        if self._work_page is not None:
            try:
                await self._work_page.close()
            except Exception:  # noqa: BLE001 - already gone is fine
                pass
            self._work_page = None

        # Verified: on a CDP-attached browser this only drops the connection.
        # Chrome keeps running with its logged-in sessions intact.
        await self.browser.close()
        await self._playwright.stop()  # type: ignore[attr-defined]

    async def detach(self) -> None:
        """Drop the connection, leaving tabs alone *if* we only attached.

        This does not survive the CDP fallback. When Playwright launched the
        browser itself — which is now the usual path, since Chrome 151 refuses
        connect_over_cdp — the browser is a child of the driver and dies with
        it. Leaving a filled form on screen therefore means keeping the
        process alive, which is what `job-agent with-me` does.
        """
        self._work_page = None
        await self._playwright.stop()  # type: ignore[attr-defined]

    @property
    def pages(self) -> list[Page]:
        return [page for context in self.browser.contexts for page in context.pages]

    async def active_page(self) -> Page:
        """The frontmost usable tab, or a new one.

        Chrome's DevTools protocol does not expose "the focused tab" directly,
        so the last non-blank page wins — which matches the tab a user was
        just looking at in practice.
        """
        candidates = [
            page for page in self.pages
            if not page.url.startswith(("chrome://", "devtools://", "about:"))
        ]
        if candidates:
            return candidates[-1]

        context = self.browser.contexts[0] if self.browser.contexts else None
        if context is None:
            raise BrowserNotRunning("Chrome has no browser context.")
        return await context.new_page()

    async def work_page(self) -> Page:
        """The single tab this run uses, created once and reused.

        Never calls bring_to_front() — doing so would yank the user's focus
        away from whatever they are actually doing. Keep it that way.
        """
        if self._work_page is not None and not self._work_page.is_closed():
            return self._work_page

        context = self.browser.contexts[0] if self.browser.contexts else None
        if context is None:
            raise BrowserNotRunning("Chrome has no browser context.")
        self._work_page = await context.new_page()
        return self._work_page

    async def open(self, url: str) -> Page:
        """Navigate the work tab to a URL, reusing it rather than adding one."""
        page = await self.work_page()
        await page.goto(url, wait_until="domcontentloaded")
        return page

    async def sweep_leftover_tabs(self, keep: int = 1) -> int:
        """Close tabs orphaned by earlier runs.

        This profile is only used for applying, so anything beyond the first
        few tabs is leaked state rather than something the user is reading.
        The first `keep` tabs are preserved — those are typically portal
        logins worth not disturbing.
        """
        pages = self.pages
        closed = 0
        for page in pages[keep:]:
            if page is self._work_page:
                continue
            try:
                await page.close()
                closed += 1
            except Exception:  # noqa: BLE001
                pass
        return closed


async def attach(port: int = DEBUG_PORT) -> Session:
    """A browser to work in: the running Chrome if possible, else launch one.

    Attaching over CDP is preferred — it reuses the window the user already
    has open and logged in. But Playwright issues `Browser.setDownloadBehavior`
    on every CDP attach, and Chrome 151 rejects it outright
    ("Browser context management is not supported"), so on current Chrome the
    attach fails no matter how healthy the browser is.

    The fallback drives the *same* profile directory directly, so the portal
    logins that make this whole approach worth having are still there. The one
    thing it cannot do is share a profile with a running Chrome — Chrome locks
    the directory — so that case is reported as something to fix rather than
    guessed at.
    """
    playwright = await async_playwright().start()
    try:
        browser = await _open_browser(playwright, port)
    except BaseException:
        await playwright.stop()
        raise
    return Session(browser=browser, _playwright=playwright)


async def _open_browser(playwright, port: int) -> Browser:
    cdp_error: Exception | None = None
    if is_running(port):
        try:
            return await playwright.chromium.connect_over_cdp(
                f"http://127.0.0.1:{port}"
            )
        except Exception as exc:  # noqa: BLE001 - fall back rather than fail
            cdp_error = exc

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        context = await playwright.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=False, channel="chrome", args=LAUNCH_FLAGS
        )
    except Exception as exc:  # noqa: BLE001 - translate into something actionable
        if cdp_error is not None and is_running(port):
            raise BrowserNotRunning(
                "Could not attach to the running Chrome:\n"
                f"  {str(cdp_error)[:160]}\n"
                "and could not launch the profile either, because that Chrome "
                "is holding it.\n"
                "  Quit the Chrome window `job-agent chrome` opened, then "
                "re-run — it will launch its own."
            ) from exc
        raise BrowserNotRunning(f"Could not start Chrome: {str(exc)[:200]}") from exc

    browser = context.browser
    if browser is None:  # pragma: no cover - persistent contexts always have one
        raise BrowserNotRunning("Chrome started but exposed no browser handle.")
    return browser
