"""Fixtures for the browser tests of the web interface.

The tests drive the system's Chromium through Playwright against the stub backend in
``stub_server.py``. They are skipped where there is no Chromium.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page, sync_playwright
from stub_server import StubBackend, StubServer

CHROMIUM = os.environ.get("TRIAGE_CHROMIUM", "/usr/bin/chromium")

# Paths that the interface may request from its backend; anything else means that some
# untrusted text was interpreted as markup.
STATIC_PREFIXES = ("/js/",)
STATIC_PATHS = ("/", "/app.css")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if Path(CHROMIUM).is_file():
        return
    skip = pytest.mark.skip(reason=f"{CHROMIUM} is not installed")
    for item in items:
        if "browser" in getattr(item, "fixturenames", ()):
            item.add_marker(skip)


@pytest.fixture(scope="session")
def browser() -> Iterator[Browser]:
    with sync_playwright() as playwright:
        chromium = playwright.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"])
        yield chromium
        chromium.close()


@dataclass
class Session:
    """A page showing the interface, the backend behind it, and what went wrong on the page."""

    page: Page
    backend: StubBackend
    url: str
    console_errors: list[str] = field(default_factory=list)

    def open(self, fragment: str = "") -> Page:
        self.page.goto(f"{self.url}/{fragment}")
        self.page.wait_for_selector("#status-body .llm")
        return self.page

    def policy_violations(self) -> list[str]:
        return list(self.page.evaluate("window.__violations || []"))

    def pwned(self) -> bool:
        return bool(self.page.evaluate("Boolean(window.__pwned)"))

    def unexpected_requests(self) -> list[str]:
        return [
            path
            for path in self.backend.paths()
            if not (
                path in STATIC_PATHS
                or path.startswith(STATIC_PREFIXES)
                or path.startswith("/api/")
                or path == "/healthz"
            )
        ]

    def assert_clean(self) -> None:
        """Nothing was logged as an error, the policy was never violated, no stray requests."""
        assert self.console_errors == []
        assert self.policy_violations() == []
        assert self.unexpected_requests() == []
        assert not self.pwned()


def _session(browser: Browser, backend: StubBackend) -> Iterator[Session]:
    server = StubServer(backend)
    context = browser.new_context(viewport={"width": 1300, "height": 900})
    page = context.new_page()
    page.set_default_timeout(10_000)
    # Record violations of the Content-Security-Policy in the page (and in frames that are
    # allowed to run this script, which the email frame is not).
    page.add_init_script(
        "window.__violations = [];"
        "document.addEventListener('securitypolicyviolation',"
        " (e) => window.top.__violations.push(e.violatedDirective + ' ' + e.blockedURI));"
    )
    session = Session(page, backend, server.url)
    page.on(
        "console",
        lambda message: (
            session.console_errors.append(message.text) if message.type == "error" else None
        ),
    )
    page.on("pageerror", lambda error: session.console_errors.append(str(error)))
    try:
        yield session
    finally:
        context.close()
        server.close()


@pytest.fixture
def ui(browser: Browser) -> Iterator[Session]:
    yield from _session(browser, StubBackend())


@pytest.fixture
def hostile_ui(browser: Browser) -> Iterator[Session]:
    yield from _session(browser, StubBackend(hostile=True))
