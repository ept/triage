"""The app's web server: the web interface and the API behind it.

It listens on plain HTTP inside the email sandbox. The only way in is the gateway's relay,
which requires the wrapper app's session and adds a strict Content-Security-Policy. All URLs
are relative.

Background work runs in three threads: keeping the local copy of the mail in sync, analysing
new messages with the local model, and listening for notifications from the gateway.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
from collections import deque
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from triage import activity
from triage.agent import Agent
from triage.analysis import Analyser
from triage.chats import ChatStore, Item, decode_key, message_card
from triage.gateway import GatewayClient, GatewayError, NotFoundError
from triage.llm import LlmClient, LlmError
from triage.mime import split_quoted
from triage.policy import PolicyError, PolicyStore
from triage.settings import SettingsStore
from triage.store import MessageSummary, Store
from triage.sync import SyncEngine, SyncSnapshot

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"
SYNC_INTERVAL = 300.0
#: The policy is applied automatically only to mail that arrived this recently.
AUTOMATIC_MAX_AGE = timedelta(days=2)
MAX_CHAT_TEXT = 20_000

#: For the document that shows an HTML email inside a sandboxed frame: nothing is loaded from
#: anywhere, and no script runs, even if the document is opened on its own.
EMAIL_CSP = "default-src 'none'; img-src data:; style-src 'unsafe-inline'; sandbox"

EMAIL_DOCUMENT = """<!doctype html>
<html><head><meta charset="utf-8">
<style>
body {{ font-family: -apple-system, system-ui, sans-serif; font-size: 14px; line-height: 1.45;
       color: #1b1b1b; background: #fff; margin: 12px; overflow-wrap: anywhere; }}
img {{ max-width: 100%; height: auto; }}
img[data-triage-blocked-src] {{ display: inline-block; min-width: 24px; min-height: 24px;
       border: 1px dashed #bbb; background: #f3f3f3; }}
blockquote {{ margin: 8px 0 8px 8px; padding-left: 10px; border-left: 2px solid #ccc;
       color: #555; }}
table {{ max-width: 100%; }}
</style></head><body>{body}</body></html>"""


class Problem(Exception):
    """An error to be reported to the interface."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _problem_response(problem: Problem) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": problem.code, "message": problem.message}}, status_code=problem.status
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _sync_json(snapshot: SyncSnapshot) -> dict[str, Any]:
    return {
        "state": snapshot.state,
        "started": _iso(snapshot.started_at),
        "finished": _iso(snapshot.finished_at),
        "messages_total": snapshot.messages_total,
        "messages_done": snapshot.messages_done,
        "errors": snapshot.errors,
        "accounts": [
            {
                "account": account.account,
                "error": account.error,
                "folders": [
                    {
                        "folder": folder.folder,
                        "state": folder.state,
                        "messages_total": folder.messages_total,
                        "messages_done": folder.messages_done,
                        "error": folder.error,
                    }
                    for folder in account.folders
                ],
            }
            for account in snapshot.accounts
        ],
    }


def _addresses(addresses: Any) -> list[dict[str, str]]:
    return [{"name": a.name, "address": a.address} for a in addresses]


class App:
    """Everything the app consists of, wired together."""

    def __init__(
        self,
        *,
        gateway_url: str | None = None,
        cache_dir: str | os.PathLike[str] | None = None,
        config_dir: str | os.PathLike[str] | None = None,
        transport: httpx.BaseTransport | None = None,
        web_dir: Path | None = None,
    ):
        self.gateway = GatewayClient(gateway_url, transport=transport)
        self.llm = LlmClient(gateway_url, transport=transport)
        self.store = Store(cache_dir)
        self.chats = ChatStore(cache_dir or self.store.cache_dir)
        self.policy = PolicyStore(config_dir)
        self.settings = SettingsStore(config_dir)
        self.sync = SyncEngine(self.gateway, self.store)
        self.analyser = Analyser(self.llm, self.store, user_name=self._user_name)
        self._sync_wanted = threading.Event()
        self.agent = Agent(
            llm=self.llm,
            store=self.store,
            gateway=self.gateway,
            chats=self.chats,
            policy=self.policy,
            on_mail_changed=self._sync_wanted.set,
        )
        self._agent_lock = threading.Lock()
        self._stopping = threading.Event()
        self._threads: list[threading.Thread] = []
        self._notices: deque[dict[str, str]] = deque(maxlen=20)
        self._cache_lock = threading.Lock()
        self._accounts_cache: tuple[float, list[dict[str, Any]]] = (0.0, [])
        self._llm_cache: tuple[float, dict[str, Any]] = (0.0, {})
        self.web_dir = web_dir or WEB_DIR
        self.asgi = self._build()

    # -- background work -----------------------------------------------------------------

    def start(self) -> None:
        """Start the background threads."""
        self._sync_wanted.set()
        for target in (self._sync_loop, self._analysis_loop, self._events_loop):
            thread = threading.Thread(target=target, name=target.__name__, daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stopping.set()
        self._sync_wanted.set()

    def close(self) -> None:
        self.stop()
        for thread in self._threads:
            thread.join(timeout=5)
        self.gateway.close()
        self.llm.close()
        self.chats.close()
        self.store.close()

    def _sync_loop(self) -> None:
        while not self._stopping.is_set():
            self._sync_wanted.wait(timeout=SYNC_INTERVAL)
            self._sync_wanted.clear()
            if self._stopping.is_set():
                return
            try:
                self.sync.sync_all()
            except Exception:
                logger.exception("sync failed")
                self._stopping.wait(10)

    def _analysis_loop(self) -> None:
        while not self._stopping.wait(2):
            if self._agent_lock.locked():
                continue  # the user's question comes first
            fresh: list[MessageSummary] = []
            try:
                done = self.analyser.run_pending(
                    limit=5, should_stop=self._agent_lock.locked, on_analysed=fresh.append
                )
                for summary in fresh:
                    self.apply_policy_to(summary)
            except LlmError:
                self._stopping.wait(30)
                continue
            except Exception:
                logger.exception("analysis failed")
                self._stopping.wait(60)
                continue
            if done == 0:
                self._stopping.wait(20)

    def apply_policy_to(self, summary: MessageSummary) -> bool:
        """Apply the policy to a message that has just arrived, if the user wants that.

        Only unread mail that reached the inbox recently is considered, so that switching the
        setting on does not set the model loose on everything that is already there.
        """
        if not self.settings.get().apply_policy_automatically:
            return False
        key = summary.key
        folder = self.store.folder(key.account, key.folder)
        recent = summary.internal_date >= datetime.now(UTC) - AUTOMATIC_MAX_AGE
        if folder is None or folder.role != "inbox" or summary.seen or not recent:
            return False
        # The user's own questions come first; wait for the assistant to be free.
        if not self._agent_lock.acquire(timeout=300):
            return False
        try:
            return self.agent.apply_policy(key)
        finally:
            self._agent_lock.release()

    def _events_loop(self) -> None:
        seq = 0
        while not self._stopping.is_set():
            try:
                page = self.gateway.events(after=seq, wait=25)
            except GatewayError:
                self._stopping.wait(5)
                continue
            except Exception:
                logger.exception("reading events failed")
                self._stopping.wait(30)
                continue
            seq = max(seq, page.last_seq)
            for event in page.events:
                self._handle_event(event.kind, event.time, event.data)

    def _handle_event(self, kind: str, when: datetime, data: Mapping[str, Any]) -> None:
        if kind == "deletion_rate":
            self._notices.append(
                {
                    "kind": kind,
                    "time": when.isoformat(),
                    "text": (
                        f"{data.get('count')} messages were moved to the trash in the account "
                        f"{data.get('account')} within {data.get('window_minutes')} minutes."
                    ),
                }
            )
        elif kind == "accounts_changed":
            with self._cache_lock:
                self._accounts_cache = (0.0, [])
            self._sync_wanted.set()
        elif kind == "change_request":
            request_id, state = data.get("id"), data.get("state")
            if isinstance(request_id, str) and isinstance(state, str):
                self.chats.set_change_request_state(request_id, state)

    # -- cached lookups ------------------------------------------------------------------

    def _accounts(self) -> list[dict[str, Any]]:
        with self._cache_lock:
            fetched, accounts = self._accounts_cache
        if time.monotonic() - fetched < 5:
            return accounts
        try:
            accounts = [
                {"id": a.id, "address": a.address, "display_name": a.display_name}
                for a in self.gateway.accounts()
            ]
        except GatewayError:
            return accounts
        with self._cache_lock:
            self._accounts_cache = (time.monotonic(), accounts)
        return accounts

    def _user_name(self) -> str:
        names = [a["display_name"] for a in self._accounts() if a["display_name"]]
        return names[0] if names else "the user"

    def _llm_status(self) -> dict[str, Any]:
        with self._cache_lock:
            fetched, status = self._llm_cache
        if status and time.monotonic() - fetched < 10:
            return status
        try:
            self.llm.forget_model()
            status = {"available": True, "model": self.llm.model(), "error": None}
        except LlmError as exc:
            status = {"available": False, "model": None, "error": str(exc)}
        with self._cache_lock:
            self._llm_cache = (time.monotonic(), status)
        return status

    def _refresh_change_requests(self) -> list[dict[str, Any]]:
        requests = self.chats.change_requests()
        for request in requests:
            if request["state"] != "pending":
                continue
            try:
                state = self.gateway.change_request(request["id"]).state
            except NotFoundError:
                state = "expired"  # the gateway was restarted and has forgotten it
            except GatewayError:
                continue
            if state != request["state"]:
                self.chats.set_change_request_state(request["id"], state)
                request["state"] = state
        return requests

    def _item_json(self, item: Item, requests: Mapping[str, str]) -> dict[str, Any]:
        """An item as the interface sees it, with the current state of what it refers to."""
        data = item.to_json()
        change_request = data["change_request"]
        if change_request and change_request.get("id") in requests:
            data["change_request"] = {**change_request, "state": requests[change_request["id"]]}
        proposal = data["policy_proposal"]
        if proposal:
            current = self.policy.proposal(proposal.get("id", ""))
            if current is not None:
                data["policy_proposal"] = {**proposal, "state": current.state}
        return data

    # -- routes --------------------------------------------------------------------------

    def _build(self) -> Starlette:
        routes = [
            Route("/healthz", self.healthz),
            Route("/api/status", self.status),
            Route("/api/sync", self.start_sync, methods=["POST"]),
            Route("/api/assistant/stop", self.stop_assistant, methods=["POST"]),
            Route("/api/conversations", self.conversations, methods=["GET", "POST"]),
            Route("/api/conversations/{conversation_id:int}", self.conversation),
            Route(
                "/api/conversations/{conversation_id:int}/messages",
                self.post_message,
                methods=["POST"],
            ),
            Route("/api/messages/{key}", self.message),
            Route("/api/messages/{key}/html", self.message_html),
            Route("/api/search", self.search),
            Route("/api/activity", self.activity),
            Route("/api/activity/{seq:int}/undo", self.undo, methods=["POST"]),
            Route("/api/policy", self.policy_endpoint, methods=["GET", "PUT"]),
            Route("/api/policy/history", self.policy_history),
            Route("/api/policy/history/{version}", self.policy_version),
            Route("/api/policy/proposals/{proposal_id}", self.decide_proposal, methods=["POST"]),
            Route("/api/settings", self.settings_endpoint, methods=["GET", "PUT"]),
            Route("/api/coding", self.coding),
            Route("/api/coding/requests", self.coding_request, methods=["POST"]),
            Route("/api/{rest:path}", self.not_found, methods=["GET", "POST", "PUT", "DELETE"]),
            Mount("/", app=StaticFiles(directory=self.web_dir, html=True, check_dir=False)),
        ]
        app = Starlette(routes=routes)
        app.add_exception_handler(Problem, self._on_problem)
        app.add_exception_handler(GatewayError, self._on_gateway_error)
        app.add_exception_handler(PolicyError, self._on_policy_error)
        app.add_middleware(_HeadersMiddleware)
        return app

    @staticmethod
    async def _on_problem(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, Problem)
        return _problem_response(exc)

    @staticmethod
    async def _on_gateway_error(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, GatewayError)
        status = exc.status if exc.status and 400 <= exc.status < 600 else 502
        return _problem_response(Problem(status, exc.code, exc.message))

    @staticmethod
    async def _on_policy_error(request: Request, exc: Exception) -> JSONResponse:
        return _problem_response(Problem(400, "bad_request", str(exc)))

    @staticmethod
    async def _body(request: Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except ValueError as exc:
            raise Problem(400, "bad_request", "The request body is not valid JSON.") from exc
        if not isinstance(body, dict):
            raise Problem(400, "bad_request", "The request body must be an object.")
        return body

    def not_found(self, request: Request) -> Response:
        raise Problem(404, "not_found", "There is no such endpoint.")

    def healthz(self, request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    def status(self, request: Request) -> Response:
        requests = self.chats.change_requests()
        return JSONResponse(
            {
                "accounts": self._accounts(),
                "sync": _sync_json(self.sync.progress.snapshot()),
                "llm": self._llm_status(),
                "counts": [
                    {"account": c.account, "folder": c.folder, "total": c.total, "unread": c.unread}
                    for c in self.store.counts()
                ],
                "notices": list(self._notices),
                "pending_change_requests": sum(r["state"] == "pending" for r in requests),
                "pending_policy_proposals": sum(
                    p.state == "pending" for p in self.policy.proposals()
                ),
            }
        )

    def start_sync(self, request: Request) -> Response:
        self._sync_wanted.set()
        return JSONResponse({}, status_code=202)

    # -- conversations

    async def conversations(self, request: Request) -> Response:
        if request.method == "POST":
            return JSONResponse(self.chats.create().to_json())
        listed = [c.to_json() for c in self.chats.conversations()]
        return JSONResponse({"conversations": listed})

    def conversation(self, request: Request) -> Response:
        conversation_id = request.path_params["conversation_id"]
        conversation = self.chats.conversation(conversation_id)
        if conversation is None:
            raise Problem(404, "not_found", "There is no such conversation.")
        states = {r["id"]: r["state"] for r in self._refresh_change_requests()}
        items = [self._item_json(item, states) for item in self.chats.items(conversation_id)]
        return JSONResponse({**conversation.to_json(), "items": items})

    async def post_message(self, request: Request) -> Response:
        conversation_id = request.path_params["conversation_id"]
        body = await self._body(request)
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise Problem(400, "bad_request", "The message is empty.")
        if len(text) > MAX_CHAT_TEXT:
            raise Problem(400, "bad_request", "The message is too long.")
        if self.chats.conversation(conversation_id) is None:
            raise Problem(404, "not_found", "There is no such conversation.")
        return StreamingResponse(
            self._reply_stream(conversation_id, text),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    async def stop_assistant(self, request: Request) -> Response:
        """End what the assistant is doing for the user, as soon as it can be interrupted."""
        if self._agent_lock.locked():
            self.agent.stop()
        return JSONResponse({})

    def _reply_stream(self, conversation_id: int, text: str) -> Iterator[bytes]:
        def frame(kind: str, data: Mapping[str, Any]) -> bytes:
            return f"event: {kind}\ndata: {json.dumps(data)}\n\n".encode()

        if not self._agent_lock.acquire(blocking=False):
            yield frame("error", {"message": "The assistant is still busy with another question."})
            yield frame("done", {})
            return
        try:
            for event in self.agent.reply(conversation_id, text):
                yield frame(event.kind, event.data)
        except Exception:
            logger.exception("the assistant failed")
            yield frame("error", {"message": "Something went wrong inside the app."})
            yield frame("done", {})
        finally:
            self._agent_lock.release()

    # -- messages

    def _key(self, request: Request) -> Any:
        key = decode_key(request.path_params["key"])
        if key is None:
            raise Problem(404, "not_found", "There is no such message.")
        return key

    def message(self, request: Request) -> Response:
        key = self._key(request)
        message = self.store.get_message(key)
        if message is None:
            raise Problem(404, "not_found", "The message is no longer in this folder.")
        parsed = self.store.get_parsed(key)
        split = split_quoted(message.body)
        analysis = self.store.get_analysis(key)
        result = analysis.result if analysis and "summary" in analysis.result else None
        return JSONResponse(
            {
                "key": request.path_params["key"],
                "account": key.account,
                "folder": key.folder,
                "subject": message.subject,
                "from": _addresses(message.from_),
                "to": _addresses(message.to),
                "cc": _addresses(message.cc),
                "date": message.timestamp.isoformat(),
                "flags": list(message.flags),
                "text": {"body": split.body, "signature": split.signature, "quoted": split.quoted},
                "has_html": bool(parsed and parsed.html),
                "blocked_remote_content": parsed.blocked_remote_content if parsed else 0,
                "attachments": [
                    {
                        "filename": a.filename or "",
                        "content_type": a.content_type,
                        "size": a.size,
                    }
                    for a in message.attachments
                    if not a.inline
                ],
                "analysis": dict(result) if result else None,
            }
        )

    def message_html(self, request: Request) -> Response:
        parsed = self.store.get_parsed(self._key(request))
        if parsed is None or not parsed.html:
            raise Problem(404, "not_found", "The message has no HTML part.")
        return HTMLResponse(
            EMAIL_DOCUMENT.format(body=parsed.html),
            headers={"Content-Security-Policy": EMAIL_CSP, "Cache-Control": "no-store"},
        )

    def search(self, request: Request) -> Response:
        params = request.query_params
        try:
            limit = max(1, min(int(params.get("limit", "30")), 100))
        except ValueError as exc:
            raise Problem(400, "bad_request", "limit must be a number.") from exc
        query = params.get("q", "")
        found = self.store.search(
            query,
            account=params.get("account") or None,
            folder=params.get("folder") or None,
            unread=True if params.get("unread") in ("1", "true") else None,
            order="relevance" if query.strip() else "date",
            limit=limit,
        )
        return JSONResponse({"messages": [message_card(m) for m in found]})

    # -- activity

    def activity(self, request: Request) -> Response:
        try:
            after = int(request.query_params.get("after", "0"))
        except ValueError as exc:
            raise Problem(400, "bad_request", "after must be a number.") from exc
        page = self.gateway.log(after=after, limit=1000)
        undone = self.chats.undone()
        entries = [
            {
                "seq": entry.seq,
                "time": entry.time.isoformat(),
                "account": entry.account,
                "op": entry.op,
                "summary": activity.describe(entry),
                "undoable": activity.undoable(entry),
                "undone": entry.seq in undone,
                "error": entry.data.get("error"),
            }
            for entry in page.entries
        ]
        return JSONResponse({"entries": entries, "last_seq": page.last_seq})

    def undo(self, request: Request) -> Response:
        seq = request.path_params["seq"]
        page = self.gateway.log(after=seq - 1, limit=1)
        entry = next((e for e in page.entries if e.seq == seq), None)
        if entry is None:
            raise Problem(404, "not_found", "There is no such entry.")
        if seq in self.chats.undone():
            raise Problem(400, "bad_request", "This has already been undone.")
        try:
            activity.undo(self.gateway, entry)
        except activity.UndoError as exc:
            raise Problem(400, "bad_request", "This cannot be undone.") from exc
        self.chats.mark_undone(seq)
        self._sync_wanted.set()
        return JSONResponse({})

    # -- policy

    async def policy_endpoint(self, request: Request) -> Response:
        if request.method == "PUT":
            body = await self._body(request)
            text = body.get("text")
            if not isinstance(text, str):
                raise Problem(400, "bad_request", "text is missing.")
            self.policy.save(text, author="user")
            return JSONResponse({})
        proposals = [
            {"id": p.id, "time": p.time, "text": p.text, "diff": p.diff, "state": p.state}
            for p in self.policy.proposals()
        ]
        return JSONResponse(
            {
                "text": self.policy.text(),
                "updated": _iso(self.policy.updated()),
                "proposals": proposals,
            }
        )

    def policy_history(self, request: Request) -> Response:
        versions = [
            {"id": v.id, "time": v.time.isoformat(), "summary": v.summary}
            for v in self.policy.history()
        ]
        return JSONResponse({"versions": versions})

    def policy_version(self, request: Request) -> Response:
        try:
            text = self.policy.version(request.path_params["version"])
        except PolicyError as exc:
            raise Problem(404, "not_found", "There is no such version.") from exc
        return JSONResponse({"text": text})

    async def decide_proposal(self, request: Request) -> Response:
        body = await self._body(request)
        accept = body.get("accept")
        if not isinstance(accept, bool):
            raise Problem(400, "bad_request", "accept must be true or false.")
        self.policy.decide(request.path_params["proposal_id"], accept)
        return JSONResponse({})

    async def settings_endpoint(self, request: Request) -> Response:
        if request.method == "PUT":
            try:
                self.settings.update(await self._body(request))
            except ValueError as exc:
                raise Problem(400, "bad_request", str(exc)) from exc
        current = self.settings.get()
        return JSONResponse({"apply_policy_automatically": current.apply_policy_automatically})

    # -- coding agent

    def coding(self, request: Request) -> Response:
        params = request.query_params
        try:
            after = int(params.get("after", "0"))
            wait = max(0.0, min(float(params.get("wait", "0")), 30.0))
        except ValueError as exc:
            raise Problem(400, "bad_request", "after and wait must be numbers.") from exc
        page = self.gateway.coding_events(after=after, wait=wait)
        events = [
            {
                "seq": event.seq,
                "time": event.time.isoformat(),
                "request_id": event.data.get("request_id"),
                "kind": event.kind,
                "text": event.text,
            }
            for event in page.events
        ]
        return JSONResponse(
            {
                "events": events,
                "last_seq": page.last_seq,
                "requests": self._refresh_change_requests(),
            }
        )

    async def coding_request(self, request: Request) -> Response:
        body = await self._body(request)
        kind, text = body.get("kind"), body.get("text")
        if kind not in ("feature", "reply") or not isinstance(text, str) or not text.strip():
            raise Problem(400, "bad_request", "kind must be feature or reply, with a text.")
        submitted = self.gateway.submit_change_request(kind, text.strip())
        self.chats.record_change_request(submitted.id, kind, text.strip(), submitted.state)
        return JSONResponse({"id": submitted.id, "state": submitted.state}, status_code=202)


class _HeadersMiddleware:
    """Adds headers that apply to every response."""

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Any) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                names = {name.lower() for name, _ in headers}
                headers.append((b"x-content-type-options", b"nosniff"))
                if b"cache-control" not in names:
                    # The app changes whenever the coding agent updates it.
                    headers.append((b"cache-control", b"no-cache"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


def main() -> None:
    """Run the app as it runs inside the email sandbox."""
    import uvicorn

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    # Request lines contain message identifiers, which name folders: keep them out of the log.
    app = App()
    app.start()
    with contextlib.suppress(KeyboardInterrupt):
        uvicorn.run(app.asgi, host="0.0.0.0", port=8080, access_log=False, log_level="warning")  # noqa: S104
    app.close()


__all__ = ["App", "Problem", "main"]
