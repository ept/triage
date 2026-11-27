"""A stand-in for the app's backend, for testing the web interface in a browser.

It implements the HTTP API that the web interface uses, with canned synthetic data (invented
people at ``example`` domains), serves the static files of the interface, and attaches to every
response the Content-Security-Policy that the gateway attaches in production, so that a test
fails if the interface relies on anything the policy forbids.

Every request is recorded in :attr:`StubBackend.requests`.

With ``hostile=True`` every text field that the backend controls carries markup that would run
script or fetch resources if the interface ever interpreted it, and the "sanitised" HTML of the
email is not sanitised at all.

Run ``python tests/ui/stub_server.py [--hostile] [--port N]`` to try the interface by hand.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

WEB_DIR = Path(__file__).resolve().parents[2] / "src" / "triage" / "web"

# Must stay identical to CONTENT_SECURITY_POLICY in trusted/gateway/src/host.rs.
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; font-src 'self' data:; media-src 'self' data: blob:; "
    "connect-src 'self'; frame-src 'self' blob:; child-src 'self' blob:; worker-src 'self'; "
    "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
)

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
}

# Markup that must never be interpreted. If any of it were, it would set `window.__pwned`,
# request `/x` or `/leak` from this server, or try to reach evil.example.
HOSTILE = (
    ' <img src=x onerror="window.__pwned=1"><script>window.__pwned=1</script>'
    " [click me](javascript:window.__pwned=1) ![pic](https://evil.example/pixel.png)"
    ' <a href="javascript:window.__pwned=1">link</a>'
    ' <iframe src="https://evil.example/"></iframe> <svg onload="window.__pwned=1">'
    " https://evil.example/bare-url"
)

HOSTILE_EMAIL_HTML = (
    "<p>Unsanitised on purpose.</p>"
    "<script>parent.__pwned = 1; top.__pwned = 1; window.__pwned = 1;</script>"
    '<img src="/leak" onerror="parent.__pwned=1">'
    '<img src="https://evil.example/pixel.png">'
    '<link rel="stylesheet" href="/leak.css">'
    '<form action="/leak" method="post"><input name="a"></form>'
    '<a href="/leak" target="_top">leave</a>'
    '<a href="/leak" id="stay">stay</a>'
    '<meta http-equiv="refresh" content="0; url=/leak">'
)

TIME = "2026-11-25T09:30:00+00:00"


@dataclass
class Failure:
    """Makes the next request to a path fail."""

    status: int
    code: str
    message: str


@dataclass
class StubBackend:
    hostile: bool = False
    #: Seconds between the events of a streamed chat reply.
    stream_delay: float = 0.12
    requests: list[tuple[str, str, Any]] = field(default_factory=list)
    failures: dict[tuple[str, str], Failure] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        t = self.t
        self.sync_running = False
        self.next_item = 100
        self.cards = {
            "m1": self._card("m1", "Alex Sample", "alex@example.com", "Budget figures", True, True),
            "m2": self._card("m2", "Example Weekly", "news@newsletter.example", "This week", False),
            "m3": self._card(
                "m3", "Kim Placeholder", "kim@example.net", "Lunch on Thursday?", True
            ),
        }
        self.messages = {
            card["key"]: self._message(card, index)
            for index, card in enumerate(self.cards.values())
        }
        self.proposal = {
            "id": "p1",
            "time": TIME,
            "state": "pending",
            "text": t("Archive newsletters after a week.\nAlways flag mail from Kim.\n"),
            "diff": t(
                "--- policy\n+++ proposed\n@@ -1,2 +1,2 @@\n Archive newsletters after a week.\n"
                "-Flag nothing.\n+Always flag mail from Kim."
            ),
        }
        self.apply_policy_automatically = False
        self.policy_text = t("Archive newsletters after a week.\nFlag nothing.\n")
        self.policy_versions = [
            {"id": "v2", "time": TIME, "summary": t("Edited by you")},
            {"id": "v1", "time": "2026-11-20T08:00:00+00:00", "summary": t("First version")},
        ]
        self.policy_version_texts = {
            "v2": self.policy_text,
            "v1": t("Archive newsletters after a month.\n"),
        }
        self.change_requests = [
            {"id": "cr-1", "kind": "feature", "state": "approved", "time": TIME,
             "text": t("Please add a dark mode.")},
            {"id": "cr-2", "kind": "bug", "state": "pending", "time": TIME,
             "text": t("Parsing fails for a message with an empty Date header.")},
        ]  # fmt: skip
        self.coding_events = [
            {"seq": 1, "time": TIME, "request_id": "cr-1", "kind": "status",
             "text": t("Edit: /workspace/app/src/triage/web/app.css")},
            {"seq": 2, "time": TIME, "request_id": "cr-1", "kind": "text",
             "text": t("I have added a **dark** colour scheme.")},
            {"seq": 3, "time": TIME, "request_id": "cr-1", "kind": "done", "text": t("Done.")},
            {"seq": 4, "time": TIME, "request_id": "cr-1", "kind": "question",
             "text": t("Should the dark scheme follow the system setting?")},
        ]  # fmt: skip
        self.activity = [
            {"seq": 5, "time": TIME, "account": "work", "op": "draft",
             "summary": t("Wrote a draft reply to Kim Placeholder"), "undoable": False,
             "undone": False, "error": None},
            {"seq": 6, "time": TIME, "account": "work", "op": "flags",
             "summary": t("Marked 2 messages as read"), "undoable": True, "undone": True,
             "error": None},
            {"seq": 7, "time": TIME, "account": "work", "op": "move",
             "summary": t("Moved 3 messages from INBOX to Archive"), "undoable": True,
             "undone": False, "error": None},
            {"seq": 8, "time": TIME, "account": "work", "op": "move",
             "summary": t("Moved 1 message from INBOX to Trash"), "undoable": False,
             "undone": False, "error": t("the IMAP server refused the command")},
        ]  # fmt: skip
        self.conversations: dict[int, dict[str, Any]] = {
            1: {
                "id": 1,
                "title": t("This week's deadlines"),
                "updated": TIME,
                "items": [
                    self._item("user", t("What do I need to reply to this week?")),
                    self._item(
                        "tool",
                        "",
                        tool={
                            "name": "search_mail",
                            "ok": True,
                            "summary": t("Searched mail that needs a reply"),
                        },
                        messages=list(self.cards.values()),
                    ),
                    self._item(
                        "tool",
                        "",
                        tool={
                            "name": "move_messages",
                            "ok": False,
                            "summary": t("Tried to move 1 message to Trash"),
                        },
                    ),
                    self._item(
                        "assistant",
                        t(
                            "You have **two** messages that need a reply:\n\n"
                            "1. [m1] from Alex, about the *budget*\n"
                            "2. [m3] from Kim\n\n"
                            "Use `Archive` for [m2]. Unknown handle: [m99]."
                        ),
                        messages=[self.cards["m1"]],
                    ),
                    self._item(
                        "assistant",
                        t("I have asked for the app to be changed."),
                        change_request=dict(self.change_requests[1]),
                    ),
                    self._item(
                        "assistant",
                        t("I suggest changing your policy."),
                        policy_proposal=self.proposal,
                    ),
                    self._item("notice", t("The policy was applied to 4 new messages.")),
                ],
            }
        }

    # -- canned data ---------------------------------------------------------------------

    def t(self, text: str) -> str:
        """A text field: with hostile markup appended when the backend is hostile."""
        return text + HOSTILE if self.hostile else text

    def _card(
        self,
        handle: str,
        name: str,
        address: str,
        subject: str,
        unread: bool,
        flagged: bool = False,
    ) -> dict[str, Any]:
        return {
            "key": f"key-{handle}",
            "handle": handle,
            "account": "work",
            "folder": "INBOX",
            "subject": self.t(subject),
            "from": [{"name": self.t(name), "address": address}],
            "date": TIME,
            "preview": self.t(f"Preview of {subject.lower()} …"),
            "unread": unread,
            "flagged": flagged,
            "has_attachments": handle == "m1",
        }

    def _message(self, card: dict[str, Any], index: int) -> dict[str, Any]:
        has_html = index == 0
        return {
            "key": card["key"],
            "account": "work",
            "folder": "INBOX",
            "subject": card["subject"],
            "from": card["from"],
            "to": [{"name": self.t("Robin Tester"), "address": "robin@example.org"}],
            "cc": [{"name": "", "address": "lee@example.com"}] if has_html else [],
            "date": TIME,
            "flags": [] if card["unread"] else ["\\Seen"],
            "text": {
                "body": self.t("Hello Robin,\n\nplease find the figures attached.\n"),
                "signature": self.t("Alex Sample\nExample Corp"),
                "quoted": self.t("On Monday Robin wrote:\n> Could you send the figures?"),
            },
            "has_html": has_html,
            "blocked_remote_content": 2 if has_html else 0,
            "attachments": (
                [
                    {
                        "filename": self.t("figures.pdf"),
                        "content_type": "application/pdf",
                        "size": 48_213,
                    }
                ]
                if has_html
                else []
            ),
            "analysis": (
                {
                    "summary": self.t("Alex sent the budget figures and asks for a reply."),
                    "category": "work",
                    "needs_reply": True,
                    "urgency": "high",
                    "deadline": "2026-11-27",
                }
                if has_html
                else None
            ),
        }

    def email_html(self) -> str:
        if self.hostile:
            return HOSTILE_EMAIL_HTML
        return (
            '<div style="font-family: sans-serif"><p>Hello <b>Robin</b>,</p>'
            "<p>please find the figures attached.</p>"
            '<img data-triage-blocked-src="" alt="logo"><img data-triage-blocked-src="">'
            '<p><a href="https://example.com/report" rel="noopener noreferrer" '
            'target="_blank">The report</a></p></div>'
        )

    def _item(self, role: str, text: str, **extra: Any) -> dict[str, Any]:
        self.next_item += 1
        item: dict[str, Any] = {
            "id": self.next_item,
            "role": role,
            "time": TIME,
            "text": text,
            "tool": None,
            "messages": [],
            "change_request": None,
            "policy_proposal": None,
        }
        item.update(extra)
        return item

    def status(self) -> dict[str, Any]:
        sync: dict[str, Any] = {
            "state": "idle",
            "started": TIME,
            "finished": TIME,
            "messages_total": 240,
            "messages_done": 240,
            "errors": [self.t("Archive: the IMAP server did not answer in time")],
            "accounts": [
                {"account": "work", "error": None,
                 "folders": [{"folder": "INBOX", "state": "done", "messages_total": 240,
                              "messages_done": 240, "error": None}]}
            ],
        }  # fmt: skip
        if self.sync_running:
            sync.update(state="running", finished=None, messages_done=60, errors=[])
            sync["accounts"][0]["folders"][0].update(state="fetching", messages_done=60)
        return {
            "accounts": [
                {"id": "work", "address": self.t("robin@example.org"),
                 "display_name": self.t("Robin Tester")}
            ],
            "sync": sync,
            "llm": {"available": True, "model": self.t("stub-model"), "error": None},
            "counts": [
                {"account": "work", "folder": "INBOX", "total": 240, "unread": 12},
                {"account": "work", "folder": "Archive", "total": 31, "unread": 0},
            ],
            "notices": [
                {"kind": "deletion_rate", "time": TIME,
                 "text": self.t("23 messages were moved to the trash within an hour.")}
            ],
            "pending_change_requests": sum(r["state"] == "pending" for r in self.change_requests),
            "pending_policy_proposals": int(self.proposal["state"] == "pending"),
        }  # fmt: skip

    def reply_events(self, conversation: dict[str, Any], text: str) -> list[tuple[str, Any]]:
        """The events of the streamed answer to a chat message."""
        user = self._item("user", text)
        conversation["items"].append(user)
        events: list[tuple[str, Any]] = [("item", user)]
        if "fail" in text:
            events.append(("error", {"message": self.t("The local model is not available.")}))
            return events
        tool = self._item(
            "tool",
            "",
            tool={"name": "search_mail", "summary": self.t("Searched mail"), "ok": True},
            messages=[self.cards["m1"], self.cards["m3"]],
        )
        answer = self.t("You have **two** messages to answer: [m1] and [m3].")
        final = self._item("assistant", answer)
        conversation["items"] += [tool, final]
        events.append(("item", tool))
        step = max(1, len(answer) // 5)
        events += [("delta", {"text": answer[i : i + step]}) for i in range(0, len(answer), step)]
        events += [("item", final), ("done", {})]
        return events

    # -- helpers for tests ---------------------------------------------------------------

    def record(self, method: str, path: str, body: Any) -> None:
        with self._lock:
            self.requests.append((method, path, body))

    def seen(self, method: str, path: str) -> list[Any]:
        """The bodies of the recorded requests with this method and path (without query)."""
        with self._lock:
            return [b for m, p, b in self.requests if m == method and p.split("?")[0] == path]

    def paths(self) -> list[str]:
        with self._lock:
            return [p.split("?")[0] for _, p, _ in self.requests]

    def fail_next(self, method: str, path: str, status: int, code: str, message: str) -> None:
        self.failures[(method, path)] = Failure(status, code, message)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    backend: StubBackend

    def log_message(self, format: str, *args: Any) -> None:
        """Keep test output quiet."""

    # -- responses -----------------------------------------------------------------------

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, value: Any, status: int = 200) -> None:
        self._send(status, "application/json", json.dumps(value).encode())

    def _error(self, status: int, code: str, message: str) -> None:
        self._json({"error": {"code": code, "message": message}}, status)

    def _stream(self, events: list[tuple[str, Any]]) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        for name, data in events:
            self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())
            self.wfile.flush()
            time.sleep(self.backend.stream_delay)
        self.close_connection = True

    def _static(self, path: str) -> None:
        relative = "index.html" if path == "/" else path.lstrip("/")
        target = (WEB_DIR / relative).resolve()
        if WEB_DIR not in target.parents or not target.is_file():
            self._error(404, "not_found", "no such page")
            return
        content_type = CONTENT_TYPES.get(target.suffix, "application/octet-stream")
        self._send(200, content_type, target.read_bytes())

    # -- requests ------------------------------------------------------------------------

    def _body(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            return raw.decode(errors="replace")

    def _handle(self, method: str) -> None:
        backend = self.backend
        body = self._body() if method in ("POST", "PUT") else None
        backend.record(method, self.path, body)
        url = urlsplit(self.path)
        path = url.path
        query = {k: v[0] for k, v in parse_qs(url.query).items()}

        failure = backend.failures.pop((method, path), None)
        if failure is not None:
            self._error(failure.status, failure.code, failure.message)
            return
        if not path.startswith("/api/") and path != "/healthz":
            if method == "GET":
                self._static(path)
            else:
                self._error(405, "bad_request", "not allowed")
            return
        parts = [unquote(p) for p in path.strip("/").split("/")]
        try:
            self._api(method, parts, query, body)
        except (KeyError, ValueError, IndexError):
            self._error(404, "not_found", backend.t("no such thing"))

    def _api(self, method: str, parts: list[str], query: dict[str, str], body: Any) -> None:
        b = self.backend
        match (method, parts):
            case ("GET", ["healthz"]):
                self._json({"status": "ok"})
            case ("GET", ["api", "status"]):
                self._json(b.status())
            case ("POST", ["api", "sync"]):
                b.sync_running = True
                self._json({}, 202)
            case ("GET", ["api", "conversations"]):
                listing = [
                    {"id": c["id"], "title": c["title"], "updated": c["updated"]}
                    for c in sorted(b.conversations.values(), key=lambda c: -c["id"])
                ]
                self._json({"conversations": listing})
            case ("POST", ["api", "conversations"]):
                new_id = max(b.conversations) + 1
                b.conversations[new_id] = {
                    "id": new_id, "title": b.t("New conversation"), "updated": TIME, "items": [],
                }  # fmt: skip
                self._json({"id": new_id, "title": b.t("New conversation"), "updated": TIME})
            case ("GET", ["api", "conversations", cid]):
                self._json(b.conversations[int(cid)])
            case ("POST", ["api", "conversations", cid, "messages"]):
                self._stream(b.reply_events(b.conversations[int(cid)], str(body["text"])))
            case ("GET", ["api", "messages", key]):
                self._json(b.messages[key])
            case ("GET", ["api", "messages", key, "html"]):
                if not b.messages[key]["has_html"]:
                    raise KeyError(key)
                self._send(200, "text/html; charset=utf-8", b.email_html().encode())
            case ("GET", ["api", "search"]):
                self._json({"messages": list(b.cards.values())})
            case ("GET", ["api", "activity"]):
                after = int(query.get("after", 0))
                entries = [e for e in b.activity if e["seq"] > after]
                self._json({"entries": entries, "last_seq": b.activity[-1]["seq"]})
            case ("POST", ["api", "activity", seq, "undo"]):
                entry = next(e for e in b.activity if e["seq"] == int(seq))
                if not entry["undoable"] or entry["undone"]:
                    self._error(400, "bad_request", "this cannot be undone")
                    return
                entry["undone"] = True
                self._json({})
            case ("GET", ["api", "policy"]):
                self._json({"text": b.policy_text, "updated": TIME, "proposals": [b.proposal]})
            case ("PUT", ["api", "policy"]):
                b.policy_text = str(body["text"])
                self._json({})
            case ("GET", ["api", "settings"]):
                self._json({"apply_policy_automatically": b.apply_policy_automatically})
            case ("PUT", ["api", "settings"]):
                b.apply_policy_automatically = body["apply_policy_automatically"] is True
                self._json({"apply_policy_automatically": b.apply_policy_automatically})
            case ("GET", ["api", "policy", "history"]):
                self._json({"versions": b.policy_versions})
            case ("GET", ["api", "policy", "history", version]):
                self._json({"text": b.policy_version_texts[version]})
            case ("POST", ["api", "policy", "proposals", pid]):
                if pid != b.proposal["id"] or b.proposal["state"] != "pending":
                    raise KeyError(pid)
                if body["accept"]:
                    b.proposal["state"] = "accepted"
                    b.policy_text = b.proposal["text"]
                else:
                    b.proposal["state"] = "rejected"
                self._json({})
            case ("GET", ["api", "coding"]):
                after = int(query.get("after", 0))
                events = [e for e in b.coding_events if e["seq"] > after]
                if not events and float(query.get("wait", 0)) > 0:
                    time.sleep(0.2)  # a real backend would wait for news; tests should not
                self._json({
                    "events": events,
                    "last_seq": b.coding_events[-1]["seq"],
                    "requests": b.change_requests,
                })  # fmt: skip
            case ("POST", ["api", "coding", "requests"]):
                request = {
                    "id": f"cr-{len(b.change_requests) + 1}",
                    "kind": body["kind"],
                    "state": "pending",
                    "time": TIME,
                    "text": str(body["text"]),
                }
                b.change_requests.append(request)
                self._json({"id": request["id"], "state": "pending"}, 202)
            case _:
                self._error(404, "not_found", "no such endpoint")

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PUT(self) -> None:
        self._handle("PUT")


class StubServer:
    """Runs a :class:`StubBackend` on a local port in a background thread."""

    def __init__(self, backend: StubBackend | None = None, port: int = 0) -> None:
        self.backend = backend or StubBackend()
        handler = type("Handler", (_Handler,), {"backend": self.backend})
        self._server = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self._server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--hostile", action="store_true")
    arguments = parser.parse_args()
    server = StubServer(StubBackend(hostile=arguments.hostile), arguments.port)
    print(f"Serving the web interface with canned data at {server.url}/")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.close()
