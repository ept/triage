"""The script that tries the assistant with a real model, here with a scripted one."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from triage.testing import evaluate
from triage.testing.fake_llm import Call, ScriptedLlm


@pytest.fixture
def model() -> Iterator[tuple[str, ScriptedLlm]]:
    """A scripted model behind a real HTTP server, as the script expects to find one."""
    scripted = ScriptedLlm()

    class Handler(BaseHTTPRequestHandler):
        def _answer(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            if self.headers.get("Authorization") != "Bearer key-for-tests":
                self.send_error(401)
                return
            response = scripted.handler(method, self.path, self.rfile.read(length))
            self.send_response(response.status)
            for name, value in response.headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(response.body)))
            self.end_headers()
            self.wfile.write(response.body)

        def do_GET(self) -> None:
            self._answer("GET")

        def do_POST(self) -> None:
            self._answer("POST")

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", scripted
    server.shutdown()
    server.server_close()


def test_a_model_that_does_the_job_passes(model: tuple[str, ScriptedLlm]) -> None:
    url, scripted = model
    scripted.replies += [
        Call("search_mail", {"query": "Garden Weekly"}),
        Call("move_messages", {"ids": ["m1"], "destination": "archive"}),
        "I moved it.",
    ]
    shown: list[str] = []
    (result,) = evaluate.run(url, "key-for-tests", ["action"], shown.append)
    assert result.error is None
    assert result.passed
    assert [ok for _, ok in result.checks] == [True, True, True]
    assert result.calls == 4, "one to sort the request, three to carry it out"
    assert "  ✓ Moved 1 message to Archive" in shown
    assert "  [pass] moved the newsletter" in shown


def test_a_model_that_does_something_else_fails(model: tuple[str, ScriptedLlm]) -> None:
    url, scripted = model
    scripted.replies += [
        Call("search_mail", {"query": "internship"}),
        Call("move_messages", {"ids": ["m1", "m2"], "destination": "trash"}),
        "Kim wants the budget figures.",
    ]
    (result,) = evaluate.run(url, "key-for-tests", ["question"], lambda text: None)
    assert not result.passed
    assert dict(result.checks) == {
        "the answer mentions the budget figures": True,
        "the answer mentions Friday": False,
        "changed nothing": False,
    }


def test_the_full_inbox_has_what_its_checks_expect() -> None:
    scenario = next(s for s in evaluate.SCENARIOS if s.name == "full-inbox")
    names = [mail.name for mail in scenario.mailbox]
    assert len(names) == len(set(names)) == 43
    assert {"cambridge", "eth", "imperial", "mit", "college-a", "college-b", "hochschule"} <= set(
        names
    )
    addresses = [mail.sender[1] for mail in scenario.mailbox if not mail.name.startswith("filler")]
    assert all(address.endswith(".example") or "@example." in address for address in addresses)


def test_a_missing_server_is_reported(capsys: pytest.CaptureFixture[str]) -> None:
    assert evaluate.main(["--url", "http://127.0.0.1:9"]) == 2
    assert "does not answer" in capsys.readouterr().out
