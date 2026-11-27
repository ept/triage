"""The whole chain in a browser: the real gateway's relay in front of the real app.

The app runs with made-up mail and a pretend model (``triage.testing.demo``); the gateway is
the real binary, used here for what the wrapper app will use it for: the session cookie, the
relay to the web interface, and the Content-Security-Policy that it adds. The test fails on any
console error or policy violation, which is how a mismatch between the interface and the
gateway's policy would show up.

Needs ``TRIAGE_GATEWAY_BIN`` and Chromium; skipped otherwise.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

CHROMIUM = os.environ.get("TRIAGE_CHROMIUM", "/usr/bin/chromium")
GATEWAY = os.environ.get("TRIAGE_GATEWAY_BIN", "")
UI_TOKEN = "u" * 40

pytestmark = pytest.mark.skipif(
    not (GATEWAY and Path(GATEWAY).is_file() and Path(CHROMIUM).is_file()),
    reason="needs TRIAGE_GATEWAY_BIN and Chromium",
)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_for_port(port: int, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"process exited with status {process.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"nothing is listening on port {port}")


@pytest.fixture
def relay_url(tmp_path: Path) -> Iterator[str]:
    app_port, ui_port = _free_port(), _free_port()
    demo = subprocess.Popen(
        [sys.executable, "-m", "triage.testing.demo", "--port", str(app_port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    gateway = subprocess.Popen(
        [GATEWAY],
        env={
            "TRIAGE_CONTROL_TOKEN": "c" * 40,
            "TRIAGE_UI_TOKEN": UI_TOKEN,
            "TRIAGE_SANDBOX_ADDR": f"127.0.0.1:{_free_port()}",
            "TRIAGE_CONTROL_ADDR": f"127.0.0.1:{_free_port()}",
            "TRIAGE_UI_ADDR": f"127.0.0.1:{ui_port}",
            "TRIAGE_DATA_DIR": str(tmp_path / "gateway"),
            "TRIAGE_APP_URL": f"http://127.0.0.1:{app_port}",
        },
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_port(app_port, demo)
        _wait_for_port(ui_port, gateway)
        yield f"http://127.0.0.1:{ui_port}"
    finally:
        for process in (gateway, demo):
            process.terminate()
        for process in (gateway, demo):
            process.wait(timeout=10)


def test_the_interface_works_through_the_gateway(relay_url: str) -> None:
    from playwright.sync_api import expect, sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"])
        page = browser.new_page()
        problems: list[str] = []
        page.on("console", lambda m: problems.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: problems.append(str(e)))
        requests: list[str] = []
        page.on("request", lambda r: requests.append(r.url))

        # Without the session nothing is served.
        assert page.request.get(f"{relay_url}/").status == 401

        # The wrapper app's first URL: sets the cookie and lands on the interface.
        response = page.goto(f"{relay_url}/_triage/session?token={UI_TOKEN}")
        assert response is not None
        assert response.status == 200
        assert page.url == f"{relay_url}/"
        policy = response.headers["content-security-policy"]
        assert "default-src 'none'" in policy
        expect(page.locator("#status-body .llm")).to_be_visible()
        expect(page.locator("#status-body")).to_contain_text("robin@example.org")

        # Ask a question: the answer streams through the relay.
        page.locator("#composer-text").fill("Anything about the lighthouse?")
        page.locator("#composer-send").click()
        answer = page.locator(".item-assistant .bubble").last
        expect(answer).to_contain_text("Here is what I found", timeout=20_000)
        expect(page.locator(".item-user .bubble")).to_have_text("Anything about the lighthouse?")

        # Open a message that the answer refers to.
        page.locator(".item-assistant .handle").first.click()
        viewer = page.locator("#viewer")
        expect(viewer).to_be_visible()
        expect(viewer).to_contain_text("ighthouse")

        # The other views load from the real backend.
        page.goto(f"{relay_url}/#policy")
        expect(page.locator("#policy-automatic")).not_to_be_checked()
        page.locator("#policy-text").fill("Archive newsletters after a week.\n")
        page.locator("#policy-save").click()
        expect(page.locator("#policy-history li button").first).to_contain_text(
            "Edited by the user"
        )
        page.goto(f"{relay_url}/#activity")
        expect(page.locator("#view-activity")).to_be_visible()
        page.goto(f"{relay_url}/#coding")
        expect(page.locator("#view-coding")).to_be_visible()

        assert problems == [], problems
        foreign = [url for url in requests if not url.startswith(relay_url)]
        assert foreign == [], "the page asked for something outside the app"
        browser.close()
